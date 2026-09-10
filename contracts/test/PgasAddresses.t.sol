// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test} from "forge-std/Test.sol";
import {PgasAddresses} from "../script/PgasAddresses.sol";
import {Register} from "../script/Register.s.sol";

/// @dev `vm.expectRevert` watches the next CALL frame, and a library's `revert` happens inside the
///      caller's own frame.
contract AddressesHarness {
    function beamPubkeyFor(address pipe) external pure returns (bytes memory) {
        return PgasAddresses.beamPubkeyFor(pipe);
    }

    function gridFor(address asset) external pure returns (uint256) {
        return PgasAddresses.gridFor(asset);
    }

    function assetOfPipe(address pipe) external pure returns (address) {
        return PgasAddresses.assetOfPipe(pipe);
    }
}

/// @title PgasAddressesTest
/// @notice The pinned table and the one script that reads it. These are the fields a route can
///         never take back, and the only place they are checked before a broadcast.
contract PgasAddressesTest is Test {
    AddressesHarness internal h;
    string internal json;

    function setUp() public {
        h = new AddressesHarness();
        json = vm.readFile("./test/vectors/grid.json");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // D4 — one Beam public key per pipe, and no default
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice D4: the key is derived from (wallet master key, PIPE CID) — one key per pipe — and
    ///         `beamPubkeyETH()` was the only one in the table while four pipes were pinned, so
    ///         every route got the ETH key by default. Nothing on chain catches it: the hook checks
    ///         33 bytes and the bridge checks 33 bytes. A route is write-once and `beamPubkey` is
    ///         absent from `tightenRoute`, so 100% of every deposit would go to a key derived for
    ///         another pipe's cid, permanently. A pipe with no key pinned must REFUSE, not default.
    function test_beamPubkeyIsPinnedPerPipeAndAnUnpinnedPipeIsRefused() public {
        bytes memory eth = h.beamPubkeyFor(PgasAddresses.ETH_PIPE);
        assertEq(eth.length, 33, "33-byte compressed point");
        assertEq(keccak256(eth), keccak256(PgasAddresses.beamPubkeyETH()), "the ETH pipe keeps its key");

        vm.expectRevert(
            bytes("PgasAddresses: no Beam pubkey pinned for this pipe - derive it from (master key, pipe cid) first")
        );
        h.beamPubkeyFor(PgasAddresses.USDT_PIPE);
        vm.expectRevert(
            bytes("PgasAddresses: no Beam pubkey pinned for this pipe - derive it from (master key, pipe cid) first")
        );
        h.beamPubkeyFor(PgasAddresses.WBTC_PIPE);
        vm.expectRevert(
            bytes("PgasAddresses: no Beam pubkey pinned for this pipe - derive it from (master key, pipe cid) first")
        );
        h.beamPubkeyFor(PgasAddresses.DAI_PIPE);
    }

    /// @notice D4: each pinned pipe carries exactly one asset, so a route's output currency can be
    ///         cross-checked against the pipe BEFORE the first deposit finds out by reverting.
    function test_assetOfPipeIsOneToOne() public view {
        assertEq(h.assetOfPipe(PgasAddresses.ETH_PIPE), PgasAddresses.NATIVE);
        assertEq(h.assetOfPipe(PgasAddresses.USDT_PIPE), PgasAddresses.USDT);
        assertEq(h.assetOfPipe(PgasAddresses.WBTC_PIPE), PgasAddresses.WBTC);
        assertEq(h.assetOfPipe(PgasAddresses.DAI_PIPE), PgasAddresses.DAI);
    }

    /// @notice D4: the tooling refuses the exact sequence the README used to invite — arm the USDT
    ///         route "the way the ETH route was armed" — instead of quietly registering the ETH key
    ///         on it. `resolveRoute` is the whole safety of an arming decision, so it is `pure` and
    ///         held here rather than only exercisable by pointing the script at a chain.
    function test_registerScriptRefusesAnotherPipesRouteWithoutItsOwnPubkey() public {
        Register reg = new Register();

        vm.expectRevert(
            bytes("PgasAddresses: no Beam pubkey pinned for this pipe - derive it from (master key, pipe cid) first")
        );
        reg.resolveRoute(PgasAddresses.USDT_PIPE, PgasAddresses.USDT, 1, 1000, "");

        // With a key of its own, the same route resolves.
        bytes memory ownKey = hex"02deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef";
        (uint256 grid, uint256 fee, bytes memory pk) =
            reg.resolveRoute(PgasAddresses.USDT_PIPE, PgasAddresses.USDT, 1, 1000, ownKey);
        assertEq(grid, 1, "USDT is 6 decimals: no grid");
        assertEq(fee, 1000, "the tariff passed in");
        assertEq(keccak256(pk), keccak256(ownKey), "and its own key, not the ETH pipe's");

        // A key of the wrong length is refused rather than truncated.
        vm.expectRevert(bytes("PUBKEY must be a 33-byte compressed Beam public key"));
        reg.resolveRoute(PgasAddresses.USDT_PIPE, PgasAddresses.USDT, 1, 1000, hex"1234");

        // And a pipe that does not carry the route's output asset is refused before the chain has
        // to find out at the first deposit.
        vm.expectRevert(bytes("PIPE does not carry the route's output asset"));
        reg.resolveRoute(PgasAddresses.ETH_PIPE, PgasAddresses.USDT, 1, 1000, "");
    }

    /// @notice D1/D7 at the tooling level: a GRID the output asset contradicts never reaches the
    ///         chain, and an unpinned asset must be stated explicitly rather than defaulted.
    function test_registerScriptRefusesAGridTheAssetContradicts() public {
        Register reg = new Register();

        vm.expectRevert(bytes("GRID contradicts the output asset's decimals"));
        reg.resolveRoute(PgasAddresses.ETH_PIPE, PgasAddresses.NATIVE, 1, 0, "");
        vm.expectRevert(bytes("GRID contradicts the output asset's decimals"));
        reg.resolveRoute(PgasAddresses.ETH_PIPE, PgasAddresses.NATIVE, 1e18, 0, "");
        vm.expectRevert(bytes("GRID: unpinned output asset - pass GRID = 10**(decimals-8) explicitly"));
        reg.resolveRoute(PgasAddresses.ETH_PIPE, address(0xBEEF), 0, 1, "");

        // Left to itself it takes the pinned grid and the pinned tariff.
        (uint256 grid, uint256 fee, bytes memory pk) =
            reg.resolveRoute(PgasAddresses.ETH_PIPE, PgasAddresses.NATIVE, 0, 0, "");
        assertEq(grid, PgasAddresses.GRID_18DEC, "the grid the asset implies");
        assertEq(fee, PgasAddresses.MIN_RELAYER_FEE_ETH_WEI, "the tariff the API quotes");
        assertEq(keccak256(pk), keccak256(PgasAddresses.beamPubkeyETH()), "the ETH pipe's own key");
    }

    /// @notice D8 at the tooling level: a floor under one grid step is not a floor.
    function test_registerScriptRefusesARelayerFloorUnderOneGridStep() public {
        Register reg = new Register();
        vm.expectRevert(bytes("MIN_RELAYER_FEE below one grid step is not a floor"));
        reg.resolveRoute(PgasAddresses.ETH_PIPE, PgasAddresses.NATIVE, 0, 1e10 - 1, "");
        vm.expectRevert(bytes("MIN_RELAYER_FEE: unpinned output asset - pass the API's tariff explicitly"));
        reg.resolveRoute(
            PgasAddresses.USDT_PIPE,
            PgasAddresses.USDT,
            1,
            0,
            hex"02deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        );
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // D8 — the relayer floor comes from the same source the API quotes from
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice D8: the script's floor was a round grid step (1e10) chosen here, ten times under the
    ///         tariff the API actually quotes — a route registered with it would accept a quote no
    ///         relayer will move for, and the floor was un-raisable. One number, one source: this
    ///         asserts the pinned constants against the shared vector file both sides read.
    function test_pinnedRelayerFloorsMatchTheSharedTariff() public view {
        assertEq(
            PgasAddresses.MIN_RELAYER_FEE_ETH_WEI,
            vm.parseUint(vm.parseJsonString(json, ".tariff.eth_min_relayer_fee_wei")),
            "ETH tariff"
        );
        assertEq(
            PgasAddresses.MIN_RELAYER_FEE_DAI_UNITS,
            vm.parseUint(vm.parseJsonString(json, ".tariff.dai_min_relayer_fee_units")),
            "DAI tariff"
        );
        assertEq(
            PgasAddresses.MIN_RELAYER_FEE_WBTC_UNITS,
            vm.parseUint(vm.parseJsonString(json, ".tariff.wbtc_min_relayer_fee_units")),
            "WBTC tariff"
        );
        // And every floor is at least one grid step, which is what `registerRoute` now demands.
        assertGe(PgasAddresses.MIN_RELAYER_FEE_ETH_WEI, PgasAddresses.gridForOrZero(PgasAddresses.NATIVE));
        assertGe(PgasAddresses.MIN_RELAYER_FEE_DAI_UNITS, PgasAddresses.gridForOrZero(PgasAddresses.DAI));
        assertGe(PgasAddresses.MIN_RELAYER_FEE_WBTC_UNITS, PgasAddresses.gridForOrZero(PgasAddresses.WBTC));
    }

    /// @notice The pinned grids are `10**(decimals-8)` for each asset this build knows, and an
    ///         asset nobody has pinned refuses rather than defaulting to something plausible.
    function test_gridForIsPinnedPerAssetAndRefusesAnUnknownOne() public {
        assertEq(h.gridFor(PgasAddresses.NATIVE), 1e10, "ETH: 18 decimals");
        assertEq(h.gridFor(PgasAddresses.DAI), 1e10, "DAI: 18 decimals");
        assertEq(h.gridFor(PgasAddresses.WBTC), 1, "WBTC: 8 decimals");
        assertEq(h.gridFor(PgasAddresses.USDT), 1, "USDT: 6 decimals");
        assertEq(h.gridFor(PgasAddresses.USDC), 1, "USDC: 6 decimals");
        vm.expectRevert(bytes("PgasAddresses: no grid pinned for this asset - add it, do not guess"));
        h.gridFor(address(0xBEEF));
    }
}
