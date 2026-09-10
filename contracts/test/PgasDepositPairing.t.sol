// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PgasBase} from "./PgasBase.t.sol";
import {console2} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";

import {PgasAddresses} from "../script/PgasAddresses.sol";
import {ForeignUnlocker} from "./mocks/ForeignUnlocker.sol";

/// @title PgasDepositPairingTest
/// @notice What a reader of these logs is allowed to assume — held here so it stays true.
///
/// @dev A deposit is credited off the pair (`NewLocalMessage`, `PgasDeposit`) in one receipt. One
///      transaction can carry N of each, from a caller with no relationship to `PgasRouter`, with a
///      `ref` it chose itself. The bridge's `msgId` cannot be carried in `PgasDeposit` — `sendFunds`
///      returns nothing — so the only pairing key that exists is log ORDER, and it holds by
///      construction: the hook emits `PgasDeposit` immediately after the pipe call. This file is
///      that invariant's only guard; the rule is written out in `test/vectors/events.json` and in
///      the README, for the reader on the other side.
contract PgasDepositPairingTest is PgasBase {
    uint256 internal constant AMOUNT_IN = 1e16;
    uint256 internal constant FEE_QUOTE = 7e10;

    bytes32 internal constant NEW_LOCAL_MESSAGE_TOPIC0 = keccak256("NewLocalMessage(uint64,uint256,uint256,bytes)");
    bytes32 internal constant PGAS_DEPOSIT_TOPIC0 =
        keccak256("PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)");

    /// @notice Two deposits, one transaction, one `ref`, a stranger's contract — and the logs are
    ///         still pairable: the k-th pipe log belongs to the k-th deposit log, and the pipe log
    ///         comes first. A reader that takes "the first NewLocalMessage in the receipt" credits
    ///         the wrong number; the test asserts the two amounts DIFFER, so that is not academic.
    function test_twoDepositsInOneTransactionPairByOrdinal() public {
        ForeignUnlocker foreign = new ForeignUnlocker(manager);
        assertFalse(hook.allowedRouter(address(foreign)), "not allow-listed, and it does not need to be");
        tokenA.mint(address(foreign), 1 ether);

        vm.recordLogs();
        foreign.swapMany(gwA, false, AMOUNT_IN, 2, hookData(bytes32("VICTIM-REF"), MIN_OUT_ANY, FEE_QUOTE));
        Vm.Log[] memory logs = vm.getRecordedLogs();

        (uint256[] memory pipeAt, uint256[] memory depositAt) = _pairs(logs, address(nativePipe));
        assertEq(pipeAt.length, 2, "two NewLocalMessage logs in ONE receipt");
        assertEq(depositAt.length, 2, "and two PgasDeposit logs, both carrying the same ref");
        assertEq(nativePipe.sentCount(), 2, "the pipe really was called twice");

        uint256[] memory amounts = new uint256[](2);
        for (uint256 k = 0; k < 2; k++) {
            // ORDER: pipe log k, then deposit log k, then pipe log k+1. The hook's re-entrancy
            // guard is what forbids a deposit inside a deposit, so the pairs cannot interleave.
            assertLt(pipeAt[k], depositAt[k], "the pipe log precedes its own deposit log");
            if (k + 1 < 2) assertLt(depositAt[k], pipeAt[k + 1], "and the next pair starts after it");

            (, uint256 amount, uint256 relayerFee,) =
                abi.decode(logs[pipeAt[k]].data, (uint64, uint256, uint256, bytes));
            (,, uint256 value, uint256 fee,) =
                abi.decode(logs[depositAt[k]].data, (uint256, address, uint256, uint256, bytes));
            assertEq(value, amount, "paired: PgasDeposit.value == NewLocalMessage.amount");
            assertEq(fee, relayerFee, "paired: the fees agree too");

            assertEq(logs[depositAt[k]].topics[1], bytes32("VICTIM-REF"), "the ref is whatever the caller chose");
            assertEq(
                address(uint160(uint256(logs[depositAt[k]].topics[2]))),
                address(foreign),
                "payer == the foreign caller, never the victim the ref names"
            );
            amounts[k] = amount;
        }

        assertTrue(amounts[0] != amounts[1], "the two amounts differ - mis-pairing credits the wrong one");
        console2.log("D5 first  NewLocalMessage.amount:", amounts[0]);
        console2.log("D5 second NewLocalMessage.amount:", amounts[1]);
    }

    /// @notice The pairing is ORDINAL, not adjacent — and this is the reason. On a native route the
    ///         deposit log IS the next log; on an ERC-20 route the allowance being cleared puts an
    ///         `Approval` between them. A reader written against "the very next log" works on ETH
    ///         and silently fails on DAI, so the difference is pinned here rather than discovered.
    function test_theDepositLogIsNotAlwaysTheVeryNextLog() public {
        vm.recordLogs();
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("native"), MIN_OUT_ANY, FEE_QUOTE));
        (uint256[] memory pipeAt, uint256[] memory depAt) = _pairs(vm.getRecordedLogs(), address(nativePipe));
        assertEq(pipeAt.length, 1);
        assertEq(depAt[0], pipeAt[0] + 1, "native route: the deposit log is the next log");

        vm.recordLogs();
        vm.prank(user);
        router.deposit{value: AMOUNT_IN}(gwB, true, AMOUNT_IN, hookData(bytes32("erc20"), MIN_OUT_ANY, FEE_QUOTE));
        (pipeAt, depAt) = _pairs(vm.getRecordedLogs(), address(erc20Pipe));
        assertEq(pipeAt.length, 1);
        assertLt(pipeAt[0], depAt[0], "ERC-20 route: the pipe log still comes first");
        assertGt(depAt[0], pipeAt[0] + 1, "but NOT immediately - the allowance clear logs in between");
    }

    /// @notice The event ABI is pinned in three places that must agree: the contract, the shared
    ///         vector file the API reads, and `PgasAddresses`. Unlike the split, this had no shared
    ///         pin at all — and the design document's draft signature is a DIFFERENT topic0, so a
    ///         reader built from the document alone would match nothing.
    function test_eventAbiIsPinnedAcrossBothSides() public {
        string memory json = vm.readFile("./test/vectors/events.json");

        assertEq(
            PGAS_DEPOSIT_TOPIC0,
            vm.parseJsonBytes32(json, ".pgas_deposit.topic0"),
            "PgasDeposit topic0 == the shared vector"
        );
        assertEq(PGAS_DEPOSIT_TOPIC0, PgasAddresses.PGAS_DEPOSIT_TOPIC0, "== the pinned constant");
        assertEq(
            keccak256(bytes(vm.parseJsonString(json, ".pgas_deposit.signature"))),
            keccak256("PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)"),
            "and the signature the file states really hashes to it"
        );
        assertEq(
            NEW_LOCAL_MESSAGE_TOPIC0,
            vm.parseJsonBytes32(json, ".new_local_message.topic0"),
            "NewLocalMessage topic0 == the shared vector"
        );
        assertEq(NEW_LOCAL_MESSAGE_TOPIC0, PgasAddresses.NEW_LOCAL_MESSAGE_TOPIC0, "== the pinned constant");

        // The draft in the design document is a different event. Say so in a test, not only in prose.
        assertTrue(
            PGAS_DEPOSIT_TOPIC0 != keccak256("PgasDeposit(bytes32,address,address,uint256,uint256,bytes)"),
            "the design document's draft signature is NOT what is deployed"
        );

        // And the deployed log really carries that shape: 4 topics, then the five data fields.
        vm.recordLogs();
        vm.prank(user);
        router.deposit(gwA, false, AMOUNT_IN, hookData(bytes32("abi"), MIN_OUT_ANY, FEE_QUOTE));
        Vm.Log[] memory logs = vm.getRecordedLogs();

        bool seen;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter != address(hook) || logs[i].topics[0] != PGAS_DEPOSIT_TOPIC0) continue;
            seen = true;
            assertEq(logs[i].topics.length, 4, "topic0 + ref + payer + tokenIn");
            assertEq(logs[i].topics[1], bytes32("abi"), "ref");
            assertEq(address(uint160(uint256(logs[i].topics[2]))), user, "payer");
            assertEq(address(uint160(uint256(logs[i].topics[3]))), address(tokenA), "tokenIn");
            (uint256 amountIn, address target, uint256 value, uint256 relayerFee, bytes memory pk) =
                abi.decode(logs[i].data, (uint256, address, uint256, uint256, bytes));
            assertEq(amountIn, AMOUNT_IN, "data[0] amountIn");
            assertEq(target, address(0), "data[1] target");
            assertEq(value + relayerFee, nativePipe.lastSent().msgValue, "data[2]+data[3] are the whole output");
            assertEq(pk.length, 33, "data[4] receiverBeamPubkey");
        }
        assertTrue(seen, "PgasDeposit was emitted");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @dev The indices of every pipe log and every deposit log in one receipt, in order.
    function _pairs(Vm.Log[] memory logs, address pipe)
        internal
        view
        returns (uint256[] memory pipeAt, uint256[] memory depositAt)
    {
        uint256 n;
        uint256 m;
        pipeAt = new uint256[](logs.length);
        depositAt = new uint256[](logs.length);
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].emitter == pipe && logs[i].topics[0] == NEW_LOCAL_MESSAGE_TOPIC0) pipeAt[n++] = i;
            if (logs[i].emitter == address(hook) && logs[i].topics[0] == PGAS_DEPOSIT_TOPIC0) depositAt[m++] = i;
        }
        assembly ("memory-safe") {
            mstore(pipeAt, n)
            mstore(depositAt, m)
        }
    }
}
