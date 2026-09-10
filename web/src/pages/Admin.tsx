/**
 * The operator console — `/admin` (T38, redesigned T49).
 *
 * ── THE THREE RULES THAT SHAPE THIS FILE (T38/T38b; NONE of them changed in T49) ──────────────
 *
 * 1. **It is not part of the public app.** `App.tsx` returns this page BEFORE the store provider
 *    mounts, so on `/admin` no wallet is discovered, no session is polled, no reference data is
 *    fetched. Nothing links here, it is not in `sitemap.xml`, and the route sets `robots: noindex`.
 * 2. **The key is the secret, the code is not.** It is typed into a password field, held in
 *    `sessionStorage` for this tab only, sent in the `X-Admin-Key` header and never in a URL. A
 *    refusal is a refusal: the API answers 404 to a wrong key and this page does not guess further.
 * 3. **A refusal re-gates and STOPS.** 401 / 404 / 429 mean the key in this tab is not a key —
 *    forget it, show the gate with the reason, stop the timer. A tab left open across a key
 *    rotation used to knock every 30 s for ever, and the fifth knock in a window pages the
 *    operator: the operator's own idle tab firing the operator's own pager.
 *
 * ── WHAT T49 CHANGED ──────────────────────────────────────────────────────────────────────────
 *
 * The old page rendered whatever JSON arrived — a label/value row per key, a tile per number, the
 * union of every leaf as a table column. That is a JSON viewer. This one is designed per tab
 * (`src/admin/`): Overview is a queue of what needs the operator, the list tabs are tables with
 * chosen columns and a status LADDER, Treasury is the three Beam tables kept apart, and everything
 * the API sent is still one click away under "Raw" in the row drawer.
 *
 * ⛔ ONE READ MAY RE-GATE THIS PAGE, AND IT IS THE TAB'S OWN. Every tab now asks for a little
 * context beside its own payload (the Overview needs the rows behind its attention list; every tab
 * needs `/overview` for the status strip). If each of those could re-gate, a stale key would fire
 * FIVE refusals in one pass — and five in a window is exactly the Telegram page rule 3 exists to
 * prevent. So the PRIMARY read is fetched first and alone; only if it succeeds do the extras go
 * out, and an extra that fails degrades its own section and never touches the key.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import {
  ADMIN_BASE,
  AdminError,
  adminGet,
  forgetAdminKey,
  gateMessage,
  getAdminKey,
  isPlainObject,
  unlock,
  type Row,
} from '../lib/adminApi';
import { currentTheme, setTheme, type ThemeChoice } from '../lib/theme';
import '../admin/ops.css';
import { clock, count, span, stamp } from '../admin/fmt';
import { idOf, num, pick, rowsOf, str, type TabId } from '../admin/model';
import { Overview } from '../admin/Overview';
import { Treasury } from '../admin/Treasury';
import { AccountsTable, DepositsTable, EventsTimeline, LocksTable, PayoutsTable, RowDrawer } from '../admin/tables';
import { Dot, Empty, Section, type Tone } from '../admin/ui';

/** The one place the route is spelled. `App.tsx` asks this, nothing else does. */
export function isAdminPath(pathname: string): boolean {
  return pathname.replace(/\/+$/, '').toLowerCase() === '/admin';
}

const TABS: { id: TabId; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'deposits', label: 'Deposits' },
  { id: 'payouts', label: 'Payouts' },
  { id: 'unattributed', label: 'Unattributed' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'treasury', label: 'Treasury' },
  { id: 'events', label: 'Events' },
];

const REFRESH_MS = 30_000;
/** the clock in the strip and every "12 min ago" on screen, without a re-read */
const TICK_MS = 15_000;
/**
 * ⚠️ `/admin/treasury` IS A LIVE WALLET READ, AND ITS OWN DOCSTRING SAYS SO: one BeamPay
 * `/balances` per registered address per asset, one `/wallet_status`, and one `get_utxo` walk. The
 * Overview shows those numbers, so it re-reads them on this much slower cadence and STAMPS THEM
 * WITH THEIR AGE; the Treasury tab, which is opened to know what is true right now, always reads.
 */
const TREASURY_TTL_MS = 5 * 60_000;
/** `/overview` is cheap Mongo plus a 30 s-cached coin count; the strip wants it on every tab. */
const OVERVIEW_TTL_MS = 25_000;

const LIMITS = [50, 200, 500];
const WINDOWS: { id: string; label: string; seconds: number | null }[] = [
  { id: '24h', label: '24 h', seconds: 24 * 3600 },
  { id: '7d', label: '7 d', seconds: 7 * 24 * 3600 },
  { id: 'all', label: 'all', seconds: null },
];

/** ⛔ THE STATUSES THAT MEAN "THE KEY IN THIS TAB IS NOT A KEY". See rule 3 above. */
const REFUSAL_STATUSES = new Set([401, 404, 429]);
/** …and after this many CONSECUTIVE failures of any other kind the timer stops too. */
const MAX_CONSECUTIVE_FAILURES = 3;

interface Cached {
  payload: unknown;
  at: number;
  error: string | null;
}

type Store = Record<string, Cached>;

interface Params {
  [k: string]: string | number | undefined;
}

interface Read {
  path: string;
  params?: Params;
}

interface Filters {
  status: string;
  window: string;
  limit: number;
  kind: string;
  source: 'events' | 'beampay';
  search: string;
}

const BLANK: Filters = { status: '', window: 'all', limit: LIMITS[0], kind: '', source: 'events', search: '' };

function sinceOf(f: Filters, now: number): number | undefined {
  const w = WINDOWS.find((x) => x.id === f.window);
  return w && w.seconds ? Math.floor(now / 1000 - w.seconds) : undefined;
}

/** What a tab reads: its OWN payload first (the only read allowed to re-gate), then context. */
function planFor(tab: TabId, f: Filters, now: number, store: Store, force: boolean): { primary: Read; extras: Read[] } {
  const since = sinceOf(f, now);
  const list = (path: string, extra: Params = {}): Read => ({ path, params: { limit: f.limit, since, ...extra } });
  const stale = (path: string, ttl: number) => force || !store[path] || now - store[path].at > ttl;

  switch (tab) {
    case 'deposits':
      return { primary: list('/deposits', { status: f.status || undefined }), extras: overviewExtra(store, now, force) };
    case 'payouts':
      return { primary: list('/payouts', { status: f.status || undefined }), extras: overviewExtra(store, now, force) };
    case 'unattributed':
      return { primary: list('/unattributed', { status: f.status || undefined }), extras: overviewExtra(store, now, force) };
    case 'accounts':
      return { primary: list('/accounts'), extras: overviewExtra(store, now, force) };
    case 'events':
      return { primary: list(f.source === 'beampay' ? '/beampay-events' : '/events'), extras: overviewExtra(store, now, force) };
    case 'treasury':
      return { primary: { path: '/treasury' }, extras: overviewExtra(store, now, force) };
    case 'overview':
    default:
      return {
        primary: { path: '/overview' },
        extras: [
          { path: '/deposits', params: { limit: 200 } },
          { path: '/payouts', params: { limit: 200 } },
          { path: '/unattributed', params: { limit: 200 } },
          { path: '/events', params: { limit: 25 } },
          ...(stale('/treasury', TREASURY_TTL_MS) ? [{ path: '/treasury' }] : []),
        ],
      };
  }
}

function overviewExtra(store: Store, now: number, force: boolean): Read[] {
  return force || !store['/overview'] || now - store['/overview'].at > OVERVIEW_TTL_MS ? [{ path: '/overview' }] : [];
}

// ────────────────────────────────────────────────────────────────────────────── the gate

function KeyGate({ onUnlocked, reason }: { onUnlocked: (payload: unknown) => void; reason?: string | null }) {
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const shown = error ?? reason ?? null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!key.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await unlock(key.trim());
      setKey('');
      onUnlocked(payload);
    } catch (err) {
      setError(gateMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ops ops-gate">
      <form onSubmit={submit} data-testid="admin-gate">
        <div className="ops-title">Operator</div>
        <p className="ops-sub">
          This page holds nothing of its own and is linked from nowhere. It reads {ADMIN_BASE} with the key you were sent; the key stays in
          this tab and is never put in a URL.
        </p>
        <div>
          <label htmlFor="admin-key">Access key</label>
          <input
            id="admin-key"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={key}
            onChange={(e) => setKey(e.target.value)}
            data-testid="admin-key-input"
          />
        </div>
        <button type="submit" className="ops-btn primary" disabled={busy || !key.trim()} data-testid="admin-unlock">
          {busy ? 'Checking…' : 'Unlock'}
        </button>
        {shown ? (
          <p className="err" data-testid="admin-gate-error">
            {shown}
          </p>
        ) : null}
      </form>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────────────── messages

function tabError(e: unknown): string {
  if (e instanceof AdminError) {
    if (e.nonJson) return `${ADMIN_BASE}${e.path} answered, but not with JSON — there is no admin API at that path.`;
    if (e.status === 429) return 'The API is rate-limiting this address (429). Wait before refreshing again.';
    if (e.status === 0) return 'The API is unreachable from this browser.';
    if (e.status === 404) return `404 at ${ADMIN_BASE}${e.path} — this build may not serve that route, or the key is no longer accepted.`;
    return `${e.status} at ${ADMIN_BASE}${e.path} — ${e.detail}`;
  }
  return 'The request failed.';
}

function relockMessage(e: AdminError): string {
  const where = `${ADMIN_BASE}${e.path}`;
  if (e.status === 429)
    return `The API is rate-limiting this address (429 at ${where}). The key was forgotten and the auto-refresh stopped — wait for the window to pass, then unlock once.`;
  if (e.status === 401) return 'This tab has no key any more. Paste it again to unlock.';
  return `Refused: ${where} answered ${e.status}. The key in this tab is no longer accepted, so it has been forgotten and the auto-refresh stopped — unlock again with the key you were sent.`;
}

function ThemeButton() {
  const [theme, setLocal] = useState<ThemeChoice>(() => currentTheme());
  const next: ThemeChoice = theme === 'dark' ? 'light' : 'dark';
  return (
    <button
      type="button"
      className="ops-btn"
      onClick={() => {
        setTheme(next);
        setLocal(next);
      }}
      aria-label={`Switch to the ${next} theme`}
      data-testid="admin-theme"
    >
      {theme === 'dark' ? 'Light' : 'Dark'}
    </button>
  );
}

// ───────────────────────────────────────────────────────────────────────── status strip

/**
 * One reading in the status strip. `short` is what a 390 px phone shows: the strip has to wrap to
 * two rows there, and it only can if the labels shrink — "payouts direct armed" becomes "direct
 * armed", "updated" disappears and the clock stays.
 */
function Stat({ tone, k, v, short, shortV, title }: { tone: Tone; k: string; v: string; short?: string; shortV?: string; title?: string }) {
  const swap = (long: string, brief?: string) =>
    brief === undefined ? (
      long
    ) : (
      <>
        <span className="long">{long}</span>
        <span className="short">{brief}</span>
      </>
    );
  return (
    <span className={`ops-stat${tone === 'crit' ? ' is-crit' : tone === 'warn' ? ' is-warn' : ''}`} title={title} data-stat={k}>
      <Dot tone={tone} />
      {swap(k, short)} <b>{swap(v, shortV)}</b>
    </span>
  );
}

function Strip({ overview, at, now }: { overview: unknown; at: number | null; now: number }) {
  const o = isPlainObject(overview) ? overview : {};
  const health = isPlainObject(o.health) ? o.health : {};
  const flags = isPlainObject(o.flags) ? o.flags : {};
  const workers = isPlainObject(o.workers) ? o.workers : {};
  const kill = o.kill_switch;
  const engaged = kill === true || (isPlainObject(kill) && kill.engaged === true);
  const lastPass = isPlainObject(workers.last_pass) ? workers.last_pass : {};
  const ages = Object.values(lastPass)
    .map((v) => (isPlainObject(v) ? num(v.at) : null))
    .filter((n): n is number => n !== null)
    .map((s) => now / 1000 - s);
  const freshest = ages.length ? Math.min(...ages) : null;
  const stats = isPlainObject(lastPass.stats_refresher) ? lastPass.stats_refresher : {};
  const height = num(pick(stats, 'detail.height'));
  const heightAge = num(stats.at) === null ? null : now / 1000 - (num(stats.at) as number);

  const on = (v: unknown, yes: string, no: string): [Tone, string] => (v === true ? ['good', yes] : ['neutral', no]);
  const [ingressTone, ingressWord] = on(flags.ingress_armed, 'armed', 'dark');
  const [directTone, directWord] = on(flags.payout_direct_enabled, 'armed', 'dark');
  const [instantTone, instantWord] = on(flags.payout_instant_enabled, 'on', 'off');

  return (
    <div className="ops-strip" data-testid="admin-strip">
      <Stat
        tone={health.ok === true ? 'good' : 'crit'}
        k="API"
        v={health.ok === true ? 'ok' : 'not ok'}
        title={`env ${str(o.env)} · version ${str(o.version)}`}
      />
      <Stat
        tone={workers.enabled === false || workers.paused === true ? 'crit' : freshest === null ? 'warn' : freshest > 600 ? 'warn' : 'good'}
        k="workers"
        v={
          workers.enabled === false
            ? 'disabled'
            : workers.paused === true
              ? 'paused'
              : freshest === null
                ? 'no trace'
                : `last pass ${span(freshest)} ago`
        }
        shortV={workers.enabled === false ? 'off' : workers.paused === true ? 'paused' : freshest === null ? 'no trace' : span(freshest)}
        title="the newest durable row any worker loop is the only writer of — not a heartbeat"
      />
      <Stat
        tone={engaged ? 'crit' : 'good'}
        k="kill switch"
        short="kill"
        v={engaged ? 'ENGAGED' : 'clear'}
        title={isPlainObject(kill) ? str(kill.file) : undefined}
      />
      <Stat tone={ingressTone} k="ingress" v={ingressWord} />
      <Stat tone={directTone} k="payouts direct" short="direct" v={directWord} />
      <Stat tone={instantTone} k="instant" v={instantWord} />
      <Stat
        tone={height === null ? 'warn' : heightAge !== null && heightAge > 600 ? 'warn' : 'good'}
        k="chain"
        v={height === null ? 'unknown' : count(height)}
        title="the tip the stats refresher last stored (stats/pool)"
      />
      <span className="ops-stat" data-testid="admin-updated">
        <span className="long">updated </span>
        <b className="n">{at ? clock(at / 1000) : '–'}</b>
      </span>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────── the page

export function AdminPage() {
  const [unlocked, setUnlocked] = useState<boolean>(() => getAdminKey() !== null);
  const [tab, setTab] = useState<TabId>('overview');
  const [store, setStore] = useState<Store>({});
  /**
   * ⛔ THE SAME STORE, AS A REF, BECAUSE THE TWO TTLs ABOVE ARE READ FROM INSIDE `load`.
   * `load` is a callback with `store` in its closure; if it read the STATE it would read the one
   * captured when the callback was built — for ever empty — and would re-fetch `/overview` and
   * `/treasury` on every single pass, which is the opposite of what a TTL is for. One writer, two
   * readers: `putStore` writes both, the ref is what a decision reads and the state is what the
   * screen renders.
   */
  const storeRef = useRef<Store>({});
  const [filters, setFilters] = useState<Filters>(BLANK);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadedAt, setLoadedAt] = useState<number | null>(null);
  const [auto, setAuto] = useState(true);
  const [gateReason, setGateReason] = useState<string | null>(null);
  const [open, setOpen] = useState<{ row: Row; path: string | null; title: string } | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const putStore = useCallback((path: string, cached: Cached) => {
    storeRef.current = { ...storeRef.current, [path]: cached };
    setStore(storeRef.current);
  }, []);

  const reqId = useRef(0);
  /** the gate already read `/overview`; the first load must not read it again */
  const seed = useRef<{ path: string; payload: unknown } | null>(null);
  /** what the screen was loaded for — a tab or a filter change is a genuine reason to re-read */
  const have = useRef<string | null>(null);
  /** consecutive failed PRIMARY reads. Reset by any success. */
  const fails = useRef(0);

  useLayoutEffect(() => {
    const prevTitle = document.title;
    document.title = 'Operator — Pgas.me';
    const meta = document.querySelector('meta[name="robots"]');
    const prev = meta?.getAttribute('content') ?? null;
    meta?.setAttribute('content', 'noindex,nofollow,noarchive,nosnippet');
    return () => {
      document.title = prevTitle;
      if (meta && prev !== null) meta.setAttribute('content', prev);
    };
  }, []);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(t);
  }, []);

  const loadKey = `${tab}|${filters.status}|${filters.window}|${filters.limit}|${filters.source}`;

  /** Back to the gate, key forgotten, timer stopped. ONE place — see rule 3. */
  const relock = useCallback((reason: string | null) => {
    forgetAdminKey();
    fails.current = 0;
    reqId.current += 1; // an in-flight read must not repaint the page we are leaving
    seed.current = null;
    setAuto(false);
    storeRef.current = {};
    setStore({});
    setLoadedAt(null);
    setError(null);
    setLoading(false);
    setOpen(null);
    have.current = null;
    setGateReason(reason);
    setUnlocked(false);
  }, []);

  const load = useCallback(
    async (force = false) => {
      const id = ++reqId.current;
      const at = Date.now();
      setLoading(true);
      const plan = planFor(tab, filters, at, storeRef.current, force);

      // ── the PRIMARY read: the only one that may decide this tab's key is not a key
      try {
        let payload: unknown;
        if (seed.current && seed.current.path === plan.primary.path) {
          payload = seed.current.payload;
          seed.current = null;
        } else {
          payload = await adminGet<unknown>(plan.primary.path, { params: plan.primary.params });
        }
        if (reqId.current !== id) return;
        putStore(plan.primary.path, { payload, at: Date.now(), error: null });
        setError(null);
        setLoadedAt(Date.now());
        have.current = loadKey;
        fails.current = 0;
      } catch (e) {
        if (reqId.current !== id) return;
        if (e instanceof AdminError && REFUSAL_STATUSES.has(e.status)) {
          relock(relockMessage(e));
          return;
        }
        fails.current += 1;
        setError(tabError(e));
        if (fails.current >= MAX_CONSECUTIVE_FAILURES) setAuto(false);
        setLoading(false);
        return; // the extras are context for a screen that did not load
      }

      // ── the EXTRAS: context only. One of these failing degrades its own section and NEVER
      //    touches the key — see the ⛔ note at the top of this file.
      await Promise.allSettled(
        plan.extras.map(async (r) => {
          try {
            const payload = await adminGet<unknown>(r.path, { params: r.params });
            if (reqId.current !== id) return;
            putStore(r.path, { payload, at: Date.now(), error: null });
          } catch (e) {
            if (reqId.current !== id) return;
            const was = storeRef.current[r.path];
            putStore(r.path, { payload: was?.payload ?? null, at: was?.at ?? 0, error: tabError(e) });
          }
        }),
      );
      if (reqId.current === id) setLoading(false);
    },
    [tab, filters, loadKey, relock, putStore],
  );

  useEffect(() => {
    if (!unlocked) return;
    if (have.current === loadKey) return;
    void load();
  }, [unlocked, load, loadKey]);

  useEffect(() => {
    if (!unlocked || !auto) return;
    const t = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(t);
  }, [unlocked, auto, load]);

  const goTo = useCallback((next: TabId, status?: string) => {
    setOpen(null);
    setTab(next);
    setFilters((f) => ({ ...BLANK, limit: f.limit, status: status ?? '' }));
    have.current = null;
  }, []);

  const overview = store['/overview']?.payload ?? null;
  const overviewAt = store['/overview']?.at ?? null;

  if (!unlocked) {
    return (
      <KeyGate
        reason={gateReason}
        onUnlocked={(p) => {
          seed.current = { path: '/overview', payload: p };
          fails.current = 0;
          setGateReason(null);
          setTab('overview');
          setFilters(BLANK);
          setAuto(true);
          have.current = null;
          setUnlocked(true);
        }}
      />
    );
  }

  return (
    <div className="ops">
      <header className="ops-bar">
        <div className="ops-wrap">
          <div className="ops-bar-top">
            <span className="ops-mark">
              <img src="/logo-256.png" alt="" width={24} height={24} />
              <span className="ops-title">Operator</span>
            </span>
            <nav className="ops-tabs" aria-label="Console sections">
              {TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  className={tab === t.id ? 'on' : ''}
                  data-tab={t.id}
                  aria-current={tab === t.id ? 'page' : undefined}
                  onClick={() => goTo(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </nav>
            <div className="ops-tools">
              <label className="ops-check">
                <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} data-testid="admin-auto" />
                auto 30 s
              </label>
              <button type="button" className="ops-btn" onClick={() => void load(true)} data-testid="admin-refresh">
                {loading ? 'Reading…' : 'Refresh'}
              </button>
              <ThemeButton />
              <button type="button" className="ops-btn is-crit" data-testid="admin-forget" onClick={() => relock(null)}>
                Forget key
              </button>
            </div>
          </div>
          <Strip overview={overview} at={loadedAt} now={now} />
        </div>
      </header>

      <main className="ops-wrap ops-body">
        <KillBanner overview={overview} />
        {error ? (
          <div className="ops-banner" role="alert" data-testid="admin-error">
            <b>Error</b>
            <span>{error}</span>
          </div>
        ) : null}
        <Body
          tab={tab}
          store={store}
          filters={filters}
          setFilters={setFilters}
          now={now}
          overview={overview}
          overviewAt={overviewAt}
          goTo={goTo}
          onOpen={setOpen}
        />
      </main>

      {open ? <RowDrawer row={open.row} path={open.path} title={open.title} onClose={() => setOpen(null)} /> : null}
    </div>
  );
}

function KillBanner({ overview }: { overview: unknown }) {
  const o = isPlainObject(overview) ? overview : {};
  const kill = o.kill_switch;
  const engaged = kill === true || (isPlainObject(kill) && kill.engaged === true);
  if (!engaged) return null;
  return (
    <div className="ops-banner" role="status" data-testid="admin-killswitch">
      <b>Kill switch engaged.</b>
      <span>
        {isPlainObject(kill) && str(kill.file) ? `${str(kill.file)} exists — ` : ''}the workers pause and the request path refuses.
      </span>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────── the tab body

interface BodyProps {
  tab: TabId;
  store: Store;
  filters: Filters;
  setFilters: (f: (p: Filters) => Filters) => void;
  now: number;
  overview: unknown;
  overviewAt: number | null;
  goTo: (t: TabId, status?: string) => void;
  onOpen: (o: { row: Row; path: string | null; title: string } | null) => void;
}

function Body(p: BodyProps) {
  const { tab, store, filters, setFilters } = p;
  const cached = (path: string) => store[path]?.payload ?? null;
  const search = filters.search.trim().toLowerCase();
  const filtered = (rows: Row[]) => (search ? rows.filter((r) => JSON.stringify(r).toLowerCase().includes(search)) : rows);

  if (tab === 'overview') {
    const payload = cached('/overview');
    if (payload === null) return <Empty>Reading the console…</Empty>;
    return (
      <Overview
        overview={payload}
        deposits={rowsOf(cached('/deposits'), 'deposits')}
        payouts={rowsOf(cached('/payouts'), 'payouts', 'requests')}
        locks={rowsOf(cached('/unattributed'), 'unattributed_locks', 'unattributed', 'locks')}
        events={rowsOf(cached('/events'), 'events')}
        treasury={cached('/treasury')}
        treasuryAt={store['/treasury']?.at ?? null}
        treasuryError={store['/treasury']?.error ?? null}
        now={p.now}
        goTo={p.goTo}
      />
    );
  }

  if (tab === 'treasury') {
    const payload = cached('/treasury');
    const o = isPlainObject(p.overview) ? p.overview : {};
    const health = isPlainObject(o.health) ? o.health : {};
    const coins = (isPlainObject(health.coins) ? health.coins : {}) as Record<string, { have?: number | null; target?: number | null }>;
    const workers = isPlainObject(o.workers) ? o.workers : {};
    const lastPass = isPlainObject(workers.last_pass) ? workers.last_pass : {};
    const chainHeight = num(pick(lastPass, 'stats_refresher.detail.height'));
    if (payload === null) return <Empty>Reading the wallet — this is a live BeamPay and wallet-api read…</Empty>;
    return <Treasury payload={payload} coinTargets={coins} chainHeight={chainHeight} now={p.now} />;
  }

  const conf = LISTS[tab];
  const payload = cached(conf.path(filters));
  const all = rowsOf(payload, ...conf.keys);
  const rows = filtered(all);
  const envelope = isPlainObject(payload) ? payload : {};
  const statuses = conf.statuses(envelope, all);
  const total = num(envelope.total);

  return (
    <Section
      eyebrow={conf.label}
      id={tab}
      note={
        <span data-testid="admin-count">
          {rows.length === all.length ? `${count(all.length)} loaded` : `${count(rows.length)} of ${count(all.length)} loaded`}
          {total !== null && total !== all.length ? ` · ${count(total)} on the server` : ''}
        </span>
      }
    >
      <div className="ops-filters">
        <input
          className="ops-input"
          type="search"
          placeholder="Search loaded rows"
          aria-label="Search loaded rows"
          value={filters.search}
          onChange={(e) => setFilters((f) => ({ ...f, search: e.target.value }))}
          data-testid="admin-search"
        />
        {statuses.length > 1 && tab !== 'events' ? (
          <select
            className="ops-select"
            aria-label="Status"
            value={filters.status}
            onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value }))}
          >
            <option value="">any status</option>
            {statuses.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        ) : null}
        <div className="ops-chips">
          {WINDOWS.map((w) => (
            <button
              key={w.id}
              type="button"
              className={`ops-chip${filters.window === w.id ? ' on' : ''}`}
              data-window={w.id}
              onClick={() => setFilters((f) => ({ ...f, window: w.id }))}
            >
              {w.label}
            </button>
          ))}
        </div>
        <select
          className="ops-select"
          aria-label="Rows to load"
          value={filters.limit}
          onChange={(e) => setFilters((f) => ({ ...f, limit: Number(e.target.value) }))}
        >
          {LIMITS.map((n) => (
            <option key={n} value={n}>
              load {n}
            </option>
          ))}
        </select>
        {tab === 'events' ? (
          <div className="ops-chips">
            {(['events', 'beampay'] as const).map((s) => (
              <button
                key={s}
                type="button"
                className={`ops-chip${filters.source === s ? ' on' : ''}`}
                data-events-source={s}
                onClick={() => setFilters((f) => ({ ...f, source: s, kind: '' }))}
              >
                {s === 'events' ? 'operator events' : 'BeamPay webhooks'}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      {payload === null ? (
        <Empty>Reading…</Empty>
      ) : tab === 'deposits' ? (
        <DepositsTable
          rows={rows}
          onOpen={(r) => p.onOpen({ row: r, path: `/deposits/${encodeURIComponent(idOf(r))}`, title: `Deposit ${idOf(r)}` })}
        />
      ) : tab === 'payouts' ? (
        <PayoutsTable
          rows={rows}
          onOpen={(r) => p.onOpen({ row: r, path: `/payouts/${encodeURIComponent(idOf(r))}`, title: `Order ${idOf(r)}` })}
        />
      ) : tab === 'unattributed' ? (
        <LocksTable rows={rows} onOpen={(r) => p.onOpen({ row: r, path: null, title: `Lock ${idOf(r)}` })} />
      ) : tab === 'accounts' ? (
        <AccountsTable rows={rows} onOpen={(r) => p.onOpen({ row: r, path: null, title: `Account ${idOf(r)}` })} />
      ) : (
        <EventsTimeline
          rows={filters.kind ? rows.filter((r) => str(pick(r, 'kind', 'event')) === filters.kind) : rows}
          kinds={statuses}
          kind={filters.kind}
          onKind={(k) => setFilters((f) => ({ ...f, kind: k }))}
        />
      )}
    </Section>
  );
}

/** Each list tab's path, its envelope's row keys, and where its filter vocabulary comes from. */
const LISTS: Record<
  string,
  { label: string; path: (f: Filters) => string; keys: string[]; statuses: (env: Row, rows: Row[]) => string[] }
> = {
  deposits: {
    label: 'Deposits',
    path: () => '/deposits',
    keys: ['deposits'],
    statuses: (env, rows) => listOf(env.statuses) ?? distinct(rows, 'status'),
  },
  payouts: {
    label: 'Payouts',
    path: () => '/payouts',
    keys: ['payouts', 'requests'],
    statuses: (env, rows) => listOf(env.statuses) ?? distinct(rows, 'status'),
  },
  unattributed: {
    label: 'Unattributed locks',
    path: () => '/unattributed',
    keys: ['unattributed_locks', 'unattributed', 'locks'],
    statuses: (env, rows) => listOf(env.statuses) ?? distinct(rows, 'status'),
  },
  accounts: { label: 'Accounts', path: () => '/accounts', keys: ['accounts'], statuses: () => [] },
  events: {
    label: 'Events',
    path: (f) => (f.source === 'beampay' ? '/beampay-events' : '/events'),
    keys: ['events', 'beampay_events', 'webhooks'],
    statuses: (env, rows) => listOf(env.kinds) ?? distinct(rows, 'kind', 'event'),
  },
};

function listOf(v: unknown): string[] | null {
  return Array.isArray(v) && v.every((x) => typeof x === 'string') ? (v as string[]) : null;
}

function distinct(rows: Row[], ...keys: string[]): string[] {
  const s = new Set<string>();
  for (const r of rows) {
    const v = str(pick(r, ...keys));
    if (v) s.add(v);
  }
  return [...s].sort();
}

export { stamp };
