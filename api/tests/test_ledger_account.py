"""Per-asset ledger sums and the account view."""

from __future__ import annotations

import pytest
from conftest import fund

from pgasme import ledger
from pgasme.db import ensure_indexes


async def test_indexes_build_on_the_mock():
    await ensure_indexes()


async def test_balances_are_per_asset_sums():
    await ledger.credit("a", "ETH", 100, "d1")
    await ledger.credit("a", "ETH", 50, "d2")
    await ledger.credit("a", "DAI", 7, "d3")
    await ledger.schedule("a", "ETH", 30, "r1")
    b = await ledger.balances("a")
    assert b["ETH"] == {"available": 120, "scheduled": 30, "sent": 0}
    assert b["DAI"] == {"available": 7, "scheduled": 0, "sent": 0}
    assert b["WBTC"] == {"available": 0, "scheduled": 0, "sent": 0}
    assert (await ledger.balances("nobody"))["ETH"] == {"available": 0, "scheduled": 0, "sent": 0}


async def test_release_and_cancel_maths():
    await ledger.credit("a", "WBTC", 1000, "d")
    await ledger.schedule("a", "WBTC", 102, "r1")
    await ledger.release("a", "WBTC", 100, 2, "r1")
    assert await ledger.balance("a", "WBTC") == {"available": 898, "scheduled": 0, "sent": 100}
    await ledger.schedule("a", "WBTC", 204, "r2")
    await ledger.cancel("a", "WBTC", 204, "r2")
    assert await ledger.balance("a", "WBTC") == {"available": 898, "scheduled": 0, "sent": 100}


async def test_unknown_asset_and_kind_are_refused():
    with pytest.raises(ValueError):
        await ledger.credit("a", "USDT", 1, "x")
    with pytest.raises(ValueError):
        await ledger._append("a", "ETH", "bogus", 1, 1, 0, 0, "x")


async def test_has_credit_and_history():
    assert not await ledger.has_credit("dep1")
    await ledger.credit("a", "ETH", 5, "dep1")
    assert await ledger.has_credit("dep1")
    rows = await ledger.history("a")
    assert rows[0]["asset"] == "ETH" and rows[0]["kind"] == "credit" and "_id" not in rows[0]


async def test_account_shows_balances_and_pending_per_asset(client, user, mock_db):
    await fund(user, "ETH", 5_000_000)
    await fund(user, "DAI", 900)
    d = mock_db["pgasme_test"].deposits
    base = {
        "account_id": user["account_id"],
        "created_at": 1.0,
        "updated_at": 1.0,
        "src": {},
        "eth": {},
    }
    await d.insert_one(
        {**base, "_id": "p1", "asset": "ETH", "status": "locked", "value_groth": 100}
    )
    await d.insert_one(
        {**base, "_id": "p2", "asset": "ETH", "status": "credited", "value_groth": 999}
    )
    await d.insert_one(
        {**base, "_id": "p3", "asset": "DAI", "status": "fallback_pending", "value_groth": 55}
    )
    body = (await client.get("/v1/account", headers=user["headers"])).json()
    assert body["balances"]["ETH"] == {
        "available": 5_000_000,
        "scheduled": 0,
        "sent": 0,
        "pending": 100,
    }
    assert body["balances"]["DAI"]["available"] == 900 and body["balances"]["DAI"]["pending"] == 0
    assert body["fee_bps"] == 200 and body["denominations"] == [1_000_000, 10_000_000]
    assert body["modes"] == {"direct": False, "instant": False}
    assert body["ingress"]["armed"] is False and len(body["deposits"]) == 3
    assert all("account_id" not in dep for dep in body["deposits"])


async def test_dev_credit_is_mounted_only_with_the_flag(client, user, monkeypatch):
    r = await client.post(
        "/v1/dev/credit", json={"asset": "wbtc", "groth": 42}, headers=user["headers"]
    )
    assert r.status_code == 200 and r.json()["balances"]["WBTC"]["available"] == 42
    import httpx

    from pgasme.config import settings
    from pgasme.main import create_app

    monkeypatch.setattr(settings, "dev_endpoints", False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://t"
    ) as c:
        assert (
            await c.post(
                "/v1/dev/credit", json={"asset": "ETH", "groth": 1}, headers=user["headers"]
            )
        ).status_code == 404
