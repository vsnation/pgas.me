// T31 H (admin, 2026-09-10, with a screenshot): "Registering the deposit… The transaction was sent
// (view) but Pgas.me could not register it — retry above. / that transaction is not visible on
// Ethereum yet."
//
// The money left the wallet the moment it returned a hash. Registering it is bookkeeping, and
// bookkeeping is not the user's job: the hash is written to storage BEFORE the call, the call
// retries itself with backoff, a reload picks the record back up, and the screen says what is true
// instead of asking for a click. A refusal that will never succeed is the one thing that stops it.
import { expect, test, type Page } from '@playwright/test';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet } from './mocks';

const PENDING_KEY = 'pgas.pending-deposit.v1';

let api: MockApi;
let pageErrors: string[];

async function boot(page: Page, mock?: (a: MockApi) => void) {
  api = new MockApi();
  api.armed = true;
  mock?.(api);
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page);
  await api.install(page);
  await installMockWallet(page);
  await page.goto('/');
}

const registrations = () => api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST');
const stored = (page: Page) => page.evaluate((k) => localStorage.getItem(k), PENDING_KEY);

async function deposit(page: Page, amount = '0.2') {
  await connectAndSignIn(page);
  await page.getByLabel('Amount (ETH)').fill(amount);
  await page.getByTestId('deposit-btn').click();
}

test.describe('a sent transaction is never stranded', () => {
  test('409 then 200: it recovers by itself, and never says "retry above"', async ({ page }) => {
    await boot(page, (a) => (a.registerFailures = 1));
    await deposit(page);

    // what the user sees while it happens: the truth, with a way to look at the transaction
    const banner = page.getByTestId('pending-registration');
    await expect(banner).toContainText('Sent — waiting for Ethereum to see it');
    await expect(banner).toContainText('Pgas.me keeps trying by itself');
    await expect(banner.getByRole('link', { name: 'view' })).toHaveAttribute('href', /etherscan\.io\/tx\/0x/);
    await expect(page.locator('body')).not.toContainText('could not register it');
    await expect(page.locator('body')).not.toContainText('retry above');
    expect(await stored(page)).toContain('"quote_id"'); // written BEFORE the call, not after it

    // the first retry lands ~3 s later, with nothing pressed
    await expect(page.getByTestId('deposit-timeline')).toBeVisible({ timeout: 15_000 });
    expect(registrations()).toHaveLength(2);
    await expect(page.getByTestId('pending-registration')).toHaveCount(0);
    expect(await stored(page)).toBeNull(); // cleared on the 2xx, and only then
    expect(pageErrors).toEqual([]);
  });

  test('a reload in the middle picks the record back up and finishes it', async ({ page }) => {
    await boot(page, (a) => (a.registerFailures = 99)); // it keeps refusing while we reload
    await deposit(page);
    await expect(page.getByTestId('pending-registration')).toBeVisible();
    const before = registrations().length;

    await page.reload();
    await expect(page.getByTestId('pending-registration')).toBeVisible({ timeout: 15_000 });
    expect(await stored(page)).toContain('"hash"');
    // the API can see it now
    api.registerFailures = 0;
    await expect(page.getByTestId('deposit-timeline')).toBeVisible({ timeout: 20_000 });
    expect(registrations().length).toBeGreaterThan(before);
    expect(await stored(page)).toBeNull();
    expect(pageErrors).toEqual([]);
  });

  test('a refusal that will never succeed stops, and says what the API said', async ({ page }) => {
    await boot(page, (a) => {
      a.registerFailures = 99;
      a.registerFailStatus = 400;
      a.registerFailDetail = 'src_tx_hash does not belong to this quote';
    });
    await deposit(page);
    const problem = page.getByTestId('register-problem');
    await expect(problem).toHaveText('src_tx_hash does not belong to this quote');
    // one attempt, no loop, and nothing left behind to resume
    await page.waitForTimeout(5000);
    expect(registrations()).toHaveLength(1);
    expect(await stored(page)).toBeNull();
    await expect(page.getByTestId('pending-registration')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test('Retry now is a secondary action — the loop is already running', async ({ page }) => {
    await boot(page, (a) => (a.registerFailures = 99));
    await deposit(page);
    await expect(page.getByTestId('pending-registration')).toBeVisible();
    const before = registrations().length;
    api.registerFailures = 0;
    await page.getByTestId('register-retry').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible({ timeout: 15_000 });
    expect(registrations().length).toBe(before + 1);
    expect(pageErrors).toEqual([]);
  });

  test('a row the API has accepted but not proven yet says it is being confirmed, not that it failed', async ({ page }) => {
    await boot(page, (a) => (a.registerVerified = false));
    await deposit(page);
    const timeline = page.getByTestId('deposit-timeline');
    await expect(timeline).toBeVisible();
    await expect(timeline.locator('.tl-step').first()).toContainText('confirming the transaction');
    await expect(timeline).not.toContainText('Failed');
    await expect(timeline).not.toContainText('could not register');
    expect(pageErrors).toEqual([]);
  });
});
