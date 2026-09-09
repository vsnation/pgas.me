"""Client for the cross-chain order router (read-only HTTP; the only state-changing thing the
router ever does is what the USER signs). Primary host first, its mirror as fallback. Errors are
never swallowed into "no data": every failure raises XchainError with the upstream message so
routes answer 502.

Our own vocabulary is neutral — mode `xchain`, `route_chain_id`, `route_fees` — and the router's
own spellings live in ONE place (`config.ROUTER_*`, `config.LEGACY_*`). `norm_mode()` below is
what lets a row written, or a client built, before that rename keep working.

Shapes (recorded live 2026-09-09):
  GET /supported-chains-info        → {"chains":[{"chainId":100000013,"originalChainId":1514,"chainName":"Story"}, …]}
  GET /token-list?chainId=42161     → {"tokens":{"0x…":{symbol,name,decimals,address,logoURI,isNative?}, …}}
  GET <ORDER_PATH>/create-tx?…      → {"estimation":{srcChainTokenIn{…},dstChainTokenOut{amount,…},costsDetails[]},
                                       "tx":{data,to,value[,allowanceTarget,allowanceValue]},"orderId","order":{…},
                                       "fixFee","protocolFee","estimatedTransactionFee":{…}}
  GET /chain/estimation?…           → {"estimation":{tokenIn{…},tokenOut{amount,minAmount,…},slippage,
                                       protocolFee,estimatedTransactionFee{…},costsDetails[]}}
  GET /chain/transaction?…          → the same fields FLAT + {"tx":{to,data,value}}  (same-chain swaps)
  GET <TX_PATH>/{hash}/order-ids    → {"orderIds":["0x…"]}
  GET <ORDER_PATH>/{id}/status      → {"status":"Fulfilled","orderId":"0x…"}   (400 UNKNOWN_ORDER when unknown)
  GET stats-api /Orders/{id}/liteModel → {state, rawOrderMetadataHex, orderFulfilledTransactionHash, …}
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .config import LEGACY_MODE, ROUTER_ORDER_PATH, ROUTER_TX_PATH, settings

CHAINS_TTL_S = 3600
TOKENS_TTL_S = 600

TERMINAL_OK = ("Fulfilled", "SentUnlock", "ClaimedUnlock")
TERMINAL_CANCELLED = ("OrderCancelled", "ClaimedOrderCancel")

# Our own name for the cross-chain quote/deposit mode, on the wire and in the database.
MODE = "xchain"


def norm_mode(mode: str | None) -> str:
    """The mode of record for a quote or deposit row.

    `None` (rows written before the same-chain modes existed) and the router's older name for
    this mode (rows and clients from before the rename) both mean the cross-chain mode; `direct`
    and `swap` pass through untouched. ONE implementation, called from every read point — the
    alias is honoured for one release and then this function is the only thing to delete."""
    m = (mode or "").strip()
    return MODE if not m or m == LEGACY_MODE else m


class XchainError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, error_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.error_id = error_id


# reachability bookkeeping for the monitor
health: dict[str, Any] = {"last_ok_at": 0.0, "last_fail_at": 0.0, "last_error": ""}

_chains_cache: dict[str, Any] = {"at": 0.0, "data": None}
_tokens_cache: dict[int, dict[str, Any]] = {}


def clear_cache() -> None:
    _chains_cache["at"] = 0.0
    _chains_cache["data"] = None
    _tokens_cache.clear()


def _bases() -> list[str]:
    out = [settings.xchain_base.rstrip("/")]
    if settings.xchain_mirror and settings.xchain_mirror.rstrip("/") != out[0]:
        out.append(settings.xchain_mirror.rstrip("/"))
    return out


def _upstream_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:200]}"
    if isinstance(body, dict):
        msg = body.get("errorMessage") or body.get("message") or body.get("error") or ""
        eid = body.get("errorId") or body.get("errorCode")
        return (
            f"{eid}: {msg}" if eid and msg else (msg or f"HTTP {r.status_code}: {str(body)[:200]}")
        )
    return f"HTTP {r.status_code}: {str(body)[:200]}"


async def _get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    bases: list[str] | None = None,
    timeout: float | None = None,
) -> Any:
    """GET path on the primary, then the mirror. A 4xx with an upstream message is final (it is
    the API's verdict, not a transport failure) and raises at once; transport errors and 5xx fall
    through to the next base."""
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout or settings.xchain_timeout_s) as c:
        for base in bases or _bases():
            url = f"{base}/{path.lstrip('/')}"
            try:
                r = await c.get(url, params=params)
            except httpx.HTTPError as e:
                last = XchainError(f"{url}: {type(e).__name__}: {e}")
                continue
            if r.status_code < 300:
                health["last_ok_at"] = time.time()
                try:
                    return r.json()
                except ValueError:
                    last = XchainError(f"{url}: not JSON: {r.text[:120]}")
                    continue
            msg = _upstream_message(r)
            err_id = None
            try:
                err_id = r.json().get("errorId")
            except Exception:  # noqa: BLE001
                pass
            if 400 <= r.status_code < 500:
                # the API answered; its verdict does not change on the mirror
                health["last_ok_at"] = time.time()
                raise XchainError(msg, status=r.status_code, error_id=err_id)
            last = XchainError(msg, status=r.status_code, error_id=err_id)
    health["last_fail_at"] = time.time()
    health["last_error"] = str(last)
    raise last if last else XchainError("cross-chain router: no base configured")


async def supported_chains(force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    if not force and _chains_cache["data"] is not None and now - _chains_cache["at"] < CHAINS_TTL_S:
        return _chains_cache["data"]
    body = await _get("supported-chains-info")
    chains = body.get("chains") if isinstance(body, dict) else body
    if not isinstance(chains, list) or not chains:
        raise XchainError("supported-chains-info: empty or malformed answer")
    _chains_cache["data"] = chains
    _chains_cache["at"] = now
    return chains


async def chain_map() -> dict[int, dict[str, Any]]:
    """originalChainId (EVM id) → chain row."""
    return {
        int(c["originalChainId"]): c for c in await supported_chains() if "originalChainId" in c
    }


async def route_chain_id(evm_chain_id: int) -> int:
    row = (await chain_map()).get(int(evm_chain_id))
    if not row:
        raise XchainError(f"chain {evm_chain_id} is not supported by the router", status=400)
    return int(row["chainId"])


async def token_list(route_chain: int, force: bool = False) -> list[dict[str, Any]]:
    """Tokens of one chain as a list (the router keys them by address), native first."""
    now = time.time()
    cached = _tokens_cache.get(int(route_chain))
    if not force and cached and now - cached["at"] < TOKENS_TTL_S:
        return cached["data"]
    body = await _get("token-list", {"chainId": int(route_chain)})
    toks = body.get("tokens") if isinstance(body, dict) else None
    if isinstance(toks, dict):
        rows = list(toks.values())
    elif isinstance(toks, list):
        rows = toks
    else:
        raise XchainError(f"token-list {route_chain}: malformed answer")
    rows.sort(
        key=lambda t: (
            0 if (t.get("isNative") or t.get("address", "").lower().endswith("0" * 40)) else 1,
            (t.get("symbol") or "").upper(),
        )
    )
    _tokens_cache[int(route_chain)] = {"at": now, "data": rows}
    return rows


async def create_tx(params: dict[str, Any]) -> dict[str, Any]:
    """The router's create-tx call. Returns the whole body (estimation, tx, orderId, …). Raises XchainError
    with the upstream message (e.g. COMPLIANCE_ADDRESS_BLOCKED, amount too small) on refusal."""
    body = await _get(f"{ROUTER_ORDER_PATH}/create-tx", params)
    if not isinstance(body, dict) or "estimation" not in body:
        raise XchainError(f"create-tx: malformed answer {str(body)[:200]}")
    return body


# --------------------------------------------------------------- single-chain (same-chain) swaps
# The router's ORDER api refuses an order whose source and destination chain are the same
# (SAME_SOURCE_AND_DESTINATION_CHAINS) and points at these two endpoints instead. They take NO
# hook parameter: the swap lands in tokenOutRecipient's OWN wallet and the pipe deposit is a
# second transaction the user signs afterwards (API_CONTRACT.md, mode "swap").
# Recorded live 2026-09-09 against the router's mainnet API, 5 USDC -> ETH:
#   GET /chain/estimation  -> {"estimation":{tokenIn{symbol,decimals,amount,approximateUsdValue},
#                              tokenOut{amount,minAmount,approximateUsdValue,...},slippage,
#                              recommendedSlippage,protocolFee,estimatedTransactionFee{total,...},
#                              comparedAggregators[],costsDetails[]}}
#   GET /chain/transaction -> the SAME fields FLAT (no "estimation" wrapper) plus {"tx":{to,data,value}}.
#                             No allowanceTarget/allowanceValue was returned for the ERC-20 input.


async def chain_estimation(params: dict[str, Any]) -> dict[str, Any]:
    """GET /chain/estimation → the inner `estimation` object (tokenIn, tokenOut, fees, costs)."""
    body = await _get("chain/estimation", params)
    est = body.get("estimation") if isinstance(body, dict) else None
    if not isinstance(est, dict) or not isinstance(est.get("tokenOut"), dict):
        raise XchainError(f"chain/estimation: malformed answer {str(body)[:200]}")
    return est


async def chain_transaction(params: dict[str, Any]) -> dict[str, Any]:
    """GET /chain/transaction → the whole body; `tx` is the swap the USER signs, into their own
    wallet. A body without a usable tx is an error, never an empty result."""
    body = await _get("chain/transaction", params)
    tx = body.get("tx") if isinstance(body, dict) else None
    if not isinstance(tx, dict) or not (tx.get("to") and tx.get("data")):
        raise XchainError(f"chain/transaction: no transaction in the answer {str(body)[:200]}")
    return body


def swap_out_amount(body: dict[str, Any]) -> int:
    """tokenOut.amount of a single-chain estimation (or transaction) answer, as int."""
    try:
        return int(body["tokenOut"]["amount"])
    except (KeyError, TypeError, ValueError) as e:
        raise XchainError(f"single-chain swap: no tokenOut.amount ({e})") from e


async def order_ids_by_tx(tx_hash: str, timeout: float | None = None) -> list[str]:
    """The router's orders created by one source transaction. Raises XchainError when the API cannot
    answer — an unreadable answer is not "this transaction created no orders"."""
    body = await _get(f"{ROUTER_TX_PATH}/{tx_hash}/order-ids", timeout=timeout)
    ids = body.get("orderIds") if isinstance(body, dict) else None
    if not isinstance(ids, list):
        raise XchainError(f"order-ids {tx_hash}: malformed answer {str(body)[:200]}")
    return [str(i) for i in ids]


async def order_status(order_id: str) -> dict[str, Any]:
    body = await _get(f"{ROUTER_ORDER_PATH}/{order_id}/status")
    if not isinstance(body, dict) or "status" not in body:
        raise XchainError(f"order status {order_id}: malformed answer {str(body)[:200]}")
    return body


async def lite_model(order_id: str) -> dict[str, Any] | None:
    """The router's stats API lite order model — best effort, never evidence. It carries
    rawOrderMetadataHex (our 5-byte metadata tag at bytes[45:50]) and the fulfilment tx hash.
    Returns None when the stats API cannot answer (it indexes with delay)."""
    try:
        body = await _get(
            f"Orders/{order_id}/liteModel", bases=[settings.xchain_stats_base.rstrip("/")], timeout=15
        )
    except XchainError:
        return None
    return body if isinstance(body, dict) else None


def out_amount(body: dict[str, Any]) -> int:
    """estimation.dstChainTokenOut.amount as int (raw units of the target on Ethereum)."""
    try:
        return int(body["estimation"]["dstChainTokenOut"]["amount"])
    except (KeyError, TypeError, ValueError) as e:
        raise XchainError(f"create-tx: no dstChainTokenOut.amount ({e})") from e


def recommended_amount(body: dict[str, Any]) -> int | None:
    """estimation.dstChainTokenOut.recommendedAmount — what solvers will actually fill at. With an
    explicit dstChainTokenOutAmount this can be BELOW `amount` (recorded live: a 250k-gas hook
    lowers it by ≈ $0.20); an order above it may sit unfilled."""
    try:
        v = body["estimation"]["dstChainTokenOut"].get("recommendedAmount")
        return int(v) if v is not None else None
    except (KeyError, TypeError, ValueError):
        return None
