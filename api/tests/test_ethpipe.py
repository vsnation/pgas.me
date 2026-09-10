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


# ─────────────── the pool answers, not the first endpoint of it (2026-09-10, T37d) ─────────────
#
# `call()` stops at the FIRST endpoint that ANSWERS, and for eth_getTransactionReceipt and
# eth_getCode a `null` / a refusal IS an answer. Both cost money on 2026-09-10: a receipt one
# endpoint could not see yet read as "no receipt" and left lock msgId 138 (0.0019999 ETH)
# unattributed, and a code read pinned to whichever endpoint answered the head first hit
# publicnode's "Archive requests require a personal token" every 30 s, so no payout was released.

CODE = "0x60006000fd"


def patch_post(monkeypatch, fn):
    monkeypatch.setattr(ethpipe.Rpc, "_post", fn)


async def test_receipt_asks_every_endpoint_not_only_the_first(monkeypatch):
    """The first endpoint answers `null` for a receipt the second one has. `null` is an answer,
    so `call()` stopped there — and the caller read it as "not mined yet"."""

    async def per_url(self, c, url, method, params):
        assert method == "eth_getTransactionReceipt"
        return {"status": "0x1", "logs": []} if url == "https://b" else None

    patch_post(monkeypatch, per_url)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b"], timeout=8.0)
    rec = await rpc.receipt("0x" + "aa" * 32)
    assert rec and rec["status"] == "0x1"


async def test_a_receipt_no_endpoint_answers_is_unreadable_never_absent(monkeypatch):
    """§an unreadable query is not evidence of anything. Every endpoint erroring is "ask again",
    never "there is no receipt" — the caller must not conclude from it."""

    async def broken(self, c, url, method, params):
        raise ethpipe.RpcError(f"{url}: 429 Too Many Requests", url=url)

    patch_post(monkeypatch, broken)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b"], timeout=8.0)
    with pytest.raises(ethpipe.RpcError, match="no endpoint answered"):
        await rpc.receipt("0x" + "aa" * 32)


async def test_every_endpoint_answering_null_is_a_receipt_that_is_not_mined_yet(monkeypatch):
    """The other half: endpoints that DID answer and none has it is an honest None."""

    async def nothing(self, c, url, method, params):
        return None

    patch_post(monkeypatch, nothing)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b"], timeout=8.0)
    assert await rpc.receipt("0x" + "aa" * 32) is None


async def test_code_at_head_anywhere_fails_over_to_an_endpoint_that_will_serve_the_read(
    monkeypatch,
):
    """publicnode answers the head and then refuses the code read at it. The remedy for an
    endpoint that will not serve a query is MORE endpoints (law 8), never a lower bar."""
    asked: list[tuple[str, str, tuple]] = []

    async def per_url(self, c, url, method, params):
        asked.append((url, method, tuple(params)))
        if method == "eth_blockNumber":
            return hex(100 if url == "https://a" else 120)
        if url == "https://a":
            raise ethpipe.RpcError(
                f"{url}: Archive requests require a personal token", url=url
            )
        return CODE

    patch_post(monkeypatch, per_url)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b"], timeout=8.0)
    code, head, url = await ethpipe.code_at_head_anywhere(rpc, "0x" + "11" * 20)
    assert (code, head, url) == (CODE, 120, "https://b")
    # each endpoint was asked for ITS OWN head, and the code read was pinned to that head
    assert ("https://a", "eth_getCode", ("0x" + "11" * 20, hex(100))) in asked
    assert ("https://b", "eth_getCode", ("0x" + "11" * 20, hex(120))) in asked


async def test_code_at_head_anywhere_is_unreadable_when_every_endpoint_refuses(monkeypatch):
    async def broken(self, c, url, method, params):
        raise ethpipe.RpcError(f"{url}: Can't route your request", url=url)

    patch_post(monkeypatch, broken)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b"], timeout=8.0)
    with pytest.raises(ethpipe.RpcError, match="no endpoint"):
        await ethpipe.code_at_head_anywhere(rpc, "0x" + "11" * 20)


async def test_code_at_head_anywhere_refuses_an_implausible_head(monkeypatch):
    """A node that says block 0 has not told us where the chain is. `"0x"` from it is not
    "this is a wallet" — it is nothing at all, so the next endpoint is asked instead."""

    async def per_url(self, c, url, method, params):
        if method == "eth_blockNumber":
            return hex(0 if url == "https://a" else 77)
        return "0x"

    patch_post(monkeypatch, per_url)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b"], timeout=8.0)
    code, head, url = await ethpipe.code_at_head_anywhere(rpc, "0x" + "11" * 20)
    assert (code, head, url) == ("0x", 77, "https://b")
