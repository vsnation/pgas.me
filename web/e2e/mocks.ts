// Test doubles for the two things the web app talks to: the Pgas.me API (API_CONTRACT.md, served
// through page.route) and an injected EIP-1193 wallet (window.ethereum + EIP-6963 announcement).
// personal_sign is delegated to Node-side ethers wallets through page.exposeFunction, so the mock
// API can really recover signers and the message templates are checked end to end.
import type { Page, Route } from '@playwright/test';
import { AbiCoder, Interface, Wallet, getAddress, getBytes, verifyMessage } from 'ethers';

export const HOST = '127.0.0.1:4173';
export const NATIVE = '0x0000000000000000000000000000000000000000';
export const USDC = '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48';
export const STATEMENT = 'Sign in to Pgas.me. This signature costs nothing and moves nothing.';

export const walletA = new Wallet('0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d');
export const walletB = new Wallet('0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a');

export interface ApiCall {
  method: string;
  path: string;
  body: unknown;
  auth: string | null;
}

// Five EVM chains (Story and Cronos have no batch-balance helper, so the scanner uses Multicall3
// there) plus the two non-EVM chains the API lists, which must render as "not scanned", never as
// errors. `route_chain_id` is the router's own id and differs from the EVM id on three of them.
const CHAINS = [
  { chain_id: 1, route_chain_id: 1, name: 'Ethereum', native_symbol: 'ETH' },
  { chain_id: 42161, route_chain_id: 42161, name: 'Arbitrum', native_symbol: 'ETH' },
  { chain_id: 8453, route_chain_id: 8453, name: 'Base', native_symbol: 'ETH' },
  { chain_id: 1514, route_chain_id: 100000013, name: 'Story', native_symbol: 'IP' },
  { chain_id: 25, route_chain_id: 100000019, name: 'Cronos', native_symbol: 'CRO' },
  { chain_id: 7565164, route_chain_id: 7565164, name: 'Solana', native_symbol: 'SOL' },
  { chain_id: 728126428, route_chain_id: 100000026, name: 'Tron', native_symbol: 'TRX' },
];

export const DAI = '0x6B175474E89094C44Da98b954EedeAC495271d0F';
export const WBTC = '0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599';

/**
 * The token list's logo host. A stand-in here — the real list names whatever CDN it likes and the
 * client never spells one out — but the URL shape is the live one; see NATIVE_TOKENS below.
 * `blockExternal(page, true)` serves anything under it locally, so screenshots stay offline.
 */
export const LOGO_HOST = 'https://tokens.cdn.invalid';
export const TOKEN_LOGO = (chainId: number, address: string) => `${LOGO_HOST}/Logo/${chainId}/${address}/small/token-logo.svg`;

const ERC20_TOKENS = [
  { address: USDC, symbol: 'USDC', name: 'USD Coin', decimals: 6, logo: TOKEN_LOGO(1, USDC) },
  { address: DAI, symbol: 'DAI', name: 'Dai', decimals: 18, logo: '' },
  { address: WBTC, symbol: 'WBTC', name: 'Wrapped BTC', decimals: 8, logo: '' },
];

/**
 * The token list's native entry, verified against the live API on 2026-09-09:
 *   GET /v1/dex/tokens?chain_id=56 → {"address":"0x0000…0000","symbol":"BNB","name":"BNB",
 *   "decimals":18,"logo":"<the list's own CDN>/Logo/56/0x0000…0000/small/token-logo.svg"}
 * Cronos is deliberately absent so a chain whose list carries NO native entry is exercised: its chip
 * must fall back to the chain icon, never to a 3-letter monogram.
 */
const NATIVE_TOKENS: Record<number, { symbol: string; name: string }> = {
  1: { symbol: 'ETH', name: 'Ethereum' },
  42161: { symbol: 'ETH', name: 'Ethereum' },
  8453: { symbol: 'ETH', name: 'Ethereum' },
  1514: { symbol: 'IP', name: 'Story IP' },
};

function tokensFor(chainId: number) {
  const n = NATIVE_TOKENS[chainId];
  return n ? [{ address: NATIVE, ...n, decimals: 18, logo: TOKEN_LOGO(chainId, NATIVE) }, ...ERC20_TOKENS] : ERC20_TOKENS;
}

/** Ethereum's list: what the Deposit form loads by default, and the decimals table for MockRpc. */
const TOKENS = tokensFor(1);

const ASSETS = [
  {
    key: 'ETH',
    symbol: 'ETH',
    beam_symbol: 'bETH',
    token: NATIVE,
    decimals: 18,
    pipe: '0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96',
    aid: 36,
  },
  {
    key: 'DAI',
    symbol: 'DAI',
    beam_symbol: 'bDAI',
    token: '0x6B175474E89094C44Da98b954EedeAC495271d0F',
    decimals: 18,
    pipe: '0xAcDc8f4559741a3c8CAAB0ba74c57807A9Fe2d73',
    aid: 39,
  },
  {
    key: 'WBTC',
    symbol: 'WBTC',
    beam_symbol: 'bWBTC',
    token: '0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599',
    decimals: 8,
    pipe: '0x604422D7eC88c45b82B71851d073eFeaA928dcEF',
    aid: 38,
  },
];

const TX = '0x' + 'ab'.repeat(32);

/** groth → the plain ETH string the API puts in its own sentences (0.612, 0.5, 0.01). */
const eth = (g: number) => String(g / 1e8);
const ROUTER_ORDER = '0xeF4fB24aD0916217251F553c0596F8Edc630EB66';
const ROUTER_ALLOWANCE_TARGET = '0x6A000F20005980200259B80c5102003040001068';

/**
 * The Uniswap V4 ingress (API_CONTRACT.md § Quote mode "uniswap"): the tx goes to the PgasRouter,
 * and the hook's address carries its permissions in its low 14 bits — hence the `2888` tail, which
 * is the real deployment rule (contracts/README.md), not decoration.
 */
export const PGAS_ROUTER = '0x9a5c1d4F2B7e8A0C3d6f1b4e7A0C3D6f9b2e5a11';
export const PGAS_HOOK = '0x1f3D5b7C9e2A4d6F8b0C2E4a6D8f0B2C4E6A2888';
const GATEWAY_POOL_ID = '0x' + 'a1'.repeat(32);
const INNER_POOL_ID = '0x' + 'b2'.repeat(32);

export class MockApi {
  armed = false;
  directEnabled = true;
  instantEnabled = false;
  /**
   * `ingress:{uniswap, xchain, direct, uniswap_tokens}` as the API publishes it on `/dex/assets`
   * (with the pairs) and `/health` (without), and `uniswap`/`xchain` inside `/account.ingress`.
   *
   * Both start where the suite's existing journeys live — the pre-Uniswap world — for the same
   * reason `armed` starts false: a test states the world it is exercising. A test of the Uniswap
   * ingress sets `uniswapEnabled = true`, and `uniswapEnabled = false` IS the "the API says the
   * path is closed" case that the rest of the suite proves on every run.
   */
  uniswapEnabled = false;
  xchainEnabled = true;
  /**
   * An API build that publishes NO flags anywhere: `/dex/assets` and `/health` are 404, `/assets`
   * carries the asset registry only, and the account's `ingress` is the old `{armed, near}`. That
   * is today's live API, and the client must land on the defaults (lib/ingress.ts) — Uniswap off.
   */
  silentIngress = false;
  /** The pairs a gateway pool is registered for; the client narrows its token list to these. */
  uniswapTokens = ['ETH', 'WETH', 'USDC', 'USDT', 'DAI', 'WBTC'];
  /** What the quoter says this size costs on the inner pool (0 when nothing is swapped). */
  uniswapPriceImpactBps = 30;
  /** `min_out_units = out × (1 − slippage_bps)` — the only slippage bound on this path. */
  uniswapSlippageBps = 50;
  /**
   * settings.min_deposit_wei. The floor lives HERE, never in the client: when a quote comes in under
   * it the API answers 400 with its own sentence, and that sentence is the only minimum the UI shows.
   */
  minDepositWei: bigint | null = null;
  /** `GET /v1/withdrawals/fees` — the form reads every one of these, none of them is a client copy. */
  feeBps = 200;
  relayerFeeGrothNow = 2200;
  minAmountGroth = 1_000_000;
  bridgeEtaS = 3960; // PGAS_BRIDGE_ETA_S — 66 min
  /**
   * What `POST /v1/quote/{id}/arm` does to the estimate, in per cent. The web client re-asks the
   * user when the armed order lands more than 0.5 % from the number they were shown.
   */
  armDriftPct = 0;
  /** `arm` on a quote the router would no longer honour: the API answers 409, never a stale tx. */
  armExpired = false;
  /**
   * What goes on the wire as the cross-chain mode. The neutral name is `xchain`; an API build that
   * still answers with its own older name must render exactly the same UI, so a test puts that name
   * here — the client treats every mode it does not know as the cross-chain order.
   */
  xchainWire = 'xchain';
  /** bytes32 `deposit_ref`s, handed out in order so a test can name the one it expects. */
  uniswapRefSeq = 0;
  quotes: Record<string, Record<string, any>> = {};
  armCalls: string[] = [];
  calls: ApiCall[] = [];
  token: string | null = null;
  accountId = 'acct-' + walletA.address.slice(2, 10).toLowerCase();
  nonces = new Set<string>();
  requestSeq = 0;
  deposits: Record<string, unknown>[] = [
    {
      // credited, and the treasury is still sweeping it into the shielded pool: a SUB-status the
      // user must never see as a pill (their balance was credited an hour ago)
      _id: 'dep-1',
      asset: 'ETH',
      mode: 'xchain',
      status: 'credited',
      src: { chain_id: 1, token: NATIVE, amount: '100000000000000000' },
      quote_id: 'q-1',
      src_tx_hash: '0x' + '11'.repeat(32),
      order_id: '0x' + '22'.repeat(32),
      eth: { tx: '0x' + '33'.repeat(32), block: 21000000, msg_id: 4821, value_units: '98000000000000000' },
      value_groth: 9800000,
      verified: true,
      address: walletA.address,
      treasury: 'shielding',
      claim_txid: 'b7c1d2e3f4a5b6c7',
      created_at: 1757400000,
      updated_at: 1757400900,
    },
    {
      _id: 'dep-2',
      asset: 'ETH',
      mode: 'xchain',
      status: 'confirming',
      src: { chain_id: 42161, token: NATIVE, amount: '50000000000000000' },
      quote_id: 'q-2',
      src_tx_hash: '0x' + '44'.repeat(32),
      eth: { tx: '0x' + '55'.repeat(32), block: 21000100 },
      verified: true,
      address: walletA.address,
      created_at: 1757403600,
      updated_at: 1757403700,
    },
    {
      // registered seconds ago; the hash has not been tied to the quote yet
      _id: 'dep-3',
      asset: 'ETH',
      mode: 'direct',
      status: 'submitted',
      src: { chain_id: 1, token: NATIVE, amount: '30000000000000000' },
      quote_id: 'q-3',
      src_tx_hash: '0x' + '88'.repeat(32),
      eth: {},
      verified: false,
      address: walletA.address,
      created_at: 1757404200,
      updated_at: 1757404200,
    },
  ];
  requests: Record<string, unknown>[] = [
    {
      _id: 'req-1',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 10000000,
      fee_groth: 200000,
      deliver_at: 1757413960,
      release_at: 1757410000,
      status: 'scheduled',
      created_at: 1757406400,
    },
    {
      _id: 'req-2',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 1000000,
      fee_groth: 20000,
      deliver_at: 1757393960,
      release_at: 1757390000,
      status: 'sent',
      beam_txid: 'a1b2c3d4e5f6a1b2c3d4e5f6',
      msg_id: 4830,
      eth_tx: '0x' + '66'.repeat(32),
      eth_block: 21000420,
      relayer_fee_groth: 2200,
      beam_confirmations: 61,
      created_at: 1757389000,
    },
    {
      // released but parked: the operator turned direct payouts off, so the row carries the reason
      _id: 'req-3',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 2000000,
      fee_groth: 40000,
      deliver_at: 1757394960,
      release_at: 1757391000,
      status: 'releasing',
      hold_reason: 'PGAS_PAYOUT_DIRECT_ENABLED=0',
      created_at: 1757390500,
    },
    {
      _id: 'req-4',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 3000000,
      fee_groth: 60000,
      deliver_at: 1757395960,
      release_at: 1757392000,
      status: 'bridging',
      beam_txid: 'c3d4e5f6a1b2c3d4e5f6a1b2',
      msg_id: 4831,
      beam_confirmations: 43,
      relayer_fee_groth: 2200,
      created_at: 1757391500,
    },
    {
      _id: 'req-5',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 4000000,
      fee_groth: 80000,
      deliver_at: 1757396960,
      release_at: 1757393000,
      status: 'delivering',
      beam_txid: 'd4e5f6a1b2c3d4e5f6a1b2c3',
      msg_id: 4832,
      beam_confirmations: 61,
      relayer_fee_groth: 2200,
      created_at: 1757392500,
    },
    // the any-asset branch is dark; these two exist so the pill map is proven to cover them
    {
      _id: 'req-6',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 1500000,
      fee_groth: 30000,
      deliver_at: 1757397960,
      release_at: 1757394000,
      status: 'waiting_for_dep_eth',
      dark: true,
      created_at: 1757393500,
    },
    {
      _id: 'req-7',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 1500000,
      fee_groth: 30000,
      deliver_at: 1757398960,
      release_at: 1757395000,
      status: 'waiting_for_swap_to_target_asset',
      dark: true,
      created_at: 1757394500,
    },
  ];
  balances = { ETH: { available: 50000000, scheduled: 10200000, sent: 1000000, pending: 4900000 } };

  private json(route: Route, status: number, body: unknown) {
    return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  }

  /** The one place the flags are stated, so every read that carries them says the same thing. */
  private ingress(withTokens = false) {
    return {
      uniswap: this.uniswapEnabled,
      xchain: this.xchainEnabled,
      // published by the API, read by nothing in the client — a key it must simply carry past
      direct: true,
      ...(withTokens ? { uniswap_tokens: this.uniswapTokenRows() } : {}),
    };
  }

  /** `uniswap_tokens` in the live shape: the registered pairs, by address. */
  private uniswapTokenRows() {
    return this.uniswapTokens
      .map((sym) => TOKENS.find((t) => t.symbol.toLowerCase() === sym.toLowerCase()))
      .filter((t): t is (typeof TOKENS)[number] => !!t)
      .map((t) => ({ address: t.address, symbol: t.symbol, decimals: t.decimals }));
  }

  private account() {
    return {
      address: walletA.address,
      account_id: this.accountId,
      balances: this.balances,
      fee_bps: this.feeBps,
      denominations: [1000000, 10000000],
      min_payout_groth: this.minAmountGroth,
      modes: { direct: this.directEnabled, instant: this.instantEnabled },
      ingress: {
        armed: this.armed,
        near: false,
        ...(this.silentIngress ? {} : { uniswap: this.uniswapEnabled, xchain: this.xchainEnabled }),
      },
      deposits: this.deposits,
      requests: this.requests,
      destinations: 1, // the signed-in wallet is still auto-listed; the app no longer reads the list
      history: [
        { kind: 'credit', groth: 9800000, d_avail: 9800000, d_sched: 0, d_sent: 0, ref: 'dep-1', note: '', at: 1757400900 },
        { kind: 'schedule', groth: 10200000, d_avail: -10200000, d_sched: 10200000, d_sent: 0, ref: 'req-1', note: '', at: 1757406400 },
      ],
    };
  }

  async install(page: Page) {
    await page.route('**/api/v1/**', (route) => this.handle(route));
  }

  private async handle(route: Route) {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api\/v1/, '');
    const method = req.method();
    let body: unknown = null;
    const raw = req.postData();
    if (raw) {
      try {
        body = JSON.parse(raw);
      } catch {
        body = raw;
      }
    }
    const auth = req.headers()['authorization'] ?? null;
    this.calls.push({ method, path: path + url.search, body, auth });
    const locked = () => {
      if (!this.token || auth !== `Bearer ${this.token}`) {
        void this.json(route, 401, { detail: 'sign in first' });
        return false;
      }
      return true;
    };
    const b = (body ?? {}) as Record<string, any>;

    if (method === 'GET' && path === '/siwe/nonce') {
      const nonce = 'n' + Math.random().toString(36).slice(2, 12);
      this.nonces.add(nonce);
      return this.json(route, 200, { nonce, statement: STATEMENT, domains: [HOST] });
    }
    if (method === 'POST' && path === '/siwe/verify') {
      const message = String(b.message ?? '');
      const lines = message.split('\n');
      const nonce = /Nonce: (\S+)/.exec(message)?.[1];
      if (!lines[0].startsWith(HOST + ' wants you to sign in')) return this.json(route, 400, { detail: 'bad host line' });
      if (!nonce || !this.nonces.has(nonce)) return this.json(route, 400, { detail: 'unknown nonce' });
      if (!/URI: http:\/\/127\.0\.0\.1:4173\n/.test(message) || lines[3] !== STATEMENT)
        return this.json(route, 400, { detail: 'bad message' });
      let signer: string;
      try {
        signer = verifyMessage(message, String(b.signature));
      } catch {
        return this.json(route, 400, { detail: 'signature unreadable' });
      }
      if (signer.toLowerCase() !== lines[1].toLowerCase()) return this.json(route, 401, { detail: 'signer mismatch' });
      this.nonces.delete(nonce);
      this.token = 'tok-' + Math.random().toString(36).slice(2);
      return this.json(route, 200, { token: this.token, account_id: this.accountId, address: getAddress(lines[1]), expires_in: 3600 });
    }
    if (method === 'GET' && path === '/dex/chains') return this.json(route, 200, { chains: CHAINS });
    if (method === 'GET' && path === '/dex/tokens')
      return this.json(route, 200, { tokens: tokensFor(Number(url.searchParams.get('chain_id'))) });
    // The asset registry has never carried the flags; the client reads it here anyway, because an
    // API build that puts them here instead must still be understood.
    if (method === 'GET' && path === '/assets') return this.json(route, 200, { assets: ASSETS });
    // Where the 2026-09-10 API publishes them, pairs and all. `silentIngress` is the build that
    // has no such route at all: 404, and the client is left with its defaults.
    if (method === 'GET' && path === '/dex/assets') {
      if (this.silentIngress) return this.json(route, 404, { detail: 'Not Found' });
      return this.json(route, 200, { assets: ASSETS, ingress: this.ingress(true) });
    }
    if (method === 'GET' && path === '/health') {
      if (this.silentIngress) return this.json(route, 404, { detail: 'Not Found' });
      return this.json(route, 200, {
        ok: true,
        version: 'test',
        env: 'test',
        ingress_armed: this.armed,
        paused: false,
        ingress: this.ingress(),
      });
    }
    if (!locked()) return;

    if (method === 'GET' && path === '/account') return this.json(route, 200, this.account());
    if (method === 'POST' && path === '/quote') {
      // API_CONTRACT.md "Quote modes": off Ethereum it is a cross-chain order; on Ethereum the source
      // token either IS the target asset (direct to the pipe) or is swapped in the user's own wallet.
      const amount = BigInt(String(b.amount));
      const target = ASSETS.find((a) => a.key === b.target_asset) ?? ASSETS[0];
      const src = tokensFor(Number(b.src_chain_id)).find((t) => t.address.toLowerCase() === String(b.src_token).toLowerCase()) ?? {
        address: String(b.src_token),
        symbol: 'UNKNOWN',
        name: 'Unknown',
        decimals: 18,
        logo: '',
      };
      const isTarget = src.address.toLowerCase() === target.token.toLowerCase();
      const mode = Number(b.src_chain_id) !== 1 ? 'xchain' : isTarget ? 'direct' : 'swap';
      const native = src.address.toLowerCase() === NATIVE;
      // a crude but deterministic conversion into the target asset's units
      const usdOf = (v: bigint, dec: number) => (Number(v) / 10 ** dec) * (native ? 4000 : src.symbol === 'WBTC' ? 100000 : 1);
      const toTarget = (v: bigint) => {
        const usd = usdOf(v, src.decimals);
        const price = target.key === 'ETH' ? 4000 : target.key === 'WBTC' ? 100000 : 1;
        return BigInt(Math.round((usd / price) * 10 ** target.decimals));
      };
      // API_CONTRACT.md § Quote mode "uniswap" (2026-09-10): asked for by name, on Ethereum, ETH
      // out, and only for a registered pair. The tx is built with the quote — the quote is local
      // (one Quoter read), so there is no /arm step and no order id.
      if (String(b.route ?? '') === 'uniswap' && this.uniswapEnabled && Number(b.src_chain_id) === 1 && target.key === 'ETH') {
        const registered = this.uniswapTokens.some(
          (t) => t.toLowerCase() === src.symbol.toLowerCase() || t.toLowerCase() === src.address.toLowerCase(),
        );
        if (!registered) return this.json(route, 400, { detail: `no Uniswap route for ${src.symbol}` });
        // nothing is swapped when the input already IS the output — the hook only locks it, so that
        // route has no price impact and no slippage bound to state
        const swapped = !native;
        const out = swapped ? (toTarget(amount) * 997n) / 1000n : amount;
        if (this.minDepositWei !== null && out < this.minDepositWei)
          return this.json(route, 400, { detail: `below the minimum deposit of ${Number(this.minDepositWei) / 1e18} ETH` });
        const minOut = swapped ? (out * BigInt(10000 - this.uniswapSlippageBps)) / 10000n : out;
        const fee = out / 1000n;
        const quoteId = 'q-' + Math.random().toString(36).slice(2, 8);
        const uni: Record<string, unknown> = {
          quote_id: quoteId,
          mode: 'uniswap',
          target_asset: target.key,
          armed: this.armed,
          expires_at: new Date(Date.now() + 30_000).toISOString(),
          deposit_ref: '0x' + (++this.uniswapRefSeq).toString(16).padStart(64, '0'),
          route: {
            hook: PGAS_HOOK,
            router: PGAS_ROUTER,
            gateway_pool_id: GATEWAY_POOL_ID,
            inner_pool_id: INNER_POOL_ID,
            token_in: src.address,
            token_out: target.token,
          },
          estimate: {
            src: { chain_id: b.src_chain_id, token: src.address, symbol: src.symbol, decimals: src.decimals, amount: String(b.amount) },
            out_units: out.toString(),
            min_out_units: minOut.toString(),
            value_units: (out - fee).toString(),
            relayer_fee_units: fee.toString(),
            out_groth: Number((out * 100000000n) / 10n ** BigInt(target.decimals)),
            usd: usdOf(amount, src.decimals),
            eta_s: 12 * 12 + 120,
            price_impact_bps: swapped ? this.uniswapPriceImpactBps : 0,
          },
        };
        if (this.armed) {
          uni.tx = { chain_id: 1, to: PGAS_ROUTER, data: '0xc0ffee00', value: native ? String(amount) : '0' };
          // one approval, and its spender is the router the deposit tx goes to
          if (!native) uni.approval = { chain_id: 1, token: src.address, spender: PGAS_ROUTER, amount: String(amount) };
        } else uni.note = 'ingress not armed: no Beam pubkey configured';
        // remembered only so the registered deposit row carries `mode:"uniswap"`, as the API's does
        this.quotes[quoteId] = { mode: 'uniswap' };
        return this.json(route, 200, uni);
      }
      const outUnits = mode === 'direct' ? amount : ((isTarget ? amount : toTarget(amount)) * 98n) / 100n;
      // routers/quote.py check_min_deposit(): 400 before anything else is built or stored
      if (this.minDepositWei !== null && outUnits < this.minDepositWei)
        return this.json(route, 400, { detail: `below the minimum deposit of ${Number(this.minDepositWei) / 1e18} ETH` });
      const relayerFee = outUnits / 1000n;
      const estimate: Record<string, unknown> = {
        src: { chain_id: b.src_chain_id, token: src.address, symbol: src.symbol, decimals: src.decimals, amount: String(b.amount) },
        out_units: outUnits.toString(),
        out_groth: Number((outUnits * 100000000n) / 10n ** BigInt(target.decimals)),
        usd: usdOf(amount, src.decimals),
        eta_s: mode === 'direct' ? 12 * 12 + 120 : 420,
      };
      const quoteId = 'q-' + Math.random().toString(36).slice(2, 8);
      const quote: Record<string, unknown> = {
        quote_id: quoteId,
        target_asset: target.key,
        mode: mode === 'xchain' ? this.xchainWire : mode,
        armed: this.armed,
        expires_at: new Date(Date.now() + 30_000).toISOString(),
        estimate,
      };
      if (mode === 'swap') {
        // the swap lands in the user's OWN wallet, so it is issued armed or not; never a deposit
        estimate.route_fees = { protocolFee: '1000000000000000' };
        quote.swap_tx = { chain_id: 1, to: ROUTER_ORDER, data: '0xfeedface', value: native ? String(amount) : '0' };
        if (!native) quote.approval = { chain_id: 1, token: src.address, spender: ROUTER_ALLOWANCE_TARGET, amount: String(amount) };
        quote.next = { src_chain_id: 1, src_token: target.token, amount: outUnits.toString() };
        return this.json(route, 200, quote);
      }
      estimate.value_units = (outUnits - relayerFee).toString();
      estimate.relayer_fee_units = relayerFee.toString();
      // T2b (2026-09-09): a cross-chain quote is ONE router call and carries no tx — the hook order
      // (and its approval and order id) is built by POST /v1/quote/{id}/arm on the Deposit click.
      if (mode === 'xchain') {
        estimate.route_fees = { protocolFee: '1000000000000000' };
        this.quotes[quoteId] = {
          mode,
          srcChainId: Number(b.src_chain_id),
          srcToken: src.address,
          amount: String(amount),
          native,
          estimate,
        };
      }
      if (this.armed) {
        if (mode === 'direct') {
          quote.tx = { chain_id: 1, to: target.pipe, data: '0xdeadbeef', value: native ? String(amount) : '0' };
          if (!native) quote.approval = { chain_id: 1, token: src.address, spender: target.pipe, amount: String(amount) };
        }
      } else quote.note = 'ingress not armed: no Beam pubkey configured';
      return this.json(route, 200, quote);
    }
    const armPath = /^\/quote\/([^/]+)\/arm$/.exec(path);
    if (method === 'POST' && armPath) {
      const q = this.quotes[armPath[1]];
      if (!q) return this.json(route, 404, { detail: 'unknown quote' });
      // a uniswap quote is built armed and has no /arm step: asking is a client bug, said out loud
      if (q.mode !== 'xchain') return this.json(route, 409, { detail: `a ${q.mode} quote carries its transaction already` });
      if (this.armExpired) return this.json(route, 409, { detail: 'the quote expired — refresh it' });
      if (!this.armed) return this.json(route, 409, { detail: 'ingress not armed: no Beam pubkey configured' });
      this.armCalls.push(armPath[1]);
      // idempotent: a second call returns the order already stored on the quote, drift and all
      if (!q.armedResult) {
        const shown = BigInt(String(q.estimate.out_units));
        const out = shown + (shown * BigInt(Math.round(this.armDriftPct * 100))) / 10000n;
        q.armedResult = {
          quote_id: armPath[1],
          tx: { chain_id: q.srcChainId, to: ROUTER_ORDER, data: '0xdeadbeef', value: q.native ? q.amount : '0' },
          ...(q.native ? {} : { approval: { chain_id: q.srcChainId, token: q.srcToken, spender: ROUTER_ORDER, amount: q.amount } }),
          order_id: '0x' + '77'.repeat(32),
          estimate: { ...q.estimate, out_units: out.toString() },
          expires_at: new Date(Date.now() + 30_000).toISOString(),
        };
      }
      return this.json(route, 200, q.armedResult);
    }
    if (method === 'POST' && path === '/deposits') {
      const id = 'dep-' + (this.deposits.length + 1);
      const from = this.quotes[String(b.quote_id)];
      this.deposits.unshift({
        _id: id,
        asset: 'ETH',
        ...(from?.mode ? { mode: from.mode } : {}),
        status: 'submitted',
        src: { chain_id: 1, token: NATIVE, amount: '200000000000000000' },
        quote_id: b.quote_id,
        src_tx_hash: b.src_tx_hash,
        eth: {},
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
      });
      return this.json(route, 200, { deposit_id: id, status: 'submitted' });
    }
    if (method === 'GET' && path === '/withdrawals/fees') {
      return this.json(route, 200, {
        fee_bps: this.feeBps,
        relayer_fee_groth_now: this.relayerFeeGrothNow,
        min_amount_groth: this.minAmountGroth,
        bridge_eta_s: this.bridgeEtaS,
      });
    }
    if (method === 'POST' && path === '/withdrawals') {
      // API_CONTRACT.md § Withdrawals (restructured 2026-09-09): a list of orders, each with its own
      // delivery time. No destination registry — W is whatever the user typed, checksummed.
      if (b.mode !== 'direct' || !this.directEnabled) return this.json(route, 409, { detail: `${b.mode} mode is not enabled yet` });
      const items = (b.items ?? []) as { W: string; amount_groth: number; deliver_at: number }[];
      if (!items.length) return this.json(route, 400, { detail: 'items must not be empty' });
      for (const it of items) {
        let W: string;
        try {
          W = getAddress(String(it.W));
        } catch {
          return this.json(route, 400, { detail: `${it.W} is not a valid EVM address` });
        }
        if (W !== it.W) return this.json(route, 400, { detail: `${it.W} is not EIP-55 checksummed` });
        if (typeof it.deliver_at !== 'number' || !Number.isFinite(it.deliver_at))
          return this.json(route, 400, { detail: 'deliver_at must be unix seconds' });
        if (it.amount_groth < this.minAmountGroth)
          return this.json(route, 400, { detail: `below the minimum payout of ${eth(this.minAmountGroth)} ETH` });
      }
      const total = items.reduce((sum, i) => sum + i.amount_groth, 0);
      const fee = Math.ceil((total * this.feeBps) / 10000);
      const available = this.balances.ETH.available;
      if (total + fee > available)
        return this.json(route, 409, {
          detail: `short by ${eth(total + fee - available)} ETH — this batch debits ${eth(total + fee)} ETH (amounts + ${
            this.feeBps / 100
          }%) and Available is ${eth(available)} ETH`,
        });
      const now = Math.floor(Date.now() / 1000);
      const out = items.map((it) => {
        const request_id = `req-new-${++this.requestSeq}`;
        const release_at = Math.max(now, it.deliver_at - this.bridgeEtaS);
        this.requests.unshift({
          _id: request_id,
          asset: 'ETH',
          mode: 'direct',
          W: it.W,
          amount_groth: it.amount_groth,
          fee_groth: Math.ceil((it.amount_groth * this.feeBps) / 10000),
          deliver_at: it.deliver_at,
          release_at,
          status: 'scheduled',
          created_at: now,
        });
        return { request_id, W: it.W, amount_groth: it.amount_groth, deliver_at: it.deliver_at, release_at };
      });
      this.balances.ETH.available -= total + fee;
      this.balances.ETH.scheduled += total + fee;
      return this.json(route, 200, {
        request_ids: out.map((o) => o.request_id),
        fee_groth: fee,
        total_debited_groth: total + fee,
        relayer_fee_groth_estimate: this.relayerFeeGrothNow,
        min_amount_groth: this.minAmountGroth,
        items: out,
      });
    }
    const cancelPath = /^\/withdrawals\/([^/]+)\/cancel$/.exec(path);
    if (method === 'POST' && cancelPath) {
      const row = this.requests.find((r) => r._id === cancelPath[1]);
      if (!row) return this.json(route, 404, { detail: 'no such request' });
      if (row.status !== 'scheduled') return this.json(route, 409, { detail: 'only a scheduled order can be cancelled' });
      row.status = 'cancelled';
      const refunded = Number(row.amount_groth) + Number(row.fee_groth);
      this.balances.ETH.available += refunded;
      this.balances.ETH.scheduled -= refunded;
      return this.json(route, 200, { cancelled: row._id, refunded_groth: refunded });
    }
    return this.json(route, 404, { detail: `no mock for ${method} ${path}` });
  }
}

/**
 * Blocks every request that leaves 127.0.0.1 (RPCs, CoinGecko, fonts unless allowed). With
 * `allowAssets` the Google Fonts CSS is fetched for real and the token logos the token list points
 * at (LOGO_HOST) are served locally, so a screenshot shows the real <img> logos without depending
 * on anyone's CDN. Chain icons need nothing here: they are this app's own files under /chains/,
 * same origin, and always load.
 */
export async function blockExternal(page: Page, allowAssets = false) {
  await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, (route) => {
    const url = route.request().url();
    if (allowAssets && /fonts\.(googleapis|gstatic)\.com/.test(url)) return route.continue();
    if (allowAssets && url.startsWith(`${LOGO_HOST}/`) && url.endsWith('.svg')) {
      const hue = [...url].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
      return route.fulfill({
        status: 200,
        contentType: 'image/svg+xml',
        body: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="16" fill="hsl(${hue} 70% 52%)"/></svg>`,
      });
    }
    return route.abort('blockedbyclient');
  });
}

// The first FALLBACK_RPCS url of each mocked chain (src/lib/chains.ts): the scanner tries it first,
// so answering it is enough to make the chain readable.
const RPC_URLS: Record<number, string> = {
  1: 'https://ethereum-rpc.publicnode.com',
  42161: 'https://arb1.arbitrum.io/rpc',
  8453: 'https://mainnet.base.org',
  1514: 'https://mainnet.storyrpc.io',
  25: 'https://evm.cronos.org',
};

const BATCH_IFACE = new Interface([
  'function balanceFor(address[] _tokens, address _account) view returns (uint256[] balances, uint256[] decimals)',
]);
const MC3_IFACE = new Interface([
  'function aggregate3((address target, bool allowFailure, bytes callData)[] calls) payable returns ((bool success, bytes returnData)[] returnData)',
]);
const CODER = AbiCoder.defaultAbiCoder();

export interface ChainHoldings {
  native: bigint;
  tokens: Record<string, bigint>;
}

/**
 * A public-RPC double: eth_chainId / eth_getBalance / eth_call for the batch-balance helper,
 * Multicall3's aggregate3 and plain balanceOf. Install it AFTER blockExternal so it wins the route.
 */
export class MockRpc {
  constructor(public holdings: Record<number, ChainHoldings> = {}) {}

  async install(page: Page) {
    for (const [id, url] of Object.entries(RPC_URLS)) {
      await page.route(url, (route) => this.handle(Number(id), route));
    }
  }

  private handle(chainId: number, route: Route) {
    let body: unknown;
    try {
      body = JSON.parse(route.request().postData() ?? 'null');
    } catch {
      body = null;
    }
    const one = (r: { id?: unknown; method?: string; params?: unknown[] }) => {
      const id = r?.id ?? 1;
      try {
        return { jsonrpc: '2.0', id, result: this.answer(chainId, String(r?.method), (r?.params ?? []) as unknown[]) };
      } catch (e) {
        return { jsonrpc: '2.0', id, error: { code: -32000, message: (e as Error).message } };
      }
    };
    const out = Array.isArray(body) ? body.map(one) : one(body as never);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(out) });
  }

  private answer(chainId: number, method: string, params: unknown[]): unknown {
    const h = this.holdings[chainId] ?? { native: 0n, tokens: {} };
    const bal = (token: string) => h.tokens[token.toLowerCase()] ?? 0n;
    switch (method) {
      case 'eth_chainId':
        return '0x' + chainId.toString(16);
      case 'net_version':
        return String(chainId);
      case 'eth_blockNumber':
        return '0x10';
      case 'eth_getBalance':
        return '0x' + h.native.toString(16);
      case 'eth_call': {
        const tx = params[0] as { to?: string; data?: string };
        const data = String(tx?.data ?? '0x');
        const selector = data.slice(0, 10);
        if (selector === MC3_IFACE.getFunction('aggregate3')!.selector) {
          const [calls] = MC3_IFACE.decodeFunctionData('aggregate3', data);
          const items = (calls as unknown as [string, boolean, string][]).map((c) => [true, CODER.encode(['uint256'], [bal(c[0])])]);
          return MC3_IFACE.encodeFunctionResult('aggregate3', [items]);
        }
        if (selector === BATCH_IFACE.getFunction('balanceFor')!.selector) {
          const [tokens] = BATCH_IFACE.decodeFunctionData('balanceFor', data);
          const list = tokens as unknown as string[];
          return BATCH_IFACE.encodeFunctionResult('balanceFor', [
            list.map((t) => bal(t)),
            list.map((t) => BigInt(TOKENS.find((x) => x.address.toLowerCase() === t.toLowerCase())?.decimals ?? 18)),
          ]);
        }
        return CODER.encode(['uint256'], [bal(String(tx?.to ?? ''))]);
      }
      default:
        throw new Error(`mock rpc: unsupported ${method}`);
    }
  }
}

const USD = {
  ethereum: 4000,
  'story-2': 3.2,
  'crypto-com-chain': 0.12,
  [USDC.toLowerCase()]: 1,
  [DAI.toLowerCase()]: 1,
  [WBTC.toLowerCase()]: 100000,
} as Record<string, number>;

/** A wallet with something on every mocked chain, including the two with no batch-balance helper. */
export const DEMO_HOLDINGS: Record<number, ChainHoldings> = {
  1: {
    native: 250000000000000000n, // 0.25 ETH
    tokens: { [USDC.toLowerCase()]: 1834500000n, [DAI.toLowerCase()]: 402000000000000000000n, [WBTC.toLowerCase()]: 3400000n },
  },
  42161: { native: 120000000000000000n, tokens: { [USDC.toLowerCase()]: 640120000n } },
  8453: { native: 45000000000000000n, tokens: { [USDC.toLowerCase()]: 96000000n, [DAI.toLowerCase()]: 12500000000000000000n } },
  1514: { native: 1500000000000000000000n, tokens: { [USDC.toLowerCase()]: 25000000n } },
  25: { native: 8200000000000000000000n, tokens: { [USDC.toLowerCase()]: 310000000n, [DAI.toLowerCase()]: 88000000000000000000n } },
};

/** CoinGecko double so the chips carry USD labels offline. Install after blockExternal. */
export async function installMockPrices(page: Page) {
  await page.route(/api\.coingecko\.com/, (route) => {
    const url = new URL(route.request().url());
    const keys = (url.searchParams.get('ids') ?? url.searchParams.get('contract_addresses') ?? '').split(',').filter(Boolean);
    const body: Record<string, { usd: number }> = {};
    for (const k of keys) if (USD[k.toLowerCase()] !== undefined) body[k] = { usd: USD[k.toLowerCase()] };
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

// ---------------------------------------------------------------------------
// Injected wallets: one factory, one `profile` per real wallet
// ---------------------------------------------------------------------------
//
// Every wallet Pgas.me is meant to work in is a different EIP-1193 implementation, and the ways
// they differ are not cosmetic — they are what breaks a dapp. So the mock is not "a wallet": it is
// a profile carrying a real wallet's identity (its EIP-6963 name/rdns/icon, or the `window.*`
// global it sets and the `is…` flag it raises) and the behaviours that wallet is known for.
//
// The quirks below are each a thing a shipped wallet does, not an invented edge case:
//   lowercase           — the accounts come back unchecksummed (Trust, several in-app browsers)
//   utf8Sign            — personal_sign refuses the EIP-191 hex message with -32602 (Coin98, Binance)
//   reversedSignParams  — personal_sign wants [address, message] (in-app browsers)
//   unknownChain        — wallet_switchEthereumChain answers 4902 until the chain has been added
//   strictChainString   — the chainId parameter is looked up as TEXT, never parsed (2026-09-10)
//   rejectSwitch        — the user presses Cancel: 4001
//   emptyAccounts       — a locked wallet answers eth_requestAccounts with [] instead of throwing
//   zeroBalance         — the wallet's own RPC answers 0 for a balance that is not 0 (Zerion)
//   spuriousDisconnect  — `disconnect` is emitted for an RPC hiccup, with the account still there

export type WalletQuirk =
  | 'lowercase'
  | 'utf8Sign'
  | 'reversedSignParams'
  | 'unknownChain'
  | 'strictChainString'
  | 'rejectSwitch'
  | 'emptyAccounts'
  | 'zeroBalance'
  | 'spuriousDisconnect';

export interface MockWalletProfile {
  /** The handle name: `window.__wallets[key]`. */
  key: string;
  /** The name the wallet gives itself — EIP-6963 `info.name`, or the one our flag detection prints. */
  name: string;
  /** Announce over EIP-6963 under this rdns. Absent → legacy detection only. */
  rdns?: string;
  /** The icon's fill, so the picker screenshot has real, distinguishable tiles. */
  color?: string;
  /** `isMetaMask`, `isTrust`, … exactly as the wallet raises them. */
  flags?: Record<string, boolean>;
  /** Dotted path under `window`: 'ethereum', 'coin98.provider', 'bitkeep.ethereum', 'BinanceChain'. */
  global?: string;
  /** Goes into `window.ethereum.providers[]` — the MetaMask/Coinbase coexistence array. */
  inProviders?: boolean;
  accounts?: string[];
  chainId?: number;
  /**
   * The chains this wallet HOLDS, the way a real install holds a list. Absent means "every chain
   * the app can ask for", which is what every profile written before 2026-09-10 assumed; give it a
   * list to exercise a chain the wallet genuinely does not have (→ 4902 → wallet_addEthereumChain).
   */
  knownChains?: number[];
  quirks?: WalletQuirk[];
  /** Announce this late (ms). 0/undefined = with the first `eip6963:requestProvider`. */
  announceDelayMs?: number;
}

/** The default pair the rest of the suite drives: an announced wallet, a second one, a legacy Coin98. */
export const DEFAULT_PROFILES: MockWalletProfile[] = [
  { key: 'mock', name: 'Mock Wallet', rdns: 'me.pgas.mock', color: '#0E8F86', flags: { isMetaMask: true }, global: 'ethereum' },
  { key: 'mock2', name: 'Mock Wallet 2', rdns: 'me.pgas.mock2', color: '#4A63E7', flags: { isMetaMask: true }, accounts: ['B'] },
  { key: 'coin98legacy', name: 'Coin98', color: '#D9B432', flags: { isCoin98: true }, global: 'coin98.provider' },
];

/**
 * The eight wallets that announce themselves over EIP-6963, with the rdns each really publishes.
 * Quirks are assigned to the wallet that actually has them, so a per-wallet journey exercises the
 * handling that wallet needs rather than a generic happy path twelve times.
 */
export const ANNOUNCED_PROFILES: MockWalletProfile[] = [
  { key: 'metamask', name: 'MetaMask', rdns: 'io.metamask', color: '#F6851B', flags: { isMetaMask: true }, inProviders: true },
  { key: 'zerion', name: 'Zerion', rdns: 'io.zerion.wallet', color: '#2962EF', flags: { isZerion: true }, quirks: ['zeroBalance'] },
  { key: 'rabby', name: 'Rabby Wallet', rdns: 'io.rabby', color: '#7084FF', flags: { isRabby: true }, quirks: ['unknownChain'] },
  {
    key: 'trust',
    name: 'Trust Wallet',
    rdns: 'com.trustwallet.app',
    color: '#0500FF',
    flags: { isTrust: true, isTrustWallet: true },
    quirks: ['lowercase'],
  },
  {
    key: 'okx',
    name: 'OKX Wallet',
    rdns: 'com.okex.wallet',
    color: '#161A22',
    flags: { isOkxWallet: true },
    quirks: ['spuriousDisconnect'],
  },
  {
    key: 'coinbase',
    name: 'Coinbase Wallet',
    rdns: 'com.coinbase.wallet',
    color: '#0052FF',
    flags: { isCoinbaseWallet: true },
    inProviders: true,
  },
  { key: 'phantom', name: 'Phantom', rdns: 'app.phantom', color: '#AB9FF2', flags: { isPhantom: true }, global: 'phantom.ethereum' },
  {
    key: 'coin98',
    name: 'Coin98 Wallet',
    rdns: 'coin98.com',
    color: '#D9B432',
    flags: { isCoin98: true },
    quirks: ['utf8Sign'],
  },
];

/**
 * The wallets that never announce: they are found only by the global they set. Coin98 is here AND
 * in the announced list on purpose — it does both, and one install must still be one row.
 */
export const LEGACY_PROFILES: MockWalletProfile[] = [
  { key: 'coin98global', name: 'Coin98', color: '#D9B432', flags: { isCoin98: true }, global: 'coin98.provider', quirks: ['utf8Sign'] },
  {
    key: 'binance',
    name: 'Binance Web3 Wallet',
    color: '#F0B90B',
    flags: { isBinance: true },
    global: 'BinanceChain',
    quirks: ['utf8Sign'],
  },
  { key: 'bitget', name: 'Bitget Wallet', color: '#1DA2B4', flags: { isBitKeep: true, isBitget: true }, global: 'bitkeep.ethereum' },
  {
    key: 'tokenpocket',
    name: 'TokenPocket',
    color: '#2980FE',
    flags: { isTokenPocket: true, isMetaMask: true },
    inProviders: true,
    quirks: ['reversedSignParams'],
  },
];

/** Everything at once: what a browser with every one of these installed would look like. */
export const ALL_PROFILES: MockWalletProfile[] = [...ANNOUNCED_PROFILES, ...LEGACY_PROFILES];

/**
 * The wallet from the 2026-09-10 bug report — "When I click Deposit, you ask me to Add ETH Network,
 * but I have it." — deliberately NOT in ALL_PROFILES: it is a behaviour family, not a twelfth
 * install, and the picker tests count installs.
 *
 * WHICH shipped wallet it was is not known (the admin was on a real wallet on pgas.me and said what
 * it did, not what it was). What IS known is the behaviour, and the behaviour is the thing that
 * breaks a dapp: `wallet_switchEthereumChain` is answered by comparing the chainId string it was
 * handed against the ones it holds, with no EIP-3326 validation. So a padded "0x01" is not refused
 * as invalid — it is simply not found, the wallet answers 4902, and the dapp's own "your wallet does
 * not know this chain" branch offers to add Ethereum mainnet to a wallet that has always had it.
 * It starts on BNB Smart Chain so a switch to Ethereum is actually asked for.
 */
export const STRICT_CHAIN_PROFILE: MockWalletProfile = {
  key: 'strictchain',
  name: 'String-Compare Wallet',
  rdns: 'me.pgas.strictchain',
  color: '#8A5CF6',
  flags: { isMetaMask: true },
  global: 'ethereum',
  chainId: 56,
  knownChains: [1, 56, 42161, 8453],
  quirks: ['strictChainString'],
};

/**
 * Installs one EIP-1193 provider per profile. Each gets a handle at `window.__wallets[key]`:
 *   .state       { accounts, chainId, sent[], signCalls[][], signed, switchCalls[], addCalls[],
 *                  addedChains[], addedChainStrings[], locked }
 *   .setAccounts(list)  → emits accountsChanged
 *   .setChain(id)       → emits chainChanged (the wallet doing it, not us asking)
 *   .hiccup()           → emits `disconnect` with the account still connected
 *   .unlock()           → a locked wallet grants its account
 */
export async function installWallets(page: Page, profiles: MockWalletProfile[] = DEFAULT_PROFILES, wallets: Wallet[] = [walletA, walletB]) {
  const byAddress = new Map(wallets.map((w) => [w.address.toLowerCase(), w]));
  await page.exposeFunction('__mockSign', async (hexMessage: string, address: string) => {
    const w = byAddress.get(String(address).toLowerCase());
    if (!w) throw new Error(`no key for ${address}`);
    return w.signMessage(getBytes(hexMessage));
  });
  const specs = profiles.map((p) => ({
    ...p,
    accounts: (p.accounts ?? ['A']).map((a) => (a === 'A' ? wallets[0].address : a === 'B' ? wallets[1].address : a)),
    chainId: p.chainId ?? 1,
    quirks: p.quirks ?? [],
    flags: p.flags ?? {},
    color: p.color ?? '#0E8F86',
  }));
  await page.addInitScript(
    ({ specs, tx }) => {
      const w = window as any;
      const handles: Record<string, any> = {};
      w.__wallets = handles;
      const enc = new TextEncoder();
      const toHex = (v: unknown) =>
        typeof v === 'string' && /^0x[0-9a-fA-F]*$/.test(v) && v.length % 2 === 0
          ? v
          : '0x' +
            Array.from(enc.encode(String(v)))
              .map((b) => b.toString(16).padStart(2, '0'))
              .join('');
      const rpcErr = (code: number, message: string) => Object.assign(new Error(message), { code });

      const make = (spec: any) => {
        const listeners: Record<string, Array<(...a: unknown[]) => void>> = {};
        const q = (name: string) => spec.quirks.includes(name);
        const state = {
          accounts: spec.accounts as string[],
          chainId: spec.chainId as number,
          sent: [] as any[],
          signCalls: [] as any[][],
          signed: 0,
          addedChains: [] as number[],
          /** The chainId STRINGS the adds arrived with — what a string-comparing wallet remembers. */
          addedChainStrings: [] as string[],
          addCalls: [] as any[],
          switchCalls: [] as any[],
          locked: q('emptyAccounts'),
        };
        const emit = (ev: string, ...args: unknown[]) => (listeners[ev] ?? []).forEach((l) => l(...args));
        const accountsOut = () => (state.locked ? [] : q('lowercase') ? state.accounts.map((a) => a.toLowerCase()) : state.accounts);

        // ---- the chain id on the wire (2026-09-10) ----
        // EIP-695, EIP-3085 and EIP-3326 all specify ONE form: 0x-prefixed, unpadded, non-zero hex.
        // This is MetaMask's own test for it, verbatim. Until 2026-09-10 this mock read the parameter
        // with a bare parseInt(), which accepts "0x01" happily — so it shipped a real bug: the app
        // sent ethers' toBeHex(1) = "0x01", the admin's wallet did not recognise it, and the app
        // offered to ADD Ethereum to a wallet that already had it. A mock that parses what real
        // wallets validate is a mock that certifies the bug.
        const CANONICAL_CHAIN_ID = /^0x[1-9a-f]+[0-9a-f]*$/i;
        const chainHex = (n: number) => '0x' + n.toString(16);
        /** MetaMask's wording, because 'unpadded' is the word an operator will grep for. */
        const badChainId = (raw: unknown) =>
          rpcErr(-32602, `Expected 0x-prefixed, unpadded, non-zero hexadecimal string 'chainId'. Received:\n${String(raw)}`);
        /** The validating family: parse the id, then look it up among the chains this wallet holds. */
        const knowsNumber = (id: number) => {
          if (state.addedChains.includes(id)) return true;
          if (q('unknownChain')) return false; // knows nothing until the dapp adds it
          return spec.knownChains ? spec.knownChains.includes(id) : true;
        };
        /**
         * The string-comparing family — the one the admin was on. The parameter is never parsed:
         * it is matched as text against the ids this wallet holds, so a padded "0x01" is not
         * "invalid", it is simply *not found*, and the wallet answers 4902 exactly as it would for
         * a chain it has never heard of. That is the whole shape of the 2026-09-10 bug.
         */
        const knowsString = (raw: string) => {
          if (state.addedChainStrings.includes(raw)) return true;
          if (q('unknownChain')) return false;
          return spec.knownChains ? spec.knownChains.map((n: number) => chainHex(n)).includes(raw) : CANONICAL_CHAIN_ID.test(raw);
        };

        const provider: any = {
          ...spec.flags,
          async request({ method, params }: { method: string; params?: any[] }) {
            switch (method) {
              case 'eth_requestAccounts':
              case 'eth_accounts':
                return accountsOut();
              case 'eth_chainId':
                return chainHex(state.chainId);
              case 'net_version':
                return String(state.chainId);
              case 'wallet_switchEthereumChain': {
                const raw = String(params?.[0]?.chainId);
                state.switchCalls.push(params?.[0]);
                if (q('rejectSwitch')) throw rpcErr(4001, 'User rejected the request.');
                const strict = q('strictChainString');
                // The validating family never gets as far as the lookup: a padded id is bad params.
                if (!strict && !CANONICAL_CHAIN_ID.test(raw)) throw badChainId(raw);
                if (!(strict ? knowsString(raw) : knowsNumber(parseInt(raw, 16))))
                  throw rpcErr(4902, 'Unrecognized chain ID. Try adding the chain using wallet_addEthereumChain first.');
                state.chainId = parseInt(raw, 16);
                emit('chainChanged', chainHex(state.chainId));
                return null;
              }
              case 'wallet_addEthereumChain': {
                // MetaMask-family behaviour: the chain is added, the switch is a second call
                const raw = String(params?.[0]?.chainId);
                state.addCalls.push(params?.[0]);
                if (!q('strictChainString') && !CANONICAL_CHAIN_ID.test(raw)) throw badChainId(raw);
                state.addedChains.push(parseInt(raw, 16));
                // …and the string-comparing wallet remembers the TEXT it was given, so a dapp that
                // added chain 1 as "0x01" can then switch to "0x01" — the prompt was the whole cost.
                state.addedChainStrings.push(raw);
                return null;
              }
              case 'eth_getBalance':
                return '0x0'; // zeroBalance: what Zerion answered for a wallet that was not empty
              case 'eth_blockNumber':
                return '0x10';
              case 'eth_estimateGas':
                return '0x5208';
              case 'eth_getTransactionReceipt':
                return {
                  transactionHash: params?.[0],
                  transactionIndex: '0x0',
                  blockHash: '0x' + 'cd'.repeat(32),
                  blockNumber: '0xf',
                  from: state.accounts[0],
                  to: '0xeF4fB24aD0916217251F553c0596F8Edc630EB66',
                  cumulativeGasUsed: '0x5208',
                  gasUsed: '0x5208',
                  effectiveGasPrice: '0x3b9aca00',
                  gasPrice: '0x3b9aca00',
                  contractAddress: null,
                  logs: [],
                  logsBloom: '0x' + '00'.repeat(256),
                  status: '0x1',
                  type: '0x2',
                };
              case 'eth_call':
                throw rpcErr(-32000, 'execution reverted');
              case 'personal_sign': {
                const [a, b] = params ?? [];
                state.signCalls.push([a, b]);
                const isAddr = (v: unknown) => typeof v === 'string' && /^0x[0-9a-fA-F]{40}$/.test(v);
                if (q('reversedSignParams')) {
                  if (!isAddr(a)) throw rpcErr(-32602, 'Invalid parameters: the address must come first.');
                  state.signed++;
                  return w.__mockSign(toHex(b), a);
                }
                const looksHex = typeof a === 'string' && /^0x[0-9a-fA-F]+$/.test(a) && !isAddr(a);
                if (q('utf8Sign')) {
                  if (looksHex) throw rpcErr(-32602, 'invalid params: message must be a utf8 string');
                  state.signed++;
                  return w.__mockSign(toHex(a), b);
                }
                state.signed++;
                return w.__mockSign(toHex(a), b);
              }
              case 'eth_sendTransaction':
                state.sent.push(params?.[0]);
                return tx;
              default:
                throw rpcErr(4200, `mock: unsupported ${method}`);
            }
          },
          on(ev: string, l: (...a: unknown[]) => void) {
            (listeners[ev] = listeners[ev] ?? []).push(l);
          },
          removeListener(ev: string, l: (...a: unknown[]) => void) {
            listeners[ev] = (listeners[ev] ?? []).filter((x) => x !== l);
          },
        };
        return {
          provider,
          state,
          spec,
          setAccounts(list: string[]) {
            state.accounts = list;
            state.locked = false;
            emit('accountsChanged', q('lowercase') ? list.map((a) => a.toLowerCase()) : list);
          },
          setChain(id: number) {
            state.chainId = id;
            emit('chainChanged', chainHex(id));
          },
          /** An RPC hiccup: the wallet says `disconnect` and stays connected. */
          hiccup() {
            emit('disconnect', { code: 1013, message: 'network connection lost' });
          },
          /** The wallet was locked and the user unlocked it. */
          unlock() {
            state.locked = false;
          },
          /** The user locked the wallet: it still answers, with no accounts. */
          lock() {
            state.locked = true;
          },
        };
      };

      const setPath = (path: string, value: unknown) => {
        const parts = path.split('.');
        let obj: any = w;
        for (const part of parts.slice(0, -1)) obj = obj[part] = obj[part] ?? {};
        obj[parts[parts.length - 1]] = value;
      };

      const icon = (fill: string) =>
        'data:image/svg+xml;utf8,' +
        encodeURIComponent(
          `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="10" fill="${fill}"/></svg>`,
        );

      const inProviders: any[] = [];
      for (const spec of specs) {
        const h = make(spec);
        handles[spec.key] = h;
        if (spec.global) setPath(spec.global, h.provider);
        if (spec.inProviders) inProviders.push(h.provider);
      }
      if (inProviders.length) {
        // several extensions sharing one page: window.ethereum is one of them and lists them all
        const head = inProviders[0];
        head.providers = inProviders;
        w.ethereum = head;
      }
      w.__mock = handles[specs[0].key]; // the suite's default handle

      const announceOne = (spec: any) => {
        const h = handles[spec.key];
        window.dispatchEvent(
          new CustomEvent('eip6963:announceProvider', {
            detail: Object.freeze({
              info: { uuid: `mock-uuid-${spec.key}`, name: spec.name, icon: icon(spec.color), rdns: spec.rdns },
              provider: h.provider,
            }),
          }),
        );
      };
      const announce = () => {
        for (const spec of specs) {
          if (!spec.rdns) continue;
          if (spec.announceDelayMs) setTimeout(() => announceOne(spec), spec.announceDelayMs);
          else announceOne(spec);
        }
      };
      window.addEventListener('eip6963:requestProvider', announce);
      announce();
    },
    { specs, tx: TX },
  );
}

/** Injects `window.ethereum` (isMetaMask) that also announces itself over EIP-6963. */
export async function installMockWallet(page: Page, wallets: Wallet[] = [walletA, walletB]) {
  await installWallets(page, DEFAULT_PROFILES, wallets);
}

export const MOCK_TX = TX;

/**
 * Connect the mock wallet. Sign-In-with-Ethereum then happens BY ITSELF (2026-09-09): the app asks
 * for the signature the moment a wallet connects, so the only thing to wait for is the account
 * pill saying the session landed.
 */
export async function connectAndSignIn(page: Page) {
  await page.getByRole('button', { name: 'Connect wallet' }).first().click();
  await page.locator('[data-wallet-id="6963:me.pgas.mock"]').click();
  await signedIn(page).waitFor();
}

/** The header pill once the signature has come back — the one "you are signed in" marker. */
export function signedIn(page: Page) {
  return page.locator('[data-testid="connected-address"][data-signed-in="yes"]');
}

/** Click a tab, whichever of the two navigations is on screen (header on desktop, bar on a phone). */
export async function goTab(page: Page, tab: 'deposit' | 'balance' | 'schedule') {
  await page.locator(`[data-tab="${tab}"]:visible`).first().click();
}

/**
 * Drive the combined "Pay with" picker: the chain row inside it, then the token. Leaving `token`
 * out picks the chain only and closes the picker again.
 */
export async function payWith(page: Page, opts: { chainId?: number; token?: string | RegExp }) {
  await page.getByTestId('pay-with').click();
  if (opts.chainId !== undefined) await page.locator(`[data-chain="${opts.chainId}"]`).click();
  if (opts.token !== undefined) {
    const q = typeof opts.token === 'string' ? opts.token : opts.token.source.replace(/[^A-Za-z0-9]/g, '');
    await page.getByLabel('Search tokens').fill(q);
    await page
      .getByRole('option', { name: typeof opts.token === 'string' ? new RegExp(opts.token, 'i') : opts.token })
      .first()
      .click();
  } else {
    await page.getByTestId('pay-with').click(); // the button toggles the popover shut
  }
}
