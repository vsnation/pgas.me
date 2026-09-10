// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BaseHook} from "@uniswap/v4-periphery/src/utils/BaseHook.sol";
import {IMsgSender} from "@uniswap/v4-periphery/src/interfaces/IMsgSender.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {ModifyLiquidityParams, SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {BeforeSwapDelta, toBeforeSwapDelta} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SafeCast} from "@uniswap/v4-core/src/libraries/SafeCast.sol";

import {IPipeNative, IPipeERC20} from "./interfaces/IPipe.sol";
import {PipeSplit} from "./libraries/PipeSplit.sol";

/// @title PgasIngressHook
/// @author Pgas.me
/// @notice One transaction: a user swaps a token on a Pgas **gateway pool**, and this hook routes
///         the input through the canonical deep hook-less Uniswap v4 pool and pushes the whole
///         output into the Beam bridge pipe, naming a Beam public key **pinned in the route**.
///         The user never holds the intermediate asset and never signs twice.
///
/// @dev Shape and accounting (exact-input only):
///
/// ```
/// beforeSwap(outerKey, amountSpecified = -amountIn):
///   d   = poolManager.swap(route.inner, same zeroForOne, -amountIn, limit)
///   out = the credit d leaves this hook in the output currency
///   require(out >= minOut)                        // the ONLY usable slippage bound here
///   poolManager.take(outCurrency, address(this), out)
///   pipe.sendFunds{value: out}(value, relayerFee, route.beamPubkey)
///   return toBeforeSwapDelta(+int128(amountIn), 0)
/// ```
///
/// The returned specified delta makes `amountToSwap = -amountIn + amountIn = 0`, so the gateway
/// pool itself never trades (`Pool.swap` early-returns ZERO_DELTA on a zero amount, *before* the
/// sqrt-price-limit check) — which is why a gateway pool is safe with **zero liquidity**.
/// PoolManager then applies +amountIn to this hook's account, netting the inner swap's input debt
/// to zero, and applies -amountIn to the caller, who pays. All four movements land on this
/// contract's PoolManager account and net to zero.
///
/// Exact-output is refused: there the *unspecified* currency is the input, and no return-delta
/// hook can reach the output.
///
/// @dev What the owner can and cannot do. Routes are **write-once per gateway pool id**: the owner
/// can ADD a destination, never REDIRECT one. Afterwards the owner may only *tighten* (raise
/// `minDeposit`, lower `maxDeposit`, lower `maxRelayerFeeBps`), flip `paused`, and `rescue`
/// stranded dust. The Beam public key is fixed at registration and is deliberately NOT taken from
/// `hookData` — hookData is attacker-controlled, and a caller-chosen key would turn this pool into
/// a public bridge front-end with no single destination.
///
/// @dev `paused` is checked immediately before `sendFunds` — the irreversible step — so flipping it
/// halts a chain mid-flight rather than only at its entrance.
contract PgasIngressHook is BaseHook {
    using SafeCast for uint256;

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Constants
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice `sqrtPriceX96` for a 1:1 pool — `2**96`. The only price a gateway pool may start at.
    uint160 internal constant SQRT_PRICE_1_1 = 79228162514264337593543950336;

    /// @notice Basis-point denominator for `maxRelayerFeeBps`.
    uint256 internal constant BPS = 10_000;

    /// @notice The Beam public key is a 33-byte compressed point.
    uint256 internal constant BEAM_PUBKEY_LENGTH = 33;

    /// @notice A selector no bridge pipe implements, used to ask a candidate pipe whether it
    ///         answers calls it cannot possibly serve.
    /// @dev Deliberately NOT `sendFunds`: a prober must never call the way that moves the money.
    ///      Verified against all four live pipes — every one of them reverts on this.
    bytes4 internal constant UNIMPLEMENTED_SELECTOR = 0xffffffff;

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Errors
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice Only `owner` may call.
    error NotOwner();
    /// @notice Deposits are halted by the on-chain kill switch.
    error IsPaused();
    /// @notice Re-entered while a deposit was in flight.
    error Reentrant();
    /// @notice No route is registered for this gateway pool.
    error UnknownRoute(PoolId poolId);
    /// @notice A route already exists for this gateway pool; routes are write-once.
    error RouteExists(PoolId poolId);
    /// @notice Only exact-input swaps can be piped.
    error NotExactInput();
    /// @notice The gateway pool's output currency is not this route's pipe asset.
    error WrongOutputCurrency(Currency got, Currency want);
    /// @notice A gateway pool must be fee 0 / tickSpacing 1 / `sqrtPriceX96 = 2**96`, owner-opened.
    error BadGatewayPool();
    /// @notice Liquidity can never be added to a gateway pool.
    error NoLiquidity();
    /// @notice `amountIn` is outside `[route.minDeposit, route.maxDeposit]`.
    error DepositOutOfRange(uint256 amountIn, uint256 minDeposit, uint256 maxDeposit);
    /// @notice The quoted relayer fee is below the route's immutable floor.
    error RelayerFeeBelowFloor(uint256 quoted, uint256 floorAmount);
    /// @notice The quoted relayer fee exceeds `maxRelayerFeeBps` of the swap output.
    error RelayerFeeAboveCeiling(uint256 quoted, uint256 out, uint256 maxBps);
    /// @notice The inner swap returned less than the caller's `minOut`.
    error InsufficientOutput(uint256 out, uint256 minOut);
    /// @notice The inner pool paid nothing — a pool initialised with zero liquidity looks real.
    error InnerSwapEmpty();
    /// @notice A route parameter is nonsensical at registration time.
    error BadRoute(string what);
    /// @notice A tightening call tried to loosen a bound.
    error NotATightening();
    /// @notice An ERC-20 `approve` did not succeed.
    error ApproveFailed(address token);
    /// @notice The route's `grid` is not the one the OUTPUT ASSET's decimals imply.
    error GridMismatch(uint256 given, uint256 required);
    /// @notice The output asset does not answer `decimals()`, so its grid cannot be proven.
    error DecimalsUnreadable(address token);
    /// @notice `minRelayerFee` is below one grid step — a route whose floor is dust has no floor.
    error RelayerFeeFloorTooLow(uint256 given, uint256 grid);
    /// @notice The fee that ACTUALLY reaches the relayer exceeds `maxRelayerFeeBps` of the output.
    error RealizedFeeAboveCeiling(uint256 relayerFee, uint256 out, uint256 maxBps);
    /// @notice `hookData` carried no slippage bound at all (`minOut == 0`).
    error NoSlippageBound();
    /// @notice Exactly `out` did not leave this contract for the pipe — a send is not a delivery.
    error PipeDidNotConsume(address pipe, uint256 expected, uint256 balanceBefore, uint256 balanceAfter);
    /// @notice The route's `pipe` has no code. A native `sendFunds` to an address with no code is
    ///         a plain ETH transfer: it succeeds, the balance post-condition passes, `PgasDeposit`
    ///         is emitted, and nothing is bridged.
    error PipeHasNoCode(address pipe);
    /// @notice The `pipe` answered a selector it cannot implement, so it answers ANY call — the
    ///         shape of a fallback-only payable sink, which swallows a native deposit whole.
    error PipeAnswersAnyCall(address pipe);

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Types
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @param registered Write-once marker; `registerRoute` refuses a second write.
    /// @param inner The canonical DEEP, HOOK-LESS pool the input is actually swapped on.
    /// @param outCurrency The pipe's asset — must be the gateway pool's output currency.
    /// @param pipe The Beam bridge pipe for `outCurrency`.
    /// @param grid `10**(decimals-8)` for `outCurrency`; 1e10 for ETH/DAI, 1 for WBTC.
    /// @param minRelayerFee IMMUTABLE floor for the quoted relayer fee. `hookData` may only raise it.
    /// @param maxRelayerFeeBps Ceiling on the quoted relayer fee, in bps of the swap output.
    /// @param minDeposit Smallest `amountIn` accepted, in the INPUT currency's units.
    /// @param maxDeposit Largest `amountIn` accepted, in the INPUT currency's units.
    /// @param beamPubkey The 33-byte compressed Beam pipe public key funds are delivered to.
    struct Route {
        bool registered;
        PoolKey inner;
        Currency outCurrency;
        address pipe;
        uint256 grid;
        uint256 minRelayerFee;
        uint256 maxRelayerFeeBps;
        uint256 minDeposit;
        uint256 maxDeposit;
        bytes beamPubkey;
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Events
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice One deposit reached the pipe.
    /// @param ref The server-issued single-use quote reference. A HINT for attribution, never an
    ///        authorisation: what binds a deposit to an account is the API resolving the tx, which
    ///        requires `tx.from == quote.address` as well as this log.
    /// @param payer The end user, resolved through `IMsgSender` when an allow-listed router calls.
    /// @param tokenIn The gateway pool's input currency.
    /// @param amountIn What the payer spent.
    /// @param target The pipe's asset.
    /// @param value What the bridge will mint on Beam — always a multiple of `route.grid`.
    /// @param relayerFee What the relayer is paid; `value + relayerFee` is the whole swap output.
    /// @param receiverBeamPubkey The route's pinned 33-byte Beam public key.
    /// @dev The bridge's own `msgId` is NOT here — `sendFunds` returns nothing. It is carried by
    ///      the pipe's `NewLocalMessage` log in this same receipt, which is the authority on it.
    event PgasDeposit(
        bytes32 indexed ref,
        address indexed payer,
        Currency indexed tokenIn,
        uint256 amountIn,
        Currency target,
        uint256 value,
        uint256 relayerFee,
        bytes receiverBeamPubkey
    );

    event RouteRegistered(PoolId indexed poolId, Currency outCurrency, address pipe, uint256 maxDeposit);
    event RouteTightened(
        PoolId indexed poolId, uint256 minDeposit, uint256 maxDeposit, uint256 maxRelayerFeeBps, uint256 minRelayerFee
    );
    event PausedSet(bool paused);
    event OwnerSet(address indexed previousOwner, address indexed newOwner);
    event RouterAllowed(address indexed router, bool allowed);
    event Rescued(Currency indexed currency, address indexed to, uint256 amount);

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // State
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice A single EOA for the build; a 2-of-3 afterwards. It can pause, tighten and rescue.
    address public owner;

    /// @notice The on-chain kill switch. A file on the server stops the API issuing quotes; only
    ///         this stops a user who already holds signed calldata.
    bool public paused;

    /// @notice Routers whose `msgSender()` is trusted for the `payer` field of `PgasDeposit`.
    mapping(address router => bool allowed) public allowedRouter;

    mapping(PoolId poolId => Route route) private _routes;

    uint256 private _entered;

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Construction
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @param _poolManager The Uniswap v4 PoolManager.
    /// @param _owner The pause / tighten / rescue authority.
    /// @dev The constructor is deliberately thin: everything per-pool lives in `registerRoute`, so
    ///      the creation code — and therefore the mined CREATE2 salt — does not change when a
    ///      route's parameters do.
    constructor(IPoolManager _poolManager, address _owner) BaseHook(_poolManager) {
        if (_owner == address(0)) revert BadRoute("owner");
        owner = _owner;
        emit OwnerSet(address(0), _owner);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier nonReentrant() {
        if (_entered != 0) revert Reentrant();
        _entered = 1;
        _;
        _entered = 0;
    }

    /// @notice `poolManager.take` of native ETH is a bare `call` with empty calldata. Without this
    ///         the take reverts and the whole design is dead. Mandatory, not decorative.
    receive() external payable {}

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Permissions — the address's low 14 bits MUST equal 0x2888
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @inheritdoc BaseHook
    /// @dev beforeInitialize (1<<13 = 0x2000) | beforeAddLiquidity (1<<11 = 0x0800)
    ///      | beforeSwap (1<<7 = 0x0080) | beforeSwapReturnDelta (1<<3 = 0x0008) == 0x2888.
    ///      `Hooks.validateHookPermissions` in BaseHook's constructor makes a wrong address
    ///      un-deployable, and `PoolManager.initialize` re-checks it.
    function getHookPermissions() public pure override returns (Hooks.Permissions memory) {
        return Hooks.Permissions({
            beforeInitialize: true,
            afterInitialize: false,
            beforeAddLiquidity: true,
            afterAddLiquidity: false,
            beforeRemoveLiquidity: false,
            afterRemoveLiquidity: false,
            beforeSwap: true,
            afterSwap: false,
            beforeDonate: false,
            afterDonate: false,
            beforeSwapReturnDelta: true,
            afterSwapReturnDelta: false,
            afterAddLiquidityReturnDelta: false,
            afterRemoveLiquidityReturnDelta: false
        });
    }

    /// @notice The permission mask this hook's address must carry: `0x2888`.
    function hookFlags() public pure returns (uint160) {
        return Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_ADD_LIQUIDITY_FLAG | Hooks.BEFORE_SWAP_FLAG
            | Hooks.BEFORE_SWAP_RETURNS_DELTA_FLAG;
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Owner surface
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice Register a gateway pool → inner pool → pipe route. **Write-once per pool id.**
    /// @param outerKey The gateway pool (this hook, fee 0, tickSpacing 1).
    /// @param innerKey The canonical deep HOOK-LESS pool the swap really happens on.
    /// @param pipe The Beam bridge pipe for the output asset.
    /// @param beamPubkey Our 33-byte Beam pipe public key. Pinned here, never in `hookData`.
    /// @param grid `10**(decimals-8)` for the output asset.
    /// @param minRelayerFee Immutable floor on the quoted relayer fee.
    /// @param maxRelayerFeeBps Ceiling on the quoted relayer fee, in bps of the output.
    /// @param minDeposit / @param maxDeposit Bounds on `amountIn`, in the INPUT currency's units.
    /// @param zeroForOne The one direction this route serves; it fixes the output currency.
    function registerRoute(
        PoolKey calldata outerKey,
        PoolKey calldata innerKey,
        address pipe,
        bytes calldata beamPubkey,
        uint256 grid,
        uint256 minRelayerFee,
        uint256 maxRelayerFeeBps,
        uint256 minDeposit,
        uint256 maxDeposit,
        bool zeroForOne
    ) external onlyOwner {
        PoolId poolId = outerKey.toId();
        Route storage r = _routes[poolId];
        if (r.registered) revert RouteExists(poolId);

        if (address(outerKey.hooks) != address(this)) revert BadRoute("outer.hooks");
        // A hook-less inner pool cannot call back into us, so the inner swap cannot recurse.
        if (address(innerKey.hooks) != address(0)) revert BadRoute("inner.hooks");
        if (Currency.unwrap(outerKey.currency0) != Currency.unwrap(innerKey.currency0)) {
            revert BadRoute("currency0");
        }
        if (Currency.unwrap(outerKey.currency1) != Currency.unwrap(innerKey.currency1)) {
            revert BadRoute("currency1");
        }
        if (pipe == address(0)) revert BadRoute("pipe");
        // The pipe is the one address on a route that receives the money and is never checked
        // again: a route is write-once, and `_sendToPipe`'s post-condition only proves the output
        // LEFT this contract. An address that takes ETH and does nothing satisfies it perfectly.
        // So a candidate pipe must prove, here, that it is at least a contract that refuses calls
        // it cannot serve. See `pipeRejectsUnknownCalls` for exactly what that does and does not
        // prove; the identity of the four known pipes is pinned by bytecode hash in
        // `script/PgasAddresses.sol` and compared by the arming script before the broadcast.
        if (pipe.code.length == 0) revert PipeHasNoCode(pipe);
        if (!pipeRejectsUnknownCalls(pipe)) revert PipeAnswersAnyCall(pipe);
        if (beamPubkey.length != BEAM_PUBKEY_LENGTH) revert BadRoute("beamPubkey");
        if (grid == 0) revert BadRoute("grid");
        if (maxRelayerFeeBps == 0 || maxRelayerFeeBps > BPS) revert BadRoute("maxRelayerFeeBps");
        if (minDeposit == 0 || maxDeposit < minDeposit) revert BadRoute("deposit bounds");

        Currency outCurrency = zeroForOne ? outerKey.currency1 : outerKey.currency0;

        // The grid is NOT a matter of taste and NOT the caller's to choose: it follows from the
        // output asset's decimals, because Beam is always 8. A route is write-once and the bridge
        // polices nothing — the real `EthPipe` accepts an off-grid `value` without complaint — so
        // an environment-variable typo here would hand every future deposit a sub-grid tail that
        // is unmintable on Beam and stuck forever, with no fix short of a new pool and a new route.
        uint256 required = requiredGrid(outCurrency);
        if (grid != required) revert GridMismatch(grid, required);
        // A floor below one grid step is no floor: the bridge accepts a zero relayer fee, and an
        // un-incentivised message sits in the pipe with the money already locked.
        if (minRelayerFee < grid) revert RelayerFeeFloorTooLow(minRelayerFee, grid);

        r.registered = true;
        r.inner = innerKey;
        r.outCurrency = outCurrency;
        r.pipe = pipe;
        r.grid = grid;
        r.minRelayerFee = minRelayerFee;
        r.maxRelayerFeeBps = maxRelayerFeeBps;
        r.minDeposit = minDeposit;
        r.maxDeposit = maxDeposit;
        r.beamPubkey = beamPubkey;

        emit RouteRegistered(poolId, outCurrency, pipe, maxDeposit);
    }

    /// @notice Tighten a live route. Every bound may only move in the restrictive direction.
    /// @dev `minRelayerFee` IS one of them. It was left out on the reasoning that "raising it would
    ///      change what a user pays" — that reasoning is wrong: the split consumes the caller's
    ///      `relayerFeeQuote`, never this floor, so raising the floor can only refuse more quotes.
    ///      It is a pure tightening, by the same argument as `minDeposit`, and a route registered
    ///      with a floor below today's tariff is otherwise stuck with it for the life of the pool.
    function tightenRoute(
        PoolId poolId,
        uint256 minDeposit,
        uint256 maxDeposit,
        uint256 maxRelayerFeeBps,
        uint256 minRelayerFee
    ) external onlyOwner {
        Route storage r = _routes[poolId];
        if (!r.registered) revert UnknownRoute(poolId);
        if (minDeposit < r.minDeposit) revert NotATightening();
        if (maxDeposit > r.maxDeposit) revert NotATightening();
        if (maxRelayerFeeBps > r.maxRelayerFeeBps) revert NotATightening();
        if (minRelayerFee < r.minRelayerFee) revert NotATightening();
        if (maxRelayerFeeBps == 0 || maxDeposit < minDeposit) revert BadRoute("deposit bounds");

        r.minDeposit = minDeposit;
        r.maxDeposit = maxDeposit;
        r.maxRelayerFeeBps = maxRelayerFeeBps;
        r.minRelayerFee = minRelayerFee;
        emit RouteTightened(poolId, minDeposit, maxDeposit, maxRelayerFeeBps, minRelayerFee);
    }

    /// @notice The on-chain kill switch.
    function setPaused(bool p) external onlyOwner {
        paused = p;
        emit PausedSet(p);
    }

    /// @notice Trust `IMsgSender(router).msgSender()` for the `payer` field of `PgasDeposit`.
    /// @dev `payer` is attribution only; the API binds a deposit by `tx.from`, never by this.
    function setAllowedRouter(address router, bool allowed) external onlyOwner {
        allowedRouter[router] = allowed;
        emit RouterAllowed(router, allowed);
    }

    /// @notice Hand the pause / tighten / rescue authority to another address.
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert BadRoute("owner");
        emit OwnerSet(owner, newOwner);
        owner = newOwner;
    }

    /// @notice Sweep stranded dust. The invariant it protects: this hook holds a balance only
    ///         *inside* one transaction, so anything resting here afterwards is an accident.
    /// @return amount What was swept.
    function rescue(Currency currency, address to) external onlyOwner nonReentrant returns (uint256 amount) {
        if (to == address(0)) revert BadRoute("to");
        amount = currency.balanceOfSelf();
        if (amount != 0) currency.transfer(to, amount);
        emit Rescued(currency, to, amount);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Views
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice The whole route for a gateway pool id, `registered == false` if there is none.
    function routeOf(PoolId poolId) external view returns (Route memory) {
        return _routes[poolId];
    }

    /// @notice Whether a gateway pool has a route.
    function isRegistered(PoolId poolId) external view returns (bool) {
        return _routes[poolId].registered;
    }

    /// @notice The ONE grid an output asset may legally have: `10**(decimals-8)`, because Beam is
    ///         always 8 decimals. `registerRoute` derives the route's grid from this, so an
    ///         operator can also read it BEFORE arming and compare it with what they were about to
    ///         pass in.
    /// @dev Native ETH is 18 decimals by definition. An ERC-20 that does not answer `decimals()`
    ///      is REFUSED, not assumed: an unreadable query is not evidence of anything, and the
    ///      thing being proven here is unfixable once a write-once route is wrong.
    function requiredGrid(Currency outCurrency) public view returns (uint256) {
        address token = Currency.unwrap(outCurrency);
        if (token == address(0)) return 1e10; // 10**(18-8)
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSignature("decimals()"));
        if (!ok || ret.length < 32) revert DecimalsUnreadable(token);
        uint256 dec = abi.decode(ret, (uint256));
        if (dec > 36) revert DecimalsUnreadable(token);
        return dec > 8 ? 10 ** (dec - 8) : 1;
    }

    /// @notice Whether `pipe` REFUSES a call to a function it does not implement — which is what
    ///         every real bridge pipe does, and what an address that swallows everything cannot do.
    ///         `registerRoute` demands it, and it is public so a dry run can ask before arming.
    ///
    /// @dev What this proves: `pipe` is not an EOA and not a bare `fallback() payable` sink. That
    ///      matters because the native branch's own post-condition cannot tell the difference — the
    ///      ETH does leave this contract, the balance check passes, `PgasDeposit` is emitted, and
    ///      nothing is bridged. A route is write-once, so there is no second chance at this.
    ///
    /// @dev What this does NOT prove — stated plainly rather than implied away: it does not prove
    ///      `pipe` bridges anything. A contract whose fallback writes storage reverts under
    ///      STATICCALL and would pass here; so would a contract that implements `sendFunds` and
    ///      keeps the money. This catches the ACCIDENT — a wrong address, an EOA, a sink. Identity
    ///      is a different question, answered at a different level: the four known pipes have their
    ///      `EXTCODEHASH` pinned in `script/PgasAddresses.sol` and the arming script compares the
    ///      live code against it before anything is broadcast.
    function pipeRejectsUnknownCalls(address pipe) public view returns (bool) {
        (bool answered,) = pipe.staticcall(abi.encodePacked(UNIMPLEMENTED_SELECTOR));
        return !answered;
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Hook callbacks
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @dev Only the owner may bring a gateway pool into existence, and only in the one shape the
    ///      accounting assumes: fee 0 (nothing is ever traded here), tickSpacing 1, price 1:1.
    function _beforeInitialize(address sender, PoolKey calldata key, uint160 sqrtPriceX96)
        internal
        view
        override
        returns (bytes4)
    {
        if (sender != owner) revert NotOwner();
        if (key.fee != 0 || key.tickSpacing != 1 || sqrtPriceX96 != SQRT_PRICE_1_1) revert BadGatewayPool();
        return IHooks.beforeInitialize.selector;
    }

    /// @dev A gateway pool stays at zero liquidity forever, so nobody can risk capital in it and no
    ///      arbitrageur is ever paid to walk its price back. Removing is impossible once adding is.
    function _beforeAddLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, bytes calldata)
        internal
        pure
        override
        returns (bytes4)
    {
        revert NoLiquidity();
    }

    /// @notice The whole ingress.
    /// @param sender Whoever called `PoolManager.swap` — a router, never the end user.
    /// @param key The gateway pool.
    /// @param params Exact-input only; `amountSpecified` must be negative.
    /// @param hookData `abi.encode(bytes32 ref, uint256 minOut, uint256 relayerFeeQuote)`.
    function _beforeSwap(address sender, PoolKey calldata key, SwapParams calldata params, bytes calldata hookData)
        internal
        override
        nonReentrant
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        PoolId poolId = key.toId();
        Route storage r = _routes[poolId];
        if (!r.registered) revert UnknownRoute(poolId);

        // Exact-output is unreachable for a beforeSwapReturnDelta hook: there the *unspecified*
        // currency is the input, and the return delta lands on the wrong side.
        if (params.amountSpecified >= 0) revert NotExactInput();
        uint256 amountIn = uint256(-params.amountSpecified);
        if (amountIn < r.minDeposit || amountIn > r.maxDeposit) {
            revert DepositOutOfRange(amountIn, r.minDeposit, r.maxDeposit);
        }

        Currency inCurrency = params.zeroForOne ? key.currency0 : key.currency1;
        Currency outCurrency = params.zeroForOne ? key.currency1 : key.currency0;
        if (Currency.unwrap(outCurrency) != Currency.unwrap(r.outCurrency)) {
            revert WrongOutputCurrency(outCurrency, r.outCurrency);
        }

        (bytes32 ref, uint256 minOut, uint256 relayerFeeQuote) = abi.decode(hookData, (bytes32, uint256, uint256));

        // 0) There is no independent price reference available to this hook — a same-block
        //    front-run moves the very spot price it could read — so `minOut` is the ONLY slippage
        //    bound on this path. `minOut == 0` is therefore not "no opinion", it is "no guard at
        //    all", and it is refused: a bound may be weak, it may not be absent.
        if (minOut == 0) revert NoSlippageBound();

        // 1) Route the input through the canonical deep pool. The credit lands on THIS contract's
        //    PoolManager account, because this contract is the `msg.sender` of that swap.
        uint256 out = _innerSwap(r.inner, params.zeroForOne, amountIn);
        if (out < minOut) revert InsufficientOutput(out, minOut);

        // 2) Bound the relayer's quoted tariff. The quote is a price we are given, not a residue we
        //    invent; the hook's job is only to refuse an implausible one.
        if (relayerFeeQuote < r.minRelayerFee) revert RelayerFeeBelowFloor(relayerFeeQuote, r.minRelayerFee);
        if (relayerFeeQuote * BPS > out * r.maxRelayerFeeBps) {
            revert RelayerFeeAboveCeiling(relayerFeeQuote, out, r.maxRelayerFeeBps);
        }

        // 3) Turn the credit into real tokens held by this contract.
        poolManager.take(outCurrency, address(this), out);

        // 4) Floor onto the pipe's 8-decimal grid; the unmintable tail rides on the relayer fee.
        //    There is deliberately NO `value % r.grid != 0` check here. It would read as a
        //    belt-and-braces guard on the number that reaches the bridge and it can never fire:
        //    `PipeSplit` floors by that same `r.grid`, so the remainder is zero by construction
        //    whatever `r.grid` holds — including a wrong one. Dead code that reads as working is
        //    worse than no code. The grid is proven where it can be proven, against the output
        //    asset's own decimals, at registration; and the suite checks the delivered `value`
        //    against a grid it derives itself rather than one read back out of the route.
        (uint256 value, uint256 relayerFee) = PipeSplit.split(out, relayerFeeQuote, r.grid);

        // 5) Re-check the guards on the numbers that actually move. Everything above bounds the
        //    QUOTE and the GROSS output; what reaches the relayer is `relayerFee` (the quote plus
        //    the grid tail) and what reaches the user on Beam is `value`. A guard that protects a
        //    number nobody is paid is not a guard: with a wrong grid the tail alone has run to
        //    thousands of bps under a 500 bps ceiling, every per-leg check passing and right to.
        if (relayerFee * BPS > out * r.maxRelayerFeeBps) {
            revert RealizedFeeAboveCeiling(relayerFee, out, r.maxRelayerFeeBps);
        }
        if (value < minOut) revert InsufficientOutput(value, minOut);

        // 6) The irreversible step. The kill switch is checked HERE, not at the entrance, so that
        //    flipping it halts a chain already in flight.
        if (paused) revert IsPaused();
        _sendToPipe(r, outCurrency, out, value, relayerFee);

        emit PgasDeposit(ref, _payerOf(sender), inCurrency, amountIn, outCurrency, value, relayerFee, r.beamPubkey);

        // 7) +amountIn on the specified (input) currency: the caller pays it, and it nets the inner
        //    swap's input debt on this contract's account to zero. `amountToSwap` becomes zero, so
        //    the zero-liquidity gateway pool never trades.
        return (IHooks.beforeSwap.selector, toBeforeSwapDelta(amountIn.toInt128(), 0), 0);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Internals
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @dev Swap `amountIn` exact-in on the inner pool and return the credit in `outCurrency`.
    ///      A pool initialised with zero liquidity looks real and pays nothing — that is
    ///      `InnerSwapEmpty`, not a silent zero.
    function _innerSwap(PoolKey memory inner, bool zeroForOne, uint256 amountIn) private returns (uint256 out) {
        BalanceDelta d = poolManager.swap(
            inner,
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: -amountIn.toInt256(),
                sqrtPriceLimitX96: zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
            }),
            ""
        );
        int128 outDelta = zeroForOne ? d.amount1() : d.amount0();
        if (outDelta <= 0) revert InnerSwapEmpty();
        out = uint256(uint128(outDelta));
    }

    /// @dev Native today; the ERC-20 branch is here so a DAI/WBTC route is a registration, not a
    ///      redeploy. Allowances are set to the EXACT amount and cleared afterwards — never
    ///      `MaxUint256` — and zeroed first, because USDT reverts on a non-zero → non-zero approve.
    ///
    /// @dev §BROADCAST-IS-NOT-DONE. A call that returned is not a delivery. The native branch used
    ///      to get its proof for free — the pipe's own `msg.value == value + relayerFee` require —
    ///      while the ERC-20 branch had none at any level: an approval the pipe declines to draw in
    ///      full leaves the output resting HERE while `PgasDeposit` and `NewLocalMessage` both say
    ///      it crossed. So both branches are now bracketed by ONE post-condition, in one helper
    ///      reached from every path: exactly `out` left this contract, and nothing came back.
    ///      What this proves is what we can see from here — the hook's invariant that it holds a
    ///      balance only INSIDE one transaction. That the destination's balance ROSE by the same
    ///      `out` is measured against the real pipes in the fork suite, where it can be measured
    ///      rather than assumed.
    function _sendToPipe(Route storage r, Currency outCurrency, uint256 out, uint256 value, uint256 relayerFee)
        private
    {
        address token = Currency.unwrap(outCurrency);
        uint256 balBefore = outCurrency.balanceOfSelf();

        if (token == address(0)) {
            IPipeNative(r.pipe).sendFunds{value: out}(value, relayerFee, r.beamPubkey);
        } else {
            _approveExact(token, r.pipe, 0);
            _approveExact(token, r.pipe, out);
            IPipeERC20(r.pipe).sendFunds(value, relayerFee, r.beamPubkey);
            _approveExact(token, r.pipe, 0);
        }

        uint256 balAfter = outCurrency.balanceOfSelf();
        if (balAfter > balBefore || balBefore - balAfter != out) {
            revert PipeDidNotConsume(r.pipe, out, balBefore, balAfter);
        }
    }

    /// @dev `approve` tolerating a token that returns nothing (USDT).
    function _approveExact(address token, address spender, uint256 amount) private {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSignature("approve(address,uint256)", spender, amount));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert ApproveFailed(token);
    }

    /// @dev The end user behind an allow-listed router. A router that is not allow-listed is
    ///      reported as the payer itself — this field is attribution, never authorisation.
    function _payerOf(address sender) private view returns (address) {
        if (!allowedRouter[sender]) return sender;
        try IMsgSender(sender).msgSender() returns (address p) {
            return p == address(0) ? sender : p;
        } catch {
            return sender;
        }
    }
}
