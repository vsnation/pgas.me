/**
 * Human units for the operator console (T49).
 *
 * The old panel printed what the API sent: `1789052843.025172` as a headline, `930600000` as a
 * balance, `float wei 0`. Every one of those is a true number in the wrong unit, and an operator
 * reading a screen full of them is doing arithmetic instead of deciding. Nothing reaches the
 * screen from here without a unit and a symbol.
 *
 * `src/lib/format.ts` already owns the site's readings (`fmtGroth`, `fmtAgo`, `explorerTx`) and is
 * imported rather than re-implemented — law 9, two implementations of one fact will disagree. What
 * lives here is only what the console needs and the site does not have.
 */
import { fmtAgo, fmtNumber, toDate } from '../lib/format';

export const GROTH = 100_000_000;

/** groth → "0.0019999 bETH". The symbol is never optional: a bare number is the old panel. */
export function amount(groth: number | string | null | undefined, asset?: string | null): string {
  if (groth === null || groth === undefined || groth === '') return '–';
  const n = typeof groth === 'string' ? Number(groth) : groth;
  if (!Number.isFinite(n)) return '–';
  const v = n / GROTH;
  const digits = Math.abs(v) >= 1 ? 4 : 8;
  const s = v.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
  return asset ? `${s} ${symbolOf(asset)}` : s;
}

/** The Beam-side spelling of an asset: what we hold is bETH, not ETH. BEAM is itself. */
export function symbolOf(asset: string): string {
  const a = String(asset || '').toUpperCase();
  if (a === 'BEAM' || a.startsWith('B')) return a === 'BEAM' ? 'BEAM' : a;
  return `b${a}`;
}

/**
 * The DECIMALS of each asset on the Ethereum side. A bridge lock carries `value_units` in the
 * token's own units, and printing "3000000000000000" on an operator's screen is the same defect as
 * printing groth: true, and useless. WBTC is 8, everything we bridge today is 18.
 */
const DECIMALS: Record<string, number> = { ETH: 18, WETH: 18, DAI: 18, USDC: 6, USDT: 6, WBTC: 8 };

/** raw token units → "0.003 ETH", by the asset's own decimals. */
export function units(raw: string | number | null | undefined, asset: string | null | undefined): string {
  if (raw === null || raw === undefined || raw === '') return '–';
  const n = Number(raw);
  if (!Number.isFinite(n)) return String(raw);
  const a = String(asset || 'ETH').toUpperCase();
  const v = n / 10 ** (DECIMALS[a] ?? 18);
  const digits = Math.abs(v) >= 1 ? 4 : 8;
  return `${v.toLocaleString('en-US', { maximumFractionDigits: digits })} ${symbolOf(a)}`;
}

/** wei (decimal string or number) → "0.0349 ETH", without pulling in a bigint formatter. */
export function wei(v: string | number | null | undefined, symbol = 'ETH'): string {
  if (v === null || v === undefined || v === '') return '–';
  const n = Number(v);
  if (!Number.isFinite(n)) return '–';
  const e = n / 1e18;
  if (e === 0) return `0 ${symbol}`;
  const digits = Math.abs(e) >= 1 ? 4 : 6;
  return `${e.toLocaleString('en-US', { maximumFractionDigits: digits })} ${symbol}`;
}

/** "15:14:21Z" — the clock face, without the date, for a strip that updates every 30 s. */
export function clock(v: number | string | null | undefined): string {
  const d = toDate(v ?? null);
  return d ? `${d.toISOString().slice(11, 19)}Z` : '–';
}

/** "2026-09-10 15:14:21Z" — the whole moment, for a title and for the drawer. */
export function stamp(v: number | string | null | undefined): string {
  const d = toDate(v ?? null);
  return d ? `${d.toISOString().slice(0, 19).replace('T', ' ')}Z` : '–';
}

/** "12 min ago" / "in 4 min", from the site's one reader. */
export function ago(v: number | string | null | undefined): string {
  const d = toDate(v ?? null);
  return d ? fmtAgo(v as number | string) : '–';
}

/** seconds → "23 s" / "14 min" / "3.2 h" / "2.1 d". A duration, never a date. */
export function span(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return '–';
  const v = Math.abs(s);
  if (v < 90) return `${Math.round(v)} s`;
  if (v < 5400) return `${Math.round(v / 60)} min`;
  if (v < 172_800) return `${(v / 3600).toFixed(1)} h`;
  return `${(v / 86_400).toFixed(1)} d`;
}

/** How long ago, in seconds, or null when the value is not a moment. */
export function agoSeconds(v: number | string | null | undefined, now = Date.now()): number | null {
  const d = toDate(v ?? null);
  return d ? (now - d.getTime()) / 1000 : null;
}

/** An id, truncated in the MIDDLE — the tail of a hash is what distinguishes two of them. */
export function middle(v: string | null | undefined, head = 8, tail = 6): string {
  if (!v) return '–';
  return v.length <= head + tail + 1 ? v : `${v.slice(0, head)}…${v.slice(-tail)}`;
}

export function count(n: number | null | undefined): string {
  return n === null || n === undefined || !Number.isFinite(n) ? '–' : fmtNumber(n, 0);
}

/** `{a: 1, b: 2}` → 3. The counts routes answer per-status maps and the totals are ours to make. */
export function sumOf(map: unknown, keys?: string[]): number {
  if (!map || typeof map !== 'object') return 0;
  const o = map as Record<string, unknown>;
  let n = 0;
  for (const [k, v] of Object.entries(o)) {
    if (keys && !keys.includes(k)) continue;
    if (typeof v === 'number' && Number.isFinite(v)) n += v;
  }
  return n;
}

/** A word for a screen: `payout_direct_enabled` → `payout direct enabled`. */
export function words(k: string): string {
  return k.replace(/[._]/g, ' ').trim();
}

/** "1 crossing" / "3 crossings" — a console that says "1 crossings" is a console nobody proofread. */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
