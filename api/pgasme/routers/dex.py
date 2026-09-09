"""Public proxies of deBridge (chains, tokens) and our own asset table.

Balances are read CLIENT-SIDE (batch-balance view contracts, §6.0b); the backend only tells the
client which contract to call on which chain."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import dln
from ..assets import ASSETS

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

# DLN does not return the native symbol; a small table for the EVM chains we know
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


def _dln_error(e: dln.DlnError) -> HTTPException:
    return HTTPException(400 if e.status and 400 <= e.status < 500 else 502, f"deBridge: {e}")


@router.get("/v1/dex/chains")
async def chains():
    try:
        rows = await dln.supported_chains()
    except dln.DlnError as e:
        raise _dln_error(e) from e
    out = []
    for c in rows:
        try:
            evm = int(c["originalChainId"])
            internal = int(c["chainId"])
        except (KeyError, TypeError, ValueError):
            continue
        row = {
            "chain_id": evm,
            "dln_chain_id": internal,
            "name": c.get("chainName") or str(evm),
            "native_symbol": NATIVE_SYMBOL.get(evm, ""),
        }
        if evm in BATCH_BALANCE:
            row["batch_balance"] = BATCH_BALANCE[evm]
        out.append(row)
    return {"chains": out}


@router.get("/v1/dex/tokens")
async def tokens(chain_id: int = Query(..., description="EVM (original) chain id")):
    try:
        internal = await dln.dln_chain_id(chain_id)
        rows = await dln.token_list(internal)
    except dln.DlnError as e:
        raise _dln_error(e) from e
    out = []
    for t in rows:
        addr = t.get("address")
        if not addr:
            continue
        out.append(
            {
                "address": addr,
                "symbol": t.get("symbol") or "",
                "name": t.get("name") or "",
                "decimals": int(t.get("decimals") or 0),
                "logo": t.get("logoURI") or "",
            }
        )
    return {"tokens": out}


@router.get("/v1/assets")
async def assets():
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
        ]
    }
