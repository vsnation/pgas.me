"""One Beam receiver key per deposit — the allocator, the key cache, and the derivations that
read them.

The shipped pipe app derives exactly ONE receiver pubkey per (wallet seed, pipe cid), so every
deposit we have ever taken names the same 33 bytes on Ethereum. The patched app (WO-20260910-3,
K1) accepts an `index` and derives `KeyID{cid, index}` instead — a different receiver key per
deposit, signed for by the same wallet, claimed into the same treasury, booked through the same
BeamPay ledger. Nothing about the money path changes; only which 33 bytes go into `sendFunds`.

    invoke_contract role=user    action=get_pk   cid=<cid> [index=N]  → {"pk": hex33, "index": N}
    invoke_contract role=user    action=receive  cid=<cid> msgId=<n> [index=N]
    invoke_contract role=manager action=view_incoming cid=<cid> [indexes=1;2;3] [maxIndex=N]

⚠️ THE LIST SEPARATOR IS `;`, NOT `,`. The wallet splits the whole `args` string on `,`
(`ProcessorManager::AddArgs`, bvm2.cpp:3373) before the shader sees a value, so a comma list
truncates to its first entry in silence — and an index nobody asked about is a message nobody
can see. `beam.INDEX_LIST_SEP` is the one place it is spelled. Index 0 (legacy) is always in the
shader's match set; we pass it anyway, so the call says what it means. And with NEITHER
`indexes` nor `maxIndex` the shader derives a default window of 1..64 — so an index above that
is invisible to a plain call, which is why `open_indexes` sends the exact set instead of
trusting the default. A `receive` whose index does not match the message's stored receiver is
REFUSED before signing (no `raw_data`); that refusal is never walked past by trying another
index — the right index is the one on the row.

⛔ **THE FLAG GATES ISSUANCE, NOT RECOGNITION.** `PGAS_RECEIVER_KEY_PER_DEPOSIT` decides whether
a NEW quote is armed with an indexed key. Everything downstream — which pubkeys the scanner
knows, which quote a lock belongs to, which indexes `view_incoming` is asked about, which index
the claim signs with — is driven by the DATA (a `receiver_keys` row, a quote's `receiver_pk`, a
deposit's `receiver_index`), never by the flag. Turning the flag off must not strand a deposit
that is already on its way to a key we issued yesterday, and money on an unrecognised key has
no refund path: `EthPipe.sol` validates only `length == 33` and the relayer forwards it
verbatim. With the flag never turned on there are no rows, so every read below is empty and the
behaviour is exactly what it was before this module existed.

⛔ **AN INDEX IS NEVER REUSED.** `next_receiver_index` is one atomic `$inc` on `counters`, per
pipe, starting at 1 — index 0 is the LEGACY key (the cid-only blob, `settings.pubkey_for`) and
is never allocated. A read-then-write allocator hands two quotes the same key, which is two
deposits landing on one receiver: the anonymity the whole change buys, spent, and two locks that
attribution can no longer tell apart. Gaps are fine and expected — an index whose `get_pk` was
unreadable is burned, never retried.

⛔ **AN UNREADABLE `get_pk` IS NOT A KEY.** It raises, and the quote route turns it into a 503.
Falling back to the legacy key would hand the user calldata that quietly undoes the change and
tell nobody; falling back to a *guessed* key would mint value nobody can claim, for ever.

⛔ **THE WALLET MUST NOT ANSWER THE LEGACY KEY FOR AN INDEX.** The SHIPPED app ignores `index=`
entirely and answers the cid-derived key for any value of it (measured on the box, T41 §1). So
"we asked for index 7 and got 33 bytes back" is not evidence that the patched app is deployed —
the one thing that distinguishes them is that the answer DIFFERS from the legacy key. A pk that
equals it is refused here, and that refusal is what stops an unpatched box from issuing N quotes
that all share one receiver while every row claims they do not.

One writer per fact:
  * `counters`      — this module's `next_receiver_index`, nothing else ever writes it.
  * `receiver_keys` — the KEY CACHE: `{_id: "<cid>:<index>", pipe_cid, index, pk, issued_at,
                      quote_id}`, written once by `pk_for_index` (`$setOnInsert`, so a race
                      keeps ONE value) and never edited afterwards except `quote_id`, which is
                      evidence and is nobody's authority for anything.
  * the QUOTE row   — the authority on which quote holds a key (`receiver_index`,
                      `receiver_pk`). Attribution reads THAT, because it is also the row whose
                      `hook_calldata` the from/to/calldata checks are made against.
  * the DEPOSIT row — `receiver_index`, copied from the quote by whichever writer creates it,
                      exactly as `pubkey` already is. The claim signs with the row's index.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pymongo import ReturnDocument

from . import beam
from .assets import Asset
from .config import settings
from .db import db

log = logging.getLogger("pgasme.receiver_keys")

# Index 0 is the legacy blob (cid only) — the key every deposit before this change landed on.
# It is never allocated, and it is what a row with no index means.
LEGACY_INDEX = 0
# The shader derives a BOUNDED window of indexes per `view_incoming` (default 64 per order.md),
# so the API may never ask about more than that at once.
MAX_VIEW_INDEXES = 64
# How many issued keys the scanner's membership set may carry. The set is also bounded in TIME
# by PGAS_QUOTE_ATTRIBUTION_WINDOW_S; this is the second bound, so a burst of quotes cannot make
# every scanner pass walk an unbounded list.
MAX_KNOWN_KEYS = 1000
PK_HEX_CHARS = 66  # 33 bytes


def enabled() -> bool:
    """Does a NEW quote get its own receiver key? Reading an existing one never asks."""
    return bool(settings.receiver_key_per_deposit)


def _counter_id(pipe_cid: str) -> str:
    return f"receiver_index:{pipe_cid}"


def _key_id(pipe_cid: str, index: int) -> str:
    return f"{pipe_cid}:{int(index)}"


def _clean_pk(pk: object) -> str:
    """A 33-byte pubkey, lowercased and 0x-stripped. Anything else RAISES — a malformed receiver
    is calldata the pipe accepts (it checks only the length) and value nobody can ever claim."""
    s = str(pk or "").strip().lower().removeprefix("0x")
    if len(s) != PK_HEX_CHARS:
        raise beam.BeamError(f"a receiver pubkey must be {PK_HEX_CHARS} hex chars, got {len(s)}")
    try:
        bytes.fromhex(s)
    except ValueError as e:
        raise beam.BeamError("a receiver pubkey must be hex") from e
    return s


async def next_receiver_index(pipe_cid: str) -> int:
    """The next receiver index for one pipe — ONE atomic `$inc`, never a read then a write.

    Starts at 1 (0 is the legacy key) and is never reused, whatever happens to the value it was
    allocated for. Two quotes sharing an index share a receiver, and two deposits on one
    receiver are exactly the state this whole change exists to end."""
    if not pipe_cid:
        raise ValueError("next_receiver_index needs a pipe cid")
    row = await db().counters.find_one_and_update(
        {"_id": _counter_id(pipe_cid)},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    n = int((row or {}).get("seq") or 0)
    if n < 1:
        raise beam.BeamError(f"the receiver-index counter for {pipe_cid[:12]}… answered {n!r}")
    return n


async def pk_for_index(asset: Asset, index: int) -> str:
    """The receiver pubkey for (this pipe, this index), read from the wallet ONCE and cached.

    RAISES rather than returning anything on an unreadable wallet, on a malformed answer, and —
    the one that matters most — on an answer that equals the pipe's LEGACY key, which is what an
    unpatched pipe app answers for every index there is."""
    n = int(index)
    if n <= LEGACY_INDEX:
        raise ValueError(f"index {n} is the legacy key, not an issued one")
    cid = asset.beam_cid
    _id = _key_id(cid, n)
    d = db()
    if (row := await d.receiver_keys.find_one({"_id": _id})) and row.get("pk"):
        return str(row["pk"])
    pk = _clean_pk(await beam.wallet().get_pk(cid, n))
    if legacy := (settings.pubkey_for(asset.key) or "").strip().lower().removeprefix("0x"):
        if pk == legacy:
            raise beam.BeamError(
                f"the wallet answered the LEGACY receiver key for {asset.key} index {n} — the "
                "patched pipe app is not the one being called; refusing to issue a key that is "
                "not per-deposit"
            )
    await d.receiver_keys.update_one(
        {"_id": _id},
        {
            "$setOnInsert": {
                "pipe_cid": cid,
                "asset": asset.key,
                "index": n,
                "pk": pk,
                "issued_at": time.time(),
                "quote_id": None,
            }
        },
        upsert=True,
    )
    stored = await d.receiver_keys.find_one({"_id": _id})
    return str((stored or {}).get("pk") or pk)


async def issue(asset: Asset) -> tuple[int, str]:
    """Allocate the next index on this pipe and read its pubkey. (index, pk).

    The index is consumed the instant it is allocated: if `get_pk` then fails, that index is
    burned and the caller refuses. Reusing it would mean two quotes could be issued the same
    receiver on two different days."""
    index = await next_receiver_index(asset.beam_cid)
    pk = await pk_for_index(asset, index)
    log.info("issued receiver key index %d on the %s pipe", index, asset.key)
    return index, pk


async def bind(asset: Asset, index: int, quote_id: str) -> None:
    """Record which quote an issued key went to. EVIDENCE ONLY — the quote row is the authority
    (it carries `receiver_pk`, and it is the row the calldata check is made against)."""
    if int(index) <= LEGACY_INDEX or not quote_id:
        return
    await db().receiver_keys.update_one(
        {"_id": _key_id(asset.beam_cid, int(index))}, {"$set": {"quote_id": str(quote_id)}}
    )


def stored_key(q: dict[str, Any]) -> tuple[int, str] | None:
    """The indexed key this quote was armed with, or None for a legacy one."""
    index = int(q.get("receiver_index") or 0)
    pk = str(q.get("receiver_pk") or "")
    return (index, pk) if index > LEGACY_INDEX and pk else None


def quote_fields(index: int, pk: str) -> dict[str, Any]:
    """What a quote row records about its key — NOTHING when the key is the legacy one, so a
    document written with the flag off is byte-identical to the one written before this change."""
    return {"receiver_index": int(index), "receiver_pk": pk} if int(index) > LEGACY_INDEX else {}


def carry(q: dict[str, Any]) -> dict[str, Any]:
    """The key fields to copy from a quote onto the deposit row it produces — empty for a
    legacy quote, so a row written with the flag off is the row that was written before."""
    stored = stored_key(q)
    return quote_fields(*stored) if stored else {}


async def for_quote(asset: Asset, q: dict[str, Any]) -> tuple[int, str]:
    """The receiver key THIS quote is armed with — allocated at most once, ever, then reused.

    /arm is called again on every retry, every double-click and every re-quote of a cross-chain
    order, and EVERY order a quote was ever armed with stays registrable. A second key per quote
    would mean the user could sign calldata naming a receiver the quote no longer records, which
    is a lock the scanner attributes to nobody and a claim nobody can sign for."""
    if stored := stored_key(q):
        return stored
    if not enabled():
        return LEGACY_INDEX, settings.pubkey_for(asset.key)
    index, pk = await issue(asset)
    won = await db().quotes.find_one_and_update(
        {"_id": q["_id"], "receiver_index": {"$exists": False}},
        {"$set": quote_fields(index, pk)},
        return_document=ReturnDocument.AFTER,
    )
    if won is None:
        # another request armed this quote first; its key is the quote's key and ours is burned
        again = await db().quotes.find_one({"_id": q["_id"]})
        if theirs := stored_key(again or {}):
            log.info("quote %s already holds receiver index %d; %d is burned",
                     q["_id"], theirs[0], index)
            return theirs
    await bind(asset, index, q["_id"])
    return index, pk


async def issued_pks(asset: Asset) -> set[str]:
    """Every receiver key issued on this pipe inside the attribution window.

    ⚠️ BOUNDED IN TIME AND IN COUNT, and both bounds have a cost: a key issued longer ago than
    `PGAS_QUOTE_ATTRIBUTION_WINDOW_S` whose deposit never opened a row drops out of the
    scanner's membership set, and a lock to it is then not merely unattributed but UNSEEN. What
    keeps a real deposit safe is the other half of `scanner.known_pubkeys`: a live deposit row's
    own pubkey stays known however old it is."""
    floor = time.time() - float(settings.quote_attribution_window_s)
    cur = (
        db().receiver_keys.find({"pipe_cid": asset.beam_cid, "issued_at": {"$gte": floor}})
        .sort("issued_at", -1)
        .limit(MAX_KNOWN_KEYS)
    )
    return {str(r["pk"]).lower() for r in await cur.to_list(MAX_KNOWN_KEYS) if r.get("pk")}


async def quote_for_pk(asset: Asset, pk: str) -> dict[str, Any] | None:
    """The quote we issued this receiver key to — the strongest identity a pipe lock can carry.

    An index is never reused, so exactly one quote can hold a key. TWO would be a defect in the
    allocator, and a defect in the allocator is not something to guess past: it refuses, the
    lock goes to the operator's desk, and nobody is credited on a coin flip."""
    want = str(pk or "").strip().lower().removeprefix("0x")
    if not want:
        return None
    rows = await db().quotes.find({"asset": asset.key, "receiver_pk": want}).limit(2).to_list(2)
    if len(rows) > 1:
        log.error(
            "%d quotes carry the receiver key ending %s — refusing to attribute either",
            len(rows), want[-8:],
        )
        return None
    return rows[0] if rows else None


async def open_indexes(asset: Asset) -> list[int]:
    """The exact set of receiver indexes `view_incoming` has to be asked about — plus legacy.

    The patched `view_incoming` derives a bounded WINDOW of indexes and matches messages against
    it, so a message delivered to an index outside the set is not "not delivered yet", it is
    INVISIBLE — and a deposit whose message is invisible waits for ever with the money already
    burned on Ethereum. Two sources, because neither alone is complete:

      (a) every key issued inside the attribution window — its deposit may still be in transit,
          and it may have no row at all yet (the lock can land before registration does);
      (b) every index on a deposit of this asset that is NOT claimed yet, however old it is —
          a transit slower than the window, or a claim held on a fee floor, must not fall out.

    Empty means "we know of no indexed key on this pipe", and the caller then asks exactly the
    question it asked before this change existed."""
    d = db()
    idx: set[int] = set()
    floor = time.time() - float(settings.quote_attribution_window_s)
    cur = (
        d.receiver_keys.find({"pipe_cid": asset.beam_cid, "issued_at": {"$gte": floor}})
        .sort("issued_at", -1)
        .limit(MAX_VIEW_INDEXES)
    )
    for row in await cur.to_list(MAX_VIEW_INDEXES):
        if (n := int(row.get("index") or 0)) > LEGACY_INDEX:
            idx.add(n)
    for raw in await d.deposits.distinct(
        "receiver_index",
        {"asset": asset.key, "status": {"$ne": "failed"}, "treasury": {"$in": [None, "claiming"]}},
    ):
        try:
            n = int(raw or 0)
        except (TypeError, ValueError):
            continue
        if n > LEGACY_INDEX:
            idx.add(n)
    if not idx:
        return []
    # newest first, because the newest keys are the ones a delivery is most likely to be on, and
    # the window the shader derives is what the tail of this list would fall off
    keep = sorted(idx, reverse=True)[: MAX_VIEW_INDEXES - 1]
    if len(idx) > len(keep):
        log.warning(
            "%s: %d open receiver indexes, asking view_incoming about the newest %d + legacy",
            asset.key, len(idx), len(keep),
        )
    return [LEGACY_INDEX, *sorted(keep)]


def claim_index(dep: dict[str, Any]) -> int | None:
    """The index the claim must sign with — None for a legacy row, whose `receive` carries no
    index at all and is byte-identical to every claim made before this change."""
    try:
        n = int(dep.get("receiver_index") or 0)
    except (TypeError, ValueError):
        return None
    return n if n > LEGACY_INDEX else None
