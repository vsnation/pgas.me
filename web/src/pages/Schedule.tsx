// Schedule: a list of orders — address, amount, when it should be there — and nothing else.
//
// Admin 2026-09-09: "user don't need to sign message or connect wallet from each wallet he wants to
// deposit ETH to. It should be a list of addresses and amounts where user can deposit. He always can
// extend and add one more. Every address scheduled like new order, user can select time when it
// should be deposited (always think that bridge takes 66 minutes, so you should send to bridge 1
// hour before user request). Always check total amount, user can't exceed total amount."
//
// So: no destination registry, no signature from the wallet being funded, no keys made in the
// browser. An address is pasted and checked (shape, then
// EIP-55 — see lib/schedule.ts), each row is its own order, and the batch is validated as one unit
// against Available before the button will do anything.
//
// MONEY (2026-09-10, admin: "Decrease minimal amount to few $. But make sure fees are covered
// (bridge + our 2%)" → "withdrawal can be any"): the charge is our 2 % PLUS the bridge crossing at
// cost, itemised, and there is no economic minimum any more. Not one of those numbers is computed
// here — `POST /v1/withdrawals/preview` prices the list on every change and this page renders what
// it says, because the bridge fee is a live gas read and a second implementation of it would be a
// second answer (law: one writer per fact). What is left here is formatting.
//
// The same rule covers the VERDICTS, not just the numbers (F4/F5, 2026-09-10): the preview says
// per item whether that item may be scheduled (`ok`/`problem`) and, in `batch`, whether the list
// as a list fits inside Available. Both come back again from `POST /v1/withdrawals` when it
// refuses — 422 `detail:{message, items, min_amount_groth}` — priced against the balance and the
// gas price of that instant. The page renders each of them where it can be acted on: an item's
// problem on its row, the batch's under the totals, the message in the banner. It composes none
// of them, and it never puts a structured refusal on the screen as JSON — that blob listed every
// destination address in the batch.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { PayoutEta, PayoutStatusCell } from '../components/PayoutStatus';
import { SignInGate } from '../components/SignInGate';
import { SwapAction, SwapDetails, SwapHead, SwapLeg, SwapNote, SwapPanel, SwapRow, SwapSeam } from '../components/SwapPanel';
import { ApiError, api, errorText } from '../lib/api';
import { fmtGroth, parseGroth, shortAddr } from '../lib/format';
import {
  BRIDGE_ETA_FALLBACK_S,
  DEFAULT_PRESET,
  DELIVERY_PRESETS,
  checkAddress,
  deliveryHint,
  fromLocalInput,
  grothDecimal,
  parsePasteList,
  presetDeliverAt,
  toCanonicalList,
  toLocalInput,
  type PasteEntry,
  type PresetId,
} from '../lib/schedule';
import {
  FROM_AMOUNT_NOTE,
  clearSchedulePrefill,
  debitedGroth,
  deliveredGroth,
  feeMode,
  payoutView,
  readSchedulePrefill,
  refundedIds,
  requestedGroth,
  shortTime,
  type PayoutRow,
} from '../lib/payouts';
import {
  batchVerdict,
  bridgeFeeNow,
  deliveredTotal,
  treasuryVerdict,
  type AssetKey,
  type PricedWithdrawalItem,
  type WithdrawalFees,
  type WithdrawalItem,
  type WithdrawalPreview,
  type WithdrawalPreviewItem,
  type WithdrawalResponse,
} from '../lib/types';
import { useStore } from '../state/store';

/** The restructured withdrawal is an ETH order book (API_CONTRACT.md § Withdrawals). */
const ASSET: AssetKey = 'ETH';
const NOW_TICK_MS = 30_000;
/**
 * How long the form waits after the last change before it asks the API what the list costs. Every
 * row change re-quotes: the bridge fee is priced off the live gas price at request time, so a
 * number that sat on screen through five edits is not this list's number.
 */
const PREVIEW_DEBOUNCE_MS = 300;
/**
 * The asset's grid — one groth, the smallest positive amount there is. `min_amount_groth` equal to
 * it is not a minimum, it is "any amount", and the form says nothing about a floor at all.
 */
const GRID_GROTH = 1;

/** The last preview that answered, and the exact request body it answers. */
interface Preview {
  /** anything else on screen makes it stale; the items line up with the priced rows by position */
  key: string;
  data: WithdrawalPreview;
}

/**
 * A refusal from `POST /v1/withdrawals`, kept against the exact list it refused.
 *
 * The API prices the batch AGAIN when the button is pressed — the gas price moves, the balance
 * moves — so its per-item verdicts, not the last preview's, are what the rows must show. And they
 * are about THAT list: the moment the user changes anything, a refusal about the old list is not
 * about anything on screen and is dropped rather than left sitting on a row that has been fixed.
 */
interface Refusal {
  key: string;
  items: WithdrawalPreviewItem[];
  /** the floor the API ruled those items against, when it stated one */
  min_amount_groth?: number;
}

interface Row {
  id: number;
  address: string;
  amount: string;
  preset: PresetId;
  custom: string; // datetime-local value, used only while preset === 'custom'
  /**
   * Has the user been in this row yet (typed in it, changed its time, or left one of its fields)?
   * Screen review 2026-09-10: a form that opens with "row 1: enter an address · row 1: enter an
   * amount" in red is telling the user off for not having started. Validation is the same either
   * way — what waits for a touch is SAYING it.
   */
  touched?: boolean;
}

let nextRowId = 1;
const blankRow = (): Row => ({ id: nextRowId++, address: '', amount: '', preset: DEFAULT_PRESET, custom: '' });

/** An untouched row with neither an address nor an amount in it is not an order — it is a blank. */
const isBlankRow = (r: Row): boolean => !r.address.trim() && !r.amount.trim();

export function WithdrawBody({ modes }: { modes: ReactNode }) {
  const { session, wallet } = useStore();
  const account = session.account;

  // ── T48 block: the form opens on the order the Balance page handed it ────────────────────────
  //
  // "Schedule again" on a returned order leaves the wallet and the amount THAT WAS ASKED FOR in
  // this tab's own memory (lib/payouts.ts — never in the URL, which is a link that gets copied and
  // logged) and navigates here. The order is not re-submitted: it arrives as a row in the form,
  // priced by the API like any other, and the user presses the button.
  //
  // `touched` because a row the user BROUGHT may say what is wrong with it straight away, the same
  // rule a pasted line follows. ASAP is named rather than inherited from `DEFAULT_PRESET`: the
  // press said "again", and again means now.
  //
  // ⛔ read here, cleared after the mount — never consumed in this initialiser, which React runs
  // twice in development.
  const [rows, setRows] = useState<Row[]>(() => {
    const again = readSchedulePrefill();
    if (!again) return [blankRow()];
    return [{ ...blankRow(), address: again.W, amount: grothDecimal(again.groth), preset: 'asap', touched: true }];
  });
  useEffect(() => clearSchedulePrefill(), []);
  // ─────────────────────────────────────────────────────────────────────────────────────────────
  const [fees, setFees] = useState<WithdrawalFees | null>(null);
  const [feesError, setFeesError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  /**
   * One counter for every preview this page has intended to send. A response whose number is not
   * the current one is answering a list that is no longer on screen and is dropped — an older
   * quote that arrives later must never become the number the user is looking at.
   */
  const previewSeq = useRef(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [result, setResult] = useState<WithdrawalResponse | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [nowS, setNowS] = useState(() => Math.floor(Date.now() / 1000));
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [pasteReplace, setPasteReplace] = useState(false);
  const [exported, setExported] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setNowS(Math.floor(Date.now() / 1000)), NOW_TICK_MS);
    return () => clearInterval(t);
  }, []);

  // fees before the form means the minimum, the fee and the bridge ETA are the API's numbers, not a
  // second copy of them living here (the fallbacks below only cover the seconds before it answers)
  const signedIn = !!session.session;
  useEffect(() => {
    if (!signedIn) return;
    let alive = true;
    api.withdrawalFees(ASSET).then(
      (f) => alive && (setFees(f), setFeesError(null)),
      (e) => alive && setFeesError(errorText(e)),
    );
    return () => {
      alive = false;
    };
  }, [signedIn]);

  // bps → per cent is formatting, not arithmetic on money: the fee itself is only ever the API's.
  const feeBps = fees?.fee_bps ?? account?.fee_bps ?? 200;
  const etaS = fees?.bridge_eta_s ?? BRIDGE_ETA_FALLBACK_S;
  const bridgeNow = bridgeFeeNow(fees);

  const items = rows.map((r) => {
    const addr = checkAddress(r.address);
    const groth = parseGroth(r.amount);
    const deliverAt = r.preset === 'custom' ? fromLocalInput(r.custom) : presetDeliverAt(r.preset, new Date(nowS * 1000));
    // "is this a number at all" is the client's; "is this amount allowed" is the API's, and it
    // comes back per item from the preview. Two implementations of one rule is how they diverge.
    const amountProblem = groth === null ? 'enter an amount' : null;
    const timeProblem = deliverAt === null ? 'pick a date and time' : null;
    const blank = isBlankRow(r);
    return {
      row: r,
      address: addr.ok ? addr.address : null,
      addressProblem: addr.ok ? null : addr.reason,
      groth,
      amountProblem,
      deliverAt,
      timeProblem,
      valid: addr.ok && !amountProblem && !timeProblem,
      /** nothing in it: it is the row "+ Add another address" made, not an order the user wants */
      blank,
      /** its problems may be SAID: the user has been in it and left something in it */
      speak: !blank && !!r.touched,
    };
  });
  /** The rows that are orders. A blank row neither counts, nor blocks, nor complains. */
  const active = items.filter((i) => !i.blank);
  /** The body `submit` sends. */
  const submitItems: WithdrawalItem[] = active.map((i) => ({ W: i.address!, amount_groth: i.groth!, deliver_at: i.deliverAt! }));
  /**
   * The list AS THE USER LEFT IT, and the identity a refusal about it is kept under. Deliberately
   * NOT the request body: an "ASAP" row's `deliver_at` is recomputed every 30 s from the clock, so
   * a body-shaped key would drop the API's refusal off the screen on a tick, with nothing on the
   * page having changed. Anything the user actually does change — an address, an amount, a time,
   * a row appearing or going — changes this, and then the refusal is about a list that no longer
   * exists and is dropped, which is the point.
   */
  const listKey = JSON.stringify(active.map((i) => [i.row.id, i.row.address.trim(), i.row.amount.trim(), i.row.preset, i.row.custom]));
  /** Only a refusal of the list ON SCREEN may be shown on it. */
  const refused = refusal && refusal.key === listKey ? refusal : null;
  /** row id → the API's refusal for it, by position in the list that was sent. */
  const refusedByRow = new Map<number, string>();
  refused?.items.forEach((it, k) => {
    const row = active[k];
    if (row && it && it.ok === false && typeof it.problem === 'string' && it.problem) refusedByRow.set(row.row.id, it.problem);
  });

  /**
   * The minimum is the API's number, and since 2026-09-10 it is a technical floor only: the old
   * economic one ("our 2 % has to cover the relayer fee") is gone because the crossing is charged
   * explicitly. Freshest source wins — a refusal ruled on THIS list a moment ago, the preview
   * answered for it, `/fees` answered for this asset, the account is whatever the last poll
   * carried. (A row refused for being under a floor while the hint beside it reads "any amount"
   * is the screen contradicting itself; the refusal is the newest thing the API has said.)
   */
  const minGroth =
    refused?.min_amount_groth ?? preview?.data.min_amount_groth ?? fees?.min_amount_groth ?? account?.min_payout_groth ?? GRID_GROTH;
  /** A floor equal to the grid is not a floor. Nothing on screen states one when there is none. */
  const hasMinimum = minGroth > GRID_GROTH;
  const minLabel = `${fmtGroth(minGroth)} ${ASSET}`;

  // Parsed live as it is typed, so a bad separator or a mistyped address is visible before the user
  // presses anything. Nothing is written into the rows until "Add to the list".
  const paste = useMemo(
    () => (pasteText.trim() ? parsePasteList(pasteText, { minGroth, minLabel, now: new Date(nowS * 1000) }) : null),
    [pasteText, minGroth, minLabel, nowS],
  );

  /** Every row that is complete enough to be exported (a half-typed row is not a line in a list). */
  const exportable = items
    .filter((i) => i.address && i.groth !== null && i.deliverAt !== null)
    .map((i) => ({ address: i.address!, groth: i.groth!, deliverAt: i.deliverAt! }));

  /** The rows that can be PRICED: an address, an amount and a time. Half a row is not an order. */
  const priceable = active.filter((i) => i.address && i.groth !== null && i.deliverAt !== null);
  /** The request body, verbatim. It is also the identity of the answer: a different body, a stale answer. */
  const previewItems: WithdrawalItem[] = priceable.map((i) => ({ W: i.address!, amount_groth: i.groth!, deliver_at: i.deliverAt! }));
  const previewKey = JSON.stringify(previewItems);
  /** Only an answer to the list ON SCREEN is allowed to be a number the user reads. */
  const fresh = preview && preview.key === previewKey ? preview.data : null;
  /**
   * row id → the API's numbers for it. Same body, same order, so position is the identity.
   * `PricedWithdrawalItem` is the same dict plus T35's three fields (`delivered_groth`,
   * `debited_groth`, `fee_mode`), each optional — a build that predates them renders a dash.
   */
  const priced = new Map<number, PricedWithdrawalItem>();
  if (fresh) priceable.forEach((i, k) => fresh.items[k] && priced.set(i.row.id, fresh.items[k]));
  const totals = fresh?.totals ?? null;
  const amountGroth = totals?.amount_groth ?? 0;
  const feeGroth = totals?.fee_groth ?? 0;
  const bridgeGroth = totals?.bridge_fee_groth ?? 0;
  const debitGroth = totals?.total_debited_groth ?? 0;
  /**
   * Σ what the wallets receive. It differs from Σ amounts only when a row had to take its fees out
   * of the amount, and it is the API's own total — never a sum made here, and never a stand-in
   * taken from `amount_groth` (that would be this page claiming a delivery the API did not state).
   */
  const deliveredGrothTotal = deliveredTotal(totals);
  /** does any priced row deliver less than it asked for? Then the totals say so, not just the row. */
  const anyFromAmount = !!fresh && (fresh.items as PricedWithdrawalItem[]).some((i) => feeMode(i) === 'from_amount');
  const available = fresh?.available_groth ?? account?.balances[ASSET]?.available ?? 0;
  /**
   * The batch rule — Σ total ≤ Available — as the API ruled on it. It is a verdict on the LIST,
   * not on any row: the row that happens to take the running total past Available is not a wrong
   * row, and marking it as one (which the mock API used to do, and the real one never did) tells
   * the user to fix the wrong thing. `POST /v1/withdrawals` refuses on the same computation, so
   * the form asks rather than deciding.
   */
  const batch = batchVerdict(fresh);
  /**
   * Over Available. The API's verdict when it publishes one; its own two numbers when it does not
   * (a build from before 2026-09-10) — a comparison that only ever REFUSES, and never admits: what
   * may be scheduled is decided by `POST /v1/withdrawals`, atomically, against the balance as it is
   * at that moment.
   */
  const overBudget = batch ? !batch.ok : !!totals && debitGroth > available;
  /** Why, in the API's words. There is no sentence composed here to stand in for a missing one. */
  const batchProblem = batch && !batch.ok ? (batch.problem ?? null) : null;
  /**
   * ⛔ THE OTHER VERDICT, AND IT IS NOT ABOUT THE USER'S MONEY AT ALL (T52, 2026-09-10). `batch`
   * asks whether Available covers this list; `treasury` asks whether we can MOVE it today — the
   * bETH shielded last night is inside a max-privacy lock for up to 72 hours and no top-up
   * shortens it. Three of the admin's own orders were accepted against exactly that wallet, and
   * the release gate then said so hourly, in its own vocabulary, on his order page.
   *
   * Null on a build that does not publish it, and `ok: true` with null numbers when the API could
   * not measure the treasury — "we could not look" is not "we cannot pay", and the page must not
   * invent a refusal the API did not make.
   */
  const treasury = treasuryVerdict(fresh);
  const treasuryProblem = treasury && !treasury.ok ? (treasury.problem ?? null) : null;
  /** the largest amount ONE order may ask for today — the API's arithmetic, never the form's */
  const deliverableNow = treasury?.deliverable_now_groth ?? null;
  /**
   * Everything one row is still missing, in the user's words — the API's included.
   *
   * ⛔ EXCEPT `problem_code: "batch"`. The API marks the row where the running total crosses
   * Available (`price_item`) and then deliberately does NOT refuse on it — `_refuse_items` skips
   * that code and the batch rule answers instead, "because a 422 telling the user their third
   * wallet is wrong when the only thing wrong is how much money is in the account" is the wrong
   * thing to fix. The page follows the same rule: the sentence is shown ONCE, under the totals,
   * where it can be acted on. (The mock used to hide this by not marking the row at all; it now
   * marks it the way the API does, which is what made the divergence visible.)
   */
  const rowFault = (q: PricedWithdrawalItem | undefined) =>
    q && q.problem_code !== 'batch' && q.problem_code !== 'treasury_float' ? q.problem : undefined;
  const rowProblem = (i: (typeof items)[number], n: number) => {
    const label = i.address ? shortAddr(i.address) : `row ${n + 1}`;
    return [i.addressProblem, i.amountProblem, i.timeProblem, rowFault(priced.get(i.row.id))].filter(Boolean).map((x) => `${label}: ${x}`);
  };
  const rowProblems = items.flatMap((i, n) => (i.blank ? [] : rowProblem(i, n)));
  const batchProblems = [
    // the API could not price the list: its sentence, and nothing is scheduled on a guess
    ...(previewError ? [previewError] : []),
    // …and the treasury's own verdict, which is a refusal `POST /v1/withdrawals` WILL make: the
    // sentence is shown once, under the totals, exactly as the batch shortfall is (T52)
    ...(treasuryProblem ? [treasuryProblem] : []),
    // the operator's switch, in the user's words — the flag it reads is not their business
    ...(account && !account.modes.direct ? ['payouts are paused right now'] : []),
    // a pasted list with a bad line is not a list yet: nothing is scheduled while one is on screen
    ...(pasteOpen && paste && paste.errors > 0
      ? [`the pasted list has ${paste.errors} line${paste.errors === 1 ? '' : 's'} that cannot be read — fix or clear it`]
      : []),
  ];
  /**
   * What the user is TOLD, which is not the same list as what BLOCKS: a row nobody has been in yet
   * still holds the button down (it would be an order with no address), and says nothing about it.
   * The gate and the words come from the same computation — only the audience differs.
   */
  const problems = [...new Set([...items.flatMap((i, n) => (i.speak ? rowProblem(i, n) : [])), ...batchProblems])];
  /** A quote for THIS list, in which every item is allowed. Nothing else opens the button. */
  const quoted = !!fresh && fresh.items.length === previewItems.length && fresh.items.every((it) => it.ok);
  /**
   * ⛔ `overBudget` HOLDS THE BUTTON DOWN. It used to only paint "Remaining" red, so a batch the
   * API had already said was over Available could be submitted, and the user's evidence that it
   * was refused was a banner after the fact (F4, 2026-09-10). A guard that only changes a colour
   * is not a guard.
   */
  const valid = rowProblems.length === 0 && batchProblems.length === 0 && !!account && active.length > 0 && quoted && !overBudget;
  /** A list with no quote yet is not a refusal — it is a question the API has not answered. */
  const checking = previewItems.length > 0 && !fresh && !previewError;
  /**
   * What a money line says. While a quote is in flight the answer is not zero, it is not known yet
   * — and every keystroke puts it back in flight, so "0.00" would sit under the user's hands for as
   * long as they type. An em dash says the true thing, next to "Pricing this list…".
   */
  const money = (g: number) => (checking ? '—' : `${fmtGroth(g)} ${ASSET}`);
  /**
   * T57 — the button is always there, and its label is either what pressing it does or WHY it
   * cannot be pressed. The muted hint that used to sit beside it ("Add a wallet and an amount.")
   * said the same thing somewhere nobody was looking, which is how a form grows prose.
   */
  const submitLabel = submitting
    ? 'Scheduling…'
    : active.length === 0
      ? 'Add a wallet and an amount'
      : overBudget
        ? 'Not enough balance'
        : treasuryProblem
          ? 'More than we can send today'
          : rowProblems.length > 0 || batchProblems.length > 0
            ? 'Fix the rows above'
            : fresh && deliveredGrothTotal !== null
              ? `Schedule ${fmtGroth(deliveredGrothTotal)} ${ASSET} to ${active.length} wallet${active.length === 1 ? '' : 's'}`
              : `Schedule ${active.length} order${active.length === 1 ? '' : 's'}`;

  /**
   * Ask the API what this list costs, ~300 ms after the last change, and again on every change
   * after that. Three rules make it safe to render the answer as money:
   *   · the request body is the answer's identity (`previewKey`), so a number is only ever shown
   *     next to the rows it was quoted for;
   *   · every intended request takes the next number off `previewSeq`, and a response that is not
   *     the current number is dropped — a slow first quote must not overwrite a fast second one;
   *   · a failure clears the quote and keeps the API's sentence. An unreadable fee is not a free
   *     one and not a stale one (law 4: a stale fee refuses, it never reuses).
   */
  useEffect(() => {
    if (!signedIn) return;
    const body = JSON.parse(previewKey) as WithdrawalItem[];
    if (body.length === 0) {
      previewSeq.current++; // whatever is in flight answers a list that is gone
      setPreview(null);
      setPreviewError(null);
      setPreviewing(false);
      return;
    }
    const seq = ++previewSeq.current;
    setPreviewing(true);
    const t = setTimeout(() => {
      api.previewWithdrawal({ asset: ASSET, items: body }).then(
        (data) => {
          if (seq !== previewSeq.current) return; // a later list is on screen
          setPreview({ key: previewKey, data });
          setPreviewError(null);
          setPreviewing(false);
        },
        (e) => {
          if (seq !== previewSeq.current) return;
          setPreview(null);
          setPreviewError(e instanceof ApiError ? e.detail : errorText(e));
          setPreviewing(false);
        },
      );
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [signedIn, previewKey]);

  const setRow = (id: number, patch: Partial<Row>) => setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch, touched: true } : r)));
  /** Leaving a field counts as having been in the row, even when nothing was typed in it. */
  const touchRow = (id: number) => setRows((rs) => rs.map((r) => (r.id === id ? { ...r, touched: true } : r)));
  const addRow = () => setRows((rs) => [...rs, blankRow()]);
  const removeRow = (id: number) => setRows((rs) => (rs.length === 1 ? [blankRow()] : rs.filter((r) => r.id !== id)));

  /** Pasted lines become rows — appended, unless the user asked for them to replace what is there. */
  const applyPaste = () => {
    if (!paste || paste.errors > 0 || paste.parsed === 0) return;
    const made: Row[] = paste.entries.map((e) => ({
      id: nextRowId++,
      address: e.address ?? '',
      amount: e.groth !== null ? grothDecimal(e.groth) : '',
      preset: e.preset,
      custom: e.custom,
      touched: true, // pasted IS typed: a line the user brought may say what is wrong with it
    }));
    setRows((rs) => (pasteReplace ? made : [...rs.filter((r) => r.address.trim() || r.amount.trim()), ...made]));
    setPasteText('');
    setPasteOpen(false);
    setPasteReplace(false);
  };

  /**
   * The list, in the one shape this page also reads back: `address,amount,deliver_at`. It goes to
   * the clipboard AND into a box on the page, because a browser that refuses `navigator.clipboard`
   * (an iframe, a locked-down phone) would otherwise "copy" nothing and say so nowhere.
   */
  const copyList = async () => {
    const text = toCanonicalList(exportable);
    setExported(text);
    setCopied(false);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // no clipboard permission: the box below is the copy
    }
  };

  const presetLabel = (id: PresetId) => DELIVERY_PRESETS.find((p) => p.id === id)?.label ?? id;
  const pasteWhen = (e: PasteEntry) => (e.preset === 'custom' ? e.custom.replace('T', ' ') : presetLabel(e.preset));

  const submit = async () => {
    if (!valid) return;
    setSubmitting(true);
    setError(null);
    setRefusal(null);
    setResult(null);
    // `submitItems` is `active`, not `items`: a blank row is not an order and must not travel as
    // one. The body is captured here so the refusal that may come back is keyed to the exact list
    // that was sent, not to whatever is on screen when it arrives.
    const sent = submitItems;
    const key = listKey;
    try {
      const r = await api.withdraw({ asset: ASSET, items: sent, mode: 'direct' });
      setResult(r);
      setRows([blankRow()]);
      void session.refreshAccount();
    } catch (e) {
      // The API's own sentence, always: a 409 is its shortfall for the batch, a 422 names the
      // items it refused. Both were decided on a fresh pricing of this list — the balance and the
      // gas price both move between the quote on screen and the click — and its numbers beat
      // anything computed here. The per-item halves go on the rows they are about (never into the
      // banner, which is read by whoever is looking over the user's shoulder too).
      setError(e instanceof ApiError ? e.detail : errorText(e));
      if (e instanceof ApiError && (e.items || typeof e.minAmountGroth === 'number'))
        setRefusal({ key, items: e.items ?? [], min_amount_groth: e.minAmountGroth });
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async (id: string) => {
    if (!window.confirm('Cancel this order? The amount and both fees go back to Available.')) return;
    setCancelling(id);
    setCancelError(null);
    try {
      await api.cancelWithdrawal(id);
      await session.refreshAccount();
    } catch (e) {
      setCancelError(errorText(e));
    } finally {
      setCancelling(null);
    }
  };

  const newIds = useMemo(() => new Set(result?.items?.map((i) => i.request_id) ?? result?.request_ids ?? []), [result]);
  const orders = account?.requests ?? [];
  /**
   * Which orders the LEDGER has already given back — a `cancel` entry for the id. It is what turns
   * a legacy `failed` row into "Returned to balance" instead of a red word about money that is
   * already home (T40, reworded T48). The evidence is the entry, never the status.
   */
  const refunded = useMemo(() => refundedIds(account?.history), [account?.history]);

  /**
   * The phone bar repeats the ONE number that decides whether the button can be pressed, for while
   * the Totals card is off screen — the breakdown stays in the card, where there is room for it.
   * Screen review 2026-09-10: the bar was also drawn ON TOP of that card
   * — a fixed bar sitting over "Total debited" at 390×844 — because a sticky element hovers over
   * whatever follows it until the scroll reaches its own place in the flow. So it is drawn only
   * while the card it repeats cannot be seen, which is the only time it is worth anything.
   */
  const totalsRef = useRef<HTMLDetailsElement | null>(null);
  const [totalsSeen, setTotalsSeen] = useState(false);
  const hasAccount = !!account;
  useEffect(() => {
    const el = totalsRef.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver((entries) => entries.forEach((e) => setTotalsSeen(e.isIntersecting)), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, [hasAccount]);

  return (
    <>
      {/* The context card (T57): what this panel spends, above it and on the same axis. The
          Deposit half shows the wallet's holdings here; this half shows the balance they became.
          Every number is the API's — `available` is its `available_groth` once a preview has
          answered, and "Deliverable now" is its treasury verdict or a dash, never a zero we made
          up (T52: "we could not measure the treasury" is not "we cannot pay you"). */}
      <section className="card pgas-balance-card" data-testid="pgas-balance">
        <div className="card-head">
          <h2>Your Pgas balance</h2>
          <span className="tiny muted">on Beam — deposits arrive here</span>
        </div>
        {account ? (
          <div className="pgas-balance">
            <div>
              <div className="b-label">Available</div>
              <div className="b-value" data-testid="bal-available">{`${fmtGroth(available)} ${ASSET}`}</div>
            </div>
            <div>
              <div className="b-label">Scheduled</div>
              <div className="b-value" data-testid="bal-scheduled">{`${fmtGroth(account.balances[ASSET]?.scheduled ?? 0)} ${ASSET}`}</div>
            </div>
            <div>
              <div className="b-label">Sent</div>
              <div className="b-value" data-testid="bal-sent">{`${fmtGroth(account.balances[ASSET]?.sent ?? 0)} ${ASSET}`}</div>
            </div>
            <div>
              <div className="b-label">Deliverable now</div>
              <div className="b-value" data-testid="bal-deliverable">
                {deliverableNow === null ? '—' : `${fmtGroth(deliverableNow)} ${ASSET}`}
              </div>
            </div>
          </div>
        ) : (
          <p className="muted small">Connect your wallet and your balance appears here.</p>
        )}
      </section>

      <SwapPanel testId="schedule-form" className="schedule-form">
        <SwapHead
          head={modes}
          sub={
            account && (
              <span className="tiny muted" data-testid="bridge-head">
                bridge takes {Math.round(etaS / 60)} min · sent that far ahead of your time
                {bridgeNow !== null ? ` · a crossing costs about ${fmtGroth(bridgeNow)} ${ASSET} right now` : ''}
              </span>
            )
          }
        />
        {!account ? (
          <>
            {/* Its children are empty on purpose: the BUTTON below carries the reason (T57). */}
            <SignInGate what="payouts" verb="schedule">
              {null}
            </SignInGate>
            <SwapAction>
              <button type="button" className="btn btn-lg" disabled data-testid="money-cta">
                {wallet.address ? 'Loading your balance…' : 'Connect your wallet'}
              </button>
            </SwapAction>
          </>
        ) : (
          <>
                {feesError && (
                  <div className="banner banner-warn" data-testid="fees-error">
                    Fees could not be read ({feesError}) — every number below is still the API's own, from the preview.
                  </div>
                )}
                {/* T53 — the same panel as the Deposit page: what leaves your balance on top, the
                    wallets it goes to underneath, the itemised cost in the strip below them.
                    T45 item 5 (admin 2026-09-10 15:35Z, "show what actually available to user"):
                    Available is AT THE TOP, beside the number being spent — not only in the
                    totals the user scrolls to afterwards. Same number, same source (`available`,
                    the API's `available_groth` once a preview has answered), said twice on
                    purpose because it is what every amount below is decided against. */}
                <div className="sw-legs">
                  <SwapLeg
                    label="From your balance"
                    aside={
                      <span className="num" data-testid="form-available">
                        {`Available ${fmtGroth(available)} ${ASSET}`}
                        {/* T52 — and what the TREASURY can hand over today, beside it. The two
                            are different facts: the first is the user's money, the second is
                            whether we can move it now (the rest is inside a max-privacy lock
                            with an end date, which the refusal names). Shown only when the API
                            published a number — a dash here would read as a fault, and a zero
                            we made up would read as a refusal nobody made. */}
                        {deliverableNow !== null && (
                          <span className="tiny muted" data-testid="form-deliverable">
                            {` · Deliverable now ${fmtGroth(deliverableNow)} ${ASSET}`}
                          </span>
                        )}
                      </span>
                    }
                    control={<span className="sw-asset">{ASSET}</span>}
                    sub="debited when you press Schedule — itemised below"
                  >
                    {money(debitGroth)}
                  </SwapLeg>
                </div>
                {/* the seam sits between the two blocks rather than inside the legs: on this page
                    the second "leg" is the LIST of wallets, and the mark belongs in the gap */}
                <SwapSeam />

                <div className="sw-rows">
                  <div className="sw-rows-head">
                    <span className="sw-leg-label">To these wallets</span>
                    <div className="row paste-actions">
                      <button
                        type="button"
                        className="btn btn-sm"
                        aria-expanded={pasteOpen}
                        data-testid="paste-toggle"
                        onClick={() => setPasteOpen((o) => !o)}
                      >
                        {pasteOpen ? 'Close the paste box' : 'Paste a list'}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm"
                        data-testid="copy-list"
                        disabled={exportable.length === 0}
                        onClick={() => void copyList()}
                      >
                        Copy list
                      </button>
                    </div>
                  </div>

                  {pasteOpen && (
                    <div className="stack-sm paste-panel" data-testid="paste-panel">
                      <textarea
                        className="textarea mono"
                        rows={6}
                        spellCheck={false}
                        autoComplete="off"
                        aria-label="Paste a list"
                        data-testid="paste-input"
                        value={pasteText}
                        onChange={(e) => setPasteText(e.target.value)}
                        placeholder={'0xAbC…,0.05,asap\n0xDeF…\t0.1\t2026-09-10 03:00\n0x123…:0.02;0x456…:0.02'}
                      />
                      <span className="help tiny">
                        One order per line, or separated by “;”. Address, amount, and — if you want one — a delivery time (asap, 2h,
                        tonight, tomorrow, or 2026-09-10 03:00). Space, tab, comma, colon or “=” between them; a spreadsheet’s header row is
                        skipped.
                      </span>
                      {paste && (
                        <>
                          <div className="row paste-head">
                            <span className="tiny strong" data-testid="paste-summary">
                              {paste.summary}
                            </span>
                            <label className="tiny row paste-replace">
                              <input
                                type="checkbox"
                                data-testid="paste-replace"
                                checked={pasteReplace}
                                onChange={(e) => setPasteReplace(e.target.checked)}
                              />
                              Replace rows
                            </label>
                          </div>
                          <ol className="paste-lines" data-testid="paste-lines">
                            {paste.entries.map((e) => (
                              <li key={e.n} data-testid={`paste-line-${e.n - 1}`} data-status={e.mark}>
                                <span className={`paste-mark paste-${e.mark}`} aria-hidden="true">
                                  {e.mark === 'ok' ? '✓' : e.mark === 'warn' ? '⚠' : '✗'}
                                </span>
                                <span className="mono tiny paste-addr">{e.address ?? e.raw}</span>
                                <span className="tiny num">{e.groth !== null ? `${grothDecimal(e.groth)} ${ASSET}` : '—'}</span>
                                <span className={`tiny ${e.error ? 'error-text' : 'muted'}`} data-testid={`paste-note-${e.n - 1}`}>
                                  {e.error ?? e.warning ?? pasteWhen(e)}
                                </span>
                              </li>
                            ))}
                          </ol>
                          <div className="row">
                            <button
                              type="button"
                              className="btn btn-primary btn-sm"
                              data-testid="paste-apply"
                              disabled={paste.errors > 0 || paste.parsed === 0}
                              onClick={applyPaste}
                            >
                              {pasteReplace ? `Replace the rows with ${paste.ok}` : `Add ${paste.ok} to the list`}
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  )}

                  {exported !== null && (
                    <div className="stack-sm" data-testid="copy-list-panel">
                      <span className="help tiny">
                        {copied
                          ? 'Copied. It is also here, if you would rather select it yourself.'
                          : 'Your browser did not let the page use the clipboard — select it here.'}
                      </span>
                      <textarea
                        className="textarea mono"
                        rows={4}
                        readOnly
                        aria-label="Your list"
                        data-testid="copy-list-output"
                        value={exported}
                      />
                    </div>
                  )}

                  <div className="list" data-testid="schedule-rows">
                    {items.map((it, n) => {
                      const r = it.row;
                      return (
                        <div className="sched-row" key={r.id} data-testid="schedule-row" data-row={n}>
                          <div className="field sched-addr">
                            <input
                              className="input mono"
                              placeholder="0x… wallet to fund"
                              spellCheck={false}
                              autoComplete="off"
                              value={r.address}
                              aria-label={`Address ${n + 1}`}
                              aria-invalid={!!r.address && !!it.addressProblem}
                              onBlur={() => touchRow(r.id)}
                              onChange={(e) => setRow(r.id, { address: e.target.value })}
                            />
                            {r.address !== '' && it.addressProblem && it.speak && (
                              <span className="error-text tiny" data-testid={`address-error-${n}`}>
                                {it.addressProblem}
                              </span>
                            )}
                            {it.address && it.address !== r.address.trim() && (
                              <span className="help tiny mono" data-testid={`address-checksummed-${n}`}>
                                {it.address}
                              </span>
                            )}
                          </div>

                          <div className="field sched-amt">
                            <div className="input-wrap">
                              <input
                                className="input num"
                                inputMode="decimal"
                                placeholder="0.00"
                                value={r.amount}
                                aria-label={`Amount ${n + 1}`}
                                aria-invalid={!!r.amount && !!it.amountProblem}
                                onBlur={() => touchRow(r.id)}
                                onChange={(e) => setRow(r.id, { amount: e.target.value })}
                              />
                              <span className="suffix tiny muted">{ASSET}</span>
                            </div>
                            {r.amount !== '' && it.amountProblem && it.speak ? (
                              <span className="error-text tiny" data-testid={`amount-error-${n}`}>
                                {it.amountProblem}
                              </span>
                            ) : (
                              <span className="help tiny" data-testid={`amount-hint-${n}`}>
                                {hasMinimum ? `min ${minLabel}` : 'any amount'}
                              </span>
                            )}
                            {/* T45 item 5 — "Use max": the largest amount THIS row can ask for and
                              still have its fees charged on top, out of what the rows above it
                              left of Available. ⛔ The number is the API's (`max_on_top_groth`,
                              off the same lattice `price_item` solves a from-amount row on); this
                              page must never compute `available − 2 % − bridge`, which would be a
                              second implementation of the fee model on the user's own money. No
                              field (an older API build) and no button — never a fallback sum. */}
                            {(() => {
                              const max = priced.get(r.id)?.max_on_top_groth;
                              if (typeof max !== 'number' || max <= 0) return null;
                              return (
                                <button
                                  type="button"
                                  className="btn btn-sm btn-ghost tiny"
                                  data-testid={`use-max-${n}`}
                                  title={`fill the largest amount your balance covers with the fees on top (${grothDecimal(max)} ${ASSET})`}
                                  // ⛔ `grothDecimal`, NOT `fmtGroth`. `fmtGroth` is a DISPLAY
                                  // formatter capped at 6 decimals: filling the box with it turned
                                  // 0.00960784 into 0.009608 — a different amount, typed into the
                                  // user's own order by us. This is the exact groth figure.
                                  onClick={() => setRow(r.id, { amount: grothDecimal(max) })}
                                >
                                  Use max
                                </button>
                              );
                            })()}
                          </div>

                          <div className="field sched-when">
                            <select
                              className="select"
                              value={r.preset}
                              aria-label={`Deliver ${n + 1}`}
                              onChange={(e) => {
                                const preset = e.target.value as PresetId;
                                setRow(r.id, {
                                  preset,
                                  custom:
                                    preset === 'custom' && !r.custom
                                      ? toLocalInput(presetDeliverAt('2h', new Date(nowS * 1000)))
                                      : r.custom,
                                });
                              }}
                            >
                              {DELIVERY_PRESETS.map((p) => (
                                <option key={p.id} value={p.id}>
                                  {p.label}
                                </option>
                              ))}
                            </select>
                            {r.preset === 'custom' && (
                              <input
                                className="input"
                                type="datetime-local"
                                value={r.custom}
                                aria-label={`Custom time ${n + 1}`}
                                aria-invalid={!!it.timeProblem}
                                onChange={(e) => setRow(r.id, { custom: e.target.value })}
                              />
                            )}
                          </div>

                          <div className="sched-rm">
                            <button
                              type="button"
                              className="btn btn-sm btn-ghost"
                              aria-label={`Remove row ${n + 1}`}
                              onClick={() => removeRow(r.id)}
                            >
                              ✕
                            </button>
                          </div>

                          <div className="sched-foot tiny muted" data-testid={`deliver-hint-${n}`}>
                            {it.deliverAt === null ? 'pick a date and time' : deliveryHint(it.deliverAt, nowS, etaS)}
                          </div>
                          {/* what the API says this one row costs — never a sum made here — and what
                            it said was wrong with it: the refusal it just answered the button with
                            if there is one, otherwise the quote's. Both are the API's own words on
                            the row they are about, which is where a per-item problem can be acted
                            on; the banner gets the sentence about the batch. */}
                          {(() => {
                            const q = priced.get(r.id);
                            const problem = refusedByRow.get(r.id) ?? (it.speak ? rowFault(q) : undefined);
                            if (!q && !problem) return null;
                            /**
                             * T35: the row says the TWO numbers a person cares about — what lands in
                             * that wallet and what leaves the balance — and they are both the API's.
                             * `delivered_groth` is a dash on a build that does not publish it: the
                             * amount typed in the box is what the user ASKED for, not evidence of
                             * what will be delivered, and this page does not turn one into the other.
                             * `debited_groth` and `total_groth` are the same fact under two names
                             * (the older builds' name is the fallback, read in one place).
                             */
                            const delivered = deliveredGroth(q);
                            const debited = debitedGroth(q) ?? q?.total_groth ?? null;
                            const fromAmount = feeMode(q) === 'from_amount';
                            return (
                              <div className="sched-foot tiny sched-price" data-testid={`row-total-${n}`}>
                                {q && (
                                  <span data-testid={`row-receives-${n}`}>
                                    Wallet receives{' '}
                                    <span className="strong">{delivered === null ? '—' : `${fmtGroth(delivered)} ${ASSET}`}</span> · debited{' '}
                                    <span className="strong">{debited === null ? '—' : `${fmtGroth(debited)} ${ASSET}`}</span>
                                  </span>
                                )}
                                {q && (
                                  <span className="muted">
                                    fee {fmtGroth(q.fee_groth)} · bridge {fmtGroth(q.bridge_fee_groth)} · total{' '}
                                    <span className="strong">
                                      {fmtGroth(q.total_groth)} {ASSET}
                                    </span>
                                  </span>
                                )}
                                {/* the one row that must never be silent: it delivers less than the
                                  number in its own amount box, and says why (admin 2026-09-10).
                                  The API writes that sentence too (`fee_note`) — when it sends
                                  one it IS the sentence, and the constant is only the fallback
                                  for a build that does not. */}
                                {fromAmount && (
                                  <span className="muted" data-testid={`row-fee-mode-${n}`}>
                                    {q?.fee_note ?? FROM_AMOUNT_NOTE}
                                  </span>
                                )}
                                {problem && (
                                  <span className="error-text" data-testid={`row-problem-${n}`}>
                                    {problem}
                                  </span>
                                )}
                              </div>
                            );
                          })()}
                        </div>
                      );
                    })}
                  </div>
                  <div className="row">
                    <button type="button" className="btn" onClick={addRow} data-testid="schedule-add">
                      + Add another address
                    </button>
                  </div>
                </div>

                {/* T57 — the cost strip is part of THIS panel now, not a second card under it.
                    What a list costs is the same object as the list; two cards asked the user to
                    read one, then the other, and hold the relationship between them in their head.
                    Every number in it still came off `POST /v1/withdrawals/preview`, and the
                    `schedule-totals` name travels with the region it always described. */}
                <SwapDetails
                  summary="What this list costs"
                  testId="schedule-totals"
                  busy={previewing ? 'yes' : 'no'}
                  detailsRef={totalsRef}
                >
                  <SwapRow label="Orders" testId="total-orders">
                    {active.length}
                  </SwapRow>
                  <SwapRow label="Amounts asked for" testId="total-amount" valueClass="num">
                    {money(amountGroth)}
                  </SwapRow>
                  {/* T35: what the WALLETS receive, when the API publishes the total. It is left
                      out — rather than shown as a dash beside a full set of numbers — on a build
                      that does not, because such a build charges every fee on top and the line
                      above is already that number. A dash there would read as a defect; a number
                      copied from `amount_groth` would be this page asserting a delivery. */}
                  {deliveredGrothTotal !== null && (
                    <SwapRow label="Wallets receive" testId="total-delivered" valueClass="num strong">
                      {money(deliveredGrothTotal)}
                    </SwapRow>
                  )}
                  <SwapRow label={`Fee (${feeBps / 100}%)`} testId="total-fee" valueClass="num">
                    {money(feeGroth)}
                  </SwapRow>
                  {/* T45 — the tooltip used to carry the headroom curve ("1× now → 3× 30 days
                      ahead"), which was the number the user was CHARGED for the wait. The
                      wait is not charged for any more (the release gate carries it) and the
                      unspent part is refunded, so the tooltip says what the line is. */}
                  <SwapRow
                    label="Bridge fee"
                    title="quoted from the live gas price; the unused part is refunded when the crossing settles"
                    testId="total-bridge-fee"
                    valueClass="num"
                  >
                    {money(bridgeGroth)}
                  </SwapRow>
                  <SwapRow label="Total debited" testId="total-debited" valueClass="num strong">
                    {money(debitGroth)}
                  </SwapRow>
                  <SwapRow label="Available" testId="total-available" valueClass="num">
                    {`${fmtGroth(available)} ${ASSET}`}
                  </SwapRow>
                  <SwapRow label="Remaining" testId="total-remaining" valueClass={`num${overBudget ? ' error-text' : ''}`}>
                    {money(available - debitGroth)}
                  </SwapRow>
                </SwapDetails>
                {/* The API's verdict on the list as a list, under the numbers it is about. It is
                    not a row's problem and is not rendered as one — and it holds the button down
                    (see `valid`), which is the whole difference between a guard and a colour. */}
                {batchProblem && (
                  <p className="small error-text" style={{ margin: 0 }} data-testid="batch-problem">
                    {batchProblem}
                  </p>
                )}
                {/* T52 — the treasury's verdict, in the API's own words. It is not a row's
                    problem (the row is fine; we are short) and it is not the batch's either —
                    topping up does not make a locked treasury liquid — so it gets its own line
                    and its own way out: ask for less now, or come back when the lock ends. */}
                {treasuryProblem && (
                  <p className="small error-text" style={{ margin: 0 }} data-testid="treasury-problem">
                    {treasuryProblem}
                  </p>
                )}
                {/* T35: a list whose fees do not fit ON TOP is not a list that is refused — the
                    rows that cannot pay them on top pay them out of their own amount and the batch
                    goes through. Said once, under the totals, where "Wallets receive" is smaller
                    than "Amounts asked for" and a person would otherwise wonder why. */}
                {anyFromAmount && (
                  <p className="small muted" style={{ margin: 0 }} data-testid="totals-fee-mode">
                    Some orders take their fees out of the amount — there is not enough in Available to pay them on top, so those wallets
                    receive a little less. Nothing is blocked.
                  </p>
                )}
                {/* T45 — "incl. headroom for the wait" was true and was not the whole truth: the
                    headroom was KEPT (two live orders funded 0.00014733 ETH against a crossing
                    that cost 0.00012778, and the difference stayed with the treasury). It is
                    refunded at settlement now, so the honest sentence is that this line is an
                    estimate and the remainder comes back. The headroom is no longer a number the
                    user has to reason about, so it is out of the sentence entirely. */}
                <SwapNote testId="bridge-fee-note">
                  Bridge fee: an estimate — whatever the crossing does not use comes back to your balance.
                </SwapNote>
                {checking && <SwapNote testId="preview-status">Pricing this list…</SwapNote>}
                {problems.length > 0 && (
                  <ul className="small error-text" style={{ margin: 0, paddingLeft: 18 }} data-testid="schedule-problems">
                    {problems.map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                )}
                <SwapAction>
                  <button
                    type="button"
                    className="btn btn-primary btn-lg"
                    disabled={!valid || submitting}
                    onClick={submit}
                    data-testid="schedule-submit"
                  >
                    {/* T53 — the button says what pressing it does, in the units the user typed:
                        the amount the WALLETS receive (the API's `delivered_groth` total, never a
                        sum made here) and how many of them. A list the API has not priced yet
                        falls back to the count — a button that named an amount while the strip
                        above it says "—" would be stating a number nothing has answered. T57 gave
                        it the other half: when it cannot act, the label is the reason. */}
                    {submitLabel}
                  </button>
                  {/* ⛔ "you receive exactly what you enter" is TRUE only while every row can pay
                    its fees on top. The moment one cannot, the same sentence is an overclaim about
                    the user's own money — so the line states which of the two rules this list is
                    under, and the rows say which of them are affected. */}
                  <SwapNote testId="fee-line">
                    {anyFromAmount
                      ? `Fee ${feeBps / 100} % · bridge fee: an estimate, whatever the crossing does not use comes back to your balance · fees come out of the amount on the orders Available cannot cover on top`
                      : `Fee ${feeBps / 100} % · bridge fee: an estimate, whatever the crossing does not use comes back to your balance · you receive exactly what you enter`}
                  </SwapNote>
                  {error && (
                    <div className="banner banner-error" data-testid="schedule-error">
                      {error}
                    </div>
                  )}
                  {result && (
                    <div className="banner banner-ok stack-sm" data-testid="schedule-result" style={{ alignItems: 'flex-start' }}>
                      <span className="strong">
                        Scheduled {result.items?.length ?? result.request_ids.length} order
                        {(result.items?.length ?? result.request_ids.length) === 1 ? '' : 's'}.
                      </span>
                      <span className="num" data-testid="result-debited">
                        Debited {fmtGroth(result.total_debited_groth)} {ASSET} (fee {fmtGroth(result.fee_groth)}
                        {typeof result.bridge_fee_groth === 'number' ? ` · bridge fee ${fmtGroth(result.bridge_fee_groth)}` : ''}).
                      </span>
                      <span className="small">They are listed below with their status.</span>
                    </div>
                  )}
                </SwapAction>
          </>
        )}
      </SwapPanel>

            {/* Phones only (CSS): the one number that decides whether the button can be pressed at
                all, kept above the tab bar while the list of rows scrolls. The API's own total,
                rendered twice — never a second sum. */}
            <div
              className={`sticky-totals${totalsSeen ? ' sticky-totals-off' : ''}`}
              data-testid="sticky-totals"
              data-shown={totalsSeen ? 'no' : 'yes'}
              aria-hidden="true"
            >
              <span>Total debited</span>
              <span className={`num strong${overBudget ? ' error-text' : ''}`}>{money(debitGroth)}</span>
            </div>

            {account && (
            <section className="card">
              <div className="card-head">
                <h2>Scheduled orders</h2>
                <span className="tiny muted">{orders.length} shown · cancel while it is still ours to stop</span>
              </div>
              {cancelError && <div className="banner banner-error">{cancelError}</div>}
              {orders.length === 0 ? (
                <div className="empty">Nothing scheduled yet.</div>
              ) : (
                <div className="table-wrap">
                  {/* the same money table as the Balance page's Payouts (T53): fixed columns that
                      fit the card, sentences that wrap, one card per order on a phone */}
                  <table className="data orders" data-testid="schedule-orders">
                    <thead>
                      <tr>
                        <th>Status</th>
                        <th>Wallet</th>
                        <th className="num">Wallet receives</th>
                        <th className="num">Fee</th>
                        <th>Deliver at</th>
                        <th>To the bridge at</th>
                        <th>Arrives</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orders.map((row) => {
                        // the order as the T40 vocabulary reads it: never "Failed", and cancellable
                        // for exactly as long as the money is still ours to give back
                        const o = row as PayoutRow;
                        const view = payoutView(o, refunded.has(o._id));
                        const delivered = deliveredGroth(o);
                        const fromAmount = feeMode(o) === 'from_amount';
                        return (
                          <tr key={o._id} data-new={newIds.has(o._id) ? '' : undefined} className={newIds.has(o._id) ? 'row-new' : ''}>
                            {/* the action lives WITH the status that permits it: an eighth column
                                pushed the button past the right edge of the card at 1280 (screen
                                review 2026-09-10), and a Cancel you have to scroll sideways to
                                find is a Cancel nobody presses. */}
                            <td data-label="Status">
                              <PayoutStatusCell row={o} refunded={refunded.has(o._id)} />
                              {view.cancellable && (
                                <div style={{ marginTop: 6 }}>
                                  <button
                                    type="button"
                                    className="btn btn-sm"
                                    disabled={cancelling === o._id}
                                    onClick={() => cancel(o._id)}
                                    data-testid={`cancel-${o._id}`}
                                  >
                                    {cancelling === o._id ? 'Cancelling…' : 'Cancel'}
                                  </button>
                                </div>
                              )}
                            </td>
                            <td className="mono" data-label="Wallet" title={o.W}>
                              {shortAddr(o.W, 6, 4)}
                            </td>
                            <td className="num" data-label="Wallet receives">
                              {delivered === null ? fmtGroth(o.amount_groth) : fmtGroth(delivered)} {o.asset}
                              {fromAmount && (
                                <div className="tiny muted status-sub" data-testid={`order-fee-mode-${o._id}`}>
                                  {/* ⛔ `requested_groth`, NOT `amount_groth` — see the same cell on
                                      the Balance page: the stored row's `amount_groth` is what the
                                      release SENDS, so "of X asked" was printing the delivery. */}
                                  of {fmtGroth(requestedGroth(o) ?? o.amount_groth)} {o.asset} asked · fees taken from the amount
                                </div>
                              )}
                            </td>
                            <td className="num" data-label="Fee">
                              {fmtGroth(o.fee_groth)}
                              {typeof o.bridge_fee_groth === 'number' && (
                                <div className="tiny muted" data-testid={`order-bridge-${o._id}`}>
                                  + {fmtGroth(o.bridge_fee_groth)} {o.mode === 'instant' ? 'gas' : 'bridge'}
                                </div>
                              )}
                            </td>
                            <td className="nowrap" data-label="Deliver at">
                              {shortTime(o.deliver_at)}
                            </td>
                            <td className="nowrap" data-label="To the bridge at">
                              {shortTime(o.release_at)}
                            </td>
                            <td data-label="Arrives" data-testid={`order-eta-${o._id}`}>
                              <PayoutEta row={o} />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="tiny muted" style={{ marginTop: 8 }}>
                An order goes to the bridge {Math.round(etaS / 60)} minutes before your delivery time, and never earlier than now — so a
                time sooner than that arrives as soon as the bridge can manage it, not before.
              </p>
            </section>
            )}
    </>
  );
}
