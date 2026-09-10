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
from typing import Any, NamedTuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from .. import auth, ethpipe, receiver_keys, scanner, tg, uniswap, workers, xchain
from ..assets import get_asset
from ..config import settings
from ..db import db
from .account import public_deposit

log = logging.getLogger("pgasme.deposits")

router = APIRouter(prefix="/v1/deposits", tags=["deposits"])

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ORDER_IDS_TIMEOUT_S = 8.0  # this sits in the request path: a slow router must not hang the user
# What the client is told when the row was opened before any endpoint could see the transaction.
# ⛔ THIS USED TO BE A 409 REFUSAL and it cost two real deposits on 2026-09-10: the first endpoint
# of the prod pool (a private-orderflow relay) does not expose pending transactions, so a hash
# the user had just signed read as "does not exist", the client offered only a manual retry, and
# both transactions mined into the pipe with no row to attribute them to. The sentence itself
# lives in ethpipe, beside the reader that decides it, because the watcher clears it again.
UNSEEN_NOTE = ethpipe.UNSEEN_NOTE
# …and what a row says when the price it was registered against had already lapsed. It is on the
# row rather than in a log because it is the evidence for why a lapsed quote produced a deposit
# at all: nothing about the price was honoured — the transaction proved itself.
EXPIRED_BUT_PROVEN = (
    "the quote had expired when this transaction was registered — an expired quote is a stale "
    "price, and this transaction proved itself: same wallet, our pipe, this quote's own calldata"
)


class Resolved(NamedTuple):
    """What resolving the hash established. `unseen` is not a failure and not a verdict: it is
    "no endpoint has this transaction YET", which the watcher keeps asking about."""

    verified: bool
    order_id: str | None = None
    unseen: str | None = None


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


async def resolve_xchain(q: dict[str, Any], tx_hash: str) -> Resolved:
    """(verified, the order id that matched). Unverified when the router cannot say yet.
    Raises 400 when the transaction carries orders and none of them is this quote's."""
    ids = await _xchain_order_ids(tx_hash)
    if not ids:
        return Resolved(False)  # not indexed yet — workers._step_submitted re-checks a mismatch
    have = [str(i).lower() for i in ids]
    for want in armed_order_ids(q):
        if want.lower() in have:
            return Resolved(True, want)
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


async def read_tx(tx_hash: str) -> dict[str, Any] | None:
    """The transaction, from ANY endpoint of the pool that has it — or None when every endpoint
    that could answer says it has not seen it.

    Two answers, two different things, and the difference is the whole fix:

      * at least one endpoint answered and none of them holds the transaction → None, and the
        caller opens an UNVERIFIED row. Not seeing a transaction is not proof it does not
        exist: a hash the user's wallet just handed them may be sitting in a mempool none of
        these nodes gossips (the prod pool's first entry is a private-orderflow relay that
        never exposes pending transactions at all — the 2026-09-10 incident).
      * NO endpoint answered at all → 503. An unreadable query is not evidence of anything, so
        it is neither a rejection nor an acceptance; the caller retries.
    """
    tx, answered, errors = await ethpipe.visible_tx(workers.get_rpc(), tx_hash)
    if tx is None and not answered:
        raise HTTPException(
            503,
            "could not read that transaction from any Ethereum endpoint "
            f"({'; '.join(errors) or 'no endpoint answered'}) — try again",
        )
    return tx


async def resolve_direct(q: dict[str, Any], tx_hash: str) -> Resolved:
    """The pipe call must be THIS user's: same sender, our pipe, this quote's calldata — and
    when no endpoint can see it yet, the row is opened unverified rather than refused.

    `scanner.direct_identity_reason` is the ONE implementation of "is this transaction this
    quote's": the same three facts decide it here, in the watcher's re-verification of an
    unverified row, and in the scanner's attribution of a lock nobody registered."""
    asset = get_asset(q["asset"])
    tx = await read_tx(tx_hash)
    if not tx:
        return Resolved(False, None, "no Ethereum endpoint has this transaction yet")
    why = scanner.direct_identity_reason(tx, q, asset)
    if why:
        raise await reject(q, f"a direct deposit was offered that is not this quote's — {why}", why)
    return Resolved(True)


async def resolve_uniswap(q: dict[str, Any], tx_hash: str) -> Resolved:
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

    A transaction we cannot READ is never a verdict: an endpoint pool that cannot answer at all
    is 503, and a hash NO endpoint has seen yet opens an unverified row (workers._reverify_chain
    keeps asking) instead of being told "no".
    """
    asset = get_asset(q["asset"])
    ref = str(q.get("deposit_ref") or "")
    if not ref:
        raise HTTPException(409, "this quote carries no deposit reference — request a new one")
    rpc = workers.get_rpc()
    tx = await read_tx(tx_hash)
    if not tx:
        return Resolved(False, None, "no Ethereum endpoint has this transaction yet")

    if why := scanner.uniswap_identity_reason(tx, q, ref):
        raise await reject(
            q, f"a Uniswap deposit was offered that is not this quote's — {why}", why
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
        return Resolved(True)
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
    return Resolved(True)


def already(row: dict[str, Any]) -> dict[str, Any]:
    """Re-registering a hash this account already registered answers with the row it has —
    same shape as a fresh registration, so a client that retried does not read a different
    contract than the one that succeeded."""
    out: dict[str, Any] = {
        "deposit_id": row["_id"],
        "status": row["status"],
        "verified": bool(row.get("verified")),
    }
    if row.get("note"):
        out["note"] = row["note"]
    return out


async def rate_guard(request: Request, acct: dict[str, Any]) -> None:
    """Two caps on registration, per account and per address.

    A session is free — any key can sign in — and since 2026-09-10 a hash NO endpoint can see
    is accepted as an unverified row rather than refused, so this route writes a permanent row
    for a hash nobody has proven anything about. Unbounded, that is unbounded storage and
    (before this build) an unbounded number of pages. The account cap is the real one; the
    per-address cap is what a fresh wallet per attempt runs into. nginx's own limits are an
    additive layer above, never a substitute (the same shape as routers/siwe.py).
    """
    await auth.rate_guard(
        "deposit_acct",
        acct["account_id"],
        settings.deposit_account_limit,
        settings.rate_window_s,
        "too many deposit registrations on this account — try again shortly",
    )
    await auth.rate_guard(
        "deposit_ip",
        auth.client_ip(request),
        settings.deposit_ip_limit,
        settings.rate_window_s,
        "too many deposit registrations from this address — try again shortly",
    )


async def replace_unseen(q: dict[str, Any], tx_hash: str, now: float) -> dict[str, Any] | None:
    """ONE unseen row per quote: the same row, moved to the latest hash, or None.

    A quote funds ONE deposit. A wallet that was re-signed (or a client that retried) hands back
    a second hash for the same quote, and before this the second one opened a SECOND row —
    permanent, uncreditable, and one more `src_tx_hash` claim on the pile. An unverified unseen
    row is not evidence of anything, so it is moved rather than duplicated; the hash it carried
    is kept on the row as `src_tx_hash_superseded`, because a row is corrected by appending to
    it, never by pretending the earlier attempt did not happen.

    A row that is VERIFIED, or that has moved past `submitted`, is never touched here: identity
    proved it, or money landed on it, and both are stronger than anything a new POST can say.
    """
    row = await db().deposits.find_one_and_update(
        {
            "quote_id": q["_id"],
            "status": "submitted",
            "verified": {"$ne": True},
            "unseen_since": {"$exists": True},
        },
        {
            "$set": {
                "src_tx_hash": tx_hash,
                "unseen_since": now,
                "updated_at": now,
                "note": (
                    f"{UNSEEN_NOTE} — the earlier hash registered for this quote was replaced "
                    "by the one you registered last"
                ),
            }
        },
        return_document=ReturnDocument.BEFORE,
    )
    if not row:
        return None
    await db().deposits.update_one(
        {"_id": row["_id"]}, {"$set": {"src_tx_hash_superseded": row.get("src_tx_hash")}}
    )
    log.info("deposit %s: unseen hash replaced for quote %s", row["_id"], q["_id"])
    return await db().deposits.find_one({"_id": row["_id"]})


@router.post("")
async def create(body: DepositIn, request: Request, acct=auth.Account):
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    await rate_guard(request, acct)
    if not HASH_RE.match(body.src_tx_hash):
        raise HTTPException(400, "src_tx_hash must be a 0x-prefixed 32-byte hex hash")
    tx_hash = body.src_tx_hash.lower()
    q = await db().quotes.find_one({"_id": body.quote_id})
    if not q or q.get("account_id") != acct["account_id"]:
        raise HTTPException(404, "unknown quote")
    now = time.time()
    # ⛔ AN EXPIRED QUOTE IS A STALE PRICE, NOT A CLOSED DOOR — and the refusal is DEFERRED until
    # identity has spoken (below, after `resolver`). A quote's expiry protects us from honouring
    # an old price for money that has not moved yet; it says nothing about money that HAS. On
    # 2026-09-10 a wallet sent two identical direct deposits against quotes issued an hour
    # earlier, and by the time anyone tried to register the second hash the quote had expired —
    # so the owner of lock msgId 138 had no way to register it at all and 0.0019999 ETH sat on
    # the operator's desk. A transaction that PROVES itself (same wallet, our pipe, this quote's
    # own sendFunds) is accepted on an expired quote; one that cannot be read still is not,
    # because otherwise a dead quote becomes an unbounded row factory.
    expired = now > float(q.get("expires_at") or 0)
    mode = xchain.norm_mode(q.get("mode"))
    # ⛔ A SWAP TRANSACTION IS NEVER A DEPOSIT. That is true of mode "swap" and equally true of a
    # two-step `uniswap` quote (`step: "swap"`), whose transaction pays the USER's own wallet
    # through Uniswap's router and never touches a pipe. Registering one would open a row on a
    # hash no pipe lock can ever match: an open claim on the ledger, and a `uniq_src_tx_hash`
    # taken from the deposit that follows it.
    if mode == "swap" or (mode == uniswap.MODE and q.get("step") == "swap"):
        raise HTTPException(
            400, "a swap is not a deposit — quote again with the target token after it lands"
        )
    if expired and not (q.get("armed") and q.get("tx")):
        # …but a quote we never issued a transaction for cannot be proven BY one, so an expired
        # one is simply dead and the deferral above has nothing to wait for. "Expired" is also
        # the actionable answer: "arm it first" would send the user back to a price we no longer
        # stand behind, and requesting a new quote fixes both faults at once.
        raise HTTPException(409, "quote expired — request a new one")
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
    # ⛔ A HASH IS A CLAIM, NOT A TITLE. It is public the moment it is broadcast, and since an
    # unseen hash opens a row instead of being refused, "whoever posted it first owns it" let a
    # stranger watching the mempool take `uniq_src_tx_hash` and answer 409 to the person who
    # actually signed the transaction until the TTL expired. Several accounts may therefore hold
    # an UNVERIFIED claim on one hash (scanner.claims_on); what is taken is a hash some row has
    # been PROVEN to own — identity, or a lock that has already landed on it.
    claims = await scanner.claims_on(tx_hash)
    mine = next((c for c in claims if c.get("account_id") == acct["account_id"]), None)
    if mine:
        return already(mine)
    if any(c.get("verified") or c.get("status") != "submitted" for c in claims):
        raise HTTPException(409, "that transaction is already registered")
    if mode == uniswap.MODE and (ref := str(q.get("deposit_ref") or "")):
        # A deposit reference is single-use: it is what the scanner matches a pipe lock by, and
        # two rows sharing one would make that match ambiguous exactly when money has landed.
        clash = await db().deposits.find_one({"deposit_ref": ref}, {"_id": 1, "account_id": 1})
        if clash:
            if clash.get("account_id") == acct["account_id"]:
                row = await db().deposits.find_one({"_id": clash["_id"]})
                return already(row)
            raise HTTPException(409, "that deposit reference is already registered")
    # resolve the hash BEFORE a row exists: the quote must own this transaction
    resolver = {
        "direct": resolve_direct,
        uniswap.MODE: resolve_uniswap,
    }.get(mode, resolve_xchain)
    res = await resolver(q, tx_hash)
    if expired and not res.verified:
        # nothing proved this transaction is this quote's, and the quote is no longer a price we
        # stand behind. (A transaction that is provably NOT this quote's has already been
        # refused, with its event row, inside the resolver — that is the better answer.)
        raise HTTPException(409, "quote expired — request a new one")
    verified, matched_order = res.verified, res.order_id
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
        # …and the receiver key that pubkey came from, when this quote holds its own. The CLAIM
        # signs with the row's index (payouts._treasury_claiming); a row that lost it would sign
        # with the legacy blob for a message delivered to an indexed key and could never claim
        # money that is already burned on Ethereum.
        **receiver_keys.carry(q),
        "created_at": now,
        "updated_at": now,
    }
    # ⛔ §A-QUOTE-IS-A-PRICE-STATEMENT-NOT-A-ONE-TIME-TICKET (scanner.reused_note is the ONE
    # writer of the sentence; this is the same law seen from registration). A SECOND distinct
    # hash offered for one quote is a second REAL deposit whenever it proves itself, so it opens
    # its own row rather than being refused — `uniq_src_tx_hash` is what stops two deposits from
    # becoming one credit, and it is per TRANSACTION, which is the thing money actually is.
    # Both facts are recorded because both are true and an operator will ask about both.
    sibling = await db().deposits.find(
        {"quote_id": q["_id"], "status": {"$ne": "failed"}, "src_tx_hash": {"$ne": tx_hash}},
        {"_id": 1},
    ).sort("created_at", 1).limit(1).to_list(1)
    reuse_note = scanner.reused_note(sibling[0]["_id"]) if sibling else ""
    if sibling:
        doc |= {"quote_reused": True, "quote_reused_of": sibling[0]["_id"]}
    if expired:
        doc["quote_expired_at_registration"] = float(q.get("expires_at") or 0)
        reuse_note = (reuse_note + " " if reuse_note else "") + EXPIRED_BUT_PROVEN
    if reuse_note:
        doc["note"] = reuse_note
    if res.unseen:
        # The row exists BECAUSE nobody could see the transaction yet. Say so on the row: the
        # watcher reads `unseen_since` to decide when waiting stops being normal, and the
        # reason is the evidence for why a `verified: false` row was accepted at all.
        doc |= {"unseen_since": now, "unseen_reason": res.unseen, "note": UNSEEN_NOTE}
        # …and this quote keeps ONE unseen row: a second unseen hash moves the row it already
        # has instead of opening another one that can never be credited.
        if moved := await replace_unseen(q, tx_hash, now):
            return already(moved)
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
        again = await db().deposits.find_one(
            {"src_tx_hash": tx_hash, "account_id": acct["account_id"], "status": {"$ne": "failed"}}
        )
        if again:
            return already(again)
        raise HTTPException(409, "that transaction is already registered") from e
    if verified:
        # identity has spoken for this hash: every other claim on it is refused, quietly (the
        # same helper `workers._reverify_chain` calls when it proves a row later on).
        await scanner.fail_claims(await scanner.claims_on(tx_hash, deposit_id))
    if res.unseen:
        # ⚠️ NO EVENT for a row nobody can see yet. An unproven claim is not a deposit, and a
        # page on one is a page on anything anyone types into the box — law 15. The event is
        # written when the row becomes verified (`workers._reverify_chain`), which is also when
        # there is something true to say. The registration itself is still recorded: the row IS
        # the record, and this line is the log the operator can grep (law 12).
        log.info("deposit %s registered unseen (quote %s)", deposit_id, q["_id"])
    else:
        await tg.queue(
            "deposit_submitted",
            f"Deposit submitted ({mode}{'' if verified else ', unverified'}): {q['asset']} "
            f"≈ {tg.fmt_groth(int(priced['value_groth']))}",
            deposit_id=deposit_id,
        )
    out: dict[str, Any] = {
        "deposit_id": deposit_id,
        "status": "submitted",
        "verified": verified,
    }
    if res.unseen:
        out["note"] = UNSEEN_NOTE
    return out


@router.get("/{deposit_id}")
async def get_one(deposit_id: str, acct=auth.Account):
    d = await db().deposits.find_one({"_id": deposit_id, "account_id": acct["account_id"]})
    if not d:
        raise HTTPException(404, "unknown deposit")
    return public_deposit(d)
