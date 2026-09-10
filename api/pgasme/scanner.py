"""On-chain scanner — the PRIMARY evidence for deposits (the rules of the founder's proven
order scanner in the reference implementation, ported):

  * eth_getLogs on each target pipe (ETH / DAI / WBTC) for NewLocalMessage, in chunks of
    `lock_scan_chunk` blocks, PINNED to the endpoint that vouched for the head: the endpoint
    that said "the chain is at H" is the only one allowed to answer "[0, H] holds no locks",
    and its own head is re-read and required to be ≥ the chunk's end BEFORE the chunk is
    checkpointed. An empty answer from a lagging node is not evidence of no deposit;
  * an endpoint that will not serve the pinned range — its logs lag the head it just claimed
    (rpc.flashbots.net answers eth_blockNumber with H and then -32602 "block range extends
    beyond current head block" for a range ending at H), or it caps the range, or the blocks are
    behind an archive/API-key wall — is DEMOTED for a cooldown, and the same pass re-resolves
    the head on the next endpoint and carries on FROM THE SAME BLOCK. The pin decides who may
    answer; it must never decide that nobody does. The scanner starts from the endpoint that
    served last time, and the pass warns ONCE, naming every endpoint it tried and why each
    failed;
  * a checkpoint per pipe in Mongo (`scanner_state` {_id: pipe, last_block}); the first run starts
    at head − `lock_scan_blocks`. ONLY the highest CONTIGUOUS successfully-scanned block is ever
    checkpointed — a failed chunk is never skipped, it is retried next pass; a pass is bounded to
    MAX_CHUNKS_PER_PASS so a backlog on one pipe cannot stall the others;
  * stalled-node guard: when the primary endpoint says "no new blocks", the pool's heads are
    compared (endpoints with eth_syncing != false are skipped); if the pool is > 50 blocks ahead
    the pass scans up to the pool head, pinned to the endpoint that reported it — "no new blocks"
    and "I cannot see the chain" must never behave the same;
  * every NewLocalMessage naming OUR pubkey is deduped by (tx, logIndex), its receipt fetched and
    the router's FulfilledOrder log in the same receipt decoded → orderId → the deposit / quote; if the
    id is unknown, the metadata tag (bytes[45:50] of rawOrderMetadataHex from the router's stats API,
    retried because the router indexes with delay) is matched against `quotes.metadata`;
  * a receipt with NO FulfilledOrder log is not automatically foreign. Two other stories end
    there: a `uniswap` gateway deposit, whose receipt carries OUR hook's own `PgasDeposit(ref,
    payer, …)` log — the reference finds the deposit (or the quote, when the user signed and
    never registered the hash) and the payer is what makes it theirs; and a `direct` deposit
    (the user called the pipe themselves), looked up by its own registered `src_tx_hash`. Only
    after both fail is the lock unattributed;
  * sanity before any state change: the lock's amount == the quote's value_units, the pipe is
    the asset's pipe, and for a `direct` deposit the receipt's `from` is the wallet that asked
    for the quote (the hash alone is public — the sender is what makes it the user's). A
    `uniswap` deposit is the one whose amount is NOT known in advance — the hook splits the real
    swap output — so there the check is a BAND (`uniswap.value_band`), plus the hook's `value`
    and the pipe's `amount` having to be the same number in the same receipt, and the credit
    takes the pipe log's amount rather than the estimate. A lock to our pubkey that nobody can
    be attributed to goes to `unattributed_locks` and pages the operator — it is never credited
    to anyone.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from eth_abi import decode
from eth_utils import keccak

from . import ethpipe, tg, uniswap, xchain
from .assets import ASSETS, Asset, to_groth
from .config import settings
from .db import db

log = logging.getLogger("pgasme.scanner")

ORDER_TUPLE = (
    "(uint64,bytes,uint256,bytes,uint256,uint256,bytes,uint256,bytes,bytes,bytes,bytes,bytes,bytes)"
)
FULFILLED_SIG = f"FulfilledOrder({ORDER_TUPLE},bytes32,uint256,address,address)"
FULFILLED_TOPIC = "0x" + keccak(text=FULFILLED_SIG).hex()
FULFILLED_TOPIC_EXPECTED = "0xc164aca37b9805a1c9027b6f32260a069723a82926f6e9ece4926e4dd3ea8ecf"
# where the router keeps the 5-byte `metadata` we sent, inside rawOrderMetadataHex
METADATA_TAG_SLICE = (45, 50)
ZERO_TAG = "0x0000000000"
POOL_AHEAD_BLOCKS = 50
MAX_CHUNKS_PER_PASS = 40
LITE_RETRIES = 5
LITE_RETRY_SLEEP_S = 5.0
CLAIMABLE = ("submitted", "order_seen", "fallback_pending")
# retry_unattributed: oldest first, backed off, and abandoned rather than retried forever
RETRY_PAGE = 20
RETRY_MAX_ROWS = 200
RETRY_BACKOFF_S = 120.0
RETRY_BACKOFF_MAX_S = 3600.0
RETRY_MAX_TRIES = 12
DEPOSIT_HASH_INDEX = "uniq_src_tx_hash"
DEPOSIT_REF_INDEX = "uniq_deposit_ref"
# an endpoint whose logs lag its own head is skipped for this long (prod: flashbots, every pass)
ENDPOINT_COOLDOWN_S = 300.0
_endpoints: dict[str, Any] = {"bad": {}, "sticky": None}


def reset_endpoint_state() -> None:
    """Forget which endpoints lagged (process state; tests and a restart start clean)."""
    _endpoints["bad"] = {}
    _endpoints["sticky"] = None


def demote_endpoint(url: str | None, why: str) -> None:
    if not url:
        return
    _endpoints["bad"][url] = time.time() + ENDPOINT_COOLDOWN_S
    if _endpoints["sticky"] == url:
        _endpoints["sticky"] = None


def endpoint_candidates(rpc: ethpipe.Rpc) -> list[str]:
    """The order to TRY endpoints in: the one that served last time first, then the rest, then
    the demoted ones (a cooling endpoint is a last resort, never a dead one — a pass with no
    healthy endpoint left must still try rather than skip the chain)."""
    urls = list(getattr(rpc, "urls", None) or [])
    if not urls:
        return []
    now = time.time()
    bad = _endpoints["bad"]
    fresh = [u for u in urls if bad.get(u, 0.0) <= now]
    cooling = [u for u in urls if bad.get(u, 0.0) > now]
    sticky = _endpoints["sticky"]
    if sticky in fresh:
        fresh = [sticky] + [u for u in fresh if u != sticky]
    return fresh + cooling


async def ensure_indexes() -> None:
    """The scanner's own guarantee, called at worker start: ONE deposit per source transaction.
    Partial, because the scanner legitimately creates rows with no hash at all (a lock seen
    before the user registered theirs) and many nulls are not a collision."""
    await db().deposits.create_index(
        "src_tx_hash",
        unique=True,
        partialFilterExpression={"src_tx_hash": {"$type": "string"}},
        name=DEPOSIT_HASH_INDEX,
    )
    # …and ONE deposit per Uniswap deposit reference, for the same reason: the reference is how
    # a pipe lock with no cross-chain fill is matched back to a row, and two rows sharing one
    # would make that match ambiguous exactly when money has already landed. Partial for the
    # same reason too — every other mode has no reference at all.
    await db().deposits.create_index(
        "deposit_ref",
        unique=True,
        partialFilterExpression={"deposit_ref": {"$type": "string"}},
        name=DEPOSIT_REF_INDEX,
    )
    # the quote side is a LOOKUP, not a guarantee: a lock may arrive before the user ever
    # registered the hash, and the reference is then the only way back to the account.
    await db().quotes.create_index("deposit_ref")


# ----------------------------------------------------------------------------- decoding


def decode_fulfilled_order(lg: dict[str, Any]) -> dict[str, Any]:
    data = bytes.fromhex(lg["data"].removeprefix("0x"))
    order, order_id, actual, sender, unlock = decode(
        [ORDER_TUPLE, "bytes32", "uint256", "address", "address"], data
    )
    (
        nonce,
        _maker_src,
        give_chain,
        _give_token,
        give_amount,
        take_chain,
        take_token,
        take_amount,
        receiver_dst,
        _gpa,
        _oaa,
        _atd,
        _acb,
        external_call,
    ) = order
    return {
        "order_id": "0x" + order_id.hex(),
        "maker_order_nonce": int(nonce),
        "give_chain_id": int(give_chain),
        "give_amount": int(give_amount),
        "take_chain_id": int(take_chain),
        "take_token": "0x" + take_token.hex(),
        "take_amount": int(take_amount),
        "receiver_dst": "0x" + receiver_dst.hex(),
        "actual_fulfill_amount": int(actual),
        "sender": sender,
        "unlock_authority": unlock,
        "has_external_call": len(external_call) > 0,
        "tx": lg.get("transactionHash"),
        "log_index": ethpipe._int(lg.get("logIndex")),
    }


def find_fulfilled_in_receipt(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lg in receipt.get("logs", []):
        if lg.get("topics") and lg["topics"][0].lower() == FULFILLED_TOPIC:
            try:
                out.append(decode_fulfilled_order(lg))
            except Exception as e:  # noqa: BLE001 — a foreign log with the same topic is not evidence
                log.warning("FulfilledOrder decode failed in %s: %s", lg.get("transactionHash"), e)
    return out


def metadata_tag(raw_hex: str | None) -> str | None:
    if not raw_hex:
        return None
    try:
        b = bytes.fromhex(raw_hex.removeprefix("0x"))
    except ValueError:
        return None
    a, z = METADATA_TAG_SLICE
    if len(b) < z:
        return None
    return "0x" + b[a:z].hex()


# ----------------------------------------------------------------------------- head / checkpoint


async def resolve_head(
    rpc: ethpipe.Rpc, checkpoint: int | None, prefer: str | None = None
) -> tuple[int, str]:
    """(head, the endpoint that vouched for it). With `prefer` the head comes from THAT endpoint
    (pinned) — the scan then reads the range from the same node that claimed to see it. The pool
    overrides a primary that reports no new blocks while others are > POOL_AHEAD_BLOCKS ahead.
    The endpoint is never None: every read of the range is pinned to whoever claimed to see it."""
    if prefer:
        head, endpoint = await rpc.block_number(prefer=prefer, pin=True), prefer
    else:
        head, endpoint = await rpc.head_from()
    if checkpoint is not None and head <= checkpoint:
        heads = await rpc.pool_heads()
        if heads:
            best_url, best = max(heads.items(), key=lambda kv: kv[1])
            if best > head + POOL_AHEAD_BLOCKS:
                log.warning("primary head %d is stalled; pool head %d via %s", head, best, best_url)
                return best, best_url
    return head, endpoint


async def known_pubkeys(asset: Asset) -> set[str]:
    keys = set()
    if pk := settings.pubkey_for(asset.key):
        keys.add(pk.lower())
    for pk in await db().deposits.distinct(
        "pubkey",
        {"asset": asset.key, "status": {"$in": list(CLAIMABLE) + ["locked", "confirming"]}},
    ):
        if pk:
            keys.add(str(pk).lower())
    return keys


async def scan_pipe(
    asset: Asset, rpc: ethpipe.Rpc, max_chunks: int = MAX_CHUNKS_PER_PASS
) -> dict[str, Any]:
    """One pass over one pipe. Bounded by max_chunks and by the number of endpoints: a chunk the
    vouching endpoint cannot serve moves the pass to the NEXT endpoint at the same block, never
    past it, and only an exhausted endpoint list ends the pass — with one warning that names
    every endpoint tried and what it said."""
    pubkeys = await known_pubkeys(asset)
    if not pubkeys:
        return {"asset": asset.key, "skipped": "no pubkey configured"}
    st = await db().scanner_state.find_one({"_id": asset.pipe})
    checkpoint = int(st["last_block"]) if st else None
    chunk = max(1, settings.lock_scan_chunk)
    stats: dict[str, Any] = {
        "asset": asset.key,
        "from": None,
        "head": None,
        "chunks": 0,
        "logs": 0,
        "locked": 0,
        "unattributed": 0,
        "failed": None,
        "prefer": None,
        "unserved": [],
    }
    cur: int | None = None
    end = 0
    for candidate in endpoint_candidates(rpc) or [None]:
        try:
            head, endpoint = await resolve_head(rpc, checkpoint, prefer=candidate)
        except Exception as e:  # noqa: BLE001 — an endpoint that cannot even give a head is out
            demote_endpoint(candidate, f"eth_blockNumber: {e}")
            stats["unserved"].append({"url": candidate, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        stats["head"], stats["prefer"] = head, endpoint
        if cur is None:
            cur = checkpoint + 1 if checkpoint is not None else max(0, head - settings.lock_scan_blocks)
            stats["from"] = cur
        lagged = False
        while cur <= head and stats["chunks"] < max_chunks:
            end = min(cur + chunk - 1, head)
            try:
                # the endpoint that vouched for the head answers the range, or nothing does
                logs = await rpc.logs(
                    asset.pipe, [ethpipe.NEWLOCAL_TOPIC], cur, end, prefer=endpoint, pin=True
                )
                # ... and the answer counts only if it still has the blocks: a lagging node's
                # empty answer must never become a checkpoint
                seen_head = await rpc.block_number(prefer=endpoint, pin=True)
                if seen_head < end:
                    raise ethpipe.RpcError(
                        f"{endpoint} answered [{cur}, {end}] but its head is {seen_head} — "
                        "the range is not readable there yet",
                        url=endpoint,
                        code=-32602,
                    )
            except Exception as e:  # noqa: BLE001 — WHY it failed decides what happens next
                if ethpipe.cannot_serve(e):
                    # this endpoint will not serve the range (its logs are behind the head it
                    # just claimed, or it caps the range, or the blocks are behind a plan wall):
                    # try the next one FROM THIS VERY BLOCK. Nothing skipped, nothing
                    # checkpointed, and the failure is never read as "no locks".
                    demote_endpoint(endpoint, str(e))
                    stats["unserved"].append(
                        {"url": endpoint, "error": f"{type(e).__name__}: {e}"[:200]}
                    )
                    lagged = True
                    break
                stats["failed"] = {"from": cur, "to": end, "error": f"{type(e).__name__}: {e}"}
                break
            try:
                for lg in logs:
                    res = await handle_log(lg, asset, pubkeys, rpc, prefer=endpoint)
                    if res in ("locked", "unattributed"):
                        stats[res] += 1
            except Exception as e:  # noqa: BLE001 — a chunk that cannot be read is retried next pass, never skipped
                stats["failed"] = {"from": cur, "to": end, "error": f"{type(e).__name__}: {e}"}
                break
            await db().scanner_state.update_one(
                {"_id": asset.pipe},
                {"$set": {"last_block": end, "at": time.time(), "asset": asset.key}},
                upsert=True,
            )
            _endpoints["sticky"] = endpoint  # start here next time
            stats["chunks"] += 1
            stats["logs"] += len(logs)
            cur = end + 1
        if not lagged:
            break
    else:
        # no endpoint would serve the range (lag, a cap, a plan wall, or no head at all)
        if stats["unserved"]:
            why = "; ".join(f"{r['url']}: {r['error']}" for r in stats["unserved"])
            stats["failed"] = {
                "from": cur if cur is not None else checkpoint,
                "to": end,
                "error": f"no endpoint could serve the range — {why}",
            }
    if stats["failed"]:  # ONE line per pass, naming what was tried
        log.warning(
            "scan %s [%s, %s] failed on %s: %s",
            asset.key,
            stats["failed"]["from"],
            stats["failed"]["to"],
            stats["prefer"] or "no endpoint",
            stats["failed"]["error"],
        )
    elif stats["unserved"]:
        log.info(
            "scan %s served by %s after %d endpoint(s) refused the range: %s",
            asset.key,
            stats["prefer"],
            len(stats["unserved"]),
            "; ".join(r["url"] or "?" for r in stats["unserved"]),
        )
    stats["checkpoint"] = cur - 1 if stats["chunks"] else checkpoint
    return stats


# ----------------------------------------------------------------------------- attribution


async def _seen(m: dict[str, Any]) -> bool:
    d = db()
    if await d.deposits.find_one({"eth.tx": m["tx"], "eth.log_index": m["log_index"]}, {"_id": 1}):
        return True
    return (
        await d.unattributed_locks.find_one({"_id": f"{m['tx']}:{m['log_index']}"}, {"_id": 1})
        is not None
    )


def _sane(doc: dict[str, Any], value_units: int, m: dict[str, Any], asset: Asset) -> str | None:
    if doc.get("asset") != asset.key:
        return f"asset mismatch: lock on the {asset.key} pipe, quote for {doc.get('asset')}"
    if value_units != m["amount"]:
        return f"amount mismatch: lock {m['amount']} vs quote value {value_units}"
    if m["address"].lower() != asset.pipe.lower():
        return "pipe mismatch"
    return None


def _payer_ok(doc: dict[str, Any], hooked: dict[str, Any]) -> str | None:
    """The hook names the payer it attributed the deposit to. `hookData` is caller-controlled —
    anyone may copy someone else's reference into their own call — so the reference alone is a
    HINT and the payer is what makes the lock this account's (§IDENTITY-BEATS-BALANCE)."""
    want = (doc.get("address") or "").lower()
    if not want:
        return "cannot check the payer: the row carries no wallet address"
    if hooked["payer"].lower() != want:
        return (
            f"payer mismatch: the hook attributed this deposit to {hooked['payer']}, not to the "
            "wallet that asked for the quote"
        )
    return None


def _sane_uniswap(
    doc: dict[str, Any], m: dict[str, Any], asset: Asset, hooked: dict[str, Any]
) -> str | None:
    """Is this pipe lock what the gateway deposit we quoted would have produced?

    Unlike `direct` / `xchain`, the amount is NOT known in advance: the hook splits the REAL
    swap output. So the check is a band, not an equality (uniswap.value_band is the one
    implementation of it), plus two facts that must hold exactly: the hook's own `value` and the
    pipe's `amount` are the same number in the same receipt, and that number sits on the asset's
    grid. Anything outside goes to `unattributed_locks` and pages — it is never credited."""
    if doc.get("asset") != asset.key:
        return f"asset mismatch: lock on the {asset.key} pipe, quote for {doc.get('asset')}"
    if m["address"].lower() != asset.pipe.lower():
        return "pipe mismatch"
    if hooked["value"] != m["amount"]:
        return (
            f"the hook logged value {hooked['value']} and the pipe logged {m['amount']} in one "
            "receipt — they must be the same number"
        )
    if asset.grid > 1 and m["amount"] % asset.grid:
        return f"lock {m['amount']} is not a multiple of the {asset.key} grid {asset.grid}"
    try:
        lo, hi = uniswap.value_band(
            int(doc["min_out_units"]),
            int(doc["out_units"]),
            int(doc["relayer_fee_quote_units"]),
            asset,
        )
    except (KeyError, TypeError, ValueError, ethpipe.SplitError) as e:
        # A row that cannot say what it expected cannot vouch for what arrived. Fail closed.
        return f"cannot check the amount against the quote ({type(e).__name__}: {e})"
    if not lo <= m["amount"] <= hi:
        return f"lock {m['amount']} is outside the quoted band [{lo}, {hi}]"
    return None


async def _attribute_uniswap(
    hooked: list[dict[str, Any]], m: dict[str, Any], asset: Asset
) -> tuple[dict[str, Any] | None, str, str]:
    """A pipe lock whose receipt carries OUR hook's `PgasDeposit`: the reference names the row.

    The deposit row is the normal case (the user registered the hash). A QUOTE is the case
    where they signed and never came back — the money is in the pipe either way, and a deposit
    the user never registered must still reach them (`lock_deposit` opens the row)."""
    d = db()
    reasons: list[str] = []
    for h in hooked:
        for coll, how in ((d.deposits, "deposit"), (d.quotes, "quote")):
            doc = await coll.find_one({"mode": uniswap.MODE, "deposit_ref": h["ref"]})
            if not doc:
                continue
            why = _payer_ok(doc, h) or _sane_uniswap(doc, m, asset, h)
            if why:
                reasons.append(why)
                break
            return doc, how, ""
        else:
            reasons.append(f"no deposit or quote carries the hook reference {h['ref']}")
    return None, "", "; ".join(reasons)


def _direct_sender_ok(dep: dict[str, Any], receipt: dict[str, Any] | None) -> str | None:
    """A registered hash is public information. For a `direct` deposit the claim is only the
    user's if the transaction was SENT by the wallet that asked for the quote."""
    want = (dep.get("address") or "").lower()
    if not want:
        return None  # a row written before the address was stored: the amount check still holds
    got = ((receipt or {}).get("from") or "").lower()
    if not got:
        return "cannot check the sender: the receipt carries no `from`"
    if got != want:
        return "sender mismatch: the pipe call was not sent by the wallet that asked for the quote"
    return None


async def attribute(
    m: dict[str, Any],
    asset: Asset,
    fulfilled: list[dict[str, Any]],
    retries: int = LITE_RETRIES,
    receipt: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """→ (doc, 'deposit'|'quote', reason). doc is None when nothing can be attributed."""
    d = db()
    if not fulfilled:
        # No cross-chain fill in this receipt. Two other stories end here, and both are proven
        # by something in THIS receipt rather than by the amount:
        #   * a `uniswap` gateway deposit — our hook's own PgasDeposit log names the reference;
        #   * a `direct` deposit — the pipe call IS the transaction the client registered, so
        #     that hash plus the sender is the whole claim.
        hooked = uniswap.find_deposits_in_receipt(receipt or {})
        if hooked:
            return await _attribute_uniswap(hooked, m, asset)
        dep = await d.deposits.find_one(
            {"mode": "direct", "src_tx_hash": (m.get("tx") or "").lower()}
        )
        if dep:
            why = _direct_sender_ok(dep, receipt) or _sane(
                dep, int(dep["eth"]["value_units"]), m, asset
            )
            return (None, "deposit", why) if why else (dep, "deposit", "")
        return None, "", "no FulfilledOrder log in the receipt (not a cross-chain fill)"
    order_ids = [f["order_id"] for f in fulfilled]
    for oid in order_ids:
        dep = await d.deposits.find_one({"order_id": oid})
        if dep:
            why = _sane(dep, int(dep["eth"]["value_units"]), m, asset)
            return (None, "deposit", why) if why else (dep, "deposit", "")
        q = await d.quotes.find_one({"order_id": oid})
        if q:
            why = _sane(q, int(q["value_units"]), m, asset)
            return (None, "quote", why) if why else (q, "quote", "")
    for attempt in range(max(1, retries)):
        for oid in order_ids:
            lm = await xchain.lite_model(oid)
            tag = metadata_tag((lm or {}).get("rawOrderMetadataHex"))
            if not tag or tag == ZERO_TAG:
                continue
            q = await d.quotes.find_one({"metadata": tag})
            if q:
                q = {**q, "order_id": oid, "quote_order_id": q.get("order_id")}
                why = _sane(q, int(q["value_units"]), m, asset)
                return (None, "quote", why) if why else (q, "quote", "")
        if attempt < retries - 1:
            await ethpipe.sleep(LITE_RETRY_SLEEP_S)
    return (
        None,
        "",
        f"no deposit or quote matches order(s) {', '.join(order_ids)} by id or metadata tag",
    )


async def lock_deposit(
    doc: dict[str, Any],
    how: str,
    m: dict[str, Any],
    asset: Asset,
    fulfilled: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Claim-first: only a deposit in a claimable state moves to `locked`. A quote without a
    deposit row (the user signed but never registered the hash) gets its deposit created here."""
    d = db()
    now = time.time()
    mode = xchain.norm_mode(doc.get("mode"))
    lock_fields = {
        "eth.tx": m["tx"],
        "eth.block": m["block"],
        "eth.msg_id": m["msg_id"],
        "eth.log_index": m["log_index"],
        "eth.relayer_fee_logged": str(m["relayer_fee"]),
        "locked_at": now,
        "updated_at": now,
    }
    if mode == uniswap.MODE:
        # ⛔ THE PIPE LOG IS THE MONEY. A gateway deposit is worth what the swap actually paid,
        # never what we quoted: the hook splits the REAL output on-chain. The estimate is kept
        # beside it as evidence, and `_sane_uniswap` has already refused anything outside the
        # band this quote could produce.
        lock_fields |= {
            "eth.value_units": str(m["amount"]),
            "eth.relayer_fee_units": str(m["relayer_fee"]),
            "value_groth": to_groth(int(m["amount"]), asset),
            "value_groth_quoted": int(doc.get("value_groth") or 0),
        }
    fill = next((f for f in fulfilled or () if f["order_id"] == doc.get("order_id")), None)
    if fill:
        lock_fields["eth.fill_units"] = str(fill["actual_fulfill_amount"])
    if how == "quote":
        dep = await d.deposits.find_one({"quote_id": doc["_id"]})
        if dep is None:
            deposit_id = secrets.token_hex(12)
            # what the LOG says for a gateway deposit, what the quote says for every other mode
            value_units = int(m["amount"]) if mode == uniswap.MODE else int(doc["value_units"])
            relayer_fee = (
                str(m["relayer_fee"]) if mode == uniswap.MODE else doc["relayer_fee_units"]
            )
            await d.deposits.insert_one(
                {
                    "_id": deposit_id,
                    "account_id": doc["account_id"],
                    # the mode this deposit really is: a row created here used to carry none,
                    # which reads as the cross-chain mode and is only right for that one.
                    "mode": mode,
                    "address": doc.get("address"),
                    "asset": doc["asset"],
                    "status": "submitted",
                    "src": {
                        "chain_id": doc["src"]["chain_id"],
                        "token": doc["src"]["token"],
                        "amount": doc["src"]["amount"],
                    },
                    "quote_id": doc["_id"],
                    "src_tx_hash": None,
                    "order_id": doc.get("order_id"),
                    "deposit_ref": doc.get("deposit_ref"),
                    "route": doc.get("route"),
                    "out_units": doc.get("out_units"),
                    "min_out_units": doc.get("min_out_units"),
                    "relayer_fee_quote_units": doc.get("relayer_fee_quote_units"),
                    "eth": {
                        "value_units": str(value_units),
                        "relayer_fee_units": relayer_fee,
                    },
                    "value_groth": to_groth(value_units, asset),
                    **(
                        {"value_groth_quoted": int(doc["value_groth"])}
                        if mode == uniswap.MODE
                        else {}
                    ),
                    "metadata": doc.get("metadata"),
                    "pubkey": doc.get("pubkey"),
                    "created_at": now,
                    "updated_at": now,
                    "note": "created by the scanner from the quote (lock seen before the hash was registered)",
                }
            )
            dep = await d.deposits.find_one({"_id": deposit_id})
        doc = dep
    claimed = await d.deposits.find_one_and_update(
        {"_id": doc["_id"], "status": {"$in": list(CLAIMABLE)}},
        {
            "$set": {
                "status": "locked",
                "order_id": doc.get("order_id") or m.get("order_id"),
                **lock_fields,
            }
        },
    )
    return claimed


async def record_unattributed(
    m: dict[str, Any], asset: Asset, fulfilled: list[dict[str, Any]], reason: str
) -> None:
    d = db()
    key = f"{m['tx']}:{m['log_index']}"
    row = {
        "_id": key,
        "asset": asset.key,
        "pipe": asset.pipe,
        "tx": m["tx"],
        "block": m["block"],
        "log_index": m["log_index"],
        "msg_id": m["msg_id"],
        "amount": str(m["amount"]),
        "relayer_fee": str(m["relayer_fee"]),
        "receiver": m["receiver"],
        "order_ids": [f["order_id"] for f in fulfilled],
        "reason": reason,
        "status": "open",
        "at": time.time(),
    }
    await d.unattributed_locks.update_one({"_id": key}, {"$setOnInsert": row}, upsert=True)
    await tg.alert(
        "lock_unattributed",
        f"MANUAL HANDLING REQUIRED: a {asset.key} lock to our pubkey cannot be attributed — nobody was "
        f"credited. tx {m['tx']} msgId {m['msg_id']} amount {m['amount']} ({reason})",
        lock=key,
    )


async def handle_log(
    lg: dict[str, Any],
    asset: Asset,
    pubkeys: set[str],
    rpc: ethpipe.Rpc,
    prefer: str | None = None,
    retries: int = LITE_RETRIES,
) -> str:
    m = ethpipe.decode_new_local_message(lg)
    if m["receiver"].lower() not in pubkeys:
        return "foreign"
    if await _seen(m):
        return "seen"
    receipt = await rpc.receipt(m["tx"], prefer=prefer, pin=prefer is not None)
    if not receipt:
        raise ethpipe.RpcError(f"receipt {m['tx']} not available yet — chunk will be retried")
    fulfilled = find_fulfilled_in_receipt(receipt)
    doc, how, reason = await attribute(m, asset, fulfilled, retries=retries, receipt=receipt)
    if doc is not None:
        claimed = await lock_deposit(doc, how, m, asset, fulfilled)
        if claimed:
            await tg.queue(
                "deposit_locked",
                f"Deposit locked in the {asset.key} pipe, msg {m['msg_id']}",
                deposit_id=claimed["_id"],
            )
            return "locked"
        reason = f"matched deposit {doc['_id']} but it is not in a claimable state"
    await record_unattributed(m, asset, fulfilled, reason)
    return "unattributed"


def _backoff(tries: int) -> float:
    return min(RETRY_BACKOFF_S * (2 ** max(0, tries - 1)), RETRY_BACKOFF_MAX_S)


async def abandon_unattributed(row: dict[str, Any], why: str) -> None:
    """Stop retrying a lock nothing can attribute. It is NOT resolved and nobody is credited —
    it stays on the operator's desk, it just no longer burns a slot in every pass."""
    await db().unattributed_locks.update_one(
        {"_id": row["_id"], "status": "open"},
        {"$set": {"status": "abandoned", "abandoned_at": time.time(), "abandoned_why": why}},
    )
    await tg.alert(
        "lock_abandoned",
        f"MANUAL HANDLING REQUIRED: a {row.get('asset')} lock is still unattributed after "
        f"{int(row.get('tries') or 0)} attempts ({why}) — automatic retries have stopped and "
        "nobody has been credited",
        lock=row["_id"],
    )


async def retry_unattributed(
    rpc: ethpipe.Rpc,
    max_age_s: float = 86400.0,
    page: int = RETRY_PAGE,
    max_rows: int = RETRY_MAX_ROWS,
) -> int:
    """Re-attempt open unattributed locks (the router's indexing lag is the usual cause), OLDEST FIRST
    and backed off per row, paging until the due rows run out. An un-sorted, un-aged window of
    20 could be filled forever by rows that will never attribute, and starve a recoverable one.
    No sleeps here."""
    d = db()
    n = 0
    seen = 0
    while seen < max_rows:
        now = time.time()
        due = {
            "status": "open",
            "$or": [{"next_try_at": {"$exists": False}}, {"next_try_at": {"$lte": now}}],
        }
        rows = await d.unattributed_locks.find(due).sort("at", 1).limit(page).to_list(page)
        if not rows:
            break
        seen += len(rows)
        for row in rows:
            tries = int(row.get("tries") or 0) + 1
            if now - float(row.get("at") or 0) > max_age_s:
                await abandon_unattributed(row, f"older than {int(max_age_s)}s")
                continue
            if tries > RETRY_MAX_TRIES:
                await abandon_unattributed(row, f"{RETRY_MAX_TRIES} attempts")
                continue
            await d.unattributed_locks.update_one(
                {"_id": row["_id"]},
                {
                    "$set": {
                        "tries": tries,
                        "last_try_at": now,
                        "next_try_at": now + _backoff(tries),
                    }
                },
            )
            if await _retry_one(row, rpc):
                n += 1
        if len(rows) < page:
            break
    return n


async def _retry_one(row: dict[str, Any], rpc: ethpipe.Rpc) -> bool:
    """One re-attribution attempt. True when the lock became a locked deposit."""
    d = db()
    asset = ASSETS.get(row["asset"])
    if asset is None:
        return False
    receipt = await rpc.receipt(row["tx"])
    if not receipt:
        return False
    m = {
        "tx": row["tx"],
        "block": row["block"],
        "log_index": row["log_index"],
        "msg_id": row["msg_id"],
        "amount": int(row["amount"]),
        "relayer_fee": int(row["relayer_fee"]),
        "receiver": row["receiver"],
        "address": row["pipe"],
    }
    fulfilled = find_fulfilled_in_receipt(receipt)
    doc, how, _ = await attribute(m, asset, fulfilled, retries=1, receipt=receipt)
    if doc is None:
        return False
    claimed = await lock_deposit(doc, how, m, asset, fulfilled)
    if not claimed:
        return False
    await d.unattributed_locks.update_one(
        {"_id": row["_id"]},
        {
            "$set": {
                "status": "resolved",
                "deposit_id": claimed["_id"],
                "resolved_at": time.time(),
            }
        },
    )
    await tg.queue(
        "deposit_locked",
        f"Deposit locked (late attribution) in the {asset.key} pipe, msg {m['msg_id']}",
        deposit_id=claimed["_id"],
    )
    return True
