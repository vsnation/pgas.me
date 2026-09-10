// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PgasAddresses} from "./PgasAddresses.sol";

/// @notice Initialise ONE gateway pool: fee 0, tickSpacing 1, `sqrtPriceX96 = 2**96`, zero
///         liquidity forever. The hook's `beforeInitialize` refuses any other shape and refuses
///         any caller but the owner, so this must run from the owner key.
///
/// @dev **DRY RUN BY DEFAULT.** No `--broadcast` sends nothing, and `PGAS_CONFIRM=1` is required
///      on top of that. Every value is printed first.
///
/// `HOOK=0x… CURRENCY0=0x0000…0000 CURRENCY1=0xA0b8…eB48 forge script script/InitPool.s.sol`
contract InitPool is Script {
    function run() external {
        address poolManager = vm.envOr("POOL_MANAGER", PgasAddresses.POOL_MANAGER);
        address hook = vm.envAddress("HOOK");
        address currency0 = vm.envOr("CURRENCY0", PgasAddresses.NATIVE);
        address currency1 = vm.envOr("CURRENCY1", PgasAddresses.USDC);
        bool confirmed = vm.envOr("PGAS_CONFIRM", false);

        require(currency0 < currency1, "currencies must be sorted: currency0 < currency1");

        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(currency0),
            currency1: Currency.wrap(currency1),
            fee: PgasAddresses.GATEWAY_FEE,
            tickSpacing: PgasAddresses.GATEWAY_TICK_SPACING,
            hooks: IHooks(hook)
        });

        console2.log("=========================================================");
        console2.log(" Gateway pool init  -  %s", confirmed ? "BROADCAST ARMED" : "DRY RUN (nothing is sent)");
        console2.log("=========================================================");
        console2.log("chain id       :", block.chainid);
        console2.log("poolManager    :", poolManager);
        console2.log("hook           :", hook);
        console2.log("currency0      :", currency0);
        console2.log("currency1      :", currency1);
        console2.log("fee            :", uint256(PgasAddresses.GATEWAY_FEE));
        console2.log("tickSpacing    :", int256(PgasAddresses.GATEWAY_TICK_SPACING));
        console2.log("sqrtPriceX96   :", uint256(PgasAddresses.SQRT_PRICE_1_1));
        console2.log("pool id        :");
        console2.logBytes32(PoolId.unwrap(key.toId()));

        if (!confirmed) {
            console2.log("");
            console2.log("PGAS_CONFIRM is not 1 -> stopping before any state change.");
            return;
        }

        vm.startBroadcast();
        IPoolManager(poolManager).initialize(key, PgasAddresses.SQRT_PRICE_1_1);
        vm.stopBroadcast();
        console2.log("initialised.");
    }
}
