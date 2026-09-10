"""T45 item 5 — every sentence a user reads states money in the ASSET, never in groth.

Admin, 2026-09-10 15:35Z, quoting the 409 he had just hit himself:

    "Error: insufficient ETH balance: this batch needs 2900846 groth and 2834074 groth is
     available — top up shortfall_groth 66772 groth …"
    → *"Why in groth? People don't understand nothing in it. You should have human readable
       errors in ETH and show what actually available to user."*

`groth` is an internal unit (1e-8 of the asset) and `shortfall_groth` is a FIELD NAME. Both were
in a sentence a person had to act on. So:

  * ONE formatter, `withdrawals.fmt_units(groth, asset)` — 8 decimals, trailing zeros trimmed,
    the asset's symbol after it ("0.02900846 ETH"). It wraps `tg.fmt_groth`, which is the same
    arithmetic every operator line already uses (law 9: one implementation).
  * EVERY user-facing sentence the withdrawals API emits goes through it: the batch shortfall
    409, the per-row `batch` problem, `fees_dominate`, `bridge_fee`, the minimum, the grid, the
    denomination rule.
  * MACHINE FIELDS ARE UNTOUCHED. `*_groth` on the wire and `X-Shortfall-Groth` are what a client
    computes with; the sentence is what a person reads, and they are not the same job.
  * `max_on_top_groth` — the largest amount this row can ask for and still have its fees charged
    on top — is published per item, so "Use max" is the API's arithmetic and not the form's.
"""

from __future__ import annotations

import math
import time

from conftest import fund
from eth_account import Account as EthAccount

from pgasme.assets import get_asset
from pgasme.config import settings
from pgasme.routers import withdrawals as w

ETH = 100_000_000  # groth
RELAYER = 18_000  # FakeRpc answers 1 gwei → an 18,000-groth b2e relayer fee
BRIDGE = math.ceil(RELAYER * w.headroom_for(0))  # 22,500 — the ASAP crossing with its floor


def _w() -> str:
    return EthAccount.create().address


def _body(items, asset="ETH", mode="direct"):
    return {"asset": asset, "items": items, "mode": mode}


def sentences(payload) -> list[str]:
    """Every string in an API answer a person could be shown. A sweep, not a list: a new
    sentence is covered the day it is written rather than the day somebody remembers it."""
    out: list[str] = []
    if isinstance(payload, str):
        # PROSE ONLY: a bare token with no space in it is a field name (pydantic's own 422 `loc`
        # carries "amount_groth"), and a wire field is not a sentence — see `fmt_units`.
        if " " in payload:
            out.append(payload)
    elif isinstance(payload, dict):
        for k, v in payload.items():
            if k.endswith("_groth") or k in ("groth", "denominations"):
                continue  # machine fields: numbers, not prose
            out.extend(sentences(v))
    elif isinstance(payload, list):
        for v in payload:
            out.extend(sentences(v))
    return out


# ═══════════════════════════════════ the formatter itself ════════════════════════════════════


def test_fmt_units_trims_and_names_the_asset():
    """8 decimals, trailing zeros trimmed, the symbol after it. The trimming matters: nobody
    reads "1.00000000 ETH" as one ETH faster than they read "1 ETH", and a fee line of
    "0.00020000" is three zeros of noise on the number the user is deciding about."""
    eth = get_asset("ETH")
    assert w.fmt_units(2_900_846, eth) == "0.02900846 ETH"
    assert w.fmt_units(66_772, eth) == "0.00066772 ETH"
    assert w.fmt_units(100_000_000, eth) == "1 ETH"
    assert w.fmt_units(150_000_000, eth) == "1.5 ETH"
    assert w.fmt_units(1, eth) == "0.00000001 ETH"
    assert w.fmt_units(0, eth) == "0 ETH"
    assert w.fmt_units(22_500, get_asset("DAI")) == "0.000225 DAI"
    assert w.fmt_units(22_500, get_asset("WBTC")) == "0.000225 WBTC"


# ═════════════════════════════ the sentence the admin was shown ══════════════════════════════


async def test_the_shortfall_sentence_is_in_the_asset_and_says_what_to_top_up(
    client, user, monkeypatch
):
    """The skeptic's example, end to end: two rows against a balance that covers one.

    The sentence names three amounts — what the batch needs, what is available, and what to add
    — in ETH, and it no longer says `shortfall_groth` (a field name) or "another withdrawal of
    yours may be in flight" (a different cause, which has a sentence of its own and must not be
    hedged into this one). The MACHINE fields carry the same numbers in groth."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    h = user["headers"]
    items = [{"W": _w(), "amount_groth": 1_000_000}, {"W": _w(), "amount_groth": 100_000}]

    r = await client.post("/v1/withdrawals", json=_body(items), headers=h)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail == (
        "Not enough ETH: this batch needs 0.011245 ETH and 0.01 ETH is available. "
        "Top up 0.00167 ETH and every wallet receives the full amount it asked for "
        "(fees are charged on top once your balance covers them)."
    )
    assert "groth" not in detail and "shortfall_groth" not in detail
    # the client's number is untouched — the sentence is for the person, the header for the code
    assert r.headers["X-Shortfall-Groth"] == "167000"

    # …and the preview says exactly the same thing before anything is sent
    pv = await client.post("/v1/withdrawals/preview", json=_body(items), headers=h)
    assert pv.status_code == 200, pv.text
    batch = pv.json()["batch"]
    assert batch["problem"] == detail
    assert batch["need_groth"] == 1_124_500 and batch["available_groth"] == 1_000_000
    assert batch["shortfall_groth"] == 167_000
    # crediting exactly that makes the batch go through with every row on top — the fixed point
    # the sentence promises (T35b), still true now that the sentence is in ETH
    await fund(user, "ETH", 167_000)
    ok = await client.post("/v1/withdrawals", json=_body(items), headers=h)
    assert ok.status_code == 200, ok.text
    assert [i["fee_mode"] for i in ok.json()["items"]] == ["on_top", "on_top"]


# ═══════════════════════ every other sentence, one per problem code ══════════════════════════


async def test_every_priced_problem_states_its_amounts_in_the_asset(client, user, monkeypatch):
    """One row per refusal the pricer can produce, and not one of them says groth."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "min_payout_groth", 10_000)
    await fund(user, "ETH", 10 * ETH)
    h = user["headers"]

    # ── the minimum
    pv = await client.post("/v1/withdrawals/preview", json=_body([{"W": _w(), "amount_groth": 1}]), headers=h)
    item = pv.json()["items"][0]
    assert item["problem_code"] == "min_amount"
    assert item["problem"] == "each payout must be at least 0.0001 ETH"

    # ── fees taken from the amount would swallow more than half of it (T35b)
    monkeypatch.setattr(settings, "min_payout_groth", 1)
    # the first row eats everything but 30,000 groth, so the second one pays its fees out of a
    # 0.0003 ETH amount and delivers less than half of it
    body = _body(
        [{"W": _w(), "amount_groth": 10 * ETH - 30_000}, {"W": _w(), "amount_groth": 30_000}]
    )
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    dominated = pv.json()["items"][1]
    assert dominated["problem_code"] == "fees_dominate", dominated
    assert dominated["problem"] == (
        "fees would take more than half of 0.0003 ETH — hold at least 0.000531 ETH of balance "
        "so fees are charged on top, or ask for at least 0.0004592 ETH"
    )

    # ── the amount cannot even pay for the crossing out of itself
    body = _body(
        [{"W": _w(), "amount_groth": 10 * ETH - 100}, {"W": _w(), "amount_groth": 100}]
    )
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    tiny = pv.json()["items"][1]
    assert tiny["problem_code"] == "bridge_fee"
    assert tiny["problem"] == (
        "0.000001 ETH is too small to pay the bridge fee from the amount — the crossing alone "
        "costs 0.000225 ETH and there must be something left to deliver. Either hold enough "
        "balance for the fees to be charged on top of the amount, or ask for at least "
        "0.00022502 ETH"
    )

    # ── the batch ran out of money at this row
    body = _body([{"W": _w(), "amount_groth": 10 * ETH}, {"W": _w(), "amount_groth": 5 * ETH}])
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    short = pv.json()["items"][1]
    assert short["problem_code"] == "batch"
    assert short["problem"] == (
        "this row needs 5 ETH and only 0 ETH of your balance is left after the rows above it — "
        "short by 5 ETH"
    )

    # ── the instant lane's denomination rule
    monkeypatch.setattr(settings, "payout_instant_enabled", True)
    body = _body([{"W": _w(), "amount_groth": 1_234_567}], mode="instant")
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    denom = pv.json()["items"][0]
    assert denom["problem_code"] == "denomination"
    assert denom["problem"] == "instant payouts must be a multiple of a denomination (0.01 ETH, 0.1 ETH)"


async def test_not_one_user_facing_sentence_says_groth(client, user, monkeypatch, mock_db):
    """THE SWEEP. Every string in every answer of the withdrawal routes — the good batch, the
    refused batch, the per-item problems, the fees route, the account row — read as prose.

    A sweep rather than a list of sentences: this is the assertion that covers the sentence
    somebody writes next month, and the reason the formatter exists at all."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    h = user["headers"]
    bad = "0x5aAeb6053f3E94C9b9A09f33669435E7Ef1BeAed"  # one letter down-cased
    payloads = []
    payloads.append((await client.get("/v1/withdrawals/fees", headers=h)).json())
    for body in (
        _body([{"W": _w(), "amount_groth": 1_000_000}, {"W": _w(), "amount_groth": 100_000}]),
        _body([{"W": bad, "amount_groth": 1_000}]),
        _body([{"W": _w(), "amount_groth": 0}]),
        _body([{"W": _w(), "amount_groth": 100}, {"W": _w(), "amount_groth": 900_000}]),
    ):
        payloads.append((await client.post("/v1/withdrawals/preview", json=body, headers=h)).json())
        payloads.append((await client.post("/v1/withdrawals", json=body, headers=h)).json())
    payloads.append((await client.get("/v1/account", headers=h)).json())
    said = [s for s in sentences(payloads) if "groth" in s.lower()]
    assert said == [], said


# ════════════════════════════════ "Use max", from the API ════════════════════════════════════


async def test_the_preview_publishes_the_largest_amount_the_balance_covers_on_top(
    client, user, monkeypatch
):
    """`max_on_top_groth` per item — the largest amount this row can ask for and still have its
    fees charged ON TOP, out of what the rows above it left of Available.

    ⛔ IT IS THE API'S ARITHMETIC, NOT THE FORM'S. A "Use max" button that computed
    `available − 2% − bridge` in the client would be a second implementation of the fee model
    (law 9) — and the one that reaches the user's own money first. It comes off the same lattice
    `deliverable_for` uses for the from-amount rows, so filling it in makes the row on_top."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    h = user["headers"]
    pv = await client.post("/v1/withdrawals/preview", json=_body([{"W": _w(), "amount_groth": 1}]), headers=h)
    top = pv.json()["items"][0]["max_on_top_groth"]
    # 1,000,000 − 22,500 of crossing, and the 2% on top of what is left: 958,333 + 19,167 + 22,500
    assert top == 958_333
    assert top + w.fee_for(top) + BRIDGE == 1_000_000

    # …and asking for exactly it is an on-top row that fits to the groth
    r = await client.post("/v1/withdrawals", json=_body([{"W": _w(), "amount_groth": top}]), headers=h)
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert item["fee_mode"] == "on_top" and item["delivered_groth"] == top
    assert item["total_groth"] == 1_000_000

    # a second row sees what the first one left — the fold, not a map
    await fund(user, "ETH", 1_000_000)
    body = _body([{"W": _w(), "amount_groth": 400_000}, {"W": _w(), "amount_groth": 1}])
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    first, second = pv.json()["items"]
    assert first["max_on_top_groth"] > second["max_on_top_groth"]
    left = 1_000_000 - first["total_groth"]
    assert second["max_on_top_groth"] + w.fee_for(second["max_on_top_groth"]) + BRIDGE <= left


async def test_a_row_the_balance_cannot_cover_at_all_offers_no_max(client, user, monkeypatch):
    """0 is an honest answer and not a button: there is nothing left for this row to ask for."""
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    await fund(user, "ETH", 1_000_000)
    h = user["headers"]
    body = _body([{"W": _w(), "amount_groth": 990_000}, {"W": _w(), "amount_groth": 1_000}])
    pv = await client.post("/v1/withdrawals/preview", json=body, headers=h)
    assert pv.json()["items"][1]["max_on_top_groth"] == 0
    assert time.time() > 0  # the module's clock is real; nothing here is time-dependent
