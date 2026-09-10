// T40 — a withdrawal never fails on the user's side, and every order says when it arrives.
//
// Admin 2026-09-10 11:2xZ, with a screenshot of his own Balance page showing two red **Failed**
// rows: "You understand that withdrawals on user's side cannot be failed … You should show all
// statuses there and estimated time of arrival of his asset."
//
// Those two rows had been REFUNDED in full hours earlier. So this file asserts two things and
// nothing else matters as much: the word Failed is nowhere on the page, and every row says when
// the money arrives — from the API's own `eta_at`/`eta_note`, never from arithmetic done here.
import { expect, test, type Page } from '@playwright/test';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet, walletA } from './mocks';
import { DELAYED_REASON, FLOAT_REASON, FLOAT_UNLOCK_WORDS, W1, installFloatWait, installPayoutStatuses } from './payout-fixtures';

const shown = (g: number) => (g / 1e8).toLocaleString('en-US', { maximumFractionDigits: 6, minimumFractionDigits: 2 });

test.describe('balance: every payout status, and when it arrives', () => {
  let api: MockApi;
  let pageErrors: string[];

  async function boot(page: Page, mock?: (a: MockApi) => void) {
    api = new MockApi();
    installPayoutStatuses(api);
    mock?.(api);
    pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await blockExternal(page);
    await api.install(page);
    await installMockWallet(page);
    await page.goto('/balance');
    await connectAndSignIn(page);
    await page.getByTestId('payouts').waitFor();
  }

  test('the word Failed is not on the page — a legacy row is Delayed, or Returned to balance when the ledger says so', async ({ page }) => {
    await boot(page);
    const rows = page.getByTestId('payouts').locator('[data-testid="payout-row"]');
    await expect(rows).toHaveCount(9);

    // ⛔ THE ONE THAT MATTERS
    await expect(page.locator('body')).not.toContainText('Failed');
    await expect(page.locator('body')).not.toContainText('failed');

    /**
     * ⛔ AND NEITHER IS "REFUNDED" (T48). Admin 2026-09-10 15:24Z, on these two rows: "Avoid
     * status Refunded, it's not clear for the user … Refunded back to the balance or what?" —
     * a refund is our bookkeeping word for it, and it leaves the user asking the question he
     * asked. The row says where the money IS, that nothing left, and what to do next.
     */
    await expect(page.locator('body')).not.toContainText('Refunded');
    await expect(page.locator('body')).not.toContainText('refunded');

    // the two orders that were returned this morning: the evidence is the ledger's `cancel` entry
    const returned = page.getByTestId('payouts').locator('tr[data-shown="refunded"]');
    await expect(returned).toHaveCount(2);
    await expect(returned.first().locator('.pill')).toHaveText('Returned to balance');
    await expect(returned.first().locator('[data-hint="hold"]')).toHaveText(
      `${shown(1000000)} ETH plus its fees are back in Available — nothing was sent; schedule it again when you like`,
    );
    // the amount is the one the user ASKED for, taken from the row — never a sentence with a
    // number in it that no row carries
    await expect(returned.first().locator('[data-hint="hold"]')).toContainText('0.01 ETH');
    // a returned row is finished: there is nothing left to cancel
    await expect(returned.first().getByRole('button', { name: 'Cancel' })).toHaveCount(0);
    // …and the way to use it again is an offer, not an instruction to retype it
    await expect(returned.first().getByRole('button', { name: 'Schedule again' })).toHaveCount(1);

    // a legacy `failed` row the API is still retrying says exactly that, and only because the API
    // published a next attempt — never as this page's own guess
    const legacy = page.getByTestId('payouts').locator('tr[data-request="p-legacy-retry"]');
    await expect(legacy.locator('.pill')).toHaveText('Delayed');
    await expect(legacy.locator('[data-hint="hold"]')).toHaveText('being retried');
    await expect(legacy.locator('[data-hint="next-try"]')).toContainText('next try');
    // …and there is no Cancel on it: the API only takes a cancel for an order it still holds, and
    // a button that answers 409 is worse than no button
    await expect(legacy.getByRole('button', { name: 'Cancel' })).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test('delayed and held: the reason in plain words, the next try, and the money still cancellable', async ({ page }) => {
    await boot(page);
    const delayed = page.getByTestId('payouts').locator('tr[data-status="delayed"]');
    await expect(delayed.locator('.pill')).toHaveText('Delayed');
    await expect(delayed.locator('[data-hint="hold"]')).toHaveText(DELAYED_REASON);
    await expect(delayed.locator('[data-hint="next-try"]')).toContainText('next try');
    // `held` is an operator's word: the user sees the same thing they would see for `delayed`
    const held = page.getByTestId('payouts').locator('tr[data-status="held"]');
    await expect(held.locator('.pill')).toHaveText('Delayed');
    await expect(held.locator('[data-hint="hold"]')).toContainText('your money is still reserved');
    // no flag name ever reaches the screen
    await expect(page.locator('body')).not.toContainText('PGAS_');

    // ⛔ and when the API publishes its OWN verdict (`cancellable`, T40) that is the one the page
    // obeys — the button it draws and the answer the cancel route gives are one decision. (The
    // row is changed on the SAME mock and the page reloaded: a second `boot` would install a
    // second mock wallet on a page that already has one.)
    api.requests.find((r) => r._id === 'p-held')!.cancellable = false;
    await page.reload();
    await page.getByTestId('payouts').waitFor();
    await expect(page.getByTestId('payout-cancel-p-held')).toHaveCount(0);
    await expect(page.getByTestId('payout-cancel-p-delayed')).toHaveCount(1);

    // and the user can end a delayed one and take the money back — that is the only way an order
    // ends without arriving (API_CONTRACT.md § Withdrawals)
    const before = api.balances.ETH.available;
    page.once('dialog', (d) => d.accept());
    await page.getByTestId('payout-cancel-p-delayed').click();
    await expect(page.getByTestId('payouts').locator('tr[data-status="cancelled"]')).toHaveCount(2, { timeout: 15_000 });
    expect(api.balances.ETH.available - before).toBe(2060000); // amount + our fee + the crossing
    expect(pageErrors).toEqual([]);
  });

  // ── T52 ────────────────────────────────────────────────────────────────────────────────────
  // The admin, 2026-09-10 15:36Z, reading his own order: *"again issues no one can understand:
  // WAITING: the wallet can spend 0.00775651 regular / 0 shielded ETH now; 0.01652864 is maturing
  // (max-privacy lock…) … no free coin: ETH spendable coins 4 … `python -m pgasme.beam split`"*.
  test('a wait for the treasury says what is happening and when, once, with no numbers in it', async ({ page }) => {
    await boot(page, (a) => installFloatWait(a));
    const row = page.getByTestId('payouts').locator('tr[data-request="p-float"]');
    await expect(row.locator('.pill')).toHaveText('Delayed');
    // the sentence, verbatim from `payouts.hold_texts` — and SAID ONCE, not under the pill and
    // again under the arrival time: `hold_reason` and `eta_note` are one fact under two names.
    await expect(row.locator('[data-hint="hold"]')).toHaveText(FLOAT_REASON);
    await expect(row.locator('[data-eta="note"]')).toHaveCount(0);
    await expect(row.getByText(FLOAT_REASON)).toHaveCount(1);
    // …and the arrival it publishes is the moment the lock ends, which the sentence names
    await expect(row.locator('[data-eta="relative"]')).toContainText('in 2 h');
    expect(FLOAT_REASON).toContain(FLOAT_UNLOCK_WORDS);
    // ⛔ NOT ONE OPERATOR NUMBER ON THE PAGE: those live on `hold_detail`, which the account
    // route strips before the row ever reaches a browser.
    for (const operatorWord of ['0.00775651', 'maturing', 'max-privacy', 'spendable coins', 'python -m']) {
      await expect(page.locator('body')).not.toContainText(operatorWord);
    }
    expect(pageErrors).toEqual([]);
  });

  test('a returned order can be scheduled again — the same wallet, the same amount, ASAP', async ({ page }) => {
    await boot(page);
    const returned = page.getByTestId('payouts').locator('tr[data-request="p-legacy-refunded-1"]');
    await returned.getByRole('button', { name: 'Schedule again' }).click();

    // it lands on the form, not on a page the user has to find their way out of
    await expect(page).toHaveURL(/\/schedule$/);
    await page.getByTestId('schedule-form').waitFor();
    await expect(page.getByLabel('Address 1')).toHaveValue(W1);
    // the amount is the one that was ASKED for — the order never delivered anything
    await expect(page.getByLabel('Amount 1')).toHaveValue('0.01');
    await expect(page.getByLabel('Deliver 1')).toHaveValue('asap');
    // one row, not one row plus the blank the form opens with
    await expect(page.getByTestId('schedule-row')).toHaveCount(1);
    // ⛔ and the handoff is spent: nothing about that order is in the URL to be copied or logged,
    // and coming back to the form later opens it empty
    expect(page.url()).not.toContain(W1);
    await page.locator('[data-tab="balance"]').first().click();
    await page.locator('[data-tab="schedule"]').first().click();
    await expect(page.getByLabel('Address 1')).toHaveValue('');
    expect(pageErrors).toEqual([]);
  });

  test("a cancel of the user's own says Cancelled, and where the money went", async ({ page }) => {
    await boot(page);
    // their action, their word — and the one thing they need to know about it (T48 item 2)
    const cancelled = page.getByTestId('payouts').locator('tr[data-status="cancelled"]');
    await expect(cancelled.locator('.pill')).toHaveText('Cancelled');
    await expect(cancelled.locator('[data-hint="hold"]')).toHaveText(
      'you cancelled this one — the amount and its fees are back in Available',
    );
    // a cancelled order is not offered a Schedule again button: nothing went wrong with it
    await expect(cancelled.getByRole('button', { name: 'Schedule again' })).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test('bridging, delivering, sent: the pills, the confirmations and the delivery transaction', async ({ page }) => {
    await boot(page);
    const t = page.getByTestId('payouts');
    await expect(t.locator('tr[data-status="bridging"] .pill')).toHaveText('Bridging');
    await expect(t.locator('tr[data-status="bridging"] [data-hint="confirmations"]')).toHaveText('43/61 Beam confirmations');
    const sent = t.locator('tr[data-status="sent"]');
    await expect(sent.locator('.pill')).toHaveText('Sent');
    await expect(sent.locator('a[href^="https://etherscan.io/tx/0x7777"]')).toHaveCount(1);
    await expect(sent).toContainText('delivered');
    // internal identifiers stay internal: no Beam txid, no message id, no order id
    await expect(t).not.toContainText('c3d4e5f6');
    expect(pageErrors).toEqual([]);
  });

  test('the columns: wallet, what it receives, the fee split, both times and the arrival', async ({ page }) => {
    await boot(page);
    const t = page.getByTestId('payouts');
    for (const h of ['Status', 'Wallet', 'Wallet receives', 'Fee', 'Deliver at', 'To the bridge at', 'Arrives'])
      await expect(t.locator('thead')).toContainText(h);

    // the arrival is the API's `eta_at`, relative AND absolute, with its own note under it
    const delayed = t.locator('tr[data-status="delayed"]');
    await expect(delayed.locator('[data-eta="relative"]')).toContainText('in ');
    await expect(delayed.locator('[data-eta="absolute"]')).not.toHaveText('–');
    await expect(delayed.locator('[data-eta="note"]')).toContainText('next try in 5 minutes');
    /**
     * T35b L5 — the next attempt is a DURATION, from the API's ISO-8601 `next_try_at`. The API
     * used to put the raw unix instant in the note ("next try at 1788950000"); a page that
     * renders a timestamp at a person is the same defect one layer down. A row with no
     * `next_try_at` says nothing at all — no time is invented here.
     */
    await expect(delayed.locator('[data-eta="next-try"]')).toHaveText(/^retries in ~\d+ min$/);
    await expect(t.locator('tr[data-status="held"] [data-eta="next-try"]')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('next try at 1');

    // a row whose API published no ETA gets a dash — this page never adds up a time of its own
    const refunded = t.locator('tr[data-shown="refunded"]').first();
    await expect(refunded.locator('[data-eta="relative"]')).toHaveCount(0);
    await expect(refunded.getByTestId('payout-eta-p-legacy-refunded-1')).toHaveText('—');

    // T35 on this page too: the row that delivers less than it asked for says why
    const fromAmount = t.locator('tr[data-request="p-from-amount"]');
    await expect(fromAmount).toContainText(shown(4881372));
    await expect(page.getByTestId('payout-fee-mode-p-from-amount')).toContainText('fees taken from the amount');
    await expect(page.getByTestId('payout-fee-mode-p-from-amount')).toContainText(`of ${shown(5000000)} ETH asked`);
    /**
     * ⛔ T35b — AND THE TWO NUMBERS ARE DIFFERENT. On a stored payout row `amount_groth` IS the
     * delivery (`_write_items`: the release spends that field) and what the user typed lives
     * beside it as `requested_groth`. The page read `amount_groth` for "asked", so against a
     * real row it printed the delivered figure twice — "0.0488 ETH · of 0.0488 ETH asked" — and
     * the one thing this line exists to say was the one thing it could not say. The fixture
     * carries the row's real shape now, so this assertion can fail.
     */
    expect(shown(4881372)).not.toBe(shown(5000000));
    await expect(page.getByTestId('payout-fee-mode-p-from-amount')).not.toContainText(`of ${shown(4881372)} ETH asked`);
    // the fee column carries both halves, ours and the crossing
    await expect(page.getByTestId('payout-bridge-p-from-amount')).toHaveText(`+ ${shown(21000)} bridge`);
    expect(pageErrors).toEqual([]);
  });

  test('T45: a settled crossing shows what came back off the bridge fee', async ({ page }) => {
    /**
     * The bridge fee is quoted at request time from a live gas price plus the headroom the wait
     * needs — an ESTIMATE — and the crossing then costs what it costs. Until 2026-09-10 the
     * difference stayed with the treasury (`headroom_for`: *"unspent headroom stays with the
     * treasury"*), which is not "the bridge at cost": two live orders funded 14,733 groth against
     * a 12,778-groth crossing and 1,955 groth of each user's money was kept for a cost nobody
     * incurred. It is credited back to Available at settlement now, and this line is where the
     * user sees that it happened.
     */
    await boot(page);
    const t = page.getByTestId('payouts');
    await expect(page.getByTestId('payout-bridge-p-sent')).toHaveText(`+ ${shown(20000)} bridge`);
    // ⛔ IN T48'S WORDS, NOT THE BRIEF'S. The brief wrote "bridge fee refunded 0.00001955 ETH";
    // the admin banned "Refunded" on this page an hour later ("it's not clear for the user …
    // Refunded back to the balance or what?"), and the test above asserts the word is nowhere on
    // it. Same fact, the vocabulary the returned rows already use.
    await expect(page.getByTestId('payout-bridge-refund-p-sent')).toHaveText(`bridge fee: ${shown(1955)} ETH back in Available`);
    // the row's own arithmetic: funded − paid == refunded
    const row = api.requests.find((r) => r._id === 'p-sent')!;
    expect((row.bridge_fee_groth as number) - (row.relayer_fee_groth as number)).toBe(row.bridge_fee_refund_groth);

    // ⛔ ONLY where there IS one. Every other row funded a crossing that has not settled (or
    // settled for more than it funded), and a "refunded 0.00 ETH" line under each of them is
    // noise on the page a user opens when something is wrong.
    await expect(t.locator('[data-testid^="payout-bridge-refund-"]')).toHaveCount(1);
    expect(pageErrors).toEqual([]);
  });

  test('the page lead, and the note that says an order is never lost', async ({ page }) => {
    await boot(page);
    await expect(page.getByTestId('page-lead')).toContainText('follow every payout to the wallet');
    await expect(page.getByTestId('payouts-note')).toContainText('Delayed');
    await expect(page.getByTestId('payouts-note')).toContainText('the money stays reserved');
    // the story below is one table, and it agrees with the one above: no "Failed" there either
    await expect(page.getByTestId('timeline')).toBeVisible();
    // three rows still CARRY the legacy status; none of them SAYS it
    await expect(page.getByTestId('timeline').locator('[data-status="failed"]')).toHaveCount(3);
    await expect(page.getByTestId('timeline').locator('[data-shown="refunded"]')).toHaveCount(2);
    await expect(page.getByTestId('timeline').locator('[data-status="failed"][data-shown="delayed"]')).toHaveCount(1);
    /**
     * T48 — and in the STORY the same fact is told with the tile's own name: the timeline is
     * "what happened to this money", and what happened is that it went back to Available. The
     * order book above says "Returned to balance", where the row is an order and "balance" is
     * the thing an order comes out of; one vocabulary (lib/payouts.ts), two surfaces.
     */
    await expect(page.getByTestId('timeline').locator('[data-shown="refunded"]').first()).toHaveText('Returned to Available');
    expect(pageErrors).toEqual([]);
  });

  test('screenshot: the returned rows, their sentence and the way back to the form', async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: 'light' });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const mock = new MockApi();
    installPayoutStatuses(mock);
    await blockExternal(page, true);
    await mock.install(page);
    await installMockWallet(page);
    await page.goto('/balance');
    await connectAndSignIn(page);
    await page.getByTestId('payouts').waitFor();
    await page.getByTestId('payouts-card').scrollIntoViewIfNeeded();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: 'e2e/screenshots/balance-returned-desktop.png' });
    expect(errors).toEqual([]);
    await ctx.close();
  });

  test('screenshots: the Balance page with every payout status on it', async ({ browser }) => {
    for (const vp of [
      { name: 'desktop', viewport: { width: 1280, height: 900 }, isMobile: false, fullPage: true },
      { name: 'mobile', viewport: { width: 390, height: 844 }, isMobile: true, fullPage: false },
    ]) {
      const ctx = await browser.newContext({
        viewport: vp.viewport,
        isMobile: vp.isMobile,
        hasTouch: vp.isMobile,
        deviceScaleFactor: vp.isMobile ? 2 : 1,
        colorScheme: 'light',
      });
      const page = await ctx.newPage();
      const errors: string[] = [];
      page.on('pageerror', (e) => errors.push(e.message));
      const mock = new MockApi();
      installPayoutStatuses(mock);
      await blockExternal(page, true);
      await mock.install(page);
      await installMockWallet(page);
      // straight to the page this shot is of: the Deposit page is another worker's surface this
      // round, and a page error of theirs would fail a Balance screenshot for no Balance reason
      await page.goto('/balance');
      await connectAndSignIn(page);
      await expect(page.getByTestId('connected-address')).toContainText(walletA.address.slice(0, 6));
      await page.getByTestId('payouts').waitFor();
      if (!vp.fullPage) await page.getByTestId('payouts-card').scrollIntoViewIfNeeded();
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `e2e/screenshots/balance-statuses-${vp.name}.png`, fullPage: vp.fullPage });
      expect(errors).toEqual([]);
      await ctx.close();
    }
  });
});
