// Wraps the locked pages: renders children once signed in, otherwise the connect / sign-in step.
import type { ReactNode } from 'react';
import { shortAddr } from '../lib/format';
import { useStore } from '../state/store';

export function SignInGate({ children, what = 'this page' }: { children: ReactNode; what?: string }) {
  const { wallet, session } = useStore();
  if (session.session) return <>{children}</>;
  const What = what[0].toUpperCase() + what.slice(1);
  return (
    <div className="card" data-testid="sign-in-gate">
      <div className="stack">
        <h2>{wallet.address ? 'Sign in with wallet' : 'Connect a wallet'}</h2>
        {session.expired && (
          <div className="banner banner-warn" role="status">
            Your session expired. Sign in again to continue.
            <button type="button" className="link-btn" onClick={session.dismissExpired}>
              Dismiss
            </button>
          </div>
        )}
        <p className="muted">
          {wallet.address
            ? `${What} belongs to the signed-in wallet. Sign a message with ${shortAddr(wallet.address)} — it costs nothing and moves nothing.`
            : `Connect an injected wallet to see ${what}. The account is the wallet: no seed phrase, no email.`}
        </p>
        <div className="row">
          {wallet.address ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={session.signingIn}
              onClick={() => session.signIn().catch(() => undefined)}
            >
              {session.signingIn ? 'Waiting for the signature…' : 'Sign in with wallet'}
            </button>
          ) : (
            <button type="button" className="btn btn-primary" onClick={wallet.openPicker}>
              Connect wallet
            </button>
          )}
        </div>
        {session.signInError && <p className="error-text">{session.signInError}</p>}
        {wallet.error && !wallet.address && <p className="error-text">{wallet.error}</p>}
      </div>
    </div>
  );
}
