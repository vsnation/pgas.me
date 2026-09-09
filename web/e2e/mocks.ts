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

/** deBridge's icon CDN, served locally by `blockExternal(page, true)` so screenshots stay offline. */
const CHAIN_ICON = (name: string) => `https://app.debridge.com/assets/images/chain/${name}.svg`;

export const walletA = new Wallet('0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d');
export const walletB = new Wallet('0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a');

export interface ApiCall {
  method: string;
  path: string;
  body: unknown;
  auth: string | null;
}

interface Destination {
  address: string;
  kind: string;
  verified_at: number;
  label: string;
  created_at: number;
}

// Five EVM chains (Story and Cronos have no batch-balance helper, so the scanner uses Multicall3
// there) plus deBridge's two non-EVM chains, which must render as "not scanned", never as errors.
const CHAINS = [
  { chain_id: 1, dln_chain_id: 1, name: 'Ethereum', native_symbol: 'ETH' },
  { chain_id: 42161, dln_chain_id: 42161, name: 'Arbitrum', native_symbol: 'ETH' },
  { chain_id: 8453, dln_chain_id: 8453, name: 'Base', native_symbol: 'ETH' },
  { chain_id: 1514, dln_chain_id: 100000013, name: 'Story', native_symbol: 'IP' },
  { chain_id: 25, dln_chain_id: 100000019, name: 'Cronos', native_symbol: 'CRO' },
  { chain_id: 7565164, dln_chain_id: 7565164, name: 'Solana', native_symbol: 'SOL' },
  { chain_id: 728126428, dln_chain_id: 100000026, name: 'Tron', native_symbol: 'TRX' },
];

export const DAI = '0x6B175474E89094C44Da98b954EedeAC495271d0F';
export const WBTC = '0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599';

const TOKENS = [
  { address: NATIVE, symbol: 'ETH', name: 'Ether', decimals: 18, logo: '' },
  { address: USDC, symbol: 'USDC', name: 'USD Coin', decimals: 6, logo: CHAIN_ICON('usdc') },
  { address: DAI, symbol: 'DAI', name: 'Dai', decimals: 18, logo: '' },
  { address: WBTC, symbol: 'WBTC', name: 'Wrapped BTC', decimals: 8, logo: '' },
];

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
const DLN_ORDER = '0xeF4fB24aD0916217251F553c0596F8Edc630EB66';
const DLN_ALLOWANCE_TARGET = '0x6A000F20005980200259B80c5102003040001068';

export class MockApi {
  armed = false;
  directEnabled = true;
  instantEnabled = false;
  calls: ApiCall[] = [];
  token: string | null = null;
  accountId = 'acct-' + walletA.address.slice(2, 10).toLowerCase();
  nonces = new Set<string>();
  destNonces = new Set<string>();
  destinations: Destination[] = [];
  deposits: Record<string, unknown>[] = [
    {
      _id: 'dep-1',
      asset: 'ETH',
      status: 'credited',
      src: { chain_id: 1, token: NATIVE, amount: '100000000000000000' },
      quote_id: 'q-1',
      src_tx_hash: '0x' + '11'.repeat(32),
      order_id: '0x' + '22'.repeat(32),
      eth: { tx: '0x' + '33'.repeat(32), block: 21000000, msg_id: 4821, value_units: '98000000000000000' },
      value_groth: 9800000,
      created_at: 1757400000,
      updated_at: 1757400900,
    },
    {
      _id: 'dep-2',
      asset: 'ETH',
      status: 'confirming',
      src: { chain_id: 42161, token: NATIVE, amount: '50000000000000000' },
      quote_id: 'q-2',
      src_tx_hash: '0x' + '44'.repeat(32),
      eth: { tx: '0x' + '55'.repeat(32), block: 21000100 },
      created_at: 1757403600,
      updated_at: 1757403700,
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
      window_s: 3600,
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
      window_s: 600,
      release_at: 1757390000,
      status: 'sent',
      beam_txid: 'a1b2c3d4e5f6a1b2c3d4e5f6',
      msg_id: 4830,
      eth_tx: '0x' + '66'.repeat(32),
      created_at: 1757389000,
    },
  ];
  balances = { ETH: { available: 50000000, scheduled: 10200000, sent: 1000000, pending: 4900000 } };

  constructor() {
    this.destinations.push({ address: walletA.address, kind: 'connected', verified_at: 1757300000, label: '', created_at: 1757300000 });
  }

  private json(route: Route, status: number, body: unknown) {
    return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  }

  private account() {
    return {
      address: walletA.address,
      account_id: this.accountId,
      balances: this.balances,
      fee_bps: 200,
      denominations: [1000000, 10000000],
      min_payout_groth: 1000000,
      modes: { direct: this.directEnabled, instant: this.instantEnabled },
      ingress: { armed: this.armed, near: false },
      deposits: this.deposits,
      requests: this.requests,
      destinations: this.destinations.length,
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
    if (method === 'GET' && path === '/dex/tokens') return this.json(route, 200, { tokens: TOKENS });
    if (method === 'GET' && path === '/assets') return this.json(route, 200, { assets: ASSETS });
    if (method === 'GET' && path === '/stats') {
      return this.json(route, 200, {
        deposits_24h: 3,
        deposits_7d: 21,
        payouts_24h: 2,
        pool: { shielded_outputs_total: 64213, shielded_outputs_per_24h: 412, height: 2534000, at: Math.floor(Date.now() / 1000) - 60 },
        float: { ETH: { active_distributors: 1, wei: '500000000000000000' } },
        armed: { ingress: this.armed, direct: this.directEnabled, instant: this.instantEnabled },
      });
    }

    if (!locked()) return;

    if (method === 'GET' && path === '/account') return this.json(route, 200, this.account());
    if (method === 'GET' && path === '/destinations') return this.json(route, 200, { destinations: this.destinations });
    if (method === 'GET' && path === '/destinations/nonce') {
      const nonce = 'd' + Math.random().toString(36).slice(2, 12);
      this.destNonces.add(nonce);
      return this.json(route, 200, {
        nonce,
        template: `Pgas.me destination\naccount: ${this.accountId}\naddress: <address>\nnonce: ${nonce}\nissued: <issued>`,
      });
    }
    if (method === 'POST' && path === '/destinations') {
      if (!['proven', 'generated'].includes(b.kind)) return this.json(route, 400, { detail: 'kind must be proven or generated' });
      if (!this.destNonces.has(b.nonce)) return this.json(route, 400, { detail: 'unknown or expired nonce — request a new one' });
      const address = getAddress(String(b.address));
      if (address !== b.address) return this.json(route, 400, { detail: 'address is not EIP-55' });
      if (!/^\d{4}-\d{2}-\d{2}T/.test(String(b.issued))) return this.json(route, 400, { detail: 'issued is not ISO-8601' });
      const msg = `Pgas.me destination\naccount: ${this.accountId}\naddress: ${address}\nnonce: ${b.nonce}\nissued: ${b.issued}`;
      let signer: string;
      try {
        signer = verifyMessage(msg, String(b.signature));
      } catch {
        return this.json(route, 400, { detail: 'signature unreadable' });
      }
      if (signer.toLowerCase() !== address.toLowerCase())
        return this.json(route, 401, { detail: 'the signature was not made by that address' });
      this.destNonces.delete(b.nonce);
      const now = Math.floor(Date.now() / 1000);
      const row = { address, kind: String(b.kind), verified_at: now, label: String(b.label ?? ''), created_at: now };
      this.destinations = this.destinations.filter((d) => d.address !== address).concat(row);
      return this.json(route, 200, { address, kind: row.kind, verified_at: now, label: row.label });
    }
    const del = /^\/destinations\/(0x[0-9a-fA-F]{40})$/.exec(path);
    if (method === 'DELETE' && del) {
      const address = getAddress(del[1]);
      if (this.requests.some((r) => r.W === address && ['scheduled', 'bridging'].includes(String(r.status))))
        return this.json(route, 409, { detail: 'a scheduled payout still targets this wallet' });
      if (address === walletA.address) return this.json(route, 409, { detail: 'the signed-in wallet cannot be removed' });
      this.destinations = this.destinations.filter((d) => d.address !== address);
      return this.json(route, 200, { removed: address });
    }
    if (method === 'POST' && path === '/quote') {
      // API_CONTRACT.md "Quote modes": off Ethereum it is a DLN order; on Ethereum the source token
      // either IS the target asset (direct to the pipe) or has to be swapped in the user's own wallet.
      const amount = BigInt(String(b.amount));
      const target = ASSETS.find((a) => a.key === b.target_asset) ?? ASSETS[0];
      const src = TOKENS.find((t) => t.address.toLowerCase() === String(b.src_token).toLowerCase()) ?? {
        address: String(b.src_token),
        symbol: 'UNKNOWN',
        name: 'Unknown',
        decimals: 18,
        logo: '',
      };
      const isTarget = src.address.toLowerCase() === target.token.toLowerCase();
      const mode = Number(b.src_chain_id) !== 1 ? 'dln' : isTarget ? 'direct' : 'swap';
      const native = src.address.toLowerCase() === NATIVE;
      // a crude but deterministic conversion into the target asset's units
      const usdOf = (v: bigint, dec: number) => (Number(v) / 10 ** dec) * (native ? 4000 : src.symbol === 'WBTC' ? 100000 : 1);
      const toTarget = (v: bigint) => {
        const usd = usdOf(v, src.decimals);
        const price = target.key === 'ETH' ? 4000 : target.key === 'WBTC' ? 100000 : 1;
        return BigInt(Math.round((usd / price) * 10 ** target.decimals));
      };
      const outUnits = mode === 'direct' ? amount : ((isTarget ? amount : toTarget(amount)) * 98n) / 100n;
      const relayerFee = outUnits / 1000n;
      const estimate: Record<string, unknown> = {
        src: { chain_id: b.src_chain_id, token: src.address, symbol: src.symbol, decimals: src.decimals, amount: String(b.amount) },
        out_units: outUnits.toString(),
        out_groth: Number((outUnits * 100000000n) / 10n ** BigInt(target.decimals)),
        usd: usdOf(amount, src.decimals),
        eta_s: mode === 'direct' ? 12 * 12 + 120 : 420,
      };
      const quote: Record<string, unknown> = {
        quote_id: 'q-' + Math.random().toString(36).slice(2, 8),
        target_asset: target.key,
        mode,
        armed: this.armed,
        expires_at: new Date(Date.now() + 30_000).toISOString(),
        estimate,
      };
      if (mode === 'swap') {
        // the swap lands in the user's OWN wallet, so it is issued armed or not; never a deposit
        estimate.dln_fees = { protocolFee: '1000000000000000' };
        quote.swap_tx = { chain_id: 1, to: DLN_ORDER, data: '0xfeedface', value: native ? String(amount) : '0' };
        if (!native) quote.approval = { chain_id: 1, token: src.address, spender: DLN_ALLOWANCE_TARGET, amount: String(amount) };
        quote.next = { src_chain_id: 1, src_token: target.token, amount: outUnits.toString() };
        return this.json(route, 200, quote);
      }
      estimate.value_units = (outUnits - relayerFee).toString();
      estimate.relayer_fee_units = relayerFee.toString();
      if (mode === 'dln') {
        estimate.dln_fees = { protocolFee: '1000000000000000' };
        quote.order_id = '0x' + '77'.repeat(32);
      }
      if (this.armed) {
        const to = mode === 'direct' ? target.pipe : DLN_ORDER;
        quote.tx = { chain_id: mode === 'direct' ? 1 : b.src_chain_id, to, data: '0xdeadbeef', value: native ? String(amount) : '0' };
        if (!native)
          quote.approval = { chain_id: mode === 'direct' ? 1 : b.src_chain_id, token: src.address, spender: to, amount: String(amount) };
      } else quote.note = 'ingress not armed: no Beam pubkey configured';
      return this.json(route, 200, quote);
    }
    if (method === 'POST' && path === '/deposits') {
      const id = 'dep-' + (this.deposits.length + 1);
      this.deposits.unshift({
        _id: id,
        asset: 'ETH',
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
    if (method === 'POST' && path === '/withdrawals') {
      if ((b.mode === 'direct' && !this.directEnabled) || (b.mode === 'instant' && !this.instantEnabled))
        return this.json(route, 409, { detail: `${b.mode} mode is not enabled yet` });
      const items = (b.items ?? []) as { W: string; amount_groth: number }[];
      for (const it of items) {
        if (!this.destinations.some((d) => d.address === it.W))
          return this.json(route, 400, { detail: `${it.W} is not a registered destination` });
        if (it.amount_groth < 1000000) return this.json(route, 400, { detail: 'below min_payout_groth' });
      }
      const total = items.reduce((s, i) => s + i.amount_groth, 0);
      const fee = Math.ceil(total * 0.02);
      if (total + fee > this.balances.ETH.available) return this.json(route, 400, { detail: 'insufficient available balance' });
      const ids = items.map((_, i) => `req-new-${i + 1}`);
      return this.json(route, 200, {
        request_ids: ids,
        fee_groth: fee,
        total_debited_groth: total + fee,
        eta: { min_s: 3600, max_s: 64800 },
        privacy_grade: 'weak',
      });
    }
    return this.json(route, 404, { detail: `no mock for ${method} ${path}` });
  }
}

/**
 * Blocks every request that leaves 127.0.0.1 (RPCs, CoinGecko, fonts unless allowed). With
 * `allowAssets` the Google Fonts CSS is fetched for real and deBridge's chain/token icons are served
 * locally, so a screenshot shows the badges without depending on a CDN.
 */
export async function blockExternal(page: Page, allowAssets = false) {
  await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, (route) => {
    const url = route.request().url();
    if (allowAssets && /fonts\.(googleapis|gstatic)\.com/.test(url)) return route.continue();
    if (allowAssets && /app\.debridge\.com\/assets\/images\/.+\.svg$/.test(url)) {
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

/** Injects `window.ethereum` (isMetaMask) that also announces itself over EIP-6963. */
export async function installMockWallet(page: Page, wallets: Wallet[] = [walletA, walletB]) {
  const byAddress = new Map(wallets.map((w) => [w.address.toLowerCase(), w]));
  await page.exposeFunction('__mockSign', async (hexMessage: string, address: string) => {
    const w = byAddress.get(String(address).toLowerCase());
    if (!w) throw new Error(`no key for ${address}`);
    return w.signMessage(getBytes(hexMessage));
  });
  await page.addInitScript(
    ({ accountsA, accountsB, tx }) => {
      // one provider factory, three instances: the announced main wallet (also window.ethereum),
      // a second announced wallet, and a legacy-only Coin98 global
      const make = (accounts: string[]) => {
        const listeners: Record<string, Array<(...a: unknown[]) => void>> = {};
        const state = { accounts, chainId: 1, sent: [] as unknown[] };
        const emit = (ev: string, ...args: unknown[]) => (listeners[ev] ?? []).forEach((l) => l(...args));
        const provider = {
          isMetaMask: true,
          async request({ method, params }: { method: string; params?: any[] }) {
            switch (method) {
              case 'eth_requestAccounts':
              case 'eth_accounts':
                return state.accounts;
              case 'eth_chainId':
                return '0x' + state.chainId.toString(16);
              case 'net_version':
                return String(state.chainId);
              case 'wallet_switchEthereumChain':
                state.chainId = parseInt(params?.[0]?.chainId, 16);
                emit('chainChanged', '0x' + state.chainId.toString(16));
                return null;
              case 'wallet_addEthereumChain':
                return null;
              case 'eth_getBalance':
                return '0x0';
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
                throw Object.assign(new Error('execution reverted'), { code: -32000 });
              case 'personal_sign':
                return (window as any).__mockSign(params?.[0], params?.[1]);
              case 'eth_sendTransaction':
                state.sent.push(params?.[0]);
                return tx;
              default:
                throw Object.assign(new Error(`mock: unsupported ${method}`), { code: 4200 });
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
          setAccounts(list: string[]) {
            state.accounts = list;
            emit('accountsChanged', list);
          },
        };
      };
      const main = make(accountsA);
      const second = make(accountsB);
      const coin98 = make(accountsA);
      (window as any).ethereum = main.provider;
      (window as any).coin98 = { provider: coin98.provider };
      (window as any).__mock = main;
      const icon = (fill: string) =>
        'data:image/svg+xml;utf8,' +
        encodeURIComponent(
          `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="10" fill="${fill}"/></svg>`,
        );
      const announce = () => {
        window.dispatchEvent(
          new CustomEvent('eip6963:announceProvider', {
            detail: Object.freeze({
              info: { uuid: 'mock-uuid-1', name: 'Mock Wallet', icon: icon('#0E8F86'), rdns: 'me.pgas.mock' },
              provider: main.provider,
            }),
          }),
        );
        window.dispatchEvent(
          new CustomEvent('eip6963:announceProvider', {
            detail: Object.freeze({
              info: { uuid: 'mock-uuid-2', name: 'Mock Wallet 2', icon: icon('#4A63E7'), rdns: 'me.pgas.mock2' },
              provider: second.provider,
            }),
          }),
        );
      };
      window.addEventListener('eip6963:requestProvider', announce);
      announce();
    },
    { accountsA: [wallets[0].address], accountsB: [wallets[1].address], tx: TX },
  );
}

export const MOCK_TX = TX;

/** Connect the mock wallet and complete Sign-In-with-Ethereum. */
export async function connectAndSignIn(page: Page) {
  await page.getByRole('button', { name: 'Connect wallet' }).first().click();
  await page.locator('[data-wallet-id="6963:me.pgas.mock"]').click();
  await page.getByTestId('connected-address').waitFor();
  await page.getByRole('button', { name: 'Sign in with wallet' }).first().click();
  await page.locator('.chip', { hasText: 'Signed in' }).waitFor();
}
