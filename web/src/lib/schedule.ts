// The Schedule page's two pieces of arithmetic, kept out of the component so they are readable and
// asserted directly by the e2e clock test.
//
// TIME. The user picks when the ETH should be IN the wallet (`deliver_at`, unix seconds — the UI
// converts local time → unix and nothing downstream deals in local time). The bridge takes
// `bridge_eta_s` (66 min, `GET /v1/withdrawals/fees`), so the order goes to the bridge at
// `release_at = max(now, deliver_at − bridge_eta_s)`. Asking for a delivery sooner than the bridge
// can manage does not make it faster — it arrives at `release_at + bridge_eta_s`, and that is the
// number the form shows, never the wish.
//
// ADDRESS. The user types it; nobody signs anything (admin 2026-09-09). A lowercase or uppercase
// string is accepted and displayed checksummed; a MIXED-case string whose EIP-55 checksum does not
// match is refused, because that one is a typo we can actually catch — ethers' `getAddress` only
// verifies the checksum when the case mix proves it was meant to carry one.
import { getAddress, isAddress } from 'ethers';
import { parseGroth } from './format';

/** `PGAS_BRIDGE_ETA_S` — used only until `GET /v1/withdrawals/fees` answers with the live one. */
export const BRIDGE_ETA_FALLBACK_S = 3960;

export type PresetId = 'asap' | '2h' | 'tonight' | 'tomorrow' | 'custom';

export const DELIVERY_PRESETS: { id: PresetId; label: string }[] = [
  { id: 'asap', label: 'ASAP' },
  { id: '2h', label: 'In 2 h' },
  { id: 'tonight', label: 'Tonight 03:00' },
  { id: 'tomorrow', label: 'Tomorrow, same time' },
  { id: 'custom', label: 'Custom…' },
];

const secs = (ms: number) => Math.floor(ms / 1000);

/** The next local 03:00 strictly after `now` — tonight when it has not happened yet, else tomorrow. */
function nextLocalHour(now: Date, hour: number): Date {
  const d = new Date(now.getTime());
  d.setHours(hour, 0, 0, 0);
  if (d.getTime() <= now.getTime()) d.setDate(d.getDate() + 1);
  return d;
}

/** Preset → `deliver_at` in unix seconds, resolved against the local clock. */
export function presetDeliverAt(id: PresetId, now = new Date()): number {
  switch (id) {
    case '2h':
      return secs(now.getTime()) + 2 * 3600;
    case 'tonight':
      return secs(nextLocalHour(now, 3).getTime());
    case 'tomorrow': {
      // same wall-clock time tomorrow, which survives a DST change; not simply +86400
      const d = new Date(now.getTime());
      d.setDate(d.getDate() + 1);
      return secs(d.getTime());
    }
    default:
      return secs(now.getTime()); // asap (and custom before the user picks one): the API releases now
  }
}

/** `max(now, deliver_at − bridge_eta_s)` — the API's own rule, mirrored so the form can show it. */
export function releaseAt(deliverAt: number, nowS: number, etaS: number): number {
  return Math.max(nowS, deliverAt - etaS);
}

/** What the bridge can actually manage: never earlier than `now + bridge_eta_s`. */
export function arrivesAt(deliverAt: number, nowS: number, etaS: number): number {
  return releaseAt(deliverAt, nowS, etaS) + etaS;
}

function hhmm(unixS: number): string {
  return new Date(unixS * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
}

function sameLocalDay(a: number, b: number): boolean {
  const x = new Date(a * 1000);
  const y = new Date(b * 1000);
  return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate();
}

/** "to the bridge at 11:09 · arrives ≈ 12:15" (+ " on Thu 10 Sep" when that is not today). */
export function deliveryHint(deliverAt: number, nowS: number, etaS: number): string {
  const rel = releaseAt(deliverAt, nowS, etaS);
  const arr = arrivesAt(deliverAt, nowS, etaS);
  const day = sameLocalDay(arr, nowS)
    ? ''
    : ` on ${new Date(arr * 1000).toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short' })}`;
  return `to the bridge at ${hhmm(rel)} · arrives ≈ ${hhmm(arr)}${day}`;
}

/** unix seconds → the value a `<input type="datetime-local">` wants (local time, no zone). */
export function toLocalInput(unixS: number): string {
  const d = new Date(unixS * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** A `datetime-local` value (local time) → unix seconds, or null when it is not a date. */
export function fromLocalInput(v: string): number | null {
  if (!v) return null;
  const t = new Date(v).getTime(); // no zone suffix → parsed as local time
  return Number.isFinite(t) ? Math.floor(t / 1000) : null;
}

export type AddressCheck = { ok: true; address: string } | { ok: false; reason: string };

/**
 * The only check a payout address gets, and the only one it needs: shape, then EIP-55.
 * `isAddress` folds both into one boolean, so the shape is tested first to tell a typo'd checksum
 * ("bad checksum" — recoverable, the user pasted half a copy) apart from something that is not an
 * address at all.
 */
export function checkAddress(raw: string): AddressCheck {
  const s = raw.trim();
  if (!s) return { ok: false, reason: 'enter an address' };
  if (!/^0x[0-9a-fA-F]{40}$/.test(s)) return { ok: false, reason: 'not an EVM address' };
  if (!isAddress(s)) return { ok: false, reason: 'bad checksum' };
  return { ok: true, address: getAddress(s) };
}

// ---------------------------------------------------------------------------
// "Paste a list" — Disperse's shapes, a spreadsheet's shapes, and a time column
// ---------------------------------------------------------------------------
//
// Admin 2026-09-09: "Paste wallets + amounts we can do like people do in Disperse contract, where
// they provide {address}:amount;… Probably you know a better way."
//
// The better way is to accept every shape on one pass instead of asking the user which one they
// have. Entries are separated by newlines or ';'; inside an entry the fields are separated by any
// of space, tab, comma, colon or '='. That covers Disperse (`0xabc:0.05;0xdef:0.1`), a CSV or TSV
// pasted straight out of a spreadsheet (header row and all), and the plain `0xabc 0.05` a person
// types. A third field, when present, is when the money should be there.
//
// The one genuine ambiguity is the comma: it is both a field separator and a decimal mark in half
// the world. It is resolved by position rather than by configuration — the address ends at the
// first separator, the amount is the numeric run that starts the remainder (so `0,05` is one
// number, and `0.05,asap` is a number and a time), and whatever is left is the delivery time.
// Nothing here guesses at a date: only an ISO-ish or slashed date, a unix timestamp or one of the
// preset words is read as a time, because `new Date("12")` is a silent wrong answer, not a parse.

/** What a row gets when the pasted line says nothing about when it should arrive. */
export const DEFAULT_PRESET: PresetId = 'asap';

const PRESET_WORDS: Record<string, PresetId> = {
  asap: 'asap',
  now: 'asap',
  immediately: 'asap',
  '2h': '2h',
  '2hr': '2h',
  '2hrs': '2h',
  '2hours': '2h',
  in2h: '2h',
  tonight: 'tonight',
  tomorrow: 'tomorrow',
};

/** Field separators inside one entry: Disperse's ':' and '=', a spreadsheet's tab/comma, a space. */
const FIELD_SEP = /[\s,;:=]/;
const ADDRESS_SHAPED = /0x[0-9a-fA-F]{40}/;
const HEADER_WORDS = /\b(address|wallet|recipient|receiver|amount|value|qty|eth|deliver|time|date|when)\b/i;

export type PasteMark = 'ok' | 'warn' | 'error';

export interface PasteEntry {
  /** 1-based position among the entries that were parsed (blank lines and the header do not count). */
  n: number;
  raw: string;
  /** EIP-55 checksummed, or null when the address could not be read. */
  address: string | null;
  /** The amount exactly as it will be sent, in groth; null when it could not be read. */
  groth: number | null;
  preset: PresetId;
  /** `datetime-local` value; only meaningful when `preset === 'custom'`. */
  custom: string;
  deliverAt: number | null;
  mark: PasteMark;
  /** ✗ — the line cannot become an order. */
  error: string | null;
  /** ⚠ — the line becomes an order, and the user should look at it anyway. */
  warning: string | null;
}

export interface PasteResult {
  entries: PasteEntry[];
  parsed: number;
  ok: number;
  errors: number;
  warnings: number;
  /** "12 parsed · 11 ok · 1 error" */
  summary: string;
}

/** ISO-ish (`2026-09-10 03:00`, `2026-09-10T03:00:00`), a slashed date, or a unix timestamp. */
function parseTimeField(v: string): number | null {
  const s = v.trim();
  if (!s) return null;
  if (/^\d{10}$/.test(s)) return Number(s); // unix seconds — what "Copy list" round-trips
  if (/^\d{13}$/.test(s)) return Math.floor(Number(s) / 1000);
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return fromLocalInput(`${s}T00:00`);
  const iso = /^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2}(?::\d{2})?)\s*(Z|[+-]\d{2}:?\d{2})?$/.exec(s);
  if (iso) {
    const t = new Date(`${iso[1]}T${iso[2].padStart(5, '0')}${iso[3] ?? ''}`).getTime();
    return Number.isFinite(t) ? Math.floor(t / 1000) : null;
  }
  const slashed = /^(\d{1,2}\/\d{1,2}\/\d{2,4})([ T]\d{1,2}:\d{2}(?::\d{2})?)?$/.exec(s);
  if (slashed) {
    const t = new Date(s).getTime();
    return Number.isFinite(t) ? Math.floor(t / 1000) : null;
  }
  return null;
}

/** One entry → its three fields, without deciding whether any of them is valid. */
function splitEntry(entry: string): { address: string; amount: string; time: string } {
  const s = entry.trim();
  const i = s.search(FIELD_SEP);
  const address = i === -1 ? s : s.slice(0, i);
  const rest = i === -1 ? '' : s.slice(i).replace(/^[\s,;:=]+/, '');
  const m = /^(\d+(?:[.,]\d+)?|[.,]\d+)/.exec(rest);
  const amount = m ? m[0] : '';
  const time = m
    ? rest
        .slice(m[0].length)
        .replace(/^[\s,;:=]+/, '')
        .trim()
    : rest.trim();
  return { address, amount, time };
}

/**
 * Split the pasted text into entries. A newline always ends one. A ';' ends one too — but only when
 * the line really holds several entries, so `0xabc;0.05` stays one entry with ';' as its field
 * separator, and `0xabc:0.05;0xdef:0.1` becomes two.
 */
function splitEntries(text: string): string[] {
  const out: string[] = [];
  for (const line of text.split(/\r?\n/)) {
    const addresses = line.match(new RegExp(ADDRESS_SHAPED.source, 'g'))?.length ?? 0;
    const pieces = line.includes(';') && addresses > 1 ? line.split(';') : [line];
    for (const piece of pieces) {
      const t = piece.trim();
      if (t) out.push(t);
    }
  }
  return out;
}

export interface PasteOptions {
  /** `GET /v1/withdrawals/fees` → `min_amount_groth`. Below it is a ⚠, not a ✗: the row still lands. */
  minGroth: number;
  /** The label the minimum warning states, so the number is written once by the caller. */
  minLabel?: string;
  /** Presets resolve against this clock, exactly as the row's own select does. */
  now?: Date;
  defaultPreset?: PresetId;
}

export function parsePasteList(text: string, opts: PasteOptions): PasteResult {
  const now = opts.now ?? new Date();
  const fallback = opts.defaultPreset ?? DEFAULT_PRESET;
  const entries: PasteEntry[] = [];
  const seen = new Set<string>();
  let first = true;

  for (const raw of splitEntries(text)) {
    if (/^(#|\/\/)/.test(raw)) continue; // a comment the user left in their own list
    // A spreadsheet's header row: the first entry, no address in it, and words that name columns.
    if (first && !ADDRESS_SHAPED.test(raw) && HEADER_WORDS.test(raw)) {
      first = false;
      continue;
    }
    first = false;

    const { address: addrRaw, amount: amountRaw, time: timeRaw } = splitEntry(raw);
    const check = checkAddress(addrRaw);
    const address = check.ok ? check.address : null;
    const groth = amountRaw ? parseGroth(amountRaw.replace(',', '.')) : null;

    let preset: PresetId = fallback;
    let custom = '';
    let deliverAt: number | null = null;
    let timeProblem: string | null = null;
    if (timeRaw) {
      const word = PRESET_WORDS[timeRaw.toLowerCase().replace(/[\s_-]/g, '')];
      if (word) {
        preset = word;
        deliverAt = presetDeliverAt(word, now);
      } else {
        const t = parseTimeField(timeRaw);
        if (t === null) timeProblem = 'bad time';
        else {
          preset = 'custom';
          custom = toLocalInput(t);
          deliverAt = fromLocalInput(custom);
        }
      }
    } else {
      deliverAt = presetDeliverAt(fallback, now);
    }

    const error = !check.ok ? check.reason : groth === null ? 'bad amount' : timeProblem;
    const warnings: string[] = [];
    if (address) {
      const key = address.toLowerCase();
      if (seen.has(key)) warnings.push('duplicate address');
      seen.add(key);
    }
    // Below the API's minimum is a warning, not a refusal: the line still becomes a row, and the row
    // states the minimum in the API's own words and holds the Schedule button down by itself.
    if (!error && groth !== null && groth < opts.minGroth)
      warnings.push(opts.minLabel ? `below the ${opts.minLabel} minimum` : 'below the minimum');
    const warning = warnings.length ? warnings.join(' · ') : null;

    entries.push({
      n: entries.length + 1,
      raw,
      address,
      groth,
      preset,
      custom,
      deliverAt,
      mark: error ? 'error' : warning ? 'warn' : 'ok',
      error,
      warning,
    });
  }

  const errors = entries.filter((e) => e.error).length;
  const warnings = entries.filter((e) => !e.error && e.warning).length;
  const ok = entries.length - errors;
  return {
    entries,
    parsed: entries.length,
    ok,
    errors,
    warnings,
    summary: `${entries.length} parsed · ${ok} ok · ${errors} error${errors === 1 ? '' : 's'}`,
  };
}

/**
 * The list the user can keep: one order per line, `address,amount,deliver_at`. The delivery time is
 * written as the local `datetime-local` form rather than a unix integer, because a person reading
 * their own list should recognise the time in it — and `parsePasteList` reads it straight back, so
 * the export is also the import.
 */
export function toCanonicalList(rows: { address: string; groth: number; deliverAt: number }[]): string {
  return rows.map((r) => `${r.address},${grothDecimal(r.groth)},${toLocalInput(r.deliverAt)}`).join('\n');
}

/** groth → the exact decimal string ("0.05"), never a locale-formatted one ("1,000.05" is not CSV). */
export function grothDecimal(groth: number): string {
  const neg = groth < 0;
  const s = String(Math.abs(Math.trunc(groth))).padStart(9, '0');
  const frac = s.slice(-8).replace(/0+$/, '');
  return `${neg ? '-' : ''}${s.slice(0, -8)}${frac ? '.' + frac : ''}`;
}
