// Evidence screenshots for T31 (the home explainer, the route toggle) and U2 (the two-step Uniswap
// route), against the mocks. Written to e2e/screenshots/ beside the ones screenshots.spec.ts takes.
//
// Desktop is full-page; mobile is deliberately viewport-sized, because the point of a phone shot is
// what a phone actually shows — a full-page capture paints the fixed header and tab bar once, at
// the top, which is a picture nobody ever sees.
import { expect, test, type Page } from '@playwright/test';
import { DEMO_HOLDINGS, MockApi, MockRpc, blockExternal, connectAndSignIn, installMockPrices, installMockWallet, payWith } from './mocks';

const OUT = 'e2e/screenshots';

/**
 * A full-page capture paints a STICKY header wherever the page happens to be scrolled — which is
 * how a header ends up drawn across the middle of an otherwise correct screenshot. Put the page
 * back at the top first; the phone shots are viewport-sized and want the element in view instead.
 */
async function settle(page: Page, fullPage: boolean) {
  if (fullPage) await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(150);
}
const VIEWPORTS: {
  name: string;
  viewport: { width: number; height: number };
  isMobile: boolean;
  colorScheme: 'light' | 'dark';
  fullPage: boolean;
}[] = [
  { name: 'desktop', viewport: { width: 1280, height: 900 }, isMobile: false, colorScheme: 'light', fullPage: true },
  { name: 'mobile', viewport: { width: 390, height: 844 }, isMobile: true, colorScheme: 'light', fullPage: false },
  { name: 'desktop-dark', viewport: { width: 1280, height: 900 }, isMobile: false, colorScheme: 'dark', fullPage: true },
];

for (const vp of VIEWPORTS) {
  test(`T31 screenshots ${vp.name}`, async ({ browser }) => {
    const ctx = await browser.newContext({
      viewport: vp.viewport,
      isMobile: vp.isMobile,
      hasTouch: vp.isMobile,
      deviceScaleFactor: vp.isMobile ? 2 : 1,
      colorScheme: vp.colorScheme,
    });
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
    await connectAndSignIn(page);
    await expect(page.locator('html')).toHaveAttribute('data-theme', vp.colorScheme);

    // ---- the home page the admin screenshotted: explainer, chips, the two cards, nothing typed --
    await expect(page.getByTestId('portfolio-chips').locator('.portfolio-chip')).toHaveCount(14, { timeout: 30_000 });
    await page.getByTestId('explainer').waitFor();
    await page.getByTestId('quote-empty').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await settle(page, vp.fullPage);
    await page.screenshot({ path: `${OUT}/deposit-home-${vp.name}.png`, fullPage: vp.fullPage });

    // ---- both ingresses open: the route toggle in the card head (desktop + mobile) --------------
    if (vp.name !== 'desktop-dark') {
      api.uniswapEnabled = true;
      api.defaultRoute = 'uniswap';
      api.uniswapTwoStep = true;
      await page.goto('/'); // the flags are read once, at load
      await page.getByTestId('deposit-form').waitFor();
      await page.getByTestId('route-toggle').waitFor();
      await page.getByLabel('Amount (ETH)').fill('0.1');
      await page.getByTestId('arrives-as').waitFor();
      if (!vp.fullPage) await page.getByTestId('route-toggle').scrollIntoViewIfNeeded();
      await page.evaluate(() => document.fonts.ready);
      await settle(page, vp.fullPage);
      await page.screenshot({ path: `${OUT}/deposit-route-toggle-${vp.name}.png`, fullPage: vp.fullPage });

      // ---- U2 step 1: swap USDC on Uniswap V4 into the user's own wallet -----------------------
      await payWith(page, { chainId: 1, token: 'usdc' });
      await page.getByLabel('Amount (USDC)').fill('250');
      await page.getByTestId('uniswap-step1').waitFor();
      await page.getByTestId('min-out').waitFor();
      if (!vp.fullPage) await page.getByTestId('uniswap-step1').scrollIntoViewIfNeeded();
      await page.evaluate(() => document.fonts.ready);
      await settle(page, vp.fullPage);
      await page.screenshot({ path: `${OUT}/deposit-uniswap-step1-${vp.name}.png`, fullPage: vp.fullPage });

      // ---- U2 step 2: the ETH that actually arrived, as an ordinary direct deposit -------------
      await page.getByTestId('swap-btn').click();
      await page.getByTestId('swap-done').waitFor();
      await page.getByTestId('direct-note').waitFor();
      if (!vp.fullPage) await page.getByTestId('swap-done').scrollIntoViewIfNeeded();
      await page.evaluate(() => document.fonts.ready);
      await settle(page, vp.fullPage);
      await page.screenshot({ path: `${OUT}/deposit-uniswap-step2-${vp.name}.png`, fullPage: vp.fullPage });
    }

    expect(errors).toEqual([]);
    await ctx.close();
  });
}
