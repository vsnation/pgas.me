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

KINDS = ("credit", "schedule", "release", "fee", "cancel", "refund", "adjust")
# one credit per ref, enforced by the database and not by a read-then-write
CREDIT_REF_INDEX = "uniq_credit_ref"
# …and one `release` and one `fee` per ref, for the same reason: the release path was a
# read-then-write with no database guard at all, so two processors that both saw the payout
# unbooked would both debit the account for it. ONE INDEX PER KIND — see ensure_indexes().
RELEASE_REF_INDEX = "uniq_release_ref"
FEE_REF_INDEX = "uniq_fee_ref"
RELEASE_KINDS = ("release", "fee")


class AlreadyCredited(RuntimeError):
    """The (credit, ref) unique index refused a second credit for the same deposit. The money is
    already in the balance; the caller must treat this as success, never as a failure."""


class AlreadyRefunded(RuntimeError):
    """A cancel entry for this ref already exists — the money is already back in Available."""


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
    account_id: str, asset: str, groth_incl_fee: int, ref: str, note: str = ""
) -> dict[str, Any]:
    """A withdrawal request: Available -= (amount + fee), Scheduled += the same."""
    return await _append(
        account_id, asset, "schedule", groth_incl_fee, -groth_incl_fee, groth_incl_fee, 0, ref, note
    )


async def release(
    account_id: str, asset: str, groth: int, fee_groth: int, ref: str, note: str = ""
) -> list[dict[str, Any]]:
    """A payout went out: Scheduled -= amount + fee; Sent += amount; the fee leaves the account.

    ⛔ ONE UNIT, EVEN THOUGH IT IS TWO ENTRIES. These were two unguarded appends and the caller
    guarded on the first alone: when the `fee` append failed (a Mongo blip, a step-down) the
    `release` entry existed, every later pass short-circuited on it, and the 2% sat in the
    account's Scheduled bucket forever — money the user could neither spend nor get back.

    So each half carries its own database guard (`RELEASE_REF_INDEX` · `FEE_REF_INDEX`) and it is
    idempotent PER KIND: it appends whichever half is missing and refuses a second of either.
    A partial write is repaired by the next call instead of being declared finished."""
    out: list[dict[str, Any]] = []
    halves = (
        ("release", int(groth), -int(groth), int(groth), note),
        ("fee", int(fee_groth), -int(fee_groth), 0, "2% at unlock"),
    )
    for kind, amount, d_sched, d_sent, why in halves:
        if await find_entry(kind, ref):
            continue
        try:
            out.append(await _append(account_id, asset, kind, amount, 0, d_sched, d_sent, ref, why))
        except DuplicateKeyError:
            continue  # another writer got there first; the entry exists, which is what matters
    return out


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
    a debit that landed, and only once."""
    if await find_entry("cancel", ref):
        raise AlreadyRefunded(f"{ref} was already refunded")
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
