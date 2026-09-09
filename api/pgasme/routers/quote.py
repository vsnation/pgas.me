"""POST /v1/quote — price a deposit through deBridge DLN and, only when armed, hand back the
transaction the user signs on the source chain.

Two-step DLN call:
  (a) an estimate with dstChainTokenOutAmount=auto → the recommended output on Ethereum;
      split it into (value, relayerFee) on the target asset's grid;
  (b) ONLY when settings.ingress_ready: the same order with that exact output amount and a
      dlnHook that calls the asset's pipe: sendFunds(value, relayerFee, OUR pipe pubkey).
      DLN's Universal Hook approves `payload.to` for the order's takeAmount and calls it (ERC-20),
      and forwards native value to the target for ETH; sendFunds pulls / expects exactly
      value + relayerFee == takeAmount, so the whole fill is locked in the pipe and the Beam
      side mints `value` (8 decimals) to our pubkey.
      Recorded live (2026-09-09): DLN prices the hook's gas into `recommendedAmount`, which then
      sits ≈ $0.20 below the hook-less estimate. An order above the recommendation may never be
      filled, so when that happens we re-quote ONCE on the recommended amount (the hook is
      rebuilt for it) and require the answer to echo it exactly — otherwise 409 "quote moved".
Verified against DLN's contract source + API spec (2026-09-09): an `evm_transaction_call` hook
is always success-required with reward 0 and fallbackAddress = dstChainTokenOutRecipient, and the
API pre-simulates the fill — a hook that would revert comes back as `errorId: HOOK_FAILED` (→ 409
here) instead of a transaction. The fallback recipient is the USER, never us: if the hook fails
on-chain the fill lands in their own wallet and nothing is lost.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from eth_utils import is_address, to_checksum_address
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, dln, ethpipe
from ..assets import Asset, PriceError, get_asset, to_groth, usd_prices
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/quote", tags=["quote"])

ZERO = "0x0000000000000000000000000000000000000000"


class QuoteIn(BaseModel):
    src_chain_id: int = Field(gt=0)
    src_token: str
    amount: str = Field(min_length=1, max_length=40, description="raw units, decimal string")
    target_asset: str = "ETH"
    sender: str | None = None


def _dln_error(e: dln.DlnError) -> HTTPException:
    if e.error_id == "HOOK_FAILED":  # DLN pre-simulates the fill; the hook would revert
        return HTTPException(
            409, "deBridge could not simulate the bridge call — try again or a different amount"
        )
    if e.status and 400 <= e.status < 500:
        return HTTPException(400, f"deBridge refused the quote: {e}")
    return HTTPException(502, f"deBridge unavailable: {e}")


def base_params(
    asset: Asset, user: str, dln_src: int, src_token: str, amount: int, metadata: str
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "srcChainId": dln_src,
        "srcChainTokenIn": src_token,
        "srcChainTokenInAmount": str(amount),
        "dstChainId": settings.eth_chain_id,
        "dstChainTokenOut": ZERO if asset.native else asset.token,
        "dstChainTokenOutAmount": "auto",
        "dstChainTokenOutRecipient": user,  # the user's own wallet is the fallback
        "srcChainOrderAuthorityAddress": user,
        "dstChainOrderAuthorityAddress": user,
        "senderAddress": user,
        "referralCode": settings.dln_referral_code,
        "prependOperatingExpenses": "false",
        "slippage": settings.dln_slippage,
        "metadata": metadata,
    }
    if settings.dln_affiliate_fee_percent > 0 and settings.dln_affiliate_recipient:
        p["affiliateFeePercent"] = settings.dln_affiliate_fee_percent
        p["affiliateFeeRecipient"] = settings.dln_affiliate_recipient
    return p


def build_hook(asset: Asset, value: int, relayer_fee: int, pubkey_hex: str) -> dict[str, Any]:
    calldata = ethpipe.encode_send_funds(value, relayer_fee, pubkey_hex)
    return {
        "type": "evm_transaction_call",
        "data": {"to": asset.pipe, "calldata": calldata, "gas": settings.hook_gas},
    }


async def check_min_deposit(asset: Asset, out_units: int) -> str | None:
    """None when the deposit clears the 0.02-ETH-equivalent floor; raises 400 when it does not;
    returns a note when the price is unavailable (allow, and say so)."""
    if asset.native:
        if out_units < settings.min_deposit_wei:
            raise HTTPException(
                400, f"below the minimum deposit of {settings.min_deposit_wei / 1e18:g} ETH"
            )
        return None
    try:
        px = await usd_prices()
    except PriceError as e:
        return f"minimum-deposit check skipped (price unavailable: {e})"
    out_usd = out_units / 10**asset.eth_decimals * px[asset.key]
    min_usd = settings.min_deposit_wei / 1e18 * px["ETH"]
    if out_usd < min_usd:
        raise HTTPException(
            400,
            f"below the minimum deposit (≈ ${min_usd:.2f}, the value of "
            f"{settings.min_deposit_wei / 1e18:g} ETH); this would be ≈ ${out_usd:.2f}",
        )
    return None


async def place_order(
    asset: Asset, params: dict[str, Any], out_units: int, pubkey: str
) -> tuple[dict[str, Any], int, int, int, dict[str, Any]]:
    """Step (b): the hooked order at an exact output. Returns (body, out_units, value, relayer_fee, hook)."""
    target = out_units
    for attempt in (0, 1):
        try:
            value, relayer_fee = ethpipe.split_for_asset(target, asset)
        except ethpipe.SplitError as e:
            raise HTTPException(400, f"amount too small: {e}") from e
        hook = build_hook(asset, value, relayer_fee, pubkey)
        p = {
            **params,
            "dstChainTokenOutAmount": str(target),
            "dlnHook": json.dumps(hook, separators=(",", ":")),
        }
        try:
            body = await dln.create_tx(p)
        except dln.DlnError as e:
            raise _dln_error(e) from e
        if dln.out_amount(body) != target:
            raise HTTPException(409, "quote moved between estimate and order — retry")
        rec = dln.recommended_amount(body)
        if rec is None or rec >= target:
            return body, target, value, relayer_fee, hook
        if attempt == 0:
            target = rec  # the hook's gas is priced in here; re-quote on it so solvers will fill
            continue
    raise HTTPException(409, "quote moved between estimate and order — retry")


@router.post("")
async def quote(body: QuoteIn, acct=auth.Account):
    try:
        asset = get_asset(body.target_asset)
    except KeyError as e:
        raise HTTPException(400, str(e.args[0])) from e
    if not is_address(body.src_token):
        raise HTTPException(
            400, "src_token must be an EVM token address (0x0 for the chain's native coin)"
        )
    src_token = to_checksum_address(body.src_token)
    try:
        amount = int(body.amount)
    except ValueError as e:
        raise HTTPException(400, "amount must be a decimal string of raw units") from e
    if amount <= 0:
        raise HTTPException(400, "amount must be positive")
    user = acct["address"]
    if body.sender and body.sender.lower() != user.lower():
        raise HTTPException(
            400, "sender must be the signed-in wallet (it is the order's refund authority)"
        )

    try:
        dln_src = await dln.dln_chain_id(body.src_chain_id)
    except dln.DlnError as e:
        raise _dln_error(e) from e

    metadata = "0x" + secrets.token_hex(5)
    params = base_params(asset, user, dln_src, src_token, amount, metadata)
    try:
        est = await dln.create_tx(params)
    except dln.DlnError as e:
        raise _dln_error(e) from e
    out_units = dln.out_amount(est)
    try:
        value, relayer_fee = ethpipe.split_for_asset(out_units, asset)
    except ethpipe.SplitError as e:
        raise HTTPException(400, f"amount too small: {e}") from e
    notes: list[str] = []
    n = await check_min_deposit(asset, out_units)
    if n:
        notes.append(n)

    armed: dict[str, Any] = {}
    pubkey = settings.pubkey_for(asset.key)
    if not settings.ingress_ready_for(asset.key):
        why = (
            "ingress is not armed"
            if not settings.ingress_armed
            else f"no Beam pipe pubkey configured for {asset.key}"
        )
        notes.append(f"estimate only — {why}; no transaction is issued")
    else:
        order, out_units, value, relayer_fee, hook = await place_order(
            asset, params, out_units, pubkey
        )
        tx = order.get("tx") or {}
        if not (tx.get("to") and tx.get("data")):
            raise HTTPException(502, "deBridge returned no transaction")
        tx_out = {
            "chain_id": body.src_chain_id,
            "to": tx["to"],
            "data": tx["data"],
            "value": str(tx.get("value") or "0"),
        }
        approval = None
        if tx.get("allowanceTarget"):
            approval = {
                "chain_id": body.src_chain_id,
                "token": src_token,
                "spender": tx["allowanceTarget"],
                "amount": str(tx.get("allowanceValue") or amount),
            }
        elif src_token.lower() != ZERO:
            # DlnSource pulls the input via transferFrom: approve the exact amount to `tx.to`
            approval = {
                "chain_id": body.src_chain_id,
                "token": src_token,
                "spender": tx["to"],
                "amount": str(amount),
            }
        armed = {
            "tx": tx_out,
            "approval": approval,
            "order_id": order.get("orderId"),
            "hook": hook,
            "dln_metadata": (order.get("order") or {}).get("metadata"),
        }

    src_meta = est.get("estimation", {}).get("srcChainTokenIn", {}) or {}
    dst_meta = est.get("estimation", {}).get("dstChainTokenOut", {}) or {}
    delay = int((est.get("order") or {}).get("approximateFulfillmentDelay") or 60)
    eta_s = delay + settings.lock_confirmations * 12 + 120
    usd = dst_meta.get("approximateUsdValue")
    estimate = {
        "src": {
            "chain_id": body.src_chain_id,
            "token": src_token,
            "symbol": src_meta.get("symbol") or "",
            "decimals": src_meta.get("decimals"),
            "amount": str(amount),
        },
        "out_units": str(out_units),
        "out_groth": to_groth(value, asset),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "usd": float(usd) if isinstance(usd, (int, float)) else None,
        "eta_s": eta_s,
        "dln_fees": {
            "fix_fee": est.get("fixFee"),
            "protocol_fee": est.get("protocolFee"),
            "estimated_tx_fee": (est.get("estimatedTransactionFee") or {}).get("total"),
            "operating_expense": src_meta.get("approximateOperatingExpense"),
            "costs": [
                {
                    "type": c.get("type"),
                    "amount_in": c.get("amountIn"),
                    "amount_out": c.get("amountOut"),
                }
                for c in (est.get("estimation", {}).get("costsDetails") or [])
            ],
        },
    }

    now = time.time()
    quote_id = secrets.token_hex(12)
    doc: dict[str, Any] = {
        "_id": quote_id,
        "account_id": acct["account_id"],
        "address": user,
        "asset": asset.key,
        "src": {
            "chain_id": body.src_chain_id,
            "dln_chain_id": dln_src,
            "token": src_token,
            "symbol": estimate["src"]["symbol"],
            "decimals": estimate["src"]["decimals"],
            "amount": str(amount),
        },
        "out_units": str(out_units),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "value_groth": to_groth(value, asset),
        "metadata": metadata,
        "armed": bool(armed),
        "at": now,
        "expires_at": now + settings.quote_ttl_s,
        "estimate": estimate,
    }
    resp: dict[str, Any] = {
        "quote_id": quote_id,
        "target_asset": asset.key,
        "armed": bool(armed),
        "expires_at": doc["expires_at"],
        "estimate": estimate,
    }
    if armed:
        doc.update(
            {
                "hook": armed["hook"],
                "hook_calldata": armed["hook"]["data"]["calldata"],
                "pubkey": pubkey,
                "order_id": armed["order_id"],
                "tx": armed["tx"],
                "approval": armed["approval"],
                "dln_metadata": armed["dln_metadata"],
            }
        )
        resp.update({"tx": armed["tx"], "order_id": armed["order_id"]})
        if armed["approval"]:
            resp["approval"] = armed["approval"]

    await db().quotes.insert_one(doc)
    if notes:
        resp["note"] = "; ".join(notes)
    return resp
