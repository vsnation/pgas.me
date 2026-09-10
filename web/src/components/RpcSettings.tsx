// T54 — the RPC settings popup. The admin's words (2026-09-10 16:47Z): "provide configuration popup
// where RPCs for all chains with dropdowns where user can select one of many".
//
// One row per chain the app scans, a dropdown of the endpoints it knows with the one it starts at
// preselected, "Custom…" for a URL of your own, and a health dot per endpoint carrying what the
// last probe found. `lib/rpc.ts` holds the choice and does the probing; this file only renders it.
//
// What it is NOT: a server setting. These endpoints are the ones the BROWSER calls. Pgas.me's API
// has its own and never sees these, and the copy at the top of the dialog says so, because a
// settings screen that lets someone believe they are re-pointing the service is worse than none.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { Modal } from './Modal';
import { CHAIN_META, chainIconUrl, chainName, isNonEvmChain } from '../lib/chains';
import {
  CUSTOM_OPTION,
  acceptCustomRpcUrl,
  activeRpcUrl,
  chainsWithRpcs,
  customRpcUrls,
  customisedChainIds,
  defaultRpcUrls,
  healthOf,
  knownRpcUrls,
  lastProbe,
  onRpcChange,
  probeRpcUrl,
  probeText,
  resetRpcChoices,
  selectRpcUrl,
  shortRpcUrl,
  type RpcHealth,
} from '../lib/rpc';
import { useStore } from '../state/store';
import type { Chain } from '../lib/types';
import './RpcSettings.css';

const OPEN_EVENT = 'pgas:open-rpc-settings';

/** Open the popup from anywhere — the header's own button, the portfolio, an unreadable chain. */
export function openRpcSettings(): void {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

/** The inline way in: used where a chain could not be read, and beside the portfolio's Refresh. */
export function RpcSettingsLink({ children, testId }: { children?: ReactNode; testId?: string }) {
  return (
    <button type="button" className="link-btn" data-testid={testId} onClick={openRpcSettings}>
      {children ?? 'RPC endpoints'}
    </button>
  );
}

function GearIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3h.1A1.7 1.7 0 0 0 10 3.1V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.6 1H23a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" />
    </svg>
  );
}

/** The header control, and the dialog it opens. One mount, so `openRpcSettings()` always has a home. */
export function RpcSettingsButton() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onOpen = () => setOpen(true);
    window.addEventListener(OPEN_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_EVENT, onOpen);
  }, []);
  return (
    <>
      <button
        type="button"
        className="btn btn-sm btn-ghost rpc-gear"
        data-testid="rpc-settings-button"
        aria-label="RPC endpoints"
        aria-haspopup="dialog"
        title="Which endpoints this browser reads chains through"
        onClick={() => setOpen(true)}
      >
        <GearIcon />
      </button>
      {/*
       * ⛔ Through a PORTAL, not inline. This button lives in `.header`, and `.header` has
       * `backdrop-filter: blur(10px)` — which makes it the containing block for every
       * `position: fixed` descendant. Rendered here, the dialog's `position: fixed; inset: 0`
       * backdrop measured 64px tall (the header's own height) instead of the viewport's, so the
       * panel was flex-centred inside a 64px box: its top sat at y = −317 and Playwright refused
       * every click on it with "element is outside of the viewport" (measured 2026-09-10 18:40Z,
       * `.modal` top −338, height 741, backdrop height 64). `document.body` is the only parent that
       * makes "fixed" mean the screen.
       */}
      {open && createPortal(<RpcSettingsDialog onClose={() => setOpen(false)} />, document.body)}
    </>
  );
}

/** Re-render whenever the choice or a probe changes: `lib/rpc.ts` is the state, this is the view. */
function useRpcVersion(): number {
  const [v, setV] = useState(0);
  useEffect(() => onRpcChange(() => setV((n) => n + 1)), []);
  return v;
}

interface RpcRow {
  chainId: number;
  name: string;
}

/**
 * The chains to show: the ones the app scans, which is the API's own chain list minus the two that
 * have no eth_* RPC to point anywhere. When that list has not loaded, every chain this app ships
 * endpoints for — an empty settings screen would be a worse answer than a complete one.
 */
function rowsFor(chains: Chain[]): RpcRow[] {
  const evm = chains.filter((c) => !isNonEvmChain(c.chain_id));
  if (evm.length) return evm.map((c) => ({ chainId: c.chain_id, name: c.name }));
  return chainsWithRpcs()
    .map((id) => ({ chainId: id, name: CHAIN_META[id]?.chainName ?? `Chain ${id}` }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function Dot({ url, chainId, testId }: { url: string | null; chainId: number; testId?: string }) {
  const health: RpcHealth = url ? healthOf(url, chainId) : 'unknown';
  const text = url ? probeText(url, chainId) : 'No endpoint';
  return (
    <span className={`rpc-dot rpc-dot-${health}`} data-health={health} data-testid={testId} title={text} role="img" aria-label={text} />
  );
}

/** The number under the dot: how long it took, or what it said instead of answering for this chain. */
function latencyText(url: string, chainId: number): string {
  const p = lastProbe(url);
  if (!p) return 'not checked';
  if (!p.ok) return p.error ?? 'no answer';
  if (p.chainId !== chainId) return `chain ${p.chainId}`;
  return `${Math.round(p.ms)} ms`;
}

function ChainRow({ row, nameOf }: { row: RpcRow; nameOf: (id: number) => string }) {
  const { chainId, name } = row;
  const [showAll, setShowAll] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const [customText, setCustomText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const urls = knownRpcUrls(chainId);
  const defaults = defaultRpcUrls(chainId);
  const custom = customRpcUrls(chainId);
  const active = activeRpcUrl(chainId);
  const label = `${name} (${chainId})`;

  /** The Check button: ask EVERY endpoint of this chain again, and show what each one said. */
  const checkAll = useCallback(async () => {
    setShowAll(true);
    setChecking(true);
    await Promise.all(urls.map((u) => probeRpcUrl(u)));
    if (alive.current) setChecking(false);
  }, [urls]);

  /**
   * Opening the list fills in the dots nobody has asked about yet — an endpoint that already has a
   * probe (the scanner runs the same one) keeps the answer it gave, because re-asking would replace
   * a real reading with a fresher one that says the same thing.
   */
  const showEndpoints = useCallback(() => {
    setShowAll(true);
    for (const u of urls) if (!lastProbe(u)) void probeRpcUrl(u);
  }, [urls]);

  const pick = (value: string) => {
    setError(null);
    if (value === CUSTOM_OPTION) {
      setCustomOpen(true);
      return;
    }
    setCustomOpen(false);
    // the head of the app's own order is "no choice", so it is stored as none rather than as itself
    selectRpcUrl(chainId, value === defaults[0] ? null : value);
    void probeRpcUrl(value);
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    const r = await acceptCustomRpcUrl(chainId, customText, label, nameOf);
    if (!alive.current) return;
    setBusy(false);
    if (r.ok) {
      setCustomOpen(false);
      setCustomText('');
      return;
    }
    setError(r.error);
  };

  return (
    <div className="rpc-row" data-testid={`rpc-chain-${chainId}`} data-active={active ?? ''}>
      <div className="rpc-row-head">
        {chainIconUrl(chainId) && <img className="rpc-chain-icon" src={chainIconUrl(chainId)!} alt="" width={18} height={18} />}
        <span className="strong">{name}</span>
        <span className="tiny muted num">{chainId}</span>
        <Dot url={active} chainId={chainId} testId={`rpc-health-${chainId}`} />
      </div>
      <div className="rpc-row-body">
        <label className="field rpc-field">
          <span className="sr-only">Endpoint for {name}</span>
          <select
            className="select"
            data-testid={`rpc-select-${chainId}`}
            value={customOpen ? CUSTOM_OPTION : (active ?? '')}
            onChange={(e) => pick(e.target.value)}
          >
            {!urls.length && (
              <option value="" disabled>
                Pgas.me ships no endpoint for this chain — add one
              </option>
            )}
            {urls.map((u) => (
              <option key={u} value={u}>
                {shortRpcUrl(u)}
                {u === defaults[0] ? ' — default' : custom.includes(u) ? ' — yours' : ''}
              </option>
            ))}
            <option value={CUSTOM_OPTION}>Custom…</option>
          </select>
        </label>
        <button
          type="button"
          className="btn btn-sm"
          data-testid={`rpc-check-${chainId}`}
          onClick={() => void checkAll()}
          disabled={checking}
        >
          {checking ? 'Checking…' : 'Check'}
        </button>
      </div>

      {customOpen && (
        <div className="rpc-custom stack-sm">
          <label className="field">
            <span className="label">Your own endpoint for {name}</span>
            <input
              className="input mono"
              data-testid={`rpc-custom-${chainId}`}
              value={customText}
              placeholder="https://your-node.example/rpc"
              spellCheck={false}
              autoComplete="off"
              aria-invalid={error ? 'true' : undefined}
              onChange={(e) => setCustomText(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && !busy && void save()}
            />
          </label>
          <div className="row rpc-custom-actions">
            <button
              type="button"
              className="btn btn-sm btn-primary"
              data-testid={`rpc-custom-save-${chainId}`}
              onClick={() => void save()}
              disabled={busy}
            >
              {busy ? 'Checking…' : 'Use this endpoint'}
            </button>
            <button
              type="button"
              className="link-btn tiny"
              onClick={() => {
                setCustomOpen(false);
                setError(null);
              }}
            >
              Cancel
            </button>
            <span className="tiny muted">It is only saved once it answers for {label}.</span>
          </div>
          {error && (
            <p className="error-text small" role="alert" data-testid={`rpc-custom-error-${chainId}`}>
              {error}
            </p>
          )}
        </div>
      )}

      {!showAll && urls.length > 1 && (
        <button type="button" className="link-btn tiny rpc-more" aria-expanded={false} onClick={showEndpoints}>
          Show all {urls.length} endpoints
        </button>
      )}
      {showAll && (
        <ul className="rpc-endpoints" data-testid={`rpc-endpoints-${chainId}`}>
          {urls.map((u, i) => (
            <li
              key={u}
              className={`rpc-endpoint${u === active ? ' active' : ''}`}
              data-testid={`rpc-endpoint-${chainId}-${i}`}
              data-health={healthOf(u, chainId)}
            >
              <Dot url={u} chainId={chainId} />
              <span className="mono tiny wrap rpc-endpoint-url">{shortRpcUrl(u)}</span>
              <span className="tiny muted nowrap">{latencyText(u, chainId)}</span>
              {u === active ? (
                <span className="pill pill-teal tiny">in use</span>
              ) : (
                <button type="button" className="link-btn tiny" onClick={() => pick(u)}>
                  Use
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RpcSettingsDialog({ onClose }: { onClose: () => void }) {
  const { data } = useStore();
  useRpcVersion();
  const [resets, setResets] = useState(0);
  const [checkingAll, setCheckingAll] = useState(false);
  const rows = useMemo(() => rowsFor(data.chains), [data.chains]);
  const nameOf = useCallback((id: number) => chainName(id, data.chains), [data.chains]);
  const changed = customisedChainIds().filter((id) => rows.some((r) => r.chainId === id));

  const checkAll = async () => {
    setCheckingAll(true);
    await Promise.all(rows.map((r) => (activeRpcUrl(r.chainId) ? probeRpcUrl(activeRpcUrl(r.chainId)!) : Promise.resolve(null))));
    setCheckingAll(false);
  };

  return (
    <Modal title="RPC endpoints" onClose={onClose} testId="rpc-settings" width={640} className="rpc-modal">
      <div className="stack rpc-settings">
        <p className="small muted" data-testid="rpc-settings-note">
          These endpoints are used by your browser to read balances and confirm transactions. Pgas.me&apos;s own servers use their own.
        </p>
        <p className="tiny muted">
          The one you pick is tried first. If it stops answering, the others below it are tried in order — a choice here is a starting
          point, never the only endpoint.
        </p>
        <div className="row rpc-toolbar">
          <button type="button" className="btn btn-sm" data-testid="rpc-check-all" onClick={() => void checkAll()} disabled={checkingAll}>
            {checkingAll ? 'Checking…' : 'Check all'}
          </button>
          <button
            type="button"
            className="btn btn-sm"
            data-testid="rpc-reset"
            onClick={() => {
              resetRpcChoices();
              setResets((n) => n + 1);
            }}
            disabled={!changed.length}
          >
            Reset to defaults
          </button>
          <span className="tiny muted" data-testid="rpc-changed">
            {changed.length ? `${changed.length} chain${changed.length === 1 ? '' : 's'} changed` : 'Every chain is on its default.'}
          </span>
        </div>
        {!data.chains.length && (
          <p className="tiny muted">
            The chain list has not loaded, so this is every chain Pgas.me ships endpoints for rather than the ones it is scanning.
          </p>
        )}
        <div className="rpc-list">
          {rows.map((r) => (
            <ChainRow key={`${r.chainId}:${resets}`} row={r} nameOf={nameOf} />
          ))}
        </div>
      </div>
    </Modal>
  );
}
