"""The Beam bridge's Ethereum side (EthPipe / EthERC20Pipe) — calldata, event decoding, amount
splitting per asset, and a small JSON-RPC pool that tries endpoints in order and never turns
"no answer" into "no event" (an unreadable query is not evidence of anything).

    function sendFunds(uint256 value, uint256 relayerFee, bytes receiverBeamPubkey) payable
    event NewLocalMessage(uint64 msgId, uint256 amount, uint256 relayerFee, bytes receiver)

Both pipe flavours share the ABI: EthPipe expects msg.value == value + relayerFee; the ERC-20
pipe pulls value + relayerFee via transferFrom (approve first). The pipe mints `value` on Beam
at 8 decimals, so `value` must sit on the asset's grid (1e10 for 18-decimal assets, 1 for WBTC).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

from .assets import Asset
from .config import GROTH_PER_WEI_GRID, settings

SENDFUNDS_SIG = "sendFunds(uint256,uint256,bytes)"
NEWLOCAL_SIG = "NewLocalMessage(uint64,uint256,uint256,bytes)"
SENDFUNDS_SELECTOR = keccak(text=SENDFUNDS_SIG)[:4]
NEWLOCAL_TOPIC = "0x" + keccak(text=NEWLOCAL_SIG).hex()
# Recorded on the founder's box from mainnet tx 0x8596…0684 (msgId 222): the topic must equal this.
NEWLOCAL_TOPIC_EXPECTED = "0x5f52670be4e2f3d7b079180b485ab44712641a10d1c77e843355f96036608ac7"


class SplitError(ValueError):
    pass


def min_relayer_fee_units(asset: Asset) -> int:
    """The smallest relayerFee we ride on the tail, in the asset's Ethereum units."""
    if asset.key == "ETH":
        floor = settings.min_relayer_fee_wei
    elif asset.key == "DAI":
        floor = settings.min_relayer_fee_dai_units
    elif asset.key == "WBTC":
        floor = settings.min_relayer_fee_wbtc_units
    else:  # pragma: no cover — assets.py is the closed list
        floor = asset.grid
    return max(int(floor), asset.grid)


def split_amount(
    amount: int, min_relayer_fee: int, grid: int = GROTH_PER_WEI_GRID
) -> tuple[int, int]:
    """value floored to the asset grid, the tail absorbed into relayerFee.

    A sub-grid tail on `value` is unmintable on Beam and stuck forever; so value is a multiple
    of `grid` and everything else rides as the relayer fee. value + relayerFee == amount.
    For WBTC (8 decimals on both sides) grid == 1 and the floor is a no-op.
    """
    if grid < 1:
        raise SplitError("grid must be ≥ 1")
    if amount <= min_relayer_fee:
        raise SplitError("amount does not cover the relayer fee")
    value = ((amount - min_relayer_fee) // grid) * grid
    if value <= 0:
        raise SplitError("amount too small to mint a single groth")
    return value, amount - value


def split_for_asset(amount: int, asset: Asset) -> tuple[int, int]:
    return split_amount(amount, min_relayer_fee_units(asset), asset.grid)


def encode_send_funds(value: int, relayer_fee: int, pubkey_hex: str) -> str:
    pk = bytes.fromhex(pubkey_hex.lower().removeprefix("0x"))
    if len(pk) != 33:
        raise ValueError("receiverBeamPubkey must be 33 bytes")
    if value <= 0 or relayer_fee < 0:
        raise ValueError("value must be positive and relayerFee non-negative")
    data = SENDFUNDS_SELECTOR + encode(["uint256", "uint256", "bytes"], [value, relayer_fee, pk])
    return "0x" + data.hex()


def decode_send_funds(calldata_hex: str) -> dict[str, Any]:
    raw = bytes.fromhex(calldata_hex.removeprefix("0x"))
    if raw[:4] != SENDFUNDS_SELECTOR:
        raise ValueError("not a sendFunds call")
    value, fee, pk = decode(["uint256", "uint256", "bytes"], raw[4:])
    return {"value": value, "relayer_fee": fee, "pubkey": pk.hex()}


def _int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, str):
        return int(v, 16) if v.startswith("0x") else int(v)
    return int(v)


def decode_new_local_message(log: dict[str, Any]) -> dict[str, Any]:
    data = bytes.fromhex(log["data"].removeprefix("0x"))
    msg_id, amount, fee, receiver = decode(["uint64", "uint256", "uint256", "bytes"], data)
    return {
        "msg_id": int(msg_id),
        "amount": int(amount),
        "relayer_fee": int(fee),
        "receiver": receiver.hex(),
        "address": to_checksum_address(log["address"]),
        "block": _int(log.get("blockNumber")),
        "tx": log.get("transactionHash"),
        "log_index": _int(log.get("logIndex")),
    }


class RpcError(RuntimeError):
    """A call that no allowed endpoint answered. `url` and `code` survive the wrapping so the
    caller can tell WHY: an endpoint that lags behind the head it just claimed is a different
    thing from an endpoint that is down, and only one of them is worth retrying elsewhere."""

    def __init__(self, message: str, *, url: str | None = None, code: int | None = None):
        super().__init__(message)
        self.url = url
        self.code = code


# What a node says when its logs backend is behind the head its eth_blockNumber just reported.
# Recorded on prod 2026-09-09: rpc.flashbots.net answers eth_blockNumber with H and then
# {"code": -32602, "message": "block range extends beyond current head block"} for [H-10, H].
LAG_HINTS = (
    "beyond current head",
    "beyond the current head",
    "block range",
    "head block",
    "header not found",
    "unknown block",
    "block not found",
    "requested block is beyond",
    "cannot query unfinalized",
)


# The other half, measured on the same probe run: providers refuse a range they COULD serve for
# plan/capability reasons — publicnode "Archive requests require a personal token", 1rpc
# "eth_getLogs is limited to 0 - 50 blocks range", drpc "Can't route your request to suitable
# provider", ankr "You must authenticate your request with an API key". Different sentence, same
# fact: THIS endpoint will not answer THIS query, and the remedy is another endpoint.
SERVE_HINTS = (
    "archive",
    "personal token",
    "api key",
    "unauthorized",
    "limited to",
    "suitable provider",
    "discontinued",
    "too many results",
    "query returned more than",
    "response size",
    "cannot fulfill",
    "capacity",
    "rate limit",
    "too many requests",
    "exceeded",
)


def is_head_lag(e: BaseException) -> bool:
    """True when the answer means "I do not have those blocks YET" — an endpoint problem, not a
    range problem. The pass switches endpoints; it never treats it as "no logs"."""
    text = str(e).lower()
    if any(h in text for h in LAG_HINTS):
        return True
    code = getattr(e, "code", None)
    return code == -32602 and any(w in text for w in ("head", "range", "block"))


# `_post` stamps transport failures with the exception's own class name; a node that does not
# answer is the strongest reason of all to ask a different one.
TRANSPORT_HINTS = (
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "connectionreset",
    "remoteprotocolerror",
    "readerror",
    "proxyerror",
    "jsondecodeerror",
    "timeoutexception",
)


def cannot_serve(e: BaseException) -> bool:
    """True when this endpoint will not answer this query — lag, a range cap, an archive/plan
    wall, a routing failure, a rate limit, or no answer at all. Ask the next endpoint; never
    conclude "no logs". A malformed request of OURS matches none of these and stays a real
    failure, because rotating endpoints cannot fix our own bug."""
    text = str(e).lower()
    return (
        is_head_lag(e)
        or any(h in text for h in SERVE_HINTS)
        or any(h in text for h in TRANSPORT_HINTS)
    )


class Rpc:
    """Ordered endpoint pool. Every call tries the endpoints in order; a call that no endpoint
    answers RAISES — callers must not read that as an empty result.

    `prefer` is an ORDERING hint (that endpoint first, the others after). `pin=True` turns it
    into a PIN: only that endpoint may answer, and if it cannot the call raises. A scan pins
    every read to the endpoint that vouched for the head, because a lagging endpoint answering
    eth_getLogs with `[]` is not "no locks" — it is "I cannot see those blocks yet", and
    checkpointing that answer skips a real deposit forever.

    A pin therefore has to fail LOUDLY and SPECIFICALLY: `is_head_lag()` on the raised error
    tells the scanner to re-resolve the head on the next endpoint instead of stalling. A pin
    that just says "no endpoint answered" turns one lagging provider into a dead watcher.
    """

    def __init__(self, urls: list[str] | None = None, timeout: float = 8.0):
        self.urls = urls or settings.eth_rpc_list
        self.timeout = timeout
        self._id = 0

    async def _post(self, c: httpx.AsyncClient, url: str, method: str, params: list[Any]) -> Any:
        self._id += 1
        try:
            r = await c.post(
                url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
            )
            body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise RpcError(f"{url}: {type(e).__name__}: {e}", url=url) from e
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        err = body.get("error") if isinstance(body, dict) else body
        code = err.get("code") if isinstance(err, dict) else None
        raise RpcError(
            f"{url}: {json.dumps(err)[:200]}",
            url=url,
            code=int(code) if isinstance(code, int) else None,
        )

    async def call(
        self, method: str, params: list[Any], prefer: str | None = None, pin: bool = False
    ) -> Any:
        """Try the endpoints in order (`prefer` first when given; ONLY it when `pin`)."""
        res, _url = await self.call_from(method, params, prefer=prefer, pin=pin)
        return res

    async def call_from(
        self, method: str, params: list[Any], prefer: str | None = None, pin: bool = False
    ) -> tuple[Any, str]:
        """(result, the endpoint that answered) — the caller can pin its next call to it."""
        if pin:
            if not prefer:
                raise RpcError(f"{method}: a pinned call needs an endpoint")
            urls = [prefer]
        else:
            urls = ([prefer] + [u for u in self.urls if u != prefer]) if prefer else list(self.urls)
        last: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            for url in urls:
                try:
                    return await self._post(c, url, method, params), url
                except RpcError as e:
                    last = e
        # the last endpoint's verdict travels with the wrapper: a pinned call must be able to
        # say "that endpoint lags" rather than the useless "nobody answered"
        raise RpcError(
            f"{method}: no endpoint answered ({last})",
            url=getattr(last, "url", None),
            code=getattr(last, "code", None),
        ) from last

    async def call_on(self, url: str, method: str, params: list[Any]) -> Any:
        """One explicit endpoint, no fallback — for capability probes."""
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            return await self._post(c, url, method, params)

    async def block_number(self, prefer: str | None = None, pin: bool = False) -> int:
        return int(await self.call("eth_blockNumber", [], prefer=prefer, pin=pin), 16)

    async def head_from(self) -> tuple[int, str]:
        """(head, the endpoint that answered it). Every read of a range that ends at this head
        has to be pinned to THAT endpoint, or the range was answered by a node that may not have
        the blocks."""
        res, url = await self.call_from("eth_blockNumber", [])
        return int(res, 16), url

    async def pool_heads(self) -> dict[str, int]:
        """Head per endpoint, skipping any that reports eth_syncing != false or does not answer."""
        heads: dict[str, int] = {}
        for url in self.urls:
            try:
                if await self.call_on(url, "eth_syncing", []) is not False:
                    continue
                heads[url] = int(await self.call_on(url, "eth_blockNumber", []), 16)
            except (RpcError, ValueError, TypeError):
                continue
        return heads

    async def receipt(self, tx_hash: str, prefer: str | None = None) -> dict[str, Any] | None:
        """The receipt from ANY endpoint that has it — None when every endpoint that ANSWERED
        says there is none yet, and RAISES when nobody answered at all.

        ⛔ 2026-09-10, the twin of the deposit-visibility bug one method above: this was
        `call()`, `call()` stops at the FIRST endpoint that answers, and for
        eth_getTransactionReceipt a `null` IS an answer. `scanner._retry_one` read that null as
        "not mined yet" and returned False — silently, on every pass — which is one of the two
        reasons a real 0.0019999 ETH lock (msgId 138) sat unattributed. One endpoint's blindness
        is not the chain's verdict; the remedy is MORE endpoints, never fewer (law 8).

        There is deliberately NO `pin` here. A pin exists so that an EMPTY RANGE answer comes
        from a node that actually has the blocks (eth_getLogs, where `[]` from a lagging node
        would be checkpointed as "no locks"). A receipt is identified by the transaction hash,
        not by a range: no endpoint's silence about one is evidence of anything, so pinning it
        could only ever turn one flaky provider into a stalled scan. `prefer` stays as an
        ordering hint — the endpoint that vouched for the log is asked first.
        """
        res, _url, answered, errors = await self._anywhere(
            "eth_getTransactionReceipt", [tx_hash], prefer=prefer
        )
        if res is None and not answered:
            raise RpcError(
                f"eth_getTransactionReceipt {tx_hash}: no endpoint answered "
                f"({'; '.join(errors) or 'no endpoint answered'})"
            )
        return res

    async def transaction(
        self, tx_hash: str, prefer: str | None = None, pin: bool = False
    ) -> dict[str, Any] | None:
        """eth_getTransactionByHash. None means "no endpoint has this transaction" — which is
        NOT the same as "it does not exist"; a caller that cannot read must not conclude."""
        return await self.call("eth_getTransactionByHash", [tx_hash], prefer=prefer, pin=pin)

    async def transaction_anywhere(
        self, tx_hash: str
    ) -> tuple[dict[str, Any] | None, str | None, int, list[str]]:
        """(tx, the endpoint that had it, how many endpoints ANSWERED, what the rest said).
        `_anywhere` is the whole implementation — see its contract."""
        return await self._anywhere("eth_getTransactionByHash", [tx_hash])

    async def _anywhere(
        self, method: str, params: list[Any], prefer: str | None = None
    ) -> tuple[Any, str | None, int, list[str]]:
        """(result, the endpoint that had it, how many endpoints ANSWERED, what the rest said).

        ⛔ `transaction()` / `receipt()` used to be `call()`, and `call()` stops at the first
        endpoint that ANSWERS. For eth_getTransactionByHash and eth_getTransactionReceipt a
        `null` result IS an answer, and some endpoints answer `null` for a transaction that
        exists: rpc.mevblocker.io (the first entry of the prod pool) is a private-orderflow
        relay that does not expose PENDING transactions at all.
        On 2026-09-10 that turned two real 0.002 ETH deposits into "that transaction is not
        visible on Ethereum yet" at registration, while the very same hashes mined and locked
        minutes later. One endpoint's blindness is not the chain's verdict — so this asks all
        of them and the FIRST ONE THAT HAS IT wins.

        `answered` is the load-bearing number and it is deliberately separate from "found":

          answered == 0   nobody could be asked. That is an unreadable query and never a
                          verdict — the caller must retry, not conclude (§the prober must call
                          the way the caller calls; an unreadable query is not evidence).
          answered  > 0   at least one endpoint gave a real answer and none of them had it.
                          Still not proof it does not exist (it may be pending in a mempool
                          none of these nodes gossips), but it IS enough to open an
                          unverified row and keep asking.

        The endpoints are asked CONCURRENTLY and the first one that HAS it wins. They are
        independent questions and this sits in the request path a user is waiting on: asked one
        after another, four endpoints at the 8 s pool timeout is 32 s of a hanging provider
        before the answer — long enough for the client to give up and for the operator to read
        it as an outage. `PGAS_TX_LOOKUP_DEADLINE_S` is the wall-clock ceiling on the whole
        question; an endpoint still silent when it expires is an ERROR, not a "no", so a pool
        that ran out of time comes back `answered == 0` — unreadable, never a verdict.
        """
        urls = ([prefer] + [u for u in self.urls if u != prefer]) if prefer else list(self.urls)
        if not urls:  # pragma: no cover — settings always parse at least one endpoint
            return None, None, 0, ["no Ethereum endpoint is configured"]
        deadline = max(0.1, float(settings.tx_lookup_deadline_s))
        per_call = min(float(self.timeout), deadline)
        answered = 0
        errors: list[str] = []
        found: tuple[Any, str] | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as c:

            async def ask(url: str) -> tuple[str, Any]:
                return url, await asyncio.wait_for(self._post(c, url, method, params), per_call)

            tasks = {asyncio.ensure_future(ask(u)): u for u in urls}
            pending = set(tasks)
            until = asyncio.get_running_loop().time() + deadline
            while pending and found is None:
                left = until - asyncio.get_running_loop().time()
                if left <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=left, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                for t in done:
                    try:
                        url, res = t.result()
                    except RpcError as e:
                        errors.append(str(e)[:200])  # an RpcError already names its endpoint
                        continue
                    except TimeoutError:
                        errors.append(f"{tasks[t]}: timed out after {per_call:g}s")
                        continue
                    except Exception as e:  # noqa: BLE001 — one endpoint's crash is not a verdict
                        errors.append(f"{tasks[t]}: {type(e).__name__}: {e}"[:200])
                        continue
                    answered += 1
                    if res and found is None:
                        found = (res, url)
            for t in pending:  # still silent when the deadline expired: an error, never a "no"
                t.cancel()
                if found is None:
                    errors.append(f"{tasks[t]}: no answer within {deadline:g}s")
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if found is not None:
            return found[0], found[1], answered, errors
        return None, None, answered, errors

    async def logs(
        self,
        address: str,
        topics: list[Any],
        from_block: int,
        to_block: int,
        prefer: str | None = None,
        pin: bool = False,
    ) -> list[dict[str, Any]]:
        res = await self.call(
            "eth_getLogs",
            [
                {
                    "address": address,
                    "topics": topics,
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                }
            ],
            prefer=prefer,
            pin=pin,
        )
        if not isinstance(res, list):
            raise RpcError(f"eth_getLogs: unexpected answer {str(res)[:120]}")
        return res


# What a row says while no endpoint of the pool has seen its transaction. ONE string, because
# two of them disagree: registration writes it and the watcher clears it again once the
# transaction is readable, and a note nobody clears is a lie that stays on the row forever.
UNSEEN_NOTE = "waiting for Ethereum to see it"


async def visible_tx(rpc: Any, tx_hash: str) -> tuple[dict[str, Any] | None, int, list[str]]:
    """"Has ANY endpoint of this pool got this transaction?" → (tx, answered, errors).

    ONE reader of that fact, for the three callers that need it: registration
    (routers/deposits), the watcher's re-verification (workers._reverify_chain) and the
    scanner's quote attribution. A pool object that cannot fan out — a test double, or any
    reader that owns a single endpoint — is asked its own way instead of being probed
    differently from how it is used: `transaction()` is then the whole pool, and one answer is
    one endpoint answering.
    """
    fan_out = getattr(rpc, "transaction_anywhere", None)
    if fan_out is None:
        try:
            return await rpc.transaction(tx_hash), 1, []
        except RpcError as e:
            return None, 0, [str(e)[:200]]
    try:
        tx, _url, answered, errors = await fan_out(tx_hash)
    except RpcError as e:  # pragma: no cover — transaction_anywhere swallows per-endpoint errors
        return None, 0, [str(e)[:200]]
    return tx, answered, errors


async def code_at_head_anywhere(rpc: Any, address: str) -> tuple[str, int, str]:
    """`(code, head, endpoint)` — the code at `address`, from the FIRST endpoint that will read
    it. Every endpoint is asked for ITS OWN head and then for the code PINNED to that head.
    RAISES `RpcError` when every one of them refused: an unreadable chain is not an empty answer,
    and `"0x"` from a node that cannot see the block is not "this is a wallet".

    ⛔ 2026-09-10. `payouts.dest_code_at_head` asked `head_from()` once and pinned eth_getCode to
    whichever endpoint answered it. On prod that is publicnode, which serves the head and then
    refuses a code read at a numeric block with "Archive requests require a personal token" — so
    the destination re-read raised every 30 s, `_dest_still_a_wallet` refused SILENTLY (correctly:
    an unreadable chain is not a verdict) and nothing was ever released. The fix for an endpoint
    that will not serve a query is MORE endpoints, never a lower bar (law 8) — and the head must
    keep coming from the same endpoint the code is read from, or the pin protects nothing.

    Ordering is the pool's own, first real answer wins; the CALLER compares the head it gets
    against the one it already proved the destination clear at (a snap-syncing node answering an
    old block is not evidence in either direction) — that decision does not belong here.
    """
    urls = list(getattr(rpc, "urls", None) or [])
    if not urls:  # pragma: no cover — settings always parse at least one endpoint
        raise RpcError("eth_getCode: no Ethereum endpoint is configured")
    errors: list[str] = []
    for url in urls:
        try:
            head = int(await rpc.block_number(prefer=url, pin=True))
            if head <= 0:
                raise RpcError(f"{url} reported block {head}", url=url)
            code = await rpc.call("eth_getCode", [address, hex(head)], prefer=url, pin=True)
            if not isinstance(code, str):
                raise RpcError(f"{url} could not read the code at that address", url=url)
            return code, head, url
        except Exception as e:  # noqa: BLE001 — one endpoint's refusal is not the chain's answer
            errors.append(f"{url}: {type(e).__name__}: {e}"[:200])
    raise RpcError(
        "eth_getCode: no endpoint would read the code at the head it reported "
        f"({'; '.join(errors)})"
    )


def _matches(log: dict[str, Any], pipe_address: str, want_pk: str) -> dict[str, Any] | None:
    if (log.get("address") or "").lower() != pipe_address.lower():
        return None
    if not log.get("topics") or log["topics"][0].lower() != NEWLOCAL_TOPIC:
        return None
    m = decode_new_local_message(log)
    if m["receiver"].lower() != want_pk:
        return None
    return m


def find_lock_in_receipt(
    receipt: dict[str, Any],
    pubkey_hex: str,
    pipe_address: str | None = None,
    amount: int | None = None,
) -> dict[str, Any] | None:
    """The NewLocalMessage log from OUR pipe naming OUR pubkey (and, if given, exactly `amount`),
    or None. Never guesses."""
    pipe = pipe_address or settings.ethpipe_address
    want_pk = pubkey_hex.lower().removeprefix("0x")
    for log in receipt.get("logs", []):
        m = _matches(log, pipe, want_pk)
        if m and (amount is None or m["amount"] == amount):
            return m
    return None


def find_locks_in_logs(
    logs: list[dict[str, Any]], pubkey_hex: str, pipe_address: str, amount: int | None = None
) -> list[dict[str, Any]]:
    want_pk = pubkey_hex.lower().removeprefix("0x")
    out = []
    for log in logs:
        m = _matches(log, pipe_address, want_pk)
        if m and (amount is None or m["amount"] == amount):
            out.append(m)
    out.sort(key=lambda m: ((m["block"] or 0), (m["log_index"] or 0)))
    return out


async def scan_locks(
    rpc: Rpc, pipe_address: str, from_block: int, to_block: int, chunk: int | None = None
) -> list[dict[str, Any]]:
    """Every NewLocalMessage log on `pipe_address` in [from_block, to_block], read in chunks.
    A chunk no endpoint answers RAISES (RpcError) — the caller must not treat it as 'no event'."""
    chunk = chunk or settings.lock_scan_chunk
    logs: list[dict[str, Any]] = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        logs.extend(await rpc.logs(pipe_address, [NEWLOCAL_TOPIC], start, end))
        start = end + 1
    return logs


async def sleep(s: float) -> None:  # indirection for tests
    await asyncio.sleep(s)
