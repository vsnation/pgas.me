"""Public numbers: GET /v1/stats (behind the honest anonymity indicator, §9.6) and GET /v1/health."""

from __future__ import annotations

import time

from fastapi import APIRouter

from .. import __version__, workers
from ..config import settings
from ..db import db

router = APIRouter(tags=["stats"])

LIVE_DEPOSIT = {"$nin": ["failed", "expired", "fallback_pending"]}


@router.get("/v1/stats")
async def stats() -> dict:
    now = time.time()
    d = db()
    deposits_24h = await d.deposits.count_documents(
        {"created_at": {"$gte": now - 86400}, "status": LIVE_DEPOSIT}
    )
    deposits_7d = await d.deposits.count_documents(
        {"created_at": {"$gte": now - 7 * 86400}, "status": LIVE_DEPOSIT}
    )
    payouts_24h = await d.payout_requests.count_documents(
        {"status": "sent", "updated_at": {"$gte": now - 86400}}
    )
    active = await d.distributors.count_documents({"state": "active"})
    return {
        "deposits_24h": deposits_24h,
        "deposits_7d": deposits_7d,
        "payouts_24h": payouts_24h,
        "pool": await workers.pool_stats(),
        "float": {"ETH": {"active_distributors": active, "wei": "0"}},
        "armed": {
            "ingress": settings.ingress_ready,
            "direct": settings.payout_direct_enabled,
            "instant": settings.payout_instant_enabled,
        },
    }


@router.get("/v1/health")
async def health() -> dict:
    mongo_ok = True
    try:
        await db().command("ping")
    except Exception:  # noqa: BLE001 — any failure means "mongo is not answering"
        mongo_ok = False
    return {
        "ok": mongo_ok,
        "version": __version__,
        "env": settings.env,
        "mongo": mongo_ok,
        "ingress_armed": settings.ingress_ready,
        "ingress_assets": {k: settings.ingress_ready_for(k) for k in ("ETH", "DAI", "WBTC")},
        "ingress_near": settings.ingress_near_enabled,
        "payout_direct": settings.payout_direct_enabled,
        "payout_instant": settings.payout_instant_enabled,
        "workers": settings.workers_enabled,
        "paused": workers.paused(),
    }
