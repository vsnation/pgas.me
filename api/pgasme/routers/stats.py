"""Public numbers: GET /v1/stats (behind the honest anonymity indicator, §9.6) and GET /v1/health."""

from __future__ import annotations

import time

from fastapi import APIRouter

from .. import __version__, distributor, payouts, tokens, uniswap, utxo, workers
from ..config import settings
from ..db import db, index_error_labels, indexes_ok

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
    # ⛔ A REFILL IS NOT A PAYOUT (T34b M4). Topping the instant distributor up is a
    # treasury→own-address crossing that belongs to no user and debited no account; counting it
    # here tells the public — and us — that we served somebody we did not serve. The filter is
    # `payouts.not_a_refill()`, the ONE reader of that fact, so this counter and every other
    # public one exclude the same rows by the same rule.
    payouts_24h = await d.payout_requests.count_documents(
        {"status": "sent", "updated_at": {"$gte": now - 86400}, **payouts.not_a_refill()}
    )
    return {
        "deposits_24h": deposits_24h,
        "deposits_7d": deposits_7d,
        "payouts_24h": payouts_24h,
        "pool": await workers.pool_stats(),
        # ⛔ **THE LAST FLOAT READ IN THE CLEAR, AND IT IS GONE** (T40b (b)). This published
        # `float.ETH.wei` — the exact number of wei in our hot wallet, on an unauthenticated
        # route anybody can poll on a timer. T34b M5 took that off /v1/health for a reason and
        # this endpoint kept serving it beside a count of how many distributors were live:
        # together, an inventory of the wallet and a schedule of when it is thin. A visitor
        # needs to know the instant path EXISTS and is WELL, which is the same two booleans
        # /v1/health answers — the one projection (`distributor.public_summary`), never a second
        # implementation of it. The operator's full view is `beam status` and /admin/overview.
        "float": {"ETH": await distributor.public_summary()},
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
        # `ok` is what the watchdog and deploy.sh page on: mongo answering AND the indexes the
        # money path relies on being present. Weak secrets cannot reach here outside dev — the
        # process refuses to boot on them — so they stay a separate assertable field.
        "ok": mongo_ok and indexes_ok(),
        "version": __version__,
        "env": settings.env,
        "mongo": mongo_ok,
        # posture a deploy can assert: real secrets, no test mint mounted, indexes present
        "secrets_ok": settings.secrets_ok,
        "dev_endpoints": settings.dev_endpoints_active,
        "indexes_ok": indexes_ok(),
        "index_errors": index_error_labels(),
        # whether BeamPay's webhook can be delivered at all: with no token the route answers 503
        # to every delivery and BeamPay's worker pages the operator on every failed attempt. The
        # BOOLEAN only — the value is a credential and never leaves the process.
        "beampay_webhook": bool(settings.beampay_webhook_token),
        "ingress_armed": settings.ingress_ready,
        # which ways in this build will serve, plus which one `route:"auto"` resolves to
        # (/v1/dex/assets carries the registry itself). ONE implementation behind this,
        # /v1/dex/assets and /v1/account.
        "ingress": uniswap.ingress_flags(),
        # the hosted token lists: when the refresher last wrote them and how many chains are on
        # disk. `updated_at: null` means the files have never been written on this box — the
        # client then falls back to /v1/dex/tokens, and this is what the watchdog pages on
        # rather than discovering a six-day-old list through a user's slow picker.
        "tokens": tokens.health(),
        "ingress_assets": {k: settings.ingress_ready_for(k) for k in ("ETH", "DAI", "WBTC")},
        "ingress_near": settings.ingress_near_enabled,
        "payout_direct": settings.payout_direct_enabled,
        "payout_instant": settings.payout_instant_enabled,
        # the INSTANT payout distributor (T34) — ⛔ TWO BOOLEANS (T34b M5). This endpoint is
        # public and unauthenticated, and the address + float + next nonce it used to publish are
        # an inventory of a hot wallet and a schedule of what it is about to sign. The FULL
        # summary (`distributor.summary()`) is for `beam status` and the key-protected admin
        # panel. Database-only either way — a health endpoint that reaches Ethereum answers as
        # slowly as the slowest endpoint and fails when it does.
        "distributor": await distributor.public_summary(),
        # the crossings whose bETH is sitting at an address of their own, funded and not yet
        # burned (T40b F11) — ⛔ COUNTS, NEVER AMOUNTS, for the same reason the distributor's
        # float is two booleans here. A watchdog needs to see one that is stuck; nobody needs a
        # live read of what the treasury is holding. The groth figure is in /admin/overview.
        "crossings": await payouts.crossing_health(),
        # how many spendable COINS the wallet has free against the policy's target, per asset
        # (T36) — ⛔ COUNTS, NEVER AMOUNTS, the same law as `crossings` and the distributor
        # above. Beam locks a whole UTXO per pending transaction, so a treasury at `have: 1` is
        # a treasury that can carry one call at a time however much value it holds, and that is
        # what a watchdog needs to see. `have: null` is an unreadable wallet, never a zero.
        # Cached for 30 s: this endpoint is polled on a timer and the numbers behind it are a
        # BeamPay call plus a wallet-api walk.
        "coins": await utxo.coin_health(),
        # the gas basis every bridge crossing is priced on (T45) — ⛔ COUNTS, NEVER THE NUMBER,
        # the same law as `crossings` and `coins` above. A basis nobody is sampling is the
        # 2026-09-10 defect silently back (one reading of a price that moved 0.66 → 2.18 gwei in
        # an hour), and that is exactly what a count and an age make visible; what we price a
        # crossing at is not something an unauthenticated route publishes.
        "gas": await payouts.gas_health(),
        "workers": settings.workers_enabled,
        "paused": workers.paused(),
    }
