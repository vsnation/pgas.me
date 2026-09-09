"""POST /v1/deposits — the user signed the DLN order transaction; register its hash against the
armed quote and let the workers follow it (order-ids → status → the pipe's lock log → credit).
GET /v1/deposits/{id} — one deposit of the signed-in account."""

from __future__ import annotations

import re
import secrets
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, tg
from ..db import db
from .account import public_deposit

router = APIRouter(prefix="/v1/deposits", tags=["deposits"])

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class DepositIn(BaseModel):
    quote_id: str = Field(min_length=8, max_length=64)
    src_tx_hash: str = Field(min_length=66, max_length=66)


@router.post("")
async def create(body: DepositIn, acct=auth.Account):
    if not HASH_RE.match(body.src_tx_hash):
        raise HTTPException(400, "src_tx_hash must be a 0x-prefixed 32-byte hex hash")
    tx_hash = body.src_tx_hash.lower()
    q = await db().quotes.find_one({"_id": body.quote_id})
    if not q or q.get("account_id") != acct["account_id"]:
        raise HTTPException(404, "unknown quote")
    now = time.time()
    if now > float(q.get("expires_at") or 0):
        raise HTTPException(409, "quote expired — request a new one")
    if not q.get("armed"):
        raise HTTPException(
            409,
            "that quote was an estimate only (ingress not armed) — no order transaction was issued for it",
        )
    existing = await db().deposits.find_one({"src_tx_hash": tx_hash})
    if existing:
        if existing.get("account_id") != acct["account_id"]:
            raise HTTPException(409, "that transaction is already registered")
        return {"deposit_id": existing["_id"], "status": existing["status"]}
    deposit_id = secrets.token_hex(12)
    doc = {
        "_id": deposit_id,
        "account_id": acct["account_id"],
        "asset": q["asset"],
        "status": "submitted",
        "src": {
            "chain_id": q["src"]["chain_id"],
            "token": q["src"]["token"],
            "amount": q["src"]["amount"],
        },
        "quote_id": q["_id"],
        "src_tx_hash": tx_hash,
        "order_id": q.get("order_id"),
        "eth": {"value_units": q["value_units"], "relayer_fee_units": q["relayer_fee_units"]},
        "value_groth": int(q["value_groth"]),
        "metadata": q.get("metadata"),
        "pubkey": q.get("pubkey"),
        "created_at": now,
        "updated_at": now,
    }
    await db().deposits.insert_one(doc)
    await tg.queue(
        "deposit_submitted",
        f"Deposit submitted: {q['asset']} ≈ {int(q['value_groth']) / 1e8:.6f}",
        deposit_id=deposit_id,
    )
    return {"deposit_id": deposit_id, "status": "submitted"}


@router.get("/{deposit_id}")
async def get_one(deposit_id: str, acct=auth.Account):
    d = await db().deposits.find_one({"_id": deposit_id, "account_id": acct["account_id"]})
    if not d:
        raise HTTPException(404, "unknown deposit")
    return public_deposit(d)
