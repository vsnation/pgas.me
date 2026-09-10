import { expect, test, type Page } from '@playwright/test';
import { getAddress } from 'ethers';
import {
  DAI,
  DEMO_HOLDINGS,
  LOGO_HOST,
  MOCK_TX,
  MockApi,
  MockRpc,
  NATIVE,
  PGAS_ROUTER,
  USDC,
  blockExternal,
  connectAndSignIn,
  goTab,
  installMockPrices,
  installMockWallet,
  payWith,
  signedIn,
  walletA,
  walletB,
} from './mocks';

const ETH_PIPE = '0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96';
const UNISWAP_NOTE = 'via Uniswap V4 — one transaction, the hook locks your ETH in the Beam bridge';
const DAI_PIPE = '0xAcDc8f4559741a3c8CAAB0ba74c57807A9Fe2d73';
const ROUTER_ORDER = '0xeF4fB24aD0916217251F553c0596F8Edc630EB66';
const ROUTER_ALLOWANCE_TARGET = '0x6A000F20005980200259B80c5102003040001068';

/** Three more payout addresses. All-digit bodies carry no case, so they are their own checksum. */
const ADDR3 = getAddress('0x' + '33'.repeat(20));
const ADDR4 = getAddress('0x' + '44'.repeat(20));
const ADDR5 = getAddress('0x' + '55'.repeat(20));

/** Flip the case of the first letter: still 40 hex chars, still mixed case, checksum now wrong. */
function breakChecksum(address: string): string {
  const body = address.slice(2);
  for (let i = 0; i < body.length; i++) {
    const flipped = body[i] === body[i].toUpperCase() ? body[i].toLowerCase() : body[i].toUpperCase();
    if (flipped !== body[i]) return '0x' + body.slice(0, i) + flipped + body.slice(i + 1);
  }
  throw new Error('no cased character in ' + address);
}

test.describe('Pgas.me web', () => {
  let api: MockApi;
  let pageErrors: string[];

  async function boot(
    page: Page,
    path = '/',
    opts: { rpc?: boolean; assets?: boolean; fixedTime?: number; mock?: (a: MockApi) => void } = {},
  ) {
    api = new MockApi();
    // The ingress flags are read once, at load, from `/assets` and `/health` — so a test that
    // exercises a path states it here, before the page ever asks.
    opts.mock?.(api);
    pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    // `assets` serves the token-logo CDN locally: without it every remote <img> aborts and falls
    // back to a monogram, so a token-logo assertion needs it to mean anything. Chain icons are this
    // app's own files and load either way.
    await blockExternal(page, opts.assets ?? false);
    await api.install(page);
    if (opts.rpc) {
      await new MockRpc(DEMO_HOLDINGS).install(page);
      await installMockPrices(page);
    }
    await installMockWallet(page);
    // `Date.now()` and `new Date()` freeze; timers keep running, so the app is not stalled — the
    // Schedule page's presets are pure functions of "now", and this is the only way to assert them.
    if (opts.fixedTime !== undefined) await page.clock.setFixedTime(opts.fixedTime);
    await page.goto(path);
  }

  test('connect signs in by itself: one Connect button, no second click, and the session sticks', async ({ page }) => {
    await boot(page);
    await expect(page).toHaveTitle(/^Pgas\.me/); // index.html carries an SEO suffix
    // exactly one Connect button on the screen: the header's (T14 — the cards lost theirs)
    await expect(page.getByRole('button', { name: 'Connect wallet' })).toHaveCount(1);
    await page.getByRole('button', { name: 'Connect wallet' }).click();
    await expect(page.getByRole('dialog', { name: 'Connect a wallet' })).toBeVisible();
    await page.locator('[data-wallet-id="6963:me.pgas.mock"]').click();

    // no "Sign in with wallet" click anywhere below: the signature is asked for on connect
    await expect(signedIn(page)).toBeVisible();
    await expect(page.getByTestId('connected-address')).toContainText(walletA.address.slice(0, 6));
    await expect(page.getByTestId('connected-address')).toHaveAttribute('title', /^Ethereum · 0x/);
    await expect(page.getByRole('button', { name: 'Sign in with wallet' })).toHaveCount(0);

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
    await expect(signedIn(page)).toBeVisible();
    // and it did not ask for a second signature on the way back in
    expect(api.calls.filter((c) => c.path === '/siwe/verify')).toHaveLength(1);
    expect(pageErrors).toEqual([]);
  });

  test('wallet picker: only wallets that can actually be picked, and nothing else on the row', async ({ page }) => {
    await boot(page);
    await page.getByRole('button', { name: 'Connect wallet' }).click();
    const dialog = page.getByRole('dialog');
    const rows = dialog.locator('.wallet-row');
    // three, not four: WalletConnect has no project id in this build, so its row would be a button
    // that can only refuse — it is not rendered at all (T14)
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toHaveAttribute('data-wallet-id', '6963:me.pgas.mock');
    await expect(rows.nth(0)).toContainText('Mock Wallet');
    await expect(rows.nth(0).locator('.pill')).toHaveText('Detected');
    await expect(rows.nth(1)).toHaveAttribute('data-wallet-id', '6963:me.pgas.mock2');
    await expect(rows.nth(2)).toHaveAttribute('data-wallet-id', 'inj:coin98');
    await expect(dialog).not.toContainText('WalletConnect');
    await expect(dialog).not.toContainText('not configured');
    await expect(dialog).not.toContainText('browser extension or in-app browser');
    await expect(dialog).toContainText('Signing in is free and moves nothing.');

    await rows.nth(1).click();
    await expect(page.getByTestId('connected-address')).toContainText(walletB.address.slice(0, 6));
    expect(await page.evaluate(() => localStorage.getItem('pgas.wallet.v1'))).toBe('6963:me.pgas.mock2');
    await expect(signedIn(page)).toBeVisible();

    // Disconnect lives in the pill's menu, which is the only place it is offered
    await expect(page.getByRole('button', { name: 'Disconnect' })).toHaveCount(0);
    await page.getByTestId('connected-address').click();
    await page.getByTestId('account-menu').getByRole('button', { name: 'Disconnect' }).click();
    await expect(page.getByTestId('connected-address')).toHaveCount(0);

    // reconnecting asks for the signature again — disconnect re-arms it
    await page.getByRole('button', { name: 'Connect wallet' }).click();
    await page.locator('[data-wallet-id="inj:coin98"]').click();
    await expect(signedIn(page)).toBeVisible();
    await expect(page.getByTestId('connected-address')).toContainText(walletA.address.slice(0, 6));
    expect(pageErrors).toEqual([]);
  });

  test('portfolio degrades gracefully when RPCs fail and eth_call reverts', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    const portfolio = page.getByTestId('portfolio');
    const empty = page.getByTestId('portfolio-empty');
    await expect(empty).toBeVisible({ timeout: 30_000 });
    await expect(empty).toContainText('No holdings found on 1 reachable chain');

    // the scan report is no longer a line of text under the chips — it is the Refresh tooltip
    await expect(portfolio).not.toContainText('EVM chains');
    await expect(portfolio).not.toContainText('not scanned (non-EVM)');
    await expect(portfolio).not.toContainText('Read client-side');
    await expect(portfolio.locator('summary')).toHaveCount(0);
    const refresh = portfolio.getByRole('button', { name: 'Refresh' });
    await expect(refresh).toHaveAttribute('title', /read 1 of 5 EVM chains/);
    await expect(refresh).toHaveAttribute('title', /2 not scanned \(non-EVM\)/);
    await expect(refresh).toHaveAttribute('title', /execution reverted/);
    expect(pageErrors).toEqual([]);
  });

  test('deposit: unarmed quote is a preview with no send button; armed quote sends and registers', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    // one picker for the pair: the token, with the chain it is on, and the chain badge on its icon
    await expect(page.getByTestId('pay-with')).toContainText('ETH');
    await expect(page.getByTestId('pay-with')).toContainText('on Ethereum');
    await expect(page.getByLabel('Source chain')).toHaveCount(0);
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('unarmed-banner')).toContainText('Deposits are paused right now — this is a preview');
    // the API's note names a flag and a Beam pubkey; neither reaches the screen (T14)
    await expect(page.locator('body')).not.toContainText('ingress not armed');
    await expect(page.getByTestId('quote-out')).toContainText('0.1'); // chain 1 + ETH is a direct deposit: nothing is skimmed
    await expect(page.getByTestId('direct-note')).toContainText('straight into the Beam bridge, no routing fee');
    await expect(page.getByTestId('deposit-btn')).toHaveCount(0);
    const quote = api.calls.find((c) => c.path === '/quote');
    expect(quote?.body).toEqual({
      src_chain_id: 1,
      src_token: NATIVE,
      amount: '100000000000000000',
      target_asset: 'ETH',
      sender: walletA.address,
    });
    // everything the operator asked to be taken off the page (T1d): no client-side minimum, no
    // countdown, no quote/order ids, no portfolio blurb, no stats strip — one footer line survives
    await expect(page.locator('body')).not.toContainText('Minimum 0.02');
    await expect(page.locator('body')).not.toContainText('ETH-equivalent');
    await expect(page.getByTestId('quote-card')).not.toContainText('refreshes in');
    await expect(page.getByTestId('quote-card')).not.toContainText('Quote id');
    await expect(page.getByTestId('quote-card')).not.toContainText('Order');
    await expect(page.getByTestId('quote-card')).not.toContainText('Credited as bETH');
    // the two bookkeeping rows are one plain line now, and the amount carries its own USD
    await expect(page.getByTestId('quote-card')).not.toContainText('Credited on Beam');
    await expect(page.getByTestId('quote-card')).not.toContainText('Paying');
    await expect(page.getByTestId('lands-in')).toContainText('Lands in your balance in ≈');
    await expect(page.getByTestId('amount-usd')).toContainText('$400');
    await expect(page.getByTestId('portfolio')).not.toContainText('Read client-side');
    await expect(page.getByTestId('stats-strip')).toHaveCount(0);
    await expect(page.locator('.footer')).not.toContainText('Shielded outputs');
    await expect(page.locator('.footer')).not.toContainText('Ingress');
    await expect(page.locator('.footer')).toContainText('Settled on Beam — a confidential ledger: no addresses on-chain, blinded amounts.');
    await expect(page.locator('body')).not.toContainText('Anonymity');

    // armed, from Arbitrum: the wallet is switched to the source chain, the order tx is sent and registered
    api.armed = true;
    await payWith(page, { chainId: 42161 });
    await page.getByLabel('Amount (ETH)').fill('0.2');
    await expect(page.getByTestId('deposit-btn')).toBeVisible();
    // the chain is IN the button now; the sentence that used to explain the switch is gone
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.2 ETH on Arbitrum');
    await expect(page.getByTestId('approve-btn')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('The wallet switches to');
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    await expect(page.getByTestId('connected-address')).toHaveAttribute('title', /^Arbitrum · 0x/); // the pill's chain dot
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

    // an ERC-20 source still needs approve(spender, amount) before the order tx — but a cross-chain
    // quote does not know the spender until it is armed, so both go out on one Deposit click (T2b)
    await payWith(page, { token: 'usdc' });
    await page.getByLabel('Amount (USDC)').fill('250');
    await expect(page.getByTestId('deposit-btn')).toBeEnabled();
    await expect(page.getByTestId('approve-btn')).toHaveCount(0);
    const usdcQuote = api.calls.filter((c) => c.path === '/quote').pop();
    expect(usdcQuote?.body).toMatchObject({ src_chain_id: 42161, src_token: USDC, amount: '250000000' });
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toContainText('Submitted');
    sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(3);
    expect(sent[1]).toMatchObject({ to: USDC });
    expect(sent[1].data).toMatch(/^0x095ea7b3000000000000000000000000ef4fb24ad0916217251f553c0596f8edc630eb66/i);
    expect(sent[1].value).toBeUndefined();
    expect(sent[2]).toMatchObject({ to: '0xeF4fB24aD0916217251F553c0596F8Edc630EB66', data: '0xdeadbeef' });
    expect(sent[2].value).toBeUndefined();

    // src_chain_id is the EVM id even where the router's own id differs (Story: 1514 vs 100000013)
    await payWith(page, { chainId: 1514 });
    await page.getByLabel(/^Amount/).fill('300'); // the token stays USDC (the mock lists it on every chain)
    await expect
      .poll(() => api.calls.filter((c) => c.path === '/quote').pop()?.body)
      .toMatchObject({ src_chain_id: 1514, src_token: USDC, amount: '300000000' });
    expect(pageErrors).toEqual([]);
  });

  test('portfolio chips: every EVM chain answers, Multicall3 covers the ones with no batch helper, non-EVM is skipped', async ({
    page,
  }) => {
    await boot(page, '/', { rpc: true });
    await connectAndSignIn(page);
    const chips = page.getByTestId('portfolio-chips').locator('.portfolio-chip');
    await expect(chips.first()).toBeVisible({ timeout: 30_000 });
    await expect(chips).toHaveCount(14); // 4 on Ethereum, 2 Arbitrum, 3 Base, 2 Story, 3 Cronos
    const portfolio = page.getByTestId('portfolio');
    const refresh = portfolio.getByRole('button', { name: 'Refresh' });
    await expect(refresh).toHaveAttribute('title', /read 5 of 5 EVM chains/); // the tooltip, not a text line
    await expect(refresh).toHaveAttribute('title', /2 not scanned \(non-EVM\)/);
    await expect(portfolio).not.toContainText('EVM chains');
    await expect(portfolio).not.toContainText('unreachable');

    // Story and Cronos have no batch-balance contract: their ERC-20s can only come from Multicall3
    await expect(chips.filter({ hasText: 'IP' }).first()).toBeVisible();
    await expect(chips.filter({ hasText: 'CRO' }).first()).toBeVisible();
    const cronosUsdc = chips.filter({ has: page.locator('.portfolio-chip-sym', { hasText: /^USDC$/ }) });
    await expect(cronosUsdc).toHaveCount(5); // one per chain, all five hold USDC

    // sorted by USD, and buybeam's label rule: "$" + value once it is worth a cent
    await expect(chips.first().locator('.portfolio-chip-sym')).toHaveText('IP');
    await expect(chips.first().locator('.portfolio-chip-usd')).toHaveText('$4,800');
    await expect(chips.first().locator('.portfolio-chip-token')).toBeVisible();

    // tapping a chip selects its chain + token and prefills the whole balance
    await chips.filter({ hasText: 'WBTC' }).first().click();
    await expect(page.getByTestId('pay-with')).toContainText('WBTC');
    await expect(page.getByTestId('pay-with')).toContainText('on Ethereum');
    await expect(page.getByLabel('Amount (WBTC)')).toHaveValue('0.034');
    await expect(chips.filter({ hasText: 'WBTC' }).first()).toHaveClass(/selected/);
    expect(pageErrors).toEqual([]);
  });

  test("native chips carry a real logo: the token list's native entry, or the chain icon when it has none", async ({ page }) => {
    await boot(page, '/', { rpc: true, assets: true });
    await connectAndSignIn(page);
    const chips = page.getByTestId('portfolio-chips').locator('.portfolio-chip');
    await expect(chips).toHaveCount(14, { timeout: 30_000 });

    // Ethereum's list HAS a native entry: the chip shows its logo, not the "ETH" monogram
    const ethChip = chips.filter({ has: page.locator('.portfolio-chip-sym', { hasText: /^ETH$/ }) }).first();
    await expect(ethChip.locator('img.portfolio-chip-token')).toHaveAttribute(
      'src',
      new RegExp(`^${LOGO_HOST}/Logo/\\d+/0x0{40}/small/token-logo\\.svg$`),
    );
    await expect(ethChip.locator('.logo-fallback')).toHaveCount(0);
    await expect(ethChip.locator('img.portfolio-chip-chain')).toBeVisible(); // the chain badge stays

    // Story's native entry is IP; Cronos has NO native entry, so its chip falls back to the chain icon
    const ipChip = chips.filter({ has: page.locator('.portfolio-chip-sym', { hasText: /^IP$/ }) }).first();
    await expect(ipChip.locator('img.portfolio-chip-token')).toHaveAttribute('src', new RegExp(`^${LOGO_HOST}/Logo/1514/`));
    const croChip = chips.filter({ has: page.locator('.portfolio-chip-sym', { hasText: /^CRO$/ }) }).first();
    await expect(croChip.locator('img.portfolio-chip-token')).toHaveAttribute('src', '/chains/25.svg');
    await expect(croChip.locator('.logo-fallback')).toHaveCount(0);

    // and the "Pay with" button shows the same logo — this is the row the operator saw as a
    // 3-letter monogram after tapping a native chip — now with the chain's badge on its corner
    await ethChip.click();
    const select = page.getByTestId('pay-with');
    await expect(select).toContainText('ETH');
    await expect(select.locator('.pw-icons img').first()).toHaveAttribute('src', new RegExp(`^${LOGO_HOST}/Logo/1/0x0{40}/`));
    await expect(select.locator('img.pw-chain-badge')).toBeVisible();
    await expect(select.locator('.logo-fallback')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test("the deposit minimum is the API's: nothing is stated until a 400 says it, and then verbatim", async ({ page }) => {
    await boot(page);
    api.minDepositWei = 2_000_000_000_000_000n; // settings.min_deposit_wei — 0.002 ETH on prod today
    await connectAndSignIn(page);

    await page.getByLabel('Amount (ETH)').fill('0.001');
    const banner = page.getByTestId('min-deposit');
    await expect(banner).toHaveText('below the minimum deposit of 0.002 ETH'); // the API's own sentence
    await expect(page.getByTestId('quote-out')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('0.02'); // no client-side copy of the floor

    // above the floor no minimum is stated anywhere
    await page.getByLabel('Amount (ETH)').fill('0.05');
    await expect(page.getByTestId('quote-out')).toContainText('0.05');
    await expect(banner).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('minimum');
    expect(pageErrors).toEqual([]);
  });

  test('quote mode "direct": ETH on Ethereum goes straight to the pipe, DAI approves the pipe', async ({ page }) => {
    await boot(page);
    api.armed = true;
    await connectAndSignIn(page);
    await page.getByLabel('Amount (ETH)').fill('0.3');
    await expect(page.getByTestId('direct-note')).toContainText('Direct deposit — your ETH goes straight into the Beam bridge');
    await expect(page.getByTestId('unarmed-banner')).toHaveCount(0);
    await expect(page.getByTestId('swap-panel')).toHaveCount(0);
    await expect(page.getByTestId('approve-btn')).toHaveCount(0); // native ETH needs no approval
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.3 ETH on Ethereum');
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    let sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ from: walletA.address, to: ETH_PIPE, data: '0xdeadbeef', value: '0x429d069189e0000' });
    const reg = api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST');
    expect(reg).toHaveLength(1);
    expect(reg[0].body).toEqual({ quote_id: expect.stringMatching(/^q-/), src_tx_hash: MOCK_TX });

    // DAI → bDAI on Ethereum is direct too, and the approval spender is the DAI pipe, not the router
    await page.getByRole('radio', { name: 'DAI' }).click();
    await payWith(page, { token: 'dai' });
    await page.getByLabel('Amount (DAI)').fill('100');
    await expect(page.getByTestId('direct-note')).toContainText('your DAI goes straight into the Beam bridge');
    await expect(page.getByTestId('approve-btn')).toContainText('Approve DAI');
    await page.getByTestId('approve-btn').click();
    await expect(page.getByTestId('approve-btn')).toContainText('Approved');
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(3);
    expect(sent[1]).toMatchObject({ to: DAI });
    expect(sent[1].data.toLowerCase()).toContain(DAI_PIPE.slice(2).toLowerCase()); // approve(pipe, amount)
    expect(sent[2]).toMatchObject({ to: DAI_PIPE, data: '0xdeadbeef' });
    expect(sent[2].value).toBeUndefined();
    expect(pageErrors).toEqual([]);
  });

  test('quote mode "swap": USDC on Ethereum swaps in the wallet, then re-quotes as a direct deposit', async ({ page }) => {
    await boot(page);
    api.armed = true;
    await connectAndSignIn(page);
    await payWith(page, { token: 'usdc' });
    await page.getByLabel('Amount (USDC)').fill('250');

    const panel = page.getByTestId('swap-panel');
    await expect(panel).toContainText('Step 1 — Swap USDC → ETH in your wallet');
    await expect(page.getByTestId('deposit-btn')).toHaveCount(0); // nothing to deposit yet
    await expect(page.getByTestId('approve-btn')).toContainText('Approve USDC');
    await page.getByTestId('approve-btn').click();
    await expect(page.getByTestId('approve-btn')).toContainText('Approved');
    await expect(page.getByTestId('swap-btn')).toContainText('Swap 250 USDC');
    await page.getByTestId('swap-btn').click();

    // the swap is never registered as a deposit; the client re-quotes what arrived instead
    await expect(page.getByTestId('swap-done')).toContainText('Swapped USDC → 0.06125 ETH');
    expect(api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST')).toHaveLength(0);
    await expect(page.getByLabel('Amount (ETH)')).toHaveValue('0.06125');
    await expect(page.getByTestId('direct-note')).toContainText('straight into the Beam bridge');
    await expect(page.getByTestId('swap-panel')).toHaveCount(0);

    const sentAfterSwap = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sentAfterSwap).toHaveLength(2);
    expect(sentAfterSwap[0]).toMatchObject({ to: USDC });
    expect(sentAfterSwap[0].data.toLowerCase()).toContain(ROUTER_ALLOWANCE_TARGET.slice(2).toLowerCase());
    expect(sentAfterSwap[1]).toMatchObject({ to: ROUTER_ORDER, data: '0xfeedface' });

    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    const sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(3);
    expect(sent[2]).toMatchObject({ to: ETH_PIPE, value: '0xd99a8cec7e2000' }); // 0.06125 ETH into the pipe
    const reg = api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST');
    expect(reg).toHaveLength(1);
    const quotes = api.calls.filter((c) => c.path === '/quote');
    expect(quotes.at(-1)?.body).toMatchObject({ src_chain_id: 1, src_token: NATIVE, amount: '61250000000000000' });
    expect(pageErrors).toEqual([]);
  });

  test('schedule: pasted addresses are checksummed, the totals are live, and the body is the contract body', async ({ page }) => {
    await boot(page, '/schedule');
    await connectAndSignIn(page);
    await expect(page.getByTestId('total-available')).toContainText('0.50 ETH');
    // the minimum, the fee and the bridge ETA are read, never assumed
    await expect.poll(() => api.calls.some((c) => c.path === '/withdrawals/fees?asset=ETH')).toBe(true);
    const rows = page.getByTestId('schedule-row');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('min 0.01 ETH');
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();

    // lowercase in, EIP-55 out
    await page.getByLabel('Address 1').fill(walletB.address.toLowerCase());
    await expect(page.getByTestId('address-checksummed-0')).toHaveText(walletB.address);
    await page.getByLabel('Amount 1').fill('0.1');
    await expect(page.getByTestId('total-amount')).toContainText('0.10 ETH');
    await expect(page.getByTestId('total-fee')).toContainText('0.002 ETH');
    await expect(page.getByTestId('total-debited')).toContainText('0.102 ETH');
    await expect(page.getByTestId('total-remaining')).toContainText('0.398 ETH');
    await expect(page.getByTestId('fee-line')).toHaveText('Fee 2 % · bridge fee paid by Pgas.me · you receive exactly what you enter');
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();

    // a mixed-case string whose checksum does not match is a typo, and says so
    await page.getByTestId('schedule-add').click();
    await expect(rows).toHaveCount(2);
    await page.getByLabel('Address 2').fill(breakChecksum(walletA.address));
    await expect(page.getByTestId('address-error-1')).toHaveText('bad checksum');
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();
    await page.getByLabel('Address 2').fill('0xnot-an-address');
    await expect(page.getByTestId('address-error-1')).toHaveText('not an EVM address');

    // below the API's minimum, then over Available: both hold the button down
    await page.getByLabel('Address 2').fill(walletA.address);
    await page.getByLabel('Amount 2').fill('0.001');
    await expect(page.getByTestId('amount-error-1')).toHaveText('minimum 0.01 ETH');
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();
    await page.getByLabel('Amount 2').fill('0.6');
    await expect(page.getByTestId('schedule-problems')).toContainText('short by 0.214 ETH');
    await expect(page.getByTestId('total-remaining')).toContainText('-0.214 ETH');
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();

    // removing the offending row is all it takes
    await page.getByLabel('Remove row 2').click();
    await expect(rows).toHaveCount(1);
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();

    await page.getByTestId('schedule-add').click();
    await page.getByLabel('Address 2').fill(walletA.address);
    await page.getByLabel('Amount 2').fill('0.05');
    await page.getByLabel('Deliver 2').selectOption('2h');
    await page.getByTestId('schedule-submit').click();
    await expect(page.getByTestId('schedule-result')).toContainText('Scheduled 2 orders');
    await expect(page.getByTestId('schedule-result')).toContainText('Debited 0.153 ETH (fee 0.003)');

    const call = api.calls.find((c) => c.path === '/withdrawals' && c.method === 'POST');
    const body = call!.body as { asset: string; mode: string; items: { W: string; amount_groth: number; deliver_at: number }[] };
    expect(Object.keys(body).sort()).toEqual(['asset', 'items', 'mode']); // no window_s, no privacy knobs
    expect(body.asset).toBe('ETH');
    expect(body.mode).toBe('direct');
    expect(body.items.map((i) => [i.W, i.amount_groth])).toEqual([
      [walletB.address, 10000000],
      [walletA.address, 5000000],
    ]);
    for (const it of body.items) expect(Number.isInteger(it.deliver_at)).toBe(true);

    // and the two new orders appear below with their status pill
    const orders = page.getByTestId('schedule-orders');
    await expect(orders.locator('tr.row-new')).toHaveCount(2, { timeout: 15_000 });
    await expect(orders.locator('tr.row-new').first().locator('[data-status="scheduled"]')).toHaveText('Scheduled');
    // the form is empty again and the balance moved
    await expect(rows).toHaveCount(1);
    await expect(page.getByLabel('Address 1')).toHaveValue('');
    await expect(page.getByTestId('total-available')).toContainText('0.347 ETH');
    expect(pageErrors).toEqual([]);
  });

  test('schedule: an untouched form says nothing in red, and a blank row is not an order', async ({ page }) => {
    await boot(page, '/schedule');
    await connectAndSignIn(page);
    await expect(page.getByTestId('schedule-form')).toBeVisible();
    // the defect (screen review 2026-09-10): "row 1: enter an address · row 1: enter an amount",
    // in red, before the user had typed anything at all
    await expect(page.getByTestId('schedule-problems')).toHaveCount(0);
    await expect(page.locator('.error-text')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText('enter an address');
    await expect(page.locator('body')).not.toContainText('enter an amount');
    // one muted line instead, and the button stays down
    await expect(page.getByTestId('schedule-hint')).toHaveText('Add a wallet and an amount.');
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();
    await expect(page.getByTestId('schedule-submit')).toHaveText('Schedule orders');
    await expect(page.getByTestId('total-orders')).toHaveText('0');

    // a row the user HAS been in says what it is still missing
    await page.getByLabel('Address 1').fill(walletB.address);
    await expect(page.getByTestId('schedule-problems')).toContainText('enter an amount');
    await expect(page.getByTestId('schedule-hint')).toHaveCount(0);
    await page.getByLabel('Amount 1').fill('0.1');
    await expect(page.getByTestId('schedule-problems')).toHaveCount(0);
    await expect(page.getByTestId('total-orders')).toHaveText('1');
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();

    // and the blank row "+ Add another address" makes neither complains nor holds the button down
    await page.getByTestId('schedule-add').click();
    await expect(page.getByTestId('schedule-row')).toHaveCount(2);
    await expect(page.getByTestId('schedule-problems')).toHaveCount(0);
    await expect(page.getByTestId('total-orders')).toHaveText('1');
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    // leaving its fields empty is not a complaint either — but the moment it has content, it is
    await page.getByLabel('Address 2').click();
    await page.getByLabel('Amount 2').click();
    await page.getByLabel('Address 1').click();
    await expect(page.getByTestId('schedule-problems')).toHaveCount(0);
    await page.getByLabel('Amount 2').fill('0.05');
    await expect(page.getByTestId('schedule-problems')).toContainText('row 2: enter an address');
    await expect(page.getByTestId('schedule-submit')).toBeDisabled();

    // the blank row does not travel: the body carries the one order that was filled in
    await page.getByLabel('Amount 2').fill('');
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    await page.getByTestId('schedule-submit').click();
    await expect(page.getByTestId('schedule-result')).toContainText('Scheduled 1 order');
    const body = api.calls.find((c) => c.path === '/withdrawals' && c.method === 'POST')!.body as { items: { W: string }[] };
    expect(body.items).toHaveLength(1);
    expect(body.items[0].W).toBe(walletB.address);
    expect(pageErrors).toEqual([]);
  });

  test("schedule: the 409 shortfall is the API's own sentence, shown verbatim", async ({ page }) => {
    await boot(page, '/schedule');
    await connectAndSignIn(page);
    await page.getByLabel('Address 1').fill(walletB.address);
    await page.getByLabel('Amount 1').fill('0.4');
    await expect(page.getByTestId('schedule-submit')).toBeEnabled();
    // the balance moves between render and click — the client's own arithmetic says yes, the API says no
    api.balances.ETH.available = 10000000;
    await page.getByTestId('schedule-submit').click();
    await expect(page.getByTestId('schedule-error')).toHaveText(
      'short by 0.308 ETH — this batch debits 0.408 ETH (amounts + 2%) and Available is 0.1 ETH',
    );
    expect(pageErrors).toEqual([]);
  });

  test('schedule: an order can be cancelled while scheduled, and the money comes back', async ({ page }) => {
    await boot(page, '/schedule');
    await connectAndSignIn(page);
    const orders = page.getByTestId('schedule-orders');
    await expect(orders.locator('[data-status="scheduled"]')).toHaveCount(1);
    await expect(orders.getByRole('button', { name: 'Cancel' })).toHaveCount(1);
    page.once('dialog', (d) => d.accept());
    await page.getByTestId('cancel-req-1').click();
    await expect(orders.locator('[data-status="cancelled"]')).toHaveCount(1);
    await expect(orders.getByRole('button', { name: 'Cancel' })).toHaveCount(0);
    const call = api.calls.find((c) => c.path === '/withdrawals/req-1/cancel');
    expect(call?.method).toBe('POST');
    // 0.1 + its 0.002 fee return to Available
    await expect(page.getByTestId('total-available')).toContainText('0.602 ETH');
    expect(pageErrors).toEqual([]);
  });

  test('balance: tiles named for the money, USD under each, and one timeline of deposits and payouts', async ({ page }) => {
    await boot(page, '/balance', { rpc: true }); // rpc:true also installs the price double
    await connectAndSignIn(page);
    const tiles = page.getByTestId('balance-ETH');
    await expect(page.getByTestId('available-ETH')).toContainText('0.50');
    await expect(tiles).toContainText('Arriving');
    await expect(tiles).toContainText('Available');
    await expect(tiles).toContainText('Scheduled');
    await expect(tiles).toContainText('Paid out');
    await expect(tiles).toContainText('deposits still bridging');
    await expect(tiles).toContainText('incl. 2 % fee');
    // the bucket names and the formula subtitles are gone (T14)
    await expect(tiles).not.toContainText('Pending bridge');
    await expect(tiles).not.toContainText('credits − scheduled − sent');
    await expect(tiles).not.toContainText('payout requests not yet released');
    // ETH is $4,000 in the price double: Available 0.5 ETH is $2,000
    await expect(tiles.locator('.tile-usd')).toHaveCount(4, { timeout: 15_000 });
    await expect(tiles.locator('.tile-usd').nth(1)).toHaveText('$2,000');
    await expect(page.getByTestId('balance-DAI')).toHaveCount(0);

    // one table, both directions, newest first
    const tl = page.getByTestId('timeline');
    await expect(tl.locator('[data-testid="timeline-row"]')).toHaveCount(10); // 3 deposits + 7 orders
    await expect(tl).toContainText('Deposit');
    await expect(tl).toContainText('Payout');
    await expect(tl.locator('[data-status="credited"]')).toHaveText('Credited');
    await expect(tl.locator('[data-status="confirming"]')).toHaveText('Confirming');
    await expect(tl.locator('a[href^="https://etherscan.io/tx/0x1111"]')).toHaveCount(1);
    await expect(tl.locator('a[href^="https://arbiscan.io/tx/0x4444"]')).toHaveCount(1);
    // internals: the Beam message id, the ledger's refs and the "append-only" note are all gone
    await expect(page.locator('body')).not.toContainText('4821');
    await expect(page.locator('body')).not.toContainText('Beam msg');
    await expect(page.locator('body')).not.toContainText('ledger is append-only');
    await expect(page.locator('body')).not.toContainText('Source amounts are shown with 18 decimals');

    // the rest of a row is one click away, and it carries no order id
    await expect(page.getByTestId('timeline-detail')).toHaveCount(0);
    const credited = tl.locator('tr', { has: page.locator('[data-status="credited"]') }).first();
    await credited.click();
    const detail = page.getByTestId('timeline-detail');
    await expect(detail).toBeVisible();
    await expect(detail).toContainText('Paid from');
    await expect(detail).toContainText('Into the bridge');
    await expect(detail).not.toContainText('0x2222'); // the cross-chain order id
    await credited.click();
    await expect(page.getByTestId('timeline-detail')).toHaveCount(0);

    // the footer is one sentence on every page — no stats strip, no armed chips
    await expect(page.getByTestId('stats-strip')).toHaveCount(0);
    await expect(page.locator('.footer')).toHaveText('Settled on Beam — a confidential ledger: no addresses on-chain, blinded amounts.');

    // a 401 on a locked route ends the session and asks for a fresh sign-in — by hand this time,
    // because auto sign-in asks once per address and this address already answered
    api.token = null;
    await page.getByRole('button', { name: 'Refresh' }).click();
    await expect(page.getByTestId('expired-banner')).toContainText('Sign in again');
    await expect(page.getByTestId('sign-in-gate')).toContainText('Sign in to see your balance');
    await expect(page.getByTestId('sign-in-gate').getByRole('button', { name: 'Sign in with wallet' })).toBeVisible();
    await expect(signedIn(page)).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.getItem('pgas.session.v1'))).toBeNull();
    expect(pageErrors).toEqual([]);
  });

  test('timeline: every payout status has a pill, holds are in user words, the treasury is never named', async ({ page }) => {
    await boot(page, '/balance');
    await connectAndSignIn(page);
    const tl = page.getByTestId('timeline');

    // the 2026-09-09 order machine, plus the two dark any-asset statuses: every one is mapped, so
    // no row falls back to its raw snake_case name
    await expect(tl.locator('[data-status="scheduled"]')).toHaveText('Scheduled');
    await expect(tl.locator('[data-status="releasing"]')).toHaveText('Releasing');
    await expect(tl.locator('[data-status="bridging"]')).toHaveText('Bridging');
    await expect(tl.locator('[data-status="delivering"]')).toHaveText('Delivering');
    await expect(tl.locator('[data-status="sent"]')).toHaveText('Sent');
    await expect(tl.locator('[data-status="waiting_for_dep_eth"]')).toHaveText('Waiting for ETH');
    await expect(tl.locator('[data-status="waiting_for_swap_to_target_asset"]')).toHaveText('Swapping');
    await expect(tl).not.toContainText('waiting_for_');
    const pillTexts = await tl.locator('.pill').allTextContents();
    expect(pillTexts.filter((t) => t.includes('_'))).toEqual([]); // no raw snake_case pill anywhere

    // hold_reason is an operator flag in the API; the screen says what the user is waiting for
    const releasing = tl.locator('tr', { has: page.locator('[data-status="releasing"]') });
    await expect(releasing.locator('[data-hint="hold"]')).toHaveText('payouts are paused right now');
    await expect(releasing.locator('[data-hint="hold"]')).toHaveClass(/tiny muted/);
    await expect(releasing.locator('.pill')).toHaveCount(1); // the reason is not a second pill
    await expect(page.locator('body')).not.toContainText('PGAS_PAYOUT_DIRECT_ENABLED');

    // beam_confirmations/61 only while bridging
    const bridging = tl.locator('tr', { has: page.locator('[data-status="bridging"]') });
    await expect(bridging.locator('[data-hint="confirmations"]')).toHaveText('43/61 Beam confirmations');
    const delivering = tl.locator('tr', { has: page.locator('[data-status="delivering"]') });
    await expect(delivering.locator('[data-hint="confirmations"]')).toHaveCount(0);

    // cancelling lives on Schedule, where the orders are made — one implementation, not two
    await expect(tl.getByRole('button', { name: 'Cancel' })).toHaveCount(0);

    // treasury:"shielding" is operator information: a hint, never a pill, and never named
    const credited = tl.locator('tr', { has: page.locator('[data-status="credited"]') });
    await expect(credited.locator('[data-hint="deposit"]')).toHaveText('settling on Beam');
    await expect(credited.locator('[data-hint="deposit"]')).toHaveClass(/tiny muted/);
    await expect(credited.locator('.pill')).toHaveCount(1);
    await expect(credited.locator('.pill')).toHaveText('Credited');
    await expect(tl).not.toContainText('shielding');

    // verified:false on a submitted deposit is a hint too
    const submitted = tl.locator('tr', { has: page.locator('[data-status="submitted"]') });
    await expect(submitted.locator('[data-hint="deposit"]')).toHaveText('verifying transaction');
    await expect(submitted.locator('.pill')).toHaveCount(1);
    expect(pageErrors).toEqual([]);
  });

  // The presets are pure functions of the local clock, so the clock is pinned: 2026-09-09 10:15 UTC
  // in a UTC browser. Every number below is arithmetic anyone can redo by hand.
  test.describe('with the clock pinned', () => {
    test.use({ timezoneId: 'UTC' });

    test('schedule: presets become deliver_at unix seconds, and the form says when it goes to the bridge', async ({ page }) => {
      const FIXED = Date.UTC(2026, 8, 9, 10, 15, 0);
      const nowS = FIXED / 1000; // 1788948900
      await boot(page, '/schedule', { fixedTime: FIXED });
      await connectAndSignIn(page);

      const fill = async (n: number, address: string, preset: string) => {
        if (n > 1) await page.getByTestId('schedule-add').click();
        await page.getByLabel(`Address ${n}`).fill(address);
        await page.getByLabel(`Amount ${n}`).fill('0.01');
        await page.getByLabel(`Deliver ${n}`).selectOption(preset);
      };
      await fill(1, walletA.address, 'asap');
      await fill(2, walletB.address, '2h');
      await fill(3, ADDR3, 'tonight');
      await fill(4, ADDR4, 'tomorrow');
      await fill(5, ADDR5, 'custom');
      await page.getByLabel('Custom time 5').fill('2026-09-12T18:30');

      // bridge_eta_s = 3960 (66 min): the order is released that far ahead, but never before now
      await expect(page.getByTestId('deliver-hint-0')).toHaveText('to the bridge at 10:15 · arrives ≈ 11:21');
      await expect(page.getByTestId('deliver-hint-1')).toHaveText('to the bridge at 11:09 · arrives ≈ 12:15');
      await expect(page.getByTestId('deliver-hint-2')).toContainText('to the bridge at 01:54 · arrives ≈ 03:00');
      await expect(page.getByTestId('deliver-hint-2')).toContainText('Thu 10 Sep');
      await expect(page.getByTestId('deliver-hint-3')).toContainText('to the bridge at 09:09 · arrives ≈ 10:15');
      await expect(page.getByTestId('deliver-hint-4')).toContainText('to the bridge at 17:24 · arrives ≈ 18:30');

      await page.getByTestId('schedule-submit').click();
      await expect(page.getByTestId('schedule-result')).toContainText('Scheduled 5 orders');
      const body = api.calls.find((c) => c.path === '/withdrawals' && c.method === 'POST')!.body as {
        items: { W: string; deliver_at: number }[];
      };
      expect(body.items.map((i) => i.deliver_at)).toEqual([
        nowS, // ASAP
        nowS + 2 * 3600, // in 2 h
        Date.UTC(2026, 8, 10, 3, 0, 0) / 1000, // tonight 03:00 local, which is tomorrow already
        nowS + 86400, // tomorrow, same time
        Date.UTC(2026, 8, 12, 18, 30, 0) / 1000, // the custom datetime, read as local
      ]);
      // and the API's release_at came back 66 min ahead of each of them (never before now)
      const rows = page.getByTestId('schedule-orders').locator('tr.row-new');
      await expect(rows).toHaveCount(5, { timeout: 15_000 });
      expect(pageErrors).toEqual([]);
    });
  });

  test('deposit: a cross-chain quote is only an estimate — the click arms the order, then sends and registers', async ({ page }) => {
    await boot(page);
    api.armed = true;
    await connectAndSignIn(page);
    await payWith(page, { chainId: 42161 });
    await page.getByLabel('Amount (ETH)').fill('0.2');
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.2 ETH on Arbitrum');
    // POST /v1/quote is one router call and carries no tx: nothing is armed until the click
    const quote = api.calls.filter((c) => c.path === '/quote').pop();
    expect(quote?.body).toMatchObject({ src_chain_id: 42161, amount: '200000000000000000' });
    expect(api.armCalls).toEqual([]);

    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    expect(api.armCalls).toHaveLength(1);
    const arm = api.calls.find((c) => c.method === 'POST' && /^\/quote\/q-[a-z0-9]+\/arm$/.test(c.path));
    expect(arm?.auth).toBe(`Bearer ${api.token}`);
    const sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ to: ROUTER_ORDER, data: '0xdeadbeef', value: '0x2c68af0bb140000' });
    const reg = api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST');
    expect(reg).toHaveLength(1);
    expect(reg[0].body).toEqual({ quote_id: arm!.path.split('/')[2], src_tx_hash: MOCK_TX });

    // an armed order that came back 1 % away from the number on the screen needs a second click
    api.armDriftPct = -1;
    await payWith(page, { token: 'usdc' });
    await page.getByLabel('Amount (USDC)').fill('250');
    await expect(page.getByTestId('deposit-btn')).toBeEnabled();
    const shown = await page.getByTestId('quote-out').textContent();
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('estimate-changed')).toContainText('came back at');
    await expect(page.getByTestId('deposit-btn')).toContainText('Deposit at');
    expect(await page.getByTestId('quote-out').textContent()).not.toBe(shown); // the new number is the one on screen
    expect(await page.evaluate(() => (window as any).__mock.state.sent)).toHaveLength(1); // nothing sent yet
    expect(api.armCalls).toHaveLength(2);

    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    const after = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(after).toHaveLength(3); // the armed order's own approval, then the order tx
    expect(after[1]).toMatchObject({ to: USDC });
    expect(after[1].data).toMatch(/^0x095ea7b3000000000000000000000000ef4fb24ad0916217251f553c0596f8edc630eb66/i);
    expect(after[2]).toMatchObject({ to: ROUTER_ORDER, data: '0xdeadbeef' });
    expect(api.armCalls).toHaveLength(2); // the second click reuses the order it already agreed to
    expect(api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST')).toHaveLength(2);
    expect(pageErrors).toEqual([]);
  });

  test('quote mode: a name this client does not know is still a cross-chain order', async ({ page }) => {
    await boot(page);
    api.armed = true;
    // The neutral name for this mode is `xchain`, and an API build that still answers with its own
    // older name must produce exactly the same screen and the same transaction. The client gets
    // there by treating every mode that is not `direct` or `swap` as the cross-chain order, so any
    // stand-in proves the rule for the real alias too.
    api.xchainWire = 'a-name-this-client-has-never-seen';
    await connectAndSignIn(page);
    await payWith(page, { chainId: 42161 });
    await page.getByLabel('Amount (ETH)').fill('0.2');
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.2 ETH on Arbitrum');
    await expect(page.getByTestId('direct-note')).toHaveCount(0);
    await expect(page.getByTestId('swap-panel')).toHaveCount(0);
    expect(api.armCalls).toEqual([]); // an estimate only, exactly like `xchain`

    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    expect(api.armCalls).toHaveLength(1);
    const sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ to: ROUTER_ORDER, data: '0xdeadbeef', value: '0x2c68af0bb140000' });
    expect(api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST')).toHaveLength(1);
    expect(pageErrors).toEqual([]);
  });

  // ---------- the Uniswap V4 ingress (2026-09-10, T12) ----------
  //
  // The flags come from the API (`/assets`, `/health`), so every one of these states the world it
  // is testing before the page loads. The default — `uniswapEnabled = false` — is the "flag off"
  // case, and it is what every other test in this file runs against.

  test('deposit: with the Uniswap ingress open it is the primary card, and native ETH needs no approval', async ({ page }) => {
    await boot(page, '/', {
      mock: (a) => {
        a.uniswapEnabled = true;
        a.armed = true;
      },
    });
    await connectAndSignIn(page);
    const form = page.getByTestId('deposit-form');
    await expect(form).toHaveAttribute('data-route', 'uniswap');
    await expect(form).toContainText('Pay with Uniswap V4');
    await expect(page.getByTestId('uniswap-primary')).toContainText('one transaction on Ethereum');

    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('quote-out')).toContainText('0.1');
    // the request names the route; everything else about the body is unchanged
    expect(api.calls.filter((c) => c.path === '/quote').pop()?.body).toEqual({
      src_chain_id: 1,
      src_token: NATIVE,
      amount: '100000000000000000',
      target_asset: 'ETH',
      sender: walletA.address,
      route: 'uniswap',
    });

    await expect(page.getByTestId('amount-usd')).toContainText('$400');
    // nothing is swapped when ETH is what you pay and what you get: the bound is the amount itself
    await expect(page.getByTestId('min-out')).toHaveText('min you receive 0.1 ETH');
    await expect(page.getByTestId('price-impact')).toHaveText('price impact 0.00%');
    await expect(page.getByTestId('lands-in')).toContainText('Lands in your balance in ≈');
    await expect(page.getByTestId('lands-in')).toContainText('bridge fee $0.40');
    await expect(page.getByTestId('uniswap-note')).toHaveText(UNISWAP_NOTE);
    await expect(page.getByTestId('direct-note')).toHaveCount(0);
    await expect(page.getByTestId('swap-panel')).toHaveCount(0);
    await expect(page.getByTestId('approve-btn')).toHaveCount(0); // native ETH: no approval, ever
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');

    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    expect(api.armCalls).toEqual([]); // the tx came with the quote — there is nothing to arm
    const sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ from: walletA.address, to: PGAS_ROUTER, data: '0xc0ffee00', value: '0x16345785d8a0000' });
    const reg = api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST');
    expect(reg).toHaveLength(1);
    expect(reg[0].body).toEqual({ quote_id: expect.stringMatching(/^q-/), src_tx_hash: MOCK_TX });
    expect(reg[0].auth).toBe(`Bearer ${api.token}`);

    // four steps, not five: on this route the swap and the lock are the same transaction, so there
    // is no order for anyone to fill and no step that can never light up
    const timeline = page.getByTestId('deposit-timeline');
    await expect(timeline.locator('.timeline')).toHaveAttribute('data-route', 'uniswap');
    await expect(timeline.locator('.tl-step')).toHaveCount(4);
    await expect(timeline.locator('.tl-title')).toHaveText(['Submitted', 'Bridging', 'Confirming', 'Credited']);
    await expect(timeline).not.toContainText('Order filled');

    // and the mechanism panel says which path this is, in step 2
    await page.getByTestId('how-it-works').click();
    const modal = page.getByTestId('how-it-works-modal');
    await expect(modal.locator('li')).toHaveCount(6);
    await expect(modal.locator('li').nth(1)).toHaveText(
      'A Uniswap V4 swap on Ethereum whose Pgas hook locks the output in the Beam bridge in the same transaction.',
    );
    await expect(modal).not.toContainText('A cross-chain order fills it');
    await expect(modal).toContainText('Beam bridge');
    expect(pageErrors).toEqual([]);
  });

  test('deposit: USDC through the Uniswap ingress approves the router and deposits on one click', async ({ page }) => {
    await boot(page, '/', {
      mock: (a) => {
        a.uniswapEnabled = true;
        a.armed = true;
      },
    });
    await connectAndSignIn(page);
    await payWith(page, { token: 'usdc' });
    await page.getByLabel('Amount (USDC)').fill('250');
    await expect(page.getByTestId('deposit-form')).toContainText('Pay with Uniswap V4');

    // a real swap this time: the estimate, the bound the hook reverts below, and what it costs
    await expect(page.getByTestId('quote-out')).toContainText(/0\.0623/);
    expect(api.calls.filter((c) => c.path === '/quote').pop()?.body).toMatchObject({
      src_chain_id: 1,
      src_token: USDC,
      amount: '250000000',
      route: 'uniswap',
    });
    await expect(page.getByTestId('min-out')).toHaveText('min you receive 0.062001 ETH');
    await expect(page.getByTestId('price-impact')).toHaveText('price impact 0.30%');
    await expect(page.getByTestId('uniswap-note')).toHaveText(UNISWAP_NOTE);
    await expect(page.getByTestId('swap-panel')).toHaveCount(0); // not the wallet-side swap mode
    // ONE button: the router's approval is known before the click, so it does not need its own
    await expect(page.getByTestId('approve-btn')).toHaveCount(0);
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 250 USDC on Ethereum');

    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    const sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatchObject({ to: USDC });
    expect(sent[0].data.toLowerCase()).toContain(PGAS_ROUTER.slice(2).toLowerCase()); // approve(router, amount)
    expect(sent[0].data).toMatch(/^0x095ea7b3/);
    expect(sent[0].value).toBeUndefined();
    expect(sent[1]).toMatchObject({ to: PGAS_ROUTER, data: '0xc0ffee00' });
    expect(sent[1].value).toBeUndefined();
    expect(api.armCalls).toEqual([]);
    const reg = api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST');
    expect(reg).toHaveLength(1);
    expect(reg[0].body).toEqual({ quote_id: expect.stringMatching(/^q-/), src_tx_hash: MOCK_TX });
    expect(pageErrors).toEqual([]);
  });

  test('deposit: the pay-with list is narrowed to the pairs the Uniswap route takes', async ({ page }) => {
    await boot(page, '/', {
      mock: (a) => {
        a.uniswapEnabled = true;
        a.armed = true;
        a.uniswapTokens = ['ETH', 'USDC']; // the API names the registered pairs; this list is read
      },
    });
    await connectAndSignIn(page);
    await page.getByTestId('pay-with').click();
    const options = page.getByRole('option');
    // narrowed by `ingress.uniswap_tokens`, which only `/dex/assets` carries — so this is that read
    expect(api.calls.some((c) => c.path === '/dex/assets')).toBe(true);
    await expect(options).toHaveCount(2);
    await expect(options.nth(0)).toContainText('ETH');
    await expect(options.nth(1)).toContainText('USDC');
    await page.getByTestId('pay-with').click();

    // DAI and WBTC are not gone from Pgas.me — they are reached by their own pipe, so choosing one
    // as the target puts the whole token list back and stops asking for the Uniswap route
    await page.getByRole('radio', { name: 'DAI' }).click();
    await page.getByTestId('pay-with').click();
    await expect(page.getByRole('option')).toHaveCount(4);
    await page.getByTestId('pay-with').click();
    await payWith(page, { token: 'dai' });
    await page.getByLabel('Amount (DAI)').fill('100');
    await expect(page.getByTestId('direct-note')).toContainText('your DAI goes straight into the Beam bridge');
    expect((api.calls.filter((c) => c.path === '/quote').pop()?.body as { route?: string }).route).toBeUndefined();
    await expect(page.getByTestId('uniswap-note')).toHaveCount(0);
    await expect(page.getByTestId('deposit-form')).toContainText('What to deposit');
    expect(pageErrors).toEqual([]);
  });

  test('deposit: a source off Ethereum falls back to the cross-chain path, or says how to pay when it is closed', async ({ page }) => {
    // both paths open: a chip on another chain is still a deposit — the cross-chain order takes it
    await boot(page, '/', {
      mock: (a) => {
        a.uniswapEnabled = true;
        a.armed = true;
      },
    });
    await connectAndSignIn(page);
    await payWith(page, { chainId: 42161 });
    await page.getByLabel('Amount (ETH)').fill('0.2');
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.2 ETH on Arbitrum');
    await expect(page.getByTestId('uniswap-note')).toHaveCount(0);
    await expect(page.getByTestId('deposit-form')).toContainText('What to deposit');
    // the button above only exists because the quote came back, so the last call is that quote
    expect((api.calls.filter((c) => c.path === '/quote').pop()?.body as { route?: string }).route).toBeUndefined();
    expect(pageErrors).toEqual([]);
  });

  test('deposit: with the cross-chain path closed a chip elsewhere says to switch to Ethereum', async ({ page }) => {
    await boot(page, '/', {
      rpc: true,
      mock: (a) => {
        a.uniswapEnabled = true;
        a.xchainEnabled = false;
        a.armed = true;
      },
    });
    await connectAndSignIn(page);
    // the chips stay: what the wallet holds elsewhere is still worth seeing
    const chips = page.getByTestId('portfolio-chips').locator('.portfolio-chip');
    await expect(chips).toHaveCount(14, { timeout: 30_000 });
    await chips.filter({ hasText: 'CRO' }).first().click();
    await expect(page.getByTestId('xchain-closed')).toHaveText('Switch to Ethereum to pay with Uniswap');
    await expect(page.getByTestId('quote-out')).toHaveCount(0);
    // and nothing was asked of the API for a chain it cannot take
    expect(api.calls.filter((c) => c.path === '/quote' && (c.body as { src_chain_id?: number }).src_chain_id === 25)).toEqual([]);

    await page.getByTestId('use-ethereum').click();
    await expect(page.getByTestId('xchain-closed')).toHaveCount(0);
    await expect(page.getByTestId('deposit-form')).toContainText('Pay with Uniswap V4');
    await page.getByLabel(/^Amount/).fill('0.1');
    await expect(page.getByTestId('uniswap-note')).toHaveText(UNISWAP_NOTE);
    expect((api.calls.filter((c) => c.path === '/quote').pop()?.body as { route?: string }).route).toBe('uniswap');
    expect(pageErrors).toEqual([]);
  });

  test('deposit: when the API says the Uniswap path is closed, nothing asks for that route', async ({ page }) => {
    await boot(page); // the default mock: `ingress.uniswap` is false, and it SAYS so on every read
    api.armed = true;
    await connectAndSignIn(page);
    await expect(page.getByTestId('deposit-form')).toContainText('What to deposit');
    await expect(page.getByTestId('deposit-form')).toHaveAttribute('data-route', 'classic');
    await expect(page.getByTestId('uniswap-primary')).toHaveCount(0);
    await page.getByLabel('Amount (ETH)').fill('0.3');
    await expect(page.getByTestId('direct-note')).toContainText('straight into the Beam bridge');
    // no `route` key at all: an API build that has never heard of the field sees the old body
    expect(api.calls.filter((c) => c.path === '/quote').pop()?.body).toEqual({
      src_chain_id: 1,
      src_token: NATIVE,
      amount: '300000000000000000',
      target_asset: 'ETH',
      sender: walletA.address,
    });
    await expect(page.getByTestId('min-out')).toHaveCount(0);
    await expect(page.getByTestId('price-impact')).toHaveCount(0);
    await expect(page.getByTestId('uniswap-note')).toHaveCount(0);
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    const sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ to: ETH_PIPE }); // the pipe itself, exactly as before
    await expect(page.getByTestId('deposit-timeline').locator('.tl-step')).toHaveCount(5);
    await page.getByTestId('how-it-works').click();
    await expect(page.getByTestId('how-it-works-modal')).toContainText('A cross-chain order fills it on Ethereum');
    expect(pageErrors).toEqual([]);
  });

  test('deposit: an API that states no ingress flags at all leaves the Uniswap path off', async ({ page }) => {
    await boot(page, '/', {
      mock: (a) => {
        // today's live API: no `/dex/assets`, no `/health`, and the account's old `{armed, near}`
        a.silentIngress = true;
        // …and the server-side switch is ON, to prove the client goes by what is STATED. Silence is
        // not a yes: the hook is deployed by a human, and a page that offers "Pay with Uniswap V4"
        // against a hook that is not there is the failure this default exists to prevent.
        a.uniswapEnabled = true;
        a.armed = true;
      },
    });
    await connectAndSignIn(page);
    // two 404s on the way in, and neither is an error the user is shown
    await expect(page.locator('body')).not.toContainText('Reference data failed to load');
    await expect(page.getByTestId('deposit-form')).toHaveAttribute('data-route', 'classic');
    await expect(page.getByTestId('deposit-form')).toContainText('What to deposit');
    await expect(page.getByTestId('uniswap-primary')).toHaveCount(0);

    await page.getByLabel('Amount (ETH)').fill('0.3');
    await expect(page.getByTestId('direct-note')).toContainText('straight into the Beam bridge');
    expect(api.calls.filter((c) => c.path === '/quote').pop()?.body).toEqual({
      src_chain_id: 1,
      src_token: NATIVE,
      amount: '300000000000000000',
      target_asset: 'ETH',
      sender: walletA.address,
    });
    await expect(page.getByTestId('min-out')).toHaveCount(0);
    await expect(page.getByTestId('price-impact')).toHaveCount(0);
    await expect(page.getByTestId('uniswap-note')).toHaveCount(0);

    // the whole pre-Uniswap path, unchanged: the pipe itself, and five steps
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    const sent = await page.evaluate(() => (window as any).__mock.state.sent);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ to: ETH_PIPE });
    await expect(page.getByTestId('deposit-timeline').locator('.tl-step')).toHaveCount(5);
    await page.getByTestId('how-it-works').click();
    await expect(page.getByTestId('how-it-works-modal')).toContainText('A cross-chain order fills it on Ethereum');
    expect(pageErrors).toEqual([]);
  });

  // ---------- the 2026-09-09 screen review (T14) ----------

  test('navigation: three tabs, no Activity page, and its old link lands on the timeline', async ({ page }) => {
    await boot(page, '/activity');
    await connectAndSignIn(page);
    const tabs = page.locator('.nav [data-tab]');
    await expect(tabs).toHaveText(['Deposit', 'Balance', 'Schedule']);
    await expect(page.locator('[data-tab="balance"]').first()).toHaveClass(/active/);
    await expect(page.getByTestId('timeline')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Activity' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Wallets' })).toHaveCount(0);

    await goTab(page, 'schedule');
    await expect(page.getByTestId('schedule-form')).toBeVisible();
    expect(new URL(page.url()).pathname).toBe('/schedule');
    await goTab(page, 'deposit');
    await expect(page.getByTestId('deposit-form')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('sign-in: the card comes back only when the signature does not land, and it is not re-asked', async ({ page }) => {
    await boot(page);
    // the last route registered wins in Playwright, so this shadows the mock API for one path:
    // the wallet signs, the server refuses — the same state a user who rejects the prompt lands in
    let attempts = 0;
    await page.route('**/api/v1/siwe/verify', (r) => {
      attempts++;
      return r.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'signature rejected' }) });
    });
    await page.getByRole('button', { name: 'Connect wallet' }).click();
    await page.locator('[data-wallet-id="6963:me.pgas.mock"]').click();

    const gate = page.getByTestId('sign-in-gate');
    await expect(gate).toContainText('Sign in to get a quote');
    await expect(gate).toContainText('signature rejected');
    await expect(page.getByTestId('connected-address')).toHaveAttribute('data-signed-in', 'no');
    // asked once: the wallet is not prompted again by itself
    expect(attempts).toBe(1);
    await page.waitForTimeout(500);
    expect(attempts).toBe(1);

    // the button in the card is the retry, and it works once the server stops refusing
    await page.unroute('**/api/v1/siwe/verify');
    await gate.getByRole('button', { name: 'Sign in with wallet' }).click();
    await expect(signedIn(page)).toBeVisible();
    await expect(page.getByTestId('sign-in-gate')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
  });

  test('theme: the switch flips it, remembers the choice, and the dark tokens really apply', async ({ page }) => {
    await boot(page);
    const html = page.locator('html');
    // the context is a light-scheme browser and nothing is stored yet: the OS decides
    await expect(html).toHaveAttribute('data-theme', 'light');
    expect(await page.evaluate(() => localStorage.getItem('pgas.theme.v1'))).toBeNull();

    await page.getByTestId('theme-toggle').click();
    await expect(html).toHaveAttribute('data-theme', 'dark');
    expect(await page.evaluate(() => localStorage.getItem('pgas.theme.v1'))).toBe('dark');
    expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(15, 19, 25)');

    await page.reload();
    await expect(html).toHaveAttribute('data-theme', 'dark');
    expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(15, 19, 25)');

    await page.getByTestId('theme-toggle').click();
    await expect(html).toHaveAttribute('data-theme', 'light');
    expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(244, 246, 250)');
    expect(pageErrors).toEqual([]);
  });

  test.describe('with the OS set to dark', () => {
    test.use({ colorScheme: 'dark' });

    test('theme: with no choice stored the OS wins, and an explicit Light beats it', async ({ page }) => {
      await boot(page);
      await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
      expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(15, 19, 25)');
      await page.getByTestId('theme-toggle').click();
      await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
      expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(244, 246, 250)');
      await page.reload();
      await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
      expect(pageErrors).toEqual([]);
    });
  });

  test('intros are one sentence; the machinery is behind "How it works"', async ({ page }) => {
    await boot(page);
    await connectAndSignIn(page);
    const head = page.locator('.page-head');
    await expect(head).toContainText('Pay from any chain. Fund fresh wallets later, with no on-chain link to the source.');
    await expect(head).not.toContainText('cross-chain order');
    await expect(head).not.toContainText('hook');

    await page.getByTestId('how-it-works').click();
    const modal = page.getByTestId('how-it-works-modal');
    await expect(modal.locator('li')).toHaveCount(6);
    await expect(modal).toContainText('cross-chain order');
    await expect(modal).toContainText('Beam bridge');
    // T17: which wallets were tested, said in the only way that is true (a simulation is not a device)
    const tested = modal.getByTestId('tested-wallets');
    await expect(tested).toContainText('MetaMask, Rabby, Zerion, Trust, OKX, Coinbase Wallet, Phantom, Coin98, Binance Web3, Bitget');
    await expect(tested).toContainText('simulated in tests');
    await expect(tested).toContainText('Real-device checks: none yet');
    await expect(tested).not.toContainText('works in every wallet');
    await modal.getByRole('button', { name: 'Close' }).click();
    await expect(modal).toHaveCount(0);

    await goTab(page, 'balance');
    await expect(page.locator('.page-head')).toContainText(
      'Your balance on Beam. Deposits arrive here; scheduled payouts leave from here.',
    );
    await goTab(page, 'schedule');
    await expect(page.locator('.page-head')).toContainText('Send ETH from your balance to any wallets, at the time you choose.');
    await expect(page.locator('.page-head')).not.toContainText('nothing to register');
    expect(pageErrors).toEqual([]);
  });

  test.describe('on a phone', () => {
    test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

    test('mobile: the tabs are a bottom bar, the header is one pill, the schedule totals stick', async ({ page }) => {
      await boot(page);
      await connectAndSignIn(page);
      const bar = page.getByTestId('tabbar');
      await expect(bar).toBeVisible();
      await expect(bar.locator('[data-tab]')).toHaveCount(3);
      await expect(page.locator('.nav')).toBeHidden(); // the header tabs are the desktop's

      // the header carries the logo, the theme switch and ONE account pill — Disconnect is inside it
      await expect(page.locator('.header').getByRole('button', { name: 'Disconnect' })).toHaveCount(0);
      await expect(page.getByTestId('connected-address')).toBeVisible();
      await page.getByTestId('connected-address').click();
      const menu = page.getByTestId('account-menu');
      await expect(menu).toContainText('Ethereum');
      await expect(menu).toContainText('Signed in');
      await expect(menu.getByRole('button', { name: 'Disconnect' })).toBeVisible();
      await page.keyboard.press('Escape');

      await goTab(page, 'schedule');
      await expect(page.getByTestId('schedule-form')).toBeVisible();
      const sticky = page.getByTestId('sticky-totals');
      const totals = page.getByTestId('schedule-totals');
      /**
       * The bar repeats the Totals card's two numbers for while that card is off screen — so while
       * the card IS on screen it is not drawn at all. Screen review 2026-09-10: it used to be, and
       * a sticky element hovers over whatever follows it until the scroll reaches its own place in
       * the flow, which put it on top of "Total debited" at exactly this viewport.
       */
      await expect(totals).toBeInViewport();
      await expect(sticky).toBeHidden();

      // push the card off the bottom and the bar comes back — above the tab bar, over nothing
      for (let i = 0; i < 4; i++) await page.getByTestId('schedule-add').click();
      await page.evaluate(() => window.scrollTo(0, 0));
      await expect(totals).not.toBeInViewport();
      await expect(sticky).toBeVisible();
      await expect(sticky).toContainText('Remaining');
      const box = (await sticky.boundingBox())!;
      const barBox = (await bar.boundingBox())!;
      expect(box.y + box.height).toBeLessThanOrEqual(844);
      expect(box.y + box.height).toBeLessThanOrEqual(barBox.y + 1); // clear of the tab bar
      const cardBox = (await totals.boundingBox())!;
      expect(cardBox.y).toBeGreaterThan(box.y + box.height); // and clear of the card it repeats

      // scrolled back to the card, the bar gets out of its way again
      await totals.scrollIntoViewIfNeeded();
      await expect(sticky).toBeHidden();

      // at the very bottom nothing is left under either fixed thing
      await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
      const lastCard = (await page.locator('.page > section.card').last().boundingBox())!;
      const barNow = (await bar.boundingBox())!;
      expect(lastCard.y + lastCard.height).toBeLessThanOrEqual(barNow.y);
      expect(pageErrors).toEqual([]);
    });
  });
});
