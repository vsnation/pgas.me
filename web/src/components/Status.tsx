// Status pills shared by the Deposit and Activity pages.
import type { DepositStatus, RequestStatus } from '../lib/types';

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
