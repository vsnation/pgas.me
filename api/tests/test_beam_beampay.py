"""BeamPay is the ONLY interface to the Beam wallet (operating law 10) — the tests for that.

Four things this file is here to keep true, each of which is a way money or accounting is lost:

  §1 the wallet-api is called for TWO things and nothing else, and `/internal/*` is reached
     with the SCOPED key or not at all
  §2 every contract txid is registered with BeamPay in the same processor step, BEFORE the row
     advances — an unregistered contract flow books to `__house__`, so the treasury balance
     never moves and the float every payout gates on is counting value that is already burned
  §3 `booked` is the daemon's idempotency flag, not "succeeded": it is set for a cancelled tx
     too, so settlement is `booked AND status == 3`
  §4 no fee is a constant we set: the invocation fee is the wallet's, the withdrawal fee is
     BeamPay's, and both are READ BACK once settled

The fakes are the project's own (`test_beam_payout`): `FakeBeamPay` subclasses the real client
and replaces only `_http`, so every key choice, every status check and every parse in
`pgasme/beampay.py` runs for real — including the 403 an unscoped key gets on `/internal/*`.
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

from pgasme import beam, beampay, payouts, tg, workers
from pgasme.config import settings

# The only wallet-api methods this project is allowed to make. Two of them SIGN or BUILD; the
# third (2026-09-10) is a READ that moves nothing and that BeamPay has no route for — the COUNT
# of spendable coins, which decides how many crossings the wallet can carry at once and which a
# per-address ledger balance cannot express. Two releases 0.7 s apart both died "Not enough
# inputs" for want of it. Anything else against :10001 is still a defect (law 10).
ALLOWED_WALLET_METHODS = {"invoke_contract", "process_invoke_data", "get_utxo"}


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    bp = FakeBeamPay()
    beampay.reset_health()  # observed health is per-process state
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)
    bp.fund(MP, 36, 5 * GROTH)
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    # the working-float policy (PGAS_SHIELD_KEEP_GROTH) has its own file
    # (test_beam_payout_spendable); this one tests the shield MECHANICS, so the
    # policy is pinned out of the way rather than silently deciding these cases
    monkeypatch.setattr(settings, "shield_keep_groth", 0)
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


# ================================================== §1 · one interface, two escape hatches


async def test_the_wallet_api_is_reached_for_exactly_two_methods(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ THE WHOLE LAW, as one assertion. A full deposit AND a full payout run end to end, and
    the wallet-api sees `invoke_contract` (create_tx:false), `process_invoke_data` and the
    read-only `get_utxo` — nothing else. Every balance, status, address and withdrawal came
    from BeamPay.

    The fake helps: it implements no other method, so a reintroduced `tx_status`, `tx_list`,
    `wallet_status`, `generate_tx_id`, `create_address`, `validate_address` or `tx_send` cannot
    silently pass — it raises."""
    await make_payout(mock_db)
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    beam_pay.fund(TREASURY, 36, 12_000_000)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    # the payout waits while the treasury still holds claimed-but-unshielded bETH (§9.3 / spec
    # S2) — and that number is the TREASURY ADDRESS's BeamPay balance, which is exactly what
    # falls as the shield chunks land. So the two machines interlock through BeamPay alone.
    for _ in range(12):
        await payouts.process_once()
    assert set(beam_wallet.methods()) <= ALLOWED_WALLET_METHODS
    assert all(p.get("create_tx") is False for p in beam_wallet.params_for("invoke_contract"))
    assert (await deposit(mock_db))["treasury"] == "shielded"
    assert (await payout(mock_db))["status"] in ("bridging", "delivering", "sent")
    # …and every contract txid either machine made is registered with BeamPay. Looked up BY THE
    # ROW rather than by ordinal: which invocation happens first is a scheduling detail, and a
    # test that pins it tests the scheduler instead of the law.
    dep_row, req_row = await deposit(mock_db), await payout(mock_db)
    assert set(beam_pay.expectations) == {dep_row["claim_txid"], req_row["beam_txid"]}
    assert beam_pay.expectations[dep_row["claim_txid"]]["address"] == TREASURY  # claim: inflow
    # …and the release books to the address that FUNDED it — since T40 a regular-funded crossing
    # is sent from a FRESH Beam address created for this order alone (admin 2026-09-10: "when
    # you specify sendFund from — it should be new SBBS address"), funded by an internal
    # transfer out of the treasury. Booking it to MP would drive MP negative by the whole
    # crossing and leave the treasury untouched, which is a float that counts value already gone.
    assert req_row["source"] == "regular"
    assert req_row["crossing_address"] is True
    assert req_row["source_address"] not in (TREASURY, MP)
    assert beam_pay.expectations[req_row["beam_txid"]]["address"] == req_row["source_address"]


async def test_invoke_contract_cannot_be_asked_to_create_a_transaction(beam_wallet):
    """`invoke()` has no `create_tx` parameter at all — the flag is the difference between a
    read and a signature, and a value a caller can pass is a value a caller can get wrong."""
    with pytest.raises(TypeError):
        await beam_wallet.invoke("role=user,action=get_pk,cid=" + ETH.beam_cid, create_tx=True)
    await beam_wallet.get_pk(ETH.beam_cid)
    assert beam_wallet.params_for("invoke_contract")[-1]["create_tx"] is False


async def test_internal_routes_need_the_scoped_key_and_reads_need_the_ordinary_one(beam_pay):
    """A scopeless key is refused on `/internal/*` with 403 by design (auth.require_scope), so
    a client that reaches for the wrong one is broken in production and must be broken here."""
    assert await beam_pay.available_groth(TREASURY, 0) == 10 * GROTH
    assert (await beam_pay.expectation_route_ready())["available"] is True

    beam_pay.internal_key = beam_pay.KEY  # the ordinary key, on the scoped route
    with pytest.raises(beampay.BeamPayError) as e:
        await beam_pay.expectation_route_ready()
    assert e.value.status == 403

    beam_pay.internal_key = ""
    with pytest.raises(beampay.BeamPayError) as e:
        await beam_pay.contract_tx("anything")
    assert "PGAS_BEAMPAY_INTERNAL_KEY" in str(e.value)  # fails closed, before the wire


async def test_a_balance_with_no_configured_address_refuses_rather_than_answering_zero(
    monkeypatch,
):
    monkeypatch.setattr(settings, "beam_treasury_address", "")
    with pytest.raises(beampay.BeamPayError) as e:
        beampay.treasury_address()
    assert "0 is not an answer" in str(e.value)


async def test_an_unreadable_beampay_is_never_a_value(beam_pay):
    beam_pay.raise_on.add("/balances")
    with pytest.raises(beampay.BeamPayError):
        await beam_pay.available_groth(TREASURY, 0)
    beam_pay.raise_on.clear()
    # 404 is a genuine answer — "not in the address book" — and only there
    assert await beam_pay.is_registered("never-created-anywhere") is False
    assert await beam_pay.is_registered(TREASURY) is True


async def test_an_exhausted_page_budget_is_an_incomplete_read_not_an_empty_one(beam_pay):
    """⛔ "We ran out of pages" must never read as "there is no such transaction": the scan is
    bounded by a TIMESTAMP, and a budget that runs out before reaching it raises."""
    now = time.time()
    for i in range(beampay.PAGE * beampay.MAX_PAGES + 10):
        beam_pay.add_tx(txId=f"noise-{i}", create_time=int(now))
    with pytest.raises(beampay.BeamPayError) as e:
        await beam_pay.find_contract_tx(ETH.beam_cid, 36, now - 86_400, -1)
    assert "INCOMPLETE read" in str(e.value)


# ================================================== §2 · registered before the row advances


async def test_a_payout_registers_its_txid_with_beampay_before_it_advances(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """The float that funds the crossing is the max-privacy address, so that is where BeamPay
    must book the burn — not `__house__`, whose balance nothing in this system reads."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing" and row["beam_txid"] == "beamtx-1"
    assert beam_pay.expectations["beamtx-1"] == {
        "txid": "beamtx-1",
        "address": MP,
        "trade_ref": "req1",
    }
    assert row["attribution"]["address"] == MP and row["attribution"]["at"] > 0
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "bridging"


async def test_a_failed_registration_holds_the_row_and_never_re_sends(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ THE KERNEL EXISTS. A registration that did not land is retried as a REGISTRATION —
    the send is never repeated, because a second `process_invoke_data` is a second signature
    over one inventory. And the row does not advance while its flow is unattributed."""
    # only the POST fails: the side-effect-free GET probe still answers, which is the state
    # this test is about — the route is there, the registration itself did not land
    beam_pay.raise_on.add("POST /internal/expect_contract_tx")
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["beam_txid"] == "beamtx-1"  # the evidence is kept whatever happens next
    assert "attribution" not in row and row["attribution_error"]

    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing"  # ⛔ NOT advanced
    assert "has not accepted its attribution" in row["hold_detail"]
    assert beam_wallet.methods().count("process_invoke_data") == 1

    beam_pay.raise_on.clear()
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging" and row["attribution"]["txid"] == "beamtx-1"
    assert beam_wallet.methods().count("process_invoke_data") == 1


async def test_a_registration_refused_as_already_booked_pages_and_does_not_strand_the_payout(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """`tx_already_booked` is a conflict no retry can ever clear. Holding a crossing that has
    already happened to fix a bookkeeping problem in another system strands the user's money,
    so it is recorded, paged IMMEDIATELY as a repair the operator owes, and let through."""
    beam_pay.expect_refusal = "tx_already_booked"
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging"
    assert row["attribution"]["refused"] == "tx_already_booked"
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "payout_attribution"})
    assert ev is not None and ev["immediate"] is True
    assert "operator owes an attribution repair" in ev["text"]


async def test_a_claim_registers_to_the_treasury_before_it_is_called_claimed(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """A claim is an INFLOW, and the value lands where the txid says it lands. Unregistered, it
    credits `__house__` — and the treasury balance the shield and every payout gate read would
    never have moved at all."""
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await payouts.process_once()  # → claiming
    beam_pay.raise_on.add("POST /internal/expect_contract_tx")  # the probe answers; the POST does not
    await payouts.process_once()  # claim submitted; the registration fails
    row = await deposit(mock_db)
    assert row["claim_txid"] == "beamtx-1" and row["attribution_error"]

    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claiming"  # ⛔ NOT advanced to `claimed`
    assert "has not accepted its attribution" in row["hold_reason"]

    beam_pay.raise_on.clear()
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claimed"
    assert beam_pay.expectations["beamtx-1"]["address"] == TREASURY
    assert beam_wallet.methods().count("process_invoke_data") == 1


# ================================================== §3 · booked is not succeeded


async def test_a_cancelled_contract_tx_is_booked_but_is_not_a_claim(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ `handle_contract_transaction` sets `success` for a CANCELLED or FAILED tx too
    ("terminal and never mined → stop retrying it"). Reading `booked` alone would march a
    reverted claim into shielding with the bETH still sitting unclaimed on the pipe."""
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await payouts.process_once()
    await payouts.process_once()
    assert (await deposit(mock_db))["claim_txid"] == "beamtx-1"
    beam_pay.tx("beamtx-1").update(
        {"status": beam.TX_CANCELLED, "status_string": "cancelled", "success": True}
    )
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claiming" and "claim_txid" not in row
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "deposit_claim_failed"})
    assert ev is not None


async def test_a_settled_but_unbooked_release_waits_for_beampay(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """§BOOKED-IS-LANDED: the ledger release is irreversible, so it does not run ahead of the
    system of record. Poll `booked`, never race it."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    # message 7 is the PREVIOUS payout's — it existed before this release, so it is the floor
    # and never this crossing's evidence; the wallet creates ours at 8 when the send is made
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    await payouts.process_once()
    await payouts.process_once()  # → bridging
    beam_pay.tx("beamtx-1")["success"] = False  # settled on chain, not yet booked
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging" and "kernel_at" not in row

    beam_pay.tx("beamtx-1")["success"] = True
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["kernel_at"] > 0 and row["msg_id"] == 8


async def test_a_transaction_beampay_has_not_seen_yet_is_a_wait_not_a_failure(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """404 `tx_not_found` is the normal state for the seconds between the wallet answering a
    txid and BeamPay's processor sweeping it up. It is an answer; it is not an outage."""
    await make_payout(mock_db, status="bridging", beam_txid="not-seen-yet")
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging" and "hold_reason" not in row


# ================================================== §4 · every fee is read back


async def test_the_invocation_fee_is_the_wallets_and_is_read_back_from_beampay(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """§WE-SET-IT-WE-DONT-READ-IT. No `fee` is sent on `invoke_contract` — exactly as
    `rebal5_beth_to_eth.py` and `bridge_watcher.py` build theirs — and the number the wallet
    actually charged is read back from the settled transaction, never assumed."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    beam_wallet.invoke_fee = 1_234_567
    await payouts.process_once()
    assert all("fee" not in p for p in beam_wallet.params_for("invoke_contract"))
    await payouts.process_once()
    await payouts.process_once()
    row = await payout(mock_db)
    # ⛔ `crossing_fee_groth`, and NOT `beam_fee_groth` (T40b F7): a payout row carries two BEAM
    # fees of different kinds — BeamPay's on the transfer that funds the crossing address, and
    # the wallet's on the invocation — and each has one writer and one field.
    assert row["crossing_fee_groth"] == 1_234_567
    assert "beam_fee_groth" not in row


async def test_an_absurd_fee_is_paged_the_moment_it_is_read_back(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """A fee nobody read back is a fee nobody checked — and a single Beam transaction fee above
    the 5-BEAM scale is not a cost, it is a fault."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    beam_wallet.invoke_fee = settings.beam_fee_alert_groth + 1
    for _ in range(3):
        await payouts.process_once()
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "beam_fee_excessive"})
    assert ev is not None and ev["immediate"] is True
    assert "read back" in ev["text"]


async def test_the_beam_fee_budget_is_the_treasurys_beampay_balance(
    mock_db, eth, armed, beam_wallet, beam_pay, paged
):
    """The BEAM every claim, shield and pipe send is paid for is the TREASURY ADDRESS's
    BeamPay balance of asset 0 — never a raw wallet balance, which is one shared UTXO pool and
    is never our inventory (law 1). It is also the exact number BeamPay itself enforces on a
    `/withdraw`."""
    beam_pay.addresses[TREASURY]["available"]["0"] = 1_000_000  # 0.01 BEAM: less than a claim
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await payouts.process_once()
    await payouts.process_once()
    row = await deposit(mock_db)
    # …and what "a claim" costs is DERIVED (payouts.fee_budget), not the 0.02 constant the first
    # live claim disproved by paying 0.121 BEAM. With no settled claim to learn from, the floor.
    assert row["hold_reason"] == (
        "the wallet holds 0.01 BEAM and this call reserves 0.15 BEAM for a claim "
        "(no settled claim to learn from yet — the 0.15 floor)"
    )
    assert "process_invoke_data" not in beam_wallet.methods()
    assert any(t.startswith("LOW BEAM") for t in paged)


async def test_no_fee_field_is_ever_sent_to_beampay_withdraw(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """BeamPay overrides and ignores the request's `fee` (api.py:322), so sending one is a
    number we would then be tempted to trust. The client does not have the field."""
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    beam_pay.fund(TREASURY, 36, 1_000_000)
    for _ in range(5):
        await payouts.process_once()
    assert beam_pay.withdrawals and all("fee" not in w for w in beam_pay.withdrawals)
    body: dict[str, Any] = beam_pay.bodies_for("/withdraw")[0]
    assert set(body) == {"from_address", "to_address", "asset_id", "amount", "comment"}


async def test_two_transactions_with_one_shield_comment_are_paged_as_a_double_send(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ `/withdraw` has no idempotency key at all, so a double send is the accident this route
    makes possible — and a `{comment: row}` map would have hidden it by keeping one and
    discarding the other. It is the one thing that must never be silent."""
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    beam_pay.fund(TREASURY, 36, 2_000_000)
    for _ in range(5):
        await payouts.process_once()
    assert len(beam_pay.withdrawals) == 1
    # an operator (or a bug in another process) sent the same chunk a second time
    beam_pay.add_tx(
        txId="wd-twin",
        type=4,
        type_string="simple",
        asset_id="36",
        value="1000000",
        fee="1100000",
        sender=TREASURY,
        receiver=MP,
        comment="shield|dep1|0",
    )
    await payouts.process_once()
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "deposit_shield_duplicate"})
    assert ev is not None and ev["immediate"] is True
    assert "DOUBLE SEND" in ev["text"] and "wd-twin" in ev["text"]
    assert (await deposit(mock_db))["treasury"] == "shielded"  # the value is gone; stalling helps nobody


async def test_beampay_being_unreachable_is_reported_once_not_per_order(
    mock_db, eth, armed, beam_pay, paged, monkeypatch
):
    """BeamPay is the SYSTEM OF RECORD: while it is unreachable nothing can read a balance or
    register a txid. Its health is what the processors themselves observed on their own calls —
    never a separate probe, which could succeed while the money path was failing (§8)."""
    beam_pay.raise_on.add("/balances")
    await make_payout(mock_db)
    await payouts.process_once()
    assert beampay.health["last_fail_at"] > beampay.health["last_ok_at"]

    monkeypatch.setattr(workers, "_started_at", time.time() - 3600)
    beampay.health["last_fail_at"] = time.time()
    await workers.upstream_checks()
    assert any(t.startswith("DOWN: BeamPay unreachable") for t in paged)


async def test_a_refusal_is_beampay_answering_and_never_reads_as_an_outage(beam_pay):
    """A 403 or a 404 is BeamPay answering us precisely. Calling that "down" would page about a
    configuration fault as though the service had fallen over."""
    beampay.reset_health()
    assert await beam_pay.is_registered("no-such-address") is False  # a 404
    assert beampay.health["last_ok_at"] > 0 and beampay.health["last_fail_at"] == 0.0
    beam_pay.internal_key = beam_pay.KEY
    with pytest.raises(beampay.BeamPayError):
        await beam_pay.expectation_route_ready()  # a 403
    assert beampay.health["last_fail_at"] == 0.0


async def test_a_missing_confirmation_count_waits_instead_of_inventing_one(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ BeamPay's `/internal/contract_tx/{txid}` reports `confirmations` but NEVER the block a
    contract tx settled in (api.py:989-1027), so maturity is counted from the WALLET HEIGHT we
    recorded at kernel time — and when that height is unreadable too there is nothing honest
    left to do but wait. An unreadable count is not a count."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()  # → bridging
    beam_pay.tx("beamtx-1")["confirmations"] = None
    beam_pay.height_ = 0  # the wallet answers no height either
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging"  # NOT delivering
    assert "refusing to guess" in row["hold_detail"]

    beam_pay.tx("beamtx-1")["confirmations"] = settings.beam_confirmations
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "delivering"
