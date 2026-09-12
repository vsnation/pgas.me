// `/how-it-works` — the whole product in seven steps, each one a screenshot of the real app, then
// the privacy mechanism with its limits, then the questions people actually ask (T44, 2026-09-10).
//
// Admin: "You need to provide How it works with small screenshots and instructions of each step and
// how it supports privacy, as Beam is a confidential chain."
//
// Not to be confused with `components/HowItWorks.tsx`, which is the six-line modal behind the "How
// it works" link on each page's intro. That panel answers "where does my money go?" in one screen;
// this page is the long form, and the panel links to it.
//
// ⛔ THE PRIVACY SECTION IS NOT MARKETING COPY. Two rules govern every sentence in it:
//
//   1. Never promise what the mechanism cannot deliver. The absolutes are banned outright: a
//      system with a custodian, a single bridge relayer and a shielded pool it does not use
//      cannot promise that nothing is ever traceable, and one such claim would make every true
//      sentence beside it worthless. "Private by design", the mechanism, then the limits, in the
//      product's own voice.
//   2. The limits come from `Pgas.me Scheme.html` § "Who can see what", which is the measured
//      statement per observer and per stage. If that table changes, this section changes with it.
//      In particular: deposits are NOT shielded today (the treasury decision of 2026-09-10 — a
//      max-privacy output's only reliable exit is a 72-hour timer, and the pool is ~28k outputs
//      against the 65,536 a full-size proof wants), so this page says fresh per-order address and
//      confidential ledger, and does not claim a shielded set.
//
// The pictures come from `e2e/how-steps.spec.ts` and are placed by `e2e/how-steps-place.mjs`. Their
// sizes live in `how-shots.ts`, which that script generates — one writer for the one fact.
import { useEffect, type ReactNode } from 'react';
import { useStore } from '../state/store';
import { HOW_SHOTS } from './how-shots';

const TITLE = 'How Pgas.me works — every step, with screenshots';
const DESCRIPTION =
  'Connect a wallet, pay with anything you already hold on any chain, and schedule ETH into fresh wallets at the times you choose. Seven steps in the real screens, what the privacy mechanism does, and what it does not.';
const URL = 'https://pgas.me/how-it-works';

declare global {
  interface Window {
    /**
     * What `index.html` replaced when it recognised this path, keyed `selector|attribute` (plus
     * `title`). It exists ONLY on a deep-link load, and it is the reason the restore below is
     * correct: that inline script runs before this bundle does, so "the value that was there when
     * the component mounted" is already this page's own — restoring it would leave the whole site
     * titled "How Pgas.me works" after one click.
     */
    __PGAS_META_DEFAULTS__?: Record<string, string | null>;
  }
}

/**
 * Give this page its own title, description and social preview, and put the site's back on the way
 * out. A crawler that does not run JavaScript gets the same two strings from the inline script in
 * `index.html` — the SPA fallback serves that file for every path — and this page's e2e spec reads
 * the served HTML and compares it with the rendered page, so the two copies cannot drift.
 */
function applyMeta(): () => void {
  const undo: (() => void)[] = [];
  const defaults = window.__PGAS_META_DEFAULTS__;
  const attr = (selector: string, name: string, value: string) => {
    const el = document.querySelector(selector);
    if (!el) return;
    const key = `${selector}|${name}`;
    const was = defaults && key in defaults ? defaults[key] : el.getAttribute(name);
    el.setAttribute(name, value);
    undo.push(() => (was === null ? el.removeAttribute(name) : el.setAttribute(name, was)));
  };
  const wasTitle = defaults?.title ?? document.title;
  document.title = TITLE;
  undo.push(() => {
    document.title = wasTitle;
  });
  attr('meta[name="description"]', 'content', DESCRIPTION);
  attr('link[rel="canonical"]', 'href', URL);
  attr('meta[property="og:title"]', 'content', TITLE);
  attr('meta[property="og:description"]', 'content', DESCRIPTION);
  attr('meta[property="og:url"]', 'content', URL);
  attr('meta[name="twitter:title"]', 'content', TITLE);
  attr('meta[name="twitter:description"]', 'content', DESCRIPTION);
  return () => undo.forEach((f) => f());
}

interface Step {
  n: number;
  title: string;
  body: ReactNode;
  alt: string;
  /**
   * The Payouts table is seven columns and 1013 px of content — at 520 px it shows three of them
   * and scrolls the rest. That one picture gets the full column instead of being shrunk into
   * illegibility; every other step is a card at its own narrow width.
   */
  wide?: boolean;
}

const STEPS: Step[] = [
  {
    n: 1,
    title: 'Connect a wallet',
    body: (
      <>
        <p>
          Any EVM wallet: a browser extension, a mobile wallet over WalletConnect, or the browser inside your wallet app. You sign one
          message to prove the address is yours — it is a signature, not a transaction, so it costs nothing and moves nothing.
        </p>
        <p>
          There is no email, no password and nothing to create. The wallet <span className="strong">is</span> the account, which also means
          whoever controls that wallet controls the balance.
        </p>
      </>
    ),
    alt: 'The Pgas.me home page before a wallet is connected: a card headed “What is Pgas.me”, the three steps Deposit, Balance and Schedule, and a “Connect your wallet to start” button beside the line “Signing in is free and moves nothing”.',
  },
  {
    n: 2,
    title: 'Pick what you pay with',
    body: (
      <>
        <p>
          Pgas.me reads what your wallet holds across every supported chain and lists it, priced, so you tap a holding instead of hunting
          for a contract address. Choose what you pay with, and what your balance is kept in — ETH, DAI or WBTC on Ethereum.
        </p>
        <p>
          Type an amount and the quote appears: what will land in your balance, roughly how long it takes, and what the bridge charges. A
          deposit carries no Pgas.me fee at all. The minimum is 0.002 ETH-equivalent.
        </p>
      </>
    ),
    alt: 'The “What to deposit” card — a “You pay” panel above a “You receive on Ethereum” one — with the pay-with list open over it, showing the wallet’s own balances grouped by chain: WBTC, USDC, ETH and DAI on Ethereum, then IP on Story, each with its amount and its dollar value.',
  },
  {
    n: 3,
    title: 'Deposit — one transaction',
    body: (
      <>
        <p>
          Paying from another chain, one cross-chain order carries the value to Ethereum and into the Beam bridge as part of the same fill:
          you sign once, and there is never a second transaction to send.
        </p>
        <p>
          If you already hold ETH, DAI or WBTC on Ethereum it is a direct deposit into that asset&rsquo;s bridge contract — also one
          transaction. The Uniswap V4 route, where it is switched on, is two steps in your own wallet instead — a swap, then a deposit of
          exactly what arrived — plus one-off approvals the first time you use a token.
        </p>
      </>
    ),
    alt: 'The quote: “You pay 0.1 ETH on Arbitrum, about $400” above “You receive on Ethereum 0.098” with ETH, DAI and WBTC to choose from; then “What this costs” — route “Cross-chain to Ethereum”, “Our fee 2 % at withdrawal, not here”, “Lands in your balance in about 7 min · bridge fee $0.40” — and a “Deposit 0.1 ETH on Arbitrum” button.',
  },
  {
    n: 4,
    title: 'Watch it cross',
    body: (
      <>
        <p>
          The status card follows the deposit — submitted, order filled, bridging, confirming (Ethereum&rsquo;s twelve), credited — and
          links your payment and the bridge transaction on the explorers.
        </p>
        <p>
          A cross-chain deposit is usually credited in about five minutes and a direct one in four to fourteen. A busy relayer can stretch
          that, and the card keeps saying where the money is rather than going quiet. A transaction no node can see yet is not lost either:
          Pgas.me keeps checking and picks it up by itself.
        </p>
      </>
    ),
    alt: 'The “Deposit status” card: Submitted, Order filled and Bridging ticked off, “Confirming (7/12)” in progress and Credited still to come, with links to your payment and to the bridge transaction.',
  },
  {
    n: 5,
    title: 'Your balance on Beam',
    body: (
      <>
        <p>
          Four numbers, always on screen. <span className="strong">Arriving</span> is deposits still crossing;{' '}
          <span className="strong">Available</span> is what you can schedule right now; <span className="strong">Scheduled</span> is
          reserved for orders you have already placed, fees included; <span className="strong">Paid out</span> is what has landed in your
          wallets.
        </p>
        <p>
          Each one is a sum over an append-only ledger rather than a stored figure, and the balance belongs to the wallet you signed in with
          — so it is the same balance from any device that connects it.
        </p>
      </>
    ),
    alt: 'The ETH balance tiles: Arriving 0.049 ETH ($196), Available 0.50 ETH ($2,000), Scheduled 0.102 ETH ($408) marked “incl. 2 % fee + bridge fee”, and Paid out 0.01 ETH ($40).',
  },
  {
    n: 6,
    title: 'Schedule the payouts',
    body: (
      <>
        <p>
          List the wallets you want funded — paste <span className="mono">address,amount</span> lines or add rows by hand. Any amount, and a
          delivery time per row: ASAP, in two hours, tonight, tomorrow, or a date and time you type.
        </p>
        <p>
          Every row prices itself from the server: what the wallet receives, our 2 % and the bridge fee at cost. When your balance covers
          them the fees go on top and the wallet receives exactly what you typed; when it does not they come out of the amount and the row
          says so. Nothing is signed here and no wallet is connected — a destination is just an address, checked for its checksum and for
          contract code.
        </p>
      </>
    ),
    alt: 'The “Orders to schedule” card: “From your balance 0.071804 ETH, debited when you press Schedule”, then two wallet rows — 0x3C44…93BC for 0.05 ETH ASAP and 0x3A3a…a3a for 0.02 ETH “Tonight 03:00” — each showing when it goes to the bridge, what the wallet receives, what the balance is debited, and the two fees.',
  },
  {
    n: 7,
    title: 'Track it to the wallet',
    wide: true,
    body: (
      <>
        <p>
          Deposits and payouts share one timeline, newest first, each with its status: scheduled, releasing, bridging, delivering, sent. Tap
          a row and it opens — the wallet, the delivery time, when it goes to the bridge, the fee, and the links. An order is never marked
          failed for a problem on our side: it becomes <span className="strong">delayed</span>, with the reason in plain words and the next
          attempt, and the money stays reserved until it goes.
        </p>
        <p>
          While a payout is crossing you can follow it on the bridge explorer; once it lands, the row links the transaction that delivered
          it. You can cancel an order while it is still ours to stop, and the amount and both fees go straight back to Available. What the
          new wallet sees at the end of all this is an ordinary inbound payment from the bridge contract.
        </p>
      </>
    ),
    alt: 'The “Deposits and payouts” timeline: a scheduled payout, one bridging at 43 of 61 Beam confirmations with its detail open — wallet, delivery time, release time, fee and a “track on the bridge explorer” link — one delayed with the reason and the next attempt, one sent with its delivery transaction, and three deposits below them.',
  },
];

const FAQ: { q: string; a: ReactNode }[] = [
  {
    q: 'What does it cost?',
    a: (
      <>
        Deposits are free. A payout is 2 % (ours) plus the bridge fee at cost — quoted live at the moment you ask for it and shown as its
        own line, around $0.10–$0.20 today. Nothing is hidden inside the percentage.
      </>
    ),
  },
  {
    q: 'Is there a minimum?',
    a: (
      <>
        A deposit has one: <span className="strong">0.002 ETH-equivalent</span>. A payout does not — the only floor is technical, one unit
        of the asset&rsquo;s eight-decimal grid. That is why the bridge fee is charged explicitly instead of being buried in a larger
        minimum.
      </>
    ),
  },
  {
    q: 'How long does it take?',
    a: (
      <>
        A deposit is credited in about five minutes cross-chain, four to fourteen minutes direct from Ethereum. A payout goes to the bridge
        about 66 minutes before your delivery time and usually arrives then; the relayer&rsquo;s queue has a tail that can run 4–18 hours,
        and the row&rsquo;s arrival estimate says so rather than pretending otherwise.
      </>
    ),
  },
  {
    q: 'What if the bridge is slow?',
    a: (
      <>
        The order goes <span className="strong">delayed</span>: the reason in plain words, the time of the next attempt, a fresh arrival
        estimate — and the money stays reserved while it is retried. Nothing is marked failed because of a problem on our side, and nothing
        is quietly dropped.
      </>
    ),
  },
  {
    q: 'Can I cancel?',
    a: (
      <>
        Yes, while an order is still <span className="strong">scheduled</span> or <span className="strong">delayed</span> — that is, before
        it has been handed to the bridge. The amount and both fees go straight back to Available. Once it is releasing or bridging it is out
        of our hands, and the page says which state it is in.
      </>
    ),
  },
  {
    q: 'Which chains can I use?',
    a: (
      <>
        You can pay from any chain the deposit page lists, with anything your wallet holds there. Payouts are ETH on Ethereum today; other
        chains and assets are built and will be named here when they are switched on.
      </>
    ),
  },
  {
    q: 'Do I need an account?',
    a: (
      <>
        No — the wallet is the account, and the same wallet reaches the same balance from any device. In this version Pgas.me is the
        custodian of the balance between your deposit and your payouts; the section above says exactly what that means.
      </>
    ),
  },
];

function Shot({ step }: { step: Step }) {
  const { theme } = useStore();
  const size = HOW_SHOTS[step.n];
  const src = `/how/step-${step.n}${theme.theme === 'dark' ? '-dark' : ''}.png`;
  return (
    <figure className="how-shot" data-testid={`how-shot-${step.n}`}>
      <img src={src} alt={step.alt} width={size.w} height={size.h} loading="lazy" decoding="async" />
    </figure>
  );
}

export function HowItWorksPage() {
  const { route } = useStore();
  useEffect(() => applyMeta(), []);
  return (
    <div className="page" data-testid="how-page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>How Pgas.me works</h1>
          <p className="muted">
            Seven steps, in the screens you will actually see. Every picture below is this app, captured by its own end-to-end tests — the
            balances, addresses and times in them are that suite&rsquo;s example data, not anybody&rsquo;s account.
          </p>
        </div>
        <button type="button" className="btn btn-primary" data-testid="how-start" onClick={() => route.navigate('money')}>
          Start a deposit →
        </button>
      </div>

      <ol className="how-steps" data-testid="how-steps">
        {STEPS.map((s) => (
          <li key={s.n} className={`card how-step${s.wide ? ' how-step-wide' : ''}`} data-step={s.n} data-testid="how-step">
            <div className="how-step-text">
              <div className="how-step-head">
                <span className="n" aria-hidden="true">
                  {s.n}
                </span>
                <h2>{s.title}</h2>
              </div>
              {s.body}
            </div>
            <Shot step={s} />
          </li>
        ))}
      </ol>

      <section className="card stack" data-testid="how-privacy">
        <div className="card-head">
          <h2>Why it is private</h2>
          <span className="tiny muted">the mechanism, and its edges</span>
        </div>
        <p className="small">
          On a public ledger a new wallet is tied forever to whatever funded it: send from a wallet you already own and the two are linked,
          withdraw from an exchange and the address is tied to an identity. Pgas.me is a third way, and it is private by design rather than
          by promise — so here is the design, and here is what it does not cover.
        </p>

        <h3>What the mechanism does</h3>
        <ul className="how-list" data-testid="privacy-gives">
          <li>
            <span className="strong">The value crosses onto Beam, a confidential ledger.</span> Beam publishes no per-address balances and
            every amount on it is blinded. Between your deposit and your payouts there is no public account for anyone to watch, and no
            figure for anyone to read.
          </li>
          <li>
            <span className="strong">Each deposit names its own receiver key.</span> A bridge message says which key may claim it, and ours
            used to be a single constant — the same 33 bytes in every deposit anyone ever made through Pgas.me, a marker tying one
            depositor&rsquo;s deposits to another&rsquo;s. The key is derived per deposit now, with index 0 kept as the old key byte for
            byte so nothing already in flight is stranded.
          </li>
          <li>
            <span className="strong">Every payout is signed from a fresh Beam address</span>, created for that one order. The address that
            signs your payout has never received a deposit, and is never used again.
          </li>
          <li>
            <span className="strong">The new wallet&rsquo;s history starts with the bridge.</span> The bridge contract pays it directly.
            There is no transfer from a wallet you already control, and no exchange withdrawal behind it.
          </li>
          <li>
            <span className="strong">You choose the timing.</span> A delivery window instead of ASAP, and an amount split across several
            rows, are what break the &ldquo;one deposit in, one payout out, same size, same minute&rdquo; match that correlation depends on.
          </li>
        </ul>

        <h3>What it does not do</h3>
        <ul className="how-list" data-testid="privacy-limits">
          <li>
            <span className="strong">The public chain can tell that the wallet was bridge-funded.</span> Anyone reading Ethereum can see the
            new wallet&rsquo;s first inbound came from the bridge contract. What they cannot see is which deposit paid for it.
          </li>
          <li>
            <span className="strong">Pgas.me sees both ends.</span> This version is custodial: while an order is open we know your deposit
            and your payouts. We null the pairing once the order has settled and keep no IP logs beyond what rate limiting needs — that is a
            procedure we follow, not a thing the maths prevents. The roadmap hands custody back; today it has not.
          </li>
          <li>
            <span className="strong">The bridge relayer is one party on both chains.</span> It stores the receiver key, delivers to it, and
            can read our wallet&rsquo;s coin graph — which means it can tie a payout crossing back to the claim that funded it. The
            per-deposit keys are all still ours, too, and on a bridge with this little traffic a key that never repeats is itself a faint
            pattern: it removes a marker, it does not create a crowd.
          </li>
          <li>
            <span className="strong">Deposits are not shielded today.</span> Beam has a shielded pool, and the code to use it is written and
            sits behind a switch that is off. The reason is measured, not editorial: the pool holds roughly 28,000 outputs and grows about
            30 a day against the 65,536 a full-size proof wants, and a shielded output&rsquo;s other exit is a 72-hour timer — which is
            exactly what once left a payout unable to spend the coins it had been promised. So this page claims a confidential ledger and a
            fresh per-order address, and does not claim a large shielded set.
          </li>
          <li>
            <span className="strong">Instant payouts share one sender.</span> Where the instant lane is enabled, those payouts arrive from a
            single Pgas.me address, so they are linkable to each other — a different trade from the scheduled lane, and the app says so
            where you choose the mode.
          </li>
          <li>
            <span className="strong">Amounts and timing are the link that is left.</span> A round number in and the same round number out a
            few minutes later is a pattern no ledger can hide for you. Split the amount, use a window, and let the two ends stop matching.
          </li>
        </ul>
      </section>

      <section className="card stack" data-testid="how-faq">
        <div className="card-head">
          <h2>Questions</h2>
        </div>
        <dl className="how-faq">
          {FAQ.map((f) => (
            <div key={f.q} className="how-faq-item">
              <dt>{f.q}</dt>
              <dd className="small muted">{f.a}</dd>
            </div>
          ))}
        </dl>
      </section>

      <div className="row how-cta">
        <button type="button" className="btn btn-primary" onClick={() => route.navigate('money')}>
          Start a deposit →
        </button>
        <button type="button" className="btn" onClick={() => route.goMoney('withdraw')}>
          Or go straight to Withdraw
        </button>
      </div>
    </div>
  );
}
