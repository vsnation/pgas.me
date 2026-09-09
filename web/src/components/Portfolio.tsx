// Balances read client-side: the connected wallet's holdings across every chain the API lists —
// the Deposit page's tap-to-pay chips. (The per-destination native balances went with the Wallets
// page on 2026-09-09: a payout address is typed now, not registered, so there is no list to price.)
// The chip row is buybeam.my's: one horizontally scrolling line of pills, token logo with the chain
// badge on its corner, symbol over the USD value (or the balance when nothing priced it).
import { useCallback, useEffect, useRef, useState } from 'react';
import { chainIconUrl } from '../lib/chains';
import { fmtAgo, fmtNumber, fmtUsd } from '../lib/format';
import {
  loadCachedPortfolio,
  pricesRateLimited,
  scanPortfolio,
  sortForDisplay,
  type ChainScan,
  type Holding,
  type Portfolio as PortfolioData,
} from '../lib/portfolio';
import { useStore } from '../state/store';

const MAX_CHIPS = 24;

/** buybeam's portfolioBalanceLabel: the USD value once it is worth a cent, else the raw balance. */
function chipLabel(h: Holding): string {
  if (h.usd !== undefined && h.usd >= 0.01) return fmtUsd(h.usd);
  return fmtNumber(h.amount, h.amount >= 1 ? 2 : 6);
}

function ChipIcons({ h }: { h: Holding }) {
  const [tokenBroken, setTokenBroken] = useState(false);
  const [chainBroken, setChainBroken] = useState(false);
  const chainIcon = chainIconUrl(h.chainId);
  return (
    <span className="portfolio-chip-icons">
      {h.logo && !tokenBroken ? (
        <img className="portfolio-chip-token" src={h.logo} alt="" loading="lazy" onError={() => setTokenBroken(true)} />
      ) : (
        <span className="portfolio-chip-token logo-fallback">{h.symbol.slice(0, 3).toUpperCase()}</span>
      )}
      {chainIcon && !chainBroken && (
        <img className="portfolio-chip-chain" src={chainIcon} alt="" loading="lazy" onError={() => setChainBroken(true)} />
      )}
    </span>
  );
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
  const [live, setLive] = useState<ChainScan[]>([]); // chains that have already answered, mid-scan
  const [scanning, setScanning] = useState(false);
  const [progress, setProgress] = useState<[number, number]>([0, 0]);
  const [error, setError] = useState<string | null>(null);
  const run = useRef(0);
  const row = useRef<HTMLDivElement>(null);
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
      setLive([]);
      setProgress([0, chains.length]);
      try {
        const p = await scanPortfolio(
          address,
          chains,
          provider ? { provider, chainId } : null,
          (d, t) => {
            if (id === run.current) setProgress([d, t]);
          },
          (s) => {
            if (id === run.current) setLive((cur) => [...cur, s]);
          },
        );
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

  // mid-scan the chips come from the chains that already answered; when the scan lands the priced,
  // fully sorted portfolio replaces them
  const scans = portfolio ? portfolio.chains : live;
  const holdings = portfolio ? portfolio.holdings : sortForDisplay(live.flatMap((s) => s.holdings));
  const evm = scans.filter((c) => !c.nonEvm);
  const reachable = evm.filter((c) => c.via !== 'none').length;
  const nonEvm = scans.filter((c) => c.nonEvm).length;
  const chips = holdings.slice(0, MAX_CHIPS);
  const firstKey = chips[0]?.key;
  const totalUsd = holdings.reduce((s, h) => s + (h.usd ?? 0), 0);
  const settled = !!portfolio && !scanning;

  // How the scan went — chains read, chains skipped, unreachable endpoints, missing prices — is not
  // something a depositor acts on, so it is the Refresh button's tooltip rather than a line of text
  // under the chips. The empty state still says it out loud when NOTHING could be read.
  const scanNote = [
    `read ${reachable} of ${evm.length} EVM chains${scans.some((c) => c.via === 'wallet') ? ' (one through the wallet)' : ''}`,
    nonEvm ? `${nonEvm} not scanned (non-EVM)` : '',
    holdings.length > MAX_CHIPS ? `showing the top ${MAX_CHIPS} of ${holdings.length}` : '',
    portfolio && !scanning && holdings.length > 0 && !portfolio.priced
      ? pricesRateLimited()
        ? 'prices skipped (CoinGecko rate limit)'
        : 'prices unavailable'
      : '',
    ...scans
      .filter((c) => c.errors.length || c.note)
      .map((c) => `${c.name} (${c.via}) — ${[...c.errors, c.note].filter(Boolean).join('; ')}`),
  ]
    .filter(Boolean)
    .join(' · ');

  // chains land one by one and the list re-sorts when prices arrive; Chrome keeps whatever chip was
  // leftmost in place, which leaves the row scrolled past the biggest holdings. Whenever a new chip
  // takes the front, put the row back at the start — a scroll the user made himself is untouched.
  useEffect(() => {
    if (row.current) row.current.scrollLeft = 0;
  }, [firstKey]);

  return (
    <section className="card" data-testid="portfolio">
      <div className="card-head">
        <div>
          <h2>Your portfolio</h2>
        </div>
        <div className="row">
          {portfolio?.priced && totalUsd > 0 && (
            <span className="num strong" title="Sum of priced holdings">
              {fmtUsd(totalUsd)}
            </span>
          )}
          {settled && <span className="tiny muted">as of {fmtAgo(portfolio.at)}</span>}
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => scan(true)}
            disabled={scanning || !chains.length}
            title={scanNote ? `Re-read every chain — ${scanNote}` : 'Re-read every chain'}
            data-scan-note={scanNote || undefined}
          >
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

      {(chips.length > 0 || settled) && (
        <div className="stack">
          {chips.length ? (
            <div className="portfolio-chips" data-testid="portfolio-chips" ref={row}>
              {chips.map((h) => (
                <button
                  key={h.key}
                  type="button"
                  className={`portfolio-chip${selectedKey === h.key ? ' selected' : ''}`}
                  onClick={() => onPick(h)}
                  title={`${h.symbol} on ${h.chainName}`}
                >
                  <ChipIcons h={h} />
                  <span className="portfolio-chip-info">
                    <span className="portfolio-chip-sym">{h.symbol}</span>
                    <span className="portfolio-chip-usd">{chipLabel(h)}</span>
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
        </div>
      )}
    </section>
  );
}
