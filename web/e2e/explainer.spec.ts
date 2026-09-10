// T31 A + B + B′ (2026-09-10) — what Pgas.me is, said on the page itself.
//
// The admin sent two screenshots. The first was the Quote card: "You need to provide info that
// deposit comes to ETH blockchain from where it will be distributed among chains user wants." The
// second was the whole signed-in Deposit page: "Add explanation about what is pgas.me. If you see
// my screenshot, it's not clear what to do here. For us it's clear, not for new users."
//
// So: an explainer for EVERY visitor, open by default, dismissible and recoverable; a lead line on
// the card that asks for something; a Quote card that says what will appear in it; and, when the
// deposit lands, the next step named as the tab it is on.
import { expect, test, type Page } from '@playwright/test';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet, signedIn } from './mocks';

let api: MockApi;
let pageErrors: string[];

async function boot(page: Page, path = '/', mock?: (a: MockApi) => void) {
  api = new MockApi();
  mock?.(api);
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page);
  await api.install(page);
  await installMockWallet(page);
  await page.goto(path);
}

test.describe('the explainer', () => {
  test('a signed-out visitor is told what this is, in the page, before connecting anything', async ({ page }) => {
    await boot(page);
    const panel = page.getByTestId('explainer');
    await expect(panel).toBeVisible();
    await expect(panel.getByTestId('explainer-what')).toContainText('Private gas for fresh EVM wallets');
    await expect(panel.getByTestId('explainer-what')).toContainText('Beam’s confidential ledger');
    await expect(panel.getByTestId('explainer-what')).toContainText('nothing on any public chain links them to the deposit');

    // three steps, the one you are on lit up, each one the tab it names
    const steps = panel.getByTestId('explainer-steps').locator('.explainer-step');
    await expect(steps).toHaveCount(3);
    await expect(steps.nth(0)).toHaveClass(/current/);
    await expect(steps.nth(0)).toContainText('Deposit');
    await expect(steps.nth(1)).toContainText('Balance');
    await expect(steps.nth(2)).toContainText('Schedule');
    await expect(steps.nth(1)).not.toHaveClass(/current/);

    // the promise and the limit, in one line, and a way in
    await expect(panel.getByTestId('explainer-limits')).toContainText('No public chain links a funded wallet to your deposit');
    await expect(panel.getByTestId('explainer-limits')).toContainText('custodian for now');
    await expect(panel.getByTestId('explainer-connect')).toBeVisible();

    // and a step is a way to get there
    await panel.getByTestId('explainer-steps').locator('.explainer-step').nth(2).getByRole('button').click();
    await expect(page).toHaveURL(/\/schedule$/);
    expect(pageErrors).toEqual([]);
  });

  test('a signed-in visitor gets the same panel — that is the whole point of B′', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(page.getByTestId('explainer')).toBeVisible();
    // …minus the button they have already pressed
    await expect(page.getByTestId('explainer-connect')).toHaveCount(0);
    await expect(page.getByTestId('explainer-collapsed')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test('Dismiss is remembered, and the "?" link brings it back', async ({ page }) => {
    await boot(page);
    await page.getByTestId('explainer-dismiss').click();
    await expect(page.getByTestId('explainer')).toHaveCount(0);
    await expect(page.getByTestId('explainer-show')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('pgas.explainer.v1'))).toBe('dismissed');

    // it survives a reload — and a first-time visitor (nothing stored) never sees it collapsed
    await page.reload();
    await expect(page.getByTestId('explainer-show')).toBeVisible();
    await page.getByTestId('explainer-show').click();
    await expect(page.getByTestId('explainer')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('pgas.explainer.v1'))).toBeNull();
    await page.reload();
    await expect(page.getByTestId('explainer')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('the two cards say what they want and what will appear — and the quote says where the money lands', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(signedIn(page)).toBeVisible();

    // the card that asks for something says what it is asking for
    await expect(page.getByTestId('deposit-lead')).toContainText('Pick something you hold');
    await expect(page.getByTestId('deposit-lead')).toContainText('enter an amount');

    // the empty Quote card describes what will be in it, rather than "enter an amount"
    const empty = page.getByTestId('quote-empty');
    await expect(empty).toContainText('what lands in your private balance');
    await expect(empty).toContainText('as ETH on Ethereum through the Beam bridge');
    await expect(empty).toContainText('what the bridge charges');

    // T31 A — the line the admin asked for, under the tile, with the numbers untouched
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('quote-out')).toContainText('0.1');
    const arrives = page.getByTestId('arrives-as');
    await expect(arrives).toHaveText(
      'Arrives as ETH on Ethereum through the Beam bridge. From your balance you schedule payouts to the wallets you choose — on Ethereum now, other chains as they are enabled.',
    );
    // truthful about today: multi-chain payout is designed, not live
    await expect(page.locator('body')).not.toContainText('payouts to any chain');
    expect(pageErrors).toEqual([]);
  });

  test('the same sentence family is in How it works, and the panel is still six steps', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await page.getByTestId('how-it-works').click();
    const modal = page.getByTestId('how-it-works-modal');
    await expect(modal.locator('li')).toHaveCount(6);
    await expect(modal.locator('li').nth(2)).toContainText('what lands on Ethereum goes straight on to Pgas.me through the Beam bridge');
    await expect(modal.locator('li').nth(5)).toContainText('on Ethereum now, other chains as they are enabled');
    expect(pageErrors).toEqual([]);
  });

  test('a credited deposit points at the next step, and it is a tab away', async ({ page }) => {
    await boot(page, '/', (a) => {
      a.armed = true;
    });
    await connectAndSignIn(page);
    await page.getByLabel('Amount (ETH)').fill('0.2');
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();

    // the operator's side credits it; the page finds out on its next account read
    api.deposits[0].status = 'credited';
    api.deposits[0].value_groth = 20000000;
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    const next = page.getByTestId('go-schedule');
    await expect(next).toBeVisible();
    await expect(next).toHaveText('Now schedule payouts →');
    await next.click();
    await expect(page).toHaveURL(/\/schedule$/);
    expect(pageErrors).toEqual([]);
  });
});
