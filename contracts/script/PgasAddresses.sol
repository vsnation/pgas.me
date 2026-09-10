// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title PgasAddresses
/// @notice Every mainnet address and constant this build pins, in ONE place.
/// @dev Two implementations of one fact will disagree and one of them reaches money — so scripts
///      and fork tests read these, and nothing hardcodes an address of its own.
library PgasAddresses {
    // ── Uniswap v4, Ethereum mainnet ──────────────────────────────────────────────────────────
    address internal constant POOL_MANAGER = 0x000000000004444c5dc75cB358380D2e3dE08A90;
    /// @notice The canonical CREATE2 proxy every hook is deployed through.
    address internal constant CREATE2_PROXY = 0x4e59b44847b379578588920cA78FbF26c0B4956C;

    // ── Tokens ────────────────────────────────────────────────────────────────────────────────
    address internal constant NATIVE = address(0);
    address internal constant USDC = 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48;
    address internal constant USDT = 0xdAC17F958D2ee523a2206206994597C13D831ec7;
    address internal constant DAI = 0x6B175474E89094C44Da98b954EedeAC495271d0F;
    address internal constant WBTC = 0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599;

    // ── Beam bridge pipes ─────────────────────────────────────────────────────────────────────
    address internal constant ETH_PIPE = 0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96;
    address internal constant USDT_PIPE = 0x7C3Fe09E86b0d8661d261a49Bfa385536b7077f9;
    address internal constant WBTC_PIPE = 0x604422D7eC88c45b82B71851d073eFeaA928dcEF;
    address internal constant DAI_PIPE = 0xAcDc8f4559741a3c8CAAB0ba74c57807A9Fe2d73;

    // ── Pipe bytecode identity ────────────────────────────────────────────────────────────────
    // A route's `pipe` is write-once and receives 100% of the output. The hook can prove a
    // candidate is a contract that refuses calls it cannot serve (`pipeRejectsUnknownCalls`), and
    // that is all it can prove from where it stands — a fallback-only payable sink takes the ETH,
    // the balance post-condition passes and nothing is bridged. Identity has to be pinned, and
    // this is where: the code hash the bridge's own deployments actually carry, read off mainnet
    // and compared against the live address by the arming script BEFORE the broadcast.

    /// @notice `EXTCODEHASH` of `ETH_PIPE`, read from mainnet at the fork suite's pinned block.
    bytes32 internal constant ETH_PIPE_CODEHASH = 0x44f00b5441a2c3d2da0d2ce424122ac7579cfdf765783f3f5dc5d9b3685b315d;

    /// @notice `EXTCODEHASH` shared by ALL THREE ERC-20 pipes — one build, three deployments, byte
    ///         for byte identical (2,007 bytes each). The ETH pipe is a different contract (1,336
    ///         bytes) and therefore a different hash; that is why there are two constants and not
    ///         one per pipe. The fork suite asserts both against the live addresses.
    bytes32 internal constant ERC20_PIPE_CODEHASH = 0xbfb427024ce037f158f31749e19c5adc4e46aa3a16bd1373d932f5216a30cf21;

    /// @notice `keccak256("")` — what `EXTCODEHASH` returns for an account that EXISTS and holds no
    ///         code. A non-existent account returns 0. Neither is a pipe, and they are told apart
    ///         so the refusal can say which.
    bytes32 internal constant EMPTY_CODEHASH = 0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470;

    /// @notice `NewLocalMessage(uint64,uint256,uint256,bytes)` — all fields non-indexed.
    bytes32 internal constant NEW_LOCAL_MESSAGE_TOPIC0 =
        0x5f52670be4e2f3d7b079180b485ab44712641a10d1c77e843355f96036608ac7;

    /// @notice `PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)` —
    ///         `ref`, `payer` and `tokenIn` indexed; `amountIn`, `target`, `value`, `relayerFee`,
    ///         `receiverBeamPubkey` in the data, IN THAT ORDER. Pinned here and in
    ///         `test/vectors/events.json` because a reader on the other side of this log has no
    ///         other way to know it: the design document's draft signature is a DIFFERENT topic0.
    bytes32 internal constant PGAS_DEPOSIT_TOPIC0 = 0xd0e41515b729c9019d2f260f5fb9501b4fc547032008d04c4cdbfda6b788ae2b;

    // ── Grids: 10**(decimals - 8) ─────────────────────────────────────────────────────────────
    uint256 internal constant GRID_18DEC = 1e10; // ETH, DAI
    uint256 internal constant GRID_8DEC = 1; // WBTC — no floor at all
    uint256 internal constant GRID_6DEC = 1; // USDT is 6 decimals; the pipe scales up, no tail

    /// @notice The grid an asset MUST be registered with. The hook derives the same number on
    ///         chain from `decimals()` (`PgasIngressHook.requiredGrid`) and refuses anything else;
    ///         this copy exists so a dry run fails on the operator's laptop instead of at the
    ///         broadcast. The fork suite asserts the two agree, asset by asset — two
    ///         implementations of one fact will disagree, and one of them reaches money.
    function gridFor(address asset) internal pure returns (uint256) {
        uint256 g = gridForOrZero(asset);
        if (g == 0) revert("PgasAddresses: no grid pinned for this asset - add it, do not guess");
        return g;
    }

    /// @notice `gridFor` for tooling that must keep going on an unpinned asset (a test token on a
    ///         test chain): 0 means "nothing is pinned here", never "the grid is zero".
    function gridForOrZero(address asset) internal pure returns (uint256) {
        if (asset == NATIVE || asset == DAI) return GRID_18DEC; // 18 decimals
        if (asset == WBTC) return GRID_8DEC; // 8 decimals
        if (asset == USDT || asset == USDC) return GRID_6DEC; // 6 decimals, below the Beam grid
        return 0;
    }

    // ── Relayer tariffs ───────────────────────────────────────────────────────────────────────
    // The floor a route is registered with, per asset. These are the SAME numbers the API quotes
    // from (`api/pgasme/config.py: min_relayer_fee_*`, read through `ethpipe.min_relayer_fee_units`)
    // — not a round grid step chosen here, because a floor invented on this side is a second
    // implementation of a price we are quoted, and the two would drift.
    uint256 internal constant MIN_RELAYER_FEE_ETH_WEI = 100_000_000_000; // 1e11 wei
    uint256 internal constant MIN_RELAYER_FEE_DAI_UNITS = 200_000_000_000_000; // 0.0002 DAI
    uint256 internal constant MIN_RELAYER_FEE_WBTC_UNITS = 1; // 1 sat — WBTC has no grid

    /// @notice The route floor to register for an output asset; 0 where nothing is pinned.
    function minRelayerFeeForOrZero(address asset) internal pure returns (uint256) {
        if (asset == NATIVE) return MIN_RELAYER_FEE_ETH_WEI;
        if (asset == DAI) return MIN_RELAYER_FEE_DAI_UNITS;
        if (asset == WBTC) return MIN_RELAYER_FEE_WBTC_UNITS;
        return 0;
    }

    // ── Inner (canonical, hook-less) v4 pools ─────────────────────────────────────────────────
    uint24 internal constant INNER_FEE_3000 = 3000;
    int24 internal constant INNER_TICK_SPACING_60 = 60;

    // ── Gateway pool shape ────────────────────────────────────────────────────────────────────
    uint24 internal constant GATEWAY_FEE = 0;
    int24 internal constant GATEWAY_TICK_SPACING = 1;
    uint160 internal constant SQRT_PRICE_1_1 = 79228162514264337593543950336;

    /// @notice The hook address's low 14 bits.
    uint160 internal constant HOOK_FLAGS = 0x2888;

    /// @notice Our Beam pipe public key for bETH — 33 bytes, compressed. Safe to publish: it is
    ///         derived from (wallet master key, **the ETH pipe's cid**) and only names a
    ///         destination.
    /// @dev ONE KEY PER PIPE. This key belongs to `ETH_PIPE` and to nothing else: point it at
    ///      another pipe and every deposit on that route mints the other asset to a key derived for
    ///      a different cid, permanently, because the route is write-once and `beamPubkey` is
    ///      absent from `tightenRoute`. Nothing on chain can catch that: the pipe/asset pairing is
    ///      self-checking by ABI (a native pipe rejects a token amount and vice versa) but the
    ///      pubkey/pipe pairing is checked NOWHERE. Use `beamPubkeyFor`.
    function beamPubkeyETH() internal pure returns (bytes memory) {
        return hex"714d04766d1072b67f00913b6d21486dfda79e6e65f841e43ca9aa64f81809d001";
    }

    /// @notice The Beam public key pinned for `pipe`, or a refusal. There is deliberately no
    ///         default: a pipe with no key pinned here is a pipe whose key nobody has derived yet,
    ///         and guessing produces a route that works perfectly and delivers to the wrong place.
    function beamPubkeyFor(address pipe) internal pure returns (bytes memory) {
        if (pipe == ETH_PIPE) return beamPubkeyETH();
        revert("PgasAddresses: no Beam pubkey pinned for this pipe - derive it from (master key, pipe cid) first");
    }

    /// @notice The asset a pinned pipe carries. A route's OUTPUT currency must be this, or the
    ///         registration is pointing the swap at the wrong bridge.
    function assetOfPipe(address pipe) internal pure returns (address) {
        if (pipe == ETH_PIPE) return NATIVE;
        if (pipe == USDT_PIPE) return USDT;
        if (pipe == WBTC_PIPE) return WBTC;
        if (pipe == DAI_PIPE) return DAI;
        revert("PgasAddresses: unknown pipe");
    }

    /// @notice Refuse when there is no chain behind this run.
    /// @param poolManagerCodeSize `POOL_MANAGER.code.length`, read by the caller.
    /// @dev Every pre-flight in these scripts is a READ, and `forge script` with no `--rpc-url`
    ///      runs against an empty in-memory EVM where every read comes back "nothing there". That
    ///      is indistinguishable from a genuine answer and reads as reassurance — "the mined
    ///      address is free", "the pipe is not there". The PoolManager is the one address that must
    ///      exist on any chain worth arming, so its code size is the whole test: no code, no chain,
    ///      no verdict. An unreadable query is not evidence of anything.
    function requireChainIsReadable(uint256 poolManagerCodeSize) internal pure {
        require(
            poolManagerCodeSize != 0,
            "no chain to read: the PoolManager has no code here - pass --rpc-url <url> (still read-only)"
        );
    }

    /// @notice The `EXTCODEHASH` pinned for `pipe`, or 0 where nothing is pinned. 0 means "nobody
    ///         has pinned this address", never "any code will do".
    function pipeCodehashForOrZero(address pipe) internal pure returns (bytes32) {
        if (pipe == ETH_PIPE) return ETH_PIPE_CODEHASH;
        if (pipe == USDT_PIPE || pipe == WBTC_PIPE || pipe == DAI_PIPE) return ERC20_PIPE_CODEHASH;
        return 0;
    }

    /// @notice `pipeCodehashForOrZero` with the refusal. There is deliberately no default: a pipe
    ///         nobody has pinned is a pipe nobody has read, and guessing produces a route that
    ///         registers perfectly and delivers to a contract we never inspected.
    function pipeCodehashFor(address pipe) internal pure returns (bytes32) {
        bytes32 h = pipeCodehashForOrZero(pipe);
        if (h == 0) {
            revert("PgasAddresses: unpinned pipe - read its EXTCODEHASH off chain and pin it before a route names it");
        }
        return h;
    }

    /// @notice Refuse unless the code LIVING at `pipe` is the bytecode pinned for it.
    /// @param pipe The candidate pipe.
    /// @param liveCodehash What `pipe.codehash` says right now — read by the CALLER, on the chain
    ///        it is about to arm, so this decision stays `pure` and the suite can exercise every
    ///        refusal offline instead of only by pointing a script at a chain.
    function requirePinnedPipe(address pipe, bytes32 liveCodehash) internal pure {
        bytes32 want = pipeCodehashFor(pipe);
        require(
            liveCodehash != 0 && liveCodehash != EMPTY_CODEHASH,
            "PIPE has no code at this address - wrong address, or this run has no chain to read (pass --rpc-url)"
        );
        require(liveCodehash == want, "PIPE is not the pinned bridge bytecode - refusing rather than guessing");
    }
}
