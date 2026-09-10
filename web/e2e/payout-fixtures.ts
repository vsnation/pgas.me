// The payout rows T40 is about, as an OPT-IN world a test asks for.
//
// Why they are not in `MockApi`'s default fixtures: those are shared with every other spec in this
// suite (one of them counts the timeline's rows), and a fixture set that changes under a test that
// did not ask for it is how a suite starts failing for reasons nobody changed. A test that is
// about delayed, held, legacy-failed and refunded orders says so, in one line.
//
// Every row here is a shape the LIVE system can produce today:
//   · `delayed` / `held` — the 2026-09-10 statuses: money still reserved, a reason in plain words,
//     a next attempt, and a fresh ETA (API_CONTRACT.md § Withdrawals).
//   · `failed` — the terminal status the processor no longer writes but old rows still carry. One
//     of them has a next attempt (the API says it is being retried); two are the orders that were
//     refunded on 2026-09-10 10:31Z, and their evidence is a `cancel` entry in the ledger, which
//     is the only thing that may turn a row into "Refunded".
import type { MockApi } from './mocks';

/** unix seconds, `d` from now. */
const AT = (d: number) => Math.floor(Date.now() / 1000) + d;

export const DELAYED_REASON = 'the bridge fee moved above what this order funded — waiting for the gas price to come down';
export const HELD_REASON = 'someone at Pgas.me is looking at this one — your money is still reserved';
// ── T52 block ────────────────────────────────────────────────────────────────────────────────
// The float-short wait as the API writes it since 2026-09-10: ONE sentence, written for the
// person reading it, on `hold_reason` AND as the `eta_note` — the same fact published under two
// names because two surfaces read it. The numbers ("the wallet can spend 0.00775651 regular / 0
// shielded … 0.01652864 is maturing") live on `hold_detail`, which `public_request` strips.
export const FLOAT_UNLOCK_WORDS = 'Sat 12 Sep, 23:21Z';
export const FLOAT_REASON = `Waiting for treasury funds — expected by ${FLOAT_UNLOCK_WORDS} at the latest (earlier if deposits come in)`;

/** The wallets these fixtures pay, checksummed. */
export const W1 = '0x1111111111111111111111111111111111111111';
export const W2 = '0x2222222222222222222222222222222222222222';
export const W3 = '0x3333333333333333333333333333333333333333';

/**
 * Replace the mock's payout rows with one per status the user can see, and give the ledger the
 * refund entries that prove two of them are settled. Returns the ids, so a test names them rather
 * than counting rows.
 */
export function installPayoutStatuses(api: MockApi) {
  api.requests = [
    {
      // an ordinary order, and the one T35 case that must never be silent: it delivers less than
      // it asked for, because Available could not pay the fees on top
      _id: 'p-from-amount',
      asset: 'ETH',
      mode: 'direct',
      W: W1,
      // ⛔ THE ROW'S SHAPE, NOT THE FORM'S (T35b). `_write_items` stores the DELIVERY in
      // `amount_groth` (the release spends that field) and what the user typed in
      // `requested_groth`. This fixture used to carry the typed figure in `amount_groth`, which
      // made "of {amount_groth} asked" look right on screen while the page was reading the wrong
      // field — against a real row it printed the delivered number twice.
      amount_groth: 4881372,
      requested_groth: 5000000,
      delivered_groth: 4881372,
      debited_groth: 5000000,
      fee_mode: 'from_amount',
      fee_groth: 97628,
      bridge_fee_groth: 21000,
      deliver_at: AT(2 * 3600),
      release_at: AT(2 * 3600 - 3960),
      status: 'scheduled',
      created_at: AT(-600),
      eta_at: AT(2 * 3600),
      eta_note: 'your delivery time — it goes to the bridge about an hour before',
    },
    {
      _id: 'p-delayed',
      asset: 'ETH',
      mode: 'direct',
      W: W2,
      amount_groth: 2000000,
      delivered_groth: 2000000,
      debited_groth: 2060000,
      fee_mode: 'on_top',
      fee_groth: 40000,
      bridge_fee_groth: 20000,
      deliver_at: AT(-1800),
      release_at: AT(-5400),
      status: 'delayed',
      hold_reason: DELAYED_REASON,
      next_attempt_at: AT(300),
      // the same instant as an ISO-8601 string: the field a PAGE reads (T35b L5). The unix one
      // stays because it is what the machine writes and the API publishes both.
      next_try_at: new Date((AT(300) as number) * 1000).toISOString(),
      created_at: AT(-7200),
      eta_at: AT(300 + 3960),
      eta_note: 'delayed: the crossing costs more than this order funded — next try in 5 minutes, then the bridge takes about an hour',
      eta_tail_s: 18 * 3600,
    },
    {
      _id: 'p-held',
      asset: 'ETH',
      mode: 'direct',
      W: W3,
      amount_groth: 1500000,
      delivered_groth: 1500000,
      debited_groth: 1550000,
      fee_mode: 'on_top',
      fee_groth: 30000,
      bridge_fee_groth: 20000,
      deliver_at: AT(-3 * 3600),
      release_at: AT(-4 * 3600),
      status: 'held',
      hold_reason: HELD_REASON,
      created_at: AT(-5 * 3600),
      eta_at: AT(2 * 3600),
      eta_note: 'delayed: a person is checking this one — the money stays reserved until it goes',
    },
    {
      // a row from before 2026-09-10 that the API says it is still retrying
      _id: 'p-legacy-retry',
      asset: 'ETH',
      mode: 'direct',
      W: W2,
      amount_groth: 1200000,
      fee_groth: 24000,
      bridge_fee_groth: 20000,
      deliver_at: AT(-6 * 3600),
      release_at: AT(-7 * 3600),
      status: 'failed',
      next_attempt_at: AT(900),
      created_at: AT(-8 * 3600),
      eta_at: AT(900 + 3960),
      eta_note: 'delayed: retrying — next try in 15 minutes, then the bridge takes about an hour',
    },
    {
      // the two orders of 2026-09-10 10:31Z: they failed under the old rule and were refunded
      _id: 'p-legacy-refunded-1',
      asset: 'ETH',
      mode: 'direct',
      W: W1,
      amount_groth: 1000000,
      fee_groth: 20000,
      bridge_fee_groth: 14733,
      deliver_at: AT(-20 * 3600),
      release_at: AT(-21 * 3600),
      status: 'failed',
      created_at: AT(-22 * 3600),
    },
    {
      _id: 'p-legacy-refunded-2',
      asset: 'ETH',
      mode: 'direct',
      W: W3,
      amount_groth: 1000000,
      fee_groth: 20000,
      bridge_fee_groth: 14733,
      deliver_at: AT(-20 * 3600),
      release_at: AT(-21 * 3600),
      status: 'failed',
      created_at: AT(-22 * 3600),
    },
    {
      _id: 'p-bridging',
      asset: 'ETH',
      mode: 'direct',
      W: W2,
      amount_groth: 3000000,
      delivered_groth: 3000000,
      debited_groth: 3080000,
      fee_mode: 'on_top',
      fee_groth: 60000,
      bridge_fee_groth: 20000,
      deliver_at: AT(1200),
      release_at: AT(-2400),
      status: 'bridging',
      beam_txid: 'c3d4e5f6a1b2c3d4e5f6a1b2',
      beam_confirmations: 43,
      created_at: AT(-4800),
      eta_at: AT(1500),
      eta_note: "bridge ≈ 1 h, up to 18 h in the relayer's tail",
      eta_tail_s: 18 * 3600,
    },
    {
      _id: 'p-sent',
      asset: 'ETH',
      mode: 'direct',
      W: W1,
      amount_groth: 800000,
      delivered_groth: 800000,
      debited_groth: 836000,
      fee_mode: 'on_top',
      fee_groth: 16000,
      bridge_fee_groth: 20000,
      // ── T45 block: the crossing settled for less than it was quoted ──────────────────────
      // The bridge fee is an ESTIMATE (a live gas read × the headroom the wait needs) and what
      // it does not spend is credited back to Available at settlement — the shape of the two
      // live orders of 2026-09-10 that funded 14,733 groth against a 12,778-groth crossing.
      // Here: funded 20,000, the send carried 18,045, and 1,955 came back.
      relayer_fee_groth: 18045,
      bridge_fee_refund_groth: 1955,
      // ────────────────────────────────────────────────────────────────────────────────────
      deliver_at: AT(-9 * 3600),
      release_at: AT(-10 * 3600),
      status: 'sent',
      eth_tx: '0x' + '77'.repeat(32),
      eth_block: 21000999,
      beam_confirmations: 61,
      created_at: AT(-11 * 3600),
    },
    {
      _id: 'p-cancelled',
      asset: 'ETH',
      mode: 'direct',
      W: W3,
      amount_groth: 600000,
      fee_groth: 12000,
      bridge_fee_groth: 20000,
      deliver_at: AT(-30 * 3600),
      release_at: AT(-31 * 3600),
      status: 'cancelled',
      created_at: AT(-32 * 3600),
    },
  ];
  // the ledger's own evidence: the two legacy rows were paid back in full, the cancelled one too
  api.history = [
    { kind: 'cancel', groth: 1034733, d_avail: 1034733, d_sched: -1034733, d_sent: 0, ref: 'p-legacy-refunded-1', at: AT(-19 * 3600) },
    { kind: 'cancel', groth: 1034733, d_avail: 1034733, d_sched: -1034733, d_sent: 0, ref: 'p-legacy-refunded-2', at: AT(-19 * 3600) },
    { kind: 'cancel', groth: 632000, d_avail: 632000, d_sched: -632000, d_sent: 0, ref: 'p-cancelled', at: AT(-30 * 3600) },
    // T45 — the settled crossing's refund, as the ledger records it: back into Available, its own
    // kind, carrying what was funded and what the crossing actually paid.
    {
      kind: 'bridge_fee_refund',
      groth: 1955,
      d_avail: 1955,
      d_sched: 0,
      d_sent: 0,
      ref: 'p-sent',
      at: AT(-9 * 3600),
    },
    ...api.history,
  ];
  return {
    fromAmount: 'p-from-amount',
    delayed: 'p-delayed',
    held: 'p-held',
    legacyRetry: 'p-legacy-retry',
    refunded: ['p-legacy-refunded-1', 'p-legacy-refunded-2'],
    bridging: 'p-bridging',
    sent: 'p-sent',
    cancelled: 'p-cancelled',
  };
}

/**
 * ⛔ ONE MORE WORLD, AND IT IS OPT-IN TOO (T52). The float-short wait is added by
 * `installFloatWait`, not by `installPayoutStatuses`: three tests in this suite count the rows
 * on the page or locate "the delayed row", and a fixture that changes under a test which did not
 * ask for it is how a suite starts failing for reasons nobody changed (this file's own rule).
 */
export function installFloatWait(api: MockApi) {
  api.requests = [
    {
      // ── T52 — the wait the admin could not read, in the words that replaced it ─────────────
      // Its ETA is the max-privacy UNLOCK, not the next rung of the retry ladder: the lock has
      // an end and that end is the honest arrival time ("never a time in the past", T40b).
      _id: 'p-float',
      asset: 'ETH',
      mode: 'direct',
      W: W3,
      amount_groth: 1000000,
      delivered_groth: 1000000,
      debited_groth: 1027925,
      fee_mode: 'on_top',
      fee_groth: 20000,
      bridge_fee_groth: 7925,
      deliver_at: AT(-600),
      release_at: AT(-4560),
      status: 'delayed',
      hold_code: 'payout-wallet',
      hold_reason: FLOAT_REASON,
      ready_at: AT(2 * 3600),
      ready_reason: 'treasury_float',
      next_attempt_at: AT(120),
      next_try_at: new Date((AT(120) as number) * 1000).toISOString(),
      created_at: AT(-7200),
      eta_at: AT(2 * 3600),
      eta_note: FLOAT_REASON,
      eta_tail_s: 18 * 3600,
    },
    ...api.requests,
  ];
  return 'p-float';
}
