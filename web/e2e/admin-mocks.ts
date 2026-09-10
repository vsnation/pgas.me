/**
 * A test double for the operator API (`/api/admin/*`, T38; enriched for the T49 redesign) — a
 * separate module from `mocks.ts`, which other hands are editing.
 *
 * It models three things about `/admin` that matter to the console:
 *
 *   • a wrong key is a **404**, never a hint, and the fifth failure is a **429** (one page, not five);
 *   • the payload SHAPES are the ones `api/pgasme/routers/admin.py` actually assembles — the
 *     `{rows, total, limit, offset, has_more}` window, `{value, error}` around every treasury
 *     figure, the deposit's two status fields, the payout's derived `hold` — because a console
 *     designed against a friendlier fixture than the API is a console that looks right and reads
 *     wrong on the box (the "mock kinder than the API" defect, twice already);
 *   • the fixtures are a BUSY morning, not a clean one: a held payout, a delayed one, an open
 *     unattributed lock, coins below target, a stale token list, a deposit stuck confirming. An
 *     attention list is only proven by things that need attention.
 *
 * ⛔ EVERY ADDRESS AND ID IN THIS FILE IS OBVIOUSLY FAKE, AND HAS TO STAY THAT WAY. `publish.sh`
 * mirrors `web/` including `e2e/`, and the screenshots taken from these fixtures are published.
 */
import type { Page, Route } from '@playwright/test';

export const ADMIN_KEY = 'gg1Yb0Xq4tR7sV2nJ8kP5wZ3cM6dF9hA1bC4eG7iK0m';
export const WRONG_KEY = 'not-the-key-at-all';

/** A fixed instant so every relative age in the fixtures — and in every screenshot — is the same
 *  one. The specs pin the browser's clock to it with `page.clock.setFixedTime`. */
export const NOW_S = Math.floor(Date.UTC(2026, 8, 10, 15, 14, 21) / 1000);
const ago = (s: number) => NOW_S - s;

const ADDR_A = '0xA11CE' + '0'.repeat(34) + '1'; // "alice"
const ADDR_B = '0xB0B' + '0'.repeat(36) + '2'; // "bob"
const ADDR_W = '0xDEAD' + '0'.repeat(32) + 'BEEF';
const ADDR_D = '0xD15' + '0'.repeat(36) + '7'; // the instant distributor
const TX_SRC = '0x' + 'a1'.repeat(32);
const TX_PIPE = '0x' + 'b2'.repeat(32);
const TX_OUT = '0x' + 'c3'.repeat(32);
const TX_ETH = '0x' + 'd7'.repeat(32);
/** Beam kernel ids are 64 hex with no `0x`. */
const BEAM_CLAIM = 'd4'.repeat(32);
const BEAM_SEND = 'e5'.repeat(32);

/**
 * ⛔ AN OBVIOUSLY FAKE BEAM ADDRESS, AND IT HAS TO STAY ONE.
 *
 * This file once carried the REAL production treasury address — the one the fee gates read and the
 * one BEAM is sent to — because it was pasted in to make the fixture look right. `publish.sh`
 * mirrors `web/` including `e2e/`, so it was one publish away from the public repo, and the
 * treasury tab's screenshots showed it in full. Beam addresses are 66 hex characters and every one
 * of them is a public receiving address, so this is not a secret in the cryptographic sense — it is
 * the thing that ties this repository to the wallet holding the float, which is exactly what §9
 * privacy says not to publish. `publish.sh` now refuses the real string outright.
 *
 * `deadbeef…01` is 66 hex characters, so it renders exactly like the real thing, and nobody will
 * ever mistake it for one.
 */
const TREASURY_ADDR = 'deadbeef' + '00'.repeat(28) + '01';
const MP_1 = 'facade01' + '00'.repeat(28) + '11';
const MP_2 = 'facade02' + '00'.repeat(28) + '22';
const MP_3 = 'facade03' + '00'.repeat(28) + '33';

// ─────────────────────────────────────────────────────────────────────────────── /overview

/** `routers/stats.health()` — the same body `/v1/health` serves, which is why the panel and the
 *  watchdog can never disagree about the posture. */
const HEALTH = {
  ok: true,
  version: '0.4.2',
  env: 'prod',
  mongo: true,
  secrets_ok: true,
  dev_endpoints: false,
  indexes_ok: true,
  index_errors: [],
  beampay_webhook: true,
  ingress_armed: true,
  ingress: { uniswap: false, xchain: true, direct: true, default_route: 'xchain' },
  // stale on purpose: the refresher writes these every 6 h and one chain kept its old file
  tokens: { updated_at: ago(8 * 3600), chains: 17, age_s: 8 * 3600, failed: [56] },
  ingress_assets: { ETH: true, DAI: true, WBTC: true },
  ingress_near: false,
  payout_direct: true,
  payout_instant: false,
  distributor: { configured: false, healthy: false },
  crossings: { orders: 1, oldest_age_s: 640 },
  // below target on purpose: Beam locks a whole UTXO per pending transaction
  coins: { ETH: { have: 1, target: 12 }, DAI: { have: 0, target: 12 }, WBTC: { have: 12, target: 12 }, BEAM: { have: 2, target: 20 } },
  gas: { samples: 412, samples_24h: 48, newest_age_s: 220, window_s: 86_400, ttl_s: 172_800 },
  workers: true,
  paused: false,
};

export const OVERVIEW = {
  at: NOW_S,
  version: '0.4.2',
  env: 'prod',
  health: HEALTH,
  flags: {
    ingress_armed: true,
    ingress_ready: true,
    ingress_assets: { ETH: true, DAI: true, WBTC: true },
    claim_enabled: true,
    shield_enabled: false,
    payout_direct_enabled: true,
    payout_instant_enabled: false,
    payout_spend_unshielded: true,
    workers_enabled: true,
    dev_endpoints: false,
    telegram_live: true,
    beampay_webhook: true,
  },
  kill_switch: { file: '/etc/pgasme.stop', engaged: false },
  distributor: { configured: false, address: '', float_wei: '0' },
  crossings: { orders: 1, oldest_age_s: 640, queued_groth: 199_990 },
  workers: {
    enabled: true,
    paused: false,
    last_pass: {
      deposit_watcher: {
        at: ago(23),
        source: 'scanner_state (the newest pipe checkpoint the lock scan wrote)',
        detail: { pipe: '8872509d…847369', last_block: 25_942_711 },
      },
      payout_processor: {
        at: ago(41),
        source: 'leases/payout_processor (renewed by the pass that holds it)',
        detail: { owner: 'pgasme-api', released_at: null },
      },
      stats_refresher: { at: ago(58), source: 'stats/pool (the explorer reading the refresher stores)', detail: { height: 4_031_015 } },
      monitor: { at: ago(96), source: 'events.notified_at (the newest event the monitor drained)', detail: { kind: 'payout_sent' } },
    },
  },
  watchdog: {
    log: '/var/log/pgasme/watch.log',
    line: '2026-09-10 15:13:49Z kill switch OK /etc/pgasme.stop absent',
    at: ago(32),
    why: null,
  },
  counts: {
    deposits: { submitted: 0, order_seen: 0, locked: 1, confirming: 1, credited: 3, fallback_pending: 0, failed: 1, expired: 0 },
    deposits_treasury: { claiming: 1, claimed: 1, shielded: 1 },
    payouts: { scheduled: 1, delayed: 1, held: 1, bridging: 1, sent: 1 },
    unattributed_locks: { open: 2, claimed: 1 },
    accounts: 16,
    beampay_events: 3,
    events_unsent: 2,
    held: { payouts: 1, deposits: 0 },
    in_flight: { payouts: 2, treasury: 1 },
  },
};

// ─────────────────────────────────────────────────────────────────────────────── /deposits

const DEPOSIT_STATUSES = ['submitted', 'order_seen', 'locked', 'confirming', 'credited', 'fallback_pending', 'failed', 'expired'];

export const DEPOSITS = [
  {
    _id: 'dep_01H8ZQ7',
    account_id: 'acc_9f2',
    address: ADDR_A,
    asset: 'ETH',
    mode: 'direct',
    status: 'credited',
    treasury: 'shielded',
    route_status: null,
    verified: true,
    quote_id: 'q_7712',
    src_tx_hash: TX_PIPE,
    src: { chain_id: 1, token: '0x' + '0'.repeat(40), amount: '2000000000000000' },
    eth: { tx: TX_PIPE, block: 25_942_113, msg_id: 137, value_units: '1999900000000000', relayer_fee_units: '100000000000' },
    value_groth: 199_990,
    claim_txid: BEAM_CLAIM,
    shield_txids: [BEAM_SEND, BEAM_CLAIM],
    beam_height: 4_030_880,
    receiver_key_index: 0,
    created_at: ago(7200),
    credited_at: ago(5600),
    updated_at: ago(5400),
    treasury_at: ago(5400),
    note: null,
    tx: {
      src: { chain_id: 1, hash: TX_PIPE },
      eth: { chain_id: 1, hash: TX_PIPE },
      beam: { claim_txid: BEAM_CLAIM, shield_txids: [BEAM_SEND] },
    },
  },
  {
    _id: 'dep_01H8ZR4',
    account_id: 'acc_9f2',
    address: ADDR_A,
    asset: 'ETH',
    mode: 'xchain',
    status: 'credited',
    treasury: 'claiming',
    route_status: 'Fulfilled',
    verified: true,
    quote_id: 'q_7719',
    src_tx_hash: TX_SRC,
    src: { chain_id: 42161, token: '0x' + '0'.repeat(40), amount: '12000000000000000' },
    eth: { tx: TX_ETH, block: 25_942_540, msg_id: 138, value_units: '11800000000000000', relayer_fee_units: '200000000000000' },
    value_groth: 1_180_000,
    claim_txid: null,
    shield_txids: [],
    beam_height: null,
    receiver_key_index: 1,
    created_at: ago(2400),
    credited_at: ago(300),
    updated_at: ago(300),
    treasury_at: ago(280),
    note: 'the claim is in flight',
    tx: { src: { chain_id: 42161, hash: TX_SRC }, eth: { chain_id: 1, hash: TX_ETH }, beam: { claim_txid: null, shield_txids: [] } },
  },
  {
    _id: 'dep_01H8ZS2',
    account_id: 'acc_3aa',
    address: ADDR_B,
    asset: 'ETH',
    mode: 'xchain',
    status: 'confirming',
    treasury: null,
    route_status: 'Fulfilled',
    verified: true,
    quote_id: 'q_7788',
    src_tx_hash: TX_SRC,
    src: { chain_id: 8453, token: '0x' + '0'.repeat(40), amount: '5000000000000000' },
    eth: { tx: TX_ETH, block: 25_942_690, msg_id: 141, confirmations: 4, confirmations_required: 12, value_units: '4900000000000000' },
    value_groth: 490_000,
    claim_txid: null,
    shield_txids: [],
    beam_height: null,
    created_at: ago(3000),
    updated_at: ago(2400),
    note: null,
    tx: { src: { chain_id: 8453, hash: TX_SRC }, eth: { chain_id: 1, hash: TX_ETH }, beam: { claim_txid: null, shield_txids: [] } },
  },
  {
    _id: 'dep_01H8ZS9',
    account_id: 'acc_3aa',
    address: ADDR_B,
    asset: 'DAI',
    mode: 'uniswap',
    status: 'failed',
    treasury: null,
    route_status: null,
    verified: false,
    quote_id: 'q_7801',
    src_tx_hash: null,
    src: { chain_id: 1, token: '0x6B175474E89094C44Da98b954EedeAC495271d0F', amount: '25000000000000000000' },
    eth: { tx: null, block: null, msg_id: null, value_units: null, relayer_fee_units: null },
    value_groth: 0,
    claim_txid: null,
    shield_txids: [],
    beam_height: null,
    created_at: ago(600),
    updated_at: ago(540),
    note: 'registration refused: to != pipe',
    tx: { src: { chain_id: 1, hash: null }, eth: { chain_id: 1, hash: null }, beam: { claim_txid: null, shield_txids: [] } },
  },
  {
    _id: 'dep_01H8ZT1',
    account_id: 'acc_71c',
    address: ADDR_W,
    asset: 'ETH',
    mode: 'direct',
    status: 'locked',
    treasury: null,
    route_status: null,
    verified: true,
    quote_id: 'q_7810',
    src_tx_hash: TX_PIPE,
    src: { chain_id: 1, token: '0x' + '0'.repeat(40), amount: '3000000000000000' },
    eth: { tx: TX_PIPE, block: 25_942_705, msg_id: 142, value_units: '2999900000000000' },
    value_groth: 299_990,
    claim_txid: null,
    shield_txids: [],
    beam_height: null,
    created_at: ago(180),
    updated_at: ago(150),
    note: null,
    tx: { src: { chain_id: 1, hash: TX_PIPE }, eth: { chain_id: 1, hash: TX_PIPE }, beam: { claim_txid: null, shield_txids: [] } },
  },
];

// ──────────────────────────────────────────────────────────────────────────────── /payouts

const PAYOUT_STATUSES = ['scheduled', 'delayed', 'releasing', 'bridging', 'delivering', 'sent', 'held', 'cancelled'];

const hold = (reason: string | null, at: number | null, count = 0) => ({
  reason,
  at,
  count,
  dark: false,
  paged_at: null,
  reminded_at: null,
});

export const PAYOUTS = [
  {
    _id: 'req_5501',
    account_id: 'acc_9f2',
    asset: 'ETH',
    mode: 'direct',
    status: 'sent',
    W: ADDR_W,
    amount_groth: 199_990,
    requested_groth: 199_990,
    delivered_groth: 195_000,
    fee_groth: 4_000,
    bridge_fee_groth: 14_665,
    bridge_fee_refund_groth: 2_100,
    fee_mode: 'on_top',
    deliver_at: ago(9000),
    release_at: ago(12_960),
    delivered_at: ago(8_100),
    status_at: ago(8_100),
    beam_txid: BEAM_SEND,
    msg_id: 92,
    eth_tx: TX_OUT,
    eth_block: 25_941_002,
    beam_height: 4_030_701,
    eta_at: null,
    created_at: ago(20_000),
    hold: hold(null, null),
  },
  {
    _id: 'req_5502',
    account_id: 'acc_9f2',
    asset: 'ETH',
    mode: 'direct',
    status: 'bridging',
    W: ADDR_B,
    amount_groth: 250_000,
    requested_groth: 250_000,
    delivered_groth: 250_000,
    fee_groth: 5_000,
    bridge_fee_groth: 14_665,
    bridge_fee_refund_groth: 0,
    fee_mode: 'on_top',
    deliver_at: ago(-3600),
    release_at: ago(640),
    status_at: ago(640),
    fund_called_at: ago(640),
    beam_txid: BEAM_CLAIM,
    msg_id: 93,
    eth_tx: null,
    eth_block: null,
    beam_height: 4_031_002,
    eta_at: ago(-2_800),
    created_at: ago(4_200),
    hold: hold(null, null),
  },
  {
    _id: 'req_5503',
    account_id: 'acc_3aa',
    asset: 'ETH',
    mode: 'direct',
    status: 'delayed',
    W: ADDR_A,
    amount_groth: 90_000,
    requested_groth: 90_000,
    delivered_groth: 68_400,
    fee_groth: 1_800,
    bridge_fee_groth: 19_800,
    bridge_fee_refund_groth: 0,
    fee_mode: 'from_amount',
    deliver_at: ago(-86_400),
    release_at: ago(-82_440),
    status_at: ago(420),
    next_try_at: new Date((NOW_S + 180) * 1000).toISOString(),
    next_attempt_at: NOW_S + 180,
    beam_txid: null,
    msg_id: null,
    eth_tx: null,
    eth_block: null,
    beam_height: null,
    eta_at: ago(-5_400),
    created_at: ago(900),
    hold: hold('the relayer wants 41,200 groth against a ceiling of 29,540 — waiting for gas', ago(420), 3),
  },
  {
    _id: 'req_5504',
    account_id: 'acc_71c',
    asset: 'ETH',
    mode: 'direct',
    status: 'held',
    W: ADDR_W,
    amount_groth: 400_000,
    requested_groth: 400_000,
    delivered_groth: 400_000,
    fee_groth: 8_000,
    bridge_fee_groth: 14_665,
    bridge_fee_refund_groth: 0,
    fee_mode: 'on_top',
    deliver_at: ago(-3600),
    release_at: ago(7_200),
    status_at: ago(9_600),
    beam_txid: null,
    msg_id: null,
    eth_tx: null,
    eth_block: null,
    beam_height: null,
    eta_at: null,
    created_at: ago(30_000),
    held_from: 'scheduled',
    hold: hold('no free BEAM fee coin for the second leg (or a split) — a human owns this row', ago(9_600), 7),
  },
  {
    _id: 'req_5505',
    account_id: 'acc_3aa',
    asset: 'ETH',
    mode: 'direct',
    status: 'scheduled',
    W: ADDR_B,
    amount_groth: 120_000,
    requested_groth: 120_000,
    delivered_groth: 120_000,
    fee_groth: 2_400,
    bridge_fee_groth: 14_665,
    bridge_fee_refund_groth: 0,
    fee_mode: 'on_top',
    deliver_at: ago(-43_200),
    release_at: ago(-39_240),
    status_at: ago(1_200),
    beam_txid: null,
    msg_id: null,
    eth_tx: null,
    eth_block: null,
    beam_height: null,
    eta_at: ago(-39_000),
    created_at: ago(1_200),
    hold: hold(null, null),
  },
];

// ───────────────────────────────────────────────────────────────────────────── /unattributed

export const UNATTRIBUTED = [
  {
    _id: 'lock_138',
    at: ago(1800),
    status: 'open',
    asset: 'ETH',
    msg_id: 138,
    value_units: '3000000000000000',
    tx: TX_SRC,
    block: 25_942_600,
    pubkey_matched: true,
    tries: 6,
    next_try_at: null,
    reason: 'every quote that could pair with this lock is already bound to another deposit.',
    candidates: [{ quote_id: 'q_7719', account_id: 'acc_9f2', created_at: ago(2400) }],
  },
  {
    _id: 'lock_143',
    at: ago(300),
    status: 'open',
    asset: 'ETH',
    msg_id: 143,
    value_units: '1000000000000000',
    tx: TX_PIPE,
    block: 25_942_712,
    pubkey_matched: true,
    tries: 1,
    next_try_at: NOW_S + 240,
    reason: 'the sender has not registered a deposit yet.',
    candidates: [],
  },
  {
    _id: 'lock_140',
    at: ago(9_400),
    status: 'claimed',
    asset: 'ETH',
    msg_id: 140,
    value_units: '2000000000000000',
    tx: TX_PIPE,
    block: 25_942_701,
    pubkey_matched: true,
    tries: 2,
    next_try_at: null,
    reason: null,
    candidates: [],
  },
];

// ──────────────────────────────────────────────────────────────────────────────── /accounts

export const ACCOUNTS = [
  {
    account_id: 'acc_9f2',
    address: ADDR_A,
    created_at: ago(400_000),
    last_login_at: ago(3_600),
    last_chain_id: 1,
    deposits: 2,
    payouts: 3,
    destinations: 2,
    balances: { ETH: { available_groth: 1_248_000, scheduled_groth: 250_000, sent_groth: 199_990 } },
  },
  {
    account_id: 'acc_3aa',
    address: ADDR_B,
    created_at: ago(90_000),
    last_login_at: ago(600),
    last_chain_id: 42161,
    deposits: 2,
    payouts: 2,
    destinations: 1,
    balances: { ETH: { available_groth: 0, scheduled_groth: 210_000, sent_groth: 0 } },
  },
  {
    account_id: 'acc_71c',
    address: ADDR_D,
    created_at: ago(12_000),
    last_login_at: ago(120),
    last_chain_id: 8453,
    deposits: 1,
    payouts: 1,
    destinations: 1,
    balances: { ETH: { available_groth: 299_990, scheduled_groth: 400_000, sent_groth: 0 } },
  },
];

// ──────────────────────────────────────────────────────────────────────────────── /treasury

const read = (value: unknown) => ({ value, error: null });
const unreadable = (why: string) => ({ value: null, error: why });

export const TREASURY = {
  at: NOW_S,
  addresses: { treasury: TREASURY_ADDR, float_primary: MP_1, mp_registry: [MP_1, MP_2, MP_3], mp_registry_size: 3 },
  beam_fees: read(930_600_000),
  fee_alert_groth: 500_000_000,
  wallet_status: read({
    available: 943_700_000,
    current_height: 4_031_015,
    current_state_hash: '3640ebda0a…b64a71',
    current_state_timestamp: ago(540),
    difficulty: 3_251_424.625,
    is_in_sync: true,
    maturing: 0,
    receiving: 15_100_000,
    sending: 15_200_000,
    totals: [
      { asset_id: 0, available: 943_700_000, available_mp: 0, available_regular: 943_700_000, maturing: 0 },
      { asset_id: 36, available: 175_681, available_mp: 0, available_regular: 175_681, maturing_mp: 1_652_864 },
    ],
  }),
  ledger: [
    { asset: 'ETH', aid: 36, treasury: read(175_681), locked: read(0), float: read(2_652_864) },
    { asset: 'DAI', aid: 39, treasury: read(0), locked: read(0), float: read(0) },
    { asset: 'WBTC', aid: 38, treasury: unreadable('BeamPayError: /balances timed out after 8 s'), locked: read(0), float: read(0) },
  ],
  wallet: [
    {
      asset: 'ETH',
      spendable: read({
        regular: 175_681,
        shielded: 0,
        maturing_regular: 0,
        maturing_mp: 1_652_864,
        maturing: 1_652_864,
        locked: 0,
        coins_regular: 1,
        coins_shielded: 0,
        coins_error: null,
      }),
    },
    {
      asset: 'DAI',
      spendable: read({
        regular: 0,
        shielded: 0,
        maturing_regular: 0,
        maturing_mp: 0,
        maturing: 0,
        locked: 0,
        coins_regular: 0,
        coins_shielded: 0,
        coins_error: null,
      }),
    },
    {
      asset: 'WBTC',
      spendable: read({
        regular: 0,
        shielded: 0,
        maturing_regular: 0,
        maturing_mp: 0,
        maturing: 0,
        locked: 0,
        coins_regular: null,
        coins_shielded: null,
        coins_error: 'BeamError: get_utxo refused',
      }),
    },
  ],
  float_policy: {
    keep_groth: 5_000_000,
    liability_buffer_bps: 1000,
    spend_unshielded: true,
    per_asset: [
      { asset: 'ETH', scheduled_liability: read(610_000), with_buffer_groth: 671_000, keep_floor_groth: 5_000_000, keeps_groth: 5_000_000 },
      { asset: 'DAI', scheduled_liability: read(0), with_buffer_groth: 0, keep_floor_groth: 5_000_000, keeps_groth: 5_000_000 },
      { asset: 'WBTC', scheduled_liability: read(0), with_buffer_groth: 0, keep_floor_groth: 5_000_000, keeps_groth: 5_000_000 },
    ],
  },
  intents: { payouts: [PAYOUTS[1]], treasury: [DEPOSITS[1]] },
  held: { payouts: [PAYOUTS[3]], deposits: [] },
  shield: [
    {
      deposit_id: 'dep_01H8ZQ7',
      asset: 'ETH',
      treasury: 'shielded',
      value_groth: 199_990,
      claim_txid: BEAM_CLAIM,
      plan: [99_995, 99_995],
      sent: 2,
      calls: [],
      hold: hold(null, null),
    },
    {
      deposit_id: 'dep_01H8ZR4',
      asset: 'ETH',
      treasury: 'claiming',
      value_groth: 1_180_000,
      claim_txid: null,
      plan: [],
      sent: 0,
      calls: [],
      hold: hold(null, null),
    },
  ],
};

// ────────────────────────────────────────────────────────────────────────────────── /events

export const EVENTS = [
  { _id: 'ev_1', at: ago(70), kind: 'worker.pass', notified: true, account_id: null, text: '[3/5] scanner: 12 logs, 1 attributed' },
  {
    _id: 'ev_2',
    at: ago(300),
    kind: 'payout.delayed',
    notified: false,
    account_id: 'acc_3aa',
    text: 'req_5503 delayed: the relayer wants 41,200 groth against a ceiling of 29,540',
  },
  {
    _id: 'ev_3',
    at: ago(640),
    kind: 'payout.released',
    notified: true,
    account_id: 'acc_9f2',
    text: 'req_5502 released, kernel e5e5e5…, bridge msg 93',
  },
  {
    _id: 'ev_4',
    at: ago(5_400),
    kind: 'deposit.credited',
    notified: true,
    account_id: 'acc_9f2',
    text: 'dep_01H8ZQ7 credited 0.0019999 bETH',
  },
  {
    _id: 'ev_5',
    at: ago(9_600),
    kind: 'payout.held',
    notified: false,
    account_id: 'acc_71c',
    text: 'req_5504 held: no free BEAM fee coin for the second leg',
  },
  { _id: 'ev_6', at: ago(540), kind: 'deposit.refused', notified: true, account_id: 'acc_3aa', text: 'registration refused: to != pipe' },
];

export const BEAMPAY_EVENTS = [
  {
    _id: 'bp_1',
    received_at: ago(5_500),
    event: 'contract_tx',
    status: 'booked',
    txid: BEAM_CLAIM,
    asset: 'bETH',
    amount: 0.0019999,
    tries: 1,
    text: 'contract_tx booked, fee 0.121 BEAM',
  },
  {
    _id: 'bp_2',
    received_at: ago(600),
    event: 'withdraw',
    status: 'pending',
    txid: BEAM_SEND,
    asset: 'bETH',
    amount: 0.0025,
    tries: 1,
    text: 'withdraw pending',
  },
  {
    _id: 'bp_3',
    received_at: ago(120),
    event: 'contract_tx',
    status: 'failed',
    txid: null,
    asset: 'BEAM',
    amount: 0,
    tries: 20,
    text: 'dead-lettered after 20 tries',
  },
];

// ──────────────────────────────────────────────────────────────────────────── the transport

function json(route: Route, status: number, body: unknown) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

/** The `{rows, total, limit, offset, has_more}` window every list route answers with. */
function window_(rows: unknown[], url: URL, extra: Record<string, unknown> = {}) {
  const limit = Number(url.searchParams.get('limit') ?? 50);
  const offset = Number(url.searchParams.get('offset') ?? 0);
  const page = rows.slice(offset, offset + limit);
  return { rows: page, total: rows.length, limit, offset, has_more: offset + page.length < rows.length, ...extra };
}

function filterRows<T extends Record<string, unknown>>(rows: T[], url: URL, timeField: string): T[] {
  const status = url.searchParams.get('status');
  const since = url.searchParams.get('since');
  const kind = url.searchParams.get('kind');
  return rows.filter((r) => {
    if (status && r.status !== status) return false;
    if (kind && r.kind !== kind) return false;
    if (since && Number(r[timeField] ?? 0) < Number(since)) return false;
    return true;
  });
}

export class MockAdminApi {
  key = ADMIN_KEY;
  /** where the API is mounted; the console only ever calls this one */
  base = '/api/admin';
  /** every admin path served, in order — the auto-refresh tests count these */
  calls: string[] = [];
  /** wrong-key attempts; the fifth is a 429, and the operator is paged once, not five times */
  failures = 0;
  pagesSent = 0;

  countOf(path: string): number {
    return this.calls.filter((c) => c.split('?')[0] === path).length;
  }

  async install(page: Page) {
    // Both candidate mounts are routed so an unmocked one answers 404 like an API would, rather
    // than falling through to `vite preview`'s SPA fallback (a 200 full of HTML).
    await page.route(/\/api\/(v1\/)?admin(\/|$)/, (route) => this.handle(route));
  }

  private handle(route: Route) {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith(`${this.base}/`)) return json(route, 404, { detail: 'Not Found' });

    const headers = route.request().headers();
    const bearer = (headers['authorization'] ?? '').replace(/^Bearer\s+/i, '');
    const given = headers['x-admin-key'] ?? bearer;
    if (given !== this.key) {
      this.failures += 1;
      if (this.failures >= 5) {
        if (this.pagesSent === 0) this.pagesSent = 1; // one digest, however many failures follow
        return json(route, 429, { detail: 'Too many attempts' });
      }
      return json(route, 404, { detail: 'Not Found' });
    }

    const path = url.pathname.slice(this.base.length);
    this.calls.push(path + (url.search || ''));
    const body = this.payload(path, url);
    if (body === undefined) return json(route, 404, { detail: 'Not Found' });
    return json(route, 200, body);
  }

  private payload(path: string, url: URL): unknown {
    switch (path) {
      case '/overview':
        return OVERVIEW;
      case '/deposits':
        return window_(filterRows(DEPOSITS, url, 'created_at'), url, { statuses: DEPOSIT_STATUSES });
      case '/payouts':
        return window_(filterRows(PAYOUTS, url, 'created_at'), url, { statuses: PAYOUT_STATUSES });
      case '/unattributed':
        return window_(filterRows(UNATTRIBUTED, url, 'at'), url);
      case '/accounts':
        return window_(ACCOUNTS, url);
      case '/treasury':
        return TREASURY;
      case '/events':
        return window_(filterRows(EVENTS, url, 'at'), url, { kinds: [...new Set(EVENTS.map((e) => e.kind))].sort() });
      case '/beampay-events':
        return window_(BEAMPAY_EVENTS, url);
      default:
        break;
    }
    const dep = /^\/deposits\/(.+)$/.exec(path);
    if (dep) {
      const row = DEPOSITS.find((d) => d._id === decodeURIComponent(dep[1]));
      if (!row) return undefined;
      return {
        deposit: row,
        raw: row,
        events: EVENTS.filter((e) => e.text.includes(row._id)),
        entries: [
          {
            _id: 'en_1',
            kind: 'deposit_credit',
            groth: row.value_groth,
            d_avail: row.value_groth,
            asset: row.asset,
            ref: row._id,
            at: row.updated_at,
          },
        ],
        quote: { _id: row.quote_id, created_at: row.created_at, mode: row.mode, address: row.address, value_groth: row.value_groth },
      };
    }
    const pay = /^\/payouts\/(.+)$/.exec(path);
    if (pay) {
      const row = PAYOUTS.find((p) => p._id === decodeURIComponent(pay[1]));
      if (!row) return undefined;
      return {
        payout: row,
        hold: row.hold,
        events: EVENTS.filter((e) => e.text.includes(row._id)),
        entries: [
          {
            _id: 'en_2',
            kind: 'payout_debit',
            groth: -(row.amount_groth + row.fee_groth),
            asset: row.asset,
            ref: row._id,
            at: row.created_at,
          },
        ],
        beampay_tx: row.beam_txid
          ? { txid: row.beam_txid, tx: { status: 3, booked: true, fee: 12_100_000 }, error: null }
          : { txid: null, tx: null, error: null },
      };
    }
    return undefined;
  }
}
