"""Regressions for the money defects found in the Beam payout pipeline (2026-09-09 review).

Every test here fails on the code as it was and passes on the code as it is. They are grouped
by the law that was broken, because that is what has to keep holding:

  §IDENTITY-BEATS-BALANCE   a lost response is resolved by identity, never by presence, and a
                            release is booked only against OUR own pipe message
  a retry never re-signs    one conditional update before every irreversible call
  refusals write a row      and a hold a human owns cannot un-hold itself
  every guard at its level  the float, the BEAM fees and the relayer's cut are cycle-level
  broadcast ≠ done          one on-chain delivery settles exactly one order
  a guard that refreshes its own deadline is not a guard

The fixtures and fakes are the project's own (`test_beam_payout`): `FakeWalletApi` subclasses
the real client and replaces only the transport, so every parse and every pre-broadcast
assertion in `pgasme/beam.py` runs for real, and nothing here can make a `create_tx: true` call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import pytest
from pymongo.errors import DuplicateKeyError
from test_beam_payout import (
    ETH,
    GROTH,
    MP,
    TREASURY,
    FakeBeamPay,
    FakeEth,
    FakeWalletApi,
    W,
    deposit,
    make_deposit,
    make_payout,
    payout,
    send_args,
)

from pgasme import beam, beampay, ledger, payouts, scanner, tg, workers
from pgasme import db as dbmod
from pgasme.config import settings

ADDRESS_RE = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{40}(?![0-9a-fA-F])")
OTHER_W = "0x2222222222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    """BeamPay is the system of record here too — same posture as `test_beam_payout`."""
    bp = FakeBeamPay()
    beampay.reset_health()  # observed health is per-process state
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)
    bp.fund(MP, 36, 5 * GROTH)
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    yield bp
    beampay.set_beampay(None)


@pytest.fixture(autouse=True)
def beam_wallet(monkeypatch: pytest.MonkeyPatch, beam_pay: FakeBeamPay) -> FakeWalletApi:
    w = FakeWalletApi(beam_pay)
    beam.set_wallet(w)
    payouts.reset_archive_pin()
    monkeypatch.setattr(settings, "beam_shader", w.shader)
    # these tests drive one order through several passes on purpose; the batch's cooling-off
    # backoff (its own regression below) would otherwise skip the row being examined
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    yield w
    beam.set_wallet(None)


@pytest.fixture
def eth(monkeypatch: pytest.MonkeyPatch) -> FakeEth:
    fake = FakeEth()
    monkeypatch.setattr(workers, "get_rpc", lambda: fake)
    return fake


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "claim_enabled", True)
    monkeypatch.setattr(settings, "shield_enabled", True)


@pytest.fixture
def paged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Everything tg.send would have sent, in order."""
    out: list[str] = []

    async def fake_send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        out.append(text)
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    return out


def record(bp: FakeBeamPay, *rows: dict[str, Any]) -> None:
    """Put these transactions into BeamPay's history, which is where every read now looks."""
    for row in rows:
        bp.add_tx(**row)


def send_tx(txid: str, groth: int, at: float | None = None) -> dict[str, Any]:
    """A b2e release as BeamPay's own history shows it: a SPEND of amount + relayerFee, and a
    SPEND IS POSITIVE — `SEND_IS_POSITIVE` above is a live row from the very pipe Pgas.me uses
    (cid 8872509d…, `{asset_id: 36, amount: +15995980}` against a −0.15995980 bETH movement)."""
    return {
        "txId": txid,
        "create_time": int(at if at is not None else time.time()),
        "invoke_data": [
            {"contract_id": ETH.beam_cid, "amounts": [{"asset_id": 36, "amount": +int(groth)}]}
        ],
    }


def claim_tx(txid: str, groth: int, at: float | None = None) -> dict[str, Any]:
    """A claim as BeamPay's own history shows it: a RECEIVE of the message's value, and a
    RECEIVE IS NEGATIVE (`CLAIM_IS_NEGATIVE`, a live e2b claim). Same pipe, same asset, same
    shape as a release — indistinguishable from one by anything but its sign, which is exactly
    why getting the sign backwards aimed each resolver at the other operation."""
    return {
        "txId": txid,
        "create_time": int(at if at is not None else time.time()),
        "invoke_data": [
            {"contract_id": ETH.beam_cid, "amounts": [{"asset_id": 36, "amount": -int(groth)}]}
        ],
    }


def settled(bp: FakeBeamPay, txid: str, confirmations: int = 0) -> None:
    """Mark a transaction settled AND booked — the pair BeamPay reports, and the pair the code
    requires: `booked` alone is also set for a cancelled tx."""
    bp.tx(txid).update(
        {
            "status": beam.TX_COMPLETED,
            "status_string": "completed",
            "success": True,
            "kernel": f"kernel-{txid}",
            "confirmations": confirmations,
        }
    )


# ================================================== §1 · identity, never presence (defects 1, 7, 18)


async def test_a_payout_never_adopts_a_deposit_claim_as_its_own_send(
    mock_db, eth, armed, beam_wallet
, beam_pay):
    """Every claim and every release on the ETH pipe carries the same (cid, asset id), so
    matching on those adopted a DEPOSIT'S CLAIM as the payout's own crossing: the row advanced
    to bridging, saw the claim's kernel confirm, and booked an irreversible ledger release for
    a crossing that never happened."""
    await make_payout(
        mock_db,
        status="releasing",
        relayer_fee_groth=3600,
        release_call_at=time.time() - 30,
        release_attempt_at=time.time() - 30,
    )
    # …and it is worth EXACTLY amount + relayerFee, which is the shape the inverted expectation
    # `-(amount + relayer_fee)` hunted for: a claim INFLOW of this crossing's size
    record(beam_pay, claim_tx("claim-of-someone-elses-deposit", 503_600))
    beam_wallet.local_msgs = {}
    await payouts.process_once()
    row = await payout(mock_db)
    assert "beam_txid" not in row and row["status"] == "releasing"
    assert await ledger.find_entry("release", "req1") is None
    assert (await ledger.balance("acct1", "ETH"))["sent"] == 0


async def test_a_payout_never_adopts_another_payouts_transaction(mock_db, eth, armed, beam_wallet, beam_pay):
    """Two payouts released seconds apart are the routine case (one withdrawal carries up to 50
    wallets). A's send landed; B's response was lost. The two are the same size to the groth, so
    only "that txid is already somebody's evidence" can tell them apart."""
    now = time.time()
    await make_payout(
        mock_db, rid="reqA", status="releasing", relayer_fee_groth=3600, beam_txid="beamtx-1"
    )
    await make_payout(
        mock_db,
        rid="reqB",
        status="releasing",
        relayer_fee_groth=3600,
        release_call_at=now - 30,
        release_attempt_at=now - 30,
    )
    record(beam_pay, send_tx("beamtx-1", 503_600))
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W}}
    settled(beam_pay, "beamtx-1")
    await payouts.process_once()
    b = await payout(mock_db, "reqB")
    assert "beam_txid" not in b
    assert await ledger.find_entry("release", "reqB") is None


async def test_find_contract_tx_takes_the_newest_match_and_skips_a_taken_txid(beam_wallet, beam_pay):
    """The reference keeps `ct > best[0]`; the port returned the first row `tx_list` yielded,
    and Beam answers OLDEST-first — so it preferentially picked the match least likely to be
    the attempt made seconds ago."""
    now = time.time()
    record(beam_pay, send_tx("old", 503_600, now - 300), send_tx("new", 503_600, now - 5))
    got = await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, +503_600)
    assert got and got["txId"] == "new"
    got = await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, +503_600, {"new"})
    assert got and got["txId"] == "old"
    assert (
        await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, +503_600, {"new", "old"})
        is None
    )


async def test_find_contract_tx_refuses_to_match_the_pipe_alone(beam_wallet, beam_pay):
    """The direction and the size are not optional: a claim is a receive, a release is a spend,
    and 'some transaction touched the pipe' is not evidence about either."""
    now = time.time()
    record(beam_pay, claim_tx("a-claim", 12_000_000), send_tx("a-send", 503_600))
    with pytest.raises(beampay.BeamPayError):
        await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, 0)
    assert await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, +503_601) is None
    assert await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, -12_000_001) is None
    # ⛔ and the SIGN is the whole discriminator: the release's own size with the claim's sign
    # is the deposit's transaction, never the payout's
    assert await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, -503_600) is None
    ours = await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, +503_600)
    assert ours and ours["txId"] == "a-send"
    theirs = await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, -12_000_000)
    assert theirs and theirs["txId"] == "a-claim"


async def test_the_database_refuses_two_rows_carrying_one_beam_txid(mock_db):
    """The read-side exclusion is the first guard; the index is the one that still holds when
    two processes read at the same moment."""
    await payouts.ensure_indexes()
    d = mock_db["pgasme_test"]
    await d.payout_requests.insert_one({"_id": "a", "beam_txid": "t1"})
    await d.payout_requests.insert_one({"_id": "b"})
    with pytest.raises(DuplicateKeyError):
        await d.payout_requests.update_one({"_id": "b"}, {"$set": {"beam_txid": "t1"}})
    await d.deposits.insert_one({"_id": "d1", "claim_txid": "c1"})
    await d.deposits.insert_one({"_id": "d2"})
    with pytest.raises(DuplicateKeyError):
        await d.deposits.update_one({"_id": "d2"}, {"$set": {"claim_txid": "c1"}})


# ================================================== §2 · a hold a human owns (defect 2)


async def test_a_held_release_cannot_un_hold_itself_hours_later(mock_db, eth, armed, beam_wallet, beam_pay):
    """The 15-minute hold used to run AFTER the resolver and leave the row in `releasing` with
    an unbounded `since`, so it only delayed the adoption: a row already carrying "NOT
    auto-retried" adopted an unrelated transaction two hours later and booked the ledger
    release on it. The reference advances to a status whose handler does not resolve."""
    await make_payout(
        mock_db,
        status="releasing",
        relayer_fee_groth=3600,
        release_call_at=time.time() - 3600,
        release_attempt_at=time.time() - 3600,
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "held" and row["unresolved_at"] > 0

    # …and now exactly the transaction it was waiting for turns up
    record(beam_pay, send_tx("beamtx-9", 503_600))
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W}}
    settled(beam_pay, "beamtx-9")
    for _ in range(3):
        await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "held" and "beam_txid" not in row
    assert "tx_list" not in beam_wallet.methods()  # nothing even looked
    assert await ledger.find_entry("release", "req1") is None


async def test_a_held_row_keeps_paging_until_a_human_moves_it(mock_db, paged):
    await make_payout(mock_db, status="held", hold_reason="the release response was lost")
    await workers.stuck_checks()
    assert any(t.startswith("HELD: payout parked for a human") for t in paged)


# ================================================== §3 · the disproof is not a warning (defect 3)


async def test_the_release_is_not_booked_without_our_own_pipe_message(
    mock_db, eth, armed, beam_wallet
):
    """`find_local_msg` is the one piece of evidence that says "OUR message, to THIS receiver,
    for THIS amount". When it answered None the code logged a warning and booked the
    irreversible ledger release anyway — a permanent debit on the strength of "some contract
    transaction confirmed"."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()  # → releasing (+ the send)
    await payouts.process_once()  # → bridging
    beam_wallet.local_msgs = {}  # the crossing cannot be identified on the pipe
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging" and "kernel_at" not in row
    assert "refusing to book the release" in row["hold_reason"]
    assert await ledger.find_entry("release", "req1") is None
    bal = await ledger.balance("acct1", "ETH")
    assert bal["sent"] == 0 and bal["scheduled"] == 510_000

    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W}}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["msg_id"] == 7 and row["kernel_at"] > 0
    entry = await ledger.find_entry("release", "req1")
    assert entry and entry["d_sent"] == 500_000


# ================================================== §4 · one writer per resource (defect 4)


async def test_two_processors_produce_exactly_one_claim_signature(
    mock_db, eth, armed, beam_wallet
):
    """`_payout_scheduled` claims its work with a conditional update; the treasury did not, and
    two passes over one row produced TWO `process_invoke_data` calls for one msgId — two
    signatures over one inventory, the law this module exists to enforce."""
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await payouts.process_once()  # (none) → claiming
    dep = await deposit(mock_db)
    await asyncio.gather(payouts._treasury_claiming(dep), payouts._treasury_claiming(dict(dep)))
    assert beam_wallet.methods().count("process_invoke_data") == 1
    row = await deposit(mock_db)
    assert row["claim_txid"] == "beamtx-1"


async def test_two_processors_produce_exactly_one_shield_chunk(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ BeamPay's `/withdraw` HAS NO IDEMPOTENCY KEY AT ALL: two passes that both read "chunk
    0 unsent" queue two withdrawals of the treasury's money, and nothing on either side can
    merge them afterwards. The conditional slot claim is written BEFORE the call, so the loser
    of the race finds it and stops."""
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    beam_pay.fund(TREASURY, 36, 1_000_000)
    for _ in range(4):
        await payouts.process_once()
    dep = await deposit(mock_db)
    assert dep["treasury"] == "shielding"
    await asyncio.gather(payouts._treasury_shielding(dep), payouts._treasury_shielding(dict(dep)))
    assert len(beam_pay.withdrawals) == 1
    row = await deposit(mock_db)
    assert len(row["shield_calls"]) == 1 and row["shield_calls"][0] > 0


async def test_a_second_processor_loop_refuses_to_run_at_all(mock_db, eth, armed, monkeypatch):
    """`--workers 1` in a unit file is a deployment fact, not code."""
    await make_payout(mock_db)
    await mock_db["pgasme_test"].leases.insert_one(
        {"_id": payouts.LEASE_ID, "owner": "another-process", "at": time.time()}
    )
    out = await payouts.process_once()
    assert out["lease"] == 0 and out["payouts"] == 0
    assert (await payout(mock_db))["status"] == "scheduled"
    # …and the process that owns the lease still works
    await mock_db["pgasme_test"].leases.delete_many({})
    assert (await payouts.process_once())["lease"] == 1


# ================================================== §5 · one delivery, one order (defects 5, 10, 23)


async def test_one_on_chain_delivery_settles_exactly_one_payout(mock_db, eth, armed, beam_wallet):
    """Two payouts of one denomination to one registered wallet is the product's normal shape,
    and they share a baseline block. `got == amount_wei` tied the pair to no crossing at all, so
    ONE delivery closed BOTH orders — and if the second is the one the relayer drops, the loss
    is silent and the record says "delivered"."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base = eth.head
    for rid in ("reqA", "reqB"):
        await make_payout(
            mock_db,
            rid=rid,
            status="delivering",
            beam_txid=f"tx-{rid}",
            eth_from_block=base,
            eth_scan_from=base,
        )
    block = base + 300
    eth.head = base + 1000
    eth.credit(block, ETH.pipe, -500_000 * ETH.grid)
    eth.credit(block, W, 500_000 * ETH.grid)
    await payouts.process_once()
    a, b = await payout(mock_db, "reqA"), await payout(mock_db, "reqB")
    assert sorted([a["status"], b["status"]]) == ["delivering", "sent"]
    loser = a if a["status"] == "delivering" else b
    assert "already booked to another payout request" in loser["hold_reason"]
    rows = await mock_db["pgasme_test"].deliveries.find({}).to_list(10)
    assert len(rows) == 1 and rows[0]["block"] == block


# ================================================== §6 · the ledger release is one unit (6, 15)


async def test_a_half_written_release_is_repaired_not_declared_booked(mock_db, monkeypatch):
    """`ledger.release` appends `release` then `fee`; the caller guarded on the first alone. One
    failed second append left the 2% in Scheduled forever — money the user could neither spend
    nor get back — and every later pass short-circuited on "already booked"."""
    real = ledger._append
    state = {"fail": True}

    async def flaky(account_id: str, asset: str, kind: str, *a: Any, **kw: Any) -> Any:
        if kind == "fee" and state["fail"]:
            raise RuntimeError("not primary")
        return await real(account_id, asset, kind, *a, **kw)

    monkeypatch.setattr(ledger, "_append", flaky)
    await ledger.credit("acct9", "ETH", 1_000_000, "seed-9")
    await ledger.schedule("acct9", "ETH", 510_000, "req9", "test")
    row = {
        "_id": "req9",
        "account_id": "acct9",
        "asset": "ETH",
        "amount_groth": 500_000,
        "fee_groth": 10_000,
    }
    with pytest.raises(RuntimeError):
        await payouts._book_release(row)
    assert await ledger.find_entry("release", "req9") is not None
    assert await ledger.find_entry("fee", "req9") is None
    assert (await ledger.balance("acct9", "ETH"))["scheduled"] == 10_000

    state["fail"] = False
    await payouts._book_release(row)
    assert await ledger.find_entry("fee", "req9") is not None
    assert (await ledger.balance("acct9", "ETH"))["scheduled"] == 0
    assert (await ledger.balance("acct9", "ETH"))["sent"] == 500_000


async def test_the_database_refuses_a_second_release_or_fee_entry(mock_db):
    await ledger.ensure_indexes()
    await ledger.credit("acct9", "ETH", 1_000_000, "seed-9")
    await ledger.release("acct9", "ETH", 500_000, 10_000, "req9", "first")
    with pytest.raises(DuplicateKeyError):
        await ledger._append("acct9", "ETH", "release", 500_000, 0, -500_000, 500_000, "req9", "x")
    with pytest.raises(DuplicateKeyError):
        await ledger._append("acct9", "ETH", "fee", 10_000, 0, -10_000, 0, "req9", "x")
    # …and calling release again is a no-op, not a second debit
    assert await ledger.release("acct9", "ETH", 500_000, 10_000, "req9", "again") == []
    assert (await ledger.balance("acct9", "ETH"))["sent"] == 500_000


# ================================================== §7 · the pipe receives deposits (8, 19)


async def test_a_deposit_into_the_pipe_cannot_hide_the_delivery(mock_db, eth):
    """`EthPipe.sendFunds` is payable and the pipe's ETH balance IS the locked ETH, so every
    Pgas.me deposit RAISES it — and the walk only entered its bisect when the pipe's balance
    FELL across the window. One 0.02 ETH deposit ten blocks after a 0.005 ETH delivery made the
    window's net change positive, the bisect never ran, and the caller then checkpointed past
    the delivery block forever."""
    amount_wei = 500_000 * ETH.grid
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base, block = 14_000, 14_350
    eth.credit(block, ETH.pipe, -amount_wei)
    eth.credit(block, W, amount_wei)
    eth.credit(block + 10, ETH.pipe, 20_000_000_000_000_000)  # an ordinary 0.02 ETH deposit
    found, _scanned, _bal = await payouts.find_delivery(
        eth, ETH.pipe, W, amount_wei, base, eth.head, max_steps=200
    )
    assert found and found["block"] == block and found["proof"] == "pair"


async def test_a_deposit_in_the_very_same_block_is_added_back_before_the_drop_is_judged(
    mock_db, eth
):
    """When the masking deposit is in the delivery's OWN block the pipe's net move is positive,
    so the raw pair cannot close. The corroboration is the value the pipe PAID OUT: the block's
    own `sendFunds` inflows are added back, and what remains is a real 0.005 ETH payment."""
    amount_wei = 500_000 * ETH.grid
    deposit_wei = 20_000_000_000_000_000
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base, block = 14_000, 14_350
    eth.credit(block, ETH.pipe, -amount_wei)
    eth.credit(block, W, amount_wei)
    eth.credit(block, ETH.pipe, deposit_wei)
    eth.blocks[block] = {
        "transactions": [{"to": ETH.pipe, "hash": "0x" + "ee" * 32, "value": hex(deposit_wei)}]
    }
    found, _scanned, _bal = await payouts.find_delivery(
        eth, ETH.pipe, W, amount_wei, base, eth.head, max_steps=200
    )
    assert found and found["block"] == block and found["proof"] == "pair"
    assert found["pipe_drop_wei"] == -(deposit_wei - amount_wei)  # the RAW move is upwards
    assert found["pipe_paid_wei"] == amount_wei  # …and the adjusted one is the delivery


async def test_a_deposit_alone_is_not_a_delivery_however_convenient_the_block_is(mock_db, eth):
    """⛔ THE CORROBORATION MUST IDENTIFY THE FLOW, NOT THE PIPE. The fallback proof used to be
    "some transaction in this block has `to == pipe`" — and `EthPipe.sendFunds` is exactly such
    a transaction, so every Pgas.me deposit qualified. W rising from an unrelated source in the
    same block as any deposit marked an undelivered payout `sent`, with the user already
    debited at bridging and the SLA silenced for good."""
    amount_wei = 500_000 * ETH.grid
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base, block = 14_000, 14_350
    eth.credit(block, W, amount_wei)  # somebody else paid W
    eth.credit(block, ETH.pipe, 3 * 10**18)  # and one of our own deposits landed
    eth.blocks[block] = {
        "transactions": [{"to": ETH.pipe, "hash": "0xdeposit", "value": hex(3 * 10**18)}]
    }
    found, scanned, _bal = await payouts.find_delivery(
        eth, ETH.pipe, W, amount_wei, base, eth.head, max_steps=200
    )
    assert found is None and scanned >= block  # the pipe paid nothing out: not a delivery


async def test_a_rise_that_is_not_ours_is_not_a_delivery(mock_db, eth):
    """W rising is a candidate, never a proof: without the pipe falling (or the relayer's own
    call) it is somebody paying W."""
    amount_wei = 500_000 * ETH.grid
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    eth.credit(15_000, W, amount_wei)  # a rise with no pipe leg at all
    found, scanned, _bal = await payouts.find_delivery(
        eth, ETH.pipe, W, amount_wei, 14_000, eth.head, max_steps=200
    )
    assert found is None and scanned >= 15_000


async def test_a_masked_delivery_still_walks_the_order_to_sent(mock_db, eth, armed, beam_wallet):
    """End to end: the ETH is in W, the pipe's window is net-positive, and the order must still
    reach `sent` rather than sitting in `delivering` while the ledger says it was paid."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base = eth.head
    await make_payout(
        mock_db, status="delivering", beam_txid="t1", eth_from_block=base, eth_scan_from=base
    )
    block = base + 350
    eth.head = base + 1000
    eth.credit(block, ETH.pipe, -500_000 * ETH.grid)
    eth.credit(block, W, 500_000 * ETH.grid)
    eth.credit(block + 10, ETH.pipe, 20_000_000_000_000_000)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "sent" and row["eth_block"] == block


# ================================================== §8 · a guard keeps its own deadline (9, 20)


async def test_the_delivering_sla_still_fires_after_an_ordinary_scan_pass(
    mock_db, eth, armed, paged
):
    """`_payout_delivering` wrote a checkpoint on EVERY pass and `_set` stamped `updated_at`,
    so a delivering row was never older than 30 seconds and the 18-hour SLA — the one monitor
    for a crossing whose bETH is already burned — never fired once."""
    old = time.time() - 30 * 3600
    await make_payout(
        mock_db,
        status="delivering",
        beam_txid="t1",
        eth_from_block=100,
        eth_scan_from=100,
        updated_at=old,
        status_at=old,
    )
    eth.start = {ETH.pipe.lower(): 10**18}
    await workers.stuck_checks()
    assert any(t.startswith("STUCK: direct payout delivering") for t in paged)

    paged.clear()
    tg._last_sent.clear()
    await payouts.process_once()  # the ordinary 30-second pass, which really does scan
    row = await payout(mock_db)
    assert row["eth_scan_from"] > 100
    await workers.stuck_checks()
    assert any(t.startswith("STUCK: direct payout delivering") for t in paged)


async def test_a_row_written_before_status_at_existed_is_still_watched(mock_db, paged):
    """Rows already in the database have no `status_at`; they must fall back to `updated_at`,
    never become invisible."""
    await make_payout(mock_db, status="bridging", beam_txid="t1", updated_at=time.time() - 30 * 3600)
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$unset": {"status_at": ""}}
    )
    await workers.stuck_checks()
    assert any(t.startswith("STUCK: direct payout bridging") for t in paged)


# ================================================== §9 · the float is a cycle-level guard (11, 25)


async def test_the_shielded_float_is_reserved_across_one_pass(mock_db, eth, armed, beam_wallet, beam_pay):
    """`available_mp` cannot fall between two orders in one pass — for a BVM invocation it may
    not fall until the kernel registers at all — so a per-order read admitted three 0.005 ETH
    payouts against 0.006 ETH of float. In production the surplus sends fail inside the wallet,
    which IS the lost-response state the resolver then mis-attributed."""
    beam_pay.addresses[MP]["available"]["36"] = 600_000
    beam_pay.addresses[TREASURY]["available"]["36"] = 0
    for rid in ("req1", "req2", "req3"):
        await make_payout(mock_db, rid=rid)
    await payouts.process_once()
    assert beam_wallet.methods().count("process_invoke_data") == 1
    rows = [await payout(mock_db, r) for r in ("req1", "req2", "req3")]
    assert sorted(r["status"] for r in rows) == ["releasing", "scheduled", "scheduled"]
    waiting = [r for r in rows if r["status"] == "scheduled"]
    assert all("already committed to crossings in flight" in r["hold_reason"] for r in waiting)


async def test_the_beam_fee_budget_is_the_passs_own_not_one_calls(mock_db, eth, armed, beam_wallet, beam_pay):
    """Same law, the other resource: N claims are about to be paid for out of one BEAM balance
    read, and a leg-level check cannot see the other N−1 legs."""
    # one derived claim budget (the 0.15-BEAM floor), not two
    beam_pay.addresses[TREASURY]["available"]["0"] = 20_000_000
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    for dep_id in ("depA", "depB"):
        await make_deposit(mock_db, dep_id=dep_id)
    await payouts.process_once()  # both → claiming
    await payouts.process_once()
    assert beam_wallet.methods().count("process_invoke_data") == 1
    held = [
        d
        for d in await mock_db["pgasme_test"].deposits.find({}).to_list(5)
        if "reserves" in str(d.get("hold_reason") or "")
    ]
    assert len(held) == 1 and "BEAM" in held[0]["hold_reason"]


# ================================================== §10 · the claim's own evidence first (12)


async def test_a_lost_claim_asks_view_incoming_before_any_transaction(
    mock_db, eth, armed, beam_wallet
, beam_pay):
    """The strongest evidence available was consulted only AFTER `find_contract_tx` had already
    matched — i.e. precisely when it would have refused. A deposit whose response was lost
    adopted an unrelated claim, marched claiming → claimed → shielding, and its own bETH sat
    unclaimed on the pipe forever while the user had already been credited at the lock."""
    await make_deposit(mock_db, dep_id="depB")
    d = mock_db["pgasme_test"]
    await d.deposits.update_one(
        {"_id": "depB"},
        {
            "$set": {
                "eth.msg_id": 333,
                "treasury": "claiming",
                "treasury_at": time.time() - 60,
                "claim_call_at": time.time() - 60,
            }
        },
    )
    # a claim of exactly this value is on the wallet — and it is NOT ours
    record(beam_pay, claim_tx("someone-elses-claim", 12_000_000))
    beam_wallet.incoming = [{"msg_id": 333, "amount": 12_000_000}]  # OUR message is still there
    await payouts.process_once()
    row = await deposit(mock_db, "depB")
    assert row["treasury"] == "claiming"
    assert "claim_txid" not in row  # not adopted: our message is still unclaimed
    # …and the call marker is KEPT, which is what stops the next pass signing again
    assert row["claim_call_at"] > 0
    assert beam_wallet.methods().count("process_invoke_data") == 0

    await payouts.process_once()
    row = await deposit(mock_db, "depB")
    assert "claim_txid" not in row and beam_wallet.methods().count("process_invoke_data") == 0


async def test_a_claim_whose_message_is_gone_and_unidentifiable_is_held_for_a_human(
    mock_db, eth, armed, beam_wallet
, beam_pay):
    await make_deposit(mock_db)
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"},
        {"$set": {"treasury": "claiming", "claim_call_at": time.time() - 3600}},
    )
    beam_wallet.incoming = []  # the message is consumed…
    # …and nothing in BeamPay's history accounts for it
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "held" and "a human must reconcile this" in row["hold_reason"]


# ================================================== §11 · what the send actually spends (13)


async def test_a_release_refuses_while_unshielded_inventory_exists(
    mock_db, eth, armed, beam_wallet, monkeypatch
, beam_pay):
    """The gate reads `available_mp`; the WALLET picks the inputs, and spec question S2 is
    unanswered. Either the invocation cannot spend max-privacy inputs (a gate that passed
    produces "not enough inputs", i.e. the lost-response state) or it quietly funds from
    freshly-claimed regular outputs — the on-chain claim → payout link §9.3 forbids. Refuse
    rather than assume."""
    beam_pay.addresses[TREASURY]["available"]["36"] = 1_000  # unshielded, at the treasury
    await make_payout(mock_db)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "spec S2" in row["hold_reason"]
    assert "process_invoke_data" not in beam_wallet.methods()

    monkeypatch.setattr(settings, "beam_send_inputs_proven", True)
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "releasing"


# ================================================== §12 · the relayer's cut (14)


async def test_a_crossing_that_costs_more_than_the_fee_charged_is_refused(mock_db, eth, armed):
    """The only economic gate was the relayer's share of the AMOUNT. `fee_groth` — the 2%
    debited from the user, the money that is supposed to pay the relayer — was never read, so
    at ordinary mainnet gas the treasury paid several times what it collected, silently, on
    every minimum payout."""
    await make_payout(mock_db, fee_groth=1_000)  # 3600 groth of relayer against 1000 collected
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "refusing to cross at a loss" in row["hold_reason"]
    assert "invoke_contract" not in beam.wallet().methods()


async def test_the_b2e_relayer_fee_never_goes_below_the_floor(
    mock_db, eth, armed, monkeypatch
):
    """Nothing bounded the fee BELOW on the b2e path, and a fee the relayer will not pick up
    means the message stalls for days with the bETH already burned and no refund path."""
    monkeypatch.setattr(settings, "min_relayer_fee_wei", 10**14)  # 10,000 groth
    fee, floor, detail = await payouts.relayer_fee_for(ETH, eth)
    assert floor == 10_000 and fee == 10_000 and detail["fee_before_floor_groth"] == 3600
    await make_payout(mock_db)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["relayer_fee_groth"] == 10_000 and row["relayer_fee_floor_groth"] == 10_000
    assert send_args(beam.wallet()).endswith("relayerFee=10000")


# ================================================== §13 · dark describes the current wait (16, 21)


async def test_dark_is_cleared_when_the_next_hold_is_a_real_one(
    mock_db, eth, beam_wallet, monkeypatch, paged
, beam_pay):
    """`dark` was set and never unset, so a row held once while a flag was off stayed invisible
    to every stuck check after the operator armed it — exactly when the monitor matters most."""
    await make_payout(mock_db, release_at=time.time() - 4 * 3600)
    await payouts.process_once()  # flag off → a deliberate, dark wait
    assert (await payout(mock_db))["dark"] is True

    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    beam_pay.addresses[MP]["available"]["36"] = 100
    await payouts.process_once()  # armed, and now genuinely stuck on the float
    row = await payout(mock_db)
    assert "shielded float" in row["hold_reason"] and "dark" not in row

    paged.clear()
    tg._last_sent.clear()
    await workers.stuck_checks()
    assert any(t.startswith("STUCK: payout due") for t in paged)


async def test_a_dark_treasury_row_is_also_cleared_when_the_flag_is_armed(
    mock_db, eth, beam_wallet, monkeypatch, paged
):
    await make_deposit(mock_db)
    await payouts.process_once()  # → claiming
    await payouts.process_once()  # PGAS_CLAIM_ENABLED=0 → dark
    assert (await deposit(mock_db))["dark"] is True

    monkeypatch.setattr(settings, "claim_enabled", True)
    beam_wallet.incoming = []  # armed, and the relayer has not delivered
    await payouts.process_once()
    row = await deposit(mock_db)
    assert "has not delivered" in row["hold_reason"] and "dark" not in row
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"}, {"$set": {"treasury_at": time.time() - 3 * 3600}}
    )
    paged.clear()
    tg._last_sent.clear()
    await workers.stuck_checks()
    assert any(t.startswith("STUCK: treasury claiming") for t in paged)


# ================================================== §14 · the shield's destination (17)


async def test_the_shield_target_must_be_proven_to_belong_to_this_wallet(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """A configured value used to win verbatim with no check at all. One typo sends every chunk
    of every claimed deposit to a stranger — one "Treasury: shielding …" success and one
    settled kernel at a time — and a plain `regular` address is a perfectly valid `/withdraw`
    target, so the shield "succeeds" while shielding nothing and every payout then starves on a
    float that can never fill.

    ⚠️ BeamPay's `/validate_address` answers only `is_valid`, so the proof is assembled from
    what it CAN answer: valid · in BeamPay's own address book (the `is_mine` substitute, and
    the thing that makes the float readable at all) · not a 64/66-hex regular address."""
    stranger = "AStrangersMaxPrivacyToken" + "w" * 60
    monkeypatch.setattr(settings, "beam_mp_address", stranger)
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    beam_pay.fund(TREASURY, 36, 1_000_000)
    for _ in range(5):
        await payouts.process_once()
    row = await deposit(mock_db)
    # valid, but NOT ours: BeamPay has never heard of it, so its balance can never be read
    assert row["treasury"] == "shielding" and "could not be proven" in row["hold_reason"]
    assert "not in BeamPay's address book" in row["hold_reason"]
    assert beam_pay.withdrawals == []

    # ours, but a REGULAR address: shielding to it settles and shields nothing
    regular = "77" + "bb" * 32
    monkeypatch.setattr(settings, "beam_mp_address", regular)
    beam_pay.register(regular, "regular")
    await payouts.process_once()
    row = await deposit(mock_db)
    assert "would settle and shield nothing" in row["hold_reason"]
    assert beam_pay.withdrawals == []

    # ours, and a max-privacy token
    beam_pay.register(stranger, "max_privacy")
    monkeypatch.setattr(settings, "beam_mp_address", stranger)
    await payouts.process_once()
    assert beam_pay.withdrawals[-1]["to_address"] == stranger
    proof = await mock_db["pgasme_test"].treasury.find_one({"_id": "mp_address"})
    assert proof["proven_at"] > 0 and proof["type"] == "max_privacy" and proof["is_mine"] is True
    assert proof["proven"] == [
        "validate_address", "beampay_registered", "not_a_regular_address"
    ]


async def test_an_invalid_shield_target_is_refused_before_anything_is_sent(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """The wallet's own verdict, proxied through BeamPay, is what catches a typo."""
    typo = "MaxPrivacyTokenWithATypo" + "v" * 60
    monkeypatch.setattr(settings, "beam_mp_address", typo)
    beam_pay.register(typo, "max_privacy")
    beam_pay.invalid.add(typo)
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    beam_pay.fund(TREASURY, 36, 1_000_000)
    for _ in range(5):
        await payouts.process_once()
    row = await deposit(mock_db)
    assert "not a valid Beam address" in row["hold_reason"]
    assert beam_pay.withdrawals == []


# ================================================== §15 · book first, mark second (22)


async def test_a_transient_ledger_failure_leaves_the_release_bookable(
    mock_db, eth, armed, beam_wallet, monkeypatch
):
    """`kernel_at` was written BEFORE the money entry, so one Mongo blip inside `ledger.release`
    left the marker set and the release unbooked forever: the user's money parked in Scheduled,
    `sent` never showing it, cancel refusing — and a later failure path would have REFUNDED a
    payout whose bETH was burned and whose ETH was delivered."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W}}
    await payouts.process_once()  # → releasing
    await payouts.process_once()  # → bridging

    real = ledger.release
    boom = {"on": True}

    async def flaky(*a: Any, **kw: Any) -> Any:
        if boom["on"]:
            raise RuntimeError("not primary")
        return await real(*a, **kw)

    monkeypatch.setattr(ledger, "release", flaky)
    await payouts.process_once()
    row = await payout(mock_db)
    assert "kernel_at" not in row
    assert await ledger.find_entry("release", "req1") is None
    assert (await ledger.balance("acct1", "ETH"))["scheduled"] == 510_000

    boom["on"] = False
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["kernel_at"] > 0
    bal = await ledger.balance("acct1", "ETH")
    assert bal["sent"] == 500_000 and bal["scheduled"] == 0


# ================================================== §16 · the batch is not a FIFO (24)


async def test_a_full_batch_of_dark_orders_does_not_starve_a_due_payout(
    mock_db, eth, armed, beam_wallet, monkeypatch
):
    """Rows that can never advance stayed in the one FIFO forever: fifty of them, all with old
    `release_at`, occupied the whole batch and a healthy due payout sat `scheduled` with no
    hold_reason at all — never even reached to be marked, so no monitor saw it either."""
    monkeypatch.setattr(settings, "hold_backoff_s", 3600.0)
    old = time.time() - 10 * 86400
    for i in range(payouts.BATCH):
        await make_payout(
            mock_db, rid=f"dark{i}", status="waiting_for_dep_eth", release_at=old
        )
    await make_payout(mock_db, rid="healthy")
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    assert (await payout(mock_db, "healthy"))["status"] == "releasing"


async def test_orders_that_hold_every_pass_stop_crowding_out_work(
    mock_db, eth, armed, beam_wallet, monkeypatch
):
    monkeypatch.setattr(settings, "hold_backoff_s", 3600.0)
    old = time.time() - 10 * 86400
    for i in range(payouts.BATCH):
        await make_payout(mock_db, rid=f"instant{i}", mode="instant", release_at=old)
    await make_payout(mock_db, rid="healthy")
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    assert (await payout(mock_db, "healthy"))["status"] == "scheduled"  # the batch was full
    await payouts.process_once()  # …and now the fifty are cooling off
    assert (await payout(mock_db, "healthy"))["status"] == "releasing"


async def test_a_full_batch_is_said_out_loud(mock_db, paged):
    old = time.time() - 10 * 86400
    for i in range(payouts.BATCH):
        await make_payout(mock_db, rid=f"dark{i}", status="waiting_for_dep_eth", release_at=old)
    await payouts.process_once()
    assert any(t.startswith("BACKLOG:") for t in paged)


# ================================================== §17 · denominations are a value (26)


def test_shield_denominations_are_sized_per_asset():
    """`shield_denoms_groth` is 0.01/0.1 ETH; the same integers read as 0.01/0.1 DAI, so a
    1000-DAI deposit became 10,000 sends and 110 BEAM of fees over 83 hours — and the 2-hour
    `shielding` SLA could not fire while they ground out, because each success refreshed the
    row."""
    assert settings.shield_denoms_for("ETH") == [10_000_000, 1_000_000]
    assert settings.shield_denoms_for("DAI") == [10_000_000_000, 1_000_000_000]
    assert settings.shield_denoms_for("WBTC") == [10_000_000, 1_000_000]
    assert len(payouts.shield_plan(1000 * 10**8, settings.shield_denoms_for("DAI"))) == 10
    assert payouts.shield_plan(50 * 10**8, settings.shield_denoms_for("DAI")) == [
        1_000_000_000
    ] * 5
    assert payouts.shield_plan(50_000_000, settings.shield_denoms_for("WBTC")) == [
        10_000_000
    ] * 5


async def test_a_shield_plan_longer_than_the_cap_is_refused_not_emitted(
    mock_db, eth, armed, beam_wallet
):
    await make_deposit(mock_db, value=10 * 10**8)  # 10 ETH → 100 × 0.1 ETH
    beam_wallet.incoming = [{"msg_id": 222, "amount": 10 * 10**8}]
    for _ in range(4):  # → claiming → claim sent → claimed → the plan is built and REFUSED
        await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claimed"
    assert "100 chunks (limit 64)" in row["hold_reason"]
    assert "tx_send" not in beam_wallet.methods()


# ================================================== §18 · logs are scrubbed of W (27)


def test_redact_hides_addresses_and_leaves_ids_alone():
    assert beam.redact(f"receiver={W}") == "receiver=<redacted-address>"
    assert beam.redact(W[2:]) == "<redacted-address>"
    # a pipe cid is 64 hex characters and must survive intact — a naive 40-hex rule ate its
    # middle and hid nothing
    assert ETH.beam_cid in beam.redact(f"cid={ETH.beam_cid},receiver={W}")
    assert W.lower() not in beam.redact(f"cid={ETH.beam_cid},receiver={W}").lower()


async def test_no_destination_address_reaches_the_application_log(
    mock_db, eth, armed, beam_wallet, caplog
):
    """§9.7: "logs are scrubbed of W at the logging layer with tests". api.log otherwise carries
    request_id → destination-wallet for every payout, and payout_requests carries request_id →
    account_id — the whole product defeated by a log file that survives the settlement purge."""
    caplog.set_level(logging.DEBUG, logger="pgasme")
    await make_payout(mock_db)
    await make_deposit(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W}}
    for _ in range(5):
        await payouts.process_once()
    assert (await payout(mock_db))["status"] in ("bridging", "delivering", "sent")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "calldata verified" in text  # the release really did run through the logging path
    assert not ADDRESS_RE.search(text)
    assert W.lower() not in text.lower() and W[2:].lower() not in text.lower()
    row = await payout(mock_db)
    assert "<redacted-address>" in row["release_args"]


async def test_the_dark_release_log_names_the_request_not_the_wallet(
    mock_db, eth, beam_wallet, caplog
):
    caplog.set_level(logging.INFO, logger="pgasme")
    await make_payout(mock_db)
    await payouts.process_once()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "would release payout req1" in text
    assert not ADDRESS_RE.search(text)


async def test_a_malformed_destination_is_not_echoed_to_the_operator(
    mock_db, eth, armed, beam_wallet
):
    """`build_bridge_send` raised `receiver {addr!r} is not an EVM address` and the refusal
    alert echoes the exception verbatim into Telegram."""
    bad = "1111111111111111111111111111111111111111"  # a 40-hex string with no 0x
    await make_payout(mock_db, W=bad)
    await payouts.process_once()
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "payout_build_refused"})
    assert ev is not None
    assert bad not in ev["text"] and "<redacted-address>" in ev["text"]
    assert (await payout(mock_db))["status"] == "scheduled"


# ============ §19 · the four findings of the final re-verification (2026-09-09, T9c) =========
#
# Every test below fails on the code as it was shipped that morning:
#   1 the release/fee guard used `$in` inside a partialFilterExpression — MongoDB 5.0 (what
#     production runs) answers CannotCreateIndex (67), so ensure_indexes RAISED at worker start
#     and /v1/health.indexes_ok would have refused the next deploy
#   2 `taken_txids()` was a 1000-row page with no sort, so the NEWEST evidence — the only kind a
#     lost response can collide with — fell out of the set the moment the table grew
#   3 `find_local_msg`'s 40-message window is smaller than one pass's 50 releases, so a payout's
#     own outgoing message became unfindable and the row stuck in `bridging` with the bETH burnt
#   4 a `shielding` row written before `shield_ids` existed got a MAP from the dotted `$set`,
#     and the retry read its keys — resending chunk 0 under the txId "0"

# What MongoDB 5.0 accepts inside a partialFilterExpression: equality, $exists, $type, the
# range operators and a top-level $and. NOT $in, $or, $nin, $ne, $regex, $expr.
MONGO_5_0_PARTIAL_OPS = {"$eq", "$exists", "$type", "$and", "$gt", "$gte", "$lt", "$lte"}


def _ops(expr: Any) -> set[str]:
    """Every operator name anywhere inside a filter expression."""
    if isinstance(expr, dict):
        out: set[str] = {k for k in expr if str(k).startswith("$")}
        for v in expr.values():
            out |= _ops(v)
        return out
    if isinstance(expr, list):
        return set().union(*(_ops(v) for v in expr)) if expr else set()
    return set()


async def test_no_index_filter_uses_an_operator_mongodb_5_0_refuses(mock_db):
    """⛔ A partial index filter is not a query, and prod is 5.0.

    `ensure_indexes` runs at worker start and its failure is what `/v1/health.indexes_ok`
    reports and `deploy.sh` refuses a deploy on. One unsupported operator in one filter is a
    boot failure, so this asserts the property for EVERY index this build creates, on every
    collection, not just the one that had it."""
    assert await dbmod.ensure_indexes() == []
    await ledger.ensure_indexes()
    await payouts.ensure_indexes()
    await scanner.ensure_indexes()
    d = dbmod.db()
    seen = 0
    for coll in await d.list_collection_names():
        for name, spec in (await d[coll].index_information()).items():
            expr = spec.get("partialFilterExpression")
            if expr is None:
                continue
            seen += 1
            assert _ops(expr) <= MONGO_5_0_PARTIAL_OPS, f"{coll}.{name}: {expr}"
    assert seen >= 4  # credit, release, fee, src_tx_hash, the payout/claim txid guards
    # the release/fee guard is now ONE INDEX PER KIND, each with an equality filter…
    entries = await d.entries.index_information()
    for name, kind in ((ledger.RELEASE_REF_INDEX, "release"), (ledger.FEE_REF_INDEX, "fee")):
        assert entries[name]["unique"] is True
        assert entries[name]["partialFilterExpression"] == {"kind": kind}
    # …with a key pattern each, because the documented restriction is that two partial indexes
    # may not differ only by their filter expression
    patterns = [tuple(tuple(k) for k in entries[n]["key"]) for n in (ledger.RELEASE_REF_INDEX, ledger.FEE_REF_INDEX)]
    assert len(set(patterns)) == 2
    # …and db.py and ledger.py create them under the SAME names, or Mongo answers
    # IndexOptionsConflict (85) at every boot and the health check goes red
    assert (ledger.RELEASE_REF_INDEX, ledger.FEE_REF_INDEX) == (
        dbmod.RELEASE_REF_INDEX,
        dbmod.FEE_REF_INDEX,
    )
    assert dbmod.LEGACY_RELEASE_FEE_INDEX not in entries
    # the guard still guards: one release and one fee per ref, and the two share the ref
    await ledger.credit("acctT9", "ETH", 10_000_000, "seed-T9")
    await ledger.release("acctT9", "ETH", 500_000, 10_000, "refT9", "first")
    for kind in ("release", "fee"):
        with pytest.raises(DuplicateKeyError):
            await ledger._append("acctT9", "ETH", kind, 1, 0, -1, 0, "refT9", "again")


async def test_a_txid_is_taken_however_deep_in_the_table_it_sits(mock_db, eth, armed, beam_wallet, beam_pay):
    """⛔ A set-membership test must not depend on a window.

    `taken_txids()` read 1000 rows with no sort and answered from that page. In production it is
    the only guard between a payout whose response was lost and another order's transaction, and
    the rows it dropped were the RECENT ones — the only ones such a collision can involve."""
    d = mock_db["pgasme_test"]
    await payouts.ensure_indexes()
    # ⚠️ mongomock does NOT honour to_list(length), so the truncation itself cannot be exhibited
    # in this suite — the depth assertions below state the law, and the shipped page is shown
    # dropping the newest txid against a real mongod 5.0 in the T9c transcript. What DOES fail
    # here on the old code is the shield id, the exclusions and the end-to-end refusal.
    await d.payout_requests.insert_many(
        [{"_id": f"old-{i}", "status": "sent", "beam_txid": f"tx-old-{i}"} for i in range(1100)]
    )
    await d.deposits.insert_many(
        [{"_id": f"old-dep-{i}", "claim_txid": f"tx-claim-{i}"} for i in range(1100)]
    )
    await d.deposits.insert_one({"_id": "shielded", "shield_txids": ["s-0", "s-1"]})
    # the newest evidence is exactly what the page dropped
    assert await payouts.txid_is_taken("tx-old-1099") is True
    assert await payouts.txid_is_taken("tx-claim-1099") is True
    assert await payouts.txid_is_taken("tx-old-0") is True
    # a shield is a tx_send of the treasury's own money; its id is evidence too
    assert await payouts.txid_is_taken("s-1") is True
    # …and a row's own txid is not "taken" from itself, nor is one nobody carries
    assert await payouts.txid_is_taken("tx-old-1099", exclude_request="old-1099") is False
    assert await payouts.txid_is_taken("tx-claim-1099", exclude_deposit="old-dep-1099") is False
    assert await payouts.txid_is_taken("tx-nobody-has-this") is False
    assert await payouts.txid_is_taken("") is False

    # end to end: a lost release response, and the ONLY matching transaction is the 1100th row's.
    # It must be refused by IDENTITY before the row is touched — not by the unique index after.
    now = time.time()
    await make_payout(mock_db, rid="reqZ", status="releasing", relayer_fee_groth=3600,
                      release_call_at=now - 60, msg_floor=0)
    beam_wallet.local_msgs = {1: {"amount": 500_000, "receiver": W}}
    record(beam_pay, send_tx("tx-old-1099", 503_600, now))
    await payouts.process_once()
    row = await payout(mock_db, "reqZ")
    assert "beam_txid" not in row
    assert row.get("hold_reason") is None  # the DATABASE never had to be the one to refuse it
    assert await payouts.txid_is_taken("tx-old-1099", exclude_request="reqZ") is True


async def test_a_release_finds_its_own_message_past_the_fixed_window(
    mock_db, eth, armed, beam_wallet
):
    """⛔ The floor is the release, not a window.

    One pass releases up to BATCH=50 orders; the look-back was 40 messages. `find_local_msg`
    answering None is not "not yet" — it is the disproof that HOLDS the payout, so the first
    orders of a full pass were unbookable in `bridging` for good, with the bETH already burnt
    and no refund path. The floor is now the pipe's own message count read before the send."""
    beam_wallet.local_msgs = {i: {"amount": 11, "receiver": OTHER_W} for i in range(1, 101)}
    await make_payout(mock_db)
    await payouts.process_once()  # scheduled → releasing, and the send
    row = await payout(mock_db)
    assert row["status"] == "releasing" and row["msg_floor"] == 100  # read BEFORE anything signed

    ours = 101
    beam_wallet.local_msgs[ours] = {"amount": 500_000, "receiver": W, "relayerFee": 3600}
    for i in range(102, 162):  # the rest of the pass and then some: 61 later messages
        beam_wallet.local_msgs[i] = {"amount": 7, "receiver": OTHER_W}
    assert ours < max(beam_wallet.local_msgs) - beam.LOCAL_MSG_WINDOW  # outside the old window

    await payouts.process_once()  # releasing → bridging
    await payouts.process_once()  # the kernel confirmed: find OUR message, then book
    row = await payout(mock_db)
    assert row["msg_id"] == ours and row.get("hold_reason") is None
    entry = await ledger.find_entry("release", "req1")
    assert entry and entry["groth"] == 500_000
    # the two halves of the fix, stated directly
    assert await beam_wallet.find_local_msg(ETH.beam_cid, W, 500_000) is None  # windowed: lost
    assert await beam_wallet.find_local_msg(ETH.beam_cid, W, 500_000, from_msg_id=100) == ours


async def test_a_shielding_row_written_before_shield_calls_does_not_send_twice(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ A dotted `$set` into a field that does not exist makes a MAP, not a list.

    `shield_calls` is initialised as a list when the row enters `shielding` — but a row already
    in `shielding` from the build before that has none, so `{"$set": {"shield_calls.0": now}}`
    created `{"0": now}` and the next pass read `list({"0": now})` == `["0"]`. Chunk 0's marker
    then read as the string "0", which is falsy-by-`float()`-failure territory and, worse, is
    not the timestamp the deadline is measured from. With `/withdraw` carrying no idempotency
    key at all, a marker that reads as "never called" is a SECOND withdrawal of the treasury's
    money that nothing on either side can merge."""
    d = mock_db["pgasme_test"]
    await make_deposit(mock_db, value=200_000)
    beam_pay.fund(TREASURY, 36, 200_000)
    await d.deposits.update_one(
        {"_id": "dep1"},
        {
            "$set": {
                "treasury": "shielding",
                "treasury_at": time.time(),
                "shield_plan": [100_000, 100_000],
                "shield_txids": [],
            }
        },  # NO shield_calls
    )
    beam_pay.withdraw_lands = False  # queued, but its transaction is not visible yet
    await payouts.process_once()
    row = await deposit(mock_db)
    assert len(beam_pay.withdrawals) == 1
    assert row["shield_calls"] == [pytest.approx(row["shield_calls"][0])]
    assert isinstance(row["shield_calls"], list) and row["shield_calls"][0] > 0
    assert row["shield_txids"] == []  # nothing is booked: the transaction has not appeared

    # the pass that follows must NOT call /withdraw again — the marker is what stops it
    await payouts.process_once()
    assert len(beam_pay.withdrawals) == 1

    # …and once BeamPay's daemon emits it, the comment is what identifies it
    beam_pay.add_tx(
        txId="wd-late",
        type=4,
        type_string="simple",
        asset_id="36",
        value="100000",
        fee="1100000",
        sender=TREASURY,
        receiver=MP,
        comment="shield|dep1|0",
        kernel="kernel-wd-late",
    )
    beam_pay.withdraw_lands = True
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["shield_txids"] == ["wd-late"]
    assert len(beam_pay.withdrawals) == 2  # chunk 1, not a second chunk 0

    # the reader is total: a map, a list, a gap and an absent field all answer with a list
    assert payouts.shield_calls_of({"shield_calls": {"0": 5.0, "2": 7.0}}) == [5.0, 0.0, 7.0]
    assert payouts.shield_calls_of({"shield_calls": [5.0, 6.0]}) == [5.0, 6.0]
    assert payouts.shield_calls_of({}) == []


async def test_a_queued_shield_that_never_appears_is_held_for_a_human(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """`/withdraw` cannot be retried, so a chunk whose transaction never shows up is a human's
    problem — never an automatic second send."""
    await make_deposit(mock_db, value=100_000)
    beam_pay.fund(TREASURY, 36, 100_000)
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"},
        {
            "$set": {
                "treasury": "shielding",
                "treasury_at": time.time(),
                "shield_plan": [100_000],
                "shield_txids": [],
                "shield_calls": [time.time() - payouts.UNRESOLVED_S - 60],
            }
        },
    )
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "held" and row["held_from"] == "shielding"
    assert "NOT auto-retried" in row["hold_reason"]
    assert beam_pay.withdrawals == []
