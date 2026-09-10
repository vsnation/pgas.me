// One payout order, as a person reads it — the T40 vocabulary in ONE place (2026-09-10).
//
// Admin, looking at his own Balance page: "You understand that withdrawals on user's side cannot be
// failed … You should show all statuses there and estimated time of arrival of his asset."
//
// So the word **Failed** is not in this file's vocabulary for anything the system did to itself. An
// internal cause — a Beam send refused, a relayer-fee spike, no free coin, an unreadable wallet, a
// dropped Ethereum tx — is `delayed`: the money stays reserved and the processor retries with
// backoff (API_CONTRACT.md § Withdrawals, "A withdrawal never fails on the user's side"). A row a
// human must look at is `held`, which is the same thing to the user and is shown as delayed. The
// only way an order ends without arriving is the user cancelling it, and that row says `Cancelled`
// and names the refund.
//
// TWO RULES THIS FILE OWNS, BOTH OF THEM THE SAME RULE:
//
//   1. ⛔ IT NEVER COMPUTES AN ETA, A DELIVERED AMOUNT OR A FEE MODE. `eta_at`, `eta_note`,
//      `delivered_groth`, `debited_groth` and `fee_mode` are written by ONE function on the API
//      side (`payouts.eta_for`, `withdrawals.price_items`) and are read here or not read at all.
//      A build that does not publish them gets a dash — law 9: two implementations of one fact
//      will disagree, and one of them will reach money. A dash is "the API did not say", which is
//      true; a number this file made up would be a second answer.
//   2. A LEGACY `failed` ROW IS NOT EVIDENCE OF ANYTHING BUT ITS OWN HISTORY. Rows written before
//      2026-09-10 carry the terminal status the processor no longer writes. Such a row is shown as
//      **Returned to balance** when the ledger carries the `cancel` entry that gave the money back
//      (the two orders of 2026-09-10 10:31Z), and as **Delayed** otherwise — with "being retried"
//      said ONLY when the API published a `next_attempt_at`, because that is the API's claim to
//      make, not this file's.
//
//   3. ⛔ AND THE WORD "REFUNDED" IS NOT IN THE VOCABULARY EITHER (T48). Admin 2026-09-10 15:24Z,
//      reading his own Balance page: "Avoid status Refunded, it's not clear for the user … Better
//      to retry this deposit instead of just refunded back. Refunded back to the balance or what?"
//      That is the question the word leaves behind, so the pill answers it — the money is back in
//      Available, nothing was sent, and the order can be asked for again in one press. `refunded`
//      survives as the internal KEY (`data-shown`, the ledger's `cancel` entry is still what
//      proves it) and reaches nobody's eyes.
import { BEAM_CONFIRMATIONS, holdReasonText } from '../components/Status';
import { fmtGroth, toDate } from './format';
import type { HistoryEntry, PayoutRequest, RequestStatus, WithdrawalFeeMode } from './types';

/** The 2026-09-10 order machine: the statuses of `RequestStatus` plus the two T40 additions. */
export type PayoutStatus = RequestStatus | 'delayed' | 'held' | 'paying';

/** How the fees were charged for one order — one name for it, declared with the other API types. */
export type FeeMode = WithdrawalFeeMode;

/**
 * A payout row with the fields the 2026-09-10 API adds. Every one of them is optional: the live
 * API may not publish them yet, and `PayoutRequest` (which mirrors the contract's stable core) is
 * assignable to this type, so the pages read one shape whichever build answered.
 */
export type PayoutRow = Omit<PayoutRequest, 'status'> & {
  status: PayoutStatus;
  /** when the money is expected to BE in `W` (unix seconds) — the API's one ETA writer */
  eta_at?: number;
  /** the same ETA in plain words, written by the API ("bridge ≈ 1 h, up to 18 h in the tail") */
  eta_note?: string;
  /** how long the relayer's tail can stretch past `eta_at`, in seconds */
  eta_tail_s?: number;
  /** a delayed order's next attempt (unix seconds) */
  next_attempt_at?: number;
  /**
   * The same next attempt as an ISO-8601 instant. It is the field a PAGE reads: a unix integer
   * is a machine value, and the one place it ever reached a person was inside an `eta_note`
   * ("next try at 1788950000"), which is the defect this replaces.
   */
  next_try_at?: string;
  /**
   * Whether the API will still take a cancel for this order — `payouts.cancellable`, published on
   * the row since T40. It is THE answer (law 9: the button a client draws and the answer
   * `POST /v1/withdrawals/{id}/cancel` gives must be one decision); the status rule below is only
   * what a build that does not publish it leaves us with.
   */
  cancellable?: boolean;
  /**
   * ⛔ WHAT THE USER TYPED — and NOT `amount_groth`, which on a stored payout row is the
   * DELIVERY (`_write_items`: "what leaves to the user … Every reader of this field spends
   * it"). Reading `amount_groth` for "asked" printed the delivered number twice on both
   * tables, so a from-amount order read as if it had shrunk by nothing at all.
   */
  requested_groth?: number;
  /** what the wallet receives — less than `requested_groth` on a `from_amount` row */
  delivered_groth?: number;
  /** what came off Available for this order (amount + fees, or just the amount) */
  debited_groth?: number;
  fee_mode?: FeeMode;
};

/** What the pill is, in the one word the rest of the UI keys off. */
export type PayoutKey =
  'scheduled' | 'delayed' | 'releasing' | 'bridging' | 'delivering' | 'paying' | 'sent' | 'cancelled' | 'refunded' | 'waiting' | 'other';

export interface PayoutView {
  /** the row's status verbatim, for `data-status` — never rendered as words */
  status: string;
  key: PayoutKey;
  label: string;
  cls: string;
  /** why it is parked, in the user's words; null when there is nothing worth saying */
  reason: string | null;
  /** the API's own next attempt, when it published one */
  nextTry: number | null;
  /** the user may end this order and take the money back */
  cancellable: boolean;
  /** Beam confirmations so far, only while the bridge is counting them */
  confirmations: number | null;
}

const PILL: Record<PayoutKey, { label: string; cls: string }> = {
  scheduled: { label: 'Scheduled', cls: 'pill-indigo' },
  delayed: { label: 'Delayed', cls: 'pill-amber' },
  releasing: { label: 'Releasing', cls: 'pill-indigo' },
  bridging: { label: 'Bridging', cls: 'pill-magenta' },
  delivering: { label: 'Delivering', cls: 'pill-magenta' },
  // the instant path's own in-flight state (`payouts.PAYING`): an Ethereum transaction of ours is
  // out there with the user's money in it. In flight, like bridging — and not cancellable.
  paying: { label: 'Paying', cls: 'pill-magenta' },
  sent: { label: 'Sent', cls: 'pill-teal' },
  cancelled: { label: 'Cancelled', cls: '' },
  // ⛔ NOT "Refunded" — see rule 3 at the top of this file. The user's own cancel keeps
  // **Cancelled**, because they did it and they know what it was.
  refunded: { label: 'Returned to balance', cls: '' },
  waiting: { label: 'Waiting', cls: 'pill-amber' },
  other: { label: '', cls: '' },
};

/**
 * Where the pill is being drawn. It changes ONE word and it is not decoration.
 *
 * The Payouts card is an ORDER BOOK: each row is an order, and what happened to this one is that
 * it went back to the balance it came out of. The timeline below it is a STORY of money moving,
 * told in the names of the tiles at the top of the same page — so there the same fact reads
 * "Returned to Available", which is the tile the user can go and look at.
 *
 * Both strings live here, in the file that owns the vocabulary; the components only draw them.
 */
export type PayoutSurface = 'orders' | 'history';
const HISTORY_LABEL: Partial<Record<PayoutKey, string>> = {
  refunded: 'Returned to Available',
};

/** The two dark any-asset statuses keep the words they already had on screen. */
const WAITING: Record<string, string> = {
  waiting_for_dep_eth: 'Waiting for ETH',
  waiting_for_swap_to_target_asset: 'Swapping',
};

/** The ledger's own evidence that an order's money went back: a `cancel` entry for its id. */
export function refundedIds(history: HistoryEntry[] | undefined | null): Set<string> {
  const out = new Set<string>();
  for (const h of history ?? []) if (h && h.kind === 'cancel' && typeof h.ref === 'string' && h.ref) out.add(h.ref);
  return out;
}

/**
 * What a returned order says under its pill — with the amount THE USER ASKED FOR in it.
 *
 * ⛔ `requestedGroth`, never `delivered_groth` and never a number made here: a returned order
 * delivered nothing, so "what came back" is what was taken, which is what was asked for plus the
 * fees. The fees are NAMED rather than added up: the row carries `fee_groth` and
 * `bridge_fee_groth`, the ledger's `cancel` entry carries what actually went back, and a total
 * summed on this line would be a third answer to one question (law 9). A row with no amount on it
 * at all says "the amount", which is true, instead of "NaN ETH", which is what arithmetic on a
 * missing field looks like on screen.
 */
function returnedReason(r: PayoutRow): string {
  const asked = requestedGroth(r);
  const what = asked === null ? 'the amount' : `${fmtGroth(asked)} ${r.asset}`;
  return `${what} plus its fees are back in Available — nothing was sent; schedule it again when you like`;
}

/**
 * One row → what the screen says about it.
 *
 * `refunded` is passed in rather than guessed: it is a fact about the LEDGER (a `cancel` entry for
 * this id), and the row itself cannot carry it on a legacy build.
 *
 * `surface` picks the one word that differs between the order book and the timeline (T48).
 */
export function payoutView(r: PayoutRow, refunded = false, surface: PayoutSurface = 'orders'): PayoutView {
  const status = String(r.status ?? '');
  const nextTry = typeof r.next_attempt_at === 'number' && Number.isFinite(r.next_attempt_at) ? r.next_attempt_at : null;
  const hold = holdReasonText(r.hold_reason);
  const confirmations = status === 'bridging' && typeof r.beam_confirmations === 'number' ? r.beam_confirmations : null;

  let key: PayoutKey;
  let reason: string | null = null;
  if (status === 'sent') key = 'sent';
  else if (status === 'cancelled') key = 'cancelled';
  else if (status === 'scheduled') key = 'scheduled';
  else if (status === 'releasing' || status === 'bridging' || status === 'delivering' || status === 'paying') {
    key = status;
    reason = hold;
  } else if (status === 'delayed' || status === 'held') {
    key = 'delayed';
    // ⛔ the two are the same PILL and not the same sentence: a `delayed` row is being retried on
    // a backoff ladder, a `held` row is one a machine has stopped retrying and a person owns.
    // Saying "we are retrying this" about a held order would be a claim the system is not making.
    reason =
      hold ?? (status === 'held' ? 'someone at Pgas.me is looking at this one — your money stays reserved' : 'we are retrying this one');
  } else if (status === 'failed' || status === 'expired') {
    // ⛔ the legacy terminal status, and the whole reason this function exists
    if (refunded) key = 'refunded';
    else {
      key = 'delayed';
      // "being retried" is the API's claim (it published a next attempt), never ours
      reason = hold ?? (nextTry !== null ? 'being retried' : null);
    }
  } else if (WAITING[status]) key = 'waiting';
  else key = 'other';

  const base = PILL[key];
  const label =
    key === 'waiting' ? WAITING[status] : key === 'other' ? status : (surface === 'history' && HISTORY_LABEL[key]) || base.label;
  return {
    status,
    key,
    label,
    cls: base.cls,
    reason:
      key === 'refunded'
        ? returnedReason(r)
        : // their own action, and the only thing left to say about it is where the money is. It
          // is NOT the returned sentence: "nothing was sent" is news about an order that tried,
          // and a cancel is an order that never started. (T48 item 2.)
          key === 'cancelled'
          ? 'you cancelled this one — the amount and its fees are back in Available'
          : reason,
    nextTry: key === 'delayed' ? nextTry : null,
    // ⛔ THE API'S ANSWER WHEN IT PUBLISHES ONE. `payouts.cancellable` is the same function the
    // cancel route enforces, so the button and the refusal are one decision; the status rule is
    // the fallback for a build that does not send the field, and a refused cancel still shows the
    // API's own sentence rather than anything composed here.
    cancellable: typeof r.cancellable === 'boolean' ? r.cancellable : status === 'scheduled' || status === 'delayed' || status === 'held',
    confirmations,
  };
}

/**
 * What the wallet receives, when the API says so: `delivered_groth`, never a number made here.
 * Takes the field rather than the row, so a scheduled ORDER and a priced PREVIEW row are read by
 * the same function — they carry the same three fields and they must not be read two ways.
 */
export function deliveredGroth(r: { delivered_groth?: number } | null | undefined): number | null {
  const v = r?.delivered_groth;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** What the order took off Available, when the API says so. */
export function debitedGroth(r: { debited_groth?: number } | null | undefined): number | null {
  const v = r?.debited_groth;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/**
 * WHAT THE USER ASKED FOR: `requested_groth`, falling back to `amount_groth`.
 *
 * ⛔ The fallback is for a row written before 2026-09-10 — and only for those. On such a row the
 * two rules were one (the fees always rode on top), so `amount_groth` IS what was asked for and
 * IS what was delivered. On a row written since, `amount_groth` is the DELIVERY, and reading it
 * as "asked" is how "of 4,881,372 ETH asked" appeared under a delivery of 4,881,372.
 */
export function requestedGroth(r: { requested_groth?: number; amount_groth?: number } | null | undefined): number | null {
  for (const v of [r?.requested_groth, r?.amount_groth]) if (typeof v === 'number' && Number.isFinite(v)) return v;
  return null;
}

/**
 * A delayed order's next attempt, as the API published it (`next_try_at`, ISO-8601) — or null.
 *
 * Nothing is derived here: `next_attempt_at` is the machine's unix field and this page has no
 * business turning one into the other (law 9). Null means "the API did not say", which is what a
 * held row and every non-delayed row are.
 */
export function nextTryAt(r: { next_try_at?: string } | null | undefined): Date | null {
  return typeof r?.next_try_at === 'string' && r.next_try_at.trim() ? toDate(r.next_try_at) : null;
}

/** `on_top` / `from_amount`, or null on a build that does not publish it. */
export function feeMode(r: { fee_mode?: string } | null | undefined): FeeMode | null {
  const m = r?.fee_mode;
  return m === 'on_top' || m === 'from_amount' ? m : null;
}

/**
 * THE sentence a from-amount row carries, on the Schedule form and on the Balance page alike.
 *
 * Admin 2026-09-10: "You need to take fees above the amount user requested … only if user doesn't
 * have deposit to pay gas fees and 2% fees to us, we take it from sending amount, so he gets less
 * than 0.01 ETH." A row that quietly delivers less than the number the user typed is the one thing
 * this note exists to prevent.
 */
export const FROM_AMOUNT_NOTE = 'fees taken from the amount — not enough balance to pay them on top';

/**
 * A time in a table cell: "10 Sept, 14:24", with the year only when it is not this one.
 *
 * `fmtTime` writes the year always, which is right in a detail panel and costs a payout table two
 * columns' worth of width — the width that pushed the Cancel button off the right edge of the card
 * (screen review 2026-09-10). Dropping a year that cannot be misread is the cheapest space there
 * is; a year that CAN be misread is still printed.
 */
export function shortTime(v: number | string | undefined | null): string {
  const d = toDate(v);
  if (!d) return '–';
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString('en-GB', {
    day: '2-digit',
    month: 'short',
    ...(sameYear ? {} : { year: 'numeric' }),
    hour: '2-digit',
    minute: '2-digit',
  });
}

export interface Eta {
  at: number;
  note: string | null;
  tailS: number | null;
}

/** `eta_at` / `eta_note` as published, or null. Nothing here derives a time from another time. */
export function etaOf(r: PayoutRow): Eta | null {
  const at = r.eta_at;
  if (typeof at !== 'number' || !Number.isFinite(at) || at <= 0) return null;
  return {
    at,
    note: typeof r.eta_note === 'string' && r.eta_note.trim() ? r.eta_note.trim() : null,
    tailS: typeof r.eta_tail_s === 'number' && Number.isFinite(r.eta_tail_s) ? r.eta_tail_s : null,
  };
}

// ── T48 block: "Schedule again" — one order handed from the Balance page to the form ──────────
//
// Admin 2026-09-10 15:24Z: "Better to retry this deposit instead of just refunded back." A
// returned order is one the user still wants; retyping the wallet is the part that goes wrong.
//
// ⛔ IT DOES NOT TRAVEL IN THE URL. `/schedule?to=0x…&amount=…` is a link: it is copied into
// chats, kept in history, and written into every log between here and the server — and the whole
// product is about a wallet nobody can tie to the person funding it. `sessionStorage` is this
// tab's own memory, it never leaves the browser, and it dies with the tab.
//
// ⛔ AND IT IS READ, THEN CLEARED — never read-and-clear in one call. The form consumes it while
// computing its initial rows, which React runs twice in development (StrictMode), and a consuming
// read would hand the second run an empty form. Clearing is a separate step, after the mount, so
// a later visit to the form opens blank.
const PREFILL_KEY = 'pgas.schedule.prefill.v1';

/** One order, as the form needs it: where it was going and what was ASKED for (never delivered). */
export interface SchedulePrefill {
  W: string;
  groth: number;
}

export function setSchedulePrefill(p: SchedulePrefill): void {
  try {
    sessionStorage.setItem(PREFILL_KEY, JSON.stringify(p));
  } catch {
    // no session storage (a locked-down browser, a private window that refuses it): the button
    // still navigates, and the form opens empty rather than the press doing nothing at all
  }
}

/** What the Balance page left for the form, or null. It stays put until `clearSchedulePrefill`. */
export function readSchedulePrefill(): SchedulePrefill | null {
  try {
    const raw = sessionStorage.getItem(PREFILL_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<SchedulePrefill>;
    if (typeof v?.W !== 'string' || !v.W) return null;
    if (typeof v.groth !== 'number' || !Number.isFinite(v.groth) || v.groth <= 0) return null;
    return { W: v.W, groth: v.groth };
  } catch {
    return null;
  }
}

export function clearSchedulePrefill(): void {
  try {
    sessionStorage.removeItem(PREFILL_KEY);
  } catch {
    // nothing was stored either, then
  }
}
// ──────────────────────────────────────────────────────────────────────────────────────────────

export { BEAM_CONFIRMATIONS };
