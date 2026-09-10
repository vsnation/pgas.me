/**
 * The operator panel's transport, and the shape-tolerance the panel renders with.
 *
 * Deliberately NOT part of lib/api.ts: a different credential (`X-Admin-Key`, never the SIWE
 * bearer), a different base path, a different failure policy. `/admin` answers **404 to a wrong
 * key** so that a caller learns nothing from a refusal — which means nothing in this file may turn
 * a status into a diagnosis. "Refused" is all we know and all we say.
 *
 * The key lives in `sessionStorage` only: it dies with the tab, it is never put in a URL (URLs are
 * written to access logs), never in `localStorage`, and never sent anywhere but this origin's
 * `/api/admin/*`.
 */

/**
 * nginx proxies `/api/` to the API with the `/api` prefix stripped, so `/api/admin/overview` is the
 * API's own `/admin/overview` — the path the T38 deploy step verifies (404 without the key, 200
 * with it). One base, stated once: a second candidate would double every failed unlock against the
 * API's 5-failures-per-IP page, and the mount point is a fact to check, not to guess at runtime.
 */
export const ADMIN_BASE = '/api/admin';

const KEY_STORAGE = 'pgas.admin.key.v1';

export function getAdminKey(): string | null {
  try {
    const v = sessionStorage.getItem(KEY_STORAGE);
    return v && v.trim() ? v : null;
  } catch {
    return null; // storage unavailable: this tab simply has no key
  }
}

export function setAdminKey(key: string): void {
  try {
    sessionStorage.setItem(KEY_STORAGE, key);
  } catch {
    // storage unavailable: the key lives in memory for this page load only
  }
}

export function forgetAdminKey(): void {
  try {
    sessionStorage.removeItem(KEY_STORAGE);
  } catch {
    // nothing to forget
  }
}

export class AdminError extends Error {
  status: number;
  /** one sentence, always safe on screen — never a serialised object */
  detail: string;
  /** the path that was asked, so a mismatch between panel and API is diagnosable */
  path: string;
  /**
   * The status said yes and the body said nothing: an answer that is not JSON. Its own flag rather
   * than a made-up status, because a real 502 from a gateway is a different fact and must read as
   * one.
   */
  nonJson: boolean;
  constructor(status: number, detail: string, path: string, nonJson = false) {
    super(detail);
    this.name = 'AdminError';
    this.status = status;
    this.detail = detail;
    this.path = path;
    this.nonJson = nonJson;
  }
}

function detailOf(data: unknown, status: number, statusText: string): string {
  const fallback = `${status} ${statusText || 'error'}`;
  const d = (data as { detail?: unknown } | null)?.detail;
  if (typeof d === 'string' && d) return d;
  if (Array.isArray(d)) {
    const msgs = d.map((e) => (e as { msg?: unknown })?.msg).filter((m): m is string => typeof m === 'string' && !!m);
    if (msgs.length) return msgs.join('; ');
  }
  if (d && typeof d === 'object') {
    const m = (d as { message?: unknown }).message;
    if (typeof m === 'string' && m) return m;
  }
  return fallback;
}

export interface GetOptions {
  signal?: AbortSignal;
  /** query parameters; undefined and '' are dropped rather than sent empty */
  params?: Record<string, string | number | undefined | null>;
  /** the key to use instead of the stored one — the unlock probe passes the typed key */
  key?: string;
}

export function qs(params: Record<string, string | number | undefined | null> | undefined): string {
  if (!params) return '';
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : '';
}

/**
 * One GET against `/api/admin`. The key travels in a header and nowhere else; a refusal carries its
 * status and the path, never an interpretation. A network failure is status 0 — "unreachable" is
 * not "refused", and an unreadable answer is not evidence of anything.
 */
export async function adminGet<T = unknown>(path: string, opts: GetOptions = {}): Promise<T> {
  const key = opts.key ?? getAdminKey();
  if (!key) throw new AdminError(401, 'No key in this tab', path);
  const url = ADMIN_BASE + path + qs(opts.params);
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'GET',
      headers: { Accept: 'application/json', 'X-Admin-Key': key },
      signal: opts.signal,
      credentials: 'omit',
      cache: 'no-store',
    });
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') throw e;
    throw new AdminError(0, 'The API is unreachable from this browser', path);
  }
  const text = await res.text();
  let data: unknown = null;
  let parsed = true;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
    parsed = false;
  }
  if (!res.ok) throw new AdminError(res.status, detailOf(data, res.status, res.statusText), path);
  /**
   * A 200 that is not JSON is not an answer. The SPA fallback turns every unrouted path into
   * `200 text/html` (Pgas.me law #4 — check the content, not the status), and without this an
   * `/admin` that no server is serving would look like an empty-but-successful panel and would
   * even store the key. Status is not evidence; the body is.
   */
  if (!parsed) throw new AdminError(res.status, 'The API answered with something that is not JSON', path, true);
  return data as T;
}

/**
 * Try a key against `/admin/overview`. A 200 is the only evidence that a key is right, and it is
 * also the first screen — so the payload is returned rather than thrown away and fetched again.
 * The key is stored ONLY on that 200: a refused key is never written anywhere.
 */
export async function unlock(key: string): Promise<unknown> {
  const payload = await adminGet<unknown>('/overview', { key });
  if (!isPlainObject(payload) && !Array.isArray(payload))
    throw new AdminError(200, 'The API did not answer with an overview', '/overview', true);
  setAdminKey(key);
  return payload;
}

/** What a refusal is allowed to say. 404 and 401/403 are the same sentence on purpose. */
export function gateMessage(e: unknown): string {
  if (e instanceof AdminError) {
    if (e.nonJson) return `No admin API at ${ADMIN_BASE} — the path answered, but not with JSON.`;
    if (e.status === 429) return 'Too many attempts. The API is rate-limiting this address — wait, then try once.';
    if (e.status === 0) return 'The API is unreachable from this browser.';
    if (e.status === 404 || e.status === 401 || e.status === 403) return `Refused. The API answered ${e.status} at ${ADMIN_BASE}/overview.`;
    // any other status is the API's own trouble, not a verdict on the key: say what came back once
    const extra = e.detail && !e.detail.startsWith(String(e.status)) ? ` — ${e.detail}` : '';
    return `The API answered ${e.status} at ${ADMIN_BASE}/overview.${extra}`;
  }
  return 'Refused.';
}

// ---------- shape tolerance ----------
//
// The panel is written against an API whose exact field names it does not own. Everything below
// reads a payload for its SHAPE — a list is whatever array of objects the body carries — so a new
// field appears in the tables the moment the API starts sending it, and a renamed one does not
// blank a column. The named-in-the-brief fields get first-class columns in the page; these
// functions are what makes the rest visible at all.

export type Row = Record<string, unknown>;

export function isPlainObject(v: unknown): v is Row {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function rowArray(v: unknown): Row[] | null {
  if (!Array.isArray(v)) return null;
  return v.every((x) => isPlainObject(x)) ? (v as Row[]) : null;
}

export interface ListView {
  rows: Row[];
  /** the key the rows were found under (`items`, `deposits`, …), or null for a bare array */
  key: string | null;
  total: number | null;
  cursor: string | null;
  hasMore: boolean;
  /** everything on the envelope that is not the rows — flags, counts, notes */
  meta: Row;
  payload: unknown;
}

const LIST_KEYS = ['items', 'rows', 'results', 'data', 'records', 'list'];
const TOTAL_KEYS = ['total', 'count', 'total_count', 'n_total'];
const CURSOR_KEYS = ['next_cursor', 'cursor', 'next'];

/**
 * The rows in a list payload, whatever it is called. Order of preference: the caller's own guesses
 * (`deposits`, `payouts`, …), then the conventional envelope names, then any NON-EMPTY array of
 * objects on the body, then any empty array — an empty unrelated key must never win over the real
 * one while the real one has rows in it.
 */
export function listView(payload: unknown, prefer: string[] = []): ListView {
  const bare = rowArray(payload);
  if (bare) return { rows: bare, key: null, total: bare.length, cursor: null, hasMore: false, meta: {}, payload };
  if (!isPlainObject(payload)) return { rows: [], key: null, total: null, cursor: null, hasMore: false, meta: {}, payload };

  let key: string | null = null;
  let rows: Row[] = [];
  for (const k of [...prefer, ...LIST_KEYS]) {
    const r = rowArray(payload[k]);
    if (r) {
      key = k;
      rows = r;
      break;
    }
  }
  if (key === null) {
    const entries = Object.entries(payload);
    const nonEmpty = entries.find(([, v]) => (rowArray(v)?.length ?? 0) > 0);
    const anyArr = nonEmpty ?? entries.find(([, v]) => rowArray(v) !== null);
    if (anyArr) {
      key = anyArr[0];
      rows = rowArray(anyArr[1]) as Row[];
    }
  }

  let total: number | null = null;
  for (const k of TOTAL_KEYS) {
    if (typeof payload[k] === 'number') {
      total = payload[k] as number;
      break;
    }
  }
  let cursor: string | null = null;
  for (const k of CURSOR_KEYS) {
    const v = payload[k];
    if (typeof v === 'string' && v) {
      cursor = v;
      break;
    }
  }
  const meta: Row = {};
  for (const [k, v] of Object.entries(payload)) {
    if (k === key || TOTAL_KEYS.includes(k) || CURSOR_KEYS.includes(k) || k === 'has_more') continue;
    meta[k] = v;
  }
  return { rows, key, total, cursor, hasMore: cursor !== null || payload.has_more === true, meta, payload };
}

/** `get(row, 'eth.tx')` — nested reads for the first-class columns, without a lodash. */
export function get(row: unknown, path: string): unknown {
  let cur: unknown = row;
  for (const part of path.split('.')) {
    if (!isPlainObject(cur)) return undefined;
    cur = cur[part];
  }
  return cur;
}

/**
 * Every scalar leaf of a row as `path -> value`, so a table can have one column per fact rather
 * than one column per top-level key with `{…}` in it. Arrays and anything below `maxDepth` stay
 * whole (one cell, rendered compactly, in full in the raw drawer).
 */
export function leaves(row: Row, maxDepth = 3): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  const walk = (obj: Row, prefix: string, depth: number) => {
    for (const [k, v] of Object.entries(obj)) {
      const path = prefix ? `${prefix}.${k}` : k;
      if (isPlainObject(v) && depth < maxDepth && Object.keys(v).length > 0) walk(v, path, depth + 1);
      else out[path] = v;
    }
  };
  walk(row, '', 1);
  return out;
}

/** The id an admin row is addressed by, in the names Mongo-backed APIs actually use. */
export function rowId(row: Row): string | null {
  for (const k of ['_id', 'id', 'deposit_id', 'request_id', 'account_id', 'event_id']) {
    const v = row[k];
    if (typeof v === 'string' && v) return v;
    if (typeof v === 'number') return String(v);
  }
  return null;
}
