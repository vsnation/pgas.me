// The one store: connected wallet, signed-in session (+ polled account), public reference data and
// the current tab. Four hooks compose into a single context so pages read `useStore()` and nothing
// else; each slice is memoized so a render only re-runs when its own inputs change.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { hexlify, toBeHex, toUtf8Bytes } from 'ethers';
import { SESSION_EVENT, SESSION_EXPIRED_EVENT, api, clearSession, errorText, getSession, setSession, type Session } from '../lib/api';
import { CHAIN_META } from '../lib/chains';
import { checksum, hexValue } from '../lib/format';
import { buildSiweMessage } from '../lib/siwe';
import type { Account, Asset, AssetKey, Balance, Chain, Stats } from '../lib/types';
import { getWalletOptions, onWalletsChanged, parseChainId, startDiscovery, type Eip1193Provider, type WalletOption } from '../lib/wallet';

const ACCOUNT_POLL_MS = 10_000;
const LAST_WALLET_KEY = 'pgas.wallet.v1';

// ---------- wallet ----------
export interface TxRequest {
  to: string;
  data?: string;
  value?: string;
  chainId?: number;
}

export interface WalletState {
  options: WalletOption[];
  option: WalletOption | null;
  provider: Eip1193Provider | null;
  address: string | null;
  chainId: number | null;
  connecting: boolean;
  error: string | null;
  pickerOpen: boolean;
  openPicker(): void;
  closePicker(): void;
  connect(o: WalletOption): Promise<void>;
  disconnect(): void;
  switchChain(chainId: number): Promise<void>;
  personalSign(message: string, from?: string): Promise<string>;
  sendTransaction(tx: TxRequest): Promise<string>;
}

interface Attached {
  provider: Eip1193Provider;
  onAccounts: (...args: unknown[]) => void;
  onChain: (...args: unknown[]) => void;
}

function useWalletState(onDisconnect: () => void): WalletState {
  const [options, setOptions] = useState<WalletOption[]>([]);
  const [option, setOption] = useState<WalletOption | null>(null);
  const [address, setAddress] = useState<string | null>(null);
  const [chainId, setChainId] = useState<number | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const attached = useRef<Attached | null>(null);
  const addressRef = useRef<string | null>(null);
  const autoTried = useRef(false);
  addressRef.current = address;

  const detach = useCallback(() => {
    const a = attached.current;
    if (!a) return;
    a.provider.removeListener?.('accountsChanged', a.onAccounts);
    a.provider.removeListener?.('chainChanged', a.onChain);
    attached.current = null;
  }, []);

  const disconnect = useCallback(() => {
    detach();
    setOption(null);
    setAddress(null);
    setChainId(null);
    setError(null);
    try {
      localStorage.removeItem(LAST_WALLET_KEY);
    } catch {
      // storage unavailable
    }
    onDisconnect();
  }, [detach, onDisconnect]);

  const attach = useCallback(
    async (o: WalletOption, accounts: string[]) => {
      detach();
      const p = o.provider;
      const cid = parseChainId(await p.request({ method: 'eth_chainId' }).catch(() => null));
      const onAccounts = (accs: unknown) => {
        const list = Array.isArray(accs) ? (accs as string[]) : [];
        if (list.length) setAddress(checksum(list[0]));
        else disconnect();
      };
      const onChain = (id: unknown) => setChainId(parseChainId(id));
      p.on?.('accountsChanged', onAccounts);
      p.on?.('chainChanged', onChain);
      attached.current = { provider: p, onAccounts, onChain };
      setOption(o);
      setAddress(checksum(accounts[0]));
      setChainId(cid);
      setError(null);
      try {
        localStorage.setItem(LAST_WALLET_KEY, o.id);
      } catch {
        // storage unavailable
      }
    },
    [detach, disconnect],
  );

  const connect = useCallback(
    async (o: WalletOption) => {
      setConnecting(true);
      setError(null);
      try {
        const accs = (await o.provider.request({ method: 'eth_requestAccounts' })) as string[];
        if (!Array.isArray(accs) || !accs.length) throw new Error('The wallet granted no account');
        await attach(o, accs);
        setPickerOpen(false);
      } catch (e) {
        setError(errorText(e));
        throw e;
      } finally {
        setConnecting(false);
      }
    },
    [attach],
  );

  useEffect(() => {
    startDiscovery();
    const update = () => setOptions(getWalletOptions());
    update();
    return onWalletsChanged(update);
  }, []);

  // silent reconnect to the remembered wallet — eth_accounts never prompts
  useEffect(() => {
    if (autoTried.current || attached.current) return;
    let last: string | null = null;
    try {
      last = localStorage.getItem(LAST_WALLET_KEY);
    } catch {
      last = null;
    }
    if (!last) {
      autoTried.current = true;
      return;
    }
    const o = options.find((x) => x.id === last);
    if (!o) return; // not announced yet
    autoTried.current = true;
    void (async () => {
      try {
        const accs = (await o.provider.request({ method: 'eth_accounts' })) as string[];
        if (Array.isArray(accs) && accs.length) await attach(o, accs);
        else localStorage.removeItem(LAST_WALLET_KEY);
      } catch {
        // locked or refused: the user connects by hand
      }
    })();
  }, [options, attach]);

  useEffect(() => detach, [detach]);

  const switchChain = useCallback(async (target: number) => {
    const p = attached.current?.provider;
    if (!p) throw new Error('Connect a wallet first');
    const hex = toBeHex(target);
    const readChain = () => p.request({ method: 'eth_chainId' }).then(parseChainId, () => null);
    if ((await readChain()) === target) {
      setChainId(target);
      return;
    }
    try {
      await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: hex }] });
    } catch (e) {
      const err = e as { code?: number; message?: string; data?: { originalError?: { code?: number } } };
      const code = err?.code ?? err?.data?.originalError?.code;
      const unknownChain = code === 4902 || /unrecognized|not (been )?added|4902|unsupported chain|unknown chain/i.test(err?.message ?? '');
      if (!unknownChain) throw e;
      const meta = CHAIN_META[target];
      if (!meta) throw new Error(`Your wallet does not know chain ${target} and Pgas.me has no parameters to add it`);
      await p.request({ method: 'wallet_addEthereumChain', params: [{ chainId: hex, ...meta }] });
      if ((await readChain()) !== target) await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: hex }] });
    }
    const now = await readChain();
    if (now !== null && now !== target) throw new Error(`The wallet is still on chain ${now} — switch it to chain ${target} and retry`);
    setChainId(target);
  }, []);

  const personalSign = useCallback(async (message: string, from?: string) => {
    const p = attached.current?.provider;
    const addr = from ?? addressRef.current;
    if (!p || !addr) throw new Error('Connect a wallet first');
    const sig = await p.request({ method: 'personal_sign', params: [hexlify(toUtf8Bytes(message)), addr] });
    if (typeof sig !== 'string' || !sig.startsWith('0x')) throw new Error('The wallet returned no signature');
    return sig;
  }, []);

  const sendTransaction = useCallback(
    async (tx: TxRequest) => {
      const p = attached.current?.provider;
      const from = addressRef.current;
      if (!p || !from) throw new Error('Connect a wallet first');
      if (tx.chainId) await switchChain(tx.chainId);
      const params: Record<string, string> = { from, to: tx.to };
      if (tx.data && tx.data !== '0x') params.data = tx.data;
      const v = hexValue(tx.value);
      if (v && v !== '0x0') params.value = v;
      const hash = await p.request({ method: 'eth_sendTransaction', params: [params] });
      if (typeof hash !== 'string') throw new Error('The wallet returned no transaction hash');
      return hash;
    },
    [switchChain],
  );

  const openPicker = useCallback(() => {
    setOptions(getWalletOptions());
    setError(null);
    setPickerOpen(true);
  }, []);
  const closePicker = useCallback(() => setPickerOpen(false), []);

  return useMemo<WalletState>(
    () => ({
      options,
      option,
      provider: option?.provider ?? null,
      address,
      chainId,
      connecting,
      error,
      pickerOpen,
      openPicker,
      closePicker,
      connect,
      disconnect,
      switchChain,
      personalSign,
      sendTransaction,
    }),
    [
      options,
      option,
      address,
      chainId,
      connecting,
      error,
      pickerOpen,
      openPicker,
      closePicker,
      connect,
      disconnect,
      switchChain,
      personalSign,
      sendTransaction,
    ],
  );
}

// ---------- session + account ----------
export interface SessionState {
  session: Session | null;
  account: Account | null;
  accountError: string | null;
  accountLoading: boolean;
  lastAccountAt: number | null;
  signingIn: boolean;
  signInError: string | null;
  expired: boolean;
  signIn(): Promise<void>;
  signOut(): void;
  refreshAccount(): Promise<void>;
  dismissExpired(): void;
}

const EMPTY_BALANCE: Balance = { available: 0, scheduled: 0, sent: 0, pending: 0 };

// Every bucket exists for ETH even on a fresh account, so the UI never branches on undefined.
function normalizeAccount(a: Account): Account {
  const balances: Partial<Record<AssetKey, Balance>> = {};
  for (const k of ['ETH', 'DAI', 'WBTC'] as AssetKey[]) {
    const b = a.balances?.[k];
    if (b) balances[k] = { ...EMPTY_BALANCE, ...b };
  }
  if (!balances.ETH) balances.ETH = { ...EMPTY_BALANCE };
  return {
    ...a,
    balances,
    deposits: Array.isArray(a.deposits) ? a.deposits : [],
    requests: Array.isArray(a.requests) ? a.requests : [],
    history: Array.isArray(a.history) ? a.history : [],
    denominations: Array.isArray(a.denominations) ? a.denominations : [],
    modes: a.modes ?? { direct: false, instant: false },
    ingress: a.ingress ?? { armed: false, near: false },
  };
}

function useSessionState(wallet: WalletState, disconnectSignal: number): SessionState {
  const [session, setSessionState] = useState<Session | null>(() => getSession());
  const [account, setAccount] = useState<Account | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [accountLoading, setAccountLoading] = useState(false);
  const [lastAccountAt, setLastAccountAt] = useState<number | null>(null);
  const [signingIn, setSigningIn] = useState(false);
  const [signInError, setSignInError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    const onSession = () => setSessionState(getSession());
    const onExpired = () => {
      setSessionState(null);
      setAccount(null);
      setExpired(true);
    };
    window.addEventListener(SESSION_EVENT, onSession);
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    return () => {
      window.removeEventListener(SESSION_EVENT, onSession);
      window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
    };
  }, []);

  // a wallet disconnect ends the session too
  useEffect(() => {
    if (disconnectSignal === 0) return;
    clearSession();
    setAccount(null);
    setExpired(false);
  }, [disconnectSignal]);

  const refreshAccount = useCallback(async () => {
    if (!getSession()) return;
    setAccountLoading(true);
    try {
      setAccount(normalizeAccount(await api.account()));
      setAccountError(null);
      setLastAccountAt(Date.now());
    } catch (e) {
      if ((e as Error)?.name !== 'AbortError') setAccountError(errorText(e));
    } finally {
      setAccountLoading(false);
    }
  }, []);

  const token = session?.token ?? null;
  useEffect(() => {
    if (!token) {
      setAccount(null);
      setAccountError(null);
      return;
    }
    void refreshAccount();
    const tick = () => {
      if (document.visibilityState === 'visible') void refreshAccount();
    };
    const t = setInterval(tick, ACCOUNT_POLL_MS);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(t);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [token, refreshAccount]);

  const signIn = useCallback(async () => {
    if (!wallet.address || !wallet.provider) throw new Error('Connect a wallet first');
    setSigningIn(true);
    setSignInError(null);
    try {
      const { nonce, statement } = await api.siweNonce();
      const chainId = wallet.chainId ?? parseChainId(await wallet.provider.request({ method: 'eth_chainId' }).catch(() => null)) ?? 1;
      const message = buildSiweMessage({
        host: window.location.host,
        origin: window.location.origin,
        address: wallet.address,
        statement,
        chainId,
        nonce,
        issuedAt: new Date().toISOString(),
      });
      const signature = await wallet.personalSign(message, wallet.address);
      const res = await api.siweVerify(message, signature);
      setSession({
        token: res.token,
        account_id: res.account_id,
        address: res.address,
        expires_at: res.expires_in ? Date.now() + res.expires_in * 1000 : undefined,
      });
      setExpired(false);
    } catch (e) {
      setSignInError(errorText(e));
      throw e;
    } finally {
      setSigningIn(false);
    }
  }, [wallet]);

  const signOut = useCallback(() => {
    clearSession();
    setAccount(null);
    setExpired(false);
  }, []);
  const dismissExpired = useCallback(() => setExpired(false), []);

  return useMemo<SessionState>(
    () => ({
      session,
      account,
      accountError,
      accountLoading,
      lastAccountAt,
      signingIn,
      signInError,
      expired,
      signIn,
      signOut,
      refreshAccount,
      dismissExpired,
    }),
    [
      session,
      account,
      accountError,
      accountLoading,
      lastAccountAt,
      signingIn,
      signInError,
      expired,
      signIn,
      signOut,
      refreshAccount,
      dismissExpired,
    ],
  );
}

// ---------- reference data ----------
export interface DataState {
  chains: Chain[];
  assets: Asset[];
  stats: Stats | null;
  statsError: string | null;
  loading: boolean;
  error: string | null;
  reload(): void;
  chainById(chainId: number): Chain | undefined;
  /** The API sends and echoes EVM ids (`tx.chain_id`, `approval.chain_id`); one place to change if that ever differs. */
  resolveEvmChainId(id: number): number;
}

function useDataState(): DataState {
  const [chains, setChains] = useState<Chain[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    void (async () => {
      const [c, a] = await Promise.allSettled([api.chains(), api.assets()]);
      if (!alive) return;
      const errs: string[] = [];
      if (c.status === 'fulfilled') setChains(Array.isArray(c.value.chains) ? c.value.chains : []);
      else errs.push(`chains: ${errorText(c.reason)}`);
      if (a.status === 'fulfilled') setAssets(Array.isArray(a.value.assets) ? a.value.assets : []);
      else errs.push(`assets: ${errorText(a.reason)}`);
      setError(errs.length ? errs.join(' · ') : null);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [generation]);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.stats().then(
        (s) => {
          if (alive) {
            setStats(s);
            setStatsError(null);
          }
        },
        (e) => alive && setStatsError(errorText(e)),
      );
    void load();
    const t = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [generation]);

  const chainById = useCallback((id: number) => chains.find((c) => c.chain_id === id), [chains]);
  const resolveEvmChainId = useCallback((id: number) => id, []);
  const reload = useCallback(() => setGeneration((n) => n + 1), []);

  return useMemo<DataState>(
    () => ({ chains, assets, stats, statsError, loading, error, reload, chainById, resolveEvmChainId }),
    [chains, assets, stats, statsError, loading, error, reload, chainById, resolveEvmChainId],
  );
}

// ---------- route ----------
export type Tab = 'deposit' | 'balance' | 'wallets' | 'withdraw' | 'activity';

export const TABS: { id: Tab; label: string; path: string }[] = [
  { id: 'deposit', label: 'Deposit', path: '/' },
  { id: 'balance', label: 'Balance', path: '/balance' },
  { id: 'wallets', label: 'Wallets', path: '/wallets' },
  { id: 'withdraw', label: 'Withdraw', path: '/withdraw' },
  { id: 'activity', label: 'Activity', path: '/activity' },
];

export interface RouteState {
  tab: Tab;
  navigate(tab: Tab): void;
}

function tabFromPath(path: string): Tab {
  const p = path.replace(/\/+$/, '') || '/';
  return TABS.find((t) => t.path === p)?.id ?? 'deposit';
}

function useRouteState(): RouteState {
  const [tab, setTab] = useState<Tab>(() => tabFromPath(window.location.pathname));
  useEffect(() => {
    const onPop = () => setTab(tabFromPath(window.location.pathname));
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const navigate = useCallback((t: Tab) => {
    const path = TABS.find((x) => x.id === t)?.path ?? '/';
    if (window.location.pathname !== path) window.history.pushState(null, '', path);
    setTab(t);
    window.scrollTo({ top: 0 });
  }, []);
  return useMemo(() => ({ tab, navigate }), [tab, navigate]);
}

// ---------- the store ----------
export interface Store {
  wallet: WalletState;
  session: SessionState;
  data: DataState;
  route: RouteState;
}

const StoreContext = createContext<Store | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [disconnects, setDisconnects] = useState(0);
  const onDisconnect = useCallback(() => setDisconnects((n) => n + 1), []);
  const wallet = useWalletState(onDisconnect);
  const session = useSessionState(wallet, disconnects);
  const data = useDataState();
  const route = useRouteState();
  const value = useMemo<Store>(() => ({ wallet, session, data, route }), [wallet, session, data, route]);
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const v = useContext(StoreContext);
  if (!v) throw new Error('useStore outside StoreProvider');
  return v;
}
