// "Add explanation about what is pgas.me. If you see my screenshot, it's not clear what to do here.
// For us it's clear, not for new users." — the admin, 2026-09-10, looking at the signed-in Deposit
// page: portfolio chips, a "What to deposit" card and an empty Quote card, with nothing on screen
// saying what the product is or which of the three tabs comes next.
//
// So this panel is for EVERY visitor, signed in or not, and it is open by default. It is not a
// modal (a modal is a thing you have to know to open) and not a marketing hero (the page under it
// is the product): the existing card, the existing type scale, two sentences and the three tabs as
// three numbered steps with the one you are on lit up.
//
// Dismissing it is remembered; the "What is Pgas.me?" link brings it back. A first-time visitor —
// anyone whose browser has never stored that choice — always gets it open.
import { useCallback, useEffect, useState } from 'react';
import { HOW_PATH, useStore, type Tab } from '../state/store';

const KEY = 'pgas.explainer.v1';

/** The three tabs, said as what the user DOES in each — the order money moves through the app. */
const STEPS: { tab: Tab; title: string; line: string }[] = [
  { tab: 'deposit', title: 'Deposit', line: 'Pay from any wallet, on any chain. This page.' },
  { tab: 'balance', title: 'Balance', line: 'See it arrive on Beam’s confidential ledger.' },
  { tab: 'schedule', title: 'Schedule', line: 'List the wallets, the amounts and the times.' },
];

function storedDismissed(): boolean {
  try {
    return localStorage.getItem(KEY) === 'dismissed';
  } catch {
    return false; // no storage is not a choice to hide it
  }
}

function remember(dismissed: boolean): void {
  try {
    if (dismissed) localStorage.setItem(KEY, 'dismissed');
    else localStorage.removeItem(KEY);
  } catch {
    // storage unavailable: the choice lasts for this page
  }
}

/**
 * The way to the long form (T44). An `<a>` with a real href rather than a button: it is a page,
 * so it must be openable in a new tab and followable by a crawler; the click handler keeps the
 * in-app navigation for everyone else.
 */
function HowLink() {
  const { route } = useStore();
  return (
    <a
      className="link-btn"
      href={HOW_PATH}
      data-testid="explainer-how"
      onClick={(e) => {
        e.preventDefault();
        route.navigate('how');
      }}
    >
      How it works, step by step →
    </a>
  );
}

/**
 * `current` is the tab this page IS — the step that lights up. Every page can render this; the
 * Deposit page is the one a first visit lands on.
 */
export function Explainer({ current = 'deposit' }: { current?: Tab } = {}) {
  const { wallet, route } = useStore();
  const [dismissed, setDismissed] = useState<boolean>(() => storedDismissed());
  // storage is read once per mount; a second tab that dismissed it does not reach back into this one
  useEffect(() => setDismissed(storedDismissed()), []);

  const hide = useCallback(() => {
    remember(true);
    setDismissed(true);
  }, []);
  const show = useCallback(() => {
    remember(false);
    setDismissed(false);
  }, []);

  if (dismissed) {
    return (
      <div className="explainer-collapsed row" data-testid="explainer-collapsed">
        <button type="button" className="link-btn" data-testid="explainer-show" onClick={show}>
          What is Pgas.me?
        </button>
        <HowLink />
      </div>
    );
  }

  return (
    <section className="card explainer" data-testid="explainer">
      <div className="card-head">
        <h2>What is Pgas.me</h2>
        <button type="button" className="link-btn" data-testid="explainer-dismiss" onClick={hide}>
          Dismiss
        </button>
      </div>
      <div className="stack">
        <p className="muted small" data-testid="explainer-what">
          Private gas for fresh EVM wallets: you deposit from any wallet and any chain, and the value settles on Beam&rsquo;s confidential
          ledger. Later you schedule payouts to new wallets, and nothing on any public chain links them to the deposit.
        </p>
        <div className="row">
          <HowLink />
        </div>
        <ol className="explainer-steps" data-testid="explainer-steps">
          {STEPS.map((s, i) => (
            <li key={s.tab} className={`explainer-step${s.tab === current ? ' current' : ''}`} data-step={s.tab}>
              <button type="button" onClick={() => route.navigate(s.tab)} aria-current={s.tab === current ? 'step' : undefined}>
                <span className="n" aria-hidden="true">
                  {i + 1}
                </span>
                <span className="explainer-step-text">
                  <span className="strong">{s.title}</span>
                  <span className="tiny muted">{s.line}</span>
                </span>
              </button>
            </li>
          ))}
        </ol>
        {!wallet.address && (
          <div className="row">
            {/* ONE control named "Connect wallet" on the screen — the header's (T14). This is the
                same flow with its own words, so a visitor reading the explainer has a way in from
                where they are reading, and nothing competes with the header for the same name. */}
            <button type="button" className="btn btn-primary" data-testid="explainer-connect" onClick={wallet.openPicker}>
              Connect your wallet to start
            </button>
            <span className="tiny muted">Signing in is free and moves nothing.</span>
          </div>
        )}
        {/* ⛔ "then are shielded" until 2026-09-10, which stopped being true the day the treasury
            decision landed: what we claim is what we distribute, the bETH stays spendable on our
            own address, and the privacy work is a FRESH Beam address per payout order (Scheme
            § "Treasury policy — no shielding step"). A page may not promise a mechanism that is
            switched off; /how-it-works says the same thing at length, with the limits. */}
        <p className="tiny muted" data-testid="explainer-limits">
          Deposits arrive as ETH on Ethereum through the Beam bridge and are held on Beam&rsquo;s confidential ledger — no per-address
          balances, blinded amounts. No public chain links a funded wallet to your deposit. Pgas.me itself is the custodian for now.
        </p>
      </div>
    </section>
  );
}
