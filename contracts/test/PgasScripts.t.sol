// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {PgasBase} from "./PgasBase.t.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {MockERC20} from "solmate/src/test/utils/mocks/MockERC20.sol";

import {PgasAddresses} from "../script/PgasAddresses.sol";
import {MineHook} from "../script/MineHook.s.sol";
import {Deploy} from "../script/Deploy.s.sol";
import {Register} from "../script/Register.s.sol";

/// @title PgasScriptsTest
/// @notice The arming scripts' decisions, exercised by the suite rather than by pointing a script
///         at a chain and hoping. Every guard here is a `pure` or `view` resolver for exactly that
///         reason: a check that can only be reached by running the script against mainnet is a
///         check nobody runs twice, and these three run once each, on the day money starts moving.
contract PgasScriptsTest is PgasBase {
    MineHook internal mine;
    Deploy internal deploy;
    Register internal reg;

    function setUp() public override {
        super.setUp();
        mine = new MineHook();
        deploy = new Deploy();
        reg = new Register();
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // R4 — MineHook: silence is a refusal, not a placeholder
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice R4: the salt is a function of the constructor args, and the owner is one of them —
    ///         so a salt mined for the placeholder is void for the real owner. The guard that was
    ///         meant to say so read `vm.envOr("ALLOW_PLACEHOLDER", true)`: it defaulted to
    ///         permission and could never fire, so an operator who forgot `OWNER` got a printed
    ///         address and a salt that belonged to nobody. Unset must refuse.
    function test_mineHookRefusesToMineForAnUnsetOwner() public {
        vm.expectRevert(
            bytes(
                "set OWNER - the salt is only valid for the owner it was mined with (ALLOW_PLACEHOLDER=1 mines a throwaway)"
            )
        );
        mine.ownerOrRefuse(address(0), false);

        // The placeholder itself is "unset" too — that is what it means.
        vm.expectRevert(
            bytes(
                "set OWNER - the salt is only valid for the owner it was mined with (ALLOW_PLACEHOLDER=1 mines a throwaway)"
            )
        );
        mine.ownerOrRefuse(0x00000000000000000000000000000000000000A1, false);
    }

    /// @notice R4: a throwaway mining run is still possible — it just has to be asked for.
    function test_mineHookMinesForThePlaceholderOnlyWhenAsked() public view {
        assertEq(mine.ownerOrRefuse(address(0), true), 0x00000000000000000000000000000000000000A1, "explicit opt-in");
        assertEq(mine.ownerOrRefuse(owner, false), owner, "a real owner needs no flag at all");
        assertEq(mine.ownerOrRefuse(owner, true), owner, "and the flag does not override one");
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // R4 — Deploy: a deterministic salt has no second chance, so name the three cases
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice R4: `Deploy` had no recovery from an occupied mined address. The salt is
    ///         deterministic, so a re-run predicts the same address, and `new …{salt}` into an
    ///         occupied one reverts inside the CREATE2 proxy with no reason string — which reads
    ///         like a flaky RPC and invites a retry that can never work.
    function test_deployReadsWhatIsLivingAtTheMinedAddress() public {
        // EMPTY: the normal case, free to deploy.
        assertTrue(
            deploy.occupancyOf(makeAddr("nothing deployed here"), address(manager)) == Deploy.Occupancy.EMPTY,
            "an address with no code is free"
        );

        // OURS: this fixture's hook is a real one, mined for this PoolManager. Already deployed.
        assertTrue(
            deploy.occupancyOf(address(hook), address(manager)) == Deploy.Occupancy.OURS,
            "our own hook, pointed at our own PoolManager"
        );

        // FOREIGN, shape 1: code that does not answer `hookFlags()` at all.
        assertTrue(
            deploy.occupancyOf(address(router), address(manager)) == Deploy.Occupancy.FOREIGN,
            "a contract that is not a hook"
        );
        assertTrue(
            deploy.occupancyOf(address(nativePipe), address(manager)) == Deploy.Occupancy.FOREIGN, "nor is a pipe"
        );

        // FOREIGN, shape 2: OUR code, pointed at a DIFFERENT PoolManager — the shape a copied
        // deployment from another chain's config leaves behind. Refusing is the only safe verdict:
        // the router this run is about to deploy would be pinned to a hook trading somewhere else.
        assertTrue(
            deploy.occupancyOf(address(hook), address(0xBEEF)) == Deploy.Occupancy.FOREIGN,
            "the right code on the wrong PoolManager is not ours"
        );
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // R1 — Register: the pipe is proven by its bytecode, on the laptop, before the broadcast
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice R1: the hook can only prove the pipe is a contract that refuses calls it cannot
    ///         serve. Identity is pinned here, and compared against the code that is actually at
    ///         the address — so a wrong-but-plausible address stops the arming on the laptop.
    function test_registerScriptRefusesAPipeThatIsNotThePinnedBytecode() public {
        // The pinned hashes, as the four pipes carry them.
        reg.requirePinnedPipe(PgasAddresses.ETH_PIPE, PgasAddresses.ETH_PIPE_CODEHASH);
        reg.requirePinnedPipe(PgasAddresses.USDT_PIPE, PgasAddresses.ERC20_PIPE_CODEHASH);
        reg.requirePinnedPipe(PgasAddresses.WBTC_PIPE, PgasAddresses.ERC20_PIPE_CODEHASH);
        reg.requirePinnedPipe(PgasAddresses.DAI_PIPE, PgasAddresses.ERC20_PIPE_CODEHASH);

        // The ETH pipe is a DIFFERENT contract from the three ERC-20 pipes: one hash may not stand
        // in for the other, in either direction.
        vm.expectRevert(bytes("PIPE is not the pinned bridge bytecode - refusing rather than guessing"));
        reg.requirePinnedPipe(PgasAddresses.ETH_PIPE, PgasAddresses.ERC20_PIPE_CODEHASH);
        vm.expectRevert(bytes("PIPE is not the pinned bridge bytecode - refusing rather than guessing"));
        reg.requirePinnedPipe(PgasAddresses.DAI_PIPE, PgasAddresses.ETH_PIPE_CODEHASH);

        // An address with no code — a wrong address, or a dry run with no chain behind it. Both
        // report "nothing there", and nothing there is a refusal, never a pass.
        vm.expectRevert(
            bytes("PIPE has no code at this address - wrong address, or this run has no chain to read (pass --rpc-url)")
        );
        reg.requirePinnedPipe(PgasAddresses.ETH_PIPE, bytes32(0));
        vm.expectRevert(
            bytes("PIPE has no code at this address - wrong address, or this run has no chain to read (pass --rpc-url)")
        );
        reg.requirePinnedPipe(PgasAddresses.ETH_PIPE, PgasAddresses.EMPTY_CODEHASH);

        // And a pipe nobody has pinned is refused by name rather than waved through.
        vm.expectRevert(
            bytes("PgasAddresses: unpinned pipe - read its EXTCODEHASH off chain and pin it before a route names it")
        );
        reg.requirePinnedPipe(address(0xBEEF), PgasAddresses.ERC20_PIPE_CODEHASH);
    }

    /// @notice The pinned table's own shape: three ERC-20 pipes, one build; the ETH pipe apart.
    ///         The fork suite is where these are read off the live bridge.
    function test_pinnedPipeCodehashTableIsOnePerBuild() public pure {
        assertEq(PgasAddresses.pipeCodehashForOrZero(PgasAddresses.USDT_PIPE), PgasAddresses.ERC20_PIPE_CODEHASH);
        assertEq(PgasAddresses.pipeCodehashForOrZero(PgasAddresses.WBTC_PIPE), PgasAddresses.ERC20_PIPE_CODEHASH);
        assertEq(PgasAddresses.pipeCodehashForOrZero(PgasAddresses.DAI_PIPE), PgasAddresses.ERC20_PIPE_CODEHASH);
        assertEq(PgasAddresses.pipeCodehashForOrZero(PgasAddresses.ETH_PIPE), PgasAddresses.ETH_PIPE_CODEHASH);
        assertTrue(PgasAddresses.ETH_PIPE_CODEHASH != PgasAddresses.ERC20_PIPE_CODEHASH, "two builds, two hashes");
        assertEq(PgasAddresses.pipeCodehashForOrZero(address(0xBEEF)), bytes32(0), "0 means unpinned, not 'anything'");
    }

    /// @notice Every pre-flight above is a READ, and `forge script` with no `--rpc-url` runs on an
    ///         empty in-memory EVM where every read answers "nothing there". That is not an answer,
    ///         and "the mined address is free" / "the pipe is not there" must not be printed as if
    ///         it were. The PoolManager is the one address any chain worth arming has.
    function test_theScriptsRefuseARunWithNoChainBehindIt() public {
        reg.requireChainIsReadable(address(manager).code.length); // a real chain: passes

        vm.expectRevert(
            bytes("no chain to read: the PoolManager has no code here - pass --rpc-url <url> (still read-only)")
        );
        reg.requireChainIsReadable(0);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // R4 — Register: the inner pool is the route's whole economics, and nothing read it
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice R4: registration compared the two keys' currencies and stopped there. A key with the
    ///         wrong fee tier or tick spacing names a pool that does not exist, and an initialised
    ///         pool with zero liquidity looks real and pays nothing — `InnerSwapEmpty`, discovered
    ///         at the first deposit, after the route is frozen. Both are free to catch beforehand.
    function test_registerScriptReadsTheInnerPoolsPriceAndDepth() public {
        // A real, funded, hook-less inner pool: a price and depth.
        (uint160 sqrtPriceX96, uint128 liquidity) = reg.readInnerPool(IPoolManager(address(manager)), innerA);
        assertGt(sqrtPriceX96, 0, "the fixture's inner pool is initialised");
        assertGt(liquidity, 0, "and funded");
        reg.requireLiveInnerPool(sqrtPriceX96, liquidity);

        // A pool nobody initialised: no price at all. This is the fee-tier typo — same currencies,
        // a tick spacing that names a pool which does not exist.
        PoolKey memory ghost = PoolKey(native, curA, 500, 10, IHooks(address(0)));
        (uint160 ghostPrice, uint128 ghostLiquidity) = reg.readInnerPool(IPoolManager(address(manager)), ghost);
        assertEq(ghostPrice, 0, "an uninitialised pool has no price");
        vm.expectRevert(bytes("INNER pool is not initialised - wrong fee tier or tick spacing, or no chain to read"));
        reg.requireLiveInnerPool(ghostPrice, ghostLiquidity);

        // Initialised and EMPTY: the gateway pool itself is exactly this shape, which is what makes
        // it the honest fixture for "looks real, pays nothing".
        (uint160 gwPrice, uint128 gwLiquidity) = reg.readInnerPool(IPoolManager(address(manager)), gwA);
        assertGt(gwPrice, 0, "initialised");
        assertEq(gwLiquidity, 0, "and empty");
        vm.expectRevert(bytes("INNER pool holds no liquidity - it looks real and would pay nothing"));
        reg.requireLiveInnerPool(gwPrice, gwLiquidity);
    }
}
