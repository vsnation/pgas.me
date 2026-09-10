// T44 — the seven step pictures the /how-it-works page and the public README are built from.
//
// They are SCREENSHOTS OF THE REAL APP, driven end to end against the mocks: the same bundle the
// site serves, the same components, the same copy. Nothing here is drawn or mocked up. The numbers
// and addresses in them are the suite's example fixtures (a wallet with holdings on five chains,
// a balance of 0.5 ETH, two payout orders) — never a real account.
//
// Geometry. The viewport is 560 CSS px wide at deviceScaleFactor 2, so a card measures 520 CSS px
// and the file is 1040 device px: the page shows it at ≈ 520 px, which is 2× and stays sharp on a
// retina screen. 560 is also the app's own narrow breakpoint, so a card is one column — a picture
// 520 px wide of a two-column desktop card is a picture of nothing.
//
// Both themes: the page swaps `-dark.png` in under `[data-theme='dark']`, because a light
// screenshot on a dark page is the one thing a reader notices before the content.
//
// ⛔ THESE FILES ARE NOT THE ONES THE SITE SERVES. This spec writes evidence into e2e/screenshots/;
// the served copies live in web/public/how/ and the README's in ../docs/, both optimised to
// ≤ 120 KB. Regenerate and re-place them together:
//
//   npx playwright test e2e/how-steps.spec.ts
//   node e2e/how-steps-place.mjs          # optimise → web/public/how/ and ../docs/
//
// Splitting it that way is deliberate: `npx playwright test` builds the bundle BEFORE it runs, so a
// spec that wrote straight into public/ would be writing build inputs that the running build
// already left behind — the gate would pass on files nobody served.
import { expect, test, type Locator, type Page } from '@playwright/test';
import { getAddress } from 'ethers';
import {
  DEMO_HOLDINGS,
  MockApi,
  MockRpc,
  blockExternal,
  connectAndSignIn,
  goTab,
  installMockPrices,
  installMockWallet,
  payWith,
  walletB,
} from './mocks';
import { installPayoutStatuses } from './payout-fixtures';

const OUT = 'e2e/screenshots';
/**
 * 560 CSS px at 2× → 1040 device px wide. The height is absurd on purpose: at 560 px the app draws
 * a FIXED tab bar at the bottom of the viewport, and a clip that reaches past the fold gets that
 * bar painted straight through the middle of the card (which is what the first run produced). A
 * viewport taller than the page puts every card inside it at scroll 0, and the bar below them all.
 */
const VIEWPORT = { width: 560, height: 3200 };

/**
 * One picture from one or more live elements: the union of their boxes, clipped to the viewport.
 *
 * A clip rather than `locator.screenshot()` because two of the steps are a card plus something
 * drawn OUTSIDE it — the "Pay with" popover is absolutely positioned, so an element shot of the
 * card would cut the list off exactly where it starts being worth looking at.
 */
async function box(target: Locator, viewport: { width: number; height: number }, pad: number) {
  const b = await target.boundingBox();
  if (!b) throw new Error('the element has no box — is it on screen?');
  const x = Math.max(0, b.x - pad);
  const y = Math.max(0, b.y - pad);
  return {
    x,
    y,
    width: Math.min(viewport.width, b.x + b.width + pad) - x,
    height: Math.min(viewport.height, b.y + b.height + pad) - y,
  };
}

async function shot(page: Page, name: string, targets: Locator[], pad = 8): Promise<void> {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(() => document.fonts.ready);
  const boxes = [];
  for (const t of targets) {
    const b = await t.boundingBox();
    if (!b) throw new Error(`${name}: an element has no box — is it on screen?`);
    boxes.push(b);
  }
  const x = Math.max(0, Math.min(...boxes.map((b) => b.x)) - pad);
  const y = Math.max(0, Math.min(...boxes.map((b) => b.y)) - pad);
  const right = Math.min(VIEWPORT.width, Math.max(...boxes.map((b) => b.x + b.width)) + pad);
  const bottom = Math.min(VIEWPORT.height, Math.max(...boxes.map((b) => b.y + b.height)) + pad);
  await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x, y, width: right - x, height: bottom - y } });
}

for (const theme of ['light', 'dark'] as const) {
  test(`how-it-works step shots (${theme})`, async ({ browser }) => {
    const suffix = theme === 'dark' ? '-dark' : '';
    const ctx = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2, colorScheme: theme });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const api = new MockApi();
    api.armed = true;
    await blockExternal(page, true);
    await api.install(page);
    await new MockRpc(DEMO_HOLDINGS).install(page);
    await installMockPrices(page);
    await installMockWallet(page);
    await page.goto('/');
    await page.getByTestId('deposit-form').waitFor();
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);

    // ── 1. Connect a wallet ────────────────────────────────────────────────────────────────────
    //
    // What a first visit actually shows, before anything is connected: the panel that says what
    // this is, the three tabs as three steps, the way in, and the line that the signature costs
    // nothing. NOT the wallet picker, deliberately — the picker lists whatever the BROWSER
    // announces, and in the suite that is "Mock Wallet", "Mock Wallet 2", "Coin98". A public page
    // showing "Mock Wallet" reads as a defect, and swapping in a fixture that says "MetaMask"
    // would be a picture of a wallet nobody has checked on a real device (components/HowItWorks:
    // real-device checks, none yet). The picker is described in the words instead.
    await page.getByTestId('explainer').waitFor();
    await page.getByTestId('explainer-connect').waitFor();
    await shot(page, `how-step-1${suffix}`, [page.getByTestId('explainer')]);
    await connectAndSignIn(page);

    // ── 2. Pick what you pay with ──────────────────────────────────────────────────────────────
    // the holdings picker open over the card: the wallet's own balances, grouped by chain
    await expect(page.getByTestId('portfolio-chips').locator('.portfolio-chip')).toHaveCount(14, { timeout: 30_000 });
    await expect(page.getByTestId('pay-with')).toHaveAttribute('data-source', 'holdings');
    await page.getByTestId('pay-with').click();
    await page.getByTestId('pay-with-list').waitFor();
    await shot(page, `how-step-2${suffix}`, [page.getByTestId('deposit-form'), page.getByTestId('pay-with-list')]);
    await page.keyboard.press('Escape');

    // ── 3. Deposit — one transaction ───────────────────────────────────────────────────────────
    // paying from Arbitrum makes it the cross-chain route, which is the default and the one a
    // depositor most often takes; the quote card carries what lands, the fee and the button
    await payWith(page, { chainId: 42161, token: 'ETH' });
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await page.getByTestId('arrives-as').waitFor();
    await page.getByTestId('deposit-btn').waitFor();
    await shot(page, `how-step-3${suffix}`, [page.getByTestId('quote-card')]);

    // ── 4. Watch it cross ──────────────────────────────────────────────────────────────────────
    await page.getByTestId('deposit-btn').click();
    await page.getByTestId('deposit-timeline').waitFor();
    // mid-flight, which is the state worth a picture: the mock RPC's head is block 16, so a lock
    // at block 10 is 7 of the 12 confirmations — the counter the card reads off the chain itself
    api.deposits[0].status = 'confirming';
    api.deposits[0].eth = { tx: '0x' + '3c'.repeat(32), block: 10 };
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect(page.getByTestId('deposit-timeline')).toContainText('Confirming');
    await shot(page, `how-step-4${suffix}`, [page.getByTestId('deposit-timeline')]);

    // ── 5. Your balance on Beam ────────────────────────────────────────────────────────────────
    await goTab(page, 'balance');
    await page.getByTestId('balance-ETH').waitFor();
    await expect(page.getByTestId('balance-ETH').locator('.tile-usd').first()).toBeVisible({ timeout: 15_000 });
    await shot(page, `how-step-5${suffix}`, [page.getByTestId('balance-ETH')]);

    // ── 6. Schedule payouts ────────────────────────────────────────────────────────────────────
    // both addresses checksummed: an un-checksummed one is CORRECT to paste and the form says so
    // underneath it, but a picture of the happy path should not carry a line that reads as a
    // correction — that lesson belongs in the FAQ, not in step six
    await goTab(page, 'schedule');
    await page.getByTestId('schedule-form').waitFor();
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.05');
    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(getAddress('0x' + '3a'.repeat(20)));
    await page.getByLabel('Amount 2').fill('0.02');
    await page.getByLabel('Deliver 2').selectOption('tonight');
    // every number on the card is the server's own preview; a shot taken before it lands is 0.00
    await page.getByTestId('row-total-1').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
    await expect(page.getByTestId('total-debited')).not.toHaveText('0.00 ETH');
    await shot(page, `how-step-6${suffix}`, [page.getByTestId('schedule-form')]);

    expect(errors).toEqual([]);
    await ctx.close();
  });
}

/**
 * ── 7. Track delivery ────────────────────────────────────────────────────────────────────────
 *
 * The one picture that cannot be 520 px wide, and the measurement says so: a table of six or seven
 * columns needs about 1,036 px of content, so at 560 px the card shows three of them and scrolls
 * the rest — a screenshot of that is a table cut through the middle of a number. Shrinking it to
 * 520 px instead makes 13 px type into 6 px, which is not a picture of anything either. So this
 * one is captured at the app's own desktop width and the page and the README give it the full
 * column.
 *
 * ⛔ IT IS THE TIMELINE, NOT THE PAYOUTS TABLE, AND THAT IS A MEASUREMENT TOO. `.container` caps at
 * 1120 px, so the card is 1080 and the scroll box inside it 1036 — at EVERY desktop size. The
 * Payouts table was 1013 px wide and fitted; T48's rename of "Refunded" to "Returned to balance"
 * (2026-09-10) widened the status column and it is 1110 now, so its last column — the arrival
 * estimate and the bridge-explorer link, the two things this step is about — is behind a
 * horizontal scroll on every screen there is. Reported rather than papered over; a picture of a
 * table clipped mid-column would have shipped that defect as if it were the design.
 *
 * The timeline carries the same story and measures exactly 1036: every deposit and payout newest
 * first, the SAME status cell (so a delayed order shows its reason here too), and — with a row
 * opened — the wallet, the delivery time, the release time, the fee, the delivery transaction and
 * the bridge explorer. Four payouts are staged: one still scheduled, one DELAYED with its reason
 * and next attempt (the status that exists so "failed" never has to), one crossing the bridge, and
 * one delivered.
 */
for (const theme of ['light', 'dark'] as const) {
  test(`how-it-works timeline shot (${theme})`, async ({ browser }) => {
    const suffix = theme === 'dark' ? '-dark' : '';
    // ≥ 1160 gives the container its full 1120 px, which is the widest this app ever lays out
    const viewport = { width: 1180, height: 2000 };
    const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2, colorScheme: theme });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const api = new MockApi();
    api.armed = true;
    installPayoutStatuses(api);
    const keep = ['p-from-amount', 'p-delayed', 'p-bridging', 'p-sent'];
    api.requests = keep.map((id) => api.requests.find((r) => r._id === id)!);
    // the Beam block the crossing's kernel landed in — what `BridgeLink` keys the bridge explorer
    // on. The shared fixture leaves it out (a row can be bridging before we know it); this picture
    // is the one where the link is the point.
    api.requests[2].beam_height = 4030417;
    // the shared deposit fixtures are pinned to fixed 2025 instants, which put "09 Sept 2025" above
    // today's payouts in one picture. They are examples either way; an example with a plausible
    // clock is the one that does not make a reader stop and wonder what they are looking at.
    const ago = (s: number) => Math.floor(Date.now() / 1000) - s;
    api.deposits[0].created_at = ago(3 * 3600);
    api.deposits[0].updated_at = ago(3 * 3600 - 900);
    api.deposits[1].created_at = ago(2 * 3600);
    api.deposits[1].updated_at = ago(2 * 3600 - 300);
    api.deposits[2].created_at = ago(600);
    api.deposits[2].updated_at = ago(600);
    await blockExternal(page, true);
    await api.install(page);
    await new MockRpc(DEMO_HOLDINGS).install(page);
    await installMockPrices(page);
    await installMockWallet(page);
    await page.goto('/balance');
    await connectAndSignIn(page);
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
    await page.getByTestId('timeline').waitFor();
    await expect(page.locator('[data-testid="payout-row"][data-shown="delayed"]')).toHaveCount(1);
    // open the crossing that is on the bridge right now — its detail is where the explorer link is
    const bridging = page.locator('[data-testid="timeline-row"]', { hasText: 'Bridging' }).first();
    await bridging.click();
    await page.getByTestId('timeline-detail').waitFor();
    await expect(page.getByTestId('timeline-detail')).toContainText('bridge explorer');
    // the table fits: nothing in this picture is hidden behind a horizontal scroll
    const overflow = await page.evaluate(() => {
      const t = document.querySelector('table[data-testid="timeline"]') as HTMLElement;
      return t.scrollWidth - (t.parentElement as HTMLElement).clientWidth;
    });
    expect(overflow).toBeLessThanOrEqual(0);
    await page.getByTestId('timeline-card').scrollIntoViewIfNeeded();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({
      path: `${OUT}/how-step-7${suffix}.png`,
      clip: await box(page.getByTestId('timeline-card'), viewport, 8),
    });
    expect(errors).toEqual([]);
    await ctx.close();
  });
}

/**
 * ── The page itself ──────────────────────────────────────────────────────────────────────────
 *
 * Evidence, not an ingredient: what /how-it-works looks like at desktop, in the dark theme, and on
 * a phone. Written to e2e/screenshots/how-page-*.png — `how-it-works-*.png` beside them is the
 * six-line MODAL, which is a different thing.
 *
 * ⛔ NOT `fullPage`. Every figure on this page is `loading="lazy"`, and a full-page capture of a
 * 6,000 px page paints the lazy ones as EMPTY BOXES even after they have loaded and decoded —
 * `naturalWidth` says 1072 and the picture is white, which is exactly the shape of defect a
 * screenshot is supposed to catch rather than produce. So the page is measured, the viewport is
 * grown past the bottom of it, and the whole thing is captured as one ordinary viewport.
 */
async function everyImageDecoded(page: Page) {
  await page.evaluate(async () => {
    const imgs = [...document.querySelectorAll('img')];
    for (const img of imgs) img.removeAttribute('loading');
    await Promise.all(imgs.map((i) => i.decode().catch(() => null)));
  });
  await expect(async () => {
    const blank = await page.evaluate(() => [...document.querySelectorAll('img')].filter((i) => !i.naturalWidth).length);
    expect(blank).toBe(0);
  }).toPass({ timeout: 20_000 });
  await page.evaluate(() => document.fonts.ready);
}

for (const vp of [
  { name: 'desktop', width: 1280, colorScheme: 'light' as const, isMobile: false },
  { name: 'desktop-dark', width: 1280, colorScheme: 'dark' as const, isMobile: false },
]) {
  test(`how-it-works page shot (${vp.name})`, async ({ browser }) => {
    const ctx = await browser.newContext({
      viewport: { width: vp.width, height: 900 },
      deviceScaleFactor: 1,
      colorScheme: vp.colorScheme,
    });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await blockExternal(page, true);
    await new MockApi().install(page);
    await installMockWallet(page);
    await page.goto('/how-it-works');
    await page.getByTestId('how-page').waitFor();
    await expect(page.getByTestId('how-step')).toHaveCount(7);
    await everyImageDecoded(page);
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    await page.setViewportSize({ width: vp.width, height: Math.min(height, 9000) });
    await everyImageDecoded(page);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(250);
    await page.screenshot({ path: `${OUT}/how-page-${vp.name}.png` });
    expect(errors).toEqual([]);
    await ctx.close();
  });
}

test('how-it-works page shot (mobile)', async ({ browser }) => {
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
    colorScheme: 'light',
  });
  const page = await ctx.newPage();
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await blockExternal(page, true);
  await new MockApi().install(page);
  await installMockWallet(page);
  await page.goto('/how-it-works');
  await page.getByTestId('how-page').waitFor();
  await everyImageDecoded(page);
  // what a phone shows on arrival: the title, the lead, the way in, and the bottom tab bar
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${OUT}/how-page-mobile.png` });
  // and one step in full, to prove the text-then-picture stack a phone actually gets
  await page.getByTestId('how-shot-2').scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${OUT}/how-page-mobile-step.png` });
  expect(errors).toEqual([]);
  await ctx.close();
});
