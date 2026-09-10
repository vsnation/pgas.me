"""One hold, one page — and the reminder carries the whole instruction.

2026-09-10 00:1x–00:2xZ, ONE deposit whose shield chunk failed produced TWO pages seconds
apart: `payouts._hold_for_a_human` alerted when it parked the row, and the very next
`workers.stuck_checks()` pass alerted again from the parked-for-a-human branch, which had no
idea the hold had already spoken. The second one was cut at 200 characters — mid-sentence, and
before the `replan-shield` command that resolves the hold. A pager that repeats itself and
truncates the fix is a pager the operator learns to skim.

  the hold stamps `hold_paged_at` in the same call that pages (`workers.held_paged_at`) — one
    writer, and `payouts._hold` (the ordinary WAITING hold, which a handler still retries) is
    NOT it: a fee-budget wait must not mute the pager for money parked for a human
  the reminder is due HELD_SLA_S after THAT page, not HELD_SLA_S after the monitor noticed
  the reminder it sends is stamped ON THE ROW (`held_reminded_at`), so a restart — which empties
    the in-memory tg cooldown — cannot page every held row again
  the reminder leads with the row id and carries the hold reason in full
  a cut, if one is ever needed, is marked and never lands inside a command
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from pgasme import payouts, tg, workers

# The real reason `payouts._payout_shielding` writes — 470 characters, with the command that
# fixes it at the END, which is exactly what the old [:200] threw away.
REASON = (
    "a shield chunk failed on Beam: chunk 1/2 is failed (tx beamtx-7), so that value is still "
    "UNSHIELDED and nothing is auto-retried. Run `python -m pgasme.beam replan-shield "
    "--deposit dep1` to see the chunk table, then the same command with --apply to re-send the "
    "failed chunk(s) to fresh max-privacy addresses"
)
COMMAND = "python -m pgasme.beam replan-shield --deposit dep1"

Call = tuple[str, "str | None", float]  # (text, cooldown key, cooldown_s)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Every (text, key, cooldown_s) tg.send was asked for, in order.

    ⚠️ Patching `tg.send` REPLACES its per-key cooldown, so these are the pages the stuck check
    *asks* for. What holds a due reminder to one per SLA is that cooldown, keyed
    `held:<coll>:<id>` — pinned separately, unpatched, in the last test here."""
    out: list[Call] = []

    async def fake_send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        out.append((text, key, cooldown_s))
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    return out


def held(calls: list[Call]) -> list[str]:
    """Only the HELD pages: a stuck-check pass legitimately says other things too."""
    return [t for t, _, _ in calls if t.startswith("HELD:")]


async def hold_a_deposit(mock_db: Any, dep_id: str = "dep1") -> None:
    """A deposit mid-shield, then held exactly the way the shield handler holds it."""
    now = time.time()
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": dep_id,
            "account_id": "acct1",
            "asset": "ETH",
            "status": "credited",
            "treasury": "shielding",
            "treasury_at": now - 7200,
            "updated_at": now - 7200,
            "value_groth": 1_200_000,
        }
    )
    await payouts._hold_for_a_human(
        "deposits", dep_id, "treasury", "shielding", REASON, "deposit_shield_failed", "deposit_id"
    )


async def rewind(mock_db: Any, coll: str, row_id: str, field: str, seconds: float) -> None:
    """Move this row's hold — its page, its last reminder and its status clock — into the past.

    Every stamp the reminder reads, so the row is exactly as it would be `seconds` later; moving
    only some of them would test a state the code can never actually be in."""
    c = mock_db["pgasme_test"][coll]
    row = await c.find_one({"_id": row_id})
    moved = {
        k: row[k] - seconds
        for k in ("hold_at", "hold_paged_at", "held_reminded_at", f"{field}_at")
        if row.get(k)
    }
    await c.update_one({"_id": row_id}, {"$set": moved})


async def test_a_fresh_hold_pages_once_and_the_stuck_check_stays_quiet(mock_db, calls):
    await hold_a_deposit(mock_db)
    assert len(held(calls)) == 1
    assert COMMAND in held(calls)[0]  # the hold's own page always carried the whole reason
    d = mock_db["pgasme_test"]
    row = await d.deposits.find_one({"_id": "dep1"})
    assert row["treasury"] == "held" and row["hold_at"] > 0
    # the page and its stamp are the same call: `hold_paged_at` is what the reminder reads
    assert row["hold_paged_at"] == row["hold_at"] and "held_reminded_at" not in row
    # …and the monitor does not announce the same hold a second time, however often it runs
    for _ in range(3):
        await workers.stuck_checks()
    assert len(held(calls)) == 1
    # the event row the hold wrote is the record either way
    assert await d.events.count_documents({"kind": "deposit_shield_failed"}) == 1


async def test_the_reminder_arrives_once_the_sla_has_passed_since_that_page(mock_db, calls):
    await hold_a_deposit(mock_db)
    calls.clear()
    # one second short of the SLA: the hold's own page is still the most recent word
    await rewind(mock_db, "deposits", "dep1", "treasury", workers.HELD_SLA_S - 1)
    await workers.stuck_checks()
    assert held(calls) == []
    # …and one second past it, exactly one reminder
    await rewind(mock_db, "deposits", "dep1", "treasury", 2)
    await workers.stuck_checks()
    assert len(held(calls)) == 1
    text = held(calls)[0]
    assert text.startswith("HELD: <code>dep1</code>")  # the id leads
    assert "deposit parked for a human" in text and "for over 6 h" in text
    assert COMMAND in text and text.endswith("max-privacy addresses")  # in full, to the last word
    assert tg.CUT_MARKER not in text
    # and it is asked for under the key whose cooldown holds it to one page per SLA
    key_and_cooldown = [(k, c) for t, k, c in calls if t.startswith("HELD:")]
    assert key_and_cooldown == [("held:deposits:dep1", workers.HELD_SLA_S)]


async def test_a_row_that_cannot_prove_it_was_paged_is_said_out_loud(mock_db, calls):
    """The safe failure mode: a held row with no hold stamp (held by an older build, or by
    hand) has no evidence anyone was ever told, so it is told. Silence is the one answer a held
    row must never get."""
    await mock_db["pgasme_test"].deposits.insert_one(
        {"_id": "dep2", "treasury": "held", "hold_reason": REASON, "updated_at": time.time()}
    )
    assert workers.held_paged_at({"treasury": "held"}) == 0.0
    # …not even a `hold_at` from an ordinary WAITING hold counts as proof of a page
    assert workers.held_paged_at({"treasury": "held", "hold_at": time.time()}) == 0.0
    await workers.stuck_checks()
    assert len(held(calls)) == 1 and held(calls)[0].startswith("HELD: <code>dep2</code>")
    assert "for over" not in held(calls)[0]  # no stamp, no invented duration
    assert COMMAND in held(calls)[0]


async def test_a_re_hold_after_a_human_moved_it_pages_again(mock_db, calls):
    """`hold_paged_at` is re-stamped by the next hold, so the dedupe window follows the LATEST
    page and can never become a permanent mute."""
    await hold_a_deposit(mock_db)
    await rewind(mock_db, "deposits", "dep1", "treasury", workers.HELD_SLA_S + 1)
    calls.clear()
    # the operator re-planned the chunk and put the row back into shielding…
    await mock_db["pgasme_test"].deposits.update_one(
        {"_id": "dep1"}, {"$set": {"treasury": "shielding"}}
    )
    await workers.stuck_checks()
    assert held(calls) == []  # not held any more (it is now a `treasury shielding` STUCK row)
    calls.clear()
    # …and it failed again
    await payouts._hold_for_a_human(
        "deposits", "dep1", "treasury", "shielding", REASON, "deposit_shield_failed", "deposit_id"
    )
    assert len(held(calls)) == 1  # the hold speaks
    await workers.stuck_checks()
    assert len(held(calls)) == 1  # and the stuck check does not repeat it


async def test_a_reason_too_long_for_telegram_is_cut_at_a_marker_never_inside_a_command(
    mock_db, calls
):
    """If a cap is ever needed it must be visible and must not hand over half a command:
    Telegram refuses anything over 4096 characters, so 3500 with a marker is the guard."""
    long_reason = "context. " * 500 + f"Run `{COMMAND}` with --apply"
    await mock_db["pgasme_test"].deposits.insert_one(
        {"_id": "dep3", "treasury": "held", "hold_reason": long_reason}
    )
    await workers.stuck_checks()
    assert len(held(calls)) == 1
    text = held(calls)[0]
    assert len(text) <= tg.MAX_CHARS
    assert text.endswith(tg.CUT_MARKER)  # the operator can SEE that there is more
    assert text.count("`") % 2 == 0  # never cut inside a code span
    assert "Run `python -m pgasme.beam replan-shield" not in text  # no half-command
    assert text.startswith("HELD: <code>dep3</code>")  # the id survives every cut


async def test_a_held_payout_follows_the_same_rule(mock_db, calls):
    """Both collections, one implementation: payout_requests keys its status on `status`, the
    deposit treasury sub-machine on `treasury`."""
    reason = "the release response was lost and the chain has no transaction of ours"
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {"_id": "req1", "status": "releasing", "amount_groth": 500_000, "updated_at": time.time()}
    )
    await payouts._hold_for_a_human(
        "payout_requests", "req1", "status", "releasing", reason, "payout_held", "request_id"
    )
    assert len(held(calls)) == 1
    await workers.stuck_checks()
    assert len(held(calls)) == 1
    await rewind(mock_db, "payout_requests", "req1", "status", workers.HELD_SLA_S + 1)
    await workers.stuck_checks()
    assert len(held(calls)) == 2
    assert held(calls)[1].startswith("HELD: <code>req1</code> payout parked for a human")
    assert reason in held(calls)[1]


async def test_the_cooldown_key_is_what_stops_the_reminder_repeating():
    """The other half of that guard, with the REAL tg.send: the first ATTEMPT starts the
    cooldown (an attempt, not a success — a failing send must not spin either), so the next
    pass inside the SLA is refused before Telegram is reached. Muted in tests, so both answers
    are False; what is asserted is that the key was taken and not re-taken."""
    key = "held:deposits:dep1"
    assert tg._last_sent.get(key) is None
    assert await tg.send("HELD: …", key=key, cooldown_s=workers.HELD_SLA_S) is False
    first = tg._last_sent[key]
    assert first > 0
    assert await tg.send("HELD: …", key=key, cooldown_s=workers.HELD_SLA_S) is False
    assert tg._last_sent[key] == first  # the second attempt never even started


# ================================================== the reminder survives a restart


async def test_two_restarts_inside_the_sla_never_re_page_a_fresh_hold(mock_db, calls):
    """The in-memory cooldown is not what holds this back — the row is.

    `tg._last_sent` is per-process: the 2026-09-10 flood's second page went out because nothing
    on disk said the hold had already spoken. Clearing that dict IS a restart, and a restart
    must change nothing about what the operator hears."""
    await hold_a_deposit(mock_db)
    assert len(held(calls)) == 1
    for _ in range(2):
        tg._last_sent.clear()  # ← the restart
        await workers.stuck_checks()
    assert len(held(calls)) == 1  # still the hold's own page, and only it


async def test_a_restart_after_the_reminder_does_not_send_the_reminder_again(mock_db, calls):
    """Same law one step later: the reminder stamps `held_reminded_at` on the row, so the next
    process knows the operator has been told, whatever the cooldown dict has forgotten."""
    await hold_a_deposit(mock_db)
    calls.clear()
    await rewind(mock_db, "deposits", "dep1", "treasury", workers.HELD_SLA_S + 1)
    await workers.stuck_checks()
    assert len(held(calls)) == 1
    d = mock_db["pgasme_test"]
    row = await d.deposits.find_one({"_id": "dep1"})
    assert row["held_reminded_at"] > 0
    before = row["updated_at"]
    for _ in range(3):
        tg._last_sent.clear()  # ← the restart, three times over
        await workers.stuck_checks()
    assert len(held(calls)) == 1
    # the stamp is bookkeeping, not progress: it must not reset the status clock the SLAs read
    assert (await d.deposits.find_one({"_id": "dep1"}))["updated_at"] == before


async def test_a_waiting_hold_on_a_held_row_does_not_silence_the_reminder(mock_db, calls):
    """`payouts._hold` writes `hold_at` — and used to be read as "we paged then".

    One writer per fact: the ordinary hold is a wait a handler still owns (a fee budget, a short
    float) and says WAITING; only `_hold_for_a_human` pages. When the two shared one field, a
    row parked for a human that was then waited on looked freshly paged and the reminder about
    money nobody can move went quiet."""
    await hold_a_deposit(mock_db)
    calls.clear()
    await rewind(mock_db, "deposits", "dep1", "treasury", workers.HELD_SLA_S + 1)
    await payouts._hold("deposits", "dep1", "waiting on the BEAM fee budget", "wait:dep1")
    row = await mock_db["pgasme_test"].deposits.find_one({"_id": "dep1"})
    assert row["hold_at"] > row["hold_paged_at"]  # the WAITING hold moved its own clock only
    await workers.stuck_checks()
    assert len(held(calls)) == 1 and "for over 6 h" in held(calls)[0]


async def test_exactly_one_reminder_per_sla_and_silence_in_between(mock_db, calls):
    """One page per SLA, from whichever stamp is the most recent — and the next one only after
    a WHOLE SLA of silence. tg.send is patched here, so the row is the only thing enforcing it."""
    await hold_a_deposit(mock_db)
    calls.clear()
    await rewind(mock_db, "deposits", "dep1", "treasury", workers.HELD_SLA_S + 1)
    for _ in range(4):  # four monitor passes inside one SLA
        await workers.stuck_checks()
    assert len(held(calls)) == 1
    # …and the SLA is counted from the REMINDER now, not from the hold: one second short of it
    await rewind(mock_db, "deposits", "dep1", "treasury", workers.HELD_SLA_S - 1)
    await workers.stuck_checks()
    assert len(held(calls)) == 1
    await rewind(mock_db, "deposits", "dep1", "treasury", 2)
    await workers.stuck_checks()
    assert len(held(calls)) == 2
    assert "for over 12 h" in held(calls)[1]  # and the duration is still measured from the page
