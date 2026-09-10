"""One regression per confirmed defect of the 2026-09-09 withdrawals review.

Every test here is a thing that was possible before the fix: a `deliver_at` of "NaN" that
slipped every guard and left the account unreadable, a batch half-written behind a 500 that a
plain retry paid twice, a compensation that refunded an order which had actually landed, a
cancel that froze the money on its first failed write, a reservation that decided on a balance
already spent, a cushion driven negative for good, a contract destination admitted because the
node answered block 0, a DAI order the executor can never pay, and a far-dated order priced for
a gas price it will never meet.
"""

from __future__ import annotations

import math
import time
from typing import Any

import pytest
from conftest import fund
from eth_account import Account as EthAccount

from pgasme import config, ledger, tg
from pgasme.config import settings
from pgasme.routers import withdrawals as w

ETH = 100_000_000  # groth
ETA = 66 * 60  # PGAS_BRIDGE_ETA_S
DB = "pgasme_test"
# FakeRpc answers 1 gwei → an 18_000-groth b2e relayer fee. An order released NOW no longer
# carries 1× headroom: since 2026-09-10 the curve has a FLOOR (`PGAS_BRIDGE_HEADROOM_MIN`, 1.25)
# because two ASAP orders quoted at exactly today's gas were held by a 7% base-fee tick seconds
# later. So the bridge fee an ASAP order is charged is derived here from the one function that
# prices it, never re-spelled as a literal (law 9).
RELAYER = 18_000
BRIDGE = math.ceil(RELAYER * w.headroom_for(0))
TOTAL_1M = 1_000_000 + 20_000 + BRIDGE  # amount + our 2% + the bridge fee it funds


def _w() -> str:
    return EthAccount.create().address


def _body(dest=None, amount=1_000_000, mode="direct", deliver_at=None, asset="ETH"):
    item: dict[str, Any] = {"W": dest or _w(), "amount_groth": amount}
    if deliver_at is not None:
        item["deliver_at"] = deliver_at
    return {"asset": asset, "items": [item], "mode": mode}


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)


# ═══════════ 1 — a `deliver_at` that is not a number never reaches the money path ════════════


async def test_a_non_finite_deliver_at_is_refused_before_a_groth_moves(client, user, mock_db, armed):
    """`deliver_at: "NaN"` used to pass EVERY guard: pydantic coerces the ordinary-JSON string to
    float('nan'), and `it.deliver_at - now > max_window_s` is False for NaN because every
    comparison with NaN is False. The reservation, the `schedule` debit, the row and the operator
    event all landed, the caller got a 500 (FastAPI renders with allow_nan=False) and never saw
    the request_id — and GET /v1/account was 500 for that account from then on, so the order
    could not even be found to cancel. Every retry of that 500 wrote another live order."""
    await fund(user, "ETH", ETH)
    h = user["headers"]
    d = mock_db[DB]
    assert (await client.get("/v1/account", headers=h)).status_code == 200
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = await client.post("/v1/withdrawals", json=_body(deliver_at=bad), headers=h)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    # the window guard behind the model is POSITIVE now: a value that is not a delivery time at
    # all is refused, instead of being admitted because it compares False to every bound.
    for absurd in (-1.0, -(10**12)):
        r = await client.post("/v1/withdrawals", json=_body(deliver_at=absurd), headers=h)
        assert r.status_code == 422 and "deliver_at" in r.json()["detail"]["message"]
    assert await d.payout_requests.count_documents({}) == 0
    assert await d.entries.count_documents({"kind": "schedule"}) == 0
    assert await d.events.count_documents({}) == 0
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    # the account is still readable — the poisoned row is what made it a permanent 500
    assert (await client.get("/v1/account", headers=h)).status_code == 200


# ═════════ 2 & 3 — the batch is one write, and a write that raised may still have landed ══════


class FlakyDb:
    """The real database with ONE `payout_requests.insert_one` scripted to fail — the Mongo blip
    the batch used to be half-written by. `commit_first` is the ambiguous failure: the server
    applies the write and the driver raises anyway (a socket timeout, a primary step-down)."""

    def __init__(self, real: Any, fail_on: int, commit_first: bool = False) -> None:
        self._real = real
        self.fail_on = fail_on
        self.commit_first = commit_first
        self.inserts = 0

    def __getattr__(self, name: str) -> Any:
        if name == "payout_requests":
            return _FlakyCollection(self._real.payout_requests, self)
        return getattr(self._real, name)


class _FlakyCollection:
    def __init__(self, real: Any, owner: FlakyDb) -> None:
        self._real = real
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def insert_one(self, doc: dict[str, Any], *a: Any, **k: Any) -> Any:
        self._owner.inserts += 1
        if self._owner.inserts == self._owner.fail_on:
            if self._owner.commit_first:
                await self._real.insert_one(doc, *a, **k)  # it LANDED …
            raise RuntimeError("mongo blip: connection reset by peer")  # … and still raised
        return await self._real.insert_one(doc, *a, **k)


def _flaky(monkeypatch, mock_db, fail_on: int, commit_first: bool = False) -> FlakyDb:
    flaky = FlakyDb(mock_db[DB], fail_on, commit_first)
    monkeypatch.setattr(w, "db", lambda: flaky)
    return flaky


async def test_a_batch_that_cannot_be_written_in_full_reverses_itself(
    client, user, mock_db, monkeypatch, armed
):
    """`_write_items` compensated the item it failed on and re-raised, so items 1..k−1 kept both
    their `schedule` debit and their live `scheduled` row while the caller got a 500 with no ids
    and no event at all — and an ordinary retry-on-500 wrote a SECOND order to the same address.
    The batch is one write now: it stands whole or it is reversed whole."""
    await fund(user, "ETH", ETH)
    h = user["headers"]
    d = mock_db[DB]
    w1, w2, w3 = _w(), _w(), _w()
    body = {
        "asset": "ETH",
        "items": [
            {"W": w1, "amount_groth": 1_000_000},
            {"W": w2, "amount_groth": 1_000_000},
            {"W": w3, "amount_groth": 1_000_000},
        ],
        "mode": "direct",
    }
    _flaky(monkeypatch, mock_db, fail_on=2)  # the SECOND item's row will not be written
    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 503 and "nothing was scheduled" in r.json()["detail"]
    assert await d.payout_requests.count_documents({"status": "scheduled"}) == 0
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    # every debit that landed has its offsetting entry, and history was never edited
    scheduled = await d.entries.find({"kind": "schedule"}).to_list(10)
    cancels = await d.entries.find({"kind": "cancel"}).to_list(10)
    assert sorted(e["ref"] for e in scheduled) == sorted(e["ref"] for e in cancels)
    # …and the refund is the whole debit: our fee AND the bridge fee that went with it
    bridges = await d.entries.find({"kind": "schedule_bridge_fee"}).to_list(10)
    assert sorted(e["ref"] for e in bridges) == sorted(e["ref"] for e in cancels)
    assert all(c["groth"] == TOTAL_1M for c in cancels)
    # no order stands, so no order event was written — and the operator was told
    assert await d.events.count_documents({"kind": "withdrawal_requested"}) == 0
    assert await d.events.find_one({"kind": "withdrawal_rolled_back"})
    # THE RETRY. The same body again is one batch of three, not a fourth order for w1.
    again = await client.post("/v1/withdrawals", json=body, headers=h)
    assert again.status_code == 200, again.text
    rows = await d.payout_requests.find({"status": "scheduled"}).to_list(10)
    assert len(rows) == 3 and sorted(r["W"] for r in rows) == sorted([w1, w2, w3])
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - 3 * TOTAL_1M,
        "scheduled": 3 * TOTAL_1M,
        "sent": 0,
    }


async def test_a_row_that_landed_is_never_refunded_out_from_under_its_order(
    client, user, mock_db, monkeypatch, armed
):
    """`except Exception: await ledger.cancel(...)` assumed an insert that raised did not commit.
    MongoDB is ambiguous on a socket timeout: the row sat in `scheduled` while its debit was
    handed back, so the payout worker (which only reads status + release_at) would release a
    payout nobody paid for, and `ledger.release` would then debit a `scheduled` bucket that no
    longer held it. Ask the database whether the row is there before refunding it."""
    await fund(user, "ETH", ETH)
    h = user["headers"]
    d = mock_db[DB]
    dest = _w()
    _flaky(monkeypatch, mock_db, fail_on=1, commit_first=True)
    r = await client.post("/v1/withdrawals", json=_body(dest), headers=h)
    assert r.status_code == 200, r.text
    rid = r.json()["request_ids"][0]
    row = await d.payout_requests.find_one({"_id": rid})
    assert row and row["status"] == "scheduled" and row["W"] == dest
    assert await ledger.find_entry("schedule", rid) is not None
    assert await ledger.find_entry("cancel", rid) is None  # the debit was NOT reversed
    assert await ledger.debited_groth(rid) == TOTAL_1M
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - TOTAL_1M,
        "scheduled": TOTAL_1M,
        "sent": 0,
    }
    # the order exists, so it has its one operator event — the file's own rule and law 12
    evs = await d.events.find({"kind": "withdrawal_requested"}).to_list(10)
    assert len(evs) == 1 and evs[0]["request_id"] == rid


# ═════════════ 4 — a cancel whose refund failed is repairable, not a frozen balance ═══════════


async def test_a_cancel_whose_refund_could_not_be_written_is_completed_by_the_next_call(
    client, user, mock_db, monkeypatch, armed
):
    """`cancel` flips the row to `cancelled` and only then appends the refund. When that append
    failed the row was already terminal: the worker only releases `scheduled`, the retry answered
    409 "request is cancelled", the groth stayed in `scheduled` forever and NOTHING was paged."""
    await fund(user, "ETH", ETH)
    h = user["headers"]
    d = mock_db[DB]
    r = await client.post("/v1/withdrawals", json=_body(), headers=h)
    rid = r.json()["request_ids"][0]
    real_cancel = ledger.cancel

    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("mongo blip: not master")

    monkeypatch.setattr(ledger, "cancel", boom)
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=h)
    assert r.status_code == 503 and "cancel again" in r.json()["detail"]
    assert (await d.payout_requests.find_one({"_id": rid}))["status"] == "cancelled"
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - TOTAL_1M,
        "scheduled": TOTAL_1M,
        "sent": 0,
    }
    assert await d.events.find_one({"kind": "withdrawal_cancel_refund_failed"})  # never silent
    # the next call finishes what the first one started, instead of 409ing on its own flip
    monkeypatch.setattr(ledger, "cancel", real_cancel)
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=h)
    assert r.status_code == 200 and r.json() == {"cancelled": rid, "refunded_groth": TOTAL_1M}
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    # …and it is still only ever refunded once
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=h)
    assert r.status_code == 409 and "cancelled" in r.json()["detail"]


# ═════════════ 5 & 6 — the reservation decides, and its counter cannot go negative ════════════


async def test_the_reservation_claims_before_it_reads_available(mock_db, monkeypatch):
    """`available` was read and then used as a CONSTANT inside the conditional update: between
    those two awaits another request could claim, write its entries and release, so `pending` was
    back at 0 and the filter passed on a balance that was already spent. Both batches were
    admitted, the account went negative and only the `after < 0` belt reversed one — a belt that
    cannot reverse a row the payout worker has already claimed. The claim goes FIRST now, so the
    number the decision is made on already counts this request's own groth."""
    aid = "acct1"
    await ledger.credit(aid, "ETH", 3_000_000, "c1")
    real = ledger.balance
    seen: dict[str, Any] = {}

    async def watch(account_id: str, asset: str) -> Any:
        res = await mock_db[DB].reservations.find_one({}) or {}
        seen["pending_when_read"] = res.get("pending")
        return await real(account_id, asset)

    monkeypatch.setattr(ledger, "balance", watch)
    assert await w.reserve(aid, "ETH", 1_000_000) is True
    assert seen["pending_when_read"] == 1_000_000  # the claim is already in the number
    monkeypatch.setattr(ledger, "balance", real)
    # and a claim that does not fit is backed out before a single entry is written
    assert await w.reserve(aid, "ETH", 2_500_000) is False
    assert (await mock_db[DB].reservations.find_one({}))["pending"] == 1_000_000


async def test_two_concurrent_batches_are_decided_by_the_reservation_not_by_the_belt(
    client, user, mock_db, monkeypatch, armed
):
    import asyncio

    # room for exactly ONE 0.01 ETH payout incl. our 2% AND the bridge fee it funds
    await fund(user, "ETH", TOTAL_1M)
    real = ledger.balance

    async def slow(account_id: str, asset: str) -> Any:
        await asyncio.sleep(0.02)  # both requests are in flight at the same time
        return await real(account_id, asset)

    monkeypatch.setattr(ledger, "balance", slow)
    body = _body()
    a, b = await asyncio.gather(
        client.post("/v1/withdrawals", json=body, headers=user["headers"]),
        client.post("/v1/withdrawals", json=body, headers=user["headers"]),
    )
    monkeypatch.setattr(ledger, "balance", real)
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    d = mock_db[DB]
    assert await d.payout_requests.count_documents({"status": "scheduled"}) == 1
    assert await d.entries.count_documents({"kind": "schedule"}) == 1
    # the loser was REFUSED, not written-and-reversed: no rolled-back row, no offsetting entry
    assert await d.payout_requests.count_documents({"status": "cancelled"}) == 0
    assert await d.entries.count_documents({"kind": "cancel"}) == 0
    assert await d.events.count_documents({"kind": "withdrawal_rolled_back"}) == 0
    assert (await d.reservations.find_one({}))["pending"] == 0


async def test_the_reservation_counter_never_goes_negative(mock_db):
    """`release()` was an unconditional `$inc -need` and the stale-clear zeroes a cushion that is
    still in flight, so the stalled request's release drove `pending` NEGATIVE — and it never
    recovered, because the stale-clear filter was `pending > 0`. From then on that account's
    admission filter was looser than intended by that amount, forever."""
    aid = "acct1"
    await ledger.credit(aid, "ETH", 10_000_000, "c1")
    d = mock_db[DB]
    assert await w.reserve(aid, "ETH", 1_000_000) is True
    await d.reservations.update_one({}, {"$set": {"at": time.time() - w.RESERVATION_STALE_S - 1}})
    assert await w.reserve(aid, "ETH", 1_000_000) is True  # the stale cushion was cleared
    await w.release(aid, "ETH", 1_000_000)
    await w.release(aid, "ETH", 1_000_000)  # …and the stalled request finally returns
    assert (await d.reservations.find_one({}))["pending"] == 0
    # a counter that somehow went negative is REPAIRED by the stale-clear, not skipped by it
    await d.reservations.update_one(
        {}, {"$set": {"pending": -1_000_000, "at": time.time() - w.RESERVATION_STALE_S - 1}}
    )
    assert await w.reserve(aid, "ETH", 1_000_000) is True
    assert (await d.reservations.find_one({}))["pending"] == 1_000_000


# ═══════════════ 7 — a head that cannot be true is not a head the guard may use ═══════════════


async def test_the_contract_guard_refuses_a_head_it_cannot_believe(
    client, user, mock_db, monkeypatch, rpc, armed
):
    """`refuse_contracts` pins eth_getCode to the endpoint that reported the head, but nothing
    checked the head. A snap-syncing or freshly-restarted provider answers `0x0`, the code read
    at that block is "0x" for a REAL contract, and the guard silently becomes a no-op — on the
    one refusal that has no refund path (a b2e delivery into a contract, §7.9)."""
    await fund(user, "ETH", ETH)
    h = user["headers"]
    contract = _w()
    rpc.code[contract.lower()] = "0x60806040"
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 400 and "is a contract" in r.json()["detail"]

    rpc.head = 0  # the node that answered has no chain to answer about
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 503 and r.json()["detail"] == w.UNREADABLE_DEST

    rpc.head = 2000  # …and a node that IS answering but is still syncing is not a verdict either
    rpc.syncing = {"currentBlock": "0x1", "highestBlock": "0x7d0"}
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 503 and r.json()["detail"] == w.UNREADABLE_DEST
    rpc.syncing = "unreadable"
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 503 and r.json()["detail"] == w.UNREADABLE_DEST

    rpc.syncing = False
    assert (await client.post("/v1/withdrawals", json=_body(contract), headers=h)).status_code == 400
    rpc.head = 100  # a restarted provider rewinding far below the head we have already seen
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 503 and r.json()["detail"] == w.UNREADABLE_DEST
    # a shallow reorg is not a rewind: the guard still works one block back
    rpc.head = 2000 - w.HEAD_REGRESSION_TOLERANCE
    assert (await client.post("/v1/withdrawals", json=_body(contract), headers=h)).status_code == 400
    assert await mock_db[DB].payout_requests.count_documents({}) == 0
    # and an admitted destination records WHICH block it was proven code-free at: the guard runs
    # only in this path, and a release up to 30 days later has nothing else to compare against
    rpc.head = 2000
    r = await client.post("/v1/withdrawals", json=_body(), headers=h)
    assert r.status_code == 200, r.text
    row = await mock_db[DB].payout_requests.find_one({"_id": r.json()["request_ids"][0]})
    assert row["dest_checked_head"] == 2000 and row["dest_checked_at"] > 0


# ═══════════════ 9 — an order the executor can never pay is refused, not debited ══════════════


async def test_an_asset_this_deployment_cannot_pay_is_refused_before_the_balance_moves(
    client, user, mock_db, armed
):
    """`payouts._payout_scheduled` HOLDS every non-ETH row ("v1 payouts are ETH only") forever.
    Admitting one here moved the user's DAI out of Available into `scheduled` for an order that
    can never execute — recoverable only by finding the order and cancelling it."""
    await fund(user, "DAI", 1_000_000_000_000)
    h = user["headers"]
    r = await client.post(
        "/v1/withdrawals", json=_body(amount=5_000_000_000, asset="DAI"), headers=h
    )
    assert r.status_code == 400 and "ETH only" in r.json()["detail"]
    d = mock_db[DB]
    assert await d.payout_requests.count_documents({}) == 0
    assert await d.entries.count_documents({"kind": "schedule"}) == 0
    assert await ledger.balance(user["account_id"], "DAI") == {
        "available": 1_000_000_000_000,
        "scheduled": 0,
        "sent": 0,
    }


# ════ 10 — the BRIDGE FEE is priced for the gas the order will meet, not for today's ═════════


def test_the_bridge_fee_scales_with_the_wait(monkeypatch):
    """The headroom curve moved from the FLOOR to the CHARGE (2026-09-10) — the arithmetic is the
    same line to 3×-at-max-window, and it is now what the user pays instead of what they must
    exceed. Its near end is the `PGAS_BRIDGE_HEADROOM_MIN` floor (1.25), not 1×: an ASAP order
    quoted at exactly today's gas is one base-fee tick from being held.

    ⚠️ **AND ON THIS DEPLOYMENT THE LINE IS FLAT** (T45). `headroom_for` divides the far-dated
    margin by the subsidy the release gate already allows, and the default is 4× since T45, so
    3/4 falls under the floor and the floor is what is charged at every window. The curve is not
    gone — it is bought by the gate instead of by the user, and the refund at settlement returns
    whatever the crossing does not spend. Both regimes are asserted below, because the ONE thing
    that must never drift is that these two readings of `relayer_subsidy` are the same number."""
    window = settings.max_window_s
    assert w.headroom_for(0) == 1.25
    assert w.fee_triple(1_000_000, 90_000, 0)[1] == 112_500  # release now: today's gas × the floor
    # the default (4×): flat at the floor, whatever the wait
    assert w.fee_triple(1_000_000, 90_000, window / 2)[1] == 112_500
    assert w.fee_triple(1_000_000, 90_000, window)[1] == 112_500
    assert w.fee_triple(1_000_000, 90_000, 10 * window)[1] == 112_500
    # with the subsidy pinned OFF the whole line comes back — the charge and the gate are two
    # views of ONE number, read from the setting the gate itself reads.
    monkeypatch.setattr(settings, "max_relayer_subsidy", 1.0)
    assert w.fee_triple(1_000_000, 90_000, window / 2)[1] == 180_000
    assert w.fee_triple(1_000_000, 90_000, window)[1] == 90_000 * int(w.FAR_DATED_MARGIN)
    assert w.fee_triple(1_000_000, 90_000, 10 * window)[1] == 90_000 * int(w.FAR_DATED_MARGIN)
    monkeypatch.setattr(settings, "max_relayer_subsidy", 3.0)
    assert w.fee_triple(1_000_000, 90_000, window)[1] == 112_500  # 3/3 = 1×, floored to 1.25×
    assert w.fee_triple(1_000_000, 90_000, 0)[1] == 112_500  # …never BELOW the ASAP floor
    # and the triple always adds up, whatever the wait
    for ahead in (0, window / 2, window):
        ours, bridge, total = w.fee_triple(1_000_000, 90_000, ahead)
        assert total == 1_000_000 + ours + bridge


def test_the_headroom_never_quotes_below_the_asap_floor(monkeypatch):
    """T33 item 7, paid for at 10:2xZ on 2026-09-10: two ASAP orders were quoted
    `bridge_fee_groth = 14_733` and the release pass seconds later measured **15_793** — a 7 %
    base-fee tick between the quote and the release — so both were held "refusing to cross at a
    loss" on money the user HAD paid for in full.

    An ASAP order carried 1× headroom by construction (`window_margin(0) == 1.0`), i.e. it funded
    exactly today's gas and nothing more, and gas moves between two adjacent blocks. So the
    curve gets a FLOOR: `PGAS_BRIDGE_HEADROOM_MIN`, read through `config.bridge_headroom_min` —
    the same shape as `relayer_subsidy`, where a knob below 1 is refused because quoting under
    today's cost is the outage by another route."""
    from pgasme import config

    window = settings.max_window_s
    assert config.bridge_headroom_min() == 1.25  # the default this deployment ships
    assert w.headroom_for(0) == 1.25
    # ⚠️ T45: the far end is the floor too, because the 4× subsidy divides the 3× margin below
    # it. The floor is the whole curve on this deployment — and the refund at settlement is what
    # makes that honest rather than a shortfall.
    assert w.headroom_for(window) == 1.25
    # the tick that held the live orders is inside the floor now
    assert math.ceil(14_733 * w.headroom_for(0)) >= 15_793

    # a knob below 1 would quote a crossing the treasury loses on at TODAY's gas: refused
    for value in (0, 0.0, None, 0.5):
        monkeypatch.setattr(settings, "bridge_headroom_min", value)
        assert config.bridge_headroom_min() == 1.0, value
        assert w.headroom_for(0) == 1.0, value
    monkeypatch.setattr(settings, "bridge_headroom_min", 2.0)
    assert w.headroom_for(0) == 2.0
    # …and the floor is a FLOOR, never a ceiling: with the subsidy pinned off, the far-dated
    # margin still wins above it
    monkeypatch.setattr(settings, "max_relayer_subsidy", 1.0)
    assert w.headroom_for(window) == w.FAR_DATED_MARGIN


def test_one_reader_of_the_subsidy_prices_the_charge_and_the_gate_alike(monkeypatch):
    """T30c/F2: `PGAS_MAX_RELAYER_SUBSIDY` had TWO readers of one number.

    Here it was `float(x or 0) or 1.0` — 0 means 1× — and in `payouts._payout_scheduled` it was
    the raw setting, where `fee > charged × 0` holds every order ever written. And the two also
    disagreed BELOW 1: this side quoted `1/0.5 = 2×` of today's gas while the gate would refuse
    anything above HALF of what it had just charged for. One function decides what the knob
    means (`config.relayer_subsidy`), both sides call it, and 0 / unset / below 1 are one case."""
    from pgasme import config

    for value in (0, 0.0, None, 0.5, 1.0):
        monkeypatch.setattr(settings, "max_relayer_subsidy", value)
        assert config.relayer_subsidy() == 1.0, value
        assert w.headroom_for(0.0) == 1.25, value  # the ASAP floor, which the subsidy cannot cut
        assert w.fee_triple(1_000_000, 90_000, 0)[1] == 112_500, value
    monkeypatch.setattr(settings, "max_relayer_subsidy", 3.0)  # a real subsidy still counts
    assert config.relayer_subsidy() == 3.0
    assert w.fee_triple(1_000_000, 90_000, settings.max_window_s)[1] == 112_500


def test_the_env_example_documents_the_gate_the_code_actually_runs():
    """The comment beside a knob is the only spec the operator on the box has for it, and this
    one still said the subsidy is measured against "the 2% we actually charged". It has been
    measured against `bridge_fee_groth` — the crossing the user paid for — since 2026-09-10, so
    an operator tuning that number was tuning it against a quantity the code never reads."""
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
    start = next(n for n, ln in enumerate(lines) if ln.startswith("PGAS_MAX_RELAYER_SUBSIDY"))
    block = [lines[start]]
    for ln in lines[start + 1 :]:  # its own indented continuation lines, not the next knob's
        if not ln.startswith(" "):
            break
        block.append(ln)
    text = "\n".join(block)
    assert "bridge_fee_groth" in text
    assert "we actually charged" not in text
    assert "1×" in text  # 0 / unset / below 1 all read as 1×: one reader, one meaning


def test_the_technical_floor_is_the_grid_when_the_knob_is_one(monkeypatch):
    """Design 2026-09-10: `min_amount_groth = max(PGAS_MIN_PAYOUT_GROTH, asset grid)`, and the
    default knob is 1 — a positive amount on the grid, and nothing economic at all."""
    from pgasme.assets import ASSETS

    assert settings.min_payout_groth == 1
    for asset in ASSETS.values():
        assert w.grid_groth(asset) == 1  # 18- and 8-decimal assets: one groth converts exactly
        assert w.min_amount_groth(asset) == 1
    monkeypatch.setattr(settings, "min_payout_groth", 250_000)  # an operator's product rule
    assert w.min_amount_groth(ASSETS["ETH"]) == 250_000


async def test_a_far_dated_order_funds_the_gas_it_will_meet(client, user, monkeypatch, rpc, armed):
    """`live_fees` measures the relayer fee now and the bridge fee is charged from it, so an
    order released now pays exactly `relayer_fee_now`. At release, `payouts` holds the row when
    `live fee > bridge_fee_groth × max_relayer_subsidy` — so ANY gas increase between scheduling
    and release, up to 30 days later, would silently hold an accepted, debited, "delivered at T"
    order forever while the user was told a delivery time.

    ⚠️ **WHO PAYS FOR THAT RISE CHANGED IN T45, AND HOW MUCH IS COVERED DID NOT SHRINK.** It used
    to be pre-paid by the user through a 3×-at-max-window headroom curve, and the treasury kept
    whatever the crossing did not spend. Now the SUBSIDY carries it (4×, the ONE number
    `headroom_for` divides by), the charge is the flat 1.25 floor at every window, and the
    unspent part is refunded at settlement. The invariant this test exists for is stated as
    arithmetic below: what one accepted order can absorb — `bridge_fee_groth × the subsidy` — is
    at least the 3× a full window used to buy."""
    await fund(user, "ETH", 100 * ETH)
    h = user["headers"]
    rpc.gas_gwei = 5.0  # 90_000 groth relayer fee
    w.clear_fees_cache()
    amount = 4_500_000
    r = await client.post("/v1/withdrawals", json=_body(amount=amount), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["bridge_fee_groth"] == 112_500  # 1.25× — the ASAP floor

    far = time.time() + settings.max_window_s - 60
    r = await client.post("/v1/withdrawals", json=_body(amount=amount, deliver_at=far), headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["min_amount_groth"] == 1  # the floor is technical, whatever the wait
    far_bridge = out["items"][0]["bridge_fee_groth"]
    assert far_bridge == 112_500  # the wait is not charged for any more…
    # …and the gate still covers more of a rise than the old curve pre-paid
    assert far_bridge * config.relayer_subsidy() >= 90_000 * w.FAR_DATED_MARGIN
    assert out["items"][0]["total_groth"] == amount + 90_000 + far_bridge
    # an order in the same batch that goes out now is charged exactly the same
    body = {
        "asset": "ETH",
        "items": [
            {"W": _w(), "amount_groth": amount},
            {"W": _w(), "amount_groth": amount, "deliver_at": far},
        ],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 200, r.text
    bridges = [i["bridge_fee_groth"] for i in r.json()["items"]]
    assert bridges == [112_500, 112_500]
    assert tg is not None
