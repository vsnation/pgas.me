"""GET /v1/account — the balances the user sees (one per asset), with history.

⛔ THIS ROUTE IS THE ONE THE USER LOOKS AT WHEN SOMETHING IS WRONG, so it renders whatever is
in the database — including rows written before a guard existed. A single legacy
`payout_requests` row carrying `deliver_at: NaN` used to make this endpoint 500 for that account
FOREVER (Starlette renders JSON with `allow_nan=False`, and a NaN anywhere in the payload raises
at encode time), which hid the very order the user needed to find in order to cancel it. A value
that cannot be serialised is therefore blanked to `null` on the way out and NAMED on its row
(`unreadable_fields`), and every derived number is read tolerantly — a poisoned row degrades one
field of one row, never the whole account.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter

from .. import auth, ledger, xchain
from ..assets import ASSETS
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/account", tags=["account"])

# money that is on its way into the balance (ordered, locked or confirming) — a fallback is NOT
# pending: the funds went to the user's own wallet and nothing of ours will move them.
PENDING_STATES = ("submitted", "order_seen", "locked", "confirming")


def is_unreadable(v: Any) -> bool:
    """True for a number that is not a number: NaN, ±Infinity. `bool` is an `int`, never a float,
    so it is not a candidate; ints (and Mongo's int64) are always finite."""
    return isinstance(v, float) and not math.isfinite(v)


def finite(value: Any) -> Any:
    """The payload with every non-finite number replaced by `null`, recursively.

    ONE implementation for the whole response (law 9), applied once at the end: a second scrub
    living somewhere else would eventually disagree with this one about what "unreadable" means.
    It is the LAST thing that happens to the answer, so nothing can add a NaN after it."""
    if is_unreadable(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite(v) for v in value]
    return value


def tolerant_int(v: Any) -> int:
    """A groth total read from a row that may predate the guards. `int(float('nan'))` RAISES —
    an aggregate over one poisoned `value_groth` would otherwise take the whole account with it."""
    if v is None or is_unreadable(v):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def public_deposit(d: dict) -> dict:
    d = dict(d)
    d.pop("account_id", None)
    d.pop("pubkey", None)
    # ONE reader for the mode (xchain.norm_mode): rows written before the same-chain modes
    # existed carry none, and rows written before the rename carry the router's own name.
    d["mode"] = xchain.norm_mode(d.get("mode"))
    return d


def public_request(r: dict) -> dict:
    """One payout order as the account shows it — with the fields that cannot be serialised
    named, so a blank `deliver_at` says WHY it is blank instead of reading as "asap"."""
    r = dict(r)
    bad = sorted(k for k, v in r.items() if is_unreadable(v))
    if bad:  # `finite()` blanks the values themselves, once, on the whole payload
        r["unreadable_fields"] = bad
    return r


@router.get("")
async def account(acct=auth.Account):
    aid = acct["account_id"]
    bal = await ledger.balances(aid)
    deposits = (
        await db().deposits.find({"account_id": aid}).sort("created_at", -1).limit(30).to_list(30)
    )
    pending_cur = db().deposits.aggregate(
        [
            {"$match": {"account_id": aid, "status": {"$in": list(PENDING_STATES)}}},
            {"$group": {"_id": "$asset", "groth": {"$sum": "$value_groth"}}},
        ]
    )
    pending = {
        r["_id"]: tolerant_int(r.get("groth"))
        for r in await pending_cur.to_list(length=len(ASSETS) + 8)
    }
    balances = {k: {**bal[k], "pending": pending.get(k, 0)} for k in ASSETS}
    requests = (
        await db()
        .payout_requests.find({"account_id": aid}, {"account_id": 0})
        .sort("created_at", -1)
        .limit(60)
        .to_list(60)
    )
    dests = await db().destinations.count_documents(
        {"account_id": aid, "removed_at": {"$exists": False}}
    )
    return finite(
        {
            "address": acct["address"],
            "account_id": aid,
            "balances": balances,
            "fee_bps": settings.fee_bps,
            "denominations": settings.denominations,
            "min_payout_groth": settings.min_payout_groth,
            "modes": {
                "direct": settings.payout_direct_enabled,
                "instant": settings.payout_instant_enabled,
            },
            "ingress": {"armed": settings.ingress_ready, "near": settings.ingress_near_enabled},
            "deposits": [public_deposit(d) for d in deposits],
            "requests": [public_request(r) for r in requests],
            "destinations": dests,
            "history": await ledger.history(aid, 100),
        }
    )
