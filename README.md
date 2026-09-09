# Pgas.me

**Private gas funding for fresh EVM wallets, settled on Beam.**

On a public ledger every wallet is permanently linked to the wallet that funded it. There are
two ways to put gas into a new address today: send from a wallet you already control (links the
two forever) or withdraw from an exchange (ties the address to a KYC identity). Pgas.me is the
third way: pay any token on any chain once, and later — whenever you choose — fund fresh wallets
with ETH that has no on-chain path back to the source.

The value crosses into **Beam** (Mimblewimble: no addresses on-chain, blinded amounts,
confidential assets) through the Beam ⇄ Ethereum bridge, is shielded there, and comes back out
through the same bridge into the wallets you tick.

Built for a hackathon. Status: **the site, wallet sign-in, portfolio, wallet manager and the
deposit/withdraw flows are live; deposits open once the first small real bridge crossings have
been validated; payouts run from a bridge-funded float.**

## How it works

### One deposit, end to end

![Topology](docs/scheme-topology.png)

1. **Connect a wallet and sign in.** The account *is* the wallet (Sign-In with Ethereum). No seed
   phrase, no email. The app shows your portfolio across every supported chain — one batch
   balance call per chain.
2. **Pay any asset.** A [deBridge DLN](https://debridge.com) order moves it to Ethereum as the
   target asset (ETH by default; DAI or WBTC to bring liquidity). The order carries a **hook**
   that calls the Beam bridge contract of that asset in the same transaction, so the ETH is
   locked in the bridge the moment the solver fills the order. If the hook ever fails, deBridge
   delivers to your own wallet — never to us.
3. **The bridge relayer mints the confidential asset on Beam**; Pgas.me claims it and credits your
   balance. You see *Pending · Available · Scheduled · Sent* at all times.
4. **Register the wallets you want to fund**: generate fresh ones in the browser (the key is shown
   once and never stored by Pgas.me) or prove control of existing ones by signing a message —
   an off-chain signature, so a wallet with zero gas can sign.
5. **Withdraw whenever you want.** Tick wallets, set an amount per wallet, pick a window. In
   *direct* mode the Beam bridge pays each wallet itself (one crossing per wallet); in *instant*
   mode a bridge-funded distributor pays within a minute. The 2% fee is charged at unlock, so the
   payout is always a whole amount.

### What the user does

![User steps](docs/scheme-user-steps.png)

### What the platform does

![Platform sequence](docs/scheme-platform-sequence.png)

### The balance you see

![Balance model](docs/scheme-balance.png)

## Honest privacy

Pgas.me does not claim privacy it cannot deliver. This is what each observer can link, stage by
stage — including Pgas.me itself, which in this version is custodial and sees the pairing until
settlement (then deletes it):

![Who sees what](docs/scheme-who-sees-what.png)

The bridge is operated by the Beam team's relayer (one key), Beam's shielded pool is small today,
and the largest anonymity sets on Ethereum belong to other tools. The roadmap moves custody to
the user step by step:

![Trust ladder](docs/scheme-trust-ladder.png)

## Stack

- **web/** — React 18 + Vite + TypeScript + ethers v6. Injected wallets (EIP-6963 + legacy
  `window.ethereum`), SIWE, cross-chain portfolio via batch-balance contracts, deposit / balance /
  wallets / withdraw / activity pages. Playwright e2e with a mock wallet.
- **api/** — FastAPI + MongoDB (Python 3.12). SIWE sessions, destination proofs
  (`personal_sign`, signature verified then discarded), deBridge DLN two-step quote with the
  `dlnHook` that calls the target asset's Beam pipe, on-chain deposit watcher (pipe
  `NewLocalMessage` logs matched to DLN `FulfilledOrder` in the same receipt, checkpoint never
  advanced past a failed chunk), append-only per-asset ledger (balances are sums), withdrawals,
  stats, Telegram alerts (default-deny), stuck-service monitor.
- **Beam side** — the public Beam ⇄ Ethereum bridge (`EthPipe` `0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96`
  for ETH; ERC-20 pipes for DAI and WBTC), a dedicated Beam wallet, Lelantus-MW shielding of the
  treasury, one bridge crossing per payout in direct mode.

Every money-moving path is behind an explicit arm flag and stays dark until it has been
validated with real, tiny amounts.

## Run it locally

```bash
# API (Python 3.12+, MongoDB on localhost)
cd api && python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
cp .env.example .env            # fill in what you need; nothing is armed by default
.venv/bin/pytest -q
.venv/bin/uvicorn pgasme.main:app --port 8300

# Web
cd web && npm ci && npm run dev  # proxies /api → http://127.0.0.1:8300
npx playwright test              # e2e against a mock wallet
```

## License

MIT — see [LICENSE](LICENSE).
