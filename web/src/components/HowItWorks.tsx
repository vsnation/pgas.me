// The mechanism, in one place, behind a link.
//
// Screen review 2026-09-09: every page opened with a paragraph about cross-chain orders, hooks,
// pipes and the Beam bridge — words a depositor has no use for while deciding what to pay with. The pages
// now say one sentence in the user's own words and the machinery lives here, one click away, for
// the person who does want it.
import { useState } from 'react';
import { resolveIngress, ingressPartial } from '../lib/ingress';
import { useStore } from '../state/store';
import { Modal } from './Modal';

const STEPS = [
  'You pay any token on any chain — one transaction, from your own wallet.',
  'A cross-chain order fills it on Ethereum in the asset you chose (ETH, DAI or WBTC).',
  'The order carries a hook, so the asset goes straight on to Pgas.me — you never send a second transaction.',
  'It locks into the Beam bridge, a confidential ledger: no addresses on chain, amounts blinded.',
  'Your balance is credited to the wallet you signed in with. Nothing is charged for a deposit.',
  'You schedule payouts: ETH leaves your balance at the time you pick and arrives in any wallet you name, with no on-chain link to where it came from. That is the only fee — 2%.',
];

/**
 * Step 2 is the only line that changes with the route, because it is the only line that describes
 * how the money gets to Ethereum. The page that knows which path the user is actually on says so
 * (`route`); everywhere else the flag decides, and the cross-chain sentence stays whenever the
 * cross-chain order is what a deposit would use.
 */
const UNISWAP_STEP =
  'A Uniswap V4 swap on Ethereum whose Pgas hook locks the output in the Beam bridge in the same transaction.';

function steps(route: 'uniswap' | 'xchain'): string[] {
  if (route !== 'uniswap') return STEPS;
  return STEPS.map((s, i) => (i === 1 ? UNISWAP_STEP : s));
}

/**
 * What "we tested it" actually means, said in the only way that is true: a simulation of each
 * wallet's own behaviour (its EIP-6963 announcement or legacy global, and the quirk it is known
 * for) driven by the e2e suite. A green suite is not a real device, and this line must never let a
 * reader think it is. When a wallet is checked by hand on a real device, name it here.
 */
const TESTED = 'MetaMask, Rabby, Zerion, Trust, OKX, Coinbase Wallet, Phantom, Coin98, Binance Web3, Bitget, TokenPocket';
const REAL_DEVICE_CHECKS = 'none yet';

export function HowItWorks({ route }: { route?: 'uniswap' | 'xchain' } = {}) {
  const { data, session } = useStore();
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
        </Modal>
      )}
    </>
  );
}
