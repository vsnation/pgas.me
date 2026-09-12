// App chrome — header (brand, tabs on desktop, theme switch, one account control), the mobile tab
// bar, notices, the wallet picker dialog, the one-line footer — and the tab switch. Page bodies
// live in pages/.
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Modal } from './components/Modal';
import { RpcSettingsButton } from './components/RpcSettings';
import { errorText } from './lib/api';
import { chainName } from './lib/chains';
import { shortAddr } from './lib/format';
import type { WalletOption } from './lib/wallet';
import { AdminPage, isAdminPath } from './pages/Admin';
import { BalancePage } from './pages/Balance';
import { HowItWorksPage } from './pages/HowItWorks';
import { MoneyPage } from './pages/Money';
import { HOW_PATH, StoreProvider, TABS, useStore, type Tab } from './state/store';

/** Sun and moon, drawn rather than fetched: two paths beat a webfont for one icon. */
function ThemeIcon({ dark }: { dark: boolean }) {
  return dark ? (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" />
    </svg>
  ) : (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
    </svg>
  );
}

function ThemeToggle() {
  const { theme } = useStore();
  const dark = theme.theme === 'dark';
  const next = dark ? 'light' : 'dark';
  return (
    <button
      type="button"
      className="btn btn-sm btn-ghost theme-toggle"
      data-testid="theme-toggle"
      data-theme-now={theme.theme}
      aria-label={`Switch to the ${next} theme`}
      title={theme.fromSystem ? `Following your system (${theme.theme}) — switch to ${next}` : `Switch to the ${next} theme`}
      onClick={theme.toggle}
    >
      <ThemeIcon dark={dark} />
    </button>
  );
}

function TabButtons({ where }: { where: 'header' | 'bar' }) {
  const { route } = useStore();
  const cls = where === 'header' ? 'nav-tab' : 'tabbar-tab';
  return (
    <>
      {TABS.map((t) => (
        <button
          key={t.id}
          type="button"
          data-tab={t.id}
          className={`${cls}${route.tab === t.id ? ' active' : ''}`}
          aria-current={route.tab === t.id ? 'page' : undefined}
          onClick={() => route.navigate(t.id)}
        >
          {where === 'bar' ? t.short : t.label}
        </button>
      ))}
    </>
  );
}

/**
 * The explainer page's link. It is an `<a>` with a real href in both places it appears — it is a
 * page, so it must open in a new tab and be followable by a crawler — and the click handler keeps
 * the in-app navigation for everyone else. T57 left these and the footer as the ONLY routes to it.
 */
function HowTab({ where }: { where: 'header' | 'bar' }) {
  const { route } = useStore();
  const cls = where === 'header' ? 'nav-tab' : 'tabbar-tab';
  return (
    <a
      className={`${cls}${route.tab === 'how' ? ' active' : ''}`}
      href={HOW_PATH}
      aria-current={route.tab === 'how' ? 'page' : undefined}
      data-testid={where === 'header' ? 'nav-how' : 'tabbar-how'}
      onClick={(e) => {
        e.preventDefault();
        route.navigate('how');
      }}
    >
      How it works
    </a>
  );
}

/**
 * The one account control: a pill carrying the chain dot and the address, with everything else
 * (the chain's name, whether the signature landed, Disconnect) inside its menu. It replaced three
 * header chips plus a Disconnect button, which wrapped onto a second row on a phone and left the
 * screen with two Connect buttons — one here and one in whichever card was locked.
 */
function AccountControl() {
  const { wallet, session, data } = useStore();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (!wallet.address) {
    return (
      <button type="button" className="btn btn-primary" onClick={wallet.openPicker} data-testid="connect-btn">
        Connect wallet
      </button>
    );
  }

  const supported = wallet.chainId !== null && data.chains.some((c) => c.chain_id === wallet.chainId);
  const chain = chainName(wallet.chainId, data.chains);
  const signedIn = !!session.session;
  return (
    <div className="acct" ref={root}>
      <button
        type="button"
        className={`chip chip-net acct-pill ${supported ? 'ok' : 'warn'}`}
        data-testid="connected-address"
        data-signed-in={signedIn ? 'yes' : 'no'}
        title={`${chain} · ${wallet.address}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="dot" />
        {wallet.option?.icon && <img src={wallet.option.icon} alt="" width={16} height={16} style={{ borderRadius: 4 }} />}
        <span className="mono">{shortAddr(wallet.address)}</span>
      </button>
      {open && (
        <div className="acct-menu" role="menu" data-testid="account-menu">
          <div className="mono tiny wrap" data-testid="account-menu-address">
            {wallet.address}
          </div>
          <div className="tiny muted">
            {chain}
            {!supported && wallet.chainId !== null && data.chains.length > 0 ? ' · not supported' : ''}
          </div>
          <div className="tiny muted" data-testid="account-menu-session">
            {signedIn ? 'Signed in' : session.signingIn ? 'Waiting for your signature…' : 'Not signed in'}
          </div>
          <button
            type="button"
            className="btn btn-sm btn-block"
            onClick={() => {
              setOpen(false);
              wallet.disconnect();
            }}
          >
            Disconnect
          </button>
        </div>
      )}
    </div>
  );
}

function Header() {
  const { route } = useStore();
  return (
    <header className="header" data-testid="header">
      <div className="container header-inner">
        <a
          className="brand"
          href="/"
          onClick={(e) => {
            e.preventDefault();
            route.navigate('money');
          }}
        >
          <img className="brand-logo" src="/logo-256.png" alt="" width={36} height={36} />
          <span>Pgas.me</span>
        </a>
        <nav className="nav" aria-label="Sections">
          <TabButtons where="header" />
          <HowTab where="header" />
        </nav>
        <div className="header-right">
          {/* T54 — which endpoints THIS BROWSER reads chains through. App chrome, like the theme
              switch: it is not about the money on screen, it is about whether the numbers can be
              read at all, so it belongs beside the switch and not inside a card. */}
          <RpcSettingsButton />
          <ThemeToggle />
          <AccountControl />
        </div>
      </div>
    </header>
  );
}

/**
 * Phones get the three tabs where a thumb reaches them; CSS hides this above 860 px.
 *
 * It also PUBLISHES ITS OWN HEIGHT as `--tabbar-h`, because anything else drawn at the bottom of
 * a phone screen has to sit exactly on top of it and that height is not a number anyone can write
 * down: it is this element's font metrics plus its padding plus the device's home-indicator inset.
 * A constant guessed it at 64px while the bar measures 50.5 at 390×844, and the 13.5px of daylight
 * that left between the two fixed things is what the Schedule totals strip was seen floating above
 * in schedule-mobile.png — with the form scrolling through the gap underneath it (F7, 2026-09-10).
 * One writer for one fact: the element that HAS the height states it, and the strip reads it.
 */
function TabBar() {
  const ref = useRef<HTMLElement | null>(null);
  // measured BEFORE the first paint (useLayoutEffect), so nothing is ever drawn at the fallback
  // offset and then moved — a strip that jumps 13.5px on load is the same defect, briefly
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const publish = () => document.documentElement.style.setProperty('--tabbar-h', `${el.getBoundingClientRect().height}px`);
    publish();
    if (typeof ResizeObserver === 'undefined') return;
    // hidden on desktop → height 0, which is the truth there and what the observer reports the
    // moment a resize crosses the breakpoint in either direction
    const ro = new ResizeObserver(publish);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <nav className="tabbar" aria-label="Sections" data-testid="tabbar" ref={ref}>
      <TabButtons where="bar" />
      {/* T57 — Deposit and Withdraw became one tab, which freed the third column. It goes to the
          explainer: on a phone the header's nav is hidden, so this and the footer are the ways in. */}
      <HowTab where="bar" />
    </nav>
  );
}

function WalletPicker() {
  const { wallet } = useStore();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { pickerOpen, closePicker } = wallet;
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
  /**
   * Only what the user can act on. "browser extension or in-app browser" told a person who is
   * looking at their own wallet's name what kind of software it is; the WalletConnect row without a
   * project id offered a button that could only refuse. What is left: the wallet is busy, or (for
   * WalletConnect, when it IS configured) what tapping it will do.
   */
  const subtitle = (o: WalletOption) => {
    if (busy === o.id) return o.source === 'walletconnect' ? 'Loading WalletConnect…' : 'Waiting for the wallet…';
    return o.source === 'walletconnect' ? (o.hint ?? null) : null;
  };
  const options = wallet.options.filter((o) => !o.disabledReason);
  const detected = options.filter((o) => o.source !== 'walletconnect');
  return (
    <Modal title="Connect a wallet" onClose={closePicker} testId="wallet-picker">
      <div className="stack">
        {detected.length === 0 && (
          <p className="small muted">
            No wallet was detected in this browser. Install one (MetaMask, Rabby, Coinbase Wallet, Zerion, Trust, OKX, Coin98…) or open
            pgas.me from your wallet's own browser.
          </p>
        )}
        <div className="wallet-list" role="list">
          {options.map((o) => {
            const sub = subtitle(o);
            return (
              <button
                key={o.id}
                type="button"
                className="wallet-row"
                onClick={() => pick(o)}
                disabled={busy !== null}
                role="listitem"
                data-wallet-id={o.id}
                data-wallet-source={o.source}
                data-inapp={o.inApp ? 'yes' : undefined}
              >
                <img src={o.icon} alt="" />
                <span className="stack-sm" style={{ gap: 0, minWidth: 0 }}>
                  <span className="row" style={{ gap: 8 }}>
                    <span className="w-name">{o.name}</span>
                    {(o.source === 'injected' || o.source === 'eip6963') && <span className="pill pill-teal">Detected</span>}
                  </span>
                  {sub && <span className="w-src">{sub}</span>}
                </span>
              </button>
            );
          })}
        </div>
        {err && <p className="error-text">{err}</p>}
        <p className="tiny muted">Signing in is free and moves nothing.</p>
      </div>
    </Modal>
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

/**
 * One line. The stats strip (deposits 24 h/7 d, payouts, shielded outputs, Beam height) and the
 * Ingress/Direct/Instant chips were operator telemetry on a depositor's screen: nothing there is
 * something a user acts on, and an unarmed ingress already says so in the quote panel itself. With
 * the strip gone the app stopped fetching `GET /v1/stats` at all (2026-09-09).
 */
function Footer() {
  const { route } = useStore();
  return (
    <footer className="footer">
      <div className="container">
        <div className="footer-note">
          <span>Settled on Beam — a confidential ledger: no addresses on-chain, blinded amounts.</span>
          {/* the only route to the explainer page on a phone, where the header's nav is hidden */}
          <a
            href={HOW_PATH}
            data-testid="footer-how"
            onClick={(e) => {
              e.preventDefault();
              route.navigate('how');
            }}
          >
            How it works
          </a>
        </div>
      </div>
    </footer>
  );
}

function Page() {
  const { route } = useStore();
  const tab: Tab = route.tab;
  switch (tab) {
    case 'balance':
      return <BalancePage />;
    case 'how':
      return <HowItWorksPage />;
    default:
      return <MoneyPage />;
  }
}

export default function App() {
  /**
   * `/admin` is the operator panel (T38): its own key gate, its own chrome, nothing public. It is
   * answered BEFORE the store provider mounts, so on that route no wallet is discovered, no session
   * is polled and no reference data is fetched — and nothing in the public UI links to it.
   */
  if (isAdminPath(window.location.pathname)) return <AdminPage />;
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
        <Footer />
        <TabBar />
        <WalletPicker />
      </div>
    </StoreProvider>
  );
}
