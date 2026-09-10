"""One Beam receiver key per deposit (WO-20260910-3) — the allocator, the key cache, the quote,
the attribution and the claim.

Every deposit Pgas has ever taken named the SAME 33 bytes on Ethereum, because the shipped pipe
app derives exactly one receiver key per (wallet seed, pipe cid). The patched app accepts an
`index` and derives `KeyID{cid, index}` instead. What this file holds still:

  §1 an index is allocated ONCE, atomically, and never reused — two quotes on one key is two
     deposits on one receiver, which is the property the change was made to end;
  §2 a pubkey is read from the wallet once per index and RAISES rather than guessing — and an
     answer equal to the pipe's legacy key is refused, because that is what an UNPATCHED app
     answers for every index there is (T41 §1, measured live);
  §3 with `PGAS_RECEIVER_KEY_PER_DEPOSIT` off the calldata, the quote row, the wallet args and
     the claim are byte-for-byte what they were before this existed;
  §4 a lock to a key we issued is attributed to THAT quote — the strongest identity a pipe call
     can carry — and the from / to / calldata checks still all have to pass;
  §5 the claim signs with the ROW's index and `view_incoming` is asked about the exact open
     ones: an index nobody asks about is INVISIBLE, and an invisible message reads as "not
     delivered yet" for ever, on money already burned on Ethereum.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from conftest import PUBKEY, lock_log
from eth_account import Account as EthAccount
from test_beam_payout import (
    GROTH,
    MP,
    TREASURY,
    FakeBeamPay,
    FakeWalletApi,
    deposit,
    make_deposit,
)

from pgasme import beam, beampay, ethpipe, payouts, receiver_keys, scanner, xchain
from pgasme.assets import ASSETS
from pgasme.config import settings
from pgasme.db import db

ETH = ASSETS["ETH"]
DAI = ASSETS["DAI"]
ZERO = "0x0000000000000000000000000000000000000000"
AMOUNT = 50_000_000_000_000_000  # 0.05 ETH, over the product floor
FEE = 100_000_000_000
VALUE = AMOUNT - FEE
TX = "0x" + "aa" * 32
Q_DIRECT = {"src_chain_id": 1, "src_token": ZERO, "amount": str(AMOUNT), "target_asset": "ETH"}

# THE SNAPSHOT. The exact `sendFunds(value, relayerFee, receiverBeamPubkey)` bytes a 0.05 ETH
# direct quote handed out BEFORE this change existed, frozen here as a literal rather than
# recomputed from the same function the code under test uses — a snapshot that is re-derived
# from the implementation proves nothing about the implementation. Produced on 2026-09-10 by
# `ethpipe.encode_send_funds(VALUE, FEE, PUBKEY)` on the pre-change tree.
LEGACY_CALLDATA = (
    "0x4d5dd2bc"
    "00000000000000000000000000000000000000000000000000b1a2a4e64e1800"
    "000000000000000000000000000000000000000000000000000000174876e800"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000021"
    "02ababababababababababababababababababababababababababababababab"
    "ab00000000000000000000000000000000000000000000000000000000000000"
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Nothing here reaches CoinGecko, the order router or an Ethereum node."""

    async def px(force: bool = False) -> dict[str, float]:
        return {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}

    async def no_ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return []

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)
    monkeypatch.setattr(xchain, "order_ids_by_tx", no_ids)
    monkeypatch.setattr(settings, "lock_scan_chunk", 500)
    monkeypatch.setattr(settings, "lock_scan_blocks", 2000)


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch) -> FakeBeamPay:
    bp = FakeBeamPay()
    beampay.reset_health()
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)
    bp.fund(MP, 36, 5 * GROTH)
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    monkeypatch.setattr(settings, "shield_keep_groth", 0)
    yield bp
    beampay.set_beampay(None)


@pytest.fixture(autouse=True)
def wallet_api(monkeypatch, beam_pay: FakeBeamPay) -> FakeWalletApi:
    """The wallet-api fake, with K1's patched pipe app deployed. `patched = False` is a test in
    its own right (§2) — the box's CURRENT app, which ignores `index=`."""
    w = FakeWalletApi(beam_pay)
    w.patched = True
    beam.set_wallet(w)
    payouts.reset_archive_pin()
    monkeypatch.setattr(settings, "beam_shader", w.shader)
    yield w
    beam.set_wallet(None)


@pytest.fixture
def per_deposit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "receiver_key_per_deposit", True)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    monkeypatch.setattr(settings, "ingress_armed", True)


@pytest.fixture
def our_key(monkeypatch) -> str:
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    return PUBKEY


@pytest.fixture
def armed_claims(monkeypatch) -> None:
    monkeypatch.setattr(settings, "claim_enabled", True)


class Yielding:
    """A collection whose every awaitable yields to the event loop BEFORE it runs — the
    interleaving a real Mongo gives two concurrent requests for free, and which the offline
    suite's in-process double otherwise never produces (measured: a read-then-write allocator
    is 50-for-50 correct under mongomock and 1-for-50 under this)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        async def wrapped(*a: Any, **k: Any) -> Any:
            await asyncio.sleep(0)
            return await attr(*a, **k)

        return wrapped


async def a_quote(account_id: str, address: str, at: float, **over: Any) -> dict[str, Any]:
    """A `direct` quote as POST /v1/quote stores one, armed with the calldata of `pubkey`."""
    pk = over.pop("pubkey", PUBKEY)
    doc = {
        "_id": over.pop("_id", "q-" + str(int(at * 1000))[-9:]),
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
        "pubkey": pk,
        "hook_calldata": ethpipe.encode_send_funds(VALUE, FEE, pk),
        "armed": True,
        "at": at,
        "expires_at": at + 900,
    }
    doc.update(over)
    await db().quotes.insert_one(doc)
    return doc


def pipe_call(address: str, pubkey: str = PUBKEY) -> dict[str, Any]:
    return {
        "from": address,
        "to": ETH.pipe,
        "input": ethpipe.encode_send_funds(VALUE, FEE, pubkey),
        "blockNumber": hex(1500),
    }


def fixed_block_time(ts: float | None):
    async def bt(rpc: Any, block: Any, prefer: Any = None) -> float | None:
        return ts

    return bt


# ═══════════════════════════════ §1 the allocator ═══════════════════════════════


async def test_fifty_concurrent_allocations_get_fifty_different_indexes(mock_db, monkeypatch):
    """⛔ ONE ATOMIC `$inc`, NEVER A READ THEN A WRITE. Two quotes handed the same index are two
    deposits handed the same receiver key — the exact state this whole change exists to end,
    now with two rows each claiming it did not happen.

    The interleaving is FORCED (`Yielding`): the offline double runs each operation to
    completion inside its own await, so a read-then-write allocator is 50-for-50 correct here
    and 1-for-50 against anything that actually schedules. A test that cannot fail is not a
    proof, so this one supplies the scheduler."""
    real = db()

    class Slow:
        def __getattr__(self, name: str) -> Any:
            got = getattr(real, name)
            return Yielding(got) if name == "counters" else got

    monkeypatch.setattr(receiver_keys, "db", lambda: Slow())
    got = await asyncio.gather(
        *[receiver_keys.next_receiver_index(ETH.beam_cid) for _ in range(50)]
    )
    assert len(set(got)) == 50, sorted(got)
    assert sorted(got) == list(range(1, 51))


async def test_indexes_start_at_one_and_are_counted_per_pipe(mock_db):
    """0 is the LEGACY key (the cid-only blob) and is never allocated; each pipe has its own
    sequence, because the key is derived from that pipe's cid."""
    assert receiver_keys.LEGACY_INDEX == 0
    assert await receiver_keys.next_receiver_index(ETH.beam_cid) == 1
    assert await receiver_keys.next_receiver_index(ETH.beam_cid) == 2
    assert await receiver_keys.next_receiver_index(DAI.beam_cid) == 1
    assert await receiver_keys.next_receiver_index(ETH.beam_cid) == 3


async def test_an_index_whose_key_could_not_be_read_is_burned_never_retried(
    mock_db, wallet_api, our_key
):
    """A failed `get_pk` does NOT put the index back. Reusing it would mean two quotes, days
    apart, could be issued one receiver — the failure mode the allocator exists to prevent,
    reached through the error path instead of the happy one."""
    wallet_api.raise_on.add("invoke_contract")
    with pytest.raises(beam.BeamError):
        await receiver_keys.issue(ETH)
    wallet_api.raise_on.clear()
    index, _pk = await receiver_keys.issue(ETH)
    assert index == 2  # 1 is gone for ever
    assert await db().receiver_keys.count_documents({"index": 1}) == 0


# ═══════════════════════════════ §2 the key cache ═══════════════════════════════


async def test_a_pubkey_is_read_from_the_wallet_once_per_index(mock_db, wallet_api, our_key):
    """One wallet call per index, ever — the answer is a pure function of (seed, cid, index), so
    a second read is a round trip that can only fail."""
    first = await receiver_keys.pk_for_index(ETH, 1)
    again = await receiver_keys.pk_for_index(ETH, 1)
    other = await receiver_keys.pk_for_index(ETH, 2)
    assert first == again and first != other
    assert wallet_api.get_pk_args == [1, 2]
    row = await db().receiver_keys.find_one({"_id": f"{ETH.beam_cid}:1"})
    assert row["pk"] == first and row["index"] == 1 and row["pipe_cid"] == ETH.beam_cid
    assert row["asset"] == "ETH" and row["issued_at"] > 0 and row["quote_id"] is None


async def test_the_wallet_answering_the_legacy_key_for_an_index_is_refused(
    mock_db, wallet_api, our_key
):
    """⛔ THE UNPATCHED-BOX TRAP. The SHIPPED pipe app ignores `index=` entirely and answers the
    cid-derived key for every value of it (T41 §1, probed live). So "we asked for index 7 and
    got 33 bytes back" is NOT evidence that a per-deposit key was derived — the one thing that
    distinguishes the two apps is that the answer DIFFERS from the legacy key. Without this
    check a deploy that forgot the wasm issues N quotes that all share one receiver while every
    row says they do not."""
    wallet_api.patched = False
    with pytest.raises(beam.BeamError, match="LEGACY"):
        await receiver_keys.pk_for_index(ETH, 1)
    assert await db().receiver_keys.count_documents({}) == 0


async def test_a_malformed_pubkey_is_never_cached_and_never_issued(mock_db, wallet_api, our_key):
    """`EthPipe.sol` validates only `receiverBeamPubkey.length == 33` and the relayer forwards it
    verbatim: a key that is not 33 bytes mints value nobody can ever claim, with no refund
    path."""
    wallet_api.pk_for = lambda index: "02beef"  # type: ignore[assignment]
    with pytest.raises(beam.BeamError, match="66 hex"):
        await receiver_keys.pk_for_index(ETH, 1)
    assert await db().receiver_keys.count_documents({}) == 0


# ═══════════════════════════ §3 the quote, on and off ═══════════════════════════


async def test_a_direct_quote_embeds_its_own_receiver_key(
    client, user, mock_db, wallet_api, per_deposit
):
    r = await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["armed"] is True
    call = ethpipe.decode_send_funds(body["tx"]["data"])
    assert call["pubkey"] == wallet_api.pk_for(1) != PUBKEY
    q = await db().quotes.find_one({"_id": body["quote_id"]})
    assert q["receiver_index"] == 1 and q["receiver_pk"] == call["pubkey"]
    assert q["pubkey"] == call["pubkey"] and q["hook_calldata"] == body["tx"]["data"]
    # the key cache says where the key went — evidence, on the row, not in a log nobody reads
    row = await db().receiver_keys.find_one({"_id": f"{ETH.beam_cid}:1"})
    assert row["quote_id"] == body["quote_id"] and row["pk"] == call["pubkey"]
    # …and the NEXT quote gets its own
    second = (await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])).json()
    assert ethpipe.decode_send_funds(second["tx"]["data"])["pubkey"] == wallet_api.pk_for(2)


async def test_with_the_flag_off_the_calldata_is_byte_for_byte_what_it_was(
    client, user, mock_db, armed_eth, wallet_api
):
    """§3. OFF MEANS OFF: the same 394 bytes, the legacy key, no receiver fields on the row and
    NOT ONE wallet call — a quote that phones the Beam wallet at all is a quote that can fail
    for a reason the old one could not."""
    assert settings.receiver_key_per_deposit is False
    body = (await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])).json()
    assert body["tx"]["data"] == LEGACY_CALLDATA
    assert ethpipe.decode_send_funds(body["tx"]["data"])["pubkey"] == PUBKEY
    q = await db().quotes.find_one({"_id": body["quote_id"]})
    assert "receiver_index" not in q and "receiver_pk" not in q and q["pubkey"] == PUBKEY
    assert wallet_api.calls == []
    assert await db().counters.count_documents({}) == 0


async def test_an_unreadable_wallet_refuses_the_quote_and_never_arms_the_legacy_key(
    client, user, mock_db, wallet_api, per_deposit
):
    """⛔ NEVER A LEGACY KEY SILENTLY. Falling back would hand this user the receiver every other
    deposit shares, quietly undo the property the flag was turned on for, and record on the row
    that it had done no such thing. 503 is the honest answer and the client can retry."""
    wallet_api.raise_on.add("invoke_contract")
    r = await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])
    assert r.status_code == 503, r.text
    assert PUBKEY not in r.text
    assert await db().quotes.count_documents({}) == 0


async def test_an_estimate_only_quote_allocates_nothing(client, user, mock_db, monkeypatch):
    """An unarmed quote issues no calldata, so allocating for one would burn an index — and the
    counter never hands the same one back — on every price refresh of a route nobody can sign."""
    monkeypatch.setattr(settings, "receiver_key_per_deposit", True)
    monkeypatch.setattr(settings, "ingress_armed", False)
    body = (await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])).json()
    assert body["armed"] is False and "tx" not in body
    assert await db().counters.count_documents({}) == 0


async def test_arming_one_quote_twice_hands_back_the_same_key(
    client, user, mock_db, wallet_api, monkeypatch
):
    """/arm is called again on every retry and every double-click, and EVERY transaction a quote
    was ever armed with stays registrable. A second key per quote would let the user sign
    calldata naming a receiver the quote no longer records — a lock attributed to nobody and a
    claim nobody can sign for."""
    monkeypatch.setattr(settings, "receiver_key_per_deposit", True)
    monkeypatch.setattr(settings, "ingress_armed", False)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    qid = (await client.post("/v1/quote", json=Q_DIRECT, headers=user["headers"])).json()[
        "quote_id"
    ]
    monkeypatch.setattr(settings, "ingress_armed", True)
    first = await client.post(f"/v1/quote/{qid}/arm", headers=user["headers"])
    second = await client.post(f"/v1/quote/{qid}/arm", headers=user["headers"])
    assert first.status_code == second.status_code == 200, first.text
    assert first.json()["tx"]["data"] == second.json()["tx"]["data"]
    assert ethpipe.decode_send_funds(first.json()["tx"]["data"])["pubkey"] == wallet_api.pk_for(1)
    assert wallet_api.get_pk_args == [1]  # one allocation, not two
    assert (await db().quotes.find_one({"_id": qid}))["receiver_index"] == 1


# ═══════════════════ §4 attribution: the key IS the identity ═══════════════════


async def test_the_scanner_knows_every_key_it_has_issued(mock_db, our_key):
    """A key that is not in this set is not merely unattributed — it is UNSEEN: `handle_log`
    drops the lock as foreign, nothing is recorded and nobody is paged."""
    index, pk = await receiver_keys.issue(ETH)
    keys = await scanner.known_pubkeys(ETH)
    assert pk in keys and PUBKEY in keys and index == 1
    # …and a key issued longer ago than the attribution window falls back out (bounded), which
    # is why a LIVE deposit's own pubkey is kept by the other half of the union
    await db().receiver_keys.update_one({"index": 1}, {"$set": {"issued_at": time.time() - 10**6}})
    assert pk not in await scanner.known_pubkeys(ETH)
    await db().deposits.insert_one({"_id": "d1", "asset": "ETH", "status": "locked", "pubkey": pk})
    assert pk in await scanner.known_pubkeys(ETH)


async def test_a_lock_to_a_key_we_issued_is_attributed_to_that_quote(
    mock_db, our_key, rpc, monkeypatch
):
    """§4. The 33 bytes were issued to ONE quote and to nothing else, so they name it directly —
    no candidate search, no time window. The from / to / calldata checks still run."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    _index, pk = await receiver_keys.issue(ETH)
    q = await a_quote("acct1", wallet, now - 60, pubkey=pk, receiver_index=1, receiver_pk=pk)
    rpc.txs[TX] = pipe_call(wallet, pk)
    rpc.logs_.append(lock_log(ETH.pipe, 138, VALUE, FEE, pk, 1500, TX, 76))
    rpc.receipts[TX] = {"from": wallet, "logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 1 and st["unattributed"] == 0
    dep = await db().deposits.find_one({"quote_id": q["_id"]})
    assert dep["verified"] is True and dep["verified_by"] == "quote-receiver-key"
    assert "attributed by receiver key" in dep["note"]
    assert dep["receiver_index"] == 1 and dep["receiver_pk"] == pk and dep["pubkey"] == pk
    ev = await db().events.find_one({"kind": "deposit_locked"})
    assert "by receiver key" in ev["text"]


async def test_a_lock_to_our_key_from_a_stranger_is_still_refused(
    mock_db, our_key, rpc, monkeypatch
):
    """The receiver key is the STRONGEST identity, not a weaker one: it does not excuse the
    other three facts. A stranger who copies our calldata into their own call is refused here
    exactly as they are on the calldata path — and the lock goes to the operator's desk rather
    than crediting the wrong account."""
    wallet = EthAccount.create().address
    now = time.time()
    monkeypatch.setattr(scanner, "_block_time", fixed_block_time(now))
    _index, pk = await receiver_keys.issue(ETH)
    await a_quote("acct1", wallet, now - 60, pubkey=pk, receiver_index=1, receiver_pk=pk)
    stranger = EthAccount.create().address
    rpc.txs[TX] = pipe_call(stranger, pk)
    rpc.logs_.append(lock_log(ETH.pipe, 138, VALUE, FEE, pk, 1500, TX, 76))
    rpc.receipts[TX] = {"from": stranger, "logs": [rpc.logs_[0]]}
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 0 and st["unattributed"] == 1
    assert await db().deposits.count_documents({}) == 0
    row = await db().unattributed_locks.find_one({})
    assert "receiver key" in row["reason"] and "refusing" in row["reason"]


async def test_two_quotes_holding_one_key_refuse_rather_than_guess(mock_db, our_key):
    """An index is never reused, so this cannot happen — and if the allocator ever breaks, a
    coin flip between two accounts is not the failure to add on top of it."""
    _index, pk = await receiver_keys.issue(ETH)
    now = time.time()
    await a_quote("acct1", EthAccount.create().address, now, _id="qa", receiver_pk=pk, pubkey=pk)
    await a_quote("acct2", EthAccount.create().address, now, _id="qb", receiver_pk=pk, pubkey=pk)
    assert await receiver_keys.quote_for_pk(ETH, pk) is None


# ═══════════════════ §5 the claim and the watcher's window ═══════════════════


async def test_the_claim_signs_with_the_rows_own_index(
    mock_db, wallet_api, armed_claims, our_key
):
    """The signing blob has to be the key the message was delivered to. The row's index, never
    the process's idea of the current one — a claim signed with the wrong key cannot succeed,
    and the value is already burned on Ethereum."""
    await make_deposit(mock_db, value=1_000_000)
    await db().deposits.update_one({"_id": "dep1"}, {"$set": {"receiver_index": 7}})
    await db().receiver_keys.insert_one(
        {"_id": f"{ETH.beam_cid}:7", "pipe_cid": ETH.beam_cid, "asset": "ETH",
         "index": 7, "pk": "02" + f"{7:064x}", "issued_at": time.time(), "quote_id": "q7"}
    )
    wallet_api.incoming = [{"msg_id": 222, "amount": 1_000_000, "index": 7}]
    for _ in range(3):
        await payouts.process_once()
    assert (await deposit(mock_db))["treasury"] in ("claimed", "shielding")
    receives = [p["args"] for p in wallet_api.params_for("invoke_contract")
                if "action=receive" in p["args"]]
    assert len(receives) == 1 and receives[0].endswith(",index=7")
    assert wallet_api.receive_index_args == [7]
    # …and view_incoming was asked about that index AND legacy, never about neither
    assert wallet_api.view_index_args and all(v == "0;7" for v in wallet_api.view_index_args)


async def test_a_legacy_row_claims_exactly_as_it_always_did(
    mock_db, wallet_api, armed_claims, our_key
):
    """§3 on the money path: a deposit that predates this change carries no index, its `receive`
    args carry no `index=` at all, and `view_incoming` asks the question it always asked."""
    await make_deposit(mock_db, value=1_000_000)
    wallet_api.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    for _ in range(3):
        await payouts.process_once()
    assert (await deposit(mock_db))["treasury"] in ("claimed", "shielding")
    receives = [p["args"] for p in wallet_api.params_for("invoke_contract")
                if "action=receive" in p["args"]]
    assert receives == [f"role=user,action=receive,cid={ETH.beam_cid},msgId=222"]
    assert wallet_api.receive_index_args == [None]
    assert wallet_api.view_index_args and all(v is None for v in wallet_api.view_index_args)


async def test_a_message_on_an_index_nobody_asked_about_is_invisible(
    mock_db, wallet_api, armed_claims, our_key
):
    """⛔ THE WINDOW IS THE WHOLE RISK. The patched `view_incoming` matches messages against the
    set of indexes it was given, so a delivery on an index the API did not ask about is not
    "not delivered yet" — it is INVISIBLE, and the deposit holds for ever with the money already
    burned. This is that failure, made deliberately: the row lost its index."""
    await make_deposit(mock_db, value=1_000_000)
    wallet_api.incoming = [{"msg_id": 222, "amount": 1_000_000, "index": 999}]
    for _ in range(3):
        await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claiming" and not row.get("claim_txid")
    assert wallet_api.receive_index_args == []
    assert "has not delivered pipe message 222" in row["hold_reason"]


async def test_view_incoming_is_asked_about_every_open_index_and_legacy(mock_db, our_key):
    """Two sources, because neither alone is complete: a key issued minutes ago whose deposit
    has no row yet, and a row whose message is still unclaimed however old the key is."""
    assert await receiver_keys.open_indexes(ETH) == []  # nothing issued: the legacy question
    await receiver_keys.issue(ETH)  # 1 — fresh, no row yet
    await db().receiver_keys.insert_one(
        {"_id": f"{ETH.beam_cid}:2", "pipe_cid": ETH.beam_cid, "asset": "ETH", "index": 2,
         "pk": "02" + f"{2:064x}", "issued_at": time.time() - 10**6, "quote_id": "q2"}
    )
    await db().deposits.insert_one(
        {"_id": "old", "asset": "ETH", "status": "locked", "receiver_index": 2}
    )
    await db().deposits.insert_one(
        {"_id": "done", "asset": "ETH", "status": "credited", "receiver_index": 3,
         "treasury": "shielding"}
    )
    # 1 (fresh key) + 2 (an old key whose message is unclaimed) + legacy; 3 is already claimed
    assert await receiver_keys.open_indexes(ETH) == [0, 1, 2]


async def test_the_open_index_window_is_bounded(mock_db, our_key):
    """The shader derives a bounded window; asking for more than it can hold would silently drop
    the tail. Newest first, because that is where a delivery is most likely to be."""
    now = time.time()
    await db().receiver_keys.insert_many(
        [
            {"_id": f"{ETH.beam_cid}:{i}", "pipe_cid": ETH.beam_cid, "asset": "ETH", "index": i,
             "pk": "02" + f"{i:064x}", "issued_at": now - i, "quote_id": None}
            for i in range(1, 200)
        ]
    )
    got = await receiver_keys.open_indexes(ETH)
    assert len(got) == receiver_keys.MAX_VIEW_INDEXES
    assert got[0] == 0 and got[1:] == list(range(2, receiver_keys.MAX_VIEW_INDEXES + 1))
