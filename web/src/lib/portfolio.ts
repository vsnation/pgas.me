// Portfolio scanner — the buybeam.my mechanism, widened to every EVM chain deBridge lists. For each
// chain in parallel: a health-checked public RPC (the wallet's own provider only as a last resort);
// the native balance via getBalance; the top-300 ERC-20s of the DLN token list in chunks of 150,
// through the chain's batch-balance contract where one exists and Multicall3 everywhere else;
// non-zero holdings priced via CoinGecko and sorted by USD. Chains finish independently and are
// reported one by one, so a slow chain never holds up the chips.
import { BrowserProvider, Contract, Interface, formatUnits, type Provider } from 'ethers';
import { api, errorText } from './api';
import {
  BATCH_BALANCE_CONTRACTS,
  COINGECKO_NATIVE_IDS,
  COINGECKO_PLATFORMS,
  MULTICALL3,
  NATIVE_ADDRESS,
  fallbackUrls,
  getFallbackProvider,
  invalidateFallback,
  isNativeToken,
  isNonEvmChain,
  withTimeout,
} from './chains';
import type { Chain, Token } from './types';
import type { Eip1193Provider } from './wallet';

export interface Holding {
  key: string;
  chainId: number;
  dlnChainId: number;
  chainName: string;
  address: string;
  symbol: string;
  name: string;
  decimals: number;
  logo?: string;
  raw: bigint;
  amount: number;
  usd?: number;
  native: boolean;
}

export type ScanVia = 'wallet' | 'public' | 'none';

export interface ChainScan {
  chainId: number;
  name: string;
  via: ScanVia;
  /** deBridge lists Solana and Tron; there is no eth_* RPC to ask, so they are skipped, not failed. */
  nonEvm?: boolean;
  holdings: Holding[];
  errors: string[];
  note?: string;
}

export interface Portfolio {
  address: string;
  at: number;
  chains: ChainScan[];
  holdings: Holding[];
  priced: boolean;
}

/** The connected wallet, if any: its provider is the last-resort reader for the chain it is on. */
export interface ScanWallet {
  provider: Eip1193Provider;
  chainId: number | null;
}

const BATCH_ABI = ['function balanceFor(address[] _tokens, address _account) view returns (uint256[] balances, uint256[] decimals)'];
const MULTICALL3_IFACE = new Interface([
  'function aggregate3((address target, bool allowFailure, bytes callData)[] calls) payable returns ((bool success, bytes returnData)[] returnData)',
]);
const ERC20_IFACE = new Interface(['function balanceOf(address owner) view returns (uint256)']);

const TOP_N = 300; // DLN orders its token list by relevance; the head is what a wallet actually holds
const CHUNK = 150; // tokens per batch-balance / aggregate3 call
const PER_TOKEN_N = 60; // last resort when neither aggregate helper answers
const PER_TOKEN_BATCH = 50; // JSON-RPC batch size for that fallback
const TOKENS_TTL_MS = 6 * 60 * 60 * 1000;
const TOKENS_STORE_CAP = 400; // localStorage keeps the head of each list; memory keeps it all
const PORTFOLIO_TTL_MS = 2 * 60 * 1000;
const NATIVE_TTL_MS = 60 * 1000;
const PRICES_TTL_MS = 5 * 60 * 1000;
const PRICES_KEY = 'pgas.prices.v1';
const COINGECKO = 'https://api.coingecko.com/api/v3';

function short(e: unknown): string {
  const t = errorText(e);
  return t.length > 120 ? t.slice(0, 120) + '…' : t;
}

// ---------- token lists (DLN via the API), cached ----------
const tokenMem = new Map<number, Promise<Token[]>>();

export function loadTokens(chainId: number): Promise<Token[]> {
  const existing = tokenMem.get(chainId);
  if (existing) return existing;
  const key = `pgas.tokens.v1.${chainId}`;
  const p = (async () => {
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const c = JSON.parse(raw) as { at: number; tokens: Token[] };
        if (Date.now() - c.at < TOKENS_TTL_MS && Array.isArray(c.tokens) && c.tokens.length) return c.tokens;
      }
    } catch {
      // unreadable cache: fetch
    }
    const { tokens } = await api.tokens(chainId);
    const list = Array.isArray(tokens) ? tokens : [];
    try {
      localStorage.setItem(key, JSON.stringify({ at: Date.now(), tokens: list.slice(0, TOKENS_STORE_CAP) }));
    } catch {
      // quota: the memory cache still holds it
    }
    return list;
  })();
  p.catch(() => tokenMem.delete(chainId));
  tokenMem.set(chainId, p);
  return p;
}

// ---------- providers ----------
interface Attempt {
  label: Exclude<ScanVia, 'none'>;
  get: () => Promise<Provider | null>;
}

/**
 * PUBLIC RPC FIRST; the wallet's own provider only when every public endpoint for that chain failed.
 * buybeam recorded Zerion's injected RPC answering 0 for a real balance, and the screenshot that
 * started this work is Zerion on Ethereum with the Ethereum holding missing. A fallback catches an
 * error, never a lie — so a public read wins and the wallet is the last resort, not the first.
 */
function attemptsFor(chainId: number, wallet: ScanWallet | null): Attempt[] {
  const out: Attempt[] = [];
  if (fallbackUrls(chainId).length) out.push({ label: 'public', get: () => getFallbackProvider(chainId) });
  if (wallet && wallet.chainId === chainId) {
    const w = wallet.provider;
    out.push({ label: 'wallet', get: async () => new BrowserProvider(w, chainId) });
  }
  return out;
}

/** The wallet's provider for this chain, used to retry a token read the public RPC could not serve. */
function walletProviderFor(chainId: number, wallet: ScanWallet | null): Provider | null {
  return wallet && wallet.chainId === chainId ? new BrowserProvider(wallet.provider, chainId) : null;
}

// ---------- ERC-20 balances ----------
interface TokenRead {
  token: Token;
  raw: bigint;
  decimals: number;
}

/** The chain has a balanceFor helper: one call per 150 tokens, decimals straight from the contract. */
async function viaBatchContract(p: Provider, batch: string, tokens: Token[], account: string): Promise<TokenRead[]> {
  const c = new Contract(batch, BATCH_ABI, p);
  const out: TokenRead[] = [];
  for (let i = 0; i < tokens.length; i += CHUNK) {
    const chunk = tokens.slice(i, i + CHUNK);
    const [balances, decimals] = await withTimeout(
      c.balanceFor(
        chunk.map((t) => t.address),
        account,
      ) as Promise<[bigint[], bigint[]]>,
      20_000,
    );
    chunk.forEach((t, j) => out.push({ token: t, raw: BigInt(balances[j] ?? 0n), decimals: Number(decimals[j] ?? 0n) || t.decimals }));
  }
  return out;
}

/**
 * Everywhere else: Multicall3 at its canonical address, allowFailure=true so a token that reverts is
 * a zero rather than a thrown chain. aggregate3 is declared payable, so it goes out as a plain
 * eth_call rather than through a Contract method.
 */
async function viaMulticall3(p: Provider, tokens: Token[], account: string): Promise<TokenRead[]> {
  const out: TokenRead[] = [];
  const callData = ERC20_IFACE.encodeFunctionData('balanceOf', [account]);
  for (let i = 0; i < tokens.length; i += CHUNK) {
    const chunk = tokens.slice(i, i + CHUNK);
    const data = MULTICALL3_IFACE.encodeFunctionData('aggregate3', [chunk.map((t) => [t.address, true, callData])]);
    const res = await withTimeout(p.call({ to: MULTICALL3, data }), 20_000);
    const [items] = MULTICALL3_IFACE.decodeFunctionResult('aggregate3', res) as unknown as [[boolean, string][]];
    chunk.forEach((t, j) => {
      const item = items?.[j];
      let raw = 0n;
      if (item?.[0] && item[1] && item[1] !== '0x') {
        try {
          raw = BigInt(ERC20_IFACE.decodeFunctionResult('balanceOf', item[1])[0] as bigint);
        } catch {
          raw = 0n; // a token that answers something that is not a uint256 holds nothing for us
        }
      }
      out.push({ token: t, raw, decimals: t.decimals });
    });
  }
  return out;
}

/**
 * No aggregate helper answered: one balanceOf per token, packed by the provider's JSON-RPC batching.
 * A single token that reverts is a zero — but a provider on which EVERY token fails has told us
 * nothing, so that throws rather than reporting an empty wallet.
 */
async function viaPerToken(p: Provider, tokens: Token[], account: string): Promise<TokenRead[]> {
  const callData = ERC20_IFACE.encodeFunctionData('balanceOf', [account]);
  const out: TokenRead[] = [];
  let failed = 0;
  let firstError: unknown = null;
  for (let i = 0; i < tokens.length; i += PER_TOKEN_BATCH) {
    const chunk = tokens.slice(i, i + PER_TOKEN_BATCH);
    const reads = await Promise.all(
      chunk.map((t) =>
        withTimeout(p.call({ to: t.address, data: callData }), 20_000)
          .then((r) => (r && r !== '0x' ? BigInt(ERC20_IFACE.decodeFunctionResult('balanceOf', r)[0] as bigint) : 0n))
          .catch((e) => {
            failed++;
            firstError = firstError ?? e;
            return 0n;
          }),
      ),
    );
    chunk.forEach((t, j) => out.push({ token: t, raw: reads[j], decimals: t.decimals }));
  }
  if (tokens.length && failed === tokens.length) throw firstError;
  return out;
}

// ---------- one chain ----------
function nativeHolding(chain: Chain, raw: bigint): Holding {
  return {
    key: `${chain.chain_id}:native`,
    chainId: chain.chain_id,
    dlnChainId: chain.dln_chain_id,
    chainName: chain.name,
    address: NATIVE_ADDRESS,
    symbol: chain.native_symbol,
    name: chain.native_symbol,
    decimals: 18,
    raw,
    amount: Number(formatUnits(raw, 18)),
    native: true,
  };
}

async function scanChain(chain: Chain, address: string, wallet: ScanWallet | null): Promise<ChainScan> {
  const result: ChainScan = { chainId: chain.chain_id, name: chain.name, via: 'none', holdings: [], errors: [] };
  if (isNonEvmChain(chain.chain_id)) {
    result.nonEvm = true;
    result.note = 'not scanned (non-EVM)';
    return result;
  }
  const attempts = attemptsFor(chain.chain_id, wallet);
  if (!attempts.length) {
    result.errors.push('no RPC for this chain (connect the wallet to it to read balances)');
    return result;
  }
  let provider: Provider | null = null;
  let native: bigint | null = null;
  for (const a of attempts) {
    try {
      const p = await a.get();
      if (!p) {
        result.errors.push(`${a.label}: no healthy RPC`);
        continue;
      }
      native = await withTimeout(p.getBalance(address), 12_000);
      provider = p;
      result.via = a.label;
      break;
    } catch (e) {
      result.errors.push(`${a.label}: ${short(e)}`);
      if (a.label === 'public') invalidateFallback(chain.chain_id);
    }
  }
  if (!provider || native === null) return result;
  if (native > 0n) result.holdings.push(nativeHolding(chain, native));

  let tokens: Token[];
  try {
    tokens = await loadTokens(chain.chain_id);
  } catch (e) {
    result.errors.push(`token list: ${short(e)}`);
    return result;
  }
  const erc20 = tokens.filter((t) => typeof t?.address === 'string' && !isNativeToken(t.address)).slice(0, TOP_N);
  if (!erc20.length) return result;

  // Readers in order of preference, each a fallback for the one before it. The batch-balance helper
  // is tried first where the chain claims one, but it is never the only option: the API advertises
  // 0x50188692… on Optimism, where eth_getCode says there is no code at all.
  const batch = chain.batch_balance || BATCH_BALANCE_CONTRACTS[chain.chain_id];
  const readers: { via: string; run: (p: Provider) => Promise<TokenRead[]> }[] = [
    ...(batch ? [{ via: 'batch-balance', run: (p: Provider) => viaBatchContract(p, batch, erc20, address) }] : []),
    { via: 'multicall3', run: (p: Provider) => viaMulticall3(p, erc20, address) },
    { via: 'balanceOf', run: (p: Provider) => viaPerToken(p, erc20.slice(0, PER_TOKEN_N), address) },
  ];
  // Providers to try, in order. The chainId probe is not the call a reader makes — rpc.flashbots.net
  // answers eth_chainId and refuses eth_call — so when every reader fails on one endpoint, rotate to
  // the next public URL before falling back to the wallet.
  const candidates: (() => Promise<Provider | null>)[] = [async () => provider];
  if (result.via === 'public') {
    candidates.push(async () => {
      invalidateFallback(chain.chain_id);
      return getFallbackProvider(chain.chain_id);
    });
    const w = walletProviderFor(chain.chain_id, wallet);
    if (w) candidates.push(async () => w);
  } else candidates.push(() => getFallbackProvider(chain.chain_id));
  const errors: string[] = [];
  for (const get of candidates) {
    const p = await get().catch(() => null);
    if (!p) continue;
    for (const reader of readers) {
      try {
        for (const r of await reader.run(p)) {
          if (r.raw <= 0n) continue;
          result.holdings.push({
            key: `${chain.chain_id}:${r.token.address.toLowerCase()}`,
            chainId: chain.chain_id,
            dlnChainId: chain.dln_chain_id,
            chainName: chain.name,
            address: r.token.address,
            symbol: r.token.symbol,
            name: r.token.name,
            decimals: r.decimals,
            logo: r.token.logo,
            raw: r.raw,
            amount: Number(formatUnits(r.raw, r.decimals)),
            native: false,
          });
        }
        if (reader.via === 'balanceOf') result.note = `ERC-20s read one by one — only the top ${PER_TOKEN_N} were checked`;
        return result;
      } catch (e) {
        errors.push(`${reader.via}: ${short(e)}`);
      }
    }
  }
  result.errors.push(`tokens: ${errors.join('; ')}`);
  return result;
}

// ---------- prices (CoinGecko, 5-minute localStorage cache; a 429 just leaves prices out) ----------
interface PriceEntry {
  usd: number | null;
  at: number;
}
let rateLimitedUntil = 0;

export function pricesRateLimited(): boolean {
  return Date.now() < rateLimitedUntil;
}

function loadPriceCache(): Record<string, PriceEntry> {
  try {
    return JSON.parse(localStorage.getItem(PRICES_KEY) ?? '{}') as Record<string, PriceEntry>;
  } catch {
    return {};
  }
}

function savePriceCache(c: Record<string, PriceEntry>): void {
  try {
    const now = Date.now();
    const kept: Record<string, PriceEntry> = {};
    for (const [k, v] of Object.entries(c)) if (now - v.at < PRICES_TTL_MS * 4) kept[k] = v;
    localStorage.setItem(PRICES_KEY, JSON.stringify(kept));
  } catch {
    // storage unavailable
  }
}

async function fetchPrices(url: string): Promise<Record<string, { usd?: number }> | null> {
  if (pricesRateLimited()) return null;
  try {
    const res = await fetch(url, { headers: { Accept: 'application/json' } });
    if (res.status === 429) {
      rateLimitedUntil = Date.now() + 60_000;
      return null;
    }
    if (!res.ok) return null;
    return (await res.json()) as Record<string, { usd?: number }>;
  } catch {
    return null;
  }
}

function priceKey(h: Holding): string | null {
  if (h.native) {
    const id = COINGECKO_NATIVE_IDS[h.chainId];
    return id ? `native:${id}` : null;
  }
  const platform = COINGECKO_PLATFORMS[h.chainId];
  return platform ? `${platform}:${h.address.toLowerCase()}` : null;
}

/** Fills `usd` on the holdings it can price. Returns whether any price was found. Never throws. */
async function priceHoldings(hs: Holding[]): Promise<boolean> {
  const cache = loadPriceCache();
  const now = Date.now();
  const missingNative = new Set<string>();
  const missingTokens = new Map<string, Set<string>>();
  for (const h of hs) {
    const key = priceKey(h);
    if (!key) continue;
    const e = cache[key];
    if (e && now - e.at < PRICES_TTL_MS) continue;
    if (h.native) missingNative.add(key.slice('native:'.length));
    else {
      const [platform, addr] = key.split(':');
      if (!missingTokens.has(platform)) missingTokens.set(platform, new Set());
      missingTokens.get(platform)!.add(addr);
    }
  }
  if (missingNative.size) {
    const ids = [...missingNative];
    const j = await fetchPrices(`${COINGECKO}/simple/price?ids=${encodeURIComponent(ids.join(','))}&vs_currencies=usd`);
    if (j) for (const id of ids) cache[`native:${id}`] = { usd: typeof j[id]?.usd === 'number' ? j[id].usd! : null, at: now };
  }
  for (const [platform, addrs] of missingTokens) {
    const list = [...addrs];
    for (let i = 0; i < list.length; i += 50) {
      const chunk = list.slice(i, i + 50);
      const j = await fetchPrices(`${COINGECKO}/simple/token_price/${platform}?contract_addresses=${chunk.join(',')}&vs_currencies=usd`);
      if (!j) break;
      const lower = Object.fromEntries(Object.entries(j).map(([k, v]) => [k.toLowerCase(), v]));
      for (const a of chunk) cache[`${platform}:${a}`] = { usd: typeof lower[a]?.usd === 'number' ? lower[a].usd! : null, at: now };
    }
  }
  savePriceCache(cache);
  let any = false;
  for (const h of hs) {
    const key = priceKey(h);
    const usd = key ? cache[key]?.usd : null;
    if (typeof usd === 'number') {
      h.usd = h.amount * usd;
      any = true;
    } else h.usd = undefined;
  }
  return any;
}

function sortHoldings(hs: Holding[]): Holding[] {
  return [...hs].sort((a, b) => {
    const au = a.usd ?? -1;
    const bu = b.usd ?? -1;
    if (au !== bu) return bu - au;
    if (a.native !== b.native) return a.native ? -1 : 1;
    return b.amount - a.amount;
  });
}

// ---------- whole portfolio ----------
export async function scanPortfolio(
  address: string,
  chains: Chain[],
  wallet: ScanWallet | null,
  onProgress?: (done: number, total: number) => void,
  /** Fired the moment a chain finishes, so its chips appear without waiting for the slowest chain. */
  onChain?: (scan: ChainScan) => void,
): Promise<Portfolio> {
  let done = 0;
  const scans = await Promise.all(
    chains.map(async (c) => {
      const r = await scanChain(c, address, wallet).catch((e): ChainScan => ({
        chainId: c.chain_id,
        name: c.name,
        via: 'none',
        holdings: [],
        errors: [short(e)],
      }));
      done++;
      onProgress?.(done, chains.length);
      onChain?.(r);
      return r;
    }),
  );
  const holdings = scans.flatMap((s) => s.holdings);
  const priced = await priceHoldings(holdings).catch(() => false);
  const portfolio: Portfolio = { address, at: Date.now(), chains: scans, holdings: sortHoldings(holdings), priced };
  savePortfolio(portfolio);
  return portfolio;
}

/** The same order the finished portfolio uses, for the chips that appear mid-scan. */
export function sortForDisplay(hs: Holding[]): Holding[] {
  return sortHoldings(hs);
}

// A short-lived cache so a reload does not hammer public RPCs. bigint is stored as a string.
function portfolioKey(address: string): string {
  return `pgas.portfolio.v1.${address.toLowerCase()}`;
}

function savePortfolio(p: Portfolio): void {
  try {
    const ser = (h: Holding) => ({ ...h, raw: h.raw.toString() });
    localStorage.setItem(
      portfolioKey(p.address),
      JSON.stringify({ ...p, holdings: p.holdings.map(ser), chains: p.chains.map((c) => ({ ...c, holdings: c.holdings.map(ser) })) }),
    );
  } catch {
    // storage unavailable or full
  }
}

export function loadCachedPortfolio(address: string, ttlMs = PORTFOLIO_TTL_MS): Portfolio | null {
  try {
    const raw = localStorage.getItem(portfolioKey(address));
    if (!raw) return null;
    const p = JSON.parse(raw) as Portfolio;
    if (Date.now() - p.at > ttlMs) return null;
    const de = (h: Holding) => ({ ...h, raw: BigInt(h.raw as unknown as string) });
    return { ...p, holdings: p.holdings.map(de), chains: p.chains.map((c) => ({ ...c, holdings: c.holdings.map(de) })) };
  } catch {
    return null;
  }
}

// ---------- native-only balances (Wallets page) ----------
export interface NativeRead {
  value: bigint | null;
  via: ScanVia;
  error?: string;
}
const nativeCache = new Map<string, { at: number; read: NativeRead }>();
const nativeInflight = new Map<string, Promise<NativeRead>>();

export function nativeBalance(chainId: number, address: string, wallet: ScanWallet | null, force = false): Promise<NativeRead> {
  const key = `${chainId}:${address.toLowerCase()}`;
  const cached = nativeCache.get(key);
  if (!force && cached && Date.now() - cached.at < NATIVE_TTL_MS) return Promise.resolve(cached.read);
  const running = nativeInflight.get(key);
  if (running) return running;
  const p = (async (): Promise<NativeRead> => {
    const errors: string[] = [];
    for (const a of attemptsFor(chainId, wallet)) {
      try {
        const prov = await a.get();
        if (!prov) continue;
        const read: NativeRead = { value: await withTimeout(prov.getBalance(address), 12_000), via: a.label };
        nativeCache.set(key, { at: Date.now(), read });
        return read;
      } catch (e) {
        errors.push(`${a.label}: ${short(e)}`);
        if (a.label === 'public') invalidateFallback(chainId);
      }
    }
    const read: NativeRead = { value: null, via: 'none', error: errors.join('; ') || 'no RPC for this chain' };
    nativeCache.set(key, { at: Date.now(), read });
    return read;
  })();
  nativeInflight.set(key, p);
  p.finally(() => nativeInflight.delete(key));
  return p;
}
