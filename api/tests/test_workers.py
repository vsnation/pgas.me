"""The DLN secondary path, the loop helper (kill switch, survives exceptions), the monitor and
the pool cache. tg.send is muted here (no env) and must return False."""

from __future__ import annotations

import asyncio
import time

import pytest

from pgasme import dln, ethpipe, ledger, tg, workers
from pgasme.config import settings


async def _deposit(mock_db, **over):
    doc = {
        "_id": "dep1",
        "account_id": "acct1",
        "asset": "ETH",
        "status": "submitted",
        "quote_id": "q1",
        "src": {},
        "src_tx_hash": "0x" + "11" * 32,
        "order_id": None,
        "eth": {"value_units": "1000000000000000"},
        "value_groth": 100_000,
        "created_at": time.time() - 300,
        "updated_at": time.time(),
    }
    doc.update(over)
    await mock_db["pgasme_test"].deposits.insert_one(doc)
    return doc


async def test_tg_is_muted_without_env():
    assert await tg.send("hello") is False
    assert await tg.send("again", key="k", cooldown_s=60) is False


async def test_submitted_becomes_order_seen_after_the_grace_period(mock_db, monkeypatch):
    await _deposit(mock_db, created_at=time.time())
    calls = []

    async def ids(h):
        calls.append(h)
        return ["0x" + "77" * 32]

    monkeypatch.setattr(dln, "order_ids_by_tx", ids)
    await workers.dln_secondary()
    assert (
        calls == []
        and (await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"}))["status"]
        == "submitted"
    )
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"}, {"$set": {"created_at": time.time() - 300}}
    )
    await workers.dln_secondary()
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"})
    assert (
        dep["status"] == "order_seen"
        and dep["order_id"] == "0x" + "77" * 32
        and calls == ["0x" + "11" * 32]
    )


async def test_cancelled_order_fails_the_deposit(mock_db, monkeypatch):
    await _deposit(mock_db, status="order_seen", order_id="0xo")

    async def status(oid):
        return {"status": "OrderCancelled", "orderId": oid}

    monkeypatch.setattr(dln, "order_status", status)
    await workers.dln_secondary()
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "failed" and "refunded" in dep["note"]
    assert await mock_db["pgasme_test"].events.find_one({"kind": "deposit_failed"})


async def test_fulfilled_alone_never_credits_and_falls_back_after_the_timer(mock_db, monkeypatch):
    await _deposit(mock_db, status="order_seen", order_id="0xo")

    async def status(oid):
        return {"status": "Fulfilled", "orderId": oid}

    monkeypatch.setattr(dln, "order_status", status)
    await workers.dln_secondary()
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"})
    assert (
        dep["status"] == "order_seen"
        and dep["dln_status"] == "Fulfilled"
        and dep["fulfilled_seen_at"] > 0
    )
    assert await mock_db["pgasme_test"].entries.count_documents({}) == 0
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"}, {"$set": {"fulfilled_seen_at": time.time() - 3600}}
    )
    await workers.dln_secondary()
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "fallback_pending" and "your own wallet" in dep["note"]
    assert await mock_db["pgasme_test"].events.find_one({"kind": "deposit_fallback"})
    assert await ledger.balance("acct1", "ETH") == {"available": 0, "scheduled": 0, "sent": 0}


async def test_created_order_gets_the_unfilled_note_and_one_event_per_six_hours(
    mock_db, monkeypatch
):
    await _deposit(
        mock_db, status="order_seen", order_id="0xo", order_seen_at=time.time() - 10 * 60
    )

    async def status(oid):
        return {"status": "Created", "orderId": oid}

    monkeypatch.setattr(dln, "order_status", status)
    d = mock_db["pgasme_test"]
    await workers.dln_secondary()
    dep = await d.deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "order_seen" and "note" not in dep  # 10 min: too early
    await d.deposits.update_one({"_id": "dep1"}, {"$set": {"order_seen_at": time.time() - 21 * 60}})
    await workers.dln_secondary()
    await workers.dln_secondary()
    dep = await d.deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "order_seen" and dep["note"] == workers.UNFILLED_NOTE
    assert await d.events.count_documents({"kind": "deposit_unfilled"}) == 1  # cooldown holds
    await d.deposits.update_one(
        {"_id": "dep1"}, {"$set": {"unfilled_alert_at": time.time() - 7 * 3600}}
    )
    await workers.dln_secondary()
    assert await d.events.count_documents({"kind": "deposit_unfilled"}) == 2
    assert await d.entries.count_documents({}) == 0


async def test_reconcile_writes_the_missing_credit_once(mock_db):
    await _deposit(mock_db, status="credited", eth={"value_units": "1", "tx": "0xt", "msg_id": 1})
    await workers.reconcile_credits()
    await workers.reconcile_credits()
    assert (
        await mock_db["pgasme_test"].entries.count_documents({"kind": "credit", "ref": "dep1"}) == 1
    )
    assert (await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"}))["ledger_ok"] is True


async def test_credit_failure_hands_the_deposit_back(mock_db, monkeypatch):
    await _deposit(
        mock_db, status="locked", eth={"value_units": "1", "block": 100, "tx": "0xt", "msg_id": 1}
    )

    async def boom(*a, **k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(ledger, "credit", boom)
    await workers.confirm_locked(head=200)
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "confirming" and "retrying" in dep["note"]


async def test_run_forever_pauses_on_the_stop_file_and_survives_exceptions(tmp_path, monkeypatch):
    stop = tmp_path / "pgasme.stop"
    monkeypatch.setattr(settings, "stop_file", str(stop))
    ticks = {"n": 0}

    async def sleep(_s):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(ethpipe, "sleep", sleep)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise RuntimeError("boom")

    stop.write_text("")
    assert workers.paused()
    with pytest.raises(asyncio.CancelledError):
        await workers.run_forever("t", 0, fn)
    assert calls["n"] == 0  # paused: nothing ran
    stop.unlink()
    ticks["n"] = 0
    with pytest.raises(asyncio.CancelledError):
        await workers.run_forever("t", 0, fn)
    assert calls["n"] == 3  # raised three times, kept running


async def test_monitor_drains_events_and_pages_stuck_states(mock_db, monkeypatch):
    sent = []

    async def fake_send(text, *, key=None, cooldown_s=0.0):
        sent.append((text, key))
        return False

    monkeypatch.setattr(tg, "send", fake_send)
    monkeypatch.setattr(dln, "supported_chains", lambda force=False: asyncio.sleep(0))
    await tg.queue(
        "withdrawal_requested", "Withdrawal requested: 1 × direct ETH", request_ids=["r1"]
    )
    d = mock_db["pgasme_test"]
    await d.payout_requests.insert_one(
        {"_id": "r1", "status": "bridging", "updated_at": time.time() - 3 * 3600}
    )
    await d.deposits.insert_one(
        {"_id": "d1", "status": "order_seen", "created_at": time.time() - 2 * 3600}
    )
    await d.deposits.insert_one(
        {"_id": "d2", "status": "confirming", "locked_at": time.time() - 3600}
    )
    await workers.monitor_once()
    texts = [t for t, _ in sent]
    assert any("Withdrawal requested" in t and "<code>r1</code>" in t for t in texts)
    assert any(t.startswith("STUCK: direct payout bridging") for t in texts)
    assert any("STUCK: deposit order_seen" in t for t in texts)
    assert any("STUCK: deposit locked" in t for t in texts)
    ev = await d.events.find_one({"kind": "withdrawal_requested"})
    assert ev["notified"] is True and ev["sent"] is False


async def test_upstream_down_alert_after_five_minutes(monkeypatch):
    sent = []

    async def fake_send(text, *, key=None, cooldown_s=0.0):
        sent.append(text)
        return False

    monkeypatch.setattr(tg, "send", fake_send)
    monkeypatch.setattr(workers, "_started_at", time.time() - 3600)
    monkeypatch.setitem(dln.health, "last_ok_at", time.time() - 10)
    monkeypatch.setitem(workers.pool_health, "last_ok_at", time.time() - 1000)
    monkeypatch.setitem(workers.pool_health, "last_fail_at", time.time())
    monkeypatch.setitem(workers.pool_health, "last_error", "ConnectError")
    await workers.upstream_checks()
    assert len(sent) == 1 and sent[0].startswith("DOWN: beamsmart explorer")


async def test_pool_stats_keep_the_last_value_marked_stale(mock_db, monkeypatch):
    async def ok():
        return {
            "height": 4029416,
            "shielded_outputs_total": 28377,
            "shielded_outputs_per_24h": 29,
            "at": time.time(),
        }

    monkeypatch.setattr(workers, "fetch_pool_status", ok)
    p = await workers.refresh_pool()
    assert p["stale"] is False and p["shielded_outputs_total"] == 28377

    async def down():
        raise RuntimeError("explorer HTTP 502")

    monkeypatch.setattr(workers, "fetch_pool_status", down)
    p = await workers.refresh_pool()
    assert p["stale"] is True and p["shielded_outputs_total"] == 28377 and p["height"] == 4029416
    workers.clear_pool_cache()  # a restart: the last value comes back from Mongo
    p = await workers.refresh_pool()
    assert p["stale"] is True and p["shielded_outputs_total"] == 28377
