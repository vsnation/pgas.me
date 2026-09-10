"""T33 — what the WALLET can spend now, which source funds a crossing, and the working float.

2026-09-10 10:2xZ, read off the box: BeamPay's registry summed **2,652,864 groth of bETH**
across three max-privacy addresses while the wallet's own `/wallet_status.totals` for asset 36
said `available 0 · available_mp 0 · maturing_mp 1,652,864` (and could not see the third coin at
all — a rescan is pending). Every ledger gate in `_payout_scheduled` would have passed and the
release would have handed the wallet a send it cannot fund. **A per-address ledger balance is
what we OWN; the wallet's totals are what it can SPEND, and the two are different facts.**

The laws this file pins:

  * ONE reader of "what can the wallet spend now", per asset and per bucket
    (`payouts.wallet_spendable`) — the release gate, `beam status` and the dry run all ask it.
  * The float is the shielded registry PLUS the treasury's unshielded balance when
    `PGAS_PAYOUT_SPEND_UNSHIELDED=1`, and a crossing is funded from ONE source: regular first
    (no lock), then shielded. The row records which, and the flow is ATTRIBUTED THERE — booking
    a regular-funded crossing to the max-privacy address is the drift `_beam_fees_ok` was
    already fixed for once.
  * A bucket the wallet cannot spend cannot have funded anything: the release holds, names the
    numbers, and sends nothing. An unreadable `wallet_status` holds too — "we cannot see" is
    never "there is enough" (law 8).
  * The §9.3/S2 "refuse while any unshielded balance exists" gate is RETIRED under the same
    flag and still available to a deployment that wants it.
  * The shield keeps a working float: `PGAS_SHIELD_KEEP_GROTH` plus every scheduled-but-
    unreleased liability × the buffer stays unshielded, and only the excess is chunked.
  * Holds page as ONE digest per hour per kind, and a released hold says so once.
"""

from __future__ import annotations

import math
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
    deposit,
    make_deposit,
    make_payout,
    payout,
)
from test_log_redaction import telegram  # noqa: F401 — the ONE live-tg fixture in the suite

from pgasme import beam, beampay, payouts, tg
from pgasme.config import settings

# The live rows the admin scheduled at 10:2xZ, and the gas that held them.
LIVE_BRIDGE_FEE = 14_733  # what the two ASAP orders funded
LIVE_RELAYER_FEE = 15_793  # what the relayer wanted seconds later (a 7% base-fee tick)
# eth_feeHistory that produces exactly LIVE_RELAYER_FEE: fee = round(18_000 × maxFeePerGas_gwei)
LIVE_TICK_GAS = {"baseFeePerGas": ["0x19d9a6ec"], "reward": [["0x989680"]]}  # 0.8773889 gwei
# the box's own numbers for the three shielded coins, 2026-09-10
BOX_LEDGER_FLOAT = 2_652_864  # BeamPay, summed over the max-privacy registry
BOX_MATURING_MP = 1_652_864  # the wallet: locked by the max-privacy lock, spendable 0


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    bp = FakeBeamPay()
    beampay.reset_health()
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)  # BEAM for fees
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
    from pgasme import workers

    fake = FakeEth()
    monkeypatch.setattr(workers, "get_rpc", lambda: fake)
    return fake


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "claim_enabled", True)
    monkeypatch.setattr(settings, "shield_enabled", True)


@pytest.fixture
def paged(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None, float]]:
    """Every (text, key, cooldown_s) `tg.send` was asked for, in order."""
    out: list[tuple[str, str | None, float]] = []

    async def fake_send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        out.append((text, key, cooldown_s))
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    return out


def waiting(paged: list[tuple[str, str | None, float]]) -> list[str]:
    # ⚠️ NOT `startswith` (T52): the digest leads with the order it is about, the way
    # `tg.format_event` leads every other notification (`payout 64ee5540… — WAITING: …`).
    return [t for t, _k, _c in paged if "WAITING" in t]


def released(paged: list[tuple[str, str | None, float]]) -> list[str]:
    # T52 — "RELEASED: N held payout(s) moved on" was about rows that went back to the QUEUE,
    # and `released` is what this system calls money leaving. One word, one meaning.
    return [t for t, _k, _c in paged if t.startswith("Back in the queue")]


# ═════════════════════════════════════════════ 1 · one reader of the wallet's spendable buckets


async def test_the_wallet_status_totals_are_read_per_asset_and_per_bucket(beam_pay):
    """The shape the live wallet answers, and the split the release actually needs."""
    beam_pay.fund(MP, ETH.aid, BOX_LEDGER_FLOAT)
    beam_pay.wallet_totals[ETH.aid] = {
        "available_regular": 0,
        "available_mp": 0,
        "maturing_mp": BOX_MATURING_MP,
    }
    spend = await payouts.wallet_spendable(beam_pay, ETH)
    assert spend["regular"] == 0
    assert spend["shielded"] == 0
    assert spend["maturing_mp"] == BOX_MATURING_MP
    assert spend["maturing"] == BOX_MATURING_MP
    # …while the LEDGER still says we own it. Two different facts, both true.
    assert await payouts.float_groth(beam_pay, ETH) == BOX_LEDGER_FLOAT


async def test_an_unreadable_wallet_status_raises_rather_than_answering_zero(beam_pay):
    """Law 8: `_U.rpc(...) or []` turns "the endpoint did not answer" into "there is nothing"."""
    beam_pay.raise_on.add("/wallet_status")
    with pytest.raises(beampay.BeamPayError):
        await payouts.wallet_spendable(beam_pay, ETH)


async def test_an_asset_the_wallet_holds_none_of_reads_as_zero_not_as_unreadable(beam_pay):
    """A totals array that WAS read and simply has no row for this asset is a measurement."""
    spend = await payouts.wallet_spendable(beam_pay, ETH)
    assert spend == {
        "regular": 0,
        "shielded": 0,
        "maturing_regular": 0,
        "maturing_mp": 0,
        "maturing": 0,
        "locked": 0,
        "coins_regular": 0,
        "coins_shielded": 0,
        # the fixture stocks the treasury with BEAM for fees, and the fake splits every funded
        # bucket into `default_coins` coins — this asset is the one the wallet holds none of.
        # Each of those BEAM coins is 1.25 BEAM, well over the 0.15 send budget below.
        "fee_coins": 8,
        "fee_budget_groth": 15_000_000,
        "amounts_regular": [],
        "amounts_shielded": [],
        # WHY the coins could not be counted, when they could not — the caller's hold reason,
        # carried on the one answer rather than re-derived (None here: they were counted)
        "coins_error": None,
    }


# ═════════════════════════════════════════════ 2 · the float, and which source funds a crossing


async def test_the_float_adds_the_unshielded_treasury_only_under_the_flag(beam_pay, monkeypatch):
    beam_pay.fund(MP, ETH.aid, 400_000)
    beam_pay.fund(TREASURY, ETH.aid, 700_000)

    monkeypatch.setattr(settings, "payout_spend_unshielded", False)
    payouts.reset_process_state()
    parts = await payouts.payout_float(beam_pay, ETH)
    # `crossing` is the bETH sitting at funded crossing addresses (T40b F11) — always reported,
    # and inside `regular` when there is any
    assert parts == {"shielded": 400_000, "regular": 0, "crossing": 0, "total": 400_000}

    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    payouts.reset_process_state()
    parts = await payouts.payout_float(beam_pay, ETH)
    assert parts == {
        "shielded": 400_000, "regular": 700_000, "crossing": 0, "total": 1_100_000
    }


async def test_a_release_spends_regular_first_and_books_the_crossing_to_its_own_address(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """⛔ THE ATTRIBUTION FOLLOWS THE SOURCE. BeamPay debits the whole flow — the bETH AND the
    BEAM fee — from the address the txid is registered to. Booking a crossing funded out of the
    treasury's regular balance to the max-privacy address drives MP negative and leaves the
    treasury untouched, so the float every later payout gates on counts value already gone.

    Since T40 the regular source is one hop longer: the treasury moves exactly what the crossing
    burns into an address created for THIS order (admin 2026-09-10: *"when you specify sendFund
    from — it should be new SBBS address"*), and the txid books THERE. The law is unchanged —
    the flow books where the value came from — and the address is simply no longer shared."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 1_000_000)  # unshielded, spendable, no lock
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing" and row["source"] == "regular"
    addr = row["source_address"]
    assert addr not in (TREASURY, MP) and row["crossing_address"] is True
    assert beampay.looks_like_regular_address(addr)  # an SBBS address, never an MP token
    # the treasury funded it with EXACTLY what the send burns, by an internal transfer
    wd = [w for w in beam_pay.withdrawals if w["comment"] == payouts.fund_comment("req1")]
    assert len(wd) == 1 and wd[0]["from_address"] == TREASURY and wd[0]["to_address"] == addr
    assert wd[0]["amount"] == int(row["amount_groth"]) + int(row["relayer_fee_groth"])
    body = beam_pay.bodies_for("/internal/expect_contract_tx")[-1]
    assert body["address"] == addr  # not MP and not the treasury


async def test_a_release_falls_back_to_the_shielded_source_when_regular_is_short(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 1_000)  # dust: cannot fund the crossing
    beam_pay.fund(MP, ETH.aid, 5 * GROTH)
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing"
    assert row["source"] == "shielded" and row["source_address"] == MP
    assert beam_pay.bodies_for("/internal/expect_contract_tx")[-1]["address"] == MP


async def test_a_float_spread_over_two_buckets_that_no_single_source_covers_is_held(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """The total is enough and neither source is: a crossing is funded from ONE address."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 300_000)
    beam_pay.fund(MP, ETH.aid, 300_000)
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)  # needs 503_600
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "no single source" in row["hold_detail"]
    assert "process_invoke_data" not in beam_wallet.methods()


# ═════════════════════════════════════════════ 3 · the wallet-spendability gate (the new level)


async def test_a_maturing_float_holds_the_release_and_nothing_is_sent(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """THE DEFECT THIS TASK EXISTS FOR, with the box's own numbers. Every ledger gate passes:
    BeamPay says the max-privacy registry holds 0.02652864 bETH. The wallet says it can spend
    nothing at all — 0.01652864 is maturing under the max-privacy lock and the rest it cannot
    even see yet. A release here is a send the wallet refuses, which IS the lost-response state
    the whole resolver exists to survive."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(MP, ETH.aid, BOX_LEDGER_FLOAT)
    beam_pay.wallet_totals[ETH.aid] = {
        "available_regular": 0,
        "available_mp": 0,
        "maturing_mp": BOX_MATURING_MP,
    }
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    why = row["hold_detail"]
    assert "the wallet can spend" in why
    assert tg.fmt_groth(BOX_MATURING_MP) in why and "max-privacy lock" in why
    assert tg.fmt_groth(503_600) in why  # what this payout needs
    assert "process_invoke_data" not in beam_wallet.methods()
    assert row["holds"] == 1  # a refusal is not a trade and not a failure — but it writes a row


async def test_an_unreadable_wallet_holds_the_release_rather_than_sending(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(MP, ETH.aid, 5 * GROTH)
    beam_pay.raise_on.add("/wallet_status")
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "could not be read" in row["hold_detail"]
    assert "process_invoke_data" not in beam_wallet.methods()


async def test_the_wallet_gate_reserves_the_crossings_already_in_flight(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """A BVM invocation's inputs may not leave the wallet's `available` until the kernel
    registers, so two orders in one pass would both see one order's worth of spendable."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 600_000)
    for rid in ("req1", "req2"):
        await make_payout(mock_db, rid=rid, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    rows = [await payout(mock_db, r) for r in ("req1", "req2")]
    assert sorted(r["status"] for r in rows) == ["releasing", "scheduled"]
    assert beam_wallet.methods().count("process_invoke_data") == 1


# ═════════════════════════════════════════════ 4 · the S2 gate, retired under the flag


async def test_the_s2_unshielded_gate_is_retired_by_the_flag(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """§9.3/S2 said: refuse while ANY unshielded balance exists, because a send funded from a
    freshly-claimed regular output links the claim to the payout. The admin accepted that
    trade-off on 2026-09-10 ("as Beam is private by default we can mix those BEAMs"), so the
    gate is flag-retired — and it must still be THERE for a deployment that wants it."""
    beam_pay.fund(TREASURY, ETH.aid, 1_000)  # unshielded dust at the treasury
    beam_pay.fund(MP, ETH.aid, 5 * GROTH)
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)

    monkeypatch.setattr(settings, "payout_spend_unshielded", False)
    monkeypatch.setattr(settings, "beam_send_inputs_proven", False)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "spec S2" in row["hold_detail"]
    assert "process_invoke_data" not in beam_wallet.methods()

    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "releasing"


# ═════════════════════════════════════════════ 5 · the shield keeps a working float


async def shielding_deposit(mock_db: Any, dep_id: str = "dep1", plan: list[int] | None = None):
    """A deposit already in `shielding` with its plan pinned, exactly as `_treasury_claimed`
    leaves it."""
    plan = plan or [1_000_000, 1_000_000]
    await make_deposit(mock_db, dep_id=dep_id, value=sum(plan))
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": dep_id},
        {
            "$set": {
                "treasury": "shielding",
                "treasury_at": time.time() - 60,
                "shield_since": time.time() - 60,
                "shield_plan": plan,
                "shield_txids": [],
                "shield_calls": [],
            }
        },
    )


async def test_the_shield_keeps_the_floor_unshielded_and_shields_only_the_excess(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    monkeypatch.setattr(settings, "shield_keep_groth", 1_500_000)
    monkeypatch.setattr(settings, "shield_liability_buffer_bps", 1000)
    await shielding_deposit(mock_db)
    # the claim booked the deposit here; pin the exact balance the policy will measure
    beam_pay.addresses[TREASURY]["available"][str(ETH.aid)] = 2_000_000
    await payouts.process_once()
    dep = await deposit(mock_db)
    assert dep["treasury"] == "shielding" and not beam_pay.withdrawals
    assert "working float" in dep["hold_reason"]
    assert tg.fmt_groth(1_500_000) in dep["hold_reason"]

    # lower the floor and the same chunk goes out
    monkeypatch.setattr(settings, "shield_keep_groth", 500_000)
    await payouts.process_once()
    assert len(beam_pay.withdrawals) == 1
    assert int(beam_pay.withdrawals[0]["amount"]) == 1_000_000


async def test_the_shield_also_keeps_the_scheduled_liabilities_plus_the_buffer(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """The floor is not the only claim on the unshielded balance: every payout already
    scheduled and not yet released has to be payable out of it, plus the buffer that pays for
    a gas tick between the quote and the release."""
    monkeypatch.setattr(settings, "shield_keep_groth", 500_000)
    monkeypatch.setattr(settings, "shield_liability_buffer_bps", 1000)
    # scheduled for later, so it is still OWED when this pass shields: a liability is what the
    # treasury has promised and not yet spent, whether or not its window has opened
    await make_payout(
        mock_db, rid="sched1", amount=1_200_000, bridge_fee_groth=100_000,
        release_at=time.time() + 3600,
    )
    await shielding_deposit(mock_db)
    beam_pay.addresses[TREASURY]["available"][str(ETH.aid)] = 2_000_000
    liability = math.ceil((1_200_000 + 100_000) * 1.1)  # 1,430,000
    assert await payouts.scheduled_liability_groth(ETH) == 1_200_000 + 100_000

    await payouts.process_once()
    dep = await deposit(mock_db)
    assert not beam_pay.withdrawals
    assert "working float" in dep["hold_reason"]
    assert tg.fmt_groth(liability) in dep["hold_reason"]


async def test_a_released_payout_stops_being_a_liability(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """`releasing` and beyond is money already committed to the chain — `inflight_groth`'s
    territory, not the shield policy's. Counting it twice keeps the treasury from ever
    shielding anything again."""
    await make_payout(mock_db, rid="gone", amount=1_000_000, bridge_fee_groth=5_000,
                      status="bridging", beam_txid="t-gone")
    assert await payouts.scheduled_liability_groth(ETH) == 0
    await make_payout(mock_db, rid="held", amount=900_000, bridge_fee_groth=4_000,
                      status="held", held_from="scheduled")
    # a row parked for a human that never left `scheduled` still owes its user the money
    assert await payouts.scheduled_liability_groth(ETH) == 904_000


# ═════════════════════════════════════════════ 6 · holds page as ONE digest per kind


async def test_two_payouts_held_for_one_reason_page_once(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, paged
):
    """Three orders held for the same reason are three pages an hour, for ever. The admin has
    already asked for this to stop: the ROW keeps its own reason, the PAGER gets one digest."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    for rid in ("req1", "req2", "req3"):
        await make_payout(mock_db, rid=rid, bridge_fee_groth=1)  # the subsidy gate holds them
    await payouts.process_once()
    msgs = waiting(paged)
    assert len(msgs) == 1, msgs
    assert "WAITING (3 payouts)" in msgs[0]
    for rid in ("req1", "req2", "req3"):
        assert rid in msgs[0]
        row = await payout(mock_db, rid)
        assert "refusing to cross at a loss" in row["hold_detail"]  # the row is unchanged


async def test_a_held_payout_that_finally_releases_says_so_once(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, paged
):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()  # no float at all → held
    assert (await payout(mock_db))["status"] == "scheduled"
    assert len(waiting(paged)) == 1 and not released(paged)

    beam_pay.fund(TREASURY, ETH.aid, 1_000_000)
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "releasing"
    out = released(paged)
    assert len(out) == 1 and "req1" in out[0]


# ═════════════════════════════════════════════ 7 · the live orders, after the deploy


def live_rows(mock_db: Any) -> Any:
    """The three orders the admin scheduled at 10:2xZ, with their own numbers."""
    return [
        ("ecfa3ce28e0fb9a402bb5943", 100_000, LIVE_BRIDGE_FEE, 0),
        ("e4cb1e451616f548dad034d4", 100_000, LIVE_BRIDGE_FEE, 1),
        ("d028cb8ad7623f95f001caf9", 200_000, 14_770, 2),
    ]


async def test_the_held_live_orders_release_in_release_at_order_from_regular(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """Item 10. With `PGAS_MAX_RELAYER_SUBSIDY=2.0` the 7% gas tick is absorbed, and with a
    fresh deposit sitting unshielded at the treasury the wallet can fund all three."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    monkeypatch.setattr(settings, "max_relayer_subsidy", 2.0)
    eth.fee_history = LIVE_TICK_GAS
    now = time.time()
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    for rid, amount, bridge, k in live_rows(mock_db):
        await make_payout(
            mock_db, rid=rid, amount=amount, bridge_fee_groth=bridge, release_at=now - 300 + k
        )
    await payouts.process_once()
    rows = [await payout(mock_db, rid) for rid, *_ in live_rows(mock_db)]
    assert [r["status"] for r in rows] == ["releasing"] * 3
    assert [r["source"] for r in rows] == ["regular"] * 3
    assert [r["beam_txid"] for r in rows] == ["beamtx-1", "beamtx-2", "beamtx-3"]
    assert [r["relayer_fee_groth"] for r in rows] == [LIVE_RELAYER_FEE] * 3


async def test_the_held_live_orders_stay_held_on_the_wallet_bucket_when_nothing_is_spendable(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """…and with the subsidy raised but NOTHING the wallet can spend, they hold on the WALLET,
    not on the fee: the reason an operator reads has to be the one that is actually true."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    monkeypatch.setattr(settings, "max_relayer_subsidy", 2.0)
    eth.fee_history = LIVE_TICK_GAS
    beam_pay.fund(MP, ETH.aid, BOX_LEDGER_FLOAT)
    beam_pay.wallet_totals[ETH.aid] = {
        "available_regular": 0,
        "available_mp": 0,
        "maturing_mp": BOX_MATURING_MP,
    }
    now = time.time()
    for rid, amount, bridge, k in live_rows(mock_db):
        await make_payout(
            mock_db, rid=rid, amount=amount, bridge_fee_groth=bridge, release_at=now - 300 + k
        )
    await payouts.process_once()
    for rid, *_ in live_rows(mock_db):
        row = await payout(mock_db, rid)
        assert row["status"] == "scheduled"
        assert "the wallet can spend" in row["hold_detail"]
        assert "refusing to cross at a loss" not in row["hold_reason"]
    assert "process_invoke_data" not in beam_wallet.methods()


# ═════════════════════════════════════════════ 8 · the CLI prints what the gate reads


async def test_beam_status_prints_both_buckets_and_the_liabilities(
    mock_db, eth, beam_pay, beam_wallet, monkeypatch
):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(MP, ETH.aid, BOX_LEDGER_FLOAT)
    beam_pay.wallet_totals[ETH.aid] = {
        "available_regular": 0,
        "available_mp": 0,
        "maturing_mp": BOX_MATURING_MP,
    }
    await make_payout(mock_db, amount=100_000, bridge_fee_groth=LIVE_BRIDGE_FEE)
    lines: list[str] = []
    assert await beam.cmd_status(lines.append) == 0
    text = "\n".join(lines)
    assert "wallet spendable" in text.lower()
    assert tg.fmt_groth(BOX_MATURING_MP) in text  # the maturing bucket, named
    assert tg.fmt_groth(100_000 + LIVE_BRIDGE_FEE) in text  # the liability the policy protects


async def test_the_dry_run_names_the_source_it_would_spend_from(
    mock_db, eth, beam_pay, beam_wallet, monkeypatch
):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 1_000_000)
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    lines: list[str] = []
    assert await beam.cmd_dry_run_payout("req1", lines.append) == 0
    text = "\n".join(lines)
    assert "wallet spendable" in text.lower()
    assert "source" in text and "regular" in text
    assert TREASURY in text  # the address this crossing would BOOK to


# ═════════════════════════════════════════════ 9 · one coin per transaction (addendum 2)


async def test_two_due_releases_share_one_coin_and_the_second_defers(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """2026-09-10 10:30Z: two releases were submitted **0.7 s apart** and BOTH came back
    `Not enough inputs to process the transaction` (txids e370eec9…, 43b3ac73…, status 4).

    Beam locks a WHOLE COIN per pending transaction, and each contract invocation also needs a
    BEAM coin for its fee — so the number of concurrent releases the wallet can carry is not a
    balance at all, it is `min(coins of the asset in this source, spendable BEAM coins)`. The
    box had **two** spendable BEAM coins (0.01 and 9.835) and **zero** spendable bETH coins.

    A row that cannot get an input is DEFERRED, not held: nothing is wrong with it, it is
    simply not its turn. So no hold reason, no page, and the next pass reaches it."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    beam_wallet.coins[(ETH.aid, "regular")] = 1
    for rid in ("req1", "req2"):
        await make_payout(mock_db, rid=rid, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    rows = [await payout(mock_db, r) for r in ("req1", "req2")]
    assert sorted(r["status"] for r in rows) == ["releasing", "scheduled"]
    assert beam_wallet.methods().count("process_invoke_data") == 1
    deferred = next(r for r in rows if r["status"] == "scheduled")
    assert "hold_reason" not in deferred  # a defer is not a refusal
    assert "holds" not in deferred

    # two coins, and both go
    beam_wallet.coins[(ETH.aid, "regular")] = 2
    await payouts.process_once()
    assert (await payout(mock_db, "req2"))["status"] == "releasing"
    assert beam_wallet.methods().count("process_invoke_data") == 2


async def test_the_beam_fee_coins_limit_concurrency_too(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """Every contract invocation needs a BEAM coin for its own fee — and since T40 a crossing
    also has a FUNDING leg that pays one (T40b F12), so two BEAM coins carry exactly one
    crossing. Four bETH coins and two BEAM coins is one release at a time; one BEAM coin is
    none at all, which is the box's shape today (0.01 BEAM + 9.835 BEAM, one of them usable)."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    beam_wallet.coins[(ETH.aid, "regular")] = 4
    beam_wallet.coins[(0, "regular")] = 2
    for rid in ("req1", "req2"):
        await make_payout(mock_db, rid=rid, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    assert beam_wallet.methods().count("process_invoke_data") == 1


async def test_an_unreadable_coin_list_holds_the_row_and_pages_once_across_passes(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, telegram  # noqa: F811
):
    """⛔ **A GUARD THAT FAILS POLITELY IS A GUARD THAT FAILS** (law 11), and a refusal that
    writes no row is unalertable (law 12).

    An unreadable `get_utxo` was swallowed into `counts = None` and the release returned in the
    LOG — for ever, with no row, no reason and no page. A wallet-api that is down, or answering
    a shape this reader cannot parse, therefore stopped every payout silently, and the only
    evidence the operator had that payouts had stopped was that they had stopped.

    It is still never a send — "we cannot see" is never "there are coins" (law 8) — but it is
    now a WAITING hold carrying the wallet's own words, and the pager hears ONCE: the row
    counts its own refusals, the digest is keyed on the kind and its cooldown is an hour."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    beam_wallet.raise_on.add("get_utxo")
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    for _ in range(3):
        await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "process_invoke_data" not in beam_wallet.methods()
    assert "coin list could not be read" in row["hold_detail"]
    assert "fake outage" in row["hold_detail"]  # the wallet's own words, on the row
    assert row["holds"] == 3  # every decision path writes a row…
    pages = [r for r in telegram if b"WAITING" in r.content]
    assert len(pages) == 1, [r.content for r in pages]  # …and the pager hears once


async def test_the_coin_counts_are_part_of_the_one_spendability_reader(beam_pay, beam_wallet):
    """One reader, one answer: the buckets AND the coins behind them, so the release gate, the
    CLI and the dry run cannot disagree about how many transactions the wallet can carry."""
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    beam_wallet.coins[(ETH.aid, "regular")] = 3
    beam_wallet.coins[(0, "regular")] = 2  # the box: 0.01 BEAM and 9.835 BEAM
    spend = await payouts.wallet_spendable(beam_pay, ETH)
    assert spend["coins_regular"] == 3
    assert spend["coins_shielded"] == 0
    assert spend["fee_coins"] == 2


async def test_beam_status_prints_the_coin_counts(mock_db, eth, beam_pay, beam_wallet):
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    beam_wallet.coins[(ETH.aid, "regular")] = 3
    beam_wallet.coins[(0, "regular")] = 2
    lines: list[str] = []
    assert await beam.cmd_status(lines.append) == 0
    text = "\n".join(lines)
    assert "spendable coins" in text
    assert "3 coin(s) regular" in text and "2 BEAM fee coin(s)" in text
    assert "concurrency: at most 2 regular" in text  # min(3 bETH coins, 2 BEAM fee coins)


async def test_a_wallet_status_row_and_a_coin_list_we_cannot_parse_each_hold_with_their_own_reason(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, paged
):
    """A shape we do not understand is not a number, and above all it is not 0.

    Both readers HOLD now (T33b: the coin list used to defer in the log, which is a guard that
    fails politely — law 11), and they hold for DIFFERENT reasons, because the reason an
    operator reads has to be the one that is actually true: an unparseable BUCKET is the
    release's FUNDING, an unparseable COIN LIST is its free INPUTS. Neither may take the pass
    down with a traceback, and neither is ever a send."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)

    # a coin whose status is not a number at all
    beam_wallet.bad_utxo = True
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "coin list could not be read" in row["hold_detail"]
    assert "ValueError" in row["hold_detail"]  # the parse failure, in its own words
    assert "process_invoke_data" not in beam_wallet.methods()

    # a totals row whose bucket is not a number
    beam_wallet.bad_utxo = False
    beam_pay.wallet_totals[ETH.aid] = {"available_regular": "lots"}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "spendable balance could not be read" in row["hold_detail"]
    assert "process_invoke_data" not in beam_wallet.methods()


async def test_a_beam_coin_too_small_to_pay_the_fee_is_not_a_fee_coin(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """⛔ COUNTING COINS IS NOT ENOUGH — A COIN HAS TO BE BIG ENOUGH TO PAY THE FEE.

    The box holds exactly two spendable BEAM coins: **0.01 BEAM and 9.835 BEAM**. The first
    cannot fund a contract invocation on its own — the first live claim paid 0.121 BEAM and the
    budget floor for a send is 0.15 — so the wallet really has ONE usable fee coin, not two. A
    gate that counted two would admit a second release and hand it the same failure the count
    exists to prevent ("Not enough inputs", 2026-09-10 10:30Z).

    So a BEAM coin counts as a fee coin only when it is at least this pass's `send` budget —
    the same derived number `_beam_fees_ok` reserves, read through the same helper."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    # the box's shape: 0.01 BEAM + 9.835 BEAM
    beam_pay.addresses[TREASURY]["available"]["0"] = 984_500_000
    beam_wallet.coin_amounts[(0, "regular")] = [1_000_000, 983_500_000]
    spend = await payouts.wallet_spendable(beampay.beampay(), ETH)
    assert spend["fee_coins"] == 1, "the 0.01 BEAM coin cannot pay a 0.15 BEAM budget"

    for rid in ("req1", "req2"):
        await make_payout(mock_db, rid=rid, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    # …and ONE usable fee coin funds NO crossing at all (T40b F12): a crossing pays BEAM twice,
    # once for the transfer that funds its own address and once for the invocation that burns
    # it. This is the box's real shape, and the answer is a coin split, never a looser gate.
    assert beam_wallet.methods().count("process_invoke_data") == 0
    assert "no free coin" in (await payout(mock_db))["hold_detail"]


async def test_a_wallet_with_no_fee_coin_at_all_is_held_and_paged_not_silently_deferred(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, paged
):
    """⛔ A DEFERRAL THAT HIDES A STARVING WALLET IS A `print()`-ONLY REFUSAL (law 12).

    "No free coin this pass" is not a problem — it is a queue. "No BEAM coin that can pay for a
    call at all" IS a problem, and the operator has to hear it. The COIN gate says so itself
    now (T33b): it used to lean on `_beam_fees_ok` to page for it, which only worked while the
    LEDGER was poor too — see the rich-ledger test below."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    beam_pay.addresses[TREASURY]["available"]["0"] = 1_000_000  # 0.01 BEAM, one coin
    beam_wallet.coin_amounts[(0, "regular")] = [1_000_000]
    spend = await payouts.wallet_spendable(beampay.beampay(), ETH)
    assert spend["fee_coins"] == 0  # nothing here can pay a 0.15 BEAM send budget
    payouts.reset_process_state()

    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "no free coin" in row["hold_detail"]  # a row and a page, before anything is signed
    assert "BEAM fee coins 0" in row["hold_detail"]
    assert len(waiting(paged)) == 1
    assert "process_invoke_data" not in beam_wallet.methods()


# ═════════════ 10 · T33b — the three gates a rich ledger walked straight through (2026-09-10)


async def test_a_zero_coin_budget_holds_and_pages_even_when_the_ledger_is_rich(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, paged
):
    """⛔ **A BUDGET OF ZERO SKIPPED ITS OWN GUARD AND THE RELEASE WENT ON TO SIGN.**

    `if budget > 0 and busy >= budget` defers only when there IS a budget. A budget of ZERO —
    no BEAM coin big enough to pay for a call, or no spendable coin of the source asset —
    fell straight through it, and the comment that said it "falls through to `_beam_fees_ok`,
    which holds with a reason and pages" was only true while the LEDGER was poor as well.

    The box's shape at 10:30Z is the counter-example: the treasury's ledger balance is 9.724
    BEAM, well over every balance gate, while the wallet's only FREE coin is 0.01 BEAM because
    the 9.714-BEAM one is locked inside a pending transaction. A coin a transaction is spending
    is not a coin another send can pick up — so every gate passed, the send was signed, and the
    wallet answered `Not enough inputs to process the transaction`."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    # the LEDGER is rich: 9.724 BEAM at the treasury, far above the 5-BEAM page floor…
    beam_pay.addresses[TREASURY]["available"]["0"] = 972_400_000
    # …and the WALLET has one free 0.01 BEAM coin, the big one being locked by a pending tx
    beam_wallet.coin_amounts[(0, "regular")] = [1_000_000]
    spend = await payouts.wallet_spendable(beampay.beampay(), ETH)
    assert spend["fee_coins"] == 0
    payouts.reset_process_state()

    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "process_invoke_data" not in beam_wallet.methods()  # nothing was signed
    assert "no free coin" in row["hold_detail"]
    assert "BEAM fee coins 0" in row["hold_detail"]
    msgs = waiting(paged)
    assert len(msgs) == 1 and "no free coin" in msgs[0]


async def test_a_source_with_no_spendable_coin_at_all_holds_instead_of_signing(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, paged
):
    """The other half of a zero budget: the BEAM fee coins are there, the SOURCE has none.

    BeamPay's ledger says the treasury owns 5 bETH; the wallet's UTXO list carries no spendable
    bETH coin at all (the box, 2026-09-10, while three shield chunks sat inside the max-privacy
    lock). `coin_capacity([], need)` is 0, so the budget is 0 — and a budget of 0 must be a
    refusal that writes a row, never a fall-through."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 5 * GROTH)
    beam_wallet.coin_amounts[(ETH.aid, "regular")] = []
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled"
    assert "process_invoke_data" not in beam_wallet.methods()
    assert "ETH spendable coins 0" in row["hold_detail"]
    assert len(waiting(paged)) == 1


async def test_the_shield_reserve_counts_the_crossings_already_in_flight(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """⛔ **`LIABLE` STOPS AT `scheduled`; BEAMPAY'S TREASURY BALANCE DOES NOT.**

    A release leaves the liabilities the moment it is admitted — correctly, because
    `inflight_groth` reserves it against the float instead, and counting it in both would stop
    the treasury ever shielding again. But the value is still SITTING at the treasury until the
    burn is booked, so the shield policy measured a balance it was not allowed to spend and
    would put the working float into a 72-hour Lelantus lock. The reserve therefore grosses up
    liabilities PLUS everything in flight."""
    monkeypatch.setattr(settings, "shield_keep_groth", 5_000_000)
    monkeypatch.setattr(settings, "shield_liability_buffer_bps", 1000)
    await make_payout(
        mock_db, rid="flight", amount=20_000_000, bridge_fee_groth=0,
        status="bridging", beam_txid="t-flight",
    )
    assert await payouts.scheduled_liability_groth(ETH) == 0  # not a liability any more…
    assert await payouts.inflight_groth(ETH) == 20_000_000  # …but committed all the same
    await shielding_deposit(mock_db, plan=[10_000_000])
    # the claim booked the deposit here; the balance still carries the crossing in flight
    beam_pay.addresses[TREASURY]["available"][str(ETH.aid)] = 30_000_000
    await payouts.process_once()
    dep = await deposit(mock_db)
    assert not beam_pay.withdrawals  # nothing was shielded
    assert "working float" in dep["hold_reason"]
    assert tg.fmt_groth(20_000_000) in dep["hold_reason"]  # the in-flight number, named
    assert tg.fmt_groth(22_000_000) in dep["hold_reason"]  # …and the reserve it produces


async def test_the_float_does_not_count_value_already_handed_to_a_shield_withdraw(
    mock_db, eth, beam_pay, beam_wallet, monkeypatch
):
    """⛔ **BROADCAST ≠ DONE, ONE LEVEL EARLIER.** `/withdraw` is a QUEUE: BeamPay records the
    request and its daemon emits the transaction seconds later — the gap `_treasury_shielding`
    waits out at `called_at`, and the reason a second call can never be made. Until the
    transaction exists the treasury's `available_groth` still carries the chunk, so
    `payout_float` counted as spendable float value a shield had already claimed."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    await shielding_deposit(mock_db, plan=[1_000_000])
    beam_pay.addresses[TREASURY]["available"][str(ETH.aid)] = 1_000_000
    payouts.reset_process_state()
    assert (await payouts.payout_float(beam_pay, ETH))["regular"] == 1_000_000  # none queued

    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"},
        {"$set": {"shield_calls": [{"at": time.time(), "to_address": MP}]}},
    )
    payouts.reset_process_state()
    parts = await payouts.payout_float(beam_pay, ETH)
    assert parts["regular"] == 0
    assert parts["total"] == parts["shielded"]


async def test_the_dry_run_registers_to_the_source_it_would_actually_spend_from(
    mock_db, eth, beam_pay, beam_wallet, monkeypatch
):
    """⛔ **A DRY RUN THAT REPORTS SOMETHING OTHER THAN THE LIVE RUN IS NOT A DRY RUN** (law 8:
    the prober must call the way the caller calls).

    Step 4 printed the max-privacy address as the registration address whatever the source was.
    A regular-funded crossing registered against MP drives MP negative and leaves the treasury
    untouched — the exact drift `attribution_address` exists to make impossible, previewed to
    the operator as if it were the plan."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    beam_pay.fund(TREASURY, ETH.aid, 1_000_000)
    await make_payout(mock_db, bridge_fee_groth=LIVE_BRIDGE_FEE)
    lines: list[str] = []
    assert await beam.cmd_dry_run_payout("req1", lines.append) == 0
    reg = next(ln for ln in lines if '"txid"' in ln)
    assert TREASURY in reg and MP not in reg

    # …and once the row has DECIDED, the row is the authority (`attribution_address`)
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"source": "shielded", "source_address": MP}}
    )
    lines = []
    assert await beam.cmd_dry_run_payout("req1", lines.append) == 0
    reg = next(ln for ln in lines if '"txid"' in ln)
    assert MP in reg and TREASURY not in reg
