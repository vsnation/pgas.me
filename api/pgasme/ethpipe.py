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
    pass


class Rpc:
    """Ordered endpoint pool. Every call tries the endpoints in order; a call that no endpoint
    answers RAISES — callers must not read that as an empty result."""

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
            raise RpcError(f"{url}: {type(e).__name__}: {e}") from e
        if isinstance(body, dict) and "result" in body:
            return body["result"]
        err = body.get("error") if isinstance(body, dict) else body
        raise RpcError(f"{url}: {json.dumps(err)[:200]}")

    async def call(self, method: str, params: list[Any], prefer: str | None = None) -> Any:
        """Try the endpoints in order (`prefer` first when given)."""
        urls = ([prefer] + [u for u in self.urls if u != prefer]) if prefer else self.urls
        last: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            for url in urls:
                try:
                    return await self._post(c, url, method, params)
                except RpcError as e:
                    last = e
        raise RpcError(f"{method}: no endpoint answered ({last})")

    async def call_on(self, url: str, method: str, params: list[Any]) -> Any:
        """One explicit endpoint, no fallback — for capability probes."""
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            return await self._post(c, url, method, params)

    async def block_number(self, prefer: str | None = None) -> int:
        return int(await self.call("eth_blockNumber", [], prefer=prefer), 16)

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
        return await self.call("eth_getTransactionReceipt", [tx_hash], prefer=prefer)

    async def logs(
        self,
        address: str,
        topics: list[Any],
        from_block: int,
        to_block: int,
        prefer: str | None = None,
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
        )
        if not isinstance(res, list):
            raise RpcError(f"eth_getLogs: unexpected answer {str(res)[:120]}")
        return res


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
