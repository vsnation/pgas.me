"""The remaining findings of the 2026-09-09 Scheduling review: a refusal that renders as a 500.

Three defects, one shape. Something that is NOT the payout — a belt read, an operator event, a
NaN in a legacy row — raised where the money path had already succeeded, and FastAPI turned it
into a 5xx. A 5xx is the one answer this endpoint must never give once the rows are live: it
tells the caller their withdrawal FAILED while the worker pays it, and it withholds the ids they
would need to cancel with, so the ordinary retry-on-500 schedules the whole batch a second time.

  §A  POST /v1/withdrawals, after `_write_items` has committed: the post-write belt read and the
      per-order event loop. Either may fail; neither may become the caller's error. The belt may
      still REVERSE the batch — that is a 409 naming the ids it reversed, a refusal, not a
      failure — and a roll-back that cannot finish names what still stands instead of raising.
  §B  a bare `NaN` / `Infinity` / `1e400` in the body. JSON has no NaN literal, python accepts
      one anyway, and Starlette renders with `allow_nan=False` — so the 422 that correctly
      refused it died at encode time and the client got a 500 for a request already refused.
  §C  a legacy `payout_requests` row carrying `deliver_at: NaN` (written before that guard
      existed) made GET /v1/account 500 for that account FOREVER — hiding the very order the
      user needed to find in order to cancel it.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import pytest
from conftest import fund
from eth_account import Account as EthAccount

from pgasme import ledger, tg
from pgasme.config import settings
from pgasme.db import db
from pgasme.routers import withdrawals as w

ETH = 100_000_000  # groth
DB = "pgasme_test"
# FakeRpc answers 1 gwei → an 18_000-groth b2e relayer fee. An order released NOW no longer
# carries 1× headroom: since 2026-09-10 the curve has a FLOOR (`PGAS_BRIDGE_HEADROOM_MIN`, 1.25)
# because two ASAP orders quoted at exactly today's gas were held by a 7% base-fee tick seconds
# later. So the bridge fee an ASAP order is charged is derived here from the one function that
# prices it, never re-spelled as a literal (law 9).
RELAYER = 18_000
BRIDGE = math.ceil(RELAYER * w.headroom_for(0))
TOTAL_1M = 1_000_000 + 20_000 + BRIDGE  # amount + our 2% + the bridge fee it funds
JSON_CT = {"Content-Type": "application/json"}


def _w() -> str:
    return EthAccount.create().address


def _body(*dests: str, amount: int = 1_000_000) -> dict[str, Any]:
    return {
        "asset": "ETH",
        "mode": "direct",
        "items": [{"W": d, "amount_groth": amount} for d in (dests or (_w(),))],
    }


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(settings, "payout_direct_enabled", True)


def _after_the_rows_exist(monkeypatch, answer: Any):
    """Make `ledger.balance` misbehave ONLY once a payout row exists for that account.

    Counting calls would pin the test to the order of three reads inside `create`; this pins it
    to the thing that actually matters — the batch is committed, so from here nothing may 5xx."""
    real = ledger.balance

    async def flaky(account_id: str, asset: str) -> dict[str, int]:
        if await db().payout_requests.find_one({"account_id": account_id}):
            if isinstance(answer, Exception):
                raise answer
            return answer
        return await real(account_id, asset)

    monkeypatch.setattr(ledger, "balance", flaky)


# ═════════════════ §A — once the rows are live, nothing may turn into a 5xx ══════════════════


async def test_an_event_that_will_not_write_still_answers_200_with_the_ids(
    client, user, mock_db, monkeypatch, armed
):
    """(a) The `tg.queue` loop ran AFTER the batch was committed and outside any guard, so a
    Mongo blip on the `events` collection answered 500 for two live, debited, correctly-written
    payouts — and the caller never saw an id. An event is an observation; a payout is money."""
    await fund(user, "ETH", ETH)
    d = mock_db[DB]
    dest_a, dest_b = _w(), _w()

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("events collection is gone")

    monkeypatch.setattr(tg, "queue", boom)
    r = await client.post("/v1/withdrawals", json=_body(dest_a, dest_b), headers=user["headers"])

    assert r.status_code == 200, r.text
    ids = r.json()["request_ids"]
    assert len(ids) == 2
    rows = await d.payout_requests.find({"_id": {"$in": ids}}).to_list(10)
    assert len(rows) == 2 and {x["status"] for x in rows} == {"scheduled"}
    assert await ledger.balance(user["account_id"], "ETH") == {
        "available": ETH - 2 * TOTAL_1M,
        "scheduled": 2 * TOTAL_1M,
        "sent": 0,
    }
    # ONE page, sent immediately, naming exactly the orders whose event is missing…
    alerts = await d.events.find({"kind": "withdrawal_events_unwritten"}).to_list(10)
    assert len(alerts) == 1
    assert alerts[0]["request_ids"] == ids
    assert alerts[0]["notified"] is True and alerts[0]["immediate"] is True
    # …and §9.7: ids only. Neither destination may appear anywhere in the operator's row.
    blob = json.dumps(alerts[0], default=str)
    assert dest_a not in blob and dest_b not in blob


async def test_a_belt_read_that_raises_still_answers_200_with_the_ids(
    client, user, mock_db, monkeypatch, armed
):
    """(b) The overdraft belt re-reads the balance after the write. An unreadable balance is not
    a negative balance (law 8) — it cannot reverse a correctly-written batch, and it must not
    500 either. It says so loudly and hands the caller the ids."""
    await fund(user, "ETH", ETH)
    d = mock_db[DB]
    _after_the_rows_exist(monkeypatch, RuntimeError("the primary stepped down"))
    r = await client.post("/v1/withdrawals", json=_body(), headers=user["headers"])

    assert r.status_code == 200, r.text
    ids = r.json()["request_ids"]
    assert len(ids) == 1
    row = await d.payout_requests.find_one({"_id": ids[0]})
    assert row and row["status"] == "scheduled"  # not reversed on a reading we never got
    alerts = await d.events.find({"kind": "withdrawal_belt_unread"}).to_list(10)
    assert len(alerts) == 1 and alerts[0]["request_ids"] == ids
    assert alerts[0]["notified"] is True and alerts[0]["immediate"] is True
    # the order still got its own event: the belt failing is not the event loop failing
    assert await d.events.count_documents({"kind": "withdrawal_requested"}) == 1


async def test_a_belt_that_reads_negative_reverses_the_batch_and_names_what_it_reversed(
    client, user, mock_db, monkeypatch, armed
):
    """(c) The belt's own refusal: the balance went negative, so the batch must not stand. That
    is a 409 — a refusal, not a failure — and it names the ids it reversed, because "the
    withdrawal was rolled back" is unactionable if the user cannot tell WHICH orders went."""
    await fund(user, "ETH", ETH)
    d = mock_db[DB]
    _after_the_rows_exist(monkeypatch, {"available": -1, "scheduled": 0, "sent": 0})
    r = await client.post("/v1/withdrawals", json=_body(_w(), _w()), headers=user["headers"])

    assert r.status_code == 409, r.text
    rows = await d.payout_requests.find({}).to_list(10)
    ids = [x["_id"] for x in rows]
    assert len(ids) == 2
    detail = r.json()["detail"]
    assert "rolled back" in detail and all(i in detail for i in ids)
    assert sorted(r.headers["X-Reversed-Request-Ids"].split(",")) == sorted(ids)
    # the rows are cancelled, and they say why — nothing was deleted
    assert {x["status"] for x in rows} == {"cancelled"}
    assert all(x["rolled_back"] is True for x in rows)
    # every debit that landed has its offsetting entry, and the money is back in Available
    scheduled = await d.entries.find({"kind": "schedule"}).to_list(10)
    cancels = await d.entries.find({"kind": "cancel"}).to_list(10)
    assert sorted(e["ref"] for e in scheduled) == sorted(ids)
    assert sorted(e["ref"] for e in cancels) == sorted(ids)
    assert all(e["refund_of"] == f"schedule:{e['ref']}" for e in cancels)
    # the bridge-fee half of the debit is reversed too, in the one `cancel` that mirrors both
    bridges = await d.entries.find({"kind": "schedule_bridge_fee"}).to_list(10)
    assert sorted(e["ref"] for e in bridges) == sorted(ids)
    assert all(c["groth"] == TOTAL_1M for c in cancels)
    assert (await ledger.balances(user["account_id"]))["ETH"] == {
        "available": ETH,
        "scheduled": 0,
        "sent": 0,
    }
    assert await d.events.find_one({"kind": "withdrawal_rolled_back"})


async def test_a_roll_back_that_cannot_finish_is_still_a_409_naming_what_stands(
    client, user, mock_db, monkeypatch, armed
):
    """The belt refuses, the refund will not write, and the pager is down too. Every one of
    those used to be an exception on a live row. `_roll_back` never raises: it names the ids it
    could not reverse, and the caller gets a 409 that tells them exactly which orders stand."""
    await fund(user, "ETH", ETH)
    d = mock_db[DB]
    _after_the_rows_exist(monkeypatch, {"available": -1, "scheduled": 0, "sent": 0})

    async def no_refund(*a: Any, **k: Any) -> None:
        raise RuntimeError("entries collection is gone")

    async def no_pager(*a: Any, **k: Any) -> None:
        raise RuntimeError("telegram row could not be written either")

    monkeypatch.setattr(ledger, "cancel", no_refund)
    monkeypatch.setattr(tg, "alert", no_pager)
    r = await client.post("/v1/withdrawals", json=_body(), headers=user["headers"])

    assert r.status_code == 409, r.text
    ids = [x["_id"] for x in await d.payout_requests.find({}).to_list(10)]
    assert len(ids) == 1
    detail = r.json()["detail"]
    assert "still stand" in detail and ids[0] in detail
    assert r.headers["X-Reversed-Request-Ids"] == ""  # nothing was PROVEN reversed
    # the truth is told, not tidied: the debit is still there and the row says it was rolled back
    assert await ledger.find_entry("schedule", ids[0]) is not None
    assert await ledger.find_entry("cancel", ids[0]) is None


async def test_a_reservation_that_cannot_be_given_back_is_not_the_callers_error(
    client, user, mock_db, monkeypatch, armed
):
    """`finally: await release(...)` sat OUTSIDE the PartialBatch handler, so a failure there
    escaped as a 500 with the ids already written. The cushion is not the money: `reserve`'s
    stale-clear repairs it within RESERVATION_STALE_S."""
    await fund(user, "ETH", ETH)

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("reservations collection is gone")

    monkeypatch.setattr(w, "release", boom)
    r = await client.post("/v1/withdrawals", json=_body(), headers=user["headers"])

    assert r.status_code == 200, r.text
    ids = r.json()["request_ids"]
    row = await mock_db[DB].payout_requests.find_one({"_id": ids[0]})
    assert row and row["status"] == "scheduled"


# ═══════════════ §B — a non-finite number in the body is a 422, never a 500 ══════════════════


def _raw(dest: str, tail: str) -> bytes:
    """A withdrawal body written BY HAND. `json.dumps` cannot emit a bare `NaN` — which is the
    whole point: this is the body a client with a laxer JSON encoder actually sends."""
    return ('{"asset":"ETH","mode":"direct","items":[{"W":"' + dest + '",' + tail + "}]}").encode()


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e400", "-1e400"])
async def test_a_non_finite_number_in_the_body_is_a_422_and_moves_nothing(
    client, user, mock_db, armed, literal
):
    """JSON has no NaN literal — python's `json` accepts one anyway, and Starlette renders with
    `allow_nan=False`, so the 422 that refused it raised at ENCODE time and the client saw a 500.
    `1e400` needs no literal at all: it parses straight to `inf`."""
    await fund(user, "ETH", ETH)
    d = mock_db[DB]
    r = await client.post(
        "/v1/withdrawals",
        content=_raw(_w(), '"amount_groth":1000000,"deliver_at":' + literal),
        headers={**user["headers"], **JSON_CT},
    )
    assert r.status_code == 422, r.text
    assert "finite" in r.json()["detail"]
    assert await d.payout_requests.count_documents({}) == 0
    assert await d.entries.count_documents({"kind": "schedule"}) == 0


async def test_a_non_finite_amount_is_refused_the_same_way(client, user, mock_db, armed):
    await fund(user, "ETH", ETH)
    r = await client.post(
        "/v1/withdrawals",
        content=_raw(_w(), '"amount_groth":NaN'),
        headers={**user["headers"], **JSON_CT},
    )
    assert r.status_code == 422
    assert await mock_db[DB].payout_requests.count_documents({}) == 0


async def test_an_ordinary_body_is_replayed_untouched_and_a_broken_one_still_gets_its_422(
    client, user, mock_db, armed
):
    """The guard buffers the body to read it. If it did not hand those exact bytes on, every
    POST would break — and a body that is not JSON at all is not this guard's business."""
    await fund(user, "ETH", ETH)
    dest = _w()
    r = await client.post(
        "/v1/withdrawals",
        content=_raw(dest, '"amount_groth":1000000'),
        headers={**user["headers"], **JSON_CT},
    )
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["W"] == dest

    broken = await client.post(
        "/v1/withdrawals", content=b"{not json at all", headers={**user["headers"], **JSON_CT}
    )
    assert broken.status_code == 422  # FastAPI's own answer, not ours, and never a 500


# ═════════ §C — a legacy poisoned row degrades one field, never the whole account ════════════


async def _poisoned_order(user: dict[str, Any], rid: str = "legacy-nan") -> str:
    now = time.time()
    await db().payout_requests.insert_one(
        {
            "_id": rid,
            "account_id": user["account_id"],
            "asset": "ETH",
            "mode": "direct",
            "W": _w(),
            "amount_groth": 1_000_000,
            "fee_groth": 20_000,
            "deliver_at": float("nan"),  # written before `allow_inf_nan=False` existed
            "release_at": float("nan"),
            "status": "scheduled",
            "created_at": now,
            "updated_at": now,
        }
    )
    # a legacy row: written before the bridge fee was itemised, so its debit is amount + fee only
    await ledger.schedule(user["account_id"], "ETH", 1_020_000, rid, "legacy row")
    return rid


async def test_a_poisoned_row_still_renders_and_says_which_fields_it_could_not_read(
    client, user, mock_db, armed
):
    """GET /v1/account was 500 for that account FOREVER — so the one order the user needed to
    find in order to cancel it was the one thing they could not see."""
    await fund(user, "ETH", ETH)
    rid = await _poisoned_order(user)
    r = await client.get("/v1/account", headers=user["headers"])

    assert r.status_code == 200, r.text
    row = next(q for q in r.json()["requests"] if q["_id"] == rid)
    assert row["deliver_at"] is None and row["release_at"] is None
    assert row["unreadable_fields"] == ["deliver_at", "release_at"]
    assert row["amount_groth"] == 1_000_000  # everything readable is still read


async def test_the_poisoned_row_can_still_be_cancelled(client, user, mock_db, armed):
    """Rendering is not the point on its own — being able to act on it is."""
    await fund(user, "ETH", ETH)
    rid = await _poisoned_order(user)
    r = await client.post(f"/v1/withdrawals/{rid}/cancel", headers=user["headers"])

    assert r.status_code == 200, r.text
    assert r.json() == {"cancelled": rid, "refunded_groth": 1_020_000}
    assert (await ledger.balances(user["account_id"]))["ETH"]["available"] == ETH


async def test_a_poisoned_deposit_total_does_not_take_the_account_with_it(
    client, user, mock_db, armed
):
    """`int(float('nan'))` RAISES: one poisoned `value_groth` inside the `pending` aggregate was
    the whole account again. The row is still shown — a booked arrival must not make its own
    evidence invisible — with the number it could not read blanked."""
    await fund(user, "ETH", ETH)
    await db().deposits.insert_one(
        {
            "_id": "dep-nan",
            "account_id": user["account_id"],
            "asset": "ETH",
            "mode": "xchain",
            "status": "confirming",
            "value_groth": float("nan"),
            "created_at": time.time(),
        }
    )
    r = await client.get("/v1/account", headers=user["headers"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["balances"]["ETH"]["pending"] == 0
    dep = next(d for d in body["deposits"] if d["_id"] == "dep-nan")
    assert dep["value_groth"] is None and dep["status"] == "confirming"
