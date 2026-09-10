// The endpoint book: which public RPC this BROWSER reads a chain through, in what order the
// fallbacks are tried after it, what the last probe said about each, and where a URL the user typed
// is kept. One reader of that fact — `chains.ts` asks here and nothing else has a copy, because two
// implementations of "which endpoint" is how a scan and a receipt wait end up on different nodes.
//
// Nothing here is a server setting. The API has its own endpoints (`PGAS_ETH_RPCS` on the box) and
// never sees these; what a visitor picks lives in that visitor's localStorage and travels nowhere.

/** The chain the endpoint must answer for, and the shape of what a probe found. */
export interface RpcProbe {
  url: string;
  /** epoch ms of the probe */
  at: number;
  ok: boolean;
  /** round trip in ms */
  ms: number;
  /** what the endpoint said it is, when it said anything */
  chainId: number | null;
  error?: string;
}

export type RpcHealth = 'unknown' | 'ok' | 'slow' | 'failed';

/** Above this a working endpoint is reported as slow rather than as fine. */
export const SLOW_MS = 800;
const PROBE_TIMEOUT_MS = 6000;
const STORE_KEY = 'pgas.rpc.v1';
/** The `<select>` value that means "let me type one" — never a URL, so it cannot collide with one. */
export const CUSTOM_OPTION = '__custom__';

// Verified 2026-09-09, twice per URL: (a) curl eth_chainId returns this chain's id, (b) fetch() from
// a page on http://127.0.0.1:4173 succeeds (CORS). Only URLs passing BOTH are listed. Every EVM
// chain on the list has at least two. Dropped for CORS: eth.merkle.io (1), op-pokt.nodies.app (10). Dropped for
// auth/rate limits: rpc.ankr.com/*, *.llamarpc.com, bsc.drpc.org, polygon-rpc.com, story.drpc.org,
// injective.drpc.org, monad-rpc.publicnode.com, rpc.arrowrpc.com.
//
// This table moved here from `chains.ts` on 2026-09-10 (T54) so that the endpoints, the user's
// choice among them and their health are one module rather than a constant in one file and a
// preference in another.
const DEFAULT_RPCS: Record<number, string[]> = {
  // ---- the 15 EVM chains the cross-chain order router offers today ----
  // rpc.flashbots.net is NOT here: it answers eth_chainId and eth_getCode and then refuses eth_call
  // with "rpc method is not whitelisted" — it is a transaction relay, not a reader.
  1: [
    'https://ethereum-rpc.publicnode.com',
    'https://eth.drpc.org',
    'https://cloudflare-eth.com',
    'https://1rpc.io/eth',
    'https://rpc.mevblocker.io',
  ],
  10: ['https://mainnet.optimism.io', 'https://optimism-rpc.publicnode.com', 'https://optimism.drpc.org', 'https://1rpc.io/op'],
  56: [
    'https://bsc-dataseed.binance.org',
    'https://bsc-dataseed1.binance.org',
    'https://bsc-dataseed2.binance.org',
    'https://bsc-dataseed3.binance.org',
    'https://bsc-dataseed4.binance.org',
    'https://bsc-rpc.publicnode.com',
    'https://bsc-dataseed1.defibit.io',
    'https://bsc-dataseed1.ninicoin.io',
  ],
  137: ['https://polygon-bor-rpc.publicnode.com', 'https://polygon.drpc.org', 'https://1rpc.io/matic'],
  4663: ['https://rpc.mainnet.chain.robinhood.com', 'https://robinhood-rpc.publicnode.com'],
  8453: ['https://mainnet.base.org', 'https://base-rpc.publicnode.com', 'https://base.drpc.org', 'https://1rpc.io/base'],
  42161: ['https://arb1.arbitrum.io/rpc', 'https://arbitrum-one-rpc.publicnode.com', 'https://arbitrum.drpc.org', 'https://1rpc.io/arb'],
  43114: [
    'https://api.avax.network/ext/bc/C/rpc',
    'https://avalanche-c-chain-rpc.publicnode.com',
    'https://avalanche.drpc.org',
    'https://1rpc.io/avax/c',
  ],
  59144: ['https://rpc.linea.build', 'https://linea-rpc.publicnode.com', 'https://linea.drpc.org', 'https://1rpc.io/linea'],
  1514: ['https://mainnet.storyrpc.io', 'https://story-mainnet-evm.itrocket.net', 'https://evm-rpc.story.mainnet.dteam.tech'],
  25: [
    'https://evm.cronos.org',
    'https://cronos-evm-rpc.publicnode.com',
    'https://cronos.drpc.org',
    'https://1rpc.io/cro',
    'https://rpc.vvs.finance',
  ],
  999: [
    'https://rpc.hyperliquid.xyz/evm',
    'https://hyperliquid.drpc.org',
    'https://rpc.hyperlend.finance',
    'https://hyperliquid-json-rpc.stakely.io',
  ],
  1776: ['https://sentry.evm-rpc.injective.network', 'https://injectiveevm-rpc.polkachu.com'],
  143: ['https://rpc.monad.xyz', 'https://monad.drpc.org'],
  4326: ['https://mainnet.megaeth.com/rpc', 'https://megaeth.rpc.thirdweb.com', 'https://megaeth.drpc.org'],
  // ---- not on the list today: kept so a chain added later is not blind (unverified) ----
  250: ['https://rpc.ftm.tools', 'https://fantom-rpc.publicnode.com'],
  100: ['https://rpc.gnosischain.com', 'https://gnosis-rpc.publicnode.com'],
  324: ['https://mainnet.era.zksync.io'],
  146: ['https://rpc.soniclabs.com'],
  80094: ['https://rpc.berachain.com'],
  1329: ['https://evm-rpc.sei-apis.com'],
  5000: ['https://rpc.mantle.xyz'],
  2741: ['https://api.mainnet.abs.xyz'],
  130: ['https://mainnet.unichain.org'],
  1868: ['https://rpc.soneium.org'],
  57073: ['https://rpc-gel.inkonchain.com'],
  480: ['https://worldchain-mainnet.g.alchemy.com/public'],
  9745: ['https://rpc.plasma.to'],
};

// ---------- what the user chose ----------
interface RpcStore {
  /** chain id → the endpoint to try FIRST. Absent means "the app's own order". */
  selected: Record<string, string>;
  /** chain id → endpoints the user added, kept so they stay in the dropdown */
  custom: Record<string, string[]>;
}

let store: RpcStore | null = null;

function read(): RpcStore {
  if (store) return store;
  try {
    const raw = localStorage.getItem(STORE_KEY);
    const parsed = raw ? (JSON.parse(raw) as Partial<RpcStore>) : null;
    store = {
      selected: parsed && typeof parsed.selected === 'object' && parsed.selected ? { ...parsed.selected } : {},
      custom: parsed && typeof parsed.custom === 'object' && parsed.custom ? { ...parsed.custom } : {},
    };
  } catch {
    store = { selected: {}, custom: {} }; // unreadable storage is no choice, not a crash
  }
  return store;
}

function write(next: RpcStore): void {
  store = next;
  try {
    if (!Object.keys(next.selected).length && !Object.keys(next.custom).length) localStorage.removeItem(STORE_KEY);
    else localStorage.setItem(STORE_KEY, JSON.stringify(next));
  } catch {
    // storage unavailable: the choice still holds for this page, and says so by simply not surviving
  }
}

// ---------- who is listening ----------
export interface RpcChange {
  /** 'selection' — what to read through changed and cached providers are now wrong; 'probe' — health only. */
  kind: 'selection' | 'probe';
  chainId: number | null;
}
type Listener = (e: RpcChange) => void;
const listeners = new Set<Listener>();

export function onRpcChange(l: Listener): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

function announce(e: RpcChange): void {
  for (const l of [...listeners]) l(e);
}

// ---------- the lists ----------
/** The endpoints this app ships for a chain, in the order it verified them. */
export function defaultRpcUrls(chainId: number): string[] {
  return DEFAULT_RPCS[chainId] ?? [];
}

/** Every chain this app ships endpoints for. */
export function chainsWithRpcs(): number[] {
  return Object.keys(DEFAULT_RPCS).map(Number);
}

/** The endpoints the user typed for this chain (kept even while another one is selected). */
export function customRpcUrls(chainId: number): string[] {
  const list = read().custom[String(chainId)];
  return Array.isArray(list) ? list.filter((u) => typeof u === 'string') : [];
}

/** Everything this chain can be read through: what the app ships, then what the user added. */
export function knownRpcUrls(chainId: number): string[] {
  return [...new Set([...defaultRpcUrls(chainId), ...customRpcUrls(chainId)])];
}

/** The user's pick, or null when this chain is on the app's own order. */
export function selectedRpcUrl(chainId: number): string | null {
  const url = read().selected[String(chainId)];
  return typeof url === 'string' && url ? url : null;
}

/**
 * The reading order: the user's pick first when there is one, then everything else in the app's
 * order. A pick is a STARTING POINT, never a restriction — an endpoint that stops answering still
 * falls through to the next one, which is the behaviour that was there before anybody could choose.
 */
export function rpcUrls(chainId: number): string[] {
  const picked = selectedRpcUrl(chainId);
  const rest = knownRpcUrls(chainId);
  return picked ? [...new Set([picked, ...rest])] : rest;
}

/** The endpoint a read starts at right now — the head of that order, or null when there is none. */
export function activeRpcUrl(chainId: number): string | null {
  return rpcUrls(chainId)[0] ?? null;
}

/** True when this chain is not on the endpoint the app would have chosen by itself. */
export function isCustomised(chainId: number): boolean {
  return selectedRpcUrl(chainId) !== null || customRpcUrls(chainId).length > 0;
}

/** Every chain the user has changed. */
export function customisedChainIds(): number[] {
  const s = read();
  return [...new Set([...Object.keys(s.selected), ...Object.keys(s.custom)])].map(Number).filter((n) => Number.isFinite(n));
}

// ---------- changing them ----------
/** Pick an endpoint for a chain; `null` puts the chain back on the app's own order. */
export function selectRpcUrl(chainId: number, url: string | null): void {
  const s = read();
  const selected = { ...s.selected };
  if (url) selected[String(chainId)] = url;
  else delete selected[String(chainId)];
  write({ ...s, selected });
  announce({ kind: 'selection', chainId });
}

/** Remember a URL the user typed for this chain and select it. Validation is the caller's job. */
export function addCustomRpcUrl(chainId: number, url: string): void {
  const s = read();
  const key = String(chainId);
  const custom = { ...s.custom, [key]: [...new Set([...(s.custom[key] ?? []), url])] };
  write({ ...s, custom, selected: { ...s.selected, [key]: url } });
  announce({ kind: 'selection', chainId });
}

/** Forget every pick and every typed endpoint: the app's own order, everywhere. */
export function resetRpcChoices(): void {
  write({ selected: {}, custom: {} });
  announce({ kind: 'selection', chainId: null });
}

// ---------- health ----------
const probes = new Map<string, RpcProbe>();
const inflight = new Map<string, Promise<RpcProbe>>();

export function lastProbe(url: string): RpcProbe | null {
  return probes.get(url) ?? null;
}

/**
 * How an endpoint is doing FOR A CHAIN. The chain is not decoration: an endpoint that answers
 * quickly for the wrong chain is a failure here, because that is exactly what the reader does with
 * it (`probeMatches`), and a green dot beside an endpoint the scanner will skip is the disagreement
 * between the prober and the caller that these systems keep paying for.
 */
export function healthOf(url: string, chainId?: number): RpcHealth {
  const p = probes.get(url);
  if (!p) return 'unknown';
  if (!p.ok) return 'failed';
  if (chainId !== undefined && p.chainId !== chainId) return 'failed';
  return p.ms >= SLOW_MS ? 'slow' : 'ok';
}

/** One sentence about an endpoint, for a title attribute and for a screen reader. */
export function probeText(url: string, chainId?: number): string {
  const p = probes.get(url);
  if (!p) return 'Not checked yet';
  if (!p.ok) return `Did not answer — ${p.error ?? 'no reason given'}`;
  if (chainId !== undefined && p.chainId !== chainId) return `Answered for chain ${p.chainId} — that is not this chain`;
  return `Answered for chain ${p.chainId} in ${Math.round(p.ms)} ms`;
}

/**
 * Ask an endpoint what chain it is, and how long it took to say so. Never throws: a probe that
 * failed is a RECORD of a failure, which is the thing the dot is showing. One probe per URL at a
 * time, so a "Check all" and a chain's own Check do not double up.
 *
 * This is also the health check the SCANNER runs (`chains.ts` → `healthyUrl`), and deliberately so:
 * "the prober must call the way the caller calls" has been paid for six times on the other stacks,
 * and a settings dot that came from a different probe than the read would be the seventh. It is why
 * the dots fill in by themselves while a scan runs.
 */
export function probeRpcUrl(url: string, timeoutMs = PROBE_TIMEOUT_MS): Promise<RpcProbe> {
  const running = inflight.get(url);
  if (running) return running;
  const p = (async (): Promise<RpcProbe> => {
    const started = Date.now();
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    let probe: RpcProbe;
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'eth_chainId', params: [] }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const j = (await res.json()) as { result?: string };
      if (typeof j.result !== 'string') throw new Error('no chain id in the answer');
      const id = parseInt(j.result, 16);
      if (!Number.isFinite(id)) throw new Error(`unreadable chain id ${j.result}`);
      probe = { url, at: Date.now(), ok: true, ms: Date.now() - started, chainId: id };
    } catch (e) {
      const msg = (e as Error)?.name === 'AbortError' ? `no answer in ${timeoutMs / 1000} s` : ((e as Error)?.message ?? 'failed');
      probe = { url, at: Date.now(), ok: false, ms: Date.now() - started, chainId: null, error: msg };
    } finally {
      clearTimeout(t);
    }
    probes.set(url, probe);
    announce({ kind: 'probe', chainId: null });
    return probe;
  })();
  inflight.set(url, p);
  void p.finally(() => inflight.delete(url));
  return p;
}

/** A probe is only "ok" for a chain when it answered FOR that chain. */
export function probeMatches(probe: RpcProbe, chainId: number): boolean {
  return probe.ok && probe.chainId === chainId;
}

// ---------- accepting a URL the user typed ----------
export type CustomResult = { ok: true; url: string; probe: RpcProbe } | { ok: false; error: string };

/**
 * The shape check, before anything is called. https only — this page is served over https and a
 * browser refuses to read from a plain-http endpoint from one, so accepting it would store a
 * setting that can only ever fail.
 */
export function parseCustomUrl(raw: string): { url: string } | { error: string } {
  const text = raw.trim();
  if (!text) return { error: 'Type the endpoint’s URL first.' };
  let u: URL;
  try {
    u = new URL(text);
  } catch {
    return { error: 'That is not a URL — it needs to look like https://your-node.example/rpc.' };
  }
  if (u.protocol !== 'https:') {
    return {
      error: `Only https endpoints work here: this page is served over https, and a browser will not read from ${u.protocol}//… .`,
    };
  }
  // `https://host` and `https://host/` are one endpoint; a path is left exactly as typed
  return { url: u.pathname === '/' && !u.search ? `${u.protocol}//${u.host}` : u.toString() };
}

/**
 * Accept a typed endpoint for a chain — or refuse it in words. It is stored only when it ANSWERED,
 * and answered for the chain it is being offered for: a URL that is a different chain would return
 * that chain's balances for this one's addresses, which reads as "your wallet is empty".
 */
export async function acceptCustomRpcUrl(
  chainId: number,
  raw: string,
  chainLabel: string,
  nameOf: (id: number) => string,
): Promise<CustomResult> {
  const parsed = parseCustomUrl(raw);
  if ('error' in parsed) return { ok: false, error: parsed.error };
  const probe = await probeRpcUrl(parsed.url);
  const host = new URL(parsed.url).host;
  if (!probe.ok) return { ok: false, error: `Nothing answered at ${host} — ${probe.error}. Not saved.` };
  if (probe.chainId !== chainId) {
    const other = probe.chainId === null ? 'another chain' : `chain ${probe.chainId} (${nameOf(probe.chainId)})`;
    return { ok: false, error: `${host} answered for ${other}, not ${chainLabel}. Not saved.` };
  }
  addCustomRpcUrl(chainId, parsed.url);
  return { ok: true, url: parsed.url, probe };
}

/** `https://rpc.example.com/v1/x` → `rpc.example.com/v1/x`: the URL without the scheme noise. */
export function shortRpcUrl(url: string): string {
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname === '/' ? '' : u.pathname}`;
  } catch {
    return url;
  }
}
