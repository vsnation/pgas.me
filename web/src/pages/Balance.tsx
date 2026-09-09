import { SignInGate } from '../components/SignInGate';
import { fmtAgo, fmtGroth, fmtTime } from '../lib/format';
import type { AssetKey, Balance, HistoryEntry } from '../lib/types';
import { useStore } from '../state/store';

const ORDER: AssetKey[] = ['ETH', 'DAI', 'WBTC'];

function nonZero(b: Balance | undefined): boolean {
  return !!b && (b.available || b.scheduled || b.sent || b.pending) !== 0;
}

function entryDelta(e: HistoryEntry): { text: string; cls: string } {
  const avail = e.d_avail ?? 0;
  const sched = e.d_sched ?? 0;
  const sent = e.d_sent ?? 0;
  if (avail > 0) return { text: `+${fmtGroth(avail)} available`, cls: 'delta-in' };
  if (sched > 0) return { text: `${fmtGroth(sched)} → scheduled`, cls: 'delta-hold' };
  if (sent > 0) return { text: `${fmtGroth(sent)} sent`, cls: 'delta-in' };
  if (avail < 0) return { text: `−${fmtGroth(-avail)} available`, cls: '' };
  if (sched < 0) return { text: `−${fmtGroth(-sched)} scheduled`, cls: '' };
  return { text: fmtGroth(e.groth), cls: '' };
}

const KIND_LABEL: Record<string, string> = {
  credit: 'Deposit credited',
  schedule: 'Withdrawal scheduled',
  release: 'Payout released',
  fee: 'Fee (2% at unlock)',
  cancel: 'Withdrawal cancelled',
  refund: 'Refund',
  adjust: 'Adjustment',
};

export function BalancePage() {
  const { account, accountError, lastAccountAt, refreshAccount, accountLoading } = useStore().session;
  const assets = account ? ORDER.filter((k) => k === 'ETH' || nonZero(account.balances[k])) : [];
  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Balance</h1>
          <p className="muted">
            Held for the signed-in wallet on Beam. Pending bridge is a deposit not yet credited; Scheduled is reserved for withdrawals
            (amount + 2%); Sent has left.
          </p>
        </div>
        {account && (
          <div className="row small muted">
            <span>updated {lastAccountAt ? fmtAgo(lastAccountAt) : '–'}</span>
            <button type="button" className="btn btn-sm" onClick={() => refreshAccount()} disabled={accountLoading}>
              Refresh
            </button>
          </div>
        )}
      </div>
      <SignInGate what="the balance">
        {accountError && <div className="banner banner-error">{accountError}</div>}
        {!account && !accountError && <p className="muted">Loading the account…</p>}
        {account &&
          assets.map((k) => {
            const b = account.balances[k] ?? { available: 0, scheduled: 0, sent: 0, pending: 0 };
            return (
              <section key={k} className="stack-sm" data-testid={`balance-${k}`}>
                <h2>{k}</h2>
                <div className="grid-4">
                  <div className="tile tile-accent-magenta">
                    <div className="tile-label">Pending bridge</div>
                    <div className="tile-value">
                      {fmtGroth(b.pending)}
                      <span className="unit">{k}</span>
                    </div>
                    <div className="tile-sub">order filled, claim not yet credited</div>
                  </div>
                  <div className="tile tile-accent-teal">
                    <div className="tile-label">Available</div>
                    <div className="tile-value" data-testid={`available-${k}`}>
                      {fmtGroth(b.available)}
                      <span className="unit">{k}</span>
                    </div>
                    <div className="tile-sub">credits − scheduled − sent</div>
                  </div>
                  <div className="tile tile-accent-indigo">
                    <div className="tile-label">Scheduled</div>
                    <div className="tile-value">
                      {fmtGroth(b.scheduled)}
                      <span className="unit">{k}</span>
                    </div>
                    <div className="tile-sub">payout requests not yet released</div>
                  </div>
                  <div className="tile tile-accent-amber">
                    <div className="tile-label">Sent</div>
                    <div className="tile-value">
                      {fmtGroth(b.sent)}
                      <span className="unit">{k}</span>
                    </div>
                    <div className="tile-sub">released payouts</div>
                  </div>
                </div>
              </section>
            );
          })}
        {account && (
          <section className="card">
            <div className="card-head">
              <h2>History</h2>
              <span className="tiny muted">{account.history.length} entries · ledger is append-only</span>
            </div>
            {account.history.length === 0 ? (
              <div className="empty">No ledger entries yet. A credited deposit is the first one.</div>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>When</th>
                      <th>Entry</th>
                      <th className="num">Amount</th>
                      <th>Effect</th>
                      <th>Reference</th>
                    </tr>
                  </thead>
                  <tbody>
                    {account.history.map((e, i) => {
                      const d = entryDelta(e);
                      return (
                        <tr key={`${e.ref ?? ''}-${e.at}-${i}`}>
                          <td className="nowrap">{fmtTime(e.at)}</td>
                          <td>
                            {KIND_LABEL[e.kind] ?? e.kind}
                            {e.note ? <span className="muted"> — {e.note}</span> : null}
                          </td>
                          <td className="num">
                            {fmtGroth(e.groth)} {e.asset?.replace(/^b/, '') ?? 'ETH'}
                          </td>
                          <td className={d.cls}>{d.text}</td>
                          <td className="mono tiny">{e.ref ?? '–'}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}
      </SignInGate>
    </div>
  );
}
