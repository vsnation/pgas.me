"""GET /v1/siwe/nonce · POST /v1/siwe/verify — the account is the wallet.

Both routes are unauthenticated and /verify writes permanent rows (an account and its
connected destination) for any wallet that can sign — a free key is free. Two app-side caps
per IP keep that from becoming unbounded storage; nginx adds its own layer above.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from .. import auth
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/siwe", tags=["siwe"])


class VerifyIn(BaseModel):
    message: str = Field(min_length=40, max_length=4000)
    signature: str = Field(min_length=130, max_length=260)


@router.get("/nonce")
async def nonce(request: Request):
    n = await auth.new_nonce_for_ip(auth.client_ip(request), limit=settings.siwe_nonce_ip_limit)
    return {
        "nonce": n,
        "statement": settings.siwe_statement,
        "domains": sorted(settings.siwe_domain_set),
    }


@router.post("/verify")
async def verify(body: VerifyIn, request: Request):
    await auth.rate_guard(
        "siwe_verify",
        auth.client_ip(request),
        settings.siwe_verify_ip_limit,
        settings.rate_window_s,
        "too many sign-in attempts from this address — try again shortly",
    )
    res = await auth.verify_siwe(body.message, body.signature)
    account_id = auth.account_id_for(res["address"])
    now = time.time()
    await db().accounts.update_one(
        {"_id": account_id},
        {
            "$setOnInsert": {
                "created_at": now,
                "quota": {"window_start": now, "used_groth": 0, "used_count": 0},
            },
            "$set": {"last_login_at": now, "last_chain_id": res["chain_id"]},
        },
        upsert=True,
    )
    # the connected wallet is always a destination
    await db().destinations.update_one(
        {"account_id": account_id, "address": res["address"]},
        {
            "$setOnInsert": {"kind": "connected", "verified_at": now, "created_at": now},
            "$unset": {"removed_at": ""},
        },
        upsert=True,
    )
    token = auth.issue_token(account_id, res["address"])
    return {
        "token": token,
        "account_id": account_id,
        "address": res["address"],
        "expires_in": settings.session_ttl_s,
    }
