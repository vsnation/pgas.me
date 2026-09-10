// T44 — the `/how-it-works` page: it renders, every step is a picture AND words, the pictures
// actually load and swap with the theme, the privacy section keeps its limits, and the page's own
// title/description reach a crawler that never runs the bundle.
//
// The last one is the drift guard that matters. Those two strings exist twice on purpose — in
// `src/pages/HowItWorks.tsx` for a client-side navigation and in `index.html` for the social-preview
// crawler that is handed the SPA fallback — and two copies of one fact disagree unless something
// checks. This spec reads the SERVED HTML and compares it with the RENDERED page.
import { expect, test, type Page } from '@playwright/test';
import { MockApi, blockExternal, connectAndSignIn, installMockWallet } from './mocks';

const TITLE = 'How Pgas.me works — every step, with screenshots';
const STEPS = 7;

let pageErrors: string[];

async function boot(page: Page, path = '/how-it-works') {
  pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await blockExternal(page, true);
  await new MockApi().install(page);
  await installMockWallet(page);
  await page.goto(path);
}

test.describe('/how-it-works', () => {
  test('renders seven steps, each with a picture that loads and words that explain it', async ({ page }) => {
    await boot(page);
    await expect(page.getByTestId('how-page')).toBeVisible();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('How Pgas.me works');

    const steps = page.getByTestId('how-step');
    await expect(steps).toHaveCount(STEPS);
    for (let i = 0; i < STEPS; i++) {
      const step = steps.nth(i);
      await expect(step.locator('h2')).toHaveCount(1);
      // two or more sentences of instruction, not a caption
      const words = (await step.locator('.how-step-text').innerText()).split(/\s+/).length;
      expect(words).toBeGreaterThan(40);
      const img = step.locator('img');
      await expect(img).toHaveCount(1);
      // lazy, and described — a picture of the product with no alt text is a picture of nothing
      await expect(img).toHaveAttribute('loading', 'lazy');
      const alt = await img.getAttribute('alt');
      expect((alt ?? '').length).toBeGreaterThan(60);
      // and it is a file that EXISTS: scrolled into view, decoded, with real pixels. `loading`
      // and a src that 404s look identical in the DOM.
      await img.scrollIntoViewIfNeeded();
      await expect(async () => {
        const w = await img.evaluate((el: HTMLImageElement) => (el.complete ? el.naturalWidth : 0));
        expect(w).toBeGreaterThan(500);
      }).toPass({ timeout: 15_000 });
    }
    expect(pageErrors).toEqual([]);
  });

  test('the pictures follow the theme', async ({ page }) => {
    await boot(page);
    const first = page.getByTestId('how-shot-1').locator('img');
    await expect(first).toHaveAttribute('src', '/how/step-1.png');
    await page.getByTestId('theme-toggle').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(first).toHaveAttribute('src', '/how/step-1-dark.png');
    await page.getByTestId('theme-toggle').click();
    await expect(first).toHaveAttribute('src', '/how/step-1.png');
    expect(pageErrors).toEqual([]);
  });

  test('the dark theme is served from the same page, top to bottom', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await boot(page);
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(page.getByTestId('how-shot-7').locator('img')).toHaveAttribute('src', '/how/step-7-dark.png');
    await expect(page.getByTestId('how-privacy')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('on a phone the page still reads, and the footer is the way to it', async ({ browser }) => {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
    const page = await ctx.newPage();
    await boot(page, '/');
    // the header's nav is hidden below 860 px — the footer link is what a thumb has
    await expect(page.getByTestId('nav-how')).toBeHidden();
    await page.getByTestId('footer-how').click();
    await expect(page).toHaveURL(/\/how-it-works$/);
    await expect(page.getByTestId('how-step')).toHaveCount(STEPS);
    // nothing overflows sideways: a page that scrolls horizontally on a phone is a broken page
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    expect(pageErrors).toEqual([]);
    await ctx.close();
  });

  test('says what the mechanism does AND what it does not — with no absolute claims', async ({ page }) => {
    await boot(page);
    const gives = page.getByTestId('privacy-gives');
    await expect(gives.locator('li')).toHaveCount(5);
    await expect(gives).toContainText('no per-address balances');
    await expect(gives).toContainText('its own receiver key');
    await expect(gives).toContainText('fresh Beam address');

    const limits = page.getByTestId('privacy-limits');
    await expect(limits.locator('li')).toHaveCount(6);
    await expect(limits).toContainText('bridge-funded');
    await expect(limits).toContainText('custodial');
    await expect(limits).toContainText('bridge relayer');
    await expect(limits).toContainText('not shielded today');
    await expect(limits).toContainText('linkable to each other');
    await expect(limits).toContainText('Amounts and timing');

    /**
     * The playbook's banned register, checked on the RENDERED page rather than on the source: a
     * word that reaches a reader is a word this product said, wherever in the tree it was typed.
     * The absolutes matter most on this page — the mechanism is a confidential ledger and a fresh
     * address per order, and it is described, never promised.
     *
     * ⛔ STEMS, NOT WHOLE WORDS, AND THAT IS NOT TIDINESS. The publish gate greps `src` and `e2e`
     * for those very words and must find none, so a test that spelled them out would be the thing
     * that failed the gate it exists to protect. The stems are also the stronger check: they catch
     * every ending the words have.
     */
    const body = (await page.locator('body').innerText()).toLowerCase();
    for (const banned of ['anonym', 'untrace', 'guarant', 'cannot be traced', 'no one can', 'completely private', 'impossible to']) {
      expect(body).not.toContain(banned);
    }
    expect(pageErrors).toEqual([]);
  });

  test('answers the money questions, and the fee answer matches the app', async ({ page }) => {
    await boot(page);
    const faq = page.getByTestId('how-faq');
    await expect(faq.locator('dt')).toHaveCount(7);
    await expect(faq).toContainText('Deposits are free');
    await expect(faq).toContainText('0.002 ETH-equivalent');
    await expect(faq).toContainText('2 %');
    await expect(faq).toContainText('bridge fee at cost');
    await expect(faq).toContainText('the only floor is technical');
    await expect(faq).toContainText('66 minutes');
    await expect(faq).toContainText('4–18 hours');
    await expect(faq).toContainText('delayed');
    // cancel: yes, and the page says exactly when it stops being possible
    await expect(faq).toContainText('before it has been handed to the bridge');
    expect(pageErrors).toEqual([]);
  });

  test('every way in leads here, and the page leads back', async ({ page }) => {
    await boot(page, '/');
    // the header link (desktop)
    await page.getByTestId('nav-how').click();
    await expect(page).toHaveURL(/\/how-it-works$/);
    await expect(page.getByTestId('nav-how')).toHaveAttribute('aria-current', 'page');

    // the "What is Pgas.me" card
    await page.goBack();
    await page.getByTestId('explainer-how').click();
    await expect(page).toHaveURL(/\/how-it-works$/);

    // and the modal behind each page's "How it works" link
    await page.goBack();
    await connectAndSignIn(page);
    await page.getByTestId('how-it-works').click();
    await page.getByTestId('how-it-works-more').click();
    await expect(page.getByTestId('how-it-works-modal')).toHaveCount(0);
    await expect(page.getByTestId('how-page')).toBeVisible();

    // the way back out is the point of the page
    await page.getByTestId('how-start').click();
    await expect(page).toHaveURL(/127\.0\.0\.1:4173\/$/);
    expect(pageErrors).toEqual([]);
  });

  test('the deep link carries its own title and description — in the HTML, not only in the bundle', async ({ page }) => {
    await boot(page);
    await expect(page).toHaveTitle(TITLE);
    const description = await page.locator('meta[name="description"]').getAttribute('content');
    const canonical = await page.locator('link[rel="canonical"]').getAttribute('href');
    expect(canonical).toBe('https://pgas.me/how-it-works');
    expect(await page.locator('meta[property="og:title"]').getAttribute('content')).toBe(TITLE);
    expect(await page.locator('meta[property="og:url"]').getAttribute('content')).toBe('https://pgas.me/how-it-works');
    expect(await page.locator('meta[property="og:description"]').getAttribute('content')).toBe(description);

    // the served bytes, with no bundle run at all — what a preview crawler is handed
    const served = await (await page.request.get('/how-it-works')).text();
    expect(served).toContain(TITLE);
    expect(served).toContain(description ?? 'no description');

    // and leaving the page puts the site's own title back
    await page.getByTestId('how-start').click();
    await expect(page).toHaveTitle('Pgas.me — Private gas for fresh EVM wallets');
    expect(pageErrors).toEqual([]);
  });

  test('it is in the sitemap, and /admin still is not', async ({ page }) => {
    const xml = await (await page.request.get('/sitemap.xml')).text();
    expect(xml).toContain('<loc>https://pgas.me/how-it-works</loc>');
    expect(xml).not.toContain('/admin');
  });
});
