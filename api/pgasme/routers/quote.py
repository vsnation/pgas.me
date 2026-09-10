"""POST /v1/quote — price a deposit. POST /v1/quote/{id}/arm — build the transaction it signs.

TWO CALLS, BECAUSE ONE OF THEM IS SLOW (2026-09-09). An `xchain` quote used to make two or three
sequential router calls (~2 s each) inside the request the user is waiting on, and the user was
waiting on it every refresh — for a number, not for a transaction. So the price and the
transaction are now separate routes: `/v1/quote` makes EXACTLY ONE router call and answers
with an estimate, and `/v1/quote/{id}/arm` builds the hook-carrying order only when the user
has decided to deposit. `armed: true` on a quote means "ingress is ready for this asset", i.e.
it CAN be armed; the transaction exists only after /arm.

The mode is decided from the source chain and token BEFORE anything is called, and the response
always names it (API_CONTRACT.md, "Quote modes"). `xchain` is the name of record; the router's
own older name for it is still accepted on a stored row for one release (`xchain.norm_mode`):

  "uniswap" the source chain IS Ethereum and the pair is a registered canonical v4 pool: the
            user swaps on UNISWAP'S OWN Universal Router into their OWN wallet, then quotes again
            in "direct" mode with what actually arrived (`step: "swap"`, then `next`). Nothing of
            ours is deployed, nothing of ours is at risk in step 1, and the swap transaction is
            never registered as a deposit. Built at once — three chain reads and arithmetic — so
            there is no /arm step.
            With `PGAS_UNISWAP_HOOK_ENABLED=1` the same route name serves the reviewed
            ONE-TRANSACTION hook shape instead (gateway pool → our hook → the pipe, in one
            signature). That shape is not deployed and the flag is off.
  "xchain"  the source chain is not Ethereum: the cross-chain order described below.
  "direct"  the source chain IS Ethereum and the source token IS the target asset's own token:
            no router call at all — the deposit is the user's own sendFunds(value, relayerFee,
            pubkey) on the asset's pipe, i.e. exactly the call the cross-chain hook would make.
            Its transaction is pure arithmetic, so the quote carries it immediately.
  "swap"    the source chain IS Ethereum and the token is anything else: the router's SINGLE-CHAIN
            swap endpoints (/chain/estimation then /chain/transaction) into the user's OWN wallet.
            Nothing of ours is at risk in that transaction and it is never registered as a
            deposit; the user quotes again in "direct" mode with what actually arrived.

The router's order API refuses a same-chain order outright (SAME_SOURCE_AND_DESTINATION_CHAINS),
so a source chain of 1 must never reach create-tx — the two branches above are what keeps that
error away from the client.

Mode "xchain", the two steps:
  (a) POST /v1/quote → ONE create-tx with dstChainTokenOutAmount=auto → the recommended output
      on Ethereum, split into (value, relayerFee) on the target asset's grid. No hook, no tx,
      nothing stored that could be signed.
  (b) POST /v1/quote/{id}/arm → the same order at that exact output WITH a hook that calls
      the asset's pipe: sendFunds(value, relayerFee, OUR pipe pubkey).
      The router's universal hook approves `payload.to` for the order's takeAmount and calls it
      (ERC-20), and forwards native value to the target for ETH; sendFunds pulls / expects exactly
      value + relayerFee == takeAmount, so the whole fill is locked in the pipe and the Beam
      side mints `value` (8 decimals) to our pubkey.
      Recorded live (2026-09-09): the router prices the hook's gas into `recommendedAmount`, which
      then sits ≈ $0.20 below the hook-less estimate. An order above the recommendation may never
      be filled, so when that happens we re-quote on the recommended amount (the hook is rebuilt
      for it) and require the answer to echo the amount we asked for exactly — otherwise 409 "quote
      moved". Up to `ARM_ORDER_ATTEMPTS` = 3 attempts, EACH ORDERING AT EXACTLY THE LATEST
      RECOMMENDATION AND NEVER ABOVE IT: that recommendation drifts again between two consecutive
      calls about one time in three (~0.013%, converging on the next call), so two attempts
      refused about one Deposit click in three AFTER the user had clicked. It stays bounded —
      chasing a moving recommendation forever is a loop with no end — and the 409 that ends it
      names BOTH numbers (what the router now recommends and what it quoted a moment ago).
      /arm is IDEMPOTENT while the built transaction is fresh (ARM_FRESH_S): a second click
      hands back the same signed-nothing transaction instead of paying for another order.
Verified against the router's contract source + API spec (2026-09-09): an `evm_transaction_call`
hook is always success-required with reward 0 and fallbackAddress = dstChainTokenOutRecipient, and
the API pre-simulates the fill — a hook that would revert comes back as `errorId: HOOK_FAILED`
(→ 409 here) instead of a transaction. The fallback recipient is the USER, never us: if the hook
fails on-chain the fill lands in their own wallet and nothing is lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from eth_utils import is_address, to_checksum_address
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, beam, ethpipe, receiver_keys, uniswap, workers, xchain
from ..assets import Asset, PriceError, get_asset, to_groth, usd_prices
from ..config import LEGACY_CHAIN_ID_FIELD, ROUTER_HOOK_PARAM, settings
from ..db import db

log = logging.getLogger("pgasme.quote")

router = APIRouter(prefix="/v1/quote", tags=["quote"])

ZERO = "0x0000000000000000000000000000000000000000"
# how long the user's own same-chain swap takes before they can quote the deposit (mode "swap")
SWAP_ETA_S = 120
# the same wait for the two-step Uniswap route: the swap is one transaction on one pool, so the
# wait is the receipt — two blocks, not a solver's fulfilment delay.
UNISWAP_SWAP_ETA_S = 24
# every quote costs us upstream calls: one account may ask this often per minute, then 429
QUOTE_CAP_PER_MIN = 20
QUOTE_CAP_WINDOW_S = 60
# how long a built order transaction is handed back unchanged. The router prices an order at the
# moment it is built, so a stale one is a worse deal for the user (or unfillable); a fresh one
# is the SAME transaction and re-asking for it must not cost another upstream order.
ARM_FRESH_S = 180.0
# one quote may be armed this many times before it is refused: /arm is the only route that can
# make an upstream call without creating a quote row, so the per-minute quote cap cannot see it
ARM_MAX_TRIES = 20
# how many times ONE /arm may re-quote on the router's `recommendedAmount` before it gives up.
# The decision of record: "/arm re-quotes on the router's
# recommendedAmount up to 3 times (never orders above the recommendation), then 409 'quote
# moved' — the measured 1-in-3 second-step drift is market noise". The code did two, so about
# one Deposit click in three answered 409 AFTER the user had clicked, on a drift that the third
# attempt converges on.
ARM_ORDER_ATTEMPTS = 3
# ⛔ ONE /arm AT A TIME PER QUOTE. The "idempotent while fresh" test is a read-then-write, so two
# concurrent calls (a double-click, a retried request) both found no stored order and both placed
# one: two live orders for one quote, and the caller that got the first was handed a
# transaction whose id and amounts the quote no longer carries. The arm is CLAIMED before
# anybody calls the router; a claim older than this belonged to a request that died mid-flight.
ARM_LOCK_S = 30.0
ARM_WAIT_S = 3.0  # how long the second caller waits for the first one's order before 409
ARM_POLL_S = 0.05
BPS = 10_000
# The routes a client may ask for by name. "auto" is the default and picks the best one that is
# actually available for the pair (uniswap → direct → swap → xchain).
ROUTE_CHOICES = ("auto", uniswap.MODE, "direct", "swap", xchain.MODE)
# What a paused route says. Neutral on purpose: a user does not need to know which upstream is
# switched off, only that this way in is closed right now and the others are not.
XCHAIN_PAUSED = "cross-chain deposits are paused — deposit from Ethereum instead"
UNISWAP_UNAVAILABLE = "the Uniswap route is not available"


def refuse(status: int, detail: str, **ctx: Any) -> HTTPException:
    """Every decision path writes a row. A refusal is not a trade and not a failure — but a
    refusal nobody can see is how a wrong cap ran for 23.5 hours before anyone noticed, so each
    one leaves a line naming the route, the reason and the account it was refused for."""
    log.warning("quote refused %d: %s (%s)", status, detail, ctx)
    return HTTPException(status, detail)


async def check_quote_cap(account_id: str) -> None:
    """A per-account ceiling on quotes per minute. nginx's per-IP limit cannot see an account,
    and one signed-in wallet behind many IPs is the case that costs us router calls."""
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
    route: str | None = Field(
        None, max_length=16, description='"uniswap" | "direct" | "swap" | "xchain" | "auto"'
    )


def _xchain_error(e: xchain.XchainError) -> HTTPException:
    if e.error_id == "HOOK_FAILED":  # the router pre-simulates the fill; the hook would revert
        return HTTPException(
            409, "the router could not simulate the bridge call — try again or a different amount"
        )
    if e.status and 400 <= e.status < 500:
        return HTTPException(400, f"the router refused the quote: {e}")
    return HTTPException(502, f"the router is unavailable: {e}")


def _why_unarmed(asset: Asset) -> str:
    return (
        "ingress is not armed"
        if not settings.ingress_armed
        else f"no Beam pipe pubkey configured for {asset.key}"
    )


async def issue_receiver_key(asset: Asset) -> tuple[int, str]:
    """The Beam receiver key this quote's calldata will name — its OWN one when
    `PGAS_RECEIVER_KEY_PER_DEPOSIT` is on, otherwise the pipe's legacy key exactly as before.

    ⛔ AN UNREADABLE WALLET REFUSES THE QUOTE. It does NOT fall back to the legacy key: that
    would hand this user calldata naming the receiver every other deposit shares, silently undo
    the property the flag was turned on for, and record on the row that it had done no such
    thing. 503 says "ask again", which is true and is the only honest answer."""
    if not receiver_keys.enabled():
        return receiver_keys.LEGACY_INDEX, settings.pubkey_for(asset.key)
    try:
        return await receiver_keys.issue(asset)
    except (beam.BeamError, beam.Halted, ValueError) as e:
        raise refuse(
            503,
            "could not issue a Beam receiver key for this deposit — try again",
            asset=asset.key,
            why=str(e)[:200],
        ) from e


async def for_quote_key(asset: Asset, q: dict[str, Any]) -> tuple[int, str]:
    """`receiver_keys.for_quote` with the same refusal `issue_receiver_key` makes: an /arm that
    cannot read a key answers 503 rather than arming the legacy one behind the user's back."""
    try:
        return await receiver_keys.for_quote(asset, q)
    except (beam.BeamError, beam.Halted, ValueError) as e:
        raise refuse(
            503,
            "could not issue a Beam receiver key for this deposit — try again",
            asset=asset.key,
            quote_id=q.get("_id"),
            why=str(e)[:200],
        ) from e


def _split(amount: int, asset: Asset) -> tuple[int, int]:
    try:
        return ethpipe.split_for_asset(amount, asset)
    except ethpipe.SplitError as e:
        raise HTTPException(400, f"amount too small: {e}") from e


def _split_at(amount: int, relayer_fee: int, asset: Asset, what: str) -> tuple[int, int]:
    """The same split with an EXPLICIT relayer-fee quote — the Uniswap hook is handed the fee in
    `hookData` and splits the REAL output by it on-chain, so what the user is shown has to be
    the same arithmetic on the same number (law 9), not `split_for_asset`'s own floor."""
    try:
        return ethpipe.split_amount(amount, relayer_fee, asset.grid)
    except ethpipe.SplitError as e:
        raise refuse(400, f"amount too small ({what}): {e}", amount=amount, fee=relayer_fee) from e


async def _usd(asset: Asset, units: int) -> float | None:
    """Best effort: a price outage blanks the field, it never fails the quote."""
    try:
        px = await usd_prices()
    except PriceError:
        return None
    return units / 10**asset.eth_decimals * px[asset.key]


def base_params(
    asset: Asset, user: str, route_src: int, src_token: str, amount: int, metadata: str
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "srcChainId": route_src,
        "srcChainTokenIn": src_token,
        "srcChainTokenInAmount": str(amount),
        "dstChainId": settings.eth_chain_id,
        "dstChainTokenOut": ZERO if asset.native else asset.token,
        "dstChainTokenOutAmount": "auto",
        "dstChainTokenOutRecipient": user,  # the user's own wallet is the fallback
        "srcChainOrderAuthorityAddress": user,
        "dstChainOrderAuthorityAddress": user,
        "senderAddress": user,
        "referralCode": settings.xchain_referral_code,
        "prependOperatingExpenses": "false",
        "slippage": settings.xchain_slippage,
        "metadata": metadata,
    }
    if settings.xchain_affiliate_fee_percent > 0 and settings.xchain_affiliate_recipient:
        p["affiliateFeePercent"] = settings.xchain_affiliate_fee_percent
        p["affiliateFeeRecipient"] = settings.xchain_affiliate_recipient
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


# ---------------------------------------------------------------- mode "xchain" (cross-chain)


async def place_order(
    asset: Asset, params: dict[str, Any], out_units: int, pubkey: str
) -> tuple[dict[str, Any], int, int, int, dict[str, Any]]:
    """Step (b): the hooked order at an exact output. Returns (body, out_units, value, relayer_fee, hook).

    UP TO `ARM_ORDER_ATTEMPTS` ATTEMPTS, EACH AT EXACTLY THE LATEST RECOMMENDATION AND NEVER
    ABOVE IT. The router prices the hook's gas into `recommendedAmount`, so the first order — built
    at the hook-less estimate — is above what solvers will fill; the re-quote is placed at the
    recommendation itself. Measured live 2026-09-09, that recommendation drifts again between two
    consecutive calls about one time in three, and the drift is small (~0.013%) and converges on
    the next call. Two attempts therefore refused a third of all Deposit clicks AFTER the user had
    clicked; three attempts is the decision of record. It is still bounded — chasing a moving
    recommendation forever is a loop with no end — and the refusal names both numbers so the
    client can say what moved."""
    target = out_units
    rec: int | None = None
    for attempt in range(ARM_ORDER_ATTEMPTS):
        value, relayer_fee = _split(target, asset)
        hook = build_hook(asset, value, relayer_fee, pubkey)
        p = {
            **params,
            "dstChainTokenOutAmount": str(target),
            ROUTER_HOOK_PARAM: json.dumps(hook, separators=(",", ":")),
        }
        try:
            body = await xchain.create_tx(p)
        except xchain.XchainError as e:
            raise _xchain_error(e) from e
        got = xchain.out_amount(body)
        if got != target:
            raise HTTPException(
                409,
                f"quote moved between estimate and order — retry (we asked for {target} and "
                f"the router answered {got})",
            )
        rec = xchain.recommended_amount(body)
        if rec is None or rec >= target:
            return body, target, value, relayer_fee, hook
        if attempt < ARM_ORDER_ATTEMPTS - 1:
            target = rec  # the hook's gas is priced in here; re-quote on it so solvers will fill
            continue
    raise HTTPException(
        409,
        f"quote moved between estimate and order — retry (the router now recommends {rec}, "
        f"below the {target} it quoted a moment ago)",
    )


def xchain_src_meta(est: dict[str, Any]) -> dict[str, Any]:
    return (est.get("estimation", {}) or {}).get("srcChainTokenIn", {}) or {}


def xchain_estimate(
    src: dict[str, Any], asset: Asset, body: dict[str, Any], value: int, relayer_fee: int
) -> dict[str, Any]:
    """The `estimate` block, built from ONE router answer — the auto-amount estimate at quote
    time, or the placed order at arm time. One implementation, so the two can never describe the
    same fill differently (law 9: two implementations of one fact will disagree)."""
    src_meta = xchain_src_meta(body)
    dst_meta = (body.get("estimation", {}) or {}).get("dstChainTokenOut", {}) or {}
    usd = dst_meta.get("approximateUsdValue")
    delay = int((body.get("order") or {}).get("approximateFulfillmentDelay") or 60)
    return {
        "src": src,
        "out_units": str(value + relayer_fee),
        "out_groth": to_groth(value, asset),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "usd": float(usd) if isinstance(usd, (int, float)) else None,
        "eta_s": delay + settings.lock_confirmations * 12 + 120,
        "route_fees": {
            "fix_fee": body.get("fixFee"),
            "protocol_fee": body.get("protocolFee"),
            "estimated_tx_fee": (body.get("estimatedTransactionFee") or {}).get("total"),
            "operating_expense": src_meta.get("approximateOperatingExpense"),
            "costs": [
                {
                    "type": c.get("type"),
                    "amount_in": c.get("amountIn"),
                    "amount_out": c.get("amountOut"),
                }
                for c in ((body.get("estimation", {}) or {}).get("costsDetails") or [])
            ],
        },
    }


def order_tx(chain_id: int, src_token: str, amount: int, order: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(tx, approval) for a placed cross-chain order. Raises 502 when the router returned no transaction —
    an order we cannot hand over is not an order."""
    tx = order.get("tx") or {}
    if not (tx.get("to") and tx.get("data")):
        raise HTTPException(502, "the router returned no transaction")
    tx_out = {
        "chain_id": chain_id,
        "to": tx["to"],
        "data": tx["data"],
        "value": str(tx.get("value") or "0"),
    }
    approval = None
    if tx.get("allowanceTarget"):
        approval = {
            "chain_id": chain_id,
            "token": src_token,
            "spender": tx["allowanceTarget"],
            "amount": str(tx.get("allowanceValue") or amount),
        }
    elif src_token.lower() != ZERO:
        # the router's source contract pulls the input via transferFrom: approve exactly `tx.to`
        approval = {
            "chain_id": chain_id,
            "token": src_token,
            "spender": tx["to"],
            "amount": str(amount),
        }
    return tx_out, approval


async def xchain_quote(
    body: QuoteIn, acct: dict[str, Any], asset: Asset, src_token: str, amount: int, user: str
) -> dict[str, Any]:
    """Step (a): ONE create-tx call, the price only. Nothing signable is built or stored here —
    `armed` says whether /arm COULD build one, and /arm is what builds it."""
    try:
        route_src = await xchain.route_chain_id(body.src_chain_id)
    except xchain.XchainError as e:
        raise _xchain_error(e) from e

    metadata = "0x" + secrets.token_hex(5)
    params = base_params(asset, user, route_src, src_token, amount, metadata)
    try:
        est = await xchain.create_tx(params)
    except xchain.XchainError as e:
        raise _xchain_error(e) from e
    out_units = xchain.out_amount(est)
    value, relayer_fee = _split(out_units, asset)
    notes: list[str] = []
    if n := await check_min_deposit(asset, out_units):
        notes.append(n)
    armed = settings.ingress_ready_for(asset.key)
    if not armed:
        notes.append(f"estimate only — {_why_unarmed(asset)}; no transaction is issued")

    src = {
        "chain_id": body.src_chain_id,
        "token": src_token,
        "symbol": xchain_src_meta(est).get("symbol") or "",
        "decimals": xchain_src_meta(est).get("decimals"),
        "amount": str(amount),
    }
    estimate = xchain_estimate(src, asset, est, value, relayer_fee)
    return await _store(
        acct,
        user,
        asset,
        xchain.MODE,
        {**src, "route_chain_id": route_src},
        out_units,
        value,
        relayer_fee,
        metadata,
        estimate,
        armed,
        notes,
    )


async def await_arm_in_flight(quote_id: str, asset: Asset) -> dict[str, Any]:
    """Another request is placing this quote's order right now: wait for ITS order and hand that
    back — never place a second one.

    The "idempotent while fresh" contract was a read-then-write: two /arm calls issued together
    (a double-click, or a retried request) both evaluated `tx and order_id and fresh` on a
    document neither had written yet, so both called the router and TWO live orders existed for one
    quote. The caller that got the first was then holding a transaction whose id and amounts the
    quote no longer carried — which is the whole class of amount-divergence bug reachable with no
    180-second wait and no client bug at all, plus a second paid-for order every time."""
    deadline = time.time() + ARM_WAIT_S
    while time.time() < deadline:
        await asyncio.sleep(ARM_POLL_S)
        q = await db().quotes.find_one({"_id": quote_id})
        if not q:
            break
        if q.get("arm_in_flight"):
            continue  # still working
        if q.get("tx") and q.get("order_id"):
            return stored_arm(q, asset)  # THEIR order, handed to us as well
        break  # it finished without an order: it failed, and so does this one
    raise HTTPException(
        409,
        "this quote is being armed by another request — retry in a moment",
        headers={"Retry-After": "2"},
    )


def stored_arm(q: dict[str, Any], asset: Asset) -> dict[str, Any]:
    """The transaction this quote was already armed with. Handing the stored one back is not a
    cache of a price — it IS the order that exists upstream; building a second one would leave
    the first unused and cost the user another quote."""
    resp: dict[str, Any] = {
        "quote_id": q["_id"],
        "mode": xchain.norm_mode(q.get("mode")),
        "target_asset": asset.key,
        "armed": True,
        "expires_at": q["expires_at"],
        "estimate": q["estimate"],
        "tx": q["tx"],
    }
    if q.get("order_id"):
        resp["order_id"] = q["order_id"]
    if q.get("approval"):
        resp["approval"] = q["approval"]
    for k in ("deposit_ref", "route"):  # mode "uniswap" carries these instead of an order id
        if q.get(k):
            resp[k] = q[k]
    return resp


async def arm_direct(q: dict[str, Any], asset: Asset) -> dict[str, Any]:
    """Mode "direct": the transaction is the pipe call itself, so there is nothing to ask
    anybody. It is rebuilt from the quote's own split every time — the same bytes for the same
    quote, which is what makes this route idempotent without a freshness window."""
    value, relayer_fee = int(q["value_units"]), int(q["relayer_fee_units"])
    # ⛔ THE QUOTE'S OWN KEY, ALLOCATED AT MOST ONCE. `for_quote` hands back the key this quote
    # already holds — which is what keeps this route idempotent: the same bytes for the same
    # quote, every time. A fresh key per /arm would mean the user could sign an earlier call
    # naming a receiver the quote no longer records.
    _index, pubkey = await for_quote_key(asset, q)
    calldata = ethpipe.encode_send_funds(value, relayer_fee, pubkey)
    tx = {
        "chain_id": settings.eth_chain_id,
        "to": asset.pipe,
        "data": calldata,
        # EthPipe requires msg.value == value + relayerFee; the ERC-20 pipes pull it instead
        "value": str(int(q["out_units"])) if asset.native else "0",
    }
    approval = (
        None
        if asset.native
        else {
            "chain_id": settings.eth_chain_id,
            "token": asset.token,
            "spender": asset.pipe,
            "amount": str(int(q["out_units"])),
        }
    )
    await db().quotes.update_one(
        {"_id": q["_id"]},
        {
            "$set": {
                "hook_calldata": calldata,
                "pubkey": pubkey,
                "tx": tx,
                "approval": approval,
                "armed": True,
                "armed_at": time.time(),
            }
        },
    )
    return stored_arm({**q, "tx": tx, "approval": approval}, asset)


async def arm_xchain(q: dict[str, Any], asset: Asset) -> dict[str, Any]:
    """Step (b) for one stored quote: place the hooked order NOW and keep what it returned.

    Everything the order needs is already on the quote document, so it is rebuilt through the
    same `base_params` the estimate used — the parameters are never a second stored copy that
    could drift from the quote they belong to."""
    src = dict(q.get("src") or {})
    # a quote stored before the rename carries the router's own name for this field
    route_src = int(src.get("route_chain_id") or src.get(LEGACY_CHAIN_ID_FIELD) or 0)
    if not route_src:
        raise HTTPException(409, "this quote cannot be armed — request a new one")
    src_token = to_checksum_address(src["token"])
    amount = int(src["amount"])
    user = q["address"]
    # allocated at most once per quote and reused by every later /arm: every order this quote
    # was ever armed with stays registrable, so they must all name ONE receiver
    _index, pubkey = await for_quote_key(asset, q)
    params = base_params(asset, user, route_src, src_token, amount, q["metadata"])
    order, out_units, value, relayer_fee, hook = await place_order(
        asset, params, int(q["out_units"]), pubkey
    )
    notes: list[str] = []
    if out_units != int(q["out_units"]):
        # the order was re-quoted on the router's recommendation: the floor belongs on the amount the
        # user will actually receive, not on the one we first priced
        if n := await check_min_deposit(asset, out_units):
            notes.append(n)
    public_src = {
        k: v for k, v in src.items() if k not in ("route_chain_id", LEGACY_CHAIN_ID_FIELD)
    }
    tx_out, approval = order_tx(int(src["chain_id"]), src_token, amount, order)
    estimate = xchain_estimate(public_src, asset, order, value, relayer_fee)
    fields: dict[str, Any] = {
        "hook": hook,
        "hook_calldata": hook["data"]["calldata"],
        "pubkey": pubkey,
        "order_id": order.get("orderId"),
        "tx": tx_out,
        "approval": approval,
        "route_metadata": (order.get("order") or {}).get("metadata"),
        "out_units": str(out_units),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "value_groth": to_groth(value, asset),
        "estimate": estimate,
        "armed": True,
        "armed_at": time.time(),
    }
    # EVERY order this quote has ever been armed with is remembered, WITH THE AMOUNTS IT WAS
    # BUILT AT. A rebuilt order does not cancel the one before it — an unsigned order exists
    # only as a transaction we handed over, so if the user signs the earlier one it is still THIS
    # quote's order and must still be registrable (routers/deposits.resolve_xchain reads this list).
    # An answer with no orderId adds nothing: a null in that list is not an id and must never be
    # matched against one.
    #
    # ⛔ THE ORDER CARRIES THE AMOUNT, NOT THE QUOTE'S TIP. `fields` above overwrites the quote's
    # ONE amount snapshot on every arm, and both the deposit row and the scanner read that
    # snapshot — so an id the user signed earlier was priced at the LATEST order's numbers.
    # Measured: a $10 deposit credited 8,100 groth of house money over what actually arrived
    # (the gap is the router pricing the 250k-gas hook into `recommendedAmount`, so it scales with gas),
    # and the same divergence the other way stranded the user's money with no automatic path to
    # credit it. Every order now keeps its own numbers, and the id that matched picks them.
    update: dict[str, Any] = {"$set": fields}
    if order.get("orderId"):
        update["$addToSet"] = {"order_ids_armed": order["orderId"]}
        update["$push"] = {
            "orders_armed": {
                "order_id": order["orderId"],
                "out_units": str(out_units),
                "value_units": str(value),
                "relayer_fee_units": str(relayer_fee),
                "value_groth": to_groth(value, asset),
                "at": time.time(),
            }
        }
    await db().quotes.update_one({"_id": q["_id"]}, update)
    resp = {
        "quote_id": q["_id"],
        "mode": xchain.MODE,
        "target_asset": asset.key,
        "armed": True,
        "expires_at": q["expires_at"],
        "estimate": estimate,
        "tx": tx_out,
        "order_id": order.get("orderId"),
    }
    if approval:
        resp["approval"] = approval
    if notes:
        resp["note"] = "; ".join(notes)
    return resp


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
    index = receiver_keys.LEGACY_INDEX
    armed = settings.ingress_ready_for(asset.key)
    if not armed:
        notes.append(f"estimate only — {_why_unarmed(asset)}; no transaction is issued")
    else:
        # ⛔ ONLY AN ARMED QUOTE ALLOCATES. An estimate issues no calldata, so allocating for one
        # would burn an index — and the counter never hands the same one back — on every price
        # refresh of a route that cannot be signed.
        index, pubkey = await issue_receiver_key(asset)
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
        doc_extra = {
            "hook_calldata": calldata,
            "pubkey": pubkey,
            "tx": tx,
            "approval": approval,
            # nothing at all when the key is the legacy one, so a row written with the flag off
            # is byte-identical to one written before this existed
            **receiver_keys.quote_fields(index, pubkey),
        }
        resp_extra = {"tx": tx}
        if approval:
            resp_extra["approval"] = approval
    resp = await _store(
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
    # evidence, written after the row it points at exists. The QUOTE is the authority on which
    # key it holds; this only lets the operator read the key cache and see where each one went.
    await receiver_keys.bind(asset, index, resp["quote_id"])
    return resp


# -------------------------------------------------------------- mode "swap" (Ethereum → Ethereum)


async def swap_quote(
    acct: dict[str, Any], asset: Asset, src_token: str, amount: int, user: str
) -> dict[str, Any]:
    """Some other Ethereum token: the router's single-chain swap into the user's OWN wallet, then a
    second `direct` quote for what actually arrives. We never touch the swap's output; the split
    below is informational, so the user can see what the deposit will look like."""
    p: dict[str, Any] = {
        "chainId": settings.eth_chain_id,
        "tokenIn": src_token,
        "tokenInAmount": str(amount),
        "tokenOut": asset.token,  # ZERO for ETH — assets.py already stores it that way
        "tokenOutAmount": "auto",
    }
    if settings.xchain_affiliate_fee_percent > 0 and settings.xchain_affiliate_recipient:
        p["affiliateFeePercent"] = settings.xchain_affiliate_fee_percent
        p["affiliateFeeRecipient"] = settings.xchain_affiliate_recipient
    try:
        est = await xchain.chain_estimation(p)
        swap = await xchain.chain_transaction(
            {
                **p,
                "tokenOutRecipient": user,  # the user's own wallet, never ours
                "senderAddress": user,
                "referralCode": settings.xchain_referral_code,
            }
        )
    except xchain.XchainError as e:
        raise _xchain_error(e) from e

    out_units = xchain.swap_out_amount(est)
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
        "route_fees": {
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


# ------------------------------------------------- mode "uniswap" (Ethereum → Uniswap → Beam)


async def uniswap_quote(
    acct: dict[str, Any],
    asset: Asset,
    route: uniswap.Route,
    amount: int,
    user: str,
) -> dict[str, Any]:
    """The `uniswap` route, in whichever shape this box serves.

    ONE reader of that decision (`uniswap.hook_enabled`), because the shape decides three things
    that must agree: which addresses have to be configured, whether the quote hands over a
    transaction that reaches OUR pipe (and is therefore gated by the kill switch), and whether
    the resulting transaction may ever be registered as a deposit."""
    if uniswap.hook_enabled():
        return await uniswap_hook_quote(acct, asset, route, amount, user)
    return await uniswap_two_step_quote(acct, asset, route, amount, user)


def check_route_bounds(route: uniswap.Route, amount: int) -> None:
    """The route's own cap and floor, in units of the SOURCE token. Shared by both shapes so a
    size one accepts is never a size the other refuses."""
    if amount > route.max_deposit_units:
        raise refuse(
            400,
            f"above this route's cap of {route.max_deposit_units} units of "
            f"{route.symbol or route.token_in} — the cap comes from the pool's live depth",
            token=route.token_in,
            amount=amount,
        )
    if amount < route.min_deposit_units:
        raise refuse(
            400,
            f"below this route's minimum of {route.min_deposit_units} units of "
            f"{route.symbol or route.token_in}",
            token=route.token_in,
            amount=amount,
        )


async def uniswap_two_step_quote(
    acct: dict[str, Any],
    asset: Asset,
    route: uniswap.Route,
    amount: int,
    user: str,
) -> dict[str, Any]:
    """STEP 1 OF TWO, and nothing of ours is deployed or at risk in it.

    The user swaps their token for the target asset on UNISWAP'S OWN Universal Router, into
    their OWN wallet (`TAKE_ALL` pays `msgSender()`); then they quote again with what actually
    arrived and the unchanged `direct` path deposits it. So:

      * this quote is NEVER a deposit — `POST /v1/deposits` refuses it and `/arm` has nothing to
        arm. A swap transaction registered as a deposit would be a hash that no pipe lock will
        ever match, sitting in the ledger as an open claim;
      * `swap_tx` is issued whether or not ingress is armed, exactly like mode "swap": it moves
        the user's money inside the user's own wallet, and the arming flags are about OURS. What
        `armed` still says is whether step 2 can be armed, and the note says it plainly;
      * `min_out_units` is the ONLY protection on the whole path, so it is the Quoter's answer
        less `PGAS_UNISWAP_SLIPPAGE_BPS` and it is never zero (`uniswap.swap_actions` refuses),
        and `next.amount` is that same floor — the client re-quotes on what ARRIVED, not on it.

    Three chain reads, in this order: the pool's liveness, the price, and the user's two
    allowances. The liveness read comes FIRST because the Quoter's own refusal for a pool that
    was never initialised is "no depth for this size", which blames the amount for something the
    amount cannot fix."""
    check_route_bounds(route, amount)
    rpc = workers.get_rpc()
    try:
        sqrt_price, liquidity = await uniswap.pool_state(rpc, route.inner)
    except uniswap.QuoteUnreadable as e:
        raise refuse(
            503, f"could not read the pool ({e}) — try again", token=route.token_in
        ) from e
    except uniswap.RouteError as e:  # an address that is not configured
        raise refuse(503, f"the Uniswap route is misconfigured: {e}", token=route.token_in) from e
    if problem := uniswap.pool_problem(route, sqrt_price, liquidity):
        raise refuse(400, problem, token=route.token_in, amount=amount)

    try:
        out_units = await uniswap.quote_out(rpc, route, amount)
    except uniswap.QuoteUnreadable as e:
        # An unreadable query is not evidence of anything — least of all of a price.
        raise refuse(
            503, f"could not read a price from the pool ({e}) — try again", token=route.token_in
        ) from e
    except uniswap.RouteError as e:
        raise refuse(400, str(e), token=route.token_in, amount=amount) from e

    slippage = max(0, min(BPS - 1, int(settings.uniswap_slippage_bps)))
    min_out = out_units * (BPS - slippage) // BPS
    deadline = uniswap.swap_deadline()
    try:
        swap_data = uniswap.swap_calldata(route, amount, min_out, deadline)
    except uniswap.RouteError as e:
        raise refuse(400, str(e), token=route.token_in, amount=amount) from e

    notes: list[str] = []
    # The floor is checked HERE, before the user pays gas for a swap whose output could not then
    # be deposited — and checked AGAIN on the second quote, against what actually arrived.
    if n := await check_min_deposit(asset, out_units):
        notes.append(n)
    # informational only: the real split happens on the second quote, on the amount that landed
    value, relayer_fee = _split(out_units, asset)

    allowances = uniswap.Allowances(0, 0, 0)
    if not route.native_in:
        allowances = await uniswap.read_allowances(rpc, route.token_in, user)
        if allowances.unreadable:
            notes.append(
                "could not read " + " and ".join(allowances.unreadable) + " — the approval "
                "step is offered anyway; your wallet will show it as already granted if it is"
            )
    approvals = uniswap.approvals_for(route, amount, deadline, allowances)

    src = {
        "chain_id": settings.eth_chain_id,
        "token": route.token_in,
        "symbol": route.symbol,
        "decimals": route.decimals,
        "amount": str(amount),
    }
    estimate: dict[str, Any] = {
        "src": src,
        "out_units": str(out_units),
        "min_out_units": str(min_out),
        "out_groth": to_groth(value, asset),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee),
        "usd": await _usd(asset, out_units),
        "eta_s": UNISWAP_SWAP_ETA_S + settings.lock_confirmations * 12 + 120,
    }
    if (impact := uniswap.price_impact_bps(route, amount, out_units, sqrt_price)) is not None:
        estimate["price_impact_bps"] = impact
    swap_tx = {
        "chain_id": settings.eth_chain_id,
        "to": to_checksum_address(settings.uniswap_universal_router),
        "data": swap_data,
        # native input is paid as msg.value (DeltaResolver._settle calls settle{value: amount});
        # an ERC-20 is pulled from the user through Permit2, so nothing is attached
        "value": str(amount) if route.native_in else "0",
    }
    nxt = {
        "src_chain_id": settings.eth_chain_id,
        "src_token": asset.token,
        "amount": str(min_out),
    }
    route_public = route.as_json()
    armed = settings.ingress_ready_for(asset.key)
    notes.append(
        f"two steps: send this swap from your own wallet, then quote {asset.symbol} on Ethereum "
        f"with the amount that actually arrived and register THAT transaction as the deposit "
        f"(the minimum-deposit floor is checked again on that second quote)"
    )
    if not armed:
        notes.append(f"the deposit step is not available yet — {_why_unarmed(asset)}")
    elif workers.paused():
        notes.append(f"the deposit step is paused right now — {workers.PAUSED_REASON}")
    doc_extra: dict[str, Any] = {
        "step": "swap",
        "route": route_public,
        "pool_id": route.inner.pool_id,
        "pool_key": route.inner.as_json(),
        "quoter_out_units": str(out_units),
        "min_out_units": str(min_out),
        "swap_deadline": deadline,
        "swap_calldata": swap_data,
        "swap_tx": swap_tx,
        "approvals": approvals,
        "next": nxt,
    }
    resp_extra: dict[str, Any] = {
        "step": "swap",
        "route": route_public,
        "swap_tx": swap_tx,
        "approvals": approvals,
        "next": nxt,
    }
    return await _store(
        acct,
        user,
        asset,
        uniswap.MODE,
        src,
        out_units,
        value,
        relayer_fee,
        "0x" + secrets.token_hex(5),  # kept for uniformity; nothing tags a two-step swap
        estimate,
        armed,
        notes,
        doc_extra,
        resp_extra,
    )


# ------------------------------- mode "uniswap", the hook shape (PGAS_UNISWAP_HOOK_ENABLED=1)


async def uniswap_hook_quote(
    acct: dict[str, Any],
    asset: Asset,
    route: uniswap.Route,
    amount: int,
    user: str,
) -> dict[str, Any]:
    """One transaction: the user swaps on our gateway pool and the hook pipes the output to Beam.

    ⚠️ REFERENCE PATH, OFF BY DEFAULT AND OFF ON THE BOX (`PGAS_UNISWAP_HOOK_ENABLED=0`): the
    hook and the router it needs are reviewed but NOT DEPLOYED (admin, 2026-09-10: "don't deploy,
    make it 2 clicks"). What ships is `uniswap_two_step_quote` above. Kept, with its tests,
    because deleting a reviewed path costs more than carrying it behind a flag.

    Built AT ONCE — there is no /arm step, because nothing upstream is ordered: the whole quote
    is one `eth_call` on the Quoter plus arithmetic we can redo any time. What the user signs is
    `PgasRouter.deposit(...)`, and the only slippage bound that works on this path is `minOut`
    inside `hookData` (through any router the hook takes 100 % of the output, so a router-level
    `amountOutMinimum` would always trip).

    `deposit_ref` is a HINT for attribution, never an authorisation: what binds the deposit to
    this account is POST /v1/deposits resolving the transaction — `from` == this wallet, `to` ==
    our router, this ref in the calldata, and, once mined, our hook's log AND the pipe's.
    """
    check_route_bounds(route, amount)
    try:
        out_units = await uniswap.quote_out(workers.get_rpc(), route, amount)
    except uniswap.QuoteUnreadable as e:
        # An unreadable query is not evidence of anything — least of all of a price.
        raise refuse(503, f"could not read a price from the pool ({e}) — try again", token=route.token_in) from e
    except uniswap.RouteError as e:
        raise refuse(400, str(e), token=route.token_in, amount=amount) from e

    slippage = max(0, min(BPS - 1, int(settings.uniswap_slippage_bps)))
    min_out = out_units * (BPS - slippage) // BPS
    # The relayer's quoted e2b tariff, raised to the route's own on-chain floor when it has one:
    # the hook refuses a quote below `route.minRelayerFee`, and handing the user calldata that
    # must revert is not a quote.
    relayer_fee = max(ethpipe.min_relayer_fee_units(asset), route.min_relayer_fee_units)
    # measured against the WORST output the hook will accept, because that is the one the bound
    # has to hold at — the same two checks the hook itself makes, one implementation
    if problem := uniswap.relayer_fee_problem(
        min_out, relayer_fee, route.min_relayer_fee_units, route.max_relayer_fee_bps
    ):
        raise refuse(
            400,
            f"{problem} — this deposit is too small for the route's relayer-fee bounds",
            token=route.token_in,
            amount=amount,
        )
    # what the user is shown, at the quoted output …
    value, relayer_fee_out = _split_at(out_units, relayer_fee, asset, "at the quoted output")
    # … and the same split at the WORST output the hook will accept: if that cannot mint a
    # groth the deposit is too small to be safe, whatever the mid-price says right now.
    _split_at(min_out, relayer_fee, asset, "at the minimum output")

    notes: list[str] = []
    if n := await check_min_deposit(asset, out_units):
        notes.append(n)
    deposit_ref = "0x" + secrets.token_hex(32)
    src = {
        "chain_id": settings.eth_chain_id,
        "token": route.token_in,
        "symbol": route.symbol,
        "decimals": route.decimals,
        "amount": str(amount),
    }
    estimate = {
        "src": src,
        "out_units": str(out_units),
        "min_out_units": str(min_out),
        "out_groth": to_groth(value, asset),
        "value_units": str(value),
        "relayer_fee_units": str(relayer_fee_out),
        "usd": await _usd(asset, out_units),
        "eta_s": settings.lock_confirmations * 12 + 120,
    }
    route_public = route.as_json()
    doc_extra: dict[str, Any] = {
        "deposit_ref": deposit_ref,
        "route": route_public,
        "min_out_units": str(min_out),
        "relayer_fee_quote_units": str(relayer_fee),
    }
    resp_extra: dict[str, Any] = {"deposit_ref": deposit_ref, "route": route_public}
    armed = settings.ingress_ready_for(asset.key)
    if not armed:
        notes.append(f"estimate only — {_why_unarmed(asset)}; no transaction is issued")
    else:
        calldata = uniswap.deposit_calldata(route, amount, deposit_ref, min_out, relayer_fee)
        tx = {
            "chain_id": settings.eth_chain_id,
            "to": to_checksum_address(settings.uniswap_router),
            "data": calldata,
            # native input: the router forwards it and refunds anything unused
            "value": str(amount) if route.native_in else "0",
        }
        approval = (
            None
            if route.native_in
            else {
                "chain_id": settings.eth_chain_id,
                "token": route.token_in,
                "spender": to_checksum_address(settings.uniswap_router),
                "amount": str(amount),
            }
        )
        doc_extra |= {
            "hook_calldata": calldata,
            # ⛔ THE HOOK ROUTE CANNOT CARRY A PER-DEPOSIT RECEIVER KEY AND MUST NOT PRETEND TO.
            # `beamPubkey` is pinned on the route on-chain, write-once, and is deliberately
            # absent from `hookData` (`uniswap.hook_data`; contracts assert
            # `test_beamPubkeyIsPinnedNotCallerSupplied`) — a caller-supplied receiver is a
            # theft-and-loss vector. So this path stays on the pipe's legacy key whatever
            # PGAS_RECEIVER_KEY_PER_DEPOSIT says, and the row records the key the HOOK will
            # actually use rather than one we would like it to.
            "pubkey": settings.pubkey_for(asset.key),
            "tx": tx,
            "approval": approval,
            "armed_at": time.time(),
        }
        resp_extra["tx"] = tx
        if approval:
            resp_extra["approval"] = approval
    return await _store(
        acct,
        user,
        asset,
        uniswap.MODE,
        src,
        out_units,
        value,
        relayer_fee_out,
        "0x" + secrets.token_hex(5),  # kept for uniformity; nothing tags a uniswap deposit
        estimate,
        armed,
        notes,
        doc_extra,
        resp_extra,
    )


def pick_uniswap_route(src_token: str, asset: Asset, explicit: bool) -> uniswap.Route | None:
    """The registered route for this pair, or None.

    A registry that cannot be PARSED is not "no such pair": asked for by name it is a 503 the
    operator can act on, and on `auto` it is logged at ERROR and the request falls through to a
    route that still works — the user's deposit must not wait for a fixed env file."""
    try:
        return uniswap.route_for(src_token, asset.key)
    except uniswap.RouteError as e:
        if explicit:
            raise refuse(503, f"the Uniswap route is misconfigured: {e}", token=src_token) from e
        log.error("PGAS_UNISWAP_POOLS is unusable, falling back to another route: %s", e)
        return None


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
    wanted = (body.route or "auto").strip().lower()
    if wanted not in ROUTE_CHOICES:
        raise refuse(
            400, f"unknown route {wanted!r} — choose one of {', '.join(ROUTE_CHOICES)}",
            account_id=acct["account_id"],
        )
    is_direct_token = src_token.lower() == asset.token.lower()

    # ── which ways in are open. Both flags sit UNDER `ingress_armed` and the kill switch below:
    # they decide which routes the API will SERVE, never whether money may move.
    if (cross_chain or wanted == xchain.MODE) and not settings.ingress_xchain:
        raise refuse(409, XCHAIN_PAUSED, account_id=acct["account_id"], src_chain_id=body.src_chain_id)
    if wanted == xchain.MODE and not cross_chain:
        raise refuse(
            400,
            "the cross-chain route needs a source chain other than Ethereum — deposit from "
            "Ethereum with route \"uniswap\" or \"direct\"",
            account_id=acct["account_id"],
        )
    if wanted == uniswap.MODE:
        if not settings.ingress_uniswap:
            raise refuse(409, UNISWAP_UNAVAILABLE, account_id=acct["account_id"])
        if cross_chain:
            raise refuse(
                400, "the Uniswap route is Ethereum-only — quote from Ethereum",
                account_id=acct["account_id"], src_chain_id=body.src_chain_id,
            )
    if wanted == "direct" and not is_direct_token:
        raise refuse(
            400,
            f"route \"direct\" needs the {asset.key} token itself as the source",
            account_id=acct["account_id"],
        )

    # ⛔ ONE resolver for "does this request ask for the Uniswap route" (T31b item 1). This used to
    # be `wanted in ("auto", uniswap.MODE)` written out here, which made `auto` mean "Uniswap
    # whenever the pair happens to be registered" and left PGAS_INGRESS_DEFAULT_ROUTE — the value
    # /v1/health, /v1/dex/assets and /v1/account all publish, and the one the client preselects its
    # route control from — with no effect on any quote. `uniswap.wants_uniswap` is the function
    # those three endpoints resolve, so the button the user sees and the route they get are now one
    # decision (law 9: two implementations of one fact will disagree).
    ask_uniswap = uniswap.wants_uniswap(wanted)
    uni: uniswap.Route | None = None
    if not cross_chain and ask_uniswap:
        uni = pick_uniswap_route(src_token, asset, explicit=wanted == uniswap.MODE)
    if wanted == uniswap.MODE and uni is None:
        raise refuse(
            400,
            f"{src_token} is not a registered source for {asset.key} on the Uniswap route",
            account_id=acct["account_id"], token=src_token,
        )

    # the kill switch reaches the request path: while it is set no ROUTE may hand back a
    # transaction that locks money in OUR pipe. A `direct` quote carries one, and so does a
    # `uniswap` one WHEN THE HOOK SHAPE IS ON (the hook calls the pipe inside the same
    # transaction); an `xchain` quote no longer does (POST /v1/quote/{id}/arm is where that is
    # refused), and an estimate costs nothing and still answers.
    #
    # ⛔ THE TWO-STEP `uniswap` QUOTE IS NOT A PIPE TRANSACTION. Its `swap_tx` moves the user's
    # own money inside the user's own wallet and never touches a pipe — refusing it under the
    # kill switch would stop nothing of ours from moving and would only strand a user mid-route.
    # What the kill switch DOES still stop is step 2 (`POST /v1/deposits` refuses outright), and
    # this quote says so in its note rather than pretending the deposit is available.
    issues_a_pipe_tx = not cross_chain and (
        is_direct_token or (uni is not None and uniswap.hook_enabled())
    )
    if issues_a_pipe_tx and settings.ingress_ready_for(asset.key) and workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    if cross_chain:
        return await xchain_quote(body, acct, asset, src_token, amount, user)
    if is_direct_token:
        return await direct_quote(acct, asset, amount, user)
    if uni is not None and ask_uniswap:
        return await uniswap_quote(acct, asset, uni, amount, user)
    return await swap_quote(acct, asset, src_token, amount, user)


@router.post("/{quote_id}/arm")
async def arm(quote_id: str, acct=auth.Account):
    """Build — or hand back — the transaction the user signs for THIS quote.

    Called when the user has decided to deposit, never on the refresh loop. It is the step that
    can put money into OUR pipe, so it is the step the kill switch and the arming flags gate;
    the estimate before it is free and answers either way. Nothing here is signed or broadcast:
    the answer is calldata the user's own wallet decides about.

    409 tells the client what to do next: `expired` → quote again, `not armed` → nothing can be
    issued for this asset yet."""
    q = await db().quotes.find_one({"_id": quote_id})
    if not q or q.get("account_id") != acct["account_id"]:
        raise HTTPException(404, "unknown quote")
    now = time.time()
    if now > float(q.get("expires_at") or 0):
        raise HTTPException(409, "quote expired — request a new one")
    mode = xchain.norm_mode(q.get("mode"))
    # A `swap` quote — and a two-step `uniswap` quote, which is the same handshake with Uniswap
    # as the venue — prices a transaction into the USER's own wallet. There is nothing of ours
    # to arm, and the honest next step is the second quote, not this route.
    if mode == "swap" or (mode == uniswap.MODE and q.get("step") == "swap"):
        raise HTTPException(
            400, "a swap is not a deposit — quote again with the target token after it lands"
        )
    try:
        asset = get_asset(q["asset"])
    except KeyError as e:  # pragma: no cover — assets.py is the closed list a quote was made on
        raise HTTPException(400, str(e.args[0])) from e
    if not settings.ingress_ready_for(asset.key):
        raise HTTPException(409, f"this quote cannot be armed — {_why_unarmed(asset)}")
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    if mode == "direct":
        return await arm_direct(q, asset)  # pure arithmetic: idempotent by construction
    if mode == uniswap.MODE:
        # A uniswap quote is built at once — /arm exists for it only so a client that calls it
        # anyway gets the SAME transaction back. Nothing upstream is ordered, so there is
        # nothing to rebuild and nothing to pay for twice.
        if not q.get("tx"):
            raise refuse(
                409,
                "that quote was an estimate only — quote again once ingress is armed",
                quote_id=quote_id,
            )
        return stored_arm(q, asset)
    if not settings.ingress_xchain:
        raise refuse(409, XCHAIN_PAUSED, quote_id=quote_id, account_id=acct["account_id"])
    if q.get("tx") and q.get("order_id") and now - float(q.get("armed_at") or 0) < ARM_FRESH_S:
        return stored_arm(q, asset)  # the SAME order, not a second one
    # ⛔ A QUOTE WHOSE TRANSACTION IS ALREADY REGISTERED IS NOT RE-ARMABLE. Building a new order
    # rewrites this quote's amounts, and the registered deposit row and the scanner both read
    # them: the sequence (arm · register · wait for gas to move · arm again) is entirely
    # caller-controlled and made the row describe an order the user never signed. A registered
    # quote has done its job; another deposit needs another quote.
    if await db().deposits.find_one({"quote_id": quote_id}, {"_id": 1}):
        raise HTTPException(
            409,
            "this quote already has a registered deposit — request a new quote for another "
            "deposit; re-arming this one would re-price an order that is already on its way",
        )
    # the arm is CLAIMED before anybody calls the router, so a second concurrent call waits for
    # this order instead of placing one of its own
    claimed = await db().quotes.find_one_and_update(
        {
            "_id": quote_id,
            "$or": [
                {"arm_in_flight": {"$exists": False}},
                {"arm_in_flight": None},
                {"arm_in_flight": {"$lt": now - ARM_LOCK_S}},
            ],
        },
        {"$set": {"arm_in_flight": now}, "$inc": {"arm_tries": 1}},
    )
    if claimed is None:
        return await await_arm_in_flight(quote_id, asset)
    try:
        if int(claimed.get("arm_tries") or 0) + 1 > ARM_MAX_TRIES:
            raise HTTPException(
                429,
                "this quote has been armed too many times — request a new one",
                headers={"Retry-After": "5"},
            )
        return await arm_xchain(claimed, asset)
    finally:
        await db().quotes.update_one({"_id": quote_id}, {"$unset": {"arm_in_flight": ""}})
