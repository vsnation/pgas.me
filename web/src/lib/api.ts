// Typed client for API_CONTRACT.md plus the session it authenticates with. Same-origin
// `/api/v1/...`; Bearer on locked routes; a 401 on a locked route clears the session and tells the
// UI to ask for a fresh sign-in. The account IS the wallet — the session holds a token, nothing else.
import type {
  Account,
  ArmedQuote,
  AssetKey,
  AssetsResponse,
  Chain,
  Deposit,
  HealthResponse,
  NonceResponse,
  Quote,
  QuoteBody,
  Token,
  VerifyResponse,
  WithdrawalBody,
  WithdrawalFees,
  WithdrawalResponse,
} from './types';

const API_BASE = '/api/v1';

// ---------- session ----------
export interface Session {
  token: string;
  account_id: string;
  address: string;
  expires_at?: number;
}

const SESSION_KEY = 'pgas.session.v1';
export const SESSION_EVENT = 'pgas:session';
export const SESSION_EXPIRED_EVENT = 'pgas:session-expired';

export function getSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    if (!s || typeof s.token !== 'string' || typeof s.address !== 'string') return null;
    if (s.expires_at && Date.now() > s.expires_at) {
      localStorage.removeItem(SESSION_KEY);
      return null;
    }
    return s;
  } catch {
    return null;
  }
}

export function setSession(s: Session | null): void {
  try {
    if (s) localStorage.setItem(SESSION_KEY, JSON.stringify(s));
    else localStorage.removeItem(SESSION_KEY);
  } catch {
    // storage unavailable: the session lives only in memory for this page
  }
  window.dispatchEvent(new Event(SESSION_EVENT));
}

export function clearSession(): void {
  setSession(null);
}

// ---------- transport ----------
export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE';
  body?: unknown;
  auth?: boolean;
  signal?: AbortSignal;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (opts.auth) {
    const s = getSession();
    if (!s) throw new ApiError(401, 'Sign in with your wallet first');
    headers.Authorization = `Bearer ${s.token}`;
  }
  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new ApiError(0, 'The Pgas.me API is unreachable');
  }
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (res.status === 401 && opts.auth) {
    clearSession();
    window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }
  if (!res.ok) {
    const d = (data as { detail?: unknown } | null)?.detail;
    const detail = typeof d === 'string' ? d : d ? JSON.stringify(d) : `${res.status} ${res.statusText || 'error'}`;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

/**
 * Every EIP-1193 code a wallet can put a rejection in — its own `code`, MetaMask's nested
 * `data.originalError`, the `error` an in-app bridge wraps it in, and ethers' `info.error`. A 4001
 * that arrives one level down used to reach the screen as "Internal JSON-RPC error", which reads
 * like a broken app rather than "you pressed Cancel".
 */
function walletCodes(e: unknown): (number | string | undefined)[] {
  if (!e || typeof e !== 'object') return [];
  const o = e as {
    code?: number | string;
    data?: { originalError?: { code?: number | string } };
    error?: { code?: number | string };
    info?: { error?: { code?: number | string } };
    cause?: { code?: number | string };
  };
  return [o.code, o.data?.originalError?.code, o.error?.code, o.info?.error?.code, o.cause?.code];
}

/** The user pressed Cancel in the wallet — not a failure to retry, and never a loop. */
export function rejectedByUser(e: unknown): boolean {
  return walletCodes(e).some((c) => c === 4001 || c === 'ACTION_REJECTED');
}

/** One plain sentence for any failure: API detail, wallet rejection, RPC error or a generic Error. */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.detail;
  if (rejectedByUser(e)) return 'You rejected the request in the wallet';
  if (e && typeof e === 'object') {
    const o = e as { message?: string; info?: { error?: { message?: string } }; data?: { originalError?: { message?: string } } };
    if (o.info?.error?.message) return o.info.error.message;
    if (o.data?.originalError?.message) return o.data.originalError.message;
    if (typeof o.message === 'string') return o.message.length > 240 ? o.message.slice(0, 240) + '…' : o.message;
  }
  return String(e);
}

// ---------- routes ----------
export const api = {
  siweNonce: () => request<NonceResponse>('/siwe/nonce'),
  siweVerify: (message: string, signature: string) =>
    request<VerifyResponse>('/siwe/verify', { method: 'POST', body: { message, signature } }),

  account: (signal?: AbortSignal) => request<Account>('/account', { auth: true, signal }),

  chains: () => request<{ chains: Chain[] }>('/dex/chains'),
  tokens: (chainId: number) => request<{ tokens: Token[] }>(`/dex/tokens?chain_id=${chainId}`),
  /** The asset registry (ETH/DAI/WBTC), and on some builds the ingress flags alongside it. */
  assets: () => request<AssetsResponse>('/assets'),
  /**
   * Where the 2026-09-10 API publishes the ingress flags and the registered Uniswap pairs
   * (`ingress:{uniswap, xchain, direct, uniswap_tokens:[{address,symbol,decimals}]}`). Read for
   * those alone; a build without the route answers 404 and the caller carries on.
   */
  dexAssets: () => request<AssetsResponse>('/dex/assets'),
  /**
   * Public health, and the second place the flags are published. A build with no such route answers
   * 404 and the caller carries on — an unreadable read is not evidence that a path is open or
   * closed, and silence leaves the defaults in lib/ingress.ts standing.
   */
  health: () => request<HealthResponse>('/health'),

  quote: (body: QuoteBody, signal?: AbortSignal) => request<Quote>('/quote', { method: 'POST', body, auth: true, signal }),
  /**
   * Builds the order for a cross-chain quote — the slow half of the old one-shot quote, moved
   * behind the Deposit click (T2b). Idempotent: a second call returns the stored tx while fresh.
   */
  armQuote: (quote_id: string) => request<ArmedQuote>(`/quote/${quote_id}/arm`, { method: 'POST', auth: true }),
  registerDeposit: (quote_id: string, src_tx_hash: string) =>
    request<{ deposit_id: string; status: string }>('/deposits', { method: 'POST', body: { quote_id, src_tx_hash }, auth: true }),
  deposit: (id: string) => request<Deposit>(`/deposits/${id}`, { auth: true }),

  withdrawalFees: (asset: AssetKey) => request<WithdrawalFees>(`/withdrawals/fees?asset=${asset}`, { auth: true }),
  withdraw: (body: WithdrawalBody) => request<WithdrawalResponse>('/withdrawals', { method: 'POST', body, auth: true }),
  cancelWithdrawal: (id: string) =>
    request<{ cancelled: string; refunded_groth?: number }>(`/withdrawals/${id}/cancel`, { method: 'POST', auth: true }),
};
