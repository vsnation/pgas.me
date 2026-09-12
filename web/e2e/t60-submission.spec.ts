// ETHGlobal submission screenshots: the whole flow with prefilled data, driven through the mock.
import { test } from '@playwright/test';
import { DEMO_HOLDINGS, MockApi, MockRpc, blockExternal, connectAndSignIn, installMockPrices, installMockWallet } from './mocks';
import { installPayoutStatuses, W1, W2 } from './payout-fixtures';

const OUT = '../docs/ethglobal';

async function open(browser: any, path: string, opts: { mobile?: boolean; statuses?: boolean } = {}) {
  const ctx = await browser.newContext({
    viewport: opts.mobile ? { width: 390, height: 844 } : { width: 1600, height: 900 },
    isMobile: !!opts.mobile, hasTouch: !!opts.mobile, deviceScaleFactor: opts.mobile ? 2 : 1,
  });
  const page = await ctx.newPage();
  const api = new MockApi();
  api.armed = true; // the submission shots show the live call to action, not the paused preview
  if (opts.statuses) installPayoutStatuses(api);
  await blockExternal(page, true);
  await api.install(page);
  await new MockRpc(DEMO_HOLDINGS).install(page);
  await installMockPrices(page);
  await installMockWallet(page);
  await page.goto(path);
  await connectAndSignIn(page);
  await page.waitForTimeout(1500);
  return { ctx, page };
}

test('flow 1 — deposit, filled', async ({ browser }) => {
  const { ctx, page } = await open(browser, '/deposit');
  await page.getByPlaceholder('0.0').first().fill('0.1');
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${OUT}/flow-1-deposit.png`, fullPage: true });
  await ctx.close();
});

test('flow 2 — withdraw, two wallets', async ({ browser }) => {
  const { ctx, page } = await open(browser, '/withdraw');
  await page.getByPlaceholder('0x… wallet to fund').first().fill(W1);
  await page.getByPlaceholder('0.00').first().fill('0.05');
  await page.getByText('+ Add another address').click().catch(() => {});
  await page.waitForTimeout(600);
  const addr = page.getByPlaceholder('0x… wallet to fund');
  if (await addr.count() > 1) { await addr.nth(1).fill(W2); await page.getByPlaceholder('0.00').nth(1).fill('0.02'); }
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${OUT}/flow-2-withdraw.png`, fullPage: true });
  await ctx.close();
});

test('flow 3 — balance and statuses', async ({ browser }) => {
  const { ctx, page } = await open(browser, '/balance', { statuses: true });
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${OUT}/flow-3-balance.png`, fullPage: true });
  await ctx.close();
});

test('flow 4 — how it works', async ({ browser }) => {
  const { ctx, page } = await open(browser, '/how-it-works');
  await page.waitForTimeout(2000);
  await page.screenshot({ path: `${OUT}/flow-4-how.png` });
  await ctx.close();
});

test('flow 5 — on a phone', async ({ browser }) => {
  const { ctx, page } = await open(browser, '/deposit', { mobile: true });
  await page.getByPlaceholder('0.0').first().fill('0.1');
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${OUT}/flow-5-mobile.png` });
  await ctx.close();
});
