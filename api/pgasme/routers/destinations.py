"""Destination wallets — a PASSIVE address book, nothing more (2026-09-09).

Payouts no longer go to "registered" destinations: a withdrawal names any address the user
types and `routers/withdrawals.py` validates it on its own (EIP-55 checksum + it carries no
contract code). So there is nothing left to prove here — the signed-proof POST and its nonce
route are GONE, and with them the only reason this collection could refuse an address.

What remains is the list the account page shows. The signed-in wallet is added to it at SIWE
(`routers/siwe.py`, kind `connected`); rows written by the old proof flow keep their `proven` /
`generated` kind and are still listed and still removable.

Removal has two doors onto ONE implementation: `POST /v1/destinations/remove {address}` is the
documented one (a body is not written to an access log next to the caller's IP);
`DELETE /v1/destinations/{address}` stays for compatibility with clients already shipped.
"""

from __future__ import annotations

import time

from eth_utils import is_address, to_checksum_address
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth
from ..db import db

router = APIRouter(prefix="/v1/destinations", tags=["destinations"])


class RemoveIn(BaseModel):
    address: str = Field(min_length=40, max_length=64)


@router.get("")
async def list_destinations(acct=auth.Account):
    cur = db().destinations.find(
        {"account_id": acct["account_id"], "removed_at": {"$exists": False}},
        {"_id": 0, "account_id": 0},
    )
    rows = await cur.to_list(length=200)
    rows.sort(key=lambda r: r.get("created_at", 0))
    return {"destinations": rows}


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
