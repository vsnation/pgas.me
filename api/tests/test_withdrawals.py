"""Every rule of POST /v1/withdrawals, the happy path, and cancel."""

from __future__ import annotations

import time

from conftest import add_destination, fund
from eth_account import Account as EthAccount

from pgasme import ledger
from pgasme.config import settings
from pgasme.routers.withdrawals import privacy_grade

ETH = 100_000_000  # groth


async def _dest(client, user):
    dest = EthAccount.create()
    assert (await add_destination(client, user, dest)).status_code == 200
    return dest.address


def _body(w, amount=1_000_000, mode="direct", window_s=600, asset="ETH"):
    return {
        "asset": asset,
        "items": [{"W": w, "amount_groth": amount}],
        "mode": mode,
        "window_s": window_s,
    }


async def test_disabled_mode_is_409_with_a_reason(client, user):
    w = await _dest(client, user)
    r = await client.post("/v1/withdrawals", json=_body(w), headers=user["headers"])
    assert r.status_code == 409 and "not enabled" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(w, mode="instant"), headers=user["headers"])
    assert r.status_code == 409 and "instant" in r.json()["detail"]


async def test_unregistered_destination_is_refused(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post(
        "/v1/withdrawals", json=_body(EthAccount.create().address), headers=user["headers"]
    )
    assert r.status_code == 400 and "registered destination" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body("not-an-address"), headers=user["headers"])
    assert r.status_code == 400


async def test_amount_below_minimum_is_refused(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w = await _dest(client, user)
    r = await client.post("/v1/withdrawals", json=_body(w, amount=999_999), headers=user["headers"])
    assert r.status_code == 400 and "at least" in r.json()["detail"]


async def test_instant_requires_a_denomination_multiple(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_instant_enabled", True)
    await fund(user, "ETH", ETH)
    w = await _dest(client, user)
    r = await client.post(
        "/v1/withdrawals", json=_body(w, amount=1_500_000, mode="instant"), headers=user["headers"]
    )
    assert r.status_code == 400 and "denomination" in r.json()["detail"]
    r = await client.post(
        "/v1/withdrawals", json=_body(w, amount=3_000_000, mode="instant"), headers=user["headers"]
    )
    assert r.status_code == 200


async def test_window_bounds(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w = await _dest(client, user)
    r = await client.post(
        "/v1/withdrawals", json=_body(w, window_s=30 * 86400 + 1), headers=user["headers"]
    )
    assert r.status_code == 400 and "window_s" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(w, window_s=-1), headers=user["headers"])
    assert r.status_code == 400
    r = await client.post("/v1/withdrawals", json=_body(w, window_s=0), headers=user["headers"])
    assert r.status_code == 200


async def test_available_must_cover_amount_plus_fee(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    w = await _dest(client, user)
    await fund(user, "ETH", 1_000_000)  # exactly the amount, not the 2%
    r = await client.post("/v1/withdrawals", json=_body(w), headers=user["headers"])
    assert r.status_code == 409 and "insufficient" in r.json()["detail"]
    await fund(user, "ETH", 20_000)
    r = await client.post("/v1/withdrawals", json=_body(w), headers=user["headers"])
    assert r.status_code == 200
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": 0,
        "scheduled": 1_020_000,
        "sent": 0,
    }


async def test_happy_path_one_row_and_one_entry_per_wallet(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w1, w2 = await _dest(client, user), await _dest(client, user)
    body = {
        "asset": "ETH",
        "items": [{"W": w1, "amount_groth": 1_000_000}, {"W": w2, "amount_groth": 2_000_000}],
        "mode": "direct",
        "window_s": 3600,
    }
    t0 = time.time()
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert (
        len(out["request_ids"]) == 2
        and out["fee_groth"] == 60_000
        and out["total_debited_groth"] == 3_060_000
    )
    assert out["privacy_grade"] == "weak" and out["eta"]["min_s"] < out["eta"]["max_s"]
    rows = (
        await mock_db["pgasme_test"]
        .payout_requests.find({"account_id": user["account_id"]})
        .to_list(10)
    )
    assert {(r["W"], r["amount_groth"], r["fee_groth"], r["status"], r["mode"]) for r in rows} == {
        (w1, 1_000_000, 20_000, "scheduled", "direct"),
        (w2, 2_000_000, 40_000, "scheduled", "direct"),
    }
    assert all(t0 <= r["release_at"] <= t0 + 3600 + 1 for r in rows)
    entries = await mock_db["pgasme_test"].entries.find({"kind": "schedule"}).to_list(10)
    assert sorted(e["groth"] for e in entries) == [1_020_000, 2_040_000]
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - 3_060_000,
        "scheduled": 3_060_000,
        "sent": 0,
    }
    acct = (await client.get("/v1/account", headers=user["headers"])).json()
    assert len(acct["requests"]) == 2


async def test_cancel_returns_the_money_and_is_final(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w = await _dest(client, user)
    rid = (await client.post("/v1/withdrawals", json=_body(w), headers=user["headers"])).json()[
        "request_ids"
    ][0]
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert r.status_code == 200 and r.json() == {"cancelled": rid}
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    assert (await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid}))[
        "status"
    ] == "cancelled"
    assert (
        await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    ).status_code == 409
    assert (
        await client.post("/v1/withdrawals/nope/cancel", headers=user["headers"])
    ).status_code == 404


async def test_cancel_only_by_the_owner(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w = await _dest(client, user)
    rid = (await client.post("/v1/withdrawals", json=_body(w), headers=user["headers"])).json()[
        "request_ids"
    ][0]
    from conftest import sign_in

    other = await sign_in(client, EthAccount.create())
    assert (
        await client.post(f"/v1/withdrawals/{rid}/cancel", headers=other["headers"])
    ).status_code == 404


def test_privacy_grade_never_overstates():
    assert privacy_grade(3, 86400) == "weak" and privacy_grade(100, 30) == "weak"
    assert privacy_grade(10, 600) == "ok" and privacy_grade(49, 86400) == "ok"
    assert privacy_grade(50, 600) == "good"
