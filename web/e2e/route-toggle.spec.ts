// T31 D2 (admin, 2026-09-10): a toggle — the cross-chain route by default, switched to Uniswap V4
// when we want to show what that route does differently.
//
// The control exists only when BOTH paths are open — one option is not a choice. It starts where
// the API says (`ingress.default_route`), the user's own choice wins over that and is remembered,
// and the Uniswap half is refused, in words, wherever that route cannot run. The provider behind
// the cross-chain path is never named on screen: to a depositor it is "Cross-chain".
import { expect, test, type Page } from '@playwright/test';
import { DEMO_HOLDINGS, MockApi, MockRpc, blockExternal, connectAndSignIn, installMockPrices, installMockWallet, payWith } from './mocks';

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

const lastQuote = () => api.calls.filter((c) => c.path === '/quote').pop()?.body as { route?: string } | undefined;

test.describe('the route toggle', () => {
  test('with one route open there is no control at all', async ({ page }) => {
    await boot(page, { mock: (a) => (a.armed = true) }); // uniswap closed: today's world
    await connectAndSignIn(page);
    await expect(page.getByTestId('route-toggle')).toHaveCount(0);
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('quote-out')).toBeVisible();
    // every quote names a route (T31b item 7); with no toggle to read, that route is `auto`
    expect(lastQuote()?.route).toBe('auto');
    expect(pageErrors).toEqual([]);
  });

  test('with both open the control appears, starts where the API says, and sends what it says', async ({ page }) => {
    await boot(page, {
      mock: (a) => {
        a.armed = true;
        a.uniswapEnabled = true;
        a.defaultRoute = 'xchain';
      },
    });
    await connectAndSignIn(page);
    const toggle = page.getByTestId('route-toggle');
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute('data-route', 'xchain');
    await expect(page.getByTestId('route-xchain')).toHaveAttribute('aria-checked', 'true');
    // the provider behind it is never named
    await expect(toggle).toContainText('Cross-chain');
    await expect(toggle).toContainText('Uniswap V4');

    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('quote-out')).toBeVisible();
    // from Ethereum there is no cross-chain order to ask for: the API's resolver decides, and with
    // `default_route: xchain` it answers the classic direct/swap shape (T31b items 1 and 7)
    await expect.poll(() => lastQuote()?.route).toBe('auto');
    await expect(page.getByTestId('deposit-form')).toContainText('What to deposit');

    // switch it: the request names the route, and the card leads with it
    await page.getByTestId('route-uniswap').click();
    await expect(toggle).toHaveAttribute('data-route', 'uniswap');
    await page.getByLabel('Amount (ETH)').fill('0.15');
    await expect.poll(() => lastQuote()?.route).toBe('uniswap');
    await expect(page.getByTestId('deposit-form')).toContainText('Pay with Uniswap V4');
    expect(pageErrors).toEqual([]);
  });

  test("the API's default is uniswap when it says so, and the user's own choice outlives a reload", async ({ page }) => {
    await boot(page, {
      mock: (a) => {
        a.armed = true;
        a.uniswapEnabled = true;
        a.defaultRoute = 'uniswap';
      },
    });
    await connectAndSignIn(page);
    await expect(page.getByTestId('route-toggle')).toHaveAttribute('data-route', 'uniswap');
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect.poll(() => lastQuote()?.route).toBe('uniswap');

    await page.getByTestId('route-xchain').click();
    expect(await page.evaluate(() => localStorage.getItem('pgas.route.v1'))).toBe('xchain');
    await page.goto('/');
    // the API still prefers uniswap; the person who pressed the other one still gets the other one
    await expect(page.getByTestId('route-toggle')).toHaveAttribute('data-route', 'xchain');
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('quote-out')).toBeVisible();
    await expect.poll(() => lastQuote()?.route).toBe('auto');
    expect(pageErrors).toEqual([]);
  });

  test("auto means whatever the API's default means — the mock resolves it the way the API does", async ({ page }) => {
    // T31b items 1 + 7. The API's ONE resolver (`uniswap.wants_uniswap`) reads `auto` through
    // `PGAS_INGRESS_DEFAULT_ROUTE`; the mock mirrors it, so a client that shipped a second opinion
    // about what `auto` means could not stay green here.
    await boot(page, {
      mock: (a) => {
        a.armed = true;
        a.uniswapEnabled = true;
        a.uniswapTwoStep = true;
        a.defaultRoute = 'uniswap';
      },
    });
    await connectAndSignIn(page);
    await page.getByTestId('route-xchain').click(); // the user asks for the other half
    await payWith(page, { chainId: 1, token: 'usdc' });
    await page.getByLabel('Amount (USDC)').fill('250');
    await expect.poll(() => lastQuote()?.route).toBe('auto');
    // …and the API — not this client — is what answered uniswap for it
    await expect(page.getByTestId('uniswap-step1')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('the Uniswap half is refused where that route cannot run, and says which reason it is', async ({ page }) => {
    await boot(page, {
      rpc: true,
      mock: (a) => {
        a.armed = true;
        a.uniswapEnabled = true;
        a.defaultRoute = 'uniswap';
      },
    });
    await connectAndSignIn(page);
    await expect(page.getByTestId('portfolio-chips').locator('.portfolio-chip')).toHaveCount(14, { timeout: 30_000 });
    const uni = page.getByTestId('route-uniswap');
    await expect(uni).toBeEnabled();

    // off Ethereum: the route takes ETH out of an Ethereum pool, and there is nothing else to say
    await payWith(page, { chainId: 42161, token: 'usdc' });
    await expect(uni).toBeDisabled();
    await expect(uni).toHaveAttribute('title', /Ethereum only/);

    // back on Ethereum, but asking for an asset that route does not take
    await payWith(page, { chainId: 1, token: 'usdc' });
    await expect(uni).toBeEnabled();
    await page.getByRole('radio', { name: 'DAI' }).click();
    await expect(uni).toBeDisabled();
    await expect(uni).toHaveAttribute('title', /it takes ETH out/);
    expect(pageErrors).toEqual([]);
  });
});
