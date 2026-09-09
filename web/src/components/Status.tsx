// Status indicators shared by the Deposit, Withdraw and Activity pages and the footer: deposit and
// payout status pills, and the honest anonymity grade computed from real counts.
import type { DepositStatus, RequestStatus, Stats } from '../lib/types';

const DEPOSIT: Record<DepositStatus, { label: string; cls: string }> = {
  submitted: { label: 'Submitted', cls: 'pill-indigo' },
  order_seen: { label: 'Order filled', cls: 'pill-indigo' },
  fallback_pending: { label: 'Bridging (manual)', cls: 'pill-amber' },
  locked: { label: 'Locked in Beam bridge', cls: 'pill-magenta' },
  confirming: { label: 'Confirming', cls: 'pill-magenta' },
  credited: { label: 'Credited', cls: 'pill-teal' },
  failed: { label: 'Failed', cls: 'pill-red' },
  expired: { label: 'Expired', cls: 'pill-red' },
};

const REQUEST: Record<RequestStatus, { label: string; cls: string }> = {
  scheduled: { label: 'Scheduled', cls: 'pill-indigo' },
  bridging: { label: 'Bridging', cls: 'pill-magenta' },
  sent: { label: 'Sent', cls: 'pill-teal' },
  failed: { label: 'Failed', cls: 'pill-red' },
  cancelled: { label: 'Cancelled', cls: '' },
};

export function depositStatusLabel(status: string): string {
  return DEPOSIT[status as DepositStatus]?.label ?? status;
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

export type Grade = 'weak' | 'ok' | 'good';

/** Fewer than 10 deposits in 24 h reads "weak" — never overstated (spec §9.6). */
export function anonymityGrade(stats: Stats | null): { grade: Grade; text: string } {
  if (!stats) return { grade: 'weak', text: 'Anonymity today: unknown — stats unavailable, assume weak' };
  const n = stats.deposits_24h ?? 0;
  const count = `${n} deposit${n === 1 ? '' : 's'} in the last 24 h`;
  if (n < 10) return { grade: 'weak', text: `Anonymity today: weak — ${count}` };
  if (n < 50) return { grade: 'ok', text: `Anonymity today: ok — ${count}` };
  return { grade: 'good', text: `Anonymity today: good — ${count}` };
}

export function GradeText({ grade, text }: { grade: Grade; text: string }) {
  return (
    <span className={`grade grade-${grade}`} data-grade={grade}>
      {text}
    </span>
  );
}
