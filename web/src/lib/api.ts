// Typed client for API_CONTRACT.md plus the session it authenticates with. Same-origin
// `/api/v1/...`; Bearer on locked routes; a 401 on a locked route clears the session and tells the
// UI to ask for a fresh sign-in. The account IS the wallet — the session holds a token, nothing else.
import type {
  Account,
  AddDestinationBody,
  Asset,
  Chain,
  Deposit,
  Destination,
  NonceResponse,
  Quote,
  QuoteBody,
  Stats,
  Token,
  VerifyResponse,
  WithdrawalBody,
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

/** One plain sentence for any failure: API detail, wallet rejection, RPC error or a generic Error. */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.detail;
  if (e && typeof e === 'object') {
    const o = e as { code?: number | string; message?: string; info?: { error?: { message?: string } } };
    if (o.code === 4001 || o.code === 'ACTION_REJECTED') return 'You rejected the request in the wallet';
    if (o.info?.error?.message) return o.info.error.message;
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

  destinations: () => request<{ destinations: Destination[] }>('/destinations', { auth: true }),
  destinationNonce: () => request<{ nonce: string; template: string }>('/destinations/nonce', { auth: true }),
  addDestination: (body: AddDestinationBody) => request<Destination>('/destinations', { method: 'POST', body, auth: true }),
  removeDestination: (address: string) => request<{ removed: string }>(`/destinations/${address}`, { method: 'DELETE', auth: true }),

  chains: () => request<{ chains: Chain[] }>('/dex/chains'),
  tokens: (chainId: number) => request<{ tokens: Token[] }>(`/dex/tokens?chain_id=${chainId}`),
  assets: () => request<{ assets: Asset[] }>('/assets'),

  quote: (body: QuoteBody, signal?: AbortSignal) => request<Quote>('/quote', { method: 'POST', body, auth: true, signal }),
  registerDeposit: (quote_id: string, src_tx_hash: string) =>
    request<{ deposit_id: string; status: string }>('/deposits', { method: 'POST', body: { quote_id, src_tx_hash }, auth: true }),
  deposit: (id: string) => request<Deposit>(`/deposits/${id}`, { auth: true }),

  withdraw: (body: WithdrawalBody) => request<WithdrawalResponse>('/withdrawals', { method: 'POST', body, auth: true }),
  cancelWithdrawal: (id: string) => request<{ cancelled: string }>(`/withdrawals/${id}/cancel`, { method: 'POST', auth: true }),

  stats: () => request<Stats>('/stats'),
};
