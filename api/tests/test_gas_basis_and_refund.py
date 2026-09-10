"""T45 — the bridge fee at cost: an honest gas basis, the unspent fee refunded, spikes absorbed.

Admin, 2026-09-10 15:0xZ: *"Make sure you have correct gas fees"*. Three things were wrong, and
each one is a test below.

  1. THE GAS BASIS WAS ONE SAMPLE. `relayer_fee_for` reads `eth_feeHistory` once — base × 2 plus
     the median tip over 10 blocks — and multiplies by `PGAS_RELAYER_FEE_MARGIN` (1.5). On
     2026-09-10 gas went 0.66 → 2.18 gwei inside an hour; a quote taken in the trough funds a
     crossing the release cannot make. The basis is now `max(live, the 24 h p75 of the
     `gas_samples` series the deposit watcher writes ONE row per pass into)`.

  2. UNSPENT HEADROOM WAS KEPT. `headroom_for`'s own docstring said "unspent headroom stays with
     the treasury", which is not "the bridge at cost": two live orders funded 14,733 groth and
     paid 12,778, and the treasury kept 1,955 groth of the user's money EACH. It is credited
     back to Available at settlement now, once, as `bridge_fee_refund`.

  3. A SPIKE HELD PAID-FOR ORDERS. Order d028 funded 14,770 and the release wanted 24,299; only
     the 2× subsidy delivered it. The default is 4× now, and what we absorb is written on the row
     (`relayer_subsidy_groth`) instead of being invisible.

⚠️ 1 and 2 are one decision: a conservative basis is only honest BECAUSE the difference comes
back. Neither may be weakened without the other.
"""

from __future__ import annotations

import datetime as dt
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
    W,
    make_payout,
    payout,
)

from pgasme import beam, beampay, config, ledger, payouts, workers
from pgasme.config import settings
from pgasme.routers import withdrawals as wr

# The three real orders of 2026-09-10, from the box (`.fable/WO-20260909-2/state.json` and the
# T45 brief): two ASAP crossings quoted at 10:2xZ, and d028 which met the morning's spike.
ORDERS = [
    ("two ASAP orders, gas fell between quote and release", 14_733, 12_778, 1_955),
    ("the second of the pair, same numbers", 14_733, 12_778, 1_955),
    ("d028 met the 0.66 → 2.18 gwei spike", 14_770, 24_299, 0),
]


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    bp = FakeBeamPay()
    beampay.reset_health()
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)  # BEAM for fees
    bp.fund(MP, ETH.aid, 5 * GROTH)  # the shielded float a crossing is funded from
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    monkeypatch.setattr(settings, "shield_keep_groth", 0)
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


def gas_history(gwei: float) -> dict[str, Any]:
    """An `eth_feeHistory` answer `beam.max_gas_price_gwei` reads as exactly `gwei`.

    base × 2 + median(tip), with the tip pinned at the 0.01 floor and the base carrying the rest
    — the same shape `conftest.FakeRpc` uses, so a gwei figure in a test is the gwei figure the
    relayer's arithmetic sees."""
    tip = 0.01  # beam.MIN_PRIORITY_FEE_GWEI
    base_wei = max(0, round((gwei - tip) / 2 * 1e9))
    return {"baseFeePerGas": [hex(base_wei)] * 11, "reward": [[hex(round(tip * 1e9))]] * 10}


async def write_samples(mock_db: Any, *pairs: tuple[float, float]) -> None:
    """(age in seconds, gwei) rows straight into the series, the way a pass would have left them."""
    now = time.time()
    for age, gwei in pairs:
        at_s = now - age
        await mock_db["pgasme_test"].gas_samples.insert_one(
            {
                "at": dt.datetime.fromtimestamp(at_s, tz=dt.UTC),
                "at_s": at_s,
                "gwei": float(gwei),
            }
        )


# ══════════════════════════════════ 1 — the gas basis ════════════════════════════════════════


async def test_the_basis_is_the_max_of_the_live_read_and_the_24h_p75(mock_db, eth):
    """The rule, stated once and provable: `max(live, p75 over 24 h) × the relayer margin`.

    The assertion is not "the number went up" — it is that the fee equals the relayer's OWN
    arithmetic run at the p75 gas. `relayer_fee_for` scales `fee_units` (which is linear in gas
    in both branches of `beam.relayer_fee_groth`) rather than re-deriving 120,000 × gas × margin
    here, so there is still exactly one implementation of the relayer's sum (law 9) and this test
    is what proves the scaling and the sum are the same number."""
    eth.fee_history = gas_history(2.0)
    at_p75, _floor, _d = await payouts.relayer_fee_for(ETH, eth)

    # the market fell to 0.5 gwei; the last 24 h say 2.0 is the number a crossing meets
    eth.fee_history = gas_history(0.5)
    live, _floor, _d = await payouts.relayer_fee_for(ETH, eth)
    assert live < at_p75  # …which is the trough a quote must not be taken in

    await write_samples(mock_db, (60, 0.4), (120, 0.5), (180, 2.0), (240, 2.0))
    charged, _floor, detail = await payouts.relayer_fee_for(ETH, eth)
    assert charged == at_p75
    assert detail["gas_gwei"] == pytest.approx(0.5)  # the MEASUREMENT, unchanged: the evidence
    assert detail["gas_basis_gwei"] == pytest.approx(2.0)  # …and what it was CHARGED at
    assert detail["gas_p75_gwei"] == pytest.approx(2.0)


async def test_a_live_price_above_the_p75_is_the_one_that_is_charged(mock_db, eth):
    """The floor never LOWERS a quote: gas that has moved up is the gas the crossing meets."""
    await write_samples(mock_db, (60, 0.5), (120, 0.5))
    eth.fee_history = gas_history(5.0)
    charged, _floor, detail = await payouts.relayer_fee_for(ETH, eth)
    eth2 = FakeEth()
    eth2.fee_history = gas_history(5.0)
    plain, _d = await beam.relayer_fee_groth(ETH, eth2)
    assert charged == plain and detail["gas_basis_gwei"] == pytest.approx(5.0)


async def test_a_spike_older_than_the_window_is_not_a_floor(mock_db, eth):
    """25 h ago is not the market. The window is `config.GAS_BASIS_WINDOW_S`, and a sample that
    falls out of it stops being a floor — otherwise one bad hour prices every crossing for two
    days (the TTL is 48 h, deliberately longer than the window: the series outlives its own use
    so an operator can still read it)."""
    await write_samples(mock_db, (25 * 3600, 50.0), (3600, 0.4))
    eth.fee_history = gas_history(0.5)
    charged, _floor, detail = await payouts.relayer_fee_for(ETH, eth)
    assert detail["gas_basis_gwei"] == pytest.approx(0.5)
    assert detail["gas_p75_gwei"] == pytest.approx(0.4)
    eth2 = FakeEth()
    eth2.fee_history = gas_history(0.5)
    plain, _d = await beam.relayer_fee_groth(ETH, eth2)
    assert charged == plain


async def test_an_empty_series_quotes_at_the_live_price_and_says_so(mock_db, eth):
    """⛔ AN UNREADABLE SERIES IS NOT A ZERO AND NOT A FLOOR (law 8). A box that has just booted
    has no samples; that must quote exactly what it can measure, and never refuse."""
    eth.fee_history = gas_history(1.0)
    charged, _floor, detail = await payouts.relayer_fee_for(ETH, eth)
    eth2 = FakeEth()
    eth2.fee_history = gas_history(1.0)
    plain, _d = await beam.relayer_fee_groth(ETH, eth2)
    assert charged == plain
    assert detail["gas_p75_gwei"] == 0.0
    assert detail["gas_basis_gwei"] == pytest.approx(1.0)


async def test_the_p75_is_the_nearest_rank_of_the_window(mock_db):
    """Stated as arithmetic so it cannot drift: sorted ascending, index ceil(0.75 × n) − 1."""
    await write_samples(mock_db, *[(60 + i, float(i + 1)) for i in range(4)])  # 1,2,3,4
    assert await payouts.gas_p75_gwei() == pytest.approx(3.0)
    await write_samples(mock_db, (65, 5.0))  # 1,2,3,4,5 → ceil(3.75) − 1 = 3
    assert await payouts.gas_p75_gwei() == pytest.approx(4.0)


async def test_the_scanner_writes_exactly_one_sample_per_pass(mock_db, eth, monkeypatch):
    """ONE WRITER (law 9). The deposit watcher's pass is the only thing that appends to the
    series; `relayer_fee_for` only ever reads it. Two writers of one fact will disagree, and
    this one prices money."""
    eth.fee_history = gas_history(1.5)
    d = mock_db["pgasme_test"]
    await payouts.record_gas_sample(eth)
    await payouts.record_gas_sample(eth)
    rows = await d.gas_samples.find({}).to_list(10)
    assert len(rows) == 2
    assert all(r["gwei"] == pytest.approx(1.5) for r in rows)
    # the TTL index needs a BSON date; the window arithmetic needs epoch seconds — both, one row
    assert isinstance(rows[0]["at"], dt.datetime) and isinstance(rows[0]["at_s"], float)

    # …and the pass is what calls it: the watcher writes one row and nothing else does
    await d.gas_samples.delete_many({})
    monkeypatch.setattr(workers, "get_rpc", lambda: eth)
    out = await workers.deposit_watcher_once()
    assert await d.gas_samples.count_documents({}) == 1
    assert out["gas"] and out["gas"]["gwei"] == pytest.approx(1.5)


async def test_a_gas_read_that_fails_writes_no_sample_and_does_not_stop_the_pass(mock_db, eth):
    """An endpoint that would not answer is not evidence of a gas price (law 8), and it must not
    take the deposit scan down with it: no row, a named reason, no raise."""
    eth.fee_history = {}
    out = await payouts.record_gas_sample(eth)
    assert out is None
    assert await mock_db["pgasme_test"].gas_samples.count_documents({}) == 0


async def test_health_publishes_counts_only(client, mock_db, eth):
    """⛔ COUNTS, NEVER THE NUMBER — the same law `crossings` and `coins` are published under.
    A watchdog needs to see that the series is being written; nobody needs our fee basis."""
    await write_samples(mock_db, (60, 1.0), (120, 2.0))
    body = (await client.get("/v1/health")).json()
    gas = body["gas"]
    assert gas["samples_24h"] == 2 and gas["samples"] == 2
    assert gas["newest_age_s"] is not None and gas["newest_age_s"] < 3600
    assert "gwei" not in repr(gas) and "p75" not in repr(gas)


# ═══════════════════════════ 2 — the unspent bridge fee comes back ═══════════════════════════


@pytest.mark.parametrize("why,funded,paid,refund", ORDERS)
def test_the_refund_is_funded_minus_paid_on_the_three_real_orders(why, funded, paid, refund):
    """The exact arithmetic of 2026-09-10, as a table. `bridge_fee_groth` is what the user was
    charged for the crossing; `relayer_fee_groth` is what went INTO the send. The difference is
    theirs — and when the crossing cost more, the difference is OURS and nothing extra is
    charged."""
    row = {"bridge_fee_groth": funded, "relayer_fee_groth": paid}
    assert payouts.bridge_fee_refund_groth(row) == refund
    assert payouts.relayer_subsidy_groth(row) == max(0, paid - funded)


def test_a_crossing_whose_cost_we_cannot_read_refunds_nothing():
    """⛔ A MISSING `relayer_fee_groth` IS NOT A CROSSING THAT COST NOTHING (law 8). Read as a
    zero it would refund the WHOLE bridge fee of a crossing we really paid for — money invented,
    on the one path that puts money back into Available."""
    for paid in (None, 0, -5, "12778", float("nan")):
        assert payouts.bridge_fee_refund_groth({"bridge_fee_groth": 14_733, "relayer_fee_groth": paid}) == 0
    # …and a row that never funded a bridge fee has nothing to give back either
    assert payouts.bridge_fee_refund_groth({"relayer_fee_groth": 12_778}) == 0


async def test_settlement_credits_the_unspent_bridge_fee_back_to_available(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """END TO END: an order funds 14,733, the crossing is priced at today's gas, and the
    difference is in the user's Available the moment the kernel confirms — one append-only
    entry carrying its own evidence."""
    await make_payout(mock_db, bridge_fee_groth=14_733)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    before = (await ledger.balance("acct1", "ETH"))["available"]

    await payouts.process_once()  # scheduled → releasing (the send is signed here)
    row = await payout(mock_db)
    paid = int(row["relayer_fee_groth"])
    assert paid == 3_600 and paid < 14_733
    await payouts.process_once()  # → bridging

    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W, "relayerFee": paid, "height": 2}}
    await payouts.process_once()  # the kernel confirms → release booked → refund credited

    entry = await ledger.find_entry("bridge_fee_refund", "req1")
    assert entry is not None
    assert entry["groth"] == 14_733 - paid == 11_133
    assert entry["d_avail"] == 11_133 and entry["d_sched"] == 0 and entry["d_sent"] == 0
    # the evidence travels ON the row, so an audit never has to re-derive it from a rate
    assert entry["request_id"] == "req1"
    assert entry["funded_groth"] == 14_733 and entry["paid_groth"] == paid
    assert entry["refunded_groth"] == 11_133
    assert (await ledger.balance("acct1", "ETH"))["available"] == before + 11_133
    assert (await payout(mock_db))["bridge_fee_refund_groth"] == 11_133


async def test_a_retry_never_refunds_twice(mock_db, eth, armed, beam_wallet, beam_pay):
    """⛔ THE ENTRY THAT PUTS MONEY BACK IS THE ONE THAT MAY NEVER RUN TWICE (`cancel`'s law).
    Idempotent by request id: the read guard for the ordinary case, the unique index for the
    race, and every later pass is a no-op."""
    await make_payout(mock_db, bridge_fee_groth=14_733)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W, "relayerFee": 3_600, "height": 2}}
    await payouts.process_once()
    after_one = (await ledger.balance("acct1", "ETH"))["available"]

    row = await payout(mock_db)
    await payouts._book_release(row)  # the repair path, entered again on purpose
    await payouts.process_once()
    await payouts.process_once()
    assert await mock_db["pgasme_test"].entries.count_documents({"kind": "bridge_fee_refund"}) == 1
    assert (await ledger.balance("acct1", "ETH"))["available"] == after_one


async def test_a_crossing_that_cost_more_than_it_funded_refunds_nothing_and_charges_nothing(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """d028's shape: funded 14,770, the crossing wanted 24,299. WE ABSORB IT — nothing is
    charged beyond the quote, no entry is written against the user, and the loss is on the row
    where an operator can add it up (`relayer_subsidy_groth`)."""
    await make_payout(mock_db, bridge_fee_groth=14_770)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    eth.fee_history = gas_history(24_299 / 18_000)  # 18,000 groth per gwei for ETH
    before = (await ledger.balance("acct1", "ETH"))["available"]

    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing", row.get("hold_reason")
    paid = int(row["relayer_fee_groth"])
    assert paid > 14_770
    await payouts.process_once()
    beam_wallet.local_msgs = {7: {"amount": 500_000, "receiver": W, "relayerFee": paid, "height": 2}}
    await payouts.process_once()

    assert await ledger.find_entry("bridge_fee_refund", "req1") is None
    assert await ledger.find_entry("release", "req1") is not None  # it DID cross
    assert (await ledger.balance("acct1", "ETH"))["available"] == before
    row = await payout(mock_db)
    assert row["bridge_fee_refund_groth"] == 0
    assert row["relayer_subsidy_groth"] == paid - 14_770


async def test_an_order_that_never_crossed_is_never_refunded(mock_db, eth, armed):
    """NEVER ON A ROW THAT IS NOT SETTLED. A scheduled order has funded a bridge fee and paid
    nothing; a refund there would hand back money that is still reserved for a crossing that is
    still going to happen — and the cancel path would then refund the same groth again."""
    await make_payout(mock_db, bridge_fee_groth=14_733)
    assert await ledger.find_entry("bridge_fee_refund", "req1") is None
    row = await payout(mock_db)
    assert payouts.bridge_fee_refund_groth(row) == 0


async def test_the_public_row_always_carries_the_refund(client, user, mock_db):
    """The number the Balance page renders, present as a 0 rather than absent: a client that has
    to ask "is this field missing or is it nothing" writes the arithmetic twice."""
    now = time.time()
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {
            "_id": "pub1",
            "account_id": user["account_id"],
            "asset": "ETH",
            "mode": "direct",
            "W": W,
            "amount_groth": 500_000,
            "fee_groth": 10_000,
            "bridge_fee_groth": 14_733,
            "relayer_fee_groth": 12_778,
            "bridge_fee_refund_groth": 1_955,
            "status": "sent",
            "created_at": now,
            "updated_at": now,
        }
    )
    body = (await client.get("/v1/account", headers=user["headers"])).json()
    row = next(r for r in body["requests"] if r["_id"] == "pub1")
    assert row["bridge_fee_refund_groth"] == 1_955

    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "pub1"}, {"$unset": {"bridge_fee_refund_groth": ""}}
    )
    body = (await client.get("/v1/account", headers=user["headers"])).json()
    row = next(r for r in body["requests"] if r["_id"] == "pub1")
    assert row["bridge_fee_refund_groth"] == 0


# ═════════════════════════════════════ 3 — the subsidy ═══════════════════════════════════════


def test_the_subsidy_default_is_four_and_the_headroom_is_flat_at_its_floor():
    """`PGAS_MAX_RELAYER_SUBSIDY` = 4.0 (T45): a bridge fee is $0.10–0.20, so absorbing a 4×
    spike costs cents and the alternative is an order the user has paid for sitting for hours.

    ⚠️ AND IT FLATTENS THE HEADROOM CURVE, on purpose. `headroom_for` divides the far-dated
    margin by the subsidy the gate already allows, so at 4× every window quotes the
    `PGAS_BRIDGE_HEADROOM_MIN` floor — 3/4 is below 1, and the floor is what is left. The user
    is not funding a 3× crossing 30 days out any more; the gate carries the wait and the refund
    returns whatever the crossing does not use."""
    assert config.relayer_subsidy() == 4.0
    assert config.bridge_headroom_min() == 1.25
    window = settings.max_window_s
    assert wr.headroom_for(0) == 1.25
    assert wr.headroom_for(window / 2) == 1.25
    assert wr.headroom_for(window) == 1.25
    assert [r["factor"] for r in wr.headroom_curve()] == [1.25] * len(wr.headroom_curve())
    # the charge is the live fee × that floor, ceiled — a fee rounded down is a crossing the
    # treasury tops up out of its own pocket
    assert wr.fee_triple(1_000_000, 90_000, window)[1] == math.ceil(90_000 * 1.25)


async def test_a_spike_inside_the_subsidy_crosses_instead_of_holding(mock_db, eth, armed, beam_wallet):
    """The 2× that saved d028, with room: at 4× a crossing that costs 3.9× what it funded still
    goes out, and at 4.1× it is HELD (the money reserved) rather than crossed at a loss."""
    await make_payout(mock_db, bridge_fee_groth=10_000)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    eth.fee_history = gas_history(39_000 / 18_000)  # 39,000 groth = 3.9 × funded
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "releasing"

    await make_payout(mock_db, rid="req2", bridge_fee_groth=10_000)
    eth.fee_history = gas_history(41_000 / 18_000)  # 4.1 × funded
    await payouts.process_once()
    row = await payout(mock_db, "req2")
    assert row["status"] != "releasing"
    assert "refusing to cross at a loss" in str(row.get("hold_detail") or "")
