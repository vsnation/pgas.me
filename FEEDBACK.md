# Uniswap developer feedback — from the Pgas.me team

Written 2026-09, after building a Uniswap V4 hook end to end and then deciding not to deploy it.
That decision is the most useful thing we can tell you, so this is the story of how we got there,
with the numbers we measured. Everything below is in the repository this file sits in.

## What we were trying to build

Pgas.me funds fresh EVM wallets with gas that has no on-chain link to whatever paid for it. The
value crosses to Beam through a bridge "pipe" contract, sits on Beam's confidential ledger there, and
comes back out into the wallets the user lists. What we wanted from Uniswap was one transaction: the user swaps a token
they already hold, and the output goes straight into the bridge pipe without ever resting in their
own wallet — one signature, nothing in an intermediate address, and if the bridge call reverts the
swap reverts with it.

That is a post-swap side effect, and in v4 the only place to put one is a hook. So we designed a
zero-liquidity gateway pool whose `beforeSwap` takes 100% of the input, routes it through the
canonical deep hook-less pool to get the real price, takes the whole output, floors it onto the
bridge's 8-decimal grid and calls `sendFunds(value, relayerFee, ourBeamPubkey)` in the same
transaction. `hookData` carries a deposit reference, a minimum output and the relayer's fee quote;
a thin router does `unlock → swap → settle` and exposes `msgSender()` so the hook can attribute the
deposit to the real payer. Server-side, the V4 Quoter prices the inner pool and the hook's own event
is how we match a bridge lock back to a user.

It works. It is built, reviewed twice, and proven against real mainnet state in a fork suite. We
are not deploying it, and the reason is the comparison below.

## The same idea, through a cross-chain solver, took one day

Before the v4 work we had already shipped "swap, then call our contract" across chains using
deBridge's DLN. That integration is one HTTP GET. We ask their order API to build an order and
attach a `dlnHook` that is three fields: the target address (our bridge pipe), the calldata
(`EthPipe.sendFunds(value, relayerFee, pubkey)`) and a gas limit; the solver that fills the order
executes it as part of the fill. If the hook would revert, the API says so when we ask for the
transaction instead of handing us one that fails; if it reverts on chain anyway, the fill lands at a
fallback recipient, which we set to the user's own wallet, so a failed hook costs the user nothing.

We deployed no contract, mined no address, created no pool, and never had to answer a liquidity
question. There was no owner key to hold, no kill switch to wire, no testnet rehearsal. Quote,
order-with-hook and the attribution that credits the user when the bridge lock appears were one
work item, and it went from nothing to a live deposit inside a single working day.

## What the v4 hook cost, step by step

About three engineer-days of equivalent effort, over two days of wall-clock because we ran the
reviews in parallel — almost none of it spent on our own logic.

**Learning the permission scheme.** Hook permissions live in the low 14 bits of the hook's address,
so the shape of the contract decides where it can live. Ours needs `beforeInitialize`,
`beforeAddLiquidity`, `beforeSwap` and `beforeSwapReturnDelta` — mask `0x2888` — which means CREATE2
salt mining before we can deploy anything, and re-mining whenever a byte of the contract changes.
That is not a one-off: our review rounds changed the bytecode twice and voided the salt both times.

**Working out who `sender` is.** A hook does not see the user; it sees whoever called
`PoolManager.swap`, which is a router. We needed the payer for attribution, so we implemented
`IMsgSender` on our own router and allow-list the routers whose `msgSender()` we believe, treating
an unknown caller as the payer itself — that field is attribution, never authorisation. None of it
is discoverable from the interface. Picking a router was no easier: four Universal Routers are live
on mainnet with four different builds, two Uniswap-published sources disagree about which is "the"
one, and `ExactInputSingleParams` gained a field between versions, so we gave up choosing and
shipped our own 184-line router — a silly thing to have to write.

**The `amountOutMinimum = 0` trap.** A hook that takes 100% of the output makes the router-level
slippage bound unusable: through any Universal Router the caller *must* pass `amountOutMinimum = 0`,
or `V4Router` reverts with `V4TooLittleReceived`. So the only slippage bound that works here is one
the hook enforces itself out of caller-supplied `hookData` — and the natural thing for an integrator
to write is `0` and move on. We refuse `minOut == 0` and bound the delivered amount, not the gross
output. For calibration: on the real ETH/USDC 0.30% pool at block 25,942,000 a 12M USDC front-run
takes 1,631 basis points off a 50-USDC deposit, so the obvious version of this loses money. Same
family: for a `beforeSwapReturnDelta` hook exact-output puts the return delta on the wrong currency,
so we refuse exact-output.

**One thing we could not verify from documentation at all.** The design rests on a hook being
allowed to call `poolManager.swap()` on a *different* pool from inside `beforeSwap`. We read
`PoolManager` and concluded it is permitted, but found it stated nowhere, and it was too
load-bearing to assume — so we wrote a mainnet-fork test whose only job is to prove it and check
that all four deltas net to zero. It passes. It should not have been our job to find out.

**Toolchain friction.** `v4-periphery`'s `main` deleted `BaseHook.sol` and `HookMiner.sol` earlier
in 2026, so the instructions everyone follows — `forge install` the repo — produce a tree without
the two files a hook developer most needs; we pinned `@uniswap/v4-core@1.0.2` and
`@uniswap/v4-periphery@1.0.3` from npm, which still ship both. `via_ir` is required and the optimizer
run count has to match upstream's `44444444`, or compiling core's `Pool` for our unit suite fails
with a Yul "stack too deep". Not yours, but worth knowing: Foundry's binaries link `glibc` 2.32 or
newer, so our Ubuntu 20.04 server builds them in a container.

**Reviewing it.** Because this contract touches user money in one irreversible step, we put it
through two adversarial review rounds. First: three independent reviewers, 26 findings (7 medium,
the rest low), consolidated into 10 fixes, each proven with a test that fails against the unfixed
code; a re-verification pass left 2 low and 1 info. The second round closed those and four more
residuals, and its own skeptic pass found one more medium — a plain `fallback() payable` sink that
swallows a whole deposit and satisfies every check the hook can make from where it stands. The fixes
were all of one character: derive the bridge grid from the output asset's own `decimals()` instead
of trusting a registration parameter; apply the fee ceiling to the fee actually paid, not the quote;
bound `minOut` against what the user receives, not the gross output; assert that exactly the output
left the contract; make a candidate pipe prove it refuses calls it does not implement, with its code
hash pinned in the arming script; pair our event with the bridge's by ordinal, not adjacency; refund
a call's own surplus rather than the contract's balance; and turn arithmetic panics into named
refusals. The suite ended at 76 offline tests and 9 mainnet-fork tests.

**Gas.** A deposit costs about 190,000 gas for 50 USDC into the ETH pipe and about 245,000 for the
ERC-20 pipe branch, both measured in the fork suite and checked into `contracts/snapshots/`.
Deploying is the expensive part: roughly 3.07M gas for the hook and 0.93M for the router estimated
against mainnet state, about 4.4M for deploy plus arming with the allow-list, the gateway pool and
the route registration. Those deploy figures predate the last review round, so they are the right
order of magnitude rather than exact.

**And what was still ahead.** Mine a salt for the real owner; deploy hook and router; allow-list the
router; initialise a gateway pool; register the route, write-once, against a pinned pipe code hash;
rehearse on Sepolia, which has no Beam bridge, so we would deploy a mock pipe, a test token and our
own funded inner pool and rehearse our scripts but not our economics; a small real-money validation
on mainnet; then hold an owner key that can pause and rescue forever, because the permissions are in
the address and the contract cannot be upgraded.

## Why we shipped two clicks instead

We stepped back and shipped the Uniswap route as two transactions: swap the user's token through the
canonical v4 pool with the Universal Router, quoted with the V4 Quoter, into the user's own wallet —
then deposit the ETH into the bridge pipe, a path we already had and already trust. One extra click,
and no deployment, no address mining, no pool, no owner key, no rehearsal.

The trade is not that the hook was bad. It is that everything the hook needs *around* it —
deployment, custody of an owner key, an unupgradeable address, a rehearsal a testnet cannot really
provide, two review rounds before we would let it near a user — is a permanent operational
commitment, and the honest comparison was sitting right next to it: the cross-chain integration
gives us the same atomicity with none of that, because somebody else runs the executor.

## What was genuinely good

The singleton `PoolManager` with `unlock`/`settle` is a pleasure to write against — all four of our
movements land on one account and net to zero, and the accounting is easy to reason about once you
accept flash accounting. The V4 Quoter did what we needed with a plain `eth_call` and no local curve
math, which matters to us: we would rather read a price than compute one.

Fork-test ergonomics against a real `PoolManager`, real deep pools and the real bridge contract were
the best part of the exercise: we proved the load-bearing assumption, measured real price impact and
real gas, and re-read our pinned code hashes at the chain tip, spending nothing. A much better gate
than a testnet.

The permission-bits design is good once understood — the address *is* the interface, and a wrong
address is un-deployable rather than subtly broken — and `hookData` is a clean channel. The
zero-liquidity gateway pattern is not even ours: we recognised it in your own `BaseTokenWrapperHook`
family, same permission bits, live pool at zero liquidity. Finding a first-party contract shaped
like what we were about to build was the moment the design stopped being speculative.

## What we would ask for

**A Uniswap-operated solver or executor that runs post-swap calldata.** Let an integrator attach to
a swap request a target address, calldata, a statement of where the output goes, and a fallback
recipient — and have the filler execute it after the swap, atomically, with the output never resting
in an intermediate address. Whether it arrives as a Universal Router command, a field on the Trading
API or a UniswapX fill step matters far less than that it exists. This is really the whole ask: it
would have removed the hook, the salt mining, the gateway pool, the route registration, the owner
key, the Sepolia rehearsal and both review rounds, leaving us to write the one thing that is
genuinely ours — the calldata. It is what the cross-chain route already gives us in a single HTTP
call, and why that route is our default today.

**An audited, Uniswap-deployed generic forwarder hook.** If the solver is far off, a first-party
hook that forwards a swap's output to a target with per-swap `hookData` would cover much of what
people write custom hooks for: we would have used an existing pool, with code somebody else audited,
and written only our own attribution. We could not do that ourselves because a hook is part of the
`PoolKey`, so it can never be attached to an existing deep pool — which pushes every integrator who
wants a post-swap effect into deploying their own contract and their own pool.

**Keep `BaseHook` and `HookMiner` in released periphery packages.** Their removal from `main` is a
trap for exactly the developer following the standard instructions for the first time.

**Document the router/sender semantics and the `amountOutMinimum = 0` interaction prominently.**
Both cost us design time, and the second is a money bug waiting for someone who does not find it. A
short page called "what your hook sees, and what it must enforce itself" would have saved us more
than any other document you could write.

**Ship testnet fixtures for hook developers** — a mock pool with depth, a quoter, and a deployed swap
helper — so a rehearsal does not start with deploying and funding your own pool.
N
## Where the code is

The shipped two-step route is `api/pgasme/uniswap.py` (route registry and V4 Quoter read), the
`uniswap` branch of `api/pgasme/routers/quote.py`, and the deposit card in
`web/src/pages/Deposit.tsx`. The hook route — built, reviewed, not deployed — is
`contracts/src/PgasIngressHook.sol` (permissions, `beforeSwap`, `_innerSwap`, `_sendToPipe`,
`registerRoute` and the two public probes), `contracts/src/libraries/PipeSplit.sol` for the grid
arithmetic and `contracts/src/PgasRouter.sol` for the `unlock`/`settle` caller and `msgSender()`.
Pinned addresses and code hashes are in `contracts/script/PgasAddresses.sol`, the fork proof is
`contracts/test/fork/PgasMainnetFork.t.sol`, and the golden vectors both our Solidity and Python
grid-split implementations run are in `contracts/test/vectors/`. The README has the line numbers.

## Where the integration stands

The contracts are built, tested and reviewed: 76 offline tests and 9 mainnet-fork tests, green. They
are **not deployed** — no mainnet address, no testnet address — and stay in the repository as a
reviewed reference rather than live infrastructure. What ships to users is the two-step Uniswap
route, landing in the next release; the cross-chain route remains the default. If a Uniswap-operated executor for
post-swap calldata ever ships, we will delete most of `contracts/` and be glad to.
