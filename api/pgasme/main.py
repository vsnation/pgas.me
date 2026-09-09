"""FastAPI app factory. `uvicorn pgasme.main:app --host 127.0.0.1 --port 8300` or `python -m pgasme`.

Importing this module constructs Settings, which refuses to exist on an unsafe posture
(placeholder secrets outside dev, /v1/dev/* in prod) — so the process fails to boot instead of
serving with a forgeable session secret. /v1/health reports the posture it did boot with.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, workers
from .config import settings
from .db import ensure_indexes, note_index_failure
from .routers import account, deposits, destinations, dev, dex, quote, siwe, stats, withdrawals

log = logging.getLogger("pgasme")


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
