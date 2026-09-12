// The one store: connected wallet, signed-in session (+ polled account), public reference data, the
// light/dark choice and the current tab. Five hooks compose into a single context so pages read
// `useStore()` and nothing else; each slice is memoized so a render only re-runs when its own
// inputs change.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { hexlify, toUtf8Bytes } from 'ethers';
import {
  SESSION_EVENT,
  SESSION_EXPIRED_EVENT,
  api,
  clearSession,
  errorText,
  getSession,
  rejectedByUser,
  setSession,
  type Session,
} from '../lib/api';
import { chainIdHex, chainMeta } from '../lib/chains';
import { checksum, hexValue } from '../lib/format';
import { ingressPartial, uniswapTokenList, type IngressPartial } from '../lib/ingress';
import { buildSiweMessage } from '../lib/siwe';
import { applyTheme, currentTheme, onSystemThemeChange, setTheme as persistTheme, storedTheme, type ThemeChoice } from '../lib/theme';
import type { Account, Asset, AssetKey, Balance, Chain } from '../lib/types';
import {
  WALLETCONNECT_ID,
  getWalletOptions,
  initWalletConnect,
  onWalletsChanged,
  parseChainId,
  restoreWalletConnect,
  startDiscovery,
  type Eip1193Provider,
  type WalletOption,
} from '../lib/wallet';

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
  onDisconnect: () => void;
}

type ConnectedOption = WalletOption & { provider: Eip1193Provider };

/**
 * "I do not understand these parameters" — the JSON-RPC -32602 answer, in every dress wallets put
 * it in (MetaMask nests the real error under `data.originalError`, ethers under `info.error`).
 * A user rejection (4001) is explicitly NOT this: retrying a rejection in another shape re-prompts
 * someone who already said no.
 */
export function isBadParams(e: unknown): boolean {
  if (!e || typeof e !== 'object') return false;
  const o = e as {
    code?: number | string;
    message?: string;
    data?: { originalError?: { code?: number | string; message?: string } };
    error?: { code?: number | string; message?: string };
    info?: { error?: { code?: number | string; message?: string } };
  };
  const codes = [o.code, o.data?.originalError?.code, o.error?.code, o.info?.error?.code];
  if (codes.some((c) => c === 4001 || c === 'ACTION_REJECTED')) return false;
  if (codes.some((c) => c === -32602)) return true;
  const text = [o.message, o.data?.originalError?.message, o.error?.message, o.info?.error?.message].filter(Boolean).join(' ');
  return /invalid (method )?param|params? (are|is) invalid|unsupported (message )?format|must be a (utf-?8 )?string/i.test(text);
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
  const optionRef = useRef<WalletOption | null>(null);
  const addressRef = useRef<string | null>(null);
  const autoTried = useRef(false);
  addressRef.current = address;

  const detach = useCallback(() => {
    const a = attached.current;
    if (!a) return;
    a.provider.removeListener?.('accountsChanged', a.onAccounts);
    a.provider.removeListener?.('chainChanged', a.onChain);
    a.provider.removeListener?.('disconnect', a.onDisconnect);
    attached.current = null;
  }, []);

  const disconnect = useCallback(() => {
    const a = attached.current;
    detach();
    // a WalletConnect session outlives the page unless ended explicitly
    if (a && optionRef.current?.source === 'walletconnect') a.provider.disconnect?.().catch(() => undefined);
    optionRef.current = null;
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
    async (o: ConnectedOption, accounts: string[]) => {
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
      /**
       * WalletConnect's `disconnect` IS the session ending. An injected wallet's is not: MetaMask,
       * OKX and Rabby all emit it when their own RPC hiccups, with the account still connected and
       * every later request still answered — a wallet that "disconnected" three times an hour while
       * nothing was wrong. So for an injected provider the event is a question: ask for the
       * accounts, and end the session only when there are none. A provider that cannot even answer
       * is having the hiccup the event is about, and is left alone.
       */
      const onDisconnect = () => {
        if (o.source === 'walletconnect') {
          disconnect();
          return;
        }
        void p.request({ method: 'eth_accounts' }).then(
          (accs) => {
            if (!Array.isArray(accs) || accs.length === 0) disconnect();
          },
          () => undefined,
        );
      };
      p.on?.('disconnect', onDisconnect);
      attached.current = { provider: p, onAccounts, onChain, onDisconnect };
      optionRef.current = o;
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
        if (o.disabledReason) throw new Error(`${o.name} is ${o.disabledReason}`);
        const provider = o.provider ?? (o.source === 'walletconnect' ? await initWalletConnect() : null);
        if (!provider) throw new Error(`${o.name} has no provider`);
        // WalletConnect pairs through its QR modal in connect(); request() throws until a session exists
        if (o.source === 'walletconnect' && !provider.session) await provider.connect?.();
        const accs = (await provider.request({ method: 'eth_requestAccounts' })) as string[];
        // A locked wallet answers [] instead of throwing (MetaMask, OKX, Bitget all do it). "The
        // wallet granted no account" left the user staring at the picker with nothing to do.
        if (!Array.isArray(accs) || !accs.length) throw new Error(`Unlock your ${o.name} wallet and try again — it granted no account`);
        await attach({ ...o, provider }, accs);
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
        // WalletConnect: only when a session survived the reload; never open the QR modal unasked
        const provider = last === WALLETCONNECT_ID ? await restoreWalletConnect() : o.provider;
        const accs = provider ? ((await provider.request({ method: 'eth_accounts' })) as string[]) : [];
        if (provider && Array.isArray(accs) && accs.length) await attach({ ...o, provider }, accs);
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
    // The canonical, unpadded EIP-3326/EIP-3085 form — `chainIdHex` and nothing else. A padded
    // "0x01" is what made a wallet that already had Ethereum answer 4902 and land the user in the
    // Add-chain branch below (see lib/chains.ts).
    const hex = chainIdHex(target);
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
      // Cancel is an answer. Reading it as "the wallet does not know this chain" would follow the
      // refusal with an Add-chain prompt — the same user, asked again, harder.
      if (rejectedByUser(e)) throw e;
      const unknownChain = code === 4902 || /unrecognized|not (been )?added|4902|unsupported chain|unknown chain/i.test(err?.message ?? '');
      if (!unknownChain) throw e;
      // `chainMeta` and not the raw table: the parameters carry the endpoint this browser actually
      // reads the chain through (T54), so a wallet that adds the chain lands on the same node.
      const meta = chainMeta(target);
      if (!meta) throw new Error(`Your wallet does not know chain ${target} and Pgas.me has no parameters to add it`);
      await p.request({ method: 'wallet_addEthereumChain', params: [{ chainId: hex, ...meta }] });
      if ((await readChain()) !== target) await p.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: hex }] });
    }
    const now = await readChain();
    if (now !== null && now !== target) throw new Error(`The wallet is still on chain ${now} — switch it to chain ${target} and retry`);
    setChainId(target);
  }, []);

  /**
   * The one signature Pgas.me ever asks for: the EIP-4361 (SIWE) sign-in message.
   *
   * EIP-191 says `personal_sign` takes the message hex-encoded, and that is what is sent first —
   * it is the only form that survives a message with a non-ASCII character. Coin98 and Binance
   * Web3 answer that with -32602 "invalid params" and want the plain UTF-8 string; a couple of
   * in-app browsers want the two parameters the other way round. So a REFUSAL of the shape (and
   * only that: never a user rejection, never a wallet error) is retried in the next shape.
   *
   * ⛔ Only a refusal retries. A wallet that signed is never asked to sign again — two signatures
   * of one nonce is two sign-ins the user did not ask for, and the second prompt is what trains
   * people to click through prompts without reading them.
   */
  const personalSign = useCallback(async (message: string, from?: string) => {
    const p = attached.current?.provider;
    const addr = from ?? addressRef.current;
    if (!p || !addr) throw new Error('Connect a wallet first');
    const shapes: unknown[][] = [
      [hexlify(toUtf8Bytes(message)), addr], // EIP-191: hex message, then the address
      [message, addr], // Coin98 / Binance Web3: the plain string
      [addr, message], // a few in-app browsers read the parameters the other way round
    ];
    let last: unknown = null;
    for (let i = 0; i < shapes.length; i++) {
      try {
        const sig = await p.request({ method: 'personal_sign', params: shapes[i] });
        if (typeof sig !== 'string' || !sig.startsWith('0x')) throw new Error('The wallet returned no signature');
        return sig;
      } catch (e) {
        last = e;
        if (i === shapes.length - 1 || !isBadParams(e)) throw e;
      }
    }
    throw last; // unreachable: the loop either returns or throws
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
  /** The address auto sign-in has already asked for: a refusal is never re-asked by itself. */
  const autoAskedFor = useRef<string | null>(null);

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

  // a wallet disconnect ends the session too — and re-arms auto sign-in, so connecting again asks
  useEffect(() => {
    if (disconnectSignal === 0) return;
    clearSession();
    setAccount(null);
    setExpired(false);
    autoAskedFor.current = null;
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

  /**
   * The account IS the wallet, so a wallet that switches account switches account here: the old
   * session is dropped and the effect below signs the new address in. Without this the header sat
   * on "signed in as someone else" and every locked page showed another wallet's money.
   */
  useEffect(() => {
    if (!wallet.address || !session) return;
    if (session.address.toLowerCase() === wallet.address.toLowerCase()) return;
    clearSession();
    setAccount(null);
    setExpired(false);
  }, [wallet.address, session]);

  /**
   * Sign in the moment the wallet connects (admin 2026-09-09): connecting and signing in were two
   * clicks for one intention, and the second one is free and moves nothing. Asked ONCE per address
   * — a user who rejects the signature gets the Sign-in card, not a wallet that keeps prompting.
   */
  useEffect(() => {
    const addr = wallet.address;
    if (!addr || !wallet.provider || session || signingIn) return;
    if (autoAskedFor.current === addr.toLowerCase()) return;
    autoAskedFor.current = addr.toLowerCase();
    void signIn().catch(() => undefined); // the error is on `signInError`; the gate offers a retry
  }, [wallet.address, wallet.provider, session, signingIn, signIn]);

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
  /**
   * Which ingress paths the API states are open — what `/v1/assets` and `/v1/health` said, and only
   * what they said. Resolve it with `resolveIngress()` (lib/ingress.ts), which supplies the
   * defaults and lets the account have the last word when the public reads stated nothing.
   */
  ingress: IngressPartial;
  /** The tokens the Uniswap route is registered for, when the API names them; else null. */
  uniswapTokens: string[] | null;
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
  const [ingress, setIngress] = useState<IngressPartial>({});
  const [uniswapTokens, setUniswapTokens] = useState<string[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    void (async () => {
      const [c, a, d, h] = await Promise.allSettled([api.chains(), api.assets(), api.dexAssets(), api.health()]);
      if (!alive) return;
      const errs: string[] = [];
      if (c.status === 'fulfilled') setChains(Array.isArray(c.value.chains) ? c.value.chains : []);
      else errs.push(`chains: ${errorText(c.reason)}`);
      if (a.status === 'fulfilled') setAssets(Array.isArray(a.value.assets) ? a.value.assets : []);
      else errs.push(`assets: ${errorText(a.reason)}`);
      /**
       * The ingress flags, from wherever this API build publishes them: `/dex/assets` is where the
       * 2026-09-10 API puts them (with the registered pairs), `/assets` and `/health` are read in
       * case a build puts them there instead, and the account has the last word only when none of
       * the three said anything (Deposit merges it in). A build that publishes none of it — or
       * 404s these routes — states nothing, and nothing is exactly what it contributes: the
       * defaults in lib/ingress.ts stand, and `uniswap` is off until an API says otherwise.
       * None of this is an error, so none of it colours the page red.
       */
      const dex = d.status === 'fulfilled' ? d.value : null;
      const assetsPayload = a.status === 'fulfilled' ? a.value : null;
      const health = h.status === 'fulfilled' ? h.value : null;
      setIngress({ ...ingressPartial(health), ...ingressPartial(assetsPayload), ...ingressPartial(dex) });
      setUniswapTokens(uniswapTokenList(dex) ?? uniswapTokenList(assetsPayload) ?? uniswapTokenList(health));
      setError(errs.length ? errs.join(' · ') : null);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [generation]);

  const chainById = useCallback((id: number) => chains.find((c) => c.chain_id === id), [chains]);
  const resolveEvmChainId = useCallback((id: number) => id, []);
  const reload = useCallback(() => setGeneration((n) => n + 1), []);

  return useMemo<DataState>(
    () => ({ chains, assets, ingress, uniswapTokens, loading, error, reload, chainById, resolveEvmChainId }),
    [chains, assets, ingress, uniswapTokens, loading, error, reload, chainById, resolveEvmChainId],
  );
}

// ---------- route ----------
/**
 * T57 (admin 2026-09-12: "Don't you think we should have DEPOSIT and WITHDRAW at the same page?").
 * Putting money in and taking it out are ONE page with two modes — they are the same sentence
 * ("you pay X, you receive Y") read in two directions — so there is one tab for both, and the
 * direction is a segmented control in the panel's head, where a DEX puts buy and sell.
 */
export type Tab = 'money' | 'balance' | 'how';
export type MoneyMode = 'deposit' | 'withdraw';

/**
 * The things you DO with money. `label` is the desktop nav's; `short` is the phone bar's, where
 * three columns share 390 px and "Deposit & withdraw" would be an ellipsis.
 */
export const TABS: { id: Tab; label: string; short: string; path: string }[] = [
  { id: 'money', label: 'Deposit & withdraw', short: 'Move', path: '/deposit' },
  { id: 'balance', label: 'Balance', short: 'Balance', path: '/balance' },
];

/**
 * `/how-it-works` (T44) is a ROUTE but not a TAB: `TABS` is what you DO, and this is a page you
 * read once. It keeps its own link in the header (desktop) and the footer (everywhere) — and,
 * since T57 left the phone bar with a spare column, a third column there too. Those two places
 * are now the ONLY ones: the lede link and the explainer's were three and four of four (T57
 * defect 1), and a page with four routes to one explainer is a page that cannot say what it wants.
 */
export const HOW_PATH = '/how-it-works';
export const MODE_PATHS: Record<MoneyMode, string> = { deposit: '/deposit', withdraw: '/withdraw' };
/** The name this page had until T57. It is in old links, DMs and bookmarks, so it still lands. */
export const LEGACY_WITHDRAW_PATH = '/schedule';
const PATHS: Record<Tab, string> = {
  money: MODE_PATHS.deposit,
  balance: '/balance',
  how: HOW_PATH,
};

export interface RouteState {
  tab: Tab;
  /** Which direction the money page is pointing; only meaningful while `tab === 'money'`. */
  mode: MoneyMode;
  navigate(tab: Tab): void;
  goMoney(mode: MoneyMode): void;
}

interface Place {
  tab: Tab;
  mode: MoneyMode;
}

function placeFromPath(path: string): Place {
  const p = path.replace(/\/+$/, '') || '/';
  if (p === '/activity') return { tab: 'balance', mode: 'deposit' }; // the old bookmark still lands on the timeline
  if (p === '/balance') return { tab: 'balance', mode: 'deposit' };
  if (p === HOW_PATH) return { tab: 'how', mode: 'deposit' };
  // `/withdraw` and its old name both point the one page the other way
  if (p === MODE_PATHS.withdraw || p === LEGACY_WITHDRAW_PATH) return { tab: 'money', mode: 'withdraw' };
  return { tab: 'money', mode: 'deposit' };
}

function useRouteState(): RouteState {
  const [place, setPlace] = useState<Place>(() => placeFromPath(window.location.pathname));
  /**
   * ⛔ The `/schedule` redirect REPLACES rather than pushes. A pushed redirect puts the old path
   * back in the history one entry down, so Back lands on it, which redirects again — a trap the
   * user cannot get out of with the control they reached for.
   */
  useEffect(() => {
    const fix = () => {
      if (window.location.pathname.replace(/\/+$/, '') === LEGACY_WITHDRAW_PATH) {
        window.history.replaceState(null, '', MODE_PATHS.withdraw);
      }
    };
    fix();
    const onPop = () => {
      setPlace(placeFromPath(window.location.pathname));
      fix();
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  const go = useCallback((next: Place) => {
    const path = next.tab === 'money' ? MODE_PATHS[next.mode] : PATHS[next.tab];
    if (window.location.pathname !== path) window.history.pushState(null, '', path);
    setPlace(next);
    window.scrollTo({ top: 0 });
  }, []);
  const navigate = useCallback((t: Tab) => go({ tab: t, mode: 'deposit' }), [go]);
  const goMoney = useCallback((m: MoneyMode) => go({ tab: 'money', mode: m }), [go]);
  return useMemo(() => ({ tab: place.tab, mode: place.mode, navigate, goMoney }), [place, navigate, goMoney]);
}

// ---------- theme ----------
export interface ThemeState {
  /** The theme in force — never "system": the OS is resolved to one of these two. */
  theme: ThemeChoice;
  /** Whether that came from the OS (no stored choice) — the toggle's title says so. */
  fromSystem: boolean;
  toggle(): void;
}

function useThemeState(): ThemeState {
  const [theme, setTheme] = useState<ThemeChoice>(() => currentTheme());
  const [fromSystem, setFromSystem] = useState<boolean>(() => storedTheme() === null);

  // while the user has made no choice of their own, the OS keeps the last word
  useEffect(
    () =>
      onSystemThemeChange((t) => {
        if (storedTheme() !== null) return;
        applyTheme(t);
        setTheme(t);
      }),
    [],
  );

  const toggle = useCallback(() => {
    const next: ThemeChoice = currentTheme() === 'dark' ? 'light' : 'dark';
    persistTheme(next);
    setTheme(next);
    setFromSystem(false);
  }, []);

  return useMemo<ThemeState>(() => ({ theme, fromSystem, toggle }), [theme, fromSystem, toggle]);
}

// ---------- the store ----------
export interface Store {
  wallet: WalletState;
  session: SessionState;
  data: DataState;
  route: RouteState;
  theme: ThemeState;
}

const StoreContext = createContext<Store | null>(null);

export function StoreProvider({ children }: { children: ReactNode }) {
  const [disconnects, setDisconnects] = useState(0);
  const onDisconnect = useCallback(() => setDisconnects((n) => n + 1), []);
  const wallet = useWalletState(onDisconnect);
  const session = useSessionState(wallet, disconnects);
  const data = useDataState();
  const route = useRouteState();
  const theme = useThemeState();
  const value = useMemo<Store>(() => ({ wallet, session, data, route, theme }), [wallet, session, data, route, theme]);
  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const v = useContext(StoreContext);
  if (!v) throw new Error('useStore outside StoreProvider');
  return v;
}
