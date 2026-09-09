"""The monitoring audit: every deposit and payout state transition emits exactly ONE operator
event, ids only and never an address, and the things the operator cannot learn a minute late
(mismatches, unattributed locks, hook fallbacks, roll-backs) are SENT at once instead of queued.

The table this file enforces:

  Deposit   submitted        deposit_submitted     routers/deposits.create            queued
            order_seen       deposit_order_seen    workers._step_submitted            queued
            locked           deposit_locked        scanner.handle_log / retry         queued
            confirming       deposit_confirming    workers._step_confirm (once)       queued
            credited         deposit_credited      workers._credit / reconcile        queued
        failed (router)      deposit_failed        workers._step_order_seen           queued
            failed (hash)    deposit_mismatch      workers._mismatch / registration   IMMEDIATE
            fallback_pending deposit_fallback      workers._step_order_seen           IMMEDIATE
            (unattributed)   lock_unattributed     scanner.record_unattributed        IMMEDIATE
  Payout    scheduled        withdrawal_requested  routers/withdrawals.create         queued
            cancelled        withdrawal_cancelled  routers/withdrawals.cancel         queued
            cancelled (bad)  withdrawal_cancel_no_debit                               IMMEDIATE
"""

from __future__ import annotations

import copy
import time
from typing import Any

import pytest
from conftest import PUBKEY, USDC_ARB, XCHAIN_ESTIMATE, fulfilled_log, fund, lock_log
from eth_account import Account as EthAccount

from pgasme import ledger, scanner, tg, workers, xchain
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
ORDER = "0x" + "77" * 32
TX = "0x" + "aa" * 32
Q = {"src_chain_id": 42161, "src_token": USDC_ARB, "amount": "10000000", "target_asset": "ETH"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(settings, "min_deposit_wei", 10**15)
    monkeypatch.setattr(scanner, "LITE_RETRY_SLEEP_S", 0.0)

    async def create_tx(params: dict[str, Any]) -> dict[str, Any]:
        body = copy.deepcopy(XCHAIN_ESTIMATE)
        if params["dstChainTokenOutAmount"] != "auto":
            out = body["estimation"]["dstChainTokenOut"]
            out["amount"] = out["recommendedAmount"] = params["dstChainTokenOutAmount"]
            body["orderId"] = ORDER
        return body

    async def no_ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return []

    monkeypatch.setattr(xchain, "create_tx", create_tx)
    monkeypatch.setattr(xchain, "order_ids_by_tx", no_ids)


async def kinds(mock_db) -> list[str]:
    rows = await mock_db["pgasme_test"].events.find({}).sort("at", 1).to_list(100)
    return [r["kind"] for r in rows]


async def test_a_deposit_emits_exactly_one_event_per_status(
    client, user, armed_eth, mock_db, rpc, monkeypatch
):
    d = mock_db["pgasme_test"]
    h = user["headers"]
    quote = (await client.post("/v1/quote", json=Q, headers=h)).json()
    assert quote["armed"] is True
    armed = (await client.post(f"/v1/quote/{quote['quote_id']}/arm", headers=h)).json()
    quote = {**quote, **armed}

    # ── submitted
    r = await client.post(
        "/v1/deposits", json={"quote_id": quote["quote_id"], "src_tx_hash": TX}, headers=h
    )
    dep_id = r.json()["deposit_id"]
    assert await kinds(mock_db) == ["deposit_submitted"]

    # ── order_seen (the quote's own order id, seen by the router)
    async def ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return [ORDER]

    monkeypatch.setattr(xchain, "order_ids_by_tx", ids)
    await d.deposits.update_one({"_id": dep_id}, {"$set": {"created_at": time.time() - 3600}})
    await workers.xchain_secondary()
    await workers.xchain_secondary()  # a second pass must not repeat the event
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "order_seen"
    assert await kinds(mock_db) == ["deposit_submitted", "deposit_order_seen"]

    # ── locked (the chain, the only evidence that moves money)
    value = int(quote["estimate"]["value_units"])
    fee = int(quote["estimate"]["relayer_fee_units"])
    rpc.logs_.append(lock_log(ETH.pipe, 222, value, fee, PUBKEY, 1500, TX, 0))
    rpc.receipts[TX] = {
        "from": user["address"],
        "logs": [fulfilled_log(ORDER, value + fee, "0x" + "ab" * 20, TX, 1), rpc.logs_[0]],
    }
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 1
    await scanner.scan_pipe(ETH, rpc)  # deduped: no second event
    assert (await kinds(mock_db))[-1] == "deposit_locked"

    # ── confirming (one event on the transition, not one per block)
    await workers.confirm_locked(head=1505)
    await workers.confirm_locked(head=1506)
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "confirming"
    assert await d.events.count_documents({"kind": "deposit_confirming"}) == 1

    # ── credited (and the reconciler cannot repeat it)
    await workers.confirm_locked(head=1600)
    await workers.reconcile_credits()
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "credited"
    assert await ledger.has_credit(dep_id)
    assert await kinds(mock_db) == [
        "deposit_submitted",
        "deposit_order_seen",
        "deposit_locked",
        "deposit_confirming",
        "deposit_credited",
    ]
    assert all(r["deposit_id"] == dep_id for r in await d.events.find({}).to_list(20))
    # ids only: no address, no pubkey, ever
    for row in await d.events.find({}).to_list(20):
        assert user["address"].lower() not in row["text"].lower()
        assert PUBKEY not in row["text"] and ETH.pipe.lower() not in row["text"].lower()


async def test_the_credited_event_is_written_once_even_if_the_first_path_crashed(mock_db):
    """A crash between the ledger entry and the event used to leave a credited deposit with no
    notification at all; the reconciler fills that gap and never doubles it."""
    d = mock_db["pgasme_test"]
    await ledger.ensure_indexes()
    await d.deposits.insert_one(
        {
            "_id": "dep1",
            "account_id": "acct1",
            "asset": "ETH",
            "status": "credited",
            "src": {},
            "eth": {"value_units": "1", "tx": "0xt", "msg_id": 1},
            "value_groth": 700,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    )
    await ledger.credit("acct1", "ETH", 700, "dep1")  # the entry landed, the event did not
    await workers.reconcile_credits()
    await workers.reconcile_credits()
    assert await d.events.count_documents({"kind": "deposit_credited"}) == 1
    assert await d.entries.count_documents({"kind": "credit"}) == 1


async def test_a_cancelled_order_and_a_hook_fallback_each_emit_one_event(mock_db, monkeypatch):
    d = mock_db["pgasme_test"]
    base = {
        "account_id": "acct1",
        "asset": "ETH",
        "mode": "xchain",
        "status": "order_seen",
        "src": {},
        "src_tx_hash": TX,
        "order_id": ORDER,
        "eth": {"value_units": "1000000000000000"},
        "value_groth": 100_000,
        "created_at": time.time() - 3600,
        "updated_at": time.time(),
    }
    await d.deposits.insert_one({**base, "_id": "cancelled1"})
    await d.deposits.insert_one(
        {**base, "_id": "fallback1", "src_tx_hash": None, "fulfilled_seen_at": time.time() - 7200}
    )

    async def status(oid: str) -> dict[str, str]:
        return {"status": "OrderCancelled" if oid == ORDER else "Fulfilled"}

    async def status_ok(oid: str) -> dict[str, str]:
        return {"status": "Fulfilled"}

    monkeypatch.setattr(xchain, "order_status", status)
    await d.deposits.delete_one({"_id": "fallback1"})
    await workers.xchain_secondary()
    assert (await d.deposits.find_one({"_id": "cancelled1"}))["status"] == "failed"
    ev = await d.events.find_one({"kind": "deposit_failed"})
    assert ev["deposit_id"] == "cancelled1" and ev["notified"] is False  # queued is fine here

    await d.deposits.delete_many({})
    await d.events.delete_many({})
    monkeypatch.setattr(xchain, "order_status", status_ok)
    await d.deposits.insert_one(
        {**base, "_id": "fallback1", "fulfilled_seen_at": time.time() - 7200}
    )
    await workers.xchain_secondary()
    assert (await d.deposits.find_one({"_id": "fallback1"}))["status"] == "fallback_pending"
    ev = await d.events.find_one({"kind": "deposit_fallback"})
    # the user's money is in their own wallet: the operator hears NOW, not on the next minute
    assert ev["notified"] is True and ev["immediate"] is True and ev["sent"] is False


async def test_an_unattributed_lock_alerts_immediately_and_once(mock_db, rpc, monkeypatch):
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    rpc.logs_.append(lock_log(ETH.pipe, 5, 10**15, 10**11, PUBKEY, 1200, TX, 4))
    rpc.receipts[TX] = {"logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 1
    d = mock_db["pgasme_test"]
    ev = await d.events.find_one({"kind": "lock_unattributed"})
    assert ev["notified"] is True and ev["immediate"] is True and ev["lock"] == f"{TX}:4"
    await scanner.scan_pipe(ETH, rpc)
    assert await d.events.count_documents({"kind": "lock_unattributed"}) == 1


async def test_a_withdrawal_emits_one_event_per_transition(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 100_000_000)
    dest = EthAccount.create()
    r = await client.post(
        "/v1/withdrawals",
        json={
            "asset": "ETH",
            "items": [{"W": dest.address, "amount_groth": 1_000_000}],
            "mode": "direct",
        },
        headers=user["headers"],
    )
    rid = r.json()["request_ids"][0]
    d = mock_db["pgasme_test"]
    ev = await d.events.find_one({"kind": "withdrawal_requested"})
    # ONE event per ORDER (2026-09-09: a withdrawal is a list of them), ids only
    assert ev["request_id"] == rid and dest.address.lower() not in ev["text"].lower()
    assert await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert await kinds(mock_db) == ["withdrawal_requested", "withdrawal_cancelled"]
    ev = await d.events.find_one({"kind": "withdrawal_cancelled"})
    assert ev["request_id"] == rid


async def test_a_due_payout_that_nothing_executed_is_paged(mock_db, monkeypatch):
    sent: list[tuple[str, str | None]] = []

    async def fake_send(text, *, key=None, cooldown_s=0.0):
        sent.append((text, key))
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {
            "_id": "r1",
            "status": "scheduled",
            "release_at": time.time() - 3600,
            "updated_at": time.time(),
        }
    )
    await workers.stuck_checks()
    assert any(t.startswith("STUCK: payout due") and "<code>r1</code>" in t for t, _ in sent)


async def test_every_worker_failure_reaches_the_operator_at_once(mock_db, monkeypatch):
    """A pass that raised, a pipe that cannot be scanned and a cross-chain step that blew up are sent
    immediately (with a cooldown), never queued behind the monitor."""
    sent: list[str] = []

    async def fake_send(text, *, key=None, cooldown_s=0.0):
        sent.append(text)
        return True

    monkeypatch.setattr(tg, "send", fake_send)

    async def boom():
        raise RuntimeError("upstream exploded")

    async def scan_boom(asset, rpc, max_chunks=40):
        raise RuntimeError("no endpoint answered")

    monkeypatch.setattr(scanner, "scan_pipe", scan_boom)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    monkeypatch.setattr(workers, "get_rpc", lambda: None)

    async def nothing(*a, **k):
        return 0

    monkeypatch.setattr(scanner, "retry_unattributed", nothing)
    monkeypatch.setattr(workers, "confirm_locked", nothing)
    await workers.deposit_watcher_once()
    assert any("Pipe scan failed for ETH" in t for t in sent)

    sent.clear()
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "dep1",
            "account_id": "a",
            "asset": "ETH",
            "mode": "xchain",
            "status": "order_seen",
            "order_id": ORDER,
            "src": {},
            "eth": {},
            "value_groth": 1,
            "created_at": 0.0,
            "updated_at": 0.0,
        }
    )
    monkeypatch.setattr(xchain, "order_status", lambda oid: boom())
    await workers.xchain_secondary()
    assert any("Cross-chain step failed on <code>dep1</code>" in t for t in sent)
