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
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__, workers
from .config import settings
from .db import ensure_indexes, note_index_failure
from .routers import account, deposits, destinations, dev, dex, quote, siwe, stats, withdrawals

log = logging.getLogger("pgasme")


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


class RedactDestinationPaths(logging.Filter):
    """uvicorn's access log records the full path beside the client address. A destination
    wallet in the URL would therefore sit in a log file next to the IP that asked for it —
    forever. Redact it there; POST /v1/destinations/remove carries it in the body instead."""

    PREFIX = "/v1/destinations/"
    KEEP = {PREFIX + "nonce", PREFIX + "remove"}

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            if path.startswith(self.PREFIX) and path.split("?")[0] not in self.KEEP:
                record.args = (*args[:2], self.PREFIX + "<redacted>", *args[3:])
        return True


_redactor = RedactDestinationPaths()


def install_access_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if _redactor not in access.filters:
        access.addFilter(_redactor)


def create_app() -> FastAPI:
    if not logging.getLogger().handlers:  # plain `uvicorn pgasme.main:app` configures nothing
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

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
    # to be outermost or the 422 below would reach a browser without its CORS headers and the
    # client would report a network error instead of the refusal.
    app.add_middleware(RefuseNonFiniteJSON)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    for r in (stats, siwe, account, destinations, dex, quote, deposits, withdrawals):
        app.include_router(r.router)
    if settings.dev_endpoints_active:  # never in prod — config refuses to boot on that combination
        log.warning("/v1/dev/* is MOUNTED (PGAS_DEV_ENDPOINTS=1, env=%s)", settings.env)
        app.include_router(dev.router)
    return app


app = create_app()
