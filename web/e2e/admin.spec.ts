/**
 * The operator console (`/admin`, T38, redesigned T49).
 *
 * The SECURITY tests below are T38/T38b's and their BEHAVIOUR is unchanged — a wrong key is a 404
 * that teaches nothing, the fifth failure is a rate limit and one page, a refusal re-gates and
 * stops the timer, a 200 that is not JSON is not an unlock, a 5xx is the API's trouble and not a
 * verdict on the key, and nothing in the public app points here. What T49 changed is what the page
 * DRAWS, so those tests changed selectors and nothing else.
 *
 * Two assertions changed shape rather than meaning, and both are called out where they sit:
 * the Overview now reads the rows behind its attention list, so a per-path call count is asserted
 * as a DELTA; and the auto-refresh switch lives in the top bar on every tab rather than in a
 * per-tab toolbar.
 */
import { expect, test, type Page } from '@playwright/test';
import { ADMIN_KEY, MockAdminApi, NOW_S, WRONG_KEY } from './admin-mocks';
import { blockExternal } from './mocks';

const KEY_STORAGE = 'pgas.admin.key.v1';
const OUT = 'e2e/screenshots';

async function boot(page: Page, admin: MockAdminApi | null, opts: { assets?: boolean } = {}) {
  await blockExternal(page, opts.assets ?? false);
  if (admin) await admin.install(page);
  await page.goto('/admin');
  await page.getByTestId('admin-gate').waitFor();
}

async function typeKey(page: Page, key: string) {
  await page.getByTestId('admin-key-input').fill(key);
  await page.getByTestId('admin-unlock').click();
}

async function unlock(page: Page) {
  await typeKey(page, ADMIN_KEY);
  await page.getByTestId('admin-object').waitFor();
}

async function storedKey(page: Page): Promise<string | null> {
  return page.evaluate((k) => sessionStorage.getItem(k), KEY_STORAGE);
}

async function openTab(page: Page, tab: string) {
  await page.locator(`[data-tab="${tab}"]`).click();
}

/** The Overview's own reads settle after the gate's seeded payload paints; wait for the money
 *  table, which is the last of them (`/treasury`). */
async function settled(page: Page) {
  await expect(page.locator('[data-section="money"]')).toContainText('BEAM');
}

test.describe('operator console', () => {
  let errors: string[];

  test.beforeEach(async ({ page }) => {
    errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
  });

  test.afterEach(() => {
    expect(errors).toEqual([]);
  });

  // ─────────────────────────────────────────────────────────────────────── the key gate

  test('a wrong key is refused, learns nothing, and is never stored', async ({ page }) => {
    const admin = new MockAdminApi();
    await boot(page, admin);
    await typeKey(page, WRONG_KEY);
    await expect(page.getByTestId('admin-gate-error')).toContainText('Refused');
    // the console never says WHY — the API answers 404 to a wrong key on purpose
    await expect(page.getByTestId('admin-gate-error')).toContainText('404');
    await expect(page.getByTestId('admin-gate')).toBeVisible();
    expect(await storedKey(page)).toBeNull();
    expect(admin.calls).toEqual([]);
    expect(admin.failures).toBe(1);
  });

  test('the fifth failure is a rate limit, and the operator is paged once', async ({ page }) => {
    const admin = new MockAdminApi();
    await boot(page, admin);
    for (let i = 0; i < 4; i++) {
      await typeKey(page, `${WRONG_KEY}-${i}`);
      await expect(page.getByTestId('admin-gate-error')).toContainText('Refused');
    }
    await typeKey(page, WRONG_KEY);
    await expect(page.getByTestId('admin-gate-error')).toContainText('rate-limiting');
    expect(admin.failures).toBe(5);
    expect(admin.pagesSent).toBe(1);
    expect(await storedKey(page)).toBeNull();
  });

  test('a 200 that is not JSON is not an unlock', async ({ page }) => {
    /**
     * The SPA fallback turns every unrouted path into `200 text/html` (Pgas.me law #4). On a host
     * without the `/api` proxy that is exactly what `/api/admin/overview` would answer, and status
     * alone would read as success — the console would look unlocked, empty, and would have STORED
     * the key. The body is the evidence, so an HTML 200 is a refusal.
     */
    await blockExternal(page);
    await page.route(/\/api\/admin\//, (route) =>
      route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><html><body><div id="root"></div></body></html>' }),
    );
    await page.goto('/admin');
    await page.getByTestId('admin-gate').waitFor();
    await typeKey(page, ADMIN_KEY);
    await expect(page.getByTestId('admin-gate-error')).toContainText('not with JSON');
    await expect(page.getByTestId('admin-gate')).toBeVisible();
    expect(await storedKey(page)).toBeNull();
  });

  test('a server error is reported as a server error, not as a wrong key', async ({ page }) => {
    await blockExternal(page);
    await page.route(/\/api\/admin\//, (route) => route.fulfill({ status: 502, contentType: 'application/json', body: '{}' }));
    await page.goto('/admin');
    await page.getByTestId('admin-gate').waitFor();
    await typeKey(page, ADMIN_KEY);
    await expect(page.getByTestId('admin-gate-error')).toContainText('502');
    expect(await storedKey(page)).toBeNull();
  });

  test('forgetting the key returns to the gate and clears the tab', async ({ page }) => {
    const admin = new MockAdminApi();
    await boot(page, admin);
    await unlock(page);
    await page.getByTestId('admin-forget').click();
    await expect(page.getByTestId('admin-gate')).toBeVisible();
    expect(await storedKey(page)).toBeNull();
    await page.reload();
    await expect(page.getByTestId('admin-gate')).toBeVisible();
  });

  test('a key that stops being accepted re-gates the page and stops the refresh', async ({ page }) => {
    /**
     * ⛔ The stale-key trap. The key lives in `sessionStorage`, so a tab left open across a key
     * rotation (or a restart that cleared the API's window) stayed "unlocked": the page kept its
     * chrome, kept its tables, and kept firing a read every 30 s FOR EVER. Each one is a refusal
     * row on the API and the fifth in a window pages the operator — a pager fired by our own UI.
     *
     * A refusal (401 / 404 / 429) means the key in this tab is not a key: it is forgotten, the
     * gate comes back carrying the reason, and the timer stops.
     */
    const admin = new MockAdminApi();
    await page.clock.install({ time: new Date(NOW_S * 1000) });
    await boot(page, admin);
    await unlock(page);
    await settled(page);
    await openTab(page, 'deposits');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);
    const before = admin.countOf('/deposits');

    // the key is rotated on the box: every further read answers 404, exactly as a wrong key does
    admin.key = 'the-key-was-rotated-on-the-box';

    await page.clock.runFor(31_000);
    await expect(page.getByTestId('admin-gate')).toBeVisible();
    await expect(page.getByTestId('admin-gate-error')).toContainText('404');
    expect(await storedKey(page)).toBeNull();

    // ⛔ EXACTLY ONE REFUSAL. The tab fires its PRIMARY read first and alone; the extras that feed
    // the status strip never go out when the primary refused, so a stale key cannot spend the
    // API's five-per-window budget in a single pass and page the operator with our own UI.
    expect(admin.failures).toBe(1);

    // …and it does not keep knocking: one refusal, then silence, however long the clock runs
    await page.clock.runFor(600_000);
    expect(admin.failures).toBe(1);
    expect(admin.countOf('/deposits')).toBe(before);

    // the operator pastes the new key and the console comes back, refreshing again
    await typeKey(page, admin.key);
    await page.getByTestId('admin-object').waitFor();
    expect(await storedKey(page)).toBe(admin.key);
    await expect(page.getByTestId('admin-auto')).toBeChecked();
  });

  test('a tab that keeps failing stops refreshing instead of knocking every 30 s', async ({ page }) => {
    /**
     * A 500 is the API's own trouble and NOT a verdict on the key, so the page stays unlocked and
     * keeps the key — but it stops the timer after three consecutive failures rather than
     * hammering a sick API twice a minute for the rest of the afternoon.
     */
    const admin = new MockAdminApi();
    await page.clock.install({ time: new Date(NOW_S * 1000) });
    await boot(page, admin);
    await unlock(page);
    await settled(page);
    await openTab(page, 'deposits');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);

    let hits = 0;
    await page.route(/\/api\/admin\/deposits/, (route) => {
      hits += 1;
      return route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' });
    });
    for (let i = 1; i <= 3; i++) {
      await page.clock.runFor(31_000);
      await expect.poll(() => hits).toBe(i);
    }
    await expect(page.getByTestId('admin-error')).toContainText('500');
    await expect(page.getByTestId('admin-auto')).not.toBeChecked();
    await page.clock.runFor(600_000);
    expect(hits).toBe(3);
    await expect(page.getByTestId('admin-gate')).toHaveCount(0);
    expect(await storedKey(page)).toBe(ADMIN_KEY);
  });

  test('nothing in the public app points at /admin, and the route is noindex', async ({ page, request }) => {
    await blockExternal(page);
    await page.goto('/');
    await page.getByTestId('deposit-form').waitFor();
    const hrefs = await page.locator('a').evaluateAll((as) => as.map((a) => a.getAttribute('href') ?? ''));
    expect(hrefs.filter((h) => /admin/i.test(h))).toEqual([]);
    expect(await page.getByRole('link', { name: /admin/i }).count()).toBe(0);
    expect(await page.getByRole('button', { name: /admin|operator/i }).count()).toBe(0);
    const tabs = await page.locator('[data-tab]').evaluateAll((els) => els.map((e) => e.getAttribute('data-tab')));
    expect(tabs.filter((t) => t === 'admin')).toEqual([]);
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute('content', /^index,follow/);

    await page.goto('/admin');
    await page.getByTestId('admin-gate').waitFor();
    await expect(page.locator('meta[name="robots"]')).toHaveAttribute('content', /noindex/);
    await expect(page).toHaveTitle(/Operator/);

    // and the sitemap does not carry it (law #4: read the content, not the status)
    const res = await request.get('/sitemap.xml');
    expect(res.status()).toBe(200);
    expect(res.headers()['content-type']).toContain('xml');
    expect(await res.text()).not.toContain('admin');
  });

  // ──────────────────────────────────────────────────────────────────── the console itself

  test('the right key unlocks, and the Overview opens on what needs the operator', async ({ page }) => {
    const admin = new MockAdminApi();
    await page.clock.setFixedTime(new Date(NOW_S * 1000));
    await boot(page, admin);
    await unlock(page);

    expect(await storedKey(page)).toBe(ADMIN_KEY);
    // the gate's own read IS the first screen: no second /overview on the way in
    expect(admin.countOf('/overview')).toBe(1);

    // ATTENTION FIRST: every critical item above every warning, newest first inside each tone
    const needs = page.locator('[data-need]');
    await expect(needs.first()).toHaveClass(/is-crit/);
    const classes = await needs.evaluateAll((els) => els.map((e) => e.className));
    const firstWarn = classes.findIndex((c) => c.includes('is-warn'));
    expect(firstWarn).toBeGreaterThan(0);
    expect(classes.slice(firstWarn).some((c) => c.includes('is-crit'))).toBe(false);
    await expect(page.locator('[data-need="pay-held-req_5504"]')).toContainText('no free BEAM fee coin');
    await expect(page.locator('[data-need="lock-lock_138"]')).toContainText('A human must attribute it');
    await expect(page.locator('[data-need="coins-DAI"]')).toContainText('0 of 12');
    await expect(page.locator('[data-need="coins-BEAM"]')).toContainText('beam split');
    await expect(page.locator('[data-need="tokens-stale"]')).toContainText('token lists');
    await expect(page.locator('[data-need="unsent"]')).toContainText('not sent');
    // MONEY in human units — never groth, never wei
    await settled(page);
    const money = page.locator('[data-section="money"]');
    // the unit is named once per row, and every figure beside it is a human amount
    await expect(money.locator('tr[data-asset="ETH"]')).toContainText('bETH');
    await expect(money.locator('tr[data-asset="ETH"]')).toContainText('0.00175681'); // treasury, from 175,681 groth
    await expect(money).toContainText('9.306'); // BEAM for fees, from 930,600,000 groth
    await expect(money).toContainText('≈ 1 crossing'); // …and it is not "1 crossings"
    await expect(money).toContainText('of 20'); // fee coins free, against the target
    // an unreadable balance is the word, never a zero
    await expect(money.locator('tr[data-asset="WBTC"]')).toContainText('unreadable');

    // the last day, and the activity timeline
    await expect(page.locator('[data-section="day"]')).toContainText('deposits credited');
    await expect(page.locator('[data-section="activity"]')).toContainText('req_5502 released');

    // the strip states the posture, and no raw epoch appears anywhere on the page
    // (a strip reading carries both its full and its phone spelling; only one is ever displayed,
    //  so the assertions name the reading rather than the concatenated text)
    const strip = page.getByTestId('admin-strip');
    await expect(strip.locator('[data-stat="kill switch"]')).toContainText('clear');
    await expect(strip.locator('[data-stat="ingress"]')).toContainText('armed');
    await expect(strip.locator('[data-stat="chain"]')).toContainText('4,031,015');
    await expect(page.getByTestId('admin-updated')).toContainText('Z');
    expect(await page.locator('body').innerText()).not.toContain(String(NOW_S));
  });

  test('nothing needing the operator says so in words', async ({ page }) => {
    const admin = new MockAdminApi();
    await page.clock.setFixedTime(new Date(NOW_S * 1000));
    await boot(page, admin);
    // a quiet box: no held or delayed order, no open lock, coins at target, lists fresh
    await page.route(/\/api\/admin\/overview/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          at: NOW_S,
          env: 'prod',
          health: {
            ok: true,
            coins: { BEAM: { have: 20, target: 20 } },
            tokens: { updated_at: NOW_S - 60, chains: 17, age_s: 60, failed: [] },
            gas: { samples_24h: 40 },
          },
          flags: { ingress_armed: true, ingress_ready: true, payout_direct_enabled: true, payout_instant_enabled: false },
          kill_switch: { file: '/etc/pgasme.stop', engaged: false },
          workers: { enabled: true, paused: false, last_pass: { monitor: { at: NOW_S - 12, source: 'events.notified_at' } } },
          watchdog: { log: '/var/log/pgasme/watch.log', line: 'all green', at: NOW_S - 20, why: null },
          counts: { events_unsent: 0 },
          crossings: { orders: 0, oldest_age_s: 0, queued_groth: 0 },
        }),
      }),
    );
    await page.route(/\/api\/admin\/(payouts|unattributed|deposits|events)(\?|$)/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ rows: [], total: 0, limit: 200, offset: 0, has_more: false }),
      }),
    );
    await unlock(page);
    await expect(page.locator('[data-section="attention"]')).toContainText('Nothing needs you.');
  });

  test('a kill switch in the payload is a banner, not a field', async ({ page }) => {
    const admin = new MockAdminApi();
    await boot(page, admin);
    await page.route(/\/api\/admin\/overview/, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true, kill_switch: true, counts: { deposits: 1, payouts: 2 } }),
      }),
    );
    await unlock(page);
    await expect(page.getByTestId('admin-killswitch')).toBeVisible();
    await expect(page.locator('[data-need="kill"]')).toContainText('kill switch is engaged');
  });

  test('every tab renders what it was designed to render', async ({ page }) => {
    const admin = new MockAdminApi();
    await page.clock.setFixedTime(new Date(NOW_S * 1000));
    await boot(page, admin);
    await unlock(page);
    await settled(page);

    // ── deposits: a ladder, human amounts, links on the row's own chain
    await openTab(page, 'deposits');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);
    await expect(page.locator('[data-row-id="dep_01H8ZQ7"]')).toContainText('shielded');
    await expect(page.locator('[data-row-id="dep_01H8ZQ7"]')).toContainText('0.0019999 bETH');
    await expect(page.locator('[data-row-id="dep_01H8ZS2"]')).toContainText('confirming 4/12');
    await expect(page.locator('[data-row-id="dep_01H8ZS9"]')).toContainText('failed');
    // the reason a row is STOPPED gets a stripe across the table, not a truncated cell — and a
    // row that is merely moving ("the claim is in flight") does NOT get one
    await expect(page.locator('.ops-stripe-in')).toHaveCount(1);
    await expect(page.locator('.ops-stripe-in')).toContainText('registration refused: to != pipe');
    // the registered hash links on the chain the row names — Base here, not Ethereum
    await expect(page.locator(`a[href="https://basescan.org/tx/0x${'a1'.repeat(32)}"]`)).toBeVisible();
    await expect(page.locator(`a[href="https://etherscan.io/tx/0x${'b2'.repeat(32)}"]`).first()).toBeVisible();
    // a Beam kernel id is shown and NEVER linked
    await expect(page.locator(`a[href*="${'d4'.repeat(32)}"]`)).toHaveCount(0);
    // and the bridge explorer keys on the Beam BLOCK, not on a kernel
    await expect(page.locator('a[href="https://beamterminal.0xmx.net/#/explorer/bridge?tx=4030880"]')).toBeVisible();

    // ── payouts: the ladder, the fee breakdown, the held/delayed stripes
    await openTab(page, 'payouts');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);
    await expect(page.locator('[data-row-id="req_5504"]')).toContainText('held');
    await expect(page.locator('.ops-stripe-in.is-crit')).toContainText('no free BEAM fee coin');
    await expect(page.locator('.ops-stripe-in', { hasText: 'delayed' })).toContainText('next try');
    await expect(page.locator('[data-row-id="req_5501"]')).toContainText('delivered');

    // ── unattributed: open first, and the question answered in words
    await openTab(page, 'unattributed');
    await expect(page.getByTestId('admin-row')).toHaveCount(3);
    await expect(page.getByTestId('admin-row').first()).toContainText('msg 138');
    await expect(page.getByTestId('admin-row').first()).toContainText('A human must attribute it');
    // the pipe's raw units are rendered in the asset's own decimals, never as 3000000000000000
    await expect(page.getByTestId('admin-row').first()).toContainText('0.003 bETH');
    expect(await page.getByTestId('admin-row').first().innerText()).not.toContain('3000000000000000');
    await expect(page.locator('[data-row-id="lock_143"]')).toContainText('the scanner tries again');

    // ── accounts
    await openTab(page, 'accounts');
    await expect(page.getByTestId('admin-row')).toHaveCount(3);
    await expect(page.getByTestId('admin-row').first()).toContainText('0.01248 bETH');

    // ── treasury: three tables that say three different things
    await openTab(page, 'treasury');
    await expect(page.locator('[data-section="balances"] tr[data-asset="ETH"]')).toContainText('0.01652864'); // maturing, still locked
    await expect(page.locator('[data-section="balances"] tr[data-asset="ETH"]')).toContainText('split · 1 of 12');
    await expect(page.locator('[data-section="beam-fees"]')).toContainText('two coins × 0.15');
    await expect(page.locator('[data-section="float-policy"]')).toContainText('0.05 BEAM');
    await expect(page.locator('[data-section="float-policy"]')).toContainText('10% buffer');
    await expect(page.locator('[data-section="wallet"]')).toContainText('in sync');
    await expect(page.locator('[data-section="addresses"]')).toContainText('deadbeef');
    await expect(page.locator('[data-section="in-flight"]')).toContainText('held payouts');

    // ── events, and the BeamPay webhook source beside them
    await openTab(page, 'events');
    await expect(page.locator('.ops-event')).toHaveCount(6);
    await expect(page.locator('.ops-event.unsent').first()).toBeVisible();
    await page.locator('[data-events-source="beampay"]').click();
    await expect(page.locator('.ops-event')).toHaveCount(3);
    await expect(page.locator('[data-section="events"]')).toContainText('dead-lettered');
    expect(admin.countOf('/beampay-events')).toBe(1);

    await openTab(page, 'overview');
    await expect(page.getByTestId('admin-object')).toBeVisible();
  });

  test('a row opens a drawer with what happened, the quote, and the raw row', async ({ page }) => {
    const admin = new MockAdminApi();
    await boot(page, admin);
    await unlock(page);
    await settled(page);
    await openTab(page, 'deposits');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);

    await page.locator('[data-row-id="dep_01H8ZQ7"]').click();
    const drawer = page.getByTestId('admin-drawer');
    await expect(drawer).toBeVisible();
    await expect(drawer).toContainText('Deposit dep_01H8ZQ7');
    await expect(drawer).toContainText('The quote it came from');
    await expect(drawer).toContainText('q_7712');
    await expect(drawer).toContainText('Ledger');
    await expect(drawer).toContainText('deposit_credit');
    // Raw is present, collapsed, and carries every field no column shows
    await expect(page.getByTestId('admin-raw')).toBeHidden();
    await drawer.locator('summary', { hasText: 'Raw' }).first().click();
    await expect(page.getByTestId('admin-raw')).toContainText('relayer_fee_units');
    expect(admin.countOf('/deposits/dep_01H8ZQ7')).toBe(1);

    await page.getByTestId('admin-drawer-close').click();
    await expect(page.getByTestId('admin-drawer')).toHaveCount(0);
  });

  test('an attention item jumps to the rows it is about', async ({ page }) => {
    const admin = new MockAdminApi();
    await boot(page, admin);
    await unlock(page);
    await settled(page);
    await page.locator('[data-need="pay-held-req_5504"] .ops-need-go').click();
    await expect(page.locator('[data-tab="payouts"]')).toHaveClass(/on/);
    // the tab arrives filtered to the status the item was about — one row, and it is that row
    await expect(page.getByTestId('admin-row')).toHaveCount(1);
    await expect(page.getByTestId('admin-row')).toContainText('req_5504');
    expect(admin.calls.some((c) => c.startsWith('/payouts?') && c.includes('status=held'))).toBe(true);
  });

  test('search narrows the loaded rows and the status filter is asked of the API', async ({ page }) => {
    const admin = new MockAdminApi();
    await boot(page, admin);
    await unlock(page);
    await settled(page);
    await openTab(page, 'deposits');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);

    await page.getByTestId('admin-search').fill('to != pipe');
    await expect(page.getByTestId('admin-row')).toHaveCount(1);
    await expect(page.getByTestId('admin-count')).toContainText('1 of 5 loaded');
    await page.getByTestId('admin-search').fill('');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);

    // the status filter is a SERVER filter: the window is a window of the collection, not of
    // whatever happened to be loaded
    await page.getByLabel('Status').selectOption('credited');
    await expect(page.getByTestId('admin-row')).toHaveCount(2);
    expect(admin.calls.some((c) => c.includes('status=credited'))).toBe(true);
    await page.getByLabel('Status').selectOption('');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);

    // …and so is the time window
    await page.locator('[data-window="24h"]').click();
    await expect.poll(() => admin.calls.some((c) => c.startsWith('/deposits?') && c.includes('since='))).toBe(true);
  });

  test('the open tab is re-read every 30 s, and stops when auto is off', async ({ page }) => {
    const admin = new MockAdminApi();
    await page.clock.install({ time: new Date(NOW_S * 1000) });
    await boot(page, admin);
    await unlock(page);
    await settled(page);
    await openTab(page, 'deposits');
    await expect(page.getByTestId('admin-row')).toHaveCount(5);

    /**
     * A DELTA, not an absolute. The Overview reads the deposit rows its attention list is derived
     * from, so `/deposits` has already been served once by the time this tab opens; what this test
     * is about is the CADENCE — one read per interval, none when the switch is off, one on demand.
     */
    const base = admin.countOf('/deposits');
    await page.clock.runFor(31_000);
    await expect.poll(() => admin.countOf('/deposits')).toBe(base + 1);
    await page.clock.runFor(31_000);
    await expect.poll(() => admin.countOf('/deposits')).toBe(base + 2);

    await page.getByTestId('admin-auto').uncheck();
    await page.clock.runFor(120_000);
    await expect.poll(() => admin.countOf('/deposits')).toBe(base + 2);

    await page.getByTestId('admin-refresh').click();
    await expect.poll(() => admin.countOf('/deposits')).toBe(base + 3);
  });

  test('the treasury read is on a slower cadence than the Mongo reads', async ({ page }) => {
    /**
     * ⚠️ `/admin/treasury` is a LIVE wallet read — one BeamPay `/balances` per registered address
     * per asset, one `/wallet_status`, one `get_utxo` walk. The Overview shows those numbers, so it
     * must not poll them twice a minute; it re-reads them slowly and stamps them with their age.
     */
    const admin = new MockAdminApi();
    await page.clock.install({ time: new Date(NOW_S * 1000) });
    await boot(page, admin);
    await unlock(page);
    await settled(page);
    expect(admin.countOf('/treasury')).toBe(1);

    for (let i = 0; i < 4; i++) await page.clock.runFor(31_000);
    await expect.poll(() => admin.countOf('/overview')).toBeGreaterThan(2);
    expect(admin.countOf('/treasury')).toBe(1); // two minutes of ticks, one wallet read

    // …and a manual Refresh always takes a fresh one
    await page.getByTestId('admin-refresh').click();
    await expect.poll(() => admin.countOf('/treasury')).toBe(2);
  });
});

/**
 * The phone pass. A table wider than the screen is fine — it scrolls inside its own box — but the
 * PAGE must never scroll sideways, and the drawer must become a sheet at the width of the screen
 * rather than the width of the table it was opened from.
 */
test('a phone can use every tab, and none of them scrolls the page sideways', async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  const errs: string[] = [];
  page.on('pageerror', (e) => errs.push(e.message));
  const admin = new MockAdminApi();
  await blockExternal(page);
  await admin.install(page);
  await page.goto('/admin');
  await page.getByTestId('admin-gate').waitFor();
  const sideways = () => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(await sideways()).toBeLessThanOrEqual(0);

  await page.getByTestId('admin-key-input').fill(ADMIN_KEY);
  await page.getByTestId('admin-unlock').click();
  await page.getByTestId('admin-object').waitFor();
  expect(await sideways()).toBeLessThanOrEqual(0);

  for (const t of ['deposits', 'payouts', 'unattributed', 'accounts', 'treasury', 'events']) {
    await page.locator(`[data-tab="${t}"]`).click();
    await expect(page.locator('.ops-table, .ops-timeline, [data-testid="admin-object"]').first()).toBeVisible();
    expect(await sideways(), `${t} scrolls the page sideways`).toBeLessThanOrEqual(0);
  }

  // ⛔ THE GUTTER SURVIVES THE PHONE. `.ops-body` and `.ops-wrap` are the same element, so a
  // `padding:` SHORTHAND in either rule silently resets the other's — which is exactly what
  // happened, and every table ran to the bare edge of the window.
  const gutter = await page.locator('main.ops-body').evaluate((el) => {
    const cs = getComputedStyle(el);
    return { left: cs.paddingLeft, right: cs.paddingRight, top: cs.paddingTop };
  });
  expect(gutter).toEqual({ left: '16px', right: '16px', top: '24px' });

  await page.locator('[data-tab="deposits"]').click();
  await expect(page.getByTestId('admin-row')).toHaveCount(5);
  await page.getByTestId('admin-row').first().click();
  const drawer = page.getByTestId('admin-drawer');
  await drawer.waitFor();
  expect(await drawer.evaluate((el) => el.getBoundingClientRect().width)).toBeLessThanOrEqual(390);
  expect(await sideways()).toBeLessThanOrEqual(0);

  expect(errs).toEqual([]);
  await ctx.close();
});

// ───────────────────────────────────────────────────────────── evidence screenshots

const TABS = ['overview', 'deposits', 'payouts', 'unattributed', 'accounts', 'treasury', 'events'] as const;

const VIEWPORTS: { name: string; viewport: { width: number; height: number }; isMobile: boolean; scheme: 'light' | 'dark' }[] = [
  { name: 'desktop', viewport: { width: 1280, height: 900 }, isMobile: false, scheme: 'light' },
  { name: 'mobile', viewport: { width: 390, height: 844 }, isMobile: true, scheme: 'light' },
  { name: 'desktop-dark', viewport: { width: 1280, height: 900 }, isMobile: false, scheme: 'dark' },
];

for (const vp of VIEWPORTS) {
  test(`admin screenshots ${vp.name}`, async ({ browser }) => {
    const ctx = await browser.newContext({
      viewport: vp.viewport,
      isMobile: vp.isMobile,
      hasTouch: vp.isMobile,
      deviceScaleFactor: vp.isMobile ? 2 : 1,
      colorScheme: vp.scheme,
    });
    const page = await ctx.newPage();
    const shotErrors: string[] = [];
    page.on('pageerror', (e) => shotErrors.push(e.message));
    const admin = new MockAdminApi();
    await blockExternal(page, true);
    await admin.install(page);
    // ⛔ `setFixedTime`, not `install`: the fixtures are stamped against NOW_S and every "23 s ago"
    // on screen has to be the one they were designed for — but the page's own timers must keep
    // running, or React never finishes the render this screenshot is of.
    await page.clock.setFixedTime(new Date(NOW_S * 1000));
    await page.goto('/admin');

    await page.getByTestId('admin-gate').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/admin-gate-${vp.name}.png` });

    await page.getByTestId('admin-key-input').fill(ADMIN_KEY);
    await page.getByTestId('admin-unlock').click();
    await page.getByTestId('admin-object').waitFor();
    await expect(page.locator('html')).toHaveAttribute('data-theme', vp.scheme);
    await expect(page.locator('[data-section="money"]')).toContainText('BEAM');

    for (const tab of TABS) {
      await page.locator(`[data-tab="${tab}"]`).click();
      await expect(page.locator('[data-testid="admin-object"], .ops-table, .ops-timeline').first()).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `${OUT}/admin-${tab}-${vp.name}.png`, fullPage: !vp.isMobile });
    }

    // the drawer, open, on the deposits tab
    await page.locator('[data-tab="deposits"]').click();
    await expect(page.getByTestId('admin-row')).toHaveCount(5);
    await page.getByTestId('admin-row').first().click();
    await page.getByTestId('admin-drawer').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/admin-drawer-${vp.name}.png` });

    expect(shotErrors).toEqual([]);
    await ctx.close();
  });
}
