"""2026-09-10 — the incident where two real 0.002 ETH deposits were refused and then lost.

The user clicked Deposit twice. Each time the wallet handed back a hash, POST /v1/deposits asked
the RPC pool for it, the FIRST endpoint (a private-orderflow relay that does not expose pending
transactions) answered `null`, and the API said 409 "that transaction is not visible on Ethereum
yet". No row was ever created. Both transactions then mined and locked in the ETH pipe to our
pubkey, the scanner could find nothing to attribute them to, and the operator was paged to move
real money by hand — twice.

Three things had to be true at once for that, and each has its own section here:

  1. the visibility check asked ONE endpoint's answer instead of the pool's;
  2. "I cannot see it" was a REFUSAL rather than an unverified row that gets proven later;
  3. a pipe lock carrying calldata WE issued could not be matched back to the quote we issued it
     from — although nothing about it was unknown.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from conftest import PUBKEY, lock_log
from eth_account import Account as EthAccount

from pgasme import ethpipe, ledger, scanner, workers, xchain
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
ZERO = "0x0000000000000000000000000000000000000000"
AMOUNT = 50_000_000_000_000_000  # 0.05 ETH, over the product floor
Q_DIRECT = {"src_chain_id": 1, "src_token": ZERO, "amount": str(AMOUNT), "target_asset": "ETH"}
TX = "0x" + "aa" * 32
TX2 = "0x" + "bb" * 32
FEE = 100_000_000_000
VALUE = AMOUNT - FEE


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No test here may reach CoinGecko, the order router or an Ethereum node by accident."""

    async def px(force: bool = False) -> dict[str, float]:
        return {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}

    async def no_ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return []

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)
    monkeypatch.setattr(xchain, "order_ids_by_tx", no_ids)
    monkeypatch.setattr(settings, "lock_scan_chunk", 500)
    monkeypatch.setattr(settings, "lock_scan_blocks", 2000)


@pytest.fixture
def our_key(monkeypatch):
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    return PUBKEY


async def direct_quote(client, user) -> dict[str, Any]:
    r = await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def register(client, user, quote_id: str, tx_hash: str):
    return await client.post(
        "/v1/deposits", json={"quote_id": quote_id, "src_tx_hash": tx_hash}, headers=user["headers"]
    )


def pipe_call(address: str, value: int = VALUE, fee: int = FEE, pubkey: str = PUBKEY) -> dict:
    return {
        "from": address,
        "to": ETH.pipe,
        "input": ethpipe.encode_send_funds(value, fee, pubkey),
        "blockNumber": hex(1500),
    }


def fixed_block_time(ts: float | None):
    async def bt(rpc, block, prefer=None):
        return ts

    return bt


async def a_quote(mock_db, account_id: str, address: str, at: float, **over) -> dict[str, Any]:
    """A `direct` quote exactly as POST /v1/quote stores one, armed with our calldata."""
    doc = {
        "_id": "q-" + str(int(at * 1000))[-9:] + str(over.pop("n", 0)),
        "account_id": account_id,
        "address": address,
        "asset": "ETH",
        "mode": "direct",
        "src": {"chain_id": 1, "token": ZERO, "amount": str(VALUE + FEE)},
        "out_units": str(VALUE),
        "value_units": str(VALUE),
        "relayer_fee_units": str(FEE),
        "value_groth": VALUE // ETH.grid,
        "metadata": "0x0102030405",
        "pubkey": PUBKEY,
        "hook_calldata": ethpipe.encode_send_funds(VALUE, FEE, PUBKEY),
        "armed": True,
        "at": at,
        "expires_at": at + 900,
    }
    doc.update(over)
    await mock_db["pgasme_test"].quotes.insert_one(doc)
    return doc


# ═══════════════════════ 1. the pool, not the first endpoint that answers ═══════════════════════


async def test_the_visibility_check_asks_every_endpoint_not_only_the_first(
    client, user, armed_eth, rpc, mock_db
):
    """THE INCIDENT ITSELF. Four endpoints; the first two answer `null` for a transaction that
    exists and the third has it. `null` is an ANSWER, so the old reader stopped at the first one
    and called a real deposit invisible."""
    q = await direct_quote(client, user)
    rpc.urls = ["https://a", "https://b", "https://c", "https://d"]
    rpc.txs_by_url = {
        "https://a": {},
        "https://b": {},
        "https://c": {TX: pipe_call(user["address"])},
        "https://d": {},
    }
    r = await register(client, user, q["quote_id"], TX)
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True and "note" not in r.json()
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": r.json()["deposit_id"]})
    assert dep["verified"] is True and "unseen_since" not in dep


async def test_an_endpoint_that_cannot_answer_at_all_is_not_a_no(client, user, armed_eth, rpc):
    """A dead endpoint is skipped, not believed. The one behind it holds the transaction."""
    q = await direct_quote(client, user)
    rpc.urls = ["https://dead", "https://good"]
    rpc.dead_urls = {"https://dead"}
    rpc.txs_by_url = {"https://good": {TX: pipe_call(user["address"])}}
    assert (await register(client, user, q["quote_id"], TX)).json()["verified"] is True


async def test_a_pool_where_nobody_answers_is_a_503_never_a_rejection(
    client, user, armed_eth, rpc, mock_db
):
    """§an unreadable query is not evidence of anything. NOBODY answering is not "not visible" —
    it is "ask again", and no row is opened on it either."""
    q = await direct_quote(client, user)
    rpc.urls = ["https://dead1", "https://dead2"]
    rpc.dead_urls = {"https://dead1", "https://dead2"}
    r = await register(client, user, q["quote_id"], TX)
    assert r.status_code == 503 and "could not read that transaction" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0


# ═══════════════════════ 2. unseen is a row, not a refusal ══════════════════════════════════════


async def test_an_unseen_hash_opens_a_submitted_unverified_row(client, user, armed_eth, rpc, mock_db):
    q = await direct_quote(client, user)
    r = await register(client, user, q["quote_id"], TX)  # rpc.txs is empty: every endpoint says no
    assert r.status_code == 200, r.text
    assert r.json() == {
        "deposit_id": r.json()["deposit_id"],
        "status": "submitted",
        "verified": False,
        "note": "waiting for Ethereum to see it",
    }
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": r.json()["deposit_id"]})
    assert dep["status"] == "submitted" and dep["verified"] is False
    assert dep["src_tx_hash"] == TX and dep["unseen_since"] > 0 and dep["unseen_reason"]
    shown = (await client.get(f"/v1/deposits/{dep['_id']}", headers=user["headers"])).json()
    assert shown["verified"] is False and shown["note"] == "waiting for Ethereum to see it"
    # …and it is a refusal of nothing: no mismatch was recorded against the quote
    assert await mock_db["pgasme_test"].events.count_documents({"kind": "deposit_mismatch"}) == 0


async def test_a_visible_transaction_that_is_not_this_quotes_is_still_refused(
    client, user, armed_eth, rpc, mock_db
):
    """Accepting what we cannot see does NOT mean accepting what we can. A readable transaction
    that fails identity is refused 400, with the row in the event log that makes it alertable."""
    q = await direct_quote(client, user)
    rpc.txs[TX] = pipe_call(EthAccount.create().address)  # somebody else's pipe call
    r = await register(client, user, q["quote_id"], TX)
    assert r.status_code == 400 and "not sent from the wallet" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0
    assert await mock_db["pgasme_test"].events.count_documents({"kind": "deposit_mismatch"}) == 1


async def test_re_registering_the_same_hash_answers_the_same_shape(client, user, armed_eth, rpc):
    q = await direct_quote(client, user)
    first = await register(client, user, q["quote_id"], TX)
    again = await register(client, user, q["quote_id"], TX)
    assert again.status_code == 200 and again.json() == first.json()


# ═══════════════════════ 3. the watcher proves it, fails it, or leaves it alone ═════════════════


async def test_reverify_flips_an_unseen_row_to_verified_with_the_eth_fields(
    client, user, armed_eth, rpc, mock_db
):
    q = await direct_quote(client, user)
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    rpc.txs[TX] = pipe_call(user["address"])  # …and now an endpoint can see it
    assert await workers.chain_secondary() == 1
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": dep_id})
    assert dep["verified"] is True and dep["verified_by"] == "chain"
    assert dep["eth"]["value_units"] == str(VALUE) and dep["eth"]["relayer_fee_units"] == str(FEE)
    assert dep["eth"]["tx_block"] == 1500 and "unseen_since" not in dep
    assert "note" not in dep  # it is not "waiting for Ethereum to see it" any more
    # …and it is not asked again on the next pass
    assert await workers.chain_secondary() == 0


async def test_an_unreadable_pool_never_advances_an_unseen_row(
    client, user, armed_eth, rpc, mock_db
):
    q = await direct_quote(client, user)
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": dep_id}, {"$set": {"unseen_since": time.time() - 10 * 3600}}
    )
    rpc.dead_urls = set(rpc.urls)  # nobody answers: not a verdict, in either direction
    await workers.chain_secondary()
    assert (await mock_db["pgasme_test"].deposits.find_one({"_id": dep_id}))["status"] == "submitted"


async def test_an_unseen_row_fails_after_the_ttl_and_pages_exactly_once(
    client, user, armed_eth, rpc, mock_db, monkeypatch
):
    monkeypatch.setattr(settings, "unseen_tx_ttl_s", 7200)
    q = await direct_quote(client, user)
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    d = mock_db["pgasme_test"]
    await d.deposits.update_one({"_id": dep_id}, {"$set": {"unseen_since": time.time() - 7201}})
    await workers.chain_secondary()
    dep = await d.deposits.find_one({"_id": dep_id})
    assert dep["status"] == "failed" and dep["verified"] is False
    assert "no Ethereum endpoint has seen this transaction" in dep["note"]
    # the hash goes back to whoever really signed it: it was never proven to be anyone's
    assert dep.get("src_tx_hash") is None and dep["src_tx_hash_unseen"] == TX
    assert await d.events.count_documents({"kind": "deposit_unseen"}) == 1
    # …and it cannot page again: the row is no longer `submitted`, so the branch is unreachable
    await workers.chain_secondary()
    await workers.chain_secondary()
    assert await d.events.count_documents({"kind": "deposit_unseen"}) == 1


async def test_the_ttl_never_fails_a_row_whose_money_has_landed(
    client, user, armed_eth, rpc, mock_db, our_key
):
    """A locked deposit was proven by the pipe log itself. The weaker check must never undo it."""
    q = await direct_quote(client, user)
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    d = mock_db["pgasme_test"]
    await d.deposits.update_one(
        {"_id": dep_id},
        {"$set": {"unseen_since": time.time() - 99999, "status": "locked", "eth.tx": TX}},
    )
    await workers.chain_secondary()
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "locked"
    assert await d.events.count_documents({"kind": "deposit_unseen"}) == 0


async def test_reverify_fails_a_row_whose_transaction_turns_out_to_be_someone_elses(
    client, user, armed_eth, rpc, mock_db
):
    """The other side of accepting an unseen hash: once it IS readable, the same identity check
    registration would have run decides — and a foreign transaction releases the hash."""
    q = await direct_quote(client, user)
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    rpc.txs[TX] = pipe_call(EthAccount.create().address)
    await workers.chain_secondary()
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": dep_id})
    assert dep["status"] == "failed" and dep.get("src_tx_hash") is None
    assert dep["src_tx_hash_rejected"] == TX and "deposit mismatch" in dep["note"]


async def test_the_secondary_pass_never_asks_the_order_router_about_a_direct_row(
    client, user, armed_eth, rpc, monkeypatch
):
    async def boom(tx_hash: str, timeout: float | None = None):
        raise AssertionError("a direct deposit has no cross-chain order to look up")

    q = await direct_quote(client, user)
    await register(client, user, q["quote_id"], TX)
    monkeypatch.setattr(xchain, "order_ids_by_tx", boom)
    assert await workers.xchain_secondary() == 0  # the direct row is skipped there…
    rpc.txs[TX] = pipe_call(user["address"])
    assert await workers.chain_secondary() == 1  # …and proven here instead


# ═══════════════════════ 4. a lock nobody registered, matched to its quote ══════════════════════


async def test_a_lock_with_no_row_is_attributed_to_its_quote_and_credited(
    mock_db, our_key, rpc, monkeypatch
):
    """THE MSGID-138 SHAPE: the pipe call landed, the registration never did. The 394 bytes of
    calldata in it are ours — the quote we issued them from is the identity."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    q = await a_quote(mock_db, "acct1", wallet, now - 60)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 138, VALUE, FEE, PUBKEY, 1500, TX, 76))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 1 and st["unattributed"] == 0
    d = mock_db["pgasme_test"]
    dep = await d.deposits.find_one({"quote_id": q["_id"]})
    assert dep["status"] == "locked" and dep["mode"] == "direct" and dep["verified"] is True
    assert dep["account_id"] == "acct1" and dep["address"] == wallet
    assert dep["src_tx_hash"] == TX and dep["eth"]["msg_id"] == 138
    assert "attributed from the quote" in dep["note"] and dep["verified_by"] == "quote-calldata"
    ev = await d.events.find_one({"kind": "deposit_locked"})
    assert "attributed from the quote" in ev["text"]
    assert await d.unattributed_locks.count_documents({}) == 0
    await workers.confirm_locked(head=1520)
    assert (await d.deposits.find_one({"_id": dep["_id"]}))["status"] == "credited"
    assert (await ledger.balance("acct1", "ETH"))["available"] == VALUE // ETH.grid


async def test_an_already_unattributed_lock_is_credited_on_the_next_retry(
    mock_db, our_key, rpc, monkeypatch
):
    """PART 1's own path: the lock was recorded and paged BEFORE the quote could be matched
    (the code that could do it did not exist). `retry_unattributed` picks it up and resolves it,
    with no hand-editing of any row."""
    wallet = EthAccount.create().address
    now = time.time()
    rpc.logs_.append(lock_log(ETH.pipe, 138, VALUE, FEE, PUBKEY, 1500, TX, 76))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 1  # no quote yet: manual
    d = mock_db["pgasme_test"]
    assert (await d.unattributed_locks.find_one({}))["status"] == "open"
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    await a_quote(mock_db, "acct1", wallet, now - 60)
    rpc.txs[TX] = pipe_call(wallet)
    assert await scanner.retry_unattributed(rpc) == 1
    lock = await d.unattributed_locks.find_one({})
    assert lock["status"] == "resolved" and lock["deposit_id"]
    dep = await d.deposits.find_one({"_id": lock["deposit_id"]})
    assert dep["status"] == "locked" and dep["verified"] is True and dep["src_tx_hash"] == TX


async def test_two_quotes_of_one_account_take_the_locks_in_order(
    mock_db, our_key, rpc, monkeypatch
):
    """Identical calldata twice — the 2026-09-10 pair. Same account either way, so the money is
    the same whichever quote is spent; what the rule decides is whether the OTHER lock still has
    one. Each lock takes the NEAREST QUOTE PRECEDING ITS OWN BLOCK, which pairs them in the order
    both happened (§NEAREST-PRECEDING) — a quote issued after a block cannot have built it."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", block_times({1500: now - 2000, 1501: now - 500}))
    monkeypatch.setattr(settings, "quote_attribution_window_s", 86400)
    first = await a_quote(mock_db, "acct1", wallet, now - 3000, n=1)
    second = await a_quote(mock_db, "acct1", wallet, now - 1000, n=2)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.txs[TX2] = pipe_call(wallet)
    rpc.logs_ += [
        lock_log(ETH.pipe, 138, VALUE, FEE, PUBKEY, 1500, TX, 76),
        lock_log(ETH.pipe, 139, VALUE, FEE, PUBKEY, 1501, TX2, 140),
    ]
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    rpc.receipts[TX2] = {"from": wallet, "logs": [rpc.logs_[1]]}
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 2
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"quote_id": first["_id"]}))["src_tx_hash"] == TX
    assert (await d.deposits.find_one({"quote_id": second["_id"]}))["src_tx_hash"] == TX2
    assert await d.deposits.count_documents({}) == 2


async def test_a_lock_no_quote_matches_is_unattributed_and_pages_as_before(
    mock_db, our_key, rpc, monkeypatch
):
    """The MANUAL HANDLING page survives for locks that really are nobody's — a wallet that
    called the pipe with our pubkey without ever asking us for a quote."""
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(time.time()))
    stranger = EthAccount.create().address
    rpc.txs[TX] = pipe_call(stranger)
    rpc.logs_.append(lock_log(ETH.pipe, 140, VALUE, FEE, PUBKEY, 1500, TX, 3))
    rpc.receipts[TX] = {"from": stranger, "logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["unattributed"] == 1 and st["locked"] == 0
    d = mock_db["pgasme_test"]
    row = await d.unattributed_locks.find_one({})
    assert row["status"] == "open" and "not a cross-chain fill" in row["reason"]
    assert "no quote of ours was armed with the calldata" in row["reason"]
    ev = await d.events.find_one({"kind": "lock_unattributed"})
    assert "MANUAL HANDLING REQUIRED" in ev["text"]
    assert await d.deposits.count_documents({}) == 0 and await d.entries.count_documents({}) == 0


async def test_a_quote_created_after_the_block_is_not_that_locks_quote(
    mock_db, our_key, rpc, monkeypatch
):
    """A quote issued AFTER the transaction was mined cannot be the one it was built from —
    however well the calldata matches."""
    wallet = EthAccount.create().address
    block_time = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(block_time))
    await a_quote(mock_db, "acct1", wallet, block_time + scanner.QUOTE_AFTER_BLOCK_SLACK_S + 60)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 141, VALUE, FEE, PUBKEY, 1500, TX, 4))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 1
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0


async def test_a_quote_older_than_the_window_is_not_a_candidate(
    mock_db, our_key, rpc, monkeypatch
):
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    monkeypatch.setattr(settings, "quote_attribution_window_s", 3600)
    wallet = EthAccount.create().address
    await a_quote(mock_db, "acct1", wallet, now - 7200)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 142, VALUE, FEE, PUBKEY, 1500, TX, 5))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 1


async def test_two_accounts_matching_one_pipe_call_stay_on_the_operators_desk(
    mock_db, our_key, rpc, monkeypatch
):
    """It should not be constructible — the calldata carries the amount and the pubkey, and the
    sender is in the transaction — so if it ever happens the scanner refuses to guess."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    await a_quote(mock_db, "acctA", wallet, now - 300, n=1)
    await a_quote(mock_db, "acctB", wallet, now - 200, n=2)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 143, VALUE, FEE, PUBKEY, 1500, TX, 6))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 1
    row = await mock_db["pgasme_test"].unattributed_locks.find_one({})
    assert "2 different accounts" in row["reason"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0


async def test_a_registered_row_still_wins_over_the_quote_match(mock_db, our_key, rpc, monkeypatch):
    """The quote path is the LAST resort, never a second claimant: a row that registered this
    hash is the one that gets the lock."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    q = await a_quote(mock_db, "acct1", wallet, now - 60)
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "dep-registered",
            "account_id": "acct1",
            "address": wallet,
            "asset": "ETH",
            "mode": "direct",
            "status": "submitted",
            "quote_id": q["_id"],
            "src": q["src"],
            "src_tx_hash": TX,
            "order_id": None,
            "eth": {"value_units": str(VALUE), "relayer_fee_units": str(FEE)},
            "value_groth": VALUE // ETH.grid,
            "pubkey": PUBKEY,
            "created_at": now,
            "updated_at": now,
        }
    )
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 144, VALUE, FEE, PUBKEY, 1500, TX, 7))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 1
    d = mock_db["pgasme_test"]
    assert await d.deposits.count_documents({}) == 1
    assert (await d.deposits.find_one({"_id": "dep-registered"}))["status"] == "locked"


# ═══════════════════════ 5. a failed row does not spend its quote (H1) ══════════════════════════


def secrets_hex() -> str:
    import secrets as _s

    return _s.token_hex(4)


async def failed_row(mock_db, q: dict[str, Any], **over: Any) -> str:
    """A row for this quote that ended terminal-failed — the TTL path, exactly as
    `workers._reverify_chain` writes it: the hash released, the quote_id kept as evidence."""
    row = {
        "_id": "dep-failed-" + secrets_hex(),
        "account_id": q["account_id"],
        "address": q["address"],
        "asset": q["asset"],
        "mode": "direct",
        "status": "failed",
        "verified": False,
        "quote_id": q["_id"],
        "src_tx_hash_unseen": TX2,
        "note": "no Ethereum endpoint has seen this transaction in 120 min.",
        "src": q["src"],
        "eth": {"value_units": q["value_units"], "relayer_fee_units": q["relayer_fee_units"]},
        "value_groth": q["value_groth"],
        "created_at": q["at"],
        "updated_at": q["at"],
    }
    row.update(over)
    await mock_db["pgasme_test"].deposits.insert_one(row)
    return row["_id"]


async def test_a_quote_whose_only_row_failed_is_still_that_locks_quote(
    mock_db, our_key, rpc, monkeypatch
):
    """⛔ H1. The TTL failed the row and RELEASED the hash — but the row kept `quote_id`, so the
    last gate ("does any deposit row carry this quote?") read the quote as spent FOREVER and the
    lock went to MANUAL HANDLING although its calldata is ours. A terminal-failed row is
    evidence, never a claim."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    q = await a_quote(mock_db, "acct1", wallet, now - 60)
    dead = await failed_row(mock_db, q)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 150, VALUE, FEE, PUBKEY, 1500, TX, 9))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 1 and st["unattributed"] == 0
    d = mock_db["pgasme_test"]
    dep = await d.deposits.find_one({"quote_id": q["_id"], "status": "locked"})
    assert dep and dep["account_id"] == "acct1" and dep["src_tx_hash"] == TX
    # the failed row is NAMED on the new one: why a second row for one quote is legitimate
    assert dead in dep["note"]
    assert (await d.deposits.find_one({"_id": dead}))["status"] == "failed"  # never rewritten


async def test_a_live_row_does_not_strand_a_second_lock_priced_by_the_same_quote(
    mock_db, our_key, rpc, monkeypatch
):
    """⛔ The other side of the same gate, REVERSED on 2026-09-10 (T37d). A live row for this
    quote used to make the quote read as spent and sent the lock to MANUAL HANDLING — with the
    money already burned on Ethereum. A quote PRICES a deposit, it does not fund one: the second
    lock gets its own row, marked, and the row that holds the other transaction is untouched."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    q = await a_quote(mock_db, "acct1", wallet, now - 60)
    await failed_row(mock_db, q, _id="dep-live", status="submitted", src_tx_hash=TX2)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 151, VALUE, FEE, PUBKEY, 1500, TX, 10))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 1 and st["unattributed"] == 0
    d = mock_db["pgasme_test"]
    assert await d.unattributed_locks.count_documents({}) == 0
    new_row = await d.deposits.find_one({"src_tx_hash": TX})
    assert new_row["_id"] != "dep-live" and new_row["status"] == "locked"
    assert new_row["quote_reused"] is True and new_row["quote_reused_of"] == "dep-live"
    # the row that carries the OTHER transaction is not moved, not locked, not rewritten
    live = await d.deposits.find_one({"_id": "dep-live"})
    assert live["status"] == "submitted" and live["src_tx_hash"] == TX2


# ═══════════════════════ 6. a hash is a CLAIM until identity decides (M1) ═══════════════════════


async def second_user(client) -> dict[str, Any]:
    from conftest import sign_in

    return await sign_in(client, EthAccount.create())


async def test_a_stranger_cannot_squat_an_unseen_hash(client, user, armed_eth, rpc, mock_db):
    """⛔ M1. An unseen hash used to take `uniq_src_tx_hash` for whoever posted it FIRST, so a
    stranger who watched the mempool could lock the person who actually signed it out with a 409
    until the TTL. An UNVERIFIED registration is a claim, not a title: several may exist."""
    squatter = await second_user(client)
    qs = await direct_quote(client, squatter)
    assert (await register(client, squatter, qs["quote_id"], TX)).status_code == 200
    q = await direct_quote(client, user)
    mine = await register(client, user, q["quote_id"], TX)
    assert mine.status_code == 200, mine.text
    assert mine.json()["verified"] is False
    d = mock_db["pgasme_test"]
    assert await d.deposits.count_documents({"src_tx_hash": TX}) == 2


async def test_identity_picks_the_owner_and_fails_the_other_claims_without_paging(
    client, user, armed_eth, rpc, mock_db
):
    """The claims are resolved by the ONE thing that is not public: who sent the transaction.
    The loser is a refusal, not an incident — a row with a note, and no page (law 15)."""
    squatter = await second_user(client)
    qs = await direct_quote(client, squatter)
    squat_id = (await register(client, squatter, qs["quote_id"], TX)).json()["deposit_id"]
    q = await direct_quote(client, user)
    mine_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    rpc.txs[TX] = pipe_call(user["address"])  # the chain can see it now: it is the user's
    await workers.chain_secondary()
    d = mock_db["pgasme_test"]
    won = await d.deposits.find_one({"_id": mine_id})
    lost = await d.deposits.find_one({"_id": squat_id})
    assert won["verified"] is True and won["src_tx_hash"] == TX
    assert lost["status"] == "failed" and lost.get("src_tx_hash") is None
    assert lost["src_tx_hash_rejected"] == TX and "was not proven" in lost["note"]
    assert await d.events.count_documents({"kind": "deposit_mismatch"}) == 0
    # …and the losing account is told, on its own row, without being told whose it was
    shown = (await client.get(f"/v1/deposits/{squat_id}", headers=squatter["headers"])).json()
    assert shown["status"] == "failed" and user["address"].lower() not in str(shown).lower()


async def test_the_loser_is_quiet_whichever_claim_the_pass_reaches_first(
    client, user, armed_eth, rpc, mock_db
):
    """Contention is explained by the contention: a claim that loses is failed quietly whether
    the winner has been proven yet or not. Only a LONE mismatch — nobody else claimed it — is
    the incident `_mismatch` pages about."""
    squatter = await second_user(client)
    qs = await direct_quote(client, squatter)
    squat_id = (await register(client, squatter, qs["quote_id"], TX)).json()["deposit_id"]
    q = await direct_quote(client, user)
    await register(client, user, q["quote_id"], TX)
    rpc.txs[TX] = pipe_call(user["address"])
    d = mock_db["pgasme_test"]
    # reach the squatter's row FIRST, on its own
    await workers._reverify_chain(await d.deposits.find_one({"_id": squat_id}))
    assert (await d.deposits.find_one({"_id": squat_id}))["status"] == "failed"
    assert await d.events.count_documents({"kind": "deposit_mismatch"}) == 0


async def test_a_lone_mismatch_still_pages(client, user, armed_eth, rpc, mock_db):
    """The existing alert is not weakened: one row, one foreign transaction, one page."""
    q = await direct_quote(client, user)
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    rpc.txs[TX] = pipe_call(EthAccount.create().address)
    await workers.chain_secondary()
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "failed"
    assert await d.events.count_documents({"kind": "deposit_mismatch"}) == 1


async def test_a_verified_row_owns_its_hash_and_a_later_claim_is_refused(
    client, user, armed_eth, rpc, mock_db
):
    """Once identity has spoken, the hash IS taken — the unique index only starts applying then,
    and the route says so before the index has to."""
    q = await direct_quote(client, user)
    rpc.txs[TX] = pipe_call(user["address"])
    assert (await register(client, user, q["quote_id"], TX)).json()["verified"] is True
    late = await second_user(client)
    ql = await direct_quote(client, late)
    r = await register(client, late, ql["quote_id"], TX)
    assert r.status_code == 409 and "already registered" in r.json()["detail"]


async def test_the_lock_goes_to_the_owners_quote_not_to_a_squatters_claim(
    client, user, armed_eth, rpc, mock_db, monkeypatch
):
    """The whole point, at the moment money lands: the owner never registered, a stranger's
    unverified claim carries the hash, and the pipe call's calldata is still ours."""
    squatter = await second_user(client)
    qs = await direct_quote(client, squatter)
    squat_id = (await register(client, squatter, qs["quote_id"], TX)).json()["deposit_id"]
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    wallet = EthAccount.create().address
    q = await a_quote(mock_db, "acct-owner", wallet, now - 60)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 152, VALUE, FEE, PUBKEY, 1500, TX, 11))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 1
    d = mock_db["pgasme_test"]
    dep = await d.deposits.find_one({"quote_id": q["_id"]})
    assert dep["account_id"] == "acct-owner" and dep["status"] == "locked"
    assert (await d.deposits.find_one({"_id": squat_id}))["status"] != "locked"
    await workers.confirm_locked(head=1520)
    assert (await ledger.balance("acct-owner", "ETH"))["available"] == VALUE // ETH.grid
    assert (await ledger.balance(squatter["account_id"], "ETH"))["available"] == 0


# ═══════════════════════ 7. registrations are bounded (M2) ══════════════════════════════════════


async def test_registrations_are_capped_per_account(client, user, armed_eth, rpc, monkeypatch):
    monkeypatch.setattr(settings, "deposit_account_limit", 3)
    q = await direct_quote(client, user)
    for i in range(3):
        r = await register(client, user, q["quote_id"], "0x" + f"{i:02x}" * 32)
        assert r.status_code == 200, r.text
    r = await register(client, user, q["quote_id"], "0x" + "99" * 32)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1


async def test_one_unseen_row_per_quote_and_the_latest_hash_wins(
    client, user, armed_eth, rpc, mock_db
):
    """A quote funds ONE deposit. A wallet that was re-signed hands back a second hash, and an
    unverified row is not evidence of anything — so the row moves to the new hash instead of
    opening a second row that can never be credited."""
    q = await direct_quote(client, user)
    first = await register(client, user, q["quote_id"], TX)
    second = await register(client, user, q["quote_id"], TX2)
    assert second.status_code == 200 and second.json()["deposit_id"] == first.json()["deposit_id"]
    d = mock_db["pgasme_test"]
    assert await d.deposits.count_documents({"quote_id": q["quote_id"]}) == 1
    dep = await d.deposits.find_one({"quote_id": q["quote_id"]})
    assert dep["src_tx_hash"] == TX2 and dep["src_tx_hash_superseded"] == TX
    assert "replaced" in dep["note"]


async def test_a_verified_row_is_never_replaced_by_a_second_hash(
    client, user, armed_eth, rpc, mock_db
):
    q = await direct_quote(client, user)
    rpc.txs[TX] = pipe_call(user["address"])
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    r = await register(client, user, q["quote_id"], TX2)
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"_id": dep_id}))["src_tx_hash"] == TX
    assert r.status_code in (200, 409)
    if r.status_code == 200:  # a second row, never a rewrite of the proven one
        assert r.json()["deposit_id"] != dep_id


async def test_an_unseen_registration_logs_and_only_verification_pages(
    client, user, armed_eth, rpc, mock_db
):
    """A row opened because NOBODY could see the transaction is not a deposit yet. Paging on it
    would page on anything anyone types; the event is written when the row becomes verified."""
    d = mock_db["pgasme_test"]
    q = await direct_quote(client, user)
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    assert await d.events.count_documents({"kind": "deposit_submitted"}) == 0
    rpc.txs[TX] = pipe_call(user["address"])
    await workers.chain_secondary()
    ev = await d.events.find_one({"kind": "deposit_submitted"})
    assert ev and ev["deposit_id"] == dep_id
    await workers.chain_secondary()  # …and exactly once
    assert await d.events.count_documents({"kind": "deposit_submitted"}) == 1


async def test_a_visible_registration_still_emits_the_event_at_once(
    client, user, armed_eth, rpc, mock_db
):
    q = await direct_quote(client, user)
    rpc.txs[TX] = pipe_call(user["address"])
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "deposit_submitted"})
    assert ev and ev["deposit_id"] == dep_id


# ═══════════════════════ 8. the wire never carries endpoint diagnostics (M3) ════════════════════


async def test_public_deposit_never_returns_endpoint_diagnostics(
    client, user, armed_eth, rpc, mock_db, monkeypatch
):
    """⛔ The row is where the operator reads WHICH endpoint said what; the wire is not. A URL,
    an API key in a query string or a provider's error body must never reach the account page."""
    monkeypatch.setattr(settings, "unseen_tx_ttl_s", 1)
    q = await direct_quote(client, user)
    rpc.urls = ["https://secret-key.example/rpc?apikey=abc", "https://good"]
    rpc.dead_urls = {"https://secret-key.example/rpc?apikey=abc"}
    dep_id = (await register(client, user, q["quote_id"], TX)).json()["deposit_id"]
    d = mock_db["pgasme_test"]
    await d.deposits.update_one({"_id": dep_id}, {"$set": {"unseen_since": time.time() - 10}})
    await workers.chain_secondary()
    row = await d.deposits.find_one({"_id": dep_id})
    assert row["status"] == "failed" and row["unseen_errors"]  # the evidence stays ON THE ROW
    one = (await client.get(f"/v1/deposits/{dep_id}", headers=user["headers"])).json()
    acct = (await client.get("/v1/account", headers=user["headers"])).json()
    for body in (one, acct):
        text = str(body)
        assert "unseen_errors" not in text and "apikey" not in text
        assert "https://" not in text and "http://" not in text
    assert "no Ethereum endpoint has seen this transaction" in one["note"]


def test_the_serialiser_names_every_private_field():
    """The deny-list is the contract; a new diagnostic field has to be added to it on purpose."""
    from pgasme.routers.account import PRIVATE_DEPOSIT_FIELDS, public_deposit

    row = dict.fromkeys(PRIVATE_DEPOSIT_FIELDS, "https://rpc.example/secret")
    out = public_deposit({**row, "_id": "d1", "status": "failed"})
    assert not set(out) & set(PRIVATE_DEPOSIT_FIELDS)
    assert {"account_id", "pubkey", "unseen_errors"} <= set(PRIVATE_DEPOSIT_FIELDS)


# ═══════════════════════ 9. the pool is asked all at once (L1) ══════════════════════════════════


def patched_post(monkeypatch, fn):
    monkeypatch.setattr(ethpipe.Rpc, "_post", fn)


async def test_transaction_anywhere_asks_every_endpoint_concurrently(monkeypatch):
    """Four endpoints × the 8 s pool timeout is 32 s inside a request the user is waiting on.
    They are independent questions: ask them at once and take the first that HAS it."""
    import asyncio

    async def slow(self, c, url, method, params):
        await asyncio.sleep(0.25)
        return {"from": "0x" + "11" * 20} if url == "https://d" else None

    patched_post(monkeypatch, slow)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b", "https://c", "https://d"], timeout=8.0)
    t0 = time.monotonic()
    tx, url, answered, errors = await rpc.transaction_anywhere(TX)
    elapsed = time.monotonic() - t0
    assert tx and url == "https://d" and answered >= 1
    assert elapsed < 0.75, f"{elapsed:.2f}s — that is serial, not concurrent"


async def test_every_endpoint_answering_null_is_unseen_not_unreadable(monkeypatch):
    async def nothing(self, c, url, method, params):
        return None

    patched_post(monkeypatch, nothing)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b", "https://c"], timeout=8.0)
    tx, url, answered, errors = await rpc.transaction_anywhere(TX)
    assert tx is None and url is None and answered == 3 and errors == []


async def test_a_pool_that_runs_out_of_time_is_unreadable_never_a_no(monkeypatch):
    """§an unreadable query is not evidence of anything. A deadline that expired says NOTHING
    about the transaction — `answered` stays 0, so registration answers 503 and reverify holds."""
    import asyncio

    monkeypatch.setattr(settings, "tx_lookup_deadline_s", 0.2)

    async def hang(self, c, url, method, params):
        await asyncio.sleep(30)
        return None

    patched_post(monkeypatch, hang)
    rpc = ethpipe.Rpc(urls=["https://a", "https://b"], timeout=8.0)
    t0 = time.monotonic()
    tx, url, answered, errors = await rpc.transaction_anywhere(TX)
    assert tx is None and answered == 0 and len(errors) == 2
    assert time.monotonic() - t0 < 2.0


async def test_one_slow_endpoint_never_delays_the_one_that_has_it(monkeypatch):
    import asyncio

    async def mixed(self, c, url, method, params):
        if url == "https://slow":
            await asyncio.sleep(30)
        return {"from": "0x" + "11" * 20}

    patched_post(monkeypatch, mixed)
    monkeypatch.setattr(settings, "tx_lookup_deadline_s", 5.0)
    rpc = ethpipe.Rpc(urls=["https://slow", "https://fast"], timeout=8.0)
    t0 = time.monotonic()
    tx, url, answered, errors = await rpc.transaction_anywhere(TX)
    assert tx and url == "https://fast"
    assert time.monotonic() - t0 < 2.0


# ═══════════════════════ 10. the index the claims design needs (M1) ═════════════════════════════


async def test_a_box_carrying_the_old_wide_index_is_upgraded_not_left_broken(mock_db):
    """⛔ The claims design is only real if the DATABASE stops enforcing the old rule. A box that
    already carries `uniq_src_tx_hash` over every string hash keeps refusing the second claim
    with a DuplicateKeyError — the 409 lockout, one layer down — unless boot repairs it. This is
    that repair, with the plain lookup index surviving it (it is built after, on purpose)."""
    from pgasme.db import DEPOSIT_HASH_INDEX, db, ensure_indexes

    d = mock_db["pgasme_test"]
    await d.deposits.create_index(
        "src_tx_hash",
        unique=True,
        partialFilterExpression={"src_tx_hash": {"$type": "string"}},  # the OLD, wider rule
        name=DEPOSIT_HASH_INDEX,
    )
    await d.deposits.create_index("src_tx_hash")
    assert await ensure_indexes() == []
    info = await db().deposits.index_information()
    assert info[DEPOSIT_HASH_INDEX]["partialFilterExpression"] == {
        "src_tx_hash": {"$type": "string"},
        "verified": True,
    }
    assert "src_tx_hash_1" in info  # the {src_tx_hash: null} lookup keeps its index
    await d.deposits.insert_one({"_id": "c1", "src_tx_hash": TX, "verified": False})
    await d.deposits.insert_one({"_id": "c2", "src_tx_hash": TX, "verified": False})
    await d.deposits.insert_one({"_id": "w", "src_tx_hash": TX, "verified": True})
    with pytest.raises(Exception, match="[Dd]uplicate"):
        await d.deposits.insert_one({"_id": "w2", "src_tx_hash": TX, "verified": True})


async def test_an_address_less_legacy_claim_never_outranks_the_row_that_names_the_sender(
    mock_db, our_key, rpc, monkeypatch
):
    """Two rows claim one hash: an old one from before `address` was stored (checkable only on
    its amount) and the sender's own. Identity ranks first — one pass over a list sorted by age
    would have handed this lock to whichever sorted first."""
    wallet = EthAccount.create().address
    now = time.time()
    d = mock_db["pgasme_test"]
    common = {
        "asset": "ETH",
        "mode": "direct",
        "status": "submitted",
        "src": {"chain_id": 1, "token": ZERO, "amount": str(VALUE + FEE)},
        "src_tx_hash": TX,
        "order_id": None,
        "eth": {"value_units": str(VALUE), "relayer_fee_units": str(FEE)},
        "value_groth": VALUE // ETH.grid,
        "pubkey": PUBKEY,
        "updated_at": now,
    }
    await d.deposits.insert_one(
        {**common, "_id": "legacy", "account_id": "acct-legacy", "created_at": now - 600}
    )
    await d.deposits.insert_one(
        {**common, "_id": "owner", "account_id": "acct1", "address": wallet, "created_at": now}
    )
    rpc.txs[TX] = pipe_call(wallet)
    rpc.logs_.append(lock_log(ETH.pipe, 153, VALUE, FEE, PUBKEY, 1500, TX, 12))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 1
    assert (await d.deposits.find_one({"_id": "owner"}))["status"] == "locked"
    assert (await d.deposits.find_one({"_id": "legacy"}))["status"] == "submitted"


# ═══════ 11. a quote is a PRICE statement, not a one-time ticket (the msgId-138 strand) ═════════
#
# 2026-09-10, on the box: wallet 0x0210a544… sent TWO identical direct deposits — lock 138
# (block 25946453, 10:40:59Z) and lock 140 (11:38). The account held six `direct` quotes with
# byte-identical calldata, and only ONE of them (10:40:03Z) predates lock 138's block.
# `retry_unattributed` reached lock 140 first (its own backoff decided that), attribute_from_quote
# handed it the OLDEST unused matching quote — the only one lock 138 could ever have used — and
# lock 138 was then refused with "every quote of this account that matches this pipe call already
# has a deposit row". 0.0019999 ETH of a real user's money sat stranded until abandonment.
#
# Two rules come out of it, and both are here: quotes pair to locks NEAREST-PRECEDING first, and
# a quote that is already spent is still a price, so a second lock priced by it is credited on
# its own row rather than refused.


def block_times(by_block: dict[int, float], default: float | None = None):
    """Per-block timestamps — the chain stamps each block, `fixed_block_time` gives them all one."""

    async def bt(rpc, block, prefer=None):
        return by_block.get(int(block), default)

    return bt


async def test_the_138_140_shape_credits_both_locks_once_each(mock_db, our_key, rpc, monkeypatch):
    """⛔ THE STRAND ITSELF. Six quotes, two locks, and only the first quote predates the first
    lock's block. The LATER lock is attributed FIRST (that is what the retry backoff did), so
    "the oldest unused quote wins" spends the only quote the earlier lock has and strands real
    money. Nearest-preceding pairing spends the right one."""
    wallet = EthAccount.create().address
    t138 = time.time() - 4000.0
    t140 = t138 + 3421.0  # 10:40:59Z → 11:38Z
    monkeypatch.setattr(scanner, "_block_time", block_times({1500: t138, 1600: t140}))
    monkeypatch.setattr(settings, "quote_attribution_window_s", 86400)
    first = await a_quote(mock_db, "acct1", wallet, t138 - 56, n=0)  # the 10:40:03Z quote
    later = [
        await a_quote(mock_db, "acct1", wallet, t138 + 600 * i, n=i) for i in range(1, 6)
    ]
    rpc.txs[TX] = pipe_call(wallet)
    rpc.txs[TX2] = pipe_call(wallet)
    # the scanner meets lock 140 FIRST — the order the retry loop reached them in
    rpc.logs_ += [
        lock_log(ETH.pipe, 140, VALUE, FEE, PUBKEY, 1600, TX2, 12),
        lock_log(ETH.pipe, 138, VALUE, FEE, PUBKEY, 1500, TX, 76),
    ]
    rpc.receipts[TX2] = {"from": wallet, "logs": [rpc.logs_[0]]}
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[1]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 2 and st["unattributed"] == 0, st
    d = mock_db["pgasme_test"]
    rows = await d.deposits.find({}).to_list(10)
    assert len(rows) == 2
    assert {r["src_tx_hash"] for r in rows} == {TX, TX2}
    assert all(r["status"] == "locked" and r["verified"] is True for r in rows)
    assert all(r["account_id"] == "acct1" for r in rows)
    # lock 138 keeps the only quote that could have built it; 140 takes the nearest preceding one
    by_tx = {r["src_tx_hash"]: r for r in rows}
    assert by_tx[TX]["quote_id"] == first["_id"]
    assert by_tx[TX2]["quote_id"] == later[-1]["_id"]
    assert await d.unattributed_locks.count_documents({}) == 0
    # …and each is credited exactly once
    await workers.confirm_locked(head=1700)
    assert (await ledger.balance("acct1", "ETH"))["available"] == 2 * (VALUE // ETH.grid)


async def test_a_second_lock_priced_by_one_spent_quote_is_credited_not_stranded(
    mock_db, our_key, rpc, monkeypatch
):
    """The backstop, when there is genuinely only ONE quote for two real deposits: a quote is a
    PRICE statement, not a one-time ticket. The second lock opens its own row, marked, naming
    the row it shares the quote with — `uniq_src_tx_hash` is what keeps the two deposits apart."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    q = await a_quote(mock_db, "acct1", wallet, now - 600)
    rpc.txs[TX] = pipe_call(wallet)
    rpc.txs[TX2] = pipe_call(wallet)
    rpc.logs_ += [
        lock_log(ETH.pipe, 138, VALUE, FEE, PUBKEY, 1500, TX, 76),
        lock_log(ETH.pipe, 140, VALUE, FEE, PUBKEY, 1501, TX2, 12),
    ]
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    rpc.receipts[TX2] = {"from": wallet, "logs": [rpc.logs_[1]]}
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 2
    d = mock_db["pgasme_test"]
    rows = {r["src_tx_hash"]: r for r in await d.deposits.find({}).to_list(10)}
    assert set(rows) == {TX, TX2} and len(rows) == 2
    assert all(r["quote_id"] == q["_id"] and r["verified"] is True for r in rows.values())
    reused = rows[TX2]
    assert reused["quote_reused"] is True
    assert reused["quote_reused_of"] == rows[TX]["_id"]
    assert rows[TX]["_id"] in reused["note"] and "price statement" in reused["note"].lower()
    assert rows[TX].get("quote_reused") is not True  # the first row is not a reuse of anything
    # ⛔ and re-scanning credits nothing twice: the LOCK is the unit, not the quote
    scanner.reset_endpoint_state()
    await d.scanner_state.delete_many({})
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 0
    assert await d.deposits.count_documents({}) == 2
    await workers.confirm_locked(head=1520)
    assert (await ledger.balance("acct1", "ETH"))["available"] == 2 * (VALUE // ETH.grid)


async def test_a_reused_quote_never_crosses_two_accounts(mock_db, our_key, rpc, monkeypatch):
    """Reuse widens WHICH quote of an account may price a lock — never WHOSE. Two wallets, two
    accounts, three locks: each row belongs to the wallet that signed its pipe call."""
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    wallet_a, wallet_b = EthAccount.create().address, EthAccount.create().address
    qa = await a_quote(mock_db, "acctA", wallet_a, now - 600, n=1)
    qb = await a_quote(mock_db, "acctB", wallet_b, now - 500, n=2)
    tx_a2 = "0x" + "cc" * 32
    rpc.txs |= {TX: pipe_call(wallet_a), tx_a2: pipe_call(wallet_a), TX2: pipe_call(wallet_b)}
    rpc.logs_ += [
        lock_log(ETH.pipe, 138, VALUE, FEE, PUBKEY, 1500, TX, 1),
        lock_log(ETH.pipe, 139, VALUE, FEE, PUBKEY, 1500, TX2, 2),
        lock_log(ETH.pipe, 140, VALUE, FEE, PUBKEY, 1501, tx_a2, 3),
    ]
    for i, h in enumerate((TX, TX2, tx_a2)):
        rpc.receipts[h] = {
            "from": rpc.txs[h]["from"], "logs": [rpc.logs_[i]]
        }
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 3
    d = mock_db["pgasme_test"]
    rows = {r["src_tx_hash"]: r for r in await d.deposits.find({}).to_list(10)}
    assert len(rows) == 3
    assert rows[TX]["account_id"] == rows[tx_a2]["account_id"] == "acctA"
    assert rows[TX2]["account_id"] == "acctB"
    assert rows[TX]["quote_id"] == rows[tx_a2]["quote_id"] == qa["_id"]
    assert rows[TX2]["quote_id"] == qb["_id"]
    await workers.confirm_locked(head=1520)
    assert (await ledger.balance("acctA", "ETH"))["available"] == 2 * (VALUE // ETH.grid)
    assert (await ledger.balance("acctB", "ETH"))["available"] == VALUE // ETH.grid


# ═══════════════════════ 12. a failed re-attribution says why, on the row ═══════════════════════


async def _open_lock(mock_db, tx: str, at: float, **over: Any) -> None:
    row = {
        "_id": f"{tx}:0",
        "asset": "ETH",
        "pipe": ETH.pipe,
        "tx": tx,
        "block": 1500,
        "log_index": 0,
        "msg_id": 138,
        "amount": str(VALUE),
        "relayer_fee": str(FEE),
        "receiver": PUBKEY,
        "order_ids": [],
        "reason": "the first pass could attribute nothing",
        "status": "open",
        "at": at,
    }
    row.update(over)
    await mock_db["pgasme_test"].unattributed_locks.insert_one(row)


async def test_a_failed_reattribution_writes_the_reason_on_the_row_and_one_log_line(
    mock_db, our_key, rpc, monkeypatch, caplog
):
    """⛔ Law 12 — every decision path writes a row. A retry that attributed nothing wrote NO
    field and NO line, so a lock could be refused twelve times and still show only the sentence
    from its very first pass. msgId 138 was refused for a reason nobody could read."""
    import logging

    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    await _open_lock(mock_db, TX, now - 60)
    stranger = EthAccount.create().address
    rpc.txs[TX] = pipe_call(stranger)
    rpc.receipts[TX] = {"from": stranger, "logs": []}
    caplog.set_level(logging.INFO, logger="pgasme.scanner")
    assert await scanner.retry_unattributed(rpc) == 0
    row = await mock_db["pgasme_test"].unattributed_locks.find_one({"_id": f"{TX}:0"})
    assert row["status"] == "open" and row["tries"] == 1
    assert row["last_try_at"] > 0 and row["last_reason"]
    assert "no quote of ours was armed with the calldata" in row["last_reason"]
    lines = [r for r in caplog.records if "did not attribute" in r.getMessage()]
    assert len(lines) == 1 and f"{TX}:0" in lines[0].getMessage()


async def _none():
    return None


async def test_an_unreadable_receipt_is_a_reason_on_the_row_never_silence(
    mock_db, our_key, rpc, monkeypatch
):
    """"No endpoint answered" and "there is no receipt" are different sentences and both have to
    reach the row — the retry used to return False for either, indistinguishably."""

    async def dead(tx, prefer=None, pin=False):
        raise ethpipe.RpcError("eth_getTransactionReceipt: no endpoint answered")

    now = time.time()
    await _open_lock(mock_db, TX, now - 60)
    monkeypatch.setattr(rpc, "receipt", dead)
    assert await scanner.retry_unattributed(rpc) == 0
    row = await mock_db["pgasme_test"].unattributed_locks.find_one({"_id": f"{TX}:0"})
    assert "no endpoint answered" in row["last_reason"]

    monkeypatch.setattr(rpc, "receipt", lambda tx, prefer=None, pin=False: _none())
    await mock_db["pgasme_test"].unattributed_locks.update_one(
        {"_id": f"{TX}:0"}, {"$unset": {"next_try_at": ""}}
    )
    assert await scanner.retry_unattributed(rpc) == 0
    row = await mock_db["pgasme_test"].unattributed_locks.find_one({"_id": f"{TX}:0"})
    assert "receipt" in row["last_reason"] and "no endpoint answered" not in row["last_reason"]


# ═══════════════ 13. registration on a spent or expired quote (the same law) ════════════════════


async def test_a_second_hash_on_a_used_and_expired_quote_is_accepted_and_marked(
    client, user, armed_eth, rpc, mock_db
):
    """The user sent twice from one quote. The first registration verified; by the time the
    second is offered the quote has expired — but the money is already on Ethereum, and the
    transaction proves itself: same wallet, our pipe, this quote's own 394 bytes."""
    q = await direct_quote(client, user)
    rpc.txs[TX] = pipe_call(user["address"])
    first = await register(client, user, q["quote_id"], TX)
    assert first.json()["verified"] is True
    d = mock_db["pgasme_test"]
    await d.quotes.update_one({"_id": q["quote_id"]}, {"$set": {"expires_at": time.time() - 1}})
    rpc.txs[TX2] = pipe_call(user["address"])
    second = await register(client, user, q["quote_id"], TX2)
    assert second.status_code == 200, second.text
    assert second.json()["deposit_id"] != first.json()["deposit_id"]
    row = await d.deposits.find_one({"_id": second.json()["deposit_id"]})
    assert row["verified"] is True and row["src_tx_hash"] == TX2
    assert row["quote_reused"] is True
    assert row["quote_reused_of"] == first.json()["deposit_id"]
    assert row["quote_expired_at_registration"] > 0
    assert await d.deposits.count_documents({"quote_id": q["quote_id"]}) == 2


async def test_an_expired_quote_still_refuses_a_transaction_nobody_can_see(
    client, user, armed_eth, rpc, mock_db
):
    """An expired quote is re-usable on PROVEN identity only. A hash no endpoint has seen proves
    nothing, so it is still 409 — otherwise a dead quote becomes an unbounded row factory."""
    q = await direct_quote(client, user)
    await mock_db["pgasme_test"].quotes.update_one(
        {"_id": q["quote_id"]}, {"$set": {"expires_at": time.time() - 1}}
    )
    r = await register(client, user, q["quote_id"], TX)  # rpc.txs empty: every endpoint says no
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0


async def test_an_expired_quote_still_refuses_someone_elses_transaction(
    client, user, armed_eth, rpc, mock_db
):
    q = await direct_quote(client, user)
    await mock_db["pgasme_test"].quotes.update_one(
        {"_id": q["quote_id"]}, {"$set": {"expires_at": time.time() - 1}}
    )
    rpc.txs[TX] = pipe_call(EthAccount.create().address)
    r = await register(client, user, q["quote_id"], TX)
    assert r.status_code == 400 and "not sent from the wallet" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0
