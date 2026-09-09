// Wallets: destinations of the signed-in account. Add by proving control (sign with THAT wallet)
// or generate a fresh keypair in the browser — key shown once, never stored, only the address
// registered.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Wallet as EthersWallet, getAddress } from 'ethers';
import { NativeBalances } from '../components/Portfolio';
import { SignInGate } from '../components/SignInGate';
import { api, errorText } from '../lib/api';
import { explorerAddress, fmtTime, shortAddr } from '../lib/format';
import { buildDestinationProof } from '../lib/siwe';
import type { Destination } from '../lib/types';
import { useStore } from '../state/store';

export function WalletsPage() {
  const { session } = useStore();
  const [list, setList] = useState<Destination[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [removing, setRemoving] = useState<string | null>(null);
  const [removeError, setRemoveError] = useState<{ address: string; text: string } | null>(null);
  const signedIn = session.session;

  const load = useCallback(async () => {
    if (!signedIn) return;
    try {
      const r = await api.destinations();
      setList(Array.isArray(r.destinations) ? r.destinations : []);
      setListError(null);
    } catch (e) {
      setListError(errorText(e));
    }
  }, [signedIn]);

  useEffect(() => {
    void load();
  }, [load, session.account?.destinations]);

  const remove = async (address: string) => {
    if (!window.confirm(`Remove ${address} from your destinations?`)) return;
    setRemoving(address);
    setRemoveError(null);
    try {
      await api.removeDestination(address);
      await load();
      void session.refreshAccount();
    } catch (e) {
      setRemoveError({ address, text: errorText(e) });
    } finally {
      setRemoving(null);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Wallets</h1>
          <p className="muted">
            Payouts go only to wallets registered here. Add a wallet you already control by signing a proof with it, or generate a fresh one
            in the browser. Balances are read client-side; Pgas.me stores only the address.
          </p>
        </div>
      </div>
      <SignInGate what="your wallets">
        <section className="card">
          <div className="card-head">
            <h2>Registered destinations</h2>
            <div className="row">
              <span className="tiny muted">balances on Ethereum · Arbitrum · Base</span>
              <button type="button" className="btn btn-sm" onClick={() => setRefreshKey((k) => k + 1)}>
                Refresh balances
              </button>
            </div>
          </div>
          {listError && <div className="banner banner-error">{listError}</div>}
          {!list && !listError && <p className="muted small">Loading…</p>}
          {list && list.length === 0 && <div className="empty">No destinations yet.</div>}
          {list && list.length > 0 && (
            <div className="list" data-testid="destinations">
              {list.map((d) => (
                <div key={d.address} className="list-row" data-address={d.address}>
                  <div className="stack-sm" style={{ flex: '1 1 320px', minWidth: 0 }}>
                    <div className="row" style={{ gap: 8 }}>
                      <span className={`badge-kind kind-${d.kind}`}>{d.kind}</span>
                      {d.label && <span className="strong">{d.label}</span>}
                      <a
                        className="address"
                        href={explorerAddress(1, d.address)}
                        target="_blank"
                        rel="noreferrer"
                        title="View on Etherscan"
                      >
                        {d.address}
                      </a>
                    </div>
                    <NativeBalances address={d.address} refreshKey={refreshKey} />
                    <span className="tiny muted">verified {fmtTime(d.verified_at)}</span>
                  </div>
                  <div className="stack-sm" style={{ alignItems: 'flex-end' }}>
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      disabled={removing === d.address}
                      onClick={() => remove(d.address)}
                    >
                      {removing === d.address ? 'Removing…' : 'Remove'}
                    </button>
                    {removeError?.address === d.address && (
                      <span className="error-text" data-testid="remove-error">
                        {removeError.text}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <div className="grid-2">
          <AddExisting onAdded={load} registered={list ?? []} />
          <GenerateNew onAdded={load} />
        </div>
      </SignInGate>
    </div>
  );
}

function AddExisting({ onAdded, registered }: { onAdded: () => Promise<void>; registered: Destination[] }) {
  const { wallet, session } = useStore();
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const connected = wallet.address;
  const isSignedInWallet = !!connected && !!session.session && connected.toLowerCase() === session.session.address.toLowerCase();
  const already = !!connected && registered.some((d) => d.address.toLowerCase() === connected.toLowerCase());

  const sign = async () => {
    if (!connected || !session.session) return;
    setBusy(true);
    setErr(null);
    setDone(null);
    try {
      const address = getAddress(connected);
      const { nonce } = await api.destinationNonce();
      const issued = new Date().toISOString();
      const message = buildDestinationProof({ accountId: session.session.account_id, address, nonce, issued });
      const signature = await wallet.personalSign(message, address);
      await api.addDestination({ address, kind: 'proven', nonce, issued, signature, ...(label.trim() ? { label: label.trim() } : {}) });
      setDone(address);
      setLabel('');
      await onAdded();
      void session.refreshAccount();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card" data-testid="add-existing">
      <div className="card-head">
        <h2>Add an existing wallet</h2>
      </div>
      <div className="stack">
        <ol className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
          <li>Switch the connected account in your wallet to the one you want to add (or pick it in the wallet picker).</li>
          <li>Sign the proof below with that wallet — off-chain, free, works with zero gas.</li>
        </ol>
        <div className="banner">
          <span className="muted small">Connected now:</span>
          <span className="mono">{connected ? shortAddr(connected, 8, 6) : 'no wallet'}</span>
          {isSignedInWallet && <span className="small muted">— this is the signed-in wallet; it is already registered as connected.</span>}
          {!isSignedInWallet && already && <span className="small muted">— already registered.</span>}
        </div>
        <div className="field">
          <label className="label" htmlFor="label-existing">
            Label (optional)
          </label>
          <input
            id="label-existing"
            className="input"
            value={label}
            maxLength={64}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="e.g. trading-3"
          />
        </div>
        <div className="row">
          <button
            type="button"
            className="btn btn-primary"
            disabled={!connected || isSignedInWallet || busy}
            onClick={sign}
            data-testid="prove-btn"
          >
            {busy ? 'Waiting for the signature…' : connected ? `Sign proof with ${shortAddr(connected)}` : 'Connect a wallet first'}
          </button>
          {!connected && (
            <button type="button" className="btn" onClick={wallet.openPicker}>
              Open wallet picker
            </button>
          )}
        </div>
        {err && <div className="banner banner-error">{err}</div>}
        {done && <div className="banner banner-ok">Registered {shortAddr(done, 8, 6)} as a proven destination.</div>}
      </div>
    </section>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    } catch {
      window.prompt('Copy this value', text); // clipboard blocked: let the user select it
    }
  };
  return (
    <button type="button" className="btn btn-sm" onClick={copy} aria-live="polite">
      {done ? 'Copied' : 'Copy'}
    </button>
  );
}

function GenerateNew({ onAdded }: { onAdded: () => Promise<void> }) {
  const { session } = useStore();
  const [fresh, setFresh] = useState<{ address: string; privateKey: string; mnemonic: string } | null>(null);
  const [saved, setSaved] = useState(false);
  const [label, setLabel] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const signer = useRef<EthersWallet | null>(null); // the ephemeral key lives here and nowhere else

  const generate = () => {
    const w = EthersWallet.createRandom();
    signer.current = new EthersWallet(w.privateKey);
    setFresh({ address: w.address, privateKey: w.privateKey, mnemonic: w.mnemonic?.phrase ?? '' });
    setSaved(false);
    setErr(null);
    setDone(null);
  };

  const discard = () => {
    signer.current = null;
    setFresh(null);
    setSaved(false);
  };

  const register = async () => {
    if (!fresh || !signer.current || !session.session) return;
    setBusy(true);
    setErr(null);
    try {
      const address = getAddress(fresh.address);
      const { nonce } = await api.destinationNonce();
      const issued = new Date().toISOString();
      const message = buildDestinationProof({ accountId: session.session.account_id, address, nonce, issued });
      const signature = await signer.current.signMessage(message);
      await api.addDestination({ address, kind: 'generated', nonce, issued, signature, ...(label.trim() ? { label: label.trim() } : {}) });
      setDone(address);
      discard();
      setLabel('');
      await onAdded();
      void session.refreshAccount();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card" data-testid="generate-new">
      <div className="card-head">
        <h2>Generate a new wallet</h2>
      </div>
      <div className="stack">
        <p className="small muted">
          A fresh keypair made in this browser. The private key and recovery phrase are shown once and never stored — Pgas.me keeps only the
          address. Save them before registering.
        </p>
        {!fresh ? (
          <div className="row">
            <button type="button" className="btn btn-primary" onClick={generate} data-testid="generate-btn">
              Generate new wallet
            </button>
          </div>
        ) : (
          <div className="stack" data-testid="generated">
            <div className="field">
              <span className="label">Address</span>
              <div className="key-box">
                <span data-testid="gen-address">{fresh.address}</span>
                <CopyButton text={fresh.address} />
              </div>
            </div>
            <div className="field">
              <span className="label">Private key</span>
              <div className="key-box">
                <span data-testid="gen-pk">{fresh.privateKey}</span>
                <CopyButton text={fresh.privateKey} />
              </div>
            </div>
            <div className="field">
              <span className="label">Recovery phrase</span>
              <div className="key-box">
                <span data-testid="gen-mnemonic">{fresh.mnemonic}</span>
                <CopyButton text={fresh.mnemonic} />
              </div>
            </div>
            <div className="field">
              <label className="label" htmlFor="label-gen">
                Label (optional)
              </label>
              <input
                id="label-gen"
                className="input"
                value={label}
                maxLength={64}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="e.g. fresh-1"
              />
            </div>
            <label className="check">
              <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid="saved-check" />
              <span className="small">I saved the private key and recovery phrase. Pgas.me cannot recover them.</span>
            </label>
            <div className="row">
              <button type="button" className="btn btn-primary" disabled={!saved || busy} onClick={register} data-testid="register-btn">
                {busy ? 'Registering…' : 'Register this wallet'}
              </button>
              <button type="button" className="btn btn-ghost" disabled={busy} onClick={discard}>
                Discard
              </button>
            </div>
          </div>
        )}
        {err && <div className="banner banner-error">{err}</div>}
        {done && (
          <div className="banner banner-ok">
            Registered {shortAddr(done, 8, 6)} as a generated destination. The key is no longer in memory.
          </div>
        )}
      </div>
    </section>
  );
}
