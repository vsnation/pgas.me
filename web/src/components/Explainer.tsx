// "Add explanation about what is pgas.me. If you see my screenshot, it's not clear what to do here.
// For us it's clear, not for new users." — the admin, 2026-09-10, looking at the signed-in Deposit
// page: portfolio chips, a "What to deposit" card and an empty Quote card, with nothing on screen
// saying what the product is or which of the tabs comes next.
//
// T57 (2026-09-12) narrowed where it appears, not what it says. Two changes, both from the admin's
// screenshot of the result:
//
//   1. ⛔ NO LINK TO THE EXPLAINER PAGE FROM HERE. This card's "How it works, step by step →" was
//      the third of FOUR routes to one page in a single view (nav, the page lede, this row, the
//      footer). It is in the nav and the footer now, and those are the only two.
//   2. It renders only while NO WALLET IS CONNECTED. A visitor who has not connected anything has
//      nothing else to do on the money page and every reason to ask what it is; once a wallet is
//      connected the panel is the page, and a card above it is the 230 px of preamble that put the
//      first control past the middle of the fold. The money page decides that (pages/Money.tsx) —
//      this component still just draws itself.
//
// Dismissing it is remembered; the "What is Pgas.me?" link brings it back.
import { useCallback, useEffect, useState } from 'react';
import { useStore } from '../state/store';

const KEY = 'pgas.explainer.v1';

/** What the user DOES, in the order money moves through the app — two tabs, three steps. */
type StepId = 'deposit' | 'balance' | 'withdraw';
const STEPS: { id: StepId; title: string; line: string }[] = [
  { id: 'deposit', title: 'Deposit', line: 'Pay from any wallet, on any chain.' },
  { id: 'balance', title: 'Balance', line: 'See it arrive on Beam’s confidential ledger.' },
  { id: 'withdraw', title: 'Withdraw', line: 'List the wallets, the amounts and the times.' },
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

/** `current` is the step this page IS — the one that lights up. */
export function Explainer({ current = 'deposit' }: { current?: StepId } = {}) {
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

  const go = useCallback(
    (id: StepId) => {
      if (id === 'balance') route.navigate('balance');
      else route.goMoney(id);
    },
    [route],
  );

  if (dismissed) {
    return (
      <div className="explainer-collapsed row" data-testid="explainer-collapsed">
        <button type="button" className="link-btn" data-testid="explainer-show" onClick={show}>
          What is Pgas.me?
        </button>
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
          ledger. Later you withdraw to new wallets, and nothing on any public chain links them to the deposit.
        </p>
        <ol className="explainer-steps" data-testid="explainer-steps">
          {STEPS.map((s, i) => (
            <li key={s.id} className={`explainer-step${s.id === current ? ' current' : ''}`} data-step={s.id}>
              <button type="button" onClick={() => go(s.id)} aria-current={s.id === current ? 'step' : undefined}>
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
