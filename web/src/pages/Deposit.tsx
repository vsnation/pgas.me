// Deposit: the explainer → portfolio chips → ONE swap-shaped panel → status timeline. When the
// Beam wallet is not armed the quote is a preview and nothing is sent.
//
// T53 (admin 2026-09-10 15:40Z, "Quote block I don't think is actually needed when you can make it
// in What to deposit, to look like DEX Swap"): the form and the quote are one panel now. You pay
// (holding + amount), an arrow, you receive on Ethereum (asset + estimate), a detail strip with
// the route, our fee, the wait and the bridge's cut, and ONE primary button whose label is the
// next thing that happens. The refresh is implicit — every edit re-quotes and a lapsed router
// window re-quotes itself — so the head says how old the number on screen is instead of counting
// down to something the page does by itself. The pieces live in components/SwapPanel.tsx and know
// nothing; every number here is still the API's.
//
// Quote modes (API_CONTRACT.md). `xchain` is the cross-chain order; `direct` skips the router and
// pays the pipe itself; `swap` is a single-chain swap into the user's OWN wallet, after which we
// re-quote what actually arrived and continue as `direct`. `uniswap` has TWO shapes and the quote
// says which: the deployed-hook one carries `tx` (one transaction, nothing to arm), and the
// two-step one (U2, 2026-09-10 — nothing deployed) carries `step:"swap"` + `swap_tx` + `next` and
// is the `swap` handshake with Uniswap V4 as the venue. A swap tx is never registered as a deposit.
// The two-step one is preceded by `approvals[]` — every allowance transaction the API found short,
// in the order they must be sent (T46); this page sends them all and stops at the first refusal.
//
// Which paths are open — and which one this page leads with when both are — is the API's statement,
// never this file's assumption: lib/ingress.ts reads it in one place and every branch here reads
// that answer.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BrowserProvider, Interface, formatUnits } from 'ethers';
import { Explainer } from '../components/Explainer';
import { HowItWorks, type HowItWorksRoute } from '../components/HowItWorks';
import { Portfolio } from '../components/Portfolio';
import { SignInGate } from '../components/SignInGate';
import { SwapAction, SwapDetails, SwapHead, SwapLeg, SwapNote, SwapPanel, SwapRow, SwapSeam } from '../components/SwapPanel';
import { depositStatusLabel } from '../components/Status';
import { ApiError, api, errorText } from '../lib/api';
import { NATIVE_ADDRESS, chainIconUrl, getFallbackProvider, isNativeToken } from '../lib/chains';
import { explorerTx, fmtDuration, fmtNumber, fmtUnits, fmtUsd, parseAmount, toDate } from '../lib/format';
import { ingressPartial, initialRoute, isUniswapToken, resolveIngress, storeRoute, storedRoute, type IngressRoute } from '../lib/ingress';
import { backoffMs, clearPending, loadPending, retryableRegistration, savePending, REGISTER_WINDOW_MS } from '../lib/pending';
import { loadTokens, prefetchTokens, readHoldingBalance, type Holding, type Portfolio as PortfolioData } from '../lib/portfolio';
import {
  approvalKey,
  quoteApprovals,
  quoteMode,
  type ArmedQuote,
  type Asset,
  type AssetKey,
  type Chain,
  type Deposit,
  type Quote,
  type QuoteApproval,
  type Token,
} from '../lib/types';
import { useStore } from '../state/store';

/**
 * 1000 ms was the admin's "Why does getting a quote take so long?" (2026-09-10): the server answers
 * in 0.3–0.5 s cold, so a full second of waiting for the user to stop typing was most of the wait.
 * 300 ms is short enough to feel immediate and long enough that a four-digit amount is one request.
 */
const QUOTE_DEBOUNCE_MS = 300;
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
type Stage =
  | 'idle'
  | 'arming'
  | 'confirm'
  | 'approving'
  | 'approved'
  /**
   * One of the allowances the two-step Uniswap route needs before the swap — always a transaction,
   * never a signature, and there can be up to three (`approvalStep` names the one being held open).
   */
  | 'permitting'
  | 'swapping'
  | 'swap-wait'
  | 'sending'
  | 'registering'
  | 'tracking';

/** |new − shown| / shown, on decimal-string raw units; a double is far finer than the 0.5 % gate. */
function estimateDrift(shown: string, next: string): number {
  const a = Number(shown);
  const b = Number(next);
  if (!Number.isFinite(a) || !Number.isFinite(b) || a <= 0) return 0;
  return Math.abs(b - a) / a;
}

// ─────────────────────────────────────────────────────────────────────────────────────────────
// T46 — the approvals the two-step Uniswap route needs, in the API's order.
//
// The API reads both allowances and answers `approvals[]` with only the short ones (U2-api,
// 2026-09-10 15:15Z). This client SENDS THEM ALL, in the order given, waiting for each receipt —
// it does not decide which are needed, does not reorder them, and does not skip one it does not
// recognise. Everything below is naming: what to call each step to the user, and what to say when
// the wallet refuses one.
// ─────────────────────────────────────────────────────────────────────────────────────────────

/** What one approval is, in the user's words. Unrecognised names are still sent — just unnamed. */
function approvalLabel(a: QuoteApproval, symbol: string): string {
  switch (a.name) {
    case 'approval_reset':
      // some tokens refuse to raise a non-zero allowance; theirs has to go to zero first
      return `the ${symbol} allowance reset to zero`;
    case 'approval':
      return `${symbol} to Permit2`;
    case 'permit_tx':
      return 'Permit2 to the router';
    default:
      return 'an approval';
  }
}

/** What the button says while the wallet is holding this one open. */
function approvalPrompt(a: QuoteApproval, symbol: string): string {
  switch (a.name) {
    case 'approval_reset':
      return `Reset the ${symbol} allowance in your wallet…`;
    case 'approval':
      return `Approve ${symbol} in your wallet…`;
    case 'permit_tx':
      return 'Approve the router in your wallet…';
    default:
      return 'Approve in your wallet…';
  }
}

const COUNT_WORDS = ['no', 'One', 'Two', 'Three', 'Four', 'Five'];

/**
 * The promise made BEFORE the click: how many transactions this costs and what each one is. It is
 * built from the list the API sent, so a route that needs three says three — the sentence cannot
 * drift from what the flow will actually send, because both read the same array.
 */
function approvalsSentence(list: QuoteApproval[], symbol: string): string {
  const labels = list.map((a) => approvalLabel(a, symbol));
  const n = labels.length;
  const count = COUNT_WORDS[n] ?? String(n);
  return n === 1 ? `${count} one-off approval first — ${labels[0]}.` : `${count} one-off approvals first — ${labels.join(', then ')}.`;
}

const ERC20 = new Interface(['function balanceOf(address owner) view returns (uint256)']);
const ERC20_APPROVE = new Interface(['function approve(address spender, uint256 amount)']);

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
  const [maxBusy, setMaxBusy] = useState(false);
  /** The scan the Portfolio card published — the chips AND the "Pay with" rows come from it (T31 G). */
  const [portfolio, setPortfolio] = useState<PortfolioData | null>(null);
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
  /**
   * How long the number on screen has been the number on screen (T53). The quote refresh is
   * implicit — a debounce on every edit, and a silent re-quote when the router's own window
   * lapses — so the panel says how old what you are reading is, instead of counting down to
   * something that happens by itself.
   */
  const [quoteAgeS, setQuoteAgeS] = useState(0);
  const [requoteTick, setRequoteTick] = useState(0);

  const [stage, setStage] = useState<Stage>('idle');
  const [flowError, setFlowError] = useState<string | null>(null);
  const [approveHash, setApproveHash] = useState<string | null>(null);
  const [txHash, setTxHash] = useState<string | null>(null);
  const [depositId, setDepositId] = useState<string | null>(null);
  const [swapHash, setSwapHash] = useState<string | null>(null);
  const [swapDone, setSwapDone] = useState<{ from: string; to: string; amount: string } | null>(null);
  /**
   * The approvals this flow has ALREADY sent, keyed by what they are and the bytes they carry
   * (`approvalKey`) — never by position, which shifts as the list shortens. A re-quote that repeats
   * an entry is therefore not a second prompt, and a flow the user resumes after a rejection picks
   * up at the step that failed instead of re-asking for the ones that landed.
   */
  const [sentApprovals, setSentApprovals] = useState<Record<string, string>>({});
  /** The approval the wallet is holding open right now — what the button names while it waits. */
  const [approvalStep, setApprovalStep] = useState<string | null>(null);
  /**
   * A transaction that HAS been sent and is not registered yet (T31 H). It is written to storage
   * before `POST /v1/deposits` is ever called, so a reload — or a tab the user closed and came back
   * to — picks it back up; the retry loop below is what actually registers it.
   */
  const [pending, setPending] = useState<ReturnType<typeof loadPending>>(null);
  const [registerAttempt, setRegisterAttempt] = useState(0);
  const [registerGaveUp, setRegisterGaveUp] = useState<string | null>(null);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const registering = useRef(false);

  const sortedChains = useMemo(
    () => [...chains].sort((a, b) => (a.chain_id === 1 ? -1 : b.chain_id === 1 ? 1 : a.name.localeCompare(b.name))),
    [chains],
  );
  const chain: Chain | undefined = useMemo(() => chains.find((c) => c.chain_id === chainId), [chains, chainId]);
  const targetAsset = assets.find((a) => a.key === target);
  const outDecimals = assetDecimals(assets, target);

  // ---- which ingress paths are open (the API's statement; see lib/ingress.ts) ----
  const flags = useMemo(() => resolveIngress(data.ingress, ingressPartial(session.account)), [data.ingress, session.account]);
  const statedDefault = data.ingress.default_route ?? ingressPartial(session.account).default_route ?? null;
  const onEthereum = chain?.chain_id === 1;
  /**
   * T31 D2 — the toggle. It exists only when BOTH paths are open, because a control with one
   * option is a control that answers a question nobody asked. The choice starts at the user's own
   * (stored), then the API's `ingress.default_route`, then the path this client has always led
   * with; it is persisted, and it is a preference, not a claim that either path is open.
   */
  const [route, setRoute] = useState<IngressRoute>(() => initialRoute(null, storedRoute()));
  const routeTouched = useRef(storedRoute() !== null);
  useEffect(() => {
    // the API's default arrives with the reference reads, after the first render; it must not
    // overwrite a choice the user has already made with their own hands
    if (routeTouched.current || !statedDefault) return;
    setRoute(statedDefault);
  }, [statedDefault]);
  const chooseRoute = useCallback((r: IngressRoute) => {
    routeTouched.current = true;
    storeRoute(r);
    setRoute(r);
  }, []);
  const bothRoutes = flags.uniswap && flags.xchain;
  /** Where the Uniswap route applies at all: it takes ETH out, and only on Ethereum. */
  const uniswapPair = flags.uniswap && !!onEthereum && target === 'ETH' && isUniswapToken(token, data.uniswapTokens);
  const uniswapChoice = flags.uniswap && !!onEthereum && target === 'ETH' && (!bothRoutes || route === 'uniswap');
  /**
   * …and only for the tokens it has a registered gateway pool for. Asking for a route the API does
   * not have would be a 400 the user did nothing to deserve, so the request simply does not name it
   * — the quote comes back as `direct` or `swap`, and the page renders what it always did.
   *
   * The other half of the toggle asks for `"auto"` rather than `"xchain"`: from Ethereum there is
   * no cross-chain order to ask for, and the API's own resolver — the one that publishes
   * `ingress.default_route`, which is what preselected this control — is the right answer there.
   * One less way for the client to ask for something that cannot exist.
   */
  const askUniswap = uniswapChoice && uniswapPair;
  /** A source off Ethereum with the cross-chain path closed: there is nothing to quote. */
  const xchainClosed = !!chain && !onEthereum && !flags.xchain;
  /**
   * The Ethereum token list, narrowed to the pairs the Uniswap route takes — but only while that is
   * the path, and never to nothing: an empty list would be this filter's opinion, not the API's, so
   * a list that matches no pair falls back to the whole list.
   */
  const payTokens = useMemo(() => {
    if (!uniswapChoice) return tokens;
    const kept = tokens.filter((t) => isUniswapToken(t, data.uniswapTokens));
    return kept.length ? kept : tokens;
  }, [tokens, uniswapChoice, data.uniswapTokens]);
  /**
   * T31 G (admin, 2026-09-10): "Let's do pay with from portfolio as we know all balances of the
   * user, he doesn't need to select tokens he doesn't have in a balance in Pay With."
   *
   * So the picker offers the scan's holdings, and only those. The catalogue above is what the scan
   * looks THROUGH; it is offered only when there is no scan to offer instead (a wallet nobody has
   * scanned yet, or one whose chains could not be read) — "we do not know what you hold" is not the
   * same claim as "you hold nothing", and a picker with nothing in it would say the second.
   */
  const holdings = useMemo(() => (portfolio?.holdings ?? []).filter((h) => h.raw > 0n), [portfolio]);
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
   * Which `uniswap` shape this quote is — the API says so on the quote and nothing here guesses.
   * `step:"swap"` (or a `swap_tx` with no `tx`) is the TWO-STEP route (U2, 2026-09-10): a swap on
   * Uniswap V4 into the user's OWN wallet, then a plain deposit of what arrived. A `tx` is the
   * deployed-hook route: one transaction. Rendering a two-click flow as one transaction — or the
   * other way round — is the only thing this branch exists to prevent.
   */
  const twoStep = uniswap && (quote?.step === 'swap' || (!!quote?.swap_tx && !quote?.tx));
  /**
   * T46 — the transactions that must precede this swap, in the API's own order. ONE reader
   * (`quoteApprovals`) resolves the shipped `approvals[]` and the two field names it replaced, so
   * the sentence the user is shown and the loop that sends them are reading the same array.
   */
  const approvals = useMemo(() => (twoStep ? quoteApprovals(quote) : []), [twoStep, quote]);
  const approvalsLeft = approvals.filter((a) => !sentApprovals[approvalKey(a)]).length;
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
    setSentApprovals({});
    setApprovalStep(null);
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

  /**
   * The chain list already carries the native coin's symbol, so "ETH on Ethereum" is known without
   * a token list at all. Setting it here means `canQuote` — and therefore the first quote — never
   * waits for a 1.3 MB catalogue to download (T31 C3: the admin's "why is getting a quote so
   * slow?"). A token the user actually chose is never overwritten; a provisional native one (the
   * one with no logo yet) is replaced when the chain changes, and upgraded when the list lands.
   */
  useEffect(() => {
    if (!chain) return;
    setToken((cur) =>
      cur && (!isNativeToken(cur.address) || cur.logo)
        ? cur
        : { address: NATIVE_ADDRESS, symbol: chain.native_symbol, name: chain.native_symbol, decimals: 18 },
    );
  }, [chain]);

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

  // "Max" needs a balance: the scan has it when the pair was scanned. The scan is the Portfolio
  // card's to publish (one writer) — this reads what it published, at whatever age it is.
  useEffect(() => {
    if (!wallet.address || !token || chainId === null) return;
    const h = holdings.find((x) => x.chainId === chainId && x.address.toLowerCase() === token.address.toLowerCase());
    setMaxRaw(h ? h.raw : null);
  }, [wallet.address, token, chainId, holdings]);

  /**
   * The catalogue for the chains this wallet actually touches, warmed in the background so the
   * Refresh scan and the picker's search have it before they are asked for. Never on the critical
   * path: `loadTokens` de-duplicates, and a prefetch that fails is a prefetch that did nothing.
   */
  useEffect(() => {
    const ids = new Set<number>();
    if (chainId !== null) ids.add(chainId);
    if (wallet.chainId !== null) ids.add(wallet.chainId);
    for (const h of holdings) ids.add(h.chainId);
    prefetchTokens([...ids].filter((id) => chains.some((c) => c.chain_id === id)));
  }, [chainId, wallet.chainId, holdings, chains]);

  /**
   * "Max" is the one number that must be right THIS second, so it is the one read that happens
   * without the Refresh button: one balance, one RPC call — not a scan of every chain (T31 F). An
   * unreadable answer keeps the cached number rather than offering a Max of nothing.
   */
  const useMax = useCallback(async () => {
    if (!token || chainId === null || !wallet.address) return;
    setMaxBusy(true);
    try {
      const live = await readHoldingBalance(
        chainId,
        token,
        wallet.address,
        wallet.provider ? { provider: wallet.provider, chainId: wallet.chainId } : null,
      );
      const raw = live ?? maxRaw;
      if (raw === null) return;
      setMaxRaw(raw);
      setAmount(formatUnits(raw, token.decimals));
      resetFlow();
    } finally {
      setMaxBusy(false);
    }
  }, [token, chainId, wallet.address, wallet.provider, wallet.chainId, maxRaw, resetFlow]);

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
  const canQuote = !!sessionToken && !!wallet.address && !!chain && !!token && rawAmount !== null && stage !== 'tracking' && !xchainClosed;
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
            // Named on every quote (T31b item 7). `uniswap` when that is what this pair and this
            // choice mean; `auto` otherwise — which is what an omitted route always meant, said out
            // loud. From Ethereum there is no cross-chain ORDER to ask for, so the other half of the
            // toggle has no route name of its own: `auto` hands the decision to the API's ONE
            // resolver (`PGAS_INGRESS_DEFAULT_ROUTE`), which is the same value this control was
            // preselected from.
            route: askUniswap ? ('uniswap' as const) : ('auto' as const),
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
    setQuoteAgeS(Math.max(0, Math.round((Date.now() - quoteAt) / 1000)));
    const t = setInterval(() => {
      const left = expiresInSeconds(expiresAt, quoteAt);
      setExpiresIn(left);
      setQuoteAgeS(Math.max(0, Math.round((Date.now() - quoteAt) / 1000)));
      // never re-quote under a user who is looking at a changed number and deciding (stage 'confirm')
      if (left <= 0 && !busy && stage !== 'tracking' && stage !== 'confirm') setRequoteTick((n) => n + 1);
    }, 1000);
    return () => clearInterval(t);
  }, [quote, quoteAt, expiresAt, busy, stage]);

  // ---- approve / arm / deposit / register ----
  /**
   * One approval transaction, sent and waited for.
   *
   * The API builds the calldata (T46) and this sends exactly those bytes. `data` is missing on one
   * shape only — the pre-2026-09-10 `approval` field, which carried `spender`/`amount` instead —
   * and that is the ONLY case in which this file encodes anything itself.
   */
  const sendApproval = async (approval: QuoteApproval) => {
    const to = approval.to ?? approval.token;
    const payload =
      approval.data ??
      (approval.spender !== undefined && approval.amount !== undefined
        ? ERC20_APPROVE.encodeFunctionData('approve', [approval.spender, BigInt(approval.amount)])
        : undefined);
    if (!to || !payload) throw new Error('That approval came back without a transaction to send — refresh the quote.');
    const hash = await wallet.sendTransaction({
      to,
      data: payload,
      value: approval.value,
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

  /**
   * T31 H — a sent transaction is never stranded.
   *
   * The money left the wallet the moment it returned a hash; registering it is bookkeeping, and
   * bookkeeping that needs a button is bookkeeping that gets lost when the tab closes. So the hash
   * is written to storage BEFORE the call, the call retries itself with backoff, and a reload picks
   * the record up again. The user is told what is true — "sent, waiting for Ethereum to see it" —
   * not asked to press Retry.
   */
  const beginRegistration = useCallback(
    (quoteId: string, hash: string, chainIdOfTx: number) => {
      const rec = { quote_id: quoteId, hash, chain_id: chainIdOfTx, sent_at: Date.now(), address: wallet.address ?? undefined };
      savePending(rec); // ⛔ before the call, never after it
      setPending(rec);
      setRegisterAttempt(0);
      setRegisterGaveUp(null);
      setTxHash(hash);
      setStage('registering');
    },
    [wallet.address],
  );

  /** A record left behind by an earlier page: resumed once, and only for the wallet that made it. */
  const resumed = useRef(false);
  useEffect(() => {
    if (resumed.current) return;
    const p = loadPending();
    if (!p) return;
    if (p.address && wallet.address && p.address.toLowerCase() !== wallet.address.toLowerCase()) return;
    if (!wallet.address) return; // wait for the wallet: a record belongs to an account
    resumed.current = true;
    setPending(p);
    setTxHash(p.hash);
    setStage('registering');
  }, [wallet.address]);

  const refreshAccount = session.refreshAccount;
  useEffect(() => {
    if (!pending || !sessionToken || registering.current) return;
    let cancelled = false;
    void (async () => {
      registering.current = true;
      try {
        const res = await api.registerDeposit(pending.quote_id, pending.hash);
        if (cancelled) return;
        clearPending();
        setPending(null);
        setDepositId(res.deposit_id);
        setFlowError(null);
        setStage('tracking');
        void refreshAccount();
      } catch (e) {
        if (cancelled) return;
        // A definitive refusal (400/404/422 …) will never succeed: stop, drop the record, and show
        // the API's own sentence. Everything else — 409 "not visible on Ethereum yet", any 5xx, a
        // network failure — is "ask again later", and the record stays.
        if (!retryableRegistration(e)) {
          clearPending();
          setPending(null);
          setRegisterGaveUp(errorText(e));
          setStage('idle');
          return;
        }
        if (Date.now() - pending.sent_at > REGISTER_WINDOW_MS) {
          // the automatic window is over; the record and the manual button both stay
          setRegisterGaveUp(errorText(e));
          return;
        }
        retryTimer.current = setTimeout(() => {
          if (!cancelled) setRegisterAttempt((n) => n + 1);
        }, backoffMs(registerAttempt));
      } finally {
        registering.current = false;
      }
    })();
    return () => {
      cancelled = true;
      if (retryTimer.current) clearTimeout(retryTimer.current);
    };
    // `registerAttempt` is what the backoff timer bumps: each bump is one more attempt
  }, [pending, registerAttempt, sessionToken, refreshAccount]);

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
      beginRegistration(quote.quote_id, hash, data.resolveEvmChainId(tx.chain_id));
    } catch (e) {
      // registering is not in this try any more: it cannot fail here, because it does not stop
      setFlowError(errorText(e));
      setStage(quote.approval ? 'approved' : 'idle');
    }
  };

  /**
   * Step 1's allowances — every one the API sent, in the order it sent them.
   *
   * ⛔ NO OFF-CHAIN PERMIT (T31b item 9, 2026-09-10). This used to ask the wallet to sign an
   * EIP-712 `PermitSingle` — and then DROP the signature on the floor: the signature belongs
   * inside the Universal Router's `execute` (the PERMIT2_PERMIT input), the API is the one writer
   * of that calldata, and no build has ever had a field to hand the signature back through. So
   * the prompt cost the user a decision that changed nothing and taught them to sign things that
   * do nothing. The decision is: the two-step route grants its allowances with TRANSACTIONS, and
   * this client never again asks for a signature it cannot use.
   *
   * ⛔ AND THE ORDER IS NOT NEGOTIABLE. A USDT-style token refuses to raise a non-zero allowance,
   * so the API puts `approval_reset` in front of `approval`; sending them in any other order — or
   * dropping the one this file does not recognise — is a swap that reverts and a user who paid gas
   * for it. WHICH approvals are needed is the API's fact (it read both allowances); this loop's
   * only decisions are "not twice in one flow" and "stop at the first refusal".
   */
  const runApprovals = async (list: QuoteApproval[], symbol: string) => {
    for (let i = 0; i < list.length; i++) {
      const a = list[i];
      const key = approvalKey(a);
      if (sentApprovals[key]) continue; // already sent in this flow; a re-quote repeats the entry
      setApprovalStep(approvalPrompt(a, symbol));
      setStage('permitting');
      try {
        const hash = await sendApproval(a);
        setSentApprovals((cur) => ({ ...cur, [key]: hash }));
      } catch (e) {
        // named, so the banner says which of three prompts the user is looking at the failure of
        throw new Error(`Stopped at approval ${i + 1} of ${list.length} — ${approvalLabel(a, symbol)}: ${errorText(e)}`);
      } finally {
        setApprovalStep(null);
      }
    }
  };

  // ---- swap: one swap into the user's own wallet, then a fresh `direct` quote ----
  // Two routes share this: the router's single-chain `swap` mode, and the two-step Uniswap route,
  // which adds the Permit2 prompts before the same handshake. Neither swap is ever registered as a
  // deposit — it lands in the user's own wallet, and step 2 quotes what actually arrived.
  const runSwap = async () => {
    if (!quote?.swap_tx || !quote.next || !targetAsset || !sender) return;
    const nextToken = quote.next.src_token;
    const fromSymbol = quote.estimate.src.symbol;
    setFlowError(null);
    const before = await readBalanceOnEthereum(nextToken, sender, wallet.provider, wallet.chainId);
    let hash: string | null = null;
    try {
      if (twoStep) {
        // the Uniswap route carries its own allowances and sends every one the API listed, in
        // order, waiting for each receipt; the router's `swap` mode has an explicit Approve button
        // above and must not send its approval twice
        await runApprovals(approvals, fromSymbol);
      }
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
      setStage(hash ? 'swap-wait' : quote.approval && !twoStep ? 'approved' : 'idle');
    }
  };

  /** The secondary action: the loop is already trying, this asks it to try NOW. */
  const retryRegister = () => {
    if (!pending) return;
    setRegisterGaveUp(null);
    if (retryTimer.current) clearTimeout(retryTimer.current);
    setRegisterAttempt((n) => n + 1);
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
  /** The mechanism panel describes the path this page is actually on, including which Uniswap it is. */
  const howItWorksRoute: HowItWorksRoute = twoStep ? 'uniswap-two-step' : uniswapPrimary ? 'uniswap' : 'xchain';
  /**
   * Why the Uniswap half of the toggle cannot be pressed, in the words of the thing that is wrong —
   * composed from what IS true here rather than from a copy of the API's registered-pair list,
   * which is the API's fact and not this file's to restate.
   */
  const uniswapBlocked = !onEthereum
    ? 'Uniswap V4 route: Ethereum only — switch what you pay with to a holding on Ethereum.'
    : target !== 'ETH'
      ? `Uniswap V4 route: it takes ETH out — choose ETH as what you receive to use it (${target} has its own bridge pipe).`
      : !uniswapPair
        ? `Uniswap V4 route: no pool is registered for ${token?.symbol ?? 'this token'}.`
        : null;

  /**
   * T53 — what the click does, in one line, for the strip's Route row. It reads the quote's own
   * mode (and, for `uniswap`, which of its two shapes the quote says it is): a route name composed
   * from what this file WANTED would be a second opinion about a decision the API already made.
   */
  const routeLabel = twoStep
    ? 'Uniswap V4, then a direct deposit'
    : uniswap
      ? 'Uniswap V4'
      : mode === 'direct'
        ? 'Direct into the Beam bridge'
        : mode === 'swap'
          ? 'A swap in your wallet, then a direct deposit'
          : 'Cross-chain to Ethereum';
  /** Our cut, which is not charged here — `fee_bps` is the account's, and unstated when unknown. */
  const ourFeeBps = session.account?.fee_bps;
  /** What the page is doing that the button cannot say, under the button while it happens. */
  const flowStatus =
    stage === 'registering'
      ? 'Registering the deposit — Pgas.me keeps trying by itself.'
      : stage === 'swap-wait'
        ? 'Waiting for the swap to be mined…'
        : stage === 'tracking'
          ? 'Sent. Its status is below.'
          : null;

  return (
    <div className="page">
      <div className="page-head">
        <div className="stack-sm">
          <h1>Deposit</h1>
          <p className="muted">
            Pay from any chain. Fund fresh wallets later, with no on-chain link to the source. <HowItWorks route={howItWorksRoute} />
          </p>
        </div>
      </div>

      {/* T31 B′: for EVERY visitor, signed in or not — the admin's screenshot of the signed-in page
          was three cards that never said what the product is or which tab comes next. */}
      <Explainer current="deposit" />

      {/* T31 H — a sent transaction, above everything, wherever the page happens to be. It survives
          a reload, so it cannot live inside a quote card that a reload leaves empty. */}
      {pending && (
        <div className="banner banner-info" role="status" data-testid="pending-registration" style={{ marginBottom: 16 }}>
          <span>
            Sent — waiting for Ethereum to see it (
            <a href={explorerTx(pending.chain_id, pending.hash)} target="_blank" rel="noreferrer">
              view
            </a>
            ). Pgas.me keeps trying by itself; nothing else is needed from you.
          </span>
          <button type="button" className="link-btn" data-testid="register-retry" onClick={retryRegister}>
            Retry now
          </button>
        </div>
      )}
      {registerGaveUp && (
        <div className="banner banner-warn" role="status" data-testid="register-problem" style={{ marginBottom: 16 }}>
          {registerGaveUp}
        </div>
      )}

      {wallet.address ? (
        <Portfolio address={wallet.address} selectedKey={selectedKey} onPick={onPick} onLoaded={setPortfolio} />
      ) : (
        <section className="card">
          <h2>Your portfolio</h2>
          <p className="muted small" style={{ marginTop: 6 }}>
            Connect your wallet and everything it holds, on every chain, becomes a tap-to-pay chip here.
          </p>
        </section>
      )}

      {/* ═══ T53 — ONE panel, shaped like a swap ═══════════════════════════════════════════════
          Admin 2026-09-10: "Quote block I don't think is actually needed when you can make it in
          What to deposit, to look like DEX Swap." Two cards side by side asked the user to read a
          form on the left and a price on the right and hold the relationship between them in their
          head. Here the relationship IS the layout: what you pay, what you receive, what it costs,
          and the one button that does the next thing. */}
      <div className="swap-col">
        <SwapPanel testId="deposit-form" route={uniswapPrimary ? 'uniswap' : 'classic'}>
          <SwapHead
            title={uniswapPrimary ? 'Pay with Uniswap V4' : 'What to deposit'}
            sub={
              uniswapPrimary && (
                <span className="tiny muted" data-testid="uniswap-primary">
                  {twoStep ? 'two steps, both in your own wallet' : 'one transaction on Ethereum'}
                </span>
              )
            }
          >
            {/* T31 D2 — only when BOTH paths are open. One option is not a choice, and a control
                that offers one would be a control the user has to dismiss. The provider behind
                the cross-chain path is never named: "Cross-chain" is what it is to a depositor.
                It stays in the head rather than in the detail strip below (where T53's brief put
                it) because it must be pressable BEFORE there is an amount to price. */}
            {bothRoutes && (
              <div className="seg seg-sm" role="radiogroup" aria-label="Route" data-testid="route-toggle" data-route={route}>
                <button
                  type="button"
                  role="radio"
                  aria-checked={route === 'xchain'}
                  className={route === 'xchain' ? 'active' : ''}
                  disabled={busy}
                  data-testid="route-xchain"
                  onClick={() => {
                    chooseRoute('xchain');
                    resetFlow();
                  }}
                >
                  Cross-chain
                </button>
                <button
                  type="button"
                  role="radio"
                  aria-checked={route === 'uniswap'}
                  className={route === 'uniswap' ? 'active' : ''}
                  disabled={busy || !!uniswapBlocked}
                  title={uniswapBlocked ?? 'Swap on Uniswap V4, then deposit'}
                  data-testid="route-uniswap"
                  onClick={() => {
                    chooseRoute('uniswap');
                    resetFlow();
                  }}
                >
                  Uniswap V4
                </button>
              </div>
            )}
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
          </SwapHead>

          {/* T31 B′ — the admin could not tell what this card wanted from him. One line, above it. */}
          <p className="tiny muted" data-testid="deposit-lead">
            Pick something you hold — the chips above are your wallet — choose what your balance is kept in, and enter an amount.
          </p>

          {/* `quote-card` is this region now: the price and everything that follows from it. The
              suite's assertions about what a quote may never say (a quote id, an order id, a
              countdown) are assertions about exactly this, and the How-it-works page crops it. */}
          <div className="sw-body" data-testid="quote-card">
            <div className="sw-legs">
              <SwapLeg
                label="You pay"
                aside={
                  <>
                    {maxRaw !== null && token && (
                      <span className="num">
                        Balance {fmtUnits(maxRaw, token.decimals)} {token.symbol}
                      </span>
                    )}
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      disabled={maxRaw === null || !token || busy || maxBusy}
                      title={
                        maxRaw === null
                          ? 'Balance unknown — pick the holding from the portfolio above'
                          : 'Use the full balance, re-read from the chain'
                      }
                      data-testid="max-btn"
                      onClick={() => void useMax()}
                    >
                      {maxBusy ? 'Reading…' : 'Max'}
                    </button>
                  </>
                }
                control={
                  <PayWithSelect
                    chains={sortedChains}
                    chainId={chainId}
                    tokens={payTokens}
                    holdings={holdings}
                    selectedKey={selectedKey}
                    onPickHolding={onPick}
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
                }
                sub={
                  <>
                    {typeof est?.usd === 'number' && est.usd > 0 && (
                      <span className="num" data-testid="amount-usd">
                        ≈ {fmtUsd(est.usd)}
                      </span>
                    )}
                    {amount !== '' && rawAmount === null && <span className="error-text">Enter a positive number.</span>}
                    {tokensError && <span className="error-text">Token list: {tokensError}</span>}
                  </>
                }
              >
                <input
                  id="amount"
                  className="sw-amount num"
                  inputMode="decimal"
                  placeholder="0.0"
                  value={amount}
                  disabled={busy}
                  aria-label={`Amount${token ? ` (${token.symbol})` : ''}`}
                  aria-invalid={amount !== '' && rawAmount === null}
                  onChange={(e) => {
                    setAmount(e.target.value);
                    resetFlow();
                  }}
                />
              </SwapLeg>

              <SwapSeam />

              <SwapLeg
                tone="out"
                label={`You receive on Ethereum${quoteLoading && quote ? ' · refreshing…' : ''}`}
                control={
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
                }
                sub={
                  quote && est ? (
                    <>
                      {/* The two numbers only the Uniswap route has, and the only slippage bound
                          that works on it: `min_out_units` is what the swap itself reverts below. */}
                      {uniswap && est.min_out_units && (
                        <span data-testid="min-out">
                          min you receive {fmtUnits(est.min_out_units, outDecimals, target === 'WBTC' ? 8 : 6)} {quote.target_asset}
                        </span>
                      )}
                      {uniswap && typeof est.price_impact_bps === 'number' && (
                        <span data-testid="price-impact">price impact {(est.price_impact_bps / 100).toFixed(2)}%</span>
                      )}
                    </>
                  ) : undefined
                }
              >
                {quote && est ? (
                  <span data-testid="quote-out">{fmtUnits(est.out_units, outDecimals, target === 'WBTC' ? 8 : 6)}</span>
                ) : (
                  <span className="muted">0.0</span>
                )}
              </SwapLeg>
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
                // T31 B′ — an empty panel that says only "enter an amount" tells a new user nothing
                // about what they are about to see, or what it costs.
                <p className="muted small" data-testid="quote-empty">
                  Enter an amount and you&rsquo;ll see what lands in your private balance — as {target} on Ethereum through the Beam bridge
                  — how long it takes and what the bridge charges. Then one click to deposit.
                </p>
              ) : (
                <div className="stack">
                  {stage === 'tracking' && <div className="banner banner-ok">Transaction sent — follow the status below.</div>}
                  {quoteLoading && !quote && <p className="muted small">Getting a quote…</p>}
                  {quoteError &&
                    (belowMin ? (
                      // the only place a floor is ever stated, and it is the API's own sentence
                      <div className="banner banner-warn" data-testid="min-deposit">
                        {quoteError}
                      </div>
                    ) : (
                      <div className="banner banner-error">{quoteError}</div>
                    ))}
                  {quote && est && (
                    <>
                      {/* ---- the detail strip: what this costs and where it goes ---- */}
                      <SwapDetails
                        summary={
                          <>
                            <span>What this costs</span>
                            {/* The refresh is implicit — every edit re-quotes, and a lapsed
                                router window re-quotes itself — so what is worth saying is how
                                old the number you are reading is, next to the numbers. The
                                countdown that used to be here was a clock nobody could act on. */}
                            {!expired && (
                              <span className="sw-fresh" data-testid="quote-age">
                                <span>updated {quoteAgeS < 5 ? 'just now' : `${quoteAgeS} s ago`}</span>
                                <button
                                  type="button"
                                  className="link-btn"
                                  disabled={busy}
                                  onClick={(e) => {
                                    // inside a <summary>: refreshing the quote must not also
                                    // fold the strip the user is reading
                                    e.preventDefault();
                                    e.stopPropagation();
                                    setRequoteTick((n) => n + 1);
                                  }}
                                >
                                  refresh
                                </button>
                              </span>
                            )}
                          </>
                        }
                      >
                        <SwapRow label="Route">{routeLabel}</SwapRow>
                        {/* Ours is charged on the way OUT and never here — said on the page where
                            a depositor is deciding, not left for them to find later. The number is
                            the account's `fee_bps`; a build that has not answered yet says the
                            rule without a number rather than a number this file made up. */}
                        <SwapRow label="Our fee">
                          {ourFeeBps === undefined ? 'charged at withdrawal, not here' : `${ourFeeBps / 100} % at withdrawal, not here`}
                        </SwapRow>
                        {/* One line where two rows of bookkeeping used to be ("Paying …",
                            "Credited on Beam …"): what a depositor waits for is the wait, and the
                            bridge's cut is a cent — a number worth rounding, not stating to six
                            decimals. */}
                        <SwapNote testId="lands-in">
                          Lands in your balance in ≈ {fmtDuration(est.eta_s)}
                          {relayerFeeUsd !== null ? ` · bridge fee ${fmtUsd(relayerFeeUsd)}` : ''}
                        </SwapNote>
                        {mode === 'direct' && (
                          <SwapNote tone="good" testId="direct-note">
                            Direct deposit — your {quote.target_asset} goes straight into the Beam bridge, no routing fee.
                          </SwapNote>
                        )}
                        {uniswap && !twoStep && canDeposit && (
                          <SwapNote testId="uniswap-note">
                            via Uniswap V4 — one transaction, the hook locks your ETH in the Beam bridge
                          </SwapNote>
                        )}
                        {/* T31 A (admin, 2026-09-10): "You need to provide info that deposit comes
                            to ETH blockchain from where it will be distributed among chains user
                            wants." Truthful about today: payouts are ETH on Ethereum, and
                            multi-chain payout is designed, not live — so it is named as what is
                            enabled, not as what exists. */}
                        <SwapNote testId="arrives-as">
                          Arrives as {quote.target_asset} on Ethereum through the Beam bridge. From your balance you schedule payouts to the
                          wallets you choose — on Ethereum now, other chains as they are enabled.
                        </SwapNote>
                      </SwapDetails>

                      {/* ---- the action: one primary button, whose label is the next step ---- */}
                      <SwapAction>
                        {swapDone && (
                          <div className="banner banner-ok" data-testid="swap-done">
                            Swapped {swapDone.from} → {swapDone.amount} {swapDone.to}. Step 2: deposit it below.
                          </div>
                        )}
                        {(mode === 'swap' || twoStep) && quote.swap_tx ? (
                          /* Two routes, one handshake: the router's single-chain `swap`, and the
                             two-step Uniswap route (U2). Both land the asset in the USER's own
                             wallet and both are followed by a plain direct deposit of what actually
                             arrived — so the only difference here is the words and, on Uniswap, the
                             one-off Permit2 allowances the Universal Router needs (T31b item 9:
                             allowances, never an off-chain permit this client could not use). */
                          <div className="stack-sm" data-testid={twoStep ? 'uniswap-step1' : 'swap-panel'}>
                            <span className="strong small">
                              {twoStep
                                ? `1 · Swap ${est.src.symbol} → ${quote.target_asset} on Uniswap V4`
                                : `Step 1 — Swap ${est.src.symbol} → ${quote.target_asset} in your wallet`}
                            </span>
                            <span className="help">
                              {est.src.symbol} is not {quote.target_asset}.{' '}
                              {twoStep
                                ? `Step 1 swaps it on Uniswap V4 into your own wallet; the ${quote.target_asset} lands in your wallet, never ours.`
                                : `Step 1 swaps it in your own wallet; the ${quote.target_asset} lands in your wallet, never ours.`}
                            </span>
                            {quote.approval && !twoStep && (
                              <>
                                <button
                                  type="button"
                                  className={`btn btn-lg${needsApproval ? ' btn-primary' : ''}`}
                                  disabled={!needsApproval || busy || expiresIn <= 0}
                                  onClick={approve}
                                  data-testid="approve-btn"
                                >
                                  {stage === 'approving'
                                    ? 'Waiting for approval…'
                                    : needsApproval
                                      ? `Approve ${est.src.symbol}`
                                      : 'Approved'}
                                </button>
                                {approveHash && (
                                  <a
                                    href={explorerTx(data.resolveEvmChainId(quote.approval.chain_id), approveHash)}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="tiny"
                                  >
                                    approval tx
                                  </a>
                                )}
                              </>
                            )}
                            <button
                              type="button"
                              /* ⛔ ONE PRIMARY AT A TIME (T53): while an allowance is still owed
                                 this is the step AFTER the one the user can take, and a second
                                 teal button is a second thing to press. It goes quiet until it
                                 is the thing to press. */
                              className={`btn btn-lg${needsApproval && !twoStep ? '' : ' btn-primary'}`}
                              disabled={(needsApproval && !twoStep) || busy || expiresIn <= 0}
                              onClick={runSwap}
                              data-testid="swap-btn"
                            >
                              {stage === 'approving'
                                ? 'Waiting for approval…'
                                : stage === 'permitting'
                                  ? (approvalStep ?? 'Approve in your wallet…')
                                  : stage === 'swapping'
                                    ? 'Confirm in the wallet…'
                                    : stage === 'swap-wait'
                                      ? 'Waiting for the swap…'
                                      : `Swap ${fmtUnits(est.src.amount, est.src.decimals)} ${est.src.symbol}`}
                            </button>
                            {swapHash && (
                              <a href={explorerTx(1, swapHash)} target="_blank" rel="noreferrer" className="tiny">
                                swap tx
                              </a>
                            )}
                            {/* T46: the promise and the progress come from the SAME array the flow
                                sends, so "two approvals first" can never be said about a route that
                                is about to ask for three. `data-sent` is how far it got. */}
                            {twoStep && approvals.length > 0 && (
                              <SwapNote
                                testId="permit-note"
                                attrs={{ 'data-count': approvals.length, 'data-sent': approvals.length - approvalsLeft }}
                              >
                                {approvalsSentence(approvals, est.src.symbol)}
                                {approvalsLeft === 0 ? ' Approved.' : ''} They are asked for once, then never again for this token.
                              </SwapNote>
                            )}
                            <SwapNote>
                              {twoStep ? '2 · Deposit' : 'Step 2'} appears by itself: Pgas.me re-quotes the {quote.target_asset} that
                              actually arrived as a direct deposit.
                            </SwapNote>
                          </div>
                        ) : !canDeposit ? (
                          // the API's own note here is `ingress not armed: no Beam pubkey
                          // configured`, which names a flag and a key the user has never heard of
                          <div className="banner banner-warn" data-testid="unarmed-banner">
                            <span>Deposits are paused right now — this is a preview of what you would get.</span>
                          </div>
                        ) : (
                          stage !== 'tracking' && (
                            <>
                              {/* The Uniswap route has no button of its own here: its spender is
                                  the router in the quote, so the one Deposit press approves and
                                  deposits. */}
                              {quote.approval && !uniswap && (
                                <>
                                  <button
                                    type="button"
                                    className={`btn btn-lg${needsApproval ? ' btn-primary' : ''}`}
                                    disabled={!needsApproval || busy || expiresIn <= 0}
                                    onClick={approve}
                                    data-testid="approve-btn"
                                  >
                                    {stage === 'approving'
                                      ? 'Waiting for approval…'
                                      : needsApproval
                                        ? `Approve ${est.src.symbol}`
                                        : 'Approved'}
                                  </button>
                                  {approveHash && (
                                    <a
                                      href={explorerTx(data.resolveEvmChainId(quote.approval.chain_id), approveHash)}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="tiny"
                                    >
                                      approval tx
                                    </a>
                                  )}
                                </>
                              )}
                              {needsConfirm && armed && (
                                <div className="banner banner-warn" data-testid="estimate-changed">
                                  <span>
                                    The cross-chain order came back at{' '}
                                    {fmtUnits(armed.estimate.out_units, outDecimals, target === 'WBTC' ? 8 : 6)} {quote.target_asset}, not{' '}
                                    {fmtUnits(quote.estimate.out_units, outDecimals, target === 'WBTC' ? 8 : 6)}. Tap Deposit again to take
                                    it.
                                  </span>
                                </div>
                              )}
                              <button
                                type="button"
                                /* one primary at a time — see the swap button above */
                                className={`btn btn-lg${needsApproval ? '' : ' btn-primary'}`}
                                disabled={needsApproval || busy || expiresIn <= 0}
                                onClick={send}
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
                                          ? `Deposit at ${fmtUnits(est.out_units, outDecimals, target === 'WBTC' ? 8 : 6)} ${
                                              quote.target_asset
                                            }`
                                          : // the chain lives in the label, so no sentence has to explain the switch
                                            `Deposit ${fmtUnits(est.src.amount, est.src.decimals)} ${est.src.symbol}${
                                              txChainName ? ` on ${txChainName}` : ''
                                            }`}
                              </button>
                            </>
                          )
                        )}
                        {flowError && <div className="banner banner-error">{flowError}</div>}
                        {flowStatus && (
                          <p className="sw-status" data-testid="deposit-status">
                            {flowStatus}
                          </p>
                        )}
                      </SwapAction>
                    </>
                  )}
                </div>
              )}
            </SignInGate>
          </div>
        </SwapPanel>

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
  const { wallet, route } = useStore();
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
          {/* T31 B′ — the end of one job is the start of the next, and the next one is a tab away. */}
          {status === 'credited' && (
            <button type="button" className="btn btn-sm btn-primary" data-testid="go-schedule" onClick={() => route.navigate('schedule')}>
              Now schedule payouts →
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
            // T37/T31 H: the API accepts a hash Ethereum has not shown it yet and proves it later.
            // `verified:false` is a row still being checked — it is not a failure and must not read
            // like one, so it says what is happening rather than nothing at all.
            if (i === 0 && status === 'submitted' && deposit?.verified === false) sub = 'confirming the transaction';
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
  holdings,
  selectedKey,
  onPickHolding,
  value,
  onChange,
  onChainChange,
  loading,
  disabled,
}: {
  chains: Chain[];
  chainId: number | null;
  tokens: Token[];
  /** The wallet's own balances, from the scan the Portfolio card published (T31 G). */
  holdings: Holding[];
  selectedKey?: string | null;
  onPickHolding: (h: Holding) => void;
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

  /**
   * T31 G — the rows ARE the portfolio: what the wallet holds, grouped by chain, biggest first,
   * with the balance and what it is worth on each. Picking one sets the chain and the token at
   * once, exactly as tapping a chip does, because they are the same holding.
   *
   * The catalogue below is the fallback for ONE case: no holding to show. "We found nothing" is
   * not proof that the wallet is empty — a public RPC can fail, a chain can be skipped, a token
   * can sit outside the head of a list — and locking someone out of their own deposit over a scan
   * that did not answer is worse than offering the list. The empty state says which it is.
   */
  const useHoldings = holdings.length > 0;

  const groups = useMemo(() => {
    const s = q.trim().toLowerCase();
    const kept = s
      ? holdings.filter(
          (h) =>
            h.symbol?.toLowerCase().includes(s) ||
            h.name?.toLowerCase().includes(s) ||
            h.chainName?.toLowerCase().includes(s) ||
            h.address?.toLowerCase() === s,
        )
      : holdings;
    const byChain = new Map<number, Holding[]>();
    for (const h of kept) {
      const list = byChain.get(h.chainId) ?? [];
      list.push(h);
      byChain.set(h.chainId, list);
    }
    // the chain with the most value first, and inside it the holdings in the order the scan
    // already sorted them (USD, then native, then size) — one ordering, not two
    return [...byChain.entries()]
      .map(([id, list]) => ({ chainId: id, name: list[0].chainName, list, usd: list.reduce((t, h) => t + (h.usd ?? 0), 0) }))
      .sort((a, b) => b.usd - a.usd || a.name.localeCompare(b.name));
  }, [holdings, q]);

  const flat = useMemo(() => groups.flatMap((g) => g.list), [groups]);

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
  const pickHolding = (h: Holding) => {
    onPickHolding(h);
    setOpen(false);
    setQ('');
  };

  const count = useHoldings ? flat.length : filtered.length;
  const choose = (i: number) => {
    if (useHoldings) {
      if (flat[i]) pickHolding(flat[i]);
    } else if (filtered[i]) pick(filtered[i]);
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
        data-source={useHoldings ? 'holdings' : 'catalogue'}
        data-chain-id={chainId ?? undefined}
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
            placeholder={useHoldings ? 'Search your balances' : 'Search by symbol, name or address'}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault();
                setActive((a) => Math.min(a + 1, count - 1));
              } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setActive((a) => Math.max(a - 1, 0));
              } else if (e.key === 'Enter') {
                e.preventDefault();
                choose(active);
              } else if (e.key === 'Escape') setOpen(false);
            }}
            aria-label={useHoldings ? 'Search your balances' : 'Search tokens'}
          />
          {!useHoldings && (
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
          )}
          <div className="combo-list" role="listbox" data-testid="pay-with-list">
            {useHoldings ? (
              <>
                {flat.length === 0 && <div className="empty">No holding matches</div>}
                {groups.map((g) => (
                  <div key={g.chainId} className="pw-group" data-chain-group={g.chainId}>
                    <div className="pw-group-head">{g.name}</div>
                    {g.list.map((h) => {
                      const i = flat.indexOf(h);
                      return (
                        <button
                          key={h.key}
                          type="button"
                          role="option"
                          aria-selected={selectedKey === h.key}
                          className={`combo-item pw-holding${i === active ? ' active' : ''}`}
                          data-holding={h.key}
                          onClick={() => pickHolding(h)}
                          onMouseEnter={() => setActive(i)}
                        >
                          <PayWithIcons
                            token={{
                              address: h.native ? NATIVE_ADDRESS : h.address,
                              symbol: h.symbol,
                              name: h.name,
                              decimals: h.decimals,
                              logo: h.logo,
                            }}
                            chainId={h.chainId}
                          />
                          <span className="ci-sym">{h.symbol}</span>
                          <span className="ci-name">{h.native ? 'native' : h.name}</span>
                          <span className="pw-bal num">
                            <span>{fmtNumber(h.amount, h.amount >= 1 ? 4 : 6)}</span>
                            {h.usd !== undefined && h.usd >= 0.01 && <span className="tiny muted">{fmtUsd(h.usd)}</span>}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                ))}
              </>
            ) : (
              <>
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
              </>
            )}
          </div>
          {!useHoldings && (
            <div className="tiny muted pw-empty-note" data-testid="pay-with-empty">
              No balances found in this wallet — Refresh above, or fund the wallet first. Until then, every token on the chain is listed.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
