// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PgasBase} from "./PgasBase.t.sol";
import {Vm} from "forge-std/Vm.sol";
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
import {MockPipeNative, RevertingPipe, ReentrantPipe} from "./mocks/MockPipe.sol";

/// @title PgasIngressHookTest
/// @notice The offline unit suite. Every refusal is asserted, because a refusal that is not
///         asserted is a refusal that will one day silently stop refusing.
contract PgasIngressHookTest is PgasBase {
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

    uint256 internal constant AMOUNT_IN = 1e16; // 0.01 token
    uint256 internal constant FEE_QUOTE = 7e10;
    /// @dev A bound a real quote would carry: the 1:1 inner pool charges 0.30%, so ~0.9964 of the
    ///      input comes back and 99% of it is a live bound rather than a formality.
    uint256 internal constant MIN_OUT_REAL = AMOUNT_IN * 99 / 100;

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Happy paths
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice ERC-20 source → NATIVE target: the shape v1 ships (a token → bETH).
    function test_happyPath_erc20SourceToNativePipe() public {
        uint256 userNativeBefore = user.balance;

        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("ref-1"), MIN_OUT_REAL, FEE_QUOTE));

        MockPipeNative.Sent memory s = nativePipe.lastSent();
        assertEq(s.caller, address(hook), "the pipe's caller is the hook");
        assertEq(s.value + s.relayerFee, s.msgValue, "value + relayerFee == msg.value exactly");
        assertEq(s.value % GRID_18, 0, "value sits on the grid");
        assertGe(s.relayerFee, FEE_QUOTE, "the tail rides on the quoted fee");
        assertEq(keccak256(s.receiver), keccak256(BEAM_PUBKEY), "our pinned pubkey");

        assertEq(user.balance, userNativeBefore, "the user received nothing");
        assertEq(address(hook).balance, 0, "the hook holds nothing afterwards");
        assertEq(tokenA.balanceOf(address(hook)), 0, "the hook holds no source token");
        assertEq(address(router).balance, 0, "the router holds nothing");
    }

    /// @notice NATIVE source → ERC-20 target: the router's native settle and the hook's ERC-20
    ///         pipe branch, including the exact-then-zero allowance discipline.
    function test_happyPath_nativeSourceToErc20Pipe() public {
        uint256 before = user.balance;

        vm.prank(user);
        router.deposit{value: AMOUNT_IN * 2}(gwB, true, AMOUNT_IN, hookData(bytes32("ref-2"), MIN_OUT_REAL, FEE_QUOTE));

        assertEq(before - user.balance, AMOUNT_IN, "only amountIn was spent; the surplus came back");
        assertEq(erc20Pipe.msgCount(), 1, "the pipe was called once");
        assertGt(tokenB.balanceOf(address(erc20Pipe)), 0, "the pipe pulled the tokens");
        assertEq(tokenB.balanceOf(address(hook)), 0, "the hook holds no target token afterwards");
        assertEq(tokenB.allowance(address(hook), address(erc20Pipe)), 0, "no standing allowance is left");
        assertEq(address(router).balance, 0, "the router holds nothing");
    }

    /// @notice Every field of the attribution log, cross-checked against what the pipe actually
    ///         received. A log that agreed with itself but not with the money would be worthless.
    function test_eventFields() public {
        vm.recordLogs();
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("ref-3"), MIN_OUT_REAL, FEE_QUOTE));

        MockPipeNative.Sent memory s = nativePipe.lastSent();
        (uint256 wantValue, uint256 wantFee) = PipeSplit.split(s.msgValue, FEE_QUOTE, GRID_18);

        Vm.Log[] memory logs = _logs();
        bytes32 topic0 = keccak256("PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)");
        bool seen;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter != address(hook) || logs[i].topics[0] != topic0) continue;
            seen = true;
            assertEq(logs[i].topics[1], bytes32("ref-3"), "ref");
            assertEq(address(uint160(uint256(logs[i].topics[2]))), user, "payer");
            assertEq(address(uint160(uint256(logs[i].topics[3]))), address(tokenA), "tokenIn");
            (uint256 amountIn, address target, uint256 value, uint256 fee, bytes memory pk) =
                abi.decode(logs[i].data, (uint256, address, uint256, uint256, bytes));
            assertEq(amountIn, AMOUNT_IN, "amountIn");
            assertEq(target, address(0), "target is native");
            assertEq(value, wantValue, "value matches PipeSplit");
            assertEq(fee, wantFee, "relayerFee matches PipeSplit");
            assertEq(value, s.value, "the log and the pipe agree on value");
            assertEq(fee, s.relayerFee, "the log and the pipe agree on the fee");
            assertEq(value + fee, s.msgValue, "and together they are the whole swap output");
            assertEq(keccak256(pk), keccak256(BEAM_PUBKEY), "receiverBeamPubkey");
        }
        assertTrue(seen, "PgasDeposit was emitted");
    }

    /// @notice `payer` is the end user behind an allow-listed router, not the router.
    function test_payerIsResolvedThroughTheRouter() public {
        vm.recordLogs();
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("who"), MIN_OUT_ANY, FEE_QUOTE));
        // The event's second topic is the payer.
        bytes32 topic0 = keccak256("PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)");
        bool seen;
        Vm.Log[] memory logs = _logs();
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(hook) && logs[i].topics[0] == topic0) {
                assertEq(address(uint160(uint256(logs[i].topics[2]))), user, "payer == the end user");
                seen = true;
            }
        }
        assertTrue(seen, "PgasDeposit was emitted");
    }

    /// @notice A router that is NOT allow-listed is reported as the payer itself — attribution
    ///         never trusts an arbitrary contract's word about who is behind it.
    function test_payerIsTheCallerWhenTheRouterIsNotAllowListed() public {
        vm.prank(owner);
        hook.setAllowedRouter(address(router), false);

        vm.recordLogs();
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("who2"), MIN_OUT_ANY, FEE_QUOTE));

        bytes32 topic0 = keccak256("PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)");
        Vm.Log[] memory logs = _logs();
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == address(hook) && logs[i].topics[0] == topic0) {
                assertEq(address(uint160(uint256(logs[i].topics[2]))), address(router), "payer == the caller");
            }
        }
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Refusals — one test per guard
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice Exact-output is unreachable for a `beforeSwapReturnDelta` hook and must be refused,
    ///         not silently mis-accounted. Driven through `PoolSwapTest`, since our own router
    ///         only ever builds exact-input calls.
    function test_exactOutputRefused() public {
        vm.prank(user);
        vm.expectRevert();
        swapRouter.swap(
            gwA,
            SwapParams({
                zeroForOne: false, amountSpecified: int256(AMOUNT_IN), sqrtPriceLimitX96: TickMath.MAX_SQRT_PRICE - 1
            }),
            PoolSwapTest.TestSettings({takeClaims: false, settleUsingBurn: false}),
            hookData(bytes32("exact-out"), MIN_OUT_ANY, FEE_QUOTE)
        );
        assertEq(nativePipe.msgCount(), 0, "and nothing reached the pipe");
    }

    /// @notice Swapping the OTHER way on a gateway pool would deliver the wrong asset to the pipe.
    function test_wrongOutputCurrencyRefused() public {
        // Route A is registered for tokenA → native. Swapping native → tokenA must be refused.
        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(abi.encodeWithSelector(PgasIngressHook.WrongOutputCurrency.selector, curA, native))
        );
        router.deposit{value: AMOUNT_IN}(gwA, true, AMOUNT_IN, hookData(bytes32("wrong-dir"), MIN_OUT_ANY, FEE_QUOTE));
    }

    /// @notice A gateway pool with no route is inert.
    function test_unknownRouteRefused() public {
        MockERC20 tokenC = new MockERC20("Other", "OTH", 18);
        PoolKey memory gwC = PoolKey(native, Currency.wrap(address(tokenC)), 0, 1, IHooks(address(hook)));
        vm.prank(owner);
        manager.initialize(gwC, SQRT_PRICE_1_1);

        tokenC.mint(user, 1 ether);
        vm.startPrank(user);
        tokenC.approve(address(router), type(uint256).max);
        vm.expectRevert(_wrappedHookRevert(abi.encodeWithSelector(PgasIngressHook.UnknownRoute.selector, gwC.toId())));
        router.deposit(gwC, false, AMOUNT_IN, hookData(bytes32("no-route"), MIN_OUT_ANY, FEE_QUOTE));
        vm.stopPrank();
    }

    /// @notice The relayer's floor is immutable and `hookData` may only raise it.
    function test_belowMinRelayerFeeRefused() public {
        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(
                abi.encodeWithSelector(PgasIngressHook.RelayerFeeBelowFloor.selector, MIN_FEE - 1, MIN_FEE)
            )
        );
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("low-fee"), MIN_OUT_ANY, MIN_FEE - 1));
    }

    /// @notice Nobody may quote a relayer fee above the route's ceiling.
    function test_aboveMaxRelayerFeeBpsRefused() public {
        // 6% of a ~0.01 output, against a 5% ceiling.
        uint256 absurd = AMOUNT_IN * 6 / 100;
        vm.prank(user);
        vm.expectRevert();
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("fat-fee"), MIN_OUT_ANY, absurd));
        assertEq(nativePipe.msgCount(), 0, "and nothing reached the pipe");
    }

    /// @notice `maxDeposit` bounds the input, from live depth. There is no flat dollar cap, but
    ///         there IS a per-route bound and it must bite.
    function test_aboveMaxDepositRefused() public {
        uint256 tooBig = MAX_DEPOSIT + 1;
        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(
                abi.encodeWithSelector(PgasIngressHook.DepositOutOfRange.selector, tooBig, MIN_DEPOSIT, MAX_DEPOSIT)
            )
        );
        router.deposit(gwA, false, tooBig, hookData(bytes32("too-big"), MIN_OUT_ANY, FEE_QUOTE));
    }

    /// @notice Below `minDeposit` the gas costs more than the deposit is worth.
    function test_belowMinDepositRefused() public {
        uint256 tooSmall = MIN_DEPOSIT - 1;
        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(
                abi.encodeWithSelector(PgasIngressHook.DepositOutOfRange.selector, tooSmall, MIN_DEPOSIT, MAX_DEPOSIT)
            )
        );
        router.deposit(gwA, false, tooSmall, hookData(bytes32("too-small"), MIN_OUT_ANY, FEE_QUOTE));
    }

    /// @notice The kill switch stops a user who already holds signed calldata.
    function test_pausedRefused() public {
        vm.prank(owner);
        hook.setPaused(true);

        vm.prank(user);
        vm.expectRevert(_wrappedHookRevert(abi.encodeWithSelector(PgasIngressHook.IsPaused.selector)));
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("paused"), MIN_OUT_ANY, FEE_QUOTE));

        vm.prank(owner);
        hook.setPaused(false);
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("unpaused"), MIN_OUT_ANY, FEE_QUOTE));
        assertEq(nativePipe.msgCount(), 1, "it works again once unpaused");
    }

    /// @notice `minOut` is the only slippage bound that works on this path.
    function test_minOutEnforced() public {
        vm.prank(user);
        vm.expectRevert();
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("minout"), 100 ether, FEE_QUOTE));
        assertEq(nativePipe.msgCount(), 0, "and nothing reached the pipe");
    }

    /// @notice `minOut` bounds what the user RECEIVES on Beam (`value`), not the gross swap output:
    ///         the relayer fee and the grid tail come out in between, and a guard has to sit at the
    ///         level it protects. Run twice from IDENTICAL state, so the two numbers are the same
    ///         deposit's — a second swap would have moved the inner pool and compared two markets.
    function test_minOutBoundsTheValueDeliveredNotTheGrossOutput() public {
        uint256 snap = vm.snapshotState();

        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("gap"), MIN_OUT_ANY, FEE_QUOTE));
        MockPipeNative.Sent memory s = nativePipe.lastSent();
        uint256 out = s.msgValue;
        uint256 value = s.value;
        assertGt(out, value, "the fee and the tail sit between the two numbers");

        uint256 inTheGap = value + 1; // > value, <= out
        assertLe(inTheGap, out, "the bound really is inside the gap");

        vm.revertToState(snap);
        vm.prank(user);
        vm.expectRevert(
            _wrappedHookRevert(abi.encodeWithSelector(PgasIngressHook.InsufficientOutput.selector, value, inTheGap))
        );
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("gap"), inTheGap, FEE_QUOTE));
        assertEq(nativePipe.msgCount(), 0, "and nothing reached the pipe");
    }

    /// @notice A pipe that reverts unwinds the whole deposit; nothing is left stranded.
    function test_revertingPipeUnwindsEverything() public {
        RevertingPipe bad = new RevertingPipe();
        MockERC20 tokenC = new MockERC20("Other", "OTH", 18);
        PoolKey memory gwC = PoolKey(native, Currency.wrap(address(tokenC)), 0, 1, IHooks(address(hook)));
        PoolKey memory innerC = PoolKey(native, Currency.wrap(address(tokenC)), 3000, 60, IHooks(address(0)));
        manager.initialize(innerC, SQRT_PRICE_1_1);
        tokenC.mint(address(this), 100 ether);
        tokenC.approve(address(lpRouter), type(uint256).max);
        lpRouter.modifyLiquidity{value: 10 ether}(
            innerC, ModifyLiquidityParams({tickLower: -887220, tickUpper: 887220, liquidityDelta: 1 ether, salt: 0}), ""
        );
        vm.startPrank(owner);
        manager.initialize(gwC, SQRT_PRICE_1_1);
        hook.registerRoute(
            gwC, innerC, address(bad), BEAM_PUBKEY, GRID_18, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false
        );
        vm.stopPrank();

        tokenC.mint(user, 1 ether);
        uint256 balBefore = tokenC.balanceOf(user);
        vm.startPrank(user);
        tokenC.approve(address(router), type(uint256).max);
        vm.expectRevert();
        router.deposit(gwC, false, AMOUNT_IN, hookData(bytes32("bad-pipe"), MIN_OUT_ANY, FEE_QUOTE));
        vm.stopPrank();

        assertEq(tokenC.balanceOf(user), balBefore, "the user's tokens never left");
        assertEq(address(hook).balance, 0, "nothing stranded in the hook");
    }

    /// @notice A pipe that calls back mid-deposit hits the re-entrancy guard.
    function test_reentrancyRefused() public {
        ReentrantPipe evil = new ReentrantPipe();
        MockERC20 tokenC = new MockERC20("Other", "OTH", 18);
        PoolKey memory gwC = PoolKey(native, Currency.wrap(address(tokenC)), 0, 1, IHooks(address(hook)));
        PoolKey memory innerC = PoolKey(native, Currency.wrap(address(tokenC)), 3000, 60, IHooks(address(0)));
        manager.initialize(innerC, SQRT_PRICE_1_1);
        tokenC.mint(address(this), 100 ether);
        tokenC.approve(address(lpRouter), type(uint256).max);
        lpRouter.modifyLiquidity{value: 10 ether}(
            innerC, ModifyLiquidityParams({tickLower: -887220, tickUpper: 887220, liquidityDelta: 1 ether, salt: 0}), ""
        );
        vm.startPrank(owner);
        manager.initialize(gwC, SQRT_PRICE_1_1);
        hook.registerRoute(
            gwC, innerC, address(evil), BEAM_PUBKEY, GRID_18, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false
        );
        vm.stopPrank();

        // Re-enter the very same gateway pool from inside the pipe call.
        evil.arm(
            address(router),
            abi.encodeCall(router.deposit, (gwC, false, AMOUNT_IN, hookData(bytes32("re"), MIN_OUT_ANY, FEE_QUOTE)))
        );

        tokenC.mint(user, 1 ether);
        vm.startPrank(user);
        tokenC.approve(address(router), type(uint256).max);
        vm.expectRevert();
        router.deposit(gwC, false, AMOUNT_IN, hookData(bytes32("re"), MIN_OUT_ANY, FEE_QUOTE));
        vm.stopPrank();
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // The pool's own shape
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice Only the owner may open a gateway pool.
    function test_nonOwnerCannotInitialiseAGatewayPool() public {
        MockERC20 tokenC = new MockERC20("Other", "OTH", 18);
        PoolKey memory gwC = PoolKey(native, Currency.wrap(address(tokenC)), 0, 1, IHooks(address(hook)));
        vm.prank(stranger);
        vm.expectRevert();
        manager.initialize(gwC, SQRT_PRICE_1_1);
    }

    /// @notice A gateway pool must be fee 0 / tickSpacing 1 / 1:1, or it is not a gateway pool.
    function test_badGatewayPoolShapeRefused() public {
        MockERC20 tokenC = new MockERC20("Other", "OTH", 18);
        Currency cc = Currency.wrap(address(tokenC));

        vm.startPrank(owner);
        vm.expectRevert();
        manager.initialize(PoolKey(native, cc, 3000, 1, IHooks(address(hook))), SQRT_PRICE_1_1);
        vm.expectRevert();
        manager.initialize(PoolKey(native, cc, 0, 60, IHooks(address(hook))), SQRT_PRICE_1_1);
        vm.expectRevert();
        manager.initialize(PoolKey(native, cc, 0, 1, IHooks(address(hook))), SQRT_PRICE_1_1 + 1);
        vm.stopPrank();
    }

    /// @notice Liquidity can never be added to a gateway pool, by anyone, including the owner.
    function test_liquidityRefused() public {
        ModifyLiquidityParams memory lp =
            ModifyLiquidityParams({tickLower: -100, tickUpper: 100, liquidityDelta: 1 ether, salt: 0});

        vm.expectRevert();
        lpRouter.modifyLiquidity{value: 1 ether}(gwA, lp, "");

        vm.prank(owner);
        vm.expectRevert();
        lpRouter.modifyLiquidity{value: 1 ether}(gwA, lp, "");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // The owner's surface — what it can, and what it must not be able to do
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice A route is write-once: the owner may ADD a destination, never REDIRECT one.
    function test_routeIsWriteOnce() public {
        MockPipeNative other = new MockPipeNative();
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.RouteExists.selector, gwA.toId()));
        hook.registerRoute(
            gwA, innerA, address(other), BEAM_PUBKEY, GRID_18, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false
        );
    }

    /// @notice Only the owner may register a route at all.
    function test_registerRouteOnlyOwner() public {
        MockERC20 tokenC = new MockERC20("Other", "OTH", 18);
        PoolKey memory gwC = PoolKey(native, Currency.wrap(address(tokenC)), 0, 1, IHooks(address(hook)));
        PoolKey memory innerC = PoolKey(native, Currency.wrap(address(tokenC)), 3000, 60, IHooks(address(0)));
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotOwner.selector));
        hook.registerRoute(
            gwC,
            innerC,
            address(nativePipe),
            BEAM_PUBKEY,
            GRID_18,
            MIN_FEE,
            MAX_FEE_BPS,
            MIN_DEPOSIT,
            MAX_DEPOSIT,
            false
        );
    }

    /// @notice A route registration is refused if the inner pool is not the same pair, has hooks,
    ///         or the public key is not 33 bytes.
    function test_registerRouteValidations() public {
        MockERC20 tokenC = new MockERC20("Other", "OTH", 18);
        Currency cc = Currency.wrap(address(tokenC));
        PoolKey memory gwC = PoolKey(native, cc, 0, 1, IHooks(address(hook)));
        PoolKey memory innerC = PoolKey(native, cc, 3000, 60, IHooks(address(0)));

        vm.startPrank(owner);
        // inner pool carries a hook → it could call back
        vm.expectRevert();
        hook.registerRoute(
            gwC,
            PoolKey(native, cc, 3000, 60, IHooks(address(hook))),
            address(nativePipe),
            BEAM_PUBKEY,
            GRID_18,
            MIN_FEE,
            MAX_FEE_BPS,
            MIN_DEPOSIT,
            MAX_DEPOSIT,
            false
        );
        // wrong pubkey length
        vm.expectRevert();
        hook.registerRoute(
            gwC, innerC, address(nativePipe), hex"1234", GRID_18, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false
        );
        // zero grid
        vm.expectRevert();
        hook.registerRoute(
            gwC, innerC, address(nativePipe), BEAM_PUBKEY, 0, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, false
        );
        // maxDeposit below minDeposit
        vm.expectRevert();
        hook.registerRoute(gwC, innerC, address(nativePipe), BEAM_PUBKEY, GRID_18, MIN_FEE, MAX_FEE_BPS, 10, 9, false);
        // the gateway key must name THIS hook
        vm.expectRevert();
        hook.registerRoute(
            PoolKey(native, cc, 0, 1, IHooks(address(0))),
            innerC,
            address(nativePipe),
            BEAM_PUBKEY,
            GRID_18,
            MIN_FEE,
            MAX_FEE_BPS,
            MIN_DEPOSIT,
            MAX_DEPOSIT,
            false
        );
        vm.stopPrank();
    }

    /// @notice Tightening may only tighten — including the relayer-fee FLOOR, which is a
    ///         tightening by the same argument as `minDeposit`: the split consumes the caller's
    ///         quote, never the floor, so raising it can only refuse more.
    function test_tightenOnlyTightens() public {
        PoolId id = gwA.toId();
        vm.startPrank(owner);
        // raise the floor, lower the cap, lower the fee ceiling, raise the relayer floor — fine
        hook.tightenRoute(id, MIN_DEPOSIT * 2, MAX_DEPOSIT / 2, MAX_FEE_BPS - 100, MIN_FEE * 2);
        PgasIngressHook.Route memory r = hook.routeOf(id);
        assertEq(r.minDeposit, MIN_DEPOSIT * 2);
        assertEq(r.maxDeposit, MAX_DEPOSIT / 2);
        assertEq(r.maxRelayerFeeBps, MAX_FEE_BPS - 100);
        assertEq(r.minRelayerFee, MIN_FEE * 2);

        // every loosening is refused
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotATightening.selector));
        hook.tightenRoute(id, MIN_DEPOSIT, MAX_DEPOSIT / 2, MAX_FEE_BPS - 100, MIN_FEE * 2);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotATightening.selector));
        hook.tightenRoute(id, MIN_DEPOSIT * 2, MAX_DEPOSIT, MAX_FEE_BPS - 100, MIN_FEE * 2);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotATightening.selector));
        hook.tightenRoute(id, MIN_DEPOSIT * 2, MAX_DEPOSIT / 2, MAX_FEE_BPS, MIN_FEE * 2);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotATightening.selector));
        hook.tightenRoute(id, MIN_DEPOSIT * 2, MAX_DEPOSIT / 2, MAX_FEE_BPS - 100, MIN_FEE * 2 - 1);
        vm.stopPrank();

        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotOwner.selector));
        hook.tightenRoute(id, MIN_DEPOSIT * 2, MAX_DEPOSIT / 2, MAX_FEE_BPS - 100, MIN_FEE * 2);
    }

    /// @notice The Beam public key is fixed at registration; there is no setter, and `hookData`
    ///         cannot supply one. This asserts the pinned key is what actually reaches the pipe.
    function test_beamPubkeyIsPinnedNotCallerSupplied() public {
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("pk"), MIN_OUT_ANY, FEE_QUOTE));
        assertEq(keccak256(nativePipe.lastSent().receiver), keccak256(BEAM_PUBKEY));
        assertEq(keccak256(hook.routeOf(gwA.toId()).beamPubkey), keccak256(BEAM_PUBKEY));
    }

    /// @notice Pause / rescue / ownership are the owner's alone.
    function test_ownerOnlySurface() public {
        vm.startPrank(stranger);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotOwner.selector));
        hook.setPaused(true);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotOwner.selector));
        hook.rescue(native, stranger);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotOwner.selector));
        hook.transferOwnership(stranger);
        vm.expectRevert(abi.encodeWithSelector(PgasIngressHook.NotOwner.selector));
        hook.setAllowedRouter(stranger, true);
        vm.stopPrank();
    }

    /// @notice `rescue` sweeps stranded dust, and only stranded dust: the hook holds a balance
    ///         only inside one transaction, so a resting balance is by definition an accident.
    function test_rescue() public {
        vm.deal(address(hook), 5 ether);
        tokenA.mint(address(hook), 3 ether);

        vm.startPrank(owner);
        uint256 got = hook.rescue(native, owner);
        assertEq(got, 5 ether);
        assertEq(owner.balance, 5 ether);
        uint256 gotToken = hook.rescue(curA, owner);
        assertEq(gotToken, 3 ether);
        assertEq(tokenA.balanceOf(owner), 3 ether);
        vm.stopPrank();
        assertEq(address(hook).balance, 0);
    }

    function test_transferOwnership() public {
        vm.prank(owner);
        hook.transferOwnership(stranger);
        assertEq(hook.owner(), stranger);
        vm.prank(stranger);
        hook.setPaused(true);
        assertTrue(hook.paused());
    }

    /// @notice The permission mask lives in the address, and the address is checked twice.
    function test_hookAddressCarriesThePermissionMask() public view {
        assertEq(uint256(uint160(address(hook))) & 0x3FFF, 0x2888, "low 14 bits are 0x2888");
        assertEq(hook.hookFlags(), uint160(0x2888), "and the hook says so too");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────

    function _logs() internal returns (Vm.Log[] memory) {
        return vm.getRecordedLogs();
    }
}
