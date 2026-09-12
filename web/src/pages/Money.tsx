// The money page: ONE page, two modes (T57, admin 2026-09-12 with a screenshot of the Deposit
// page — "Don't you think we should have DEPOSIT and WITHDRAW at the same page? Improve it, it
// looks like AI slop now").
//
// Putting money in and taking it out are the same sentence read in two directions — you pay X,
// you receive Y — so they are one panel with a `Deposit | Withdraw` control in its head, where a
// DEX puts buy and sell. Everything else on the old page was in the way of that sentence:
//
//   · FOUR links to the explainer in one view (nav, the lede, the collapsed "What is Pgas.me"
//     row, the footer). It lives in the nav and the footer now, and nowhere else.
//   · ~230 px of header before any control, so the panel began past the middle of the fold.
//     The header is one line.
//   · Two page widths fighting — a full-bleed portfolio card over a 620 px panel — which is what
//     made the bottom third read as dead space. One column, one width, one axis.
//
// This file owns the shape; the two modes own their own money. Each body renders its own context
// card (the wallet's holdings for a deposit, the Pgas balance for a withdrawal) and its own panel,
// because the state they are made of is theirs — a card lifted up here would be a second reader of
// a fact the body already owns.
import { useCallback, type ReactNode } from 'react';
import { Explainer } from '../components/Explainer';
import { SwapModes } from '../components/SwapPanel';
import { DepositBody } from './Deposit';
import { WithdrawBody } from './Schedule';
import { useStore, type MoneyMode } from '../state/store';

const MODES: { id: MoneyMode; label: string }[] = [
  { id: 'deposit', label: 'Deposit' },
  { id: 'withdraw', label: 'Withdraw' },
];

export function MoneyPage() {
  const { route, wallet } = useStore();
  const mode = route.mode;
  const goMoney = route.goMoney;
  const choose = useCallback((m: MoneyMode) => goMoney(m), [goMoney]);

  /**
   * The head of whichever panel is on screen. It is built here and handed down so that BOTH modes
   * carry the identical control in the identical place — a switch that moves by a few pixels
   * between two halves of one page is a switch that reads as two pages.
   */
  const modes: ReactNode = <SwapModes value={mode} options={MODES} onChange={choose} label="Deposit or withdraw" testId="money-modes" />;

  return (
    <div className="page money-page">
      <div className="page-head money-head">
        <h1>Deposit &amp; withdraw</h1>
      </div>
      {/* T31 B′, kept where it still earns its place: a visitor with no wallet connected has
          nothing to do on this page yet and every reason to ask what it is. Once a wallet is
          connected the question is answered and the panel is the page (T57 defect 2). */}
      {!wallet.address && <Explainer current={mode} />}
      <div className="swap-col">{mode === 'deposit' ? <DepositBody modes={modes} /> : <WithdrawBody modes={modes} />}</div>
    </div>
  );
}
