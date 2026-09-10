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

from .config import GAS_SAMPLE_TTL_S, settings

# the gas-basis series (payouts.record_gas_sample is its ONE writer). Named here because this
# module builds its TTL and this module must not import `payouts` — `payouts` imports this one.
GAS_SAMPLES = "gas_samples"

log = logging.getLogger("pgasme.db")

_client: Any = None
_db: Any = None

# Nonce collections and how many nonce_ttl_s each one lives for. auth.py reads this map so the
# expiry the TTL index enforces and the expiry consume_nonce checks can never drift apart.
NONCE_TTL_MULT: dict[str, int] = {"siwe_nonces": 1, "dest_nonces": 2}

# Written only by ensure_indexes(); read by /v1/health.
INDEX_ERRORS: list[str] = []

# These unique indexes are ALSO created by the modules that depend on them at worker start
# (scanner.DEPOSIT_HASH_INDEX, ledger.CREDIT_REF_INDEX / RELEASE_REF_INDEX / FEE_REF_INDEX /
# BRIDGE_FEE_REF_INDEX / CANCEL_REF_INDEX).
# Mongo answers IndexOptionsConflict (85) "Index already exists with a different name" when the
# same spec is created twice under two names, so these names must stay byte-identical to
# theirs — an identical create is a silent no-op, a renamed one pages the operator with "the
# unique guards are NOT in place" every boot. tests/test_review_auth.py pins the agreement.
# ⛔ AND THE LIST HAS TO BE COMPLETE, because THIS is the one the API process builds (main.py
# lifespan): `ledger.cancel` — the refund — runs in the API, and `ledger.ensure_indexes` runs
# only where the workers do. A guard missing from here is a guard a workers-off deployment does
# not have at all, on the entry that puts money back into Available.
DEPOSIT_HASH_INDEX = "uniq_src_tx_hash"
CREDIT_REF_INDEX = "uniq_credit_ref"
RELEASE_REF_INDEX = "uniq_release_ref"
FEE_REF_INDEX = "uniq_fee_ref"
BRIDGE_FEE_REF_INDEX = "uniq_bridge_fee_ref"
CANCEL_REF_INDEX = "uniq_cancel_ref"
# The build before this one guarded both kinds with ONE index whose filter was
# `{"kind": {"$in": ["release", "fee"]}}`. MongoDB supports `$in` inside a
# partialFilterExpression only from 6.0; PRODUCTION RUNS 5.0 and answers CannotCreateIndex
# (67), so that index exists nowhere on prod — but it may exist on a 6.0+ development box,
# where it would be a second writer of the same fact. Dropped by name.
LEGACY_RELEASE_FEE_INDEX = "uniq_release_fee_ref"
# One row per BeamPay delivery: its webhooks are at-least-once and INTEGRATION.md §5 names
# (txId, event) as the key to dedupe on. `routers/internal.py` is the only writer of this
# collection, and it derives the row's `_id` from the SAME pair — so the dedupe holds even
# before this index exists (a fresh box, or a boot where ensure_indexes failed), and the two
# can never disagree about what a duplicate is.
BEAMPAY_EVENT_INDEX = "uniq_beampay_event"


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


# "this index already exists and it is not the one you asked for". MongoDB answers
# IndexOptionsConflict (85) / IndexKeySpecsConflict (86); the SAME refusal arrives from
# mongomock — and from any driver that does not surface a code — as text alone, so a repair
# keyed only on the number is a repair the offline suite can never prove and a fresh mongo
# minor version can silently stop reaching. Both are read (§the prober must call the way the
# caller calls). Every other OperationFailure is a real failure and is re-raised.
INDEX_CONFLICT_CODES = (85, 86)
INDEX_CONFLICT_HINTS = ("already exists with different options", "already exists with a different")


def is_index_conflict(e: OperationFailure) -> bool:
    text = str(e).lower()
    return e.code in INDEX_CONFLICT_CODES or any(h in text for h in INDEX_CONFLICT_HINTS)


async def _ensure(coll: Any, keys: Any, **opts: Any) -> None:
    """create_index, repairing an index that already exists with different options.

    Mongo answers IndexOptionsConflict (85) / IndexKeySpecsConflict (86) when the same key
    pattern or name already exists with other options (e.g. the non-unique src_tx_hash index
    this build replaces with a unique one). Drop that one and create the intended index.
    `is_index_conflict` reads the message as well as the code — a repair that only ever fires
    on a number is a repair the offline suite cannot prove and a box can silently stop getting.

    ⚠️ It drops EVERY index on the same key, so a caller that also wants a plain index on that
    key must build the repaired one FIRST (see the two src_tx_hash steps below).
    """
    try:
        await coll.create_index(keys, **opts)
        return
    except OperationFailure as e:
        if not is_index_conflict(e):
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
    # the gas basis a bridge crossing is priced on (T45): one row per deposit-watcher pass,
    # read as a 24 h p75, kept for 48 h so the evidence outlives the window it counts in. `at`
    # is a tz-aware datetime for exactly the reason the two TTLs above are — a TTL index only
    # fires on a BSON date — and `at_s` beside it is the epoch second the window is read on.
    await step(
        f"{GAS_SAMPLES}.at TTL", _ensure(d[GAS_SAMPLES], "at", expireAfterSeconds=GAS_SAMPLE_TTL_S)
    )
    await step(f"{GAS_SAMPLES}.at_s", _ensure(d[GAS_SAMPLES], [("at_s", -1)]))

    # quotes: NO TTL. A cross-chain fill can be indexed hours late and the scanner resolves it through
    # quotes.order_id / quotes.metadata; expiring the quote would orphan the deposit.
    # prune_quotes() (wired into the monitor loop) does the housekeeping instead.
    await step("quotes.at TTL drop", _drop_if_present(d.quotes, "at_1"))
    await step("quotes account/at", _ensure(d.quotes, [("account_id", 1), ("at", -1)]))
    await step("quotes.order_id", _ensure(d.quotes, "order_id"))
    await step("quotes.metadata", _ensure(d.quotes, "metadata"))
    # ── one Beam receiver key per deposit (pgasme/receiver_keys.py). The QUOTE row is the
    # authority on which quote holds an issued key, and `scanner.attribute_from_quote` looks a
    # lock's 33 bytes up here on the money path — an unindexed collection scan is what that
    # lookup becomes on a box with a year of quotes. Sparse, because only an indexed quote
    # carries the field at all.
    await step("quotes.receiver_pk", _ensure(d.quotes, "receiver_pk", sparse=True))
    # the KEY CACHE, read by `issued_pks` (what the scanner will recognise) and `open_indexes`
    # (what the claim watcher asks view_incoming about) — both bounded, both newest-first
    await step(
        "receiver_keys pipe/issued", _ensure(d.receiver_keys, [("pipe_cid", 1), ("issued_at", -1)])
    )

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
    # one `release` and one `fee` per ref. ⛔ ONE EQUALITY FILTER PER INDEX and a key pattern
    # each: `$in` inside a partialFilterExpression is a MongoDB 6.0 feature and prod runs 5.0
    # (see ledger.ensure_indexes for the whole argument). Same specs and same names as there.
    await step(
        "entries release/fee legacy drop",
        _drop_if_present(d.entries, LEGACY_RELEASE_FEE_INDEX),
    )
    await step(
        "entries release/ref unique",
        _ensure(
            d.entries,
            [("ref", 1), ("kind", 1)],
            unique=True,
            partialFilterExpression={"kind": "release"},
            name=RELEASE_REF_INDEX,
        ),
    )
    await step(
        "entries fee/ref unique",
        _ensure(
            d.entries,
            [("ref", 1)],
            unique=True,
            partialFilterExpression={"kind": "fee"},
            name=FEE_REF_INDEX,
        ),
    )
    # …one `bridge_fee` per ref (the pass-through half of a release, itemised since 2026-09-10)
    # and one `cancel` per ref (the REFUND). A fourth and a fifth key pattern for the same
    # reason the others differ: one equality filter per index, and no two of them sharing a key
    # pattern. Same specs and same names as ledger.ensure_indexes().
    await step(
        "entries bridge_fee/ref unique",
        _ensure(
            d.entries,
            [("ref", 1), ("account_id", 1)],
            unique=True,
            partialFilterExpression={"kind": "bridge_fee"},
            name=BRIDGE_FEE_REF_INDEX,
        ),
    )
    await step(
        "entries cancel/ref unique",
        _ensure(
            d.entries,
            [("account_id", 1), ("ref", 1)],
            unique=True,
            partialFilterExpression={"kind": "cancel"},
            name=CANCEL_REF_INDEX,
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
    # one VERIFIED deposit per source transaction. Partial on $type:string because the scanner
    # inserts rows with src_tx_hash: null (lock seen before the hash was registered) and a plain
    # unique index would allow only ONE of those in the whole collection. The non-unique
    # src_tx_hash index stays: a partial index cannot serve a {src_tx_hash: null} lookup. Same
    # spec and same name as scanner.ensure_indexes().
    # ⛔ `verified: true` IS PART OF THE FILTER, and it is a deliberate weakening. A hash is
    # public the moment it is broadcast, so registering one is a CLAIM (scanner.claims_on): with
    # the old, wider filter whoever POSTED first took the hash, and after 2026-09-10 — when an
    # unseen hash started opening a row instead of being refused — a stranger watching the
    # mempool could lock the person who actually signed it out with a 409 until the TTL. Several
    # unverified claims may coexist; identity (the transaction's sender) can prove exactly one,
    # and from that moment the index is what makes the hash that row's alone.
    # `_ensure` repairs a box that still carries the old filter under this name
    # (IndexOptionsConflict 85 → drop → recreate), and this runs in the API process at boot,
    # before workers.start() calls the scanner's copy.
    # ⚠️ THE UNIQUE ONE IS BUILT FIRST, and the order is load-bearing: `_ensure`'s repair drops
    # every index on the same KEY before recreating, so repairing this one after the plain one
    # would take the plain one with it and leave it missing until the next boot.
    await step(
        "deposits.src_tx_hash unique",
        _ensure(
            d.deposits,
            "src_tx_hash",
            unique=True,
            partialFilterExpression={"src_tx_hash": {"$type": "string"}, "verified": True},
            name=DEPOSIT_HASH_INDEX,
        ),
    )
    await step("deposits.src_tx_hash", _ensure(d.deposits, "src_tx_hash"))
    # `receiver_keys.open_indexes` distincts this over the rows whose message is still
    # unclaimed, once per treasury pass, per asset
    await step(
        "deposits asset/receiver_index",
        _ensure(d.deposits, [("asset", 1), ("receiver_index", 1)], sparse=True),
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
    # BeamPay's webhook log: evidence only, never money state (routers/internal.py). Unique on
    # the delivery key BeamPay itself documents, so a redelivery cannot become a second row.
    await step(
        "beampay_events txId/event unique",
        _ensure(
            d.beampay_events,
            [("txId", 1), ("event", 1)],
            unique=True,
            name=BEAMPAY_EVENT_INDEX,
        ),
    )
    await step("beampay_events.received_at", _ensure(d.beampay_events, [("received_at", -1)]))

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
