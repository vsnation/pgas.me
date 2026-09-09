"""The pipe ABI, the grid maths per asset, and the log/receipt evidence helpers."""

from __future__ import annotations

import pytest
from conftest import OTHER_PUBKEY, PUBKEY, FakeRpc, lock_log

from pgasme import ethpipe
from pgasme.assets import ASSETS
from pgasme.config import settings


def test_newlocal_topic_matches_the_recorded_mainnet_topic():
    assert ethpipe.NEWLOCAL_TOPIC == ethpipe.NEWLOCAL_TOPIC_EXPECTED


def test_send_funds_round_trip():
    data = ethpipe.encode_send_funds(3_770_000_000_000_000, 4_812_168_855_201, PUBKEY)
    assert data.startswith("0x" + ethpipe.SENDFUNDS_SELECTOR.hex())
    assert ethpipe.decode_send_funds(data) == {
        "value": 3_770_000_000_000_000,
        "relayer_fee": 4_812_168_855_201,
        "pubkey": PUBKEY,
    }
    with pytest.raises(ValueError):
        ethpipe.encode_send_funds(1, 0, "02" + "00" * 31)
    with pytest.raises(ValueError):
        ethpipe.decode_send_funds("0xdeadbeef" + "00" * 32)


def test_split_eth_floors_value_to_1e10_and_rides_the_tail_as_fee():
    amount = 3_774_812_168_855_201  # the recorded 10-USDC estimate
    value, fee = ethpipe.split_for_asset(amount, ASSETS["ETH"])
    assert value % 10**10 == 0 and value + fee == amount
    assert settings.min_relayer_fee_wei <= fee < settings.min_relayer_fee_wei + 10**10
    assert value == 3_774_710_000_000_000


def test_split_dai_uses_the_dai_floor_on_the_same_grid():
    amount = 9_190_021_264_249_749_267
    value, fee = ethpipe.split_for_asset(amount, ASSETS["DAI"])
    assert value % 10**10 == 0 and value + fee == amount
    assert (
        fee >= settings.min_relayer_fee_dai_units
        and fee < settings.min_relayer_fee_dai_units + 10**10
    )


def test_split_wbtc_has_no_grid():
    assert ASSETS["WBTC"].grid == 1
    value, fee = ethpipe.split_for_asset(12_345, ASSETS["WBTC"])
    assert (value, fee) == (12_344, 1)


def test_split_refuses_dust():
    with pytest.raises(ethpipe.SplitError):
        ethpipe.split_amount(10**11, 10**11)
    with pytest.raises(ethpipe.SplitError):
        ethpipe.split_amount(10**11 + 5, 10**11, 10**10)  # nothing left for a single groth
    with pytest.raises(ethpipe.SplitError):
        ethpipe.split_amount(5, 1, 0)


def test_decode_new_local_message():
    lg = lock_log(ASSETS["ETH"].pipe, 222, 10**15, 10**11, PUBKEY, 1234, "0x" + "aa" * 32, 7)
    m = ethpipe.decode_new_local_message(lg)
    assert m == {
        "msg_id": 222,
        "amount": 10**15,
        "relayer_fee": 10**11,
        "receiver": PUBKEY,
        "address": ASSETS["ETH"].pipe,
        "block": 1234,
        "tx": "0x" + "aa" * 32,
        "log_index": 7,
    }


def test_find_lock_in_receipt_matches_only_our_pipe_pubkey_and_amount():
    pipe = ASSETS["ETH"].pipe
    tx = "0x" + "bb" * 32
    receipt = {
        "logs": [
            {"address": pipe, "topics": ["0x" + "00" * 32], "data": "0x"},  # other event
            lock_log("0x" + "11" * 20, 1, 500, 1, PUBKEY, 10, tx, 1),  # wrong contract
            lock_log(pipe, 2, 500, 1, OTHER_PUBKEY, 10, tx, 2),  # someone else's key
            lock_log(pipe, 3, 500, 1, PUBKEY, 10, tx, 3),  # ours
        ]
    }
    assert ethpipe.find_lock_in_receipt(receipt, PUBKEY, pipe)["msg_id"] == 3
    assert ethpipe.find_lock_in_receipt(receipt, PUBKEY, pipe, amount=500)["msg_id"] == 3
    assert ethpipe.find_lock_in_receipt(receipt, PUBKEY, pipe, amount=501) is None
    assert ethpipe.find_lock_in_receipt(receipt, "02" + "ee" * 32, pipe) is None
    assert ethpipe.find_lock_in_receipt({"logs": []}, PUBKEY, pipe) is None


def test_find_locks_in_logs_sorted_by_block_and_index():
    pipe = ASSETS["DAI"].pipe
    logs = [
        lock_log(pipe, 9, 1, 1, PUBKEY, 20, "0xb", 0),
        lock_log(pipe, 8, 1, 1, PUBKEY, 10, "0xa", 3),
        lock_log(pipe, 7, 1, 1, PUBKEY, 10, "0xa", 1),
    ]
    assert [m["msg_id"] for m in ethpipe.find_locks_in_logs(logs, PUBKEY, pipe)] == [7, 8, 9]


async def test_scan_locks_chunks_and_raises_on_an_unreadable_range():
    rpc = FakeRpc(head=1600)
    pipe = ASSETS["ETH"].pipe
    rpc.logs_.append(lock_log(pipe, 1, 1, 1, PUBKEY, 1234, "0xc", 0))
    logs = await ethpipe.scan_locks(rpc, pipe, 100, 1600, chunk=500)
    assert [(c[2], c[3]) for c in rpc.calls] == [
        (100, 599),
        (600, 1099),
        (1100, 1599),
        (1600, 1600),
    ]
    assert len(logs) == 1
    rpc.fail_ranges.add((600, 1099))
    with pytest.raises(ethpipe.RpcError):
        await ethpipe.scan_locks(rpc, pipe, 100, 1600, chunk=500)
