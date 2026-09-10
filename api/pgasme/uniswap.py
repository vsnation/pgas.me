"""The Uniswap V4 ingress: the route registry, the Quoter read, the pool's own liveness, and the
calldata for BOTH shapes this route has had.

TWO STEPS, NOTHING OF OURS DEPLOYED (U2, 2026-09-10 — the shipping shape). Step 1 is a plain
swap on **Uniswap's own Universal Router**: `execute(commands, inputs, deadline)` carrying
V4_SWAP → (SWAP_EXACT_IN_SINGLE on the canonical hook-less pool, SETTLE_ALL, TAKE_ALL), which
pays the output to `msg.sender` — the USER's own wallet. Step 2 is the unchanged `direct`
deposit of what actually arrived. Nothing of ours is at risk in step 1, it is never registered
as a deposit and it is never armed; the only thing that protects the user there is
`amountOutMinimum`, which is the V4 Quoter's answer minus `PGAS_UNISWAP_SLIPPAGE_BPS` and is
NEVER zero.

ONE TRANSACTION, BEHIND `PGAS_UNISWAP_HOOK_ENABLED=0` (the reviewed reference; not deployed).
The user swaps on a Pgas **gateway pool** (zero liquidity, fee 0, our hook); the hook's
`beforeSwap` routes the input through the canonical deep hook-less pool, takes the whole output,
splits it on the bridge's 8-decimal grid and calls the pipe's `sendFunds` in the same
transaction, so the transaction the user signs IS the deposit. Every function below that builds
or reads that shape is unreachable while the flag is off, and its tests are kept.

What this module owns:

  * the registry — `PGAS_UNISWAP_POOLS`, parsed and VALIDATED against what the two-step swap
    requires (the canonical pool must be HOOK-LESS, the pair sorted, `token_in` the input side
    of it and the output side the target asset's own token) and, with the hook flag on, against
    what the contracts require as well (`PgasRouter.deposit` refuses a pool whose hook is not ours;
    `PgasIngressHook.registerRoute` refuses an inner pool that has a hook, or a key whose
    currencies differ from the gateway's). A row that could only ever produce reverting
    calldata is a configuration error, not a route: it raises, and the route refuses;
  * the quote — `quoteExactInputSingle` on the INNER pool through an `eth_call`, never our own
    curve math (§WE-SET-IT-WE-DONT-READ-IT: the price is read, not set). An endpoint that will
    not answer raises `QuoteUnreadable`; the caller answers 503. An unreadable quote is never a
    zero, and a zero is never an estimate;
  * the swap the user signs — `execute(bytes commands, bytes[] inputs, uint256 deadline)` on the
    Universal Router, plus the TWO approvals an ERC-20 input needs before it can work (the
    token's allowance to Permit2, and Permit2's allowance for the router). Both allowances are
    READ, never assumed — but an allowance we could not read is never "it is already approved":
    the approval is offered anyway, because an extra approve costs gas and a missing one costs
    a reverted swap;
  * the pool's liveness — slot0 and liquidity through `extsload` on the PoolManager, read BEFORE
    a price is quoted. A pool that was never initialised quotes nothing and a pool with no
    liquidity quotes nothing useful; both are refusals with a reason, and an unreadable answer
    is a 503 and never a zero;
  * the hook calldata (flag off) — `deposit(PoolKey, zeroForOne, amountIn, hookData)` with
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
import time
from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from eth_utils import is_address, keccak, to_checksum_address

from . import ethpipe, xchain
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
MAX_UINT160 = 2**160 - 1
MAX_UINT48 = 2**48 - 1
HOOK_DATA_ABI = ["bytes32", "uint256", "uint256"]
BPS = 10_000

# ── the two-step route: Uniswap's own Universal Router ────────────────────────────────────────
# Every selector below is computed from its signature and PINNED to the value the deployed
# contract actually answers to (all six read off mainnet with `cast sig` / `cast call`,
# 2026-09-10), so a signature typed differently here fails at import instead of on the money path.
EXECUTE_SIG = "execute(bytes,bytes[],uint256)"
EXECUTE_SELECTOR = keccak(text=EXECUTE_SIG)[:4]
EXECUTE_SELECTOR_EXPECTED = "0x3593564c"

# UniversalRouter `Commands.sol`. Proven on the deployed router 2026-09-10 rather than read from
# a source tree we do not vendor: command 0x3f reverts `InvalidCommandType(63)` while command
# 0x10 reverts `SliceOutOfBounds` — i.e. 0x10 IS dispatched, into the v4 action decoder.
COMMAND_V4_SWAP = 0x10
# v4-periphery `Actions.sol` (vendored at contracts/node_modules/@uniswap/v4-periphery 1.0.3).
ACTION_SWAP_EXACT_IN_SINGLE = 0x06
ACTION_SETTLE_ALL = 0x0C
ACTION_TAKE_ALL = 0x0F

# IV4Router.ExactInputSingleParams{PoolKey poolKey; bool zeroForOne; uint128 amountIn;
#                                  uint128 amountOutMinimum; bytes hookData}
# — a DYNAMIC struct, so `abi.encode(params)` is an offset word then the body, which is exactly
# what `CalldataDecoder.decodeSwapExactInSingleParams` dereferences.
EXACT_IN_SINGLE_ABI = f"({POOL_KEY_ABI},bool,uint128,uint128,bytes)"

# ERC-20 and Permit2, for the two allowances an ERC-20 input needs.
ERC20_ALLOWANCE_SELECTOR = keccak(text="allowance(address,address)")[:4]  # 0xdd62ed3e
ERC20_APPROVE_SELECTOR = keccak(text="approve(address,uint256)")[:4]  # 0x095ea7b3
PERMIT2_ALLOWANCE_SELECTOR = keccak(text="allowance(address,address,address)")[:4]  # 0x927da105
PERMIT2_APPROVE_SELECTOR = keccak(text="approve(address,address,uint160,uint48)")[:4]  # 0x87517c45
# IExtsload on the v4 PoolManager — the ONLY way its pool state is readable from outside.
EXTSLOAD_SELECTOR = keccak(text="extsload(bytes32)")[:4]  # 0x1e2eaeaf
# v4-core `StateLibrary`: pools live in slot 6, and within a pool's state `liquidity` is at +3.
POOLS_SLOT = 6
LIQUIDITY_OFFSET = 3


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
    # The Pgas gateway pool — ONLY with `PGAS_UNISWAP_HOOK_ENABLED=1`. The two-step route has no
    # pool of ours at all, so this is None there and every hook-shaped builder refuses.
    gateway: PoolKey | None
    inner: PoolKey  # the canonical, HOOK-LESS pool: where the swap really happens, either way
    zero_for_one: bool
    target: str  # the target asset key — ETH / DAI / WBTC
    max_deposit_units: int
    min_deposit_units: int
    min_relayer_fee_units: int  # the route's on-chain floor (0 when not configured)
    max_relayer_fee_bps: int  # the route's on-chain ceiling (0 = not configured)

    @property
    def token_out(self) -> str:
        # From the INNER pool in both shapes: `_route` refuses a gateway that names a different
        # pair or a different order, so with the hook on these are the same two addresses. One
        # implementation, so the swap and the deposit can never disagree about which side is out.
        return self.inner.currency1 if self.zero_for_one else self.inner.currency0

    @property
    def native_in(self) -> bool:
        return self.token_in == NATIVE

    def as_json(self) -> dict[str, Any]:
        """What the client is told about the route (addresses and ids only — nothing secret)."""
        if self.gateway is None:
            # the two-step route: Uniswap's own router, the canonical pool, no contract of ours
            return {
                "router": to_checksum_address(settings.uniswap_universal_router),
                "permit2": to_checksum_address(settings.uniswap_permit2),
                "pool_id": self.inner.pool_id,
                "pool_key": self.inner.as_json(),
                "token_in": self.token_in,
                "token_out": self.token_out,
                "fee": self.inner.fee,
                "symbol": self.symbol,
            }
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
    # ⛔ THE GATEWAY POOL EXISTS ONLY FOR THE HOOK SHAPE. With the flag off there is no contract
    # of ours on chain, so requiring one here would refuse every route on a box that never
    # deployed anything — and a `gateway_pool_key` left in an operator's env from the hook era is
    # not a reason to refuse either. It is read (and fully validated) when the flag is on, and
    # ignored when it is off; nothing downstream may reach for it without asking.
    gateway = _pool_key(raw.get("gateway_pool_key"), "gateway_pool_key") if hook else None
    inner = _pool_key(raw.get("inner_pool_key"), "inner_pool_key")
    zero_for_one = bool(raw.get("zero_for_one", True))
    target = str(raw.get("target") or "ETH").upper()
    try:
        asset = get_asset(target)
    except KeyError as e:
        raise RouteError(str(e.args[0])) from e

    # Everything below is what the SWAP requires — and, with the hook on, what the CONTRACTS
    # require as well. A row that fails one of them can only ever produce calldata the chain
    # refuses, so it is a configuration error and not a route.
    if inner.hooks != NATIVE:
        raise RouteError(
            "inner_pool_key.hooks must be the zero address: the inner pool is the canonical "
            "HOOK-LESS pool the swap really happens on"
        )
    if gateway is not None:
        if gateway.hooks != hook:
            raise RouteError(
                f"gateway_pool_key.hooks {gateway.hooks} is not the configured hook {hook} — "
                "PgasRouter.deposit refuses a pool that is not ours"
            )
        if (gateway.currency0, gateway.currency1) != (inner.currency0, inner.currency1):
            raise RouteError(
                "the gateway and inner pools must name the same pair, in the same order"
            )
    want_in = inner.currency0 if zero_for_one else inner.currency1
    if token_in != want_in:
        raise RouteError(
            f"token_in {token_in} is not the input side of the pool for "
            f"zero_for_one={zero_for_one} (that is {want_in})"
        )
    want_out = inner.currency1 if zero_for_one else inner.currency0
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


def hook_enabled() -> bool:
    """Is the ONE-TRANSACTION hook shape the one this box serves?

    Off by default and off on the box: nothing of ours is deployed, so `uniswap` means the
    two-step route. ONE reader, so the registry, the quote and the kill-switch decision can
    never disagree about which shape a `uniswap` quote is (law 9)."""
    return bool(settings.uniswap_hook_enabled)


def routes() -> dict[str, Route]:
    """"<token_in lowercased>:<target>" → Route, parsed from PGAS_UNISWAP_POOLS.

    Raises RouteError on a registry that cannot be trusted — one bad row takes the WHOLE
    registry down rather than quietly serving the rest, because "the pair you asked for is
    missing" and "the pair you asked for is misconfigured" must never look the same."""
    text = settings.uniswap_pools or "[]"
    # "" is the two-step shape's own cache key: with the hook off there is no hook address to
    # validate against, and a flag flip changes what a row MEANS, so it has to change the key.
    hook = ""
    if hook_enabled():
        hook = (
            _address(settings.uniswap_hook, "PGAS_UNISWAP_HOOK") if settings.uniswap_hook else ""
        )
    if _cache["text"] == text and _cache["hook"] == hook:
        return _cache["routes"]
    if hook_enabled() and not hook:
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


def default_route() -> str:
    """Which route `route:"auto"` resolves to — `PGAS_INGRESS_DEFAULT_ROUTE`, CLAMPED to what is
    actually open (T31 D1).

    Clamped, because the client preselects its route control from this value: pointing it at a
    route whose flag is off would offer the user a button that can only answer 409. When the
    preferred route is closed and the other one is open, the open one is the answer; when
    neither is open the preference is stated as it is — the flags beside it already say that
    nothing is selectable, and inventing a third answer would only be a fourth flag.

    Never raises: it is read by /v1/health."""
    want = (settings.ingress_default_route or xchain.MODE).strip().lower()
    is_open = {MODE: configured(), xchain.MODE: bool(settings.ingress_xchain)}
    if is_open.get(want):
        return want
    other = xchain.MODE if want == MODE else MODE
    return other if is_open.get(other) else want


def wants_uniswap(route: str | None) -> bool:
    """Does this client's `route` parameter ask for the Uniswap route?

    `"uniswap"` always does. `"auto"` (and an omitted route, which is auto) does only when
    `default_route()` resolves to it — that is the whole of the toggle: the DEFAULT decides what
    auto means, an explicit route always wins over the default. ONE implementation, so the
    quote path and what /v1/health advertises can never disagree about which route auto picks."""
    r = (route or "auto").strip().lower()
    return r == MODE or (r == "auto" and default_route() == MODE)


def ingress_flags() -> dict[str, Any]:
    """Which ingress routes the API will serve — the ONE implementation behind /v1/health,
    /v1/dex/assets and /v1/account (law 9: two implementations of one fact will disagree).

      uniswap        the flag is on AND the hook/router/quoter and registry are usable
      xchain         the cross-chain flag (an unarmed quote still answers an estimate, as before)
      direct         the global arming — `ingress_armed` with a pipe pubkey for some asset
      default_route  which of the two `route:"auto"` resolves to (see `default_route()`)

    The client defaults a route to OFF unless the API says otherwise, so this must always be
    stated and must never raise."""
    return {
        "uniswap": configured(),
        "xchain": bool(settings.ingress_xchain),
        "direct": bool(settings.ingress_ready),
        "default_route": default_route(),
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


# The addresses each shape actually calls. Named here, once, so `addresses_ok` and every test
# ask the same question: the two-step route touches only Uniswap's own deployments, and the hook
# route additionally needs the two contracts of ours it would be pointing at.
TWO_STEP_ADDRESSES = (
    "uniswap_universal_router",
    "uniswap_permit2",
    "uniswap_pool_manager",
    "uniswap_quoter",
)
HOOK_ADDRESSES = ("uniswap_hook", "uniswap_router", "uniswap_quoter")


def addresses_ok() -> str:
    """'' when every address THIS SHAPE calls is set and well-formed, else why not.

    Shape-specific on purpose: requiring the hook and PgasRouter addresses on a box that
    deployed neither would close the two-step route for a contract it never calls, and not
    requiring the Universal Router would let it build a transaction with an empty `to`."""
    for name in HOOK_ADDRESSES if hook_enabled() else TWO_STEP_ADDRESSES:
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


# ------------------------------------------------------ the pool's own liveness (extsload)


def pool_state_slot(pool_id: str) -> bytes:
    """`keccak256(abi.encodePacked(poolId, POOLS_SLOT))` — where v4-core keeps this pool's state
    (`StateLibrary._getPoolStateSlot`). The PoolManager has no getters: `extsload` is the whole
    read interface, so the slot arithmetic lives on this side and is pinned by the fork test."""
    raw = bytes.fromhex((pool_id or "").removeprefix("0x"))
    if len(raw) != 32:
        raise RouteError("a pool id is 32 bytes")
    return keccak(raw + POOLS_SLOT.to_bytes(32, "big"))


def extsload_calldata(slot: bytes) -> str:
    if len(slot) != 32:
        raise RouteError("a storage slot is 32 bytes")
    return "0x" + (EXTSLOAD_SELECTOR + slot).hex()


async def _extsload(rpc: Any, slot: bytes) -> int:
    manager = settings.uniswap_pool_manager
    if not manager or not is_address(manager):
        raise RouteError("PGAS_UNISWAP_POOL_MANAGER is not set — the pool is read, never assumed")
    try:
        res = await rpc.call(
            "eth_call",
            [
                {"to": to_checksum_address(manager), "data": extsload_calldata(slot)},
                "latest",
            ],
        )
    except ethpipe.RpcError as e:
        raise QuoteUnreadable(f"no Ethereum endpoint answered the PoolManager ({e})") from e
    body = bytes.fromhex((res or "").removeprefix("0x"))
    if len(body) < 32:
        # ⛔ An answer we cannot read is NEVER a zero. A zero here means "this pool was never
        # initialised", which is a refusal with a reason; a short answer means the endpoint said
        # something else entirely, and treating it as zero would refuse a live pool for a reason
        # that is not true.
        raise QuoteUnreadable(f"the PoolManager answered {len(body)} bytes, not a storage word")
    return int.from_bytes(body[:32], "big")


async def pool_state(rpc: Any, key: PoolKey) -> tuple[int, int]:
    """(sqrtPriceX96, liquidity) for this pool, read BEFORE anything is quoted.

    `sqrtPriceX96 == 0` is a pool that was never initialised and `liquidity == 0` is a pool that
    holds nothing — both quote a price that means nothing, and the Quoter's own refusal ("no
    depth for this size") would blame the SIZE for a pool that is simply not there. Raises
    `QuoteUnreadable` when nobody answered; the caller turns that into 503."""
    base = pool_state_slot(key.pool_id)
    slot0 = await _extsload(rpc, base)
    liquidity_slot = ((int.from_bytes(base, "big") + LIQUIDITY_OFFSET) % 2**256).to_bytes(32, "big")
    liquidity = await _extsload(rpc, liquidity_slot)
    # slot0 packs sqrtPriceX96 | tick | protocolFee | lpFee, lowest 160 bits first.
    return slot0 & (2**160 - 1), liquidity & MAX_UINT128


def pool_problem(route: Route, sqrt_price_x96: int, liquidity: int) -> str | None:
    """Why this pool cannot be quoted right now — or None. A refusal names the pair, because
    "no pool" and "no depth" are different things to an operator and to a user."""
    pair = f"{route.symbol or route.token_in} → {route.target}"
    if sqrt_price_x96 <= 0:
        return (
            f"the canonical Uniswap pool for {pair} ({route.inner.pool_id}) has never been "
            "initialised on this chain"
        )
    if liquidity <= 0:
        return f"the canonical Uniswap pool for {pair} holds no liquidity right now"
    return None


def price_impact_bps(route: Route, amount_in: int, out_units: int, sqrt_price_x96: int) -> int | None:
    """How far below the pool's MID price this size actually executes, in bps — or None when it
    cannot be computed honestly.

    Measured against `slot0`, which we already read for liveness, so it costs no extra call. It
    INCLUDES the pool's own fee, because the fee is part of the gap between the price on the
    screen and the amount that lands: splitting them would need a second reference read and
    would tell the user a number nobody is paid. Never negative — a quote above mid is the
    rounding of an integer ratio, not a gift."""
    if amount_in <= 0 or out_units <= 0 or sqrt_price_x96 <= 0:
        return None
    sq = sqrt_price_x96 * sqrt_price_x96  # currency1 per currency0, × 2**192
    at_mid = (amount_in * sq) >> 192 if route.zero_for_one else (amount_in << 192) // sq
    if at_mid <= 0:
        return None
    return max(0, (at_mid - out_units) * BPS // at_mid)


# ------------------------------------------------- the two-step swap (Uniswap's own router)


def swap_deadline(now: float | None = None) -> int:
    """When the swap the user is about to sign stops being valid.

    One number for two jobs: it is the router's `deadline` AND the expiry of the Permit2
    allowance the swap needs, because a swap cannot land after its own deadline and an allowance
    that outlives it is a standing permission the user did not ask for. `now` is a parameter so
    the golden vector is a fixed set of bytes and not a clock reading."""
    ttl = max(60, int(settings.uniswap_deadline_s))
    return int(now if now is not None else time.time()) + ttl


def swap_actions(route: Route, amount_in: int, min_out: int) -> tuple[bytes, list[bytes]]:
    """The v4 action list the Universal Router runs inside one `unlock`, and its parameters.

      SWAP_EXACT_IN_SINGLE  the canonical HOOK-LESS pool, exact input, `amountOutMinimum` = the
                            Quoter's answer less the slippage bound
      SETTLE_ALL            pay the input — native from the router's own `msg.value`, an ERC-20
                            pulled from the user through Permit2 (hence the two approvals)
      TAKE_ALL              pay the output to `msgSender()` — the USER's own wallet, never ours

    `TAKE_ALL`'s `minAmount` repeats `min_out` on purpose: the swap's own bound and the take's
    bound are the same fact, so they are the same number, and the router refuses if either is
    missed."""
    if amount_in <= 0 or amount_in > MAX_UINT128:
        raise RouteError("amount does not fit the router's uint128 amountIn")
    # ⛔ `amountOutMinimum` IS THE ONLY THING BETWEEN THE USER AND A SANDWICH on this path: the
    # output goes to their own wallet, so nothing of ours would notice a bad fill. A zero bound
    # is not a loose bound, it is no bound, and it is refused here rather than defaulted.
    if min_out <= 0:
        raise RouteError("amountOutMinimum must be positive — a zero bound is no bound at all")
    if min_out > MAX_UINT128:
        raise RouteError("amountOutMinimum does not fit the router's uint128")
    actions = bytes([ACTION_SWAP_EXACT_IN_SINGLE, ACTION_SETTLE_ALL, ACTION_TAKE_ALL])
    params = [
        encode(
            [EXACT_IN_SINGLE_ABI],
            [(route.inner.as_tuple(), route.zero_for_one, amount_in, min_out, b"")],
        ),
        encode(["address", "uint256"], [route.token_in, amount_in]),
        encode(["address", "uint256"], [route.token_out, min_out]),
    ]
    return actions, params


def swap_calldata(route: Route, amount_in: int, min_out: int, deadline: int) -> str:
    """`UniversalRouter.execute(bytes commands, bytes[] inputs, uint256 deadline)` — the whole of
    step 1. One command, V4_SWAP, whose single input is `abi.encode(actions, params)`."""
    if deadline <= 0:
        raise RouteError("a swap needs a deadline")
    actions, params = swap_actions(route, amount_in, min_out)
    inputs = [encode(["bytes", "bytes[]"], [actions, params])]
    data = EXECUTE_SELECTOR + encode(
        ["bytes", "bytes[]", "uint256"], [bytes([COMMAND_V4_SWAP]), inputs, deadline]
    )
    return "0x" + data.hex()


def decode_swap_calldata(calldata: str) -> dict[str, Any]:
    """The swap, read back out of its own bytes. Raises ValueError on anything that is not one.

    This is what makes the golden vector a shared fact rather than a copy: the API test decodes
    what it built, the fork test executes the same bytes through the real router, and neither
    side gets to describe the swap in its own words."""
    raw = bytes.fromhex((calldata or "").removeprefix("0x"))
    if raw[:4] != EXECUTE_SELECTOR:
        raise ValueError("not a UniversalRouter.execute call")
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], raw[4:])
    if commands != bytes([COMMAND_V4_SWAP]):
        raise ValueError(f"not a single V4_SWAP command: 0x{bytes(commands).hex()}")
    if len(inputs) != 1:
        raise ValueError(f"V4_SWAP takes exactly one input, not {len(inputs)}")
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    want = bytes([ACTION_SWAP_EXACT_IN_SINGLE, ACTION_SETTLE_ALL, ACTION_TAKE_ALL])
    if actions != want or len(params) != 3:
        raise ValueError(f"not a swap/settle/take action list: 0x{bytes(actions).hex()}")
    (swap,) = decode([EXACT_IN_SINGLE_ABI], params[0])
    key, zero_for_one, amount_in, min_out, hook_data = swap
    settle_token, settle_max = decode(["address", "uint256"], params[1])
    take_token, take_min = decode(["address", "uint256"], params[2])
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
        "min_out": int(min_out),
        "hook_data": "0x" + bytes(hook_data).hex(),
        "settle_token": to_checksum_address(settle_token),
        "settle_max": int(settle_max),
        "take_token": to_checksum_address(take_token),
        "take_min": int(take_min),
        "deadline": int(deadline),
    }


# ------------------------------------------------------- the two approvals an ERC-20 needs


@dataclass(frozen=True)
class Allowances:
    """What the chain says the user has already permitted — and whether it said anything."""

    erc20_to_permit2: int
    permit2_amount: int
    permit2_expiration: int
    unreadable: tuple[str, ...] = ()  # why, in words, for the quote's note
    # PER READ, not one flag for both: they are two independent questions and they fail
    # independently. A single `readable` made a Permit2 outage add a token-approval reset the
    # token's own allowance had already ruled out.
    erc20_readable: bool = True
    permit2_readable: bool = True

    @property
    def readable(self) -> bool:
        return not self.unreadable


def erc20_allowance_calldata(owner: str, spender: str) -> str:
    data = ERC20_ALLOWANCE_SELECTOR + encode(
        ["address", "address"], [to_checksum_address(owner), to_checksum_address(spender)]
    )
    return "0x" + data.hex()


def permit2_allowance_calldata(owner: str, token: str, spender: str) -> str:
    data = PERMIT2_ALLOWANCE_SELECTOR + encode(
        ["address", "address", "address"],
        [to_checksum_address(owner), to_checksum_address(token), to_checksum_address(spender)],
    )
    return "0x" + data.hex()


def erc20_approve_calldata(spender: str, amount: int, *, allow_zero: bool = False) -> str:
    """`approve(spender, amount)`. Zero is refused unless asked for explicitly: an approval of
    nothing is almost always a bug, and the ONE place it is deliberate (the USDT-style reset in
    `approvals_for`) says so at the call site."""
    if amount < 0 or (amount == 0 and not allow_zero):
        raise RouteError("an approval is for a positive amount")
    data = ERC20_APPROVE_SELECTOR + encode(
        ["address", "uint256"], [to_checksum_address(spender), amount]
    )
    return "0x" + data.hex()


def permit2_approve_calldata(token: str, spender: str, amount: int, expiration: int) -> str:
    """`Permit2.approve(token, spender, uint160 amount, uint48 expiration)` — the transaction form
    of the permission the Universal Router needs. There is deliberately no off-chain PermitSingle
    here: the signature would have to exist BEFORE the calldata that carries it, and it did not
    (the T31/U2 skeptic finding, 2026-09-10)."""
    if amount <= 0 or amount > MAX_UINT160:
        raise RouteError("a Permit2 allowance is a positive uint160")
    if expiration <= 0 or expiration > MAX_UINT48:
        raise RouteError("a Permit2 expiration is a positive uint48")
    data = PERMIT2_APPROVE_SELECTOR + encode(
        ["address", "address", "uint160", "uint48"],
        [to_checksum_address(token), to_checksum_address(spender), amount, expiration],
    )
    return "0x" + data.hex()


def _one_word(raw: str | None, what: str) -> int:
    """One `uint256` out of an `eth_call` answer. A SHORT answer is refused rather than decoded:
    the ABI decoder raises its own exception type for that, which is not `ValueError`, and a
    caller that catches only the obvious ones turns a malformed answer into a 500 on the quote
    path. Length is checked here so nothing downstream has to know that."""
    body = bytes.fromhex((raw or "").removeprefix("0x"))
    if len(body) < 32:
        raise ValueError(f"{what}: answered {len(body)} bytes, not a word")
    return int.from_bytes(body[:32], "big")


async def read_allowances(rpc: Any, token: str, owner: str) -> Allowances:
    """Both allowances that stand between an ERC-20 input and a swap that works.

    ⛔ AN ALLOWANCE WE COULD NOT READ IS NOT AN ALLOWANCE OF ZERO — and it is not "probably
    fine" either. It comes back as zero with the endpoint named in `unreadable`, so the approval
    is OFFERED and the quote says why: skipping an approval we cannot prove exists produces a
    swap that reverts and a user who paid gas for it, while offering one that already exists
    costs a cheap transaction their wallet will show them. The safe direction is not a
    conclusion about the chain, and the note keeps it from reading as one."""
    permit2 = settings.uniswap_permit2
    router = settings.uniswap_universal_router
    if not is_address(permit2 or "") or not is_address(router or ""):
        raise RouteError("PGAS_UNISWAP_PERMIT2 / PGAS_UNISWAP_UNIVERSAL_ROUTER is not set")
    erc20, p2_amount, p2_expiry = 0, 0, 0
    erc20_ok = permit2_ok = True
    unreadable: list[str] = []
    try:
        res = await rpc.call(
            "eth_call",
            [
                {
                    "to": to_checksum_address(token),
                    "data": erc20_allowance_calldata(owner, permit2),
                },
                "latest",
            ],
        )
        erc20 = _one_word(res, "the token's allowance to Permit2")
    except (ethpipe.RpcError, ValueError, TypeError) as e:
        erc20_ok = False
        unreadable.append(f"token allowance to Permit2 ({e})")
    try:
        res = await rpc.call(
            "eth_call",
            [
                {
                    "to": to_checksum_address(permit2),
                    "data": permit2_allowance_calldata(owner, token, router),
                },
                "latest",
            ],
        )
        body = bytes.fromhex((res or "").removeprefix("0x"))
        if len(body) < 96:
            raise ValueError(f"answered {len(body)} bytes, not (uint160,uint48,uint48)")
        p2_amount, p2_expiry, _nonce = decode(["uint160", "uint48", "uint48"], body[:96])
    except (ethpipe.RpcError, ValueError, TypeError) as e:
        permit2_ok = False
        unreadable.append(f"Permit2 allowance for the router ({e})")
    return Allowances(
        int(erc20), int(p2_amount), int(p2_expiry), tuple(unreadable), erc20_ok, permit2_ok
    )


def approvals_for(
    route: Route, amount_in: int, deadline: int, allow: Allowances
) -> list[dict[str, Any]]:
    """The transactions the user must send BEFORE the swap — in order, and only the short ones.

    Native input needs neither. An ERC-20 needs the token's own allowance to Permit2 (once per
    token) and then Permit2's allowance for the Universal Router, which carries an EXPIRY as
    well as an amount: an allowance that is large enough but has expired is exactly as short as
    one that was never granted, and reading only the amount is how that bug is written.

    ⛔ AND SOME TOKENS REFUSE TO RAISE A NON-ZERO ALLOWANCE. USDT's `approve` is
    `require(!(value != 0 && allowed[msg.sender][spender] != 0))` — a source token this route is
    meant to accept — so an existing-but-short allowance has to be zeroed FIRST or the approval
    itself reverts and the user is stuck with no way forward from the UI. The reset is emitted
    whenever the current allowance is short and NOT KNOWN to be zero, which includes the case
    where nobody would tell us what it is: a spare 29k-gas transaction beats a route that dead
    ends on the one token most likely to be sitting in a fresh wallet."""
    if route.native_in:
        return []
    permit2 = to_checksum_address(settings.uniswap_permit2)
    router = to_checksum_address(settings.uniswap_universal_router)
    out: list[dict[str, Any]] = []
    if allow.erc20_to_permit2 < amount_in:
        if allow.erc20_to_permit2 > 0 or not allow.erc20_readable:
            out.append(
                {
                    "name": "approval_reset",
                    "chain_id": settings.eth_chain_id,
                    "token": route.token_in,
                    "spender": permit2,
                    "amount": "0",
                    "to": route.token_in,
                    "data": erc20_approve_calldata(permit2, 0, allow_zero=True),
                    "value": "0",
                }
            )
        out.append(
            {
                "name": "approval",
                "chain_id": settings.eth_chain_id,
                "token": route.token_in,
                "spender": permit2,
                "amount": str(amount_in),
                "to": route.token_in,
                "data": erc20_approve_calldata(permit2, amount_in),
                "value": "0",
            }
        )
    if allow.permit2_amount < amount_in or allow.permit2_expiration <= deadline:
        out.append(
            {
                "name": "permit_tx",
                "chain_id": settings.eth_chain_id,
                "token": route.token_in,
                "spender": router,
                "amount": str(amount_in),
                "expiration": deadline,
                "to": permit2,
                "data": permit2_approve_calldata(route.token_in, router, amount_in, deadline),
                "value": "0",
            }
        )
    return out


# ------------------------------------------------------- the hook calldata (flag off)


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
