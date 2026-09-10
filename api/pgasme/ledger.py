"""The account ledger — append-only entries, balances are sums, one balance per asset.

Every entry carries explicit deltas for the three visible buckets so a balance is one
aggregation and never a stored number (the arb_tracker `arbdb` discipline):

    available  += d_avail
    scheduled  += d_sched
    sent       += d_sent

Amounts are Beam groth (8 decimals: 1 ETH = 1e8 groth, 1 DAI = 1e8 groth, 1 WBTC = 1e8 groth).
Every entry names its `asset` (ETH | DAI | WBTC) and never mixes them. Nothing is ever edited
or deleted; a mistake is corrected by a further entry that says why.
"""

from __future__ import annotations

import time
from typing import Any

from pymongo.errors import DuplicateKeyError

from .assets import ASSETS
from .db import _ensure, db

KINDS = (
    "credit",
    "schedule",
    # the bridge fee an order funds its own crossing with, charged EXPLICITLY since 2026-09-10
    # (it used to come out of our 2%). A distinct kind on both sides of the order's life so an
    # audit can tell "our cut" from "the pass-through" without re-deriving either: the debit at
    # scheduling is `schedule_bridge_fee`, and what leaves the account at release is `bridge_fee`.
    "schedule_bridge_fee",
    "release",
    "fee",
    "bridge_fee",
    # …and what the crossing DID NOT SPEND, back into Available at settlement (T45, 2026-09-10).
    # The bridge fee is quoted from a live gas price plus headroom, so it is an ESTIMATE — and
    # "the bridge at cost" is only true if the difference comes back. It is its own kind, on the
    # Available side, because it is not a correction of the `bridge_fee` entry (nothing here is
    # ever edited) and an audit must be able to separate "what the crossing cost" from "what we
    # over-collected and returned".
    "bridge_fee_refund",
    # the distributor's GAS for an instant payout, charged at cost and cleared at `sent` (T34).
    # A distinct kind from `bridge_fee` on purpose: the two are different costs on different
    # chains and an audit that added them up would be adding a bridge crossing to an Ethereum
    # transfer. The debit at scheduling is still `schedule_bridge_fee` — one order reserves one
    # pass-through, whichever venue ends up spending it.
    "instant_gas_fee",
    "cancel",
    "refund",
    "adjust",
)
# the two halves of what one payout order takes out of Available. `debited_groth()` is the ONE
# reader of this pair — every refund path asks it, so a refund can never be a recomputed number.
SCHEDULE_KINDS = ("schedule", "schedule_bridge_fee")
# one credit per ref, enforced by the database and not by a read-then-write
CREDIT_REF_INDEX = "uniq_credit_ref"
# …and one `release`, one `fee` and one `bridge_fee` per ref, for the same reason: the release
# path was a read-then-write with no database guard at all, so two processors that both saw the
# payout unbooked would both debit the account for it. ONE INDEX PER KIND — see ensure_indexes().
RELEASE_REF_INDEX = "uniq_release_ref"
FEE_REF_INDEX = "uniq_fee_ref"
BRIDGE_FEE_REF_INDEX = "uniq_bridge_fee_ref"
INSTANT_GAS_FEE_REF_INDEX = "uniq_instant_gas_fee_ref"
# …and ONE `cancel`, for the strongest reason of the lot: it is the entry that GIVES MONEY BACK.
# Four paths refund (the user's cancel route, `_void`, `_roll_back`, the payout worker's own
# refund) and its guard was a read-then-write, so two of them in flight together both read "not
# refunded yet" and both appended — the whole debit back into Available twice, money invented.
CANCEL_REF_INDEX = "uniq_cancel_ref"
# …and ONE `bridge_fee_refund`, for exactly `cancel`'s reason: it is the OTHER entry that puts
# money back into Available. It is appended on the settlement path, which is a path a retry
# re-enters by design (a partial write is repaired by the next pass rather than declared
# finished), so "we already did this" has to be a fact in the database and not a read.
BRIDGE_FEE_REFUND_REF_INDEX = "uniq_bridge_fee_refund_ref"
RELEASE_KINDS = ("release", "fee", "bridge_fee", "instant_gas_fee")


class AlreadyCredited(RuntimeError):
    """The (credit, ref) unique index refused a second credit for the same deposit. The money is
    already in the balance; the caller must treat this as success, never as a failure."""


class AlreadyRefunded(RuntimeError):
    """A cancel entry for this ref already exists — the money is already back in Available."""


class AlreadyBridgeFeeRefunded(RuntimeError):
    """A `bridge_fee_refund` for this ref already exists — the unspent bridge fee is already back
    in Available. The caller treats this as success: it is the settlement path being re-entered,
    which is the ordinary shape of a repair, never a second refund."""


class AlreadyReleased(RuntimeError):
    """The unique (release, ref) index refused a SECOND release entry for one order.

    ⛔ **THIS IS THE MONEY-LEVEL GUARD AGAINST A DOUBLE SPEND** (T40, admin 2026-09-10: *"make
    sure you won't have double spending"*). Since a delayed payout is retried, two crossings for
    one order became possible in principle — `payouts.previous_attempt_dead` is what stops one
    starting while the other could still settle. If both ever settle anyway, the index is what
    keeps the ACCOUNT debited exactly once, and this exception is how the caller finds out
    instead of quietly treating it as "already done". The caller holds the row for a human: the
    treasury has paid twice at that point and no automatic path may resolve it.

    ⚠️ It is raised only on a genuine RACE. `release()` reads first and skips a half that
    already exists, so the ordinary idempotent repair of a partial write never reaches it."""


async def ensure_indexes() -> None:
    """The ledger's own guarantees. Called at worker start (db.ensure_indexes owns the rest):
    a second `credit` for one ref is refused by the DATABASE, so two racing writers cannot both
    pass a has_credit() read and both append.

    ⛔ **A partial index filter is not a query.** `partialFilterExpression` accepts only
    equality, `$exists: true`, `$type`, the range operators and a top-level `$and`; **`$in` is
    supported only from MongoDB 6.0** and PRODUCTION RUNS 5.0, which answers
    `CannotCreateIndex (67) … unsupported expression in partial index` — proved on mongod
    5.0.29. This function runs at worker start and its failure is reported through
    `/v1/health.indexes_ok`, which `deploy.sh` refuses a deploy on: one `$in` here is a boot
    failure and a blocked deploy, not a slow query.

    So `release` and `fee` get ONE INDEX EACH with an equality filter. The two also carry
    DIFFERENT key patterns — 5.0.29 does accept two partial indexes on one key pattern that
    differ only by their filter, but the documented restriction says it may not, and the fee
    guard loses nothing by indexing `ref` alone: its filter already pins the kind, so
    "unique on ref within kind=fee" IS "one fee per ref".

    Created here with db._ensure, the one implementation that repairs an index whose options
    changed (IndexOptionsConflict 85 / IndexKeySpecsConflict 86) instead of raising at boot."""
    await _ensure(
        db().entries,
        [("kind", 1), ("ref", 1)],
        unique=True,
        partialFilterExpression={"kind": "credit"},
        name=CREDIT_REF_INDEX,
    )
    await _ensure(
        db().entries,
        [("ref", 1), ("kind", 1)],
        unique=True,
        partialFilterExpression={"kind": "release"},
        name=RELEASE_REF_INDEX,
    )
    await _ensure(
        db().entries,
        [("ref", 1)],
        unique=True,
        partialFilterExpression={"kind": "fee"},
        name=FEE_REF_INDEX,
    )
    # a FOURTH distinct key pattern for the same reason the `fee` one differs from `release`'s:
    # 5.0 does accept two partial indexes that differ only by their filter, but the documented
    # restriction says it may not, and one ref belongs to exactly one account — so "unique on
    # (ref, account_id) within kind=bridge_fee" IS "one bridge_fee per ref".
    await _ensure(
        db().entries,
        [("ref", 1), ("account_id", 1)],
        unique=True,
        partialFilterExpression={"kind": "bridge_fee"},
        name=BRIDGE_FEE_REF_INDEX,
    )
    # a SIXTH distinct key pattern, for the instant path's gas (T34), by the rule the four
    # above already follow: one ref belongs to one account, so "unique on (ref, account_id,
    # kind) within kind=instant_gas_fee" IS "one gas charge per order". The key pattern differs
    # from every other one here because 5.0's documented restriction says two partial indexes on
    # one pattern may be refused — and a boot that cannot build an index is a blocked deploy.
    await _ensure(
        db().entries,
        [("ref", 1), ("account_id", 1), ("kind", 1)],
        unique=True,
        partialFilterExpression={"kind": "instant_gas_fee"},
        name=INSTANT_GAS_FEE_REF_INDEX,
    )
    # a FIFTH distinct key pattern, by the same rule: `(account_id, ref)` is the same pair the
    # bridge-fee index carries in the other order, and one ref belongs to exactly one account —
    # so "unique on (account_id, ref) within kind=cancel" IS "one refund per ref". The order is
    # not arbitrary: this index also serves "every refund this account has ever had".
    await _ensure(
        db().entries,
        [("account_id", 1), ("ref", 1)],
        unique=True,
        partialFilterExpression={"kind": "cancel"},
        name=CANCEL_REF_INDEX,
    )
    # a SEVENTH distinct key pattern, by the rule every one above follows (5.0's documented
    # restriction on two partial indexes over one pattern): one ref belongs to one account and to
    # one asset, so "unique on (ref, kind, account_id) within kind=bridge_fee_refund" IS "one
    # refund of the unspent bridge fee per order".
    await _ensure(
        db().entries,
        [("ref", 1), ("kind", 1), ("account_id", 1)],
        unique=True,
        partialFilterExpression={"kind": "bridge_fee_refund"},
        name=BRIDGE_FEE_REFUND_REF_INDEX,
    )


def _asset(asset: str) -> str:
    a = (asset or "").upper()
    if a not in ASSETS:
        raise ValueError(f"unknown ledger asset {asset!r}")
    return a


async def _append(
    account_id: str,
    asset: str,
    kind: str,
    groth: int,
    d_avail: int,
    d_sched: int,
    d_sent: int,
    ref: str,
    note: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind {kind}")
    doc = {
        "account_id": account_id,
        "asset": _asset(asset),
        "kind": kind,
        "groth": int(groth),
        "d_avail": int(d_avail),
        "d_sched": int(d_sched),
        "d_sent": int(d_sent),
        "ref": ref,
        "note": note,
        "at": time.time(),
        **(extra or {}),
    }
    await db().entries.insert_one(doc)
    doc.pop("_id", None)
    return doc


async def credit(
    account_id: str, asset: str, groth: int, ref: str, note: str = ""
) -> dict[str, Any]:
    """A deposit landed: Available += groth. No fee at deposit (founder rule).

    Raises AlreadyCredited when the unique (credit, ref) index refuses the row — the deposit is
    already paid for and nothing more is owed."""
    try:
        return await _append(account_id, asset, "credit", groth, groth, 0, 0, ref, note)
    except DuplicateKeyError as e:
        raise AlreadyCredited(f"credit for {ref} already exists") from e


async def schedule(
    account_id: str,
    asset: str,
    groth_incl_fee: int,
    ref: str,
    note: str = "",
    bridge_fee_groth: int = 0,
) -> list[dict[str, Any]]:
    """A withdrawal request: Available -= (amount + fee + bridge fee), Scheduled += the same.

    TWO ENTRIES, ONE DEBIT (2026-09-10). `groth_incl_fee` is amount + OUR cut; the bridge fee the
    order funds its own crossing with is a pass-through and is appended beside it under its own
    kind, so an audit can separate revenue from cost without re-deriving either from a rate.

    ⛔ THE ORDER OF THE TWO APPENDS IS LOAD-BEARING. `debited_groth()` — the one reader every
    refund path uses — keys on the presence of the `schedule` entry, so that half goes FIRST: a
    failure between the two leaves a debit of exactly `groth_incl_fee`, which is exactly what the
    refund then returns. Written the other way round, a bridge-fee debit whose `schedule` half
    never landed would be money taken out of Available that no refund path can see."""
    out = [
        await _append(
            account_id,
            asset,
            "schedule",
            groth_incl_fee,
            -groth_incl_fee,
            groth_incl_fee,
            0,
            ref,
            note,
        )
    ]
    bridge = int(bridge_fee_groth or 0)
    if bridge > 0:
        out.append(
            await _append(
                account_id,
                asset,
                "schedule_bridge_fee",
                bridge,
                -bridge,
                bridge,
                0,
                ref,
                "bridge fee held for this crossing",
            )
        )
    return out


async def debited_groth(ref: str) -> int | None:
    """What this order actually took out of Available at scheduling, or None if nothing did.

    THE ONE READER OF THE DEBIT (law 14: a record that must exist on every path belongs in a
    helper called from every path). Every refund — cancel, the batch roll back, a payout that
    failed before anything was sent — asks THIS, never a recomputed `amount + fee`: a refund is
    the mirror of a debit that landed, and a debit that landed is a fact in the ledger. `None`
    means no `schedule` entry exists, and refunding against that would MINT money."""
    base = await find_entry("schedule", ref)
    if not base:
        return None
    bridge = await find_entry("schedule_bridge_fee", ref)
    return int(base["groth"]) + int((bridge or {}).get("groth") or 0)


async def release(
    account_id: str,
    asset: str,
    groth: int,
    fee_groth: int,
    ref: str,
    note: str = "",
    bridge_fee_groth: int = 0,
    gas_fee_groth: int = 0,
) -> list[dict[str, Any]]:
    """A payout went out: Scheduled -= amount + fee + the pass-through; Sent += amount; the fees leave.

    ⛔ ONE UNIT, EVEN THOUGH IT IS TWO ENTRIES. These were two unguarded appends and the caller
    guarded on the first alone: when the `fee` append failed (a Mongo blip, a step-down) the
    `release` entry existed, every later pass short-circuited on it, and the 2% sat in the
    account's Scheduled bucket forever — money the user could neither spend nor get back.

    So each half carries its own database guard (`RELEASE_REF_INDEX` · `FEE_REF_INDEX`) and it is
    idempotent PER KIND: it appends whichever half is missing and refuses a second of either.
    A partial write is repaired by the next call instead of being declared finished."""
    out: list[dict[str, Any]] = []
    halves = [
        ("release", int(groth), -int(groth), int(groth), note),
        ("fee", int(fee_groth), -int(fee_groth), 0, "2% at unlock"),
    ]
    # ⛔ WHATEVER THE SCHEDULE DEBITED, THE RELEASE MUST CLEAR. The bridge fee went into
    # `Scheduled` with the rest of the order (`schedule_bridge_fee`); a release that cleared only
    # amount + fee would leave it stuck there forever — money the user can neither spend nor get
    # back, which is the exact defect the two halves above were split for. A row written before
    # the bridge fee was itemised carries 0 and gets no third entry: it never debited one.
    if int(bridge_fee_groth or 0) > 0:
        halves.append(
            (
                "bridge_fee",
                int(bridge_fee_groth),
                -int(bridge_fee_groth),
                0,
                "the bridge fee this order funded its crossing with",
            )
        )
    # …and the same rule one venue along (T34): an INSTANT payout funds a 21,000-gas Ethereum
    # transfer instead of a bridge crossing, reserved by the same `schedule_bridge_fee` debit and
    # cleared here under its own kind. An order has one pass-through or the other, never both —
    # the caller passes the one its mode actually spent.
    if int(gas_fee_groth or 0) > 0:
        halves.append(
            (
                "instant_gas_fee",
                int(gas_fee_groth),
                -int(gas_fee_groth),
                0,
                "the distributor's gas this order funded its transfer with",
            )
        )
    for kind, amount, d_sched, d_sent, why in halves:
        if await find_entry(kind, ref):
            continue
        try:
            out.append(await _append(account_id, asset, kind, amount, 0, d_sched, d_sent, ref, why))
        except DuplicateKeyError as e:
            if kind == "release":
                # ⛔ NOT "the entry exists, which is what matters". On the RELEASE half a
                # duplicate means two writers both believed this order had not been paid out —
                # i.e. two crossings for one order — and swallowing it would advance both of
                # them. The fee halves stay idempotent: they are corrections of a partial write,
                # not evidence that money moved twice.
                raise AlreadyReleased(f"a release entry for {ref} already exists") from e
            continue  # another writer got there first; the entry exists, which is what matters
    return out


async def bridge_fee_refund(
    account_id: str,
    asset: str,
    groth: int,
    ref: str,
    funded_groth: int,
    paid_groth: int,
    note: str = "",
) -> dict[str, Any]:
    """The crossing settled for LESS than it was quoted: Available += the difference (T45).

    ⛔ **THE BRIDGE FEE IS AN ESTIMATE, AND "AT COST" IS A CLAIM ABOUT WHAT IS KEPT.** It is
    quoted at request time from a live gas price plus the headroom the wait needs, and the
    release pays whatever the crossing actually costs. Until 2026-09-10 the difference stayed
    with the treasury — `headroom_for`'s own docstring said so — and two live orders funded
    14,733 groth against a 12,778-groth crossing, so 1,955 groth of each user's money was kept
    for a cost nobody incurred.

    ⛔ **AND IT IS THE OTHER ENTRY THAT PUTS MONEY BACK**, so it carries `cancel`'s guards: the
    read for the ordinary sequential case, the unique partial index for the race. The settlement
    path is re-entered by design (a half-written release is repaired by the next pass), so
    "already refunded" must be a fact in the database.

    The evidence travels ON the row — what was funded, what the crossing paid, what came back —
    because a refund that has to be re-derived from a rate is a refund nobody can audit."""
    amount = int(groth)
    if amount <= 0:
        # never negative and never a no-op entry: a crossing that cost MORE than it funded is a
        # loss we absorb (`payouts.relayer_subsidy_groth`), never a second debit to the user.
        raise ValueError(f"a bridge-fee refund must be positive, got {amount}")
    if await find_entry("bridge_fee_refund", ref):
        raise AlreadyBridgeFeeRefunded(f"{ref} already had its unspent bridge fee refunded")
    try:
        return await _append(
            account_id,
            asset,
            "bridge_fee_refund",
            amount,
            amount,
            0,
            0,
            ref,
            note or "the part of the bridge fee this crossing did not spend",
            {
                "request_id": ref,
                "funded_groth": int(funded_groth),
                "paid_groth": int(paid_groth),
                "refunded_groth": amount,
            },
        )
    except DuplicateKeyError as e:
        raise AlreadyBridgeFeeRefunded(f"{ref} already had its unspent bridge fee refunded") from e


async def cancel(
    account_id: str,
    asset: str,
    groth_incl_fee: int,
    ref: str,
    note: str = "",
    refund_of: str | None = None,
) -> dict[str, Any]:
    """A scheduled request cancelled before release: money back to Available.

    `refund_of` names the entry this reverses (the schedule row that actually debited the
    account). A second cancel for the same ref is refused: a refund is only ever the mirror of
    a debit that landed, and only once.

    ⛔ THE INDEX IS THE GUARD, NOT THE READ. This was a read-then-write and nothing else — the
    same shape `credit` and `release`/`fee` each have a unique partial index for, on the one
    entry that puts money BACK into Available. Four paths refund (the user's cancel route,
    `_void`, `_roll_back`, and the payout worker when a release failed before anything was
    sent); two of them in flight together both read "not refunded yet" and both appended, and
    the account got the whole debit back twice. `CANCEL_REF_INDEX` decides it now. The read
    stays in front of it as a cheap answer for the ordinary sequential case — and as the only
    guard on a database where the index has not been built yet — but it is not what makes this
    safe: a DuplicateKey here is not an error, it is the other writer having already given the
    money back, which is exactly what `AlreadyRefunded` means to every caller."""
    if await find_entry("cancel", ref):
        raise AlreadyRefunded(f"{ref} was already refunded")
    try:
        return await _append(
            account_id,
            asset,
            "cancel",
            groth_incl_fee,
            groth_incl_fee,
            -groth_incl_fee,
            0,
            ref,
            note,
            {"refund_of": refund_of} if refund_of else None,
        )
    except DuplicateKeyError as e:
        raise AlreadyRefunded(f"{ref} was already refunded") from e


def empty_balance() -> dict[str, int]:
    return {"available": 0, "scheduled": 0, "sent": 0}


async def balances(account_id: str) -> dict[str, dict[str, int]]:
    """{ETH:{available,scheduled,sent}, DAI:{...}, WBTC:{...}} — every asset present, zeros included."""
    cur = db().entries.aggregate(
        [
            {"$match": {"account_id": account_id}},
            {
                "$group": {
                    "_id": "$asset",
                    "available": {"$sum": "$d_avail"},
                    "scheduled": {"$sum": "$d_sched"},
                    "sent": {"$sum": "$d_sent"},
                }
            },
        ]
    )
    rows = await cur.to_list(length=len(ASSETS) + 8)
    out = {k: empty_balance() for k in ASSETS}
    for r in rows:
        key = r["_id"]
        if key in out:
            out[key] = {
                "available": int(r["available"]),
                "scheduled": int(r["scheduled"]),
                "sent": int(r["sent"]),
            }
    return out


async def balance(account_id: str, asset: str) -> dict[str, int]:
    return (await balances(account_id))[_asset(asset)]


async def history(account_id: str, limit: int = 100) -> list[dict[str, Any]]:
    cur = db().entries.find({"account_id": account_id}, {"_id": 0}).sort("at", -1).limit(limit)
    return await cur.to_list(length=limit)


async def find_entry(kind: str, ref: str) -> dict[str, Any] | None:
    """The entry of this kind for this ref, or None. The evidence a refund needs."""
    return await db().entries.find_one({"kind": kind, "ref": ref}, {"_id": 0})


async def has_credit(ref: str) -> bool:
    """True if a credit entry with this ref already exists (the double-credit guard)."""
    return await db().entries.find_one({"kind": "credit", "ref": ref}, {"_id": 1}) is not None
