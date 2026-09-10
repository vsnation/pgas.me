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

from . import ethpipe, receiver_keys, tg, uniswap, xchain
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
# quote attribution: a quote is a candidate for a lock only if it was created BEFORE the block,
# with a little slack because OUR clock stamps the quote and the CHAIN stamps the block.
QUOTE_AFTER_BLOCK_SLACK_S = 300.0
# newest-first inside the window: the quote a lock was built from is the NEAREST PRECEDING one,
# and the newest N are the right end of the list to keep when a busy account overflows it.
QUOTE_CANDIDATE_LIMIT = 200
DEPOSIT_HASH_INDEX = "uniq_src_tx_hash"
DEPOSIT_REF_INDEX = "uniq_deposit_ref"
# ⛔ A TERMINAL-FAILED ROW IS EVIDENCE, NEVER A CLAIM — and this filter is the ONE writer of
# that rule. `workers._mismatch` and the unseen-TTL path both fail a row, release its hash
# (`$unset src_tx_hash`) and credit nothing, but the row KEEPS its `quote_id`, because a ledger
# is corrected by appending, never by editing history. The last gate of `attribute_from_quote`
# used to ask "does any deposit row carry this quote?" with no status filter, so one failed row
# made its quote read as spent FOREVER: the very pipe call whose 394 bytes we issued could not
# be matched back to it and went to MANUAL HANDLING instead. Every reader of "is this quote /
# this hash already claimed?" excludes `failed` — here, and nowhere else.
FAILED = "failed"
LIVE: dict[str, Any] = {"$ne": FAILED}
# how many rows may claim one hash before we stop reading them. A bound on the query, not a cap
# on truth: identity decides which one owns it, and only one can ever pass identity.
CLAIM_LIMIT = 20
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
    # One VERIFIED deposit per source transaction — a weaker index than it looks,
    # and deliberately so. A hash is public the moment it is broadcast, so registering one is a
    # CLAIM: several accounts may hold a claim on the same hash (see `claims_on`), and until
    # identity has spoken none of them is a title. Making the index cover every string hash
    # meant whoever POSTED first took it — a stranger watching the mempool could lock the person
    # who signed the transaction out with a 409 until the TTL expired. `verified: true` is
    # exactly the moment identity spoke, and it can be true for one row only.
    # (`db.ensure_indexes` builds the same index under the same name and repairs a pre-existing
    # one whose filter is the old, wider expression; it runs first, in the API process, before
    # the workers this is called from.)
    await db().deposits.create_index(
        "src_tx_hash",
        unique=True,
        partialFilterExpression={"src_tx_hash": {"$type": "string"}, "verified": True},
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
    """Every receiver key a lock on this pipe could legitimately name: the pipe's legacy key,
    every live deposit's own key, and every per-deposit key issued inside the attribution
    window (`receiver_keys.issued_pks`).

    ⛔ A KEY THAT IS NOT IN THIS SET IS NOT MERELY UNATTRIBUTED — IT IS UNSEEN. `handle_log`
    drops a lock whose receiver is not here as "foreign", so nothing records it and nobody is
    paged. That is why the issued-key half is a union with the live-deposit half rather than a
    replacement for it: a transit slower than the window keeps its key known through its row.
    And it is why the read RAISES rather than returning what it managed to collect — this set
    coming back short reads as "somebody else's deposit"."""
    keys = set()
    if pk := settings.pubkey_for(asset.key):
        keys.add(pk.lower())
    for pk in await db().deposits.distinct(
        "pubkey",
        {"asset": asset.key, "status": {"$in": list(CLAIMABLE) + ["locked", "confirming"]}},
    ):
        if pk:
            keys.add(str(pk).lower())
    keys |= await receiver_keys.issued_pks(asset)
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


# --------------------------------------------- identity: is this TRANSACTION this quote's?

# The refusal sentences POST /v1/deposits answers with. They live here because the check lives
# here: registration, the watcher's re-verification and the scanner's own attribution must all
# ask the SAME question and get the SAME answer (law 9 — two implementations of one fact will
# disagree, and one of them reaches money).
NOT_YOUR_WALLET = (
    "that transaction was not sent from the wallet this quote was issued to — the pipe "
    "call must come from your signed-in wallet"
)
NOT_A_SENDFUNDS = "that transaction is not a sendFunds call on the pipe"
WRONG_PUBKEY = "that pipe call locks to a different Beam pubkey than this quote"
WRONG_AMOUNT = (
    "that pipe call locks a different amount than this quote — quote again for the "
    "amount you actually sent, then register it"
)


def quote_created_at(q: dict[str, Any]) -> float:
    """When this quote was issued. `_store` stamps `at`; a row written by anything else may
    carry `created_at` instead. 0.0 when neither exists — an undated quote can never be proven
    to predate a block, so it is never attributed to one."""
    return float(q.get("at") or q.get("created_at") or 0.0)


def expected_send_funds(q: dict[str, Any]) -> str | None:
    """The exact `sendFunds` calldata THIS quote was armed with, lowercased — the stored one
    when the quote carries it, otherwise re-derived from the three numbers it was built from.
    None when the quote cannot say (an estimate that was never armed)."""
    stored = str(q.get("hook_calldata") or "").lower()
    if stored:
        return stored
    try:
        return ethpipe.encode_send_funds(
            int(q["value_units"]), int(q["relayer_fee_units"]), str(q["pubkey"])
        ).lower()
    except (KeyError, TypeError, ValueError):
        return None


def direct_identity_reason(
    tx: dict[str, Any], q: dict[str, Any], asset: Asset
) -> str | None:
    """Why this transaction is NOT the pipe call this `direct` quote was issued for — or None.

    Three facts, and all of them (§IDENTITY-BEATS-BALANCE): `from` is the wallet that asked for
    the quote, `to` is the asset's pipe, and the calldata is this quote's own sendFunds — the
    stored calldata byte for byte, or failing that the pubkey and the value it was priced at.
    A hash is public the moment it is broadcast; none of this is."""
    sender = (tx.get("from") or "").lower()
    to = (tx.get("to") or "").lower()
    if sender != str(q.get("address") or "").lower():
        return NOT_YOUR_WALLET
    if to != asset.pipe.lower():
        return f"that transaction is not a call to the {asset.key} pipe of this quote"
    data = str(tx.get("input") or tx.get("data") or "").lower()
    want = expected_send_funds(q)
    if want and data == want:
        return None
    try:
        call = ethpipe.decode_send_funds(data)
    except (ValueError, KeyError):
        return NOT_A_SENDFUNDS
    if call["pubkey"].lower() != str(q.get("pubkey") or "").lower():
        return WRONG_PUBKEY
    if str(call["value"]) != str(q.get("value_units")):
        return WRONG_AMOUNT
    return None


def uniswap_identity_reason(tx: dict[str, Any], doc: dict[str, Any], ref: str) -> str | None:
    """Why this transaction is NOT the gateway deposit this `uniswap` quote was issued for — or
    None. The TRANSACTION-level half of the four checks in `routers/deposits.resolve_uniswap`
    (the receipt-level half needs a mined receipt): `from` is the wallet the quote was issued
    to, `to` is OUR router — not the pipe, whose caller on this path is the hook — and the
    calldata carries EXACTLY this quote's reference, decoded, never matched as a substring.

    One implementation, two callers: registration and the watcher's re-verification of a row
    that was opened before any endpoint could see the transaction."""
    if (tx.get("from") or "").lower() != str(doc.get("address") or "").lower():
        return "that transaction was not sent from the wallet this quote was issued to"
    router_address = (settings.uniswap_router or "").lower()
    if not router_address or (tx.get("to") or "").lower() != router_address:
        return "that transaction is not a call to the Pgas router this quote was built for"
    if not uniswap.carries_ref(tx.get("input") or tx.get("data") or "", ref):
        return (
            "that transaction does not carry this quote's deposit reference — register the "
            "transaction you signed for this quote"
        )
    return None


def quote_owns_tx(q: dict[str, Any], tx: dict[str, Any], asset: Asset) -> bool:
    """The STRICT form, for attributing a lock nobody registered: the calldata must be this
    quote's byte for byte.

    Registration may fall back to "the pubkey and the amount are this quote's" because the user
    is standing there with a session; nothing is standing behind a lock that arrived on its own,
    so the whole 394 bytes have to be the ones we handed out."""
    want = expected_send_funds(q)
    if not want:
        return False
    data = str(tx.get("input") or tx.get("data") or "").lower()
    return (
        data == want
        and (tx.get("from") or "").lower() == str(q.get("address") or "").lower()
        and (tx.get("to") or "").lower() == asset.pipe.lower()
    )


# ------------------------------------------- claims: a hash is public, a claim is not a title

# What the loser of a contested hash is told. It names no other account and no other row: the
# only thing it can honestly say is that this claim was never proven.
CLAIM_LOST_NOTE = (
    "that transaction was registered by the wallet that signed it — this claim was not proven "
    "and nothing was credited. If you did send it, register the transaction you signed."
)


async def claims_on(tx_hash: str, exclude_id: str | None = None) -> list[dict[str, Any]]:
    """Every LIVE deposit row that registered this hash, oldest first — the CLAIMS on it.

    ⛔ A transaction hash is public the moment it is broadcast, so posting one is a claim and
    never a title. Since 2026-09-10 a hash no endpoint can see yet opens an unverified row
    instead of being refused — and while `uniq_src_tx_hash` covered every string hash, that made
    the row a title anyway: whoever POSTED first took it, and a stranger watching the mempool
    could lock the person who actually signed the transaction out with a 409 until the TTL.
    So several accounts may hold a claim on one hash, the unique index applies only from
    `verified: true`, and identity — the transaction's own sender, which is not public
    information — is what turns exactly one of them into the owner.
    """
    q: dict[str, Any] = {"src_tx_hash": tx_hash, "status": LIVE}
    if exclude_id:
        q["_id"] = {"$ne": exclude_id}
    return await db().deposits.find(q).sort("created_at", 1).to_list(CLAIM_LIMIT)


async def fail_claims(rows: list[dict[str, Any]], note: str = CLAIM_LOST_NOTE) -> int:
    """Fail unproven claims and GIVE THE HASH BACK. Returns how many actually moved.

    ⚠️ NO PAGE, on purpose (law 15: a monitor that pages about the ordinary trains the operator
    to ignore the pager). Two accounts posting one hash is contention, not an incident: nothing
    was credited, nothing is stuck, and the row itself carries the whole story to the account
    that reads it. What still pages is the case where nobody can be shown to own money that HAS
    landed — `record_unattributed`.

    Every write is conditional on the row still being an unproven claim, so this can never fail
    a row whose identity was proven, nor one whose money has already landed (`status` past
    `submitted` means the scanner matched a lock to it on the lock's own evidence).
    """
    n = 0
    for row in rows:
        res = await db().deposits.update_one(
            {"_id": row["_id"], "status": "submitted", "verified": {"$ne": True}},
            {
                "$set": {
                    "status": FAILED,
                    "updated_at": time.time(),
                    "verified": False,
                    "src_tx_hash_rejected": row.get("src_tx_hash"),
                    "note": note,
                },
                "$unset": {"src_tx_hash": "", "unseen_since": "", "unseen_reason": ""},
            },
        )
        n += int(res.modified_count or 0)
    if n:
        log.warning("failed %d unproven claim(s) on a hash another row owns", n)
    return n


async def direct_row_for_lock(
    tx_hash: str, receipt: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, str]:
    """(the registered `direct` row that OWNS this pipe call, why no claim on it does).

    §IDENTITY-BEATS-BALANCE, at the moment money has landed: a verified row has already been
    proven; an unverified claim is this call's only if the wallet that SENT the call is the
    wallet the row was issued to. Returning a foreign claim here would refuse the lock on
    "sender mismatch" and put real money on the operator's desk while the owner's own quote was
    sitting there, matchable by the 394 bytes of calldata we issued — so a hash claimed only by
    strangers falls through to the quote instead, carrying the reason with it: the operator's
    unattributed row must still SAY that a foreign claim was rejected, not merely omit it.
    """
    rows = [r for r in await claims_on(tx_hash) if xchain.norm_mode(r.get("mode")) == "direct"]
    if not rows:
        return None, ""
    # verified first, then oldest: the proven row is the answer wherever one exists
    rows.sort(key=lambda r: (0 if r.get("verified") else 1, float(r.get("created_at") or 0.0)))
    sender = ((receipt or {}).get("from") or "").lower()
    if not sender:
        # the receipt will not say who sent it — `_direct_sender_ok` refuses below rather than
        # guessing, and an unreadable fact is never resolved by picking one.
        return rows[0], ""
    # Identity first, in two passes, because one hash may now be claimed by several rows: a row
    # that NAMES this sender beats a row from before the address was stored, which can only be
    # checked on its amount (`_direct_sender_ok` skips it). One pass would have handed a lock to
    # whichever of the two happened to sort first.
    for r in rows:
        if r.get("verified") or str(r.get("address") or "").lower() == sender:
            return r, ""
    for r in rows:
        if not r.get("address"):
            return r, ""  # a row written before the address was stored: the amount decides
    return None, (
        f"sender mismatch: {len(rows)} row(s) registered this hash and the pipe call was sent by "
        "none of the wallets they were issued to"
    )


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


async def _block_time(rpc: Any, block: Any, prefer: str | None = None) -> float | None:
    """The block's own timestamp, or None when no endpoint would say. None is "I do not know",
    never "any time" — the caller drops the upper bound and keeps every other check."""
    if rpc is None or block is None:
        return None
    try:
        b = await rpc.call(
            "eth_getBlockByNumber", [hex(int(block)), False], prefer=prefer, pin=prefer is not None
        )
        return float(int(str((b or {})["timestamp"]), 16))
    except Exception:  # noqa: BLE001 — an unreadable timestamp is not evidence of anything
        return None


async def attribute_from_quote(
    m: dict[str, Any], asset: Asset, rpc: Any, prefer: str | None = None
) -> tuple[dict[str, Any] | None, str, str]:
    """A pipe lock to OUR pubkey that no deposit row claims — matched back to the QUOTE whose
    calldata built it.

    ⛔ 2026-09-10, the incident this exists for: two real 0.002 ETH deposits mined and locked
    while POST /v1/deposits was answering 409 "not visible on Ethereum yet" (the first endpoint
    of the pool hides pending transactions), so no row was ever created, both locks landed in
    `unattributed_locks` and the operator was paged to move money by hand. Nothing about that
    lock was unknown: WE issued the 394 bytes of calldata in it.

    So the match is identity, never amount (§IDENTITY-BEATS-BALANCE):
      * `from` is the wallet the quote was issued to,
      * `to` is that asset's pipe,
      * the calldata is this quote's own `sendFunds` BYTE FOR BYTE (`quote_owns_tx`),
      * the quote was created before the block (with a little clock slack) and inside
        `PGAS_QUOTE_ATTRIBUTION_WINDOW_S`,
      * and the lock's amount is the amount that quote was priced at (`_sane`).

    Ambiguity refuses rather than guesses: two accounts whose quotes both match one calldata AND
    one from-address cannot happen (the calldata carries the amount, the pubkey, and the sender
    is in the transaction) — if it ever does, the lock stays on the operator's desk. Two quotes
    of the SAME account matching is ordinary (the user quoted twice, or re-armed), and the
    account is the same either way, so WHICH of them is spent decides nothing about the money —
    but it decides whether the OTHER lock still has a quote left. See §NEAREST-PRECEDING below.
    """
    if rpc is None:
        return None, "", "no Ethereum endpoint was available to read the pipe call with"
    try:
        tx, answered, errors = await ethpipe.visible_tx(rpc, m["tx"])
    except Exception as e:  # noqa: BLE001 — an unreadable read is retried, never concluded from
        return None, "", f"could not read the pipe call ({type(e).__name__}: {e})"
    if tx is None:
        why = "no endpoint has it" if answered else ("; ".join(errors) or "no endpoint answered")
        return None, "", f"the pipe call {m['tx']} could not be read to match it against a quote ({why})"
    d = db()
    bt = await _block_time(rpc, m.get("block"), prefer)
    window = float(settings.quote_attribution_window_s)
    floor = (bt if bt else time.time()) - window
    upper = bt + QUOTE_AFTER_BLOCK_SLACK_S if bt else None
    sender = (tx.get("from") or "").lower()
    matches: list[dict[str, Any]] = []
    attributed_by = "calldata"
    # ── FIRST: the receiver key itself, when we issued a per-deposit one. An index is allocated
    # once and never reused, so the 33 bytes in this lock name exactly one quote — a stronger
    # identity than a calldata search, and one that needs no time window to bound it. It does
    # NOT relax anything: the from / to / calldata checks (`quote_owns_tx`) and the amount check
    # still have to pass, so a stranger copying our calldata into their own call is refused here
    # exactly as it is below. With no per-deposit key ever issued this lookup finds nothing and
    # the search below is the one that ran before this existed.
    keyed = await receiver_keys.quote_for_pk(asset, m["receiver"])
    if keyed is not None:
        if not quote_owns_tx(keyed, tx, asset):
            return (
                None,
                "",
                f"the pipe call from {sender} locks to a receiver key we issued for quote "
                f"{keyed['_id']}, but it is not that quote's own sendFunds from that quote's "
                "wallet — refusing to credit it",
            )
        try:
            why = _sane(keyed, int(keyed["value_units"]), m, asset)
        except (KeyError, TypeError, ValueError) as e:
            why = f"the quote holding this receiver key cannot be read ({type(e).__name__})"
        if why:
            return (
                None,
                "",
                f"the pipe call locks to the receiver key of quote {keyed['_id']}, but {why}",
            )
        matches = [keyed]
        attributed_by = "receiver-key"
    else:
        # §NEAREST-PRECEDING (see the tail of this function): newest first, with the upper bound
        # in the QUERY and not only in the loop — a busy account's quotes issued AFTER this block
        # would otherwise fill the limit and starve the ones that could have built the call.
        at: dict[str, Any] = {"$gte": floor}
        if upper is not None:
            at["$lte"] = upper
        cur = (
            d.quotes.find({"mode": "direct", "asset": asset.key, "at": at})
            .sort("at", -1)
            .limit(QUOTE_CANDIDATE_LIMIT)
        )
        for q in await cur.to_list(QUOTE_CANDIDATE_LIMIT):
            created = quote_created_at(q)
            if created <= 0 or created < floor or (upper is not None and created > upper):
                continue
            if not quote_owns_tx(q, tx, asset):
                continue
            try:
                if _sane(q, int(q["value_units"]), m, asset):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            matches.append(q)
    if not matches:
        return (
            None,
            "",
            f"no quote of ours was armed with the calldata of this pipe call from {sender}",
        )
    accounts = {str(q.get("account_id")) for q in matches}
    if len(accounts) > 1:
        return (
            None,
            "",
            f"{len(accounts)} different accounts hold a quote matching this pipe call — "
            "refusing to guess which one it is",
        )
    owner = next(iter(accounts))
    # A row that already carries this hash blocks a second one — but only a row that could
    # actually OWN it: one that identity has proven (`verified`), or one of this same account.
    # An unverified claim from ANOTHER account is exactly the squat this lock is the victim of;
    # letting it block here would send real money to `unattributed_locks` and the operator's
    # desk, which is the outcome this whole path exists to prevent.
    if await d.deposits.find_one(
        {
            "src_tx_hash": m["tx"],
            "status": LIVE,
            "$or": [{"verified": True}, {"account_id": owner}],
        },
        {"_id": 1},
    ):
        return None, "", f"another deposit row already carries the hash {m['tx']}"
    # §NEAREST-PRECEDING. `matches` is newest first, so the quote spent here is the LAST one
    # issued before this block — the one this call was most probably built from. It used to be
    # the OLDEST, and that is half of the 2026-09-10 msgId-138 strand: one wallet sent two
    # identical direct deposits (lock 138 at 10:40:59Z, lock 140 at 11:38) against six quotes
    # with byte-identical calldata, of which only the 10:40:03Z one predates lock 138's block.
    # `retry_unattributed` reached 140 first, "oldest unused" handed it the ONLY quote 138 could
    # ever have used, and 0.0019999 ETH of a user's money was refused and left to abandonment.
    for q in matches:
        if await d.deposits.find_one({"quote_id": q["_id"], "status": LIVE}, {"_id": 1}):
            continue
        # …and a TERMINAL-FAILED row for that quote does not spend it (see §FAILED above). Its
        # id travels with the match so the new row can NAME it: that is the evidence for why a
        # second row exists for one quote, on the row itself rather than in a log nobody reads.
        dead = await d.deposits.find_one({"quote_id": q["_id"], "status": FAILED}, {"_id": 1})
        return (
            {**q, "_attributed_by": attributed_by, "_superseded": (dead or {}).get("_id")},
            "quote",
            "",
        )
    # ⛔ §A-QUOTE-IS-A-PRICE-STATEMENT-NOT-A-ONE-TIME-TICKET — the other half of that strand.
    # Every matching quote already backs a row, and this REFUSED, which sent a lock whose 394
    # bytes we issued, from a wallet we issued them to, for an amount we priced, to the
    # operator's desk. But a quote does not fund a deposit; it PRICES one. A user who sends the
    # same call twice has made two real deposits, and the second one is money already burned on
    # Ethereum: refusing it strands it, it does not protect anything.
    #
    # What keeps two deposits from becoming one credit is the TRANSACTION, not the quote:
    # `_seen` skips a lock a row already carries, the gate above refuses a hash a row of this
    # account already owns, and `uniq_src_tx_hash` refuses two verified rows on one hash. So the
    # nearest-preceding quote prices this lock too, on a row of its own that says so and names
    # the row it shares the quote with.
    reuse = matches[0]
    other = await d.deposits.find_one({"quote_id": reuse["_id"], "status": LIVE}, {"_id": 1})
    return (
        {
            **reuse,
            "_attributed_by": attributed_by,
            "_reused": str((other or {}).get("_id") or ""),
        },
        "quote",
        "",
    )


# The two ways `attribute_from_quote` can prove a lock is a given quote's. BOTH check the same
# three facts (sender, pipe, this quote's own sendFunds byte for byte); `receiver-key` adds the
# 33 bytes we issued to that one quote and to nothing else, which is why it is matched first.
# Anything else — a cross-chain order id, a metadata tag, a hook reference — is a HINT and
# cannot vouch for the transaction's identity by itself.
PROVEN_BY = {
    "calldata": (
        "quote-calldata",
        "attributed from the quote — the pipe call carries this quote's own sendFunds calldata "
        "and came from the wallet the quote was issued to; it was never registered from the site",
    ),
    "receiver-key": (
        "quote-receiver-key",
        "attributed by receiver key — the pipe call locks to the Beam receiver key issued to "
        "this quote alone, and carries this quote's own sendFunds calldata from the wallet it "
        "was issued to; it was never registered from the site",
    ),
}


def _by_calldata(doc: dict[str, Any], how: str) -> bool:
    """Was this quote matched by something that proves the TRANSACTION is ours — the calldata we
    issued, or the receiver key we issued — rather than by a cross-chain order id, a metadata
    tag or a hook reference?"""
    return how == "quote" and doc.get("_attributed_by") in PROVEN_BY


def reused_note(other_id: str | None) -> str:
    """Why a second deposit row legitimately exists for ONE quote — the ONE sentence both
    writers of that fact use (the scanner's attribution here, and POST /v1/deposits when the
    user registers the second hash themselves). Law 9: two implementations of one fact will
    disagree, and one of them reaches money."""
    return (
        "a quote is a PRICE statement, not a one-time ticket: this is a second deposit priced "
        "by the same quote"
        + (f", and the row {other_id} carries the other one" if other_id else "")
    )


def from_quote(doc: dict[str, Any], how: str) -> dict[str, Any] | None:
    """What a deposit row created from a PROVEN quote match knows that the ordinary
    scanner-from-quote row does not: the source transaction is the pipe call in front of us, and
    its identity is PROVEN — same sender, same pipe, the same 394 bytes we handed out, and (on
    the receiver-key path) the 33 bytes only this quote was ever given."""
    if not _by_calldata(doc, how):
        return None
    dead = doc.get("_superseded")
    reused = doc.get("_reused")
    verified_by, note = PROVEN_BY[str(doc.get("_attributed_by"))]
    out = {
        "verified": True,
        "verified_at": time.time(),
        "verified_by": verified_by,
        "note": note
        + (
            # why a second row exists for one quote, named on the row (never edit history)
            f" (the earlier row {dead} for this quote had already failed and released "
            "everything it claimed)"
            if dead
            else ""
        )
        + (f" — {reused_note(reused)}" if reused is not None else ""),
    }
    if reused is not None:
        # §A-QUOTE-IS-A-PRICE-STATEMENT. Both facts on the row, because the row is where an
        # operator asks "why are there two deposits for one quote?" and gets an answer.
        out |= {"quote_reused": True, "quote_reused_of": reused or None}
    return out


async def attribute(
    m: dict[str, Any],
    asset: Asset,
    fulfilled: list[dict[str, Any]],
    retries: int = LITE_RETRIES,
    receipt: dict[str, Any] | None = None,
    rpc: Any = None,
    prefer: str | None = None,
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
        # …the row that OWNS this hash out of everyone who claimed it (several accounts may
        # have registered it; only the sender's own row is this pipe call's).
        dep, claim_why = await direct_row_for_lock((m.get("tx") or "").lower(), receipt)
        if dep:
            why = _direct_sender_ok(dep, receipt) or _sane(
                dep, int(dep["eth"]["value_units"]), m, asset
            )
            return (None, "deposit", why) if why else (dep, "deposit", "")
        #   * …and last, a `direct` deposit NOBODY REGISTERED. The calldata in the pipe call is
        #     ours — we issued it — so the quote it was built from is the identity, and a user
        #     whose registration never went through must still be credited.
        q, how, why = await attribute_from_quote(m, asset, rpc, prefer)
        if q is not None:
            return q, how, ""
        return (
            None,
            "",
            f"no FulfilledOrder log in the receipt (not a cross-chain fill), "
            f"{claim_why or 'no deposit row registered this hash'}, and {why}",
        )
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
    created: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Claim-first: only a deposit in a claimable state moves to `locked`. A quote without a
    deposit row (the user signed but never registered the hash) gets its deposit created here;
    `created` carries whatever the path that found the quote can prove about it."""
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
        # LIVE only, for the same reason the attribution gate is (§FAILED): a terminal-failed row
        # released everything it claimed, so it can neither be moved to `locked` nor stop this
        # quote's real deposit from being opened.
        by_quote: dict[str, Any] = {"quote_id": doc["_id"], "status": LIVE}
        if doc.get("_reused") is not None:
            # ⛔ …and when this lock is a SECOND deposit priced by ONE quote
            # (§A-QUOTE-IS-A-PRICE-STATEMENT), the row that quote already has belongs to the
            # OTHER transaction. Moving THAT row to `locked` here would hand this lock to a
            # deposit that has already been paid for by different money — so only a row already
            # carrying THIS transaction may be reused, and otherwise this lock opens its own.
            by_quote["src_tx_hash"] = m["tx"]
        dep = await d.deposits.find_one(by_quote)
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
                    # ⛔ ONLY where the source transaction and the lock are the SAME transaction.
                    # A `direct` deposit IS the pipe call and a `uniswap` one reaches the pipe
                    # through our hook inside the user's own swap, so the lock's tx is theirs.
                    # A cross-chain deposit's lock is the SOLVER's fill — writing that hash here
                    # would hand the user's row a transaction they never signed.
                    "src_tx_hash": (
                        m["tx"] if mode in ("direct", uniswap.MODE) else None
                    ),
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
                    # the receiver key this quote was armed with, carried onto the row exactly
                    # as `pubkey` is: the CLAIM signs with the row's index, and a row that lost
                    # it would try to sign with the legacy blob for a message delivered to an
                    # indexed key — a claim that can never succeed, on money already burned.
                    **receiver_keys.carry(doc),
                    "created_at": now,
                    "updated_at": now,
                    "note": "created by the scanner from the quote (lock seen before the hash was registered)",
                    **(created or {}),
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
    # `prefer` orders the pool, it no longer PINS it: the endpoint that vouched for the log is
    # asked first, and every other endpoint is asked too. A receipt is identified by its hash,
    # not by a block range, so one endpoint's `null` is not the chain's answer (see
    # `ethpipe.Rpc.receipt`) — and this call raising rather than returning None keeps a chunk
    # nobody could read from being checkpointed as read.
    receipt = await rpc.receipt(m["tx"], prefer=prefer)
    if not receipt:
        raise ethpipe.RpcError(f"receipt {m['tx']} not available yet — chunk will be retried")
    fulfilled = find_fulfilled_in_receipt(receipt)
    doc, how, reason = await attribute(
        m, asset, fulfilled, retries=retries, receipt=receipt, rpc=rpc, prefer=prefer
    )
    if doc is not None:
        claimed = await lock_deposit(doc, how, m, asset, fulfilled, created=from_quote(doc, how))
        if claimed:
            how_note = ""
            if _by_calldata(doc, how):
                how_note = (
                    " (attributed from the quote by receiver key)"
                    if doc.get("_attributed_by") == "receiver-key"
                    else " (attributed from the quote)"
                )
            await tg.queue(
                "deposit_locked",
                f"Deposit locked in the {asset.key} pipe, msg {m['msg_id']}{how_note}",
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
    last = str(row.get("last_reason") or "")
    await tg.alert(
        "lock_abandoned",
        f"MANUAL HANDLING REQUIRED: a {row.get('asset')} lock is still unattributed after "
        f"{int(row.get('tries') or 0)} attempts ({why}) — automatic retries have stopped and "
        "nobody has been credited"
        # what the LAST attempt actually refused it for; before `_retry_failed` existed the page
        # could only quote the age, and the row still carried the very first pass's sentence.
        + (f". Last refusal: {last}" if last else ""),
        lock=row["_id"],
    )


async def _retry_failed(row: dict[str, Any], tries: int, why: str) -> None:
    """⛔ EVERY DECISION PATH WRITES A ROW (law 12), and this one wrote nothing at all. A
    re-attribution attempt that attributed nothing returned False — no field, no log line — so a
    lock could be refused twelve times and abandoned while `reason` still held the sentence from
    its very first pass, and the operator paged about msgId 138 had no way to see WHY the retries
    kept failing. One helper, called from the one place an attempt can fail (law 14)."""
    await db().unattributed_locks.update_one(
        {"_id": row["_id"]},
        {"$set": {"last_reason": why[:500], "last_try_at": time.time()}},
    )
    log.info(
        "unattributed lock %s (%s msgId %s): attempt %d did not attribute it — %s",
        row["_id"],
        row.get("asset"),
        row.get("msg_id"),
        tries,
        why,
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
            ok, why = await _retry_one(row, rpc)
            if ok:
                n += 1
            else:
                await _retry_failed(row, tries, why)
        if len(rows) < page:
            break
    return n


async def _retry_one(row: dict[str, Any], rpc: ethpipe.Rpc) -> tuple[bool, str]:
    """One re-attribution attempt → `(became a locked deposit, why it did not)`.

    ⛔ EVERY failure path here returns a SENTENCE, and `_retry_failed` writes it (law 12). This
    used to be a bare `bool`: "no such asset", "no endpoint answered", "no receipt yet", "no
    quote matched" and "the row is not claimable" were one indistinguishable False, discarded by
    the caller — including the reason `attribute` had already computed. That is why msgId 138
    could be refused every pass with nothing anywhere saying what for."""
    d = db()
    asset = ASSETS.get(row["asset"])
    if asset is None:
        return False, f"unknown asset {row.get('asset')!r} — there is no pipe to attribute it to"
    try:
        receipt = await rpc.receipt(row["tx"])
    except Exception as e:  # noqa: BLE001 — an unreadable receipt is a WAIT, never a verdict
        return False, f"the receipt could not be read ({type(e).__name__}: {e})"
    if not receipt:
        return False, "no endpoint that answered has a receipt for this transaction yet"
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
    doc, how, why = await attribute(m, asset, fulfilled, retries=1, receipt=receipt, rpc=rpc)
    if doc is None:
        return False, why or "nothing could be attributed to this lock"
    claimed = await lock_deposit(doc, how, m, asset, fulfilled, created=from_quote(doc, how))
    if not claimed:
        return False, f"matched deposit {doc['_id']} but it is not in a claimable state"
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
    return True, ""
