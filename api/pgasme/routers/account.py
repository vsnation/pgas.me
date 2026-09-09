"""GET /v1/account — the balances the user sees (one per asset), with history."""

from __future__ import annotations

from fastapi import APIRouter

from .. import auth, ledger
from ..assets import ASSETS
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/account", tags=["account"])

# money that is on its way into the balance (ordered, locked or confirming) — a fallback is NOT
# pending: the funds went to the user's own wallet and nothing of ours will move them.
PENDING_STATES = ("submitted", "order_seen", "locked", "confirming")


def public_deposit(d: dict) -> dict:
    d = dict(d)
    d.pop("account_id", None)
    d.pop("pubkey", None)
    return d


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
        r["_id"]: int(r["groth"] or 0) for r in await pending_cur.to_list(length=len(ASSETS) + 8)
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
    return {
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
        "requests": requests,
        "destinations": dests,
        "history": await ledger.history(aid, 100),
    }
