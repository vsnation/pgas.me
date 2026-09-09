"""Background workers — started from the app lifespan when PGAS_WORKERS_ENABLED=1.

  deposit_watcher (15 s)  PRIMARY: scan the pipes on-chain (scanner.py) → locked → confirming → credited
                          SECONDARY: the DLN API for what the chain cannot show —
                          submitted → order_seen (order id), cancelled → failed,
                          filled-but-no-lock → fallback_pending
  monitor         (60 s)  Telegram (default-deny) for queued events, stuck states, upstream reachability
  stats_refresher (60 s)  the shielded-pool numbers from the explorer (served by /v1/stats)

One loop helper, `run_forever`, owns what every loop shares: the kill switch (PGAS_STOP_FILE —
while it exists nothing changes and the operator hears so once an hour), the catch-log-alert
of a pass that raised (15 min cooldown), and the sleep.

Evidence rule (the only one that moves money): a deposit is credited ONLY after the pipe's
NewLocalMessage log naming OUR pubkey and EXACTLY the deposit's value is found and has
`lock_confirmations` blocks on top. A DLN status of Fulfilled by itself is never enough.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from . import dln, ethpipe, ledger, scanner, tg
from .assets import ASSETS
from .config import settings
from .db import db

log = logging.getLogger("pgasme.workers")

SUBMITTED_GRACE_S = 120  # the chain is asked first; DLN's order-ids only after this
UNFILLED_AFTER_S = 20 * 60  # a `Created` order this old gets a note (a solver may still fill it)
UNFILLED_ALERT_COOLDOWN_S = 6 * 3600
UNFILLED_NOTE = (
    "not filled yet — a solver can still fill it; you can cancel from the deBridge order page "
    "for a refund"
)
_started_at = time.time()
_rpc: dict[str, ethpipe.Rpc | None] = {"rpc": None}


def paused() -> bool:
    return os.path.exists(settings.stop_file)


def get_rpc() -> ethpipe.Rpc:
    if _rpc["rpc"] is None:
        _rpc["rpc"] = ethpipe.Rpc()
    return _rpc["rpc"]


async def _set(deposit_id: str, status: str | None = None, **fields: Any) -> None:
    upd = {"updated_at": time.time(), **fields}
    if status:
        upd["status"] = status
    await db().deposits.update_one({"_id": deposit_id}, {"$set": upd})


# ----------------------------------------------------------------------------- confirmations → credit


async def _credit(dep: dict[str, Any], confirmations: int) -> None:
    now = time.time()
    if await ledger.has_credit(dep["_id"]):
        await _set(
            dep["_id"],
            "credited",
            ledger_ok=True,
            confirmations=confirmations,
            credited_at=dep.get("credited_at") or now,
        )
        return
    claimed = await db().deposits.find_one_and_update(
        {"_id": dep["_id"], "status": {"$in": ["locked", "confirming"]}},
        {
            "$set": {
                "status": "credited",
                "credited_at": now,
                "updated_at": now,
                "confirmations": confirmations,
            }
        },
    )
    if not claimed:
        return  # another pass got there first
    try:
        await ledger.credit(
            dep["account_id"],
            dep["asset"],
            int(dep["value_groth"]),
            dep["_id"],
            f"lock msg {dep['eth'].get('msg_id')} in {dep['eth'].get('tx')}",
        )
    except Exception:
        # the balance write failed: hand the deposit back so the next pass retries (has_credit guards doubles)
        await _set(dep["_id"], "confirming", note="balance write failed; retrying")
        raise
    await _set(dep["_id"], ledger_ok=True)
    await tg.queue(
        "deposit_credited",
        f"Deposit credited: {dep['asset']} {tg.fmt_groth(int(dep['value_groth']))}",
        deposit_id=dep["_id"],
    )


async def _step_confirm(dep: dict[str, Any], head: int) -> None:
    block = dep["eth"].get("block")
    if block is None:
        raise RuntimeError(f"deposit {dep['_id']} is {dep['status']} without a lock block")
    confirmations = head - int(block) + 1
    if confirmations < settings.lock_confirmations:
        if dep["status"] != "confirming" or dep.get("confirmations") != confirmations:
            await _set(dep["_id"], "confirming", confirmations=confirmations)
        return
    await _credit(dep, confirmations)


async def confirm_locked(head: int | None = None) -> int:
    cur = (
        db()
        .deposits.find({"status": {"$in": ["locked", "confirming"]}})
        .sort("locked_at", 1)
        .limit(100)
    )
    rows = await cur.to_list(100)
    if not rows:
        return 0
    if head is None:
        head = await get_rpc().block_number()
    n = 0
    for dep in rows:
        try:
            await _step_confirm(dep, head)
            n += 1
        except Exception as e:  # noqa: BLE001 — one deposit must not block the others
            log.exception("confirm %s: %s", dep["_id"], e)
            await tg.send(
                f"Confirmation step failed on <code>{dep['_id']}</code>: "
                f"{tg.esc(f'{type(e).__name__}: {e}')[:300]}",
                key=f"dep-err:{dep['_id']}",
                cooldown_s=900,
            )
    return n


async def reconcile_credits() -> None:
    """A deposit marked credited whose balance entry never landed (a crash between the two
    writes) gets its entry now; one that has it gets ledger_ok so it is never looked at again."""
    cur = db().deposits.find({"status": "credited", "ledger_ok": {"$ne": True}}).limit(50)
    for dep in await cur.to_list(50):
        if not await ledger.has_credit(dep["_id"]):
            await ledger.credit(
                dep["account_id"],
                dep["asset"],
                int(dep["value_groth"]),
                dep["_id"],
                "reconciled: credited status without a balance entry",
            )
            await tg.queue(
                "deposit_credited",
                f"Deposit credited (reconciled): {dep['asset']}",
                deposit_id=dep["_id"],
            )
        await _set(dep["_id"], ledger_ok=True)


# ----------------------------------------------------------------------------- SECONDARY: DLN API


async def _step_submitted(dep: dict[str, Any]) -> None:
    if time.time() - float(dep.get("created_at") or 0) < SUBMITTED_GRACE_S:
        return
    ids = await dln.order_ids_by_tx(dep["src_tx_hash"])
    if not ids:
        return  # not indexed yet; the monitor pages after 60 min
    order_id = dep.get("order_id") if dep.get("order_id") in ids else ids[0]
    await _set(dep["_id"], "order_seen", order_id=order_id, order_seen_at=time.time())


async def _step_order_seen(dep: dict[str, Any]) -> None:
    status = (await dln.order_status(dep["order_id"])).get("status")
    now = time.time()
    if status in dln.TERMINAL_CANCELLED:
        note = "order cancelled — funds refunded to you on the source chain; nothing was locked"
        await _set(dep["_id"], "failed", dln_status=status, note=note)
        await tg.queue(
            "deposit_failed", f"Deposit failed: DLN order {status}", deposit_id=dep["_id"]
        )
        return
    if status not in dln.TERMINAL_OK:
        fields: dict[str, Any] = {"dln_status": status}
        seen_at = float(dep.get("order_seen_at") or dep.get("created_at") or now)
        if status == "Created" and now - seen_at > UNFILLED_AFTER_S:
            # EthPipe.sendFunds needs msg.value == value + relayerFee EXACTLY; a solver that
            # over-fills makes the native hook revert and the order simply stays open.
            fields["note"] = UNFILLED_NOTE
            if now - float(dep.get("unfilled_alert_at") or 0) > UNFILLED_ALERT_COOLDOWN_S:
                await tg.queue(
                    "deposit_unfilled",
                    "Deposit order still Created after 20 min — no solver has filled it",
                    deposit_id=dep["_id"],
                )
                fields["unfilled_alert_at"] = now
        await _set(dep["_id"], **fields)
        return
    fulfilled_at = float(dep.get("fulfilled_seen_at") or now)
    if now - fulfilled_at > settings.deposit_fallback_after_s:
        note = (
            f"hook did not run — the {dep['asset']} is in your own wallet (the order's fallback recipient); "
            "nothing is credited here. If the pipe lock turns up later the deposit resumes on its own."
        )
        await _set(
            dep["_id"],
            "fallback_pending",
            dln_status=status,
            fulfilled_seen_at=fulfilled_at,
            note=note,
        )
        await tg.queue(
            "deposit_fallback",
            "Deposit filled WITHOUT a pipe lock: funds are in the user's own wallet",
            deposit_id=dep["_id"],
        )
        return
    await _set(dep["_id"], dln_status=status, fulfilled_seen_at=fulfilled_at)


async def dln_secondary() -> int:
    cur = (
        db()
        .deposits.find({"status": {"$in": ["submitted", "order_seen"]}})
        .sort("created_at", 1)
        .limit(100)
    )
    n = 0
    for dep in await cur.to_list(100):
        try:
            if dep["status"] == "submitted":
                if dep.get("src_tx_hash"):
                    await _step_submitted(dep)
            else:
                await _step_order_seen(dep)
            n += 1
        except Exception as e:  # noqa: BLE001 — one bad deposit must not block the others
            log.exception("dln step %s: %s", dep["_id"], e)
            await tg.send(
                f"DLN step failed on <code>{dep['_id']}</code>: {tg.esc(f'{type(e).__name__}: {e}')[:300]}",
                key=f"dep-err:{dep['_id']}",
                cooldown_s=900,
            )
    return n


# ----------------------------------------------------------------------------- the deposit pass


async def deposit_watcher_once() -> dict[str, Any]:
    rpc = get_rpc()
    out: dict[str, Any] = {"scans": []}
    for asset in ASSETS.values():  # PRIMARY: the chain
        try:
            out["scans"].append(await scanner.scan_pipe(asset, rpc))
        except Exception as e:  # noqa: BLE001 — one pipe must not stall the others
            log.exception("scan %s: %s", asset.key, e)
            await tg.send(
                f"Pipe scan failed for {asset.key} and will retry: {tg.esc(f'{type(e).__name__}: {e}')[:300]}",
                key=f"scan-err:{asset.key}",
                cooldown_s=900,
            )
    out["late_attributed"] = await scanner.retry_unattributed(rpc)
    out["confirmed"] = await confirm_locked()
    await reconcile_credits()
    out["dln"] = await dln_secondary()  # SECONDARY: what the chain cannot show
    return out


# ----------------------------------------------------------------------------- pool stats (explorer)

pool_health: dict[str, Any] = {"last_ok_at": 0.0, "last_fail_at": 0.0, "last_error": ""}
_pool: dict[str, Any] = {"at": 0.0, "data": None, "stale": True}
POOL_TTL_S = 60


def clear_pool_cache() -> None:
    _pool.update({"at": 0.0, "data": None, "stale": True})


async def fetch_pool_status() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{settings.explorer_base.rstrip('/')}/status")
    if r.status_code != 200:
        raise RuntimeError(f"explorer HTTP {r.status_code}")
    body = r.json()
    for k in ("height", "shielded_outputs_total", "shielded_outputs_per_24h"):
        if k not in body:
            raise RuntimeError(f"explorer status lacks {k}")
    return {
        "height": int(body["height"]),
        "shielded_outputs_total": int(body["shielded_outputs_total"]),
        "shielded_outputs_per_24h": int(body["shielded_outputs_per_24h"]),
        "at": time.time(),
    }


def pool_current() -> dict[str, Any]:
    d = _pool["data"]
    if d is None:
        return {
            "height": None,
            "shielded_outputs_total": None,
            "shielded_outputs_per_24h": None,
            "at": None,
            "stale": True,
        }
    return {**d, "stale": bool(_pool["stale"])}


async def refresh_pool() -> dict[str, Any]:
    """Read the explorer; on failure keep the last value (memory, then Mongo) marked stale."""
    try:
        data = await fetch_pool_status()
        _pool.update({"data": data, "at": data["at"], "stale": False})
        pool_health["last_ok_at"] = data["at"]
        await db().stats.update_one({"_id": "pool"}, {"$set": data}, upsert=True)
    except Exception as e:  # noqa: BLE001 — a read failure must never blank the number
        pool_health["last_fail_at"] = time.time()
        pool_health["last_error"] = f"{type(e).__name__}: {e}"
        _pool["stale"] = True
        if _pool["data"] is None:
            row = await db().stats.find_one({"_id": "pool"})
            if row:
                keys = ("height", "shielded_outputs_total", "shielded_outputs_per_24h", "at")
                _pool.update(
                    {"data": {k: row.get(k) for k in keys}, "at": float(row.get("at") or 0)}
                )
    return pool_current()


async def pool_stats(max_age_s: float = POOL_TTL_S) -> dict[str, Any]:
    if _pool["data"] is None or time.time() - float(_pool["at"]) > max_age_s:
        return await refresh_pool()
    return pool_current()


# ----------------------------------------------------------------------------- monitor


async def drain_events() -> int:
    rows = await db().events.find({"notified": False}).sort("at", 1).limit(50).to_list(50)
    for ev in rows:
        ok = await tg.send(tg.format_event(ev))
        await db().events.update_one(
            {"_id": ev["_id"]}, {"$set": {"notified": True, "notified_at": time.time(), "sent": ok}}
        )
    return len(rows)


async def stuck_checks() -> None:
    now = time.time()
    d = db()
    q = {"status": "bridging", "updated_at": {"$lt": now - 120 * 60}}
    for r in await d.payout_requests.find(q).limit(50).to_list(50):
        await tg.send(
            "STUCK: direct payout bridging for over 120 min — the relayer lags or is down. "
            f"<code>{r['_id']}</code>",
            key=f"stuck:req:{r['_id']}",
            cooldown_s=6 * 3600,
        )
    q = {"status": {"$in": ["submitted", "order_seen"]}, "created_at": {"$lt": now - 60 * 60}}
    for r in await d.deposits.find(q).limit(50).to_list(50):
        await tg.send(
            f"STUCK: deposit {r['status']} for over 60 min — no DLN fill yet. <code>{r['_id']}</code>",
            key=f"stuck:dep:{r['_id']}:{r['status']}",
            cooldown_s=6 * 3600,
        )
    q = {"status": {"$in": ["locked", "confirming"]}, "locked_at": {"$lt": now - 45 * 60}}
    for r in await d.deposits.find(q).limit(50).to_list(50):
        await tg.send(
            f"STUCK: deposit locked but not credited after 45 min. <code>{r['_id']}</code>",
            key=f"stuck:lock:{r['_id']}",
            cooldown_s=6 * 3600,
        )


def down_for(health: dict[str, Any]) -> float:
    if health["last_fail_at"] <= health["last_ok_at"]:
        return 0.0
    return time.time() - max(float(health["last_ok_at"]), _started_at)


async def upstream_checks() -> None:
    if time.time() - float(dln.health["last_ok_at"]) > 300:
        try:
            await dln.supported_chains(force=True)
        except dln.DlnError:
            pass  # the health bookkeeping happened inside dln._get
    for name, health in (("deBridge DLN", dln.health), ("beamsmart explorer", pool_health)):
        down = down_for(health)
        if down > 300:
            await tg.send(
                f"DOWN: {name} unreachable for {int(down // 60)} min — {tg.esc(health['last_error'])[:200]}",
                key=f"down:{name}",
                cooldown_s=3600,
            )


async def monitor_once() -> None:
    await drain_events()
    await stuck_checks()
    await upstream_checks()


# ----------------------------------------------------------------------------- loops


async def run_forever(name: str, interval_s: float, fn: Callable[[], Awaitable[Any]]) -> None:
    """The one loop every worker runs in: kill switch, catch-log-alert, sleep. Never exits."""
    log.info("worker %s started (every %ss)", name, interval_s)
    while True:
        if paused():
            await tg.send(
                f"PAUSED: <code>{settings.stop_file}</code> exists — workers idle, nothing changes "
                "until it is removed",
                key="stop-file",
                cooldown_s=3600,
            )
        else:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a pass that raised is reported and retried
                log.exception("worker %s raised: %s", name, e)
                await tg.send(
                    f"Worker <code>{name}</code> raised and keeps running: "
                    f"{tg.esc(f'{type(e).__name__}: {e}')[:300]}",
                    key=f"worker:{name}",
                    cooldown_s=900,
                )
        await ethpipe.sleep(interval_s)


def start() -> list[asyncio.Task]:
    return [
        asyncio.create_task(
            run_forever("deposit_watcher", settings.watcher_interval_s, deposit_watcher_once)
        ),
        asyncio.create_task(run_forever("monitor", settings.monitor_interval_s, monitor_once)),
        asyncio.create_task(
            run_forever("stats_refresher", settings.stats_interval_s, refresh_pool)
        ),
    ]


async def stop(tasks: list[asyncio.Task]) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown never raises
            pass
