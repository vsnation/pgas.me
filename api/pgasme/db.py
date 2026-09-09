"""Mongo access. One client per process; tests inject a mongomock client via set_client()."""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .config import settings

_client: Any = None
_db: Any = None


def set_client(client: Any, name: str = "pgasme") -> None:
    global _client, _db
    _client = client
    _db = client[name]


def db() -> AsyncIOMotorDatabase:
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(
            settings.mongo_url,
            serverSelectionTimeoutMS=1500,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
        )
        _db = _client.get_default_database("pgasme")
    return _db


async def ensure_indexes() -> None:
    d = db()
    await d.siwe_nonces.create_index("at", expireAfterSeconds=settings.nonce_ttl_s)
    await d.dest_nonces.create_index("at", expireAfterSeconds=settings.nonce_ttl_s * 2)
    await d.quotes.create_index("at", expireAfterSeconds=settings.quote_ttl_s)
    await d.quotes.create_index([("account_id", 1), ("at", -1)])
    await d.entries.create_index([("account_id", 1), ("asset", 1), ("at", 1)])
    await d.entries.create_index([("kind", 1), ("ref", 1)])
    await d.deposits.create_index([("account_id", 1), ("created_at", -1)])
    await d.deposits.create_index([("status", 1), ("updated_at", 1)])
    await d.deposits.create_index("src_tx_hash")
    await d.destinations.create_index([("account_id", 1), ("address", 1)], unique=True)
    await d.payout_requests.create_index([("account_id", 1), ("created_at", -1)])
    await d.payout_requests.create_index([("status", 1), ("release_at", 1)])
    await d.events.create_index([("notified", 1), ("at", 1)])
    await d.events.create_index("at", expireAfterSeconds=30 * 86400)
