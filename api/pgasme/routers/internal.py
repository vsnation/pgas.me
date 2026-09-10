"""POST /internal/beampay/webhook — BeamPay's notifications, RECORDED and nothing else.

Why it exists at all: BeamPay's webhook worker pages the operator's Telegram group on EVERY
failed attempt and then retries the same record up to 20 times. So a URL configured in
`BEAMPAY_WEBHOOK_URLS` that answers 404 is not a quiet no-op — it is an alert flood. On
2026-09-10 00:1x–00:2xZ the group took seven "Webhook Failed … Event: failed" messages in
minutes for one transaction, because the URL was configured before the endpoint was written.
The fix is an endpoint that answers 2xx fast, so the worker marks the event delivered and stops.

What it does NOT do — and this is the whole design:

* **No money state.** It writes ONE row to `beampay_events` and touches nothing else: no
  ledger entry, no deposit row, no payout row. The pollers (`payouts.py`, `scanner.py`, the
  balances BeamPay itself reports) stay the only writers of those facts — law 9, one writer
  per fact, because two implementations of one fact will disagree and one of them reaches
  money. INTEGRATION.md §5 says the same from BeamPay's side: BeamPay is the ledger of
  record, treat webhooks as notifications and read balances back from `/balances`.
* **It is evidence, not an instruction.** Delivery is at-least-once and the body is whatever
  posted to a loopback port; a `failed` event here must never fail an order, and a
  `deposit_confirmed` must never credit anybody. What the row buys the operator is a
  timestamped log of what BeamPay believed and when — which is exactly what was missing while
  the pages were being sent.

Authentication, in the order the checks run:

1. **Un-proxied loopback, or 404.** BeamPay posts to `http://127.0.0.1:8300` directly, so a
   caller that is not on the loopback interface — or one whose request carries
   `X-Forwarded-For` / `X-Real-IP` / `Forwarded`, which is what our own nginx adds to
   everything it proxies — is not BeamPay. It gets the answer an unrouted path gets, and
   learns nothing about whether a token exists or matched. Two more guards say the same thing
   at their own levels, and neither replaces this one: `main.InternalIsLoopbackOnly` answers
   404 for the whole family BEFORE routing (so a GET here cannot answer 405 and prove the path
   exists), and nginx refuses `/api/internal` at the edge (deploy/nginx-pgas.me.conf).
2. **A configured token, or 503.** `PGAS_BEAMPAY_WEBHOOK_TOKEN` unset means the operator has
   not provisioned this route yet: it refuses, loudly, in the log. A missing secret is a
   refusal, never an open door.
3. **The right token, or 401.** Compared with `secrets.compare_digest`, and the refusal names
   nothing about the request.

Only then is the body read: garbage is a 422 and the caller learns the shape it got wrong,
which no unauthenticated caller ever reaches.

The token rides in the query string because BeamPay sends no headers — so it would land in
uvicorn's access log next to the request. `main.RedactAccessLog` strips the query of every
`/internal/` line for exactly that reason. Never log the value here either.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import time
from typing import Any, Literal, get_args

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pymongo.errors import DuplicateKeyError

from ..config import settings
from ..db import db

log = logging.getLogger("pgasme.internal")

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

# INTEGRATION.md §5. `failed` and `cancelled` carry a `reason` and no comment/kernel; the
# deposit/withdraw pairs carry the full set. Extra keys are kept verbatim, unknown ones are a
# 422: an event name this build has never heard of must be looked at by a human, not filed.
EventName = Literal[
    "deposit_pending",
    "deposit_confirmed",
    "withdraw_pending",
    "withdraw_confirmed",
    "failed",
    "cancelled",
]
EVENTS: tuple[str, ...] = get_args(EventName)  # one writer of the list; tests read it back
# BeamPay's payload is nine small fields. Anything this large is not it, and a body that Mongo
# would refuse must not become a 500 that the worker reads as "retry me for the next 20 tries".
MAX_BODY_BYTES = 64_000
# Headers that mean "something proxied this". Our own nginx sets the first on every /api/ route,
# so their presence is proof the request did not come from BeamPay's direct loopback post.
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


class WebhookIn(BaseModel):
    """The two fields the dedupe key is made of. Everything else BeamPay sends is optional and
    is stored verbatim — this model exists to refuse garbage, not to re-specify BeamPay."""

    model_config = ConfigDict(extra="allow")

    event: EventName
    txId: str = Field(min_length=1, max_length=200)  # BeamPay's own wire name, kept verbatim


def loopback(host: str) -> bool:
    """True when this address is this machine talking to itself.

    `is_loopback` rather than a literal `{"127.0.0.1", "::1"}` set: uvicorn reports whatever the
    socket says, which on a dual-stack listener is the IPv4-mapped `::ffff:127.0.0.1`, and a
    string comparison would refuse the very caller this route is for."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool((getattr(ip, "ipv4_mapped", None) or ip).is_loopback)


def authorize(request: Request) -> None:
    """404 / 503 / 401, in that order — see this module's docstring. Returns nothing; the
    refusals are exceptions so no caller can forget to check a boolean."""
    client = request.client
    if not (client and loopback(client.host)):
        log.warning("beampay webhook: refused a delivery from a non-loopback caller (404)")
        raise HTTPException(404, "not found")
    if present := [h for h in PROXY_HEADERS if h in request.headers]:
        # A proxied request cannot be BeamPay's direct post, whatever address it claims.
        log.warning("beampay webhook: refused a proxied delivery (404) — %s", ", ".join(present))
        raise HTTPException(404, "not found")
    want = settings.beampay_webhook_token
    if not want:
        log.error(
            "beampay webhook: PGAS_BEAMPAY_WEBHOOK_TOKEN is not configured — refusing every "
            "delivery with 503 and recording nothing (set it in /etc/pgasme.env and give "
            "BeamPay the same value in BEAMPAY_WEBHOOK_URLS)"
        )
        raise HTTPException(503, "this endpoint is not configured")
    got = request.query_params.get("token") or ""
    # ⛔ BYTES, both sides. `secrets.compare_digest` on `str` raises TypeError the moment either
    # side is not ASCII-only — and the query string is whatever the caller typed, so `?token=tüken`
    # turned a refusal into a 500 (which BeamPay's worker reads as "retry me", i.e. the flood).
    # Encoding is not a weakening: two different strings still encode to two different byte
    # strings, and the comparison stays constant-time.
    if not secrets.compare_digest(got.encode("utf-8"), want.encode("utf-8")):
        log.warning("beampay webhook: refused a delivery with a wrong or absent token (401)")
        raise HTTPException(401, "unauthorized")


async def payload(request: Request) -> tuple[WebhookIn, dict[str, Any]]:
    """The validated body and the raw object it came from. 422 on anything else.

    The body is parsed HERE rather than through a route signature so that the refusals above
    run first: a route parameter would let a garbage body answer 422 to a caller that has no
    business knowing the path exists."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(422, f"the body exceeds {MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(raw or b"")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "the body is not JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "the body must be a JSON object") from None
    try:
        return WebhookIn.model_validate(body), body
    except ValidationError as e:
        # the field names and why, never the values: this body is money-adjacent
        detail = "; ".join(f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in e.errors())
        raise HTTPException(422, f"unusable webhook body — {detail}") from None


def row_id(event: str, tx_id: str) -> str:
    """The delivery key BeamPay documents, as the row's primary key."""
    return f"{event}:{tx_id}"


async def store(event: str, tx_id: str, body: dict[str, Any]) -> bool:
    """Insert one row; False when this (txId, event) was already delivered.

    The insert is the dedupe — no read-then-write, so two simultaneous redeliveries cannot both
    decide they are the first. The payload is kept under `payload` verbatim: hoisting it to the
    top level would let a key named `_id` or `received_at` collide with ours.

    The collection is append-only evidence and deliberately carries NO TTL: `received_at` is a
    float, which a Mongo TTL index ignores, and what the operator needs after an incident is
    the whole history of what BeamPay said, not the last month of it."""
    doc = {
        "_id": row_id(event, tx_id),
        "event": event,
        "txId": tx_id,
        "payload": body,
        "received_at": time.time(),
    }
    try:
        await db().beampay_events.insert_one(doc)
    except DuplicateKeyError:
        return False
    return True


@router.post("/beampay/webhook")
async def beampay_webhook(request: Request) -> dict[str, Any]:
    """Record one BeamPay notification. Returns fast; changes no money state, ever.

    A storage failure is deliberately NOT swallowed into a 200: the delivery was not recorded,
    so BeamPay must be told to retry it. `{"ok": true}` here means "this is written down", and
    nothing else in the system will write it down later.
    """
    authorize(request)
    model, body = await payload(request)
    stored = await store(model.event, model.txId, body)
    if stored:
        log.info("beampay webhook: %s %s recorded", model.event, model.txId)
    return {"ok": True, "stored": stored}
