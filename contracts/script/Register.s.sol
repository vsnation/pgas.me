// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Script, console2} from "forge-std/Script.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {PgasIngressHook} from "../src/PgasIngressHook.sol";
import {PgasAddresses} from "./PgasAddresses.sol";

/// @notice Register ONE route on a live gateway pool. **Write-once**: after this the owner can
///         only tighten it, never redirect it, so read every printed value before confirming.
///
/// @dev **DRY RUN BY DEFAULT.** No `--broadcast` sends nothing, and `PGAS_CONFIRM=1` is required
///      on top of that.
///
/// @dev `MAX_DEPOSIT` is not a taste: it is re-derived from the inner pool's live depth before
///      every arming (a share of the ±0.5% band), never carried over from a previous run, and
///      never a flat dollar cap. A pool initialised with ZERO liquidity looks real and pays
///      nothing, so the depth behind `INNER_FEE`/`INNER_TICK_SPACING` is read, not assumed.
///
/// @dev **This script READS the chain it is about to arm**, so point it at one: `--rpc-url <url>`
///      (read-only; a dry run still sends nothing). It refuses rather than passing when the pipe's
///      code is not the pinned bridge bytecode, or when the inner pool is uninitialised or empty —
///      and with no chain to read, every one of those reads comes back as "nothing there", which is
///      a refusal and not a pass. An unreadable query is not evidence of anything.
///
/// @dev The three fields a route can never take back — `grid`, `minRelayerFee` and `beamPubkey` —
///      are resolved and cross-checked by `resolveRoute`, which is `pure` and covered by the
///      suite. They used to be plain environment variables with a hard-coded default each; the
///      pubkey default in particular was the ETH pipe's key on EVERY route, and nothing on chain
///      or in this tooling would have caught it.
contract Register is Script {
    /// @notice Resolve `grid`, `minRelayerFee` and `beamPubkey` for a route, or refuse.
    /// @param pipe The Beam bridge pipe the output is pushed into.
    /// @param outAsset The route's OUTPUT currency — `address(0)` for native ETH.
    /// @param gridEnv `GRID`, or 0 to take the grid pinned for `outAsset`.
    /// @param minFeeEnv `MIN_RELAYER_FEE`, or 0 to take the tariff pinned for `outAsset`.
    /// @param pubkeyEnv `PUBKEY`, or empty to take the key pinned for `pipe` — which REFUSES for a
    ///        pipe whose key nobody has derived, rather than handing back somebody else's.
    /// @dev `pure` and `public` on purpose: this is the whole safety of an arming decision, and a
    ///      check that can only be exercised by running the script against a chain is a check
    ///      nobody exercises.
    function resolveRoute(address pipe, address outAsset, uint256 gridEnv, uint256 minFeeEnv, bytes memory pubkeyEnv)
        public
        pure
        returns (uint256 grid, uint256 minRelayerFee, bytes memory beamPubkey)
    {
        uint256 gridPinned = PgasAddresses.gridForOrZero(outAsset);
        grid = gridEnv == 0 ? gridPinned : gridEnv;
        require(grid != 0, "GRID: unpinned output asset - pass GRID = 10**(decimals-8) explicitly");
        require(gridPinned == 0 || grid == gridPinned, "GRID contradicts the output asset's decimals");

        uint256 feePinned = PgasAddresses.minRelayerFeeForOrZero(outAsset);
        minRelayerFee = minFeeEnv == 0 ? feePinned : minFeeEnv;
        require(minRelayerFee != 0, "MIN_RELAYER_FEE: unpinned output asset - pass the API's tariff explicitly");
        require(minRelayerFee >= grid, "MIN_RELAYER_FEE below one grid step is not a floor");

        // ONE KEY PER PIPE: the key is derived from (wallet master key, PIPE CID). The hook checks
        // 33 bytes and so does the bridge; the pubkey/pipe pairing is checked NOWHERE on chain, and
        // a route is write-once with `beamPubkey` absent from `tightenRoute`. A wrong key here
        // delivers 100% of every deposit on this route to a destination we do not control.
        beamPubkey = pubkeyEnv.length == 0 ? PgasAddresses.beamPubkeyFor(pipe) : pubkeyEnv;
        require(beamPubkey.length == 33, "PUBKEY must be a 33-byte compressed Beam public key");

        // The pipe/asset pairing IS self-checking on chain (a native pipe rejects a token amount,
        // an ERC-20 pipe rejects `msg.value`) — but only at the first deposit, after the route is
        // frozen. Check it here, where it is still free.
        if (
            pipe == PgasAddresses.ETH_PIPE || pipe == PgasAddresses.USDT_PIPE || pipe == PgasAddresses.WBTC_PIPE
                || pipe == PgasAddresses.DAI_PIPE
        ) {
            require(PgasAddresses.assetOfPipe(pipe) == outAsset, "PIPE does not carry the route's output asset");
        }
    }

    /// @notice Refuse unless the code living at `pipe` is the bytecode pinned for that pipe.
    /// @param pipe The address the route will send 100% of its output to, forever.
    /// @param liveCodehash `pipe.codehash`, read by the caller against the chain being armed.
    /// @dev The hook proves what it can from where it stands — that the pipe is a contract which
    ///      refuses calls it cannot serve. It CANNOT prove identity: an address with a payable
    ///      fallback takes the ETH, the balance post-condition passes, `PgasDeposit` is emitted and
    ///      nothing crosses the bridge. Identity is proven here, against a hash read off the live
    ///      bridge, on the operator's laptop, before anything is signed. `pure` on purpose.
    function requirePinnedPipe(address pipe, bytes32 liveCodehash) public pure {
        PgasAddresses.requirePinnedPipe(pipe, liveCodehash);
    }

    /// @notice Refuse a run with no chain behind it, before any read is mistaken for an answer.
    /// @param poolManagerCodeSize `POOL_MANAGER.code.length`, read by the caller. `pure`, so the
    ///        suite holds the refusal rather than a human remembering to try it both ways.
    function requireChainIsReadable(uint256 poolManagerCodeSize) public pure {
        PgasAddresses.requireChainIsReadable(poolManagerCodeSize);
    }

    /// @notice Read the INNER pool's price and depth. A read, with no opinion — the refusal is
    ///         `requireLiveInnerPool`, so a dry run can print the numbers it is about to refuse on.
    function readInnerPool(IPoolManager pm, PoolKey memory innerKey)
        public
        view
        returns (uint160 sqrtPriceX96, uint128 liquidity)
    {
        PoolId id = innerKey.toId();
        (sqrtPriceX96,,,) = StateLibrary.getSlot0(pm, id);
        liquidity = StateLibrary.getLiquidity(pm, id);
    }

    /// @notice Refuse an inner pool that is not initialised, or that holds no liquidity.
    /// @dev The inner pool is the whole economics of a route and NOTHING checked it: the
    ///      registration only compared the two keys' currencies. A key with the wrong fee tier or
    ///      tick spacing names a pool that does not exist — `sqrtPriceX96 == 0` — and an
    ///      initialised pool with no liquidity looks real and pays nothing (`InnerSwapEmpty`, at
    ///      the first deposit, after the route is frozen). Both are free to catch here and
    ///      permanent afterwards. `pure`: the read is the caller's, the decision is the suite's.
    function requireLiveInnerPool(uint160 sqrtPriceX96, uint128 liquidity) public pure {
        require(
            sqrtPriceX96 != 0, "INNER pool is not initialised - wrong fee tier or tick spacing, or no chain to read"
        );
        require(liquidity != 0, "INNER pool holds no liquidity - it looks real and would pay nothing");
    }

    function run() external {
        address hookAddr = vm.envAddress("HOOK");
        address poolManager = vm.envOr("POOL_MANAGER", PgasAddresses.POOL_MANAGER);
        address currency0 = vm.envOr("CURRENCY0", PgasAddresses.NATIVE);
        address currency1 = vm.envOr("CURRENCY1", PgasAddresses.USDC);
        uint24 innerFee = uint24(vm.envOr("INNER_FEE", uint256(PgasAddresses.INNER_FEE_3000)));
        int24 innerTickSpacing = int24(vm.envOr("INNER_TICK_SPACING", int256(PgasAddresses.INNER_TICK_SPACING_60)));
        address pipe = vm.envOr("PIPE", PgasAddresses.ETH_PIPE);
        uint256 maxRelayerFeeBps = vm.envOr("MAX_RELAYER_FEE_BPS", uint256(500));
        uint256 minDeposit = vm.envOr("MIN_DEPOSIT", uint256(1e6));
        uint256 maxDeposit = vm.envOr("MAX_DEPOSIT", uint256(500e6));
        // false: currency1 in, currency0 out — the USDC -> ETH shape.
        bool zeroForOne = vm.envOr("ZERO_FOR_ONE", false);
        bool confirmed = vm.envOr("PGAS_CONFIRM", false);

        require(currency0 < currency1, "currencies must be sorted: currency0 < currency1");

        address outAsset = zeroForOne ? currency1 : currency0;
        (uint256 grid, uint256 minRelayerFee, bytes memory beamPubkey) = resolveRoute(
            pipe,
            outAsset,
            vm.envOr("GRID", uint256(0)),
            vm.envOr("MIN_RELAYER_FEE", uint256(0)),
            vm.envOr("PUBKEY", bytes(""))
        );

        PoolKey memory outerKey = PoolKey({
            currency0: Currency.wrap(currency0),
            currency1: Currency.wrap(currency1),
            fee: PgasAddresses.GATEWAY_FEE,
            tickSpacing: PgasAddresses.GATEWAY_TICK_SPACING,
            hooks: IHooks(hookAddr)
        });
        PoolKey memory innerKey = PoolKey({
            currency0: Currency.wrap(currency0),
            currency1: Currency.wrap(currency1),
            fee: innerFee,
            tickSpacing: innerTickSpacing,
            hooks: IHooks(address(0))
        });

        // ── pre-flight, read from the chain being armed ───────────────────────────────────────
        // Neither of these was checked before: the registration compared the two keys' currencies
        // and took the pipe on the strength of `!= address(0)`.
        requireChainIsReadable(poolManager.code.length);
        bytes32 pipeCodehash = pipe.codehash;
        (uint160 innerSqrtPriceX96, uint128 innerLiquidity) = readInnerPool(IPoolManager(poolManager), innerKey);

        console2.log("=========================================================");
        console2.log(" Route registration  -  %s", confirmed ? "BROADCAST ARMED" : "DRY RUN (nothing is sent)");
        console2.log("=========================================================");
        console2.log("chain id           :", block.chainid);
        console2.log("hook               :", hookAddr);
        console2.log("poolManager        :", poolManager);
        console2.log("gateway pool id    :");
        console2.logBytes32(PoolId.unwrap(outerKey.toId()));
        console2.log("inner pool id      :");
        console2.logBytes32(PoolId.unwrap(innerKey.toId()));
        console2.log("currency0          :", currency0);
        console2.log("currency1          :", currency1);
        console2.log("inner fee          :", uint256(innerFee));
        console2.log("inner tickSpacing  :", int256(innerTickSpacing));
        console2.log("zeroForOne         :", zeroForOne);
        console2.log("output currency    :", outAsset);
        console2.log("inner sqrtPriceX96 :", uint256(innerSqrtPriceX96));
        console2.log("inner liquidity    :", uint256(innerLiquidity));
        console2.log("pipe               :", pipe);
        console2.log("pipe codehash      :");
        console2.logBytes32(pipeCodehash);
        console2.log("pipe codehash pinned for this pipe:");
        console2.logBytes32(PgasAddresses.pipeCodehashForOrZero(pipe));
        console2.log("grid               :", grid);
        console2.log("minRelayerFee      :", minRelayerFee);
        console2.log("maxRelayerFeeBps   :", maxRelayerFeeBps);
        console2.log("minDeposit         :", minDeposit);
        console2.log("maxDeposit         :", maxDeposit);
        console2.log("beam pubkey (33 B) :");
        console2.logBytes(beamPubkey);
        console2.log("");
        console2.log("WRITE-ONCE. The owner may afterwards tighten the deposit bounds, the relayer");
        console2.log("fee ceiling and the relayer fee FLOOR - never the pipe, the grid or the key.");
        console2.log("Before PGAS_CONFIRM=1: prove on the Beam side that this key is spendable for");
        console2.log("THIS pipe's cid. Nothing on chain checks the pubkey/pipe pairing - not the");
        console2.log("hook, not the bridge - so a wrong key delivers every deposit on this route to");
        console2.log("a destination we do not control, permanently.");

        // The refusals, on the numbers printed above. They run in the DRY RUN too — that is the
        // whole point: an unreadable chain, a dead inner pool or a pipe that is not the pinned
        // bridge bytecode stops the operator on the laptop, not at the broadcast.
        requirePinnedPipe(pipe, pipeCodehash);
        requireLiveInnerPool(innerSqrtPriceX96, innerLiquidity);

        if (!confirmed) {
            console2.log("PGAS_CONFIRM is not 1 -> stopping before any state change.");
            return;
        }

        vm.startBroadcast();
        PgasIngressHook(payable(hookAddr))
            .registerRoute(
                outerKey,
                innerKey,
                pipe,
                beamPubkey,
                grid,
                minRelayerFee,
                maxRelayerFeeBps,
                minDeposit,
                maxDeposit,
                zeroForOne
            );
        vm.stopBroadcast();
        console2.log("registered.");
    }
}
