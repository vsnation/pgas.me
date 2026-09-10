"""Per-asset ledger sums and the account view."""

from __future__ import annotations

import asyncio

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


async def test_the_schedule_debit_is_two_halves_that_reconcile():
    """2026-09-10: a payout debits amount + OUR fee under `schedule` and the bridge fee it funds
    under `schedule_bridge_fee`, so an audit can tell revenue from pass-through cost. The sum is
    what `debited_groth` reports and what every refund path gives back."""
    await ledger.credit("a", "ETH", 1_000_000, "d1")
    await ledger.schedule("a", "ETH", 510_000, "r1", "test", bridge_fee_groth=3_600)
    assert await ledger.debited_groth("r1") == 513_600
    assert (await ledger.find_entry("schedule", "r1"))["groth"] == 510_000
    assert (await ledger.find_entry("schedule_bridge_fee", "r1"))["groth"] == 3_600
    assert await ledger.balance("a", "ETH") == {
        "available": 486_400,
        "scheduled": 513_600,
        "sent": 0,
    }
    # amount + fee + bridge_fee == what was debited
    assert 500_000 + 10_000 + 3_600 == await ledger.debited_groth("r1")
    # …and the one `cancel` that mirrors both halves puts all of it back
    await ledger.cancel("a", "ETH", await ledger.debited_groth("r1"), "r1", refund_of="schedule:r1")
    assert await ledger.balance("a", "ETH") == {"available": 1_000_000, "scheduled": 0, "sent": 0}


async def test_debited_groth_is_none_when_nothing_was_debited():
    """A refund needs the debit it reverses: `None` is "nothing was ever taken", and refunding
    against that would MINT money. A bridge-fee half with no `schedule` half cannot exist —
    `schedule` writes that one first, on purpose."""
    assert await ledger.debited_groth("never-scheduled") is None
    await ledger.credit("a", "ETH", 1_000, "d1")
    await ledger.schedule("a", "ETH", 1_020, "r1")  # a legacy-shaped debit: no bridge fee
    assert await ledger.debited_groth("r1") == 1_020


async def test_two_concurrent_cancels_refund_exactly_once(mock_db, monkeypatch):
    """T30c/F3: `cancel`'s already-refunded guard was a READ-THEN-WRITE with nothing behind it.

    `credit`, `release`, `fee` and `bridge_fee` each have a unique partial index precisely
    because a guard that reads and then writes is not a guard when two writers are in flight —
    and `cancel` is the one entry that GIVES MONEY BACK. Two paths race for it in normal
    operation: the user's `POST /{id}/cancel`, `_void`, `_roll_back` and the payout worker's own
    refund all call it, and both readers seeing "no cancel yet" refunds the order twice, which
    mints the whole debit a second time into Available. The DATABASE decides now: one cancel per
    (account, ref), and the loser is an `AlreadyRefunded`, which every caller already treats as
    "the money is back" rather than as a failure."""
    await ledger.ensure_indexes()
    await ledger.credit("a", "ETH", 1_000_000, "d1")
    await ledger.schedule("a", "ETH", 510_000, "r1", "test", bridge_fee_groth=3_600)
    total = await ledger.debited_groth("r1")
    assert total == 513_600

    real = ledger.find_entry

    async def slow(kind: str, ref: str):
        seen = await real(kind, ref)
        if kind == "cancel":
            await asyncio.sleep(0.02)  # the answer is already in hand when the other one asks:
        return seen  # both refunds have READ "no cancel yet" before either of them writes

    monkeypatch.setattr(ledger, "find_entry", slow)
    out = await asyncio.gather(
        *(
            ledger.cancel("a", "ETH", total, "r1", "raced", refund_of="schedule:r1")
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    monkeypatch.setattr(ledger, "find_entry", real)
    assert sum(isinstance(o, ledger.AlreadyRefunded) for o in out) == 1, out
    assert not [o for o in out if isinstance(o, Exception) and not isinstance(o, ledger.AlreadyRefunded)]
    assert await mock_db["pgasme_test"].entries.count_documents({"kind": "cancel", "ref": "r1"}) == 1
    # the debit is back exactly once — not 1_513_600
    assert await ledger.balance("a", "ETH") == {"available": 1_000_000, "scheduled": 0, "sent": 0}


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
