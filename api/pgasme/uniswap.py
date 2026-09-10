"""The Uniswap V4 ingress: the route registry, the Quoter read, the PgasRouter calldata and the
hook's own deposit log.

ONE TRANSACTION. The user swaps an Ethereum token on a Pgas **gateway pool** (zero liquidity,
fee 0, our hook); the hook's `beforeSwap` routes the input through the canonical deep hook-less
pool, takes the whole output, splits it on the bridge's 8-decimal grid and calls the pipe's
`sendFunds` in the same transaction. There is no intermediate balance in the user's wallet and
no second signature, so — unlike mode "swap" — the transaction the user signs IS the deposit.

What this module owns:

  * the registry — `PGAS_UNISWAP_POOLS`, parsed and VALIDATED against what the contracts
    themselves require (`PgasRouter.deposit` refuses a pool whose hook is not ours;
    `PgasIngressHook.registerRoute` refuses an inner pool that has a hook, or a key whose
    currencies differ from the gateway's). A row that could only ever produce reverting
    calldata is a configuration error, not a route: it raises, and the route refuses;
  * the quote — `quoteExactInputSingle` on the INNER pool through an `eth_call`, never our own
    curve math (§WE-SET-IT-WE-DONT-READ-IT: the price is read, not set). An endpoint that will
    not answer raises `QuoteUnreadable`; the caller answers 503. An unreadable quote is never a
    zero, and a zero is never an estimate;
  * the calldata — `deposit(PoolKey, zeroForOne, amountIn, hookData)` with
    `hookData = abi.encode(bytes32 depositRef, uint256 minOut, uint256 relayerFeeQuote)`. The
    Beam public key is NOT in it: it is pinned in the on-chain route, because hookData is
    caller-controlled and a caller-chosen destination would turn the pool into a public bridge
    front-end;
  * the hook's `PgasDeposit` log, which is how a pipe lock in a receipt with no cross-chain fill
    is attributed back to a deposit row (§IDENTITY-BEATS-BALANCE: the ref AND the pipe's own
    `NewLocalMessage` for OUR pubkey, in the SAME receipt);
  * `value_band()` — the ONE implementation of "what may this lock be worth" (law 9: two
    implementations of one fact will disagree and one of them reaches money). The split rule
    itself lives in `ethpipe.split_amount` and is pinned to the golden vectors shared with the
    Solidity suite; nothing here re-implements it.

Selectors and the event topic are computed from their signatures and PINNED to the values the
compiled artifacts carry (`contracts/out/**`, cross-checked 2026-09-10), so a signature typed
differently here fails at import instead of on the money path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from eth_utils import is_address, keccak, to_checksum_address

from . import ethpipe
from .assets import Asset, get_asset
from .config import settings

log = logging.getLogger("pgasme.uniswap")

MODE = "uniswap"
NATIVE = "0x0000000000000000000000000000000000000000"

# PoolKey{Currency currency0, Currency currency1, uint24 fee, int24 tickSpacing, IHooks hooks};
# `Currency` and `IHooks` are user-defined value types over `address`, so the ABI sees plain
# addresses. PoolId == keccak256(abi.encode(key)) — every member is static, so that is these
# five words hashed (cross-checked against `cast abi-encode` + `cast keccak`, 2026-09-10).
POOL_KEY_ABI = "(address,address,uint24,int24,address)"

DEPOSIT_SIG = f"deposit({POOL_KEY_ABI},bool,uint256,bytes)"
DEPOSIT_SELECTOR = keccak(text=DEPOSIT_SIG)[:4]
DEPOSIT_SELECTOR_EXPECTED = "0x7b6b15fb"  # contracts/out/PgasRouter.sol/PgasRouter.json

QUOTE_SIG = f"quoteExactInputSingle(({POOL_KEY_ABI},bool,uint128,bytes))"
QUOTE_SELECTOR = keccak(text=QUOTE_SIG)[:4]
QUOTE_SELECTOR_EXPECTED = "0xaa9d21cb"  # v4-periphery 1.0.3 IV4Quoter.json

PGAS_DEPOSIT_SIG = "PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)"
PGAS_DEPOSIT_TOPIC = "0x" + keccak(text=PGAS_DEPOSIT_SIG).hex()
PGAS_DEPOSIT_TOPIC_EXPECTED = (
    "0xd0e41515b729c9019d2f260f5fb9501b4fc547032008d04c4cdbfda6b788ae2b"
)

MAX_UINT128 = 2**128 - 1
HOOK_DATA_ABI = ["bytes32", "uint256", "uint256"]
BPS = 10_000


class RouteError(RuntimeError):
    """The registry, the request or the answer is wrong in a way no retry fixes: an unknown
    pair, an amount above the route's cap, a pool with no liquidity at this size. It is a
    refusal with a reason, never a silent fallback to another route."""


class QuoteUnreadable(RuntimeError):
    """No endpoint answered the Quoter (or it answered nothing decodable). "I could not read"
    is not "the output is zero" — the caller answers 503 and the user retries."""


class ContradictoryBand(ValueError):
    """A quote row whose own numbers describe a band that admits nothing: its `min_out` floor
    sits ABOVE the highest `value` the hook could ever log for it (`split(out × upside)`).

    It is a ValueError on purpose: `scanner._sane_uniswap` already catches `ValueError` around
    `value_band` and turns it into "cannot check the amount against the quote", which sends the
    lock to `unattributed_locks` and pages — never to a balance. A row that cannot say what it
    expected cannot vouch for what arrived."""


# ----------------------------------------------------------------------------- the pool key


@dataclass(frozen=True)
class PoolKey:
    currency0: str  # EIP-55, the lower address of the pair (v4 requires currency0 < currency1)
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str

    def as_tuple(self) -> tuple[str, str, int, int, str]:
        return (self.currency0, self.currency1, self.fee, self.tick_spacing, self.hooks)

    @property
    def pool_id(self) -> str:
        return "0x" + keccak(encode([POOL_KEY_ABI], [self.as_tuple()])).hex()

    def as_json(self) -> dict[str, Any]:
        return {
            "currency0": self.currency0,
            "currency1": self.currency1,
            "fee": self.fee,
            "tickSpacing": self.tick_spacing,
            "hooks": self.hooks,
        }


@dataclass(frozen=True)
class Route:
    token_in: str  # EIP-55; NATIVE for ether
    symbol: str
    decimals: int
    gateway: PoolKey
    inner: PoolKey
    zero_for_one: bool
    target: str  # the target asset key — ETH / DAI / WBTC
    max_deposit_units: int
    min_deposit_units: int
    min_relayer_fee_units: int  # the route's on-chain floor (0 when not configured)
    max_relayer_fee_bps: int  # the route's on-chain ceiling (0 = not configured)

    @property
    def token_out(self) -> str:
        return self.gateway.currency1 if self.zero_for_one else self.gateway.currency0

    @property
    def native_in(self) -> bool:
        return self.token_in == NATIVE

    def as_json(self) -> dict[str, Any]:
        """What the client is told about the route (addresses and ids only — nothing secret)."""
        return {
            "hook": self.gateway.hooks,
            "router": to_checksum_address(settings.uniswap_router),
            "gateway_pool_id": self.gateway.pool_id,
            "inner_pool_id": self.inner.pool_id,
            "token_in": self.token_in,
            "token_out": self.token_out,
            "fee": self.inner.fee,
            "symbol": self.symbol,
        }


# ----------------------------------------------------------------------------- the registry


def _address(raw: Any, what: str) -> str:
    if not isinstance(raw, str) or not is_address(raw):
        raise RouteError(f"{what} is not an EVM address: {raw!r}")
    return to_checksum_address(raw)


def _int(raw: Any, what: str, *, minimum: int = 0) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError) as e:
        raise RouteError(f"{what} is not an integer: {raw!r}") from e
    if v < minimum:
        raise RouteError(f"{what} must be ≥ {minimum} (got {v})")
    return v


def _pool_key(raw: Any, what: str) -> PoolKey:
    if not isinstance(raw, dict):
        raise RouteError(f"{what} must be an object with currency0/currency1/fee/tickSpacing/hooks")
    key = PoolKey(
        currency0=_address(raw.get("currency0"), f"{what}.currency0"),
        currency1=_address(raw.get("currency1"), f"{what}.currency1"),
        fee=_int(raw.get("fee"), f"{what}.fee"),
        tick_spacing=_int(raw.get("tickSpacing", raw.get("tick_spacing")), f"{what}.tickSpacing", minimum=1),
        hooks=_address(raw.get("hooks", NATIVE), f"{what}.hooks"),
    )
    # v4 sorts the pair; an unsorted key is a DIFFERENT pool id from the one that exists, so it
    # would quote nothing and revert on-chain.
    if int(key.currency0, 16) >= int(key.currency1, 16):
        raise RouteError(f"{what}: currency0 must be strictly below currency1 (Uniswap sorts the pair)")
    return key


def _route(raw: Any, hook: str) -> Route:
    if not isinstance(raw, dict):
        raise RouteError("every entry of PGAS_UNISWAP_POOLS must be an object")
    token_in = _address(raw.get("token_in", NATIVE), "token_in")
    gateway = _pool_key(raw.get("gateway_pool_key"), "gateway_pool_key")
    inner = _pool_key(raw.get("inner_pool_key"), "inner_pool_key")
    zero_for_one = bool(raw.get("zero_for_one", True))
    target = str(raw.get("target") or "ETH").upper()
    try:
        asset = get_asset(target)
    except KeyError as e:
        raise RouteError(str(e.args[0])) from e

    # Everything below is what the CONTRACTS require. A row that fails one of them can only ever
    # produce calldata the chain refuses, so it is a configuration error and not a route.
    if gateway.hooks != hook:
        raise RouteError(
            f"gateway_pool_key.hooks {gateway.hooks} is not the configured hook {hook} — "
            "PgasRouter.deposit refuses a pool that is not ours"
        )
    if inner.hooks != NATIVE:
        raise RouteError(
            "inner_pool_key.hooks must be the zero address: the inner pool is the canonical "
            "HOOK-LESS pool the swap really happens on"
        )
    if (gateway.currency0, gateway.currency1) != (inner.currency0, inner.currency1):
        raise RouteError("the gateway and inner pools must name the same pair, in the same order")
    want_in = gateway.currency0 if zero_for_one else gateway.currency1
    if token_in != want_in:
        raise RouteError(
            f"token_in {token_in} is not the input side of the gateway pool for "
            f"zero_for_one={zero_for_one} (that is {want_in})"
        )
    want_out = gateway.currency1 if zero_for_one else gateway.currency0
    if want_out.lower() != asset.token.lower():
        raise RouteError(
            f"the pool's output {want_out} is not the {asset.key} pipe's token {asset.token}"
        )
    # `token_in == the target asset` needs no check of its own: the pair is sorted and distinct,
    # so a route whose OUTPUT is the asset's token can never have it as the input as well. Such
    # a deposit is mode "direct" and never reaches here.

    max_deposit = _int(raw.get("max_deposit_units"), "max_deposit_units", minimum=1)
    min_deposit = _int(raw.get("min_deposit_units", 1), "min_deposit_units", minimum=1)
    if min_deposit > max_deposit:
        raise RouteError("min_deposit_units is above max_deposit_units")
    if max_deposit > MAX_UINT128:
        raise RouteError("max_deposit_units does not fit the Quoter's uint128 exactAmount")
    return Route(
        token_in=token_in,
        symbol=str(raw.get("symbol") or ""),
        decimals=_int(raw.get("decimals", 18), "decimals"),
        gateway=gateway,
        inner=inner,
        zero_for_one=zero_for_one,
        target=asset.key,
        max_deposit_units=max_deposit,
        min_deposit_units=min_deposit,
        min_relayer_fee_units=_int(raw.get("min_relayer_fee_units", 0), "min_relayer_fee_units"),
        max_relayer_fee_bps=_int(raw.get("max_relayer_fee_bps", 0), "max_relayer_fee_bps"),
    )


_cache: dict[str, Any] = {"text": None, "hook": None, "routes": {}}


def clear_cache() -> None:
    _cache.update({"text": None, "hook": None, "routes": {}})


def routes() -> dict[str, Route]:
    """"<token_in lowercased>:<target>" → Route, parsed from PGAS_UNISWAP_POOLS.

    Raises RouteError on a registry that cannot be trusted — one bad row takes the WHOLE
    registry down rather than quietly serving the rest, because "the pair you asked for is
    missing" and "the pair you asked for is misconfigured" must never look the same."""
    text = settings.uniswap_pools or "[]"
    hook = _address(settings.uniswap_hook, "PGAS_UNISWAP_HOOK") if settings.uniswap_hook else ""
    if _cache["text"] == text and _cache["hook"] == hook:
        return _cache["routes"]
    if not hook:
        raise RouteError("PGAS_UNISWAP_HOOK is not set")
    try:
        rows = json.loads(text)
    except ValueError as e:
        raise RouteError(f"PGAS_UNISWAP_POOLS is not valid JSON: {e}") from e
    if not isinstance(rows, list):
        raise RouteError("PGAS_UNISWAP_POOLS must be a JSON array")
    out: dict[str, Route] = {}
    for i, raw in enumerate(rows):
        try:
            r = _route(raw, hook)
        except RouteError as e:
            raise RouteError(f"PGAS_UNISWAP_POOLS[{i}]: {e}") from e
        key = f"{r.token_in.lower()}:{r.target}"
        if key in out:
            raise RouteError(
                f"PGAS_UNISWAP_POOLS[{i}]: {r.symbol or r.token_in} → {r.target} is registered twice"
            )
        out[key] = r
    _cache.update({"text": text, "hook": hook, "routes": out})
    return out


def route_for(token_in: str, target: str) -> Route | None:
    """The registered route for this pair, or None. Raises RouteError only when the registry
    itself cannot be read — "no such pair" and "the registry is broken" are different answers."""
    if not is_address(token_in or ""):
        return None
    key = f"{to_checksum_address(token_in).lower()}:{(target or '').upper()}"
    return routes().get(key)


def ingress_flags() -> dict[str, bool]:
    """Which ingress routes the API will serve — the ONE implementation behind /v1/health,
    /v1/dex/assets and /v1/account (law 9: two implementations of one fact will disagree).

      uniswap  the flag is on AND the hook/router/quoter and registry are usable
      xchain   the cross-chain flag (an unarmed quote still answers an estimate, as before)
      direct   the global arming — `ingress_armed` with a pipe pubkey for some asset

    The client defaults a route to OFF unless the API says otherwise, so this must always be
    stated and must never raise."""
    return {
        "uniswap": configured(),
        "xchain": bool(settings.ingress_xchain),
        "direct": bool(settings.ingress_ready),
    }


def public_tokens() -> list[dict[str, Any]]:
    """The source tokens this route accepts right now, for the client's own token picker:
    [{address, symbol, decimals}], native ether as the zero address.

    NEVER raises and never guesses: with the flag off, the addresses unset or the registry
    unreadable it is an EMPTY list, and the client falls back to its own built-in list. A flag
    the API does not state is a flag that is off."""
    if not configured():
        return []
    try:
        rows = routes()
    except RouteError:  # pragma: no cover — configured() already refused a bad registry
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows.values():
        if r.token_in.lower() in seen:
            continue
        seen.add(r.token_in.lower())
        out.append(
            {
                "address": r.token_in,
                "symbol": r.symbol or ("ETH" if r.native_in else ""),
                "decimals": r.decimals,
            }
        )
    return out


def addresses_ok() -> str:
    """'' when hook, router and quoter are all set and well-formed, else why not."""
    for name in ("uniswap_hook", "uniswap_router", "uniswap_quoter"):
        value = getattr(settings, name, "")
        if not value:
            return f"PGAS_{name.upper()} is not set"
        if not is_address(value):
            return f"PGAS_{name.upper()} is not an EVM address"
    return ""


def configured() -> bool:
    """True when this route could serve a quote: the flag is on, the three addresses are set and
    the registry parses to at least one pair. NEVER raises — it is read by /v1/health, and a
    health endpoint that dies on a bad env tells the operator nothing."""
    if not settings.ingress_uniswap:
        return False
    if addresses_ok():
        return False
    try:
        return bool(routes())
    except RouteError as e:
        log.warning("PGAS_UNISWAP_POOLS is not usable: %s", e)
        return False


# ----------------------------------------------------------------------------- the Quoter read


def quote_calldata(route: Route, amount_in: int) -> str:
    """`quoteExactInputSingle` on the INNER pool — the canonical deep one, which is where the
    price actually comes from. Exact-input only: the hook cannot serve exact-output."""
    if amount_in <= 0 or amount_in > MAX_UINT128:
        raise RouteError("amount does not fit the Quoter's uint128 exactAmount")
    params = (route.inner.as_tuple(), route.zero_for_one, amount_in, b"")
    data = QUOTE_SELECTOR + encode(
        [f"(({POOL_KEY_ABI},bool,uint128,bytes))"], [(params,)]
    )
    return "0x" + data.hex()


def decode_quote_result(raw: str | None) -> int:
    """amountOut from the Quoter's (uint256 amountOut, uint256 gasEstimate). Anything that is
    not two words is an answer we cannot read — never a zero."""
    body = bytes.fromhex((raw or "").removeprefix("0x"))
    if len(body) < 64:
        raise QuoteUnreadable(f"the quoter answered {len(body)} bytes, not (uint256,uint256)")
    amount_out, _gas = decode(["uint256", "uint256"], body[:64])
    return int(amount_out)


async def quote_out(rpc: Any, route: Route, amount_in: int) -> int:
    """The swap output the inner pool would pay for `amount_in`, read through the SAME RPC pool
    (and the same failover) every other chain read in this codebase uses.

    Raises QuoteUnreadable when nobody answered — the caller turns that into 503, because an
    unreadable query is not evidence of anything, least of all of a zero price."""
    quoter = settings.uniswap_quoter
    if not quoter or not is_address(quoter):
        raise RouteError("PGAS_UNISWAP_QUOTER is not set — the price is read, never guessed")
    data = quote_calldata(route, amount_in)
    try:
        res = await rpc.call("eth_call", [{"to": to_checksum_address(quoter), "data": data}, "latest"])
    except ethpipe.RpcError as e:
        raise QuoteUnreadable(f"no Ethereum endpoint answered the quoter ({e})") from e
    out = decode_quote_result(res)
    if out <= 0:
        # A pool initialised with zero liquidity looks real and pays nothing (ETH/DAI 500/10 on
        # mainnet is exactly that). Refuse; never hand back a zero estimate.
        raise RouteError(
            f"the pool quoted no output for {amount_in} units of "
            f"{route.symbol or route.token_in} — there is no depth for this size"
        )
    return out


# ----------------------------------------------------------------------------- the calldata


def hook_data(deposit_ref: str, min_out: int, relayer_fee: int) -> bytes:
    """abi.encode(bytes32 ref, uint256 minOut, uint256 relayerFeeQuote) — exactly what
    `PgasIngressHook._beforeSwap` decodes. The Beam pubkey is deliberately NOT here."""
    ref = bytes.fromhex(deposit_ref.lower().removeprefix("0x"))
    if len(ref) != 32:
        raise RouteError("a deposit ref must be 32 bytes")
    if min_out <= 0 or relayer_fee < 0:
        raise RouteError("minOut must be positive and the relayer fee non-negative")
    return encode(HOOK_DATA_ABI, [ref, min_out, relayer_fee])


def deposit_calldata(
    route: Route, amount_in: int, deposit_ref: str, min_out: int, relayer_fee: int
) -> str:
    """PgasRouter.deposit(PoolKey key, bool zeroForOne, uint256 amountIn, bytes hookData)."""
    if amount_in <= 0:
        raise RouteError("amount must be positive")
    data = DEPOSIT_SELECTOR + encode(
        [POOL_KEY_ABI, "bool", "uint256", "bytes"],
        [
            route.gateway.as_tuple(),
            route.zero_for_one,
            amount_in,
            hook_data(deposit_ref, min_out, relayer_fee),
        ],
    )
    return "0x" + data.hex()


def decode_deposit_calldata(calldata: str) -> dict[str, Any]:
    """The deposit call, read back. Raises ValueError on anything that is not one."""
    raw = bytes.fromhex((calldata or "").removeprefix("0x"))
    if raw[:4] != DEPOSIT_SELECTOR:
        raise ValueError("not a PgasRouter.deposit call")
    key, zero_for_one, amount_in, hd = decode(
        [POOL_KEY_ABI, "bool", "uint256", "bytes"], raw[4:]
    )
    ref, min_out, relayer_fee = decode(HOOK_DATA_ABI, hd)
    return {
        "pool_key": PoolKey(
            currency0=to_checksum_address(key[0]),
            currency1=to_checksum_address(key[1]),
            fee=int(key[2]),
            tick_spacing=int(key[3]),
            hooks=to_checksum_address(key[4]),
        ),
        "zero_for_one": bool(zero_for_one),
        "amount_in": int(amount_in),
        "deposit_ref": "0x" + ref.hex(),
        "min_out": int(min_out),
        "relayer_fee": int(relayer_fee),
    }


def carries_ref(calldata: str, deposit_ref: str) -> bool:
    """True when this calldata is a deposit call carrying EXACTLY this reference. The ref is
    decoded out of hookData, never matched as a substring: a 32-byte value that happens to
    appear inside another field is not the reference the hook will emit."""
    try:
        call = decode_deposit_calldata(calldata)
    except Exception:  # noqa: BLE001 — anything undecodable simply does not carry the ref
        return False
    return call["deposit_ref"].lower() == (deposit_ref or "").lower()


# ----------------------------------------------------------------------------- the hook's log


def decode_pgas_deposit(lg: dict[str, Any]) -> dict[str, Any]:
    topics = lg.get("topics") or []
    if len(topics) < 4:
        raise ValueError("PgasDeposit carries three indexed parameters")
    amount_in, target, value, relayer_fee, pubkey = decode(
        ["uint256", "address", "uint256", "uint256", "bytes"],
        bytes.fromhex(lg["data"].removeprefix("0x")),
    )
    return {
        "ref": "0x" + bytes.fromhex(topics[1].removeprefix("0x")).hex(),
        "payer": to_checksum_address("0x" + topics[2][-40:]),
        "token_in": to_checksum_address("0x" + topics[3][-40:]),
        "amount_in": int(amount_in),
        "target": to_checksum_address(target),
        "value": int(value),
        "relayer_fee": int(relayer_fee),
        "pubkey": pubkey.hex(),
        "address": to_checksum_address(lg["address"]),
        "tx": lg.get("transactionHash"),
        "log_index": ethpipe._int(lg.get("logIndex")),
    }


def find_deposits_in_receipt(
    receipt: dict[str, Any], hook_address: str | None = None
) -> list[dict[str, Any]]:
    """Every PgasDeposit log in this receipt that came from OUR hook. A log with our topic from
    a stranger's contract is not evidence of anything — the address is checked first."""
    hook = (hook_address or settings.uniswap_hook or "").lower()
    if not hook:
        return []
    out: list[dict[str, Any]] = []
    for lg in receipt.get("logs", []) or []:
        if (lg.get("address") or "").lower() != hook:
            continue
        topics = lg.get("topics") or []
        if not topics or topics[0].lower() != PGAS_DEPOSIT_TOPIC:
            continue
        try:
            out.append(decode_pgas_deposit(lg))
        except Exception as e:  # noqa: BLE001 — an undecodable log is not a deposit
            log.warning("PgasDeposit decode failed in %s: %s", lg.get("transactionHash"), e)
    return out


def find_deposit_ref(receipt: dict[str, Any], ref: str, hook_address: str | None = None) -> dict[str, Any] | None:
    want = (ref or "").lower()
    for d in find_deposits_in_receipt(receipt, hook_address):
        if d["ref"].lower() == want:
            return d
    return None


# ----------------------------------------------------------------------------- the value band


def relayer_fee_problem(
    out: int, quote: int, min_relayer_fee: int, max_relayer_fee_bps: int
) -> str | None:
    """The hook's own two bounds on the quoted relayer fee, in Python: '' when it would pass.

    `minRelayerFee` is the route's IMMUTABLE floor — hookData may only raise it — and
    `maxRelayerFeeBps` is the ceiling in bps of the swap output. Both are checked here so a
    quote never hands the user calldata that the hook will revert, and both are pinned to the
    `fee_bounds` rows of the golden vectors the Solidity suite runs (law 9)."""
    if quote < min_relayer_fee:
        return f"the relayer fee {quote} is below this route's floor of {min_relayer_fee}"
    if max_relayer_fee_bps and quote * BPS > out * max_relayer_fee_bps:
        return (
            f"the relayer fee {quote} is above this route's ceiling of "
            f"{max_relayer_fee_bps} bps of {out}"
        )
    return None


def value_band(min_out: int, out_units: int, relayer_fee: int, asset: Asset) -> tuple[int, int]:
    """(lowest, highest) `value` the pipe may log for a deposit quoted like this.

    The FLOOR is `min_out` itself. The hook bounds `value` — what actually lands on Beam — and
    not the gross swap output the relayer fee and the grid tail come out of: it refuses
    `value < minOut`, so nothing under `min_out` can be logged at all. This band used to reason
    from the older rule (refuse `out < minOut`) and put its floor at `split(min_out)`, a whole
    relayer fee plus a grid step lower — a floor that would have credited a lock the hook could
    not have produced. A guard that protects a number nobody is paid is not a guard.

    The CEILING is the quoted output plus `PGAS_UNISWAP_MAX_UPSIDE_BPS`, because the quote was
    measured at quote time and the price moves inside the quote's TTL — but a lock that is
    wildly larger than what we quoted is somebody else's money, and crediting it to this row
    would be the mirror of the amount-divergence bug that credited house money once already.

    The floor is not grid-aligned and does not need to be: what the pipe logs is, and the
    scanner checks that separately.

    A row whose floor sits ABOVE its own ceiling describes no band at all, and this REFUSES
    (`ContradictoryBand`) rather than repairing it. The repair it used to do — `max(hi, lo)` —
    widened the answer instead of narrowing it: an empty band became the single point `min_out`,
    the one value such a row would then have credited, silently, on numbers that contradict
    each other. Fail closed. The scanner catches the ValueError, records the lock as
    unattributed and pages; nobody is credited off a row that cannot state what it expected.

    ONE implementation, called by the scanner and by its tests; the split itself is
    `ethpipe.split_amount`, pinned to the vectors the Solidity side runs."""
    lo = min_out
    ceiling = out_units * (BPS + max(0, int(settings.uniswap_max_upside_bps))) // BPS
    hi, _ = ethpipe.split_amount(max(ceiling, min_out), relayer_fee, asset.grid)
    if hi < lo:
        raise ContradictoryBand(
            f"this quote's floor ({lo}) is above the highest value it could produce ({hi}): "
            f"out={out_units}, relayer_fee={relayer_fee}, grid={asset.grid}, "
            f"upside_bps={max(0, int(settings.uniswap_max_upside_bps))}"
        )
    return lo, hi
