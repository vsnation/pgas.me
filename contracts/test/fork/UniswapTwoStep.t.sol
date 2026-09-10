// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console2} from "forge-std/Test.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
    function allowance(address, address) external view returns (uint256);
}

interface IPermit2 {
    function DOMAIN_SEPARATOR() external view returns (bytes32);
    function approve(address token, address spender, uint160 amount, uint48 expiration) external;
    function allowance(address user, address token, address spender)
        external
        view
        returns (uint160 amount, uint48 expiration, uint48 nonce);
}

/// @dev `poolManager()` is the DISCRIMINATING view: it is the one function the v4-capable
///      Universal Router has and the pre-v4 one (0x3fC91A3a…) does not.
interface IHasPoolManager {
    function poolManager() external view returns (address);
}

interface IUniversalRouter {
    function execute(bytes calldata commands, bytes[] calldata inputs, uint256 deadline) external payable;
}

interface IV4Quoter {
    struct QuoteExactSingleParams {
        PoolKey poolKey;
        bool zeroForOne;
        uint128 exactAmount;
        bytes hookData;
    }

    function quoteExactInputSingle(QuoteExactSingleParams memory params)
        external
        returns (uint256 amountOut, uint256 gasEstimate);
}

interface IExtsload {
    function extsload(bytes32 slot) external view returns (bytes32);
}

/// @title UniswapTwoStepForkTest
/// @notice THE OTHER HALF OF ONE FACT. `api/tests/test_uniswap_two_step.py` builds the swap the
///         API hands a user's wallet and writes it to `test/vectors/uniswap-two-step.json`; this
///         suite takes those exact bytes and sends them to the REAL Universal Router on a mainnet
///         fork, from a funded and permitted account, and checks the ETH actually lands.
///
/// @dev A Python test that asserted its own encoding would only prove Python agrees with itself.
///      Nothing here re-encodes anything: the calldata is read from the file as `bytes` and used
///      verbatim, and the addresses the API pins are read off the same file and proven on chain.
///
/// @dev Offline elsewhere by construction: runs only under `FOUNDRY_PROFILE=fork` with
///      `FORK_RPC_URL` in the environment (an ARCHIVE endpoint — the pinned block is old enough
///      that a pruned node has no state for it). A bare `forge test` never reaches it.
contract UniswapTwoStepForkTest is Test {
    struct Vector {
        address universalRouter;
        address permit2;
        address quoter;
        address poolManager;
        address tokenIn;
        address tokenOut;
        bool zeroForOne;
        uint24 fee;
        int24 tickSpacing;
        uint256 block_;
        uint256 amountIn;
        uint256 quoterOut;
        uint256 minOut;
        uint256 deadline;
        bytes32 poolId;
        bytes callData;
    }

    Vector internal v;
    address internal user = makeAddr("pgas-two-step-user");

    function setUp() public {
        string memory raw = vm.readFile("./test/vectors/uniswap-two-step.json");
        v.universalRouter = vm.parseJsonAddress(raw, ".universal_router");
        v.permit2 = vm.parseJsonAddress(raw, ".permit2");
        v.quoter = vm.parseJsonAddress(raw, ".quoter");
        v.poolManager = vm.parseJsonAddress(raw, ".pool_manager");
        v.tokenIn = vm.parseJsonAddress(raw, ".token_in");
        v.tokenOut = vm.parseJsonAddress(raw, ".token_out");
        v.zeroForOne = vm.parseJsonBool(raw, ".zero_for_one");
        v.fee = uint24(vm.parseJsonUint(raw, ".pool_key.fee"));
        v.tickSpacing = int24(vm.parseJsonInt(raw, ".pool_key.tickSpacing"));
        v.block_ = vm.parseJsonUint(raw, ".block");
        // amounts travel as decimal STRINGS (the API's own convention, and the only shape that
        // survives a JSON reader without losing precision), so they are parsed, not cast.
        v.amountIn = vm.parseUint(vm.parseJsonString(raw, ".amount_in"));
        v.quoterOut = vm.parseUint(vm.parseJsonString(raw, ".quoter_out"));
        v.minOut = vm.parseUint(vm.parseJsonString(raw, ".min_out"));
        v.deadline = vm.parseJsonUint(raw, ".deadline");
        v.poolId = vm.parseJsonBytes32(raw, ".pool_id");
        v.callData = vm.parseJsonBytes(raw, ".calldata");

        vm.createSelectFork(vm.envOr("FORK_RPC_URL", string("https://eth.drpc.org")), v.block_);
    }

    // ── the addresses the API pins, proven on chain ──────────────────────────────────────────

    /// @notice Code present AND a discriminating view, for each of the four. A pin nobody reads
    ///         back is an assumption; an address with code is not yet the contract you meant.
    function test_theFourPinnedAddressesAreTheContractsWeThinkTheyAre() public view {
        assertGt(v.universalRouter.code.length, 0, "universal router has no code");
        assertGt(v.permit2.code.length, 0, "permit2 has no code");
        assertGt(v.quoter.code.length, 0, "quoter has no code");
        assertGt(v.poolManager.code.length, 0, "pool manager has no code");

        assertEq(
            IHasPoolManager(v.universalRouter).poolManager(),
            v.poolManager,
            "this Universal Router is not the v4-capable one"
        );
        assertEq(IHasPoolManager(v.quoter).poolManager(), v.poolManager, "quoter points elsewhere");
        assertTrue(IPermit2(v.permit2).DOMAIN_SEPARATOR() != bytes32(0), "permit2 has no domain");
    }

    /// @notice The pre-v4 Universal Router does not answer `poolManager()` at all — which is why
    ///         that call is the discriminator above and not decoration.
    function test_thePreV4UniversalRouterWouldNotHavePassedThatCheck() public view {
        address old = 0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD;
        vm.assume(old.code.length > 0);
        (bool ok,) = old.staticcall(abi.encodeWithSignature("poolManager()"));
        assertFalse(ok, "the pre-v4 router answered poolManager() - the discriminator is not one");
    }

    /// @notice Command 0x10 IS `V4_SWAP` in the deployed router: an unknown command reverts
    ///         `InvalidCommandType(commandType)` and 0x10 does not — it gets as far as decoding
    ///         the v4 action payload. The API pins 0x10; this is what makes that a fact.
    function test_commandTenIsV4SwapAndNotAnUnknownCommand() public {
        bytes[] memory empty = new bytes[](1);
        empty[0] = "";

        (bool ok, bytes memory ret) =
            v.universalRouter.call(abi.encodeCall(IUniversalRouter.execute, (hex"3f", empty, v.deadline)));
        assertFalse(ok, "an unknown command must revert");
        assertEq(bytes4(ret), bytes4(keccak256("InvalidCommandType(uint256)")), "expected InvalidCommandType");

        (ok, ret) = v.universalRouter.call(abi.encodeCall(IUniversalRouter.execute, (hex"10", empty, v.deadline)));
        assertFalse(ok, "an empty V4_SWAP input must still revert");
        assertTrue(
            bytes4(ret) != bytes4(keccak256("InvalidCommandType(uint256)")),
            "0x10 is not dispatched as V4_SWAP by this router"
        );
    }

    // ── the pool the API quoted, read the way the API reads it ───────────────────────────────

    /// @notice `PoolId == keccak256(abi.encode(PoolKey))` and the state slot the API computes
    ///         (`keccak256(abi.encodePacked(poolId, 6))`) both name the live pool.
    function test_thePoolIdAndTheStateSlotTheApiComputesAreTheLiveOnes() public view {
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(v.zeroForOne ? v.tokenIn : v.tokenOut),
            currency1: Currency.wrap(v.zeroForOne ? v.tokenOut : v.tokenIn),
            fee: v.fee,
            tickSpacing: v.tickSpacing,
            hooks: IHooks(address(0))
        });
        assertEq(keccak256(abi.encode(key)), v.poolId, "pool id");

        bytes32 stateSlot = keccak256(abi.encodePacked(v.poolId, uint256(6)));
        uint256 slot0 = uint256(IExtsload(v.poolManager).extsload(stateSlot));
        uint256 liquidity = uint256(IExtsload(v.poolManager).extsload(bytes32(uint256(stateSlot) + 3)));
        assertGt(slot0 & type(uint160).max, 0, "sqrtPriceX96 is zero - the pool is not initialised");
        assertGt(liquidity, 0, "the pool holds no liquidity at this block");
    }

    /// @notice The Quoter's own answer at this block is the number the vector was built from —
    ///         so `min_out` is a bound on a measured price and not on a remembered one.
    function test_theQuoterStillAnswersTheNumberTheVectorWasBuiltFrom() public {
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(v.zeroForOne ? v.tokenIn : v.tokenOut),
            currency1: Currency.wrap(v.zeroForOne ? v.tokenOut : v.tokenIn),
            fee: v.fee,
            tickSpacing: v.tickSpacing,
            hooks: IHooks(address(0))
        });
        (uint256 out,) = IV4Quoter(v.quoter).quoteExactInputSingle(
            IV4Quoter.QuoteExactSingleParams({
                poolKey: key,
                zeroForOne: v.zeroForOne,
                exactAmount: uint128(v.amountIn),
                hookData: ""
            })
        );
        assertEq(out, v.quoterOut, "the quoter no longer answers what the vector pinned");
        assertGt(v.minOut, 0, "amountOutMinimum must never be zero");
        assertLt(v.minOut, out, "min_out must sit below the quote, not on it");
    }

    // ── the whole point: the API's own bytes, executed ───────────────────────────────────────

    /// @notice STEP 1, EXACTLY AS THE API HANDS IT OVER. The two approvals, then the verbatim
    ///         calldata, and the output lands in the USER's own wallet — which is the property
    ///         the whole two-step design rests on (`TAKE_ALL` pays `msgSender()`).
    function test_theApiCalldataSwapsAndTheEthLandsInTheUsersOwnWallet() public {
        deal(v.tokenIn, user, v.amountIn);
        assertEq(IERC20(v.tokenIn).balanceOf(user), v.amountIn, "funding");
        uint256 before = user.balance;

        vm.startPrank(user);
        IERC20(v.tokenIn).approve(v.permit2, v.amountIn);
        IPermit2(v.permit2).approve(v.tokenIn, v.universalRouter, uint160(v.amountIn), uint48(v.deadline));
        (bool ok, bytes memory ret) = v.universalRouter.call(v.callData);
        vm.stopPrank();

        if (!ok) {
            console2.logBytes(ret);
            revert("the API's calldata reverted on the real Universal Router");
        }
        uint256 got = user.balance - before;
        assertGe(got, v.minOut, "the ETH that landed is below the quote's own floor");
        assertEq(IERC20(v.tokenIn).balanceOf(user), 0, "the whole input was settled");
        // the router keeps nothing: the output is taken to msgSender in the same unlock
        assertEq(v.universalRouter.balance, 0, "the router kept ether");
        console2.log("50 USDC ->", got, "wei; floor", v.minOut);
    }

    /// @notice WITHOUT THE PERMIT2 ALLOWANCE THE SWAP CANNOT WORK — which is why the API reads
    ///         both allowances and offers the short ones, and why an allowance it could not read
    ///         is never treated as "already approved".
    function test_theSwapNeedsBothAllowancesAndSaysSoByReverting() public {
        deal(v.tokenIn, user, v.amountIn);

        vm.prank(user);
        (bool ok,) = v.universalRouter.call(v.callData);
        assertFalse(ok, "the swap must not work with no allowance at all");

        vm.prank(user);
        IERC20(v.tokenIn).approve(v.permit2, v.amountIn);
        vm.prank(user);
        (ok,) = v.universalRouter.call(v.callData);
        assertFalse(ok, "the token allowance to Permit2 alone is not enough");

        vm.prank(user);
        IPermit2(v.permit2).approve(v.tokenIn, v.universalRouter, uint160(v.amountIn), uint48(v.deadline));
        vm.prank(user);
        (ok,) = v.universalRouter.call(v.callData);
        assertTrue(ok, "with both allowances it must work");
    }

    /// @notice A Permit2 allowance that has EXPIRED is exactly as short as one never granted —
    ///         the reason the API compares the expiry against the swap's own deadline and not
    ///         only the amount.
    function test_anExpiredPermit2AllowanceIsAsShortAsNoAllowance() public {
        deal(v.tokenIn, user, v.amountIn);
        vm.startPrank(user);
        IERC20(v.tokenIn).approve(v.permit2, v.amountIn);
        // generous in amount, already over in time
        IPermit2(v.permit2).approve(
            v.tokenIn, v.universalRouter, type(uint160).max, uint48(block.timestamp - 1)
        );
        (bool ok,) = v.universalRouter.call(v.callData);
        vm.stopPrank();
        assertFalse(ok, "an expired Permit2 allowance must not authorise the swap");
    }

    /// @notice The deadline in the bytes is real: past it, the router refuses.
    function test_thedeadlineInTheCalldataIsEnforced() public {
        deal(v.tokenIn, user, v.amountIn);
        vm.startPrank(user);
        IERC20(v.tokenIn).approve(v.permit2, v.amountIn);
        IPermit2(v.permit2).approve(v.tokenIn, v.universalRouter, uint160(v.amountIn), type(uint48).max);
        vm.warp(v.deadline + 1);
        (bool ok, bytes memory ret) = v.universalRouter.call(v.callData);
        vm.stopPrank();
        assertFalse(ok, "a swap past its deadline must revert");
        assertEq(bytes4(ret), bytes4(keccak256("TransactionDeadlinePassed()")), "expected the deadline error");
    }
}
