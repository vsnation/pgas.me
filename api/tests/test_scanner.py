"""The on-chain scanner: checkpoints, the stalled-node guard, FulfilledOrder attribution, the
metadata-tag fallback, and the unattributed path (never a credit)."""

from __future__ import annotations

import time

import pytest
from conftest import DLN_ESTIMATE, PUBKEY, FakeRpc, fulfilled_log, lock_log

from pgasme import dln, ledger, scanner, workers
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
ORDER = "0x" + "77" * 32
TX = "0x" + "aa" * 32
VALUE = 3_774_700_000_000_000


@pytest.fixture
def our_key(monkeypatch):
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    monkeypatch.setattr(settings, "lock_scan_chunk", 500)
    monkeypatch.setattr(settings, "lock_scan_blocks", 2000)
    monkeypatch.setattr(scanner, "LITE_RETRY_SLEEP_S", 0.0)
    return PUBKEY


async def _deposit(mock_db, **over):
    doc = {
        "_id": "dep1",
        "account_id": "acct1",
        "asset": "ETH",
        "status": "submitted",
        "quote_id": "q1",
        "src": {"chain_id": 42161, "token": "0x0", "amount": "10000000"},
        "src_tx_hash": "0x" + "11" * 32,
        "order_id": ORDER,
        "eth": {"value_units": str(VALUE), "relayer_fee_units": "112168855201"},
        "value_groth": VALUE // 10**10,
        "metadata": "0x0102030405",
        "pubkey": PUBKEY,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    doc.update(over)
    await mock_db["pgasme_test"].deposits.insert_one(doc)
    return doc


def test_fulfilled_order_topic_matches_the_given_one():
    assert scanner.FULFILLED_TOPIC == scanner.FULFILLED_TOPIC_EXPECTED


def test_decode_fulfilled_order_from_a_fabricated_log():
    lg = fulfilled_log(ORDER, VALUE, "0x" + "ab" * 20, TX, 3)
    f = scanner.decode_fulfilled_order(lg)
    assert (
        f["order_id"] == ORDER and f["take_amount"] == VALUE and f["actual_fulfill_amount"] == VALUE
    )
    assert (
        f["take_chain_id"] == 1
        and f["give_chain_id"] == 42161
        and f["receiver_dst"] == "0x" + "ab" * 20
    )
    assert f["has_external_call"] is True and f["tx"] == TX and f["log_index"] == 3
    assert (
        scanner.find_fulfilled_in_receipt({"logs": [lg, {"topics": ["0x00"], "data": "0x"}]})[0][
            "order_id"
        ]
        == ORDER
    )


def test_metadata_tag_is_bytes_45_to_50():
    assert scanner.metadata_tag(DLN_ESTIMATE["order"]["metadata"]) == "0x0102030405"
    assert scanner.metadata_tag("0x" + "00" * 66) == scanner.ZERO_TAG
    assert scanner.metadata_tag("0x0102") is None and scanner.metadata_tag(None) is None
    assert scanner.metadata_tag("zz") is None


async def test_checkpoint_never_advances_past_a_failed_chunk(mock_db, our_key):
    rpc = FakeRpc(head=2000)
    rpc.fail_ranges.add((1000, 1499))
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["from"] == 0 and st["chunks"] == 2 and st["failed"]["from"] == 1000
    assert (await mock_db["pgasme_test"].scanner_state.find_one({"_id": ETH.pipe}))[
        "last_block"
    ] == 999
    rpc.fail_ranges.clear()
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["from"] == 1000 and st["failed"] is None and st["chunks"] == 3
    assert (await mock_db["pgasme_test"].scanner_state.find_one({"_id": ETH.pipe}))[
        "last_block"
    ] == 2000
    st = await scanner.scan_pipe(ETH, rpc)  # nothing new
    assert st["chunks"] == 0 and st["checkpoint"] == 2000


async def test_a_pass_is_bounded_so_one_pipe_cannot_stall_the_others(mock_db, our_key):
    rpc = FakeRpc(head=100_000)
    st = await scanner.scan_pipe(ETH, rpc, max_chunks=3)
    assert st["chunks"] == 3
    assert (await mock_db["pgasme_test"].scanner_state.find_one({"_id": ETH.pipe}))[
        "last_block"
    ] == 98_000 + 1499


async def test_stalled_primary_is_overridden_by_the_pool_head(mock_db, our_key):
    await mock_db["pgasme_test"].scanner_state.insert_one({"_id": ETH.pipe, "last_block": 5000})
    rpc = FakeRpc(head=5000)  # "no new blocks"
    rpc.heads = {"https://a": 5000, "https://b": 5020}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["chunks"] == 0 and rpc.calls == []  # 20 ahead is within tolerance
    rpc.heads["https://b"] = 5100
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["head"] == 5100 and st["prefer"] == "https://b" and st["chunks"] == 1
    assert rpc.calls[0] == ("logs", ETH.pipe, 5001, 5100, "https://b")
    assert (await mock_db["pgasme_test"].scanner_state.find_one({"_id": ETH.pipe}))[
        "last_block"
    ] == 5100


async def test_no_pubkey_means_no_scan(mock_db):
    assert "skipped" in await scanner.scan_pipe(ASSETS["WBTC"], FakeRpc(head=10))


async def test_lock_attributed_by_order_id_then_credited_exactly_once(mock_db, our_key, rpc):
    await _deposit(mock_db)
    rpc.logs_.append(lock_log(ETH.pipe, 222, VALUE, 112_168_855_201, PUBKEY, 1500, TX, 0))
    rpc.receipts[TX] = {
        "logs": [
            fulfilled_log(ORDER, VALUE + 112_168_855_201, "0x" + "ab" * 20, TX, 1),
            rpc.logs_[0],
        ]
    }
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 1 and st["unattributed"] == 0
    d = mock_db["pgasme_test"]
    dep = await d.deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "locked" and dep["eth"]["tx"] == TX and dep["eth"]["block"] == 1500
    assert dep["eth"]["msg_id"] == 222 and dep["eth"]["log_index"] == 0 and dep["order_id"] == ORDER
    assert (await d.events.find_one({"kind": "deposit_locked"}))["deposit_id"] == "dep1"
    # confirmations: 11 on top → confirming, 12 → credited
    await workers.confirm_locked(head=1510)
    dep = await d.deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "confirming" and dep["confirmations"] == 11
    assert not await ledger.has_credit("dep1")
    await workers.confirm_locked(head=1511)
    dep = await d.deposits.find_one({"_id": "dep1"})
    assert dep["status"] == "credited" and dep["ledger_ok"] is True
    assert await ledger.balance("acct1", "ETH") == {
        "available": VALUE // 10**10,
        "scheduled": 0,
        "sent": 0,
    }
    # a second full pass changes nothing: the log is deduped, the credit is guarded
    await scanner.scan_pipe(ETH, rpc)
    await workers.confirm_locked(head=1600)
    await workers.reconcile_credits()
    assert await d.entries.count_documents({"kind": "credit", "ref": "dep1"}) == 1
    assert (await d.deposits.find_one({"_id": "dep1"}))["status"] == "credited"


async def test_lock_records_the_solvers_fill_amount(mock_db, our_key, rpc):
    await _deposit(mock_db)
    fee = 112_168_855_201
    rpc.logs_.append(lock_log(ETH.pipe, 1, VALUE, fee, PUBKEY, 1500, TX, 0))
    rpc.receipts[TX] = {
        "logs": [fulfilled_log(ORDER, VALUE + fee, "0x" + "ab" * 20, TX, 1), rpc.logs_[0]]
    }
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 1
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"})
    assert dep["eth"]["fill_units"] == str(VALUE + fee)  # actualFulfillAmount from FulfilledOrder
    assert dep["eth"]["relayer_fee_logged"] == str(fee)


async def test_lock_attributed_by_metadata_tag_creates_the_deposit_from_the_quote(
    mock_db, our_key, rpc, monkeypatch
):
    d = mock_db["pgasme_test"]
    await d.quotes.insert_one(
        {
            "_id": "q9",
            "account_id": "acct9",
            "address": "0x" + "ab" * 20,
            "asset": "ETH",
            "armed": True,
            "src": {"chain_id": 42161, "dln_chain_id": 42161, "token": "0x0", "amount": "10000000"},
            "out_units": str(VALUE + 5),
            "value_units": str(VALUE),
            "relayer_fee_units": "5",
            "value_groth": VALUE // 10**10,
            "metadata": "0x0102030405",
            "pubkey": PUBKEY,
            "order_id": "0x" + "99" * 32,
            "at": time.time(),
            "expires_at": time.time() + 900,
        }
    )
    unknown = "0x" + "12" * 32

    async def lite(order_id):
        assert order_id == unknown
        return {"rawOrderMetadataHex": DLN_ESTIMATE["order"]["metadata"]}

    monkeypatch.setattr(dln, "lite_model", lite)
    rpc.logs_.append(lock_log(ETH.pipe, 7, VALUE, 5, PUBKEY, 1700, TX, 2))
    rpc.receipts[TX] = {
        "logs": [fulfilled_log(unknown, VALUE + 5, "0x" + "ab" * 20, TX, 0), rpc.logs_[0]]
    }
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 1
    dep = await d.deposits.find_one({"quote_id": "q9"})
    assert (
        dep
        and dep["status"] == "locked"
        and dep["account_id"] == "acct9"
        and dep["order_id"] == unknown
    )
    assert dep["src_tx_hash"] is None and "scanner" in dep["note"]


async def test_lock_without_a_dln_fill_is_unattributed_and_never_credited(mock_db, our_key, rpc):
    rpc.logs_.append(lock_log(ETH.pipe, 5, 10**15, 10**11, PUBKEY, 1200, TX, 4))
    rpc.receipts[TX] = {"logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["unattributed"] == 1 and st["locked"] == 0
    d = mock_db["pgasme_test"]
    row = await d.unattributed_locks.find_one({"_id": f"{TX}:4"})
    assert (
        row["status"] == "open"
        and row["msg_id"] == 5
        and row["amount"] == str(10**15)
        and "not a DLN fill" in row["reason"]
    )
    ev = await d.events.find_one({"kind": "lock_unattributed"})
    assert "MANUAL HANDLING REQUIRED" in ev["text"] and TX in ev["text"] and "msgId 5" in ev["text"]
    assert await d.entries.count_documents({}) == 0 and await d.deposits.count_documents({}) == 0
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 0  # deduped by (tx, logIndex)
    assert await d.events.count_documents({"kind": "lock_unattributed"}) == 1


async def test_amount_mismatch_is_unattributed(mock_db, our_key, rpc):
    await _deposit(mock_db)
    rpc.logs_.append(lock_log(ETH.pipe, 8, VALUE + 10**10, 1, PUBKEY, 1300, TX, 0))
    rpc.receipts[TX] = {"logs": [fulfilled_log(ORDER, VALUE, "0x" + "ab" * 20, TX, 1)]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["unattributed"] == 1
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"_id": "dep1"}))["status"] == "submitted"
    assert "amount mismatch" in (await d.unattributed_locks.find_one({}))["reason"]


async def test_foreign_pubkey_locks_are_ignored(mock_db, our_key, rpc):
    rpc.logs_.append(lock_log(ETH.pipe, 9, 10**15, 1, "03" + "cd" * 32, 1300, TX, 0))
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["logs"] == 1 and st["locked"] == 0 and st["unattributed"] == 0
    assert await mock_db["pgasme_test"].unattributed_locks.count_documents({}) == 0


async def test_missing_receipt_fails_the_chunk_instead_of_skipping_the_lock(mock_db, our_key, rpc):
    rpc.logs_.append(lock_log(ETH.pipe, 1, VALUE, 1, PUBKEY, 1300, TX, 0))  # no receipt scripted
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["failed"] and "receipt" in st["failed"]["error"]
    assert (await mock_db["pgasme_test"].scanner_state.find_one({"_id": ETH.pipe}))[
        "last_block"
    ] == 999


async def test_late_attribution_resolves_an_open_unattributed_lock(
    mock_db, our_key, rpc, monkeypatch
):
    unknown = "0x" + "34" * 32
    monkeypatch.setattr(scanner, "LITE_RETRIES", 1)
    rpc.logs_.append(lock_log(ETH.pipe, 3, VALUE, 1, PUBKEY, 1300, TX, 0))
    rpc.receipts[TX] = {"logs": [fulfilled_log(unknown, VALUE, "0x" + "ab" * 20, TX, 1)]}
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 1
    await _deposit(
        mock_db, order_id=unknown, eth={"value_units": str(VALUE), "relayer_fee_units": "1"}
    )
    assert await scanner.retry_unattributed(rpc) == 1
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"_id": "dep1"}))["status"] == "locked"
    assert (await d.unattributed_locks.find_one({}))["status"] == "resolved"
