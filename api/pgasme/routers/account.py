"""GET /v1/account — the balances the user sees (one per asset), with history.

⛔ THIS ROUTE IS THE ONE THE USER LOOKS AT WHEN SOMETHING IS WRONG, so it renders whatever is
in the database — including rows written before a guard existed. A single legacy
`payout_requests` row carrying `deliver_at: NaN` used to make this endpoint 500 for that account
FOREVER (Starlette renders JSON with `allow_nan=False`, and a NaN anywhere in the payload raises
at encode time), which hid the very order the user needed to find in order to cancel it. A value
that cannot be serialised is therefore blanked to `null` on the way out and NAMED on its row
(`unreadable_fields`), and every derived number is read tolerantly — a poisoned row degrades one
field of one row, never the whole account.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from fastapi import APIRouter

from .. import auth, ledger, payouts, uniswap, xchain
from ..assets import ASSETS, get_asset
from ..config import LEGACY_STATUS_FIELD, settings
from ..db import db
from .withdrawals import min_amount_groth

router = APIRouter(prefix="/v1/account", tags=["account"])

# money that is on its way into the balance (ordered, locked or confirming) — a fallback is NOT
# pending: the funds went to the user's own wallet and nothing of ours will move them.
PENDING_STATES = ("submitted", "order_seen", "locked", "confirming")


def is_unreadable(v: Any) -> bool:
    """True for a number that is not a number: NaN, ±Infinity. `bool` is an `int`, never a float,
    so it is not a candidate; ints (and Mongo's int64) are always finite."""
    return isinstance(v, float) and not math.isfinite(v)


def finite(value: Any) -> Any:
    """The payload with every non-finite number replaced by `null`, recursively.

    ONE implementation for the whole response (law 9), applied once at the end: a second scrub
    living somewhere else would eventually disagree with this one about what "unreadable" means.
    It is the LAST thing that happens to the answer, so nothing can add a NaN after it."""
    if is_unreadable(value):
        return None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [finite(v) for v in value]
    return value


def tolerant_int(v: Any) -> int:
    """A groth total read from a row that may predate the guards. `int(float('nan'))` RAISES —
    an aggregate over one poisoned `value_groth` would otherwise take the whole account with it."""
    if v is None or is_unreadable(v):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ⛔ ONE READER for "which Beam block does the bridge explorer key on for this row" (T31b item 8,
# admin 2026-09-10: *"Here is the link to block explorer of the bridge …?tx={block_height}, so user
# can track his withdrawals and funding his wallets"*).
#
# The explorer keys on a BLOCK HEIGHT, not on a txid or a kernel id, and the client must never
# derive one: it renders the link when — and only when — this number is present. So both kinds of
# row answer the same question through the same function (law 9), under one name on the wire.
#
# The candidates, in the order a row is trusted:
#   beam_height            an exact kernel height, if a writer ever records one. It wins.
#   beam_height_at_kernel  the payout crossing's. ⚠️ It is the WALLET's height at the moment the
#                          kernel was first seen (payouts.py:4379-4386 — BeamPay's contract_tx does
#                          not expose the block a contract tx settled in), so it is late by at most
#                          one poll interval. It is a tracking link, never evidence.
#   claim_height           the deposit claim's, written by `payouts._treasury_claiming` on the
#                          transition to `claimed` and nowhere else. ⚠️ Same caveat as above: it
#                          is the record's own block if BeamPay ever states one, else the WALLET's
#                          height when the kernel was first seen. A claim whose height could not
#                          be read carries none — the deposit still advances, and the link is
#                          simply not drawn, which is the honest state and not a bug.
BEAM_HEIGHT_FIELDS = ("beam_height", "beam_height_at_kernel", "claim_height")


def beam_height(row: dict) -> int | None:
    """The Beam block height of this row's crossing, or None.

    ⛔ NONE, NEVER ZERO. 0 is a block, and `?tx=0` is a link to somebody else's bridge traffic
    presented to the user as their own. Anything that is not a positive whole number — a NaN from
    a row written before the guards, a bool (which is an `int` in Python), a string Mongo handed
    back, a list — is "we do not know", and the client draws no link."""
    for field in BEAM_HEIGHT_FIELDS:
        v = row.get(field)
        if v is None or isinstance(v, bool) or is_unreadable(v):
            continue
        try:
            h = int(v)
        except (TypeError, ValueError):
            continue
        if h > 0:
            return h
    return None


# ⛔ THE DENY-LIST IS THE CONTRACT. Everything else on a deposit row reaches the wire, so a new
# field is public the day it is written unless it is named here — which is why the list is a
# constant with a test on it rather than three `pop`s inside the function.
#
#   account_id     the database key; the session already knows whose account it is.
#   pubkey         OUR Beam receiver key for that pipe. Never on a user-facing wire.
#   unseen_errors  WHICH endpoint said what while nobody could see the transaction (2026-09-10).
#                  It is the operator's diagnostic and it is written ON THE ROW for exactly that
#                  reason — but it carries endpoint URLs verbatim, and a provider URL can carry
#                  an API key in its query string. A refusal must be explainable to the user
#                  ("no Ethereum endpoint has seen this transaction in N min") without handing
#                  them our infrastructure; the row keeps the evidence, the wire keeps the note.
PRIVATE_DEPOSIT_FIELDS = ("account_id", "pubkey", "unseen_errors")


def public_deposit(d: dict) -> dict:
    d = dict(d)
    for field in PRIVATE_DEPOSIT_FIELDS:
        d.pop(field, None)
    # ONE reader for the mode (xchain.norm_mode): rows written before the same-chain modes
    # existed carry none, and rows written before the rename carry the router's own name.
    d["mode"] = xchain.norm_mode(d.get("mode"))
    # …and rows written before the rename carry the router's own name for the status field too.
    # The STORED row is never rewritten (never edit history to fix a ledger); it is read here,
    # once, for both this route and GET /v1/deposits/{id}, and the old spelling never reaches
    # the wire. A row with neither key gains neither: a null `route_status` on a deposit that
    # has no router status would read as "unknown", not as "not applicable".
    legacy_status = d.pop(LEGACY_STATUS_FIELD, None)
    if legacy_status is not None and not d.get("route_status"):
        d["route_status"] = legacy_status
    # the Beam block the claim's kernel landed in, so the client can link this deposit into the
    # bridge's own explorer. One writer (`beam_height` above), null until it is known.
    d["beam_height"] = beam_height(d)
    return d


# ⛔ THE DENY-LIST IS THE CONTRACT HERE TOO (T52). Everything else on a payout row reaches the
# wire, so a field written by the order machine is public the day it exists unless it is named
# here.
#
#   hold_detail  the OPERATOR half of `payouts.hold_texts` — the same refusal WITH its numbers
#                (spendable/maturing buckets, coin counts, fee budgets). The row keeps it
#                because the admin panel and the Telegram digest need it; a user's page gets
#                `hold_reason`, which is the sentence written for them. The admin's own words on
#                2026-09-10 15:36Z were "again issues no one can understand", holding exactly
#                one of these.
PRIVATE_REQUEST_FIELDS = ("hold_detail",)


def public_request(r: dict) -> dict:
    """One payout order as the account shows it — with the fields that cannot be serialised
    named, so a blank `deliver_at` says WHY it is blank instead of reading as "asap".

    ⛔ **AND ITS ETA, FROM THE ONE WRITER** (T40, admin 2026-09-10: *"You should show all
    statuses there and estimated time of arrival of his asset"*). `payouts.eta_for` is the only
    implementation of "when does this arrive" and the CLI reads the same function, so the page
    the user refreshes and the line an operator reads cannot drift apart. It is COMPUTED here
    and never stored: an `eta_at` written onto the row would go stale the moment the order is
    delayed, released or re-delayed, and a stale promise is worse than none.

    `eta_at: null` is an honest "we do not know" (a row parked for a human, a row with no
    delivery window) — never a zero and never an invented time. `cancellable` comes from the
    same module the route enforces it with, so the button the client draws and the answer
    `POST /v1/withdrawals/{id}/cancel` gives are one decision."""
    r = dict(r)
    for field in PRIVATE_REQUEST_FIELDS:
        r.pop(field, None)
    bad = sorted(k for k, v in r.items() if is_unreadable(v))
    if bad:  # `finite()` blanks the values themselves, once, on the whole payload
        r["unreadable_fields"] = bad
    try:
        eta_at, tail_s, note = payouts.eta_for(r)
    except Exception:  # noqa: BLE001 — a poisoned row degrades ONE field, never the account
        eta_at, tail_s, note = None, 0, ""
    r["eta_at"] = eta_at
    r["eta_tail_s"] = tail_s
    r["eta_note"] = note
    # ⛔ THE NEXT ATTEMPT AS A TIME, NEVER INSIDE THE PROSE (M3, handed over from T35b). The note
    # used to end "; next try at 1789046877", which no page can render and every page would have
    # had to parse back out of a sentence. The words say WHAT is happening; this says WHEN, in
    # the one format a browser turns into "in 2 minutes" without knowing our clock. `None`
    # whenever there is no next attempt to name — a row nothing retries must not imply one, and
    # a row carrying a NaN must not take the account down (`tolerant_int`'s reason).
    nxt = tolerant_int(r.get("next_attempt_at"))
    r["next_try_at"] = (
        dt.datetime.fromtimestamp(nxt, tz=dt.UTC).isoformat().replace("+00:00", "Z")
        if nxt > 0
        else None
    )
    r["cancellable"] = payouts.cancellable(r)[0]
    # ⛔ WHAT CAME BACK, ALWAYS PRESENT (T45). The bridge fee is an ESTIMATE — a live gas read
    # times the headroom the wait needs — and since 2026-09-10 whatever the crossing does not
    # spend is credited back to Available at settlement (`ledger.bridge_fee_refund`). The page
    # renders THIS number; a client that has to ask "is the field missing or is it nothing"
    # ends up writing the arithmetic a second time, so a row that refunded nothing says 0.
    # Read tolerantly for the same reason every derived number on this route is: a poisoned row
    # degrades one field, never the account.
    r["bridge_fee_refund_groth"] = tolerant_int(r.get("bridge_fee_refund_groth"))
    # …and the Beam block its crossing was seen in, under the SAME name as a deposit's, so the
    # client has one reader for one kind of link (T31b item 8).
    r["beam_height"] = beam_height(r)
    return r


@router.get("")
async def account(acct=auth.Account):
    aid = acct["account_id"]
    bal = await ledger.balances(aid)
    deposits = (
        await db().deposits.find({"account_id": aid}).sort("created_at", -1).limit(30).to_list(30)
    )
    pending_cur = db().deposits.aggregate(
        [
            {"$match": {"account_id": aid, "status": {"$in": list(PENDING_STATES)}}},
            {"$group": {"_id": "$asset", "groth": {"$sum": "$value_groth"}}},
        ]
    )
    pending = {
        r["_id"]: tolerant_int(r.get("groth"))
        for r in await pending_cur.to_list(length=len(ASSETS) + 8)
    }
    balances = {k: {**bal[k], "pending": pending.get(k, 0)} for k in ASSETS}
    requests = (
        await db()
        .payout_requests.find({"account_id": aid}, {"account_id": 0})
        .sort("created_at", -1)
        .limit(60)
        .to_list(60)
    )
    dests = await db().destinations.count_documents(
        {"account_id": aid, "removed_at": {"$exists": False}}
    )
    return finite(
        {
            "address": acct["address"],
            "account_id": aid,
            "balances": balances,
            "fee_bps": settings.fee_bps,
            "denominations": settings.denominations,
            # ONE implementation of the withdrawal floor (law 9). This used to echo the raw
            # `PGAS_MIN_PAYOUT_GROTH` setting while `GET /v1/withdrawals/fees` returned
            # `max(that, the asset's grid)` — two names for one number, and the one a form falls
            # back to when the fees call has not answered yet is this one. They cannot disagree
            # now: both call `routers.withdrawals.min_amount_groth`.
            "min_payout_groth": min_amount_groth(get_asset("ETH")),
            "modes": {
                "direct": settings.payout_direct_enabled,
                "instant": settings.payout_instant_enabled,
            },
            # `armed`/`near` as before, plus which ways in are open — the client reads the
            # route flags here as well as from /v1/health, and both come from the ONE
            # implementation in uniswap.ingress_flags().
            "ingress": {
                "armed": settings.ingress_ready,
                "near": settings.ingress_near_enabled,
                **{k: v for k, v in uniswap.ingress_flags().items() if k != "direct"},
            },
            "deposits": [public_deposit(d) for d in deposits],
            "requests": [public_request(r) for r in requests],
            "destinations": dests,
            "history": await ledger.history(aid, 100),
        }
    )
