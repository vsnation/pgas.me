// Wallet providers: EIP-6963 announcements merged with the legacy window globals (MetaMask,
// Coinbase, Zerion, Trust, Rabby, OKX, Brave, Coin98, Binance, Phantom, Bitget, TokenPocket,
// Rainbow), the Farcaster mini-app provider when embedded, and WalletConnect v2 (initialised
// lazily, only when picked). Connection state lives in state/store.tsx; this module only finds
// and creates providers.
import { api } from './api';
import { fallbackUrls } from './chains';

export interface Eip1193Provider {
  request(args: { method: string; params?: unknown[] | Record<string, unknown> }): Promise<unknown>;
  on?(event: string, listener: (...args: unknown[]) => void): void;
  removeListener?(event: string, listener: (...args: unknown[]) => void): void;
  /** WalletConnect: opens the QR modal and resolves once a session exists; request() refuses before that. */
  connect?(): Promise<void>;
  /** WalletConnect (and a few injected wallets) can end the session from our side. */
  disconnect?(): Promise<void>;
  /** WalletConnect: the restored session, when one exists. */
  session?: unknown;
}

export type WalletSource = 'eip6963' | 'injected' | 'farcaster' | 'walletconnect';

export interface WalletOption {
  id: string;
  name: string;
  icon: string;
  /** null for WalletConnect until it is initialised */
  provider: Eip1193Provider | null;
  source: WalletSource;
  rdns?: string;
  hint?: string;
  disabledReason?: string;
  /**
   * An injected global that is the ONLY wallet in this browser because no EIP-6963 announcement
   * arrived inside the grace window — i.e. a wallet's own in-app browser (Trust, Coin98, Bitget,
   * TokenPocket on a phone). It is the row the user gets, not a fallback behind a detection that is
   * never coming.
   */
  inApp?: boolean;
}

export const WALLETCONNECT_ID = 'walletconnect';

interface Eip6963ProviderDetail {
  info: { uuid: string; name: string; icon: string; rdns: string };
  provider: Eip1193Provider;
}

type FlaggedProvider = Eip1193Provider & {
  isMetaMask?: boolean;
  isCoinbaseWallet?: boolean;
  isZerion?: boolean;
  isTrust?: boolean;
  isTrustWallet?: boolean;
  isRabby?: boolean;
  isOkxWallet?: boolean;
  isOKExWallet?: boolean;
  isBraveWallet?: boolean;
  isCoin98?: boolean;
  isBinance?: boolean;
  isPhantom?: boolean;
  isBitKeep?: boolean;
  isBitget?: boolean;
  isTokenPocket?: boolean;
  isRainbow?: boolean;
  providers?: FlaggedProvider[];
};

const BRAND_COLORS: Record<string, string> = {
  MetaMask: '#F6851B',
  'Coinbase Wallet': '#0052FF',
  Zerion: '#2962EF',
  'Trust Wallet': '#0500FF',
  Rabby: '#7084FF',
  'OKX Wallet': '#161A22',
  'Brave Wallet': '#FB542B',
  Coin98: '#D9B432',
  'Binance Web3 Wallet': '#F0B90B',
  Phantom: '#AB9FF2',
  'Bitget Wallet': '#1DA2B4',
  TokenPocket: '#2980FE',
  Rainbow: '#001E59',
  Farcaster: '#855DCD',
  WalletConnect: '#3B99FC',
};

/** Legacy detections carry no icon; a lettered tile keeps the picker uniform. */
function monogram(name: string): string {
  const color = BRAND_COLORS[name] ?? '#0E8F86';
  const letter = name.trim().charAt(0).toUpperCase() || 'W';
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40">` +
    `<rect width="40" height="40" rx="10" fill="${color}"/>` +
    `<text x="20" y="26" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" font-size="20" font-weight="700" fill="#fff">${letter}</text>` +
    `</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

const announced = new Map<string, WalletOption>();
const listeners = new Set<() => void>();
let discoveryStarted = false;
let farcasterOption: WalletOption | null = null;

/**
 * How long EIP-6963 gets before a legacy `window.*` global is treated as the wallet rather than as a
 * duplicate of an announcement still on its way. Extensions answer `eip6963:requestProvider`
 * synchronously; the in-app browsers that never announce at all (Trust, Coin98, TokenPocket,
 * Binance) are the case this exists for — they must not sit behind a detection that never lands.
 */
const LEGACY_GRACE_MS = 300;
let graceOver = false;

/** True once the grace window closed with nothing announced: this browser IS the wallet. */
export function inAppOnly(): boolean {
  return graceOver && announced.size === 0;
}

function notify(): void {
  for (const l of listeners) l();
}

export function onWalletsChanged(l: () => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function startDiscovery(): void {
  if (discoveryStarted || typeof window === 'undefined') return;
  discoveryStarted = true;
  window.addEventListener('eip6963:announceProvider', (e: Event) => {
    const detail = (e as CustomEvent<Eip6963ProviderDetail>).detail;
    if (!detail?.info?.rdns || !detail.provider) return;
    announced.set(detail.info.rdns, {
      id: `6963:${detail.info.rdns}`,
      name: detail.info.name,
      icon: detail.info.icon || monogram(detail.info.name),
      provider: detail.provider,
      source: 'eip6963',
      rdns: detail.info.rdns,
    });
    notify();
  });
  window.dispatchEvent(new Event('eip6963:requestProvider'));
  // some wallets inject after DOMContentLoaded
  window.addEventListener('ethereum#initialized', notify);
  setTimeout(() => {
    graceOver = true;
    notify();
  }, LEGACY_GRACE_MS);
  setTimeout(notify, 1500);
  void detectFarcaster();
}

// Specific flags first: several wallets also set isMetaMask for compatibility.
function legacyName(p: FlaggedProvider): { name: string; key: string } {
  if (p.isRabby) return { name: 'Rabby', key: 'rabby' };
  if (p.isZerion) return { name: 'Zerion', key: 'zerion' };
  if (p.isCoin98) return { name: 'Coin98', key: 'coin98' };
  if (p.isTrust || p.isTrustWallet) return { name: 'Trust Wallet', key: 'trust' };
  if (p.isOkxWallet || p.isOKExWallet) return { name: 'OKX Wallet', key: 'okx' };
  if (p.isBinance) return { name: 'Binance Web3 Wallet', key: 'binance' };
  if (p.isPhantom) return { name: 'Phantom', key: 'phantom' };
  if (p.isBitKeep || p.isBitget) return { name: 'Bitget Wallet', key: 'bitget' };
  if (p.isTokenPocket) return { name: 'TokenPocket', key: 'tokenpocket' };
  if (p.isRainbow) return { name: 'Rainbow', key: 'rainbow' };
  if (p.isCoinbaseWallet) return { name: 'Coinbase Wallet', key: 'coinbase' };
  if (p.isBraveWallet) return { name: 'Brave Wallet', key: 'brave' };
  if (p.isMetaMask) return { name: 'MetaMask', key: 'metamask' };
  return { name: 'Browser wallet', key: 'injected' };
}

function legacyOptions(): WalletOption[] {
  const w = window as unknown as Record<string, Record<string, unknown> | undefined>;
  const out: WalletOption[] = [];
  const seen = new Set<Eip1193Provider>();
  const push = (p: unknown, name?: string, key?: string) => {
    const prov = p as FlaggedProvider | undefined;
    if (!prov || typeof prov.request !== 'function' || seen.has(prov)) return;
    seen.add(prov);
    const n = name && key ? { name, key } : legacyName(prov);
    let id = `inj:${n.key}`;
    if (out.some((o) => o.id === id)) id = `${id}:${out.length}`;
    out.push({ id, name: n.name, icon: monogram(n.name), provider: prov, source: 'injected' });
  };
  const ethereum = w.ethereum as FlaggedProvider | undefined;
  if (ethereum?.providers?.length)
    for (const p of ethereum.providers) push(p); // several wallets sharing window.ethereum
  else if (ethereum) push(ethereum);
  push(w.coinbaseWalletExtension, 'Coinbase Wallet', 'coinbase');
  push(w.zerionWallet, 'Zerion', 'zerion');
  push(w.trustwallet, 'Trust Wallet', 'trust');
  push(w.okxwallet, 'OKX Wallet', 'okx');
  push(w.rabby, 'Rabby', 'rabby');
  push(w.coin98?.provider, 'Coin98', 'coin98');
  push(w.BinanceChain, 'Binance Web3 Wallet', 'binance');
  push(w.phantom?.ethereum, 'Phantom', 'phantom');
  push(w.bitkeep?.ethereum, 'Bitget Wallet', 'bitget');
  return out;
}

/**
 * A wallet's identity across the two detections, so one install is one row. Provider identity is the
 * strongest signal but not a reliable one: several wallets announce one object over EIP-6963 and put
 * a different proxy on their `window.*` global, and their two names differ only by the word "Wallet"
 * (announced "Rabby Wallet" / flag-detected "Rabby", announced "Coin98 Wallet" / "Coin98"). Matching
 * on the exact string showed those wallets twice.
 */
function nameKey(name: string): string {
  return name
    .toLowerCase()
    .replace(/\bwallet\b/g, '')
    .replace(/[^a-z0-9]/g, '');
}

/** EIP-6963 first (real icons), then legacy globals not already announced, Farcaster, WalletConnect last. */
export function getWalletOptions(): WalletOption[] {
  const list: WalletOption[] = [...announced.values()];
  const byProvider = new Set(list.map((o) => o.provider));
  const byName = new Set(list.map((o) => nameKey(o.name)));
  const inApp = inAppOnly();
  for (const o of legacyOptions()) {
    if (byProvider.has(o.provider)) continue;
    if (o.name !== 'Browser wallet' && byName.has(nameKey(o.name))) continue;
    if (o.name === 'Browser wallet' && list.length) continue; // a bare window.ethereum already announced under its rdns
    byName.add(nameKey(o.name));
    list.push(inApp ? { ...o, inApp: true } : o);
  }
  if (farcasterOption) list.push(farcasterOption);
  list.push(walletConnectOption());
  return list;
}

/**
 * READING a chain id off a wallet: tolerant on purpose, and the exact opposite of the write side.
 * Wallets answer `eth_chainId`/`chainChanged` with whatever they like — `'0x1'` (the EIP-695 form),
 * `'0x01'` (padded), a plain number, a decimal string, a bigint — and every one of those means the
 * same chain. What goes back OUT to a wallet is only ever `chainIdHex()` from `lib/chains.ts`.
 */
export function parseChainId(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'bigint') return Number(v);
  if (typeof v === 'string') {
    const n = /^0x/i.test(v) ? parseInt(v, 16) : Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

// ---------- Farcaster mini app ----------
// Loaded lazily and only when the page is embedded (Warpcast renders mini apps in an iframe or a
// React Native webview) so the SDK never enters the main bundle and cannot block the app.
function looksEmbedded(): boolean {
  try {
    return window.self !== window.top || !!(window as unknown as { ReactNativeWebView?: unknown }).ReactNativeWebView;
  } catch {
    return true; // cross-origin parent access throws: we are framed
  }
}

async function detectFarcaster(): Promise<void> {
  if (!looksEmbedded()) return;
  try {
    const { sdk } = await import('@farcaster/miniapp-sdk');
    const inside = await Promise.race([sdk.isInMiniApp(), new Promise<boolean>((r) => setTimeout(() => r(false), 1500))]);
    if (!inside) return;
    const provider = (await sdk.wallet.getEthereumProvider()) as Eip1193Provider | undefined;
    if (!provider) return;
    farcasterOption = { id: 'farcaster', name: 'Farcaster', icon: monogram('Farcaster'), provider, source: 'farcaster' };
    notify();
    sdk.actions.ready().catch(() => undefined);
  } catch {
    // not inside a Farcaster client, or the SDK failed to load — injected wallets still work
  }
}

// ---------- WalletConnect v2 ----------
// The provider (and its QR modal) is a large dependency, so it is imported only when the user
// picks WalletConnect or a remembered WalletConnect session has to be restored.
const WC_PROJECT_ID = (import.meta.env.VITE_WALLETCONNECT_PROJECT_ID ?? '').trim();
const WC_FALLBACK_CHAINS = [1, 42161, 8453, 10, 137, 56];
let wcProvider: Eip1193Provider | null = null;
let wcInit: Promise<Eip1193Provider> | null = null;

function walletConnectOption(): WalletOption {
  return {
    id: WALLETCONNECT_ID,
    name: 'WalletConnect',
    icon: monogram('WalletConnect'),
    provider: wcProvider,
    source: 'walletconnect',
    hint: 'scan with any mobile wallet',
    disabledReason: WC_PROJECT_ID ? undefined : 'not configured',
  };
}

export function initWalletConnect(): Promise<Eip1193Provider> {
  if (wcProvider) return Promise.resolve(wcProvider);
  if (wcInit) return wcInit;
  wcInit = (async () => {
    if (!WC_PROJECT_ID) throw new Error('WalletConnect is not configured on this deployment');
    const [{ EthereumProvider }, listed] = await Promise.all([
      import('@walletconnect/ethereum-provider'),
      api.chains().then(
        (r) => r.chains.map((c) => c.chain_id).filter((n) => Number.isInteger(n) && n > 0),
        () => [] as number[],
      ),
    ]);
    const chains = listed.length ? listed : WC_FALLBACK_CHAINS;
    // The endpoint WalletConnect will read each chain through: the head of the same order every
    // other read in this app uses, so a pick in the RPC settings popup is not quietly ignored by
    // the one reader that is configured up front rather than per call (T54).
    const rpcMap: Record<number, string> = {};
    for (const id of chains) {
      const url = fallbackUrls(id)[0];
      if (url) rpcMap[id] = url;
    }
    const provider = await EthereumProvider.init({
      projectId: WC_PROJECT_ID,
      optionalChains: chains as [number, ...number[]],
      showQrModal: true,
      rpcMap,
      metadata: {
        name: 'Pgas.me',
        description: 'Private gas funding for fresh EVM wallets, settled on Beam.',
        url: window.location.origin,
        icons: [`${window.location.origin}/logo-256.png`],
      },
    });
    wcProvider = provider as unknown as Eip1193Provider;
    notify();
    return wcProvider;
  })();
  wcInit.catch(() => {
    wcInit = null;
  });
  return wcInit;
}

/** For the remembered-wallet path on reload: the provider only when a session already exists. */
export async function restoreWalletConnect(): Promise<Eip1193Provider | null> {
  if (!WC_PROJECT_ID) return null;
  try {
    const p = await initWalletConnect();
    return p.session ? p : null;
  } catch {
    return null;
  }
}
