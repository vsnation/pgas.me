// Deposit: portfolio → target asset + source chain/token/amount → quote → (armed) approve/deposit
// → status timeline. When the Beam wallet is not armed the quote is a preview and nothing is sent.
//
// Three quote modes (API_CONTRACT.md): `dln` is the cross-chain order; `direct` skips deBridge and
// pays the pipe itself; `swap` is a single-chain DLN swap into the user's OWN wallet, after which we
// re-quote what actually arrived and continue as `direct`. A swap tx is never registered as a deposit.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BrowserProvider, Interface, formatUnits } from 'ethers';
import { Portfolio } from '../components/Portfolio';
import { SignInGate } from '../components/SignInGate';
import { depositStatusLabel } from '../components/Status';
import { api, errorText } from '../lib/api';
import { NATIVE_ADDRESS, getFallbackProvider, isNativeToken } from '../lib/chains';
import { explorerTx, fmtDuration, fmtUnits, fmtUsd, parseAmount, toDate } from '../lib/format';
import { loadCachedPortfolio, loadTokens, type Holding } from '../lib/portfolio';
import type { Asset, AssetKey, Chain, Deposit, Quote, Token } from '../lib/types';
import { useStore } from '../state/store';

const QUOTE_DEBOUNCE_MS = 500;
const QUOTE_TTL_FALLBACK_S = 30; // DLN quotes live ~30 s; used when the API sends no expires_at
const MIN_ETH_GROTH = 2_000_000; // 0.02 ETH-equivalent product minimum

type Stage = 'idle' | 'approving' | 'approved' | 'swapping' | 'swap-wait' | 'sending' | 'registering' | 'tracking';

const ERC20 = new Interface(['function balanceOf(address owner) view returns (uint256)']);

/** A read-only provider for Ethereum: the wallet when it is already there, else a public RPC. */
async function ethereumProvider(provider: unknown, chainId: number | null) {
  if (provider && chainId === 1) return new BrowserProvider(provider as never, 1);
  return await getFallbackProvider(1);
}

/** The wallet's balance of the swap's target token, or null when nothing could read it. */
async function readBalanceOnEthereum(token: string, owner: string, provider: unknown, chainId: number | null): Promise<bigint | null> {
  try {
    const p = await ethereumProvider(provider, chainId);
    if (!p) return null;
    if (isNativeToken(token)) return await p.getBalance(owner);
    const res = await p.call({ to: token, data: ERC20.encodeFunctionData('balanceOf', [owner]) });
    return res && res !== '0x' ? BigInt(ERC20.decodeFunctionResult('balanceOf', res)[0] as bigint) : null;
  } catch {
    return null; // an unreadable balance is not evidence of anything — the caller uses `next.amount`
  }
}

function assetDecimals(assets: Asset[], key: AssetKey): number {
  return assets.find((a) => a.key === key)?.decimals ?? (key === 'WBTC' ? 8 : 18);
}

function expiresInSeconds(q: Quote, receivedAt: number): number {
  const d = toDate(q.expires_at);
  if (d) return Math.max(0, Math.round((d.getTime() - Date.now()) / 1000));
  return Math.max(0, QUOTE_TTL_FALLBACK_S - Math.round((Date.now() - receivedAt) / 1000));
}

export function DepositPage() {
  const { wallet, session, data } = useStore();
  const { chains, assets } = data;

  const [target, setTarget] = useState<AssetKey>('ETH');
  const [chainId, setChainId] = useState<number | null>(null);
  const [tokens, setTokens] = useState<Token[]>([]);
  const [tokensLoading, setTokensLoading] = useState(false);
  const [tokensError, setTokensError] = useState<string | null>(null);
  const [token, setToken] = useState<Token | null>(null);
  const [amount, setAmount] = useState('');
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [maxRaw, setMaxRaw] = useState<bigint | null>(null);

  const [quote, setQuote] = useState<Quote | null>(null);
  const [quoteAt, setQuoteAt] = useState(0);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [expiresIn, setExpiresIn] = useState(0);
  const [requoteTick, setRequoteTick] = useState(0);

  const [stage, setStage] = useState<Stage>('idle');
  const [flowError, setFlowError] = useState<string | null>(null);
  const [approveHash, setApproveHash] = useState<string | null>(null);
  const [txHash, setTxHash] = useState<string | null>(null);
  const [depositId, setDepositId] = useState<string | null>(null);
  const [swapHash, setSwapHash] = useState<string | null>(null);
  const [swapDone, setSwapDone] = useState<{ from: string; to: string; amount: string } | null>(null);

  const sortedChains = useMemo(
    () => [...chains].sort((a, b) => (a.chain_id === 1 ? -1 : b.chain_id === 1 ? 1 : a.name.localeCompare(b.name))),
    [chains],
  );
  const chain: Chain | undefined = useMemo(() => chains.find((c) => c.chain_id === chainId), [chains, chainId]);
  const targetAsset = assets.find((a) => a.key === target);
  const outDecimals = assetDecimals(assets, target);
  const busy = stage === 'approving' || stage === 'sending' || stage === 'registering' || stage === 'swapping' || stage === 'swap-wait';
  const mode = quote?.mode ?? 'dln';
  const rawAmount = token ? parseAmount(amount, token.decimals) : null;

  const resetFlow = useCallback(() => {
    setStage('idle');
    setFlowError(null);
    setApproveHash(null);
    setTxHash(null);
    setDepositId(null);
    setSwapHash(null);
    setSwapDone(null);
  }, []);

  // default chain: the wallet's if supported, else Ethereum
  useEffect(() => {
    if (chainId !== null || !chains.length) return;
    const w = wallet.chainId;
    if (w !== null && chains.some((c) => c.chain_id === w)) setChainId(w);
    else setChainId(chains.find((c) => c.chain_id === 1)?.chain_id ?? chains[0].chain_id);
  }, [chains, chainId, wallet.chainId]);

  // token list per chain (native first; keep the current token when it exists on the new chain)
  useEffect(() => {
    if (chainId === null) return;
    let alive = true;
    setTokensLoading(true);
    setTokensError(null);
    loadTokens(chainId)
      .then((list) => {
        if (!alive) return;
        setTokens(list);
        setToken((cur) => {
          const native = list.find((t) => isNativeToken(t.address)) ?? null;
          if (!cur) return native ?? list[0] ?? null;
          if (isNativeToken(cur.address)) return native ?? cur;
          return list.find((t) => t.address.toLowerCase() === cur.address.toLowerCase()) ?? cur;
        });
      })
      .catch((e) => {
        if (!alive) return;
        setTokens([]);
        setTokensError(errorText(e));
      })
      .finally(() => alive && setTokensLoading(false));
    return () => {
      alive = false;
    };
  }, [chainId]);

  // "Max" needs a balance: the cached portfolio has it when the pair was scanned
  useEffect(() => {
    if (!wallet.address || !token || chainId === null) return;
    const p = loadCachedPortfolio(wallet.address, 10 * 60 * 1000);
    const h = p?.holdings.find((x) => x.chainId === chainId && x.address.toLowerCase() === token.address.toLowerCase());
    setMaxRaw(h ? h.raw : null);
  }, [wallet.address, token, chainId]);

  const onPick = useCallback(
    (h: Holding) => {
      setChainId(h.chainId);
      setSelectedKey(h.key);
      setMaxRaw(h.raw);
      setToken({ address: h.native ? NATIVE_ADDRESS : h.address, symbol: h.symbol, name: h.name, decimals: h.decimals, logo: h.logo });
      setAmount(formatUnits(h.raw, h.decimals));
      resetFlow();
    },
    [resetFlow],
  );

  // ---- quote: debounced, abortable, re-quoted automatically when it expires ----
  const sessionToken = session.session?.token ?? null;
  const canQuote = !!sessionToken && !!wallet.address && !!chain && !!token && rawAmount !== null && stage !== 'tracking';
  const rawAmountKey = rawAmount?.toString() ?? '';
  const srcChain = chain?.chain_id; // the API takes EVM ids and maps to deBridge's internal id itself
  const srcToken = token?.address;
  const sender = wallet.address;
  useEffect(() => {
    if (!canQuote || srcChain === undefined || !srcToken || !rawAmountKey || !sender) {
      setQuote(null);
      setQuoteError(null);
      setQuoteLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setQuoteLoading(true);
    setQuoteError(null);
    const t = setTimeout(async () => {
      try {
        const q = await api.quote(
          { src_chain_id: srcChain, src_token: srcToken, amount: rawAmountKey, target_asset: target, sender },
          ctrl.signal,
        );
        if (ctrl.signal.aborted) return;
        setQuote(q);
        setQuoteAt(Date.now());
        setExpiresIn(expiresInSeconds(q, Date.now()));
      } catch (e) {
        if (ctrl.signal.aborted || (e as Error)?.name === 'AbortError') return;
        setQuote(null);
        setQuoteError(errorText(e));
      } finally {
        if (!ctrl.signal.aborted) setQuoteLoading(false);
      }
    }, QUOTE_DEBOUNCE_MS);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [canQuote, srcChain, srcToken, rawAmountKey, target, sender, sessionToken, requoteTick]);

  useEffect(() => {
    if (!quote) return;
    const t = setInterval(() => {
      const left = expiresInSeconds(quote, quoteAt);
      setExpiresIn(left);
      if (left <= 0 && !busy && stage !== 'tracking') setRequoteTick((n) => n + 1);
    }, 1000);
    return () => clearInterval(t);
  }, [quote, quoteAt, busy, stage]);

  // ---- approve / deposit / register ----
  const approve = async () => {
    if (!quote?.approval) return;
    setFlowError(null);
    setStage('approving');
    try {
      const iface = new Interface(['function approve(address spender, uint256 amount)']);
      const hash = await wallet.sendTransaction({
        to: quote.approval.token,
        data: iface.encodeFunctionData('approve', [quote.approval.spender, BigInt(quote.approval.amount)]),
        chainId: data.resolveEvmChainId(quote.approval.chain_id),
      });
      setApproveHash(hash);
      if (wallet.provider) {
        // best effort: wait for the approval to be mined; the wallet's RPC may not expose receipts
        await new BrowserProvider(wallet.provider).waitForTransaction(hash, 1, 120_000).catch(() => null);
      }
      setStage('approved');
    } catch (e) {
      setFlowError(errorText(e));
      setStage('idle');
    }
  };

  const register = async (q: Quote, hash: string) => {
    setStage('registering');
    const res = await api.registerDeposit(q.quote_id, hash);
    setDepositId(res.deposit_id);
    setStage('tracking');
    void session.refreshAccount();
  };

  const send = async () => {
    if (!quote?.tx) return;
    setFlowError(null);
    setStage('sending');
    let hash: string | null = null;
    try {
      hash = await wallet.sendTransaction({
        to: quote.tx.to,
        data: quote.tx.data,
        value: quote.tx.value,
        chainId: data.resolveEvmChainId(quote.tx.chain_id),
      });
      setTxHash(hash);
      await register(quote, hash);
    } catch (e) {
      setFlowError(errorText(e));
      setStage(hash ? 'registering' : quote.approval ? 'approved' : 'idle');
    }
  };

  // ---- swap (mode "swap"): one DLN swap into the user's own wallet, then a fresh `direct` quote ----
  const runSwap = async () => {
    if (!quote?.swap_tx || !quote.next || !targetAsset || !sender) return;
    const nextToken = quote.next.src_token;
    const fromSymbol = quote.estimate.src.symbol;
    setFlowError(null);
    const before = await readBalanceOnEthereum(nextToken, sender, wallet.provider, wallet.chainId);
    let hash: string | null = null;
    try {
      setStage('swapping');
      hash = await wallet.sendTransaction({
        to: quote.swap_tx.to,
        data: quote.swap_tx.data,
        value: quote.swap_tx.value,
        chainId: data.resolveEvmChainId(quote.swap_tx.chain_id),
      });
      setSwapHash(hash);
      setStage('swap-wait');
      const p = await ethereumProvider(wallet.provider, 1);
      if (p) await p.waitForTransaction(hash, 1, 180_000);
      // what actually arrived beats what was estimated; when nothing can read it, take `next.amount`
      const after = await readBalanceOnEthereum(nextToken, sender, wallet.provider, 1);
      let received = BigInt(quote.next.amount);
      if (before !== null && after !== null && after > before) received = after - before;
      setChainId(1);
      setSelectedKey(null);
      setToken({ address: nextToken, symbol: targetAsset.symbol, name: targetAsset.symbol, decimals: targetAsset.decimals });
      setAmount(formatUnits(received, targetAsset.decimals));
      setMaxRaw(received);
      setApproveHash(null);
      setSwapDone({ from: fromSymbol, to: targetAsset.symbol, amount: formatUnits(received, targetAsset.decimals) });
      setStage('idle');
    } catch (e) {
      setFlowError(errorText(e));
      setStage(hash ? 'swap-wait' : quote.approval ? 'approved' : 'idle');
    }
  };

  const retryRegister = async () => {
    if (!quote || !txHash) return;
    setFlowError(null);
    try {
      await register(quote, txHash);
    } catch (e) {
      setFlowError(errorText(e));
    }
  };

  const tracked = useMemo(
    () => session.account?.deposits.find((d) => (depositId && d._id === depositId) || (txHash && d.src_tx_hash === txHash)),
    [session.account, depositId, txHash],
  );

  const needsApproval = !!quote?.approval && (stage === 'idle' || stage === 'approving');
  const belowMin = !!quote && target === 'ETH' && quote.estimate.out_groth > 0 && quote.estimate.out_groth < MIN_ETH_GROTH;
  const txChainName = quote?.tx ? (data.chainById(data.resolveEvmChainId(quote.tx.chain_id))?.name ?? `chain ${quote.tx.chain_id}`) : '';

  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Deposit</h1>
          <p className="muted">
            Pay any token on any chain. One transaction from your wallet: a deBridge order whose hook locks the target asset in the Beam
            bridge. The credit lands as a balance held for this wallet — no fee at deposit, 2% when you withdraw.
          </p>
        </div>
      </div>

      {wallet.address ? (
        <Portfolio address={wallet.address} selectedKey={selectedKey} onPick={onPick} />
      ) : (
        <section className="card">
          <h2>Your portfolio</h2>
          <p className="muted small" style={{ marginTop: 6 }}>
            Connect a wallet and its holdings across every supported chain show up here as tap-to-pay chips.
          </p>
          <div style={{ marginTop: 12 }}>
            <button type="button" className="btn btn-primary" onClick={wallet.openPicker}>
              Connect wallet
            </button>
          </div>
        </section>
      )}

      <div className="grid-2">
        <section className="card" data-testid="deposit-form">
          <div className="card-head">
            <h2>What to deposit</h2>
          </div>
          <div className="stack">
            <div className="field">
              <span className="label">Receive as</span>
              <div className="seg" role="radiogroup" aria-label="Target asset">
                {(assets.length ? assets : ([{ key: 'ETH', symbol: 'ETH' }] as Asset[])).map((a) => (
                  <button
                    key={a.key}
                    type="button"
                    role="radio"
                    aria-checked={target === a.key}
                    className={target === a.key ? 'active' : ''}
                    disabled={busy}
                    onClick={() => {
                      setTarget(a.key);
                      resetFlow();
                    }}
                  >
                    {a.symbol}
                  </button>
                ))}
              </div>
              {targetAsset && (
                <span className="help">
                  Credited as {targetAsset.beam_symbol} on Beam (8 decimals). Withdrawals pay out {targetAsset.symbol} on Ethereum.
                </span>
              )}
            </div>

            <div className="field">
              <label className="label" htmlFor="src-chain">
                Source chain
              </label>
              <select
                id="src-chain"
                className="select"
                value={chainId ?? ''}
                disabled={!chains.length || busy}
                onChange={(e) => {
                  setChainId(Number(e.target.value));
                  setSelectedKey(null);
                  setAmount('');
                  resetFlow();
                }}
              >
                {!chains.length && <option value="">Loading chains…</option>}
                {sortedChains.map((c) => (
                  <option key={c.chain_id} value={c.chain_id}>
                    {c.name} ({c.native_symbol})
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <span className="label">Token</span>
              <TokenSelect
                tokens={tokens}
                value={token}
                loading={tokensLoading}
                disabled={busy}
                onChange={(t) => {
                  setToken(t);
                  setSelectedKey(null);
                  resetFlow();
                }}
              />
              {tokensError && <span className="error-text">Token list: {tokensError}</span>}
            </div>

            <div className="field">
              <label className="label" htmlFor="amount">
                Amount{token ? ` (${token.symbol})` : ''}
              </label>
              <div className="input-wrap">
                <input
                  id="amount"
                  className="input num"
                  inputMode="decimal"
                  placeholder="0.0"
                  value={amount}
                  disabled={busy}
                  aria-invalid={amount !== '' && rawAmount === null}
                  onChange={(e) => {
                    setAmount(e.target.value);
                    resetFlow();
                  }}
                />
                <span className="suffix">
                  <button
                    type="button"
                    className="btn btn-sm"
                    disabled={maxRaw === null || !token || busy}
                    title={maxRaw === null ? 'Balance unknown — pick the holding from the portfolio above' : 'Use the full balance'}
                    onClick={() => token && maxRaw !== null && setAmount(formatUnits(maxRaw, token.decimals))}
                  >
                    Max
                  </button>
                </span>
              </div>
              {amount !== '' && rawAmount === null && <span className="error-text">Enter a positive number.</span>}
              {maxRaw !== null && token && (
                <span className="help num">
                  Balance {fmtUnits(maxRaw, token.decimals)} {token.symbol}
                </span>
              )}
              <span className="help">
                Minimum 0.02 ETH-equivalent — below it the deposit is still credited, but ingress costs dominate.
              </span>
            </div>
          </div>
        </section>

        <section className="card" data-testid="quote-card">
          <div className="card-head">
            <h2>Quote</h2>
            {quote && expiresIn > 0 && !busy && stage !== 'tracking' && (
              <span className="tiny muted num" aria-live="polite">
                refreshes in {expiresIn}s
              </span>
            )}
          </div>
          <SignInGate what="a quote">
            {!chain || !token ? (
              <p className="muted small">Pick a chain and a token.</p>
            ) : rawAmount === null ? (
              <p className="muted small">
                Enter an amount to get a quote. DLN quotes are valid for about 30 s and refresh here automatically.
              </p>
            ) : (
              <div className="stack">
                {stage === 'tracking' && <div className="banner banner-ok">Transaction sent — follow the status below.</div>}
                {quoteLoading && !quote && <p className="muted small">Getting a quote…</p>}
                {quoteError && <div className="banner banner-error">{quoteError}</div>}
                {quote && (
                  <>
                    <div className="tile tile-accent-teal">
                      <div className="tile-label">You receive (estimate)</div>
                      <div className="tile-value" data-testid="quote-out">
                        {fmtUnits(quote.estimate.out_units, outDecimals, target === 'WBTC' ? 8 : 6)}
                        <span className="unit">{quote.target_asset}</span>
                      </div>
                      <div className="tile-sub">
                        {typeof quote.estimate.usd === 'number' ? `≈ ${fmtUsd(quote.estimate.usd)} · ` : ''}
                        ETA {fmtDuration(quote.estimate.eta_s)}
                        {quote.estimate.relayer_fee_units
                          ? ` · bridge relayer fee ${fmtUnits(quote.estimate.relayer_fee_units, outDecimals, 6)} ${quote.target_asset}`
                          : ''}
                        {quoteLoading ? ' · refreshing…' : ''}
                      </div>
                    </div>
                    {mode === 'direct' && (
                      <div className="banner banner-ok" data-testid="direct-note">
                        Direct deposit — your {quote.target_asset} goes straight into the Beam bridge, no deBridge fee.
                      </div>
                    )}
                    {swapDone && (
                      <div className="banner banner-ok" data-testid="swap-done">
                        Swapped {swapDone.from} → {swapDone.amount} {swapDone.to}. Step 2: deposit it below.
                      </div>
                    )}
                    <dl className="kv">
                      <dt>Paying</dt>
                      <dd className="num">
                        {fmtUnits(quote.estimate.src.amount, quote.estimate.src.decimals)} {quote.estimate.src.symbol} on {chain.name}
                      </dd>
                      <dt>Credited on Beam</dt>
                      <dd className="num">
                        {(quote.estimate.out_groth / 1e8).toLocaleString('en-US', { maximumFractionDigits: 8 })}{' '}
                        {targetAsset?.beam_symbol ?? `b${target}`}
                      </dd>
                      <dt>Quote id</dt>
                      <dd className="mono wrap">{quote.quote_id}</dd>
                      {quote.order_id && (
                        <>
                          <dt>Order</dt>
                          <dd className="mono wrap">{quote.order_id}</dd>
                        </>
                      )}
                    </dl>
                    {belowMin && (
                      <div className="banner banner-warn">Below the 0.02 ETH minimum — credited, but ingress costs dominate.</div>
                    )}

                    {mode === 'swap' && quote.swap_tx ? (
                      <div className="stack-sm" data-testid="swap-panel">
                        <div className="banner banner-warn">
                          <span>
                            {quote.estimate.src.symbol} is not {quote.target_asset}. Step 1 swaps it in your own wallet through deBridge;
                            the {quote.target_asset} lands in your wallet, never ours.
                          </span>
                        </div>
                        <span className="strong">
                          Step 1 — Swap {quote.estimate.src.symbol} → {quote.target_asset} in your wallet (deBridge)
                        </span>
                        {quote.approval && (
                          <div className="row">
                            <button
                              type="button"
                              className="btn btn-indigo"
                              disabled={!needsApproval || busy || expiresIn <= 0}
                              onClick={approve}
                              data-testid="approve-btn"
                            >
                              {stage === 'approving'
                                ? 'Waiting for approval…'
                                : needsApproval
                                  ? `Approve ${quote.estimate.src.symbol}`
                                  : 'Approved'}
                            </button>
                            {approveHash && (
                              <a
                                href={explorerTx(data.resolveEvmChainId(quote.approval.chain_id), approveHash)}
                                target="_blank"
                                rel="noreferrer"
                                className="small"
                              >
                                approval tx
                              </a>
                            )}
                          </div>
                        )}
                        <div className="row">
                          <button
                            type="button"
                            className="btn btn-primary btn-lg"
                            disabled={needsApproval || busy || expiresIn <= 0}
                            onClick={runSwap}
                            data-testid="swap-btn"
                          >
                            {stage === 'swapping'
                              ? 'Confirm in the wallet…'
                              : stage === 'swap-wait'
                                ? 'Waiting for the swap…'
                                : `Swap ${fmtUnits(quote.estimate.src.amount, quote.estimate.src.decimals)} ${quote.estimate.src.symbol}`}
                          </button>
                          {swapHash && (
                            <a href={explorerTx(1, swapHash)} target="_blank" rel="noreferrer" className="small">
                              swap tx
                            </a>
                          )}
                        </div>
                        <span className="help">
                          Step 2 appears by itself: Pgas.me re-quotes the {quote.target_asset} that actually arrived as a direct deposit.
                        </span>
                      </div>
                    ) : !quote.armed || !quote.tx ? (
                      <div className="banner banner-warn" data-testid="unarmed-banner">
                        <span>Deposits open when the Beam wallet is armed — this is a preview.</span>
                        {quote.note && <span className="small muted">{quote.note}</span>}
                      </div>
                    ) : (
                      stage !== 'tracking' && (
                        <div className="stack-sm">
                          {quote.approval && (
                            <div className="row">
                              <button
                                type="button"
                                className="btn btn-indigo"
                                disabled={!needsApproval || busy || expiresIn <= 0}
                                onClick={approve}
                                data-testid="approve-btn"
                              >
                                {stage === 'approving'
                                  ? 'Waiting for approval…'
                                  : needsApproval
                                    ? `Approve ${quote.estimate.src.symbol}`
                                    : 'Approved'}
                              </button>
                              {approveHash && (
                                <a
                                  href={explorerTx(data.resolveEvmChainId(quote.approval.chain_id), approveHash)}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="small"
                                >
                                  approval tx
                                </a>
                              )}
                            </div>
                          )}
                          <button
                            type="button"
                            className="btn btn-primary btn-lg"
                            disabled={needsApproval || busy || expiresIn <= 0}
                            onClick={stage === 'registering' && txHash ? retryRegister : send}
                            data-testid="deposit-btn"
                          >
                            {stage === 'sending'
                              ? 'Confirm in the wallet…'
                              : stage === 'registering'
                                ? 'Registering the deposit…'
                                : `Deposit ${fmtUnits(quote.estimate.src.amount, quote.estimate.src.decimals)} ${quote.estimate.src.symbol}`}
                          </button>
                          {stage === 'registering' && txHash && flowError && (
                            <span className="small">
                              The transaction was sent (
                              <a href={explorerTx(data.resolveEvmChainId(quote.tx.chain_id), txHash)} target="_blank" rel="noreferrer">
                                view
                              </a>
                              ) but Pgas.me could not register it — retry above.
                            </span>
                          )}
                          <span className="help">
                            {mode === 'direct'
                              ? `The wallet switches to ${txChainName} and sends one transaction to the Beam bridge pipe.`
                              : `The wallet switches to ${txChainName} and sends one transaction to the deBridge order contract.`}
                          </span>
                        </div>
                      )
                    )}
                    {flowError && <div className="banner banner-error">{flowError}</div>}
                  </>
                )}
              </div>
            )}
          </SignInGate>
        </section>
      </div>

      {(stage === 'tracking' || tracked) && (txHash || depositId) && (
        <DepositTimeline
          deposit={tracked}
          txHash={txHash}
          chainId={chain?.chain_id}
          onNew={() => {
            resetFlow();
            setAmount('');
            setQuote(null);
          }}
        />
      )}
    </div>
  );
}

// ---------- status timeline ----------
const STEPS = ['Submitted', 'Order filled', 'Locked in Beam bridge', 'Confirming', 'Credited'];

function stepIndex(status: string): number {
  switch (status) {
    case 'submitted':
      return 0;
    case 'order_seen':
    case 'fallback_pending':
      return 1;
    case 'locked':
      return 2;
    case 'confirming':
      return 3;
    case 'credited':
      return 4;
    default:
      return -1;
  }
}

function DepositTimeline({
  deposit,
  txHash,
  chainId,
  onNew,
}: {
  deposit: Deposit | undefined;
  txHash: string | null;
  chainId?: number;
  onNew: () => void;
}) {
  const { wallet } = useStore();
  const [confs, setConfs] = useState<number | null>(null);
  const status = deposit?.status ?? 'submitted';
  const idx = stepIndex(status);
  const failed = status === 'failed' || status === 'expired';
  const block = deposit?.eth?.block;
  const updatedAt = deposit?.updated_at;

  // "Confirming (n/12)": the API gives the lock block; the head comes from Ethereum directly
  useEffect(() => {
    if (status !== 'confirming' || !block) {
      setConfs(null);
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const p = wallet.provider && wallet.chainId === 1 ? new BrowserProvider(wallet.provider, 1) : await getFallbackProvider(1);
        if (!p) return;
        const head = await p.getBlockNumber();
        if (alive) setConfs(Math.max(0, Math.min(12, head - block + 1)));
      } catch {
        if (alive) setConfs(null);
      }
    })();
    return () => {
      alive = false;
    };
  }, [status, block, updatedAt, wallet.provider, wallet.chainId]);

  return (
    <section className="card" data-testid="deposit-timeline">
      <div className="card-head">
        <h2>Deposit status</h2>
        <div className="row">
          {deposit && <span className="tiny muted">status polls every 10 s</span>}
          {(status === 'credited' || failed) && (
            <button type="button" className="btn btn-sm" onClick={onNew}>
              New deposit
            </button>
          )}
        </div>
      </div>
      <div className="grid-2">
        <div className="timeline">
          {STEPS.map((label, i) => {
            const cur = Math.max(idx, 0);
            const cls =
              failed && i === cur ? 'failed' : i < idx || (i === idx && status === 'credited') ? 'done' : i === idx ? 'current' : '';
            let sub = '';
            if (i === 3 && status === 'confirming') sub = confs !== null ? `${confs}/12 confirmations` : 'waiting for 12 confirmations';
            if (i === 1 && status === 'fallback_pending')
              sub = 'Bridging (manual) — the hook fell back to a Pgas.me address; a worker bridges it';
            if (i === 2 && deposit?.eth?.msg_id !== undefined) sub = `Beam message id ${deposit.eth.msg_id}`;
            if (failed && i === cur) sub = depositStatusLabel(status) + (deposit?.note ? ` — ${deposit.note}` : '');
            return (
              <div key={label} className={`tl-step ${cls}`}>
                <div className="tl-dot">{cls === 'done' ? '✓' : i + 1}</div>
                <div>
                  <div className="tl-title">
                    {i === 3 && status === 'confirming' && confs !== null ? `Confirming (${confs}/12)` : label}
                  </div>
                  {sub && <div className="tl-sub">{sub}</div>}
                </div>
              </div>
            );
          })}
        </div>
        <dl className="kv">
          <dt>Status</dt>
          <dd>{deposit ? depositStatusLabel(deposit.status) : 'Registered — waiting for the first status poll'}</dd>
          {txHash && (
            <>
              <dt>Source tx</dt>
              <dd>
                <a href={explorerTx(chainId ?? deposit?.src.chain_id, txHash)} target="_blank" rel="noreferrer" className="mono">
                  {txHash.slice(0, 10)}…{txHash.slice(-6)}
                </a>
              </dd>
            </>
          )}
          {deposit?.order_id && (
            <>
              <dt>DLN order</dt>
              <dd className="mono wrap">{deposit.order_id}</dd>
            </>
          )}
          {deposit?.eth?.tx && (
            <>
              <dt>Ethereum tx</dt>
              <dd>
                <a href={explorerTx(1, deposit.eth.tx)} target="_blank" rel="noreferrer" className="mono">
                  {deposit.eth.tx.slice(0, 10)}…{deposit.eth.tx.slice(-6)}
                </a>
              </dd>
            </>
          )}
          {deposit?.value_groth !== undefined && (
            <>
              <dt>Credited</dt>
              <dd className="num">
                {(deposit.value_groth / 1e8).toLocaleString('en-US', { maximumFractionDigits: 8 })} {deposit.asset}
              </dd>
            </>
          )}
          {deposit?.note && (
            <>
              <dt>Note</dt>
              <dd>{deposit.note}</dd>
            </>
          )}
        </dl>
      </div>
    </section>
  );
}

// ---------- searchable token picker ----------
function TokenLogo({ token, size = 22 }: { token: Token | null | undefined; size?: number }) {
  const [broken, setBroken] = useState(false);
  const logo = token?.logo;
  useEffect(() => setBroken(false), [logo]);
  if (logo && !broken) return <img src={logo} alt="" width={size} height={size} onError={() => setBroken(true)} loading="lazy" />;
  return <span className="logo-fallback">{(token?.symbol ?? '?').slice(0, 3).toUpperCase()}</span>;
}

function TokenSelect({
  tokens,
  value,
  onChange,
  loading,
  disabled,
}: {
  tokens: Token[];
  value: Token | null;
  onChange: (t: Token) => void;
  loading?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    input.current?.focus();
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    const list = s
      ? tokens.filter((t) => t.symbol?.toLowerCase().includes(s) || t.name?.toLowerCase().includes(s) || t.address?.toLowerCase() === s)
      : tokens;
    return list.slice(0, 200);
  }, [tokens, q]);

  useEffect(() => setActive(0), [q, open]);

  const pick = (t: Token) => {
    onChange(t);
    setOpen(false);
    setQ('');
  };

  return (
    <div className="combo" ref={root}>
      <button
        type="button"
        className="combo-btn"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled || loading}
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="token-select"
      >
        <TokenLogo token={value} />
        <span className="strong">{loading ? 'Loading tokens…' : value ? value.symbol : 'Choose a token'}</span>
        {value && (
          <span className="muted small" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {isNativeToken(value.address) ? 'native' : value.name}
          </span>
        )}
      </button>
      {open && (
        <div className="combo-pop">
          <input
            ref={input}
            className="input"
            placeholder="Search by symbol, name or address"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setActive((a) => Math.min(a + 1, filtered.length - 1));
              } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setActive((a) => Math.max(a - 1, 0));
              } else if (e.key === 'Enter') {
                e.preventDefault();
                if (filtered[active]) pick(filtered[active]);
              } else if (e.key === 'Escape') setOpen(false);
            }}
            aria-label="Search tokens"
          />
          <div className="combo-list" role="listbox">
            {filtered.length === 0 && <div className="empty">No token matches</div>}
            {filtered.map((t, i) => (
              <button
                key={`${t.address}-${i}`}
                type="button"
                role="option"
                aria-selected={value?.address === t.address}
                className={`combo-item${i === active ? ' active' : ''}`}
                onClick={() => pick(t)}
                onMouseEnter={() => setActive(i)}
              >
                <TokenLogo token={t} size={24} />
                <span className="ci-sym">{t.symbol}</span>
                <span className="ci-name">{isNativeToken(t.address) ? 'native' : t.name}</span>
              </button>
            ))}
            {tokens.length > 200 && filtered.length === 200 && (
              <div className="tiny muted" style={{ padding: '6px 8px' }}>
                Showing the first 200 — type to narrow the list.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
