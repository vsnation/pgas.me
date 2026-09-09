"""Sign-In with Ethereum (EIP-4361) and session tokens.

The account IS the connected wallet: account_id = keccak(lowercase address ‖ salt), so the
database key is not the raw address. A session is a short-lived HS256 JWT.

Nonces carry a tz-aware datetime `at`: the TTL index only fires on a BSON date (a float is
just a number to it), and consumption re-checks the age itself, because "the sweeper has not
run yet" must never mean "this nonce is still good".

Per-IP caps live here too — /v1/siwe/* is unauthenticated and every verify writes permanent
account and destination rows. nginx limits are an additive layer, not a substitute.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from eth_utils import keccak, to_checksum_address
from fastapi import Depends, HTTPException, Request
from pymongo import ReturnDocument
from siwe import SiweMessage

from .config import settings
from .db import NONCE_TTL_MULT, db


def account_id_for(address: str) -> str:
    return keccak(text=address.lower() + "|" + settings.account_salt).hex()


def nonce_max_age_s(collection: str = "siwe_nonces") -> int:
    """The lifetime the TTL index gives this collection — one number, two readers."""
    return settings.nonce_ttl_s * NONCE_TTL_MULT.get(collection, 1)


def client_ip(request: Request) -> str:
    """The caller's address. uvicorn resolves X-Forwarded-For from the trusted local nginx
    (which overwrites the header), so this is the real client, not the proxy."""
    return (request.client.host if request.client else "") or "unknown"


def too_many(retry_after_s: int, detail: str) -> HTTPException:
    return HTTPException(429, detail, headers={"Retry-After": str(max(1, int(retry_after_s)))})


async def rate_hit(bucket: str, key: str, limit: int, window_s: int) -> int:
    """Count one hit in a fixed window. Returns 0 when allowed, else Retry-After seconds.

    One conditional upsert per call — no read-then-write race, and the counter is shared by
    every uvicorn worker instead of living in one process's memory.
    """
    now = time.time()
    window = int(now // window_s)
    doc = await db().rate_limits.find_one_and_update(
        {"_id": f"{bucket}:{key}:{window}"},
        {"$inc": {"n": 1}, "$setOnInsert": {"at": datetime.now(UTC), "bucket": bucket}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if int((doc or {}).get("n", 1)) <= limit:
        return 0
    return int((window + 1) * window_s - now) + 1


async def rate_guard(bucket: str, key: str, limit: int, window_s: int, detail: str) -> None:
    retry = await rate_hit(bucket, key, limit, window_s)
    if retry:
        raise too_many(retry, detail)


async def new_nonce(collection: str = "siwe_nonces", ip: str = "") -> str:
    # EIP-4361 nonces: alphanumeric, ≥ 8 chars. 16 random bytes → 32 hex chars.
    nonce = secrets.token_hex(16)
    await db()[collection].insert_one({"_id": nonce, "at": datetime.now(UTC), "ip": ip})
    return nonce


async def outstanding_nonces(ip: str, collection: str = "siwe_nonces") -> int:
    """Un-consumed, un-expired nonces this IP is holding."""
    cutoff = datetime.now(UTC) - timedelta(seconds=nonce_max_age_s(collection))
    return await db()[collection].count_documents({"ip": ip, "at": {"$gte": cutoff}})


async def new_nonce_for_ip(ip: str, collection: str = "siwe_nonces", limit: int = 0) -> str:
    """A fresh nonce, refused with 429 once this IP holds `limit` outstanding ones."""
    if limit and await outstanding_nonces(ip, collection) >= limit:
        raise too_many(
            nonce_max_age_s(collection),
            "too many sign-in nonces from this address — use one or wait for them to expire",
        )
    return await new_nonce(collection, ip=ip)


async def consume_nonce(nonce: str, collection: str = "siwe_nonces") -> bool:
    """Delete-and-check, age included: an expired nonce is refused even if the TTL sweeper
    (which runs about once a minute, and never at all on a float `at`) has not reached it."""
    cutoff = datetime.now(UTC) - timedelta(seconds=nonce_max_age_s(collection))
    res = await db()[collection].delete_one({"_id": nonce, "at": {"$gte": cutoff}})
    if res.deleted_count == 1:
        return True
    # An expired (or float-`at`, pre-upgrade) row is still spent — never leave it reusable.
    await db()[collection].delete_one({"_id": nonce})
    return False


async def verify_siwe(message: str, signature: str) -> dict[str, Any]:
    """Parse + verify an EIP-4361 message. Returns {address, chain_id, nonce, domain}."""
    try:
        msg = SiweMessage.from_message(message=message)
    except Exception as e:  # noqa: BLE001 — siwe raises several parse errors
        raise HTTPException(400, f"malformed SIWE message: {type(e).__name__}") from e
    if msg.domain not in settings.siwe_domain_set:
        raise HTTPException(400, f"domain {msg.domain!r} is not this site")
    if not await consume_nonce(msg.nonce):
        raise HTTPException(400, "unknown or expired nonce — request a new one")
    try:
        msg.verify(signature, domain=msg.domain, nonce=msg.nonce)
    except Exception as e:  # noqa: BLE001 — VerificationError subclasses
        raise HTTPException(401, f"signature rejected: {type(e).__name__}") from e
    address = to_checksum_address(msg.address)
    return {
        "address": address,
        "chain_id": int(msg.chain_id),
        "nonce": msg.nonce,
        "domain": msg.domain,
    }


def issue_token(account_id: str, address: str) -> str:
    now = int(time.time())
    payload = {"sub": account_id, "addr": address, "iat": now, "exp": now + settings.session_ttl_s}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(401, "session expired — sign in again") from e
    except jwt.PyJWTError as e:
        raise HTTPException(401, "invalid session") from e


async def current_account(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "sign in first")
    claims = decode_token(auth[7:].strip())
    acct = await db().accounts.find_one({"_id": claims["sub"]})
    if not acct:
        raise HTTPException(401, "unknown account — sign in again")
    return {"account_id": claims["sub"], "address": claims["addr"], "doc": acct}


Account = Depends(current_account)
