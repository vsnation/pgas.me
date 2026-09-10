// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PgasBase} from "./PgasBase.t.sol";
import {console2} from "forge-std/Test.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";

import {PgasIngressHook} from "../src/PgasIngressHook.sol";
import {PipeSplit} from "../src/libraries/PipeSplit.sol";
import {
    MockPipeNative,
    MockPipeERC20,
    UnderPullingPipeERC20,
    RefundingPipeNative,
    NoDecimalsToken,
    FallbackSink,
    ReceiveOnlySink,
    StorageWritingSink
} from "./mocks/MockPipe.sol";

/// @title PgasRegressionsTest
/// @notice One test per defect found in review. Every one of them failed against the code as it
///         stood and passes against the code as it stands; a fix with no test is a claim.
///
/// @dev The naming convention is deliberate: each test says the RULE it holds, not the bug it came
///      from, so it still reads as a specification once the bug is forgotten.
contract PgasRegressionsTest is PgasBase {
    uint256 internal constant AMOUNT_IN = 1e16; // 0.01 token
    uint256 internal constant FEE_QUOTE = 7e10;

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // D1 / D7 — the grid is derived from the asset, never trusted from the caller
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice D1/D7: `grid = 1` on an 18-decimal target was accepted, and every deposit after it
    ///         would hand the pipe a `value` carrying up to 1e10 wei of tail that Beam's 8 decimals
    ///         cannot represent — unmintable, stuck forever, and unfixable because a route is
    ///         write-once. The real `EthPipe` polices nothing: it has exactly two requires and no
    ///         grid arithmetic, so this floor is the only thing between a deposit and a lost tail.
    function test_registerRouteRefusesAGridTheOutputAssetContradicts() public {
        (, PoolKey memory inner, PoolKey memory gw) = _newPairKeys(18);

        vm.startPrank(owner);
        // The exact mistake: an env-var GRID of 1 on an 18-decimal asset.
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.GridMismatch.selector, 1, 1e10));
        hook.registerRoute(
            gw, inner, address(erc20Pipe), BEAM_PUBKEY, 1, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );

        // And the other direction of the same typo: 1e18 where 1e10 belongs.
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.GridMismatch.selector, 1e18, 1e10));
        hook.registerRoute(
            gw, inner, address(erc20Pipe), BEAM_PUBKEY, 1e18, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );

        // The one grid the asset implies is accepted.
        hook.registerRoute(
            gw, inner, address(erc20Pipe), BEAM_PUBKEY, 1e10, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );
        vm.stopPrank();
        assertEq(hook.routeOf(gw.toId()).grid, 1e10, "the stored grid is the derived one");
    }

    /// @notice D1: the derivation itself, across the decimals this build can meet. Beam is always
    ///         8 decimals, so the grid is `10**(decimals-8)` and 1 below that.
    function test_requiredGridIsDerivedFromTheOutputAssetsDecimals() public {
        assertEq(hook.requiredGrid(native), 1e10, "native ETH is 18 decimals by definition");
        assertEq(hook.requiredGrid(Currency.wrap(address(new MockERC20("d18", "D18", 18)))), 1e10, "18 dec");
        assertEq(hook.requiredGrid(Currency.wrap(address(new MockERC20("d8", "D8", 8)))), 1, "8 dec: no floor");
        assertEq(hook.requiredGrid(Currency.wrap(address(new MockERC20("d6", "D6", 6)))), 1, "6 dec: scales up");
        assertEq(hook.requiredGrid(Currency.wrap(address(new MockERC20("d9", "D9", 9)))), 10, "9 dec");
    }

    /// @notice D1: an asset that will not say what its decimals are cannot have its grid proven, so
    ///         the route is refused rather than assumed. An unreadable query is not evidence.
    function test_registerRouteRefusesAnAssetThatWillNotStateItsDecimals() public {
        NoDecimalsToken t = new NoDecimalsToken();
        Currency c = Currency.wrap(address(t));
        PoolKey memory inner = PoolKey(native, c, 3000, 60, IHooks(address(0)));
        PoolKey memory gw = PoolKey(native, c, 0, 1, IHooks(address(hook)));

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.DecimalsUnreadable.selector, address(t)));
        hook.registerRoute(
            gw, inner, address(erc20Pipe), BEAM_PUBKEY, 1e10, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );
    }

    /// @notice D7: the money-side half of the same rule — what actually reaches the pipe sits on the
    ///         grid the OUTPUT ASSET implies, computed here from `decimals()` and not read back out
    ///         of the route that is under test.
    function test_valueReachingThePipeSitsOnTheGridTheAssetImplies() public {
        uint256 independentGrid = 10 ** (uint256(tokenB.decimals()) - 8); // tokenB is the target
        assertEq(independentGrid, 1e10, "the fixture's target really is 18 decimals");

        vm.prank(user);
        router.deposit{value: AMOUNT_IN}(gwB, true, AMOUNT_IN, hookData(bytes32("grid"), MIN_OUT_ANY, FEE_QUOTE));

        uint256 sentValue = tokenB.balanceOf(address(erc20Pipe));
        assertGt(sentValue, 0, "the pipe was paid");
        // The pipe holds value + relayerFee; the mintable part is what must be on the grid.
        (uint256 value,) = PipeSplit.split(sentValue, FEE_QUOTE, independentGrid);
        assertEq(value % independentGrid, 0, "value sits on the asset's own grid");
        assertEq(hook.routeOf(gwB.toId()).grid, independentGrid, "and the route stores that same grid");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // D2 / D9 — a send is not a delivery
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice D2/D9: an ERC-20 pipe that draws only `value` and leaves the relayer fee standing
    ///         used to return happily, leaving the money in the hook while `PgasDeposit` and
    ///         `NewLocalMessage` both reported the whole sum delivered.
    function test_erc20PipeThatUnderPullsIsRefused() public {
        (MockERC20 t, PoolKey memory inner, PoolKey memory gw) = _newFundedPair(18);
        uint256 snap = vm.snapshotState();

        // Measure the honest run first, from identical state, so the refusal can be asserted with
        // the exact numbers rather than a bare "it reverted".
        MockPipeERC20 good = new MockPipeERC20(address(t));
        _register(gw, inner, address(good), 1e10);
        vm.prank(user);
        router.deposit{value: AMOUNT_IN}(gw, true, AMOUNT_IN, hookData(bytes32("ok"), MIN_OUT_ANY, FEE_QUOTE));
        uint256 out = t.balanceOf(address(good));
        (uint256 value,) = PipeSplit.split(out, FEE_QUOTE, 1e10);

        vm.revertToState(snap);

        UnderPullingPipeERC20 bad = new UnderPullingPipeERC20(address(t));
        _register(gw, inner, address(bad), 1e10);
        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(
                abi.encodeWithSelector(PgasIngressHook.PipeDidNotConsume.selector, address(bad), out, out, out - value)
            )
        );
        router.deposit{value: AMOUNT_IN}(gw, true, AMOUNT_IN, hookData(bytes32("bad"), MIN_OUT_ANY, FEE_QUOTE));

        assertEq(t.balanceOf(address(hook)), 0, "nothing stranded in the hook");
        assertEq(t.balanceOf(address(bad)), 0, "and nothing half-delivered to the pipe");
        assertEq(t.allowance(address(hook), address(bad)), 0, "no standing allowance is left");
    }

    /// @notice D9: the native mirror — a pipe that satisfies `msg.value == value + relayerFee` and
    ///         then refunds the fee. The pipe's own require is not a delivery guarantee either.
    function test_nativePipeThatRefundsIsRefused() public {
        RefundingPipeNative bad = new RefundingPipeNative();
        (MockERC20 t, PoolKey memory inner, PoolKey memory gw) = _newFundedPair(18);
        // token in → native out, so the native branch of `_sendToPipe` is the one exercised.
        vm.prank(owner);
        hook.registerRoute(
            gw, inner, address(bad), BEAM_PUBKEY, 1e10, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false
        );

        t.mint(user, 1 ether);
        vm.startPrank(user);
        t.approve(address(router), type(uint256).max);
        vm.expectRevert();
        router.deposit(gw, false, AMOUNT_IN, hookData(bytes32("refund"), MIN_OUT_ANY, FEE_QUOTE));
        vm.stopPrank();

        assertEq(address(hook).balance, 0, "nothing stranded in the hook");
        assertEq(address(bad).balance, 0, "and nothing part-delivered to the pipe");
        assertEq(bad.msgCount(), 0, "the whole transaction unwound, log included");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // D3 / D6 — the ceiling binds the fee that is PAID, not the fee that is quoted
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice D3/D6: `maxRelayerFeeBps` used to bound `relayerFeeQuote` — a number nobody is paid.
    ///         What the relayer receives is `quote + ((out - quote) mod grid)`, produced AFTER every
    ///         guard had run. Here the quote sits exactly on the ceiling and the grid tail carries
    ///         the realized fee past it; before the fix the deposit went through.
    function test_realizedRelayerFeeIsBoundedNotJustTheQuote() public {
        uint256 snap = vm.snapshotState();

        // What this deposit actually pays out, measured from identical state.
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("m"), MIN_OUT_ANY, FEE_QUOTE));
        uint256 out = nativePipe.lastSent().msgValue;
        vm.revertToState(snap);

        // The largest quote the pre-split ceiling accepts: quote * 10000 <= out * 500.
        uint256 quoteAtCeiling = out * MAX_FEE_BPS / 10_000;
        assertGe(quoteAtCeiling, MIN_FEE, "the quote still clears the route's floor");
        uint256 tail = (out - quoteAtCeiling) % GRID_18;
        assertGt(tail, 0, "this deposit really does have a grid tail to carry");
        uint256 realized = quoteAtCeiling + tail;
        assertGt(realized * 10_000, out * MAX_FEE_BPS, "and the realized fee really is over the ceiling");

        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(
                abi.encodeWithSelector(PgasIngressHook.RealizedFeeAboveCeiling.selector, realized, out, MAX_FEE_BPS)
            )
        );
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("ceil"), MIN_OUT_ANY, quoteAtCeiling));
        assertEq(nativePipe.msgCount(), 0, "and nothing reached the pipe");

        console2.log("D3/D6 out                :", out);
        console2.log("D3/D6 quote at ceiling   :", quoteAtCeiling);
        console2.log("D3/D6 realized fee       :", realized);
        console2.log("D3/D6 realized fee (bps) :", realized * 10_000 / out);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // D8 — a relayer-fee floor that is dust is not a floor, and it must be raisable
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice D8: `minRelayerFee` had no check at all, so `0` was a legal floor — and the real
    ///         `EthPipe` accepts a zero relayer fee, so nothing downstream refuses one either. The
    ///         message then sits in the pipe, un-incentivised, with the money already locked.
    function test_registerRouteRefusesARelayerFloorBelowOneGridStep() public {
        (, PoolKey memory inner, PoolKey memory gw) = _newPairKeys(18);

        vm.startPrank(owner);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.RelayerFeeFloorTooLow.selector, 0, 1e10));
        hook.registerRoute(
            gw, inner, address(erc20Pipe), BEAM_PUBKEY, 1e10, 0, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );

        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.RelayerFeeFloorTooLow.selector, 1e10 - 1, 1e10));
        hook.registerRoute(
            gw, inner, address(erc20Pipe), BEAM_PUBKEY, 1e10, 1e10 - 1, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );

        // One whole grid step is the smallest floor that means anything.
        hook.registerRoute(
            gw, inner, address(erc20Pipe), BEAM_PUBKEY, 1e10, 1e10, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );
        vm.stopPrank();
        assertEq(hook.routeOf(gw.toId()).minRelayerFee, 1e10);
    }

    /// @notice D8: raising the floor is a pure tightening — the split consumes the caller's quote,
    ///         never the floor — so a route registered under today's tariff is not stuck with it.
    ///         Before the fix `tightenRoute` could not touch it and the route was write-once.
    function test_minRelayerFeeCanBeRaisedAndThenBites() public {
        PoolId id = gwA.toId();
        // The quote passes at the registered floor.
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("pre"), MIN_OUT_ANY, FEE_QUOTE));
        assertEq(nativePipe.msgCount(), 1);

        vm.prank(owner);
        hook.tightenRoute(id, MIN_DEPOSIT, MAX_DEPOSIT, MAX_FEE_BPS, FEE_QUOTE + 1);
        assertEq(hook.routeOf(id).minRelayerFee, FEE_QUOTE + 1, "the floor moved up");

        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(
                abi.encodeWithSelector(PgasIngressHook.RelayerFeeBelowFloor.selector, FEE_QUOTE, FEE_QUOTE + 1)
            )
        );
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("post"), MIN_OUT_ANY, FEE_QUOTE));
        assertEq(nativePipe.msgCount(), 1, "the raised floor refuses the old quote");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // R1 — the pipe must be PROVEN to be a pipe, not merely non-zero
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice R1: `registerRoute` checked the pipe for `!= address(0)` and nothing else. A payable
    ///         contract with nothing but a fallback, registered as a native pipe, swallows the
    ///         whole deposit: the ETH leaves the hook so `PipeDidNotConsume` is satisfied,
    ///         `PgasDeposit` is emitted with a value and a fee, and nothing is bridged. The route
    ///         is write-once and the money is not rescuable. It has to be refused at registration.
    function test_registerRouteRefusesAPipeThatAnswersAnyCall() public {
        (, PoolKey memory inner, PoolKey memory gw) = _newPairKeys(18);
        FallbackSink sink = new FallbackSink();

        // The sink is indistinguishable from a pipe by the old test: it is a contract, and it is
        // not the zero address.
        assertGt(address(sink).code.length, 0, "the sink really is a deployed contract");
        assertFalse(hook.pipeRejectsUnknownCalls(address(sink)), "and it answers a selector nobody implements");

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.PipeAnswersAnyCall.selector, address(sink)));
        hook.registerRoute(
            gw, inner, address(sink), BEAM_PUBKEY, 1e10, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false
        );
        assertFalse(hook.isRegistered(gw.toId()), "and no route exists to be tightened or paused");
    }

    /// @notice R1: an EOA — or an address whose contract is not deployed yet — is the same loss with
    ///         no code at all. A native `sendFunds` to it is a plain ETH transfer that succeeds.
    function test_registerRouteRefusesAPipeWithNoCode() public {
        (, PoolKey memory inner, PoolKey memory gw) = _newPairKeys(18);
        address eoa = makeAddr("someone's wallet, pasted into PIPE");

        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.PipeHasNoCode.selector, eoa));
        hook.registerRoute(gw, inner, eoa, BEAM_PUBKEY, 1e10, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false);
    }

    /// @notice R1: what the probe actually decides, stated as narrowly as it is true. It says YES
    ///         for every pipe this suite has — real-shaped or dishonest — so the refusal above is
    ///         about a STATELESS PAYABLE FALLBACK's shape and not a check that refuses everything.
    ///         It does NOT classify sinks: a sink whose fallback writes storage passes it, and the
    ///         KNOWN-PASS line below is here so that fact is read as a limit of the probe rather
    ///         than rediscovered as a surprise. The fork suite asks the same question of the four
    ///         live bridge pipes; identity is answered by the pinned `EXTCODEHASH`, not by this.
    function test_theProbeRefusesAStatelessPayableFallbackAndNotEveryOtherShape() public {
        assertTrue(hook.pipeRejectsUnknownCalls(address(nativePipe)), "the native double");
        assertTrue(hook.pipeRejectsUnknownCalls(address(erc20Pipe)), "the ERC-20 double");
        assertTrue(hook.pipeRejectsUnknownCalls(address(new UnderPullingPipeERC20(address(tokenB)))), "under-puller");
        assertTrue(hook.pipeRejectsUnknownCalls(address(new RefundingPipeNative())), "refunder");
        // `receive()`-only is refused by the probe too, and would have been refused anyway: a
        // `sendFunds` call carries calldata and cannot reach `receive()`.
        assertTrue(hook.pipeRejectsUnknownCalls(address(new ReceiveOnlySink())), "receive-only");

        // The one refusal: a fallback that only takes the money and returns. It answers 0xffffffff
        // under STATICCALL, which no bridge pipe does.
        assertFalse(hook.pipeRejectsUnknownCalls(address(new FallbackSink())), "a stateless payable fallback sink");

        // KNOWN-PASS, and deliberately asserted rather than omitted: this sink is the same loss —
        // it takes `sendFunds{value: out}`, keeps the ETH and bridges nothing — but its fallback
        // WRITES STORAGE, so under the probe's STATICCALL it reverts, and the probe reads a revert
        // as "refuses calls it does not implement". `assertTrue` here is not an endorsement: it is
        // the boundary of what a `view` probe can decide from inside the hook. A future reader must
        // not mistake this line for a refusal, and must not weaken the pinned-codehash check on the
        // theory that the probe already catches sinks — it does not.
        assertTrue(
            hook.pipeRejectsUnknownCalls(address(new StorageWritingSink())),
            "KNOWN-PASS: a storage-writing fallback sink passes the probe (STATICCALL reverts on SSTORE)"
        );
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // D10 — the only price guard on this path may be weak, but it may not be absent
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice D10: `minOut = 0` was legal, and the whole economic safety of a deposit rests on that
    ///         number. There is no independent reference this hook could use instead — a same-block
    ///         front-run moves the very spot price it would read — so the refusal is the fix.
    function test_minOutZeroIsRefused() public {
        vm.prank(user);
        vm.expectRevert(_wrappedHookRevert(abi.encodeWithSelector(PgasIngressHook.NoSlippageBound.selector)));
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("nobound"), 0, FEE_QUOTE));
        assertEq(nativePipe.msgCount(), 0, "and nothing reached the pipe");
    }

    /// @notice D10: what a real bound does. A front-run drains the inner pool, the deposit that
    ///         follows is refused by its own `minOut` — and the same deposit with the weakest legal
    ///         bound goes through at the sandwiched price, which is exactly why the README says a
    ///         bound may be weak and a caller who states `1` has stated nothing.
    function test_minOutBitesAgainstAFrontRun() public {
        uint256 snap = vm.snapshotState();

        // The honest price, measured with no attacker in the block.
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("clean"), MIN_OUT_ANY, FEE_QUOTE));
        uint256 cleanOut = nativePipe.lastSent().msgValue;
        vm.revertToState(snap);

        uint256 bound = cleanOut * 99 / 100; // a 1% tolerance, the shape a quote carries
        _frontRun();

        vm.prank(user);
        vm.expectRevert(); // InsufficientOutput — the sandwiched output is far under the bound
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("sandwich"), bound, FEE_QUOTE));
        assertEq(nativePipe.msgCount(), 0, "the bound refused it");

        // The same deposit with the weakest legal bound is NOT protected — stated plainly rather
        // than pretended away.
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("sandwich"), MIN_OUT_ANY, FEE_QUOTE));
        uint256 sandwichedOut = nativePipe.lastSent().msgValue;
        assertLt(sandwichedOut, bound, "a bound of 1 protects nothing");
        console2.log("D10 clean output      :", cleanOut);
        console2.log("D10 sandwiched output :", sandwichedOut);
        console2.log("D10 taken (bps)       :", (cleanOut - sandwichedOut) * 10_000 / cleanOut);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Helpers
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @dev Pool keys only — `registerRoute` never touches the PoolManager, so a registration test
    ///      does not need a live pool and must not pretend it does.
    function _newPairKeys(uint8 decimals) internal returns (MockERC20 t, PoolKey memory inner, PoolKey memory gw) {
        t = new MockERC20("Target", "TGT", decimals);
        Currency c = Currency.wrap(address(t));
        inner = PoolKey(native, c, 3000, 60, IHooks(address(0)));
        gw = PoolKey(native, c, 0, 1, IHooks(address(hook)));
    }

    /// @dev A funded hook-less inner pool plus its gateway pool, for the tests that move money.
    function _newFundedPair(uint8 decimals) internal returns (MockERC20 t, PoolKey memory inner, PoolKey memory gw) {
        (t, inner, gw) = _newPairKeys(decimals);
        manager.initialize(inner, SQRT_PRICE_1_1);
        t.mint(address(this), 1_000 ether);
        t.approve(address(lpRouter), type(uint256).max);
        lpRouter.modifyLiquidity{value: 100 ether}(
            inner, ModifyLiquidityParams({tickLower: -887220, tickUpper: 887220, liquidityDelta: 10 ether, salt: 0}), ""
        );
        vm.prank(owner);
        manager.initialize(gw, SQRT_PRICE_1_1);
    }

    /// @dev native in → token out, the ERC-20 pipe branch.
    function _register(PoolKey memory gw, PoolKey memory inner, address pipe, uint256 grid) internal {
        vm.prank(owner);
        hook.registerRoute(gw, inner, pipe, BEAM_PUBKEY, grid, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true);
    }

    /// @dev A swap large enough to move the inner pool hard, from a stranger, in front of the user.
    function _frontRun() internal {
        address attacker = makeAddr("front-runner");
        tokenA.mint(attacker, 500 ether);
        vm.startPrank(attacker);
        tokenA.approve(address(swapRouter), type(uint256).max);
        swapRouter.swap(
            innerA,
            SwapParams({
                zeroForOne: false, amountSpecified: -int256(200 ether), sqrtPriceLimitX96: TickMath.MAX_SQRT_PRICE - 1
            }),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            ""
        );
        vm.stopPrank();
    }
}
