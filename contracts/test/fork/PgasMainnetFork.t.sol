// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Test, console2} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {HookMiner} from "@uniswap/v4-periphery/src/utils/HookMiner.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";

import {PgasIngressHook} from "../../src/PgasIngressHook.sol";
import {PgasRouter} from "../../src/PgasRouter.sol";
import {PgasAddresses} from "../../script/PgasAddresses.sol";
import {MockPipeNative, MockPipeERC20} from "../mocks/MockPipe.sol";

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
    function approve(address, uint256) external returns (bool);
}

/// @title PgasMainnetForkTest
/// @notice The mainnet-fork suite — the only evidence the ingress path works before real money
///         touches it. Real PoolManager, real deep v4 pools, real Beam bridge pipes.
///
/// @dev Offline elsewhere by construction: this file runs only under `FOUNDRY_PROFILE=fork` and
///      needs `FORK_RPC_URL` in the environment. A bare `forge test` never reaches it.
///
/// @dev Spikes promoted here:
///      * S3 — a hook MAY call `poolManager.swap()` on a DIFFERENT pool from inside `beforeSwap`,
///        and all four deltas net to zero. The whole design rests on this.
///      * S4 — the real `EthPipe` accepts `sendFunds` from a CONTRACT sender, and its
///        `NewLocalMessage` carries our public key and the grid-floored value.
contract PgasMainnetForkTest is Test {
    /// @dev Pinned so depth, price and gas are reproducible. Depth is still re-read before any
    ///      arming decision — a pinned block is a fixture, never a claim about today's market.
    uint256 internal constant FORK_BLOCK = 25_942_000;

    IPoolManager internal poolManager;
    PgasIngressHook internal hook;
    PgasRouter internal router;

    address internal owner = makeAddr("pgas-owner");
    address internal user = makeAddr("pgas-user");

    Currency internal ethCurrency = Currency.wrap(PgasAddresses.NATIVE);
    Currency internal usdcCurrency = Currency.wrap(PgasAddresses.USDC);
    Currency internal usdtCurrency = Currency.wrap(PgasAddresses.USDT);

    /// @dev The real deep ETH/USDC 0.30% pool, no hooks — where the swap actually happens.
    PoolKey internal innerUsdc;
    /// @dev The real deep ETH/USDT 0.30% pool, no hooks.
    PoolKey internal innerUsdt;
    /// @dev Our zero-liquidity gateway pools.
    PoolKey internal gatewayUsdcToEth;
    PoolKey internal gatewayEthToUsdt;

    uint256 internal constant USDC_IN = 50e6; // 50 USDC
    uint256 internal constant ETH_IN = 0.01 ether;

    /// @dev The e2b relayer tariff for ETH — a price we are quoted, never a residue this code
    ///      invents, and never a round number chosen here. It is the SAME constant the API quotes
    ///      from (`min_relayer_fee_wei`), pinned once in `PgasAddresses`: registering a route at
    ///      "one grid step" because it looked tidy would put the floor an order of magnitude under
    ///      the tariff, and a route's floor cannot be lowered afterwards — nor, until now, raised.
    uint256 internal constant RELAYER_FEE_QUOTE_ETH = PgasAddresses.MIN_RELAYER_FEE_ETH_WEI; // 1e11 wei
    uint256 internal constant MIN_RELAYER_FEE_ETH = PgasAddresses.MIN_RELAYER_FEE_ETH_WEI;

    /// @dev `minOut` is the only slippage bound on this path and `0` is refused, so the fork suite
    ///      states real ones: ~1% under the measured output at this block.
    uint256 internal constant MIN_OUT_ETH = 0.0199 ether; // 50 USDC buys ~0.020094 ETH here
    uint256 internal constant MIN_OUT_USDT = 24_000_000; // 0.01 ETH buys ~24.71 USDT here

    /// @dev A TEST-ONLY receiver key for the USDT route below. It is deliberately NOT
    ///      `beamPubkeyETH()`: that key is derived from (wallet master key, the ETH PIPE's cid) and
    ///      naming it on a USDT route would mint bUSDT to a key derived for another pipe — which
    ///      nothing on chain checks, and which a write-once route can never take back. This suite
    ///      used to model exactly that mistake.
    bytes internal constant TEST_ONLY_USDT_PUBKEY =
        hex"02deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef";

    function setUp() public {
        string memory rpc = vm.envOr("FORK_RPC_URL", string("https://eth.drpc.org"));
        vm.createSelectFork(rpc, FORK_BLOCK);

        poolManager = IPoolManager(PgasAddresses.POOL_MANAGER);

        innerUsdc = PoolKey({
            currency0: ethCurrency,
            currency1: usdcCurrency,
            fee: PgasAddresses.INNER_FEE_3000,
            tickSpacing: PgasAddresses.INNER_TICK_SPACING_60,
            hooks: IHooks(address(0))
        });
        innerUsdt = PoolKey({
            currency0: ethCurrency,
            currency1: usdtCurrency,
            fee: PgasAddresses.INNER_FEE_3000,
            tickSpacing: PgasAddresses.INNER_TICK_SPACING_60,
            hooks: IHooks(address(0))
        });

        bytes memory args = abi.encode(poolManager, owner);
        (address expected, bytes32 salt) =
            HookMiner.find(address(this), PgasAddresses.HOOK_FLAGS, type(PgasIngressHook).creationCode, args);
        hook = new PgasIngressHook{salt: salt}(poolManager, owner);
        assertEq(address(hook), expected, "mined address");
        assertEq(uint256(uint160(address(hook))) & 0x3FFF, 0x2888, "hook flags 0x2888");

        router = new PgasRouter(poolManager, address(hook));
        vm.prank(owner);
        hook.setAllowedRouter(address(router), true);

        // FORK TRAP, verified this session: a contract `new`-ed inside a fork test lands on a
        // deterministic address that may ALREADY hold a balance on mainnet. `PgasRouter` lands on
        // 0x2e234DAe75C793f67A35089C9d99245E1C58470b, which holds exactly 1 wei at this block. That
        // wei used to reach the user — the router refunded its whole BALANCE — and an exact
        // assertion failed for a reason that had nothing to do with this code; the router now
        // refunds the call's own surplus and leaves a stranger's wei alone (see
        // `test_forcedEthIsNotPaidToTheNextDepositor`). These two lines stay anyway, so that what
        // the assertions below measure is what this build did and nothing it inherited.
        vm.deal(address(router), 0);
        vm.deal(address(hook), 0);

        gatewayUsdcToEth = PoolKey({
            currency0: ethCurrency,
            currency1: usdcCurrency,
            fee: PgasAddresses.GATEWAY_FEE,
            tickSpacing: PgasAddresses.GATEWAY_TICK_SPACING,
            hooks: IHooks(address(hook))
        });
        gatewayEthToUsdt = PoolKey({
            currency0: ethCurrency,
            currency1: usdtCurrency,
            fee: PgasAddresses.GATEWAY_FEE,
            tickSpacing: PgasAddresses.GATEWAY_TICK_SPACING,
            hooks: IHooks(address(hook))
        });

        vm.startPrank(owner);
        poolManager.initialize(gatewayUsdcToEth, PgasAddresses.SQRT_PRICE_1_1);
        poolManager.initialize(gatewayEthToUsdt, PgasAddresses.SQRT_PRICE_1_1);
        vm.stopPrank();
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // S3 — the load-bearing assumption
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice S3: the hook calls `poolManager.swap()` on a DIFFERENT pool from inside
    ///         `beforeSwap`, and every delta nets to zero. The pipe here is the offline double so
    ///         the ONLY thing under test is the v4 accounting.
    function test_S3_innerSwapFromBeforeSwap_deltasNetToZero() public {
        MockPipeNative pipe = new MockPipeNative();
        _registerEthRoute(address(pipe), 1e6, 100_000e6);

        deal(PgasAddresses.USDC, user, USDC_IN);
        vm.prank(user);
        IERC20(PgasAddresses.USDC).approve(address(router), USDC_IN);

        uint256 userEthBefore = user.balance;
        uint256 pmUsdcBefore = IERC20(PgasAddresses.USDC).balanceOf(address(poolManager));

        bytes memory hookData = abi.encode(bytes32("S3-ref"), MIN_OUT_ETH, RELAYER_FEE_QUOTE_ETH);

        vm.prank(user);
        // zeroForOne == false: currency1 (USDC) in, currency0 (native ETH) out.
        router.deposit(gatewayUsdcToEth, false, USDC_IN, hookData);

        // PoolManager reverts with CurrencyNotSettled if the unlock closes with any non-zero delta.
        // Reaching this line at all is the proof that all four movements netted to zero.
        MockPipeNative.Sent memory s = pipe.lastSent();
        console2.log("S3 fork block         :", block.number);
        console2.log("S3 usdc in            :", USDC_IN);
        console2.log("S3 pipe value    (wei):", s.value);
        console2.log("S3 pipe relayerFee(wei):", s.relayerFee);
        console2.log("S3 pipe msg.value (wei):", s.msgValue);

        assertEq(s.caller, address(hook), "the pipe's caller is the hook itself");
        assertEq(s.value + s.relayerFee, s.msgValue, "value + relayerFee == msg.value");
        assertEq(s.value % PgasAddresses.GRID_18DEC, 0, "value sits on the 1e10 grid");
        assertGe(s.relayerFee, RELAYER_FEE_QUOTE_ETH, "the unmintable tail rides on the quoted fee");
        assertEq(keccak256(s.receiver), keccak256(PgasAddresses.beamPubkeyETH()), "our pinned Beam pubkey");

        // The user received NOTHING: the whole output went to the bridge, in one transaction.
        assertEq(user.balance, userEthBefore, "user got no ETH");
        assertEq(IERC20(PgasAddresses.USDC).balanceOf(user), 0, "user spent all the USDC");

        // The hook holds a balance only INSIDE one transaction.
        assertEq(address(hook).balance, 0, "hook holds no ETH afterwards");
        assertEq(IERC20(PgasAddresses.USDC).balanceOf(address(hook)), 0, "hook holds no USDC afterwards");
        assertEq(address(router).balance, 0, "router holds nothing");

        // The input really reached the inner pool's PoolManager balance.
        assertEq(
            IERC20(PgasAddresses.USDC).balanceOf(address(poolManager)) - pmUsdcBefore,
            USDC_IN,
            "PoolManager received the whole input"
        );
    }

    /// @notice S3 corollary: a gateway pool refuses liquidity forever, which is what makes a
    ///         zero-liquidity pool safe rather than merely empty.
    function test_S3_gatewayPoolRefusesLiquidity() public {
        MockPipeNative pipe = new MockPipeNative();
        _registerEthRoute(address(pipe), 1e6, 100_000e6);
        assertTrue(hook.isRegistered(gatewayUsdcToEth.toId()), "route registered");
        // Adding liquidity reverts inside `beforeAddLiquidity`; see the unit suite for the direct
        // assertion against `PoolModifyLiquidityTest`.
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // S4 / T5 — the real Beam bridge pipes
    // ─────────────────────────────────────────────────────────────────────────────────────────

    /// @notice S4 / T5: 50 USDC → bETH through the REAL mainnet `EthPipe`, from a CONTRACT sender.
    function test_S4_usdc50_toBeth_realEthPipe() public {
        _registerEthRoute(PgasAddresses.ETH_PIPE, 1e6, 100_000e6);

        deal(PgasAddresses.USDC, user, USDC_IN);
        vm.prank(user);
        IERC20(PgasAddresses.USDC).approve(address(router), USDC_IN);

        uint256 pipeEthBefore = PgasAddresses.ETH_PIPE.balance;
        bytes memory hookData = abi.encode(bytes32("pgas-usdc-1"), MIN_OUT_ETH, RELAYER_FEE_QUOTE_ETH);

        vm.recordLogs();
        vm.prank(user);
        router.deposit(gatewayUsdcToEth, false, USDC_IN, hookData);
        uint256 gasUsed = vm.snapshotGasLastCall("PgasIngress", "usdc50_to_beth_realEthPipe");

        Vm.Log[] memory logs = vm.getRecordedLogs();
        (uint64 msgId, uint256 amount, uint256 relayerFee, bytes memory receiver, bytes32 ref) = _readPair(logs);

        console2.log("== S4 / T5: USDC 50 -> bETH through the real EthPipe ==");
        console2.log("fork block               :", block.number);
        console2.log("EthPipe                  :", PgasAddresses.ETH_PIPE);
        console2.log("NewLocalMessage.msgId    :", msgId);
        console2.log("NewLocalMessage.amount   :", amount);
        console2.log("NewLocalMessage.relayerFee:", relayerFee);
        console2.log("NewLocalMessage.receiver :");
        console2.logBytes(receiver);
        console2.log("swap output (value+fee)  :", amount + relayerFee);
        console2.log("gas, router.deposit      :", gasUsed);

        assertEq(keccak256(receiver), keccak256(PgasAddresses.beamPubkeyETH()), "pipe log names OUR pubkey");
        assertEq(receiver.length, 33, "33-byte compressed pubkey");
        assertEq(amount % PgasAddresses.GRID_18DEC, 0, "amount sits on the 1e10 grid");
        assertGt(amount, 0, "something is mintable on Beam");
        assertGe(relayerFee, RELAYER_FEE_QUOTE_ETH, "relayer fee >= the quote");
        // §IDENTITY-BEATS-BALANCE: the destination's balance rose by exactly what we sent.
        assertEq(PgasAddresses.ETH_PIPE.balance - pipeEthBefore, amount + relayerFee, "pipe balance == value + fee");

        // The hook's own log and the pipe's log agree, in one receipt, paired ORDINALLY — the k-th
        // `NewLocalMessage` from the pipe belongs to the k-th `PgasDeposit` from the hook, and the
        // pipe log comes first. NOT by adjacency: on an ERC-20 route clearing the allowance puts an
        // `Approval` between the two, so a reader written against `logIndex + 1` works on ETH and
        // silently fails on DAI. `_readPair` enforces the ordinal rule for the one-pair case this
        // test makes — exactly one of each, pipe before deposit, same value and same fee.
        assertEq(ref, bytes32("pgas-usdc-1"), "ref round-trips");

        assertEq(address(hook).balance, 0, "hook holds nothing afterwards");
    }

    /// @notice S4 / T5: native ETH 0.01 in. ETH is already the bETH source, so a native INPUT is
    ///         exercised against a USDT target: it proves the router's native settle path and the
    ///         hook's ERC-20 pipe branch (approve exact → sendFunds → approve 0) together.
    function test_S4_nativeEth_toUsdt_erc20PipeBranch() public {
        MockPipeERC20 pipe = new MockPipeERC20(PgasAddresses.USDT);
        vm.prank(owner);
        hook.registerRoute(
            gatewayEthToUsdt,
            innerUsdt,
            address(pipe),
            TEST_ONLY_USDT_PUBKEY,
            PgasAddresses.GRID_6DEC,
            1_000, // 0.001 USDT floor
            500,
            0.0005 ether,
            0.5 ether,
            true
        );

        vm.deal(user, ETH_IN * 3);
        uint256 balBefore = user.balance;
        bytes memory hookData = abi.encode(bytes32("pgas-eth-1"), MIN_OUT_USDT, uint256(1_000));

        vm.recordLogs();
        vm.prank(user);
        // Deliberately over-send: the surplus must come back.
        router.deposit{value: ETH_IN * 2}(gatewayEthToUsdt, true, ETH_IN, hookData);
        uint256 gasUsed = vm.snapshotGasLastCall("PgasIngress", "eth0_01_to_usdt_erc20Pipe");

        Vm.Log[] memory logs = vm.getRecordedLogs();
        (uint64 msgId, uint256 amount, uint256 relayerFee, bytes memory receiver,) = _readPair(logs);

        console2.log("== S4 / T5: native ETH 0.01 -> USDT pipe branch ==");
        console2.log("NewLocalMessage.msgId    :", msgId);
        console2.log("NewLocalMessage.amount   :", amount);
        console2.log("NewLocalMessage.relayerFee:", relayerFee);
        console2.log("gas, router.deposit      :", gasUsed);

        assertEq(keccak256(receiver), keccak256(TEST_ONLY_USDT_PUBKEY), "the route's OWN key, not the ETH pipe's");
        assertTrue(
            keccak256(receiver) != keccak256(PgasAddresses.beamPubkeyETH()),
            "one key per pipe: the ETH key must never appear on another pipe's route"
        );
        assertEq(balBefore - user.balance, ETH_IN, "only amountIn was spent; the surplus was refunded");
        assertEq(address(router).balance, 0, "router keeps nothing");
        assertEq(IERC20(PgasAddresses.USDT).balanceOf(address(hook)), 0, "hook holds no USDT afterwards");
        assertEq(IERC20(PgasAddresses.USDT).balanceOf(address(pipe)), amount + relayerFee, "the pipe pulled it all");
    }

    /// @notice The route's `maxDeposit` is a real bound on a real pool, not a comment.
    function test_maxDeposit_refusesOversize() public {
        MockPipeNative pipe = new MockPipeNative();
        _registerEthRoute(address(pipe), 1e6, 100e6); // cap: 100 USDC

        deal(PgasAddresses.USDC, user, 1_000e6);
        vm.prank(user);
        IERC20(PgasAddresses.USDC).approve(address(router), 1_000e6);

        bytes memory hookData = abi.encode(bytes32("too-big"), MIN_OUT_ETH, RELAYER_FEE_QUOTE_ETH);
        vm.prank(user);
        vm.expectRevert();
        router.deposit(gatewayUsdcToEth, false, 1_000e6, hookData);
    }

    /// @notice `minOut` is the only usable slippage bound on this path — prove it bites.
    function test_minOut_enforcedAgainstRealDepth() public {
        MockPipeNative pipe = new MockPipeNative();
        _registerEthRoute(address(pipe), 1e6, 100_000e6);

        deal(PgasAddresses.USDC, user, USDC_IN);
        vm.prank(user);
        IERC20(PgasAddresses.USDC).approve(address(router), USDC_IN);

        // 50 USDC will never buy 1 ETH.
        bytes memory hookData = abi.encode(bytes32("minout"), uint256(1 ether), RELAYER_FEE_QUOTE_ETH);
        vm.prank(user);
        vm.expectRevert();
        router.deposit(gatewayUsdcToEth, false, USDC_IN, hookData);
    }

    // ─────────────────────────────────────────────────────────────────────────────────────────
    // Helpers
    // ─────────────────────────────────────────────────────────────────────────────────────────

    function _registerEthRoute(address pipe, uint256 minDeposit, uint256 maxDeposit) internal {
        assertEq(MIN_RELAYER_FEE_ETH, PgasAddresses.MIN_RELAYER_FEE_ETH_WEI, "the route's floor IS the API's tariff");
        vm.prank(owner);
        hook.registerRoute(
            gatewayUsdcToEth,
            innerUsdc,
            pipe,
            PgasAddresses.beamPubkeyETH(),
            PgasAddresses.GRID_18DEC,
            MIN_RELAYER_FEE_ETH,
            500, // maxRelayerFeeBps == 5%
            minDeposit,
            maxDeposit,
            false // zeroForOne == false → the output is currency0, native ETH
        );
    }

    /// @dev THE PAIRING RULE. One transaction can carry N deposits — any caller may take the
    ///      PoolManager lock itself and swap N times inside one unlock, with a `ref` of its own
    ///      choosing — so "the first NewLocalMessage in the receipt" is not a reader, it is a
    ///      guess. What IS guaranteed: each deposit emits exactly one pipe log and then exactly one
    ///      `PgasDeposit`, and the hook's re-entrancy guard forbids a deposit inside a deposit, so
    ///      the k-th `NewLocalMessage` and the k-th `PgasDeposit` belong to each other and the pipe
    ///      log always comes first. It is ORDINAL, not adjacent: on an ERC-20 route the allowance
    ///      being cleared puts an `Approval` log between the two. These tests make exactly one
    ///      deposit, so exactly one pair must exist. `test/PgasDepositPairing.t.sol` holds the
    ///      general invariant.
    function _readPair(Vm.Log[] memory logs)
        internal
        view
        returns (uint64 msgId, uint256 amount, uint256 relayerFee, bytes memory receiver, bytes32 ref)
    {
        uint256 pipeLogs;
        uint256 depositLogs;
        uint256 pipeAt;
        uint256 depositAt;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics.length == 1 && logs[i].topics[0] == PgasAddresses.NEW_LOCAL_MESSAGE_TOPIC0) {
                pipeLogs++;
                pipeAt = i;
                (msgId, amount, relayerFee, receiver) = abi.decode(logs[i].data, (uint64, uint256, uint256, bytes));
            }
            if (
                logs[i].emitter == address(hook) && logs[i].topics.length == 4
                    && logs[i].topics[0] == PgasAddresses.PGAS_DEPOSIT_TOPIC0
            ) {
                depositLogs++;
                depositAt = i;
                ref = logs[i].topics[1];
                (,, uint256 value, uint256 fee,) = abi.decode(logs[i].data, (uint256, address, uint256, uint256, bytes));
                require(value == amount && fee == relayerFee, "the paired logs disagree about the money");
            }
        }
        require(pipeLogs == 1, "expected exactly one NewLocalMessage in this receipt");
        require(depositLogs == 1, "expected exactly one PgasDeposit in this receipt");
        require(pipeAt < depositAt, "the pipe log must precede its own deposit log");
    }

    /// @notice The pinned pipe bytecode hashes ARE the code that was living at the four pipe
    ///         addresses AT THE PINNED BLOCK, and all four pipes passed the hook's registration
    ///         probe there. This one runs at `FORK_BLOCK`, so once it is true it stays true: it is
    ///         a fixture, not a watch. The watch is
    ///         `test_pinnedPipeCodehashesStillMatchAtTheChainTip` below, which re-reads the same
    ///         four hashes at the chain TIP and is the test that fails if the bridge ever
    ///         redeploys a pipe. Both are kept: this one is reproducible, that one is current.
    function test_pinnedPipeCodehashesMatchTheLivePipes() public view {
        address[4] memory pipes =
            [PgasAddresses.ETH_PIPE, PgasAddresses.USDT_PIPE, PgasAddresses.WBTC_PIPE, PgasAddresses.DAI_PIPE];
        for (uint256 i = 0; i < pipes.length; i++) {
            assertGt(pipes[i].code.length, 0, "a pinned pipe address with no code is not a pipe");
            assertEq(
                pipes[i].codehash,
                PgasAddresses.pipeCodehashFor(pipes[i]),
                "the pinned EXTCODEHASH must be the code that is actually there"
            );
            // The hook's own on-chain guard, asked against the real bridge: every real pipe
            // refuses a selector it does not implement. If this ever failed, `registerRoute` would
            // refuse the real bridge — the prober must call the way the caller calls.
            assertTrue(hook.pipeRejectsUnknownCalls(pipes[i]), "a real pipe refuses an unknown selector");
            console2.log("pipe                     :", pipes[i]);
            console2.log("live codehash            :");
            console2.logBytes32(pipes[i].codehash);
        }
        // Three ERC-20 pipes, one build: identical bytecode. The ETH pipe is a different contract.
        assertEq(PgasAddresses.USDT_PIPE.codehash, PgasAddresses.WBTC_PIPE.codehash, "USDT/WBTC pipes: one build");
        assertEq(PgasAddresses.USDT_PIPE.codehash, PgasAddresses.DAI_PIPE.codehash, "USDT/DAI pipes: one build");
        assertTrue(PgasAddresses.ETH_PIPE.codehash != PgasAddresses.USDT_PIPE.codehash, "the ETH pipe differs");
    }

    /// @notice The same four `EXTCODEHASH`es, read at the CHAIN TIP rather than at the pinned
    ///         block — and the hook's registration probe asked of the live pipes there too.
    ///
    /// @dev This is the only assertion in the suite that can start failing without anyone
    ///      changing this repository, and that is the whole point of it. `FORK_BLOCK` is frozen,
    ///      so the test above can never notice a redeploy: a pinned number nobody re-reads is a
    ///      number that was true once. `Register.s.sol` refuses a pipe whose live code does not
    ///      hash to the pinned value, so if the bridge redeploys a pipe, arming stops dead with a
    ///      hash mismatch and no clue where the truth is. This test is that clue, before the day
    ///      money moves. A failure here is NOT a licence to edit the constant: it means the pinned
    ///      address now holds a different contract, and which contract it is has to be established
    ///      off-chain before any route names it.
    ///
    /// @dev The hook is made persistent across the fork switch on purpose — the probe must be
    ///      asked by the same bytecode the rest of the suite asks with (the prober must call the
    ///      way the caller calls), not by a second deployment mined at the tip.
    function test_pinnedPipeCodehashesStillMatchAtTheChainTip() public {
        string memory rpc = vm.envOr("FORK_RPC_URL", string("https://eth.drpc.org"));
        vm.makePersistent(address(hook));
        vm.createSelectFork(rpc); // no block number == latest
        console2.log("chain tip block          :", block.number);
        assertGt(block.number, FORK_BLOCK, "the tip must be ahead of the pinned block");
        assertGt(address(hook).code.length, 0, "the hook survived the fork switch");

        address[4] memory pipes =
            [PgasAddresses.ETH_PIPE, PgasAddresses.USDT_PIPE, PgasAddresses.WBTC_PIPE, PgasAddresses.DAI_PIPE];
        for (uint256 i = 0; i < pipes.length; i++) {
            assertGt(pipes[i].code.length, 0, "a pinned pipe address with no code is not a pipe");
            assertEq(
                pipes[i].codehash,
                PgasAddresses.pipeCodehashFor(pipes[i]),
                "the pinned EXTCODEHASH is no longer the code at this address"
            );
            assertTrue(hook.pipeRejectsUnknownCalls(pipes[i]), "a real pipe refuses an unknown selector, today");
            console2.log("pipe (tip)               :", pipes[i]);
        }
    }

    /// @notice The grid the hook derives on chain from the real tokens equals the grid the pinned
    ///         table says. Two implementations of one fact will disagree and one of them reaches
    ///         money: this is the only place both can be asked at once.
    function test_derivedGridAgreesWithThePinnedTable() public view {
        address[5] memory assets =
            [PgasAddresses.NATIVE, PgasAddresses.USDC, PgasAddresses.USDT, PgasAddresses.DAI, PgasAddresses.WBTC];
        for (uint256 i = 0; i < assets.length; i++) {
            assertEq(
                hook.requiredGrid(Currency.wrap(assets[i])),
                PgasAddresses.gridFor(assets[i]),
                "the hook and the table must agree, asset by asset"
            );
        }
    }
}
