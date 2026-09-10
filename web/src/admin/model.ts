/**
 * What the payloads MEAN (T49) — the readings the console is designed around, kept out of the
 * views so each one is a pure function a test can drive.
 *
 * Two of them earn their place:
 *
 *  * **the ladders.** A status word is a fact; where that word sits in the machine is the thing an
 *    operator reads at a glance. `credited` and `claiming` are both "fine, still moving"; `held`
 *    and `expired` are both "stopped", and only one of them is anybody's fault. A pill cannot say
 *    that. A five-step ladder with the current step lit can.
 *  * **`needs()`.** The Overview tab is not a summary, it is a queue: everything the machine
 *    cannot finish by itself, newest first, each line carrying WHAT, WHY and SINCE. Every rule
 *    below names the field it read, and a rule that cannot read its field says so rather than
 *    rendering an absence as an all-clear (law 8 — an unreadable query is not evidence).
 */
import { get, isPlainObject, type Row } from '../lib/adminApi';
import { agoSeconds, middle, span } from './fmt';
import type { Tone } from './ui';

export type TabId = 'overview' | 'deposits' | 'payouts' | 'unattributed' | 'accounts' | 'treasury' | 'events';

/** The first of these keys the row actually carries. Rows outlive field names. */
export function pick(row: unknown, ...names: string[]): unknown {
  for (const n of names) {
    const v = get(row, n);
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return undefined;
}

export function str(v: unknown): string {
  return typeof v === 'string' ? v : v === undefined || v === null ? '' : String(v);
}

export function num(v: unknown): number | null {
  const n = typeof v === 'string' ? Number(v) : v;
  return typeof n === 'number' && Number.isFinite(n) ? n : null;
}

export function rowsOf(payload: unknown, ...keys: string[]): Row[] {
  if (Array.isArray(payload)) return payload.filter(isPlainObject) as Row[];
  if (!isPlainObject(payload)) return [];
  for (const k of [...keys, 'rows', 'items', 'results', 'data']) {
    const v = payload[k];
    if (Array.isArray(v) && v.every(isPlainObject)) return v as Row[];
  }
  return [];
}

export function idOf(row: Row): string {
  for (const k of ['_id', 'id', 'deposit_id', 'request_id', 'account_id', 'event_id', 'msg_id']) {
    const v = row[k];
    if (typeof v === 'string' && v) return v;
    if (typeof v === 'number') return String(v);
  }
  return '';
}

// ──────────────────────────────────────────────────────────────────────────── ladders

export interface Rung {
  steps: string[];
  at: number;
  tone: Tone;
  name: string;
  /** the quiet line under the ladder: what a row that is still moving is waiting for */
  side: string | null;
  /**
   * ⛔ THE REASON A ROW IS *STOPPED*, and only that. It is drawn as a stripe across the whole
   * table, so it must be reserved for rows that are somebody's problem — held, delayed, failed.
   * A progress note on a row that is moving normally ("the claim is in flight") belongs on the
   * ladder's own line; promoting it to a stripe makes a healthy table look like an incident.
   */
  stripe: string | null;
}

export const DEPOSIT_STEPS = ['submitted', 'locked', 'confirming', 'credited', 'claimed'];
export const PAYOUT_STEPS = ['scheduled', 'released', 'bridging', 'delivering', 'delivered'];

/** Where a deposit is, from its two status fields — the ingress one and the treasury one. */
export function depositRung(row: Row): Rung {
  const status = str(pick(row, 'status'));
  const treasury = str(pick(row, 'treasury'));
  const hold = str(pick(row, 'hold_reason', 'hold.reason'));
  const note = str(pick(row, 'note'));
  const confs = num(pick(row, 'eth.confirmations', 'confirmations'));
  const need = num(pick(row, 'eth.confirmations_required', 'confirmations_required')) ?? 12;
  const stripe = hold ? `waiting: ${hold}` : null;
  const side = stripe ? null : note || null;
  const rung = (at: number, tone: Tone, name: string): Rung => ({ steps: DEPOSIT_STEPS, at, tone, name, side, stripe });

  if (status === 'failed' || status === 'expired')
    return {
      steps: DEPOSIT_STEPS,
      at: 1,
      tone: 'crit',
      name: status,
      side: null,
      stripe: stripe ?? note ?? 'nothing further will happen to this row',
    };
  if (status === 'submitted' || status === 'order_seen') return rung(0, 'warn', status);
  if (status === 'fallback_pending') return rung(1, 'warn', 'fallback pending');
  if (status === 'locked') return rung(1, 'warn', 'locked');
  if (status === 'confirming') return rung(2, 'warn', confs === null ? 'confirming' : `confirming ${confs}/${need}`);
  // credited — and then the treasury's own leg
  if (treasury === 'claiming') return rung(3, 'warn', 'claiming');
  if (treasury === 'shielding') return rung(4, 'warn', 'shielding');
  if (treasury === 'shielded') return rung(4, 'good', 'shielded');
  if (treasury === 'claimed') return rung(4, 'good', 'claimed');
  if (status === 'credited') return rung(3, 'good', 'credited');
  return rung(0, 'neutral', status || 'unknown');
}

/** Where a payout order is. `held`, `delayed` and `cancelled` are side states, not steps. */
export function payoutRung(row: Row): Rung {
  const status = str(pick(row, 'status'));
  const reason = str(pick(row, 'hold.reason', 'hold_reason'));
  const nextTry = pick(row, 'next_try_at', 'next_attempt_at');
  const later = nextTry ? `next try ${relative(nextTry)}` : '';
  const both = (s: string) => [s, later].filter(Boolean).join(' · ') || null;
  const moving = (at: number, tone: Tone, name: string): Rung => ({
    steps: PAYOUT_STEPS,
    at,
    tone,
    name,
    side: later || null,
    stripe: null,
  });
  const stopped = (tone: Tone, name: string, why: string): Rung => ({
    steps: PAYOUT_STEPS,
    at: 0,
    tone,
    name,
    side: null,
    stripe: both(why),
  });

  if (status === 'held') return stopped('crit', 'held', reason || 'a human owns this row');
  if (status === 'delayed') return stopped('warn', 'delayed', reason || 'a gate is holding it');
  if (status === 'failed' || status === 'refunded') return stopped('crit', status, reason || 'a legacy terminal row');
  if (status === 'cancelled') return { steps: PAYOUT_STEPS, at: 0, tone: 'neutral', name: 'cancelled', side: null, stripe: null };
  if (status === 'scheduled') return moving(0, 'warn', 'scheduled');
  if (status === 'releasing' || status === 'paying') return moving(1, 'warn', status);
  if (status === 'bridging') return moving(2, 'warn', 'bridging');
  if (status === 'delivering') return moving(3, 'warn', 'delivering');
  if (status === 'sent' || status === 'delivered')
    return { steps: PAYOUT_STEPS, at: 4, tone: 'good', name: 'delivered', side: null, stripe: null };
  if (status.startsWith('waiting_for')) return moving(1, 'warn', status.replace(/_/g, ' '));
  return { steps: PAYOUT_STEPS, at: 0, tone: 'neutral', name: status || 'unknown', side: null, stripe: null };
}

function relative(v: unknown): string {
  const s = agoSeconds(v as number);
  if (s === null) return '—';
  return s >= 0 ? `${span(s)} ago` : `in ${span(-s)}`;
}

/** The statuses that mean a lock is still somebody's job. Everything else has been resolved. */
const LOCK_OPEN = ['open', 'unattributed', 'pending', 'new', ''];

export function lockIsOpen(row: Row): boolean {
  return LOCK_OPEN.includes(str(pick(row, 'status')).toLowerCase());
}

/**
 * The operator's only question about an unattributed lock is "will it credit by itself?" — so it
 * is answered in words on the row rather than left as five columns to reason over.
 */
export function lockVerdict(row: Row): { tone: Tone; text: string } {
  if (!lockIsOpen(row)) return { tone: 'good', text: `Resolved — ${str(pick(row, 'status'))}.` };
  const next = pick(row, 'next_try_at', 'next_attempt_at', 'retry_at');
  const tries = num(pick(row, 'tries', 'attempts'));
  const reason = str(pick(row, 'reason', 'note', 'last_reason'));
  const nextIn = next ? agoSeconds(next as number) : null;
  if (next && nextIn !== null && nextIn < 0)
    return { tone: 'warn', text: `Not yet — the scanner tries again in ${span(-nextIn)}${tries ? ` (try ${tries + 1})` : ''}.` };
  if (reason) return { tone: 'crit', text: `No — ${reason} A human must attribute it.` };
  return { tone: 'crit', text: 'No — nothing is retrying this row. A human must attribute it.' };
}

// ─────────────────────────────────────────────────────────────────────────── attention

export interface Need {
  key: string;
  tone: Tone;
  what: string;
  why: string;
  since?: unknown;
  go?: { tab: TabId; status?: string; label: string };
}

/** A worker whose newest trace is older than this is worth a line. The passes run in seconds. */
const WORKER_STALE_S = 600;
/** A deposit that has not moved in this long, in a state something should be moving. */
const DEPOSIT_STUCK_S = 15 * 60;
/** The refresher writes the hosted token lists every 6 h; past this it is behind. */
const TOKENS_STALE_S = 6.5 * 3600;
/** A crossing funded longer ago than this has missed the relayer's whole typical window. */
const CROSSING_SLOW_S = 2 * 3600;

export interface NeedsInput {
  overview: unknown;
  payouts: Row[];
  deposits: Row[];
  locks: Row[];
  now?: number;
}

export function needs({ overview, payouts, deposits, locks, now = Date.now() }: NeedsInput): Need[] {
  const out: Need[] = [];
  const o = isPlainObject(overview) ? overview : {};
  const add = (n: Need) => out.push(n);

  // ── posture that stops everything
  const kill = o.kill_switch;
  const engaged = kill === true || (isPlainObject(kill) && kill.engaged === true);
  if (engaged)
    add({
      key: 'kill',
      tone: 'crit',
      what: 'The kill switch is engaged',
      why: `${isPlainObject(kill) ? str(kill.file) || 'the stop file' : 'the stop file'} exists — the workers pause and the request path refuses.`,
    });

  const workers = isPlainObject(o.workers) ? o.workers : {};
  if (workers.enabled === false)
    add({
      key: 'workers-off',
      tone: 'crit',
      what: 'The workers are disabled',
      why: 'PGAS_WORKERS_ENABLED is off — nothing scans, claims, shields or releases.',
    });
  if (workers.paused === true)
    add({
      key: 'workers-paused',
      tone: 'crit',
      what: 'The workers are paused',
      why: 'Every loop is parked; the request path may still be serving.',
    });

  const lastPass = isPlainObject(workers.last_pass) ? workers.last_pass : {};
  for (const [name, v] of Object.entries(lastPass)) {
    if (!isPlainObject(v)) continue;
    const age = agoSeconds(v.at as number, now);
    if (v.at === null || v.at === undefined)
      add({ key: `worker-${name}`, tone: 'warn', what: `${name.replace(/_/g, ' ')} has left no trace`, why: str(v.source) });
    else if (age !== null && age > WORKER_STALE_S)
      add({
        key: `worker-${name}`,
        tone: 'warn',
        what: `${name.replace(/_/g, ' ')} last left a trace ${span(age)} ago`,
        why: str(v.source),
        since: v.at,
      });
  }

  // ── the money rows that are stopped
  for (const r of payouts) {
    const status = str(pick(r, 'status'));
    const reason = str(pick(r, 'hold.reason', 'hold_reason'));
    if (status === 'held')
      add({
        key: `pay-held-${idOf(r)}`,
        tone: 'crit',
        what: `Payout ${middle(idOf(r), 6, 4)} is held`,
        why: reason || 'no reason on the row — open it.',
        since: pick(r, 'hold.at', 'hold_at', 'status_at', 'created_at'),
        go: { tab: 'payouts', status: 'held', label: 'Payouts' },
      });
    else if (status === 'delayed')
      add({
        key: `pay-delayed-${idOf(r)}`,
        tone: 'warn',
        what: `Payout ${middle(idOf(r), 6, 4)} is delayed`,
        why: [
          reason || 'a gate is holding it',
          pick(r, 'next_try_at', 'next_attempt_at') ? `next try ${relative(pick(r, 'next_try_at', 'next_attempt_at'))}` : '',
        ]
          .filter(Boolean)
          .join(' · '),
        since: pick(r, 'status_at', 'hold_at', 'created_at'),
        go: { tab: 'payouts', status: 'delayed', label: 'Payouts' },
      });
  }

  for (const r of locks) {
    if (!lockIsOpen(r)) continue;
    const v = lockVerdict(r);
    add({
      key: `lock-${idOf(r)}`,
      tone: v.tone === 'crit' ? 'crit' : 'warn',
      what: `Bridge message ${str(pick(r, 'msg_id', 'msgId')) || middle(idOf(r), 6, 4)} is unattributed`,
      why: v.text,
      since: pick(r, 'at', 'created_at', 'seen_at'),
      go: { tab: 'unattributed', label: 'Unattributed' },
    });
  }

  for (const r of deposits) {
    const status = str(pick(r, 'status'));
    const treasury = str(pick(r, 'treasury'));
    const moving = status === 'confirming' || status === 'locked' || treasury === 'claiming' || treasury === 'shielding';
    if (!moving) continue;
    const at = pick(r, 'treasury_at', 'status_at', 'updated_at', 'created_at');
    const age = agoSeconds(at as number, now);
    if (age === null || age <= DEPOSIT_STUCK_S) continue;
    add({
      key: `dep-${idOf(r)}`,
      tone: 'warn',
      what: `Deposit ${middle(idOf(r), 6, 4)} has been ${treasury === 'claiming' || treasury === 'shielding' ? treasury : status} for ${span(age)}`,
      why: str(pick(r, 'hold_reason', 'note')) || 'nothing on the row says why — open it for its events.',
      since: at,
      go: { tab: 'deposits', label: 'Deposits' },
    });
  }

  // ── the wallet's own capacity
  const health = isPlainObject(o.health) ? o.health : {};
  const coins = isPlainObject(health.coins) ? health.coins : {};
  for (const [asset, v] of Object.entries(coins)) {
    if (!isPlainObject(v)) continue;
    const have = num(v.have);
    const target = num(v.target);
    if (have === null)
      add({
        key: `coins-${asset}`,
        tone: 'warn',
        what: `${asset} coin count is unreadable`,
        why: 'the wallet did not answer — "we could not look" is not "there are none".',
      });
    else if (target !== null && have < target)
      add({
        key: `coins-${asset}`,
        tone: have === 0 ? 'crit' : 'warn',
        what: `${asset} has ${have} of ${target} spendable coins`,
        why: 'Beam locks a whole UTXO per pending transaction — run `beam split`.',
      });
  }

  const crossings = isPlainObject(o.crossings) ? o.crossings : {};
  const oldest = num(crossings.oldest_age_s);
  if (oldest !== null && oldest > CROSSING_SLOW_S)
    add({
      key: 'crossing',
      tone: 'warn',
      what: `A funded crossing is ${span(oldest)} old`,
      why: `${num(crossings.orders) ?? 0} order(s) hold bETH at a crossing address that nothing has burned yet.`,
      go: { tab: 'payouts', label: 'Payouts' },
    });

  // ── the reference data the ingress depends on
  const tokens = isPlainObject(health.tokens) ? health.tokens : {};
  const failed = Array.isArray(tokens.failed) ? tokens.failed : [];
  if (tokens.updated_at === null || tokens.updated_at === undefined)
    add({
      key: 'tokens-none',
      tone: 'warn',
      what: 'The hosted token lists have never been written',
      why: 'the picker falls back to /v1/dex/tokens on every load.',
    });
  else if ((num(tokens.age_s) ?? 0) > TOKENS_STALE_S)
    add({
      key: 'tokens-stale',
      tone: 'warn',
      what: `The token lists are ${span(num(tokens.age_s))} old`,
      why: `the refresher writes them every 6 h; ${num(tokens.chains) ?? 0} chains on disk.`,
      since: tokens.updated_at,
    });
  if (failed.length > 0)
    add({
      key: 'tokens-failed',
      tone: 'warn',
      what: `${failed.length} token list${failed.length === 1 ? '' : 's'} failed to refresh`,
      why: `chain ids ${failed.join(', ')} kept their old file.`,
    });

  const dist = isPlainObject(o.distributor) ? o.distributor : {};
  if (dist.configured === true && str(dist.error))
    add({ key: 'distributor', tone: 'warn', what: 'The instant distributor is unhealthy', why: str(dist.error) });

  const gas = isPlainObject(health.gas) ? health.gas : {};
  if (num(gas.samples_24h) === 0)
    add({
      key: 'gas',
      tone: 'warn',
      what: 'No gas samples in the last 24 h',
      why: 'a crossing would be priced on one live read — the basis moved 0.66 → 2.18 gwei in an hour on 2026-09-10.',
    });

  const flags = isPlainObject(o.flags) ? o.flags : {};
  if (flags.ingress_armed === true && flags.ingress_ready === false)
    add({
      key: 'ingress',
      tone: 'warn',
      what: 'Ingress is armed but not ready',
      why: 'the arming flag is on and some asset has no usable pipe — quotes will refuse.',
    });

  const counts = isPlainObject(o.counts) ? o.counts : {};
  const unsent = num(counts.events_unsent);
  if (unsent !== null && unsent > 0)
    add({
      key: 'unsent',
      tone: 'warn',
      what: `${unsent} operator event${unsent === 1 ? '' : 's'} not sent`,
      why: 'the monitor drains events.notified — Telegram may be off, or the drain is behind.',
      go: { tab: 'events', label: 'Events' },
    });

  const wd = isPlainObject(o.watchdog) ? o.watchdog : {};
  if (str(wd.why))
    add({ key: 'watchdog', tone: 'warn', what: 'The watchdog log could not be read', why: `${str(wd.log)}: ${str(wd.why)}` });
  else if (str(wd.line) && !/\bOK\b|all green|absent/i.test(str(wd.line)))
    add({ key: 'watchdog', tone: 'warn', what: 'The watchdog reported a transition', why: str(wd.line), since: wd.at });

  const rank: Record<Tone, number> = { crit: 0, warn: 1, good: 2, neutral: 3 };
  return out.sort((a, b) => {
    if (rank[a.tone] !== rank[b.tone]) return rank[a.tone] - rank[b.tone];
    const ta = agoSeconds(a.since as number, now);
    const tb = agoSeconds(b.since as number, now);
    if (ta === null && tb === null) return 0;
    if (ta === null) return 1;
    if (tb === null) return -1;
    return ta - tb; // newest (smallest age) first
  });
}

// ────────────────────────────────────────────────────────── what a key MEANS, by its name

/**
 * ⛔ THE DRAWER IS NOT A JSON DUMP EITHER. The row's own fields are rendered through the same
 * readings the tables use — a `*_at` is a moment, a `*_groth` is an amount, a 0x-40 is an address
 * and a 0x-64 is a transaction — because "created at 1789046061" and "value groth 199990" in a
 * drawer are exactly the defect this redesign exists to remove, one click further in. Everything
 * VERBATIM lives under "Raw", which is what that disclosure is for.
 *
 * Conservative on purpose: a key this list does not know renders its raw value, which is unhelpful
 * but true. Guessing that any large number is a date prints a confident wrong timestamp for an
 * amount in wei.
 */
const TIME_WORDS = /^(at|ts|time|timestamp|since|seen|expires?|expiry|deadline|when|date|login|pass|try)$/;

export function isTimeKey(key: string): boolean {
  const k = key.toLowerCase();
  const tail = k.includes('_') ? k.slice(k.lastIndexOf('_') + 1) : k;
  return TIME_WORDS.test(k) || TIME_WORDS.test(tail) || k.endsWith('_at');
}

export function isGrothKey(key: string): boolean {
  const k = key.toLowerCase();
  return k === 'groth' || k.endsWith('_groth');
}

export function isWeiKey(key: string): boolean {
  const k = key.toLowerCase();
  return k === 'wei' || k.endsWith('_wei') || k.endsWith('_units');
}

/** Plausible epoch seconds or milliseconds — a BLOCK number must never render as a date. */
export function looksLikeEpoch(v: number): boolean {
  return Number.isFinite(v) && ((v > 1_000_000_000 && v < 100_000_000_000) || (v > 1e12 && v < 1e14));
}

export const HASH_RE = /^0x[0-9a-fA-F]{64}$/;
export const ADDR_RE = /^0x[0-9a-fA-F]{40}$/;
/** Beam kernel ids / txids: 64 hex with no `0x`. Shown and copyable, NEVER linked — the Beam
 *  explorer's transaction URL shape was not verified, and a link that 404s is worse than a value. */
export const BEAM_ID_RE = /^[0-9a-f]{64}$/i;
