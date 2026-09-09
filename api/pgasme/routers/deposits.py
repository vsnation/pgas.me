"""POST /v1/deposits — the user signed the transaction; register its hash against the armed
quote and let the workers follow it. A `dln` quote registers the DLN order tx (order-ids →
status → the pipe's lock log → credit); a `direct` quote registers the pipe call itself, which
has no order id at all and is attributed by that hash in the receipt the scanner reads. A `swap`
quote is not a deposit: its transaction lands in the user's own wallet.
GET /v1/deposits/{id} — one deposit of the signed-in account.

THE HASH IS NOT A CLAIM. A transaction hash is public the moment it is broadcast, so
"whoever posts it first owns it" would let a stranger bind someone else's fill to their own
account (and lock the person who signed it out with a 409 forever). The hash is RESOLVED here
before a row exists:

  mode "dln"     deBridge's own index must list THIS quote's order id among the transaction's
                 orders. A transaction whose orders are someone else's is refused outright; a
                 transaction DLN has not indexed yet is accepted unverified and re-checked by
                 workers._step_submitted, which fails it on a mismatch and never adopts a
                 foreign id.
  mode "direct"  the chain is asked: `from` must be the wallet that asked for the quote, `to`
                 the asset's pipe, and the calldata this quote's own sendFunds. A pipe call
                 from another wallet is not registered at all — its lock lands in
                 unattributed_locks and pages the operator, which is the manual path.

An answer nobody could read is never treated as a verdict: an unreachable RPC / DLN gives a
retryable refusal or an unverified row, never an accepted claim and never a rejection.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, dln, ethpipe, tg, workers
from ..assets import get_asset
from ..db import db
from .account import public_deposit

router = APIRouter(prefix="/v1/deposits", tags=["deposits"])

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ORDER_IDS_TIMEOUT_S = 8.0  # this sits in the request path: a slow DLN must not hang the user
NOT_VISIBLE_YET = (
    "that transaction is not visible on Ethereum yet — wait for it to be broadcast and try again"
)


class DepositIn(BaseModel):
    quote_id: str = Field(min_length=8, max_length=64)
    src_tx_hash: str = Field(min_length=66, max_length=66)


async def _dln_order_ids(tx_hash: str) -> list[str] | None:
    """The DLN order ids of this transaction, or None when DLN could not answer (not indexed
    yet, or unreachable). None is "I do not know", never "there are none"."""
    try:
        return await dln.order_ids_by_tx(tx_hash, timeout=ORDER_IDS_TIMEOUT_S)
    except dln.DlnError:
        return None


async def resolve_dln(q: dict[str, Any], tx_hash: str) -> bool:
    """True when deBridge already vouches for the pair (verified), False when it cannot say yet.
    Raises 400 when the transaction carries orders and none of them is this quote's."""
    ids = await _dln_order_ids(tx_hash)
    if not ids:
        return False  # not indexed yet — workers._step_submitted re-checks and fails a mismatch
    want = str(q.get("order_id") or "").lower()
    have = [str(i).lower() for i in ids]
    if want and want in have:
        return True
    await tg.queue(
        "deposit_mismatch",
        "REJECTED at registration: a transaction was offered for a quote whose deBridge order "
        "it does not carry (hash attribution attempt)",
        quote_id=q["_id"],
    )
    raise HTTPException(
        400,
        "that transaction does not carry this quote's deBridge order — register the transaction "
        "you signed for this quote",
    )


async def resolve_direct(q: dict[str, Any], tx_hash: str) -> bool:
    """The pipe call must be THIS user's: same sender, our pipe, this quote's calldata."""
    asset = get_asset(q["asset"])
    rpc = workers.get_rpc()
    try:
        tx = await rpc.transaction(tx_hash)
    except ethpipe.RpcError as e:
        raise HTTPException(
            503, f"could not read that transaction from any Ethereum endpoint ({e}) — try again"
        ) from e
    if not tx:
        raise HTTPException(409, NOT_VISIBLE_YET)
    sender = (tx.get("from") or "").lower()
    to = (tx.get("to") or "").lower()
    if sender != (q.get("address") or "").lower():
        raise HTTPException(
            400,
            "that transaction was not sent from the wallet this quote was issued to — the pipe "
            "call must come from your signed-in wallet",
        )
    if to != asset.pipe.lower():
        raise HTTPException(
            400, f"that transaction is not a call to the {asset.key} pipe of this quote"
        )
    data = (tx.get("input") or tx.get("data") or "").lower()
    want = str(q.get("hook_calldata") or "").lower()
    if want and data == want:
        return True
    try:
        call = ethpipe.decode_send_funds(data)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, "that transaction is not a sendFunds call on the pipe") from e
    if call["pubkey"].lower() != str(q.get("pubkey") or "").lower():
        raise HTTPException(400, "that pipe call locks to a different Beam pubkey than this quote")
    if str(call["value"]) != str(q["value_units"]):
        raise HTTPException(
            400,
            "that pipe call locks a different amount than this quote — quote again for the "
            "amount you actually sent, then register it",
        )
    return True


@router.post("")
async def create(body: DepositIn, acct=auth.Account):
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    if not HASH_RE.match(body.src_tx_hash):
        raise HTTPException(400, "src_tx_hash must be a 0x-prefixed 32-byte hex hash")
    tx_hash = body.src_tx_hash.lower()
    q = await db().quotes.find_one({"_id": body.quote_id})
    if not q or q.get("account_id") != acct["account_id"]:
        raise HTTPException(404, "unknown quote")
    now = time.time()
    if now > float(q.get("expires_at") or 0):
        raise HTTPException(409, "quote expired — request a new one")
    mode = q.get("mode") or "dln"
    if mode == "swap":
        raise HTTPException(
            400, "a swap is not a deposit — quote again with the target token after it lands"
        )
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
    # resolve the hash BEFORE a row exists: the quote must own this transaction
    verified = await (resolve_direct if mode == "direct" else resolve_dln)(q, tx_hash)
    deposit_id = secrets.token_hex(12)
    doc = {
        "_id": deposit_id,
        "account_id": acct["account_id"],
        "address": q.get("address"),  # the sender the scanner checks a direct receipt against
        "asset": q["asset"],
        "mode": mode,
        "status": "submitted",
        "verified": verified,
        "src": {
            "chain_id": q["src"]["chain_id"],
            "token": q["src"]["token"],
            "amount": q["src"]["amount"],
        },
        "quote_id": q["_id"],
        "src_tx_hash": tx_hash,
        "order_id": q.get("order_id"),  # the QUOTE's own id — never the transaction's first id
        "eth": {"value_units": q["value_units"], "relayer_fee_units": q["relayer_fee_units"]},
        "value_groth": int(q["value_groth"]),
        "metadata": q.get("metadata"),
        "pubkey": q.get("pubkey"),
        "created_at": now,
        "updated_at": now,
    }
    try:
        await db().deposits.insert_one(doc)
    except Exception as e:  # noqa: BLE001 — the unique index is the one that decides
        again = await db().deposits.find_one({"src_tx_hash": tx_hash})
        if again and again.get("account_id") == acct["account_id"]:
            return {"deposit_id": again["_id"], "status": again["status"]}
        raise HTTPException(409, "that transaction is already registered") from e
    await tg.queue(
        "deposit_submitted",
        f"Deposit submitted ({mode}{'' if verified else ', unverified'}): {q['asset']} "
        f"≈ {tg.fmt_groth(int(q['value_groth']))}",
        deposit_id=deposit_id,
    )
    return {"deposit_id": deposit_id, "status": "submitted"}


@router.get("/{deposit_id}")
async def get_one(deposit_id: str, acct=auth.Account):
    d = await db().deposits.find_one({"_id": deposit_id, "account_id": acct["account_id"]})
    if not d:
        raise HTTPException(404, "unknown deposit")
    return public_deposit(d)
