// T54 — the RPC settings popup. The admin's words (2026-09-10 16:47Z): "provide configuration popup
// where RPCs for all chains with dropdowns where user can select one of many".
//
// What these tests hold the feature to:
//   · every chain the app scans has a row, and the row offers the endpoints the app actually knows;
//   · a pick survives a reload;
//   · a custom endpoint is only accepted when it answers `eth_chainId` with the chain it is being
//     offered for — a wrong-chain, an http:// and a dead URL are each refused IN WORDS and change
//     nothing;
//   · the scan really starts at the user's pick (the RPC double records the order);
//   · "Reset to defaults" puts every chain back and forgets the custom endpoints;
//   · the popup is reachable from the portfolio's Refresh area, not only from the header.
import { expect, test, type Page } from '@playwright/test';
import { DEMO_HOLDINGS, MockApi, MockRpc, blockExternal, connectAndSignIn, installMockPrices, installMockWallet } from './mocks';
import { CUSTOM_DEAD, CUSTOM_OK, CUSTOM_WRONG_CHAIN, ENDPOINTS, RpcRecorder } from './rpc-mocks';

const STORE_KEY = 'pgas.rpc.v1';
const EVM_CHAINS = [1, 42161, 8453, 1514, 25];

let api: MockApi;
let rpc: RpcRecorder;
let pageErrors: string[];

async function boot(page: Page) {
  api = new MockApi();
  rpc = new RpcRecorder();
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page);
  await api.install(page);
  await new MockRpc(DEMO_HOLDINGS).install(page);
  await installMockPrices(page);
  await rpc.install(page); // last, so it wins the endpoints MockRpc also claims
  await installMockWallet(page);
  await page.goto('/');
}

const dialog = (page: Page) => page.getByTestId('rpc-settings');
const row = (page: Page, chainId: number) => page.getByTestId(`rpc-chain-${chainId}`);
const select = (page: Page, chainId: number) => page.getByTestId(`rpc-select-${chainId}`);

async function openSettings(page: Page) {
  await page.getByTestId('rpc-settings-button').click();
  await dialog(page).waitFor();
}

async function closeSettings(page: Page) {
  await dialog(page).getByRole('button', { name: 'Close' }).click();
  await expect(dialog(page)).toHaveCount(0);
}

/** Offer a custom endpoint for a chain and press the button that accepts it. */
async function offerCustom(page: Page, chainId: number, url: string) {
  await select(page, chainId).selectOption('__custom__');
  await page.getByTestId(`rpc-custom-${chainId}`).fill(url);
  await page.getByTestId(`rpc-custom-save-${chainId}`).click();
}

test.describe('RPC settings', () => {
  test('every chain the app scans has a row, offering the endpoints the app knows', async ({ page }) => {
    await boot(page);
    await openSettings(page);

    await expect(dialog(page).getByTestId('rpc-settings-note')).toContainText(
      "These endpoints are used by your browser to read balances and confirm transactions. Pgas.me's own servers use their own.",
    );
    for (const id of EVM_CHAINS) await expect(row(page, id)).toBeVisible();
    // Solana and Tron are on the API's chain list and have no eth_* RPC to configure
    await expect(row(page, 7565164)).toHaveCount(0);
    await expect(row(page, 728126428)).toHaveCount(0);

    // the dropdown IS the app's endpoint list, in the app's order, plus the custom entry
    const values = await select(page, 1)
      .locator('option')
      .evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value));
    expect(values).toEqual([...ENDPOINTS[1], '__custom__']);
    await expect(select(page, 1)).toHaveValue(ENDPOINTS[1][0]);
    await expect(row(page, 1)).toHaveAttribute('data-active', ENDPOINTS[1][0]);
    expect(pageErrors).toEqual([]);
  });

  test('a pick is remembered across a reload', async ({ page }) => {
    await boot(page);
    await openSettings(page);
    await select(page, 1).selectOption(ENDPOINTS[1][1]);
    await expect(row(page, 1)).toHaveAttribute('data-active', ENDPOINTS[1][1]);
    await closeSettings(page);

    await page.reload();
    await openSettings(page);
    await expect(select(page, 1)).toHaveValue(ENDPOINTS[1][1]);
    await expect(row(page, 1)).toHaveAttribute('data-active', ENDPOINTS[1][1]);
    expect(pageErrors).toEqual([]);
  });

  test('the scan starts at the endpoint the user picked', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await page.getByTestId('portfolio-as-of').waitFor({ timeout: 30_000 });
    expect(rpc.order(1)[0]).toBe(ENDPOINTS[1][0]); // the default, before anyone chose anything

    await openSettings(page);
    await select(page, 1).selectOption(ENDPOINTS[1][1]);
    await closeSettings(page);

    rpc.reset();
    await page.getByRole('button', { name: 'Refresh' }).first().click();
    await expect(page.getByRole('button', { name: 'Scanning…' })).toHaveCount(0, { timeout: 30_000 });
    expect(rpc.order(1)[0]).toBe(ENDPOINTS[1][1]);
    expect(pageErrors).toEqual([]);
  });

  test('a custom endpoint on the wrong chain is refused, and changes nothing', async ({ page }) => {
    await boot(page);
    await openSettings(page);
    await offerCustom(page, 8453, CUSTOM_WRONG_CHAIN);

    await expect(page.getByTestId('rpc-custom-error-8453')).toContainText(/answered for chain 1 \(Ethereum\), not Base/);
    await expect(page.getByTestId('rpc-custom-error-8453')).toContainText('Not saved');
    await expect(row(page, 8453)).toHaveAttribute('data-active', ENDPOINTS[8453][0]);
    expect(await page.evaluate((k) => localStorage.getItem(k), STORE_KEY)).toBeNull();
    expect(pageErrors).toEqual([]);
  });

  test('an http endpoint is refused without a call, and a dead one is refused with the reason', async ({ page }) => {
    await boot(page);
    await openSettings(page);

    rpc.reset();
    await offerCustom(page, 8453, 'http://192.168.1.9:8545');
    await expect(page.getByTestId('rpc-custom-error-8453')).toContainText(/Only https endpoints/);
    expect(rpc.hits).toEqual([]); // refused on its face: nothing was called

    await page.getByTestId('rpc-custom-8453').fill(CUSTOM_DEAD);
    await page.getByTestId('rpc-custom-save-8453').click();
    await expect(page.getByTestId('rpc-custom-error-8453')).toContainText(/Nothing answered at dead-node\.test/);
    await expect(row(page, 8453)).toHaveAttribute('data-active', ENDPOINTS[8453][0]);
    expect(pageErrors).toEqual([]);
  });

  test('a custom endpoint that is the chain it claims is accepted, and Reset puts everything back', async ({ page }) => {
    await boot(page);
    await openSettings(page);

    await select(page, 1).selectOption(ENDPOINTS[1][2]);
    await offerCustom(page, 8453, CUSTOM_OK);
    await expect(row(page, 8453)).toHaveAttribute('data-active', CUSTOM_OK, { timeout: 15_000 });
    await expect(select(page, 8453)).toHaveValue(CUSTOM_OK);
    // the accepted endpoint was probed, so its health is known rather than assumed
    await expect(page.getByTestId('rpc-health-8453')).toHaveAttribute('data-health', /ok|slow/);

    await page.reload();
    await openSettings(page);
    await expect(select(page, 8453)).toHaveValue(CUSTOM_OK);
    await expect(select(page, 1)).toHaveValue(ENDPOINTS[1][2]);

    await page.getByTestId('rpc-reset').click();
    await expect(row(page, 1)).toHaveAttribute('data-active', ENDPOINTS[1][0]);
    await expect(row(page, 8453)).toHaveAttribute('data-active', ENDPOINTS[8453][0]);
    const values = await select(page, 8453)
      .locator('option')
      .evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value));
    expect(values).toEqual([...ENDPOINTS[8453], '__custom__']); // the custom endpoint is forgotten
    expect(await page.evaluate((k) => localStorage.getItem(k), STORE_KEY)).toBeNull();
    expect(pageErrors).toEqual([]);
  });

  test('checking a chain probes every endpoint it offers and shows what each answered', async ({ page }) => {
    await boot(page);
    await openSettings(page);
    await page.getByTestId('rpc-check-1').click();

    const list = page.getByTestId('rpc-endpoints-1');
    await expect(list).toBeVisible();
    for (let i = 0; i < ENDPOINTS[1].length; i++) {
      await expect(list.getByTestId(`rpc-endpoint-1-${i}`)).toHaveAttribute('data-health', /ok|slow/, { timeout: 20_000 });
    }
    await expect(list.getByTestId('rpc-endpoint-1-0')).toContainText(/ms/);
    expect(pageErrors).toEqual([]);
  });

  test('the portfolio can open it too', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await page.getByTestId('portfolio-as-of').waitFor({ timeout: 30_000 });
    await page.getByTestId('rpc-settings-portfolio').click();
    await expect(dialog(page)).toBeVisible();
    expect(pageErrors).toEqual([]);
  });
});
