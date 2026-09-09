"""The three operational laws the first live claim (2026-09-09) paid for, plus the re-verify.

  §A **A restart is not a second processor.** The payout lease is released on the way out and,
     when it is not (a crash), the refusal it causes is SILENT until it has outlived the TTL —
     because only a lease somebody is renewing can still be held after that, and only that is
     a second live owner. Paging about every ordinary deploy is how an operator is trained to
     ignore the pager (law 15).

  §B **A fee is data, not a constant** (§WE-SET-IT-WE-DONT-READ-IT). `beam_claim_fee_groth`
     said 0.02 BEAM. The first live claim — pipe message 137, txid `e3fceec7…`, 21:40:19Z —
     paid **12,100,000 groth, 0.121 BEAM**: six times the number, on transaction one, on a
     number nobody had ever measured. The budget is now derived from what the wallet really
     charged, with a floor and a margin, and it is a RESERVATION — no call carries a `fee`.

  §C **The fee leg of an unregistered contract tx books to `__house__` too.** The claim above
     was submitted before its txid was registered, so BeamPay booked the whole flow to the
     house: the bETH credit was repaired the same night, the 0.121 BEAM debit was not.
     `repair-fee` is the one-off that posts BeamPay's documented, zero-sum ledger adjustment.

  §D The 20:40Z deposit is credited and `verified: false` because the router had not indexed its hash
     when it was registered. `_reverify` asks again — and the window it asks within lives in
     `workers.py`.

The fakes are the project's own (`test_beam_payout`), so every key choice, status check and
parse in `pgasme/beampay.py` runs for real.
"""

from __future__ import annotations

import json
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

from pgasme import beam, beampay, ledger, payouts, tg, workers, xchain
from pgasme.config import settings

# The live facts, written down as numbers so a change to either has to change this file.
LIVE_CLAIM_TXID = "e3fceec730af436d98c75013f2b090ed"
LIVE_CLAIM_FEE_GROTH = 12_100_000  # 0.121 BEAM — what claim e3fceec7… actually cost
LIVE_CLAIM_VALUE_GROTH = 2_652_864  # the bETH it claimed (asset 36), booked to __house__
OLD_CONSTANT_GROTH = 2_000_000  # settings.beam_claim_fee_groth, superseded
CLAIM_FLOOR_GROTH = 15_000_000  # PGAS_BEAM_BEAM_CLAIM_FEE_FLOOR_GROTH's default, 0.15 BEAM
HOUSE = "__house__"


class LedgerBeamPay(FakeBeamPay):
    """FakeBeamPay plus the ONE route the repair needs: `POST /internal/ledger/adjust`, with
    BeamPay's own gates (api.py:633-760) — the scoped key, the zero-sum shape, the mandatory
    booked `after_tx`, the direction fixed by the gate tx's signed flow, and the bound."""

    def __init__(self) -> None:
        super().__init__()
        self.adjustments: list[dict[str, Any]] = []

    def _route(self, method, path, params, body):
        if path == "/internal/ledger/adjust" and method == "POST":
            b = dict(body or {})
            gate = self.tx_index.get(str(b.get("after_tx") or ""))
            if not gate or gate.get("status") != beam.TX_COMPLETED:
                return 409, {"detail": "tx_not_booked"}
            if HOUSE not in (b["from_address"], b["to_address"]):
                return 400, {"detail": "house_leg_required"}
            flow = beam.house_flow(
                {"invoke_data": gate.get("invoke_data"), "fee": gate.get("fee")},
                int(b["asset_id"]),
            )
            if flow > 0 and b["to_address"] != HOUSE:
                return 409, {"detail": "wrong_direction"}
            if flow < 0 and b["from_address"] != HOUSE:
                return 409, {"detail": "wrong_direction"}
            if int(b["amount_groth"]) > abs(flow):
                return 409, {"detail": "amount_exceeds_tx_flow"}
            self.adjustments.append(b)
            return 200, {"status": True, "adjust_id": b["adjust_id"], "replayed": False}
        return super()._route(method, path, params, body)


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> LedgerBeamPay:
    bp = LedgerBeamPay()
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
def beam_wallet(monkeypatch: pytest.MonkeyPatch, beam_pay: LedgerBeamPay) -> FakeWalletApi:
    w = FakeWalletApi(beam_pay)
    beam.set_wallet(w)
    payouts.reset_archive_pin()
    monkeypatch.setattr(settings, "beam_shader", w.shader)
    yield w
    beam.set_wallet(None)


@pytest.fixture(autouse=True)
def clean_pass_state() -> None:
    """`conftest.mock_db` already clears it before every test; this clears it AFTER too, so a
    file that exercises the cache directly cannot leave one behind for a suite run in any order."""
    yield
    payouts.reset_process_state()


@pytest.fixture
def eth(monkeypatch: pytest.MonkeyPatch) -> FakeEth:
    fake = FakeEth()
    monkeypatch.setattr(workers, "get_rpc", lambda: fake)
    return fake


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "claim_enabled", True)


@pytest.fixture
def paged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []

    async def fake_send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        out.append(text)
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    return out


async def out_lines(fn, *a, **kw) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await fn(*a, out=lines.append, **kw)
    return code, lines


async def hold_a_lease(mock_db, owner: str = "other-process:beef", age_s: float = 0.0) -> None:
    await mock_db["pgasme_test"].leases.replace_one(
        {"_id": payouts.LEASE_ID},
        {"_id": payouts.LEASE_ID, "owner": owner, "at": time.time() - age_s},
        upsert=True,
    )


# ==================================================== §A · a restart is not a second processor


async def test_a_restart_within_the_ttl_is_refused_silently(mock_db, eth, paged):
    """The whole incident, as one assertion. A process killed mid-pass leaves its lease held
    for one TTL; its replacement is refused for that long and used to page "a second payout
    processor is running" on EVERY ordinary deploy. It is our own corpse, and the refusal is
    over before a human could act on it."""
    await hold_a_lease(mock_db, age_s=1.0)  # the dead process renewed a second ago
    res = await payouts.process_once()
    assert res == {"payouts": 0, "treasury": 0, "lease": 0, "lease_refused_s": 0}
    assert paged == []  # ⛔ no page, and the refusal still wrote a row: `lease: 0`
    res = await payouts.process_once()  # …and it stays silent for the whole window
    assert res["lease"] == 0 and paged == []


async def test_a_second_live_owner_renewing_pages_once(mock_db, eth, paged, monkeypatch):
    """The case that IS worth a page: a lease still held after longer than its own TTL can only
    be one somebody is renewing — a second live processor over one wallet. The clock is OURS
    (how long WE have been refused), because measuring the holder's `at` would be reset by every
    renewal the other process makes and would therefore page never."""
    monkeypatch.setattr(settings, "payout_lease_ttl_s", 120.0)
    await hold_a_lease(mock_db, age_s=1.0)
    await payouts.process_once()
    assert paged == []
    # …the other process is still renewing, and we have now been refused for five minutes
    payouts._LEASE["refused_since"] = time.time() - 300
    await hold_a_lease(mock_db, age_s=1.0)
    res = await payouts.process_once()
    assert res["lease"] == 0 and res["lease_refused_s"] >= 300
    assert len(paged) == 1
    assert "second payout processor" in paged[0] and "RENEWED" in paged[0]


async def test_the_refusal_clock_resets_the_moment_the_lease_is_won(mock_db, eth, paged):
    """A guard that does not reset is a guard that pages forever after one bad minute."""
    await hold_a_lease(mock_db, age_s=1.0)
    await payouts.process_once()
    assert payouts._LEASE["refused_since"] is not None
    await hold_a_lease(mock_db, age_s=10_000)  # the dead owner's lease finally expires
    res = await payouts.process_once()
    assert res["lease"] == 1
    assert payouts._LEASE["refused_since"] is None and paged == []


async def test_release_lease_hands_it_back_to_the_next_process(mock_db, eth, monkeypatch):
    """`workers.stop()` calls this: a graceful shutdown must not cost the next process a TTL."""
    assert await payouts.acquire_lease() is True
    assert await payouts.release_lease() is True
    row = await payouts.lease_holder()
    assert row["at"] == 0.0 and row["released_by"] == payouts.OWNER
    # `{"at": {"$lt": now - ttl}}` in acquire_lease already means "free" — one implementation
    monkeypatch.setattr(payouts, "OWNER", "the-next-process:0001")
    assert await payouts.acquire_lease() is True
    assert (await payouts.lease_holder())["owner"] == "the-next-process:0001"


async def test_release_lease_never_frees_somebody_elses(mock_db, eth):
    """⛔ A process whose TTL expired mid-pass has ALREADY lost the lease. Releasing on the way
    out would hand one wallet to two writers at the exact moment one of them is dying."""
    await hold_a_lease(mock_db, owner="the-live-one:cafe", age_s=1.0)
    assert await payouts.release_lease() is False
    row = await payouts.lease_holder()
    assert row["owner"] == "the-live-one:cafe" and row["at"] > 0 and "released_by" not in row


async def test_release_lease_never_raises_on_the_shutdown_path(mock_db, monkeypatch, caplog):
    """A shutdown helper that throws leaves the other tasks uncancelled. The TTL still expires
    the lease, so this failing is a delay, not a loss — but it is said out loud."""

    def broken():
        raise RuntimeError("mongo went away during shutdown")

    monkeypatch.setattr(payouts, "db", broken)
    assert await payouts.release_lease() is False


# ============================================================ §B · a fee is data, not a constant


async def seed_claims(mock_db, fees: list[int]) -> None:
    now = time.time()
    for i, fee in enumerate(fees):
        await mock_db["pgasme_test"].deposits.insert_one(
            {
                "_id": f"histdep{i}",
                "status": "credited",
                "treasury": "claimed",
                "claim_txid": f"claimtx-{i}",
                "claim_fee_groth": int(fee),
                "claim_call_at": now - 600 + i,
            }
        )


async def test_the_claim_budget_is_what_claims_really_cost_times_the_margin(mock_db, beam_pay):
    """⚠️ THE MONEY FACT. 12,100,000 groth on the first live claim against a 2,000,000 constant.
    The budget is `max(floor, 1.5 × the worst of the last 10)` — it tracks the wallet instead of
    asserting at it, and it cannot fall below a floor generous enough for an unmeasured market."""
    await seed_claims(mock_db, [3_000_000, LIVE_CLAIM_FEE_GROTH, 4_000_000])
    budget, why = await payouts.fee_budget("claim", beam_pay)
    assert budget == int(LIVE_CLAIM_FEE_GROTH * 1.5) == 18_150_000
    assert why["observed"] == 3 and why["max_observed_groth"] == LIVE_CLAIM_FEE_GROTH
    assert why["floor_groth"] == CLAIM_FLOOR_GROTH
    # ⛔ and this is what the constant was doing to the CYCLE-level guard: one BeamPay balance
    # read admits N calls, and the number it divides by was six times too small.
    have = 20_000_000
    assert have // OLD_CONSTANT_GROTH == 10  # ten claims admitted against 0.2 BEAM…
    assert have // budget == 1  # …which really buys one


async def test_a_budget_with_no_history_is_the_floor_never_zero(mock_db, beam_pay):
    budget, why = await payouts.fee_budget("claim", beam_pay)
    assert budget == CLAIM_FLOOR_GROTH and why["observed"] == 0
    assert payouts.fee_budget_line("claim", budget, why) == (
        "0.15 BEAM for a claim (no settled claim to learn from yet — the 0.15 floor)"
    )


async def test_a_recorded_fee_below_the_floor_never_lowers_the_budget(mock_db, beam_pay):
    """A cheap week is not evidence that the next call is cheap. The floor is the floor."""
    await seed_claims(mock_db, [1_000_000, 1_100_000])
    budget, why = await payouts.fee_budget("claim", beam_pay)
    assert budget == CLAIM_FLOOR_GROTH and why["max_observed_groth"] == 1_100_000


async def test_an_unrecorded_fee_is_read_back_from_beampay_and_the_row_is_not_written(
    mock_db, beam_pay
):
    """The fee is read from the place that CHARGES it (`GET /internal/contract_tx/{txid}.fee` —
    the identical number BeamPay debits from the registered address).

    ⛔ And the reader does NOT write the row: `fee_charged` is the single writer of every
    recorded fee, and two implementations of one fact disagree eventually (law 9)."""
    beam_pay.add_tx(txId=LIVE_CLAIM_TXID, fee=str(LIVE_CLAIM_FEE_GROTH))
    await mock_db["pgasme_test"].deposits.insert_one(
        {"_id": "d-live", "claim_txid": LIVE_CLAIM_TXID, "claim_call_at": time.time()}
    )
    assert await payouts.observed_fees("claim", beam_pay) == [LIVE_CLAIM_FEE_GROTH]
    row = await mock_db["pgasme_test"].deposits.find_one({"_id": "d-live"})
    assert "claim_fee_groth" not in row


async def test_an_unsettled_transaction_teaches_the_budget_nothing(mock_db, beam_pay):
    """`booked` is the daemon's idempotency flag, not success (it is set for a cancelled tx
    too). A fee that is not final is not a fee."""
    beam_pay.add_tx(
        txId="pending-1", fee="99000000", status=beam.TX_IN_PROGRESS, status_string="in progress"
    )
    await mock_db["pgasme_test"].deposits.insert_one(
        {"_id": "d-pending", "claim_txid": "pending-1", "claim_call_at": time.time()}
    )
    assert await payouts.observed_fees("claim", beam_pay) == []


async def test_an_unreadable_history_falls_back_to_the_floor_and_never_to_zero(
    mock_db, beam_pay
):
    """An unreadable query is not evidence of anything. A sample we could not read is DROPPED,
    and dropping samples can only make a max-based budget more conservative."""
    beam_pay.add_tx(txId="unreadable-1", fee=str(LIVE_CLAIM_FEE_GROTH))
    await mock_db["pgasme_test"].deposits.insert_one(
        {"_id": "d-dark", "claim_txid": "unreadable-1", "claim_call_at": time.time()}
    )
    beam_pay.raise_on = {"/internal/contract_tx/"}
    assert await payouts.observed_fees("claim", beam_pay) == []
    budget, why = await payouts.fee_budget("claim", beam_pay)
    assert budget == CLAIM_FLOOR_GROTH and why["observed"] == 0

    # …and a BeamPay build with no /internal route at all is the same answer, not a zero
    payouts.reset_process_state()
    beam_pay.raise_on = set()
    beam_pay.missing_routes = {"/internal/"}
    assert (await payouts.fee_budget("claim", beam_pay))[0] == CLAIM_FLOOR_GROTH


async def test_the_shield_budget_is_per_chunk_and_floors_at_beampays_own_fee(mock_db, beam_pay):
    """A shield is a `/withdraw`, whose fee BEAMPAY sets (0.011 BEAM to an offline / max-privacy
    address) and whose request `fee` field it ignores outright. So the floor is that number with
    the margin on it, and the history is per CHUNK — the sum across chunks is what one deposit's
    shielding cost, never what the next chunk will."""
    assert (await payouts.fee_budget("shield", beam_pay))[0] == 1_650_000
    payouts.reset_process_state()
    await mock_db["pgasme_test"].deposits.insert_one(
        {"_id": "d-sh", "shield_fee_groth": 2_000_000, "shielded_at": time.time()}
    )
    assert (await payouts.fee_budget("shield", beam_pay))[0] == 3_000_000


async def test_the_floor_is_an_environment_knob_and_a_bad_one_never_becomes_zero(monkeypatch):
    monkeypatch.setenv("PGAS_BEAM_CLAIM_FEE_FLOOR_GROTH", "25000000")
    assert payouts.fee_floor("claim") == 25_000_000
    monkeypatch.setenv("PGAS_BEAM_CLAIM_FEE_FLOOR_GROTH", "0.25 BEAM")
    assert payouts.fee_floor("claim") == CLAIM_FLOOR_GROTH  # refused, and the default stands
    monkeypatch.delenv("PGAS_BEAM_CLAIM_FEE_FLOOR_GROTH")
    assert payouts.fee_floor("send") == CLAIM_FLOOR_GROTH
    assert payouts.fee_floor("shield") == 1_650_000


async def test_the_gate_refuses_a_treasury_that_only_covered_the_old_constant(
    mock_db, eth, beam_pay, beam_wallet, monkeypatch, paged
):
    """End to end: 0.125 BEAM in the treasury is six claims by the old constant and not one by
    what a claim really costs. The claim is HELD, with the derivation on the row, and nothing is
    signed — instead of being admitted and failing somewhere inside the wallet."""
    monkeypatch.setattr(settings, "claim_enabled", True)
    await seed_claims(mock_db, [LIVE_CLAIM_FEE_GROTH])
    beam_pay.addresses[TREASURY]["available"]["0"] = 12_500_000
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await payouts.process_once()
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claiming" and "process_invoke_data" not in beam_wallet.methods()
    assert row["hold_reason"] == (
        "the wallet holds 0.125 BEAM and this call reserves 0.1815 BEAM for a claim "
        "(1 settled claim(s) read back, worst 0.121 × 1.5, floor 0.15)"
    )
    assert 12_500_000 // OLD_CONSTANT_GROTH == 6  # what the constant would have admitted


async def test_the_claim_records_its_own_fee_where_the_shield_cannot_overwrite_it(
    mock_db, eth, beam_pay, beam_wallet, monkeypatch
):
    """⛔ TWO WRITERS OF ONE FIELD. `deposits.beam_fee_groth` was written by the claim and then
    overwritten by the shield, so the claim's own fee — the sample the next claim's budget is
    derived from — was gone by the time anything could read it."""
    monkeypatch.setattr(settings, "claim_enabled", True)
    monkeypatch.setattr(settings, "shield_enabled", True)
    beam_wallet.invoke_fee = LIVE_CLAIM_FEE_GROTH
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    beam_pay.fund(TREASURY, 36, 1_000_000)
    for _ in range(6):
        await payouts.process_once()
        payouts.reset_process_state()
    row = await deposit(mock_db)
    assert row["treasury"] == "shielded"
    assert row["claim_fee_groth"] == LIVE_CLAIM_FEE_GROTH  # the claim's, still readable
    assert row["shield_fee_groth"] == beam_pay.withdraw_fee  # per CHUNK, not the sum
    assert await payouts.observed_fees("claim", beam_pay) == [LIVE_CLAIM_FEE_GROTH]


# ================================================== §C · the fee leg of an unregistered claim


def live_claim(bp: LedgerBeamPay, **over: Any) -> dict[str, Any]:
    """Claim `e3fceec7…` as BeamPay booked it: a NEGATIVE asset-36 amount (a receive) and a
    12,100,000-groth BEAM fee, with no expectation on it — so the whole flow went to __house__."""
    return bp.add_tx(
        txId=LIVE_CLAIM_TXID,
        fee=str(LIVE_CLAIM_FEE_GROTH),
        kernel="06ebea61",
        invoke_data=[{"amounts": [{"asset_id": 36, "amount": -LIVE_CLAIM_VALUE_GROTH}]}],
        **over,
    )


def body_of(lines: list[str]) -> dict[str, Any]:
    return next(json.loads(ln.strip()) for ln in lines if ln.strip().startswith("{"))


async def test_the_fee_leg_repair_is_a_dry_run_that_sends_nothing(mock_db, beam_pay):
    """The default is a dry run: the exact body, and every gate the route will apply, printed.

    ⛔ The DIRECTION is not this tool's to choose. BeamPay derives it from the gate tx's own
    signed flow — a POSITIVE asset-0 flow is an OUTFLOW, `__house__` was DEBITED, so the repair
    pays the house back out of the treasury."""
    live_claim(beam_pay)
    code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID)
    assert code == 0
    body = body_of(lines)
    assert body == {
        "adjust_id": f"pgasme:fee:{LIVE_CLAIM_TXID}",
        "asset_id": 0,
        "from_address": TREASURY,
        "to_address": HOUSE,
        "amount_groth": LIVE_CLAIM_FEE_GROTH,
        "reason": body["reason"],
        "trade_ref": body["trade_ref"],
        "after_tx": LIVE_CLAIM_TXID,
    }
    assert "DRY RUN — nothing was sent." in "\n".join(lines)
    assert beam_pay.adjustments == [] and "/internal/ledger/adjust" not in beam_pay.paths()


async def test_the_repair_names_the_crossing_it_belongs_to(mock_db, beam_pay):
    """`trade_ref` is the row the transaction is evidence for, so the adjustment is traceable
    back to the deposit rather than floating free in BeamPay's audit log."""
    live_claim(beam_pay)
    await make_deposit(mock_db, dep_id="dep-137")
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep-137"}, {"$set": {"claim_txid": LIVE_CLAIM_TXID}}
    )
    _code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID)
    assert body_of(lines)["trade_ref"] == "dep-137"
    assert any("deposits dep-137" in ln for ln in lines)


async def test_apply_posts_the_documented_body_and_beampay_accepts_it(mock_db, beam_pay):
    live_claim(beam_pay)
    code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID, apply=True)
    assert code == 0 and len(beam_pay.adjustments) == 1
    sent = beam_pay.adjustments[0]
    assert sent["asset_id"] == 0 and sent["amount_groth"] == LIVE_CLAIM_FEE_GROTH
    assert (sent["from_address"], sent["to_address"]) == (TREASURY, HOUSE)
    assert sent["after_tx"] == LIVE_CLAIM_TXID
    assert any("APPLIED" in ln for ln in lines)
    # the scoped key, or not at all: /internal/* with the ordinary key is a 403 by design
    assert ("POST", "/internal/ledger/adjust", sent) in beam_pay.calls


async def test_the_kill_switch_stops_the_repair_inside_the_mover(mock_db, beam_pay, monkeypatch, tmp_path):
    stop = tmp_path / "pgasme.stop"
    stop.write_text("stop")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    live_claim(beam_pay)
    code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID, apply=True)
    assert code == 1 and beam_pay.adjustments == []
    assert any("kill switch is set" in ln for ln in lines)


async def test_the_repair_refuses_a_transaction_already_attributed_directly(mock_db, beam_pay):
    """A txid booked to the address that made it never credited or debited the house at all, so
    there is nothing to repair — and 'repairing' it would move value out of an account that
    never received it, crediting the address a SECOND time. Permanent, not a retry."""
    live_claim(beam_pay)
    await beam_pay.expect_contract_tx(LIVE_CLAIM_TXID, TREASURY, "dep-137")
    code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID, apply=True)
    assert code == 1 and beam_pay.adjustments == []
    assert any("already attributed directly" in ln for ln in lines)


async def test_the_repair_refuses_a_transaction_that_is_not_settled(mock_db, beam_pay):
    live_claim(beam_pay, status=beam.TX_CANCELLED, status_string="cancelled")
    code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID, apply=True)
    assert code == 1 and beam_pay.adjustments == []
    assert any("NOT SETTLED" in ln for ln in lines)


async def test_the_repair_refuses_past_beampays_own_gate_window(mock_db, beam_pay):
    """`GATE_TX_MAX_AGE_SEC` is 24 h: past it the route answers 409 `tx_too_old` and the repair
    is a human's correction. Refused HERE, so the operator reads why instead of a 409."""
    live_claim(beam_pay, create_time=int(time.time()) - 25 * 3600)
    code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID, apply=True)
    assert code == 1 and beam_pay.adjustments == []
    assert any("tx_too_old" in ln for ln in lines)


async def test_the_repair_refuses_a_transaction_beampay_has_not_booked(mock_db, beam_pay):
    code, lines = await out_lines(beam.cmd_repair_fee, "no-such-txid", apply=True)
    assert code == 1 and beam_pay.adjustments == []
    assert any("tx_not_found" in ln for ln in lines)


async def test_an_unreadable_beampay_is_never_nothing_to_do(mock_db, beam_pay):
    live_claim(beam_pay)
    beam_pay.raise_on = {"/internal/contract_tx/"}
    code, lines = await out_lines(beam.cmd_repair_fee, LIVE_CLAIM_TXID, apply=True)
    assert code == 1 and beam_pay.adjustments == []
    assert any("never 'nothing to do'" in ln for ln in lines)


def test_the_flow_arithmetic_is_beampays_own():
    """⛔ POSITIVE = wallet OUTFLOW = the house was DEBITED (process_payments.py:189-201). On a
    claim the asset-0 flow IS the fee, because the invocation moves no BEAM of its own."""
    claim = {"invoke_data": [{"amounts": [{"asset_id": 36, "amount": -LIVE_CLAIM_VALUE_GROTH}]}],
             "fee": str(LIVE_CLAIM_FEE_GROTH)}
    assert beam.house_flow(claim, 0) == LIVE_CLAIM_FEE_GROTH  # OUT: repay the house
    assert beam.house_flow(claim, 36) == -LIVE_CLAIM_VALUE_GROTH  # IN: the house pays the address
    send = {"invoke_data": [{"amounts": [{"asset_id": 36, "amount": 15_995_980}]}], "fee": "1100000"}
    assert beam.house_flow(send, 36) == 15_995_980
    assert beam.house_flow({"invoke_data": [], "fee": "0"}, 0) == 0


# ========================================================= §D · the 20:40Z deposit's `verified`


def deposit_2040z(**over: Any) -> dict[str, Any]:
    """The row as `.fable/WO-20260909-2/state.json` `live_deposit_1` records it: a BSC deposit
    registered seconds after signing (so the router's index could not vouch for the hash — `verified`
    correctly false), whose PRIMARY pass then found the pipe lock and carried it straight to
    `credited` and on through the treasury claim."""
    row = {
        "_id": "dep-2040z",
        "account_id": "acct1",
        "asset": "ETH",
        "mode": "xchain",
        "status": "credited",
        "verified": False,
        "quote_id": "q-2040z",
        "src_tx_hash": "0x55b64ffe" + "00" * 28 + "f32214",
        "order_id": "0x5d6f9014" + "00" * 28 + "0c488b",
        "eth": {"block": 25942261, "msg_id": 137, "value_units": "26528745000000000"},
        "value_groth": 2_652_864,
        "treasury": "claiming",
        "claim_txid": LIVE_CLAIM_TXID,
        "created_at": time.time() - 3 * 3600,
        "credited_at": time.time() - 3 * 3600,
        "updated_at": time.time() - 60,
    }
    row.update(over)
    return row


async def test_the_2040z_deposit_gets_its_verified_flag_on_the_next_deploy(mock_db, monkeypatch):
    """The evidence existed all along; it was simply never asked for again. `_reverify` only
    ever sets the flag TRUE — a row whose money already landed is not failed here."""
    row = deposit_2040z()
    await mock_db["pgasme_test"].deposits.insert_one(dict(row))

    async def ids(h, timeout=None):
        assert h == row["src_tx_hash"]
        return ["0x" + "88" * 32, row["order_id"]]

    monkeypatch.setattr(xchain, "order_ids_by_tx", ids)
    await workers.xchain_secondary()
    after = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep-2040z"})
    assert after["verified"] is True
    # …and nothing else about the row moved: not the status, not the claim, no new event
    assert after["status"] == "credited" and after["claim_txid"] == LIVE_CLAIM_TXID
    assert after["treasury"] == "claiming"
    assert await mock_db["pgasme_test"].events.count_documents({}) == 0


# =================================== §E · the destination is re-read with the burn one step away

CODE = "0x60806040"  # any non-empty code hex: a deployed contract at W


async def ready_payout(mock_db, eth: FakeEth, **over: Any) -> dict[str, Any]:
    """A payout that would release THIS pass, with the request-time code proof on the row."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    over.setdefault("dest_checked_head", eth.head - 50)
    over.setdefault("dest_checked_at", time.time() - 3 * 86_400)
    return await make_payout(mock_db, **over)


async def test_a_destination_that_is_still_a_wallet_releases(mock_db, eth, armed, beam_wallet):
    """The happy path, and the evidence it leaves: the block the re-check proved it at, next to
    the block the request path proved it at three days earlier."""
    await ready_payout(mock_db, eth)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing" and row["beam_txid"] == "beamtx-1"
    assert row["dest_recheck_head"] == eth.head and row["dest_recheck_at"] > 0
    reads = [c for c in eth.calls if c[0] == "eth_getCode"]
    assert reads and all(pin is True and prefer == FakeEth.LITE for _m, _p, prefer, pin in reads)
    assert reads[0][1] == [W, hex(eth.head)]  # at the head the endpoint itself reported


async def test_code_deployed_after_scheduling_holds_the_order_and_alerts_once(
    mock_db, eth, armed, beam_wallet, paged
):
    """⛔ THE REFUSAL WITH NO REFUND PATH. `refuse_contracts` ran 30 days ago at request time and
    nothing re-read it; a counterfactual CREATE2 account or an EIP-7702 delegation deployed since
    would have been delivered into, and a b2e crossing cannot be recalled.

    NOTHING is cancelled and nothing refunded: the groth stays in `scheduled` and an operator
    decides. And the immediate page happens ONCE — a held row is re-read forever, and an alert
    that repeats every five minutes is the pager nobody reads (law 15)."""
    await ready_payout(mock_db, eth)
    eth.code = {W.lower(): CODE}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "beam_txid" not in row
    assert row["hold_reason"] == payouts.DEST_NOW_CONTRACT
    assert "process_invoke_data" not in beam_wallet.methods()
    events = await mock_db["pgasme_test"].events.find({}).to_list(10)
    assert [e["kind"] for e in events] == ["payout_dest_now_contract"]
    assert events[0]["request_id"] == "req1" and events[0]["notified"] is True
    # …the money is untouched: still scheduled, not sent, not refunded
    bal = await ledger.balance("acct1", "ETH")
    assert bal["scheduled"] == 510_000 and bal["sent"] == 0

    # a second (and third) pass re-refuses without paging again
    await payouts.process_once()
    await payouts.process_once()
    assert len(await mock_db["pgasme_test"].events.find({}).to_list(10)) == 1
    assert (await payout(mock_db))["status"] == "scheduled"


async def test_the_alert_and_the_row_never_carry_the_destination(
    mock_db, eth, armed, beam_wallet, paged, caplog
):
    """§9.7: `api.log` pairs request_id → W and `payout_requests` pairs request_id → account_id,
    so one log line carrying W is the whole de-anonymisation."""
    import logging

    caplog.set_level(logging.INFO, logger="pgasme.payouts")
    await ready_payout(mock_db, eth)
    eth.code = {W.lower(): CODE}
    await payouts.process_once()
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "payout_dest_now_contract"})
    assert W.lower() not in str(ev).lower()
    assert all(W.lower() not in t.lower() for t in paged)
    assert W.lower() not in caplog.text.lower()


async def test_an_unreadable_destination_is_a_silent_retry_and_never_a_verdict(
    mock_db, eth, armed, beam_wallet, paged
):
    """"We could not look" is not "it is a contract" and certainly not "it is a wallet". The pass
    simply does not release: no hold on the row, no page, and the next pass asks again."""
    await ready_payout(mock_db, eth)
    eth.unreadable = {W.lower()}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "hold_reason" not in row
    assert "process_invoke_data" not in beam_wallet.methods()
    assert paged == [] and await mock_db["pgasme_test"].events.count_documents({}) == 0

    eth.unreadable = set()  # the endpoint comes back
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "releasing"


async def test_a_dead_endpoint_is_the_same_silent_wait(mock_db, eth, armed, beam_wallet, paged):
    await ready_payout(mock_db, eth)
    eth.head_dead = True
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "scheduled"
    assert paged == [] and "process_invoke_data" not in beam_wallet.methods()


async def test_a_head_below_the_one_already_proven_is_not_evidence(
    mock_db, eth, armed, beam_wallet, paged
):
    """⛔ A node that answers an OLD block returns "0x" for code deployed since — the guard
    silently becomes a no-op. The row already carries the block it was proven clear at, so a
    re-read below it is a lagging provider, and a lagging provider's answer is not acted on in
    EITHER direction."""
    await ready_payout(mock_db, eth, dest_checked_head=eth.head + 500)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "hold_reason" not in row
    assert paged == [] and "process_invoke_data" not in beam_wallet.methods()


async def test_the_repair_command_parses_its_arguments(mock_db, beam_pay, capsys):
    """`python -m pgasme.beam repair-fee --txid <txid> [--apply]` — and a missing txid is a
    usage error (2), never a run against something unnamed."""
    live_claim(beam_pay)
    assert await beam.cli_main(["repair-fee"]) == 2
    assert await beam.cli_main(["repair-fee", "--txid"]) == 2
    assert await beam.cli_main(["repair-fee", "--txid", LIVE_CLAIM_TXID]) == 0
    text = capsys.readouterr().out
    assert "DRY RUN · repair the BEAM fee leg" in text and "nothing was sent" in text
    assert beam_pay.adjustments == []
    assert "repair-fee --txid <txid>" in beam.USAGE
