// Status pills shared by the Deposit page and the Balance timeline, plus the muted hint lines that
// sit under them. Every status in API_CONTRACT.md has an entry here: an unmapped status still
// renders (with its raw name and no tone), but that is the fallback, not the design.
//
// The words are the user's, not the ledger's: `locked` is the asset sitting in the Beam bridge, and
// what a depositor is waiting through is "Bridging"; `fallback_pending` is the same wait with a
// worker doing the crossing by hand, which changes nothing they can act on.
import type { Deposit, DepositStatus, PayoutRequest, RequestStatus } from '../lib/types';

const DEPOSIT: Record<DepositStatus, { label: string; cls: string }> = {
  submitted: { label: 'Submitted', cls: 'pill-indigo' },
  order_seen: { label: 'Order filled', cls: 'pill-indigo' },
  fallback_pending: { label: 'Bridging', cls: 'pill-magenta' },
  locked: { label: 'Bridging', cls: 'pill-magenta' },
  confirming: { label: 'Confirming', cls: 'pill-magenta' },
  credited: { label: 'Credited', cls: 'pill-teal' },
  failed: { label: 'Failed', cls: 'pill-red' },
  expired: { label: 'Failed', cls: 'pill-red' },
};

// scheduled → releasing → bridging → delivering → sent (indigo = queued/starting,
// magenta = in flight, teal = done), and the two dark any-asset statuses as amber holds.
const REQUEST: Record<RequestStatus, { label: string; cls: string }> = {
  scheduled: { label: 'Scheduled', cls: 'pill-indigo' },
  releasing: { label: 'Releasing', cls: 'pill-indigo' },
  bridging: { label: 'Bridging', cls: 'pill-magenta' },
  delivering: { label: 'Delivering', cls: 'pill-magenta' },
  sent: { label: 'Sent', cls: 'pill-teal' },
  failed: { label: 'Failed', cls: 'pill-red' },
  cancelled: { label: 'Cancelled', cls: '' },
  waiting_for_dep_eth: { label: 'Waiting for ETH', cls: 'pill-amber' },
  waiting_for_swap_to_target_asset: { label: 'Swapping', cls: 'pill-amber' },
};

/** Beam confirmations the bridge waits for before the relayer can deliver (≈ 1 h at 59.3 s/block). */
export const BEAM_CONFIRMATIONS = 61;

export function depositStatusLabel(status: string): string {
  return DEPOSIT[status as DepositStatus]?.label ?? status;
}

export function requestStatusLabel(status: string): string {
  return REQUEST[status as RequestStatus]?.label ?? status;
}

export function DepositStatusPill({ status }: { status: string }) {
  const m = DEPOSIT[status as DepositStatus] ?? { label: status, cls: '' };
  return (
    <span className={`pill ${m.cls}`} data-status={status}>
      {m.label}
    </span>
  );
}

export function RequestStatusPill({ status }: { status: string }) {
  const m = REQUEST[status as RequestStatus] ?? { label: status, cls: '' };
  return (
    <span className={`pill ${m.cls}`} data-status={status}>
      {m.label}
    </span>
  );
}

/**
 * The one line a user may see under a deposit pill.
 *
 * `treasury` is operator information — the money is already the user's the moment the row says
 * `credited`, and claiming/shielding is our own sweep into the shielded pool. So it is a hint, never
 * a pill, it never names the sub-status, and once it is `shielded` (or `claimed`) it says nothing at
 * all. `verified:false` on a `submitted` row means the registered hash has not yet been tied to the
 * quote, which is worth saying because it is the step the user is waiting on.
 */
export function depositHint(d: Pick<Deposit, 'status' | 'treasury' | 'verified'>): string | null {
  if (d.status === 'submitted' && d.verified === false) return 'verifying transaction';
  if (d.treasury === 'claiming' || d.treasury === 'shielding') return 'settling on Beam';
  return null;
}

export function DepositStatusCell({ deposit }: { deposit: Deposit }) {
  const hint = depositHint(deposit);
  return (
    <>
      <DepositStatusPill status={deposit.status} />
      {deposit.note && <div className="tiny muted status-sub">{deposit.note}</div>}
      {hint && (
        <div className="tiny muted status-sub" data-hint="deposit">
          {hint}
        </div>
      )}
    </>
  );
}

/**
 * Why an order is parked, in the user's words.
 *
 * `hold_reason` is written for the operator and often IS a flag (`PGAS_PAYOUT_DIRECT_ENABLED=0`).
 * A flag name on a user's screen is noise at best and alarming at worst, so a reason that looks
 * like one is translated to the thing the user is actually waiting on; anything already written as
 * a sentence is shown as it stands. Returns null when there is nothing worth saying.
 */
export function holdReasonText(raw: string | undefined | null): string | null {
  const s = (raw ?? '').trim();
  if (!s) return null;
  const looksLikeFlag = /^[A-Z0-9_]+\s*=/.test(s) || /^[A-Z][A-Z0-9_]{5,}$/.test(s);
  if (!looksLikeFlag) return s;
  if (/PAYOUT|DIRECT|PAUSE|STOP|KILL/i.test(s)) return 'payouts are paused right now';
  if (/BRIDGE|RELAYER|CONFIRM|ETA/i.test(s)) return 'waiting for the bridge';
  return 'waiting on Pgas.me';
}

export function RequestStatusCell({ request }: { request: PayoutRequest }) {
  const confs = request.status === 'bridging' && typeof request.beam_confirmations === 'number' ? request.beam_confirmations : null;
  const hold = holdReasonText(request.hold_reason);
  return (
    <>
      <RequestStatusPill status={request.status} />
      {confs !== null && (
        <div className="tiny muted status-sub" data-hint="confirmations">
          {confs}/{BEAM_CONFIRMATIONS} Beam confirmations
        </div>
      )}
      {hold && (
        <div className="tiny muted status-sub" data-hint="hold">
          {hold}
        </div>
      )}
    </>
  );
}
