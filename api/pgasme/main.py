"""FastAPI app factory. `uvicorn pgasme.main:app --host 127.0.0.1 --port 8300` or `python -m pgasme`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, workers
from .config import settings
from .db import ensure_indexes
from .routers import account, deposits, destinations, dev, dex, quote, siwe, stats, withdrawals

log = logging.getLogger("pgasme")


def create_app() -> FastAPI:
    if not logging.getLogger().handlers:  # plain `uvicorn pgasme.main:app` configures nothing
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await ensure_indexes()
        except Exception as e:  # noqa: BLE001 — /v1/health reports mongo=false; the process still answers
            log.error("ensure_indexes failed: %s: %s", type(e).__name__, e)
        tasks = workers.start() if settings.workers_enabled else []
        log.info(
            "pgasme %s env=%s ingress_armed=%s workers=%d dev_endpoints=%s",
            __version__,
            settings.env,
            settings.ingress_ready,
            len(tasks),
            settings.dev_endpoints,
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
    if settings.dev_endpoints:
        app.include_router(dev.router)
    return app


app = create_app()
