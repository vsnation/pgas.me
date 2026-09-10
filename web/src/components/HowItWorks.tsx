// The mechanism, in one place, behind a link.
//
// Screen review 2026-09-09: every page opened with a paragraph about cross-chain orders, hooks,
// pipes and the Beam bridge — words a depositor has no use for while deciding what to pay with. The pages
// now say one sentence in the user's own words and the machinery lives here, one click away, for
// the person who does want it.
import { useState } from 'react';
import { resolveIngress, ingressPartial } from '../lib/ingress';
import { HOW_PATH, useStore } from '../state/store';
import { Modal } from './Modal';

const STEPS = [
  'You pay any token on any chain — one transaction, from your own wallet.',
  'A cross-chain order fills it on Ethereum in the asset you chose (ETH, DAI or WBTC).',
  // T31 A (2026-09-10): the deposit LANDS on Ethereum and travels on through the Beam bridge —
  // said here in the same words the quote card uses, because "where does my money actually go"
  // was the question the admin's screenshot could not answer.
  'The order carries a hook, so what lands on Ethereum goes straight on to Pgas.me through the Beam bridge — you never send a second transaction.',
  'It locks into the Beam bridge, a confidential ledger: no addresses on chain, amounts blinded.',
  'Your balance is credited to the wallet you signed in with. Nothing is charged for a deposit.',
  // ⛔ "That is the only fee — 2%" until 2026-09-10, when the crossing started being charged
  // explicitly instead of being hidden inside the percentage: the Schedule card itemises TWO
  // lines and so does this one, or the panel and the form are two answers to one question.
  'You schedule payouts from that balance to the wallets you choose: ETH leaves at the time you pick and arrives with no on-chain link to where it came from — on Ethereum now, other chains as they are enabled. Ours is the only fee we add — 2% — with the bridge fee passed through at cost beside it.',
];

/**
 * Which path the money takes to Ethereum is the only thing that changes with the route, so it is
 * the only thing that is rewritten — steps 2 and 3. The page that knows which path the user is
 * actually on says so (`route`); everywhere else the flag decides, and the cross-chain sentence
 * stays whenever the cross-chain order is what a deposit would use.
 *
 * Two Uniswap shapes exist and they are not the same story: the deployed-hook one is a single
 * transaction, and `uniswap-two-step` (U2, 2026-09-10 — nothing deployed) is a swap into the
 * user's OWN wallet followed by a deposit. Telling a two-click flow it is one transaction is
 * exactly the lie this panel exists to avoid.
 */
export type HowItWorksRoute = 'uniswap' | 'uniswap-two-step' | 'xchain';

const UNISWAP_STEP = 'A Uniswap V4 swap on Ethereum whose Pgas hook locks the output in the Beam bridge in the same transaction.';
const TWO_STEP_STEPS: Record<number, string> = {
  1: 'You swap it to ETH on Uniswap V4 — the ETH lands in your own wallet, never ours.',
  // ⛔ NOT "two clicks, two transactions" (coordinator, 2026-09-10). That count is wrong the
  // first time a token is used: the swap needs the token approved to Permit2 and Permit2
  // approved to the router, and a USDT-shaped token needs its allowance reset to zero before
  // either. A number a user can watch go up is worse than no number — say the shape instead,
  // and let the card that is about to ask for them count them.
  2: 'One more step sends that ETH on to Pgas.me through the Beam bridge. Two steps in your own wallet, plus one-off approvals the first time you use a token.',
};

function steps(route: HowItWorksRoute): string[] {
  if (route === 'uniswap') return STEPS.map((s, i) => (i === 1 ? UNISWAP_STEP : s));
  if (route === 'uniswap-two-step') return STEPS.map((s, i) => TWO_STEP_STEPS[i] ?? s);
  return STEPS;
}

/**
 * What "we tested it" actually means, said in the only way that is true: a simulation of each
 * wallet's own behaviour (its EIP-6963 announcement or legacy global, and the quirk it is known
 * for) driven by the e2e suite. A green suite is not a real device, and this line must never let a
 * reader think it is. When a wallet is checked by hand on a real device, name it here.
 */
const TESTED = 'MetaMask, Rabby, Zerion, Trust, OKX, Coinbase Wallet, Phantom, Coin98, Binance Web3, Bitget, TokenPocket';
const REAL_DEVICE_CHECKS = 'none yet';

export function HowItWorks({ route }: { route?: HowItWorksRoute } = {}) {
  const { data, session, route: nav } = useStore();
  const [open, setOpen] = useState(false);
  // No route named (Balance, Schedule): the open path is the one a deposit would take right now.
  const flags = resolveIngress(data.ingress, ingressPartial(session.account));
  const active = route ?? (flags.uniswap ? 'uniswap' : 'xchain');
  return (
    <>
      <button type="button" className="link-btn" onClick={() => setOpen(true)} data-testid="how-it-works">
        How it works
      </button>
      {open && (
        <Modal title="How Pgas.me works" onClose={() => setOpen(false)} testId="how-it-works-modal" width={520}>
          <ol className="steps" data-testid="how-it-works-steps" data-route={active}>
            {steps(active).map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
          <p className="tiny muted" data-testid="tested-wallets" style={{ marginTop: 12 }}>
            Tested wallets: {TESTED} — simulated in tests (their EIP-6963 announcement or legacy global, and the quirks each is known for).
            Real-device checks: {REAL_DEVICE_CHECKS}. Any EIP-6963 wallet should work; if yours does not, tell us which one.
          </p>
          {/* T44 — six lines is the answer to "where does my money go?". The page is the answer to
              everything else, so the panel points at it rather than growing into it. */}
          <p style={{ marginTop: 10 }}>
            <a
              className="link-btn"
              href={HOW_PATH}
              data-testid="how-it-works-more"
              onClick={(e) => {
                e.preventDefault();
                setOpen(false);
                nav.navigate('how');
              }}
            >
              Every step, with screenshots →
            </a>
          </p>
        </Modal>
      )}
    </>
  );
}
