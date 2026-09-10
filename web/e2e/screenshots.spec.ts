// Evidence screenshots (playbook §3): the connected, signed-in Deposit, Balance and Schedule pages
// at desktop 1280×900, mobile 390×844 and desktop in the dark theme, against the mocks. Written to
// e2e/screenshots/.
//
// Desktop shots are full-page. The mobile ones are deliberately viewport-sized: the point of the
// mobile pass is the chrome — the one-row header, the bottom tab bar and the sticky Schedule totals
// — and a full-page capture paints fixed elements once, at the top, which is exactly what a phone
// never shows.
import { expect, test } from '@playwright/test';
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
  walletB,
} from './mocks';

const OUT = 'e2e/screenshots';
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
  test(`screenshots ${vp.name}`, async ({ browser }) => {
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
    await blockExternal(page, true);
    await api.install(page);
    await new MockRpc(DEMO_HOLDINGS).install(page);
    await installMockPrices(page);
    await installMockWallet(page);
    await page.goto('/');
    // what a first-time visitor sees: one Connect button (the header's) and one-line empty states
    await page.getByTestId('deposit-form').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/landing-${vp.name}.png`, fullPage: vp.fullPage });

    await connectAndSignIn(page);
    // the theme actually in force in this context, before anything is captured
    await expect(page.locator('html')).toHaveAttribute('data-theme', vp.colorScheme);

    // Deposit: the chips are the point of the shot, so wait for the whole row, then a live quote
    await expect(page.getByTestId('portfolio-chips').locator('.portfolio-chip')).toHaveCount(14, { timeout: 30_000 });
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await page.getByTestId('direct-note').waitFor();
    await page.getByTestId('lands-in').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/deposit-${vp.name}.png`, fullPage: vp.fullPage });

    // Balance: the four tiles with their dollar lines, and the one timeline underneath
    await goTab(page, 'balance');
    await page.getByTestId('timeline').waitFor();
    await expect(page.getByTestId('balance-ETH').locator('.tile-usd').first()).toBeVisible({ timeout: 15_000 });
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/balance-${vp.name}.png`, fullPage: vp.fullPage });

    // Schedule with two orders half-typed: the checksummed address, the live totals, the delivery
    // presets with their "to the bridge at …" line, and the existing orders with their pills
    await goTab(page, 'schedule');
    await page.getByTestId('schedule-form').waitFor();
    // the untouched form: one muted hint and nothing in red (screen review 2026-09-10), and on a
    // phone the sticky totals bar is not drawn over the Totals card it repeats
    await page.getByTestId('schedule-hint').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/schedule-pristine-${vp.name}.png`, fullPage: vp.fullPage });

    await page.getByLabel('Address 1').fill(walletB.address.toLowerCase());
    await page.getByLabel('Amount 1').fill('0.05');
    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(getAddress('0x' + '33'.repeat(20)));
    await page.getByLabel('Amount 2').fill('0.02');
    await page.getByLabel('Deliver 2').selectOption('tonight');
    await page.getByTestId('schedule-orders').waitFor();
    /**
     * Wait for the API's price for THIS list before capturing. Since 2026-09-10 every number on the
     * card comes off `POST /v1/withdrawals/preview`, ~300 ms after the last edit — a shot taken
     * before it lands is a page full of 0.00, which is not what anyone sees a moment later.
     */
    await page.getByTestId('row-total-1').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
    await expect(page.getByTestId('total-debited')).not.toHaveText('0.00 ETH');
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/schedule-${vp.name}.png`, fullPage: vp.fullPage });

    // the paste box, with a list half-checked: the ✓/✗/⚠ column is the thing to look at
    await page.getByTestId('paste-toggle').click();
    await page
      .getByTestId('paste-input')
      .fill(
        [
          `${getAddress('0x' + '44'.repeat(20))},0.05,asap`,
          `${getAddress('0x' + '55'.repeat(20))}:0.1;${getAddress('0x' + '66'.repeat(20))}:0.02`,
          `${getAddress('0x' + '77'.repeat(20))}\t0.03\t2026-09-10 03:00`,
          '0xnot-an-address,0.05',
          `${getAddress('0x' + '44'.repeat(20))},0.001`,
        ].join('\n'),
      );
    await page.getByTestId('paste-summary').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/schedule-paste-${vp.name}.png`, fullPage: vp.fullPage });
    await page.getByTestId('paste-toggle').click();

    // and the panel the intros now point at
    await page.getByTestId('how-it-works').click();
    await page.getByTestId('how-it-works-modal').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/how-it-works-${vp.name}.png` });

    /**
     * Deposit again, with the Uniswap ingress open — the primary path (T12). The flags are read
     * once, at load, so the state is set and the page loaded again; the session is in localStorage
     * and the wallet reconnects silently, so nothing is clicked twice. The shots above stay on the
     * direct path deliberately: both are shipping paths, and both are worth a look.
     */
    api.uniswapEnabled = true;
    api.armed = true;
    await page.goto('/');
    await page.getByTestId('deposit-form').waitFor();
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await page.getByTestId('uniswap-note').waitFor();
    await page.getByTestId('min-out').waitFor();
    await page.getByTestId('price-impact').waitFor();
    // the phone shot stays viewport-sized, but here the quote panel IS the change — so it is what
    // the viewport is pointed at, rather than the chrome the other mobile shots are for
    if (!vp.fullPage) await page.getByTestId('uniswap-note').scrollIntoViewIfNeeded();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/deposit-uniswap-${vp.name}.png`, fullPage: vp.fullPage });
    expect(errors).toEqual([]);
    await ctx.close();
  });
}
