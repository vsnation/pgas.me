// Withdraw: tick destinations, set an amount per wallet, choose Direct/Instant and a window.
import { useEffect, useMemo, useState } from 'react';
import { SignInGate } from '../components/SignInGate';
import { GradeText } from '../components/Status';
import { api, errorText } from '../lib/api';
import { GROTH, fmtDuration, fmtGroth, parseGroth, shortAddr } from '../lib/format';
import type { AssetKey, Destination, PayoutMode, WithdrawalResponse } from '../lib/types';
import { useStore } from '../state/store';

const WINDOWS: { id: string; label: string; s: number | null }[] = [
  { id: '0', label: 'Instant (≤ 60 s)', s: 0 },
  { id: '600', label: '10 min', s: 600 },
  { id: '3600', label: '1 h', s: 3600 },
  { id: '21600', label: '6 h', s: 21600 },
  { id: '86400', label: '24 h', s: 86400 },
  { id: 'custom', label: 'Custom', s: null },
];
const MAX_WINDOW_S = 30 * 86400;

export function WithdrawPage() {
  const { session, data } = useStore();
  const { assets } = data;
  const account = session.account;

  const [asset, setAsset] = useState<AssetKey>('ETH');
  const [dests, setDests] = useState<Destination[] | null>(null);
  const [destsError, setDestsError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  const [amounts, setAmounts] = useState<Record<string, string>>({});
  const [splitTotal, setSplitTotal] = useState('');
  const [mode, setMode] = useState<PayoutMode>('direct');
  const [windowId, setWindowId] = useState('3600');
  const [customMin, setCustomMin] = useState('120');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WithdrawalResponse | null>(null);

  useEffect(() => {
    if (!session.session) return;
    let alive = true;
    api
      .destinations()
      .then((r) => alive && setDests(Array.isArray(r.destinations) ? r.destinations : []))
      .catch((e) => alive && setDestsError(errorText(e)));
    return () => {
      alive = false;
    };
  }, [session.session, account?.destinations]);

  // pick the first enabled mode when the current one is disabled
  useEffect(() => {
    if (!account) return;
    if (!account.modes[mode]) {
      if (account.modes.direct) setMode('direct');
      else if (account.modes.instant) setMode('instant');
    }
  }, [account, mode]);

  const feeBps = account?.fee_bps ?? 200;
  const minPayout = account?.min_payout_groth ?? 1_000_000;
  const denominations = account?.denominations?.length ? account.denominations : [1_000_000, 10_000_000];
  const available = account?.balances[asset]?.available ?? 0;
  const selected = useMemo(() => (dests ?? []).filter((d) => ticked[d.address]), [dests, ticked]);

  const items = selected.map((d) => {
    const g = parseGroth(amounts[d.address] ?? '');
    let problem: string | null = null;
    if (g === null) problem = 'enter an amount';
    else if (g < minPayout) problem = `minimum ${fmtGroth(minPayout)} ${asset}`;
    else if (mode === 'instant' && !denominations.some((den) => g % den === 0))
      problem = `instant mode needs a multiple of ${denominations.map((x) => fmtGroth(x)).join(' or ')}`;
    return { W: d.address, amount_groth: g ?? 0, problem };
  });
  const totalGroth = items.reduce((s, i) => s + i.amount_groth, 0);
  const feeGroth = Math.ceil((totalGroth * feeBps) / 10_000);
  const debitGroth = totalGroth + feeGroth;
  const windowS = windowId === 'custom' ? Math.round(Number(customMin) * 60) : Number(windowId);
  const windowProblem =
    windowId === 'custom' && (!Number.isFinite(windowS) || windowS < 0 || windowS > MAX_WINDOW_S)
      ? 'window must be between 0 and 30 days'
      : null;
  const overBudget = account ? debitGroth > available : false;
  const problems = [
    ...items.filter((i) => i.problem).map((i) => `${shortAddr(i.W)}: ${i.problem}`),
    ...(selected.length === 0 ? ['tick at least one wallet'] : []),
    ...(overBudget ? [`Available ${fmtGroth(available)} ${asset} is below ${fmtGroth(debitGroth)} (amounts + ${feeBps / 100}%)`] : []),
    ...(windowProblem ? [windowProblem] : []),
    ...(account && !account.modes[mode] ? [`${mode} mode is not enabled yet`] : []),
  ];
  const valid = problems.length === 0 && !!account;

  const splitEvenly = () => {
    const total = parseGroth(splitTotal);
    if (total === null || selected.length === 0) return;
    const step = minPayout; // 0.01 steps
    const units = Math.floor(total / step);
    const base = Math.floor(units / selected.length);
    let rest = units - base * selected.length;
    const next: Record<string, string> = { ...amounts };
    for (const d of selected) {
      const u = base + (rest > 0 ? 1 : 0);
      if (rest > 0) rest--;
      next[d.address] = ((u * step) / GROTH).toFixed(2);
    }
    setAmounts(next);
  };

  const submit = async () => {
    if (!valid) return;
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.withdraw({
        asset,
        items: items.map((i) => ({ W: i.W, amount_groth: i.amount_groth })),
        mode,
        window_s: windowS,
      });
      setResult(r);
      setTicked({});
      setAmounts({});
      void session.refreshAccount();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setSubmitting(false);
    }
  };

  const example = totalGroth > 0 ? totalGroth : 10_000_000;
  const exampleCost = example + Math.ceil((example * feeBps) / 10_000);

  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Withdraw</h1>
          <p className="muted">
            Fund registered wallets with gas. Each wallet gets its own release time inside the window; the 2% fee is debited from the
            balance at unlock.
          </p>
        </div>
      </div>
      <SignInGate what="withdrawals">
        {!account ? (
          <p className="muted">Loading the account…</p>
        ) : (
          <div className="grid-2">
            <section className="card stack" data-testid="withdraw-form">
              <div className="field">
                <span className="label">Asset</span>
                <div className="seg" role="radiogroup" aria-label="Asset">
                  {(assets.length ? assets.map((a) => a.key) : (['ETH'] as AssetKey[])).map((k) => (
                    <button
                      key={k}
                      type="button"
                      role="radio"
                      aria-checked={asset === k}
                      className={asset === k ? 'active' : ''}
                      onClick={() => setAsset(k)}
                    >
                      {k}
                    </button>
                  ))}
                </div>
                <span className="help num">
                  Available {fmtGroth(available)} {asset}
                </span>
              </div>

              <div className="field">
                <span className="label">Wallets to fund</span>
                {destsError && <div className="banner banner-error">{destsError}</div>}
                {!dests && !destsError && <span className="muted small">Loading destinations…</span>}
                {dests && dests.length === 0 && <div className="empty">No destinations yet — add one on the Wallets page.</div>}
                {dests && dests.length > 0 && (
                  <div className="list">
                    {dests.map((d) => {
                      const on = !!ticked[d.address];
                      const item = items.find((i) => i.W === d.address);
                      return (
                        <div key={d.address} className={`list-row${on ? ' selected' : ''}`} data-address={d.address}>
                          <label className="check" style={{ flex: '1 1 260px', minWidth: 0 }}>
                            <input
                              type="checkbox"
                              checked={on}
                              onChange={(e) => setTicked((t) => ({ ...t, [d.address]: e.target.checked }))}
                              aria-label={`Fund ${d.address}`}
                            />
                            <span className="stack-sm" style={{ gap: 2, minWidth: 0 }}>
                              <span className="row" style={{ gap: 6 }}>
                                <span className={`badge-kind kind-${d.kind}`}>{d.kind}</span>
                                {d.label && <span className="strong small">{d.label}</span>}
                              </span>
                              <span className="address">{d.address}</span>
                            </span>
                          </label>
                          {on && (
                            <div className="field" style={{ width: 160 }}>
                              <div className="input-wrap">
                                <input
                                  className="input num"
                                  inputMode="decimal"
                                  type="number"
                                  step="0.01"
                                  min={fmtGroth(minPayout)}
                                  placeholder="0.00"
                                  value={amounts[d.address] ?? ''}
                                  aria-label={`Amount for ${d.address}`}
                                  aria-invalid={!!item?.problem}
                                  onChange={(e) => setAmounts((a) => ({ ...a, [d.address]: e.target.value }))}
                                />
                                <span className="suffix tiny muted">{asset}</span>
                              </div>
                              {item?.problem && <span className="error-text tiny">{item.problem}</span>}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
                {selected.length > 1 && (
                  <div className="row">
                    <input
                      className="input num"
                      style={{ width: 140 }}
                      inputMode="decimal"
                      placeholder={`total ${asset}`}
                      value={splitTotal}
                      onChange={(e) => setSplitTotal(e.target.value)}
                      aria-label="Total to split"
                    />
                    <button type="button" className="btn btn-sm" onClick={splitEvenly}>
                      Split evenly across {selected.length}
                    </button>
                  </div>
                )}
              </div>

              <div className="field">
                <span className="label">Mode</span>
                <div className="radio-group" role="radiogroup" aria-label="Mode">
                  {(
                    [
                      { id: 'direct', title: 'Direct', text: 'The Beam bridge pays each wallet itself. Bridge ≈ 1 h, up to 18 h.' },
                      { id: 'instant', title: 'Instant', text: 'A distributor pays at the release time, ≤ 60 s. Fixed denominations.' },
                    ] as { id: PayoutMode; title: string; text: string }[]
                  ).map((m) => {
                    const enabled = !!account.modes[m.id];
                    return (
                      <label key={m.id} className={`radio${mode === m.id ? ' checked' : ''}${enabled ? '' : ' disabled'}`}>
                        <input
                          type="radio"
                          name="mode"
                          value={m.id}
                          checked={mode === m.id}
                          disabled={!enabled}
                          onChange={() => setMode(m.id)}
                        />
                        <span className="stack-sm" style={{ gap: 2 }}>
                          <span className="strong">
                            {m.title}
                            {!enabled && <span className="muted"> — not enabled yet</span>}
                          </span>
                          <span className="small muted">{m.text}</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              </div>

              <div className="field">
                <label className="label" htmlFor="window">
                  Release window
                </label>
                <select id="window" className="select" value={windowId} onChange={(e) => setWindowId(e.target.value)}>
                  {WINDOWS.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.label}
                    </option>
                  ))}
                </select>
                {windowId === 'custom' && (
                  <div className="input-wrap" style={{ maxWidth: 220 }}>
                    <input
                      className="input num"
                      type="number"
                      min={0}
                      max={MAX_WINDOW_S / 60}
                      value={customMin}
                      onChange={(e) => setCustomMin(e.target.value)}
                      aria-label="Custom window in minutes"
                      aria-invalid={!!windowProblem}
                    />
                    <span className="suffix tiny muted">minutes</span>
                  </div>
                )}
                <span className="help">
                  Each wallet's release time is drawn independently inside the window. Longer windows read better on the privacy grade.
                </span>
              </div>
            </section>

            <section className="card stack" data-testid="withdraw-summary">
              <h2>Summary</h2>
              <dl className="kv">
                <dt>Wallets</dt>
                <dd>{selected.length}</dd>
                <dt>Amounts</dt>
                <dd className="num">
                  {fmtGroth(totalGroth)} {asset}
                </dd>
                <dt>Fee ({feeBps / 100}%)</dt>
                <dd className="num">
                  {fmtGroth(feeGroth)} {asset}
                </dd>
                <dt>Debited</dt>
                <dd className="num strong" data-testid="debit-total">
                  {fmtGroth(debitGroth)} {asset}
                </dd>
                <dt>Available</dt>
                <dd className={`num${overBudget ? ' error-text' : ''}`}>
                  {fmtGroth(available)} {asset}
                </dd>
                <dt>Mode</dt>
                <dd>
                  {mode} · window {fmtDuration(windowS)}
                </dd>
              </dl>
              <div className="banner" data-testid="fee-line">
                {feeBps / 100}% at unlock: funding {fmtGroth(example)} costs {fmtGroth(exampleCost)}
              </div>
              {problems.length > 0 && selected.length > 0 && (
                <ul className="small error-text" style={{ margin: 0, paddingLeft: 18 }} data-testid="withdraw-problems">
                  {problems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              )}
              <button
                type="button"
                className="btn btn-primary btn-lg"
                disabled={!valid || submitting}
                onClick={submit}
                data-testid="withdraw-submit"
              >
                {submitting
                  ? 'Submitting…'
                  : `Withdraw ${fmtGroth(totalGroth)} ${asset} to ${selected.length} wallet${selected.length === 1 ? '' : 's'}`}
              </button>
              {error && (
                <div className="banner banner-error" data-testid="withdraw-error">
                  {error}
                </div>
              )}
              {result && (
                <div className="banner banner-ok stack-sm" data-testid="withdraw-result" style={{ alignItems: 'flex-start' }}>
                  <span className="strong">
                    Scheduled {result.request_ids.length} payout request{result.request_ids.length === 1 ? '' : 's'}.
                  </span>
                  <span className="num">
                    Debited {fmtGroth(result.total_debited_groth)} {asset} (fee {fmtGroth(result.fee_groth)}).
                  </span>
                  <span>
                    ETA {fmtDuration(result.eta?.min_s)} – {fmtDuration(result.eta?.max_s)}.
                  </span>
                  <GradeText grade={result.privacy_grade} text={`Privacy grade for this withdrawal: ${result.privacy_grade}`} />
                  <span className="tiny mono wrap">{result.request_ids.join(', ')}</span>
                </div>
              )}
            </section>
          </div>
        )}
      </SignInGate>
    </div>
  );
}
