// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PgasBase} from "./PgasBase.t.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";

import {PgasRouter} from "../src/PgasRouter.sol";

/// @notice A depositor that is a CONTRACT and has no `receive()` — the shape a refund it never
///         asked for cannot be paid to. It deposits the exact `amountIn`, so a correct router owes
///         it nothing; a router that refunds its own BALANCE owes it a stranger's dust and fails.
contract ContractDepositorNoReceive {
    PgasRouter internal immutable router;

    constructor(PgasRouter _router) {
        router = _router;
    }

    function deposit(PoolKey memory key, bool zeroForOne, uint256 amountIn, bytes memory hookData) external {
        router.deposit{value: amountIn}(key, zeroForOne, amountIn, hookData);
    }
}

/// @title PgasRouterTest
/// @notice The router holds nothing between transactions and takes no fee. These tests assert both.
contract PgasRouterTest is PgasBase {
    uint256 internal constant AMOUNT_IN = 1e16;
    uint256 internal constant FEE_QUOTE = 7e10;

    function test_amountZeroRefused() public {
        vm.prank(user);
        vm.expectRevert(abi.encodeWithSelector(PgasRouter.AmountZero.selector));
        router.deposit(gwA, false, 0, hookData(bytes32("z"), MIN_OUT_ANY, FEE_QUOTE));
    }

    /// @notice The router will only ever swap on a pool carrying OUR hook. Anything else could
    ///         hand the user's input to a stranger's contract.
    function test_foreignPoolRefused() public {
        PoolKey memory foreign = PoolKey(native, curA, 3000, 60, IHooks(address(0)));
        vm.prank(user);
        vm.expectRevert(abi.encodeWithSelector(PgasRouter.NotPgasPool.selector, address(0)));
        router.deposit(foreign, false, AMOUNT_IN, hookData(bytes32("f"), MIN_OUT_ANY, FEE_QUOTE));
    }

    /// @notice A native deposit must be funded; a short `msg.value` is refused up front rather
    ///         than left to fail deep inside `settle`.
    function test_nativeUnderfundedRefused() public {
        vm.prank(user);
        vm.expectRevert(abi.encodeWithSelector(PgasRouter.BadMsgValue.selector, AMOUNT_IN - 1, AMOUNT_IN));
        router.deposit{value: AMOUNT_IN - 1}(gwB, true, AMOUNT_IN, hookData(bytes32("u"), MIN_OUT_ANY, FEE_QUOTE));
    }

    /// @notice Value sent with an ERC-20 deposit would be stranded; refuse instead of refunding
    ///         something the caller did not mean to send.
    function test_valueWithErc20DepositRefused() public {
        vm.prank(user);
        vm.expectRevert(abi.encodeWithSelector(PgasRouter.BadMsgValue.selector, 1 ether, 0));
        router.deposit{value: 1 ether}(gwA, false, AMOUNT_IN, hookData(bytes32("v"), MIN_OUT_ANY, FEE_QUOTE));
    }

    /// @notice Only the PoolManager may drive the callback.
    function test_unlockCallbackOnlyFromPoolManager() public {
        vm.prank(stranger);
        vm.expectRevert(abi.encodeWithSelector(PgasRouter.NotPoolManager.selector));
        router.unlockCallback("");
    }

    /// @notice `msgSender()` is transient: valid only while a deposit is on the stack, zero at rest.
    function test_msgSenderIsZeroAtRest() public view {
        assertEq(router.msgSender(), address(0));
    }

    /// @notice Surplus native value comes straight back, and the router keeps nothing.
    function test_surplusNativeIsRefunded() public {
        uint256 before = user.balance;
        vm.prank(user);
        router.deposit{value: 5 ether}(gwB, true, AMOUNT_IN, hookData(bytes32("r"), MIN_OUT_ANY, FEE_QUOTE));
        assertEq(before - user.balance, AMOUNT_IN, "only amountIn left the user");
        assertEq(address(router).balance, 0, "the router kept nothing");
    }

    /// @notice Without an allowance the pull fails and nothing moves.
    function test_missingAllowanceRefused() public {
        address poor = makeAddr("poor");
        tokenA.mint(poor, 1 ether);
        vm.prank(poor);
        vm.expectRevert();
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("a"), MIN_OUT_ANY, FEE_QUOTE));
        assertEq(tokenA.balanceOf(poor), 1 ether, "the tokens never left");
        assertEq(nativePipe.msgCount(), 0, "and nothing reached the pipe");
    }

    /// @notice The router is a pass-through: it never keeps a token balance either.
    function test_routerHoldsNothingAfterASuccessfulDeposit() public {
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("k"), MIN_OUT_ANY, FEE_QUOTE));
        assertEq(address(router).balance, 0);
        assertEq(tokenA.balanceOf(address(router)), 0);
        assertEq(tokenB.balanceOf(address(router)), 0);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // R5 — the refund is THIS CALL's surplus, never the contract's balance
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice R5: the refund used to be `address(this).balance`. Anyone can force ETH into any
    ///         address, so that read is "unspent value PLUS whatever a stranger left" — and it was
    ///         paid to whoever deposited next. This asserts the stranger's ETH stays put and the
    ///         depositor gets back exactly what they over-sent.
    function test_forcedEthIsNotPaidToTheNextDepositor() public {
        vm.deal(address(router), 1 ether); // as if someone had selfdestructed into it
        uint256 before = user.balance;

        vm.prank(user);
        router.deposit{value: AMOUNT_IN + 7}(gwB, true, AMOUNT_IN, hookData(bytes32("forced"), MIN_OUT_ANY, FEE_QUOTE));

        assertEq(before - user.balance, AMOUNT_IN, "the depositor was refunded exactly its own surplus");
        assertEq(address(router).balance, 1 ether, "and the stranger's ETH is still there, not in a pocket");
    }

    /// @notice R5: 1 wei of somebody else's dust must not brick a contract depositor. With the old
    ///         balance-based refund this reverted with `RefundFailed` on a refund that was not the
    ///         depositor's money and that it had not asked for.
    function test_aContractDepositorIsNotBrickedByAStrangersWei() public {
        ContractDepositorNoReceive dep = new ContractDepositorNoReceive(router);
        vm.deal(address(dep), 1 ether);
        vm.deal(address(router), 1); // the one wei that used to be fatal

        dep.deposit(gwB, true, AMOUNT_IN, hookData(bytes32("contract"), MIN_OUT_ANY, FEE_QUOTE));

        assertEq(tokenB.balanceOf(address(erc20Pipe)) > 0, true, "the deposit went through");
        assertEq(address(router).balance, 1, "the stray wei was never touched");
        assertEq(address(dep).balance, 1 ether - AMOUNT_IN, "and the depositor paid exactly amountIn");
    }

    /// @notice R5, the other consequence: an ERC-20 deposit sends no value, so it is refunded
    ///         nothing at all — not even a balance that happens to be sitting here.
    function test_anErc20DepositIsRefundedNothing() public {
        vm.deal(address(router), 5);
        uint256 before = user.balance;

        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("erc20"), MIN_OUT_ANY, FEE_QUOTE));

        assertEq(user.balance, before, "an ERC-20 depositor is paid no ETH");
        assertEq(address(router).balance, 5, "and the stray wei stays put");
    }

    /// @notice The router is pinned to one hook at construction and cannot be repointed.
    function test_hookIsImmutable() public view {
        assertEq(router.hook(), address(hook));
        assertEq(address(router.poolManager()), address(manager));
    }
}
