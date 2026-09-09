"""GET /v1/siwe/nonce · POST /v1/siwe/verify — the account is the wallet."""

from __future__ import annotations

import time

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import auth
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/siwe", tags=["siwe"])


class VerifyIn(BaseModel):
    message: str = Field(min_length=40, max_length=4000)
    signature: str = Field(min_length=130, max_length=260)


@router.get("/nonce")
async def nonce():
    n = await auth.new_nonce()
    return {
        "nonce": n,
        "statement": settings.siwe_statement,
        "domains": sorted(settings.siwe_domain_set),
    }


@router.post("/verify")
async def verify(body: VerifyIn):
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
