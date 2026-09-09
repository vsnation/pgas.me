import { expect, test, type Page } from '@playwright/test';
import { MOCK_TX, MockApi, NATIVE, USDC, blockExternal, connectAndSignIn, installMockWallet, walletA, walletB } from './mocks';

test.describe('Pgas.me web', () => {
  let api: MockApi;
  let pageErrors: string[];

  async function boot(page: Page, path = '/') {
    api = new MockApi();
    pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await blockExternal(page);
    await api.install(page);
    await installMockWallet(page);
    await page.goto(path);
  }

  test('connect → sign in: the SIWE message carries the nonce and host, the session sticks', async ({ page }) => {
    await boot(page);
    await expect(page).toHaveTitle('Pgas.me');
    await page.getByRole('button', { name: 'Connect wallet' }).first().click();
    await expect(page.getByRole('dialog', { name: 'Connect a wallet' })).toBeVisible();
    await page.locator('[data-wallet-id="6963:me.pgas.mock"]').click();
    await expect(page.getByTestId('connected-address')).toContainText(walletA.address.slice(0, 6));
    await expect(page.locator('.chip-net')).toContainText('Ethereum');

    await page.getByRole('button', { name: 'Sign in with wallet' }).first().click();
    await expect(page.locator('.chip', { hasText: 'Signed in' })).toBeVisible();

    const nonceCall = api.calls.find((c) => c.path === '/siwe/nonce');
    const verify = api.calls.find((c) => c.path === '/siwe/verify');
    expect(nonceCall).toBeTruthy();
    expect(verify).toBeTruthy();
    const message = (verify!.body as { message: string }).message;
    expect(message.startsWith(`127.0.0.1:4173 wants you to sign in with your Ethereum account:\n${walletA.address}\n\n`)).toBe(true);
    expect(message).toContain('URI: http://127.0.0.1:4173\nVersion: 1\nChain ID: 1\nNonce: ');
    expect(message).toMatch(/Issued At: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
    const accountCall = api.calls.find((c) => c.path === '/account');
    expect(accountCall?.auth).toBe(`Bearer ${api.token}`);

    await page.reload();
    await expect(page.locator('.chip', { hasText: 'Signed in' })).toBeVisible();
    await expect(page.getByTestId('connected-address')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('portfolio degrades gracefully when RPCs fail and eth_call reverts', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    const empty = page.getByTestId('portfolio-empty');
    await expect(empty).toBeVisible({ timeout: 30_000 });
    await expect(empty).toContainText('No holdings found on 1 reachable chain');
    await expect(page.getByTestId('portfolio')).toContainText('1 of 4 chains read');
    await page.getByTestId('portfolio').locator('summary').click();
    await expect(page.getByTestId('portfolio')).toContainText('execution reverted');
    expect(pageErrors).toEqual([]);
  });

  test('deposit: unarmed quote is a preview with no send button; armed quote sends and registers', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    await expect(page.getByTestId('token-select')).toContainText('ETH');
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('unarmed-banner')).toContainText('Deposits open when the Beam wallet is armed — this is a preview');
    await expect(page.getByTestId('unarmed-banner')).toContainText('ingress not armed');
    await expect(page.getByTestId('quote-out')).toContainText('0.098');
    await expect(page.getByTestId('deposit-btn')).toHaveCount(0);
    const quote = api.calls.find((c) => c.path === '/quote');
    expect(quote?.body).toEqual({
      src_chain_id: 1,
      src_token: NATIVE,
      amount: '100000000000000000',
      target_asset: 'ETH',
      sender: walletA.address,
    });
    await expect(page.getByText('Minimum 0.02 ETH-equivalent')).toBeVisible();
    await expect(page.locator('[data-grade="weak"]').first()).toContainText('weak');

    // armed, from Arbitrum: the wallet is switched to the source chain, the order tx is sent and registered
    api.armed = true;
    await page.getByLabel('Source chain').selectOption('42161');
    await page.getByLabel('Amount (ETH)').fill('0.2');
    await expect(page.getByTestId('deposit-btn')).toBeVisible();
    await expect(page.getByTestId('deposit-btn')).toContainText('Deposit 0.2 ETH');
    await expect(page.getByTestId('approve-btn')).toHaveCount(0);
    await expect(page.getByText('The wallet switches to Arbitrum')).toBeVisible();
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    await expect(page.locator('.chip-net')).toContainText('Arbitrum');
    expect(await page.evaluate(() => (window as any).__mock.state.chainId)).toBe(42161);
    const reg = api.calls.find((c) => c.path === '/deposits' && c.method === 'POST');
    expect(reg?.body).toEqual({ quote_id: expect.stringMatching(/^q-/), src_tx_hash: MOCK_TX });
    let sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      from: walletA.address,
      to: '0xeF4fB24aD0916217251F553c0596F8Edc630EB66',
      data: '0xdeadbeef',
      value: '0x2c68af0bb140000',
    });
    await expect(page.getByTestId('deposit-timeline')).toContainText('Submitted');
    await expect(page.getByText('Transaction sent — follow the status below.')).toBeVisible();

    // an ERC-20 source needs approve(spender, amount) first, then the order tx
    await page.getByTestId('token-select').click();
    await page.getByLabel('Search tokens').fill('usdc');
    await page.getByRole('option', { name: /USDC/ }).click();
    await page.getByLabel('Amount (USDC)').fill('250');
    await expect(page.getByTestId('approve-btn')).toContainText('Approve USDC');
    await expect(page.getByTestId('deposit-btn')).toBeDisabled();
    const usdcQuote = api.calls.filter((c) => c.path === '/quote').pop();
    expect(usdcQuote?.body).toMatchObject({ src_chain_id: 42161, src_token: USDC, amount: '250000000' });
    await page.getByTestId('approve-btn').click();
    await expect(page.getByTestId('approve-btn')).toContainText('Approved');
    await expect(page.getByTestId('deposit-btn')).toBeEnabled();
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toContainText('Submitted');
    sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(3);
    expect(sent[1]).toMatchObject({ to: USDC });
    expect(sent[1].data).toMatch(/^0x095ea7b3000000000000000000000000ef4fb24ad0916217251f553c0596f8edc630eb66/i);
    expect(sent[1].value).toBeUndefined();
    expect(sent[2]).toMatchObject({ to: '0xeF4fB24aD0916217251F553c0596F8Edc630EB66', data: '0xdeadbeef' });
    expect(sent[2].value).toBeUndefined();

    // src_chain_id is the EVM id even where deBridge's internal id differs (Story: 1514 vs 100000013)
    await page.getByLabel('Source chain').selectOption('1514');
    await page.getByLabel(/^Amount/).fill('300'); // the token stays USDC (the mock lists it on every chain)
    await expect
      .poll(() => api.calls.filter((c) => c.path === '/quote').pop()?.body)
      .toMatchObject({ src_chain_id: 1514, src_token: USDC, amount: '300000000' });
    expect(pageErrors).toEqual([]);
  });

  test('wallets: generate and proven flows post the contract bodies; the 409 reason is shown', async ({ page }) => {
    await boot(page, '/wallets');
    await connectAndSignIn(page);
    const rows = page.getByTestId('destinations').locator('.list-row');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('connected');
    await expect(rows.first()).toContainText(walletA.address);

    // generate
    await page.getByTestId('generate-btn').click();
    const genAddress = (await page.getByTestId('gen-address').textContent())!.trim();
    expect(genAddress).toMatch(/^0x[0-9a-fA-F]{40}$/);
    expect((await page.getByTestId('gen-pk').textContent())!.trim()).toMatch(/^0x[0-9a-f]{64}$/);
    expect((await page.getByTestId('gen-mnemonic').textContent())!.trim().split(' ')).toHaveLength(12);
    await expect(page.getByTestId('register-btn')).toBeDisabled();
    await page.getByTestId('saved-check').check();
    await page.getByLabel('Label (optional)').nth(1).fill('fresh-1');
    await page.getByTestId('register-btn').click();
    await expect(rows).toHaveCount(2);
    const gen = api.calls.find((c) => c.path === '/destinations' && c.method === 'POST');
    expect(gen?.body).toMatchObject({ address: genAddress, kind: 'generated', label: 'fresh-1' });
    expect((gen!.body as any).nonce).toMatch(/^d/);
    expect((gen!.body as any).issued).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect((gen!.body as any).signature).toMatch(/^0x[0-9a-f]{130}$/);
    await expect(page.getByTestId('generated')).toHaveCount(0); // key gone from the page
    await expect(rows.nth(1)).toContainText('generated');

    // proven: switch the connected account to wallet B, sign with it
    await expect(page.getByTestId('prove-btn')).toBeDisabled();
    await page.evaluate((addr) => (window as any).__mock.setAccounts([addr]), walletB.address);
    await expect(page.getByTestId('connected-address')).toContainText(walletB.address.slice(0, 6));
    await expect(page.locator('.chip', { hasText: 'Signed in: ' })).toBeVisible();
    await page.getByTestId('prove-btn').click();
    await expect(rows).toHaveCount(3);
    const proven = api.calls.filter((c) => c.path === '/destinations' && c.method === 'POST').pop();
    expect(proven?.body).toMatchObject({ address: walletB.address, kind: 'proven' });
    await expect(rows.nth(2)).toContainText('proven');

    // remove: the signed-in wallet is refused with the API's reason
    page.once('dialog', (d) => d.accept());
    await rows.first().getByRole('button', { name: 'Remove' }).click();
    await expect(page.getByTestId('remove-error')).toContainText('the signed-in wallet cannot be removed');
    await expect(rows).toHaveCount(3);
    expect(pageErrors).toEqual([]);
  });

  test('withdraw: validates against Available, disables an unarmed mode, posts the contract body', async ({ page }) => {
    await boot(page, '/withdraw');
    await connectAndSignIn(page);
    await expect(page.getByText('Available 0.50 ETH')).toBeVisible();
    await expect(page.getByRole('radio', { name: /Instant/ })).toBeDisabled();
    await expect(page.getByText('not enabled yet')).toBeVisible();
    await expect(page.getByTestId('withdraw-submit')).toBeDisabled();

    await page.getByRole('checkbox', { name: `Fund ${walletA.address}` }).check();
    await page.getByLabel(`Amount for ${walletA.address}`).fill('0.6');
    await expect(page.getByTestId('withdraw-problems')).toContainText('Available 0.50 ETH is below 0.612');
    await expect(page.getByTestId('withdraw-submit')).toBeDisabled();

    await page.getByLabel(`Amount for ${walletA.address}`).fill('0.1');
    await expect(page.getByTestId('fee-line')).toHaveText('2% at unlock: funding 0.10 costs 0.102');
    await expect(page.getByTestId('debit-total')).toContainText('0.102 ETH');
    await page.getByLabel('Release window').selectOption('3600');
    await expect(page.getByTestId('withdraw-submit')).toBeEnabled();
    await page.getByTestId('withdraw-submit').click();
    await expect(page.getByTestId('withdraw-result')).toContainText('Scheduled 1 payout request');
    await expect(page.getByTestId('withdraw-result')).toContainText('ETA 1 h – 18 h');
    await expect(page.getByTestId('withdraw-result').locator('[data-grade="weak"]')).toBeVisible();
    const call = api.calls.find((c) => c.path === '/withdrawals');
    expect(call?.body).toEqual({ asset: 'ETH', items: [{ W: walletA.address, amount_groth: 10000000 }], mode: 'direct', window_s: 3600 });

    api.directEnabled = false;
    await page.getByRole('checkbox', { name: `Fund ${walletA.address}` }).check();
    await page.getByLabel(`Amount for ${walletA.address}`).fill('0.01');
    await expect(page.getByTestId('withdraw-problems')).toContainText('direct mode is not enabled yet', { timeout: 15_000 });
    expect(pageErrors).toEqual([]);
  });

  test('balance and activity render the account: tiles, history, status pills, Etherscan links', async ({ page }) => {
    await boot(page, '/balance');
    await connectAndSignIn(page);
    await expect(page.getByTestId('available-ETH')).toContainText('0.50');
    await expect(page.getByTestId('balance-ETH')).toContainText('Pending bridge');
    await expect(page.getByTestId('balance-DAI')).toHaveCount(0);
    await expect(page.getByText('Deposit credited')).toBeVisible();

    await page.getByRole('button', { name: 'Activity' }).click();
    const deposits = page.getByTestId('deposits-table');
    await expect(deposits.locator('[data-status="credited"]')).toHaveText('Credited');
    await expect(deposits.locator('[data-status="confirming"]')).toHaveText('Confirming');
    await expect(deposits).toContainText('4821');
    await expect(deposits.locator('a[href^="https://etherscan.io/tx/0x3333"]')).toHaveCount(1);
    await expect(deposits.locator('a[href^="https://arbiscan.io/tx/0x4444"]')).toHaveCount(1);
    const requests = page.getByTestId('requests-table');
    await expect(requests.locator('[data-status="scheduled"]')).toHaveText('Scheduled');
    await expect(requests.locator('[data-status="sent"]')).toHaveText('Sent');
    await expect(requests).toContainText('4830');
    await expect(requests.getByRole('button', { name: 'Cancel' })).toHaveCount(1);
    await expect(page.getByTestId('stats-strip')).toContainText('Ingress not armed');

    // a 401 on a locked route ends the session and asks for a fresh sign-in
    api.token = null;
    await page.getByRole('button', { name: 'Balance' }).click();
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByTestId('expired-banner')).toContainText('Sign in again');
    await expect(page.getByTestId('sign-in-gate')).toContainText('Sign in with wallet');
    await expect(page.locator('.chip', { hasText: 'Signed in' })).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.getItem('pgas.session.v1'))).toBeNull();
    expect(pageErrors).toEqual([]);
  });
});
