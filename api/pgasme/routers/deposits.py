"""POST /v1/deposits — the user signed the transaction; register its hash against the armed
quote and let the workers follow it. An `xchain` quote registers the cross-chain order tx (order-ids →
status → the pipe's lock log → credit); a `direct` quote registers the pipe call itself, which
has no order id at all and is attributed by that hash in the receipt the scanner reads. A `swap`
quote is not a deposit: its transaction lands in the user's own wallet.
GET /v1/deposits/{id} — one deposit of the signed-in account.

THE HASH IS NOT A CLAIM. A transaction hash is public the moment it is broadcast, so
"whoever posts it first owns it" would let a stranger bind someone else's fill to their own
account (and lock the person who signed it out with a 409 forever). The hash is RESOLVED here
before a row exists:

  mode "xchain"  the router's own index must list THIS quote's order id among the transaction's
                 orders. A transaction whose orders are someone else's is refused outright; a
                 transaction the router has not indexed yet is accepted unverified and re-checked by
                 workers._step_submitted, which fails it on a mismatch and never adopts a
                 foreign id.
  mode "direct"  the chain is asked: `from` must be the wallet that asked for the quote, `to`
                 the asset's pipe, and the calldata this quote's own sendFunds. A pipe call
                 from another wallet is not registered at all — its lock lands in
                 unattributed_locks and pages the operator, which is the manual path.

An answer nobody could read is never treated as a verdict: an unreachable RPC / router gives a
retryable refusal or an unverified row, never an accepted claim and never a rejection.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, ethpipe, tg, uniswap, workers, xchain
from ..assets import get_asset
from ..config import settings
from ..db import db
from .account import public_deposit

log = logging.getLogger("pgasme.deposits")

router = APIRouter(prefix="/v1/deposits", tags=["deposits"])

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ORDER_IDS_TIMEOUT_S = 8.0  # this sits in the request path: a slow router must not hang the user
NOT_VISIBLE_YET = (
    "that transaction is not visible on Ethereum yet — wait for it to be broadcast and try again"
)


async def reject(q: dict[str, Any], why: str, detail: str) -> HTTPException:
    """A registration refused BEFORE a row exists still writes one — the event log is where a
    hash-attribution attempt (or our own broken understanding of a receipt) becomes visible."""
    log.warning("deposit registration refused: %s (quote %s)", why, q.get("_id"))
    await tg.queue(
        "deposit_mismatch",
        f"REJECTED at registration: {why}",
        quote_id=q.get("_id"),
    )
    return HTTPException(400, detail)


class DepositIn(BaseModel):
    quote_id: str = Field(min_length=8, max_length=64)
    src_tx_hash: str = Field(min_length=66, max_length=66)


async def _xchain_order_ids(tx_hash: str) -> list[str] | None:
    """The router's order ids for this transaction, or None when the router could not answer (not indexed
    yet, or unreachable). None is "I do not know", never "there are none"."""
    try:
        return await xchain.order_ids_by_tx(tx_hash, timeout=ORDER_IDS_TIMEOUT_S)
    except xchain.XchainError:
        return None


def armed_orders(q: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """order id (lowercased) → the amounts THAT order was built at.

    /arm overwrites the quote's own `value_units` / `value_groth` on every arm, so the quote's
    tip describes only the LAST order it built. A deposit is worth what the order the user
    actually signed locks, so the row copies the snapshot of the id that matched — never the
    tip. (Law 9: two implementations of one fact will disagree, and one of them reaches money.)"""
    out: dict[str, dict[str, Any]] = {}
    for snap in q.get("orders_armed") or []:
        oid = str((snap or {}).get("order_id") or "")
        if oid:
            out[oid.lower()] = snap
    return out


def armed_order_ids(q: dict[str, Any]) -> list[str]:
    """Every order id this quote has been armed with, newest first.

    /arm can rebuild the order once the router's transaction validity window has passed, and an
    UNSIGNED cross-chain order exists only as a transaction we handed over — so an earlier one is not
    cancelled by the later one and is still this quote's own order. Registering the transaction
    the user actually signed must therefore accept any of them (they were all built for THIS
    account, from THIS quote), and the row records the one that matched."""
    ids = [str(q.get("order_id") or "")] + [str(i) for i in (q.get("order_ids_armed") or [])]
    seen: list[str] = []
    for i in ids:
        if i and i.lower() not in [s.lower() for s in seen]:
            seen.append(i)
    return seen


async def resolve_xchain(q: dict[str, Any], tx_hash: str) -> tuple[bool, str | None]:
    """(verified, the order id that matched). `False` when the router cannot say yet.
    Raises 400 when the transaction carries orders and none of them is this quote's."""
    ids = await _xchain_order_ids(tx_hash)
    if not ids:
        return False, None  # not indexed yet — workers._step_submitted re-checks a mismatch
    have = [str(i).lower() for i in ids]
    for want in armed_order_ids(q):
        if want.lower() in have:
            return True, want
    await tg.queue(
        "deposit_mismatch",
        "REJECTED at registration: a transaction was offered for a quote whose cross-chain order "
        "it does not carry (hash attribution attempt)",
        quote_id=q["_id"],
    )
    raise HTTPException(
        400,
        "that transaction does not carry this quote's cross-chain order — register the transaction "
        "you signed for this quote",
    )


async def resolve_direct(q: dict[str, Any], tx_hash: str) -> tuple[bool, str | None]:
    """The pipe call must be THIS user's: same sender, our pipe, this quote's calldata."""
    asset = get_asset(q["asset"])
    rpc = workers.get_rpc()
    try:
        tx = await rpc.transaction(tx_hash)
    except ethpipe.RpcError as e:
        raise HTTPException(
            503, f"could not read that transaction from any Ethereum endpoint ({e}) — try again"
        ) from e
    if not tx:
        raise HTTPException(409, NOT_VISIBLE_YET)
    sender = (tx.get("from") or "").lower()
    to = (tx.get("to") or "").lower()
    if sender != (q.get("address") or "").lower():
        raise HTTPException(
            400,
            "that transaction was not sent from the wallet this quote was issued to — the pipe "
            "call must come from your signed-in wallet",
        )
    if to != asset.pipe.lower():
        raise HTTPException(
            400, f"that transaction is not a call to the {asset.key} pipe of this quote"
        )
    data = (tx.get("input") or tx.get("data") or "").lower()
    want = str(q.get("hook_calldata") or "").lower()
    if want and data == want:
        return True, None
    try:
        call = ethpipe.decode_send_funds(data)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, "that transaction is not a sendFunds call on the pipe") from e
    if call["pubkey"].lower() != str(q.get("pubkey") or "").lower():
        raise HTTPException(400, "that pipe call locks to a different Beam pubkey than this quote")
    if str(call["value"]) != str(q["value_units"]):
        raise HTTPException(
            400,
            "that pipe call locks a different amount than this quote — quote again for the "
            "amount you actually sent, then register it",
        )
    return True, None


async def resolve_uniswap(q: dict[str, Any], tx_hash: str) -> tuple[bool, str | None]:
    """The gateway swap must be THIS user's, on OUR router, carrying THIS quote's reference —
    and, once it is mined, it must have reached OUR pipe.

    Four things, and all of them (§3 of the design, §IDENTITY-BEATS-BALANCE):

      1. `from` is the wallet the quote was issued to. A hash is public the moment it is
         broadcast; the sender is what makes it the user's.
      2. `to` is our PgasRouter — not the pipe: on this path the pipe's caller is the hook.
      3. the calldata is a `deposit(...)` call whose hookData carries EXACTLY this quote's
         reference (decoded, never matched as a substring).
      4. once mined: our hook's own `PgasDeposit(ref, payer, …)` log AND the target pipe's
         `NewLocalMessage` for OUR pubkey, in the SAME receipt.

    A transaction we cannot READ is never a verdict: an unreachable endpoint is 503 and an
    unbroadcast hash is 409, so the user retries instead of being told "no".
    """
    asset = get_asset(q["asset"])
    ref = str(q.get("deposit_ref") or "")
    if not ref:
        raise HTTPException(409, "this quote carries no deposit reference — request a new one")
    rpc = workers.get_rpc()
    try:
        tx = await rpc.transaction(tx_hash)
    except ethpipe.RpcError as e:
        raise HTTPException(
            503, f"could not read that transaction from any Ethereum endpoint ({e}) — try again"
        ) from e
    if not tx:
        raise HTTPException(409, NOT_VISIBLE_YET)

    sender = (tx.get("from") or "").lower()
    to = (tx.get("to") or "").lower()
    router_address = (settings.uniswap_router or "").lower()
    if sender != (q.get("address") or "").lower():
        raise await reject(
            q,
            "a Uniswap deposit was offered from a wallet the quote was not issued to "
            "(hash attribution attempt)",
            "that transaction was not sent from the wallet this quote was issued to",
        )
    if not router_address or to != router_address:
        raise await reject(
            q,
            "a Uniswap deposit was offered whose `to` is not our router",
            "that transaction is not a call to the Pgas router this quote was built for",
        )
    data = tx.get("input") or tx.get("data") or ""
    if not uniswap.carries_ref(data, ref):
        raise await reject(
            q,
            "a Uniswap deposit was offered whose calldata does not carry this quote's reference",
            "that transaction does not carry this quote's deposit reference — register the "
            "transaction you signed for this quote",
        )

    try:
        receipt = await rpc.receipt(tx_hash)
    except ethpipe.RpcError as e:
        raise HTTPException(
            503, f"could not read that receipt from any Ethereum endpoint ({e}) — try again"
        ) from e
    if not receipt:
        # Not mined yet. The transaction-level proof above already binds it to this account, and
        # the scanner completes the story from the chain — broadcast is not done, but it IS
        # enough to open the row and follow it.
        return True, None
    if int(str(receipt.get("status") or "0x1"), 16) == 0:
        raise await reject(
            q,
            "a Uniswap deposit was offered whose transaction REVERTED",
            "that transaction reverted — nothing reached the bridge; quote again and retry",
        )
    hook_log = uniswap.find_deposit_ref(receipt, ref)
    if not hook_log:
        raise await reject(
            q,
            "a mined Uniswap deposit carries no PgasDeposit log from our hook for this reference",
            "that transaction did not reach the Pgas hook with this quote's reference",
        )
    if hook_log["payer"].lower() != (q.get("address") or "").lower():
        raise await reject(
            q,
            "a mined Uniswap deposit names a different payer than the quote's wallet",
            "that deposit was paid by a different wallet than this quote was issued to",
        )
    pubkey = str(q.get("pubkey") or settings.pubkey_for(asset.key) or "")
    if not pubkey:
        raise HTTPException(409, f"no Beam pipe pubkey is configured for {asset.key}")
    if not ethpipe.find_lock_in_receipt(receipt, pubkey, asset.pipe):
        raise await reject(
            q,
            f"a mined Uniswap deposit has no {asset.key} pipe lock to our pubkey in its receipt",
            f"that transaction did not lock anything in the {asset.key} pipe for us",
        )
    return True, None


@router.post("")
async def create(body: DepositIn, acct=auth.Account):
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    if not HASH_RE.match(body.src_tx_hash):
        raise HTTPException(400, "src_tx_hash must be a 0x-prefixed 32-byte hex hash")
    tx_hash = body.src_tx_hash.lower()
    q = await db().quotes.find_one({"_id": body.quote_id})
    if not q or q.get("account_id") != acct["account_id"]:
        raise HTTPException(404, "unknown quote")
    now = time.time()
    if now > float(q.get("expires_at") or 0):
        raise HTTPException(409, "quote expired — request a new one")
    mode = xchain.norm_mode(q.get("mode"))
    if mode == "swap":
        raise HTTPException(
            400, "a swap is not a deposit — quote again with the target token after it lands"
        )
    if not q.get("armed"):
        raise HTTPException(
            409,
            "that quote was an estimate only (ingress not armed) — no order transaction was issued for it",
        )
    if not q.get("tx"):
        # 2026-09-09: an `xchain` quote is an ESTIMATE until POST /v1/quote/{id}/arm builds the
        # hook-carrying order. A hash offered against an unarmed quote belongs to some other
        # order (or to nothing), and there is no order id to resolve it against — refuse.
        # A `uniswap` quote is built at once, so having no transaction means it was priced while
        # ingress was unarmed; there is nothing to arm and nothing to register.
        raise HTTPException(
            409,
            "that quote was priced without a transaction — quote again once ingress is armed"
            if mode == uniswap.MODE
            else "that quote has no transaction yet — call POST /v1/quote/{quote_id}/arm first "
            "and register the transaction it hands back",
        )
    existing = await db().deposits.find_one({"src_tx_hash": tx_hash})
    if existing:
        if existing.get("account_id") != acct["account_id"]:
            raise HTTPException(409, "that transaction is already registered")
        return {"deposit_id": existing["_id"], "status": existing["status"]}
    if mode == uniswap.MODE and (ref := str(q.get("deposit_ref") or "")):
        # A deposit reference is single-use: it is what the scanner matches a pipe lock by, and
        # two rows sharing one would make that match ambiguous exactly when money has landed.
        clash = await db().deposits.find_one({"deposit_ref": ref}, {"_id": 1, "account_id": 1})
        if clash:
            if clash.get("account_id") == acct["account_id"]:
                row = await db().deposits.find_one({"_id": clash["_id"]})
                return {"deposit_id": row["_id"], "status": row["status"]}
            raise HTTPException(409, "that deposit reference is already registered")
    # resolve the hash BEFORE a row exists: the quote must own this transaction
    resolver = {
        "direct": resolve_direct,
        uniswap.MODE: resolve_uniswap,
    }.get(mode, resolve_xchain)
    verified, matched_order = await resolver(q, tx_hash)
    deposit_id = secrets.token_hex(12)
    # the amounts of the order that MATCHED — falling back to the quote's tip only when the router
    # could not say yet which order this transaction carries (workers._step_submitted adopts the
    # right one, with its numbers, as soon as the index can answer).
    snapshots = armed_orders(q)
    priced = snapshots.get(str(matched_order or "").lower()) or q
    candidates = armed_order_ids(q)
    doc = {
        "_id": deposit_id,
        "account_id": acct["account_id"],
        "address": q.get("address"),  # the sender the scanner checks a direct receipt against
        "asset": q["asset"],
        "mode": mode,
        "status": "submitted",
        "verified": verified,
        "src": {
            "chain_id": q["src"]["chain_id"],
            "token": q["src"]["token"],
            "amount": q["src"]["amount"],
        },
        "quote_id": q["_id"],
        "src_tx_hash": tx_hash,
        # the QUOTE's own id — never the transaction's first id. When the hash resolved against
        # an EARLIER order of this same quote (it was re-armed), that is the one the row carries,
        # because it is the one the worker's re-check and the scanner will see.
        "order_id": matched_order or q.get("order_id"),
        # EVERY order this quote was armed with, frozen here: the worker's re-check matches the
        # transaction's orders against THIS LIST, not against the single id above. Matching the
        # one id failed a legitimate deposit as a hijack whenever the user signed anything but
        # the latest order — and a failed row can never claim its own lock afterwards.
        "order_ids_armed": candidates,
        "orders_armed": [snapshots[c.lower()] for c in candidates if c.lower() in snapshots],
        "eth": {
            "value_units": priced["value_units"],
            "relayer_fee_units": priced["relayer_fee_units"],
        },
        "value_groth": int(priced["value_groth"]),
        "metadata": q.get("metadata"),
        "pubkey": q.get("pubkey"),
        "created_at": now,
        "updated_at": now,
    }
    if mode == uniswap.MODE:
        # What the scanner needs to recognise this deposit's lock and to sanity-check what it is
        # worth: the reference the hook echoes, the route, and the two numbers the hook was
        # given (`min_out_units` is the floor it enforces; `out_units` is what we quoted).
        doc |= {
            "deposit_ref": q.get("deposit_ref"),
            "route": q.get("route"),
            "out_units": q.get("out_units"),
            "min_out_units": q.get("min_out_units"),
            "relayer_fee_quote_units": q.get("relayer_fee_quote_units"),
        }
    try:
        await db().deposits.insert_one(doc)
    except Exception as e:  # noqa: BLE001 — the unique index is the one that decides
        again = await db().deposits.find_one({"src_tx_hash": tx_hash})
        if again and again.get("account_id") == acct["account_id"]:
            return {"deposit_id": again["_id"], "status": again["status"]}
        raise HTTPException(409, "that transaction is already registered") from e
    await tg.queue(
        "deposit_submitted",
        f"Deposit submitted ({mode}{'' if verified else ', unverified'}): {q['asset']} "
        f"≈ {tg.fmt_groth(int(priced['value_groth']))}",
        deposit_id=deposit_id,
    )
    return {"deposit_id": deposit_id, "status": "submitted"}


@router.get("/{deposit_id}")
async def get_one(deposit_id: str, acct=auth.Account):
    d = await db().deposits.find_one({"_id": deposit_id, "account_id": acct["account_id"]})
    if not d:
        raise HTTPException(404, "unknown deposit")
    return public_deposit(d)
