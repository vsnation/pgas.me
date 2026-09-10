// T35 — "Wallet receives X · debited Y", and a batch that is never blocked because the fees do not
// fit on top (admin 2026-09-10 10:3xZ).
//
// The rule, in the admin's words: "You need to take fees above the amount user requested. If user
// requested to get 0.01 ETH, we should deposit 0.01 ETH; only if user doesn't have deposit to pay
// gas fees and 2% fees to us, we take it from sending amount, so he gets less than 0.01 ETH."
//
// Everything asserted here is a number the API sent. The page's job is to show which of the two
// rules each row is under and what that means for the wallet, and to STOP CLAIMING "you receive
// exactly what you enter" the moment that stops being true.
import { expect, test, type Page } from '@playwright/test';
import * as SAY from './api-sentences';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet, walletA, walletB } from './mocks';

/** groth → exactly what `lib/format.fmtGroth` prints, so a fixture number becomes screen text. */
const shown = (g: number) => (g / 1e8).toLocaleString('en-US', { maximumFractionDigits: 6, minimumFractionDigits: 2 });

const ADDR3 = '0x3333333333333333333333333333333333333333';

test.describe('schedule: fees on top, or out of the amount', () => {
  let api: MockApi;
  let pageErrors: string[];

  async function boot(page: Page, mock?: (a: MockApi) => void) {
    api = new MockApi();
    mock?.(api);
    pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await blockExternal(page);
    await api.install(page);
    await installMockWallet(page);
    await page.goto('/schedule');
    await connectAndSignIn(page);
  }

  test('on top when Available can pay them: the wallet receives exactly what was typed', async ({ page }) => {
    await boot(page);
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.03');
    await page.getByTestId('row-total-0').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    const q = api.previewResponses.at(-1)!.items[0];
    expect(q.fee_mode).toBe('on_top');
    expect(q.delivered_groth).toBe(3000000);
    expect(q.debited_groth).toBe(3080000);
    // the two numbers a person cares about, both the API's
    await expect(page.getByTestId('row-receives-0')).toHaveText(`Wallet receives ${shown(3000000)} ETH · debited ${shown(3080000)} ETH`);
    // …and the itemisation under them, unchanged
    await expect(page.getByTestId('row-total-0')).toContainText(`fee ${shown(q.fee_groth)} · bridge ${shown(q.bridge_fee_groth)}`);
    // nothing says the fees came out of the amount, because they did not
    await expect(page.getByTestId('row-fee-mode-0')).toHaveCount(0);
    await expect(page.getByTestId('totals-fee-mode')).toHaveCount(0);
    await expect(page.getByTestId('fee-line')).toContainText('you receive exactly what you enter');
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    expect(pageErrors).toEqual([]);
  });

  test('from the amount when they do not fit: the row says so, and the batch still goes', async ({ page }) => {
    // 0.05 ETH available. The first order can pay its fees on top; the second cannot, and takes
    // them out of its own amount instead of being refused.
    await boot(page, (a) => (a.balances.ETH.available = 5000000));
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.03');
    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(ADDR3);
    await page.getByLabel('Amount 2').fill('0.019');
    await page.getByTestId('row-total-1').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    const priced = api.previewResponses.at(-1)!;
    // IN ROW ORDER: the first row was allocated first, so the second is the one that has to pay
    // its fees out of the amount — not the other way round
    expect(priced.items.map((i) => i.fee_mode)).toEqual(['on_top', 'from_amount']);
    const second = priced.items[1];
    expect(second.debited_groth).toBe(1900000); // exactly the amount leaves the balance
    expect(second.delivered_groth).toBeLessThan(1900000); // and the wallet gets a little less
    // the API's own arithmetic holds: delivered + our fee + the crossing == what was debited
    expect(second.delivered_groth! + second.fee_groth + second.bridge_fee_groth).toBe(second.debited_groth);

    await expect(page.getByTestId('row-receives-1')).toHaveText(
      `Wallet receives ${shown(second.delivered_groth!)} ETH · debited ${shown(1900000)} ETH`,
    );
    await expect(page.getByTestId('row-fee-mode-1')).toHaveText('fees taken from the amount — not enough balance to pay them on top');
    await expect(page.getByTestId('row-fee-mode-0')).toHaveCount(0);

    // the totals say it too: Σ delivered, Σ debited, and one line explaining the difference
    await expect(page.getByTestId('total-delivered')).toHaveText(`${shown(priced.totals.delivered_groth!)} ETH`);
    await expect(page.getByTestId('total-debited')).toHaveText(`${shown(priced.totals.total_debited_groth)} ETH`);
    await expect(page.getByTestId('totals-fee-mode')).toContainText('Nothing is blocked.');
    // ⛔ the sentence that would now be false is gone
    await expect(page.getByTestId('fee-line')).not.toContainText('you receive exactly what you enter');

    // THE POINT: the batch is not refused. Σ debited ≤ Available, so the button is live.
    expect(priced.batch.ok).toBe(true);
    await expect(page.getByTestId('batch-problem')).toHaveCount(0);
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();

    // and what was quoted is what is charged: the orders that land carry the same three numbers
    const availableBefore = api.balances.ETH.available;
    await page.getByTestId('schedule-submit').click();
    await expect(page.getByTestId('schedule-result')).toContainText('Scheduled 2 orders');
    expect(availableBefore - api.balances.ETH.available).toBe(priced.totals.total_debited_groth);
    const written = api.requests.filter((r) => String(r._id).startsWith('req-new'));
    expect(written.map((r) => r.fee_mode).sort()).toEqual(['from_amount', 'on_top']);
    const fromAmountRow = written.find((r) => r.fee_mode === 'from_amount')!;
    expect(fromAmountRow.delivered_groth).toBe(second.delivered_groth);
    // ⛔ T35b — the WRITTEN row has the API's shape: `amount_groth` is the DELIVERY (the release
    // spends it) and `requested_groth` is what was typed. They are different numbers here, which
    // is what makes the line below able to fail.
    expect(fromAmountRow.amount_groth).toBe(second.delivered_groth);
    expect(fromAmountRow.requested_groth).toBe(1900000);
    expect(fromAmountRow.amount_groth).not.toBe(fromAmountRow.requested_groth);
    // the order table shows the delivery, with what was asked for beside it
    const orders = page.getByTestId('schedule-orders');
    await expect(orders.locator('tr.row-new')).toHaveCount(2, { timeout: 15_000 });
    const cell = page.getByTestId(`order-fee-mode-${fromAmountRow._id}`);
    await expect(cell).toContainText(`of ${shown(1900000)} ETH asked`);
    await expect(cell).not.toContainText(`of ${shown(second.delivered_groth!)} ETH asked`);
    expect(pageErrors).toEqual([]);
  });

  test('T35b: a delivery that would be mostly fees is refused on its own row, both ways out named', async ({ page }) => {
    /**
     * The admin's rule is "withdrawal can be any" — which is about the FLOOR, not about handing
     * someone 0.00020408 ETH against a 0.00040817 ETH debit and calling it a withdrawal. The API
     * states the assumption in the refusal (`price_item`, `fees_dominate`), and the boundary is
     * exact: 40 818 groth is the smallest amount whose from-amount delivery still covers half of
     * it, and one groth less is not.
     */
    await boot(page, (a) => (a.balances.ETH.available = 40818));
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.00040817');
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
    await expect(page.getByTestId('row-problem-0')).toHaveText(SAY.feesDominate(40817, 61634, 40818));
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();
    // the row is still priced, and it still adds up — a refused row renders like any other
    const bad = api.previewResponses.at(-1)!.items[0];
    expect([bad.ok, bad.problem_code, bad.fee_mode, bad.delivered_groth]).toEqual([false, 'fees_dominate', 'from_amount', 20408]);
    expect(bad.delivered_groth! + bad.fee_groth + bad.bridge_fee_groth).toBe(bad.debited_groth);

    // ONE GROTH MORE and half of it survives the fees: the same balance, an order that goes
    await page.getByLabel('Amount 1').fill('0.00040818');
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
    await expect(page.getByTestId('row-problem-0')).toHaveCount(0);
    const ok = api.previewResponses.at(-1)!.items[0];
    expect([ok.ok, ok.fee_mode, ok.delivered_groth]).toEqual([true, 'from_amount', 20409]);
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    expect(pageErrors).toEqual([]);
  });

  test('an amount that cannot even pay the crossing is refused on its own row, in the API words', async ({ page }) => {
    // Available is 0.03 + a hair: the first row takes nearly all of it, and the second is smaller
    // than the crossing it would have to fund out of itself.
    await boot(page, (a) => (a.balances.ETH.available = 3080015));
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.03');
    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(ADDR3);
    await page.getByLabel('Amount 2').fill('0.0000001'); // 10 groth, against a 20 000-groth crossing
    // the API's own sentence, verbatim: 10 groth cannot pay a 20 000-groth crossing out of itself,
    // and the smallest amount that could is 20 002 groth (1 + its 1-groth fee + the crossing)
    await expect(page.getByTestId('row-problem-1')).toHaveText(SAY.tooSmallForBridge(10, 20000, 20002));
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();
    /**
     * BOTH sentences are on the screen, and both are the API's, because both are true: this row
     * cannot pay its own crossing (the row's refusal, where it can be acted on) AND the list as
     * priced no longer fits inside Available (the batch's verdict, under the totals). The refused
     * row is still priced on top — the API does that too — so its full cost is what the batch is
     * ruled against. A page that showed only one of them would be hiding half the reason.
     */
    await expect(page.getByTestId('batch-problem')).toHaveCount(1);
    await expect(page.getByTestId('row-problem-0')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test("a batch that does not fit at all is still the batch's refusal — in the API's own sentence", async ({ page }) => {
    await boot(page, (a) => {
      a.balances.ETH.available = 5000000;
      // the wording production actually answers with (api/pgasme/routers/withdrawals.py
      // `batch_problem`), rather than this mock's older, friendlier paraphrase
      a.batchSentence = SAY.batchShort;
    });
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.03');
    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(ADDR3);
    await page.getByLabel('Amount 2').fill('0.03');
    await page.getByTestId('row-total-1').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    const sentence = SAY.batchShort('ETH', 6160000, 5000000, 1160000, 200);
    await expect(page.getByTestId('batch-problem')).toHaveText(sentence);
    // …verbatim, and on the batch, never on a row — even though the API DOES mark the row that
    // ran out of money (T35b). `problem_code: "batch"` is a verdict about the LIST, so the page
    // reads the code and shows the sentence once, under the totals, where it can be acted on.
    await expect(page.getByTestId('row-problem-0')).toHaveCount(0);
    await expect(page.getByTestId('row-problem-1')).toHaveCount(0);
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();
    expect(api.previewResponses.at(-1)!.items.map((i) => [i.ok, i.problem_code])).toEqual([
      [true, undefined],
      [false, 'batch'],
    ]);
    expect(pageErrors).toEqual([]);
  });

  test('an API build that publishes no split shows a dash, and derives nothing', async ({ page }) => {
    await boot(page, (a) => (a.publishFeeSplit = false));
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.03');
    await page.getByTestId('row-total-0').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    const q = api.previewResponses.at(-1)!;
    expect(q.items[0].delivered_groth).toBeUndefined();
    expect(q.totals.delivered_groth).toBeUndefined();
    // ⛔ "the API did not say" is a dash. The amount in the box is what the user ASKED for, and
    // the page does not turn a request into a promise about what will be delivered.
    await expect(page.getByTestId('row-receives-0')).toHaveText(`Wallet receives — · debited ${shown(3080000)} ETH`);
    // and the totals line is simply not there, rather than a dash beside four real numbers
    await expect(page.getByTestId('total-delivered')).toHaveCount(0);
    await expect(page.getByTestId('total-debited')).toHaveText(`${shown(3080000)} ETH`);
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    expect(pageErrors).toEqual([]);
  });

  // ══════════════════════ T45 — the bridge fee is an estimate, and the numbers are human ═══════

  test('the fee copy says the bridge fee is an estimate and the remainder comes back', async ({ page }) => {
    /**
     * T45 (admin 2026-09-10 15:0xZ, "Make sure you have correct gas fees"). The page used to say
     * "charged at cost — incl. headroom for the wait", and the headroom was KEPT: two live orders
     * funded 0.00014733 ETH against a crossing that cost 0.00012778 and the treasury kept the
     * 0.00001955 difference each. The API refunds it at settlement now, so the sentence says what
     * is actually true — an estimate, with the remainder coming back.
     */
    await boot(page);
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.03');
    await page.getByTestId('row-total-0').waitFor();
    await expect(page.getByTestId('bridge-fee-note')).toHaveText(
      'Bridge fee: an estimate — whatever the crossing does not use comes back to your balance.',
    );
    await expect(page.getByTestId('fee-line')).toContainText(
      'bridge fee: an estimate, whatever the crossing does not use comes back to your balance',
    );
    // the claim that was never true is gone from both lines
    await expect(page.getByTestId('bridge-fee-note')).not.toContainText('headroom');
    await expect(page.getByTestId('fee-line')).not.toContainText('at cost');
    expect(pageErrors).toEqual([]);
  });

  // ═══════════════ T45 item 5 — the sentences are in ETH, and Available is on screen ════════════

  test('the refusal is in ETH, names the top-up, and says nothing about groth', async ({ page }) => {
    /**
     * The admin hit this 409 himself (2026-09-10 15:35Z): *"insufficient ETH balance: this batch
     * needs 2900846 groth and 2834074 groth is available — top up shortfall_groth 66772 groth"* →
     * *"Why in groth? People don't understand nothing in it. You should have human readable errors
     * in ETH and show what actually available to user."*
     *
     * `groth` is an internal unit and `shortfall_groth` is a FIELD NAME. The sentence is the API's,
     * verbatim (`api-sentences.batchShort`, copied from `withdrawals.batch_problem`); the page's
     * job is to render it and to have the balance on screen while the user is typing amounts.
     */
    await boot(page, (a) => (a.balances.ETH.available = 5000000));
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.03');
    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(ADDR3);
    await page.getByLabel('Amount 2').fill('0.03');
    await page.getByTestId('row-total-1').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    await expect(page.getByTestId('batch-problem')).toHaveText(
      'Not enough ETH: this batch needs 0.0616 ETH and 0.05 ETH is available. Top up 0.0116 ETH and every wallet ' +
        'receives the full amount it asked for (fees are charged on top once your balance covers them).',
    );
    // ⛔ THE ONE THAT MATTERS: no internal unit and no field name anywhere on the screen
    await expect(page.locator('body')).not.toContainText('groth');
    await expect(page.locator('body')).not.toContainText('shortfall');
    expect(pageErrors).toEqual([]);
  });

  test('Available is on screen where the amounts are typed, not only under the totals', async ({ page }) => {
    await boot(page, (a) => (a.balances.ETH.available = 2834074));
    await expect(page.getByTestId('form-available')).toHaveText('Available 0.028341 ETH');
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.01');
    await page.getByTestId('row-total-0').waitFor();
    // the same number under the totals, from the same source — the API's `available_groth`
    await expect(page.getByTestId('total-available')).toHaveText('0.028341 ETH');
    expect(pageErrors).toEqual([]);
  });

  test('Use max fills the largest amount the balance covers WITH the fees on top', async ({ page }) => {
    /**
     * ⛔ THE NUMBER IS THE API'S (`max_on_top_groth`). A "Use max" that computed
     * `available − 2 % − bridge` in the browser would be a second implementation of the fee model,
     * on the user's own money, and it would be wrong by the rounding the API absorbs into our fee.
     */
    await boot(page, (a) => (a.balances.ETH.available = 1000000));
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.001');
    await page.getByTestId('row-total-0').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    const max = api.previewResponses.at(-1)!.items[0].max_on_top_groth;
    expect(max).toBe(960784); // 1,000,000 − the 20,000 crossing, less the 2% on what is left
    await page.getByTestId('use-max-0').click();
    await expect(page.getByLabel('Amount 1')).toHaveValue('0.00960784');
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    // …and what it filled in is an ON-TOP row that spends the balance exactly
    const q = api.previewResponses.at(-1)!;
    expect(q.items[0].fee_mode).toBe('on_top');
    expect(q.items[0].delivered_groth).toBe(max);
    expect(q.totals.total_debited_groth).toBe(1000000);
    expect(q.batch.ok).toBe(true);
    await expect(page.getByTestId('batch-problem')).toHaveCount(0);
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    expect(pageErrors).toEqual([]);
  });

  test('a row with nothing left for it offers no Use max at all', async ({ page }) => {
    // the first row spends the balance; there is nothing for the second to ask for, and a button
    // that fills in 0 is worse than no button
    await boot(page, (a) => (a.balances.ETH.available = 1000000));
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.0099');
    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(ADDR3);
    await page.getByLabel('Amount 2').fill('0.00001');
    await page.getByTestId('row-total-1').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
    expect(api.previewResponses.at(-1)!.items[1].max_on_top_groth).toBe(0);
    await expect(page.getByTestId('use-max-1')).toHaveCount(0);
    await expect(page.getByTestId('use-max-0')).toHaveCount(1);
    expect(pageErrors).toEqual([]);
  });

  test('the page lead says what this page is for', async ({ page }) => {
    await boot(page);
    await expect(page.getByTestId('page-lead')).toContainText('List the wallets you want funded');
    await expect(page.getByTestId('page-lead')).toContainText('one order per line');
    expect(pageErrors).toEqual([]);
  });

  test('screenshots: a list where one order pays its fees out of the amount', async ({ browser }) => {
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
      mock.balances.ETH.available = 5000000;
      await blockExternal(page, true);
      await mock.install(page);
      await installMockWallet(page);
      await page.goto('/schedule');
      await connectAndSignIn(page);
      await expect(page.getByTestId('connected-address')).toContainText(walletA.address.slice(0, 6));
      await page.getByLabel('Address 1').fill(walletB.address);
      await page.getByLabel('Amount 1').fill('0.03');
      await page.getByTestId('schedule-add').click();
      await page.getByLabel('Address 2').fill(ADDR3);
      await page.getByLabel('Amount 2').fill('0.019');
      await page.getByTestId('row-fee-mode-1').waitFor();
      await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
      if (!vp.fullPage) await page.getByTestId('row-fee-mode-1').scrollIntoViewIfNeeded();
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: `e2e/screenshots/schedule-from-amount-${vp.name}.png`, fullPage: vp.fullPage });
      expect(errors).toEqual([]);
      await ctx.close();
    }
  });
});

// ── T52 ──────────────────────────────────────────────────────────────────────────────────────
// "Why do you accept user request if user cannot spend this?" (admin, 2026-09-10 15:38Z) →
// "Next time when user wants to withdraw just tell him he can't" (15:45Z).
//
// The live wallet that minute could move **0.00775651 ETH** and was holding **0.01652864** more
// inside a max-privacy lock until 12 Sep 23:21Z, with no early exit. Three of his own 0.01 ETH
// orders were accepted anyway, because acceptance checked the USER's balance and never the
// treasury's spendable float — and the release gate then said so, hourly, in its own vocabulary,
// on his order page.
//
// What the form has to do about it: show what CAN be delivered today beside Available, refuse the
// list in the API's own words, and put the refusal where the user can act on it — under the
// totals, never as "your third wallet is wrong".
test.describe('schedule: what the treasury can deliver today', () => {
  let api: MockApi;
  let pageErrors: string[];

  async function boot(page: Page, mock?: (a: MockApi) => void) {
    api = new MockApi();
    // the box, 2026-09-10 15:36Z
    api.treasuryFloatGroth = 775_651;
    api.treasuryUnlockAt = Date.UTC(2026, 8, 12, 23, 21) / 1000;
    api.treasuryUnlockWords = 'Sat 12 Sep, 23:21Z';
    mock?.(api);
    pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await blockExternal(page);
    await api.install(page);
    await installMockWallet(page);
    await page.goto('/schedule');
    await connectAndSignIn(page);
  }

  test('an order the treasury cannot move today is refused in the API’s own words', async ({ page }) => {
    await boot(page);
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.01'); // the admin's own order
    await page.getByTestId('treasury-problem').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');

    const q = api.previewResponses.at(-1)!;
    const deliverable = 775_651 - q.items[0].bridge_fee_groth;
    // THE sentence, verbatim from `withdrawals.treasury_float_problem`
    await expect(page.getByTestId('treasury-problem')).toHaveText(SAY.treasuryShort(deliverable, 'Sat 12 Sep, 23:21Z'));
    // …and it is NOT rendered as a fault in the row: the row is fine, we are short
    await expect(page.getByTestId('row-problem-0')).toHaveCount(0);
    // the button is down, so the 409 is a thing the user never has to meet
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();
    // …and what CAN be asked for is on screen, beside Available, in the same units
    await expect(page.getByTestId('form-deliverable')).toHaveText(`· Deliverable now ${shown(q.treasury!.deliverable_now_groth!)} ETH`);
    expect(pageErrors).toEqual([]);
  });

  test('an order it can move is accepted, and nothing is said about the treasury', async ({ page }) => {
    await boot(page);
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.005');
    await page.getByTestId('row-total-0').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
    await expect(page.getByTestId('treasury-problem')).toHaveCount(0);
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    expect(api.previewResponses.at(-1)!.treasury!.ok).toBe(true);
    expect(pageErrors).toEqual([]);
  });

  test('a build that publishes no treasury verdict says nothing at all about it', async ({ page }) => {
    // ⛔ null is not zero: an API that cannot measure the treasury (or does not know about it)
    // must not make this page invent a refusal — or a "Deliverable now 0" that reads as one.
    await boot(page, (a) => {
      a.treasuryFloatGroth = null;
    });
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.01');
    await page.getByTestId('row-total-0').waitFor();
    await expect(page.getByTestId('schedule-totals')).toHaveAttribute('data-busy', 'no');
    await expect(page.getByTestId('treasury-problem')).toHaveCount(0);
    await expect(page.getByTestId('form-deliverable')).toHaveCount(0);
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    expect(pageErrors).toEqual([]);
  });
});
