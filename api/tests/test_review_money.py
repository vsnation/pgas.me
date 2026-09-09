"""One regression per confirmed money-path defect of the 2026-09-09 adversarial review.

Every test here is a thing that was possible before the fix: a stranger's fill credited to
whoever posted the hash first, a lagging RPC checkpointing "no locks" over a real deposit, two
withdrawals spending the same groth, a refund with no debit behind it, an alert dropped because
the send failed, an ingress that kept taking money after the kill switch was thrown.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

import pytest
from conftest import DLN_ESTIMATE, OTHER_PUBKEY, PUBKEY, USDC_ARB, FakeRpc, fund, lock_log
from eth_account import Account as EthAccount
from pymongo.errors import DuplicateKeyError

from pgasme import dln, ethpipe, ledger, scanner, tg, workers
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
ZERO = "0x0000000000000000000000000000000000000000"
HALF_ETH_TENTH = 50_000_000_000_000_000  # 0.05 ETH — clears the 0.02 ETH floor
Q_DLN = {"src_chain_id": 42161, "src_token": USDC_ARB, "amount": "10000000", "target_asset": "ETH"}
Q_DIRECT = {"src_chain_id": 1, "src_token": ZERO, "amount": str(HALF_ETH_TENTH), "target_asset": "ETH"}
VALUE = 3_774_700_000_000_000
TX = "0x" + "aa" * 32


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Nothing in this file may reach CoinGecko, deBridge or an Ethereum node by accident."""

    async def px(force: bool = False) -> dict[str, float]:
        return {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}

    async def no_ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return []

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)
    monkeypatch.setattr(dln, "order_ids_by_tx", no_ids)
    monkeypatch.setattr(scanner, "LITE_RETRY_SLEEP_S", 0.0)


class FakeDln:
    """create_tx like the recorded API: `auto` → the estimate, an explicit amount → it echoed."""

    def __init__(self, recommended: dict[int, int] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.recommended = recommended or {}

    async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        body = copy.deepcopy(DLN_ESTIMATE)
        out = body["estimation"]["dstChainTokenOut"]
        if params["dstChainTokenOutAmount"] != "auto":
            amt = int(params["dstChainTokenOutAmount"])
            base = int(DLN_ESTIMATE["estimation"]["dstChainTokenOut"]["amount"])
            out["amount"] = str(amt)
            out["recommendedAmount"] = str(self.recommended.get(amt, amt))
            out["approximateUsdValue"] = round(out["approximateUsdValue"] * amt / base, 6)
            body["orderId"] = "0x" + "77" * 32
        return body


@pytest.fixture
def fake_dln(monkeypatch):
    f = FakeDln()
    monkeypatch.setattr(dln, "create_tx", f)
    monkeypatch.setattr(settings, "min_deposit_wei", 10**15)
    return f


async def armed_dln_quote(client, user) -> dict[str, Any]:
    r = await client.post("/v1/quote", json=Q_DLN, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def armed_direct_quote(client, user) -> dict[str, Any]:
    r = await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ═══════════════════════ defect 1 — the hash is not a claim (dln + direct) ═══════════════════


async def test_dln_hijack_the_registration_refuses_a_foreign_transaction(
    client, user, armed_eth, fake_dln, mock_db, monkeypatch
):
    """The attacker holds a quote of their own and offers somebody else's transaction hash.
    deBridge's index says that transaction created a DIFFERENT order → 400, no row, no claim."""
    quote = await armed_dln_quote(client, user)
    victim_tx = "0x" + "be" * 32

    async def ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        assert tx_hash == victim_tx
        return ["0x" + "99" * 32]  # the victim's order, not this quote's

    monkeypatch.setattr(dln, "order_ids_by_tx", ids)
    r = await client.post(
        "/v1/deposits",
        json={"quote_id": quote["quote_id"], "src_tx_hash": victim_tx},
        headers=user["headers"],
    )
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "that transaction does not carry this quote's deBridge order — register the transaction "
        "you signed for this quote"
    )
    d = mock_db["pgasme_test"]
    assert await d.deposits.count_documents({}) == 0  # nothing was registered at all
    ev = await d.events.find_one({"kind": "deposit_mismatch"})
    assert ev and ev["quote_id"] == quote["quote_id"] and user["address"] not in ev["text"]


async def test_dln_a_not_yet_indexed_hash_is_accepted_unverified_and_rechecked(
    client, user, armed_eth, fake_dln, mock_db, monkeypatch
):
    """DLN indexes with delay, so a freshly signed transaction cannot be verified at
    registration. It is taken UNVERIFIED and the worker is the gate: a foreign order id fails
    the row, is never adopted, and the hash goes back so its real owner can register it."""
    quote = await armed_dln_quote(client, user)
    tx = "0x" + "cc" * 32
    r = await client.post(
        "/v1/deposits",
        json={"quote_id": quote["quote_id"], "src_tx_hash": tx},
        headers=user["headers"],
    )
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    dep_id = r.json()["deposit_id"]
    d = mock_db["pgasme_test"]
    dep = await d.deposits.find_one({"_id": dep_id})
    assert dep["verified"] is False and dep["order_id"] == quote["order_id"]

    stranger = "0x" + "99" * 32

    async def ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return [stranger]

    monkeypatch.setattr(dln, "order_ids_by_tx", ids)
    await d.deposits.update_one({"_id": dep_id}, {"$set": {"created_at": time.time() - 3600}})
    await workers.dln_secondary()
    dep = await d.deposits.find_one({"_id": dep_id})
    assert dep["status"] == "failed" and dep["order_id"] == quote["order_id"] != stranger
    assert dep.get("src_tx_hash") is None and dep["src_tx_hash_rejected"] == tx
    assert "deposit mismatch" in dep["note"]
    assert await d.deposits.find_one({"src_tx_hash": tx}) is None  # the hash is free again
    ev = await d.events.find_one({"kind": "deposit_mismatch"})
    assert ev and ev["deposit_id"] == dep_id and ev["notified"] is True  # sent at once


async def test_direct_hijack_every_way_a_pipe_call_can_fail_to_be_yours(
    client, user, armed_eth, mock_db, rpc
):
    quote = await armed_direct_quote(client, user)
    h, qid = user["headers"], quote["quote_id"]
    calldata = quote["tx"]["data"]
    stranger = EthAccount.create().address

    async def register(tx_hash: str):
        return await client.post(
            "/v1/deposits", json={"quote_id": qid, "src_tx_hash": tx_hash}, headers=h
        )

    # (a) the pipe call of a DIFFERENT wallet — the case the review found ("first registrant wins")
    foreign = "0x" + "11" * 32
    rpc.txs[foreign] = {"from": stranger, "to": ETH.pipe, "input": calldata}
    r = await register(foreign)
    assert r.status_code == 400 and "not sent from the wallet" in r.json()["detail"]
    # (b) a transaction to something that is not our pipe
    other_to = "0x" + "22" * 32
    rpc.txs[other_to] = {"from": user["address"], "to": "0x" + "de" * 20, "input": calldata}
    assert "not a call to the ETH pipe" in (await register(other_to)).json()["detail"]
    # (c) our pipe, our wallet, but locking to a different Beam pubkey
    wrong_pk = "0x" + "33" * 32
    call = ethpipe.decode_send_funds(calldata)
    rpc.txs[wrong_pk] = {
        "from": user["address"],
        "to": ETH.pipe,
        "input": ethpipe.encode_send_funds(call["value"], call["relayer_fee"], OTHER_PUBKEY),
    }
    assert "different Beam pubkey" in (await register(wrong_pk)).json()["detail"]
    # (d) our pipe, our wallet, our pubkey — a different amount than this quote priced
    wrong_amt = "0x" + "44" * 32
    rpc.txs[wrong_amt] = {
        "from": user["address"],
        "to": ETH.pipe,
        "input": ethpipe.encode_send_funds(call["value"] - 10**10, call["relayer_fee"], PUBKEY),
    }
    assert "locks a different amount" in (await register(wrong_amt)).json()["detail"]
    # (e) a hash no node has seen: retryable, NOT a rejection and NOT an acceptance
    r = await register("0x" + "55" * 32)
    assert r.status_code == 409 and "not visible on Ethereum yet" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0
    # (f) the real thing
    mine = "0x" + "66" * 32
    rpc.txs[mine] = {"from": user["address"].lower(), "to": ETH.pipe.lower(), "input": calldata}
    ok = await register(mine)
    assert ok.status_code == 200
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": ok.json()["deposit_id"]})
    assert dep["verified"] is True and dep["address"] == user["address"]


async def test_direct_registration_never_concludes_from_an_unreadable_rpc(
    client, user, armed_eth, monkeypatch, mock_db
):
    quote = await armed_direct_quote(client, user)

    class DeadRpc:
        async def transaction(self, tx_hash, prefer=None, pin=False):
            raise ethpipe.RpcError("eth_getTransactionByHash: no endpoint answered")

    monkeypatch.setattr(workers, "get_rpc", DeadRpc)
    r = await client.post(
        "/v1/deposits",
        json={"quote_id": quote["quote_id"], "src_tx_hash": "0x" + "77" * 32},
        headers=user["headers"],
    )
    assert r.status_code == 503 and "could not read that transaction" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0


async def test_the_scanner_checks_the_sender_of_a_direct_lock_too(mock_db, rpc, monkeypatch):
    """Belt to the registration check: the receipt's `from` must be the quote's wallet."""
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    mine = "0x" + "ab" * 20
    d = mock_db["pgasme_test"]
    await d.deposits.insert_one(
        {
            "_id": "dep1",
            "account_id": "acct1",
            "address": mine,
            "asset": "ETH",
            "mode": "direct",
            "status": "submitted",
            "src": {"chain_id": 1, "token": ZERO, "amount": str(VALUE + 1)},
            "quote_id": "q1",
            "src_tx_hash": TX,
            "order_id": None,
            "eth": {"value_units": str(VALUE), "relayer_fee_units": "1"},
            "value_groth": VALUE // 10**10,
            "pubkey": PUBKEY,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    )
    rpc.logs_.append(lock_log(ETH.pipe, 42, VALUE, 1, PUBKEY, 1500, TX, 0))
    rpc.receipts[TX] = {"from": "0x" + "cd" * 20, "logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["unattributed"] == 1 and st["locked"] == 0
    assert (await d.deposits.find_one({"_id": "dep1"}))["status"] == "submitted"
    assert "sender mismatch" in (await d.unattributed_locks.find_one({}))["reason"]
    assert await d.entries.count_documents({}) == 0
    # the same lock from the right wallet does attribute
    rpc.receipts[TX]["from"] = mine.upper()
    await d.unattributed_locks.delete_many({})
    await d.scanner_state.delete_many({})
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 1


async def test_one_deposit_per_source_transaction_is_a_database_rule(mock_db):
    await scanner.ensure_indexes()
    d = mock_db["pgasme_test"]
    await d.deposits.insert_one({"_id": "a", "src_tx_hash": TX})
    with pytest.raises(DuplicateKeyError):
        await d.deposits.insert_one({"_id": "b", "src_tx_hash": TX})
    # the scanner's own rows carry no hash at all; many of those are not a collision
    await d.deposits.insert_one({"_id": "c", "src_tx_hash": None})
    await d.deposits.insert_one({"_id": "e", "src_tx_hash": None})


# ═══════════════════ defect 2 — a lagging endpoint cannot checkpoint "no locks" ═══════════════


async def test_a_pinned_scan_refuses_a_range_the_answering_endpoint_cannot_see(mock_db, monkeypatch):
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    monkeypatch.setattr(settings, "lock_scan_chunk", 500)
    monkeypatch.setattr(settings, "lock_scan_blocks", 2000)
    rpc = FakeRpc(head=2000)
    rpc.heads[FakeRpc.PRIMARY] = 1200  # it vouched for 2000 and can only see 1200
    rpc.logs_.append(lock_log(ETH.pipe, 9, VALUE, 1, PUBKEY, 1300, TX, 0))  # inside the gap
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["prefer"] == FakeRpc.PRIMARY and st["chunks"] == 2
    assert "head is 1200" in st["failed"]["error"] and st["failed"]["from"] == 1000
    assert (await mock_db["pgasme_test"].scanner_state.find_one({"_id": ETH.pipe}))[
        "last_block"
    ] == 999
    assert all(c[4] == FakeRpc.PRIMARY for c in rpc.calls)  # every read pinned to that endpoint
    # the lock is NOT lost: when the endpoint catches up the same range is read again
    rpc.heads[FakeRpc.PRIMARY] = 2000
    rpc.receipts[TX] = {"logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["from"] == 1000 and st["failed"] is None and st["unattributed"] == 1


# ═══════════════════════ defect 3 — the retry queue cannot starve ════════════════════════════


async def _unattributed(mock_db, _id: str, at: float, **over: Any) -> None:
    row = {
        "_id": _id,
        "asset": "ETH",
        "pipe": ETH.pipe,
        "tx": _id,
        "block": 1,
        "log_index": 0,
        "msg_id": 1,
        "amount": str(VALUE),
        "relayer_fee": "1",
        "receiver": PUBKEY,
        "order_ids": [],
        "reason": "test",
        "status": "open",
        "at": at,
        **over,
    }
    await mock_db["pgasme_test"].unattributed_locks.insert_one(row)


async def test_retry_unattributed_pages_backs_off_and_abandons(mock_db, monkeypatch):
    d = mock_db["pgasme_test"]
    now = time.time()
    for i in range(25):  # more than one page: the old limit(20) window could never reach these
        await _unattributed(mock_db, f"junk{i:02d}", now - 100 - i)
    await _unattributed(mock_db, "old", now - 90000)  # older than max_age → abandoned
    await _unattributed(mock_db, "spent", now - 50, tries=scanner.RETRY_MAX_TRIES)

    class CountingRpc(FakeRpc):
        def __init__(self):
            super().__init__(head=10)
            self.n = 0

        async def receipt(self, tx, prefer=None, pin=False):
            self.n += 1
            return None

    rpc = CountingRpc()
    assert await scanner.retry_unattributed(rpc) == 0
    assert rpc.n == 25  # every open, due, in-age row was looked at — not just the first 20
    rows = {r["_id"]: r for r in await d.unattributed_locks.find({}).to_list(50)}
    assert rows["old"]["status"] == "abandoned" and rows["spent"]["status"] == "abandoned"
    assert all(rows[f"junk{i:02d}"]["tries"] == 1 for i in range(25))
    assert all(rows[f"junk{i:02d}"]["next_try_at"] > time.time() for i in range(25))
    assert await d.events.count_documents({"kind": "lock_abandoned"}) == 2
    rpc.n = 0
    assert await scanner.retry_unattributed(rpc) == 0 and rpc.n == 0  # backed off, no spin


async def test_a_recoverable_lock_is_not_starved_by_older_junk(mock_db, monkeypatch, rpc):
    """The oldest rows are tried first AND paging reaches beyond one window, so the one lock
    that can now be attributed is picked up in the same pass as 20 that cannot."""
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    now = time.time()
    for i in range(20):
        await _unattributed(mock_db, f"junk{i:02d}", now - 1000 - i)
    await _unattributed(mock_db, TX, now - 10, tx=TX)
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "dep1",
            "account_id": "acct1",
            "asset": "ETH",
            "mode": "direct",
            "status": "submitted",
            "src": {},
            "src_tx_hash": TX,
            "order_id": None,
            "eth": {"value_units": str(VALUE), "relayer_fee_units": "1"},
            "value_groth": VALUE // 10**10,
            "created_at": now,
            "updated_at": now,
        }
    )
    rpc.receipts = {r: {"logs": []} for r in [f"junk{i:02d}" for i in range(20)]}
    rpc.receipts[TX] = {"from": "0x" + "ab" * 20, "logs": []}
    assert await scanner.retry_unattributed(rpc) == 1
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"_id": "dep1"}))["status"] == "locked"
    assert (await d.unattributed_locks.find_one({"_id": TX}))["status"] == "resolved"


# ══════════════════ defect 4 — the credit claim is atomic and the ref is unique ═══════════════


async def test_a_credit_ref_is_unique_in_the_database(mock_db):
    await ledger.ensure_indexes()
    await ledger.credit("acct1", "ETH", 100, "dep1")
    with pytest.raises(ledger.AlreadyCredited):
        await ledger.credit("acct1", "ETH", 100, "dep1")
    assert (await ledger.balance("acct1", "ETH"))["available"] == 100


async def test_reconcile_claims_before_it_writes(mock_db):
    """Two passes at once used to both read has_credit() == False and both append."""
    await ledger.ensure_indexes()
    d = mock_db["pgasme_test"]
    await d.deposits.insert_one(
        {
            "_id": "dep1",
            "account_id": "acct1",
            "asset": "ETH",
            "status": "credited",
            "src": {},
            "eth": {"value_units": "1", "tx": "0xt", "msg_id": 1},
            "value_groth": 500,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    )
    await asyncio.gather(workers.reconcile_credits(), workers.reconcile_credits())
    assert await d.entries.count_documents({"kind": "credit", "ref": "dep1"}) == 1
    assert (await ledger.balance("acct1", "ETH"))["available"] == 500
    assert (await d.deposits.find_one({"_id": "dep1"}))["ledger_ok"] is True
    assert await d.events.count_documents({"kind": "deposit_credited"}) == 1


# ═════════════════ defect 5 — a refund needs the debit it reverses ═══════════════════════════


async def test_cancel_without_a_schedule_entry_refunds_nothing(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 100_000_000)
    d = mock_db["pgasme_test"]
    await d.payout_requests.insert_one(
        {
            "_id": "ghost",
            "account_id": user["account_id"],
            "asset": "ETH",
            "mode": "direct",
            "W": "0x" + "ab" * 20,
            "amount_groth": 5_000_000,
            "fee_groth": 100_000,
            "status": "scheduled",
            "release_at": time.time(),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    )
    before = await ledger.balance(user["account_id"], "ETH")
    r = await client.post("/v1/withdrawals/ghost/cancel", headers=user["headers"])
    assert r.status_code == 200 and r.json() == {"cancelled": "ghost", "refunded_groth": 0}
    assert await ledger.balance(user["account_id"], "ETH") == before  # nothing was minted
    assert await d.events.find_one({"kind": "withdrawal_cancel_no_debit"})


async def test_a_refund_happens_once_and_only_for_what_was_debited(mock_db):
    await ledger.schedule("acct1", "ETH", 1_020_000, "r1")
    await ledger.cancel("acct1", "ETH", 1_020_000, "r1", refund_of="schedule:r1")
    with pytest.raises(ledger.AlreadyRefunded):
        await ledger.cancel("acct1", "ETH", 1_020_000, "r1", refund_of="schedule:r1")
    assert await ledger.balance("acct1", "ETH") == {"available": 0, "scheduled": 0, "sent": 0}
    entry = await ledger.find_entry("cancel", "r1")
    assert entry["refund_of"] == "schedule:r1"


async def test_the_debit_is_written_before_the_row_it_belongs_to(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 100_000_000)
    dest = EthAccount.create()
    from conftest import add_destination

    assert (await add_destination(client, user, dest)).status_code == 200
    r = await client.post(
        "/v1/withdrawals",
        json={
            "asset": "ETH",
            "items": [{"W": dest.address, "amount_groth": 1_000_000}],
            "mode": "direct",
            "window_s": 0,
        },
        headers=user["headers"],
    )
    assert r.status_code == 200, r.text
    rid = r.json()["request_ids"][0]
    entry = await ledger.find_entry("schedule", rid)
    row = await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid})
    assert entry and row and entry["groth"] == 1_020_000
    assert entry["at"] <= row["created_at"] + 1  # the debit is never after its row


# ═════════════════ defect 6 — two withdrawals cannot spend the same groth ════════════════════


async def test_concurrent_withdrawals_cannot_overdraw(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_020_000)  # room for exactly ONE 0.01 ETH payout incl. the 2% fee
    dest = EthAccount.create()
    from conftest import add_destination

    assert (await add_destination(client, user, dest)).status_code == 200
    real_balance = ledger.balance

    async def slow_balance(account_id: str, asset: str):
        await asyncio.sleep(0.02)  # both requests read Available before either writes
        return await real_balance(account_id, asset)

    monkeypatch.setattr(ledger, "balance", slow_balance)
    body = {
        "asset": "ETH",
        "items": [{"W": dest.address, "amount_groth": 1_000_000}],
        "mode": "direct",
        "window_s": 0,
    }
    a, b = await asyncio.gather(
        client.post("/v1/withdrawals", json=body, headers=user["headers"]),
        client.post("/v1/withdrawals", json=body, headers=user["headers"]),
    )
    codes = sorted([a.status_code, b.status_code])
    assert codes == [200, 409], (a.text, b.text)
    monkeypatch.setattr(ledger, "balance", real_balance)
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": 0,
        "scheduled": 1_020_000,
        "sent": 0,
    }
    d = mock_db["pgasme_test"]
    assert await d.payout_requests.count_documents({"status": "scheduled"}) == 1
    assert await d.entries.count_documents({"kind": "schedule"}) == 1
    # the reservation is given back either way — the next withdrawal is not blocked by a ghost
    assert (await d.reservations.find_one({}))["pending"] == 0


# ═══════════════════════ defect 7 — the kill switch reaches the request path ══════════════════


@pytest.fixture
def paused(tmp_path, monkeypatch):
    stop = tmp_path / "pgasme.stop"
    stop.write_text("")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    return stop


async def test_the_stop_file_closes_ingress_not_the_whole_site(
    client, user, armed_eth, fake_dln, paused, mock_db, rpc, monkeypatch
):
    h = user["headers"]
    monkeypatch.setattr(settings, "min_deposit_wei", 1)  # the floor is not what is under test
    assert workers.paused() is True
    r = await client.post("/v1/quote", json=Q_DLN, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == workers.PAUSED_REASON
    assert (await client.post("/v1/quote", json=Q_DIRECT, headers=h)).status_code == 409
    r = await client.post(
        "/v1/deposits", json={"quote_id": "x" * 12, "src_tx_hash": "0x" + "ab" * 32}, headers=h
    )
    assert r.status_code == 409 and r.json()["detail"] == workers.PAUSED_REASON
    r = await client.post(
        "/v1/withdrawals",
        json={"asset": "ETH", "items": [{"W": "0x" + "ab" * 20, "amount_groth": 1}],
              "mode": "direct", "window_s": 0},
        headers=h,
    )
    assert r.status_code == 409 and r.json()["detail"] == workers.PAUSED_REASON
    health = (await client.get("/v1/health")).json()
    assert health["paused"] is True
    # a quote that could never carry OUR transaction still answers: an estimate spends nothing
    estimate_only = await client.post("/v1/quote", json={**Q_DLN, "target_asset": "DAI"}, headers=h)
    assert estimate_only.status_code == 200 and estimate_only.json()["armed"] is False


async def test_ingress_reopens_when_the_stop_file_goes(client, user, armed_eth, fake_dln, paused):
    assert (await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])).status_code == 409
    paused.unlink()
    r = await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])
    assert r.status_code == 200 and r.json()["armed"] is True


# ═══════════════ defect 8 — the floor is checked on what the user actually gets ═══════════════


async def test_the_minimum_deposit_is_rechecked_after_the_requote(client, user, armed_eth, monkeypatch):
    first = 3774812168855201
    rec = first - 81_000_000_000_000  # DLN prices the hook's gas in and recommends less
    monkeypatch.setattr(dln, "create_tx", FakeDln(recommended={first: rec}))
    monkeypatch.setattr(settings, "min_deposit_wei", first - 10)  # between the two amounts
    r = await client.post("/v1/quote", json=Q_DLN, headers=user["headers"])
    assert r.status_code == 400 and "minimum deposit" in r.json()["detail"]
    # and below the floor for BOTH amounts it is refused before the order is ever placed
    monkeypatch.setattr(settings, "min_deposit_wei", first + 10**15)
    assert (await client.post("/v1/quote", json=Q_DLN, headers=user["headers"])).status_code == 400


async def test_the_usd_of_a_requoted_order_is_never_the_old_one(client, user, armed_eth, monkeypatch):
    first = 3774812168855201
    rec = first - 81_000_000_000_000
    monkeypatch.setattr(dln, "create_tx", FakeDln(recommended={first: rec}))
    monkeypatch.setattr(settings, "min_deposit_wei", 10**15)
    body = (await client.post("/v1/quote", json=Q_DLN, headers=user["headers"])).json()
    est = body["estimate"]
    first_usd = DLN_ESTIMATE["estimation"]["dstChainTokenOut"]["approximateUsdValue"]
    assert est["out_units"] == str(rec)
    # the usd describes the ORDER that was placed, not the estimate that was abandoned
    assert est["usd"] != first_usd
    assert est["usd"] == pytest.approx(first_usd * rec / first, rel=1e-6)


# ═══════════════════ defect 9 — an event is notified only when it was sent ═══════════════════


async def test_drain_keeps_an_event_whose_send_failed(mock_db, monkeypatch):
    monkeypatch.setattr(tg, "enabled", lambda: True)
    calls = {"n": 0}

    async def down(text, *, key=None, cooldown_s=0.0):
        calls["n"] += 1
        return False

    monkeypatch.setattr(tg, "send", down)
    await tg.queue("deposit_credited", "Deposit credited: ETH 1", deposit_id="dep1")
    d = mock_db["pgasme_test"]
    assert await workers.drain_events() == 1
    ev = await d.events.find_one({"deposit_id": "dep1"})
    assert ev["notified"] is False and ev["tries"] == 1 and ev["next_try_at"] > time.time()
    assert await workers.drain_events() == 0 and calls["n"] == 1  # backed off, not spinning
    for _ in range(2, workers.EVENT_MAX_TRIES + 1):
        await d.events.update_one({"_id": ev["_id"]}, {"$set": {"next_try_at": 0}})
        await workers.drain_events()
    ev = await d.events.find_one({"deposit_id": "dep1"})
    assert ev["notified"] is True and ev["sent"] is False and ev["gave_up"] is True
    # ... and a send that works closes the row honestly
    await tg.queue("deposit_credited", "Deposit credited: ETH 2", deposit_id="dep2")

    async def up(text, *, key=None, cooldown_s=0.0):
        return True

    monkeypatch.setattr(tg, "send", up)
    await workers.drain_events()
    ev2 = await d.events.find_one({"deposit_id": "dep2"})
    assert ev2["notified"] is True and ev2["sent"] is True


async def test_a_cooldown_counts_attempts_not_successes():
    tg._last_sent.clear()
    assert await tg.send("first", key="k", cooldown_s=60) is False  # muted → False
    assert "k" in tg._last_sent  # the ATTEMPT started the cooldown
    assert await tg.send("second", key="k", cooldown_s=60) is False


# ═══════════════════ defect 10 — a down explorer is asked once, not once per visitor ══════════


async def test_stats_negative_caches_a_failing_explorer(mock_db, monkeypatch, client):
    calls = {"n": 0}

    async def down():
        calls["n"] += 1
        raise RuntimeError("explorer HTTP 502")

    monkeypatch.setattr(workers, "fetch_pool_status", down)
    workers.clear_pool_cache()
    for _ in range(5):
        body = (await client.get("/v1/stats")).json()
        assert body["pool"]["stale"] is True
    assert calls["n"] == 1  # not five 15-second timeouts
    workers._pool["tried_at"] = time.time() - workers.POOL_RETRY_AFTER_FAIL_S - 1
    await client.get("/v1/stats")
    assert calls["n"] == 2  # it does try again, just not per request


# ═══════════════════════ defect 11 — one account cannot flood the quote path ══════════════════


async def test_a_per_account_quote_cap_answers_429_with_retry_after(client, user, fake_dln, mock_db):
    now = time.time()
    for i in range(20):
        await mock_db["pgasme_test"].quotes.insert_one(
            {"_id": f"q{i}", "account_id": user["account_id"], "at": now - 1}
        )
    r = await client.post("/v1/quote", json=Q_DLN, headers=user["headers"])
    assert r.status_code == 429 and "too many quotes" in r.json()["detail"]
    assert 1 <= int(r.headers["retry-after"]) <= 61
    # another account is unaffected, and an aged-out window is too
    await mock_db["pgasme_test"].quotes.update_many({}, {"$set": {"at": now - 120}})
    assert (await client.post("/v1/quote", json=Q_DLN, headers=user["headers"])).status_code == 200
