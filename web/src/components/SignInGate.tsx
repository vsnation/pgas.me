// Wraps the locked pages: renders children once signed in, otherwise the smallest true thing.
//
// Screen review 2026-09-09: signing in now happens by itself the moment the wallet connects, so
// this is no longer a step in the normal path — it is what is left when there is no wallet yet (one
// line, and the header's Connect button is the only Connect button on the screen), while the
// signature is being waited for (one line), or when the user said no (the card, with the retry).
import type { ReactNode } from 'react';
import { shortAddr } from '../lib/format';
import { useStore } from '../state/store';

export function SignInGate({
  children,
  what = 'this page',
  verb = 'see',
}: {
  children: ReactNode;
  what?: string;
  /** "…to <verb> <what>": get a quote, see your balance, schedule payouts. */
  verb?: string;
}) {
  const { wallet, session } = useStore();
  if (session.session) return <>{children}</>;

  if (!wallet.address) {
    return (
      <p className="muted small" data-testid="sign-in-gate">
        Connect your wallet to {verb} {what}.
      </p>
    );
  }
  if (session.signingIn) {
    return (
      <p className="muted small" data-testid="sign-in-gate">
        Waiting for your signature in the wallet…
      </p>
    );
  }
  return (
    <div className="card" data-testid="sign-in-gate">
      <div className="stack">
        <h2>
          Sign in to {verb} {what}
        </h2>
        <p className="muted">Sign a message with {shortAddr(wallet.address)}. It is free and moves nothing.</p>
        <div className="row">
          <button type="button" className="btn btn-primary" onClick={() => session.signIn().catch(() => undefined)}>
            Sign in with wallet
          </button>
        </div>
        {session.signInError && <p className="error-text">{session.signInError}</p>}
      </div>
    </div>
  );
}
