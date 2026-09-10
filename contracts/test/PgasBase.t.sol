// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";
import {PoolManager} from "@uniswap/v4-core/src/PoolManager.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {PoolSwapTest} from "@uniswap/v4-core/src/test/PoolSwapTest.sol";
import {PoolModifyLiquidityTest} from "@uniswap/v4-core/src/test/PoolModifyLiquidityTest.sol";
import {HookMiner} from "@uniswap/v4-periphery/src/utils/HookMiner.sol";
import {CustomRevert} from "@uniswap/v4-core/src/libraries/CustomRevert.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";

import {PgasIngressHook} from "../src/PgasIngressHook.sol";
import {PgasRouter} from "../src/PgasRouter.sol";
import {MockPipeNative, MockPipeERC20} from "./mocks/MockPipe.sol";

/// @title PgasBase
/// @notice Offline fixture for the unit suite: a fresh PoolManager, two funded hook-less inner
///         pools, two zero-liquidity gateway pools, the hook, the router and both pipe doubles.
///
/// @dev NOTHING here touches a network. There is no fork, no RPC and no live address; a bare
///      `forge test` runs the whole suite offline, which is what makes it safe to run anywhere.
///
/// @dev Two shapes are set up because they exercise opposite halves of the code:
///      * pair A — token in, NATIVE out → the shipping v1 shape (source token → bETH).
///      * pair B — NATIVE in, token out → the router's native settle path and the hook's ERC-20
///        pipe branch (approve exact → sendFunds → approve 0).
abstract contract PgasBase is Test {
    uint160 internal constant SQRT_PRICE_1_1 = 79228162514264337593543950336;
    uint256 internal constant GRID_18 = 1e10;

    PoolManager internal manager;
    PgasIngressHook internal hook;
    PgasRouter internal router;
    PoolSwapTest internal swapRouter;
    PoolModifyLiquidityTest internal lpRouter;

    MockPipeNative internal nativePipe;
    MockPipeERC20 internal erc20Pipe;

    MockERC20 internal tokenA; // pair A, currency1 (source)
    MockERC20 internal tokenB; // pair B, currency1 (target)

    Currency internal native = Currency.wrap(address(0));
    Currency internal curA;
    Currency internal curB;

    PoolKey internal innerA; // native/tokenA 0.30%, hook-less, funded
    PoolKey internal innerB; // native/tokenB 0.30%, hook-less, funded
    PoolKey internal gwA; // gateway: tokenA in → native out
    PoolKey internal gwB; // gateway: native in → tokenB out

    address internal owner = makeAddr("pgas-owner");
    address internal user = makeAddr("pgas-user");
    address internal stranger = makeAddr("stranger");

    bytes internal constant BEAM_PUBKEY = hex"714d04766d1072b67f00913b6d21486dfda79e6e65f841e43ca9aa64f81809d001";

    uint256 internal constant MIN_FEE = 1e10;
    uint256 internal constant MAX_FEE_BPS = 500; // 5%
    uint256 internal constant MIN_DEPOSIT = 1e12;
    uint256 internal constant MAX_DEPOSIT = 5e17;

    /// @notice The WEAKEST bound `minOut` may legally carry — one unit. `minOut == 0` is refused
    ///         outright (`NoSlippageBound`), so every test that is about some OTHER guard still has
    ///         to state a bound; this is that statement, and it deliberately protects nothing. What
    ///         a real bound does is asserted in `test_minOutBitesAgainstAFrontRun`.
    uint256 internal constant MIN_OUT_ANY = 1;

    function setUp() public virtual {
        manager = new PoolManager(address(this));
        swapRouter = new PoolSwapTest(manager);
        lpRouter = new PoolModifyLiquidityTest(manager);

        tokenA = new MockERC20("Source", "SRC", 18);
        tokenB = new MockERC20("Target", "TGT", 18);
        curA = Currency.wrap(address(tokenA));
        curB = Currency.wrap(address(tokenB));

        bytes memory args = abi.encode(IPoolManager(address(manager)), owner);
        (address expected, bytes32 salt) =
            HookMiner.find(address(this), uint160(0x2888), type(PgasIngressHook).creationCode, args);
        hook = new PgasIngressHook{salt: salt}(IPoolManager(address(manager)), owner);
        require(address(hook) == expected, "mining");

        router = new PgasRouter(IPoolManager(address(manager)), address(hook));
        nativePipe = new MockPipeNative();
        erc20Pipe = new MockPipeERC20(address(tokenB));

        vm.prank(owner);
        hook.setAllowedRouter(address(router), true);

        // ── inner pools: hook-less, deep, real liquidity ──────────────────────────────────────
        innerA = PoolKey(native, curA, 3000, 60, IHooks(address(0)));
        innerB = PoolKey(native, curB, 3000, 60, IHooks(address(0)));
        manager.initialize(innerA, SQRT_PRICE_1_1);
        manager.initialize(innerB, SQRT_PRICE_1_1);

        vm.deal(address(this), 1_000 ether);
        tokenA.mint(address(this), 1_000 ether);
        tokenB.mint(address(this), 1_000 ether);
        tokenA.approve(address(lpRouter), type(uint256).max);
        tokenB.approve(address(lpRouter), type(uint256).max);

        ModifyLiquidityParams memory lp =
            ModifyLiquidityParams({tickLower: -887220, tickUpper: 887220, liquidityDelta: 10 ether, salt: 0});
        lpRouter.modifyLiquidity{value: 100 ether}(innerA, lp, "");
        lpRouter.modifyLiquidity{value: 100 ether}(innerB, lp, "");

        // ── gateway pools: fee 0, tickSpacing 1, 1:1, zero liquidity forever ──────────────────
        gwA = PoolKey(native, curA, 0, 1, IHooks(address(hook)));
        gwB = PoolKey(native, curB, 0, 1, IHooks(address(hook)));
        vm.startPrank(owner);
        manager.initialize(gwA, SQRT_PRICE_1_1);
        manager.initialize(gwB, SQRT_PRICE_1_1);

        // Route A: zeroForOne == false → tokenA in, NATIVE out, native pipe.
        hook.registerRoute(
            gwA,
            innerA,
            address(nativePipe),
            BEAM_PUBKEY,
            GRID_18,
            MIN_FEE,
            MAX_FEE_BPS,
            MIN_DEPOSIT,
            MAX_DEPOSIT,
            false
        );
        // Route B: zeroForOne == true → NATIVE in, tokenB out, ERC-20 pipe.
        hook.registerRoute(
            gwB, innerB, address(erc20Pipe), BEAM_PUBKEY, GRID_18, MIN_FEE, MAX_FEE_BPS, MIN_DEPOSIT, MAX_DEPOSIT, true
        );
        vm.stopPrank();

        // ── the user ──────────────────────────────────────────────────────────────────────────
        tokenA.mint(user, 100 ether);
        vm.deal(user, 100 ether);
        vm.startPrank(user);
        tokenA.approve(address(router), type(uint256).max);
        tokenA.approve(address(swapRouter), type(uint256).max);
        vm.stopPrank();

        // Fresh contracts can inherit a stray balance on a fork; keep the invariant explicit here
        // too, so "the hook holds nothing" always means the same thing in both suites.
        vm.deal(address(hook), 0);
        vm.deal(address(router), 0);
    }

    function hookData(bytes32 ref, uint256 minOut, uint256 feeQuote) internal pure returns (bytes memory) {
        return abi.encode(ref, minOut, feeQuote);
    }

    /// @dev v4-core does not let a hook's revert through unchanged: `Hooks.callHook` re-throws it
    ///      as the ERC-7751 `WrappedError(target, selector, reason, details)`. Asserting the wrapped
    ///      shape is what makes "it reverted" into "it reverted FOR THIS REASON" — a bare
    ///      `expectRevert()` would pass just as happily on an out-of-gas or a typo.
    function _wrappedHookRevert(bytes memory inner) internal view returns (bytes memory) {
        return abi.encodeWithSelector(
            CustomRevert.WrappedError.selector,
            address(hook),
            IHooks.beforeSwap.selector,
            inner,
            abi.encodeWithSelector(Hooks.HookCallFailed.selector)
        );
    }

    receive() external payable {}
}
