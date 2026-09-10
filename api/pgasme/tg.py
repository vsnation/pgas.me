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
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from typing import Any

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


def format_event(ev: dict[str, Any]) -> str:
    ids = [
        ev.get("deposit_id"),
        ev.get("request_id"),
        ev.get("quote_id"),
        ev.get("lock"),
    ] + list(ev.get("request_ids") or [])
    tail = " ".join(f"<code>{i}</code>" for i in ids if i)
    return f"{esc(ev['text'])} {tail}".strip()


def fmt_groth(groth: int, decimals: int = 8) -> str:
    return f"{groth / 10**decimals:.8f}".rstrip("0").rstrip(".")
