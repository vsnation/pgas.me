"""Sign-In with Ethereum (EIP-4361) and session tokens.

The account IS the connected wallet: account_id = keccak(lowercase address ‖ salt), so the
database key is not the raw address. A session is a short-lived HS256 JWT.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt
from eth_utils import keccak, to_checksum_address
from fastapi import Depends, HTTPException, Request
from siwe import SiweMessage

from .config import settings
from .db import db


def account_id_for(address: str) -> str:
    return keccak(text=address.lower() + "|" + settings.account_salt).hex()


async def new_nonce(collection: str = "siwe_nonces") -> str:
    # EIP-4361 nonces: alphanumeric, ≥ 8 chars. 16 random bytes → 32 hex chars.
    nonce = secrets.token_hex(16)
    await db()[collection].insert_one({"_id": nonce, "at": time.time()})
    return nonce


async def consume_nonce(nonce: str, collection: str = "siwe_nonces") -> bool:
    res = await db()[collection].delete_one({"_id": nonce})
    return res.deleted_count == 1


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
