"""Operator notifications — DEFAULT-DENY Telegram plus a queue the monitor drains.

The bot token and chat id live only in the server .env (PGAS_TG_BOT_TOKEN / PGAS_TG_CHAT_ID).
A message is sent only when PGAS_TG_LIVE=1; everything else logs [tg-muted] and returns False,
so a dev box, a test run or a copied .env can never page the operator's group (the firo_arb
2026-09-05 double flood is why). Never log the token.

`queue()` appends an event row (ids only, never addresses) that the monitor turns into one
message later — routes and workers use it so a slow Telegram call never sits inside a request.
"""

from __future__ import annotations

import html
import logging
import os
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


async def send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
    """Send an HTML-formatted message. `key` + `cooldown_s` rate-limit repeats of one condition."""
    token, chat, live = _cfg()
    if key and cooldown_s > 0:
        last = _last_sent.get(key, 0.0)
        if time.time() - last < cooldown_s:
            return False
    if not (live and token and chat):
        log.info("[tg-muted] %r", text[:160])
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat,
                    "text": text[:4000],
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            ok = r.status_code == 200 and r.json().get("ok") is True
    except httpx.HTTPError as e:
        log.warning("[tg-error] %s", type(e).__name__)
        ok = False
    if ok and key:
        _last_sent[key] = time.time()
    return ok


async def queue(kind: str, text: str, **ids: Any) -> None:
    """Record an operator event; the monitor sends it (verdict first, ids in <code>)."""
    await db().events.insert_one(
        {"kind": kind, "text": text, "at": time.time(), "notified": False, **ids}
    )


def format_event(ev: dict[str, Any]) -> str:
    ids = [ev.get("deposit_id"), ev.get("request_id"), ev.get("lock")] + list(
        ev.get("request_ids") or []
    )
    tail = " ".join(f"<code>{i}</code>" for i in ids if i)
    return f"{esc(ev['text'])} {tail}".strip()


def fmt_groth(groth: int, decimals: int = 8) -> str:
    return f"{groth / 10**decimals:.8f}".rstrip("0").rstrip(".")
