"""Treasury UTXO management — how many COINS the wallet holds, and how to make more (T36/T36b).

⛔ **A COIN IS A UNIT OF CONCURRENCY, NOT OF VALUE.** Beam locks a whole UTXO for as long as a
transaction that spends it is pending, so what bounds how many things the treasury can do at
once is the COUNT of spendable coins, never their total. On 2026-09-10 10:30Z two releases went
out 0.7 s apart and both came back `Not enough inputs to process the transaction` with 9.8 BEAM
sitting at the treasury: one usable BEAM coin, two sends. T40b's F12 then made every regular
crossing budget TWO BEAM coins (BeamPay's fee on the funding transfer, and then the wallet's own
on the pipe invocation), so a wallet holding a single fee coin admits no direct payout at all —
every one of them HOLDS with "no free coin".

This module owns the policy that measures it (`coin_targets`) and the one operator command that
fixes it (`plan_split` / `run_split`, driven by `python -m pgasme.beam split`).

── THE METHOD: `tx_split`, ONE TRANSACTION (T36b, the admin's decision 2026-09-10 15:07Z) ───

The admin, watching T36's self-transfers land in his group: *"What happened, why do you send
beam to yourself? If it's for UTXOs, there's split_utxos method"*. There is, and the wallet-api
serves it. Read out of the wallet's own source (`wallet/api/v6_0/`; **v6_1 and v6_2 add no
`Split` of their own**, so this IS what :10001 answers):

    v6_api_defs.h:52    macro(Split, "tx_split", API_WRITE_ACCESS, API_SYNC, APPS_BLOCKED)
    v6_api_parse.cpp:555-597
        split.assetId = readOptionalAssetID(*this, params);            // optional, 0 == BEAM
        const json coins = getMandatoryParam<NonEmptyJsonArray>(params, "coins");
        …each amount must be a NON-ZERO 64-bit unsigned integer…
        auto outsCnt = split.coins.size() + 1;                         // + the change coin
        if (split.assetId … != beam::Asset::s_BeamID) outsCnt++;       // + the BEAM change coin
        Amount minimumFee = std::max(fs.m_Kernel + fs.m_Output * outsCnt, fs.get_DefaultStd());
        split.fee  = getBeamFeeParam(params, "fee", minimumFee);       // omitted ⇒ the minimum
        split.txId = getOptionalParam<ValidTxID>(params, "txId");      // 16 bytes, 32 hex
    v6_api_handle.cpp:501-537
        if (data.txId && walletDB->getTx(*data.txId)) { doTxAlreadyExistsError(id); return; }
        walletDB->createAddress(senderAddress);                        // a FRESH address…
        CreateSplitTransactionParameters(senderAddress.m_walletID, data.coins, data.txId)
    → {"txId": "<32 hex>"}

ONE transaction, ONE kernel fee, no `/withdraw`, and therefore none of the three Telegram
notifications BeamPay sends per transfer. `simple_transaction.cpp:36-42` sets **MyID and PeerID
to that same fresh address**, so the value never leaves the wallet and no address anybody tracks
is on either side of it. Two consequences, and they are the whole design:

  ⛔ **THE TXID IS OURS AND A RETRY CANNOT RE-SEND.** `tx_split` takes an optional `txId` and
     REFUSES one the wallet already holds. `split_txid(plan_id)` derives it from the plan, so a
     lost answer, a crash or a re-run resolves to the same id and the wallet itself is the last
     line of defence — a second split is impossible, not merely unlikely. (`submit()` has no
     such parameter, which is why every lost `process_invoke_data` is a human's problem and this
     is not.)

  ⚠️ **THE FEE CANNOT BE BOOKED, AND THAT IS DISCLOSED, NOT REPAIRED.** Read on the box
     2026-09-10 in `/opt/pgasme/beampay`, this is what happens to a split transaction:

        process_payments.py:125  `is_contract_tx` → `tx.get("tx_type") == 12` — a split is
                                 TxType::Simple (0), so the contract path is never entered and
                                 `POST /internal/expect_contract_tx` can NEVER redirect it: an
                                 expectation is only ever consumed inside
                                 `handle_contract_transaction` (process_payments.py:365).
        process_payments.py:557  `if is_self_send(tx, sender_exists, receiver_exists):` and
        bridge_offset.py:78-83   `return bool(receiver_exists) and not sender_exists and not
                                 tx.get("income")` — the receiver is the wallet's own fresh
                                 address, so `receiver_exists` is None and this is NOT a
                                 self-send either. No bridge-claim hunt, no house offset, and
                                 no "Self-send with no bridge claim" page.
        process_payments.py:479  the tx IS inserted into `db.txs` (that is what makes it
                                 findable by `find_tx_by_id`) …
        process_payments.py:656  … and then: `if not sender_exists and not receiver_exists:`
                                 → `⚠️ *Untracked transfer — NOT booked* … attribute manually
                                 if needed (ledger vs wallet will drift until then)` → `return`.
        api.py:694-696           the adjust route's own gate:
                                 `is_contract = gate_doc.get("type") == 12 …`
                                 `if not is_contract or … : 409 tx_not_booked`
        api.py:997               and a read answers `400 not_a_contract_tx`.

     So `POST /internal/ledger/adjust` — the T22 fee repair the brief named — REFUSES a split.
     Left there, the ledger ends **exactly the kernel fee above the wallet**, for ever, until a
     human acknowledges it in BeamPay's own `db.reconciliation` `ack_residual` (there is no
     endpoint for that), and `verify_balances` re-pages it every pass. The operator forbids both
     the manual step and the repeated page.

── THE FEE IS BOOKED, BY BEAMPAY, THROUGH A REGISTRATION (T36c + BeamPay patch #10) ─────────

     The fix is the FOURTH branch of that list, added where the third one is: BeamPay's
     `handle_finalized_transaction` now looks for a self-tx expectation BEFORE it pages
     "Untracked transfer", and books `{asset 0: −fee}` to the address that registered the txid.

        POST /internal/expect_self_tx   {txid, address, trade_ref, kind, asset_id,
                                         expected_fee_groth?}      scope: ledger:adjust
        GET  /internal/expect_self_tx   the side-effect-free preflight (an unpatched BeamPay
                                        answers FastAPI's routing 404 `{"detail":"Not Found"}`)
        GET  /internal/self_tx/{txid}   the read-back — the AUTHORITY on what was booked, since
                                        our own POST reply can be lost while the booking still
                                        happens on a later processor sweep

     ⛔ **THE MARKER GOES IN BEFORE THE IRREVERSIBLE CALL**, exactly as `expect_contract_tx`
     does on every bridge invocation — and here it is also the only order that works, because
     BeamPay claims the transaction's `success` flag before the branch that consumes the
     expectation runs, and refuses a late registration with `409 tx_already_booked`.

     ⛔ **NO PATCH, NO SPLIT.** `run_tx_split` probes the route and REFUSES `--apply` when the
     deployment cannot book a self-transaction, or cannot be asked: a fee burned outside the
     ledger is a drift this command would be choosing to create, and the operator has forbidden
     the manual acknowledgement that clears it. The way out is `--method beampay`, which books
     both sides through BeamPay itself at nine times the fee and three notifications a leg — the
     refusal names it.

     Only the FEE is ever booked, and only the fee the CHAIN charged: a split's outputs are the
     wallet's own coins, so no value leaves the wallet and Σ ledger falls by exactly what the
     wallet burned. `expected_fee_groth` is this module's PREDICTION (`split_min_fee`) and is
     never booked; a charge more than 10 % away from it still books the charge and pages once
     naming both numbers (§WE-SET-IT-WE-DONT-READ-IT).

     ⛔ **A COMMAND DOES NOT POST A BODY WHOSE REFUSAL IT ALREADY KNOWS.** On a plan resumed
     from before any of this existed there is no registration to read back, and `book_split_fee`
     then falls through to the older accounting: it ASKS BeamPay which kind of transaction it
     thinks this is — the same `type` field the adjust route gates on, so the prober calls the
     way the caller calls (law 8) — and posts the adjust only if the answer says it would be
     accepted. Otherwise it writes the refusal, the exact drift and the exact remedy onto the
     plan's row and prints them. It never edits a balance to make its own arithmetic come out
     (law 6), and the dry run says all of this BEFORE the operator authorises anything.

  The ascent T36 needed is GONE with the method: it existed because chunks were made one at a
  time and the wallet kept re-spending the coin the previous chunk had just minted. `tx_split`
  names every output in one transaction, so the coins are EQUAL — for BEAM, all of them exactly
  the size a call costs (`payouts.fee_floor("send")`, the number `coin_capacity` groups
  against), and for a payout asset, the budget divided onto the grid. No dust: what does not
  divide stays in the change coin.

── THE FALLBACK: `--method beampay`, TWO TRANSFERS PER COIN (T36, kept and demoted) ─────────

Everything below is the ORIGINAL method and it is still correct — it is simply nine times the
fee, nine transactions and three Telegram notifications per leg where `tx_split` is one of each.
It is reachable as `--method beampay` and it is the right answer for exactly one situation: a
wallet-api that will not serve `tx_split` at all. Its one advantage is that BeamPay books both
sides itself, so it leaves NO drift.

── THE EVIDENCE FOR THE FALLBACK ────────────────────────────────────────────────────────────

The T36 brief carried a DECISION that a BeamPay `/withdraw` between two addresses of one wallet
is "an INTERNAL ledger move (no on-chain tx, no split)", and that the split therefore had to be
a wallet-api `tx_split` with its fee repaired onto BeamPay's ledger by hand. **That is not what
this deployment does.** Read on the box 2026-09-10, `/opt/pgasme/beampay`:

    api.py:403   `# Handle Internal Transfers (Lock Receiver's Balance) - atomic $add, never …`
                 — when the receiver is a BeamPay address the route locks the RECEIVER as well,
                   and then falls through into the SAME queue every other withdrawal uses:
    api.py:429   `await db.pending_withdrawals.insert_one(withdrawal_request)`
    process_payments.py:1733
                 `response = beam_api.tx_send(value=amount, fee=fee, sender=sender,`
                 `                            receiver=receiver, asset_id=int(asset_id), …)`
                 — unconditional. There is no internal-only branch anywhere on that path.
    process_payments.py:692
                 `*[3/3]* 🔄 *Internal Transfer Confirmed* … 🆔 *Kernel:* {kernel}`
                 — an internal transfer settles with a KERNEL, which is to say: on chain.

The wallet's own UTXO table says the same thing out loud. Transaction `7b5a42bc24…` on THIS
wallet spent one 9.879 BEAM coin and created **two new outputs** — 9.868 `norm` (the receiver's,
ours, later spent by `f15dd77d87…`) and 0.01 `chng` — for a fee of exactly 100,000 groth, which
is BeamPay's own `FEE_REGULAR`. A self-transfer is a real transaction and **it mints a coin of
exactly the amount sent**. That is the entire mechanism a split needs, and it needs no new
wallet-api call to get it: **BeamPay stays the only interface that moves value** (law 10), it
keeps its own books on both sides, so there is no fee to repair through `/internal/ledger/adjust`
and no txid to register with `/internal/expect_contract_tx`.

── THE SHAPE OF A SPLIT ─────────────────────────────────────────────────────────────────────

Every chunk is a ROUND TRIP through one dedicated address created for this plan alone:

    out   treasury ──→ split   (asset amount `size`, plus BeamPay's fee for BEAM)
    back  split ──→ treasury   (the split address's WHOLE balance of the asset)

so the treasury's asset-0 ledger balance — the number every fee gate in `payouts.py` reads —
ends where it started **minus the fees and nothing else**, and the split address ends at exactly
zero. The `back` leg is what makes that true; the `out` leg alone would park the value at an
address the gates do not count, which is a hold of a different kind.

For a payout asset (bETH/bDAI/bWBTC) the fee is paid in BEAM by whichever address is sending, so
the plan opens with one `prime` leg that moves exactly `coins × fee` BEAM to the split address —
the split address then pays its own way and finishes empty in both assets.

⚠️ **THE SIZES ASCEND BY ONE GRID STEP, ON PURPOSE.** The wallet picks the inputs, not us, and
the selection Beam's history on this box is consistent with is "the smallest single coin that
covers the amount". Equal-sized chunks make every coin this command mints the smallest coin that
covers the NEXT request of the same size — so the split would spend its own output and stand
still. Ascending sizes mean a minted coin is always strictly smaller than the next amount asked
for, and the only coin that can cover it is the change. ⛔ This is an inference about somebody
else's algorithm, not a fact we control, so it is never trusted: `run_split` re-reads
`get_utxo` after EVERY chunk and STOPS when the count did not rise, rather than burning forty
fees on a plan that is not working. What it reports then is the shortfall, in words.

⚠️ **THE FEE IS BEAMPAY'S, NOT OURS** (§WE-SET-IT-WE-DONT-READ-IT). `SPLIT_FEE_GROTH` is what
the box's `api.py:311` charges a regular→regular transfer, and it is an ESTIMATE used to size
the plan. The first `out` leg to settle is a CANARY: its fee is read back off the transaction
BeamPay made, and a fee that is not the one the plan was built on halts the whole plan there,
having spent exactly one of them.

Three laws are structural here:

  * ⛔ **The kill switch is checked before every leg**, and again inside `beampay.withdraw`
    itself, so a switch thrown mid-plan halts the plan where it is.
  * ⛔ **`/withdraw` is not idempotent and answers no txid.** Every leg's row is written BEFORE
    the call, keyed by the comment WE chose, and a retry never re-sends a leg: it looks the
    comment up in BeamPay's history first. A leg that was queued and cannot be found is a
    human's, never a machine's.
  * ⛔ **Nothing here re-writes history.** A plan that ends short says so; it does not adjust a
    balance to make its own arithmetic come out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from . import beam, beampay, payouts, workers
from .assets import ASSETS
from .config import settings
from .db import db

log = logging.getLogger("pgasme.utxo")

GROTH = 10**8
BEAM_KEY = "BEAM"
BEAM_ASSET_ID = 0

# The two ways to cut a coin. `tx_split` is one wallet transaction and the DEFAULT; `beampay` is
# T36's round trip through an address of our own — nine times the fee for the same four coins,
# and three Telegram notifications per leg — kept because it is the only method that works on a
# wallet-api that will not serve `tx_split`, and because BeamPay books both its sides itself.
SPLIT_METHOD_TX = "tx_split"
SPLIT_METHOD_BEAMPAY = "beampay"
SPLIT_METHODS = (SPLIT_METHOD_TX, SPLIT_METHOD_BEAMPAY)

# Beam's own fee settings, post-HF3, `core/block_crypt.cpp:1489-1491` — ⛔ THESE ARE THE CHAIN'S
# NUMBERS AND NOT OURS TO TUNE. They exist here so the dry run can PRICE a split before it is
# authorised; no `fee` is ever put on the wire (§WE-SET-IT-WE-DONT-READ-IT), the wallet charges
# its own minimum, and what it charged is read back off BeamPay's record and compared.
FEE_KERNEL_GROTH = 10_000
FEE_OUTPUT_GROTH = 18_000
FEE_DEFAULT_STD_GROTH = 100_000  # `m_Default` — "exactly covers 5 outputs + 1 kernel"

# BeamPay's own fee for a transfer to a REGULAR address (`api.py:311 FEE_REGULAR = 100000`),
# re-exported from the one place this project already named it. We do not send a `fee` field and
# BeamPay ignores one if we did; this is what the plan is SIZED with and it is verified against
# the first settled transaction before the second one is made.
SPLIT_FEE_GROTH = payouts.BEAMPAY_REGULAR_FEE_GROTH

# The grid a coin's size is rounded down to, per asset — and the step the sizes ascend by. A
# coin is a unit of concurrency, so its size only has to be (a) enough to fund what the coin is
# FOR, (b) strictly increasing across the plan, and (c) round enough that an operator reading
# `beam status` recognises the family. No dust is ever created: the remainder stays as change.
GRID_GROTH: dict[str, int] = {
    BEAM_KEY: 100_000,  # 0.001 BEAM
    "ETH": 1_000,  # 0.00001 bETH
    "DAI": 1_000_000,  # 0.01 bDAI
    "WBTC": 100,  # 0.000001 bWBTC
}

# How long a leg may sit between "BeamPay accepted it" and "the transaction is settled" before
# the plan stops and hands it to a human. BeamPay's daemon emits within seconds; a Beam block is
# ~1 min and a withdrawal is finalised after its confirmations.
POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 900.0

# …and how long we then wait for BeamPay's PROCESSOR to book the kernel fee against the
# registration made before the call (BeamPay patch #10). Settlement and booking are two events:
# the processor writes `db.txs` on one 120 s sweep and calls `handle_finalized_transaction` only
# once the tx has its confirmations, so a fee is routinely booked a couple of minutes after the
# transaction is visible. Reading once would report "not booked" about a booking that is simply
# still coming, so this is a bounded WAIT and a timeout is reported as "we could not see it",
# never as "it did not happen".
BOOK_INTERVAL_S = 5.0
BOOK_TIMEOUT_S = 600.0

# What a `tx_split` is, in BeamPay's self-tx vocabulary (`contract_attribution.SELF_TX_KINDS`).
# BeamPay whitelists the kinds it will book, so this string is a contract between the two sides
# and not a label: a kind it does not know is refused at the door with a 422.
SELF_TX_KIND = "utxo_split"
# how far back `find_txs_by_comments` walks when it looks for a leg we may already have sent
LOOKBACK_S = 6 * 3600

# every payout status that is holding wallet inputs right now, and every deposit one
BUSY_PAYOUTS = ("releasing", "bridging", payouts.PAYING)
BUSY_DEPOSITS = ("claiming", "shielding")

# ⛔ A HALTED PLAN IS STILL OPEN. Every way this command stops early leaves something behind —
# value at the split address, a leg queued that nobody can find, a fee that was not the one the
# plan was built on — and starting a SECOND plan on a fresh address on top of that is how the
# first one's money is forgotten. A re-run therefore resumes the halted plan (re-adopting every
# leg by its comment and sending nothing twice), and halts again at the same place with the same
# sentence until a human resolves it.
SPLIT_STATUS_OPEN = ("planned", "running", "halted")

# …and the ONE way out of that set that is not "finish it". A plan whose work is over but whose
# row never reached `done` — every leg settled, its coins long since re-spent, the run that made
# them stopped by a guard — stays OPEN for ever and HIJACKS every later run of this command,
# because a resume adopts the open plan's method whatever was typed. On 2026-09-10 that turned a
# `--method tx_split` into `--method beampay` on the box, with no CLI way out and Mongo off
# limits. `--abandon` is that way out, and it is gated on the two facts that make it safe:
# nothing of the plan is in flight, and its split address holds nothing.
LEG_STATUS_DONE = ("settled", "refused")
# the states in which the WALLET may hold, or may have made, a transaction whose fate this row
# does not record. A plan in one of them is not finished — it is unknown, and the answer to
# unknown is never "forget it".
TX_SPLIT_IN_FLIGHT = ("calling", "sent", "lost")

# the note every split address is created with: `<note>|<asset>|<stamp>`, so an operator reading
# BeamPay's address book can see what an address was for and which plan made it
SPLIT_NOTE = "pgasme-utxo-split"

# /v1/health is polled on a timer by the watchdog and by deploy.sh; the coin counts behind it are
# a BeamPay call and a wallet-api walk, so they are answered from a short cache
HEALTH_TTL_S = 30.0
_HEALTH: dict[str, Any] = {"at": 0.0, "value": {}}


class SplitError(RuntimeError):
    """The plan cannot be built or cannot be continued. NEVER a partial answer."""


def split_min_fee(coins: int, asset_id: int) -> int:
    """What the WALLET will charge for a `tx_split` of `coins` outputs — its own arithmetic,
    port for port (`v6_api_parse.cpp:580-589` over `core/block_crypt.cpp:1489-1491`).

    ⚠️ A PREDICTION, NOT AN INSTRUCTION. We send no `fee`; this is what the plan is priced with
    and what the charged fee is compared against afterwards. `outsCnt` counts the split's own
    outputs PLUS the change coin, and one more for an asset — the BEAM change the fee leaves."""
    outs = int(coins) + 1 + (1 if int(asset_id) != BEAM_ASSET_ID else 0)
    return max(FEE_KERNEL_GROTH + FEE_OUTPUT_GROTH * outs, FEE_DEFAULT_STD_GROTH)


def split_txid(plan_id: str) -> str:
    """The transaction id THIS PLAN will always ask for — 16 bytes, 32 lower-case hex
    (`wallet/core/common.h:56`, `parse_utils.h:215-225`).

    ⛔ THIS IS THE IDEMPOTENCY, AND THE WALLET IS THE ONE THAT ENFORCES IT.
    `v6_api_handle.cpp:518` refuses a `txId` the wallet already holds, so a retry of a plan
    cannot make a second split even if every check in this module were removed. Derived from the
    plan id, which already carries the asset and the stamp, so two plans can never collide and
    one plan can never drift."""
    return hashlib.sha256(f"pgasme:split:{plan_id}".encode()).hexdigest()[:32]


# ───────────────────────────────────────────────────────────────────── the policy (targets)


def target_for(key: str) -> int:
    """How many spendable coins this asset should hold. BEAM's coins are FEE coins — one per
    BEAM-spending leg of a crossing, and T40b's F12 made a regular crossing spend BEAM twice."""
    if key == BEAM_KEY:
        return max(0, int(settings.beam_fee_coins))
    return max(0, int(settings.utxo_target_coins))


async def coin_targets(bp: beampay.BeamPay | None = None) -> dict[str, dict[str, Any]]:
    """`{asset: {"have", "target", "spendable_groth", "why"}}` — target vs actual, per asset.

    ⛔ **`have` IS THE NUMBER THE RELEASE GATE ITSELF COUNTS** (law 8: the prober must call the
    way the caller calls). For a payout asset that is `coins_regular`; for BEAM it is
    `fee_coins`, which is not "how many BEAM coins are there" but "how many of them can each pay
    for a call" — the box holds 0.01 BEAM and 9.58 BEAM and exactly ONE of those is a fee coin.
    Both come off `payouts.wallet_spendable`, the one reader.

    `have` is **None**, never 0, when the coin list could not be read: "we cannot see" is not
    "there are none" (law 8), and a monitor that renders an unreadable wallet as an empty one
    pages about a shortage that may not exist."""
    bp = bp or beampay.beampay()
    out: dict[str, dict[str, Any]] = {}
    fee_coins: int | None = None
    fee_budget_groth: int | None = None
    beam_amounts: list[int] | None = None
    error: str | None = None
    for key, asset in ASSETS.items():
        try:
            spend = await payouts.wallet_spendable(bp, asset)
        except beampay.BeamPayError as e:
            out[key] = {
                "have": None,
                "target": target_for(key),
                "spendable_groth": None,
                "why": f"unreadable: {beam.redact(e)[:160]}",
            }
            error = error or f"{type(e).__name__}: {beam.redact(e)[:160]}"
            continue
        out[key] = {
            "have": spend["coins_regular"],
            "target": target_for(key),
            "spendable_groth": int(spend["regular"]),
            "why": spend.get("coins_error") or "",
        }
        if spend["fee_coins"] is not None:
            fee_coins = int(spend["fee_coins"])
            fee_budget_groth = int(spend["fee_budget_groth"] or 0)
        elif spend.get("coins_error"):
            error = error or str(spend["coins_error"])
    # BEAM is not in ASSETS (it is the fee asset, not a bridgeable one), and its spendable total
    # is the sum of the very coin list `fee_coins` was counted from — one reader, arithmetic on
    # its answer, not a second source.
    try:
        counts = await payouts.coin_counts()
    except (beam.BeamError, TypeError, ValueError) as e:
        counts, error = None, error or f"{type(e).__name__}: {beam.redact(e)[:160]}"
    if counts is not None:
        beam_amounts = list((counts.get(BEAM_ASSET_ID) or {}).get("amounts_regular", []))
    out[BEAM_KEY] = {
        "have": fee_coins,
        "target": target_for(BEAM_KEY),
        "spendable_groth": None if beam_amounts is None else sum(beam_amounts),
        "why": (
            ""
            if fee_coins is not None
            else (error or "the wallet's coin list could not be read")
        ),
        "fee_budget_groth": fee_budget_groth,
    }
    return out


def split_needed(targets: dict[str, dict[str, Any]]) -> list[str]:
    """The assets that are BELOW target and hold something to split — in the order an operator
    should run them (BEAM first: every other split pays its fees out of BEAM coins).

    An asset whose spendable balance is zero is not "below target", it is EMPTY, and telling an
    operator to split nothing is the kind of alert that teaches them to ignore the pager."""
    order = [BEAM_KEY, *ASSETS]
    return [
        k
        for k in order
        if (row := targets.get(k))
        and row.get("have") is not None
        and int(row["have"]) < int(row["target"])
        and int(row.get("spendable_groth") or 0) > 0
    ]


async def coin_health() -> dict[str, dict[str, int | None]]:
    """`{asset: {"have", "target"}}` for `GET /v1/health` — ⛔ **COUNTS, NEVER AMOUNTS.**

    The same law that took the distributor's float and the crossing groths off this endpoint:
    how much value the treasury holds is an inventory anybody could poll on a timer, while how
    many coins it has free is what a watchdog needs to see going to zero. `have: null` is an
    unreadable wallet and is never rendered as 0.

    Fails SOFT and is cached for `HEALTH_TTL_S`: /v1/health is polled by the watchdog and by
    deploy.sh, and a health route that reaches BeamPay and the wallet-api on every poll answers
    as slowly as the slowest of them. An outage answers `{}` — the endpoint's own `ok` is what
    pages, not this."""
    now = time.time()
    if _HEALTH["at"] and now - float(_HEALTH["at"]) < HEALTH_TTL_S:
        return dict(_HEALTH["value"])  # type: ignore[arg-type]
    try:
        targets = await coin_targets()
    except Exception as e:  # noqa: BLE001 — a health route never raises; it says less
        log.warning("coin health unreadable (%s: %s)", type(e).__name__, beam.redact(e))
        value: dict[str, dict[str, int | None]] = {}
    else:
        value = {
            k: {
                "have": None if row["have"] is None else int(row["have"]),
                "target": int(row["target"]),
            }
            for k, row in targets.items()
        }
    _HEALTH.update({"at": now, "value": value})
    return dict(value)


def reset_health_cache() -> None:
    _HEALTH.update({"at": 0.0, "value": {}})


# ───────────────────────────────────────────────────────────────────────────────── the plan


@dataclass(frozen=True)
class Leg:
    """One `/withdraw`. `direction` is which way it goes; the addresses are resolved at apply
    time, because the split address is created once per plan and lives on the plan's row."""

    seq: int
    kind: str  # "prime" | "out" | "back"
    index: int  # the chunk this leg belongs to; -1 for the prime leg
    aid: int
    amount_groth: int
    comment: str
    outbound: bool  # treasury → split (True) or split → treasury (False)

    def fingerprint(self) -> tuple[Any, ...]:
        return (self.seq, self.kind, self.index, self.aid, self.amount_groth, self.outbound)


@dataclass(frozen=True)
class Plan:
    asset_key: str
    aid: int
    stamp: str
    # ⛔ THE METHOD IS PART OF THE PLAN, NOT OF THE INVOCATION. A plan half-made one way can
    # never be continued the other: the legs and the txid are not interchangeable, and a
    # `tx_split` resumed as a `beampay` plan would re-send its coins as transfers.
    method: str
    coins: int
    base_groth: int
    step_groth: int
    sizes: tuple[int, ...]
    fee_groth: int
    legs: tuple[Leg, ...]
    asset_needed_groth: int
    beam_needed_groth: int
    asset_budget_groth: int
    beam_budget_groth: int
    have: int | None
    target: int
    fee_budget_groth: int | None
    # ⛔ WHAT THE OPERATOR ASKED FOR, NOT WHAT WE DERIVED FROM IT. `--apply` re-derives the plan
    # from fresh reads and refuses when it differs — and re-deriving it from the SIZE the dry
    # run computed would pin the very number that is supposed to be re-computed, so a wallet
    # whose fee budget moved in between would answer "unchanged" for a plan that had changed.
    requested_coins: int | None
    requested_size: int | None

    @property
    def plan_id(self) -> str:
        return f"split|{self.asset_key}|{self.stamp}"

    @property
    def is_tx_split(self) -> bool:
        return self.method == SPLIT_METHOD_TX

    @property
    def txid(self) -> str:
        """The one transaction a `tx_split` plan will ever ask the wallet for."""
        return split_txid(self.plan_id)

    @property
    def call(self) -> dict[str, Any]:
        """The exact JSON-RPC params. ⛔ No `fee` field — see `split_min_fee`."""
        return {"coins": list(self.sizes), "asset_id": self.aid, "txId": self.txid}

    @property
    def total_fee_groth(self) -> int:
        """ONE kernel fee for a `tx_split`; one BeamPay fee per leg for the fallback."""
        return self.fee_groth if self.is_tx_split else len(self.legs) * self.fee_groth

    def fingerprint(self) -> tuple[Any, ...]:
        """Everything a plan IS, minus its identity and minus the numbers that are allowed to
        move between the dry run and `--apply` (the coin count it is trying to fix, above all).
        `--apply` re-derives the plan against fresh reads and refuses when this differs."""
        return (
            self.asset_key,
            self.aid,
            self.method,
            self.coins,
            self.base_groth,
            self.step_groth,
            self.sizes,
            self.fee_groth,
            tuple(leg.fingerprint() for leg in self.legs),
        )


def grid_of(key: str) -> int:
    grid = int(GRID_GROTH.get(key, 0))
    if grid <= 0:
        raise SplitError(f"no coin grid is defined for {key}")
    return grid


def _floor_to(value: int, grid: int) -> int:
    return (int(value) // grid) * grid


def _ceil_to(value: int, grid: int) -> int:
    return -((-int(value)) // grid) * grid


def plan_sizes(base: int, step: int, coins: int) -> tuple[int, ...]:
    """`coins` sizes ascending by one grid step from `base`. See the ⚠️ in the module docstring:
    the ascent is what stops the wallet spending a coin this very command just minted."""
    return tuple(int(base) + i * int(step) for i in range(int(coins)))


def build_legs(asset_key: str, aid: int, stamp: str, sizes: tuple[int, ...], fee: int) -> tuple[Leg, ...]:
    """The exact `/withdraw` calls, in the order they are made.

    BEAM: the `out` leg carries `size + fee` so the split address can pay for its own `back`
    leg out of what it was sent, and finishes at exactly zero.
    An asset: the fee is BEAM either way, so ONE `prime` leg carries `coins × fee` of BEAM up
    front and every `back` leg spends one fee of it — the split address finishes at exactly zero
    in both assets."""
    legs: list[Leg] = []
    prefix = f"split|{asset_key}|{stamp}"
    seq = 0
    if aid != BEAM_ASSET_ID:
        legs.append(
            Leg(
                seq=seq,
                kind="prime",
                index=-1,
                aid=BEAM_ASSET_ID,
                amount_groth=len(sizes) * fee,
                comment=f"{prefix}|prime",
                outbound=True,
            )
        )
        seq += 1
    for i, size in enumerate(sizes):
        out_amount = size + fee if aid == BEAM_ASSET_ID else size
        legs.append(
            Leg(seq=seq, kind="out", index=i, aid=aid, amount_groth=out_amount,
                comment=f"{prefix}|{i}", outbound=True)
        )
        seq += 1
        legs.append(
            Leg(seq=seq, kind="back", index=i, aid=aid, amount_groth=size,
                comment=f"{prefix}|{i}|back", outbound=False)
        )
        seq += 1
    return tuple(legs)


async def _budgets(bp: beampay.BeamPay, asset_key: str) -> tuple[int, int, dict[str, Any]]:
    """`(asset_budget, beam_budget, targets)` — what may be split, clamped DOWNWARD by the
    wallet (law 1: a position is what the ledger derives, never more than the wallet holds).

    The ledger half is the treasury's own `available` for the asset; the wallet half is what
    `wallet_spendable` says can move today. A shielded coin is deliberately not counted: a
    max-privacy output is locked for up to 72 h after it settles and a `/withdraw` from a
    max-privacy address is charged the offline fee, so splitting one is a way to make the float
    unspendable, not a way to make coins."""
    targets = await coin_targets(bp)
    treasury = beampay.treasury_address()
    beam_ledger = await bp.available_groth(treasury, BEAM_ASSET_ID)
    beam_wallet = targets[BEAM_KEY]["spendable_groth"]
    if beam_wallet is None:
        raise SplitError(
            "the wallet's coin list could not be read, so how much BEAM it can spend is unknown "
            "— and 'we cannot see' is never 'there is enough' (law 8). Nothing was planned."
        )
    beam_budget = min(int(beam_ledger), int(beam_wallet))
    if asset_key == BEAM_KEY:
        return beam_budget, beam_budget, targets
    asset = ASSETS[asset_key]
    asset_ledger = await bp.available_groth(treasury, asset.aid)
    asset_wallet = targets[asset_key]["spendable_groth"]
    if asset_wallet is None:
        raise SplitError(
            f"the wallet's spendable {asset_key} could not be read — nothing was planned"
        )
    return min(int(asset_ledger), int(asset_wallet)), beam_budget, targets


async def plan_split(
    asset_key: str,
    coins: int | None = None,
    size: int | None = None,
    stamp: str | None = None,
    bp: beampay.BeamPay | None = None,
    method: str | None = None,
) -> Plan:
    """Derive the whole plan from what the ledger and the wallet say RIGHT NOW.

    Deterministic given those two answers, which is what lets `--apply` re-derive it and refuse
    when it differs from the one the operator read."""
    key = asset_key.upper()
    if key not in (BEAM_KEY, *ASSETS):
        raise SplitError(f"{asset_key} is not an asset this treasury holds (BEAM, {', '.join(ASSETS)})")
    how = str(method or SPLIT_METHOD_TX)
    if how not in SPLIT_METHODS:
        raise SplitError(
            f"{how!r} is not a split method — it is {' or '.join(SPLIT_METHODS)} "
            f"({SPLIT_METHOD_TX} is one wallet transaction; {SPLIT_METHOD_BEAMPAY} is the "
            f"fallback that makes two BeamPay transfers per coin)"
        )
    bp = bp or beampay.beampay()
    aid = BEAM_ASSET_ID if key == BEAM_KEY else ASSETS[key].aid
    grid = grid_of(key)
    n = int(coins) if coins else target_for(key)
    if n <= 0:
        raise SplitError(f"a split of {n} coins is not a split")
    # ⛔ `tx_split` names every output in ONE transaction, so there is no sequence for the wallet
    # to poison and the sizes are EQUAL; the fallback makes them one at a time and must ascend.
    fee = split_min_fee(n, aid) if how == SPLIT_METHOD_TX else SPLIT_FEE_GROTH
    step = 0 if how == SPLIT_METHOD_TX else grid
    asset_budget, beam_budget, targets = await _budgets(bp, key)
    fee_budget_groth = targets[BEAM_KEY].get("fee_budget_groth")

    # the ascent's own cost: sizes base, base+step … base+(n-1)·step sum to n·base + step·n(n-1)/2
    ascent = step * n * (n - 1) // 2
    if size is not None:
        base = int(size)
        if base % grid:
            raise SplitError(
                f"--size {base} is not a multiple of {key}'s coin grid ({grid} groth); a size "
                f"off the grid makes coins nobody reading `beam status` can recognise"
            )
    elif key == BEAM_KEY:
        # ⛔ THE SIZE OF A FEE COIN IS WHAT A CALL COSTS, AND THAT IS DATA. `fee_budget("send")`
        # is the same number the release gate groups coins against (`coin_capacity`), so a coin
        # minted below it would not be counted a fee coin by the very gate this is fixing. The
        # configured floor is under it, never over it.
        want = max(int(settings.beam_fee_coin_groth), int(fee_budget_groth or 0))
        base = _ceil_to(want, grid)
    else:
        base = _floor_to(max(0, asset_budget - ascent) // n, grid)
    if base <= 0:
        raise SplitError(
            f"the treasury can spend {asset_budget} groth of {key}, which is not enough to make "
            f"{n} coins on a {grid}-groth grid — ask for fewer coins, or a smaller --size"
        )
    sizes = plan_sizes(base, step, n)
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    legs = () if how == SPLIT_METHOD_TX else build_legs(key, aid, stamp, sizes, fee)

    # what the treasury must be able to spend. For `tx_split` every output is made at once, so
    # the whole sum plus the one fee has to be there; for the fallback the value ROUND-TRIPS and
    # only the fees are consumed — but the largest chunk has to fit at the moment it is made,
    # after every fee before it.
    if how == SPLIT_METHOD_TX:
        asset_needed = sum(sizes) + (fee if aid == BEAM_ASSET_ID else 0)
        beam_needed = asset_needed if aid == BEAM_ASSET_ID else fee
    elif aid == BEAM_ASSET_ID:
        asset_needed = sizes[-1] + 2 * fee * n
        beam_needed = asset_needed
    else:
        asset_needed = sizes[-1]
        beam_needed = (2 * n + 1) * fee
    if asset_budget < asset_needed:
        raise SplitError(
            f"this plan needs {asset_needed} groth of {key} at its peak (the largest chunk plus "
            f"every fee before it) and the treasury can spend {asset_budget}"
        )
    if key != BEAM_KEY and beam_budget < beam_needed:
        raise SplitError(
            f"this plan needs {beam_needed} groth of BEAM for its "
            f"{'kernel fee' if how == SPLIT_METHOD_TX else str(len(legs)) + ' fees'} and the "
            f"treasury can spend {beam_budget} — split BEAM first"
        )
    return Plan(
        asset_key=key,
        aid=aid,
        stamp=stamp,
        method=how,
        coins=n,
        base_groth=base,
        step_groth=step or grid,
        sizes=sizes,
        fee_groth=fee,
        legs=legs,
        asset_needed_groth=asset_needed,
        beam_needed_groth=beam_needed,
        asset_budget_groth=asset_budget,
        beam_budget_groth=beam_budget,
        have=targets[key]["have"],
        target=target_for(key),
        fee_budget_groth=fee_budget_groth,
        requested_coins=int(coins) if coins else None,
        requested_size=int(size) if size is not None else None,
    )


def _fmt(groth: int | None) -> str:
    return "—" if groth is None else f"{groth / GROTH:.8f}"


def render_plan(
    plan: Plan,
    out: Any = print,
    split_address: str | None = None,
    self_tx: dict[str, Any] | None = None,
) -> None:
    """The whole plan, in the words an operator has to be able to check it by.

    `self_tx` is `self_tx_route`'s verdict, threaded in because the fee disclosure is a fact
    about the DEPLOYMENT and this function may not do I/O of its own."""
    key = plan.asset_key
    out(f"  asset      : {key} (Beam asset id {plan.aid})")
    out(
        f"  coins      : have {plan.have if plan.have is not None else 'UNREADABLE'} · "
        f"target {plan.target} · this plan mints {plan.coins}"
    )
    if key == BEAM_KEY:
        out(
            f"  fee coin   : a BEAM coin counts only if it can pay for a call — budget "
            f"{_fmt(plan.fee_budget_groth)} BEAM, so the smallest coin here is "
            f"{_fmt(plan.base_groth)}"
        )
    out(f"  method     : {plan.method}{'' if plan.is_tx_split else '   ⚠️ FALLBACK'}")
    if plan.is_tx_split:
        out(
            f"  sizes      : {plan.coins} × {_fmt(plan.base_groth)} — EQUAL, because one "
            f"transaction names every output at once (no sequence for the wallet to poison)"
        )
    else:
        out(
            f"  sizes      : {_fmt(plan.base_groth)} … {_fmt(plan.sizes[-1])}, ascending by "
            f"{_fmt(plan.step_groth)} (the ascent is what stops the wallet re-spending a coin "
            f"this plan just minted)"
        )
    out(
        f"  budget     : {key} spendable {_fmt(plan.asset_budget_groth)} · this plan needs "
        f"{_fmt(plan.asset_needed_groth)} at its peak"
    )
    if key != BEAM_KEY:
        out(
            f"               BEAM spendable {_fmt(plan.beam_budget_groth)} · fees need "
            f"{_fmt(plan.beam_needed_groth)}"
        )
    out(f"  treasury   : {beampay.treasury_address()}")
    out(f"  plan id    : {plan.plan_id}   (the row in `utxo_splits`)")
    out("")
    if plan.is_tx_split:
        out(
            f"  fee        : {_fmt(plan.fee_groth)} BEAM — ONE kernel fee, PREDICTED with the "
            f"wallet's own arithmetic (max(m_Kernel + m_Output × {plan.coins + 1 + (0 if plan.aid == BEAM_ASSET_ID else 1)}, "
            f"m_Default), v6_api_parse.cpp:580-589). No `fee` is sent; the wallet charges its "
            f"minimum and what it charged is read back and compared."
        )
        out(f"  txid       : {plan.txid}   (ours — the wallet REFUSES a second use of it)")
        out("")
        out(f"  POST {settings.beam_wallet_api}")
        out("  " + json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tx_split", "params": plan.call}
        ))
        out("")
        out(
            f"  net effect : {plan.coins} coin(s) of {_fmt(plan.base_groth)} {key} where the "
            f"wallet had fewer, out of inputs the WALLET picks; the remainder stays as change, "
            f"never as dust. No value leaves the wallet and no address is created."
        )
        for line in fee_accounting_note(plan.fee_groth, self_tx):
            out(line)
    else:
        out(
            f"  fees       : {len(plan.legs)} × {_fmt(plan.fee_groth)} = "
            f"{_fmt(plan.total_fee_groth)} BEAM — BeamPay's own FEE_REGULAR, verified against "
            f"the first settled transaction before the second is made"
        )
        where = split_address or (
            f"— created at --apply: one fresh REGULAR address, note "
            f"{SPLIT_NOTE}|{key}|{plan.stamp}"
        )
        out(f"  split addr : {where}")
        out("")
        out("  seq  leg    chunk  asset  amount            comment")
        for leg in plan.legs:
            name = "BEAM" if leg.aid == BEAM_ASSET_ID else key
            arrow = "treasury → split" if leg.outbound else "split → treasury"
            out(
                f"  {leg.seq:>3}  {leg.kind:<6} {('—' if leg.index < 0 else leg.index):>5}  "
                f"{name:<5}  {_fmt(leg.amount_groth):<16}  {leg.comment}   ({arrow})"
            )
        out("")
        unchanged = "" if key == BEAM_KEY else f"the treasury's {key} ends where it started; "
        out(
            f"  net effect : {unchanged}its BEAM ends {_fmt(plan.total_fee_groth)} lower — the "
            f"fees and nothing else; the split address ends at exactly 0 in every asset."
        )
        out(
            "  ⚠️ FALLBACK METHOD. Every one of those legs is a BeamPay /withdraw, and BeamPay "
            "sends the operator's group THREE Telegram notifications for each of them. It costs "
            f"{len(plan.legs)}× the fee of `--method tx_split` for the same {plan.coins} coin(s). "
            "Its one advantage: BeamPay books both sides, so it leaves the ledger EXACT."
        )


async def self_tx_route(bp: beampay.BeamPay) -> dict[str, Any]:
    """Can THIS BeamPay book a self-transaction's fee? `ready` is True, False or **None**.

    ⛔ THE THIRD ANSWER IS THE POINT. `False` is BeamPay saying "I do not have that route"
    (patch #10 is not deployed) — a real answer, told apart from every other 404 by FastAPI's
    own routing-miss `detail`. `None` is "we could not ask", which is not the same fact and must
    not become one: an unreadable query is evidence of nothing (law 8), and treating it as a no
    would run a split whose fee nobody can book while telling the operator we had checked."""
    try:
        info = await bp.self_tx_route_ready()
    except beampay.BeamPayError as e:
        return {
            "ready": None,
            "info": None,
            "reason": (
                f"BeamPay could not be asked whether it books self-transactions "
                f"({beam.redact(e)[:160]}) — that is 'we cannot see', never 'it does not'"
            ),
        }
    if info is None:
        return {
            "ready": False,
            "info": None,
            "reason": (
                "this BeamPay has no /internal/expect_self_tx route: patch #10 "
                "(patches/010-self-tx-expectation.patch) is not applied to "
                "/opt/pgasme/beampay, so nothing there can book a split's kernel fee"
            ),
        }
    if SELF_TX_KIND not in (info.get("kinds") or []):
        return {
            "ready": False,
            "info": info,
            "reason": (
                f"this BeamPay books self-transactions but not {SELF_TX_KIND!r} "
                f"(it books {', '.join(info.get('kinds') or []) or 'nothing'}) — a kind it does "
                f"not know is refused at the door, so the fee would go unbooked"
            ),
        }
    return {
        "ready": True,
        "info": info,
        "reason": (
            f"BeamPay books {SELF_TX_KIND} self-transactions "
            f"(ttl {int(info.get('ttl_sec') or 0)}s, {int(info.get('pending') or 0)} pending)"
        ),
    }


def fee_accounting_note(fee_groth: int, route: dict[str, Any] | None = None) -> list[str]:
    """What a `tx_split`'s kernel fee does to BeamPay's books — said BEFORE it is authorised.

    ⛔ THIS IS A DISCLOSURE, NOT AN APOLOGY, and which disclosure it is depends on what the
    deployment ANSWERED, never on what this file believes about it. `route` is `self_tx_route`'s
    verdict; `None` means nobody asked, and then the honest thing to print is the old, worse
    case, because that is what is true of a BeamPay we have not checked."""
    ready = (route or {}).get("ready")
    if ready is True:
        return [
            "",
            f"  ✓ THE FEE IS BOOKED, by BeamPay, to the treasury. The txid is registered with "
            f"`POST /internal/expect_self_tx` BEFORE the wallet is asked for the transaction, and "
            f"when it settles BeamPay's processor books {_fmt(fee_groth)} BEAM out of the "
            f"treasury — the fee it actually charged, not this prediction.",
            "     So the ledger falls by exactly what the wallet burned, `verify_balances` stays "
            "exact, and there is no residual for anyone to acknowledge. "
            + str((route or {}).get("reason") or ""),
        ]
    unknown = ready is None
    return [
        "",
        ("  ⚠️ WHETHER THE FEE CAN BE BOOKED IS UNKNOWN. " if unknown
         else "  ⚠️ THE FEE CANNOT BE BOOKED. ")
        + "A split is TxType::Simple with a sender and receiver "
        "the WALLET invented, so BeamPay books nothing (process_payments.py:656 →"
        ' "⚠️ Untracked transfer — NOT booked", once, naming the txid) and its adjust route'
        " refuses it (api.py:694 → 409 tx_not_booked; a read answers 400 not_a_contract_tx).",
        f"     {(route or {}).get('reason') or 'The self-tx route was not probed.'}",
        f"     BeamPay's ledger would therefore read {_fmt(fee_groth)} BEAM ABOVE the wallet, and "
        f"`verify_balances` re-pages that drift hourly until a human adds {fee_groth} groth to "
        f"asset 0 in `db.reconciliation` `ack_residual` (there is no endpoint for it).",
        "     `--apply` REFUSES rather than create that drift. Deploy BeamPay patch #10, or use "
        "`--method beampay`, which costs more in fees and notifications but books both sides.",
    ]


# ──────────────────────────────────────────────────────────────────────────── the execution


async def apply_refusals(bp: beampay.BeamPay) -> list[str]:
    """Every reason `--apply` must not run right now, all of them, in one answer.

    ⛔ **A SPLIT COMPETES FOR THE VERY INPUTS IT IS TRYING TO CREATE.** A release in flight is
    holding a coin of the asset AND a BEAM fee coin; a queued withdrawal BeamPay has not emitted
    yet is holding one it has not spent. Running a forty-transaction plan across either is how
    a payout gets `Not enough inputs` from the tool that exists to prevent exactly that."""
    reasons: list[str] = []
    if workers.paused():
        reasons.append(f"the kill switch is set ({settings.stop_file})")
    busy = await db().payout_requests.count_documents({"status": {"$in": list(BUSY_PAYOUTS)}})
    if busy:
        reasons.append(
            f"{busy} payout(s) are {' / '.join(BUSY_PAYOUTS)} — each is holding a coin of its "
            f"asset and a BEAM coin for its fee"
        )
    deps = await db().deposits.count_documents({"treasury": {"$in": list(BUSY_DEPOSITS)}})
    if deps:
        reasons.append(
            f"{deps} deposit(s) are {' / '.join(BUSY_DEPOSITS)} — a claim and a shield each "
            f"spend a BEAM coin"
        )
    try:
        st = await bp.wallet_status()
    except beampay.BeamPayError as e:
        reasons.append(f"the wallet's status could not be read ({beam.redact(e)[:160]})")
    else:
        if st.get("is_in_sync") is not True:
            reasons.append(
                f"the wallet is NOT in sync (height {st.get('current_height')}) — a coin count "
                f"read off a wallet that is still catching up is not a coin count"
            )
    try:
        rows = await bp.transactions(count=beampay.PAGE)
    except beampay.BeamPayError as e:
        reasons.append(
            f"BeamPay's transaction history could not be read ({beam.redact(e)[:160]}) — an "
            f"unreadable history is not evidence that nothing is pending (law 8)"
        )
    else:
        flying = [
            str(r.get("txId") or "")[:12]
            for r in rows
            if int(r.get("status", -1)) in beam.TX_IN_FLIGHT
        ]
        if flying:
            reasons.append(
                f"{len(flying)} wallet transaction(s) are still in flight "
                f"({', '.join(flying[:5])}) — every one of them is holding an input"
            )
    return reasons


async def split_address_for(bp: beampay.BeamPay, plan: Plan) -> str:
    """The one address this plan round-trips through — created ONCE and recorded on the plan's
    row before it is used, so a re-run reuses it rather than stranding value on an address the
    next run has never heard of.

    ⛔ REGULAR, and the SHAPE is checked. A max-privacy token would be charged the offline fee
    and its outputs are locked for up to 72 h: a split through one makes the treasury's money
    unspendable, which is the opposite of the job."""
    row = await db().utxo_splits.find_one({"_id": plan.plan_id})
    if row and row.get("split_address"):
        return str(row["split_address"])
    addr = await bp.create_wallet(f"{SPLIT_NOTE}|{plan.asset_key}|{plan.stamp}", "regular")
    if not beampay.looks_like_regular_address(addr):
        raise SplitError(
            f"/create_wallet answered {addr[:18]}…, which is not the shape of a regular Beam "
            f"address — a split through a max-privacy address locks the treasury's own money"
        )
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id}, {"$set": {"split_address": addr}}, upsert=True
    )
    return addr


async def _plan_row(
    plan: Plan, split_addr: str, treasury: str, before: dict[str, int], coins_before: int | None
) -> dict[str, Any]:
    """The plan's own row, and the answer is the STORED one.

    ⛔ **`before` IS WRITTEN ONCE, AT THE FIRST RUN.** The ledger check at the end is
    `treasury == before − Σ fees`, and every fee of every leg counts — including the ones a
    PREVIOUS run of this plan paid. Re-stamping `before` on a resume would compare this run's
    starting balance against the whole plan's fees and cry drift at its own bookkeeping."""
    now = time.time()
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id},
        {
            "$set": {
                "kind": "plan",
                # ⛔ THE METHOD IS ON THE ROW, because a resume must continue the way the plan
                # started: `cmd_split` reads it back and ignores whatever --method was typed.
                "method": plan.method,
                "asset": plan.asset_key,
                "aid": plan.aid,
                "stamp": plan.stamp,
                "coins": plan.coins,
                "sizes": list(plan.sizes),
                "fee_groth": plan.fee_groth,
                "legs": len(plan.legs),
                "split_address": split_addr,
                "treasury": treasury,
                "status": "running",
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    # ⛔ …AND `before` IS SET ONLY WHILE IT IS ABSENT, not with `$setOnInsert`. The row already
    # exists by now: `split_address_for` upserted the address into it BEFORE this call (the
    # address must be recorded before it is used, T40b F10), so `$setOnInsert` would never fire
    # at all and every run would answer with its own opening balance — which on a resume is the
    # balance AFTER the fees the first run paid, and the ledger check would report drift at its
    # own bookkeeping. A guarded `$set` writes it exactly once, on whichever run gets there
    # first.
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id, "before": {"$exists": False}},
        {"$set": {"before": before, "coins_before": coins_before}},
    )
    return await db().utxo_splits.find_one({"_id": plan.plan_id}) or {}


async def _leg_row(plan: Plan, leg: Leg, **fields: Any) -> dict[str, Any]:
    """The leg's own row, `_id` = the comment. ⛔ IT IS WRITTEN BEFORE THE CALL: `/withdraw` is
    not idempotent and answers no txid, so the only thing that stops a second call is the record
    that a first one was made — and keying the row on the comment makes a duplicate impossible
    at the database rather than merely unlikely in the code."""
    now = time.time()
    await db().utxo_splits.update_one(
        {"_id": leg.comment},
        {
            "$set": {"updated_at": now, **fields},
            "$setOnInsert": {
                "kind": "leg",
                "plan_id": plan.plan_id,
                "asset": plan.asset_key,
                "aid": leg.aid,
                "seq": leg.seq,
                "leg": leg.kind,
                "chunk": leg.index,
                "amount_groth": leg.amount_groth,
                "comment": leg.comment,
                "created_at": now,
            },
        },
        upsert=True,
    )
    return await db().utxo_splits.find_one({"_id": leg.comment}) or {}


async def _one(bp: beampay.BeamPay, addr: str, comment: str, since: float) -> dict[str, Any] | None:
    """The ONE transaction carrying this comment on this address, or None.

    ⛔ TWO IS NEVER RESOLVED BY A MACHINE. `find_txs_by_comments` answers a LIST precisely so a
    double send is visible; two rows here means the treasury sent this chunk twice and nothing
    in this module will send on top of that."""
    found = await bp.find_txs_by_comments(addr, [comment], since)
    rows = list(found.get(comment) or [])
    if len(rows) > 1:
        raise SplitError(
            f"{len(rows)} transactions carry the comment {comment!r} "
            f"({', '.join(str(r.get('txId'))[:12] for r in rows)}) — this chunk was sent twice "
            f"and that is a human's to resolve; nothing further was sent"
        )
    return rows[0] if rows else None


async def _settled(
    bp: beampay.BeamPay, addr: str, comment: str, since: float, out: Any
) -> dict[str, Any]:
    """Wait for the transaction carrying `comment` to SETTLE, and answer it.

    Raises rather than returning a partial verdict: a transaction that is dead, or that never
    appeared, is a stop — never a retry, because `/withdraw` is not idempotent."""
    deadline = time.time() + POLL_TIMEOUT_S
    while True:
        tx = await _one(bp, addr, comment, since)
        if tx is not None:
            status = int(tx.get("status", -1))
            if status in beam.TX_SETTLED:
                return tx
            if status in beam.TX_DEAD:
                raise SplitError(
                    f"the transaction for {comment!r} is "
                    f"{tx.get('status_string') or status} (tx {str(tx.get('txId'))[:12]}) — "
                    f"nothing is auto-retried, because /withdraw is not idempotent"
                )
        if time.time() >= deadline:
            raise SplitError(
                f"no settled transaction carrying {comment!r} appeared within "
                f"{int(POLL_TIMEOUT_S)}s. It may still be in flight: this is NOT re-sent, and a "
                f"re-run of this command will find it by its comment and carry on"
            )
        if workers.paused():
            raise SplitError(
                f"the kill switch was set while {comment!r} was in flight ({settings.stop_file})"
                f" — the plan stops here; the transaction itself is unaffected"
            )
        out(f"      … waiting for {comment} to settle")
        await asyncio.sleep(POLL_INTERVAL_S)


async def _coins_now(key: str, bp: beampay.BeamPay) -> int | None:
    """The coin count, re-read from the wallet. ⛔ The per-pass cache is cleared first: a count
    answered from a cache taken before this command sent anything is not a measurement."""
    payouts.reset_process_state()
    targets = await coin_targets(bp)
    have = targets.get(key, {}).get("have")
    return None if have is None else int(have)


# ──────────────────────────────────────────────────── the split the WALLET makes (tx_split)


async def _snapshot(key: str, bp: beampay.BeamPay) -> dict[str, int | None]:
    """Coins, and what the WALLET can spend, from ONE read of the one reader (law 8/9).

    ⛔ The per-pass cache is cleared first: a count answered from a cache taken before this
    command did anything is not a measurement."""
    payouts.reset_process_state()
    targets = await coin_targets(bp)
    have = targets.get(key, {}).get("have")
    return {
        "coins": None if have is None else int(have),
        "wallet_asset": targets.get(key, {}).get("spendable_groth"),
        "wallet_beam": targets[BEAM_KEY].get("spendable_groth"),
    }


async def _tx_row(plan: Plan, **fields: Any) -> dict[str, Any]:
    """The plan's ONE row — written BEFORE the irreversible call and again after it."""
    now = time.time()
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id},
        {
            "$set": {
                "kind": "plan",
                "method": plan.method,
                "asset": plan.asset_key,
                "aid": plan.aid,
                "stamp": plan.stamp,
                "coins": plan.coins,
                "sizes": list(plan.sizes),
                "legs": 0,
                "txid": plan.txid,
                "call": plan.call,
                "fee_predicted_groth": plan.fee_groth,
                "treasury": beampay.treasury_address(),
                "updated_at": now,
                **fields,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return await db().utxo_splits.find_one({"_id": plan.plan_id}) or {}


async def _settled_tx(
    bp: beampay.BeamPay, txid: str, since: float, out: Any
) -> dict[str, Any]:
    """Wait for OUR transaction to settle, matched on the id we chose. Raises, never guesses."""
    deadline = time.time() + POLL_TIMEOUT_S
    while True:
        tx = await bp.find_tx_by_id(txid, since)
        if tx is not None:
            status = int(tx.get("status", -1))
            if status in beam.TX_SETTLED:
                return tx
            if status in beam.TX_DEAD:
                raise SplitError(
                    f"the split transaction {txid[:12]}… is "
                    f"{tx.get('status_string') or status} — it made no coins and it is NOT "
                    f"re-issued under this plan: the wallet still holds that txId and would "
                    f"refuse it. Plan a new one (a new stamp) once you know why it failed"
                )
        if time.time() >= deadline:
            raise SplitError(
                f"no settled transaction {txid[:12]}… appeared within {int(POLL_TIMEOUT_S)}s. "
                f"It may still be in flight: nothing is re-issued, and a re-run of this command "
                f"finds it by that same txid and carries on"
            )
        if workers.paused():
            raise SplitError(
                f"the kill switch was set while {txid[:12]}… was in flight "
                f"({settings.stop_file}) — the plan stops here; the transaction is unaffected"
            )
        out(f"      … waiting for {txid[:12]}… to settle")
        await asyncio.sleep(POLL_INTERVAL_S)


async def _register_split_expectation(
    bp: beampay.BeamPay, plan: Plan, txid: str, out: Any, late: bool = False
) -> dict[str, Any]:
    """Tell BeamPay whose fee this split's is — BEFORE the wallet is asked to make it.

    ⛔ ORDER IS THE WHOLE MECHANISM. BeamPay's processor consumes the registration inside the
    branch that would otherwise page "Untracked transfer — NOT booked", and it claims the
    transaction's `success` flag before that branch runs and never clears it; so a registration
    that arrives after settlement is refused (`409 tx_already_booked`) and can never be honoured.
    Registering first also means a crash between the two leaves a harmless pending row that
    expires on its own, whereas the reverse would leave a fee nobody can attribute.

    A refusal here STOPS the plan. The gate in `run_tx_split` has already told the operator this
    fee would be booked; sending the transaction anyway would make that sentence false.

    ⛔ `late=True` INVERTS THAT, because the transaction already exists. BeamPay accepts a
    registration for a txid it has already seen — a self-tx fee that was never booked is booked
    late as readily as early, since booking it is the FIRST move either way — so a resume whose
    split was made before any of this existed can still claim its fee, and its `sweep_self_tx_
    expectations` pass books it. There is nothing left to stop: refusing to register a
    transaction that has already been made cannot un-make it, so a refusal is recorded and the
    accounting below reports whatever is true."""
    treasury = beampay.treasury_address()
    ref = plan.plan_id[:128]
    try:
        res = await bp.expect_self_tx(
            txid,
            treasury,
            ref,
            kind=SELF_TX_KIND,
            asset_id=plan.aid,
            expected_fee_groth=plan.fee_groth,
        )
    except beampay.BeamPayError as e:
        # ⛔ EVERY DECISION PATH WRITES A ROW (law 12), including this one — a refusal that only
        # printed would be unalertable, and this one is the reason nothing was sent.
        await db().utxo_splits.update_one(
            {"_id": plan.plan_id},
            {"$set": {"self_tx": {"registered": False, "at": time.time(), "txid": txid,
                                  "trade_ref": ref, "late": late,
                                  "error": beam.redact(e)[:200]}}},
        )
        if late:
            out(
                f"    ⚠️ a late fee registration for {txid[:12]}… was refused "
                f"({beam.redact(e)[:120]}) — the transaction is already made, so nothing stops "
                f"here; the fee accounting below reports what is actually true of it"
            )
            return {}
        raise SplitError(
            f"BeamPay refused to register the fee expectation for {txid[:12]}… "
            f"({beam.redact(e)[:160]}). NOTHING WAS SENT: the wallet was never asked for the "
            f"transaction, so there is no split, no fee and nothing to unwind"
        ) from None
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id},
        {"$set": {"self_tx": {"registered": True, "at": time.time(), "txid": txid,
                              "trade_ref": ref, "address": treasury,
                              "kind": SELF_TX_KIND, "late": late,
                              "expected_fee_groth": plan.fee_groth,
                              "replayed": bool(res.get("replayed"))}}},
    )
    out(
        f"    ✓ fee registered{' LATE' if late else ''}: POST /internal/expect_self_tx "
        f"{txid[:12]}… → treasury{' (replayed)' if res.get('replayed') else ''}; BeamPay books "
        f"it {'on its next pass' if late else 'when it settles'}"
    )
    return res


async def _await_self_tx_booking(
    bp: beampay.BeamPay, txid: str, fee: int, out: Any
) -> dict[str, Any] | None:
    """Wait, bounded, for BeamPay to book the fee against our registration. Or say what it did.

    `None` means **this route does not apply to this transaction** — there is no registration
    for it (a plan resumed from before the fee was ever registered), or this BeamPay has no
    self-tx route at all. The caller then falls through to the older accounting exactly as it
    did before patch #10 existed.

    ⛔ SETTLEMENT AND BOOKING ARE TWO EVENTS. BeamPay's processor writes `db.txs` on one sweep
    and books on a later one, once the transaction has its confirmations, so a single read would
    report "not booked" about a booking that is simply still coming — §BOOKED-IS-LANDED read
    backwards. A timeout here is reported as "we could not see it", never as "it did not
    happen": nothing about the money changes either way, only what we are entitled to say."""
    deadline = time.time() + BOOK_TIMEOUT_S
    last = "BeamPay has not booked it yet"
    waited = False
    while True:
        doc: dict[str, Any] | None | bool
        try:
            doc = await bp.self_tx(txid)
        except beampay.BeamPayError as e:
            if getattr(e, "status", None) == 404:
                # FastAPI's routing miss: this deployment has no /internal/self_tx at all.
                return None
            last = f"BeamPay could not be read: {beam.redact(e)[:160]}"
            doc = False
        if doc is None:
            return None
        if doc is not False:
            status = str(doc.get("status") or "")
            if status == "consumed":
                booked = doc.get("booked") or {}
                got = -int(booked.get(str(BEAM_ASSET_ID), 0) or 0)
                acct: dict[str, Any] = {
                    "route": "self_tx",
                    "posted": True,
                    "booked_groth": got,
                    "drift_groth": fee - got,
                    "ack_residual_groth": max(0, fee - got),
                    "address": doc.get("address"),
                    "trade_ref": doc.get("trade_ref"),
                    "reason": (
                        f"booked: BeamPay debited the treasury {got} groth for the kernel fee "
                        f"of this split, under the expectation registered before the call."
                    ),
                }
                if got != fee:
                    # ⛔ TWO READS OF ONE FACT DISAGREEING. Both numbers are BeamPay's own
                    # `db.txs.fee`; a difference is not ours to reconcile and not ours to hide.
                    acct["reason"] = (
                        f"BOOKED A DIFFERENT NUMBER: BeamPay booked {got} groth and the settled "
                        f"transaction reports a fee of {fee}. The ledger moved by what BeamPay "
                        f"booked; the {fee - got} groth difference is unaccounted for."
                    )
                return acct
            if status in ("abandoned", "expired"):
                return {
                    "route": "none",
                    "drift_groth": fee,
                    "ack_residual_groth": fee,
                    "reason": (
                        f"BeamPay did not book it: the registration is {status} — "
                        f"{doc.get('error') or 'no reason recorded'}. Nothing was posted."
                    ),
                }
            last = f"BeamPay holds the registration and its status is {status!r}"
        if time.time() >= deadline:
            return {
                "route": "unreadable",
                "drift_groth": fee,
                "ack_residual_groth": fee,
                "reason": (
                    f"{last} after {int(BOOK_TIMEOUT_S)}s. That is 'we could not see it booked', "
                    f"NOT 'it was not booked' — re-read "
                    f"`GET /internal/self_tx/{txid}` before acknowledging any residual."
                ),
            }
        if workers.paused():
            return {
                "route": "unreadable",
                "drift_groth": fee,
                "ack_residual_groth": fee,
                "reason": (
                    f"the kill switch was set while waiting for the booking "
                    f"({settings.stop_file}); {last}"
                ),
            }
        if not waited:
            out("      … waiting for BeamPay to book the kernel fee")
            waited = True
        await asyncio.sleep(BOOK_INTERVAL_S)


async def book_split_fee(
    bp: beampay.BeamPay, plan_id: str, txid: str, fee_groth: int, out: Any = print
) -> dict[str, Any]:
    """Account for the kernel fee a settled `tx_split` burned — or say why nothing can.

    ⛔ **THE PROBER CALLS THE WAY THE CALLER CALLS (law 8).** `/internal/ledger/adjust` gates on
    `gate_doc["type"] == 12` (api.py:694) and `GET /internal/contract_tx/{txid}` answers
    `400 not_a_contract_tx` off the SAME field (api.py:997), so asking the read route is asking
    the write route's own question — without leaving a refused adjustment behind on an id that
    can then never be reused. On this deployment the answer is always "no" (a split is
    TxType::Simple); the adjust path is written and exercised anyway, because the decision must
    be made by BeamPay's answer and not by our belief about it.

    ⛔ Every path writes a row (law 12). This never raises into the money path: the split has
    already settled by the time it is called, and a fee that could not be booked is a sentence,
    not a failed split."""
    row = await db().utxo_splits.find_one({"_id": plan_id}) or {}
    prior = row.get("fee_accounting") or {}
    if prior.get("posted") and prior.get("route") in ("self_tx", "ledger_adjust"):
        # ⛔ `prior_route` TRAVELS, because the report downstream has to know how much of the
        # treasury's BEAM this plan legitimately took off the ledger — and on a resumed plan
        # that number is in the booking the FIRST run made, not in this call. Collapsing it to
        # "already" and losing the amount made the resume flag its own correct bookkeeping as
        # somebody else moving BEAM.
        return {**prior, "route": "already", "prior_route": prior.get("route")}

    fee = int(fee_groth)
    adjust_id = f"pgasme:split:{txid}"
    acct: dict[str, Any] = {
        "route": "none",  # …meaning "no route can book it", which is a verdict, not an absence
        "adjust_id": adjust_id,
        "drift_groth": fee,
        "ack_residual_groth": fee,
        "at": time.time(),
    }

    # ── the route this command now takes: the expectation registered before the call ────────
    # ⛔ FIRST, AND ON ITS OWN EVIDENCE. What BeamPay actually did is on ITS row, not in our
    # POST's reply — the reply can be lost while the booking still happens — so this reads the
    # row back. `None` is "no registration for this txid, or no such route here", and only then
    # does the older accounting below get a turn: on a plan resumed from before patch #10, and
    # on a BeamPay that has not had it applied.
    self_booked = await _await_self_tx_booking(bp, txid, fee, out)
    if self_booked is not None:
        acct.update(self_booked)
        await db().utxo_splits.update_one({"_id": plan_id}, {"$set": {"fee_accounting": acct}})
        if acct["route"] == "self_tx" and not acct.get("drift_groth"):
            out(
                f"  ✓ fee booked  : BeamPay debited the treasury "
                f"{_fmt(acct.get('booked_groth'))} BEAM under `{acct.get('trade_ref')}` — the "
                f"ledger falls by exactly what the wallet burned, so nothing drifted"
            )
        else:
            out(f"  ⚠️ fee accounting ({acct['route']}): {acct['reason']}")
            out(
                f"     BeamPay's ledger reads {_fmt(acct['drift_groth'])} BEAM above the wallet "
                f"until this is resolved and `verify_balances` re-pages it every pass: add "
                f"{acct['ack_residual_groth']} groth to asset 0 in `db.reconciliation` "
                f"`ack_residual` to acknowledge it — but re-read "
                f"`GET /internal/self_tx/{txid}` first, because an unreadable answer is not a "
                f"booking that did not happen. Nothing here edits a balance to make its own "
                f"arithmetic come out."
            )
        return acct

    tx: dict[str, Any] | None = None
    try:
        tx = await bp.contract_tx(txid)
    except beampay.BeamPayError as e:
        detail = str(getattr(e, "detail", "") or "") + " " + str(e)
        if "not_a_contract_tx" in detail:
            acct["reason"] = (
                f"BeamPay answers 400 not_a_contract_tx for {txid[:12]}…: a tx_split is "
                f"TxType::Simple, and /internal/ledger/adjust gates on type == 12 "
                f"(api.py:694 → 409 tx_not_booked). Nothing was posted."
            )
        else:
            acct["route"] = "unreadable"
            acct["reason"] = (
                f"BeamPay could not be read ({beam.redact(e)[:160]}) — that is 'we cannot see', "
                f"never 'nothing to do' (law 8). Nothing was posted."
            )
    else:
        if tx is None:
            acct["route"] = "unreadable"
            acct["reason"] = (
                "BeamPay's processor has not recorded this transaction as a contract tx "
                "(tx_not_found) — nothing was posted."
            )
        elif tx.get("attributed_to"):
            acct["reason"] = (
                f"already attributed directly to {tx.get('attributed_to')} — it never credited "
                f"or debited the house, so there is nothing to repair "
                f"(409 already_attributed_directly, permanent)."
            )
        elif not (tx.get("booked") is True and int(tx.get("status", -1)) == beam.TX_COMPLETED):
            acct["route"] = "unreadable"
            acct["reason"] = (
                "BeamPay has not booked this transaction yet (booked AND status == 3 is "
                "settlement) — nothing was posted."
            )
        else:
            flow = beam.house_flow(tx, BEAM_ASSET_ID)
            if not flow:
                acct["reason"] = "this transaction moved no BEAM at all (409 no_flow_for_asset)."
            else:
                treasury = beampay.treasury_address()
                frm, to = (
                    (treasury, beam.HOUSE_ACCOUNT_ID)
                    if flow > 0
                    else (beam.HOUSE_ACCOUNT_ID, treasury)
                )
                body = {
                    "adjust_id": adjust_id,
                    "asset_id": BEAM_ASSET_ID,
                    "from_address": frm,
                    "to_address": to,
                    "amount_groth": abs(flow),
                    "reason": (
                        f"kernel fee of tx_split {txid}: the wallet cut its own coin and no "
                        f"BeamPay address is on either side of the transaction"
                    )[:256],
                    "trade_ref": plan_id[:128],
                    "after_tx": txid,
                }
                if workers.paused():
                    acct["route"] = "unreadable"
                    acct["reason"] = (
                        f"the kill switch is set ({settings.stop_file}) — nothing was posted"
                    )
                else:
                    try:
                        res = await bp.call(
                            "POST", beam.LEDGER_ADJUST_PATH, body=body, internal=True
                        )
                    except beampay.BeamPayError as e:
                        acct["route"] = "refused"
                        acct["reason"] = f"BeamPay refused the adjustment: {beam.redact(e)[:200]}"
                    else:
                        acct.update(
                            route="ledger_adjust",
                            posted=True,
                            drift_groth=0,
                            ack_residual_groth=0,
                            body=body,
                            result=str(res)[:200],
                            reason="booked: the fee leg was moved off __house__ (zero-sum).",
                        )
    await db().utxo_splits.update_one({"_id": plan_id}, {"$set": {"fee_accounting": acct}})
    if acct["route"] == "ledger_adjust":
        out(f"  ✓ fee booked  : {beam.LEDGER_ADJUST_PATH} {adjust_id} — {acct['reason']}")
    else:
        # ⛔ ONE LINE, NOT THE WHOLE SERMON AGAIN. `render_plan` already printed the full
        # disclosure before the operator authorised this; repeating it here is how the sentence
        # that matters — the ack figure — gets skimmed past.
        out(f"  ⚠️ fee NOT booked ({acct['route']}): {acct['reason']}")
        out(
            f"     BeamPay's ledger now reads {_fmt(acct['drift_groth'])} BEAM above the wallet "
            f"and `verify_balances` re-pages it hourly: add {acct['ack_residual_groth']} groth to "
            f"asset 0 in `db.reconciliation` `ack_residual` to acknowledge it. Nothing here "
            f"edits a balance to make its own arithmetic come out."
        )
    return acct


async def run_tx_split(plan: Plan, out: Any = print, bp: beampay.BeamPay | None = None) -> int:
    """ONE transaction. Returns a process exit code.

    Row before the call and after it; the txid is ours and the wallet refuses a second use of
    it; the coin count is re-measured from `get_utxo` afterwards and the verdict is that
    number."""
    bp = bp or beampay.beampay()
    treasury = beampay.treasury_address()
    key, aid = plan.asset_key, plan.aid

    reasons = await apply_refusals(bp)
    if reasons:
        out("REFUSED — nothing was sent:")
        for r in reasons:
            out(f"  ⛔ {r}")
        return 1

    # ⛔ A SPLIT WHOSE FEE NOBODY CAN BOOK IS A DRIFT THIS COMMAND CHOOSES TO CREATE, and the
    # operator has forbidden both the manual `ack_residual` that clears it and the hourly page
    # that nags about it until then. So the route is a GATE, not a footnote: if BeamPay cannot
    # book a self-transaction, or cannot be asked, nothing is sent. The way out is not a flag —
    # it is `--method beampay`, which books both sides through BeamPay itself at a higher cost
    # in fees and notifications, and which this refusal names.
    route = await self_tx_route(bp)
    if route["ready"] is not True:
        out("REFUSED — nothing was sent:")
        out(f"  ⛔ {route['reason']}")
        out(
            "     The kernel fee would then be burned on chain and absent from the ledger, and "
            "`verify_balances` re-pages that gap every pass until a human writes an "
            "`ack_residual` into BeamPay's Mongo by hand."
        )
        out(
            "     Apply BeamPay patch #10 (`patches/010-self-tx-expectation.patch` in "
            "/opt/pgasme/beampay, then restart pgasme-beampay-api and "
            "pgasme-beampay-processor), or re-run this with `--method beampay`."
        )
        return 1

    # ⛔ RE-DERIVED FROM FRESH READS, exactly as the fallback does: the operator approved
    # NUMBERS, and a wallet that moved in between is a different plan.
    payouts.reset_process_state()
    try:
        fresh = await plan_split(
            key, plan.requested_coins, plan.requested_size, stamp=plan.stamp, bp=bp,
            method=plan.method,
        )
    except (SplitError, beampay.BeamPayError) as e:
        out(f"REFUSED — the plan cannot be re-derived at apply time: {beam.redact(e)}")
        out("  Nothing was sent.")
        return 1
    if fresh.fingerprint() != plan.fingerprint():
        out(
            "REFUSED — the plan re-derived at apply time is NOT the plan that was printed (the "
            "ledger or the wallet moved in between). Nothing was sent; run the dry run again."
        )
        return 1

    txid = plan.txid
    snap = await _snapshot(key, bp)
    before = {
        "beam_ledger": await bp.available_groth(treasury, BEAM_ASSET_ID),
        "asset_ledger": await bp.available_groth(treasury, aid),
        "beam_wallet": snap["wallet_beam"],
        "asset_wallet": snap["wallet_asset"],
    }
    row = await _tx_row(plan, status="planned")
    # ⛔ `before` and `coins_before` are written ONCE, on whichever run gets there first: a
    # resume that re-stamped them would measure this run's opening balance against the whole
    # plan's fee and cry drift at its own bookkeeping.
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id, "before": {"$exists": False}},
        {"$set": {"before": before, "coins_before": snap["coins"]}},
    )
    row = await db().utxo_splits.find_one({"_id": plan.plan_id}) or row
    before = dict(row.get("before") or before)
    coins_before = row.get("coins_before", snap["coins"])
    out(f"  txid          : {txid}")
    out(f"  coins before  : {coins_before if coins_before is not None else 'UNREADABLE'}")

    since = min(time.time(), float(row.get("created_at") or time.time())) - LOOKBACK_S
    try:
        tx = await bp.find_tx_by_id(txid, since)
        if tx is not None:
            out(f"    ↺ {txid[:12]}… already made — not re-issued")
            # …and CLAIM ITS FEE ANYWAY, if nobody has. A plan whose split was made before this
            # route existed left a kernel fee burned on chain and absent from the ledger, which
            # `verify_balances` re-pages every pass; the registration is accepted late and
            # BeamPay's sweep books it. Idempotent: an existing registration replays, and one
            # whose flow already has an owner is refused without stopping anything.
            await _register_split_expectation(bp, plan, txid, out, late=True)
        else:
            if workers.paused():
                raise SplitError(f"the kill switch is set ({settings.stop_file})")
            await _tx_row(plan, status="calling", called_at=time.time())
            # ⛔ THE MARKER GOES IN BEFORE THE IRREVERSIBLE CALL — the same discipline as
            # `expect_contract_tx` on every bridge invocation, and here it is also the only
            # order that WORKS: BeamPay refuses a registration for a transaction it has already
            # finished with (`409 tx_already_booked`), because the branch that would consume it
            # has already run. A registration that cannot be made STOPS the plan, because the
            # gate above promised the operator this fee would be booked.
            await _register_split_expectation(bp, plan, txid, out)
            try:
                await beam.wallet().split(list(plan.sizes), aid, txid)
            except beam.Halted:
                await _tx_row(plan, status="halted")
                raise SplitError(
                    "the kill switch was set inside the mover — the split was NOT issued"
                ) from None
            except beam.BeamError as e:
                if "already exists" in str(e):
                    # the WALLET's own idempotency answered: it holds this txId already, and
                    # BeamPay has simply not recorded it yet. Nothing is re-issued.
                    await _tx_row(plan, status="sent", note="wallet already held this txId")
                    out(f"    ↺ the wallet already holds {txid[:12]}… — not re-issued")
                else:
                    await _tx_row(plan, status="lost", error=beam.redact(e)[:200])
                    raise SplitError(
                        f"the answer to tx_split was lost ({beam.redact(e)[:160]}). The wallet "
                        f"may hold it, so it is NOT re-issued by this run: the txid is "
                        f"deterministic ({txid[:12]}…), so a re-run finds it — in BeamPay's "
                        f"history, or in the wallet's own refusal to take it twice"
                    ) from None
            else:
                await _tx_row(plan, status="sent", sent_at=time.time())
                out(f"    → tx_split issued: {plan.coins} × {_fmt(plan.base_groth)} {key}")

        tx = await _settled_tx(bp, txid, since, out)
        fee = int(tx.get("fee") or 0)
        await _tx_row(
            plan, status="settled", fee_groth=fee, kernel=tx.get("kernel"),
            settled_at=time.time(),
        )
        if fee != plan.fee_groth:
            # ⛔ REPORTED, NEVER ABSORBED. There is no later leg to protect here, but the
            # arithmetic below and the drift both use the CHARGED fee, never the prediction.
            out(
                f"  ⚠️ the wallet charged {_fmt(fee)} BEAM and this plan predicted "
                f"{_fmt(plan.fee_groth)} — the prediction is `split_min_fee`, ported from "
                f"v6_api_parse.cpp:580-589; everything below uses what was CHARGED"
            )
    except (SplitError, beampay.BeamPayError, beam.BeamError) as e:
        # ⛔ EVERY WAY THIS STOPS WRITES A ROW (law 12), including the ones that are somebody
        # else's exception type: an unreadable BeamPay mid-plan is a decision with a reason, and
        # a decision path that only prints is unalertable. The plan stays OPEN, so a re-run
        # resumes it — the txid is deterministic and the wallet refuses a second use of it.
        # …and it keeps WHERE it stopped. `lost` (the wallet may hold the transaction) and
        # `settled` (it does, and the read after it failed) are different problems for the human
        # who picks this up, and overwriting both with `halted` would throw that away.
        was = (await db().utxo_splits.find_one({"_id": plan.plan_id}) or {}).get("status")
        await db().utxo_splits.update_one(
            {"_id": plan.plan_id},
            {"$set": {"status": "halted", "halted_from": was, "halted_at": time.time(),
                      "reason": beam.redact(e)[:400]}},
        )
        out("")
        out(f"STOPPED: {beam.redact(e)}")
        return 1

    acct = await book_split_fee(bp, plan.plan_id, txid, fee, out)
    out("")
    rc = await _report_tx_split(plan, bp, treasury, before, coins_before, fee, acct, out)
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id},
        {"$set": {"status": "done", "done_at": time.time()}},
    )
    return rc


async def _report_tx_split(
    plan: Plan,
    bp: beampay.BeamPay,
    treasury: str,
    before: dict[str, Any],
    coins_before: int | None,
    fee: int,
    acct: dict[str, Any],
    out: Any,
) -> int:
    """What the transaction actually did, measured — never asserted.

    ⛔ THE LEDGER ASSERTION IS THE ONE THAT ROUTE CHANGED. Before patch #10 the only correct
    statement was "the ledger did not move and the wallet is down by the fee" — the gap between
    them WAS the drift. With the fee booked, both sides move together and an unchanged ledger is
    now the failure. So this checks against what was actually booked (`acct`), not against a
    constant: a guard that keeps asserting yesterday's invariant passes on today's bug."""
    key, aid = plan.asset_key, plan.aid
    rc = 0
    after = await _snapshot(key, bp)
    coins_after = after["coins"]
    beam_ledger = await bp.available_groth(treasury, BEAM_ASSET_ID)
    asset_ledger = await bp.available_groth(treasury, aid)
    await db().utxo_splits.update_one(
        {"_id": plan.plan_id},
        {"$set": {"coins_after": coins_after, "after": {
            "beam_ledger": beam_ledger, "asset_ledger": asset_ledger,
            "beam_wallet": after["wallet_beam"], "asset_wallet": after["wallet_asset"],
        }}},
    )
    out("RESULT")
    out(
        f"  coins ({key:<4}): {coins_before if coins_before is not None else '—'} → "
        f"{coins_after if coins_after is not None else 'UNREADABLE'} (target {plan.target})"
    )
    if coins_after is None:
        out("  ⚠️ the wallet's coin list could not be read at the end, so whether the target was "
            "reached is UNKNOWN — that is not the same as 'no' and not the same as 'yes'")
        rc = 1
    elif coins_after < plan.target:
        out(
            f"  ⚠️ SHORT by {plan.target - coins_after} coin(s). The wallet holds "
            f"{coins_after} spendable {key} coin(s) against a target of {plan.target}: run this "
            f"command again for the rest, or lower PGAS_"
            f"{'BEAM_FEE_COINS' if key == BEAM_KEY else 'UTXO_TARGET_COINS'} to what the "
            f"treasury can actually carry"
        )
        rc = 1
    else:
        out(f"  ✓ the target of {plan.target} spendable {key} coin(s) is met")
    out(f"  fee charged   : {_fmt(fee)} BEAM (predicted {_fmt(plan.fee_groth)})")
    # ⛔ BOTH SIDES, AND THE GAP BETWEEN THEM IS THE POINT. A split moves no value between
    # BeamPay addresses, so the only thing the LEDGER may move by is the kernel fee — and only
    # if something booked it. The WALLET is down by that fee either way. Both are measured, and
    # a difference is a sentence, not a correction.
    # `already` is this plan's own earlier booking, read back on a resume: what it booked is
    # what the ledger legitimately moved by, so the route it booked BY is what decides here.
    route = acct.get("prior_route") or acct.get("route")
    if route == "self_tx":
        booked_out = int(acct.get("booked_groth") or 0)
    elif route == "ledger_adjust" and acct.get("posted"):
        booked_out = fee
    else:
        booked_out = 0
    exp_ledger = int(before.get("beam_ledger") or 0) - booked_out
    if beam_ledger == exp_ledger and booked_out:
        out(
            f"  ✓ treasury BEAM ledger {_fmt(before.get('beam_ledger'))} → {_fmt(beam_ledger)} "
            f"= before − {_fmt(booked_out)} of kernel fee: the ledger and the wallet moved "
            f"together, so there is no drift to acknowledge"
        )
    elif beam_ledger == exp_ledger:
        out(
            f"  ✓ treasury BEAM ledger {_fmt(beam_ledger)} — unchanged, which is what an "
            f"UNBOOKED fee looks like: no value left the wallet, so BeamPay booked nothing"
        )
    else:
        out(
            f"  ⚠️ the treasury's BEAM ledger moved {_fmt(before.get('beam_ledger'))} → "
            f"{_fmt(beam_ledger)} and this plan accounts for {_fmt(booked_out)} of that. "
            f"Something ELSE moved BEAM while this ran — nothing here adjusts a balance to make "
            f"its own arithmetic come out"
        )
        rc = 1
    if aid != BEAM_ASSET_ID and asset_ledger != int(before.get("asset_ledger") or 0):
        out(
            f"  ⚠️ the treasury's {key} ledger moved {_fmt(before.get('asset_ledger'))} → "
            f"{_fmt(asset_ledger)}; a split must leave it unchanged"
        )
        rc = 1
    exp = None if before.get("beam_wallet") is None else int(before["beam_wallet"]) - fee
    got = after["wallet_beam"]
    if exp is None or got is None:
        out("  ⚠️ the wallet's spendable BEAM could not be read on both sides, so 'down by "
            "exactly the fee' is UNKNOWN — never assumed")
        rc = 1
    elif int(got) == exp:
        out(
            f"  ✓ wallet BEAM {_fmt(before['beam_wallet'])} → {_fmt(got)} = before − "
            f"{_fmt(fee)} of kernel fee, exactly"
        )
    else:
        out(
            f"  ⚠️ the wallet's spendable BEAM is {_fmt(got)} and before − fee is {_fmt(exp)}, "
            f"a difference of {_fmt(int(got) - exp)} — something else moved BEAM while this ran"
        )
        rc = 1
    return rc


async def run_split(plan: Plan, out: Any = print, bp: beampay.BeamPay | None = None) -> int:
    """Make the plan real. ⛔ THE METHOD IS THE PLAN'S, never the caller's: a plan half-made one
    way can only ever be continued the same way."""
    if plan.is_tx_split:
        return await run_tx_split(plan, out, bp)
    return await _run_beampay_split(plan, out, bp)


async def _run_beampay_split(plan: Plan, out: Any = print, bp: beampay.BeamPay | None = None) -> int:  # noqa: C901
    """THE FALLBACK (`--method beampay`). Make the plan real, one leg at a time. Returns a
    process exit code.

    Every irreversible step is preceded by a row and followed by one; every leg is looked up by
    its comment before it is sent; the coin count is re-measured after every chunk and the plan
    STOPS when it is not rising."""
    bp = bp or beampay.beampay()
    treasury = beampay.treasury_address()
    key, aid = plan.asset_key, plan.aid

    reasons = await apply_refusals(bp)
    if reasons:
        out("REFUSED — nothing was sent:")
        for r in reasons:
            out(f"  ⛔ {r}")
        return 1

    # ⛔ RE-DERIVED FROM FRESH READS, NOT FROM THE PASS CACHE. `wallet_spendable` and the coin
    # walk are cached per pass precisely so N gates in one pass share one answer — which would
    # make this comparison compare the printed plan with itself.
    payouts.reset_process_state()
    try:
        fresh = await plan_split(
            key, plan.requested_coins, plan.requested_size, stamp=plan.stamp, bp=bp,
            method=plan.method,
        )
    except (SplitError, beampay.BeamPayError) as e:
        # the treasury cannot fund the plan any more, or cannot be read at all. That is a
        # decision with a reason, not a traceback out of a money gate.
        out(f"REFUSED — the plan cannot be re-derived at apply time: {beam.redact(e)}")
        out("  Nothing was sent.")
        return 1
    if fresh.fingerprint() != plan.fingerprint():
        out(
            "REFUSED — the plan re-derived at apply time is NOT the plan that was printed (the "
            "ledger or the wallet moved in between). Nothing was sent; run the dry run again."
        )
        return 1

    split_addr = await split_address_for(bp, plan)
    before = {
        "beam": await bp.available_groth(treasury, BEAM_ASSET_ID),
        "asset": await bp.available_groth(treasury, aid),
    }
    coins_now = await _coins_now(key, bp)
    row = await _plan_row(plan, split_addr, treasury, before, coins_now)
    # …and from here on the PLAN's own starting numbers are the ones the report measures against
    before = dict(row.get("before") or before)
    coins_before = row.get("coins_before", coins_now)
    out(f"  split address : {split_addr}")
    out(f"  coins before  : {coins_now if coins_now is not None else 'UNREADABLE'}")

    # ⛔ THE WINDOW REACHES BACK TO THE PLAN, NOT A FIXED HOUR. A resume hours later must still
    # be able to FIND the legs it already sent, and a lookup that cannot see them is exactly the
    # read that would send one of them twice.
    since = min(time.time(), float(row.get("created_at") or time.time())) - LOOKBACK_S
    fees: list[int] = []
    fee_verified = False
    last_coins = coins_now
    made = 0
    # ⛔ AN ADOPTED CHUNK MINTS NOTHING, BECAUSE IT ALREADY DID. The progress guard below asks
    # "did the count rise", and on a RESUME the answer for a leg that was merely found in
    # BeamPay's history is "no, it rose the first time" — which is not the failure the guard is
    # for. So it only applies to a chunk this run actually SENT something for.
    chunk_sent = False
    try:
        for leg in plan.legs:
            src = treasury if leg.outbound else split_addr
            dst = split_addr if leg.outbound else treasury
            if workers.paused():
                raise SplitError(f"the kill switch is set ({settings.stop_file})")

            row = await db().utxo_splits.find_one({"_id": leg.comment}) or {}
            tx = await _one(bp, src, leg.comment, since)
            if tx is None:
                if row.get("status") in ("queued", "calling"):
                    # queued, and no transaction carrying it: never re-sent by a machine
                    raise SplitError(
                        f"{leg.comment!r} was handed to BeamPay at "
                        f"{time.strftime('%H:%M:%SZ', time.gmtime(float(row.get('called_at') or 0)))} "
                        f"and no transaction carrying it can be found. /withdraw is not "
                        f"idempotent, so this is NOT re-sent — a human resolves it"
                    )
                if leg.kind == "back":
                    # ⛔ THE PRECONDITION OF A `back` LEG, CHECKED THE INSTANT BEFORE IT IS SENT.
                    # It moves the split address's WHOLE balance of the asset, so that balance
                    # has to be exactly what the `out` leg put there — including, for BEAM, the
                    # fee this leg is about to pay. Anything else and the round trip would not
                    # close at zero, and an address left holding a remainder is value the next
                    # run has no plan for.
                    want = leg.amount_groth + (plan.fee_groth if leg.aid == BEAM_ASSET_ID else 0)
                    held = await bp.available_groth(split_addr, leg.aid)
                    if held != want:
                        raise SplitError(
                            f"the split address holds {held} groth of asset {leg.aid} and "
                            f"{leg.comment!r} needs it to hold exactly {want}; the `back` leg "
                            f"would not leave it at zero, so nothing further was sent"
                        )
                # ── the irreversible step, with its row on both sides of it ──
                await _leg_row(
                    plan, leg, status="calling", called_at=time.time(),
                    from_address=src, to_address=dst,
                )
                try:
                    res = await bp.withdraw(src, dst, leg.aid, leg.amount_groth, leg.comment)
                except beam.Halted:
                    await _leg_row(plan, leg, status="halted")
                    raise SplitError(
                        f"the kill switch was set inside the mover — {leg.comment!r} was NOT "
                        f"queued and the plan stops here"
                    ) from None
                except beampay.BeamPayError as e:
                    await _leg_row(plan, leg, status="queued", error=beam.redact(e)[:200])
                    raise SplitError(
                        f"the answer to {leg.comment!r} was lost ({beam.redact(e)[:160]}). It "
                        f"may have been queued, so it is NOT re-sent: a re-run of this command "
                        f"looks the comment up in BeamPay's history first"
                    ) from None
                if res.get("status") is not True:
                    await _leg_row(
                        plan, leg, status="refused", error=str(res.get("msg") or res)[:200]
                    )
                    raise SplitError(
                        f"BeamPay refused {leg.comment!r}: {str(res.get('msg') or res)[:160]} — "
                        f"nothing was queued"
                    )
                await _leg_row(plan, leg, status="queued")
                chunk_sent = True
                out(f"    → {leg.comment}  {_fmt(leg.amount_groth)} queued")
            else:
                out(f"    ↺ {leg.comment}  already sent (tx {str(tx.get('txId'))[:12]}) — not re-sent")

            tx = await _settled(bp, src, leg.comment, since, out)
            fee = int(tx.get("fee") or 0)
            fees.append(fee)
            await _leg_row(
                plan, leg, status="settled", txid=str(tx.get("txId") or ""),
                kernel=tx.get("kernel"), fee_groth=fee, settled_at=time.time(),
            )
            # ⛔ THE CANARY. BeamPay sets the fee and ignores ours (§WE-SET-IT-WE-DONT-READ-IT),
            # so the constant the whole plan is sized with is checked against the FIRST
            # transaction it made — before the second one is paid for.
            if not fee_verified:
                fee_verified = True
                if fee != plan.fee_groth:
                    raise SplitError(
                        f"the first leg settled at a fee of {fee} groth and this plan is built "
                        f"on {plan.fee_groth} (BeamPay's FEE_REGULAR). Every later chunk's "
                        f"arithmetic would be wrong by the difference, so the plan stops here "
                        f"with exactly one fee spent. The split address holds "
                        f"{await bp.available_groth(split_addr, leg.aid)} groth of asset "
                        f"{leg.aid}"
                    )
            if leg.kind == "back":
                left_asset = await bp.available_groth(split_addr, aid)
                if left_asset:
                    raise SplitError(
                        f"after chunk {leg.index} the split address still holds {left_asset} "
                        f"groth of {key} — it must end each chunk at zero"
                    )
                made += 1
                now_coins = await _coins_now(key, bp)
                await db().utxo_splits.update_one(
                    {"_id": plan.plan_id},
                    {"$set": {f"progress.{leg.index}": now_coins, "updated_at": time.time()}},
                )
                out(
                    f"    ✓ chunk {leg.index}: coin of {_fmt(plan.sizes[leg.index])} {key} "
                    f"minted — coins now {now_coins if now_coins is not None else 'UNREADABLE'}"
                )
                # ⛔ THE WALLET PICKS THE INPUTS, NOT US. If the count did not rise, this plan is
                # spending the coins it is making and the remaining fees would buy nothing.
                if (
                    chunk_sent
                    and now_coins is not None
                    and last_coins is not None
                    and now_coins <= last_coins
                ):
                    raise SplitError(
                        f"chunk {leg.index} settled and the spendable {key} coin count did not "
                        f"rise ({last_coins} → {now_coins}): the wallet is funding these "
                        f"transfers out of the coins this plan just minted, so the remaining "
                        f"{len(plan.legs) - leg.seq - 1} leg(s) would buy nothing. Stopped with "
                        f"{made} of {plan.coins} chunk(s) made"
                    )
                last_coins = now_coins
                chunk_sent = False
    except SplitError as e:
        await db().utxo_splits.update_one(
            {"_id": plan.plan_id},
            {"$set": {"status": "halted", "halted_at": time.time(), "reason": str(e)[:400]}},
        )
        out("")
        out(f"STOPPED after {made} of {plan.coins} chunk(s): {e}")
        await _report(plan, bp, treasury, split_addr, before, fees, made, coins_before, out)
        return 1

    await db().utxo_splits.update_one(
        {"_id": plan.plan_id}, {"$set": {"status": "done", "done_at": time.time()}}
    )
    out("")
    return await _report(plan, bp, treasury, split_addr, before, fees, made, coins_before, out)


async def _report(
    plan: Plan,
    bp: beampay.BeamPay,
    treasury: str,
    split_addr: str,
    before: dict[str, int],
    fees: list[int],
    made: int,
    coins_before: int | None,
    out: Any,
) -> int:
    """What the plan actually did, measured — never asserted.

    Three questions, and every one of them is answered by a fresh read: did the coin count reach
    the target; did the treasury's BEAM fall by exactly the fees and nothing else; is the split
    address empty. A `no` is REPORTED, in words, and never repaired by a ledger edit (law 6)."""
    key, aid = plan.asset_key, plan.aid
    rc = 0
    coins_after = await _coins_now(key, bp)
    beam_after = await bp.available_groth(treasury, BEAM_ASSET_ID)
    asset_after = await bp.available_groth(treasury, aid)
    left_beam = await bp.available_groth(split_addr, BEAM_ASSET_ID)
    left_asset = await bp.available_groth(split_addr, aid)
    spent = sum(fees)
    out("RESULT")
    out(f"  chunks made   : {made} of {plan.coins}")
    out(
        f"  coins ({key:<4}): {coins_before if coins_before is not None else '—'} → "
        f"{coins_after if coins_after is not None else 'UNREADABLE'} (target {plan.target})"
    )
    if coins_after is None:
        out("  ⚠️ the wallet's coin list could not be read at the end, so whether the target was "
            "reached is UNKNOWN — that is not the same as 'no' and not the same as 'yes'")
        rc = 1
    elif coins_after < plan.target:
        out(
            f"  ⚠️ SHORT by {plan.target - coins_after} coin(s). The wallet holds "
            f"{coins_after} spendable {key} coin(s) against a target of {plan.target}: run this "
            f"command again for the rest, or lower PGAS_"
            f"{'BEAM_FEE_COINS' if key == BEAM_KEY else 'UTXO_TARGET_COINS'} to what the "
            f"treasury can actually carry"
        )
        rc = 1
    else:
        out(f"  ✓ the target of {plan.target} spendable {key} coin(s) is met")
    out(
        f"  fees paid     : {len(fees)} leg(s), {_fmt(spent)} BEAM "
        f"(planned {_fmt(plan.total_fee_groth)} for {len(plan.legs)})"
    )
    # ⛔ THE LEDGER HAS TO COME OUT EXACTLY. The value round-trips, so the ONLY thing that may
    # have left the treasury is the fees. Anything else means something ELSE moved money while
    # this ran, and that is a sentence, not a correction.
    expected = before["beam"] - spent
    if beam_after == expected:
        out(
            f"  ✓ treasury BEAM {_fmt(before['beam'])} → {_fmt(beam_after)} = before − "
            f"{_fmt(spent)} of fees, exactly"
        )
    else:
        out(
            f"  ⚠️ treasury BEAM is {_fmt(beam_after)} and before − fees is {_fmt(expected)}, a "
            f"difference of {_fmt(beam_after - expected)}. Something other than this plan moved "
            f"BEAM while it ran — nothing here adjusts a balance to make its own arithmetic come "
            f"out; the legs are in `utxo_splits` with their txids"
        )
        rc = 1
    if aid != BEAM_ASSET_ID:
        if asset_after == before["asset"]:
            out(f"  ✓ treasury {key} {_fmt(asset_after)} — unchanged, as a round trip must be")
        else:
            out(
                f"  ⚠️ treasury {key} {_fmt(before['asset'])} → {_fmt(asset_after)}: a round "
                f"trip must leave it unchanged"
            )
            rc = 1
    if left_beam or left_asset:
        out(
            f"  ⚠️ the split address {split_addr[:16]}… still holds {_fmt(left_beam)} BEAM and "
            f"{_fmt(left_asset)} {key}. It is ours and it is spendable; sweep it back with a "
            f"BeamPay /withdraw, or re-run this command to finish the plan"
        )
        rc = 1
    else:
        out("  ✓ the split address is empty in both assets")
    return rc


# ──────────────────────────────────────────────────────── closing a plan that is not finishable


def plan_id_of(row: dict[str, Any]) -> str:
    """A plan row's id, for a sentence that has to name it precisely enough to act on."""
    return str(row.get("_id") or "")


async def abandon_plan(
    plan_id: str, reason: str | None = None, out: Any = print, bp: beampay.BeamPay | None = None
) -> int:
    """Take an OPEN plan whose work is over out of the resume set. Returns a process exit code.

    ⛔ **THIS EXISTS BECAUSE AN OPEN PLAN IS NOT A DORMANT ONE — IT IS AN INSTRUCTION.**
    `cmd_split` resumes the newest open plan for an asset and continues it with the METHOD IT
    WAS MADE WITH, deliberately: a plan half-made one way cannot be finished the other. The cost
    is that a plan which will never be finished — every leg settled, the coins it minted long
    since re-spent, the run stopped by a guard that no longer applies — captures every later
    invocation for that asset. On 2026-09-10 `split|BEAM|20260910T150102Z` did exactly that on
    the box: a `--method tx_split` run became a `--method beampay` run, and there was no CLI way
    out and no editing Mongo by hand.

    ⛔ **AND IT IS GATED ON EVIDENCE, NOT ON THE OPERATOR'S CONFIDENCE.** Abandoning a plan with
    something in flight, or with value sitting at its split address, does not end the work — it
    ends the only record of the work, and the money is then attached to an address nothing will
    ever look at again. So both facts are MEASURED here, from the plan's own rows and from
    BeamPay, and a refusal names exactly what is in the way. Nothing about the chain changes:
    this writes a status and an event row and moves not one groth.
    """
    bp = bp or beampay.beampay()
    row = await db().utxo_splits.find_one({"_id": plan_id, "kind": "plan"})
    if not row:
        out(f"⛔ no split plan `{plan_id}` — `beam status` names the open one for each asset")
        return 2

    status = str(row.get("status") or "")
    if status == "abandoned":
        was = row.get("abandon_reason") or "no reason recorded"
        out(f"  ↺ `{plan_id}` is already abandoned ({was}) — nothing to do, nothing resumes it")
        return 0
    if status not in SPLIT_STATUS_OPEN:
        out(
            f"⛔ `{plan_id}` is `{status}`, and only {'/'.join(SPLIT_STATUS_OPEN)} plans are ever "
            f"resumed — this one is already ignored, so abandoning it would change nothing and "
            f"record a decision that was never needed"
        )
        return 1

    # ── gate 1: nothing of this plan may be in flight ───────────────────────────────────────
    blocking: list[str] = []
    legs = await db().utxo_splits.find({"kind": "leg", "plan_id": plan_id}).to_list(None)
    for leg in sorted(legs, key=lambda r: int(r.get("seq") or 0)):
        st = str(leg.get("status") or "planned")
        if st not in LEG_STATUS_DONE:
            blocking.append(f"leg {leg.get('comment')} is `{st}`")
    if row.get("method") == SPLIT_METHOD_TX:
        # No legs on this method: the plan row IS the transaction, so its own status is the one
        # that says whether the wallet may be holding one.
        for field in ("status", "halted_from"):
            if str(row.get(field) or "") in TX_SPLIT_IN_FLIGHT:
                blocking.append(
                    f"the plan's `{field}` is `{row.get(field)}`, so the wallet may hold or have "
                    f"made transaction {str(row.get('txid') or '')[:12]}… and this row does not "
                    f"record what became of it"
                )
    if blocking:
        out(f"⛔ `{plan_id}` is not finished, so it cannot be abandoned:")
        for b in blocking:
            out(f"     {b}")
        out(
            "     Re-run the split to resume it — a settled leg is adopted, never re-sent — and "
            "abandon it once every leg is `settled` or `refused`."
        )
        return 1

    # ── gate 2: its split address must hold nothing, in any asset ───────────────────────────
    addr = row.get("split_address")
    held: dict[str, str] = {}
    if addr:
        try:
            bal = await bp.balances(str(addr))
        except beampay.BeamPayError as e:
            # ⛔ AN UNREADABLE BALANCE IS NOT AN EMPTY ONE (law 8). Abandoning on a read we could
            # not make is exactly how value gets attached to an address nobody looks at again.
            out(
                f"⛔ the split address {str(addr)[:18]}… could not be read "
                f"({beam.redact(e)[:160]}) — that is 'we cannot see', never 'it is empty'. "
                f"Nothing was changed."
            )
            return 1
        for bucket in ("available", "locked"):
            for aid, groth in (bal.get(bucket) or {}).items():
                if int(groth or 0):
                    held[f"{bucket}.{aid}"] = str(groth)
    if held:
        out(f"⛔ `{plan_id}`'s split address {str(addr)[:18]}… still holds value:")
        for k, v in sorted(held.items()):
            out(f"     {k} = {v} groth")
        out(
            "     Abandoning it now would leave that value attached to an address nothing will "
            "look at again. Resume the plan so its `back` legs return it to the treasury first."
        )
        return 1

    # ── the write: guarded on the status we just read, so a concurrent run cannot lose ──────
    now = time.time()
    why = str(reason or "").strip() or "closed by the operator: its work is over and it was capturing every new plan for this asset"
    res = await db().utxo_splits.update_one(
        {"_id": plan_id, "kind": "plan", "status": status},
        {"$set": {"status": "abandoned", "abandoned_at": now, "abandon_reason": why[:400],
                  "abandoned_from": status, "updated_at": now}},
    )
    if res.modified_count != 1:
        out(
            f"⛔ `{plan_id}` changed while this was deciding (it was `{status}`) — nothing was "
            f"written. Re-read it and try again."
        )
        return 1
    # ⛔ EVERY DECISION PATH WRITES A ROW (law 12), and this row carries the EVIDENCE that let it
    # be taken, not merely the fact that it was: the leg statuses that satisfied gate 1 and the
    # address that satisfied gate 2. An event whose justification has to be reconstructed later
    # from a timestamp is not an audit trail.
    await db().utxo_splits.update_one(
        {"_id": f"{plan_id}|abandoned"},
        {"$set": {
            "kind": "event", "event": "abandoned", "plan_id": plan_id, "at": now,
            "reason": why[:400], "from_status": status, "method": row.get("method"),
            "asset": row.get("asset"), "split_address": addr,
            "legs": {str(le.get("comment")): str(le.get("status")) for le in legs},
            "split_address_empty": True if addr else None,
        }},
        upsert=True,
    )
    out(f"ABANDONED  {plan_id}")
    out(f"  was       : {status} ({row.get('method')}, {len(legs)} leg(s), asset {row.get('asset')})")
    if addr:
        out(f"  split addr: {addr} — read via BeamPay and empty in every asset")
    else:
        out("  split addr: none — this method makes one wallet transaction and creates no address")
    out(f"  reason    : {why}")
    out("  Nothing on chain changed. A new split for this asset now starts a fresh plan.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────── the command


async def cmd_split(
    asset_key: str,
    coins: int | None = None,
    size: int | None = None,
    apply: bool = False,
    out: Any = print,
    method: str | None = None,
) -> int:
    """`python -m pgasme.beam split` — dry run by default, `--apply` to make it real.

    A re-run RESUMES the newest open plan for this asset rather than minting a new stamp: the
    identity of the work is the stamp (the txid for `tx_split`, the comments for the fallback),
    so a new stamp would be new work and everything already done would be done again.

    ⛔ AND IT RESUMES WITH THE METHOD IT WAS MADE WITH, whatever was asked for on the command
    line: a `beampay` plan half-made and then continued as a `tx_split` would mint its coins a
    second time, and the reverse would re-send its legs."""
    bp = beampay.beampay()
    payouts.reset_process_state()
    try:
        open_row = await db().utxo_splits.find_one(
            {"kind": "plan", "asset": asset_key.upper(), "status": {"$in": list(SPLIT_STATUS_OPEN)}},
            sort=[("created_at", -1)],
        )
        stamp = str(open_row["stamp"]) if open_row else None
        halted = (open_row or {}).get("reason") if (open_row or {}).get("status") == "halted" else None
        if open_row:
            coins = coins or int(open_row.get("coins") or 0)
            size = size or int((open_row.get("sizes") or [0])[0])
            was = str(open_row.get("method") or SPLIT_METHOD_BEAMPAY)
            if method and method != was:
                # ⛔ A WARNING IS NOT A DECISION, AND THIS ONE COST A PRODUCTION ATTEMPT. Saying
                # "the --method you typed does not apply" and then doing the other thing anyway
                # means the operator authorises one plan and a different one runs — on
                # 2026-09-10 a `--method tx_split` became a `--method beampay` on the box,
                # because a plan whose work was long over was still the newest OPEN one for
                # BEAM. When what was asked for and what would happen differ, nothing happens.
                out(
                    f"⛔ --method {method} was asked for, but the newest OPEN plan for "
                    f"{asset_key.upper()} is `{plan_id_of(open_row)}` and it was made with "
                    f"--method {was}."
                )
                out(
                    f"     A plan half-made one way cannot be finished the other — its legs and "
                    f"its txid are not interchangeable — so this run would silently become a "
                    f"--method {was} run. It is refused instead."
                )
                out(
                    f"     Either finish it (`--method {was}`, or just leave --method off and it "
                    f"resumes as it was), or close it if its work is over:"
                )
                out(f"         python -m pgasme.beam split --abandon {plan_id_of(open_row)} "
                    f"--reason \"…\"")
                out(
                    "     `--abandon` refuses unless every leg is settled or refused and its "
                    "split address is empty in every asset — it changes nothing on chain."
                )
                return 2
            method = was
        plan = await plan_split(asset_key, coins, size, stamp=stamp, bp=bp, method=method)
    except (SplitError, beampay.BeamPayError, KeyError) as e:
        out(f"⛔ {e}")
        return 2
    out(
        f"UTXO split — {'APPLY' if apply else 'DRY RUN'}"
        f"{'  (resuming ' + plan.plan_id + ')' if stamp else ''}"
    )
    if halted:
        out(f"  ⚠️ this plan HALTED earlier and has not been resolved: {halted}")
    row = await db().utxo_splits.find_one({"_id": plan.plan_id})
    # ⛔ ASKED, NEVER ASSUMED, and asked on the DRY RUN too: the fee disclosure the operator
    # authorises this plan on is the one that matches the BeamPay it will actually run against.
    route = await self_tx_route(bp) if plan.is_tx_split else None
    render_plan(plan, out, split_address=(row or {}).get("split_address"), self_tx=route)
    if not apply:
        out("")
        out(
            "  Nothing was sent. Re-run with --apply to make it, after checking that no payout "
            "is in flight (`beam status`)."
        )
        return 0
    out("")
    try:
        return await run_split(plan, out, bp)
    except (beampay.BeamPayError, beam.BeamError) as e:
        out(f"⛔ {beam.redact(e)}")
        return 1
