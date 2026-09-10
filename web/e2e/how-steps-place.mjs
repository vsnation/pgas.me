// Place the T44 step pictures: e2e/screenshots/how-step-*.png  →  the two places that serve them.
//
//   node e2e/how-steps-place.mjs        # after `npx playwright test e2e/how-steps.spec.ts`
//
//   web/public/how/step-<n>.png         the site (light) — Vite copies public/ into dist verbatim
//   web/public/how/step-<n>-dark.png    the site (dark) — the page swaps these in by theme
//   ../docs/how-step-<n>.png            the public README (light), mirrored by publish.sh
//
// Why a separate step rather than writing them from the spec: `npx playwright test` BUILDS the
// bundle before it runs, so anything a spec writes into public/ is a build input the running build
// has already left behind — the gate would be green against files nobody served. And the size
// budget needs a tool a browser does not have.
//
// Budget: ≤ 120 KB each. Quantising a flat-UI screenshot to a 256-colour palette is what buys it
// (136 KB → 59 KB on step 1) and costs nothing visible; the wide payouts table is also scaled to
// 1400 px, which is still above the ~1120 px it is ever displayed at.
//
// ⛔ `-dither None`, NOT Floyd-Steinberg. Dithering these pictures is not a smaller file, it is a
// DIFFERENT PRODUCT: on the first pass the app's two near-white tints — the grey #EEF1F6 of an
// inactive step and the mint #E6F4F3 of the current one — both landed on #EEFFFF, so the "you are
// here" highlight vanished and every card came out faintly cyan. A screenshot that has been
// recoloured is not evidence of anything. Flat UI has no gradients to dither anyway; the cost of
// turning it off is ~10 KB.
//
// And the recolouring is CHECKED rather than trusted: every output is compared with an unquantised
// reference of the same size, and anything past a small RMSE is refused. This script refuses on
// size too — a picture that quietly grew to 400 KB is a page that quietly got slower.
import { execFileSync } from 'node:child_process';
import { mkdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = `${WEB}/e2e/screenshots`;
const SITE = `${WEB}/public/how`;
const DOCS = resolve(WEB, '../docs');
const LIMIT = 120 * 1024;
/**
 * Palette sizes to try, largest first: the first one that fits the budget AND stays inside MAX_RMSE
 * wins. It is a ladder rather than one number because the compressed size of an indexed PNG is not
 * monotonic in anything you can predict — the dark payouts table came out 171 KB at 256 colours and
 * 72 KB at 192, for the same picture. Guessing a constant means either a picture that is refused for
 * being 2 KB over or one that is quietly uglier than it needed to be.
 */
const PALETTES = [256, 224, 192, 160, 128, 96];
/**
 * Step 7 is the desktop-width payouts table (2192 px captured); everything else is already
 * 1040–1072 px and is not resized at all. 1280 is the measured landing point: it is still wider
 * than the ~1040 px the page ever draws it at, and both themes come in under budget AND inside
 * MAX_RMSE there, which 1400 and 1200 do not — a table of small text on a dark ground runs out of
 * 256 colours before it runs out of pixels.
 */
const WIDTH = { 7: 1280 };

mkdirSync(SITE, { recursive: true });
mkdirSync(DOCS, { recursive: true });

const kb = (p) => Math.round(statSync(p).size / 102.4) / 10;

/** How far the palette may move the picture, as ImageMagick's normalised RMSE (1 = black vs white). */
const MAX_RMSE = 0.01;

/**
 * `magick compare` EXITS 1 WHEN THE IMAGES DIFFER — which they always do a little, that being the
 * whole measurement — so a bare execFileSync throws on every successful comparison. The number is
 * on stderr either way; only an exit past 1 is a real failure.
 */
function rmse(a, b) {
  let measured = '';
  try {
    measured = execFileSync('magick', ['compare', '-metric', 'RMSE', a, b, 'null:'], { stdio: ['ignore', 'pipe', 'pipe'] }).toString();
  } catch (e) {
    if ((e.status ?? 2) > 1) throw e;
    measured = (e.stderr ?? '').toString();
  }
  const normalised = Number((measured.match(/\(([\d.eE-]+)\)/) ?? [])[1]);
  if (!Number.isFinite(normalised)) throw new Error(`could not read an RMSE from "${measured.trim()}"`);
  return normalised;
}

function shrink(from, to, width) {
  const resize = width ? ['-resize', `${width}x`] : [];
  // the same picture at the same size with every colour intact — what `to` is allowed to look like
  const ref = `${to}.ref.png`;
  execFileSync('magick', [from, ...resize, '-strip', ref]);
  try {
    let last = '';
    for (const colors of PALETTES) {
      execFileSync('magick', [ref, '-colors', String(colors), '-dither', 'None', `PNG8:${to}`]);
      const size = statSync(to).size;
      const moved = rmse(ref, to);
      last = `${kb(to)} KB at ${colors} colours, RMSE ${moved.toFixed(5)}`;
      if (size <= LIMIT && moved <= MAX_RMSE) return { size, rmse: moved, colors };
      if (moved > MAX_RMSE) throw new Error(`${to}: ${colors} colours moved the picture (RMSE ${moved}, limit ${MAX_RMSE})`);
    }
    throw new Error(`${to}: still over the ${LIMIT / 1024} KB budget at the smallest palette (${last})`);
  } finally {
    rmSync(ref, { force: true });
  }
}

const rows = [];
const sizes = [];
for (let n = 1; n <= 7; n++) {
  for (const dark of [false, true]) {
    const suffix = dark ? '-dark' : '';
    const from = `${SRC}/how-step-${n}${suffix}.png`;
    const to = `${SITE}/step-${n}${suffix}.png`;
    shrink(from, to, WIDTH[n]);
    rows.push([`step-${n}${suffix}.png`, `${kb(from)} KB`, `${kb(to)} KB`, 'public/how']);
    if (!dark) {
      const doc = `${DOCS}/how-step-${n}.png`;
      shrink(from, doc, WIDTH[n]);
      rows.push([`how-step-${n}.png`, `${kb(from)} KB`, `${kb(doc)} KB`, 'docs']);
      const [w, h] = execFileSync('magick', ['identify', '-format', '%w %h', to]).toString().split(' ');
      sizes.push(`  ${n}: { w: ${Number(w)}, h: ${Number(h)} },`);
    }
  }
}

/**
 * The page reserves each picture's space before it loads (they are all lazy), and the only honest
 * source for those two numbers is the file itself — so the thing that WRITES the file states them.
 * A hand-kept copy would go stale the first time a card grew a line.
 */
writeFileSync(
  `${WEB}/src/pages/how-shots.ts`,
  [
    '// GENERATED by e2e/how-steps-place.mjs — do not edit by hand.',
    '//',
    '// The intrinsic size of every step picture in public/how/, so the page can reserve its box',
    '// before a lazy image arrives. The dark twin of each step is the same size (it is the same',
    '// card, captured twice). Regenerate with `node e2e/how-steps-place.mjs`.',
    'export const HOW_SHOTS: Record<number, { w: number; h: number }> = {',
    ...sizes,
    '};',
    '',
  ].join('\n'),
);

for (const r of rows) console.log(r[0].padEnd(24), r[1].padStart(9), '→', r[2].padStart(9), ' ', r[3]);
console.log(`\n${rows.length} files, all under ${LIMIT / 1024} KB. src/pages/how-shots.ts written.`);
