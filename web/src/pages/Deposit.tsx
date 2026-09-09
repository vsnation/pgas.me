// Deposit: portfolio chips → what you pay with (chain + token in one picker) → what you receive →
// amount → quote → (armed) approve/deposit → status timeline. When the Beam wallet is not armed the
// quote is a preview and nothing is sent.
//
// Four quote modes (API_CONTRACT.md). `uniswap` is the primary ingress and the one this page leads
// with when the API says that path is open: one transaction on Ethereum into the Pgas gateway pool,
// whose hook swaps on the canonical pool and locks the output in the Beam bridge in the same
// transaction — so its tx is built with the quote and there is nothing to arm. `xchain` is the
// cross-chain order; `direct` skips the router and pays the pipe itself; `swap` is a single-chain
// swap into the user's OWN wallet, after which we re-quote what actually arrived and continue as
// `direct`. A swap tx is never registered as a deposit.
//
// Which paths are open is the API's statement, never this file's assumption — lib/ingress.ts reads
// it in one place and every branch here reads that answer.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BrowserProvider, Interface, formatUnits } from 'ethers';
import { HowItWorks } from '../components/HowItWorks';
import { Portfolio } from '../components/Portfolio';
import { SignInGate } from '../components/SignInGate';
import { depositStatusLabel } from '../components/Status';
import { ApiError, api, errorText } from '../lib/api';
import { NATIVE_ADDRESS, chainIconUrl, getFallbackProvider, isNativeToken } from '../lib/chains';
import { explorerTx, fmtDuration, fmtUnits, fmtUsd, parseAmount, toDate } from '../lib/format';
import { ingressPartial, isUniswapToken, resolveIngress } from '../lib/ingress';
import { loadCachedPortfolio, loadTokens, type Holding } from '../lib/portfolio';
import { quoteMode, type ArmedQuote, type Asset, type AssetKey, type Chain, type Deposit, type Quote, type Token } from '../lib/types';
import { useStore } from '../state/store';

const QUOTE_DEBOUNCE_MS = 500;
const QUOTE_TTL_FALLBACK_S = 30; // a router quote lives ~30 s; used when the API sends no expires_at
/** How far the armed order may move from the estimate the user was shown before they re-agree. */
const ESTIMATE_DRIFT_LIMIT = 0.005;

/**
 * The deposit floor is the API's and only the API's — `POST /v1/quote` answers 400 "below the
 * minimum deposit …" with the live number in it. The client used to carry its own 0.02 ETH copy,
 * which said the wrong thing the moment the operator moved the floor (it is 0.002 ETH today).
 * Two implementations of one fact disagree; this one reads.
 */
function isBelowMinimum(e: unknown): boolean {
  return e instanceof ApiError && e.status === 400 && /below the minimum deposit/i.test(e.detail);
}

/**
 * `arming` is the T2b split (2026-09-09): `POST /v1/quote` costs ONE router call and returns the
 * estimate, and the hook-carrying order is built by `POST /v1/quote/{id}/arm` when the user clicks
 * Deposit. `confirm` is the stage that stage can land in — the armed order came back more than
 * ESTIMATE_DRIFT_LIMIT away from the number on the screen, so the new one is shown and the user
 * clicks again. A quote is never auto-refreshed out from under either.
 */
type Stage = 'idle' | 'arming' | 'confirm' | 'approving' | 'approved' | 'swapping' | 'swap-wait' | 'sending' | 'registering' | 'tracking';

/** |new − shown| / shown, on decimal-string raw units; a double is far finer than the 0.5 % gate. */
function estimateDrift(shown: string, next: string): number {
  const a = Number(shown);
  const b = Number(next);
  if (!Number.isFinite(a) || !Number.isFinite(b) || a <= 0) return 0;
  return Math.abs(b - a) / a;
}

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

function expiresInSeconds(expiresAt: string | number | null, receivedAt: number): number {
  const d = toDate(expiresAt);
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
  /** Set the moment the user picks a source chain by hand; from then on the wallet stops steering. */
  const pickedByUser = useRef(false);

  const [quote, setQuote] = useState<Quote | null>(null);
  const [quoteAt, setQuoteAt] = useState(0);
  const [expiresAt, setExpiresAt] = useState<string | number | null>(null);
  const [armed, setArmed] = useState<ArmedQuote | null>(null);
  const [needsConfirm, setNeedsConfirm] = useState(false);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [belowMin, setBelowMin] = useState(false); // the API said 400 "below the minimum deposit …"
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

  // ---- which ingress paths are open (the API's statement; see lib/ingress.ts) ----
  const flags = useMemo(() => resolveIngress(data.ingress, ingressPartial(session.account)), [data.ingress, session.account]);
  const onEthereum = chain?.chain_id === 1;
  /**
   * The Uniswap route takes ETH out (DAI and WBTC keep their own pipe, which is the direct path)
   * and only the tokens it has a registered gateway pool for. Asking for a route the API does not
   * have would be a 400 the user did nothing to deserve, so the request simply does not name it.
   */
  const uniswapPair = isUniswapToken(token, data.uniswapTokens);
  const askUniswap = flags.uniswap && !!onEthereum && target === 'ETH' && uniswapPair;
  /** A source off Ethereum with the cross-chain path closed: there is nothing to quote. */
  const xchainClosed = !!chain && !onEthereum && !flags.xchain;
  /**
   * The Ethereum token list, narrowed to the pairs the Uniswap route takes — but only while that is
   * the path, and never to nothing: an empty list would be this filter's opinion, not the API's, so
   * a list that matches no pair falls back to the whole list.
   */
  const uniswapChoice = flags.uniswap && !!onEthereum && target === 'ETH';
  const payTokens = useMemo(() => {
    if (!uniswapChoice) return tokens;
    const kept = tokens.filter((t) => isUniswapToken(t, data.uniswapTokens));
    return kept.length ? kept : tokens;
  }, [tokens, uniswapChoice, data.uniswapTokens]);
  const busy =
    stage === 'arming' ||
    stage === 'approving' ||
    stage === 'sending' ||
    stage === 'registering' ||
    stage === 'swapping' ||
    stage === 'swap-wait';
  const mode = quoteMode(quote);
  const uniswap = mode === 'uniswap';
  /**
   * The card leads with Uniswap when that is what the click would do — and stops the moment a quote
   * comes back as something else, because a title that names a path the API did not take is a lie.
   */
  const uniswapPrimary = askUniswap && (!quote || uniswap);
  const rawAmount = token ? parseAmount(amount, token.decimals) : null;

  const resetFlow = useCallback(() => {
    setStage('idle');
    setFlowError(null);
    setArmed(null);
    setNeedsConfirm(false);
    setApproveHash(null);
    setTxHash(null);
    setDepositId(null);
    setSwapHash(null);
    setSwapDone(null);
  }, []);

  /**
   * "Pay with" follows the wallet — until the user says otherwise. On the first render it is the
   * chain the wallet is on (Ethereum when that is not a chain we take), and a `chainChanged` from
   * the wallet moves it too, which re-quotes on the chain the money is actually on. Once the user
   * has picked a chain themselves (the picker, or a portfolio chip) the wallet stops steering: they
   * asked to pay from somewhere else, and switching the wallet to send it must not undo that.
   */
  useEffect(() => {
    if (!chains.length) return;
    if (chainId === null) {
      const w = wallet.chainId;
      if (w !== null && chains.some((c) => c.chain_id === w)) setChainId(w);
      else setChainId(chains.find((c) => c.chain_id === 1)?.chain_id ?? chains[0].chain_id);
      return;
    }
    if (pickedByUser.current) return;
    const w = wallet.chainId;
    if (w === null || w === chainId || !chains.some((c) => c.chain_id === w)) return;
    setChainId(w);
    setSelectedKey(null);
    setMaxRaw(null);
    resetFlow();
  }, [chains, chainId, wallet.chainId, resetFlow]);

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
      pickedByUser.current = true;
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
  const canQuote =
    !!sessionToken && !!wallet.address && !!chain && !!token && rawAmount !== null && stage !== 'tracking' && !xchainClosed;
  const rawAmountKey = rawAmount?.toString() ?? '';
  const srcChain = chain?.chain_id; // the API takes EVM ids and maps to the router's own id itself
  const srcToken = token?.address;
  const sender = wallet.address;
  useEffect(() => {
    if (!canQuote || srcChain === undefined || !srcToken || !rawAmountKey || !sender) {
      setQuote(null);
      setExpiresAt(null);
      setArmed(null);
      setNeedsConfirm(false);
      setQuoteError(null);
      setBelowMin(false);
      setQuoteLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setQuoteLoading(true);
    setQuoteError(null);
    const t = setTimeout(async () => {
      try {
        const q = await api.quote(
          {
            src_chain_id: srcChain,
            src_token: srcToken,
            amount: rawAmountKey,
            target_asset: target,
            sender,
            // named only when the API says that path is open AND it takes this pair; left off
            // otherwise so an API build that has never heard of the field answers exactly as before
            ...(askUniswap ? { route: 'uniswap' as const } : {}),
          },
          ctrl.signal,
        );
        if (ctrl.signal.aborted) return;
        setQuote(q);
        setQuoteAt(Date.now());
        setExpiresAt(q.expires_at ?? null);
        setExpiresIn(expiresInSeconds(q.expires_at ?? null, Date.now()));
        // a fresh estimate invalidates any order armed against the previous one
        setArmed(null);
        setNeedsConfirm(false);
        setBelowMin(false);
      } catch (e) {
        if (ctrl.signal.aborted || (e as Error)?.name === 'AbortError') return;
        setQuote(null);
        setExpiresAt(null);
        setArmed(null);
        setNeedsConfirm(false);
        setQuoteError(errorText(e)); // the API's own sentence, verbatim — including its minimum
        setBelowMin(isBelowMinimum(e));
      } finally {
        if (!ctrl.signal.aborted) setQuoteLoading(false);
      }
    }, QUOTE_DEBOUNCE_MS);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [canQuote, srcChain, srcToken, rawAmountKey, target, sender, sessionToken, requoteTick, askUniswap]);

  useEffect(() => {
    if (!quote) return;
    const t = setInterval(() => {
      const left = expiresInSeconds(expiresAt, quoteAt);
      setExpiresIn(left);
      // never re-quote under a user who is looking at a changed number and deciding (stage 'confirm')
      if (left <= 0 && !busy && stage !== 'tracking' && stage !== 'confirm') setRequoteTick((n) => n + 1);
    }, 1000);
    return () => clearInterval(t);
  }, [quote, quoteAt, expiresAt, busy, stage]);

  // ---- approve / arm / deposit / register ----
  const sendApproval = async (approval: NonNullable<Quote['approval']>) => {
    const iface = new Interface(['function approve(address spender, uint256 amount)']);
    const hash = await wallet.sendTransaction({
      to: approval.token,
      data: iface.encodeFunctionData('approve', [approval.spender, BigInt(approval.amount)]),
      chainId: data.resolveEvmChainId(approval.chain_id),
    });
    setApproveHash(hash);
    if (wallet.provider) {
      // best effort: wait for the approval to be mined; the wallet's RPC may not expose receipts
      await new BrowserProvider(wallet.provider).waitForTransaction(hash, 1, 120_000).catch(() => null);
    }
    return hash;
  };

  /** The explicit button, for the quotes that carry their approval up front: `direct` and `swap`. */
  const approve = async () => {
    if (!quote?.approval) return;
    setFlowError(null);
    setStage('approving');
    try {
      await sendApproval(quote.approval);
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

  /**
   * One click, up to four steps: arm the order (`xchain` only — `uniswap`, `direct` and `swap`
   * already carry their tx), let the user re-agree if the armed number moved, approve if the order
   * needs it, send, register. A second click after `confirm` reuses the SAME armed order: `/arm` is
   * idempotent but re-calling it would be a second chance to move the number the user just accepted.
   */
  const send = async () => {
    if (!quote) return;
    setFlowError(null);
    let plan: ArmedQuote | null = armed;
    if (quoteMode(quote) === 'xchain' && !plan) {
      setStage('arming');
      try {
        plan = await api.armQuote(quote.quote_id);
      } catch (e) {
        setFlowError(errorText(e));
        setStage('idle');
        return;
      }
      setArmed(plan);
      if (plan.expires_at) {
        setExpiresAt(plan.expires_at);
        setQuoteAt(Date.now());
        setExpiresIn(expiresInSeconds(plan.expires_at, Date.now()));
      }
      if (plan.estimate?.out_units && estimateDrift(quote.estimate.out_units, plan.estimate.out_units) > ESTIMATE_DRIFT_LIMIT) {
        setNeedsConfirm(true);
        setStage('confirm');
        return;
      }
    }
    const tx = plan?.tx ?? quote.tx;
    if (!tx) {
      setFlowError('The quote carries no transaction to send — refresh it.');
      setStage('idle');
      return;
    }
    setNeedsConfirm(false);
    let hash: string | null = null;
    try {
      // the armed order brings its own approval (the estimate never had one), and the Uniswap route
      // carries its router approval on the quote; the up-front approvals of `direct`/`swap` went
      // through the button above and must not be sent twice
      const upfront = plan?.approval ?? (quoteMode(quote) === 'uniswap' ? quote.approval : undefined);
      if (upfront && approveHash === null) {
        setStage('approving');
        await sendApproval(upfront);
      }
      setStage('sending');
      hash = await wallet.sendTransaction({
        to: tx.to,
        data: tx.data,
        value: tx.value,
        chainId: data.resolveEvmChainId(tx.chain_id),
      });
      setTxHash(hash);
      await register(quote, hash);
    } catch (e) {
      setFlowError(errorText(e));
      setStage(hash ? 'registering' : quote.approval ? 'approved' : 'idle');
    }
  };

  // ---- swap (mode "swap"): one swap into the user's own wallet, then a fresh `direct` quote ----
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

  /**
   * The button that appears BEFORE Deposit, for the quotes whose approval the user must send first:
   * `direct` and `swap`. The Uniswap route is deliberately not one of them — its spender is the
   * router named in the quote, known before the click, so `send()` approves and deposits on one
   * press. Two buttons for one intention is what the second click was.
   */
  const needsApproval = !!quote?.approval && !uniswap && (stage === 'idle' || stage === 'approving');
  // a cross-chain order tx exists only after /arm, and it always lands on the source chain — which
  // is what the button has to name BEFORE the click that arms it
  const txChainId =
    armed?.tx?.chain_id ?? quote?.tx?.chain_id ?? (quote && quoteMode(quote) === 'xchain' ? quote.estimate.src.chain_id : undefined);
  const txChainName = txChainId === undefined ? '' : (data.chainById(data.resolveEvmChainId(txChainId))?.name ?? `chain ${txChainId}`);
  const expired = !!quote && expiresIn <= 0 && !busy && stage !== 'tracking';
  /** What the user is looking at: the armed order's final estimate once there is one, else the quote's. */
  const est = quote ? (armed?.estimate ?? quote.estimate) : null;
  /**
   * The bridge relayer's cut in dollars, priced off the quote's own USD figure so there is no
   * second price source: fee/out is a ratio of the same units, and `usd` is what that `out` is
   * worth. Null when the quote carries no USD — a fee we cannot price is one we do not state.
   */
  const relayerFeeUsd = useMemo(() => {
    if (!est?.relayer_fee_units || typeof est.usd !== 'number') return null;
    try {
      const out = Number(formatUnits(est.out_units, outDecimals));
      const fee = Number(formatUnits(est.relayer_fee_units, outDecimals));
      if (!Number.isFinite(out) || !Number.isFinite(fee) || out <= 0) return null;
      return (est.usd * fee) / out;
    } catch {
      return null;
    }
  }, [est, outDecimals]);
  /** `xchain` gets its tx from /arm on the click; `direct` and `swap` must already carry one. */
  const canDeposit = !!quote?.armed && (quoteMode(quote) === 'xchain' || !!quote.tx);

  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Deposit</h1>
          <p className="muted">
            Pay from any chain. Fund fresh wallets later, with no on-chain link to the source.{' '}
            <HowItWorks route={uniswapPrimary ? 'uniswap' : 'xchain'} />
          </p>
        </div>
      </div>

      {wallet.address ? (
        <Portfolio address={wallet.address} selectedKey={selectedKey} onPick={onPick} />
      ) : (
        <section className="card">
          <h2>Your portfolio</h2>
          <p className="muted small" style={{ marginTop: 6 }}>
            Connect your wallet and everything it holds, on every chain, becomes a tap-to-pay chip here.
          </p>
        </section>
      )}

      <div className="grid-2">
        <section className="card" data-testid="deposit-form" data-route={uniswapPrimary ? 'uniswap' : 'classic'}>
          <div className="card-head">
            <h2>{uniswapPrimary ? 'Pay with Uniswap V4' : 'What to deposit'}</h2>
            {uniswapPrimary && (
              <span className="tiny muted" data-testid="uniswap-primary">
                one transaction on Ethereum
              </span>
            )}
          </div>
          <div className="stack">
            <div className="field">
              <span className="label">Pay with</span>
              <PayWithSelect
                chains={sortedChains}
                chainId={chainId}
                tokens={payTokens}
                value={token}
                loading={tokensLoading}
                disabled={busy}
                onChainChange={(id) => {
                  pickedByUser.current = true;
                  setChainId(id);
                  setSelectedKey(null);
                  setAmount('');
                  resetFlow();
                }}
                onChange={(t) => {
                  setToken(t);
                  setSelectedKey(null);
                  resetFlow();
                }}
              />
              {tokensError && <span className="error-text">Token list: {tokensError}</span>}
            </div>

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
              <span className="help">
                Your balance is kept in this asset.
                {uniswapChoice ? '' : flags.uniswap && onEthereum ? ' DAI and WBTC go through their own bridge pipe.' : ''}
              </span>
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
              {typeof est?.usd === 'number' && est.usd > 0 && (
                <span className="help num" data-testid="amount-usd">
                  ≈ {fmtUsd(est.usd)}
                </span>
              )}
              {maxRaw !== null && token && (
                <span className="help num">
                  Balance {fmtUnits(maxRaw, token.decimals)} {token.symbol}
                </span>
              )}
            </div>
          </div>
        </section>

        <section className="card" data-testid="quote-card">
          <div className="card-head">
            <h2>Quote</h2>
            {/* The countdown was noise: the quote re-quotes itself silently. Only a quote that
                actually lapsed (a re-quote in flight, or one suppressed mid-send) is worth a word. */}
            {expired && (
              <button
                type="button"
                className="btn btn-sm"
                data-testid="quote-expired"
                aria-live="polite"
                onClick={() => setRequoteTick((n) => n + 1)}
              >
                Quote expired — refresh
              </button>
            )}
          </div>
          <SignInGate what="a quote" verb="get">
            {xchainClosed ? (
              /* The chips stay: what the wallet holds elsewhere is still worth seeing. What is
                 closed is the crossing, and the only thing that opens it is paying from Ethereum. */
              <div className="stack-sm">
                <div className="banner banner-warn" data-testid="xchain-closed">
                  <span>Switch to Ethereum to pay with Uniswap</span>
                </div>
                <div className="row">
                  <button
                    type="button"
                    className="btn btn-sm"
                    data-testid="use-ethereum"
                    onClick={() => {
                      pickedByUser.current = true;
                      setChainId(1);
                      setSelectedKey(null);
                      setAmount('');
                      resetFlow();
                    }}
                  >
                    Pay from Ethereum
                  </button>
                </div>
              </div>
            ) : !chain || !token ? (
              <p className="muted small">Pick what you want to pay with.</p>
            ) : rawAmount === null ? (
              <p className="muted small">Enter an amount and the quote appears here.</p>
            ) : (
              <div className="stack">
                {stage === 'tracking' && <div className="banner banner-ok">Transaction sent — follow the status below.</div>}
                {quoteLoading && !quote && <p className="muted small">Getting a quote…</p>}
                {quoteError &&
                  (belowMin ? (
                    // the only place a minimum is ever stated, and it is the API's own sentence
                    <div className="banner banner-warn" data-testid="min-deposit">
                      {quoteError}
                    </div>
                  ) : (
                    <div className="banner banner-error">{quoteError}</div>
                  ))}
                {quote && est && (
                  <>
                    <div className="tile tile-accent-teal">
                      <div className="tile-label">You receive (estimate)</div>
                      <div className="tile-value" data-testid="quote-out">
                        {fmtUnits(est.out_units, outDecimals, target === 'WBTC' ? 8 : 6)}
                        <span className="unit">{quote.target_asset}</span>
                      </div>
                      <div className="tile-sub">
                        {typeof est.usd === 'number' ? `≈ ${fmtUsd(est.usd)}` : ''}
                        {quoteLoading ? (typeof est.usd === 'number' ? ' · refreshing…' : 'refreshing…') : ''}
                      </div>
                    </div>
                    {/* One line where two rows of bookkeeping used to be ("Paying …", "Credited on
                        Beam …"): what a depositor waits for is the wait, and the bridge's cut is a
                        cent — a number worth rounding, not stating to six decimals. */}
                    {/* The two numbers that only the Uniswap route has, and the only slippage bound
                        that works on it: `min_out_units` is what the hook itself reverts below. */}
                    {uniswap && est.min_out_units && (
                      <p className="help num" data-testid="min-out">
                        min you receive {fmtUnits(est.min_out_units, outDecimals, target === 'WBTC' ? 8 : 6)} {quote.target_asset}
                      </p>
                    )}
                    {uniswap && typeof est.price_impact_bps === 'number' && (
                      <p className="help num" data-testid="price-impact">
                        price impact {(est.price_impact_bps / 100).toFixed(2)}%
                      </p>
                    )}
                    <p className="help" data-testid="lands-in">
                      Lands in your balance in ≈ {fmtDuration(est.eta_s)}
                      {relayerFeeUsd !== null ? ` · bridge fee ${fmtUsd(relayerFeeUsd)}` : ''}
                    </p>
                    {mode === 'direct' && (
                      <div className="banner banner-ok" data-testid="direct-note">
                        Direct deposit — your {quote.target_asset} goes straight into the Beam bridge, no routing fee.
                      </div>
                    )}
                    {swapDone && (
                      <div className="banner banner-ok" data-testid="swap-done">
                        Swapped {swapDone.from} → {swapDone.amount} {swapDone.to}. Step 2: deposit it below.
                      </div>
                    )}
                    {mode === 'swap' && quote.swap_tx ? (
                      <div className="stack-sm" data-testid="swap-panel">
                        <div className="banner banner-warn">
                          <span>
                            {est.src.symbol} is not {quote.target_asset}. Step 1 swaps it in your own wallet; the {quote.target_asset} lands
                            in your wallet, never ours.
                          </span>
                        </div>
                        <span className="strong">
                          Step 1 — Swap {est.src.symbol} → {quote.target_asset} in your wallet
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
                              {stage === 'approving' ? 'Waiting for approval…' : needsApproval ? `Approve ${est.src.symbol}` : 'Approved'}
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
                                : `Swap ${fmtUnits(est.src.amount, est.src.decimals)} ${est.src.symbol}`}
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
                    ) : !canDeposit ? (
                      // the API's own note here is `ingress not armed: no Beam pubkey configured`,
                      // which names a flag and a key the user has never heard of
                      <div className="banner banner-warn" data-testid="unarmed-banner">
                        <span>Deposits are paused right now — this is a preview of what you would get.</span>
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
                                {stage === 'approving' ? 'Waiting for approval…' : needsApproval ? `Approve ${est.src.symbol}` : 'Approved'}
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
                          {needsConfirm && armed && (
                            <div className="banner banner-warn" data-testid="estimate-changed">
                              <span>
                                The cross-chain order came back at{' '}
                                {fmtUnits(armed.estimate.out_units, outDecimals, target === 'WBTC' ? 8 : 6)} {quote.target_asset}, not{' '}
                                {fmtUnits(quote.estimate.out_units, outDecimals, target === 'WBTC' ? 8 : 6)}. Tap Deposit again to take it.
                              </span>
                            </div>
                          )}
                          <button
                            type="button"
                            className="btn btn-primary btn-lg"
                            disabled={needsApproval || busy || expiresIn <= 0}
                            onClick={stage === 'registering' && txHash ? retryRegister : send}
                            data-testid="deposit-btn"
                          >
                            {stage === 'arming'
                              ? 'Preparing the cross-chain order…'
                              : stage === 'approving'
                                ? 'Waiting for approval…'
                                : stage === 'sending'
                                  ? 'Confirm in the wallet…'
                                  : stage === 'registering'
                                    ? 'Registering the deposit…'
                                    : needsConfirm
                                      ? `Deposit at ${fmtUnits(est.out_units, outDecimals, target === 'WBTC' ? 8 : 6)} ${quote.target_asset}`
                                      : // the chain lives in the label, so no sentence has to explain the switch
                                        `Deposit ${fmtUnits(est.src.amount, est.src.decimals)} ${est.src.symbol}${
                                          txChainName ? ` on ${txChainName}` : ''
                                        }`}
                          </button>
                          {uniswap && (
                            <span className="help" data-testid="uniswap-note">
                              via Uniswap V4 — one transaction, the hook locks your ETH in the Beam bridge
                            </span>
                          )}
                          {stage === 'registering' && txHash && flowError && (
                            <span className="small">
                              The transaction was sent (
                              <a
                                href={explorerTx(txChainId === undefined ? undefined : data.resolveEvmChainId(txChainId), txHash)}
                                target="_blank"
                                rel="noreferrer"
                              >
                                view
                              </a>
                              ) but Pgas.me could not register it — retry above.
                            </span>
                          )}
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
          uniswap={tracked?.mode ? tracked.mode === 'uniswap' : uniswap}
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
// Five words for five waits, and it appears only once a transaction has actually been sent — before
// that there is nothing to follow, and a greyed-out list of steps is a page full of things the user
// cannot act on.
//
// The Uniswap route has four: there is no order for anyone to fill — the swap and the lock are the
// same transaction — so `order_seen` cannot happen on it and a step that can never light up is a
// step that makes the wait look longer than it is.
const STEPS = ['Submitted', 'Order filled', 'Bridging', 'Confirming', 'Credited'];
const UNISWAP_STEPS = ['Submitted', 'Bridging', 'Confirming', 'Credited'];

function stepIndex(status: string, uniswap: boolean): number {
  const at = (label: string) => (uniswap ? UNISWAP_STEPS : STEPS).indexOf(label);
  switch (status) {
    case 'submitted':
      return at('Submitted');
    case 'order_seen':
    case 'fallback_pending':
      // never reached on the Uniswap route; if the API ever said it, "Bridging" is the honest step
      return uniswap ? at('Bridging') : at('Order filled');
    case 'locked':
      return at('Bridging');
    case 'confirming':
      return at('Confirming');
    case 'credited':
      return at('Credited');
    default:
      return -1;
  }
}

function DepositTimeline({
  deposit,
  txHash,
  chainId,
  uniswap,
  onNew,
}: {
  deposit: Deposit | undefined;
  txHash: string | null;
  chainId?: number;
  /** the Uniswap route's four steps instead of the cross-chain order's five */
  uniswap?: boolean;
  onNew: () => void;
}) {
  const { wallet } = useStore();
  const [confs, setConfs] = useState<number | null>(null);
  const status = deposit?.status ?? 'submitted';
  const steps = uniswap ? UNISWAP_STEPS : STEPS;
  const idx = stepIndex(status, !!uniswap);
  /** The step the confirmation counter belongs to, whichever list is on screen. */
  const confirmIdx = steps.indexOf('Confirming');
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
        <div className="timeline" data-route={uniswap ? 'uniswap' : 'classic'}>
          {steps.map((label, i) => {
            const cur = Math.max(idx, 0);
            const cls =
              failed && i === cur ? 'failed' : i < idx || (i === idx && status === 'credited') ? 'done' : i === idx ? 'current' : '';
            let sub = '';
            if (i === confirmIdx && status === 'confirming')
              sub = confs !== null ? `${confs}/12 confirmations` : 'waiting for 12 confirmations';
            if (failed && i === cur) sub = depositStatusLabel(status) + (deposit?.note ? ` — ${deposit.note}` : '');
            return (
              <div key={label} className={`tl-step ${cls}`}>
                <div className="tl-dot">{cls === 'done' ? '✓' : i + 1}</div>
                <div>
                  <div className="tl-title">
                    {i === confirmIdx && status === 'confirming' && confs !== null ? `Confirming (${confs}/12)` : label}
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
              <dt>Your payment</dt>
              <dd>
                <a href={explorerTx(chainId ?? deposit?.src.chain_id, txHash)} target="_blank" rel="noreferrer" className="mono">
                  {txHash.slice(0, 10)}…{txHash.slice(-6)}
                </a>
              </dd>
            </>
          )}
          {deposit?.eth?.tx && (
            <>
              <dt>Into the bridge</dt>
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

// ---------- the "Pay with" picker ----------
// One control for two facts that only ever move together: the chain and the token on it. Two
// separate rows (a chain <select>, then a token combobox that reloaded under it) made the user
// answer a question — "which chain?" — that they only ever answer because they hold something
// there. Here the chain is a row of buttons INSIDE the picker, the search covers the chosen
// chain's list, and the portfolio chips above set both at once.
function TokenLogo({ token, size = 22 }: { token: Token | null | undefined; size?: number }) {
  const [broken, setBroken] = useState(false);
  const logo = token?.logo;
  useEffect(() => setBroken(false), [logo]);
  if (logo && !broken) return <img src={logo} alt="" width={size} height={size} onError={() => setBroken(true)} loading="lazy" />;
  return <span className="logo-fallback">{(token?.symbol ?? '?').slice(0, 3).toUpperCase()}</span>;
}

/** The token's logo with the chain's badge on its corner — the portfolio chip, at picker size. */
function PayWithIcons({ token, chainId }: { token: Token | null; chainId: number | null }) {
  const [chainBroken, setChainBroken] = useState(false);
  const icon = chainId === null ? null : chainIconUrl(chainId);
  useEffect(() => setChainBroken(false), [icon]);
  return (
    <span className="pw-icons">
      <TokenLogo token={token} size={24} />
      {icon && !chainBroken && <img className="pw-chain-badge" src={icon} alt="" loading="lazy" onError={() => setChainBroken(true)} />}
    </span>
  );
}

function PayWithSelect({
  chains,
  chainId,
  tokens,
  value,
  onChange,
  onChainChange,
  loading,
  disabled,
}: {
  chains: Chain[];
  chainId: number | null;
  tokens: Token[];
  value: Token | null;
  onChange: (t: Token) => void;
  onChainChange: (chainId: number) => void;
  loading?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const chain = chains.find((c) => c.chain_id === chainId);

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

  useEffect(() => setActive(0), [q, open, chainId]);

  const pick = (t: Token) => {
    onChange(t);
    setOpen(false);
    setQ('');
  };

  return (
    <div className="combo" ref={root}>
      <button
        type="button"
        className="combo-btn pay-with-btn"
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="pay-with"
      >
        <PayWithIcons token={value} chainId={chainId} />
        <span className="strong">{value ? value.symbol : loading ? 'Loading tokens…' : 'Choose a token'}</span>
        <span className="muted small pw-on">{chain ? `on ${chain.name}` : chains.length ? '' : 'loading chains…'}</span>
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
          <div className="pw-chains" role="group" aria-label="Chain" data-testid="pay-with-chains">
            {chains.map((c) => {
              const icon = chainIconUrl(c.chain_id);
              return (
                <button
                  key={c.chain_id}
                  type="button"
                  className={`pw-chain${c.chain_id === chainId ? ' active' : ''}`}
                  aria-pressed={c.chain_id === chainId}
                  data-chain={c.chain_id}
                  title={c.name}
                  onClick={() => onChainChange(c.chain_id)}
                >
                  {icon && <img src={icon} alt="" loading="lazy" />}
                  <span>{c.name}</span>
                </button>
              );
            })}
          </div>
          <div className="combo-list" role="listbox">
            {loading && <div className="empty">Loading tokens…</div>}
            {!loading && filtered.length === 0 && <div className="empty">No token matches</div>}
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
