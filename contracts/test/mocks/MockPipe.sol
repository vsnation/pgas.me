// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPipeEvents} from "../../src/interfaces/IPipe.sol";

/// @title MockPipeNative
/// @notice Offline double for the mainnet `EthPipe` `0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96`.
/// @dev Same ABI, same event (all fields non-indexed, topic0
///      `0x5f52670be4e2f3d7b079180b485ab44712641a10d1c77e843355f96036608ac7`) and the same
///      `msg.value == value + relayerFee` rule, which the real pipe enforces with the revert
///      string "Invalid sent fund". A double that is laxer than the thing it stands in for is a
///      test that passes for a contract that would fail.
contract MockPipeNative is IPipeEvents {
    /// @notice Highest message id issued so far. The bridge counts from 1.
    uint64 public msgCount;

    /// @notice Every call this pipe accepted, in order, for assertions.
    struct Sent {
        uint64 msgId;
        uint256 value;
        uint256 relayerFee;
        uint256 msgValue;
        bytes receiver;
        address caller;
    }

    Sent[] internal _sent;

    function sendFunds(uint256 value, uint256 relayerFee, bytes calldata receiverBeamPubkey) external payable {
        require(msg.value == value + relayerFee, "Invalid sent fund");
        require(receiverBeamPubkey.length == 33, "Invalid pubkey");
        require(value > 0, "Invalid value");
        uint64 id = ++msgCount;
        _sent.push(
            Sent({
                msgId: id,
                value: value,
                relayerFee: relayerFee,
                msgValue: msg.value,
                receiver: receiverBeamPubkey,
                caller: msg.sender
            })
        );
        // The bridge mints `value`; `amount` in the log is the minted value, not the sum.
        emit NewLocalMessage(id, value, relayerFee, receiverBeamPubkey);
    }

    function sentCount() external view returns (uint256) {
        return _sent.length;
    }

    function sentAt(uint256 i) external view returns (Sent memory) {
        return _sent[i];
    }

    function lastSent() external view returns (Sent memory) {
        return _sent[_sent.length - 1];
    }
}

/// @title MockPipeERC20
/// @notice Offline double for `EthERC20Pipe` (DAI / WBTC / USDT). Pulls `value + relayerFee`.
contract MockPipeERC20 is IPipeEvents {
    uint64 public msgCount;
    address public immutable token;

    constructor(address _token) {
        token = _token;
    }

    function sendFunds(uint256 value, uint256 relayerFee, bytes calldata receiverBeamPubkey) external {
        require(receiverBeamPubkey.length == 33, "Invalid pubkey");
        require(value > 0, "Invalid value");
        (bool ok, bytes memory ret) = token.call(
            abi.encodeWithSignature(
                "transferFrom(address,address,uint256)", msg.sender, address(this), value + relayerFee
            )
        );
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "Invalid sent fund");
        emit NewLocalMessage(++msgCount, value, relayerFee, receiverBeamPubkey);
    }
}

/// @title RevertingPipe
/// @notice A pipe that always reverts — proves a failed `sendFunds` unwinds the whole deposit.
contract RevertingPipe {
    function sendFunds(uint256, uint256, bytes calldata) external payable {
        revert("Invalid sent fund");
    }
}

/// @title ReentrantPipe
/// @notice A pipe that calls back into the router mid-deposit, to prove the hook's guard holds.
contract ReentrantPipe {
    address public target;
    bytes public payload;

    function arm(address _target, bytes calldata _payload) external {
        target = _target;
        payload = _payload;
    }

    function sendFunds(uint256, uint256, bytes calldata) external payable {
        (bool ok, bytes memory ret) = target.call(payload);
        if (!ok) {
            assembly ("memory-safe") {
                revert(add(ret, 0x20), mload(ret))
            }
        }
    }
}

/// @title UnderPullingPipeERC20
/// @notice A pipe that draws only `value` and leaves `relayerFee` standing, then logs the whole
///         sum as delivered. This is the shape that made an ERC-20 deposit "succeed" while
///         79,810,399,032 units of the target token sat in the hook and both logs said otherwise —
///         §BROADCAST-IS-NOT-DONE with no on-chain evidence the money moved.
contract UnderPullingPipeERC20 is IPipeEvents {
    uint64 public msgCount;
    address public immutable token;

    constructor(address _token) {
        token = _token;
    }

    function sendFunds(uint256 value, uint256 relayerFee, bytes calldata receiverBeamPubkey) external {
        require(receiverBeamPubkey.length == 33, "Invalid pubkey");
        (bool ok, bytes memory ret) = token.call(
            abi.encodeWithSignature("transferFrom(address,address,uint256)", msg.sender, address(this), value)
        );
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "Invalid sent fund");
        // The lie: the log reports value + relayerFee as crossed.
        emit NewLocalMessage(++msgCount, value, relayerFee, receiverBeamPubkey);
    }
}

/// @title RefundingPipeNative
/// @notice The native mirror of `UnderPullingPipeERC20`: it honours `msg.value == value +
///         relayerFee`, emits the log, and hands the relayer fee straight back. The pipe's own
///         require is satisfied and the money is still here.
contract RefundingPipeNative is IPipeEvents {
    uint64 public msgCount;

    function sendFunds(uint256 value, uint256 relayerFee, bytes calldata receiverBeamPubkey) external payable {
        require(msg.value == value + relayerFee, "Invalid sent fund");
        require(receiverBeamPubkey.length == 33, "Invalid pubkey");
        emit NewLocalMessage(++msgCount, value, relayerFee, receiverBeamPubkey);
        if (relayerFee != 0) {
            (bool ok,) = msg.sender.call{value: relayerFee}("");
            require(ok, "refund failed");
        }
    }
}

/// @title FallbackSink
/// @notice A payable contract with nothing but a fallback. Registered as a NATIVE pipe it is the
///         quietest loss in this codebase: `sendFunds{value: out}` hits the fallback and returns,
///         the hook's balance post-condition passes (the ETH really did leave), `PgasDeposit` is
///         emitted with a `value` and a `relayerFee`, and not one wei crossed the bridge. Nothing
///         is rescuable afterwards and a route is write-once. `registerRoute` must refuse it.
contract FallbackSink {
    fallback() external payable {}
}

/// @title ReceiveOnlySink
/// @notice The near miss: `receive()` but no fallback. `sendFunds(...)` carries calldata, so it
///         cannot reach `receive()` and the call reverts — this one is refused by accident rather
///         than by design, which is exactly why the refusal must not rest on it.
contract ReceiveOnlySink {
    receive() external payable {}
}

/// @title NoDecimalsToken
/// @notice A token that answers `decimals()` with nothing at all. The grid cannot be derived from
///         it, so a route naming it must be refused rather than guessed at.
contract NoDecimalsToken {
    mapping(address => uint256) public balanceOf;

    function transferFrom(address, address, uint256) external pure returns (bool) {
        return true;
    }

    function approve(address, uint256) external pure returns (bool) {
        return true;
    }
}

/// @title StorageWritingSink
/// @notice The sink the probe CANNOT catch, kept in the suite so nobody has to rediscover it.
///         Its fallback writes storage, so under `STATICCALL` — which is how
///         `pipeRejectsUnknownCalls` asks — it reverts, and a revert is exactly what the probe
///         reads as "this address refuses calls it does not implement". It passes. Called for
///         real by `sendFunds{value: out}` the write succeeds, the ETH stays here and nothing
///         crosses the bridge: the same loss as `FallbackSink`, wearing the right clothes.
///         What stands between this and a route is the pinned `EXTCODEHASH` in
///         `script/PgasAddresses.sol`, checked off the live chain by the fork suite — not the probe.
contract StorageWritingSink {
    uint256 public calls;

    fallback() external payable {
        calls++;
    }
}
