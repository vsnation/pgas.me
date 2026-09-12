// The swap panel — the shape both money pages are built out of (T53, admin 2026-09-10 15:40Z:
// "Let's match Deposit page and Withdrawal page. Quote block I don't think is actually needed when
// you can make it in What to deposit, to look like DEX Swap.").
//
// One card, read top to bottom: what leaves (a LEG), what arrives (a LEG), what it costs (the
// DETAIL STRIP), and one primary button whose label is the next thing that happens. A separate
// "Quote" card beside the form asked the user to read two boxes and hold the relationship between
// them in their head; a swap panel states the relationship as a layout.
//
// ⛔ NOTHING HERE KNOWS ANYTHING. Every piece in this file is presentation: it takes what the page
// gives it and renders it. No API call, no arithmetic on money, no opinion about what a number
// means — the pages own that, because the API owns it and they are the ones reading the API.
// (Law: one writer per fact. A shared component that "helpfully" computed a fee would be a second
// implementation of the fee model, sitting where two pages would both trust it.)
import type { CSSProperties, ReactNode, Ref } from 'react';

/** The card. `route`/`busy` are the attributes the suite reads to know which state it is in. */
export function SwapPanel({
  children,
  testId,
  className,
  route,
  busy,
  panelRef,
}: {
  children: ReactNode;
  testId?: string;
  className?: string;
  route?: string;
  busy?: string;
  panelRef?: Ref<HTMLElement>;
}) {
  return (
    <section
      ref={panelRef}
      className={`card sw${className ? ` ${className}` : ''}`}
      data-testid={testId}
      data-route={route}
      data-busy={busy}
    >
      {children}
    </section>
  );
}

/**
 * Title on the left, whatever the page needs on the right (a route toggle, a freshness line).
 *
 * `head` replaces the `<h2>` for a panel whose head is a CONTROL rather than a name — the money
 * page's `Deposit | Withdraw` (T57). A heading element wrapped round a radiogroup would announce
 * the control as a title to a screen reader, which is a different thing from what it is; and the
 * control already says what the panel is, so there is nothing left for a title to add.
 */
export function SwapHead({ title, head, sub, children }: { title?: ReactNode; head?: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="sw-head">
      <div className="sw-head-main">
        {head !== undefined ? head : <h2>{title}</h2>}
        {sub}
      </div>
      {children !== undefined && children !== null && children !== false && <div className="sw-head-aside">{children}</div>}
    </div>
  );
}

/**
 * The segmented control a panel is headed by: the money page's direction (T57), and nothing else
 * so far. Presentation only — it is handed the value and the options and reports a press.
 */
export function SwapModes<T extends string>({
  value,
  options,
  onChange,
  label,
  testId,
  disabled,
}: {
  value: T;
  options: { id: T; label: string }[];
  onChange: (id: T) => void;
  label: string;
  testId?: string;
  disabled?: boolean;
}) {
  return (
    <div className="seg seg-modes" role="radiogroup" aria-label={label} data-testid={testId} data-mode={value}>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          role="radio"
          aria-checked={value === o.id}
          className={value === o.id ? 'active' : ''}
          disabled={disabled}
          data-mode={o.id}
          data-testid={`mode-${o.id}`}
          onClick={() => onChange(o.id)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/**
 * One leg: the label and its aside on top, the big number and its control in the middle, a small
 * line underneath. `children` is the number — an `<input>` on the leg the user types in, plain
 * text on the one the API answers.
 */
export function SwapLeg({
  label,
  aside,
  control,
  sub,
  subAside,
  children,
  tone,
  testId,
}: {
  label: ReactNode;
  aside?: ReactNode;
  control?: ReactNode;
  sub?: ReactNode;
  subAside?: ReactNode;
  children: ReactNode;
  /** `out` renders the arriving side: same geometry, a quieter ground. */
  tone?: 'in' | 'out';
  testId?: string;
}) {
  return (
    <div className={`sw-leg${tone ? ` sw-leg-${tone}` : ''}`} data-testid={testId}>
      <div className="sw-leg-top">
        <span className="sw-leg-label">{label}</span>
        {aside !== undefined && aside !== null && aside !== false && <span className="sw-leg-aside">{aside}</span>}
      </div>
      <div className="sw-leg-main">
        <div className="sw-leg-value">{children}</div>
        {control !== undefined && control !== null && control !== false && <div className="sw-leg-control">{control}</div>}
      </div>
      {(sub || subAside) && (
        <div className="sw-leg-sub">
          <span className="sw-leg-subtext">{sub}</span>
          {subAside !== undefined && subAside !== null && subAside !== false && <span className="sw-leg-subaside">{subAside}</span>}
        </div>
      )}
    </div>
  );
}

/**
 * The seam between two legs. A DEX puts a button here that reverses the direction; there is no
 * direction to reverse in either of these flows, so it is a mark and not a control — and it is
 * hidden from assistive tech, which reads the two labels instead and needs no arrow between them.
 */
export function SwapSeam() {
  return (
    <div className="sw-seam" aria-hidden="true">
      <span className="sw-seam-node">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" focusable="false">
          <path d="M7 2.4v9.2M3.3 8l3.7 3.7L10.7 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
    </div>
  );
}

/**
 * The detail strip: what the panel costs and where it goes, under a hairline.
 *
 * It is a `<details>` so a phone can put it away — at ≤640 px the summary is a tappable row; above
 * that the summary is not drawn at all and the rows are simply there. It opens by DEFAULT on every
 * width, including the phone: what is in here is the fee, the route and the wait, and a money page
 * that hides its own fee behind a tap has hidden the thing the user came to check.
 */
export function SwapDetails({
  summary,
  children,
  testId,
  busy,
  detailsRef,
}: {
  summary: ReactNode;
  children: ReactNode;
  testId?: string;
  busy?: string;
  detailsRef?: Ref<HTMLDetailsElement>;
}) {
  return (
    <details ref={detailsRef} className="sw-details" open data-testid={testId} data-busy={busy}>
      <summary className="sw-details-summary">
        <span>{summary}</span>
        <svg className="sw-details-chevron" width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true" focusable="false">
          <path d="M2.6 4.4 6 7.8l3.4-3.4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </summary>
      <div className="sw-details-body">{children}</div>
    </details>
  );
}

/** One priced line in the strip: what it is on the left, the number on the right. */
export function SwapRow({
  label,
  children,
  testId,
  valueClass,
  title,
}: {
  label: ReactNode;
  children: ReactNode;
  /** goes on the VALUE, so an assertion about the number is about the number */
  testId?: string;
  valueClass?: string;
  title?: string;
}) {
  return (
    <div className="sw-row">
      <span className="sw-row-label" title={title}>
        {label}
      </span>
      <span className={`sw-row-value${valueClass ? ` ${valueClass}` : ''}`} data-testid={testId}>
        {children}
      </span>
    </div>
  );
}

/** A sentence that runs the width of the strip — the API's own words, or ours about the route. */
export function SwapNote({
  children,
  testId,
  tone,
  attrs,
}: {
  children: ReactNode;
  testId?: string;
  /** `good` for a route that costs nothing extra, `warn` for one the user should read twice */
  tone?: 'good' | 'warn' | 'plain';
  /** extra data-* the suite reads (T46's `data-count`/`data-sent` on the approvals promise) */
  attrs?: Record<string, string | number | undefined>;
}) {
  return (
    <p className={`sw-note${tone && tone !== 'plain' ? ` sw-note-${tone}` : ''}`} data-testid={testId} {...attrs}>
      {children}
    </p>
  );
}

/** The bottom of the panel: the button, and the line that says what is happening under it. */
export function SwapAction({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return (
    <div className="sw-action" style={style}>
      {children}
    </div>
  );
}
