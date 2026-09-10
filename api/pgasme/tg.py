"""Operator notifications — DEFAULT-DENY Telegram plus a queue the monitor drains.

The bot token and chat id live only in the server .env (PGAS_TG_BOT_TOKEN / PGAS_TG_CHAT_ID).
A message is sent only when PGAS_TG_LIVE=1; everything else logs [tg-muted] and returns False,
so a dev box, a test run or a copied .env can never page the operator's group (the firo_arb
2026-09-05 double flood is why). Never log the token.

`queue()` appends an event row (ids only, never addresses) that the monitor turns into one
message later — routes use it so a slow Telegram call never sits inside a request. `alert()` is
the same row written by a worker that sends it AT ONCE (failures, unattributed locks, hook
fallbacks): the row is marked notified with the send's real verdict, so the monitor never sends
it twice and the event log still holds every transition.

`format_event()` is the ONE writer of the wire text (law 9) — the monitor and `alert()` both go
through it — and since 2026-09-10 it also says WHICH STEP OF WHICH ORDER the message is about:

    [2/5] Locked in the pipe · deposit 209d4d6a… — Deposit locked in the ETH pipe, msg 141 <code>…</code>

The rung comes from the event's KIND (`STEPS` below), never from a status word in the free text:
that text is prose from twenty call sites, and a message that happens to contain "delivered" is
not a delivered order. `tests/test_notification_ladder.py` holds the registry to the code — every
kind emitted anywhere in `pgasme/` must have an entry, so a new kind cannot ship un-numbered.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from typing import Any, NamedTuple

import httpx

from .db import db

log = logging.getLogger("pgasme.tg")

_last_sent: dict[str, float] = {}


def _cfg() -> tuple[str, str, bool]:
    return (
        os.environ.get("PGAS_TG_BOT_TOKEN", ""),
        os.environ.get("PGAS_TG_CHAT_ID", ""),
        os.environ.get("PGAS_TG_LIVE", "0") == "1",
    )


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


# Telegram refuses a message over 4096 characters, so SOMETHING has to cut a long one. It used
# to be a bare `text[:4000]` inside send(), which cut mid-sentence and mid-command and said
# nothing about it — the operator read a truncated instruction as the whole instruction. One
# implementation, one number, and a marker: a cut the reader can SEE.
MAX_CHARS = 3500  # under Telegram's 4096 with room for the marker and any HTML tail
CUT_MARKER = " …(truncated)"
# The tags Telegram's HTML subset accepts and this codebase actually writes. A message that ends
# with one of them still OPEN is refused whole ("can't parse entities"), so cutting is not enough:
# an unbalanced pair deletes the alert the cut was supposed to shorten. The check is on the PAIR,
# not on the last `<`: `…<code>` is a complete tag and the old guard passed it happily.
TAGS = frozenset({"b", "i", "code", "pre", "a"})
TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9]*)[^>]*>")


def unclosed(head: str) -> str:
    """The closing tags `head` still owes, innermost first — `""` when it is balanced."""
    stack: list[str] = []
    for m in TAG_RE.finditer(head):
        name = m.group(2).lower()
        if name not in TAGS:
            continue
        if m.group(1):  # a closer: it ends its opener and anything still open inside it
            if name in stack:
                del stack[len(stack) - 1 - stack[::-1].index(name) :]
        else:
            stack.append(name)
    return "".join(f"</{n}>" for n in reversed(stack))


def _cut(text: str, room: int) -> str:
    """`text` shortened to at most `room` characters, never landing mid-token.

    Three things the cut must not do, in the order they are repaired:
      * split a word — and therefore a command's flag or a txid — so it cuts at whitespace;
      * end inside a `code span`, which is how a runbook command reaches the operator: an odd
        number of backticks means the cut landed inside one, so it moves back to that backtick
        rather than handing over half a command;
      * end inside an HTML tag or entity, which makes Telegram refuse the WHOLE message.
    """
    head = text[: max(0, room)]
    at = head.rfind(" ")
    if at > 0:
        head = head[:at]
    if head.count("`") % 2:
        head = head[: head.rfind("`")]
    for opener, closer in (("<", ">"), ("&", ";")):
        i = head.rfind(opener)
        if i != -1 and closer not in head[i:]:
            head = head[:i]
    return head


def cap(text: str, limit: int = MAX_CHARS) -> str:
    """`text`, shortened to `limit` characters with a visible marker if it had to be cut — and
    HANDED OVER BALANCED: every tag the cut left open is closed (or, when its opener falls off
    the end of the budget, dropped with it), so the message Telegram gets always parses.
    """
    if len(text) <= limit:
        return text
    room = limit - len(CUT_MARKER)
    head = _cut(text, room)
    closers = unclosed(head)
    # Make room for the closers, and keep making it while a SHORTER head owes MORE of them (a
    # cut can drop a `</code>` and leave its opener behind). Whenever this body runs,
    # `room - len(closers) < len(head)`, so `head` strictly shrinks — it cannot spin — and on
    # exit the closers and the marker are both inside `limit`.
    while closers and len(head) + len(closers) > room:
        head = _cut(text, room - len(closers))
        closers = unclosed(head)
    return head.rstrip() + closers + CUT_MARKER


def enabled() -> bool:
    """True when a send can actually reach Telegram. False on a dev box, in tests, or with an
    unconfigured .env — a muted send is not a failure and must never be retried as one."""
    token, chat, live = _cfg()
    return bool(live and token and chat)


async def send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
    """Send an HTML-formatted message. `key` + `cooldown_s` rate-limit repeats of one condition."""
    token, chat, live = _cfg()
    if key and cooldown_s > 0:
        last = _last_sent.get(key, 0.0)
        if time.time() - last < cooldown_s:
            return False
    if key:
        # every ATTEMPT starts the cooldown: a failing send must not spin on the next pass
        _last_sent[key] = time.time()
    if not (live and token and chat):
        log.info("[tg-muted] %r", text[:160])
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat,
                    "text": cap(text),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            ok = r.status_code == 200 and r.json().get("ok") is True
    except httpx.HTTPError as e:
        log.warning("[tg-error] %s", type(e).__name__)
        ok = False
    return ok


async def queue(kind: str, text: str, **ids: Any) -> None:
    """Record an operator event; the monitor sends it (verdict first, ids in <code>)."""
    await db().events.insert_one(
        {"kind": kind, "text": text, "at": time.time(), "notified": False, **ids}
    )


async def alert(kind: str, text: str, **ids: Any) -> bool:
    """Record the event AND send it now — for the things the operator must not learn a minute
    late. The row carries the real verdict so the monitor never re-sends it."""
    now = time.time()
    ev: dict[str, Any] = {"kind": kind, "text": text, "at": now, "notified": False, **ids}
    res = await db().events.insert_one(ev)  # insert_one stamps _id on the document it is given
    ok = await send(format_event(ev))
    await db().events.update_one(
        {"_id": ev.get("_id", res.inserted_id)},
        {"$set": {"notified": True, "notified_at": time.time(), "sent": ok, "immediate": True}},
    )
    return ok


# ------------------------------------------------------------------ the step ladders (T47)

# The value `payout_requests.mode` carries for the instant lane. A row's own FIELD picks the
# lane; nothing here reads the message text for it.
#
# ⚠️ **NO EMITTER PUTS `mode` ON AN EVENT ROW TODAY** (checked 2026-09-10). `queue()`/`alert()`
# write exactly the ids their caller passes, and every payout call site passes `request_id` and
# nothing else — so an INSTANT order whose kind is shared with the crossing lane
# (`withdrawal_requested`, `payout_sent`, `payout_delayed`, …) still renders on the 5-rung
# ladder. That is a wrong denominator, not a wrong story, and it is invisible in production
# while `PGAS_PAYOUT_INSTANT_ENABLED=0`. The whole fix is one argument in `payouts._advance`
# (and the same in `routers/withdrawals.create`): pass `mode=<the row's mode>` alongside the id.
# It is NOT done here because those files belong to another writer this session.
INSTANT = "instant"

# The rungs, in order. `len()` is the N in "[k/N]" — one number, derived, so a ladder cannot
# grow a step in one place and keep its old length in another (law 9).
LADDERS: dict[str, tuple[str, ...]] = {
    "deposit": ("Submitted", "Locked in the pipe", "Confirming", "Credited", "Claimed on Beam"),
    "payout": ("Scheduled", "Released", "Bridging", "Delivering", "Delivered"),
    INSTANT: ("Scheduled", "Paying", "Delivered"),
}


class Step(NamedTuple):
    """Where one event kind sits.

    `ladder` None  → a treasury/ops event: no counter, but the id still leads (a page about one
                     of five deposits has to name which).
    `step`   None  → a state OFF the ladder (cancelled): rendered "[·]", because "6/5" and "1/5"
                     would both be lies.
    `label`  None  → the rung's own name from `LADDERS`. A label is what a SIDE state says
                     instead ("Waiting", "Held") while still showing the rung it belongs to.
    `instant`      → this kind's rung on the 3-step instant ladder, for the kinds BOTH payout
                     lanes emit. Absent means the kind belongs to the crossing lane only, and a
                     row marked instant still renders where it actually is rather than being
                     clamped onto a ladder that has no such step.
    """

    ladder: str | None
    step: int | None = None
    label: str | None = None
    instant: int | None = None


OPS = Step(None)  # treasury / ops: text unchanged, no counter, id first

# ⛔ EVERY KIND EMITTED IN `pgasme/` IS HERE. The exhaustiveness test scans the source for
# `tg.queue`/`tg.alert` and for the four helpers that forward a `kind` to them, and fails on a
# kind with no entry — an un-numbered message is exactly what the admin asked us to stop.
STEPS: dict[str, Step] = {
    # ── deposit (5): submitted → locked → confirming → credited → claimed on Beam
    "deposit_submitted": Step("deposit", 1),
    "deposit_order_seen": Step("deposit", 1, "Order seen"),
    "deposit_unfilled": Step("deposit", 1, "Waiting"),
    "deposit_unverifiable": Step("deposit", 1, "Unverified"),
    "deposit_mismatch": Step("deposit", 1, "Rejected"),
    # the transaction was never seen on any endpoint and the row is FAILED. Not "[2/5] Waiting":
    # a page that says a dead row is waiting is the pager saying the opposite of what happened.
    "deposit_unseen": Step("deposit", 1, "Failed"),
    "deposit_failed": Step("deposit", 1, "Failed"),
    "deposit_fallback": Step("deposit", 1, "Fallback"),
    "deposit_locked": Step("deposit", 2),
    "lock_unattributed": Step("deposit", 2, "Waiting"),
    "lock_abandoned": Step("deposit", 2, "Waiting"),
    "deposit_confirming": Step("deposit", 3),
    "deposit_credited": Step("deposit", 4),
    "deposit_claiming": Step("deposit", 5, "Claiming on Beam"),
    "deposit_claimed": Step("deposit", 5),
    "deposit_claim_failed": Step("deposit", 5, "Claim held"),
    "deposit_claim_unconfirmed": Step("deposit", 5, "Claim held"),
    "deposit_claim_unresolved": Step("deposit", 5, "Claim held"),
    # ── scheduled payout (5): scheduled → released → bridging → delivering → delivered
    "withdrawal_requested": Step("payout", 1, None, 1),
    "payout_delayed": Step("payout", 1, "Delayed", 1),
    "payout_retrying": Step("payout", 1, "Retrying", 1),
    "payout_delayed_too_long": Step("payout", 1, "Held", 1),
    "payout_dest_now_contract": Step("payout", 1, "Refused", 1),
    "payout_release_contradiction": Step("payout", 1, "Held"),
    "payout_fund_unconfirmed": Step("payout", 2, "Unconfirmed"),
    "payout_fund_lost": Step("payout", 2, "Held"),
    "payout_fund_double": Step("payout", 2, "Held"),
    "payout_fund_failed": Step("payout", 2, "Held"),
    "payout_fund_unresolved": Step("payout", 2, "Held"),
    "payout_releasing": Step("payout", 2),
    "payout_build_refused": Step("payout", 2, "Refused"),
    "payout_send_unconfirmed": Step("payout", 2, "Unconfirmed"),
    "payout_send_resolved": Step("payout", 2, "Released"),
    "payout_unresolved": Step("payout", 2, "Held"),
    "payout_bridging": Step("payout", 3),
    "payout_double_release": Step("payout", 3, "Held"),
    "payout_delivering": Step("payout", 4),
    "payout_sent": Step("payout", 5, None, 3),
    "withdrawal_cancelled": Step("payout", None, "Cancelled"),
    "withdrawal_cancel_no_debit": Step("payout", None, "Cancelled"),
    "withdrawal_cancel_refund_failed": Step("payout", None, "Cancelled"),
    # ── instant payout (3): scheduled → paying → delivered. These three exist on one lane only.
    "payout_paying": Step(INSTANT, 2),
    "payout_instant_unsigned": Step(INSTANT, 2, "Unsigned"),
    "payout_instant_lost": Step(INSTANT, 2, "Held"),
    "payout_instant_nonce_taken": Step(INSTANT, 2, "Held"),
    "payout_instant_not_ours": Step(INSTANT, 2, "Held"),
    # ── treasury / ops: the house's own work, not a rung of anybody's order
    "deposit_shielding": OPS,
    "deposit_shielded": OPS,
    "deposit_shield_failed": OPS,
    "deposit_shield_unconfirmed": OPS,
    "deposit_shield_unresolved": OPS,
    "deposit_shield_duplicate": OPS,
    "deposit_shield_replanned": OPS,
    "deposit_attribution": OPS,
    "payout_attribution": OPS,
    "beam_fee_excessive": OPS,
    "withdrawal_rolled_back": OPS,
    "withdrawal_belt_unread": OPS,
    "withdrawal_events_unwritten": OPS,
}

# The id fields an event row can carry, in the order they are read, with the word for the thing
# each one identifies. `request_ids` (a batch page) is appended after them.
NOUNS: tuple[tuple[str, str], ...] = (
    ("deposit_id", "deposit"),
    ("request_id", "payout"),
    ("quote_id", "quote"),
    ("lock", "lock"),
)
SHORT_CHARS = 8  # the admin reads ids at a glance as "209d4d6a…"; the full one is in the tail


def short(i: object) -> str:
    """`209d4d6aef90b33a436937b9` → `209d4d6a…`, and a `0x…` id keeps its prefix.

    A handle, never the identity: the full id is in <code> at the end of every message, which is
    what an operator copies into a query.
    """
    s = str(i)
    head, body = ("0x", s[2:]) if s[:2].lower() == "0x" else ("", s)
    return s if len(body) <= SHORT_CHARS else f"{head}{body[:SHORT_CHARS]}…"


def ids_of(ev: dict[str, Any]) -> list[tuple[str, str]]:
    """Every id on the row, as (what it identifies, the id) — ONE reader, so the id that leads
    the message and the ids in the tail can never be a different set."""
    out = [(noun, str(ev[key])) for key, noun in NOUNS if ev.get(key)]
    return out + [("payout", str(i)) for i in (ev.get("request_ids") or []) if i]


def rung(ev: dict[str, Any]) -> str:
    """`[2/5] Locked in the pipe` — from the KIND, never from the text. `""` for an ops event
    and for a kind with no entry."""
    s = STEPS.get(str(ev.get("kind") or ""))
    if s is None or s.ladder is None:
        return ""
    ladder, step = s.ladder, s.step
    if s.instant is not None and str(ev.get("mode") or "") == INSTANT:
        ladder, step = INSTANT, s.instant
    if step is None:
        return f"[·] {s.label}"
    return f"[{step}/{len(LADDERS[ladder])}] {s.label or LADDERS[ladder][step - 1]}"


def whose(ev: dict[str, Any]) -> str:
    """`deposit 209d4d6a…` — which order this is about, ahead of the prose."""
    ids = ids_of(ev)
    if not ids:
        return ""
    noun, first = ids[0]
    more = f" +{len(ids) - 1} more" if len(ids) > 1 else ""
    return f"{noun} {esc(short(first))}{more}"


def format_event(ev: dict[str, Any]) -> str:
    """`[k/N] <rung> · <what> <short id> — <the event's own text> <code>full id</code>`.

    A kind with no entry renders exactly as it did before the ladder existed (text, then the
    ids) — it is not given a number it may not deserve; the exhaustiveness test is what stops
    one shipping. THE ONE PLACE the event's text is escaped: escaping it at a call site too
    pages the operator in &amp;-speak.
    """
    ids = ids_of(ev)
    tail = " ".join(f"<code>{esc(i)}</code>" for _, i in ids)
    known = str(ev.get("kind") or "") in STEPS
    lead = " · ".join(p for p in (rung(ev), whose(ev)) if p) if known else ""
    body = f"{lead} — {esc(ev['text'])}" if lead else esc(ev["text"])
    return f"{body} {tail}".strip()


def fmt_groth(groth: int, decimals: int = 8) -> str:
    return f"{groth / 10**decimals:.8f}".rstrip("0").rstrip(".")
