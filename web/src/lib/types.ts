// Types mirror API_CONTRACT.md (v1) field-for-field.

export type AssetKey = 'ETH' | 'DAI' | 'WBTC';

export interface NonceResponse {
  nonce: string;
  statement: string;
  domains: string[];
}

export interface VerifyResponse {
  token: string;
  account_id: string;
  address: string;
  expires_in: number;
}

/** groth integers (8 decimals; 1 ETH = 100_000_000 groth) */
export interface Balance {
  available: number;
  scheduled: number;
  sent: number;
  pending: number;
}

export type DepositStatus = 'submitted' | 'order_seen' | 'fallback_pending' | 'locked' | 'confirming' | 'credited' | 'failed' | 'expired';

/**
 * Operator-side sweep of an already-credited deposit (claim off the pipe, then shield into the
 * Lelantus pool). It is a SUB-status: `status` stays `credited` throughout, the user's balance
 * never waits for it, and it is never rendered as a status pill — see components/Status.tsx.
 */
export type TreasuryStatus = 'claiming' | 'claimed' | 'shielding' | 'shielded';

/**
 * The quote mode that produced the deposit; `swap` is never registered, so it cannot appear.
 * The only thing the client branches on is `uniswap`, and only to name the steps of that route's
 * own timeline (there is no order to fill); an older API build still sending the router's own name
 * for `xchain` cannot change what is rendered.
 */
export type DepositMode = 'uniswap' | 'xchain' | 'direct';

export interface Deposit {
  _id: string;
  asset: AssetKey;
  mode?: DepositMode;
  status: DepositStatus;
  src: { chain_id: number; token: string; amount: string };
  quote_id: string;
  src_tx_hash?: string;
  order_id?: string;
  eth: {
    tx?: string;
    block?: number;
    msg_id?: string | number;
    value_units?: string;
    relayer_fee_units?: string;
  };
  value_groth?: number;
  created_at: number | string;
  updated_at: number | string;
  note?: string;
  /** the registered hash was proven to belong to this quote */
  verified?: boolean;
  /** the wallet the quote was issued to */
  address?: string;
  treasury?: TreasuryStatus;
  claim_txid?: string;
  shield_txids?: string[];
  /**
   * The Beam BLOCK HEIGHT the bridge explorer keys on (T31b item 8) — a deposit's claim kernel,
   * a payout's crossing kernel. The API is its one writer and answers `null` until it is known;
   * the client draws the link on presence and never derives a height of its own.
   */
  beam_height?: number | null;
}

/**
 * The 2026-09-09 payout order machine (API_CONTRACT.md):
 * `scheduled → releasing → bridging → delivering → sent | failed`, plus `cancelled`.
 * The last two are the designed-but-dark any-asset branch and only appear on rows with `dark`.
 */
export type RequestStatus =
  | 'scheduled'
  | 'releasing'
  | 'bridging'
  | 'delivering'
  | 'sent'
  | 'failed'
  | 'cancelled'
  | 'waiting_for_dep_eth'
  | 'waiting_for_swap_to_target_asset';
export type PayoutMode = 'direct' | 'instant';

export interface PayoutRequest {
  _id: string;
  asset: AssetKey;
  mode: PayoutMode;
  W: string;
  amount_groth: number;
  /** OUR cut only (`fee_bps` of the amount) — the bridge is charged separately, at cost. */
  fee_groth: number;
  /** what the crossing itself cost, quoted when the order was written, headroom included */
  bridge_fee_groth?: number;
  /** when the user asked for the ETH to be in `W` (unix seconds) */
  deliver_at: number | string;
  /** when the order goes to the bridge: `max(now, deliver_at − bridge_eta_s)` */
  release_at: number | string;
  status: RequestStatus;
  beam_txid?: string;
  msg_id?: string | number;
  eth_tx?: string;
  /** what the treasury paid the bridge relayer for this crossing */
  relayer_fee_groth?: number;
  // ── T45 block: the bridge fee is an ESTIMATE, and what it does not spend comes back ─────────
  // The charge is quoted at request time from a live gas price plus headroom; the crossing then
  // costs what it costs. At settlement the API credits `bridge_fee_groth − relayer_fee_groth`
  // back to Available as a `bridge_fee_refund` ledger entry and writes the number here — always
  // present on a settled row, 0 included, so the page never has to ask "missing, or nothing?".
  /** what came BACK to the user: the part of the bridge fee the crossing did not spend */
  bridge_fee_refund_groth?: number;
  /** the other side of the same difference: what the treasury absorbed when gas had risen */
  relayer_subsidy_groth?: number;
  // ────────────────────────────────────────────────────────────────────────────────────────────
  /** Beam confirmations on the bridge message so far, out of BEAM_CONFIRMATIONS */
  beam_confirmations?: number;
  /** the Ethereum block the delivery landed in */
  eth_block?: number;
  /** plain English, operator-written: why the order is parked instead of progressing */
  hold_reason?: string;
  /** the row came from the dark any-asset branch */
  dark?: boolean;
  created_at: number | string;
  /**
   * The Beam BLOCK HEIGHT the bridge explorer keys on (T31b item 8) — a deposit's claim kernel,
   * a payout's crossing kernel. The API is its one writer and answers `null` until it is known;
   * the client draws the link on presence and never derives a height of its own.
   */
  beam_height?: number | null;
}

/** Ledger entry as returned in `history` (append-only; deltas per bucket). */
export interface HistoryEntry {
  kind: string;
  groth: number;
  d_avail?: number;
  d_sched?: number;
  d_sent?: number;
  ref?: string;
  note?: string;
  at: number | string;
  asset?: string;
}

export interface Account {
  address: string;
  account_id: string;
  balances: Partial<Record<AssetKey, Balance>>;
  fee_bps: number;
  denominations: number[];
  min_payout_groth: number;
  modes: { direct: boolean; instant: boolean; ingress?: { uniswap?: boolean; xchain?: boolean } };
  /** `armed` is the Beam side being ready; `uniswap`/`xchain` are the paths, read via lib/ingress */
  ingress: { armed: boolean; near: boolean; uniswap?: boolean; xchain?: boolean };
  deposits: Deposit[];
  requests: PayoutRequest[];
  destinations: number;
  history: HistoryEntry[];
}

export interface Chain {
  /** EVM/original id — used for wallets and RPC */
  chain_id: number;
  /** the cross-chain order router's own id — used for the API */
  route_chain_id?: number;
  name: string;
  native_symbol: string;
  batch_balance?: string;
}

/**
 * The router's own id for a chain, which differs from the EVM id on a few of them (Story is 1514
 * on chain and another number to the router). The API sends it as `route_chain_id`; an older build
 * sent the same number under its own vendor-prefixed key, so any other `*_chain_id` number on the
 * row is read as the same fact. A row carrying neither is its own EVM id.
 */
export function routeChainId(c: Chain): number {
  if (typeof c.route_chain_id === 'number') return c.route_chain_id;
  for (const [k, v] of Object.entries(c)) {
    if (k !== 'chain_id' && k.endsWith('_chain_id') && typeof v === 'number') return v;
  }
  return c.chain_id;
}

export interface Token {
  address: string;
  symbol: string;
  name: string;
  decimals: number;
  logo?: string;
}

/** `GET /v1/assets` — the assets, plus (2026-09-10) the ingress flags; see lib/ingress.ts. */
export interface AssetsResponse {
  assets: Asset[];
  ingress?: Record<string, unknown>;
}

/** `GET /v1/health` — public, and the second place the ingress flags are published. */
export interface HealthResponse {
  ok?: boolean;
  version?: string;
  ingress_armed?: boolean;
  paused?: boolean;
  ingress?: Record<string, unknown>;
}

export interface Asset {
  key: AssetKey;
  symbol: string;
  beam_symbol: string;
  token: string;
  decimals: number;
  pipe: string;
  aid: number;
}

export interface QuoteBody {
  src_chain_id: number;
  src_token: string;
  /** raw units, decimal string */
  amount: string;
  target_asset: AssetKey;
  sender: string;
  /**
   * Which ingress to quote. Sent as `"uniswap"` only when the API says that path is open and the
   * pair is one it takes; left off otherwise, and the API picks (`"auto"`: uniswap → direct →
   * cross-chain). An API build that has never heard of the field ignores it and answers as before.
   */
  route?: 'uniswap' | 'auto';
}

/**
 * `uniswap` — the primary ingress: one transaction on Ethereum into the Pgas gateway pool, whose
 * hook swaps on the canonical pool and locks the output in the Beam bridge in the same transaction.
 * `xchain` — a cross-chain order. `direct` — chain 1 and the source token already IS the target
 * asset, so the tx goes straight to the pipe. `swap` — chain 1, any other token: a single-chain
 * swap through the router into the user's OWN wallet, then a fresh `direct` quote for what
 * actually arrived.
 */
export type QuoteMode = 'uniswap' | 'xchain' | 'direct' | 'swap';

/**
 * The mode, normalised. `uniswap`, `direct` and `swap` are the three on-Ethereum shapes and every
 * API build names them the same; anything else — the neutral `xchain`, the router's own older name
 * for it, or no field at all — is the cross-chain order and renders exactly the same UI.
 */
export function quoteMode(q: { mode?: string } | null | undefined): QuoteMode {
  const m = q?.mode;
  return m === 'uniswap' || m === 'direct' || m === 'swap' ? m : 'xchain';
}

/**
 * `mode:"uniswap"` — what the swap goes through, echoed for the record; nothing here is rendered.
 *
 * This IS the two-step route (U2-api, shipped 2026-09-10 15:15Z): Uniswap's own Universal Router,
 * Permit2, and the canonical HOOK-LESS pool the swap happens on. Nothing of ours is on chain, so
 * there is no hook and no gateway pool — the four fields that named them are kept, optional, only
 * because a box still running `PGAS_UNISWAP_HOOK_ENABLED=1` answers with them and a client that
 * cannot parse that answer is a client that breaks on a rollback.
 */
export interface UniswapRoute {
  /** the Universal Router (two-step) or the PgasRouter (legacy hook shape) */
  router: string;
  token_in: string;
  token_out: string;
  /** two-step: the Permit2 both allowances are granted through */
  permit2?: string;
  /** two-step: the canonical pool and its key — `hooks` is the zero address there, by definition */
  pool_id?: string;
  pool_key?: { currency0: string; currency1: string; fee: number; tickSpacing: number; hooks: string };
  fee?: number;
  symbol?: string;
  // ---- legacy: the deployed-hook shape, `PGAS_UNISWAP_HOOK_ENABLED=1`, not deployed ----
  hook?: string;
  gateway_pool_id?: string;
  inner_pool_id?: string;
}

/**
 * One transaction the user must send BEFORE the swap, exactly as the API built it (U2-api,
 * 2026-09-10 15:15Z). `approvals[]` carries only the SHORT ones, already in the order they must be
 * sent — the client sends them and does not decide which exist:
 *
 *  - `approval_reset` — `approve(Permit2, 0)`. A USDT-style token refuses to raise a non-zero
 *    allowance, so a short-but-non-zero one is zeroed first or the next approval reverts.
 *  - `approval` — `approve(Permit2, amount)`, the token's own allowance.
 *  - `permit_tx` — `Permit2.approve(token, router, amount, expiration)`, the router's.
 *
 * `to`/`data` are the transaction. `token`/`spender`/`amount` are context for the UI — and, on the
 * PRE-SHIPPED `approval` field alone, they were all there was: that shape carried no calldata and
 * the client encoded `approve` itself. `quoteApprovals()` below is the one place that difference
 * lives; `data` is optional here for that reason only.
 */
export type ApprovalKind = 'approval_reset' | 'approval' | 'permit_tx';

export interface QuoteApproval {
  /** what this transaction is; an unrecognised name is still SENT, just described generically */
  name?: ApprovalKind | string;
  chain_id: number;
  /** the contract the transaction goes to — the token, or Permit2. Falls back to `token`. */
  to?: string;
  /** the calldata the API built; absent only on the legacy `approval` shape */
  data?: string;
  value?: string;
  token?: string;
  spender?: string;
  amount?: string;
  /** `permit_tx` only: the Permit2 expiry, which is the router deadline */
  expiration?: number;
}

/**
 * ONE adapter for "which transactions come before this swap, in what order".
 *
 * The shipped API answers `approvals[]`. An API built before 2026-09-10 15:15Z — or a box that has
 * not been deployed yet — answers the two named fields instead: `approval` (three arguments, no
 * calldata) and `permit_tx` / `permit_fallback_tx` (its pre-rename name). Both are read HERE, once,
 * so the flow that sends them has exactly one shape to know about (law 9: two implementations of
 * one fact will disagree, and one of them will reach money).
 *
 * ⛔ An `approvals` field that is present but not an array is NOT "no approvals": the old fields
 * are read in that case too, because a shape we cannot parse is not evidence that nothing is owed.
 */
export function quoteApprovals(q: Quote | null | undefined): QuoteApproval[] {
  if (!q) return [];
  const list = q.approvals;
  if (Array.isArray(list)) {
    // an entry with nothing to send is dropped: it would be a wallet prompt for an empty tx
    return list.filter((a): a is QuoteApproval => !!a && typeof a === 'object' && !!(a.data || (a.spender && a.amount !== undefined)));
  }
  const out: QuoteApproval[] = [];
  if (q.approval) out.push({ name: 'approval', ...q.approval });
  const permit = q.permit_tx ?? q.permit_fallback_tx;
  if (permit) out.push({ name: 'permit_tx', ...permit });
  return out;
}

/**
 * The identity of one approval transaction — what it is, where it goes, and the bytes it carries.
 * Used to remember that THIS transaction has already been sent in this flow, so a re-quote that
 * repeats an entry does not prompt the wallet for it twice. Position would be the wrong key: the
 * list shortens as allowances land, and step 2 of three is step 1 of two the moment one is done.
 */
export function approvalKey(a: QuoteApproval): string {
  return `${a.name ?? 'approval'}:${(a.to ?? a.token ?? '').toLowerCase()}:${(a.data ?? `${a.spender}:${a.amount}`).toLowerCase()}`;
}

export interface Quote {
  quote_id: string;
  target_asset: AssetKey;
  /** read it through `quoteMode()`: an older API build sends the router's own name for `xchain` */
  mode: QuoteMode;
  armed: boolean;
  expires_at: string | number;
  estimate: {
    src: { chain_id: number; token: string; symbol: string; decimals: number; amount: string };
    out_units: string;
    /** `uniswap` only: `out_units × (1 − slippage)`, the bound the hook itself reverts below */
    min_out_units?: string;
    out_groth: number;
    value_units?: string;
    relayer_fee_units?: string;
    usd?: number;
    eta_s: number;
    /** `uniswap` only: what this size costs on the inner pool, in basis points */
    price_impact_bps?: number;
    /** the router's own fee breakdown, passed straight through; nothing renders it */
    route_fees?: Record<string, unknown>;
  };
  /** `uniswap` only: the id the hook emits on `PgasDeposit`, which ties the receipt to this quote */
  deposit_ref?: string;
  /** `uniswap` only: the router, Permit2 and the pool the swap goes through (legacy: the hook) */
  route?: UniswapRoute;
  tx?: { chain_id: number; to: string; data: string; value: string };
  /** `swap` only: the single-chain swap the user signs; never registered as a deposit. */
  swap_tx?: { chain_id: number; to: string; data: string; value: string };
  approval?: { chain_id: number; token: string; spender: string; amount: string };
  /** `swap` only: the quote to ask for once the swap lands (amount = what actually arrived). */
  next?: { src_chain_id: number; src_token: string; amount: string };
  order_id?: string;
  note?: string;

  // ---- U2 (2026-09-10): mode "uniswap" as TWO steps, no contract deployed ----
  // The hook route above is one transaction and carries `tx`; this one is the `swap` handshake
  // with Uniswap V4 as the venue — `step:"swap"` + `swap_tx` + `next`, then a fresh `direct`
  // quote for what actually arrived. Which shape a quote is, is read off the quote, never
  // assumed: `tx` means one transaction, `step:"swap"` means two.
  /** `"swap"` on the first leg of the two-step Uniswap route; absent on every other shape. */
  step?: 'swap' | 'deposit';
  /**
   * SHIPPED (U2-api, 2026-09-10 15:15Z): every transaction that must precede the swap, in order,
   * and only the ones that are actually short. Read it through `quoteApprovals()` — never
   * directly — so the three older field names below are the same fact rather than a second one.
   *
   * ⛔ There is NO off-chain permit here (T31b item 9, 2026-09-10). The quote used to carry an
   * EIP-712 `PermitSingle` for the wallet to sign, and the signature had nowhere to go: it belongs
   * inside the router's `execute` calldata, which the API builds. A prompt whose answer is
   * discarded is worse than no prompt. Every entry here is a transaction, never a signature.
   */
  approvals?: QuoteApproval[];
  /**
   * PRE-SHIPPED shape, still read: `Permit2.approve(token, router, amount, expiration)` as its own
   * field, with `approval` beside it for the token's own allowance. `permit_fallback_tx` is the
   * name the contract gave this transaction while it was still described as the fallback to a
   * signature. All three are read only when `approvals` is absent — see `quoteApprovals()`.
   */
  permit_tx?: { chain_id: number; to: string; data: string; value?: string };
  /** The pre-rename name of `permit_tx` (API_CONTRACT.md § Quote mode "uniswap"). */
  permit_fallback_tx?: { chain_id: number; to: string; data: string; value?: string };
}

/**
 * `POST /v1/quote/{id}/arm` (2026-09-09): for a cross-chain quote the estimate above costs ONE
 * router call and carries no `tx`; the hook-carrying order is built only when the user clicks
 * Deposit. `estimate` is the final one — it may differ from the estimate the user was shown.
 */
export interface ArmedQuote {
  quote_id: string;
  tx: { chain_id: number; to: string; data: string; value: string };
  approval?: { chain_id: number; token: string; spender: string; amount: string };
  order_id?: string;
  estimate: Quote['estimate'];
  expires_at: string | number;
}

/** One scheduled order: an address the user typed, an amount, and when it should be there. */
export interface WithdrawalItem {
  W: string;
  amount_groth: number;
  /** unix seconds — when the ETH should be IN `W` (the UI converts local time → unix) */
  deliver_at: number;
}

export interface WithdrawalBody {
  asset: AssetKey;
  items: WithdrawalItem[];
  mode: PayoutMode;
}

export interface WithdrawalResponse {
  request_ids: string[];
  /** ours (2 %) */
  fee_groth: number;
  /** the crossing, at cost — absent on an API build from before 2026-09-10 */
  bridge_fee_groth?: number;
  /** amount + fee + bridge fee */
  total_debited_groth: number;
  relayer_fee_groth_estimate?: number;
  min_amount_groth?: number;
  items: {
    request_id: string;
    W: string;
    amount_groth: number;
    fee_groth?: number;
    bridge_fee_groth?: number;
    total_groth?: number;
    deliver_at: number;
    release_at: number;
  }[];
}

/**
 * What `POST /v1/withdrawals/preview` says one row costs, and whether that row may be scheduled.
 * Every number here is the API's. The SAME dicts come back inside a 422 `detail.items` from
 * `POST /v1/withdrawals` (one pricing call serves both), so a refusal is rendered by the code
 * that renders a quote — there is no second shape for "the API said no".
 */
export interface WithdrawalPreviewItem {
  W: string;
  amount_groth: number;
  /** ours: `ceil(amount × fee_bps / 10000)` */
  fee_groth: number;
  /** the bridge, at cost: the live relayer fee × the headroom the wait until release needs */
  bridge_fee_groth: number;
  /** amount + fee + bridge fee — what this row takes off Available */
  total_groth: number;
  release_at: number;
  /** echoed back from the request */
  deliver_at?: number;
  /** the floor THIS item was ruled against, carried on the item by the API */
  min_amount_groth?: number;
  // ── T45 item 5 block: "Use max", priced by the API ──────────────────────────────────────────
  // The largest amount THIS row can ask for and still have its fees charged ON TOP, out of what
  // the rows above it left of Available. ⛔ The form must never compute it: `available − 2 % −
  // bridge` is a second implementation of the fee model, and it is the one that would reach the
  // user's own money first. Absent on an API build from before T45 — the button is not drawn.
  max_on_top_groth?: number;
  // ────────────────────────────────────────────────────────────────────────────────────────────
  ok: boolean;
  /** why this row cannot be scheduled, in the API's own words */
  problem?: string;
  /** the same refusal as a stable token (`address`, `min_amount`, `grid`, …) */
  problem_code?: string;
}

/**
 * The verdict on the list AS A LIST: Σ `total_groth` ≤ Available is one decision about the whole
 * batch, not a property of any row in it — the row that happens to cross the line is not the row
 * that is wrong. The API rules on it with the same function `POST /v1/withdrawals` refuses on, so
 * the form never compares two numbers of its own (law: one writer per fact).
 */
export interface WithdrawalBatchVerdict {
  ok: boolean;
  /** how much the list is over Available by; `null` when the API sent no readable number */
  shortfall_groth: number | null;
  /** why the batch cannot be scheduled, in the API's own words */
  problem?: string;
  /**
   * The API also sends the two numbers the verdict was decided FROM — `need_groth` and
   * `available_groth`. They are DELIBERATELY not read here: the same two facts are already on
   * `totals.total_debited_groth` and `available_groth`, and reading them from a second place is
   * how one number gets rendered beside a verdict that was reached on another one.
   */
}

/**
 * `POST /v1/withdrawals/preview` (2026-09-10) — the whole point of it: the form shows THESE
 * numbers and does no fee arithmetic of its own, and `POST /v1/withdrawals` prices the same list
 * with the same function, so what is quoted is what is charged.
 */
export interface WithdrawalPreview {
  items: WithdrawalPreviewItem[];
  totals: { amount_groth: number; fee_groth: number; bridge_fee_groth: number; total_debited_groth: number };
  available_groth: number;
  min_amount_groth: number;
  /** the batch rule, ruled on by the API — absent on a build from before 2026-09-10 */
  batch?: WithdrawalBatchVerdict;
  /** what the TREASURY can deliver today (T52) — absent on a build from before it */
  treasury?: WithdrawalTreasuryVerdict;
  /** our cut, restated beside the numbers it produced */
  fee_bps?: number;
  /** how far ahead of `deliver_at` this mode's orders are released */
  bridge_eta_s?: number;
}

/**
 * ONE reader for "may this list be scheduled as a batch". The API's verdict when it publishes one,
 * and `null` — never a fabricated yes — when it does not: an answer that cannot be read is not
 * evidence of anything, so the caller decides what silence means rather than being handed an ok.
 * `shortfall_groth` stays `null` when the API sent no readable number: a shortfall of 0 would say
 * "over by nothing", which is a different claim from "we do not know by how much".
 */
export function batchVerdict(p: WithdrawalPreview | null | undefined): WithdrawalBatchVerdict | null {
  const b = p?.batch;
  if (!b || typeof b.ok !== 'boolean') return null;
  return {
    ok: b.ok,
    shortfall_groth: typeof b.shortfall_groth === 'number' && Number.isFinite(b.shortfall_groth) ? b.shortfall_groth : null,
    problem: typeof b.problem === 'string' && b.problem ? b.problem : undefined,
  };
}

/**
 * The TREASURY's verdict, beside the batch's (T52, 2026-09-10). Two different questions with two
 * different answers: `batch` is "does your Available cover this list", this is "can we move it
 * today". A user can fix the first by topping up and cannot fix the second at all — the bETH the
 * treasury shielded last night is inside a max-privacy lock for up to 72 hours, and no amount of
 * money in their balance shortens it.
 *
 * Admin, 2026-09-10 15:38Z: *"Why do you accept user request if user cannot spend this?"* →
 * 15:45Z: *"Next time when user wants to withdraw just tell him he can't"*.
 *
 * ⛔ EVERY FIELD MAY BE NULL, AND NULL IS NOT ZERO. `float_now_groth: null` means the API could
 * not measure the treasury, which is `ok: true` — the order is accepted and the release gate
 * says when. A page that rendered "Deliverable now: 0 ETH" for it would be inventing a refusal
 * the API did not make.
 */
export interface WithdrawalTreasuryVerdict {
  ok: boolean;
  /** what the wallet can move right now, minus everything already promised */
  float_now_groth: number | null;
  /** the largest amount ONE order may ask for today — the API's arithmetic, never the form's */
  deliverable_now_groth: number | null;
  /** when the locked rest comes back (unix seconds), or null when nothing is maturing */
  next_unlock_at: number | null;
  /** why this list cannot be scheduled today, in the API's own words */
  problem?: string;
}

/**
 * ONE reader for "can the treasury move this list today" — the same shape as `batchVerdict`, and
 * `null` on a build that does not publish it, never a fabricated verdict.
 */
export function treasuryVerdict(p: WithdrawalPreview | null | undefined): WithdrawalTreasuryVerdict | null {
  const t = p?.treasury;
  if (!t || typeof t.ok !== 'boolean') return null;
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  return {
    ok: t.ok,
    float_now_groth: num(t.float_now_groth),
    deliverable_now_groth: num(t.deliverable_now_groth),
    next_unlock_at: num(t.next_unlock_at),
    problem: typeof t.problem === 'string' && t.problem ? t.problem : undefined,
  };
}

export interface WithdrawalPreviewBody {
  asset: AssetKey;
  items: WithdrawalItem[];
}

/** `GET /v1/withdrawals/fees?asset=ETH` — read before rendering the form. */
export interface WithdrawalFees {
  fee_bps: number;
  /**
   * The technical floor: `max(PGAS_MIN_PAYOUT_GROTH, the asset grid)`, default 1 groth. The
   * economic floor is gone (2026-09-10) — the bridge fee is charged explicitly instead of being
   * taken out of our 2 %, so there is nothing left for a minimum to protect.
   */
  min_amount_groth: number;
  /** what a crossing costs right now, before headroom — the pass-through the user is charged */
  bridge_fee_groth_now?: number;
  /** the curve: an order that waits `window_s` before it is released funds `factor ×` today's fee */
  headroom?: { window_s: number; factor: number }[];
  /** an API build from before 2026-09-10 calls the same number this */
  relayer_fee_groth_now?: number;
  /** how long the bridge takes; the order is released this far ahead of `deliver_at` */
  bridge_eta_s?: number;
}

/**
 * ONE reader for "what the bridge costs now", so the old key and the new one are the same fact
 * rather than two (law: one writer per fact, and one reader for the names it has had).
 */
export function bridgeFeeNow(f: WithdrawalFees | null | undefined): number | null {
  if (!f) return null;
  const v = typeof f.bridge_fee_groth_now === 'number' ? f.bridge_fee_groth_now : f.relayer_fee_groth_now;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** "1× now → 3× a month ahead" from the published curve, or null when it publishes none. */
export function headroomHint(f: WithdrawalFees | null | undefined): string | null {
  const rows = f?.headroom;
  if (!rows || rows.length < 2) return null;
  const sorted = [...rows].sort((a, b) => a.window_s - b.window_s);
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  const days = Math.round(last.window_s / 86400);
  return `${first.factor}× now → ${last.factor}× ${days} day${days === 1 ? '' : 's'} ahead`;
}

// ─────────────────────────────────────────────────────────────────────────────────────────────
// T35 — fees on top when affordable, from the amount otherwise (2026-09-10). ADDITIVE BLOCK.
//
// Admin: "You need to take fees above the amount user requested. If user requested to get 0.01
// ETH, we should deposit 0.01 ETH; only if user doesn't have deposit to pay gas fees and 2% fees
// to us, we take it from sending amount, so he gets less than 0.01 ETH."
//
// So a priced row carries THREE numbers now instead of one: what the wallet receives
// (`delivered_groth`), what leaves the balance (`debited_groth`) and which of the two rules was
// applied (`fee_mode`). They are optional here on purpose — an API build from before this change
// publishes none of them, and the UI renders a dash rather than deriving one (law 9).
// ─────────────────────────────────────────────────────────────────────────────────────────────

/** How the fees were charged for one row (API_CONTRACT.md § Withdrawals — fee model). */
export type WithdrawalFeeMode = 'on_top' | 'from_amount';

/** The three fields T35 adds to every priced row, in `preview` and in a 422's `detail.items`. */
export interface WithdrawalFeeSplit {
  /** what the wallet receives — `amount_groth` on an `on_top` row, less on a `from_amount` one */
  delivered_groth?: number;
  /** what this row takes off Available (amount + fees, or exactly the amount) */
  debited_groth?: number;
  fee_mode?: WithdrawalFeeMode;
  /** what the user typed, restated under an unambiguous name */
  requested_groth?: number;
  /** the API's own sentence for a from-amount row — rendered instead of any wording of ours */
  fee_note?: string;
}

/** A priced row as the 2026-09-10 API returns it. Every T35 field is optional (see above). */
export type PricedWithdrawalItem = WithdrawalPreviewItem & WithdrawalFeeSplit;

/**
 * Σ what the wallets receive, when the API publishes it.
 *
 * Read through a function for the same reason `bridgeFeeNow` is: the field is new, an older build
 * omits it, and "the API did not say" must not become a zero anywhere on a money screen.
 */
export function deliveredTotal(t: WithdrawalPreview['totals'] | null | undefined): number | null {
  const v = (t as { delivered_groth?: unknown } | null | undefined)?.delivered_groth;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}
