"""POST /v1/withdrawals — schedule a LIST of orders: (address, amount, delivery time).
POST …/preview · POST …/{id}/cancel · GET /v1/withdrawals/fees.

Restructured 2026-09-09 ("Scheduling"). The user types the wallets they want funded, how much
each should receive and WHEN it should be there; nothing is signed, connected or registered
first — an address is admitted on its own arithmetic (a valid EIP-55 checksum) and on one fact
read from the chain (it carries no contract code).

Every rule of the contract, in order, failing closed:
  a valid address that is not a contract · a delivery time that IS a time · an asset this
  deployment can actually pay · amount ≥ min_amount_groth (a technical floor) on the asset's
  grid · instant → denomination multiples · Available ≥ Σ total_groth · a disabled mode → 409.
One `payout_requests` row per item with its own `release_at`, and the ledger debit for amount +
our fee + the bridge fee. Execution is a worker concern (payouts.py) and is dark here.

THE INVARIANTS THIS FILE OWNS:

  * "Available ≥ need" read and then acted on is not a check — two requests arriving together
    both read the same Available and both pass it. The admission is a per-(account, asset)
    reservation: `pending` is claimed FIRST and the ledger is read AFTER the claim, so a
    balance already spent by a request that finished in between is visible to the arithmetic
    that decides. A claim that does not fit is backed out before a groth is written. Entries
    are written only when that claim held, the ledger is re-read afterwards, and a balance that
    went negative is rolled back with offsetting entries (never by editing history). The WHOLE
    BATCH is one reservation: a batch is admitted or refused as a unit, never half-written —
    and a batch that cannot be written in full REVERSES ITSELF before the error leaves.
  * A refund needs the debit it reverses, and A WRITE THAT RAISED IS NOT A WRITE THAT DID NOT
    LAND. The `schedule` entry is appended BEFORE the request row exists, so a row can never
    exist without its debit; cancel refunds exactly the groth THOSE entries took — `schedule`
    plus its `schedule_bridge_fee` half, through `ledger.debited_groth`, never a recomputed
    amount + fee — and only once; and a compensation ASKS whether the row it is compensating for
    actually landed before it gives the money back — an order whose debit was reversed is an
    order the payout worker pays for free.
  * CANCEL IS REPAIRABLE, NOT TERMINAL. The row is flipped to `cancelled` before the refund is
    appended, so a failure between the two used to freeze the groth in `scheduled` forever with
    no alert and no second chance. A `cancelled` row whose `schedule` entry has no matching
    `cancel` entry RE-ENTERS the refund path (ledger.cancel is idempotent by construction), and
    any failure to write the refund pages the operator.
  * DELIVERY TIME IS NOT RELEASE TIME. The bridge takes ~66 minutes (`PGAS_BRIDGE_ETA_S`), so an
    order the user wants delivered at T is handed to the bridge at `max(now, T − eta)`. A
    `deliver_at` in the past (or absent, i.e. "asap") releases now — it never schedules backwards.
    The window guard is POSITIVE (the value must BE in range): a `deliver_at` of NaN compares
    False to every bound, so a guard phrased as "refuse it when it is out of range" admits it.
  * THE BRIDGE FEE IS CHARGED, NOT ABSORBED — AND IT IS PRICED FOR THE PROMISE, NOT THE MOMENT.
    Until 2026-09-10 the relayer fee came out of our 2%, so the smallest payout that did not lose
    the treasury money was a DERIVED FLOOR of `ceil(relayer_fee × headroom × 10000 / fee_bps)` —
    0.045 ETH at 5 gwei, for a product whose whole point is funding a fresh wallet with a few
    dollars. The bridge fee is now an itemised line the user pays at cost
    (`bridge_fee_groth = relayer_fee_now × headroom(ahead_s)`), so the economic floor is GONE and
    what remains is technical: a positive amount on the asset's grid (`min_amount_groth`).
    §WE-SET-IT-WE-DONT-READ-IT: nobody quotes that relayer fee back to us, so it is MEASURED at
    request time through `payouts.relayer_fee_for` — the same one implementation the release
    itself pays with — never assumed, never stale (cached ≤ 60 s), and a fee we could not read
    REFUSES the request (503). An unreadable gas price is not a zero fee (law 8).
  * THE FEES RIDE ON TOP WHEN THE BALANCE CAN CARRY THEM AND COME OUT OF THE AMOUNT WHEN IT
    CANNOT (2026-09-10, the admin: "If user requested to get 0.01 ETH, we should deposit 0.01
    ETH; only if user doesn't have deposit to pay gas fees and 2% fees to us, we take it from
    sending amount"). Charging them on top ONLY was a product that could not spend its own
    balance: an account holding exactly 0.01 ETH could not withdraw 0.01 ETH, and the refusal
    said "insufficient balance" about money the user was looking at. The decision is per item,
    IN ROW ORDER, against what is left of Available after the rows above it — `remaining ≥
    amount + fee + bridge` delivers the whole amount, `remaining ≥ amount` delivers the largest
    grid value whose OWN fee and crossing fit inside it, and less than the amount is short. In
    both modes `delivered + our fee + the bridge fee == what the ledger debits`, so the
    rounding a grid step leaves behind is revenue rather than a groth nobody accounts for, and
    the release has one number to send (`delivered_groth`, stored as the row's `amount_groth`).
  * AND ONE FUNCTION RULES ON THE BATCH. Σ `total_groth` ≤ Available is the only rule left at
    the batch level, and it had no answer of its own: every item came back `ok: true` and the
    verdict lived in the difference between two other numbers the preview returned, so a client
    had to re-derive the one rule that decides whether the money moves. `batch_verdict` says it
    — `{ok, need_groth, available_groth, shortfall_groth, problem?}` — the preview renders it and
    `create` refuses with the same sentence and the same shortfall (`batch_problem`,
    `X-Shortfall-Groth`). A batch that does not fit is never reported as one ITEM's problem: the
    row where a running total happens to cross is arithmetic, not a mistake the user made.
  * ONE FUNCTION PRICES AN ITEM, AND EVERYTHING ELSE READS IT. `price_item` returns the triple
    (our fee, the bridge fee, the total) plus the verdict; `preview` renders it, `create` refuses
    the batch on it, the row stores it and the ledger debits it. Two implementations of one fact
    will disagree and one of them will reach money (law 9) — so the number the user is shown, the
    number Available is checked against, the number on the row and the number in the ledger are
    all the same call.
  * ONCE THE ROWS ARE LIVE, NOTHING MAY BECOME A 5xx. Past the point where `_write_items` has
    committed the batch, the orders exist, the balance is debited and the worker will pay them.
    A 500 from there tells the caller their withdrawal FAILED *and* withholds the ids — so the
    ordinary retry-on-500 schedules every one of those payouts a second time, which is exactly
    the duplication the batch reverses itself to avoid. What is left after the commit is a belt
    and an observation: the belt may still REVERSE the batch (a 409 naming the ids it reversed
    — a refusal, not a failure), and everything else (an unreadable balance, an event that will
    not write) is logged, paged and swallowed, with the 200 and the ids leaving regardless.
  * AN UNREADABLE CHAIN IS NOT AN EMPTY ANSWER, AND AN IMPLAUSIBLE ANSWER IS NOT A READING.
    `eth_getCode(W)` is read from ONE pinned endpoint at the head that endpoint itself reported;
    a contract there is a 400 (a contract with no payable receive strands the bridge delivery),
    and an endpoint that could not answer is a 503 — never "no code, therefore an EOA". The head
    itself is checked before it is trusted: a node that answers block 0, a node that is still
    syncing, or a node whose head is far below one this process has already seen would make
    `eth_getCode` return "0x" for a real contract and turn the guard into a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from typing import Annotated, Any

from eth_utils import is_checksum_address, is_hex_address, to_checksum_address
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, ethpipe, ledger, payouts, tg, workers
from ..assets import ASSETS, Asset, get_asset
from ..config import bridge_headroom_min, relayer_subsidy, settings
from ..db import db

log = logging.getLogger("pgasme.withdrawals")

router = APIRouter(prefix="/v1/withdrawals", tags=["withdrawals"])

MODES = ("direct", "instant")
# what this deployment's executor can actually pay. `payouts._payout_scheduled` HOLDS every
# non-ETH row ("v1 payouts are ETH only") and `_payout_any_asset` holds every non-direct one, so
# admitting either here debits the user's balance into `scheduled` for an order that can never
# execute. A refusal the executor makes is a refusal the request path should have made.
PAYOUT_ASSETS = ("ETH",)
# a reservation left behind by a request that died mid-flight; far longer than any request lives
RESERVATION_STALE_S = 300.0
# how long before `deliver_at` each mode has to leave: direct = one bridge crossing
# (PGAS_BRIDGE_ETA_S, ~66 min), instant = a transfer that is there in seconds.
INSTANT_ETA_S = 30
# the live relayer fee is one gas read shared by every caller for a minute — a fee that moves
# with the block is still a fee that must not cost one upstream call per rendered form
FEES_CACHE_S = 60.0
# the earliest value that is a delivery TIME at all. A `deliver_at` in the past means "asap" and
# is accepted; 0, a negative number or NaN is a caller bug, and admitting it is how a batch got
# debited and scheduled behind a value that compares False to every bound.
MIN_DELIVER_AT = 0.0
# ⛔ how much more than TODAY's relayer fee a far-dated order must fund. `payouts` holds a
# release whose live relayer fee exceeds `bridge_fee_groth × config.relayer_subsidy()`, and an
# order may wait up to `max_window_s` (30 days) for that gate to run. An order accepted at
# exactly today's floor is therefore held — retried, paged, never delivered — by ANY gas
# increase, with the user's groth stuck in `scheduled` behind a delivery time we already
# promised. So the further ahead an order releases, the more headroom it buys: 1× when it
# releases now, FAR_DATED_MARGIN× a full window out, linear in the wait.
#
# ⚠️ **AND SINCE T45 (2026-09-10) THE SUBSIDY IS 4×, SO THIS LINE IS DIVIDED FLAT** (see
# `headroom_for`). The protection did not shrink — it moved from the CHARGE to the GATE, which
# is the side that does not park a month of the user's money to buy it, and the refund at
# settlement returns whatever the crossing does not spend either way. The constant stays because
# it is still the whole curve at any deployment that lowers the subsidy.
FAR_DATED_MARGIN = 3.0
# WHERE THE FEES COME FROM, per item, decided against the remaining Available in row order.
# `on_top`: the user receives exactly what they asked for and the balance pays the fees beside
# it. `from_amount`: the balance covers the amount but not the fees, so the fees come OUT of the
# amount and the wallet receives less than it asked for. A row written before 2026-09-10 carries
# neither and reads as `on_top` — see `row_fee_mode`.
ON_TOP = "on_top"
FROM_AMOUNT = "from_amount"
# the sentence the UI shows under a from-amount row. Said HERE, once, so the mock the web tests
# render and the answer production returns are the same words (T30c skeptic (a)).
FROM_AMOUNT_NOTE = "fees taken from the amount — not enough balance to pay them on top"
# the row ran out of balance entirely: a verdict about the LIST, not a mistake in the row, so
# `_refuse_items` never turns it into a 422 — the batch rule refuses it with the shortfall.
BATCH_CODE = "batch"
# …and the row whose amount is too small to pay the crossing out of itself: that one IS about
# the row (raise the amount, or fund the fees on top), so it refuses per item.
BRIDGE_FEE_CODE = "bridge_fee"
# …and the row that CAN pay its crossing out of itself and would have almost nothing left. The
# admin's rule is "withdrawal can be any" — that is about the FLOOR, not about handing someone 1
# groth of ETH against a 22,502-groth debit and calling it a withdrawal. THE ASSUMPTION IS
# STATED IN THE REFUSAL (T35b): a withdrawal delivers at least half of what was asked for; below
# that the row is refused with both ways out named, and nothing is written.
FEES_DOMINATE_CODE = "fees_dominate"
# how much of the requested amount must survive the fees when they come out of it: 1/2.
MIN_DELIVERED_NUM, MIN_DELIVERED_DEN = 1, 2
# ⛔ **WHAT THE TREASURY CANNOT DELIVER TODAY IS NOT ACCEPTED TODAY** (T52, admin 2026-09-10
# 15:38Z: *"Why do you accept user request if user cannot spend this?"* → 15:45Z: *"Next time
# when user wants to withdraw just tell him he can't"*). Acceptance read the USER's ledger
# balance and never the treasury's spendable float, so three orders were taken against a wallet
# that could move 0.00775651 ETH and was holding 0.01652864 more inside a max-privacy lock. The
# release gate then said so — correctly, hourly, in the operator's vocabulary, on the user's own
# page. There is no accept-anyway: the row carries this code and the batch is refused with ONE
# sentence naming what CAN be delivered now and when the rest unlocks.
TREASURY_FLOAT_CODE = "treasury_float"
# how many times the shortfall may re-price itself before it is taken as settled — see
# `top_up_needed`. A bound, not an expectation: the fixed point is reached on the first round.
SHORTFALL_ROUNDS = 8
# ⛔ WHAT MAKES A ROW UNCANCELLABLE WHATEVER ITS STATUS SAYS — the field names
# `payouts.cancellable` refuses on, so the atomic claim asks the same question the read asked.
CANCEL_BLOCKERS = ("kernel_at", "beam_txid", "instant_tx")
# every value `payouts.cancellable`'s truthiness test treats as "this field is not set". `None`
# also matches a MISSING field in Mongo, which is how the overwhelming majority of rows carry it.
NOTHING: list[Any] = [None, "", 0, False]
# ⛔ WHY THE RESERVATION REFUSED, WHEN THE BATCH ITSELF FITS. `reserve` claims against Available
# atomically, so it can refuse a batch whose verdict was `ok` — another batch of this same
# account is in flight and holds the cushion. That answered "insufficient balance … shortfall 0"
# with an `X-Shortfall-Groth: 0` header: a cause that is not true (the money IS there), sending
# the user to top up an account that needs nothing, and a number nobody can act on. The cause is
# named here and the header is omitted entirely — a shortfall of zero is not a shortfall.
INFLIGHT_PROBLEM = (
    "another order of yours is being written right now — nothing was scheduled and nothing was "
    "debited; retry in a moment"
)
# ⛔ **THE REASON IS OURS, NOT THEIRS** (T52 item 6, a user hit this live at 16:47Z). It used to
# read *"could not verify the destination on Ethereum right now (https://eth.drpc.org could not
# read the code at that address) — nothing was scheduled; try again in a moment"*: a provider URL
# and an internal read, in a sentence somebody has to act on, about a failure they cannot fix.
# The endpoint names go to the log line beside it, where they are evidence.
UNREADABLE_DEST = (
    "We could not check the destination address right now — nothing was scheduled; try again "
    "in a moment."
)
# a head far below one this process has already seen is not a head: a snap-syncing or
# freshly-restarted provider answers an old block, and eth_getCode at a block a node does not
# have is "0x" — which this guard would read as "this address is a wallet".
HEAD_REGRESSION_TOLERANCE = 64


class Item(BaseModel):
    W: str = Field(min_length=40, max_length=64)
    amount_groth: int = Field(gt=0)
    # unix seconds: when the money should BE in W. Absent (or in the past) means "as soon as
    # possible" — the order is released now and arrives about one bridge ETA later.
    # ⛔ allow_inf_nan=False. Ordinary JSON has no NaN literal, but pydantic coerces the STRING
    # "NaN" / "Infinity" / "-Infinity" to a float, and EVERY comparison against NaN is False —
    # so `deliver_at: "NaN"` walked through a window guard phrased as "refuse it if it is out of
    # range", made `release_at` = now, got the batch reserved, debited, scheduled and paged, and
    # only then made the response a 500 (FastAPI renders with allow_nan=False) — and every
    # retry of that 500 wrote another live order. Refused at the door: 422, before a groth moves.
    deliver_at: Annotated[float, Field(allow_inf_nan=False)] | None = None


class WithdrawIn(BaseModel):
    asset: str = "ETH"
    items: list[Item] = Field(min_length=1)
    mode: str = "direct"


class PreviewIn(BaseModel):
    """What POST /v1/withdrawals would charge for these items. No writes, no promises.

    The same body as a withdrawal minus the intent — `mode` is optional here because the form
    asks for numbers before the user has chosen anything, and it defaults to the mode a
    withdrawal defaults to so the two never quote different arithmetic."""

    asset: str = "ETH"
    items: list[Item] = Field(min_length=1)
    mode: str = "direct"


class PartialBatch(RuntimeError):
    """The batch could not be written in full and was reversed. `standing` names the orders that
    could NOT be reversed — an empty list means nothing was written and nothing was debited."""

    def __init__(self, standing: list[str]) -> None:
        super().__init__(f"{len(standing)} order(s) still stand")
        self.standing = standing


def fee_for(amount_groth: int) -> int:
    """OUR cut of one payout — `ceil(amount × fee_bps / 10000)`, and never the bridge's.

    ⛔ CEIL, NOT FLOOR, AND INTEGER-ONLY. Floor division is how a payout smaller than
    `10000 / fee_bps` groth (50 groth at 2%) is charged NOTHING at all; since the minimum became
    a technical floor of 1 groth that is a real amount a user can ask for. Rounding up is the
    only direction that cannot cost the treasury money, and `-(-a * b // c)` does it without ever
    building a float — a float would start losing whole groth above 2^53."""
    return -(-int(amount_groth) * int(settings.fee_bps) // 10000)


def fmt_units(groth: int, asset: Asset) -> str:
    """A groth figure as a PERSON reads it: "0.02900846 ETH". THE one formatter for every
    sentence this module puts in front of a user (T45 item 5).

    Admin, 2026-09-10 15:35Z, holding a 409 he had just hit: *"Why in groth? People don't
    understand nothing in it. You should have human readable errors in ETH and show what actually
    available to user."* — the sentence had said "this batch needs 2900846 groth … top up
    shortfall_groth 66772 groth", which is an internal unit and a FIELD NAME, in a line somebody
    had to act on.

    ⛔ **THE SENTENCE AND THE FIELD ARE TWO DIFFERENT JOBS.** Every `*_groth` on the wire and
    `X-Shortfall-Groth` stay exactly as they are: a client must not have to parse English to know
    a number, and a person must not have to divide by 1e8 to know an amount. This converts only
    the prose.

    The arithmetic is `tg.fmt_groth` — 8 decimals with the trailing zeros trimmed — which is
    already what every operator line and every Telegram message uses, so there is one
    implementation of "what does this many groth look like" (law 9) and the symbol is appended
    here because a bare number in a sentence about money is half a fact.

    ⛔ **AND IT MOVED TO `payouts` ON 2026-09-10** (T52 item 7). The operator half of every hold
    needs the same formatter, `payouts` cannot import this module (this one imports it), and a
    second copy over there would be exactly the divergence law 9 is about. This is the delegate;
    the sentence above is still where the rule is written down."""
    return payouts.fmt_units(int(groth), asset)


def address_problem(raw: str) -> tuple[str, str | None]:
    """(the address in the form we store it, why it cannot be paid) — NEVER raises.

    ONE reading of "is this an address we may pay": `preview` reports it per item, `create`
    refuses the batch on it, and `normalise_address` below is the same answer with a raise
    attached. A lowercase (or uppercase) string carries NO checksum information and is accepted
    as typed; a MIXED-CASE string carries one, and a mixed-case string whose checksum does not
    match is a typo the user can still fix — refuse it rather than move money to it."""
    s = (raw or "").strip()
    if not is_hex_address(s):
        return s[:64], f"{s[:64]!r} is not an EVM address"
    body = s[2:] if s[:2].lower() == "0x" else s
    if body != body.lower() and body != body.upper() and not is_checksum_address(s):
        return s[:64], (
            f"{s[:64]} has a bad EIP-55 checksum — check the address you pasted (a lowercase "
            f"address is accepted as typed; a mixed-case one must checksum)"
        )
    return to_checksum_address(s), None


def eta_for(mode: str) -> int:
    """How long the money needs between release and delivery, in seconds."""
    return int(settings.bridge_eta_s) if mode == "direct" else INSTANT_ETA_S


def release_at_for(deliver_at: float | None, now: float, eta_s: int) -> float:
    """`max(now, deliver_at − eta)`. Absent or past → now; never a time in the past."""
    if deliver_at is None:
        return now
    return max(now, float(deliver_at) - eta_s)


def normalise_address(raw: str) -> str:
    """`address_problem` with a 400 attached, for callers that refuse the whole request."""
    addr, problem = address_problem(raw)
    if problem:
        raise HTTPException(400, problem)
    return addr


# --------------------------------------------------------------- the destination must be a wallet

_head_seen: dict[str, int] = {"head": 0}


def clear_head_floor() -> None:
    """Forget the highest head this process has seen (tests; a restart does the same)."""
    _head_seen["head"] = 0


def _unreadable(why: str) -> HTTPException:
    """503 with the sentence a PERSON gets; `why` is evidence and goes to the log.

    §9.7: the log line carries no address — `api.log` already has the request beside the account,
    and pairing an account with a destination is the whole product defeated."""
    log.info("withdrawal: the destination could not be verified (%s)", why)
    return HTTPException(503, UNREADABLE_DEST)


async def in_sync(rpc: Any, url: str) -> None:
    """Raise 503 unless `url` says it is in sync. A node that IS answering and is still catching
    up serves an old block happily, and `eth_getCode` at a block it does not have is "0x" — which
    this guard would read as "this address is a wallet", on the one refusal with no refund path."""
    try:
        syncing = await rpc.call("eth_syncing", [], prefer=url, pin=True)
    except ethpipe.RpcError as e:
        raise _unreadable(f"{url} could not say whether it is synced") from e
    if syncing is not False:
        raise _unreadable(f"{url} is still syncing")


async def refuse_contracts(addresses: list[str]) -> tuple[int, str]:
    """400 when any of these carries contract code; 503 when the chain could not say. Returns the
    (head, endpoint) the answer was proven at, so the rows can record their own evidence.

    ⛔ **EVERY ENDPOINT, EACH AT ITS OWN HEAD — THE READER THE RELEASE USES** (T52 item 6, from
    T37d). This used to take the head from `head_from()` and pin `eth_getCode` to whoever
    answered it. On prod that is publicnode, which serves the head happily and then refuses a
    code read at a numeric block ("Archive requests require a personal token") — so a user asking
    for a perfectly ordinary withdrawal got a 503 naming a provider URL, and nothing was
    scheduled. `payouts._dest_still_a_wallet` had the identical defect and was fixed with
    `ethpipe.code_at_head_anywhere`; ONE implementation of one fact (law 9) means the address the
    release will re-read is admitted by the same reader that re-reads it. The fix for an endpoint
    that will not serve a query is MORE endpoints, never a lower bar (law 8): this refuses only
    when NOT ONE of them answers.

    The head still has to be EVIDENCE, so the two checks that made `trusted_head` worth having
    stay — the endpoint that answered must be in sync, and its block must not be far below one
    this process has already seen. The head recorded on the rows is the LOWEST of the addresses'
    (the release compares its own re-read against it, and a baseline that is too high refuses a
    healthy order for ever).

    ⚠️ EVALUATED HERE AND AT RELEASE. `deliver_at` may be up to `max_window_s` (30 days) out, so
    an address that is a bare EOA today and a deployed contract tomorrow (a counterfactual CREATE2
    account, an EIP-7702 delegation) is caught by `payouts._dest_still_a_wallet`, which holds the
    row for a human rather than delivering into it."""
    rpc = workers.get_rpc()
    unique = sorted({a for a in addresses})
    floor = int(_head_seen["head"])
    answers = await asyncio.gather(
        *(ethpipe.code_at_head_anywhere(rpc, a) for a in unique), return_exceptions=True
    )
    heads: list[int] = []
    urls: list[str] = []
    for a, got in zip(unique, answers, strict=True):
        if isinstance(got, BaseException):
            # not one endpoint would read it: "we could not look" is not a verdict in either
            # direction, so nothing is scheduled and nothing is refused about the address itself
            raise _unreadable(f"{type(got).__name__}: {str(got)[:300]}")
        code, head, url = got
        if head <= 0:
            raise _unreadable(f"{url} reported block {head}")
        if head < floor - HEAD_REGRESSION_TOLERANCE:
            raise _unreadable(f"{url} reported block {head}, far below the {floor} already seen")
        if url not in urls:
            await in_sync(rpc, url)
        # ⛔ THE FLOOR RISES ON THE READING, NOT ON THE VERDICT. Recorded at the end of the loop
        # it was never recorded at all for a batch that refused — a contract in the list raises
        # before the last line — so the next request had no floor to compare a rewound node
        # against, and the guard that catches a snap-syncing provider was a no-op after every
        # 400 this function makes.
        _head_seen["head"] = max(int(_head_seen["head"]), head)
        if code.replace("0x", "").strip("0"):  # "0x" / "0x0" are the only "no code" answers
            raise HTTPException(
                400,
                f"{a} is a contract, not a wallet — the bridge delivery would be stranded there. "
                f"Use an address you control the keys to",
            )
        heads.append(head)
        urls.append(url)
    return min(heads), urls[0]


# ----------------------------------------------------------------------------- the live fee floor

_fees_cache: dict[str, dict[str, Any]] = {}


def window_margin(ahead_s: float) -> float:
    """How much more than today's relayer fee an order releasing `ahead_s` from now must fund."""
    window = float(settings.max_window_s or 0)
    if window <= 0 or ahead_s <= 0:
        return 1.0
    return 1.0 + (FAR_DATED_MARGIN - 1.0) * min(1.0, float(ahead_s) / window)


def headroom_for(ahead_s: float) -> float:
    """The margin the BRIDGE FEE has to carry, net of the subsidy the release gate already allows.

    The fee quoted at request time and the release-time gate (`live fee > bridge_fee_groth ×
    subsidy`) are two views of ONE number, so this reads it through the one function the gate
    itself reads it through — `config.relayer_subsidy`, which is where "0 / unset / below 1 all
    mean 1×" is decided ONCE. Read here as `float(x or 0) or 1.0` and there as the raw setting,
    the two disagreed at exactly 0: this side quoted a 1× crossing and the gate then held every
    order in existence. It never goes below 1× either, which would quote a crossing the treasury
    loses money on at TODAY's gas.

    ⛔ **AND NEVER BELOW `PGAS_BRIDGE_HEADROOM_MIN` EITHER** (2026-09-10). `window_margin(0)` is
    exactly 1×, i.e. an ASAP order funded the gas of the block its quote was taken in and not
    one groth more — and gas moves between two adjacent blocks. Two live orders were quoted
    14,733 groth at 10:2xZ and the release pass measured 15,793 seconds later; both were held
    "refusing to cross at a loss" on a crossing their user HAD paid for in full. The floor is
    read through `config.bridge_headroom_min`, which is where "below 1 means 1" is decided once.

    ⛔ **AND UNSPENT HEADROOM DOES NOT STAY WITH THE TREASURY ANY MORE** (T45, 2026-09-10). This
    docstring used to end "unspent headroom stays with the treasury", which is not "the bridge at
    cost": two live orders funded 14,733 groth against a 12,778-groth crossing and 1,955 groth of
    each user's money was kept for a cost nobody incurred. `payouts._book_release` credits the
    difference back to Available at settlement, so headroom is now a RESERVATION rather than a
    charge — which is what makes a conservative gas basis (`payouts.relayer_fee_for`) honest, and
    why the floor is a product choice rather than a compromise.

    ⚠️ On this deployment the curve is FLAT at that floor, because `relayer_subsidy()` is 4× and
    3/4 falls under 1.25. That is the same fact from the other side: the release gate carries the
    wait, so the user does not have to pre-fund it."""
    return max(bridge_headroom_min(), window_margin(ahead_s) / relayer_subsidy())


def grid_groth(asset: Asset) -> int:
    """The smallest amount of this asset that is a whole number on BOTH sides, in groth.

    `Asset.grid` is Ethereum units per groth (10**(decimals−8)), so for every asset this
    deployment carries — 18-decimal ETH and DAI, 8-decimal WBTC — one groth converts exactly and
    the step is 1. Stated as arithmetic rather than as the literal `1` so an asset whose Ethereum
    side is COARSER than a groth (fewer than 8 decimals) raises the floor here instead of
    silently rounding a payout down to nothing on the way out."""
    return max(1, 10 ** max(0, 8 - int(asset.eth_decimals)))


def min_amount_groth(asset: Asset) -> int:
    """The smallest payout this deployment accepts — A TECHNICAL FLOOR, NOT AN ECONOMIC ONE.

    ⛔ THE DERIVED FLOOR IS GONE (2026-09-10). It used to be "an amount whose 2% covers the
    relayer fee", which at ordinary mainnet gas is ~0.045 ETH — a floor that made the product
    (fund a fresh wallet with a few dollars of gas) impossible to use. The bridge fee is charged
    explicitly now, per item, at cost, so nothing about the economics depends on the size of the
    payout any more. What is left is arithmetic: the amount must be positive and land on the
    asset's grid. `PGAS_MIN_PAYOUT_GROTH` is a knob an operator can raise; its default is 1."""
    return max(int(settings.min_payout_groth), grid_groth(asset))


def fee_triple(
    amount_groth: int, relayer_fee_groth: int, ahead_s: float = 0.0
) -> tuple[int, int, int]:
    """(our fee, the bridge fee, the total debited) for ONE item — THE one implementation.

    `preview` shows it, `create` checks Available against it, `_write_items` stores it on the row
    and the ledger debits it. Law 9: two implementations of one fact will disagree and one of
    them will reach money, so there is exactly one place this arithmetic happens.

      ours   = ceil(amount × fee_bps / 10000)                    — revenue
      bridge = ceil(relayer_fee_now × headroom(ahead_s))         — pass-through, AT COST
      total  = amount + ours + bridge

    The bridge part is quoted at REQUEST time from the documented gas basis — `max(the live
    eth_feeHistory read, the 24 h p75 of the gas_samples series)`, `payouts.relayer_fee_for` —
    and rounded up like ours: a bridge fee rounded down is a crossing the treasury tops up out of
    its own pocket.

    ⚠️ **THE BRIDGE PART IS AN ESTIMATE, AND WHAT IT DOES NOT SPEND COMES BACK** (T45). Headroom
    is a reservation against the gas the crossing will actually meet, not revenue: at settlement
    `payouts._book_release` credits `bridge_fee_groth − relayer_fee_groth` back to Available as a
    `bridge_fee_refund` entry, and a crossing that costs MORE is absorbed by the treasury up to
    `PGAS_MAX_RELAYER_SUBSIDY`. Nothing is ever charged beyond this quote."""
    amount = int(amount_groth)
    ours = fee_for(amount)
    bridge = math.ceil(int(relayer_fee_groth) * headroom_for(ahead_s))
    return ours, bridge, amount + ours + bridge


def _first_on(step: int, floor: int) -> int:
    """The smallest multiple of `step` that is at least `floor` — and never 0.

    A floor that does not itself land on the lattice raises to the next value that does, rather
    than admitting a delivery the Ethereum side cannot pay out (or, for an instant payout, an
    amount the denominated float has no coin for)."""
    step = max(1, int(step))
    return -(-max(1, int(floor)) // step) * step


def deliverable_groth(amount_groth: int, bridge_fee_groth: int, *, step: int, floor: int) -> int:
    """The largest `d` we can DELIVER when the fees come out of the amount: a multiple of `step`,
    at least `floor`, with `d + fee_for(d) + bridge ≤ amount`. 0 when nothing fits.

    ⛔ INTEGER-ONLY AND MONOTONE. `g(d) = d + ceil(d × fee_bps / 10000)` never decreases as `d`
    grows, so "the largest lattice point whose g still fits" is a binary search over the number
    of steps — bounded by log2(amount) whatever the amount is, and never a float division, which
    starts losing whole groth above 2^53. The caller charges `amount − bridge − d` as our fee, so
    the remainder one step leaves behind (at most a step plus its own rounding) stays with the
    treasury instead of becoming a groth no bucket accounts for.

    ⚠️ A 0 here is NOT "deliver nothing" — it is "this amount cannot pay for its own crossing",
    which is a refusal (`BRIDGE_FEE_CODE`), never an order with nothing in it."""
    amount = int(amount_groth)
    bridge = int(bridge_fee_groth)
    step = max(1, int(step))
    first = _first_on(step, floor)
    cap = amount - bridge  # what is left for (the delivery + our cut on it)
    k_lo, k_hi = first // step, cap // step  # d = k × step, and g(d) ≥ d bounds k by cap // step
    if k_hi < k_lo or cap < first + fee_for(first):
        return 0

    def fits(k: int) -> bool:
        d = k * step
        return d + fee_for(d) + bridge <= amount

    lo, hi = k_lo, k_hi  # invariant: fits(lo) is True (checked above), fits(hi + 1) is False
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo * step


def fees_may_come_from_the_amount(mode: str) -> bool:
    """Whether this mode may take the fees OUT of the amount instead of beside it.

    ⛔ NOT FOR AN INSTANT PAYOUT. The rule works because the delivery lands on a grid of ONE
    groth: the most an amount can lose to the rounding is a groth, which our fee absorbs, and
    "debited == the amount you typed" stays true to within dust. An instant payout is paid out of
    DENOMINATED float (0.01 / 0.1 ETH) and `price_item` already refuses an amount that is not a
    multiple of a denomination, so the amount that leaves must stay on that lattice — and the
    next denomination DOWN is not a rounding, it is half the order. Shaving 0.02 ETH to 0.01 ETH
    while debiting the whole 0.02 would book 0.0098 ETH as "our 2% fee": a windfall of half the
    payout, taken from a user who asked for the other half. So an instant order whose fees do not
    fit on top is short (the batch rule refuses it and says by how much), never quietly halved.
    A direct payout crosses the bridge and has no such lattice."""
    return mode != "instant"


def deliverable_for(amount_groth: int, bridge_fee_groth: int, *, asset: Asset, floor: int) -> int:
    """`deliverable_groth` on the asset's own grid — the lattice a direct delivery lands on."""
    return deliverable_groth(amount_groth, bridge_fee_groth, step=grid_groth(asset), floor=floor)


def smallest_from_amount(bridge_fee_groth: int, *, asset: Asset, floor: int) -> int:
    """The smallest amount that can still pay its own fees out of itself — the number the
    refusal names, derived from the same lattice the solver searches (law 9)."""
    first = _first_on(grid_groth(asset), floor)
    return first + fee_for(first) + int(bridge_fee_groth)


def delivery_is_dominated(delivered_groth: int, requested_groth: int) -> bool:
    """Would this order be mostly fees? THE ASSUMPTION, in one place, as integer arithmetic.

    Stated as a cross-multiplication rather than as `delivered / requested < 0.5`: a float
    division of two groth counts starts losing whole groth above 2^53, and this decides whether
    an order is written."""
    return int(delivered_groth) * MIN_DELIVERED_DEN < int(requested_groth) * MIN_DELIVERED_NUM


def _half_fits(amount_groth: int, bridge_fee_groth: int, *, step: int, floor: int) -> bool:
    """Does the from-amount rule still deliver at least half of `amount_groth`?

    `deliverable_groth` returns the LARGEST feasible delivery and `d + fee_for(d)` never
    decreases as `d` grows, so "the largest feasible delivery covers half" and "the smallest
    legal delivery at or above half is feasible" are the same statement — and this one costs one
    comparison instead of a search. `test_the_half_rule_and_the_solver_agree_groth_by_groth`
    brute-forces the equivalence rather than assuming it."""
    amount = int(amount_groth)
    half = -(-amount * MIN_DELIVERED_NUM // MIN_DELIVERED_DEN)  # ceil, integer-only
    d = _first_on(max(1, int(step)), max(half, int(floor)))
    return d + fee_for(d) + int(bridge_fee_groth) <= amount


def smallest_undominated(bridge_fee_groth: int, *, asset: Asset, floor: int) -> int:
    """The smallest amount whose from-amount delivery is still at least half of it — the number
    `FEES_DOMINATE_CODE` names as the way out.

    ⛔ DERIVED FROM THE SAME LATTICE THE SOLVER SEARCHES (law 9), never from a rearranged
    formula: a refusal that names an amount the next request refuses again is a lie that costs
    the user a round trip. The predicate rises by roughly half a step per step but wobbles by
    one groth either side of the crossing (the fee is a ceiling and half of an odd amount rounds
    up), so the search finds *a* crossing and then walks DOWN while the answer still holds. Both
    bounds are there to make the loop finite, never to shape the answer: every exit returns an
    amount the predicate accepts, so the error can only ever be upward — an amount that works."""
    step = max(1, grid_groth(asset))
    bridge = int(bridge_fee_groth)
    # below this there is no delivery at all (`BRIDGE_FEE_CODE` owns that refusal), so the
    # search starts where the from-amount rule starts existing
    k_lo = _first_on(step, smallest_from_amount(bridge, asset=asset, floor=floor)) // step

    def ok(k: int) -> bool:
        return _half_fits(k * step, bridge, step=step, floor=floor)

    k_hi = max(k_lo, 1)
    for _ in range(64):  # the amount doubles: 64 rounds is every integer this API can carry
        if ok(k_hi):
            break
        k_hi *= 2
    else:  # pragma: no cover — unreachable while g(d) ≈ 1.02·d; never loop forever
        return k_hi * step
    lo, hi = k_lo, k_hi
    while lo < hi:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid + 1
    for _ in range(64):  # the wobble around the crossing is a groth wide; 64 is generous
        if lo <= k_lo or not ok(lo - 1):
            break
        lo -= 1
    return lo * step


# ─────────────────────────────────────────────── reading a row that was written under either rule


def row_fee_mode(row: dict[str, Any]) -> str:
    """Which side of the amount THIS order's fees came from. THE ONE READER (law 9).

    A `payout_requests` row written before 2026-09-10 carries no `fee_mode` at all, and there was
    only one rule then: the fees rode on top and the user received the whole amount. Rows are
    never rewritten (never edit history to fix a ledger), so the compatibility lives here, in one
    function every reader — this router, `payouts`, the account route, the admin panel — calls."""
    return str(row.get("fee_mode") or ON_TOP)


def row_delivered_groth(row: dict[str, Any]) -> int:
    """What this order puts in the user's wallet. `amount_groth` on the row IS that number (the
    release sends it), and `delivered_groth` is written beside it as the unambiguous name."""
    value = row.get("delivered_groth")
    if value is None:
        value = row.get("amount_groth") or 0
    return int(value)


def row_debited_groth(row: dict[str, Any]) -> int:
    """What the order took out of Available, from the row. A row that recorded its own total is
    read; an older one is re-derived from its parts — never guessed, and never from a rate.

    ⚠️ This is the ROW's evidence, for display and audit. A REFUND still asks the LEDGER
    (`ledger.debited_groth`): a refund is the mirror of a debit that landed, and only the entries
    know which halves landed."""
    total = row.get("total_debited_groth")
    if total is None:
        total = row.get("debited_groth")
    if total is None:
        total = (
            row_delivered_groth(row)
            + int(row.get("fee_groth") or 0)
            + int(row.get("bridge_fee_groth") or 0)
        )
    return int(total)


# the windows a form actually offers, sampled off the (linear) headroom curve so a UI can label
# the rows it shows without re-deriving our arithmetic. `max_window_s` is always included.
HEADROOM_POINTS_S = (0, 3600, 86_400, 7 * 86_400)


def headroom_curve() -> list[dict[str, float]]:
    """[{window_s, factor}] — the headroom the bridge fee carries at each of those waits."""
    window = int(settings.max_window_s or 0)
    points = sorted({int(x) for x in HEADROOM_POINTS_S if x <= window} | {window})
    return [{"window_s": x, "factor": round(headroom_for(x), 4)} for x in points]


async def live_fees(asset: Asset) -> dict[str, Any]:
    """{fee_bps, min_amount_groth, bridge_fee_groth_now, headroom, relayer_fee_groth_now,
    bridge_eta_s} — measured, ≤ 60 s old.

    A fee we could not read RAISES (503). Law 4: a stale fee refuses, it never reuses; and the
    number we set is the number nobody quotes back to us, so it is read from the live gas price
    through the same helper the release itself uses (payouts.relayer_fee_for), never assumed.

    ⛔ AND A FEE THAT READS AS ZERO IS NOT A READING EITHER (law 8: an unreadable query is not
    evidence of anything, and never a 0). Every caller of this multiplies that number by the
    headroom and charges it; a 0 would quote free crossings for as long as the cache holds, and
    the release-time gate — `live fee > bridge_fee_groth × subsidy` — would then hold every one
    of those orders. So it refuses the same way an exception does.

    `bridge_fee_groth_now` is what an order released NOW is charged for its crossing (headroom
    1×); an order scheduled further out is charged more, per item, through `fee_triple`."""
    from .. import payouts  # local: payouts imports the worker module this router also uses

    now = time.time()
    hit = _fees_cache.get(asset.key)
    if hit and now - float(hit["at"]) < FEES_CACHE_S:
        return hit["fees"]
    try:
        relayer_fee, _floor, _detail = await payouts.relayer_fee_for(asset, workers.get_rpc())
    except Exception as e:  # noqa: BLE001 — gas or price unreadable: say so, never guess a fee
        raise HTTPException(
            503,
            f"could not read the live bridge relayer fee ({type(e).__name__}) — nothing was "
            f"scheduled; try again in a moment",
        ) from e
    if int(relayer_fee) <= 0:
        raise HTTPException(
            503,
            "the live bridge relayer fee read as 0, which is not a price this crossing can be "
            "quoted at — nothing was scheduled; try again in a moment",
        )
    # the bridge half of the SAME call every item is priced with — amount 0 because the bridge
    # fee does not depend on the amount, and asking `fee_triple` rather than re-multiplying is
    # what keeps the number the form shows and the number an order is charged one implementation.
    _ours, bridge_now, _total = fee_triple(0, int(relayer_fee), 0.0)
    fees = {
        "fee_bps": settings.fee_bps,
        "min_amount_groth": min_amount_groth(asset),
        # what the user pays the bridge for an order that leaves now, and the raw measurement it
        # came from: one is the charge, the other is the evidence, and they part company as soon
        # as the order is scheduled ahead (headroom) or the deployment allows a subsidy.
        "bridge_fee_groth_now": bridge_now,
        "headroom": headroom_curve(),
        "relayer_fee_groth_now": int(relayer_fee),
        "bridge_eta_s": int(settings.bridge_eta_s),
    }
    _fees_cache[asset.key] = {"at": now, "fees": fees}
    return fees


# ----------------------------------------------------------------- pricing one item, once, here


def price_item(
    it: Item,
    *,
    asset: Asset,
    mode: str,
    now: float,
    eta_s: int,
    relayer_fee_groth: int,
    min_amount: int,
    remaining: int,
) -> dict[str, Any]:
    """One item priced and ruled on, as `preview` renders it and `create` acts on it.

    NEVER RAISES: a problem is a field on the answer, not an exception, because `preview` has to
    show the user every row — the good ones with their numbers and the bad ones with the reason —
    and `create` has to be able to say which of five items it refused for what. The item is
    priced even when it is refused: a wrong address does not change what the amount would cost,
    and a form that blanks the totals the moment one row is mistyped is a form nobody can fill in.

    The first problem found is the one reported, in the order a user can act on them: the address
    they pasted, the time they picked, the amount they typed, then the denomination rule. Every
    problem carries a stable `problem_code` beside its English: a UI keys off the code, and — the
    reason it exists at all — §9.7 lets the OPERATOR LOG name what was refused without repeating
    the sentence, because the address-shaped problems quote the address the user typed and
    api.log must never pair an account with a destination.

    `remaining` is what is left of Available AFTER the rows above this one, and it decides where
    this row's fees come from — on top of the amount, or out of it (2026-09-10). It is the only
    input here that is not about the item itself, which is why the answer carries `fee_mode`,
    `delivered_groth` and `debited_groth` rather than leaving a caller to re-derive them: the
    number the user is shown, the number the batch is checked against, the number on the row and
    the number the ledger takes are one call (law 9)."""
    amount = int(it.amount_groth)
    w, problem = address_problem(it.W)
    code = "address" if problem else ""
    # POSITIVE: the value must BE a delivery time in range. Phrased the other way round ("refuse
    # it when it is out of range") a NaN passes, because every comparison with NaN is False — and
    # pydantic will hand us one from the ordinary JSON string "NaN" if the model lets it.
    if problem is None and it.deliver_at is not None and not (
        MIN_DELIVER_AT <= float(it.deliver_at) <= now + settings.max_window_s
    ):
        days = settings.max_window_s // 86400
        code, problem = "deliver_at", (
            f"deliver_at must be a unix time in the next {days} days — a delivery more than "
            f"{days} days away (or a time that is not a time) is refused; schedule it closer to "
            f"the time you want the money"
        )
    if problem is None and amount < min_amount:
        code, problem = "min_amount", f"each payout must be at least {fmt_units(min_amount, asset)}"
    grid = grid_groth(asset)
    if problem is None and amount % grid:
        code, problem = "grid", (
            f"{asset.key} amounts must be a whole multiple of {fmt_units(grid, asset)} — "
            f"anything finer cannot be paid out on the Ethereum side"
        )
    denoms = settings.denominations
    if problem is None and mode == "instant" and not any(amount % d == 0 for d in denoms):
        code, problem = "denomination", (
            "instant payouts must be a multiple of a denomination "
            f"({', '.join(fmt_units(d, asset) for d in denoms)})"
        )
    release_at = release_at_for(it.deliver_at, now, eta_s)
    ours, bridge, total = fee_triple(amount, relayer_fee_groth, release_at - now)
    # ── WHERE THIS ROW'S FEES COME FROM, against what the rows above it left behind ────────
    # The order of the three branches is the rule itself: the user gets everything they asked
    # for whenever the balance can pay the fees beside it, and only a balance that cannot does
    # the amount shrink. A row that cannot even cover its own amount is not a mistake the user
    # made in THIS row — it is the batch running out of money, so it carries the batch's code
    # and `_refuse_items` leaves it to the batch rule (a 409 with the shortfall, never a 422).
    left = max(0, int(remaining))
    from_amount_ok = fees_may_come_from_the_amount(mode)
    fee_mode, delivered, debited, note = ON_TOP, amount, total, ""
    if left >= total:
        pass  # the fees ride on top: delivered == amount, debited == amount + fee + bridge
    elif from_amount_ok and left >= amount:
        fee_mode, debited, note = FROM_AMOUNT, amount, FROM_AMOUNT_NOTE
        delivered = deliverable_for(amount, bridge, asset=asset, floor=min_amount)
        if delivered > 0:
            # OUR FEE ABSORBS THE ROUNDING, so the three parts are exactly the debit. The
            # remainder a grid (or denomination) step leaves behind is at most one step; giving
            # it to the delivery would break the lattice, and leaving it out of every line would
            # be a groth debited from Available that no bucket names.
            ours = amount - bridge - delivered
            # ⛔ …AND A DELIVERY THAT IS MOSTLY FEES IS NOT A WITHDRAWAL (T35b). The rule above
            # is happy to deliver 1 groth against a 22,502-groth debit — arithmetically correct
            # and nothing anybody asked for. THE ASSUMPTION IS STATED HERE, in the refusal: at
            # least half of what was asked for reaches the wallet. Unlike the `delivered == 0`
            # case below, the QUOTE IS LEFT ALONE — there IS a delivery to show, the refusal is
            # a policy about its size, and re-quoting the row on top would silently re-price
            # every row under it in a batch that is refused as a unit anyway.
            if problem is None and delivery_is_dominated(delivered, amount):
                least = smallest_undominated(bridge, asset=asset, floor=min_amount)
                code, problem = FEES_DOMINATE_CODE, (
                    f"fees would take more than half of {fmt_units(amount, asset)} — hold at "
                    f"least {fmt_units(total, asset)} of balance so fees are charged on top, or "
                    f"ask for at least {fmt_units(least, asset)}"
                )
        else:
            # nothing is left to deliver once the crossing is paid out of the amount: a refusal
            # about THIS row (raise it, or fund the fees on top), not about the batch. The QUOTE
            # falls back to the on-top one — the row is not going anywhere, and every answer this
            # function returns keeps `delivered + fee + bridge == debited` so a client can render
            # the arithmetic of ANY row without a special case for the refused ones.
            fee_mode, delivered, debited, note = ON_TOP, amount, total, ""
            if problem is None:
                least = smallest_from_amount(bridge, asset=asset, floor=min_amount)
                code, problem = BRIDGE_FEE_CODE, (
                    f"{fmt_units(amount, asset)} is too small to pay the bridge fee from the "
                    f"amount — the crossing alone costs {fmt_units(bridge, asset)} and there "
                    f"must be something left to deliver. Either hold enough balance for the fees "
                    f"to be charged on top of the amount, or ask for at least "
                    f"{fmt_units(least, asset)}"
                )
    elif problem is None:
        # what the row NEEDS depends on where its fees may come from: an amount for a direct
        # payout (the fees can come out of it), the whole total for an instant one (they cannot).
        need_here = amount if from_amount_ok else total
        code, problem = BATCH_CODE, (
            f"this row needs {fmt_units(need_here, asset)} and only {fmt_units(left, asset)} of "
            f"your balance is left after the rows above it — short by "
            f"{fmt_units(need_here - left, asset)}"
        )
    out: dict[str, Any] = {
        "W": w,
        # WHAT THE USER TYPED. `delivered_groth` is what their wallet receives, and the two are
        # the same number unless the fees had to come out of the amount.
        "amount_groth": amount,
        "requested_groth": amount,
        "delivered_groth": delivered,
        "fee_groth": ours,
        "bridge_fee_groth": bridge,
        # what this row takes out of Available. `total_groth` is the name it has always had here
        # and `debited_groth` is the name the contract gave it on 2026-09-10; they are one value,
        # written twice rather than derived twice.
        "total_groth": debited,
        "debited_groth": debited,
        "fee_mode": fee_mode,
        "deliver_at": it.deliver_at,
        "release_at": release_at,
        # the floor this item was ruled against, carried ON the item so the row it becomes and
        # the answer the user gets read the same number from the same place — never patched in
        # afterwards by whichever caller remembered to
        "min_amount_groth": min_amount,
        # ⛔ **"USE MAX" IS THE API'S ARITHMETIC** (T45 item 5). The largest amount THIS row can
        # ask for and still have its fees charged ON TOP, out of what the rows above it left of
        # Available — the same lattice a from-amount row is solved on (`deliverable_for`: the
        # largest d with d + fee(d) + bridge ≤ left). A form that computed `available − 2 % −
        # bridge` for a "max" button would be a second implementation of the fee model, and the
        # one that reaches the user's own money first (law 9). 0 means there is nothing left for
        # this row to ask for — an honest answer, and not a button.
        "max_on_top_groth": deliverable_for(left, bridge, asset=asset, floor=min_amount),
        "ok": problem is None,
    }
    if note:
        out["fee_note"] = note
    if problem is not None:
        out["problem"] = problem
        out["problem_code"] = code
    return out


def price_items(
    items: list[Item],
    *,
    asset: Asset,
    mode: str,
    now: float,
    relayer_fee_groth: int,
    available: int,
    treasury: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """`price_item` over the batch, IN ROW ORDER — the ONE call `preview` and `create` both make.

    ⛔ THE ROWS ARE NOT INDEPENDENT ANY MORE (2026-09-10). Where a row's fees come from depends
    on what the rows ABOVE it left of Available, so this is a fold and not a map: `remaining`
    starts at Available and falls by each row's debit. The order the user typed their wallets in
    is therefore load-bearing — the early rows are funded in full and the row where the balance
    runs out is the one that pays its fees out of its amount — which is why it is stated here
    rather than being an accident of the comprehension it used to be.

    A row that is refused for a reason of its own (a mistyped address, a time out of range) is
    still priced and still consumes its debit: the whole batch is refused as a unit, so nothing
    is written either way, and a form that re-shuffles every other row's numbers while the user
    fixes one typo is a form nobody can fill in.

    ⛔ **AND THE TREASURY'S FLOAT IS A SECOND FOLD, SPENT THE WAY THE RELEASE SPENDS IT** (T52).
    `treasury` is `payouts.float_schedule(asset)` whole — what the WALLET can move now, per
    source, minus everything already promised, and when the locked rest comes back. Each admitted
    row takes its whole crossing (`payouts.crossing_need_groth`) out of ONE source, because a
    crossing is funded from one address and never from two. `None` means the float could not be
    measured, and then this fold does not run at all: "we could not look" is not "we cannot pay"
    (law 8)."""
    eta_s = eta_for(mode)
    floor = min_amount_groth(asset)
    remaining = max(0, int(available))
    left = dict(treasury["sources"]) if treasury is not None else None
    unlock = treasury.get("next_unlock_at") if treasury is not None else None
    out: list[dict[str, Any]] = []
    for it in items:
        priced = price_item(
            it,
            asset=asset,
            mode=mode,
            now=now,
            eta_s=eta_s,
            relayer_fee_groth=relayer_fee_groth,
            min_amount=floor,
            remaining=remaining,
        )
        if left is not None and priced["ok"]:
            need = payouts.crossing_need_groth(
                int(priced["delivered_groth"]), int(priced["bridge_fee_groth"])
            )
            # the biggest bucket first: it is the one most likely to cover the next row too, and
            # it is the order `payouts._funding_gates` picks a source in
            src = next(
                (k for k, v in sorted(left.items(), key=lambda kv: -kv[1]) if v >= need), None
            )
            if src is None:
                priced["ok"] = False
                priced["problem_code"] = TREASURY_FLOAT_CODE
                priced["problem"] = treasury_float_problem(
                    asset.key,
                    deliverable_now_groth(
                        max(left.values(), default=0),
                        int(priced["bridge_fee_groth"]),
                        asset=asset,
                    ),
                    unlock,
                )
            else:
                left[src] -= need
        remaining = max(0, remaining - int(priced["debited_groth"]))
        out.append(priced)
    return out


def totals_of(priced: list[dict[str, Any]]) -> dict[str, int]:
    """What the batch costs, summed from the SAME dicts the user was shown.

    `amount_groth` is what was REQUESTED and `delivered_groth` what the wallets receive; they
    differ by whatever the from-amount rows had to give up. `total_debited_groth` is what leaves
    Available, and it is the number the batch rule is decided on."""
    return {
        "amount_groth": sum(int(i["amount_groth"]) for i in priced),
        "delivered_groth": sum(int(i["delivered_groth"]) for i in priced),
        "fee_groth": sum(int(i["fee_groth"]) for i in priced),
        "bridge_fee_groth": sum(int(i["bridge_fee_groth"]) for i in priced),
        "total_debited_groth": sum(int(i["total_groth"]) for i in priced),
    }


def on_top_total(item: dict[str, Any]) -> int:
    """What ONE priced row costs when its fees ride on top — from the row's own numbers.

    It is `requested + our fee on the requested amount + the bridge fee`, and it does not depend
    on the balance: the crossing is quoted off `release_at`, which the user chose. That is what
    makes `top_up_needed` converge, and it is why the answer is read from the dict rather than
    re-derived by a caller (law 9)."""
    amount = int(item["requested_groth"])
    return amount + fee_for(amount) + int(item["bridge_fee_groth"])


def top_up_needed(
    items: list[Item],
    priced: list[dict[str, Any]],
    *,
    asset: Asset,
    mode: str,
    now: float,
    relayer_fee_groth: int,
    available: int,
) -> int:
    """THE SMALLEST TOP-UP THAT MAKES THIS BATCH GO THROUGH IN FULL — a fixed point, not a gap.

    ⛔ THE OLD NUMBER WAS SELF-DEFEATING. It was `Σ(what the batch debits at TODAY'S balance) −
    Available`, and today's balance is exactly what prices a row FROM ITS AMOUNT: credit the
    published figure and that row flips to on-top, which costs our fee and the crossing MORE
    than it did, so the batch is refused a second time — by a number we published ourselves.
    (Skeptic's example: Available 1,000,000 with rows of 1,000,000 and 100,000.)

    So the answer is the fixed point: price the batch against `Available + T`, and raise `T`
    until the pricing stops growing. A row's on-top total is independent of the balance, so it
    settles on the first round — the loop is what makes that a CHECKED fact rather than an
    assumption, and if a balance-dependent fee ever appears the number stays correct. The bound
    is there so a pathological pricing cannot spin; the value it leaves is Σ of every row's
    on-top total minus Available, which is the answer that always holds because no row can ever
    debit more than its own fees-on-top total.

    It is never SMALLER than the gap it replaced (Σ debited ≤ Σ on-top), and it may be larger
    than the smallest top-up that would merely be ACCEPTED — deliberately: the sentence promises
    that after topping this up every wallet receives the full amount it asked for."""
    available = max(0, int(available))
    top = max(0, sum(on_top_total(i) for i in priced) - available)
    for _ in range(SHORTFALL_ROUNDS):
        if top <= 0:
            return 0
        again = price_items(
            items,
            asset=asset,
            mode=mode,
            now=now,
            relayer_fee_groth=relayer_fee_groth,
            available=available + top,
        )
        nxt = max(0, sum(on_top_total(i) for i in again) - available)
        if nxt <= top:
            break
        top = nxt
    return top


def batch_problem(asset: str, need: int, available: int, shortfall: int) -> str:
    """THE sentence about a batch that is over Available — said once, in one place.

    `preview` puts it on `batch.problem` and `create` refuses with it verbatim, so the reason
    the user reads before they send and the reason they read when it is refused cannot drift.
    The number is in the sentence AND in `X-Shortfall-Groth` / `batch.shortfall_groth`, because
    a client must not have to parse English to know how much short the batch is.

    ⛔ `need` AND `shortfall` ARE NOT THE SAME SUBTRACTION (T35b), and the sentence says so.
    `need` is what this batch debits AS TYPED, against the balance as it is; `shortfall` is the
    top-up that makes every row's fees ride on top, which is more — see `top_up_needed`. A
    sentence that let the reader subtract one from the other would be teaching them the number
    that did not work.

    ⛔ **IN THE ASSET, NOT IN GROTH, AND WITHOUT A FIELD NAME IN IT** (T45 item 5). This used to
    read *"insufficient ETH balance: this batch needs 2900846 groth and 2834074 groth is
    available — top up shortfall_groth 66772 groth …"*, which the admin hit himself: an internal
    unit and a wire field, in the one line a person has to act on.

    ⛔ **AND IT NO LONGER HEDGES.** It ended "(… another withdrawal of yours may be in flight)",
    which is a DIFFERENT cause with a sentence of its own (`INFLIGHT_PROBLEM`, raised where that
    is actually what happened). A refusal that lists two possible causes is a refusal the reader
    cannot act on — and this one is a fixed point: crediting exactly `shortfall` makes the batch
    go through with every row on top, which is a promise, not a maybe."""
    return (
        f"Not enough {asset}: this batch needs {fmt_units(need, get_asset(asset))} and "
        f"{fmt_units(available, get_asset(asset))} is available. Top up "
        f"{fmt_units(shortfall, get_asset(asset))} and every wallet receives the full amount it "
        f"asked for (fees are charged on top once your balance covers them)."
    )


def batch_verdict(
    totals: dict[str, int], asset: str, available: int, *, shortfall: int
) -> dict[str, Any]:
    """{ok, need_groth, available_groth, shortfall_groth, problem?} — THE batch rule, once.

    ⛔ THE ONE RULE LEFT AT THE BATCH LEVEL (Σ total_groth ≤ Available) HAD NO ANSWER OF ITS OWN.
    Every item came back `ok: true` and the verdict lived only in the difference between two
    other numbers, so a client had to re-derive it — a second implementation of the fact that
    decides whether money moves, and the two are not even asked in the same request. Now the
    preview carries the verdict itself, computed from the totals the user was shown against the
    balance as it is right now.

    It is a QUOTE, NOT A RESERVATION: `create` decides atomically (`reserve`) against Available
    at that moment, so `ok: true` here is "it fits right now", never a promise, and `ok: false`
    is a batch the withdrawal WILL refuse unless the balance moves. Per-ITEM verdicts stay about
    the item alone (`price_item`) — a batch that does not fit is not one row's fault, and marking
    the row where a running total happens to cross is arithmetic no user can act on.

    ⛔ `ok` IS STILL "Σ debited ≤ Available", NOT "the shortfall is zero" (T35b). The two were
    the same number until the shortfall became a fixed point; deciding `ok` on the new one would
    refuse the very batch T35 exists to allow — Σ amounts fits, Σ (amounts + fees) does not, so
    the last row pays its own fees and the batch goes through. `shortfall` is what to TOP UP,
    and it is published only when there is something to refuse."""
    need = int(totals["total_debited_groth"])
    available = int(available)
    ok = need <= available
    shortfall = 0 if ok else max(0, int(shortfall))
    out: dict[str, Any] = {
        "ok": ok,
        "need_groth": need,
        "available_groth": available,
        "shortfall_groth": shortfall,
    }
    if not ok:
        out["problem"] = batch_problem(asset, need, available, shortfall)
    return out


def deliverable_now_groth(float_groth: int, bridge_fee_groth: int, *, asset: Asset) -> int:
    """The largest amount a user may ASK FOR right now and have the treasury able to move it.

    The treasury has to move the delivery AND the crossing's own relayer fee (the send burns
    both) and keep its float reserve, which is `payouts.crossing_need_groth` read backwards — one
    reader, so the number the sentence offers is a number the very next request is accepted at.
    OUR 2% is not in it: that is a ledger fee, it never leaves the Beam wallet.

    0 means nothing can be asked for at all (below the deployment's own floor), which is an
    honest answer and not an offer."""
    step = grid_groth(asset)
    room = int(float_groth) - int(bridge_fee_groth) - int(settings.float_min_groth or 0)
    if room < min_amount_groth(asset):
        return 0
    return (room // step) * step


def treasury_float_problem(asset: str, deliverable_groth: int, next_unlock_at: float | None) -> str:
    """THE sentence about a batch the treasury cannot deliver yet — said once, in one place.

    `preview` puts it on the row and on `treasury.problem`, `create` refuses with it verbatim.
    Human units per T45 (`fmt_units`), the date from the float schedule (`payouts.fmt_when`), and
    two ways out because there really are two: ask for less now, or come back when the lock ends.
    When nothing is maturing there is no date to promise, and the sentence says so rather than
    inventing one — an unmeasurable unlock is not a time (law 8)."""
    a = get_asset(asset)
    when = payouts.fmt_when(next_unlock_at) if next_unlock_at else ""
    if int(deliverable_groth) <= 0:
        if when:
            return (
                f"No withdrawal can be delivered right now. The treasury's funds unlock on "
                f"{when} — try again then."
            )
        return "No withdrawal can be delivered right now — please try again in a little while."
    if when:
        return (
            f"Withdrawals of up to {fmt_units(deliverable_groth, a)} can be delivered right now. "
            f"The rest of the treasury's funds unlock on {when} — ask for a smaller amount now, "
            f"or come back then."
        )
    return (
        f"Withdrawals of up to {fmt_units(deliverable_groth, a)} can be delivered right now — "
        f"ask for a smaller amount now, or try again later."
    )


def treasury_verdict(
    priced: list[dict[str, Any]],
    schedule: dict[str, Any] | None,
    bridge_fee_now: int,
    *,
    asset: Asset,
) -> dict[str, Any]:
    """`{ok, float_now_groth, deliverable_now_groth, next_unlock_at, problem?}` — the TREASURY's
    verdict on this batch, beside `batch_verdict`'s verdict on the user's balance.

    Two different questions with two different answers, and neither is the other: `batch` is
    "does your Available cover this", this is "can we move it today". A form shows both; `create`
    refuses on this one first, because it is the harder cap and the only one the user cannot fix
    by topping up.

    `schedule is None` is "we could not measure it", and it is `ok: true` with nulls — the
    release gate holds such an order honestly and tells the user when. Refusing every withdrawal
    because BeamPay hiccuped would be a guess in the other direction (law 8).

    `problem` is the FIRST refused row's own sentence, never a second composition of it: in a
    batch the rows are served in order, so the row that ran out names what is left where it ran
    out, and that is the number that makes the list go through."""
    if schedule is None:
        return {
            "ok": True,
            "float_now_groth": None,
            "deliverable_now_groth": None,
            "next_unlock_at": None,
        }
    short = next((i for i in priced if i.get("problem_code") == TREASURY_FLOAT_CODE), None)
    out: dict[str, Any] = {
        "ok": short is None,
        "float_now_groth": int(schedule["spendable_now_groth"]),
        "deliverable_now_groth": deliverable_now_groth(
            int(schedule["spendable_now_groth"]), int(bridge_fee_now), asset=asset
        ),
        "next_unlock_at": schedule.get("next_unlock_at"),
    }
    if short is not None:
        out["problem"] = short["problem"]
    return out


def clear_fees_cache() -> None:
    _fees_cache.clear()


# ------------------------------------------------------ the claim a cancel makes, as a query


def cancel_claim() -> dict[str, Any]:
    """`payouts.cancellable` written as a Mongo filter — THE SAME QUESTION, asked atomically.

    ⛔ A READ AND THEN AN ACT IS NOT A CHECK (the invariant this file already states about
    Available, one level down). `cancel` reads the row, `payouts.cancellable` rules on it, and
    in between the processor can release the order and sign its crossing; a flip filtered on
    anything looser would then refund money that has left. So the flip is conditional on exactly
    what the reader refuses on — the status set, and the three fields that mean "something has
    been signed for this order" — and a row that moved matches nothing and is refused.

    Law 9 says one writer per fact, and this IS a second statement of one rule: it is built from
    `payouts.CANCELLABLE` itself, and `test_the_atomic_claim_asks_exactly_what_the_reader_asks`
    checks the two against each other over every status × blocker combination, because a filter
    that is LAXER refunds an order that has left and one that is STRICTER keeps a user's money."""
    return {
        "status": {"$in": list(payouts.CANCELLABLE)},
        **{field: {"$in": NOTHING} for field in CANCEL_BLOCKERS},
    }


# ------------------------------------------------------------------------- the atomic reservation


def _res_id(account_id: str, asset: str) -> str:
    return f"{account_id}:{asset}"


async def reserve(account_id: str, asset: str, need: int) -> bool:
    """Atomically admit `need` groth against Available. Returns False when there is no room.

    ⛔ THE CLAIM COMES FIRST AND THE BALANCE IS READ AFTER IT. This used to read Available and
    then use that number as a constant inside the conditional update: between the two awaits
    another request could claim, write its entries and release, so `pending` was back at 0 and
    the filter passed on a balance that was already spent — both batches were admitted and only
    the `after < 0` belt reversed one. Incrementing FIRST and reading the ledger AFTERWARDS makes
    the two facts commute: a request that finished before the read is in `available`, and one
    still in flight is in `pending`, so the sum can never exceed the balance. The increment is
    backed out immediately when it does not fit, before a single entry is written."""
    d = db()
    key = _res_id(account_id, asset)
    now = time.time()
    await d.reservations.update_one(
        {"_id": key},
        {"$setOnInsert": {"pending": 0, "account_id": account_id, "asset": asset, "at": now}},
        upsert=True,
    )
    # a crash between claim and release would otherwise block the account forever. Clearing it is
    # safe: the ledger, not this counter, is the balance — pending is only the in-flight cushion.
    # `$ne: 0` and not `$gt: 0`, so a counter that somehow went NEGATIVE is repaired too: a
    # negative cushion is a permanently DISARMED gate, and `$gt: 0` never matches one.
    await d.reservations.update_one(
        {"_id": key, "pending": {"$ne": 0}, "at": {"$lt": now - RESERVATION_STALE_S}},
        {"$set": {"pending": 0, "at": now, "stale_cleared_at": now}},
    )
    before = await d.reservations.find_one_and_update(
        {"_id": key}, {"$inc": {"pending": need}, "$set": {"at": now}}, upsert=True
    )
    pending = int((before or {}).get("pending") or 0) + need
    available = (await ledger.balance(account_id, asset))["available"]
    if pending > available:
        await release(account_id, asset, need)
        return False
    return True


async def release(account_id: str, asset: str, need: int) -> None:
    """Give the reservation back: the entries have landed (Available already reflects them) or
    nothing was written at all.

    ⛔ CLAMPED. This was an unconditional `$inc -need`, and the stale-clear above can zero a
    cushion that is still in flight — so the release that followed drove `pending` NEGATIVE and
    it never recovered, leaving that account's admission filter permanently looser than intended
    (and the balance defended only by the compensation belt). The decrement now happens only
    while there is something to decrement, and otherwise the counter is set to the floor."""
    d = db()
    key = _res_id(account_id, asset)
    now = time.time()
    res = await d.reservations.update_one(
        {"_id": key, "pending": {"$gte": need}},
        {"$inc": {"pending": -need}, "$set": {"at": now}},
    )
    if not res.matched_count:
        await d.reservations.update_one(
            {"_id": key}, {"$set": {"pending": 0, "at": now, "clamped_at": now}}
        )


# ------------------------------------------------------------------------------- writing the rows


async def _page_operator(kind: str, text: str, ids: list[str]) -> None:
    """Page NOW about orders that are already live — and never let the paging itself be the
    caller's error. §9.7: ids only, never `W`.

    This is the last thing standing between a silent defect and the operator, and it is called
    from the paths where Mongo is already misbehaving — so `tg.alert` (which writes an event row
    before it sends) is exactly as likely to fail as the thing it is reporting. A failure to page
    is logged and swallowed: it must not turn a live, debited, correctly-written batch into a
    500 that the client retries into a second set of payouts."""
    try:
        await tg.alert(kind, text, request_ids=ids)
    except Exception:  # noqa: BLE001 — the pager failing is not the caller's problem
        log.exception("could not page the operator (%s) about %s", kind, ",".join(ids))


async def _void(aid: str, asset: str, rid: str, why: str) -> None:
    """Give one item's debit back — WHATEVER IT ACTUALLY WAS.

    The amount comes from `ledger.debited_groth` (the `schedule` entry plus its bridge-fee half),
    never from a recomputed amount + fee: a refund is the mirror of a debit that landed, and only
    the ledger knows which halves landed. Nothing debited means nothing to give back — refunding
    against a debit that is not there would MINT money. Idempotent: a second cancel for a ref is
    refused by design."""
    total = await ledger.debited_groth(rid)
    if not total:
        return
    try:
        await ledger.cancel(aid, asset, total, rid, why, refund_of=f"schedule:{rid}")
    except ledger.AlreadyRefunded:
        pass


async def _write_items(
    aid: str,
    asset: str,
    mode: str,
    items: list[dict[str, Any]],
    relayer_fee_groth: int,
    dest_head: int = 0,
) -> list[str]:
    """The debit FIRST, then the row it belongs to — and THE WHOLE BATCH OR NONE OF IT.

    Two laws meet here. (1) A request row can never exist without the ledger entry that paid for
    it, so the `schedule` entry is appended first and cancel always has its evidence. (2) A batch
    is admitted or refused as a unit. This loop used to compensate only the item it failed on and
    re-raise: items 1..k−1 kept both their debit and their live `scheduled` row, the caller got a
    500 with no ids and no event at all, and an ordinary retry-on-500 paid the same address a
    second time. ANY failure now reverses everything this call wrote before the error leaves.

    THE DEBIT IS THE NUMBER THE USER WAS QUOTED. `items` are the dicts `price_item` returned —
    the delivery, our fee and the bridge fee, all priced once, at the top of `create` — so the
    row, the ledger and the answer cannot disagree about what this order cost. The bridge half of
    the debit is its own ledger kind (`ledger.schedule`), and the row records the total it added
    up to as evidence an audit can check without redoing the multiplication.

    ⛔ THE TWO HALVES SUM TO WHAT WAS DEBITED, IN BOTH FEE MODES. `schedule` takes (delivered +
    our fee) and its bridge-fee half takes the crossing, and `price_item` guarantees those three
    parts are exactly `debited_groth` — so a from-amount order debits the AMOUNT the user typed
    (nothing on top of it) and an on-top order debits amount + fee + bridge, through one call.

    ⛔ AN INSERT THAT RAISED IS NOT AN INSERT THAT DID NOT LAND. A socket timeout or a primary
    step-down can apply the write and still raise at the driver, so the compensation ASKS whether
    the row is there before it refunds. Refunding a row that stands leaves an order with no debit
    — which `payouts._payout_scheduled` releases for free, and whose `ledger.release` then debits
    a `scheduled` bucket that no longer holds it."""
    now = time.time()
    ids: list[str] = []
    try:
        for i in items:
            rid = secrets.token_hex(12)
            await ledger.schedule(
                aid,
                asset,
                int(i["delivered_groth"]) + int(i["fee_groth"]),
                rid,
                f"{mode} payout scheduled",
                bridge_fee_groth=int(i["bridge_fee_groth"]),
            )
            row = {
                "_id": rid,
                "account_id": aid,
                "asset": asset,
                "mode": mode,
                "W": i["W"],
                # ⛔ WHAT LEAVES TO THE USER — the DELIVERED amount, not what they typed. Every
                # reader of this field spends it: `payouts._payout_scheduled` sends it over the
                # bridge, `_book_release` clears it out of `scheduled`, the delivery check
                # multiplies it by the asset grid to look for it on Ethereum. Under the
                # from-amount mode the two differ, and a row that stored the REQUESTED amount
                # here would send fee + bridge more than the ledger ever debited. What the user
                # asked for is `requested_groth`, beside it, for the answer and the UI.
                "amount_groth": int(i["delivered_groth"]),
                "delivered_groth": int(i["delivered_groth"]),
                "requested_groth": int(i["amount_groth"]),
                # which side of the amount the fees came from, ON the row: rows are never
                # rewritten, so this is how a reader tells this order from one written under the
                # fees-always-on-top rule (`row_fee_mode` resolves a missing one to `on_top`).
                "fee_mode": i["fee_mode"],
                # OURS ONLY. The bridge's cut is `bridge_fee_groth` and the release-time subsidy
                # gate reads THAT — `fee_groth` is revenue and has never been a budget for a
                # crossing since the bridge fee became an itemised charge.
                "fee_groth": int(i["fee_groth"]),
                "bridge_fee_groth": int(i["bridge_fee_groth"]),
                # what the ledger took out of Available for this order, recorded so an audit can
                # check delivered + fee + bridge_fee against the entries without re-deriving it
                "total_debited_groth": int(i["debited_groth"]),
                "debited_groth": int(i["debited_groth"]),
                "deliver_at": i["deliver_at"],
                "release_at": i["release_at"],
                # what the relayer wanted when the order was accepted: the release reads its OWN
                # live number, so this is evidence of what we priced, never an input to the send
                "relayer_fee_groth_estimate": int(relayer_fee_groth),
                "min_amount_groth": int(i["min_amount_groth"]),
                # the block W was proven to carry no contract code at, and when. The guard runs
                # only here, so this is the evidence a release-time re-check would compare to.
                "dest_checked_head": int(dest_head),
                "dest_checked_at": now,
                "status": "scheduled",
                "dest_chain": settings.eth_chain_id,
                "created_at": now,
                "updated_at": now,
            }
            if i.get("fee_note"):
                row["fee_note"] = str(i["fee_note"])
            try:
                await db().payout_requests.insert_one(row)
            except Exception:  # noqa: BLE001 — ask before you refund; never refund on a guess
                try:
                    landed = await db().payout_requests.find_one({"_id": rid}, {"_id": 1})
                except Exception:  # noqa: BLE001 — we cannot even ask: the debit STAYS with it
                    ids.append(rid)
                    raise
                if landed is None:
                    await _void(
                        aid,
                        asset,
                        rid,
                        "the payout request row could not be written; the debit is reversed",
                    )
                    raise
                # the row IS there: the write landed and only the answer was lost.
            ids.append(rid)
    except Exception as e:  # noqa: BLE001 — reverse the batch, then say what still stands
        standing = await _roll_back(aid, asset, ids, "the batch could not be written in full")
        raise PartialBatch(standing) from e
    return ids


async def _roll_back(aid: str, asset: str, ids: list[str], why: str) -> list[str]:
    """Undo a withdrawal that must not stand — with offsetting entries, never by deleting.

    Returns the ids it could NOT reverse. A row that has already moved past `scheduled` is a
    payout the worker has claimed and may have in flight; telling the user "nothing moved" about
    it would be a lie, so it is named, and the operator is paged with the count.

    ⛔ THIS FUNCTION NEVER RAISES. It is called on rows that are already live, where an exception
    is a 500 that hides the ids and invites a duplicating retry — so EVERY failure it meets is
    reported by NAMING that id in `standing`, and the whole per-id reversal (not just the refund)
    is guarded, so one id whose row or entry could not even be read does not abandon the rest."""
    standing: list[str] = []
    for rid in ids:
        try:
            row = await db().payout_requests.find_one_and_update(
                {"_id": rid, "status": "scheduled"},
                {
                    "$set": {
                        "status": "cancelled",
                        "cancelled_at": time.time(),
                        "rolled_back": True,
                        "roll_back_reason": why,
                    }
                },
            )
            debited = await ledger.debited_groth(rid)  # amount + our fee + the bridge fee
            refunded = await ledger.find_entry("cancel", rid)
            if not row:
                if debited and not refunded:  # it exists, it is debited, and we could not reverse it
                    standing.append(rid)
                continue
            if not debited:
                continue  # nothing was ever debited for it: refunding would MINT money
            try:
                await ledger.cancel(
                    aid,
                    asset,
                    debited,
                    rid,
                    f"rolled back: {why}",
                    refund_of=f"schedule:{rid}",
                )
            except ledger.AlreadyRefunded:
                pass
        except Exception:  # noqa: BLE001 — one failed reversal must not abandon the others
            log.exception("withdrawal roll back: %s could not be reversed", rid)
            standing.append(rid)
    await _page_operator(
        "withdrawal_rolled_back",
        f"ROLLED BACK: a withdrawal was reversed ({why})"
        + (
            f" — {len(standing)} order(s) could NOT be reversed and STILL STAND"
            if standing
            else " — every order was reversed"
        )
        + "; check the ledger for this account",
        ids,
    )
    return standing


# ------------------------------------------------------------------------------------- the routes


def _refused(status: int, detail: Any, *, aid: str, why: str, headers: Any = None) -> HTTPException:
    """The refusal, and the row that says it happened. `raise _refused(...)`.

    Law 12: refusals are not trades and not failures — but every decision path must write one.
    A refusal that only renders as an HTTP body is invisible the moment the caller closes the
    tab, and a rule nobody can see firing is how a $250 cap ran for 23.5 hours and cost $383.
    §9.7: the account, the status and the REASON — never `W`, and never the amounts beside it,
    because api.log already carries the request line and pairing those is the product defeated."""
    log.info("withdrawal refused %s for %s: %s", status, aid, why)
    return HTTPException(status, detail, headers=headers)


def _batch_gates(body: WithdrawIn | PreviewIn, aid: str) -> str:
    """The rules that are about the REQUEST, not about any one item, in the order they fail.

    Shared by `create` and `preview` so a quote can never be given for a batch a withdrawal
    would refuse outright — the asset, the mode and the size of the list. What `preview` does
    NOT share is the arming check: it writes nothing and promises nothing, so a deployment
    whose payouts are still dark can still price a form (`GET /v1/account.modes` is what says
    whether the button works). Returns the normalised asset key."""
    asset = body.asset.upper()
    if asset not in ASSETS:
        raise _refused(400, f"unknown asset {body.asset!r}", aid=aid, why=f"unknown asset {asset!r}")
    if body.mode not in MODES:
        raise _refused(400, "mode must be direct or instant", aid=aid, why=f"mode {body.mode!r}")
    if asset not in PAYOUT_ASSETS:
        # the executor refuses this row forever ("v1 payouts are ETH only"), so admitting it
        # would move the user's balance into `scheduled` for an order that can never execute.
        raise _refused(
            400,
            f"v1 payouts are {'/'.join(PAYOUT_ASSETS)} only — a {asset} withdrawal cannot be "
            f"executed by this deployment, so nothing was scheduled",
            aid=aid,
            why=f"{asset} is not a payable asset",
        )
    if len(body.items) > settings.max_items_per_withdrawal:
        raise _refused(
            400,
            f"at most {settings.max_items_per_withdrawal} wallets per withdrawal",
            aid=aid,
            why=f"{len(body.items)} items, max {settings.max_items_per_withdrawal}",
        )
    return asset


def _refuse_items(priced: list[dict[str, Any]], aid: str, min_amount: int) -> None:
    """422 naming EVERY item that cannot be scheduled, or nothing at all.

    One bad row in a batch of fifty used to be a 400 with one sentence and no way to tell which
    row it was about. The whole batch is priced first (`price_items` never raises), so the answer
    can name each failing item by its position, in the same shape `preview` returns — the client
    does not need a second code path to render a refusal.

    ⛔ EXCEPT THE ROW THAT RAN OUT OF MONEY. `BATCH_CODE` is a verdict about the LIST — the row
    where the running total happens to cross is arithmetic, not a mistake the user made in that
    row — so it is left to the batch rule, which refuses it as one unit with the shortfall in the
    sentence and in `X-Shortfall-Groth`. Reported here it would be a 422 telling the user their
    third wallet is wrong when the only thing wrong is how much money is in the account.

    ⛔ AND EXCEPT `TREASURY_FLOAT_CODE`, for a stronger version of the same reason: that row is
    not wrong at all, WE are short. It is refused by `treasury_verdict` with one sentence about
    the whole list (T52)."""
    bad = [
        (n, i)
        for n, i in enumerate(priced)
        if not i["ok"] and i.get("problem_code") not in (BATCH_CODE, TREASURY_FLOAT_CODE)
    ]
    if not bad:
        return
    problems = "; ".join(f"item {n + 1}: {i['problem']}" for n, i in bad)
    raise _refused(
        422,
        {
            "message": (
                f"{len(bad)} of {len(priced)} item(s) cannot be scheduled ({problems}) — "
                f"nothing was scheduled and nothing was debited"
            ),
            "items": priced,
            "min_amount_groth": min_amount,
        },
        aid=aid,
        # ⛔ CODES, NOT SENTENCES. The address-shaped problems quote the address the user typed,
        # and api.log already carries the request line beside the account — §9.7: an operator log
        # that pairs an account with a destination is the whole product defeated.
        why=f"{len(bad)}/{len(priced)} items refused: "
        + ", ".join(f"item {n + 1} {i['problem_code']}" for n, i in bad),
    )


@router.get("/fees")
async def fees(asset: str = "ETH", acct=auth.Account):
    """What the form has to know before it can be filled in: our cut, the bridge fee the user
    pays at cost for a crossing that leaves now, how much more a wait adds to it, the smallest
    payout this deployment accepts, and how early an order has to leave to arrive on time."""
    try:
        a = get_asset(asset)
    except KeyError as e:
        raise HTTPException(400, str(e.args[0])) from e
    return await live_fees(a)


async def treasury_now(mode: str, asset: Asset) -> dict[str, Any] | None:
    """The float schedule this request is priced against, or None when it is not measurable.

    ONLY the crossing lane: an instant payout is paid out of our Ethereum float and never touches
    the Beam treasury, so gating it on the treasury's spendable buckets would refuse orders for a
    shortage that has nothing to do with them.

    ⚠️ It is a READ on the request path, so it carries a freshness bound (`FLOAT_SCHEDULE_TTL_S`)
    — a form being typed into must not cost one wallet read per keystroke, and a number 30 s old
    is the same number: the max-privacy lock moves in days."""
    if mode != "direct":
        return None
    return await payouts.float_schedule(asset, max_age_s=payouts.FLOAT_SCHEDULE_TTL_S)


@router.post("/preview")
async def preview(body: PreviewIn, acct=auth.Account):
    """What this batch would cost, item by item — A READ. Nothing is written and nothing is held.

    The UI shows THESE numbers and does no fee arithmetic of its own: `create` prices the same
    items with the same call, so the only way the two can differ is the gas price moving between
    the two requests (which is a real thing that happens, and why the answer is quoted from a
    measurement ≤ 60 s old rather than from a constant).

    `available_groth` is read here too, and `batch` is the VERDICT on it — the same function
    `create` refuses with, so a form marks an unaffordable batch in the words the refusal would
    use instead of re-deriving the rule. It is NOT a reservation: the batch is admitted or
    refused atomically in `create`, against Available as it is at that moment, and a preview
    that fits is not a promise."""
    aid = acct["account_id"]
    asset = _batch_gates(body, aid)
    a = get_asset(asset)
    live = await live_fees(a)  # 503 if the gas price is unreadable — never a guessed fee
    # ⛔ AVAILABLE IS AN INPUT TO THE PRICING NOW, NOT A NUMBER RENDERED BESIDE IT. Where each
    # row's fees come from depends on what the rows above it left, so it is read BEFORE the
    # items are priced and the same reading rules on the batch — one balance, one verdict. Read
    # afterwards it would price against a balance the verdict never saw.
    available = (await ledger.balance(aid, asset))["available"]
    now = time.time()
    relayer_now = int(live["relayer_fee_groth_now"])
    # …and what the TREASURY can move today, which is the other half of "may this be scheduled"
    # (T52). Read before the pricing, because it is folded through the rows in the same order.
    schedule = await treasury_now(body.mode, a)
    priced = price_items(
        body.items,
        asset=a,
        mode=body.mode,
        now=now,
        relayer_fee_groth=relayer_now,
        available=available,
        treasury=schedule,
    )
    totals = totals_of(priced)
    # …and the top-up that WOULD make it fit, priced against the balance it would then have —
    # the same fixed point `create` publishes in `X-Shortfall-Groth`, so a form that shows the
    # user what to credit shows the number the refusal would have given them.
    top_up = top_up_needed(
        body.items,
        priced,
        asset=a,
        mode=body.mode,
        now=now,
        relayer_fee_groth=relayer_now,
        available=available,
    )
    return {
        "items": priced,
        "totals": totals,
        # the batch rule with its own verdict, from the same call `create` refuses on: a client
        # renders `batch.problem` and disables its button on `batch.ok` instead of re-deriving
        # "Σ total ≤ Available" from the two numbers below it
        "batch": batch_verdict(totals, asset, available, shortfall=top_up),
        # …and the treasury's own verdict beside it: how much of this the wallet can move TODAY,
        # what it could deliver right now if this list were smaller, and when the locked rest
        # comes back. `create` refuses on this one, so the form shows the sentence it would get.
        "treasury": treasury_verdict(
            priced, schedule, int(live["bridge_fee_groth_now"]), asset=a
        ),
        "available_groth": available,
        "min_amount_groth": int(live["min_amount_groth"]),
        "fee_bps": settings.fee_bps,
        "bridge_eta_s": eta_for(body.mode),
    }


@router.post("")
async def create(body: WithdrawIn, acct=auth.Account):
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    aid = acct["account_id"]
    asset = _batch_gates(body, aid)
    enabled = (
        settings.payout_direct_enabled if body.mode == "direct" else settings.payout_instant_enabled
    )
    if not enabled:
        raise _refused(
            409,
            f"{body.mode} payouts are not enabled on this deployment yet"
            + (
                " (no float / distributors)"
                if body.mode == "instant"
                else " (no Beam treasury wallet)"
            ),
            aid=aid,
            why=f"{body.mode} payouts are not enabled",
        )

    now = time.time()
    eta_s = eta_for(body.mode)
    live = await live_fees(get_asset(asset))
    relayer_now = int(live["relayer_fee_groth_now"])
    min_amount = int(live["min_amount_groth"])
    # the balance is read ONCE and it is an INPUT to the pricing (where each row's fees come
    # from depends on what the rows above it left), then the SAME reading rules on the batch.
    # It is a quote, not a claim: `reserve` below decides atomically against Available as it is
    # at that moment, which is what makes two batches arriving together safe.
    available = (await ledger.balance(aid, asset))["available"]
    schedule = await treasury_now(body.mode, get_asset(asset))
    # ONE pricing call, and the batch is refused as a unit on what it says. Every number below —
    # what the row stores, what the ledger debits, what the answer reports — comes from here.
    items = price_items(
        body.items,
        asset=get_asset(asset),
        mode=body.mode,
        now=now,
        relayer_fee_groth=relayer_now,
        available=available,
        treasury=schedule,
    )
    _refuse_items(items, aid, min_amount)
    # ⛔ **WHAT THE TREASURY CANNOT DELIVER TODAY IS REFUSED TODAY** (T52, and no accept-anyway).
    # It is asked BEFORE the chain read and before the reservation: a refusal that costs the user
    # nothing should cost us nothing either, and nothing here has touched their balance yet.
    #
    # It goes ahead of the Available shortfall on purpose. Both can be true at once, and only one
    # of them can be acted on by the person reading it: topping up does not make a locked
    # treasury liquid, so telling them to top up first would earn them a second refusal.
    treasury = treasury_verdict(items, schedule, int(live["bridge_fee_groth_now"]), asset=get_asset(asset))
    if not treasury["ok"]:
        raise _refused(
            409,
            treasury["problem"],
            aid=aid,
            why=(
                f"the treasury can move {treasury['float_now_groth']} groth and this batch needs "
                f"more; next unlock {treasury['next_unlock_at']}"
            ),
        )
    # the chain is asked BEFORE a groth is reserved: a contract in the list refuses the batch
    dest_head, _dest_url = await refuse_contracts([i["W"] for i in items])

    # THE ONLY REMAINING BATCH RULE: Σ total ≤ Available, decided atomically by the reservation
    # and STATED by the same function the preview showed the user (`batch_verdict`) — one
    # summation, one sentence, one shortfall, whichever end of the request they arrive at.
    totals = totals_of(items)
    verdict = batch_verdict(
        totals,
        asset,
        available,
        # the fixed point, not the gap: crediting this makes every row's fees ride on top, and a
        # number that gets the user refused a second time is worse than no number at all (T35b)
        shortfall=top_up_needed(
            body.items,
            items,
            asset=get_asset(asset),
            mode=body.mode,
            now=now,
            relayer_fee_groth=relayer_now,
            available=available,
        ),
    )
    need = int(verdict["need_groth"])
    # a row that could not be covered at all (`BATCH_CODE`) is the batch running out of money, and
    # `_refuse_items` deliberately let it through to be refused HERE, as one unit. The verdict
    # always agrees — a short row's quote alone already pushes `need` past Available — and the
    # second half of this condition is the belt that says so out loud: NO BATCH CONTAINING A ROW
    # THAT CANNOT BE PAID IS EVER WRITTEN, whatever the summation says (law 10: every guard needs
    # the level it protects).
    short_rows = [n for n, i in enumerate(items) if i.get("problem_code") == BATCH_CODE]
    if not verdict["ok"] or short_rows:
        shortfall = int(verdict["shortfall_groth"])
        raise _refused(
            409,
            batch_problem(asset, need, available, shortfall)
            if shortfall
            else "; ".join(f"item {n + 1}: {items[n]['problem']}" for n in short_rows),
            aid=aid,
            why=f"short {shortfall} groth of {need} against {available} available"
            + (f" (rows {[n + 1 for n in short_rows]})" if short_rows else ""),
            # ⛔ A HEADER ONLY WHEN THERE IS A SHORTFALL TO PUT IN IT. `X-Shortfall-Groth: 0`
            # is not a shortfall, it is a client being told to parse a number that means nothing.
            headers={"X-Shortfall-Groth": str(shortfall)} if shortfall else None,
        )
    if not await reserve(aid, asset, need):
        # THE BATCH FITS AND THE CLAIM STILL FAILED, so this is NOT "insufficient balance": the
        # verdict above was computed against the same Available and said `ok`. What `reserve`
        # refuses on is the cushion — another batch of this same account is being written right
        # now and has claimed the room. Answering the shortfall sentence here told the user to
        # top up an account that has the money, with a `X-Shortfall-Groth: 0` header they could
        # not act on. The cause is named, the header is omitted, and retrying is the fix.
        raise _refused(
            409,
            INFLIGHT_PROBLEM,
            aid=aid,
            why=f"the {asset} reservation refused {need} groth: another batch is in flight",
        )
    try:
        try:
            ids = await _write_items(aid, asset, body.mode, items, relayer_now, dest_head)
        finally:
            try:
                await release(aid, asset, need)
            except Exception:  # noqa: BLE001 — the cushion is not the money and not the caller's
                # a cushion that could not be given back is repaired by the stale-clear in
                # `reserve` (RESERVATION_STALE_S); raising here would replace a written batch's
                # 200 with a 500 that hides its ids. Logged, never paged: law 15, a monitor that
                # pages about a self-healing counter trains the operator to ignore the pager.
                log.exception("withdrawal: the %s reservation of %s could not be released", asset, aid)
    except PartialBatch as e:
        # the caller must never see a 5xx while an order of theirs is live: either the batch
        # reversed itself (retrying is safe) or it did not, and then the ids that stand are named.
        if e.standing:
            raise HTTPException(
                500,
                f"the withdrawal could not be written in full and {len(e.standing)} order(s) "
                f"could not be reversed ({', '.join(e.standing)}) — the operator has been paged; "
                f"check /v1/account before retrying",
            ) from e
        raise HTTPException(
            503,
            "the withdrawal could not be written and was reversed — nothing was scheduled and "
            "nothing was debited; try again in a moment",
        ) from e
    # ══════════════════ THE ORDERS ARE LIVE AND DEBITED FROM HERE DOWN ══════════════════
    # ⛔ NOTHING BELOW THIS LINE MAY TURN INTO A 5xx. The answer is built FIRST, so every path
    # from here has the ids in its hand: a 500 would tell the caller the withdrawal failed while
    # the worker pays it, and withhold the only ids they could cancel with — an ordinary
    # retry-on-500 then schedules the whole batch a second time. The belt below may still
    # REVERSE the batch (409, naming what it reversed); anything that merely fails is logged,
    # paged and swallowed, and this answer leaves anyway.
    out = {
        "request_ids": ids,
        "fee_groth": totals["fee_groth"],
        "bridge_fee_groth": totals["bridge_fee_groth"],
        # what the wallets actually receive, beside what the account was charged: the two differ
        # by whatever the from-amount rows gave up, and a client must never have to subtract.
        "delivered_groth": totals["delivered_groth"],
        "total_debited_groth": totals["total_debited_groth"],
        "totals": totals,
        "relayer_fee_groth_estimate": relayer_now,
        "min_amount_groth": min_amount,
        "bridge_eta_s": eta_s,
        "items": [
            {
                "request_id": rid,
                "W": i["W"],
                # `amount_groth` is what was REQUESTED here (it always has been, and a client
                # matches its rows on it); `delivered_groth` is what that wallet receives.
                "amount_groth": i["amount_groth"],
                "requested_groth": i["requested_groth"],
                "delivered_groth": i["delivered_groth"],
                "fee_groth": i["fee_groth"],
                "bridge_fee_groth": i["bridge_fee_groth"],
                "total_groth": i["total_groth"],
                "debited_groth": i["debited_groth"],
                "fee_mode": i["fee_mode"],
                **({"fee_note": i["fee_note"]} if i.get("fee_note") else {}),
                "deliver_at": i["deliver_at"],
                "release_at": i["release_at"],
                "min_amount_groth": i["min_amount_groth"],
            }
            for rid, i in zip(ids, items, strict=True)
        ],
    }
    after: int | None
    try:
        after = (await ledger.balance(aid, asset))["available"]
    except Exception:  # noqa: BLE001 — AN UNREADABLE BALANCE IS NOT A NEGATIVE BALANCE (law 8)
        # The belt did not run, so we cannot claim the account is sound — but we also cannot
        # reverse a correctly-written batch on a reading we never got. `None` is "we do not
        # know", which is explicitly not "< 0": say so loudly, and carry on writing the events
        # these live orders are owed. The operator (and the reconciler) decide the rest.
        after = None
        log.exception("withdrawal %s: the post-write balance could not be read", ",".join(ids))
        await _page_operator(
            "withdrawal_belt_unread",
            f"BELT DID NOT RUN: {len(ids)} {asset} payout order(s) were written and debited and "
            f"the post-write balance could NOT be read, so the overdraft check never happened — "
            f"the orders stand and the caller has their ids; check this account",
            ids,
        )
    if after is not None and after < 0:  # belt: nothing may leave the account overdrawn, ever
        # A REFUSAL, NOT A FAILURE: reverse what we just wrote and answer 409 naming BOTH sets —
        # the ids that were reversed (retrying is safe for those) and any that still stand.
        standing = await _roll_back(aid, asset, ids, "the balance would have gone negative")
        reversed_ids = [r for r in ids if r not in standing]
        detail = f"insufficient {asset} balance — the withdrawal was rolled back"
        if reversed_ids:
            detail += f" ({len(reversed_ids)} order(s) reversed: {', '.join(reversed_ids)})"
        detail += (
            f", but {len(standing)} order(s) had already been claimed for release and still "
            f"stand ({', '.join(standing)}); the operator has been paged"
            if standing
            else " and nothing moved"
        )
        raise HTTPException(409, detail, headers={"X-Reversed-Request-Ids": ",".join(reversed_ids)})
    failed_events: list[str] = []
    for rid, i in zip(ids, items, strict=True):
        # §9.7: ids only — the request id, never W. api.log otherwise pairs request_id →
        # destination and payout_requests pairs request_id → account_id, which is the whole
        # product defeated. ONE event per order, so the operator's log has one row per order.
        # Guarded PER ITEM: an event that will not write is an observability failure, never a
        # failed payout — and one that fails must not swallow the events of the orders after it.
        try:
            await tg.queue(
                "withdrawal_requested",
                # the DELIVERED amount, because that is what leaves — plus the mode, so an
                # operator reading the log can tell an order that shrank from one that did not
                f"Payout scheduled: {asset} {tg.fmt_groth(i['delivered_groth'])} "
                f"(fee {tg.fmt_groth(i['fee_groth'])}, bridge "
                f"{tg.fmt_groth(i['bridge_fee_groth'])}, {i['fee_mode']}), releases in "
                f"{max(0, int(i['release_at'] - now))}s",
                request_id=rid,
                # ⛔ THE LANE TRAVELS WITH THE EVENT (T47, wired 2026-09-10). `tg.rung` renders
                # `[k/N]` and switches to the 3-rung instant ladder on the event's own `mode`;
                # with none, an instant order was numbered against the 5-rung crossing ladder.
                mode=body.mode,
            )
        except Exception:  # noqa: BLE001 — law 11: a guard that fails politely is a guard that fails
            log.exception("withdrawal %s: its `withdrawal_requested` event could not be written", rid)
            failed_events.append(rid)
    if failed_events:
        await _page_operator(
            "withdrawal_events_unwritten",
            f"EVENT MISSING: {len(failed_events)} of {len(ids)} scheduled {asset} payout order(s) "
            f"could not write their `withdrawal_requested` event — the orders ARE live and the "
            f"caller has their ids; the operator log is the thing that is short",
            failed_events,
        )
    return out


@router.post("/{request_id}/cancel")
async def cancel(request_id: str, acct=auth.Account):
    """Cancel an order the user may still end, and give its debit back.

    ⛔ ONE READER DECIDES, AND IT IS `payouts.cancellable` (T35b, law 9). This route used to
    filter on `{status: "scheduled"}` and compose its own refusal, while `payouts.CANCELLABLE`
    is `(scheduled, delayed, held)` and `GET /v1/account` publishes `cancellable: true` for
    delayed and held rows from that same function — so the button the page drew and the answer
    this route gave were two sentences about one fact, and the one the user pressed lost. The
    verdict and its `why` both come from there now; the flip is conditional on the SAME facts
    (`cancel_claim`), so an order the processor released between the read and the update matches
    nothing and is refused rather than refunded twice.

    ⛔ REPAIRABLE, NOT TERMINAL. The row is flipped to `cancelled` first and the refund appended
    second, so a failure between the two left the row terminal (the worker only releases
    `scheduled`), the retry answering 409 "request is cancelled", the groth frozen in
    `scheduled` forever and NOTHING paged. A `cancelled` row whose `schedule` entry has no
    matching `cancel` entry is therefore an UNFINISHED cancel, and it re-enters the refund path
    here — the same doctrine `ledger.release` states for its own two halves: a partial write is
    repaired by the next call instead of being declared finished."""
    if workers.paused():
        raise HTTPException(409, workers.PAUSED_REASON)
    aid = acct["account_id"]
    now = time.time()
    existing = await db().payout_requests.find_one({"_id": request_id, "account_id": aid})
    if not existing:
        raise HTTPException(404, "unknown request")
    may, why = payouts.cancellable(existing)
    if may:
        row = await db().payout_requests.find_one_and_update(
            {"_id": request_id, "account_id": aid, **cancel_claim()},
            {"$set": {"status": "cancelled", "cancelled_at": now, "updated_at": now}},
        )
        if row is None:
            # THE RACE, REFUSED: the read said yes and the claim found the order already gone —
            # released, signed, or cancelled by a request arriving with this one. Re-read it and
            # let the SAME reader say why, so the sentence is never one this route invented.
            fresh = await db().payout_requests.find_one({"_id": request_id, "account_id": aid})
            _again, moved = payouts.cancellable(fresh or {"status": "gone"})
            raise _refused(
                409,
                moved or "this order moved while it was being cancelled — nothing was changed",
                aid=aid,
                why=f"cancel raced: {request_id} advanced between the read and the claim",
            )
    else:
        # the flip landed and the refund did not: that is not a refusal, it is an unfinished
        # cancel, and it is finished here (see the doctrine above).
        unfinished = existing.get("status") == "cancelled" and not await ledger.find_entry(
            "cancel", request_id
        )
        if not unfinished:
            # §9.7: the status and the FIELD NAME that blocked it — never the sentence (which
            # quotes nothing here, but this line is the one an operator reads, and "not
            # cancellable: 'delayed'" reads as a contradiction when `delayed` is cancellable).
            # Names of fields, never their values: a txid is evidence, not a log line.
            blocked = [f for f in CANCEL_BLOCKERS if existing.get(f)]
            raise _refused(
                409,
                why,
                aid=aid,
                why=f"not cancellable: status {existing.get('status')!r}"
                + (f", blocked by {'+'.join(blocked)}" if blocked else ""),
            )
        row = existing
    # WHAT WAS DEBITED, NOT WHAT IT SHOULD HAVE COST: amount + our fee + the bridge fee, read
    # from the entries themselves through the one helper every refund path uses.
    total = await ledger.debited_groth(request_id)
    if not total:
        # the row exists but nothing was ever debited for it: refunding would MINT money
        await tg.alert(
            "withdrawal_cancel_no_debit",
            "CANCELLED WITHOUT A REFUND: a scheduled payout had no `schedule` ledger entry, so "
            "nothing was debited and nothing was returned — investigate this row",
            request_id=request_id,
        )
        return {"cancelled": request_id, "refunded_groth": 0}
    try:
        await ledger.cancel(
            aid,
            row["asset"],
            total,
            request_id,
            "cancelled by the user before release",
            refund_of=f"schedule:{request_id}",
        )
    except ledger.AlreadyRefunded:
        return {"cancelled": request_id, "refunded_groth": 0}
    except Exception as e:  # noqa: BLE001 — a frozen balance is not allowed to be silent
        await tg.alert(
            "withdrawal_cancel_refund_failed",
            "CANCELLED WITHOUT ITS REFUND: the request row is `cancelled` and the offsetting "
            "ledger entry could NOT be written, so the groth is stuck in `scheduled` — the next "
            "cancel of this id completes it; check this account",
            request_id=request_id,
        )
        raise HTTPException(
            503,
            "the request is cancelled but the refund could not be written — nothing is lost; "
            "call cancel again in a moment and it will be completed",
        ) from e
    await tg.queue(
        "withdrawal_cancelled",
        f"Withdrawal cancelled: {row['asset']} {tg.fmt_groth(total)} back to Available",
        request_id=request_id,
    )
    return {"cancelled": request_id, "refunded_groth": total}
