# Pgas.me — Uniswap V4 ingress contracts

One transaction. A user swaps a token on a Pgas **gateway pool**, and the pool's hook routes that
input through the canonical deep Uniswap v4 pool and pushes the whole output straight into the Beam
bridge pipe. No intermediate balance in the user's wallet, no second signature, no second approval.

```
wallet ──swap──▶ gateway pool (fee 0, zero liquidity, hook 0x…2888)
                      │ beforeSwap
                      ├── poolManager.swap(canonical ETH/USDC 0.30% pool)   ← the real price
                      ├── take(output)
                      ├── split on the bridge's 8-decimal grid
                      └── pipe.sendFunds{value}(value, relayerFee, beamPubkey) ──▶ Beam
```

## Contracts

| contract | what it does | mutable state |
|---|---|---|
| `src/PgasIngressHook.sol` | `beforeSwap` takes 100% of the input, swaps it on the inner pool, takes the output, splits it on the pipe's grid, calls `sendFunds` — and checks that exactly that much left this contract — then emits `PgasDeposit` | `paused`, and monotone *tightening* of a route's bounds — nothing else |
| `src/PgasRouter.sol` | `unlock → swap → settle the input`; exposes `msgSender()` so the hook can attribute the deposit to the real payer. Plain ERC-20 approvals, no Permit2. Refunds **this call's** surplus (`msg.value - amountIn`), never its balance | none |
| `src/libraries/PipeSplit.sol` | `value` floored onto the bridge grid, the unmintable tail absorbed into the relayer fee | — |
| `test/mocks/MockPipe.sol` | offline doubles for the native and ERC-20 pipes — same ABI, same event, same `msg.value == value + relayerFee` rule — plus the dishonest ones: a pipe that draws only `value`, a pipe that refunds the fee, a bare payable **sink** that swallows the lot and the storage-writing sink that swallows it while passing the probe, a token with no `decimals()` | test only |

### Permissions live in the address

`beforeInitialize (1<<13)` | `beforeAddLiquidity (1<<11)` | `beforeSwap (1<<7)` |
`beforeSwapReturnDelta (1<<3)` → the hook address's **low 14 bits must equal `0x2888`**. The salt is
mined with `HookMiner` and deployed through the canonical CREATE2 proxy
`0x4e59b44847b379578588920cA78FbF26c0B4956C`; `Hooks.validateHookPermissions` in the constructor
makes a wrong address un-deployable, and `PoolManager.initialize` checks it again.

Because the permissions are encoded in the address, **the hook is not upgradeable**. A fixed hook is
a new address, a new pool and a new route.

### What the owner can and cannot do

* **Can**: add a route, `pause` (the on-chain kill switch, checked immediately before the
  irreversible `sendFunds` so it halts a chain already in flight), *tighten* a live route (raise
  `minDeposit`, lower `maxDeposit`, lower `maxRelayerFeeBps`, **raise `minRelayerFee`**), `rescue`
  stranded dust, allow-list a router, transfer ownership.
* **Cannot**: redirect a route. `registerRoute` is **write-once per gateway pool id**, and the
  destination Beam public key is fixed there — it is deliberately *not* read from `hookData`,
  because hookData is caller-controlled and a caller-chosen destination would turn the pool into a
  public bridge front-end. The `pipe` and the `grid` are equally frozen.

`minRelayerFee` is a tightening like the others, and it took a review to see it: the split consumes
the **caller's `relayerFeeQuote`**, never the floor, so raising the floor can only refuse more
quotes — it can never make a user pay more. A route registered under an out-of-date tariff would
otherwise be stuck with it for the life of the pool.

### The pipe has to prove it is a pipe

A route's `pipe` is written once and then receives 100% of that route's output forever, and the
hook's own delivery post-condition only proves the output **left** this contract. An address that
takes ETH and does nothing satisfies it perfectly: a bare `fallback() payable` sink, or an EOA. The
deposit "succeeds", `PgasDeposit` is emitted with a value and a fee, and nothing crosses the bridge —
with no route to change and nothing to rescue. Two levels now stand between that and a registration:

* **On chain, at `registerRoute`.** The pipe must have code, and must **refuse a selector it does not
  implement** — `pipeRejectsUnknownCalls()` is public, so the answer can be read before arming.
  That is everything the hook can prove from where it stands, and it is stated as exactly that: it
  catches the accident (a wrong address, an EOA, a **stateless payable fallback**). It does not
  prove the pipe bridges, and it does not classify sinks: a contract whose fallback **writes
  storage** reverts under `STATICCALL`, which the probe reads as a refusal, so that sink passes.
  `test_theProbeRefusesAStatelessPayableFallbackAndNotEveryOtherShape` asserts that pass out loud
  as a KNOWN-PASS, so nobody weakens the check below on the belief that this one covers it.
* **On the laptop, before the broadcast.** The four pipes' `EXTCODEHASH` is pinned in
  `script/PgasAddresses.sol` — one hash for the ETH pipe, one shared by the three ERC-20 pipes,
  which are the same build deployed three times — and `Register.s.sol` compares the code actually
  living at the address against it. A pipe nobody has pinned is refused by name, not guessed at.
  Two fork tests read those hashes off the live bridge: `test_pinnedPipeCodehashesMatchTheLivePipes`
  reads them at the **pinned block**, which makes it reproducible but means it can never notice a
  change, and `test_pinnedPipeCodehashesStillMatchAtTheChainTip` forks again at the **latest**
  block and re-reads all four there — that is the one that fails if the bridge redeploys a pipe,
  so a pinned number nobody re-reads cannot quietly become a number that was true once.

### Gateway pools hold no liquidity, ever

A gateway pool is `fee = 0`, `tickSpacing = 1`, `sqrtPriceX96 = 2**96`, opened only by the owner.
`beforeAddLiquidity` reverts unconditionally, so nobody can put capital at risk in one. The pool
never trades: the hook's `+amountIn` return delta makes the swap amount exactly zero, and
`Pool.swap` early-returns on a zero amount *before* the price-limit check. The price the user gets
comes from the canonical pool that Uniswap's own LPs fund.

## The grid, and why it matters

Beam is always 8 decimals. An 18-decimal asset crossing the pipe has its last 10 digits dropped, and
**a non-zero sub-grid tail is unmintable and stuck forever**. So:

```
value      = floor((out - relayerFeeQuote) / grid) * grid     # grid = 10**(decimals-8)
relayerFee = out - value                                      # the tail rides on the fee
```

`grid` is `1e10` for ETH and DAI, and `1` for WBTC and USDT (no floor at all). The relayer fee is a
**price we are quoted**, not a residue this code invents; the hook only bounds it, between the
route's `minRelayerFee` floor and its `maxRelayerFeeBps` ceiling.

**The grid is derived, never trusted.** `registerRoute` reads the output asset's `decimals()` and
demands `grid == 10**(decimals-8)` (native ETH is 18 by definition; an asset that will not answer
is refused, because an unreadable query is not evidence of anything). This is not defensive
decoration: the real `EthPipe` contains exactly two `require`s — a 33-byte pubkey and
`value + relayerFee == msg.value` — and **no grid arithmetic at all**, so it will happily accept an
off-grid `value` and strand the tail on Beam forever. `PipeSplit`'s floor is the only thing between
a deposit and an unmintable tail, a route is write-once, and a mis-typed `GRID` would therefore ruin
every future deposit on that pair with no fix short of a new pool. `PgasIngressHook.requiredGrid()`
is public, so the number can be read *before* arming rather than discovered afterwards.

**Both fee bounds are checked on the numbers that move.** `maxRelayerFeeBps` is applied to the
caller's quote *and* to `relayerFee = quote + ((out - quote) mod grid)` — the sum the relayer is
actually paid — and `minOut` bounds `value`, what the user actually receives on Beam, not the gross
swap output the fee and the tail come out of. A guard on a number nobody is paid is not a guard: on
a mis-gridded route the tail alone reached 3,113 bps under a 500 bps ceiling, with every per-leg
check passing and right to.

The same algorithm exists in the API, in Python. Two implementations of one fact will disagree and
one of them reaches money — so neither owns the truth. Both are pinned to the shared golden-vector
file **`test/vectors/grid.json`** (16 split vectors + 4 fee-bound vectors + **4 realized-fee-bound
vectors** + the **relayer tariff** per asset, including the tails, the no-grid WBTC case, the fee
floor and both bps ceilings). `test/PipeSplit.t.sol` runs it on this side; the API suite runs the
identical file on the other. **A change to the algorithm changes that file first, and both suites
second.**

> **For the API side:** read `contracts/test/vectors/grid.json` (do not copy it — read the one
> file). Every numeric field is a decimal **string**, so nothing is lost to float parsing. Each
> entry in `vectors` is `{amount, min_relayer_fee, grid}` → either `{value, relayer_fee}` or a
> non-empty `revert` naming which refusal is expected; the `fee_bounds` entries are the hook's
> pre-split floor/ceiling checks on the **quote** (`out`, `quote`, `min_relayer_fee`,
> `max_relayer_fee_bps`, `accept`, `revert`). The Python parity test asserts `split_amount`
> reproduces every row, **including which side reverts** — a divergence in the refusals is a
> divergence.
>
> **Three blocks are new (2026-09-10) and the existing ones are untouched:**
> `realized_fee_bounds` pins the check the hook now makes *after* the split — `relayer_fee * 10000
> <= out * max_relayer_fee_bps` — which a quote sitting on the ceiling can fail on the grid tail
> alone; every row's quote passes the old check, which is why the overrun was invisible. `tariff`
> is the relayer's e2b price per asset, the single source both the API's
> `ethpipe.min_relayer_fee_units` and the route's registered floor come from. And
> **`contracts/test/vectors/events.json`** pins the two logs a deposit leaves — topic0, indexing and
> field order for `PgasDeposit` and `NewLocalMessage`, plus the pairing rule and what `ref` is
> worth. ⚠️ The `PgasDeposit` signature drafted in the build design document is **not** the deployed
> one; it is a different topic0. The deployed one is
> `PgasDeposit(bytes32,address,address,uint256,address,uint256,uint256,bytes)` =
> `0xd0e41515…ae2b`.

## The two logs a deposit leaves, and how to pair them

A deposit is credited off two logs in one receipt: the target pipe's `NewLocalMessage` and the
hook's `PgasDeposit`. Both are pinned in **`test/vectors/events.json`** — read that file, do not
re-derive the ABI from prose. Three things a reader has to know:

* **One transaction can carry N deposits.** The hook's re-entrancy guard blocks *nesting*, not a
  sequence: any contract can take the PoolManager lock itself and swap N times on a gateway pool
  inside one unlock — no allow-listing needed, and `payer` is then that contract. The suite proves
  it with a 60-line stranger.
* **The pairing rule is ordinal, not adjacency.** Within one receipt the *k*-th `NewLocalMessage`
  from the target pipe belongs to the *k*-th `PgasDeposit` from the hook, and the pipe log always
  comes first. That holds because each deposit emits exactly one of each, in that order, and no
  deposit can begin inside another. It is **not** `logIndex + 1`: on a native route the deposit log
  is indeed the next log, but on an ERC-20 route clearing the allowance puts an `Approval` between
  them, so a reader written against adjacency works on ETH and silently fails on DAI. Never pair on
  "the first `NewLocalMessage` in the receipt" — with two deposits in one transaction that credits
  one quote with the other's amount.
* **`ref` is unauthenticated, forgeable and repeatable.** It is copied out of caller-controlled
  `hookData`; nothing on chain checks it, marks it used, or refuses a repeat. Match a deposit on
  **(ref, `tx.from`)** — the sender is what makes a public receipt the user's — and mark a ref spent
  only *after* a credit succeeds, never on sighting, or a $1 deposit carrying a victim's ref burns
  their quote. The credited amount is the paired `NewLocalMessage`'s `amount`.

### What changed on 2026-09-10, for whoever builds the calldata

The ABI of `PgasRouter.deposit` and of `PgasDeposit` is **unchanged**. Four things around them are:

1. **`hookData`'s `minOut` may no longer be `0`.** Same encoding (`abi.encode(bytes32 ref, uint256
   minOut, uint256 relayerFeeQuote)`), same field — but `0` now reverts with `NoSlippageBound`, and
   `minOut` is compared against `value` (what lands on Beam) rather than the gross output. The API
   already computes `min_out_units` from the V4Quoter, so nothing changes for it except that a
   regression to `0` becomes a loud refusal instead of a silent sandwich.
2. **`relayerFeeQuote` is bounded twice.** The quote must clear the route's floor and sit under
   `maxRelayerFeeBps`, *and* `quote + ((out - quote) mod grid)` must also sit under it
   (`RealizedFeeAboveCeiling`). With a correct grid the tail is under one grid step, so this only
   bites for a quote sitting on the ceiling — the `realized_fee_bounds` rows in `grid.json` are the
   shared statement of it.
3. **`tightenRoute` takes a fifth argument**, `minRelayerFee` (raise-only), and `RouteTightened`
   carries it. Owner surface only; nothing on the deposit path calls either.
4. **The native refund is exactly `msg.value - amountIn`**, where it used to be the router's whole
   balance. An EOA caller that over-sends is unaffected. A **contract** caller is: it is no longer
   handed a stranger's dust, and it no longer needs a `receive()` to deposit the exact `amountIn`.

## Build and test

Requires [Foundry](https://getfoundry.sh) and Node (for the pinned npm sources).

```bash
npm install          # @uniswap/v4-core@1.0.2 + @uniswap/v4-periphery@1.0.3, pinned exactly
forge build
forge test -vv       # the offline suite: 76 tests, no network, no RPC, no fork
```

`test/PgasRegressions.t.sol` carries one test per defect found in review, named for the rule it
holds rather than the bug it came from. Every one of them was checked against the code *without* its
fix and fails there — a fix with no failing test behind it is a claim, not a fix.

The pins are not cosmetic. v4-periphery's `main` branch **deleted** `src/utils/BaseHook.sol` and
`src/utils/HookMiner.sol` in Feb 2026; the `1.0.3` npm tarball still ships both, which is why this
project resolves Uniswap through `node_modules` and never through `forge install <repo> main`.
`via_ir = true` is required, and `optimizer_runs` matches upstream v4-core's `44444444` — at lower
run counts, compiling v4-core's `Pool` library for the unit suite fails with a Yul "stack too deep".

Read the **exit code**, not the last line:

```bash
forge test -vv | tail; EXIT=${PIPESTATUS[0]}; echo "exit=$EXIT"
```

### The mainnet-fork suite

The fork suite is where the design is actually proven: real `PoolManager`, real deep pools, real
bridge pipes. It is excluded from a bare `forge test` by construction and needs an archive RPC.

```bash
export FORK_RPC_URL=https://eth.drpc.org     # must serve archive state at the pinned block
FOUNDRY_PROFILE=fork forge test -vv
```

It is 9 tests, 8 of them at block **25,942,000** and one that re-forks at the chain tip
(`test_pinnedPipeCodehashesStillMatchAtTheChainTip` — the only assertion here that can start failing
without anyone changing this repository, which is the point of it). It asserts: that a hook may call `poolManager.swap()` on a
*different* pool from inside `beforeSwap` and that all four deltas net to zero; that the real pipe
accepts `sendFunds` from a **contract** sender; that the emitted `NewLocalMessage` names our public
key with a grid-aligned `value`; that the pipe's balance rose by exactly `value + relayerFee`
(§IDENTITY-BEATS-BALANCE, measured against the real pipe rather than assumed in the hook); that the
grid the hook derives from each real token equals the grid the pinned table states, asset by asset;
that the pinned pipe `EXTCODEHASH`es **are** the code living at the four pipe addresses — at the
pinned block *and* again at the chain tip — and that every real pipe passes the hook's registration
probe in both; and that `minOut` and `maxDeposit` bite against real depth. Gas is snapshotted to `snapshots/PgasIngress.json` so a regression is visible rather than
discovered.

**Fork trap worth knowing:** a contract `new`-ed inside a fork test lands on a deterministic address
that may already hold a balance on mainnet. The router's test address holds 1 wei. That wei used to
reach the user — the router refunded its whole *balance* — and an exact assertion failed for a reason
that had nothing to do with this code; the router now refunds the call's own surplus and leaves it
alone. The suite still zeroes it in `setUp`, so what the assertions measure is what this build did
and nothing it inherited.

## Scripts — all dry-run by default

```bash
OWNER=0x… forge script script/MineHook.s.sol                        # the salt and the 0x2888 address
OWNER=0x… forge script script/Deploy.s.sol  --rpc-url $RPC          # hook + router
HOOK=0x…  forge script script/InitPool.s.sol --rpc-url $RPC         # one gateway pool
HOOK=0x…  forge script script/Register.s.sol --rpc-url $RPC         # one route (WRITE-ONCE)
```

**`Deploy` and `Register` read the chain they are about to arm, so point them at one.** `--rpc-url`
is read-only and a dry run still sends nothing. With no chain behind them every read comes back as
"nothing there" — which would print as reassurance ("the mined address is free", "the pipe is not
that one") — so both refuse up front, on the PoolManager having no code. An unreadable query is not
evidence of anything.

Three more refusals, one per script, after a review found each of them missing:

* **`MineHook` refuses to mine for an owner nobody set.** The salt is a function of the constructor
  args and the owner is one of them, so a salt mined for the placeholder is void for the real owner.
  The guard that said so read `vm.envOr("ALLOW_PLACEHOLDER", true)` — it defaulted to permission and
  could never fire. Silence is the refusal now; `ALLOW_PLACEHOLDER=1` mines a throwaway on purpose.
* **`Deploy` names what is living at the mined address** before it broadcasts, instead of walking
  into a `CREATE2` revert with no reason string. Empty → deploy. Ours (it answers `hookFlags()` and
  `poolManager()` with our values) → already deployed, skip the hook and carry on to the router.
  Anything else → refuse, and say that the salt is deterministic so a re-run cannot clear it: mine
  the next one. It asks the code what it is rather than inferring identity from the arithmetic that
  produced the address.
* **`Register` pre-flights the inner pool and the pipe.** The registration compared the two keys'
  currencies and stopped there, so a wrong fee tier or tick spacing named a pool that does not exist
  and an initialised-but-empty pool looked real and would have paid nothing — `InnerSwapEmpty`, at
  the first deposit, after the route was frozen. Both are read here (`sqrtPriceX96 != 0`,
  `liquidity != 0`) while they are still free to fix, along with the pipe's bytecode hash.

Each of those decisions is a `pure` or `view` resolver covered by `test/PgasScripts.t.sol`, for the
same reason `resolveRoute` is: a check reachable only by pointing a script at mainnet is a check
nobody exercises, and these run once each, on the day money starts moving.

`Register.s.sol` resolves the three fields a route can never take back — `grid`, `minRelayerFee`,
`beamPubkey` — from the pinned table in `script/PgasAddresses.sol` instead of from a per-field
default, and refuses rather than guessing:

| env | default | refused when |
|---|---|---|
| `GRID` | the grid pinned for the **output asset** | it contradicts that asset's decimals; the asset is unpinned and `GRID` was not given |
| `MIN_RELAYER_FEE` | the tariff pinned for that asset — the same number the API quotes | below one grid step; the asset is unpinned and it was not given |
| `PIPE` | the ETH pipe | its live `EXTCODEHASH` is not the one pinned for it; it has no code at this address; no hash is pinned for it at all |
| `PUBKEY` | the key pinned **for that pipe** | the pipe has no key pinned (it does not fall back to another pipe's); not 33 bytes; the pipe does not carry the route's output asset |

That last row is the one with no on-chain backstop. The Beam public key is derived from *(wallet
master key, **pipe cid**)* — **one key per pipe** — and the hook checks only that it is 33 bytes, as
does the bridge. The pipe/asset pairing *is* self-checking by ABI (a native pipe rejects a token
amount, an ERC-20 pipe rejects `msg.value`), but the pubkey/pipe pairing is checked nowhere, and the
route is write-once with `beamPubkey` absent from `tightenRoute`. So the arming checklist has one
step that no test can do for you: **prove on the Beam side that the key is spendable for THAT pipe's
cid before `PGAS_CONFIRM=1`** — a derive-and-compare check that proves the destination without
revealing the key. `resolveRoute` is `pure` and covered by the suite, so the refusals above are
exercised by `forge test`, not only by pointing a script at a chain.

None of these send anything. `forge script` without `--broadcast` only simulates, and each script
*additionally* refuses to enter a broadcast unless `PGAS_CONFIRM=1` — so an accidental `--broadcast`
still does nothing. Each prints every value it would use, so a human reads them before signing.
`MAX_DEPOSIT` is re-derived from the inner pool's live depth before each arming, never carried over
from a previous run, and never a flat dollar cap: a pool initialised with zero liquidity looks real
and pays nothing, so depth is read, not assumed.

Deployment, arming and any movement of funds are the operator's call, with a key that is neither a
treasury nor a settlement wallet.

## Addresses

Every pinned address lives in exactly one place, `script/PgasAddresses.sol`, so scripts and fork
tests read the same values and nothing hardcodes its own copy.

| | |
|---|---|
| PoolManager (mainnet) | `0x000000000004444c5dc75cB358380D2e3dE08A90` |
| CREATE2 proxy | `0x4e59b44847b379578588920cA78FbF26c0B4956C` |
| Beam bridge pipe, ETH | `0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96` |
| `NewLocalMessage` topic0 | `0x5f52670be4e2f3d7b079180b485ab44712641a10d1c77e843355f96036608ac7` |
| `PgasDeposit` topic0 | `0xd0e41515b729c9019d2f260f5fb9501b4fc547032008d04c4cdbfda6b788ae2b` |
| ETH pipe `EXTCODEHASH` | `0x44f00b5441a2c3d2da0d2ce424122ac7579cfdf765783f3f5dc5d9b3685b315d` |
| ERC-20 pipes `EXTCODEHASH` (all three) | `0xbfb427024ce037f158f31749e19c5adc4e46aa3a16bd1373d932f5216a30cf21` |

Four pipes are pinned and **one Beam public key is** — the ETH pipe's. `beamPubkeyFor(pipe)` refuses
for the other three rather than handing back this one; deriving each key from its own pipe cid is
work that has to happen before those routes exist.

## Risks, stated plainly

* **Price impact** on the inner pool is bounded by `minOut` in `hookData` (the hook reverts) and by
  the route's `maxDeposit`. `minOut` is the *only* slippage bound that works on this path — a
  router-level `amountOutMinimum` cannot be used, because the hook takes the whole output — and it
  now bounds `value`, what the user receives on Beam, rather than the gross swap output.
* **`minOut = 0` is refused** (`NoSlippageBound`). The hook has no independent price reference and
  cannot have one from the pool it is about to trade on: a same-block front-run moves the very spot
  price it would read, so a "reference" taken there would wave through exactly the attack it is
  meant to catch. Measured on the real ETH/USDC 0.30% pool at block 25,942,000, a 12M USDC front-run
  took **1,631 bps** off a 50-USDC deposit; offline, on a shallower pool, 9,977 bps. So the bound
  has to come from the caller, and the one thing this contract can enforce is that a caller states
  one. **A weak bound is still weak** — `minOut = 1` protects nothing, and the suite says so out
  loud rather than pretending otherwise. What actually limits the exposure is the route's
  `maxDeposit`, re-derived from live depth, and a `minOut` the API computes from the V4Quoter.
* **MEV.** The deposit is a public swap; sandwiching is bounded by `minOut`. Nothing here is
  slippage-free and this project does not claim otherwise.
* **The pipe is trusted to consume what it is handed.** Both branches now assert the counterpart —
  exactly `out` left this contract and nothing came back — so a pipe that draws only part of an
  allowance, or refunds, reverts the whole deposit instead of stranding the output here while both
  logs report it delivered. What the hook can see from where it stands is its own balance; that the
  destination's balance *rose* by the same amount is measured against the real pipe in the fork
  suite, where it can be measured. **That an address which takes ETH and does nothing satisfies the
  same post-condition** is why a pipe must also prove itself at registration — see *The pipe has to
  prove it is a pipe* above.
* **The router refunds this call's surplus, and only that.** Anyone can force ETH into any address,
  so a balance read there is "unspent value plus whatever a stranger left"; it used to be paid out
  to whoever deposited next, and 1 wei of it was enough to fail a contract depositor with no
  `receive()` on a refund it had not asked for. Value that comes to rest in the router therefore
  stays there: it has no owner and no rescue, which is deliberate.
* **No upgradeability**, as above. The owner can pause and rescue; the owner cannot redirect a
  route.
* **Unaudited.** These contracts have not been audited.
