// Injected wallet discovery: EIP-6963 announcements merged with the legacy window globals
// (MetaMask, Coinbase, Zerion, Trust, Rabby, OKX) and, when embedded in a Farcaster client, the
// mini-app provider. Connection state lives in state/store.tsx; this module only finds providers.

export interface Eip1193Provider {
  request(args: { method: string; params?: unknown[] | Record<string, unknown> }): Promise<unknown>;
  on?(event: string, listener: (...args: unknown[]) => void): void;
  removeListener?(event: string, listener: (...args: unknown[]) => void): void;
}

export type WalletSource = 'eip6963' | 'injected' | 'farcaster';

export interface WalletOption {
  id: string;
  name: string;
  icon: string;
  provider: Eip1193Provider;
  source: WalletSource;
  rdns?: string;
}

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
  Farcaster: '#855DCD',
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
  setTimeout(notify, 300);
  setTimeout(notify, 1500);
  void detectFarcaster();
}

// Specific flags first: several wallets also set isMetaMask for compatibility.
function legacyName(p: FlaggedProvider): { name: string; key: string } {
  if (p.isRabby) return { name: 'Rabby', key: 'rabby' };
  if (p.isZerion) return { name: 'Zerion', key: 'zerion' };
  if (p.isTrust || p.isTrustWallet) return { name: 'Trust Wallet', key: 'trust' };
  if (p.isOkxWallet || p.isOKExWallet) return { name: 'OKX Wallet', key: 'okx' };
  if (p.isCoinbaseWallet) return { name: 'Coinbase Wallet', key: 'coinbase' };
  if (p.isBraveWallet) return { name: 'Brave Wallet', key: 'brave' };
  if (p.isMetaMask) return { name: 'MetaMask', key: 'metamask' };
  return { name: 'Browser wallet', key: 'injected' };
}

function legacyOptions(): WalletOption[] {
  const w = window as unknown as Record<string, unknown>;
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
  if (ethereum?.providers?.length) for (const p of ethereum.providers) push(p);
  else if (ethereum) push(ethereum);
  push(w.coinbaseWalletExtension, 'Coinbase Wallet', 'coinbase');
  push(w.zerionWallet, 'Zerion', 'zerion');
  push(w.trustwallet, 'Trust Wallet', 'trust');
  push(w.okxwallet, 'OKX Wallet', 'okx');
  push(w.rabby, 'Rabby', 'rabby');
  return out;
}

/** EIP-6963 first (real icons), then legacy globals not already announced, then Farcaster. */
export function getWalletOptions(): WalletOption[] {
  const list: WalletOption[] = [...announced.values()];
  const byProvider = new Set(list.map((o) => o.provider));
  const byName = new Set(list.map((o) => o.name.toLowerCase()));
  for (const o of legacyOptions()) {
    if (byProvider.has(o.provider)) continue;
    if (o.name !== 'Browser wallet' && byName.has(o.name.toLowerCase())) continue;
    if (o.name === 'Browser wallet' && list.length) continue; // a bare window.ethereum already announced under its rdns
    list.push(o);
  }
  if (farcasterOption) list.push(farcasterOption);
  return list;
}

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
