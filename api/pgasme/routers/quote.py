"""POST /v1/quote — price a deposit and, only when armed, hand back the transaction the user signs.

The mode is decided from the source chain and token BEFORE anything is called, and the response
always names it (API_CONTRACT.md, "Quote modes"):

  "dln"     the source chain is not Ethereum: the two-step DLN cross-chain order described below.
  "direct"  the source chain IS Ethereum and the source token IS the target asset's own token:
            no DLN call at all — the deposit is the user's own sendFunds(value, relayerFee,
            pubkey) on the asset's pipe, i.e. exactly the call the cross-chain hook would make.
  "swap"    the source chain IS Ethereum and the token is anything else: DLN's SINGLE-CHAIN swap
            endpoints (/chain/estimation then /chain/transaction) into the user's OWN wallet.
            Nothing of ours is at risk in that transaction and it is never registered as a
            deposit; the user quotes again in "direct" mode with what actually arrived.

DLN's order API refuses a same-chain order outright (SAME_SOURCE_AND_DESTINATION_CHAINS), so a
source chain of 1 must never reach create-tx — the two branches above are what keeps that error
away from the client.

Two-step DLN call (mode "dln"):
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

from .. import auth, dln, ethpipe, workers
from ..assets import Asset, PriceError, get_asset, to_groth, usd_prices
from ..config import settings
from ..db import db

router = APIRouter(prefix="/v1/quote", tags=["quote"])

ZERO = "0x0000000000000000000000000000000000000000"
# how long the user's own same-chain swap takes before they can quote the deposit (mode "swap")
SWAP_ETA_S = 120
# every quote costs us upstream calls: one account may ask this often per minute, then 429
QUOTE_CAP_PER_MIN = 20
QUOTE_CAP_WINDOW_S = 60


async def check_quote_cap(account_id: str) -> None:
    """A per-account ceiling on quotes per minute. nginx's per-IP limit cannot see an account,
    and one signed-in wallet behind many IPs is the case that costs us deBridge calls."""
    cap = int(getattr(settings, "quote_cap_per_min", QUOTE_CAP_PER_MIN))
    if cap <= 0:
        return
    now = time.time()
    since = now - QUOTE_CAP_WINDOW_S
    q = {"account_id": account_id, "at": {"$gte": since}}
    if await db().quotes.count_documents(q) < cap:
        return
    oldest = await db().quotes.find(q, {"at": 1}).sort("at", 1).limit(1).to_list(1)
    retry_after = max(1, int(QUOTE_CAP_WINDOW_S - (now - float(oldest[0]["at"])) + 1)) if oldest else QUOTE_CAP_WINDOW_S
    raise HTTPException(
        429,
        f"too many quotes — at most {cap} per minute per account; try again in {retry_after}s",
        headers={"Retry-After": str(retry_after)},
    )


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


def _why_unarmed(asset: Asset) -> str:
    return (
        "ingress is not armed"
        if not settings.ingress_armed
        else f"no Beam pipe pubkey configured for {asset.key}"
    )


def _split(amount: int, asset: Asset) -> tuple[int, int]:
    try:
        return ethpipe.split_for_asset(amount, asset)
    except ethpipe.SplitError as e:
        raise HTTPException(400, f"amount too small: {e}") from e


async def _usd(asset: Asset, units: int) -> float | None:
    """Best effort: a price outage blanks the field, it never fails the quote."""
    try:
        px = await usd_prices()
    except PriceError:
        return None
    return units / 10**asset.eth_decimals * px[asset.key]


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


async def _store(
    acct: dict[str, Any],
    user: str,
    asset: Asset,
    mode: str,
    src: dict[str, Any],
    out_units: int,
    value: int,
    relayer_fee: int,
    metadata: str,
    estimate: dict[str, Any],
    armed: bool,
    notes: list[str],
    doc_extra: dict[str, Any] | None = None,
    resp_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One writer for the quote document and its response — every mode goes through here."""
    now = time.time()
    quote_id = secrets.token_hex(12)
    doc: dict[str, Any] = {
        "_id": quote_id,
        "account_id": acct["account_id"],
        "address": user,
        "asset": asset.key,
        "mode": mode,
        "src": src,
        "out_units": str(out_units),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "value_groth": to_groth(value, asset),
        "metadata": metadata,
        "armed": armed,
        "at": now,
        "expires_at": now + settings.quote_ttl_s,
        "estimate": estimate,
        **(doc_extra or {}),
    }
    resp: dict[str, Any] = {
        "quote_id": quote_id,
        "mode": mode,
        "target_asset": asset.key,
        "armed": armed,
        "expires_at": doc["expires_at"],
        "estimate": estimate,
        **(resp_extra or {}),
    }
    await db().quotes.insert_one(doc)
    if notes:
        resp["note"] = "; ".join(notes)
    return resp


# ------------------------------------------------------------------- mode "dln" (cross-chain)


async def place_order(
    asset: Asset, params: dict[str, Any], out_units: int, pubkey: str
) -> tuple[dict[str, Any], int, int, int, dict[str, Any]]:
    """Step (b): the hooked order at an exact output. Returns (body, out_units, value, relayer_fee, hook)."""
    target = out_units
    for attempt in (0, 1):
        value, relayer_fee = _split(target, asset)
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


async def dln_quote(
    body: QuoteIn, acct: dict[str, Any], asset: Asset, src_token: str, amount: int, user: str
) -> dict[str, Any]:
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
    out_units = first_out = dln.out_amount(est)
    value, relayer_fee = _split(out_units, asset)
    notes: list[str] = []
    if n := await check_min_deposit(asset, out_units):
        notes.append(n)

    doc_extra: dict[str, Any] = {}
    resp_extra: dict[str, Any] = {}
    order: dict[str, Any] = {}
    pubkey = settings.pubkey_for(asset.key)
    armed = settings.ingress_ready_for(asset.key)
    if not armed:
        notes.append(f"estimate only — {_why_unarmed(asset)}; no transaction is issued")
    else:
        order, out_units, value, relayer_fee, hook = await place_order(
            asset, params, out_units, pubkey
        )
        if out_units != first_out:
            # the order was re-quoted on DLN's recommendation: the floor belongs on the amount
            # the user will actually receive, not on the one we first priced
            notes = [n for n in notes if "minimum-deposit check skipped" not in n]
            if n := await check_min_deposit(asset, out_units):
                notes.append(n)
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
        doc_extra = {
            "hook": hook,
            "hook_calldata": hook["data"]["calldata"],
            "pubkey": pubkey,
            "order_id": order.get("orderId"),
            "tx": tx_out,
            "approval": approval,
            "dln_metadata": (order.get("order") or {}).get("metadata"),
        }
        resp_extra = {"tx": tx_out, "order_id": order.get("orderId")}
        if approval:
            resp_extra["approval"] = approval

    src_meta = est.get("estimation", {}).get("srcChainTokenIn", {}) or {}
    dst_meta = est.get("estimation", {}).get("dstChainTokenOut", {}) or {}
    delay = int((est.get("order") or {}).get("approximateFulfillmentDelay") or 60)
    usd = dst_meta.get("approximateUsdValue")
    if order:  # the ORDER that was actually placed is what the estimate must describe
        ordered_dst = (order.get("estimation") or {}).get("dstChainTokenOut") or {}
        if "approximateUsdValue" in ordered_dst:
            usd = ordered_dst["approximateUsdValue"]
        elif out_units != first_out:
            usd = None  # priced for an amount that is no longer the one being ordered
        delay = int((order.get("order") or {}).get("approximateFulfillmentDelay") or delay)
    src = {
        "chain_id": body.src_chain_id,
        "token": src_token,
        "symbol": src_meta.get("symbol") or "",
        "decimals": src_meta.get("decimals"),
        "amount": str(amount),
    }
    estimate = {
        "src": src,
        "out_units": str(out_units),
        "out_groth": to_groth(value, asset),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "usd": float(usd) if isinstance(usd, (int, float)) else None,
        "eta_s": delay + settings.lock_confirmations * 12 + 120,
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
    return await _store(
        acct,
        user,
        asset,
        "dln",
        {**src, "dln_chain_id": dln_src},
        out_units,
        value,
        relayer_fee,
        metadata,
        estimate,
        armed,
        notes,
        doc_extra,
        resp_extra,
    )


# ---------------------------------------------------------------- mode "direct" (Ethereum → pipe)


async def direct_quote(
    acct: dict[str, Any], asset: Asset, amount: int, user: str
) -> dict[str, Any]:
    """The source token IS the target asset on Ethereum: nothing to bridge and nothing to swap.
    out_units == amount; the transaction is the pipe call itself, signed by the user."""
    value, relayer_fee = _split(amount, asset)
    notes: list[str] = []
    if n := await check_min_deposit(asset, amount):
        notes.append(n)
    src = {
        "chain_id": settings.eth_chain_id,
        "token": asset.token,
        "symbol": asset.symbol,
        "decimals": asset.eth_decimals,
        "amount": str(amount),
    }
    estimate = {
        "src": src,
        "out_units": str(amount),
        "out_groth": to_groth(value, asset),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "usd": await _usd(asset, amount),
        "eta_s": settings.lock_confirmations * 12 + 120,
    }

    doc_extra: dict[str, Any] = {}
    resp_extra: dict[str, Any] = {}
    pubkey = settings.pubkey_for(asset.key)
    armed = settings.ingress_ready_for(asset.key)
    if not armed:
        notes.append(f"estimate only — {_why_unarmed(asset)}; no transaction is issued")
    else:
        calldata = ethpipe.encode_send_funds(value, relayer_fee, pubkey)
        tx = {
            "chain_id": settings.eth_chain_id,
            "to": asset.pipe,
            "data": calldata,
            # EthPipe requires msg.value == value + relayerFee; the ERC-20 pipes pull it instead
            "value": str(amount) if asset.native else "0",
        }
        approval = (
            None
            if asset.native
            else {
                "chain_id": settings.eth_chain_id,
                "token": asset.token,
                "spender": asset.pipe,
                "amount": str(amount),
            }
        )
        doc_extra = {"hook_calldata": calldata, "pubkey": pubkey, "tx": tx, "approval": approval}
        resp_extra = {"tx": tx}
        if approval:
            resp_extra["approval"] = approval
    return await _store(
        acct,
        user,
        asset,
        "direct",
        src,
        amount,
        value,
        relayer_fee,
        "0x" + secrets.token_hex(5),  # kept for uniformity; nothing tags a direct deposit
        estimate,
        armed,
        notes,
        doc_extra,
        resp_extra,
    )


# -------------------------------------------------------------- mode "swap" (Ethereum → Ethereum)


async def swap_quote(
    acct: dict[str, Any], asset: Asset, src_token: str, amount: int, user: str
) -> dict[str, Any]:
    """Some other Ethereum token: DLN's single-chain swap into the user's OWN wallet, then a
    second `direct` quote for what actually arrives. We never touch the swap's output; the split
    below is informational, so the user can see what the deposit will look like."""
    p: dict[str, Any] = {
        "chainId": settings.eth_chain_id,
        "tokenIn": src_token,
        "tokenInAmount": str(amount),
        "tokenOut": asset.token,  # ZERO for ETH — assets.py already stores it that way
        "tokenOutAmount": "auto",
    }
    if settings.dln_affiliate_fee_percent > 0 and settings.dln_affiliate_recipient:
        p["affiliateFeePercent"] = settings.dln_affiliate_fee_percent
        p["affiliateFeeRecipient"] = settings.dln_affiliate_recipient
    try:
        est = await dln.chain_estimation(p)
        swap = await dln.chain_transaction(
            {
                **p,
                "tokenOutRecipient": user,  # the user's own wallet, never ours
                "senderAddress": user,
                "referralCode": settings.dln_referral_code,
            }
        )
    except dln.DlnError as e:
        raise _dln_error(e) from e

    out_units = dln.swap_out_amount(est)
    value, relayer_fee = _split(out_units, asset)
    tin = est.get("tokenIn") or {}
    tout = est.get("tokenOut") or {}
    usd = tout.get("approximateUsdValue")
    src = {
        "chain_id": settings.eth_chain_id,
        "token": src_token,
        "symbol": tin.get("symbol") or "",
        "decimals": tin.get("decimals"),
        "amount": str(amount),
    }
    estimate = {
        "src": src,
        "out_units": str(out_units),
        "out_groth": to_groth(value, asset),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "usd": float(usd) if isinstance(usd, (int, float)) else None,
        "eta_s": SWAP_ETA_S + settings.lock_confirmations * 12 + 120,
        "dln_fees": {
            "fix_fee": est.get("fixFee"),  # single-chain swaps have none; kept for one shape
            "protocol_fee": est.get("protocolFee"),
            "estimated_tx_fee": (est.get("estimatedTransactionFee") or {}).get("total"),
            "slippage": est.get("slippage"),
            "min_out_units": tout.get("minAmount"),
            "costs": [
                {
                    "type": c.get("type"),
                    "amount_in": c.get("amountIn"),
                    "amount_out": c.get("amountOut"),
                }
                for c in (est.get("costsDetails") or [])
            ],
        },
    }

    tx = swap["tx"]
    swap_tx = {
        "chain_id": settings.eth_chain_id,
        "to": tx["to"],
        "data": tx["data"],
        "value": str(tx.get("value") or "0"),
    }
    approval = None
    if src_token.lower() != ZERO:
        approval = {
            "chain_id": settings.eth_chain_id,
            "token": src_token,
            "spender": tx.get("allowanceTarget") or tx["to"],
            "amount": str(tx.get("allowanceValue") or amount),
        }
    nxt = {
        "src_chain_id": settings.eth_chain_id,
        "src_token": asset.token,
        "amount": str(out_units),
    }
    armed = settings.ingress_ready_for(asset.key)
    notes = [
        f"two steps: send this swap from your own wallet, then quote {asset.symbol} on Ethereum "
        f"with the amount that actually arrived and register THAT transaction as the deposit "
        f"(the minimum-deposit floor is checked on that second quote)"
    ]
    if not armed:
        notes.append(f"the deposit step is not available yet — {_why_unarmed(asset)}")
    resp_extra = {"swap_tx": swap_tx, "next": nxt}
    if approval:
        resp_extra["approval"] = approval
    return await _store(
        acct,
        user,
        asset,
        "swap",
        src,
        out_units,
        value,
        relayer_fee,
        "0x" + secrets.token_hex(5),
        estimate,
        armed,
        notes,
        {"swap_tx": swap_tx, "approval": approval, "next": nxt},
        resp_extra,
    )


# ----------------------------------------------------------------------------------- the route


@router.post("")
async def quote(body: QuoteIn, acct=auth.Account):
    await check_quote_cap(acct["account_id"])
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

    cross_chain = int(body.src_chain_id) != int(settings.eth_chain_id)
    into_the_pipe = cross_chain or src_token.lower() == asset.token.lower()
    # the kill switch reaches the request path: while it is set no quote may carry a transaction
    # that locks money in OUR pipe. An estimate costs nothing and still answers.
    if into_the_pipe and settings.ingress_ready_for(asset.key) and workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    if cross_chain:
        return await dln_quote(body, acct, asset, src_token, amount, user)
    if src_token.lower() == asset.token.lower():
        return await direct_quote(acct, asset, amount, user)
    return await swap_quote(acct, asset, src_token, amount, user)
