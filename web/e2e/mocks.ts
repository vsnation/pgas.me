// Test doubles for the two things the web app talks to: the Pgas.me API (API_CONTRACT.md, served
// through page.route) and an injected EIP-1193 wallet (window.ethereum + EIP-6963 announcement).
// personal_sign is delegated to Node-side ethers wallets through page.exposeFunction, so the mock
// API can really recover signers and the message templates are checked end to end.
import type { Page, Route } from '@playwright/test';
import { Wallet, getAddress, getBytes, verifyMessage } from 'ethers';

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

interface Destination {
  address: string;
  kind: string;
  verified_at: number;
  label: string;
  created_at: number;
}

const CHAINS = [
  { chain_id: 1, dln_chain_id: 1, name: 'Ethereum', native_symbol: 'ETH' },
  { chain_id: 42161, dln_chain_id: 42161, name: 'Arbitrum', native_symbol: 'ETH' },
  { chain_id: 8453, dln_chain_id: 8453, name: 'Base', native_symbol: 'ETH' },
  { chain_id: 1514, dln_chain_id: 100000013, name: 'Story', native_symbol: 'IP' },
];

const TOKENS = [
  { address: NATIVE, symbol: 'ETH', name: 'Ether', decimals: 18, logo: '' },
  { address: USDC, symbol: 'USDC', name: 'USD Coin', decimals: 6, logo: '' },
  { address: '0x6B175474E89094C44Da98b954EedeAC495271d0F', symbol: 'DAI', name: 'Dai', decimals: 18, logo: '' },
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
      const amount = BigInt(String(b.amount));
      const native = String(b.src_token).toLowerCase() === NATIVE;
      const decimals = native ? 18 : 6;
      const outUnits = native ? (amount * 98n) / 100n : (amount * 10n ** 12n * 98n) / 100n / 4000n;
      const quote: Record<string, unknown> = {
        quote_id: 'q-' + Math.random().toString(36).slice(2, 8),
        target_asset: b.target_asset,
        armed: this.armed,
        expires_at: new Date(Date.now() + 30_000).toISOString(),
        estimate: {
          src: { chain_id: b.src_chain_id, token: b.src_token, symbol: native ? 'ETH' : 'USDC', decimals, amount: String(b.amount) },
          out_units: outUnits.toString(),
          out_groth: Number(outUnits / 10n ** 10n),
          value_units: outUnits.toString(),
          relayer_fee_units: '40000000000000',
          usd: (Number(outUnits) / 1e18) * 4000,
          eta_s: 420,
        },
      };
      if (this.armed) {
        quote.tx = {
          chain_id: b.src_chain_id,
          to: '0xeF4fB24aD0916217251F553c0596F8Edc630EB66',
          data: '0xdeadbeef',
          value: native ? String(b.amount) : '0',
        };
        if (!native)
          quote.approval = {
            chain_id: b.src_chain_id,
            token: b.src_token,
            spender: '0xeF4fB24aD0916217251F553c0596F8Edc630EB66',
            amount: String(b.amount),
          };
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

/** Blocks every request that leaves 127.0.0.1 (RPCs, CoinGecko, fonts unless allowed). */
export async function blockExternal(page: Page, allowFonts = false) {
  await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, (route) => {
    const url = route.request().url();
    if (allowFonts && /fonts\.(googleapis|gstatic)\.com/.test(url)) return route.continue();
    return route.abort('blockedbyclient');
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
    ({ accounts, tx }) => {
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
      (window as any).ethereum = provider;
      (window as any).__mock = {
        state,
        setAccounts(list: string[]) {
          state.accounts = list;
          emit('accountsChanged', list);
        },
      };
      const icon =
        'data:image/svg+xml;utf8,' +
        encodeURIComponent(
          '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="10" fill="#0E8F86"/></svg>',
        );
      const announce = () =>
        window.dispatchEvent(
          new CustomEvent('eip6963:announceProvider', {
            detail: Object.freeze({ info: { uuid: 'mock-uuid-1', name: 'Mock Wallet', icon, rdns: 'me.pgas.mock' }, provider }),
          }),
        );
      window.addEventListener('eip6963:requestProvider', announce);
      announce();
    },
    { accounts: [wallets[0].address], tx: TX },
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
