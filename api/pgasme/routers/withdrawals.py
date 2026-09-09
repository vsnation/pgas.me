"""POST /v1/withdrawals — schedule payouts to registered destinations; POST …/{id}/cancel.

Every rule of the contract, in order, failing closed:
  registered destinations only · amount ≥ min_payout_groth · instant → denomination multiples ·
  Available ≥ Σ amount × (1 + fee_bps/10000) · window_s ∈ [0, 30 d] · a disabled mode → 409.
One `payout_requests` row per item with its own release_at = now + U(0, window_s) (drawn
independently per wallet so a multi-wallet withdrawal never lands as one burst), and ONE ledger
`schedule` entry per item for amount + fee. Execution is a worker concern and is dark here.
"""

from __future__ import annotations

import random
import secrets
import time
from typing import Any

from eth_utils import is_address, to_checksum_address
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, ledger, tg
from ..assets import ASSETS
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/withdrawals", tags=["withdrawals"])

MODES = ("direct", "instant")
# time the money needs after release: direct = one bridge crossing (~66 min typical), instant = a transfer
MODE_BASE_S = {"direct": 66 * 60, "instant": 30}
MODE_TAIL_S = {"direct": 4 * 3600, "instant": 120}


class Item(BaseModel):
    W: str
    amount_groth: int = Field(gt=0)


class WithdrawIn(BaseModel):
    asset: str = "ETH"
    items: list[Item] = Field(min_length=1)
    mode: str
    window_s: int


def fee_for(amount_groth: int) -> int:
    return amount_groth * settings.fee_bps // 10000


async def deposits_24h() -> int:
    return await db().deposits.count_documents(
        {
            "created_at": {"$gte": time.time() - 86400},
            "status": {"$nin": ["failed", "expired", "fallback_pending"]},
        }
    )


def privacy_grade(n_deposits_24h: int, window_s: int) -> str:
    if n_deposits_24h < 10 or window_s < 600:
        return "weak"
    if n_deposits_24h < 50:
        return "ok"
    return "good"


@router.post("")
async def create(body: WithdrawIn, acct=auth.Account):
    aid = acct["account_id"]
    asset = body.asset.upper()
    if asset not in ASSETS:
        raise HTTPException(400, f"unknown asset {body.asset!r}")
    if body.mode not in MODES:
        raise HTTPException(400, "mode must be direct or instant")
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
    if not 0 <= body.window_s <= settings.max_window_s:
        raise HTTPException(
            400, f"window_s must be between 0 and {settings.max_window_s} (30 days)"
        )

    dests = (
        await db()
        .destinations.find({"account_id": aid, "removed_at": {"$exists": False}}, {"address": 1})
        .to_list(500)
    )
    registered = {d["address"].lower() for d in dests}
    items: list[dict[str, Any]] = []
    denoms = settings.denominations
    for it in body.items:
        if not is_address(it.W):
            raise HTTPException(400, f"{it.W!r} is not an EVM address")
        w = to_checksum_address(it.W)
        if w.lower() not in registered:
            raise HTTPException(400, f"{w} is not a registered destination of this account")
        if it.amount_groth < settings.min_payout_groth:
            raise HTTPException(
                400,
                f"each payout must be at least {settings.min_payout_groth} groth "
                f"({settings.min_payout_groth / 1e8:g} {asset})",
            )
        if body.mode == "instant" and not any(it.amount_groth % d == 0 for d in denoms):
            raise HTTPException(
                400,
                f"instant payouts must be a multiple of a denomination ({', '.join(str(d) for d in denoms)} groth)",
            )
        items.append({"W": w, "amount": it.amount_groth, "fee": fee_for(it.amount_groth)})

    need = sum(i["amount"] + i["fee"] for i in items)
    bal = await ledger.balance(aid, asset)
    if bal["available"] < need:
        raise HTTPException(
            409,
            f"insufficient {asset} balance: need {need} groth (amount + {settings.fee_bps / 100:g}% fee), "
            f"available {bal['available']}",
        )

    now = time.time()
    ids: list[str] = []
    for i in items:
        rid = secrets.token_hex(12)
        release_at = now + random.uniform(0, body.window_s) if body.window_s > 0 else now
        row = {
            "_id": rid,
            "account_id": aid,
            "asset": asset,
            "mode": body.mode,
            "W": i["W"],
            "amount_groth": i["amount"],
            "fee_groth": i["fee"],
            "window_s": body.window_s,
            "release_at": release_at,
            "status": "scheduled",
            "dest_chain": settings.eth_chain_id,
            "created_at": now,
            "updated_at": now,
        }
        await db().payout_requests.insert_one(row)
        await ledger.schedule(
            aid, asset, i["amount"] + i["fee"], rid, f"{body.mode} payout scheduled"
        )
        ids.append(rid)
    await tg.queue(
        "withdrawal_requested",
        f"Withdrawal requested: {len(ids)} × {body.mode} {asset}, "
        f"total {need / 1e8:.6f} incl. fee, window {body.window_s}s",
        request_ids=ids,
    )
    base, tail = MODE_BASE_S[body.mode], MODE_TAIL_S[body.mode]
    return {
        "request_ids": ids,
        "fee_groth": sum(i["fee"] for i in items),
        "total_debited_groth": need,
        "eta": {"min_s": base, "max_s": body.window_s + base + tail},
        "privacy_grade": privacy_grade(await deposits_24h(), body.window_s),
    }


@router.post("/{request_id}/cancel")
async def cancel(request_id: str, acct=auth.Account):
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
        raise HTTPException(
            409, f"request is {existing['status']} — only a scheduled request can be cancelled"
        )
    total = int(row["amount_groth"]) + int(row["fee_groth"])
    await ledger.cancel(
        aid, row["asset"], total, request_id, "cancelled by the user before release"
    )
    await tg.queue(
        "withdrawal_cancelled",
        f"Withdrawal cancelled: {row['asset']} {total / 1e8:.6f} back to Available",
        request_id=request_id,
    )
    return {"cancelled": request_id}
