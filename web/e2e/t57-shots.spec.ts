// T57 evidence screenshots: the one money page in both modes.
import { test } from '@playwright/test';
import { DEMO_HOLDINGS, MockApi, MockRpc, blockExternal, connectAndSignIn, installMockPrices, installMockWallet } from './mocks';

const OUT = 'e2e/screenshots';
const SHOTS = [
  { name: 't57-deposit-desktop', path: '/deposit', viewport: { width: 1280, height: 900 }, mobile: false, scheme: 'light' as const },
  { name: 't57-withdraw-desktop', path: '/withdraw', viewport: { width: 1280, height: 900 }, mobile: false, scheme: 'light' as const },
  { name: 't57-deposit-mobile', path: '/deposit', viewport: { width: 390, height: 844 }, mobile: true, scheme: 'light' as const },
];

for (const shot of SHOTS) {
  test(`shot ${shot.name}`, async ({ browser }) => {
    const ctx = await browser.newContext({
      viewport: shot.viewport, isMobile: shot.mobile, hasTouch: shot.mobile,
      deviceScaleFactor: shot.mobile ? 2 : 1, colorScheme: shot.scheme,
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
    await page.goto(shot.path);
    await connectAndSignIn(page);
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${OUT}/${shot.name}.png`, fullPage: !shot.mobile });
    console.log(shot.name, 'pageerrors:', errors.length, errors.slice(0, 2).join(' | '));
    await ctx.close();
  });
}
