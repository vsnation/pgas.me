// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

/// @title IPipeNative
/// @notice The Beam bridge pipe for a NATIVE asset (ETH).
/// @dev `msg.value` must equal `value + relayerFee` **exactly** — the pipe reverts with
///      "Invalid sent fund" otherwise. It mints `value` on Beam and pays the relayer
///      `relayerFee`. Selector is `keccak256("sendFunds(uint256,uint256,bytes)")[:4]`
///      (`0x4d5dd2bc`), never hardcoded here — the compiler derives it.
interface IPipeNative {
    function sendFunds(uint256 value, uint256 relayerFee, bytes calldata receiverBeamPubkey) external payable;
}

/// @title IPipeERC20
/// @notice The Beam bridge pipe for an ERC-20 asset (DAI / WBTC / USDT).
/// @dev The pipe pulls `value + relayerFee` by `transferFrom`, so the caller must hold an
///      allowance of exactly that amount. Never `MaxUint256`.
interface IPipeERC20 {
    function sendFunds(uint256 value, uint256 relayerFee, bytes calldata receiverBeamPubkey) external;
}

/// @title IPipeEvents
/// @notice The single log every `sendFunds` emits. All fields are NON-indexed;
///         topic0 is `0x5f52670be4e2f3d7b079180b485ab44712641a10d1c77e843355f96036608ac7`.
interface IPipeEvents {
    event NewLocalMessage(uint64 msgId, uint256 amount, uint256 relayerFee, bytes receiver);
}
