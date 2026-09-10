// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {HookMiner} from "@uniswap/v4-periphery/src/utils/HookMiner.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IImmutableState} from "@uniswap/v4-periphery/src/interfaces/IImmutableState.sol";
import {PgasIngressHook} from "../src/PgasIngressHook.sol";
import {PgasRouter} from "../src/PgasRouter.sol";
import {PgasAddresses} from "./PgasAddresses.sol";

/// @notice Deploy `PgasIngressHook` (at a mined `0x2888` address) and `PgasRouter`.
///
/// @dev **DRY RUN BY DEFAULT — TWICE OVER.** `forge script` without `--broadcast` only simulates,
///      and this script additionally refuses to enter a broadcast unless `PGAS_CONFIRM=1` is set.
///      So an accidental `--broadcast` still sends nothing. It prints every value it would use, and
///      a human reads them before anything is signed.
///
/// @dev It READS the chain first — what is living at the mined address — so point it at one with
///      `--rpc-url` even for the dry run. Read-only; nothing is sent without `--broadcast`.
///
/// Dry run:  `OWNER=0x… forge script script/Deploy.s.sol --rpc-url <url>`
/// Real:     the operator's call, with a key that is neither a treasury nor a settlement wallet.
contract Deploy is Script {
    /// @notice What is living at the mined address right now.
    /// @dev EMPTY — deploy. OURS — already deployed, there is nothing to do. FOREIGN — refuse.
    enum Occupancy {
        EMPTY,
        OURS,
        FOREIGN
    }

    /// @notice Read the mined address and say which of the three cases this is.
    /// @param predicted The CREATE2 address `HookMiner` mined.
    /// @param poolManager_ The PoolManager this run means to point the hook at.
    /// @dev The salt is deterministic, so a second run predicts the SAME address. Into an occupied
    ///      one, `new PgasIngressHook{salt: salt}` reverts inside the CREATE2 proxy with no reason
    ///      string at all — which reads like an RPC failure and invites a retry that cannot ever
    ///      work. Name the three cases instead, and name them BEFORE the broadcast.
    /// @dev It asks the CODE what it is rather than inferring identity from the arithmetic that
    ///      produced the address: `hookFlags()` and `poolManager()` are both ours and both
    ///      readable. `owner()` is deliberately NOT part of the verdict — ownership can be
    ///      transferred after a deploy, and that is still our hook — but it IS printed, because a
    ///      surprise there is something the operator has to see.
    /// @dev `public view`: the whole recovery path is this function, so the suite exercises all
    ///      three verdicts offline instead of a human discovering them at a broadcast.
    function occupancyOf(address predicted, address poolManager_) public view returns (Occupancy) {
        if (predicted.code.length == 0) return Occupancy.EMPTY;

        (bool okFlags, bytes memory flags) =
            predicted.staticcall(abi.encodeWithSelector(PgasIngressHook.hookFlags.selector));
        (bool okPm, bytes memory pm) =
            predicted.staticcall(abi.encodeWithSelector(IImmutableState.poolManager.selector));
        if (!okFlags || flags.length < 32 || !okPm || pm.length < 32) return Occupancy.FOREIGN;
        if (abi.decode(flags, (uint160)) != PgasAddresses.HOOK_FLAGS) return Occupancy.FOREIGN;
        if (abi.decode(pm, (address)) != poolManager_) return Occupancy.FOREIGN;
        return Occupancy.OURS;
    }

    function run() external {
        address poolManager = vm.envOr("POOL_MANAGER", PgasAddresses.POOL_MANAGER);
        address owner = vm.envAddress("OWNER");
        bool confirmed = vm.envOr("PGAS_CONFIRM", false);

        bytes memory constructorArgs = abi.encode(IPoolManager(poolManager), owner);
        (address hookAddress, bytes32 salt) = HookMiner.find(
            PgasAddresses.CREATE2_PROXY, PgasAddresses.HOOK_FLAGS, type(PgasIngressHook).creationCode, constructorArgs
        );

        console2.log("=========================================================");
        console2.log(" Pgas ingress deploy  -  %s", confirmed ? "BROADCAST ARMED" : "DRY RUN (nothing is sent)");
        console2.log("=========================================================");
        console2.log("chain id            :", block.chainid);
        console2.log("poolManager         :", poolManager);
        console2.log("owner (pause/rescue):", owner);
        console2.log("create2 proxy       :", PgasAddresses.CREATE2_PROXY);
        console2.log("required flags      : 0x2888");
        console2.log("hook creationCode B :", type(PgasIngressHook).creationCode.length);
        console2.log("hook address        :", hookAddress);
        console2.log("hook low 14 bits    :", uint256(uint160(hookAddress)) & 0x3FFF);
        console2.log("salt                :");
        console2.logBytes32(salt);
        require(uint160(hookAddress) & 0x3FFF == PgasAddresses.HOOK_FLAGS, "mask mismatch");

        // Every check below is a READ. With no chain behind this run they all come back "nothing
        // there", which reads as reassurance rather than as the absence of an answer.
        PgasAddresses.requireChainIsReadable(poolManager.code.length);

        Occupancy occ = occupancyOf(hookAddress, poolManager);
        console2.log("mined address holds :", uint256(occ) == 0 ? "nothing - free to deploy" : "code (see below)");
        if (occ == Occupancy.OURS) {
            console2.log("ALREADY DEPLOYED    : this hook is live at the mined address; the deploy is a no-op.");
            console2.log("hook owner on chain :", PgasIngressHook(payable(hookAddress)).owner());
            console2.log("(if that owner is not the one printed above, ownership was transferred - check why.)");
        } else if (occ == Occupancy.FOREIGN) {
            console2.log("OCCUPIED BY SOMETHING ELSE. Refusing.");
            console2.log("The salt is deterministic, so re-running cannot clear this. Mine the NEXT");
            console2.log("salt - HookMiner.find with a higher starting seed, or change one byte of the");
            console2.log("constructor args - and re-run this script with the new one.");
            revert("mined address is occupied by code that is not this hook - mine the next salt");
        }

        if (!confirmed) {
            console2.log("");
            console2.log("PGAS_CONFIRM is not 1 -> stopping before any state change.");
            console2.log("Next, in order: Deploy -> InitPool -> Register -> a single small deposit.");
            return;
        }

        vm.startBroadcast();
        PgasIngressHook hook;
        if (occ == Occupancy.OURS) {
            // Already deployed. Skip the CREATE2 and carry on to the router, which is a plain
            // CREATE and therefore a different address every time.
            hook = PgasIngressHook(payable(hookAddress));
        } else {
            hook = new PgasIngressHook{salt: salt}(IPoolManager(poolManager), owner);
            require(address(hook) == hookAddress, "deployed address != mined address");
        }
        PgasRouter router = new PgasRouter(IPoolManager(poolManager), address(hook));
        vm.stopBroadcast();

        console2.log("hook  deployed at   :", address(hook));
        console2.log("router deployed at  :", address(router));
        console2.log("Remember: the router must be allow-listed by the owner (setAllowedRouter).");
    }
}
