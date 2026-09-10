// T53 — the money table fits the card it is in.
//
// T44 measured the defect: 1110 px of Payouts table inside a 1036 px scroll box at 1280×900, so
// the "Arrives" column — the arrival estimate AND the bridge-explorer link — was permanently
// behind a horizontal scroll. The columns had no widths of their own, so the widest sentence in
// each cell set them, and the sentences keep growing: "Returned to balance", a hold reason, an
// ETA note, "bridge fee: 0.00001955 ETH back in Available".
//
// So this file asserts the property rather than a pixel: the table never scrolls sideways inside
// its own box, on either page, with every status on screen at once — and on a phone it is not a
// table at all but one card per order, so there is nothing to scroll.
//
// ⛔ It measures the WIDEST case on purpose (`installPayoutStatuses` puts every status, every
// hold reason and every note on the page at the same time). A table that fits the happy path and
// not the delayed one is a table that hides the row a person came to look at.
import { expect, test, type Page } from '@playwright/test';
import { getAddress } from 'ethers';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet } from './mocks';
import { installPayoutStatuses } from './payout-fixtures';

const OUT = 'e2e/screenshots';

/** How many pixels of the table are outside the box that holds it. ≤ 0 is "it fits". */
async function overflowOf(page: Page, testId: string): Promise<number> {
  return await page.evaluate((id) => {
    const t = document.querySelector(`table[data-testid="${id}"]`) as HTMLElement | null;
    if (!t) return Number.NaN; // a selector that matches nothing must never read as "it fits"
    const box = t.parentElement as HTMLElement;
    return t.scrollWidth - box.clientWidth;
  }, testId);
}

/** And the page itself: a card that overflows its container moves the whole document sideways. */
const pageOverflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

async function boot(page: Page, path = '/balance') {
  const api = new MockApi();
  installPayoutStatuses(api);
  await blockExternal(page);
  await api.install(page);
  await installMockWallet(page);
  await page.goto(path);
  await connectAndSignIn(page);
  return api;
}

test.describe('the money table fits the card it is in', () => {
  test('Payouts at 1280: no sideways scroll, and the Arrives column is on screen', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.setViewportSize({ width: 1280, height: 900 });
    await boot(page);
    const table = page.getByTestId('payouts');
    await table.waitFor();
    await expect(table.locator('[data-testid="payout-row"]')).toHaveCount(9);

    expect(await overflowOf(page, 'payouts')).toBeLessThanOrEqual(0);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);

    // the column that was behind the scroll: its content is inside the table's own box
    const arrives = page.locator('[data-testid^="payout-eta-"]').first();
    const inside = await arrives.evaluate((el) => {
      const box = (el.closest('table') as HTMLElement).parentElement as HTMLElement;
      return box.getBoundingClientRect().right - el.getBoundingClientRect().right;
    });
    expect(inside).toBeGreaterThanOrEqual(0);

    await page.evaluate(() => document.fonts.ready);
    await page.getByTestId('payouts-card').scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${OUT}/payouts-table-desktop.png`, fullPage: false });
    expect(errors).toEqual([]);
  });

  test('Scheduled orders at 1280: the same table, the same fit', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.setViewportSize({ width: 1280, height: 900 });
    await boot(page, '/schedule');
    await page.getByTestId('schedule-orders').waitFor();
    expect(await overflowOf(page, 'schedule-orders')).toBeLessThanOrEqual(0);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    expect(errors).toEqual([]);
  });

  test('on a phone it is one card per order — no header row, nothing to scroll', async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await boot(page);
    await page.getByTestId('payouts').waitFor();

    // the header row is what a table is; at 390 px there is none, and every value is labelled
    await expect(page.getByTestId('payouts').locator('thead')).toBeHidden();
    const first = page.locator('[data-testid="payout-row"]').first();
    await expect(first.locator('[data-label="Arrives"]')).toBeVisible();
    await expect(first.locator('[data-label="Wallet receives"]')).toBeVisible();
    expect(await overflowOf(page, 'payouts')).toBeLessThanOrEqual(0);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);

    await page.evaluate(() => document.fonts.ready);
    await page.getByTestId('payouts-card').scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${OUT}/payouts-table-mobile.png`, fullPage: false });
    expect(errors).toEqual([]);
    await ctx.close();
  });

  test('a scheduled row the user typed lands in the same table, still inside the card', async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.setViewportSize({ width: 1280, height: 900 });
    await boot(page, '/schedule');
    await page.getByLabel('Address 1').fill(getAddress('0x' + '77'.repeat(20)));
    await page.getByLabel('Amount 1').fill('0.05');
    await page.getByTestId('row-total-0').waitFor();
    expect(await overflowOf(page, 'schedule-orders')).toBeLessThanOrEqual(0);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    expect(errors).toEqual([]);
  });
});
