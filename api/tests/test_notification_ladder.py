"""Every operator message says WHICH STEP OF WHICH ORDER it is about.

The admin, watching his own deposit go by in the group (2026-09-10): *"Make notifications with
order id and each status, if steps are 5, do [1/5] [2/5] and so on"*. The messages that day read

    Deposit submitted (direct): ETH ≈ 0.0019999 209d4d6a…
    Deposit locked in the ETH pipe, msg 141 209d4d6a…

— no counter, and the id last, so five lines about five different orders all looked the same and
none of them said how far along it was.

`tg.format_event` is the ONE writer of the wire text (law 9), so the ladder lives there and
nowhere else. The rung is derived from the event's **kind** — never from a status word in the free
text, which is written by twenty different call sites and is not a state machine. Two things this
file therefore enforces beyond the rendering:

  * **exhaustiveness** — every kind emitted anywhere in `pgasme/` has an entry, so a new kind
    cannot ship un-numbered (and a registry entry nobody emits cannot rot unnoticed);
  * **reachability** — a ladder that claims N rungs has a kind for every one of them, so
    "[4/5]" can never be a step no message ever shows.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from pgasme import tg

DEP = "209d4d6aef90b33a436937b9"  # the admin's own deposit, and the shape every id has
REQ = "d0280a1b3f5c9e77ab120044"  # secrets.token_hex(12), same as a deposit id


def ev(kind: str, text: str, **ids: object) -> dict[str, object]:
    return {"kind": kind, "text": text, "at": 0.0, "notified": False, **ids}


# --------------------------------------------------------------------- the three the admin sees


def test_the_deposit_lock_message_the_admin_watched_now_carries_its_step():
    assert tg.format_event(
        ev("deposit_locked", "Deposit locked in the ETH pipe, msg 141", deposit_id=DEP)
    ) == (
        "[2/5] Locked in the pipe · deposit 209d4d6a… — "
        "Deposit locked in the ETH pipe, msg 141 <code>209d4d6aef90b33a436937b9</code>"
    )


def test_a_delayed_payout_shows_the_rung_it_is_waiting_on():
    assert tg.format_event(
        ev(
            "payout_delayed",
            "Payout DELAYED (nothing is lost, the money stays reserved): the treasury has no "
            "free BEAM coin for the crossing's fee. Next attempt in 5 min",
            request_id=REQ,
        )
    ) == (
        "[1/5] Delayed · payout d0280a1b… — Payout DELAYED (nothing is lost, the money stays "
        "reserved): the treasury has no free BEAM coin for the crossing's fee. Next attempt in "
        "5 min <code>d0280a1b3f5c9e77ab120044</code>"
    )


def test_a_delivered_payout_is_the_last_rung_of_five():
    assert tg.format_event(
        ev(
            "payout_sent",
            "Payout SENT: ETH 0.001 delivered on Ethereum in block 23412345",
            request_id=REQ,
        )
    ) == (
        "[5/5] Delivered · payout d0280a1b… — Payout SENT: ETH 0.001 delivered on Ethereum in "
        "block 23412345 <code>d0280a1b3f5c9e77ab120044</code>"
    )


# ------------------------------------------------------------------------------ every rung


@pytest.mark.parametrize(
    ("kind", "want"),
    [
        ("deposit_submitted", "[1/5] Submitted"),
        ("deposit_locked", "[2/5] Locked in the pipe"),
        ("deposit_confirming", "[3/5] Confirming"),
        ("deposit_credited", "[4/5] Credited"),
        ("deposit_claimed", "[5/5] Claimed on Beam"),
    ],
)
def test_each_deposit_rung_renders_its_own_number_and_name(kind, want):
    out = tg.format_event(ev(kind, "the existing text", deposit_id=DEP))
    assert out == f"{want} · deposit 209d4d6a… — the existing text <code>{DEP}</code>"


@pytest.mark.parametrize(
    ("kind", "want"),
    [
        ("withdrawal_requested", "[1/5] Scheduled"),
        ("payout_releasing", "[2/5] Released"),
        ("payout_bridging", "[3/5] Bridging"),
        ("payout_delivering", "[4/5] Delivering"),
        ("payout_sent", "[5/5] Delivered"),
    ],
)
def test_each_scheduled_payout_rung_renders_its_own_number_and_name(kind, want):
    out = tg.format_event(ev(kind, "the existing text", request_id=REQ))
    assert out == f"{want} · payout d0280a1b… — the existing text <code>{REQ}</code>"


@pytest.mark.parametrize(
    ("kind", "want"),
    [
        ("withdrawal_requested", "[1/3] Scheduled"),
        ("payout_paying", "[2/3] Paying"),
        ("payout_sent", "[3/3] Delivered"),
    ],
)
def test_each_instant_payout_rung_renders_on_the_three_rung_ladder(kind, want):
    """The instant lane is three steps, not five — and which lane an order is on is read from
    the row's own `mode` FIELD, never from a word in the message."""
    out = tg.format_event(ev(kind, "the existing text", request_id=REQ, mode=tg.INSTANT))
    assert out == f"{want} · payout d0280a1b… — the existing text <code>{REQ}</code>"


def test_an_instant_only_kind_needs_no_mode_field_to_find_its_ladder():
    """`payout_paying` exists on one lane only, so its rung is not a guess."""
    assert tg.format_event(ev("payout_paying", "t", request_id=REQ)).startswith("[2/3] Paying · ")


def test_a_payout_kind_the_instant_lane_never_emits_stays_on_the_five_rung_ladder():
    """`mode` selects a lane; it does not invent a rung. A crossing-only kind on a row somehow
    marked instant still renders where it actually belongs, rather than being clamped onto a
    ladder that has no such step."""
    out = tg.format_event(ev("payout_bridging", "t", request_id=REQ, mode=tg.INSTANT))
    assert out.startswith("[3/5] Bridging · ")


# ------------------------------------------------------------------------------ side states


@pytest.mark.parametrize(
    ("kind", "idkey", "want"),
    [
        ("lock_unattributed", "lock", "[2/5] Waiting"),
        ("deposit_claim_unresolved", "deposit_id", "[5/5] Claim held"),
        ("deposit_unseen", "deposit_id", "[1/5] Failed"),
        ("payout_delayed_too_long", "request_id", "[1/5] Held"),
        ("payout_unresolved", "request_id", "[2/5] Held"),
        ("payout_double_release", "request_id", "[3/5] Held"),
    ],
)
def test_a_side_state_shows_the_step_it_belongs_to(kind, idkey, want):
    assert tg.format_event(ev(kind, "why", **{idkey: "abcdef0123456789"})).startswith(want + " · ")


def test_a_terminal_side_state_carries_no_step_number_at_all():
    """Cancelled is not rung 6 of 5 and it is not rung 1 either — it is off the ladder, and the
    marker says so rather than lying with a number."""
    assert tg.format_event(
        ev("withdrawal_cancelled", "Withdrawal cancelled: ETH 0.01 back to Available", request_id=REQ)
    ) == (
        "[·] Cancelled · payout d0280a1b… — Withdrawal cancelled: ETH 0.01 back to Available "
        f"<code>{REQ}</code>"
    )


# ------------------------------------------------------------- treasury / ops and unknown kinds


def test_a_treasury_event_keeps_its_text_with_no_counter_but_leads_with_the_id():
    """Shielding, attribution and fee events are not rungs of the user's order — they are the
    treasury's own work. No counter; the id still comes first, so a page about one of five
    deposits names which."""
    assert tg.format_event(
        ev("deposit_shielding", "Treasury: shielding ETH 0.0019999 in 3 chunk(s)", deposit_id=DEP)
    ) == (
        "deposit 209d4d6a… — Treasury: shielding ETH 0.0019999 in 3 chunk(s) "
        f"<code>{DEP}</code>"
    )


def test_an_unregistered_kind_renders_exactly_as_it_did_before_this_change():
    """A kind with no entry is not given a number it might not deserve. The exhaustiveness test
    below is what stops one shipping; this is what it looks like if one ever does."""
    assert tg.format_event(ev("something_new", "Some text", deposit_id=DEP)) == (
        f"Some text <code>{DEP}</code>"
    )


def test_an_event_with_no_id_at_all_still_shows_its_rung():
    assert tg.format_event(ev("payout_bridging", "no id on this one")) == (
        "[3/5] Bridging — no id on this one"
    )


# ------------------------------------------------------------------------------ ids and nouns


def test_the_short_id_is_the_first_eight_characters_and_a_marker():
    assert tg.short(DEP) == "209d4d6a…"
    assert tg.short("0x" + "ab" * 32 + ":4") == "0xabababab…"  # a lock keeps its 0x
    assert tg.short("short") == "short"  # nothing to shorten, nothing marked


@pytest.mark.parametrize(
    ("idkey", "noun"),
    [("deposit_id", "deposit"), ("request_id", "payout"), ("quote_id", "quote"), ("lock", "lock")],
)
def test_each_id_field_names_the_kind_of_order_it_identifies(idkey, noun):
    out = tg.format_event(ev("deposit_shielding", "t", **{idkey: "abcdef0123456789"}))
    assert out.startswith(f"{noun} abcdef01… — t ")


def test_a_batch_event_leads_with_the_first_order_and_says_how_many_more():
    out = tg.format_event(ev("withdrawal_rolled_back", "ROLLED BACK: …", request_ids=["a" * 24, "b" * 24, "c" * 24]))
    assert out.startswith("payout aaaaaaaa… +2 more — ROLLED BACK: … ")
    assert out.endswith(f"<code>{'a' * 24}</code> <code>{'b' * 24}</code> <code>{'c' * 24}</code>")


def test_the_full_id_still_ends_the_message_in_code_as_it_always_did():
    """`tests/test_withdrawals.py` asserts this shape too — the tail is the operator's copy
    handle and the ladder is not allowed to take it away."""
    for kind in ("withdrawal_requested", "payout_sent", "deposit_shielded", "unknown_kind"):
        assert tg.format_event(ev(kind, "t", request_id=REQ)).endswith(f"<code>{REQ}</code>")


# ------------------------------------------------------------------------------ escaping / cap


def test_the_text_is_escaped_exactly_once_and_the_markup_around_it_is_not():
    out = tg.format_event(ev("deposit_locked", "a<b> & c", deposit_id=DEP))
    assert "a&lt;b&gt; &amp; c" in out and "&amp;lt;" not in out
    assert out.startswith("[2/5] Locked in the pipe · deposit 209d4d6a… — ")
    assert out.endswith(f"<code>{DEP}</code>")  # the tail's own tags survive


def test_an_id_that_carries_markup_cannot_open_a_tag_in_either_place():
    out = tg.format_event(ev("deposit_locked", "t", deposit_id="<b>0123456789"))
    assert "<b>" not in out and out.count("&lt;b&gt;") == 2  # once short, once in full


def test_a_very_long_event_still_caps_to_something_telegram_will_parse():
    out = tg.cap(tg.format_event(ev("payout_sent", "y " * 4000, request_id=REQ)))
    assert len(out) <= tg.MAX_CHARS and out.endswith(tg.CUT_MARKER)
    assert out.startswith("[5/5] Delivered · payout d0280a1b… — ")
    assert out.count("<code>") == out.count("</code>")


def test_the_rung_is_never_read_from_a_status_word_in_the_free_text():
    """The one rule the whole design rests on: the text is prose from twenty call sites, and a
    message that happens to contain the word "delivered" is not a delivered order."""
    out = tg.format_event(ev("deposit_locked", "Delivered SENT scheduled cancelled", deposit_id=DEP))
    assert out.startswith("[2/5] Locked in the pipe · ")


# ------------------------------------------------------------------------------ exhaustiveness


PKG = pathlib.Path(__file__).resolve().parent.parent / "pgasme"

# The helpers that take a `kind` from their caller and hand it to `tg.queue`/`tg.alert`, and
# WHERE in their argument list that kind sits. A forwarder this table does not know is a hole in
# the scan, so the scan fails on one rather than quietly reporting fewer kinds than exist.
FORWARDERS = {
    "_advance": 5,
    "_hold_for_a_human": 5,
    "register_attribution": 5,
    "_page_operator": 0,
}


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def emitted_kinds() -> tuple[set[str], list[str]]:
    """Every event kind written anywhere in `pgasme/`, and the call sites the scan could not
    read. Source-level rather than import-level: a kind that only one branch ever emits is still
    a kind, and it must still have a rung before it reaches the operator."""
    kinds: set[str] = set()
    blind: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        tree = ast.parse(path.read_text())
        # which function each node sits in, so a non-literal kind can be blamed on its forwarder
        owner: dict[ast.AST, str] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(fn):
                    owner.setdefault(child, fn.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            direct = (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "tg"
                and f.attr in ("queue", "alert")
            )
            fwd = isinstance(f, ast.Name) and f.id in FORWARDERS
            if not (direct or fwd):
                continue
            at = 0 if direct else FORWARDERS[f.id]  # type: ignore[union-attr]
            lit = _literal(node.args[at]) if len(node.args) > at else None
            if lit is not None:
                kinds.add(lit)
            elif direct and owner.get(node) in FORWARDERS:
                pass  # the forwarders themselves: their callers supply the literal
            else:
                blind.append(f"{path.name}:{node.lineno} (in {owner.get(node, '?')})")
    return kinds, blind


def test_the_scan_can_read_every_call_site_that_pages_the_operator():
    _, blind = emitted_kinds()
    assert not blind, (
        "these calls hand `tg` a kind this scan cannot resolve — add the forwarder to "
        f"FORWARDERS (with the position of its `kind` argument): {blind}"
    )


def test_every_kind_in_the_code_has_a_rung_so_none_can_ship_un_numbered():
    kinds, _ = emitted_kinds()
    missing = sorted(kinds - set(tg.STEPS))
    assert not missing, (
        "these event kinds are emitted with no entry in tg.STEPS, so they would reach the "
        f"operator with no step counter: {missing}"
    )


def test_no_rung_is_registered_for_a_kind_nothing_emits():
    """The other direction: a typo in the registry is a kind that renders un-numbered forever
    while the table says it does not."""
    kinds, _ = emitted_kinds()
    stale = sorted(set(tg.STEPS) - kinds)
    assert not stale, f"tg.STEPS has entries no emitter writes (typo, or a dead kind): {stale}"


def test_every_rung_of_every_ladder_is_reachable_by_some_kind():
    for name, rungs in tg.LADDERS.items():
        steps = {
            s.step for s in tg.STEPS.values() if s.ladder == name and s.step
        } | {
            s.instant for s in tg.STEPS.values() if name == tg.INSTANT and s.instant
        }
        assert steps == set(range(1, len(rungs) + 1)), (
            f"ladder {name!r} claims {len(rungs)} rungs; kinds only reach {sorted(steps)}"
        )


def test_no_entry_points_past_the_end_of_its_own_ladder():
    for kind, s in tg.STEPS.items():
        if s.ladder is not None and s.step is not None:
            assert 1 <= s.step <= len(tg.LADDERS[s.ladder]), kind
        if s.instant is not None:
            assert 1 <= s.instant <= len(tg.LADDERS[tg.INSTANT]), kind
