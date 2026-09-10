// "Paste a list" on the Schedule page (T17 part B, admin 2026-09-09: "Paste wallets + amounts we
// can do like people do in Disperse contract, where they provide {address}:amount;… Probably you
// know a better way").
//
// The better way is to accept every shape people actually have instead of asking them which one it
// is: Disperse's `0xabc:0.05;0xdef:0.1`, a CSV or TSV straight out of a spreadsheet (header row and
// all), `0xabc = 0.1`, and a third column saying when it should arrive. Each test below is one of
// those shapes, pasted verbatim.
import { expect, test, type Page } from '@playwright/test';
import { getAddress } from 'ethers';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet, walletA, walletB } from './mocks';

/** 2026-09-09 10:15 UTC in a UTC browser: every time below is arithmetic anyone can redo. */
const FIXED = Date.UTC(2026, 8, 9, 10, 15, 0);
const ADDR = (n: number) => getAddress('0x' + n.toString(16).padStart(40, '0'));
const A = walletA.address;
const B = walletB.address;

/** Flip the case of the first cased character: still 40 hex chars, still mixed case, checksum wrong. */
function breakChecksum(address: string): string {
  const body = address.slice(2);
  for (let i = 0; i < body.length; i++) {
    const flipped = body[i] === body[i].toUpperCase() ? body[i].toLowerCase() : body[i].toUpperCase();
    if (flipped !== body[i]) return '0x' + body.slice(0, i) + flipped + body.slice(i + 1);
  }
  throw new Error('no cased character in ' + address);
}

test.use({ timezoneId: 'UTC' });

let api: MockApi;
let pageErrors: string[];

async function boot(page: Page) {
  api = new MockApi();
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page);
  await api.install(page);
  await installMockWallet(page);
  // the API prices against the same instant the page reads, so `release_at` and the headroom on it
  // are one arithmetic rather than two clocks
  api.nowOverrideS = Math.floor(FIXED / 1000);
  await page.clock.setFixedTime(FIXED);
  await page.goto('/schedule');
  await connectAndSignIn(page);
  await page.getByTestId('schedule-form').waitFor();
}

async function paste(page: Page, text: string) {
  if (
    !(await page
      .getByTestId('paste-panel')
      .isVisible()
      .catch(() => false))
  )
    await page.getByTestId('paste-toggle').click();
  await page.getByTestId('paste-input').fill(text);
  await page.getByTestId('paste-summary').waitFor();
}

const lines = (page: Page) => page.getByTestId('paste-lines').locator('li');
const rows = (page: Page) => page.getByTestId('schedule-row');
const addressAt = (page: Page, n: number) => page.getByLabel(`Address ${n}`);
const amountAt = (page: Page, n: number) => page.getByLabel(`Amount ${n}`);

test('Disperse: "0xabc:0.05;0xdef:0.1" on one line is two orders', async ({ page }) => {
  await boot(page);
  await paste(page, `${A}:0.05;${B}:0.1`);
  await expect(page.getByTestId('paste-summary')).toHaveText('2 parsed · 2 ok · 0 errors');
  await expect(lines(page)).toHaveCount(2);
  await expect(lines(page).nth(0)).toHaveAttribute('data-status', 'ok');
  await expect(lines(page).nth(0)).toContainText('✓');
  await expect(lines(page).nth(1)).toContainText('✓');

  await page.getByTestId('paste-apply').click();
  await expect(page.getByTestId('paste-panel')).toHaveCount(0);
  await expect(rows(page)).toHaveCount(2);
  await expect(addressAt(page, 1)).toHaveValue(A);
  await expect(amountAt(page, 1)).toHaveValue('0.05');
  await expect(addressAt(page, 2)).toHaveValue(B);
  await expect(amountAt(page, 2)).toHaveValue('0.1');
  await expect(page.getByLabel('Deliver 1')).toHaveValue('asap');
  await expect(page.getByTestId('total-amount')).toContainText('0.15 ETH');
  expect(pageErrors).toEqual([]);
});

test('a spreadsheet: the CSV header row is skipped and the time column is read', async ({ page }) => {
  await boot(page);
  await paste(page, ['address,amount,deliver_at', `${A},0.05,asap`, `${B},0.1,2026-09-10 03:00`, `${ADDR(3)},0.02,tomorrow`].join('\n'));
  await expect(page.getByTestId('paste-summary')).toHaveText('3 parsed · 3 ok · 0 errors');
  await expect(lines(page).nth(1)).toContainText('2026-09-10 03:00');
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(3);
  await expect(page.getByLabel('Deliver 2')).toHaveValue('custom');
  await expect(page.getByLabel('Custom time 2')).toHaveValue('2026-09-10T03:00');
  await expect(page.getByLabel('Deliver 3')).toHaveValue('tomorrow');
  // and the hint is the same arithmetic the rest of the page does: 66 min ahead of the delivery
  await expect(page.getByTestId('deliver-hint-1')).toContainText('to the bridge at 01:54 · arrives ≈ 03:00');
  expect(pageErrors).toEqual([]);
});

test('TSV pasted out of a spreadsheet, tabs and all', async ({ page }) => {
  await boot(page);
  await paste(page, [`${A}\t0.05\ttonight`, `${B}\t0.1`].join('\n'));
  await expect(page.getByTestId('paste-summary')).toHaveText('2 parsed · 2 ok · 0 errors');
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(2);
  await expect(page.getByLabel('Deliver 1')).toHaveValue('tonight');
  await expect(page.getByLabel('Deliver 2')).toHaveValue('asap'); // no third field → the page's default
  await expect(amountAt(page, 1)).toHaveValue('0.05');
  expect(pageErrors).toEqual([]);
});

test('mixed separators in one paste: space, comma-decimal, "=", colon and a tab', async ({ page }) => {
  await boot(page);
  await paste(page, [`${A} 0,05`, `${B}=0.1`, `${ADDR(3)}:0.02 tomorrow`, `${ADDR(4)}\t0.03\t2h`].join('\n'));
  await expect(page.getByTestId('paste-summary')).toHaveText('4 parsed · 4 ok · 0 errors');
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(4);
  await expect(amountAt(page, 1)).toHaveValue('0.05'); // "0,05" is one number, not two fields
  await expect(amountAt(page, 2)).toHaveValue('0.1');
  await expect(page.getByLabel('Deliver 3')).toHaveValue('tomorrow');
  await expect(page.getByLabel('Deliver 4')).toHaveValue('2h');
  await expect(page.getByTestId('total-amount')).toContainText('0.20 ETH');
  expect(pageErrors).toEqual([]);
});

test('a bad line among good ones is marked, and nothing can be scheduled while it is there', async ({ page }) => {
  await boot(page);
  const badChecksum = breakChecksum(walletA.address);
  await paste(
    page,
    [`${A},0.05,asap`, `0xnot-an-address,0.05`, `${badChecksum},0.05`, `${ADDR(3)},abc`, `${ADDR(4)},0.05,next thursday`, `${B},0.02`].join(
      '\n',
    ),
  );
  await expect(page.getByTestId('paste-summary')).toHaveText('6 parsed · 2 ok · 4 errors');
  await expect(lines(page).nth(0)).toHaveAttribute('data-status', 'ok');
  await expect(lines(page).nth(1)).toHaveAttribute('data-status', 'error');
  await expect(page.getByTestId('paste-note-1')).toHaveText('not an EVM address');
  await expect(page.getByTestId('paste-note-2')).toHaveText('bad checksum');
  await expect(page.getByTestId('paste-note-3')).toHaveText('bad amount');
  await expect(page.getByTestId('paste-note-4')).toHaveText('bad time');
  await expect(lines(page).nth(5)).toHaveAttribute('data-status', 'ok');
  await expect(lines(page).nth(1)).toContainText('✗');

  // nothing is imported, and the page cannot be submitted while the box says this
  await expect(page.getByTestId('paste-apply')).toBeDisabled();
  await expect(page.getByTestId('schedule-submit')).toBeDisabled();
  await expect(page.getByTestId('schedule-problems')).toContainText('the pasted list has 4 lines that cannot be read');
  await expect(rows(page)).toHaveCount(1);

  // fix the list and it goes through
  await page.getByTestId('paste-input').fill([`${A},0.05,asap`, `${B},0.02`].join('\n'));
  await expect(page.getByTestId('paste-summary')).toHaveText('2 parsed · 2 ok · 0 errors');
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(2);
  await expect(page.getByTestId('schedule-submit')).toBeEnabled();
  expect(pageErrors).toEqual([]);
});

test('a duplicate is flagged; a tiny amount is not — there is no minimum any more', async ({ page }) => {
  await boot(page);
  await paste(page, [`${A},0.05`, `${A.toLowerCase()},0.02`, `${B},0.0001`].join('\n'));
  await expect(page.getByTestId('paste-summary')).toHaveText('3 parsed · 3 ok · 0 errors');
  await expect(lines(page).nth(1)).toHaveAttribute('data-status', 'warn');
  await expect(lines(page).nth(1)).toContainText('⚠');
  await expect(page.getByTestId('paste-note-1')).toHaveText('duplicate address'); // lowercase is the same wallet
  // 0.0001 ETH used to be flagged "below the 0.01 ETH minimum" (the economic floor). The bridge is
  // charged explicitly now, so `min_amount_groth` is the 1-groth grid and the line is simply an order.
  await expect(lines(page).nth(2)).toHaveAttribute('data-status', 'ok');
  await expect(page.getByTestId('paste-note-2')).toHaveText('ASAP');
  await expect(page.locator('body')).not.toContainText('minimum');

  await expect(page.getByTestId('paste-apply')).toBeEnabled();
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(3);
  // the duplicate is a real order, and so is the small one — priced by the API, not refused here
  await expect(addressAt(page, 2)).toHaveValue(A);
  await expect(page.getByTestId('amount-error-2')).toHaveCount(0);
  await expect(page.getByTestId('amount-hint-2')).toHaveText('any amount');
  await expect(page.getByTestId('row-total-2')).toContainText('fee 0.000002 · bridge 0.0002 · total 0.000302 ETH');
  await expect(page.getByTestId('schedule-submit')).toBeEnabled();
  expect(pageErrors).toEqual([]);
});

test('append by default, replace when asked', async ({ page }) => {
  await boot(page);
  await addressAt(page, 1).fill(A);
  await amountAt(page, 1).fill('0.03');
  await paste(page, `${B},0.05`);
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(2); // appended, and the typed row survived
  await expect(addressAt(page, 1)).toHaveValue(A);
  await expect(addressAt(page, 2)).toHaveValue(B);

  await paste(page, `${ADDR(7)},0.01`);
  await page.getByTestId('paste-replace').check();
  await expect(page.getByTestId('paste-apply')).toHaveText('Replace the rows with 1');
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(1);
  await expect(addressAt(page, 1)).toHaveValue(ADDR(7));
  expect(pageErrors).toEqual([]);
});

test('200 lines parse in well under a second', async ({ page }) => {
  await boot(page);
  const text = Array.from({ length: 200 }, (_, i) => `${ADDR(i + 1)},0.01,asap`).join('\n');
  await page.getByTestId('paste-toggle').click();
  const t0 = Date.now();
  await page.getByTestId('paste-input').fill(text);
  await expect(page.getByTestId('paste-summary')).toHaveText('200 parsed · 200 ok · 0 errors');
  const ms = Date.now() - t0;
  console.log(`200-line paste: parsed and rendered in ${ms} ms`);
  expect(ms).toBeLessThan(1000);
  await expect(lines(page)).toHaveCount(200);
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(200);
  expect(pageErrors).toEqual([]);
});

test('Copy list exports the canonical form, and pasting it back rebuilds the same rows', async ({ page }) => {
  await boot(page);
  await paste(page, [`${A},0.05,asap`, `${B},0.1,2026-09-10 03:00`].join('\n'));
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(2);

  await page.getByTestId('copy-list').click();
  const exported = await page.getByTestId('copy-list-output').inputValue();
  // address,amount,deliver_at — one order per line, the delivery time in local form
  expect(exported).toBe([`${A},0.05,2026-09-09T10:15`, `${B},0.1,2026-09-10T03:00`].join('\n'));

  // and it is also the import: the same list, read back, is the same two orders
  await paste(page, exported);
  await page.getByTestId('paste-replace').check();
  await expect(page.getByTestId('paste-summary')).toHaveText('2 parsed · 2 ok · 0 errors');
  await page.getByTestId('paste-apply').click();
  await expect(rows(page)).toHaveCount(2);
  await expect(addressAt(page, 1)).toHaveValue(A);
  await expect(amountAt(page, 1)).toHaveValue('0.05');
  await expect(page.getByLabel('Custom time 1')).toHaveValue('2026-09-09T10:15');
  await expect(addressAt(page, 2)).toHaveValue(B);
  await expect(amountAt(page, 2)).toHaveValue('0.1');
  await expect(page.getByLabel('Custom time 2')).toHaveValue('2026-09-10T03:00');

  // the round trip is the same order book the API is given
  await page.getByTestId('schedule-submit').click();
  await expect(page.getByTestId('schedule-result')).toContainText('Scheduled 2 orders');
  const body = api.calls.find((c) => c.path === '/withdrawals' && c.method === 'POST')!.body as {
    items: { W: string; amount_groth: number; deliver_at: number }[];
  };
  expect(body.items).toEqual([
    { W: A, amount_groth: 5000000, deliver_at: FIXED / 1000 },
    { W: B, amount_groth: 10000000, deliver_at: Date.UTC(2026, 8, 10, 3, 0, 0) / 1000 },
  ]);
  expect(pageErrors).toEqual([]);
});
