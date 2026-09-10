"""The INSTANT payout distributor — an Ethereum EOA that pays a user directly, in one block.

The direct payout path burns bETH into the pipe and waits for the bridge relayer (~66 min,
tail hours). The instant path spends ETH we are already holding on Ethereum: one plain
21,000-gas transfer, signed here, from a key this process reads out of a 0600 file at first use
and never writes down again. Two venues, one direction, no bridge.

⛔ **THE PRIVACY TRADE IS EXPLICIT.** Every instant payout leaves the same address, so instant
payouts are linkable TO EACH OTHER. What stays broken is the link that matters: the deposit went
into Beam and the payout comes out of a float that was funded separately, so nothing on
Ethereum connects a user's deposit to their withdrawal. The UI says so before the user picks
the mode.

What this module owns, and nothing else does
--------------------------------------------
  * **The key.** `signer()` reads `PGAS_DISTRIBUTOR_KEY_FILE`, refuses it unless the mode is
    0600 (group/other bits clear), derives the address, and keeps the key inside a `Signer`
    whose `repr` cannot leak it. The key is never an argument, never a log line, never a page —
    a secret passed as a command-line argument leaks through `ps`, and a secret in an exception
    message leaks into a transcript.
  * **The gas price**, read from `eth_feeHistory` on the pool. Deliberately NOT
    `beam.max_gas_price_gwei`: that is the b2e RELAYER's own arithmetic, ported number for
    number from their code, and it must stay free to track theirs. This is what OUR 21,000-gas
    transfer costs, and the two facts have two consumers.
  * **The nonce**, whose writer of record is the `distributors` row and whose sanity check is
    the chain. One conditional update reserves it (`reserve_nonce`), so two passes cannot sign
    over each other and no gap can stall the account.
  * **The bytes.** `sign_transfer` is the only signature in the system, and a caller that
    already holds signed bytes must re-broadcast THOSE — never ask for new ones. One nonce
    means at most one fill; two signatures over one inventory is how money is lost twice.
  * **The verdict.** `verify_receipt` answers mined / not mined / not ours, and RAISES
    `Unreadable` when no endpoint answered. An unreadable query is not evidence of anything.

Every read here raises `Unreadable` rather than returning a zero: a float we could not read is
not an empty float, and a nonce we could not read is not nonce 0.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterable
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address
from pymongo.errors import DuplicateKeyError

from . import ethpipe
from .config import settings
from .db import db

log = logging.getLogger("pgasme.distributor")

# A key file may be group- or world-readable by nobody. 0600 exactly (0400 is fine too); any
# bit in this mask and the file is refused — the key is presumed compromised, not "probably ok".
UNSAFE_MODE_BITS = 0o077
# What the key looks like inside the file. Either `<NAME>=<hex>` (the env-file shape the box's
# key ceremony writes) or the bare hex on its own. 32 bytes, with or without the 0x.
_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(0[xX])?([0-9a-fA-F]{64})\s*$")
_BARE_KEY = re.compile(r"^\s*(0[xX])?([0-9a-fA-F]{64})\s*$")
# …and the name it is expected to carry. Anything ending in KEY is accepted so a ceremony that
# spelled it differently still loads, but a file with several is refused rather than guessed at.
_KEY_NAME = re.compile(r"KEY$")

# eth_feeHistory, the same shape the relayer-fee reader uses: N blocks back, the 50th-percentile
# tip of each. The answer carries ONE MORE baseFeePerGas than blocks asked for and the extra one
# is the block being built now — which is the block we are trying to get into.
FEE_HISTORY_BLOCKS = 10
TIP_PERCENTILE = 50
# The tip is clamped: a chain that reports no tips at all must not produce a zero-tip
# transaction nobody will mine, and a single 300-gwei block must not price our next transfer.
MIN_TIP_WEI = 10_000_000  # 0.01 gwei
MAX_TIP_WEI = 3_000_000_000  # 3 gwei
BASE_FEE_MULTIPLIER = 2  # room for two full base-fee rises before the transaction goes stale

# `distributors.state` — the SAME field `routers/stats.py` counts active distributors on. One
# spelling, or /v1/stats reports zero for ever while the money moves.
ACTIVE = "active"
DRAINING = "draining"
RETIRED = "retired"

# what eth_sendRawTransaction can tell us, in the four flavours the caller has to act on
ACCEPTED = "accepted"  # an endpoint took the bytes
KNOWN = "already_known"  # …because it already had them: the same thing, said differently
NONCE_TAKEN = "nonce_taken"  # this nonce is spoken for. NOT a verdict — resolve it by receipt
REFUSED = "refused"  # nobody took it and nobody said why in a way we can act on

# A node that already holds these exact bytes. Acceptance, not failure — re-broadcasting is
# supposed to hit this, every time, for as long as the transaction sits in a mempool.
KNOWN_HINTS = (
    "already known",
    "known transaction",
    "alreadyknown",
    "already exists",
    "already imported",
    "duplicate transaction",
    "transaction already in the pool",
)
# The nonce is spoken for — by our own mined transaction, or by somebody else's. Which of the
# two it is is decided by reading the receipt, never by reading this sentence.
NONCE_HINTS = (
    "nonce too low",
    "nonce is too low",
    "oldnonce",
    "replacement transaction underpriced",
    "already have transaction with same nonce",
)


class DistributorError(RuntimeError):
    """Anything this module refuses to do. Never carries key material."""


class KeyFileError(DistributorError):
    """The key file is missing, readable by somebody else, or does not hold one key."""


class Unreadable(DistributorError):
    """No endpoint answered. ⛔ NOT "the answer was zero" — the caller must hold, never conclude."""


# ----------------------------------------------------------------------------- the key


class Signer:
    """The distributor's key, alive only inside this process.

    ⛔ `__repr__` and `__str__` are overridden and `__slots__` closes the object: an exception
    handler that formats this object, a `log.info("%s", signer)`, or a page built from a
    traceback must be incapable of printing the key. That is not politeness — a worker on this
    project put a secret into a transcript while proving no secret travelled."""

    __slots__ = ("_key", "address", "path")

    def __init__(self, key: bytes, path: str) -> None:
        self._key = key
        self.address: str = to_checksum_address(Account.from_key(key).address)
        self.path = path

    def __repr__(self) -> str:
        return f"<Signer {self.address} from {self.path}>"

    __str__ = __repr__

    def sign(self, tx: dict[str, Any]) -> tuple[str, str]:
        """(raw hex, transaction hash). Deterministic: the same fields sign to the same bytes."""
        signed = Account.sign_transaction(tx, self._key)
        raw = signed.raw_transaction
        return "0x" + bytes(raw).hex(), "0x" + bytes(signed.hash).hex()


_SIGNER: dict[str, Any] = {"signer": None, "path": None}


def reset() -> None:
    """Forget the loaded key (a rotated file, or a test). The next call re-reads and re-checks."""
    _SIGNER["signer"] = None
    _SIGNER["path"] = None


def key_path() -> str:
    return str(settings.distributor_key_file or "").strip()


def configured() -> bool:
    """Is a key file NAMED? Not "does it load" — `signer()` answers that, and it raises."""
    return bool(key_path())


def _read_key(path: str) -> bytes:
    """The 32 bytes in `path`, with the mode checked FIRST. Nothing here ever returns, logs or
    raises the key itself: the messages name the path and the mode, which are not secrets."""
    try:
        st = os.stat(path)
    except OSError as e:
        raise KeyFileError(f"the distributor key file {path!r} could not be read: {type(e).__name__}") from e
    mode = st.st_mode & 0o777
    if mode & UNSAFE_MODE_BITS:
        raise KeyFileError(
            f"the distributor key file {path!r} is mode {mode:04o} — a key any other account "
            f"can read is not a key. chmod 600 it and rotate it"
        )
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise KeyFileError(f"the distributor key file {path!r} could not be opened: {type(e).__name__}") from e
    found: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KEY_LINE.match(line)
        if m and _KEY_NAME.search(m.group(1).upper()):
            found.append(m.group(3))
            continue
        m = _BARE_KEY.match(line)
        if m:
            found.append(m.group(2))
    if not found:
        raise KeyFileError(
            f"the distributor key file {path!r} holds no 32-byte private key "
            f"(expected a single `…KEY=<64 hex>` line)"
        )
    if len(found) > 1:
        raise KeyFileError(
            f"the distributor key file {path!r} holds {len(found)} keys — refusing to guess "
            f"which one signs"
        )
    return bytes.fromhex(found[0])


def signer(path: str | None = None) -> Signer:
    """The process's signer, loaded once per path and re-checked whenever the path changes.

    RAISES KeyFileError — a caller that cannot load the key must HOLD the order and say so, not
    fall back to anything."""
    want = (path or key_path()).strip()
    if not want:
        raise KeyFileError("PGAS_DISTRIBUTOR_KEY_FILE is not set — there is no distributor")
    cached = _SIGNER["signer"]
    if cached is not None and _SIGNER["path"] == want:
        return cached  # type: ignore[return-value]
    s = Signer(_read_key(want), want)
    # ⛔ THE ONE ADDRESS THIS KEY MUST NOT BE. A pipe is a contract that mints on Beam from
    # what it receives; a key that derives it is a configuration accident, and the first thing
    # it would do is pay a user out of the bridge's own balance.
    for pipe in {a.pipe.lower() for a in _pipes()}:
        if s.address.lower() == pipe:
            raise KeyFileError(
                f"the key in {want!r} derives the {s.address} pipe address — that is the "
                f"bridge's contract, not a distributor"
            )
    _SIGNER["signer"], _SIGNER["path"] = s, want
    return s


def _pipes() -> Any:
    from .assets import ASSETS

    return ASSETS.values()


def address() -> str:
    """The distributor's address, or "" when no key file is configured. RAISES KeyFileError when
    one IS configured and cannot be loaded — a misconfigured key must never read as "no key"."""
    if not configured():
        return ""
    return signer().address


# ----------------------------------------------------------------------------- the chain reads


def _int(v: Any) -> int:
    if v is None:
        raise Unreadable("the endpoint answered nothing where a number was expected")
    if isinstance(v, str):
        return int(v, 16) if v.startswith("0x") else int(v)
    return int(v)


async def _call(rpc: Any, method: str, params: list[Any]) -> Any:
    try:
        return await rpc.call(method, params)
    except ethpipe.RpcError as e:
        raise Unreadable(f"{method}: {str(e)[:180]}") from e


def with_headroom(wei: int, headroom: float | None = None) -> int:
    """`wei` grossed up by PGAS_INSTANT_GAS_HEADROOM, in integers, rounded UP.

    ⛔ A headroom below 1 reads as 1. It is the multiple that makes a float gate cover a base-fee
    tick between the gate and the block; under 1 it would make the gate admit a spend the float
    cannot pay for, which is the one thing the gate exists to stop."""
    h = float(settings.instant_gas_headroom if headroom is None else headroom)
    if not (h > 1.0):
        h = 1.0
    bps = int(round(h * 10_000))
    return -(-int(wei) * bps // 10_000)


async def fee_estimate(rpc: Any, gas_limit: int | None = None) -> dict[str, Any]:
    """What one plain transfer costs RIGHT NOW: `{base_wei, tip_wei, max_fee_wei, gas_limit,
    gas_cost_wei}`, the last already grossed up by the headroom.

    `maxFeePerGas = 2 × base + tip`, the base being the LAST `baseFeePerGas` entry — the block
    being built, not the last one mined. RAISES `Unreadable`: a gas price we cannot read is a
    transaction we cannot price, and pricing it from a constant is §WE-SET-IT-WE-DONT-READ-IT."""
    h = await _call(rpc, "eth_feeHistory", [hex(FEE_HISTORY_BLOCKS), "latest", [TIP_PERCENTILE]])
    if not isinstance(h, dict):
        raise Unreadable("eth_feeHistory answered something that is not a fee history")
    bases = h.get("baseFeePerGas") or []
    if not bases:
        raise Unreadable("eth_feeHistory returned no baseFeePerGas")
    base = _int(bases[-1])
    tips = [_int(r[0]) for r in (h.get("reward") or []) if r and r[0] is not None]
    tip = sorted(tips)[len(tips) // 2] if tips else MIN_TIP_WEI
    tip = min(max(int(tip), MIN_TIP_WEI), MAX_TIP_WEI)
    max_fee = BASE_FEE_MULTIPLIER * base + tip
    if max_fee <= 0:
        raise Unreadable("eth_feeHistory produced a non-positive gas price")
    limit = int(gas_limit or settings.instant_gas_limit)
    return {
        "base_wei": base,
        "tip_wei": tip,
        "max_fee_wei": max_fee,
        "gas_limit": limit,
        "gas_cost_wei": with_headroom(limit * max_fee),
    }


async def float_wei(rpc: Any, addr: str) -> int:
    """The distributor's ETH balance at the head. RAISES `Unreadable` — a balance nobody could
    read is not a balance of zero, and it is certainly not "enough"."""
    return _int(await _call(rpc, "eth_getBalance", [to_checksum_address(addr), "latest"]))


async def nonce(rpc: Any, addr: str, block: str = "pending") -> int:
    """`eth_getTransactionCount`. `pending` counts what is in the mempool as well as what is
    mined; `latest` counts only what is mined, and the gap between them is our own in-flight
    transaction. RAISES `Unreadable`."""
    return _int(await _call(rpc, "eth_getTransactionCount", [to_checksum_address(addr), block]))


# ----------------------------------------------------------------------------- the bytes


def sign_transfer(
    s: Signer,
    *,
    nonce: int,
    to: str,
    value_wei: int,
    max_fee_wei: int,
    tip_wei: int,
    gas_limit: int | None = None,
    chain_id: int | None = None,
) -> dict[str, Any]:
    """One plain ETH transfer, signed. Returns `{raw, hash, nonce, to, value_wei, …}`.

    ⛔ THE ONLY PLACE THIS SYSTEM SIGNS AN ETHEREUM TRANSACTION, and it is called exactly once
    per order. A retry re-broadcasts `raw`; it never comes back here, because a second signature
    over one order's inventory is two independently-valid transactions and the reason the arb
    stack's law exists at all. (Two of ours carrying one nonce could still only fill once — but
    "only one can mine" is not a thing to lean on when not signing twice is free.)"""
    dest = to_checksum_address(to)
    if dest.lower() == s.address.lower():
        raise DistributorError("refusing to sign a transfer from the distributor to itself")
    if int(value_wei) <= 0:
        raise DistributorError(f"refusing to sign a transfer of {int(value_wei)} wei")
    if int(nonce) < 0 or int(max_fee_wei) <= 0 or int(tip_wei) < 0:
        raise DistributorError("refusing to sign with a negative nonce or a non-positive fee")
    limit = int(gas_limit or settings.instant_gas_limit)
    tx = {
        "type": 2,
        "chainId": int(chain_id or settings.eth_chain_id),
        "nonce": int(nonce),
        "to": dest,
        "value": int(value_wei),
        "gas": limit,
        "maxFeePerGas": int(max_fee_wei),
        "maxPriorityFeePerGas": min(int(tip_wei), int(max_fee_wei)),
        "data": b"",
    }
    raw, tx_hash = s.sign(tx)
    return {
        "raw": raw,
        "hash": tx_hash,
        "nonce": int(nonce),
        "to": dest,
        "value_wei": int(value_wei),
        "gas_limit": limit,
        "max_fee_wei": int(max_fee_wei),
        "tip_wei": int(tx["maxPriorityFeePerGas"]),
        "chain_id": int(tx["chainId"]),
    }


def classify_send(text: str) -> str:
    """What a refusal from every endpoint actually MEANS. Order matters: bytes a node already
    holds are accepted bytes whatever else anybody says about the nonce."""
    low = (text or "").lower()
    if any(h in low for h in KNOWN_HINTS):
        return KNOWN
    if any(h in low for h in NONCE_HINTS):
        return NONCE_TAKEN
    return REFUSED


async def broadcast(rpc: Any, raw: str) -> dict[str, Any]:
    """Hand the SAME bytes to every endpoint until one takes them.

    `{outcome, hash, errors}`. It never raises: "nobody took it" is an outcome the caller acts
    on (keep the bytes, try again next pass), not an exception that loses the row's place. The
    fan-out is deliberate — `Rpc.call` stops at the first endpoint that answers, and for a
    broadcast an ERROR is an answer, so one node with a full mempool would look like the chain
    refusing. A pool that cannot fan out is asked its own way (law: the prober must call the
    way the caller calls)."""
    urls = list(getattr(rpc, "urls", []) or [])
    call_on = getattr(rpc, "call_on", None)
    errors: list[str] = []
    if urls and callable(call_on):
        for url in urls:
            try:
                res = await call_on(url, "eth_sendRawTransaction", [raw])
            except ethpipe.RpcError as e:
                errors.append(str(e)[:200])
                continue
            return {"outcome": ACCEPTED, "hash": res, "errors": errors, "url": url}
    else:
        try:
            res = await rpc.call("eth_sendRawTransaction", [raw])
        except ethpipe.RpcError as e:
            errors.append(str(e)[:200])
        else:
            return {"outcome": ACCEPTED, "hash": res, "errors": errors, "url": None}
    return {
        "outcome": classify_send(" | ".join(errors)),
        "hash": None,
        "errors": errors,
        "url": None,
    }


async def verify_receipt(rpc: Any, tx_hash: str, to: str, value_wei: int) -> dict[str, Any]:
    """Did OUR transaction deliver OUR amount to OUR destination?

    `{mined, ok, problem, block, status, to, value_wei}`. RAISES `Unreadable` when the receipt
    or the transaction could not be read at all — §broadcast-is-not-done cuts both ways: a
    receipt we cannot read is not a receipt that says no.

    The value is deliberately read from the TRANSACTION: a receipt carries the status and the
    recipient and no amount at all, so "status 1" alone would prove a transfer happened, not
    that it was this one."""
    try:
        rec = await rpc.receipt(tx_hash)
    except ethpipe.RpcError as e:
        raise Unreadable(f"eth_getTransactionReceipt: {str(e)[:180]}") from e
    if not rec:
        return {"mined": False, "ok": False, "problem": "", "block": 0, "status": None}
    try:
        tx = await rpc.transaction(tx_hash)
    except ethpipe.RpcError as e:
        raise Unreadable(f"eth_getTransactionByHash: {str(e)[:180]}") from e
    if not tx:
        # a receipt with no transaction behind it is an endpoint disagreeing with itself
        raise Unreadable(f"{tx_hash} has a receipt but no transaction on this pool")
    status = _int(rec.get("status"))
    got_to = str(tx.get("to") or rec.get("to") or "").lower()
    got_value = _int(tx.get("value"))
    block = int(_int(rec.get("blockNumber") or 0))
    out = {
        "mined": True,
        "ok": False,
        "problem": "",
        "block": block,
        "status": status,
        "to": got_to,
        "value_wei": got_value,
    }
    if status != 1:
        out["problem"] = f"the transaction mined in block {block} with status {status} — it reverted"
    elif got_to != to_checksum_address(to).lower():
        out["problem"] = (
            f"the transaction mined in block {block} paid {got_to} and this order's destination "
            f"is {to_checksum_address(to)}"
        )
    elif got_value != int(value_wei):
        out["problem"] = (
            f"the transaction mined in block {block} carried a value of {got_value} wei and this "
            f"order owes {int(value_wei)} wei"
        )
    else:
        out["ok"] = True
    return out


# ----------------------------------------------------------------------------- the float plan


def plan_refill(have_wei: int, min_wei: int, target_wei: int) -> int:
    """How much to add, in wei: nothing while the float is at or above the floor, and the whole
    way to the target once it is below it.

    A top-up is not a trickle. Refilling to the floor would put us back under it after one
    payout and turn one crossing into a permanent stream of them, each with its own bridge fee
    and its own ~66-minute wait — so the floor is a trigger and the target is the destination."""
    have, low, high = int(have_wei), int(min_wei), int(target_wei)
    if low <= 0 or have >= low:
        return 0
    return max(0, max(high, low) - have)


# ----------------------------------------------------------------------------- the row


async def active_row() -> dict[str, Any] | None:
    """The active distributor, or None. One at a time in v1 — `routers/stats.py` counts them."""
    return await db().distributors.find_one({"state": ACTIVE})


async def ensure_row(rpc: Any) -> dict[str, Any]:
    """The active distributor's row for the CONFIGURED key, created on first use.

    ⛔ THE NONCE IS SEEDED FROM THE CHAIN, ONCE. Seeding it with 0 on a key that has already
    signed would make every payout collide with a mined transaction for ever; re-reading it
    every pass would let a stranger's transaction silently move OUR record forward, which is
    exactly the case `_payout_paying` has to be able to detect. So: read once at creation,
    then the row is the writer and the chain is only ever the CHECK."""
    s = signer()
    key = s.address.lower()
    row = await db().distributors.find_one({"_id": key})
    if row:
        if str(row.get("key_file") or "") != s.path:
            await db().distributors.update_one({"_id": key}, {"$set": {"key_file": s.path}})
            row["key_file"] = s.path
        return row
    n = await nonce(rpc, s.address, "pending")
    now = time.time()
    doc: dict[str, Any] = {
        "_id": key,
        "address": s.address,
        "state": ACTIVE,
        # the PATH, which is not a secret; the key it holds never reaches this database
        "key_file": s.path,
        "nonce_next": int(n),
        "nonce_seeded_from_chain": int(n),
        "float_wei": "0",
        "created_at": now,
    }
    try:
        await db().distributors.insert_one(dict(doc))
    except DuplicateKeyError:  # another pass created it between the read and the write
        return await db().distributors.find_one({"_id": key}) or doc
    # one ACTIVE distributor at a time: anything else that was active is now draining, and
    # /v1/stats counts exactly one
    await db().distributors.update_many(
        {"state": ACTIVE, "_id": {"$ne": key}},
        {"$set": {"state": DRAINING, "retired_at": now}},
    )
    log.info("distributor %s registered, nonce seeded from the chain at %d", s.address, n)
    return doc


async def reserve_nonce(addr: str, n: int, ref: str) -> bool:
    """Claim nonce `n` for `ref` with ONE conditional update, or answer False.

    A read-then-write is not a claim: two orders in one pass that both read `nonce_next` would
    both sign it, and only one of the two transactions could ever mine — the other is a payout
    that silently never happens. The filter carries the nonce, so the second finds nothing."""
    res = await db().distributors.find_one_and_update(
        {"_id": str(addr).lower(), "nonce_next": int(n)},
        {
            "$set": {
                "nonce_next": int(n) + 1,
                "reserved_by": str(ref),
                "reserved_nonce": int(n),
                "reserved_at": time.time(),
            }
        },
    )
    return res is not None


async def ensure_nonce_reserved(addr: str, n: int, ref: str) -> bool:
    """"Our record covers nonce `n`" — made true, idempotently, or answered False.

    ⛔ THE CRASH WINDOW `reserve_nonce` CANNOT CLOSE. `_payout_instant` claims the ROW (which
    writes `instant_nonce`) and reserves the nonce in a second update; a process killed between
    the two leaves an order that owns nonce `n` and a distributor whose `nonce_next` is still
    `n`. The next order then reads `n`, reserves it, signs it — two transactions over one nonce,
    of which at most one can ever mine, and the other is a payout that silently never happens.
    Meanwhile the chain moves to `n+1` when the first of them lands and every later order holds
    on "our record and the chain disagree": a stalled queue, from one lost millisecond.

    So the recovery is a CONDITIONAL update with `$lte`, not `==`: it moves the record forward
    when it is behind and matches nothing when it is already ahead. Re-running it is free, which
    is what makes it safe to call on every resume and before every new signature."""
    key = str(addr).lower()
    res = await db().distributors.find_one_and_update(
        {"_id": key, "nonce_next": {"$lte": int(n)}},
        {
            "$set": {
                "nonce_next": int(n) + 1,
                "reserved_by": str(ref),
                "reserved_nonce": int(n),
                "reserved_at": time.time(),
                "nonce_recovered_at": time.time(),
            }
        },
    )
    if res is not None:
        return True
    row = await db().distributors.find_one({"_id": key})
    # nothing matched because the record is already past `n` — which is the state we wanted
    return bool(row) and int(row.get("nonce_next") or 0) > int(n)


# ------------------------------------------------------------------ the fee-bumped replacement

# What a node demands before it will replace a transaction it already holds: +10 % on BOTH the
# fee cap and the tip (geth `txpool.pricebump`, and every fork of it). Asking for exactly the
# market price when the market has not moved is a replacement every endpoint refuses as
# underpriced — which is a fee bump that bumps nothing.
BUMP_MIN_BPS = 11_000


def bump_fees(est: dict[str, Any], max_fee_wei: int, tip_wei: int) -> dict[str, Any]:
    """The gas price for the ONE replacement an unminable transfer is allowed (payouts L1).

    The market's own number when it has risen, and the node's +10 % minimum when it has not —
    whichever is higher, on BOTH caps, because a node compares both. The `gas_cost_wei` comes
    back grossed up by the same headroom the first estimate used, so the float gate re-runs
    against what the replacement actually costs."""
    old_max, old_tip = int(max_fee_wei), int(tip_wei)
    new_max = max(int(est["max_fee_wei"]), -(-old_max * BUMP_MIN_BPS // 10_000))
    new_tip = max(int(est["tip_wei"]), -(-old_tip * BUMP_MIN_BPS // 10_000))
    new_tip = min(new_tip, new_max)
    limit = int(est["gas_limit"])
    return {
        **est,
        "max_fee_wei": new_max,
        "tip_wei": new_tip,
        "gas_limit": limit,
        "gas_cost_wei": with_headroom(limit * new_max),
        "bumped_from": {"max_fee_wei": old_max, "tip_wei": old_tip},
    }


async def verify_receipt_any(
    rpc: Any, hashes: Iterable[str], to: str, value_wei: int
) -> dict[str, Any]:
    """`verify_receipt` over EVERY hash this order has ever signed — the first that is mined wins.

    ⛔ An order that was fee-bumped has two sets of bytes and one nonce, so exactly one of them
    can ever be included — but WHICH one is the chain's choice, not ours. Reading only the
    newest would make a late inclusion of the original look like "our transaction is nowhere",
    i.e. the one verdict that ends a paid-for order.

    The answer carries `hash`, the one that actually mined. RAISES `Unreadable` only when no
    hash was mined AND at least one could not be read at all: "not mined" needs every hash
    answered, and one endpoint blinking is not evidence that nothing landed."""
    unreadable: list[str] = []
    out: dict[str, Any] = {"mined": False, "ok": False, "problem": "", "block": 0, "status": None}
    for h in hashes:
        if not h:
            continue
        try:
            seen = await verify_receipt(rpc, str(h), to, value_wei)
        except Unreadable as e:
            unreadable.append(f"{h}: {e}")
            continue
        if seen["mined"]:
            return {**seen, "hash": str(h)}
    if unreadable:
        raise Unreadable(" | ".join(unreadable)[:400])
    return {**out, "hash": ""}


async def record_float(addr: str, wei: int) -> None:
    """The last float we READ, as a decimal string.

    ⚠️ A string on purpose: wei does not fit a 64-bit integer above ~9.2 ETH, and a document
    that cannot be written is a float nobody can see. It is evidence for the operator and for
    /v1/health — never a gate. Gates read the chain."""
    await db().distributors.update_one(
        {"_id": str(addr).lower()},
        {"$set": {"float_wei": str(int(wei)), "float_at": time.time()}},
    )


async def record_refill(addr: str, ref: str, wei: int) -> None:
    await db().distributors.update_one(
        {"_id": str(addr).lower()},
        {"$set": {"last_refill": {"ref": str(ref), "wei": str(int(wei)), "at": time.time()}}},
    )


def healthy(full: dict[str, Any]) -> bool:
    """Can this distributor pay the next order? ONE derivation, so the public boolean and the
    operator's own view cannot disagree about what "well" means.

    Three things have to be true: a key file is configured AND it loaded (an `error` on the
    summary is a distributor that exists only on paper), the row is ACTIVE, and the last float a
    pass read is at or above the floor. It is deliberately NOT a live chain read — a health
    endpoint that reaches Ethereum answers as slowly as the slowest endpoint — so a `healthy`
    that has never been measured (`float_at` absent, float "0") reads False rather than True."""
    if not full.get("configured") or full.get("error"):
        return False
    if str(full.get("state") or "") != ACTIVE:
        return False
    try:
        have = int(str(full.get("float_wei") or "0"))
    except ValueError:
        return False
    return have >= int(settings.distributor_float_min_wei)


async def public_summary() -> dict[str, Any]:
    """What **/v1/health** publishes: two booleans, and not one fact more.

    ⛔ THE ADDRESS, THE FLOAT AND THE NEXT NONCE ARE NOT PUBLIC (T34b M5). /v1/health is
    unauthenticated: together those three are an inventory of a hot wallet, a live read of how
    much it holds, and a schedule of the transaction it is about to sign — everything an
    observer needs to watch our payouts, and to know when the float is thin enough to be worth
    front-running. The operator's full view is `beam status` and the key-protected admin panel;
    the public one answers "is there a distributor" and "is it well".

    It is a PROJECTION of `summary()` and never a second implementation of it (law 9)."""
    full = await summary()
    return {"configured": bool(full.get("configured")), "healthy": healthy(full)}


async def summary() -> dict[str, Any]:
    """What `beam status` and the key-protected admin panel say about the distributor — the FULL
    view, never the public one (`public_summary`). READ-ONLY and DATABASE-ONLY:
    a health endpoint that reaches Ethereum answers as slowly as the slowest endpoint and fails
    when the chain does. The float here is the last one a refill pass read."""
    out: dict[str, Any] = {"configured": configured(), "address": "", "float_wei": "0"}
    if not configured():
        return out
    try:
        out["address"] = address()
    except KeyFileError as e:
        out["error"] = str(e)[:200]
        return out
    try:
        row = await active_row()
    except Exception as e:  # noqa: BLE001 — /v1/health exists to answer when things are broken
        out["error"] = f"the distributor row could not be read: {type(e).__name__}"
        return out
    if row:
        out["float_wei"] = str(row.get("float_wei") or "0")
        out["float_at"] = row.get("float_at")
        out["nonce_next"] = row.get("nonce_next")
        out["last_refill"] = row.get("last_refill")
        out["state"] = row.get("state")
    return out
