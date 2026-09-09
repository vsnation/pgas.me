// Balance: four tiles per asset, then one timeline of everything that moved.
//
// Screen review 2026-09-09. The tiles are named for what the money is doing — Arriving, Available,
// Scheduled, Paid out — instead of which ledger bucket it sits in, and each one carries what it is
// worth in dollars, because that is the number a person checks. The formula subtitles ("credits −
// scheduled − sent") went: they explained our bookkeeping, not their money.
//
// The Activity page went too. Deposits and payouts are one story told in one table, newest first,
// with When · What · Amount · Status · tx on the row and everything else — the source chain, the
// second transaction, the delivery times, the fee — inside the row, on click. Internal identifiers
// (Beam message ids, order ids, ledger refs, the payout "mode") are gone entirely: nobody outside
// this codebase can do anything with them.
import { Fragment, useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import { HowItWorks } from '../components/HowItWorks';
import { SignInGate } from '../components/SignInGate';
import { DepositStatusCell, RequestStatusCell } from '../components/Status';
import { chainName } from '../lib/chains';
import { explorerTx, fmtAgo, fmtGroth, fmtTime, fmtUsd, shortAddr } from '../lib/format';
import { assetPricesUsd } from '../lib/portfolio';
import { routeChainId, type Asset, type AssetKey, type Balance, type Deposit, type PayoutRequest } from '../lib/types';
import { useStore } from '../state/store';

const ORDER: AssetKey[] = ['ETH', 'DAI', 'WBTC'];
const GROTH = 1e8;

function nonZero(b: Balance | undefined): boolean {
  return !!b && (b.available || b.scheduled || b.sent || b.pending) !== 0;
}

/** The dollar line under a tile — absent, never wrong, when nothing could price the asset. */
function UsdLine({ groth, price }: { groth: number; price: number | undefined }) {
  if (typeof price !== 'number' || !groth) return null;
  return <div className="tile-usd">{fmtUsd((groth / GROTH) * price)}</div>;
}

function Tiles({ asset, balance, price, feeBps }: { asset: AssetKey; balance: Balance; price: number | undefined; feeBps: number }) {
  return (
    <section className="stack-sm" data-testid={`balance-${asset}`}>
      <h2>{asset}</h2>
      <div className="grid-4">
        <div className="tile tile-accent-magenta">
          <div className="tile-label">Arriving</div>
          <div className="tile-value" data-testid={`arriving-${asset}`}>
            {fmtGroth(balance.pending)}
            <span className="unit">{asset}</span>
          </div>
          <UsdLine groth={balance.pending} price={price} />
          <div className="tile-sub">deposits still bridging</div>
        </div>
        <div className="tile tile-accent-teal">
          <div className="tile-label">Available</div>
          <div className="tile-value" data-testid={`available-${asset}`}>
            {fmtGroth(balance.available)}
            <span className="unit">{asset}</span>
          </div>
          <UsdLine groth={balance.available} price={price} />
        </div>
        <div className="tile tile-accent-indigo">
          <div className="tile-label">Scheduled</div>
          <div className="tile-value" data-testid={`scheduled-${asset}`}>
            {fmtGroth(balance.scheduled)}
            <span className="unit">{asset}</span>
          </div>
          <UsdLine groth={balance.scheduled} price={price} />
          <div className="tile-sub">incl. {feeBps / 100} % fee</div>
        </div>
        <div className="tile tile-accent-amber">
          <div className="tile-label">Paid out</div>
          <div className="tile-value" data-testid={`paid-out-${asset}`}>
            {fmtGroth(balance.sent)}
            <span className="unit">{asset}</span>
          </div>
          <UsdLine groth={balance.sent} price={price} />
        </div>
      </div>
    </section>
  );
}

// ---------- the one timeline ----------
type Row =
  | { id: string; at: number | string; kind: 'deposit'; deposit: Deposit }
  | { id: string; at: number | string; kind: 'payout'; request: PayoutRequest };

function seconds(v: number | string | undefined): number {
  const n = typeof v === 'string' ? Date.parse(v) / 1000 : v;
  return Number.isFinite(n) ? (n as number) : 0;
}

function TxLink({ chainId, hash, children }: { chainId?: number; hash?: string; children: string }) {
  if (!hash) return <span className="muted">–</span>;
  return (
    <a
      href={explorerTx(chainId, hash)}
      target="_blank"
      rel="noreferrer"
      className="mono"
      title={hash}
      onClick={(e) => e.stopPropagation()} // the row toggles; the link does not
    >
      {children}
    </a>
  );
}

function Timeline({ deposits, requests }: { deposits: Deposit[]; requests: PayoutRequest[] }) {
  const { data } = useStore();
  const { chains, chainById } = data;
  const [open, setOpen] = useState<string | null>(null);

  const rows: Row[] = useMemo(() => {
    const all: Row[] = [
      ...deposits.map((d): Row => ({ id: `d-${d._id}`, at: d.created_at, kind: 'deposit', deposit: d })),
      ...requests.map((r): Row => ({ id: `p-${r._id}`, at: r.created_at, kind: 'payout', request: r })),
    ];
    return all.sort((a, b) => seconds(b.at) - seconds(a.at));
  }, [deposits, requests]);

  const srcChain = (d: Deposit) => {
    const c = chainById(d.src?.chain_id) ?? chains.find((x) => routeChainId(x) === d.src?.chain_id);
    return { name: c?.name ?? chainName(d.src?.chain_id), evm: c?.chain_id ?? d.src?.chain_id };
  };

  return (
    <section className="card" data-testid="timeline-card">
      <div className="card-head">
        <h2>Deposits and payouts</h2>
        <span className="tiny muted">newest first · tap a row for the detail</span>
      </div>
      {rows.length === 0 ? (
        <div className="empty">Nothing yet. A deposit shows up here the moment it is sent.</div>
      ) : (
        <div className="table-wrap">
          <table className="data timeline-table" data-testid="timeline">
            <thead>
              <tr>
                <th>When</th>
                <th>What</th>
                <th className="num">Amount</th>
                <th>Status</th>
                <th>Transaction</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const isOpen = open === row.id;
                const toggle = () => setOpen(isOpen ? null : row.id);
                const common = {
                  className: `tl-row${isOpen ? ' open' : ''}`,
                  onClick: toggle,
                  onKeyDown: (e: KeyboardEvent<HTMLTableRowElement>) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      toggle();
                    }
                  },
                  tabIndex: 0,
                  role: 'button' as const,
                  'aria-expanded': isOpen,
                  'data-testid': 'timeline-row',
                };
                if (row.kind === 'deposit') {
                  const d = row.deposit;
                  const src = srcChain(d);
                  return (
                    <Fragment key={row.id}>
                      <tr key={row.id} {...common}>
                        <td className="nowrap">{fmtTime(d.created_at)}</td>
                        <td>
                          Deposit <span className="muted">from {src.name}</span>
                        </td>
                        <td className="num">
                          {d.value_groth !== undefined ? (
                            <span className="delta-in">
                              +{fmtGroth(d.value_groth)} {d.asset}
                            </span>
                          ) : (
                            <span className="muted">–</span>
                          )}
                        </td>
                        <td>
                          <DepositStatusCell deposit={d} />
                        </td>
                        <td>
                          <TxLink chainId={src.evm} hash={d.src_tx_hash}>
                            your payment
                          </TxLink>
                        </td>
                      </tr>
                      {isOpen && (
                        <tr key={`${row.id}-detail`} className="tl-detail" data-testid="timeline-detail">
                          <td colSpan={5}>
                            <dl className="kv">
                              <dt>Paid from</dt>
                              <dd>{src.name}</dd>
                              <dt>Your payment</dt>
                              <dd>
                                <TxLink chainId={src.evm} hash={d.src_tx_hash}>
                                  {d.src_tx_hash ? `${d.src_tx_hash.slice(0, 10)}…${d.src_tx_hash.slice(-6)}` : ''}
                                </TxLink>
                              </dd>
                              <dt>Into the bridge</dt>
                              <dd>
                                <TxLink chainId={1} hash={d.eth?.tx}>
                                  {d.eth?.tx ? `${d.eth.tx.slice(0, 10)}…${d.eth.tx.slice(-6)}` : ''}
                                </TxLink>
                              </dd>
                              <dt>Credited</dt>
                              <dd className="num">{d.value_groth !== undefined ? `${fmtGroth(d.value_groth)} ${d.asset}` : 'not yet'}</dd>
                              {d.note ? (
                                <>
                                  <dt>Note</dt>
                                  <dd>{d.note}</dd>
                                </>
                              ) : null}
                            </dl>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                }
                const r = row.request;
                return (
                  <Fragment key={row.id}>
                    <tr key={row.id} {...common}>
                      <td className="nowrap">{fmtTime(r.created_at)}</td>
                      <td>
                        Payout <span className="mono muted">{shortAddr(r.W, 8, 6)}</span>
                      </td>
                      <td className="num">
                        −{fmtGroth(r.amount_groth)} {r.asset}
                      </td>
                      <td>
                        <RequestStatusCell request={r} />
                      </td>
                      <td>
                        <TxLink chainId={1} hash={r.eth_tx}>
                          delivery
                        </TxLink>
                      </td>
                    </tr>
                    {isOpen && (
                      <tr key={`${row.id}-detail`} className="tl-detail" data-testid="timeline-detail">
                        <td colSpan={5}>
                          <dl className="kv">
                            <dt>To wallet</dt>
                            <dd className="mono wrap">{r.W}</dd>
                            <dt>Should arrive</dt>
                            <dd>{fmtTime(r.deliver_at)}</dd>
                            <dt>Goes to the bridge</dt>
                            <dd>{fmtTime(r.release_at)}</dd>
                            <dt>Fee</dt>
                            <dd className="num">
                              {fmtGroth(r.fee_groth)} {r.asset}
                            </dd>
                            <dt>Delivery</dt>
                            <dd>
                              <TxLink chainId={1} hash={r.eth_tx}>
                                {r.eth_tx ? `${r.eth_tx.slice(0, 10)}…${r.eth_tx.slice(-6)}` : ''}
                              </TxLink>
                            </dd>
                          </dl>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function BalancePage() {
  const { session, data } = useStore();
  const { account, accountError, lastAccountAt, refreshAccount, accountLoading } = session;
  const [prices, setPrices] = useState<Record<string, number>>({});
  const assets: Asset[] = data.assets;

  // one price read per asset list, off the same cache as the deposit chips; failure is silent and
  // simply leaves the dollar lines out
  useEffect(() => {
    if (!assets.length) return;
    let alive = true;
    void assetPricesUsd(assets.map((a) => ({ key: a.key, token: a.token }))).then((p) => alive && setPrices(p));
    return () => {
      alive = false;
    };
  }, [assets]);

  const shown = account ? ORDER.filter((k) => k === 'ETH' || nonZero(account.balances[k])) : [];
  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Balance</h1>
          <p className="muted">
            Your balance on Beam. Deposits arrive here; scheduled payouts leave from here. <HowItWorks />
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
      <SignInGate what="your balance">
        {accountError && <div className="banner banner-error">{accountError}</div>}
        {!account && !accountError && <p className="muted">Loading the account…</p>}
        {account &&
          shown.map((k) => (
            <Tiles
              key={k}
              asset={k}
              balance={account.balances[k] ?? { available: 0, scheduled: 0, sent: 0, pending: 0 }}
              price={prices[k]}
              feeBps={account.fee_bps ?? 200}
            />
          ))}
        {account && <Timeline deposits={account.deposits} requests={account.requests} />}
      </SignInGate>
    </div>
  );
}
