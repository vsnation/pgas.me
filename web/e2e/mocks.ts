// Test doubles for the two things the web app talks to: the Pgas.me API (API_CONTRACT.md, served
// through page.route) and an injected EIP-1193 wallet (window.ethereum + EIP-6963 announcement).
// personal_sign is delegated to Node-side ethers wallets through page.exposeFunction, so the mock
// API can really recover signers and the message templates are checked end to end.
import type { Page, Route } from '@playwright/test';
import { AbiCoder, Interface, Wallet, getAddress, getBytes, verifyMessage } from 'ethers';
// ── T35 block ─────────────────────────────────────────────────────────────────────────────────
// The API's own refusal sentences (e2e/api-sentences.ts), so this mock refuses the way production
// refuses and a screenshot shows the wording a user will actually read.
import * as SAY from './api-sentences';
// ──────────────────────────────────────────────────────────────────────────────────────────────

export const HOST = '127.0.0.1:4173';
export const NATIVE = '0x0000000000000000000000000000000000000000';
export const USDC = '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48';
export const STATEMENT = 'Sign in to Pgas.me. This signature costs nothing and moves nothing.';

export const walletA = new Wallet('0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d');
export const walletB = new Wallet('0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a');

export interface ApiCall {
  method: string;
  path: string;
  body: unknown;
  auth: string | null;
}

// Five EVM chains (Story and Cronos have no batch-balance helper, so the scanner uses Multicall3
// there) plus the two non-EVM chains the API lists, which must render as "not scanned", never as
// errors. `route_chain_id` is the router's own id and differs from the EVM id on three of them.
const CHAINS = [
  { chain_id: 1, route_chain_id: 1, name: 'Ethereum', native_symbol: 'ETH' },
  { chain_id: 42161, route_chain_id: 42161, name: 'Arbitrum', native_symbol: 'ETH' },
  { chain_id: 8453, route_chain_id: 8453, name: 'Base', native_symbol: 'ETH' },
  { chain_id: 1514, route_chain_id: 100000013, name: 'Story', native_symbol: 'IP' },
  { chain_id: 25, route_chain_id: 100000019, name: 'Cronos', native_symbol: 'CRO' },
  { chain_id: 7565164, route_chain_id: 7565164, name: 'Solana', native_symbol: 'SOL' },
  { chain_id: 728126428, route_chain_id: 100000026, name: 'Tron', native_symbol: 'TRX' },
];

export const DAI = '0x6B175474E89094C44Da98b954EedeAC495271d0F';
export const WBTC = '0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599';

/**
 * The token list's logo host. A stand-in here — the real list names whatever CDN it likes and the
 * client never spells one out — but the URL shape is the live one; see NATIVE_TOKENS below.
 * `blockExternal(page, true)` serves anything under it locally, so screenshots stay offline.
 */
export const LOGO_HOST = 'https://tokens.cdn.invalid';
export const TOKEN_LOGO = (chainId: number, address: string) => `${LOGO_HOST}/Logo/${chainId}/${address}/small/token-logo.svg`;

const ERC20_TOKENS = [
  { address: USDC, symbol: 'USDC', name: 'USD Coin', decimals: 6, logo: TOKEN_LOGO(1, USDC) },
  { address: DAI, symbol: 'DAI', name: 'Dai', decimals: 18, logo: '' },
  { address: WBTC, symbol: 'WBTC', name: 'Wrapped BTC', decimals: 8, logo: '' },
];

/**
 * The token list's native entry, verified against the live API on 2026-09-09:
 *   GET /v1/dex/tokens?chain_id=56 → {"address":"0x0000…0000","symbol":"BNB","name":"BNB",
 *   "decimals":18,"logo":"<the list's own CDN>/Logo/56/0x0000…0000/small/token-logo.svg"}
 * Cronos is deliberately absent so a chain whose list carries NO native entry is exercised: its chip
 * must fall back to the chain icon, never to a 3-letter monogram.
 */
const NATIVE_TOKENS: Record<number, { symbol: string; name: string }> = {
  1: { symbol: 'ETH', name: 'Ethereum' },
  42161: { symbol: 'ETH', name: 'Ethereum' },
  8453: { symbol: 'ETH', name: 'Ethereum' },
  1514: { symbol: 'IP', name: 'Story IP' },
};

function tokensFor(chainId: number) {
  const n = NATIVE_TOKENS[chainId];
  return n ? [{ address: NATIVE, ...n, decimals: 18, logo: TOKEN_LOGO(chainId, NATIVE) }, ...ERC20_TOKENS] : ERC20_TOKENS;
}

/** Ethereum's list: what the Deposit form loads by default, and the decimals table for MockRpc. */
const TOKENS = tokensFor(1);

const ASSETS = [
  {
    key: 'ETH',
    symbol: 'ETH',
    beam_symbol: 'bETH',
    token: NATIVE,
    decimals: 18,
    pipe: '0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96',
    aid: 36,
  },
  {
    key: 'DAI',
    symbol: 'DAI',
    beam_symbol: 'bDAI',
    token: '0x6B175474E89094C44Da98b954EedeAC495271d0F',
    decimals: 18,
    pipe: '0xAcDc8f4559741a3c8CAAB0ba74c57807A9Fe2d73',
    aid: 39,
  },
  {
    key: 'WBTC',
    symbol: 'WBTC',
    beam_symbol: 'bWBTC',
    token: '0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599',
    decimals: 8,
    pipe: '0x604422D7eC88c45b82B71851d073eFeaA928dcEF',
    aid: 38,
  },
];

const TX = '0x' + 'ab'.repeat(32);
// ── T35 block: the asset's grid, `max(1, 10 ** (8 − eth_decimals))` — 1 groth for every asset
// this deployment carries (api/pgasme/routers/withdrawals.py `grid_groth`). ───────────────────
const GRID_GROTH = 1;
/** unix seconds, `d` seconds from the moment the suite loaded — see the payout fixtures below. */
const AT = (d: number) => Math.floor(Date.now() / 1000) + d;
/** `withdrawals.FROM_AMOUNT_NOTE` — the API's own words for a row that pays its fees out of itself. */
const FROM_AMOUNT_NOTE = 'fees taken from the amount — not enough balance to pay them on top';

const ROUTER_ORDER = '0xeF4fB24aD0916217251F553c0596F8Edc630EB66';
const ROUTER_ALLOWANCE_TARGET = '0x6A000F20005980200259B80c5102003040001068';

/**
 * The Uniswap V4 ingress (API_CONTRACT.md § Quote mode "uniswap"): the tx goes to the PgasRouter,
 * and the hook's address carries its permissions in its low 14 bits — hence the `2888` tail, which
 * is the real deployment rule (contracts/README.md), not decoration.
 */
/**
 * U2 (2026-09-10) — the two-step route deploys NOTHING: the swap goes to Uniswap's own Universal
 * Router through Permit2, and the ETH lands in the user's own wallet. Permit2 is the real mainnet
 * address (it is the same on every chain); the router is a stand-in with the right shape.
 */
export const UNIVERSAL_ROUTER = '0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af';
export const PERMIT2 = '0x000000000022D473030F116dDEE9F6B43aC78BA3';

export const PGAS_ROUTER = '0x9a5c1d4F2B7e8A0C3d6f1b4e7A0C3D6f9b2e5a11';
export const PGAS_HOOK = '0x1f3D5b7C9e2A4d6F8b0C2E4a6D8f0B2C4E6A2888';
const GATEWAY_POOL_ID = '0x' + 'a1'.repeat(32);
const INNER_POOL_ID = '0x' + 'b2'.repeat(32);

// ─────────────────────────────────────────────────────────────────────────────────────────────
// T46 BLOCK — the SHIPPED two-step shape (U2-api, 2026-09-10 15:15Z). ADDITIVE.
//
// `approvals:[…]` in the order they must be sent, each one a whole transaction the API built:
// `approval_reset` (USDT-style: an existing, non-zero, short allowance has to be zeroed before it
// can be raised), `approval` (token → Permit2) and `permit_tx` (Permit2 → the Universal Router).
// The client sends what it is given, in order — it does not decide which of them exist, and it no
// longer builds the ERC-20 calldata itself, so the calldata here is REAL: a test that asserts on
// it is asserting the bytes the wallet would be handed.
// ─────────────────────────────────────────────────────────────────────────────────────────────

/** Real selectors, so `data` is the transaction and not a label. */
export const ERC20_APPROVE_SELECTOR = '0x095ea7b3';
export const PERMIT2_APPROVE_SELECTOR = '0x87517c45';
const word = (v: bigint | number) => BigInt(v).toString(16).padStart(64, '0');
const addrWord = (a: string) => a.slice(2).toLowerCase().padStart(64, '0');
/** `approve(spender, amount)` — the token's own allowance to Permit2 (or the zeroing of it). */
export const erc20ApproveData = (spender: string, amount: bigint) => ERC20_APPROVE_SELECTOR + addrWord(spender) + word(amount);
/** `Permit2.approve(token, spender, uint160 amount, uint48 expiration)` — the router's allowance. */
export const permit2ApproveData = (token: string, spender: string, amount: bigint, expiration: number) =>
  PERMIT2_APPROVE_SELECTOR + addrWord(token) + addrWord(spender) + word(amount) + word(expiration);
/** The router deadline doubles as the Permit2 expiry (API_CONTRACT.md); frozen so tests can assert it. */
export const SWAP_DEADLINE = 1_780_000_000;

/**
 * One row, priced and ruled on — the shape `POST /v1/withdrawals/preview` returns, `POST
 * /v1/withdrawals` puts inside a 422 `detail.items`, and the real API's `price_item` writes
 * (api/pgasme/routers/withdrawals.py): the numbers, the echoed `deliver_at`, the floor the item
 * was ruled against, and — when it cannot be scheduled — the sentence AND its stable code.
 *
 * ⛔ A problem here is the ITEM's. The batch rule (Σ total ≤ Available) is not an item's problem
 * and never was one in the real API; this mock used to invent one, which is how the web grew a
 * refusal shape the API does not send (F4, 2026-09-10).
 */
export interface PricedItem {
  W: string;
  amount_groth: number;
  fee_groth: number;
  bridge_fee_groth: number;
  total_groth: number;
  deliver_at: number;
  release_at: number;
  min_amount_groth: number;
  /**
   * T45 item 5 — the largest amount this row can ask for and still have its fees charged ON TOP,
   * out of what the rows above it left of Available (`price_item`, api side). It is what "Use max"
   * fills in: the API's own lattice, never `available − 2% − bridge` done in the form.
   */
  max_on_top_groth: number;
  ok: boolean;
  problem?: string;
  problem_code?: string;
  // ── T35 block: fees on top when affordable, from the amount otherwise (2026-09-10) ──────────
  // Optional because `publishFeeSplit = false` models the API build that predates them, which is
  // the world in which the UI must render a dash instead of deriving a delivery of its own.
  /** what the wallet receives — `amount_groth` on an `on_top` row, less on a `from_amount` one */
  delivered_groth?: number;
  /** what this row takes off Available (`amount + fees`, or exactly `amount`) */
  debited_groth?: number;
  fee_mode?: 'on_top' | 'from_amount';
  /** what the user typed, restated under an unambiguous name */
  requested_groth?: number;
  /** the API's own sentence for a from-amount row */
  fee_note?: string;
  // ────────────────────────────────────────────────────────────────────────────────────────────
}

/**
 * The verdict on the LIST: `preview` publishes it, `POST /v1/withdrawals` refuses on it.
 *
 * Field for field what the real `batch_verdict` writes (api/pgasme/routers/withdrawals.py) — the
 * two numbers it was decided FROM travel with it, not just the difference. The client reads none
 * of them but `ok`, `shortfall_groth` and `problem`; a mock that sends less than the API does is
 * how the web grows a shape production never had, which is the whole of F4.
 */
export interface PricedVerdict {
  ok: boolean;
  /** Σ `total_groth` over the list — what the batch would debit */
  need_groth: number;
  /** the balance it was ruled against, as it was at that moment */
  available_groth: number;
  shortfall_groth: number;
  problem?: string;
}

/**
 * ── T52 block ───────────────────────────────────────────────────────────────────────────────
 * The TREASURY's verdict, beside the batch's. `batch` asks whether the user's Available covers
 * the list; this asks whether we can MOVE it today — the bETH shielded last night is inside a
 * max-privacy lock for up to 72 hours, and no top-up shortens it.
 *
 * Field for field what `withdrawals.treasury_verdict` writes. Every number may be `null`, and
 * `null` is not zero: it is "the API could not measure the treasury", which is `ok: true`.
 */
export interface TreasuryVerdict {
  ok: boolean;
  float_now_groth: number | null;
  deliverable_now_groth: number | null;
  next_unlock_at: number | null;
  problem?: string;
}

export interface PricedBatch {
  items: PricedItem[];
  // ── T35 block: `delivered_groth` is Σ what the wallets receive. See `priceBatch` for why it is
  // optional in this mock and never optional in the API. ───────────────────────────────────────
  totals: {
    amount_groth: number;
    fee_groth: number;
    bridge_fee_groth: number;
    total_debited_groth: number;
    delivered_groth?: number;
  };
  available_groth: number;
  min_amount_groth: number;
  batch: PricedVerdict;
  /** T52 — absent on an API build from before it, exactly as the real one omits it */
  treasury?: TreasuryVerdict;
  fee_bps: number;
  bridge_eta_s: number;
}

export class MockApi {
  armed = false;
  directEnabled = true;
  instantEnabled = false;
  /**
   * `ingress:{uniswap, xchain, direct, uniswap_tokens}` as the API publishes it on `/dex/assets`
   * (with the pairs) and `/health` (without), and `uniswap`/`xchain` inside `/account.ingress`.
   *
   * Both start where the suite's existing journeys live — the pre-Uniswap world — for the same
   * reason `armed` starts false: a test states the world it is exercising. A test of the Uniswap
   * ingress sets `uniswapEnabled = true`, and `uniswapEnabled = false` IS the "the API says the
   * path is closed" case that the rest of the suite proves on every run.
   */
  uniswapEnabled = false;
  xchainEnabled = true;
  /**
   * An API build that publishes NO flags anywhere: `/dex/assets` and `/health` are 404, `/assets`
   * carries the asset registry only, and the account's `ingress` is the old `{armed, near}`. That
   * is today's live API, and the client must land on the defaults (lib/ingress.ts) — Uniswap off.
   */
  silentIngress = false;
  /** The pairs a gateway pool is registered for; the client narrows its token list to these. */
  uniswapTokens = ['ETH', 'WETH', 'USDC', 'USDT', 'DAI', 'WBTC'];

  // ---- T31 C3 / T31 D1 / U2 (2026-09-10) — added by the web worker, all defaults = today ----
  /**
   * `PGAS_INGRESS_DEFAULT_ROUTE`, published as `ingress.default_route` (T31 D1). Null is the build
   * that has never heard of the setting — which is every build before 2026-09-10, and the case the
   * client must keep behaving exactly as it did.
   */
  defaultRoute: 'xchain' | 'uniswap' | null = null;
  /**
   * The hosted token lists (T31 C3): `GET /tokens/<chain_id>.json`, served by nginx beside the SPA.
   * `false` is the box that has not built them yet — the file is missing and the client must fall
   * back to `/api/v1/dex/tokens` without the user seeing anything at all.
   */
  staticTokens = true;
  /** Every `/tokens/*.json` actually served, so a test can prove which read the picker made. */
  tokenFileReads: number[] = [];
  /**
   * Does this request ask for the Uniswap route? The mirror of the API's ONE resolver
   * (`pgasme/uniswap.py: wants_uniswap`, T31b item 1): `"uniswap"` always does; `"auto"` — and an
   * omitted route, which IS auto — only when `default_route` resolves to it. A mock that answered
   * uniswap for an explicit route alone would let the client ship a bug this shape and stay green.
   */
  wantsUniswap(route: unknown): boolean {
    const r =
      String(route ?? 'auto')
        .trim()
        .toLowerCase() || 'auto';
    return r === 'uniswap' || (r === 'auto' && this.defaultRoute === 'uniswap');
  }
  /**
   * U2 (2026-09-10): mode `uniswap` as TWO steps with nothing deployed — a swap on Uniswap V4 into
   * the user's OWN wallet, then a plain direct deposit of what arrived. `false` keeps the
   * deployed-hook shape (one transaction, `PGAS_UNISWAP_HOOK_ENABLED=1`), which the API still emits
   * behind that flag and the client must still render.
   */
  uniswapTwoStep = false;
  /** Whether the token → Permit2 allowance is already in place (the API omits `approval` when it is). */
  uniswapAllowanceCovers = false;
  /**
   * Whether Permit2's allowance for the Universal Router already covers this swap. The API omits
   * the `Permit2.approve(token, router, …)` transaction when it does.
   *
   * ⛔ There is no off-chain permit any more (T31b item 9): the mock used to send an EIP-712
   * `PermitSingle` for the wallet to sign, and the app dropped the signature on the floor because
   * it belongs inside calldata the API builds. Two one-off allowances, both transactions.
   */
  uniswapPermitCovers = false;
  /**
   * Emit that transaction under its PRE-RENAME name, `permit_fallback_tx` (the name API_CONTRACT.md
   * gave it while it was still the fallback to a signature). The API-side rename lands in a
   * different task, so the client has to read both — and this is what proves it does.
   */
  uniswapLegacyPermitField = false;
  // ───────────────────────────────────────────────────────────────────────────────────────────
  // T46 BLOCK — the shipped `approvals[]` shape, and the old names it replaced. ADDITIVE.
  // ───────────────────────────────────────────────────────────────────────────────────────────
  /**
   * The token holds a SHORT BUT NON-ZERO allowance to Permit2 — the USDT case. `approve` on such a
   * token reverts unless the allowance is zeroed first, so the API puts an `approval_reset` in
   * front of the `approval` and the client must send BOTH, in that order. Three transactions
   * before the swap is the deepest this route ever goes.
   */
  uniswapNeedsAllowanceReset = false;
  /**
   * Answer with the PRE-SHIPPED field names — `approval` + `permit_tx`/`permit_fallback_tx` and no
   * `approvals[]` — which is what an API built before 2026-09-10 15:15Z sends, and what a box that
   * has not been deployed yet still sends. The client reads one shape through one adapter; this is
   * the knob that proves the other half of it.
   */
  uniswapLegacyApprovalFields = false;
  /**
   * T31 H — how many times `POST /v1/deposits` refuses before it accepts, and with what. 409 is the
   * live one: "that transaction is not visible on Ethereum yet" (the admin's screenshot, 2026-09-10).
   * A definitive status here (400) is the other case: a refusal that will never succeed.
   */
  registerFailures = 0;
  registerFailStatus = 409;
  registerFailDetail = 'that transaction is not visible on Ethereum yet';
  /**
   * T37: a hash Ethereum has not shown the API yet is accepted anyway and proven later, so the row
   * comes back `verified:false`. It is not a failure and must never be rendered as one.
   */
  registerVerified = true;
  /** What the quoter says this size costs on the inner pool (0 when nothing is swapped). */
  uniswapPriceImpactBps = 30;
  /** `min_out_units = out × (1 − slippage_bps)` — the only slippage bound on this path. */
  uniswapSlippageBps = 50;
  /**
   * settings.min_deposit_wei. The floor lives HERE, never in the client: when a quote comes in under
   * it the API answers 400 with its own sentence, and that sentence is the only minimum the UI shows.
   */
  minDepositWei: bigint | null = null;
  /** `GET /v1/withdrawals/fees` — the form reads every one of these, none of them is a client copy. */
  feeBps = 200;
  /**
   * What a crossing costs right now, before headroom: the ETH transaction the relayer sends, the
   * $0.1–$0.2 the admin named on 2026-09-10. It is charged to the user at cost, itemised, which is
   * why there is no economic minimum any more.
   */
  bridgeFeeGrothNow = 20_000; // 0.0002 ETH
  /**
   * `max(PGAS_MIN_PAYOUT_GROTH, the asset grid)` with the default 1: a technical floor only. The
   * UI must state no minimum at all while this is the grid.
   */
  minAmountGroth = 1;
  bridgeEtaS = 3960; // PGAS_BRIDGE_ETA_S — 66 min
  /** The far end of the headroom curve: an order released a full window out funds 3× today's fee. */
  maxWindowS = 30 * 86400;
  farDatedMargin = 3;
  /**
   * The clock the API prices against. A test that pins the page's clock pins this too, so
   * `release_at` and the headroom that depends on it are the same arithmetic on both sides.
   */
  nowOverrideS: number | null = null;
  /** How long the n-th `POST /v1/withdrawals/preview` takes to answer, in ms (0 when unlisted). */
  previewDelaysMs: number[] = [];
  /** A preview the API cannot price: its own status and sentence, e.g. the 503 of an unreadable fee. */
  previewFail: { status: number; detail: string } | null = null;
  // ── T35 block ──────────────────────────────────────────────────────────────────────────────
  /**
   * The sentence a batch that does not fit is refused with. `null` = this mock's own older
   * wording (what `e2e/app.spec.ts` pins today); set it to `SAY.batchShort` for the API's verbatim
   * refusal, which is what the user actually reads.
   */
  batchSentence: ((asset: string, need: number, available: number, shortfall: number, feeBps: number) => string) | null = null;
  /**
   * Does this API build know about `delivered_groth` / `debited_groth` / `fee_mode` at all?
   * `false` is the build that is live while this is written: every fee is charged on top, a batch
   * that cannot pay them is simply refused, and the three fields never appear on the wire. The UI
   * must then say "the API did not say" — a dash — rather than showing the amount as a delivery.
   */
  publishFeeSplit = true;
  // ───────────────────────────────────────────────────────────────────────────────────────────
  // ── T52 block ──────────────────────────────────────────────────────────────────────────────
  /**
   * What the TREASURY can move right now, in groth — `null` means this build publishes no
   * treasury verdict at all (the default, so every test written before T52 sees the shape it was
   * written against). Set it to model the live wallet of 2026-09-10 15:36Z: 775,651 spendable and
   * 1,652,864 locked until the max-privacy lock ends.
   */
  treasuryFloatGroth: number | null = null;
  /** when the locked rest comes back (unix seconds), and the API's own rendering of that date */
  treasuryUnlockAt: number | null = null;
  treasuryUnlockWords = '';
  // ───────────────────────────────────────────────────────────────────────────────────────────
  /** Every preview answered, in order — so a test can assert which one the screen is showing. */
  previewResponses: PricedBatch[] = [];
  /**
   * What `POST /v1/quote/{id}/arm` does to the estimate, in per cent. The web client re-asks the
   * user when the armed order lands more than 0.5 % from the number they were shown.
   */
  armDriftPct = 0;
  /** `arm` on a quote the router would no longer honour: the API answers 409, never a stale tx. */
  armExpired = false;
  /**
   * What goes on the wire as the cross-chain mode. The neutral name is `xchain`; an API build that
   * still answers with its own older name must render exactly the same UI, so a test puts that name
   * here — the client treats every mode it does not know as the cross-chain order.
   */
  xchainWire = 'xchain';
  /** bytes32 `deposit_ref`s, handed out in order so a test can name the one it expects. */
  uniswapRefSeq = 0;
  quotes: Record<string, Record<string, any>> = {};
  armCalls: string[] = [];
  calls: ApiCall[] = [];
  token: string | null = null;
  accountId = 'acct-' + walletA.address.slice(2, 10).toLowerCase();
  nonces = new Set<string>();
  requestSeq = 0;
  deposits: Record<string, unknown>[] = [
    {
      // credited, and the treasury is still sweeping it into the shielded pool: a SUB-status the
      // user must never see as a pill (their balance was credited an hour ago)
      _id: 'dep-1',
      asset: 'ETH',
      mode: 'xchain',
      status: 'credited',
      src: { chain_id: 1, token: NATIVE, amount: '100000000000000000' },
      quote_id: 'q-1',
      src_tx_hash: '0x' + '11'.repeat(32),
      order_id: '0x' + '22'.repeat(32),
      eth: { tx: '0x' + '33'.repeat(32), block: 21000000, msg_id: 4821, value_units: '98000000000000000' },
      value_groth: 9800000,
      verified: true,
      address: walletA.address,
      treasury: 'shielding',
      claim_txid: 'b7c1d2e3f4a5b6c7',
      // T31b item 8 — the Beam block the claim's kernel landed in. The API is its one writer and
      // sends `null` until it is known; `dep-2` below (still confirming) has none, which is what
      // the page must render as "no link", not as a link to block 0.
      beam_height: 4030012,
      created_at: 1757400000,
      updated_at: 1757400900,
    },
    {
      _id: 'dep-2',
      asset: 'ETH',
      mode: 'xchain',
      status: 'confirming',
      src: { chain_id: 42161, token: NATIVE, amount: '50000000000000000' },
      quote_id: 'q-2',
      src_tx_hash: '0x' + '44'.repeat(32),
      eth: { tx: '0x' + '55'.repeat(32), block: 21000100 },
      verified: true,
      address: walletA.address,
      created_at: 1757403600,
      updated_at: 1757403700,
    },
    {
      // registered seconds ago; the hash has not been tied to the quote yet
      _id: 'dep-3',
      asset: 'ETH',
      mode: 'direct',
      status: 'submitted',
      src: { chain_id: 1, token: NATIVE, amount: '30000000000000000' },
      quote_id: 'q-3',
      src_tx_hash: '0x' + '88'.repeat(32),
      eth: {},
      verified: false,
      address: walletA.address,
      created_at: 1757404200,
      updated_at: 1757404200,
    },
  ];
  /**
   * ── T40 block ──────────────────────────────────────────────────────────────────────────────
   * The payout fixtures, one per status the order machine can be in, positioned AROUND NOW rather
   * than at fixed 2025 timestamps: the Balance page shows "Arrives ≈ in 40 min" now, and an ETA
   * anchored to a year-old fixture would have rendered "366 d ago" in every screenshot — which is
   * how a working column comes to look like a defect. No spec pins these times.
   *
   * `eta_at` / `eta_note` are what the API's ONE eta writer publishes (`payouts.eta_for`,
   * API_CONTRACT.md § Withdrawals): scheduled → the delivery time; releasing/bridging → release +
   * one bridge crossing, with the 18 h relayer tail on `eta_tail_s`; delivering → about five more
   * minutes; sent → nothing, because it is already there and the row carries the transaction.
   * ───────────────────────────────────────────────────────────────────────────────────────────
   */
  requests: Record<string, unknown>[] = [
    {
      _id: 'req-1',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 10000000,
      fee_groth: 200000,
      deliver_at: AT(92 * 60),
      release_at: AT(26 * 60),
      status: 'scheduled',
      created_at: AT(-40 * 60),
      eta_at: AT(92 * 60),
      eta_note: 'your delivery time — it goes to the bridge about an hour before',
    },
    {
      _id: 'req-2',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 1000000,
      fee_groth: 20000,
      deliver_at: AT(-6 * 3600),
      release_at: AT(-7 * 3600),
      status: 'sent',
      beam_txid: 'a1b2c3d4e5f6a1b2c3d4e5f6',
      msg_id: 4830,
      eth_tx: '0x' + '66'.repeat(32),
      eth_block: 21000420,
      relayer_fee_groth: 2200,
      beam_confirmations: 61,
      beam_height: 4031777,
      created_at: AT(-8 * 3600),
    },
    {
      // released but parked: the operator turned direct payouts off, so the row carries the reason
      _id: 'req-3',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 2000000,
      fee_groth: 40000,
      deliver_at: AT(-3 * 3600),
      release_at: AT(-4 * 3600),
      status: 'releasing',
      hold_reason: 'PGAS_PAYOUT_DIRECT_ENABLED=0',
      created_at: AT(-5 * 3600),
      eta_at: AT(66 * 60),
      eta_note: "bridge ≈ 1 h, up to 18 h in the relayer's tail",
      eta_tail_s: 18 * 3600,
    },
    {
      _id: 'req-4',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 3000000,
      fee_groth: 60000,
      bridge_fee_groth: 20000,
      deliver_at: AT(-3600),
      release_at: AT(-2 * 3600),
      status: 'bridging',
      beam_txid: 'c3d4e5f6a1b2c3d4e5f6a1b2',
      msg_id: 4831,
      beam_confirmations: 43,
      relayer_fee_groth: 2200,
      created_at: AT(-3 * 3600),
      eta_at: AT(40 * 60),
      eta_note: "bridge ≈ 1 h, up to 18 h in the relayer's tail",
      eta_tail_s: 18 * 3600,
    },
    {
      _id: 'req-5',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 4000000,
      fee_groth: 80000,
      bridge_fee_groth: 20000,
      deliver_at: AT(-1800),
      release_at: AT(-5400),
      status: 'delivering',
      beam_txid: 'd4e5f6a1b2c3d4e5f6a1b2c3',
      msg_id: 4832,
      beam_confirmations: 61,
      relayer_fee_groth: 2200,
      created_at: AT(-9000),
      eta_at: AT(300),
      eta_note: 'the relayer is sending it to your wallet — about five minutes',
    },
    // the any-asset branch is dark; these two exist so the pill map is proven to cover them
    {
      _id: 'req-6',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 1500000,
      fee_groth: 30000,
      deliver_at: AT(-2400),
      release_at: AT(-6000),
      status: 'waiting_for_dep_eth',
      dark: true,
      created_at: AT(-9600),
    },
    {
      _id: 'req-7',
      asset: 'ETH',
      mode: 'direct',
      W: walletB.address,
      amount_groth: 1500000,
      fee_groth: 30000,
      deliver_at: AT(-1200),
      release_at: AT(-4800),
      status: 'waiting_for_swap_to_target_asset',
      dark: true,
      created_at: AT(-8400),
    },
  ];
  balances = { ETH: { available: 50000000, scheduled: 10200000, sent: 1000000, pending: 4900000 } };
  // ── T40 block ────────────────────────────────────────────────────────────────────────────────
  // The ledger entries `GET /v1/account` publishes. They were an inline literal inside `account()`
  // until a cancel had to append to them: a refund is a `cancel` entry against the request's id,
  // and that entry is the ONLY evidence that a legacy `failed` row has already been paid back —
  // the Balance page reads it rather than guessing from a status (lib/payouts.ts `refundedIds`).
  history: Record<string, unknown>[] = [
    { kind: 'credit', groth: 9800000, d_avail: 9800000, d_sched: 0, d_sent: 0, ref: 'dep-1', note: '', at: 1757400900 },
    { kind: 'schedule', groth: 10200000, d_avail: -10200000, d_sched: 10200000, d_sent: 0, ref: 'req-1', note: '', at: 1757406400 },
  ];
  // ─────────────────────────────────────────────────────────────────────────────────────────────

  private json(route: Route, status: number, body: unknown, headers?: Record<string, string>) {
    return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body), headers });
  }

  /** The one place the flags are stated, so every read that carries them says the same thing. */
  private ingress(withTokens = false) {
    return {
      uniswap: this.uniswapEnabled,
      xchain: this.xchainEnabled,
      // published by the API, read by nothing in the client — a key it must simply carry past
      direct: true,
      // T31 D1: which route the API picks for itself when both are open. Absent unless a test
      // states it, because an API that does not publish it is the build the client must tolerate.
      ...(this.defaultRoute ? { default_route: this.defaultRoute } : {}),
      ...(withTokens ? { uniswap_tokens: this.uniswapTokenRows() } : {}),
    };
  }

  /** `uniswap_tokens` in the live shape: the registered pairs, by address. */
  private uniswapTokenRows() {
    return this.uniswapTokens
      .map((sym) => TOKENS.find((t) => t.symbol.toLowerCase() === sym.toLowerCase()))
      .filter((t): t is (typeof TOKENS)[number] => !!t)
      .map((t) => ({ address: t.address, symbol: t.symbol, decimals: t.decimals }));
  }

  private account() {
    return {
      address: walletA.address,
      account_id: this.accountId,
      balances: this.balances,
      fee_bps: this.feeBps,
      denominations: [1000000, 10000000],
      min_payout_groth: this.minAmountGroth,
      modes: { direct: this.directEnabled, instant: this.instantEnabled },
      ingress: {
        armed: this.armed,
        near: false,
        ...(this.silentIngress ? {} : { uniswap: this.uniswapEnabled, xchain: this.xchainEnabled }),
      },
      deposits: this.deposits,
      requests: this.requests,
      destinations: 1, // the signed-in wallet is still auto-listed; the app no longer reads the list
      history: this.history,
    };
  }

  /** The clock this API prices against (a test may pin it, exactly as it pins the page's). */
  private now(): number {
    return this.nowOverrideS ?? Math.floor(Date.now() / 1000);
  }

  /**
   * How much more than today's bridge fee an order that waits `aheadS` before release must fund:
   * 1× when it goes now, `farDatedMargin×` a full window out, linear in between (the API's
   * `window_margin`, routers/withdrawals.py). The unspent part stays with the treasury.
   */
  headroomFor(aheadS: number): number {
    if (this.maxWindowS <= 0 || aheadS <= 0) return 1;
    return 1 + (this.farDatedMargin - 1) * Math.min(1, aheadS / this.maxWindowS);
  }

  /**
   * ONE implementation of what a list of orders costs (API_CONTRACT.md § Withdrawals — fee model,
   * 2026-09-10): our `fee_bps` cut, plus the crossing at cost × the headroom the wait needs, and
   * the batch rule Σ total ≤ Available. `preview` renders it and `POST /v1/withdrawals` charges
   * it — the same function, so the quote IS the charge.
   */
  priceBatch(raw: unknown[]): PricedBatch {
    const now = this.now();
    const available = this.balances.ETH.available;
    /**
     * ── T35 block ────────────────────────────────────────────────────────────────────────────
     * The fee rule the admin asked for on 2026-09-10: "You need to take fees above the amount user
     * requested … only if user doesn't have deposit to pay gas fees and 2% fees to us, we take it
     * from sending amount, so he gets less than 0.01 ETH."
     *
     * So the batch is allocated IN ROW ORDER against what is left of Available:
     *   · remaining ≥ amount + fee + bridge → `on_top`: the wallet gets the amount, the balance
     *     pays the fees;
     *   · remaining ≥ amount → `from_amount`: the balance pays exactly the amount and the wallet
     *     gets the largest grid value that still covers both fees out of it;
     *   · remaining < amount → nothing can be allocated. The item is priced on top and the BATCH
     *     verdict refuses the list — never the row (F4: a batch that does not fit is not one row's
     *     fault, and the real API has never marked an item for it).
     * ─────────────────────────────────────────────────────────────────────────────────────────
     */
    let remaining = available;
    const feeOf = (groth: number) => Math.ceil((groth * this.feeBps) / 10000);
    /**
     * The largest amount that can be DELIVERED when both fees come out of `amount` itself — the
     * same lattice as the API's `deliverable_groth`: a multiple of the grid, at least the floor,
     * with `d + fee(d) + bridge ≤ amount`, and 0 when nothing fits (a refusal, never an order
     * with nothing in it).
     */
    const first = Math.max(GRID_GROTH, Math.ceil(this.minAmountGroth / GRID_GROTH) * GRID_GROTH);
    const deliverableFrom = (amount: number, bridge: number) => {
      const cap = amount - bridge; // what is left for the delivery and our cut together
      if (cap < first + feeOf(first)) return 0;
      let d = Math.floor((cap * 10000) / (10000 + this.feeBps));
      while (d > 0 && d + feeOf(d) > cap) d--;
      while (d + 1 + feeOf(d + 1) <= cap) d++;
      return d >= first ? d - (d % GRID_GROTH) : 0;
    };
    /** the smallest amount that can still pay its own crossing (the API's `smallest_from_amount`) */
    const smallestFromAmount = (bridge: number) => first + feeOf(first) + bridge;
    /**
     * T35b — the smallest amount whose from-amount delivery is still at least HALF of it (the
     * API's `smallest_undominated`). A delivery that is mostly fees is not a withdrawal anyone
     * asked for, and the refusal names this number as one of the two ways out, so it has to be a
     * number that actually works: the predicate wobbles by a groth around the crossing, so this
     * walks down from the first amount that satisfies it, exactly as the API does.
     */
    const halfFits = (amount: number, bridge: number) => {
      const d = Math.max(first, Math.ceil(Math.ceil(amount / 2) / GRID_GROTH) * GRID_GROTH);
      return d + feeOf(d) + bridge <= amount;
    };
    const smallestUndominated = (bridge: number) => {
      const lo0 = Math.ceil(smallestFromAmount(bridge) / GRID_GROTH);
      let hi = Math.max(lo0, 1);
      for (let i = 0; i < 64 && !halfFits(hi * GRID_GROTH, bridge); i++) hi *= 2;
      let lo = lo0;
      while (lo < hi) {
        const mid = Math.floor((lo + hi) / 2);
        if (halfFits(mid * GRID_GROTH, bridge)) hi = mid;
        else lo = mid + 1;
      }
      // the predicate wobbles by a groth around the crossing, so walk down while it still holds
      for (let i = 0; i < 64 && lo > lo0 && halfFits((lo - 1) * GRID_GROTH, bridge); i++) lo--;
      return lo * GRID_GROTH;
    };

    const items: PricedItem[] = (raw ?? []).map((r) => {
      const it = (r ?? {}) as { W?: unknown; amount_groth?: unknown; deliver_at?: unknown };
      let W = String(it.W ?? '');
      let problem: string | undefined;
      let code: string | undefined;
      try {
        const checksummed = getAddress(W);
        if (checksummed !== W) ((problem = SAY.badChecksum(W)), (code = 'address'));
        W = checksummed;
      } catch {
        ((problem = SAY.notAnAddress(W)), (code = 'address'));
      }
      const amount = Number(it.amount_groth);
      if (!problem && (!Number.isInteger(amount) || amount <= 0))
        ((problem = 'amount must be a positive whole number of groth'), (code = 'amount'));
      if (!problem && amount < this.minAmountGroth) ((problem = SAY.belowMinimum(this.minAmountGroth, 'ETH')), (code = 'min_amount'));
      if (!problem && amount % GRID_GROTH) ((problem = SAY.offGrid('ETH', GRID_GROTH)), (code = 'grid'));
      const deliverAt = Number(it.deliver_at);
      if (!problem && !Number.isFinite(deliverAt))
        ((problem = SAY.badDeliverAt(Math.floor(this.maxWindowS / 86400))), (code = 'deliver_at'));
      const release_at = Math.max(now, (Number.isFinite(deliverAt) ? deliverAt : now) - this.bridgeEtaS);
      const amount_groth = Number.isFinite(amount) ? Math.trunc(amount) : 0;
      const bridge_fee_groth = Math.ceil(this.bridgeFeeGrothNow * this.headroomFor(release_at - now));
      const onTop = amount_groth + feeOf(amount_groth) + bridge_fee_groth;
      // T45 item 5 — what "Use max" fills in, from what the rows ABOVE this one left behind, on
      // the same lattice a from-amount row is solved on. Taken before this row consumes anything.
      const max_on_top_groth = deliverableFrom(Math.max(0, remaining), bridge_fee_groth);

      // priced even when it is refused, exactly as the API does it: a mistyped address does not
      // change what the amount would cost, and a form that blanks its totals over one bad row is
      // a form nobody can fill in
      let fee_mode: 'on_top' | 'from_amount' = 'on_top';
      let delivered_groth = amount_groth;
      let debited_groth = onTop;
      let fee_groth = feeOf(amount_groth);
      let fee_note = '';
      if (!this.publishFeeSplit || remaining >= onTop) {
        remaining -= onTop;
      } else if (remaining >= amount_groth) {
        const d = deliverableFrom(amount_groth, bridge_fee_groth);
        if (d > 0) {
          // OUR FEE ABSORBS THE ROUNDING, exactly as the API does it, so the three parts are the
          // debit to the groth: delivered + fee + bridge == amount
          fee_mode = 'from_amount';
          debited_groth = amount_groth;
          delivered_groth = d;
          fee_groth = amount_groth - bridge_fee_groth - d;
          fee_note = FROM_AMOUNT_NOTE;
        } else if (!problem) {
          problem = SAY.tooSmallForBridge(amount_groth, bridge_fee_groth, smallestFromAmount(bridge_fee_groth));
          code = 'bridge_fee';
        }
        // T35b — there IS a delivery and it is mostly fees: the API refuses that as its own
        // problem (`fees_dominate`), with the row's quote left exactly as priced
        if (!problem && d > 0 && 2 * d < amount_groth) {
          problem = SAY.feesDominate(amount_groth, onTop, smallestUndominated(bridge_fee_groth));
          code = 'fees_dominate';
        }
        remaining -= amount_groth;
      } else {
        // ── T35b block ─────────────────────────────────────────────────────────────────────
        // ⛔ THE API DOES MARK THIS ROW, and this mock used to leave it `ok: true`. `price_item`
        // gives it `problem_code: "batch"` with its own sentence; `_refuse_items` then SKIPS
        // that code and lets the batch rule answer (409 + the shortfall), which is where the
        // "the real API has never marked an item" reading came from — it refuses like a batch,
        // it just does not stay silent about which row ran out. A mock that sends less than the
        // API does is a mock that hides a page bug, and it hid one: the Schedule form rendered
        // every priced `problem` as a row error, so the first build to see a real answer would
        // have told the user their wallet was wrong.
        // ────────────────────────────────────────────────────────────────────────────────────
        if (!problem) {
          problem = SAY.rowShortfall(amount_groth, Math.max(0, remaining));
          code = 'batch';
        }
        // …and the row still CONSUMES its (on-top) quote, exactly as `price_items` folds it —
        // clamped at zero, so the row after it sees an empty balance and not a leftover
        remaining = Math.max(0, remaining - onTop);
      }
      // a row that cannot be paid at all keeps its ON-TOP quote (the API does that too), so
      // `delivered + fee + bridge == debited` holds for every row a client renders

      return {
        W,
        amount_groth,
        fee_groth,
        bridge_fee_groth,
        total_groth: debited_groth,
        deliver_at: Number.isFinite(deliverAt) ? deliverAt : 0,
        release_at,
        min_amount_groth: this.minAmountGroth,
        max_on_top_groth,
        ok: !problem,
        ...(problem ? { problem, problem_code: code } : {}),
        ...(this.publishFeeSplit
          ? { requested_groth: amount_groth, delivered_groth, debited_groth, fee_mode, ...(fee_note ? { fee_note } : {}) }
          : {}),
      };
    });
    // ── T52 block: the treasury's float, folded through the rows the way the release spends it
    // (`price_items`). Each admitted row takes its whole crossing — the delivery plus the relayer
    // fee the send burns — out of what the WALLET can move today; the row that no longer fits is
    // marked `treasury_float` and the batch is refused with one sentence. A `null` float is a
    // build that publishes no treasury verdict, and the fold does not run at all. ───────────────
    let floatLeft = this.treasuryFloatGroth;
    if (floatLeft !== null) {
      for (const i of items) {
        if (!i.ok) continue;
        const need = (i.delivered_groth ?? i.amount_groth) + i.bridge_fee_groth;
        if (need <= floatLeft) {
          floatLeft -= need;
          continue;
        }
        i.ok = false;
        i.problem_code = 'treasury_float';
        i.problem = SAY.treasuryShort(this.deliverableNow(floatLeft, i.bridge_fee_groth), this.treasuryUnlockWords);
      }
    }
    const sum = (f: (i: PricedItem) => number) => items.reduce((t, i) => t + f(i), 0);
    const totals = {
      amount_groth: sum((i) => i.amount_groth),
      fee_groth: sum((i) => i.fee_groth),
      bridge_fee_groth: sum((i) => i.bridge_fee_groth),
      total_debited_groth: sum((i) => i.total_groth),
      // ── T35b block ─────────────────────────────────────────────────────────────────────────
      // ON EVERY BATCH, as the real API sends it (`totals_of`). It used to appear only when some
      // row delivered less than it asked for, because two `toEqual` assertions in
      // `e2e/app.spec.ts` fail on an extra key and that file had another writer — a conditional
      // FIELD is a shape that exists nowhere in production, and the page it feeds cannot be
      // tested for the case it will actually meet. Both assertions carry the key now.
      ...(this.publishFeeSplit ? { delivered_groth: sum((i) => i.delivered_groth ?? i.amount_groth) } : {}),
      // ────────────────────────────────────────────────────────────────────────────────────────
    };
    return {
      items,
      totals,
      available_groth: available,
      min_amount_groth: this.minAmountGroth,
      // ⛔ THE SHORTFALL IS THE FIXED POINT (T35b): the top-up after which every row's fees ride
      // ON TOP, which is more than `need − available` whenever a row was priced from its amount.
      // The API iterates to it (`top_up_needed`); a row's on-top total does not depend on the
      // balance, so summing them here reaches the same number.
      batch: this.batchVerdict(
        totals.total_debited_groth,
        available,
        sum((i) => i.amount_groth + feeOf(i.amount_groth) + i.bridge_fee_groth),
      ),
      ...(this.treasuryFloatGroth === null ? {} : { treasury: this.treasuryVerdict(items) }),
      fee_bps: this.feeBps,
      bridge_eta_s: this.bridgeEtaS,
    };
  }

  /**
   * The largest amount ONE order may ask for out of `floatGroth` — `withdrawals.
   * deliverable_now_groth`. The crossing burns the delivery AND the relayer fee, so the offer is
   * the float minus the fee; 0 (never a negative) when nothing can be asked for.
   */
  deliverableNow(floatGroth: number, bridgeFeeGroth: number): number {
    return Math.max(0, floatGroth - bridgeFeeGroth);
  }

  /**
   * The TREASURY's verdict on this batch (`withdrawals.treasury_verdict`), beside the batch's own.
   * `problem` is the FIRST refused row's sentence — in a batch the rows are served in order, so
   * the row that ran out names what is left where it ran out, and that is the number that makes
   * the list go through.
   */
  treasuryVerdict(items: PricedItem[]): TreasuryVerdict {
    const short = items.find((i) => i.problem_code === 'treasury_float');
    const float = this.treasuryFloatGroth ?? 0;
    return {
      ok: !short,
      float_now_groth: float,
      deliverable_now_groth: this.deliverableNow(float, this.bridgeFeeGrothNow),
      next_unlock_at: this.treasuryUnlockAt,
      ...(short ? { problem: short.problem } : {}),
    };
  }

  /**
   * The batch rule, ruled on ONCE: Σ `total_groth` ≤ Available. It is a verdict on the LIST — the
   * row that happens to take the running total past Available is not a wrong row — and `preview`
   * publishes it as `batch` while `POST /v1/withdrawals` refuses with the same sentence and the
   * same number in `X-Shortfall-Groth`. One function, so the quote and the refusal cannot differ.
   */
  batchVerdict(need: number, available: number, onTopNeed: number = need): PricedVerdict {
    // ⛔ `ok` IS "Σ debited ≤ Available", NOT "the shortfall is zero" — the two stopped being one
    // number when the shortfall became a fixed point. Deciding `ok` on the shortfall would refuse
    // the batch T35 exists to allow: Σ amounts fits, Σ (amounts + fees) does not, so the last row
    // pays its own fees and the list goes through.
    const ok = need <= available;
    const shortfall = ok ? 0 : Math.max(0, onTopNeed - available);
    const out: PricedVerdict = { ok, need_groth: need, available_groth: available, shortfall_groth: shortfall };
    if (ok) return out;
    // ── T35b block ───────────────────────────────────────────────────────────────────────────
    // THE API'S OWN SENTENCE IS THE DEFAULT NOW (`e2e/api-sentences.ts` `batchShort`, copied from
    // `withdrawals.batch_problem`). The mock's older, friendlier paraphrase was kept only because
    // three assertions in `e2e/app.spec.ts` pinned it and that file had another writer; a mock
    // that says something production never says is a page tested against words nobody will read.
    // `batchSentence` stays as the hook a test uses to pin a DIFFERENT wording.
    // ─────────────────────────────────────────────────────────────────────────────────────────
    const say = this.batchSentence ?? SAY.batchShort;
    return { ...out, problem: say('ETH', need, available, shortfall, this.feeBps) };
  }

  async install(page: Page) {
    await page.route('**/api/v1/**', (route) => this.handle(route));
    // T31 C3 — the hosted lists nginx serves from /opt/pgasme/tokens, same origin, no /api prefix.
    await page.route(/\/tokens\/\d+\.json(\?.*)?$/, (route) => {
      const id = Number(/\/tokens\/(\d+)\.json/.exec(route.request().url())?.[1]);
      if (!this.staticTokens) {
        // the box has not built the file: a real 404 from nginx, not the SPA's index.html
        return route.fulfill({ status: 404, contentType: 'text/plain', body: 'Not Found' });
      }
      this.tokenFileReads.push(id);
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        headers: { 'Cache-Control': 'public, max-age=3600' },
        body: JSON.stringify({ chain_id: id, updated_at: '2026-09-10T00:00:00Z', tokens: tokensFor(id) }),
      });
    });
  }

  private async handle(route: Route) {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^\/api\/v1/, '');
    const method = req.method();
    let body: unknown = null;
    const raw = req.postData();
    if (raw) {
      try {
        body = JSON.parse(raw);
      } catch {
        body = raw;
      }
    }
    const auth = req.headers()['authorization'] ?? null;
    this.calls.push({ method, path: path + url.search, body, auth });
    const locked = () => {
      if (!this.token || auth !== `Bearer ${this.token}`) {
        void this.json(route, 401, { detail: 'sign in first' });
        return false;
      }
      return true;
    };
    const b = (body ?? {}) as Record<string, any>;

    if (method === 'GET' && path === '/siwe/nonce') {
      const nonce = 'n' + Math.random().toString(36).slice(2, 12);
      this.nonces.add(nonce);
      return this.json(route, 200, { nonce, statement: STATEMENT, domains: [HOST] });
    }
    if (method === 'POST' && path === '/siwe/verify') {
      const message = String(b.message ?? '');
      const lines = message.split('\n');
      const nonce = /Nonce: (\S+)/.exec(message)?.[1];
      if (!lines[0].startsWith(HOST + ' wants you to sign in')) return this.json(route, 400, { detail: 'bad host line' });
      if (!nonce || !this.nonces.has(nonce)) return this.json(route, 400, { detail: 'unknown nonce' });
      if (!/URI: http:\/\/127\.0\.0\.1:4173\n/.test(message) || lines[3] !== STATEMENT)
        return this.json(route, 400, { detail: 'bad message' });
      let signer: string;
      try {
        signer = verifyMessage(message, String(b.signature));
      } catch {
        return this.json(route, 400, { detail: 'signature unreadable' });
      }
      if (signer.toLowerCase() !== lines[1].toLowerCase()) return this.json(route, 401, { detail: 'signer mismatch' });
      this.nonces.delete(nonce);
      this.token = 'tok-' + Math.random().toString(36).slice(2);
      return this.json(route, 200, { token: this.token, account_id: this.accountId, address: getAddress(lines[1]), expires_in: 3600 });
    }
    if (method === 'GET' && path === '/dex/chains') return this.json(route, 200, { chains: CHAINS });
    if (method === 'GET' && path === '/dex/tokens')
      return this.json(route, 200, { tokens: tokensFor(Number(url.searchParams.get('chain_id'))) });
    // The asset registry has never carried the flags; the client reads it here anyway, because an
    // API build that puts them here instead must still be understood.
    if (method === 'GET' && path === '/assets') return this.json(route, 200, { assets: ASSETS });
    // Where the 2026-09-10 API publishes them, pairs and all. `silentIngress` is the build that
    // has no such route at all: 404, and the client is left with its defaults.
    if (method === 'GET' && path === '/dex/assets') {
      if (this.silentIngress) return this.json(route, 404, { detail: 'Not Found' });
      return this.json(route, 200, { assets: ASSETS, ingress: this.ingress(true) });
    }
    if (method === 'GET' && path === '/health') {
      if (this.silentIngress) return this.json(route, 404, { detail: 'Not Found' });
      return this.json(route, 200, {
        ok: true,
        version: 'test',
        env: 'test',
        ingress_armed: this.armed,
        paused: false,
        ingress: this.ingress(),
      });
    }
    if (!locked()) return;

    if (method === 'GET' && path === '/account') return this.json(route, 200, this.account());
    if (method === 'POST' && path === '/quote') {
      // API_CONTRACT.md "Quote modes": off Ethereum it is a cross-chain order; on Ethereum the source
      // token either IS the target asset (direct to the pipe) or is swapped in the user's own wallet.
      const amount = BigInt(String(b.amount));
      const target = ASSETS.find((a) => a.key === b.target_asset) ?? ASSETS[0];
      const src = tokensFor(Number(b.src_chain_id)).find((t) => t.address.toLowerCase() === String(b.src_token).toLowerCase()) ?? {
        address: String(b.src_token),
        symbol: 'UNKNOWN',
        name: 'Unknown',
        decimals: 18,
        logo: '',
      };
      const isTarget = src.address.toLowerCase() === target.token.toLowerCase();
      const mode = Number(b.src_chain_id) !== 1 ? 'xchain' : isTarget ? 'direct' : 'swap';
      const native = src.address.toLowerCase() === NATIVE;
      // a crude but deterministic conversion into the target asset's units
      const usdOf = (v: bigint, dec: number) => (Number(v) / 10 ** dec) * (native ? 4000 : src.symbol === 'WBTC' ? 100000 : 1);
      const toTarget = (v: bigint) => {
        const usd = usdOf(v, src.decimals);
        const price = target.key === 'ETH' ? 4000 : target.key === 'WBTC' ? 100000 : 1;
        return BigInt(Math.round((usd / price) * 10 ** target.decimals));
      };
      // API_CONTRACT.md § Quote mode "uniswap" (2026-09-10): asked for by name — OR resolved to
      // by `auto` — on Ethereum, ETH out, and only for a registered pair. The tx is built with the
      // quote (the quote is local, one Quoter read), so there is no /arm step and no order id.
      if (this.wantsUniswap(b.route) && this.uniswapEnabled && Number(b.src_chain_id) === 1 && target.key === 'ETH') {
        const registered = this.uniswapTokens.some(
          (t) => t.toLowerCase() === src.symbol.toLowerCase() || t.toLowerCase() === src.address.toLowerCase(),
        );
        if (!registered) return this.json(route, 400, { detail: `no Uniswap route for ${src.symbol}` });
        /**
         * U2 — the TWO-STEP shape (API_CONTRACT.md § Quote mode "uniswap" — TWO-STEP): step 1 is a
         * swap through the Universal Router into the user's OWN wallet, and step 2 is the ordinary
         * direct deposit of what actually arrived. Native ETH never needs step 1, so it falls
         * through to `direct` below — exactly as the contract says.
         */
        if (this.uniswapTwoStep && !native) {
          const out = (toTarget(amount) * 997n) / 1000n;
          const minOut = (out * BigInt(10000 - this.uniswapSlippageBps)) / 10000n;
          const quoteId = 'q-' + Math.random().toString(36).slice(2, 8);
          const two: Record<string, unknown> = {
            quote_id: quoteId,
            mode: 'uniswap',
            step: 'swap',
            target_asset: target.key,
            armed: this.armed,
            expires_at: new Date(Date.now() + 30_000).toISOString(),
            estimate: {
              src: { chain_id: b.src_chain_id, token: src.address, symbol: src.symbol, decimals: src.decimals, amount: String(b.amount) },
              out_units: out.toString(),
              min_out_units: minOut.toString(),
              out_groth: Number((out * 100000000n) / 10n ** BigInt(target.decimals)),
              usd: usdOf(amount, src.decimals),
              eta_s: 12 * 12 + 120,
              price_impact_bps: this.uniswapPriceImpactBps,
            },
            // the swap is the user's own: it is issued armed or not, it lands in their wallet, and
            // it is never registered as a deposit
            swap_tx: { chain_id: 1, to: UNIVERSAL_ROUTER, data: '0xu2swap', value: '0' },
            next: { src_chain_id: 1, src_token: NATIVE, amount: minOut.toString() },
            // T46: the SHIPPED two-step `route` — Uniswap's own router, Permit2, and the canonical
            // hook-less pool the swap happens on. No `hook`, no gateway: nothing of ours is deployed.
            route: {
              router: UNIVERSAL_ROUTER,
              permit2: PERMIT2,
              pool_id: INNER_POOL_ID,
              pool_key: { currency0: NATIVE, currency1: src.address, fee: 3000, tickSpacing: 60, hooks: NATIVE },
              token_in: src.address,
              token_out: target.token,
              fee: 3000,
              symbol: src.symbol,
            },
          };
          // ── T46 BLOCK: the approvals, in the order they must be sent ───────────────────────
          // The API decides WHICH of them exist (it read both allowances); the client sends what it
          // is handed, in order. `approval_reset` leads when the token holds a short but non-zero
          // allowance, because `approve` on a USDT-style token reverts otherwise.
          const approvals: Record<string, unknown>[] = [];
          if (!this.uniswapAllowanceCovers) {
            if (this.uniswapNeedsAllowanceReset)
              approvals.push({
                name: 'approval_reset',
                chain_id: 1,
                to: src.address,
                data: erc20ApproveData(PERMIT2, 0n),
                value: '0',
                token: src.address,
                spender: PERMIT2,
                amount: '0',
              });
            approvals.push({
              name: 'approval',
              chain_id: 1,
              to: src.address,
              data: erc20ApproveData(PERMIT2, amount),
              value: '0',
              token: src.address,
              spender: PERMIT2,
              amount: String(amount),
            });
          }
          if (!this.uniswapPermitCovers)
            approvals.push({
              name: 'permit_tx',
              chain_id: 1,
              to: PERMIT2,
              data: permit2ApproveData(src.address, UNIVERSAL_ROUTER, amount, SWAP_DEADLINE),
              value: '0',
              token: src.address,
              spender: UNIVERSAL_ROUTER,
              amount: String(amount),
              expiration: SWAP_DEADLINE,
            });
          if (this.uniswapLegacyApprovalFields) {
            // the pre-2026-09-10-15:15Z shape: two named fields, no array, and the ERC-20 approve
            // carried its three arguments instead of calldata (the client built the bytes itself).
            // A reset had no name at all there — it simply could not be expressed.
            for (const a of approvals) {
              if (a.name === 'approval') two.approval = { chain_id: 1, token: a.token, spender: a.spender, amount: a.amount };
              if (a.name === 'permit_tx')
                two[this.uniswapLegacyPermitField ? 'permit_fallback_tx' : 'permit_tx'] = {
                  chain_id: 1,
                  to: a.to,
                  data: a.data,
                  value: '0',
                };
            }
          } else {
            two.approvals = approvals;
          }
          if (!this.armed) two.note = 'ingress not armed: no Beam pubkey configured';
          return this.json(route, 200, two);
        }
        /**
         * The deployed-hook shape, one transaction (`PGAS_UNISWAP_HOOK_ENABLED=1`). With the
         * two-step route on, native ETH gets here and must NOT be answered with it: nothing is
         * swapped, so it is a plain direct deposit, and the branch below writes exactly that.
         */
        if (this.uniswapTwoStep) {
          // fall out of the uniswap branch entirely — `mode` is already `direct` for native ETH
        } else {
          // nothing is swapped when the input already IS the output — the hook only locks it, so that
          // route has no price impact and no slippage bound to state
          const swapped = !native;
          const out = swapped ? (toTarget(amount) * 997n) / 1000n : amount;
          if (this.minDepositWei !== null && out < this.minDepositWei)
            return this.json(route, 400, { detail: `below the minimum deposit of ${Number(this.minDepositWei) / 1e18} ETH` });
          const minOut = swapped ? (out * BigInt(10000 - this.uniswapSlippageBps)) / 10000n : out;
          const fee = out / 1000n;
          const quoteId = 'q-' + Math.random().toString(36).slice(2, 8);
          const uni: Record<string, unknown> = {
            quote_id: quoteId,
            mode: 'uniswap',
            target_asset: target.key,
            armed: this.armed,
            expires_at: new Date(Date.now() + 30_000).toISOString(),
            deposit_ref: '0x' + (++this.uniswapRefSeq).toString(16).padStart(64, '0'),
            route: {
              hook: PGAS_HOOK,
              router: PGAS_ROUTER,
              gateway_pool_id: GATEWAY_POOL_ID,
              inner_pool_id: INNER_POOL_ID,
              token_in: src.address,
              token_out: target.token,
            },
            estimate: {
              src: { chain_id: b.src_chain_id, token: src.address, symbol: src.symbol, decimals: src.decimals, amount: String(b.amount) },
              out_units: out.toString(),
              min_out_units: minOut.toString(),
              value_units: (out - fee).toString(),
              relayer_fee_units: fee.toString(),
              out_groth: Number((out * 100000000n) / 10n ** BigInt(target.decimals)),
              usd: usdOf(amount, src.decimals),
              eta_s: 12 * 12 + 120,
              price_impact_bps: swapped ? this.uniswapPriceImpactBps : 0,
            },
          };
          if (this.armed) {
            uni.tx = { chain_id: 1, to: PGAS_ROUTER, data: '0xc0ffee00', value: native ? String(amount) : '0' };
            // one approval, and its spender is the router the deposit tx goes to
            if (!native) uni.approval = { chain_id: 1, token: src.address, spender: PGAS_ROUTER, amount: String(amount) };
          } else uni.note = 'ingress not armed: no Beam pubkey configured';
          // remembered only so the registered deposit row carries `mode:"uniswap"`, as the API's does
          this.quotes[quoteId] = { mode: 'uniswap' };
          return this.json(route, 200, uni);
        }
      }
      const outUnits = mode === 'direct' ? amount : ((isTarget ? amount : toTarget(amount)) * 98n) / 100n;
      // routers/quote.py check_min_deposit(): 400 before anything else is built or stored
      if (this.minDepositWei !== null && outUnits < this.minDepositWei)
        return this.json(route, 400, { detail: `below the minimum deposit of ${Number(this.minDepositWei) / 1e18} ETH` });
      const relayerFee = outUnits / 1000n;
      const estimate: Record<string, unknown> = {
        src: { chain_id: b.src_chain_id, token: src.address, symbol: src.symbol, decimals: src.decimals, amount: String(b.amount) },
        out_units: outUnits.toString(),
        out_groth: Number((outUnits * 100000000n) / 10n ** BigInt(target.decimals)),
        usd: usdOf(amount, src.decimals),
        eta_s: mode === 'direct' ? 12 * 12 + 120 : 420,
      };
      const quoteId = 'q-' + Math.random().toString(36).slice(2, 8);
      const quote: Record<string, unknown> = {
        quote_id: quoteId,
        target_asset: target.key,
        mode: mode === 'xchain' ? this.xchainWire : mode,
        armed: this.armed,
        expires_at: new Date(Date.now() + 30_000).toISOString(),
        estimate,
      };
      if (mode === 'swap') {
        // the swap lands in the user's OWN wallet, so it is issued armed or not; never a deposit
        estimate.route_fees = { protocolFee: '1000000000000000' };
        quote.swap_tx = { chain_id: 1, to: ROUTER_ORDER, data: '0xfeedface', value: native ? String(amount) : '0' };
        if (!native) quote.approval = { chain_id: 1, token: src.address, spender: ROUTER_ALLOWANCE_TARGET, amount: String(amount) };
        quote.next = { src_chain_id: 1, src_token: target.token, amount: outUnits.toString() };
        return this.json(route, 200, quote);
      }
      estimate.value_units = (outUnits - relayerFee).toString();
      estimate.relayer_fee_units = relayerFee.toString();
      // T2b (2026-09-09): a cross-chain quote is ONE router call and carries no tx — the hook order
      // (and its approval and order id) is built by POST /v1/quote/{id}/arm on the Deposit click.
      if (mode === 'xchain') {
        estimate.route_fees = { protocolFee: '1000000000000000' };
        this.quotes[quoteId] = {
          mode,
          srcChainId: Number(b.src_chain_id),
          srcToken: src.address,
          amount: String(amount),
          native,
          estimate,
        };
      }
      if (this.armed) {
        if (mode === 'direct') {
          quote.tx = { chain_id: 1, to: target.pipe, data: '0xdeadbeef', value: native ? String(amount) : '0' };
          if (!native) quote.approval = { chain_id: 1, token: src.address, spender: target.pipe, amount: String(amount) };
        }
      } else quote.note = 'ingress not armed: no Beam pubkey configured';
      return this.json(route, 200, quote);
    }
    const armPath = /^\/quote\/([^/]+)\/arm$/.exec(path);
    if (method === 'POST' && armPath) {
      const q = this.quotes[armPath[1]];
      if (!q) return this.json(route, 404, { detail: 'unknown quote' });
      // a uniswap quote is built armed and has no /arm step: asking is a client bug, said out loud
      if (q.mode !== 'xchain') return this.json(route, 409, { detail: `a ${q.mode} quote carries its transaction already` });
      if (this.armExpired) return this.json(route, 409, { detail: 'the quote expired — refresh it' });
      if (!this.armed) return this.json(route, 409, { detail: 'ingress not armed: no Beam pubkey configured' });
      this.armCalls.push(armPath[1]);
      // idempotent: a second call returns the order already stored on the quote, drift and all
      if (!q.armedResult) {
        const shown = BigInt(String(q.estimate.out_units));
        const out = shown + (shown * BigInt(Math.round(this.armDriftPct * 100))) / 10000n;
        q.armedResult = {
          quote_id: armPath[1],
          tx: { chain_id: q.srcChainId, to: ROUTER_ORDER, data: '0xdeadbeef', value: q.native ? q.amount : '0' },
          ...(q.native ? {} : { approval: { chain_id: q.srcChainId, token: q.srcToken, spender: ROUTER_ORDER, amount: q.amount } }),
          order_id: '0x' + '77'.repeat(32),
          estimate: { ...q.estimate, out_units: out.toString() },
          expires_at: new Date(Date.now() + 30_000).toISOString(),
        };
      }
      return this.json(route, 200, q.armedResult);
    }
    if (method === 'POST' && path === '/deposits') {
      // T31 H: the refusals the client must survive without the user pressing anything
      if (this.registerFailures > 0) {
        this.registerFailures--;
        return this.json(route, this.registerFailStatus, { detail: this.registerFailDetail });
      }
      const id = 'dep-' + (this.deposits.length + 1);
      const from = this.quotes[String(b.quote_id)];
      this.deposits.unshift({
        _id: id,
        asset: 'ETH',
        ...(from?.mode ? { mode: from.mode } : {}),
        status: 'submitted',
        src: { chain_id: 1, token: NATIVE, amount: '200000000000000000' },
        quote_id: b.quote_id,
        src_tx_hash: b.src_tx_hash,
        verified: this.registerVerified,
        eth: {},
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
      });
      return this.json(route, 200, {
        deposit_id: id,
        status: 'submitted',
        ...(this.registerVerified ? {} : { verified: false, note: 'that transaction is not visible on Ethereum yet' }),
      });
    }
    if (method === 'GET' && path === '/withdrawals/fees') {
      if (!locked()) return;
      // the 2026-09-10 shape: our cut, the technical floor, what a crossing costs now, and the
      // curve the wait is charged against
      const points = [0, 86400, 7 * 86400, this.maxWindowS];
      return this.json(route, 200, {
        fee_bps: this.feeBps,
        min_amount_groth: this.minAmountGroth,
        bridge_fee_groth_now: this.bridgeFeeGrothNow,
        headroom: points.map((window_s) => ({ window_s, factor: Number(this.headroomFor(window_s).toFixed(4)) })),
        bridge_eta_s: this.bridgeEtaS,
      });
    }
    if (method === 'POST' && path === '/withdrawals/preview') {
      if (!locked()) return;
      const n = this.calls.filter((c) => c.path === '/withdrawals/preview').length - 1;
      const delay = this.previewDelaysMs[n] ?? 0;
      if (delay > 0) await new Promise((r) => setTimeout(r, delay));
      // a fee that cannot be read is a refusal, never a guess and never the last one (law 4)
      if (this.previewFail) return this.json(route, this.previewFail.status, { detail: this.previewFail.detail });
      const priced = this.priceBatch((b.items ?? []) as unknown[]);
      this.previewResponses.push(priced);
      // nothing is written: no rows, no reservation, no ledger — it is a question, not an order
      return this.json(route, 200, priced);
    }
    if (method === 'POST' && path === '/withdrawals') {
      // API_CONTRACT.md § Withdrawals (restructured 2026-09-09): a list of orders, each with its own
      // delivery time. No destination registry — W is whatever the user typed, checksummed.
      if (b.mode !== 'direct' || !this.directEnabled) return this.json(route, 409, { detail: `${b.mode} mode is not enabled yet` });
      const raw = (b.items ?? []) as { W: string; amount_groth: number; deliver_at: number }[];
      if (!raw.length) return this.json(route, 400, { detail: 'items must not be empty' });
      // the SAME pricing the preview showed — one implementation, so the quote is the charge
      const priced = this.priceBatch(raw);
      // ⛔ EXCEPT THE ROW THAT RAN OUT OF MONEY (`_refuse_items`): `problem_code: "batch"` is a
      // verdict about the LIST, refused below as one unit with the shortfall — a 422 would tell
      // the user their third wallet is wrong when the only thing wrong is the balance.
      const bad = priced.items
        .map((i, n) => [n, i] as const)
        // ⛔ AND `treasury_float` IS LEFT TO THE BATCH TOO (T52): that row is not wrong at all —
        // WE are short — so it is refused as one unit with the sentence, never as a 422 about
        // the user's third wallet.
        .filter(([, i]) => !i.ok && i.problem_code !== 'batch' && i.problem_code !== 'treasury_float');
      if (bad.length) {
        // 422 naming EVERY item that cannot be scheduled, in the shape `preview` returns them, so
        // the client renders a refusal with the code that renders a quote. `detail` is a DICT: the
        // sentence in `message`, the structured halves beside it — never a blob of JSON to print.
        const problems = bad.map(([n, i]) => `item ${n + 1}: ${i.problem}`).join('; ');
        return this.json(route, 422, {
          detail: {
            message:
              `${bad.length} of ${priced.items.length} item(s) cannot be scheduled (${problems}) — ` +
              'nothing was scheduled and nothing was debited',
            items: priced.items,
            min_amount_groth: priced.min_amount_groth,
          },
        });
      }
      // ⛔ THE TREASURY FIRST (T52): both refusals can be true at once and only one of them can
      // be acted on — topping up does not make a locked treasury liquid.
      if (priced.treasury && !priced.treasury.ok) return this.json(route, 409, { detail: priced.treasury.problem });
      if (!priced.batch.ok)
        // the batch rule alone: the verdict `preview` published, with its number in a header a UI
        // can read without parsing English
        return this.json(route, 409, { detail: priced.batch.problem }, { 'X-Shortfall-Groth': String(priced.batch.shortfall_groth) });
      const now = this.now();
      const out = priced.items.map((it, k) => {
        const request_id = `req-new-${++this.requestSeq}`;
        const deliver_at = Number(raw[k].deliver_at);
        this.requests.unshift({
          _id: request_id,
          asset: 'ETH',
          mode: 'direct',
          W: it.W,
          // ⛔ WHAT LEAVES TO THE USER, exactly as `_write_items` stores it: `amount_groth` on a
          // payout ROW is the DELIVERY (the release spends this field), and what was typed lives
          // beside it as `requested_groth`. The mock used to store the typed figure here, which
          // is how "of {amount_groth} asked" could print the delivered number twice and look right.
          amount_groth: this.publishFeeSplit ? (it.delivered_groth ?? it.amount_groth) : it.amount_groth,
          fee_groth: it.fee_groth,
          bridge_fee_groth: it.bridge_fee_groth,
          deliver_at,
          release_at: it.release_at,
          status: 'scheduled',
          created_at: now,
          // ── T35/T40 block ─────────────────────────────────────────────────────────────────
          // the row carries what was decided at scheduling (T35) and the ETA the order machine
          // publishes for it (T40 `eta_for`: a scheduled order arrives at `deliver_at`).
          ...(this.publishFeeSplit
            ? {
                requested_groth: it.amount_groth,
                delivered_groth: it.delivered_groth,
                debited_groth: it.debited_groth,
                fee_mode: it.fee_mode,
              }
            : {}),
          eta_at: deliver_at,
          eta_note: 'your delivery time — it goes to the bridge about an hour before',
          // ──────────────────────────────────────────────────────────────────────────────────
        });
        return {
          request_id,
          W: it.W,
          amount_groth: it.amount_groth,
          fee_groth: it.fee_groth,
          bridge_fee_groth: it.bridge_fee_groth,
          total_groth: it.total_groth,
          deliver_at,
          release_at: it.release_at,
          // ── T35 block ─────────────────────────────────────────────────────────────────────
          ...(this.publishFeeSplit ? { delivered_groth: it.delivered_groth, debited_groth: it.debited_groth, fee_mode: it.fee_mode } : {}),
          // ──────────────────────────────────────────────────────────────────────────────────
        };
      });
      // the ledger debit is amount + our fee + the bridge fee
      this.balances.ETH.available -= priced.totals.total_debited_groth;
      this.balances.ETH.scheduled += priced.totals.total_debited_groth;
      return this.json(route, 200, {
        request_ids: out.map((o) => o.request_id),
        fee_groth: priced.totals.fee_groth,
        bridge_fee_groth: priced.totals.bridge_fee_groth,
        total_debited_groth: priced.totals.total_debited_groth,
        relayer_fee_groth_estimate: this.bridgeFeeGrothNow,
        min_amount_groth: this.minAmountGroth,
        // ── T35 block: the same totals the preview showed, delivered total included ──────────
        totals: priced.totals,
        items: out,
      });
    }
    const cancelPath = /^\/withdrawals\/([^/]+)\/cancel$/.exec(path);
    if (method === 'POST' && cancelPath) {
      const row = this.requests.find((r) => r._id === cancelPath[1]);
      if (!row) return this.json(route, 404, { detail: SAY.UNKNOWN_REQUEST });
      // ── T40 block ─────────────────────────────────────────────────────────────────────────
      // Cancel is the user's own end for an order the system still holds: `scheduled`, and now
      // `delayed`/`held` too — money that is being retried is still reserved, and the user must
      // be able to take it back (API_CONTRACT.md § Withdrawals, "never fails on the user's side").
      // Anything the bridge or Ethereum has touched is refused, in the API's own words.
      // ──────────────────────────────────────────────────────────────────────────────────────
      if (!['scheduled', 'delayed', 'held'].includes(String(row.status)))
        return this.json(route, 409, { detail: SAY.cancelTooLate(String(row.status)) });
      row.status = 'cancelled';
      const refunded =
        Number(row.debited_groth ?? Number(row.amount_groth) + Number(row.fee_groth) + Number(row.bridge_fee_groth ?? 0)) || 0;
      this.balances.ETH.available += refunded;
      this.balances.ETH.scheduled -= refunded;
      // the ledger's own evidence that the money went back — what turns a legacy `failed` row
      // into "Refunded" on the Balance page
      this.history.unshift({
        kind: 'cancel',
        groth: refunded,
        d_avail: refunded,
        d_sched: -refunded,
        d_sent: 0,
        ref: row._id,
        at: this.now(),
      });
      return this.json(route, 200, { cancelled: row._id, refunded_groth: refunded });
    }
    return this.json(route, 404, { detail: `no mock for ${method} ${path}` });
  }
}

/**
 * Blocks every request that leaves 127.0.0.1 (RPCs, CoinGecko, fonts unless allowed). With
 * `allowAssets` the Google Fonts CSS is fetched for real and the token logos the token list points
 * at (LOGO_HOST) are served locally, so a screenshot shows the real <img> logos without depending
 * on anyone's CDN. Chain icons need nothing here: they are this app's own files under /chains/,
 * same origin, and always load.
 */
export async function blockExternal(page: Page, allowAssets = false) {
  await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, (route) => {
    const url = route.request().url();
    if (allowAssets && /fonts\.(googleapis|gstatic)\.com/.test(url)) return route.continue();
    if (allowAssets && url.startsWith(`${LOGO_HOST}/`) && url.endsWith('.svg')) {
      const hue = [...url].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
      return route.fulfill({
        status: 200,
        contentType: 'image/svg+xml',
        body: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="16" fill="hsl(${hue} 70% 52%)"/></svg>`,
      });
    }
    return route.abort('blockedbyclient');
  });
}

// The first FALLBACK_RPCS url of each mocked chain (src/lib/chains.ts): the scanner tries it first,
// so answering it is enough to make the chain readable.
const RPC_URLS: Record<number, string> = {
  1: 'https://ethereum-rpc.publicnode.com',
  42161: 'https://arb1.arbitrum.io/rpc',
  8453: 'https://mainnet.base.org',
  1514: 'https://mainnet.storyrpc.io',
  25: 'https://evm.cronos.org',
};

const BATCH_IFACE = new Interface([
  'function balanceFor(address[] _tokens, address _account) view returns (uint256[] balances, uint256[] decimals)',
]);
const MC3_IFACE = new Interface([
  'function aggregate3((address target, bool allowFailure, bytes callData)[] calls) payable returns ((bool success, bytes returnData)[] returnData)',
]);
const CODER = AbiCoder.defaultAbiCoder();

export interface ChainHoldings {
  native: bigint;
  tokens: Record<string, bigint>;
}

/**
 * A public-RPC double: eth_chainId / eth_getBalance / eth_call for the batch-balance helper,
 * Multicall3's aggregate3 and plain balanceOf. Install it AFTER blockExternal so it wins the route.
 */
export class MockRpc {
  constructor(public holdings: Record<number, ChainHoldings> = {}) {}

  async install(page: Page) {
    for (const [id, url] of Object.entries(RPC_URLS)) {
      await page.route(url, (route) => this.handle(Number(id), route));
    }
  }

  private handle(chainId: number, route: Route) {
    let body: unknown;
    try {
      body = JSON.parse(route.request().postData() ?? 'null');
    } catch {
      body = null;
    }
    const one = (r: { id?: unknown; method?: string; params?: unknown[] }) => {
      const id = r?.id ?? 1;
      try {
        return { jsonrpc: '2.0', id, result: this.answer(chainId, String(r?.method), (r?.params ?? []) as unknown[]) };
      } catch (e) {
        return { jsonrpc: '2.0', id, error: { code: -32000, message: (e as Error).message } };
      }
    };
    const out = Array.isArray(body) ? body.map(one) : one(body as never);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(out) });
  }

  private answer(chainId: number, method: string, params: unknown[]): unknown {
    const h = this.holdings[chainId] ?? { native: 0n, tokens: {} };
    const bal = (token: string) => h.tokens[token.toLowerCase()] ?? 0n;
    switch (method) {
      case 'eth_chainId':
        return '0x' + chainId.toString(16);
      case 'net_version':
        return String(chainId);
      case 'eth_blockNumber':
        return '0x10';
      case 'eth_getBalance':
        return '0x' + h.native.toString(16);
      case 'eth_call': {
        const tx = params[0] as { to?: string; data?: string };
        const data = String(tx?.data ?? '0x');
        const selector = data.slice(0, 10);
        if (selector === MC3_IFACE.getFunction('aggregate3')!.selector) {
          const [calls] = MC3_IFACE.decodeFunctionData('aggregate3', data);
          const items = (calls as unknown as [string, boolean, string][]).map((c) => [true, CODER.encode(['uint256'], [bal(c[0])])]);
          return MC3_IFACE.encodeFunctionResult('aggregate3', [items]);
        }
        if (selector === BATCH_IFACE.getFunction('balanceFor')!.selector) {
          const [tokens] = BATCH_IFACE.decodeFunctionData('balanceFor', data);
          const list = tokens as unknown as string[];
          return BATCH_IFACE.encodeFunctionResult('balanceFor', [
            list.map((t) => bal(t)),
            list.map((t) => BigInt(TOKENS.find((x) => x.address.toLowerCase() === t.toLowerCase())?.decimals ?? 18)),
          ]);
        }
        return CODER.encode(['uint256'], [bal(String(tx?.to ?? ''))]);
      }
      default:
        throw new Error(`mock rpc: unsupported ${method}`);
    }
  }
}

const USD = {
  ethereum: 4000,
  'story-2': 3.2,
  'crypto-com-chain': 0.12,
  [USDC.toLowerCase()]: 1,
  [DAI.toLowerCase()]: 1,
  [WBTC.toLowerCase()]: 100000,
} as Record<string, number>;

/** A wallet with something on every mocked chain, including the two with no batch-balance helper. */
export const DEMO_HOLDINGS: Record<number, ChainHoldings> = {
  1: {
    native: 250000000000000000n, // 0.25 ETH
    tokens: { [USDC.toLowerCase()]: 1834500000n, [DAI.toLowerCase()]: 402000000000000000000n, [WBTC.toLowerCase()]: 3400000n },
  },
  42161: { native: 120000000000000000n, tokens: { [USDC.toLowerCase()]: 640120000n } },
  8453: { native: 45000000000000000n, tokens: { [USDC.toLowerCase()]: 96000000n, [DAI.toLowerCase()]: 12500000000000000000n } },
  1514: { native: 1500000000000000000000n, tokens: { [USDC.toLowerCase()]: 25000000n } },
  25: { native: 8200000000000000000000n, tokens: { [USDC.toLowerCase()]: 310000000n, [DAI.toLowerCase()]: 88000000000000000000n } },
};

/** CoinGecko double so the chips carry USD labels offline. Install after blockExternal. */
export async function installMockPrices(page: Page) {
  await page.route(/api\.coingecko\.com/, (route) => {
    const url = new URL(route.request().url());
    const keys = (url.searchParams.get('ids') ?? url.searchParams.get('contract_addresses') ?? '').split(',').filter(Boolean);
    const body: Record<string, { usd: number }> = {};
    for (const k of keys) if (USD[k.toLowerCase()] !== undefined) body[k] = { usd: USD[k.toLowerCase()] };
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

// ---------------------------------------------------------------------------
// Injected wallets: one factory, one `profile` per real wallet
// ---------------------------------------------------------------------------
//
// Every wallet Pgas.me is meant to work in is a different EIP-1193 implementation, and the ways
// they differ are not cosmetic — they are what breaks a dapp. So the mock is not "a wallet": it is
// a profile carrying a real wallet's identity (its EIP-6963 name/rdns/icon, or the `window.*`
// global it sets and the `is…` flag it raises) and the behaviours that wallet is known for.
//
// The quirks below are each a thing a shipped wallet does, not an invented edge case:
//   lowercase           — the accounts come back unchecksummed (Trust, several in-app browsers)
//   utf8Sign            — personal_sign refuses the EIP-191 hex message with -32602 (Coin98, Binance)
//   reversedSignParams  — personal_sign wants [address, message] (in-app browsers)
//   unknownChain        — wallet_switchEthereumChain answers 4902 until the chain has been added
//   strictChainString   — the chainId parameter is looked up as TEXT, never parsed (2026-09-10)
//   rejectSwitch        — the user presses Cancel: 4001
//   emptyAccounts       — a locked wallet answers eth_requestAccounts with [] instead of throwing
//   zeroBalance         — the wallet's own RPC answers 0 for a balance that is not 0 (Zerion)
//   spuriousDisconnect  — `disconnect` is emitted for an RPC hiccup, with the account still there

export type WalletQuirk =
  | 'lowercase'
  | 'utf8Sign'
  | 'reversedSignParams'
  | 'unknownChain'
  | 'strictChainString'
  | 'rejectSwitch'
  | 'emptyAccounts'
  | 'zeroBalance'
  | 'spuriousDisconnect';
// (T31b item 9, 2026-09-10: the two `eth_signTypedData_v4` quirks are gone with the prompt. The
// two-step Uniswap route asks this wallet for transactions only, so a wallet that cannot sign
// typed data is no longer a case this app has — there is nothing for it to fail at.)

export interface MockWalletProfile {
  /** The handle name: `window.__wallets[key]`. */
  key: string;
  /** The name the wallet gives itself — EIP-6963 `info.name`, or the one our flag detection prints. */
  name: string;
  /** Announce over EIP-6963 under this rdns. Absent → legacy detection only. */
  rdns?: string;
  /** The icon's fill, so the picker screenshot has real, distinguishable tiles. */
  color?: string;
  /** `isMetaMask`, `isTrust`, … exactly as the wallet raises them. */
  flags?: Record<string, boolean>;
  /** Dotted path under `window`: 'ethereum', 'coin98.provider', 'bitkeep.ethereum', 'BinanceChain'. */
  global?: string;
  /** Goes into `window.ethereum.providers[]` — the MetaMask/Coinbase coexistence array. */
  inProviders?: boolean;
  accounts?: string[];
  chainId?: number;
  /**
   * The chains this wallet HOLDS, the way a real install holds a list. Absent means "every chain
   * the app can ask for", which is what every profile written before 2026-09-10 assumed; give it a
   * list to exercise a chain the wallet genuinely does not have (→ 4902 → wallet_addEthereumChain).
   */
  knownChains?: number[];
  quirks?: WalletQuirk[];
  /** Announce this late (ms). 0/undefined = with the first `eip6963:requestProvider`. */
  announceDelayMs?: number;
}

/** The default pair the rest of the suite drives: an announced wallet, a second one, a legacy Coin98. */
export const DEFAULT_PROFILES: MockWalletProfile[] = [
  { key: 'mock', name: 'Mock Wallet', rdns: 'me.pgas.mock', color: '#0E8F86', flags: { isMetaMask: true }, global: 'ethereum' },
  { key: 'mock2', name: 'Mock Wallet 2', rdns: 'me.pgas.mock2', color: '#4A63E7', flags: { isMetaMask: true }, accounts: ['B'] },
  { key: 'coin98legacy', name: 'Coin98', color: '#D9B432', flags: { isCoin98: true }, global: 'coin98.provider' },
];

/**
 * The eight wallets that announce themselves over EIP-6963, with the rdns each really publishes.
 * Quirks are assigned to the wallet that actually has them, so a per-wallet journey exercises the
 * handling that wallet needs rather than a generic happy path twelve times.
 */
export const ANNOUNCED_PROFILES: MockWalletProfile[] = [
  { key: 'metamask', name: 'MetaMask', rdns: 'io.metamask', color: '#F6851B', flags: { isMetaMask: true }, inProviders: true },
  { key: 'zerion', name: 'Zerion', rdns: 'io.zerion.wallet', color: '#2962EF', flags: { isZerion: true }, quirks: ['zeroBalance'] },
  { key: 'rabby', name: 'Rabby Wallet', rdns: 'io.rabby', color: '#7084FF', flags: { isRabby: true }, quirks: ['unknownChain'] },
  {
    key: 'trust',
    name: 'Trust Wallet',
    rdns: 'com.trustwallet.app',
    color: '#0500FF',
    flags: { isTrust: true, isTrustWallet: true },
    quirks: ['lowercase'],
  },
  {
    key: 'okx',
    name: 'OKX Wallet',
    rdns: 'com.okex.wallet',
    color: '#161A22',
    flags: { isOkxWallet: true },
    quirks: ['spuriousDisconnect'],
  },
  {
    key: 'coinbase',
    name: 'Coinbase Wallet',
    rdns: 'com.coinbase.wallet',
    color: '#0052FF',
    flags: { isCoinbaseWallet: true },
    inProviders: true,
  },
  { key: 'phantom', name: 'Phantom', rdns: 'app.phantom', color: '#AB9FF2', flags: { isPhantom: true }, global: 'phantom.ethereum' },
  {
    key: 'coin98',
    name: 'Coin98 Wallet',
    rdns: 'coin98.com',
    color: '#D9B432',
    flags: { isCoin98: true },
    quirks: ['utf8Sign'],
  },
];

/**
 * The wallets that never announce: they are found only by the global they set. Coin98 is here AND
 * in the announced list on purpose — it does both, and one install must still be one row.
 */
export const LEGACY_PROFILES: MockWalletProfile[] = [
  { key: 'coin98global', name: 'Coin98', color: '#D9B432', flags: { isCoin98: true }, global: 'coin98.provider', quirks: ['utf8Sign'] },
  {
    key: 'binance',
    name: 'Binance Web3 Wallet',
    color: '#F0B90B',
    flags: { isBinance: true },
    global: 'BinanceChain',
    quirks: ['utf8Sign'],
  },
  { key: 'bitget', name: 'Bitget Wallet', color: '#1DA2B4', flags: { isBitKeep: true, isBitget: true }, global: 'bitkeep.ethereum' },
  {
    key: 'tokenpocket',
    name: 'TokenPocket',
    color: '#2980FE',
    flags: { isTokenPocket: true, isMetaMask: true },
    inProviders: true,
    quirks: ['reversedSignParams'],
  },
];

/** Everything at once: what a browser with every one of these installed would look like. */
export const ALL_PROFILES: MockWalletProfile[] = [...ANNOUNCED_PROFILES, ...LEGACY_PROFILES];

/**
 * The wallet from the 2026-09-10 bug report — "When I click Deposit, you ask me to Add ETH Network,
 * but I have it." — deliberately NOT in ALL_PROFILES: it is a behaviour family, not a twelfth
 * install, and the picker tests count installs.
 *
 * WHICH shipped wallet it was is not known (the admin was on a real wallet on pgas.me and said what
 * it did, not what it was). What IS known is the behaviour, and the behaviour is the thing that
 * breaks a dapp: `wallet_switchEthereumChain` is answered by comparing the chainId string it was
 * handed against the ones it holds, with no EIP-3326 validation. So a padded "0x01" is not refused
 * as invalid — it is simply not found, the wallet answers 4902, and the dapp's own "your wallet does
 * not know this chain" branch offers to add Ethereum mainnet to a wallet that has always had it.
 * It starts on BNB Smart Chain so a switch to Ethereum is actually asked for.
 */
export const STRICT_CHAIN_PROFILE: MockWalletProfile = {
  key: 'strictchain',
  name: 'String-Compare Wallet',
  rdns: 'me.pgas.strictchain',
  color: '#8A5CF6',
  flags: { isMetaMask: true },
  global: 'ethereum',
  chainId: 56,
  knownChains: [1, 56, 42161, 8453],
  quirks: ['strictChainString'],
};

/**
 * Installs one EIP-1193 provider per profile. Each gets a handle at `window.__wallets[key]`:
 *   .state       { accounts, chainId, sent[], signCalls[][], signed, switchCalls[], addCalls[],
 *                  addedChains[], addedChainStrings[], locked }
 *   .setAccounts(list)  → emits accountsChanged
 *   .setChain(id)       → emits chainChanged (the wallet doing it, not us asking)
 *   .hiccup()           → emits `disconnect` with the account still connected
 *   .unlock()           → a locked wallet grants its account
 */
export async function installWallets(page: Page, profiles: MockWalletProfile[] = DEFAULT_PROFILES, wallets: Wallet[] = [walletA, walletB]) {
  const byAddress = new Map(wallets.map((w) => [w.address.toLowerCase(), w]));
  await page.exposeFunction('__mockSign', async (hexMessage: string, address: string) => {
    const w = byAddress.get(String(address).toLowerCase());
    if (!w) throw new Error(`no key for ${address}`);
    return w.signMessage(getBytes(hexMessage));
  });
  const specs = profiles.map((p) => ({
    ...p,
    accounts: (p.accounts ?? ['A']).map((a) => (a === 'A' ? wallets[0].address : a === 'B' ? wallets[1].address : a)),
    chainId: p.chainId ?? 1,
    quirks: p.quirks ?? [],
    flags: p.flags ?? {},
    color: p.color ?? '#0E8F86',
  }));
  await page.addInitScript(
    ({ specs, tx }) => {
      const w = window as any;
      const handles: Record<string, any> = {};
      w.__wallets = handles;
      const enc = new TextEncoder();
      const toHex = (v: unknown) =>
        typeof v === 'string' && /^0x[0-9a-fA-F]*$/.test(v) && v.length % 2 === 0
          ? v
          : '0x' +
            Array.from(enc.encode(String(v)))
              .map((b) => b.toString(16).padStart(2, '0'))
              .join('');
      const rpcErr = (code: number, message: string) => Object.assign(new Error(message), { code });

      const make = (spec: any) => {
        const listeners: Record<string, Array<(...a: unknown[]) => void>> = {};
        const q = (name: string) => spec.quirks.includes(name);
        const state = {
          accounts: spec.accounts as string[],
          chainId: spec.chainId as number,
          sent: [] as any[],
          signCalls: [] as any[][],
          signed: 0,
          addedChains: [] as number[],
          /** The chainId STRINGS the adds arrived with — what a string-comparing wallet remembers. */
          addedChainStrings: [] as string[],
          addCalls: [] as any[],
          switchCalls: [] as any[],
          /** every `eth_signTypedData_v4` this wallet was handed, in the shape it was handed */
          /** every `eth_signTypedData_v4` this wallet was handed; the deposit flow must hand it none */
          typedDataCalls: [] as any[][],
          locked: q('emptyAccounts'),
        };
        const emit = (ev: string, ...args: unknown[]) => (listeners[ev] ?? []).forEach((l) => l(...args));
        const accountsOut = () => (state.locked ? [] : q('lowercase') ? state.accounts.map((a) => a.toLowerCase()) : state.accounts);

        // ---- the chain id on the wire (2026-09-10) ----
        // EIP-695, EIP-3085 and EIP-3326 all specify ONE form: 0x-prefixed, unpadded, non-zero hex.
        // This is MetaMask's own test for it, verbatim. Until 2026-09-10 this mock read the parameter
        // with a bare parseInt(), which accepts "0x01" happily — so it shipped a real bug: the app
        // sent ethers' toBeHex(1) = "0x01", the admin's wallet did not recognise it, and the app
        // offered to ADD Ethereum to a wallet that already had it. A mock that parses what real
        // wallets validate is a mock that certifies the bug.
        const CANONICAL_CHAIN_ID = /^0x[1-9a-f]+[0-9a-f]*$/i;
        const chainHex = (n: number) => '0x' + n.toString(16);
        /** MetaMask's wording, because 'unpadded' is the word an operator will grep for. */
        const badChainId = (raw: unknown) =>
          rpcErr(-32602, `Expected 0x-prefixed, unpadded, non-zero hexadecimal string 'chainId'. Received:\n${String(raw)}`);
        /** The validating family: parse the id, then look it up among the chains this wallet holds. */
        const knowsNumber = (id: number) => {
          if (state.addedChains.includes(id)) return true;
          if (q('unknownChain')) return false; // knows nothing until the dapp adds it
          return spec.knownChains ? spec.knownChains.includes(id) : true;
        };
        /**
         * The string-comparing family — the one the admin was on. The parameter is never parsed:
         * it is matched as text against the ids this wallet holds, so a padded "0x01" is not
         * "invalid", it is simply *not found*, and the wallet answers 4902 exactly as it would for
         * a chain it has never heard of. That is the whole shape of the 2026-09-10 bug.
         */
        const knowsString = (raw: string) => {
          if (state.addedChainStrings.includes(raw)) return true;
          if (q('unknownChain')) return false;
          return spec.knownChains ? spec.knownChains.map((n: number) => chainHex(n)).includes(raw) : CANONICAL_CHAIN_ID.test(raw);
        };

        const provider: any = {
          ...spec.flags,
          async request({ method, params }: { method: string; params?: any[] }) {
            switch (method) {
              case 'eth_requestAccounts':
              case 'eth_accounts':
                return accountsOut();
              case 'eth_chainId':
                return chainHex(state.chainId);
              case 'net_version':
                return String(state.chainId);
              case 'wallet_switchEthereumChain': {
                const raw = String(params?.[0]?.chainId);
                state.switchCalls.push(params?.[0]);
                if (q('rejectSwitch')) throw rpcErr(4001, 'User rejected the request.');
                const strict = q('strictChainString');
                // The validating family never gets as far as the lookup: a padded id is bad params.
                if (!strict && !CANONICAL_CHAIN_ID.test(raw)) throw badChainId(raw);
                if (!(strict ? knowsString(raw) : knowsNumber(parseInt(raw, 16))))
                  throw rpcErr(4902, 'Unrecognized chain ID. Try adding the chain using wallet_addEthereumChain first.');
                state.chainId = parseInt(raw, 16);
                emit('chainChanged', chainHex(state.chainId));
                return null;
              }
              case 'wallet_addEthereumChain': {
                // MetaMask-family behaviour: the chain is added, the switch is a second call
                const raw = String(params?.[0]?.chainId);
                state.addCalls.push(params?.[0]);
                if (!q('strictChainString') && !CANONICAL_CHAIN_ID.test(raw)) throw badChainId(raw);
                state.addedChains.push(parseInt(raw, 16));
                // …and the string-comparing wallet remembers the TEXT it was given, so a dapp that
                // added chain 1 as "0x01" can then switch to "0x01" — the prompt was the whole cost.
                state.addedChainStrings.push(raw);
                return null;
              }
              case 'eth_getBalance':
                return '0x0'; // zeroBalance: what Zerion answered for a wallet that was not empty
              case 'eth_blockNumber':
                return '0x10';
              case 'eth_estimateGas':
                return '0x5208';
              case 'eth_getTransactionReceipt':
                return {
                  transactionHash: params?.[0],
                  transactionIndex: '0x0',
                  blockHash: '0x' + 'cd'.repeat(32),
                  blockNumber: '0xf',
                  from: state.accounts[0],
                  to: '0xeF4fB24aD0916217251F553c0596F8Edc630EB66',
                  cumulativeGasUsed: '0x5208',
                  gasUsed: '0x5208',
                  effectiveGasPrice: '0x3b9aca00',
                  gasPrice: '0x3b9aca00',
                  contractAddress: null,
                  logs: [],
                  logsBloom: '0x' + '00'.repeat(256),
                  status: '0x1',
                  type: '0x2',
                };
              case 'eth_call':
                throw rpcErr(-32000, 'execution reverted');
              /**
               * ⛔ NOT A METHOD THIS APP CALLS ANY MORE (T31b item 9). It is answered "method not
               * found" — the honest answer for a great many shipped wallets — and every call is
               * recorded, so a test can assert that the deposit flow asks for NO signature at all.
               */
              case 'eth_signTypedData_v4': {
                const [addr, payload] = params ?? [];
                state.typedDataCalls.push([addr, payload]);
                throw rpcErr(-32601, 'The method eth_signTypedData_v4 does not exist/is not available.');
              }
              case 'personal_sign': {
                const [a, b] = params ?? [];
                state.signCalls.push([a, b]);
                const isAddr = (v: unknown) => typeof v === 'string' && /^0x[0-9a-fA-F]{40}$/.test(v);
                if (q('reversedSignParams')) {
                  if (!isAddr(a)) throw rpcErr(-32602, 'Invalid parameters: the address must come first.');
                  state.signed++;
                  return w.__mockSign(toHex(b), a);
                }
                const looksHex = typeof a === 'string' && /^0x[0-9a-fA-F]+$/.test(a) && !isAddr(a);
                if (q('utf8Sign')) {
                  if (looksHex) throw rpcErr(-32602, 'invalid params: message must be a utf8 string');
                  state.signed++;
                  return w.__mockSign(toHex(a), b);
                }
                state.signed++;
                return w.__mockSign(toHex(a), b);
              }
              case 'eth_sendTransaction':
                state.sent.push(params?.[0]);
                return tx;
              default:
                throw rpcErr(4200, `mock: unsupported ${method}`);
            }
          },
          on(ev: string, l: (...a: unknown[]) => void) {
            (listeners[ev] = listeners[ev] ?? []).push(l);
          },
          removeListener(ev: string, l: (...a: unknown[]) => void) {
            listeners[ev] = (listeners[ev] ?? []).filter((x) => x !== l);
          },
        };
        return {
          provider,
          state,
          spec,
          setAccounts(list: string[]) {
            state.accounts = list;
            state.locked = false;
            emit('accountsChanged', q('lowercase') ? list.map((a) => a.toLowerCase()) : list);
          },
          setChain(id: number) {
            state.chainId = id;
            emit('chainChanged', chainHex(id));
          },
          /** An RPC hiccup: the wallet says `disconnect` and stays connected. */
          hiccup() {
            emit('disconnect', { code: 1013, message: 'network connection lost' });
          },
          /** The wallet was locked and the user unlocked it. */
          unlock() {
            state.locked = false;
          },
          /** The user locked the wallet: it still answers, with no accounts. */
          lock() {
            state.locked = true;
          },
        };
      };

      const setPath = (path: string, value: unknown) => {
        const parts = path.split('.');
        let obj: any = w;
        for (const part of parts.slice(0, -1)) obj = obj[part] = obj[part] ?? {};
        obj[parts[parts.length - 1]] = value;
      };

      const icon = (fill: string) =>
        'data:image/svg+xml;utf8,' +
        encodeURIComponent(
          `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="10" fill="${fill}"/></svg>`,
        );

      const inProviders: any[] = [];
      for (const spec of specs) {
        const h = make(spec);
        handles[spec.key] = h;
        if (spec.global) setPath(spec.global, h.provider);
        if (spec.inProviders) inProviders.push(h.provider);
      }
      if (inProviders.length) {
        // several extensions sharing one page: window.ethereum is one of them and lists them all
        const head = inProviders[0];
        head.providers = inProviders;
        w.ethereum = head;
      }
      w.__mock = handles[specs[0].key]; // the suite's default handle

      const announceOne = (spec: any) => {
        const h = handles[spec.key];
        window.dispatchEvent(
          new CustomEvent('eip6963:announceProvider', {
            detail: Object.freeze({
              info: { uuid: `mock-uuid-${spec.key}`, name: spec.name, icon: icon(spec.color), rdns: spec.rdns },
              provider: h.provider,
            }),
          }),
        );
      };
      const announce = () => {
        for (const spec of specs) {
          if (!spec.rdns) continue;
          if (spec.announceDelayMs) setTimeout(() => announceOne(spec), spec.announceDelayMs);
          else announceOne(spec);
        }
      };
      window.addEventListener('eip6963:requestProvider', announce);
      announce();
    },
    { specs, tx: TX },
  );
}

/** Injects `window.ethereum` (isMetaMask) that also announces itself over EIP-6963. */
export async function installMockWallet(page: Page, wallets: Wallet[] = [walletA, walletB]) {
  await installWallets(page, DEFAULT_PROFILES, wallets);
}

export const MOCK_TX = TX;

/**
 * Connect the mock wallet. Sign-In-with-Ethereum then happens BY ITSELF (2026-09-09): the app asks
 * for the signature the moment a wallet connects, so the only thing to wait for is the account
 * pill saying the session landed.
 */
export async function connectAndSignIn(page: Page) {
  await page.getByRole('button', { name: 'Connect wallet' }).first().click();
  await page.locator('[data-wallet-id="6963:me.pgas.mock"]').click();
  await signedIn(page).waitFor();
}

/** The header pill once the signature has come back — the one "you are signed in" marker. */
export function signedIn(page: Page) {
  return page.locator('[data-testid="connected-address"][data-signed-in="yes"]');
}

/** Click a tab, whichever of the two navigations is on screen (header on desktop, bar on a phone). */
export async function goTab(page: Page, tab: 'deposit' | 'balance' | 'schedule') {
  await page.locator(`[data-tab="${tab}"]:visible`).first().click();
}

/**
 * Drive the combined "Pay with" picker: the chain row inside it, then the token. Leaving `token`
 * out picks the chain only and closes the picker again.
 */
export async function payWith(page: Page, opts: { chainId?: number; token?: string | RegExp }) {
  const button = page.getByTestId('pay-with');
  const current = Number(await button.getAttribute('data-chain-id'));
  await button.click();
  /**
   * T31 G — the picker has two shapes and says which it is on the button. With a scan to show it
   * lists the wallet's own holdings, grouped by chain (there is no chain row: a row IS a chain and
   * a token); with nothing to show it is the catalogue it has always been.
   */
  if ((await button.getAttribute('data-source')) === 'holdings') {
    const chainId = opts.chainId ?? current;
    const group = page.locator(`[data-chain-group="${chainId}"]`);
    const name = opts.token === undefined ? undefined : typeof opts.token === 'string' ? new RegExp(opts.token, 'i') : opts.token;
    await group
      .getByRole('option', name ? { name } : {})
      .first()
      .click();
    return;
  }
  if (opts.chainId !== undefined) await page.locator(`[data-chain="${opts.chainId}"]`).click();
  if (opts.token !== undefined) {
    const q = typeof opts.token === 'string' ? opts.token : opts.token.source.replace(/[^A-Za-z0-9]/g, '');
    await page.getByLabel('Search tokens').fill(q);
    await page
      .getByRole('option', { name: typeof opts.token === 'string' ? new RegExp(opts.token, 'i') : opts.token })
      .first()
      .click();
  } else {
    await page.getByTestId('pay-with').click(); // the button toggles the popover shut
  }
}
