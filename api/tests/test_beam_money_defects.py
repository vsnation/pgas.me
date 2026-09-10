"""One regression per money defect found in the 2026-09-09 review of the BeamPay transport.

Every test here fails on the code as it was and passes on the code as it is, and each one is
anchored to something read off the LIVE system rather than to a shape somebody imagined:

  §A  `find_contract_tx` met `{"type": "withdrawal"}` — a STRING, 2,628 of the newest 3,000
      rows in the reference implementation's tx collection — and raised ValueError, killing both lost-response resolvers
  §B  the invoke-amount SIGN: BeamPay books `available_delta = -amount` under its own comment
      "A POSITIVE invoke amount is a wallet OUTFLOW", so a b2e send is POSITIVE and a claim is
      NEGATIVE. Both resolvers asked for the opposite operation's shape
  §C  the release burned bETH before knowing the txid could be registered at all
  §D  a lost claim re-signed while `view_incoming` still listed the message — which reflects
      MINED state, not "nothing was broadcast"
  §E  the payout's BEAM fee books to the max-privacy address (`deltas["0"] -= fee`), while the
      fee gate read the treasury's balance, which a payout can never move
  §F  the kill switch was in the caller of `/withdraw`, not in the mover
  §G  BeamPay FREEZES a contract tx's `confirmations` when it books it (159 of 171 live
      contract rows read 0), so a 61-confirmation gate could never be satisfied
  §H  `is_taken` could only ever see one candidate; the float reservation was a 500-row window;
      a held burn left the reservation entirely
  §I  a delivery was "proven" by any transaction sent to the pipe — and every Pgas.me deposit
      is one; a batched delivery was invisible and got checkpointed past
  §J  `find_local_msg`'s floor included the previous payout's message
  §K  every 404 read as "BeamPay has not seen this yet", including a routing 404 from a build
      with no `/internal/*` routes at all (master `e09bfc2` — the commit this deployment was
      copied from — has none)

The fakes are the project's own (`test_beam_payout`), and they now encode the live shapes: a
withdrawal row's `type` is the string, a send's invoke amount is positive, a claim's is
negative, `confirmations` never grows, and `book_attribution` is BeamPay's own arithmetic.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
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
)
from test_beam_payout_regressions import claim_tx, record, send_tx

from pgasme import beam, beampay, ledger, payouts, tg, workers
from pgasme.config import settings

OTHER_W = "0x2222222222222222222222222222222222222222"


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    bp = FakeBeamPay()
    beampay.reset_health()
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)
    bp.fund(MP, 36, 5 * GROTH)
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    yield bp
    beampay.set_beampay(None)


@pytest.fixture(autouse=True)
def beam_wallet(monkeypatch: pytest.MonkeyPatch, beam_pay: FakeBeamPay) -> FakeWalletApi:
    w = FakeWalletApi(beam_pay)
    beam.set_wallet(w)
    payouts.reset_archive_pin()
    monkeypatch.setattr(settings, "beam_shader", w.shader)
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
    out: list[str] = []

    async def fake_send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        out.append(text)
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    return out


async def lost_release(mock_db: Any, **over: Any) -> dict[str, Any]:
    """A payout whose `process_invoke_data` answer was lost 30 s ago: the intent marker is on
    the row, there is no txid, and only the chain can say what happened."""
    now = time.time()
    return await make_payout(
        mock_db,
        status="releasing",
        relayer_fee_groth=3600,
        release_call_at=now - 30,
        release_attempt_at=now - 30,
        msg_floor=3,
        **over,
    )


async def lost_claim(mock_db: Any, seconds_ago: float = 30.0, **over: Any) -> dict[str, Any]:
    now = time.time()
    dep = await make_deposit(mock_db)
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": dep["_id"]},
        {
            "$set": {
                "treasury": "claiming",
                "treasury_at": now - seconds_ago,
                "claim_call_at": now - seconds_ago,
                **over,
            }
        },
    )
    return dep


# ============================================ §A · never int() a field production fills with a word


async def test_find_contract_tx_walks_past_the_rows_production_actually_stores(beam_pay):
    """⛔ `int(row.get("type", -1))` was evaluated BEFORE the `type_string` fallback (`and`
    cannot short-circuit an argument), and BeamPay writes a withdrawal's `type` as the STRING
    "withdrawal" at reservation time — 2,628 of the newest 3,000 rows on the live wallet. The
    walk deliberately runs with NO address filter (the only way a contract tx, whose sender and
    receiver are both "", is visible at all), so those rows are handed to it intermixed, and
    Pgas.me's own shield chunks add one more each. Every lost-response resolve on both money
    paths died with `ValueError: invalid literal for int() with base 10: 'withdrawal'`."""
    now = time.time()
    beam_pay.withdrawal_row(comment="shield|dep1|0")  # our own shield chunk, newest first
    beam_pay.withdrawal_row(comment="an ordinary payout")
    beam_pay.add_tx(**send_tx("ours", 503_600, now))
    beam_pay.withdrawal_row(comment="and another")
    assert [type(r["type"]).__name__ for r in beam_pay.tx_rows].count("str") == 3

    got = await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, +503_600)
    assert got and got["txId"] == "ours"


async def test_a_lost_release_resolves_through_a_history_of_withdrawals(mock_db, eth, armed, beam_pay):
    """The same defect where it costs money: the resolver is the ONLY thing that can recover a
    lost `process_invoke_data` answer without a second signature, and it never ran once."""
    now = time.time()
    await lost_release(mock_db)
    beam_pay.withdrawal_row(comment="shield|other|0")
    beam_pay.add_tx(**send_tx("the-real-send", 503_600, now - 20))
    beam.wallet().local_msgs = {4: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["beam_txid"] == "the-real-send" and row["resolved_from_chain"] is True
    assert beam_pay.expectations["the-real-send"]["address"] == MP


# ============================================ §B · a spend is POSITIVE, a receive is NEGATIVE


async def test_a_release_resolves_the_transaction_beampay_really_recorded(
    mock_db, eth, armed, beam_pay
):
    """BeamPay books a contract flow as `available_delta = -amount` ("A POSITIVE invoke amount
    is a wallet OUTFLOW", process_payments.py:216) and the live rows on the very pipe Pgas.me
    uses carry `{asset_id: 36, amount: +15995980}` for a 0.15995980 bETH SEND. Asking for
    −(amount + relayerFee) could never match our own send: the row went to `held` with the bETH
    burned, the burn unregistered (so it booked to `__house__`), the user's balance frozen in
    Scheduled and cancel refused."""
    await lost_release(mock_db)
    beam_pay.add_tx(**send_tx("the-real-send", 503_600))
    beam.wallet().local_msgs = {4: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["beam_txid"] == "the-real-send"
    assert row["msg_id"] == 4 and row["status"] == "releasing"
    assert beam.wallet().methods().count("process_invoke_data") == 0  # never re-signed


async def test_a_payout_never_adopts_a_deposit_claim_of_exactly_its_own_size(
    mock_db, eth, armed, beam_pay
):
    """⛔ THE INVERTED SIGN WAS NOT MERELY BLIND — IT WAS AIMED AT THE OTHER OPERATION. A claim
    on this pipe of exactly (amount + relayerFee) is precisely the shape `-(amount + fee)`
    hunted for, and the second discriminator passes because our own send really did land: the
    payout would adopt the DEPOSIT's txid, register that claim's kernel to the max-privacy
    float, book an irreversible ledger release on somebody else's crossing, and leave its own
    burn unregistered — the float overstated by roughly twice the crossing."""
    await lost_release(mock_db)
    record(beam_pay, claim_tx("a-deposits-claim", 503_600))
    beam.wallet().local_msgs = {4: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    await payouts.process_once()
    row = await payout(mock_db)
    assert "beam_txid" not in row and row["status"] == "releasing"
    assert beam_pay.expectations == {}
    assert await ledger.find_entry("release", "req1") is None
    assert (await ledger.balance("acct1", "ETH"))["sent"] == 0


async def test_a_lost_claim_resolves_the_inflow_beampay_really_recorded(
    mock_db, eth, armed, beam_pay
):
    """The mirror: a claim is an INFLOW and an inflow is NEGATIVE (a live e2b claim reads
    `{"amount": -75058, "asset_id": 38}`). Asking for +value searched for a b2e SEND — so a
    lost claim could never find its own transaction, its inflow booked to `__house__`, and the
    treasury balance that both the shield and the §9.3 unshielded-value gate read never moved."""
    await lost_claim(mock_db)
    beam.wallet().incoming = []  # our claim consumed the message
    record(beam_pay, claim_tx("the-real-claim", 12_000_000))
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["claim_txid"] == "the-real-claim" and row["resolved_from_chain"] is True
    assert beam_pay.expectations["the-real-claim"]["address"] == TREASURY


async def test_a_lost_claim_never_adopts_a_payouts_send_of_its_own_size(
    mock_db, eth, armed, beam_pay
):
    """…and the deposit must not adopt a real payout's crossing either — the incident
    `_resolve_lost_claim`'s docstring says the view_incoming-first order fixed."""
    await lost_claim(mock_db)
    beam.wallet().incoming = []
    record(beam_pay, send_tx("a-payouts-send", 12_000_000))
    await payouts.process_once()
    row = await deposit(mock_db)
    assert "claim_txid" not in row
    assert beam_pay.expectations == {}


# ============================================ §C · ask before the irreversible step


async def test_nothing_is_signed_until_beampay_can_accept_the_registration(
    mock_db, eth, armed, beam_pay
):
    """⛔ The release used to gate on the flag, the switch, the relayer's share and subsidy, the
    float, the unshielded balance and the fee budget — and then burn the bETH, and only THEN
    discover that `POST /internal/expect_contract_tx` was unreachable. BeamPay's daemon then
    settles the tx with no expectation on record, the whole flow books to `__house__`, and every
    later retry gets the terminal `tx_already_booked`. This is the first-arming shape:
    `PGAS_BEAMPAY_INTERNAL_KEY` has never been exercised on this deployment."""
    beam_pay.missing_routes.add("/internal/")  # a BeamPay built from master e09bfc2
    await make_payout(mock_db)
    await make_deposit(mock_db)
    beam.wallet().incoming = [{"msg_id": 222, "amount": 12_000_000}]
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()

    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "cannot accept a contract-tx registration" in row["hold_reason"]
    dep = await deposit(mock_db)
    assert dep["treasury"] == "claiming" and "cannot accept a contract-tx registration" in dep["hold_reason"]
    assert beam.wallet().methods().count("process_invoke_data") == 0  # NOTHING was signed

    beam_pay.missing_routes.clear()
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "releasing"
    assert beam_pay.expectations["beamtx-1"]["address"] == MP


async def test_an_unset_internal_key_is_a_refusal_before_the_burn_not_after_it(
    mock_db, eth, armed, beam_pay
):
    """`settings.secrets_ok` checks only the JWT secret and the account salt, so an unset
    `PGAS_BEAMPAY_INTERNAL_KEY` is invisible at boot. The probe calls the way the caller calls
    — same key, same route — so it is the money path that notices."""
    beam_pay.internal_key = ""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "PGAS_BEAMPAY_INTERNAL_KEY is not configured" in row["hold_reason"]
    assert beam.wallet().methods().count("process_invoke_data") == 0


async def test_the_attribution_probe_is_asked_once_per_pass_not_once_per_order(
    mock_db, eth, armed, beam_pay
):
    """N releases are about to be made against ONE answer, and a probe per order is a probe
    that costs more than the work."""
    await make_payout(mock_db, rid="r1")
    await make_payout(mock_db, rid="r2")
    await make_payout(mock_db, rid="r3")
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    probes = [b for m, p, b in beam_pay.calls if m == "GET" and p == "/internal/expect_contract_tx"]
    assert len(probes) == 1
    await make_payout(mock_db, rid="r4")
    await payouts.process_once()  # a NEW pass asks again: a key or a route can change
    probes = [b for m, p, b in beam_pay.calls if m == "GET" and p == "/internal/expect_contract_tx"]
    assert len(probes) == 2


# ============================================ §D · a retry never re-signs


async def test_a_lost_claim_is_never_re_signed_while_its_message_is_still_listed(
    mock_db, eth, armed, beam_pay
):
    """⛔ `view_incoming` reflects MINED state. A claim broadcast but not yet mined (~15 Beam
    blocks; the wallet has a TX_REGISTERING state for exactly this) leaves its message listed,
    and the handler `$unset` `claim_call_at` past the deadline — the one marker that stops the
    next pass building the call again. That is a SECOND signature over one inventory: claim #1
    consumes the message and books to `__house__` (its txid was never captured), claim #2
    reverts, and the re-plan then holds forever on "the relayer has not delivered pipe message
    222 yet", which is false."""
    await lost_claim(mock_db, seconds_ago=payouts.UNRESOLVED_S + 60)
    beam.wallet().incoming = [{"msg_id": 222, "amount": 12_000_000}]  # still listed: unmined
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "held" and row["held_from"] == "claiming"
    assert "not that nothing was signed" in row["hold_reason"]
    assert row["claim_call_at"] > 0  # ⛔ the marker is KEPT

    for _ in range(3):
        await payouts.process_once()
    assert beam.wallet().methods().count("process_invoke_data") == 0
    assert (await deposit(mock_db))["treasury"] == "held"  # a human owns it


async def test_an_in_flight_claim_is_named_in_the_hold_so_a_human_can_see_it(
    mock_db, eth, armed, beam_pay
):
    """"The message is still claimable" is not evidence that nothing was signed — and when a
    transaction of exactly this claim's identity is IN FLIGHT, that is the fact the human needs."""
    await lost_claim(mock_db, seconds_ago=payouts.UNRESOLVED_S + 60)
    beam.wallet().incoming = [{"msg_id": 222, "amount": 12_000_000}]
    row = beam_pay.add_tx(**claim_tx("unmined-claim", 12_000_000))
    row.update({"status": beam.TX_REGISTERING, "status_string": "registering", "success": False})
    await payouts.process_once()
    dep = await deposit(mock_db)
    assert dep["treasury"] == "held" and "IN FLIGHT: unmined-claim" in dep["hold_reason"]
    assert beam.wallet().methods().count("process_invoke_data") == 0


# ============================================ §E · the guard reads where the fee lands


async def test_the_beam_fee_gate_reads_the_address_the_payout_fee_books_to(
    mock_db, eth, armed, beam_pay, paged
):
    """⛔ `contract_attribution.attribution_deltas` ends with `deltas["0"] -= fee`: the whole
    BEAM transaction fee of an invocation is debited from the address the txid was REGISTERED
    to. A release registers to the max-privacy address, which holds no BEAM — so the treasury's
    asset-0 balance, which `_beam_fees_ok` read, never falls for a payout however many
    crossings drain the wallet. The gate that exists to stop a fee-starved wallet failing
    mid-chain drifted high without bound."""
    # 0.155 BEAM: enough for ONE derived send budget (the 0.15 floor) and not for two
    beam_pay.addresses[TREASURY]["available"]["0"] = 15_500_000
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await make_payout(mock_db, rid="r1")
    await payouts.process_once()
    assert (await payout(mock_db, "r1"))["beam_txid"] == "beamtx-1"

    # BeamPay's own arithmetic, applied where BeamPay applies it
    deltas = beam_pay.book_attribution("beamtx-1")
    assert deltas["0"] == -1_100_000  # the fee, off the REGISTERED address
    assert beam_pay.addresses[MP]["available"]["0"] == -1_100_000
    assert beam_pay.addresses[TREASURY]["available"]["0"] == 15_500_000  # never moved

    await make_payout(mock_db, rid="r2")
    await payouts.process_once()
    row = await payout(mock_db, "r2")
    assert row["status"] == "scheduled"  # the drain is VISIBLE to the gate now
    assert row["hold_reason"] == (
        "the wallet holds 0.144 BEAM and this call reserves 0.15 BEAM for a send "
        "(1 settled send(s) read back, worst 0.011 × 1.5, floor 0.15)"
    )
    assert any("max-privacy address's BEAM balance is -0.011" in t for t in paged)


# ============================================ §F · the kill switch lives in the mover


async def test_the_kill_switch_is_checked_inside_the_withdrawal_mover(
    mock_db, beam_pay, monkeypatch, tmp_path
):
    """`beam.Wallet.submit` checks the switch immediately before `process_invoke_data`;
    `BeamPay.withdraw` — the project's OTHER irreversible call, which moves the treasury's
    shielded asset — checked nothing at all. The guard lived in `_treasury_shielding`, so the
    window between its last look and the POST still queued the chunk, and every future call
    site (the any-asset branch, an ops script, a repair tool) inherited no guard whatsoever."""
    stop = tmp_path / "pgasme.stop"
    stop.write_text("stop")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    with pytest.raises(beam.Halted):
        await beam_pay.withdraw(TREASURY, MP, 36, 100_000, "shield|dep1|0")
    assert beam_pay.withdrawals == []  # nothing was queued
    assert "/withdraw" not in beam_pay.paths()


async def test_a_switch_thrown_mid_shield_halts_the_chain_and_keeps_the_chunk_retryable(
    mock_db, eth, armed, beam_pay, monkeypatch, tmp_path
):
    """…and the caller must survive the mover's refusal: the slot marker is released, because a
    chunk that was never queued has to stay retryable."""
    stop = tmp_path / "pgasme.stop"
    monkeypatch.setattr(settings, "stop_file", str(stop))
    dep = await make_deposit(mock_db, value=1_000_000)
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": dep["_id"]},
        {"$set": {"treasury": "shielding", "treasury_at": time.time() - 60,
                  "shield_plan": [1_000_000], "shield_calls": [], "shield_txids": []}},
    )
    real = workers.paused
    calls = {"n": 0}

    def paused_after_the_callers_checks() -> bool:
        calls["n"] += 1
        if calls["n"] >= 3:  # the switch is thrown after the caller's two looks
            stop.write_text("stop")
        return real()

    monkeypatch.setattr(workers, "paused", paused_after_the_callers_checks)
    await payouts.process_once()
    row = await deposit(mock_db)
    assert beam_pay.withdrawals == []  # the mover refused
    assert row["treasury"] == "shielding"
    assert [c["at"] for c in payouts.shield_calls_of(row)] == [0.0]


# ============================================ §G · a number BeamPay stops maintaining


async def test_a_frozen_confirmation_count_does_not_strand_the_payout_in_bridging(
    mock_db, eth, armed, beam_pay
):
    """⛔ `handle_contract_transaction` early-returns `if existing.success and existing.status
    == status` BEFORE the `$set` that writes `confirmations`, and `reconcile_unfinished_
    transactions` only visits rows with `success != True`. So once a contract tx is booked — the
    very precondition `_payout_bridging` waits for — the count is frozen at the first status-3
    sighting, which the 3-second poller reads as 0. 159 of 171 live contract rows are 0. The
    payout burned the bETH, booked the irreversible ledger release, and then sat in `bridging`
    for ever: `delivering` unreachable, `find_delivery` never executed once, the 3-hour SLA
    paging on every payout, and `inflight_groth` counting the row as committed float for good."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await make_payout(mock_db)
    await payouts.process_once()  # → releasing
    await payouts.process_once()  # → bridging
    await payouts.process_once()  # the kernel: the release is booked
    row = await payout(mock_db)
    assert row["kernel_at"] > 0 and row["beam_height_at_kernel"] == beam_pay.height_
    assert await ledger.find_entry("release", "req1") is not None

    for _ in range(20):  # ten minutes of passes with the count frozen, exactly as live
        await payouts.process_once()
    assert beam_pay.tx("beamtx-1")["confirmations"] == 0
    row = await payout(mock_db)
    assert row["status"] == "bridging" and row["beam_confirmations"] == 0

    # …and the number that DOES move is the wallet's own height
    beam_pay.height_ += settings.beam_confirmations - 1
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "bridging"
    beam_pay.height_ += 1
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "delivering" and row["beam_confirmations"] == settings.beam_confirmations


# ============================================ §H · guards that must not depend on a window


async def test_a_taken_newest_match_falls_through_to_the_older_one(beam_pay, mock_db):
    """`visit` returned True on the first full match, which ends the walk — so `candidates` held
    at most ONE row and the loop written to skip a txid that is already somebody's evidence
    could never iterate twice. Two payouts of one size in flight, one already carrying its
    txid, left the other unable to find its own transaction: `held` after 15 minutes with the
    bETH burned. `taken_txid_check` is the path production uses; only `exclude_txids` — checked
    inside `visit` — ever worked."""
    now = time.time()
    record(beam_pay, send_tx("older", 503_600, now - 300), send_tx("newest", 503_600, now - 5))

    async def is_taken(txid: str) -> bool:
        return txid == "newest"

    got = await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 600, +503_600, is_taken=is_taken)
    assert got and got["txId"] == "older"


async def test_the_float_reservation_never_depends_on_a_window(mock_db, monkeypatch):
    """⛔ `find(…).to_list(500)` with no sort, on the one guard that decides whether a pass may
    spend the float — the identical defect `txid_is_taken`'s own docstring records as fixed.
    `_due` admits BATCH rows per status every 30 s and `bridging` lasts ~61 Beam confirmations,
    so the in-flight population is far above any window; past it the reservation is understated
    by an arbitrary subset and the gate admits crossings against float already committed.

    It cannot be caught by counting rows — `mongomock` ignores the `to_list` cap, as this test
    asserts — so the QUESTION is what gets asserted."""
    d = mock_db["pgasme_test"]
    await d.payout_requests.insert_many(
        [
            {"_id": f"r{i}", "status": "releasing", "asset": "ETH",
             "amount_groth": 1_000, "relayer_fee_groth": 100}
            for i in range(600)
        ]
    )
    # ⛔ the double CANNOT fail the old way: mongomock ignores the `to_list` cap, so 600 rows
    # come back from a call that asked for 500. That is why the QUESTION is the assertion.
    assert len(await d.payout_requests.find({"status": "releasing"}).to_list(500)) == 600

    pipeline = payouts.inflight_pipeline(ETH)
    assert [next(iter(s)) for s in pipeline] == ["$match", "$group"]
    assert not any("$limit" in s or "$sample" in s for s in pipeline)

    seen: list[tuple[str, Any]] = []

    class Cursor:
        def __init__(self, inner: Any, kind: str) -> None:
            self.inner, self.kind = inner, kind

        def sort(self, *a: Any, **k: Any) -> Any:
            self.inner = self.inner.sort(*a, **k)
            return self

        def limit(self, n: int) -> Any:
            seen.append((f"{self.kind}.limit", n))
            self.inner = self.inner.limit(n)
            return self

        async def to_list(self, n: Any) -> Any:
            seen.append((f"{self.kind}.to_list", n))
            return await self.inner.to_list(n)

    class Coll:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def find(self, *a: Any, **k: Any) -> Any:
            return Cursor(self.inner.find(*a, **k), "find")

        def aggregate(self, *a: Any, **k: Any) -> Any:
            return Cursor(self.inner.aggregate(*a, **k), "aggregate")

        def __getattr__(self, n: str) -> Any:
            return getattr(self.inner, n)

    class Db:
        payout_requests = property(lambda self: Coll(d.payout_requests))

        def __getattr__(self, n: str) -> Any:
            return getattr(d, n)

        def __getitem__(self, n: str) -> Any:
            return Coll(d[n]) if n == "payout_requests" else d[n]

    monkeypatch.setattr(payouts, "db", Db)
    assert await payouts.inflight_groth(ETH) == 600 * 1_100
    assert await payouts.inflight_groth(ETH, exclude_rid="r0") == 599 * 1_100
    # ⛔ THE DATABASE SUMMED IT. No page of rows was ever read, so there is no window to be
    # past — which is the only form this guard can safely take.
    assert {k for k, _ in seen} == {"aggregate.to_list"}


async def test_a_held_release_stays_reserved_against_the_float(mock_db, eth, armed, beam_pay):
    """⛔ A HELD BURN IS STILL COMMITTED FLOAT. `_hold_for_a_human` moved an unresolved release
    out of `releasing`, so it stopped being reserved — but a held release is by definition one
    whose txid was never captured, so it was never registered either, the flow booked to
    `__house__`, and the max-privacy balance did NOT fall. The float was overstated in both
    terms at once, with nothing to correct it, and given the two resolver defects `held` was the
    NORMAL outcome of a lost response."""
    now = time.time()
    await make_payout(
        mock_db, rid="stuck", status="held", held_from="releasing",
        amount_groth=4 * GROTH, relayer_fee_groth=3600, unresolved_at=now,
    )
    assert await payouts.inflight_groth(ETH) == 4 * GROTH + 3600

    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await make_payout(mock_db, rid="fresh", amount=2 * GROTH)
    await payouts.process_once()
    row = await payout(mock_db, "fresh")
    assert row["status"] == "scheduled" and "already committed to crossings in flight" in row["hold_reason"]

    # only an operator's explicit resolution frees it
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "stuck"}, {"$set": {"float_resolved": True}}
    )
    assert await payouts.inflight_groth(ETH) == 0


async def test_the_hold_says_the_float_is_now_wrong_by_a_known_amount(mock_db, eth, armed):
    """…and the operator is told, on the row, what the measured float is now wrong by."""
    now = time.time()
    await make_payout(
        mock_db, status="releasing", relayer_fee_groth=3600,
        release_call_at=now - payouts.UNRESOLVED_S - 60,
        release_attempt_at=now - payouts.UNRESOLVED_S - 60,
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "held"
    assert "stays RESERVED against the shielded float" in row["hold_reason"]
    assert "float_resolved" in row["hold_reason"]


# ============================================ §I · a delivery is identified, never assumed


async def test_a_batched_delivery_settles_every_payout_it_pays(mock_db, eth, armed):
    """The relayer batches, and the product's documented shape is a list of items to one W (up
    to 50 per withdrawal). `got == amount_wei` matched neither request when W rose by the sum:
    `find_delivery` answered None, the scan checkpointed PAST the block, and both crossings sat
    in `delivering` until the 18-hour SLA with the money actually delivered."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base = eth.head
    for rid, amount in (("a", 500_000), ("b", 700_000)):
        await make_payout(
            mock_db, rid=rid, amount=amount, status="delivering", beam_txid=f"t-{rid}",
            eth_from_block=base, eth_scan_from=base,
        )
    block = base + 350
    eth.head = base + 1000
    eth.credit(block, ETH.pipe, -1_200_000 * ETH.grid)
    eth.credit(block, W, 1_200_000 * ETH.grid)  # ONE block, BOTH deliveries
    await payouts.process_once()
    assert (await payout(mock_db, "a"))["status"] == "sent"
    await payouts.process_once()
    assert (await payout(mock_db, "b"))["status"] == "sent"
    keys = [r["_id"] async for r in mock_db["pgasme_test"].deliveries.find({})]
    assert len(keys) == 2  # one consumed delivery each, never one closing both


async def test_a_batch_of_two_identical_payouts_consumes_two_deliveries_not_one(
    mock_db, eth, armed
):
    """Two payouts of one denomination to one wallet are the product's normal shape, so the
    batch that pays both is two deliveries — and the (pipe, block, W, amount) key must admit
    exactly as many as the block's rise has been PROVEN to contain, never more."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base = eth.head
    for rid in ("a", "b"):
        await make_payout(
            mock_db, rid=rid, amount=500_000, status="delivering", beam_txid=f"t-{rid}",
            eth_from_block=base, eth_scan_from=base,
        )
    block = base + 350
    eth.head = base + 1000
    eth.credit(block, ETH.pipe, -1_000_000 * ETH.grid)
    eth.credit(block, W, 1_000_000 * ETH.grid)
    await payouts.process_once()
    await payouts.process_once()
    assert (await payout(mock_db, "a"))["status"] == "sent"
    assert (await payout(mock_db, "b"))["status"] == "sent"
    assert await mock_db["pgasme_test"].deliveries.count_documents({}) == 2


async def test_an_unattributable_block_is_held_and_never_scanned_past(mock_db, eth, armed, paged):
    """⛔ "WE CANNOT ATTRIBUTE" IS NOT "NOT OURS". When the rise cannot be explained the scan
    must not move past it — `eth_scan_from` is one-way, and past the delivery block the balance
    change that proves the crossing can never be seen again."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    base = eth.head
    await make_payout(
        mock_db, status="delivering", beam_txid="t1", eth_from_block=base, eth_scan_from=base
    )
    block = base + 350
    eth.head = base + 1000
    eth.credit(block, ETH.pipe, -900_000 * ETH.grid)
    eth.credit(block, W, 900_000 * ETH.grid)  # more than this payout's 500_000, unexplained
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "delivering"
    assert "cannot be attributed" in row["hold_reason"]
    assert row["eth_scan_from"] == block - 1  # ⛔ NOT past it

    # the sibling that explains the rest turns up, and both settle from the same block
    await make_payout(
        mock_db, rid="req2", amount=400_000, status="delivering", beam_txid="t2",
        eth_from_block=base, eth_scan_from=base,
    )
    await payouts.process_once()
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "sent"
    assert (await payout(mock_db, "req2"))["status"] == "sent"


# ============================================ §J · the floor is exclusive


async def test_the_previous_payouts_message_is_below_the_floor_and_never_proves_this_one(
    beam_wallet,
):
    """`msg_floor` is the highest id that existed the instant BEFORE our send, so that message
    is by construction not ours — and the walk `range(top + 1, floor - 1, -1)` included it. Two
    payouts to one W for one amount are the normal shape, so the included message was exactly
    the twin most likely to match on (receiver, amount): it would unblock the irreversible
    ledger release and validate a lost-response adoption on somebody else's kernel."""
    beam_wallet.local_msgs = {5: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    assert await beam_wallet.find_local_msg(ETH.beam_cid, W, 500_000, from_msg_id=5) is None
    assert await beam_wallet.find_local_msg(ETH.beam_cid, W, 500_000, from_msg_id=4) == 5


async def test_two_payouts_cannot_carry_one_pipe_message(mock_db):
    """…and the database refuses it too: `_our_msg` is one of the two discriminators the whole
    identity design rests on, and nothing stopped two rows claiming message N."""
    from pymongo.errors import DuplicateKeyError

    await payouts.ensure_indexes()
    d = mock_db["pgasme_test"]
    await d.payout_requests.insert_one({"_id": "a", "asset": "ETH", "msg_id": 7})
    await d.payout_requests.insert_one({"_id": "b", "asset": "ETH"})
    with pytest.raises(DuplicateKeyError):
        await d.payout_requests.update_one({"_id": "b"}, {"$set": {"msg_id": 7}})
    await d.payout_requests.insert_one({"_id": "c", "asset": "DAI", "msg_id": 7})  # another pipe


async def test_a_second_row_claiming_one_message_holds_instead_of_crashing(
    mock_db, eth, armed, beam_pay
):
    """The index refuses the write; the handler must turn that into a refusal with a reason, not
    an exception on the money path."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.ensure_indexes()
    await make_payout(mock_db, rid="twin", status="delivering", asset="ETH", msg_id=1,
                      beam_txid="other", eth_from_block=eth.head, eth_scan_from=eth.head)
    await make_payout(mock_db)
    await payouts.process_once()  # → releasing, the send makes message 1
    await payouts.process_once()  # → bridging
    await payouts.process_once()  # the kernel: message 1 is already somebody's evidence
    row = await payout(mock_db)
    assert row["status"] == "bridging" and "already booked to another payout" in row["hold_reason"]
    assert await ledger.find_entry("release", "req1") is None


# ============================================ §K · an unreadable query is not evidence


async def test_a_routing_404_is_not_beampay_saying_it_has_not_seen_it_yet(beam_pay):
    """⛔ FastAPI answers a request for a route it does not have with 404 `{"detail": "Not
    Found"}`, and BeamPay master `e09bfc2` — the commit this deployment was copied from — has
    NO `/internal/*` routes at all. Mapping every 404 to None made a version-skewed BeamPay
    indistinguishable from "the processor has not caught up": every payout would wait in
    `bridging` and every claim in `claiming`, with no error, no health degradation and no alert
    until the SLA."""
    beam_pay.add_tx(txId="tx1", kernel="k")
    assert (await beam_pay.contract_tx("tx1")) is not None
    assert (await beam_pay.contract_tx("never-existed")) is None  # tx_not_found: a real answer

    beam_pay.missing_routes.add("/internal/")
    with pytest.raises(beampay.BeamPayError):
        await beam_pay.contract_tx("tx1")
    with pytest.raises(beampay.BeamPayError):
        await beam_pay.expectation_route_ready()


async def test_an_unknown_address_is_an_answer_and_a_missing_route_is_not(beam_pay):
    """`is_registered` is the `is_mine` substitute the shield target proof rests on: "not in the
    book" is a genuine 404 `Address not found`; a routing 404 is not."""
    assert await beam_pay.is_registered(TREASURY) is True
    assert await beam_pay.is_registered("not" + "0" * 61) is False

    beam_pay.missing_routes.add("/balances")
    with pytest.raises(beampay.BeamPayError):
        await beam_pay.is_registered(TREASURY)


async def test_a_bridging_payout_is_never_silently_stalled_by_a_missing_route(
    mock_db, eth, armed, beam_pay, paged
):
    """The money consequence: `bridging` polls `/internal/contract_tx/{txid}`, and "None" there
    is the normal wait. A BeamPay without the route made every payout wait for ever in silence."""
    await make_payout(mock_db, status="bridging", beam_txid="beamtx-1")
    beam_pay.missing_routes.add("/internal/")
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "bridging"
    assert any("payout step failed" in t for t in paged)  # LOUD, not silent
