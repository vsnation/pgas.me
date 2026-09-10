// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

/// @title ForeignUnlocker
/// @notice A caller with NO relationship to `PgasRouter` and no place in `allowedRouter`: it takes
///         the PoolManager lock itself and issues N swaps on a gateway pool inside ONE unlock, with
///         a `ref` of its own choosing.
///
/// @dev It exists to hold one fact still: the hook's `nonReentrant` blocks NESTING, not a sequence,
///         so one transaction can legitimately carry N deposits with N `PgasDeposit` logs and N
///         `NewLocalMessage` logs. `ref` is a hint, never an authorisation, and a reader that pairs
///         "the pipe log" with "the deposit log" by receipt rather than by adjacency will mis-pair
///         them. The suite asserts the adjacency that IS guaranteed.
contract ForeignUnlocker is IUnlockCallback {
    IPoolManager public immutable poolManager;

    error NotPoolManager();

    constructor(IPoolManager _poolManager) {
        poolManager = _poolManager;
    }

    receive() external payable {}

    struct Data {
        PoolKey key;
        bool zeroForOne;
        uint256 amountIn;
        uint256 times;
        bytes hookData;
    }

    /// @param times How many swaps to issue inside the single unlock.
    function swapMany(PoolKey calldata key, bool zeroForOne, uint256 amountIn, uint256 times, bytes calldata hookData)
        external
    {
        poolManager.unlock(
            abi.encode(Data({key: key, zeroForOne: zeroForOne, amountIn: amountIn, times: times, hookData: hookData}))
        );
    }

    function unlockCallback(bytes calldata raw) external override returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        Data memory d = abi.decode(raw, (Data));

        for (uint256 i = 0; i < d.times; i++) {
            poolManager.swap(
                d.key,
                SwapParams({
                    zeroForOne: d.zeroForOne,
                    amountSpecified: -int256(d.amountIn),
                    sqrtPriceLimitX96: d.zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
                }),
                d.hookData
            );
        }

        // Pay the whole debt from this contract's own funds — the deposits are its own.
        Currency inCurrency = d.zeroForOne ? d.key.currency0 : d.key.currency1;
        uint256 owed = d.amountIn * d.times;
        address token = Currency.unwrap(inCurrency);
        if (token == address(0)) {
            poolManager.settle{value: owed}();
        } else {
            poolManager.sync(inCurrency);
            (bool ok, bytes memory ret) =
                token.call(abi.encodeWithSignature("transfer(address,uint256)", address(poolManager), owed));
            require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "transfer failed");
            poolManager.settle();
        }
        return "";
    }
}
