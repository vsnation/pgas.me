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

export interface Deposit {
  _id: string;
  asset: AssetKey;
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
}

export type RequestStatus = 'scheduled' | 'bridging' | 'sent' | 'failed' | 'cancelled';
export type PayoutMode = 'direct' | 'instant';

export interface PayoutRequest {
  _id: string;
  asset: AssetKey;
  mode: PayoutMode;
  W: string;
  amount_groth: number;
  fee_groth: number;
  window_s: number;
  release_at: number | string;
  status: RequestStatus;
  beam_txid?: string;
  msg_id?: string | number;
  eth_tx?: string;
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
  modes: { direct: boolean; instant: boolean };
  ingress: { armed: boolean; near: boolean };
  deposits: Deposit[];
  requests: PayoutRequest[];
  destinations: number;
  history: HistoryEntry[];
}

export type DestinationKind = 'connected' | 'proven' | 'generated';

export interface Destination {
  address: string;
  kind: DestinationKind;
  verified_at: number | string;
  label?: string;
  created_at?: number | string;
}

export interface AddDestinationBody {
  address: string;
  kind: 'proven' | 'generated';
  nonce: string;
  issued: string;
  signature: string;
  label?: string;
}

export interface Chain {
  /** EVM/original id — used for wallets and RPC */
  chain_id: number;
  /** deBridge internal id — used for the API */
  dln_chain_id: number;
  name: string;
  native_symbol: string;
  batch_balance?: string;
}

export interface Token {
  address: string;
  symbol: string;
  name: string;
  decimals: number;
  logo?: string;
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
}

export interface Quote {
  quote_id: string;
  target_asset: AssetKey;
  armed: boolean;
  expires_at: string | number;
  estimate: {
    src: { chain_id: number; token: string; symbol: string; decimals: number; amount: string };
    out_units: string;
    out_groth: number;
    value_units?: string;
    relayer_fee_units?: string;
    usd?: number;
    eta_s: number;
    dln_fees?: Record<string, unknown>;
  };
  tx?: { chain_id: number; to: string; data: string; value: string };
  approval?: { chain_id: number; token: string; spender: string; amount: string };
  order_id?: string;
  note?: string;
}

export interface WithdrawalBody {
  asset: AssetKey;
  items: { W: string; amount_groth: number }[];
  mode: PayoutMode;
  window_s: number;
}

export interface WithdrawalResponse {
  request_ids: string[];
  fee_groth: number;
  total_debited_groth: number;
  eta: { min_s: number; max_s: number };
  privacy_grade: 'weak' | 'ok' | 'good';
}

export interface Stats {
  deposits_24h: number;
  deposits_7d: number;
  payouts_24h: number;
  pool: {
    shielded_outputs_total: number;
    shielded_outputs_per_24h: number;
    height: number;
    at: number | string;
  };
  float: Record<string, { active_distributors: number; wei: string | number }>;
  armed: { ingress: boolean; direct: boolean; instant: boolean };
}
