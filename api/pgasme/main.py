"""FastAPI app factory. `uvicorn pgasme.main:app --host 127.0.0.1 --port 8300` or `python -m pgasme`.

Importing this module constructs Settings, which refuses to exist on an unsafe posture
(placeholder secrets outside dev, /v1/dev/* in prod) — so the process fails to boot instead of
serving with a forgeable session secret. /v1/health reports the posture it did boot with.
"""

from __future__ import annotations

import email.message
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__, workers
from .config import settings
from .db import ensure_indexes, note_index_failure
from .routers import (
    account,
    deposits,
    destinations,
    dev,
    dex,
    internal,
    quote,
    siwe,
    stats,
    withdrawals,
)

log = logging.getLogger("pgasme")


# ── Secrets in log files: one mask, applied to every record this process makes ────────────────
# 2026-09-10: httpx logs one INFO line per request carrying the FULL url, and the Telegram send
# path's url is `https://api.telegram.org/bot<TOKEN>/sendMessage` — so the operator group's bot
# token was written into /var/log/pgasme/api.log 21 times, in cleartext, next to everything else
# a log gets copied into. Two independent guards, because either alone is one flag away from
# leaking again:
#   * the libraries that log a url per request are held at WARNING (`quieten`), so the line is
#     not made in the first place;
#   * every record that IS made goes through this mask (`RedactSecrets`), so a future debug flag,
#     a third library, or an exception carrying the url cannot re-leak it.
BOT_TOKEN_RE = re.compile(r"bot[0-9]+:[A-Za-z0-9_-]+")
BOT_TOKEN_MASK = "bot<REDACTED>"
QUIET_LOGGERS = ("httpx", "httpcore")


def redact_secrets(text: str) -> str:
    """`text` with every Telegram bot token masked. ONE implementation of what a secret looks
    like and what it is replaced by — the filter, the record factory and the runbook's
    `sed -E 's#bot[0-9]+:[A-Za-z0-9_-]+#bot<REDACTED>#g'` all describe the same shape."""
    return BOT_TOKEN_RE.sub(BOT_TOKEN_MASK, text)


class RedactSecrets(logging.Filter):
    """Mask secrets in a record's formatted message — and in its traceback.

    The record is MUTATED rather than dropped: a masked line is still the operator's evidence
    that the send happened, and every downstream handler (a file, stdout, a test's capture) sees
    the same masked text because there is only one record. `record.args` is cleared only when a
    mask was actually applied, so the access-log filter below still gets its `args` tuple.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — a broken format string is the emitter's bug, not ours
            text = ""
        if text and BOT_TOKEN_RE.search(text):
            record.msg = redact_secrets(text)
            record.args = ()
        if record.exc_info:
            # A traceback can carry the same url (httpx names it in HTTPStatusError). A Formatter
            # reuses `exc_text` when it is already set, so pre-formatting it here is the only
            # place a filter can reach the traceback's text at all.
            try:
                exc_text = record.exc_text or logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001 — an unformattable traceback stays as it was
                exc_text = ""
            if exc_text and BOT_TOKEN_RE.search(exc_text):
                record.exc_text = redact_secrets(exc_text)
        return True


_redact_secrets = RedactSecrets()


def install_log_redaction() -> None:
    """Idempotent; called at import AND from `create_app()` — whichever happens first.

    A filter on a logger only sees the records logged THROUGH that logger, so a root filter
    alone would never touch httpx's. The record factory is what makes the guarantee hold for
    every logger and every handler, including handlers added later (a test's capture, a future
    file handler): the mask is applied where the record is born. The factory only runs for
    records that already passed their logger's level check, so nothing is formatted in vain.
    """
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    root = logging.getLogger()
    if _redact_secrets not in root.filters:
        root.addFilter(_redact_secrets)
    factory = logging.getLogRecordFactory()
    if getattr(factory, "pgas_redacting", False):
        return

    def redacting(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = factory(*args, **kwargs)
        _redact_secrets.filter(record)
        return record

    redacting.pgas_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(redacting)


install_log_redaction()  # at import: a module that logs before create_app() is still covered


# ── The loopback-only /internal family ────────────────────────────────────────────────────────
INTERNAL_PREFIX = "/internal"
# ONE list of "something proxied this", owned by the route that first needed it. The ASGI scope
# carries raw bytes where `Request.headers` carries str, which is the only difference here — two
# hand-written lists would be two implementations of one fact, and the one that drifted would be
# the one holding the door open.
PROXY_HEADERS = tuple(h.encode("ascii") for h in internal.PROXY_HEADERS)
_REPEATED_SLASHES = re.compile(r"/{2,}")


def normalised_path(path: str) -> str:
    """One reading of "which path is this", shared by the guard and the access-log filter.

    Lowercased and with repeated slashes collapsed, because `//internal/x` and `/INTERNAL/x`
    reach the same routes on some stacks and read as a different path to a naive prefix test —
    two implementations of one fact disagreeing is how a guard gets walked around."""
    return _REPEATED_SLASHES.sub("/", path.lower())


def is_internal_path(path: str) -> bool:
    """True for every spelling of the loopback-only family — `/internal`, `/internal/x`,
    `//INTERNAL/x`, and `/internalx` too. A PREFIX, not a directory: a guard that only matched
    `/internal/` leaves `/internal` and `/internalx` to whatever is mounted next."""
    return normalised_path(path).startswith(INTERNAL_PREFIX)


class InternalIsLoopbackOnly:
    """404 for the whole `/internal` family unless this machine is talking to itself.

    The route refuses by itself too (routers/internal.authorize) — this is the same guard at the
    level that owns the ANSWER rather than the handler. Before routing, because routing is what
    leaks: FastAPI answers a GET on a POST-only route with **405**, and a 405 tells an internet
    prober that the path exists and that only its method was wrong. Every method, every spelling
    and every body must get the answer an unrouted path gets, and get it before the body is read.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not is_internal_path(str(scope.get("path") or "")):
            await self.app(scope, receive, send)
            return
        client = scope.get("client") or ()
        host = str(client[0]) if client else ""
        proxied = [k for k, _ in scope.get("headers") or [] if k.lower() in PROXY_HEADERS]
        if internal.loopback(host) and not proxied:
            await self.app(scope, receive, send)
            return
        # the path only — the credential rides in the QUERY STRING, which is not in `scope["path"]`
        log.warning(
            "refused %s %s: /internal is loopback-only%s",
            scope.get("method"),
            scope.get("path"),
            f" (proxied: {', '.join(k.decode() for k in proxied)})" if proxied else "",
        )
        response = JSONResponse(status_code=404, content={"detail": "not found"})
        await response(scope, receive, send)


BODY_METHODS = ("POST", "PUT", "PATCH")
NON_FINITE_DETAIL = (
    "the request body contains {token}, which is not a finite JSON number — every number in the "
    "body must be a real, finite value; nothing was read and nothing was changed"
)


class _NonFinite(ValueError):
    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


def non_finite_token(body: bytes) -> str | None:
    """The first token in this body that is not a finite number, or None.

    A body that is not JSON at all (or not decodable) is NOT this function's business — it
    answers None and FastAPI refuses it exactly the way it always did."""
    if not body.strip():
        return None

    def constant(token: str) -> float:  # the NaN / Infinity / -Infinity literals
        raise _NonFinite(token)

    def number(token: str) -> float:  # …and `1e400`, which needs no literal to become `inf`
        value = float(token)
        if not math.isfinite(value):
            raise _NonFinite(token)
        return value

    try:
        json.loads(body, parse_constant=constant, parse_float=number)
    except _NonFinite as e:
        return e.token
    except (ValueError, UnicodeDecodeError):
        return None
    return None


def is_json_body(scope: dict[str, Any]) -> bool:
    """Exactly what FastAPI itself treats as a JSON body: `application/json`, anything
    `+json`, or NO content-type at all. Law 8 — the prober must call the way the caller calls;
    a middleware that scanned a different set than the parser would guard the wrong requests."""
    raw = b""
    for k, v in scope.get("headers") or []:
        if k.lower() == b"content-type":
            raw = v
            break
    if not raw:
        return True
    msg = email.message.Message()
    msg["content-type"] = raw.decode("latin-1")
    main, _, sub = (msg.get_content_type() or "").partition("/")
    return main == "application" and (sub == "json" or sub.endswith("+json"))


class RefuseNonFiniteJSON:
    """422 for a body carrying NaN / Infinity — BEFORE it can become a 500.

    JSON has no NaN literal, but python's `json` accepts one by default and Starlette renders
    responses with `allow_nan=False`. So a bare `NaN` in the body parsed fine, was correctly
    refused by pydantic (`allow_inf_nan=False`), and then killed the 422 itself at encode time —
    FastAPI echoes the offending `input` back inside the validation error, and a NaN there
    raises `ValueError: Out of range float values are not JSON compliant`. The client saw a 500
    for a request the API had already refused, which reads as "the server broke, retry" for a
    body that will break it again. `1e400` does the same with no literal at all: it parses to
    `inf`.

    The body is therefore parsed ONCE at the door with a loader that refuses every non-finite
    number, and a body that carries one never reaches a route. Nothing else about the request is
    touched: the bytes are replayed downstream unchanged.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") not in BODY_METHODS:
            await self.app(scope, receive, send)
            return
        if not is_json_body(scope):
            await self.app(scope, receive, send)
            return
        body = b""
        buffered: list[dict[str, Any]] = []
        more = True
        while more:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":  # a disconnect: hand it straight on
                await self.app(scope, _replay(buffered, receive), send)
                return
            body += message.get("body", b"") or b""
            more = bool(message.get("more_body"))
        token = non_finite_token(body)
        if token is not None:
            response = JSONResponse(
                status_code=422, content={"detail": NON_FINITE_DETAIL.format(token=token)}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, _replay(buffered, receive), send)


def _replay(
    messages: list[dict[str, Any]], receive: Any
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """Hand the buffered body back to the app, then defer to the real receive channel."""
    pending = list(messages)

    async def rx() -> dict[str, Any]:
        if pending:
            return pending.pop(0)
        return await receive()

    return rx


class RedactAccessLog(logging.Filter):
    """What must never reach uvicorn's access log, decided in ONE place.

    The access log records the full request target — path AND query string — beside the client
    address, and it is kept for as long as the box keeps logs. Two things must not be in there:

    * **a destination wallet in the path.** It would sit next to the IP that asked for it,
      forever. Redacted here; POST /v1/destinations/remove carries the address in the body.
    * **the `/internal` query string.** BeamPay sends no auth header, so its webhook
      credential can only ride in the URL (`?token=…`) — which the access log would then hold
      in cleartext. The whole query of every `/internal` line is dropped: a secret is never
      logged, and no future `/internal` route can leak one by forgetting about this filter.
      The prefix is matched through `is_internal_path`, the SAME reading the middleware guard
      uses — `//internal/x?token=…` and `/INTERNAL/x?token=…` are that route family too, and a
      filter that tested `startswith("/internal/")` on the raw target logged both in full.
    """

    PREFIX = "/v1/destinations/"
    KEEP = {PREFIX + "nonce", PREFIX + "remove"}

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            target = args[2]
            path, query, _ = target.partition("?")
            if target.startswith(self.PREFIX) and path not in self.KEEP:
                record.args = (*args[:2], self.PREFIX + "<redacted>", *args[3:])
            elif query and is_internal_path(path):
                record.args = (*args[:2], path + "?<redacted>", *args[3:])
        return True


_redactor = RedactAccessLog()


def install_access_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if _redactor not in access.filters:
        access.addFilter(_redactor)


def create_app() -> FastAPI:
    if not logging.getLogger().handlers:  # plain `uvicorn pgasme.main:app` configures nothing
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    # again here, and not only at import: uvicorn configures logging AFTER importing the app, so
    # a run that installs its own handlers or raises the root level must not un-quieten httpx.
    install_log_redaction()
    install_access_log_redaction()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            problems = await ensure_indexes()
        except Exception as e:  # noqa: BLE001 — mongo unreachable: name it, do not swallow it
            note_index_failure(f"ensure_indexes: {type(e).__name__}: {e}")
            problems = [f"ensure_indexes: {type(e).__name__}: {e}"]
        if problems:
            # Loud, and assertable: /v1/health says indexes_ok=false until a restart fixes it.
            log.error("INDEXES NOT READY — %d failed: %s", len(problems), " | ".join(problems))
        tasks = workers.start() if settings.workers_enabled else []
        log.info(
            "pgasme %s env=%s ingress_armed=%s workers=%d dev_endpoints=%s secrets_ok=%s indexes_ok=%s",
            __version__,
            settings.env,
            settings.ingress_ready,
            len(tasks),
            settings.dev_endpoints_active,
            settings.secrets_ok,
            not problems,
        )
        try:
            yield
        finally:
            await workers.stop(tasks)

    app = FastAPI(
        title="Pgas.me API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/v1/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )
    # ORDER MATTERS: `add_middleware` prepends, so the LAST one added is the outermost. CORS has
    # to be outside the 422 below or that refusal would reach a browser without its CORS headers
    # and the client would report a network error instead of the refusal. The /internal guard is
    # outside CORS on purpose and is the ONLY thing there: it answers nothing but `/internal`,
    # where a browser has no business at all, and a caller that has no business there must not
    # get as far as having its body read.
    app.add_middleware(RefuseNonFiniteJSON)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.add_middleware(InternalIsLoopbackOnly)
    # `internal` is mounted ALWAYS and refuses by itself (unconfigured token → 503, wrong token
    # → 401, anything not an un-proxied loopback caller → 404). Mounting it on a flag would mean
    # BeamPay's webhook worker gets a 404 and pages the operator's group on every attempt, which
    # is the flood this route exists to end. nginx refuses /api/internal at the edge as well.
    for r in (stats, siwe, account, destinations, dex, quote, deposits, withdrawals, internal):
        app.include_router(r.router)
    if settings.dev_endpoints_active:  # never in prod — config refuses to boot on that combination
        log.warning("/v1/dev/* is MOUNTED (PGAS_DEV_ENDPOINTS=1, env=%s)", settings.env)
        app.include_router(dev.router)
    return app


app = create_app()
