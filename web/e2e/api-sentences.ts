// The API's OWN refusal sentences, in one file, so the mock refuses the way production refuses.
//
// Why this exists (T30c skeptic, 2026-09-10 11:05Z): "web/e2e/mocks.ts must use the API's OWN
// refusal sentences (copy them from routers/withdrawals.py at build time or import a shared
// fixture) so screenshots/tests show production wording". A mock that invents friendlier prose
// tests a product that does not exist — the screenshots taken from it are of a screen nobody will
// ever see, and every assertion about "the API's own words, verbatim" is really an assertion about
// this repo's e2e folder.
//
// ⛔ THESE ARE COPIES, AND A COPY GOES STALE. Each one names the function in
// `api/pgasme/routers/withdrawals.py` (or `workers.py`) it was taken from, so a change on that
// side has one place to land. Re-read them whenever that file's refusals change — T35-api and
// T40-api are both rewriting parts of it today.
//
// The one number-formatting difference worth knowing: Python writes small floats with `:g`
// (`1e-08`) and JavaScript writes `1e-8`. It shows up only for amounts under a millionth of an
// asset unit — every fixture in this suite is far above that — and the fix, if it ever matters,
// is a formatter here rather than a second sentence.

/**
 * T45 item 5 (2026-09-10 15:35Z) — `withdrawals.fmt_units`, the ONE formatter every user-facing
 * sentence the API emits now goes through: 8 decimals, trailing zeros trimmed, the asset's symbol
 * after it ("0.02900846 ETH").
 *
 * The admin hit the old wording himself — *"insufficient ETH balance: this batch needs 2900846
 * groth … top up shortfall_groth 66772 groth"* — and said: *"Why in groth? People don't understand
 * nothing in it. You should have human readable errors in ETH and show what actually available to
 * user."* `groth` is an internal unit and `shortfall_groth` is a FIELD NAME; neither belongs in a
 * line somebody has to act on. Machine fields (`*_groth`, `X-Shortfall-Groth`) are unchanged.
 *
 * Python writes `f"{g/1e8:.8f}".rstrip("0").rstrip(".")`; this is that, in JS.
 */
export const fmtUnits = (groth: number, asset = 'ETH') => `${(groth / 1e8).toFixed(8).replace(/0+$/, '').replace(/\.$/, '')} ${asset}`;

/** `address_problem` — not an address at all. Python's `!r` puts the value in single quotes. */
export const notAnAddress = (raw: string) => `'${raw.slice(0, 64)}' is not an EVM address`;

/** `address_problem` — a mixed-case string whose EIP-55 checksum does not match. */
export const badChecksum = (raw: string) =>
  `${raw.slice(0, 64)} has a bad EIP-55 checksum — check the address you pasted (a lowercase ` +
  `address is accepted as typed; a mixed-case one must checksum)`;

/** `price_item` — the delivery time is not a time, or is beyond `PGAS_MAX_WINDOW_S`. */
export const badDeliverAt = (days: number) =>
  `deliver_at must be a unix time in the next ${days} days — a delivery more than ` +
  `${days} days away (or a time that is not a time) is refused; schedule it closer to ` +
  `the time you want the money`;

/** `price_item` — under `min_amount_groth`, the technical floor. */
export const belowMinimum = (minGroth: number, asset: string) => `each payout must be at least ${fmtUnits(minGroth, asset)}`;

/** `price_item` — off the asset's grid (1 groth for every asset this deployment carries). */
export const offGrid = (asset: string, grid: number) =>
  `${asset} amounts must be a whole multiple of ${fmtUnits(grid, asset)} — ` + `anything finer cannot be paid out on the Ethereum side`;

/** `price_item` — the instant path only. */
export const badDenomination = (denoms: number[], asset = 'ETH') =>
  `instant payouts must be a multiple of a denomination (${denoms.map((d) => fmtUnits(d, asset)).join(', ')})`;

/**
 * T35 — the amount cannot even pay the crossing, so there is nothing to take the fees out of.
 * Copied from `price_item` (BRIDGE_FEE_CODE branch) after T35-api landed it, 2026-09-10.
 */
export const tooSmallForBridge = (amountGroth: number, bridgeGroth: number, leastGroth: number, asset = 'ETH') =>
  `${fmtUnits(amountGroth, asset)} is too small to pay the bridge fee from the ` +
  `amount — the crossing alone costs ${fmtUnits(bridgeGroth, asset)} and there ` +
  `must be something left to deliver. Either hold enough balance for the fees ` +
  `to be charged on top of the amount, or ask for at least ` +
  `${fmtUnits(leastGroth, asset)}`;

/**
 * T35b — the amount CAN pay its crossing out of itself and would have almost nothing left. A
 * delivery that is mostly fees is not a withdrawal anyone asked for, and the assumption is
 * stated in the refusal (`price_item`, FEES_DOMINATE_CODE branch, 2026-09-10).
 */
export const feesDominate = (amountGroth: number, needGroth: number, leastGroth: number, asset = 'ETH') =>
  `fees would take more than half of ${fmtUnits(amountGroth, asset)} — hold at ` +
  `least ${fmtUnits(needGroth, asset)} of balance so fees are charged on top, or ` +
  `ask for at least ${fmtUnits(leastGroth, asset)}`;

/**
 * T35 — this row cannot be covered by what the rows above it left. `price_item` marks the ITEM
 * with the batch code and `_refuse_items` leaves it to the batch rule (409 + shortfall).
 */
export const rowShortfall = (needGroth: number, leftGroth: number, asset = 'ETH') =>
  `this row needs ${fmtUnits(needGroth, asset)} and only ${fmtUnits(leftGroth, asset)} of ` +
  `your balance is left after the rows above it — short by ` +
  `${fmtUnits(needGroth - leftGroth, asset)}`;

/** `_refuse_items` — the 422 that names every item it refused. */
export const itemsRefused = (bad: { n: number; problem: string }[], total: number) =>
  `${bad.length} of ${total} item(s) cannot be scheduled (${bad.map((b) => `item ${b.n + 1}: ${b.problem}`).join('; ')}) — ` +
  `nothing was scheduled and nothing was debited`;

/**
 * `batch_problem` — Σ total > Available. THE MOCK'S DEFAULT SINCE T35b (2026-09-10): the sentence
 * the user reads in a test is now the sentence production answers with, word for word.
 *
 * ⛔ `need` AND `shortfall` ARE NOT THE SAME SUBTRACTION, which is why the wording changed.
 * `need` is what the batch debits as typed; `shortfall` is `top_up_needed` — the FIXED POINT, the
 * top-up after which every row's fees ride on top. The old sentence invited the reader to
 * subtract one from the other, and that number was the one that got them refused twice.
 *
 * `feeBps` is no longer in the sentence; the parameter is kept so every caller (and the
 * `MockApi.batchSentence` hook) keeps one signature.
 */
export const batchShort = (asset: string, need: number, available: number, shortfall: number, _feeBps?: number) =>
  `Not enough ${asset}: this batch needs ${fmtUnits(need, asset)} and ` +
  `${fmtUnits(available, asset)} is available. Top up ${fmtUnits(shortfall, asset)} and ` +
  `every wallet receives the full amount it asked for (fees are charged on top once your ` +
  `balance covers them).`;

/** `refuse_contracts` — the destination carries code, so a delivery would strand there. */
export const destinationIsContract = (address: string) =>
  `${address} is a contract, not a wallet — the bridge delivery would be stranded there. ` + `Use an address you control the keys to`;

/**
 * `UNREADABLE_DEST` — not one Ethereum endpoint would read the code at that address, so nothing
 * is decided on a guess.
 *
 * ⛔ IT TOOK A PARAMETER UNTIL T52 (2026-09-10 16:47Z, a user hit it live): the sentence carried
 * the endpoint that refused — *"(https://eth.drpc.org could not read the code at that address)"* —
 * which is a provider URL and an internal read, in a line somebody has to act on and cannot. The
 * endpoint names go to the API's log now; the person gets one sentence.
 */
export const DESTINATION_UNREADABLE =
  'We could not check the destination address right now — nothing was scheduled; try again in a moment.';

/**
 * T52 (`withdrawals.treasury_float_problem`) — the treasury cannot move this much TODAY. The
 * admin's rule, 2026-09-10 15:45Z: *"Next time when user wants to withdraw just tell him he
 * can't"*, after three of his own orders were accepted against a wallet that could spend
 * 0.00775651 ETH and was holding 0.01652864 more inside a max-privacy lock until 12 Sep 23:21Z.
 *
 * `when` is the API's own rendering of the unlock ("Sat 12 Sep, 23:21Z"): the date is composed
 * on that side (`payouts.fmt_when`), so this copy never re-derives one and cannot drift by a
 * timezone. Empty `when` is "nothing is maturing", which is a different sentence and not a
 * blank in this one.
 */
export const treasuryShort = (deliverableGroth: number, when: string, asset = 'ETH') =>
  deliverableGroth <= 0
    ? when
      ? `No withdrawal can be delivered right now. The treasury's funds unlock on ${when} — try again then.`
      : 'No withdrawal can be delivered right now — please try again in a little while.'
    : when
      ? `Withdrawals of up to ${fmtUnits(deliverableGroth, asset)} can be delivered right now. ` +
        `The rest of the treasury's funds unlock on ${when} — ask for a smaller amount now, or come back then.`
      : `Withdrawals of up to ${fmtUnits(deliverableGroth, asset)} can be delivered right now — ` +
        `ask for a smaller amount now, or try again later.`;

/** `workers.PAUSED_REASON` — the kill switch, in the words the user gets. */
export const PAUSED =
  'Pgas.me is paused by the operator (the stop file is set): nothing that moves money is ' +
  'accepted right now. Balances and estimates are unaffected — try again shortly.';

/** `cancel` — the row exists but is past the point where cancelling means anything. */
export const cancelTooLate = (status: string) => `request is ${status} — only a scheduled request can be cancelled`;

/** `cancel` — no such row for this account. */
export const UNKNOWN_REQUEST = 'unknown request';
