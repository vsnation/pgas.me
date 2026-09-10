// U2 (admin, 2026-09-10): "don't deploy, make it 2 clicks, like Swap crosschain > Hook."
//
// So the Uniswap ingress is the `swap` handshake with Uniswap V4 as the venue and nothing of ours
// on chain: step 1 swaps the token to ETH into the USER's own wallet through the Universal Router,
// and step 2 is the ordinary direct deposit of what actually arrived. Native ETH never needs step 1.
//
// ⛔ AND IT ASKS FOR NO SIGNATURE (T31b item 9, 2026-09-10). Step 1 used to end with an EIP-712
// `PermitSingle` the wallet signed — and the signature went nowhere: it belongs inside the router's
// `execute` calldata, which the API builds, and no field has ever carried it back. A prompt whose
// answer is discarded is worse than no prompt, so the router's allowance is granted with a
// TRANSACTION (`Permit2.approve`) like the token's, and `eth_signTypedData_v4` is a method this
// flow never calls. The mock wallet refuses it and records every attempt, so that is assertable.
//
// T46 (2026-09-10, against the SHIPPED U2-api of 15:15Z): the API answers `approvals[]` — every
// transaction that must precede the swap, IN ORDER, and only the short ones. There can be nought,
// one, two or three of them (three when the token holds a short but non-zero allowance: a USDT-style
// `approve` reverts unless it is zeroed first). The client sends what it is handed, in that order,
// waiting for each receipt, and STOPS at the first refusal with the step named. What is asserted
// below is the bytes: the API builds the calldata now, so a client that quietly re-encodes it — or
// drops the entry it does not recognise — is a swap that reverts and a user who paid gas for it.
//
// The other two facts this file holds down: the swap is never registered as a deposit, and the
// pre-shipped field names (`approval` + `permit_tx` / `permit_fallback_tx`) still work, because the
// API and the SPA are deployed separately and either order has to be a working build.
import { expect, test, type Page } from '@playwright/test';
import {
  DEFAULT_PROFILES,
  MockApi,
  PERMIT2,
  SWAP_DEADLINE,
  UNIVERSAL_ROUTER,
  USDC,
  blockExternal,
  connectAndSignIn,
  erc20ApproveData,
  installWallets,
  payWith,
  permit2ApproveData,
} from './mocks';

const ETH_PIPE = '0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96';
/** 250 USDC, six decimals — what every test below types into the amount field. */
const AMOUNT = 250_000_000n;
const RESET_DATA = erc20ApproveData(PERMIT2, 0n);
const APPROVE_DATA = erc20ApproveData(PERMIT2, AMOUNT);
const PERMIT_DATA = permit2ApproveData(USDC, UNIVERSAL_ROUTER, AMOUNT, SWAP_DEADLINE);

let api: MockApi;
let pageErrors: string[];

async function boot(page: Page, opts: { mock?: (a: MockApi) => void } = {}) {
  api = new MockApi();
  api.armed = true;
  api.uniswapEnabled = true;
  api.uniswapTwoStep = true;
  api.defaultRoute = 'uniswap';
  opts.mock?.(api);
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page);
  await api.install(page);
  await installWallets(page, DEFAULT_PROFILES);
  await page.goto('/');
}

const sent = (page: Page) => page.evaluate(() => (window as never as Record<string, any>).__mock.state.sent);
const walletState = (page: Page) => page.evaluate(() => (window as never as Record<string, any>).__mock.state);

/** Type 250 USDC in and wait for the two-step card. */
async function quoteUsdc(page: Page) {
  await payWith(page, { token: 'usdc' });
  await page.getByLabel('Amount (USDC)').fill('250');
  await expect(page.getByTestId('uniswap-step1')).toBeVisible();
}

/**
 * The user presses Cancel on the Nth wallet prompt of this page — the one thing the mock wallet
 * itself has no knob for, because a rejection is not a property of a wallet, it is a thing a person
 * does once. Patching the provider the app already holds is the honest way to say that: every other
 * call still goes to the real mock, and the rejected transaction is never recorded as sent.
 */
async function rejectNthSend(page: Page, nth: number) {
  await page.evaluate((n) => {
    const provider = (window as never as Record<string, any>).__mock.provider;
    const original = provider.request.bind(provider);
    let seen = 0;
    provider.request = async (args: { method: string; params?: unknown[] }) => {
      if (args.method === 'eth_sendTransaction' && ++seen === n) {
        const e: Error & { code?: number } = new Error('User rejected the request.');
        e.code = 4001; // EIP-1193 userRejectedRequest
        throw e;
      }
      return original(args);
    };
  }, nth);
}

test.describe('the Uniswap route, in two clicks', () => {
  test('three approvals: a USDT-style reset, the token, the router — then the swap and the deposit', async ({ page }) => {
    await boot(page, { mock: (a) => (a.uniswapNeedsAllowanceReset = true) });
    await connectAndSignIn(page);
    await quoteUsdc(page);

    // the card says what it is: two steps, and both of them the user's own
    await expect(page.getByTestId('deposit-form')).toContainText('Pay with Uniswap V4');
    await expect(page.getByTestId('uniswap-primary')).toHaveText('two steps, both in your own wallet');
    const step1 = page.getByTestId('uniswap-step1');
    await expect(step1).toContainText('1 · Swap USDC → ETH on Uniswap V4');
    await expect(step1).toContainText('the ETH lands in your wallet, never ours');
    await expect(page.getByTestId('swap-panel')).toHaveCount(0); // not the router's swap mode
    await expect(page.getByTestId('deposit-btn')).toHaveCount(0); // nothing to deposit yet
    await expect(page.getByTestId('approve-btn')).toHaveCount(0); // one button, not four

    // the promise names all THREE, in the order they will be sent — and never a signature
    const note = page.getByTestId('permit-note');
    await expect(note).toHaveAttribute('data-count', '3');
    await expect(note).toHaveAttribute('data-sent', '0');
    await expect(note).toContainText(
      'Three one-off approvals first — the USDC allowance reset to zero, then USDC to Permit2, then Permit2 to the router.',
    );
    await expect(note).not.toContainText('signature');

    await page.getByTestId('swap-btn').click();
    await expect(page.getByTestId('swap-done')).toContainText('Swapped USDC → 0.0620009375 ETH');

    // four transactions, in the API's order, carrying the API's bytes — nothing re-encoded here
    const afterSwap = await sent(page);
    expect(afterSwap).toHaveLength(4);
    expect(afterSwap[0]).toMatchObject({ to: USDC, data: RESET_DATA });
    expect(afterSwap[1]).toMatchObject({ to: USDC, data: APPROVE_DATA });
    expect(afterSwap[2]).toMatchObject({ to: PERMIT2, data: PERMIT_DATA });
    expect(afterSwap[3]).toMatchObject({ to: UNIVERSAL_ROUTER, data: '0xu2swap' });
    expect((await walletState(page)).typedDataCalls).toHaveLength(0); // ⛔ nothing was asked to sign
    expect(api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST')).toHaveLength(0);

    // step 2 is the ordinary direct deposit of what actually arrived
    await expect(page.getByLabel('Amount (ETH)')).toHaveValue('0.0620009375');
    await expect(page.getByTestId('direct-note')).toContainText('straight into the Beam bridge');
    await expect(page.getByTestId('uniswap-step1')).toHaveCount(0);
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    const all = await sent(page);
    expect(all).toHaveLength(5);
    expect(all[4]).toMatchObject({ to: ETH_PIPE });
    const reg = api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST');
    expect(reg).toHaveLength(1);
    expect(reg[0].body).toMatchObject({ src_tx_hash: expect.stringMatching(/^0x/) });
    expect(pageErrors).toEqual([]);
  });

  test('two approvals: the token to Permit2, then Permit2 to the router', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await quoteUsdc(page);
    const note = page.getByTestId('permit-note');
    await expect(note).toHaveAttribute('data-count', '2');
    await expect(note).toContainText('Two one-off approvals first — USDC to Permit2, then Permit2 to the router.');

    await page.getByTestId('swap-btn').click();
    await expect(page.getByTestId('swap-done')).toBeVisible();
    const afterSwap = await sent(page);
    expect(afterSwap).toHaveLength(3);
    expect(afterSwap[0]).toMatchObject({ to: USDC, data: APPROVE_DATA });
    expect(afterSwap[1]).toMatchObject({ to: PERMIT2, data: PERMIT_DATA });
    expect(afterSwap[2]).toMatchObject({ to: UNIVERSAL_ROUTER, data: '0xu2swap' });
    expect((await walletState(page)).typedDataCalls).toHaveLength(0);
    expect(pageErrors).toEqual([]);
  });

  test('one approval: an allowance that already covers it is not asked for again', async ({ page }) => {
    await boot(page, { mock: (a) => (a.uniswapAllowanceCovers = true) });
    await connectAndSignIn(page);
    await quoteUsdc(page);
    const note = page.getByTestId('permit-note');
    await expect(note).toHaveAttribute('data-count', '1');
    await expect(note).toContainText('One one-off approval first — Permit2 to the router.');

    await page.getByTestId('swap-btn').click();
    await expect(page.getByTestId('swap-done')).toBeVisible();
    const afterSwap = await sent(page);
    expect(afterSwap).toHaveLength(2); // the router's allowance, then the swap
    expect(afterSwap[0]).toMatchObject({ to: PERMIT2, data: PERMIT_DATA });
    expect(afterSwap[1]).toMatchObject({ to: UNIVERSAL_ROUTER });
    expect((await walletState(page)).typedDataCalls).toHaveLength(0);
    expect(pageErrors).toEqual([]);
  });

  test('no approvals: with both allowances in place the swap is the only transaction', async ({ page }) => {
    await boot(page, {
      mock: (a) => {
        a.uniswapAllowanceCovers = true;
        a.uniswapPermitCovers = true;
      },
    });
    await connectAndSignIn(page);
    await quoteUsdc(page);
    await expect(page.getByTestId('permit-note')).toHaveCount(0); // nothing to promise
    await page.getByTestId('swap-btn').click();
    await expect(page.getByTestId('swap-done')).toBeVisible();
    const afterSwap = await sent(page);
    expect(afterSwap).toHaveLength(1);
    expect(afterSwap[0]).toMatchObject({ to: UNIVERSAL_ROUTER, data: '0xu2swap' });
    expect(pageErrors).toEqual([]);
  });

  test('a rejection stops the chain where it is, names the step, and resumes without re-asking', async ({ page }) => {
    await boot(page, { mock: (a) => (a.uniswapNeedsAllowanceReset = true) });
    await connectAndSignIn(page);
    await quoteUsdc(page);
    await rejectNthSend(page, 3); // the user presses Cancel on the router's allowance

    await page.getByTestId('swap-btn').click();
    // the banner names WHICH of the three prompts failed — "it didn't work" is not an answer
    const err = page.locator('.banner-error');
    await expect(err).toContainText('Stopped at approval 3 of 3 — Permit2 to the router: You rejected the request in the wallet');

    // ⛔ and the swap was NOT sent: a swap without the router's allowance reverts and costs gas
    const afterStop = await sent(page);
    expect(afterStop).toHaveLength(2);
    expect(afterStop[0]).toMatchObject({ to: USDC, data: RESET_DATA });
    expect(afterStop[1]).toMatchObject({ to: USDC, data: APPROVE_DATA });
    const note = page.getByTestId('permit-note');
    await expect(note).toHaveAttribute('data-sent', '2');
    await expect(note).not.toContainText('Approved.');

    // pressing Swap again picks up at the step that failed — the two that landed are not re-asked
    await page.getByTestId('swap-btn').click();
    await expect(page.getByTestId('swap-done')).toBeVisible();
    const afterResume = await sent(page);
    expect(afterResume).toHaveLength(4);
    expect(afterResume[2]).toMatchObject({ to: PERMIT2, data: PERMIT_DATA });
    expect(afterResume[3]).toMatchObject({ to: UNIVERSAL_ROUTER, data: '0xu2swap' });
    expect(pageErrors).toEqual([]);
  });

  test('a rejection of the FIRST approval sends nothing else at all', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await quoteUsdc(page);
    await rejectNthSend(page, 1);
    await page.getByTestId('swap-btn').click();
    await expect(page.locator('.banner-error')).toContainText('Stopped at approval 1 of 2 — USDC to Permit2');
    expect(await sent(page)).toHaveLength(0);
    await expect(page.getByTestId('permit-note')).toHaveAttribute('data-sent', '0');
    expect(pageErrors).toEqual([]);
  });

  test('native ETH needs no step 1 — it is a deposit, and the quote says so', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('direct-note')).toBeVisible();
    await expect(page.getByTestId('uniswap-step1')).toHaveCount(0);
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');
    // it still ASKED for the route: the API is the one that decides there is nothing to swap
    expect((api.calls.filter((c) => c.path === '/quote').pop()?.body as { route?: string }).route).toBe('uniswap');
    expect(pageErrors).toEqual([]);
  });

  // ---------------------------------------------------------------- the shape it replaced
  // The API and the SPA are deployed separately, so both orders have to work: a new SPA against an
  // API that still sends `approval` + `permit_tx`, and the same SPA against one that has been
  // renamed to `permit_fallback_tx`. One adapter reads all three, and these two prove it.

  test('the pre-shipped field names still work — approval + permit_tx, no approvals[]', async ({ page }) => {
    await boot(page, { mock: (a) => (a.uniswapLegacyApprovalFields = true) });
    await connectAndSignIn(page);
    await quoteUsdc(page);
    const note = page.getByTestId('permit-note');
    await expect(note).toHaveAttribute('data-count', '2');
    await expect(note).toContainText('Two one-off approvals first — USDC to Permit2, then Permit2 to the router.');

    await page.getByTestId('swap-btn').click();
    await expect(page.getByTestId('swap-done')).toBeVisible();
    const afterSwap = await sent(page);
    expect(afterSwap).toHaveLength(3);
    // that shape carried no calldata for the ERC-20 approve — three arguments, and the client
    // encodes it. Same selector, same spender, same amount as the API builds today.
    expect(afterSwap[0]).toMatchObject({ to: USDC });
    expect(afterSwap[0].data.toLowerCase()).toBe(APPROVE_DATA.toLowerCase());
    expect(afterSwap[1]).toMatchObject({ to: PERMIT2, data: PERMIT_DATA });
    expect(afterSwap[2]).toMatchObject({ to: UNIVERSAL_ROUTER, data: '0xu2swap' });
    expect((await walletState(page)).typedDataCalls).toHaveLength(0);
    expect(pageErrors).toEqual([]);
  });

  test("the API's pre-rename name for that transaction is still read", async ({ page }) => {
    await boot(page, {
      mock: (a) => {
        a.uniswapLegacyApprovalFields = true;
        a.uniswapAllowanceCovers = true;
        a.uniswapLegacyPermitField = true;
      },
    });
    await connectAndSignIn(page);
    await quoteUsdc(page);
    await page.getByTestId('swap-btn').click();
    await expect(page.getByTestId('swap-done')).toBeVisible();
    const afterSwap = await sent(page);
    expect(afterSwap).toHaveLength(2);
    expect(afterSwap[0]).toMatchObject({ to: PERMIT2, data: PERMIT_DATA });
    expect(pageErrors).toEqual([]);
  });

  test('the mechanism panel tells the two-step story, not the one-transaction one', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await quoteUsdc(page);
    await page.getByTestId('how-it-works').click();
    const modal = page.getByTestId('how-it-works-modal');
    await expect(modal.locator('ol')).toHaveAttribute('data-route', 'uniswap-two-step');
    await expect(modal.locator('li').nth(1)).toContainText('the ETH lands in your own wallet, never ours');
    // the count moved to a shape on 2026-09-10: the first use of a token also costs one or two
    // one-off approvals, so "two transactions" was a promise the approval card then broke
    await expect(modal.locator('li').nth(2)).toContainText(
      'Two steps in your own wallet, plus one-off approvals the first time you use a token',
    );
    await expect(modal).not.toContainText('in the same transaction');
    expect(pageErrors).toEqual([]);
  });
});
