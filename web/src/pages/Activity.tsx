import { useState } from 'react';
import { SignInGate } from '../components/SignInGate';
import { DepositStatusPill, RequestStatusPill } from '../components/Status';
import { api, errorText } from '../lib/api';
import { chainName } from '../lib/chains';
import { explorerTx, fmtDuration, fmtGroth, fmtTime, fmtUnits, shortAddr } from '../lib/format';
import { useStore } from '../state/store';

function TxLink({ chainId, hash }: { chainId?: number; hash?: string }) {
  if (!hash) return <span className="muted">–</span>;
  return (
    <a href={explorerTx(chainId, hash)} target="_blank" rel="noreferrer" className="mono" title={hash}>
      {hash.slice(0, 10)}…{hash.slice(-4)}
    </a>
  );
}

export function ActivityPage() {
  const { session, data } = useStore();
  const { chains, chainById } = data;
  const account = session.account;
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);

  const cancel = async (id: string) => {
    if (!window.confirm('Cancel this scheduled payout? The amount and fee return to Available.')) return;
    setCancelling(id);
    setCancelError(null);
    try {
      await api.cancelWithdrawal(id);
      void session.refreshAccount();
    } catch (e) {
      setCancelError(errorText(e));
    } finally {
      setCancelling(null);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Activity</h1>
          <p className="muted">Deposits into the balance and payout requests out of it. Times are local.</p>
        </div>
      </div>
      <SignInGate what="your activity">
        {!account ? (
          <p className="muted">Loading the account…</p>
        ) : (
          <>
            <section className="card">
              <div className="card-head">
                <h2>Deposits</h2>
                <span className="tiny muted">{account.deposits.length} shown</span>
              </div>
              {account.deposits.length === 0 ? (
                <div className="empty">No deposits yet.</div>
              ) : (
                <div className="table-wrap">
                  <table className="data" data-testid="deposits-table">
                    <thead>
                      <tr>
                        <th>Created</th>
                        <th>Status</th>
                        <th>Asset</th>
                        <th>Source</th>
                        <th>Source tx</th>
                        <th>Ethereum tx</th>
                        <th>Beam msg</th>
                        <th className="num">Credited</th>
                        <th>Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {account.deposits.map((d) => {
                        const src = chainById(d.src?.chain_id) ?? chains.find((c) => c.dln_chain_id === d.src?.chain_id);
                        const evm = src?.chain_id ?? d.src?.chain_id;
                        return (
                          <tr key={d._id}>
                            <td>{fmtTime(d.created_at)}</td>
                            <td>
                              <DepositStatusPill status={d.status} />
                              {d.note && (
                                <div className="tiny muted" style={{ whiteSpace: 'normal', maxWidth: 260 }}>
                                  {d.note}
                                </div>
                              )}
                            </td>
                            <td>{d.asset}</td>
                            <td>
                              <span className="num">{d.src?.amount ? fmtUnits(d.src.amount, 18) : '–'}</span>{' '}
                              <span className="muted tiny">
                                {d.src?.token ? shortAddr(d.src.token) : ''} on {src?.name ?? chainName(evm)}
                              </span>
                            </td>
                            <td>
                              <TxLink chainId={evm} hash={d.src_tx_hash} />
                            </td>
                            <td>
                              <TxLink chainId={1} hash={d.eth?.tx} />
                            </td>
                            <td className="mono">{d.eth?.msg_id ?? '–'}</td>
                            <td className="num">{d.value_groth !== undefined ? `${fmtGroth(d.value_groth)} ${d.asset}` : '–'}</td>
                            <td>{fmtTime(d.updated_at)}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="tiny muted" style={{ marginTop: 8 }}>
                Source amounts are shown with 18 decimals unless the token list says otherwise; the Ethereum tx is the DLN fill that locked
                the asset in the Beam bridge.
              </p>
            </section>

            <section className="card">
              <div className="card-head">
                <h2>Payout requests</h2>
                <span className="tiny muted">{account.requests.length} shown</span>
              </div>
              {cancelError && <div className="banner banner-error">{cancelError}</div>}
              {account.requests.length === 0 ? (
                <div className="empty">No payout requests yet.</div>
              ) : (
                <div className="table-wrap">
                  <table className="data" data-testid="requests-table">
                    <thead>
                      <tr>
                        <th>Created</th>
                        <th>Status</th>
                        <th>Wallet</th>
                        <th className="num">Amount</th>
                        <th className="num">Fee</th>
                        <th>Mode</th>
                        <th>Window</th>
                        <th>Release at</th>
                        <th>Beam tx</th>
                        <th>Beam msg</th>
                        <th>Ethereum tx</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {account.requests.map((r) => (
                        <tr key={r._id}>
                          <td>{fmtTime(r.created_at)}</td>
                          <td>
                            <RequestStatusPill status={r.status} />
                          </td>
                          <td className="mono" title={r.W}>
                            {shortAddr(r.W, 8, 6)}
                          </td>
                          <td className="num">
                            {fmtGroth(r.amount_groth)} {r.asset}
                          </td>
                          <td className="num">{fmtGroth(r.fee_groth)}</td>
                          <td>{r.mode}</td>
                          <td>{fmtDuration(r.window_s)}</td>
                          <td>{fmtTime(r.release_at)}</td>
                          <td className="mono" title={r.beam_txid}>
                            {r.beam_txid ? shortAddr(r.beam_txid, 8, 4) : '–'}
                          </td>
                          <td className="mono">{r.msg_id ?? '–'}</td>
                          <td>
                            <TxLink chainId={1} hash={r.eth_tx} />
                          </td>
                          <td>
                            {r.status === 'scheduled' && (
                              <button type="button" className="btn btn-sm" disabled={cancelling === r._id} onClick={() => cancel(r._id)}>
                                {cancelling === r._id ? 'Cancelling…' : 'Cancel'}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </>
        )}
      </SignInGate>
    </div>
  );
}
