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
import { BridgeLink } from '../components/BridgeLink';
import { HowItWorks } from '../components/HowItWorks';
import { PayoutEta, PayoutStatusCell } from '../components/PayoutStatus';
import { SignInGate } from '../components/SignInGate';
import { DepositStatusCell } from '../components/Status';
import { api, errorText } from '../lib/api';
import { chainName } from '../lib/chains';
import { explorerTx, fmtAgo, fmtGroth, fmtTime, fmtUsd, shortAddr } from '../lib/format';
import {
  deliveredGroth,
  feeMode,
  payoutView,
  refundedIds,
  requestedGroth,
  setSchedulePrefill,
  shortTime,
  type PayoutRow,
} from '../lib/payouts';
import { assetPricesUsd } from '../lib/portfolio';
import { routeChainId, type Asset, type AssetKey, type Balance, type Deposit, type HistoryEntry, type PayoutRequest } from '../lib/types';
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
          {/* since 2026-09-10 the debit is amount + our cut + the crossing at cost, so the bucket
              holds all three — saying only "incl. 2 % fee" would not add up to what is here */}
          <div className="tile-sub">incl. {feeBps / 100} % fee + bridge fee</div>
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

// ---------- every payout, with its status and when it arrives (T40) ----------
//
// Admin 2026-09-10, with a screenshot of this page showing two red **Failed** rows: "You understand
// that withdrawals on user's side cannot be failed … You should show all statuses there and
// estimated time of arrival of his asset."
//
// Both halves of that are here. The status vocabulary is lib/payouts.ts — an internal problem is
// `Delayed` (money still reserved, retried, reason and next attempt in plain words), a row a human
// must look at is `held` and reads as delayed too, and the two legacy `failed` rows that were paid
// back this morning say **Returned to balance** (T48 — never "Refunded", which left the admin
// asking "back to the balance or what?"), on the evidence of the ledger's own `cancel` entry, and
// offer a **Schedule again** button rather than making the user retype the wallet. The arrival is
// the API's `eta_at`/`eta_note`, rendered relative AND absolute, never computed here.
//
// It sits above the timeline rather than inside it: the timeline is the story of everything that
// moved (both directions, tap for detail), and this is the order book — seven columns and an
// action, which is not something a five-column story can carry.
function Payouts({ requests, history }: { requests: PayoutRequest[]; history: HistoryEntry[] }) {
  const { session, route } = useStore();
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  /** the ledger's refunds, by request id — the evidence a legacy row is already settled */
  const refunded = useMemo(() => refundedIds(history), [history]);

  /**
   * T48 — the way back from a returned order to the form, filled in.
   *
   * Admin 2026-09-10 15:24Z: "Better to retry this deposit instead of just refunded back." The
   * order is not re-submitted from here: it is the SCHEDULE page's job to price a list, ask the
   * API and take the money, and a button on a balance table that spent money would be a second
   * money path. What it does is carry the two things the user would otherwise retype — the wallet
   * and the amount they asked for — and open the form on them, ASAP, ready to be looked at once
   * and pressed.
   */
  const scheduleAgain = (o: PayoutRow) => {
    const asked = requestedGroth(o);
    if (asked !== null) setSchedulePrefill({ W: o.W, groth: asked });
    route.navigate('schedule');
  };

  const cancel = async (id: string) => {
    if (!window.confirm('Cancel this order? The amount and both fees go back to Available.')) return;
    setCancelling(id);
    setCancelError(null);
    try {
      await api.cancelWithdrawal(id);
      await session.refreshAccount();
    } catch (e) {
      // the API's own sentence: it decides what may still be cancelled, and it says why not
      setCancelError(errorText(e));
    } finally {
      setCancelling(null);
    }
  };

  return (
    <section className="card" data-testid="payouts-card">
      <div className="card-head">
        <h2>Payouts</h2>
        <span className="tiny muted">{requests.length} shown · newest first</span>
      </div>
      {cancelError && (
        <div className="banner banner-error" data-testid="payout-cancel-error">
          {cancelError}
        </div>
      )}
      {requests.length === 0 ? (
        <div className="empty">No payouts yet. Schedule one and it appears here with its status and arrival time.</div>
      ) : (
        /* T53 — the seven columns FIT (T44 measured 1110 px of table in a 1036 px box, which put
           "Arrives" — the arrival estimate and the bridge-explorer link — permanently behind a
           horizontal scroll). `data` + `orders` is the money-table language both pages use: fixed
           column widths, sentences that wrap instead of widening a column, and on a phone the
           whole thing becomes one card per order (the `data-label` on each cell is what names the
           value there, in place of the header row). */
        <div className="table-wrap">
          <table className="data orders" data-testid="payouts">
            <thead>
              <tr>
                <th>Status</th>
                <th>Wallet</th>
                <th className="num">Wallet receives</th>
                <th className="num">Fee</th>
                <th>Deliver at</th>
                <th>To the bridge at</th>
                <th>Arrives</th>
              </tr>
            </thead>
            <tbody>
              {requests.map((row) => {
                const o = row as PayoutRow;
                const isRefunded = refunded.has(o._id);
                const view = payoutView(o, isRefunded);
                const delivered = deliveredGroth(o);
                const fromAmount = feeMode(o) === 'from_amount';
                return (
                  <tr key={o._id} data-testid="payout-row" data-request={o._id} data-status={o.status} data-shown={view.key}>
                    {/* Cancel sits with the status that permits it — an eighth column put the
                        button past the right edge of the card (screen review 2026-09-10). */}
                    <td data-label="Status">
                      <PayoutStatusCell row={o} refunded={isRefunded} />
                      {view.cancellable && (
                        <div style={{ marginTop: 6 }}>
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={cancelling === o._id}
                            onClick={() => cancel(o._id)}
                            data-testid={`payout-cancel-${o._id}`}
                          >
                            {cancelling === o._id ? 'Cancelling…' : 'Cancel'}
                          </button>
                        </div>
                      )}
                      {/* A returned order is the only row with nothing to wait for and something
                          worth doing: the money is in Available and the wallet still has no gas. */}
                      {view.key === 'refunded' && (
                        <div style={{ marginTop: 6 }}>
                          <button
                            type="button"
                            className="btn btn-sm"
                            onClick={() => scheduleAgain(o)}
                            data-testid={`payout-again-${o._id}`}
                          >
                            Schedule again
                          </button>
                        </div>
                      )}
                    </td>
                    <td className="mono" data-label="Wallet" title={o.W}>
                      {shortAddr(o.W, 6, 4)}
                    </td>
                    <td className="num" data-label="Wallet receives">
                      {delivered === null ? fmtGroth(o.amount_groth) : fmtGroth(delivered)} {o.asset}
                      {fromAmount && (
                        <div className="tiny muted status-sub" data-testid={`payout-fee-mode-${o._id}`}>
                          {/* ⛔ `requested_groth`, NOT `amount_groth`: on a stored payout row
                              `amount_groth` IS the delivery (the release spends it), so reading it
                              here printed the SAME number twice — "0.0488 ETH · of 0.0488 ETH
                              asked" — and the one thing this line exists to say, that the order
                              shrank, was the one thing it could not say. */}
                          of {fmtGroth(requestedGroth(o) ?? o.amount_groth)} {o.asset} asked · fees taken from the amount
                        </div>
                      )}
                    </td>
                    <td className="num" data-label="Fee">
                      {fmtGroth(o.fee_groth)}
                      {typeof o.bridge_fee_groth === 'number' && (
                        <div className="tiny muted" data-testid={`payout-bridge-${o._id}`}>
                          + {fmtGroth(o.bridge_fee_groth)} {o.mode === 'instant' ? 'gas' : 'bridge'}
                        </div>
                      )}
                      {/* T45 — the bridge fee is an ESTIMATE (a live gas read plus the headroom
                          the wait needs), so "at cost" is a claim about what is KEPT. Whatever
                          the crossing did not spend is credited back to Available at settlement,
                          and this is where the user sees that it happened. Only when there IS
                          something to say: a "0 back" line under every row is noise, and the API
                          sends 0 on purpose so this is a number and not a guess about presence.

                          ⛔ NOT THE WORD "REFUNDED" (T48, admin 2026-09-10 15:24Z about the
                          status pill: *"Avoid status Refunded, it's not clear for the user …
                          Refunded back to the balance or what?"*). The brief for this line wrote
                          "bridge fee refunded"; the admin's later note is about the word itself,
                          on this page, and it is the same question a person would ask here. So it
                          says where the money IS, in the sentence the returned rows already use. */}
                      {typeof o.bridge_fee_refund_groth === 'number' && o.bridge_fee_refund_groth > 0 && (
                        <div className="tiny muted" data-testid={`payout-bridge-refund-${o._id}`}>
                          bridge fee: {fmtGroth(o.bridge_fee_refund_groth)} {o.asset} back in Available
                        </div>
                      )}
                    </td>
                    <td className="nowrap" data-label="Deliver at">
                      {shortTime(o.deliver_at)}
                    </td>
                    <td className="nowrap" data-label="To the bridge at">
                      {shortTime(o.release_at)}
                    </td>
                    <td data-label="Arrives" data-testid={`payout-eta-${o._id}`}>
                      {view.key === 'sent' && o.eth_tx ? (
                        <>
                          <span className="nowrap strong">delivered</span>
                          <div className="tiny">
                            <TxLink chainId={1} hash={o.eth_tx}>
                              view the transaction
                            </TxLink>
                          </div>
                        </>
                      ) : (
                        <PayoutEta row={o} />
                      )}
                      {/* T31b item 8 — while it is crossing, and after: the bridge's own explorer,
                          keyed on the Beam block the API recorded. Absent until there is one. */}
                      {o.beam_height ? (
                        <div className="tiny">
                          <BridgeLink height={o.beam_height} label="track on the bridge explorer" />
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="tiny muted" style={{ marginTop: 8 }} data-testid="payouts-note">
        An order that hits a snag on our side is marked <span className="strong">Delayed</span> — the money stays reserved and it is tried
        again, with the reason and the next attempt on the row. Cancel it while it is still ours to stop and the amount and both fees go
        back to Available.
      </p>
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

function Timeline({ deposits, requests, history }: { deposits: Deposit[]; requests: PayoutRequest[]; history: HistoryEntry[] }) {
  const { data } = useStore();
  const { chains, chainById } = data;
  const [open, setOpen] = useState<string | null>(null);
  /** the same refund evidence the Payouts card reads — one reader, so the two cannot disagree */
  const refunded = useMemo(() => refundedIds(history), [history]);

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
                              {d.beam_height ? (
                                <>
                                  <dt>On the bridge</dt>
                                  <dd>
                                    <BridgeLink height={d.beam_height} />
                                  </dd>
                                </>
                              ) : null}
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
                        {/* the same cell as the Payouts card: a story that still said "Failed"
                            while the table above it said "Returned to balance" would be two
                            answers. `surface="history"` changes ONE word — this is the story of
                            money moving, and it moved back to the tile called Available (T48). */}
                        <PayoutStatusCell row={r as PayoutRow} refunded={refunded.has(r._id)} surface="history" />
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
                            {r.beam_height ? (
                              <>
                                <dt>On the bridge</dt>
                                <dd>
                                  <BridgeLink height={r.beam_height} />
                                </dd>
                              </>
                            ) : null}
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
          {/* T31 B′: one line under the title saying what this page is for and what to do next. */}
          <p className="tiny muted" data-testid="page-lead">
            Watch a deposit land and follow every payout to the wallet — with what it costs and when it arrives. Ready to send? Go to
            Schedule.
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
        {account && <Payouts requests={account.requests} history={account.history} />}
        {account && <Timeline deposits={account.deposits} requests={account.requests} history={account.history} />}
      </SignInGate>
    </div>
  );
}
