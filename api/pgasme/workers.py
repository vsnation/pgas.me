"""Background workers — started from the app lifespan when PGAS_WORKERS_ENABLED=1.

  deposit_watcher (15 s)  PRIMARY: scan the pipes on-chain (scanner.py) → locked → confirming → credited
                          SECONDARY: the DLN API for what the chain cannot show —
                          submitted → order_seen (order id), cancelled → failed,
                          filled-but-no-lock → fallback_pending. A `direct` deposit has no DLN
                          order at all and is skipped there: the chain is its only witness.
  monitor         (60 s)  Telegram (default-deny) for queued events, stuck states, upstream reachability
  stats_refresher (60 s)  the shielded-pool numbers from the explorer (served by /v1/stats)

One loop helper, `run_forever`, owns what every loop shares: the kill switch (PGAS_STOP_FILE —
while it exists nothing changes and the operator hears so once an hour), the catch-log-alert
of a pass that raised (15 min cooldown), and the sleep.

Evidence rule (the only one that moves money): a deposit is credited ONLY after the pipe's
NewLocalMessage log naming OUR pubkey and EXACTLY the deposit's value is found and has
`lock_confirmations` blocks on top. A DLN status of Fulfilled by itself is never enough.

Notification rule: every status transition writes exactly ONE event row (tg.queue for the ones
the operator can read a minute later, tg.alert for the ones they cannot: worker failures,
unattributed locks, hook fallbacks, attribution mismatches). The row is the record; the send is
the delivery, and a send that did not happen never marks the row notified.
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
EVENT_MAX_TRIES = 6  # a poison event row is parked, never spun on
EVENT_RETRY_BACKOFF_S = 60.0
EVENT_RETRY_MAX_S = 3600.0
PAYOUT_DUE_GRACE_S = 30 * 60  # scheduled, release_at passed, still not executed → page
PAUSED_REASON = (
    "Pgas.me is paused by the operator (the stop file is set): nothing that moves money is "
    "accepted right now. Balances and estimates are unaffected — try again shortly."
)
_started_at = time.time()
_rpc: dict[str, ethpipe.Rpc | None] = {"rpc": None}
_indexes: dict[str, bool] = {"done": False}


def paused() -> bool:
    """THE kill switch, one file and one implementation: the routes that accept money and every
    worker loop ask this before doing anything irreversible."""
    return os.path.exists(settings.stop_file)


async def ensure_indexes() -> None:
    """The indexes the money path depends on, owned by the modules that depend on them
    (db.ensure_indexes owns the rest). Idempotent; called once at worker start."""
    if _indexes["done"]:
        return
    await scanner.ensure_indexes()
    await ledger.ensure_indexes()
    _indexes["done"] = True


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
    except ledger.AlreadyCredited:
        pass  # the unique index won the race: the money is already in the balance
    except Exception:
        # the balance write failed: hand the deposit back so the next pass retries (has_credit guards doubles)
        await _set(dep["_id"], "confirming", note="balance write failed; retrying")
        raise
    await _set(dep["_id"], ledger_ok=True)
    await _notify_credited(dep)


async def _notify_credited(dep: dict[str, Any], how: str = "") -> None:
    """ONE credited event per deposit, whichever path got there (the credit itself or the
    reconciler picking up after a crash between the two writes)."""
    claimed = await db().deposits.find_one_and_update(
        {"_id": dep["_id"], "credit_event_at": {"$exists": False}},
        {"$set": {"credit_event_at": time.time()}},
    )
    if not claimed:
        return
    await tg.queue(
        "deposit_credited",
        f"Deposit credited{how}: {dep['asset']} {tg.fmt_groth(int(dep['value_groth']))}",
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
        if dep["status"] != "confirming":  # the transition, not every block after it
            await tg.queue(
                "deposit_confirming",
                f"Deposit confirming: {dep['asset']} {tg.fmt_groth(int(dep['value_groth']))} "
                f"({confirmations}/{settings.lock_confirmations} blocks)",
                deposit_id=dep["_id"],
            )
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
    writes) gets its entry now; one that has it gets ledger_ok so it is never looked at again.

    The claim comes FIRST, exactly like _credit: `ledger_ok` is flipped by one conditional
    update and only the pass that flipped it writes the entry. Two passes (or two processes)
    reading `has_credit() == False` at the same moment would otherwise both append, and a
    read-then-write is not a claim. The unique (credit, ref) index is the second belt."""
    cur = db().deposits.find({"status": "credited", "ledger_ok": {"$ne": True}}).limit(50)
    for dep in await cur.to_list(50):
        claimed = await db().deposits.find_one_and_update(
            {"_id": dep["_id"], "status": "credited", "ledger_ok": {"$ne": True}},
            {"$set": {"ledger_ok": True, "updated_at": time.time()}},
        )
        if not claimed:
            continue  # another pass owns this row
        if await ledger.has_credit(dep["_id"]):
            await _notify_credited(dep)  # a crash after the entry, before the event
            continue
        try:
            await ledger.credit(
                dep["account_id"],
                dep["asset"],
                int(dep["value_groth"]),
                dep["_id"],
                "reconciled: credited status without a balance entry",
            )
        except ledger.AlreadyCredited:
            pass  # the index refused a double — the balance already holds it
        except Exception:
            # hand the row back so the next pass retries it; nothing was written
            await db().deposits.update_one({"_id": dep["_id"]}, {"$unset": {"ledger_ok": ""}})
            raise
        await _notify_credited(dep, " (reconciled)")


# ----------------------------------------------------------------------------- SECONDARY: DLN API


async def _step_submitted(dep: dict[str, Any]) -> None:
    """The registered hash is re-checked here against DLN's own index. The row's order id is the
    QUOTE's, never the transaction's first id: adopting ids[0] hands a stranger's fill to
    whoever registered the hash first, and locks the person who actually signed it out with a
    409 forever. A hash whose orders do not include ours is a mismatch, not a rename."""
    if time.time() - float(dep.get("created_at") or 0) < SUBMITTED_GRACE_S:
        return
    ids = await dln.order_ids_by_tx(dep["src_tx_hash"])
    if not ids:
        return  # not indexed yet; the monitor pages after 60 min
    want = str(dep.get("order_id") or "").lower()
    have = [str(i).lower() for i in ids]
    if not want:
        # an armed quote DLN gave no orderId for: nothing to verify against. Do not adopt one —
        # the scanner still attributes this deposit by its metadata tag.
        if not dep.get("order_ids_seen"):
            await _set(dep["_id"], order_ids_seen=ids)
            await tg.alert(
                "deposit_unverifiable",
                "Deposit has no order id of its own: the DLN order ids of its transaction were "
                "recorded but NOT adopted; attribution falls back to the metadata tag",
                deposit_id=dep["_id"],
            )
        return
    if want not in have:
        await _mismatch(
            dep,
            f"the registered transaction carries DLN order(s) {', '.join(have)}, not this "
            f"quote's own order",
        )
        return
    await _set(dep["_id"], "order_seen", order_seen_at=time.time(), verified=True)
    await tg.queue(
        "deposit_order_seen",
        f"Deposit order seen on deBridge: {dep['asset']} "
        f"{tg.fmt_groth(int(dep['value_groth']))}",
        deposit_id=dep["_id"],
    )


async def _mismatch(dep: dict[str, Any], why: str) -> None:
    """The transaction is not this quote's. Fail the row and GIVE THE HASH BACK: the person who
    actually signed it must be able to register it, so the hash is unset (freeing the unique
    index) and kept as evidence under src_tx_hash_rejected."""
    await db().deposits.update_one(
        {"_id": dep["_id"]},
        {
            "$set": {
                "status": "failed",
                "updated_at": time.time(),
                "verified": False,
                "src_tx_hash_rejected": dep.get("src_tx_hash"),
                "note": f"deposit mismatch — {why}. Nothing was credited.",
            },
            "$unset": {"src_tx_hash": ""},
        },
    )
    await tg.alert(
        "deposit_mismatch",
        f"REJECTED: deposit mismatch — {why}; the row is failed and the hash released",
        deposit_id=dep["_id"],
    )


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
        await tg.alert(
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
        if dep.get("mode") == "direct":
            continue  # no DLN order exists for a direct deposit; only the chain can advance it
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
_pool: dict[str, Any] = {"at": 0.0, "data": None, "stale": True, "tried_at": 0.0, "failed": False}
POOL_TTL_S = 60
POOL_RETRY_AFTER_FAIL_S = 60  # while the explorer is down, /v1/stats serves the stale value


def clear_pool_cache() -> None:
    _pool.update({"at": 0.0, "data": None, "stale": True, "tried_at": 0.0, "failed": False})


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
    """Read the explorer; on failure keep the last value (memory, then Mongo) marked stale.
    The ATTEMPT is recorded whatever the outcome, so a request path can back off."""
    _pool["tried_at"] = time.time()
    try:
        data = await fetch_pool_status()
        _pool.update({"data": data, "at": data["at"], "stale": False, "failed": False})
        pool_health["last_ok_at"] = data["at"]
        await db().stats.update_one({"_id": "pool"}, {"$set": data}, upsert=True)
    except Exception as e:  # noqa: BLE001 — a read failure must never blank the number
        pool_health["last_fail_at"] = time.time()
        pool_health["last_error"] = f"{type(e).__name__}: {e}"
        _pool["stale"] = True
        _pool["failed"] = True
        if _pool["data"] is None:
            row = await db().stats.find_one({"_id": "pool"})
            if row:
                keys = ("height", "shielded_outputs_total", "shielded_outputs_per_24h", "at")
                _pool.update(
                    {"data": {k: row.get(k) for k in keys}, "at": float(row.get("at") or 0)}
                )
    return pool_current()


async def pool_stats(max_age_s: float = POOL_TTL_S) -> dict[str, Any]:
    """What /v1/stats serves. NEGATIVE CACHING: after a failed read the explorer is not asked
    again for POOL_RETRY_AFTER_FAIL_S, so an explorer outage costs one slow request a minute
    instead of one per visitor (the refresher loop keeps trying in the background)."""
    now = time.time()
    if _pool["data"] is not None and now - float(_pool["at"]) <= max_age_s:
        return pool_current()
    if _pool["failed"] and now - float(_pool["tried_at"] or 0) < POOL_RETRY_AFTER_FAIL_S:
        return pool_current()  # known down: serve what we have (possibly nothing), do not wait
    return await refresh_pool()


# ----------------------------------------------------------------------------- monitor


async def drain_events() -> int:
    """Send the queued events. An event is marked `notified` ONLY when the send returned True:
    marking a failed send as notified silently drops the operator's alert. Telegram being muted
    (no token / PGAS_TG_LIVE=0) is not a failure — there is nothing to retry — so those rows are
    closed as `muted`. A real failure is retried with a growing backoff and parked after
    EVENT_MAX_TRIES so one poison row cannot spin the monitor."""
    now = time.time()
    due = {
        "notified": False,
        "$or": [{"next_try_at": {"$exists": False}}, {"next_try_at": {"$lte": now}}],
    }
    rows = await db().events.find(due).sort("at", 1).limit(50).to_list(50)
    live = tg.enabled()
    for ev in rows:
        tries = int(ev.get("tries") or 0) + 1
        ok = await tg.send(tg.format_event(ev))
        if ok:
            upd = {"notified": True, "notified_at": time.time(), "sent": True, "tries": tries}
        elif not live:
            # muted by configuration: there is nothing to retry, and the row says so honestly
            upd = {"notified": True, "notified_at": time.time(), "sent": False, "muted": True}
        elif tries >= EVENT_MAX_TRIES:
            upd = {
                "notified": True,
                "notified_at": time.time(),
                "sent": False,
                "tries": tries,
                "gave_up": True,
            }
            log.error("event %s could not be sent in %d tries", ev.get("kind"), tries)
        else:
            back = min(EVENT_RETRY_BACKOFF_S * 2 ** (tries - 1), EVENT_RETRY_MAX_S)
            upd = {"tries": tries, "last_try_at": time.time(), "next_try_at": time.time() + back}
        await db().events.update_one({"_id": ev["_id"]}, {"$set": upd})
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
    # a scheduled payout whose release_at has passed and which nothing has moved on
    q = {"status": "scheduled", "release_at": {"$lt": now - PAYOUT_DUE_GRACE_S}}
    for r in await d.payout_requests.find(q).limit(50).to_list(50):
        await tg.send(
            "STUCK: payout due for over 30 min and still scheduled — no executor moved it. "
            f"<code>{r['_id']}</code>",
            key=f"stuck:due:{r['_id']}",
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


async def _ensure_indexes_task() -> None:
    try:
        await ensure_indexes()
    except Exception as e:  # noqa: BLE001 — the app still answers; the operator must hear
        log.error("worker index build failed: %s: %s", type(e).__name__, e)
        await tg.send(
            f"Index build failed at start: {tg.esc(f'{type(e).__name__}: {e}')[:300]} — the "
            "unique credit/deposit-hash guards are NOT in place",
            key="index-build",
            cooldown_s=3600,
        )


def start() -> list[asyncio.Task]:
    return [
        asyncio.create_task(_ensure_indexes_task()),
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
