// T31b item 8 / T31 item I — the bridge's own explorer on the Balance page.
//
// Admin, 2026-09-10 12:5xZ: "Here is the link to block explorer of the bridge
// https://beamterminal.0xmx.net/#/explorer/bridge?tx={block_height}, so user can track his
// withdrawals and funding his wallets."
//
// ⛔ IT KEYS ON THE BEAM BLOCK HEIGHT. Not a txid, not a kernel id — so the client draws the link
// from ONE field the API publishes (`beam_height` on the public deposit and payout rows) and never
// from anything it derives. A row whose crossing has no recorded block gets NO link: a link built
// on a guessed height points at somebody else's bridge traffic and reads as evidence about this
// user's money.
//
// The Ethereum-side links are untouched: etherscan for what happened on Ethereum, the bridge
// explorer for what happened on Beam.
import { expect, test, type Page } from '@playwright/test';
import { bridgeExplorerUrl } from '../src/components/BridgeLink';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet } from './mocks';

const OUT = 'e2e/screenshots';
const CLAIM_BLOCK = 4030012; // the mock's credited deposit
const CROSSING_BLOCK = 4031777; // the mock's delivered payout

let api: MockApi;
let pageErrors: string[];

async function boot(page: Page, mock?: (a: MockApi) => void) {
  api = new MockApi();
  api.armed = true;
  mock?.(api);
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page);
  await api.install(page);
  await installMockWallet(page);
  await page.goto('/balance');
  await connectAndSignIn(page);
  await page.getByTestId('timeline').waitFor();
}

/**
 * Open the timeline row whose text contains `text`, and return its detail block.
 *
 * ⚠️ Pick the row by its STATUS, not by the word "Deposit": the mock's newest deposit is one that
 * is still confirming — no crossing, no block, and correctly no link — so a `.first()` on "Deposit"
 * opens the row that proves nothing.
 */
async function openRow(page: Page, text: string) {
  const row = page.getByTestId('timeline-row').filter({ hasText: text }).first();
  await row.scrollIntoViewIfNeeded();
  await row.click();
  return page.getByTestId('timeline-detail').first();
}

test.describe('the URL itself', () => {
  test('one writer, one shape, and nothing that is not a block ever becomes one', () => {
    expect(bridgeExplorerUrl(CLAIM_BLOCK)).toBe(`https://beamterminal.0xmx.net/#/explorer/bridge?tx=${CLAIM_BLOCK}`);
    // ⛔ every one of these is "we do not know", and none of them is block 0
    for (const bad of [null, undefined, 0, -1, 1.5, NaN, Infinity]) {
      expect(bridgeExplorerUrl(bad as number)).toBeNull();
    }
  });
});

test.describe('balance: tracking the crossing on the bridge', () => {
  test('a credited deposit links its claim, and one still confirming links nothing', async ({ page }) => {
    await boot(page);
    const detail = await openRow(page, 'Credited');
    const link = detail.getByTestId('bridge-link');
    await expect(link).toHaveText('track on the bridge explorer ↗');
    await expect(link).toHaveAttribute('href', `https://beamterminal.0xmx.net/#/explorer/bridge?tx=${CLAIM_BLOCK}`);
    await expect(link).toHaveAttribute('target', '_blank');
    // the Ethereum side keeps its own explorer
    await expect(detail).toContainText('Into the bridge');
    await expect(detail.locator('a[href^="https://etherscan.io/tx/"]')).not.toHaveCount(0);

    // …and the deposit that is still confirming has no crossing yet, so it has no link
    const pending = await openRow(page, 'Confirming');
    await expect(pending.getByTestId('bridge-link')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test('a delivered payout links its crossing, on the row the user watches', async ({ page }) => {
    await boot(page);
    const link = page.getByTestId('payouts').getByTestId('bridge-link');
    await expect(link).toHaveCount(1); // only the row that has a block
    await expect(link).toHaveAttribute('href', `https://beamterminal.0xmx.net/#/explorer/bridge?tx=${CROSSING_BLOCK}`);
    expect(pageErrors).toEqual([]);
  });

  test('⛔ a row the API has no block for is not linked at all', async ({ page }) => {
    await boot(page, (a) => {
      a.deposits = a.deposits.map((d) => ({ ...d, beam_height: null }));
      a.requests = a.requests.map((r) => ({ ...r, beam_height: undefined }));
    });
    await expect(page.getByTestId('bridge-link')).toHaveCount(0);
    // …and not because the page failed to render: the rows are all still there
    await expect(page.getByTestId('timeline-row')).not.toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test('a height that is not a block is not rendered as one', async ({ page }) => {
    // rows written before the guards, and a row whose height came back as text
    await boot(page, (a) => {
      a.deposits = a.deposits.map((d) => ({ ...d, beam_height: 0 }));
      a.requests = a.requests.map((r) => ({ ...r, beam_height: '4031777' }));
    });
    await expect(page.getByTestId('bridge-link')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });
});

// ---------------------------------------------------------------- the evidence screenshots

for (const vp of [
  { name: 'desktop', viewport: { width: 1280, height: 900 }, isMobile: false, fullPage: true },
  { name: 'mobile', viewport: { width: 390, height: 844 }, isMobile: true, fullPage: false },
]) {
  test(`balance-explorer-links-${vp.name}`, async ({ browser }) => {
    const ctx = await browser.newContext({
      viewport: vp.viewport,
      isMobile: vp.isMobile,
      hasTouch: vp.isMobile,
      deviceScaleFactor: vp.isMobile ? 2 : 1,
      colorScheme: 'light',
    });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const mock = new MockApi();
    mock.armed = true;
    await blockExternal(page, true);
    await mock.install(page);
    await installMockWallet(page);
    await page.goto('/balance');
    await connectAndSignIn(page);
    await page.getByTestId('timeline').waitFor();
    // open the deposit's detail so both links are in one frame: the payout's on its own row above,
    // the deposit's inside the story
    const row = page.getByTestId('timeline-row').filter({ hasText: 'Credited' }).first();
    await row.scrollIntoViewIfNeeded();
    await row.click();
    await expect(page.getByTestId('timeline-detail').first().getByTestId('bridge-link')).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    if (vp.fullPage) await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(150);
    await page.screenshot({ path: `${OUT}/balance-explorer-links-${vp.name}.png`, fullPage: vp.fullPage });
    expect(errors).toEqual([]);
    await ctx.close();
  });
}
