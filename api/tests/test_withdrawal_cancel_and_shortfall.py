"""T35b — the three findings the T35/T40 skeptic left on the withdrawals router.

H1  `cancel` matched `{status: "scheduled"}` and never asked `payouts.cancellable`, while
    `payouts.CANCELLABLE` is `(scheduled, delayed, held)` and `/v1/account` publishes
    `cancellable: true` for delayed and held rows. Two sentences for one fact (law 9): the
    button the client drew and the answer the route gave disagreed. ONE reader decides, and the
    atomic claim asks the SAME question so a row that advanced between the read and the update
    is refused rather than refunded twice.

M1  the 409's shortfall UNDER-stated: it was Σ(what the batch debits AT TODAY'S balance) minus
    Available, and topping up by exactly that flips a from-amount row to on-top, which costs
    MORE — so the user credited the published number and was refused again. The published
    number is now the FIXED POINT: top it up and every wallet receives the full amount it asked
    for.

L1  a from-amount order could deliver 1 groth against a 22,502-groth debit. A delivery that is
    mostly fees is not a withdrawal anyone asked for; the row is refused with the assumption
    stated (`fees_dominate`) and both ways out named.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

from conftest import fund
from eth_account import Account as EthAccount

from pgasme import ledger, payouts
from pgasme.assets import get_asset
from pgasme.config import settings
from pgasme.routers import withdrawals as w

ETH = 100_000_000  # groth
RELAYER = 18_000  # FakeRpc answers 1 gwei → an 18,000-groth b2e relayer fee
BRIDGE = math.ceil(RELAYER * w.headroom_for(0))  # the ASAP crossing, headroom floor included
TOTAL_1M = 1_000_000 + 20_000 + BRIDGE  # one 0.01 ETH payout with the fees ON TOP


def _w() -> str:
    return EthAccount.create().address


def _items(*specs) -> list[dict]:
    return [{"W": _w(), "amount_groth": a} for a in specs]


def _body(items, mode="direct", asset="ETH") -> dict:
    return {"asset": asset, "items": items, "mode": mode}


async def _schedule(client, user, amount: int = 1_000_000) -> str:
    r = await client.post("/v1/withdrawals", json=_body(_items(amount)), headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()["request_ids"][0]


async def _set(mock_db, rid: str, **fields: Any) -> dict[str, Any]:
    d = mock_db["pgasme_test"]
    await d.payout_requests.update_one({"_id": rid}, {"$set": fields})
    return await d.payout_requests.find_one({"_id": rid})


# ═════════════════════════════════════════════ H1 · ONE READER DECIDES WHO MAY CANCEL


async def test_a_delayed_order_can_be_cancelled_and_is_refunded_in_full(
    client, user, mock_db, monkeypatch
):
    """`payouts.CANCELLABLE` includes `delayed`, `/v1/account` publishes `cancellable: true` for
    it, and the route used to answer 409. The money is still reserved: it comes back."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = await _schedule(client, user)
    await _set(mock_db, rid, status=payouts.DELAYED, hold_reason="the relayer fee spiked")

    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"cancelled": rid, "refunded_groth": TOTAL_1M}
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    row = await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid})
    assert row["status"] == "cancelled"


async def test_a_held_order_can_be_cancelled_too(client, user, mock_db, monkeypatch):
    """`held` is an OPERATOR state, not a terminal one — to the user it reads as delayed, and
    refusing their refund because we parked their row would be the product keeping their money."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = await _schedule(client, user)
    await _set(mock_db, rid, status=payouts.HELD, hold_reason="an operator is looking at this")

    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["refunded_groth"] == TOTAL_1M
    assert (await ledger.balance(user["account_id"], "ETH"))["available"] == ETH


async def test_the_refusal_is_the_readers_own_sentence(client, user, mock_db, monkeypatch):
    """The 409 detail is `payouts.cancellable(row)[1]` VERBATIM — never a sentence composed in
    the router, which is how the two drifted apart in the first place."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 4 * ETH)
    h = user["headers"]
    for fields in (
        {"status": "releasing"},
        {"status": "bridging"},
        {"status": payouts.DELAYED, "beam_txid": "beamtx-1"},
        {"status": payouts.HELD, "kernel_at": time.time()},
        {"status": payouts.DELAYED, "instant_tx": "0x" + "ab" * 32},
    ):
        rid = await _schedule(client, user)
        row = await _set(mock_db, rid, **fields)
        may, why = payouts.cancellable(row)
        assert not may
        r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=h)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == why
        # …and nothing was given back
        assert (await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid}))[
            "status"
        ] == fields["status"]


async def test_the_atomic_claim_asks_exactly_what_the_reader_asks(mock_db):
    """Law 9 again, one level down: the Mongo filter the flip uses and `payouts.cancellable`
    are two statements of one rule, so they are checked against each other on a table of rows —
    a filter that is LAXER refunds an order that has left, a filter that is STRICTER refuses a
    user their own money."""
    d = mock_db["pgasme_test"]
    rows: list[dict[str, Any]] = []
    for n, extra in enumerate(
        [
            {},
            {"beam_txid": "beamtx-1"},
            {"beam_txid": None},
            {"beam_txid": ""},
            {"kernel_at": 1.0},
            {"kernel_at": 0},
            {"instant_tx": "0x" + "ab" * 32},
            {"instant_tx": ""},
        ]
    ):
        for status in (
            "scheduled",
            payouts.DELAYED,
            payouts.HELD,
            "releasing",
            "bridging",
            "delivering",
            "sent",
            "cancelled",
            payouts.PAYING,
        ):
            rows.append({"_id": f"r{len(rows)}-{n}", "status": status, **extra})
    await d.payout_requests.insert_many([dict(r) for r in rows])

    matched = {
        r["_id"]
        for r in await d.payout_requests.find(w.cancel_claim()).to_list(len(rows) + 1)
    }
    for r in rows:
        assert payouts.cancellable(r)[0] is (r["_id"] in matched), r


async def test_a_row_that_advances_between_the_read_and_the_claim_is_refused(
    client, user, mock_db, monkeypatch
):
    """THE RACE THIS FIX EXISTS TO CLOSE. The read says the order may be cancelled; by the time
    the flip runs the processor has released it. The claim is conditional on the same facts, so
    it matches nothing — and the answer is a refusal, NOT a refund of money that has left.

    The race is injected where it really happens: `cancellable` is made to answer the way it
    would have answered a moment earlier, while the row in the database has already moved on."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = await _schedule(client, user)
    await _set(mock_db, rid, status="bridging", beam_txid="beamtx-9")

    real = payouts.cancellable
    calls: list[int] = []

    def stale_then_true(row: dict[str, Any]) -> tuple[bool, str]:
        calls.append(1)
        return (True, "") if len(calls) == 1 else real(row)

    monkeypatch.setattr(payouts, "cancellable", stale_then_true)
    before = await ledger.balance(user["account_id"], "ETH")
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == real(
        await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid})
    )[1]
    assert await ledger.balance(user["account_id"], "ETH") == before  # not a groth moved
    assert (await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid}))["status"] == "bridging"


async def test_the_refusal_writes_a_row_that_names_what_blocked_it(
    client, user, mock_db, monkeypatch, caplog
):
    """Law 12: every decision path writes a row — and §9.7: the row carries the status and the
    FIELD that blocked it, never the txid and never the destination. "not cancellable:
    'delayed'" read as a contradiction to the operator, because `delayed` IS cancellable."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 2 * ETH)
    rid = await _schedule(client, user)
    await _set(mock_db, rid, status=payouts.DELAYED, beam_txid="beamtx-secret-1")
    with caplog.at_level("INFO", logger="pgasme.withdrawals"):
        assert (
            await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
        ).status_code == 409
    lines = [r.getMessage() for r in caplog.records if r.name == "pgasme.withdrawals"]
    assert any("status 'delayed', blocked by beam_txid" in x for x in lines), lines
    assert not any("beamtx-secret-1" in x for x in lines)  # names of fields, never their values


async def test_a_delayed_from_amount_order_gives_back_exactly_what_it_took(
    client, user, mock_db, monkeypatch
):
    """A refund is the mirror of the debit that landed, read from the ENTRIES — so a
    from-amount order that debited only the amount gets only the amount back, whatever status
    it was cancelled from."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    rid = await _schedule(client, user, 1_000_000)  # the fees cannot ride on top
    row = await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid})
    assert row["fee_mode"] == "from_amount" and row["debited_groth"] == 1_000_000
    await _set(mock_db, rid, status=payouts.DELAYED)

    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"cancelled": rid, "refunded_groth": 1_000_000}
    assert (await ledger.balance(user["account_id"], "ETH"))["available"] == 1_000_000


async def test_the_unfinished_cancel_repair_still_works(client, user, mock_db, monkeypatch):
    """A `cancelled` row whose refund never landed is finished by the next call — the doctrine
    the route already had, kept while the gate around it changed."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = await _schedule(client, user)
    # the flip landed and the refund did not
    await _set(mock_db, rid, status="cancelled", cancelled_at=time.time())
    assert await ledger.find_entry("cancel", rid) is None
    assert payouts.cancellable(
        await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid})
    )[0] is False

    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"cancelled": rid, "refunded_groth": TOTAL_1M}
    # …and once it IS finished, the next call is a plain refusal again
    r2 = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert r2.status_code == 409 and "cancelled" in r2.json()["detail"]


# ═════════════════════════════════════════════ M1 · THE SHORTFALL IS A NUMBER THAT WORKS


async def test_the_published_shortfall_is_enough_to_make_the_batch_go_through(
    client, user, mock_db, monkeypatch
):
    """The skeptic's own example. Available 1,000,000; rows of 1,000,000 and 100,000.

    The old number was Σ(debits at TODAY'S balance) − Available, and today's balance prices row
    one FROM ITS AMOUNT. Credit exactly that and row one flips to on-top, which costs 20,000 +
    the bridge fee more than it did — so the batch is refused a second time, by a number we
    published ourselves. The fixed point is what is published now."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    h = user["headers"]
    body = _body(_items(1_000_000, 100_000))

    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 409, r.text
    short = int(r.headers["X-Shortfall-Groth"])
    assert f"Top up {w.fmt_units(short, get_asset('ETH'))} " in r.json()["detail"]  # T45: in ETH
    # what the OLD code published: Σ(debits at today's balance) − Available. Row one is priced
    # from its amount at this balance (1,000,000) and row two is quoted on top (124,500).
    naive = (1_000_000 + (100_000 + 2_000 + BRIDGE)) - 1_000_000
    assert short > naive  # …and the old number was not enough
    assert short == TOTAL_1M + (100_000 + 2_000 + BRIDGE) - 1_000_000  # Σ on-top − Available
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0

    # …credit EXACTLY the number we published, and the batch goes through with both wallets
    # receiving the full amount they asked for
    await fund(user, "ETH", short)
    ok = await client.post("/v1/withdrawals", json=body, headers=h)
    assert ok.status_code == 200, ok.text
    out = ok.json()
    assert [i["fee_mode"] for i in out["items"]] == ["on_top", "on_top"]
    assert [i["delivered_groth"] for i in out["items"]] == [1_000_000, 100_000]
    assert (await ledger.balance(user["account_id"], "ETH"))["available"] == 0


async def test_the_preview_publishes_the_same_shortfall_as_the_refusal(
    client, user, monkeypatch
):
    """One function, one number, both ends of the request (law 9)."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    h = user["headers"]
    body = _body(_items(1_000_000, 100_000))
    quote = (await client.post("/v1/withdrawals/preview", json=body, headers=h)).json()
    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 409
    assert quote["batch"]["ok"] is False
    assert str(quote["batch"]["shortfall_groth"]) == r.headers["X-Shortfall-Groth"]
    assert quote["batch"]["problem"] == r.json()["detail"]
    # the batch as typed still reports what IT would debit — the shortfall is the top-up, and
    # the sentence says which is which
    assert quote["batch"]["need_groth"] == quote["totals"]["total_debited_groth"]
    assert "fees are charged on top once your balance covers them" in quote["batch"]["problem"]


async def test_the_shortfall_is_never_smaller_than_the_gap_it_replaced(
    client, user, monkeypatch
):
    """The property, over random batches: whatever `top_up_needed` answers, crediting it is
    ENOUGH — the batch is then accepted and every row rides on top — and it is never less than
    the naive "Σ debits − Available" the old code published.

    (`top_up_needed` is a pure "what would it take" number and is positive for a batch that
    already fits with a from-amount row in it; `batch_verdict` is what publishes 0 when the
    batch as typed is affordable, and that is asserted where the route is exercised.)"""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    rng = random.Random(20260910)
    asset = get_asset("ETH")
    for _ in range(200):
        amounts = [rng.randint(50_000, 3_000_000) for _ in range(rng.randint(1, 4))]
        available = rng.randint(0, sum(amounts) + 3 * BRIDGE)
        items = [w.Item(W=_w(), amount_groth=a) for a in amounts]
        now = time.time()
        priced = w.price_items(
            items, asset=asset, mode="direct", now=now, relayer_fee_groth=RELAYER,
            available=available,
        )
        need = w.totals_of(priced)["total_debited_groth"]
        top = w.top_up_needed(
            items, priced, asset=asset, mode="direct", now=now,
            relayer_fee_groth=RELAYER, available=available,
        )
        assert top >= max(0, need - available)  # never under-states the gap it replaced
        after = w.price_items(
            items, asset=asset, mode="direct", now=now, relayer_fee_groth=RELAYER,
            available=available + top,
        )
        assert all(i["fee_mode"] == w.ON_TOP for i in after)
        assert all(i["ok"] for i in after)
        assert w.totals_of(after)["total_debited_groth"] <= available + top
        # …and the verdict publishes 0 for a batch that already fits as typed
        verdict = w.batch_verdict(w.totals_of(priced), "ETH", available, shortfall=top)
        assert verdict["ok"] is (need <= available)
        assert verdict["shortfall_groth"] == (0 if need <= available else top)


# ═════════════════════════════════════════════ L1 · A DELIVERY THAT IS MOSTLY FEES


async def test_a_row_whose_fees_would_eat_more_than_half_is_refused(
    client, user, mock_db, monkeypatch
):
    """The assumption, stated in the refusal: a withdrawal delivers most of what was asked for.

    "withdrawal can be any" (admin) is about the FLOOR, not about handing someone 1 groth for a
    22,502-groth debit. Both ways out are named, and both of them work."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    asset = get_asset("ETH")
    floor = w.min_amount_groth(asset)
    least = w.smallest_undominated(BRIDGE, asset=asset, floor=floor)
    await fund(user, "ETH", least - 1)
    h = user["headers"]

    amount = least - 1  # the whole balance, and the fees would take more than half of it
    r = await client.post("/v1/withdrawals", json=_body(_items(amount)), headers=h)
    assert r.status_code == 422, r.text
    item = r.json()["detail"]["items"][0]
    assert item["ok"] is False and item["problem_code"] == "fees_dominate"
    assert item["fee_mode"] == "from_amount"
    assert 2 * item["delivered_groth"] < amount
    need = amount + w.fee_for(amount) + BRIDGE
    eth = get_asset("ETH")
    assert f"more than half of {w.fmt_units(amount, eth)}" in item["problem"]
    assert f"at least {w.fmt_units(need, eth)} of balance" in item["problem"]
    assert f"at least {w.fmt_units(least, eth)}" in item["problem"]
    assert "groth" not in item["problem"]  # T45 item 5
    # every row this API returns still adds up, refused or not
    assert item["delivered_groth"] + item["fee_groth"] + item["bridge_fee_groth"] == item["debited_groth"]
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0

    # WAY OUT 1 — ask for at least `least`, from the same (topped-up) balance
    await fund(user, "ETH", 1)
    ok = await client.post("/v1/withdrawals", json=_body(_items(least)), headers=h)
    assert ok.status_code == 200, ok.text
    got = ok.json()["items"][0]
    assert got["fee_mode"] == "from_amount" and 2 * got["delivered_groth"] >= least

    # WAY OUT 2 — hold enough balance for the fees to ride on top of the ORIGINAL amount
    await fund(user, "ETH", ETH)
    ok2 = await client.post("/v1/withdrawals", json=_body(_items(amount)), headers=h)
    assert ok2.status_code == 200, ok2.text
    assert ok2.json()["items"][0]["fee_mode"] == "on_top"
    assert ok2.json()["items"][0]["delivered_groth"] == amount


def test_the_named_minimum_is_the_smallest_amount_that_actually_works():
    """`smallest_undominated` is derived from the same lattice the solver searches, so the
    number in the sentence is a number the user can act on: one groth less is still refused."""
    asset = get_asset("ETH")
    for bridge in (0, 1, 37, 1_000, 18_000, 22_500, 250_000, 3_000_000):
        for floor in (1, 2, 1_000):
            least = w.smallest_undominated(bridge, asset=asset, floor=floor)
            assert least >= w.smallest_from_amount(bridge, asset=asset, floor=floor)
            d = w.deliverable_for(least, bridge, asset=asset, floor=floor)
            assert d > 0 and 2 * d >= least
            below = w.deliverable_for(least - 1, bridge, asset=asset, floor=floor)
            assert 2 * below < least - 1


def test_the_half_rule_and_the_solver_agree_groth_by_groth():
    """Brute force over the interesting window: "the solver's answer covers half" and "the
    smallest legal delivery at or above half fits" are the same statement."""
    asset = get_asset("ETH")
    bridge = BRIDGE
    floor = 1
    least = w.smallest_undominated(bridge, asset=asset, floor=floor)
    for amount in range(max(1, least - 200), least + 200):
        d = w.deliverable_for(amount, bridge, asset=asset, floor=floor)
        assert (2 * d >= amount) is (amount >= least), amount


async def test_a_delivery_of_nothing_is_still_the_bridge_fee_refusal(
    client, user, monkeypatch
):
    """The older, narrower refusal is unchanged: below the crossing plus one grid step there is
    no delivery at all, and that is its own sentence."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", BRIDGE + 1)
    r = await client.post("/v1/withdrawals", json=_body(_items(BRIDGE + 1)), headers=user["headers"])
    assert r.status_code == 422, r.text
    item = r.json()["detail"]["items"][0]
    assert item["problem_code"] == "bridge_fee" and item["fee_mode"] == "on_top"
