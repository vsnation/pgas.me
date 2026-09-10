# Pgas.me

**Private gas for fresh EVM wallets, settled on Beam.**

On a public ledger a new wallet is tied forever to whatever funded it. There are two ordinary ways
to put gas into one: send from a wallet you already control (links the two permanently) or withdraw
from an exchange (ties the address to a KYC identity). Pgas.me is a third way: pay once with a token
you already hold, then have ETH show up in any set of fresh wallets, at the times you choose, with
no on-chain path back to what you paid with.

The value crosses into **Beam** — Mimblewimble: no addresses on chain, blinded amounts, confidential
assets — through the Beam ⇄ Ethereum bridge, is shielded there, and comes back out through the same
bridge into the wallets you listed.

Status: the site, wallet sign-in, portfolio, balance and the scheduler are live; the Uniswap V4 hook
ingress is written and tested and not yet deployed. Every money-moving path stays behind an explicit
arm flag until it has been proven with tiny real amounts.

## How it works

![One deposit, end to end](docs/scheme-topology.png)

1. **Connect a wallet and sign in.** The account *is* the wallet (Sign-In with Ethereum) — no seed
   phrase, no email, no password. The app reads your portfolio on every EVM chain it supports, one
   batch-balance call per chain, and shows your top holdings as tap-to-pay chips.
2. **Deposit: pay any token on Ethereum.** One **Uniswap V4** swap whose **Pgas hook** locks the
   output into the Beam bridge in the same transaction — ETH by default, DAI or WBTC if you would
   rather bring those. One signature, one transaction: the hook forwards the swap output into the
   bridge pipe itself, so nothing is ever left sitting in an intermediate address, and a hook that
   reverts reverts the whole swap.
3. **The bridge relayer mints the confidential asset on Beam.** Pgas.me claims it into its
   BeamPay-managed wallet, shields it into the Lelantus pool as max-privacy outputs, and credits
   your balance: **Arriving · Available · Scheduled · Paid out**.
4. **Schedule the payouts.** Paste the wallets and amounts — one per line, or Disperse-style
   `address:amount;address:amount` — and pick when each one should arrive. Pgas.me hands each order
   to the Beam bridge 66 minutes ahead of the time you asked for, so the ETH is there when you said,
   not whenever a queue drains.
5. **Each wallet is paid from the bridge.** Its first inbound comes from the Beam bridge contract,
   not from you and not from an exchange.

The fee is **2 %**, taken from your balance. The bridge fee is paid by Pgas.me, so each wallet
receives exactly the amount you typed.

## What the user does

![What the user does, step by step](docs/scheme-user-steps.png)

Destinations are just addresses. There is **no wallet generation** and **no signature to prove a
destination** — you paste an address, and the only check is its EIP-55 checksum, which catches the
one class of typo a checksum can actually catch. Every wallet you list becomes its own order with
its own status: `scheduled → releasing → bridging → delivering → sent`.

## What the platform does

![What the platform does, step by step](docs/scheme-platform-sequence.png)

Deposits are watched on chain, not trusted from the client: the pipe's own lock event is matched to
the swap that produced it in the same receipt, and the checkpoint never advances past a chunk that
failed. Balances are **sums over an append-only ledger**, never stored numbers. Every step emits a
Telegram event, and an independent watchdog runs outside the API process, so a stuck worker is
noticed by something that is not the stuck worker.

## Balance model

![The balance the user sees](docs/scheme-balance.png)

Four states, always visible, named for what the money is doing rather than which bucket it sits in.
Custodial in this version: the platform's Beam wallet holds the asset behind a BeamPay ledger with
one address per deposit, and the account that sums its own entries is the wallet you signed in with —
so the same balance appears on any device you connect that wallet from.

## Who sees what

Pgas.me does not claim privacy it cannot deliver. This is what each observer can link, stage by
stage — including Pgas.me itself, which in this version is custodial and knows the pairing until
settlement, then nulls it:

![Who can see what](docs/scheme-who-sees-what.png)

The bridge is operated by the Beam team's relayer (one key), Beam's shielded pool is small today,
and the largest anonymity sets on Ethereum belong to other tools. Saying that plainly is part of the
product.

## Trust ladder

![Trust ladder](docs/scheme-trust-ladder.png)

Each version hands a little more custody back to the user, until the last one needs no operator at
all.

## Stack

- **web/** — React 18 + Vite + TypeScript + ethers v6. Injected wallets (EIP-6963 and legacy
  `window.ethereum`), Sign-In with Ethereum, cross-chain portfolio through batch-balance contracts,
  deposit / balance / schedule / activity screens. Playwright end-to-end tests against a mock wallet.
- **api/** — FastAPI + MongoDB (Python 3.12). SIWE sessions, quotes, the on-chain deposit watcher,
  an append-only per-asset ledger, the scheduler and the payout workers, stats, Telegram events
  (default-deny) and the stuck-service watchdog.
- **Beam side** — a Beam node plus `wallet-api` and **BeamPay** as the only interface to the wallet's
  money, the public Beam ⇄ Ethereum bridge pipes (`EthPipe`
  `0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96` for ETH, ERC-20 pipes for DAI and WBTC), and
  Lelantus max-privacy shielding of the treasury.
- **contracts/** — the Solidity ingress (Foundry). A Pgas **gateway pool** holds no liquidity and
  carries a hook whose `beforeSwap` takes the whole input, routes it through the canonical Uniswap
  V4 pool that Uniswap's own LPs fund, and forwards the entire output into the Beam bridge pipe in
  the same transaction. Because the hook returns `+amountIn` as its delta, the gateway pool itself
  never trades — the price comes from the canonical pool, and nothing is ever left sitting in an
  intermediate address. `hookData` carries the deposit reference, the minimum output and the
  relayer-fee quote; the entry point is `PgasRouter.deposit(PoolKey, bool, uint256, bytes)`. The
  permissions are encoded in the hook's address, so the hook is not upgradeable. Details, risks and
  the grid arithmetic that keeps a bridged amount mintable: [contracts/README.md](contracts/README.md).

**Deployed addresses: pending.** Nothing is deployed yet — the hook, the router and the gateway
pools get their addresses published here once they are live and verified. The addresses the code
pins today are third-party infrastructure only: the Uniswap V4 `PoolManager`, the canonical CREATE2
proxy and the public Beam bridge pipes, all in `contracts/script/PgasAddresses.sol`.

## Running it

```bash
# API (Python 3.12+, MongoDB on localhost)
cd api && python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
cp .env.example .env            # fill in what you need; nothing is armed by default
.venv/bin/pytest -q
.venv/bin/uvicorn pgasme.main:app --port 8300

# Web
cd web && npm ci && npm run dev  # proxies /api → http://127.0.0.1:8300
npx playwright test              # end-to-end against a mock wallet

# Contracts (Foundry, plus Node for the pinned Uniswap sources)
cd contracts && npm install      # @uniswap/v4-core and v4-periphery, pinned exactly
forge build
forge test -vv                   # the offline suite: no network, no RPC, no fork

# The mainnet-fork suite needs an archive RPC and is excluded from a bare `forge test`
export FORK_RPC_URL=https://eth.drpc.org
FOUNDRY_PROFILE=fork forge test -vv
```

Read the exit code, not the last line: `forge test | tail; EXIT=${PIPESTATUS[0]}`.

## License

MIT — see [LICENSE](LICENSE).
