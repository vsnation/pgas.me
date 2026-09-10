/**
 * Treasury (T49) — the Beam side, in three tables that say three different things and are kept
 * apart on purpose, exactly as `/admin/treasury` assembles them:
 *
 *   * **ledger** — what we OWN, per address, from BeamPay.
 *   * **wallet** — what a send can FUND today. A max-privacy output is locked for up to 72 h after
 *     it settles, so these two have already parted company by the whole float once.
 *   * **float policy** — what the shield deliberately keeps unshielded, and the liabilities that
 *     number is protecting. Said in one sentence, in words, above the numbers.
 *
 * Every figure arrives as `{value, error}` and is rendered that way: an unreadable balance is the
 * word "unreadable" carrying the wallet's own sentence, and it is NEVER a zero.
 */
import { isPlainObject, type Row } from '../lib/adminApi';
import { amount, count, plural, span, stamp, symbolOf } from './fmt';
import { idOf, num, pick, str } from './model';
import { Empty, Figures, Id, KV, Pill, Raw, Scroll, Section, When } from './ui';

const CROSSING_BEAM_GROTH = 2 * 15_000_000; // two fee coins at 0.15 BEAM — see Overview.tsx

function read(v: unknown): { value: unknown; error: string } {
  if (isPlainObject(v) && ('value' in v || 'error' in v)) return { value: v.value, error: str(v.error) };
  return { value: v, error: '' };
}

function Amt({ of, asset }: { of: unknown; asset?: string }) {
  const { value, error } = read(of);
  if (error)
    return (
      <Pill tone="crit" title={error}>
        unreadable
      </Pill>
    );
  const n = num(value);
  return n === null ? <span className="ops-sub">–</span> : <span className="n">{amount(n, asset)}</span>;
}

export interface TreasuryProps {
  payload: unknown;
  /** `health.coins` from /admin/overview: the per-asset coin TARGETS this wallet is held to. */
  coinTargets: Record<string, { have?: number | null; target?: number | null }>;
  /** the chain tip the stats refresher last stored, for the wallet's lag */
  chainHeight: number | null;
  now: number;
}

export function Treasury({ payload, coinTargets, chainHeight, now }: TreasuryProps) {
  const t = isPlainObject(payload) ? payload : {};
  const addresses = isPlainObject(t.addresses) ? t.addresses : {};
  const registry = Array.isArray(addresses.mp_registry) ? (addresses.mp_registry as string[]) : [];
  const ledger = Array.isArray(t.ledger) ? (t.ledger as Row[]) : [];
  const wallet = Array.isArray(t.wallet) ? (t.wallet as Row[]) : [];
  const policy = isPlainObject(t.float_policy) ? t.float_policy : {};
  const perAsset = Array.isArray(policy.per_asset) ? (policy.per_asset as Row[]) : [];
  const ws = read(t.wallet_status);
  const wsv = isPlainObject(ws.value) ? ws.value : {};
  const intents = isPlainObject(t.intents) ? t.intents : {};
  const held = isPlainObject(t.held) ? t.held : {};
  const shield = Array.isArray(t.shield) ? (t.shield as Row[]) : [];

  const fees = read(t.beam_fees);
  const feeGroth = num(fees.value);
  const beamCoins = coinTargets.BEAM ?? {};
  const beamHave = num(beamCoins.have);
  const runway =
    feeGroth === null
      ? null
      : Math.min(Math.floor(feeGroth / CROSSING_BEAM_GROTH), beamHave === null ? Number.POSITIVE_INFINITY : Math.floor(beamHave / 2));

  const height = num(wsv.current_height);
  const lag = height !== null && chainHeight !== null ? chainHeight - height : null;
  const inSync = wsv.is_in_sync === true;

  const keep = num(policy.keep_groth);
  const bufferPct = num(policy.liability_buffer_bps) === null ? null : (num(policy.liability_buffer_bps) as number) / 100;

  return (
    <div data-testid="admin-object">
      <Section
        eyebrow="Balances"
        id="balances"
        note={
          num(t.at) === null ? undefined : <span title={stamp(t.at as number)}>read {span(now / 1000 - (num(t.at) as number))} ago</span>
        }
      >
        <p className="ops-sub" style={{ marginBottom: 'var(--ops-s2)' }}>
          <b>What we own</b> is the ledger; <b>what the wallet can spend</b> is what a send can fund today. A max-privacy output is locked
          for up to 72 h after it settles, so the two part company by the whole float when a shield has just landed.
        </p>
        <Scroll>
          <table className="ops-table static">
            <thead>
              <tr>
                <th>Asset</th>
                <th className="r">Ledger treasury</th>
                <th className="r">Locked</th>
                <th className="r">Float (shielded)</th>
                <th className="r">Wallet regular</th>
                <th className="r">Wallet shielded</th>
                <th className="r">Maturing</th>
                <th className="r">Coins</th>
              </tr>
            </thead>
            <tbody>
              {ledger.map((l) => {
                const a = str(l.asset);
                const w = wallet.find((r) => str(r.asset) === a) ?? {};
                const sp = read(w.spendable);
                const s = isPlainObject(sp.value) ? sp.value : {};
                const target = num((coinTargets[a] ?? {}).target);
                const cr = num(s.coins_regular);
                const csh = num(s.coins_shielded);
                const have = cr === null && csh === null ? null : (cr ?? 0) + (csh ?? 0);
                return (
                  <tr key={a} data-asset={a}>
                    <td>
                      <b>{a}</b>
                      {/* the unit once per row, never once per cell */}
                      <span className="ops-cellsub">
                        {symbolOf(a)} · aid {str(l.aid)}
                      </span>
                    </td>
                    <td className="r">
                      <Amt of={l.treasury} />
                    </td>
                    <td className="r">
                      <Amt of={l.locked} />
                    </td>
                    <td className="r">
                      <Amt of={l.float} />
                    </td>
                    <td className="r">
                      {sp.error ? (
                        <Pill tone="crit" title={sp.error}>
                          unreadable
                        </Pill>
                      ) : (
                        <span className="n">{amount(num(s.regular))}</span>
                      )}
                    </td>
                    <td className="r">
                      {sp.error ? <span className="ops-sub">–</span> : <span className="n">{amount(num(s.shielded))}</span>}
                    </td>
                    <td className="r" title="a max-privacy output is locked for up to 72 h after it settles">
                      {sp.error ? <span className="ops-sub">–</span> : <span className="n">{amount(num(s.maturing))}</span>}
                    </td>
                    <td className="r">
                      {str(s.coins_error) ? (
                        <Pill tone="warn" title={str(s.coins_error)}>
                          unreadable
                        </Pill>
                      ) : have === null ? (
                        <span className="ops-sub">–</span>
                      ) : target !== null && have < target ? (
                        <Pill
                          tone={have === 0 ? 'crit' : 'warn'}
                          title="Beam locks a whole UTXO per pending transaction — run `beam split`"
                        >
                          split · {have} of {target}
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
              {ledger.length === 0 ? (
                <tr>
                  <td colSpan={8}>
                    <Empty>No asset row came back.</Empty>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </Scroll>
      </Section>

      <Section eyebrow="BEAM fees" id="beam-fees">
        <Figures
          items={[
            { k: 'balance at the treasury', v: <Amt of={t.beam_fees} />, sub: 'BEAM' },
            { k: 'alert floor', v: <span className="n">{amount(num(t.fee_alert_groth))}</span>, sub: 'BEAM' },
            { k: 'fee coins free', v: <>{beamHave === null ? '–' : beamHave}</>, sub: `of ${num(beamCoins.target) ?? '–'}` },
            {
              k: 'per crossing',
              v: <span className="n">{amount(CROSSING_BEAM_GROTH)}</span>,
              sub: 'two coins × 0.15',
              title: "the funding transfer's fee, then the invocation's",
            },
            {
              k: 'of runway',
              v: <>{runway === null ? 'unknown' : Number.isFinite(runway) ? `≈ ${plural(runway, 'crossing')}` : '∞'}</>,
              sub: 'balance or coins, whichever runs out first',
            },
          ]}
        />
      </Section>

      <Section eyebrow="Float policy" id="float-policy">
        <p>
          The shield keeps <b>{keep === null ? 'an unstated floor' : amount(keep, 'BEAM')}</b> unshielded
          {bufferPct === null ? (
            ''
          ) : (
            <>
              {' '}
              and a <b>{bufferPct}% buffer</b> over the scheduled liabilities
            </>
          )}
          ; releases spend <b>{policy.spend_unshielded === true ? 'unshielded coins' : 'only shielded coins'}</b>.
        </p>
        <Scroll>
          <table className="ops-table static">
            <thead>
              <tr>
                <th>Asset</th>
                <th className="r">Scheduled liability</th>
                <th className="r">With buffer</th>
                <th className="r">Keep floor (BEAM-eq)</th>
                <th className="r">Keeps</th>
              </tr>
            </thead>
            <tbody>
              {perAsset.map((r) => (
                <tr key={str(r.asset)}>
                  <td>
                    <b>{str(r.asset)}</b>
                    <span className="ops-cellsub">{symbolOf(str(r.asset))}</span>
                  </td>
                  <td className="r">
                    <Amt of={r.scheduled_liability} />
                  </td>
                  <td className="r">
                    <span className="n">{amount(num(r.with_buffer_groth))}</span>
                  </td>
                  <td className="r">
                    <span className="n">{amount(num(r.keep_floor_groth))}</span>
                  </td>
                  <td className="r">
                    <span className="n">{amount(num(r.keeps_groth))}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Scroll>
      </Section>

      <Section eyebrow="Wallet" id="wallet">
        {ws.error ? (
          <p className="ops-sub">The wallet did not answer: {ws.error}</p>
        ) : (
          <KV
            rows={[
              {
                k: 'height',
                v: (
                  <>
                    <span className="n">{count(height)}</span>{' '}
                    <Pill tone={inSync ? 'good' : 'warn'}>{inSync ? 'in sync' : 'catching up'}</Pill>
                    {lag !== null ? (
                      <span className="ops-cellsub">
                        {lag === 0
                          ? 'level with the chain tip the refresher stored'
                          : `${count(Math.abs(lag))} blocks ${lag > 0 ? 'behind' : 'ahead of'} the stored tip`}
                      </span>
                    ) : null}
                  </>
                ),
              },
              { k: 'state at', v: <When at={pick(wsv, 'current_state_timestamp')} absolute /> },
              { k: 'receiving', v: <span className="n">{amount(num(wsv.receiving), 'BEAM')}</span> },
              { k: 'sending', v: <span className="n">{amount(num(wsv.sending), 'BEAM')}</span> },
            ]}
          />
        )}
      </Section>

      <Section eyebrow="Addresses" id="addresses" note={`${registry.length} max-privacy address${registry.length === 1 ? '' : 'es'}`}>
        <KV
          rows={[
            { k: 'treasury', v: <Id value={str(addresses.treasury)} head={12} tail={8} label="treasury" /> },
            { k: 'float primary', v: <Id value={str(addresses.float_primary)} head={12} tail={8} label="float primary" /> },
            {
              k: 'mp registry',
              v:
                registry.length === 0 ? (
                  <span className="ops-sub">none registered — the shielded float has nowhere to live</span>
                ) : (
                  <>
                    {registry.map((a, i) => (
                      <div key={a}>
                        <Id value={a} head={12} tail={8} label={`mp ${i + 1}`} />
                      </div>
                    ))}
                  </>
                ),
            },
          ]}
        />
        {str(t.treasury_error) ? <p className="ops-sub">treasury: {str(t.treasury_error)}</p> : null}
        {str(t.registry_error) ? <p className="ops-sub">registry: {str(t.registry_error)}</p> : null}
      </Section>

      <Section eyebrow="In flight" id="in-flight">
        <InFlight label="payout crossings" rows={(intents.payouts as Row[]) ?? []} now={now} />
        <InFlight label="treasury claims and shields" rows={(intents.treasury as Row[]) ?? []} now={now} />
        <InFlight label="held payouts" rows={(held.payouts as Row[]) ?? []} now={now} />
        <InFlight label="held deposits" rows={(held.deposits as Row[]) ?? []} now={now} />
      </Section>

      <Section eyebrow="Shield" id="shield">
        <details className="ops-disclosure">
          <summary>
            {shield.length} deposit{shield.length === 1 ? '' : 's'} in or past a claim
          </summary>
          <Scroll>
            <table className="ops-table static">
              <thead>
                <tr>
                  <th>Deposit</th>
                  <th>Stage</th>
                  <th className="r">Value</th>
                  <th className="r">Chunks sent</th>
                  <th>Claim</th>
                </tr>
              </thead>
              <tbody>
                {shield.map((s) => (
                  <tr key={str(s.deposit_id)}>
                    <td>
                      <Id value={str(s.deposit_id)} />
                    </td>
                    <td>
                      <Pill tone={str(s.treasury) === 'shielded' || str(s.treasury) === 'claimed' ? 'good' : 'warn'}>
                        {str(s.treasury)}
                      </Pill>
                    </td>
                    <td className="r">
                      <span className="n">{amount(num(s.value_groth), str(s.asset))}</span>
                    </td>
                    <td className="r">
                      <span className="n">
                        {num(s.sent) ?? 0} of {Array.isArray(s.plan) ? s.plan.length : 0}
                      </span>
                    </td>
                    <td>
                      <Id value={str(s.claim_txid)} label="claim kernel" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Scroll>
        </details>
        <Raw value={payload} label="Raw treasury payload" />
      </Section>
    </div>
  );
}

function InFlight({ label, rows, now }: { label: string; rows: Row[]; now: number }) {
  if (!Array.isArray(rows) || rows.length === 0)
    return (
      <p className="ops-empty">
        <span className="dot is-good" /> No {label}.
      </p>
    );
  return (
    <div style={{ marginBottom: 'var(--ops-s2)' }}>
      <h3 className="ops-eyebrow" style={{ marginBottom: 'var(--ops-s1)' }}>
        {label}
      </h3>
      {rows.map((r, i) => {
        const at = num(pick(r, 'status_at', 'treasury_at', 'hold_at', 'updated_at', 'created_at'));
        return (
          <div className="ops-line" key={idOf(r) || i}>
            <time>{at === null ? '–' : stamp(at).slice(11)}</time>
            <span>
              <Id value={idOf(r)} /> · {str(pick(r, 'status', 'treasury'))} ·{' '}
              {amount(num(pick(r, 'amount_groth', 'value_groth')), str(pick(r, 'asset')))}
              {at === null ? null : <span className="ops-cellsub">{span(now / 1000 - at)} in this state</span>}
              {str(pick(r, 'hold_reason', 'hold.reason')) ? (
                <span className="ops-cellsub">{str(pick(r, 'hold_reason', 'hold.reason'))}</span>
              ) : null}
            </span>
          </div>
        );
      })}
    </div>
  );
}
