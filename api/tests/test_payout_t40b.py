"""T40b — the skeptic's round on the never-failed payout machine (F2…F13).

Every test here is a defect that was found by reading T40's own code back, and each one is
named for the thing the machine may not do. The two properties the whole file is subordinate
to are still `tests/test_payout_never_failed.py`'s: **a withdrawal never fails on the user's
side**, and **one order is never paid twice**. Nothing below may be made to pass by weakening
either of them.

  F2  a FUNDED retry re-enters the ordinary gate chain before it signs — the funding marker
      was a bypass round the wallet gate, the coin gate and the float gate.
  F3  a gate refusal on an order whose delivery window has passed is a DELAY, not a silent
      hold: the row said `scheduled` with an ETA 28 minutes in the past.
  F4  a terminal row is told what happened, and no ETA note carries a raw timestamp.
  F5  a funded crossing is committed float: two orders could fund against one crossing's worth.
  F6  a BOOKED release never re-enters the retry ladder, whatever a later status read says.
  F7  one writer per fee field — the funding transfer's fee was read back as the invocation's.
  F8  a crossing's own BEAM fee is counted ONCE, not once at its address and again in the debt.
  F10 the funding marker can never exist without the address it funds.
  F11 value sitting at a funded crossing address is visible to the float readers.
  F12 a crossing locks TWO ordinary BEAM coins (the funding transfer, then the invocation).
  F13 the destination re-read asks every endpoint for its own head (an archive refusal on the
      first endpoint used to stop every release).
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
    kinds,
    make_deposit,
    make_payout,
    payout,
)

from pgasme import beam, beampay, ethpipe, payouts, tg, workers
from pgasme.config import settings
from pgasme.routers import account, admin

ADMIN_KEY = "t40b-admin-key-" + "0123456789abcdef" * 2


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    bp = FakeBeamPay()
    beampay.reset_health()
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)  # BEAM for fees
    bp.fund(TREASURY, ETH.aid, 5 * GROTH)  # the unshielded float a crossing is funded from
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    monkeypatch.setattr(settings, "shield_keep_groth", 0)
    # every crossing in this file is funded out of the treasury's regular balance — the shielded
    # pool is never refilled and the box has spent it (T40 (g))
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
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


@pytest.fixture
def paged(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None, float]]:
    out: list[tuple[str, str | None, float]] = []

    async def fake_send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        out.append((text, key, cooldown_s))
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    return out


def sends(w: FakeWalletApi) -> int:
    """How many pipe SENDs have been built — the only number that can prove a double spend."""
    return len([p for p in w.params_for("invoke_contract") if "action=send," in p["args"]])


async def funded_crossing(
    mock_db: Any, beam_pay: FakeBeamPay, eth: FakeEth, rid: str = "req1", amount: int = 500_000
) -> dict[str, Any]:
    """One order whose crossing address exists and holds exactly what the send burns, with the
    send NOT yet made — the state every retry re-enters through."""
    await make_payout(mock_db, amount=amount, rid=rid)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_pay.withdraw_lands = False  # BeamPay queued the transfer; its daemon has not emitted it
    await payouts.process_once()
    payouts.reset_process_state()
    row = await payout(mock_db, rid)
    addr = str(row["source_address"])
    need = int(row["amount_groth"]) + int(row["relayer_fee_groth"])
    beam_pay.add_tx(
        txId=f"wd-fund-{rid}", type="withdrawal", type_string=None, asset_id=str(ETH.aid),
        value=str(need), fee=str(beam_pay.withdraw_fee), sender=TREASURY, receiver=addr,
        comment=f"payout|{rid}|fund", kernel=f"k-wd-{rid}",
    )
    beam_pay.fund(addr, ETH.aid, need)
    beam_pay.withdraw_lands = True
    return await payout(mock_db, rid)


# ═════════════════════════════════════════ F2 · a funded retry meets every gate


async def test_a_funded_order_re_enters_the_wallet_and_coin_gates_before_it_signs(
    mock_db, eth, armed, beam_pay, beam_wallet, monkeypatch
):
    """⛔ THE FUNDING MARKER WAS A BYPASS. `_payout_scheduled` returned early into
    `_payout_funding` the moment `fund_called_at` existed, so the wallet-spendability gate, the
    free-coin gate and the float gate never ran again on a retry — on the one path where the
    money has already left the treasury. The shape is T33b's live one: the wallet can spend no
    regular bETH and holds no free BEAM fee coin."""
    await funded_crossing(mock_db, beam_pay, eth)
    asked: list[str] = []

    async def spy(bp: Any, asset: Any) -> dict[str, Any]:
        asked.append(asset.key)
        return {
            "regular": 0, "shielded": 0, "maturing_regular": 0, "maturing_mp": 1_652_864,
            "maturing": 1_652_864, "locked": 0, "coins_regular": 0, "coins_shielded": 0,
            "amounts_regular": [], "amounts_shielded": [], "fee_coins": 0,
            "fee_budget_groth": 15_000_000, "coins_error": None,
        }

    monkeypatch.setattr(payouts, "wallet_spendable", spy)
    before = sends(beam_wallet)
    await payouts.process_once()
    row = await payout(mock_db)
    assert asked == ["ETH"], "a retry must ask the wallet what it can spend before it signs"
    assert sends(beam_wallet) == before  # ⛔ NOTHING SIGNED
    assert row["status"] in ("scheduled", payouts.DELAYED)
    assert row.get("beam_txid") is None
    assert "spend" in str(row["hold_detail"]) or "coin" in str(row["hold_detail"])


# ═════════════════════════════════════════ F3 · a refusal past the window is a delay


async def test_a_gate_refusal_after_the_delivery_window_delays_and_never_shows_a_past_eta(
    mock_db, eth, armed, beam_pay, beam_wallet
):
    """The live row (d028cb8ad7623f95f001caf9): 13 holds, `deliver_at` 1,705 s in the past,
    status `scheduled` and the note "the delivery time you chose". A refusal after the window
    has passed is a DELAY, through the one ladder, with an ETA that is still in the future."""
    now = time.time()
    await make_payout(mock_db, deliver_at=now - 1800)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_pay.addresses[TREASURY]["available"][str(ETH.aid)] = 0  # no float at all
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == payouts.DELAYED
    assert float(row["next_attempt_at"]) > now
    assert row["delayed_from"] == "scheduled"
    eta_at, _tail, note = payouts.eta_for(row)
    assert eta_at is not None and eta_at > time.time(), "an ETA in the past is a broken promise"
    # ⛔ AND IT SAYS WHAT IS HAPPENING, NOT WHAT THE STATUS IS CALLED (T52). "delayed: the float
    # holds 0.0242 ETH …" was a status word glued to an operator fragment; the note is now the
    # sentence written for this reader, and the machine word lives on `status` where a client
    # keys off it.
    assert note == payouts.WAIT_FLOAT + payouts.FLOAT_SOON


async def test_a_scheduled_row_whose_window_has_passed_is_never_promised_a_past_time():
    now = time.time()
    at, _tail, note = payouts.eta_for(
        {"status": "scheduled", "mode": "direct", "deliver_at": now - 1705}
    )
    assert at is not None and at > now
    assert "passed" in note or "next in line" in note


# ═════════════════════════════════════════ F4 · terminal rows, and no raw timestamps


async def test_eta_for_says_what_happened_on_a_terminal_row():
    """The two refunded legacy rows (ecfa3ce2…, e4cb1e45…) were being told they were on their
    way: `eta_for` had no `failed` branch at all, so they fell through to the scheduled one."""
    for status, word in (
        ("failed", "returned"),
        ("refunded", "returned"),
        ("cancelled", "cancel"),
        ("sent", "deliver"),
    ):
        at, tail, note = payouts.eta_for({"status": status, "mode": "direct"})
        assert at is None, status
        assert tail == 0, status
        assert word in note.lower(), (status, note)


async def test_a_returned_row_never_says_refunded(mock_db, client, user):
    """T48 (admin 2026-09-10 15:24Z, looking at the two rows from this morning): "Avoid status
    Refunded, it's not clear for the user … Refunded back to the balance or what?"

    So the word is gone from the one place that writes it — a note the web renders verbatim —
    and what replaces it says where the money IS and what to do next. It also says **nothing
    was sent**, which is the question behind the admin's: an order that ends without arriving
    is either the user's own cancel or one of these, and neither of them moved a coin."""
    at, tail, note = payouts.eta_for({"status": "failed", "mode": "direct"})
    assert (at, tail) == (None, 0)
    assert note == "returned to your balance — nothing was sent; schedule again when ready"
    assert "refund" not in note.lower()

    # …and the row a signed-in user actually reads carries that note verbatim
    now = time.time()
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {"_id": "r-returned", "account_id": user["account_id"], "asset": "ETH",
         "mode": "direct", "W": W, "amount_groth": 100_000, "status": "failed",
         "created_at": now - 3600}
    )
    r = await client.get("/v1/account", headers=user["headers"])
    assert r.status_code == 200
    row = next(x for x in r.json()["requests"] if x["_id"] == "r-returned")
    assert row["eta_at"] is None
    assert row["eta_note"] == "returned to your balance — nothing was sent; schedule again when ready"


async def test_a_delayed_note_carries_no_raw_timestamp(mock_db, client, user):
    """M3 (handed over from T35b): a page cannot render an epoch, so the note says what is
    happening in words and the ROW carries `next_try_at` as ISO-8601 for the client to render
    relatively."""
    now = time.time()
    nxt = now + 120
    aid = user["account_id"]
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {"_id": "r-delay", "account_id": aid, "asset": "ETH", "mode": "direct", "W": W,
         "amount_groth": 500_000, "status": payouts.DELAYED, "hold_reason": "no free coin",
         "next_attempt_at": nxt, "created_at": now}
    )
    r = await client.get("/v1/account", headers=user["headers"])
    assert r.status_code == 200
    row = next(x for x in r.json()["requests"] if x["_id"] == "r-delay")
    assert "no free coin" in row["eta_note"]
    assert "next try at" not in row["eta_note"].lower()
    assert str(int(nxt)) not in row["eta_note"]
    assert "we retry automatically" in row["eta_note"]
    assert isinstance(row["next_try_at"], str) and row["next_try_at"].endswith("Z")


# ═════════════════════════════════════════ F5 · a funded crossing is committed float


async def test_two_orders_cannot_fund_against_one_crossings_worth(
    mock_db, eth, armed, beam_pay, beam_wallet
):
    """A funded order stayed `scheduled` and counted for nothing, so the float gate admitted the
    next one against money that had already left the treasury."""
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    fee, _floor, _detail = await payouts.relayer_fee_for(ETH, eth)
    payouts.reset_process_state()
    beam_pay.addresses[TREASURY]["available"][str(ETH.aid)] = 500_000 + fee  # ONE crossing
    beam_pay.withdraw_lands = False  # neither funding settles in this pass
    await make_payout(mock_db, rid="req1")
    await make_payout(mock_db, rid="req2")
    await payouts.process_once()

    funds = [w for w in beam_pay.withdrawals if str(w.get("comment", "")).endswith("|fund")]
    assert len(funds) == 1, funds
    loser = "req2" if funds[0]["comment"] == "payout|req1|fund" else "req1"
    row = await payout(mock_db, loser)
    assert "fund_called_at" not in row
    assert "committed to crossings in flight" in str(row["hold_detail"])


async def test_a_funded_order_counts_as_in_flight_for_the_float(mock_db, eth, armed, beam_pay):
    row = await funded_crossing(mock_db, beam_pay, eth)
    need = int(row["amount_groth"]) + int(row["relayer_fee_groth"])
    assert await payouts.inflight_groth(ETH) == need
    assert await payouts.inflight_groth(ETH, "req1") == 0  # …and never against itself


# ═════════════════════════════════════════ F6 · a booked release is never "dead"


async def test_a_booked_release_never_re_enters_the_retry_ladder(
    mock_db, eth, armed, beam_pay, beam_wallet
):
    """§BOOKED-IS-LANDED. The double-release alarm lives on `release_booked_txid`, and a retry
    that cleared it would blind the one check that notices two crossings for one order. So a row
    that has a kernel or a booked release NEVER retries, whatever a later status read says — it
    goes to a human with the contradiction on it."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_pay.add_tx(
        txId="beamtx-1", status=beam.TX_FAILED, status_string="failed", kernel=None, fee="0"
    )
    now = time.time()
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"},
        {"$set": {"status": payouts.DELAYED, "beam_txid": "beamtx-1",
                  "kernel_at": now - 600, "release_booked_txid": "beamtx-1",
                  "next_attempt_at": now - 1, "delayed_since": now - 300,
                  "delayed_from": "releasing", "delays": 1}},
    )
    before = sends(beam_wallet)
    await payouts.process_once()
    row = await payout(mock_db)
    assert sends(beam_wallet) == before  # ⛔ NOT ONE MORE SEND
    assert row["status"] == payouts.HELD
    assert "booked" in str(row["hold_detail"]) or "kernel" in str(row["hold_detail"])
    assert (await kinds(mock_db)).count("payout_release_contradiction") == 1

    dead, why = await payouts.previous_attempt_dead(row)
    assert dead is False and ("booked" in why or "kernel" in why)


# ═════════════════════════════════════════ F7 · one writer per fee field


async def test_fee_charged_writes_only_the_field_it_is_given(mock_db):
    await mock_db["pgasme_test"].payout_requests.insert_one({"_id": "req1", "status": "scheduled"})
    await payouts.fee_charged(
        "payout_requests", "req1", {"fee": 100_000}, "funding req1", "request_id",
        field="fund_fee_groth",
    )
    row = await payout(mock_db)
    assert row["fund_fee_groth"] == 100_000
    assert "beam_fee_groth" not in row  # ⛔ the field a SEND's fee history used to be read from


async def test_the_send_fee_history_reads_the_invocation_and_never_the_funding_transfer(mock_db):
    """`_payout_funding` wrote the /withdraw's 0.001 BEAM into `beam_fee_groth` and
    `_FEE_SOURCE["send"]` read it straight back as the cost of a pipe invocation — a budget 121×
    too small, derived from a number about a different kind of transaction."""
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {"_id": "old1", "beam_txid": "t1", "release_call_at": 1.0,
         "beam_fee_groth": 100_000, "crossing_fee_groth": 12_100_000}
    )
    assert await payouts.observed_fees("send") == [12_100_000]


# ═════════════════════════════════════════ F8 · a crossing's own fee, once


async def test_a_crossings_own_beam_fee_is_counted_once(mock_db, beam_pay, monkeypatch):
    """`_beam_fees_ok` ADDS the fee address's (negative) balance and then SUBTRACTS the whole
    crossing-fee debt — and after a settled crossing on a retry, this crossing's fee is in both.
    Counted twice, the gate refuses a wallet that can pay."""
    fee = 12_100_000
    addr = "7c" + "aa" * 31 + "01"
    beam_pay.register(addr, "regular")
    beam_pay.fund(addr, 0, -fee)  # BeamPay books an invocation's whole fee to the registered address
    beam_pay.addresses[TREASURY]["available"]["0"] = 2 * fee + 3_050_000
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {"_id": "req1", "status": "scheduled", "asset": "ETH", "crossing_address": True,
         "crossing_fee_groth": fee, "source_address": addr}
    )
    payouts.reset_process_state()
    ok = await payouts._beam_fees_ok(beam_pay, "send", "req1", "payout_requests", fee_addr=addr)
    assert ok is True, (await payout(mock_db)).get("hold_reason")


# ═════════════════════════════════════════ F10 · no marker without an address


async def test_a_funding_marker_can_never_exist_without_its_address(
    mock_db, eth, armed, beam_pay, monkeypatch
):
    """The marker was written BEFORE the address was created, so a process that died in between
    left an order parked for a human over a transfer that was never queued."""

    async def boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("the process died between the marker and the address")

    monkeypatch.setattr(beam_pay, "create_wallet", boom)
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    row = await payout(mock_db)
    assert "fund_called_at" not in row
    assert "source_address" not in row
    assert not [w for w in beam_pay.withdrawals if str(w.get("comment", "")).endswith("|fund")]


# ═════════════════════════════════════════ F11 · stranded value is visible


async def test_value_at_a_funded_crossing_is_visible_to_the_float_readers(
    mock_db, eth, armed, beam_pay
):
    row = await funded_crossing(mock_db, beam_pay, eth)
    need = int(row["amount_groth"]) + int(row["relayer_fee_groth"])
    payouts.reset_process_state()
    assert await payouts.queued_crossing_groth(ETH) == need
    parts = await payouts.payout_float(beam_pay, ETH)
    assert parts["crossing"] == need
    assert parts["total"] == parts["regular"] + parts["shielded"]
    assert parts["regular"] >= need  # the treasury's balance fell; this did not vanish with it

    # …and how long it has been sitting there, which is what a watchdog pages on
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"fund_called_at": time.time() - 900}}
    )
    health = await payouts.crossing_health(ETH)
    assert health["orders"] == 1 and 890 <= health["oldest_age_s"] <= 910


async def test_the_admin_overview_carries_the_distributor_and_the_crossing_float(
    mock_db, client, eth, armed, beam_pay, monkeypatch
):
    monkeypatch.setattr(settings, "admin_key", ADMIN_KEY)
    admin.reset_failures()
    row = await funded_crossing(mock_db, beam_pay, eth)
    need = int(row["amount_groth"]) + int(row["relayer_fee_groth"])
    r = await client.get("/admin/overview", headers={"X-Admin-Key": ADMIN_KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "configured" in body["distributor"]  # T34b's handoff: the FULL view, key-protected
    assert body["crossings"]["queued_groth"] == need
    assert body["crossings"]["orders"] == 1


async def test_the_public_health_publishes_crossing_counts_and_never_an_amount(
    mock_db, client, eth, armed, beam_pay
):
    """⛔ COUNTS, NEVER AMOUNTS. /v1/health is unauthenticated (T34b M5): a live read of what the
    treasury holds is an inventory anybody can poll."""
    row = await funded_crossing(mock_db, beam_pay, eth)
    need = int(row["amount_groth"]) + int(row["relayer_fee_groth"])
    body = (await client.get("/v1/health")).json()
    assert body["crossings"]["orders"] == 1
    assert str(need) not in json.dumps(body)


# ═════════════════════════════════════════ F12 · two BEAM coins per crossing


async def test_a_crossing_budgets_two_beam_fee_coins():
    assert payouts.fee_coins_needed({}, payouts.SOURCE_REGULAR) == 2
    # …one once the funding transfer has been made, and one for a shielded crossing, which has
    # no funding leg at all
    assert payouts.fee_coins_needed({"fund_called_at": 1.0}, payouts.SOURCE_REGULAR) == 1
    assert payouts.fee_coins_needed({}, payouts.SOURCE_SHIELDED) == 1


async def test_one_free_beam_coin_does_not_fund_a_crossing_that_needs_two(
    mock_db, eth, armed, beam_pay, beam_wallet
):
    """A crossing spends BEAM twice — BeamPay's fee on the funding transfer, then the wallet's
    on the invocation — and each takes a whole ordinary coin while it is pending."""
    beam_wallet.coin_amounts[(0, "regular")] = [20 * GROTH]  # exactly ONE usable fee coin
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    row = await payout(mock_db)
    assert not [w for w in beam_pay.withdrawals if str(w.get("comment", "")).endswith("|fund")]
    assert sends(beam_wallet) == 0
    assert "no free coin" in str(row["hold_detail"])


# ═════════════════════════════════════════ F13 · the destination re-read


async def test_the_destination_is_read_from_the_endpoint_that_will_answer(mock_db, monkeypatch):
    """publicnode serves the head and then refuses `eth_getCode` at a numeric block ("Archive
    requests require a personal token"), so the re-read raised every 30 s and NOTHING was ever
    released. The fix for an endpoint that will not serve a query is more endpoints (law 8)."""

    class TwoEndpoints:
        urls = ["https://refuses.test", "https://answers.test"]

        def __init__(self) -> None:
            self.asked: list[str] = []

        async def block_number(self, prefer: str | None = None, pin: bool = False) -> int:
            return 20_000

        async def head_from(self) -> tuple[int, str]:
            return 20_000, self.urls[0]

        async def call(
            self, method: str, params: Any, prefer: str | None = None, pin: bool = False
        ) -> Any:
            self.asked.append(str(prefer))
            if prefer == self.urls[0]:
                raise ethpipe.RpcError("Archive requests require a personal token")
            return "0x"

    rpc = TwoEndpoints()
    monkeypatch.setattr(payouts, "get_rpc", lambda: rpc)
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {"_id": "req1", "status": "scheduled", "W": W, "dest_checked_head": 19_000}
    )
    row = await payout(mock_db)
    assert await payouts._dest_still_a_wallet(row) is True
    assert rpc.asked == ["https://refuses.test", "https://answers.test"]
    assert not hasattr(payouts, "dest_code_at_head")  # one reader, in ethpipe


# ═════════════════════════════════════════ the migration note, and the public numbers


async def test_the_skipped_shield_migration_moves_every_row_of_that_shape(mock_db, monkeypatch):
    """Both live rows are the same shape — planned before the policy changed, nothing sent."""
    monkeypatch.setattr(settings, "shield_enabled", False)
    for dep_id in ("45ef74d9" + "0" * 16, "3aaae73d" + "0" * 16):
        await make_deposit(mock_db, dep_id=dep_id)
        await mock_db["pgasme_test"].deposits.update_one(
            {"_id": dep_id}, {"$set": {"treasury": "shielding", "shield_plan": [1_000_000]}}
        )
    assert await payouts.migrate_skipped_shields() == 2
    rows = await mock_db["pgasme_test"].deposits.find({}).to_list(10)
    assert {r["treasury"] for r in rows} == {"claimed"}


async def test_the_public_stats_no_longer_publish_the_float_in_the_clear(mock_db, client):
    """(b) The last float read in the clear. A visitor needs to know the instant path is there
    and well; how much is in the hot wallet is not theirs to poll."""
    body = (await client.get("/v1/stats")).json()
    assert set(body["float"]["ETH"]) == {"configured", "healthy"}
    assert "wei" not in json.dumps(body)


def test_the_env_example_documents_the_instant_confirmation_floor_once():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert text.count("PGAS_INSTANT_CONFIRMATIONS") == 1


# ═════════════════════════════════ a hole F5/F11 made visible: cancel after funding


async def test_a_funded_crossing_cannot_be_cancelled_out_from_under_its_own_money(
    mock_db, eth, armed, beam_pay
):
    """⛔ A REFUND IS THE MIRROR OF A DEBIT THAT LANDED — and once the treasury has moved this
    order's crossing to an address of its own, the money is no longer where a refund would put
    it back from. Cancelling there credits the user AND leaves `fund_groth` at an address the
    row no longer names (a `cancelled` row is not in `crossing_pipeline`, so the float readers
    stop seeing it too): the ledger says we own it, the treasury does not hold it, and nothing
    sweeps it. The order is not lost — it is delayed, it retries, and it delivers.

    Every other refusable order still cancels normally: nothing is funded before the gates pass.
    """
    row = await funded_crossing(mock_db, beam_pay, eth)
    assert row["fund_called_at"] > 0 and row.get("beam_txid") is None
    ok, why = payouts.cancellable(row)
    assert ok is False
    assert "crossing" in why and "address" in why

    fresh = await make_payout(mock_db, rid="req2")
    assert payouts.cancellable(fresh)[0] is True


# ═══════════════════════════ the block the claim landed in, so the user can watch it


async def test_a_claimed_deposit_carries_the_beam_block_its_claim_landed_in(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """T31b item 8, the deposit half. `routers/account.beam_height` has published `claim_height`
    under `beam_height` since the day the reader was written; NOTHING WROTE IT, so every deposit
    answered `null` and the bridge-explorer link was never drawn on the one row a user most wants
    to watch — the crossing that funds their wallet.

    The number is the WALLET's height at the moment the kernel was first seen, exactly as
    `_payout_bridging` records a crossing's: BeamPay's `contract_tx` states confirmations and
    never a block. Late by at most one poll interval, which is why it is a tracking link and
    never evidence."""
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await payouts.process_once()  # → claiming
    await payouts.process_once()  # the claim is signed, submitted and registered
    assert (await deposit(mock_db))["claim_txid"] == "beamtx-1"

    beam_pay.height_ = 4_030_777  # the wallet has moved on since the deposit was made
    await payouts.process_once()  # → claimed
    row = await deposit(mock_db)
    assert row["treasury"] == "claimed"
    assert row["claim_kernel"] == "kernel-beamtx-1"
    assert row["claim_height"] == 4_030_777
    # …and it reaches the user under the ONE name both kinds of row answer on
    assert account.public_deposit(row)["beam_height"] == 4_030_777


async def test_a_height_the_beampay_record_states_itself_beats_the_wallets_approximation(
    beam_pay,
):
    """⛔ THE APPROXIMATION IS THE FALLBACK, NOT THE ANSWER. The wallet's height at kernel time is
    what we can read TODAY; if BeamPay ever states the block the kernel actually settled in, that
    is the truth and the row must carry it instead — with no other edit, which is the whole
    reason the record is asked first.

    ⚠️ Asked of a HAND-WRITTEN record and not of `FakeBeamPay`, deliberately: the fake answers
    the live shape (`api.py:989-1027` — confirmations, never a height) and it must keep doing so,
    or every other test in this suite would be rehearsing against a BeamPay that does not exist."""
    stated = {"txId": "beamtx-1", "booked": True, "status": 3, "height": 4_030_101}
    beam_pay.height_ = 4_030_777  # …and the wallet is 676 blocks further on
    assert payouts.tx_height(stated) == 4_030_101
    assert await payouts.claim_height(beam_pay, stated, "dep1") == 4_030_101


async def test_a_height_that_is_not_a_positive_whole_block_is_never_written_down(beam_pay):
    """0 is a block, and `?tx=0` is a link to somebody else's bridge traffic shown to the user as
    their own. A record that states a shape which is not a height states none, and the wallet's
    own height answers instead. Every shape below has reached this codebase on a real row."""
    for bad in (None, 0, -1, "", "later", float("nan"), float("inf"), True, [4_030_101]):
        assert payouts.tx_height({"height": bad}) is None, bad
    assert payouts.tx_height({"height": "4030101"}) == 4_030_101  # Mongo hands back str
    assert payouts.tx_height({"confirmations": 61}) is None  # the live shape: no height at all

    beam_pay.height_ = 4_030_777
    assert await payouts.claim_height(beam_pay, {"height": 0}, "dep1") == 4_030_777
    beam_pay.height_ = 0  # a wallet that answers no height at all is not a height of 0
    assert await payouts.claim_height(beam_pay, {}, "dep1") is None


async def test_an_unreadable_height_never_holds_a_claim_that_settled(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ A TRACKING LINK MAY NEVER STOP THE MONEY. `_payout_bridging` lets an unreadable height
    raise because maturity is COUNTED from it there; here it decorates a row that has already
    been claimed, booked and attributed. An unreadable one is logged and the deposit advances
    without it — the client draws no link, which is the honest state and not a bug."""
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await payouts.process_once()
    await payouts.process_once()
    beam_pay.raise_on.add("/wallet_status")
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claimed"  # ⛔ the claim is NOT held on a link
    assert "claim_height" not in row
    assert account.public_deposit(row)["beam_height"] is None
