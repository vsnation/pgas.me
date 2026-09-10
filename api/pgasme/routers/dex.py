"""Public proxies of the cross-chain order router (chains, tokens) and our own asset table.

Balances are read CLIENT-SIDE (batch-balance view contracts, §6.0b); the backend only tells the
client which contract to call on which chain."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from .. import uniswap, xchain
from ..assets import ASSETS
from ..config import LEGACY_CHAIN_ID_FIELD
from ..tokens import normalise

router = APIRouter(tags=["dex"])

# EVM chain id → batch-balance view contract (balanceFor(address[] tokens, address account))
BATCH_BALANCE = {
    1: "0x50188692d5549386d102642036bab916b998c814",
    56: "0x50188692d5549386d102642036bab916b998c814",
    137: "0x50188692d5549386d102642036bab916b998c814",
    42161: "0x50188692d5549386d102642036bab916b998c814",
    10: "0x50188692d5549386d102642036bab916b998c814",
    8453: "0x202eF28cA6D4d2B94C4Ea0534a8E6261581c70a4",
    250: "0x55C93b20Dd2F790AC429D6341a022A781791654A",
    43114: "0x55C93b20Dd2F790AC429D6341a022A781791654A",
    59144: "0x0e4AdD4DC86Ae1Aa0FA43Bd7e6a9fB8Be2d5504d",
}

# the router does not return the native symbol; a small table for the EVM chains we know
NATIVE_SYMBOL = {
    1: "ETH",
    10: "ETH",
    56: "BNB",
    137: "POL",
    250: "FTM",
    8453: "ETH",
    42161: "ETH",
    43114: "AVAX",
    59144: "ETH",
    1514: "IP",
    25: "CRO",
    999: "HYPE",
    143: "MON",
    4326: "ETH",
    4663: "ETH",
    7565164: "SOL",
    728126428: "TRX",
    1776: "INJ",
}


def _xchain_error(e: xchain.XchainError) -> HTTPException:
    return HTTPException(400 if e.status and 400 <= e.status < 500 else 502, f"cross-chain router: {e}")


@router.get("/v1/dex/chains")
async def chains():
    try:
        rows = await xchain.supported_chains()
    except xchain.XchainError as e:
        raise _xchain_error(e) from e
    out = []
    for c in rows:
        try:
            evm = int(c["originalChainId"])
            internal = int(c["chainId"])
        except (KeyError, TypeError, ValueError):
            continue
        row = {
            "chain_id": evm,
            "route_chain_id": internal,
            # the field's older name, emitted alongside for one release so a client built
            # before the rename keeps resolving chains. `route_chain_id` is the one of record.
            LEGACY_CHAIN_ID_FIELD: internal,
            "name": c.get("chainName") or str(evm),
            "native_symbol": NATIVE_SYMBOL.get(evm, ""),
        }
        if evm in BATCH_BALANCE:
            row["batch_balance"] = BATCH_BALANCE[evm]
        out.append(row)
    return {"chains": out}


@router.get("/v1/dex/tokens")
async def tokens(
    response: Response, chain_id: int = Query(..., description="EVM (original) chain id")
):
    """The FALLBACK for one chain's token list.

    The list a visitor normally gets is the static `/tokens/<chain_id>.json` written by
    `pgasme.tokens` and served by nginx from the edge's cache; this route stays exactly as it
    was for the case that file is missing (a box that has never run the refresher, a chain the
    refresher could not fetch) — and it builds its rows with the SAME `normalise()` the static
    file is built with, so falling back changes the speed and never the answer.

    An hour of cache: the underlying list changes daily at most, and a client that fell back
    once should not pay for the proxy on every chain switch afterwards."""
    try:
        internal = await xchain.route_chain_id(chain_id)
        rows = await xchain.token_list(internal)
    except xchain.XchainError as e:
        raise _xchain_error(e) from e
    response.headers["Cache-Control"] = "public, max-age=3600"
    return {"tokens": normalise(rows)}


# `/v1/assets` is the path of record (API_CONTRACT.md); `/v1/dex/assets` is the same handler
# under the name the client and the work order both use for it. ONE implementation, two paths —
# never two handlers, which is how two answers to one question start.
@router.get("/v1/assets")
@router.get("/v1/dex/assets")
async def assets():
    """The target assets, and which ways in are open.

    `ingress.uniswap_tokens` is the registry itself — the source tokens the gateway route
    accepts right now — so the client never has to keep its own copy of a list that lives in
    the server's environment. It is EMPTY whenever the route is off or unusable, and the client
    treats a route the API does not state as off: an ingress that cannot be served must never
    look available."""
    flags = uniswap.ingress_flags()
    return {
        "assets": [
            {
                "key": a.key,
                "symbol": a.symbol,
                "beam_symbol": a.beam_symbol,
                "token": a.token,
                "decimals": a.eth_decimals,
                "pipe": a.pipe,
                "aid": a.aid,
            }
            for a in ASSETS.values()
        ],
        "ingress": {**flags, "uniswap_tokens": uniswap.public_tokens()},
    }
