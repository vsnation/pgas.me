"""T52 — accept only what the treasury can deliver, say when, and speak the user's language.

Admin, 2026-09-10 15:36–15:45Z, reading his own orders: *"again issues no one can understand:
WAITING: the wallet can spend 0.00775651 regular / 0 shielded ETH now; 0.01652864 is maturing
(max-privacy lock…) … no free coin: ETH spendable coins 4 … BEAM fee coins 1 and this crossing
needs 2 … `python -m pgasme.beam split --asset BEAM`"* and *"Why do you accept user request if
user cannot spend this?"* → *"Next time when user wants to withdraw just tell him he can't"*.

The live facts this file pins (15:36Z):

  * the wallet could spend **775,651 groth** of bETH (4 regular coins) and **0** shielded;
  * **1,652,864 groth** was MATURING in three max-privacy outputs shielded the night before,
    unlocking 2026-09-12 23:21Z and 09-13 00:27Z, with no early exit;
  * the admin's 0.01 ETH order needed **1,007,925 groth** (the delivery plus the crossing it
    funded) and was ACCEPTED anyway, because acceptance looked at the USER's ledger balance and
    never at the treasury's spendable float.

So:

  1. ONE reader of the float schedule (`payouts.float_schedule`) — the release gate and the
     request path ask it the same question and get the same answer (law 9).
  2. `POST /v1/withdrawals` refuses what the treasury cannot deliver NOW, with one plain
     sentence naming the deliverable amount and the next unlock. No accept-anyway.
  3. Two texts per hold, one writer (`payouts.hold_texts`): the user's row says what is
     happening in words, the operator's digest keeps the numbers. A user never reads a coin
     count, a wallet bucket or a shell command.
  4. A float-short delayed row's ETA is the unlock, never a time already past.
  6. The schedule-time destination check reads the code the way the release does — every
     endpoint, each at its own head — and refuses only when NONE of them answers.
  7. Operator sentences carry units; user sentences carry no numbers at all.

And the 17:58Z addition: once a crossing is FUNDED the row is polled every pass. Order 64ee
funded its address and then waited an hour for its own invocation, because the delay ladder's
rung was taken from the gate refusals it had collected before the funding.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import time
from typing import Any

import pytest
from conftest import fund
from eth_account import Account as EthAccount
from test_beam_payout import (
    ETH,
    GROTH,
    MP,
    TREASURY,
    FakeBeamPay,
    FakeWalletApi,
    make_payout,
    payout,
)
from test_log_redaction import telegram  # noqa: F401 — the ONE live-tg fixture in the suite

from pgasme import beam, beampay, ethpipe, payouts, tg
from pgasme.assets import get_asset
from pgasme.config import settings
from pgasme.routers import account as account_router
from pgasme.routers import withdrawals as w

# ── the live numbers, 2026-09-10 15:36Z ───────────────────────────────────────────────────────
SPENDABLE = 775_651  # what the wallet could spend, regular, in groth
MATURING = 1_652_864  # locked in three max-privacy outputs
UNLOCK = dt.datetime(2026, 9, 12, 23, 21, 0, tzinfo=dt.UTC).timestamp()
UNLOCK_2 = dt.datetime(2026, 9, 13, 0, 27, 0, tzinfo=dt.UTC).timestamp()
UNLOCK_WORDS = "Sat 12 Sep, 23:21Z"
RELAYER = 6_340  # the crossing's own fee at that minute…
BRIDGE = math.ceil(RELAYER * w.headroom_for(0))  # …7,925 groth, the headroom floor included
ASKED = 1_000_000  # the admin's 0.01 ETH
NEED = ASKED + BRIDGE  # 1,007,925 — what the treasury has to be able to move
DELIVERABLE = SPENDABLE - BRIDGE  # 767,726 — the largest order it CAN deliver right now


def _w() -> str:
    return EthAccount.create().address


def _body(*amounts: int, mode: str = "direct") -> dict[str, Any]:
    return {"asset": "ETH", "items": [{"W": _w(), "amount_groth": a} for a in amounts], "mode": mode}


# ── the fakes: a wallet that can spend a little and is waiting on a max-privacy lock ──────────


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    bp = FakeBeamPay()
    beampay.reset_health()
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)  # BEAM for fees
    bp.fund(TREASURY, ETH.aid, SPENDABLE + MATURING)  # the ledger owns it all…
    # …and the WALLET can spend only the unshielded part: the three shield chunks settled last
    # night and are inside the max-privacy lock (the fact BeamPay's per-address ledger cannot
    # express, and the whole reason `wallet_spendable` exists).
    bp.wallet_totals[ETH.aid] = {
        "available": SPENDABLE,
        "available_regular": SPENDABLE,
        "available_mp": 0,
        "maturing_mp": MATURING,
    }
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    yield bp
    beampay.set_beampay(None)


@pytest.fixture(autouse=True)
def beam_wallet(monkeypatch: pytest.MonkeyPatch, beam_pay: FakeBeamPay) -> FakeWalletApi:
    wallet = FakeWalletApi(beam_pay)
    beam.set_wallet(wallet)
    payouts.reset_archive_pin()
    monkeypatch.setattr(settings, "beam_shader", wallet.shader)
    yield wallet
    beam.set_wallet(None)


@pytest.fixture(autouse=True)
def pinned_fee(monkeypatch: pytest.MonkeyPatch) -> None:
    """The crossing's price, pinned to the minute the admin hit this. ONE reader is patched, so
    the preview, the create and the release gate all quote the same 6,340 groth."""

    async def fee(asset: Any, rpc: Any) -> tuple[int, int, dict[str, Any]]:
        return RELAYER, 1, {"gas_gwei": 0.35}

    monkeypatch.setattr(payouts, "relayer_fee_for", fee)
    w.clear_fees_cache()


@pytest.fixture
async def shielded_last_night(mock_db: Any) -> None:
    """The three max-privacy chunks of 2026-09-09 23:2xZ, on the deposit row that made them."""
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "dep-shield",
            "account_id": "acct1",
            "asset": "ETH",
            "status": "credited",
            "treasury": "shielded",
            "value_groth": MATURING,
            "shield_plan": [1_000_000, 600_000, 52_864],
            "shield_txids": ["s0", "s1", "s2"],
            "shield_calls": [
                {"at": UNLOCK - payouts.MAX_PRIVACY_LOCK_S, "to_address": MP},
                {"at": UNLOCK - payouts.MAX_PRIVACY_LOCK_S, "to_address": MP},
                {"at": UNLOCK_2 - payouts.MAX_PRIVACY_LOCK_S, "to_address": MP},
            ],
            "created_at": time.time() - 86_400,
            "updated_at": time.time() - 86_400,
        }
    )


# ═══════════════════════════════════════════ 1. one float schedule reader ═════════════════════


async def test_the_float_schedule_is_one_reader_of_what_the_treasury_can_move(
    mock_db, shielded_last_night
):
    """What can be delivered now, what is locked, and WHEN it stops being locked."""
    got = await payouts.float_schedule(ETH)
    assert got is not None
    assert got["spendable_now_groth"] == SPENDABLE
    assert got["sources"] == {"regular": SPENDABLE, "shielded": 0}
    assert got["maturing_groth"] == MATURING
    assert [m["unlocks_at"] for m in got["maturing"]] == [UNLOCK, UNLOCK, UNLOCK_2]
    assert got["next_unlock_at"] == UNLOCK
    assert got["pipeline_groth"] == 0


async def test_what_is_already_promised_is_not_spendable_twice(mock_db, shielded_last_night):
    """An order accepted a minute ago has a claim on this float; the next preview must see it."""
    await make_payout(mock_db, amount=500_000, rid="earlier", bridge_fee_groth=BRIDGE)
    got = await payouts.float_schedule(ETH)
    assert got["pipeline_groth"] == 500_000 + BRIDGE
    assert got["spendable_now_groth"] == SPENDABLE - 500_000 - BRIDGE


async def test_an_unreadable_wallet_is_not_an_empty_treasury(mock_db, beam_pay):
    """Law 8: we cannot claim it can deliver, and we must not claim it cannot. `None` is neither."""
    beam_pay.raise_on.add("/wallet_status")
    assert await payouts.float_schedule(ETH) is None


# ═══════════════════════════════════ 2. the refusal, on the live numbers ══════════════════════

SENTENCE = (
    f"Withdrawals of up to 0.00767726 ETH can be delivered right now. The rest of the "
    f"treasury's funds unlock on {UNLOCK_WORDS} — ask for a smaller amount now, or come back "
    f"then."
)


async def test_the_preview_marks_the_row_the_treasury_cannot_deliver(
    client, user, mock_db, shielded_last_night, monkeypatch
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 10 * GROTH)  # the USER has the money; the treasury cannot move it
    r = await client.post("/v1/withdrawals/preview", json=_body(ASKED), headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    item = body["items"][0]
    assert item["ok"] is False and item["problem_code"] == "treasury_float"
    assert item["problem"] == SENTENCE
    assert body["treasury"] == {
        "ok": False,
        "float_now_groth": SPENDABLE,
        "deliverable_now_groth": DELIVERABLE,
        "next_unlock_at": UNLOCK,
        "problem": SENTENCE,
    }
    # …and the batch rule is untouched: the USER's balance covers this order in full
    assert body["batch"]["ok"] is True


async def test_create_refuses_the_order_the_treasury_cannot_deliver_and_writes_nothing(
    client, user, mock_db, shielded_last_night, monkeypatch
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 10 * GROTH)
    before = await mock_db["pgasme_test"].ledger.count_documents({})
    r = await client.post("/v1/withdrawals", json=_body(ASKED), headers=user["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == SENTENCE
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0
    assert await mock_db["pgasme_test"].ledger.count_documents({}) == before


async def test_the_order_the_treasury_can_deliver_is_accepted(
    client, user, mock_db, shielded_last_night, monkeypatch
):
    """The sentence is actionable: ask for exactly what it names and the order goes through."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 10 * GROTH)
    r = await client.post("/v1/withdrawals", json=_body(DELIVERABLE), headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["delivered_groth"] == DELIVERABLE


async def test_a_batch_whose_first_row_fits_and_second_does_not(
    client, user, mock_db, shielded_last_night, monkeypatch
):
    """In row order, cumulative: the float the first row takes is not there for the second."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 10 * GROTH)
    r = await client.post(
        "/v1/withdrawals/preview", json=_body(500_000, 500_000), headers=user["headers"]
    )
    items = r.json()["items"]
    assert items[0]["ok"] is True and "problem_code" not in items[0]
    assert items[1]["problem_code"] == "treasury_float"
    # …and the second row's sentence names what is left AFTER the first, not the whole float
    left = SPENDABLE - 500_000 - BRIDGE
    assert w.fmt_units(max(0, left - BRIDGE), get_asset("ETH")) in items[1]["problem"]


async def test_nothing_maturing_says_try_again_later_and_never_invents_a_date(
    client, user, mock_db, monkeypatch, beam_pay
):
    beam_pay.wallet_totals[ETH.aid] = {
        "available": SPENDABLE,
        "available_regular": SPENDABLE,
        "available_mp": 0,
        "maturing_mp": 0,
    }
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 10 * GROTH)
    r = await client.post("/v1/withdrawals", json=_body(ASKED), headers=user["headers"])
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Withdrawals of up to 0.00767726 ETH can be delivered right now — ask for a smaller "
        "amount now, or try again later."
    )


async def test_an_unmeasurable_float_is_not_a_refusal(
    client, user, mock_db, monkeypatch, beam_pay
):
    """"We could not look" is not "it cannot pay" (law 8) — the release gate holds it honestly."""
    beam_pay.raise_on.add("/wallet_status")
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 10 * GROTH)
    r = await client.post("/v1/withdrawals", json=_body(ASKED), headers=user["headers"])
    assert r.status_code == 200, r.text


# ═════════════════════════════ 3 & 7. two texts per hold, one writer ══════════════════════════


def test_every_hold_code_has_a_user_text_and_an_operator_text():
    for code in payouts.HOLD_CODES:
        user, operator = payouts.hold_texts(code, {"operator": "the numbers, for an operator"})
        assert user and operator, code


# a `payout-*` key that is a PAGER key and never a row's hold: nothing carries it on a row, so
# it has no user to speak to. Named here so the scan below stays a scan and not a wish.
NOT_A_HOLD = {"payout-lease"}


def test_a_hold_code_the_registry_does_not_know_is_a_defect_not_a_default():
    """Exhaustiveness: a new refusal key with no user text fails HERE, not on a user's page.

    The scan is over the SOURCE because that is where a new refusal is written — a registry that
    only knows the codes somebody remembered to register is the same defect one step later."""
    import inspect

    src = inspect.getsource(payouts)
    used = {m.group(1) for m in re.finditer(r'"(payout-[a-z-]+)[:"]', src)}
    used |= {m.group(1) for m in re.finditer(r'"(beam-fee)[:"]', src)}
    assert len(used) > 20, f"the scan found only {sorted(used)} — it has stopped testing anything"
    assert used - NOT_A_HOLD <= set(payouts.HOLD_CODES), sorted(
        used - NOT_A_HOLD - set(payouts.HOLD_CODES)
    )


def test_the_user_never_reads_a_number_a_coin_or_a_command():
    """Item 7: the USER text carries no bare numbers, no wallet buckets, no shell."""
    for code in payouts.HOLD_CODES:
        user, _op = payouts.hold_texts(code, {})
        # ⛔ NO AMOUNTS. "checking every 5 minutes" is a plain interval and is exactly the kind
        # of thing a user CAN act on; what the admin read and could not act on were money-shaped
        # numbers — "0.00775651", "1652864", "4 coins".
        assert not re.search(r"\d[.,]\d|\d{3,}", user), (code, user)
        assert "`" not in user and "python -m" not in user, (code, user)
        # the operator's vocabulary, every word of which the admin quoted back at us
        assert not re.search(
            r"\b(groth|utxo|shielded|regular|maturing|spendable|relayer|subsidy|PGAS_[A-Z_]+)\b",
            user,
        ), (code, user)


async def test_a_short_float_writes_the_user_sentence_and_the_operator_detail(
    mock_db, shielded_last_night, monkeypatch
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "claim_enabled", True)
    # the admin's own order: the LEDGER owns enough (0.02428515) and the WALLET can move only
    # 0.00775651 of it — every gate above this one passes on a number that is true and irrelevant
    await make_payout(mock_db, amount=ASKED, rid="big", bridge_fee_groth=BRIDGE)
    await payouts.payouts_once()
    row = await payout(mock_db, "big")
    assert row["hold_reason"] == (
        f"Waiting for treasury funds — expected by {UNLOCK_WORDS} at the latest (earlier if "
        f"deposits come in)"
    )
    assert "ETH" in row["hold_detail"] and tg.fmt_groth(SPENDABLE) in row["hold_detail"]
    assert row["ready_at"] == UNLOCK and row["ready_reason"] == "treasury_float"


async def test_the_operator_detail_never_reaches_the_users_page(mock_db):
    row = {
        "_id": "r1",
        "status": "delayed",
        "asset": "ETH",
        "hold_reason": "Waiting for treasury funds",
        "hold_detail": "the wallet can spend 0.00775651 ETH; 0.01652864 is maturing",
    }
    public = account_router.public_request(row)
    assert "hold_detail" not in public
    assert public["hold_reason"] == "Waiting for treasury funds"


def test_the_waiting_digest_leads_with_the_order_id():
    line = payouts.hold_digest(
        {"coll": "payout_requests", "reason": "the float is short", "ids": ["64ee5540aabbccdd"]}
    )
    assert line.startswith(f"payout {tg.short('64ee5540aabbccdd')} — WAITING:")
    many = payouts.hold_digest(
        {"coll": "payout_requests", "reason": "the float is short", "ids": ["a" * 24, "b" * 24]}
    )
    assert many.startswith(f"payout {tg.short('a' * 24)} +1 more — WAITING (2 payouts):")


async def test_a_row_that_stops_waiting_is_back_in_the_queue_not_released(mock_db, telegram):  # noqa: F811
    payouts._PASS["unheld"] = {"payout_requests": ["r1", "r2"]}
    await payouts.flush_hold_digest()
    sent = [r.content.decode() for r in telegram]
    assert any("Back in the queue: 2 payout(s) (delayed → retrying)" in m for m in sent), sent
    assert not any("RELEASED" in m for m in sent)


# ═══════════════════════════════ 4. the ETA of a float-short order ════════════════════════════


def test_eta_for_a_float_short_delayed_row_is_the_unlock_it_was_promised():
    now = time.time()
    at, tail, note = payouts.eta_for(
        {
            "status": "delayed",
            "hold_code": "payout-wallet",
            "hold_reason": "Waiting for treasury funds — expected by Sat 12 Sep, 23:21Z",
            "ready_at": now + 3600,
            "ready_reason": "treasury_float",
            "next_attempt_at": now + 60,
        }
    )
    assert at == now + 3600 and tail > 0
    assert note == "Waiting for treasury funds — expected by Sat 12 Sep, 23:21Z"


def test_a_ready_at_that_has_passed_is_never_published_as_an_eta():
    now = time.time()
    at, _tail, _note = payouts.eta_for(
        {
            "status": "delayed",
            "hold_code": "payout-wallet",
            "hold_reason": "Waiting for treasury funds",
            "ready_at": now - 3600,
            "ready_reason": "treasury_float",
            "next_attempt_at": now + 60,
        }
    )
    assert at is not None and at > now


# ════════════════════════ 6. the destination check reads the way the release does ═════════════


async def test_one_endpoint_that_will_not_serve_getcode_does_not_refuse_the_order(
    client, user, mock_db, monkeypatch, rpc
):
    """T37d's fix, on the schedule path: the fix for an endpoint that will not answer is MORE
    endpoints, never a lower bar."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    rpc.urls = ["https://archive-refuses.test", "https://answers.test"]
    rpc.heads = {"https://archive-refuses.test": 2000, "https://answers.test": 2000}
    original = rpc.call

    async def call(method, params, prefer=None, pin=False):
        if method == "eth_getCode" and prefer == "https://archive-refuses.test":
            raise ethpipe.RpcError("Archive requests require a personal token")
        return await original(method, params, prefer=prefer, pin=pin)

    monkeypatch.setattr(rpc, "call", call)
    await fund(user, "ETH", 10 * GROTH)
    r = await client.post("/v1/withdrawals", json=_body(100_000), headers=user["headers"])
    assert r.status_code == 200, r.text
    row = await mock_db["pgasme_test"].payout_requests.find_one({})
    assert row["dest_checked_head"] == 2000


async def test_no_endpoint_answering_is_one_plain_sentence_and_nothing_written(
    client, user, mock_db, monkeypatch, rpc
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    rpc.head_dead = True
    await fund(user, "ETH", 10 * GROTH)
    r = await client.post("/v1/withdrawals", json=_body(100_000), headers=user["headers"])
    assert r.status_code == 503
    assert r.json()["detail"] == (
        "We could not check the destination address right now — nothing was scheduled; try "
        "again in a moment."
    )
    assert "http" not in r.json()["detail"]
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0


async def test_a_contract_destination_is_still_refused(client, user, mock_db, monkeypatch, rpc):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 10 * GROTH)
    dest = _w()
    rpc.code[dest.lower()] = "0x60006000"
    r = await client.post(
        "/v1/withdrawals",
        json={"asset": "ETH", "items": [{"W": dest, "amount_groth": 100_000}], "mode": "direct"},
        headers=user["headers"],
    )
    assert r.status_code == 400 and "is a contract, not a wallet" in r.json()["detail"]


# ══════════════════════ the 17:58Z addition: a funded crossing is polled every pass ═══════════


async def test_a_funded_crossing_is_not_put_on_the_delay_ladder(mock_db):
    row = await make_payout(mock_db, rid="64ee", fund_called_at=time.time() - 60, delays=6)
    await payouts._delay(row, "scheduled", "the funding transfer has not settled yet")
    got = await payout(mock_db, "64ee")
    assert got["delays"] == 7
    assert got["next_attempt_at"] <= time.time() + 1  # the next pass, not an hour from now


async def test_a_funded_crossing_is_picked_up_on_the_next_pass(mock_db, monkeypatch):
    """Order 64ee5540…, 17:58Z: funded, settled, and then an hour of nothing — the rung came from
    the gate refusals it collected BEFORE the funding, and `_payout_delayed` obeyed it."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "hold_backoff_s", 300.0)  # the cooldown that parked it
    now = time.time()
    await make_payout(
        mock_db,
        rid="64ee",
        status=payouts.DELAYED,
        deliver_at=now - 1800,  # an ASAP order, already past its window
        fund_called_at=now - 600,
        source_address="7c" + "5e" * 31,
        delays=7,
        next_attempt_at=now + 3600,
        delayed_since=now - 600,
        hold_at=now,
    )
    assert await payouts.payouts_once() == 1  # the hold cooldown does not park a funded row
    got = await payout(mock_db, "64ee")
    assert got["delays"] == 8 and got["next_attempt_at"] <= time.time() + 1
    assert got["hold_reason"] == "Funds are on their way to the bridge — usually a few minutes"


def test_the_funded_wait_says_the_money_is_on_its_way():
    user, _op = payouts.hold_texts("payout-funding", {"operator": "x"})
    assert user == "Funds are on their way to the bridge — usually a few minutes"
