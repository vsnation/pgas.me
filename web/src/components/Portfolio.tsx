// Balances read client-side: the connected wallet's holdings across every supported chain (Deposit
// page, tap-to-pay chips) and a destination's native balance per chain (Wallets page).
import { useCallback, useEffect, useRef, useState } from 'react';
import { formatEther } from 'ethers';
import { DESTINATION_CHAINS, chainName } from '../lib/chains';
import { fmtAgo, fmtNumber, fmtUsd } from '../lib/format';
import {
  loadCachedPortfolio,
  nativeBalance,
  pricesRateLimited,
  scanPortfolio,
  type Holding,
  type NativeRead,
  type Portfolio as PortfolioData,
} from '../lib/portfolio';
import { useStore } from '../state/store';

const MAX_CHIPS = 24;

function HoldingLogo({ h }: { h: Holding }) {
  const [broken, setBroken] = useState(false);
  if (h.logo && !broken) return <img src={h.logo} alt="" onError={() => setBroken(true)} loading="lazy" />;
  return <span className="logo-fallback">{h.symbol.slice(0, 3).toUpperCase()}</span>;
}

export function Portfolio({
  address,
  selectedKey,
  onPick,
}: {
  address: string;
  selectedKey?: string | null;
  onPick: (h: Holding) => void;
}) {
  const { wallet, data } = useStore();
  const { chains, loading: chainsLoading, error: dataError } = data;
  const [portfolio, setPortfolio] = useState<PortfolioData | null>(null);
  const [scanning, setScanning] = useState(false);
  const [progress, setProgress] = useState<[number, number]>([0, 0]);
  const [error, setError] = useState<string | null>(null);
  const run = useRef(0);
  const { provider, chainId } = wallet;

  const scan = useCallback(
    async (force: boolean) => {
      if (!chains.length) return;
      const cached = force ? null : loadCachedPortfolio(address);
      if (cached) {
        setPortfolio(cached);
        return;
      }
      const id = ++run.current;
      setScanning(true);
      setError(null);
      setProgress([0, chains.length]);
      try {
        const p = await scanPortfolio(address, chains, provider ? { provider, chainId } : null, (d, t) => {
          if (id === run.current) setProgress([d, t]);
        });
        if (id === run.current) setPortfolio(p);
      } catch (e) {
        if (id === run.current) setError((e as Error)?.message ?? String(e));
      } finally {
        if (id === run.current) setScanning(false);
      }
    },
    [address, chains, provider, chainId],
  );

  useEffect(() => {
    setPortfolio(null);
    void scan(false);
  }, [address, chains, scan]);

  const reachable = portfolio ? portfolio.chains.filter((c) => c.via !== 'none').length : 0;
  const unreachable = portfolio ? portfolio.chains.filter((c) => c.via === 'none').length : 0;
  const chips = portfolio ? portfolio.holdings.slice(0, MAX_CHIPS) : [];
  const totalUsd = portfolio ? portfolio.holdings.reduce((s, h) => s + (h.usd ?? 0), 0) : 0;

  return (
    <section className="card" data-testid="portfolio">
      <div className="card-head">
        <div>
          <h2>Your portfolio</h2>
          <p className="small muted">Read client-side from every supported chain — tap a holding to pay with it.</p>
        </div>
        <div className="row">
          {portfolio?.priced && totalUsd > 0 && (
            <span className="num strong" title="Sum of priced holdings">
              {fmtUsd(totalUsd)}
            </span>
          )}
          {portfolio && !scanning && <span className="tiny muted">as of {fmtAgo(portfolio.at)}</span>}
          <button type="button" className="btn btn-sm" onClick={() => scan(true)} disabled={scanning || !chains.length}>
            {scanning ? 'Scanning…' : 'Refresh'}
          </button>
        </div>
      </div>

      {chainsLoading && <p className="muted small">Loading the chain list…</p>}
      {!chainsLoading && !chains.length && (
        <div className="banner banner-warn">Chain list unavailable{dataError ? `: ${dataError}` : ''} — nothing to scan.</div>
      )}
      {scanning && (
        <div className="stack-sm" aria-live="polite">
          <p className="small muted">
            Scanning {progress[1]} chains — {progress[0]} done
          </p>
          <div className="progress">
            <span style={{ width: `${progress[1] ? (100 * progress[0]) / progress[1] : 0}%` }} />
          </div>
        </div>
      )}
      {error && <div className="banner banner-error">Scan failed: {error}</div>}

      {portfolio && !scanning && (
        <div className="stack">
          {chips.length ? (
            <div className="chips" data-testid="portfolio-chips">
              {chips.map((h) => (
                <button
                  key={h.key}
                  type="button"
                  className={`holding${selectedKey === h.key ? ' selected' : ''}`}
                  onClick={() => onPick(h)}
                  title={`${h.symbol} on ${h.chainName}`}
                >
                  <HoldingLogo h={h} />
                  <span className="h-main">
                    <span className="h-sym">{h.symbol}</span>
                    <span className="h-chain">{h.chainName}</span>
                  </span>
                  <span className="h-amt">
                    {fmtNumber(h.amount, h.amount >= 1000 ? 0 : h.amount >= 1 ? 3 : 5)}
                    <br />
                    <span className="h-usd">{h.usd !== undefined ? fmtUsd(h.usd) : 'unpriced'}</span>
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="empty" data-testid="portfolio-empty">
              {reachable === 0
                ? 'No chain could be read right now — the RPCs did not answer. Pick a chain and token below by hand.'
                : `No holdings found on ${reachable} reachable chain${reachable === 1 ? '' : 's'}.`}
            </div>
          )}
          <div className="row small muted">
            <span>
              {reachable} of {portfolio.chains.length} chains read
              {portfolio.chains.some((c) => c.via === 'wallet') ? ' (one through the wallet)' : ''}
            </span>
            {portfolio.holdings.length > MAX_CHIPS && (
              <span>
                · showing the top {MAX_CHIPS} of {portfolio.holdings.length}
              </span>
            )}
            {portfolio.holdings.length > 0 && !portfolio.priced && (
              <span>· {pricesRateLimited() ? 'prices skipped (CoinGecko rate limit)' : 'prices unavailable'}</span>
            )}
            {portfolio.chains.some((c) => c.errors.length || c.note) && (
              <details className="plain">
                <summary>{unreachable ? `${unreachable} unreachable` : 'notes'}</summary>
                <ul className="tiny" style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                  {portfolio.chains
                    .filter((c) => c.errors.length || c.note)
                    .map((c) => (
                      <li key={c.chainId}>
                        <b>{c.name}</b> ({c.via}) — {[...c.errors, c.note].filter(Boolean).join('; ')}
                      </li>
                    ))}
                </ul>
              </details>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

/** A destination's native balance on Ethereum · Arbitrum · Base, cached for a minute. */
export function NativeBalances({ address, refreshKey }: { address: string; refreshKey: number }) {
  const { wallet, data } = useStore();
  const [reads, setReads] = useState<Record<number, NativeRead | undefined>>({});
  const { provider, chainId } = wallet;
  useEffect(() => {
    let alive = true;
    setReads({});
    for (const cid of DESTINATION_CHAINS) {
      nativeBalance(cid, address, provider ? { provider, chainId } : null, refreshKey > 0).then((r) => {
        if (alive) setReads((s) => ({ ...s, [cid]: r }));
      });
    }
    return () => {
      alive = false;
    };
  }, [address, provider, chainId, refreshKey]);
  return (
    <div className="row" style={{ gap: '6px 14px' }}>
      {DESTINATION_CHAINS.map((cid) => {
        const r = reads[cid];
        return (
          <span key={cid} className="bal-line" title={r?.error ?? (r ? `read via ${r.via}` : 'reading…')}>
            <span className="muted">{chainName(cid, data.chains)}</span>
            <b>{r === undefined ? '…' : r.value === null ? 'n/a' : fmtNumber(Number(formatEther(r.value)), 5)}</b>
            {r && r.value !== null && <span className="muted">ETH</span>}
          </span>
        );
      })}
    </div>
  );
}
