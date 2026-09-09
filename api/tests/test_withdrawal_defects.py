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

import time
from typing import Any

import pytest
from conftest import fund
from eth_account import Account as EthAccount

from pgasme import ledger, tg
from pgasme.config import settings
from pgasme.routers import withdrawals as w

ETH = 100_000_000  # groth
ETA = 66 * 60  # PGAS_BRIDGE_ETA_S
DB = "pgasme_test"


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
        assert r.status_code == 400 and "deliver_at" in r.json()["detail"]
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
    # no order stands, so no order event was written — and the operator was told
    assert await d.events.count_documents({"kind": "withdrawal_requested"}) == 0
    assert await d.events.find_one({"kind": "withdrawal_rolled_back"})
    # THE RETRY. The same body again is one batch of three, not a fourth order for w1.
    again = await client.post("/v1/withdrawals", json=body, headers=h)
    assert again.status_code == 200, again.text
    rows = await d.payout_requests.find({"status": "scheduled"}).to_list(10)
    assert len(rows) == 3 and sorted(r["W"] for r in rows) == sorted([w1, w2, w3])
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - 3_060_000,
        "scheduled": 3_060_000,
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
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - 1_020_000,
        "scheduled": 1_020_000,
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
        "available": ETH - 1_020_000,
        "scheduled": 1_020_000,
        "sent": 0,
    }
    assert await d.events.find_one({"kind": "withdrawal_cancel_refund_failed"})  # never silent
    # the next call finishes what the first one started, instead of 409ing on its own flip
    monkeypatch.setattr(ledger, "cancel", real_cancel)
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=h)
    assert r.status_code == 200 and r.json() == {"cancelled": rid, "refunded_groth": 1_020_000}
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

    await fund(user, "ETH", 1_020_000)  # room for exactly ONE 0.01 ETH payout incl. the 2% fee
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
    assert r.status_code == 503 and "could not verify the destination" in r.json()["detail"]

    rpc.head = 2000  # …and a node that IS answering but is still syncing is not a verdict either
    rpc.syncing = {"currentBlock": "0x1", "highestBlock": "0x7d0"}
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 503 and "could not verify the destination" in r.json()["detail"]
    rpc.syncing = "unreadable"
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 503 and "could not verify the destination" in r.json()["detail"]

    rpc.syncing = False
    assert (await client.post("/v1/withdrawals", json=_body(contract), headers=h)).status_code == 400
    rpc.head = 100  # a restarted provider rewinding far below the head we have already seen
    r = await client.post("/v1/withdrawals", json=_body(contract), headers=h)
    assert r.status_code == 503 and "could not verify the destination" in r.json()["detail"]
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


# ═════════ 10 — the floor is priced for the gas the order will meet, not for today's ═════════


def test_the_derived_floor_scales_with_the_wait(monkeypatch):
    window = settings.max_window_s
    assert w.headroom_for(0) == 1.0
    assert w._min_amount_groth(90_000) == 4_500_000  # release now: today's gas, no headroom
    assert w._min_amount_groth(90_000, window / 2) == 9_000_000
    assert w._min_amount_groth(90_000, window) == 4_500_000 * int(w.FAR_DATED_MARGIN)
    assert w._min_amount_groth(90_000, 10 * window) == 4_500_000 * int(w.FAR_DATED_MARGIN)
    # the release gate the floor is protecting is `fee_groth > charged × max_relayer_subsidy`, so
    # a deployment that already allows a subsidy has already bought the headroom: ONE number.
    monkeypatch.setattr(settings, "max_relayer_subsidy", 3.0)
    assert w._min_amount_groth(90_000, window) == 4_500_000
    assert w._min_amount_groth(90_000) == 4_500_000  # …and it never goes BELOW today's cost


async def test_a_far_dated_order_must_fund_the_gas_it_will_meet(client, user, monkeypatch, rpc, armed):
    """`live_fees` measures the relayer fee now and the floor is derived from it, so an order at
    the floor charges exactly `relayer_fee_now`. At release, `payouts` holds the row when
    `fee_groth > charged × max_relayer_subsidy` (1.0) — so ANY gas increase between scheduling
    and release, up to 30 days later, silently held an accepted, debited, "delivered at T" order
    forever while the user was told a delivery time."""
    await fund(user, "ETH", 100 * ETH)
    h = user["headers"]
    rpc.gas_gwei = 5.0  # 90_000 groth relayer fee → an asap floor of 4_500_000
    w.clear_fees_cache()
    asap = 4_500_000
    assert (await client.post("/v1/withdrawals", json=_body(amount=asap), headers=h)).status_code == 200
    far = time.time() + settings.max_window_s - 60
    r = await client.post("/v1/withdrawals", json=_body(amount=asap, deliver_at=far), headers=h)
    assert r.status_code == 400 and "rise in it before it is due" in r.json()["detail"]
    r = await client.post(
        "/v1/withdrawals", json=_body(amount=asap * 3, deliver_at=far), headers=h
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["min_amount_groth"] == asap  # the form's number is still "release it now"
    assert asap * 2.9 < out["items"][0]["min_amount_groth"] <= asap * 3
    # an order in the same batch that goes out now is not charged for a wait it does not have
    body = {
        "asset": "ETH",
        "items": [
            {"W": _w(), "amount_groth": asap},
            {"W": _w(), "amount_groth": asap * 3, "deliver_at": far},
        ],
        "mode": "direct",
    }
    r = await client.post("/v1/withdrawals", json=body, headers=h)
    assert r.status_code == 200, r.text
    mins = [i["min_amount_groth"] for i in r.json()["items"]]
    assert mins[0] == asap and mins[1] > asap * 2.9
    assert tg is not None
