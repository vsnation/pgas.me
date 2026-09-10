// T54 evidence screenshots: the RPC settings popup at desktop 1280×900 (light and dark) and on a
// phone at 390×844, where it is a full-screen sheet rather than a card in a puddle of backdrop.
//
// Viewport captures, not full-page: the dialog is an overlay, and a full-page shot paints fixed
// elements once at the top — which is exactly what neither a desktop nor a phone ever shows.
import { expect, test } from '@playwright/test';
import { DEMO_HOLDINGS, MockApi, MockRpc, blockExternal, connectAndSignIn, installMockPrices, installMockWallet } from './mocks';
import { ENDPOINTS, RpcRecorder } from './rpc-mocks';

const OUT = 'e2e/screenshots';
const SHOTS: { name: string; viewport: { width: number; height: number }; isMobile: boolean; colorScheme: 'light' | 'dark' }[] = [
  { name: 'settings-rpc-desktop', viewport: { width: 1280, height: 900 }, isMobile: false, colorScheme: 'light' },
  { name: 'settings-rpc-desktop-dark', viewport: { width: 1280, height: 900 }, isMobile: false, colorScheme: 'dark' },
  { name: 'settings-rpc-mobile', viewport: { width: 390, height: 844 }, isMobile: true, colorScheme: 'light' },
];

for (const shot of SHOTS) {
  test(`screenshot ${shot.name}`, async ({ browser }) => {
    const ctx = await browser.newContext({
      viewport: shot.viewport,
      isMobile: shot.isMobile,
      hasTouch: shot.isMobile,
      deviceScaleFactor: shot.isMobile ? 2 : 1,
      colorScheme: shot.colorScheme,
    });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const api = new MockApi();
    await blockExternal(page, true);
    await api.install(page);
    await new MockRpc(DEMO_HOLDINGS).install(page);
    await installMockPrices(page);
    await new RpcRecorder().install(page);
    await installMockWallet(page);
    await page.goto('/');
    await connectAndSignIn(page);
    await page.getByTestId('portfolio-as-of').waitFor({ timeout: 30_000 });

    await page.getByTestId('rpc-settings-button').click();
    await page.getByTestId('rpc-settings').waitFor();
    // a screenshot of dots that were never probed shows nothing: check every chain's own endpoint,
    // then open Ethereum's full list so the per-endpoint health and latency are on screen too
    await page.getByTestId('rpc-check-all').click();
    await page.getByTestId('rpc-check-1').click();
    const list = page.getByTestId('rpc-endpoints-1');
    for (let i = 0; i < ENDPOINTS[1].length; i++) {
      await expect(list.getByTestId(`rpc-endpoint-1-${i}`)).toHaveAttribute('data-health', /ok|slow/, { timeout: 20_000 });
    }
    await expect(page.getByTestId('rpc-health-8453')).toHaveAttribute('data-health', /ok|slow/, { timeout: 20_000 });
    // the clicks above scroll the panel; a shot of the dialog starts at its top, where its own copy is
    await page.getByTestId('rpc-settings').evaluate((el) => el.scrollTo(0, 0));
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/${shot.name}.png` });

    expect(errors).toEqual([]);
    await ctx.close();
  });
}
