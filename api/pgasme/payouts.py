"""Order processors — one loop, handlers keyed by status, every order carries its own step.

Admin's design: "each user order is an order and it should have status of each step where the
processor understands what to do — execute/wait — until it's completed."

  PAYOUT (`payout_requests.status`), v1 = ETH only, bETH → ETH straight to the user's W:

    scheduled ──release_at passed, float ≥ amount+fee, flag on──▶ releasing ──▶ bridging
      │  waits: too early · float short · relayer share > 10% · relayer fee > the 2% charged ·
      │         unshielded balance present (§9.3) · PGAS_PAYOUT_DIRECT_ENABLED=0
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
     bETH already burned and no refund path) and two ceilings (`max_relayer_share` protects the
     user's amount, `max_relayer_subsidy` protects the treasury). Four pinned test vectors are
     `bridge_fee.py`'s OWN answers, run on the box.
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

import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from pymongo.errors import DuplicateKeyError

from . import beam, beampay, ethpipe, ledger, tg, workers
from .assets import Asset, get_asset
from .config import settings
from .db import db

log = logging.getLogger("pgasme.payouts")

PAYOUT_ACTIVE = ("scheduled", "releasing", "bridging", "delivering")
PAYOUT_DARK = ("waiting_for_dep_eth", "waiting_for_swap_to_target_asset")
# ⛔ NOT active and NOT dark: a human owns these rows. No handler is registered for `held`, so
# nothing in this module can resolve, adopt or advance one. That is the whole point.
HELD = "held"
TREASURY_ACTIVE = ("claiming", "claimed", "shielding")

# A call whose response was lost is RESOLVED from the chain, never repeated. After this long
# without an identifiable transaction a human decides — a second process_invoke_data would be a
# second signature over one inventory.
UNRESOLVED_S = 15 * 60
BATCH = 50
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
                "dark": "",
            },
        },
    )
    if not claimed:
        return None
    if immediate:
        await tg.alert(kind, text, **{id_key: row_id})
    else:
        await tg.queue(kind, text, **{id_key: row_id})
    return claimed


async def _hold(
    coll: str, row_id: str, reason: str, key: str, dark: bool = False, cooldown_s: float = 3600.0
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
    upd: dict[str, Any] = {"hold_reason": reason, "hold_at": now}
    change: dict[str, Any] = {"$set": upd, "$inc": {"holds": 1}}
    if dark:
        upd["dark"] = True
    else:
        change["$unset"] = {"dark": ""}
    await db()[coll].update_one({"_id": row_id}, change)
    await tg.send(f"WAITING: {tg.esc(reason)} <code>{row_id}</code>", key=key, cooldown_s=cooldown_s)


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
    claimed = await db()[coll].find_one_and_update(
        {"_id": row_id, field: frm},
        {
            "$set": {
                field: HELD,
                f"{field}_at": now,
                "updated_at": now,
                "held_from": frm,
                "hold_reason": reason,
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
    kind-specific one (`claim_fee_groth`, `shield_fee_groth`) and has exactly one writer;
    `beam_fee_groth` stays as the row's "last BEAM fee" for the dashboards that read it.

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
    fields: dict[str, Any] = {"beam_fee_groth": charged}
    if field:
        fields[field] = charged
    await _checkpoint(coll, row_id, **fields)
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

# kind → (collection, the field proving the transaction exists, the recorded fee, the sort key).
# The fee field is the KIND-SPECIFIC one: `deposits.beam_fee_groth` is written by the claim and
# then overwritten by the shield, so a history read of it would mix two different costs.
_FEE_SOURCE: dict[str, tuple[str, str, str, str]] = {
    "claim": ("deposits", "claim_txid", "claim_fee_groth", "claim_call_at"),
    "send": ("payout_requests", "beam_txid", "beam_fee_groth", "release_call_at"),
    "shield": ("deposits", "shield_txids", "shield_fee_groth", "shielded_at"),
}

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
        if kind == "shield"
        else {txid_field: {"$exists": True, "$ne": None}}
    )
    rows = await db()[coll].find(q).sort(sort_key, -1).limit(n).to_list(n)
    fees: list[int] = []
    for row in rows:
        fee = int(row.get(fee_field) or 0)
        txid = str(row.get(txid_field) or "") if kind != "shield" else ""
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


async def relayer_fee_for(asset: Asset, rpc: Any) -> tuple[int, int, dict[str, Any]]:
    """(fee, floor, detail) — the relayer's own arithmetic, never BELOW the floor we ride.

    §WE-SET-IT-WE-DONT-READ-IT cuts both ways. Too high and we simply hand over the difference;
    too LOW and the relayer may never pick the message up — and per §7.9 #7 that message stalls
    for days with the bETH already burned and no refund path. `ethpipe.min_relayer_fee_units` is
    the floor the e2b side already rides; the b2e side had none at all."""
    fee, detail = await beam.relayer_fee_groth(asset, rpc)
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
    have -= int(_PASS["beam_fee"])
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


async def dest_code_at_head(rpc: Any, w_addr: str) -> tuple[str, int, str]:
    """`(code, head, endpoint)` for W, read from ONE endpoint PINNED to the head IT reported.

    ⛔ A code read spread over a pool can be answered by a node that does not have that block's
    state yet, and `"0x"` from a node that cannot see the block is not "this is a wallet" — it is
    this guard silently becoming a no-op on the one refusal that has no refund path (the request
    path's `trusted_head` exists for exactly this). RAISES rather than returning a default: an
    unreadable chain is not an empty answer."""
    head, url = await rpc.head_from()
    if int(head) <= 0:
        raise ethpipe.RpcError(f"the endpoint that answered reported block {head}")
    code = await rpc.call("eth_getCode", [w_addr, hex(int(head))], prefer=url, pin=True)
    if not isinstance(code, str):
        raise ethpipe.RpcError(f"{url} could not read the code at the destination")
    return code, int(head), url


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

    §9.7: the request id, never W. `api.log` otherwise pairs request_id → destination and
    `payout_requests` pairs request_id → account_id, which is the whole product defeated."""
    rid = str(row["_id"])
    was_head = int(row.get("dest_checked_head") or 0)
    try:
        code, head, url = await dest_code_at_head(get_rpc(), str(row["W"]))
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


async def _payout_scheduled(row: dict[str, Any]) -> None:
    rid = row["_id"]
    now = time.time()
    if now < float(row.get("release_at") or 0):
        return  # the window has not opened; not a refusal, nothing to say
    if str(row.get("mode")) != "direct":
        await _payout_any_asset(row)
        return
    asset = _asset_of(row)
    if asset.key != "ETH":
        await _hold(
            "payout_requests",
            rid,
            f"v1 payouts are ETH only; this request is {asset.key}",
            key=f"payout-asset:{rid}",
        )
        return
    amount = int(row["amount_groth"])
    charged = int(row.get("fee_groth") or 0)
    if not settings.payout_direct_enabled:
        # §9.7: the request id, never W. api.log otherwise pairs request_id → destination and
        # payout_requests pairs request_id → account_id, which is the whole product defeated.
        log.info(
            "would release payout %s: %s %s (PGAS_PAYOUT_DIRECT_ENABLED=0)",
            rid,
            tg.fmt_groth(amount),
            asset.key,
        )
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
    fee_groth, floor_groth, detail = await relayer_fee_for(asset, get_rpc())
    if amount <= 0 or fee_groth / amount > settings.max_relayer_share:
        await _hold(
            "payout_requests",
            rid,
            f"the relayer wants {tg.fmt_groth(fee_groth)} of {tg.fmt_groth(amount)} "
            f"(max {settings.max_relayer_share:.0%}) — refusing to cross at this gas price",
            key=f"payout-share:{rid}",
        )
        return
    # ⛔ THE CYCLE-LEVEL GATE. The share gate protects the user's amount; nothing protected the
    # TREASURY. At the 0.01 ETH product floor the crossing is loss-making above ~1.11 gwei and
    # the share gate still admits it to 5 gwei, where we pay 0.0009 ETH against 0.0002 charged.
    # Every guard needs the level it protects.
    if fee_groth > charged * settings.max_relayer_subsidy:
        await _hold(
            "payout_requests",
            rid,
            f"the relayer wants {tg.fmt_groth(fee_groth)} and this payout only charged "
            f"{tg.fmt_groth(charged)} (subsidy limit {settings.max_relayer_subsidy:g}×) — "
            f"refusing to cross at a loss",
            key=f"payout-subsidy:{rid}",
        )
        return
    need = amount + fee_groth + settings.float_min_groth
    # THE FLOAT IS A SUM OF BEAMPAY BALANCES. Shielded value lives at our max-privacy
    # addresses because a BeamPay `/withdraw` put it there — one FRESH address per shield chunk
    # (`shield_target`) — so `float_groth` sums the whole registry and never reads one address.
    # The primary is still named here because it is the address this release BOOKS its flow to
    # and the address BeamPay debits the BEAM fee from, and an address we cannot name is a
    # float we cannot read.
    mp_addr = await float_address()
    if not mp_addr:
        await _hold(
            "payout_requests",
            rid,
            "no max_privacy address is configured or stored, so the shielded float cannot be "
            "read and a release would be spending a number nobody measured",
            key=f"payout-nofloat:{rid}",
        )
        return
    have = await float_groth(bp, asset)
    # every crossing already in flight is committed float, whether or not the wallet's
    # `available_mp` has noticed yet (for a BVM invocation it may not fall until the kernel
    # registers at all). `_advance` writes `releasing` before `_release` sends, so an order
    # admitted earlier in THIS pass is already counted here.
    reserved = await inflight_groth(asset, rid)
    if have - reserved < need:
        await _hold(
            "payout_requests",
            rid,
            f"the shielded float holds {tg.fmt_groth(have)} {asset.key} across "
            f"{len(await mp_registry())} max-privacy address(es), {tg.fmt_groth(reserved)} of "
            f"it is already committed to crossings in flight, and this payout needs "
            f"{tg.fmt_groth(need)} (amount + relayer fee + reserve)",
            key=f"payout-float:{rid}",
        )
        return
    # §9.3 / spec question S2: the gate above reads `available_mp`, but the WALLET picks the
    # inputs for a shader invocation and nobody has proven which bucket it reaches for. Until
    # an operator proves it on the box, refuse while any unshielded balance of this asset
    # exists — a send funded from a freshly-claimed regular output is exactly the deposit ↔
    # payout link the product promises does not exist. Refuse rather than assume.
    if not settings.beam_send_inputs_proven:
        # claimed-but-not-yet-shielded value sits at the TREASURY address (the claim was
        # registered to it); shielded value sits at the max-privacy one. That split is the
        # whole reason both addresses exist.
        regular = await bp.available_groth(beampay.treasury_address(), asset.aid)
        if regular > settings.beam_regular_tolerance_groth:
            await _hold(
                "payout_requests",
                rid,
                f"the wallet still holds {tg.fmt_groth(regular)} unshielded {asset.key} and it "
                f"is not proven which inputs a pipe send spends (spec S2) — refusing while a "
                f"send could link a claim to this payout",
                key=f"payout-unshielded:{rid}",
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
    # a pipe `send` is a BVM contract invocation and costs BEAM in the same class as a claim —
    # and BeamPay debits that fee from the address the txid is REGISTERED to, which for a
    # release is the max-privacy one, so that is the balance this gate has to see fall.
    if not await _beam_fees_ok(bp, "send", rid, "payout_requests", fee_addr=mp_addr):
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
    )
    if claimed is None:
        return  # another pass owns it
    await _release(rid, asset, amount, fee_groth, str(row["W"]), mp_addr)


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
            float_addr = await float_address()
            if not float_addr or not await register_attribution(
                "payout_requests", rid, str(txid), float_addr, rid,
                "payout_attribution", "request_id",
            ):
                await _hold(
                    "payout_requests",
                    rid,
                    "the crossing is on the chain but BeamPay has not accepted its attribution"
                    + (" (no max_privacy address to attribute it to)" if not float_addr else "")
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
        "payout_requests", rid, str(tx["txId"]), await float_address() or beampay.treasury_address(),
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
    """Scheduled −= amount + fee · Sent += amount · the 2% leaves the account (§6.5b step 14b).
    Booked when the Beam kernel confirms — that is the moment the bETH is provably burned.

    Guarded on BOTH halves. `ledger.release` appends a `release` entry and a `fee` entry; a
    guard that reads only the first declares "already booked" forever when the second append
    failed, and the 2% sits in Scheduled where nothing can clear or charge it."""
    rid = row["_id"]
    if await ledger.find_entry("release", rid) and await ledger.find_entry("fee", rid):
        return
    await ledger.release(
        row["account_id"],
        row.get("asset") or "ETH",
        int(row["amount_groth"]),
        int(row.get("fee_groth") or 0),
        rid,
        f"direct payout bridged, Beam txid {row.get('beam_txid')}",
    )


async def _fail(row: dict[str, Any], frm: str, reason: str) -> None:
    """Terminal failure. The schedule debit is reversed ONLY when no `release` was booked — if
    the bETH left the wallet the money is gone from the treasury and refunding it here would
    invent it. A refund needs the debit it reverses, and only once."""
    rid = row["_id"]
    claimed = await _advance(
        "payout_requests",
        rid,
        "status",
        frm,
        "failed",
        "payout_failed",
        f"Payout FAILED: {reason}",
        "request_id",
        immediate=True,
        failed_reason=reason,
        failed_at=time.time(),
    )
    if claimed is None:
        return
    if await ledger.find_entry("release", rid):
        return  # the value left the treasury: this is an operator matter, not a refund
    entry = await ledger.find_entry("schedule", rid)
    if not entry:
        return
    try:
        await ledger.cancel(
            row["account_id"],
            row.get("asset") or "ETH",
            int(entry["groth"]),
            rid,
            f"payout failed before anything was sent: {reason}",
            refund_of=f"schedule:{rid}",
        )
    except ledger.AlreadyRefunded:
        pass
    await _set("payout_requests", rid, refunded_groth=int(entry["groth"]))


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
        await _fail(
            row,
            "bridging",
            f"the Beam transaction is {tx.get('status_string') or status} — nothing crossed",
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
        await fee_charged("payout_requests", rid, tx, f"payout {rid}", "request_id")
        # ⛔ BOOK FIRST, MARK SECOND. `kernel_at` used to be written before the ledger entry, so
        # one transient Mongo error left the marker set and the release unbooked forever: the
        # user's money parked in Scheduled, `sent` never showing it, cancel refusing, and a
        # later _fail path would have REFUNDED a payout whose bETH was burned.
        await _book_release({**row, **fields})
        fields["kernel_at"] = time.time()
        await _set("payout_requests", rid, kernel_at=fields["kernel_at"])
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
    incoming = await w.view_incoming(asset.beam_cid)  # unreadable RAISES: never "not delivered"
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
    mine = [m for m in await w.view_incoming(asset.beam_cid) if m["msg_id"] == msg_id]
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
        raw, args = await w.build_receive(asset.beam_cid, msg_id)
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
      * the SHAPE — a 64/66-hex SBBS token is a REGULAR address. Shielding to one is a send
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
            "the shield target is a 64/66-hex SBBS (regular) address, not a max-privacy token "
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
    "releasing": _payout_releasing,
    "bridging": _payout_bridging,
    "delivering": _payout_delivering,
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
) -> list[dict[str, Any]]:
    """The rows to work THIS pass, selected per status with its own limit.

    ⛔ One FIFO over every active status starves the queue. `waiting_for_dep_eth` rows always
    hold (the any-asset stub is not implemented), releases held for a human never move, and
    crossings stuck in `delivering` never leave: fifty of those, all with old `release_at`,
    occupied the whole batch and a healthy due payout sat `scheduled` with no hold_reason at
    all — never even reached to be marked, so no monitor saw it either.

    A row whose last pass ended in a hold is also skipped for `hold_backoff_s`: a refusal is a
    decision, and repeating it every 30 seconds crowds out work."""
    now = time.time()
    cool = {
        "$or": [
            {"hold_at": {"$exists": False}},
            {"hold_at": {"$lt": now - settings.hold_backoff_s}},
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
        "payout_requests", "status", PAYOUT_ACTIVE + PAYOUT_DARK, "release_at"
    )
    return await _run(rows, lambda r: PAYOUT_HANDLERS.get(str(r.get("status"))), "payout")


async def treasury_once() -> int:
    rows = await _due(
        "deposits",
        "treasury",
        (None, *TREASURY_ACTIVE),
        "credited_at",
        base={"status": "credited"},
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
    return {"payouts": await payouts_once(), "treasury": await treasury_once(), "lease": 1}
