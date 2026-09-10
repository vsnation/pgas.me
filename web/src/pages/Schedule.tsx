// Schedule: a list of orders — address, amount, when it should be there — and nothing else.
//
// Admin 2026-09-09: "user don't need to sign message or connect wallet from each wallet he wants to
// deposit ETH to. It should be a list of addresses and amounts where user can deposit. He always can
// extend and add one more. Every address scheduled like new order, user can select time when it
// should be deposited (always think that bridge takes 66 minutes, so you should send to bridge 1
// hour before user request). Always check total amount, user can't exceed total amount."
//
// So: no destination registry, no signature from the wallet being funded, no keys made in the
// browser. An address is pasted and checked (shape, then
// EIP-55 — see lib/schedule.ts), each row is its own order, and the batch is validated as one unit
// against Available before the button will do anything. The 2 % fee is charged to the balance; the
// user receives exactly what they typed and the bridge relayer fee comes out of our side.
import { useEffect, useMemo, useRef, useState } from 'react';
import { HowItWorks } from '../components/HowItWorks';
import { RequestStatusCell } from '../components/Status';
import { SignInGate } from '../components/SignInGate';
import { ApiError, api, errorText } from '../lib/api';
import { fmtGroth, fmtTime, parseGroth, shortAddr } from '../lib/format';
import {
  BRIDGE_ETA_FALLBACK_S,
  DEFAULT_PRESET,
  DELIVERY_PRESETS,
  checkAddress,
  deliveryHint,
  fromLocalInput,
  grothDecimal,
  parsePasteList,
  presetDeliverAt,
  toCanonicalList,
  toLocalInput,
  type PasteEntry,
  type PresetId,
} from '../lib/schedule';
import type { AssetKey, WithdrawalFees, WithdrawalResponse } from '../lib/types';
import { useStore } from '../state/store';

/** The restructured withdrawal is an ETH order book (API_CONTRACT.md § Withdrawals). */
const ASSET: AssetKey = 'ETH';
const NOW_TICK_MS = 30_000;

interface Row {
  id: number;
  address: string;
  amount: string;
  preset: PresetId;
  custom: string; // datetime-local value, used only while preset === 'custom'
  /**
   * Has the user been in this row yet (typed in it, changed its time, or left one of its fields)?
   * Screen review 2026-09-10: a form that opens with "row 1: enter an address · row 1: enter an
   * amount" in red is telling the user off for not having started. Validation is the same either
   * way — what waits for a touch is SAYING it.
   */
  touched?: boolean;
}

let nextRowId = 1;
const blankRow = (): Row => ({ id: nextRowId++, address: '', amount: '', preset: DEFAULT_PRESET, custom: '' });

/** An untouched row with neither an address nor an amount in it is not an order — it is a blank. */
const isBlankRow = (r: Row): boolean => !r.address.trim() && !r.amount.trim();

export function SchedulePage() {
  const { session } = useStore();
  const account = session.account;

  const [rows, setRows] = useState<Row[]>(() => [blankRow()]);
  const [fees, setFees] = useState<WithdrawalFees | null>(null);
  const [feesError, setFeesError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WithdrawalResponse | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [nowS, setNowS] = useState(() => Math.floor(Date.now() / 1000));
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [pasteReplace, setPasteReplace] = useState(false);
  const [exported, setExported] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setNowS(Math.floor(Date.now() / 1000)), NOW_TICK_MS);
    return () => clearInterval(t);
  }, []);

  // fees before the form means the minimum, the fee and the bridge ETA are the API's numbers, not a
  // second copy of them living here (the fallbacks below only cover the seconds before it answers)
  const signedIn = !!session.session;
  useEffect(() => {
    if (!signedIn) return;
    let alive = true;
    api.withdrawalFees(ASSET).then(
      (f) => alive && (setFees(f), setFeesError(null)),
      (e) => alive && setFeesError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [signedIn]);

  const feeBps = fees?.fee_bps ?? account?.fee_bps ?? 200;
  const minGroth = fees?.min_amount_groth ?? account?.min_payout_groth ?? 1_000_000;
  const etaS = fees?.bridge_eta_s ?? BRIDGE_ETA_FALLBACK_S;
  const available = account?.balances[ASSET]?.available ?? 0;
  const minLabel = `${fmtGroth(minGroth)} ${ASSET}`;

  // Parsed live as it is typed, so a bad separator or a mistyped address is visible before the user
  // presses anything. Nothing is written into the rows until "Add to the list".
  const paste = useMemo(
    () => (pasteText.trim() ? parsePasteList(pasteText, { minGroth, minLabel, now: new Date(nowS * 1000) }) : null),
    [pasteText, minGroth, minLabel, nowS],
  );

  const items = rows.map((r) => {
    const addr = checkAddress(r.address);
    const groth = parseGroth(r.amount);
    const deliverAt = r.preset === 'custom' ? fromLocalInput(r.custom) : presetDeliverAt(r.preset, new Date(nowS * 1000));
    const amountProblem = groth === null ? 'enter an amount' : groth < minGroth ? `minimum ${fmtGroth(minGroth)} ${ASSET}` : null;
    const timeProblem = deliverAt === null ? 'pick a date and time' : null;
    const blank = isBlankRow(r);
    return {
      row: r,
      address: addr.ok ? addr.address : null,
      addressProblem: addr.ok ? null : addr.reason,
      groth,
      amountProblem,
      deliverAt,
      timeProblem,
      valid: addr.ok && !amountProblem && !timeProblem,
      /** nothing in it: it is the row "+ Add another address" made, not an order the user wants */
      blank,
      /** its problems may be SAID: the user has been in it and left something in it */
      speak: !blank && !!r.touched,
    };
  });
  /** The rows that are orders. A blank row neither counts, nor blocks, nor complains. */
  const active = items.filter((i) => !i.blank);

  /** Every row that is complete enough to be exported (a half-typed row is not a line in a list). */
  const exportable = items
    .filter((i) => i.address && i.groth !== null && i.deliverAt !== null)
    .map((i) => ({ address: i.address!, groth: i.groth!, deliverAt: i.deliverAt! }));

  const totalGroth = active.reduce((s, i) => s + (i.groth ?? 0), 0);
  const feeGroth = Math.ceil((totalGroth * feeBps) / 10_000);
  const debitGroth = totalGroth + feeGroth;
  const shortfall = debitGroth - available;
  const overBudget = !!account && shortfall > 0;
  /** Everything one row is still missing, in the user's words. */
  const rowProblem = (i: (typeof items)[number], n: number) => {
    const label = i.address ? shortAddr(i.address) : `row ${n + 1}`;
    return [i.addressProblem, i.amountProblem, i.timeProblem].filter(Boolean).map((p) => `${label}: ${p}`);
  };
  const rowProblems = items.flatMap((i, n) => (i.blank ? [] : rowProblem(i, n)));
  const batchProblems = [
    ...(overBudget
      ? [
          `short by ${fmtGroth(shortfall)} ${ASSET} — Available is ${fmtGroth(available)}, this batch debits ${fmtGroth(debitGroth)} (amounts + ${feeBps / 100}%)`,
        ]
      : []),
    // the operator's switch, in the user's words — the flag it reads is not their business
    ...(account && !account.modes.direct ? ['payouts are paused right now'] : []),
    // a pasted list with a bad line is not a list yet: nothing is scheduled while one is on screen
    ...(pasteOpen && paste && paste.errors > 0
      ? [`the pasted list has ${paste.errors} line${paste.errors === 1 ? '' : 's'} that cannot be read — fix or clear it`]
      : []),
  ];
  /**
   * What the user is TOLD, which is not the same list as what BLOCKS: a row nobody has been in yet
   * still holds the button down (it would be an order with no address), and says nothing about it.
   * The gate and the words come from the same computation — only the audience differs.
   */
  const problems = [...items.flatMap((i, n) => (i.speak ? rowProblem(i, n) : [])), ...batchProblems];
  const valid = rowProblems.length === 0 && batchProblems.length === 0 && !!account && active.length > 0;
  /** Nothing typed anywhere yet: one muted line, not a list of complaints. */
  const pristine = active.length === 0 && problems.length === 0;

  const setRow = (id: number, patch: Partial<Row>) => setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch, touched: true } : r)));
  /** Leaving a field counts as having been in the row, even when nothing was typed in it. */
  const touchRow = (id: number) => setRows((rs) => rs.map((r) => (r.id === id ? { ...r, touched: true } : r)));
  const addRow = () => setRows((rs) => [...rs, blankRow()]);
  const removeRow = (id: number) => setRows((rs) => (rs.length === 1 ? [blankRow()] : rs.filter((r) => r.id !== id)));

  /** Pasted lines become rows — appended, unless the user asked for them to replace what is there. */
  const applyPaste = () => {
    if (!paste || paste.errors > 0 || paste.parsed === 0) return;
    const made: Row[] = paste.entries.map((e) => ({
      id: nextRowId++,
      address: e.address ?? '',
      amount: e.groth !== null ? grothDecimal(e.groth) : '',
      preset: e.preset,
      custom: e.custom,
      touched: true, // pasted IS typed: a line the user brought may say what is wrong with it
    }));
    setRows((rs) => (pasteReplace ? made : [...rs.filter((r) => r.address.trim() || r.amount.trim()), ...made]));
    setPasteText('');
    setPasteOpen(false);
    setPasteReplace(false);
  };

  /**
   * The list, in the one shape this page also reads back: `address,amount,deliver_at`. It goes to
   * the clipboard AND into a box on the page, because a browser that refuses `navigator.clipboard`
   * (an iframe, a locked-down phone) would otherwise "copy" nothing and say so nowhere.
   */
  const copyList = async () => {
    const text = toCanonicalList(exportable);
    setExported(text);
    setCopied(false);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // no clipboard permission: the box below is the copy
    }
  };

  const presetLabel = (id: PresetId) => DELIVERY_PRESETS.find((p) => p.id === id)?.label ?? id;
  const pasteWhen = (e: PasteEntry) => (e.preset === 'custom' ? e.custom.replace('T', ' ') : presetLabel(e.preset));

  const submit = async () => {
    if (!valid) return;
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.withdraw({
        asset: ASSET,
        // `active`, not `items`: a blank row is not an order and must not travel as one
        items: active.map((i) => ({ W: i.address!, amount_groth: i.groth!, deliver_at: i.deliverAt! })),
        mode: 'direct',
      });
      setResult(r);
      setRows([blankRow()]);
      void session.refreshAccount();
    } catch (e) {
      // a 409 is the API's own shortfall sentence — the batch was validated as one unit, and its
      // numbers beat anything computed here (the balance can move between render and submit)
      setError(e instanceof ApiError ? e.detail : errorText(e));
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async (id: string) => {
    if (!window.confirm('Cancel this scheduled order? The amount and fee return to Available.')) return;
    setCancelling(id);
    setCancelError(null);
    try {
      await api.cancelWithdrawal(id);
      await session.refreshAccount();
    } catch (e) {
      setCancelError(errorText(e));
    } finally {
      setCancelling(null);
    }
  };

  const newIds = useMemo(() => new Set(result?.items?.map((i) => i.request_id) ?? result?.request_ids ?? []), [result]);
  const orders = account?.requests ?? [];

  /**
   * The phone bar repeats the two numbers that decide whether the button can be pressed, for while
   * the Totals card is off screen. Screen review 2026-09-10: it was also drawn ON TOP of that card
   * — a fixed bar sitting over "Total debited" at 390×844 — because a sticky element hovers over
   * whatever follows it until the scroll reaches its own place in the flow. So it is drawn only
   * while the card it repeats cannot be seen, which is the only time it is worth anything.
   */
  const totalsRef = useRef<HTMLElement | null>(null);
  const [totalsSeen, setTotalsSeen] = useState(false);
  const hasAccount = !!account;
  useEffect(() => {
    const el = totalsRef.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver((entries) => entries.forEach((e) => setTotalsSeen(e.isIntersecting)), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, [hasAccount]);

  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Schedule</h1>
          <p className="muted">
            Send ETH from your balance to any wallets, at the time you choose. <HowItWorks />
          </p>
        </div>
      </div>
      <SignInGate what="payouts" verb="schedule">
        {!account ? (
          <p className="muted">Loading the account…</p>
        ) : (
          <>
            <div className="grid-2">
              <section className="card stack" data-testid="schedule-form">
                <div className="card-head">
                  <h2>Orders to schedule</h2>
                  <span className="tiny muted">bridge takes {Math.round(etaS / 60)} min · sent that far ahead of your time</span>
                </div>
                {feesError && (
                  <div className="banner banner-warn" data-testid="fees-error">
                    Fees could not be read ({feesError}) — showing the last known 2 % / {Math.round(etaS / 60)} min.
                  </div>
                )}
                <div className="row paste-actions">
                  <button
                    type="button"
                    className="btn btn-sm"
                    aria-expanded={pasteOpen}
                    data-testid="paste-toggle"
                    onClick={() => setPasteOpen((o) => !o)}
                  >
                    {pasteOpen ? 'Close the paste box' : 'Paste a list'}
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm"
                    data-testid="copy-list"
                    disabled={exportable.length === 0}
                    onClick={() => void copyList()}
                  >
                    Copy list
                  </button>
                </div>

                {pasteOpen && (
                  <div className="stack-sm paste-panel" data-testid="paste-panel">
                    <textarea
                      className="textarea mono"
                      rows={6}
                      spellCheck={false}
                      autoComplete="off"
                      aria-label="Paste a list"
                      data-testid="paste-input"
                      value={pasteText}
                      onChange={(e) => setPasteText(e.target.value)}
                      placeholder={'0xAbC…,0.05,asap\n0xDeF…\t0.1\t2026-09-10 03:00\n0x123…:0.02;0x456…:0.02'}
                    />
                    <span className="help tiny">
                      One order per line, or separated by “;”. Address, amount, and — if you want one — a delivery time (asap, 2h, tonight,
                      tomorrow, or 2026-09-10 03:00). Space, tab, comma, colon or “=” between them; a spreadsheet’s header row is skipped.
                    </span>
                    {paste && (
                      <>
                        <div className="row paste-head">
                          <span className="tiny strong" data-testid="paste-summary">
                            {paste.summary}
                          </span>
                          <label className="tiny row paste-replace">
                            <input
                              type="checkbox"
                              data-testid="paste-replace"
                              checked={pasteReplace}
                              onChange={(e) => setPasteReplace(e.target.checked)}
                            />
                            Replace rows
                          </label>
                        </div>
                        <ol className="paste-lines" data-testid="paste-lines">
                          {paste.entries.map((e) => (
                            <li key={e.n} data-testid={`paste-line-${e.n - 1}`} data-status={e.mark}>
                              <span className={`paste-mark paste-${e.mark}`} aria-hidden="true">
                                {e.mark === 'ok' ? '✓' : e.mark === 'warn' ? '⚠' : '✗'}
                              </span>
                              <span className="mono tiny paste-addr">{e.address ?? e.raw}</span>
                              <span className="tiny num">{e.groth !== null ? `${grothDecimal(e.groth)} ${ASSET}` : '—'}</span>
                              <span className={`tiny ${e.error ? 'error-text' : 'muted'}`} data-testid={`paste-note-${e.n - 1}`}>
                                {e.error ?? e.warning ?? pasteWhen(e)}
                              </span>
                            </li>
                          ))}
                        </ol>
                        <div className="row">
                          <button
                            type="button"
                            className="btn btn-primary btn-sm"
                            data-testid="paste-apply"
                            disabled={paste.errors > 0 || paste.parsed === 0}
                            onClick={applyPaste}
                          >
                            {pasteReplace ? `Replace the rows with ${paste.ok}` : `Add ${paste.ok} to the list`}
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                )}

                {exported !== null && (
                  <div className="stack-sm" data-testid="copy-list-panel">
                    <span className="help tiny">
                      {copied
                        ? 'Copied. It is also here, if you would rather select it yourself.'
                        : 'Your browser did not let the page use the clipboard — select it here.'}
                    </span>
                    <textarea
                      className="textarea mono"
                      rows={4}
                      readOnly
                      aria-label="Your list"
                      data-testid="copy-list-output"
                      value={exported}
                    />
                  </div>
                )}

                <div className="list" data-testid="schedule-rows">
                  {items.map((it, n) => {
                    const r = it.row;
                    return (
                      <div className="sched-row" key={r.id} data-testid="schedule-row" data-row={n}>
                        <div className="field sched-addr">
                          <input
                            className="input mono"
                            placeholder="0x… wallet to fund"
                            spellCheck={false}
                            autoComplete="off"
                            value={r.address}
                            aria-label={`Address ${n + 1}`}
                            aria-invalid={!!r.address && !!it.addressProblem}
                            onBlur={() => touchRow(r.id)}
                            onChange={(e) => setRow(r.id, { address: e.target.value })}
                          />
                          {r.address !== '' && it.addressProblem && it.speak && (
                            <span className="error-text tiny" data-testid={`address-error-${n}`}>
                              {it.addressProblem}
                            </span>
                          )}
                          {it.address && it.address !== r.address.trim() && (
                            <span className="help tiny mono" data-testid={`address-checksummed-${n}`}>
                              {it.address}
                            </span>
                          )}
                        </div>

                        <div className="field sched-amt">
                          <div className="input-wrap">
                            <input
                              className="input num"
                              inputMode="decimal"
                              placeholder="0.00"
                              value={r.amount}
                              aria-label={`Amount ${n + 1}`}
                              aria-invalid={!!r.amount && !!it.amountProblem}
                              onBlur={() => touchRow(r.id)}
                              onChange={(e) => setRow(r.id, { amount: e.target.value })}
                            />
                            <span className="suffix tiny muted">{ASSET}</span>
                          </div>
                          {r.amount !== '' && it.amountProblem && it.speak ? (
                            <span className="error-text tiny" data-testid={`amount-error-${n}`}>
                              {it.amountProblem}
                            </span>
                          ) : (
                            <span className="help tiny">
                              min {fmtGroth(minGroth)} {ASSET}
                            </span>
                          )}
                        </div>

                        <div className="field sched-when">
                          <select
                            className="select"
                            value={r.preset}
                            aria-label={`Deliver ${n + 1}`}
                            onChange={(e) => {
                              const preset = e.target.value as PresetId;
                              setRow(r.id, {
                                preset,
                                custom:
                                  preset === 'custom' && !r.custom ? toLocalInput(presetDeliverAt('2h', new Date(nowS * 1000))) : r.custom,
                              });
                            }}
                          >
                            {DELIVERY_PRESETS.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.label}
                              </option>
                            ))}
                          </select>
                          {r.preset === 'custom' && (
                            <input
                              className="input"
                              type="datetime-local"
                              value={r.custom}
                              aria-label={`Custom time ${n + 1}`}
                              aria-invalid={!!it.timeProblem}
                              onChange={(e) => setRow(r.id, { custom: e.target.value })}
                            />
                          )}
                        </div>

                        <div className="sched-rm">
                          <button
                            type="button"
                            className="btn btn-sm btn-ghost"
                            aria-label={`Remove row ${n + 1}`}
                            onClick={() => removeRow(r.id)}
                          >
                            ✕
                          </button>
                        </div>

                        <div className="sched-foot tiny muted" data-testid={`deliver-hint-${n}`}>
                          {it.deliverAt === null ? 'pick a date and time' : deliveryHint(it.deliverAt, nowS, etaS)}
                        </div>
                      </div>
                    );
                  })}
                </div>
                <div className="row">
                  <button type="button" className="btn" onClick={addRow} data-testid="schedule-add">
                    + Add another address
                  </button>
                </div>
              </section>

              <section className="card stack schedule-totals" data-testid="schedule-totals" ref={totalsRef}>
                <h2>Total</h2>
                <dl className="kv">
                  <dt>Orders</dt>
                  <dd data-testid="total-orders">{active.length}</dd>
                  <dt>Amounts</dt>
                  <dd className="num" data-testid="total-amount">
                    {fmtGroth(totalGroth)} {ASSET}
                  </dd>
                  <dt>Fee ({feeBps / 100}%)</dt>
                  <dd className="num" data-testid="total-fee">
                    {fmtGroth(feeGroth)} {ASSET}
                  </dd>
                  <dt>Total debited</dt>
                  <dd className="num strong" data-testid="total-debited">
                    {fmtGroth(debitGroth)} {ASSET}
                  </dd>
                  <dt>Available</dt>
                  <dd className="num" data-testid="total-available">
                    {fmtGroth(available)} {ASSET}
                  </dd>
                  <dt>Remaining</dt>
                  <dd className={`num${overBudget ? ' error-text' : ''}`} data-testid="total-remaining">
                    {fmtGroth(available - debitGroth)} {ASSET}
                  </dd>
                </dl>
                {problems.length > 0 && (
                  <ul className="small error-text" style={{ margin: 0, paddingLeft: 18 }} data-testid="schedule-problems">
                    {problems.map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                )}
                {/* a form nobody has typed in yet: one muted line, and a button that stays down */}
                {pristine && (
                  <span className="help" data-testid="schedule-hint">
                    Add a wallet and an amount.
                  </span>
                )}
                <button
                  type="button"
                  className="btn btn-primary btn-lg"
                  disabled={!valid || submitting}
                  onClick={submit}
                  data-testid="schedule-submit"
                >
                  {submitting
                    ? 'Scheduling…'
                    : active.length === 0
                      ? 'Schedule orders'
                      : `Schedule ${active.length} order${active.length === 1 ? '' : 's'}`}
                </button>
                <span className="help" data-testid="fee-line">
                  Fee {feeBps / 100} % · bridge fee paid by Pgas.me · you receive exactly what you enter
                </span>
                {error && (
                  <div className="banner banner-error" data-testid="schedule-error">
                    {error}
                  </div>
                )}
                {result && (
                  <div className="banner banner-ok stack-sm" data-testid="schedule-result" style={{ alignItems: 'flex-start' }}>
                    <span className="strong">
                      Scheduled {result.items?.length ?? result.request_ids.length} order
                      {(result.items?.length ?? result.request_ids.length) === 1 ? '' : 's'}.
                    </span>
                    <span className="num">
                      Debited {fmtGroth(result.total_debited_groth)} {ASSET} (fee {fmtGroth(result.fee_groth)}).
                    </span>
                    <span className="small">They are listed below with their status.</span>
                  </div>
                )}
              </section>
            </div>

            {/* Phones only (CSS): the two numbers that decide whether the button can be pressed at
                all, kept above the tab bar while the list of rows scrolls. Same values as the card
                above — one computation, rendered twice, never a second sum. */}
            <div
              className={`sticky-totals${totalsSeen ? ' sticky-totals-off' : ''}`}
              data-testid="sticky-totals"
              data-shown={totalsSeen ? 'no' : 'yes'}
              aria-hidden="true"
            >
              <span>
                Total{' '}
                <span className="num strong">
                  {fmtGroth(debitGroth)} {ASSET}
                </span>
              </span>
              <span className={overBudget ? 'error-text' : ''}>
                Remaining{' '}
                <span className="num">
                  {fmtGroth(available - debitGroth)} {ASSET}
                </span>
              </span>
            </div>

            <section className="card">
              <div className="card-head">
                <h2>Scheduled orders</h2>
                <span className="tiny muted">{orders.length} shown · cancel while scheduled</span>
              </div>
              {cancelError && <div className="banner banner-error">{cancelError}</div>}
              {orders.length === 0 ? (
                <div className="empty">Nothing scheduled yet.</div>
              ) : (
                <div className="table-wrap">
                  <table className="data" data-testid="schedule-orders">
                    <thead>
                      <tr>
                        <th>Status</th>
                        <th>Wallet</th>
                        <th className="num">Amount</th>
                        <th className="num">Fee</th>
                        <th>Deliver at</th>
                        <th>To the bridge at</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map((o) => (
                        <tr key={o._id} data-new={newIds.has(o._id) ? '' : undefined} className={newIds.has(o._id) ? 'row-new' : ''}>
                          <td>
                            <RequestStatusCell request={o} />
                          </td>
                          <td className="mono" title={o.W}>
                            {shortAddr(o.W, 8, 6)}
                          </td>
                          <td className="num">
                            {fmtGroth(o.amount_groth)} {o.asset}
                          </td>
                          <td className="num">{fmtGroth(o.fee_groth)}</td>
                          <td className="nowrap">{fmtTime(o.deliver_at)}</td>
                          <td className="nowrap">{fmtTime(o.release_at)}</td>
                          <td>
                            {o.status === 'scheduled' && (
                              <button
                                type="button"
                                className="btn btn-sm"
                                disabled={cancelling === o._id}
                                onClick={() => cancel(o._id)}
                                data-testid={`cancel-${o._id}`}
                              >
                                {cancelling === o._id ? 'Cancelling…' : 'Cancel'}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="tiny muted" style={{ marginTop: 8 }}>
                An order goes to the bridge {Math.round(etaS / 60)} minutes before your delivery time, and never earlier than now — so a
                time sooner than that arrives as soon as the bridge can manage it, not before.
              </p>
            </section>
          </>
        )}
      </SignInGate>
    </div>
  );
}
