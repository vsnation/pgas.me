/**
 * The five list tabs (T49): Deposits, Payouts, Unattributed, Accounts, Events.
 *
 * Every column here was chosen, not generated. The old panel took the union of every JSON leaf and
 * printed it, which is how a deposits table ended up 3,400 px wide with twelve columns beginning
 * "AVAILABLE MP STR". A console is read at a glance or it is not read: what an operator needs on
 * the row is WHEN, WHICH, HOW MUCH, WHERE IT IS and WHAT IS HOLDING IT — everything else is one
 * click away in the drawer, verbatim, under "Raw".
 */
import { useEffect, useState } from 'react';
import { ADMIN_BASE, AdminError, adminGet, type Row } from '../lib/adminApi';
import { bridgeExplorerUrl } from '../components/BridgeLink';
import { amount, ago, middle, span, stamp, units } from './fmt';
import { depositRung, idOf, lockIsOpen, lockVerdict, num, payoutRung, pick, str, type Rung } from './model';
import { AddrLink, Drawer, Empty, Field, Id, KV, Ladder, Pill, Raw, Scroll, Timeline, TxLink, When, type Tone } from './ui';

/** One ladder cell, plus the stripe row that carries a held/delayed reason under it. */
function RungCell({ rung }: { rung: Rung }) {
  return <Ladder steps={rung.steps} at={rung.at} tone={rung.tone} name={rung.name} side={rung.side} />;
}

function Age({ at }: { at: unknown }) {
  const n = num(at);
  if (n === null) return <span className="ops-sub">–</span>;
  return (
    <span className="n" title={stamp(n)}>
      {ago(n)}
    </span>
  );
}

function chainOf(row: Row): number {
  return num(pick(row, 'src.chain_id', 'chain_id', 'tx.src.chain_id')) ?? 1;
}

// ───────────────────────────────────────────────────────────────────────────── deposits

export function DepositsTable({ rows, onOpen }: { rows: Row[]; onOpen: (r: Row) => void }) {
  if (rows.length === 0) return <Empty>No deposit matches these filters.</Empty>;
  return (
    <Scroll>
      <table className="ops-table">
        <thead>
          <tr>
            <th>When</th>
            <th>Deposit</th>
            <th className="r">Amount</th>
            <th>Route</th>
            <th>Status</th>
            <th>Bridge</th>
            <th>Links</th>
            <th className="r">In state</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const rung = depositRung(r);
            const asset = str(pick(r, 'asset'));
            const srcTx = str(pick(r, 'tx.src.hash', 'src_tx_hash'));
            const ethTx = str(pick(r, 'tx.eth.hash', 'eth.tx'));
            const height = num(pick(r, 'beam_height'));
            const bridge = bridgeExplorerUrl(height);
            const stateAt = pick(r, 'treasury_at', 'status_at', 'updated_at', 'created_at');
            return (
              <Rowish key={idOf(r) || i} row={r} onOpen={onOpen} rung={rung}>
                <td>
                  <When at={pick(r, 'created_at')} />
                </td>
                <td>
                  <Id value={idOf(r)} label="deposit" />
                  <span className="ops-cellsub">
                    acct <Id value={str(pick(r, 'account_id'))} head={6} tail={3} label="account" />
                  </span>
                </td>
                <td className="r">
                  <span className="n">{amount(num(pick(r, 'value_groth')), asset)}</span>
                </td>
                <td>
                  {str(pick(r, 'mode')) || '–'}
                  <span className="ops-cellsub">chain {chainOf(r)}</span>
                </td>
                <td>
                  <RungCell rung={rung} />
                </td>
                <td>
                  <span className="n">msg {str(pick(r, 'eth.msg_id')) || '–'}</span>
                  <span className="ops-cellsub">key {str(pick(r, 'receiver_key_index', 'key_index')) || '0'}</span>
                </td>
                <td>
                  {srcTx ? <TxLink hash={srcTx} chainId={chainOf(r)} /> : <span className="ops-sub">–</span>}
                  {ethTx && ethTx !== srcTx ? (
                    <span className="ops-cellsub">
                      <TxLink hash={ethTx} chainId={1} />
                    </span>
                  ) : null}
                  {bridge ? (
                    <span className="ops-cellsub">
                      <a href={bridge} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} title={`Beam block ${height}`}>
                        bridge ↗
                      </a>
                    </span>
                  ) : null}
                </td>
                <td className="r">
                  <Age at={stateAt} />
                </td>
              </Rowish>
            );
          })}
        </tbody>
      </table>
    </Scroll>
  );
}

// ────────────────────────────────────────────────────────────────────────────── payouts

export function PayoutsTable({ rows, onOpen }: { rows: Row[]; onOpen: (r: Row) => void }) {
  if (rows.length === 0) return <Empty>No payout matches these filters.</Empty>;
  return (
    <Scroll>
      <table className="ops-table">
        <thead>
          <tr>
            <th>When</th>
            <th>Order</th>
            <th>To</th>
            <th className="r">Amount</th>
            <th className="r">Fees</th>
            <th>Status</th>
            <th>Arrives</th>
            <th>Links</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const rung = payoutRung(r);
            const asset = str(pick(r, 'asset')) || 'ETH';
            const w = str(pick(r, 'W', 'destination', 'to'));
            const ethTx = str(pick(r, 'eth_tx'));
            const height = num(pick(r, 'beam_height'));
            const bridge = bridgeExplorerUrl(height);
            const funded = num(pick(r, 'bridge_fee_groth')) ?? 0;
            const refund = num(pick(r, 'bridge_fee_refund_groth')) ?? 0;
            const eta = pick(r, 'eta_at');
            return (
              <Rowish key={idOf(r) || i} row={r} onOpen={onOpen} rung={rung}>
                <td>
                  <When at={pick(r, 'created_at')} />
                </td>
                <td>
                  <Id value={idOf(r)} label="order" />
                  <span className="ops-cellsub">{str(pick(r, 'mode')) || 'direct'}</span>
                </td>
                <td>{w ? <AddrLink address={w} /> : <span className="ops-sub">–</span>}</td>
                <td className="r">
                  <span className="n">{amount(num(pick(r, 'delivered_groth', 'amount_groth')), asset)}</span>
                  <span className="ops-cellsub">asked {amount(num(pick(r, 'requested_groth', 'amount_groth')), asset)}</span>
                </td>
                <td
                  className="r"
                  title={`fee ${amount(num(pick(r, 'fee_groth')), asset)} · bridge funded ${amount(funded, asset)} · refunded ${amount(refund, asset)}`}
                >
                  <span className="n">{amount(num(pick(r, 'fee_groth')), asset)}</span>
                  <span className="ops-cellsub">bridge {amount(funded - refund, asset)}</span>
                </td>
                <td>
                  <RungCell rung={rung} />
                </td>
                <td>
                  {eta ? <When at={eta as number} /> : <span className="ops-sub">–</span>}
                  <span className="ops-cellsub">
                    due {pick(r, 'deliver_at') ? stamp(pick(r, 'deliver_at') as number).slice(0, 16) : '–'}
                  </span>
                </td>
                <td>
                  {ethTx ? <TxLink hash={ethTx} chainId={1} /> : <span className="ops-sub">–</span>}
                  {str(pick(r, 'beam_txid')) ? (
                    <span className="ops-cellsub n" title={`Beam kernel ${str(pick(r, 'beam_txid'))}`}>
                      {middle(str(pick(r, 'beam_txid')), 8, 4)}
                    </span>
                  ) : null}
                  {bridge ? (
                    <span className="ops-cellsub">
                      <a href={bridge} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} title={`Beam block ${height}`}>
                        bridge ↗
                      </a>
                    </span>
                  ) : null}
                </td>
              </Rowish>
            );
          })}
        </tbody>
      </table>
    </Scroll>
  );
}

/**
 * A clickable row plus, when the ladder has a side state that matters, a STRIPE under it carrying
 * the reason across the whole width. A reason truncated into a 90-px cell is a reason nobody reads.
 */
function Rowish({ row, rung, onOpen, children }: { row: Row; rung: Rung; onOpen: (r: Row) => void; children: React.ReactNode }) {
  const cols = Array.isArray(children) ? children.length : 1;
  const stripe = rung.stripe;
  const open = () => onOpen(row);
  return (
    <>
      <tr
        className="ops-row"
        data-testid="admin-row"
        data-row-id={idOf(row)}
        data-status={str(pick(row, 'status'))}
        tabIndex={0}
        onClick={open}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            open();
          }
        }}
      >
        {children}
      </tr>
      {stripe ? (
        <tr className="ops-stripe">
          <td colSpan={cols}>
            <span className={`ops-stripe-in${rung.tone === 'crit' ? ' is-crit' : ''}`}>
              <b>{rung.name}</b>
              <span>{stripe}</span>
            </span>
          </td>
        </tr>
      ) : null}
    </>
  );
}

// ───────────────────────────────────────────────────────────────────────── unattributed

export function LocksTable({ rows, onOpen }: { rows: Row[]; onOpen: (r: Row) => void }) {
  if (rows.length === 0) return <Empty tone="good">No lock is waiting for a human.</Empty>;
  // open first: these are the only rows on this page that are somebody's job
  const sorted = [...rows].sort((a, b) => Number(lockIsOpen(b)) - Number(lockIsOpen(a)));
  return (
    <Scroll>
      <table className="ops-table">
        <thead>
          <tr>
            <th>Message</th>
            <th className="r">Amount</th>
            <th>Since</th>
            <th className="r">Tries</th>
            <th>Next try</th>
            <th>Will it credit by itself?</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r, i) => {
            const v = lockVerdict(r);
            const next = pick(r, 'next_try_at', 'next_attempt_at', 'retry_at');
            const raw = pick(r, 'value_units', 'amount_units', 'amount');
            const asset = str(pick(r, 'asset'));
            return (
              <tr
                key={idOf(r) || i}
                className="ops-row"
                data-testid="admin-row"
                data-row-id={idOf(r)}
                data-status={str(pick(r, 'status'))}
                tabIndex={0}
                onClick={() => onOpen(r)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onOpen(r);
                  }
                }}
              >
                <td>
                  <b className="n">msg {str(pick(r, 'msg_id', 'msgId')) || '–'}</b>
                  <span className="ops-cellsub">{asset || '–'}</span>
                </td>
                <td className="r">
                  {/* the pipe carries the token's own units; the raw integer stays on the title */}
                  <span className="n" title={raw === undefined ? undefined : `${String(raw)} raw units`}>
                    {units(raw as string, asset)}
                  </span>
                </td>
                <td>
                  <When at={pick(r, 'at', 'created_at', 'seen_at')} />
                </td>
                <td className="r">
                  <span className="n">{num(pick(r, 'tries', 'attempts')) ?? 0}</span>
                </td>
                <td>{next ? <When at={next as number} /> : <span className="ops-sub">nothing scheduled</span>}</td>
                <td>
                  <Pill tone={v.tone as Tone}>{lockIsOpen(r) ? 'open' : str(pick(r, 'status'))}</Pill>{' '}
                  <span className="ops-cellsub">{v.text}</span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Scroll>
  );
}

// ────────────────────────────────────────────────────────────────────────────── accounts

export function AccountsTable({ rows, onOpen }: { rows: Row[]; onOpen: (r: Row) => void }) {
  if (rows.length === 0) return <Empty>No account matches these filters.</Empty>;
  return (
    <Scroll>
      <table className="ops-table">
        <thead>
          <tr>
            <th>Address</th>
            <th>Account</th>
            <th className="r">Available</th>
            <th className="r">Scheduled</th>
            <th className="r">Deposits</th>
            <th className="r">Payouts</th>
            <th>Last login</th>
            <th>First seen</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const bal = (r.balances ?? {}) as Record<string, Record<string, number>>;
            const eth = bal.ETH ?? {};
            return (
              <tr
                key={idOf(r) || i}
                className="ops-row"
                data-testid="admin-row"
                data-row-id={idOf(r)}
                tabIndex={0}
                onClick={() => onOpen(r)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onOpen(r);
                  }
                }}
              >
                <td>
                  {str(pick(r, 'address')) ? (
                    <AddrLink address={str(pick(r, 'address'))} />
                  ) : (
                    <span className="ops-sub">never signed in</span>
                  )}
                </td>
                <td>
                  <Id value={str(pick(r, 'account_id')) || idOf(r)} head={8} tail={4} label="account" />
                </td>
                <td className="r">
                  <span className="n">{amount(num(eth.available_groth ?? eth.available), 'ETH')}</span>
                </td>
                <td className="r">
                  <span className="n">{amount(num(eth.scheduled_groth ?? eth.scheduled), 'ETH')}</span>
                </td>
                <td className="r">
                  <span className="n">{num(pick(r, 'deposits')) ?? 0}</span>
                </td>
                <td className="r">
                  <span className="n">{num(pick(r, 'payouts')) ?? 0}</span>
                </td>
                <td>
                  <When at={pick(r, 'last_login_at')} />
                </td>
                <td>
                  <When at={pick(r, 'created_at')} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Scroll>
  );
}

// ──────────────────────────────────────────────────────────────────────────────── events

export function EventsTimeline({ rows, kinds, kind, onKind }: { rows: Row[]; kinds: string[]; kind: string; onKind: (k: string) => void }) {
  return (
    <>
      {kinds.length > 1 ? (
        <div className="ops-chips" style={{ marginBottom: 'var(--ops-s2)' }}>
          <button type="button" className={`ops-chip${kind === '' ? ' on' : ''}`} onClick={() => onKind('')}>
            every kind
          </button>
          {kinds.map((k) => (
            <button key={k} type="button" className={`ops-chip${kind === k ? ' on' : ''}`} data-kind={k} onClick={() => onKind(k)}>
              {k}
            </button>
          ))}
        </div>
      ) : null}
      <Timeline
        rows={rows.map((e) => ({
          at: pick(e, 'at', 'received_at', 'created_at'),
          kind: str(pick(e, 'kind', 'event')),
          text: str(pick(e, 'text', 'message', 'note')) || idOf(e),
          unsent: e.notified === false,
        }))}
      />
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────── drawer

export interface DetailProps {
  row: Row;
  /** `/deposits/{id}` etc., or null when this kind of row has no detail route */
  path: string | null;
  title: string;
  onClose: () => void;
}

/**
 * The row, opened. Sections in the order the question is asked: what it is, what happened to it,
 * what it came from — and Raw last, collapsed, because it is evidence rather than a view.
 *
 * ⛔ A 404 HERE IS NOT A REFUSAL AND MUST NOT RE-GATE THE PAGE. `/deposits/{id}` answers 404 for a
 * row that has been reaped as well as for a key that is not accepted; the TAB's own read is the
 * one allowed to decide the key is stale (see Admin.tsx REFUSAL_STATUSES).
 */
export function RowDrawer({ row, path, title, onClose }: DetailProps) {
  const [detail, setDetail] = useState<Row | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!path) return;
    let live = true;
    setDetail(null);
    setError(null);
    adminGet<Row>(path)
      .then((d) => live && setDetail(d))
      .catch((e) => live && setError(e instanceof AdminError ? `${e.status} at ${ADMIN_BASE}${e.path}` : 'unreadable'));
    return () => {
      live = false;
    };
  }, [path]);

  const events = Array.isArray(detail?.events) ? (detail!.events as Row[]) : [];
  const entries = Array.isArray(detail?.entries) ? (detail!.entries as Row[]) : [];
  const quote = detail && detail.quote ? (detail.quote as Row) : null;
  const beamTx = detail && detail.beampay_tx ? (detail.beampay_tx as Row) : null;

  return (
    <Drawer title={title} onClose={onClose}>
      <div>
        <h3 className="ops-eyebrow">The row</h3>
        <KV
          rows={Object.entries(row)
            .filter(([, v]) => v !== null && v !== undefined && v !== '' && typeof v !== 'object')
            .slice(0, 26)
            .map(([k, v]) => ({
              k: k.replace(/_/g, ' '),
              v: <Field name={k} value={v} asset={str(pick(row, 'asset'))} chainId={chainOf(row)} />,
            }))}
        />
      </div>

      {path ? (
        <div>
          <h3 className="ops-eyebrow">What happened</h3>
          {error ? (
            <p className="ops-sub" data-testid="admin-detail-error">
              No detail from the API ({error}) — the row above is what the list returned.
            </p>
          ) : detail === null ? (
            <p className="ops-sub">reading {path}…</p>
          ) : (
            <Timeline
              rows={events.map((e) => ({
                at: pick(e, 'at', 'created_at'),
                kind: str(pick(e, 'kind')),
                text: str(pick(e, 'text', 'message')),
                unsent: e.notified === false,
              }))}
            />
          )}
        </div>
      ) : null}

      {entries.length > 0 ? (
        <div>
          <h3 className="ops-eyebrow">Ledger</h3>
          {entries.map((e, i) => (
            <div className="ops-line" key={i}>
              <time title={stamp(pick(e, 'at') as number)}>{stamp(pick(e, 'at') as number).slice(11)}</time>
              <span>
                {str(pick(e, 'kind'))}{' '}
                <span className="n">
                  {amount(num(pick(e, 'groth', 'd_avail')), str(pick(e, 'asset')) || str(pick(row, 'asset')) || undefined)}
                </span>
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {quote ? (
        <div>
          <h3 className="ops-eyebrow">The quote it came from</h3>
          <KV
            rows={[
              { k: 'quote', v: <Id value={str(pick(quote, '_id', 'id'))} /> },
              { k: 'created', v: <When at={pick(quote, 'created_at', 'at')} absolute /> },
              { k: 'mode', v: <span>{str(pick(quote, 'mode')) || '–'}</span> },
              {
                k: 'address',
                v: str(pick(quote, 'address')) ? <AddrLink address={str(pick(quote, 'address'))} /> : <span className="ops-sub">–</span>,
              },
            ]}
          />
        </div>
      ) : null}

      {beamTx ? (
        <div>
          <h3 className="ops-eyebrow">BeamPay says</h3>
          <p className="ops-sub">
            ⚠️ <b>booked</b> is BeamPay&apos;s idempotency flag, not success — settlement is booked <i>and</i> status 3.
          </p>
          <Raw value={beamTx} label="the contract transaction" />
        </div>
      ) : null}

      <div>
        <h3 className="ops-eyebrow">Evidence</h3>
        <Raw value={detail ?? row} />
      </div>
    </Drawer>
  );
}

export { span };
