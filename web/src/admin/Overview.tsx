/**
 * Overview = ATTENTION (T49).
 *
 * Not a summary of the API and not a wall of tiles: a queue of the things the machine could not
 * finish by itself, then the money, then what the last day did, then what just happened. If
 * nothing needs the operator the first thing they read is the sentence that says so.
 *
 * ⚠️ THE MONEY TABLE IS A LIVE WALLET READ AND IT SAYS HOW OLD IT IS. `/admin/treasury` costs one
 * BeamPay `/balances` per registered address per asset, one `/wallet_status` and one `get_utxo`
 * walk — the route's own docstring says so out loud. The shell refreshes it far more slowly than
 * the Mongo reads (see TREASURY_TTL_MS in Admin.tsx) and this section stamps the answer with its
 * age, because a float that is quietly thirty seconds old is the kind of number a release gets
 * decided on.
 */
import { isPlainObject, type Row } from '../lib/adminApi';
import { amount, count, plural, span, stamp, symbolOf } from './fmt';
import { idOf, needs, num, pick, str, type Need, type TabId } from './model';
import { Dot, Empty, Figures, Pill, Scroll, Section, Timeline, When, type Tone } from './ui';

/** What one bETH crossing costs in BEAM fees: TWO fee coins, floored at 0.15 BEAM each — the
 *  funding transfer's fee and then the invocation's (the project's operating doc, T40b F12). Stated here so the
 *  runway on screen can be checked against it rather than trusted. */
const CROSSING_BEAM_GROTH = 2 * 15_000_000;
const DAY_S = 24 * 3600;

/** The payout statuses that still owe a user money (payouts.LIABLE). */
const LIABLE = ['scheduled', 'delayed', 'held', 'waiting_for_dep_eth', 'waiting_for_swap_to_target_asset'];
const DELIVERED = ['sent', 'delivered'];
const TREASURY_INFLIGHT = ['claiming', 'shielding'];

function readOf(v: unknown): { value: unknown; error: string } {
  if (isPlainObject(v) && ('value' in v || 'error' in v)) return { value: v.value, error: str(v.error) };
  return { value: v, error: '' };
}

/** A treasury cell: the number, or the reason there is no number. Never a zero for "we could not look". */
function Cell({ read, asset }: { read: unknown; asset?: string }) {
  const { value, error } = readOf(read);
  if (error)
    return (
      <Pill tone="crit" title={error}>
        unreadable
      </Pill>
    );
  const n = num(value);
  if (n === null) return <span className="ops-sub">–</span>;
  return <span className="n">{amount(n, asset)}</span>;
}

export interface OverviewProps {
  overview: unknown;
  deposits: Row[];
  payouts: Row[];
  locks: Row[];
  events: Row[];
  treasury: unknown;
  treasuryAt: number | null;
  treasuryError: string | null;
  now: number;
  goTo: (tab: TabId, status?: string) => void;
}

export function Overview(p: OverviewProps) {
  const o = isPlainObject(p.overview) ? p.overview : {};
  const items = needs({ overview: p.overview, deposits: p.deposits, payouts: p.payouts, locks: p.locks, now: p.now });
  const health = isPlainObject(o.health) ? o.health : {};
  const coins = isPlainObject(health.coins) ? health.coins : {};
  const crossings = isPlainObject(o.crossings) ? o.crossings : {};

  const t = isPlainObject(p.treasury) ? p.treasury : null;
  const ledger = t && Array.isArray(t.ledger) ? (t.ledger as Row[]) : [];
  const wallet = t && Array.isArray(t.wallet) ? (t.wallet as Row[]) : [];
  const assets = ledger.length > 0 ? ledger.map((r) => str(r.asset)) : Object.keys(coins).filter((k) => k !== 'BEAM');

  const owed = (asset: string) =>
    p.payouts
      .filter((r) => str(pick(r, 'asset')) === asset && LIABLE.includes(str(pick(r, 'status'))))
      .reduce((n, r) => n + (num(pick(r, 'amount_groth')) ?? 0), 0);
  const claiming = (asset: string) =>
    p.deposits
      .filter((r) => str(pick(r, 'asset')) === asset && TREASURY_INFLIGHT.includes(str(pick(r, 'treasury'))))
      .reduce((n, r) => n + (num(pick(r, 'value_groth')) ?? 0), 0);

  const beamFees = t ? readOf(t.beam_fees) : { value: null, error: '' };
  const beamCoins = isPlainObject(coins.BEAM) ? coins.BEAM : {};
  const beamHave = num(beamFees.value);
  const beamCoinsHave = num(beamCoins.have);
  const runway =
    beamHave === null
      ? null
      : Math.min(
          Math.floor(beamHave / CROSSING_BEAM_GROTH),
          beamCoinsHave === null ? Number.POSITIVE_INFINITY : Math.floor(beamCoinsHave / 2),
        );

  const day = last24h(p.deposits, p.payouts, p.now);

  return (
    <div data-testid="admin-object">
      <Section eyebrow="Needs you" id="attention" note={items.length > 0 ? `${items.length} open` : undefined}>
        {items.length === 0 ? (
          <Empty tone="good">Nothing needs you.</Empty>
        ) : (
          <div className="ops-needs">
            {items.slice(0, 14).map((n) => (
              <NeedRow key={n.key} need={n} goTo={p.goTo} />
            ))}
            {items.length > 14 ? <p className="ops-sub">+{items.length - 14} more, in the tabs.</p> : null}
          </div>
        )}
      </Section>

      <Section
        eyebrow="Money"
        id="money"
        note={
          p.treasuryError ? (
            <span title={p.treasuryError}>the wallet read failed — see below</span>
          ) : p.treasuryAt ? (
            <span title={stamp(p.treasuryAt / 1000)}>wallet read {span((p.now - p.treasuryAt) / 1000)} ago</span>
          ) : (
            'reading the wallet…'
          )
        }
      >
        {p.treasuryError ? <p className="ops-sub">{p.treasuryError}</p> : null}
        <Scroll>
          <table className="ops-table static">
            <thead>
              <tr>
                <th>Asset</th>
                <th className="r">Treasury</th>
                <th className="r">Float</th>
                <th className="r">Can spend</th>
                <th className="r">Owed</th>
                <th className="r">In flight</th>
                <th className="r">Coins</th>
              </tr>
            </thead>
            <tbody>
              {assets.map((a) => {
                const l = ledger.find((r) => str(r.asset) === a) ?? {};
                const w = wallet.find((r) => str(r.asset) === a) ?? {};
                const spend = readOf(w.spendable);
                const sp = isPlainObject(spend.value) ? spend.value : {};
                const c = isPlainObject(coins[a]) ? (coins[a] as Row) : {};
                const have = num(c.have);
                const target = num(c.target);
                const flight = claiming(a) + (a === 'ETH' ? (num(crossings.queued_groth) ?? 0) : 0);
                return (
                  <tr key={a} data-asset={a}>
                    <td>
                      <b>{a}</b>
                      {/* the unit is named ONCE per row: repeating "bETH" in six numeric cells is
                          what pushed this table off the right edge of a 1280 screen */}
                      <span className="ops-cellsub">{symbolOf(a)}</span>
                    </td>
                    <td className="r">{t ? <Cell read={l.treasury} /> : <span className="ops-sub">–</span>}</td>
                    <td className="r" title="the shielded float, summed over the max-privacy registry">
                      {t ? <Cell read={l.float} /> : <span className="ops-sub">–</span>}
                    </td>
                    <td className="r" title="what the wallet could fund a send with today — not what we own">
                      {spend.error ? (
                        <Pill tone="crit" title={spend.error}>
                          unreadable
                        </Pill>
                      ) : t ? (
                        <span className="n">{amount((num(sp.regular) ?? 0) + (num(sp.shielded) ?? 0))}</span>
                      ) : (
                        <span className="ops-sub">–</span>
                      )}
                    </td>
                    <td className="r" title="the sum of the orders that still owe a user money">
                      <span className="n">{amount(owed(a))}</span>
                    </td>
                    <td className="r" title="claims and shields mid-flight, plus bETH funded at a crossing address">
                      <span className="n">{amount(flight)}</span>
                    </td>
                    <td className="r">
                      {have === null ? (
                        <Pill tone="warn">unreadable</Pill>
                      ) : target !== null && have < target ? (
                        <Pill
                          tone={have === 0 ? 'crit' : 'warn'}
                          title="Beam locks a whole UTXO per pending transaction — run `beam split`"
                        >
                          {have} of {target}
                        </Pill>
                      ) : (
                        <span className="n">
                          {have} of {target ?? '–'}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Scroll>
        <Figures
          items={[
            { k: 'BEAM for fees', v: t ? <Cell read={t.beam_fees} /> : <span className="ops-sub">–</span>, sub: 'at the treasury' },
            {
              k: 'fee coins free',
              v: <>{beamCoinsHave === null ? '–' : beamCoinsHave}</>,
              sub: `of ${num(beamCoins.target) ?? '–'}`,
              title: 'Beam locks a whole UTXO per pending transaction',
            },
            {
              k: 'of runway',
              v: <>{runway === null ? 'unknown' : Number.isFinite(runway) ? `≈ ${plural(runway, 'crossing')}` : '∞'}</>,
              sub: 'at 0.3 BEAM and two coins each',
              title: 'whichever runs out first — the BEAM balance, or the free coins two at a time',
            },
          ]}
        />
      </Section>

      <Section eyebrow="Last 24 hours" id="day">
        <Figures
          items={[
            {
              k: 'deposits credited',
              v: <>{count(day.creditedN)}</>,
              sub: amount(day.creditedG, 'ETH'),
              title: 'rows whose credit landed in the last 24 h, and the sum of their value_groth',
            },
            {
              k: 'payouts delivered',
              v: <>{count(day.deliveredN)}</>,
              sub: amount(day.deliveredG, 'ETH'),
              title: 'orders that reached sent/delivered, and the sum of their delivered_groth',
            },
            { k: 'fees earned', v: <span className="n">{amount(day.feeG, 'ETH')}</span>, title: 'the sum of fee_groth on those orders' },
            {
              k: 'bridge fee funded',
              v: <span className="n">{amount(day.bridgeG, 'ETH')}</span>,
              sub: `${amount(day.refundG, 'ETH')} refunded`,
              title: 'the sum of bridge_fee_groth, and of what came back at settlement',
            },
            { k: 'cancelled', v: <>{count(day.cancelledN)}</>, title: 'orders cancelled in the last 24 h' },
          ]}
        />
      </Section>

      <Section
        eyebrow="Activity"
        id="activity"
        note={
          <button type="button" className="ops-need-go" onClick={() => p.goTo('events')}>
            All events
          </button>
        }
      >
        <Timeline
          max={20}
          rows={p.events.map((e) => ({
            at: pick(e, 'at', 'created_at'),
            kind: str(pick(e, 'kind')),
            text: str(pick(e, 'text', 'message', 'note')) || idOf(e),
            unsent: e.notified === false,
          }))}
        />
      </Section>
    </div>
  );
}

function NeedRow({ need, goTo }: { need: Need; goTo: (tab: TabId, status?: string) => void }) {
  return (
    <div className={`ops-need is-${need.tone}`} data-need={need.key}>
      <Dot tone={need.tone as Tone} />
      <div>
        <div className="ops-need-what">{need.what}</div>
        <div className="ops-need-why">{need.why}</div>
      </div>
      <div className="ops-need-when">
        {need.since ? <When at={need.since} /> : null}
        {need.go ? (
          <button type="button" className="ops-need-go" onClick={() => goTo(need.go!.tab, need.go!.status)}>
            {need.go.label} →
          </button>
        ) : null}
      </div>
    </div>
  );
}

/** The day's figures, computed from the rows on screen rather than from a second aggregate. */
function last24h(deposits: Row[], payouts: Row[], now: number) {
  const since = now / 1000 - DAY_S;
  const within = (v: unknown) => {
    const n = num(v);
    return n !== null && n >= since;
  };
  let creditedN = 0;
  let creditedG = 0;
  for (const d of deposits) {
    if (str(pick(d, 'status')) !== 'credited') continue;
    if (!within(pick(d, 'credited_at', 'updated_at', 'created_at'))) continue;
    creditedN += 1;
    creditedG += num(pick(d, 'value_groth')) ?? 0;
  }
  let deliveredN = 0;
  let deliveredG = 0;
  let feeG = 0;
  let bridgeG = 0;
  let refundG = 0;
  let cancelledN = 0;
  for (const r of payouts) {
    const s = str(pick(r, 'status'));
    const at = pick(r, 'delivered_at', 'sent_at', 'status_at', 'updated_at', 'created_at');
    if (!within(at)) continue;
    if (s === 'cancelled') cancelledN += 1;
    if (!DELIVERED.includes(s)) continue;
    deliveredN += 1;
    deliveredG += num(pick(r, 'delivered_groth', 'amount_groth')) ?? 0;
    feeG += num(pick(r, 'fee_groth')) ?? 0;
    bridgeG += num(pick(r, 'bridge_fee_groth')) ?? 0;
    refundG += num(pick(r, 'bridge_fee_refund_groth')) ?? 0;
  }
  return { creditedN, creditedG, deliveredN, deliveredG, feeG, bridgeG, refundG, cancelledN };
}

/** Exported for the strip in Admin.tsx: how long ago each worker last left a trace. */
export function workerAges(overview: unknown, now: number): { name: string; age: number | null }[] {
  const o = isPlainObject(overview) ? overview : {};
  const w = isPlainObject(o.workers) ? o.workers : {};
  const last = isPlainObject(w.last_pass) ? w.last_pass : {};
  return Object.entries(last).map(([name, v]) => {
    const at = isPlainObject(v) ? num(v.at) : null;
    return { name, age: at === null ? null : now / 1000 - at };
  });
}

export { span };
