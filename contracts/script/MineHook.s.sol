// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {HookMiner} from "@uniswap/v4-periphery/src/utils/HookMiner.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PgasIngressHook} from "../src/PgasIngressHook.sol";
import {PgasAddresses} from "./PgasAddresses.sol";

/// @notice Mine the CREATE2 salt whose deployed hook address carries the `0x2888` permission mask.
/// @dev DRY RUN by design — this script deploys nothing and broadcasts nothing. It prints the salt
///      and the address so a human can check them before `Deploy.s.sol` is ever pointed at a chain.
///
///      The salt is a function of (deployer, creationCode, constructorArgs). Change the owner, the
///      PoolManager, the compiler settings or one byte of the hook, and the salt is void: re-mine.
///      That is why every per-pool parameter lives in `registerRoute` and not in the constructor.
///
/// Usage: `OWNER=0x… forge script script/MineHook.s.sol --via-ir`
///        `ALLOW_PLACEHOLDER=1 forge script script/MineHook.s.sol --via-ir`   (throwaway mining)
contract MineHook is Script {
    /// @dev Placeholder owner. Mining is only valid for the owner it was run with; the real
    ///      deployment re-runs this with the founder's EOA in `OWNER`.
    address internal constant PLACEHOLDER_OWNER = 0x00000000000000000000000000000000000000A1;

    /// @notice The owner to mine for, or a refusal.
    /// @param ownerEnv Whatever `OWNER` resolved to; the placeholder or zero means "unset".
    /// @param allowPlaceholder `ALLOW_PLACEHOLDER=1` — an explicit request for throwaway mining.
    /// @dev The salt is a function of (deployer, creation code, CONSTRUCTOR ARGS), and the owner is
    ///      one of those args: a salt mined for the placeholder is void for the real owner. The
    ///      guard for that used to sit at the very bottom of `run()` as
    ///      `require(owner != PLACEHOLDER || vm.envOr("ALLOW_PLACEHOLDER", true), "set OWNER")` —
    ///      a guard that could never fire, because the default it read was `true`. So the script
    ///      always mined happily for the placeholder and printed an address the operator was meant
    ///      to trust. Silence is now the refusal, and mining a throwaway takes the explicit flag.
    /// @dev `public pure`: this is the whole guard, so the suite exercises it rather than a human
    ///      remembering to run the script both ways.
    function ownerOrRefuse(address ownerEnv, bool allowPlaceholder) public pure returns (address) {
        if (ownerEnv == PLACEHOLDER_OWNER || ownerEnv == address(0)) {
            require(
                allowPlaceholder,
                "set OWNER - the salt is only valid for the owner it was mined with (ALLOW_PLACEHOLDER=1 mines a throwaway)"
            );
            return PLACEHOLDER_OWNER;
        }
        return ownerEnv;
    }

    function run() external view returns (address hookAddress, bytes32 salt) {
        address poolManager = vm.envOr("POOL_MANAGER", PgasAddresses.POOL_MANAGER);
        address owner = ownerOrRefuse(vm.envOr("OWNER", PLACEHOLDER_OWNER), vm.envOr("ALLOW_PLACEHOLDER", false));

        bytes memory constructorArgs = abi.encode(IPoolManager(poolManager), owner);

        (hookAddress, salt) = HookMiner.find(
            PgasAddresses.CREATE2_PROXY, PgasAddresses.HOOK_FLAGS, type(PgasIngressHook).creationCode, constructorArgs
        );

        console2.log("== PgasIngressHook CREATE2 mining ==");
        console2.log("create2 proxy   :", PgasAddresses.CREATE2_PROXY);
        console2.log("poolManager     :", poolManager);
        console2.log("owner           :", owner);
        console2.log("required flags  : 0x2888  (beforeInitialize|beforeAddLiquidity|beforeSwap|beforeSwapReturnDelta)");
        console2.log("creationCode len:", type(PgasIngressHook).creationCode.length);
        console2.log("hook address    :", hookAddress);
        console2.log("salt            :");
        console2.logBytes32(salt);
        console2.log("low 14 bits     :", uint256(uint160(hookAddress)) & 0x3FFF);
        if (owner == PLACEHOLDER_OWNER) {
            console2.log("THROWAWAY: mined for the placeholder owner. This salt is void for the real one.");
        }

        require(uint160(hookAddress) & 0x3FFF == PgasAddresses.HOOK_FLAGS, "mask mismatch");
    }
}
