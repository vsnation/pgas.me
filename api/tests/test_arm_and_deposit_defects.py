"""The MANDATED /arm re-quote policy, and one regression per confirmed defect of the
2026-09-09 quote/deposit review.

Every test here is a thing that was possible before the fix: a Deposit click refused after two
attempts on a drift the third converges on, a deposit credited the amounts of an order the user
never signed, a legitimate deposit failed as a hijack and its lock left unclaimable forever, and
two live router orders built for one quote by a plain double-click.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

import pytest
from conftest import USDC_ARB, XCHAIN_ESTIMATE

from pgasme import workers, xchain
from pgasme.routers import quote as quote_router

Q = {"src_chain_id": 42161, "src_token": USDC_ARB, "amount": "10000000", "target_asset": "ETH"}
ESTIMATE_OUT = 3774812168855201  # the recorded auto-amount estimate
HOOK_GAS_DRIFT = 81_000_000_000_000  # what the recorded hooked call priced the 250k-gas hook at
DB = "pgasme_test"
A_ID = "0x" + "a1" * 32
B_ID = "0x" + "b2" * 32


@pytest.fixture(autouse=True)
def low_floor(monkeypatch):
    from pgasme.config import settings

    monkeypatch.setattr(settings, "min_deposit_wei", 10**15)


@pytest.fixture(autouse=True)
def xchain_index_empty(monkeypatch):
    """The normal case at registration: the router has not indexed the transaction yet."""

    async def order_ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return []

    monkeypatch.setattr(xchain, "order_ids_by_tx", order_ids)


class ArmScript:
    """create_tx like the recorded API, with the two knobs the review's scenarios need:
    `drop` = how far below the ask the NEXT order's recommendedAmount comes back (one drop per
    order, so the re-quote converges), and `next_id` = the orderId that order gets."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.drop = 0
        self.next_id = A_ID
        self.delay = 0.0

    @property
    def orders(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["dstChainTokenOutAmount"] != "auto"]

    async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        if self.delay:
            await asyncio.sleep(self.delay)
        body = copy.deepcopy(XCHAIN_ESTIMATE)
        if params["dstChainTokenOutAmount"] == "auto":
            return body
        amt = int(params["dstChainTokenOutAmount"])
        out = body["estimation"]["dstChainTokenOut"]
        out["amount"] = str(amt)
        out["recommendedAmount"] = str(amt - self.drop)
        self.drop = 0
        body["orderId"] = self.next_id
        return body


async def arm(client, user, quote_id):
    return await client.post(f"/v1/quote/{quote_id}/arm", headers=user["headers"])


async def new_quote(client, user):
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 200, r.text
    return r.json()["quote_id"]


async def register(client, user, quote_id, tx):
    return await client.post(
        "/v1/deposits", json={"quote_id": quote_id, "src_tx_hash": tx}, headers=user["headers"]
    )


# ══════ THE MANDATE — /arm re-quotes on the recommendation up to three times, then 409 ═══════


async def test_arm_requotes_three_times_and_the_third_attempt_converges(
    client, user, armed_eth, monkeypatch
):
    """The decision of record is three attempts; the code did two, so a second-step drift of a
    fraction of a percent — measured at about one Deposit click in three — answered 409 AFTER the
    user had clicked, burning two upstream calls and one of the quote's 20 arm tries, when a
    third attempt at the latest recommendation is accepted."""

    class Drifting(ArmScript):
        def __init__(self) -> None:
            super().__init__()
            self.step = 0

        async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
            if params["dstChainTokenOutAmount"] != "auto":
                amt = int(params["dstChainTokenOutAmount"])
                self.step += 1
                if self.step == 1:
                    self.drop = HOOK_GAS_DRIFT  # the hook's gas, priced in
                elif self.step == 2:
                    self.drop = round(amt * 0.00013)  # the measured 0.013 % second-step drift
                else:
                    self.drop = 0  # the third attempt converges
            return await super().__call__(params)

    fake = Drifting()
    monkeypatch.setattr(xchain, "create_tx", fake)
    qid = await new_quote(client, user)
    r = await arm(client, user, qid)
    assert r.status_code == 200, r.text
    asked = [int(c["dstChainTokenOutAmount"]) for c in fake.orders]
    assert len(asked) == 3  # the estimate's amount, then each latest recommendation
    assert asked[0] == ESTIMATE_OUT
    assert asked[1] == ESTIMATE_OUT - HOOK_GAS_DRIFT
    assert asked[2] == asked[1] - round(asked[1] * 0.00013)
    assert asked[1] < asked[0] and asked[2] < asked[1]  # never above the recommendation
    assert r.json()["estimate"]["out_units"] == str(asked[2])  # the order that stands
    est = r.json()["estimate"]
    assert int(est["value_units"]) + int(est["relayer_fee_units"]) == asked[2]


async def test_arm_gives_up_after_three_attempts_and_names_both_numbers(
    client, user, armed_eth, monkeypatch
):
    """Bounded on purpose: a recommendation that moves on every call is chased three times and
    no further, and the refusal names what we asked for and what the router now recommends."""

    class AlwaysMoving(ArmScript):
        async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
            if params["dstChainTokenOutAmount"] != "auto":
                self.drop = HOOK_GAS_DRIFT
            return await super().__call__(params)

    fake = AlwaysMoving()
    monkeypatch.setattr(xchain, "create_tx", fake)
    qid = await new_quote(client, user)
    r = await arm(client, user, qid)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "quote moved" in detail
    asked = [int(c["dstChainTokenOutAmount"]) for c in fake.orders]
    assert len(asked) == 3 and asked == [
        ESTIMATE_OUT,
        ESTIMATE_OUT - HOOK_GAS_DRIFT,
        ESTIMATE_OUT - 2 * HOOK_GAS_DRIFT,
    ]
    assert str(asked[2]) in detail  # what we asked for
    assert str(asked[2] - HOOK_GAS_DRIFT) in detail  # what it now recommends


# ═══ 11 — a deposit is worth what the order it carries locked, not the quote's latest arm ═════


async def test_the_row_is_priced_by_the_order_that_matched_not_by_the_quotes_tip(
    client, user, armed_eth, mock_db, monkeypatch
):
    """`arm_xchain` overwrites the quote's ONE amount snapshot on every arm while deliberately
    keeping every order registrable, and `create` copied the quote's CURRENT numbers onto the
    row. So a user who signed the FIRST order got a row describing the SECOND: measured +8,100
    groth of house money credited over the fill of one $10 deposit (the gap is the router pricing
    the 250k-gas hook into `recommendedAmount`, so it scales with gas), and the mirror case made
    `scanner._sane` refuse the lock — real money in our pipe with no automatic path to credit."""
    fake = ArmScript()
    monkeypatch.setattr(xchain, "create_tx", fake)
    monkeypatch.setattr(quote_router, "ARM_FRESH_S", 0.0)
    qid = await new_quote(client, user)
    fake.next_id = A_ID
    a = (await arm(client, user, qid)).json()
    fake.next_id, fake.drop = B_ID, HOOK_GAS_DRIFT  # gas moved between the two clicks
    b = (await arm(client, user, qid)).json()
    assert a["order_id"] == A_ID and b["order_id"] == B_ID
    assert a["estimate"]["out_groth"] != b["estimate"]["out_groth"]
    d = mock_db[DB]
    assert (await d.quotes.find_one({"_id": qid}))["value_groth"] == b["estimate"]["out_groth"]

    async def indexed_a(tx_hash: str, timeout: float | None = None) -> list[str]:
        return [A_ID]  # the user signed the FIRST order and the router has indexed it

    monkeypatch.setattr(xchain, "order_ids_by_tx", indexed_a)
    r = await register(client, user, qid, "0x" + "cd" * 32)
    assert r.status_code == 200, r.text
    dep = await d.deposits.find_one({"_id": r.json()["deposit_id"]})
    assert dep["order_id"] == A_ID and dep["verified"] is True
    # the row describes the order that will actually fill — the number the chain will prove
    assert dep["value_groth"] == a["estimate"]["out_groth"]
    assert dep["eth"]["value_units"] == a["estimate"]["value_units"]
    assert dep["eth"]["relayer_fee_units"] == a["estimate"]["relayer_fee_units"]


async def test_a_quote_whose_deposit_is_registered_can_no_longer_be_re_armed(
    client, user, armed_eth, mock_db, monkeypatch
):
    """The exploitable sequence was (arm · register · wait for gas to rise · arm again · sign the
    second order): the second arm re-priced the quote UNDER a row already written from it, and
    the scanner resolves that fill through the quote while `_credit` pays the ROW. Caller
    controlled and repeatable. A registered quote has done its job."""
    fake = ArmScript()
    monkeypatch.setattr(xchain, "create_tx", fake)
    monkeypatch.setattr(quote_router, "ARM_FRESH_S", 0.0)
    qid = await new_quote(client, user)
    armed = (await arm(client, user, qid)).json()
    r = await register(client, user, qid, "0x" + "ef" * 32)
    assert r.status_code == 200, r.text
    fake.next_id, fake.drop = B_ID, HOOK_GAS_DRIFT
    again = await arm(client, user, qid)
    assert again.status_code == 409 and "already has a registered deposit" in again.json()["detail"]
    assert len(fake.orders) == 1  # no second live order was ever built
    d = mock_db[DB]
    q = await d.quotes.find_one({"_id": qid})
    dep = await d.deposits.find_one({"quote_id": qid})
    # the quote and the row still describe ONE order — the one the user was handed
    assert q["value_groth"] == dep["value_groth"] == armed["estimate"]["out_groth"]
    assert q["order_id"] == dep["order_id"] == A_ID


# ══ 12 — an earlier order of the same quote is this quote's deposit, not a stranger's ════════


async def test_an_earlier_order_of_the_same_quote_is_not_failed_as_a_hijack(
    client, user, armed_eth, mock_db, monkeypatch
):
    """When the router has not indexed the transaction yet — the normal case, and the whole reason
    `verified` starts false — the row is stamped with the LATEST armed order. `_step_submitted`
    then re-checked that single id, so a user who signed an EARLIER order of the SAME quote was
    failed as a hijack: status `failed`, hash released, a REJECTED page — and the fill's lock
    then resolved to the quote, found a row that is not claimable, and went to
    unattributed_locks with MANUAL HANDLING REQUIRED. Re-registering rebuilt the same wrong row,
    so the loop never resolved itself."""
    fake = ArmScript()
    monkeypatch.setattr(xchain, "create_tx", fake)
    monkeypatch.setattr(quote_router, "ARM_FRESH_S", 0.0)
    qid = await new_quote(client, user)
    fake.next_id = A_ID
    a = (await arm(client, user, qid)).json()
    fake.next_id, fake.drop = B_ID, HOOK_GAS_DRIFT
    b = (await arm(client, user, qid)).json()
    tx = "0x" + "cc" * 32
    r = await register(client, user, qid, tx)  # the router cannot say yet: unverified
    assert r.status_code == 200, r.text
    dep_id = r.json()["deposit_id"]
    d = mock_db[DB]
    dep = await d.deposits.find_one({"_id": dep_id})
    assert dep["verified"] is False and dep["order_id"] == B_ID  # the latest, best guess
    assert dep["order_ids_armed"] == [B_ID, A_ID]

    async def indexed_a(tx_hash: str, timeout: float | None = None) -> list[str]:
        return [A_ID]  # …and the user had signed the FIRST one

    monkeypatch.setattr(xchain, "order_ids_by_tx", indexed_a)
    await d.deposits.update_one({"_id": dep_id}, {"$set": {"created_at": time.time() - 3600}})
    await workers.xchain_secondary()
    dep = await d.deposits.find_one({"_id": dep_id})
    assert dep["status"] == "order_seen" and dep["verified"] is True
    assert dep.get("src_tx_hash") == tx  # the hash was never released
    assert dep["order_id"] == A_ID  # the id that matched is adopted…
    assert dep["value_groth"] == a["estimate"]["out_groth"] != b["estimate"]["out_groth"]
    assert dep["eth"]["value_units"] == a["estimate"]["value_units"]  # …with ITS amounts
    assert await d.events.count_documents({"kind": "deposit_mismatch"}) == 0
    ev = await d.events.find_one({"kind": "deposit_order_seen"})
    assert ev and ev["deposit_id"] == dep_id


async def test_a_stranger_s_order_is_still_a_mismatch(
    client, user, armed_eth, mock_db, monkeypatch
):
    """The widened match is the quote's OWN ids and nothing else."""
    fake = ArmScript()
    monkeypatch.setattr(xchain, "create_tx", fake)
    qid = await new_quote(client, user)
    await arm(client, user, qid)
    tx = "0x" + "dd" * 32
    dep_id = (await register(client, user, qid, tx)).json()["deposit_id"]

    async def stranger(tx_hash: str, timeout: float | None = None) -> list[str]:
        return ["0x" + "99" * 32]

    monkeypatch.setattr(xchain, "order_ids_by_tx", stranger)
    d = mock_db[DB]
    await d.deposits.update_one({"_id": dep_id}, {"$set": {"created_at": time.time() - 3600}})
    await workers.xchain_secondary()
    dep = await d.deposits.find_one({"_id": dep_id})
    assert dep["status"] == "failed" and dep.get("src_tx_hash") is None
    assert dep["order_id"] == A_ID and "deposit mismatch" in dep["note"]
    assert await d.events.find_one({"kind": "deposit_mismatch"})


# ═════════════ 13 — two concurrent /arm calls place ONE order, not two live ones ══════════════


async def test_two_concurrent_arms_build_one_order(client, user, armed_eth, mock_db, monkeypatch):
    """`q.get("tx") and q.get("order_id") and fresh` is a read-then-write, evaluated by both
    requests on a document neither had written yet — so a plain double-click built TWO live
    router orders for one quote and handed one caller a transaction whose id and amounts the
    quote no longer carries (findings 11 and 12, with no 180-second wait and no client bug), and
    paid for a second create-tx every time."""
    fake = ArmScript()
    fake.delay = 0.05  # the upstream call the second request must not duplicate
    monkeypatch.setattr(xchain, "create_tx", fake)
    monkeypatch.setattr(quote_router, "ARM_POLL_S", 0.005)
    qid = await new_quote(client, user)
    a, b = await asyncio.gather(arm(client, user, qid), arm(client, user, qid))
    assert [a.status_code, b.status_code] == [200, 200], (a.text, b.text)
    assert a.json()["order_id"] == b.json()["order_id"] == A_ID
    assert a.json()["tx"] == b.json()["tx"]
    assert len(fake.orders) == 1  # ONE router order, not two
    q = await mock_db[DB].quotes.find_one({"_id": qid})
    assert q["order_ids_armed"] == [A_ID] and len(q["orders_armed"]) == 1
    assert q.get("arm_in_flight") is None  # the claim is always given back
    # …and the caller who waited was handed the order the quote actually holds
    assert q["order_id"] == a.json()["order_id"] and q["tx"] == b.json()["tx"]
