// App chrome — header (brand, tabs, network chip, connect), notices, the wallet picker dialog, the
// stats footer — and the tab switch. Page bodies live in pages/.
import { useEffect, useRef, useState } from 'react';
import { errorText } from './lib/api';
import { chainName } from './lib/chains';
import { fmtAgo, fmtNumber, shortAddr } from './lib/format';
import type { WalletOption } from './lib/wallet';
import { ActivityPage } from './pages/Activity';
import { BalancePage } from './pages/Balance';
import { DepositPage } from './pages/Deposit';
import { WalletsPage } from './pages/Wallets';
import { WithdrawPage } from './pages/Withdraw';
import { StoreProvider, TABS, useStore } from './state/store';

function Header() {
  const { wallet, session, data, route } = useStore();
  const supported = wallet.chainId !== null && data.chains.some((c) => c.chain_id === wallet.chainId);
  const signedInElsewhere = !!session.session && !!wallet.address && session.session.address.toLowerCase() !== wallet.address.toLowerCase();
  return (
    <header className="header">
      <div className="container header-inner">
        <a
          className="brand"
          href="/"
          onClick={(e) => {
            e.preventDefault();
            route.navigate('deposit');
          }}
        >
          <img className="brand-logo" src="/logo-256.png" alt="" width={36} height={36} />
          <span>Pgas.me</span>
        </a>
        <nav className="nav" aria-label="Sections">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`nav-tab${route.tab === t.id ? ' active' : ''}`}
              aria-current={route.tab === t.id ? 'page' : undefined}
              onClick={() => route.navigate(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="header-right">
          {wallet.address && (
            <span className={`chip chip-net ${supported ? 'ok' : 'warn'}`} title={`Wallet chain id ${wallet.chainId ?? '?'}`}>
              <span className="dot" />
              {chainName(wallet.chainId, data.chains)}
              {!supported && wallet.chainId !== null && data.chains.length > 0 ? ' · not supported' : ''}
            </span>
          )}
          {session.session && (
            <span className="chip ok" title={`Signed in as ${session.session.address}`}>
              <span className="dot" />
              {signedInElsewhere ? `Signed in: ${shortAddr(session.session.address)}` : 'Signed in'}
            </span>
          )}
          {wallet.address ? (
            <>
              <span className="chip mono" title={wallet.address} data-testid="connected-address">
                {wallet.option?.icon && <img src={wallet.option.icon} alt="" width={16} height={16} style={{ borderRadius: 4 }} />}
                {shortAddr(wallet.address)}
              </span>
              <button type="button" className="btn btn-sm" onClick={wallet.disconnect}>
                Disconnect
              </button>
            </>
          ) : (
            <button type="button" className="btn btn-primary" onClick={wallet.openPicker}>
              Connect wallet
            </button>
          )}
        </div>
      </div>
    </header>
  );
}

function WalletPicker() {
  const { wallet } = useStore();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const { pickerOpen, closePicker } = wallet;
  useEffect(() => {
    if (!pickerOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && closePicker();
    document.addEventListener('keydown', onKey);
    dialog.current?.querySelector<HTMLElement>('button')?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [pickerOpen, closePicker]);
  if (!pickerOpen) return null;
  const pick = async (o: WalletOption) => {
    setBusy(o.id);
    setErr(null);
    try {
      await wallet.connect(o);
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  const subtitle = (o: WalletOption) => {
    if (busy === o.id) return o.source === 'walletconnect' ? 'Loading WalletConnect…' : 'Waiting for the wallet…';
    if (o.disabledReason) return o.disabledReason;
    if (o.hint) return o.hint;
    return o.source === 'farcaster' ? 'Farcaster mini app' : 'browser extension or in-app browser';
  };
  const detected = wallet.options.filter((o) => o.source !== 'walletconnect');
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && closePicker()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Connect a wallet" ref={dialog}>
        <div className="modal-head">
          <h2>Connect a wallet</h2>
          <button type="button" className="btn btn-ghost btn-sm" onClick={closePicker}>
            Close
          </button>
        </div>
        <div className="stack">
          {detected.length === 0 && (
            <p className="small muted">
              No browser wallet was detected on this page. Install one (MetaMask, Rabby, Coinbase Wallet, Zerion, Trust, OKX, Coin98…), open
              pgas.me from your wallet's in-app browser, or scan with WalletConnect below.
            </p>
          )}
          <div className="wallet-list" role="list">
            {wallet.options.map((o) => (
              <button
                key={o.id}
                type="button"
                className="wallet-row"
                onClick={() => pick(o)}
                disabled={busy !== null || !!o.disabledReason}
                aria-disabled={!!o.disabledReason}
                role="listitem"
                data-wallet-id={o.id}
              >
                <img src={o.icon} alt="" />
                <span className="stack-sm" style={{ gap: 0, minWidth: 0 }}>
                  <span className="row" style={{ gap: 8 }}>
                    <span className="w-name">{o.name}</span>
                    {(o.source === 'injected' || o.source === 'eip6963') && <span className="pill pill-teal">Detected</span>}
                  </span>
                  <span className="w-src">{subtitle(o)}</span>
                </span>
              </button>
            ))}
          </div>
          {err && <p className="error-text">{err}</p>}
          <p className="tiny muted">
            The account is the wallet. Signing in costs nothing and moves nothing; whoever controls the wallet controls the balance.
          </p>
        </div>
      </div>
    </div>
  );
}

function Notices() {
  const { session, data } = useStore();
  return (
    <>
      {session.expired && (
        <div className="banner banner-warn" role="status" style={{ marginBottom: 16 }} data-testid="expired-banner">
          Your session expired or was rejected. Sign in again to continue.
          <button type="button" className="link-btn" onClick={session.dismissExpired}>
            Dismiss
          </button>
        </div>
      )}
      {data.error && (
        <div className="banner banner-error" role="alert" style={{ marginBottom: 16 }}>
          Reference data failed to load — {data.error}.
          <button type="button" className="link-btn" onClick={data.reload}>
            Retry
          </button>
        </div>
      )}
    </>
  );
}

function ArmedChip({ on, label }: { on: boolean; label: string }) {
  return (
    <span className={`chip ${on ? 'ok' : 'warn'}`}>
      <span className="dot" />
      {label}
    </span>
  );
}

function StatsFooter() {
  const { data } = useStore();
  const { stats, statsError } = data;
  return (
    <footer className="footer">
      <div className="container">
        {stats ? (
          <div className="stats-strip" data-testid="stats-strip">
            <span className="stat">
              Deposits 24 h <b>{fmtNumber(stats.deposits_24h)}</b>
            </span>
            <span className="stat">
              7 d <b>{fmtNumber(stats.deposits_7d)}</b>
            </span>
            <span className="stat">
              Payouts 24 h <b>{fmtNumber(stats.payouts_24h)}</b>
            </span>
            <span className="stat">
              Shielded outputs <b>{fmtNumber(stats.pool?.shielded_outputs_total ?? 0)}</b>
              <span className="tiny">total</span>
            </span>
            <span className="stat">
              <b>{fmtNumber(stats.pool?.shielded_outputs_per_24h ?? 0)}</b>
              <span className="tiny">per 24 h</span>
            </span>
            <span className="stat">
              Beam height <b>{fmtNumber(stats.pool?.height ?? 0)}</b>
              {stats.pool?.at ? <span className="tiny">{fmtAgo(stats.pool.at)}</span> : null}
            </span>
            <span className="spacer" />
            <ArmedChip on={!!stats.armed?.ingress} label={`Ingress ${stats.armed?.ingress ? 'armed' : 'not armed'}`} />
            <ArmedChip on={!!stats.armed?.direct} label={`Direct ${stats.armed?.direct ? 'on' : 'off'}`} />
            <ArmedChip on={!!stats.armed?.instant} label={`Instant ${stats.armed?.instant ? 'on' : 'off'}`} />
          </div>
        ) : (
          <div className="stats-strip">
            <span>{statsError ? `Stats unavailable: ${statsError}` : 'Loading stats…'}</span>
          </div>
        )}
        <div className="footer-note">
          <span>Settled on Beam — a confidential ledger: no addresses on-chain, blinded amounts.</span>
        </div>
      </div>
    </footer>
  );
}

function Page() {
  const { route } = useStore();
  switch (route.tab) {
    case 'balance':
      return <BalancePage />;
    case 'wallets':
      return <WalletsPage />;
    case 'withdraw':
      return <WithdrawPage />;
    case 'activity':
      return <ActivityPage />;
    default:
      return <DepositPage />;
  }
}

export default function App() {
  return (
    <StoreProvider>
      <div className="app">
        <Header />
        <main className="main">
          <div className="container">
            <Notices />
            <Page />
          </div>
        </main>
        <StatsFooter />
        <WalletPicker />
      </div>
    </StoreProvider>
  );
}
