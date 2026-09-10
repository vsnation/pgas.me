"""GET /admin/* — the operator's own read-only panel. One key, no writes, nothing public.

The operator asked for one place that shows every deposit, its statuses, the Beam wallet's
balances and what is pending — "protected and never public, only for me with an access key".
So this router is built to three rules, in this order:

1. **It never writes.** Every handler is a GET and every database call here is a `find`, a
   `count_documents` or an `aggregate`. Nothing in this file appends a ledger entry, advances a
   row, creates an address or moves value — there is deliberately no button that spends. What
   the panel shows is what the money path already decided; a panel that could also decide would
   be a second writer of facts that already have one (law 9), reachable from a browser.
2. **An unconfigured key is a REFUSAL, never an open door.** With `PGAS_ADMIN_KEY` unset — or
   set to something too short to be a secret — *every* route here answers the 404 an unrouted
   path answers. Not 401, not 403: a prober must not learn that `/admin` exists on this box.
   The same 404 answers a wrong key, so a guess teaches nothing either.
   ⛔ **"THE SAME 404" IS A BYTE COMPARISON, NOT A STATUS CODE.** Two ways it was not, both
   measured on this file before they were closed:
     * `GET/POST/HEAD/OPTIONS /admin` — no trailing slash — answered **307 to `/admin/`**. The
       catch-all lives on a router with a `/admin` prefix, so its real path is
       `/admin/{rest:path}`; the bare prefix matched nothing and Starlette's `redirect_slashes`
       helpfully offered the spelling that does. A redirect is a YES. The bare prefix is now a
       route of its own (`bare` below, included beside this router) that answers the 404.
     * the refusal's BODY was `{"detail":"not found"}` where an unrouted path answers
       `{"detail":"Not Found"}` — the same 22 bytes of content-length, two of them different,
       and a prober diffing the two learns that something is listening. Every refusal here now
       raises `HTTPException(404)` with no detail of its own, so the framework writes the body
       and the two cannot drift (`not_found()`).
3. **The key never reaches a log.** The header is compared and dropped; the success line
   records the METHOD, the PATH and the caller's address and nothing else — never the query
   string, which carries the filters (an account address in a log next to the IP that asked for
   it is the very thing `main.RedactAccessLog` exists to prevent). That filter drops the query
   of every `/admin` line for the same reason, and `main.redact_secrets` masks the key's value
   in any record that somehow carries it.
   ⚠️ THAT COVERS THIS PROCESS'S LOGS AND NOT THE EDGE'S. nginx logs the full request target of
   everything under `location /api/`, so `/api/admin/deposits?account=0x…` lands in
   `nginx-access.log` with the address the panel asked about. The key itself is a HEADER and is
   not in nginx's format at all; the filters are. `deploy/nginx-pgas.me.conf` already turns the
   access log off for `^~ /api/internal` and for the destinations route, and a `^~ /api/admin`
   block would do the same here — a deploy-side decision, not this file's.

**Why a 429 at all, when the answer is otherwise 404.** Five failures from one address inside
`PGAS_RATE_WINDOW_S` is not a typo, it is somebody working through a list, and the operator
wants to hear about it once. So the FIFTH refusal — the one that reaches `FAIL_LIMIT`, counting
itself — is already the 429, and ONE digest page is sent per address per window. That 429 does tell a persistent prober the path exists — it is a deliberate
trade of a little silence for an alert, and it is taken ONLY when a key is configured: on a box
where the panel is not provisioned there is nothing to protect and the answer stays 404 forever.

The failure counter is in this process's memory on purpose: a counter in Mongo would be a WRITE
from a read-only router (rule 1), and the unit runs a single worker. It is an abuse damper and
the pager's trigger — not a ban list, and a restart clears it. Said out loud rather than
implied, because a guard that is weaker than it looks is worse than no guard.

**What the numbers here are, and where each came from.** This panel derives nothing of its own:
every field is either a raw row, a count of rows, or the answer of the ONE reader the money path
itself uses (`ledger.balances`, `routers.stats.health`, `payouts.spend_unshielded`,
`payouts.scheduled_liability_groth`) — with the four BEAM reads as the stated exception below,
where sharing the reader would mean sharing a cache. Where a fact has no single durable writer — "when did this worker last
run" — the answer names the state it was read from instead of pretending to be a heartbeat, and
a fact that could not be read says so: an unreadable query is never rendered as a zero.

⛔ **THE TREASURY ROUTE MUST NOT REACH INTO THE PAYOUT PROCESSOR'S MEMORY.** It used to: it
called `payouts.mp_registry`, `payouts.float_groth` and `payouts.wallet_spendable`, and every
one of those CACHES ITS ANSWER IN `payouts._PASS` for the duration of a pass. The API and the
payout loop are one process and one event loop (`--workers 1`, the loop `workers.start()`
starts), so a browser refreshing this tab mid-pass could plant the float, the registry, the
wallet buckets and the fee budget that the next gate of that pass would then read as its own
measurement — a number no pass took, arriving through a page with no business writing one.
Restoring the caches afterwards would be no better: it would drop whatever the pass had cached
in between, and one pass with two float readings is the exact defect those caches exist to
prevent. So the panel reads through its OWN helpers below (`mp_registry_now`, `float_now`,
`wallet_buckets`, `coin_counts_now`), which consult no cache and write none.
They are a SECOND reading of one fact, which is law 9's warning, so the split is held together
by a test rather than by hope: `test_the_panels_treasury_readers_agree_with_the_money_paths`
asserts the panel's numbers equal `payouts`' own on the same wallet, and the parsing primitives
(`payouts._groth`, `payouts._looks_like_asset`) are IMPORTED rather than re-typed — only the
assembly is local, and only because the assembly is where the cache lives.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from .. import __version__, auth, ledger, payouts, tg, workers
from ..assets import ASSETS, get_asset
from ..config import settings
from ..db import db
from .account import finite, public_deposit
from .stats import health as health_summary

log = logging.getLogger("pgasme.admin")

# The route family, named ONCE. `main.RedactAccessLog` imports this to decide which access-log
# lines must lose their query string — two hand-written prefixes would be two implementations of
# one fact, and the one that drifted would be the one still logging the filters.
PREFIX = "/admin"

# `X-Admin-Key: <key>` or `Authorization: Bearer <key>`. Two spellings because the panel is a
# browser page (a header its fetch sets) and a curl one-liner is how the operator checks the
# box; both land in the same comparison.
HEADER = "X-Admin-Key"
BEARER = "bearer "

# Failures from one address inside `settings.rate_window_s` before the answer becomes 429 and
# the operator is paged once. See the module docstring for why this is in memory.
FAIL_LIMIT = 5
MAX_TRACKED_IPS = 4096  # a bound, so a spray from many addresses cannot grow this without end
_fails: dict[str, list[float]] = {}

# The watchdog is a separate unit with its own log (deploy/watch): the panel shows its last line
# so the operator can see the box's own verdict beside the API's. A CONSTANT, not a knob — the
# brief allows exactly one new setting and this path is fixed by the unit that writes it.
WATCHDOG_LOG = "/var/log/pgasme/watch.log"
WATCHDOG_TAIL_BYTES = 8192

# Paging: the panel is a table, so a page is an offset window plus the total it is a window of.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

# ── the raw drawer's allow-list ────────────────────────────────────────────────────────────────
# ONE closed list. Every collection a route above already renders, plus `quotes`, which no list
# route shows and every deposit row names (`quote_id`) — the drawer is the only way to open the
# row a deposit came from. Deliberately absent: `siwe_nonces` / `dest_nonces` (live credentials)
# and `rate_limits` (a counter nobody reads by id).
RAW_COLLECTIONS = (
    "accounts",
    "beampay_events",
    "deliveries",
    "deposits",
    "destinations",
    "entries",
    "events",
    "leases",
    "mp_addresses",
    "payout_requests",
    "quotes",
    "reservations",
    "scanner_state",
    "stats",
    "treasury",
    "unattributed_locks",
)

# The statuses a deposit and a payout order pass through, in the order they pass through them,
# so the panel can show an empty bucket as a zero rather than not at all. Read from the modules
# that own the machines wherever one exists (`payouts.PAYOUT_ACTIVE` and friends are imported
# lazily inside the routes — payouts imports workers, and workers is imported here).
# What a treasury cell says when there is no address to read a balance OF. `beampay.treasury_
# address()` raises for the same reason: a balance read with no address is not a zero.
NO_TREASURY: dict[str, Any] = {
    "value": None,
    "error": "PGAS_BEAM_TREASURY_ADDRESS is not configured — there is no address to read",
}

DEPOSIT_STATUSES = (
    "submitted",
    "order_seen",
    "locked",
    "confirming",
    "credited",
    "fallback_pending",
    "failed",
    "expired",
)


# ─────────────────────────────────────────────────────────────────────── the key and its guard


def configured_key() -> str:
    """The admin key, or `""` when there is nothing fit to compare against.

    `settings.secret_problem` is the SAME test `jwt_secret` and `account_salt` are held to at
    boot — empty, still a placeholder, or shorter than a secret can be. A key that fails it is
    not a key, and this route family answers 404 exactly as if it were unset: half a secret must
    never be a whole door."""
    return "" if settings.secret_problem("admin_key") else settings.admin_key.strip()


def presented_key(request: Request) -> str:
    """The key this request carries, from either accepted header ('' when it carries none)."""
    got = (request.headers.get(HEADER) or "").strip()
    if got:
        return got
    authz = request.headers.get("authorization") or ""
    if authz[: len(BEARER)].lower() == BEARER:
        return authz[len(BEARER) :].strip()
    return ""


def matches(got: str, want: str) -> bool:
    """Constant-time, on BYTES.

    ⛔ Both sides encoded, for the reason `routers/internal.authorize` encodes them: on `str`,
    `compare_digest` raises TypeError the moment either side is not ASCII-only — and the header
    is whatever the caller typed, so one non-ASCII character would turn a refusal into a 500.
    Encoding weakens nothing: two different strings still encode to two different byte strings."""
    if not (got and want):
        return False
    return hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8"))


def note_failure(ip: str, now: float) -> int:
    """Record one refusal for this address and return how many it has made in the window."""
    window = now - float(settings.rate_window_s)
    hits = [t for t in _fails.get(ip, ()) if t >= window]
    if len(hits) < FAIL_LIMIT:  # bounded per address: past the limit the answer is already 429
        hits.append(now)
    _fails[ip] = hits
    if len(_fails) > MAX_TRACKED_IPS:
        for addr, seen in list(_fails.items()):
            if not seen or seen[-1] < window:
                _fails.pop(addr, None)
    return len(hits)


def reset_failures() -> None:
    """Forget every counted refusal — for a test, and for a caller that needs a clean window.
    Per-process state that outlived one test is state the next one inherits by accident."""
    _fails.clear()


async def require_admin(request: Request) -> str:
    """404 (or 429) unless this request carries the configured key. Returns the caller's address.

    Hung on the ROUTER, not on each route: a record that must exist on every path belongs in a
    helper called from every path (law 14), and a new handler added to this file cannot forget
    a guard it never had to remember."""
    ip = auth.client_ip(request)
    # ⛔ `request.url.path` and never the target: the QUERY carries the filters, and an account
    # address in a log line next to the address that asked for it is there for as long as the
    # box keeps logs. `main.RedactAccessLog` drops the query of every /admin line for the same
    # reason — this is that decision made once more, on the line this router writes itself.
    path = request.url.path
    want = configured_key()
    if matches(presented_key(request), want):
        log.info("admin: %s %s from %s", request.method, path, ip)
        return ip
    now = time.time()
    n = note_failure(ip, now)
    problem = settings.secret_problem("admin_key")
    why = (
        f"PGAS_ADMIN_KEY {problem} — the panel is not provisioned on this deployment"
        if problem
        else ("no key presented" if not presented_key(request) else "the key did not match")
    )
    log.warning(
        "admin: refused %s %s from %s (%s; %d failure(s) in %ds)",
        request.method, path, ip, why, n, int(settings.rate_window_s),
    )
    if n >= FAIL_LIMIT:
        # ONE page per address per window (tg.send's own cooldown is the digest), and it names
        # the count, never the key or what was tried.
        await tg.send(
            f"REFUSED: {n} admin-panel attempts from <code>{tg.esc(ip)}</code> in the last "
            f"{int(settings.rate_window_s)}s — {tg.esc(why)}. Further attempts from this "
            f"address are answered {'429' if want else '404'} for the rest of the window",
            key=f"admin-auth:{ip}",
            cooldown_s=float(settings.rate_window_s),
        )
        if want:
            # Only with a key configured: on an unprovisioned box every answer stays 404, so a
            # prober cannot even learn that this family of routes is mounted.
            raise auth.too_many(
                int(settings.rate_window_s), "too many attempts — try again later"
            )
    raise not_found()


def not_found() -> HTTPException:
    """THE refusal this whole family answers with — the framework's own 404, byte for byte.

    ⛔ NO DETAIL OF ITS OWN. `HTTPException(404, "not found")` rendered `{"detail":"not found"}`
    while an unrouted path renders `{"detail":"Not Found"}`: two bodies of identical length whose
    difference is two capital letters, which is all a prober needs to tell "nothing is mounted
    here" from "something is refusing me". `HTTPException(404)` takes its detail from
    `http.HTTPStatus(404).phrase` — the SAME string Starlette's own not-found path uses — so
    there is one implementation of the answer and it cannot drift (law 9)."""
    return HTTPException(404)


Admin = Depends(require_admin)
router = APIRouter(prefix=PREFIX, tags=["admin"], include_in_schema=False, dependencies=[Admin])

# ⛔ The one route that CANNOT live on the router above: the prefix itself. A prefixed
# `APIRouter` refuses an empty path, so `/admin` matched nothing, and Starlette's
# `redirect_slashes` answered every method with **307 → /admin/** — an unprovisioned box
# advertising the family it is meant to be hiding. Registered beside `router` in `main` and
# carrying the SAME guard, so a probe of the bare prefix is counted and paged like any other.
bare = APIRouter(include_in_schema=False, dependencies=[Admin])

# Every verb a panel READ answers. GET is the read; HEAD must answer exactly what GET answers
# with the body dropped (FastAPI's `APIRoute` does NOT add it the way Starlette's plain `Route`
# does — measured: `HEAD /admin/overview` fell through to the catch-all and 404'd while `GET`
# on the same path returned 200); OPTIONS says which verbs there are and runs nothing.
READ_METHODS = ("GET", "HEAD", "OPTIONS")
ALLOW = ", ".join(READ_METHODS)

F = TypeVar("F", bound=Callable[..., Any])


async def verbs() -> Response:
    """The answer to OPTIONS on a panel read: the verbs, and no body.

    ⛔ It must never run the read itself. `OPTIONS /admin/treasury` reaching the treasury handler
    would spend one BeamPay call per registered address plus a wallet UTXO walk to answer a
    question about grammar."""
    return Response(status_code=204, headers={"Allow": ALLOW})


def read(path: str) -> Callable[[F], F]:
    """Register one panel read: GET, the HEAD that must match it, and a body-free OPTIONS.

    ⛔ MUST BE USED BY EVERY ROUTE IN THIS FILE, and every one of them must be registered BEFORE
    the catch-all at the bottom. The catch-all answers 404 for every method on every path the
    reads did not claim — that is what makes a wrong path indistinguishable from an unrouted
    one — so an OPTIONS route mounted on `/{rest:path}` would answer 204 for paths that do not
    exist and map the family for anyone holding the key."""

    def register(fn: F) -> F:
        # OPTIONS on its own route (a different endpoint, so `fn` cannot run for it). Path match
        # + method mismatch is a PARTIAL match in Starlette, and a FULL match anywhere in the
        # table wins over it, so the two routes on one path do not shadow each other.
        router.add_api_route(path, verbs, methods=["OPTIONS"])
        return router.api_route(path, methods=["GET", "HEAD"])(fn)  # type: ignore[return-value]

    return register


# ────────────────────────────────────────────────────────────────────── shapes on the way out


def plain(value: Any) -> Any:
    """The payload with every value Mongo can hold but JSON cannot rendered as text.

    A raw row is the whole point of this panel, so the exotic types come with it: `entries` and
    `events` carry an ObjectId `_id` (nothing projects it away here), a quote's `at` may be a
    BSON date, and bytes reach a row through `metadata`. Starlette would answer 500 on any of
    them — which would hide exactly the row the operator opened the drawer to read.

    It does NOT decide what a broken number is: that definition lives in `routers/account`
    (`is_unreadable`) and blanking them stays `finite()`'s job, applied once at the end.
    """
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return value  # `finite()` owns the non-finite ones
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    return str(value)  # ObjectId, Decimal128, Binary, anything a future driver adds


def out(payload: Any) -> Any:
    """THE one exit every route in this file uses: JSON-safe, then non-finite numbers blanked.

    `finite()` LAST, and imported rather than re-implemented — a second reading of "this number
    cannot be serialised" would eventually disagree with the one `/v1/account` uses, and one of
    the two would 500 on the row the operator needs most."""
    return finite(plain(payload))


def admin_deposit(row: dict[str, Any]) -> dict[str, Any]:
    """One deposit row for the panel: everything it holds, read through the ONE normaliser.

    `public_deposit` maps the pre-rename mode and status spellings onto the names of record
    (rows are never rewritten to fix a ledger), and it drops the two fields the public account
    view has no business showing. The operator's panel wants those two back, so they are put
    back HERE rather than by a second copy of the normalisation."""
    return {
        **public_deposit(row),
        "account_id": row.get("account_id"),
        "pubkey": row.get("pubkey"),
        # The identifiers a link is built FROM. No URLs: the web already owns one implementation
        # of "which explorer is this chain's" (`web/src/lib/format.ts explorerTx`), and a second
        # map here would be the one that goes stale.
        "tx": {
            "src": {"chain_id": (row.get("src") or {}).get("chain_id"), "hash": row.get("src_tx_hash")},
            "eth": {"chain_id": settings.eth_chain_id, "hash": (row.get("eth") or {}).get("tx")},
            "beam": {
                "claim_txid": row.get("claim_txid"),
                "shield_txids": row.get("shield_txids") or [],
            },
        },
    }


def hold_of(row: dict[str, Any]) -> dict[str, Any]:
    """The hold as it sits ON the row — `payouts._hold` is the only writer of these fields.

    Derived, never stored twice: a hold that is over has had every one of these unset by
    `payouts._advance`, so an empty `reason` here means the row is not waiting on anything.

    ⛔ **`reason` IS THE OPERATOR'S HALF** (T52, 2026-09-10). A payout row's `hold_reason` is now
    the sentence written for the USER ("Waiting for treasury funds — expected by Sat 12 Sep,
    23:21Z at the latest"), and an operator console that showed only that would be telling the
    person who has to FIX it the least useful version of the fact. `payouts.operator_reason` is
    the one reader of "why, with its numbers"; `user_reason` travels beside it so this console
    can show what the customer is being told, which is the other half of answering them."""
    return {
        "reason": payouts.operator_reason(row) or None,
        "user_reason": row.get("hold_reason"),
        "code": row.get("hold_code"),
        "at": row.get("hold_at"),
        "count": int(row.get("holds") or 0),
        "dark": bool(row.get("dark")),
        "paged_at": row.get("hold_paged_at"),
        "reminded_at": row.get("held_reminded_at"),
    }


# ────────────────────────────────────────────────────────────────────────────── paging + counts


async def page(
    collection: str,
    query: dict[str, Any],
    sort_key: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """One window of a collection, newest first, with the total it is a window of."""
    coll = db()[collection]
    total = await coll.count_documents(query)
    cur = coll.find(query).sort(sort_key, -1).skip(offset).limit(limit)
    rows = await cur.to_list(length=limit)
    return {
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(rows) < total,
    }


async def counts(
    collection: str, field: str, base: dict[str, Any] | None = None
) -> dict[str, int]:
    """`{value: n}` for one field of one collection. A row with no such field counts under
    `"none"` — for `deposits.treasury` that bucket is "no treasury work started yet", which is
    a real state and not an absence."""
    cur = db()[collection].aggregate(
        [
            *([{"$match": base}] if base else []),
            {"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
        ]
    )
    rows = await cur.to_list(length=200)
    return {("none" if r["_id"] is None else str(r["_id"])): int(r["n"]) for r in rows}


def since_query(field: str, since: float | None) -> dict[str, Any]:
    return {field: {"$gte": float(since)}} if since is not None else {}


# ───────────────────────────────────────────────────────────────────────────────── the routes


@read("/overview")
async def overview() -> Any:
    """Everything the operator checks first: flags, the kill switch, health, the workers' last
    pass, the watchdog's last word, and how many rows sit in each status."""
    now = time.time()
    d = db()
    from .. import distributor, payouts  # local: payouts imports workers, which this module imports

    stop_file = settings.stop_file
    health = await health_summary()
    active_payouts = list(payouts.PAYOUT_ACTIVE) + list(payouts.PAYOUT_DARK) + [payouts.HELD]
    return out(
        {
            "at": now,
            "version": __version__,
            "env": settings.env,
            # ONE implementation of "what is armed" — the same body /v1/health serves, so the
            # panel and the watchdog can never disagree about the posture.
            "health": health,
            "flags": {
                "ingress_armed": settings.ingress_armed,
                "ingress_ready": settings.ingress_ready,
                "ingress_assets": {k: settings.ingress_ready_for(k) for k in ASSETS},
                "claim_enabled": settings.claim_enabled,
                "shield_enabled": settings.shield_enabled,
                "payout_direct_enabled": settings.payout_direct_enabled,
                "payout_instant_enabled": settings.payout_instant_enabled,
                "payout_spend_unshielded": payouts.spend_unshielded(),
                "workers_enabled": settings.workers_enabled,
                "dev_endpoints": settings.dev_endpoints_active,
                # the BOOLEAN only, never the token (same rule as /v1/health)
                "telegram_live": tg.enabled(),
                "beampay_webhook": bool(settings.beampay_webhook_token),
            },
            "kill_switch": {"file": stop_file, "engaged": os.path.exists(stop_file)},
            # the INSTANT payout wallet, in FULL — the address, the last float a refill pass
            # read and the next nonce (T34b's handoff). This route is key-protected; /v1/health
            # and /v1/stats get the two-boolean projection of the same function and nothing more.
            "distributor": await distributor.summary(),
            # …and the bETH sitting at crossing addresses: funded for one order each, burned by
            # nothing yet, and invisible to every other float reader (T40b F11). The number is
            # here rather than on a public route for the reason above.
            "crossings": {
                **await payouts.crossing_health(),
                "queued_groth": await payouts.queued_crossing_groth(get_asset("ETH")),
            },
            "workers": {
                "enabled": settings.workers_enabled,
                "paused": workers.paused(),
                # ⛔ NOT a heartbeat. No worker writes "I ran"; these are the newest rows each
                # loop leaves behind, and every one names the state it was read from so nobody
                # reads a missing number as a stopped worker.
                "last_pass": await worker_last_pass(),
            },
            "watchdog": watchdog_tail(),
            "counts": {
                "deposits": await counts("deposits", "status"),
                "deposits_treasury": await counts(
                    "deposits", "treasury", {"status": "credited"}
                ),
                "payouts": await counts("payout_requests", "status"),
                "unattributed_locks": await counts("unattributed_locks", "status"),
                "accounts": await d.accounts.count_documents({}),
                "beampay_events": await d.beampay_events.count_documents({}),
                "events_unsent": await d.events.count_documents({"notified": False}),
                "held": {
                    "payouts": await d.payout_requests.count_documents(
                        {"hold_reason": {"$exists": True}}
                    ),
                    "deposits": await d.deposits.count_documents(
                        {"hold_reason": {"$exists": True}}
                    ),
                },
                "in_flight": {
                    "payouts": await d.payout_requests.count_documents(
                        {"status": {"$in": active_payouts}}
                    ),
                    "treasury": await d.deposits.count_documents(
                        {"treasury": {"$in": list(payouts.TREASURY_ACTIVE)}}
                    ),
                },
            },
        }
    )


async def worker_last_pass() -> dict[str, Any]:
    """When each loop last left a trace, and WHERE that trace was read from.

    There is no heartbeat collection and this route does not create one: a panel that wrote a
    row would be a writer (rule 1), and a second writer of "is this worker alive" is exactly the
    kind of fact that ends up disagreeing with the thing it describes. So each entry is the
    newest durable row that loop is the only writer of, and it says which."""
    d = db()
    from .. import payouts  # local: see `overview`

    scan = await d.scanner_state.find({}).sort("at", -1).limit(1).to_list(1)
    lease = await d.leases.find_one({"_id": payouts.LEASE_ID})
    pool = await d.stats.find_one({"_id": "pool"})
    notified = await d.events.find({"notified": True}).sort("notified_at", -1).limit(1).to_list(1)
    return {
        "deposit_watcher": {
            "at": (scan[0].get("at") if scan else None),
            "source": "scanner_state (the newest pipe checkpoint the lock scan wrote)",
            "detail": {"pipe": (scan[0].get("_id") if scan else None),
                       "last_block": (scan[0].get("last_block") if scan else None)},
        },
        "payout_processor": {
            "at": (lease or {}).get("at"),
            "source": f"leases/{payouts.LEASE_ID} (renewed by the pass that holds it)",
            "detail": {"owner": (lease or {}).get("owner"),
                       "released_at": (lease or {}).get("released_at")},
        },
        "stats_refresher": {
            "at": (pool or {}).get("at"),
            "source": "stats/pool (the explorer reading the refresher stores)",
            "detail": {"height": (pool or {}).get("height")},
        },
        "monitor": {
            "at": (notified[0].get("notified_at") if notified else None),
            "source": "events.notified_at (the newest event the monitor drained)",
            "detail": {"kind": (notified[0].get("kind") if notified else None)},
        },
    }


def watchdog_tail() -> dict[str, Any]:
    """The watchdog's last line, or why it could not be read.

    Its log is written by another unit and lives on a 750 directory: "the API cannot open it" is
    a real and ordinary answer (a dev laptop has no such file at all), and it is said rather than
    rendered as silence. The line goes through `main.redact_secrets` because logs on this box
    are secrets-adjacent — that is the incident that made the mask exist."""
    from ..main import redact_secrets  # local: main imports this module

    path = WATCHDOG_LOG
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > WATCHDOG_TAIL_BYTES:
                fh.seek(-WATCHDOG_TAIL_BYTES, os.SEEK_END)
            tail = fh.read().decode("utf-8", "replace")
        mtime = os.path.getmtime(path)
    except OSError as e:  # missing, unreadable, a directory — named, never swallowed
        return {"log": path, "line": None, "at": None, "why": f"{type(e).__name__}: {e}"}
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    return {
        "log": path,
        "line": redact_secrets(lines[-1]) if lines else None,
        "at": mtime,
        "why": None if lines else "the log is empty",
    }


@read("/deposits")
async def deposits(
    status: str | None = None,
    treasury: str | None = None,
    account: str | None = None,
    chain: int | None = None,
    since: float | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Any:
    """Every deposit, newest first. `account` matches either the account id or the sender
    address the row was registered with — the operator has one of the two, never both."""
    q: dict[str, Any] = {**since_query("created_at", since)}
    if status:
        q["status"] = status
    if treasury:
        q["treasury"] = treasury
    if account:
        q["$or"] = [{"account_id": account}, {"address": account}]
    if chain is not None:
        q["src.chain_id"] = int(chain)
    got = await page("deposits", q, "created_at", limit, offset)
    got["rows"] = [admin_deposit(r) for r in got["rows"]]
    got["statuses"] = list(DEPOSIT_STATUSES)
    return out(got)


@read("/deposits/{deposit_id}")
async def deposit(deposit_id: str) -> Any:
    """One deposit: the raw row, the operator events that name it, and its ledger entries.

    The entries are looked up by `ref`, which for a deposit IS its id (`ledger.credit` is called
    with `dep["_id"]`) — the same key the double-credit index is unique on."""
    row = await db().deposits.find_one({"_id": deposit_id})
    if not row:
        raise HTTPException(404, "unknown deposit")
    return out(
        {
            "deposit": admin_deposit(row),
            "raw": row,
            "events": await db().events.find({"deposit_id": deposit_id})
            .sort("at", -1).limit(200).to_list(200),
            "entries": await db().entries.find({"ref": deposit_id})
            .sort("at", -1).limit(200).to_list(200),
            "quote": await db().quotes.find_one({"_id": row.get("quote_id")})
            if row.get("quote_id")
            else None,
        }
    )


@read("/payouts")
async def payouts_list(
    status: str | None = None,
    account: str | None = None,
    asset: str | None = None,
    since: float | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Any:
    """Every payout order, newest first, each with the hold that is stopping it (if any)."""
    from .. import payouts as payouts_mod  # local: see `overview`

    q: dict[str, Any] = {**since_query("created_at", since)}
    if status:
        q["status"] = status
    if account:
        q["account_id"] = account
    if asset:
        q["asset"] = asset.upper()
    got = await page("payout_requests", q, "created_at", limit, offset)
    got["rows"] = [{**r, "hold": hold_of(r)} for r in got["rows"]]
    got["statuses"] = [
        *payouts_mod.PAYOUT_ACTIVE, "sent", payouts_mod.HELD, "cancelled", "failed",
        *payouts_mod.PAYOUT_DARK,
    ]
    return out(got)


@read("/payouts/{request_id}")
async def payout(request_id: str) -> Any:
    """One order: the raw row, its hold, its events, its ledger entries, and — when it has
    already been handed to the wallet — what BeamPay says about that contract transaction.

    ⚠️ `booked` is BeamPay's idempotency flag and NOT success (it is set for a cancelled or
    failed transaction too); settlement is `booked AND status == 3`. The raw answer is shown
    rather than a verdict of this route's own, because the release path already owns that
    reading and a second one here would be the one that is wrong."""
    row = await db().payout_requests.find_one({"_id": request_id})
    if not row:
        raise HTTPException(404, "unknown payout request")
    txid = str(row.get("beam_txid") or "")
    beam_tx: dict[str, Any] = {"txid": txid or None, "tx": None, "error": None}
    if txid:
        from .. import beampay  # local: keeps the import cost off every other route

        try:
            beam_tx["tx"] = await beampay.beampay().contract_tx(txid)
        except Exception as e:  # noqa: BLE001 — BeamPay unreachable is a fact, not a 500
            beam_tx["error"] = f"{type(e).__name__}: {e}"
    return out(
        {
            "payout": row,
            "hold": hold_of(row),
            "events": await db().events.find({"request_id": request_id})
            .sort("at", -1).limit(200).to_list(200),
            "entries": await db().entries.find({"ref": request_id})
            .sort("at", -1).limit(200).to_list(200),
            "beampay_tx": beam_tx,
        }
    )


@read("/unattributed")
async def unattributed(
    status: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Any:
    """Locks to our pubkey that nobody could be credited for. Every row here is a human's job:
    the scanner never credits one, and it never will."""
    q: dict[str, Any] = {"status": status} if status else {}
    return out(await page("unattributed_locks", q, "at", limit, offset))


@read("/accounts")
async def accounts(
    account: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Any:
    """Accounts by last sign-in, each with its balances and how much it has done.

    The address is NOT on the account row — the account id is a keccak of the address and the
    salt, deliberately one-way — so it is read from the `connected` destination `POST
    /v1/siwe/verify` writes for every wallet that signs in.

    Balances come from `ledger.balances`, once per account, rather than from one aggregate over
    the whole page: it is the SAME reader `/v1/account` answers the user with, so the panel can
    never show the operator a number the user is not seeing (law 9). The page is bounded, and an
    operator's panel can afford the calls that guarantee that."""
    q: dict[str, Any] = {}
    ids: list[str] = []
    if account:
        # either spelling: the account id itself, or an address in the destination book
        ids = [
            str(r["account_id"])
            for r in await db().destinations.find({"address": account}, {"account_id": 1})
            .limit(50).to_list(50)
        ]
        q = {"_id": {"$in": [account, *ids]}}
    got = await page("accounts", q, "last_login_at", limit, offset)
    rows: list[dict[str, Any]] = []
    for row in got["rows"]:
        aid = str(row["_id"])
        connected = await db().destinations.find_one({"account_id": aid, "kind": "connected"})
        rows.append(
            {
                "account_id": aid,
                "address": (connected or {}).get("address"),
                "created_at": row.get("created_at"),
                "last_login_at": row.get("last_login_at"),
                "last_chain_id": row.get("last_chain_id"),
                "balances": await ledger.balances(aid),
                "deposits": await db().deposits.count_documents({"account_id": aid}),
                "payouts": await db().payout_requests.count_documents({"account_id": aid}),
                "destinations": await db().destinations.count_documents(
                    {"account_id": aid, "removed_at": {"$exists": False}}
                ),
            }
        )
    got["rows"] = rows
    return out(got)


# ────────────────────────────── the Beam side, read WITHOUT the payout processor's per-pass memory
#
# ⛔ EVERY READER IN `payouts` THAT THIS PANEL WANTS IS CACHED IN `payouts._PASS`.
# `mp_registry`, `float_groth`, `wallet_spendable`, `coin_counts` and the `fee_budget`
# `wallet_spendable` derives all read that dict and all write it, and it is the payout
# processor's memory FOR ONE PASS, in this process. Calling them from here made a browser
# refresh a writer of the numbers a money gate reads (see the module docstring). These four
# helpers are the same reads with no cache on either side.
#
# They are deliberately THIN: the parsing primitives are imported from `payouts` rather than
# retyped, so what is duplicated is the assembly (the loops the cache sits in) and never the
# meaning of a number. `tests/test_admin_routes.py` pins the two readings equal.


async def mp_registry_now() -> list[str]:
    """Every max-privacy address the shielded float is spread across, primary first, read fresh.

    Same two sources `payouts.mp_registry` reads — `payouts.float_address()` (which is itself
    cache-free and creation-free) and the `mp_addresses` rows, oldest first, deduplicated. It
    does not warn about a long registry the way the money path does: a page is not the place
    that decides the operator needs paging, and the pass already sends that one."""
    from .. import payouts  # local: payouts imports workers, which this module imports

    out: list[str] = []
    primary = await payouts.float_address()
    if primary:
        out.append(primary)
    for row in await db().mp_addresses.find({}).sort("created_at", 1).to_list(None):
        addr = str(row.get("address") or "")
        if addr and addr not in out:
            out.append(addr)
    return out


async def float_now(bp: Any, asset: Any, registry: list[str] | None = None) -> int:
    """THE SHIELDED FLOAT of one asset: the SUM over the registry, never one address's balance.

    Every shield chunk goes to a FRESH max-privacy address, so a reader that named only
    `PGAS_BEAM_MP_ADDRESS` would show the operator one chunk of a three-chunk shielding and call
    it the float. RAISES when any one address cannot be read — a partial sum is a number nobody
    measured, and `_read` turns the raise into a cell that says so."""
    regs = await mp_registry_now() if registry is None else registry
    aid = int(asset.aid)
    total = 0
    for addr in regs:
        total += await bp.available_groth(addr, aid)
    return total


async def coin_counts_now() -> dict[int, dict[str, int]]:
    """`{asset_id: {"regular": n, "shielded": n}}` — how many SPENDABLE COINS the wallet holds.

    A balance says what one transaction may carry; the COUNT says how many can be in flight,
    because Beam locks a whole UTXO per pending transaction. The amounts each bucket is made of
    are the release gate's business (`payouts.coin_capacity`) and are deliberately not collected
    here: this is a panel, and a list of coin sizes on a page is a privacy leak with no reader."""
    from .. import beam, payouts  # local: see `mp_registry_now`

    out: dict[int, dict[str, int]] = {}
    for u in await beam.wallet().utxos():
        if payouts._groth(u.get("status")) != payouts.UTXO_AVAILABLE:
            continue  # maturing, spent, in flight — not a coin a send can pick up
        aid = payouts._groth(u.get("asset_id"))
        bucket = (
            payouts.SOURCE_SHIELDED
            if str(u.get("type") or "").lower() == payouts.UTXO_SHIELDED_TYPE
            else payouts.SOURCE_REGULAR
        )
        row = out.setdefault(aid, {payouts.SOURCE_REGULAR: 0, payouts.SOURCE_SHIELDED: 0})
        row[bucket] += 1
    return out


async def wallet_buckets(bp: Any, asset: Any) -> dict[str, Any]:
    """What the WALLET can spend of one asset right now, per bucket — which is not what we own.

    2026-09-10 on the box: BeamPay's registry summed 2,652,864 groth of bETH and the wallet's own
    `/wallet_status.totals` said `available 0 · available_mp 0 · maturing_mp 1,652,864` — the
    chunks had settled ten hours earlier and were still locked. A panel that showed one of those
    two numbers would have shown the wrong one, so the treasury tab shows both, side by side.

    RAISES when `wallet_status` cannot be read or carries no `totals`: an unreadable query is not
    evidence of anything (law 8), and it is certainly not "the wallet can spend nothing". The
    coin count degrades on its own instead — `coins_error` carries the wallet's own words and
    `coins_*` stay None, because a count we could not take is not a zero either."""
    from .. import beam, beampay, payouts  # local: see `mp_registry_now`

    aid = int(asset.aid)
    st = await bp.wallet_status()
    totals = st.get("totals")
    if not isinstance(totals, list):
        raise beampay.BeamPayError(
            "wallet_status carried no `totals` — the wallet's spendable buckets could not be "
            "read, and 'we cannot see' is never 'there is nothing to spend'"
        )
    row: dict[str, Any] = {}
    for t in totals:
        if isinstance(t, dict) and payouts._looks_like_asset(t, aid):
            row = t
            break
    # ⛔ A SHAPE WE DO NOT UNDERSTAND IS NOT A NUMBER — and above all it is not 0. Same reading
    # as the money path's, through the same primitive, so the two cannot part company on what a
    # groth is. An asset with no row in a totals array that WAS read is a real zero.
    try:
        available = payouts._groth(row.get("available"))
        shielded = payouts._groth(row.get("available_mp"))
        regular = (
            payouts._groth(row["available_regular"])
            if "available_regular" in row
            else max(0, available - shielded)
        )
        maturing_regular = payouts._groth(row.get("maturing_regular"))
        maturing_mp = payouts._groth(row.get("maturing_mp"))
        locked = payouts._groth(row.get("locked"))
    except (TypeError, ValueError) as e:
        raise beampay.BeamPayError(
            f"wallet_status.totals for asset {aid} carried a bucket this reader cannot parse "
            f"({e}) — the wallet's spendable balance could not be read"
        ) from e
    out_row: dict[str, Any] = {
        "regular": regular,
        "shielded": shielded,
        "maturing_regular": maturing_regular,
        "maturing_mp": maturing_mp,
        "maturing": maturing_regular + maturing_mp,
        "locked": locked,
    }
    coins_error: str | None = None
    try:
        counts: dict[int, dict[str, int]] | None = await coin_counts_now()
    except (beam.BeamError, TypeError, ValueError) as e:
        # a read that moves nothing must not take the page down, and a list we cannot parse is
        # exactly as unknown as one we could not fetch
        log.warning("admin/treasury: the wallet's coin list could not be read (%s)", e)
        counts, coins_error = None, f"{type(e).__name__}: {beam.redact(e)}"
    asset_coins = (counts or {}).get(aid, {})
    out_row["coins_regular"] = (
        None if counts is None else int(asset_coins.get(payouts.SOURCE_REGULAR, 0))
    )
    out_row["coins_shielded"] = (
        None if counts is None else int(asset_coins.get(payouts.SOURCE_SHIELDED, 0))
    )
    out_row["coins_error"] = coins_error
    return out_row


@read("/treasury")
async def treasury() -> Any:
    """The Beam side: what we own, what the wallet can actually spend, and what is in flight.

    THREE TABLES THAT SAY DIFFERENT THINGS, and they are kept apart on purpose (the same three
    `python -m pgasme.beam status` prints):

      * **ledger** — what we OWN, per address, from BeamPay. The only per-address truth on Beam.
      * **wallet** — what a send can FUND today (`/wallet_status.totals` through BeamPay, plus
        the coin counts from the wallet's own UTXO list). A max-privacy output is locked for up
        to 72 h after it settles, so these two parted company by the whole float once already.
      * **float policy** — what the shield deliberately keeps unshielded, and the liabilities
        that number is protecting.

    Every section is guarded on its own: one unreadable address must not blank the other twelve
    numbers, and an unreadable number is rendered as an `error` and NEVER as a zero.

    ⛔ EVERY BEAM READ BELOW GOES THROUGH THIS FILE'S OWN HELPERS (`mp_registry_now`,
    `float_now`, `wallet_buckets`), NEVER `payouts`', because `payouts`' cache their answers in
    the payout processor's per-pass memory and this page must not write one. See the module
    docstring. `payouts` is still imported for the pure policy numbers — `spend_unshielded()`,
    `scheduled_liability_groth()`, `liability_reserve_groth()`, the status tuples — none of
    which touch `_PASS`; there must be exactly one reader of a policy, and it is not this file.

    ⚠️ THIS ROUTE COSTS REAL CALLS — say it out loud rather than discovering it from a load
    graph. One request is: one `/balances` per registered address per asset (the float is a SUM
    over the max-privacy registry, which GROWS by one address per shield chunk), one
    `/wallet_status`, and one `get_utxo` walk of the wallet. It is deliberately NOT cached: this
    is the page an operator opens when they want to know what is true right now, and a treasury
    number that is quietly 30 seconds old is the kind of thing a release gets decided on. The
    page that polls it should poll it slowly — the other tabs are pure Mongo reads and cheap."""
    from .. import beampay, payouts  # local: see `overview`

    bp = beampay.beampay()
    body: dict[str, Any] = {"at": time.time()}

    try:
        treasury_addr = beampay.treasury_address()
    except Exception as e:  # noqa: BLE001 — unconfigured is a fact the panel must state
        treasury_addr = ""
        body["treasury_error"] = f"{type(e).__name__}: {e}"
    try:
        registry = await mp_registry_now()
    except Exception as e:  # noqa: BLE001
        registry = []
        body["registry_error"] = f"{type(e).__name__}: {e}"
    body["addresses"] = {
        "treasury": treasury_addr or None,
        "float_primary": registry[0] if registry else None,
        "mp_registry": registry,
        "mp_registry_size": len(registry),
    }

    # the BEAM every claim, shield and pipe send is paid for with (asset 0 at the treasury)
    body["beam_fees"] = (
        await _read("beam_fees", lambda: bp.available_groth(treasury_addr, 0))
        if treasury_addr
        else NO_TREASURY
    )
    body["fee_alert_groth"] = int(settings.beam_fee_alert_groth or 0)
    body["wallet_status"] = await _read("wallet_status", bp.wallet_status)

    ledger_rows: list[dict[str, Any]] = []
    wallet_rows: list[dict[str, Any]] = []
    policy_rows: list[dict[str, Any]] = []
    for key, asset in ASSETS.items():
        row: dict[str, Any] = {"asset": key, "aid": asset.aid}
        if treasury_addr:
            row["treasury"] = await _read(
                "available", lambda a=asset: bp.available_groth(treasury_addr, a.aid)
            )
            row["locked"] = await _read(
                "locked", lambda a=asset: bp.locked_groth(treasury_addr, a.aid)
            )
        else:
            row["treasury"] = NO_TREASURY
            row["locked"] = NO_TREASURY
        row["float"] = (
            await _read("float", lambda a=asset: float_now(bp, a, registry))
            if registry
            else {"value": None, "error": "no max-privacy address is registered"}
        )
        ledger_rows.append(row)
        wallet_rows.append(
            {
                "asset": key,
                "spendable": await _read(
                    "spendable", lambda a=asset: wallet_buckets(bp, a)
                ),
            }
        )
        owed = await _read("liability", lambda a=asset: payouts.scheduled_liability_groth(a))
        reserve = (
            payouts.liability_reserve_groth(int(owed["value"]))
            if owed.get("value") is not None
            else None
        )
        keep = max(0, int(settings.shield_keep_groth or 0))
        policy_rows.append(
            {
                "asset": key,
                "scheduled_liability": owed,
                "with_buffer_groth": reserve,
                "keep_floor_groth": keep,
                "keeps_groth": max(keep, reserve) if reserve is not None else None,
            }
        )
    body["ledger"] = ledger_rows
    body["wallet"] = wallet_rows
    body["float_policy"] = {
        "keep_groth": max(0, int(settings.shield_keep_groth or 0)),
        "liability_buffer_bps": settings.shield_liability_buffer_bps,
        "spend_unshielded": payouts.spend_unshielded(),
        "per_asset": policy_rows,
    }

    d = db()
    # PENDING INTENTS: a row that recorded what it was ABOUT to do and has no settled txid yet.
    # These are the rows a restart reconciles FROM THE CHAIN and must never re-sign — the panel
    # names them so an operator can see at a glance whether anything is mid-flight.
    body["intents"] = {
        "payouts": await d.payout_requests.find({"status": {"$in": list(payouts.INFLIGHT)}})
        .sort("status_at", -1).limit(100).to_list(100),
        "treasury": await d.deposits.find({"treasury": {"$in": ["claiming", "shielding"]}})
        .sort("treasury_at", -1).limit(100).to_list(100),
    }
    body["held"] = {
        "payouts": await d.payout_requests.find({"hold_reason": {"$exists": True}})
        .sort("hold_at", -1).limit(100).to_list(100),
        "deposits": await d.deposits.find({"hold_reason": {"$exists": True}})
        .sort("hold_at", -1).limit(100).to_list(100),
    }
    # the shield's own state, per deposit that is in or past a claim
    shielding = await d.deposits.find(
        {"treasury": {"$in": list(payouts.TREASURY_ACTIVE)}}
    ).sort("credited_at", -1).limit(100).to_list(100)
    body["shield"] = [
        {
            "deposit_id": r["_id"],
            "asset": r.get("asset"),
            "treasury": r.get("treasury"),
            "value_groth": r.get("value_groth"),
            "claim_txid": r.get("claim_txid"),
            "plan": r.get("shield_plan") or [],
            "sent": len(r.get("shield_txids") or []),
            "calls": r.get("shield_calls") or [],
            "hold": hold_of(r),
        }
        for r in shielding
    ]
    return out(body)


async def _read(what: str, fn: Any) -> dict[str, Any]:
    """Call one reader and answer `{value, error}` — an unreadable number is NEVER a zero.

    Every treasury figure goes through this: BeamPay refusing one address must degrade that one
    cell, and it must say so on the cell. "We could not look" and "there is none" are different
    answers and this panel is where the difference matters most."""
    try:
        return {"value": await fn(), "error": None}
    except Exception as e:  # noqa: BLE001 — every reader here raises its own error type
        log.warning("admin/treasury: %s unreadable: %s: %s", what, type(e).__name__, e)
        return {"value": None, "error": f"{type(e).__name__}: {e}"}


@read("/events")
async def events(
    kind: str | None = None,
    notified: bool | None = None,
    since: float | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Any:
    """The operator event log — every alert this system decided to raise, sent or not."""
    q: dict[str, Any] = {**since_query("at", since)}
    if kind:
        q["kind"] = kind
    if notified is not None:
        q["notified"] = notified
    got = await page("events", q, "at", limit, offset)
    got["kinds"] = sorted(await db().events.distinct("kind"))
    return out(got)


@read("/beampay-events")
async def beampay_events(
    event: str | None = None,
    since: float | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
) -> Any:
    """What BeamPay told us and when — evidence only, never money state (see routers/internal)."""
    q: dict[str, Any] = {**since_query("received_at", since)}
    if event:
        q["event"] = event
    return out(await page("beampay_events", q, "received_at", limit, offset))


@read("/raw/{collection}/{doc_id}")
async def raw(collection: str, doc_id: str) -> Any:
    """One document, verbatim, from an ALLOW-LISTED collection.

    A closed list and not a deny-list: a route that took any name would read `siwe_nonces` (live
    sign-in credentials) the moment someone typed it, and the door would have been opened by a
    collection nobody thought about rather than by a decision."""
    if collection not in RAW_COLLECTIONS:
        raise HTTPException(
            400,
            f"{collection!r} is not one of the collections this panel may read: "
            + ", ".join(RAW_COLLECTIONS),
        )
    doc = await db()[collection].find_one({"_id": doc_id})
    if doc is None:
        raise HTTPException(404, "unknown document")
    return out({"collection": collection, "id": doc_id, "doc": doc})


# ⛔ REGISTERED LAST, AND IT IS NOT DECORATION. Starlette answers a POST on a GET-only route
# with **405**, and a 405 tells an unauthenticated prober that the path exists — which is
# precisely what rule 2 above buys, given away by a verb. (`main.InternalIsLoopbackOnly` exists
# for the same reason on `/internal`: "every method, every spelling and every body must get the
# answer an unrouted path gets".) Verified before this was written: `POST /admin/overview` on a
# box with no key configured answered `405 Method Not Allowed`.
#
# A route that FULL-matches beats an earlier PARTIAL match, so this one takes every method and
# every path under the prefix that the routes above did not claim, runs the SAME guard (it is on
# the router) and answers the same 404 as an unrouted path. It also means a v2 that adds a
# writing verb has to add it deliberately: nothing here can grow a POST by accident.
ANY_METHOD = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@router.api_route("/{rest:path}", methods=ANY_METHOD)
async def unrouted(rest: str) -> Any:
    raise not_found()


@bare.api_route(PREFIX, methods=ANY_METHOD)
async def unrouted_prefix() -> Any:
    """`/admin` itself — see `bare` above. It answers what `/admin/anything-else` answers, which
    is what a path this app does not serve at all answers."""
    raise not_found()


__all__ = ["PREFIX", "bare", "router"]
