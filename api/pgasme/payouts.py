"""Order processors — one loop, handlers keyed by status, every order carries its own step.

Admin's design: "each user order is an order and it should have status of each step where the
processor understands what to do — execute/wait — until it's completed."

  PAYOUT (`payout_requests.status`), v1 = ETH only, bETH → ETH straight to the user's W:

    scheduled ──release_at passed, float ≥ amount+fee, the WALLET can spend it from ONE
      │            source, a free coin for it and for its BEAM fee, flag on──▶ releasing ──▶ bridging
      │  waits: too early · float short · no single source covers it · the wallet can spend
      │         neither bucket (max-privacy lock) · wallet_status unreadable · relayer fee >
      │         the bridge fee this order funded × the subsidy · relayer share > 10% (legacy
      │         rows) · unshielded balance present (§9.3, RETIRED under
      │         PGAS_PAYOUT_SPEND_UNSHIELDED) · PGAS_PAYOUT_DIRECT_ENABLED=0
      │  defers (no row, no page): no free coin this pass · the coin list unreadable
      │                                       (intent written BEFORE the send; never re-signed)
    bridging ──Beam kernel confirmed AND our outgoing pipe message matched by identity
      │        (receiver + amount) ──▶ ledger release booked ──▶ 61 Beam confirmations
      │        ──▶ delivering ──W UP by exactly the amount and the pipe DOWN in the SAME
      │            Ethereum block, and that (block, W, amount) not already consumed──▶ sent
      ├── the Beam tx failed/cancelled ──▶ failed(reason) + the schedule debit refunded
      └── the response was lost and nothing identifiable turned up ──▶ held (a HUMAN owns it)

  ANY-ASSET (v1.1, designed, DARK): scheduled ─▶ waiting_for_dep_eth ─▶
  waiting_for_swap_to_target_asset ─▶ sent — statuses exist, handlers REFUSE.

  DEPOSIT TREASURY (`deposits.treasury`, a SUB-machine: `deposits.status` stays `credited`, so
  the user-facing contract is untouched):

    (none) ──▶ claiming ──msgId in view_incoming → receive → claim_txid──▶ claimed(kernel)
           ──▶ shielding ──one max-privacy self-send per denomination chunk, each to its
           │              OWN fresh max-privacy address──▶ shielded
           ├── the claim response was lost and the message is gone ──▶ held
           └── a chunk's send FAILED on Beam ──▶ held (a HUMAN owns it; the row names the
               `python -m pgasme.beam replan-shield --deposit <id>` that resolves it, and
               --apply hands the failed chunks back as unsent — never the machine's own retry)

Nothing here spends while its flag is 0: the handler logs what it WOULD do, marks the row
`dark`, and pages once an hour that treasury work is waiting. Every irreversible call re-checks
the kill switch inside the mover (`beam.Wallet.submit`), every refusal writes its reason on the
row, and every transition emits exactly ONE operator event.

⛔ **BeamPay is the ONLY interface to the Beam wallet** (operating law 10). Every balance,
every transaction status, every address and the shield itself are BeamPay's; `pgasme/beam.py`
is called for exactly two things BeamPay has no endpoint for — `invoke_contract` with
`create_tx:false` and `process_invoke_data`. And **every contract txid those produce is
registered with BeamPay in the same processor step, before the row advances**
(`POST /internal/expect_contract_tx`): an unregistered contract flow books to the synthetic
`__house__` account, so the treasury's balance never moves and the float we gate on is a lie.

Six structural rules this module now enforces, each of which was absent and each of which is
a way real money is lost:

  1. **Identity, never presence.** A lost response is resolved by finding a transaction whose
     SIGNED amount is this crossing's and whose message is ours; a claim asks whether the
     message is still claimable BEFORE it looks at any transaction.
  2. **A hold a human owns cannot un-hold itself.** The unresolved timeout moves the row to a
     status whose handler does not resolve — the reference (`bridge_watcher`) advances to
     `on_hold`; copying only its message left the row armed to adopt a stranger's transaction
     hours later.
  3. **One conditional update before every irreversible call**, on the claim and on every
     shield chunk exactly as `_advance` already does for the payout — `--workers 1` in a unit
     file is not code, and a second loop signed the same inventory twice.
  4. **A delivery is consumed once.** (pipe, block, W, amount) is claimed by a unique insert;
     two payouts of one denomination to one wallet cannot both settle on one crossing.
  5. **A guard does not refresh its own deadline.** Status SLAs read `status_at` / `treasury_at`
     — written only when the status CHANGES — so a 30-second checkpoint write cannot make an
     18-hour stall invisible.
  6. **A withdrawal is identified by its comment.** BeamPay's `/withdraw` is not idempotent and
     answers no txid, so a shield chunk records that it is ABOUT to call before it calls, and a
     lost answer is resolved by finding `shield|<deposit>|<k>` in `/transactions` — never by
     calling again.
  7. **One max-privacy address per shield chunk, and the float is the SUM over them.** Two
     sends to one max-privacy address in quick succession re-use its one-time voucher and build
     the identical shielded output, which the chain refuses ("Shielded outp duplicate ← Kernel
     Type 3"): that is how chunks 1 and 2 of deposit 60b0e57… died on 2026-09-09 while chunk 0
     settled. Each chunk therefore gets a FRESH `/create_wallet` address, registered in
     `mp_addresses` before anything is sent to it, and `float_groth` sums `available` over that
     registry plus the primary — an address the registry does not know is value nothing can
     measure. A chunk being re-planned after a failure gets a NEW address again, because the
     one that failed has already published its voucher.

FEES — three sources, and we set exactly one of them
---------------------------------------------------
§WE-SET-IT-WE-DONT-READ-IT was paid for on the arb stack: every number that stack *set* rather
than read was wrong, in our favour to nobody. There are three fees in this pipeline and they
have three different owners.

  1. **The b2e RELAYER fee — the only one we set**, `relayerFee=<groth>` inside the pipe
     invocation, so whatever we pass leaves our balance. It is `arb_tracker/bridge_fee.py`
     port-for-port (`beam.relayer_fee_groth`): `maxFeePerGas = 2*baseFee + clamp(median
     50th-percentile tip over 10 blocks, 0.01, 3.0 gwei)`, `fee = 120_000 * maxFeePerGas *
     ethRate / 1e9 / assetRate`, times `FEE_MARGIN = 1.5`. It is guarded at BOTH levels — a
     floor (`min_relayer_fee_units`, because too low means the message sits for days with the
     bETH already burned and no refund path) and by a ceiling that measures what the crossing
     costs against what the order FUNDED (`bridge_budget_groth × config.relayer_subsidy()` — one
     reader of each, shared with the charge at request time). Four pinned test vectors are
     `bridge_fee.py`'s OWN answers, run on the box. Since 2026-09-10 the user is CHARGED this
     fee explicitly at request time (`bridge_fee_groth` on the row, quoted through the same
     `relayer_fee_for`), so the subsidy gate measures the live fee against what THIS order
     funded instead of against our 2% — and `max_relayer_share`, the old ceiling on the relayer's
     cut of the AMOUNT, applies only to rows written before that (`funds_its_own_crossing`): with
     a 1-groth floor it would hold every small payout, each already paid for in full.
  2. **The Beam transaction fee of a contract invocation — the WALLET sets it.** Neither
     `rebal5_beth_to_eth.py:261` nor `bridge_watcher.py:320` passes a `fee` to
     `invoke_contract`, and neither reads one back; we build ours the same way (no `fee` field
     anywhere in `beam.build_receive` / `beam.build_bridge_send`), and then go one step further
     — the fee is read BACK from BeamPay once the tx is booked
     (`GET /internal/contract_tx/{txid}.fee`, the identical number BeamPay debits the
     registered address), recorded on the row, and paged above the ceiling (`fee_charged`).
  3. **The Beam transaction fee of a withdrawal — BEAMPAY sets it** (0.001 BEAM to a regular
     address, 0.011 to an offline / max-privacy one) and *ignores* the request's `fee` field
     outright (api.py:322). Our client never sends one. It too is read back, from the
     transaction BeamPay actually made, and paged above the ceiling.

The BUDGET `_beam_fees_ok` reserves out of the treasury's asset-0 balance before a pass spends
(so N calls cannot be admitted against one balance read) is therefore **derived, not declared**:
`fee_budget(kind)` = `max(floor, 1.5 × the worst of the last 10 settled transactions of that
kind)`, read back from BeamPay. ⚠️ It was a constant until 2026-09-09, and the very first live
claim paid **12,100,000 groth against a 2,000,000 constant** — 6× wrong on transaction one, on a
number nobody had ever measured. `settings.beam_claim_fee_groth` / `beam_shield_fee_groth` are
superseded and no longer read by anything on the money path. The budget reaches no wallet call.
And the BEAM the fees come out of is read from BeamPay too — never from the wallet, whose
balance is one shared UTXO pool and never our inventory.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from eth_utils import to_checksum_address
from pymongo.errors import DuplicateKeyError

from . import beam, beampay, distributor, ethpipe, ledger, receiver_keys, tg, workers
from .assets import Asset, get_asset
from .config import (
    GAS_BASIS_PERCENTILE,
    GAS_BASIS_WINDOW_S,
    GAS_SAMPLE_TTL_S,
    relayer_subsidy,
    settings,
)

# GAS_SAMPLES: the collection name lives with the module that builds its TTL index — one
# spelling of one fact, and `db` cannot import this module (it is the other way round).
from .db import GAS_SAMPLES, db

log = logging.getLogger("pgasme.payouts")

# `paying` is the INSTANT path's in-flight status (T34): the transfer is signed, its hash is on
# the row and it is waiting for a receipt. It is deliberately NOT `releasing` — `releasing`
# means "this order is holding a Beam wallet input and a BEAM fee coin", which is what
# `inflight_release_query` counts to bound concurrent bridge sends. An instant payout holds
# neither; putting it in that status would throttle the direct queue on a wallet it never touches.
PAYING = "paying"
# ⛔ **A WITHDRAWAL NEVER FAILS ON THE USER'S SIDE** (admin, 2026-09-10 11:2xZ, looking at a
# Balance page with two "Failed" rows: *"withdrawals on user's side cannot be failed"*). Every
# internal cause — a Beam send refused, a relayer-fee spike, no free coin, an unreadable wallet,
# a dropped Ethereum transaction — parks the order HERE instead: the money stays reserved, the
# row carries the reason in plain words and a `next_attempt_at`, and the processor tries again
# on a backoff ladder. `failed` is not in this module's vocabulary any anymore (there is a grep
# test); a row a human must look at is `held`, which is still reserved and still reads as
# "delayed" to the user.
DELAYED = "delayed"
# ⛔ READ-ONLY, AND THE ONLY PLACE THESE TWO WORDS APPEAR IN THIS MODULE. Nothing here writes
# them any more (there is a grep test), but rows written before 2026-09-10 carry them and are
# read for ever — never edit history to fix a ledger — and `eta_for` has to be able to tell a
# refunded order that it was refunded instead of promising it a delivery.
LEGACY_REFUNDED = ("failed", "refunded")
PAYOUT_ACTIVE = ("scheduled", DELAYED, "releasing", "bridging", "delivering", PAYING)
PAYOUT_DARK = ("waiting_for_dep_eth", "waiting_for_swap_to_target_asset")
# The retry ladder, in seconds: 1 → 2 → 5 → 15 → 60 minutes, and then 60 for ever. It is a
# LADDER and not a constant because the causes are different sizes — a coin frees up in a
# minute, a gas spike takes an hour — and it is CAPPED because an order the user has paid for
# must never fall off the end of an exponential and stop being tried at all.
DELAY_BACKOFF_S = (60.0, 120.0, 300.0, 900.0, 3600.0)
# …and after this long in `delayed`, whatever the cause, a HUMAN owns it. Still reserved, still
# "delayed" to the user, but nothing retries it any more: a machine that has been trying one
# thing for a day is not going to succeed on the next pass either.
DELAY_HOLD_AFTER_S = 24 * 3600
# The relayer's own tail on a b2e crossing, measured (§7.6: 4 h 03 m, 5 h 30 m, 18 h). It is
# what `eta_for` publishes as `eta_tail_s` beside the typical figure, so a page can say "≈ 1 h,
# up to 18 h" instead of promising an hour it cannot keep.
BRIDGE_TAIL_S = 18 * 3600
# What an INSTANT payout takes: one Ethereum transfer, signed and broadcast in the pass the
# order becomes due. A minute is the block time plus the pass interval, not a hope.
INSTANT_ETA_S = 60
# …and what is left once the Beam side is confirmed and the relayer is paying (the `delivering`
# leg): the scan finds the balance pair within a few blocks of it landing.
DELIVERING_ETA_S = 300
# The order MODES the machine can execute. `direct` and `refill` are the same bETH→ETH bridge
# crossing and take the same path — a refill just happens to pay OUR OWN distributor and belongs
# to no user (`_book_release` books nothing for it, and no ledger entry ever debited it).
# `instant` is the Ethereum-side path in `_payout_instant`. Anything else is the designed and
# unimplemented any-asset branch.
INSTANT = "instant"
REFILL = "refill"
DIRECT_MODES = ("direct", REFILL)
# A refill has no user. The id says so out loud rather than borrowing a real account's — the
# `__`-prefix is the same convention BeamPay's own `__house__` bucket uses for value that
# belongs to the system rather than to somebody.
REFILL_ACCOUNT = "__distributor__"
# ⛔ NOT active and NOT dark: a human owns these rows. No handler is registered for `held`, so
# nothing in this module can resolve, adopt or advance one. That is the whole point.
HELD = "held"
TREASURY_ACTIVE = ("claiming", "claimed", "shielding")

# A call whose response was lost is RESOLVED from the chain, never repeated. After this long
# without an identifiable transaction a human decides — a second process_invoke_data would be a
# second signature over one inventory.
UNRESOLVED_S = 15 * 60
BATCH = 50
# ⛔ THE ROWS THE HOLD COOLDOWN MAY NOT PARK (T52, 17:58Z). A crossing whose own address the
# treasury has already funded has real money sitting on it and ONE invocation left to make; it is
# looked at on EVERY pass until it moves. See `_delay`, `_due` and `_payout_delayed`.
#
# ⛔ **AND ONLY UNTIL THAT INVOCATION IS MADE.** Once a send has been signed (`release_call_at`,
# `beam_txid`) the row is in the retry ladder's territory and nowhere else: what it is waiting on
# then is a transaction that may still settle, and asking again every 30 seconds is precisely
# what `previous_attempt_dead` exists to stop being cheap.
POLL_EVERY_PASS: dict[str, Any] = {
    "fund_called_at": {"$exists": True},
    "release_call_at": {"$exists": False},
    "beam_txid": {"$exists": False},
}


def awaiting_invocation(row: dict[str, Any]) -> bool:
    """The funded-but-not-yet-sent state `POLL_EVERY_PASS` selects — ONE reader, so the query the
    pass runs and the branch each handler takes cannot describe two different rows (law 9)."""
    return bool(
        row.get("fund_called_at")
        and not row.get("release_call_at")
        and not row.get("beam_txid")
    )
# delivery detection: sample coarsely, bisect, and never spend more than this per pass
DELIVERY_STEP = 100
DELIVERY_MAX_STEPS = 24

_ARCHIVE: dict[str, str | None] = {"url": None}

# One writer per resource. `--workers 1` in deploy/pgasme-api.service is a deployment fact, not
# a guarantee: a stray `python -m pgasme`, an edited unit or a second box gives two loops over
# one wallet. The lease is the code-level answer; the conditional updates below are the one
# that still holds when it fails.
OWNER = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
LEASE_ID = "payout_processor"
# How long THIS process has been refused the lease, continuously. A restart is refused for at
# most one TTL (the dead owner's lease expires and we take it); a refusal that outlives the TTL
# means somebody is RENEWING it, which is a second live processor and the only thing worth
# paging about. See `process_once`.
_LEASE: dict[str, Any] = {"refused_since": None}

# The pass's own BEAM spending — the cycle-level guard a per-leg read cannot be: N calls are
# about to be made against ONE BeamPay balance read of the treasury's asset 0. (The shielded
# float needs no counter: a
# released order is already `releasing` in the database, so `inflight_groth` sees it.)
_PASS: dict[str, Any] = {
    "beam_fee": 0,
    "expect_route": None,
    "fee_budget": {},
    # The shielded float is spread over MANY max-privacy addresses — one per shield chunk, see
    # `shield_target` — so reading it is N BeamPay calls and no longer one. The registry and
    # the sum it produces are cached per PASS for the same reason the fee budget is: N payouts
    # are about to be gated on one answer, and a float that changes between two orders of one
    # pass is exactly the defect `inflight_groth` exists to prevent.
    "mp_registry": None,
    "float": {},
    # the same law, one level down: `payout_float` splits the float by SOURCE and
    # `wallet_spendable` says which of those buckets the wallet can actually move today. Both
    # are one answer per pass per asset — N orders gated on a number that changed between them
    # is the defect `inflight_groth` exists to prevent, arriving through a different reader.
    "float_parts": {},
    "wallet": {},
    # how many spendable COINS the wallet holds, per asset per bucket — one `get_utxo` walk per
    # pass, because Beam locks a whole coin per pending transaction and N releases are about to
    # be admitted against one reading of that
    "coins": None,
    # WHEN each `wallet` answer above was read. The processor never asks for a freshness bound
    # (one pass, one answer); the REQUEST path does — see `wallet_spendable`.
    "wallet_at": {},
    # the BEAM every crossing address is carrying instead of the treasury (T40) — one
    # aggregation per pass, for the same reason every float read is
    "crossing_debt": None,
    # the pass's HOLDS, grouped by kind, and the rows that stopped being held. Three orders
    # refused for one reason used to be three pages an hour, for ever; the ROW still carries its
    # own reason and the PAGER gets one digest (`flush_hold_digest`).
    "holds": {},
    "unheld": {},
}


def get_rpc() -> Any:
    """The Ethereum RPC pool — through workers so one process has one pool (and one fake)."""
    return workers.get_rpc()


def reset_archive_pin() -> None:
    _ARCHIVE["url"] = None


def reset_process_state() -> None:
    """Everything this module remembers BETWEEN passes, cleared. `process_once` clears the
    per-pass halves itself; this is for a test (isolation kept per file is isolation a new file
    does not inherit) and for any caller that reaches a gate without going through a pass — the
    CLI dry runs do, and a fee budget cached from one database must never answer for another."""
    _PASS["beam_fee"] = 0
    _PASS["expect_route"] = None
    _PASS["fee_budget"] = {}
    _PASS["mp_registry"] = None
    _PASS["float"] = {}
    _PASS["float_parts"] = {}
    _PASS["wallet"] = {}
    _PASS["wallet_at"] = {}
    _PASS["coins"] = None
    _PASS["crossing_debt"] = None
    _PASS["holds"] = {}
    _PASS["unheld"] = {}
    _FLOAT_SCHED.clear()
    _LEASE["refused_since"] = None


def _asset_of(row: dict[str, Any]) -> Asset:
    return get_asset(row.get("asset") or "ETH")


async def ensure_indexes() -> None:
    """The guards the DATABASE owns, not a read.

    A txid is evidence, and evidence belongs to exactly one row: two rows carrying one
    `beam_txid` means one burn was booked twice. A delivery is consumed by exactly one request.
    Idempotent; called from `workers.ensure_indexes()` at start."""
    d = db()
    await d.payout_requests.create_index(
        [("beam_txid", 1)],
        unique=True,
        partialFilterExpression={"beam_txid": {"$type": "string"}},
        name="uniq_payout_beam_txid",
    )
    await d.deposits.create_index(
        [("claim_txid", 1)],
        unique=True,
        partialFilterExpression={"claim_txid": {"$type": "string"}},
        name="uniq_deposit_claim_txid",
    )
    # A pipe message is evidence too, and evidence belongs to exactly one row. `_our_msg` is
    # one of the two discriminators the whole identity design rests on — it is what unblocks the
    # irreversible ledger release and what validates a lost-response adoption — and nothing
    # stopped two payouts of one amount to one wallet from both claiming message N.
    await d.payout_requests.create_index(
        [("asset", 1), ("msg_id", 1)],
        unique=True,
        partialFilterExpression={"msg_id": {"$type": "int"}},
        name="uniq_payout_asset_msg_id",
    )
    await d.payout_requests.create_index([("status", 1), ("status_at", 1)])
    await d.deposits.create_index([("treasury", 1), ("treasury_at", 1)])
    # `txid_is_taken` asks the database whether ONE exact txid is already somebody's evidence.
    # beam_txid and claim_txid are already indexed by the unique guards above; the shield ids
    # are the third place a txid of ours lives, and that lookup needs an index of its own.
    await d.deposits.create_index([("shield_txids", 1)])
    # THE MAX-PRIVACY REGISTRY: one row per shield chunk, and the shielded float is the SUM
    # over it (`float_groth`). `_id` IS the chunk's identity (`shield_comment`), so two
    # addresses for one chunk are impossible by construction rather than by convention.
    await d.mp_addresses.create_index([("created_at", 1)])
    await d.mp_addresses.create_index([("deposit_id", 1), ("k", 1)])
    # THE INSTANT DISTRIBUTOR (T34). `_id` is the address, so one row per key is free; this is
    # the index behind "the active one" — read by every instant payout, by the refill pass and
    # by /v1/stats.
    await d.distributors.create_index([("state", 1)])
    # …and the refill queue: "is a top-up already in flight" is asked once per pass.
    await d.payout_requests.create_index([("mode", 1), ("status", 1)])


# ----------------------------------------------------------------------------- rows and events


async def _advance(
    coll: str,
    row_id: str,
    field: str,
    frm: str | None,
    to: str,
    kind: str,
    text: str,
    id_key: str,
    immediate: bool = False,
    still_waiting: bool = False,
    unset: Iterable[str] = (),
    **fields: Any,
) -> dict[str, Any] | None:
    """ONE conditional update per transition, and ONLY the pass that matched writes the event.

    A read-then-write is not a claim: two passes (or two processes) that both see `scheduled`
    would both send. The filter carries the from-status, so the second finds nothing.
    Returns the row as it was BEFORE the update, or None when another pass owned it.

    `<field>_at` is stamped here and NOWHERE else: it is the moment the row ENTERED this
    status, and it is what the SLA monitor measures. `updated_at` cannot be that clock — the
    delivery scan writes a checkpoint on every 30-second pass, which reset an 18-hour deadline
    to zero and made the one monitor that watches burned bETH dead code."""
    now = time.time()
    q: dict[str, Any] = {"_id": row_id}
    q[field] = {"$exists": False} if frm is None else frm
    claimed = await db()[coll].find_one_and_update(
        q,
        {
            "$set": {field: to, f"{field}_at": now, "updated_at": now, **fields},
            # the hold is over, so every trace of it goes — including the two stamps the HELD
            # reminder reads. A row that is held AGAIN later must page again, not inherit the
            # silence of a hold that was resolved.
            "$unset": {
                "hold_reason": "",
                "hold_at": "",
                "hold_paged_at": "",
                "held_reminded_at": "",
                # …and the two-text split's own fields: a row that has moved on must not keep an
                # operator detail, a code or a promised `ready_at` from the wait it just left.
                "hold_detail": "",
                "hold_code": "",
                "ready_at": "",
                "ready_reason": "",
                "dark": "",
                # …plus whatever the caller says this transition must forget. `_payout_delayed`
                # is the one that needs it: a retry that kept the dead attempt's `beam_txid`
                # would be short-circuited by it in `_payout_releasing` for ever.
                **{k: "" for k in unset},
            },
        },
    )
    if not claimed:
        return None
    # a row that WAS held and has now moved is the other half of the digest below: an operator
    # who was told "3 payouts are waiting on the float" has to be told when they stop waiting,
    # or the only thing the pager ever says about a hold is that it exists.
    if claimed.get("hold_reason") and not still_waiting:
        # ⛔ …unless the row is moving INTO another wait (`_delay`). "3 payouts stopped waiting"
        # about rows that are still waiting, for a new reason, is the pager saying the opposite
        # of what happened.
        _PASS["unheld"].setdefault(coll, []).append(str(row_id))
    # ⛔ **THE ROW'S LANE TRAVELS WITH THE EVENT** (T47, wired here 2026-09-10). `tg.rung`
    # renders `[k/N]` from the KIND and switches to the 3-rung instant ladder on the event's own
    # `mode` — and no emitter ever wrote one, so an instant order rendered on the 5-rung crossing
    # ladder. A wrong denominator is a wrong story about how far along an order is.
    ids: dict[str, Any] = {id_key: row_id}
    mode = str(claimed.get("mode") or "")
    if mode:
        ids["mode"] = mode
    if immediate:
        await tg.alert(kind, text, **ids)
    else:
        await tg.queue(kind, text, **ids)
    return claimed


async def _hold(
    coll: str,
    row_id: str,
    reason: str,
    key: str,
    dark: bool = False,
    cooldown_s: float = 3600.0,
    ctx: dict[str, Any] | None = None,
) -> None:
    """A refusal is not a trade and not a failure — but every decision path writes a row. The
    reason lands ON the order (unbounded refusal rows would be their own outage) and the
    operator hears at most once an hour.

    `dark` marks a wait that a flag caused on purpose, so the stuck monitor does not cry wolf
    about a deployment that is deliberately off. ⛔ It is written on EVERY hold, never only on
    the dark ones: a row held once while a flag was off kept `dark: True` after the operator
    armed the flag, and every later hold — short float, relayer starvation, a crossing that
    never delivered — was then invisible to every stuck check, forever. `dark` describes the
    CURRENT reason for waiting or it describes nothing."""
    now = time.time()
    # ⛔ `hold_at` and NOT `hold_paged_at`: this hold is a WAITING row that a handler still owns
    # and will retry. Only `_hold_for_a_human` — the terminal hold nothing retries — stamps when
    # the operator was paged, because that stamp is what silences the HELD reminder. This hold
    # writing it too made a fee-budget wait mute the pager for a row already parked for a human.
    # ⛔ **THE ROW SPEAKS TO WHOEVER READS IT** (T52). A payout order has a page a user
    # refreshes, so its `hold_reason` is the USER half of `hold_texts` and the numbers go to
    # `hold_detail`, which only the admin routes publish. A deposit's treasury hold has no such
    # page — it is our own sweep — and keeps the operator sentence it always had.
    code = hold_kind(key)
    user, operator = hold_texts(code, {"operator": reason, **(ctx or {})})
    upd: dict[str, Any] = {"hold_reason": reason, "hold_at": now}
    if coll == "payout_requests":
        upd["hold_reason"] = user
        upd["hold_detail"] = operator
        upd["hold_code"] = code
        ready = (ctx or {}).get("ready_at")
        if ready and code in FLOAT_CODES:
            # …and WHEN it is expected to stop waiting, so `eta_for` never has to publish a
            # delivery time that has already passed (item 4).
            upd["ready_at"] = float(ready)
            upd["ready_reason"] = "treasury_float"
    change: dict[str, Any] = {"$set": upd, "$inc": {"holds": 1}}
    if dark:
        upd["dark"] = True
    else:
        change["$unset"] = {"dark": ""}
    await db()[coll].update_one({"_id": row_id}, change)
    # the PAGER gets the operator half: the digest is the one place the numbers belong.
    _digest(coll, row_id, operator, key, cooldown_s)


def _digest(coll: str, row_id: str, reason: str, key: str, cooldown_s: float = 3600.0) -> None:
    """This row joins the pass's WAITING digest — ONE writer of the bucket (law 9).

    ⛔ THE PAGE IS PER KIND, NOT PER ROW (2026-09-10). `key` is `<kind>:<row id>`, so three
    orders refused for one reason were three separate cooldowns and three pages an hour, for
    ever — and the operator had already asked for the flood to stop. The row keeps its own
    reason and its own `holds` counter (law 12: every decision path writes a row); the PAGER
    gets one digest per kind per pass, sent under the KIND's cooldown, so the first is
    immediate and the rest are hourly however many rows are waiting.

    Split out of `_hold` so `_delay` can join the same digest WITHOUT stamping `hold_at`: that
    field is `_due`'s own backoff (`hold_backoff_s`, 5 min) and a delayed order carries its own,
    shorter schedule (`next_attempt_at`, first rung 1 min). Written by both, the 5-minute one
    would silently win and the ladder's first three rungs would never be climbed."""
    bucket = _PASS["holds"].setdefault(
        hold_kind(key), {"coll": coll, "reason": reason, "ids": [], "cooldown_s": 0.0}
    )
    if str(row_id) not in bucket["ids"]:
        bucket["ids"].append(str(row_id))
    bucket["cooldown_s"] = max(float(bucket["cooldown_s"]), float(cooldown_s))


def hold_kind(key: str) -> str:
    """The hold's KIND — its cooldown key with the row id taken off (`payout-float:req1` →
    `payout-float`). One derivation, so the digest groups exactly the holds that share a reason
    and a caller cannot silently opt one row out of the grouping by spelling its key oddly."""
    return str(key).split(":", 1)[0] or str(key)


# The words the digest uses for a row of each collection, and how many ids it will name before
# it says "+N more" — a page Telegram has to truncate is a page that hides its own tail.
_ROW_NOUN = {"payout_requests": "payout", "deposits": "deposit"}
DIGEST_IDS = 10


# ══════════════════════════════ T52 — TWO TEXTS PER HOLD, ONE WRITER ══════════════════════════
#
# Admin, 2026-09-10 15:36Z, reading his own order's page: *"again issues no one can understand:
# WAITING: the wallet can spend 0.00775651 regular / 0 shielded ETH now; 0.01652864 is maturing
# (max-privacy lock…) … no free coin: ETH spendable coins 4 … BEAM fee coins 1 and this crossing
# needs 2 … `python -m pgasme.beam split --asset BEAM`"*. Every one of those sentences was
# written for an operator and published to a user, and the command at the end of it is OURS to
# run — it is not an instruction anybody outside this box can act on.
#
# So a refusal has TWO texts and this is the ONE writer of the pair (law 9):
#
#   * the USER text — what is happening, in words. No number, no wallet bucket, no coin count,
#     no flag name, no command. It is keyed by the refusal's CODE and never composed at the call
#     site, because a sentence written where the numbers are is a sentence that carries them. It
#     lands on `hold_reason` (the field `routers/account.public_request` publishes) and, through
#     `eta_for`, on `eta_note`.
#   * the OPERATOR text — the same refusal WITH its numbers, every amount in the asset
#     (`fmt_units`, item 7). It lands on `hold_detail`, which the admin routes publish and
#     `public_request` strips, and it is what the Telegram digest sends.
#
# ⛔ A DEPOSIT'S TREASURY HOLD IS NOT AN ORDER'S. `_hold` is shared with the deposit half, whose
# `hold_reason` is operator prose about our own sweep and has no user-facing order page behind
# it; only `payout_requests` rows get the split, so a change here cannot silently reword the
# treasury's own diagnostics.
WAIT_FLOAT = "Waiting for treasury funds"
FLOAT_WHEN = " — expected by {when} at the latest (earlier if deposits come in)"
FLOAT_SOON = " — expected as soon as new deposits come in"
USER_COIN = "Waiting for the wallet to free a coin — usually a few minutes"
USER_FEES = "Waiting for network fees to drop — checking every 5 minutes"
USER_NODE = "Waiting for a node to answer — retrying"
USER_DEST = "The destination address is a contract now — held for review"
# the 17:58Z addition: the treasury has already moved this crossing's money to an address of its
# own and the invocation is the next step. Nothing is wrong and nothing is short.
USER_FUNDED = "Funds are on their way to the bridge — usually a few minutes"
USER_PAUSED = "Payouts are paused right now — your money stays reserved"
# the crossing is ON the bridge: the bETH is burned, the relayer is paying, and every one of
# these waits is us MATCHING what arrived to the order that paid for it (identity, never
# presence). Nothing is wrong with the order and there is nothing for the user to do.
USER_BRIDGE = "On its way across the bridge — we are matching the delivery"
USER_CONFIRMING = "Waiting for the Beam network to confirm the crossing"
USER_HUMAN = "Someone at Pgas.me is checking this order — your money stays reserved"
USER_RETRY = "Checking the last attempt before trying again"

# The refusals that are about the FLOAT, and therefore the ones whose user text can name a time:
# the max-privacy lock has an end, and "expected by <then> at the latest" is the honest promise.
FLOAT_CODES = (
    "payout-float",
    "payout-source",
    "payout-wallet",
    "payout-nofloat",
    "payout-unshielded",
    "payout-instant-float",
)
# …and the ones that mean "this crossing is funded and moving" (`fund_called_at` is set).
FUNDED_CODES = ("payout-funding", "payout-fundsettle")

HOLD_USER: dict[str, str] = {
    **{c: WAIT_FLOAT for c in FLOAT_CODES},
    **{c: USER_FUNDED for c in FUNDED_CODES},
    "payout-coins": USER_COIN,
    "beam-fee": USER_COIN,
    "payout-share": USER_FEES,
    "payout-subsidy": USER_FEES,
    "payout-walletread": USER_NODE,
    "payout-coins-unreadable": USER_NODE,
    "payout-crossaddr": USER_NODE,
    "payout-fundrefused": USER_NODE,
    "payout-dest": USER_DEST,
    "payout-dark": USER_PAUSED,
    "payout-anyasset": USER_PAUSED,
    "payout-amount": USER_HUMAN,
    "payout-asset": USER_HUMAN,
    "payout-delayed": USER_RETRY,
    # ── after the send: the crossing exists and these are the reads that prove WHICH ────────
    "payout-attr": USER_NODE,
    "payout-nomsg": USER_BRIDGE,
    "payout-nomsgmatch": USER_BRIDGE,
    "payout-notxid": USER_BRIDGE,
    "payout-nobase": USER_BRIDGE,
    "payout-unattributable": USER_BRIDGE,
    "payout-batchunclear": USER_BRIDGE,
    "payout-noconfs": USER_CONFIRMING,
    # …and the three that mean two orders claim one thing: never a machine's to resolve
    "payout-msgtaken": USER_HUMAN,
    "payout-txtaken": USER_HUMAN,
    "payout-dupdelivery": USER_HUMAN,
    # the terminal hold a human owns (`_hold_for_a_human`): to the user it is still a delay, and
    # what they need to know is that the money is theirs and nothing was sent.
    "payout-held": USER_HUMAN,
    # ── the instant lane (dark under PGAS_PAYOUT_INSTANT_ENABLED=0, and covered anyway: a lane
    # that is armed one day must not be the day its refusals learn to speak) ──────────────────
    "payout-instant-asset": USER_HUMAN,
    "payout-instant-amount": USER_HUMAN,
    "payout-instant-dark": USER_PAUSED,
    "payout-instant-nokey": USER_PAUSED,
    "payout-instant-key": USER_PAUSED,
    "payout-instant-dest": USER_DEST,
    "payout-instant-self": USER_HUMAN,
    "payout-instant-registerread": USER_NODE,
    "payout-instant-gasread": USER_NODE,
    "payout-instant-unpriced": USER_NODE,
    "payout-instant-subsidy": USER_FEES,
    "payout-instant-floatread": USER_NODE,
    "payout-instant-inflight": USER_NODE,
    "payout-instant-nonceread": USER_NODE,
    "payout-instant-nonce": USER_HUMAN,
    "payout-instant-reserve": USER_HUMAN,
    "payout-instant-receiptread": USER_NODE,
}
HOLD_CODES = tuple(sorted(HOLD_USER))


def fmt_units(groth: int, asset: Asset | str) -> str:
    """A groth figure as a PERSON reads it: "0.00775651 ETH" — THE one formatter (T45 item 5).

    `tg.fmt_groth` is the arithmetic (8 decimals, trailing zeros trimmed) and the symbol is
    appended here, because a bare number in a sentence about money is half a fact — which is
    exactly what the operator digest was sending: *"the relayer wants 0.00039 and this payout
    only funded 0.0001"*, of what? `routers/withdrawals.fmt_units` is this function; it is
    stated once, on this side, because `withdrawals` already imports `payouts` and the reverse
    would be a cycle."""
    key = asset if isinstance(asset, str) else asset.key
    return f"{tg.fmt_groth(int(groth))} {key}"


def fmt_when(ts: float) -> str:
    """A moment as a person reads it: "Sat 12 Sep, 23:21Z". UTC, always, and it SAYS so — the
    user's browser renders its own clock from `eta_at`; this is the copy inside a sentence, and
    a bare "23:21" in prose is a time in nobody's particular zone.

    Built from the parts rather than one `strftime`: `%-d` is a GNU extension and this string is
    asserted on by tests that run on both a laptop and the box."""
    d = dt.datetime.fromtimestamp(float(ts), tz=dt.UTC)
    return f"{d:%a} {d.day} {d:%b}, {d:%H:%M}Z"


def hold_texts(code: str, ctx: dict[str, Any] | None = None) -> tuple[str, str]:
    """`(what the user reads, what the operator reads)` for one refusal code — THE writer.

    `ctx` carries what the pair needs and nothing else: `operator` is the sentence the call site
    composed (with its numbers, in the asset), and `ready_at` is the moment a float-short wait
    expects to end, which is the only number a USER text ever contains — as a date, in words.

    ⛔ **AN UNKNOWN CODE IS NOT AN EXCEPTION HERE.** A missing sentence must never take down the
    path that writes the refusal row (law 11: the row is the point), so it falls back to the
    "a person is looking at this" text and the EXHAUSTIVENESS is a test — every `payout-*` key
    literal in this module has to appear in `HOLD_USER` or the suite fails."""
    ctx = dict(ctx or {})
    user = HOLD_USER.get(str(code), USER_HUMAN)
    if user == WAIT_FLOAT:
        ready = ctx.get("ready_at")
        user += FLOAT_WHEN.format(when=fmt_when(ready)) if ready else FLOAT_SOON
    operator = str(ctx.get("operator") or "").strip() or user
    return user, operator


def operator_reason(row: dict[str, Any]) -> str:
    """Why this row is waiting, WITH its numbers — for an operator surface, never a user's page.

    ONE reader, so the monitor, the digest and the admin panel cannot end up reading different
    fields: `hold_detail` when the two-text split wrote one, and `hold_reason` for every row
    written before it (and for the deposit half, which has no split)."""
    return str(row.get("hold_detail") or row.get("hold_reason") or "")


def hold_digest(bucket: dict[str, Any]) -> str:
    """One kind's WAITING line: WHICH order, then the shared reason, then every id it covers.

    ⛔ **IT LEADS WITH THE ORDER** (T47's shape, applied here 2026-09-10). `tg.format_event`
    opens every notification with `[k/N] rung · payout 64ee5540…`, and this line opened with the
    word WAITING and buried the ids at the end — so the one message an operator gets about a
    stuck order looked like a different product from every other message about it."""
    ids = [str(i) for i in bucket["ids"]]
    noun = _ROW_NOUN.get(str(bucket["coll"]), "row")
    shown = " ".join(f"<code>{i}</code>" for i in ids[:DIGEST_IDS])
    more = f" +{len(ids) - DIGEST_IDS} more" if len(ids) > DIGEST_IDS else ""
    lead = f"{noun} {tg.esc(tg.short(ids[0]))}" if ids else noun
    if len(ids) == 1:
        return f"{lead} — WAITING: {tg.esc(bucket['reason'])} {shown}"
    return (
        f"{lead} +{len(ids) - 1} more — WAITING ({len(ids)} {noun}s): "
        f"{tg.esc(bucket['reason'])} — {shown}{more}"
    )


async def flush_hold_digest() -> None:
    """Send this pass's holds as ONE message per kind, and one line for the rows that released.

    Called at the end of `process_once` — after both halves, so a payout and a deposit held for
    the same class of reason are still counted separately (they are different collections and
    different kinds). It never raises: a pager that can throw would take the pass down with it,
    and the rows are already written."""
    holds: dict[str, Any] = _PASS["holds"]
    unheld: dict[str, Any] = _PASS["unheld"]
    _PASS["holds"] = {}
    _PASS["unheld"] = {}
    for kind, bucket in holds.items():
        await tg.send(
            hold_digest(bucket), key=kind, cooldown_s=float(bucket["cooldown_s"]) or 3600.0
        )
    for coll, ids in unheld.items():
        noun = _ROW_NOUN.get(str(coll), "row")
        shown = " ".join(f"<code>{i}</code>" for i in ids[:DIGEST_IDS])
        more = f" +{len(ids) - DIGEST_IDS} more" if len(ids) > DIGEST_IDS else ""
        # ⛔ NOT "RELEASED" (T52). A release is a thing this system does with money — the bETH
        # crosses, the ledger books it — and this line is about a row that stopped waiting for
        # one reason and went back to the queue to be tried again. The operator read the word
        # and looked for a crossing that had not happened.
        await tg.send(
            f"Back in the queue: {len(ids)} {noun}(s) (delayed → retrying) — {shown}{more}",
            key=f"unheld:{coll}",
        )


async def _hold_for_a_human(
    coll: str, row_id: str, field: str, frm: str, reason: str, kind: str, id_key: str
) -> None:
    """The terminal hold: the row LEAVES the status whose handler resolves things.

    `bridge_watcher.resolve_unconfirmed_contract` advances the order to `on_hold` and its
    handler no longer resolves. The port copied the message and dropped the state change, so
    the row stayed in `releasing` with the same unbounded `since` — and un-held itself two
    hours later by adopting an unrelated transaction. A row a human owns must not be able to
    un-hold itself.

    THE ONE WRITER of `hold_paged_at`: this call sends the page (`tg.alert`, immediate) and
    stamps the moment it did, in the same update. `workers.held_paged_at` reads that field and
    nothing else, so "when were we last told about this hold" has exactly one author — and a
    hold of a different kind (`_hold`, which a handler still retries) can never silence the
    reminder that follows."""
    now = time.time()
    # ⛔ THE SAME SPLIT AS `_hold` (T52): the sentences here are the longest and most operator-
    # shaped in the module ("/withdraw is not idempotent", txids, comments), and every one of
    # them was rendering on a user's order page. What a user needs from a parked order is that
    # a person has it and their money is untouched; the evidence goes to `hold_detail`.
    user, operator = hold_texts("payout-held", {"operator": reason})
    payout_row = coll == "payout_requests"
    claimed = await db()[coll].find_one_and_update(
        {"_id": row_id, field: frm},
        {
            "$set": {
                field: HELD,
                f"{field}_at": now,
                "updated_at": now,
                "held_from": frm,
                "hold_reason": user if payout_row else reason,
                **({"hold_detail": operator, "hold_code": "payout-held"} if payout_row else {}),
                "hold_at": now,
                "hold_paged_at": now,  # ← read by workers.held_paged_at; written NOWHERE else
                "unresolved_at": now,
            },
            # a fresh hold starts a fresh reminder clock: the last reminder was about the
            # previous hold, and THIS page is now the most recent thing that said anything
            "$unset": {"dark": "", "held_reminded_at": ""},
        },
    )
    if claimed is None:
        return
    await tg.alert(kind, f"HELD: {reason}", **{id_key: row_id})


async def _set(coll: str, row_id: str, **fields: Any) -> None:
    await db()[coll].update_one({"_id": row_id}, {"$set": {"updated_at": time.time(), **fields}})


async def _checkpoint(coll: str, row_id: str, **fields: Any) -> None:
    """A scan checkpoint is bookkeeping, not progress: it must NOT stamp `updated_at`.

    `_payout_delivering` writes one on every pass, found or not. Stamping `updated_at` there
    kept every delivering row 30 seconds young and the 18-hour SLA never fired once — on the
    one status where the bETH is already burned and cannot be recalled."""
    await db()[coll].update_one({"_id": row_id}, {"$set": fields})


# ------------------------------------------------------- never failed: delay, attempts, ETA


def delay_backoff_s(n: int) -> float:
    """How long to wait before attempt `n + 1`, in seconds — 1 → 2 → 5 → 15 → 60 min, capped.

    ONE reader, so the number the row is stamped with and the number the operator reads in the
    digest cannot drift. `n` is 1 for the FIRST delay."""
    i = max(0, int(n) - 1)
    return DELAY_BACKOFF_S[min(i, len(DELAY_BACKOFF_S) - 1)]


def attempts_of(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every attempt this order has made, oldest first. Append-only, and it is the ONLY counter:
    a mutable `retries` field is a number two writers eventually disagree about, and the thing
    it would be disagreeing about is whether to sign a second transaction over one inventory."""
    got = row.get("attempts")
    return list(got) if isinstance(got, list) else []


async def append_attempt(
    coll: str, row_id: str, kind: str, txid: str | None = None, state: str = "sending"
) -> int:
    """Write down that this order is ABOUT to make attempt N, and return N.

    ⛔ **BEFORE THE CALL, NEVER AFTER IT.** A `process_invoke_data` whose answer is lost has
    still landed; an attempt nobody recorded is a transaction nobody can ever prove, and the
    only way to resolve one would be a second signature. The txid does not exist yet for a Beam
    send (the route answers it), so the entry is opened without one and `settle_attempt` fills
    it in on the SAME entry a moment later — the list itself is never re-ordered, never
    shortened and never renumbered, which is what "append-only" protects."""
    n = len(attempts_of(await db()[coll].find_one({"_id": row_id}) or {})) + 1
    await db()[coll].update_one(
        {"_id": row_id},
        {
            "$push": {
                "attempts": {
                    "n": n,
                    "kind": kind,
                    "txid": txid,
                    "at": time.time(),
                    "state": state,
                }
            },
            "$set": {"updated_at": time.time()},
        },
    )
    return n


async def settle_attempt(coll: str, row_id: str, n: int, txid: str, state: str) -> None:
    """The identifier the attempt got, written onto the entry that was opened for it."""
    i = int(n) - 1
    await db()[coll].update_one(
        {"_id": row_id},
        {"$set": {f"attempts.{i}.txid": str(txid), f"attempts.{i}.state": state}},
    )


async def _delay(
    row: dict[str, Any],
    frm: str,
    reason: str,
    backoff: float | None = None,
    key: str | None = None,
    ctx: dict[str, Any] | None = None,
) -> None:
    """⛔ **THE ONE REPLACEMENT FOR `_fail`.** The order is not over — it is waiting.

    Status `delayed`, the reason in plain words on the row, a `next_attempt_at` on the ladder,
    the money STILL RESERVED (no `cancel` entry, no refund: nothing was given back because
    nothing was finished), one operator event and one line in the pass's WAITING digest.

    `frm` is the status it is leaving; a row already delayed keeps its transition and only its
    reason and its next attempt move, so the ladder does not emit an event per rung. The FIRST
    delay pins `delayed_since` and nothing moves it afterwards — that clock is what
    `_payout_delayed` measures the 24-hour handover to a human with, and a guard must not be
    able to refresh its own deadline (law: `_checkpoint` exists for the same reason).

    `key` is the DIGEST's key, and a caller that has one passes it (T40b F3): `_refuse` turns a
    gate refusal into a delay, and the digest is grouped per KIND — three orders short of float
    are one `payout-float` line, not three `payout-delayed` ones sharing whichever reason
    happened to be written first."""
    rid = row["_id"]
    now = time.time()
    n = int(row.get("delays") or 0) + 1
    # ⛔ **A FUNDED CROSSING IS NOT ON THE LADDER** (17:58Z, order 64ee5540…). Once
    # `fund_called_at` is set the treasury has ALREADY moved this crossing's money to an address
    # of its own; what is left is to see the transfer settle and make the invocation, and both
    # are ours to do within seconds. That order collected seven rungs from the gate refusals it
    # met BEFORE it was funded, and then sat for an hour with its money on a fresh address
    # waiting for a backoff that was measuring the wrong thing. The ladder exists to stop a
    # refused gate being re-asked every 30 s; a funded crossing is not a refused gate, it is
    # work in progress, and it is polled every pass (`_due`'s `always`, and
    # `_payout_delayed`'s first gate) until it moves.
    funded = awaiting_invocation(row)
    wait = 0.0 if funded else (float(backoff) if backoff is not None else delay_backoff_s(n))
    nxt = now + wait
    fields: dict[str, Any] = {
        "delays": n,
        "delayed_since": float(row.get("delayed_since") or 0) or now,
        "delayed_from": str(row.get("delayed_from") or frm),
        "next_attempt_at": nxt,
    }
    if str(row.get("status")) == DELAYED and frm == DELAYED:
        await _set("payout_requests", rid, **fields)
    else:
        claimed = await _advance(
            "payout_requests",
            rid,
            "status",
            frm,
            DELAYED,
            "payout_delayed",
            f"Payout DELAYED (nothing is lost, the money stays reserved): {reason}. Next "
            f"attempt {'on the next pass' if funded else f'in {int(wait // 60) or 1} min'}",
            "request_id",
            still_waiting=True,
            **fields,
        )
        if claimed is None:
            return  # another pass owns it
    # ⛔ AFTER the transition and never inside it: `_advance` unsets `hold_reason` in the same
    # update, and MongoDB refuses a `$set` and a `$unset` of one path in one document update.
    #
    # …and the SAME two texts a hold writes (T52): the row a user reads says what is happening,
    # `hold_detail` keeps the numbers, and the digest below sends the operator half.
    code = hold_kind(key or f"payout-delayed:{rid}")
    user, operator = hold_texts(code, {"operator": reason, **(ctx or {})})
    ready = (ctx or {}).get("ready_at")
    await _set(
        "payout_requests",
        rid,
        hold_reason=user,
        delay_reason=user,
        hold_detail=operator,
        hold_code=code,
        **(
            {"ready_at": float(ready), "ready_reason": "treasury_float"}
            if ready and code in FLOAT_CODES
            else {}
        ),
    )
    _digest("payout_requests", rid, operator, key=key or f"payout-delayed:{rid}")


async def _refuse(
    row: dict[str, Any], reason: str, key: str, dark: bool = False, cooldown_s: float = 3600.0
) -> None:
    """A gate said no. WHERE that lands depends on one thing: has the user been promised a time?

    ⛔ **A REFUSAL AFTER THE DELIVERY WINDOW IS A DELAY, NOT A QUIET HOLD** (T40b F3). `_hold`
    leaves the row in `scheduled` with a `hold_reason` nothing outside the operator's digest
    reads — and `eta_for` then answers `deliver_at`, which is in the PAST. The live row
    `d028cb8ad7623f95f001caf9` had thirteen of those: a healthy-looking order, an arrival time
    28 minutes gone, and the note "the delivery time you chose". So once the window has passed,
    a refusal moves the row onto the SAME ladder every other internal cause uses — `delayed`,
    the reason in plain words, a `next_attempt_at`, and an ETA that is still in the future.

    Before that, there is nothing to correct: the order is not late, the ETA is still the time
    it was given, and a hold — a WAITING row an operator sees and a user does not — is exactly
    right. ⛔ **AND "LATE" MEANS PAST THE ARRIVAL TIME WE PUBLISHED, NOT PAST `release_at`.**
    `release_at` is when the machine may START (`deliver_at − the bridge's ETA`), so a refusal
    there is early, not late, and treating it as late would put a perfectly healthy two-step
    release — the treasury funds an address, the send follows seconds later — on a retry ladder
    and tell the user it is delayed. An order with no chosen delivery time ("asap") was promised
    the venue's own time from the moment its window opened, and that is what it is measured by.

    The digest key travels with it, so the pager groups by KIND either way and this changes what
    the row says, never how much the operator hears."""
    rid = row["_id"]
    status = str(row.get("status") or "")
    # ⛔ THE FLOAT REFUSALS ARE THE ONES THAT CAN NAME A TIME (T52 item 4). The max-privacy lock
    # ENDS, and a user told "waiting for treasury funds" with no end is told nothing. The unlock
    # comes from the ONE float reader, so the sentence on the row and the number the release
    # gate decides on are the same fact — and an unreadable float simply drops the date rather
    # than inventing one (law 8).
    ctx: dict[str, Any] = {"operator": reason}
    if hold_kind(key) in FLOAT_CODES:
        ready = await next_unlock_at(row)
        if ready:
            ctx["ready_at"] = ready
    due = float(row.get("deliver_at") or 0)
    if due <= 0:
        due = float(row.get("release_at") or 0)
        due = due + _eta_mode_s(row) if due > 0 else 0.0
    if status in ("scheduled", DELAYED) and due > 0 and time.time() >= due:
        await _delay(row, status, reason, key=key, ctx=ctx)
        return
    await _hold(
        "payout_requests", rid, reason, key=key, dark=dark, cooldown_s=cooldown_s, ctx=ctx
    )


async def previous_attempt_dead(row: dict[str, Any]) -> tuple[bool, str]:
    """⛔ **THE NO-DOUBLE-SPEND GATE.** May this order sign another transaction?

    Only when the previous attempt is PROVEN dead — the Beam contract transaction is status 4
    (failed) or 2 (cancelled) **and carries no kernel**. Everything else is a wait:

      * a kernel exists → the crossing HAPPENED whatever the status line said, and a retry
        would burn the bETH a second time for one order;
      * BeamPay has not seen the transaction yet → not an answer;
      * the status could not be read → an unreadable query is not evidence of anything (law 8),
        and this is the one gate whose false positive is a second payment;
      * still pending / registering → the money is on the wire.

    A row that never captured a txid but DID make the call is in the same position: the answer
    was lost, the transaction may exist, and `_payout_releasing`'s own resolver — never this —
    is what settles it.

    ⛔ **AND A BOOKED RELEASE IS NEVER DEAD, WHATEVER A LATER STATUS READ SAYS** (T40b F6,
    §BOOKED-IS-LANDED). `kernel_at` and `release_booked_txid` are stamped by `_book_release`
    AFTER the ledger entry for this order's release exists — they are the record that the money
    left. A status line contradicting them is a contradiction to be handed to a human, not a
    licence to sign again; and it is asked FIRST, before BeamPay is consulted at all, because
    the answer cannot depend on a read that might fail."""
    if row.get("kernel_at") or row.get("release_booked_txid"):
        return False, (
            f"this order's release is already BOOKED (kernel_at "
            f"{row.get('kernel_at') or '—'}, txid "
            f"{str(row.get('release_booked_txid') or '—')[:12]}) — a booked release is never "
            f"'dead', and re-sending would pay this order twice"
        )
    txid = str(row.get("beam_txid") or "")
    if not txid:
        if float(row.get("release_call_at") or 0) > 0:
            return False, (
                "a release call was made for this order and no txid was ever captured, so the "
                "previous attempt is not settled"
            )
        return True, "nothing was ever signed for this order"
    try:
        tx = await beampay.beampay().contract_tx(txid)
    except beampay.BeamPayError as e:
        return False, (
            f"the previous attempt's status could not be read ({beam.redact(e)[:120]}) — "
            f"'we cannot see' is never 'it is dead'"
        )
    if tx is None:
        return False, "BeamPay has not seen the previous attempt's transaction yet"
    if tx.get("kernel"):
        return False, (
            "the previous attempt has a Beam kernel — the crossing is on the chain whatever "
            "its status line says, and re-sending would pay this order twice"
        )
    status = int(tx.get("status", -1))
    if status in beam.TX_DEAD:
        return True, (
            f"the previous attempt is {tx.get('status_string') or status} with no kernel — "
            f"nothing crossed"
        )
    return False, f"the previous attempt is {tx.get('status_string') or status} and not settled"


# ⛔ THE STATUSES A USER MAY STILL END. `held` is in the list on purpose: it is an OPERATOR
# state, not a terminal one, and to the user it reads as delayed — refusing their refund
# because we parked their row would be the product deciding to keep their money.
CANCELLABLE = ("scheduled", DELAYED, HELD)


def cancellable(row: dict[str, Any]) -> tuple[bool, str]:
    """`(may the user cancel this order and be refunded, why not)` — ONE reader (law 9).

    ⛔ **NEVER ONCE A KERNEL OR AN ETHEREUM TRANSACTION EXISTS.** A refund is the mirror of a
    debit that landed; once the bETH is burned or the transfer is signed and broadcast the
    value has left, there is no refund path on a b2e crossing, and giving the money back here
    would invent it. Read by `routers/withdrawals.cancel` and by the CLI, so the route the user
    presses and the answer an operator gets cannot disagree.

    ⚠️ A `delayed` order that ALREADY HAS a Beam txid is refused too, even though its crossing
    is the one that died. The txid is a transaction the wallet made: until the machine has
    proven it dead (`previous_attempt_dead`) it could still settle, and a refund plus a late
    settlement is exactly the double spend this whole task is about. Every other delayed order —
    a gas spike, no free coin, an unreadable wallet, a refused build, i.e. every cause that
    stops the order BEFORE anything is signed — carries no txid and cancels normally."""
    status = str(row.get("status") or "")
    if row.get("kernel_at"):
        return False, (
            "this order's Beam crossing is already signed — its kernel is on the chain and a "
            "b2e crossing has no refund path, so it cannot be cancelled"
        )
    if row.get("beam_txid"):
        return False, (
            "this order already has a Beam transaction and it is not proven dead — it could "
            "still settle, and refunding an order that then crosses would pay it twice"
        )
    if row.get("instant_tx"):
        return False, (
            "this order's Ethereum transaction is already signed and broadcast, so it cannot "
            "be cancelled"
        )
    # ⛔ …AND NOT ONCE THE TREASURY HAS MOVED THIS ORDER'S CROSSING TO AN ADDRESS OF ITS OWN
    # (T40b, a hole F5/F11 made visible). Nothing is burned yet, so this is not the same refusal
    # as the two above — it is the accounting one. A refund credits the user's ledger balance
    # while `fund_groth` stays at a fresh address that only this row names, and a `cancelled`
    # row is not in `crossing_pipeline`, so the float readers stop seeing it the moment the
    # refund lands: the ledger says we own value the treasury does not hold and nothing sweeps
    # it back. The order is not lost — it is delayed, it retries, and it delivers.
    if row.get("fund_called_at"):
        return False, (
            "this order's crossing has already been funded to a Beam address of its own — the "
            "money has left the treasury for this payout and a refund now would credit it "
            "twice; it retries automatically until it delivers"
        )
    if status not in CANCELLABLE:
        return False, f"request is {status} — only a scheduled or delayed request can be cancelled"
    return True, ""


def _eta_mode_s(row: dict[str, Any]) -> int:
    """How long this order's own venue takes once it is released: one Ethereum transfer, or a
    bridge crossing. ONE reader, shared by the `delayed` row's estimate and the released one's."""
    if str(row.get("mode") or "") == INSTANT:
        return int(INSTANT_ETA_S)
    return int(settings.bridge_eta_s)


def eta_for(row: dict[str, Any]) -> tuple[float | None, int, str]:
    """`(eta_at, eta_tail_s, eta_note)` — WHEN THIS ARRIVES, in one implementation.

    ⛔ It is COMPUTED, never stored. A stored `eta_at` would be a second writer of one fact
    (law 9) and would go stale the moment a row is delayed, released or re-delayed; the row
    carries the INPUTS (`deliver_at`, `next_attempt_at`, `status_at`, the release stamps) and
    this is the only place the answer exists. `routers/account.public_request` and
    `pgasme.beam status` both call it, so the page a user reads and the line an operator reads
    are the same number.

    `eta_tail_s` is the honest half: the relayer's measured tail on a b2e crossing is 18 hours
    (§7.6 — 4 h 03 m, 5 h 30 m, 18 h observed) against a typical 66 minutes, and a page that
    printed only the typical figure would be promising something nobody can keep. `None` means
    the estimate is not knowable from this row, and an unknown time is said out loud rather
    than invented (law 8).

    ⛔ **AND IT IS NEVER A TIME THAT HAS ALREADY PASSED** (T40b F3/F4). Three ways it used to
    be: a `failed`/`refunded` legacy row had no branch at all and fell through to the scheduled
    one, so two refunded orders were told they were on their way; a `scheduled` row whose window
    had passed answered `deliver_at`, in the past, with the note "the delivery time you chose";
    and a `delayed` row whose rung was due answered a rung already behind us. An estimate in the
    past is not a conservative estimate, it is a broken promise on a page the user refreshes.

    ⛔ **AND THE NOTE CARRIES NO RAW TIMESTAMP** (M3, from T35b). A page cannot render an epoch
    and must not be asked to parse one out of prose: the note says what is happening in words,
    and `routers/account.public_request` publishes `next_try_at` as ISO-8601 beside it."""
    now = time.time()
    status = str(row.get("status") or "")
    mode_s = _eta_mode_s(row)
    instant = str(row.get("mode") or "") == INSTANT
    tail = 0 if instant else BRIDGE_TAIL_S
    # ⛔ TERMINAL FIRST, AND EVERY TERMINAL STATUS NAMED. `failed`/`refunded` are not written by
    # this module any more, but rows written before 2026-09-10 carry them and are read for ever
    # (never edit history to fix a ledger) — and an order that was refunded has to SAY so.
    #
    # ⛔ AND IT SAYS IT WITHOUT THE WORD "REFUNDED" (T48, admin 2026-09-10 15:24Z on those very
    # rows): "Avoid status Refunded, it's not clear for the user … Refunded back to the balance
    # or what?" A refund is our bookkeeping word for it; what the user needs to know is where
    # the money is (their balance), that nothing left (no coin moved on either chain) and what
    # they can do (ask for it again). This is the ONE writer of that sentence — the web renders
    # it verbatim wherever it shows an eta note.
    if status in LEGACY_REFUNDED:
        return None, 0, "returned to your balance — nothing was sent; schedule again when ready"
    if status in ("sent", "cancelled"):
        return None, 0, "delivered" if status == "sent" else "cancelled"
    if status == DELAYED:
        nxt = float(row.get("next_attempt_at") or 0)
        why = str(row.get("hold_reason") or row.get("delay_reason") or "waiting on the treasury")
        # ⛔ A ROW WHOSE REASON WAS WRITTEN BY `hold_texts` IS ALREADY A SENTENCE FOR THIS PAGE
        # (T52). "delayed: <why>; we retry automatically" wrapped an operator fragment in the
        # words a user needed; the user half says what is happening AND that it is expected to
        # end, so wrapping it again would read as two systems talking at once. A row written
        # before the split (no `hold_code`) keeps exactly the note it had.
        note = why if row.get("hold_code") else f"delayed: {why}; we retry automatically"
        # …and when the wait has a KNOWN end — the max-privacy lock — that is the arrival time,
        # never the next rung of the ladder. `ready_at` in the past is not an estimate at all
        # (T40b F3/F4), so it falls through to the ordinary answer.
        ready = float(row.get("ready_at") or 0)
        if ready > now:
            return ready, tail, note
        if nxt <= 0:
            return None, tail, note
        return max(nxt, now) + mode_s, tail, note
    if status == HELD:
        why = str(row.get("hold_reason") or "an operator is looking at this order")
        return None, tail, why if row.get("hold_code") else f"delayed: {why}"
    if status in ("releasing", "bridging", PAYING):
        started = float(
            row.get("released_at")
            or row.get("release_attempt_at")
            or row.get("status_at")
            or row.get("updated_at")
            or 0
        )
        if started <= 0:
            return None, tail, "on its way"
        if instant:
            return started + INSTANT_ETA_S, 0, "paying from our Ethereum float — about a minute"
        return (
            started + settings.bridge_eta_s,
            BRIDGE_TAIL_S,
            "bridge ≈ 1 h, up to 18 h in the relayer's tail",
        )
    if status == "delivering":
        started = float(row.get("status_at") or row.get("updated_at") or 0)
        if started <= 0:
            return None, tail, "the relayer is paying"
        return (
            started + DELIVERING_ETA_S,
            0,
            "the Beam side is confirmed and the relayer is paying — usually minutes",
        )
    # scheduled (and the designed-but-dark any-asset statuses): the window the user chose
    if instant:
        created = float(row.get("created_at") or row.get("updated_at") or 0)
        if created <= 0:
            return None, 0, "paying from our Ethereum float — about a minute"
        return created + INSTANT_ETA_S, 0, "paying from our Ethereum float — about a minute"
    deliver_at = float(row.get("deliver_at") or 0)
    if deliver_at <= 0:
        return None, tail, "waiting for its delivery window"
    if deliver_at > now:
        return deliver_at, tail, "the delivery time you chose; the bridge is ≈ 1 h after release"
    # the window has PASSED and the order has not been released — it is waiting for a gate (the
    # float, a free coin, the fee budget), and the only honest estimate left is the venue's own
    # time from here. `deliver_at` is a promise that has already expired.
    return (
        now + mode_s,
        tail,
        "its delivery window has passed and it is next in line; the bridge is ≈ 1 h after release",
    )


# ----------------------------------------------------------------------------- one writer


async def acquire_lease() -> bool:
    """Claim the processor lease for THIS process. False → another loop is running; do nothing.

    The conditional updates below make a lost race harmless; this makes it visible."""
    now = time.time()
    d = db()
    await d.leases.update_one(
        {"_id": LEASE_ID},
        {"$setOnInsert": {"owner": OWNER, "at": 0.0}},
        upsert=True,
    )
    claimed = await d.leases.find_one_and_update(
        {
            "_id": LEASE_ID,
            "$or": [{"owner": OWNER}, {"at": {"$lt": now - settings.payout_lease_ttl_s}}],
        },
        {"$set": {"owner": OWNER, "at": now}},
    )
    if claimed is not None:
        _LEASE["refused_since"] = None
    return claimed is not None


async def release_lease() -> bool:
    """Hand the lease back — the graceful half of `acquire_lease`, called from the shutdown path.

    Without it a restart leaves the dead process's lease held for a whole TTL, so the new
    processor does nothing for two minutes and (before this change) paged that "a second payout
    processor is running" while it was simply its own corpse. The TTL is what makes a CRASH
    safe; this is what makes a restart fast, and the two must not be confused: a release is a
    convenience, an expiry is the guarantee.

    ⛔ **Only the owner may release.** The filter carries `owner: OWNER`, so a process that lost
    the lease (its TTL expired mid-pass and another loop took it) cannot free somebody else's
    claim on the way out — which would be this module handing one wallet to two writers at the
    exact moment one of them is dying.

    `at: 0.0` rather than a delete: the row keeps WHO held it and when they let go, so the next
    process's first log line can name its predecessor, and `{"at": {"$lt": now - ttl}}` in
    `acquire_lease` already treats 0 as free — one implementation of "expired", not two.

    Never raises: a shutdown path that can throw leaves the other tasks uncancelled, and the
    expiry covers this failing anyway. The failure is logged, not swallowed silently."""
    try:
        now = time.time()
        res = await db().leases.update_one(
            {"_id": LEASE_ID, "owner": OWNER},
            {"$set": {"at": 0.0, "released_at": now, "released_by": OWNER}},
        )
        freed = bool(getattr(res, "matched_count", 0))
    except Exception as e:  # noqa: BLE001 — shutdown never raises; the TTL still expires it
        log.warning(
            "payout processor: the lease could not be released (%s: %s) — it expires in %ss",
            type(e).__name__, beam.redact(e), settings.payout_lease_ttl_s,
        )
        return False
    _LEASE["refused_since"] = None
    log.info(
        "payout processor: lease %s %s by %s",
        LEASE_ID, "released" if freed else "was not ours to release", OWNER,
    )
    return freed


async def lease_holder() -> dict[str, Any] | None:
    return await db().leases.find_one({"_id": LEASE_ID})


# ----------------------------------------------------------------------------- shared evidence


async def txid_is_taken(
    txid: str, exclude_request: str | None = None, exclude_deposit: str | None = None
) -> bool:
    """True when some OTHER row already carries this Beam txid as its evidence.

    A transaction is evidence for exactly one crossing. This is the read-side half of the
    unique indexes in `ensure_indexes()`: the index refuses the write, this refuses the
    adoption before the row is even touched.

    ⛔ **A set-membership test must never depend on a window.** This was a
    `find(…).to_list(1000)` of every row that carries a txid, with NO sort — so past a thousand
    payouts it answered with the OLDEST thousand and the recent txids, the only ones a lost
    response could collide with, were exactly the ones missing. In production it is the one
    guard between a payout and adopting another order's transaction, and it would have failed
    open at the first moment it mattered. So the question is asked of the DATABASE, one exact
    txid at a time, on the indexed fields — no page, no ordering, no limit.

    `shield_txids` counts too: a shield is a BeamPay `/withdraw` of the treasury's own money,
    and the txid BeamPay made for it is no more available for adoption than a claim's."""
    tx = str(txid or "")
    if not tx:
        return False
    d = db()
    row = await d.payout_requests.find_one({"beam_txid": tx}, {"_id": 1})
    if row is not None and row["_id"] != exclude_request:
        return True
    dep = await d.deposits.find_one(
        {"$or": [{"claim_txid": tx}, {"shield_txids": tx}]}, {"_id": 1}
    )
    return dep is not None and dep["_id"] != exclude_deposit


async def _our_msg(w: beam.Wallet, row: dict[str, Any], asset: Asset) -> int | None:
    """The id of OUR outgoing pipe message for this payout, or None — the disproof that holds
    the row (§IDENTITY-BEATS-BALANCE). One helper, because both the lost-response resolver and
    the kernel path ask it and both must ask it the SAME way: from `msg_floor`, the highest
    message id that existed just before this release, and not from a fixed look-back window
    (see `beam.Wallet.find_local_msg`). A row released before `msg_floor` existed has none, and
    the window remains its only floor."""
    floor = row.get("msg_floor")
    return await w.find_local_msg(
        asset.beam_cid,
        str(row["W"]),
        int(row["amount_groth"]),
        from_msg_id=int(floor) if floor is not None else None,
    )


def taken_txid_check(
    exclude_request: str | None = None, exclude_deposit: str | None = None
) -> Callable[[str], Awaitable[bool]]:
    """`is_taken` for `beam.Wallet.find_contract_tx`: the same question, bound to this row."""

    async def is_taken(txid: str) -> bool:
        return await txid_is_taken(txid, exclude_request, exclude_deposit)

    return is_taken


# ----------------------------------------------------------------------------- fees


async def fee_charged(
    coll: str, row_id: str, tx: dict[str, Any], what: str, id_key: str, field: str | None = None
) -> int:
    """The BEAM fee the wallet ACTUALLY charged, read back from BeamPay and recorded.

    ⛔ **TWO WRITERS OF ONE FIELD.** `beam_fee_groth` on a deposit is written by the CLAIM and
    then overwritten by the SHIELD, so the claim's own fee — the number the next claim's budget
    has to be derived from — was gone by the time anyone could read it. `field` is the
    kind-specific one (`claim_fee_groth`, `shield_fee_groth`, `fund_fee_groth`,
    `crossing_fee_groth`) and has exactly one writer.

    ⛔ …AND IT IS THE ONLY FIELD THIS WRITES (T40b F7). Writing `beam_fee_groth` *as well* put
    the kind-specific field back in company: a payout's crossing funding (a 0.001 BEAM
    `/withdraw`) and its pipe invocation (0.121 BEAM, the wallet's own number) both landed
    there, one after the other, and `_FEE_SOURCE["send"]` read whichever was last as the cost of
    an INVOCATION — a budget derived from a number about a different kind of transaction. A
    caller that names no `field` still writes `beam_fee_groth`, which is what the treasury
    shield's summed-over-chunks figure is and all it has ever been.

    ⚠️ §WE-SET-IT-WE-DONT-READ-IT. `beam_claim_fee_groth` / `beam_shield_fee_groth` are the
    BUDGET this pass reserves before it spends — they are not numbers we send anywhere. No call
    this module makes carries a `fee` field at all: `invoke_contract` is built without one
    (exactly as `arb_tracker/rebal5_beth_to_eth.py:261` and `bridge_test/bridge_watcher.py:320`
    build theirs — neither passes a fee, the wallet sets it), and BeamPay's `/withdraw`
    overrides and ignores the field outright (api.py:322, `fee = tx_fee`).

    So the only honest thing to do with a fee is to READ IT BACK once it is settled, which is
    what this does — and to say so out loud when it is absurd. A single Beam transaction fee
    above the 5-BEAM scale (`PGAS_BEAM_FEE_ALERT_GROTH`, which is the scale at which fee money
    matters in both directions: below it in BALANCE the wallet is starving, above it in ONE FEE
    something is wrong) pages immediately rather than quietly draining the fee float."""
    charged = int(tx.get("fee") or 0)
    await _checkpoint(coll, row_id, **{field or "beam_fee_groth": charged})
    if charged > settings.beam_fee_alert_groth:
        await tg.alert(
            "beam_fee_excessive",
            f"{what} paid {tg.fmt_groth(charged)} BEAM in transaction fee — above the "
            f"{tg.fmt_groth(settings.beam_fee_alert_groth)} ceiling. Nobody sets this number: "
            f"the wallet does for an invocation, BeamPay does for a withdrawal, and this is it "
            f"read back",
            **{id_key: row_id},
        )
    return charged


# ------------------------------------------------------------------------ fee budgets


# How many settled transactions of a kind the budget is derived from, and the safety margin on
# the WORST of them. A max rather than a mean: the budget's job is to admit the next call, and a
# call sized by the average is refused by the wallet exactly on the expensive days.
FEE_HISTORY_N = 10
FEE_HISTORY_MARGIN = 1.5
# BeamPay's own withdrawal fee for an offline / max-privacy destination (api.py: `fee = tx_fee`,
# and it ignores whatever we send). A shield chunk is one of these.
BEAMPAY_WITHDRAW_FEE_GROTH = 1_100_000
# …and its fee for an ORDINARY regular address (api.py:310 `FEE_REGULAR = 100000`). The
# internal transfer that funds a crossing's own fresh address is one of these.
BEAMPAY_REGULAR_FEE_GROTH = 100_000

# kind → (collection, the field proving the transaction exists, the recorded fee, the sort key).
# The fee field is the KIND-SPECIFIC one: `deposits.beam_fee_groth` is written by the claim and
# then overwritten by the shield, so a history read of it would mix two different costs.
_FEE_SOURCE: dict[str, tuple[str, str, str, str]] = {
    "claim": ("deposits", "claim_txid", "claim_fee_groth", "claim_call_at"),
    # ⛔ `crossing_fee_groth` AND NEVER `beam_fee_groth` (T40b F7): a payout row's BEAM fees come
    # from two different kinds of transaction — BeamPay's 0.001 on the `/withdraw` that funds the
    # crossing address, and the wallet's own (0.121 live) on the pipe invocation. A send's budget
    # may only ever be derived from the second.
    "send": ("payout_requests", "beam_txid", "crossing_fee_groth", "release_call_at"),
    "shield": ("deposits", "shield_txids", "shield_fee_groth", "shielded_at"),
    # the internal transfer that funds a crossing's own fresh address (T40). Like a shield it
    # is a `/withdraw` and NOT a contract tx, so `contract_tx` cannot answer for one and its fee
    # is read back from the transaction BeamPay made.
    "fund": ("payout_requests", "fund_txid", "fund_fee_groth", "fund_called_at"),
}
# The kinds whose transaction is a WITHDRAWAL rather than a contract invocation: their fee comes
# from the transaction itself, already recorded per row, and `GET /internal/contract_tx` would
# answer `not_a_contract_tx` for them. ONE list, because `observed_fees` asks twice.
_FEE_NO_CONTRACT = ("shield", "fund")

# kind → (settings attribute if config.py ever grows one, env var, default). The FLOOR is what
# the budget is when there is no history at all, and it is deliberately generous: reserving too
# much only delays a call, reserving too little sends a call the wallet cannot pay for.
_FEE_FLOOR: dict[str, tuple[str, str, int]] = {
    "claim": ("beam_claim_fee_floor_groth", "PGAS_BEAM_CLAIM_FEE_FLOOR_GROTH", 15_000_000),
    # a pipe `send` is a BVM invocation in the same class as a claim
    "send": ("beam_send_fee_floor_groth", "PGAS_BEAM_SEND_FEE_FLOOR_GROTH", 15_000_000),
    "shield": (
        "beam_shield_fee_floor_groth",
        "PGAS_BEAM_SHIELD_FEE_FLOOR_GROTH",
        int(BEAMPAY_WITHDRAW_FEE_GROTH * FEE_HISTORY_MARGIN),
    ),
    # a transfer to a REGULAR address costs BeamPay's regular fee (api.py:311 `FEE_REGULAR =
    # 100000`), not the offline one — and, like every other number here, the floor is only what
    # the budget is until a settled transfer has been read back.
    "fund": (
        "beam_fund_fee_floor_groth",
        "PGAS_BEAM_FUND_FEE_FLOOR_GROTH",
        int(BEAMPAY_REGULAR_FEE_GROTH * FEE_HISTORY_MARGIN),
    ),
}


def fee_floor(kind: str) -> int:
    """The floor under this kind's budget, from the environment or the default.

    `config.py` is checked first so a knob added there later wins without a change here; a
    malformed value is refused (logged) and the default stands, because a floor that cannot be
    parsed must not become a floor of zero."""
    name, env, default = _FEE_FLOOR[kind]
    value = getattr(settings, name, None)
    if value is None:
        raw = os.getenv(env)
        if raw not in (None, ""):
            try:
                value = int(str(raw).strip())
            except ValueError:
                log.warning("%s=%r is not an integer number of groths — using %s", env, raw, default)
                value = None
    return max(0, int(value if value is not None else default))


async def observed_fees(
    kind: str, bp: beampay.BeamPay | None = None, n: int = FEE_HISTORY_N
) -> list[int]:
    """The BEAM transaction fees our last `n` settled transactions of this kind actually paid.

    §WE-SET-IT-WE-DONT-READ-IT, applied to the one number in this pipeline that we had been
    setting from a constant. Nobody sets a Beam transaction fee: the WALLET picks it for a
    contract invocation and BEAMPAY picks it for a withdrawal. So the only honest budget is the
    one derived from what those two really charged — read back from
    `GET /internal/contract_tx/{txid}.fee` for our own settled contract txs (a claim's
    `deposits.claim_txid`, a release's `payout_requests.beam_txid`), which is the identical
    number BeamPay debits from the registered address.

    A shield is a `/withdraw` and NOT a contract tx, so `contract_tx` cannot answer for one:
    its fee comes from the transaction BeamPay made, already read back and recorded per chunk by
    `_treasury_shielding`. No walk is started here for it.

    The recorded field is preferred and the BeamPay read is the fill-in for the one or two rows
    still in flight. ⛔ **The fill-in does NOT write the row.** `fee_charged` is the single
    writer of every recorded fee (law 9); a reader that also wrote would be the second, and two
    implementations of one fact disagree eventually even when they start from the same source.
    ⛔ An unreadable row is SKIPPED, never counted as a zero: this returns what it could
    establish, and `fee_budget` floors the answer — an incomplete history can only make the
    budget more conservative, never less."""
    coll, txid_field, fee_field, sort_key = _FEE_SOURCE[kind]
    q: dict[str, Any] = (
        {fee_field: {"$gt": 0}}
        if kind in _FEE_NO_CONTRACT
        else {txid_field: {"$exists": True, "$ne": None}}
    )
    rows = await db()[coll].find(q).sort(sort_key, -1).limit(n).to_list(n)
    fees: list[int] = []
    for row in rows:
        fee = int(row.get(fee_field) or 0)
        txid = "" if kind in _FEE_NO_CONTRACT else str(row.get(txid_field) or "")
        if fee <= 0 and txid and bp is not None:
            try:
                tx = await bp.contract_tx(txid)
            except beampay.BeamPayError as e:
                # "we could not read it" is not "it cost nothing" — drop the sample and say so
                log.warning("fee history: %s %s unreadable (%s)", kind, txid[:12], e)
                continue
            if not tx or not tx.get("booked") or int(tx.get("status", -1)) not in beam.TX_SETTLED:
                continue  # not settled yet: it has no final fee to learn from
            fee = int(tx.get("fee") or 0)
        if fee > 0:
            fees.append(fee)
    return fees


async def fee_budget(
    kind: str, bp: beampay.BeamPay | None = None
) -> tuple[int, dict[str, Any]]:
    """`(groth, why)` — what to reserve for the next `kind` call, and the derivation.

    ⚠️ THE FACT THIS EXISTS FOR. The first live claim (pipe message 137, 2026-09-09 21:40Z) cost
    **12,100,000 groth — 0.121 BEAM — against a `beam_claim_fee_groth` constant of 2,000,000**.
    The constant was not a number anyone had read; it was a number somebody had chosen once, and
    it was 6× wrong on the very first transaction. `max(floor, 1.5 × the worst of the last N)`
    is the same shape as `bridge_fee.py`'s margin: it tracks what the wallet really does, and it
    cannot fall below a floor generous enough for a market nobody has measured yet.

    Cached for the PASS (`_PASS["fee_budget"]`, cleared by `process_once`): three gates in one
    pass must reserve against one derivation, and a budget re-read per gate is N HTTP calls for
    a number that cannot change between them. It is a reservation, not a fee we send anywhere —
    no call this module makes carries a `fee` field at all."""
    cache = _PASS.setdefault("fee_budget", {})
    if kind in cache:
        return cache[kind]
    floor = fee_floor(kind)
    try:
        fees = await observed_fees(kind, bp)
    except Exception as e:  # noqa: BLE001 — a history we cannot read falls back to the floor
        log.warning("fee history for %s unreadable (%s: %s)", kind, type(e).__name__, e)
        fees = []
    worst = max(fees) if fees else 0
    budget = max(floor, int(worst * FEE_HISTORY_MARGIN))
    why = {
        "kind": kind,
        "budget_groth": budget,
        "floor_groth": floor,
        "observed": len(fees),
        "max_observed_groth": worst,
        "margin": FEE_HISTORY_MARGIN,
    }
    cache[kind] = (budget, why)
    return budget, why


def fee_budget_line(kind: str, budget: int, why: dict[str, Any]) -> str:
    """One human sentence saying where the number came from — the CLI and the hold reason share
    it, so an operator reading either sees the same derivation."""
    if why.get("observed"):
        return (
            f"{tg.fmt_groth(budget)} BEAM for a {kind} "
            f"({why['observed']} settled {kind}(s) read back, worst "
            f"{tg.fmt_groth(int(why['max_observed_groth']))} × {why['margin']}, floor "
            f"{tg.fmt_groth(int(why['floor_groth']))})"
        )
    return (
        f"{tg.fmt_groth(budget)} BEAM for a {kind} (no settled {kind} to learn from yet — the "
        f"{tg.fmt_groth(int(why['floor_groth']))} floor)"
    )


# ----------------------------------------------------------------------------- attribution


async def float_address() -> str:
    """The address whose BeamPay balance IS the shielded float — READ ONLY, never creates.

    `mp_address()` is the half that may CREATE and must PROVE; this is the half a gate reads,
    because a balance read must never have an address creation as a side effect. Empty means
    the float cannot be read at all, and a release holds rather than guessing at it."""
    want = (settings.beam_mp_address or "").strip()
    if want:
        return want
    row = await db().treasury.find_one({"_id": "mp_address"})
    return str((row or {}).get("address") or "")


# One max-privacy address per shield chunk means the registry only ever GROWS: a per-chunk
# address is credited when its withdrawal lands and is never debited, because a release
# registers its contract txid to the PRIMARY address (`float_address`) and every spend books
# there. The SUM stays right — the primary simply carries the debits and may go negative — but
# each float read costs one BeamPay balance call per registered address, so a long registry is
# a load problem and an operator's cue to consolidate. It is said out loud and never silently
# truncated: a read that stopped early would UNDERSTATE the float, and an understated float is
# a payout that holds for a reason nobody can see.
MP_REGISTRY_WARN = 200


async def mp_registry() -> list[str]:
    """Every max-privacy address the shielded float is spread across, PRIMARY FIRST.

    `PGAS_BEAM_MP_ADDRESS` (through `float_address`) is the first entry and is deliberately not
    required to be a row: it is the address a release BOOKS its flow to, so it is part of the
    float whether or not a chunk was ever sent to it. Everything after it is an `mp_addresses`
    row — one fresh address per shield chunk (`shield_target`) — oldest first, deduplicated.

    Cached per pass. READ ONLY: it never creates an address, because a balance read must never
    have an address creation as a side effect."""
    cached = _PASS.get("mp_registry")
    if cached is not None:
        return list(cached)
    out: list[str] = []
    primary = await float_address()
    if primary:
        out.append(primary)
    for row in await db().mp_addresses.find({}).sort("created_at", 1).to_list(None):
        addr = str(row.get("address") or "")
        if addr and addr not in out:
            out.append(addr)
    if len(out) > MP_REGISTRY_WARN:
        await tg.send(
            f"the shielded float is spread over {len(out)} max-privacy addresses and every "
            f"float read costs one BeamPay balance call per address — they want consolidating",
            key="mp-registry-size",
            cooldown_s=24 * 3600,
        )
    _PASS["mp_registry"] = list(out)
    return list(out)


async def float_groth(bp: beampay.BeamPay, asset: Asset) -> int:
    """THE SHIELDED FLOAT of one asset: the SUM over every registered max-privacy address.

    ⛔ IT IS NOT ONE ADDRESS'S BALANCE. Every shield chunk goes to a FRESH max-privacy address
    (`shield_target`, and the incident that made it necessary), so a float read that named only
    `PGAS_BEAM_MP_ADDRESS` would see one chunk of a three-chunk shielding and every payout
    would starve on value that is sitting there, shielded, in the addresses it did not ask
    about. `mp_addresses` is the registry that makes the sum knowable at all — which is why the
    address is written there BEFORE anything is sent to it.

    RAISES through `available_groth` when any one address cannot be read: an unreadable query
    is not evidence of anything and a partial sum is a number nobody measured. Cached per pass
    per asset."""
    cache: dict[int, int] = _PASS["float"]
    aid = int(asset.aid)
    if aid in cache:
        return cache[aid]
    total = 0
    for addr in await mp_registry():
        total += await bp.available_groth(addr, aid)
    cache[aid] = total
    return total


# ────────────────────────────────────────────── what the WALLET can spend, which is not the ledger

# The two buckets a crossing can be funded from, in the order a release prefers them. REGULAR
# FIRST because it carries no lock: a max-privacy output is unspendable for up to
# `MaxPrivacyLockTimeLimitHours` (Beam's default, 72) after it settles, and on this deployment
# the anonymity-set target can never be reached first — the shielded pool grows ~28 outputs a
# day. Shielded value is therefore value the product cannot pay out today.
SOURCE_REGULAR = "regular"
SOURCE_SHIELDED = "shielded"
SOURCES = (SOURCE_REGULAR, SOURCE_SHIELDED)


def spend_unshielded() -> bool:
    """May a crossing be funded from the treasury's UNSHIELDED balance? ONE reader.

    It decides three things that must never disagree: whether `payout_float` counts the
    treasury, whether `SOURCE_REGULAR` is offered at all, and whether the §9.3/S2 gate runs
    (`s2_unshielded_gate`). Read three ways, a deployment could count regular float, refuse to
    pick it, and then hold on S2 anyway — three consistent-looking gates and no payouts."""
    return bool(settings.payout_spend_unshielded)


def _looks_like_asset(row: dict[str, Any], aid: int) -> bool:
    """Is this totals row the one for `aid`? An id we cannot read is not a match — never an
    exception, because one malformed row must not hide every other asset's numbers."""
    try:
        return _groth(row.get("asset_id")) == aid
    except (TypeError, ValueError):
        return False


def _groth(v: Any) -> int:
    """A groth count from BeamPay/the wallet, which spells the same number as an int and as a
    string ⛔ and must never be read as 0 when it is unparseable."""
    if v is None:
        return 0
    return int(str(v).strip())


# The wallet's own words for a spendable coin and for a shielded one (read off the box
# 2026-09-10: `{"amount": …, "asset_id": 36, "type": "shld", "status": 3, "status_string":
# "maturing"}`). `norm` and `chng` are ordinary outputs; only status 1 can be spent.
UTXO_AVAILABLE = 1
UTXO_SHIELDED_TYPE = "shld"
BEAM_ASSET_ID = 0


def coin_capacity(amounts: Iterable[int], need: int) -> int:
    """How many INDEPENDENT transactions this set of coins can fund, at `need` groth each.

    ⛔ NOT `len(amounts)`, and not `sum // need` either. A Beam transaction may take SEVERAL
    inputs, so eight coins of 0.019 BEAM really can pay one 0.15 fee — but the coins it takes
    are locked while it is pending, so they cannot also pay the next one. What bounds
    concurrency is therefore the number of DISJOINT groups that each reach `need`, which is
    neither the count nor the total.

    Greedy, largest first, and deliberately so: this can only UNDER-count (coins [3,3,2,2] at
    need 5 answers 1 where 2 is possible), and an undercount defers a release for one pass
    while an overcount hands the wallet a send it cannot fund — which is the failure this whole
    reader exists to prevent (2026-09-10 10:30Z, two sends 0.7 s apart, both "Not enough
    inputs")."""
    if need <= 0:
        return sum(1 for _ in amounts)
    n = acc = 0
    for a in sorted((int(x) for x in amounts), reverse=True):
        acc += a
        if acc >= need:
            n, acc = n + 1, 0
    return n


# ⛔ **A REGULAR CROSSING SPENDS BEAM TWICE** (T40b F12). T40 gave every crossing an address of
# its own, which added a leg: BeamPay's `/withdraw` from the treasury pays a BEAM fee, and then
# the pipe invocation pays another. Beam locks a whole ordinary coin per pending transaction, so
# a crossing that is about to do both needs TWO free coins, not one — and the coin gate budgeted
# one, which is how a wallet with a single usable fee coin was admitted to a two-fee crossing.
FEE_COINS_PER_CROSSING = 2


def fee_coins_needed(row: dict[str, Any], source: str) -> int:
    """How many ORDINARY BEAM coins one crossing of this shape locks — ONE reader (law 9).

    Two for a regular-funded crossing that still has to fund its address; one once that transfer
    has been made (a retry re-enters with `fund_called_at` set and only the invocation left);
    and one for a shielded crossing, which has no funding leg at all."""
    if source != SOURCE_REGULAR:
        return 1
    return 1 if row.get("fund_called_at") else FEE_COINS_PER_CROSSING


async def coin_counts() -> dict[int, dict[str, Any]]:
    """`{asset_id: {"regular": n, "shielded": n}}` — how many SPENDABLE COINS the wallet holds.

    ⛔ **A COIN IS LOCKED BY THE TRANSACTION SPENDING IT, SO CONCURRENCY IS A COUNT, NOT A
    BALANCE.** 2026-09-10 10:30Z: two releases were submitted 0.7 s apart and both came back
    `Not enough inputs to process the transaction` (txids e370eec9…, 43b3ac73…, status 4). Every
    balance gate had passed; what the wallet did not have was a free input for the second one —
    and a contract invocation needs TWO free coins, one of the asset and one of BEAM for its own
    fee. The box holds two spendable BEAM coins (0.01 and 9.835) and no spendable bETH at all.

    Read from `beam.Wallet.utxos` — the one wallet-api read this project makes that is not a
    build or a submit, because BeamPay has no route that can answer it (see that method).
    RAISES what the wallet raised: an unreadable UTXO list is not "no coins" and not "plenty".
    Cached per pass — N releases are decided against one answer."""
    cached = _PASS.get("coins")
    if cached is not None:
        return {aid: dict(v) for aid, v in cached.items()}
    out: dict[int, dict[str, Any]] = {}
    for u in await beam.wallet().utxos():
        if _groth(u.get("status")) != UTXO_AVAILABLE:
            continue  # maturing, spent, in flight — not a coin a send can pick up
        aid = _groth(u.get("asset_id"))
        bucket = (
            SOURCE_SHIELDED
            if str(u.get("type") or "").lower() == UTXO_SHIELDED_TYPE
            else SOURCE_REGULAR
        )
        row = out.setdefault(
            aid,
            {
                SOURCE_REGULAR: 0,
                SOURCE_SHIELDED: 0,
                "amounts_regular": [],
                "amounts_shielded": [],
            },
        )
        row[bucket] += 1
        # the SIZES matter, not only the count: what bounds concurrency is how many DISJOINT
        # groups of these coins each reach what one transaction needs (`coin_capacity`)
        row[f"amounts_{bucket}"].append(_groth(u.get("amount")))
    _PASS["coins"] = {aid: dict(v) for aid, v in out.items()}
    return out


async def wallet_spendable(
    bp: beampay.BeamPay, asset: Asset, *, max_age_s: float | None = None
) -> dict[str, int]:
    """THE ONE READER of "what can the wallet spend RIGHT NOW", per asset and per bucket.

    ⛔ **A LEDGER BALANCE IS WHAT WE OWN; THIS IS WHAT THE WALLET CAN SPEND, AND THEY DIFFER.**
    2026-09-10 10:2xZ on the box: BeamPay's max-privacy registry summed **2,652,864 groth** of
    bETH across three addresses, and the wallet's own `/wallet_status.totals` for asset 36 said
    `available 0 · available_regular 0 · available_mp 0 · maturing_mp 1,652,864` — the three
    shield chunks had settled ten hours earlier and were still locked, and the wallet could not
    see the third coin at all (a rescan is pending). Every gate in `_payout_scheduled` reads
    BeamPay, so every one of them would have passed, and the release would have handed the
    wallet a send it cannot fund — which is the lost-response state the whole resolver exists to
    survive, entered on purpose.

    So spendability is asked of the WALLET, through BeamPay's `/wallet_status` proxy (law 10:
    BeamPay is still the only interface; this is a READ it exposes). The buckets:

      regular   `available_regular` — ordinary outputs, no lock
      shielded  `available_mp`      — Lelantus outputs whose max-privacy lock has expired
      maturing  `maturing_mp` (+ `maturing_regular`) — value that IS ours and cannot move yet

    RAISES when `wallet_status` cannot be read or carries no `totals` at all: an unreadable
    query is not evidence of anything (law 8) and certainly not "the wallet can spend nothing"
    — the caller HOLDS on it, which is a different decision from refusing a shortage. An asset
    with no row in a totals array that WAS read is a real zero: the wallet holds none of it.

    Cached per pass, for the reason every other float read is: N releases are about to be gated
    on one answer, and a number that changes between two orders of one pass is exactly the
    defect `inflight_groth` exists to prevent.

    ⛔ **`max_age_s` IS THE REQUEST PATH'S BOUND, AND IT NEVER OVERWRITES THE PASS'S ANSWER**
    (T52). `float_schedule` is read by `POST /v1/withdrawals/preview` on every keystroke, in the
    same process the processor loop runs in, and a route that replaced a cached number the
    current pass had already decided one release against would be doing precisely what the
    sentence above forbids. So a caller with a freshness bound may DECLINE a stale entry and
    read the wallet itself — and the answer it gets stays in `float_schedule`'s own cache, never
    in this one."""
    cache: dict[int, dict[str, int]] = _PASS["wallet"]
    stamped: dict[int, float] = _PASS["wallet_at"]
    aid = int(asset.aid)
    if aid in cache and (
        max_age_s is None or time.time() - float(stamped.get(aid) or 0) <= float(max_age_s)
    ):
        return dict(cache[aid])
    keep = aid not in cache  # never move a number the current pass is already deciding on
    st = await bp.wallet_status()
    totals = st.get("totals")
    if not isinstance(totals, list):
        raise beampay.BeamPayError(
            "wallet_status carried no `totals` — the wallet's spendable buckets could not be "
            "read, and 'we cannot see' is never 'there is nothing to spend'"
        )
    row: dict[str, Any] = {}
    for t in totals:
        if isinstance(t, dict) and _looks_like_asset(t, aid):
            row = t
            break
    # ⛔ A SHAPE WE DO NOT UNDERSTAND IS NOT A NUMBER — and above all it is not 0. A bucket the
    # wallet spelled as something unparseable is refused the way an unreadable answer is: the
    # caller HOLDS. Reading it as 0 would be safe; reading it as anything else would not, and a
    # bare ValueError out of a money gate is a traceback where a decision belongs.
    try:
        available = _groth(row.get("available"))
        shielded = _groth(row.get("available_mp"))
        # `available_regular` is the wallet's own split and is preferred; the subtraction is the
        # fallback for a build that does not publish it, and is clamped because a total below
        # its own max-privacy half is not a negative bucket, it is a shape we cannot read.
        regular = (
            _groth(row["available_regular"])
            if "available_regular" in row
            else max(0, available - shielded)
        )
        maturing_mp = _groth(row.get("maturing_mp"))
        maturing_regular = _groth(row.get("maturing_regular"))
    except (TypeError, ValueError) as e:
        raise beampay.BeamPayError(
            f"wallet_status.totals for asset {aid} carried a bucket this reader cannot parse "
            f"({e}) — the wallet's spendable balance could not be read"
        ) from e
    out: dict[str, Any] = {
        "regular": regular,
        "shielded": shielded,
        "maturing_regular": maturing_regular,
        "maturing_mp": maturing_mp,
        "maturing": maturing_regular + maturing_mp,
        "locked": _groth(row.get("locked")),
    }
    # …AND HOW MANY COINS THOSE BUCKETS ARE. A balance says what one transaction may carry; the
    # COUNT says how many transactions can be in flight at once, because Beam locks a whole UTXO
    # per pending transaction and every contract invocation also needs a BEAM coin for its fee.
    # `None` is "we could not read the list" — never 0 and never "as many as you like": the
    # caller HOLDS on it, and the reason it writes is the wallet's own words.
    #
    # ⛔ IT USED TO DEFER, SILENTLY (2026-09-10). A wallet-api that was down — or answering a
    # shape this reader cannot parse — stopped every payout with no row, no reason and no page,
    # for ever: `counts = None` and a `log.info` in the caller. A guard that fails politely is a
    # guard that fails (law 11), and a refusal nothing records is unalertable (law 12). The read
    # is attempted ONCE per pass per asset (this whole answer is cached in `_PASS["wallet"]`),
    # so N due orders each write their own row against ONE attempt and the digest pages once.
    coins_error: str | None = None
    try:
        counts = await coin_counts()
    except (beam.BeamError, TypeError, ValueError) as e:
        # a read that moves nothing must not take the pass down, and a coin list we cannot parse
        # is exactly as unknown as one we could not fetch — the caller HOLDS on both
        log.warning("the wallet's coin list could not be read (%s) — concurrency unknown", e)
        counts, coins_error = None, f"{type(e).__name__}: {beam.redact(e)}"
    asset_coins = (counts or {}).get(aid, {})
    beam_coins = (counts or {}).get(BEAM_ASSET_ID, {})
    out["coins_regular"] = None if counts is None else int(asset_coins.get(SOURCE_REGULAR, 0))
    out["coins_shielded"] = None if counts is None else int(asset_coins.get(SOURCE_SHIELDED, 0))
    # ⛔ A FEE COIN IS A COIN BIG ENOUGH TO PAY THE FEE. The fee always comes out of an
    # ORDINARY BEAM coin (a shielded BEAM output cannot pay one), and it comes out of ONE — so
    # a coin below what a call costs is not a fee coin at all. The box holds 0.01 BEAM and
    # 9.835 BEAM: two coins, ONE of which can fund an invocation whose budget is 0.15. Counting
    # both would admit a second release and hand it the "Not enough inputs" this exists to stop.
    # The threshold is the SAME derived budget `_beam_fees_ok` reserves — one reader, per pass.
    out["amounts_regular"] = None if counts is None else list(
        asset_coins.get("amounts_regular", [])
    )
    # a shielded spend takes shielded inputs; their sizes are not published per coin by
    # `get_utxo`'s shielded rows in any way this reader needs beyond the amount it does carry
    out["amounts_shielded"] = None if counts is None else list(
        asset_coins.get("amounts_shielded", [])
    )
    if counts is None:
        out["fee_coins"] = None
        out["fee_budget_groth"] = None
    else:
        try:
            need_fee, _why = await fee_budget("send", bp)
        except Exception as e:  # noqa: BLE001 — a budget we cannot derive falls back to the floor
            log.warning("fee budget for the coin count unreadable (%s) — using the floor", e)
            need_fee = fee_floor("send")
        out["fee_coins"] = coin_capacity(beam_coins.get("amounts_regular", []), int(need_fee))
        out["fee_budget_groth"] = int(need_fee)
    # …and WHY they could not be counted, when they could not. It travels on the one answer
    # because the hold the caller writes has to carry the wallet's own words, and re-deriving
    # them at the call site would be a second reader of one fact (law 9).
    out["coins_error"] = coins_error
    if keep:
        cache[aid] = dict(out)
        stamped[aid] = time.time()
    return out


def inflight_release_query(asset: Asset) -> dict[str, Any]:
    """Releases whose Beam transaction is not mined yet — each is holding a coin of the asset
    AND a BEAM coin for its fee. `kernel_at` is stamped only once the kernel confirmed and the
    ledger release was booked, so its absence is exactly "this transaction still owns inputs"."""
    return {"status": {"$in": list(INFLIGHT)}, "kernel_at": {"$exists": False}, **_asset_match(asset)}


async def inflight_releases(asset: Asset) -> int:
    """How many crossings are currently holding wallet inputs. Counted by the DATABASE — an
    order admitted earlier in THIS pass is already `releasing`, so it counts itself in."""
    return int(await db().payout_requests.count_documents(inflight_release_query(asset)))


async def payout_float(bp: beampay.BeamPay, asset: Asset) -> dict[str, int]:
    """`{"shielded", "regular", "total"}` — THE float a release may be funded from, by source.

    The shielded half is `float_groth` (the sum over the max-privacy registry). The regular half
    is the treasury's own unshielded balance and is counted ONLY under `spend_unshielded()`:
    with the flag off this returns exactly what the float meant before 2026-09-10, so a
    deployment that wants the stricter privacy posture is unchanged rather than merely
    discouraged.

    It is BEAMPAY's numbers — the ledger of record, what we OWN. What the wallet can SPEND is
    `wallet_spendable`, and a release has to pass both: the ledger says the value is ours, the
    wallet says it can move today. Cached per pass per asset."""
    cache: dict[int, dict[str, int]] = _PASS["float_parts"]
    aid = int(asset.aid)
    if aid in cache:
        return dict(cache[aid])
    shielded = await float_groth(bp, asset)
    regular = (
        await bp.available_groth(beampay.treasury_address(), aid) if spend_unshielded() else 0
    )
    # ⛔ **A CHUNK ALREADY HANDED TO A SHIELD `/withdraw` IS NOT FLOAT ANY MORE** — broadcast ≠
    # done, one level earlier. BeamPay QUEUES a withdrawal and its daemon emits the transaction
    # seconds later (the gap `_treasury_shielding` waits out at `called_at` rather than calling
    # again), and until then the treasury's `available_groth` still carries the chunk. Counted
    # here it is float twice over: the release it admits meets a wallet whose value is already
    # committed to a 72-hour Lelantus lock. Clamped at 0 — a subtraction larger than the balance
    # is a shape we cannot interpret, never a negative float.
    if regular:
        regular = max(0, regular - await queued_shield_groth(asset))
    # ⛔ **…AND WHAT HAS ALREADY LEFT THE TREASURY FOR A CROSSING OF ITS OWN IS STILL OURS**
    # (T40b F5/F11). A funded crossing address holds exactly `amount + relayerFee` from the
    # moment its `/withdraw` settles until the send burns it, and it is in NEITHER of the two
    # buckets above: the treasury's balance has fallen and the max-privacy registry never held
    # it. Left out, the float appears to shrink by a whole crossing the instant one is funded,
    # while `inflight_groth` — which now counts that same funded order — reserves it as well:
    # the order's money would be subtracted twice and the queue behind it would stall on a
    # shortage that does not exist. Counted here and reserved there, the two cancel exactly, and
    # the order's own retry (which excludes itself from the reservation) finds its funding where
    # it left it.
    crossing = await queued_crossing_groth(asset)
    regular += crossing
    out = {
        "shielded": shielded,
        "regular": regular,
        "crossing": crossing,
        "total": shielded + regular,
    }
    cache[aid] = dict(out)
    return out


async def queued_shield_groth(asset: Asset) -> int:
    """Σ over the shield chunks whose `/withdraw` HAS been called and whose transaction is not
    on record yet — treasury value that is spoken for and that BeamPay's balance still counts.

    A chunk whose transaction IS on record has already left the treasury's `available` (BeamPay
    reserves at withdrawal-request time, `type: "withdrawal"`, before the send), so only the
    chunks past `len(shield_txids)` are double-counted — and `_treasury_shielding` sends
    strictly in order, so those are exactly the slots whose `at` is stamped and whose
    transaction `_shield_scan` has not found yet.

    ⛔ No page, no ordering, no limit: a guard that admits a spend must never depend on a
    window (`inflight_groth`, `txid_is_taken`, `scheduled_liability_groth` — the same law)."""
    total = 0
    rows = await db().deposits.find(
        {"treasury": "shielding", **_asset_match(asset)}
    ).to_list(None)
    for dep in rows:
        plan = list(dep.get("shield_plan") or [])
        on_record = len(dep.get("shield_txids") or [])
        for k, slot in enumerate(shield_calls_of(dep)):
            if k < on_record or k >= len(plan):
                continue  # its transaction exists: the balance has already fallen
            if float(slot.get("at") or 0) > 0:
                total += int(plan[k])
    return total


def crossing_pipeline(asset: Asset) -> list[dict[str, Any]]:
    """The aggregation `queued_crossing_groth` runs — exposed so a test can assert on the
    QUESTION rather than on a mongomock double's answer to it."""
    match: dict[str, Any] = {
        "$and": [
            # the treasury has moved (or is moving) this order's crossing to an address of its
            # own…
            {"fund_called_at": {"$exists": True}},
            # …and nothing has burned it: `kernel_at` is stamped only once the crossing's kernel
            # confirmed and its ledger release was booked, so its ABSENCE is exactly "the value
            # is still sitting at that address". A dead attempt keeps its `beam_txid` until the
            # retry clears it and the bETH never moved, so the txid cannot be the test.
            {"kernel_at": {"$exists": False}},
            {
                "$or": [
                    {"status": {"$in": ["scheduled", DELAYED]}},
                    # a row parked for a human is the one MOST likely to be holding value at an
                    # address nobody is watching — it is exactly what this reader is for
                    {"status": HELD, "held_from": {"$in": ["scheduled", DELAYED]}},
                ]
            },
            _asset_match(asset),
        ]
    }
    return [
        {"$match": match},
        {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$fund_groth", 0]}},
                    "orders": {"$sum": 1}}},
    ]


async def queued_crossing_groth(asset: Asset) -> int:
    """Σ over the crossing addresses that have been funded and not yet burned — value that IS
    ours, is committed to one order each, and that no other float reader can see.

    ⛔ **A BUCKET NOBODY READS IS A BUCKET NOBODY NOTICES IS STUCK** (T40b F11). `payout_float`
    sums the treasury and the max-privacy registry; a crossing's own fresh address is in
    neither, so from the moment its funding settles to the moment the send burns it, that value
    is invisible — and an order held for a human between those two points strands it silently.

    Derived from the ROWS and not from N BeamPay balance calls, for `crossing_fee_debt_groth`'s
    reason: the number is the same one (the address is funded with exactly `fund_groth` and
    nothing else ever touches it), and a reader whose cost grows with the number of crossings we
    have ever made is a reader that eventually times out inside a money gate.

    ⛔ No page, no ordering, no limit: a guard that admits a spend must never depend on a window
    (`inflight_groth`, `txid_is_taken`, `queued_shield_groth` — the same law)."""
    rows = await db().payout_requests.aggregate(crossing_pipeline(asset)).to_list(1)
    return int((rows[0] if rows else {}).get("total") or 0)


async def crossing_health(asset: Asset | None = None) -> dict[str, Any]:
    """`{"orders": n, "oldest_age_s": s}` — what an unauthenticated caller may know about the
    crossings in flight, and not one fact more.

    ⛔ **COUNTS, NEVER AMOUNTS** (T34b M5, the same law that took the distributor's float off
    /v1/health). How much value is sitting in our wallet right now is an inventory; how many
    orders are mid-crossing, and how long the oldest has been there, is what a watchdog needs to
    notice one that is stuck. The groth figure is in the key-protected admin panel."""
    a = asset or get_asset("ETH")
    rows = await db().payout_requests.aggregate(
        [
            crossing_pipeline(a)[0],
            {"$group": {"_id": None, "orders": {"$sum": 1},
                        "since": {"$min": "$fund_called_at"}}},
        ]
    ).to_list(1)
    got = rows[0] if rows else {}
    since = float(got.get("since") or 0)
    return {
        "orders": int(got.get("orders") or 0),
        "oldest_age_s": int(time.time() - since) if since > 0 else 0,
    }


# ⛔ A RELEASED PAYOUT IS NOT A LIABILITY THE FLOAT STILL OWES. `releasing` and everything after
# it is money already committed to the chain, and that is `inflight_groth`'s territory: counting
# it here too would reserve one payout twice and stop the treasury ever shielding again.
# …and a DELAYED order is a scheduled one with a reason: nothing was sent (or what was sent is
# proven dead), the ledger debit stands, and the float still has to be able to pay it.
LIABLE = ("scheduled", DELAYED, *PAYOUT_DARK)


def scheduled_liability_pipeline(asset: Asset) -> list[dict[str, Any]]:
    """The aggregation `scheduled_liability_groth` runs — exposed so a test can assert on the
    QUESTION rather than on a mongomock double's answer to it."""
    owed: list[dict[str, Any]] = [
        {"status": {"$in": list(LIABLE)}},
        # a row parked for a human that never LEFT `scheduled` still owes its user the money:
        # nothing was sent, the ledger debit stands, and the float has to be able to pay it
        {"status": HELD, "held_from": {"$in": list(LIABLE)}},
    ]
    return [
        {"$match": {"$and": [{"$or": owed}, _asset_match(asset)]}},
        {
            "$group": {
                "_id": None,
                "total": {
                    "$sum": {
                        "$add": [
                            {"$ifNull": ["$amount_groth", 0]},
                            {"$ifNull": ["$bridge_fee_groth", 0]},
                        ]
                    }
                },
            }
        },
    ]


async def scheduled_liability_groth(asset: Asset) -> int:
    """Σ(amount + the bridge fee it funded) over every payout that is scheduled and NOT released.

    This is what the treasury has already promised and has not yet spent — the number the shield
    policy has to leave unshielded, because value put into the max-privacy pool is value no
    payout can spend for up to three days. A held-from-scheduled row counts: it was never sent.

    ⛔ **A GUARD THAT ADMITS A SPEND MUST NEVER DEPEND ON A WINDOW** — the database sums it, no
    page, no ordering, no limit (the identical defect `txid_is_taken` and `inflight_groth`
    record as fixed, on the guard that decides whether the treasury may lock its own float up)."""
    rows = await db().payout_requests.aggregate(
        scheduled_liability_pipeline(asset)
    ).to_list(1)
    return int((rows[0] if rows else {}).get("total") or 0)


def liability_reserve_groth(liability: int) -> int:
    """The liabilities grossed up by `PGAS_SHIELD_LIABILITY_BUFFER_BPS`. ONE reader, so the hold
    reason an operator reads and the number the chunk was refused against cannot drift.

    The buffer is what a gas tick between a quote and a release costs: the bridge fee on a row
    was measured when the order was written, and the crossing is paid for at release."""
    bps = max(0, int(settings.shield_liability_buffer_bps or 0))
    return -(-int(liability) * (10_000 + bps) // 10_000)  # ceil, in integers


# ═══════════════════ T52 — THE FLOAT SCHEDULE: WHAT CAN MOVE NOW, AND WHEN THE REST CAN ═══════
#
# Admin, 2026-09-10 15:38Z: *"Why do you accept user request if user cannot spend this?"* —
# acceptance read the USER's ledger balance and nothing else, so three orders were taken for a
# treasury that could move 0.00775651 ETH and was holding 0.01652864 more inside a max-privacy
# lock with no early exit. The release gate then said so, correctly, once an hour, in the
# operator's vocabulary, on the user's page.
#
# ⛔ **ONE READER, TWO CALLERS** (law 9). `routers/withdrawals` asks it whether an order can be
# ACCEPTED and `_refuse` asks it when a float-short order can be EXPECTED; a second implementation
# of "what can the treasury move" would eventually accept what the release refuses, which is the
# defect this whole task exists to remove.
#
# The three numbers it is made of, and why each is the one it is:
#
#   spendable   `wallet_spendable` PER SOURCE, not the ledger's balance and not their sum: a
#               crossing is funded from ONE address, so what bounds a single order is the larger
#               bucket, and what bounds a BATCH is the two of them spent in turn.
#   committed   the crossings already in flight PLUS every scheduled-but-unreleased order. ⚠️
#               The release gate reserves only the first (`inflight_groth`) — at release time the
#               other scheduled orders have committed nothing and it is first come, first served.
#               ACCEPTANCE has to reserve both, or the same groth is promised to two people.
#   maturing    the shield chunks still inside the 72-hour Lelantus lock, with the moment each
#               one ends. The AMOUNT is the wallet's (`maturing_mp` — it is the authority on how
#               much is locked); the TIMES come from the chunks that made them, and the list is
#               capped at the wallet's number so a row we cannot explain never becomes a promise.
MAX_PRIVACY_LOCK_S = 72 * 3600
# How stale an answer a REQUEST may be served. The processor passes no bound at all (one pass,
# one answer); a form being typed into gets a wallet read at most this often.
FLOAT_SCHEDULE_TTL_S = 30.0
_FLOAT_SCHED: dict[int, dict[str, Any]] = {}


def _sched_copy(data: dict[str, Any]) -> dict[str, Any]:
    """The cached answer, deep enough that a caller's fold cannot edit the cache."""
    return {
        **data,
        "sources": dict(data["sources"]),
        "maturing": [dict(m) for m in data["maturing"]],
    }


def crossing_need_groth(delivered_groth: int, bridge_fee_groth: int) -> int:
    """What the TREASURY has to be able to move for one order — ONE reader.

    The delivery plus the crossing's own relayer fee (the send burns both) plus the float
    reserve. `_payout_scheduled` computes it with the LIVE relayer fee, the request path with the
    fee the order is being charged; the shape is the same, so the number the user is accepted
    against and the number the release is gated on cannot drift apart."""
    return int(delivered_groth) + int(bridge_fee_groth) + int(settings.float_min_groth or 0)


async def maturing_schedule(asset: Asset, locked_groth: int) -> list[dict[str, Any]]:
    """Every shield chunk still inside its max-privacy lock, soonest first, capped at what the
    WALLET says is locked.

    ⛔ The chunk rows say WHEN (their `/withdraw` was called, plus the 72-hour lock — at most a
    minute early, which is the safe direction for a promise phrased "at the latest"); the wallet
    says HOW MUCH. Neither is asked the other's question: a chunk whose value the wallet cannot
    see is not float, and a locked balance no chunk explains gets no date at all."""
    if int(locked_groth) <= 0:
        return []
    now = time.time()
    found: list[dict[str, Any]] = []
    rows = await db().deposits.find(
        {"treasury": {"$in": ["shielding", "shielded"]}, **_asset_match(asset)}
    ).to_list(None)
    for dep in rows:
        plan = list(dep.get("shield_plan") or [])
        for k, slot in enumerate(shield_calls_of(dep)):
            if k >= len(plan):
                continue
            at = float(slot.get("at") or 0)
            if at <= 0:
                continue
            unlocks = at + MAX_PRIVACY_LOCK_S
            if unlocks <= now:
                continue  # its lock has already expired; it is spendable, not maturing
            found.append({"groth": int(plan[k]), "unlocks_at": unlocks})
    found.sort(key=lambda m: float(m["unlocks_at"]))
    out: list[dict[str, Any]] = []
    total = 0
    for m in found:
        if total >= int(locked_groth):
            break
        take = min(int(m["groth"]), int(locked_groth) - total)
        out.append({"groth": take, "unlocks_at": float(m["unlocks_at"])})
        total += take
    return out


async def _float_schedule(asset: Asset, bp: beampay.BeamPay | None) -> dict[str, Any] | None:
    if not (settings.beam_treasury_address or "").strip():
        return None  # no Beam side configured: nothing to measure, and never a zero
    spendable = await wallet_spendable(bp or beampay.beampay(), asset, max_age_s=FLOAT_SCHEDULE_TTL_S)
    committed = int(await inflight_groth(asset)) + int(await scheduled_liability_groth(asset))
    sources = {
        SOURCE_REGULAR: max(0, int(spendable[SOURCE_REGULAR]) - committed),
        SOURCE_SHIELDED: max(0, int(spendable[SOURCE_SHIELDED]) - committed),
    }
    locked = int(spendable.get("maturing") or 0)
    maturing = await maturing_schedule(asset, locked)
    return {
        # what ONE order may draw on right now: a crossing is funded from one address, so this
        # is the larger bucket and never the sum of the two.
        "spendable_now_groth": max(sources.values()),
        "sources": sources,
        "committed_groth": committed,
        "pipeline_groth": committed,
        "maturing_groth": locked,
        "maturing": maturing,
        "next_unlock_at": float(maturing[0]["unlocks_at"]) if maturing else None,
    }


async def float_schedule(
    asset: Asset, bp: beampay.BeamPay | None = None, *, max_age_s: float | None = None
) -> dict[str, Any] | None:
    """`{spendable_now_groth, sources, committed_groth, pipeline_groth, maturing[], maturing_groth,
    next_unlock_at}` — or **None**, which is "we could not measure it".

    ⛔ NONE IS NOT ZERO (law 8). An unreadable wallet is not an empty treasury: the caller that
    accepts orders lets them through (the release gate will hold them honestly and say when), and
    the caller that writes a hold simply leaves the date off its sentence. Refusing every
    withdrawal because BeamPay hiccuped would be a guess in the other direction, and just as wrong.

    Never raises: a reader whose failure mode is an exception ends up wrapped in a bare `except`
    at each call site, which is the shape law 11 is about."""
    aid = int(asset.aid)
    hit = _FLOAT_SCHED.get(aid)
    if (
        hit is not None
        and max_age_s is not None
        and time.time() - float(hit["at"]) <= float(max_age_s)
    ):
        got = hit.get("data")
        return _sched_copy(got) if got else None
    data: dict[str, Any] | None = None
    try:
        data = await _float_schedule(asset, bp)
    except Exception as e:  # noqa: BLE001 — an unmeasurable float is a `None`, never a traceback
        log.info(
            "the treasury float schedule could not be read (%s: %s)",
            type(e).__name__,
            beam.redact(e),
        )
    _FLOAT_SCHED[aid] = {"at": time.time(), "data": data}
    return _sched_copy(data) if data else None


async def next_unlock_at(row: dict[str, Any]) -> float | None:
    """When the treasury expects to be able to pay this order at the latest, or None."""
    sched = await float_schedule(_asset_of(row))
    return float((sched or {}).get("next_unlock_at") or 0) or None



async def register_attribution(
    coll: str,
    row_id: str,
    txid: str,
    address: str,
    trade_ref: str,
    kind: str,
    id_key: str,
) -> bool:
    """Register a contract txid with BeamPay, and write the outcome ON the row.

    ⛔ **A record that must exist on every path belongs in a helper called from every path.**
    Every `process_invoke_data` this project makes produces a txid whose whole flow — the asset
    AND the fee — books to `__house__` unless it is claimed here. Without the claim the
    treasury's balance never moves, the float every payout gates on is wrong, and the repair is
    the two-phase `/internal/ledger/adjust` route that can be refused AFTER the money moved
    (BeamPay incident cc64eb41cc5a).

    Idempotent on the txid, which is why retrying is safe and why a lost reply is not a
    disaster: the authority on what happened is the tx (`attributed_to` on
    `GET /internal/contract_tx`), never this reply.

    Returns True when the registration is on record. False means the caller must HOLD and retry
    THE REGISTRATION — never the send: the kernel exists, and a second `process_invoke_data`
    would be a second signature over one inventory.

    One refusal class is deliberately not retried: `tx_already_booked` and its siblings are
    conflicts with something already on record, which no retry can ever clear. Those are
    recorded, paged IMMEDIATELY (an operator owes an attribution repair) and allowed to pass,
    because holding a crossing that has already happened strands the user's money to fix a
    bookkeeping problem in another system."""
    bp = beampay.beampay()
    try:
        res = await bp.expect_contract_tx(txid, address, trade_ref)
    except beampay.BeamPayError as e:
        terminal = str(e.detail) if e.detail is not None else ""
        if terminal in beampay.TERMINAL_EXPECT_REFUSALS:
            await _set(
                coll,
                row_id,
                attribution={
                    "txid": txid,
                    "address": address,
                    "trade_ref": trade_ref,
                    "refused": terminal,
                    "at": time.time(),
                },
            )
            await tg.alert(
                kind,
                f"BeamPay REFUSED the attribution of this contract transaction ({terminal}) — "
                f"the crossing itself stands, but its flow is booked to __house__ and an "
                f"operator owes an attribution repair",
                **{id_key: row_id},
            )
            return True
        await _set(coll, row_id, attribution_error=beam.redact(e)[:300], attribution_at=time.time())
        await tg.alert(
            kind,
            f"BeamPay did NOT accept the attribution of this contract transaction "
            f"({beam.redact(e)[:160]}). The transaction is on the record and is NEVER re-sent; "
            f"the registration is what retries",
            **{id_key: row_id},
        )
        return False
    await db()[coll].update_one(
        {"_id": row_id},
        {
            "$set": {
                "attribution": {
                    "txid": txid,
                    "address": address,
                    "trade_ref": trade_ref,
                    "replayed": bool(res.get("replayed")),
                    "at": time.time(),
                },
                "updated_at": time.time(),
            },
            "$unset": {"attribution_error": "", "attribution_at": ""},
        },
    )
    return True


async def attribution_ready(bp: beampay.BeamPay, coll: str, row_id: str) -> bool:
    """Can this deployment register a contract txid at all — and does our key reach the route?

    ⛔ **ASK BEFORE THE IRREVERSIBLE STEP, NOT AFTER IT.** `_payout_scheduled` gated on the
    flag, the kill switch, the relayer's share and subsidy, the float, the unshielded balance
    and the BEAM fee budget — and then burned the bETH, and only THEN discovered that
    `POST /internal/expect_contract_tx` was unreachable (`PGAS_BEAMPAY_INTERNAL_KEY` unset, a
    key with no `ledger:adjust` scope → 403, or a BeamPay whose build has no `/internal/*`
    routes at all — master `e09bfc2`, the commit this deployment was copied from, has none).
    Once BeamPay's daemon settles that transaction with no expectation on record the whole flow
    books to `__house__` and every later retry gets the terminal `tx_already_booked`: the repair
    is then the two-phase `/internal/ledger/adjust` route, which can be refused AFTER the money
    moved. `settings.secrets_ok` checks neither key, so nothing at boot notices.

    The probe is side-effect-free (a GET; POSTing a real registration would leave a claim behind
    on a txid that may never exist) and it is the SAME call, with the SAME key, on the SAME
    route the money path uses — §8, the prober must call the way the caller calls. Asked once
    per pass, because N releases are about to be made against one answer."""
    state = _PASS.get("expect_route")
    if state is None:
        try:
            res = await bp.expectation_route_ready()
            ok = bool(res.get("available"))
            why = "" if ok else "BeamPay reports direct attribution is not available here"
        except beampay.BeamPayError as e:
            ok, why = False, beam.redact(e)[:200]
        state = _PASS["expect_route"] = (ok, why)
    ok, why = state
    if not ok:
        await _hold(
            coll,
            row_id,
            f"BeamPay cannot accept a contract-tx registration ({why}) — refusing to sign "
            f"anything whose txid could not then be registered, because an unregistered "
            f"contract flow books to __house__ and cannot be repaired without an operator",
            key=f"attr-route:{row_id}",
        )
    return ok


def attributed(row: dict[str, Any], txid: str) -> bool:
    """True when THIS txid is the one already registered on this row. A registration for a
    different txid is not this crossing's evidence."""
    att = row.get("attribution") or {}
    return str(att.get("txid") or "") == str(txid)


def funds_its_own_crossing(row: dict[str, Any]) -> bool:
    """True when this order was written under the 2026-09-10 model and PAID for its crossing.

    THE ONE DISCRIMINATOR between the two fee models, because the two gates below disagree
    about which rows they apply to and a second reading of "is this a modern row" would put
    them out of step. `bridge_fee_groth` is `ceil(relayer_fee_now × headroom)` with the live
    fee refused at 0 (routers/withdrawals.live_fees), so every row written since carries a
    positive one and no row written before carries the field at all."""
    return int(row.get("bridge_fee_groth") or 0) > 0


def bridge_budget_groth(row: dict[str, Any]) -> int:
    """What THIS order funded its crossing with — the number the subsidy gate measures against.

    ONE READER (law 9). Since 2026-09-10 the bridge fee is charged to the user EXPLICITLY and
    stored as `bridge_fee_groth`, quoted at request time from the live gas price plus the
    headroom the wait needs (routers/withdrawals.fee_triple); `fee_groth` is our 2% and is
    revenue, not a budget for a crossing. A row written before that change carries no
    `bridge_fee_groth`, and under the model it was written under its `fee_groth` WAS the budget —
    so that is what the gate reads for it. Two readers of this would disagree about exactly the
    rows nobody looks at, on the one gate that stops the treasury crossing at a loss."""
    if funds_its_own_crossing(row):
        return int(row["bridge_fee_groth"])
    return int(row.get("fee_groth") or 0)


def _positive_int(v: Any) -> int:
    """A groth figure off a row, or 0 — never a guess, never a NaN, never a string.

    ⛔ THE TOLERANCE IS THE POINT (law 8). Both readers below decide how much money moves, and
    "the field was missing / unreadable" must land on 0 — i.e. *do nothing* — rather than on an
    exception on the settlement path or, far worse, on an arithmetic that treats an unreadable
    cost as a cost of zero."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return 0
    if isinstance(v, float) and not math.isfinite(v):
        return 0
    return int(v) if int(v) > 0 else 0


def bridge_fee_refund_groth(row: dict[str, Any]) -> int:
    """What this order OVERPAID for its crossing — the number the settlement refunds (T45).

    `bridge_fee_groth` is what the user was charged for the crossing at request time (a live gas
    read × the headroom the wait needs); `relayer_fee_groth` is what actually went INTO the send,
    pinned at funding and never re-priced. "The bridge at cost" is a claim about the difference,
    and until 2026-09-10 the difference stayed with the treasury: two live orders funded 14,733
    groth against a 12,778-groth crossing and 1,955 groth of each user's money was kept for a
    cost nobody incurred.

    ⛔ **NEVER NEGATIVE, AND NEVER ON A COST WE CANNOT READ.** A row with no `relayer_fee_groth`
    has not crossed yet, or crossed under a build that did not record it — read as a zero it
    would refund the WHOLE bridge fee of a crossing we really paid for, on the one path that puts
    money back into Available. A crossing that cost MORE than it funded refunds nothing: we
    absorb it (`relayer_subsidy_groth`), and nothing is ever charged beyond the quote."""
    funded = _positive_int(row.get("bridge_fee_groth"))
    paid = _positive_int(row.get("relayer_fee_groth"))
    if not funded or not paid:
        return 0
    return max(0, funded - paid)


def relayer_subsidy_groth(row: dict[str, Any]) -> int:
    """What the TREASURY absorbed on this crossing — the mirror of the refund, and a loss.

    `PGAS_MAX_RELAYER_SUBSIDY` is how much more than the funded bridge fee we will still cross
    for (4× since T45), and d028 is why it exists: funded 14,770, the crossing wanted 24,299, and
    only the subsidy delivered it. Written down on the row so the loss is a number an operator
    can add up — an absorbed cost that appears nowhere is a decision nobody can review."""
    funded = _positive_int(row.get("bridge_fee_groth"))
    paid = _positive_int(row.get("relayer_fee_groth"))
    if not funded or not paid:
        return 0
    return max(0, paid - funded)


# ───────────────────────────────────────────────────────── the gas basis a crossing is priced on
#
# ⛔ **ONE SAMPLE IS NOT A PRICE** (T45, admin 2026-09-10: *"Make sure you have correct gas
# fees"*). `beam.relayer_fee_groth` reads `eth_feeHistory` ONCE — `beam.max_gas_price_gwei`:
# baseFee(of the block being built) × 2 + the median 50th-percentile tip over 10 blocks — and
# multiplies the relayer's 120,000-gas sum by `PGAS_RELAYER_FEE_MARGIN` (1.5). That is a single
# reading of a number that moved 0.66 → 2.18 gwei inside an hour on 2026-09-10: two orders were
# quoted 14,733 groth in the trough, and d028 met a 24,299-groth crossing at its release.
#
# So the basis is a rule with a memory, written down once here:
#
#     basis = max(the live eth_feeHistory read, the 24 h p75 of `gas_samples`)
#     fee   = the relayer's own arithmetic at that basis × PGAS_RELAYER_FEE_MARGIN, then floored
#
# ONE WRITER for the series (law 9): `record_gas_sample`, called once per deposit-watcher pass
# and from nowhere else. Everything that prices a crossing READS. The series carries a 48 h TTL
# (`config.GAS_SAMPLE_TTL_S`) — deliberately longer than the 24 h window it is read over, so the
# evidence that priced a crossing outlives its own use — and only COUNTS of it are published on
# /v1/health.
#
# ⚠️ THIS IS ONLY HONEST BECAUSE THE DIFFERENCE COMES BACK. A conservative basis over-collects
# by design; `bridge_fee_refund_groth` credits every groth the crossing does not spend back to
# the user's Available at settlement. Neither half may be weakened without the other.


async def record_gas_sample(rpc: Any) -> dict[str, Any] | None:
    """Append ONE gas reading to the series. THE ONLY WRITER — see the block above.

    ⛔ **AN UNREADABLE ENDPOINT IS NOT A GAS PRICE** (law 8) and it is not a reason to stop the
    deposit scan this runs inside either: a read that will not answer writes no row and returns
    None with the reason logged. A DATABASE failure is a different thing and is left to raise —
    `workers.run_forever` logs and pages it, which is what a series nobody is writing deserves.

    Both stamps are on the row on purpose: `at` is a tz-aware datetime because a Mongo TTL index
    only fires on a BSON date, and `at_s` is epoch seconds because that is what every other row
    in this app measures a window with."""
    try:
        gwei = float(await beam.max_gas_price_gwei(rpc))
    except Exception as e:  # noqa: BLE001 — named, never a zero and never a stalled pass
        log.info("gas sample skipped: %s: %s", type(e).__name__, e)
        return None
    if not (gwei > 0):
        log.info("gas sample skipped: eth_feeHistory gave %r", gwei)
        return None
    now = time.time()
    doc = {"at": dt.datetime.fromtimestamp(now, tz=dt.UTC), "at_s": now, "gwei": gwei}
    await db()[GAS_SAMPLES].insert_one(dict(doc))
    return doc


async def gas_p75_gwei(now: float | None = None) -> float:
    """The `GAS_BASIS_PERCENTILE`-th percentile of the last `GAS_BASIS_WINDOW_S` of samples, in
    gwei — or **0.0 when the series says nothing**, which is not a floor and never a refusal.

    NEAREST RANK, stated as arithmetic so it cannot drift between a reader and a report: sort
    ascending, take index `ceil(p/100 × n) − 1`. No interpolation, no `statistics` variant whose
    definition changes what an operator's own `sort | awk` would say.

    A box that has just booted has an empty series and prices at exactly what it can measure —
    the behaviour before T45. An unreadable series must never quote a crossing at 0 (that is the
    `live_fees` refusal's law, one door along), and it never can here: 0.0 is only ever a FLOOR
    that does not bind, never a price."""
    cut = (time.time() if now is None else now) - GAS_BASIS_WINDOW_S
    rows = await db()[GAS_SAMPLES].find({"at_s": {"$gte": cut}}, {"gwei": 1}).to_list(100_000)
    xs = sorted(float(r["gwei"]) for r in rows if float(r.get("gwei") or 0) > 0)
    if not xs:
        return 0.0
    rank = math.ceil(GAS_BASIS_PERCENTILE / 100 * len(xs))
    return xs[min(len(xs) - 1, max(0, rank - 1))]


async def gas_health() -> dict[str, Any]:
    """What /v1/health publishes about the series — ⛔ **COUNTS, NEVER THE NUMBER**, the same law
    `crossings` and `coins` are published under. A watchdog has to see that the series is being
    written (a basis nobody is sampling is the 2026-09-10 defect, silently back); nobody needs an
    unauthenticated read of what we price a crossing at."""
    now = time.time()
    coll = db()[GAS_SAMPLES]
    newest = await coll.find({}, {"at_s": 1}).sort("at_s", -1).limit(1).to_list(1)
    age = None
    if newest:
        age = max(0.0, now - float(newest[0].get("at_s") or 0))
    return {
        "samples": await coll.count_documents({}),
        "samples_24h": await coll.count_documents({"at_s": {"$gte": now - GAS_BASIS_WINDOW_S}}),
        "newest_age_s": None if age is None else int(age),
        "window_s": int(GAS_BASIS_WINDOW_S),
        "ttl_s": int(GAS_SAMPLE_TTL_S),
    }


async def relayer_fee_for(asset: Asset, rpc: Any) -> tuple[int, int, dict[str, Any]]:
    """(fee, floor, detail) — the relayer's own arithmetic at the documented basis, never BELOW
    the floor we ride. **THE ONE PLACE A CROSSING IS PRICED**: the quote at request time
    (`routers/withdrawals.live_fees`), the release, and the operator CLI all come through here.

    §WE-SET-IT-WE-DONT-READ-IT cuts both ways. Too high and we simply hand over the difference;
    too LOW and the relayer may never pick the message up — and per §7.9 #7 that message stalls
    for days with the bETH already burned and no refund path. `ethpipe.min_relayer_fee_units` is
    the floor the e2b side already rides; the b2e side had none at all.

    THE RULE (T45), and there is only one:
      * WHAT IS READ: `beam.max_gas_price_gwei(rpc)` → `eth_feeHistory` over
        `beam.FEE_HISTORY_BLOCKS` blocks, `baseFeePerGas[-1] × 2 + median(reward)` with the tip
        clamped to [0.01, 3.0] gwei — the relayer's own `estimate_max_gas_price_gwei`.
      * WHAT IS CHARGED: that read, RAISED to the 24 h p75 of the `gas_samples` series when the
        series knows better (`gas_p75_gwei`). A quote taken in a trough funds a crossing the
        release cannot make.
      * THE MARGIN: `PGAS_RELAYER_FEE_MARGIN` (1.5), inside `beam.relayer_fee_groth` — it is the
        relayer's insurance against REFUSAL, and it is not touched here.

    ⛔ **THE FLOOR IS APPLIED TO `fee_units`, NOT RE-DERIVED.** `beam.relayer_fee_groth` is the
    ONE implementation of the relayer's sum, and it is exactly linear in gas in both of its
    branches (ETH: `120_000 × gas/1e9 × margin`; any other asset: the same times a price ratio),
    so raising the basis is one multiplication of the number that function already returned,
    rounded the way that function rounds it. Re-deriving `120_000 × basis …` here would be a
    second implementation of a number that reaches money (law 9) — and
    `tests/test_gas_basis_and_refund.py` proves the scaled fee IS the fee that function answers
    when the gas really is the basis.

    `detail` keeps the two apart because they are two different things: `gas_gwei` is what we
    MEASURED (the evidence, and what the row records), `gas_basis_gwei` is what we CHARGED at."""
    fee, detail = await beam.relayer_fee_groth(asset, rpc)
    live = float(detail.get("gas_gwei") or 0.0)
    p75 = await gas_p75_gwei()
    detail["gas_p75_gwei"] = p75
    basis = max(live, p75)
    detail["gas_basis_gwei"] = basis
    if live > 0 and basis > live:
        units = float(detail.get("fee_units") or 0.0) * (basis / live)
        detail["fee_units"] = units
        fee = int(round(units * beam.GROTH))
    floor = int(ethpipe.min_relayer_fee_units(asset) // asset.grid)
    detail["floor_groth"] = floor
    detail["fee_before_floor_groth"] = fee
    return max(int(fee), floor), floor, detail


# ----------------------------------------------------------------------------- payout: scheduled


async def _beam_fees_ok(
    bp: beampay.BeamPay,
    kind: str,
    row_id: str,
    coll: str,
    fee_addr: str | None = None,
) -> bool:
    """Every claim, shield and pipe send is paid for in BEAM. Refuse below what THIS call costs
    and page below the 5-BEAM floor (§6.4 step 12) — a fee-starved wallet fails mid-chain.

    ⚠️ **WHAT THIS CALL COSTS IS DATA, NOT A CONSTANT** (`fee_budget`). It used to be
    `settings.beam_claim_fee_groth` = 2,000,000, and the first live claim paid 12,100,000. The
    number is now `max(floor, 1.5 × the worst of the last 10 settled transactions of this kind)`,
    read back from the place that charges it, and `kind` — not a groth amount — is what a call
    site names, so three gates cannot drift apart by three literals.

    The number is a BeamPay ADDRESS balance of asset 0, not the wallet's — a raw wallet balance
    is one shared UTXO pool and is never our inventory (law 1). For a claim and for a shield the
    address is the treasury's, which is both where the fee is debited from and the exact number
    BeamPay itself applies to a `/withdraw` (api.py:283-400).

    ⛔ **A GUARD MUST READ THE PLACE THE FEE BOOKS TO.** A release registers its contract txid
    to the MAX-PRIVACY address, and BeamPay's `contract_attribution.attribution_deltas` ends
    with `deltas["0"] -= fee`: the whole BEAM transaction fee of the invocation is debited from
    the REGISTERED address, which holds no BEAM at all, so it goes negative by the fee on every
    crossing. The treasury's asset-0 balance — the number this gate used to read for a release
    too — therefore never falls for a payout, and the one guard that exists to stop a fee-starved
    wallet failing mid-chain drifted high without bound while the wallet really paid. So the
    caller names the address the fee will land on (`fee_addr`) and it is SUMMED with the
    treasury: the sum is what the wallet is really spending across the two addresses we can
    read, and it is never looser than the treasury alone (an MP balance driven negative by
    payout fees only ever subtracts).

    The pass's own spending counts: N calls are about to be made against one balance read, and
    a leg-level check cannot see the other N−1 legs."""
    need_groth, why = await fee_budget(kind, bp)
    treasury = beampay.treasury_address()
    have = await bp.available_groth(treasury, 0)
    if fee_addr and fee_addr != treasury:
        booked_to = await bp.available_groth(fee_addr, 0)
        have += booked_to
        if booked_to < 0:
            # deliberate, and said out loud ONCE an hour so BeamPay's own "Direct attribution
            # left a NEGATIVE balance" page stays a fact the operator can place rather than a
            # mystery that trains them to ignore the pager.
            await tg.send(
                f"The max-privacy address's BEAM balance is {tg.fmt_groth(booked_to)} — that is "
                f"the payout fee attribution (BeamPay books every invocation's fee to the "
                f"REGISTERED address), and it is counted against the fee budget here",
                key="beam-fee-mp-negative",
                cooldown_s=3600,
            )
    # ⛔ …AND WHAT THE CROSSING ADDRESSES ARE CARRYING INSTEAD OF THE TREASURY (T40). Every
    # release now registers its txid to a FRESH address, so BeamPay books that invocation's BEAM
    # fee there and the treasury's balance does not fall for it — the identical drift the
    # `fee_addr` sum above was added to close, arriving through a new door. `fee_addr` still
    # covers THIS crossing's address; this covers every one before it.
    debt = await crossing_fee_debt_groth()
    # ⛔ …BUT THIS CROSSING'S OWN FEE IS ONE FEE, NOT TWO (T40b F8). After a settled crossing —
    # which is exactly the state a retry re-enters in — the row carries `crossing_fee_groth` AND
    # its address balance is already negative by that same fee. `booked_to` above added the
    # negative balance and `debt` below subtracts the recorded number: counted twice, the gate
    # refuses a wallet that can pay, on the retry path, for ever. The address's OWN balance is
    # the more direct evidence, so the row's recorded copy is the one that comes back out.
    own = 0
    if fee_addr and fee_addr != treasury:
        mine = await db()[coll].find_one({"_id": row_id}, {"crossing_fee_groth": 1})
        own = int((mine or {}).get("crossing_fee_groth") or 0)
    have -= int(_PASS["beam_fee"]) + (debt - own)
    if have < settings.beam_fee_alert_groth:
        await tg.send(
            f"LOW BEAM: the treasury holds {tg.fmt_groth(have)} BEAM for fees (floor "
            f"{tg.fmt_groth(settings.beam_fee_alert_groth)}) — claims, shields and payouts each "
            f"need BEAM",
            key="beam-fee-low",
            cooldown_s=3600,
        )
    if have < need_groth:
        await _hold(
            coll,
            row_id,
            f"the wallet holds {tg.fmt_groth(have)} BEAM and this call reserves "
            f"{fee_budget_line(kind, need_groth, why)}",
            key=f"beam-fee:{row_id}",
        )
        return False
    _PASS["beam_fee"] = int(_PASS["beam_fee"]) + int(need_groth)
    return True


# ------------------------------------------------------------- the destination, re-read

# `routers/withdrawals.refuse_contracts` proves W carries no contract code AT REQUEST TIME and
# writes the block it proved it at onto the row (`dest_checked_head` / `dest_checked_at`).
# `deliver_at` may be `max_window_s` (30 days) later, so that proof can be a month old when the
# bETH is burned — and a bare EOA today is a deployed contract tomorrow (a counterfactual
# CREATE2 account, an EIP-7702 delegation). A b2e crossing has NO refund path: value delivered
# into a contract with no payable receive is stranded permanently. So it is asked again HERE,
# with the burn one step away, and the request-time block is the baseline it is compared to.
DEST_NOW_CONTRACT = (
    "the destination now holds contract code — a bridge delivery would strand there"
)


def has_code(code: str) -> bool:
    """`"0x"` and `"0x0"` are the only answers that mean "no code"."""
    return bool(str(code).replace("0x", "").strip("0"))


async def _dest_still_a_wallet(row: dict[str, Any]) -> bool:
    """True when W is still a bare wallet at the CURRENT head. False refuses this pass.

    Three answers, and the difference between them is the whole guard:

      * **no code** → release. The head it was proven at is recorded next to the request-time one.
      * **code** → HOLD, and alert ONCE. No auto-cancel and no refund: the user's groth stays in
        `scheduled`, because a crossing that cannot be recalled is not a decision this loop gets
        to make on its own. An operator does.
      * **unreadable** (no endpoint, an implausible head, a head BELOW the one this order was
        already proven at) → refuse SILENTLY and try again next pass. "We could not look" is not
        "it is a contract" and is certainly not "it is a wallet"; it is not a verdict at all, so
        it writes no hold and pages nobody.

    ⛔ **AND IT ASKS EVERY ENDPOINT, EACH FOR ITS OWN HEAD** (T40b F13, handed over from T37d).
    This used to be `payouts.dest_code_at_head`, which took the head from `head_from()` and
    pinned `eth_getCode` to whichever endpoint answered it. On prod that is publicnode, which
    serves the head happily and then refuses a code read at a numeric block ("Archive requests
    require a personal token") — so this guard raised every 30 s, refused SILENTLY (correctly:
    an unreadable chain is not a verdict) and NOTHING was ever released. `ethpipe.
    code_at_head_anywhere` is the one reader now: the fix for an endpoint that will not serve a
    query is MORE endpoints, never a lower bar (law 8), and the head still comes from the same
    endpoint the code is read from or the pin protects nothing.

    §9.7: the request id, never W. `api.log` otherwise pairs request_id → destination and
    `payout_requests` pairs request_id → account_id, which is the whole product defeated."""
    rid = str(row["_id"])
    was_head = int(row.get("dest_checked_head") or 0)
    try:
        code, head, url = await ethpipe.code_at_head_anywhere(get_rpc(), str(row["W"]))
    except Exception as e:  # noqa: BLE001 — an unreadable chain is a wait, never a verdict
        log.info(
            "payout %s: the destination could not be re-read this pass (%s: %s); it was proven "
            "clear at block %s — not releasing, asking again next pass",
            rid, type(e).__name__, beam.redact(e), was_head or "(unrecorded)",
        )
        return False
    if was_head and head < was_head:
        # a snap-syncing or freshly-restarted provider answering an OLD block reads "0x" for code
        # deployed since — the answer is not evidence, so it is not acted on in either direction
        log.info(
            "payout %s: %s answered block %s, below the block %s this order was already proven "
            "at — not evidence, not releasing",
            rid, url, head, was_head,
        )
        return False
    if not has_code(code):
        log.info(
            "payout %s: the destination is still a bare wallet at block %s (proven clear at "
            "block %s when the order was accepted)", rid, head, was_head or "(unrecorded)",
        )
        await _checkpoint(
            "payout_requests", rid, dest_recheck_head=head, dest_recheck_at=time.time()
        )
        return True
    await _hold(
        "payout_requests", rid, DEST_NOW_CONTRACT, key=f"payout-dest:{rid}", cooldown_s=6 * 3600
    )
    # ⛔ ALERT ONCE, ON THE TRANSITION. `tg.alert` has no cooldown and writes an event row, and a
    # held row is re-read every `hold_backoff_s` for as long as it exists — an immediate page
    # that repeats every five minutes forever is the pager the operator learns to ignore
    # (law 15). The marker is the transition.
    marked = await db().payout_requests.find_one_and_update(
        {"_id": rid, "dest_code_at": {"$exists": False}},
        {"$set": {"dest_code_at": time.time(), "dest_code_head": head}},
    )
    if marked is not None:
        await tg.alert(
            "payout_dest_now_contract",
            f"Payout REFUSED: {DEST_NOW_CONTRACT}. It was proven a bare wallet at block "
            f"{was_head or '(unrecorded)'} when the order was accepted and carries code at "
            f"block {head} now. NOTHING was cancelled and NOTHING refunded — a b2e crossing "
            f"has no refund path, so an operator decides",
            request_id=rid,
        )
    return False


async def s2_unshielded_gate(bp: beampay.BeamPay, rid: str, asset: Asset) -> bool:
    """§9.3 / spec question S2 — **RETIRED 2026-09-10 under `PGAS_PAYOUT_SPEND_UNSHIELDED`**,
    kept and flag-gated for a deployment that wants the stricter rule. True → the release may
    proceed.

    WHAT IT SAID. Nobody has proven which inputs the WALLET picks for a
    `role=user,action=send` shader invocation. Two things could go wrong and the gate refused
    both: the invocation cannot reach max-privacy inputs at all (a gate that passed on the
    shielded float then produces "not enough inputs", which is the lost-response state), or it
    quietly funds itself from a freshly-claimed REGULAR output — and a send funded from the
    output a deposit claim created is exactly the on-chain deposit ↔ payout link §9.3 promises
    does not exist. So it refused while ANY unshielded balance of that asset existed.

    ⚠️ **WHY IT WAS RETIRED, AND WHAT THAT GAVE UP.** With `PGAS_SHIELD_ENABLED=0` and a claim
    that books to the treasury, "any unshielded balance exists" is the steady state — the gate
    held every direct payout, for ever, on money the user had already paid for. And the shielded
    float it insisted on is unspendable anyway for up to 72 h after each chunk settles (the
    max-privacy lock; the box measured `available_mp 0 / maturing_mp 1,652,864` ten hours in).
    The admin accepted the trade-off on 2026-09-10: *"in case we don't have available ETH for
    unlock to let people withdraw, let's keep bETH out of Lelantus"* and *"as Beam is private by
    default we can mix those BEAMs."*

    THE PRIVACY THAT IS GONE, stated plainly rather than implied: a crossing funded from a
    regular output can be linked, by the relayer or by anyone reading the chain, to the claim
    UTXO that created it — deposit and payout become one graph. **Amounts and addresses stay
    blinded** (Beam's base layer hides both; the pipe carries no address of ours), so what
    leaks is the LINK, not the numbers or the parties. Shielding still runs and still breaks
    that link for everything it covers; what changed is that the product no longer refuses to
    pay while it waits.

    ⛔ The flag that retires it is the SAME one that lets the float count unshielded value
    (`spend_unshielded`). Two flags would eventually be set apart, and the state "count the
    regular float, then refuse because a regular balance exists" is a deadlock that looks like
    three healthy gates."""
    if spend_unshielded() or settings.beam_send_inputs_proven:
        return True
    # claimed-but-not-yet-shielded value sits at the TREASURY address (the claim was registered
    # to it); shielded value sits at the max-privacy one. That split is why both addresses exist.
    regular = await bp.available_groth(beampay.treasury_address(), asset.aid)
    if regular <= settings.beam_regular_tolerance_groth:
        return True
    await _hold(
        "payout_requests",
        rid,
        f"the wallet still holds {tg.fmt_groth(regular)} unshielded {asset.key} and it is not "
        f"proven which inputs a pipe send spends (spec S2) — refusing while a send could link a "
        f"claim to this payout",
        key=f"payout-unshielded:{rid}",
    )
    return False


# ────────────────────────────────────── a FRESH Beam address per crossing (T40, admin 2026-09-10)
#
# *"we just distribute what we receive from different new wallets (when you specify sendFund
# from — it should be new SBBS address)"*, and in the same breath *"let's not shield deposits"*.
# Shielding is off for good, so the link between a claim output and a payout spend is broken the
# other way: the treasury moves exactly what this crossing burns into an address created for
# THIS order and nothing else, and the contract txid is registered there. Every crossing then
# books against its own address, and no two of them share one.
#
# ⛔ The move is a BeamPay `/withdraw` — internal because the destination is a BeamPay address
# (INTEGRATION.md §4), which locks the receiver's side too, but STILL A REAL BEAM TRANSACTION:
# api.py queues it in `pending_withdrawals` and the daemon sends it, it costs BeamPay's regular
# 0.001-BEAM fee out of the treasury, and it takes a coin. So it is treated exactly as a shield
# chunk is: the marker before the call, the comment as its identity, and a lost answer resolved
# from history rather than by calling again.

FUND_LOOKBACK_S = 300.0


def crossing_note(rid: str) -> str:
    """The note the crossing's own address is created with — one address, one order."""
    return f"payout|{rid}"


def fund_comment(rid: str) -> str:
    """The IDENTITY of this order's funding transfer.

    ⛔ `/withdraw` is not idempotent and answers no txid (INTEGRATION.md §4), so this string is
    the only thing that can ever tell a resend from a first send. Chosen before the call,
    written on the row before the call, and looked for in `/transactions` instead of calling
    again — the same law a shield chunk is sent under."""
    return f"payout|{rid}|fund"


def crossing_groth(row: dict[str, Any]) -> int:
    """WHAT THE b2e SEND DELIVERS — ONE reader, shared by the funding and the send.

    `routers/withdrawals._write_items` writes `amount_groth = delivered_groth` on every direct
    row (so the release has exactly one number to send), and every later reader — `_our_msg`,
    `find_delivery`, `_book_release`, `inflight_pipeline` — matches on `amount_groth`. The
    funding of the crossing's own address must be the SAME number to the groth or the address
    does not zero out and the invariant stops being checkable, which is why it is read here and
    not re-derived at either call site."""
    return int(row.get("amount_groth") or 0)


async def crossing_address_for(bp: beampay.BeamPay, row: dict[str, Any]) -> str:
    """This order's own fresh regular Beam address — CREATED ONCE, and a retry reuses it.

    ⛔ **ONCE PER ORDER, NOT ONCE PER ATTEMPT.** A retry that made a second address would leave
    the first one holding the funding of the attempt that died, in a bucket nothing reads: the
    float gate counts the treasury and the max-privacy registry, and a stranded crossing address
    is in neither. So the row is the authority (`source_address`, and `attribution_address`
    reads it), exactly as it is for which SOURCE funded the crossing.

    Created THROUGH BeamPay and never through the wallet-api (law 10, INTEGRATION.md §6 rule 3):
    `/create_wallet` makes it on the wallet AND registers it in the ledger in one call, so what
    lands on it is tracked. The shape is checked because the admin asked for an **SBBS** address
    — a 64–72-hex regular one (64 hex of PeerID + the BBS channel) — and a max-privacy token
    here would put the funding inside a 72-hour lock the crossing cannot spend out of."""
    addr = str(row.get("source_address") or "")
    if addr:
        return addr
    addr = await bp.create_wallet(crossing_note(str(row["_id"])), "regular")
    if not beampay.looks_like_regular_address(addr):
        raise beampay.BeamPayError(
            f"/create_wallet answered {addr[:12]}…, which is not a regular SBBS address — a "
            f"crossing funded into a max-privacy or offline address could not be spent for 72 h"
        )
    await _set("payout_requests", str(row["_id"]), source_address=addr, crossing_address=True)
    return addr


async def crossing_fee_debt_groth() -> int:
    """Σ over the BEAM invocation fees BeamPay booked to crossing addresses instead of to the
    treasury — the number `_beam_fees_ok` has to subtract to stay honest.

    ⛔ **A GUARD MUST READ THE PLACE THE FEE BOOKS TO**, and moving the attribution onto a fresh
    address per crossing moves it somewhere the gate cannot see. BeamPay's
    `contract_attribution.attribution_deltas` ends with `deltas["0"] -= fee`, so each crossing
    address ends at exactly minus its invocation fee and the TREASURY's BEAM never falls for it.
    Read from the ROWS (`crossing_fee_groth`, written once per crossing by `fee_charged`) rather
    than from N BeamPay balance calls, which is the same number and does not grow the pass's
    cost with the number of crossings we have ever made.

    ⛔ No page, no ordering, no limit: a guard that admits a spend must never depend on a window
    (`txid_is_taken`, `inflight_groth`, `scheduled_liability_groth` — the same law). Cached per
    pass, because N gates are about to reserve against one balance read."""
    cached = _PASS.get("crossing_debt")
    if cached is not None:
        return int(cached)
    rows = await db().payout_requests.aggregate(
        [
            {"$match": {"crossing_address": True, "crossing_fee_groth": {"$gt": 0}}},
            {"$group": {"_id": None, "total": {"$sum": "$crossing_fee_groth"}}},
        ]
    ).to_list(1)
    total = int((rows[0] if rows else {}).get("total") or 0)
    _PASS["crossing_debt"] = total
    return total


async def attribution_address(row: dict[str, Any]) -> str:
    """WHERE this crossing's flow books with BeamPay — ONE reader, and the row is the authority.

    `_payout_scheduled` decides it once (the treasury for a regular-funded crossing, the
    max-privacy primary for a shielded one) and writes `source_address` before anything is
    signed. Every later step — the registration after the send, the retry in `_payout_releasing`,
    the registration of a txid resolved from the chain — reads it from HERE rather than deciding
    again, because a second decision made on a float that has moved since would register a
    regular-funded burn against the max-privacy address and put BeamPay's books out by the whole
    crossing plus its fee.

    A row released before this field existed carries none, and under the model it was written
    under the max-privacy primary WAS the answer — so that is the fallback, and it is the only
    one (law 9: one reader, one fallback chain)."""
    addr = str(row.get("source_address") or "")
    return addr or await float_address()


INFLIGHT = ("releasing", "bridging")


def _asset_match(asset: Asset) -> dict[str, Any]:
    """The Mongo clause that means what `_asset_of` means — a missing or blank `asset` reads
    as ETH, so a query that only asked `{"asset": "ETH"}` would miss those rows."""
    if asset.key != "ETH":
        return {"asset": asset.key}
    return {"$or": [{"asset": asset.key}, {"asset": None}, {"asset": {"$exists": False}}]}


def inflight_pipeline(asset: Asset, exclude_rid: str | None = None) -> list[dict[str, Any]]:
    """The aggregation `inflight_groth` runs — exposed so a test can assert on the QUESTION,
    which is the only way to assert on a limit a mongomock double does not honour."""
    committed: list[dict[str, Any]] = [
        {"status": {"$in": list(INFLIGHT)}},
        # ⛔ A HELD BURN IS STILL COMMITTED FLOAT. `_hold_for_a_human` moves an unresolved
        # release out of `releasing`, and a held release is BY DEFINITION one whose txid was
        # never captured — so it was never registered with BeamPay either, the flow booked to
        # `__house__`, and the max-privacy address's balance did NOT fall. Dropping the
        # reservation there overstates the float in both terms at once, with nothing to correct
        # it. It stays reserved until an operator writes `float_resolved` on the row.
        {
            "status": HELD,
            "held_from": {"$in": list(INFLIGHT)},
            "float_resolved": {"$ne": True},
        },
        # ⛔ **AND A FUNDED CROSSING IS COMMITTED FLOAT BEFORE IT IS RELEASED** (T40b F5). T40
        # split the release in two — the treasury funds an address of this order's own, and the
        # send follows once that transfer settles — and between those two steps the row is still
        # `scheduled`. It counted for NOTHING here, so two orders in one pass could each fund
        # against the same crossing's worth: the first one's money was already gone and the
        # second one's gate could not see it. `kernel_at` is the burn (see `crossing_pipeline`),
        # so its absence is what "not settled yet" means here too.
        {
            "fund_called_at": {"$exists": True},
            "kernel_at": {"$exists": False},
            "status": {"$in": ["scheduled", DELAYED]},
        },
        {
            "fund_called_at": {"$exists": True},
            "kernel_at": {"$exists": False},
            "status": HELD,
            "held_from": {"$in": ["scheduled", DELAYED]},
            "float_resolved": {"$ne": True},
        },
    ]
    match: dict[str, Any] = {"$and": [{"$or": committed}, _asset_match(asset)]}
    if exclude_rid is not None:
        match["_id"] = {"$ne": exclude_rid}
    return [
        {"$match": match},
        {
            "$group": {
                "_id": None,
                "total": {
                    "$sum": {
                        "$add": [
                            {"$ifNull": ["$amount_groth", 0]},
                            {"$ifNull": ["$relayer_fee_groth", 0]},
                        ]
                    }
                },
            }
        },
    ]


async def inflight_groth(asset: Asset, exclude_rid: str | None = None) -> int:
    """Σ(amount + relayerFee) over every payout that has been released and not yet settled.

    The wallet's `available_mp` cannot fall between two orders in one pass — for a BVM
    invocation it may not fall until the kernel registers at all — so a per-order read of it
    admits N payouts against ONE payout's worth of float. The surplus sends are then refused
    inside the wallet, which is exactly the lost-response state, which is exactly the state the
    resolver used to mis-attribute. This is the reservation `withdrawals.reserve()` already
    makes against Available, made against the float.

    ⛔ **A GUARD THAT ADMITS A SPEND MUST NEVER DEPEND ON A WINDOW.** This was
    `find(…).to_list(500)` with no sort — the identical defect `txid_is_taken`'s own docstring
    records as fixed ("it would have failed open at the first moment it mattered"), on the one
    guard that decides whether a pass may spend the float. `_due` admits up to BATCH rows per
    status every 30 s and `bridging` lasts ~61 Beam confirmations, so the steady-state
    population is far above any window. The DATABASE sums it now: no page, no ordering, no
    limit."""
    rows = await db().payout_requests.aggregate(
        inflight_pipeline(asset, exclude_rid)
    ).to_list(1)
    return int((rows[0] if rows else {}).get("total") or 0)


async def _funding_gates(
    bp: beampay.BeamPay, row: dict[str, Any], asset: Asset, need: int, candidates: list[str]
) -> str | None:
    """⛔ **THE GATES BETWEEN A DECIDED CROSSING AND A SIGNATURE — ONE IMPLEMENTATION.**

    Returns the source that may fund this crossing, or `None` when this pass must not sign (a
    refusal has been written, or it is simply not this order's turn and nothing needed saying).

    It exists because T40's funding step gave the machine a SECOND way in. `_payout_scheduled`
    returned early into `_payout_funding` the moment `fund_called_at` was set, and that path ran
    two gates where the first attempt ran seven: the wallet's spendable buckets, the free-coin
    count and the float were never asked again. A retry is not a lesser decision than a first
    attempt — it is the same decision, on a wallet that has moved since — so both enter here,
    and a gate added to one is a gate added to both.

    The order is deliberate and unchanged from the path that had it right: the LEDGER says the
    value is ours (`payout_float`), then the WALLET says it can move today (`wallet_spendable`),
    then the COINS say a transaction can be built out of it at all. Money-cheap questions first,
    RPC-expensive ones last, and nothing irreversible anywhere near here."""
    rid = row["_id"]
    parts = await payout_float(bp, asset)
    have = int(parts["total"])
    # every crossing already in flight is committed float, whether or not the wallet's
    # `available_mp` has noticed yet (for a BVM invocation it may not fall until the kernel
    # registers at all). `_advance` writes `releasing` before `_release` sends and
    # `_fund_the_crossing` writes its marker before it calls, so an order admitted earlier in
    # THIS pass is already counted here — and an order asking about ITSELF is not.
    reserved = await inflight_groth(asset, rid)
    if have - reserved < need:
        await _refuse(
            row,
            f"the float holds {fmt_units(have, asset)} "
            f"({fmt_units(int(parts['shielded']), asset)} shielded across "
            f"{len(await mp_registry())} max-privacy address(es), "
            f"{fmt_units(int(parts['regular']), asset)} unshielded at the treasury and at "
            f"funded crossing addresses), {fmt_units(reserved, asset)} of it is already "
            f"committed to crossings in flight, and this payout needs {fmt_units(need, asset)} "
            f"(amount + relayer fee + reserve)",
            key=f"payout-float:{rid}",
        )
        return None
    # ⛔ **A CROSSING IS FUNDED FROM ONE SOURCE, AND THE FLOW BOOKS THERE.** BeamPay debits the
    # whole registered flow — the bETH and the invocation's BEAM fee — from the address the
    # txid was registered to. Booking a crossing funded out of the treasury's unshielded balance
    # to the max-privacy address drives MP negative and leaves the treasury untouched, so the
    # float the next payout gates on counts value that is already gone: the same drift
    # `_beam_fees_ok` had to be repaired for once, from the other end.
    #
    # A source is viable when the LEDGER says we own that much there AND the WALLET says it can
    # spend that much from that bucket today. Regular first: a max-privacy output is locked for
    # up to 72 h after it settles and the anonymity-set target that would release it sooner
    # cannot be reached on a pool growing ~28 outputs a day.
    owned = [s for s in candidates if int(parts[s]) >= need]
    if not owned:
        await _refuse(
            row,
            f"no single source covers this payout: the float holds "
            f"{fmt_units(int(parts['regular']), asset)} unshielded and "
            f"{fmt_units(int(parts['shielded']), asset)} shielded, this payout needs "
            f"{fmt_units(need, asset)}, and a crossing is funded from ONE address",
            key=f"payout-source:{rid}",
        )
        return None
    try:
        spendable = await wallet_spendable(bp, asset)
    except beampay.BeamPayError as e:
        # ⛔ AN UNREADABLE WALLET IS NOT AN EMPTY ONE AND CERTAINLY NOT A FULL ONE (law 8).
        await _refuse(
            row,
            f"the wallet's own spendable balance could not be read ({beam.redact(e)[:160]}) — "
            f"refusing to sign a send whose funding nobody could measure",
            key=f"payout-walletread:{rid}",
        )
        return None
    source = next((s for s in owned if int(spendable[s]) - reserved >= need), None)
    if source is None:
        # THE LEVEL THE RELEASE ACTUALLY NEEDED, and the one nothing had. 2026-09-10: BeamPay's
        # registry summed 0.02652864 bETH while the wallet could spend NONE of it — the shield
        # chunks had settled ten hours earlier and were still inside the max-privacy lock. Every
        # gate above passes on a number that is true and irrelevant.
        await _refuse(
            row,
            f"the wallet can spend {fmt_units(int(spendable['regular']), asset)} regular / "
            f"{fmt_units(int(spendable['shielded']), asset)} shielded now; "
            f"{fmt_units(int(spendable['maturing']), asset)} is maturing (max-privacy lock, up "
            f"to 72 h after a shield settles); {fmt_units(reserved, asset)} is committed to "
            f"crossings in flight; this payout needs {fmt_units(need, asset)}",
            key=f"payout-wallet:{rid}",
        )
        return None
    # ⛔ **ONE COIN PER TRANSACTION, AND EVERY INVOCATION ALSO NEEDS A BEAM COIN FOR ITS FEE.**
    # 2026-09-10 10:30Z: two releases went out 0.7 s apart and BOTH came back "Not enough inputs
    # to process the transaction". Beam locks a whole UTXO while a transaction that spends it is
    # pending, so what bounds concurrent releases is the COUNT of free coins, which no balance
    # can express.
    amounts = spendable[f"amounts_{source}"]
    fee_coins = spendable["fee_coins"]
    if amounts is None or fee_coins is None:
        # ⛔ AN UNREADABLE COIN LIST IS A REFUSAL, AND A REFUSAL WRITES A ROW (laws 11 and 12).
        # This deferred in the log, for ever: a wallet-api that was down stopped every payout
        # with nothing an operator or a monitor could see. It is still never a send — "we
        # cannot see" is never "there are free inputs" (law 8) — but it is now a WAITING hold
        # carrying the wallet's own words, and ONE attempt per pass means ONE page.
        await _refuse(
            row,
            f"the wallet's coin list could not be read "
            f"({str(spendable.get('coins_error') or 'no reason recorded')[:160]}) — refusing to "
            f"sign a send whose free inputs nobody could count",
            key=f"payout-coins-unreadable:{rid}",
        )
        return None
    # how many crossings THIS SIZE the source's coins can fund at once, and how many BEAM fee
    # coins there are to pay for them: a crossing needs one coin of the asset and one BEAM coin
    # PER BEAM-SPENDING LEG (`fee_coins_needed`), and all of them are locked while it is pending
    per_crossing = fee_coins_needed(row, source)
    budget = min(coin_capacity(amounts, need), int(fee_coins) // per_crossing)
    busy = await inflight_releases(asset)
    # ⛔ **A BUDGET OF ZERO IS NOT "NOT YOUR TURN" — AND IT USED TO SKIP THIS GUARD ENTIRELY.**
    # `budget > 0 and busy >= budget` defers only when there IS a budget, so a wallet that could
    # fund NO call fell through to sign. The box at 10:30Z had 9.724 BEAM at the treasury and
    # one FREE 0.01 BEAM coin (the big one locked inside a pending transaction): every balance
    # gate passed, the send was signed, and the wallet answered "Not enough inputs to process
    # the transaction". So a zero budget is its own refusal — a WAITING hold that names both
    # counts, never a send.
    if budget == 0:
        await _refuse(
            row,
            f"no free coin: {asset.key} spendable coins {len(amounts)} "
            f"(largest {fmt_units(max(amounts, default=0), asset)}), BEAM fee coins "
            f"{int(fee_coins)} and this crossing needs {per_crossing}; this send needs "
            f"{fmt_units(need, asset)} + fee budget "
            f"{fmt_units(int(spendable['fee_budget_groth'] or 0), 'BEAM')}; the short asset is "
            f"{'BEAM' if int(fee_coins) < per_crossing else asset.key} — waiting for a coin to "
            f"free up, or for a split",
            key=f"payout-coins:{rid}",
        )
        return None
    # …while "every free coin is busy" IS just a queue: nothing is wrong with the order, it is
    # simply not its turn, so it writes no refusal row, pages nobody, and the next pass reaches
    # it.
    if busy >= budget:
        log.info(
            "payout %s: waiting for a free coin (%d in flight, %d fundable: %d %s coin(s) of "
            "%s, %d BEAM fee coin(s) of %s at %d per crossing)", rid, busy, budget, len(amounts),
            source, asset.key, int(fee_coins),
            tg.fmt_groth(int(spendable["fee_budget_groth"] or 0)), per_crossing,
        )
        return None
    return source


async def _payout_scheduled(row: dict[str, Any]) -> None:
    rid = row["_id"]
    now = time.time()
    if now < float(row.get("release_at") or 0):
        return  # the window has not opened; not a refusal, nothing to say
    mode = str(row.get("mode") or "")
    if mode == INSTANT:
        await _payout_instant(row)  # ← paid on Ethereum from our own float, in one block
        return
    if mode not in DIRECT_MODES:
        await _payout_any_asset(row)
        return
    asset = _asset_of(row)
    if asset.key != "ETH":
        await _refuse(
            row,
            f"v1 payouts are ETH only; this request is {asset.key}",
            key=f"payout-asset:{rid}",
        )
        return
    amount = int(row["amount_groth"])
    charged = bridge_budget_groth(row)  # what this order paid FOR THE CROSSING, not our 2%
    if not settings.payout_direct_enabled:
        # §9.7: the request id, never W. api.log otherwise pairs request_id → destination and
        # payout_requests pairs request_id → account_id, which is the whole product defeated.
        log.info(
            "would release payout %s: %s %s (PGAS_PAYOUT_DIRECT_ENABLED=0)",
            rid,
            tg.fmt_groth(amount),
            asset.key,
        )
        # ⛔ …AND THIS ONE STAYS A HOLD (T40b F3). Every other gate refusal past the delivery
        # window becomes a `delayed` row on the retry ladder; a flag the operator turned off on
        # purpose must not, because `dark` is exactly the marker that stops the stuck monitor
        # crying wolf about a deployment that is deliberately not spending — and `_delay` would
        # drop it and page for a day. The user-visible half of F3 is fixed where it belongs:
        # `eta_for` no longer publishes a delivery time that has already passed.
        await _hold(
            "payout_requests",
            rid,
            "PGAS_PAYOUT_DIRECT_ENABLED=0 — the release is DARK and nothing was sent",
            key=f"payout-dark:{rid}",
            dark=True,
        )
        return
    if workers.paused():
        return  # the movers refuse anyway; do not even plan while the switch is set
    bp = beampay.beampay()
    # ⛔ **A FUNDED ORDER HAS ALREADY BEEN PRICED** (T40, corrected by T40b F2). Its crossing
    # address exists, the treasury has moved exactly what the send burns into it, and the
    # relayer fee it was funded with is PINNED on the row — so this branch exists to make sure
    # the crossing is never priced a SECOND time, which would have the send burn a different
    # number from the one the address holds and leave the address at something other than zero.
    #
    # ⚠️ It used to be read as "already decided", and skipping the pricing had it skip the GATES
    # too: `_payout_funding` proved the transfer landed and signed, and the wallet's spendable
    # buckets, the free-coin count and the float were never asked again on a retry. They are
    # asked there now (`_funding_gates`, the same chain this function enters below) — a retry is
    # the same decision on a wallet that has moved, not a lesser one.
    if row.get("fund_called_at"):
        await _payout_funding(row)
        return
    fee_groth, floor_groth, detail = await relayer_fee_for(asset, get_rpc())
    if amount <= 0:
        await _refuse(
            row,
            f"amount_groth is {amount} — an order with nothing to deliver cannot be crossed "
            f"and a human has to look at this row",
            key=f"payout-amount:{rid}",
        )
        return
    # ⛔ THE SHARE GATE IS A LEGACY-ROW GATE (2026-09-10). It was unreachable when it was
    # written: the minimum payout was `ceil(relayer_fee × 10000 / fee_bps)`, i.e. 50× the
    # relayer fee, so its cut could not reach a tenth of the amount. The floor is 1 groth now
    # that the crossing is an ITEMISED CHARGE, so on a modern row this gate holds every payout
    # between 1 and 10× the relayer fee — orders the user has already paid the whole crossing
    # for, debited, promised a delivery time, and then held for ever. A share of the amount was
    # never the treasury's economics; what the crossing costs against what THIS order funded is,
    # and that is the gate below. A legacy row funded its crossing out of our 2% and nothing
    # else, so for those rows both ceilings stay exactly where they were.
    if not funds_its_own_crossing(row) and fee_groth / amount > settings.max_relayer_share:
        await _refuse(
            row,
            f"the relayer wants {fmt_units(fee_groth, asset)} of {fmt_units(amount, asset)} "
            f"(max {settings.max_relayer_share:.0%}) — refusing to cross at this gas price",
            key=f"payout-share:{rid}",
        )
        return
    # ⛔ THE CYCLE-LEVEL GATE, AND SINCE 2026-09-10 THE ONLY ECONOMIC ONE A MODERN ROW MEETS.
    # The share gate protected the user's amount; nothing protected the TREASURY. At the 0.01
    # ETH product floor the crossing is loss-making above ~1.11 gwei and the share gate still
    # admitted it to 5 gwei, where we pay 0.0009 ETH against 0.0002 charged. Every guard needs
    # the level it protects — and this one measures the live fee against the bridge fee THIS
    # order funded (`bridge_budget_groth`), times the ONE subsidy multiple both the charge and
    # this gate read (`config.relayer_subsidy`: 0 or unset is 1×, never `× 0`, which would hold
    # every order ever written including the ones that funded a full crossing).
    subsidy = relayer_subsidy()
    if fee_groth > charged * subsidy:
        await _refuse(
            row,
            f"the relayer wants {fmt_units(fee_groth, asset)} and this payout only funded "
            f"{fmt_units(charged, asset)} of bridge fee (subsidy limit "
            f"{subsidy:g}×) — refusing to cross at a loss",
            key=f"payout-subsidy:{rid}",
        )
        return
    need = crossing_need_groth(amount, fee_groth)
    # THE FLOAT IS A SUM OF BEAMPAY BALANCES, IN TWO BUCKETS. Shielded value lives at our
    # max-privacy addresses because a BeamPay `/withdraw` put it there — one FRESH address per
    # shield chunk (`shield_target`) — so `float_groth` sums the whole registry and never reads
    # one address; unshielded value sits at the treasury, where a claim booked it, and counts
    # only under `spend_unshielded()`. The primary max-privacy address is still named here
    # because it is where a SHIELDED crossing books its flow, and an address we cannot name is
    # a float we cannot read.
    mp_addr = await float_address()
    if not mp_addr and not spend_unshielded():
        await _refuse(
            row,
            "no max_privacy address is configured or stored, so the shielded float cannot be "
            "read and a release would be spending a number nobody measured",
            key=f"payout-nofloat:{rid}",
        )
        return
    if not await s2_unshielded_gate(bp, rid, asset):
        return
    # ⛔ THE FLOAT, THE WALLET AND THE COINS — THE SAME GATES A RETRY MEETS (T40b F2). Every
    # question from here to "may this be signed" lives in `_funding_gates`, and a funded order
    # coming back for its send re-enters exactly this chain rather than a shortcut round it.
    order = [s for s in SOURCES if s != SOURCE_REGULAR or spend_unshielded()]
    source = await _funding_gates(bp, row, asset, need, order)
    if source is None:
        return
    # where this crossing BOOKS. ⛔ **A REGULAR-FUNDED CROSSING BOOKS TO ITS OWN FRESH ADDRESS**
    # (T40): the treasury moves exactly `amount + relayerFee` into an address created for this
    # order and nothing else, and the txid is registered THERE — so no two crossings share a
    # bucket and the spend is one hop away from the claim output that funded it. A SHIELDED
    # crossing is unchanged and still books to the max-privacy primary: its value is already
    # inside Lelantus, moving it to a regular address first would undo exactly the thing it is
    # there for, and that pool is never refilled (shielding is off for good).
    # One decision, recorded on the row, and every later reader takes it from there
    # (`attribution_address`) rather than deciding again.
    source_addr = "" if source == SOURCE_REGULAR else mp_addr
    if source != SOURCE_REGULAR and not source_addr:
        await _refuse(
            row,
            "the shielded float would fund this crossing but no max_privacy address is "
            "configured or stored to book it to — an unregistered flow books to __house__",
            key=f"payout-nofloat:{rid}",
        )
        return
    # ⛔ THE DESTINATION IS RE-READ HERE, NOT ONLY AT REQUEST TIME. The code check that admitted
    # this order can be up to `max_window_s` old, and a b2e crossing cannot be recalled. Asked
    # after the economic gates (so a waiting order costs no RPC calls) and before anything
    # irreversible — a destination fault is not a shortage and must not spend the fee budget.
    if not await _dest_still_a_wallet(row):
        return
    # the registration this crossing will need must be reachable BEFORE the bETH is burned —
    # asked before the fee budget is spent, because a configuration fault is not a shortage
    if not await attribution_ready(bp, "payout_requests", rid):
        return
    if source == SOURCE_REGULAR:
        # ⛔ THE CROSSING'S OWN ADDRESS IS FUNDED FIRST, AND THE SEND IS THE NEXT STEP — not
        # this one. BeamPay QUEUES a `/withdraw` and its daemon emits the transaction seconds
        # later, so the value is not at that address yet and the output the send should spend
        # does not exist yet. Broadcast ≠ done, one leg earlier: the row stays `scheduled` with
        # its funding marker, and `_payout_funding` releases it once the transfer has SETTLED.
        await _fund_the_crossing(row, asset, source, fee_groth, floor_groth, detail, charged)
        return
    # a pipe `send` is a BVM contract invocation and costs BEAM in the same class as a claim —
    # and BeamPay debits that fee from the address the txid is REGISTERED to, which is this
    # crossing's SOURCE address, so that is the balance this gate has to see fall.
    if not await _beam_fees_ok(bp, "send", rid, "payout_requests", fee_addr=source_addr):
        return
    # The Ethereum baseline for delivery detection, written down BEFORE the send: a delivery is
    # proven by a balance PAIR in one block, and a pair needs a block to start looking from.
    head = int(await get_rpc().block_number())
    claimed = await _advance(
        "payout_requests",
        rid,
        "status",
        "scheduled",
        "releasing",
        "payout_releasing",
        f"Payout releasing: {asset.key} {tg.fmt_groth(amount)} "
        f"(relayer fee {tg.fmt_groth(fee_groth)})",
        "request_id",
        relayer_fee_groth=fee_groth,
        relayer_fee_floor_groth=floor_groth,
        relayer_fee_gwei=detail.get("gas_gwei"),
        fee_charged_groth=charged,
        release_attempt_at=now,
        eth_from_block=head,
        eth_scan_from=head,
        source=source,
        source_address=source_addr,
    )
    if claimed is None:
        return  # another pass owns it
    await _release(rid, asset, amount, fee_groth, str(row["W"]), source_addr)


async def _fund_the_crossing(
    row: dict[str, Any],
    asset: Asset,
    source: str,
    fee_groth: int,
    floor_groth: int,
    detail: dict[str, Any],
    charged: int,
) -> None:
    """Move exactly what this crossing burns into an address created for this order alone.

    ⛔ **THE RELAYER FEE IS PINNED HERE AND NEVER RE-PRICED.** The address is funded with
    `amount + relayerFee` and the send burns `amount + relayerFee`; if the release re-quoted the
    fee, those two numbers would differ by whatever gas did in between and the crossing address
    would end at something other than zero — after which nothing can tell an exact crossing from
    a leaky one. The fee was priced seconds ago through the one reader, with its 1.5× margin and
    its floor under it, and a funding that has not settled within `UNRESOLVED_S` is a human's
    anyway. §WE-SET-IT-WE-DONT-READ-IT cuts both ways and this is the side that keeps the books.

    ⛔ **THE MARKER IS WRITTEN BEFORE THE CALL.** `/withdraw` is not idempotent and answers no
    txid, so a second call would queue a SECOND transfer of the treasury's money for one order.
    The conditional update claims the right to call; the loser of a race finds it and stops; and
    a lost answer leaves behind the one thing that stops the next pass from calling again. It is
    released ONLY on the paths where nothing was queued — the kill switch, an outright refusal —
    because those are the only ones where a retry is not a double send.

    ⛔ **…AND THE ADDRESS IS CREATED BEFORE THE MARKER** (T40b F10). The marker used to go first
    and the address second, so a process that died between them left a row with
    `fund_called_at` and no `source_address` — and the next pass reads that shape as "this
    crossing was funded and we cannot find the transfer", parks it for a human and never
    retries, for a case where NOTHING was queued. Both now land in the same conditional update,
    with the address already recorded, so the broken shape cannot be written at all: a crash
    before it leaves an address (created once per order, reused by every attempt) and no claim,
    and a crash after it leaves the claim that stops a second `/withdraw`."""
    rid = row["_id"]
    bp = beampay.beampay()
    need = crossing_groth(row) + int(fee_groth)
    if not await _beam_fees_ok(bp, "fund", rid, "payout_requests"):
        return
    if workers.paused():
        # nothing has been created and nothing queued: the chain halts exactly here
        return
    try:
        addr = await crossing_address_for(bp, row)
    except beampay.BeamPayError as e:
        await _refuse(
            row,
            f"a fresh Beam address for this crossing could not be created: "
            f"{beam.redact(e)[:160]} — nothing was sent",
            key=f"payout-crossaddr:{rid}",
            cooldown_s=6 * 3600,
        )
        return
    now = time.time()
    won = await db().payout_requests.find_one_and_update(
        {"_id": rid, "status": "scheduled", "fund_called_at": {"$exists": False}},
        {
            "$set": {
                "fund_called_at": now,
                "fund_groth": need,
                "source": source,
                "source_address": addr,
                "crossing_address": True,
                # the fee this crossing is FUNDED with, and therefore the fee it will send with
                "relayer_fee_groth": int(fee_groth),
                "relayer_fee_floor_groth": int(floor_groth),
                "relayer_fee_gwei": detail.get("gas_gwei"),
                "fee_charged_groth": int(charged),
                "updated_at": now,
            }
        },
    )
    if won is None:
        return  # another pass owns it
    if workers.paused():
        # the switch was thrown while we were claiming: nothing is queued yet, so the marker is
        # released and the chain halts here. The ADDRESS stays — it is created once per order
        # and costs nothing to hold.
        await db().payout_requests.update_one({"_id": rid}, {"$unset": {"fund_called_at": ""}})
        return
    comment = fund_comment(str(rid))
    try:
        res = await bp.withdraw(beampay.treasury_address(), addr, asset.aid, need, comment)
    except beam.Halted:
        # the switch was thrown INSIDE the mover, so nothing was queued
        await db().payout_requests.update_one({"_id": rid}, {"$unset": {"fund_called_at": ""}})
        return
    except beampay.BeamPayError as e:
        # ⛔ THE ANSWER WAS LOST AND THE TRANSFER MAY HAVE BEEN QUEUED. The marker STAYS: the
        # next pass looks for the comment in BeamPay's history rather than calling again.
        await tg.alert(
            "payout_fund_unconfirmed",
            f"The crossing's funding transfer is UNCONFIRMED ({beam.redact(e)[:160]}). NOT "
            f"re-sending — /withdraw is not idempotent, so the next pass looks for the comment "
            f"{comment!r} in BeamPay's history instead",
            request_id=rid,
        )
        return
    if res.get("status") is not True:
        # a REFUSAL, not a failure: HTTP 200 with a reason, and the atomic lock matched nothing,
        # so nothing was queued and the marker must be released or this order can never move.
        await db().payout_requests.update_one({"_id": rid}, {"$unset": {"fund_called_at": ""}})
        await _refuse(
            row,
            f"BeamPay refused to fund this crossing's address: "
            f"{str(res.get('msg') or res)[:160]} — nothing was queued",
            key=f"payout-fundrefused:{rid}",
        )
        return
    log.info(
        "payout %s: %s of %s queued to this crossing's own address (comment %s)",
        rid, tg.fmt_groth(need), asset.key, comment,
    )
    # …and if BeamPay's daemon has already emitted it, this same pass crosses. It is a READ,
    # never a second call: the authority is the transaction, not this reply.
    fresh = await db().payout_requests.find_one({"_id": rid})
    if fresh is not None:
        await _payout_funding(fresh)


async def _payout_funding(row: dict[str, Any]) -> None:
    """The crossing's address has been funded — has the transfer LANDED, and may we cross?

    Identity, never presence (rule 1): the transfer is found by the comment WE chose, on the
    address WE created, and it must be SETTLED before anything is signed. An unsettled output
    is one the send cannot spend — and spending the treasury's own claim output instead is
    exactly the link this whole mechanism exists to break."""
    rid = row["_id"]
    asset = _asset_of(row)
    bp = beampay.beampay()
    addr = str(row.get("source_address") or "")
    since = float(row.get("fund_called_at") or 0)
    comment = fund_comment(str(rid))
    if not addr or since <= 0:
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            "scheduled",
            "this order's crossing address was funded and the row records no address (or no "
            "time to search BeamPay's history from), so the transfer can be neither found nor "
            "safely re-sent",
            "payout_fund_lost",
            "request_id",
        )
        return
    try:
        found = await bp.find_txs_by_comments(addr, [comment], since - FUND_LOOKBACK_S)
    except beampay.BeamPayError as e:
        # an INCOMPLETE read is never "no such transaction" — it is not a verdict at all
        log.info("payout %s: BeamPay's history could not be read this pass (%s)", rid, e)
        return
    rows = list(found.get(comment) or [])
    if len(rows) > 1:
        # TWO transfers carrying one comment is a double send of the treasury's money, which is
        # the exact accident `/withdraw` makes possible. It is never resolved by a machine.
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            "scheduled",
            f"{len(rows)} transactions carry the funding comment {comment!r} for this one "
            f"order ({', '.join(str(r.get('txId'))[:12] for r in rows)}) — the treasury may "
            f"have funded this crossing twice and nothing here will send on top of that",
            "payout_fund_double",
            "request_id",
        )
        return
    if not rows:
        if time.time() - since > UNRESOLVED_S:
            await _hold_for_a_human(
                "payout_requests",
                rid,
                "status",
                "scheduled",
                f"this crossing's funding transfer was queued with BeamPay "
                f"{int((time.time() - since) // 60)} min ago and no transaction carrying "
                f"{comment!r} has appeared — this needs a human and is NOT auto-retried, "
                f"because /withdraw is not idempotent",
                "payout_fund_unresolved",
                "request_id",
            )
            return
        await _refuse(
            row,
            "the treasury has moved this crossing's own funding to a fresh Beam address and "
            "BeamPay's daemon has not emitted the transaction yet — waiting, never calling "
            "again",
            key=f"payout-funding:{rid}",
        )
        return
    tx = rows[0]
    txid = str(tx.get("txId") or "")
    if txid and txid != str(row.get("fund_txid") or ""):
        await _checkpoint("payout_requests", rid, fund_txid=txid)
    status = int(tx.get("status", -1))
    if status in beam.TX_DEAD:
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            "scheduled",
            f"the transfer funding this crossing's own address is "
            f"{tx.get('status_string') or status} (tx {txid}) — nothing is auto-retried, "
            f"because /withdraw is not idempotent and a second call would queue a second "
            f"transfer of the treasury's money",
            "payout_fund_failed",
            "request_id",
        )
        return
    if status not in beam.TX_SETTLED:
        await _refuse(
            row,
            f"this crossing's funding transfer is {tx.get('status_string') or status} — the "
            f"send waits for it to settle, because an output that does not exist yet cannot "
            f"fund the crossing",
            key=f"payout-fundsettle:{rid}",
        )
        return
    # BeamPay sets a withdrawal's fee itself and ignores ours, so it is READ BACK from the
    # transaction it made (§WE-SET-IT-WE-DONT-READ-IT) and paged when it is absurd.
    await fee_charged(
        "payout_requests", rid, tx, f"payout {rid} crossing funding", "request_id",
        field="fund_fee_groth",
    )
    fee_groth = int(row.get("relayer_fee_groth") or 0)
    amount = crossing_groth(row)
    # ⛔ **THE ORDINARY GATES, ON THE WAY IN** (T40b F2). A funded order is not exempt from the
    # questions a first attempt answers — it is the SAME decision on a wallet that has moved
    # since, and this is the path a retry takes. What is NOT re-asked is the PRICE: the relayer
    # fee was pinned when the address was funded and the send must burn exactly what the address
    # holds, so re-quoting it here would leave the crossing address at something other than zero
    # and stop the whole invariant being checkable.
    #
    # `need` is what the SEND burns and carries no `float_min_groth` reserve: that reserve exists
    # to stop the last of the float being COMMITTED, and this order's float was committed a leg
    # ago — it is sitting at an address of its own, spendable for nothing else. Source selection
    # is the source the row recorded, for the same reason.
    need = amount + fee_groth
    source = str(row.get("source") or SOURCE_REGULAR)
    if await _funding_gates(bp, row, asset, need, [source]) is None:
        return
    # the invocation's own BEAM fee books to the address the txid is registered to — which is
    # this crossing's own — so that is the balance this gate has to see, summed with the treasury
    if not await _beam_fees_ok(bp, "send", rid, "payout_requests", fee_addr=addr):
        return
    if not await _dest_still_a_wallet(row):
        return
    if not await attribution_ready(bp, "payout_requests", rid):
        return
    head = int(await get_rpc().block_number())
    claimed = await _advance(
        "payout_requests",
        rid,
        "status",
        "scheduled",
        "releasing",
        "payout_releasing",
        f"Payout releasing: {asset.key} {tg.fmt_groth(amount)} "
        f"(relayer fee {tg.fmt_groth(fee_groth)}) from this crossing's own Beam address",
        "request_id",
        release_attempt_at=time.time(),
        eth_from_block=head,
        eth_scan_from=head,
    )
    if claimed is None:
        return  # another pass owns it
    await _release(rid, asset, amount, fee_groth, str(row["W"]), addr)


async def _release(
    rid: str, asset: Asset, amount: int, fee_groth: int, w_addr: str, float_addr: str
) -> None:
    """The irreversible half, in three provable stages.

    BUILD is a `create_tx:false` read plus the pre-broadcast assertions — nothing can have been
    signed, so a failure hands the row back to `scheduled`. SUBMIT may land even when its
    response is lost, so `release_call_at` is written BEFORE it: a row with that marker and no
    txid is RESOLVED from BeamPay's own history on the next pass and NEVER re-sent. REGISTER
    then claims the txid with BeamPay so the burn books to the float that funded it instead of
    to `__house__`; it happens before the row advances to `bridging`, and a failure there is
    retried as a REGISTRATION, never as a send."""
    w = beam.wallet()
    try:
        # The FLOOR for finding our own message again, read before anything is signed: every
        # message id above it was created after this send, so ours is one of them however many
        # orders the pass releases. A fixed look-back window smaller than one pass (40 < 50)
        # made the first orders of a full pass permanently unbookable — see find_local_msg.
        msg_floor = await w.local_msg_count(asset.beam_cid)
        raw, args, blob = await w.build_bridge_send(asset.beam_cid, amount, w_addr, fee_groth)
    except beam.BeamError as e:
        await db().payout_requests.update_one(
            {"_id": rid, "status": "releasing", "beam_txid": {"$exists": False}},
            {
                "$set": {
                    "status": "scheduled",
                    "status_at": time.time(),
                    "updated_at": time.time(),
                    "hold_reason": beam.redact(e),
                }
            },
        )
        await tg.alert(
            "payout_build_refused",
            f"Payout NOT sent — the pipe call could not be built or verified: "
            f"{beam.redact(e)[:200]}. Nothing was signed; the request is scheduled again",
            request_id=rid,
        )
        return
    # §9.7: the args string carries `receiver=0x…`, and this line ran on EVERY release.
    log.info("payout %s calldata verified (receiver, cid) %d bytes: %s", rid, len(blob),
             beam.redact(args))
    await _set(
        "payout_requests",
        rid,
        release_call_at=time.time(),
        release_args=beam.redact(args),
        msg_floor=int(msg_floor),
    )
    # ⛔ THE ATTEMPT IS ON THE ROW BEFORE THE IRREVERSIBLE CALL, and its number comes from the
    # LIST — never from a counter something else could also write. `process_invoke_data`
    # answers the txid, so the entry is opened without one and settled a moment later.
    attempt = await append_attempt("payout_requests", rid, "beam")
    try:
        txid = await w.submit(raw, f"payout {rid}")
    except beam.Halted as e:
        # The switch is checked BEFORE process_invoke_data, so nothing was signed here either.
        await db().payout_requests.update_one(
            {"_id": rid, "status": "releasing", "beam_txid": {"$exists": False}},
            {
                "$set": {
                    "status": "scheduled",
                    "status_at": time.time(),
                    "updated_at": time.time(),
                    "hold_reason": str(e),
                },
                "$unset": {"release_call_at": ""},
            },
        )
        return
    except beam.BeamError as e:
        await tg.alert(
            "payout_send_unconfirmed",
            f"Payout send UNCONFIRMED: the wallet did not answer ({beam.redact(e)[:160]}). NOT "
            f"re-sending — the next pass resolves it from the chain",
            request_id=rid,
        )
        return
    # the txid is EVIDENCE and is written first: losing it is losing the only thing that can
    # ever prove this crossing without a second signature.
    await _set("payout_requests", rid, beam_txid=txid, released_at=time.time())
    await settle_attempt("payout_requests", rid, attempt, str(txid), "submitted")
    await register_attribution(
        "payout_requests", rid, txid, float_addr, rid, "payout_attribution", "request_id"
    )


async def _payout_any_asset(row: dict[str, Any]) -> None:
    """The v1.1 branch: bETH → ETH to OUR distributor (`waiting_for_dep_eth`), then a cross-chain order
    to the user's chain/asset (`waiting_for_swap_to_target_asset`), then `sent`. The statuses
    exist so an order can carry the step; the handlers refuse — nothing is implemented."""
    why = (
        "the any-asset payout branch (bETH → ETH to our distributor, then a cross-chain swap to the "
        "user's chain and asset) is designed but not implemented"
    )
    if not settings.payout_instant_enabled:
        why += "; PGAS_PAYOUT_INSTANT_ENABLED=0"
    await _hold("payout_requests", row["_id"], why, key=f"payout-anyasset:{row['_id']}", dark=True)


# ------------------------------------------------------------------ payout: instant (Ethereum)
#
# The user asked for their ETH now, so it does not go near the bridge. We hold ETH on Ethereum
# already (the distributor float), the transfer is 21,000 gas, and the whole order is one signed
# transaction that either mines or does not.
#
# Everything the direct path was built out of applies one venue along:
#
#   * BROADCAST IS NOT DONE. The hash is on the row before the bytes leave; `sent` needs a
#     receipt with status 1, `to == W` and `value == delivered`, read back from the chain.
#   * A RETRY NEVER RE-SIGNS. One order signs one transaction (`distributor.sign_transfer`), the
#     bytes are stored, and every later pass hands the SAME bytes to the pool again. The order
#     ends only when the nonce is proven consumed by a DIFFERENT transaction.
#   * EVERY GUARD NEEDS THE LEVEL IT PROTECTS. The float gate is the float this pass reads
#     against delivered + gas × headroom; the economic gate is what THIS order funded against
#     what the gas actually costs; the nonce gate is a conditional update on the distributor row.
#   * AN UNREADABLE ANSWER IS NEVER A VERDICT. Gas, float, nonce, receipt: each of them HOLDS or
#     defers when the pool would not answer, and none of them substitutes a zero.


def delivered_groth(row: dict[str, Any]) -> int:
    """WHAT THE WALLET RECEIVES — one reader, for every gate, every signature and the ledger.

    Since the fee model went "on top when affordable, out of the amount otherwise" an order
    carries both what the user asked for and what actually lands: `delivered_groth`. A row
    written before that carries only `amount_groth`, and for it the two are the same number.
    Two readers of this would let an order be GATED on one amount and PAID another."""
    v = row.get("delivered_groth")
    if v is None:
        v = row.get("amount_groth") or 0
    return int(v)


def instant_gas_charged_groth(row: dict[str, Any]) -> int:
    """What THIS order funded the distributor's gas with — the number the economic gate measures
    against, exactly as `bridge_budget_groth` does for a crossing. ONE reader (law 9).

    An instant order is charged `gas_fee_groth` and no bridge fee; a row written by an older
    build put its whole pass-through in `bridge_fee_groth`, and under the model it was written
    under that WAS the budget. A row with NEITHER funded nothing — and since T34b that is not a
    reason to skip the gate, it is the one order paying is guaranteed to lose money on
    (§M3: `_instant_plan` holds it for a re-quote instead)."""
    v = row.get("gas_fee_groth")
    if v is None:
        v = row.get("bridge_fee_groth") or 0
    return int(v)


def instant_hashes(row: dict[str, Any]) -> list[str]:
    """EVERY Ethereum hash this order has ever signed, newest first. ONE reader (law 9).

    ⛔ AN ORDER CAN HAVE TWO SETS OF BYTES AND ONLY ONE NONCE. The fee-bumped replacement
    (`_instant_fee_bump`) is the single documented exception to "a retry never re-signs", and it
    is safe for exactly one reason: both transactions carry the same nonce, so the chain can
    include AT MOST ONE of them. Which one is the chain's choice, not ours — so every verdict
    this module reaches (mined? ours? did somebody else take the nonce?) has to be asked about
    all of them. Reading only `instant_tx` would make a late inclusion of the original look like
    "our transaction is nowhere", which is the one finding that ends a paid-for order.

    The list is derived from the APPEND-ONLY `attempts` (T40) plus the hash currently on the
    row: nothing here is a second, mutable copy of the same fact."""
    out: list[str] = []
    cur = str(row.get("instant_tx") or "")
    if cur:
        out.append(cur)
    for a in reversed(attempts_of(row)):
        if str(a.get("kind") or "") != "eth":
            continue
        h = str(a.get("txid") or "")
        if h and h not in out:
            out.append(h)
    return out


# How many signed-and-unmined instant orders one pass will count before it refuses to answer.
# A cap is not a limit on the queue: it is the point at which "how much of the float is already
# spoken for" stops being a number this pass can measure, and an unmeasurable float HOLDS (law
# 8) rather than being under-counted — an under-count is a gate that admits a spend twice.
INFLIGHT_CAP = 500


class FloatUnmeasurable(RuntimeError):
    """What the float already owes cannot be summed this pass — too many rows to read, or a row
    whose commitment cannot be valued. ⛔ It is NOT a small number and it is NOT zero: an
    under-count is a gate that admits one spend twice, so the caller HOLDS (law 8)."""


async def instant_inflight(addr: str, exclude_rid: str | None = None) -> dict[str, Any]:
    """What this distributor has already SIGNED AND NOT SEEN MINED — one read, two facts.

    `{"wei": Σ value + gas, "nonce_floor": the lowest nonce a NEW order may use, "rows": n}`.

    ⛔ **`eth_getBalance` AT `latest` COUNTS WHAT IS MINED, NOT WHAT IS COMMITTED.** A transfer
    signed a minute ago is in a mempool with the money still showing in the balance, so three
    orders gated on the raw balance all pass, all sign, and the last two produce transactions
    that can never be paid for. The float gate therefore subtracts this — within one pass AND
    across passes, because the rows outlive the process.

    ⛔ **AND THE NONCE FLOOR IS THE SAME READ.** `_payout_instant` claims the ROW (writing
    `instant_nonce`) and reserves the nonce in a second update: a process killed between the two
    leaves an order owning a nonce the distributor's record never advanced past. The next order
    would read that same nonce and sign a second transaction over it. `nonce_floor` is
    `max(instant_nonce) + 1` over everything in flight, and it is what repairs the record before
    anything is signed (`distributor.ensure_nonce_reserved`).

    `exclude_rid` keeps a row from being counted against ITSELF when its own gates are re-run —
    but it stays in the nonce floor, because it does own that nonce."""
    want = str(addr).lower()
    rows = await db().payout_requests.find({"status": PAYING}).limit(INFLIGHT_CAP + 1).to_list(
        INFLIGHT_CAP + 1
    )
    if len(rows) > INFLIGHT_CAP:
        raise FloatUnmeasurable(
            f"{len(rows)}+ payouts are in flight at once — more than one pass reads, so what "
            f"the float already owes cannot be measured this pass"
        )
    wei = 0
    floor = 0
    n = 0
    for r in rows:
        if str(r.get("instant_from") or "").lower() != want:
            continue
        if r.get("instant_nonce") is not None:
            floor = max(floor, int(r["instant_nonce"]) + 1)
        if exclude_rid is not None and str(r["_id"]) == str(exclude_rid):
            continue
        n += 1
        raw_value = str(r.get("instant_value_wei") or "")
        # a row from before the field existed is still a commitment: derive it, never call it 0
        try:
            value = int(raw_value) if raw_value else delivered_groth(r) * _asset_of(r).grid
        except (KeyError, ValueError) as e:  # an unvaluable row is unmeasurable, never free
            raise FloatUnmeasurable(
                f"payout {r['_id']} is in flight and what it commits cannot be valued ({e}) — "
                f"refusing to gate a signature on a float that is missing a number"
            ) from e
        wei += value + int(str(r.get("instant_gas_cost_wei") or 0) or 0)
    return {"wei": wei, "nonce_floor": floor, "rows": n}


async def committed_float_wei(addr: str, exclude_rid: str | None = None) -> int:
    """Σ (value + gas) over every instant transfer this distributor has signed and not seen
    mined — THE ONE READER of "how much of the float is already spoken for" (law 9).

    Subtracted from the live balance by every float gate, so the answer a gate refuses on and
    the answer an operator reads are the same number."""
    return int((await instant_inflight(addr, exclude_rid))["wei"])


def not_a_refill() -> dict[str, Any]:
    """The query fragment that excludes the distributor's OWN top-ups from anything counting
    user payouts — ONE reader (law 9), used by `routers/stats` for every public counter.

    A refill is a treasury→own-address crossing: no account was debited for it, `_book_release`
    books nothing, and no refund path can reach it. Counting one as a payout tells the public —
    and us — that we served somebody we did not serve. Both halves are asserted because a row
    written before `mode` existed carries only the account, and one written by a future mode
    carries only the mode."""
    return {"mode": {"$ne": REFILL}, "account_id": {"$ne": REFILL_ACCOUNT}}


async def _instant_hold(rid: str, reason: str, key: str, dark: bool = False) -> None:
    await _hold("payout_requests", rid, reason, key=f"{key}:{rid}", dark=dark)


async def _instant_plan(
    row: dict[str, Any], resume_nonce: int | None = None, quiet: bool = False
) -> dict[str, Any] | None:
    """⛔ **EVERY GATE AN INSTANT TRANSFER MUST PASS, IN ONE IMPLEMENTATION** — asked before the
    first signature, again before the crash-window resume's, and again before the one fee-bumped
    replacement. Returns the plan, or None when a gate refused (with its reason ON the row).

    Two implementations of one gate will disagree and one of them will reach money (law 9): the
    resume path used to check the flag, the kill switch and the key and NOTHING ELSE, so an order
    whose float had drained away in the hours since the crash would sign anyway.

    `resume_nonce` is the nonce the row ALREADY OWNS. For a new order the nonce comes from the
    distributor's record and must agree with the chain; for a resume it comes from the row and
    the only question is whether something else has already spent it.

    `quiet` is for the ONE caller whose refusal is not the order's refusal: the fee bump
    (`_instant_fee_bump`) is an optional improvement on a transfer that is ALREADY signed and
    broadcast, and its "not this pass" must not park that row. A hold there would stamp
    `hold_at` (five minutes of `_due` backoff on a payout in flight) and, for the flag-off case,
    `dark` — and a row whose ETH is on the wire is never dark: that flag describes the CURRENT
    reason for waiting or it describes nothing. So a quiet refusal writes a note on the row and
    a log line, and the order's own path keeps doing exactly what it was doing.

    Order matters: the free refusals first, then the chain reads, then the one read that costs a
    round trip per pass (`_dest_still_a_wallet`) — and NOTHING is claimed or signed in here."""
    rid = row["_id"]
    resuming = resume_nonce is not None
    asset = _asset_of(row)

    async def refuse(reason: str, key: str, dark: bool = False) -> None:
        """This gate said no — held on the row, or noted quietly (see `quiet` above). Either way
        it is written down: a refusal is not a trade and not a failure, but every decision path
        writes a row (law 12)."""
        if quiet:
            await _checkpoint(
                "payout_requests",
                rid,
                instant_plan_note=reason[:300],
                instant_plan_note_at=time.time(),
            )
            log.info("instant payout %s: no replacement this pass — %s", rid, reason)
            return
        await _instant_hold(rid, reason, key, dark=dark)
    if asset.key != "ETH":
        await refuse(
                        f"instant payouts are ETH only; this request is {asset.key}",
            "payout-instant-asset",
        )
        return None
    delivered = delivered_groth(row)
    if delivered <= 0:
        await refuse(
                        f"this order delivers {delivered} groth — an order with nothing to deliver cannot "
            f"be paid and a human has to look at this row",
            "payout-instant-amount",
        )
        return None
    if not settings.payout_instant_enabled:
        # §9.7: the request id, never W — api.log otherwise pairs request_id → destination.
        log.info(
            "would pay instantly %s: %s ETH (PGAS_PAYOUT_INSTANT_ENABLED=0)",
            rid,
            tg.fmt_groth(delivered),
        )
        await refuse(
                        "PGAS_PAYOUT_INSTANT_ENABLED=0 — the transfer is DARK and nothing was signed",
            "payout-instant-dark",
            dark=True,
        )
        return None
    if workers.paused():
        return None  # the movers refuse anyway; do not even plan while the switch is set
    if not distributor.configured():
        await refuse(
                        "PGAS_DISTRIBUTOR_KEY_FILE is not set, so there is no distributor to pay from",
            "payout-instant-nokey",
            dark=True,
        )
        return None
    try:
        signer = distributor.signer()
    except distributor.KeyFileError as e:
        # the message names the path and the mode; `KeyFileError` is built so it cannot name
        # the key itself
        await refuse(f"the distributor key could not be loaded: {e}", "payout-instant-key")
        return None
    # THE DESTINATION, in the two ways a signature could fail after the nonce is reserved — a
    # malformed address and our own address. Both are asked BEFORE anything is claimed, because
    # a reserved nonce that never gets signed is a gap that stalls every payout behind it.
    try:
        dest = to_checksum_address(str(row["W"]))
    except (ValueError, TypeError, KeyError):
        await refuse(
                        "this order's destination is not a valid Ethereum address",
            "payout-instant-dest",
        )
        return None
    if dest.lower() == signer.address.lower():
        await refuse(
                        "this order's destination is the distributor's own address — that is not a payout",
            "payout-instant-self",
        )
        return None
    rpc = get_rpc()
    try:
        # the distributor's row, created on first use with its nonce seeded FROM THE CHAIN
        drow = await distributor.ensure_row(rpc)
    except distributor.Unreadable as e:
        await refuse(
                        f"the distributor's own nonce could not be read to register it ({e})",
            "payout-instant-registerread",
        )
        return None
    try:
        est = await distributor.fee_estimate(rpc)
    except distributor.Unreadable as e:
        await refuse(
                        f"the gas price could not be read ({e}) — refusing to price a transfer from a "
            f"constant",
            "payout-instant-gasread",
        )
        return None
    delivered_wei = delivered * asset.grid
    gas_cost = int(est["gas_cost_wei"])
    # ⛔ THE CYCLE-LEVEL ECONOMIC GATE, the instant path's version of the subsidy ceiling the
    # crossing has. Every per-leg check can pass on an order that costs us more to deliver than
    # it charged: the gas was quoted when the order was written and is PAID when it is released,
    # and one base-fee tick between the two is ordinary. `relayer_subsidy()` is the ONE reader
    # of how much of that we absorb, shared with the crossing so the two cannot drift.
    charged = instant_gas_charged_groth(row)
    if charged <= 0:
        # ⛔ NOT "no budget, no gate" (T34b M3). An order that funded NOTHING is the one order
        # paying is CERTAIN to lose money on, and it is not a market condition that will pass —
        # it is a row that was written before instant orders were priced, or by a build that
        # priced them somewhere else. A human re-quotes it; the machine never pays it.
        await refuse(
                        "instant order not priced for gas — re-quote. This row funded no gas at all "
            "(neither gas_fee_groth nor bridge_fee_groth), so paying it would be a delivery at "
            "our own expense with nothing to measure it against",
            "payout-instant-unpriced",
        )
        return None
    # integers throughout: a float multiplication of a wei amount silently loses precision
    # above ~9×10^15, and this is a gate that admits a spend
    subsidy_bps = max(10_000, int(round(relayer_subsidy() * 10_000)))
    budget_wei = charged * asset.grid * subsidy_bps // 10_000
    if gas_cost > budget_wei:
        await refuse(
                        f"the gas for this transfer costs {gas_cost} wei and this payout funded "
            f"{charged * asset.grid} wei of it (subsidy limit {relayer_subsidy():g}×) — "
            f"refusing to pay out at a loss",
            "payout-instant-subsidy",
        )
        return None
    try:
        have = await distributor.float_wei(rpc, signer.address)
    except distributor.Unreadable as e:
        # ⛔ AN UNREADABLE FLOAT IS NOT AN EMPTY ONE AND CERTAINLY NOT A FULL ONE (law 8).
        await refuse(
                        f"the distributor's float could not be read ({e}) — refusing to sign a transfer "
            f"whose funding nobody could measure",
            "payout-instant-floatread",
        )
        return None
    await distributor.record_float(signer.address, have)
    try:
        infl = await instant_inflight(signer.address, exclude_rid=str(rid) if resuming else None)
    except FloatUnmeasurable as e:
        await refuse(str(e), "payout-instant-inflight")
        return None
    # ⛔ THE FLOAT GATE IS AGAINST WHAT IS FREE, NOT WHAT IS VISIBLE (T34b H1). Everything this
    # distributor has already signed is spent from this order's point of view, whatever
    # `eth_getBalance` still shows.
    committed = int(infl["wei"])
    free = have - committed
    need = delivered_wei + gas_cost
    if free < need:
        await refuse(
                        f"the distributor's float holds {have} wei, of which {committed} is already signed "
            f"and unmined ({infl['rows']} in flight), leaving {free}; this payout needs {need} "
            f"({delivered_wei} to the wallet + {gas_cost} of gas at "
            f"{settings.instant_gas_headroom:g}× headroom)",
            "payout-instant-float",
        )
        return None
    if resuming:
        # ⛔ THE ROW OWNS THIS NONCE and nothing was ever broadcast for it (a row with bytes has
        # `instant_tx` and never reaches a resume). The only question is whether the account has
        # moved past it — which nothing of ours can have done, so it is somebody else.
        n = int(resume_nonce)
        try:
            latest = await distributor.nonce(rpc, signer.address, "latest")
        except distributor.Unreadable as e:
            await refuse(
                f"the distributor's nonce could not be read ({e}) — refusing to sign blind",
                "payout-instant-nonceread",
            )
            return None
        if latest > n:
            if quiet:
                # the bump path: this row HAS bytes on the wire, and whether a spent nonce means
                # "ours mined" or "a stranger" is `_payout_paying`'s question (it reads every
                # hash we ever signed before it says the second one). Never decided from here.
                return None
            await _hold_for_a_human(
                "payout_requests",
                rid,
                "status",
                PAYING,
                f"this order owns nonce {n} and nothing was ever broadcast for it, but the "
                f"account has already moved to {latest}. ⚠️ SOMETHING ELSE IS SIGNING WITH THIS "
                f"KEY: check for a second processor or a copy of the key file. Nothing is signed "
                f"on a fresh nonce automatically — the money stays reserved",
                "payout_instant_nonce_taken",
                "request_id",
            )
            return None
    else:
        # ⛔ THE NONCE OUR RECORD OWNS AND THE NONCE THE CHAIN REPORTS MUST BE THE SAME NUMBER.
        # Ahead of us and something else is signing with this key — a second process, a rotated
        # file still in use, a human. Behind us and a transaction we recorded has fallen out of
        # every mempool, which the in-flight handler resolves and a NEW order must not step over.
        # Either way this is not the moment to add a signature.
        n = int(drow.get("nonce_next") or 0)
        floor = int(infl["nonce_floor"] or 0)
        if floor > n:
            # THE CRASH WINDOW, REPAIRED BEFORE ANYTHING IS SIGNED (T34b H2). An order already
            # owns `floor - 1`; our record never advanced past it because the process died
            # between claiming the row and reserving the nonce. Giving this order the same number
            # would produce two transactions of which at most one can ever mine.
            await distributor.ensure_nonce_reserved(signer.address, floor - 1, f"recover:{rid}")
            drow = await db().distributors.find_one({"_id": signer.address.lower()}) or drow
            n = int(drow.get("nonce_next") or 0)
            log.warning(
                "distributor %s: nonce_next was %d with nonce %d already signed for by an "
                "in-flight order — recovered to %d before planning %s",
                signer.address, floor - 1, floor - 1, n, rid,
            )
        try:
            pending = await distributor.nonce(rpc, signer.address, "pending")
        except distributor.Unreadable as e:
            await refuse(
                f"the distributor's nonce could not be read ({e}) — refusing to sign blind",
                "payout-instant-nonceread",
            )
            return None
        if pending != n:
            await refuse(
                                f"the distributor's next nonce is {n} on our record and the chain reports "
                f"{pending} pending — refusing to sign until the two agree",
                "payout-instant-nonce",
            )
            return None
    # asked after the economic gates (so a waiting order costs no RPC calls) and before anything
    # irreversible: a destination that grew contract code cannot be paid by a plain transfer
    if not await _dest_still_a_wallet(row):
        return None
    return {
        "signer": signer,
        "est": est,
        "delivered": delivered,
        "delivered_wei": delivered_wei,
        "gas_cost": gas_cost,
        "nonce": n,
        "float_wei": have,
        "committed_wei": committed,
    }


async def _payout_instant(row: dict[str, Any]) -> None:
    """`scheduled` → `paying`: pass every gate, claim the row, claim the nonce, sign ONE
    transfer, broadcast it.

    Nothing is signed until every gate has passed (`_instant_plan`), and the nonce is claimed by
    a conditional update AFTER the row is claimed — so a lost race costs a discarded plan and
    never a gap in the account's nonce sequence, which would stall every payout behind it. The
    window between the two claims is the one a crash can leave open, and `_instant_plan` repairs
    it for the NEXT order rather than letting it hand out the same nonce twice."""
    rid = row["_id"]
    plan = await _instant_plan(row)
    if plan is None:
        return
    signer = plan["signer"]
    n = int(plan["nonce"])
    gas_cost = int(plan["gas_cost"])
    try:
        # provenance only — the instant path is proven by a RECEIPT, not by a block scan — so a
        # head nobody would answer must not cost this order its turn
        head = int(await get_rpc().block_number())
    except ethpipe.RpcError:
        head = 0
    claimed = await _advance(
        "payout_requests",
        rid,
        "status",
        "scheduled",
        PAYING,
        "payout_paying",
        f"Instant payout releasing: ETH {tg.fmt_groth(plan['delivered'])} from the distributor "
        f"(gas ≤ {gas_cost} wei)",
        "request_id",
        instant_nonce=n,
        instant_from=signer.address,
        instant_value_wei=str(plan["delivered_wei"]),
        instant_gas_cost_wei=str(gas_cost),
        eth_from_block=head,
        release_attempt_at=time.time(),
    )
    if claimed is None:
        return  # another pass owns it
    # ONE CONDITIONAL UPDATE CLAIMS THE NONCE. A read-then-write is not a claim: two orders in
    # one pass would both sign `n`, and only one of the two could ever mine — the other is a
    # payout that silently never happens.
    if not await distributor.reserve_nonce(signer.address, n, str(rid)):
        # nothing has been signed, so the order simply goes back in the queue
        await db().payout_requests.update_one(
            {"_id": rid, "status": PAYING, "instant_tx": {"$exists": False}},
            {
                "$set": {"status": "scheduled", "status_at": time.time(), "updated_at": time.time()},
                "$unset": {"instant_nonce": "", "instant_value_wei": ""},
            },
        )
        log.info("instant payout %s: nonce %d was taken by another order this pass", rid, n)
        return
    await _instant_sign_and_send({**row, **{"instant_nonce": n}}, signer, plan["est"], plan["delivered_wei"])


async def _instant_sign_and_send(
    row: dict[str, Any],
    signer: Any,
    est: dict[str, Any],
    delivered_wei: int,
    bump: bool = False,
) -> None:
    """Sign the one transaction this order will ever have, WRITE IT DOWN, then broadcast it.

    ⛔ THE EVIDENCE IS WRITTEN BEFORE THE BYTES LEAVE. A broadcast whose answer is lost has
    still landed; a hash we never recorded is a transfer nobody can ever prove or find, and the
    only way to resolve it would be a second signature. The nonce comes off the ROW, not off a
    fresh read — this order owns that number from the moment it was reserved.

    `bump` is the ONE case in which this is called twice for one order (`_instant_fee_bump`):
    the SAME nonce, destination and value at a higher fee. The previous hash is not overwritten
    — it stays in the append-only `attempts`, which is what `instant_hashes` reads — so both
    bundles of bytes remain provable and a late inclusion of either is recognised."""
    rid = row["_id"]
    n = int(row["instant_nonce"])
    try:
        tx = distributor.sign_transfer(
            signer,
            nonce=n,
            to=str(row["W"]),
            value_wei=int(delivered_wei),
            max_fee_wei=int(est["max_fee_wei"]),
            tip_wei=int(est["tip_wei"]),
            gas_limit=int(est["gas_limit"]),
        )
    except (distributor.DistributorError, ValueError) as e:
        # Nothing was signed. The gates above make this unreachable for an ordinary order, so it
        # is a fault and it gets a page — and the row stays `paying` with the nonce it owns
        # rather than going back to the queue, because the nonce is spent from our record's
        # point of view and `_instant_resume` is what re-tries it.
        await _set("payout_requests", rid, instant_sign_error=str(e)[:200])
        await tg.alert(
            "payout_instant_unsigned",
            f"Instant payout could NOT be signed: {tg.esc(str(e))[:200]}. Nothing was broadcast",
            request_id=rid,
        )
        return
    # the ATTEMPT, recorded before the bytes are offered to any endpoint. A re-broadcast of
    # these same bytes is NOT another attempt — it is the same transaction being offered again,
    # which is exactly what makes the instant path safe to retry at all. A fee bump IS another
    # attempt, and this is where its number is taken: BEFORE the broadcast, never after it.
    attempt = await append_attempt("payout_requests", rid, "eth", str(tx["hash"]), "signed")
    fields: dict[str, Any] = {
        "instant_attempt": attempt,
        "instant_tx": tx["hash"],
        "instant_raw": tx["raw"],
        "instant_nonce": int(tx["nonce"]),
        "instant_from": signer.address,
        "instant_to": tx["to"],
        "instant_value_wei": str(tx["value_wei"]),
        "instant_gas_limit": int(tx["gas_limit"]),
        "instant_max_fee_wei": str(tx["max_fee_wei"]),
        "instant_tip_wei": str(tx["tip_wei"]),
        "instant_gas_cost_wei": str(int(est["gas_cost_wei"])),
        "instant_signed_at": time.time(),
    }
    if bump:
        fields["instant_bumped_at"] = time.time()
        fields["instant_bumped_from"] = est.get("bumped_from")
        await db().payout_requests.update_one(
            {"_id": rid},
            {"$set": {"updated_at": time.time(), **fields}, "$inc": {"instant_bumps": 1}},
        )
    else:
        await _set("payout_requests", rid, **fields)
    await _instant_broadcast(rid, tx["raw"])


async def _instant_broadcast(rid: str, raw: str) -> str:
    """Hand `raw` to the pool and record what happened. Returns the outcome.

    ⛔ THE KILL SWITCH IS CHECKED INSIDE THE MOVER (law 13), not only by the planner: a switch
    thrown between the two halts the chain where it is, with the bytes signed, recorded and
    un-broadcast — which is a state the next pass resolves, not one that loses anything."""
    if workers.paused():
        await _set(
            "payout_requests",
            rid,
            instant_send_error="the kill switch is set; these bytes were not broadcast",
        )
        return "halted"
    res = await distributor.broadcast(get_rpc(), raw)
    outcome = str(res["outcome"])
    now = time.time()
    change: dict[str, Any] = {
        "$set": {"instant_last_try_at": now, "instant_outcome": outcome, "updated_at": now},
        "$inc": {"instant_broadcasts": 1},
    }
    if outcome in (distributor.ACCEPTED, distributor.KNOWN):
        # `already known` is a node telling us it HAS these bytes. That is acceptance said
        # differently, and it is what a re-broadcast is supposed to hit every single time.
        change["$set"]["instant_broadcast_at"] = now
        change["$unset"] = {"instant_send_error": ""}
    else:
        change["$set"]["instant_send_error"] = (" | ".join(res["errors"]) or outcome)[:400]
    await db().payout_requests.update_one({"_id": rid}, change)
    if outcome == distributor.REFUSED:
        # not a failure of the ORDER: the same bytes go out again next pass. It is worth saying
        # once an hour, because "every endpoint refuses this transaction" is usually ours to fix.
        await tg.send(
            f"REFUSED: every endpoint refused an instant payout's transaction — "
            f"{tg.esc(' | '.join(res['errors']))[:300]}. The SAME bytes will be offered again",
            key=f"instant-refused:{rid}",
            cooldown_s=3600,
        )
    return outcome


async def _instant_resume(row: dict[str, Any]) -> None:
    """A `paying` row with a nonce and no transaction — the one window a crash can leave open,
    between the nonce reservation and the row that records the signature.

    Signing here is not a second signature: it is the FIRST one, for a nonce this order already
    owns and that nothing else can be given. A row that HAS `instant_tx` never reaches here.

    ⛔ **EVERY GATE RUNS AGAIN** (T34b M2). Hours can pass between the crash and the recovery:
    the float can have drained, the gas price can have left the subsidy behind, the destination
    can have grown code, and the nonce can have been spent by whatever else is holding this key.
    A resume that re-checked only the flag and the key was a signature admitted by gates that
    were true yesterday."""
    rid = row["_id"]
    if row.get("instant_nonce") is None:
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            PAYING,
            "this order is in flight with neither a nonce nor a transaction, so nothing can be "
            "found or re-sent for it automatically",
            "payout_instant_lost",
            "request_id",
        )
        return
    n = int(row["instant_nonce"])
    plan = await _instant_plan(row, resume_nonce=n)
    if plan is None:
        return
    # ⛔ AND THE RECORD IS MADE TO COVER THE NONCE BEFORE THE BYTES EXIST (T34b H2). The crash
    # may have happened before `reserve_nonce` ran at all; this is idempotent, so it repairs that
    # case and changes nothing in the ordinary one.
    if not await distributor.ensure_nonce_reserved(plan["signer"].address, n, str(rid)):
        await _instant_hold(
            rid,
            f"the distributor's record could not be made to cover nonce {n}, which this order "
            f"already owns — refusing to sign until it does",
            "payout-instant-reserve",
        )
        return
    await _instant_sign_and_send(row, plan["signer"], plan["est"], int(plan["delivered_wei"]))


async def _nonce_was_taken(row: dict[str, Any]) -> bool:
    """Has a DIFFERENT transaction consumed this order's nonce?

    ⛔ TWO DISPROOFS, BECAUSE EITHER ALONE IS A LIE. "The account has moved past our nonce" is
    true of our OWN transaction the moment it mines. "No endpoint has our transaction" is true
    of an endpoint that simply lags. Only both together mean somebody else spent it — and both
    are asked so that an unreadable answer says False, never True: this is the one path that
    ends a paid-for order, and it must not end one on a network blink.

    ⛔ AND IT ASKS ABOUT **EVERY HASH THIS ORDER EVER SIGNED** (T34b L2). After a fee bump the
    row carries two, sharing one nonce; if the chain included the ORIGINAL, the replacement is
    nowhere and reading only the newest would page "SOMETHING ELSE IS SIGNING WITH THIS KEY"
    about our own transaction — sending an operator to hunt for a second processor that does not
    exist, which is precisely the page that teaches them to skim the pager (law 15)."""
    rpc = get_rpc()
    addr = str(row.get("instant_from") or "")
    hashes = instant_hashes(row)
    if not addr or not hashes or row.get("instant_nonce") is None:
        return False
    try:
        latest = await distributor.nonce(rpc, addr, "latest")
    except distributor.Unreadable:
        return False
    if latest <= int(row["instant_nonce"]):
        return False  # the nonce is still ours to spend
    # ASK EVERY ENDPOINT (law 8, and the 2026-09-10 incident where one endpoint's `null` was
    # read as the chain's verdict). `answered == 0` is nobody could be asked, which is never
    # a verdict.
    answered_any = False
    for h in hashes:
        tx, answered, _errors = await ethpipe.visible_tx(rpc, h)
        answered_any = answered_any or bool(answered)
        if tx is not None:
            return False  # one of OURS is what the account moved past
    return answered_any


async def _book_instant_release(row: dict[str, Any]) -> None:
    """Scheduled −= delivered + our fee + the gas it funded · Sent += delivered.

    Guarded on EVERY half for the reason `_book_release` is: `ledger.release` appends three
    entries and a guard that reads only the first declares "already booked" for ever when a
    later append failed, leaving that groth stuck in Scheduled where nothing can clear it.
    WHATEVER THE SCHEDULE DEBITED, THE RELEASE CLEARS — a row with no gas charge never debited
    one and gets no third entry."""
    rid = row["_id"]
    gas = instant_gas_charged_groth(row)
    if (
        await ledger.find_entry("release", rid)
        and await ledger.find_entry("fee", rid)
        and (not gas or await ledger.find_entry("instant_gas_fee", rid))
    ):
        return
    await ledger.release(
        row["account_id"],
        row.get("asset") or "ETH",
        delivered_groth(row),
        int(row.get("fee_groth") or 0),
        rid,
        f"instant payout delivered, Ethereum tx {row.get('instant_tx')}",
        gas_fee_groth=gas,
    )


async def _instant_stuck_page(row: dict[str, Any], now: float) -> None:
    """The guard at the level it protects: the money is signed and on the wire, so this is a
    page and not a refusal — nothing about the retry changes.

    Called from BOTH in-flight outcomes (T34b M1): no receipt yet, and a receipt nobody could
    read. The second used to `return` on a log line, so an instant payout whose pool had gone
    dark aged silently for as long as the pool stayed dark."""
    rid = row["_id"]
    since = float(row.get("status_at") or row.get("updated_at") or 0)
    if not since or now - since <= float(settings.instant_stuck_after_s):
        return
    unread = int(row.get("instant_receipt_unreadable") or 0)
    tail = f", {unread} unreadable receipt read(s)" if unread else ""
    await tg.send(
        f"STUCK: an instant payout has been on the wire for {int((now - since) // 60)} min with "
        f"no receipt after {int(row.get('instant_broadcasts') or 0)} broadcast(s) of the same "
        f"bytes{tail}. <code>{rid}</code>",
        key=f"instant-stuck:{rid}",
        cooldown_s=6 * 3600,
    )


async def _instant_receipt_unreadable(row: dict[str, Any], err: str) -> None:
    """⛔ NOT A VERDICT — AND NOT SILENT EITHER (T34b M1, law 12).

    A receipt we could not read is not a receipt that says no, and this is the status where the
    money is already on the wire. But a decision path that only logs is one nothing can alert
    on: the row records what happened and how often, the pass's WAITING digest names it once per
    kind, and the same `PGAS_INSTANT_STUCK_AFTER_S` clock that watches a transfer with no
    receipt watches this one too.

    It deliberately does NOT stamp `hold_at`: that field is `_due`'s own five-minute backoff, and
    a payout in flight must be re-read on every pass, not parked for five minutes because one
    endpoint blinked."""
    rid = row["_id"]
    now = time.time()
    reason = (
        f"the receipt for this instant payout could not be read ({err[:160]}) — the money is on "
        f"the wire, nothing here is a verdict, and the same bytes stay the order's only transaction"
    )
    await db().payout_requests.update_one(
        {"_id": rid},
        {
            "$set": {
                "instant_receipt_error": reason,
                "instant_receipt_error_at": now,
                "updated_at": now,
            },
            "$inc": {"instant_receipt_unreadable": 1},
        },
    )
    _digest("payout_requests", rid, reason, key=f"payout-instant-receiptread:{rid}")
    log.info("instant payout %s: the receipt could not be read this pass (%s)", rid, err)
    await _instant_stuck_page(row, now)


async def _instant_settle(row: dict[str, Any], seen: dict[str, Any], value_wei: int) -> None:
    """A matching receipt, `PGAS_INSTANT_CONFIRMATIONS` blocks deep, read AGAIN — then booked.

    ⛔ **ONE CONFIRMATION IS A CANDIDATE, NOT A FACT** (T34b R1). A re-organisation removes the
    block, and with it the transfer; a row already marked `sent` and a ledger already credited
    have no un-send, so the user would be told their ETH left when it never did. The floor is
    two blocks (~25 s), which is inside the instant path's own one-minute promise.

    And the receipt is READ AGAIN at the moment of booking, from the pool, over every hash this
    order signed: the read at the top of the pass proved a candidate, and between the two the
    chain can reorganise. `broadcast ≠ done` cuts here too — so does `mined ≠ settled`."""
    rid = row["_id"]
    block = int(seen.get("block") or 0)
    try:
        head = int(await get_rpc().block_number())
    except ethpipe.RpcError as e:
        # a head nobody would answer is not a confirmation count, and it is certainly not zero
        log.info("instant payout %s: the head could not be read to count confirmations (%s)", rid, e)
        return
    need = max(1, int(settings.instant_confirmations))
    confs = (head - block + 1) if block else 0
    if confs < need:
        await _checkpoint(
            "payout_requests",
            rid,
            instant_mined_block=block,
            instant_mined_hash=str(seen.get("hash") or ""),
            instant_confirmations_seen=int(max(confs, 0)),
        )
        log.info(
            "instant payout %s: mined in block %d with %d confirmation(s) of %d — not booking yet",
            rid, block, max(confs, 0), need,
        )
        return
    try:
        again = await distributor.verify_receipt_any(
            get_rpc(), instant_hashes(row), str(row["W"]), value_wei
        )
    except distributor.Unreadable as e:
        await _instant_receipt_unreadable(row, str(e))
        return
    if not (again["mined"] and again["ok"]):
        # the candidate is gone or no longer matches: back to the in-flight machinery, which
        # re-broadcasts the SAME bytes. Nothing is booked and nothing is failed.
        log.warning(
            "instant payout %s: the receipt did not survive the confirmation floor (%s) — not "
            "booking", rid, again.get("problem") or "it is no longer mined",
        )
        return
    tx_hash = str(again.get("hash") or seen.get("hash") or row.get("instant_tx") or "")
    # BOOK FIRST, MARK SECOND: a `sent` row whose ledger entry never landed leaves the user's
    # money parked in Scheduled with nothing able to clear or refund it.
    await _book_instant_release({**row, "instant_tx": tx_hash})
    await _advance(
        "payout_requests",
        rid,
        "status",
        PAYING,
        "sent",
        "payout_sent",
        f"Instant payout SENT: ETH {tg.fmt_groth(delivered_groth(row))} delivered on Ethereum "
        f"in block {int(again['block'])} ({confs} confirmations)",
        "request_id",
        eth_tx=tx_hash,
        eth_block=int(again["block"]),
        eth_proof="receipt",
        eth_confirmations=int(confs),
        sent_at=time.time(),
    )


async def _instant_fee_bump(row: dict[str, Any], hashes: list[str], now: float) -> bool:
    """⛔ **THE ONE DOCUMENTED EXCEPTION TO "A RETRY NEVER RE-SIGNS"** (T34b L1). Returns True
    when it signed a replacement.

    A transaction can be un-minable rather than slow: signed at a fee the market left behind,
    dropped by every mempool, and re-broadcast for ever into nodes that will not keep it. The
    same bytes will never mine, and the order's nonce is frozen behind it — so is every payout
    after it.

    **Why this is not the double spend the law forbids:** the replacement carries THE SAME
    NONCE, the same destination and the same value. Two transactions with one nonce are two
    candidates for one slot; the chain includes at most one, ever. That is a property of the
    protocol, not of our bookkeeping — which is why this exception exists here and nowhere else,
    and why the nonce is re-read and required to be FREE before anything is signed.

    Its four conditions, all of which must hold:
      * `PGAS_INSTANT_STUCK_AFTER_S` has passed since THIS order's last signature;
      * no endpoint of the pool holds ANY of the hashes we have signed (one that does is a
        transaction that is waiting, not one that is dead — a second fee buys nothing);
      * the nonce is still free at `latest` (spent means the answer is a receipt, not a re-sign);
      * every gate `_instant_plan` owns still passes, and the float still covers the HIGHER gas.

    And it happens ONCE. A second replacement is a fee war with ourselves, and the operator has
    been paged by then."""
    rid = row["_id"]
    if int(row.get("instant_bumps") or 0) >= 1:
        return False
    if row.get("instant_nonce") is None or not row.get("instant_raw"):
        return False
    signed_at = float(row.get("instant_signed_at") or row.get("status_at") or 0)
    if not signed_at or now - signed_at <= float(settings.instant_stuck_after_s):
        return False
    rpc = get_rpc()
    for h in hashes:
        tx, answered, _errors = await ethpipe.visible_tx(rpc, h)
        if not answered or tx is not None:
            # unreadable, or somebody still holds it: neither is "this can never mine"
            return False
    n = int(row["instant_nonce"])
    try:
        latest = await distributor.nonce(rpc, str(row.get("instant_from") or ""), "latest")
    except distributor.Unreadable:
        return False
    if latest > n:
        return False  # the nonce is spoken for; `_nonce_was_taken` decides what that means
    plan = await _instant_plan(row, resume_nonce=n, quiet=True)
    if plan is None:
        return False
    old_max = int(str(row.get("instant_max_fee_wei") or 0) or 0)
    old_tip = int(str(row.get("instant_tip_wei") or 0) or 0)
    est = distributor.bump_fees(plan["est"], old_max, old_tip)
    if int(est["max_fee_wei"]) <= old_max:
        return False  # nothing to replace it with
    need = int(plan["delivered_wei"]) + int(est["gas_cost_wei"])
    free = int(plan["float_wei"]) - int(plan["committed_wei"])
    if free < need:
        # written down, not held: the ORDER is not waiting on this — its own bytes are still
        # being offered every pass, and parking the row would delay that for `hold_backoff_s`.
        await _checkpoint(
            "payout_requests",
            rid,
            instant_plan_note=(
                f"this transfer is un-minable and its replacement would cost {need} wei against "
                f"{free} free in the float — the SAME bytes keep being offered instead"
            ),
            instant_plan_note_at=time.time(),
        )
        log.warning(
            "instant payout %s: the fee-bumped replacement needs %d wei and %d is free — not "
            "re-signing", rid, need, free,
        )
        return False
    # ONE page, on the transition, with both prices: this is a deliberate second signature and
    # an operator should be able to see it happen without going looking for it.
    await tg.send(
        f"FEE BUMP: an instant payout has been un-minable for "
        f"{int((now - signed_at) // 60)} min and is being re-signed ONCE on the SAME nonce "
        f"({n}), destination and value — {old_max} → {int(est['max_fee_wei'])} wei/gas. Both "
        f"hashes stay on the row and at most one of them can ever mine. <code>{rid}</code>",
        key=f"instant-bump:{rid}",
        cooldown_s=6 * 3600,
    )
    log.warning(
        "instant payout %s: fee-bumped replacement of nonce %d (%d → %d wei/gas)",
        rid, n, old_max, int(est["max_fee_wei"]),
    )
    await _instant_sign_and_send(row, plan["signer"], est, int(plan["delivered_wei"]), bump=True)
    return True


async def _payout_paying(row: dict[str, Any]) -> None:
    """`paying`: read the receipt back, and only then call it sent.

    Three answers and three different actions. MINED AND OURS → the confirmation floor, then the
    release (`_instant_settle`). MINED AND NOT OURS (reverted, another destination, another
    amount) → hold for a human and page immediately; that is not a delivery however green the
    receipt looks. NOT MINED → re-broadcast the SAME bytes, unless a different transaction has
    provably taken our nonce, which is the only thing that ends this order without a delivery.

    Every question is asked about EVERY hash this order has signed (`instant_hashes`): after a
    fee bump there are two, sharing one nonce, and which of them the chain took is not ours to
    choose."""
    rid = row["_id"]
    asset = _asset_of(row)
    # ⛔ **THE BYTES ARE THE TEST, NOT THE HASH.** `_instant_sign_and_send` opens the attempt
    # (which carries a hash) BEFORE it records the transaction and broadcasts it, so a crash
    # between those two leaves a hash that nothing ever offered to any endpoint. That row has
    # nothing to re-broadcast and nothing that could have mined: it is the crash window, and it
    # is RESUMED — signing there is the first signature, not a second one. Keying this off the
    # hash instead would try to verify a transaction that never existed and then re-broadcast an
    # `instant_raw` that is not on the row.
    if not row.get("instant_raw"):
        await _instant_resume(row)
        return
    hashes = instant_hashes(row)
    value_wei = int(row.get("instant_value_wei") or delivered_groth(row) * asset.grid)
    try:
        seen = await distributor.verify_receipt_any(get_rpc(), hashes, str(row["W"]), value_wei)
    except distributor.Unreadable as e:
        # ⛔ NOT A VERDICT. A receipt we could not read is not a receipt that says no, and this
        # is the status where the money is already on the wire — but it is written down and it
        # ages against the stuck clock (T34b M1).
        await _instant_receipt_unreadable(row, str(e))
        return
    if seen["mined"]:
        if seen["ok"]:
            await _instant_settle(row, seen, value_wei)
            return
        # ⛔ THE NONCE IS SPENT BY OUR OWN TRANSACTION AND IT DID NOT DELIVER. Whether it
        # reverted or paid something else, a machine must not sign a SECOND transfer for one
        # order on the strength of a receipt it does not understand — that is the double spend
        # this whole block exists to prevent. A human reads the receipt; the money stays
        # reserved and the user's row still reads as delayed.
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            PAYING,
            f"this order's Ethereum transaction mined and is not this payout: "
            f"{seen['problem']}. Nothing is re-signed automatically — a second transfer for one "
            f"order is a double spend, so an operator reads the receipt and decides",
            "payout_instant_not_ours",
            "request_id",
        )
        return
    now = time.time()
    last = float(row.get("instant_last_try_at") or row.get("instant_signed_at") or 0)
    if now - last < float(settings.instant_rebroadcast_after_s):
        return  # give the mempool its time before doing anything at all
    if await _nonce_was_taken(row):
        # ⛔ **NEVER RE-SIGN.** The order's own bytes can no longer mine, but a fresh signature
        # on a fresh nonce would be a second independently-valid payment for one order — and
        # the very fact that brought us here says something else is signing with this key. So a
        # human owns it: still reserved, still "delayed" to the user, nothing broadcast.
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            PAYING,
            f"nonce {row.get('instant_nonce')} of the distributor was consumed by a different "
            f"transaction and none of this order's own transactions is on any endpoint — it can "
            f"never mine, and it is NEVER re-signed on a fresh nonce (that would be two payments "
            f"for one order). ⚠️ SOMETHING ELSE IS SIGNING WITH THIS KEY: check for a second "
            f"processor or a copy of the key file before doing anything with this row",
            "payout_instant_nonce_taken",
            "request_id",
        )
        return
    # …and the one case where the same bytes will never do: nobody holds them, the nonce is
    # free, and the market has moved past the fee they carry (L1). ONE replacement, same nonce.
    if await _instant_fee_bump(row, hashes, now):
        return
    # ⛔ THE SAME BYTES. Re-signing would give this one order two independently-valid
    # transactions; the hash on the row is the only thing that can ever prove this payout.
    await _instant_broadcast(rid, str(row["instant_raw"]))
    await _instant_stuck_page(row, now)


# --------------------------------------------------------------- the distributor's own float


async def refill_once() -> int:
    """Keep the distributor's ETH float above its floor. Returns how many refills it wrote (0 or 1).

    A refill is an ORDINARY DIRECT CROSSING that happens to pay our own address: bETH out of the
    treasury, through the pipe, delivered as ETH to the distributor. It therefore takes the same
    path, passes the same gates and is proven the same way — and it is NOT A USER PAYOUT. No
    account was debited for it, so `_book_release` books nothing and no refund path can ever
    reach it (`ledger.debited_groth` answers None, which is exactly "nothing to give back").

    One at a time, and only below the floor: a crossing takes ~66 minutes and costs a bridge fee,
    so a second one started while the first is in flight would be paid for twice for one top-up."""
    if not settings.payout_instant_enabled or not distributor.configured():
        return 0
    if workers.paused():
        return 0
    try:
        signer = distributor.signer()
    except distributor.KeyFileError as e:
        await tg.send(
            f"REFUSED: the instant distributor's key file cannot be loaded — {tg.esc(str(e))[:250]}",
            key="distributor-key",
            cooldown_s=3600,
        )
        return 0
    rpc = get_rpc()
    try:
        await distributor.ensure_row(rpc)
        have = await distributor.float_wei(rpc, signer.address)
    except distributor.Unreadable as e:
        # an unreadable float is not a float below the floor either: refill nothing, say so
        log.info("distributor float unreadable this pass (%s) — no refill decided", e)
        return 0
    await distributor.record_float(signer.address, have)
    need = distributor.plan_refill(
        have, settings.distributor_float_min_wei, settings.distributor_float_target_wei
    )
    if need <= 0:
        return 0
    if await db().payout_requests.count_documents(
        {"mode": REFILL, "status": {"$in": [*PAYOUT_ACTIVE, HELD]}}
    ):
        return 0  # one is already on its way
    asset = get_asset("ETH")
    groth = int(need) // asset.grid  # a crossing mints on the grid; the tail waits for next time
    if groth <= 0:
        return 0
    # the crossing the treasury is about to pay for, priced through the SAME reader a user's
    # order is priced through, with the same floor under the headroom. A gas price we cannot
    # read is a crossing we cannot price — and a refill is the least urgent decision in the
    # pass, so it simply waits for the next one rather than being written unpriced.
    try:
        fee_groth, _floor, _detail = await relayer_fee_for(asset, rpc)
    except (beam.BeamError, ethpipe.RpcError) as e:
        log.info("refill deferred: the crossing could not be priced (%s)", beam.redact(e))
        return 0
    headroom_bps = max(10_000, int(round(float(settings.bridge_headroom_min) * 10_000)))
    bridge = -(-int(fee_groth) * headroom_bps // 10_000)
    rid = f"refill-{uuid.uuid4().hex[:12]}"
    now = time.time()
    await db().payout_requests.insert_one(
        {
            "_id": rid,
            "account_id": REFILL_ACCOUNT,
            "asset": "ETH",
            "mode": REFILL,
            "W": signer.address,
            "amount_groth": groth,
            "delivered_groth": groth,
            "fee_groth": 0,  # we do not charge ourselves 2%
            "bridge_fee_groth": bridge,
            "release_at": now,
            "deliver_at": now + float(settings.bridge_eta_s),
            "status": "scheduled",
            "dest_chain": settings.eth_chain_id,
            "refill": True,
            "float_wei_at_plan": str(int(have)),
            "created_at": now,
            "updated_at": now,
        }
    )
    await distributor.record_refill(signer.address, rid, int(need))
    # ONE page per refill: the key names the row, and the row is written once.
    await tg.send(
        f"REFILL: the instant distributor's float is {have} wei, below the "
        f"{int(settings.distributor_float_min_wei)} floor — a crossing of "
        f"{tg.fmt_groth(groth)} ETH to it was scheduled. <code>{rid}</code>",
        key=f"distributor-refill:{rid}",
        cooldown_s=3600,
    )
    log.info("refill %s scheduled: %s ETH to the distributor (float %d wei)", rid, tg.fmt_groth(groth), have)
    return 1


# ----------------------------------------------------------------------------- payout: delayed

# What a retry must FORGET about the attempt that died, so the gates it re-enters cannot
# short-circuit on it. ⛔ `source_address` is deliberately NOT here: the crossing's own fresh
# address is created once per ORDER and every attempt funds and books through the same one, so
# a retry that made a second address would leave the first one holding value nothing reads.
_RETRY_CLEARS = (
    "beam_txid",          # the dead transaction: `_payout_releasing` short-circuits on it
    "release_call_at",    # the "this MAY have landed" marker of the attempt that is now proven dead
    "release_args",
    "msg_floor",
    "msg_id",             # the pipe message of a crossing that never happened
    "attribution",
    "attribution_error",
    "attribution_at",
    "kernel",
    "beam_kernel_seen_at",
    "beam_height_at_kernel",
    "beam_confirmations",
    "resolved_from_chain",
    "next_attempt_at",
)


async def _payout_delayed(row: dict[str, Any]) -> None:
    """A delayed order is a scheduled one with a reason. This decides whether it is its turn.

    Three gates, in this order and no other:

      1. **Is it due?** Before `next_attempt_at` there is nothing to say and nothing to write —
         a refusal that repeats every 30 s is the pager the operator learns to skim (law 15).
      2. **Has it been delayed for a day?** Then a HUMAN owns it. Still reserved, still
         "delayed" to the user, but a machine that has been trying one thing for 24 hours will
         not succeed on the next pass either.
      3. **⛔ IS THE PREVIOUS ATTEMPT PROVEN DEAD?** This is the double-spend gate
         (`previous_attempt_dead`), and it is asked BEFORE the row is handed back to the gates
         that sign things. Unknown, unreadable, pending, or dead-with-a-kernel all mean the row
         stays delayed with that reason — never a second send.

    The hand-back is ONE conditional update (`_advance` from `delayed`), so two processors that
    both read this row make exactly one attempt between them."""
    rid = row["_id"]
    now = time.time()
    # ⛔ **A FUNDED CROSSING HAS NO TURN TO WAIT FOR** (T52, 17:58Z — see `_delay`). Its money
    # has left the treasury for an address of its own; the only thing between it and the bridge
    # is a settled transfer and one invocation, and both are asked for on every pass.
    if now < float(row.get("next_attempt_at") or 0) and not awaiting_invocation(row):
        return  # not its turn; not a refusal, nothing to say
    since = float(row.get("delayed_since") or row.get("status_at") or 0)
    if since and now - since > DELAY_HOLD_AFTER_S:
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            DELAYED,
            f"this order has been delayed for {int((now - since) // 3600)} h after "
            f"{len(attempts_of(row))} attempt(s) — the last reason was "
            f"{str(row.get('hold_reason') or 'not recorded')!r}. The money is STILL RESERVED "
            f"and nothing is retried automatically any more; an operator decides",
            "payout_delayed_too_long",
            "request_id",
        )
        return
    # ⛔ **A BOOKED RELEASE NEVER RE-ENTERS THE LADDER** (T40b F6, §BOOKED-IS-LANDED). If this
    # row carries `kernel_at` or `release_booked_txid` and something has nevertheless delayed it,
    # two facts about one order contradict each other: the release was booked (the ledger entry
    # exists) and a status read says it is not settled. It is NOT resolved by trying again — the
    # double-release alarm in `_book_release` lives on `release_booked_txid`, so a retry that
    # kept it would be silent about the second crossing and a retry that cleared it would blind
    # the alarm. A human owns it, with the contradiction written on the row.
    if row.get("kernel_at") or row.get("release_booked_txid"):
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            DELAYED,
            f"this order's release is BOOKED (kernel_at {row.get('kernel_at') or '—'}, txid "
            f"{str(row.get('release_booked_txid') or '—')[:12]}) and it has been delayed anyway "
            f"— the last reason was {str(row.get('hold_reason') or 'not recorded')!r}. A booked "
            f"release is never 'dead' whatever a status line says, so NOTHING is retried and "
            f"the money stays where it is until an operator decides",
            "payout_release_contradiction",
            "request_id",
        )
        return
    dead, why = await previous_attempt_dead(row)
    if not dead:
        # ⛔ NOT AN ERROR AND NOT A FAILURE — the row simply waits another rung. Re-delaying
        # from `delayed` writes no event: the ladder is one decision repeated, not N.
        await _delay(row, DELAYED, f"previous attempt not settled: {why}")
        return
    claimed = await _advance(
        "payout_requests",
        rid,
        "status",
        DELAYED,
        "scheduled",
        "payout_retrying",
        f"Payout retrying: {why}; attempt {len(attempts_of(row)) + 1}",
        "request_id",
        retry_at=now,
        unset=_RETRY_CLEARS,
    )
    if claimed is None:
        return  # another pass owns it
    # …and it re-enters the ORDINARY gates, in this same pass: source selection, the float, the
    # wallet's spendable buckets, the free-coin count, the destination re-read, the fee budget.
    # A delayed row is a scheduled row with a reason, so it is decided by the same code and
    # never by a shortcut round it.
    fresh = await db().payout_requests.find_one({"_id": rid})
    if fresh is not None:
        await _payout_scheduled(fresh)


# ----------------------------------------------------------------------------- payout: releasing


async def _payout_releasing(row: dict[str, Any]) -> None:
    rid = row["_id"]
    asset = _asset_of(row)
    txid = row.get("beam_txid")
    if txid:
        # ⛔ THE REGISTRATION HAPPENS BEFORE THE ROW ADVANCES. An unregistered contract txid
        # books its whole flow — the bETH and the BEAM fee — to `__house__`, so the float this
        # and every later payout gates on keeps counting value that is already burned. The
        # send is never repeated; only the registration is.
        if not attributed(row, txid):
            # THE ROW SAYS WHERE IT BOOKS (`attribution_address`): a crossing funded from the
            # treasury's unshielded balance must not be registered against the max-privacy
            # address just because a retry re-derived the answer from today's float.
            float_addr = await attribution_address(row)
            if not float_addr or not await register_attribution(
                "payout_requests", rid, str(txid), float_addr, rid,
                "payout_attribution", "request_id",
            ):
                await _hold(
                    "payout_requests",
                    rid,
                    "the crossing is on the chain but BeamPay has not accepted its attribution"
                    + (" (no address on the row or in config to attribute it to)"
                       if not float_addr else "")
                    + " — the transaction is NEVER re-sent; the registration is what retries",
                    key=f"payout-attr:{rid}",
                )
                return
            row = {**row, "attribution": {"txid": str(txid)}}
        await _advance(
            "payout_requests",
            rid,
            "status",
            "releasing",
            "bridging",
            "payout_bridging",
            f"Payout bridging: {asset.key} {tg.fmt_groth(int(row['amount_groth']))} — Beam kernel "
            f"then {settings.beam_confirmations} confirmations",
            "request_id",
        )
        return
    since = float(row.get("release_call_at") or 0)
    if not since:
        # the intent was written and the process died before the call: provably nothing signed
        await db().payout_requests.update_one(
            {"_id": rid, "status": "releasing", "beam_txid": {"$exists": False}},
            {"$set": {"status": "scheduled", "status_at": time.time(), "updated_at": time.time()}},
        )
        log.warning("payout %s: intent without a call — handed back to scheduled", rid)
        return
    # ⛔ THE TIMEOUT IS CHECKED FIRST. It used to run after the resolver, so the "a human
    # decides" hold only DELAYED the adoption: `since` has no upper bound, and a held row
    # adopted an unrelated claim two hours later and booked the ledger release on it.
    if time.time() - since > UNRESOLVED_S:
        reserved = int(row["amount_groth"]) + int(row.get("relayer_fee_groth") or 0)
        await _hold_for_a_human(
            "payout_requests",
            rid,
            "status",
            "releasing",
            f"the release response was lost and no transaction matching this crossing turned up "
            f"within {UNRESOLVED_S // 60} min — this needs a human and is NOT auto-retried. "
            f"{tg.fmt_groth(reserved)} {asset.key} stays RESERVED against the shielded float "
            f"(the txid was never captured, so the burn was never registered and the "
            f"max-privacy balance did not fall either); an operator clears the reservation by "
            f"writing float_resolved on this row",
            "payout_unresolved",
            "request_id",
        )
        return
    w = beam.wallet()
    bp = beampay.beampay()
    # IDENTITY: the signed amount this call would have moved, and nothing another row carries.
    # The history is BeamPay's (`GET /transactions` with no address — the only way to see a
    # contract tx, whose sender and receiver are both ""); the wallet's own `tx_list` is not
    # called, and is not on this client any more.
    #
    # ⛔ **AN OUTFLOW IS POSITIVE.** BeamPay books a contract flow as
    # `available_delta = -amount` under the comment "A POSITIVE invoke amount is a wallet
    # OUTFLOW" (process_payments.py:216), and the live rows agree: a b2e send on the pipe
    # carries `{asset 36, amount: +15995980}` against a −0.15995980 bETH movement, while claims
    # on the same pipe are negative. Asking for −(amount + relayerFee) therefore searched for
    # exactly the shape of a CLAIM on this pipe: our own send could never be found, and a
    # deposit claim of the same size WOULD be — the payout would then adopt someone else's
    # kernel, register it to the max-privacy float, and book an irreversible ledger release on
    # a crossing that never happened.
    expect = int(row["amount_groth"]) + int(row.get("relayer_fee_groth") or 0)
    tx = await bp.find_contract_tx(
        asset.beam_cid, asset.aid, since, expect, is_taken=taken_txid_check(exclude_request=rid)
    )
    if not tx or not tx.get("txId"):
        return
    # …and the transaction is still only a transaction. The CROSSING is proven by our own
    # outgoing message: THIS receiver, THIS amount, on this pipe (§IDENTITY-BEATS-BALANCE).
    msg_id = await _our_msg(w, row, asset)
    if msg_id is None:
        await _hold(
            "payout_requests",
            rid,
            "a contract transaction of exactly this size is on the wallet but no outgoing pipe "
            "message to this destination and amount exists — refusing to adopt it",
            key=f"payout-nomsgmatch:{rid}",
            cooldown_s=6 * 3600,
        )
        return
    try:
        claimed = await db().payout_requests.find_one_and_update(
            {"_id": rid, "status": "releasing", "beam_txid": {"$exists": False}},
            {
                "$set": {
                    "beam_txid": str(tx["txId"]),
                    "resolved_from_chain": True,
                    "msg_id": int(msg_id),
                    "updated_at": time.time(),
                }
            },
        )
    except DuplicateKeyError:
        # the database refused it: another row already carries this txid as its evidence
        await _hold(
            "payout_requests",
            rid,
            "the only matching contract transaction — or the pipe message it would be "
            "adopted with — is already booked to another request; refusing to adopt it",
            key=f"payout-txtaken:{rid}",
            cooldown_s=6 * 3600,
        )
        return
    if claimed is None:
        return
    # a resolved txid is still a contract txid of ours: register it too. BeamPay may already
    # have booked it to the house by now — `register_attribution` records that refusal and
    # pages rather than holding a crossing that has already happened.
    await register_attribution(
        "payout_requests", rid, str(tx["txId"]),
        await attribution_address(row) or beampay.treasury_address(),
        rid, "payout_attribution", "request_id",
    )
    await tg.alert(
        "payout_send_resolved",
        "Payout send resolved from the chain: the lost response DID land — the transaction's "
        "own amount and our outgoing pipe message both match this crossing, and it was never "
        "re-sent",
        request_id=rid,
    )


# ----------------------------------------------------------------------------- payout: bridging


async def _book_release(row: dict[str, Any]) -> None:
    """Scheduled −= amount + fee + bridge fee · Sent += amount · both fees leave the account
    (§6.5b step 14b) · **Available += whatever the crossing did not spend** (T45). Booked when
    the Beam kernel confirms — that is the moment the bETH is provably burned AND the moment
    `relayer_fee_groth` (what we actually put into the send) is final.

    Guarded on EVERY half. `ledger.release` appends a `release` entry, a `fee` entry and — for an
    order that funded its own crossing (2026-09-10) — a `bridge_fee` entry; a guard that reads
    only the first declares "already booked" forever when a later append failed, and that groth
    sits in Scheduled where nothing can clear or charge it. WHATEVER THE SCHEDULE DEBITED, THE
    RELEASE CLEARS: a row with no `bridge_fee_groth` never debited one, and gets no third entry.

    ⛔ **AND THE REFUND IS ONE OF THE HALVES** (T45). The bridge fee is an ESTIMATE — a live gas
    read times the headroom the wait needs — so "at cost" is only true if the unspent part comes
    back. It is appended here, after the release, because this is the one point where both
    numbers are final and it is the one function every settlement path calls (law 14). It is in
    the early-return guard for the same reason the fee half is: a refund that failed after the
    release landed must be repaired by the next pass, not declared finished — and the caller does
    not stamp `kernel_at` until this function returns, so there IS a next pass.

    ⛔ **NEVER ON A ROW THAT HAS NOT CROSSED.** `bridge_fee_refund_groth` answers 0 unless the
    row carries a positive `relayer_fee_groth`, which only exists once the crossing was funded
    and priced; and `ledger.bridge_fee_refund` refuses a non-positive amount outright."""
    rid = row["_id"]
    # ⛔ A REFILL IS NOT A USER PAYOUT (T34). It is the treasury crossing bETH to OUR OWN
    # distributor address so instant payouts have ETH to spend: no account was debited for it,
    # nobody is owed it, and `row["account_id"]` names no user. Booking a release here would
    # invent a `Sent` balance for an account that never had a `Scheduled` one — money out of
    # nothing, in the ledger that is supposed to be the truth.
    if str(row.get("mode")) == REFILL:
        return
    bridge = int(row.get("bridge_fee_groth") or 0)
    refund = bridge_fee_refund_groth(row)
    if (
        await ledger.find_entry("release", rid)
        and await ledger.find_entry("fee", rid)
        and (not bridge or await ledger.find_entry("bridge_fee", rid))
        and (not refund or await ledger.find_entry("bridge_fee_refund", rid))
    ):
        return
    await ledger.release(
        row["account_id"],
        row.get("asset") or "ETH",
        int(row["amount_groth"]),
        int(row.get("fee_groth") or 0),
        rid,
        f"direct payout bridged, Beam txid {row.get('beam_txid')}",
        bridge_fee_groth=bridge,
    )
    await _refund_unspent_bridge_fee(row)


async def _refund_unspent_bridge_fee(row: dict[str, Any]) -> None:
    """Credit back whatever this crossing did not spend, exactly once, and record BOTH sides of
    the difference on the row (T45).

    The row fields are what the Balance page renders and what an operator adds up:
    `bridge_fee_refund_groth` (the user's money, returned) and `relayer_subsidy_groth` (ours,
    absorbed). Both are written on every settled crossing — a 0 is an answer, and a field that is
    only written when it is interesting is a field nobody can aggregate.

    `AlreadyBridgeFeeRefunded` is SUCCESS: it means another pass (or another writer) already gave
    the money back, which is the only outcome this function is trying to reach."""
    rid = row["_id"]
    refund = bridge_fee_refund_groth(row)
    absorbed = relayer_subsidy_groth(row)
    await _set(
        "payout_requests",
        rid,
        bridge_fee_refund_groth=refund,
        relayer_subsidy_groth=absorbed,
    )
    if not refund:
        return
    funded = _positive_int(row.get("bridge_fee_groth"))
    paid = _positive_int(row.get("relayer_fee_groth"))
    try:
        await ledger.bridge_fee_refund(
            row["account_id"],
            row.get("asset") or "ETH",
            refund,
            rid,
            funded,
            paid,
            f"bridge fee refunded: funded {funded} groth, the crossing paid {paid}",
        )
    except ledger.AlreadyBridgeFeeRefunded:
        return


# ⛔ **`_fail` IS GONE, ON PURPOSE** (T40, admin 2026-09-10 11:2xZ: *"withdrawals on user's
# side cannot be failed"*). It used to move an order to `failed` and refund the debit; a user
# who had asked for their money then saw the word Failed and had to schedule the whole thing
# again, for a cause that was always OURS — a Beam send refused, a gas spike, no free coin.
# Every one of those is `_delay` now (money reserved, a reason, a next attempt), and the two
# states a machine must not resolve — a nonce somebody else consumed, a receipt that mined and
# is not ours — are `_hold_for_a_human`, which is still reserved and still reads as delayed to
# the user. THE REFUND IS THE USER'S ALONE: `routers/withdrawals.cancel`, gated by
# `cancellable()`. Nothing in this module gives money back any more, which is also why nothing
# in it can give it back TWICE.


async def _payout_bridging(row: dict[str, Any]) -> None:
    rid = row["_id"]
    asset = _asset_of(row)
    txid = row.get("beam_txid")
    if not txid:
        await _hold(
            "payout_requests",
            rid,
            "bridging without a Beam txid — this row cannot be advanced automatically",
            key=f"payout-notxid:{rid}",
        )
        return
    w = beam.wallet()
    bp = beampay.beampay()
    # ⛔ A CONTRACT TX HAS ONE READ ROUTE, AND `booked` IS NOT `succeeded`. `/transactions`
    # cannot find these at all (sender and receiver are ""), so the status comes from
    # `/internal/contract_tx/{txid}` — and its `booked` flag is the daemon's idempotency flag,
    # which it also sets for a CANCELLED or FAILED tx ("terminal and never mined → stop
    # retrying it", process_payments.py:176-186). Settlement is `booked` AND status 3.
    tx = await bp.contract_tx(str(txid))
    if tx is None:
        return  # BeamPay's processor has not seen it yet — an answer, and the normal one
    status = int(tx.get("status", -1))
    if status in beam.TX_DEAD:
        # ⛔ NOT A FAILURE OF THE ORDER — a failure of one ATTEMPT. Nothing crossed and nothing
        # was refunded: the money is still the user's and still reserved, and the retry gate
        # (`previous_attempt_dead`) re-reads this transaction before anything is signed again,
        # because a status line without a kernel is the only proof this module accepts.
        await _delay(
            row,
            "bridging",
            f"the Beam transaction is {tx.get('status_string') or status} — nothing crossed, "
            f"and the crossing is re-tried once that transaction is proven dead",
        )
        return
    if status not in beam.TX_SETTLED:
        return  # still in flight; the SLA monitor pages if it stays here
    if not tx.get("booked"):
        # settled on chain, not yet booked by BeamPay's daemon. §BOOKED-IS-LANDED: the ledger
        # release below is irreversible, so it does not run ahead of the system of record.
        # Poll it, never race it (INTEGRATION.md §6b).
        return
    if not row.get("kernel_at"):
        # ⛔ THE DISPROOF IS NOT A WARNING. `find_local_msg` is the one piece of evidence that
        # says "OUR message, to THIS receiver, for THIS amount, exists on this pipe". When it
        # answers None the crossing is unproven, and the ledger release is IRREVERSIBLE: it
        # debits the user and takes the 2%. It used to log a warning and book anyway.
        msg_id = row.get("msg_id")
        if msg_id is None:
            msg_id = await _our_msg(w, row, asset)
        if msg_id is None:
            await _hold(
                "payout_requests",
                rid,
                "the Beam kernel confirmed but no outgoing pipe message with this destination "
                "and amount exists on the pipe — refusing to book the release until the "
                "crossing is identified",
                key=f"payout-nomsg:{rid}",
                cooldown_s=6 * 3600,
            )
            return
        fields: dict[str, Any] = {
            "kernel": tx.get("kernel"),
            # ⚠️ BeamPay's contract_tx does NOT expose the block a contract tx settled in
            # (api.py:989-1027 returns confirmations, never height), and the `confirmations` it
            # does return is FROZEN at booking (see below). So the wallet's own height at the
            # moment we first saw this kernel is written down here, and maturity is counted
            # from it — over-waiting by at most one poll interval, which is the safe direction.
            "beam_kernel_seen_at": time.time(),
            "beam_height_at_kernel": int(await bp.height()),
            "msg_id": int(msg_id),
        }
        try:
            await _set("payout_requests", rid, **fields)
        except DuplicateKeyError:
            # another request already carries this pipe message: it is not this crossing's
            # evidence, and the ledger release below is irreversible
            await _hold(
                "payout_requests",
                rid,
                f"pipe message {msg_id} is already booked to another payout request — refusing "
                f"to book this release on it",
                key=f"payout-msgtaken:{rid}",
                cooldown_s=6 * 3600,
            )
            return
        # ⛔ TWO WRITERS OF ONE FIELD, avoided ahead of time: `beam_fee_groth` is the row's
        # "last BEAM fee" and the funding transfer writes it first, so the number
        # `crossing_fee_debt_groth` sums gets a field of its own that only this line writes.
        await fee_charged(
            "payout_requests", rid, tx, f"payout {rid}", "request_id", field="crossing_fee_groth"
        )
        # ⛔ **TWO CROSSINGS, ONE ORDER — THE ALARM THE RETRY LADDER EARNS** (T40). `_delay`
        # lets an order try again, and `previous_attempt_dead` is what stops a second send while
        # the first could still settle. If both ever DO settle, the ledger's unique release
        # index makes sure the account is only debited once — and this row-local check is what
        # notices, because a second `_book_release` would otherwise short-circuit on the first
        # one's entry and march the row on to `delivering` as though nothing had happened. The
        # money has left the treasury twice at that point; nothing here may resolve it.
        booked_txid = str(row.get("release_booked_txid") or "")
        if booked_txid and booked_txid != str(txid):
            await _hold_for_a_human(
                "payout_requests",
                rid,
                "status",
                "bridging",
                f"this order's ledger release was already booked against Beam transaction "
                f"{booked_txid} and this pass is settling {txid} — TWO crossings for one order, "
                f"which means the treasury has paid it twice. Nothing is advanced or booked",
                "payout_double_release",
                "request_id",
            )
            return
        # ⛔ BOOK FIRST, MARK SECOND. `kernel_at` used to be written before the ledger entry, so
        # one transient Mongo error left the marker set and the release unbooked forever: the
        # user's money parked in Scheduled, `sent` never showing it, cancel refusing, and a
        # later _fail path would have REFUNDED a payout whose bETH was burned.
        try:
            await _book_release({**row, **fields})
        except ledger.AlreadyReleased:
            # the DATABASE refused a second `release` entry for this ref — two writers reached
            # it at once, which on this path means two crossings settling for one order
            await _hold_for_a_human(
                "payout_requests",
                rid,
                "status",
                "bridging",
                f"the ledger already carries a release for this order and refused a second — "
                f"this crossing ({txid}) would pay it twice, so nothing was booked",
                "payout_double_release",
                "request_id",
            )
            return
        fields["release_booked_txid"] = str(txid)
        fields["kernel_at"] = time.time()
        await _set(
            "payout_requests",
            rid,
            kernel_at=fields["kernel_at"],
            release_booked_txid=str(txid),
        )
        row = {**row, **fields}
    # ⛔ **BEAMPAY STOPS MAINTAINING `confirmations` THE MOMENT IT BOOKS THE TX**, which is the
    # very precondition this handler waits for. `handle_contract_transaction` opens with
    # `if existing and existing.get("success") and existing.get("status") == status: return`
    # BEFORE the `$set` that writes `confirmations`, and `reconcile_unfinished_transactions`
    # only visits rows with `success != True` — so the stored count is whatever the wallet
    # reported on the first status-3 sighting, and the processor polls every 3 s ("Completed
    # txs can still report confirmations 0", process_payments.py:958). On the live wallet 159
    # of 171 contract txs are frozen at 0. Gating on that number meant every payout stopped
    # dead in `bridging` with the bETH burned and the ledger release already taken.
    #
    # So a frozen 0 is treated as NO COUNT AVAILABLE — never as a real zero — and maturity is
    # counted from the wallet height we recorded at kernel time against BeamPay's
    # `/wallet_status.current_height`, which does move.
    reported = tx.get("confirmations")
    confs = int(reported) if reported is not None else 0
    if confs <= 0:
        height = int(await bp.height())
        h0 = int(row.get("beam_height_at_kernel") or 0)
        if not h0:
            # a row booked before this was recorded: start the clock now and over-wait
            h0 = height
            await _checkpoint("payout_requests", rid, beam_height_at_kernel=h0)
            row = {**row, "beam_height_at_kernel": h0}
        if height <= 0:
            # ⛔ AN UNREADABLE COUNT IS NOT A COUNT — and neither is an unreadable height.
            await _hold(
                "payout_requests",
                rid,
                "BeamPay reported no confirmation count for this crossing and no wallet height "
                "to derive one from — refusing to guess",
                key=f"payout-noconfs:{rid}",
                cooldown_s=6 * 3600,
            )
            return
        confs = max(height - h0, 0)
    if confs < settings.beam_confirmations:
        if int(row.get("beam_confirmations") or -1) != confs:
            await _checkpoint("payout_requests", rid, beam_confirmations=confs)
        return
    await _advance(
        "payout_requests",
        rid,
        "status",
        "bridging",
        "delivering",
        "payout_delivering",
        f"Payout delivering: {confs} Beam confirmations — waiting for the relayer to pay "
        f"{tg.fmt_groth(int(row['amount_groth']))} {asset.key} on Ethereum",
        "request_id",
        beam_confirmations=confs,
    )


# ----------------------------------------------------------------------------- payout: delivering


async def archive_endpoint(rpc: Any, probe_addr: str, block: int) -> str:
    """Which endpoint can actually serve HISTORICAL state — probed ENDPOINT-EXPLICITLY, then
    pinned. `rpc.call` rotates, and a rotating historical read succeeded about half the time,
    which is worse than failing: it made a half-blind sample look like a clean measurement and
    reported "the ETH never arrived" about a delivery that was sitting in the block
    (`rebalancer.py:_b2e_eth_delivery._bal`). RAISES when none can — "no archive" must never
    read as "no delivery"."""
    urls = ([_ARCHIVE["url"]] if _ARCHIVE["url"] else []) + [
        u for u in rpc.urls if u != _ARCHIVE["url"]
    ]
    for url in urls:
        try:
            v = await rpc.call_on(url, "eth_getBalance", [probe_addr, hex(block)])
        except (ethpipe.RpcError, ValueError, TypeError):
            continue
        if isinstance(v, str) and v.startswith("0x"):
            _ARCHIVE["url"] = url
            return url
    _ARCHIVE["url"] = None
    raise ethpipe.RpcError(
        f"no configured RPC served historical state for block {block} — this is 'we cannot see', "
        f"never 'nothing arrived'"
    )


async def find_delivery(
    rpc: Any,
    pipe: str,
    w_addr: str,
    amount_wei: int,
    from_block: int,
    head: int,
    prev_wei: int | None = None,
    step: int = DELIVERY_STEP,
    max_steps: int = DELIVERY_MAX_STEPS,
) -> tuple[dict[str, Any] | None, int, int]:
    """(delivery | None, block scanned to, W's balance there).

    ⚠️ IDENTITY, NOT A WALLET DELTA. A native-ETH delivery is an internal call and emits no log,
    so the only honest evidence is the PAIR: the block in which W's balance ROSE by exactly the
    amount and the PIPE's FELL. Neither half alone is proof — a rise is somebody paying W, a
    drop is somebody else's delivery.

    ⛔ THE WALK IS DRIVEN BY W, NOT BY THE PIPE. The old entry condition was "the pipe's balance
    at `hi` is below its balance at `lo`", and `EthPipe.sendFunds` is payable: the pipe's ETH
    balance IS the locked ETH, so every Pgas.me deposit RAISES it and the spec says Pgas.me will
    be the overwhelming majority of the pipe's traffic. One 0.02 ETH deposit ten blocks after a
    0.005 ETH delivery makes the window's net change positive, the bisect never runs, and the
    caller then checkpoints PAST the delivery block forever. That is not "we cannot see" — it is
    a complete read that is structurally blind, and it is silent.

    W is a fresh, single-purpose destination, so ANY change in its balance is a candidate worth
    bisecting; the pipe is then read only to CONFIRM. `prev_wei` is W's balance at `from_block`.

    Sampled coarsely then bisected, and bounded per pass, so an 18-hour tail costs a few dozen
    reads a minute rather than one per block. Every read is PINNED to the archive endpoint; an
    unreadable read RAISES and the caller must not checkpoint past it."""
    url = await archive_endpoint(rpc, w_addr, from_block)

    async def bal(addr: str, blk: int) -> int:
        v = await rpc.call("eth_getBalance", [addr, hex(blk)], prefer=url, pin=True)
        if not isinstance(v, str) or not v.startswith("0x"):
            raise ethpipe.RpcError(f"eth_getBalance({addr[:10]}…, {blk}) answered {str(v)[:60]}")
        return int(v, 16)

    lo = int(from_block)
    prev = int(prev_wei) if prev_wei is not None else await bal(w_addr, lo)
    for _ in range(max_steps):
        if lo >= head:
            break
        hi = min(lo + step, head)
        cur = await bal(w_addr, hi)
        if cur == prev:
            prev, lo = cur, hi
            continue
        a, b = lo, hi
        while b - a > 1:  # the first block in (lo, hi] where W's balance is no longer `prev`
            mid = (a + b) // 2
            if await bal(w_addr, mid) != prev:
                b = mid
            else:
                a = mid
        got = await bal(w_addr, b) - prev  # prev == bal(W, b-1) by construction
        if got >= amount_wei:
            # the other half of the pair. The pipe's raw net move is masked by OUR OWN traffic —
            # `EthPipe.sendFunds` is payable, so every Pgas.me deposit RAISES the pipe's balance
            # in the same block — so the deposits of that block are added BACK before the drop
            # is judged. That is the corroboration: value the pipe PAID OUT, measured, and not
            # "somebody touched the pipe".
            drop = await bal(pipe, b - 1) - await bal(pipe, b)
            inflow, pipe_tx = await pipe_flow(rpc, b, pipe)
            adjusted = drop + int(inflow or 0)
            if adjusted >= got:
                return (
                    {
                        "block": b,
                        "amount_wei": got,
                        "pipe_drop_wei": drop,
                        "pipe_inflow_wei": inflow,
                        "pipe_paid_wei": adjusted,
                        "pipe_tx": pipe_tx,
                        "proof": "pair",
                        "batch": got != amount_wei,
                        "w_wei_before": prev,
                    },
                    b,
                    await bal(w_addr, b),
                )
            if adjusted >= amount_wei:
                # ⛔ "WE CANNOT ATTRIBUTE", NOT "NOT OURS". The pipe really did pay out at least
                # our amount in this block and W really did rise, but by MORE than the pipe can
                # account for — so the block's change cannot be explained and the caller must
                # NOT checkpoint past it. `got == amount_wei` was the old test, and the relayer
                # batches: two 0.005 deliveries to one W in one block make W rise by 0.01, both
                # requests miss it, and both are checkpointed past their own money.
                return (
                    {
                        "block": b,
                        "amount_wei": got,
                        "pipe_drop_wei": drop,
                        "pipe_inflow_wei": inflow,
                        "pipe_paid_wei": adjusted,
                        "pipe_tx": pipe_tx,
                        "proof": "unattributable",
                        "batch": True,
                        "w_wei_before": prev,
                    },
                    b,
                    await bal(w_addr, b),
                )
        prev, lo = await bal(w_addr, b), b  # not ours — carry on from that block
    return None, lo, prev


async def pipe_flow(rpc: Any, block: int, pipe: str) -> tuple[int | None, str | None]:
    """(Σ wei sent INTO the pipe in this block, one tx hash touching the pipe) — or (None, None).

    ⛔ **A TRANSACTION TO THE PIPE IS NOT THE RELAYER'S DELIVERY.** This used to answer "the
    first tx in the block whose `to` is the pipe" and that answer was accepted as PROOF of a
    delivery whenever the pipe's balance had not visibly fallen. `EthPipe.sendFunds` is exactly
    such a transaction, every Pgas.me deposit is one, and the spec says Pgas.me will be the
    overwhelming majority of the pipe's traffic — so the proof degraded to "W rose by the amount
    AND somebody deposited". An unrelated inflow of the right size then marked an undelivered
    payout `sent`, with the user already debited and the SLA silenced forever.

    So the block is read for the NUMBER instead: the deposits it carries into the pipe, which
    are exactly what masks the pipe's outgoing move, added back by the caller. Unreadable
    answers (None) cost nothing — the caller simply cannot close the pair this pass."""
    try:
        blk = await rpc.call(
            "eth_getBlockByNumber", [hex(block), True], prefer=_ARCHIVE["url"], pin=True
        )
    except (ethpipe.RpcError, AttributeError, TypeError, ValueError):
        return None, None
    txs = (blk or {}).get("transactions") or []
    inflow, first = 0, None
    for t in txs:
        if str(t.get("to") or "").lower() != pipe.lower():
            continue
        if first is None:
            first = str(t.get("hash"))
        v = t.get("value")
        try:
            inflow += int(v, 16) if isinstance(v, str) else int(v or 0)
        except (TypeError, ValueError):
            return None, first  # an unparseable value is not a zero
    return inflow, first


async def _delivering_to(asset: Asset, w_addr: str, block: int) -> list[dict[str, Any]]:
    """Every payout of this asset to this wallet that this block's rise could be explaining:
    the ones still `delivering`, plus the ones ALREADY settled ON THIS BLOCK.

    The second half matters: the relayer batches, and once the first of a batched pair has
    reached `sent` the block's rise is no longer explained by the rows still waiting. Without
    it the second order of every batch would hold forever on money that really did arrive."""
    return await db().payout_requests.find(
        {
            "$and": [
                {"W": w_addr},
                _asset_match(asset),
                {
                    "$or": [
                        {"status": "delivering"},
                        {"status": "sent", "eth_block": int(block)},
                    ]
                },
            ]
        },
        {"amount_groth": 1, "status": 1},
    ).to_list(None)


async def _claim_delivery(
    asset: Asset, block: int, w_addr: str, amount_wei: int, rid: str, slots: int = 1
) -> bool:
    """Consume this (pipe, block, W, amount) for THIS request, exactly once.

    Two payouts of one denomination to one wallet are the product's normal shape (0.01/0.1 ETH,
    up to 50 items per withdrawal, "top up fresh wallets"), they share a baseline block, and
    `got == amount_wei` tied the pair to no particular crossing — so ONE on-chain delivery
    closed BOTH orders and the second loss was silent. The database decides, not a read.

    `slots` is how many crossings of exactly this size the block's W-rise has been PROVEN to
    contain (see `_payout_delivering`): a batch of two identical deliveries really is two
    deliveries, so it may consume two — and never more than the arithmetic explains."""
    base = f"{asset.pipe.lower()}:{int(block)}:{w_addr.lower()}:{int(amount_wei)}"
    for n in range(1, max(int(slots), 1) + 1):
        key = base if n == 1 else f"{base}#{n}"
        try:
            await db().deliveries.insert_one(
                {
                    "_id": key,
                    "request_id": rid,
                    "asset": asset.key,
                    "block": int(block),
                    "amount_wei": int(amount_wei),
                    "at": time.time(),
                }
            )
            return True
        except DuplicateKeyError:
            owner = await db().deliveries.find_one({"_id": key})
            if str((owner or {}).get("request_id") or "") == rid:
                return True
    return False


async def _payout_delivering(row: dict[str, Any]) -> None:
    rid = row["_id"]
    asset = _asset_of(row)
    frm = int(row.get("eth_scan_from") or row.get("eth_from_block") or 0)
    if not frm:
        await _hold(
            "payout_requests",
            rid,
            "no Ethereum baseline block was recorded for this crossing, so a delivery can be "
            "neither proven nor disproven automatically",
            key=f"payout-nobase:{rid}",
        )
        return
    rpc = get_rpc()
    head = int(await rpc.block_number())
    amount_wei = int(row["amount_groth"]) * asset.grid
    found, scanned_to, w_wei = await find_delivery(
        rpc,
        asset.pipe,
        str(row["W"]),
        amount_wei,
        frm,
        head,
        row.get("w_wei_at_scan_from"),
    )
    if not found:
        # a CHECKPOINT, not progress: `_checkpoint` deliberately does not stamp `updated_at`
        await _checkpoint(
            "payout_requests", rid, eth_scan_from=scanned_to, w_wei_at_scan_from=w_wei
        )
        return
    block = int(found["block"])
    got = int(found.get("amount_wei") or 0)

    async def rewind() -> None:
        """⛔ NEVER CHECKPOINT PAST A BLOCK WE COULD NOT EXPLAIN. The scan is one-way: once
        `eth_scan_from` is past the delivery block, the balance change that proves this crossing
        can never be seen again and the row waits out its SLA on money that arrived."""
        await _checkpoint(
            "payout_requests",
            rid,
            eth_scan_from=max(block - 1, 0),
            w_wei_at_scan_from=int(found.get("w_wei_before") or 0),
        )

    if found.get("proof") == "unattributable":
        await rewind()
        await _hold(
            "payout_requests",
            rid,
            f"in Ethereum block {block} this wallet rose by {got} wei — more than this payout's "
            f"{amount_wei} — and the pipe paid out {found.get('pipe_paid_wei')} wei, which does "
            f"not account for the rise. That is 'we cannot attribute', not 'not ours', so the "
            f"scan is NOT advanced past it",
            key=f"payout-unattributable:{rid}",
            cooldown_s=6 * 3600,
        )
        return
    slots = 1
    if found.get("batch"):
        # the relayer paid several of our messages in one block. It is ours only if the payouts
        # to this wallet ACCOUNT for the whole rise — otherwise part of it is somebody else's.
        siblings = await _delivering_to(asset, str(row["W"]), block)
        total = sum(int(r.get("amount_groth") or 0) for r in siblings) * asset.grid
        if total != got or not any(r["_id"] == rid for r in siblings):
            await rewind()
            await _hold(
                "payout_requests",
                rid,
                f"Ethereum block {block} raised this wallet by {got} wei and the payouts of "
                f"this asset to it account for {total} wei — the block cannot be attributed, so "
                f"nothing is settled and the scan is NOT advanced past it",
                key=f"payout-batchunclear:{rid}",
                cooldown_s=6 * 3600,
            )
            return
        slots = sum(
            1
            for r in siblings
            if int(r.get("amount_groth") or 0) == int(row["amount_groth"])
        )
    await _checkpoint(
        "payout_requests", rid, eth_scan_from=scanned_to, w_wei_at_scan_from=w_wei
    )
    if not await _claim_delivery(asset, block, str(row["W"]), amount_wei, rid, slots):
        await _hold(
            "payout_requests",
            rid,
            f"the delivery in Ethereum block {block} is already booked to another payout "
            f"request — this crossing is NOT proven and the scan continues past it",
            key=f"payout-dupdelivery:{rid}",
            cooldown_s=6 * 3600,
        )
        return
    eth_tx = found.get("pipe_tx")
    await _advance(
        "payout_requests",
        rid,
        "status",
        "delivering",
        "sent",
        "payout_sent",
        f"Payout SENT: {asset.key} {tg.fmt_groth(int(row['amount_groth']))} delivered on "
        f"Ethereum in block {block}",
        "request_id",
        eth_tx=eth_tx,
        eth_block=block,
        eth_proof=found.get("proof"),
        sent_at=time.time(),
    )


# ----------------------------------------------------------------------------- treasury: claim


# The keys a BeamPay contract-tx record could state the settling block under. ⚠️ **IT STATES
# NONE OF THEM TODAY** — `GET /internal/contract_tx` answers confirmations and never a height
# (api.py:989-1027), which is exactly why `_payout_bridging` writes down the WALLET's height at
# kernel time instead. They are asked first anyway, in ONE place, so that the day BeamPay does
# expose the kernel's own block the exact number wins over the approximation with no other edit
# — and so that "does the record carry one?" is never answered twice, differently (law 9).
_TX_HEIGHT_FIELDS = ("height", "kernel_height", "block_height")


def tx_height(tx: dict[str, Any]) -> int | None:
    """The Beam block a settled contract transaction states it landed in, or None.

    ⛔ NONE, NEVER ZERO — and never a bool, a NaN, an infinity or a string that is not a number.
    0 is a block, and a height nobody recorded, published as one, is a bridge-explorer link
    pointing at somebody else's bridge traffic and reading to the user as evidence about their
    own money. `routers/account.beam_height` refuses the same shapes on the way OUT; this
    refuses them on the way in, so the row never carries one to begin with."""
    for field in _TX_HEIGHT_FIELDS:
        v = tx.get(field)
        if v is None or isinstance(v, bool):
            continue
        try:
            h = int(v)
        except (TypeError, ValueError, OverflowError):
            continue  # a NaN, an infinity, "later" — none of them is a block
        if h > 0:
            return h
    return None


async def claim_height(bp: beampay.BeamPay, tx: dict[str, Any], dep_id: str) -> int | None:
    """The Beam block to publish for a claim that has just settled, or None.

    The record's own height if it states one, else the WALLET's height at the moment the kernel
    was first seen — the same approximation `_payout_bridging` records for a crossing, late by
    at most one poll interval and in the safe direction.

    ⛔ **AND A TRACKING LINK MAY NEVER HOLD THE MONEY.** There the height is load-bearing —
    maturity is counted from it — so an unreadable one must stop that pass. Here it decorates a
    deposit that has already been claimed, booked and attributed, and the only thing it buys the
    user is a link they can watch. So an unreadable height is written to the log and the claim
    advances without one: the client draws no link, which `routers/account.py` already calls the
    honest state and not a bug."""
    stated = tx_height(tx)
    if stated is not None:
        return stated
    try:
        h = int(await bp.height())
    except Exception as e:  # noqa: BLE001 — an explorer link must never hold a settled claim
        log.warning(
            "deposit %s claimed: the wallet height for the explorer link is unreadable "
            "(%s: %s) — advancing without it",
            dep_id, type(e).__name__, beam.redact(e),
        )
        return None
    return h if h > 0 else None


async def _treasury_new(dep: dict[str, Any]) -> None:
    msg_id = (dep.get("eth") or {}).get("msg_id")
    if msg_id is None:
        await _hold(
            "deposits",
            dep["_id"],
            "credited without a pipe message id — there is nothing to claim on Beam",
            key=f"treasury-nomsg:{dep['_id']}",
        )
        return
    await _advance(
        "deposits",
        dep["_id"],
        "treasury",
        None,
        "claiming",
        "deposit_claiming",
        f"Treasury: claiming pipe message {msg_id} for {dep.get('asset')} "
        f"{tg.fmt_groth(int(dep.get('value_groth') or 0))}",
        "deposit_id",
    )


async def _resolve_lost_claim(dep: dict[str, Any], asset: Asset, msg_id: int, since: float) -> None:
    """A claim whose response was lost, resolved the only honest way round.

    ⛔ `view_incoming` FIRST. The message's own claimability is evidence a wallet response
    cannot give: if it is gone then our claim consumed it and the transaction can be matched by
    its own signed amount. Asking `find_contract_tx` first meant a deposit adopted an unrelated
    payout's send, marched claiming → claimed → shielding while its own bETH sat unclaimed on
    the pipe forever — and the user had already been credited at the lock.

    ⛔ **AND NOTHING HERE RE-SIGNS.** "msgId is still listed, therefore nothing landed" is
    FALSE: `view_incoming` reflects MINED state, and a claim that was broadcast but is not yet
    mined (~15 Beam blocks; the wallet has a whole TX_REGISTERING state for exactly this
    window, and a lagging node is the very condition that loses the HTTP answer in the first
    place) leaves its message listed. This used to `$unset` `claim_call_at` past the deadline —
    the one marker that stops `_treasury_claiming` building the call again — so the next pass
    submitted a SECOND `process_invoke_data` for one msgId: two signatures over one inventory,
    the law this module exists to enforce. Claim #1 then consumes the message and books to
    `__house__` (its txid was never captured, so it was never registered), claim #2 reverts,
    the re-plan finds the message gone, and the row holds forever on "the relayer has not
    delivered pipe message N", which is false. The marker is never cleared now: inside the
    window this waits, past it a HUMAN decides and can re-arm the claim after reading the chain.

    ⛔ **AN INFLOW IS NEGATIVE.** A claim moves value INTO the wallet, and BeamPay records a
    wallet inflow as a NEGATIVE invoke amount ("A POSITIVE invoke amount is a wallet OUTFLOW",
    process_payments.py:216; a live e2b claim reads `{"amount": -75058, "asset_id": 38}`).
    Asking for +value searched for the shape of a b2e SEND on this pipe — a real payout's
    crossing — so a lost claim could never find its own transaction and could adopt a payout's
    instead."""
    dep_id = dep["_id"]
    w = beam.wallet()
    bp = beampay.beampay()
    # ⛔ ASK ABOUT THE INDEXES, OR THE ANSWER IS A LIE OF OMISSION. A message delivered to an
    # indexed receiver key is absent from a `view_incoming` that was not told about that index,
    # and "absent" is what this function reads as "our claim consumed it" — the one conclusion
    # that lets a lost response be resolved by adopting a transaction. `open_indexes` is empty
    # unless a per-deposit key was actually issued, so this is the same call it always was.
    incoming = await w.view_incoming(  # unreadable RAISES: never "not delivered"
        asset.beam_cid, await receiver_keys.open_indexes(asset)
    )
    still_claimable = any(m["msg_id"] == msg_id for m in incoming)
    value = int(dep.get("value_groth") or 0)
    if value <= 0:
        await _hold(
            "deposits",
            dep_id,
            "the claim response was lost and this deposit records no value, so no transaction "
            "can be matched to it by identity",
            key=f"treasury-novalue:{dep_id}",
            cooldown_s=6 * 3600,
        )
        return
    # the identity of OUR claim: an inflow of exactly this message's value on this pipe
    tx = await bp.find_contract_tx(
        asset.beam_cid, asset.aid, since, -value,
        is_taken=taken_txid_check(exclude_deposit=dep_id),
    )
    if still_claimable:
        # ⛔ a matching transaction while OUR message is still unclaimed is NOT proof our claim
        # landed — ours would have consumed the message — so nothing is adopted here. It is
        # still worth naming: an in-flight one is very likely the call whose answer we lost.
        if time.time() - since <= UNRESOLVED_S:
            return  # inside the window: wait. Never call again.
        in_flight = tx is not None and int((tx or {}).get("status", -1)) in beam.TX_IN_FLIGHT
        await _hold_for_a_human(
            "deposits",
            dep_id,
            "treasury",
            "claiming",
            f"the claim response was lost {UNRESOLVED_S // 60} min ago and pipe message "
            f"{msg_id} is still listed as claimable — which proves nothing was MINED, not that "
            f"nothing was signed, so this is NOT auto-retried"
            + (
                f" (a contract transaction of exactly this value is IN FLIGHT: "
                f"{tx.get('txId')})" if in_flight and tx else ""
            )
            + " — a human must read the chain and re-arm the claim",
            "deposit_claim_unresolved",
            "deposit_id",
        )
        return
    if tx and tx.get("txId"):
        try:
            await db().deposits.update_one(
                {"_id": dep_id, "claim_txid": {"$exists": False}},
                {
                    "$set": {
                        "claim_txid": str(tx["txId"]),
                        "resolved_from_chain": True,
                        "updated_at": time.time(),
                    }
                },
            )
        except DuplicateKeyError:
            await _hold(
                "deposits",
                dep_id,
                "the only matching contract transaction is already booked to another row — "
                "refusing to adopt it",
                key=f"treasury-txtaken:{dep_id}",
                cooldown_s=6 * 3600,
            )
            return
        # a resolved claim is still a contract txid of ours, and the value it brought in belongs
        # to the treasury address: register it, or the whole inflow books to `__house__`
        await register_attribution(
            "deposits", dep_id, str(tx["txId"]), beampay.treasury_address(), dep_id,
            "deposit_attribution", "deposit_id",
        )
        return
    if time.time() - since > UNRESOLVED_S:
        await _hold_for_a_human(
            "deposits",
            dep_id,
            "treasury",
            "claiming",
            f"the claim response was lost, message {msg_id} is no longer claimable and no "
            f"transaction of exactly its value turned up in {UNRESOLVED_S // 60} min — a human "
            f"must reconcile this",
            "deposit_claim_unresolved",
            "deposit_id",
        )


async def _treasury_claiming(dep: dict[str, Any]) -> None:
    dep_id = dep["_id"]
    asset = _asset_of(dep)
    msg_id = int((dep.get("eth") or {}).get("msg_id"))
    w = beam.wallet()
    bp = beampay.beampay()
    claim_txid = dep.get("claim_txid")
    if claim_txid:
        # ⛔ THE CLAIM'S EVIDENCE IS BEAMPAY'S, NOT `tx_status`. A claim is a contract
        # invocation, so its only read route is `/internal/contract_tx/{txid}` — and `booked`
        # there is the daemon's idempotency flag, which is set for a CANCELLED or FAILED tx
        # too. "Claimed" is `booked` AND status 3: booked alone would march a reverted claim
        # straight into shielding with the bETH still sitting unclaimed on the pipe.
        tx = await bp.contract_tx(str(claim_txid))
        if tx is None:
            return  # BeamPay's processor has not seen it yet — an answer, and the normal one
        status = int(tx.get("status", -1))
        if status in beam.TX_DEAD:
            # nothing was consumed — the message is claimable again, and view_incoming (below)
            # is the guard that decides, so the markers are cleared and the next pass re-plans
            await db().deposits.update_one(
                {"_id": dep_id},
                {"$unset": {"claim_txid": "", "claim_call_at": ""}},
            )
            await tg.alert(
                "deposit_claim_failed",
                f"Treasury claim FAILED on Beam ({tx.get('status_string') or status}); the pipe "
                f"message is still unclaimed and will be re-planned",
                deposit_id=dep_id,
            )
            return
        if status in beam.TX_SETTLED and tx.get("booked"):
            # …and the row does not advance until the txid is BeamPay's to book. An
            # unregistered claim credits `__house__`, so the treasury balance every shield and
            # every payout gate reads would never have moved.
            if not attributed(dep, claim_txid):
                if not await register_attribution(
                    "deposits", dep_id, str(claim_txid), beampay.treasury_address(), dep_id,
                    "deposit_attribution", "deposit_id",
                ):
                    await _hold(
                        "deposits",
                        dep_id,
                        "the claim settled but BeamPay has not accepted its attribution — the "
                        "claim is NEVER re-sent; the registration is what retries",
                        key=f"treasury-attr:{dep_id}",
                    )
                    return
            await fee_charged(
                "deposits", dep_id, tx, f"claim {dep_id}", "deposit_id", field="claim_fee_groth"
            )
            # the Beam block the user can watch this crossing in (T31b item 8). ONE writer for
            # `claim_height`, and it is written on the transition that proves the kernel — never
            # on a row whose claim has not settled. Absent when it could not be read.
            height = await claim_height(bp, tx, dep_id)
            await _advance(
                "deposits",
                dep_id,
                "treasury",
                "claiming",
                "claimed",
                "deposit_claimed",
                f"Treasury: claimed pipe message {msg_id} (kernel confirmed, booked by BeamPay)",
                "deposit_id",
                claim_kernel=tx.get("kernel"),
                claimed_at=time.time(),
                **({"claim_height": height} if height else {}),
            )
        return
    since = float(dep.get("claim_call_at") or 0)
    if since:
        await _resolve_lost_claim(dep, asset, msg_id, since)
        return
    if not settings.claim_enabled:
        log.info("would claim msg %s on %s for deposit %s", msg_id, asset.beam_symbol, dep_id)
        await _hold(
            "deposits",
            dep_id,
            f"PGAS_CLAIM_ENABLED=0 — treasury work is waiting: pipe message {msg_id} is unclaimed",
            key=f"treasury-dark-claim:{dep_id}",
            dark=True,
        )
        return
    if workers.paused():
        return
    # the exact set of receiver keys a delivery to us could be on — legacy included. An index we
    # do not ask about is INVISIBLE, and an invisible message reads as "the relayer has not
    # delivered yet", which holds the deposit for ever on money already burned on Ethereum.
    mine = [
        m
        for m in await w.view_incoming(asset.beam_cid, await receiver_keys.open_indexes(asset))
        if m["msg_id"] == msg_id
    ]
    if not mine:
        await _hold(
            "deposits",
            dep_id,
            f"the relayer has not delivered pipe message {msg_id} to Beam yet",
            key=f"treasury-wait:{dep_id}",
        )
        return
    # the claim's txid has to be registrable BEFORE the claim is signed: an unregistered claim
    # books the whole inflow to `__house__`, so the treasury balance the shield and the §9.3
    # unshielded-value gate both read would never move, and the repair route can be refused
    # after the money has already been claimed.
    if not await attribution_ready(bp, "deposits", dep_id):
        return
    if not await _beam_fees_ok(bp, "claim", dep_id, "deposits"):
        return
    try:
        # THE ROW'S OWN INDEX, never the process's idea of the current one: the signing blob has
        # to be the key this message was delivered to. A legacy row carries none and its call is
        # byte-identical to every claim made before per-deposit keys existed.
        raw, args = await w.build_receive(
            asset.beam_cid, msg_id, receiver_keys.claim_index(dep)
        )
    except beam.BeamError as e:
        # a create_tx:false read: nothing was signed, so this is a wait and not an unknown
        await _hold(
            "deposits",
            dep_id,
            f"the claim call could not be built or verified: {beam.redact(e)[:200]}",
            key=f"treasury-build:{dep_id}",
        )
        return
    log.info("deposit %s claim calldata verified: %s", dep_id, beam.redact(args))
    # ⛔ ONE CONDITIONAL UPDATE BEFORE THE IRREVERSIBLE CALL, exactly as `_advance` does for a
    # payout. `_payout_scheduled` got this right and the treasury did not: two concurrent
    # passes produced TWO `process_invoke_data` calls for one msgId — literally two signatures
    # over one inventory, the law this module exists to enforce. The second reverts on chain,
    # the claim is unset, and the re-plan then finds the message gone and holds forever on a
    # deposit that WAS claimed.
    claimed = await db().deposits.find_one_and_update(
        {
            "_id": dep_id,
            "treasury": "claiming",
            "claim_call_at": {"$exists": False},
            "claim_txid": {"$exists": False},
        },
        {"$set": {"claim_call_at": time.time(), "updated_at": time.time()}},
    )
    if claimed is None:
        return  # another pass owns this claim
    try:
        txid = await w.submit(raw, f"claim {dep_id}")
    except beam.Halted:
        await db().deposits.update_one({"_id": dep_id}, {"$unset": {"claim_call_at": ""}})
        return
    except beam.BeamError as e:
        await tg.alert(
            "deposit_claim_unconfirmed",
            f"Treasury claim UNCONFIRMED: the wallet did not answer ({beam.redact(e)[:160]}). NOT "
            f"re-sending — the next pass resolves it from the chain",
            deposit_id=dep_id,
        )
        return
    # EVIDENCE FIRST, then the registration — in the same processor step, before the row can
    # advance. A lost registration is retried above; a lost txid could only be recovered by a
    # second signature, which is the one thing this module never does.
    await _set("deposits", dep_id, claim_txid=txid)
    await register_attribution(
        "deposits", dep_id, txid, beampay.treasury_address(), dep_id,
        "deposit_attribution", "deposit_id",
    )


# ----------------------------------------------------------------------------- treasury: shield


def shield_plan(value_groth: int, denoms: list[int] | None = None) -> list[int]:
    """Denomination chunks, largest first, remainder last (§6.4 step 10). Every chunk becomes
    one shielded output; equal-sized outputs are what makes the pool a crowd.

    `denoms` are per ASSET (`settings.shield_denoms_for`): 0.01/0.1 ETH is a crowd, and the
    same integers read as 0.01/0.1 DAI — 10,000 sends and 110 BEAM of fees for one ordinary
    1000-DAI deposit, over 83 hours, well past the §9.3 outputs-per-block rule."""
    out: list[int] = []
    left = int(value_groth)
    for d in denoms if denoms is not None else settings.shield_denoms:
        if d <= 0:
            continue
        n, left = divmod(left, d)
        out.extend([d] * n)
    if left > 0:
        out.append(left)
    return out


async def _prove_mp_address(bp: beampay.BeamPay, addr: str) -> None:
    """RAISES unless `addr` is a max-privacy address of THIS deployment. Never a value.

    Every value-moving call in this module proves its destination. A BeamPay `/withdraw` has no
    calldata to inspect, and `PGAS_BEAM_MP_ADDRESS` used to win verbatim with no check at all:
    one typo sends every chunk of every claimed deposit to a stranger, one "Treasury:
    shielding …" success at a time, until somebody reads the balance. A plain `regular` address
    is a perfectly valid `/withdraw` target too, so the shield would "succeed" while shielding
    nothing and every payout would then starve on a float that can never fill.

    ⚠️ BeamPay's `/validate_address` answers ONLY `is_valid` — the wallet's `is_mine` and `type`
    are not exposed by that route (api.py:258-263). So the proof is assembled from what BeamPay
    CAN answer, and each part is load-bearing:

      * `validate_address` — the wallet's own verdict on the token. Catches a typo.
      * `is_registered` — the address is in BeamPay's OWN address book, i.e. it was created by
        `/create_wallet` on this deployment's wallet. This is the `is_mine` substitute, and for
        our purposes the stronger claim: an address BeamPay does not know is one whose balance
        `/balances` cannot report, so the float would be permanently invisible even if the
        value did arrive.
      * the SHAPE — a 64–72-hex SBBS token is a REGULAR address. Shielding to one is a send
        that settles and shields nothing. ⚠️ This one also catches a BeamPay that ignored
        `wallet_type` on `/create_wallet`, which is the only way a FRESH address can be wrong.

    Applied to the configured primary AND to every freshly created per-chunk target: an address
    this deployment made is not exempt, because "we asked for max_privacy" is not the same fact
    as "the wallet made one" (§WE-SET-IT-WE-DONT-READ-IT)."""
    if not await bp.validate_address(addr):
        raise beampay.BeamPayError(
            "the shield target is not a valid Beam address — refusing to send treasury value "
            "to it"
        )
    if not await bp.is_registered(addr):
        raise beampay.BeamPayError(
            "the shield target is not in BeamPay's address book, so it was not created by this "
            "deployment and its balance can never be read — refusing to shield into a float "
            "nothing can measure"
        )
    if beampay.looks_like_regular_address(addr):
        raise beampay.BeamPayError(
            "the shield target is a 64–72-hex SBBS (regular) address, not a max-privacy token "
            "— shielding to it would settle and shield nothing"
        )


async def mp_address(bp: beampay.BeamPay) -> str:
    """OUR PRIMARY max_privacy address, PROVEN to be ours.

    ⚠️ IT IS NO LONGER THE ADDRESS A SHIELD SENDS TO — `shield_target` makes a fresh one per
    chunk, because consecutive sends to one max-privacy address collide. This is the address
    that stays load-bearing for everything else about the float: it is the FIRST entry of
    `mp_registry`, the address a release registers its contract txid to, and therefore the
    address BeamPay books the crossing and its BEAM fee against. A shield that cannot prove it
    is shielding into a float the payout side can neither read nor spend, so the proof still
    gates the shield.

    The verdict is stored on the row, in the spirit of `verify_arb_key.py`. RAISES when the
    address cannot be proven; never created while shielding is off."""
    want = (settings.beam_mp_address or "").strip()
    row = await db().treasury.find_one({"_id": "mp_address"})
    stored = str((row or {}).get("address") or "")
    if stored and (row or {}).get("proven_at") and (not want or stored == want):
        return stored
    if not settings.shield_enabled:
        return ""
    created = False
    addr = want or stored
    if not addr:
        # created THROUGH BeamPay, never through the wallet-api: an address the ledger does not
        # know about is a float nothing can read (law 10, and INTEGRATION.md §6 rule 3).
        addr = await bp.create_wallet("pgasme treasury float primary", "max_privacy")
        created = True
    await _prove_mp_address(bp, addr)
    await db().treasury.update_one(
        {"_id": "mp_address"},
        {
            "$set": {
                "address": addr,
                "type": "max_privacy",
                "is_mine": True,
                "source": "config" if want else ("created" if created else "stored"),
                "proven": ["validate_address", "beampay_registered", "not_a_regular_address"],
                "proven_at": time.time(),
            }
        },
        upsert=True,
    )
    return addr


async def _treasury_claimed(dep: dict[str, Any]) -> None:
    """⛔ **THE TREASURY MACHINE ENDS HERE WHEN SHIELDING IS OFF** (admin, 2026-09-10 11:2xZ:
    *"let's not shield deposits. Skip this step, we just distribute what we receive from
    different new wallets"*). `PGAS_SHIELD_ENABLED=0` is the permanent setting now, and payout
    privacy on the Beam side comes from a FRESH regular address per crossing instead
    (`crossing_address`), not from Lelantus.

    So with the flag off this plans nothing, holds nothing and pages nothing: it stamps
    `treasury_done_at` once and the row is never reached again (`treasury_once` filters on it).
    It used to advance to `shielding` regardless and let `_treasury_shielding` refuse — which
    left every claimed deposit sitting `shielding`(dark) with an hourly "treasury work is
    waiting" page about work that is never going to be done. The live deposit
    45ef74d98f10abe7e82aa1f9 is exactly that row, and `migrate_skipped_shields` is what frees it.

    The shield code below stays, behind the flag, for a deployment that wants it and for the
    0.02652864 bETH already inside the max-privacy lock — which the SHIELDED source still
    spends once the 72 h expire, and which is never refilled."""
    if not settings.shield_enabled:
        marked = await db().deposits.find_one_and_update(
            {"_id": dep["_id"], "treasury": "claimed", "treasury_done_at": {"$exists": False}},
            {
                "$set": {
                    "treasury_done_at": time.time(),
                    "shield_skipped": "PGAS_SHIELD_ENABLED=0 — deposits are not shielded; "
                                      "payout privacy is a fresh Beam address per crossing",
                    "updated_at": time.time(),
                },
                "$unset": {"dark": "", "hold_reason": "", "hold_at": ""},
            },
        )
        if marked is not None:
            log.info(
                "deposit %s: claimed and DONE — shielding is off, so the treasury machine ends "
                "here", dep["_id"],
            )
        return
    asset = _asset_of(dep)
    plan = shield_plan(
        int(dep.get("value_groth") or 0), settings.shield_denoms_for(asset.key)
    )
    if not plan:
        await _advance(
            "deposits",
            dep["_id"],
            "treasury",
            "claimed",
            "shielded",
            "deposit_shielded",
            "Treasury: nothing to shield (zero value) — the deposit is done",
            "deposit_id",
            shield_plan=[],
            shielded_at=time.time(),
        )
        return
    if len(plan) > settings.shield_max_chunks:
        # refuse and hold rather than emit ten thousand transactions — and the `shielding` SLA
        # could never fire while they ground out, because each success refreshed the row
        await _hold(
            "deposits",
            dep["_id"],
            f"shielding {tg.fmt_groth(int(dep.get('value_groth') or 0))} {asset.key} would take "
            f"{len(plan)} chunks (limit {settings.shield_max_chunks}) — the denominations for "
            f"this asset need an operator, nothing was sent",
            key=f"treasury-plan:{dep['_id']}",
            cooldown_s=6 * 3600,
        )
        return
    await _advance(
        "deposits",
        dep["_id"],
        "treasury",
        "claimed",
        "shielding",
        "deposit_shielding",
        f"Treasury: shielding {tg.fmt_groth(int(dep.get('value_groth') or 0))} "
        f"{dep.get('asset')} in {len(plan)} max-privacy chunk(s)",
        "deposit_id",
        shield_plan=plan,
        shield_txids=[],
        shield_ids=[],
        # THE HISTORY FLOOR, PINNED AT THE TRANSITION. `treasury_at` cannot be it: a hold for a
        # human re-stamps it (and so would a re-plan), and a floor that moves FORWARD makes an
        # already-settled chunk's transaction invisible — which reads as "never sent".
        shield_since=time.time(),
        # the shape the slot claim needs, so no later pass has to migrate the row
        shield_calls=[],
    )


def shield_comment(dep_id: str, k: int) -> str:
    """The IDENTITY of shield chunk `k` of this deposit.

    ⛔ BeamPay's `/withdraw` is not idempotent and answers no txid (INTEGRATION.md §4), so this
    string is the only thing that can ever tell a resend from a first send. It is chosen before
    the call, written on the row before the call, and looked for in `/transactions` instead of
    calling again."""
    return f"shield|{dep_id}|{int(k)}"


async def shield_target(bp: beampay.BeamPay, dep_id: str, k: int, attempt: int = 0) -> str:
    """A max-privacy address for EXACTLY ONE shield chunk, created through BeamPay.

    ⛔ **CONSECUTIVE SENDS TO ONE MAX-PRIVACY ADDRESS COLLIDE.** 2026-09-09 23:21–23:23Z,
    deposit 60b0e5703cbad6955af059f1: three max-privacy self-sends of 1,000,000 groth bETH to
    the SAME `PGAS_BEAM_MP_ADDRESS`, seconds apart. Chunk 0 settled (tx f15dd77d…, shielded
    output 28390 at height 4030052). Chunks 1 and 2 were refused by the wallet with
    **"Shielded outp duplicate ← Kernel Type 3"** and status 4, "failed maximum anonymity": a
    max-privacy address publishes ONE-TIME vouchers, the wallet re-used the same voucher for
    every send to it in quick succession, and the identical shielded output cannot be spent
    into the pool twice. Two thirds of that deposit stayed unshielded and the row was held.

    So the address belongs to the CHUNK, not to the deployment: one `/create_wallet` per chunk,
    registered in `mp_addresses` BEFORE anything is sent to it — the registry is what
    `float_groth` sums over, and an address the registry does not know is value nothing can
    measure — and never used for a second send.

    Idempotent on the chunk ATTEMPT: `mp_addresses._id` is `shield_comment(dep_id, k)`, plus
    `#<attempt>` from the second attempt on. A chunk that was prepared and then refused —
    `{"status": false}`, a kill switch, a lost address write — re-uses the address it already
    made rather than leaking one per pass, and that is safe precisely because NOTHING was sent
    to it.

    ⛔ **`attempt` IS NOT DECORATION.** It is how many transactions carrying this chunk's
    comment are already DEAD, counted from BeamPay's own history — so a chunk an operator
    re-plans after a failure gets a NEW address and never the one that failed. Re-sending to
    the address a failed max-privacy send already used reproduces the exact collision this
    function exists to prevent: the voucher it published has been used once, and the wallet
    builds the same shielded output again."""
    doc_id = shield_comment(dep_id, k) + (f"#{int(attempt)}" if int(attempt) > 0 else "")
    row = await db().mp_addresses.find_one({"_id": doc_id})
    addr = str((row or {}).get("address") or "")
    if addr:
        return addr
    addr = await bp.create_wallet(f"pgasme shielded|{dep_id}|{int(k)}", "max_privacy", "never")
    await _prove_mp_address(bp, addr)
    await db().mp_addresses.update_one(
        {"_id": doc_id},
        {
            "$setOnInsert": {
                "address": addr,
                "created_at": time.time(),
                "deposit_id": str(dep_id),
                "k": int(k),
                "attempt": int(attempt),
                "purpose": "shield",
            }
        },
        upsert=True,
    )
    # the REGISTRY wins, never the local variable: an upsert that found a document did not
    # write ours, and a float summed over one registry must not be sent to a second belief
    # about which address this chunk uses. The per-pass cache is dropped so the next read of
    # the float includes what was just registered.
    _PASS["mp_registry"] = None
    _PASS["float"] = {}
    row = await db().mp_addresses.find_one({"_id": doc_id})
    return str((row or {}).get("address") or addr)


def _shield_slot(v: Any) -> dict[str, Any]:
    """One chunk's slot, normalised: `{"at": when /withdraw was called, "to_address": where}`.

    ⚠️ TWO SHAPES ARE LIVE. The slot was a bare timestamp before the per-chunk address existed
    and is `{"at": …, "to_address": …}` now, so a number is read as `{"at": it,
    "to_address": ""}` — a legacy chunk's destination is recoverable from its transaction's
    `receiver` and is never invented here. `0` (and absent) is a slot never claimed."""
    if isinstance(v, dict):
        return {"at": float(v.get("at") or 0), "to_address": str(v.get("to_address") or "")}
    return {"at": float(v or 0), "to_address": ""}


def shield_calls_of(dep: dict[str, Any]) -> list[dict[str, Any]]:
    """Each chunk's slot — WHEN `/withdraw` was called and the address it was called with — as
    a LIST, whatever shape the row carries.

    ⛔ **A dotted `$set` into a field that does not exist makes a MAP, not a list.** A deposit
    that entered `shielding` before `shield_calls` was initialised has no array for the
    `shield_calls.<k>` slot claim to write into, so Mongo creates `{"0": 1788…}` — and
    `list({"0": …})` is `["0"]`, a list of KEYS. The next pass then reads chunk 0's marker as
    the string `"0"`, concludes the call was never made, and calls `/withdraw` a SECOND time:
    one chunk becomes two sends of the treasury's money, and BeamPay dedupes neither because
    the route has no idempotency key at all. Read through here, never `dep.get("shield_calls")`
    directly."""
    raw = dep.get("shield_calls")
    if isinstance(raw, dict):
        out: list[dict[str, Any]] = []
        for k, v in sorted(raw.items(), key=lambda kv: int(kv[0])):
            # a gap is a slot never claimed
            out.extend(_shield_slot(0) for _ in range(int(k) - len(out)))
            out.append(_shield_slot(v))
        return out
    return [_shield_slot(x) for x in (raw or [])]


# The words a chunk can be in. Only `failed` may be re-planned by a human; the three racy ones
# must never be, because the wallet may still be about to emit a transaction for them and a
# re-plan that races it queues a second send of one chunk.
SHIELD_SETTLED = "settled"
SHIELD_PENDING = "pending"
SHIELD_FAILED = "failed"
SHIELD_UNSENT = "unsent"
SHIELD_UNKNOWN = "unknown"
SHIELD_DUPLICATE = "duplicate"
SHIELD_RACY = (SHIELD_PENDING, SHIELD_UNKNOWN, SHIELD_DUPLICATE)


def shield_classify(
    matches: list[dict[str, Any]], called_at: float, written_off: set[str]
) -> dict[str, Any]:
    """ONE reading of what BeamPay's history says about ONE chunk — the fact both the processor
    and the re-plan CLI act on, so there is one writer of it and not two that disagree.

    `{"state": …, "live": [live txs, newest first], "dead": [cancelled/failed ones]}`:

      settled    one live transaction, status COMPLETED — this chunk's value IS shielded
      pending    one live transaction that has not settled: the wallet may still land it
      failed     no live transaction, and at least one dead one nobody has written off — no
                 value moved, and this is the only state a human may re-plan
      unknown    no transaction at all, but `/withdraw` WAS called: BeamPay queues the send and
                 its daemon emits it seconds later, so this is the one state where calling
                 again (or re-planning) would double-send
      unsent     no transaction and `/withdraw` was never called for it
      duplicate  TWO live transactions for one comment — the chunk went out twice, and only a
                 human can decide what that means

    A written-off dead transaction is one an operator has already accounted for in a re-plan;
    it stays visible in `dead` (it is evidence) and stops being a reason to hold."""
    alive = [m for m in matches if int(m.get("status", -1)) not in beam.TX_DEAD]
    gone = [m for m in matches if int(m.get("status", -1)) in beam.TX_DEAD]
    if len(alive) > 1:
        state = SHIELD_DUPLICATE
    elif alive:
        state = (
            SHIELD_SETTLED
            if int(alive[0].get("status", -1)) in beam.TX_SETTLED
            else SHIELD_PENDING
        )
    elif [d for d in gone if str(d.get("txId")) not in written_off]:
        state = SHIELD_FAILED
    elif float(called_at or 0) > 0:
        state = SHIELD_UNKNOWN
    else:
        state = SHIELD_UNSENT
    return {"state": state, "live": alive, "dead": gone}


async def _shield_scan(
    bp: beampay.BeamPay, treasury: str, dep_id: str, chunks: int, since_ts: float
) -> tuple[dict[int, dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    """`({chunk: its LIVE transaction}, {chunk: its DEAD transactions})` — ONE scan per pass.

    This is both the idempotency check and the settlement read, because for a `/withdraw` they
    are the same question: BeamPay's history is the only place a withdrawal we made can be
    identified, and the comment is the only thing in it that is ours.

    ⛔ **A DEAD TRANSACTION IS NOT A LANDING.** A chunk whose send FAILED or was CANCELLED moved
    no value at all — the wallet built a shielded output the chain refused (`shield_target`) —
    and the groth is still sitting unshielded at the treasury. Counting it with the live ones
    made this scan answer "chunk 1 has a transaction", which walked the machine PAST that chunk
    to queue the next send into the same broken plan, and made an ordinary failed-then-resent
    pair read as a DOUBLE SEND. The two are separated here, and only a live transaction is a
    chunk's transaction.

    ⛔ TWO LIVE transactions carrying ONE comment IS a double send of that chunk. `/withdraw`
    has no idempotency key at all, so it is the accident this route makes possible — and the
    one thing that must never be silent. It is paged IMMEDIATELY; the newest is used so the row
    still advances, because the second send's value is already gone and stalling the deposit
    does not bring it back."""
    comments = {shield_comment(dep_id, k): k for k in range(chunks)}
    rows = await bp.find_txs_by_comments(treasury, comments, since_ts)
    live: dict[int, dict[str, Any]] = {}
    dead: dict[int, list[dict[str, Any]]] = {}
    for comment, matches in rows.items():
        k = comments[comment]
        seen = shield_classify(matches, 0.0, set())
        if seen["dead"]:
            dead[k] = seen["dead"]
        if seen["state"] == SHIELD_DUPLICATE:
            await tg.alert(
                "deposit_shield_duplicate",
                f"DOUBLE SEND: {len(seen['live'])} live transactions carry the shield comment "
                f"{comment!r} — /withdraw is not idempotent and this chunk went out more than "
                f"once. Transactions: {', '.join(str(m.get('txId')) for m in seen['live'])}",
                deposit_id=dep_id,
            )
        if seen["live"]:
            live[k] = seen["live"][0]
    return live, dead


async def _treasury_shielding(dep: dict[str, Any]) -> None:
    dep_id = dep["_id"]
    asset = _asset_of(dep)
    plan: list[int] = list(dep.get("shield_plan") or [])
    calls: list[float] = shield_calls_of(dep)
    if not isinstance(dep.get("shield_calls"), list):
        # migrate the row to the shape the slot claim needs, conditional on the shape we read
        # (so a pass that already migrated it is not overwritten). `shield_calls: None` matches
        # a row where the field is absent, which is the legacy case this repairs.
        await db().deposits.update_one(
            {"_id": dep_id, "treasury": "shielding", "shield_calls": dep.get("shield_calls")},
            {"$set": {"shield_calls": calls, "updated_at": time.time()}},
        )
    bp = beampay.beampay()
    treasury = beampay.treasury_address()
    # ⛔ THE FLOOR THE HISTORY WALK GOES BACK TO, AND IT MUST NEVER MOVE FORWARD. `shield_since`
    # is stamped once, when the row entered shielding; `treasury_at` is only the fallback for a
    # row that entered before that field existed, because `_hold_for_a_human` re-stamps
    # `treasury_at` and a floor later than a settled chunk's transaction hides it — after which
    # the chunk reads as never sent. A walk bounded by a row count instead would fail open
    # exactly when the wallet is busy, which is exactly when a double-send costs the most.
    since = float(
        dep.get("shield_since")
        or dep.get("treasury_at")
        or dep.get("claimed_at")
        or dep.get("created_at")
        or 0
    )
    if since <= 0:
        # without a floor the history walk has nothing to walk back TO, and a scan that cannot
        # complete must refuse rather than answer "no such transaction" — which here would mean
        # sending a chunk that may already be on its way.
        await _hold(
            "deposits",
            dep_id,
            "this row records no time at which it entered shielding, so BeamPay's history "
            "cannot be searched for the chunks it may already have sent",
            key=f"treasury-nosince:{dep_id}",
            cooldown_s=6 * 3600,
        )
        return
    if not dep.get("shield_since"):
        # a row that entered shielding before the field existed: pin its floor NOW, from the
        # only evidence it has, BEFORE anything can re-stamp `treasury_at` under it
        await _checkpoint("deposits", dep_id, shield_since=since)
    live, dead = await _shield_scan(bp, treasury, dep_id, len(plan), since)
    txids = [str(live[i]["txId"]) for i in range(len(plan)) if i in live]
    if txids != list(dep.get("shield_txids") or []):
        await _set("deposits", dep_id, shield_txids=txids)
    # ⛔ A FAILED CHUNK IS A HUMAN'S, ONCE — AND IT IS CHECKED BEFORE THE NEXT CHUNK IS CHOSEN.
    # A plan with a dead chunk in it must not grow another send: the collision `shield_target`
    # describes repeats for every chunk that follows, so one refusal becomes N. And this hold
    # is TERMINAL (`_hold_for_a_human` takes the row out of `shielding`), because a `_hold` here
    # re-decided the same thing every 6 h forever and paged every time.
    # `shield_writeoffs` is how the operator's re-plan says "that txid is accounted for" —
    # append-only, carried on the row, never an edit of history.
    written_off = {str(t) for t in (dep.get("shield_writeoffs") or [])}
    unresolved = {
        i: [t for t in txs if str(t.get("txId")) not in written_off]
        for i, txs in dead.items()
    }
    unresolved = {i: txs for i, txs in unresolved.items() if txs}
    if unresolved:
        first = min(unresolved)
        tx = unresolved[first][0]
        await _hold_for_a_human(
            "deposits",
            dep_id,
            "treasury",
            "shielding",
            f"a shield chunk failed on Beam: chunk {first + 1}/{len(plan)} is "
            f"{tx.get('status_string') or tx.get('status')} (tx {tx.get('txId')}), so that value "
            f"is still UNSHIELDED and nothing is auto-retried. Run `python -m pgasme.beam "
            f"replan-shield --deposit {dep_id}` to see the chunk table, then the same command "
            f"with --apply to re-send the failed chunk(s) to fresh max-privacy addresses",
            "deposit_shield_failed",
            "deposit_id",
        )
        return
    # the first chunk BeamPay has no LIVE transaction for. Not `len(txids)`: a gap must not be
    # silently skipped by counting.
    k = next((i for i in range(len(plan)) if i not in live), len(plan))

    if k >= len(plan):
        for i in sorted(live):  # every chunk must be a settled kernel before the float counts
            if int(live[i].get("status", -1)) not in beam.TX_SETTLED:
                return
        # BeamPay sets the withdrawal fee itself (0.001 BEAM regular, 0.011 offline /
        # max-privacy) and ignores any fee we send. So it is read back from the transactions it
        # made, summed across the chunks, and said out loud when it is absurd.
        total_fee = sum(int(live[i].get("fee") or 0) for i in live)
        await fee_charged(
            "deposits", dep_id, {"fee": total_fee},
            f"shielding {dep_id} ({len(txids)} chunk(s))", "deposit_id",
        )
        # the BUDGET is per CHUNK, so the history has to be per chunk too: the sum above is
        # what this deposit's shielding cost, the max below is what the next chunk may cost
        per_chunk = max((int(live[i].get("fee") or 0) for i in live), default=0)
        if per_chunk > 0:
            await _checkpoint("deposits", dep_id, shield_fee_groth=per_chunk)
        await _advance(
            "deposits",
            dep_id,
            "treasury",
            "shielding",
            "shielded",
            "deposit_shielded",
            f"Treasury: shielded {len(txids)} chunk(s) — the value joins the float once the "
            f"outputs mature",
            "deposit_id",
            shielded_at=time.time(),
        )
        return

    called_at = calls[k]["at"] if k < len(calls) else 0.0
    if called_at:
        # ⛔ `/withdraw` WAS CALLED FOR THIS CHUNK AND ITS TRANSACTION IS NOT VISIBLE YET.
        # BeamPay queues the send and its daemon emits it seconds later, so a gap here is
        # normal — and a SECOND call would queue a second withdrawal, which is precisely what
        # this route cannot dedupe. So it waits, and past the deadline a human decides.
        if time.time() - called_at > UNRESOLVED_S:
            await _hold_for_a_human(
                "deposits",
                dep_id,
                "treasury",
                "shielding",
                f"shield chunk {k + 1}/{len(plan)} was queued with BeamPay "
                f"{int((time.time() - called_at) // 60)} min ago and no transaction carrying "
                f"{shield_comment(dep_id, k)!r} has appeared — this needs a human and is NOT "
                f"auto-retried",
                "deposit_shield_unresolved",
                "deposit_id",
            )
        return

    if not settings.shield_enabled:
        log.info("would shield chunk %d/%d of %s: %s groth", k + 1, len(plan), dep_id, plan[k])
        await _hold(
            "deposits",
            dep_id,
            f"PGAS_SHIELD_ENABLED=0 — treasury work is waiting: {len(plan) - k} shield chunk(s) "
            f"of deposit {dep_id} are unsent",
            key=f"treasury-dark-shield:{dep_id}",
            dark=True,
        )
        return
    if workers.paused():
        return
    # ⛔ **KEEP A WORKING FLOAT OUT OF LELANTUS** (2026-09-10, admin: "in case we don't have
    # available ETH for unlock to let people withdraw, let's keep bETH out of Lelantus").
    # Shielding is not a transfer between two places we can spend from: a max-privacy output is
    # LOCKED for up to `MaxPrivacyLockTimeLimitHours` (Beam's default, 72) after it settles, and
    # the anonymity-set target that would release it sooner cannot be reached on a pool growing
    # ~28 outputs a day. Measured on the box: three chunks confirmed at heights 4,030,052–127 and
    # ten hours later the wallet still reported `available_mp 0 / maturing_mp 1,652,864`. So
    # value shielded is value NO PAYOUT CAN SPEND for three days, and the treasury must keep
    # enough unshielded to cover what it has already promised.
    #
    # The reserve is recomputed from live numbers on EVERY pass — a constant here would be
    # §WE-SET-IT-WE-DONT-READ-IT one more time — and it is the larger of the flat floor and
    # everything the treasury's balance is already promised to, grossed up by the buffer. Only
    # the EXCESS is chunked; a chunk that does not fit under it is a refusal, and a refusal
    # writes a row.
    #
    # ⛔ **`LIABLE` STOPS AT `scheduled`; THE TREASURY'S BALANCE DOES NOT.** A release leaves the
    # liabilities the moment it is admitted — correctly, because `inflight_groth` reserves it
    # against the float instead and counting it in both would stop the treasury ever shielding
    # again — but the value is STILL SITTING at the treasury until the burn is booked. Measuring
    # the reserve without it shielded money a crossing in flight was about to spend, into a lock
    # no payout can reach for 72 hours. Two DISJOINT sets (`LIABLE` vs `INFLIGHT`), each summed
    # once, so the sum reserves each groth exactly once.
    keep_floor = max(0, int(settings.shield_keep_groth or 0))
    liability = await scheduled_liability_groth(asset)
    inflight = await inflight_groth(asset)
    liability_keep = liability_reserve_groth(liability + inflight)
    keep = max(keep_floor, liability_keep)
    unshielded = await bp.available_groth(treasury, asset.aid)
    if unshielded - keep < int(plan[k]):
        await _hold(
            "deposits",
            dep_id,
            f"keeping a working float unshielded: the treasury holds "
            f"{tg.fmt_groth(unshielded)} {asset.key} and this policy keeps "
            f"{tg.fmt_groth(keep)} spendable (floor {tg.fmt_groth(keep_floor)}, "
            f"scheduled-but-unreleased payouts {tg.fmt_groth(liability)} + crossings in flight "
            f"{tg.fmt_groth(inflight)} → "
            f"{tg.fmt_groth(liability_keep)} with the buffer), so chunk {k + 1}/{len(plan)} of "
            f"{tg.fmt_groth(int(plan[k]))} is not sent — a shielded output is locked for up to "
            f"72 h and could not pay those orders",
            key=f"treasury-float:{dep_id}",
        )
        return
    # THE PRIMARY, PROVEN FIRST. It is not this chunk's destination any more (`shield_target`
    # makes a fresh one below) — it is the float's first entry and the address every release
    # books its crossing and its BEAM fee to, so shielding into a float that address cannot
    # read or spend is work with nowhere to go.
    try:
        primary = await mp_address(bp)
    except beampay.BeamPayError as e:
        await _hold(
            "deposits",
            dep_id,
            f"the shield target could not be proven to belong to this wallet: {e}",
            key=f"treasury-mpproof:{dep_id}",
            cooldown_s=6 * 3600,
        )
        return
    if not primary:
        await _hold(
            "deposits",
            dep_id,
            "no max_privacy address is configured or stored, so there is nowhere to shield to",
            key=f"treasury-nomp:{dep_id}",
        )
        return
    if not await _beam_fees_ok(bp, "shield", dep_id, "deposits"):
        return
    # ⛔ ONE CONDITIONAL UPDATE BEFORE THE IRREVERSIBLE CALL. `--workers 1` in a unit file is
    # not code: two passes that both read "chunk k unsent" would both call `/withdraw`, and
    # BeamPay would queue two withdrawals of the treasury's money for one chunk. The marker is
    # written FIRST, so the loser of the race finds it and stops, and so a lost response leaves
    # behind the one thing that stops the next pass from calling again.
    now = time.time()
    won = await db().deposits.find_one_and_update(
        {
            "_id": dep_id,
            "treasury": "shielding",
            # unclaimed is "absent" OR "the zero placeholder a migrated gap left behind" — a
            # slot that exists but holds no timestamp has never been called with, and a filter
            # that only asked `$exists: False` would refuse to claim it forever
            "$or": [{f"shield_calls.{k}": {"$exists": False}}, {f"shield_calls.{k}": 0}],
        },
        # ⛔ THE SLOT IS AN OBJECT, WRITTEN WHOLE. The destination goes in beside `at` a moment
        # later, and `$set` of `shield_calls.<k>.to_address` into a SCALAR is a hard MongoDB
        # error ("cannot create field in element"), which on this path would mean the address
        # of a chunk that is about to be sent never reaching the row. mongomock accepts it
        # silently, so no test would have caught it — the shape is chosen here on purpose.
        {"$set": {f"shield_calls.{k}": {"at": now}, "updated_at": now}},
    )
    if won is None:
        return  # another pass owns this chunk
    if workers.paused():
        # the switch may have been thrown while we were claiming the slot; nothing is queued
        # yet, so the marker is released and the chain halts exactly here
        await db().deposits.update_one({"_id": dep_id}, {"$set": {f"shield_calls.{k}": 0}})
        return
    # ⛔ A FRESH MAX-PRIVACY ADDRESS FOR THIS CHUNK AND NO OTHER — the whole reason this
    # function exists (`shield_target`: the wallet re-uses a max-privacy voucher and the chain
    # refuses the duplicate shielded output). Asked AFTER the slot is claimed, so exactly one
    # pass can ever ask for this chunk's address, and released back to unsent when it cannot be
    # made and proven, because nothing has been queued yet.
    try:
        # a chunk with dead transactions has already had a max-privacy address used on its
        # behalf; the attempt count comes from BeamPay's history, never from a counter of ours
        addr = await shield_target(bp, dep_id, k, attempt=len(dead.get(k) or []))
    except beampay.BeamPayError as e:
        await db().deposits.update_one({"_id": dep_id}, {"$set": {f"shield_calls.{k}": 0}})
        await _hold(
            "deposits",
            dep_id,
            f"a fresh max-privacy address for shield chunk {k + 1}/{len(plan)} could not be "
            f"created and proven: {e} — nothing was sent",
            key=f"treasury-mpcreate:{dep_id}",
            cooldown_s=6 * 3600,
        )
        return
    # ON THE CHUNK RECORD BEFORE THE CALL, like the marker itself: which address this chunk was
    # sent to is the only local record of where the value went, and `/withdraw` answers nothing
    await db().deposits.update_one(
        {"_id": dep_id}, {"$set": {f"shield_calls.{k}.to_address": addr}}
    )
    comment = shield_comment(dep_id, k)
    try:
        res = await bp.withdraw(treasury, addr, asset.aid, int(plan[k]), comment)
    except beam.Halted:
        # the switch was thrown INSIDE the mover, so nothing was queued: release the slot and
        # halt the chain exactly here (the caller's two checks are the early refusal, this is
        # the one that still holds when a new call site forgets them)
        await db().deposits.update_one({"_id": dep_id}, {"$set": {f"shield_calls.{k}": 0}})
        return
    except beampay.BeamPayError as e:
        await tg.alert(
            "deposit_shield_unconfirmed",
            f"Shield chunk {k + 1}/{len(plan)} UNCONFIRMED ({beam.redact(e)[:160]}). NOT "
            f"re-sending — /withdraw is not idempotent, so the next pass looks for the comment "
            f"{comment!r} in BeamPay's history instead",
            deposit_id=dep_id,
        )
        return
    if res.get("status") is not True:
        # a REFUSAL, not a failure: HTTP 200 with a reason, and nothing was queued (the lock is
        # atomic and matched nothing). So the marker is released — a chunk that was never
        # queued must stay retryable — and the reason lands on the row.
        await db().deposits.update_one({"_id": dep_id}, {"$set": {f"shield_calls.{k}": 0}})
        await _hold(
            "deposits",
            dep_id,
            f"BeamPay refused shield chunk {k + 1}/{len(plan)}: "
            f"{str(res.get('msg') or res)[:160]} — nothing was queued",
            key=f"treasury-shieldrefused:{dep_id}",
        )
        return
    # one chunk per pass: §9.3 caps shields at ≤ 10 outputs per block and Pgas.me must never be
    # the reason an organic user's shield is delayed. The txid is NOT here to record — the
    # route does not answer one — so the next pass finds it by its comment.
    log.info("deposit %s: shield chunk %d/%d queued with BeamPay", dep_id, k + 1, len(plan))


# ------------------------------------------------------------- treasury: the shield re-plan


def shield_since_of(dep: dict[str, Any]) -> float:
    """The floor a shield history walk searches back to. ONE reader of this fallback chain, so
    the CLI cannot search a different window from the processor's."""
    return float(
        dep.get("shield_since")
        or dep.get("treasury_at")
        or dep.get("claimed_at")
        or dep.get("created_at")
        or 0
    )


async def shield_chunk_report(
    bp: beampay.BeamPay, dep: dict[str, Any]
) -> list[dict[str, Any]]:
    """One row per shield chunk: the plan, what BeamPay's history says, and the ONE word that
    decides whether a human may re-plan it (`shield_classify`).

    READ ONLY, and it alerts about nothing: it is what a `--dry-run` prints, and a tool an
    operator runs to look must not page anybody or move a row.

    RAISES when BeamPay cannot be read — an incomplete history walk answers "no such
    transaction", which here would mean re-sending a chunk that is already on its way — and
    when the row carries no time to search back to."""
    dep_id = str(dep["_id"])
    plan = [int(x) for x in (dep.get("shield_plan") or [])]
    calls = shield_calls_of(dep)
    written_off = {str(t) for t in (dep.get("shield_writeoffs") or [])}
    since = shield_since_of(dep)
    if since <= 0:
        raise beampay.BeamPayError(
            f"deposit {dep_id} records no time at which it entered shielding, so BeamPay's "
            f"history cannot be searched for the chunks it may already have sent"
        )
    comments = {shield_comment(dep_id, k): k for k in range(len(plan))}
    rows = await bp.find_txs_by_comments(beampay.treasury_address(), comments, since)
    out: list[dict[str, Any]] = []
    for k, amount in enumerate(plan):
        comment = shield_comment(dep_id, k)
        slot = calls[k] if k < len(calls) else _shield_slot(0)
        seen = shield_classify(rows.get(comment) or [], slot["at"], written_off)
        tx = (seen["live"] or seen["dead"] or [{}])[0]
        out.append(
            {
                "k": k,
                "amount": amount,
                "comment": comment,
                "state": seen["state"],
                "txid": str(tx.get("txId") or ""),
                "tx_status": int(tx.get("status", -1)) if tx else -1,
                "tx_status_string": str(tx.get("status_string") or ""),
                # the slot is where WE recorded the destination; a legacy chunk has none, and
                # then the transaction's own receiver is the evidence — never a guess
                "to_address": slot["to_address"] or str(tx.get("receiver") or ""),
                "called_at": slot["at"],
                # only the dead transactions this re-plan would ACCOUNT FOR; one already
                # written off is not written off twice
                "writeoff_txids": [
                    str(d.get("txId"))
                    for d in seen["dead"]
                    if str(d.get("txId")) not in written_off
                ],
            }
        )
    return out


def shield_replan_plan(rows: list[dict[str, Any]]) -> tuple[list[int], list[str], list[str]]:
    """`(chunks to re-plan, txids to write off, the reasons a re-plan must REFUSE)`.

    ⛔ THE REFUSALS ARE THE POINT. A `pending` chunk may still settle, and an `unknown` one is a
    `/withdraw` BeamPay has accepted and not yet emitted — re-planning either one races the
    wallet and queues a second send of one chunk, which is the exact accident `/withdraw` has
    no idempotency key to prevent. A `duplicate` is already an operator's problem and is not
    something a tool may tidy away."""
    ks = [int(r["k"]) for r in rows if r["state"] == SHIELD_FAILED]
    txids = [t for r in rows if r["state"] == SHIELD_FAILED for t in r["writeoff_txids"]]
    racy = [
        f"chunk {int(r['k']) + 1} is {r['state']}"
        + (f" (tx {r['txid']})" if r["txid"] else "")
        for r in rows
        if r["state"] in SHIELD_RACY
    ]
    return ks, txids, racy


async def replan_shield(
    dep_id: str, chunks: Iterable[int], writeoffs: Iterable[str]
) -> dict[str, Any]:
    """Hand the named shield chunks back to the processor as UNSENT — the ONE write
    `replan-shield --apply` makes.

    ⛔ NEVER AN EDIT OF HISTORY. The settled chunks and their txids are untouched: they ARE the
    shielded value and the only record of it. The failed transactions are written off by
    APPENDING their txids to `shield_writeoffs` — which is what stops `_treasury_shielding`
    holding the row for a human again over transactions an operator has already accounted for —
    and one `shield_replans` entry carries the evidence: when, which chunks, which txids.

    The chunk's IDENTITY does not change: `shield_comment(dep_id, k)` stays the one thing that
    can tell a resend from a first send. What changes is the DESTINATION, and the processor
    picks a fresh max-privacy address for it (`shield_target`) — which is the whole repair.

    ONE CONDITIONAL TRANSITION, through `_advance`, so a row another pass has moved on is not
    re-planned from a stale read and `treasury_at` is stamped by the only function that stamps
    it. `shield_since` is left exactly where it was: the floor a history walk searches back to
    must never move forward, or an already-settled chunk becomes invisible and reads as never
    sent — after which the machine would send it again."""
    ks = sorted({int(k) for k in chunks})
    ids = sorted({str(t) for t in writeoffs if t})
    if not ks:
        return {"ok": False, "why": "no chunk qualifies for a re-plan"}
    dep = await db().deposits.find_one({"_id": dep_id})
    if not dep:
        return {"ok": False, "why": f"no deposit {dep_id!r}"}
    was = dep.get("treasury")
    if was == HELD and str(dep.get("held_from") or "") != "shielding":
        return {
            "ok": False,
            "why": f"this row was held from {dep.get('held_from')!r}, not from shielding",
        }
    if was not in (HELD, "shielding"):
        return {
            "ok": False,
            "why": f"treasury is {was!r} — only a shielding or held-from-shielding row can be "
            f"re-planned",
        }
    plan = list(dep.get("shield_plan") or [])
    claimed = await _advance(
        "deposits",
        dep_id,
        "treasury",
        was,
        "shielding",
        "deposit_shield_replanned",
        f"Treasury: an operator re-planned shield chunk(s) "
        f"{', '.join(str(k + 1) for k in ks)} of {len(plan)} on deposit {dep_id} — they are "
        f"unsent again and will go to fresh max-privacy addresses",
        "deposit_id",
        **{f"shield_calls.{k}": 0 for k in ks},
    )
    if claimed is None:
        return {"ok": False, "why": "another pass moved this row while it was being read"}
    change: dict[str, Any] = {
        "$push": {
            "shield_replans": {
                "at": time.time(),
                "chunks": ks,
                "txids": ids,
                "from": was,
                "by": "replan-shield",
            }
        },
        "$unset": {"held_from": "", "unresolved_at": ""},
    }
    if ids:
        change["$addToSet"] = {"shield_writeoffs": {"$each": ids}}
    await db().deposits.update_one({"_id": dep_id}, change)
    return {"ok": True, "chunks": ks, "writeoffs": ids, "from": was, "chunks_total": len(plan)}


# ----------------------------------------------------------------------------- the loop


PAYOUT_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "scheduled": _payout_scheduled,
    DELAYED: _payout_delayed,
    "releasing": _payout_releasing,
    "bridging": _payout_bridging,
    "delivering": _payout_delivering,
    PAYING: _payout_paying,
    "waiting_for_dep_eth": _payout_any_asset,
    "waiting_for_swap_to_target_asset": _payout_any_asset,
}

TREASURY_HANDLERS: dict[str | None, Callable[[dict[str, Any]], Awaitable[None]]] = {
    None: _treasury_new,
    "claiming": _treasury_claiming,
    "claimed": _treasury_claimed,
    "shielding": _treasury_shielding,
}


async def _run(rows: list[dict[str, Any]], pick: Callable[[dict[str, Any]], Any], what: str) -> int:
    n = 0
    for row in rows:
        handler = pick(row)
        if handler is None:
            continue
        try:
            await handler(row)
            n += 1
        except Exception as e:  # noqa: BLE001 — one order must never block the others
            log.exception("%s %s: %s", what, row["_id"], beam.redact(e))
            await tg.send(
                f"{what} step failed on <code>{row['_id']}</code>: "
                f"{tg.esc(beam.redact(f'{type(e).__name__}: {e}'))[:300]}",
                key=f"{what}-err:{row['_id']}",
                cooldown_s=900,
            )
    return n


async def _due(
    coll: str,
    field: str,
    statuses: Iterable[str | None],
    sort_key: str,
    per_status: int = BATCH,
    base: dict[str, Any] | None = None,
    always: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The rows to work THIS pass, selected per status with its own limit.

    ⛔ One FIFO over every active status starves the queue. `waiting_for_dep_eth` rows always
    hold (the any-asset stub is not implemented), releases held for a human never move, and
    crossings stuck in `delivering` never leave: fifty of those, all with old `release_at`,
    occupied the whole batch and a healthy due payout sat `scheduled` with no hold_reason at
    all — never even reached to be marked, so no monitor saw it either.

    A row whose last pass ended in a hold is also skipped for `hold_backoff_s`: a refusal is a
    decision, and repeating it every 30 seconds crowds out work.

    ⛔ …EXCEPT THE ROWS `always` NAMES (T52, 17:58Z). A crossing whose address the treasury has
    already funded is not a refused gate — it is work in progress with real money sitting on a
    fresh address — and parking it for five minutes at a time is how order 64ee5540… waited an
    hour for an invocation that takes seconds. Those rows are looked at EVERY pass."""
    now = time.time()
    cool = {
        "$or": [
            {"hold_at": {"$exists": False}},
            {"hold_at": {"$lt": now - settings.hold_backoff_s}},
            *([always] if always else []),
        ]
    }
    out: list[dict[str, Any]] = []
    for s in statuses:
        q: dict[str, Any] = {**(base or {}), field: {"$exists": False} if s is None else s, **cool}
        rows = await db()[coll].find(q).sort(sort_key, 1).limit(per_status).to_list(per_status)
        if len(rows) >= per_status:
            await tg.send(
                f"BACKLOG: {coll} {field}={s or '(none)'} filled the whole {per_status}-row "
                f"batch — orders behind it are not being reached",
                key=f"batch-full:{coll}:{s}",
                cooldown_s=3600,
            )
        out.extend(rows)
    return out


async def payouts_once() -> int:
    rows = await _due(
        "payout_requests",
        "status",
        PAYOUT_ACTIVE + PAYOUT_DARK,
        "release_at",
        always=POLL_EVERY_PASS,
    )
    return await _run(rows, lambda r: PAYOUT_HANDLERS.get(str(r.get("status"))), "payout")


async def migrate_skipped_shields() -> int:
    """Free the deposits that are parked at `shielding` because the flag went off under them.

    ⛔ **ONLY THE ROWS THAT SENT NOTHING.** A deposit with a `shield_txids` entry has value on
    its way into (or already inside) the max-privacy pool, and moving its sub-status would make
    a real chunk invisible to `_shield_scan` — the one thing that can ever find it again. Those
    rows keep their state and wait for the flag or for `replan-shield`.

    Never edits history: the row gains `shield_skipped` saying WHY it moved, and the transition
    itself is the conditional update, so it is idempotent by construction and a second pass
    moves nothing. Runs before the treasury pass, so a freed row is worked in the same pass it
    is freed. Returns how many rows it moved."""
    if settings.shield_enabled:
        return 0
    now = time.time()
    moved = 0
    rows = await db().deposits.find(
        {
            "treasury": "shielding",
            "$or": [{"shield_txids": {"$exists": False}}, {"shield_txids": []}],
        }
    ).to_list(BATCH)
    for dep in rows:
        claimed = await db().deposits.find_one_and_update(
            {
                "_id": dep["_id"],
                "treasury": "shielding",
                "$or": [{"shield_txids": {"$exists": False}}, {"shield_txids": []}],
            },
            {
                "$set": {
                    "treasury": "claimed",
                    "treasury_at": now,
                    "updated_at": now,
                    "shield_skipped": "PGAS_SHIELD_ENABLED=0 — this deposit was planned for "
                                      "shielding before the policy changed and NOTHING was "
                                      "ever sent, so the treasury machine ends at claimed",
                    "shield_skipped_at": now,
                },
                "$unset": {"dark": "", "hold_reason": "", "hold_at": "", "hold_paged_at": ""},
            },
        )
        if claimed is None:
            continue
        moved += 1
        log.info("deposit %s: shielding was planned and never sent — back to claimed", dep["_id"])
    if moved:
        await tg.send(
            f"{moved} deposit(s) parked at `shielding` with nothing sent were returned to "
            f"`claimed` — shielding is off and their value stays spendable",
            key="shield-skipped-migration",
            cooldown_s=24 * 3600,
        )
    return moved


async def treasury_once() -> int:
    await migrate_skipped_shields()
    rows = await _due(
        "deposits",
        "treasury",
        (None, *TREASURY_ACTIVE),
        "credited_at",
        # ⛔ …and a deposit whose treasury work is FINISHED is not due for anything. With
        # shielding off `claimed` is the end state (`_treasury_claimed`), so without this filter
        # every settled deposit would be re-read and re-decided on every pass, for ever,
        # crowding the batch that the rows with work left in them share.
        base={"status": "credited", "treasury_done_at": {"$exists": False}},
    )
    return await _run(rows, lambda r: TREASURY_HANDLERS.get(r.get("treasury")), "treasury")


async def process_once() -> dict[str, int]:
    """One pass of the order processor: every payout order, then every deposit's treasury work.

    Refuses to run at all when another process holds the lease — one writer per resource.

    ⚠️ **A RESTART IS NOT A SECOND PROCESSOR.** A process killed mid-pass leaves its lease held
    until it expires, so the replacement is refused for up to one `payout_lease_ttl_s` — every
    ordinary deploy paged "a second payout processor is running" for two minutes, which is
    precisely the alert that trains an operator to ignore the pager (law 15). The refusal is
    therefore TIMED: silent (INFO, and the row it writes is the return value) while it could
    still be our own corpse, and paged only once it has OUTLIVED the TTL — which can only happen
    if somebody is renewing the lease, i.e. a second live owner. `release_lease()` in the
    shutdown path makes the common case not even reach the silent branch.

    ⛔ The clock is OURS, not the lease document's. Measuring "how long has the holder held it"
    from `at` would restart on every renewal the other process makes, so a genuinely duplicated
    processor — the one case worth paging — would page never."""
    if not await acquire_lease():
        holder = await lease_holder()
        now = time.time()
        if _LEASE["refused_since"] is None:
            _LEASE["refused_since"] = now
        waited = now - float(_LEASE["refused_since"])
        ttl = float(settings.payout_lease_ttl_s)
        # a decision path writes a row: `lease: 0` with the wait on it is what /v1/health and
        # the caller see, so this refusal is never a print()-only one (law 12)
        if waited > ttl:
            log.warning(
                "payout processor: the lease has been held by %s for %.0fs (TTL %.0fs) — a "
                "second processor is renewing it",
                (holder or {}).get("owner"), waited, ttl,
            )
            await tg.send(
                f"REFUSED: a second payout processor is running — the lease has been held by "
                f"another process for {int(waited)}s, longer than the {int(ttl)}s TTL, so it is "
                f"being RENEWED and is not a restart. This one did nothing",
                key="payout-lease",
                cooldown_s=3600,
            )
        else:
            log.info(
                "payout processor: the lease is held by %s (%.0fs of at most %.0fs) — a restart "
                "within the TTL; waiting it out",
                (holder or {}).get("owner"), waited, ttl,
            )
        return {"payouts": 0, "treasury": 0, "lease": 0, "lease_refused_s": int(waited)}
    _PASS["beam_fee"] = 0
    _PASS["fee_budget"] = {}
    # the max-privacy registry and the float summed over it are one pass's answer, never two:
    # an address created by a shield in THIS pass is in the next pass's registry
    _PASS["mp_registry"] = None
    _PASS["float"] = {}
    # the attribution pre-flight is asked ONCE per pass, and must not survive into the next one:
    # a key rotated, a BeamPay restarted or a route deployed between passes is a new answer
    _PASS["expect_route"] = None
    _PASS["float_parts"] = {}
    _PASS["wallet"] = {}
    _PASS["wallet_at"] = {}
    _PASS["coins"] = None
    _PASS["crossing_debt"] = None
    _PASS["holds"] = {}
    _PASS["unheld"] = {}
    _FLOAT_SCHED.clear()
    out = {"payouts": await payouts_once(), "treasury": await treasury_once(), "lease": 1}
    # ⛔ THE REFILL PASS MUST NOT BE ABLE TO TAKE THE PASS DOWN. Every payout above has already
    # run; an exception here would lose the hold digest and the return value with it, and the
    # thing it is deciding — "does our own float need topping up" — is the least urgent work in
    # the pass. It says what failed and the next pass tries again.
    try:
        out["refills"] = await refill_once()
    except Exception as e:  # noqa: BLE001 — one optional decision must never block the others
        log.exception("refill pass failed: %s", beam.redact(e))
        out["refills"] = 0
        await tg.send(
            f"the distributor refill pass failed: "
            f"{tg.esc(beam.redact(f'{type(e).__name__}: {e}'))[:300]}",
            key="refill-err",
            cooldown_s=3600,
        )
    # the holds this pass decided, as ONE page per kind (and one line for what stopped waiting)
    await flush_hold_digest()
    return out
