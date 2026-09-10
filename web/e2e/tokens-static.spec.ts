// T31 C3 (admin, 2026-09-10): "Why getting quote takes so long?" — measured: the server answers a
// quote in 0.3–0.5 s cold, the client waited 1000 ms for the user to stop typing, and the token
// list for the chosen chain (Ethereum: 1.3 MB, 6,617 tokens, 2.5 s through the edge) had to arrive
// before `canQuote` was even true. "Why you didn't host token list on server? It saves us a lot of
// time. Download and use it."
//
// So the catalogue is our own file now (`/tokens/<chain_id>.json`, same origin, cacheable), the
// API route is the fallback for a box that has not built it, the parsed list is cached in
// localStorage, the debounce is 300 ms, and the first quote does not wait for any of it.
import { expect, test, type Page } from '@playwright/test';
import { DEMO_HOLDINGS, MockApi, MockRpc, blockExternal, connectAndSignIn, installMockPrices, installMockWallet } from './mocks';

let api: MockApi;
let pageErrors: string[];

async function boot(page: Page, opts: { rpc?: boolean; mock?: (a: MockApi) => void } = {}) {
  api = new MockApi();
  opts.mock?.(api);
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page);
  await api.install(page);
  if (opts.rpc) {
    await new MockRpc(DEMO_HOLDINGS).install(page);
    await installMockPrices(page);
  }
  await installMockWallet(page);
  await page.goto('/');
}

test.describe('the token catalogue is hosted, cached and never in the way', () => {
  test('the hosted file is what is read — the API route is not called at all', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(page.getByTestId('pay-with')).toContainText('ETH');
    await expect.poll(() => api.tokenFileReads).toContain(1);
    expect(api.calls.filter((c) => c.path.startsWith('/dex/tokens'))).toEqual([]);

    // and it is remembered, keyed by chain and the file's own updated_at
    const cached = await page.evaluate(() => localStorage.getItem('pgas.tokens.v2.1'));
    expect(cached).toBeTruthy();
    expect(JSON.parse(cached!).updated_at).toBe('2026-09-10T00:00:00Z');

    // a second load reads the cache, not the file
    api.tokenFileReads = [];
    await page.goto('/');
    await expect(page.getByTestId('pay-with')).toContainText('ETH');
    await page.waitForTimeout(800);
    expect(api.tokenFileReads).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test('a box that has not built the files falls back to the API, and nobody notices', async ({ page }) => {
    await boot(page, { mock: (a) => (a.staticTokens = false) });
    await connectAndSignIn(page);
    await page.getByTestId('pay-with').click();
    await expect(page.getByRole('option').first()).toBeVisible();
    // the fallback is the route the API has always had — asked for only after the file 404s
    await expect.poll(() => api.calls.filter((c) => c.path.startsWith('/dex/tokens')).length).toBeGreaterThan(0);
    await expect(page.getByRole('option', { name: /USDC/i }).first()).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('the quote goes out within 300 ms of the last keystroke, and does not wait for any list', async ({ page }) => {
    // every catalogue is unreachable: the hosted file 404s and the API route never answers. The
    // chain list already says what the native coin is, so the quote must not care.
    await boot(page, { mock: (a) => (a.staticTokens = false) });
    await page.route('**/api/v1/dex/tokens*', () => {
      /* never fulfilled */
    });
    await connectAndSignIn(page);
    await expect(page.getByTestId('pay-with')).toContainText('ETH');

    const before = api.calls.filter((c) => c.path === '/quote').length;
    const t0 = Date.now();
    await page.getByLabel('Amount (ETH)').fill('0.25');
    await expect.poll(() => api.calls.filter((c) => c.path === '/quote').length).toBeGreaterThan(before);
    const elapsed = Date.now() - t0;
    expect(elapsed).toBeLessThan(900); // the old 1000 ms debounce could not pass this
    await expect(page.getByTestId('quote-out')).toContainText('0.25');
    expect(pageErrors).toEqual([]);
  });

  test('the lists of the chains the wallet holds are prefetched, with no scan running', async ({ page }) => {
    await boot(page, { rpc: true });
    await connectAndSignIn(page);
    await expect(page.getByTestId('portfolio-chips').locator('.portfolio-chip')).toHaveCount(14, { timeout: 30_000 });

    // wipe the token caches and load again: the portfolio comes from ITS cache, so no scan runs —
    // the only thing that can ask for these files now is the prefetch
    await page.evaluate(() => {
      for (const k of Object.keys(localStorage)) if (k.startsWith('pgas.tokens.v2.')) localStorage.removeItem(k);
    });
    api.tokenFileReads = [];
    await page.goto('/');
    await expect(page.getByTestId('portfolio-chips').locator('.portfolio-chip')).toHaveCount(14, { timeout: 15_000 });
    for (const chainId of [1, 42161, 8453, 1514, 25]) {
      await expect.poll(() => api.tokenFileReads, { timeout: 15_000 }).toContain(chainId);
    }
    expect(pageErrors).toEqual([]);
  });
});
