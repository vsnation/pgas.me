"""Background workers — started from the app lifespan when PGAS_WORKERS_ENABLED=1.

  deposit_watcher (15 s)  PRIMARY: scan the pipes on-chain (scanner.py) → locked → confirming → credited
                          SECONDARY: the router's API for what the chain cannot show —
                          submitted → order_seen (order id), cancelled → failed,
                          filled-but-no-lock → fallback_pending. A `direct` deposit has no cross-chain
                          order at all and is skipped there: the chain is its only witness.
  payout_processor (30 s) the order processors (payouts.py): every payout order's next step
                          (scheduled → releasing → bridging → delivering → sent) and every
                          credited deposit's treasury work (claim → shield). Dark by flag.
  monitor         (60 s)  Telegram (default-deny) for queued events, stuck states, upstream reachability
  stats_refresher (60 s)  the shielded-pool numbers from the explorer (served by /v1/stats)

One loop helper, `run_forever`, owns what every loop shares: the kill switch (PGAS_STOP_FILE —
while it exists nothing changes and the operator hears so once an hour), the catch-log-alert
of a pass that raised (15 min cooldown), and the sleep.

Evidence rule (the only one that moves money): a deposit is credited ONLY after the pipe's
NewLocalMessage log naming OUR pubkey and EXACTLY the deposit's value is found and has
`lock_confirmations` blocks on top. A router status of Fulfilled by itself is never enough.

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

from . import beampay, ethpipe, ledger, scanner, tg, uniswap, xchain
from .assets import ASSETS
from .config import settings
from .db import db

log = logging.getLogger("pgasme.workers")

SUBMITTED_GRACE_S = 120  # the chain is asked first; the router's order-ids only after this
# how long a deposit that advanced without being verified keeps being offered to the router's index.
# Bounded on purpose: an unbounded re-check would ask the same question about the same old rows
# every 15 seconds forever. ⛔ A DAY WAS TOO SHORT, and for a reason that has nothing to do with
# how fast the router indexes: the re-check only runs while this process runs, so a row whose evidence
# was there all along stays unverified unless a DEPLOY lands inside the window. The 2026-09-09
# 20:40Z deposit is the proof — real, credited, `verified: false`, and out of a 24 h window
# before anyone looked. A week covers a normal deploy cadence; it is still finite.
REVERIFY_WINDOW_S = int(getattr(settings, "reverify_window_s", 7 * 86400))
UNFILLED_AFTER_S = 20 * 60  # a `Created` order this old gets a note (a solver may still fill it)
UNFILLED_ALERT_COOLDOWN_S = 6 * 3600
UNFILLED_NOTE = (
    "not filled yet — a solver can still fill it; you can cancel from the router's order page "
    "for a refund"
)
EVENT_MAX_TRIES = 6  # a poison event row is parked, never spun on
EVENT_RETRY_BACKOFF_S = 60.0
EVENT_RETRY_MAX_S = 3600.0
PAYOUT_DUE_GRACE_S = 30 * 60  # scheduled, release_at passed, still not executed → page
# The order processors' SLAs (pgasme/payouts.py). Each is the point at which waiting stops
# being normal: `bridging` is 61 Beam confirmations (~60 min) plus the relayer's own batching,
# and `delivering` is the measured b2e tail (4 h 03 m, 5.5 h, 18 h — §7.6). A row a FLAG is
# holding is marked `dark` and is deliberately not stuck: pages that cry wolf get ignored.
SLA_S: dict[str, float] = {
    "releasing": 15 * 60,
    "bridging": 3 * 3600,
    "delivering": 18 * 3600,
}
TREASURY_SLA_S: dict[str, float] = {"claiming": 30 * 60, "shielding": 2 * 3600}
NOT_DARK = {"dark": {"$ne": True}}
# A row a HUMAN owns (payouts._hold_for_a_human): no handler resolves it, so it can only leave
# by hand — and it must be said out loud until it does. This is the REMINDER interval, measured
# from the last thing that said it: the page the hold itself sent (`hold_paged_at`) or the last
# reminder (`held_reminded_at`), both on the row so a restart cannot lose them.
HELD_SLA_S = 6 * 3600


def stale(field: str, cut: float) -> dict[str, Any]:
    """"This row has been in its status since before `cut`", measured by a clock the handlers
    do not touch.

    ⛔ `updated_at` cannot be that clock. `_payout_delivering` writes a scan checkpoint on every
    30-second pass, so a delivering row's `updated_at` was never older than 30 s and the
    18-hour SLA — the ONE monitor for a crossing whose bETH is already burned and cannot be
    recalled — never fired once. `<field>_at` is stamped only by `payouts._advance`, i.e. only
    when the status actually changes. Rows written before this build have no such stamp, so
    they fall back to `updated_at` rather than becoming invisible."""
    return {
        "$or": [
            {field: {"$lt": cut}},
            {field: {"$exists": False}, "updated_at": {"$lt": cut}},
        ]
    }


def held_paged_at(row: dict[str, Any]) -> float:
    """When this held row was last paged about — 0.0 when nothing proves it ever was.

    ONE writer of that fact: `payouts._hold_for_a_human`, which stamps `hold_paged_at` in the
    same call that sends the page (`tg.alert`, immediate). This reads THAT FIELD AND NOTHING
    ELSE — two implementations of "when did we last say this" would disagree, and the
    disagreement is a flood.

    ⛔ It used to fall back to `hold_at` / `<field>_at`, and `payouts._hold` writes `hold_at` on
    every ordinary WAITING hold — so a row parked for a human that was then held once more for
    a fee budget looked freshly paged, and the reminder about money nobody could move went
    silent. A row with no `hold_paged_at` has no proof it was ever paged (held by an older
    build, or by hand), and the safe answer there is to page it.
    """
    return float(row.get("hold_paged_at") or 0.0)


def held_reminded_at(row: dict[str, Any]) -> float:
    """When the monitor last sent the reminder about this hold — 0.0 if it never has.

    Written by `stuck_checks()` on the row, because the tg cooldown that also holds this back
    is per-PROCESS: a restart inside the SLA emptied `tg._last_sent` and every held row was
    paged again, which is the flood arriving by a different door. The row outlives the process.
    """
    return float(row.get("held_reminded_at") or 0.0)


def held_reminder_due(row: dict[str, Any], now: float) -> bool:
    """True when HELD_SLA_S has passed since the last thing that SAID this hold out loud —
    the hold's own page or the last reminder, whichever is later.

    The 2026-09-10 flood: `_hold_for_a_human` paged "HELD: a shield chunk failed…" and the very
    next monitor pass paged the same hold again from here, because this check had no idea the
    hold had already spoken (and its in-memory tg cooldown key had never been used). One event,
    two pages, and the second one truncated — which is how an operator learns to skim the pager.
    """
    return now - max(held_paged_at(row), held_reminded_at(row)) >= HELD_SLA_S


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
    from . import payouts  # local: payouts imports this module (kill switch, RPC pool)

    await scanner.ensure_indexes()
    await ledger.ensure_indexes()
    # one txid is one row's evidence, and one delivery settles one request — the DATABASE says
    # so, not a read (payouts.ensure_indexes)
    await payouts.ensure_indexes()
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


# ----------------------------------------------------------------------------- SECONDARY: the router's API


def _candidate_orders(dep: dict[str, Any]) -> list[str]:
    """Every cross-chain order id THIS deposit's own quote was armed with, newest first.

    ⛔ THE ROW'S SINGLE `order_id` IS NOT THE CANDIDATE SET. /arm can rebuild a quote's order,
    and an unsigned cross-chain order exists only as a transaction we handed over — so an EARLIER order
    of the same quote is still this quote's own order. When the router has not indexed the transaction
    yet (the normal case, and the whole reason `verified` starts false) the row is stamped with
    the LATEST armed id, so a user who signed anything but the last one was failed here as a
    hijack: `_mismatch` set the row `failed` and released the hash, the fill's lock then resolved
    to the quote by metadata tag and found a row that is no longer claimable, and re-registering
    the hash only rebuilt the same wrong row. A guard that fails politely is a guard that fails.
    """
    ids = [str(i) for i in (dep.get("order_ids_armed") or []) if i]
    want = str(dep.get("order_id") or "")
    if want and want.lower() not in [i.lower() for i in ids]:
        ids.insert(0, want)
    return ids


def _adopt(dep: dict[str, Any], matched: str) -> dict[str, Any]:
    """The fields that make the row describe the order the chain says was actually signed —
    including the amounts THAT order was built at (/arm re-prices on the router's recommendation,
    so two orders of one quote are worth different money, and the credit pays the row's number)."""
    fields: dict[str, Any] = {"verified": True}
    if matched.lower() == str(dep.get("order_id") or "").lower():
        return fields
    fields["order_id"] = matched
    fields["order_id_adopted_from"] = dep.get("order_id")
    snap = next(
        (
            s
            for s in (dep.get("orders_armed") or [])
            if str((s or {}).get("order_id") or "").lower() == matched.lower()
        ),
        None,
    )
    if snap:
        fields["eth.value_units"] = str(snap["value_units"])
        fields["eth.relayer_fee_units"] = str(snap["relayer_fee_units"])
        fields["value_groth"] = int(snap["value_groth"])
    return fields


async def _step_submitted(dep: dict[str, Any]) -> None:
    """The registered hash is re-checked here against the router's own index. The ids it is checked
    against are THIS QUOTE's own (see `_candidate_orders`), never the transaction's first id:
    adopting ids[0] hands a stranger's fill to whoever registered the hash first, and locks the
    person who actually signed it out with a 409 forever. A hash carrying none of this quote's
    orders is a mismatch; a hash carrying an earlier one of them is this quote's deposit."""
    if time.time() - float(dep.get("created_at") or 0) < SUBMITTED_GRACE_S:
        return
    ids = await xchain.order_ids_by_tx(dep["src_tx_hash"])
    if not ids:
        return  # not indexed yet; the monitor pages after 60 min
    candidates = _candidate_orders(dep)
    have = [str(i).lower() for i in ids]
    if not candidates:
        # an armed quote the router gave no orderId for: nothing to verify against. Do not adopt one —
        # the scanner still attributes this deposit by its metadata tag.
        if not dep.get("order_ids_seen"):
            await _set(dep["_id"], order_ids_seen=ids)
            await tg.alert(
                "deposit_unverifiable",
                "Deposit has no order id of its own: the router's order ids for its transaction were "
                "recorded but NOT adopted; attribution falls back to the metadata tag",
                deposit_id=dep["_id"],
            )
        return
    matched = next((c for c in candidates if c.lower() in have), None)
    if matched is None:
        await _mismatch(
            dep,
            f"the registered transaction carries router order(s) {', '.join(have)}, not any of this "
            f"quote's own orders",
        )
        return
    await _set(dep["_id"], "order_seen", order_seen_at=time.time(), **_adopt(dep, matched))
    dep = await db().deposits.find_one({"_id": dep["_id"]}) or dep
    await tg.queue(
        "deposit_order_seen",
        f"Deposit order seen by the router: {dep['asset']} "
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
    status = (await xchain.order_status(dep["order_id"])).get("status")
    now = time.time()
    if status in xchain.TERMINAL_CANCELLED:
        note = "order cancelled — funds refunded to you on the source chain; nothing was locked"
        await _set(dep["_id"], "failed", route_status=status, note=note)
        await tg.queue(
            "deposit_failed", f"Deposit failed: cross-chain order {status}", deposit_id=dep["_id"]
        )
        return
    if status not in xchain.TERMINAL_OK:
        fields: dict[str, Any] = {"route_status": status}
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
            route_status=status,
            fulfilled_seen_at=fulfilled_at,
            note=note,
        )
        await tg.alert(
            "deposit_fallback",
            "Deposit filled WITHOUT a pipe lock: funds are in the user's own wallet",
            deposit_id=dep["_id"],
        )
        return
    await _set(dep["_id"], route_status=status, fulfilled_seen_at=fulfilled_at)


async def _reverify(dep: dict[str, Any]) -> None:
    """Flip `verified` on a row the chain moved past `submitted` before the router had indexed it.

    Recorded live 2026-09-09 20:40Z: a BSC deposit was registered seconds after it was signed,
    so the router's index could not vouch for the hash yet (`verified: false`, which is correct); the
    PRIMARY pass then found the pipe lock and moved the row straight to `locked`, and the
    re-check only ever ran on `submitted` rows — so the flag stayed false through credited,
    forever. The evidence exists, it was simply never asked for again.

    Only ever sets it TRUE. A row whose money already landed is not failed here: the mismatch
    path belongs to `_step_submitted`, where nothing has been credited yet."""
    try:
        ids = await xchain.order_ids_by_tx(dep["src_tx_hash"])
    except xchain.XchainError:
        return  # not indexed yet, or unreachable — "I do not know" is never a verdict
    have = [str(i).lower() for i in ids]
    # the SAME candidate set the re-check uses: an earlier order of this quote is this quote's
    # own evidence too. The row is past `submitted` here, so only the flag moves — the amounts
    # and the id belong to whatever already claimed the lock.
    if any(c.lower() in have for c in _candidate_orders(dep)):
        await _set(dep["_id"], verified=True)


async def xchain_secondary() -> int:
    cur = (
        db()
        .deposits.find(
            {
                "$or": [
                    {"status": {"$in": ["submitted", "order_seen"]}},
                    # advanced past the re-check without ever being verified: ask the router once more
                    # while its index can still be expected to hold the transaction
                    {
                        "status": {"$in": ["locked", "confirming", "credited"]},
                        "verified": False,
                        "created_at": {"$gte": time.time() - REVERIFY_WINDOW_S},
                    },
                ]
            }
        )
        .sort("created_at", 1)
        .limit(100)
    )
    n = 0
    for dep in await cur.to_list(100):
        if xchain.norm_mode(dep.get("mode")) in ("direct", uniswap.MODE):
            # No cross-chain order exists on either of these paths: a `direct` deposit IS the pipe
            # call and a `uniswap` one reaches the pipe through our own hook. Only the chain can
            # advance them, and asking the router about their hash pages the operator for nothing.
            continue
        try:
            if dep["status"] == "submitted":
                if dep.get("src_tx_hash"):
                    await _step_submitted(dep)
            elif dep["status"] == "order_seen":
                await _step_order_seen(dep)
            elif dep.get("src_tx_hash"):
                await _reverify(dep)
            n += 1
        except Exception as e:  # noqa: BLE001 — one bad deposit must not block the others
            log.exception("xchain step %s: %s", dep["_id"], e)
            await tg.send(
                f"Cross-chain step failed on <code>{dep['_id']}</code>: {tg.esc(f'{type(e).__name__}: {e}')[:300]}",
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
    out["xchain"] = await xchain_secondary()  # SECONDARY: what the chain cannot show
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
    # the payout order machine: each status has its own SLA, and only the relayer's own two
    # legs get hours — a release that has not gone out in 15 min is ours to explain.
    for status, sla in SLA_S.items():
        q = {"status": status, **stale("status_at", now - sla), **NOT_DARK}
        for r in await d.payout_requests.find(q).limit(50).to_list(50):
            mins = int(sla // 60)
            tail = (
                " — the relayer lags or is down; the message cannot be cancelled once the bETH "
                "is burned"
                if status in ("bridging", "delivering")
                else " — no processor moved it"
            )
            await tg.send(
                f"STUCK: direct payout {status} for over {mins} min{tail}. <code>{r['_id']}</code>",
                key=f"stuck:req:{r['_id']}:{status}",
                cooldown_s=6 * 3600,
            )
    # the deposit treasury sub-machine (claim → shield)
    for tstatus, sla in TREASURY_SLA_S.items():
        q = {"treasury": tstatus, **stale("treasury_at", now - sla), **NOT_DARK}
        for r in await d.deposits.find(q).limit(50).to_list(50):
            await tg.send(
                f"STUCK: treasury {tstatus} for over {int(sla // 60)} min — the claim/shield "
                f"chain is not advancing. <code>{r['_id']}</code>",
                key=f"stuck:treasury:{r['_id']}:{tstatus}",
                cooldown_s=6 * 3600,
            )
    # rows parked for a human: the money is somewhere the machine may not touch, so the pager
    # keeps saying so — a held row that nobody hears about is the same as a lost one. But it is
    # a REMINDER, not a second announcement: `_hold_for_a_human` already paged when it held the
    # row, so this waits HELD_SLA_S from THAT page (`held_reminder_due`).
    for coll, field, what in (
        ("payout_requests", "status", "payout"),
        ("deposits", "treasury", "deposit"),
    ):
        for r in await d[coll].find({field: "held"}).limit(50).to_list(50):
            if not held_reminder_due(r, now):
                continue
            # The reason IN FULL, and the id FIRST. The old `[:200]` cut the shield-failure
            # reason mid-sentence, which meant the operator's copy of the alert ended before the
            # `replan-shield` command that resolves it — an instruction cut in half is worse
            # than no instruction. tg.cap() is the one length guard, and it marks its cut.
            reason = str(r.get("hold_reason") or "no reason was recorded on the row")
            # said only when the row can prove it: a missing stamp is not "held since 1970"
            paged_at = held_paged_at(r)
            since = f" for over {int((now - paged_at) // 3600)} h" if paged_at else ""
            await tg.send(
                tg.cap(
                    f"HELD: <code>{r['_id']}</code> {what} parked for a human{since} and "
                    f"nothing retries it — {tg.esc(reason)}"
                ),
                key=f"held:{coll}:{r['_id']}",
                cooldown_s=HELD_SLA_S,
            )
            # …and the reminder is now the most recent thing that said it — ON THE ROW, because
            # the cooldown above dies with the process and a restart inside the SLA re-paged
            # every held row. Stamped on the ATTEMPT, exactly like that cooldown: a send that
            # failed must not spin on the next pass either. Bookkeeping, so it deliberately does
            # NOT touch `updated_at` — that clock belongs to the status machine.
            await d[coll].update_one({"_id": r["_id"]}, {"$set": {"held_reminded_at": now}})
    q = {"status": {"$in": ["submitted", "order_seen"]}, "created_at": {"$lt": now - 60 * 60}}
    for r in await d.deposits.find(q).limit(50).to_list(50):
        # say what is actually missing: a `direct` or `uniswap` deposit has no cross-chain order
        # in its story at all, and a pager that describes the wrong thing is one the operator
        # learns to skim.
        missing = (
            "no pipe lock yet"
            if xchain.norm_mode(r.get("mode")) in ("direct", uniswap.MODE)
            else "no cross-chain fill yet"
        )
        await tg.send(
            f"STUCK: deposit {r['status']} for over 60 min — {missing}. <code>{r['_id']}</code>",
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
    q = {"status": "scheduled", "release_at": {"$lt": now - PAYOUT_DUE_GRACE_S}, **NOT_DARK}
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
    if time.time() - float(xchain.health["last_ok_at"]) > 300:
        try:
            await xchain.supported_chains(force=True)
        except xchain.XchainError:
            pass  # the health bookkeeping happened inside xchain._get
    # BeamPay is the SYSTEM OF RECORD for the Beam side: while it is unreachable no claim, no
    # shield and no payout can read a balance or register a txid. Its health is what the
    # processors themselves observed on their own calls — never a separate probe (§8).
    for name, health in (
        ("cross-chain router", xchain.health),
        ("beamsmart explorer", pool_health),
        ("BeamPay", beampay.health),
    ):
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
    # imported HERE, not at module scope: payouts imports this module for the kill switch and
    # the RPC pool, so a top-level import would be a cycle.
    from . import payouts

    return [
        asyncio.create_task(_ensure_indexes_task()),
        asyncio.create_task(
            run_forever("deposit_watcher", settings.watcher_interval_s, deposit_watcher_once)
        ),
        asyncio.create_task(
            run_forever("payout_processor", settings.payout_interval_s, payouts.process_once)
        ),
        asyncio.create_task(run_forever("monitor", settings.monitor_interval_s, monitor_once)),
        asyncio.create_task(
            run_forever("stats_refresher", settings.stats_interval_s, refresh_pool)
        ),
    ]


async def stop(tasks: list[asyncio.Task]) -> None:
    """Cancel every loop, then GIVE THE PAYOUT LEASE BACK.

    The TTL is what makes a crash safe; handing the lease back is what makes a restart fast.
    Without this the dying process's claim stands for a whole `payout_lease_ttl_s`, so the new
    processor does nothing for two minutes and pages that "a second payout processor is running"
    — about its own corpse. It runs AFTER the loops are cancelled, so the lease is never released
    out from under a pass that is still executing, and `release_lease` releases only a lease THIS
    process owns and never raises."""
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown never raises
            pass
    from . import payouts  # local import: payouts imports this module (see `start`)

    await payouts.release_lease()
