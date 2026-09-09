"""Destination wallets — where payouts may go.

A wallet joins the account by proving control: it signs an account-bound, nonce-bound message
off-chain (EIP-191 personal_sign; free, works with zero gas). The server recovers the signer,
checks it is the claimed address, stores {address, kind, verified_at} and DISCARDS the
signature. A wallet generated in the browser registers the same way (the page signs with the
fresh key right after creating it), so the server treats both identically.

Removal has two doors onto ONE implementation: `POST /v1/destinations/remove {address}` is the
documented one (a body is not written to an access log next to the caller's IP);
`DELETE /v1/destinations/{address}` stays for compatibility with clients already shipped.
"""

from __future__ import annotations

import time

from eth_account import Account as EthAccount
from eth_account.messages import encode_defunct
from eth_utils import is_address, to_checksum_address
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth
from ..db import db

router = APIRouter(prefix="/v1/destinations", tags=["destinations"])

KINDS = {"proven", "generated"}


def proof_message(account_id: str, address: str, nonce: str, issued: str) -> str:
    return (
        "Pgas.me destination\n"
        f"account: {account_id}\n"
        f"address: {address}\n"
        f"nonce: {nonce}\n"
        f"issued: {issued}"
    )


class AddIn(BaseModel):
    address: str
    kind: str
    nonce: str = Field(min_length=8, max_length=64)
    issued: str = Field(min_length=10, max_length=40)
    signature: str = Field(min_length=130, max_length=260)
    label: str = Field(default="", max_length=64)


class RemoveIn(BaseModel):
    address: str = Field(min_length=40, max_length=64)


@router.get("/nonce")
async def nonce(request: Request, acct=auth.Account):
    n = await auth.new_nonce("dest_nonces", ip=auth.client_ip(request))
    return {"nonce": n, "template": proof_message(acct["account_id"], "<address>", n, "<issued>")}


@router.get("")
async def list_destinations(acct=auth.Account):
    cur = db().destinations.find(
        {"account_id": acct["account_id"], "removed_at": {"$exists": False}},
        {"_id": 0, "account_id": 0},
    )
    rows = await cur.to_list(length=200)
    rows.sort(key=lambda r: r.get("created_at", 0))
    return {"destinations": rows}


@router.post("")
async def add_destination(body: AddIn, acct=auth.Account):
    if body.kind not in KINDS:
        raise HTTPException(400, "kind must be proven or generated")
    if not is_address(body.address):
        raise HTTPException(400, "not an EVM address")
    address = to_checksum_address(body.address)
    if not await auth.consume_nonce(body.nonce, "dest_nonces"):
        raise HTTPException(400, "unknown or expired nonce — request a new one")
    msg = proof_message(acct["account_id"], address, body.nonce, body.issued)
    try:
        signer = EthAccount.recover_message(encode_defunct(text=msg), signature=body.signature)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"signature unreadable: {type(e).__name__}") from e
    if signer.lower() != address.lower():
        raise HTTPException(401, "the signature was not made by that address")
    now = time.time()
    await db().destinations.update_one(
        {"account_id": acct["account_id"], "address": address},
        {
            "$set": {"kind": body.kind, "verified_at": now, "label": body.label},
            "$setOnInsert": {"created_at": now},
            "$unset": {"removed_at": ""},
        },
        upsert=True,
    )
    return {"address": address, "kind": body.kind, "verified_at": now, "label": body.label}


async def _remove(address: str, acct: dict) -> dict:
    """The one implementation both removal routes call."""
    if not is_address(address):
        raise HTTPException(400, "not an EVM address")
    address = to_checksum_address(address)
    pending = await db().payout_requests.count_documents(
        {
            "account_id": acct["account_id"],
            "W": address,
            "status": {"$in": ["scheduled", "bridging"]},
        }
    )
    if pending:
        raise HTTPException(409, "a scheduled payout still targets this wallet")
    if address.lower() == acct["address"].lower():
        raise HTTPException(409, "the signed-in wallet cannot be removed")
    res = await db().destinations.update_one(
        {"account_id": acct["account_id"], "address": address},
        {"$set": {"removed_at": time.time()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "not a destination of this account")
    return {"removed": address}


@router.post("/remove")
async def remove_destination(body: RemoveIn, acct=auth.Account):
    """Documented removal: the address travels in the body, not in the URL."""
    return await _remove(body.address, acct)


@router.delete("/{address}")
async def remove_destination_by_path(address: str, acct=auth.Account):
    """Compatibility with clients that already ship the DELETE call. Same implementation."""
    return await _remove(address, acct)
