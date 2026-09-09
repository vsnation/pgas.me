"""python -m pgasme — run the API with uvicorn on PGAS_HOST:PGAS_PORT (default 127.0.0.1:8300)."""

from __future__ import annotations

import logging

import uvicorn

from .config import settings

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run("pgasme.main:app", host=settings.host, port=settings.port, log_level="info")
