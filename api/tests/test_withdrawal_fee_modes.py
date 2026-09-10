"""T35 — fees on top when the balance can carry them, out of the amount when it cannot.

The admin's rule (2026-09-10 10:3xZ): *"You need to take fees above the amount user requested.
If user requested to get 0.01 ETH, we should deposit 0.01 ETH; only if user doesn't have deposit
to pay gas fees and 2% fees to us, we take it from sending amount, so he gets less than 0.01
ETH."* — API_CONTRACT.md § "Withdrawals — fee model and minimums", last bullet.

Per item, IN ROW ORDER, against what is left of Available after the rows above it:

  remaining ≥ amount + fee(amount) + bridge  → on_top      delivered = amount, debited = total
  remaining ≥ amount                          → from_amount debited = amount, delivered = the
                                                largest grid value d with d+fee(d)+bridge ≤ amount
  otherwise                                   → refused     (problem_code "batch", short by X)

and in BOTH modes `delivered + fee + bridge == debited`, which is what the ledger takes and what
the release later sends. The old behaviour — a batch refused outright because the fees did not
fit on top — is gone: a user with 0.01 ETH can withdraw 0.01 ETH and receive slightly less.
"""

from __future__ import annotations

import math
import random
import time

from conftest import fund
from eth_account import Account as EthAccount

from pgasme import ledger
from pgasme.assets import get_asset
from pgasme.config import settings
from pgasme.routers import withdrawals as w

ETH = 100_000_000  # groth
ETH_ASSET = get_asset("ETH")  # T45 item 5: every user-facing amount is stated in the asset
RELAYER = 18_000  # FakeRpc answers 1 gwei → an 18,000-groth b2e relayer fee
BRIDGE = math.ceil(RELAYER * w.headroom_for(0))  # the ASAP crossing, headroom floor included
TOTAL_1M = 1_000_000 + 20_000 + BRIDGE  # one 0.01 ETH payout with the fees ON TOP


def _w() -> str:
    return EthAccount.create().address


def _items(*specs) -> list[dict]:
    return [{"W": _w(), "amount_groth": a} for a in specs]


def _body(items, mode="direct", asset="ETH") -> dict:
    return {"asset": asset, "items": items, "mode": mode}


# ───────────────────────────────────────────────────────── the two modes, one item at a time


async def test_fees_ride_on_top_when_the_balance_can_carry_them(client, user, mock_db, monkeypatch):
    """The default and the promise: ask for 0.01 ETH, the wallet receives 0.01 ETH."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post("/v1/withdrawals", json=_body(_items(1_000_000)), headers=user["headers"])
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["fee_mode"] == "on_top"
    assert item["requested_groth"] == item["amount_groth"] == 1_000_000
    assert item["delivered_groth"] == 1_000_000  # the wallet gets exactly what was asked for
    assert item["fee_groth"] == 20_000 and item["bridge_fee_groth"] == BRIDGE
    assert item["debited_groth"] == item["total_groth"] == TOTAL_1M
    assert "fee_note" not in item
    row = await mock_db["pgasme_test"].payout_requests.find_one({})
    assert (row["fee_mode"], row["delivered_groth"], row["requested_groth"]) == (
        "on_top",
        1_000_000,
        1_000_000,
    )
    # the executor's send amount IS the delivered amount (payouts.py reads `amount_groth`)
    assert row["amount_groth"] == row["delivered_groth"] == 1_000_000
    assert row["total_debited_groth"] == row["debited_groth"] == TOTAL_1M
    assert await ledger.debited_groth(row["_id"]) == TOTAL_1M


async def test_fees_come_out_of_the_amount_when_the_balance_cannot_carry_them(
    client, user, mock_db, monkeypatch
):
    """Exactly 0.01 ETH in the balance, 0.01 ETH requested: the order goes, smaller.

    The old rule refused this batch (409, "insufficient balance") — the user could never spend
    their last groth. Now the fees come out of the amount: the account is debited exactly what it
    has, and the wallet receives what is left after our 2% and the crossing."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    r = await client.post("/v1/withdrawals", json=_body(_items(1_000_000)), headers=user["headers"])
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["fee_mode"] == "from_amount"
    assert item["requested_groth"] == item["amount_groth"] == 1_000_000
    # 1,000,000 − 22,500 bridge = 977,500 for (d + 2% of d): the largest whole d is 958,333
    assert item["delivered_groth"] == 958_333
    assert item["fee_groth"] == 19_167 and item["bridge_fee_groth"] == BRIDGE
    assert item["debited_groth"] == item["total_groth"] == 1_000_000
    assert item["fee_note"] == w.FROM_AMOUNT_NOTE
    # the three parts are the whole: nothing is charged that the user was not shown
    assert item["delivered_groth"] + item["fee_groth"] + item["bridge_fee_groth"] == 1_000_000

    row = await mock_db["pgasme_test"].payout_requests.find_one({})
    assert row["fee_mode"] == "from_amount"
    assert row["amount_groth"] == row["delivered_groth"] == 958_333
    assert row["requested_groth"] == 1_000_000
    assert row["fee_groth"] + row["bridge_fee_groth"] + row["amount_groth"] == 1_000_000
    # THE LEDGER: the two halves of one debit sum to the amount, not to amount + fees
    assert await ledger.debited_groth(row["_id"]) == 1_000_000
    d = mock_db["pgasme_test"]
    base = await d.entries.find_one({"kind": "schedule", "ref": row["_id"]})
    bridge = await d.entries.find_one({"kind": "schedule_bridge_fee", "ref": row["_id"]})
    assert base["groth"] == 958_333 + 19_167 == 977_500
    assert bridge["groth"] == BRIDGE
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": 0,
        "scheduled": 1_000_000,
        "sent": 0,
    }


async def test_the_whole_balance_can_be_withdrawn_in_one_order(client, user, monkeypatch):
    """The user's last groth is spendable: Available goes to exactly 0, never negative.

    T35b raised the balance this is proven at from 40,000 to 100,000 groth: a 40,000-groth
    order pays 22,500 of it to the crossing and delivers 17,156, which is now refused as
    `fees_dominate` (a delivery that is mostly fees is not a withdrawal anyone asked for). The
    property being tested is the one that mattered — the last groth is spendable — and it is
    unchanged for every amount the rule admits."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 100_000)
    r = await client.post("/v1/withdrawals", json=_body(_items(100_000)), headers=user["headers"])
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["fee_mode"] == "from_amount" and item["debited_groth"] == 100_000
    assert 0 < item["delivered_groth"] < 100_000 - BRIDGE
    assert 2 * item["delivered_groth"] >= 100_000  # …and most of it still reaches the wallet
    assert (await ledger.balance(user["account_id"], "ETH"))["available"] == 0


# ─────────────────────────────────────────────────────────────────── the batch, in row order


async def test_a_mixed_batch_decides_row_by_row_against_what_is_left(
    client, user, mock_db, monkeypatch
):
    """Rows are priced in the order the user typed them: the early ones ride on top, the row
    where the balance runs out pays its own fees, and a row past that is short."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    # room for two on-top rows and then exactly 1,000,000 groth — enough for a third row's
    # amount but not for its fees, and nothing at all for a fourth
    await fund(user, "ETH", 2 * TOTAL_1M + 1_000_000)
    h = user["headers"]
    body = _body(_items(1_000_000, 1_000_000, 1_000_000, 1_000_000))

    quote = (await client.post("/v1/withdrawals/preview", json=body, headers=h)).json()
    modes = [i["fee_mode"] for i in quote["items"]]
    assert modes == ["on_top", "on_top", "from_amount", "on_top"]
    assert [i["ok"] for i in quote["items"]] == [True, True, True, False]
    assert quote["items"][3]["problem_code"] == "batch"
    assert "short" in quote["items"][3]["problem"]
    assert [i["delivered_groth"] for i in quote["items"][:3]] == [1_000_000, 1_000_000, 958_333]
    assert [i["debited_groth"] for i in quote["items"][:3]] == [TOTAL_1M, TOTAL_1M, 1_000_000]
    assert quote["batch"]["ok"] is False  # the batch as typed does not fit
    # ⛔ THE TOP-UP, NOT THE GAP (T35b). `need − available` is TOTAL_1M here, and crediting that
    # would flip the third row from `from_amount` to `on_top` — 20,000 + the crossing more than
    # it debits today — and the batch would be refused a second time by our own number. What is
    # published is what makes all four rows ride on top: Σ on-top − Available.
    assert quote["batch"]["need_groth"] - quote["batch"]["available_groth"] == TOTAL_1M
    assert quote["batch"]["shortfall_groth"] == 4 * TOTAL_1M - (2 * TOTAL_1M + 1_000_000)

    # …and the withdrawal refuses the whole batch with THAT sentence — a 409 about the batch,
    # never a 422 about the row where the running total happened to cross
    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == quote["batch"]["problem"]
    assert r.headers["X-Shortfall-Groth"] == str(quote["batch"]["shortfall_groth"])
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0

    # drop the row that did not fit and the same batch goes through, third row from the amount
    body["items"] = body["items"][:3]
    out = (await client.post("/v1/withdrawals", json=body, headers=h)).json()
    assert [i["fee_mode"] for i in out["items"]] == ["on_top", "on_top", "from_amount"]
    assert out["totals"]["delivered_groth"] == 1_000_000 + 1_000_000 + 958_333
    assert out["totals"]["amount_groth"] == 3_000_000  # what was REQUESTED
    assert out["totals"]["total_debited_groth"] == 2 * TOTAL_1M + 1_000_000
    assert (await ledger.balance(user["account_id"], "ETH"))["available"] == 0


async def test_a_batch_that_only_the_fees_do_not_fit_is_no_longer_refused(
    client, user, monkeypatch
):
    """The regression this task exists for: Σ amounts ≤ Available < Σ (amounts + fees) used to be
    a 409 with a shortfall and nothing scheduled. It is now two orders, the last one smaller.

    The greedy is deliberate and it is IN ROW ORDER: the rows the balance can fund on top are
    funded on top, and only the row the money runs out on gives anything up. It is not an
    optimiser — it never shrinks row 1 to make room for row 3 — so a batch whose LAST row cannot
    even cover its own amount is still refused as one unit (the test above)."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", TOTAL_1M + 1_000_000)  # Σ amounts fits; Σ (amounts + fees) does not
    body = _body(_items(1_000_000, 1_000_000))
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["request_ids"]) == 2
    assert [i["fee_mode"] for i in out["items"]] == ["on_top", "from_amount"]
    assert out["totals"]["amount_groth"] == 2_000_000  # requested
    assert out["totals"]["delivered_groth"] == 1_000_000 + 958_333  # received
    assert out["totals"]["total_debited_groth"] == TOTAL_1M + 1_000_000
    assert (await ledger.balance(user["account_id"], "ETH"))["available"] == 0


async def test_a_row_with_nothing_left_is_a_batch_problem_and_never_a_422(
    client, user, mock_db, monkeypatch
):
    """`problem_code: "batch"` is a verdict about the LIST, so it refuses the way the batch rule
    refuses (409 + the shortfall) — a 422 would tell the user their row is wrong when the only
    thing wrong is how much money is in the account."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000)
    r = await client.post("/v1/withdrawals", json=_body(_items(1_000_000)), headers=user["headers"])
    assert r.status_code == 409, r.text
    # T45 item 5: the sentence is in ETH and names the top-up, never groth and never a field
    assert r.json()["detail"].startswith("Not enough ETH: this batch needs ")
    assert "groth" not in r.json()["detail"]
    assert r.headers["X-Shortfall-Groth"] == str(TOTAL_1M - 1_000)
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0


# ───────────────────────────────────────────────────────────── the amount that cannot pay itself


async def test_an_amount_too_small_to_pay_the_bridge_out_of_itself_is_refused(
    client, user, mock_db, monkeypatch
):
    """The edge of the from-amount rule: below the bridge fee plus one grid step there is no
    delivery left to make, so the row is refused in words that name both ways out."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    # the exact boundary: one groth of delivery plus its own 2% plus the crossing
    least = w.smallest_from_amount(BRIDGE, asset=get_asset("ETH"), floor=1)
    assert least == BRIDGE + 2
    await fund(user, "ETH", least)  # covers each amount below; never their fees on top
    h = user["headers"]
    for amount in (BRIDGE, BRIDGE + 1):  # nothing would be left to deliver
        r = await client.post("/v1/withdrawals", json=_body(_items(amount)), headers=h)
        assert r.status_code == 422, r.text
        item = r.json()["detail"]["items"][0]
        assert item["ok"] is False and item["problem_code"] == "bridge_fee"
        assert "too small to pay the bridge fee from the amount" in item["problem"]
        assert f"at least {w.fmt_units(least, ETH_ASSET)}" in item["problem"]  # the way out
        # the refused row keeps a CONSISTENT quote (the on-top one, i.e. what funding it would
        # cost): every row this API returns satisfies delivered + fee + bridge == debited, so a
        # client renders any row's arithmetic without a special case for the refused ones
        assert item["fee_mode"] == "on_top" and item["delivered_groth"] == amount
        assert (
            item["delivered_groth"] + item["fee_groth"] + item["bridge_fee_groth"]
            == item["debited_groth"]
        )
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0
    # …one groth more and there IS a delivery to make — and it is ONE GROTH against a 22,502
    # groth debit, which T35b refuses under its own code: `bridge_fee` is "nothing would be
    # left to deliver", `fees_dominate` is "what is left is not a withdrawal". The arithmetic
    # below the boundary is unchanged; what changed is that we no longer write that order.
    r = await client.post("/v1/withdrawals", json=_body(_items(least)), headers=h)
    assert r.status_code == 422, r.text
    item = r.json()["detail"]["items"][0]
    assert item["problem_code"] == "fees_dominate"
    assert item["fee_mode"] == "from_amount" and item["delivered_groth"] == 1
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0
    # …and the SAME amount with the fees affordable on top is a perfectly good order
    await fund(user, "ETH", ETH)
    r = await client.post("/v1/withdrawals", json=_body(_items(BRIDGE)), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["fee_mode"] == "on_top"


# ────────────────────────────────────────────────────────────────────── the arithmetic itself


def test_the_delivered_amount_is_the_largest_grid_value_that_pays_its_own_fees():
    """The property, over random amounts and random bridge fees: what we deliver lands on the
    grid, its own fee and the crossing fit inside the amount, and one grid step more does not."""
    rng = random.Random(20260910)
    for _ in range(3000):
        bridge = rng.choice([0, 1, 37, 1_000, 18_000, 22_500, 250_000, 3_000_000])
        amount = rng.choice(
            [rng.randint(1, 100), rng.randint(1, 10**6), rng.randint(1, 10**9), rng.randint(1, 10**14)]
        )
        # every asset this deployment carries has a grid of 1 (`grid_groth`); the coarser steps
        # are here so the solver is proved on a lattice, not on the identity
        step = rng.choice([1, 1, 1, 10, 1_000])
        d = w.deliverable_groth(amount, bridge, step=step, floor=step)
        if d:
            assert d % step == 0 and d >= step
            assert d + w.fee_for(d) + bridge <= amount
            assert (d + step) + w.fee_for(d + step) + bridge > amount  # maximal
        else:  # nothing fits: not even one step of it
            assert step + w.fee_for(step) + bridge > amount


def test_the_fee_absorbs_the_rounding_so_the_three_parts_are_always_the_whole():
    """`delivered + fee + bridge == debited`, in both modes, for every amount — the remainder a
    grid step leaves behind stays in OUR fee rather than becoming a groth nobody accounts for."""
    rng = random.Random(4242)
    eth = get_asset("ETH")
    for _ in range(2000):
        amount = rng.randint(1, 10**12)
        bridge = rng.choice([0, 18_000, 22_500, 400_000])
        d = w.deliverable_for(amount, bridge, asset=eth, floor=1)
        if not d:
            continue
        fee = amount - bridge - d
        assert fee >= w.fee_for(d)  # never less than our 2% of what we deliver
        assert fee - w.fee_for(d) <= w.grid_groth(eth) + 1  # …and never more than the rounding
        assert d + fee + bridge == amount


# ──────────────────────────────────────────────────────────────────────── preview == create


async def test_the_preview_quotes_the_from_amount_order_exactly_as_it_is_charged(
    client, user, monkeypatch
):
    """Law 9 under the new rule: the mode decision, the delivered amount and the debit are ONE
    call, so what the form showed is what the money path does."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    # room for the first row with its fees on top and 920,000 groth after it — enough for the
    # second row's amount, not for its fees
    await fund(user, "ETH", 1_500_000 + 30_000 + BRIDGE + 920_000)
    h = user["headers"]
    body = _body(_items(1_500_000, 900_000))
    quote = (await client.post("/v1/withdrawals/preview", json=body, headers=h)).json()
    assert [i["fee_mode"] for i in quote["items"]] == ["on_top", "from_amount"]
    assert quote["totals"]["delivered_groth"] == sum(i["delivered_groth"] for i in quote["items"])
    assert quote["batch"]["ok"] is True
    out = (await client.post("/v1/withdrawals", json=body, headers=h)).json()
    for shown, charged in zip(quote["items"], out["items"], strict=True):
        for k in (
            "delivered_groth",
            "debited_groth",
            "fee_groth",
            "bridge_fee_groth",
            "total_groth",
            "fee_mode",
            "requested_groth",
        ):
            assert shown[k] == charged[k], k
    assert quote["totals"] == out["totals"]


async def test_the_preview_writes_nothing_when_it_prices_a_from_amount_row(
    client, user, mock_db, monkeypatch
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    body = _body(_items(1_000_000))
    r = await client.post("/v1/withdrawals/preview", json=body, headers=user["headers"])
    assert r.status_code == 200 and r.json()["items"][0]["fee_mode"] == "from_amount"
    d = mock_db["pgasme_test"]
    assert await d.payout_requests.count_documents({}) == 0
    assert await d.entries.count_documents({"kind": "schedule"}) == 0
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": 1_000_000,
        "scheduled": 0,
        "sent": 0,
    }


# ────────────────────────────────────────────────────────────────── rows written before today


async def test_a_row_with_no_fee_mode_reads_as_on_top(client, user, mock_db, monkeypatch):
    """Legacy rows are never rewritten (law: never edit history). Every reader of a payout row
    resolves the mode ONCE, here, and a row from before 2026-09-10 delivered its full amount."""
    legacy = {
        "_id": "legacy1",
        "account_id": user["account_id"],
        "asset": "ETH",
        "mode": "direct",
        "W": _w(),
        "amount_groth": 1_000_000,
        "fee_groth": 20_000,
        "bridge_fee_groth": BRIDGE,
        "total_debited_groth": TOTAL_1M,
        "status": "scheduled",
        "created_at": time.time(),
    }
    assert w.row_fee_mode(legacy) == "on_top"
    assert w.row_delivered_groth(legacy) == 1_000_000
    assert w.row_debited_groth(legacy) == TOTAL_1M
    # …including one written before the bridge fee was itemised at all
    older = dict(legacy, bridge_fee_groth=0, total_debited_groth=1_020_000)
    assert w.row_fee_mode(older) == "on_top" and w.row_debited_groth(older) == 1_020_000
    older.pop("total_debited_groth")
    assert w.row_debited_groth(older) == 1_020_000  # amount + our fee, derived, never guessed

    await mock_db["pgasme_test"].payout_requests.insert_one(legacy)
    got = (await client.get("/v1/account", headers=user["headers"])).json()["requests"][0]
    assert got["amount_groth"] == 1_000_000 and "fee_mode" not in got  # the row is untouched
    assert w.row_fee_mode(got) == "on_top" and w.row_delivered_groth(got) == 1_000_000


# ──────────────────────────────────────────────────── the 409 that used to claim a zero shortfall


async def test_a_reservation_refused_while_the_batch_fits_says_what_actually_happened(
    client, user, mock_db, monkeypatch
):
    """T30c skeptic (b): `create` answered "insufficient balance … shortfall_groth 0" with a
    `X-Shortfall-Groth: 0` header when the batch DID fit and the reservation refused because
    another batch of the same account was in flight. A refusal that names the wrong cause sends
    the user to top up an account that has the money; a header claiming a shortfall of zero is a
    number that cannot be acted on at all."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    # a live reservation of this account's whole balance: the batch below fits Available and the
    # atomic claim still refuses it, which is exactly the race the sentence has to explain
    await mock_db["pgasme_test"].reservations.insert_one(
        {
            "_id": f"{user['account_id']}:ETH",
            "account_id": user["account_id"],
            "asset": "ETH",
            "pending": ETH,
            "at": time.time(),
        }
    )
    r = await client.post("/v1/withdrawals", json=_body(_items(1_000_000)), headers=user["headers"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == w.INFLIGHT_PROBLEM
    assert "another order of yours is being written" in r.json()["detail"]
    assert "insufficient" not in r.json()["detail"] and "shortfall" not in r.json()["detail"]
    assert "X-Shortfall-Groth" not in r.headers  # never a shortfall of 0
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0
    assert (await ledger.balance(user["account_id"], "ETH"))["available"] == ETH


# ─────────────────────────────────────────────────────────────────────── instant denominations


async def test_an_instant_payout_never_takes_its_fees_out_of_the_amount(
    client, user, mock_db, monkeypatch
):
    """⛔ An instant payout is paid out of DENOMINATED float, and the next denomination down is
    not a rounding — it is half the order.

    The from-amount rule works because a direct delivery lands on a grid of one groth: the most
    an amount can lose is a groth, and our fee absorbs it. Applied to 0.01/0.1 ETH denominations
    the same arithmetic would deliver 0.01 ETH against a 0.02 ETH order, debit the whole 0.02 and
    book the difference as "our 2% fee" — a windfall of half the payout. So an instant row whose
    fees do not fit on top is SHORT (the batch rule, with the number it is short by), and it is
    the whole total it is short of, never just the amount."""
    monkeypatch.setattr(settings, "payout_instant_enabled", True)
    denoms = settings.denominations
    h = user["headers"]
    await fund(user, "ETH", 2 * denoms[0])  # the amount exactly, and nothing for the fees
    body = _body(_items(2 * denoms[0]), mode="instant")

    quote = (await client.post("/v1/withdrawals/preview", json=body, headers=h)).json()
    item = quote["items"][0]
    assert item["ok"] is False and item["problem_code"] == "batch"
    assert item["fee_mode"] == "on_top"  # the quote never pretends the amount could shrink
    assert item["delivered_groth"] == 2 * denoms[0]
    fees = item["fee_groth"] + item["bridge_fee_groth"]
    assert f"short by {w.fmt_units(fees, ETH_ASSET)}" in item["problem"]  # the FEES, not the amount

    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 409, r.text
    assert r.headers["X-Shortfall-Groth"] == str(fees)
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0

    # with the fees affordable on top it goes out whole, on its denomination
    await fund(user, "ETH", ETH)
    out = (await client.post("/v1/withdrawals", json=body, headers=h)).json()
    assert out["items"][0]["fee_mode"] == "on_top"
    assert out["items"][0]["delivered_groth"] == 2 * denoms[0]
    row = await mock_db["pgasme_test"].payout_requests.find_one({})
    assert any(row["amount_groth"] % d == 0 for d in denoms)


# ──────────────────────────────────────────────── the handoff to the release side (payouts.py)


async def test_the_release_side_reads_and_books_a_from_amount_order_correctly(
    client, user, mock_db, monkeypatch
):
    """⛔ THE HANDOFF, PROVEN END TO END — the one place this change could invent money.

    `payouts` sends and books against the row: `delivered_groth(row)` is what leaves, and
    `_book_release` clears `amount_groth + fee_groth + bridge_fee_groth` out of `Scheduled`. A
    from-amount order debits only the AMOUNT, so a row that stored the REQUESTED amount in
    `amount_groth` would send fee + bridge more than the ledger ever took and clear more than it
    put there — Scheduled would go negative and the treasury would pay the difference. The row
    therefore stores the DELIVERED number under `amount_groth` (and again, unambiguously, as
    `delivered_groth`), which is what both readers want. This test spends nothing: it books the
    release the way the worker books it and reads the ledger back."""
    from pgasme import payouts

    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    r = await client.post("/v1/withdrawals", json=_body(_items(1_000_000)), headers=user["headers"])
    assert r.status_code == 200, r.text
    row = await mock_db["pgasme_test"].payout_requests.find_one({})
    assert row["fee_mode"] == "from_amount"
    # the reader every gate, signature and ledger call on the release side goes through
    assert payouts.delivered_groth(row) == row["delivered_groth"] == 958_333
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": 0,
        "scheduled": 1_000_000,
        "sent": 0,
    }
    await payouts._book_release(row)
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": 0,
        "scheduled": 0,  # exactly what the schedule put there, cleared by the release
        "sent": 958_333,  # …and what the wallet receives is what is booked as sent
    }


async def test_the_release_side_still_reads_an_on_top_order_the_way_it_always_did(
    client, user, mock_db, monkeypatch
):
    from pgasme import payouts

    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post("/v1/withdrawals", json=_body(_items(1_000_000)), headers=user["headers"])
    assert r.status_code == 200, r.text
    row = await mock_db["pgasme_test"].payout_requests.find_one({})
    assert payouts.delivered_groth(row) == row["amount_groth"] == 1_000_000
    await payouts._book_release(row)
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - TOTAL_1M,
        "scheduled": 0,
        "sent": 1_000_000,
    }


async def test_cancelling_a_from_amount_order_returns_exactly_what_it_took(
    client, user, mock_db, monkeypatch
):
    """A refund is the mirror of a debit that landed — read from the entries, never recomputed
    from amount + fee, which for a from-amount order would give the money back twice over."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    r = await client.post("/v1/withdrawals", json=_body(_items(1_000_000)), headers=user["headers"])
    rid = r.json()["request_ids"][0]
    out = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    assert out.status_code == 200 and out.json() == {"cancelled": rid, "refunded_groth": 1_000_000}
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": 1_000_000,
        "scheduled": 0,
        "sent": 0,
    }


async def test_every_row_the_api_returns_adds_up(client, user, monkeypatch):
    """THE INVARIANT THE UI RENDERS: on every item, in every mode, refused or not,

        delivered_groth + fee_groth + bridge_fee_groth == debited_groth == total_groth

    so a client never has to know which rule priced a row to show what it costs. The batch below
    has a row funded on top, a row that pays its fees out of its amount, and two rows the balance
    has nothing left for — including a tiny one, to show that a short row is short whatever its
    size, and that its quote still adds up."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", TOTAL_1M + 900_000)
    body = _body(_items(1_000_000, 900_000, BRIDGE, 5_000_000))
    quote = (await client.post("/v1/withdrawals/preview", json=body, headers=user["headers"])).json()
    assert [i["fee_mode"] for i in quote["items"]] == ["on_top", "from_amount", "on_top", "on_top"]
    assert [i.get("problem_code") for i in quote["items"]] == [None, None, "batch", "batch"]
    for i in quote["items"]:
        assert i["delivered_groth"] + i["fee_groth"] + i["bridge_fee_groth"] == i["debited_groth"]
        assert i["debited_groth"] == i["total_groth"]
        assert i["requested_groth"] == i["amount_groth"]
        assert i["fee_groth"] >= w.fee_for(i["delivered_groth"])  # never under our own 2%
    assert quote["totals"]["total_debited_groth"] == sum(i["total_groth"] for i in quote["items"])
    assert quote["totals"]["delivered_groth"] == sum(i["delivered_groth"] for i in quote["items"])
