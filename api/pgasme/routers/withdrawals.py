"""POST /v1/withdrawals — schedule payouts to registered destinations; POST …/{id}/cancel.

Every rule of the contract, in order, failing closed:
  registered destinations only · amount ≥ min_payout_groth · instant → denomination multiples ·
  Available ≥ Σ amount × (1 + fee_bps/10000) · window_s ∈ [0, 30 d] · a disabled mode → 409.
One `payout_requests` row per item with its own release_at = now + U(0, window_s) (drawn
independently per wallet so a multi-wallet withdrawal never lands as one burst), and ONE ledger
`schedule` entry per item for amount + fee. Execution is a worker concern and is dark here.

TWO INVARIANTS THIS FILE OWNS:

  * "Available ≥ need" read and then acted on is not a check — two requests arriving together
    both read the same Available and both pass it. The admission is ONE conditional update on a
    per-(account, asset) reservation document: `pending` may only grow while it stays within the
    ledger's Available, so the second request finds no room and is refused. Entries are written
    only when that update matched, the ledger is re-read afterwards, and a balance that went
    negative is rolled back with offsetting entries (never by editing history).
  * A refund needs the debit it reverses. The `schedule` entry is appended BEFORE the request
    row exists, so a row can never exist without its debit; cancel refunds exactly the groth of
    THAT entry (not a recomputed amount + fee) and only once.
"""

from __future__ import annotations

import random
import secrets
import time
from typing import Any

from eth_utils import is_address, to_checksum_address
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, ledger, tg, workers
from ..assets import ASSETS
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/withdrawals", tags=["withdrawals"])

MODES = ("direct", "instant")
# a reservation left behind by a request that died mid-flight; far longer than any request lives
RESERVATION_STALE_S = 300.0
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


def _res_id(account_id: str, asset: str) -> str:
    return f"{account_id}:{asset}"


async def reserve(account_id: str, asset: str, need: int) -> bool:
    """Atomically admit `need` groth against Available. ONE conditional update decides; a second
    concurrent request finds `pending` too high and is refused. Returns False when there is no
    room. The reservation is released by `release()` once the entries have landed."""
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
    await d.reservations.update_one(
        {"_id": key, "pending": {"$gt": 0}, "at": {"$lt": now - RESERVATION_STALE_S}},
        {"$set": {"pending": 0, "at": now, "stale_cleared_at": now}},
    )
    available = (await ledger.balance(account_id, asset))["available"]
    claimed = await d.reservations.find_one_and_update(
        {"_id": key, "pending": {"$lte": available - need}},
        {"$inc": {"pending": need}, "$set": {"at": now}},
    )
    return claimed is not None


async def release(account_id: str, asset: str, need: int) -> None:
    """Give the reservation back: the entries have landed (Available already reflects them) or
    nothing was written at all."""
    await db().reservations.update_one(
        {"_id": _res_id(account_id, asset)},
        {"$inc": {"pending": -need}, "$set": {"at": time.time()}},
    )


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


async def _write_items(
    aid: str, asset: str, body: WithdrawIn, items: list[dict[str, Any]]
) -> list[str]:
    """The debit FIRST, then the row it belongs to: a request row can never exist without the
    ledger entry that paid for it, so cancel always has its evidence."""
    now = time.time()
    ids: list[str] = []
    for i in items:
        rid = secrets.token_hex(12)
        total = i["amount"] + i["fee"]
        await ledger.schedule(aid, asset, total, rid, f"{body.mode} payout scheduled")
        row = {
            "_id": rid,
            "account_id": aid,
            "asset": asset,
            "mode": body.mode,
            "W": i["W"],
            "amount_groth": i["amount"],
            "fee_groth": i["fee"],
            "window_s": body.window_s,
            "release_at": (now + random.uniform(0, body.window_s)) if body.window_s > 0 else now,
            "status": "scheduled",
            "dest_chain": settings.eth_chain_id,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await db().payout_requests.insert_one(row)
        except Exception:  # noqa: BLE001 — the debit landed and its row did not: give it back
            await ledger.cancel(
                aid,
                asset,
                total,
                rid,
                "the payout request row could not be written; the debit is reversed",
                refund_of=f"schedule:{rid}",
            )
            raise
        ids.append(rid)
    return ids


async def _roll_back(aid: str, asset: str, ids: list[str]) -> None:
    """Undo a withdrawal that must not stand — with offsetting entries, never by deleting."""
    for rid in ids:
        row = await db().payout_requests.find_one_and_update(
            {"_id": rid, "status": "scheduled"},
            {"$set": {"status": "cancelled", "cancelled_at": time.time(), "rolled_back": True}},
        )
        entry = await ledger.find_entry("schedule", rid)
        if row and entry:
            try:
                await ledger.cancel(
                    aid,
                    asset,
                    int(entry["groth"]),
                    rid,
                    "rolled back: the balance would have gone negative",
                    refund_of=f"schedule:{rid}",
                )
            except ledger.AlreadyRefunded:
                pass
    await tg.alert(
        "withdrawal_rolled_back",
        "ROLLED BACK: a withdrawal was reversed because the balance would have gone negative — "
        "check the ledger for this account",
        request_ids=ids,
    )


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
    available = (await ledger.balance(aid, asset))["available"]
    if not await reserve(aid, asset, need):
        raise HTTPException(
            409,
            f"insufficient {asset} balance: need {need} groth (amount + {settings.fee_bps / 100:g}% fee), "
            f"available {available} (another withdrawal of yours may be in flight)",
        )
    try:
        ids = await _write_items(aid, asset, body, items)
    finally:
        await release(aid, asset, need)
    after = (await ledger.balance(aid, asset))["available"]
    if after < 0:  # belt: nothing may leave the account overdrawn, ever
        await _roll_back(aid, asset, ids)
        raise HTTPException(
            409, f"insufficient {asset} balance — the withdrawal was rolled back and nothing moved"
        )
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
        raise HTTPException(
            409, f"request is {existing['status']} — only a scheduled request can be cancelled"
        )
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
    await tg.queue(
        "withdrawal_cancelled",
        f"Withdrawal cancelled: {row['asset']} {tg.fmt_groth(total)} back to Available",
        request_id=request_id,
    )
    return {"cancelled": request_id, "refunded_groth": total}
