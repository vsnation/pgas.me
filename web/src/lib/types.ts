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
  fee_groth: number;
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
  /** Beam confirmations on the bridge message so far, out of BEAM_CONFIRMATIONS */
  beam_confirmations?: number;
  /** the Ethereum block the delivery landed in */
  eth_block?: number;
  /** plain English, operator-written: why the order is parked instead of progressing */
  hold_reason?: string;
  /** the row came from the dark any-asset branch */
  dark?: boolean;
  created_at: number | string;
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

/** `mode:"uniswap"` — where the deposit goes, echoed for the record; nothing here is rendered. */
export interface UniswapRoute {
  hook: string;
  router: string;
  gateway_pool_id: string;
  inner_pool_id: string;
  token_in: string;
  token_out: string;
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
  /** `uniswap` only: the hook, the router the tx goes to, and the two pools behind it */
  route?: UniswapRoute;
  tx?: { chain_id: number; to: string; data: string; value: string };
  /** `swap` only: the single-chain swap the user signs; never registered as a deposit. */
  swap_tx?: { chain_id: number; to: string; data: string; value: string };
  approval?: { chain_id: number; token: string; spender: string; amount: string };
  /** `swap` only: the quote to ask for once the swap lands (amount = what actually arrived). */
  next?: { src_chain_id: number; src_token: string; amount: string };
  order_id?: string;
  note?: string;
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
  fee_groth: number;
  total_debited_groth: number;
  relayer_fee_groth_estimate?: number;
  min_amount_groth?: number;
  items: { request_id: string; W: string; amount_groth: number; deliver_at: number; release_at: number }[];
}

/** `GET /v1/withdrawals/fees?asset=ETH` — read before rendering the form. */
export interface WithdrawalFees {
  fee_bps: number;
  relayer_fee_groth_now: number;
  min_amount_groth: number;
  /** how long the bridge takes; the order is released this far ahead of `deliver_at` */
  bridge_eta_s: number;
}
