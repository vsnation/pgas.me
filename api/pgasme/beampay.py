"""BeamPay — the ONE interface to the Beam wallet's money (operating law 10: BeamPay-only).

Admin, 2026-09-09: *"Avoid using wallet-api. Only Beampay as it counts all balances."* Every
balance, every transaction, every status, every address and every withdrawal on the Beam side
goes through the Pgas BeamPay instance. `pgasme/beam.py` keeps exactly TWO wallet-api calls,
because BeamPay has no endpoint for them:

    invoke_contract   create_tx: false      building the pipe calldata — signs nothing
    process_invoke_data                     the irreversible submit — the only signature

…and every txid those produce is registered HERE, in the same processor step, with
`POST /internal/expect_contract_tx`. Without that registration BeamPay books the whole flow of
a contract tx to the synthetic `__house__` account and the treasury's balance never moves —
which is the same failure class as reading a raw wallet balance as inventory.

Five things about this API are load-bearing and none of them are guessable:

  * **Every amount is an integer groth**, for BEAM and for every confidential asset alike
    (INTEGRATION.md §2). `/balances` answers *strings* keyed by *string* asset ids.
  * **`GET /transactions` takes a JSON BODY**, even though it is a GET, and with no `address`
    the filter is empty — which is the only way to see contract txs at all, because they carry
    `sender == receiver == ""` and an address filter excludes them (api.py:457-478).
  * **`booked` is not "succeeded".** `handle_contract_transaction` sets the flag for a
    cancelled or failed tx too ("terminal and never mined → nothing to book, just stop
    retrying it", process_payments.py:176-186). `booked` means *the daemon is finished with
    this tx*, so settlement is `booked AND status == 3`, never `booked` alone.
  * **`/withdraw` is NOT idempotent and returns no txid.** A retry after a timeout queues a
    SECOND withdrawal. The identity of a withdrawal we made is its **comment**, found in
    `/transactions` — so a resend is resolved by looking, never by calling again.
  * **`{"status": false}` is a refusal, not a failure.** It is HTTP 200 with a reason, and it
    means nothing was queued.

FEES. BeamPay sets the Beam transaction fee of a `/withdraw` itself — 0.001 BEAM to a regular
address, 0.011 to an offline or max-privacy one — and IGNORES the request's `fee` field
(api.py:322, `fee = tx_fee`). `withdraw()` therefore never sends one, and the fee that was
actually charged is read back from the transaction it made (`/transactions[].fee`, and
`/internal/contract_tx/{txid}.fee` for an invocation), because a fee nobody read back is a fee
nobody checked. The BEAM those fees come out of is the treasury address's `available["0"]`
here — never a raw wallet balance.

An unreadable answer is never a value: every method raises `BeamPayError` rather than
returning a default, and a 404 is only ever turned into `None`/`False` where "BeamPay has not
seen this yet" is a genuine, distinguishable answer (`contract_tx`, `is_registered`).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("pgasme.beampay")

# tx_type 12: a shader invocation (bridge PIPE, AMM, deploy). It carries no sender and no
# receiver, so BeamPay books its whole flow to `__house__` unless the txid was registered.
TX_TYPE_CONTRACT = 12

# The refusals `POST /internal/expect_contract_tx` can answer that NO retry can ever clear
# (contract_attribution.REFUSALS). They are conflicts with something already on record — an
# operator owes an attribution repair — so a caller that keeps retrying one is a caller stuck
# forever on a crossing that already happened.
TERMINAL_EXPECT_REFUSALS = frozenset(
    {
        "tx_already_booked",
        "expectation_conflict",
        "expectation_already_used",
        "expectation_expired",
        "expectation_abandoned",
    }
)

# A Beam SBBS ("regular") address is `hex(PeerID)` — 32 bytes, 64 characters — followed by the
# BBS channel in hex WITH ITS LEADING ZEROS STRIPPED, so the string is 64 characters plus a tail
# of 0–8. A max-privacy or offline token is base58 and far longer. Shielding to a regular
# address would "succeed" and shield nothing, so the shape is checked before the first chunk.
#
# ⛔ THE UPPER BOUND WAS 66 AND THAT WAS A GUESS ("64, 66 with the parity byte") — there is no
# parity byte here, the tail is a channel number. It refused the live wallet's own addresses:
# on 2026-09-10 (T3s) `/create_wallet` answered a **67-character** regular address and the UTXO
# split refused to start, with a second 67-character regular address (BeamPay's own "default")
# already sitting in the same address book. 72 = 64 + the 8 hex digits a uint32 channel can
# need. Widening it makes the classifier MORE accurate at all three call sites, and the one
# that demands "NOT regular" (`_prove_mp_address`) therefore becomes stricter, never looser —
# nothing else in this system is a bare 64–72-character hex string.
_SBBS_RE = re.compile(r"^[0-9a-fA-F]{64,72}$")

# `/transactions` answers at most 100 rows per call (api.py:461, `le=100`).
PAGE = 100
# How many pages a scan may spend before it declares the read INCOMPLETE. A scan walks back to
# a timestamp, never to a row count, so this budget is only ever reached when something is very
# wrong — and an exhausted budget RAISES, because "we ran out of pages" must never be allowed
# to read as "there is no such transaction".
MAX_PAGES = 8
# `create_time` is written from the wallet's clock; the same 120 s slack `find_contract_tx`
# already allowed for it applies to a paging floor.
CLOCK_SLACK_S = 120


# Observed health, in the shape `workers.down_checks` already reads (`xchain.health`). ⛔ There is
# deliberately NO separate probe: §8 says the prober must call the way the caller calls, and a
# probe that succeeds while the trading path is failing is worse than no probe. This is what the
# processors themselves saw on their own calls, with their own keys, on their own routes.
health: dict[str, Any] = {"last_ok_at": 0.0, "last_fail_at": 0.0, "last_error": ""}


def reset_health() -> None:
    health.update({"last_ok_at": 0.0, "last_fail_at": 0.0, "last_error": ""})


class BeamPayError(RuntimeError):
    """BeamPay did not answer, or answered an error. NEVER a value."""

    def __init__(self, message: str, status: int | None = None, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def treasury_address() -> str:
    """`PGAS_BEAM_TREASURY_ADDRESS` — the regular address every claim books to and every shield
    spends from. RAISES when it is unset: a balance read with no address is not a zero."""
    addr = (settings.beam_treasury_address or "").strip()
    if not addr:
        raise BeamPayError(
            "PGAS_BEAM_TREASURY_ADDRESS is not configured — there is no address to read a "
            "balance of, and 0 is not an answer"
        )
    return addr


def looks_like_regular_address(address: str) -> bool:
    """True for a 64–72-hex SBBS address — i.e. NOT a max-privacy or offline token."""
    return bool(_SBBS_RE.match((address or "").strip()))


class BeamPay:
    """One BeamPay deployment. Two keys: the ordinary one, and the `ledger:adjust`-scoped one
    that `/internal/*` demands (a scopeless key is refused there with 403, by design)."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        internal_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.url = (url if url is not None else settings.beampay_url).rstrip("/")
        self.key = key if key is not None else settings.beampay_key
        self.internal_key = (
            internal_key if internal_key is not None else settings.beampay_internal_key
        )
        self.timeout = timeout if timeout is not None else settings.beampay_timeout_s

    # ---------------------------------------------------------------- transport

    async def _http(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        json_body: Any,
        headers: dict[str, str],
    ) -> tuple[int, Any]:
        """The ONLY place httpx is touched. Tests replace exactly this, so every key choice,
        every status check and every parse below runs for real."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.HTTPError as e:
            raise BeamPayError(f"{method} {url}: {type(e).__name__}: {e}") from e
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        internal: bool = False,
    ) -> tuple[int, Any]:
        """(status, parsed body). Picks the key the route demands and FAILS CLOSED when it is
        missing — an `/internal/*` call made with the ordinary key is a 403 at best and a
        wrong-key habit at worst, so it is refused before it leaves this process."""
        key = self.internal_key if internal else self.key
        if not key:
            which = "PGAS_BEAMPAY_INTERNAL_KEY" if internal else "PGAS_BEAMPAY_KEY"
            raise BeamPayError(f"{method} {path}: {which} is not configured")
        try:
            status, parsed = await self._http(
                method,
                f"{self.url}{path}",
                params=params,
                json_body=body,
                headers={"X-API-Key": key},
            )
        except BeamPayError as e:
            health["last_fail_at"] = time.time()
            health["last_error"] = str(e)[:200]
            raise
        # ⛔ A 403 or a 404 is BeamPay ANSWERING — a service that refuses us precisely is up, and
        # calling that "down" would page about a configuration fault as though it were an outage.
        # Only a 5xx or a transport failure is the service itself being unreachable.
        if status >= 500:
            health["last_fail_at"] = time.time()
            health["last_error"] = f"{method} {path}: HTTP {status}"
        else:
            health["last_ok_at"] = time.time()
        return status, parsed

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        internal: bool = False,
    ) -> Any:
        status, parsed = await self._request(
            method, path, params=params, body=body, internal=internal
        )
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            raise BeamPayError(
                f"{method} {path}: HTTP {status} {str(detail)[:200]}", status=status, detail=detail
            )
        if parsed is None:
            raise BeamPayError(f"{method} {path}: HTTP {status} with no JSON body", status=status)
        return parsed

    # ---------------------------------------------------------------- balances

    async def balances(self, address: str) -> dict[str, dict[str, str]]:
        """`{"available": {"0": "12345", "36": …}, "locked": {…}}` — string groths, string
        asset-id keys. This is the LEDGER balance of one address, which is the only per-address
        truth that exists on Beam: the wallet itself is one shared UTXO pool."""
        res = await self.call("GET", "/balances", params={"address": address})
        if not isinstance(res, dict) or "available" not in res:
            raise BeamPayError(f"balances({address[:12]}…): unexpected shape {str(res)[:120]}")
        return res

    async def available_groth(self, address: str, asset_id: int) -> int:
        """Spendable groths of ONE asset at ONE address. A missing key is a real zero (the
        address holds none of it); an unparseable one RAISES."""
        avail = (await self.balances(address)).get("available") or {}
        raw = avail.get(str(int(asset_id)), "0")
        try:
            return int(raw)
        except (TypeError, ValueError) as e:
            raise BeamPayError(
                f"balances({address[:12]}…).available[{asset_id}] is {raw!r}, not groths"
            ) from e

    async def locked_groth(self, address: str, asset_id: int) -> int:
        """In-flight groths: a queued `/withdraw` locks here the moment it is accepted, which
        is what makes `locked` the evidence that a withdrawal we lost the answer to was taken."""
        locked = (await self.balances(address)).get("locked") or {}
        raw = locked.get(str(int(asset_id)), "0")
        try:
            return int(raw)
        except (TypeError, ValueError) as e:
            raise BeamPayError(
                f"balances({address[:12]}…).locked[{asset_id}] is {raw!r}, not groths"
            ) from e

    async def is_registered(self, address: str) -> bool:
        """Is this address in BeamPay's OWN address book?

        The BeamPay equivalent of the wallet's `is_mine`, and for our purposes the stronger of
        the two: `/create_wallet` is the only way an address gets in there (INTEGRATION.md §6
        rule 3), so a registered address is one THIS deployment created on the shared hot
        wallet — and an unregistered one is invisible to `/balances`, which means value sent to
        it would never appear in any float we can read.

        404 `Address not found` is a genuine answer ("not in the book"); anything else RAISES,
        a routing 404 from a BeamPay without this route included (see `contract_tx`)."""
        status, parsed = await self._request("GET", "/balances", params={"address": address})
        if status == 404 and _detail_of(parsed) == "Address not found":
            return False
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            raise BeamPayError(
                f"is_registered({address[:12]}…): HTTP {status} {str(detail)[:160]}", status=status
            )
        return True

    # ---------------------------------------------------------------- wallet / addresses

    async def wallet_status(self) -> dict[str, Any]:
        """The wallet's own status through BeamPay: `current_height`, `is_in_sync`, totals."""
        res = await self.call("GET", "/wallet_status")
        body = res.get("result") if isinstance(res, dict) else None
        if not isinstance(body, dict) or "current_height" not in body:
            raise BeamPayError(f"wallet_status: unexpected shape {str(res)[:120]}")
        return body

    async def height(self) -> int:
        return int((await self.wallet_status()).get("current_height") or 0)

    async def validate_address(self, address: str) -> bool:
        """The wallet's verdict, proxied. ⚠️ BeamPay returns ONLY `is_valid` — the wallet's
        `is_mine` and `type` are not exposed by this route (api.py:258-263), so a shield target
        is proven `is_mine` with `is_registered` and non-regular with its SHAPE instead."""
        res = await self.call("GET", "/validate_address", params={"address": address})
        if not isinstance(res, dict) or "result" not in res:
            raise BeamPayError(f"validate_address: unexpected answer {str(res)[:120]}")
        return bool(res["result"])

    async def create_wallet(
        self, note: str, wallet_type: str = "regular", expiration: str = "never"
    ) -> str:
        """THE only way to create an address (INTEGRATION.md §6 rule 3): it is created on the
        wallet AND registered in BeamPay's ledger in one call, so what lands on it is tracked."""
        res = await self.call(
            "POST",
            "/create_wallet",
            body={"note": note, "wallet_type": wallet_type, "expiration": expiration},
        )
        addr = res.get("address") if isinstance(res, dict) else None
        if not isinstance(addr, str) or not addr:
            raise BeamPayError(f"create_wallet: no address in {str(res)[:120]}")
        return addr

    # ---------------------------------------------------------------- transactions

    async def transactions(
        self, address: str | None = None, count: int = PAGE, skip: int = 0
    ) -> list[dict[str, Any]]:
        """One page of history, NEWEST FIRST.

        ⚠️ The parameters go in a JSON BODY even though this is a GET (api.py:457). With no
        `address` the filter is empty and contract txs are included — with one, they are not,
        because their `sender`/`receiver` are `""`."""
        body: dict[str, Any] = {"count": int(count), "skip": int(skip)}
        if address:
            body["address"] = address
        res = await self.call("GET", "/transactions", body=body)
        rows = res.get("txs") if isinstance(res, dict) else None
        if not isinstance(rows, list):
            raise BeamPayError(f"transactions: unexpected shape {str(res)[:120]}")
        return [_normalise(r) for r in rows if isinstance(r, dict)]

    async def _walk(
        self,
        address: str | None,
        since_ts: float,
        visit: Callable[[dict[str, Any]], bool],
        max_pages: int = MAX_PAGES,
    ) -> None:
        """Page backwards through history until `since_ts`, handing every row to `visit`.

        `visit` returns True to stop early. The walk is bounded by TIME, not by a row count: a
        budget that runs out before reaching `since_ts` RAISES, because an incomplete scan that
        answers "nothing found" is exactly the shape of guard that fails open at the first
        moment it matters."""
        floor = float(since_ts) - CLOCK_SLACK_S
        for page in range(max_pages):
            rows = await self.transactions(address, count=PAGE, skip=page * PAGE)
            for row in rows:
                if visit(row):
                    return
            if len(rows) < PAGE:
                return  # the history itself ended: a complete read
            if rows and int(rows[-1].get("create_time") or 0) < floor:
                return  # walked past the floor: a complete read of the window that matters
        raise BeamPayError(
            f"transactions: {max_pages} pages of {PAGE} did not reach {int(since_ts)} — this is "
            f"an INCOMPLETE read, never 'no such transaction'"
        )

    async def contract_tx(self, txid: str) -> dict[str, Any] | None:
        """`GET /internal/contract_tx/{txid}` — the settled state of ONE contract tx.

        `/transactions` cannot find these (sender/receiver are `""`), so this is the only route
        that answers. Returns None for 404 **`tx_not_found`**, which is the genuine answer
        "BeamPay's processor has not seen this transaction yet" and the normal state for seconds
        after a submit. Anything else RAISES — including any OTHER 404.

        ⛔ A ROUTING 404 IS NOT AN ANSWER. FastAPI replies to a request for a route it does not
        have with 404 `{"detail": "Not Found"}`, and BeamPay master `e09bfc2` — the commit this
        deployment was copied from — contains NO `/internal/*` routes at all (the attribution
        machinery exists only in the reference implementation's working tree). Mapping every 404 to None made a
        version-skewed BeamPay indistinguishable from "the processor has not caught up": every
        payout would wait in `bridging` and every claim in `claiming`, with no error, no health
        degradation and no alert until the SLA. An unreadable query is not evidence of anything.

        ⚠️ `booked` is the daemon's idempotency flag, NOT success: it is set for a cancelled or
        failed tx too (process_payments.py:176-186). Settlement is `booked AND status == 3`."""
        status, parsed = await self._request(
            "GET", f"/internal/contract_tx/{txid}", internal=True
        )
        if status == 404 and _detail_of(parsed) == "tx_not_found":
            return None
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            raise BeamPayError(
                f"contract_tx({txid[:12]}…): HTTP {status} {str(detail)[:160]}",
                status=status,
                detail=detail,
            )
        if not isinstance(parsed, dict) or "booked" not in parsed:
            raise BeamPayError(f"contract_tx({txid[:12]}…): unexpected shape {str(parsed)[:120]}")
        return parsed

    async def find_contract_tx(
        self,
        cid: str,
        asset_id: int,
        since_ts: float,
        expect_amount: int,
        exclude_txids: Iterable[str] = (),
        is_taken: Callable[[str], Awaitable[bool]] | None = None,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, Any] | None:
        """The contract transaction a lost `process_invoke_data` response may have made,
        identified by IDENTITY — never by presence. Same three discriminators as before, read
        from BeamPay's own history instead of the wallet's `tx_list`:

          * `expect_amount` is the SIGNED movement of `asset_id` this call would have made.
            ⛔ **A POSITIVE invoke amount is a wallet OUTFLOW** — BeamPay says so in its own
            arithmetic (`process_payments.py:216`, and `attribution_deltas` books
            `available_delta = -amount`), and the chain agrees: on the AMM pipe a live swap
            reads `[{asset 0: +500125656929}, {asset 37: −5055196828}]`, BEAM spent positive,
            the asset received negative. So a b2e send is **+(amount + relayerFee)** and a
            claim is **−value**. Direction AND size.
          * `exclude_txids` / `is_taken` — a txid that is already somebody's evidence can never
            be this row's, and the database is asked one exact txid at a time, last, only about
            a transaction that already matches everything else.
          * newest first, which is what BeamPay's `/transactions` already sorts by (api.py:474).
            ⛔ EVERY match is collected, not just the first: `is_taken` is asked afterwards, and
            a walk that stopped at the newest match left that loop with exactly one row to
            iterate — so a newest match already booked to another crossing made this answer
            None instead of falling through to the older row that really is ours."""
        if not int(expect_amount):
            raise BeamPayError(
                "find_contract_tx needs the signed amount it is looking for — a match on the "
                "pipe alone adopts somebody else's transaction"
            )
        seen = {str(t) for t in exclude_txids if t}
        candidates: list[dict[str, Any]] = []

        def visit(row: dict[str, Any]) -> bool:
            # ⛔ NEVER int() A FIELD PRODUCTION FILLS WITH A WORD. BeamPay writes `type` as the
            # STRING "withdrawal" at reservation time (process_payments.py:1605) — 2,628 of the
            # newest 3,000 rows on the live wallet — and this walk runs with NO address filter
            # (the only way a contract tx, whose sender and receiver are both "", is visible at
            # all), so those rows are handed to it intermixed. `int(row.get("type", -1))` was
            # evaluated BEFORE the `type_string` fallback (`and` cannot short-circuit an
            # argument), so the whole lost-response resolver died with `ValueError: invalid
            # literal for int() with base 10: 'withdrawal'` on both money paths.
            if (
                str(row.get("type")) != str(TX_TYPE_CONTRACT)
                and str(row.get("type_string") or "") != "contract"
            ):
                return False
            if int(row.get("create_time") or 0) < int(since_ts) - CLOCK_SLACK_S:
                return False
            txid = str(row.get("txId") or "")
            if not txid or txid in seen:
                return False
            if not moves(row, cid, asset_id, expect_amount):
                return False
            candidates.append(row)
            return False  # keep walking: `is_taken` below must be able to reach the next match

        await self._walk(None, since_ts, visit, max_pages=max_pages)
        for row in candidates:
            # asked LAST, and only about a transaction that already matches on every other
            # discriminator: `is_taken` is a database round trip, not a set lookup.
            if is_taken is not None and await is_taken(str(row["txId"])):
                continue
            return row
        return None

    async def find_tx_by_id(
        self, txid: str, since_ts: float, max_pages: int = MAX_PAGES
    ) -> dict[str, Any] | None:
        """ONE transaction of ANY kind, matched on the id **we** chose (§IDENTITY-BEATS-BALANCE).

        ⚠️ THE WALK CARRIES NO ADDRESS FILTER, AND IT HAS TO. `/transactions` filters on
        `sender`/`receiver` (api.py:468), and the transaction this exists for — a `tx_split` —
        has BOTH of them EMPTY: measured on the box 2026-09-10 17:55Z, tx
        `390def92c7deb2e9c0274b73daaeef57` came back `sender: '' receiver: ''
        sender_identity: '' receiver_identity: ''`, NOT the fresh walletID
        `simple_transaction.cpp:36-42` puts in MyID and PeerID (which is what BeamPay patch #10
        was written against, and why it refused to book a live split until #10b). Either way no
        address filter can match it; the unfiltered walk can, because `process_payments.py:479`
        inserts every non-contract tx into `db.txs` whether or not it can book it.

        ⛔ `/internal/contract_tx/{txid}` is NOT the route for this: it answers
        `400 not_a_contract_tx` for anything that is not tx_type 12 (api.py:997).

        None means "BeamPay's processor has not recorded it yet" — and only that, because
        `_walk` RAISES on an incomplete read rather than letting a short scan read as absence."""
        want = str(txid or "").lower()
        if not want:
            raise BeamPayError("find_tx_by_id needs a txid — a walk with nothing to match is not a search")
        found: dict[str, Any] = {}

        def visit(row: dict[str, Any]) -> bool:
            if str(row.get("txId") or "").lower() != want:
                return False
            found.update(row)
            return True

        await self._walk(None, since_ts, visit, max_pages=max_pages)
        return found or None

    async def find_txs_by_comments(
        self,
        address: str,
        comments: Iterable[str],
        since_ts: float,
        max_pages: int = MAX_PAGES,
    ) -> dict[str, list[dict[str, Any]]]:
        """`{comment: [every transaction carrying it, newest first]}` for one address.

        ⛔ THIS IS THE IDEMPOTENCY OF `/withdraw`. The route is not idempotent and answers no
        txid, so the only thing that can tell a resend from a first send is the comment WE
        chose — looked up here before any retry, exactly as INTEGRATION.md §4 demands.

        A LIST and not a single row on purpose: two transactions carrying one comment is a
        DOUBLE SEND of the same chunk, which is the exact accident this route makes possible
        and the exact accident a `{comment: row}` map would hide by keeping one and discarding
        the other. The caller uses the newest and says the rest out loud.

        ⚠️ The walk cannot stop early. A comment found on page one may have a twin on page two,
        so it reads the whole window back to `since_ts` — which is what makes a duplicate
        VISIBLE rather than merely possible."""
        want = {str(c) for c in comments if c}
        found: dict[str, list[dict[str, Any]]] = {}
        if not want:
            return found

        def visit(row: dict[str, Any]) -> bool:
            c = str(row.get("comment") or "")
            if c in want:
                found.setdefault(c, []).append(row)
            return False

        await self._walk(address, since_ts, visit, max_pages=max_pages)
        return found

    # ---------------------------------------------------------------- movers

    async def withdraw(
        self, from_address: str, to_address: str, asset_id: int, amount: int, comment: str
    ) -> dict[str, Any]:
        """Queue a transfer. ⛔ NOT idempotent and it returns NO txid — the caller must record
        that it made this call BEFORE making it, and resolve a lost answer by `comment`.

        `{"status": false, "msg": …}` is a 200: a REFUSAL (insufficient funds), meaning nothing
        was queued. `fee` is deliberately not sent — the server sets it and ignores ours.

        ⛔ THE KILL SWITCH IS CHECKED HERE, INSIDE THE MOVER, exactly as `beam.Wallet.submit`
        checks it before `process_invoke_data` — one file, one implementation, checked before
        every irreversible step. This is the project's OTHER irreversible call (it moves the
        treasury's shielded asset), and it had no check of its own: the guard lived in
        `_treasury_shielding`, so an operator who touched the stop file after that caller's last
        look still got the chunk queued, and the next call site — the any-asset branch, an ops
        script, a repair tool — would have inherited nothing at all."""
        # imported late: `beam` imports this module at module scope
        from .beam import Halted
        from .workers import paused

        if paused():
            raise Halted(
                f"withdraw {int(amount)} of asset {int(asset_id)} ({comment}): the kill switch "
                f"is set ({settings.stop_file})"
            )
        res = await self.call(
            "POST",
            "/withdraw",
            body={
                "from_address": from_address,
                "to_address": to_address,
                "asset_id": int(asset_id),
                "amount": int(amount),
                "comment": comment,
            },
        )
        if not isinstance(res, dict) or "status" not in res:
            raise BeamPayError(f"withdraw: unexpected answer {str(res)[:160]}")
        return res

    async def expect_contract_tx(self, txid: str, address: str, trade_ref: str) -> dict[str, Any]:
        """Claim, IN ADVANCE, that this contract txid belongs to this address.

        Carries no amount and cannot: the amounts come from the settled tx's own `invoke_data`
        + `fee`, the identical source the house booking reads. It only decides WHICH address a
        flow the chain has already fixed lands on, so it can move nothing by itself — and it is
        idempotent on the txid, which is why retrying it is safe."""
        return await self.call(
            "POST",
            "/internal/expect_contract_tx",
            body={"txid": str(txid), "address": str(address), "trade_ref": str(trade_ref)},
            internal=True,
        )

    async def expectation_route_ready(self) -> dict[str, Any]:
        """Side-effect-free probe: is direct attribution available here, and does our scoped
        key reach it? Probing by POSTing a real registration would leave a claim behind on a
        txid that may never exist."""
        return await self.call("GET", "/internal/expect_contract_tx", internal=True)

    # ------------------------------------------- self-transactions (BeamPay patch #10, T36c)

    async def expect_self_tx(
        self,
        txid: str,
        address: str,
        trade_ref: str,
        *,
        kind: str,
        asset_id: int,
        expected_fee_groth: int | None = None,
    ) -> dict[str, Any]:
        """Claim, IN ADVANCE, that this SELF-transaction's kernel fee belongs to this address.

        A `tx_split` is `TxType::Simple` with sender == receiver == an address the WALLET
        invented, so BeamPay books nothing for it and its kernel fee leaves the ledger reading
        above the wallet for ever. This registration is what turns that into a booking — and
        like `expect_contract_tx` it carries no amount and cannot: the fee comes from the
        settled transaction's own `fee` field. `expected_fee_groth` is our PREDICTION, which
        BeamPay never books; it only decides whether a wildly different charge is said out loud.

        ⛔ IT MUST BE SENT BEFORE THE WALLET IS ASKED FOR THE TRANSACTION. BeamPay refuses a
        registration for a tx it has already finished with (`409 tx_already_booked`), because
        the branch that would consume it has already run. Idempotent on the txid, so a retry of
        a POST whose reply was lost is safe."""
        body: dict[str, Any] = {
            "txid": str(txid),
            "address": str(address),
            "trade_ref": str(trade_ref),
            "kind": str(kind),
            "asset_id": int(asset_id),
        }
        if expected_fee_groth is not None:
            body["expected_fee_groth"] = int(expected_fee_groth)
        return await self.call(
            "POST", "/internal/expect_self_tx", body=body, internal=True
        )

    async def self_tx(self, txid: str) -> dict[str, Any] | None:
        """`GET /internal/self_tx/{txid}` — the registration and what was booked under it.

        THIS IS THE AUTHORITY, not our own POST reply: the reply can be lost in transit while
        the booking still happens later, in BeamPay's processor, so what actually became of the
        fee is this row. `status` is the answer — `consumed` carries `booked` and the fee that
        was really charged; `abandoned`/`expired` carry `error`.

        None means **`expectation_not_found`** — BeamPay has this route and has never heard of
        that txid.

        ⛔ A ROUTING 404 IS NOT AN ANSWER, and here the two 404s mean opposite things. A build
        without patch #10 answers `{"detail": "Not Found"}` for every txid, and collapsing that
        to None would read a version-skewed BeamPay as "you never registered it" — which is the
        one sentence that would make the command give up on a fee that IS booked."""
        status, parsed = await self._request(
            "GET", f"/internal/self_tx/{txid}", internal=True
        )
        if status == 404 and _detail_of(parsed) == "expectation_not_found":
            return None
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            raise BeamPayError(
                f"self_tx({txid[:12]}…): HTTP {status} {str(detail)[:160]}",
                status=status,
                detail=detail,
            )
        if not isinstance(parsed, dict) or "status" not in parsed:
            raise BeamPayError(f"self_tx({txid[:12]}…): unexpected shape {str(parsed)[:120]}")
        return parsed

    async def self_tx_route_ready(self) -> dict[str, Any] | None:
        """Side-effect-free preflight: does THIS deployment book self-transactions?

        `None` means it does not — the route is absent, i.e. BeamPay has not had patch #10
        applied — and that is a real answer, distinguished from every other 404 by FastAPI's
        own routing-miss `detail` of `Not Found`. Anything unreadable RAISES instead, because
        "we could not ask" is not "the answer is no" (law 8), and the two lead to opposite
        decisions about whether a split may run."""
        status, parsed = await self._request(
            "GET", "/internal/expect_self_tx", internal=True
        )
        if status == 404 and _detail_of(parsed) == "Not Found":
            return None
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            raise BeamPayError(
                f"self_tx_route_ready: HTTP {status} {str(detail)[:160]}",
                status=status,
                detail=detail,
            )
        if not isinstance(parsed, dict) or "available" not in parsed:
            raise BeamPayError(f"self_tx_route_ready: unexpected shape {str(parsed)[:120]}")
        return parsed


def _detail_of(parsed: Any) -> str:
    """FastAPI's `detail` string, or "" when the body is not one. The DIFFERENCE between "this
    deployment has no such route" (`Not Found`) and "this deployment has not seen it yet"
    (`tx_not_found`, `Address not found`) lives in exactly this field."""
    if isinstance(parsed, dict):
        d = parsed.get("detail")
        if isinstance(d, str):
            return d
    return ""


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    """BeamPay stores the txId as the document `_id`; every caller in this project reads
    `txId`. Normalised HERE, at the boundary, so no call site has to remember."""
    if "txId" not in row and "_id" in row:
        return {**row, "txId": row["_id"]}
    return row


def moves(tx: dict[str, Any], cid: str, asset_id: int, expect_amount: int) -> bool:
    """True when this transaction's invoke data moves exactly `expect_amount` of `asset_id` on
    the pipe `cid` — the signed amount, so direction as well as size.

    ⛔ THE SIGN IS BEAMPAY'S, NOT OURS: a POSITIVE invoke amount is a wallet OUTFLOW
    (process_payments.py:216 `# A POSITIVE invoke amount is a wallet OUTFLOW`, and
    `attribution_deltas` books `available_delta = -amount`). A b2e send is therefore positive
    and a claim negative — see `SEND_IS_POSITIVE` in the tests, which pins a live row."""
    for item in tx.get("invoke_data") or []:
        if str(item.get("contract_id") or "") != str(cid):
            continue
        for a in item.get("amounts") or []:
            if int(a.get("asset_id", -1)) != int(asset_id):
                continue
            if int(a.get("amount") or 0) == int(expect_amount):
                return True
    return False


_client: dict[str, BeamPay | None] = {"c": None}


def beampay() -> BeamPay:
    """The process's BeamPay client (tests replace this with a FakeBeamPay)."""
    if _client["c"] is None:
        _client["c"] = BeamPay()
    return _client["c"]


def set_beampay(c: BeamPay | None) -> None:
    _client["c"] = c
