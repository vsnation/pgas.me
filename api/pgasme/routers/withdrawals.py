"""POST /v1/withdrawals — schedule a LIST of orders: (address, amount, delivery time).
POST …/{id}/cancel · GET /v1/withdrawals/fees.

Restructured 2026-09-09 ("Scheduling"). The user types the wallets they want funded, how much
each should receive and WHEN it should be there; nothing is signed, connected or registered
first — an address is admitted on its own arithmetic (a valid EIP-55 checksum) and on one fact
read from the chain (it carries no contract code).

Every rule of the contract, in order, failing closed:
  a valid address that is not a contract · a delivery time that IS a time · an asset this
  deployment can actually pay · amount ≥ min_amount_groth for THAT delivery time · instant →
  denomination multiples · Available ≥ Σ amount × (1 + fee_bps/10000) · a disabled mode → 409.
One `payout_requests` row per item with its own `release_at`, and ONE ledger `schedule` entry
per item for amount + fee. Execution is a worker concern (payouts.py) and is dark here.

THE INVARIANTS THIS FILE OWNS:

  * "Available ≥ need" read and then acted on is not a check — two requests arriving together
    both read the same Available and both pass it. The admission is a per-(account, asset)
    reservation: `pending` is claimed FIRST and the ledger is read AFTER the claim, so a
    balance already spent by a request that finished in between is visible to the arithmetic
    that decides. A claim that does not fit is backed out before a groth is written. Entries
    are written only when that claim held, the ledger is re-read afterwards, and a balance that
    went negative is rolled back with offsetting entries (never by editing history). The WHOLE
    BATCH is one reservation: a batch is admitted or refused as a unit, never half-written —
    and a batch that cannot be written in full REVERSES ITSELF before the error leaves.
  * A refund needs the debit it reverses, and A WRITE THAT RAISED IS NOT A WRITE THAT DID NOT
    LAND. The `schedule` entry is appended BEFORE the request row exists, so a row can never
    exist without its debit; cancel refunds exactly the groth of THAT entry (not a recomputed
    amount + fee) and only once; and a compensation ASKS whether the row it is compensating for
    actually landed before it gives the money back — an order whose debit was reversed is an
    order the payout worker pays for free.
  * CANCEL IS REPAIRABLE, NOT TERMINAL. The row is flipped to `cancelled` before the refund is
    appended, so a failure between the two used to freeze the groth in `scheduled` forever with
    no alert and no second chance. A `cancelled` row whose `schedule` entry has no matching
    `cancel` entry RE-ENTERS the refund path (ledger.cancel is idempotent by construction), and
    any failure to write the refund pages the operator.
  * DELIVERY TIME IS NOT RELEASE TIME. The bridge takes ~66 minutes (`PGAS_BRIDGE_ETA_S`), so an
    order the user wants delivered at T is handed to the bridge at `max(now, T − eta)`. A
    `deliver_at` in the past (or absent, i.e. "asap") releases now — it never schedules backwards.
    The window guard is POSITIVE (the value must BE in range): a `deliver_at` of NaN compares
    False to every bound, so a guard phrased as "refuse it when it is out of range" admits it.
  * THE MINIMUM IS DERIVED, NOT DECLARED — AND IT IS PRICED FOR THE PROMISE, NOT THE MOMENT. We
    charge the user `fee_bps` and pay the bridge's relayer fee out of it, so an amount whose 2%
    cannot cover the relayer fee is a payout WE lose money on: the floor is
    `max(PGAS_MIN_PAYOUT_GROTH, ceil(relayer_fee_now × headroom × 10000 / fee_bps))` computed
    from the live gas price at request time (payouts.relayer_fee_for, the same arithmetic the
    release itself will use). §WE-SET-IT-WE-DONT-READ-IT: nobody quotes that fee back to us, so
    it is measured, never assumed — and a fee we could not read REFUSES the request (503). It is
    never a guessed number and never a stale one (cached ≤ 60 s).
  * ONCE THE ROWS ARE LIVE, NOTHING MAY BECOME A 5xx. Past the point where `_write_items` has
    committed the batch, the orders exist, the balance is debited and the worker will pay them.
    A 500 from there tells the caller their withdrawal FAILED *and* withholds the ids — so the
    ordinary retry-on-500 schedules every one of those payouts a second time, which is exactly
    the duplication the batch reverses itself to avoid. What is left after the commit is a belt
    and an observation: the belt may still REVERSE the batch (a 409 naming the ids it reversed
    — a refusal, not a failure), and everything else (an unreadable balance, an event that will
    not write) is logged, paged and swallowed, with the 200 and the ids leaving regardless.
  * AN UNREADABLE CHAIN IS NOT AN EMPTY ANSWER, AND AN IMPLAUSIBLE ANSWER IS NOT A READING.
    `eth_getCode(W)` is read from ONE pinned endpoint at the head that endpoint itself reported;
    a contract there is a 400 (a contract with no payable receive strands the bridge delivery),
    and an endpoint that could not answer is a 503 — never "no code, therefore an EOA". The head
    itself is checked before it is trusted: a node that answers block 0, a node that is still
    syncing, or a node whose head is far below one this process has already seen would make
    `eth_getCode` return "0x" for a real contract and turn the guard into a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from typing import Annotated, Any

from eth_utils import is_checksum_address, is_hex_address, to_checksum_address
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, ethpipe, ledger, tg, workers
from ..assets import ASSETS, Asset, get_asset
from ..config import settings
from ..db import db

log = logging.getLogger("pgasme.withdrawals")

router = APIRouter(prefix="/v1/withdrawals", tags=["withdrawals"])

MODES = ("direct", "instant")
# what this deployment's executor can actually pay. `payouts._payout_scheduled` HOLDS every
# non-ETH row ("v1 payouts are ETH only") and `_payout_any_asset` holds every non-direct one, so
# admitting either here debits the user's balance into `scheduled` for an order that can never
# execute. A refusal the executor makes is a refusal the request path should have made.
PAYOUT_ASSETS = ("ETH",)
# a reservation left behind by a request that died mid-flight; far longer than any request lives
RESERVATION_STALE_S = 300.0
# how long before `deliver_at` each mode has to leave: direct = one bridge crossing
# (PGAS_BRIDGE_ETA_S, ~66 min), instant = a transfer that is there in seconds.
INSTANT_ETA_S = 30
# the live relayer fee is one gas read shared by every caller for a minute — a fee that moves
# with the block is still a fee that must not cost one upstream call per rendered form
FEES_CACHE_S = 60.0
# the earliest value that is a delivery TIME at all. A `deliver_at` in the past means "asap" and
# is accepted; 0, a negative number or NaN is a caller bug, and admitting it is how a batch got
# debited and scheduled behind a value that compares False to every bound.
MIN_DELIVER_AT = 0.0
# ⛔ how much more than TODAY's relayer fee a far-dated order must fund. `payouts` holds a
# release whose live relayer fee exceeds `fee_groth × settings.max_relayer_subsidy` (1.0 by
# default: no headroom at all), and an order may wait up to `max_window_s` (30 days) for that
# gate to run. An order accepted at exactly today's floor is therefore held — retried, paged,
# never delivered — by ANY gas increase, with the user's groth stuck in `scheduled` behind a
# delivery time we already promised. So the further ahead an order releases, the more headroom
# it buys: 1× when it releases now, FAR_DATED_MARGIN× a full window out, linear in the wait.
FAR_DATED_MARGIN = 3.0
UNREADABLE_DEST = (
    "could not verify the destination on Ethereum right now ({why}) — "
    "nothing was scheduled; try again in a moment"
)
# a head far below one this process has already seen is not a head: a snap-syncing or
# freshly-restarted provider answers an old block, and eth_getCode at a block a node does not
# have is "0x" — which this guard would read as "this address is a wallet".
HEAD_REGRESSION_TOLERANCE = 64


class Item(BaseModel):
    W: str = Field(min_length=40, max_length=64)
    amount_groth: int = Field(gt=0)
    # unix seconds: when the money should BE in W. Absent (or in the past) means "as soon as
    # possible" — the order is released now and arrives about one bridge ETA later.
    # ⛔ allow_inf_nan=False. Ordinary JSON has no NaN literal, but pydantic coerces the STRING
    # "NaN" / "Infinity" / "-Infinity" to a float, and EVERY comparison against NaN is False —
    # so `deliver_at: "NaN"` walked through a window guard phrased as "refuse it if it is out of
    # range", made `release_at` = now, got the batch reserved, debited, scheduled and paged, and
    # only then made the response a 500 (FastAPI renders with allow_nan=False) — and every
    # retry of that 500 wrote another live order. Refused at the door: 422, before a groth moves.
    deliver_at: Annotated[float, Field(allow_inf_nan=False)] | None = None


class WithdrawIn(BaseModel):
    asset: str = "ETH"
    items: list[Item] = Field(min_length=1)
    mode: str = "direct"


class PartialBatch(RuntimeError):
    """The batch could not be written in full and was reversed. `standing` names the orders that
    could NOT be reversed — an empty list means nothing was written and nothing was debited."""

    def __init__(self, standing: list[str]) -> None:
        super().__init__(f"{len(standing)} order(s) still stand")
        self.standing = standing


def fee_for(amount_groth: int) -> int:
    return amount_groth * settings.fee_bps // 10000


def eta_for(mode: str) -> int:
    """How long the money needs between release and delivery, in seconds."""
    return int(settings.bridge_eta_s) if mode == "direct" else INSTANT_ETA_S


def release_at_for(deliver_at: float | None, now: float, eta_s: int) -> float:
    """`max(now, deliver_at − eta)`. Absent or past → now; never a time in the past."""
    if deliver_at is None:
        return now
    return max(now, float(deliver_at) - eta_s)


def normalise_address(raw: str) -> str:
    """Any EVM address the user types, in the form we store it (EIP-55 checksummed).

    A lowercase (or uppercase) string carries NO checksum information and is accepted as typed;
    a MIXED-CASE string carries one, and a mixed-case string whose checksum does not match is a
    typo the user can still fix — refuse it rather than move money to it."""
    s = (raw or "").strip()
    if not is_hex_address(s):
        raise HTTPException(400, f"{s[:64]!r} is not an EVM address")
    body = s[2:] if s[:2].lower() == "0x" else s
    if body != body.lower() and body != body.upper() and not is_checksum_address(s):
        raise HTTPException(
            400,
            f"{s[:64]} has a bad EIP-55 checksum — check the address you pasted (a lowercase "
            f"address is accepted as typed; a mixed-case one must checksum)",
        )
    return to_checksum_address(s)


# --------------------------------------------------------------- the destination must be a wallet

_head_seen: dict[str, int] = {"head": 0}


def clear_head_floor() -> None:
    """Forget the highest head this process has seen (tests; a restart does the same)."""
    _head_seen["head"] = 0


def _unreadable(why: str) -> HTTPException:
    return HTTPException(503, UNREADABLE_DEST.format(why=why))


async def trusted_head(rpc: Any) -> tuple[int, str]:
    """(head, the endpoint that reported it) — or 503. The head is EVIDENCE, so it is checked.

    `head_from()` takes the first endpoint that answers `eth_blockNumber` and never asks whether
    that endpoint is in sync (`pool_heads()` does, which is exactly law 8: the prober must call
    the way the caller calls). A node that answers 0 — or any block far behind — makes
    `eth_getCode(W, <that block>)` return "0x" for a deployed contract, and the guard silently
    becomes a no-op on the one refusal that has no refund path."""
    try:
        head, url = await rpc.head_from()
    except ethpipe.RpcError as e:
        raise _unreadable("no endpoint answered") from e
    if head <= 0:
        raise _unreadable(f"the endpoint that answered reported block {head}")
    try:
        syncing = await rpc.call("eth_syncing", [], prefer=url, pin=True)
    except ethpipe.RpcError as e:
        raise _unreadable("the endpoint that reported the head could not say whether it is synced") from e
    if syncing is not False:
        raise _unreadable("the endpoint that reported the head is still syncing")
    floor = int(_head_seen["head"])
    if head < floor - HEAD_REGRESSION_TOLERANCE:
        raise _unreadable(f"the endpoint reported block {head}, far below the {floor} already seen")
    _head_seen["head"] = max(floor, head)
    return head, url


async def refuse_contracts(addresses: list[str]) -> tuple[int, str]:
    """400 when any of these carries contract code; 503 when the chain could not say. Returns the
    (head, endpoint) the answer was proven at, so the rows can record their own evidence.

    ONE endpoint answers all of them, pinned to the node that reported the head they are read
    at: a code read spread over a pool can be answered by a node that does not have that state
    yet, and "0x" from a node that cannot see the block is not "this is a wallet".

    ⚠️ EVALUATED HERE AND NOWHERE ELSE. `deliver_at` may be up to `max_window_s` (30 days) out
    and nothing re-reads `eth_getCode` before the release, so an address that is a bare EOA today
    and a deployed contract tomorrow (a counterfactual CREATE2 account, an EIP-7702 delegation)
    is delivered into anyway. The block this was proven at travels onto every row so a release-
    time re-check has a baseline to compare against; the re-check itself belongs in
    `payouts._payout_scheduled` and is NOT implemented."""
    rpc = workers.get_rpc()
    head, url = await trusted_head(rpc)
    unique = sorted({a for a in addresses})

    async def code_of(a: str) -> str | None:
        try:
            return await rpc.call("eth_getCode", [a, hex(head)], prefer=url, pin=True)
        except ethpipe.RpcError:
            return None

    codes = await asyncio.gather(*(code_of(a) for a in unique))
    for a, code in zip(unique, codes, strict=True):
        if not isinstance(code, str):  # no answer, or an answer we cannot read: never a verdict
            raise _unreadable(f"{url} could not read the code at that address")
        if code.replace("0x", "").strip("0"):  # "0x" / "0x0" are the only "no code" answers
            raise HTTPException(
                400,
                f"{a} is a contract, not a wallet — the bridge delivery would be stranded there. "
                f"Use an address you control the keys to",
            )
    return head, url


# ----------------------------------------------------------------------------- the live fee floor

_fees_cache: dict[str, dict[str, Any]] = {}


def window_margin(ahead_s: float) -> float:
    """How much more than today's relayer fee an order releasing `ahead_s` from now must fund."""
    window = float(settings.max_window_s or 0)
    if window <= 0 or ahead_s <= 0:
        return 1.0
    return 1.0 + (FAR_DATED_MARGIN - 1.0) * min(1.0, float(ahead_s) / window)


def headroom_for(ahead_s: float) -> float:
    """The margin the FLOOR has to carry, net of the subsidy the release gate already allows.

    The request-time floor and the release-time gate (`fee_groth > charged ×
    max_relayer_subsidy`) are two views of ONE number, so the floor reads the same setting the
    gate does instead of inventing a second one — and it never goes below 1×, which would price
    a payout the treasury loses money on at TODAY's gas."""
    subsidy = float(settings.max_relayer_subsidy or 0) or 1.0
    return max(1.0, window_margin(ahead_s) / subsidy)


def _min_amount_groth(relayer_fee_groth: int, ahead_s: float = 0.0) -> int:
    """The smallest payout whose `fee_bps` cut still covers the relayer fee we will pay for it —
    measured now, and multiplied by the headroom the wait until its release needs."""
    if settings.fee_bps <= 0:  # a zero-fee deployment cannot fund a crossing out of the fee
        return int(settings.min_payout_groth)
    covers = math.ceil(int(relayer_fee_groth) * headroom_for(ahead_s))
    return max(
        int(settings.min_payout_groth),
        math.ceil(covers * 10000 / int(settings.fee_bps)),
    )


async def live_fees(asset: Asset) -> dict[str, Any]:
    """{fee_bps, relayer_fee_groth_now, min_amount_groth, bridge_eta_s} — measured, ≤ 60 s old.

    A fee we could not read RAISES (503). Law 4: a stale fee refuses, it never reuses; and the
    number we set is the number nobody quotes back to us, so it is read from the live gas price
    through the same helper the release itself uses (payouts.relayer_fee_for), never assumed.

    `min_amount_groth` here is the floor for an order released NOW — the number the form shows.
    An order scheduled further out is priced higher (headroom_for), per item, in `create`."""
    from .. import payouts  # local: payouts imports the worker module this router also uses

    now = time.time()
    hit = _fees_cache.get(asset.key)
    if hit and now - float(hit["at"]) < FEES_CACHE_S:
        return hit["fees"]
    try:
        relayer_fee, _floor, _detail = await payouts.relayer_fee_for(asset, workers.get_rpc())
    except Exception as e:  # noqa: BLE001 — gas or price unreadable: say so, never guess a fee
        raise HTTPException(
            503,
            f"could not read the live bridge relayer fee ({type(e).__name__}) — nothing was "
            f"scheduled; try again in a moment",
        ) from e
    fees = {
        "fee_bps": settings.fee_bps,
        "relayer_fee_groth_now": int(relayer_fee),
        "min_amount_groth": _min_amount_groth(int(relayer_fee)),
        "bridge_eta_s": int(settings.bridge_eta_s),
    }
    _fees_cache[asset.key] = {"at": now, "fees": fees}
    return fees


def clear_fees_cache() -> None:
    _fees_cache.clear()


# ------------------------------------------------------------------------- the atomic reservation


def _res_id(account_id: str, asset: str) -> str:
    return f"{account_id}:{asset}"


async def reserve(account_id: str, asset: str, need: int) -> bool:
    """Atomically admit `need` groth against Available. Returns False when there is no room.

    ⛔ THE CLAIM COMES FIRST AND THE BALANCE IS READ AFTER IT. This used to read Available and
    then use that number as a constant inside the conditional update: between the two awaits
    another request could claim, write its entries and release, so `pending` was back at 0 and
    the filter passed on a balance that was already spent — both batches were admitted and only
    the `after < 0` belt reversed one. Incrementing FIRST and reading the ledger AFTERWARDS makes
    the two facts commute: a request that finished before the read is in `available`, and one
    still in flight is in `pending`, so the sum can never exceed the balance. The increment is
    backed out immediately when it does not fit, before a single entry is written."""
    d = db()
    key = _res_id(account_id, asset)
    now = time.time()
    await d.reservations.update_one(
        {"_id": key},
        {"$setOnInsert": {"pending": 0, "account_id": account_id, "asset": asset, "at": now}},
        upsert=True,
    )
    # a crash between claim and release would otherwise block the account forever. Clearing it is
    # safe: the ledger, not this counter, is the balance — pending is only the in-flight cushion.
    # `$ne: 0` and not `$gt: 0`, so a counter that somehow went NEGATIVE is repaired too: a
    # negative cushion is a permanently DISARMED gate, and `$gt: 0` never matches one.
    await d.reservations.update_one(
        {"_id": key, "pending": {"$ne": 0}, "at": {"$lt": now - RESERVATION_STALE_S}},
        {"$set": {"pending": 0, "at": now, "stale_cleared_at": now}},
    )
    before = await d.reservations.find_one_and_update(
        {"_id": key}, {"$inc": {"pending": need}, "$set": {"at": now}}, upsert=True
    )
    pending = int((before or {}).get("pending") or 0) + need
    available = (await ledger.balance(account_id, asset))["available"]
    if pending > available:
        await release(account_id, asset, need)
        return False
    return True


async def release(account_id: str, asset: str, need: int) -> None:
    """Give the reservation back: the entries have landed (Available already reflects them) or
    nothing was written at all.

    ⛔ CLAMPED. This was an unconditional `$inc -need`, and the stale-clear above can zero a
    cushion that is still in flight — so the release that followed drove `pending` NEGATIVE and
    it never recovered, leaving that account's admission filter permanently looser than intended
    (and the balance defended only by the compensation belt). The decrement now happens only
    while there is something to decrement, and otherwise the counter is set to the floor."""
    d = db()
    key = _res_id(account_id, asset)
    now = time.time()
    res = await d.reservations.update_one(
        {"_id": key, "pending": {"$gte": need}},
        {"$inc": {"pending": -need}, "$set": {"at": now}},
    )
    if not res.matched_count:
        await d.reservations.update_one(
            {"_id": key}, {"$set": {"pending": 0, "at": now, "clamped_at": now}}
        )


# ------------------------------------------------------------------------------- writing the rows


async def _page_operator(kind: str, text: str, ids: list[str]) -> None:
    """Page NOW about orders that are already live — and never let the paging itself be the
    caller's error. §9.7: ids only, never `W`.

    This is the last thing standing between a silent defect and the operator, and it is called
    from the paths where Mongo is already misbehaving — so `tg.alert` (which writes an event row
    before it sends) is exactly as likely to fail as the thing it is reporting. A failure to page
    is logged and swallowed: it must not turn a live, debited, correctly-written batch into a
    500 that the client retries into a second set of payouts."""
    try:
        await tg.alert(kind, text, request_ids=ids)
    except Exception:  # noqa: BLE001 — the pager failing is not the caller's problem
        log.exception("could not page the operator (%s) about %s", kind, ",".join(ids))


async def _void(aid: str, asset: str, rid: str, total: int, why: str) -> None:
    """Give one item's debit back. Idempotent: a second cancel for a ref is refused by design."""
    try:
        await ledger.cancel(aid, asset, total, rid, why, refund_of=f"schedule:{rid}")
    except ledger.AlreadyRefunded:
        pass


async def _write_items(
    aid: str,
    asset: str,
    mode: str,
    items: list[dict[str, Any]],
    relayer_fee_groth: int,
    dest_head: int = 0,
) -> list[str]:
    """The debit FIRST, then the row it belongs to — and THE WHOLE BATCH OR NONE OF IT.

    Two laws meet here. (1) A request row can never exist without the ledger entry that paid for
    it, so the `schedule` entry is appended first and cancel always has its evidence. (2) A batch
    is admitted or refused as a unit. This loop used to compensate only the item it failed on and
    re-raise: items 1..k−1 kept both their debit and their live `scheduled` row, the caller got a
    500 with no ids and no event at all, and an ordinary retry-on-500 paid the same address a
    second time. ANY failure now reverses everything this call wrote before the error leaves.

    ⛔ AN INSERT THAT RAISED IS NOT AN INSERT THAT DID NOT LAND. A socket timeout or a primary
    step-down can apply the write and still raise at the driver, so the compensation ASKS whether
    the row is there before it refunds. Refunding a row that stands leaves an order with no debit
    — which `payouts._payout_scheduled` releases for free, and whose `ledger.release` then debits
    a `scheduled` bucket that no longer holds it."""
    now = time.time()
    ids: list[str] = []
    try:
        for i in items:
            rid = secrets.token_hex(12)
            total = i["amount"] + i["fee"]
            await ledger.schedule(aid, asset, total, rid, f"{mode} payout scheduled")
            row = {
                "_id": rid,
                "account_id": aid,
                "asset": asset,
                "mode": mode,
                "W": i["W"],
                "amount_groth": i["amount"],
                "fee_groth": i["fee"],
                "deliver_at": i["deliver_at"],
                "release_at": i["release_at"],
                # what the relayer wanted when the order was accepted: the release reads its OWN
                # live number, so this is evidence of what we priced, never an input to the send
                "relayer_fee_groth_estimate": int(relayer_fee_groth),
                "min_amount_groth": i["min_amount"],
                # the block W was proven to carry no contract code at, and when. The guard runs
                # only here, so this is the evidence a release-time re-check would compare to.
                "dest_checked_head": int(dest_head),
                "dest_checked_at": now,
                "status": "scheduled",
                "dest_chain": settings.eth_chain_id,
                "created_at": now,
                "updated_at": now,
            }
            try:
                await db().payout_requests.insert_one(row)
            except Exception:  # noqa: BLE001 — ask before you refund; never refund on a guess
                try:
                    landed = await db().payout_requests.find_one({"_id": rid}, {"_id": 1})
                except Exception:  # noqa: BLE001 — we cannot even ask: the debit STAYS with it
                    ids.append(rid)
                    raise
                if landed is None:
                    await _void(
                        aid,
                        asset,
                        rid,
                        total,
                        "the payout request row could not be written; the debit is reversed",
                    )
                    raise
                # the row IS there: the write landed and only the answer was lost.
            ids.append(rid)
    except Exception as e:  # noqa: BLE001 — reverse the batch, then say what still stands
        standing = await _roll_back(aid, asset, ids, "the batch could not be written in full")
        raise PartialBatch(standing) from e
    return ids


async def _roll_back(aid: str, asset: str, ids: list[str], why: str) -> list[str]:
    """Undo a withdrawal that must not stand — with offsetting entries, never by deleting.

    Returns the ids it could NOT reverse. A row that has already moved past `scheduled` is a
    payout the worker has claimed and may have in flight; telling the user "nothing moved" about
    it would be a lie, so it is named, and the operator is paged with the count.

    ⛔ THIS FUNCTION NEVER RAISES. It is called on rows that are already live, where an exception
    is a 500 that hides the ids and invites a duplicating retry — so EVERY failure it meets is
    reported by NAMING that id in `standing`, and the whole per-id reversal (not just the refund)
    is guarded, so one id whose row or entry could not even be read does not abandon the rest."""
    standing: list[str] = []
    for rid in ids:
        try:
            row = await db().payout_requests.find_one_and_update(
                {"_id": rid, "status": "scheduled"},
                {
                    "$set": {
                        "status": "cancelled",
                        "cancelled_at": time.time(),
                        "rolled_back": True,
                        "roll_back_reason": why,
                    }
                },
            )
            entry = await ledger.find_entry("schedule", rid)
            refunded = await ledger.find_entry("cancel", rid)
            if not row:
                if entry and not refunded:  # it exists, it is debited, and we could not reverse it
                    standing.append(rid)
                continue
            if not entry:
                continue  # nothing was ever debited for it: refunding would MINT money
            try:
                await ledger.cancel(
                    aid,
                    asset,
                    int(entry["groth"]),
                    rid,
                    f"rolled back: {why}",
                    refund_of=f"schedule:{rid}",
                )
            except ledger.AlreadyRefunded:
                pass
        except Exception:  # noqa: BLE001 — one failed reversal must not abandon the others
            log.exception("withdrawal roll back: %s could not be reversed", rid)
            standing.append(rid)
    await _page_operator(
        "withdrawal_rolled_back",
        f"ROLLED BACK: a withdrawal was reversed ({why})"
        + (
            f" — {len(standing)} order(s) could NOT be reversed and STILL STAND"
            if standing
            else " — every order was reversed"
        )
        + "; check the ledger for this account",
        ids,
    )
    return standing


# ------------------------------------------------------------------------------------- the routes


@router.get("/fees")
async def fees(asset: str = "ETH", acct=auth.Account):
    """What the form has to know before it can be filled in: our cut, the bridge fee we pay out
    of it right now, the smallest payout that works at this gas price, and how early an order
    has to leave to arrive on time."""
    try:
        a = get_asset(asset)
    except KeyError as e:
        raise HTTPException(400, str(e.args[0])) from e
    return await live_fees(a)


@router.post("")
async def create(body: WithdrawIn, acct=auth.Account):
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    aid = acct["account_id"]
    asset = body.asset.upper()
    if asset not in ASSETS:
        raise HTTPException(400, f"unknown asset {body.asset!r}")
    if body.mode not in MODES:
        raise HTTPException(400, "mode must be direct or instant")
    if asset not in PAYOUT_ASSETS:
        # the executor refuses this row forever ("v1 payouts are ETH only"), so admitting it
        # would move the user's balance into `scheduled` for an order that can never execute.
        raise HTTPException(
            400,
            f"v1 payouts are {'/'.join(PAYOUT_ASSETS)} only — a {asset} withdrawal cannot be "
            f"executed by this deployment, so nothing was scheduled",
        )
    enabled = (
        settings.payout_direct_enabled if body.mode == "direct" else settings.payout_instant_enabled
    )
    if not enabled:
        raise HTTPException(
            409,
            f"{body.mode} payouts are not enabled on this deployment yet"
            + (
                " (no float / distributors)"
                if body.mode == "instant"
                else " (no Beam treasury wallet)"
            ),
        )
    if len(body.items) > settings.max_items_per_withdrawal:
        raise HTTPException(
            400, f"at most {settings.max_items_per_withdrawal} wallets per withdrawal"
        )

    now = time.time()
    eta_s = eta_for(body.mode)
    live = await live_fees(get_asset(asset))
    relayer_now = int(live["relayer_fee_groth_now"])
    min_amount = int(live["min_amount_groth"])
    items: list[dict[str, Any]] = []
    denoms = settings.denominations
    for it in body.items:
        w = normalise_address(it.W)
        # POSITIVE: the value must BE a delivery time in range. Phrased the other way round
        # ("refuse it when it is out of range") a NaN passes, because every comparison with NaN
        # is False — and pydantic will hand us one from the ordinary JSON string "NaN".
        if it.deliver_at is not None and not (
            MIN_DELIVER_AT <= float(it.deliver_at) <= now + settings.max_window_s
        ):
            raise HTTPException(
                400,
                f"deliver_at must be a unix time in the next {settings.max_window_s // 86400} "
                f"days — a delivery more than {settings.max_window_s // 86400} days away (or a "
                f"time that is not a time) is refused; schedule it closer to the time you want "
                f"the money",
            )
        release_at = release_at_for(it.deliver_at, now, eta_s)
        item_min = _min_amount_groth(relayer_now, release_at - now)
        if it.amount_groth < item_min:
            raise HTTPException(
                400,
                f"each payout must be at least {item_min} groth ({item_min / 1e8:g} {asset}) "
                f"— below that the {settings.fee_bps / 100:g}% fee does not cover the bridge fee "
                f"we pay for it ({relayer_now} groth at this gas price"
                + (
                    f", and an order releasing in {int(release_at - now)}s has to fund a "
                    f"{headroom_for(release_at - now):.2f}× rise in it before it is due"
                    if item_min > min_amount
                    else ""
                )
                + ")",
            )
        if body.mode == "instant" and not any(it.amount_groth % d == 0 for d in denoms):
            raise HTTPException(
                400,
                f"instant payouts must be a multiple of a denomination ({', '.join(str(d) for d in denoms)} groth)",
            )
        items.append(
            {
                "W": w,
                "amount": it.amount_groth,
                "fee": fee_for(it.amount_groth),
                "deliver_at": it.deliver_at,
                "release_at": release_at,
                "min_amount": item_min,
            }
        )
    # the chain is asked BEFORE a groth is reserved: a contract in the list refuses the batch
    dest_head, _dest_url = await refuse_contracts([i["W"] for i in items])

    need = sum(i["amount"] + i["fee"] for i in items)
    available = (await ledger.balance(aid, asset))["available"]
    if not await reserve(aid, asset, need):
        # the whole batch is one decision: nothing is written, and the number the user needs to
        # act on (how much SHORT they are) travels in the message and in a header a UI can read
        # without parsing English.
        shortfall = max(0, need - available)
        raise HTTPException(
            409,
            f"insufficient {asset} balance: need {need} groth (amount + {settings.fee_bps / 100:g}% fee), "
            f"available {available}, shortfall_groth {shortfall} (another withdrawal of yours "
            f"may be in flight)",
            headers={"X-Shortfall-Groth": str(shortfall)},
        )
    try:
        try:
            ids = await _write_items(aid, asset, body.mode, items, relayer_now, dest_head)
        finally:
            try:
                await release(aid, asset, need)
            except Exception:  # noqa: BLE001 — the cushion is not the money and not the caller's
                # a cushion that could not be given back is repaired by the stale-clear in
                # `reserve` (RESERVATION_STALE_S); raising here would replace a written batch's
                # 200 with a 500 that hides its ids. Logged, never paged: law 15, a monitor that
                # pages about a self-healing counter trains the operator to ignore the pager.
                log.exception("withdrawal: the %s reservation of %s could not be released", asset, aid)
    except PartialBatch as e:
        # the caller must never see a 5xx while an order of theirs is live: either the batch
        # reversed itself (retrying is safe) or it did not, and then the ids that stand are named.
        if e.standing:
            raise HTTPException(
                500,
                f"the withdrawal could not be written in full and {len(e.standing)} order(s) "
                f"could not be reversed ({', '.join(e.standing)}) — the operator has been paged; "
                f"check /v1/account before retrying",
            ) from e
        raise HTTPException(
            503,
            "the withdrawal could not be written and was reversed — nothing was scheduled and "
            "nothing was debited; try again in a moment",
        ) from e
    # ══════════════════ THE ORDERS ARE LIVE AND DEBITED FROM HERE DOWN ══════════════════
    # ⛔ NOTHING BELOW THIS LINE MAY TURN INTO A 5xx. The answer is built FIRST, so every path
    # from here has the ids in its hand: a 500 would tell the caller the withdrawal failed while
    # the worker pays it, and withhold the only ids they could cancel with — an ordinary
    # retry-on-500 then schedules the whole batch a second time. The belt below may still
    # REVERSE the batch (409, naming what it reversed); anything that merely fails is logged,
    # paged and swallowed, and this answer leaves anyway.
    out = {
        "request_ids": ids,
        "fee_groth": sum(i["fee"] for i in items),
        "total_debited_groth": need,
        "relayer_fee_groth_estimate": relayer_now,
        "min_amount_groth": min_amount,
        "bridge_eta_s": eta_s,
        "items": [
            {
                "request_id": rid,
                "W": i["W"],
                "amount_groth": i["amount"],
                "fee_groth": i["fee"],
                "deliver_at": i["deliver_at"],
                "release_at": i["release_at"],
                "min_amount_groth": i["min_amount"],
            }
            for rid, i in zip(ids, items, strict=True)
        ],
    }
    after: int | None
    try:
        after = (await ledger.balance(aid, asset))["available"]
    except Exception:  # noqa: BLE001 — AN UNREADABLE BALANCE IS NOT A NEGATIVE BALANCE (law 8)
        # The belt did not run, so we cannot claim the account is sound — but we also cannot
        # reverse a correctly-written batch on a reading we never got. `None` is "we do not
        # know", which is explicitly not "< 0": say so loudly, and carry on writing the events
        # these live orders are owed. The operator (and the reconciler) decide the rest.
        after = None
        log.exception("withdrawal %s: the post-write balance could not be read", ",".join(ids))
        await _page_operator(
            "withdrawal_belt_unread",
            f"BELT DID NOT RUN: {len(ids)} {asset} payout order(s) were written and debited and "
            f"the post-write balance could NOT be read, so the overdraft check never happened — "
            f"the orders stand and the caller has their ids; check this account",
            ids,
        )
    if after is not None and after < 0:  # belt: nothing may leave the account overdrawn, ever
        # A REFUSAL, NOT A FAILURE: reverse what we just wrote and answer 409 naming BOTH sets —
        # the ids that were reversed (retrying is safe for those) and any that still stand.
        standing = await _roll_back(aid, asset, ids, "the balance would have gone negative")
        reversed_ids = [r for r in ids if r not in standing]
        detail = f"insufficient {asset} balance — the withdrawal was rolled back"
        if reversed_ids:
            detail += f" ({len(reversed_ids)} order(s) reversed: {', '.join(reversed_ids)})"
        detail += (
            f", but {len(standing)} order(s) had already been claimed for release and still "
            f"stand ({', '.join(standing)}); the operator has been paged"
            if standing
            else " and nothing moved"
        )
        raise HTTPException(409, detail, headers={"X-Reversed-Request-Ids": ",".join(reversed_ids)})
    failed_events: list[str] = []
    for rid, i in zip(ids, items, strict=True):
        # §9.7: ids only — the request id, never W. api.log otherwise pairs request_id →
        # destination and payout_requests pairs request_id → account_id, which is the whole
        # product defeated. ONE event per order, so the operator's log has one row per order.
        # Guarded PER ITEM: an event that will not write is an observability failure, never a
        # failed payout — and one that fails must not swallow the events of the orders after it.
        try:
            await tg.queue(
                "withdrawal_requested",
                f"Payout scheduled: {asset} {tg.fmt_groth(i['amount'])} "
                f"(fee {tg.fmt_groth(i['fee'])}), releases in {max(0, int(i['release_at'] - now))}s",
                request_id=rid,
            )
        except Exception:  # noqa: BLE001 — law 11: a guard that fails politely is a guard that fails
            log.exception("withdrawal %s: its `withdrawal_requested` event could not be written", rid)
            failed_events.append(rid)
    if failed_events:
        await _page_operator(
            "withdrawal_events_unwritten",
            f"EVENT MISSING: {len(failed_events)} of {len(ids)} scheduled {asset} payout order(s) "
            f"could not write their `withdrawal_requested` event — the orders ARE live and the "
            f"caller has their ids; the operator log is the thing that is short",
            failed_events,
        )
    return out


@router.post("/{request_id}/cancel")
async def cancel(request_id: str, acct=auth.Account):
    """Cancel a scheduled order and give its debit back.

    ⛔ REPAIRABLE, NOT TERMINAL. The row is flipped to `cancelled` first and the refund appended
    second, so a failure between the two left the row terminal (the worker only releases
    `scheduled`), the retry answering 409 "request is cancelled", the groth frozen in
    `scheduled` forever and NOTHING paged. A `cancelled` row whose `schedule` entry has no
    matching `cancel` entry is therefore an UNFINISHED cancel, and it re-enters the refund path
    here — the same doctrine `ledger.release` states for its own two halves: a partial write is
    repaired by the next call instead of being declared finished."""
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    aid = acct["account_id"]
    now = time.time()
    row = await db().payout_requests.find_one_and_update(
        {"_id": request_id, "account_id": aid, "status": "scheduled"},
        {"$set": {"status": "cancelled", "cancelled_at": now, "updated_at": now}},
    )
    if not row:
        existing = await db().payout_requests.find_one({"_id": request_id, "account_id": aid})
        if not existing:
            raise HTTPException(404, "unknown request")
        unfinished = existing.get("status") == "cancelled" and not await ledger.find_entry(
            "cancel", request_id
        )
        if not unfinished:
            raise HTTPException(
                409, f"request is {existing['status']} — only a scheduled request can be cancelled"
            )
        row = existing  # the flip landed and the refund did not: finish it now
    entry = await ledger.find_entry("schedule", request_id)
    if not entry:
        # the row exists but nothing was ever debited for it: refunding would MINT money
        await tg.alert(
            "withdrawal_cancel_no_debit",
            "CANCELLED WITHOUT A REFUND: a scheduled payout had no `schedule` ledger entry, so "
            "nothing was debited and nothing was returned — investigate this row",
            request_id=request_id,
        )
        return {"cancelled": request_id, "refunded_groth": 0}
    total = int(entry["groth"])
    try:
        await ledger.cancel(
            aid,
            row["asset"],
            total,
            request_id,
            "cancelled by the user before release",
            refund_of=f"schedule:{request_id}",
        )
    except ledger.AlreadyRefunded:
        return {"cancelled": request_id, "refunded_groth": 0}
    except Exception as e:  # noqa: BLE001 — a frozen balance is not allowed to be silent
        await tg.alert(
            "withdrawal_cancel_refund_failed",
            "CANCELLED WITHOUT ITS REFUND: the request row is `cancelled` and the offsetting "
            "ledger entry could NOT be written, so the groth is stuck in `scheduled` — the next "
            "cancel of this id completes it; check this account",
            request_id=request_id,
        )
        raise HTTPException(
            503,
            "the request is cancelled but the refund could not be written — nothing is lost; "
            "call cancel again in a moment and it will be completed",
        ) from e
    await tg.queue(
        "withdrawal_cancelled",
        f"Withdrawal cancelled: {row['asset']} {tg.fmt_groth(total)} back to Available",
        request_id=request_id,
    )
    return {"cancelled": request_id, "refunded_groth": total}
