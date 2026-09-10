// A transaction that was SENT and not yet registered (T31 H, admin 2026-09-10: "Registering the
// deposit… The transaction was sent (view) but Pgas.me could not register it — retry above").
//
// The money left the wallet the moment the wallet returned a hash. Everything after that is
// bookkeeping — and bookkeeping that asks the user to press a button is bookkeeping that gets lost
// when they close the tab. So the hash is written to storage BEFORE `POST /v1/deposits` is called,
// the call retries itself with backoff, and a reload picks the record back up.
//
// ⛔ The record is cleared on a 2xx or a DEFINITIVE 4xx only. A 409 ("that transaction is not
// visible on Ethereum yet"), a 5xx and a network failure are all "ask again later" — clearing on
// one of those is how a sent transaction becomes a deposit nobody ever registered.
import { ApiError } from './api';

export interface PendingRegistration {
  quote_id: string;
  hash: string;
  /** the chain the transaction went to, so the "view" link points at the right explorer */
  chain_id: number;
  /** ms since epoch — what the retry window is measured from */
  sent_at: number;
  /** the wallet that sent it: a record belongs to one account, never to whoever signs in next */
  address?: string;
}

const KEY = 'pgas.pending-deposit.v1';

/** Retries: 3 s, 6 s, 12 s, 24 s … capped at a minute, for at most REGISTER_WINDOW_MS. */
export const REGISTER_WINDOW_MS = 15 * 60 * 1000;
const FIRST_DELAY_MS = 3000;
const MAX_DELAY_MS = 60_000;

export function backoffMs(attempt: number): number {
  return Math.min(MAX_DELAY_MS, FIRST_DELAY_MS * 2 ** Math.max(0, attempt));
}

export function loadPending(): PendingRegistration | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as PendingRegistration;
    if (!p || typeof p.quote_id !== 'string' || typeof p.hash !== 'string' || !p.hash.startsWith('0x')) return null;
    return { ...p, sent_at: typeof p.sent_at === 'number' ? p.sent_at : Date.now() };
  } catch {
    return null;
  }
}

export function savePending(p: PendingRegistration): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    // storage unavailable: the retry loop in this page still runs, it just does not survive a reload
  }
}

export function clearPending(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // nothing to clear
  }
}

/**
 * Should this failure be tried again? Yes for "not yet" and "not now": 409 (the hash is not visible
 * on Ethereum yet), any 5xx, and the transport failure `ApiError(0)`. No for a definitive refusal —
 * 400/401/403/404/422 are the API saying this registration will never succeed, and repeating it
 * would be a loop with a sentence the user is never shown.
 */
export function retryableRegistration(e: unknown): boolean {
  if (!(e instanceof ApiError)) return true; // an unreadable failure is not evidence of a refusal
  if (e.status === 0) return true; // "The Pgas.me API is unreachable"
  if (e.status === 409) return true;
  return e.status >= 500;
}
