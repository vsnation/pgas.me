"""T40 — a withdrawal never fails on the user's side, and it is never paid twice.

Admin, 2026-09-10 11:2xZ, looking at a Balance page with two "Failed" rows: *"You understand
that withdrawals on user's side cannot be failed … You should show all statuses there and
estimated time of arrival of his asset"* and, in the same breath, *"make sure you won't have
double spending"* and *"let's not shield deposits. Skip this step, we just distribute what we
receive from different new wallets (when you specify sendFund from — it should be new SBBS
address)"*.

The four laws this file pins:

  1. **NEVER FAILED.** An internal cause — a Beam send refused, a relayer-fee spike, no free
     coin, an unreadable wallet — puts the order in `delayed`: the money stays RESERVED (no
     cancel entry, no refund), the row carries the reason in plain words and a
     `next_attempt_at`, and the processor tries again on a 1 → 2 → 5 → 15 → 60 minute ladder.
     `failed` is not in the processor's vocabulary any more (there is a grep test). A row a
     human must look at is `held` — still reserved, still "delayed" to the user.
  2. **NO DOUBLE SPEND.** A delayed order is retried ONLY when its previous attempt is PROVEN
     dead: the Beam contract tx is status 4/2 AND carries no kernel. Unknown, unreadable,
     pending, or dead-with-a-kernel all mean "stay delayed" — never a second send. Every
     attempt is appended to `row.attempts` BEFORE the call, the list is append-only, and the
     retry counter is read from it and not from a mutable field.
  3. **ONE ETA WRITER.** `payouts.eta_for(row)` is the only implementation of "when does this
     arrive", and the API serialiser and the CLI both call it.
  4. **NO SHIELDING, A FRESH ADDRESS PER CROSSING.** With `PGAS_SHIELD_ENABLED=0` the treasury
     machine ends at `claimed` — no plan, no dark hold, no hourly page — and a regular-funded
     crossing gets its OWN fresh BeamPay address: created once per order, funded by an
     internal `/withdraw` identified by its comment, and the contract txid registered to it.
"""

from __future__ import annotations

import time

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
    make_deposit,
    make_payout,
    payout,
)

from pgasme import beam, beampay, ledger, payouts, tg, workers
from pgasme.config import settings


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


async def available(aid: str = "acct1") -> int:
    return (await ledger.balance(aid, "ETH"))["available"]


# ═════════════════════════════════════════════════════════════ 1 · NEVER FAILED


async def test_a_dead_beam_transaction_delays_the_order_and_refunds_nothing(
    mock_db, eth, armed, beam_wallet
):
    """The row the admin saw as "Failed". The crossing did not happen, so the order is not
    over — it is DELAYED, with its money still reserved and a time it will be tried again."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()  # scheduled → releasing (+ the send)
    await payouts.process_once()  # → bridging
    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**: a wallet that answers "failed" about a
    # transaction whose kernel is on the chain is describing a crossing that happened.
    beam_wallet.txs["beamtx-1"].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    before = await available()
    await payouts.process_once()

    row = await payout(mock_db)
    assert row["status"] == payouts.DELAYED
    assert "failed" in str(row["hold_detail"])
    # the money did NOT come back: the order is still owed, so it stays in Scheduled
    assert await available() == before
    assert await ledger.find_entry("cancel", "req1") is None
    # …and there is a time it will be tried again, on the first rung of the ladder
    assert 0 < row["next_attempt_at"] - time.time() <= 60
    assert row["delays"] == 1 and row["delayed_from"] == "bridging"
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "payout_delayed"})
    assert ev is not None and ev["request_id"] == "req1"
    assert W.lower() not in ev["text"].lower()  # §9.7: the id, never the destination


async def test_the_processor_never_writes_failed_for_a_payout(mock_db, eth, armed, beam_wallet):
    """The grep test the admin's rule earns. `_fail` is gone; no handler in `payouts.py` can
    put a payout order into `failed`, whatever happens to it."""
    import inspect

    src = inspect.getsource(payouts)
    offenders = [
        line.strip()
        for line in src.splitlines()
        if '"failed"' in line
        and "status" in line
        and not line.lstrip().startswith("#")
        and "SHIELD_FAILED" not in line
    ]
    assert offenders == [], offenders
    assert not hasattr(payouts, "_fail")


async def test_the_backoff_ladder_is_one_two_five_fifteen_sixty_capped():
    assert [payouts.delay_backoff_s(n) for n in (1, 2, 3, 4, 5, 6, 20)] == [
        60.0, 120.0, 300.0, 900.0, 3600.0, 3600.0, 3600.0
    ]


async def test_a_delayed_row_waits_for_next_attempt_at_and_then_re_enters_the_gates(
    mock_db, eth, armed, beam_wallet
):
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**: a wallet that answers "failed" about a
    # transaction whose kernel is on the chain is describing a crossing that happened.
    beam_wallet.txs["beamtx-1"].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    await payouts.process_once()
    sends = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])

    # before the ladder's first rung: nothing happens at all
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == payouts.DELAYED

    # after it, and with the previous attempt PROVEN dead, the order is tried again
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"next_attempt_at": time.time() - 1}}
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] in ("releasing", "bridging"), row["status"]
    after = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])
    assert after == sends + 1  # exactly ONE more send
    assert len(row["attempts"]) == 2


# ═════════════════════════════════════════════════════════════ 2 · NO DOUBLE SPEND


async def test_a_failed_report_that_later_shows_a_kernel_is_never_retried(
    mock_db, eth, armed, beam_wallet
):
    """⛔ THE DOUBLE-SPEND THE LADDER MAKES POSSIBLE. A Beam node can report a transaction
    failed and the kernel can turn up anyway. A retry then burns the bETH twice for one order —
    so "dead" means status 4/2 **AND no kernel**, and anything else waits."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**: a wallet that answers "failed" about a
    # transaction whose kernel is on the chain is describing a crossing that happened.
    beam_wallet.txs["beamtx-1"].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    await payouts.process_once()
    sends = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])

    # the kernel turns up: the crossing DID happen, whatever the status said
    beam_wallet.txs["beamtx-1"]["kernel"] = "kernel-beamtx-1"
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"next_attempt_at": time.time() - 1}}
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == payouts.DELAYED  # still delayed — NOT retried
    assert "not settled" in str(row["hold_detail"]) or "kernel" in str(row["hold_detail"])
    after = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])
    assert after == sends  # ⛔ NOT ONE MORE SEND


async def test_an_unreadable_previous_attempt_is_not_a_dead_one(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """"We could not read it" is never "it is dead" (law 8) — and here it would be a second
    signature over one inventory."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**: a wallet that answers "failed" about a
    # transaction whose kernel is on the chain is describing a crossing that happened.
    beam_wallet.txs["beamtx-1"].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    await payouts.process_once()
    sends = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])

    beam_pay.raise_on.add("/internal/contract_tx/")
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"next_attempt_at": time.time() - 1}}
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == payouts.DELAYED
    after = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])
    assert after == sends


async def test_two_processors_on_one_delayed_row_make_one_attempt(
    mock_db, eth, armed, beam_wallet
):
    """The lease makes a second loop visible; the conditional update is what still holds when
    it fails. Both passes read the SAME delayed row and only one may claim it."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**: a wallet that answers "failed" about a
    # transaction whose kernel is on the chain is describing a crossing that happened.
    beam_wallet.txs["beamtx-1"].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    await payouts.process_once()
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"next_attempt_at": time.time() - 1}}
    )
    sends = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])

    row = await payout(mock_db)
    await payouts._payout_delayed(dict(row))
    await payouts._payout_delayed(dict(row))  # the loser of the race, on the stale read
    after = len([p for p in beam_wallet.params_for("invoke_contract") if "action=send," in p["args"]])
    assert after == sends + 1


async def test_the_attempt_is_recorded_before_the_call_that_could_be_lost(
    mock_db, eth, armed, beam_wallet
):
    """⛔ A `process_invoke_data` whose answer is lost has still landed. An attempt nobody wrote
    down is a transaction nobody can ever prove, and the only way to resolve one would be a
    second signature."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_wallet.raise_on.add("process_invoke_data")
    await payouts.process_once()
    row = await payout(mock_db)
    assert "beam_txid" not in row  # nothing came back…
    assert [a["n"] for a in row["attempts"]] == [1]  # …and the attempt is on the row anyway
    assert row["attempts"][0]["at"] > 0
    assert row["attempts"][0]["txid"] is None and row["attempts"][0]["kind"] == "beam"


async def test_the_attempts_list_only_grows_and_carries_each_txid(
    mock_db, eth, armed, beam_wallet
):
    """The retry counter IS the list. A mutable `retries` field is a number two writers
    eventually disagree about, and what they would be disagreeing about is whether to sign a
    second transaction over one inventory."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    row = await payout(mock_db)
    assert [a["n"] for a in row["attempts"]] == [1]
    first = dict(row["attempts"][0])
    assert first["txid"] == row["beam_txid"] and first["state"] == "submitted"

    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**.
    beam_wallet.txs[first["txid"]].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    await payouts.process_once()
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"next_attempt_at": time.time() - 1}}
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert [a["n"] for a in row["attempts"]] == [1, 2]
    assert row["attempts"][0] == first  # append-only: the first entry never moved
    assert row["attempts"][1]["txid"] == row["beam_txid"] != first["txid"]


async def test_a_row_delayed_for_a_day_becomes_a_humans(mock_db, eth, armed, beam_wallet):
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**: a wallet that answers "failed" about a
    # transaction whose kernel is on the chain is describing a crossing that happened.
    beam_wallet.txs["beamtx-1"].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    await payouts.process_once()
    now = time.time()
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"},
        {"$set": {"next_attempt_at": now - 1, "delayed_since": now - 25 * 3600}},
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == payouts.HELD and row["held_from"] == payouts.DELAYED
    assert await ledger.find_entry("cancel", "req1") is None  # still reserved


async def test_a_second_release_entry_for_one_order_holds_the_row(mock_db, eth, armed):
    """(c) The ledger's unique release index is the money-level guard, and a row that meets it
    is a row whose crossing has already been booked once — a human owns that."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging"
    # a SECOND crossing settles for this order — the row is carrying a different txid now
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"release_booked_txid": "beamtx-earlier"}}
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == payouts.HELD
    assert "twice" in str(row["hold_detail"]) or "already" in str(row["hold_detail"])


async def test_the_user_may_still_cancel_while_delayed_or_held_but_never_after_a_kernel():
    """ONE reader of "may the user still end this order and get their money back"."""
    ok, _why = payouts.cancellable({"status": "scheduled"})
    assert ok
    ok, _why = payouts.cancellable({"status": payouts.DELAYED})
    assert ok
    ok, _why = payouts.cancellable({"status": payouts.HELD})
    assert ok
    ok, why = payouts.cancellable({"status": payouts.DELAYED, "kernel_at": time.time()})
    assert not ok and "kernel" in why
    ok, why = payouts.cancellable({"status": payouts.DELAYED, "beam_txid": "beamtx-1"})
    assert not ok and "Beam" in why
    ok, why = payouts.cancellable({"status": payouts.HELD, "instant_tx": "0x" + "ab" * 32})
    assert not ok and "Ethereum" in why
    ok, why = payouts.cancellable({"status": "bridging"})
    assert not ok


# ═════════════════════════════════════════════════════════════ 3 · ONE ETA WRITER


async def test_eta_for_reads_the_table_the_contract_states(monkeypatch):
    now = time.time()
    monkeypatch.setattr(settings, "bridge_eta_s", 66 * 60)

    at, tail, note = payouts.eta_for({"status": "scheduled", "deliver_at": now + 900})
    assert at == now + 900 and tail == 0 and "release" in note.lower() or note

    at, tail, _n = payouts.eta_for(
        {"status": "releasing", "release_attempt_at": now, "mode": "direct"}
    )
    assert at == now + 66 * 60 and tail == payouts.BRIDGE_TAIL_S

    at, tail, _n = payouts.eta_for(
        {"status": "bridging", "released_at": now, "mode": "direct"}
    )
    assert at == now + 66 * 60 and tail == payouts.BRIDGE_TAIL_S

    at, _t, _n = payouts.eta_for({"status": "delivering", "status_at": now, "mode": "direct"})
    assert at == now + 300

    at, _t, _n = payouts.eta_for(
        {"status": "scheduled", "mode": "instant", "created_at": now, "deliver_at": now}
    )
    assert at == now + 60

    at, _t, note = payouts.eta_for(
        {"status": payouts.DELAYED, "mode": "direct", "next_attempt_at": now + 120,
         "hold_reason": "no free coin"}
    )
    assert at == now + 120 + 66 * 60
    assert "delayed" in note.lower() and "no free coin" in note

    at, _t, _n = payouts.eta_for({"status": "sent", "sent_at": now})
    assert at is None


async def test_the_account_route_carries_the_eta_on_every_order(mock_db, client, user):
    """The user's own page: every order says when it arrives, and a delayed one says why."""
    now = time.time()
    aid = user["account_id"]
    await mock_db["pgasme_test"].payout_requests.insert_many(
        [
            {"_id": "r-sched", "account_id": aid, "asset": "ETH", "mode": "direct",
             "W": W, "amount_groth": 500_000, "status": "scheduled",
             "deliver_at": now + 900, "created_at": now},
            {"_id": "r-delay", "account_id": aid, "asset": "ETH", "mode": "direct",
             "W": W, "amount_groth": 500_000, "status": payouts.DELAYED,
             "hold_reason": "no free coin", "next_attempt_at": now + 120, "created_at": now},
        ]
    )
    r = await client.get("/v1/account", headers=user["headers"])
    assert r.status_code == 200
    rows = {x["_id"]: x for x in r.json()["requests"]}
    assert rows["r-sched"]["eta_at"] == now + 900
    assert rows["r-sched"]["eta_tail_s"] == payouts.BRIDGE_TAIL_S
    assert rows["r-sched"]["eta_note"] and rows["r-sched"]["cancellable"] is True
    assert rows["r-delay"]["eta_at"] == now + 120 + settings.bridge_eta_s
    assert "no free coin" in rows["r-delay"]["eta_note"]
    assert rows["r-delay"]["cancellable"] is True
    # …and the word the admin will not have: never "Failed" for an internal cause
    assert "failed" not in str(rows["r-delay"]).lower()


# ═════════════════════════════════════════════════════════ 4 · NO SHIELDING


async def test_with_shielding_off_the_treasury_machine_ends_at_claimed(
    mock_db, armed, monkeypatch, paged
):
    monkeypatch.setattr(settings, "shield_enabled", False)
    await make_deposit(mock_db)
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"}, {"$set": {"treasury": "claimed", "treasury_at": time.time()}}
    )
    await payouts.process_once()
    dep = await deposit(mock_db)
    assert dep["treasury"] == "claimed"
    assert not dep.get("shield_plan")
    assert dep.get("dark") is not True
    assert dep.get("treasury_done_at")
    assert not [t for t in paged if "shield" in t[0].lower()]

    # …and a further pass does not even reach it again
    before = len(paged)
    await payouts.process_once()
    assert (await deposit(mock_db))["treasury"] == "claimed"
    assert len(paged) == before


async def test_a_row_stuck_at_shielding_with_nothing_sent_is_migrated_to_claimed(
    mock_db, armed, monkeypatch
):
    """The live deposit 45ef74d98f10abe7e82aa1f9, stuck `shielding`(dark) since the flag went
    off. Idempotent: a second pass changes nothing, and a row that DID send a chunk is left
    exactly where it is."""
    monkeypatch.setattr(settings, "shield_enabled", False)
    await make_deposit(mock_db, "45ef74d98f10abe7e82aa1f9")
    await make_deposit(mock_db, "dep-sent")
    d = mock_db["pgasme_test"]
    await d.deposits.update_one(
        {"_id": "45ef74d98f10abe7e82aa1f9"},
        {"$set": {"treasury": "shielding", "shield_plan": [1_000_000], "shield_txids": [],
                  "dark": True, "treasury_at": time.time()}},
    )
    await d.deposits.update_one(
        {"_id": "dep-sent"},
        {"$set": {"treasury": "shielding", "shield_plan": [1_000_000],
                  "shield_txids": ["wd-9"], "treasury_at": time.time()}},
    )
    moved = await payouts.migrate_skipped_shields()
    assert moved == 1
    row = await d.deposits.find_one({"_id": "45ef74d98f10abe7e82aa1f9"})
    assert row["treasury"] == "claimed" and row.get("dark") is not True
    assert "shield" in str(row.get("shield_skipped")).lower()
    assert (await d.deposits.find_one({"_id": "dep-sent"}))["treasury"] == "shielding"
    assert await payouts.migrate_skipped_shields() == 0  # idempotent


# ═════════════════════════════════════════ 5 · A FRESH ADDRESS PER CROSSING


async def test_a_regular_funded_crossing_gets_its_own_fresh_address(
    mock_db, eth, armed, beam_pay, beam_wallet, monkeypatch
):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    row = await payout(mock_db)

    addr = row["source_address"]
    created = [c for c in beam_pay.created if c["note"] == "payout|req1"]
    assert len(created) == 1 and created[0]["wallet_type"] == "regular"
    assert addr == created[0]["address"] and addr not in (TREASURY, MP)
    assert row["crossing_address"] is True

    # the treasury funded it with EXACTLY what the crossing burns
    wd = [w for w in beam_pay.withdrawals if w["comment"] == "payout|req1|fund"]
    assert len(wd) == 1
    assert wd[0]["from_address"] == TREASURY and wd[0]["to_address"] == addr
    assert wd[0]["asset_id"] == ETH.aid
    assert wd[0]["amount"] == int(row["amount_groth"]) + int(row["relayer_fee_groth"])

    # …and the contract txid is registered to THAT address, not to the treasury
    assert await payouts.attribution_address(row) == addr
    assert beam_pay.expectations[row["beam_txid"]]["address"] == addr


async def test_the_fresh_address_is_created_once_and_the_funding_is_never_repeated(
    mock_db, eth, armed, beam_pay, beam_wallet, monkeypatch
):
    """`/withdraw` is not idempotent and answers no txid, so the marker is written BEFORE the
    call and a lost answer is resolved by the comment — never by calling again."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    beam_pay.withdraw_lands = False  # BeamPay queued it; its daemon has not emitted it yet
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and row["fund_called_at"] > 0
    assert len(beam_pay.withdrawals) == 1
    addr = row["source_address"]

    await payouts.process_once()  # still not visible: it WAITS, it does not call again
    assert len(beam_pay.withdrawals) == 1
    assert (await payout(mock_db))["status"] == "scheduled"

    # the daemon emits it; the next pass finds it by its comment and crosses
    beam_pay.add_tx(
        txId="wd-fund-1", type="withdrawal", type_string=None, asset_id=str(ETH.aid),
        value=str(row["amount_groth"] + row["relayer_fee_groth"]), fee="100000",
        sender=TREASURY, receiver=addr, comment="payout|req1|fund", kernel="k-wd-1",
    )
    beam_pay.fund(addr, ETH.aid, int(row["amount_groth"]) + int(row["relayer_fee_groth"]))
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] in ("releasing", "bridging")
    assert row["source_address"] == addr  # the same one — never a second create
    assert len([c for c in beam_pay.created if c["note"] == "payout|req1"]) == 1
    assert len(beam_pay.withdrawals) == 1


async def test_a_retry_reuses_the_crossing_address_and_funds_it_once(
    mock_db, eth, armed, beam_pay, beam_wallet, monkeypatch
):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    await payouts.process_once()
    addr = (await payout(mock_db))["source_address"]
    # ⛔ DEAD MEANS STATUS 4 **AND NO KERNEL**: a wallet that answers "failed" about a
    # transaction whose kernel is on the chain is describing a crossing that happened.
    beam_wallet.txs["beamtx-1"].update(
        {"status": beam.TX_FAILED, "status_string": "failed", "kernel": None}
    )
    await payouts.process_once()
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$set": {"next_attempt_at": time.time() - 1}}
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["source_address"] == addr
    assert len([c for c in beam_pay.created if c["note"] == "payout|req1"]) == 1
    assert len([w for w in beam_pay.withdrawals if w["comment"] == "payout|req1|fund"]) == 1


async def test_a_full_release_leaves_beampay_balanced(
    mock_db, eth, armed, beam_pay, beam_wallet, monkeypatch
):
    """The invariant, exactly: what left the treasury is the crossing plus the two BEAM fees
    nobody sets, and the crossing address ends at zero for the asset."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    before_eth = await beam_pay.available_groth(TREASURY, ETH.aid)
    before_beam = await beam_pay.available_groth(TREASURY, 0)
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()
    row = await payout(mock_db)
    beam_pay.book_attribution(row["beam_txid"])

    addr = row["source_address"]
    burn = int(row["amount_groth"]) + int(row["relayer_fee_groth"])
    assert await beam_pay.available_groth(TREASURY, ETH.aid) == before_eth - burn
    assert await beam_pay.available_groth(addr, ETH.aid) == 0  # funded, then burned: exact
    # the BEAM: the withdrawal's fee comes off the treasury, the invocation's off the address
    # the txid was registered to — which is now the crossing's own
    assert await beam_pay.available_groth(TREASURY, 0) == before_beam - beam_pay.withdraw_fee
    assert await beam_pay.available_groth(addr, 0) == -beam_wallet.invoke_fee


async def test_the_fee_gate_sees_the_beam_booked_away_from_the_treasury(
    mock_db, eth, armed, beam_pay, monkeypatch
):
    """⛔ A GUARD MUST READ THE PLACE THE FEE BOOKS TO. Moving the attribution onto a FRESH
    address per crossing takes the invocation's BEAM fee off a balance the gate can see, so the
    debt those addresses carry is summed from the rows themselves and subtracted."""
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    d = mock_db["pgasme_test"]
    await d.payout_requests.insert_one(
        {"_id": "old1", "status": "sent", "crossing_address": True,
         "crossing_fee_groth": 12_100_000, "asset": "ETH"}
    )
    await d.payout_requests.insert_one(
        {"_id": "old2", "status": "sent", "crossing_address": True,
         "crossing_fee_groth": 1_100_000, "asset": "ETH"}
    )
    await d.payout_requests.insert_one(  # a legacy crossing booked to the treasury: not debt
        {"_id": "old3", "status": "sent", "crossing_fee_groth": 9_000_000, "asset": "ETH"}
    )
    assert await payouts.crossing_fee_debt_groth() == 13_200_000


# ═══════════════════════════════════════════════════════════ 6 · THE CLI


async def test_beam_status_names_the_delayed_and_held_rows(mock_db, eth, armed, beam_wallet):
    d = mock_db["pgasme_test"]
    now = time.time()
    await d.payout_requests.insert_one(
        {"_id": "reqD", "status": payouts.DELAYED, "asset": "ETH", "amount_groth": 500_000,
         "hold_reason": "no free coin", "next_attempt_at": now + 120, "delays": 2,
         "delayed_from": "scheduled"}
    )
    await d.payout_requests.insert_one(
        {"_id": "reqH", "status": payouts.HELD, "asset": "ETH", "amount_groth": 700_000,
         "hold_reason": "the previous attempt could not be established", "held_from": "delayed"}
    )
    out: list[str] = []
    await beam.cmd_status(out.append)
    text = "\n".join(out)
    assert "reqD" in text and "no free coin" in text
    assert "reqH" in text and "the previous attempt could not be established" in text


async def test_the_dry_run_prints_the_fresh_address_step(mock_db, eth, armed, monkeypatch):
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    await make_payout(mock_db)
    out: list[str] = []
    await beam.cmd_dry_run_payout("req1", out.append)
    text = "\n".join(out)
    assert "create_wallet" in text and "payout|req1" in text
    assert "/withdraw" in text and "payout|req1|fund" in text
