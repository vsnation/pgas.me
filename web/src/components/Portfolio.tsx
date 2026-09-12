// Balances read client-side: the connected wallet's holdings across every chain the API lists —
// the money page's tap-to-pay chips, and the context card above the Deposit panel.
//
// T57 (2026-09-12) rebuilt the row itself. The admin's screenshot showed four entries all labelled
// ETH with tiny chain badges and no chain names, in two different formats ($ for most, a bare `1`
// for a token nothing could price), with "as of 44 h ago" whispered in grey beside a quiet
// Refresh. So:
//
//   · GROUPED BY CHAIN, with the chain NAMED. A badge 13 px across is not an answer to "which
//     ETH is this?" — four rows called ETH are four different holdings and the page has to say so.
//   · ONE VALUE FORMAT: the dollar figure, with the token amount under it. A holding nothing could
//     price shows the amount and the words "no price" — which is what is true, where a bare
//     number was a quantity masquerading as a value.
//   · TOP 6, then "+N more". Sorted by value, so the six are the six that matter; the rest are one
//     press away rather than a sideways scroll nobody finds.
//   · STALENESS IS LOUD WHEN IT MATTERS. Under five minutes it is quiet grey; older than that it
//     is amber and Refresh becomes the prominent control. ⛔ Refresh is still the ONLY thing that
//     starts a scan (T31 F) — saying an old number is old is not the same as re-reading thirty
//     public RPCs because a page was opened.
//   · "Endpoints" is gone. It duplicated the header's gear and put jargon beside money controls;
//     the gear is the one entry point. The links that appear only when a chain COULD NOT be read
//     stay — those are a way out of a failure, not a second copy of a control.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
import { RpcSettingsLink } from './RpcSettings';
import { useStore } from '../state/store';

/** How many chips are shown before the disclosure. Sorted by value, so these are the six. */
const TOP_CHIPS = 6;
/** Older than this and the age is worth saying out loud (amber), not whispering. */
const STALE_MS = 5 * 60 * 1000;

/** Has anything priced this holding? A value under a cent is not a value worth two decimals. */
function isPriced(h: Holding): boolean {
  return h.usd !== undefined && h.usd >= 0.01;
}

/** The token amount, at the precision the size of it deserves. */
function chipAmount(h: Holding): string {
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

/** The holdings on screen, in chain order of first appearance — which is value order, since the
 *  list arrives sorted by value and a chain's place is its biggest holding's. */
function groupByChain(hs: Holding[]): { chainId: number; name: string; list: Holding[] }[] {
  const out: { chainId: number; name: string; list: Holding[] }[] = [];
  for (const h of hs) {
    const hit = out.find((g) => g.chainId === h.chainId);
    if (hit) hit.list.push(h);
    else out.push({ chainId: h.chainId, name: h.chainName, list: [h] });
  }
  return out;
}

export function Portfolio({
  address,
  selectedKey,
  onPick,
  onLoaded,
}: {
  address: string;
  selectedKey?: string | null;
  onPick: (h: Holding) => void;
  /**
   * The scan result, published to whoever composes this card. ONE writer: this component scans and
   * caches, the Deposit body reads what it publishes — the chips and the "Pay with" picker are the
   * same holdings, not two lists that can disagree (T31 G).
   */
  onLoaded?: (p: PortfolioData | null) => void;
}) {
  const { wallet, data } = useStore();
  const { chains, loading: chainsLoading, error: dataError } = data;
  const [portfolio, setPortfolio] = useState<PortfolioData | null>(null);
  const [live, setLive] = useState<ChainScan[]>([]); // chains that have already answered, mid-scan
  const [scanning, setScanning] = useState(false);
  const [progress, setProgress] = useState<[number, number]>([0, 0]);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const run = useRef(0);
  const { provider, chainId } = wallet;

  /**
   * T31 F (admin, 2026-09-10): "You need to cache balances result to avoid update during same or
   * next update session, if user can't see some balances, he can just click refresh btn."
   *
   * So a cached result is rendered whatever its age — no TTL, no automatic re-read on a tab switch,
   * a reconnect or the next session — and the ONLY scan that happens without the Refresh button is
   * the first one for an address this browser has never scanned. Thirty public RPCs are not
   * something to spend because a page was opened again.
   */
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

  // A NEW address starts from nothing (two wallets keep two caches, and neither is shown for the
  // other); everything else — a chain switch in the wallet, a re-render — must not blank the chips
  // that are already on screen and must never start a scan of its own.
  const shownFor = useRef<string | null>(null);
  useEffect(() => {
    if (shownFor.current !== address) {
      shownFor.current = address;
      setPortfolio(null);
      setLive([]);
      setExpanded(false);
    }
    void scan(false);
  }, [address, chains, scan]);

  useEffect(() => {
    onLoaded?.(portfolio);
  }, [portfolio, onLoaded]);

  // mid-scan the chips come from the chains that already answered; when the scan lands the priced,
  // fully sorted portfolio replaces them
  const scans = portfolio ? portfolio.chains : live;
  const holdings = portfolio ? portfolio.holdings : sortForDisplay(live.flatMap((s) => s.holdings));
  const evm = scans.filter((c) => !c.nonEvm);
  const reachable = evm.filter((c) => c.via !== 'none').length;
  const nonEvm = scans.filter((c) => c.nonEvm).length;
  const chips = expanded ? holdings : holdings.slice(0, TOP_CHIPS);
  const groups = useMemo(() => groupByChain(chips), [chips]);
  const hidden = holdings.length - chips.length;
  const totalUsd = holdings.reduce((s, h) => s + (h.usd ?? 0), 0);
  const settled = !!portfolio && !scanning;
  /** An old number needs a date on it — and, past five minutes, one nobody has to look for. */
  const stale = settled && Date.now() - portfolio.at > STALE_MS;
  /** EVM chains no endpoint could be read through — the failure a different endpoint might fix. */
  const unreadable = evm.filter((c) => c.via === 'none');

  // How the scan went — chains read, chains skipped, unreachable endpoints, missing prices — is not
  // something a depositor acts on, so it is the Refresh button's tooltip rather than a line of text
  // under the chips. The empty state still says it out loud when NOTHING could be read.
  const scanNote = [
    `read ${reachable} of ${evm.length} EVM chains${scans.some((c) => c.via === 'wallet') ? ' (one through the wallet)' : ''}`,
    nonEvm ? `${nonEvm} not scanned (non-EVM)` : '',
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

  return (
    <section className="card portfolio-card" data-testid="portfolio">
      <div className="card-head">
        <h2>Your wallet</h2>
        <div className="row">
          {portfolio?.priced && totalUsd > 0 && (
            <span className="num strong" title="Sum of priced holdings">
              {fmtUsd(totalUsd)}
            </span>
          )}
          {settled && (
            <span
              className={`tiny portfolio-as-of${stale ? '' : ' muted'}`}
              data-testid="portfolio-as-of"
              data-stale={stale ? 'yes' : 'no'}
              title={new Date(portfolio.at).toLocaleString()}
            >
              updated {fmtAgo(portfolio.at)}
            </span>
          )}
          <button
            type="button"
            className={`btn btn-sm${stale ? ' btn-primary' : ''}`}
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
      {error && (
        <div className="banner banner-error">
          Scan failed: {error} <RpcSettingsLink>Try a different endpoint</RpcSettingsLink>
        </div>
      )}
      {/* An RPC that could not be read is the ONE scan note a person can do something about, so it
          is the one that gets a line of its own — with the way to act on it beside it (T54). */}
      {settled && unreadable.length > 0 && (
        <p className="tiny muted" data-testid="portfolio-unreadable">
          Could not read {unreadable.map((c) => c.name).join(', ')} — the endpoint did not answer.{' '}
          <RpcSettingsLink testId="rpc-settings-unreadable">Choose another</RpcSettingsLink>
        </p>
      )}

      {(chips.length > 0 || settled) && (
        <div className="stack-sm">
          {chips.length ? (
            <div className="portfolio-chips" data-testid="portfolio-chips" data-holdings={holdings.length} data-shown={chips.length}>
              {groups.map((g) => (
                <div className="pf-group" key={g.chainId} data-pf-chain={g.chainId}>
                  <div className="pf-group-head">{g.name}</div>
                  <div className="pf-group-chips">
                    {g.list.map((h) => {
                      const priced = isPriced(h);
                      const amount = chipAmount(h);
                      return (
                        <button
                          key={h.key}
                          type="button"
                          className={`portfolio-chip${selectedKey === h.key ? ' selected' : ''}`}
                          onClick={() => onPick(h)}
                          title={`${h.symbol} on ${h.chainName}`}
                          data-holding={h.key}
                          data-priced={priced ? 'yes' : 'no'}
                        >
                          <ChipIcons h={h} />
                          <span className="portfolio-chip-info">
                            {/* ⛔ ONE FORMAT. The dollar figure when there is one, the amount when
                                there is not — and the class that names it USD is on the element
                                only while it really is USD. */}
                            <span className={`portfolio-chip-value${priced ? ' portfolio-chip-usd' : ''}`}>
                              {priced ? fmtUsd(h.usd) : amount}
                            </span>
                            <span className="portfolio-chip-line">
                              <span className="portfolio-chip-sym">{h.symbol}</span>
                              <span className="portfolio-chip-amt">{priced ? amount : 'no price'}</span>
                            </span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="empty" data-testid="portfolio-empty">
              {reachable === 0
                ? 'No chain could be read right now — the RPCs did not answer. Pick a chain and token below by hand.'
                : `No holdings found on ${reachable} reachable chain${reachable === 1 ? '' : 's'}.`}
            </div>
          )}
          {(hidden > 0 || expanded) && (
            <button
              type="button"
              className="link-btn tiny portfolio-more"
              data-testid={expanded ? 'portfolio-less' : 'portfolio-more'}
              aria-expanded={expanded}
              onClick={() => setExpanded((o) => !o)}
            >
              {expanded ? 'Show fewer' : `+${hidden} more`}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
