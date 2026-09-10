// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console2} from "forge-std/Test.sol";
import {PipeSplit} from "../src/libraries/PipeSplit.sol";

/// @dev External wrapper: `vm.expectRevert` watches the next CALL frame, and a library's `revert`
///      happens inside the caller's own frame.
contract PipeSplitHarness {
    function split(uint256 amount, uint256 minRelayerFee, uint256 grid)
        external
        pure
        returns (uint256 value, uint256 relayerFee)
    {
        return PipeSplit.split(amount, minRelayerFee, grid);
    }
}

/// @title PipeSplitVectorsTest
/// @notice Runs the SHARED golden vectors (`test/vectors/grid.json`) through the Solidity split.
/// @dev The same file is the fixture for the Python implementation in the API. Neither side owns
///      the truth — the file does. Two implementations of one fact will disagree, and one of them
///      reaches money; this is how that disagreement becomes a red test instead of a stuck deposit.
contract PipeSplitVectorsTest is Test {
    PipeSplitHarness internal h;
    string internal json;

    function setUp() public {
        h = new PipeSplitHarness();
        json = vm.readFile("./test/vectors/grid.json");
    }

    function test_goldenVectors() public {
        uint256 count = vm.parseJsonUint(json, ".count");
        assertGe(count, 12, "the fixture must carry at least 12 vectors");

        for (uint256 i = 0; i < count; i++) {
            string memory base = string.concat(".vectors[", vm.toString(i), "]");
            string memory name = vm.parseJsonString(json, string.concat(base, ".name"));
            uint256 amount = _u(base, ".amount");
            uint256 minFee = _u(base, ".min_relayer_fee");
            uint256 grid = _u(base, ".grid");
            string memory rev = vm.parseJsonString(json, string.concat(base, ".revert"));

            if (bytes(rev).length != 0) {
                vm.expectRevert(_selectorFor(rev, amount, minFee, grid));
                h.split(amount, minFee, grid);
                console2.log("refused as expected:", name);
            } else {
                uint256 wantValue = _u(base, ".value");
                uint256 wantFee = _u(base, ".relayer_fee");
                (uint256 gotValue, uint256 gotFee) = h.split(amount, minFee, grid);
                assertEq(gotValue, wantValue, name);
                assertEq(gotFee, wantFee, name);
                // The three invariants that make a deposit mintable and the relayer paid.
                assertEq(gotValue + gotFee, amount, "value + relayerFee == amount");
                assertEq(gotValue % grid, 0, "value sits on the grid");
                assertGe(gotFee, minFee, "the relayer keeps at least its quote");
            }
        }
        console2.log("golden vectors checked:", count);
    }

    /// @notice The POST-split bound. `fee_bounds` above pins the checks the hook makes on the
    ///         caller's QUOTE; these pin the check it makes on the fee that is actually PAID,
    ///         `relayerFee = quote + ((out - quote) mod grid)`. Every row's quote clears the
    ///         pre-split ceiling — that is the point: the quote passing is what made the realized
    ///         fee's overrun invisible. The API's quote builder must not exceed this rule either,
    ///         which is why the rows live in the shared file rather than in this test.
    function test_realizedFeeBoundVectors() public {
        uint256 count = vm.parseJsonUint(json, ".realized_fee_bounds_count");
        assertGe(count, 4, "the fixture must carry the realized-fee rows");

        for (uint256 i = 0; i < count; i++) {
            string memory base = string.concat(".realized_fee_bounds[", vm.toString(i), "]");
            string memory name = vm.parseJsonString(json, string.concat(base, ".name"));
            uint256 out = _u(base, ".out");
            uint256 quote = _u(base, ".quote");
            uint256 grid = _u(base, ".grid");
            uint256 maxBps = _u(base, ".max_relayer_fee_bps");
            bool accept = vm.parseJsonBool(json, string.concat(base, ".accept"));
            string memory rev = vm.parseJsonString(json, string.concat(base, ".revert"));

            (uint256 value, uint256 fee) = h.split(out, quote, grid);
            assertEq(value, _u(base, ".value"), name);
            assertEq(fee, _u(base, ".relayer_fee"), name);
            assertGe(quote, _u(base, ".min_relayer_fee"), "the quote clears the floor");
            assertLe(quote * 10_000, out * maxBps, "every row's QUOTE passes the pre-split ceiling");

            assertEq(fee * 10_000 <= out * maxBps, accept, name);
            assertEq(bytes(rev).length == 0, accept, "the row's own verdict and its revert agree");
            if (!accept) {
                assertEq(keccak256(bytes(rev)), keccak256("RealizedFeeAboveCeiling"), name);
            }
            console2.log(accept ? "realized fee accepted:" : "realized fee refused :", name);
        }
    }

    /// @notice The whole point of the grid: a sub-grid tail is unmintable on Beam and stuck forever.
    function testFuzz_valueIsAlwaysOnTheGrid(uint256 amount, uint256 minFee, uint8 gridPow) public view {
        uint256 grid = 10 ** (uint256(gridPow) % 19);
        amount = bound(amount, 1, type(uint128).max);
        minFee = bound(minFee, 0, amount);
        if (amount <= minFee) return;
        if ((amount - minFee) / grid == 0) return;
        (uint256 value, uint256 fee) = PipeSplit.split(amount, minFee, grid);
        assertEq(value % grid, 0);
        assertEq(value + fee, amount);
        assertGe(fee, minFee);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────

    function _u(string memory base, string memory field) internal view returns (uint256) {
        return vm.parseUint(vm.parseJsonString(json, string.concat(base, field)));
    }

    function _selectorFor(string memory name, uint256 amount, uint256 minFee, uint256 grid)
        internal
        pure
        returns (bytes memory)
    {
        bytes32 k = keccak256(bytes(name));
        if (k == keccak256("GridZero")) return abi.encodeWithSelector(PipeSplit.GridZero.selector);
        if (k == keccak256("AmountBelowRelayerFee")) {
            return abi.encodeWithSelector(PipeSplit.AmountBelowRelayerFee.selector, amount, minFee);
        }
        if (k == keccak256("NothingMintable")) {
            return abi.encodeWithSelector(PipeSplit.NothingMintable.selector, amount, minFee, grid);
        }
        revert("unknown revert name in the fixture");
    }
}
