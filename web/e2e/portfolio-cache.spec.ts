// T31 F + G (admin, 2026-09-10):
//   "You need to cache balances result to avoid update during same or next update session, if user
//    can't see some balances, he can just click refresh btn."
//   "Let's do pay with from portfolio as we know all balances of the user, he doesn't need to
//    select tokens he doesn't have in a balance in Pay With."
//
// So: the scan is remembered per wallet and rendered whatever its age, the Refresh button is the
// only thing that starts a new one, and the "Pay with" picker offers the holdings that scan found.
import { expect, test, type Page } from '@playwright/test';
import {
  DEMO_HOLDINGS,
  MockApi,
  MockRpc,
  blockExternal,
  connectAndSignIn,
  installMockPrices,
  installMockWallet,
  payWith,
  signedIn,
  walletA,
  walletB,
} from './mocks';

let api: MockApi;
let pageErrors: string[];
/** Every POST that leaves 127.0.0.1 is an RPC call: the price double and the token files are GETs. */
let rpcCalls: string[];

async function boot(page: Page, opts: { rpc?: boolean; mock?: (a: MockApi) => void } = {}) {
  api = new MockApi();
  opts.mock?.(api);
  pageErrors = [];
  rpcCalls = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  page.on('request', (r) => {
    if (r.method() === 'POST' && !r.url().includes('127.0.0.1')) rpcCalls.push(r.url());
  });
  await blockExternal(page);
  await api.install(page);
  if (opts.rpc !== false) {
    await new MockRpc(DEMO_HOLDINGS).install(page);
    await installMockPrices(page);
  }
  await installMockWallet(page);
  await page.goto('/');
}

const chips = (page: Page) => page.getByTestId('portfolio-chips').locator('.portfolio-chip');

test.describe('the portfolio is cached, and the picker is the portfolio', () => {
  test('the second load renders from the cache with no RPC call at all; Refresh is the only scan', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(chips(page)).toHaveCount(14, { timeout: 30_000 });
    // the chips appear chain by chain; the "as of" stamp is the scan having LANDED, and only then
    // is the counter below measuring the second load rather than the tail of the first
    await expect(page.getByTestId('portfolio-as-of')).toBeVisible({ timeout: 30_000 });
    expect(rpcCalls.length).toBeGreaterThan(0); // the first visit for an address scans once

    // second load: the chips are there, and nothing was read to put them there
    rpcCalls = [];
    await page.goto('/');
    await expect(chips(page)).toHaveCount(14, { timeout: 15_000 });
    await expect(page.getByTestId('portfolio-as-of')).toBeVisible();
    await page.waitForTimeout(1500); // long enough for any automatic scan to have started
    expect(rpcCalls).toEqual([]);

    // a tab switch and a return is not a reason to re-read thirty endpoints either
    await page.locator('[data-tab="balance"]:visible').first().click();
    await page.locator('[data-tab="deposit"]:visible').first().click();
    await expect(chips(page)).toHaveCount(14);
    expect(rpcCalls).toEqual([]);

    // …and the button that says it will is the one that does
    await page.getByTestId('portfolio').getByRole('button', { name: 'Refresh' }).click();
    await expect(chips(page)).toHaveCount(14, { timeout: 30_000 });
    expect(rpcCalls.length).toBeGreaterThan(0);
    expect(pageErrors).toEqual([]);
  });

  test('a cache from days ago still renders, and says how old it is', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(chips(page)).toHaveCount(14, { timeout: 30_000 });
    // the chips appear chain by chain, mid-scan; the "as of" stamp is the scan having LANDED
    await expect(page.getByTestId('portfolio-as-of')).toBeVisible({ timeout: 30_000 });

    // age the stored scan by three days — an old number needs a date on it, not a silent re-read
    await page.evaluate(() => {
      const key = Object.keys(localStorage).find((k) => k.startsWith('pgas.portfolio.v1.'))!;
      const p = JSON.parse(localStorage.getItem(key)!);
      p.at = Date.now() - 3 * 24 * 60 * 60 * 1000;
      localStorage.setItem(key, JSON.stringify(p));
    });
    rpcCalls = [];
    await page.goto('/');
    await expect(chips(page)).toHaveCount(14, { timeout: 15_000 });
    await expect(page.getByTestId('portfolio-as-of')).toContainText(/d ago|ago/);
    await page.waitForTimeout(1500);
    expect(rpcCalls).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test('two wallets keep two caches, and going back to the first one reads nothing', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(chips(page)).toHaveCount(14, { timeout: 30_000 });
    await expect(page.getByTestId('portfolio-as-of')).toBeVisible({ timeout: 30_000 });
    expect(await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('pgas.portfolio.v1.')))).toHaveLength(1);

    // the wallet switches account: a different address is a different portfolio, and the first
    // one's cache is not thrown away
    await page.evaluate((addr) => (window as never as Record<string, any>).__wallets.mock.setAccounts([addr]), walletB.address);
    await expect(signedIn(page)).toBeVisible({ timeout: 20_000 });
    await expect(chips(page)).toHaveCount(14, { timeout: 30_000 });
    await expect
      .poll(() => page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('pgas.portfolio.v1.')).length))
      .toBe(2);

    // back to the first wallet: its scan is still there, and nothing is read to show it
    rpcCalls = [];
    await page.evaluate((addr) => (window as never as Record<string, any>).__wallets.mock.setAccounts([addr]), walletA.address);
    await expect(signedIn(page)).toBeVisible({ timeout: 20_000 });
    await expect(chips(page)).toHaveCount(14, { timeout: 15_000 });
    expect(rpcCalls).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test('the picker lists the holdings, grouped by chain — and never a token the wallet does not hold', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(chips(page)).toHaveCount(14, { timeout: 30_000 });

    const button = page.getByTestId('pay-with');
    await expect(button).toHaveAttribute('data-source', 'holdings');
    await button.click();
    const rows = page.getByTestId('pay-with-list').getByRole('option');
    await expect(rows).toHaveCount(14); // exactly the chips — one data source, not two
    // there is no chain row any more: a row IS a chain and a token
    await expect(page.getByTestId('pay-with-chains')).toHaveCount(0);

    // Base holds ETH, USDC and DAI in the double — and WBTC is offered nowhere but Ethereum
    const base = page.locator('[data-chain-group="8453"]');
    await expect(base.getByRole('option')).toHaveCount(3);
    await expect(base).not.toContainText('WBTC');
    await expect(base.locator('.pw-group-head')).toHaveText('Base'); // the chain names its group

    // every row carries what it is worth, and picking one sets the chain AND the token
    const usdcOnBase = base.getByRole('option', { name: /USDC/i }).first();
    await expect(usdcOnBase).toContainText('$96');
    await usdcOnBase.click();
    await expect(button).toContainText('USDC');
    await expect(button).toContainText('on Base');
    await expect(button).toHaveAttribute('data-chain-id', '8453');

    // search filters the holdings, and only those
    await button.click();
    await page.getByLabel('Search your balances').fill('wbtc');
    await expect(page.getByTestId('pay-with-list').getByRole('option')).toHaveCount(1);
    await page.getByLabel('Search your balances').fill('zzz-not-a-token');
    await expect(page.getByTestId('pay-with-list')).toContainText('No holding matches');
    expect(pageErrors).toEqual([]);
  });

  test('a holding quotes without waiting for any token catalogue', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(chips(page)).toHaveCount(14, { timeout: 30_000 });

    // Reload with every token list unreachable: the hosted file 404s and the API route never
    // answers. The picker still works, because the scan already knows what this wallet holds.
    api.staticTokens = false;
    await page.route('**/api/v1/dex/tokens*', () => {
      /* never fulfilled: a list that hangs must not hold up a quote */
    });
    await page.goto('/');
    await expect(chips(page)).toHaveCount(14, { timeout: 15_000 });
    await payWith(page, { chainId: 42161, token: 'usdc' });
    await page.getByLabel('Amount (USDC)').fill('100');
    await expect
      .poll(() => api.calls.filter((c) => c.path === '/quote').pop()?.body)
      .toMatchObject({ src_chain_id: 42161, amount: '100000000' });
    await expect(page.getByTestId('quote-out')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('a wallet with nothing found says so — and still lets you choose, because a failed scan is not an empty wallet', async ({
    page,
  }) => {
    await boot(page, { rpc: false }); // every public RPC is blocked: nothing can be read
    await connectAndSignIn(page);
    await expect(page.getByTestId('portfolio-empty')).toBeVisible({ timeout: 30_000 });
    const button = page.getByTestId('pay-with');
    await expect(button).toHaveAttribute('data-source', 'catalogue');
    await button.click();
    await expect(page.getByTestId('pay-with-empty')).toContainText(
      'No balances found in this wallet — Refresh above, or fund the wallet first.',
    );
    await expect(page.getByTestId('pay-with-chains')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });
});
