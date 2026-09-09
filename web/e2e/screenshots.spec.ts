// Evidence screenshots (playbook §3): the connected, signed-in Deposit and Wallets pages at
// desktop 1280×900 and mobile 390×844, against the mocks. Written to e2e/screenshots/.
import { expect, test } from '@playwright/test';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet } from './mocks';

const OUT = 'e2e/screenshots';
const VIEWPORTS: { name: string; viewport: { width: number; height: number }; isMobile: boolean; colorScheme: 'light' | 'dark' }[] = [
  { name: 'desktop', viewport: { width: 1280, height: 900 }, isMobile: false, colorScheme: 'light' },
  { name: 'mobile', viewport: { width: 390, height: 844 }, isMobile: true, colorScheme: 'light' },
  { name: 'desktop-dark', viewport: { width: 1280, height: 900 }, isMobile: false, colorScheme: 'dark' },
];

for (const vp of VIEWPORTS) {
  test(`screenshots ${vp.name}`, async ({ browser }) => {
    const ctx = await browser.newContext({
      viewport: vp.viewport,
      isMobile: vp.isMobile,
      deviceScaleFactor: vp.isMobile ? 2 : 1,
      colorScheme: vp.colorScheme,
    });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const api = new MockApi();
    await blockExternal(page, true);
    await api.install(page);
    await installMockWallet(page);
    await page.goto('/');
    await connectAndSignIn(page);
    await page.getByTestId('portfolio-empty').waitFor({ timeout: 30_000 });
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await page.getByTestId('unarmed-banner').waitFor();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `${OUT}/deposit-${vp.name}.png`, fullPage: true });

    await page.getByRole('button', { name: 'Wallets' }).click();
    await page.getByTestId('destinations').waitFor();
    await page.getByTestId('generate-btn').click();
    await page.getByTestId('generated').waitFor();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${OUT}/wallets-${vp.name}.png`, fullPage: true });
    expect(errors).toEqual([]);
    await ctx.close();
  });
}
