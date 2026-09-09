"""Auth / config / ops regressions found by the 2026-09-09 adversarial review (task T4b).

One test per defect:
  1  config: placeholder or short session secrets must refuse to boot outside `dev`
  2  main:   /v1/dev/credit must not exist on a prod process
  3  auth:   a nonce past its TTL must be refused at consumption, and `at` must be a date
  4  db:     the indexes the scanner queries, the unique ones, and no TTL on quotes
  5  siwe:   per-IP caps on nonce issuance and verify attempts (429 + Retry-After)
  6  dest:   removal by body, with the path route kept and its access-log line redacted
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import add_destination, sign_in, sign_text, siwe_message
from eth_account import Account as EthAccount
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from pgasme import auth
from pgasme import db as dbmod
from pgasme.config import MIN_SECRET_CHARS, Settings, settings
from pgasme.db import db, ensure_indexes, prune_quotes
from pgasme.main import RedactDestinationPaths, create_app

API_DIR = Path(__file__).resolve().parents[1]
GOOD = "x" * MIN_SECRET_CHARS


def build(**over):
    """Settings from explicit values only — the process environment must not leak in."""
    base = {
        "env": "prod",
        "jwt_secret": GOOD + "-jwt",
        "account_salt": GOOD + "-salt",
        "dev_endpoints": False,  # the suite exports PGAS_DEV_ENDPOINTS=1; prod would refuse
    }
    return Settings(**{**base, **over})


async def prod_client(monkeypatch, **over):
    import httpx

    for k, v in {"env": "prod", **over}.items():
        monkeypatch.setattr(settings, k, v)
    app = create_app()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ── 1. secrets fail closed ────────────────────────────────────────────────────────────────
def test_prod_settings_refuse_the_committed_placeholders():
    for field in ("jwt_secret", "account_salt"):
        for bad in ("dev-only-change-me", "dev-only-change-me-too", "", "short"):
            with pytest.raises(ValidationError) as e:
                build(**{field: bad})
            assert f"PGAS_{field.upper()}" in str(e.value)
            assert "refusing to boot" in str(e.value)
    assert build().secrets_ok is True
    assert build(env="staging").secrets_ok is True


def test_a_secret_shorter_than_the_minimum_is_refused_but_dev_still_boots():
    with pytest.raises(ValidationError) as e:
        build(jwt_secret="x" * (MIN_SECRET_CHARS - 1))
    assert f"minimum {MIN_SECRET_CHARS}" in str(e.value)
    # dev is the one lax environment: the placeholders still work there
    s = Settings(env="dev", jwt_secret="dev-only-change-me", account_salt="dev-only-change-me-too")
    assert s.secrets_ok is False and s.env == "dev"


def test_the_process_actually_refuses_to_start(tmp_path):
    """A real boot attempt, not a unit call: importing the app must fail on a placeholder."""
    env = {
        **os.environ,
        "PGAS_ENV": "prod",
        "PGAS_JWT_SECRET": "dev-only-change-me",
        "PGAS_ACCOUNT_SALT": GOOD + "-salt",
        "PGAS_DEV_ENDPOINTS": "0",
    }
    r = subprocess.run(
        [sys.executable, "-c", "import pgasme.main"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode != 0, r.stdout
    assert "refusing to boot with PGAS_ENV='prod'" in r.stderr
    assert "PGAS_JWT_SECRET" in r.stderr
    # control: the same boot with a real secret succeeds, so it is the secret that refuses
    ok = subprocess.run(
        [sys.executable, "-c", "import pgasme.main"],
        cwd=API_DIR,
        env={**env, "PGAS_JWT_SECRET": GOOD + "-jwt"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert ok.returncode == 0, ok.stderr


# ── 2. the dev mint is never mounted in prod ──────────────────────────────────────────────
def test_dev_endpoints_with_prod_env_refuse_to_boot():
    with pytest.raises(ValidationError) as e:
        build(dev_endpoints=True)
    assert "PGAS_DEV_ENDPOINTS=1" in str(e.value)
    assert build(dev_endpoints=True, env="staging").dev_endpoints_active is True
    assert build(dev_endpoints=False).dev_endpoints_active is False


async def test_dev_credit_is_mounted_in_test_and_absent_in_prod(client, user, monkeypatch):
    r = await client.post(
        "/v1/dev/credit", json={"asset": "ETH", "groth": 5}, headers=user["headers"]
    )
    assert r.status_code == 200 and r.json()["credited"] == 5
    async with await prod_client(monkeypatch) as pc:
        # mounted would be 401 (auth first); absent is 404
        assert (
            await pc.post("/v1/dev/credit", json={"asset": "ETH", "groth": 5})
        ).status_code == 404
        r = await pc.post(
            "/v1/dev/credit", json={"asset": "ETH", "groth": 5}, headers=user["headers"]
        )
        assert r.status_code == 404


async def test_health_reports_the_posture_a_deploy_asserts(client, monkeypatch):
    body = (await client.get("/v1/health")).json()
    assert body["env"] == "test" and body["secrets_ok"] is True
    assert body["dev_endpoints"] is True and body["indexes_ok"] is True and body["ok"] is True
    async with await prod_client(monkeypatch) as pc:
        b2 = (await pc.get("/v1/health")).json()
    assert b2["env"] == "prod" and b2["dev_endpoints"] is False and b2["secrets_ok"] is True


async def test_health_is_not_ok_while_an_index_is_missing(client):
    dbmod.note_index_failure("deposits.src_tx_hash unique: OperationFailure: duplicate key")
    try:
        body = (await client.get("/v1/health")).json()
        assert body["indexes_ok"] is False and body["ok"] is False
        assert body["index_errors"] == ["deposits.src_tx_hash unique"]  # names only, no detail
    finally:
        dbmod.INDEX_ERRORS.clear()


# ── 3. nonce age ──────────────────────────────────────────────────────────────────────────
async def test_nonce_at_is_a_date_so_the_ttl_index_can_see_it(client):
    n = (await client.get("/v1/siwe/nonce")).json()["nonce"]
    row = await db().siwe_nonces.find_one({"_id": n})
    assert isinstance(row["at"], datetime)


async def test_a_stale_nonce_is_refused_at_consumption(client, wallet):
    n = (await client.get("/v1/siwe/nonce")).json()["nonce"]
    await db().siwe_nonces.update_one(
        {"_id": n},
        {"$set": {"at": datetime.now(UTC) - timedelta(seconds=settings.nonce_ttl_s + 60)}},
    )
    msg = siwe_message(wallet.address, n)
    r = await client.post(
        "/v1/siwe/verify", json={"message": msg, "signature": sign_text(wallet.key, msg)}
    )
    assert r.status_code == 400 and "nonce" in r.json()["detail"]
    assert await db().siwe_nonces.find_one({"_id": n}) is None  # refused AND spent


async def test_consume_nonce_honours_each_collections_ttl():
    fresh = await auth.new_nonce()
    assert await auth.consume_nonce(fresh) is True
    assert await auth.consume_nonce(fresh) is False  # single use
    old = await auth.new_nonce("dest_nonces")
    # inside the doubled dest TTL, outside the plain one
    await db().dest_nonces.update_one(
        {"_id": old},
        {"$set": {"at": datetime.now(UTC) - timedelta(seconds=settings.nonce_ttl_s + 30)}},
    )
    assert auth.nonce_max_age_s("dest_nonces") == 2 * settings.nonce_ttl_s
    assert await auth.consume_nonce(old, "dest_nonces") is True


async def test_a_float_at_row_from_the_old_build_is_never_usable():
    await db().siwe_nonces.insert_one({"_id": "legacy", "at": time.time()})
    assert await auth.consume_nonce("legacy") is False
    assert await db().siwe_nonces.find_one({"_id": "legacy"}) is None


# ── 4. indexes ────────────────────────────────────────────────────────────────────────────
async def test_ensure_indexes_creates_what_the_scanner_queries():
    assert await ensure_indexes() == []
    quotes = await db().quotes.index_information()
    deposits = await db().deposits.index_information()
    entries = await db().entries.index_information()
    keys = lambda info: {tuple(tuple(k) for k in v["key"]) for v in info.values()}  # noqa: E731
    assert (("order_id", 1),) in keys(quotes) and (("metadata", 1),) in keys(quotes)
    for k in ((("order_id", 1),), (("quote_id", 1),), (("eth.tx", 1), ("eth.log_index", 1))):
        assert k in keys(deposits), k
    assert deposits[dbmod.DEPOSIT_HASH_INDEX]["unique"] is True
    assert deposits[dbmod.DEPOSIT_HASH_INDEX]["partialFilterExpression"] == {
        "src_tx_hash": {"$type": "string"}
    }
    assert entries[dbmod.CREDIT_REF_INDEX]["unique"] is True
    assert entries[dbmod.CREDIT_REF_INDEX]["partialFilterExpression"] == {"kind": "credit"}
    for coll, mult in dbmod.NONCE_TTL_MULT.items():
        info = await db()[coll].index_information()
        assert info["at_1"]["expireAfterSeconds"] == settings.nonce_ttl_s * mult


def test_the_index_names_agree_with_the_modules_that_also_create_them():
    """scanner.py and ledger.py create the SAME four unique indexes at worker start. Mongo
    refuses an identical spec under a second name (IndexOptionsConflict 85), so a rename on
    either side must fail here, not in production at 03:00."""
    from pgasme import ledger, scanner

    assert dbmod.DEPOSIT_HASH_INDEX == scanner.DEPOSIT_HASH_INDEX
    assert dbmod.CREDIT_REF_INDEX == ledger.CREDIT_REF_INDEX
    assert dbmod.RELEASE_REF_INDEX == ledger.RELEASE_REF_INDEX
    assert dbmod.FEE_REF_INDEX == ledger.FEE_REF_INDEX


async def test_quotes_carry_no_ttl_and_an_old_one_is_dropped():
    await db().quotes.create_index("at", expireAfterSeconds=settings.quote_ttl_s)  # the old build
    assert await ensure_indexes() == []
    info = await db().quotes.index_information()
    assert "at_1" not in info
    assert not any("expireAfterSeconds" in v for v in info.values())


async def test_the_unique_indexes_actually_refuse_a_replay():
    await ensure_indexes()
    await db().deposits.insert_one({"_id": "d1", "src_tx_hash": "0x" + "ab" * 32})
    with pytest.raises(DuplicateKeyError):
        await db().deposits.insert_one({"_id": "d2", "src_tx_hash": "0x" + "ab" * 32})
    # the scanner writes src_tx_hash: null for a lock seen before the hash was registered —
    # a plain unique index would allow exactly one of those in the whole collection
    await db().deposits.insert_one({"_id": "d3", "src_tx_hash": None})
    await db().deposits.insert_one({"_id": "d4", "src_tx_hash": None})
    await db().entries.insert_one({"kind": "credit", "ref": "lock:1"})
    with pytest.raises(DuplicateKeyError):
        await db().entries.insert_one({"kind": "credit", "ref": "lock:1"})
    await db().entries.insert_one({"kind": "schedule", "ref": "req:1"})
    await db().entries.insert_one({"kind": "schedule", "ref": "req:1"})  # other kinds share refs


async def test_prune_quotes_keeps_what_a_late_fill_still_needs():
    now = time.time()
    await db().quotes.insert_many(
        [
            {"_id": "old", "at": now - 8 * 86400, "order_id": "0xdead"},
            {"_id": "yesterday", "at": now - 86400, "order_id": "0xbeef"},
            {"_id": "dated", "at": datetime.now(UTC) - timedelta(days=9)},
        ]
    )
    assert await prune_quotes() == 2
    left = {q["_id"] async for q in db().quotes.find({})}
    assert left == {"yesterday"}  # a 1-day-old quote outlives the 15-minute quote TTL


# ── 5. per-IP caps ────────────────────────────────────────────────────────────────────────
async def test_nonce_issuance_is_capped_per_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "siwe_nonce_ip_limit", 3)
    for _ in range(3):
        assert (await client.get("/v1/siwe/nonce")).status_code == 200
    r = await client.get("/v1/siwe/nonce")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # consuming one frees a slot: the cap is on OUTSTANDING nonces, not on lifetime issuance
    n = (await db().siwe_nonces.find_one({}))["_id"]
    assert await auth.consume_nonce(n) is True
    assert (await client.get("/v1/siwe/nonce")).status_code == 200


async def test_verify_attempts_are_capped_per_ip(client, wallet, monkeypatch):
    monkeypatch.setattr(settings, "siwe_verify_ip_limit", 2)
    body = {"message": "x" * 60, "signature": "0x" + "11" * 65}
    for _ in range(2):
        assert (await client.post("/v1/siwe/verify", json=body)).status_code == 400  # malformed
    r = await client.post("/v1/siwe/verify", json=body)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # and a legitimate sign-in is refused too while the window is full — the cap is per IP
    n = (await client.get("/v1/siwe/nonce")).json()["nonce"]
    msg = siwe_message(wallet.address, n)
    r = await client.post(
        "/v1/siwe/verify", json={"message": msg, "signature": sign_text(wallet.key, msg)}
    )
    assert r.status_code == 429


async def test_the_cap_counts_per_ip_not_globally(client, monkeypatch):
    monkeypatch.setattr(settings, "siwe_verify_ip_limit", 1)
    assert await auth.rate_hit("siwe_verify", "1.2.3.4", 1, 600) == 0
    assert await auth.rate_hit("siwe_verify", "1.2.3.4", 1, 600) > 0
    assert await auth.rate_hit("siwe_verify", "5.6.7.8", 1, 600) == 0


# ── 6. destination removal without the address in the URL ─────────────────────────────────
async def test_remove_by_body_is_the_documented_route(client, user):
    dest = EthAccount.create()
    await add_destination(client, user, dest)
    r = await client.post(
        "/v1/destinations/remove", json={"address": dest.address}, headers=user["headers"]
    )
    assert r.status_code == 200 and r.json()["removed"] == dest.address
    rows = (await client.get("/v1/destinations", headers=user["headers"])).json()["destinations"]
    assert dest.address not in {d["address"] for d in rows}
    # same guards as the path route, same implementation
    assert (
        await client.post(
            "/v1/destinations/remove", json={"address": user["address"]}, headers=user["headers"]
        )
    ).status_code == 409
    # removing again is idempotent (the row is tombstoned, not deleted) — same as DELETE
    assert (
        await client.post(
            "/v1/destinations/remove", json={"address": dest.address}, headers=user["headers"]
        )
    ).status_code == 200
    never = EthAccount.create().address
    assert (
        await client.post(
            "/v1/destinations/remove", json={"address": never}, headers=user["headers"]
        )
    ).status_code == 404
    assert (
        await client.post(
            "/v1/destinations/remove", json={"address": "nope"}, headers=user["headers"]
        )
    ).status_code in (400, 422)
    assert (
        await client.post("/v1/destinations/remove", json={"address": dest.address})
    ).status_code == 401


async def test_the_path_route_still_works_for_shipped_clients(client, wallet):
    user = await sign_in(client, wallet)
    dest = EthAccount.create()
    await add_destination(client, user, dest)
    r = await client.delete(f"/v1/destinations/{dest.address}", headers=user["headers"])
    assert r.status_code == 200 and r.json()["removed"] == dest.address


def test_the_access_log_does_not_keep_a_destination_beside_an_ip():
    f = RedactDestinationPaths()

    def line(path: str) -> str:
        rec = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            0,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "DELETE", path, "1.1", 200),
            None,
        )
        f.filter(rec)
        return rec.getMessage()

    addr = "0x1111111111111111111111111111111111111111"
    assert addr not in line(f"/v1/destinations/{addr}")
    assert "<redacted>" in line(f"/v1/destinations/{addr}")
    assert "/v1/destinations/nonce" in line("/v1/destinations/nonce")
    assert "/v1/siwe/nonce" in line("/v1/siwe/nonce")
