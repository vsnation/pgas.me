// Portfolio scanner — the buybeam.my mechanism. For every chain in parallel: the connected
// wallet's own RPC when it is on that chain (no public request), otherwise a health-checked public
// fallback; native balance via getBalance; the top-150 ERC-20s in ONE batch-balance call; non-zero
// holdings priced via CoinGecko and sorted by USD.
//
// Lesson kept from buybeam: a wallet RPC can answer wrong-but-valid (Zerion returned 0 for a
// 79,702 ETH balance). A fallback catches an error, never a lie — when the wallet answers we take
// it; when it throws we fall back.
import { BrowserProvider, Contract, formatUnits, type Provider } from 'ethers';
import { api, errorText } from './api';
import {
  BATCH_BALANCE_CONTRACTS,
  COINGECKO_NATIVE_IDS,
  COINGECKO_PLATFORMS,
  NATIVE_ADDRESS,
  fallbackUrls,
  getFallbackProvider,
  invalidateFallback,
  isNativeToken,
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

/** The connected wallet, if any: its provider is preferred for the chain it is on. */
export interface ScanWallet {
  provider: Eip1193Provider;
  chainId: number | null;
}

const BATCH_ABI = ['function balanceFor(address[] _tokens, address _account) view returns (uint256[] balances, uint256[] decimals)'];
const TOP_N = 150;
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

function attemptsFor(chainId: number, wallet: ScanWallet | null): Attempt[] {
  const out: Attempt[] = [];
  if (wallet && wallet.chainId === chainId) {
    const bp = new BrowserProvider(wallet.provider, chainId);
    out.push({ label: 'wallet', get: async () => bp });
  }
  if (fallbackUrls(chainId).length) out.push({ label: 'public', get: () => getFallbackProvider(chainId) });
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

  const batch = chain.batch_balance || BATCH_BALANCE_CONTRACTS[chain.chain_id];
  if (!batch) {
    result.note = 'native balance only — no batch-balance contract on this chain';
    return result;
  }
  let tokens: Token[];
  try {
    tokens = await loadTokens(chain.chain_id);
  } catch (e) {
    result.errors.push(`token list: ${short(e)}`);
    return result;
  }
  const erc20 = tokens.filter((t) => typeof t?.address === 'string' && !isNativeToken(t.address)).slice(0, TOP_N);
  if (!erc20.length) return result;

  const readBatch = (p: Provider) =>
    withTimeout(
      new Contract(batch, BATCH_ABI, p).balanceFor(
        erc20.map((t) => t.address),
        address,
      ) as Promise<[bigint[], bigint[]]>,
      15_000,
    );
  try {
    let res: [bigint[], bigint[]];
    try {
      res = await readBatch(provider);
    } catch (e) {
      // the wallet RPC failed on the batch call: one try through the public fallback
      const fb = result.via === 'wallet' ? await getFallbackProvider(chain.chain_id) : null;
      if (!fb) throw e;
      res = await readBatch(fb);
    }
    const [balances, decimals] = res;
    erc20.forEach((t, i) => {
      const b = BigInt(balances[i] ?? 0n);
      if (b <= 0n) return;
      const d = Number(decimals[i] ?? 0n) || t.decimals;
      result.holdings.push({
        key: `${chain.chain_id}:${t.address.toLowerCase()}`,
        chainId: chain.chain_id,
        dlnChainId: chain.dln_chain_id,
        chainName: chain.name,
        address: t.address,
        symbol: t.symbol,
        name: t.name,
        decimals: d,
        logo: t.logo,
        raw: b,
        amount: Number(formatUnits(b, d)),
        native: false,
      });
    });
  } catch (e) {
    result.errors.push(`tokens: ${short(e)}`);
  }
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
      return r;
    }),
  );
  const holdings = scans.flatMap((s) => s.holdings);
  const priced = await priceHoldings(holdings).catch(() => false);
  const portfolio: Portfolio = { address, at: Date.now(), chains: scans, holdings: sortHoldings(holdings), priced };
  savePortfolio(portfolio);
  return portfolio;
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
