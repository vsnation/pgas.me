// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

/// @title PipeSplit
/// @notice Splits an amount into the part the Beam bridge can mint (`value`) and the part the
///         relayer is paid (`relayerFee`), such that `value + relayerFee == amount` exactly.
/// @dev Beam is always 8 decimals. An 18-decimal asset crossing the pipe has its last 10 digits
///      dropped by the bridge, and **a non-zero sub-grid tail is unmintable and stuck forever**
///      (a 0.35619677 ETH lock on 2026-08-31 stranded its tail exactly this way). So `value` is
///      floored onto `grid = 10**(decimals-8)` — 1e10 for ETH/DAI, 1 for WBTC (8 decimals, a
///      no-op floor) — and the tail rides on the relayer fee.
///
///      `minRelayerFee` is the relayer's quoted e2b tariff, NOT a residue this library invents:
///      it is a price we are quoted (0.02 BEAM of value in the sent asset). The tail is added to
///      it, never substituted for it.
///
///      This is the SAME algorithm as `api/pgasme/ethpipe.py:split_amount`. Two implementations
///      of one fact will disagree and one of them reaches money, so both are pinned to the
///      shared golden-vector file `test/vectors/grid.json`.
library PipeSplit {
    /// @notice `grid` must be at least 1 — a zero grid has no floor to apply.
    error GridZero();
    /// @notice `amount` does not cover the relayer fee (amount <= minRelayerFee).
    error AmountBelowRelayerFee(uint256 amount, uint256 minRelayerFee);
    /// @notice What is left after the fee is smaller than one grid step: nothing can be minted.
    error NothingMintable(uint256 amount, uint256 minRelayerFee, uint256 grid);

    /// @param amount The whole sum arriving at the pipe, in the asset's Ethereum units.
    /// @param minRelayerFee The relayer's quoted tariff, same units.
    /// @param grid `10**(decimals-8)`; 1 means no floor.
    /// @return value The mintable part, always a multiple of `grid` and always > 0.
    /// @return relayerFee `amount - value`; always >= `minRelayerFee`.
    function split(uint256 amount, uint256 minRelayerFee, uint256 grid)
        internal
        pure
        returns (uint256 value, uint256 relayerFee)
    {
        if (grid < 1) revert GridZero();
        if (amount <= minRelayerFee) revert AmountBelowRelayerFee(amount, minRelayerFee);
        unchecked {
            value = ((amount - minRelayerFee) / grid) * grid;
        }
        if (value == 0) revert NothingMintable(amount, minRelayerFee, grid);
        unchecked {
            relayerFee = amount - value;
        }
    }
}
