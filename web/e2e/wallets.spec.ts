// The multi-wallet suite (T17, admin 2026-09-09: "make sure I can test it among many wallets and it
// will work").
//
// Twelve wallets, each simulated as itself: the eight that announce over EIP-6963 carry the rdns
// they really publish (io.metamask, io.zerion.wallet, io.rabby, com.trustwallet.app,
// com.okex.wallet, com.coinbase.wallet, app.phantom, coin98.com), and four are found only by the
// global they set (window.coin98.provider, window.BinanceChain, window.bitkeep.ethereum, and
// TokenPocket's isTokenPocket). Each one goes through the whole app — picker row, connect, chain,
// sign-in, portfolio scan, quote, send, disconnect — plus the two events a wallet fires at us
// (accountsChanged, chainChanged). The table at the end of the run is the summary.
//
// The second half is the quirks. Each is a thing a shipped wallet does that breaks a dapp that
// assumes the happy path; the mock reproduces the behaviour and the test proves the handling.
import { expect, test, type Browser, type Page } from '@playwright/test';
import { toBeHex } from 'ethers';
import { CHAIN_META, chainIdHex } from '../src/lib/chains';
import {
  ALL_PROFILES,
  ANNOUNCED_PROFILES,
  DEMO_HOLDINGS,
  LEGACY_PROFILES,
  MockApi,
  MockRpc,
  blockExternal,
  installMockPrices,
  STRICT_CHAIN_PROFILE,
  installWallets,
  signedIn,
  walletA,
  walletB,
  type MockWalletProfile,
} from './mocks';

const ETH_PIPE = '0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96';
const ROUTER_ORDER = '0xeF4fB24aD0916217251F553c0596F8Edc630EB66';

const profile = (key: string, patch: Partial<MockWalletProfile> = {}): MockWalletProfile => {
  const base = ALL_PROFILES.find((p) => p.key === key);
  if (!base) throw new Error(`no profile ${key}`);
  return { ...base, ...patch };
};

// ---------------------------------------------------------------------------
// the run table
// ---------------------------------------------------------------------------
const STEPS = ['row', 'connect', 'chain', 'sign-in', 'scan', 'quote', 'send', 'disconnect'] as const;
type Step = (typeof STEPS)[number];
const REPORT: { label: string; marks: Record<Step, string> }[] = [];

function tracker(label: string) {
  const marks = Object.fromEntries(STEPS.map((s) => [s, '·'])) as Record<Step, string>;
  REPORT.push({ label, marks });
  return (s: Step) => {
    marks[s] = '✓';
  };
}

test.afterAll(() => {
  if (!REPORT.length) return;
  const w = Math.max(6, ...REPORT.map((r) => r.label.length));
  const head = `${'wallet'.padEnd(w)} | ${STEPS.map((s) => s.padEnd(10)).join('| ')}`;
  const lines = REPORT.map((r) => `${r.label.padEnd(w)} | ${STEPS.map((s) => r.marks[s].padEnd(10)).join('| ')}`);
  console.log(`\n${head}\n${'-'.repeat(head.length)}\n${lines.join('\n')}\n`);
});

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------
interface BootOpts {
  rpc?: boolean;
  armed?: boolean;
  path?: string;
  assets?: boolean;
}

async function boot(page: Page, profiles: MockWalletProfile[], opts: BootOpts = {}) {
  const api = new MockApi();
  api.armed = opts.armed ?? true;
  const pageErrors: string[] = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page, opts.assets ?? false);
  await api.install(page);
  if (opts.rpc) {
    await new MockRpc(DEMO_HOLDINGS).install(page);
    await installMockPrices(page);
  }
  await installWallets(page, profiles);
  await page.goto(opts.path ?? '/');
  return { api, pageErrors };
}

const handleState = (page: Page, key: string) => page.evaluate((k) => (window as never as Record<string, any>).__wallets[k].state, key);
const call = (page: Page, key: string, method: string, arg?: unknown) =>
  page.evaluate(({ k, m, a }) => (window as never as Record<string, any>).__wallets[k][m](a), { k: key, m: method, a: arg });

/**
 * One raw EIP-1193 call straight at a mock wallet's provider — no UI — returning the shape a dapp
 * sees: `ok`, plus the `code` and `message` of the rejection. The chain-id tests use it to hand a
 * wallet something the app itself must never send.
 */
const walletCall = (page: Page, handle: string, method: string, param: Record<string, unknown>) =>
  page.evaluate(
    async ({ h, m, p }) => {
      const prov = (window as never as Record<string, any>).__wallets[h].provider;
      try {
        await prov.request({ method: m, params: [p] });
        return { ok: true, code: 0, message: '' };
      } catch (e: any) {
        return { ok: false, code: e?.code, message: String(e?.message ?? '') };
      }
    },
    { h: handle, m: method, p: param },
  );

async function openPicker(page: Page) {
  await page.getByRole('button', { name: 'Connect wallet' }).first().click();
  return page.getByRole('dialog', { name: 'Connect a wallet' }).locator('.wallet-row');
}

// ---------------------------------------------------------------------------
// the twelve journeys
// ---------------------------------------------------------------------------
interface Journey {
  label: string;
  handle: string;
  rowId: string;
  rowName: string;
  profiles: MockWalletProfile[];
}

/**
 * Each wallet is installed the way it really installs itself: an announcement AND the global it
 * also sets, so "one row" is a claim about a real double sighting rather than about a mock that
 * only announces. Coin98 appears twice on purpose — announced under coin98.com and again as
 * `window.coin98.provider` (a different object, as its extension really does) — and must be one row.
 */
const JOURNEYS: Journey[] = [
  {
    label: 'MetaMask',
    handle: 'metamask',
    rowId: '6963:io.metamask',
    rowName: 'MetaMask',
    profiles: [profile('metamask', { inProviders: false, global: 'ethereum' })],
  },
  {
    label: 'Zerion',
    handle: 'zerion',
    rowId: '6963:io.zerion.wallet',
    rowName: 'Zerion',
    profiles: [profile('zerion', { global: 'ethereum' })],
  },
  {
    label: 'Rabby',
    handle: 'rabby',
    rowId: '6963:io.rabby',
    rowName: 'Rabby Wallet',
    profiles: [profile('rabby', { global: 'rabby' })],
  },
  {
    label: 'Trust',
    handle: 'trust',
    rowId: '6963:com.trustwallet.app',
    rowName: 'Trust Wallet',
    profiles: [profile('trust', { global: 'trustwallet' })],
  },
  {
    label: 'OKX',
    handle: 'okx',
    rowId: '6963:com.okex.wallet',
    rowName: 'OKX Wallet',
    profiles: [profile('okx', { global: 'okxwallet' })],
  },
  {
    label: 'Coinbase Wallet',
    handle: 'coinbase',
    rowId: '6963:com.coinbase.wallet',
    rowName: 'Coinbase Wallet',
    profiles: [profile('coinbase', { inProviders: false, global: 'coinbaseWalletExtension' })],
  },
  {
    label: 'Phantom',
    handle: 'phantom',
    rowId: '6963:app.phantom',
    rowName: 'Phantom',
    profiles: [profile('phantom')],
  },
  {
    label: 'Coin98',
    handle: 'coin98',
    rowId: '6963:coin98.com',
    rowName: 'Coin98 Wallet',
    profiles: [profile('coin98'), profile('coin98global')],
  },
  {
    label: 'Coin98 (in-app)',
    handle: 'coin98global',
    rowId: 'inj:coin98',
    rowName: 'Coin98',
    profiles: [profile('coin98global')],
  },
  {
    label: 'Binance Web3',
    handle: 'binance',
    rowId: 'inj:binance',
    rowName: 'Binance Web3 Wallet',
    profiles: [profile('binance')],
  },
  {
    label: 'Bitget',
    handle: 'bitget',
    rowId: 'inj:bitget',
    rowName: 'Bitget Wallet',
    profiles: [profile('bitget')],
  },
  {
    label: 'TokenPocket',
    handle: 'tokenpocket',
    rowId: 'inj:tokenpocket',
    rowName: 'TokenPocket',
    profiles: [profile('tokenpocket', { inProviders: false, global: 'ethereum' })],
  },
];

for (const j of JOURNEYS) {
  test(`wallet ${j.label}: picker → connect → chain → sign-in → scan → quote → send → disconnect`, async ({ page }) => {
    const mark = tracker(j.label);
    const { api, pageErrors } = await boot(page, j.profiles, { rpc: true, armed: true });

    // ---- one row, whatever the wallet announced and whichever globals it set ----
    const rows = await openPicker(page);
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toHaveAttribute('data-wallet-id', j.rowId);
    await expect(rows.first()).toContainText(j.rowName);
    mark('row');

    // ---- connect: the address comes back, and is shown EIP-55 whatever case the wallet used ----
    await rows.first().click();
    await expect(page.getByTestId('connected-address')).toContainText(walletA.address.slice(0, 6));
    await page.getByTestId('connected-address').click();
    await expect(page.getByTestId('account-menu-address')).toHaveText(walletA.address);
    await page.keyboard.press('Escape');
    mark('connect');

    // ---- the chain chip ----
    await expect(page.getByTestId('connected-address')).toHaveAttribute('title', `Ethereum · ${walletA.address}`);
    mark('chain');

    // ---- SIWE, asked for by itself, signed once whatever shape the wallet demanded ----
    await expect(signedIn(page)).toBeVisible();
    const verify = api.calls.filter((c) => c.path === '/siwe/verify');
    expect(verify).toHaveLength(1);
    expect((verify[0].body as { message: string }).message).toContain(walletA.address);
    expect((await handleState(page, j.handle)).signed).toBe(1);
    mark('sign-in');

    // ---- the portfolio scan (public RPCs, never the wallet's own numbers) ----
    await expect(page.getByTestId('portfolio-chips').locator('.portfolio-chip')).toHaveCount(14, { timeout: 30_000 });
    mark('scan');

    // ---- a direct quote on Ethereum ----
    await page.getByLabel('Amount (ETH)').fill('0.1');
    await expect(page.getByTestId('quote-out')).toContainText('0.1');
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');
    mark('quote');

    // ---- chainChanged: the chip follows the wallet and the quote is asked again on the new chain ----
    await call(page, j.handle, 'setChain', 8453);
    await expect(page.getByTestId('connected-address')).toHaveAttribute('title', `Base · ${walletA.address}`);
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Base');
    await expect
      .poll(() => (api.calls.filter((c) => c.path === '/quote').pop()?.body as { src_chain_id?: number })?.src_chain_id)
      .toBe(8453);
    await call(page, j.handle, 'setChain', 1);
    await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');

    // ---- the send goes through THIS provider ----
    await page.getByTestId('deposit-btn').click();
    await expect(page.getByTestId('deposit-timeline')).toBeVisible();
    const sent = (await handleState(page, j.handle)).sent;
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ to: ETH_PIPE, data: '0xdeadbeef', value: '0x16345785d8a0000' });
    expect(String(sent[0].from).toLowerCase()).toBe(walletA.address.toLowerCase());
    expect(api.calls.filter((c) => c.path === '/deposits' && c.method === 'POST')).toHaveLength(1);
    mark('send');

    // ---- accountsChanged: the old session goes, the new address signs in, the portfolio re-scans ----
    await call(page, j.handle, 'setAccounts', [walletB.address]);
    await expect(page.getByTestId('connected-address')).toContainText(walletB.address.slice(0, 6));
    await expect.poll(() => api.calls.filter((c) => c.path === '/siwe/verify').length).toBe(2);
    expect((api.calls.filter((c) => c.path === '/siwe/verify').pop()!.body as { message: string }).message).toContain(walletB.address);
    await expect
      .poll(() => page.evaluate((a) => localStorage.getItem(`pgas.portfolio.v1.${a}`) !== null, walletB.address.toLowerCase()), {
        timeout: 30_000,
      })
      .toBe(true);

    // ---- disconnect leaves nothing behind ----
    await page.getByTestId('connected-address').click();
    await page.getByTestId('account-menu').getByRole('button', { name: 'Disconnect' }).click();
    await expect(page.getByTestId('connected-address')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Connect wallet' })).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('pgas.wallet.v1'))).toBeNull();
    expect(await page.evaluate(() => localStorage.getItem('pgas.session.v1'))).toBeNull();
    mark('disconnect');

    expect(pageErrors).toEqual([]);
  });
}

// ---------------------------------------------------------------------------
// the quirks
// ---------------------------------------------------------------------------

test('quirk (a) Zerion: a wallet that answers 0 for a balance it has never empties the portfolio', async ({ page }) => {
  // buybeam recorded Zerion's injected RPC answering 0 for a real balance. The scanner reads a
  // public RPC first and falls back to the wallet only when every public endpoint failed — a
  // fallback catches an error, never a lie.
  const { api, pageErrors } = await boot(page, [profile('zerion', { global: 'ethereum' })], { rpc: true });
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  const chips = page.getByTestId('portfolio-chips').locator('.portfolio-chip');
  await expect(chips).toHaveCount(14, { timeout: 30_000 });
  const ethChip = chips.filter({ has: page.locator('.portfolio-chip-sym', { hasText: /^ETH$/ }) }).first();
  await expect(ethChip.locator('.portfolio-chip-usd')).toHaveText('$1,000'); // 0.25 ETH × $4,000
  // and the proof it was not the wallet: the tooltip names how each chain was read
  const refresh = page.getByTestId('portfolio').getByRole('button', { name: 'Refresh' });
  await expect(refresh).toHaveAttribute('title', /read 5 of 5 EVM chains/);
  await expect(refresh).not.toHaveAttribute('title', /through the wallet/);
  expect(api.calls.some((c) => c.path === '/account')).toBe(true);
  expect(pageErrors).toEqual([]);
});

test('quirk (b) Rabby: 4902 adds the chain with our own parameters and retries the switch', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('rabby', { global: 'rabby' })], { armed: true });
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  await page.getByTestId('pay-with').click();
  await page.locator('[data-chain="42161"]').click();
  await page.getByLabel('Amount (ETH)').fill('0.2');
  await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.2 ETH on Arbitrum');
  await page.getByTestId('deposit-btn').click();
  await expect(page.getByTestId('deposit-timeline')).toBeVisible();

  const state = await handleState(page, 'rabby');
  // CHAIN_META[42161], verbatim: a wallet that does not know the chain is told what it is
  expect(state.addCalls).toHaveLength(1);
  expect(state.addCalls[0]).toMatchObject({
    chainId: '0xa4b1',
    chainName: 'Arbitrum One',
    nativeCurrency: { name: 'Ether', symbol: 'ETH', decimals: 18 },
    rpcUrls: ['https://arb1.arbitrum.io/rpc'],
    blockExplorerUrls: ['https://arbiscan.io'],
  });
  expect(state.switchCalls).toHaveLength(2); // refused with 4902, then accepted after the add
  expect(state.chainId).toBe(42161);
  expect(state.sent).toHaveLength(1);
  expect(state.sent[0]).toMatchObject({ to: ROUTER_ORDER });
  expect(pageErrors).toEqual([]);
});

test('quirk (b) 4001: a refused chain switch is a sentence, and it is asked exactly once', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('metamask', { inProviders: false, global: 'ethereum', quirks: ['rejectSwitch'] })], {
    armed: true,
  });
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  await page.getByTestId('pay-with').click();
  await page.locator('[data-chain="42161"]').click();
  await page.getByLabel('Amount (ETH)').fill('0.2');
  await page.getByTestId('deposit-btn').click();

  await expect(page.getByTestId('quote-card')).toContainText('You rejected the request in the wallet');
  await expect(page.locator('body')).not.toContainText('Internal JSON-RPC');
  const state = await handleState(page, 'metamask');
  expect(state.switchCalls).toHaveLength(1); // asked once — a Cancel is not retried, nor "added"
  expect(state.addCalls).toHaveLength(0);
  expect(state.sent).toHaveLength(0);
  expect(state.chainId).toBe(1);
  // and the button is still there to try again, by hand
  await expect(page.getByTestId('deposit-btn')).toBeEnabled();
  expect(pageErrors).toEqual([]);
});

test('quirk (c) Trust: lowercase accounts are matched case-insensitively and shown checksummed', async ({ page }) => {
  const { api, pageErrors } = await boot(page, [profile('trust', { global: 'trustwallet' })], { armed: true });
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  // the wallet said 0x70997970…, all lowercase; every place it is shown is EIP-55
  await expect(page.getByTestId('connected-address')).toHaveAttribute('title', `Ethereum · ${walletA.address}`);
  await page.getByTestId('connected-address').click();
  await expect(page.getByTestId('account-menu-address')).toHaveText(walletA.address);
  await page.keyboard.press('Escape');

  // the SIWE message carries the checksummed address and the server matched the recovered signer
  const message = (api.calls.find((c) => c.path === '/siwe/verify')!.body as { message: string }).message;
  expect(message.split('\n')[1]).toBe(walletA.address);
  expect(api.token).toBeTruthy();

  // the quote's sender, and the transaction's `from`, are the same address in the same case
  await page.getByLabel('Amount (ETH)').fill('0.1');
  await expect(page.getByTestId('deposit-btn')).toBeEnabled();
  expect((api.calls.filter((c) => c.path === '/quote').pop()!.body as { sender: string }).sender).toBe(walletA.address);
  await page.getByTestId('deposit-btn').click();
  await expect(page.getByTestId('deposit-timeline')).toBeVisible();
  const state = await handleState(page, 'trust');
  expect(state.sent[0].from).toBe(walletA.address);

  // and a lowercase accountsChanged is still one account switch, not a second wallet
  await call(page, 'trust', 'setAccounts', [walletB.address]);
  await expect(page.getByTestId('connected-address')).toContainText(walletB.address.slice(0, 6));
  await page.getByTestId('connected-address').click();
  await expect(page.getByTestId('account-menu-address')).toHaveText(walletB.address);
  expect(pageErrors).toEqual([]);
});

test('quirk (d) Coin98/Binance: the hex message is refused, the plain string is signed — once', async ({ page }) => {
  const { api, pageErrors } = await boot(page, [profile('coin98global')]);
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  const state = await handleState(page, 'coin98global');
  expect(state.signCalls).toHaveLength(2); // hex (refused with -32602), then the plain string
  expect(String(state.signCalls[0][0])).toMatch(/^0x[0-9a-f]+$/);
  expect(String(state.signCalls[1][0])).toContain('wants you to sign in with your Ethereum account');
  expect(state.signCalls[0][1]).toBe(walletA.address);
  expect(state.signed).toBe(1); // ⛔ one signature, one session — a wallet that signed is never re-asked
  expect(api.calls.filter((c) => c.path === '/siwe/verify')).toHaveLength(1);
  expect(pageErrors).toEqual([]);
});

test('quirk (d) TokenPocket: a wallet that wants [address, message] gets it on the third shape', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('tokenpocket', { inProviders: false, global: 'ethereum' })]);
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  const state = await handleState(page, 'tokenpocket');
  expect(state.signCalls).toHaveLength(3);
  expect(state.signCalls[2][0]).toBe(walletA.address);
  expect(String(state.signCalls[2][1])).toContain('wants you to sign in');
  expect(state.signed).toBe(1);
  expect(pageErrors).toEqual([]);
});

test('quirk (d) MetaMask: a wallet that takes the EIP-191 hex message is asked exactly once', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('metamask', { inProviders: false, global: 'ethereum' })]);
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();
  const state = await handleState(page, 'metamask');
  expect(state.signCalls).toHaveLength(1);
  expect(state.signed).toBe(1);
  expect(pageErrors).toEqual([]);
});

test('quirk (e) OKX: a `disconnect` with the account still there is ignored; with none, it ends', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('okx', { global: 'okxwallet' })]);
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  // an RPC hiccup: injected wallets emit `disconnect` for it, with the account still connected
  await call(page, 'okx', 'hiccup');
  await page.waitForTimeout(400);
  await expect(page.getByTestId('connected-address')).toBeVisible();
  await expect(signedIn(page)).toBeVisible();

  // the same event, with the wallet actually locked: eth_accounts answers [] and the session ends
  await call(page, 'okx', 'lock');
  await call(page, 'okx', 'hiccup');
  await expect(page.getByTestId('connected-address')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Connect wallet' })).toBeVisible();
  expect(pageErrors).toEqual([]);
});

test('quirk (f) a locked wallet answers [] — the picker says to unlock it, and then it works', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('metamask', { inProviders: false, global: 'ethereum', quirks: ['emptyAccounts'] })]);
  const rows = await openPicker(page);
  await rows.first().click();
  const dialog = page.getByRole('dialog', { name: 'Connect a wallet' });
  await expect(dialog).toContainText('Unlock your MetaMask wallet and try again');
  await expect(page.getByTestId('connected-address')).toHaveCount(0);

  await call(page, 'metamask', 'unlock');
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();
  await expect(page.getByTestId('connected-address')).toContainText(walletA.address.slice(0, 6));
  expect(pageErrors).toEqual([]);
});

test('quirk (g) two extensions sharing window.ethereum.providers[] are two rows, each listed once', async ({ page }) => {
  // MetaMask and Coinbase Wallet installed together: window.ethereum is one of them and carries
  // `providers` with both. Both also announce, so each is seen twice and must still be one row.
  const { pageErrors } = await boot(page, [profile('metamask'), profile('coinbase')]);
  const rows = await openPicker(page);
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toHaveAttribute('data-wallet-id', '6963:io.metamask');
  await expect(rows.nth(1)).toHaveAttribute('data-wallet-id', '6963:com.coinbase.wallet');
  // and each connects to its own provider
  await rows.nth(1).click();
  await expect(signedIn(page)).toBeVisible();
  expect((await handleState(page, 'coinbase')).signed).toBe(1);
  expect((await handleState(page, 'metamask')).signed).toBe(0);
  expect(pageErrors).toEqual([]);
});

test('quirk (g) the same array with no EIP-6963 at all is still one row per wallet', async ({ page }) => {
  const { pageErrors } = await boot(page, [
    profile('metamask', { rdns: undefined }),
    profile('coinbase', { rdns: undefined }),
    profile('tokenpocket', { rdns: undefined }),
  ]);
  const rows = await openPicker(page);
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toHaveAttribute('data-wallet-id', 'inj:metamask');
  await expect(rows.nth(1)).toHaveAttribute('data-wallet-id', 'inj:coinbase');
  await expect(rows.nth(2)).toHaveAttribute('data-wallet-id', 'inj:tokenpocket');
  for (const r of [0, 1, 2]) await expect(rows.nth(r)).toHaveAttribute('data-inapp', 'yes');
  expect(pageErrors).toEqual([]);
});

test("quirk (h) in a wallet's own browser the legacy global is the row — no announcement is coming", async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('trust', { rdns: undefined, global: 'ethereum' })], { rpc: true, armed: true });
  const rows = await openPicker(page);
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toHaveAttribute('data-wallet-id', 'inj:trust');
  await expect(rows.first()).toHaveAttribute('data-inapp', 'yes');
  await expect(rows.first()).toContainText('Trust Wallet');
  await expect(page.getByRole('dialog')).not.toContainText('No wallet was detected');

  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();
  await page.getByLabel('Amount (ETH)').fill('0.1');
  await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');
  expect(pageErrors).toEqual([]);
});

test('quirk (h) an announcement that arrives after the grace window replaces the row, never doubles it', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('trust', { global: 'ethereum', announceDelayMs: 3000 })]);
  const rows = await openPicker(page);
  // within the first 3 s only the global exists, and it is the row the user gets
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toHaveAttribute('data-wallet-id', 'inj:trust');
  await expect(rows.first()).toHaveAttribute('data-inapp', 'yes');
  // then Trust announces the same provider: still one row, now the announced one
  await expect(rows.first()).toHaveAttribute('data-wallet-id', '6963:com.trustwallet.app', { timeout: 15_000 });
  await expect(rows).toHaveCount(1);
  expect(pageErrors).toEqual([]);
});

test('WalletConnect stays hidden until a project id is built in', async ({ page }) => {
  // VITE_WALLETCONNECT_PROJECT_ID is empty in every build this suite runs against, so the row would
  // be a button that can only refuse. It is not rendered at all.
  const { pageErrors } = await boot(page, ALL_PROFILES);
  const dialog = page.getByRole('dialog', { name: 'Connect a wallet' });
  await openPicker(page);
  await expect(dialog).not.toContainText('WalletConnect');
  await expect(dialog).not.toContainText('not configured');
  expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// quirk (i) the chain id on the wire — EIP-3326's unpadded form
// ---------------------------------------------------------------------------
//
// The 2026-09-10 bug report, in the admin's words: "When I click Deposit, you ask me to Add ETH
// Network, but I have it." The Deposit button switches the wallet to the source chain, and the id
// was built with ethers' `toBeHex`, which pads a quantity to whole bytes — chain 1 went out as
// "0x01". EIP-695/3085/3326 all want the unpadded "0x1". Wallets that compare the string answered
// 4902, and our own 4902 branch then offered to ADD Ethereum mainnet to a wallet that had it since
// the day it was installed; MetaMask's family refuses "0x01" outright with -32602 "unpadded", so on
// those the Deposit button could not switch chains at all.
//
// The mock did not catch it because it read the parameter with parseInt(), which takes "0x01"
// happily. It no longer does — see the chain-id block in mocks.ts — so the tests in this block are
// the ones that would have failed the day the padding was introduced.

/**
 * Every chain id in CHAIN_META that `toBeHex` would pad, DERIVED at runtime rather than typed out.
 * ethers pads a quantity to whole BYTES, so an id whose hex has an ODD number of nibbles comes back
 * with a leading zero — "0x01", "0x0138de" — and that is the form no wallet accepts as a chainId.
 *
 * It is derived because a hand-written list is a list someone forgets: the first version of this
 * table named five ids and called itself "exactly the ones toBeHex pads", while CHAIN_META already
 * held eight (zkSync Era 324, Soneium 1868 and Berachain 80094 were outside the guard, unlisted and
 * untested). Deriving it means a chain added to CHAIN_META tomorrow is inside the guard the moment
 * it is added. Measured 2026-09-10: 23 chains in CHAIN_META, 8 of them padded by `toBeHex`.
 */
const CHAIN_META_IDS = Object.keys(CHAIN_META).map(Number);
const PADDED_BY_TO_BE_HEX = CHAIN_META_IDS.filter((id) => id.toString(16).length % 2 === 1).sort((a, b) => a - b);

/**
 * The named rows, kept as examples so a red run says WHICH chain instead of "some id", and so the
 * derived set is checked against something a human wrote down. Every row must still be in
 * CHAIN_META — if one is removed from the app, this table has to say so.
 */
const CANONICAL_EXAMPLES: [number, string, string][] = [
  [1, '0x1', '0x01'], // Ethereum — the admin's case
  [10, '0xa', '0x0a'], // OP Mainnet
  [324, '0x144', '0x0144'], // zkSync Era
  [999, '0x3e7', '0x03e7'], // HyperEVM
  [1514, '0x5ea', '0x05ea'], // Story
  [1776, '0x6f0', '0x06f0'], // Injective EVM
  [1868, '0x74c', '0x074c'], // Soneium
  [80094, '0x138de', '0x0138de'], // Berachain
];

test('quirk (i) already on Ethereum: nothing is switched, and no chain is offered to be added', async ({ page }) => {
  const { pageErrors } = await boot(page, [profile('metamask', { inProviders: false, global: 'ethereum', chainId: 1 })], { armed: true });
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  await page.getByLabel('Amount (ETH)').fill('0.1');
  await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');
  await page.getByTestId('deposit-btn').click();
  await expect(page.getByTestId('deposit-timeline')).toBeVisible();

  const state = await handleState(page, 'metamask');
  expect(state.switchCalls).toEqual([]); // the wallet is already there: it is not asked
  expect(state.addCalls).toEqual([]); // ⛔ and it is NEVER offered "Add Ethereum" — the bug report
  await expect(page.locator('body')).not.toContainText('does not know chain');
  expect(state.chainId).toBe(1);
  expect(state.sent).toHaveLength(1);
  expect(state.sent[0]).toMatchObject({ from: walletA.address, to: ETH_PIPE, data: '0xdeadbeef' });
  expect(pageErrors).toEqual([]);
});

test('quirk (i) on another chain: exactly one switch, and the chainId on it is "0x1"', async ({ page }) => {
  // BNB Smart Chain is not a chain the API lists, so "Pay with" stays on Ethereum and the click has
  // a real switch to make. `toBeHex(1)` would have put "0x01" on this wire.
  const { pageErrors } = await boot(page, [profile('metamask', { inProviders: false, global: 'ethereum', chainId: 56 })], { armed: true });
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  await page.getByLabel('Amount (ETH)').fill('0.1');
  await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');
  await page.getByTestId('deposit-btn').click();
  await expect(page.getByTestId('deposit-timeline')).toBeVisible();

  const state = await handleState(page, 'metamask');
  expect(state.switchCalls).toEqual([{ chainId: '0x1' }]); // one call, unpadded, and nothing else
  expect(state.addCalls).toEqual([]);
  expect(state.chainId).toBe(1);
  expect(state.sent).toHaveLength(1);
  expect(state.sent[0]).toMatchObject({ from: walletA.address, to: ETH_PIPE });
  expect(pageErrors).toEqual([]);
});

test('quirk (i) the wallet from the bug report: the canonical id is found, so nothing is added', async ({ page }) => {
  // Same journey on the family that compares the chainId as TEXT. This is the test that fails with
  // "0x01": the string is not in the wallet's list, it answers 4902, and the Add prompt appears.
  const { pageErrors } = await boot(page, [STRICT_CHAIN_PROFILE], { armed: true });
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  await page.getByLabel('Amount (ETH)').fill('0.1');
  await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.1 ETH on Ethereum');
  await page.getByTestId('deposit-btn').click();
  await expect(page.getByTestId('deposit-timeline')).toBeVisible();

  const state = await handleState(page, 'strictchain');
  expect(state.switchCalls).toEqual([{ chainId: '0x1' }]);
  expect(state.addCalls).toEqual([]); // ⛔ "you ask me to Add ETH Network, but I have it"
  expect(state.addedChains).toEqual([]);
  await expect(page.locator('body')).not.toContainText('does not know chain');
  expect(state.chainId).toBe(1);
  expect(state.sent).toHaveLength(1);
  expect(pageErrors).toEqual([]);
});

test('quirk (i) a chain the wallet really does not have: 4902, then one add with the unpadded id', async ({ page }) => {
  // The 4902 branch is not deleted, it is aimed: a wallet holding only Ethereum and BNB, asked for
  // Story (1514 — another id `toBeHex` pads, to "0x05ea"), is told what the chain is and switched.
  const { pageErrors } = await boot(
    page,
    [profile('metamask', { inProviders: false, global: 'ethereum', chainId: 56, knownChains: [1, 56] })],
    { armed: true },
  );
  const rows = await openPicker(page);
  await rows.first().click();
  await expect(signedIn(page)).toBeVisible();

  await page.getByTestId('pay-with').click();
  await page.locator('[data-chain="1514"]').click();
  await page.getByLabel('Amount (IP)').fill('0.2');
  await expect(page.getByTestId('deposit-btn')).toHaveText('Deposit 0.2 IP on Story');
  await page.getByTestId('deposit-btn').click();
  await expect(page.getByTestId('deposit-timeline')).toBeVisible();

  const state = await handleState(page, 'metamask');
  // CHAIN_META[1514], verbatim, with the canonical id — "0x05ea" would have been refused as unpadded
  expect(state.addCalls).toHaveLength(1);
  expect(state.addCalls[0]).toMatchObject({
    chainId: '0x5ea',
    chainName: 'Story',
    nativeCurrency: { name: 'IP', symbol: 'IP', decimals: 18 },
    rpcUrls: ['https://mainnet.storyrpc.io'],
    blockExplorerUrls: ['https://www.storyscan.io'],
  });
  expect(state.switchCalls).toEqual([{ chainId: '0x5ea' }, { chainId: '0x5ea' }]); // refused, added, accepted
  expect(state.addedChains).toEqual([1514]);
  expect(state.chainId).toBe(1514);
  expect(state.sent).toHaveLength(1);
  expect(state.sent[0]).toMatchObject({ to: ROUTER_ORDER, data: '0xdeadbeef' });
  expect(pageErrors).toEqual([]);
});

test('quirk (i) the mock answers a padded id the way the two real families do', async ({ page }) => {
  // The guard on the guard: proof that these tests can fail. Both families are asked for chain 1
  // twice — once in ethers' `toBeHex` form, once in the canonical one — at the provider, no UI.
  await boot(page, [STRICT_CHAIN_PROFILE, profile('metamask', { inProviders: false, global: undefined, chainId: 56 })], { armed: true });
  const ask = (handle: string, chainId: string) => walletCall(page, handle, 'wallet_switchEthereumChain', { chainId });

  // the validating family (MetaMask's RPC middleware): a padded id is bad params, not a chain
  const mmPadded = await ask('metamask', '0x01');
  expect(mmPadded.ok).toBe(false);
  expect(mmPadded.code).toBe(-32602);
  expect(mmPadded.message).toContain('unpadded');
  // the string-comparing family: the id is simply not found — the 4902 that produced the Add prompt
  const strictPadded = await ask('strictchain', '0x01');
  expect(strictPadded.ok).toBe(false);
  expect(strictPadded.code).toBe(4902);
  // and the canonical form is accepted by both, with no add and no complaint
  expect(await ask('metamask', '0x1')).toMatchObject({ ok: true });
  expect(await ask('strictchain', '0x1')).toMatchObject({ ok: true });
  expect((await handleState(page, 'metamask')).addCalls).toEqual([]);
  expect((await handleState(page, 'strictchain')).addCalls).toEqual([]);

  // …and this is the table the fix is against, DERIVED from CHAIN_META so a chain added later
  // cannot silently fall outside it: every id ethers would pad is an id this app must not pad.
  expect(PADDED_BY_TO_BE_HEX.length).toBeGreaterThan(0);
  for (const id of PADDED_BY_TO_BE_HEX) {
    expect(chainIdHex(id)).toBe(`0x${id.toString(16)}`);
    expect(toBeHex(id)).toBe(`0x0${id.toString(16)}`);
    expect(chainIdHex(id)).not.toBe(toBeHex(id)); // the whole bug, in one line, per chain
  }
  // the named rows, so a failure names the chain — and so the derived set cannot quietly shrink
  for (const [id, canonical, padded] of CANONICAL_EXAMPLES) {
    expect(PADDED_BY_TO_BE_HEX).toContain(id);
    expect(chainIdHex(id)).toBe(canonical);
    expect(toBeHex(id)).toBe(padded);
  }
  // every other chain in CHAIN_META: the two forms agree, which is exactly why the bug hid so long
  for (const id of CHAIN_META_IDS.filter((id) => !PADDED_BY_TO_BE_HEX.includes(id))) {
    expect(chainIdHex(id)).toBe(toBeHex(id));
  }
});

test('quirk (i) wallet_addEthereumChain refuses a padded id too — and the string family adds a second Ethereum', async ({ page }) => {
  // The other half of the same guard, and until now the untested half. EIP-3085's `chainId` is the
  // same string EIP-3326's is, and the validating family checks it on the ADD as well — but every
  // test above reaches the add through the app, which sends the canonical form, so the -32602 branch
  // in `wallet_addEthereumChain` could have been deleted with the whole suite still green.
  const { pageErrors } = await boot(
    page,
    [STRICT_CHAIN_PROFILE, profile('metamask', { inProviders: false, global: undefined, chainId: 56 })],
    { armed: true },
  );
  // CHAIN_META[1] verbatim, exactly as the app's own 4902 branch would send it, with only the id
  // padded — so the refusal below can be about nothing except the padding.
  const addChain = (handle: string, chainId: string) => walletCall(page, handle, 'wallet_addEthereumChain', { chainId, ...CHAIN_META[1] });

  // the validating family: "0x01" is bad params on the add exactly as it is on the switch…
  const mmPadded = await addChain('metamask', '0x01');
  expect(mmPadded.ok).toBe(false);
  expect(mmPadded.code).toBe(-32602);
  expect(mmPadded.message).toContain('unpadded');
  // …and NO chain was added: the attempt is recorded, the wallet's own list is untouched
  let mm = await handleState(page, 'metamask');
  expect(mm.addCalls).toHaveLength(1);
  expect(mm.addedChains).toEqual([]);
  expect(mm.addedChainStrings).toEqual([]);
  // the canonical form on the same wallet with the same params is accepted — padding was the fault
  expect(await addChain('metamask', '0x1')).toMatchObject({ ok: true });
  mm = await handleState(page, 'metamask');
  expect(mm.addedChains).toEqual([1]);
  expect(mm.addedChainStrings).toEqual(['0x1']);

  // the string-comparing family — the admin's — never validates, so it refuses the padded id one
  // step later, at the switch, with 4902 and nothing added. That 4902 is what made our own branch
  // offer to ADD a chain the wallet had had since it was installed.
  const strictPadded = await walletCall(page, 'strictchain', 'wallet_switchEthereumChain', { chainId: '0x01' });
  expect(strictPadded.ok).toBe(false);
  expect(strictPadded.code).toBe(4902);
  let strict = await handleState(page, 'strictchain');
  expect(strict.addCalls).toEqual([]);
  expect(strict.addedChains).toEqual([]);

  // …and this is what accepting that prompt costs, which is why the padded form must never leave
  // this app: the add is not refused, it is remembered as TEXT, and the wallet now holds Ethereum
  // twice — once as "0x1" (knownChains) and again as "0x01".
  expect(await addChain('strictchain', '0x01')).toMatchObject({ ok: true });
  strict = await handleState(page, 'strictchain');
  expect(strict.addedChains).toEqual([1]);
  expect(strict.addedChainStrings).toEqual(['0x01']);
  expect(chainIdHex(1)).not.toBe('0x01'); // the one line that keeps this journey unreachable

  expect(pageErrors).toEqual([]);
});

// ---------------------------------------------------------------------------
// every wallet at once — the picker screenshot
// ---------------------------------------------------------------------------

/**
 * Twelve installs, eleven rows. The eight announcements are eight rows; of the four legacy globals,
 * Binance, Bitget and TokenPocket are three more, and `window.coin98.provider` is NOT a twelfth —
 * Coin98 both announces (coin98.com) and sets its global, and one wallet is one row. That collapse
 * is the point of the test, not an omission: the picker's job is to list wallets, not sightings.
 */
const EXPECTED_ROWS = [
  ['6963:io.metamask', 'MetaMask'],
  ['6963:io.zerion.wallet', 'Zerion'],
  ['6963:io.rabby', 'Rabby Wallet'],
  ['6963:com.trustwallet.app', 'Trust Wallet'],
  ['6963:com.okex.wallet', 'OKX Wallet'],
  ['6963:com.coinbase.wallet', 'Coinbase Wallet'],
  ['6963:app.phantom', 'Phantom'],
  ['6963:coin98.com', 'Coin98 Wallet'],
  ['inj:tokenpocket', 'TokenPocket'],
  ['inj:binance', 'Binance Web3 Wallet'],
  ['inj:bitget', 'Bitget Wallet'],
];

test('the picker with all twelve installed: eight announced, three legacy-only, Coin98 counted once', async ({ page }) => {
  const { pageErrors } = await boot(page, ALL_PROFILES);
  const rows = await openPicker(page);
  await expect(rows).toHaveCount(EXPECTED_ROWS.length);
  for (const [i, [id, name]] of EXPECTED_ROWS.entries()) {
    await expect(rows.nth(i)).toHaveAttribute('data-wallet-id', id);
    await expect(rows.nth(i)).toContainText(name);
  }
  expect(ANNOUNCED_PROFILES).toHaveLength(8);
  expect(LEGACY_PROFILES).toHaveLength(4);
  expect(pageErrors).toEqual([]);
});

const PICKER_SHOTS: { name: string; viewport: { width: number; height: number }; isMobile: boolean }[] = [
  { name: 'desktop', viewport: { width: 1280, height: 900 }, isMobile: false },
  { name: 'mobile', viewport: { width: 390, height: 844 }, isMobile: true },
];

for (const vp of PICKER_SHOTS) {
  test(`screenshot: the wallet picker with every wallet installed (${vp.name})`, async ({ browser }: { browser: Browser }) => {
    const ctx = await browser.newContext({
      viewport: vp.viewport,
      isMobile: vp.isMobile,
      hasTouch: vp.isMobile,
      deviceScaleFactor: vp.isMobile ? 2 : 1,
    });
    const page = await ctx.newPage();
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    const api = new MockApi();
    await blockExternal(page, true);
    await api.install(page);
    await installWallets(page, ALL_PROFILES);
    await page.goto('/');
    const rows = await openPicker(page);
    await expect(rows).toHaveCount(EXPECTED_ROWS.length);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `e2e/screenshots/wallet-picker-${vp.name}.png` });
    expect(errors).toEqual([]);
    await ctx.close();
  });
}
