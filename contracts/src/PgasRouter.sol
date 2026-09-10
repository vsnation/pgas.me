// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IMsgSender} from "@uniswap/v4-periphery/src/interfaces/IMsgSender.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {SafeCast} from "@uniswap/v4-core/src/libraries/SafeCast.sol";

/// @title PgasRouter
/// @author Pgas.me
/// @notice The thin caller for a Pgas gateway swap: `unlock → swap → settle the input`.
///         It holds nothing between transactions and takes no fee.
///
/// @dev Why this exists rather than the Universal Router. Four Universal Routers are live on
///      mainnet with four different builds, two Uniswap-published sources disagree on which is
///      "the" one, and their `ExactInputSingleParams` struct gained a field between versions. Worse,
///      through any UR the caller MUST set `amountOutMinimum = 0` — the hook takes 100% of the
///      output, so the router-level slippage bound would always trip. The usable slippage bound is
///      `minOut` inside `hookData`, enforced by the hook, which protects a UR route too. This
///      contract removes the ambiguity from the path we actually ship.
///
/// @dev ERC-20 sources use a plain `approve` to this router; the router moves the tokens straight
///      to the PoolManager with `sync → transferFrom → settle`. No Permit2, no allowance left
///      standing on a third party.
contract PgasRouter is IUnlockCallback, IMsgSender {
    using SafeCast for uint256;

    /// @notice Only the PoolManager may call `unlockCallback`.
    error NotPoolManager();
    /// @notice The pool's hook is not the Pgas ingress hook this router serves.
    error NotPgasPool(address hooks);
    /// @notice `msg.value` does not cover a native deposit, or was sent for an ERC-20 one.
    error BadMsgValue(uint256 sent, uint256 needed);
    /// @notice `amountIn` is zero.
    error AmountZero();
    /// @notice The refund of unused `msg.value` failed.
    error RefundFailed();
    /// @notice The ERC-20 pull from the payer failed.
    error TransferFromFailed(address token);

    /// @notice The Uniswap v4 PoolManager.
    IPoolManager public immutable poolManager;

    /// @notice The only hook whose pools this router will swap on.
    address public immutable hook;

    /// @dev Transient slot holding the end user for the duration of one `deposit` call, so the
    ///      hook can read it through `IMsgSender`. Transient storage leaves nothing behind.
    bytes32 private constant MSG_SENDER_SLOT = 0x3a2c1f2ba9d0dfe1a1e2b95d7f1fbb2b6a90a9dbc27fdbb1de6c9bdb0e9d3a11;

    struct CallbackData {
        address payer;
        PoolKey key;
        bool zeroForOne;
        uint256 amountIn;
        bytes hookData;
    }

    /// @param _poolManager The Uniswap v4 PoolManager.
    /// @param _hook The Pgas ingress hook.
    constructor(IPoolManager _poolManager, address _hook) {
        poolManager = _poolManager;
        hook = _hook;
    }

    /// @notice `poolManager` refunds unused native value through a bare call.
    /// @dev Value that comes to rest here is nobody's: `deposit` refunds the CALL's own surplus and
    ///      never this balance, and there is no owner and no rescue. That is deliberate — handing a
    ///      stranger's ETH to whoever deposits next is worse than leaving it untouched.
    receive() external payable {}

    /// @notice Swap `amountIn` on a Pgas gateway pool; the hook pipes the output to Beam.
    /// @param key The gateway pool. Its `hooks` must be `hook`.
    /// @param zeroForOne The swap direction; the input currency follows from it.
    /// @param amountIn Exact input, in the input currency's units.
    /// @param hookData `abi.encode(bytes32 ref, uint256 minOut, uint256 relayerFeeQuote)`.
    /// @dev For a native input, send `msg.value >= amountIn`; exactly `msg.value - amountIn` is
    ///      refunded to the caller. For an ERC-20 input, approve this router for `amountIn` first
    ///      and send no value; nothing is refunded, because nothing was sent.
    function deposit(PoolKey calldata key, bool zeroForOne, uint256 amountIn, bytes calldata hookData)
        external
        payable
    {
        if (amountIn == 0) revert AmountZero();
        if (address(key.hooks) != hook) revert NotPgasPool(address(key.hooks));

        Currency inCurrency = zeroForOne ? key.currency0 : key.currency1;
        bool nativeIn = Currency.unwrap(inCurrency) == address(0);
        if (nativeIn) {
            if (msg.value < amountIn) revert BadMsgValue(msg.value, amountIn);
        } else if (msg.value != 0) {
            revert BadMsgValue(msg.value, 0);
        }

        _setMsgSender(msg.sender);
        poolManager.unlock(
            abi.encode(
                CallbackData({
                    payer: msg.sender, key: key, zeroForOne: zeroForOne, amountIn: amountIn, hookData: hookData
                })
            )
        );
        _setMsgSender(address(0));

        // Refund THIS CALL's surplus — never the contract's balance. Anyone can force ETH into any
        // address (`selfdestruct`, a block reward, a pre-deployment transfer), and this contract has
        // no owner and no rescue, so a balance read here is not "unspent value", it is "unspent
        // value plus whatever a stranger left". Paying that out handed the next depositor somebody
        // else's money — and 1 wei of it was enough to brick every CONTRACT depositor with no
        // `receive()`, on a refund it never asked for. Stray value therefore stays where it is.
        //
        // The ERC-20 path is refunded NOTHING, and cannot be: `msg.value != 0` is refused above.
        if (nativeIn) {
            uint256 surplus = msg.value - amountIn; // `msg.value >= amountIn` was checked above
            if (surplus != 0) {
                (bool ok,) = msg.sender.call{value: surplus}("");
                if (!ok) revert RefundFailed();
            }
        }
    }

    /// @inheritdoc IUnlockCallback
    function unlockCallback(bytes calldata rawData) external override returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        CallbackData memory d = abi.decode(rawData, (CallbackData));

        // The gateway pool's own swap resolves to zero (the hook's return delta cancels it), so the
        // price limit is never reached; the extremes are passed for completeness.
        poolManager.swap(
            d.key,
            SwapParams({
                zeroForOne: d.zeroForOne,
                amountSpecified: -d.amountIn.toInt256(),
                sqrtPriceLimitX96: d.zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
            }),
            d.hookData
        );

        // The hook's `+amountIn` specified delta became the caller's debt. Pay it.
        Currency inCurrency = d.zeroForOne ? d.key.currency0 : d.key.currency1;
        _settle(inCurrency, d.payer, d.amountIn);
        return "";
    }

    /// @inheritdoc IMsgSender
    /// @dev Valid only while a `deposit` call is on the stack; zero at rest.
    function msgSender() external view override returns (address sender) {
        bytes32 slot = MSG_SENDER_SLOT;
        assembly ("memory-safe") {
            sender := tload(slot)
        }
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Internals
    // ─────────────────────────────────────────────────────────────────────────────────────────

    function _setMsgSender(address sender) private {
        bytes32 slot = MSG_SENDER_SLOT;
        assembly ("memory-safe") {
            tstore(slot, sender)
        }
    }

    /// @dev Native: hand the PoolManager the value. ERC-20: `sync`, move the payer's tokens
    ///      directly to the PoolManager, then `settle` the amount it observes.
    function _settle(Currency currency, address payer, uint256 amount) private {
        address token = Currency.unwrap(currency);
        if (token == address(0)) {
            poolManager.settle{value: amount}();
        } else {
            poolManager.sync(currency);
            (bool ok, bytes memory ret) = token.call(
                abi.encodeWithSignature("transferFrom(address,address,uint256)", payer, address(poolManager), amount)
            );
            if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFromFailed(token);
            poolManager.settle();
        }
    }
}
