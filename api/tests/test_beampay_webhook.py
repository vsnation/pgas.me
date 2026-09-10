"""POST /internal/beampay/webhook — the endpoint whose absence flooded the operator's group.

2026-09-10 00:1x–00:2xZ: BeamPay was configured to post to `http://127.0.0.1:8300/internal/
beampay/webhook`, the endpoint did not exist, and its worker pages Telegram on EVERY failed
attempt and retries the same record up to 20 times — seven "Webhook Failed … Event: failed"
messages in minutes for ONE transaction.

What these tests pin, in the order the endpoint decides it:

  a caller that is not an un-proxied loopback caller gets 404 and learns nothing
  an unconfigured token is a REFUSAL (503), never an open door
  a wrong token is 401 and the body is never read
  garbage is 422
  a delivery is stored exactly once, and a redelivery changes nothing
  NO money state is ever touched — not by a `failed` event, not by a `deposit_confirmed`
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import fund, sign_in

from pgasme.config import settings
from pgasme.db import BEAMPAY_EVENT_INDEX, ensure_indexes
from pgasme.main import create_app
from pgasme.routers import internal

HOOK = "/internal/beampay/webhook"
TOKEN = "webhook-token-" + "z" * 32
TXID = "5b0b6a1a0f7a4b0d8f3e2c1d9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b"

# The exact shape BeamPay's webhook_worker.py posts for a confirmed deposit.
DEPOSIT_CONFIRMED: dict[str, Any] = {
    "event": "deposit_confirmed",
    "txId": TXID,
    "amount": 1200000,
    "value_formatted": "0.01200000",
    "asset_id": 36,
    "asset_name": "bETH",
    # a Beam address SHAPE (66 hex, X ‖ parity byte); never a real one — this file is published
    "address": "7f" * 32 + "01",
    "comment": "pgas shield dep1 chunk 0",
    "kernel": "9f" * 32,
}
# `failed` carries a `reason` and NO comment/kernel — a different shape, same endpoint.
FAILED: dict[str, Any] = {
    "event": "failed",
    "txId": TXID,
    "amount": 1200000,
    "value_formatted": "0.01200000",
    "asset_id": 36,
    "asset_name": "bETH",
    "reason": "Failed to send transaction",
    "address": None,
}


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configured by default; the 503 test unsets it again."""
    monkeypatch.setattr(settings, "beampay_webhook_token", TOKEN)
    return TOKEN


def url(tok: str | None = TOKEN) -> str:
    return HOOK if tok is None else f"{HOOK}?token={tok}"


async def rows(mock_db: Any) -> list[dict[str, Any]]:
    return await mock_db["pgasme_test"].beampay_events.find({}).to_list(100)


@pytest.fixture
def remote() -> Any:
    """A client whose ASGI scope says the request came from somewhere else on the network."""

    async def make(host: str = "203.0.113.9") -> Any:
        app = create_app()
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(host, 40000)),
            base_url="http://test",
        )

    return make


# ================================================== §1 · the door


async def test_an_unconfigured_token_refuses_every_delivery(client, mock_db, monkeypatch, caplog):
    """A missing secret is a refusal, not an open door — and it says so in the log, by KEY NAME."""
    monkeypatch.setattr(settings, "beampay_webhook_token", "")
    with caplog.at_level("ERROR", logger="pgasme.internal"):
        r = await client.post(url(), json=DEPOSIT_CONFIRMED)
    assert r.status_code == 503
    assert "PGAS_BEAMPAY_WEBHOOK_TOKEN is not configured" in caplog.text
    assert await rows(mock_db) == []
    # …and it refuses the same way when no token is offered at all
    assert (await client.post(url(None), json=DEPOSIT_CONFIRMED)).status_code == 503


async def test_a_wrong_or_absent_token_is_401_and_the_body_is_never_read(client, mock_db, caplog):
    with caplog.at_level("WARNING", logger="pgasme.internal"):
        wrong = await client.post(url("not-the-token"), json=DEPOSIT_CONFIRMED)
        absent = await client.post(url(None), json=DEPOSIT_CONFIRMED)
        empty = await client.post(f"{HOOK}?token=", json=DEPOSIT_CONFIRMED)
    assert [wrong.status_code, absent.status_code, empty.status_code] == [401, 401, 401]
    assert "wrong or absent token" in caplog.text
    assert TOKEN not in caplog.text and "not-the-token" not in caplog.text  # never the value
    assert await rows(mock_db) == []


async def test_a_forwarded_request_does_not_exist(client, mock_db):
    """nginx adds X-Forwarded-For to everything it proxies, so its presence proves the request
    did not come from BeamPay's direct loopback post — even with the right token."""
    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
        r = await client.post(url(), json=DEPOSIT_CONFIRMED, headers={header: "203.0.113.9"})
        assert r.status_code == 404, header
        assert r.json() == {"detail": "not found"}
    assert await rows(mock_db) == []


async def test_a_caller_off_the_loopback_interface_does_not_exist(remote, mock_db):
    async with await remote() as c:
        r = await c.post(url(), json=DEPOSIT_CONFIRMED)
    assert r.status_code == 404 and await rows(mock_db) == []
    # …and the refusal happens BEFORE the token is looked at: a public prober cannot tell a
    # missing token from a wrong one from a right one
    async with await remote() as c:
        assert (await c.post(url("not-the-token"), json=DEPOSIT_CONFIRMED)).status_code == 404
        garbage = await c.post(
            url(None), content=b"{", headers={"Content-Type": "application/json"}
        )
        assert garbage.status_code == 404  # not even 422: the body is never reached


async def test_the_ipv4_mapped_loopback_address_is_still_loopback(remote, mock_db):
    """A dual-stack listener reports 127.0.0.1 as ::ffff:127.0.0.1; a string comparison against
    {"127.0.0.1", "::1"} would refuse the only caller this route exists for."""
    assert internal.loopback("::ffff:127.0.0.1") and internal.loopback("::1")
    assert internal.loopback("127.0.0.1") and internal.loopback("127.0.1.1")
    assert not internal.loopback("203.0.113.9") and not internal.loopback("nonsense")
    async with await remote("::ffff:127.0.0.1") as c:
        r = await c.post(url(), json=DEPOSIT_CONFIRMED)
    assert r.status_code == 200 and len(await rows(mock_db)) == 1


# ================================================== §2 · the body


async def test_garbage_is_422_and_writes_nothing(client, mock_db):
    bad: list[Any] = [
        {},  # nothing at all
        {"event": "deposit_confirmed"},  # no txId
        {"txId": TXID},  # no event
        {"event": "deposit_confirmed", "txId": ""},  # empty txId
        {"event": "deposit_confirmed", "txId": 17},  # not a string
        {"event": "settled", "txId": TXID},  # an event this build has never heard of
        [DEPOSIT_CONFIRMED],  # a JSON array, not an object
        "hello",  # a JSON string
    ]
    for body in bad:
        r = await client.post(url(), json=body)
        assert r.status_code == 422, (body, r.text)
    # not JSON at all, and a body far larger than any BeamPay payload
    raw = await client.post(
        url(), content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert raw.status_code == 422
    huge = await client.post(
        url(),
        content=json.dumps({**DEPOSIT_CONFIRMED, "comment": "x" * internal.MAX_BODY_BYTES}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert huge.status_code == 422
    assert await rows(mock_db) == []


async def test_every_documented_event_is_accepted(client, mock_db):
    for i, event in enumerate(internal.EVENTS):
        r = await client.post(url(), json={**DEPOSIT_CONFIRMED, "event": event, "txId": f"tx{i}"})
        assert r.status_code == 200 and r.json() == {"ok": True, "stored": True}, event
    stored = {r["event"] for r in await rows(mock_db)}
    assert stored == set(internal.EVENTS)


# ================================================== §3 · exactly once


async def test_a_delivery_is_stored_once_and_a_redelivery_writes_nothing(client, mock_db):
    """Delivery is at-least-once (INTEGRATION.md §5) — the dedupe key is (txId, event)."""
    first = await client.post(url(), json=DEPOSIT_CONFIRMED)
    assert first.status_code == 200 and first.json() == {"ok": True, "stored": True}
    again = await client.post(url(), json=DEPOSIT_CONFIRMED)
    assert again.status_code == 200 and again.json() == {"ok": True, "stored": False}
    # …and a THIRD attempt whose body differs is still the same delivery
    third = await client.post(url(), json={**DEPOSIT_CONFIRMED, "comment": "rewritten"})
    assert third.status_code == 200 and third.json()["stored"] is False
    got = await rows(mock_db)
    assert len(got) == 1
    row = got[0]
    assert row["_id"] == f"deposit_confirmed:{TXID}"
    assert row["event"] == "deposit_confirmed" and row["txId"] == TXID
    assert row["payload"] == DEPOSIT_CONFIRMED  # verbatim, including the fields we never read
    assert row["received_at"] > 0
    # the same transaction in another state is a DIFFERENT delivery and is recorded
    assert (await client.post(url(), json={**DEPOSIT_CONFIRMED, "event": "failed"})).json()[
        "stored"
    ] is True
    assert len(await rows(mock_db)) == 2


async def test_the_unique_index_carries_the_same_key_as_the_row_id(client, mock_db):
    """db.ensure_indexes owns the index; routers/internal owns the _id. They must agree — the
    _id is what makes the dedupe hold on a box where ensure_indexes has not run yet."""
    assert await ensure_indexes() == []
    info = await mock_db["pgasme_test"].beampay_events.index_information()
    assert BEAMPAY_EVENT_INDEX in info
    spec = info[BEAMPAY_EVENT_INDEX]
    assert [tuple(k) for k in spec["key"]] == [("txId", 1), ("event", 1)]
    assert spec.get("unique") is True
    assert internal.row_id("failed", TXID) == f"failed:{TXID}"
    assert (await client.post(url(), json=FAILED)).json() == {"ok": True, "stored": True}
    assert (await client.post(url(), json=FAILED)).json() == {"ok": True, "stored": False}
    assert len(await rows(mock_db)) == 1


# ================================================== §4 · no money state, ever


async def test_a_failed_event_changes_no_deposit_no_payout_and_no_ledger_row(
    client, mock_db, wallet
):
    """The pollers are the only writers of money state (law 9). A webhook is EVIDENCE: BeamPay
    delivers at-least-once to an unauthenticated-by-design local port, and a `failed` that could
    fail an order would be a stranger's ability to fail our money."""
    user = await sign_in(client, wallet)
    await fund(user, "ETH", 5_000_000)
    d = mock_db["pgasme_test"]
    await d.deposits.insert_one(
        {
            "_id": "dep1",
            "account_id": user["account_id"],
            "status": "credited",
            "treasury": "shielding",
            "shield_txids": [TXID],
            "value_groth": 1_200_000,
        }
    )
    await d.payout_requests.insert_one(
        {"_id": "req1", "account_id": user["account_id"], "status": "bridging", "beam_txid": TXID}
    )
    before = {
        "deposits": await d.deposits.find({}).to_list(10),
        "payouts": await d.payout_requests.find({}).to_list(10),
        "entries": await d.entries.find({}).to_list(50),
        "events": await d.events.count_documents({}),
    }
    for body in (FAILED, DEPOSIT_CONFIRMED, {**DEPOSIT_CONFIRMED, "event": "cancelled"}):
        assert (await client.post(url(), json=body)).status_code == 200
    assert await d.deposits.find({}).to_list(10) == before["deposits"]
    assert await d.payout_requests.find({}).to_list(10) == before["payouts"]
    assert await d.entries.find({}).to_list(50) == before["entries"]
    # and it does not page anyone either: the row IS the record (a webhook that alerted would be
    # the flood again, from our side this time)
    assert await d.events.count_documents({}) == before["events"]
    assert len(await rows(mock_db)) == 3


async def test_the_route_is_not_in_the_public_schema(client):
    """/internal/* is a loopback contract, not part of the API the SPA is written against."""
    app = create_app()
    paths = app.openapi()["paths"]
    assert HOOK not in paths
    assert not any(p.startswith("/internal") for p in paths)


# ================================================== §5 · the door, for token strings

async def test_a_non_ascii_token_is_a_clean_401_and_never_a_500(
    client, mock_db, monkeypatch, caplog
):
    """⛔ `secrets.compare_digest` on `str` raises TypeError the moment either side is not
    ASCII-only, and the query string is whatever the caller typed. A 500 here is not a cosmetic
    defect: BeamPay's worker reads 5xx as "retry me", and every retry pages the operator's group
    — the exact flood this endpoint exists to end. Both sides are compared as BYTES."""
    with caplog.at_level("WARNING", logger="pgasme.internal"):
        r = await client.post(f"{HOOK}?token=t%C3%BCken", json=DEPOSIT_CONFIRMED)
    assert r.status_code == 401
    assert "wrong or absent token" in caplog.text
    assert "tüken" not in caplog.text  # a refused credential is still a credential
    assert await rows(mock_db) == []
    # …and encoding both sides is not a weakening: the same bytes still match, and any other
    # bytes still do not
    monkeypatch.setattr(settings, "beampay_webhook_token", "tüken")
    ok = await client.post(f"{HOOK}?token=t%C3%BCken", json=DEPOSIT_CONFIRMED)
    assert ok.status_code == 200 and ok.json() == {"ok": True, "stored": True}
    assert (await client.post(f"{HOOK}?token=tuken", json=DEPOSIT_CONFIRMED)).status_code == 401


async def test_health_says_whether_a_delivery_can_be_authorised_at_all(client, monkeypatch):
    """The posture a deploy can assert: with no token the route answers 503 to every delivery,
    and BeamPay's worker pages on every failed attempt. The BOOLEAN only — never the value."""
    body = (await client.get("/v1/health")).json()
    assert body["beampay_webhook"] is True
    assert TOKEN not in json.dumps(body)
    monkeypatch.setattr(settings, "beampay_webhook_token", "")
    assert (await client.get("/v1/health")).json()["beampay_webhook"] is False
