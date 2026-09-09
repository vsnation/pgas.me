"""Mongo access. One client per process; tests inject a mongomock client via set_client().

Indexes live here and only here for the collections this module names: `ensure_indexes()` is
the single writer of that fact, it repairs an index whose options changed, and it NEVER
swallows a failure — every one is logged at ERROR and reported through /v1/health
(`indexes_ok` / `index_errors`) so a deploy can assert it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure

from .config import settings

log = logging.getLogger("pgasme.db")

_client: Any = None
_db: Any = None

# Nonce collections and how many nonce_ttl_s each one lives for. auth.py reads this map so the
# expiry the TTL index enforces and the expiry consume_nonce checks can never drift apart.
NONCE_TTL_MULT: dict[str, int] = {"siwe_nonces": 1, "dest_nonces": 2}

# Written only by ensure_indexes(); read by /v1/health.
INDEX_ERRORS: list[str] = []

# Two unique indexes are ALSO created by the modules that depend on them at worker start
# (scanner.DEPOSIT_HASH_INDEX, ledger.CREDIT_REF_INDEX). Mongo answers IndexOptionsConflict
# (85) "Index already exists with a different name" when the same spec is created twice under
# two names, so these names must stay byte-identical to theirs — an identical create is a
# silent no-op, a renamed one pages the operator with "the unique guards are NOT in place"
# every boot. tests/test_review_auth.py pins the agreement.
DEPOSIT_HASH_INDEX = "uniq_src_tx_hash"
CREDIT_REF_INDEX = "uniq_credit_ref"


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


def _key_list(keys: Any) -> list[tuple[str, Any]]:
    return [(keys, 1)] if isinstance(keys, str) else [tuple(k) for k in keys]


async def _ensure(coll: Any, keys: Any, **opts: Any) -> None:
    """create_index, repairing an index that already exists with different options.

    Mongo answers IndexOptionsConflict (85) / IndexKeySpecsConflict (86) when the same key
    pattern or name already exists with other options (e.g. the non-unique src_tx_hash index
    this build replaces with a unique one). Drop that one and create the intended index.
    """
    try:
        await coll.create_index(keys, **opts)
        return
    except OperationFailure as e:
        if e.code not in (85, 86):
            raise
        log.warning("index conflict on %s %s: %s — rebuilding", coll.name, keys, e)
    want = _key_list(keys)
    name = opts.get("name")
    info = await coll.index_information()
    for iname, spec in info.items():
        if iname == "_id_":
            continue
        if iname == name or [tuple(k) for k in spec.get("key", [])] == want:
            await coll.drop_index(iname)
    await coll.create_index(keys, **opts)


async def _drop_if_present(coll: Any, name: str) -> None:
    try:
        await coll.drop_index(name)
        log.info("dropped index %s.%s", coll.name, name)
    except OperationFailure as e:
        if e.code not in (27, None) and "not found" not in str(e).lower():  # 27 = IndexNotFound
            raise


async def ensure_indexes() -> list[str]:
    """Create every index the app and the scanner query on. Returns the failures (also kept in
    INDEX_ERRORS). Callers must report a non-empty list loudly — never ignore it."""
    d = db()
    problems: list[str] = []

    async def step(what: str, coro: Any) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001 — one bad index must not hide the other twenty
            problems.append(f"{what}: {type(e).__name__}: {e}")
            log.error("ensure_indexes: %s failed: %s: %s", what, type(e).__name__, e)

    # sign-in / destination nonces: `at` is a tz-aware datetime (auth.py), so these TTLs fire.
    for coll, mult in NONCE_TTL_MULT.items():
        await step(
            f"{coll}.at TTL",
            _ensure(d[coll], "at", expireAfterSeconds=settings.nonce_ttl_s * mult),
        )
    await step(
        "rate_limits.at TTL",
        _ensure(d.rate_limits, "at", expireAfterSeconds=2 * settings.rate_window_s),
    )

    # quotes: NO TTL. A DLN fill can be indexed hours late and the scanner resolves it through
    # quotes.order_id / quotes.metadata; expiring the quote would orphan the deposit.
    # prune_quotes() (wired into the monitor loop) does the housekeeping instead.
    await step("quotes.at TTL drop", _drop_if_present(d.quotes, "at_1"))
    await step("quotes account/at", _ensure(d.quotes, [("account_id", 1), ("at", -1)]))
    await step("quotes.order_id", _ensure(d.quotes, "order_id"))
    await step("quotes.metadata", _ensure(d.quotes, "metadata"))

    await step(
        "entries account/asset/at", _ensure(d.entries, [("account_id", 1), ("asset", 1), ("at", 1)])
    )
    await step("entries kind/ref", _ensure(d.entries, [("kind", 1), ("ref", 1)]))
    # one credit per ref, ever (a replayed credit is money invented). Partial, because the
    # other kinds legitimately share a ref. Same spec and same name as ledger.ensure_indexes().
    await step(
        "entries credit/ref unique",
        _ensure(
            d.entries,
            [("kind", 1), ("ref", 1)],
            unique=True,
            partialFilterExpression={"kind": "credit"},
            name=CREDIT_REF_INDEX,
        ),
    )

    await step(
        "deposits account/created", _ensure(d.deposits, [("account_id", 1), ("created_at", -1)])
    )
    await step("deposits status/updated", _ensure(d.deposits, [("status", 1), ("updated_at", 1)]))
    await step("deposits.order_id", _ensure(d.deposits, "order_id"))
    await step("deposits.quote_id", _ensure(d.deposits, "quote_id"))
    await step(
        "deposits eth.tx/log_index", _ensure(d.deposits, [("eth.tx", 1), ("eth.log_index", 1)])
    )
    # one deposit per source transaction. Partial on $type:string because the scanner inserts
    # rows with src_tx_hash: null (lock seen before the hash was registered) and a plain unique
    # index would allow only ONE of those in the whole collection. The non-unique src_tx_hash
    # index stays: a partial index cannot serve a {src_tx_hash: null} lookup. Same spec and
    # same name as scanner.ensure_indexes().
    await step("deposits.src_tx_hash", _ensure(d.deposits, "src_tx_hash"))
    await step(
        "deposits.src_tx_hash unique",
        _ensure(
            d.deposits,
            "src_tx_hash",
            unique=True,
            partialFilterExpression={"src_tx_hash": {"$type": "string"}},
            name=DEPOSIT_HASH_INDEX,
        ),
    )

    await step(
        "destinations account/address",
        _ensure(d.destinations, [("account_id", 1), ("address", 1)], unique=True),
    )
    await step(
        "payout_requests account/created",
        _ensure(d.payout_requests, [("account_id", 1), ("created_at", -1)]),
    )
    await step(
        "payout_requests status/release",
        _ensure(d.payout_requests, [("status", 1), ("release_at", 1)]),
    )
    await step("events notified/at", _ensure(d.events, [("notified", 1), ("at", 1)]))
    await step("events.at TTL", _ensure(d.events, "at", expireAfterSeconds=30 * 86400))

    INDEX_ERRORS[:] = problems
    return problems


def note_index_failure(msg: str) -> None:
    """Record a failure raised around ensure_indexes (kept here so INDEX_ERRORS has one writer)."""
    INDEX_ERRORS.append(msg)
    log.error("ensure_indexes: %s", msg)


def indexes_ok() -> bool:
    return not INDEX_ERRORS


def index_error_labels() -> list[str]:
    """The failing index names only — the exception text stays in the log, not in a public body."""
    return [m.split(":", 1)[0] for m in INDEX_ERRORS]


async def prune_quotes(older_than_s: int | None = None) -> int:
    """Delete quotes older than `older_than_s` (default settings.quote_prune_after_s).

    This replaces the TTL index the quotes collection used to carry: housekeeping, not
    expiry — nothing that a late fill could still need is inside the window. Handles both
    the float `at` the quote router writes and a datetime, so it keeps working either way.
    """
    age = int(older_than_s if older_than_s is not None else settings.quote_prune_after_s)
    cutoff = datetime.now(UTC).timestamp() - age
    res = await db().quotes.delete_many(
        {
            "$or": [
                {"at": {"$lt": cutoff}},
                {"at": {"$lt": datetime.fromtimestamp(cutoff, UTC)}},
            ]
        }
    )
    n = int(res.deleted_count or 0)
    if n:
        log.info("pruned %d quote(s) older than %ds", n, age)
    return n
