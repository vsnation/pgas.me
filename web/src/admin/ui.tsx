/**
 * The console's vocabulary (T49): the handful of elements every tab is built out of.
 *
 * The rule this file exists to enforce is that state is encoded ONCE, in FORM, and the same way
 * everywhere: a tone is one of four words, a moment is always relative-then-absolute, an id is
 * always mono, truncated in the middle and copyable, and a number is always right-aligned tabular
 * mono with its unit. No component here takes a colour; they take a tone.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { explorerAddress, explorerTx } from '../lib/format';
import { amount, ago, middle, stamp, wei } from './fmt';
import { ADDR_RE, BEAM_ID_RE, HASH_RE, isGrothKey, isTimeKey, isWeiKey, looksLikeEpoch } from './model';

export type Tone = 'good' | 'warn' | 'crit' | 'neutral';

const toneClass = (t: Tone) => (t === 'neutral' ? '' : ` is-${t}`);

export function Dot({ tone, title }: { tone: Tone; title?: string }) {
  return <span className={`dot${toneClass(tone)}`} title={title} aria-hidden="true" />;
}

export function Pill({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`ops-pill${toneClass(tone)}`} title={title}>
      {children}
    </span>
  );
}

/**
 * An identifier. Mono, middle-truncated, the whole value on the title, and a click copies it —
 * which is what an operator actually does with one. `stopPropagation` because these live inside
 * rows that open a drawer, and copying an id is not asking for the drawer.
 */
export function Id({
  value,
  head = 8,
  tail = 6,
  label,
}: {
  value: string | null | undefined;
  head?: number;
  tail?: number;
  label?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => (timer.current !== null ? window.clearTimeout(timer.current) : undefined), []);
  const copy = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value);
      } catch {
        return; // no clipboard permission: the full value is on the title either way
      }
      setCopied(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1200);
    },
    [value],
  );
  if (!value) return <span className="ops-sub">–</span>;
  return (
    <button
      type="button"
      className={`ops-id${copied ? ' copied' : ''}`}
      title={`${label ? label + ': ' : ''}${value} — click to copy`}
      onClick={copy}
    >
      {copied ? 'copied' : middle(value, head, tail)}
    </button>
  );
}

export function Num({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="n" title={title}>
      {children}
    </span>
  );
}

/**
 * A moment: how long ago it was, with the UTC clock behind it. Relative first because that is the
 * question ("is this stuck?"); absolute on the title and, where there is room, beside it — a
 * console that only says "12 min ago" cannot be compared with a log.
 */
export function When({ at, absolute = false }: { at: unknown; absolute?: boolean }) {
  if (at === null || at === undefined || at === '' || (typeof at !== 'number' && typeof at !== 'string'))
    return <span className="ops-sub">–</span>;
  return (
    <span className="n" title={stamp(at)}>
      {ago(at)}
      {absolute ? <span className="ops-sub"> · {stamp(at).slice(11)}</span> : null}
    </span>
  );
}

export function TxLink({ hash, chainId = 1 }: { hash: string; chainId?: number }) {
  return (
    <a className="n" href={explorerTx(chainId, hash)} target="_blank" rel="noreferrer" title={hash} onClick={(e) => e.stopPropagation()}>
      {middle(hash, 6, 4)}
    </a>
  );
}

export function AddrLink({ address, chainId = 1 }: { address: string; chainId?: number }) {
  return (
    <a
      className="n"
      href={explorerAddress(chainId, address)}
      target="_blank"
      rel="noreferrer"
      title={address}
      onClick={(e) => e.stopPropagation()}
    >
      {middle(address, 6, 4)}
    </a>
  );
}

/**
 * A five-step progress ladder. The steps are the machine's own states in order; `at` is which one
 * the row is on; `side` is the one sentence that says what it is waiting for. A row that failed
 * carries tone `crit` and the ladder stops where it stopped rather than pretending to finish.
 */
export function Ladder({ steps, at, tone, name, side }: { steps: string[]; at: number; tone: Tone; name: string; side?: string | null }) {
  return (
    <div className={`ops-ladder${toneClass(tone)}`}>
      <span className="ops-ladder-name">{name}</span>
      <span className="ops-ladder-steps" role="img" aria-label={`${name} — step ${at + 1} of ${steps.length}`} title={steps.join(' → ')}>
        {steps.map((s, i) => (
          <i key={s} className={i < at ? 'done' : i === at ? 'now' : ''} />
        ))}
      </span>
      {side ? <span className="ops-cellsub">{side}</span> : null}
    </div>
  );
}

export function Section({ eyebrow, note, id, children }: { eyebrow: string; note?: ReactNode; id?: string; children: ReactNode }) {
  return (
    <section className="ops-section" data-section={id ?? eyebrow.toLowerCase().replace(/\s+/g, '-')}>
      <div className="ops-section-head">
        <h2 className="ops-eyebrow">{eyebrow}</h2>
        {note ? <div className="ops-section-note">{note}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Empty({ tone = 'neutral', children }: { tone?: Tone; children: ReactNode }) {
  return (
    <p className="ops-empty">
      <Dot tone={tone} />
      {children}
    </p>
  );
}

export function Figures({ items }: { items: { k: string; v: ReactNode; sub?: ReactNode; title?: string }[] }) {
  return (
    <div className="ops-figs">
      {items.map((f) => (
        <span className="ops-fig" key={f.k} title={f.title}>
          <span className="ops-fig-v">{f.v}</span>
          <span className="ops-fig-k">
            {f.k}
            {f.sub ? <span className="ops-fig-sub">{f.sub}</span> : null}
          </span>
        </span>
      ))}
    </div>
  );
}

export function Scroll({ children }: { children: ReactNode }) {
  return <div className="ops-scroll">{children}</div>;
}

/**
 * The row drawer — the ONE place on this page that gets a shadow. It is fixed to the viewport
 * rather than laid out inside a `<td>`: a table on this page is routinely wider than the screen,
 * and a drawer that inherits that width puts its own content off screen (the defect the old panel
 * carried a ResizeObserver to work around). On a phone the stylesheet drops it to a bottom sheet.
 */
export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', esc);
    return () => window.removeEventListener('keydown', esc);
  }, [onClose]);
  return (
    <>
      <button type="button" className="ops-drawer-back" aria-label="Close" onClick={onClose} />
      <aside className="ops-drawer" data-testid="admin-drawer" role="dialog" aria-label="Row detail">
        <div className="ops-drawer-head">
          <div className="ops-lead">{title}</div>
          <button type="button" className="ops-btn" onClick={onClose} data-testid="admin-drawer-close">
            Close
          </button>
        </div>
        <div className="ops-drawer-body">{children}</div>
      </aside>
    </>
  );
}

/** Everything the API sent, behind a disclosure. Collapsed by default: it is evidence, not a view. */
export function Raw({ value, label = 'Raw' }: { value: unknown; label?: string }) {
  const [copied, setCopied] = useState(false);
  const json = JSON.stringify(value, null, 2);
  return (
    <details className="ops-disclosure">
      <summary>{label}</summary>
      <button
        type="button"
        className="ops-btn"
        data-testid="admin-copy"
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(json);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          } catch {
            /* the JSON is on screen and selectable */
          }
        }}
      >
        {copied ? 'Copied' : 'Copy JSON'}
      </button>
      <pre className="ops-raw" data-testid="admin-raw">
        {json}
      </pre>
    </details>
  );
}

export function KV({ rows }: { rows: { k: string; v: ReactNode }[] }) {
  return (
    <dl className="ops-kv">
      {rows.map((r) => (
        <div key={r.k} style={{ display: 'contents' }}>
          <dt>{r.k}</dt>
          <dd>{r.v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A list of moments with words beside them: the event log, a row's own history. */
export function Timeline({ rows, max }: { rows: { at?: unknown; kind?: unknown; text?: unknown; unsent?: boolean }[]; max?: number }) {
  const shown = max ? rows.slice(0, max) : rows;
  if (shown.length === 0) return <Empty>Nothing recorded.</Empty>;
  return (
    <div className="ops-timeline">
      {shown.map((r, i) => (
        <div className={`ops-event${r.unsent ? ' unsent' : ''}`} key={i}>
          <time title={stamp(r.at as number)}>{stamp(r.at as number).slice(11)}</time>
          <span className="kind">{String(r.kind ?? '')}</span>
          <span className="text">{String(r.text ?? '')}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * One field of a row, in human units, classified by its key name. See the note on `isTimeKey` in
 * `model.ts` for why this exists and why it is deliberately conservative.
 */
export function Field({ name, value, asset, chainId = 1 }: { name: string; value: unknown; asset?: string; chainId?: number }) {
  if (value === null || value === undefined || value === '') return <span className="ops-sub">–</span>;
  if (typeof value === 'boolean') return <Pill tone={value ? 'good' : 'neutral'}>{value ? 'yes' : 'no'}</Pill>;
  if (typeof value === 'number') {
    if (isTimeKey(name) && looksLikeEpoch(value)) return <When at={value} absolute />;
    if (isGrothKey(name))
      return (
        <span className="n" title={`${value} groth`}>
          {amount(value, asset)}
        </span>
      );
    return <span className="n">{value.toLocaleString('en-US', { maximumFractionDigits: 8 })}</span>;
  }
  if (typeof value !== 'string') return <span className="n">{String(value)}</span>;
  if (isTimeKey(name) && (Number.isFinite(Number(value)) || !Number.isNaN(Date.parse(value)))) return <When at={value} absolute />;
  if (isWeiKey(name) && /^\d+$/.test(value))
    return (
      <span className="n" title={`${value} raw units`}>
        {wei(value, asset ?? 'ETH')}
      </span>
    );
  if (HASH_RE.test(value)) return <TxLink hash={value} chainId={chainId} />;
  if (ADDR_RE.test(value)) return <AddrLink address={value} chainId={chainId} />;
  if (BEAM_ID_RE.test(value)) return <Id value={value} head={10} tail={6} label="Beam kernel" />;
  if (value.length > 44) return <Id value={value} head={12} tail={8} label={name} />;
  return <span className="n">{value}</span>;
}
