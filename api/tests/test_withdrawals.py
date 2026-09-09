"""Every rule of POST /v1/withdrawals (the 2026-09-09 "Scheduling" shape), fees, and cancel.

A withdrawal is a LIST of orders: (address, amount, when it should be delivered). No address is
registered, connected or signed for; the server checks the checksum and the chain, derives the
minimum from the live bridge fee, validates the batch against Available as ONE unit, and writes
one order per item.
"""

from __future__ import annotations

import time

from conftest import fund
from eth_account import Account as EthAccount

from pgasme import ledger, tg
from pgasme.config import settings
from pgasme.routers.withdrawals import clear_fees_cache, release_at_for

ETH = 100_000_000  # groth
ETA = 66 * 60  # PGAS_BRIDGE_ETA_S


def _w() -> str:
    return EthAccount.create().address


def _body(w, amount=1_000_000, mode="direct", deliver_at=None, asset="ETH"):
    item = {"W": w, "amount_groth": amount}
    if deliver_at is not None:
        item["deliver_at"] = deliver_at
    return {"asset": asset, "items": [item], "mode": mode}


async def _ok(client, user, **kw):
    r = await client.post("/v1/withdrawals", json=_body(_w(), **kw), headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ────────────────────────────────────────────────────────────── the address the user types


async def test_any_valid_address_is_accepted_without_registration(client, user, monkeypatch):
    """The whole point of the 2026-09-09 restructure: no proof, no connection, no address book."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    out = await _ok(client, user)
    assert len(out["request_ids"]) == 1
    dests = (await client.get("/v1/destinations", headers=user["headers"])).json()["destinations"]
    assert [d["address"] for d in dests] == [user["address"]]  # nothing was registered


async def test_checksum_cases(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    good = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    bad = "0x5aAeb6053f3E94C9b9A09f33669435E7Ef1BeAed"  # one letter down-cased: checksum breaks
    h = user["headers"]

    r = await client.post("/v1/withdrawals", json=_body(bad), headers=h)
    assert r.status_code == 400 and "checksum" in r.json()["detail"]
    # lowercase carries no checksum information at all: accepted, and stored checksummed
    r = await client.post("/v1/withdrawals", json=_body(good.lower()), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["W"] == good
    r = await client.post("/v1/withdrawals", json=_body(good), headers=h)
    assert r.status_code == 200 and r.json()["items"][0]["W"] == good
    assert (await client.post("/v1/withdrawals", json=_body("not-an-address"), headers=h)).status_code in (400, 422)
    assert (await client.post("/v1/withdrawals", json=_body("0x" + "zz" * 20), headers=h)).status_code == 400


async def test_a_contract_destination_is_refused_and_an_unreadable_chain_is_a_503(
    client, user, monkeypatch, rpc, mock_db
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    h = user["headers"]
    contract, wallet = _w(), _w()
    rpc.code[contract.lower()] = "0x60806040"

    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 400 and "is a contract, not a wallet" in r.json()["detail"]
    # one contract in the list refuses the WHOLE batch, and nothing is written
    body = {
        "asset": "ETH",
        "items": [{"W": wallet, "amount_groth": 1_000_000}, {"W": contract, "amount_groth": 1_000_000}],
        "mode": "direct",
    }
    assert (await client.post("/v1/withdrawals", json=body, headers=h)).status_code == 400
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    # an endpoint that could not answer is NEVER read as "no code, therefore a wallet"
    rpc.unreadable.add(wallet.lower())
    r = await client.post("/v1/withdrawals", json=_body(wallet), headers=h)
    assert r.status_code == 503 and "could not verify the destination" in r.json()["detail"]
    rpc.unreadable.clear()
    rpc.head_dead = True
    r = await client.post("/v1/withdrawals", json=_body(wallet), headers=h)
    assert r.status_code == 503 and "could not verify the destination" in r.json()["detail"]
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0


async def test_the_code_read_is_pinned_to_the_endpoint_that_gave_the_head(client, user, monkeypatch, rpc):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    await _ok(client, user)
    reads = [c for c in rpc.calls if c[0] == "call" and c[1] == "eth_getCode"]
    assert len(reads) == 1
    (_, _, params, prefer, pin) = reads[0]
    assert pin is True and prefer == rpc.PRIMARY and params[1] == hex(rpc.head)


# ─────────────────────────────────────────────────────────────────────── the money arithmetic


async def test_fee_is_two_percent_and_the_user_receives_exactly_the_amount(
    client, user, mock_db, monkeypatch
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w1, w2 = _w(), _w()
    body = {
        "asset": "ETH",
        "items": [{"W": w1, "amount_groth": 1_000_000}, {"W": w2, "amount_groth": 2_500_000}],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["fee_groth"] == 20_000 + 50_000
    assert out["total_debited_groth"] == 3_500_000 + 70_000
    assert [i["amount_groth"] for i in out["items"]] == [1_000_000, 2_500_000]
    rows = await mock_db["pgasme_test"].payout_requests.find({}).to_list(10)
    assert {(r["W"], r["amount_groth"], r["fee_groth"], r["status"]) for r in rows} == {
        (w1, 1_000_000, 20_000, "scheduled"),
        (w2, 2_500_000, 50_000, "scheduled"),
    }
    # the ledger moved exactly amount + fee per item, and Sent is untouched until release
    entries = await mock_db["pgasme_test"].entries.find({"kind": "schedule"}).to_list(10)
    assert sorted(e["groth"] for e in entries) == [1_020_000, 2_550_000]
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - 3_570_000,
        "scheduled": 3_570_000,
        "sent": 0,
    }


async def test_the_minimum_is_derived_from_the_live_relayer_fee(client, user, monkeypatch, rpc):
    """We pay the bridge out of the 2% we charge, so the floor is whatever makes that work.

    The fee is the b2e relayer's OWN arithmetic run on the live gas price (payouts.relayer_fee_for
    → beam.relayer_fee_groth → eth_feeHistory): 120_000 gas × gwei × 1.5 margin."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    h = user["headers"]
    # 1 gwei → 18_000 groth; × 10000/200 = 900_000, under the product floor, which then wins
    fees = (await client.get("/v1/withdrawals/fees", headers=h)).json()
    assert fees == {
        "fee_bps": 200,
        "relayer_fee_groth_now": 18_000,
        "min_amount_groth": 1_000_000,
        "bridge_eta_s": ETA,
    }
    assert (await client.post("/v1/withdrawals", json=_body(_w(), amount=999_999), headers=h)).status_code == 400

    rpc.gas_gwei = 5.0  # 90_000 groth: now the DERIVED minimum is the one that binds
    clear_fees_cache()
    fees = (await client.get("/v1/withdrawals/fees", headers=h)).json()
    assert fees["relayer_fee_groth_now"] == 90_000 and fees["min_amount_groth"] == 4_500_000
    r = await client.post("/v1/withdrawals", json=_body(_w(), amount=4_499_999), headers=h)
    assert r.status_code == 400 and "4500000 groth" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(_w(), amount=4_500_000), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["min_amount_groth"] == 4_500_000
    assert r.json()["relayer_fee_groth_estimate"] == 90_000


async def test_a_fee_we_could_not_read_refuses_rather_than_guesses(client, user, monkeypatch, rpc):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rpc.gas_gwei = None  # eth_feeHistory unreadable
    clear_fees_cache()
    h = user["headers"]
    r = await client.get("/v1/withdrawals/fees", headers=h)
    assert r.status_code == 503 and "live bridge relayer fee" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(_w()), headers=h)
    assert r.status_code == 503
    assert (await client.get("/v1/withdrawals/fees?asset=NOPE", headers=h)).status_code == 400


async def test_the_fee_read_is_cached_for_a_minute(client, user, rpc):
    """Every rendered form asks for the fee; the gas read behind it is one call a minute."""
    h = user["headers"]
    for _ in range(3):
        assert (await client.get("/v1/withdrawals/fees", headers=h)).status_code == 200
    assert len([c for c in rpc.calls if c[0] == "call" and c[1] == "eth_feeHistory"]) == 1
    clear_fees_cache()
    assert (await client.get("/v1/withdrawals/fees", headers=h)).status_code == 200
    assert len([c for c in rpc.calls if c[0] == "call" and c[1] == "eth_feeHistory"]) == 2


async def test_the_batch_is_validated_as_one_unit_with_a_shortfall(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 3_000_000)  # room for two 1 M payouts + fee, not for three
    body = {
        "asset": "ETH",
        "items": [{"W": _w(), "amount_groth": 1_000_000} for _ in range(3)],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 409
    assert r.headers["X-Shortfall-Groth"] == str(3_060_000 - 3_000_000)
    assert "shortfall_groth 60000" in r.json()["detail"]
    d = mock_db["pgasme_test"]
    assert await d.payout_requests.count_documents({}) == 0  # not even the two it could afford
    assert await d.entries.count_documents({"kind": "schedule"}) == 0
    body["items"] = body["items"][:2]
    assert (await client.post("/v1/withdrawals", json=body, headers=user["headers"])).status_code == 200


async def test_too_many_items_is_refused(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "max_items_per_withdrawal", 2)
    await fund(user, "ETH", ETH)
    body = {
        "asset": "ETH",
        "items": [{"W": _w(), "amount_groth": 1_000_000} for _ in range(3)],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 400 and "at most 2 wallets" in r.json()["detail"]


# ────────────────────────────────────────────────────────────────────────── the delivery time


def test_release_at_is_never_in_the_past_and_never_late():
    now = 1_000_000.0
    assert release_at_for(None, now, ETA) == now  # "asap"
    assert release_at_for(now - 3600, now, ETA) == now  # a delivery time already gone
    assert release_at_for(now + 10, now, ETA) == now  # sooner than the bridge can manage
    assert release_at_for(now + 3 * 3600, now, ETA) == now + 3 * 3600 - ETA  # one ETA early


async def test_release_at_math_end_to_end(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    t0 = time.time()
    future = t0 + 6 * 3600
    body = {
        "asset": "ETH",
        "items": [
            {"W": _w(), "amount_groth": 1_000_000, "deliver_at": future},
            {"W": _w(), "amount_groth": 1_000_000, "deliver_at": t0 - 7200},  # already past
            {"W": _w(), "amount_groth": 1_000_000},  # absent → asap
        ],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert abs(items[0]["release_at"] - (future - ETA)) < 1
    assert items[0]["deliver_at"] == future
    assert t0 <= items[1]["release_at"] <= time.time() and items[1]["deliver_at"] == t0 - 7200
    assert t0 <= items[2]["release_at"] <= time.time() and items[2]["deliver_at"] is None
    rows = {r["_id"]: r for r in await mock_db["pgasme_test"].payout_requests.find({}).to_list(10)}
    for i in items:
        row = rows[i["request_id"]]
        assert row["release_at"] == i["release_at"] and row["deliver_at"] == i["deliver_at"]
        assert row["relayer_fee_groth_estimate"] == 18_000


async def test_a_delivery_further_out_than_the_window_is_refused(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post(
        "/v1/withdrawals",
        json=_body(_w(), deliver_at=time.time() + 31 * 86400),
        headers=user["headers"],
    )
    assert r.status_code == 400 and "30 days away" in r.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────── modes and events


async def test_disabled_mode_is_409_with_a_reason(client, user):
    r = await client.post("/v1/withdrawals", json=_body(_w()), headers=user["headers"])
    assert r.status_code == 409 and "not enabled" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(_w(), mode="instant"), headers=user["headers"])
    assert r.status_code == 409 and "instant" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(_w(), mode="teleport"), headers=user["headers"])
    assert r.status_code == 400


async def test_instant_requires_a_denomination_multiple(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_instant_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post(
        "/v1/withdrawals", json=_body(_w(), amount=1_500_000, mode="instant"), headers=user["headers"]
    )
    assert r.status_code == 400 and "denomination" in r.json()["detail"]
    r = await client.post(
        "/v1/withdrawals", json=_body(_w(), amount=3_000_000, mode="instant"), headers=user["headers"]
    )
    assert r.status_code == 200


async def test_one_event_per_order_carrying_ids_only(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w1, w2 = _w(), _w()
    body = {
        "asset": "ETH",
        "items": [{"W": w1, "amount_groth": 1_000_000}, {"W": w2, "amount_groth": 1_000_000}],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    ids = r.json()["request_ids"]
    evs = await mock_db["pgasme_test"].events.find({"kind": "withdrawal_requested"}).to_list(10)
    assert len(evs) == 2 and sorted(e["request_id"] for e in evs) == sorted(ids)
    for e in evs:
        assert w1.lower() not in e["text"].lower() and w2.lower() not in e["text"].lower()
        assert e["notified"] is False and "request_ids" not in e
        assert tg.format_event(e).endswith(f"<code>{e['request_id']}</code>")


# ───────────────────────────────────────────────────────────────────────────────────── cancel


async def test_cancel_returns_the_money_and_is_final(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = (await _ok(client, user))["request_ids"][0]
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    # the refund is the groth of the schedule entry that actually debited the account
    assert r.status_code == 200 and r.json() == {"cancelled": rid, "refunded_groth": 1_020_000}
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    assert (await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid}))["status"] == "cancelled"
    assert (await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])).status_code == 409
    assert (await client.post("/v1/withdrawals/nope/cancel", headers=user["headers"])).status_code == 404


async def test_cancel_only_by_the_owner(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = (await _ok(client, user))["request_ids"][0]
    from conftest import sign_in

    other = await sign_in(client, EthAccount.create())
    assert (
        await client.post(f"/v1/withdrawals/{rid}/cancel", headers=other["headers"])
    ).status_code == 404
