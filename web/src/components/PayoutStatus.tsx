// The payout pill and the muted lines under it, in one component both pages use.
//
// It replaces `RequestStatusCell` (components/Status.tsx) for payout ORDERS on 2026-09-10: same
// pills, same hints, same test ids — plus the T40 vocabulary, which is the whole point. The
// deposit half of Status.tsx is untouched and still owns deposits.
//
// ⛔ THE WORD "FAILED" IS NOT HERE. Status.tsx maps `failed → Failed`, which is what put two red
// Failed rows on the admin's own Balance page for orders that had been refunded in full. What a
// status means to a user is decided once, in lib/payouts.ts; this file only draws it.
//
// ⛔ AND IT SETS NO WIDTHS (T53, 2026-09-10). These lines used to carry `minWidth: 150` inline,
// which is a column width written in the wrong place: it made the status and "Arrives" cells the
// widest things in a seven-column table and pushed the table 74 px past the card that holds it
// (T44 measured 1110 px of table in a 1036 px box, with the arrival estimate behind a sideways
// scroll). A column's width belongs to the table — `.data.orders` in styles.css — and a sentence
// under a pill belongs inside whatever width that column is.
import { holdReasonText } from './Status';
import { fmtAgo, fmtDuration } from '../lib/format';
import { BEAM_CONFIRMATIONS, etaOf, nextTryAt, payoutView, shortTime, type PayoutRow, type PayoutSurface } from '../lib/payouts';

export function PayoutStatusPill({ row, refunded = false, surface }: { row: PayoutRow; refunded?: boolean; surface?: PayoutSurface }) {
  const v = payoutView(row, refunded, surface);
  return (
    <span className={`pill ${v.cls}`} data-status={v.status} data-shown={v.key}>
      {v.label}
    </span>
  );
}

/**
 * The pill and everything that has to be said under it: why it is parked, when the next attempt
 * is, and how far the bridge has counted. One pill, never two — a reason is a line, not a badge.
 */
export function PayoutStatusCell({
  row,
  refunded = false,
  // `history` is the timeline's telling of the same row — one word changes (lib/payouts.ts owns
  // both), and the caller says which surface it is rather than the component guessing.
  surface,
}: {
  row: PayoutRow;
  refunded?: boolean;
  surface?: PayoutSurface;
}) {
  const v = payoutView(row, refunded, surface);
  return (
    <>
      <span className={`pill ${v.cls}`} data-status={v.status} data-shown={v.key}>
        {v.label}
      </span>
      {v.confirmations !== null && (
        <div className="tiny muted status-sub" data-hint="confirmations">
          {v.confirmations}/{BEAM_CONFIRMATIONS} Beam confirmations
        </div>
      )}
      {v.reason && (
        <div className="tiny muted status-sub" data-hint="hold">
          {v.reason}
        </div>
      )}
      {v.nextTry !== null && (
        <div className="tiny muted status-sub" data-hint="next-try">
          next try {shortTime(v.nextTry)}
        </div>
      )}
    </>
  );
}

/**
 * When the money is expected to be in the wallet — **the API's `eta_at`, relative and absolute**,
 * with its own `eta_note` under it (admin 2026-09-10: "You should show all statuses there and
 * estimated time of arrival of his asset").
 *
 * ⛔ A row whose API published no ETA gets a dash. The page knows `deliver_at`, `release_at` and
 * that the bridge takes about 66 minutes — and adding them up here would be a SECOND ETA writer,
 * disagreeing with the one the order machine uses the moment a retry moves the order (law 9).
 *
 * ⛔ AND THE NEXT ATTEMPT IS A DURATION, NEVER A TIMESTAMP. `next_try_at` is the API's ISO-8601
 * instant and it is rendered as "retries in ~12 min": "next try at 1788950000" is what a unix
 * integer looks like when it reaches a person, and nobody can read it. Absent — a held row, a
 * row that is not delayed at all, a build that publishes only `next_attempt_at` — nothing is
 * said, because a retry this page invented would be a promise the machine never made.
 *
 * ⛔ AND THE NOTE IS NOT PRINTED TWICE (T52, 2026-09-10). Since the API split every hold into a
 * user sentence and an operator one, `hold_reason` IS the plain sentence and `eta_note` is that
 * same sentence — one fact, published under two names because two different surfaces read it. A
 * row would otherwise carry "Waiting for treasury funds — expected by Sat 12 Sep, 23:21Z at the
 * latest" under its pill and again under its arrival time, which reads as two systems talking.
 */
export function PayoutEta({ row }: { row: PayoutRow }) {
  const eta = etaOf(row);
  const retry = nextTryAt(row);
  const inS = retry ? (retry.getTime() - Date.now()) / 1000 : null;
  const alreadySaid = eta?.note && eta.note === holdReasonText(row.hold_reason);
  if (!eta) return <span className="muted">—</span>;
  return (
    <>
      <span className="nowrap strong" data-eta="relative">
        {fmtAgo(eta.at)}
      </span>
      <div className="tiny muted nowrap" data-eta="absolute">
        {shortTime(eta.at)}
      </div>
      {inS !== null && (
        <div className="tiny muted nowrap" data-eta="next-try">
          {inS > 0 ? `retries in ~${fmtDuration(inS)}` : 'retrying now'}
        </div>
      )}
      {eta.note && !alreadySaid && (
        <div className="tiny muted status-sub" data-eta="note">
          {eta.note}
        </div>
      )}
    </>
  );
}
