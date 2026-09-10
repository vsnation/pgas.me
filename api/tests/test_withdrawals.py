"""Every rule of POST /v1/withdrawals (the 2026-09-09 "Scheduling" shape), fees, and cancel.

A withdrawal is a LIST of orders: (address, amount, when it should be delivered). No address is
registered, connected or signed for; the server checks the checksum and the chain, derives the
minimum from the live bridge fee, validates the batch against Available as ONE unit, and writes
one order per item.
"""

from __future__ import annotations

import math
import time

from conftest import fund
from eth_account import Account as EthAccount

from pgasme import ledger, tg
from pgasme.assets import get_asset
from pgasme.config import settings
from pgasme.routers import withdrawals as w
from pgasme.routers.withdrawals import clear_fees_cache, release_at_for

ETH = 100_000_000  # groth
ETA = 66 * 60  # PGAS_BRIDGE_ETA_S
# FakeRpc answers 1 gwei → an 18_000-groth b2e relayer fee. An order released NOW no longer
# carries 1× headroom: since 2026-09-10 the curve has a FLOOR (`PGAS_BRIDGE_HEADROOM_MIN`, 1.25)
# because two ASAP orders quoted at exactly today's gas were held by a 7% base-fee tick seconds
# later. So the bridge fee an ASAP order is charged is derived here from the one function that
# prices it, never re-spelled as a literal (law 9).
RELAYER = 18_000
BRIDGE = math.ceil(RELAYER * w.headroom_for(0))
# what one 0.01 ETH payout costs an account: amount + our 2% + the bridge fee it funds
TOTAL_1M = 1_000_000 + 20_000 + BRIDGE


def _w() -> str:
    return EthAccount.create().address


def _body(w, amount=1_000_000, mode="direct", deliver_at=None, asset="ETH"):
    item = {"W": w, "amount_groth": amount}
    if deliver_at is not None:
        item["deliver_at"] = deliver_at
    return {"asset": asset, "items": [item], "mode": mode}


async def _ok(client, user, **kw):
    r = await client.post("/v1/withdrawals", json=_body(_w(), **kw), headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()


# ────────────────────────────────────────────────────────────── the address the user types


async def test_any_valid_address_is_accepted_without_registration(client, user, monkeypatch):
    """The whole point of the 2026-09-09 restructure: no proof, no connection, no address book."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    out = await _ok(client, user)
    assert len(out["request_ids"]) == 1
    dests = (await client.get("/v1/destinations", headers=user["headers"])).json()["destinations"]
    assert [d["address"] for d in dests] == [user["address"]]  # nothing was registered


async def test_checksum_cases(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    good = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    bad = "0x5aAeb6053f3E94C9b9A09f33669435E7Ef1BeAed"  # one letter down-cased: checksum breaks
    h = user["headers"]

    # a per-item problem is a 422 that NAMES the item (design 2026-09-10), not a bare 400
    r = await client.post("/v1/withdrawals", json=_body(bad), headers=h)
    assert r.status_code == 422 and "checksum" in r.json()["detail"]["message"]
    assert r.json()["detail"]["items"][0]["ok"] is False
    # lowercase carries no checksum information at all: accepted, and stored checksummed
    r = await client.post("/v1/withdrawals", json=_body(good.lower()), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["W"] == good
    r = await client.post("/v1/withdrawals", json=_body(good), headers=h)
    assert r.status_code == 200 and r.json()["items"][0]["W"] == good
    assert (await client.post("/v1/withdrawals", json=_body("not-an-address"), headers=h)).status_code in (400, 422)
    assert (await client.post("/v1/withdrawals", json=_body("0x" + "zz" * 20), headers=h)).status_code == 422


async def test_a_contract_destination_is_refused_and_an_unreadable_chain_is_a_503(
    client, user, monkeypatch, rpc, mock_db
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    h = user["headers"]
    contract, wallet = _w(), _w()
    rpc.code[contract.lower()] = "0x60806040"

    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 400 and "is a contract, not a wallet" in r.json()["detail"]
    # one contract in the list refuses the WHOLE batch, and nothing is written
    body = {
        "asset": "ETH",
        "items": [{"W": wallet, "amount_groth": 1_000_000}, {"W": contract, "amount_groth": 1_000_000}],
        "mode": "direct",
    }
    assert (await client.post("/v1/withdrawals", json=body, headers=h)).status_code == 400
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    # an endpoint that could not answer is NEVER read as "no code, therefore a wallet"
    rpc.unreadable.add(wallet.lower())
    r = await client.post("/v1/withdrawals", json=_body(wallet), headers=h)
    assert r.status_code == 503 and r.json()["detail"] == w.UNREADABLE_DEST
    rpc.unreadable.clear()
    rpc.head_dead = True
    r = await client.post("/v1/withdrawals", json=_body(wallet), headers=h)
    assert r.status_code == 503 and r.json()["detail"] == w.UNREADABLE_DEST
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0


async def test_the_code_read_is_pinned_to_the_endpoint_that_gave_the_head(client, user, monkeypatch, rpc):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    await _ok(client, user)
    reads = [c for c in rpc.calls if c[0] == "call" and c[1] == "eth_getCode"]
    assert len(reads) == 1
    (_, _, params, prefer, pin) = reads[0]
    assert pin is True and prefer == rpc.PRIMARY and params[1] == hex(rpc.head)


# ─────────────────────────────────────────────────────────────────────── the money arithmetic


async def test_the_fee_is_our_two_percent_plus_the_bridge_at_cost(
    client, user, mock_db, monkeypatch
):
    """The 2026-09-10 model: our cut and the bridge's are two lines, not one — the user pays the
    crossing at cost and receives exactly `amount_groth`."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w1, w2 = _w(), _w()
    body = {
        "asset": "ETH",
        "items": [{"W": w1, "amount_groth": 1_000_000}, {"W": w2, "amount_groth": 2_500_000}],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["fee_groth"] == 20_000 + 50_000  # OURS only
    assert out["bridge_fee_groth"] == 2 * BRIDGE  # theirs, at cost, per item
    assert out["total_debited_groth"] == 3_500_000 + 70_000 + 2 * BRIDGE
    assert out["totals"] == {
        "amount_groth": 3_500_000,
        # T35: what the wallets RECEIVE, beside what was requested. Both rows are `on_top` here
        # (the balance carries the fees), so the two numbers are the same.
        "delivered_groth": 3_500_000,
        "fee_groth": 70_000,
        "bridge_fee_groth": 2 * BRIDGE,
        "total_debited_groth": 3_500_000 + 70_000 + 2 * BRIDGE,
    }
    assert [i["amount_groth"] for i in out["items"]] == [1_000_000, 2_500_000]
    assert [i["total_groth"] for i in out["items"]] == [
        1_000_000 + 20_000 + BRIDGE,
        2_500_000 + 50_000 + BRIDGE,
    ]
    rows = await mock_db["pgasme_test"].payout_requests.find({}).to_list(10)
    assert {
        (r["W"], r["amount_groth"], r["fee_groth"], r["bridge_fee_groth"], r["status"])
        for r in rows
    } == {
        (w1, 1_000_000, 20_000, BRIDGE, "scheduled"),
        (w2, 2_500_000, 50_000, BRIDGE, "scheduled"),
    }
    # …and every row's own arithmetic reconciles with what the ledger took
    for row in rows:
        assert (
            row["amount_groth"] + row["fee_groth"] + row["bridge_fee_groth"]
            == row["total_debited_groth"]
        )
    # the ledger moved amount + OUR fee under `schedule` and the bridge fee under its own kind,
    # and Sent is untouched until release
    d = mock_db["pgasme_test"]
    entries = await d.entries.find({"kind": "schedule"}).to_list(10)
    assert sorted(e["groth"] for e in entries) == [1_020_000, 2_550_000]
    bridge_entries = await d.entries.find({"kind": "schedule_bridge_fee"}).to_list(10)
    assert sorted(e["groth"] for e in bridge_entries) == [BRIDGE, BRIDGE]
    debited = 3_500_000 + 70_000 + 2 * BRIDGE
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - debited,
        "scheduled": debited,
        "sent": 0,
    }
    # the one helper every refund path reads gives back exactly what the two halves took
    for row in rows:
        assert await ledger.debited_groth(row["_id"]) == row["total_debited_groth"]


async def test_the_bridge_fee_is_quoted_from_the_live_gas_price_and_the_floor_is_technical(
    client, user, monkeypatch, rpc
):
    """The 2026-09-10 model. There is NO economic minimum: the bridge fee is charged per item at
    cost, so a payout of a few dollars — the entire point of the product — is admissible.

    The fee is the b2e relayer's OWN arithmetic run on the live gas price (payouts.relayer_fee_for
    → beam.relayer_fee_groth → eth_feeHistory): 120_000 gas × gwei × 1.5 margin."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    h = user["headers"]
    fees = (await client.get("/v1/withdrawals/fees", headers=h)).json()
    assert fees["fee_bps"] == 200
    # what the crossing COSTS today (`relayer_fee_groth_now`) and what an ASAP order is
    # CHARGED for it (`bridge_fee_groth_now`) are two numbers now: the charge carries the
    # `PGAS_BRIDGE_HEADROOM_MIN` floor, which is what a base-fee tick between the quote and the
    # release costs, and unspent headroom stays with the treasury.
    assert fees["relayer_fee_groth_now"] == RELAYER
    assert fees["bridge_fee_groth_now"] == BRIDGE == 22_500
    assert fees["min_amount_groth"] == 1  # the technical floor: a positive amount on the grid
    assert fees["bridge_eta_s"] == ETA
    assert fees["headroom"][0] == {"window_s": 0, "factor": 1.25}  # the ASAP floor
    # …and since T45 the far end is the same floor: the 4× subsidy the release gate allows is
    # what carries a 30-day wait, and the unspent part comes back at settlement, so charging a
    # 3× crossing up front only parked the user's money for a month.
    assert fees["headroom"][-1] == {"window_s": settings.max_window_s, "factor": 1.25}

    # the amount the OLD derived floor refused outright is now an ordinary payout
    r = await client.post("/v1/withdrawals", json=_body(_w(), amount=999_999), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["bridge_fee_groth"] == BRIDGE

    rpc.gas_gwei = 5.0  # 90_000 groth of relayer fee — the CHARGE moves, the floor does not
    clear_fees_cache()
    fees = (await client.get("/v1/withdrawals/fees", headers=h)).json()
    assert fees["relayer_fee_groth_now"] == 90_000
    assert fees["bridge_fee_groth_now"] == 112_500 and fees["min_amount_groth"] == 1
    r = await client.post("/v1/withdrawals", json=_body(_w(), amount=200_000), headers=h)
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["fee_groth"] == 4_000 and item["bridge_fee_groth"] == 112_500
    assert item["total_groth"] == 200_000 + 4_000 + 112_500
    assert r.json()["min_amount_groth"] == 1
    assert r.json()["relayer_fee_groth_estimate"] == 90_000


async def test_a_payout_of_a_few_groth_is_charged_a_whole_groth_of_fee(client, user, monkeypatch):
    """Grid rounding. `amount × 200 // 10000` floors 1 groth of ETH to a fee of ZERO — which was
    unreachable while the floor was 0.01 ETH and is reachable now that it is 1 groth. Both parts
    of the charge round UP: the only direction that cannot cost the treasury money."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post("/v1/withdrawals", json=_body(_w(), amount=1), headers=user["headers"])
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["fee_groth"] == 1  # ceil(1 × 0.02), never 0
    assert item["bridge_fee_groth"] == BRIDGE
    assert item["total_groth"] == 1 + 1 + BRIDGE


async def test_the_preview_quotes_exactly_what_the_withdrawal_charges(client, user, monkeypatch):
    """ONE implementation (law 9): the form's numbers and the money path's numbers are one call.

    A preview writes nothing — no row, no ledger entry, no reservation — and quoting it does not
    reserve anything, which is why `create` still decides the batch against Available atomically."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    h = user["headers"]
    far = time.time() + settings.max_window_s - 60
    items = [
        {"W": _w(), "amount_groth": 250_000},
        {"W": _w(), "amount_groth": 3_000_000, "deliver_at": far},
    ]
    body = {"asset": "ETH", "items": items, "mode": "direct"}
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    assert pv.status_code == 200, pv.text
    quote = pv.json()
    assert [i["ok"] for i in quote["items"]] == [True, True]
    assert quote["available_groth"] == ETH and quote["min_amount_groth"] == 1
    # ⚠️ THE WAIT IS NOT PRICED INTO THE CHARGE ANY MORE (T45): `headroom_for` divides the
    # far-dated margin by the subsidy the release gate already allows (4×), so every window
    # quotes the `PGAS_BRIDGE_HEADROOM_MIN` floor. The rise is carried by the gate, and whatever
    # the crossing does not spend is refunded at settlement — so both items are quoted the same.
    assert quote["items"][0]["bridge_fee_groth"] == BRIDGE
    assert quote["items"][1]["bridge_fee_groth"] == BRIDGE
    assert quote["totals"]["total_debited_groth"] == sum(i["total_groth"] for i in quote["items"])

    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    for shown, charged in zip(quote["items"], out["items"], strict=True):
        assert shown["amount_groth"] == charged["amount_groth"]
        assert shown["fee_groth"] == charged["fee_groth"]
        assert shown["bridge_fee_groth"] == charged["bridge_fee_groth"]
        assert shown["total_groth"] == charged["total_groth"]
    assert quote["totals"]["total_debited_groth"] == out["total_debited_groth"]


async def test_the_preview_writes_nothing_and_names_the_items_it_would_refuse(
    client, user, mock_db, monkeypatch
):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    h = user["headers"]
    bad = "0x5aAeb6053f3E94C9b9A09f33669435E7Ef1BeAed"  # one letter down-cased
    body = {
        "asset": "ETH",
        "items": [{"W": _w(), "amount_groth": 500_000}, {"W": bad, "amount_groth": 500_000}],
        "mode": "direct",
    }
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    assert pv.status_code == 200, pv.text
    items = pv.json()["items"]
    assert items[0]["ok"] is True and "problem" not in items[0]
    assert items[1]["ok"] is False and "checksum" in items[1]["problem"]
    # a refused row is still PRICED — a form that blanks its totals on one typo is unusable
    assert items[1]["total_groth"] == 500_000 + 10_000 + BRIDGE

    # …and the withdrawal refuses the batch on the same verdicts, without writing anything
    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 422
    assert [i["ok"] for i in r.json()["detail"]["items"]] == [True, False]
    d = mock_db["pgasme_test"]
    assert await d.payout_requests.count_documents({}) == 0
    assert await d.entries.count_documents({"kind": "schedule"}) == 0
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }


async def test_a_refused_batch_names_each_item_and_never_dumps_the_destination_list(
    client, user, mock_db, monkeypatch
):
    """T30c/F5: the 422 detail is a DICT — {message, items, min_amount_groth} — and it stays one.

    A client that renders `detail` as it arrives (JSON.stringify of a dict) puts every
    destination address in the batch on the screen in one blob. The shape is what lets it render
    ONE sentence (`detail.message`) and put each item's own `problem` beside the row it belongs
    to, so nothing here may grow a second address-bearing field: `items` already carry the
    addresses, because they are the rows being rendered. §9.7 lives on the other side of this —
    the operator LOG gets problem CODES, never these sentences, because the address-shaped ones
    quote what the user typed and api.log already carries the account beside the request."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    good1, good2 = _w(), _w()
    bad = "0x5aAeb6053f3E94C9b9A09f33669435E7Ef1BeAed"  # mixed case, one letter down-cased
    body = {
        "asset": "ETH",
        "items": [
            {"W": good1, "amount_groth": 500_000},
            {"W": bad, "amount_groth": 500_000},
            {"W": good2, "amount_groth": 500_000},
        ],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert set(detail) == {"message", "items", "min_amount_groth"}  # nothing else, ever
    assert isinstance(detail["message"], str)
    assert "1 of 3 item(s) cannot be scheduled" in detail["message"]
    assert "nothing was scheduled and nothing was debited" in detail["message"]
    # the reason travels ON the item, beside the row that will render it
    assert [i["ok"] for i in detail["items"]] == [True, False, True]
    assert "checksum" in detail["items"][1]["problem"]
    assert detail["items"][1]["problem_code"] == "address"
    assert detail["min_amount_groth"] == 1
    # …and the message is not a dump of the batch: the addresses that are FINE are not in it
    assert good1 not in detail["message"] and good2 not in detail["message"]
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0


async def test_a_fee_we_could_not_read_refuses_rather_than_guesses(client, user, monkeypatch, rpc):
    """Law 8: an unreadable query is not evidence of anything, and it is NEVER a 0. The bridge
    fee is charged from that number, so a 0 would quote free crossings the release then holds."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rpc.gas_gwei = None  # eth_feeHistory unreadable
    clear_fees_cache()
    h = user["headers"]
    r = await client.get("/v1/withdrawals/fees", headers=h)
    assert r.status_code == 503 and "live bridge relayer fee" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(_w()), headers=h)
    assert r.status_code == 503
    r = await client.post(
        "/v1/withdrawals/preview",
        json={"asset": "ETH", "items": [{"W": _w(), "amount_groth": 1_000_000}]},
        headers=h,
    )
    assert r.status_code == 503  # the preview never invents a fee either
    assert (await client.get("/v1/withdrawals/fees?asset=NOPE", headers=h)).status_code == 400


async def test_a_relayer_fee_that_reads_as_zero_is_not_a_price(client, user, monkeypatch):
    """An answer that arrived, carrying a number that cannot be true. 0 × any headroom is 0, so a
    zero here would quote free crossings for as long as the 60 s cache holds — and the release
    gate (`live fee > bridge_fee_groth × subsidy`) would then hold every one of those orders,
    with the user's groth already in `scheduled` behind a delivery time we promised."""
    from pgasme import payouts

    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)

    async def no_fee(asset, rpc):
        return 0, 0, {}

    monkeypatch.setattr(payouts, "relayer_fee_for", no_fee)
    clear_fees_cache()
    h = user["headers"]
    r = await client.get("/v1/withdrawals/fees", headers=h)
    assert r.status_code == 503 and "read as 0" in r.json()["detail"]
    assert (await client.post("/v1/withdrawals", json=_body(_w()), headers=h)).status_code == 503
    r = await client.post(
        "/v1/withdrawals/preview",
        json={"asset": "ETH", "items": [{"W": _w(), "amount_groth": 1_000_000}]},
        headers=h,
    )
    assert r.status_code == 503


async def test_the_fee_read_is_cached_for_a_minute(client, user, rpc):
    """Every rendered form asks for the fee; the gas read behind it is one call a minute."""
    h = user["headers"]
    for _ in range(3):
        assert (await client.get("/v1/withdrawals/fees", headers=h)).status_code == 200
    assert len([c for c in rpc.calls if c[0] == "call" and c[1] == "eth_feeHistory"]) == 1
    clear_fees_cache()
    assert (await client.get("/v1/withdrawals/fees", headers=h)).status_code == 200
    assert len([c for c in rpc.calls if c[0] == "call" and c[1] == "eth_feeHistory"]) == 2


async def test_the_batch_is_validated_as_one_unit_against_the_sum_of_the_totals(
    client, user, mock_db, monkeypatch
):
    """The one batch rule that survives the 2026-09-10 model: Σ total_groth ≤ Available — where
    `total` is what the row debits, the number the preview showed.

    Since T35 a row whose fees do not fit on top is not refused — it takes them out of its amount
    — so what refuses the batch as one unit is a row that cannot cover its own AMOUNT. Two rows
    ride on top here and the third is one groth short of its own 1,000,000."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 2 * TOTAL_1M + 1_000_000 - 1)
    body = {
        "asset": "ETH",
        "items": [{"W": _w(), "amount_groth": 1_000_000} for _ in range(3)],
        "mode": "direct",
    }
    h = user["headers"]
    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 409
    short = TOTAL_1M - (1_000_000 - 1)  # the refused row is quoted with its fees ON TOP
    assert r.headers["X-Shortfall-Groth"] == str(short)
    # T45 item 5: the top-up is named in ETH — `shortfall_groth` was a FIELD NAME in a
    # sentence a person had to act on, and the number a client uses is the header above.
    assert f"Top up {w.fmt_units(short, get_asset('ETH'))} " in r.json()["detail"]
    assert "groth" not in r.json()["detail"]
    d = mock_db["pgasme_test"]
    assert await d.payout_requests.count_documents({}) == 0  # not even the two it could afford
    assert await d.entries.count_documents({"kind": "schedule"}) == 0
    assert await d.entries.count_documents({"kind": "schedule_bridge_fee"}) == 0
    # the preview says the same thing before anything is sent, and still writes nothing
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    assert pv.status_code == 200
    quote = pv.json()
    assert quote["totals"]["total_debited_groth"] == 3 * TOTAL_1M > quote["available_groth"]
    assert await d.payout_requests.count_documents({}) == 0
    body["items"] = body["items"][:2]
    assert (await client.post("/v1/withdrawals", json=body, headers=h)).status_code == 200


async def test_the_preview_says_whether_the_batch_fits_and_create_refuses_in_the_same_words(
    client, user, mock_db, monkeypatch
):
    """T30c/F4: the batch rule is ONE function, and the preview renders its verdict.

    The preview priced every item `ok: true` and left the batch rule — Σ total ≤ Available —
    implicit in two numbers it also returned, so a client either re-derived the verdict (law 9:
    two implementations of one fact, and the OTHER one is the one that moves money) or showed a
    batch as fine that `create` then refused. Now `batch` says it, computed by the same call
    `create` refuses on, in the same words and with the same shortfall."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 2 * TOTAL_1M)  # room for exactly two of the three
    h = user["headers"]
    body = {
        "asset": "ETH",
        "items": [{"W": _w(), "amount_groth": 1_000_000} for _ in range(3)],
        "mode": "direct",
    }
    quote = (await client.post("/v1/withdrawals/preview", json=body, headers=h)).json()
    # the row the balance ran out on carries the BATCH's code, so a form can mark it — and it is
    # still refused as one unit below (409 + the shortfall), never as a 422 about that row
    assert [i["ok"] for i in quote["items"]] == [True, True, False]
    assert quote["items"][2]["problem_code"] == "batch"
    batch = quote["batch"]
    assert batch["ok"] is False and batch["shortfall_groth"] == TOTAL_1M
    assert batch["need_groth"] == 3 * TOTAL_1M == quote["totals"]["total_debited_groth"]
    assert batch["available_groth"] == 2 * TOTAL_1M == quote["available_groth"]

    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 409
    assert r.headers["X-Shortfall-Groth"] == str(TOTAL_1M) == str(batch["shortfall_groth"])
    assert r.json()["detail"] == batch["problem"]  # ONE sentence, not two that drift
    assert await mock_db["pgasme_test"].payout_requests.count_documents({}) == 0

    body["items"] = body["items"][:2]
    fits = (await client.post("/v1/withdrawals/preview", json=body, headers=h)).json()["batch"]
    assert fits == {
        "ok": True,
        "need_groth": 2 * TOTAL_1M,
        "available_groth": 2 * TOTAL_1M,
        "shortfall_groth": 0,
    }
    assert (await client.post("/v1/withdrawals", json=body, headers=h)).status_code == 200


async def test_too_many_items_is_refused(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "max_items_per_withdrawal", 2)
    await fund(user, "ETH", ETH)
    body = {
        "asset": "ETH",
        "items": [{"W": _w(), "amount_groth": 1_000_000} for _ in range(3)],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 400 and "at most 2 wallets" in r.json()["detail"]


# ────────────────────────────────────────────────────────────────────────── the delivery time


def test_release_at_is_never_in_the_past_and_never_late():
    now = 1_000_000.0
    assert release_at_for(None, now, ETA) == now  # "asap"
    assert release_at_for(now - 3600, now, ETA) == now  # a delivery time already gone
    assert release_at_for(now + 10, now, ETA) == now  # sooner than the bridge can manage
    assert release_at_for(now + 3 * 3600, now, ETA) == now + 3 * 3600 - ETA  # one ETA early


async def test_release_at_math_end_to_end(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    t0 = time.time()
    future = t0 + 6 * 3600
    body = {
        "asset": "ETH",
        "items": [
            {"W": _w(), "amount_groth": 1_000_000, "deliver_at": future},
            {"W": _w(), "amount_groth": 1_000_000, "deliver_at": t0 - 7200},  # already past
            {"W": _w(), "amount_groth": 1_000_000},  # absent → asap
        ],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert abs(items[0]["release_at"] - (future - ETA)) < 1
    assert items[0]["deliver_at"] == future
    assert t0 <= items[1]["release_at"] <= time.time() and items[1]["deliver_at"] == t0 - 7200
    assert t0 <= items[2]["release_at"] <= time.time() and items[2]["deliver_at"] is None
    rows = {r["_id"]: r for r in await mock_db["pgasme_test"].payout_requests.find({}).to_list(10)}
    for i in items:
        row = rows[i["request_id"]]
        assert row["release_at"] == i["release_at"] and row["deliver_at"] == i["deliver_at"]
        assert row["relayer_fee_groth_estimate"] == 18_000


async def test_a_delivery_further_out_than_the_window_is_refused(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post(
        "/v1/withdrawals",
        json=_body(_w(), deliver_at=time.time() + 31 * 86400),
        headers=user["headers"],
    )
    assert r.status_code == 422 and "30 days away" in r.json()["detail"]["message"]


# ─────────────────────────────────────────────────────────────────────────── modes and events


async def test_disabled_mode_is_409_with_a_reason(client, user):
    r = await client.post("/v1/withdrawals", json=_body(_w()), headers=user["headers"])
    assert r.status_code == 409 and "not enabled" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(_w(), mode="instant"), headers=user["headers"])
    assert r.status_code == 409 and "instant" in r.json()["detail"]
    r = await client.post("/v1/withdrawals", json=_body(_w(), mode="teleport"), headers=user["headers"])
    assert r.status_code == 400


async def test_instant_requires_a_denomination_multiple(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_instant_enabled", True)
    await fund(user, "ETH", ETH)
    r = await client.post(
        "/v1/withdrawals", json=_body(_w(), amount=1_500_000, mode="instant"), headers=user["headers"]
    )
    assert r.status_code == 422 and "denomination" in r.json()["detail"]["message"]
    r = await client.post(
        "/v1/withdrawals", json=_body(_w(), amount=3_000_000, mode="instant"), headers=user["headers"]
    )
    assert r.status_code == 200


async def test_one_event_per_order_carrying_ids_only(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    w1, w2 = _w(), _w()
    body = {
        "asset": "ETH",
        "items": [{"W": w1, "amount_groth": 1_000_000}, {"W": w2, "amount_groth": 1_000_000}],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=user["headers"])
    ids = r.json()["request_ids"]
    evs = await mock_db["pgasme_test"].events.find({"kind": "withdrawal_requested"}).to_list(10)
    assert len(evs) == 2 and sorted(e["request_id"] for e in evs) == sorted(ids)
    for e in evs:
        assert w1.lower() not in e["text"].lower() and w2.lower() not in e["text"].lower()
        assert e["notified"] is False and "request_ids" not in e
        assert tg.format_event(e).endswith(f"<code>{e['request_id']}</code>")


async def test_the_account_and_the_fees_route_report_the_same_floor(client, user):
    """Two names for one number is how they drift apart (law 9). A form falls back to
    `/v1/account.min_payout_groth` while `/v1/withdrawals/fees` has not answered yet, so the two
    must be the same call — `routers.withdrawals.min_amount_groth`."""
    h = user["headers"]
    fees = (await client.get("/v1/withdrawals/fees", headers=h)).json()
    acct = (await client.get("/v1/account", headers=h)).json()
    assert acct["min_payout_groth"] == fees["min_amount_groth"] == 1


async def test_every_refusal_leaves_a_row_in_the_log(client, user, monkeypatch, caplog):
    """Law 12: refusals are not trades and not failures, but every decision path writes one. A
    rule that only ever renders as an HTTP body is a rule nobody can see firing — which is how a
    $250 cap on the arb stack ran for 23.5 hours and cost $383 before anyone noticed.

    §9.7: the log line carries the account and the reason and NEVER `W`. api.log already records
    the request line; pairing an account with a destination in the same file is the product
    defeated."""
    import logging

    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000)  # far too little for the batch below
    h = user["headers"]
    dest = _w()
    bad = "0x5aAeb6053f3E94C9b9A09f33669435E7Ef1BeAed"
    with caplog.at_level(logging.INFO, logger="pgasme.withdrawals"):
        assert (await client.post("/v1/withdrawals", json=_body(bad), headers=h)).status_code == 422
        r = await client.post("/v1/withdrawals", json=_body(dest), headers=h)
        assert r.status_code == 409
        assert (
            await client.post("/v1/withdrawals", json=_body(dest, asset="DAI"), headers=h)
        ).status_code == 400
    lines = [r.getMessage() for r in caplog.records if r.name == "pgasme.withdrawals"]
    assert sum("withdrawal refused 422" in x for x in lines) == 1
    assert sum("withdrawal refused 409" in x for x in lines) == 1
    assert sum("withdrawal refused 400" in x for x in lines) == 1
    assert any("item 1 address" in x for x in lines) and any("short" in x for x in lines)
    # …and never the destination: the refusal logs the problem CODE, not the sentence that
    # quotes the address the user typed
    blob = "\n".join(lines).lower()
    assert dest.lower() not in blob and bad.lower() not in blob


# ───────────────────────────────────────────────────────────────────────────────────── cancel


async def test_cancel_returns_the_money_and_is_final(client, user, mock_db, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = (await _ok(client, user))["request_ids"][0]
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])
    # the refund is what the schedule entries actually debited — BOTH halves, our fee and the
    # bridge fee, never a recomputed amount + fee
    assert r.status_code == 200 and r.json() == {"cancelled": rid, "refunded_groth": TOTAL_1M}
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    assert (await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid}))["status"] == "cancelled"
    assert (await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])).status_code == 409
    assert (await client.post("/v1/withdrawals/nope/cancel", headers=user["headers"])).status_code == 404


async def test_cancel_only_by_the_owner(client, user, monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", ETH)
    rid = (await _ok(client, user))["request_ids"][0]
    from conftest import sign_in

    other = await sign_in(client, EthAccount.create())
    assert (
        await client.post(f"/v1/withdrawals/{rid}/cancel", headers=other["headers"])
    ).status_code == 404
