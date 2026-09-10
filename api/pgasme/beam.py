"""The Beam side: the TWO wallet-api calls BeamPay has no endpoint for, the b2e relayer fee,
and a read-only CLI.

⛔ **BeamPay is the ONLY interface to the Beam wallet** (operating law 10, admin 2026-09-09:
*"Avoid using wallet-api. Only Beampay as it counts all balances."*). Balances, transactions,
statuses, addresses, withdrawals and shielding all live in `pgasme/beampay.py`. This file keeps
exactly two wallet-api methods, because BeamPay has no endpoint for either:

    invoke_contract, create_tx:false   build the pipe calldata (view_incoming, receive, send)
    process_invoke_data                the irreversible submit — the only signature we make

Anything else against `:10001` is a defect. `invoke()` therefore takes no `create_tx` parameter
at all: the flag it would set is the difference between a read and a signature, and a value a
caller can pass is a value a caller can get wrong.

Every txid `submit` returns is registered with BeamPay in the same processor step
(`POST /internal/expect_contract_tx`) or the flow books to `__house__` and the treasury's
balance never moves — the same failure class as reading a raw wallet balance as inventory.

Two movers remain here and each is written the way the founder's live movers are written
(`bridge_test/bridge_watcher.py`, `arb_tracker/rebal5_beth_to_eth.py`):

  receive(cid, msg_id)     CLAIM an e2b message   invoke → assert cid in the raw calldata →
                           process_invoke_data → txid
  bridge_send(...)         b2e, bETH → ETH        …→ assert the RECEIVER and the pipe CID are in
                           the bytes we are about to sign → process_invoke_data → txid

The shield is no longer here at all: it is a BeamPay `/withdraw` from the treasury address to
our max-privacy address, and its identity is its comment (`payouts._treasury_shielding`).

Three laws are structural here, not advisory:

  * ⛔ **The kill switch is checked INSIDE the mover**, immediately before the irreversible call,
    so a switch thrown mid-chain halts the chain where it is. One file, one implementation:
    `workers.paused()`.
  * ⛔ **A retry never re-signs.** `process_invoke_data` takes no txId, so a lost response cannot
    be resolved by repeating the call — it is resolved by FINDING the transaction, in BeamPay's
    own history (`beampay.BeamPay.find_contract_tx`), exactly as
    `bridge_watcher.resolve_unconfirmed_contract` does against the wallet.
  * **An unreadable answer is not a value.** Every method raises `BeamError` rather than
    returning a default; `view_incoming` returning nothing means "the relayer has not delivered
    yet", and a transport failure is never allowed to look like that.

`view_incoming` keys are **`MsgId`** (capital M, capital I) — a lowercase match waits forever
(bridge_watcher.py:559). `local_msg_count` returns the HIGHEST id, not a length, so the message
we just sent is found by identity (receiver + amount), never by an index. And every call uses the
PIPE cid; the asset-owner cid answers `count: 0` and a `get_pk` nobody can claim.

FEES. This file owns exactly one fee and it is the only one anybody sets: the b2e **relayer**
fee (`relayer_fee_groth`), `arb_tracker/bridge_fee.py` port-for-port, passed into the pipe
invocation as `relayerFee=<groth>` — so whatever it returns leaves our balance. The Beam
transaction fee of the invocation itself is set by the WALLET: no `fee` field is sent on
`invoke_contract` (exactly as `rebal5_beth_to_eth.py:261` and `bridge_watcher.py:320` build
theirs), and it is read BACK from BeamPay after settlement rather than assumed
(`payouts.fee_charged`). The withdrawal fee is BeamPay's and is never ours to send. See the
FEES section of `pgasme/payouts.py` for the whole picture.

CLI (read-only, nothing is ever sent — with TWO named exceptions):
    python -m pgasme.beam status
    python -m pgasme.beam dry-run --payout <request_id>
    python -m pgasme.beam dry-run --claim <deposit_id>
    python -m pgasme.beam repair-fee --txid <contract txid> [--apply] [--adjust-id <id>]
        the one-off operator repair for a contract tx made by the OLD claim path, which
        submitted before it registered the txid: BeamPay booked the whole flow to `__house__`,
        including the BEAM fee. `--apply` posts one ZERO-SUM `POST /internal/ledger/adjust`
        (asset 0, treasury → `__house__`, `after_tx` = the settled claim) and nothing else; the
        default is a dry run that prints the body and every gate the route will apply. It never
        reaches the wallet's signing path, and the kill switch is checked before it posts.
    python -m pgasme.beam replan-shield --deposit <deposit id> [--apply]
        the chunk table of one deposit's shielding, read from BeamPay's own history, and which
        FAILED chunks a re-plan would hand back to the processor. `--apply` clears the hold and
        marks those chunks unsent so the processor re-sends them, each to its own FRESH
        max-privacy address (see `payouts.shield_target`: consecutive sends to ONE max-privacy
        address collide). It refuses while any chunk is pending or unresolved — that would race
        the wallet — writes no value itself, and checks the kill switch before it writes at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import sys
import time
from typing import Any

import httpx

from . import beampay
from .assets import ASSETS, Asset, get_asset, usd_prices
from .beampay import BeamPayError
from .config import relayer_subsidy, settings
from .db import db

log = logging.getLogger("pgasme.beam")

# Beam wallet transaction statuses, now read from BeamPay (`/internal/contract_tx/{txid}.status`
# and `/transactions[].status` — the same integers the wallet answers, because BeamPay stores
# them verbatim). Named, because `== 3` scattered through a money path is how "cancelled" and
# "completed" end up one typo apart.
TX_PENDING, TX_IN_PROGRESS, TX_CANCELLED, TX_COMPLETED, TX_FAILED, TX_REGISTERING = 0, 1, 2, 3, 4, 5
TX_SETTLED = (TX_COMPLETED,)
TX_DEAD = (TX_CANCELLED, TX_FAILED)
TX_IN_FLIGHT = (TX_PENDING, TX_IN_PROGRESS, TX_REGISTERING)

# bridge_fee.py, verbatim — these are the RELAYER's own constants, not ours to tune.
RELAY_COSTS_IN_GAS = 120_000
BASE_FEE_MULTIPLIER = 2
FEE_HISTORY_BLOCKS = 10
PRIORITY_FEE_PERCENTILE = 50
MIN_PRIORITY_FEE_GWEI = 0.01
MAX_PRIORITY_FEE_GWEI = 3.0

GROTH = 10**8
# how far back from local_msg_count we look for the message we just sent
LOCAL_MSG_WINDOW = 40


# §9.7: "logs are scrubbed of W at the logging layer with tests". An address in api.log pairs
# request_id → destination wallet, and payout_requests pairs request_id → account_id, so a log
# line is the whole deanonymisation. This is that layer: everything that reaches `log`, a
# Telegram row or an exception message goes through it.
# The lookarounds matter: a pipe cid is 64 hex chars and a Beam address is 66, and a plain
# `[0-9a-f]{40}` would eat 40 characters out of the MIDDLE of one and make the log unreadable
# without hiding anything. Only a hex run that is exactly an address (optionally 0x-prefixed)
# is replaced.
_EVM_RE = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{40}(?![0-9a-fA-F])")


def redact(text: object) -> str:
    """`text` with every EVM address replaced by a marker. Ids stay; addresses never do."""
    return _EVM_RE.sub("<redacted-address>", str(text))


class BeamError(RuntimeError):
    """The wallet-api did not answer, or answered an error. NEVER a value."""


class Halted(RuntimeError):
    """The kill switch was set before an irreversible call. A refusal, not a failure."""


def _paused() -> bool:
    """THE kill switch — one file, one implementation (workers.paused). Imported late so the
    worker module can import this one without a cycle."""
    from .workers import paused

    return paused()


def _blob(raw: Any) -> bytes:
    """The raw invoke data as bytes, whether the wallet answered a byte array or hex."""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(raw, str):
        return bytes.fromhex(raw.removeprefix("0x"))
    if isinstance(raw, list):
        return bytes(int(b) & 0xFF for b in raw)
    raise BeamError(f"raw_data is a {type(raw).__name__}, not bytes")


# ⛔ **`;`, NOT `,`.** The wallet splits the whole `args` string on `,` in
# `ProcessorManager::AddArgs` (bvm2.cpp:3373) BEFORE the shader ever sees a value, so
# `indexes=1,2,3` reaches it as three broken pairs — the list would silently become `1`, and a
# message on index 2 or 3 would then be invisible with the money already burned. The shader
# treats any non-digit as a separator and `;` is the convention (K1, WO-20260910-3). It lives
# here once, so the two spellings of one fact cannot drift (law 9).
INDEX_LIST_SEP = ";"


def _index_arg(index: int | None) -> str:
    """`,index=N` for an issued receiver key — and NOTHING for the legacy one.

    The absence of the argument is the interface's own way of saying "the cid-only blob", so a
    legacy call must not carry `index=0`: that would be a different string reaching a shader
    that is entitled to read it differently."""
    return f",index={int(index)}" if index is not None and int(index) > 0 else ""


def _shader_error(built: dict[str, Any]) -> str:
    """The shader's own refusal text out of an `invoke_contract` answer that carries no
    `raw_data`. Empty when it did not say — which is not the same thing as it not having
    refused, so the caller still refuses either way."""
    err = built.get("error")
    if isinstance(err, str) and err:
        return err
    out = built.get("output")
    if isinstance(out, str) and out:
        try:
            body = json.loads(out)
        except ValueError:
            return out[:160]
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return body["error"]
    return ""


def _msg_index(m: dict[str, Any]) -> int | None:
    """Which receiver key a delivered message matched, or None when the shader did not say.

    The patched shader answers the index beside the MsgId and spells the legacy key either `0`
    or `"legacy"`. NONE IS NOT ZERO: a shader that answers no index at all is one that has no
    concept of them, and the row it produced is byte-identical to the rows this call has always
    returned. That distinction is what keeps `view_incoming` unchanged with the flag off — and
    an unparseable index is None too, because a value we cannot read is never a value."""
    if "index" not in m and "Index" not in m:
        return None
    raw = m.get("index", m.get("Index"))
    if raw is None:
        return None
    if str(raw).strip().lower() == "legacy":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("view_incoming answered an unreadable index %r — treating it as unknown", raw)
        return None


class Wallet:
    """One dedicated wallet-api. Every method raises BeamError rather than guessing."""

    def __init__(self, url: str | None = None, timeout: float | None = None) -> None:
        self.url = url or settings.beam_wallet_api
        self.timeout = timeout if timeout is not None else settings.beam_wallet_timeout_s
        self.shader = settings.beam_shader
        self._id = 0

    # ---------------------------------------------------------------- transport

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params:
            body["params"] = params
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.url, json=body)
            answer = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise BeamError(f"{method}: {type(e).__name__}: {e}") from e
        if not isinstance(answer, dict):
            raise BeamError(f"{method}: unexpected answer {str(answer)[:120]}")
        if "error" in answer:
            raise BeamError(f"{method}: {json.dumps(answer['error'])[:200]}")
        if "result" not in answer:
            raise BeamError(f"{method}: no result in {str(answer)[:120]}")
        return answer["result"]

    # ---------------------------------------------------------------- shader views
    #
    # ⛔ There are no balance, status, history or address methods on this client any more.
    # `wallet_status`, `totals`, `float_groth`, `regular_groth`, `tx_status`, `tx_list`,
    # `generate_tx_id`, `create_address` and `validate_address` are BeamPay's (law 10) — the
    # wallet-api answers what the WALLET holds, and what the wallet holds is one shared UTXO
    # pool, not our inventory. `tx_send` is gone with them: a shield is a BeamPay `/withdraw`.
    #
    # ⚠️ TWO EXCEPTIONS, BOTH ADDED 2026-09-10 AND BOTH NARROW BY CONSTRUCTION — `get_utxo`,
    # which reads the coin COUNT, and `tx_split` (see `split()` below), which cuts one of our
    # own coins into many WITHOUT moving value out of the wallet. Both are about the same fact:
    # Beam locks a whole UTXO per pending transaction, so the COUNT of coins is what bounds
    # concurrency, and BeamPay's per-address ledger cannot express a count at all.
    #
    # `get_utxo`. It is not a
    # balance — it is the COUNT of coins, which is the one fact BeamPay's per-address ledger
    # cannot express and which decides how many transactions the wallet can carry at once (Beam
    # locks a whole UTXO per pending transaction). BeamPay has no route for it, it moves
    # nothing, and it is never read as inventory: `payouts.coin_counts` uses it for concurrency
    # and for nothing else. Two releases 0.7 s apart both died "Not enough inputs" on
    # 2026-09-10 for want of exactly this number.

    async def utxos(self, page: int = 200, max_pages: int = 25) -> list[dict[str, Any]]:
        """Every UTXO the wallet knows about, paged — a READ that moves nothing.

        ⛔ RAISES on an unreadable page rather than returning what it managed to collect: a
        short list is an UNDERCOUNT of coins, and an undercount here reads as "the wallet is
        busy", which stalls releases silently and for ever. The caller must know it could not
        look. A walk that hits `max_pages` says so in the log for the same reason."""
        out: list[dict[str, Any]] = []
        for _ in range(max_pages):
            rows = await self.rpc("get_utxo", {"count": int(page), "skip": len(out)})
            if not isinstance(rows, list):
                raise BeamError(f"get_utxo: unexpected shape {str(rows)[:120]}")
            out.extend(r for r in rows if isinstance(r, dict))
            if len(rows) < page:
                return out
        log.warning(
            "get_utxo: stopped after %d pages (%d coins) — the count may be short, which reads "
            "as a busier wallet than it is", max_pages, len(out),
        )
        return out

    async def invoke(self, args: str) -> dict[str, Any]:
        """`invoke_contract` with **create_tx: false**, always — one of the only two wallet-api
        calls this project makes.

        ⛔ There is deliberately no `create_tx` parameter. The flag is the difference between a
        read and a signature, and a value a caller can pass is a value a caller can get wrong;
        with it fixed here, a `create_tx: true` call cannot be expressed anywhere in the
        codebase. The signature is made by `submit()` and nowhere else."""
        res = await self.rpc(
            "invoke_contract",
            {"contract_file": self.shader, "args": args, "create_tx": False},
        )
        if not isinstance(res, dict):
            raise BeamError(f"invoke_contract: unexpected shape {str(res)[:120]}")
        return res

    async def view(self, args: str) -> dict[str, Any]:
        """A read-only shader view. `output` is a JSON string; an unparseable one RAISES."""
        res = await self.invoke(args)
        out = res.get("output")
        if out in (None, ""):
            raise BeamError(f"{args}: the shader returned no output")
        try:
            body = json.loads(out) if isinstance(out, str) else out
        except ValueError as e:
            raise BeamError(f"{args}: output is not JSON ({str(out)[:80]})") from e
        if not isinstance(body, dict):
            raise BeamError(f"{args}: output is not an object ({str(out)[:80]})")
        return body

    async def get_pk(self, cid: str, index: int | None = None) -> str:
        """The receiver pubkey of this pipe. `index` absent → the LEGACY blob (the cid alone),
        which is the only key the shipped pipe app can derive; `index` ≥ 1 → `KeyID{cid, index}`
        on the patched app (WO-20260910-3).

        ⚠️ THE SHIPPED APP IGNORES `index=` AND ANSWERS THE LEGACY KEY FOR ANY VALUE OF IT
        (measured on the box, T41 §1). A 33-byte answer is therefore NOT evidence that an
        indexed key was derived — `receiver_keys.pk_for_index` is where that is checked, by
        refusing an answer equal to the pipe's configured legacy key."""
        body = await self.view(f"role=user,action=get_pk,cid={cid}{_index_arg(index)}")
        pk = body.get("pk") or body.get("pubkey")
        if not isinstance(pk, str) or not pk:
            raise BeamError(f"get_pk({cid[:12]}…): no pk in {str(body)[:120]}")
        # ⛔ THE ANSWER MUST BE FOR THE INDEX WE ASKED FOR. The patched app echoes it; the
        # shipped one answers no index at all. A key derived from a different blob than the one
        # we will later SIGN with is a receiver whose message we can never claim, so an answer
        # that names another index is refused rather than trusted for its 33 bytes.
        echoed = _msg_index(body)
        if echoed is not None and echoed != int(index or 0):
            raise BeamError(
                f"get_pk({cid[:12]}…): asked for index {int(index or 0)}, the shader answered "
                f"index {echoed}"
            )
        return pk

    async def view_incoming(
        self, cid: str, indexes: list[int] | None = None
    ) -> list[dict[str, int]]:
        """What the relayer has delivered to OUR keys on this pipe and we have not claimed.

        ⚠️ The keys are `MsgId` / `amount` — capital M, capital I. Matching `msgId` yields −1 for
        every row, `mine` is always empty, and a genuinely claimable transfer waits forever
        (bridge_watcher.py:559, verified against the live pipe).

        `indexes` is the EXACT set of receiver indexes to match against, legacy (0) included.
        The patched shader derives a bounded window per call, so an index the caller does not
        ask about is INVISIBLE — never "not delivered yet". Empty or None asks exactly the
        question this call asked before indexed keys existed, and every row then answers
        `index: 0`."""
        args = f"role=manager,action=view_incoming,startFrom=0,cid={cid}"
        if indexes:
            args += ",indexes=" + INDEX_LIST_SEP.join(str(int(i)) for i in indexes)
        body = await self.view(args)
        rows = body.get("incoming")
        if rows is None:
            raise BeamError(f"view_incoming({cid[:12]}…): no 'incoming' key in {str(body)[:120]}")
        out: list[dict[str, int]] = []
        for m in rows:
            for k in ("MsgId", "msgId", "msg_id", "id"):
                if k in m:
                    row = {"msg_id": int(m[k]), "amount": int(m.get("amount") or 0)}
                    if (i := _msg_index(m)) is not None:
                        row["index"] = i
                    out.append(row)
                    break
        return out

    async def local_msg_count(self, cid: str) -> int:
        body = await self.view(f"role=user,action=local_msg_count,cid={cid}")
        n = body.get("count")
        if n is None:
            raise BeamError(f"local_msg_count({cid[:12]}…): no count in {str(body)[:120]}")
        return int(n)

    async def local_msg(self, cid: str, msg_id: int) -> dict[str, Any] | None:
        """One outgoing message, or None when that id does not exist (a probe, not an error)."""
        try:
            return await self.view(f"role=user,action=local_msg,cid={cid},msgId={msg_id}")
        except BeamError:
            return None

    async def find_local_msg(
        self,
        cid: str,
        receiver: str,
        amount_groth: int,
        window: int = LOCAL_MSG_WINDOW,
        from_msg_id: int | None = None,
    ) -> int | None:
        """The id of OUR outgoing message, matched on IDENTITY (receiver + amount).

        `local_msg_count` answers the HIGHEST id, not a length, so the id past the count is
        queried too and the walk goes downwards. Nothing here trusts ordering: two crossings
        to the same wallet in the same minute are told apart by their amounts, and a message
        that matches neither is simply not ours.

        ⛔ **THE FLOOR IS THE RELEASE, NOT A WINDOW.** `from_msg_id` is the highest message id
        that existed on this pipe the instant BEFORE our send (`payout_requests.msg_floor`),
        and the walk goes all the way down to it. The fixed `window` it replaces was 40 while
        one pass releases up to `BATCH` = 50 orders: the first ten of a full pass could never
        find their own message again, and this answering None is not "not found yet" — it is
        the disproof that holds the payout, so those orders were permanently unbookable in
        `bridging` with the bETH already burned. The window survives only as the fallback for a
        row written before `msg_floor` existed, and for callers that have no floor to give.

        ⛔ **THE FLOOR IS EXCLUSIVE.** `from_msg_id` is the highest id that existed BEFORE our
        send, so that message is by construction NOT ours — and the walk used to include it
        (`range(top + 1, floor - 1, -1)` reaches `floor`). Two payouts to one wallet for one
        amount are the product's normal shape, so the message the inclusive floor let in was
        exactly the twin most likely to match on (receiver, amount): the PREVIOUS payout's
        message would have proved this crossing, unblocking the irreversible ledger release and
        validating a lost-response adoption on somebody else's kernel."""
        top = await self.local_msg_count(cid)
        floor = int(from_msg_id) + 1 if from_msg_id is not None else max(top - window, 0) + 1
        floor = max(floor, 0)
        want = receiver.lower().removeprefix("0x")
        for msg_id in range(top + 1, floor - 1, -1):
            m = await self.local_msg(cid, msg_id)
            if not m:
                continue
            got = str(m.get("receiver") or "").lower().removeprefix("0x")
            if got == want and int(m.get("amount") or 0) == int(amount_groth):
                return msg_id
        return None

    # ---------------------------------------------------------------- movers

    async def submit(self, raw: Any, what: str) -> str:
        """The irreversible half: `process_invoke_data`. The kill switch is checked HERE — the
        last thing before the wallet signs — so a switch thrown mid-chain halts the chain where
        it is. ⛔ There is no txId parameter, so this call can NEVER be safely repeated: a lost
        response is resolved with `beampay.BeamPay.find_contract_tx`, never by calling again."""
        if _paused():
            raise Halted(f"{what}: the kill switch is set ({settings.stop_file})")
        res = await self.rpc("process_invoke_data", {"data": raw})
        txid = res.get("txid") if isinstance(res, dict) else None
        if not isinstance(txid, str) or not txid:
            raise BeamError(f"{what}: process_invoke_data returned no txid ({str(res)[:120]})")
        return txid

    async def split(self, coins: list[int], asset_id: int, txid: str) -> str:
        """`tx_split` — the wallet cutting ONE of its own coins into many, in one transaction.

        ⚠️ THE THIRD WALLET-API CARVE-OUT, and it is the only one that WRITES. Law 10 says
        BeamPay is the only interface that moves value, and this does not move value: sender and
        receiver are one address the wallet makes for itself (`v6_api_handle.cpp:513` calls
        `walletDB->createAddress`, and `CreateSplitTransactionParameters` sets MyID **and**
        PeerID to it), every output stays in the same wallet, and the only thing that leaves is
        the kernel fee. BeamPay has no `/withdraw` shape that can express it — the equivalent it
        DOES have is a self-transfer, which is nine transactions, nine fees and three Telegram
        notifications per leg for one coin. See `pgasme/utxo.py` for the whole argument and for
        what the fee costs the ledger.

        ⛔ **`txid` IS MANDATORY HERE ALTHOUGH THE WALLET MAKES IT OPTIONAL.** It is the whole
        idempotency of this call: `v6_api_handle.cpp:518` refuses a txId the wallet already
        holds (`ApiError::InvalidTxId`, *"Provided transaction ID already exists in the
        wallet."*), so a caller that derives the id from its plan CANNOT make a second split by
        retrying — the wallet says no, and it says it without signing anything. `submit()` has
        no such parameter and is therefore never repeatable; this one is safe to retry by
        construction, which is the opposite property and worth naming.

        ⛔ **NO `fee` GOES ON THE WIRE** (§WE-SET-IT-WE-DONT-READ-IT). `v6_api_parse.cpp:589`
        defaults it to the wallet's own minimum for this many outputs; `utxo.split_min_fee`
        PREDICTS that number so a dry run can price the plan, and the charged fee is read back
        off BeamPay's record of the transaction and compared with the prediction.

        The kill switch is checked HERE, inside the mover, exactly as `submit` checks it."""
        if not coins or any(int(c) <= 0 for c in coins):
            raise BeamError(
                f"tx_split: `coins` must be a non-empty list of NON-ZERO amounts (got {coins!r})"
                f" — v6_api_parse.cpp:571 refuses a zero amount outright"
            )
        if not re.fullmatch(r"[0-9a-f]{32}", str(txid or "")):
            raise BeamError(
                f"tx_split: {str(txid)[:40]!r} is not a Beam transaction id — 16 bytes, 32 "
                f"lower-case hex characters (wallet/core/common.h:56, parse_utils.h:215)"
            )
        if _paused():
            raise Halted(f"tx_split: the kill switch is set ({settings.stop_file})")
        res = await self.rpc(
            "tx_split",
            {"coins": [int(c) for c in coins], "asset_id": int(asset_id), "txId": str(txid)},
        )
        got = res.get("txId") if isinstance(res, dict) else None
        if not isinstance(got, str) or got.lower() != str(txid).lower():
            raise BeamError(
                f"tx_split: the wallet answered txId {str(got)[:40]!r} and we asked for "
                f"{str(txid)[:40]!r} — a transaction that is not the one we named is not ours"
            )
        return str(got).lower()

    async def build_receive(
        self, cid: str, msg_id: int, index: int | None = None
    ) -> tuple[Any, str]:
        """(raw_data, args) for a CLAIM, with the pipe CID proven present in the bytes.

        Split from `receive` on purpose: this half is a `create_tx:false` read and CANNOT have
        signed anything, so a caller that fails here knows it is safe to plan again.

        `index` is the receiver key the message was delivered to — the SIGNING blob, not the
        contract. `GenerateKernel`'s cid stays the real pipe cid either way; only the key the
        signature is made with changes. Absent (a legacy row) means the cid-only blob, and the
        args are then byte-identical to every claim made before indexed keys existed."""
        args = f"role=user,action=receive,cid={cid},msgId={int(msg_id)}{_index_arg(index)}"
        built = await self.invoke(args)
        raw = built.get("raw_data")
        if not raw:
            # ⛔ NO raw_data IS A REFUSAL AND IS NEVER RETRIED WITH ANOTHER INDEX. The patched
            # app checks the message's stored receiver against the key of the index it was given
            # and refuses BEFORE signing ("receiver key mismatch: …"). Walking indexes until one
            # is accepted is a search for somebody else's message with our signature on it; the
            # right index is the one on the row, and a mismatch is a human's problem.
            raise BeamError(
                f"claim msg {msg_id}: the shader refused to build the claim "
                f"({_shader_error(built) or str(built)[:160]})"
            )
        if bytes.fromhex(cid) not in _blob(raw):
            raise BeamError(f"claim msg {msg_id}: the pipe CID is NOT in the calldata — refusing")
        return raw, args

    async def receive(self, cid: str, msg_id: int, index: int | None = None) -> str:
        """CLAIM one delivered message. Returns the Beam txid."""
        raw, _args = await self.build_receive(cid, msg_id, index)
        return await self.submit(raw, f"claim msg {msg_id}")

    def bridge_args(self, cid: str, groth: int, receiver_eth: str, relayer_fee_groth: int) -> str:
        """The exact `args` string of a b2e send (rebal5_beth_to_eth.py:271). `amount` is what
        the receiver GETS; `relayerFee` is charged on top, so the wallet is debited both."""
        return (
            f"role=user,action=send,cid={cid},amount={int(groth)},"
            f"receiver={receiver_eth},relayerFee={int(relayer_fee_groth)}"
        )

    async def build_bridge_send(
        self, cid: str, groth: int, receiver_eth: str, relayer_fee_groth: int
    ) -> tuple[Any, str, bytes]:
        """(raw_data, args, calldata) with the PRE-BROADCAST ASSERTIONS already passed.

        A wrong receiver or a wrong pipe is unrecoverable, so both must be VISIBLE in the bytes
        we are about to sign — not merely in the string we composed."""
        if int(groth) <= 0 or int(relayer_fee_groth) < 0:
            raise BeamError("a b2e send needs a positive amount and a non-negative relayer fee")
        if not receiver_eth.startswith("0x") or len(receiver_eth) != 42:
            # §9.7: this string reaches the operator's Telegram through payout_build_refused,
            # so the malformed address must not ride along with it.
            raise BeamError(f"receiver {redact(receiver_eth)!r} is not an EVM address")
        args = self.bridge_args(cid, groth, receiver_eth, relayer_fee_groth)
        built = await self.invoke(args)
        raw = built.get("raw_data")
        if not raw:
            raise BeamError(f"b2e send: could not build the pipe call ({str(built)[:160]})")
        blob = _blob(raw)
        if bytes.fromhex(receiver_eth[2:].lower()) not in blob:
            raise BeamError("b2e send: the RECEIVER is NOT in the calldata — refusing")
        if bytes.fromhex(cid) not in blob:
            raise BeamError("b2e send: the pipe CID is NOT in the calldata — refusing")
        return raw, args, blob

    async def bridge_send(
        self, cid: str, groth: int, receiver_eth: str, relayer_fee_groth: int
    ) -> str:
        """b2e: burn `groth + relayerFee` of the asset here, the pipe pays `groth` to
        `receiver_eth` on Ethereum after ~61 Beam confirmations. Returns the Beam txid."""
        raw, _args, _blob_ = await self.build_bridge_send(
            cid, groth, receiver_eth, relayer_fee_groth
        )
        return await self.submit(raw, f"b2e send to {redact(receiver_eth)}")


_wallet: dict[str, Wallet | None] = {"w": None}


def wallet() -> Wallet:
    """The process's wallet client (tests replace this with a FakeWalletApi)."""
    if _wallet["w"] is None:
        _wallet["w"] = Wallet()
    return _wallet["w"]


def set_wallet(w: Wallet | None) -> None:
    _wallet["w"] = w


# --------------------------------------------------------------------- the relayer's own fee


async def max_gas_price_gwei(rpc: Any) -> float:
    """`bridge_fee.estimate_max_gas_price_gwei`, port-for-port.

    maxFeePerGas = baseFee × 2 + median(50th-percentile tips over 10 blocks) clamped to
    [0.01, 3.0] gwei. The base fee is the LAST entry of `baseFeePerGas` — eth_feeHistory returns
    one more than asked for and that extra entry is the block being built now, which is the one
    the relayer will actually pay."""
    h = await rpc.call(
        "eth_feeHistory", [hex(FEE_HISTORY_BLOCKS), "latest", [PRIORITY_FEE_PERCENTILE]]
    )
    if not isinstance(h, dict):
        raise BeamError("eth_feeHistory unreadable — refusing to guess a fee")
    bases = h.get("baseFeePerGas") or []
    if not bases:
        raise BeamError("eth_feeHistory returned no baseFeePerGas")
    base = int(bases[-1], 16) / 1e9
    rewards = [int(r[0], 16) / 1e9 for r in (h.get("reward") or []) if r and r[0] is not None]
    tip = statistics.median(rewards) if rewards else MIN_PRIORITY_FEE_GWEI
    tip = min(max(tip, MIN_PRIORITY_FEE_GWEI), MAX_PRIORITY_FEE_GWEI)
    return base * BASE_FEE_MULTIPLIER + tip


async def relayer_fee_groth(
    asset: Asset, rpc: Any, margin: float | None = None
) -> tuple[int, dict[str, Any]]:
    """(fee in the asset's groth, detail). ⚠️ WE SET THIS NUMBER — nobody quotes it back and
    nobody checks it against a minimum. Too low and the message sits for days; too high and we
    simply hand over the difference. So it is the RELAYER's arithmetic, not ours.

    For ETH the USD terms cancel exactly, as in their code, so no price lookup can distort an
    ETH crossing — and a CoinGecko outage cannot stop one."""
    margin = settings.relayer_fee_margin if margin is None else margin
    gas = await max_gas_price_gwei(rpc)
    if not gas or gas <= 0:
        raise BeamError("eth_feeHistory gave a non-positive gas price")
    detail: dict[str, Any] = {"gas_gwei": gas, "margin": margin, "rate_source": {}}
    if asset.key == "ETH":
        # relay_costs_usd / eth_usd == 120_000 × gas / 1e9, whatever ETH is worth
        fee_units = RELAY_COSTS_IN_GAS * gas / 1e9 * margin
        detail["rate_source"] = {"eth": "cancels"}
    else:
        prices = await usd_prices()
        eth_usd, asset_usd = prices["ETH"], prices[asset.key]
        if asset_usd <= 0:
            raise BeamError(f"no usable USD price for {asset.key}")
        relay_costs = RELAY_COSTS_IN_GAS * gas * eth_usd / 1e9
        fee_units = relay_costs / asset_usd * margin
        detail.update({"eth_usd": eth_usd, "asset_usd": asset_usd, "fee_usd": relay_costs * margin})
        detail["rate_source"] = {"eth": "coingecko", "asset": "coingecko"}
    detail["fee_units"] = fee_units
    return int(round(fee_units * GROTH)), detail


# --------------------------------------------------------------------- the read-only CLI


def _fmt(groth: int | None) -> str:
    return "—" if groth is None else f"{groth / GROTH:.8f}"


async def _explorer_height() -> tuple[int | None, str]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{settings.explorer_base.rstrip('/')}/status")
        return int(r.json()["height"]), ""
    except Exception as e:  # noqa: BLE001 — a read failure is named, never silently a number
        return None, f"{type(e).__name__}: {e}"


async def cmd_status(out: Any = print) -> int:
    """Every BALANCE here is read from **BeamPay**; the wallet-api answers exactly one thing.

    That is the point of law 10: a per-address ledger balance is the only per-address truth
    that exists on Beam, and a raw wallet balance is one shared UTXO pool that includes value
    that is not ours to spend. The treasury address holds claimed-but-not-yet-shielded value
    and the BEAM every claim, shield and pipe send is paid for with; the max-privacy address
    holds the shielded float a payout may cross with.

    ⚠️ THREE TABLES, AND THEY SAY DIFFERENT THINGS (2026-09-10):
      * the **LEDGER** — what we OWN, per address, from BeamPay.
      * the **WALLET** — what it can SPEND today, from `/wallet_status.totals` (still BeamPay,
        which proxies it). The two parted company on 2026-09-10: the registry summed 0.02652864
        bETH and the wallet could spend none of it, because a max-privacy output is locked for
        up to 72 h after it settles. A release gated on the first alone signs a send the wallet
        refuses.
      * the **COINS** — how many spendable UTXOs those buckets are, from the wallet-api's
        `get_utxo` (the one wallet-api READ this project makes; BeamPay has no route for it and
        it moves nothing). Beam locks a whole coin per pending transaction and every contract
        invocation also needs a BEAM coin for its fee, so this is how many crossings can be in
        flight at once — two releases 0.7 s apart died "Not enough inputs" for want of it.
    Plus the working float the shield policy keeps unshielded, and what it is protecting."""
    from . import payouts  # local: payouts imports this module

    bp = beampay.beampay()
    out("Pgas.me Beam treasury — READ ONLY (every number below is BeamPay's)")
    out(f"  beampay    : {bp.url}")
    out(f"  wallet-api : {wallet().url}   (invoke_contract create_tx:false + "
        f"process_invoke_data ONLY)")
    out(f"  shader     : {wallet().shader}")
    try:
        treasury = beampay.treasury_address()
    except BeamPayError as e:
        out(f"  ⛔ {e}")
        return 1
    mp = await payouts.float_address()
    out(f"  treasury   : {treasury}")
    out(f"  max-privacy: {mp or '— not configured (the float cannot be read)'}  (primary: "
        f"the float's first entry and the address a release books to)")
    registry = await payouts.mp_registry()
    out(f"  mp registry: {len(registry)} max-privacy address(es) — the FLOAT column below is the "
        f"SUM over all of them (one fresh address per shield chunk)")
    try:
        st = await bp.wallet_status()  # BeamPay's GET /wallet_status, not the wallet-api's
    except BeamPayError as e:
        out(f"  ⛔ BeamPay unreadable: {e}")
        return 1
    height = int(st.get("current_height") or 0)
    tip, why = await _explorer_height()
    lag = "—" if tip is None else str(tip - height)
    out(f"  height     : wallet {height} · node/explorer {tip if tip else 'unreadable'} "
        f"(lag {lag}){'  ' + why if why else ''}")
    out(f"  in sync    : {st.get('is_in_sync')}")
    try:
        beam_fees = await bp.available_groth(treasury, 0)
    except BeamPayError as e:
        out(f"  BEAM (fees): unreadable ({e})")
    else:
        out(f"  BEAM (fees): {_fmt(beam_fees)}  (treasury, asset 0)")
    out("")
    out("  BeamPay LEDGER — what we OWN (per address; the only per-address truth on Beam)")
    out("  asset   aid  treasury(unshielded)  FLOAT(shielded)  locked")
    for key, asset in ASSETS.items():
        try:
            regular = await bp.available_groth(treasury, asset.aid)
            floated = await payouts.float_groth(bp, asset) if mp else None
            locked = await bp.locked_groth(treasury, asset.aid)
        except BeamPayError as e:
            out(f"  {key:6} {asset.aid:>4}  unreadable: {e}")
            continue
        out(
            f"  {key:6} {asset.aid:>4}  {_fmt(regular):<21} "
            f"{(_fmt(floated) if floated is not None else '—'):<16} {_fmt(locked)}"
        )
    out("")
    # ⛔ AND WHAT THE WALLET CAN SPEND, WHICH IS A DIFFERENT FACT. The table above is the ledger:
    # value that is ours. This one is `/wallet_status.totals` — value that can MOVE today.
    # 2026-09-10: the registry said 0.02652864 bETH and the wallet said it could spend none of
    # it (three shield chunks settled ten hours earlier, still inside the max-privacy lock).
    # Read through the ONE reader the release gate itself uses (law 8: the prober must call the
    # way the caller calls).
    out("  WALLET spendable — what a send can actually fund NOW (the release gate reads this)")
    out("  asset   regular              shielded             maturing(mp lock)    spendable coins")
    for key, asset in ASSETS.items():
        try:
            spend = await payouts.wallet_spendable(bp, asset)
        except BeamPayError as e:
            out(f"  {key:6}  unreadable: {e}   ← that is 'we cannot see', never 'nothing to spend'")
            continue
        # ⛔ AND THE COIN COUNTS, WHICH ARE NOT THE BALANCE. Beam locks a whole UTXO per
        # pending transaction, so these are how many crossings can be in flight at once — two
        # releases 0.7 s apart died "Not enough inputs" on 2026-09-10 for want of this number.
        coins = (
            "coins unreadable" if spend["coins_regular"] is None
            else f"{spend['coins_regular']} coin(s) regular · "
                 f"{spend['coins_shielded']} coin(s) shielded"
        )
        out(
            f"  {key:6}  {_fmt(spend['regular']):<19} {_fmt(spend['shielded']):<20} "
            f"{_fmt(spend['maturing']):<19} {coins}"
        )
        fee_coins = spend["fee_coins"]
        if fee_coins is None or spend["coins_regular"] is None:
            out("          concurrency: UNKNOWN — the wallet's coin list could not be read, so "
                "a release defers rather than guessing at how many inputs are free")
        else:
            out(f"          concurrency: at most "
                f"{min(spend['coins_regular'], fee_coins)} regular / "
                f"{min(spend['coins_shielded'], fee_coins)} shielded release(s) in flight "
                f"({fee_coins} BEAM fee coin(s), one per invocation)")
    out(f"  source     : {'regular first, then shielded' if payouts.spend_unshielded() else 'shielded ONLY'}"
        f"  (PGAS_PAYOUT_SPEND_UNSHIELDED={int(payouts.spend_unshielded())}; it also decides "
        f"whether the §9.3/S2 gate runs)")
    out("")
    await _print_coin_targets(bp, out)
    out("")
    # what the shield policy is protecting: the orders already promised out of the unshielded
    # float, and the floor under it. Printed from the same helpers the policy itself calls.
    out("  the working float the shield keeps unshielded (a shielded output is locked ≤ 72 h):")
    keep_floor = max(0, int(settings.shield_keep_groth or 0))
    for key, asset in ASSETS.items():
        owed = await payouts.scheduled_liability_groth(asset)
        with_buffer = payouts.liability_reserve_groth(owed)
        if not owed and key != "ETH":
            continue  # only the asset that has orders, plus ETH which always shows its floor
        out(
            f"  {key:6}  scheduled-but-unreleased {_fmt(owed)} → {_fmt(with_buffer)} with the "
            f"{settings.shield_liability_buffer_bps} bps buffer · floor {_fmt(keep_floor)} "
            f"→ keeps {_fmt(max(keep_floor, with_buffer))}"
        )
    out("")
    try:
        ready = await bp.expectation_route_ready()
        out(f"  attribution: direct available={ready.get('available')} "
            f"pending={ready.get('pending')} ttl={ready.get('ttl_sec')}s")
    except BeamPayError as e:
        out(f"  attribution: ⛔ /internal/expect_contract_tx unreachable ({e}) — every contract "
            f"txid would book to __house__")
    out(f"  flags      : claim={int(settings.claim_enabled)} shield={int(settings.shield_enabled)} "
        f"payout_direct={int(settings.payout_direct_enabled)} "
        f"payout_instant={int(settings.payout_instant_enabled)} paused={int(_paused())}")
    out("")
    await _print_distributor(out)
    await _print_intents(out)
    return 0


async def _print_coin_targets(bp: beampay.BeamPay, out: Any) -> None:
    """Target vs actual COINS, per asset, and the one line that says what to do about it (T36).

    ⛔ The counts are the release gate's own (`utxo.coin_targets` → `payouts.wallet_spendable`),
    not a second implementation of them: an operator who reads a different number here than the
    number that held the payout has been told a story, not a fact. A count that could not be
    read prints UNREADABLE and never 0 — "we cannot see" is not "there are none" (law 8)."""
    from . import utxo  # local: utxo imports payouts, which imports this module

    out("  COIN TARGETS — how many coins the policy wants free, and how many there are")
    out("  asset   have  target  state                                       what they fund")
    try:
        targets = await utxo.coin_targets(bp)
    except (BeamPayError, BeamError) as e:
        out(f"  ⛔ unreadable ({e}) — that is 'we cannot see', never 'the wallet has none'")
        return
    for key in (utxo.BEAM_KEY, *ASSETS):
        row = targets.get(key) or {}
        have, target = row.get("have"), int(row.get("target") or 0)
        note = ""
        if have is None:
            note = f"UNREADABLE — {row.get('why') or 'no reason recorded'}"
        elif int(have) < target and int(row.get("spendable_groth") or 0) > 0:
            note = "← below target"
        elif int(have) < target:
            note = "(holds none of this asset — not a shortage)"
        what = "fee coins, one per BEAM-spending leg" if key == utxo.BEAM_KEY else "payout coins"
        out(
            f"  {key:6} {('—' if have is None else have):>5}  {target:>6}  "
            f"{note:<44}{what}"
        )
    need = utxo.split_needed(targets)
    out(f"  split needed: {', '.join(need) if need else 'none'}")
    if need:
        out(
            f"               → python -m pgasme.beam split --asset {need[0]}   (dry run; add "
            f"--apply to make it. BEAM first: every other split pays its fees in BEAM coins)"
        )


async def _print_distributor(out: Any) -> None:
    """The INSTANT payout distributor (T34): which address, how much ETH it can still pay out
    of, which nonce it will sign next, and when it was last topped up.

    ⛔ THE FLOAT PRINTED HERE IS THE LAST ONE A PASS READ, not a fresh read — the same number
    /v1/health serves, from the same row, so an operator and a monitor can never disagree about
    it. The gates read the chain; this is evidence. The KEY FILE'S PATH is printed and its
    CONTENT never is."""
    from . import distributor  # local: distributor imports config/db, not this module

    if not distributor.configured():
        out("  distributor: — (PGAS_DISTRIBUTOR_KEY_FILE is not set; instant payouts have no float)")
        return
    try:
        addr = distributor.address()
    except distributor.KeyFileError as e:
        out(f"  distributor: ⛔ the key file could not be loaded — {e}")
        return
    row = await distributor.active_row()
    if not row:
        out(f"  distributor: {addr}  (key {distributor.key_path()})")
        out("               no row yet — the first instant payout or refill pass registers it "
            "and seeds its nonce from the chain")
        return
    have = int(str(row.get("float_wei") or "0"))
    low = int(settings.distributor_float_min_wei)
    mark = "⛔ BELOW THE FLOOR" if have < low else "ok"
    age = f"{int(time.time() - float(row.get('float_at') or 0))}s ago" if row.get("float_at") else "never read"
    out(f"  distributor: {addr}  state={row.get('state')}  (key {row.get('key_file')})")
    out(f"               float {have} wei ({have / 1e18:.6f} ETH, {age}) · floor {low} · "
        f"target {int(settings.distributor_float_target_wei)}  {mark}")
    out(f"               next nonce {row.get('nonce_next')} "
        f"(seeded from the chain at {row.get('nonce_seeded_from_chain')}) · gas "
        f"{int(settings.instant_gas_limit)} × {settings.instant_gas_headroom:g} headroom")
    last = row.get("last_refill") or {}
    if last:
        out(f"               last refill {last.get('wei')} wei as {last.get('ref')} "
            f"{int(time.time() - float(last.get('at') or 0))}s ago")
    else:
        out("               last refill: none — the float was funded by hand, or not at all")
    d = db()
    paying = await d.payout_requests.count_documents({"status": "paying"})
    # ⛔ `failed` is not a payout status any more (T40): nothing in the processor writes it, so
    # a filter that excludes it is excluding a state that cannot occur — and would silently miss
    # the two that CAN, `delayed` and `held`, both of which are still crossings in flight.
    refills = await d.payout_requests.count_documents(
        {"mode": "refill", "status": {"$nin": ["sent", "cancelled"]}}
    )
    out(f"               in flight: {paying} instant payout(s) on the wire · {refills} refill(s) crossing")


async def _print_intents(out: Any) -> None:
    """Intents: a row that recorded what it was ABOUT to do and has no txid yet. These are the
    rows a restart must reconcile from the chain and MUST NEVER re-sign."""
    d = db()
    rows = await d.payout_requests.find({"status": {"$in": ["releasing", "bridging"]}}).to_list(50)
    deps = await d.deposits.find({"treasury": {"$in": ["claiming", "shielding"]}}).to_list(50)
    held_p = await d.payout_requests.count_documents({"status": "held"})
    held_d = await d.deposits.count_documents({"treasury": "held"})
    out(f"  pending intents: {len(rows)} payout · {len(deps)} treasury"
        + (f"   ⚠️ HELD FOR A HUMAN: {held_p} payout · {held_d} deposit" if held_p or held_d else ""))
    await _print_waiting(out)
    for r in rows:
        out(
            f"    payout   {r['_id']} {r.get('status')} amount {_fmt(int(r.get('amount_groth', 0)))} "
            f"fee {_fmt(int(r.get('relayer_fee_groth') or 0))} beam_txid={r.get('beam_txid') or '—'} "
            f"msg_id={r.get('msg_id') if r.get('msg_id') is not None else '—'}"
        )
    for r in deps:
        out(
            f"    treasury {r['_id']} {r.get('treasury')} claim_txid={r.get('claim_txid') or '—'} "
            f"shield_txids={len(r.get('shield_txids') or [])}/{len(r.get('shield_plan') or [])}"
        )


async def _print_waiting(out: Any) -> None:
    """⛔ **NOTHING FAILS ANY MORE, SO SOMETHING HAS TO SAY WHAT IS WAITING** (T40). An order
    the user was told is "delayed" is one an operator has to be able to see in one command: the
    reason in the processor's own words, how many rungs of the ladder it has climbed, and when
    it will be tried again. A `held` row is the same thing with nobody retrying it.

    The ETA comes from `payouts.eta_for` — the SAME function the user's own page reads — so an
    operator and the person asking them never see two different answers (law 9)."""
    from . import payouts  # local: payouts imports this module

    d = db()
    delayed = await d.payout_requests.find({"status": payouts.DELAYED}).to_list(50)
    held = await d.payout_requests.find({"status": payouts.HELD}).to_list(50)
    if not delayed and not held:
        return
    now = time.time()
    out("")
    out("  DELAYED / HELD payout orders — the money is RESERVED and nothing was refunded")
    for r in delayed:
        nxt = float(r.get("next_attempt_at") or 0)
        when = f"in {int(nxt - now)}s" if nxt > now else "now (due)"
        eta_at, tail_s, _note = payouts.eta_for(r)
        out(
            f"    delayed  {r['_id']} {_fmt(int(r.get('amount_groth') or 0))} "
            f"{r.get('asset') or 'ETH'} · attempt {len(payouts.attempts_of(r)) + 1} "
            f"(delay {int(r.get('delays') or 0)}, from {r.get('delayed_from') or '—'}) · "
            f"next try {when} · arrives ≈ "
            f"{int(eta_at - now) if eta_at else '—'}s (tail {int(tail_s // 3600)}h)"
        )
        out(f"             why: {r.get('hold_reason') or 'no reason was recorded on the row'}")
    for r in held:
        out(
            f"    HELD     {r['_id']} {_fmt(int(r.get('amount_groth') or 0))} "
            f"{r.get('asset') or 'ETH'} · from {r.get('held_from') or '—'} · nothing retries "
            f"this; the user still sees it as delayed"
        )
        out(f"             why: {r.get('hold_reason') or 'no reason was recorded on the row'}")


async def cmd_dry_run_payout(request_id: str, out: Any = print) -> int:
    """Print the EXACT calls a release would make. create_tx:false throughout — a dry run that
    creates a real order is failure mode §7.9 #12; a dry estimate must be local."""
    from . import payouts  # local: payouts imports this module

    row = await db().payout_requests.find_one({"_id": request_id})
    if not row:
        out(f"⛔ no payout request {request_id!r}")
        return 1
    asset = get_asset(row.get("asset", "ETH"))
    amount = int(row["amount_groth"])
    w = wallet()
    out(f"DRY RUN · payout {request_id} · {row.get('status')} · mode {row.get('mode')}")
    out(f"  asset        : {asset.key} (b{asset.key}, aid {asset.aid})")
    out(f"  pipe cid     : {asset.beam_cid}")
    out(f"  receiver W   : {row['W']}")
    out(f"  amount       : {_fmt(amount)} {asset.key}  ({amount} groth)")
    # ⛔ THE NUMBERS THE GATES READ, READ THROUGH THE GATES' OWN HELPERS (law 8: the prober must
    # call the way the caller calls). Our 2% is revenue; what the release measures its cost
    # against is the bridge fee THIS order funded — `payouts.bridge_budget_groth`, with the
    # pre-2026-09-10 fallback in one place. Printed apart so an operator can see both.
    ours = int(row.get("fee_groth") or 0)
    charged = payouts.bridge_budget_groth(row)
    modern = payouts.funds_its_own_crossing(row)
    out(f"  our fee (2%) : {_fmt(ours)} — already debited at schedule (revenue, not a budget)")
    out(f"  bridge fee funded: {_fmt(charged)} — what the subsidy gate measures against"
        + ("" if modern else "  [legacy row: our 2% WAS the budget]"))
    try:
        fee_groth, floor_groth, detail = await payouts.relayer_fee_for(asset, payouts.get_rpc())
    except Exception as e:  # noqa: BLE001 — a CLI prints the failure, it never tracebacks
        out(f"  ⛔ relayer fee unreadable: {type(e).__name__}: {e} — a fee we cannot compute is "
            f"a crossing we cannot make")
        return 1
    out(f"  relayer fee  : {_fmt(fee_groth)} {asset.key}  ({fee_groth} groth) at "
        f"{detail['gas_gwei']:.3f} gwei × margin {detail['margin']}"
        + (f"  [raised to the {floor_groth}-groth floor]"
           if fee_groth > int(detail.get("fee_before_floor_groth") or fee_groth) else ""))
    out(f"  treasury pays: {_fmt(amount + fee_groth)} (amount + relayerFee)")
    share = fee_groth / amount if amount else 1.0
    if modern:
        # the share gate does not run for a row that paid for its own crossing: at a 1-groth
        # floor it would hold every small payout, which is the defect it became on 2026-09-10
        out(f"  relayer share: {share:.3%} — not a gate for this row (it funded its crossing)")
    else:
        out(f"  relayer share: {share:.3%} (max {settings.max_relayer_share:.0%}) "
            f"{'OK' if share <= settings.max_relayer_share else '⛔ TOO HIGH'}")
    limit = relayer_subsidy()  # the ONE reader — 0 / unset / below 1 all mean 1×
    subsidy = fee_groth / charged if charged else float("inf")
    out(f"  vs the bridge fee funded: {_fmt(fee_groth)} paid against {_fmt(charged)} funded "
        f"({subsidy:.2f}×, limit {limit:g}×) "
        f"{'OK' if fee_groth <= charged * limit else '⛔ LOSS-MAKING'}")
    bp = beampay.beampay()
    mp = await payouts.float_address()
    out(f"  float address : {mp or '— none configured; the release would hold'}  (BeamPay; "
        f"the shielded float's primary — where a SHIELDED crossing books)")
    # ⛔ **WHERE A CROSSING BOOKS IS ITS SOURCE, NOT THE MAX-PRIVACY ADDRESS** (law 8: the prober
    # must call the way the caller calls). Step 4 below printed `mp` whatever the source was, so
    # a dry run of a regular-funded crossing previewed the registration that drives MP negative
    # and leaves the treasury untouched — the drift `attribution_address` exists to make
    # impossible, shown to the operator as if it were the plan. `attribution_address` is the ONE
    # reader: the row once `_payout_scheduled` has decided, the primary as its only fallback.
    reg_addr = await payouts.attribution_address(row)
    try:
        treasury = beampay.treasury_address()
        registry = await payouts.mp_registry()
        fl = await payouts.float_groth(bp, asset) if mp else 0
        out(f"  float spread  : {len(registry)} max-privacy address(es) — the float below is the "
            f"SUM over them (one per shield chunk)")
        reserved = await payouts.inflight_groth(asset, request_id)
        need = amount + fee_groth + settings.float_min_groth
        parts = await payouts.payout_float(bp, asset)
        out(f"  shielded float: {_fmt(fl)} · {_fmt(reserved)} committed to crossings in flight "
            f"· needs {_fmt(need)} → "
            f"{'ENOUGH' if int(parts['total']) - reserved >= need else 'NOT ENOUGH'}")
        reg = await bp.available_groth(treasury, asset.aid)
        out(f"  unshielded    : {_fmt(reg)} {asset.key} (treasury) · counted as float="
            f"{int(payouts.spend_unshielded())} · inputs proven="
            f"{int(settings.beam_send_inputs_proven)} → "
            f"{'OK' if payouts.spend_unshielded() or settings.beam_send_inputs_proven or reg <= settings.beam_regular_tolerance_groth else '⛔ REFUSES (spec S2 unanswered)'}")
        # ⛔ THE BUCKET THE WALLET CAN ACTUALLY SPEND — the gate the dry run exists to preview.
        # Asked through `payouts.wallet_spendable`, the same reader the release calls, so an
        # operator reading this sees the number the gate will see (law 8).
        try:
            spend = await payouts.wallet_spendable(bp, asset)
        except BeamPayError as e:
            out(f"  wallet spendable: unreadable ({e}) — the release would HOLD, never send")
        else:
            order = [s for s in payouts.SOURCES
                     if s != payouts.SOURCE_REGULAR or payouts.spend_unshielded()]
            owned = [s for s in order if int(parts[s]) >= need]
            source = next((s for s in owned if int(spend[s]) - reserved >= need), None)
            out(f"  wallet spendable: {_fmt(spend['regular'])} regular · "
                f"{_fmt(spend['shielded'])} shielded · {_fmt(spend['maturing'])} maturing "
                f"(max-privacy lock, up to 72 h after a shield settles)")
            addr = (
                treasury if source == payouts.SOURCE_REGULAR
                else (mp if source == payouts.SOURCE_SHIELDED else "")
            )
            # a row that has not been released yet has decided nothing, so the SOURCE this dry
            # run just picked is what the release would register; a row that HAS decided keeps
            # its own answer, because the row is the authority (`attribution_address`)
            if not row.get("source_address"):
                reg_addr = addr or reg_addr
            out(f"  source        : {source or '⛔ NONE — the release would HOLD'}"
                + (f" → books to {addr}" if addr else ""))
        budget, why = await payouts.fee_budget("send", bp)
        out(f"  BEAM for fees : {_fmt(await bp.available_groth(treasury, 0))} (treasury, asset 0)"
            f" · this call reserves {payouts.fee_budget_line('send', budget, why)}")
    except BeamPayError as e:
        out(f"  shielded float: unreadable ({e}) — that is 'we cannot see', never 'no float'")
    # ⛔ **THE STEP THAT COMES BEFORE THE CROSSING** (T40): a regular-funded crossing is sent
    # from an address created for THIS order and nothing else, and the dry run has to preview it
    # or an operator reading this is being shown a release that no longer exists. A SHIELDED
    # crossing is unchanged — its value is already inside Lelantus.
    fund_need = payouts.crossing_groth(row) + fee_groth
    out("")
    if row.get("source") == payouts.SOURCE_SHIELDED:
        out("  the crossing is SHIELDED-funded: no fresh address, it books to the max-privacy "
            "primary (that pool is never refilled — shielding is off)")
    else:
        out("  a FRESH Beam address for this crossing (the treasury never sends from itself):")
        out("   0a) POST " + bp.url + "/create_wallet   " + json.dumps(
            {"note": payouts.crossing_note(request_id), "wallet_type": "regular",
             "expiration": "never"}
        ) + (f"   ← already created: {row['source_address']}" if row.get("source_address")
             else "   ← created ONCE per order; a retry reuses it"))
        out("   0b) POST " + bp.url + "/withdraw   " + json.dumps(
            {"from_address": "<the treasury>",
             "to_address": row.get("source_address") or "<the address 0a answers>",
             "asset_id": asset.aid, "amount": fund_need,
             "comment": payouts.fund_comment(request_id)}
        ))
        out(f"       exactly {_fmt(fund_need)} = amount + relayerFee, so the address ends at "
            f"ZERO once the crossing burns it. /withdraw is NOT idempotent and answers no "
            f"txid — the comment is its identity and a lost answer is resolved from history")
        out("       the release WAITS for that transfer to settle before it signs anything")
    args = w.bridge_args(asset.beam_cid, amount, row["W"], fee_groth)
    out("")
    out("  the exact JSON-RPC calls (nothing is sent):")
    out("   1) " + json.dumps(
        {
            "jsonrpc": "2.0", "id": 1, "method": "invoke_contract",
            "params": {"contract_file": w.shader, "args": args, "create_tx": False},
        }
    ))
    out("   2) assert bytes.fromhex(W[2:]) in raw_data  AND  bytes.fromhex(cid) in raw_data")
    out('   3) {"jsonrpc":"2.0","id":2,"method":"process_invoke_data","params":{"data":<raw_data>}}'
        "   ← THE IRREVERSIBLE CALL (kill switch checked immediately before it)")
    out("   4) POST " + bp.url + "/internal/expect_contract_tx   (X-API-Key: "
        "PGAS_BEAMPAY_INTERNAL_KEY, scope ledger:adjust)")
    out("      " + json.dumps(
        {"txid": "<the txid step 3 returns>", "address": reg_addr or "<PGAS_BEAM_MP_ADDRESS>",
         "trade_ref": request_id}
    ) + "   ← the registration, BEFORE the row advances")
    out("      the flow books to the address that FUNDS the crossing (the treasury for a "
        "regular source, the max-privacy primary for a shielded one); without this the whole "
        "invocation — the bETH and its BEAM fee — books to __house__")
    try:
        raw, _args, blob = await w.build_bridge_send(asset.beam_cid, amount, row["W"], fee_groth)
        out(f"  calldata verified: receiver ✅  cid ✅  ({len(blob):,} bytes) — step 2 passes")
    except (BeamError, Halted) as e:
        out(f"  ⛔ step 1/2 would refuse: {e}")
        return 1
    out(f"  flag PGAS_PAYOUT_DIRECT_ENABLED={int(settings.payout_direct_enabled)} — "
        f"{'the release would run' if settings.payout_direct_enabled else 'DARK: nothing releases'}")
    return 0


async def cmd_dry_run_claim(deposit_id: str, out: Any = print) -> int:
    dep = await db().deposits.find_one({"_id": deposit_id})
    if not dep:
        out(f"⛔ no deposit {deposit_id!r}")
        return 1
    asset = get_asset(dep.get("asset", "ETH"))
    msg_id = (dep.get("eth") or {}).get("msg_id")
    w = wallet()
    out(f"DRY RUN · claim deposit {deposit_id} · {dep.get('status')} · "
        f"treasury {dep.get('treasury') or '—'}")
    out(f"  asset      : {asset.key} (aid {asset.aid})   pipe cid {asset.beam_cid}")
    out(f"  lock msg id: {msg_id}   value {_fmt(int(dep.get('value_groth') or 0))}")
    from . import payouts  # local: payouts imports this module

    budget, why = await payouts.fee_budget("claim", beampay.beampay())
    # ⚠️ §WE-SET-IT-WE-DONT-READ-IT: this line used to print a 0.02 constant, and the first live
    # claim paid 0.121. It is now what the last settled claims really cost, ×1.5, over a floor.
    out(f"  claim fee  : reserves {payouts.fee_budget_line('claim', budget, why)}")
    if msg_id is None:
        out("  ⛔ this deposit has no pipe message id — nothing to claim")
        return 1
    # ⛔ THE DRY RUN MUST ASK THE SAME QUESTION THE LIVE PASS ASKS. A message delivered to an
    # indexed receiver key is invisible to a `view_incoming` that was not told about that index,
    # so a dry run without them would report "not yet" for a claim the processor can make — a
    # dry run that reports LESS than the live run is how a real drain once logged "nothing moved".
    from .receiver_keys import claim_index, open_indexes  # local: it imports this module

    index = claim_index(dep)
    try:
        indexes = await open_indexes(asset)
    except Exception as e:  # noqa: BLE001 — an unreadable database is not "no indexes"
        out(f"  ⛔ could not read the open receiver indexes: {e}")
        return 1
    view_args = f"role=manager,action=view_incoming,startFrom=0,cid={asset.beam_cid}" + (
        ",indexes=" + INDEX_LIST_SEP.join(str(i) for i in indexes) if indexes else ""
    )
    out(f"  receiver key: {'legacy (cid blob)' if index is None else f'index {index}'}"
        f"   · view_incoming asks about {indexes or '[legacy only]'}")
    try:
        incoming = await w.view_incoming(asset.beam_cid, indexes)
    except BeamError as e:
        out(f"  ⛔ view_incoming unreadable: {e} (that is 'we cannot see', never 'not delivered')")
        return 1
    mine = [m for m in incoming if m["msg_id"] == int(msg_id)]
    out(f"  view_incoming: {len(incoming)} claimable · ours {'PRESENT' if mine else 'not yet'}"
        + (f" (amount {mine[0]['amount']})" if mine else ""))
    args = (
        f"role=user,action=receive,cid={asset.beam_cid},msgId={int(msg_id)}"
        + _index_arg(index)
    )
    out("")
    out("  the exact JSON-RPC calls (nothing is sent):")
    out("   0) " + json.dumps(
        {
            "jsonrpc": "2.0", "id": 1, "method": "invoke_contract",
            "params": {
                "contract_file": w.shader,
                "args": view_args,
                "create_tx": False,
            },
        }
    ))
    out("   1) " + json.dumps(
        {
            "jsonrpc": "2.0", "id": 2, "method": "invoke_contract",
            "params": {"contract_file": w.shader, "args": args, "create_tx": False},
        }
    ))
    out("   2) assert bytes.fromhex(cid) in raw_data")
    out('   3) {"jsonrpc":"2.0","id":3,"method":"process_invoke_data","params":{"data":<raw_data>}}'
        "   ← THE IRREVERSIBLE CALL (kill switch checked immediately before it)")
    bp = beampay.beampay()
    try:
        treasury = beampay.treasury_address()
    except BeamPayError as e:
        out(f"  ⛔ {e}")
        return 1
    out("   4) POST " + bp.url + "/internal/expect_contract_tx   (X-API-Key: "
        "PGAS_BEAMPAY_INTERNAL_KEY, scope ledger:adjust)")
    out("      " + json.dumps(
        {"txid": "<the txid step 3 returns>", "address": treasury, "trade_ref": deposit_id}
    ) + "   ← the registration, BEFORE the row advances")
    out("      then GET " + bp.url + f"/internal/contract_tx/<txid> until booked==true AND "
        f"status=={TX_COMPLETED} — that, not tx_status, is the claim's evidence")
    try:
        built = await w.invoke(args)
        raw = built.get("raw_data")
        ok = bool(raw) and bytes.fromhex(asset.beam_cid) in _blob(raw)
        out(f"  calldata verified: cid {'✅' if ok else '🚨 MISSING'} "
            f"({len(_blob(raw)) if raw else 0:,} bytes)")
        if not ok:
            return 1
    except BeamError as e:
        out(f"  ⛔ step 1/2 would refuse: {e}")
        return 1
    out(f"  flag PGAS_CLAIM_ENABLED={int(settings.claim_enabled)} — "
        f"{'the claim would run' if settings.claim_enabled else 'DARK: nothing is claimed'}")
    return 0


# ----------------------------------------------------------- the one-off attribution repair

HOUSE_ACCOUNT_ID = "__house__"  # BeamPay api.py:57 — the synthetic account an unregistered flow books to
LEDGER_ADJUST_PATH = "/internal/ledger/adjust"
# BeamPay api.py:66 `GATE_TX_MAX_AGE_SEC`. An adjustment must name a settled contract tx that is
# still RECENT: past this the route answers 409 `tx_too_old` and the repair is a human problem.
GATE_TX_MAX_AGE_S = 24 * 3600


def house_flow(tx: dict[str, Any], asset_id: int = 0) -> int:
    """BeamPay's own `_tx_asset_flow` (api.py:517), port-for-port, so this tool computes the
    SAME number the route will bound it by rather than a number of its own.

    ⛔ POSITIVE = wallet OUTFLOW = `__house__` was DEBITED (process_payments.py:189-201). The
    BEAM transaction fee is always an outflow, so it is added on asset 0 only — and on a CLAIM,
    whose `invoke_data` moves the bridged asset and no BEAM, the whole asset-0 flow IS the fee."""
    key = str(asset_id)
    flow = 0
    for entry in tx.get("invoke_data") or []:
        for amt in entry.get("amounts") or []:
            if str(amt.get("asset_id", 0)) == key:
                flow += int(amt.get("amount", 0) or 0)
    if key == "0":
        flow += int(tx.get("fee", 0) or 0)
    return flow


async def _row_for_txid(txid: str) -> tuple[str, str] | None:
    """`(collection, row id)` of the crossing this txid is the evidence for, or None."""
    dep = await db().deposits.find_one({"claim_txid": txid}, {"_id": 1})
    if dep:
        return "deposits", str(dep["_id"])
    req = await db().payout_requests.find_one({"beam_txid": txid}, {"_id": 1})
    if req:
        return "payout_requests", str(req["_id"])
    return None


async def cmd_repair_fee(
    txid: str, apply: bool = False, out: Any = print, adjust_id: str | None = None
) -> int:
    """Book the BEAM FEE LEG of one already-settled contract tx off `__house__`.

    ⚠️ WHY THIS EXISTS, ONCE. The deployed claim path submitted `process_invoke_data` before it
    registered the txid with BeamPay, so BeamPay booked the WHOLE flow of claim
    `e3fceec7…` to `__house__`: `__house__` was credited the bETH (repaired at the time by a
    ledger/adjust of asset 36) and DEBITED the 12,100,000-groth BEAM fee, which was never
    repaired. The treasury therefore still shows 0.121 BEAM it has already spent. The code path
    is fixed — `attribution_ready` is asked and `expect_contract_tx` is registered before the row
    advances — so this is a repair for transactions made by the OLD build, not a routine.

    It posts BeamPay's documented `POST /internal/ledger/adjust`, which is ZERO-SUM by
    construction: one asset, one debit, one matching credit, and no field that can credit
    without debiting. The DIRECTION is not ours to choose — BeamPay derives it from the gate
    tx's own signed flow (api.py rule 7): an OUTFLOW debited the house, so the repair pays the
    house back (treasury → `__house__`); an INFLOW credited it, so the repair pays the address.
    This computes the same flow with the same arithmetic and refuses locally rather than
    discovering the mismatch as a 409.

    THE ID IS DETERMINISTIC ON PURPOSE, AND OVERRIDABLE FOR ONE REASON. `pgasme:fee:<txid>` is
    what makes a *replay* free: post it twice and BeamPay answers `replayed: true` instead of
    moving value twice. But an adjustment that was REFUSED (a 409 on a body we then corrected,
    a reversed pair of addresses) has burned that id, and the corrected adjustment can never be
    posted under it. `--adjust-id` is the escape hatch for exactly that: a fresh id for a repair
    that has never been applied. It is never a way to post the same repair a second time — the
    operator carries the burden of knowing which of the two it is.

    DRY RUN BY DEFAULT: it prints the exact body and every gate the route will apply, and sends
    nothing. `--apply` posts it, and checks the kill switch immediately before doing so."""
    bp = beampay.beampay()
    try:
        treasury = beampay.treasury_address()
    except BeamPayError as e:
        out(f"⛔ {e}")
        return 1
    out(f"{'APPLY' if apply else 'DRY RUN'} · repair the BEAM fee leg of contract tx {txid}")
    try:
        tx = await bp.contract_tx(txid)
    except BeamPayError as e:
        out(f"  ⛔ BeamPay could not be read: {e} — that is 'we cannot see', never 'nothing to do'")
        return 1
    if tx is None:
        out("  ⛔ BeamPay's processor has not booked this transaction (tx_not_found). Nothing "
            "to repair yet — an adjustment must name a BOOKED contract tx (rule 5)")
        return 1

    status = int(tx.get("status", -1))
    booked = tx.get("booked") is True
    fee = int(tx.get("fee") or 0)
    flow = house_flow(tx, 0)
    age = time.time() - int(tx.get("create_time") or 0)
    owner = await _row_for_txid(txid)
    trade_ref = f"{owner[1]}" if owner else f"pgasme:fee:{txid[:24]}"
    out(f"  booked     : {booked}   status {status} ({tx.get('status_string') or '—'})   "
        f"{'SETTLED' if booked and status == TX_COMPLETED else '⛔ NOT SETTLED'}")
    out(f"  BEAM fee   : {_fmt(fee)} BEAM ({fee} groth)")
    out(f"  asset-0 flow: {flow:+d} groth  (BeamPay's own _tx_asset_flow: invoke_data + fee)")
    out(f"  attributed : {tx.get('attributed_to') or '— none (it booked to __house__)'}")
    out(f"  age        : {int(age)}s of the {GATE_TX_MAX_AGE_S}s gate window")
    out(f"  crossing   : {owner[0] + ' ' + owner[1] if owner else '— no row carries this txid'}")

    if not booked or status != TX_COMPLETED:
        out("  ⛔ REFUSING: `booked` is the daemon's idempotency flag, not success — settlement "
            "is booked AND status == 3, and the route answers 409 tx_not_booked otherwise")
        return 1
    if tx.get("attributed_to"):
        out(f"  ⛔ REFUSING: this txid is already attributed directly to "
            f"{tx.get('attributed_to')}, so it never credited or debited the house at all — "
            f"there is nothing here to repair, and 'repairing' it would move value out of an "
            f"account that never received it (409 already_attributed_directly, permanent)")
        return 1
    if flow == 0:
        out("  ⛔ REFUSING: this transaction moved no BEAM at all (409 no_flow_for_asset)")
        return 1
    if flow > 0:
        frm, to = treasury, HOUSE_ACCOUNT_ID
        which = ("the fee was an OUTFLOW, so __house__ was DEBITED and the repair pays the "
                 "house back out of the treasury")
    else:
        frm, to = HOUSE_ACCOUNT_ID, treasury
        which = "the flow was an INFLOW, so __house__ was CREDITED and the repair pays the treasury"
    body = {
        "adjust_id": adjust_id or f"pgasme:fee:{txid}",
        "asset_id": 0,
        "from_address": frm,
        "to_address": to,
        "amount_groth": abs(flow),
        "reason": (
            f"BEAM fee leg of contract tx {txid} booked to __house__: the txid was not "
            f"registered with expect_contract_tx before the invocation settled"
        )[:256],
        "trade_ref": trade_ref[:128],
        "after_tx": txid,
    }
    out(f"  direction  : {which}")
    out("")
    out(f"  POST {bp.url}{LEDGER_ADJUST_PATH}   (X-API-Key: PGAS_BEAMPAY_INTERNAL_KEY, "
        f"scope ledger:adjust)")
    out("  " + json.dumps(body))
    if age > GATE_TX_MAX_AGE_S:
        out(f"  ⛔ REFUSING: the gate tx is {int(age / 3600)}h old and BeamPay's window is "
            f"{GATE_TX_MAX_AGE_S // 3600}h — the route answers 409 tx_too_old. This is now a "
            f"human's correction, not this tool's")
        return 1
    if not apply:
        out("  DRY RUN — nothing was sent. Re-run with --apply to post it.")
        return 0
    if _paused():
        out(f"  ⛔ the kill switch is set ({settings.stop_file}) — nothing was sent")
        return 1
    try:
        res = await bp.call("POST", LEDGER_ADJUST_PATH, body=body, internal=True)
    except BeamPayError as e:
        out(f"  ⛔ BeamPay refused: {e}")
        return 1
    out(f"  APPLIED: {json.dumps(res)[:400]}")
    return 0


async def cmd_replan_shield(deposit_id: str, apply: bool = False, out: Any = print) -> int:
    """The chunk table of one deposit's shielding, and the re-plan of the chunks that FAILED.

    ⚠️ WHY THIS EXISTS. 2026-09-09 23:21–23:23Z, deposit 60b0e5703cbad6955af059f1: three
    max-privacy self-sends of 1,000,000 groth bETH to the SAME `PGAS_BEAM_MP_ADDRESS`. Chunk 0
    settled; chunks 1 and 2 were refused by the wallet with "Shielded outp duplicate ← Kernel
    Type 3" (status 4, "failed maximum anonymity") — a max-privacy address publishes ONE-TIME
    vouchers and the wallet re-used the same one for every send in quick succession. The code
    path is fixed (`payouts.shield_target` makes a fresh address per chunk), but the two chunks
    that already failed are not something the machine may retry on its own: `/withdraw` has no
    idempotency key, so a resend a human did not look at first is exactly the double send the
    whole design refuses to make possible.

    THE STATE IS READ FROM BEAMPAY'S OWN HISTORY, by the comment WE chose (`shield_comment`) —
    never from what the row believes. A chunk qualifies for a re-plan only when its transactions
    are ALL dead: no live transaction carries its comment. A `pending` or unresolved chunk
    REFUSES the whole run, because the wallet may still emit its transaction and a re-plan that
    races it queues a second send of one chunk.

    DRY RUN BY DEFAULT. `--apply` writes exactly one transition (`payouts.replan_shield`): the
    failed chunks become unsent, their dead txids are written off on the row, the hold is
    cleared, and the settled chunks and their txids are left untouched — they ARE the shielded
    value. Nothing here reaches the wallet or BeamPay's movers, and the kill switch is checked
    before the write."""
    from . import payouts  # local: payouts imports this module

    dep = await db().deposits.find_one({"_id": deposit_id})
    if not dep:
        out(f"⛔ no deposit {deposit_id!r}")
        return 1
    bp = beampay.beampay()
    try:
        treasury = beampay.treasury_address()
    except BeamPayError as e:
        out(f"⛔ {e}")
        return 1
    asset = get_asset(dep.get("asset", "ETH"))
    plan = [int(x) for x in (dep.get("shield_plan") or [])]
    out(f"{'APPLY' if apply else 'DRY RUN'} · re-plan the shield chunks of deposit {deposit_id}")
    held = str(dep.get("treasury")) == payouts.HELD
    out(f"  treasury   : {dep.get('treasury')}"
        + (f"  (held from {dep.get('held_from')})" if held else ""))
    if dep.get("hold_reason"):
        out(f"  hold       : {str(dep['hold_reason'])[:400]}")
    out(f"  asset      : {asset.key} (aid {asset.aid}) · sent from the treasury {treasury}")
    out(f"  plan       : {len(plan)} chunk(s), {_fmt(sum(plan))} {asset.key} of a "
        f"{_fmt(int(dep.get('value_groth') or 0))} deposit")
    out(f"  history    : searched back to create_time {int(payouts.shield_since_of(dep))} — the "
        f"window every chunk's comment is looked for in")
    if not plan:
        out("  ⛔ this deposit has no shield plan, so there are no chunks to re-plan")
        return 1
    try:
        rows = await payouts.shield_chunk_report(bp, dep)
    except BeamPayError as e:
        out(f"  ⛔ BeamPay could not be read: {e} — that is 'we cannot see', and it is NEVER "
            f"'nothing was sent'")
        return 1
    out("")
    out("   k  amount            state      BeamPay status              txid          comment")
    for r in rows:
        status = (
            f"{r['tx_status']} {r['tx_status_string'] or '—'}" if r["txid"] else "— no transaction"
        )
        out(f"  {r['k']:>2}  {_fmt(r['amount']):<16}  {r['state']:<9}  {status[:26]:<26}  "
            f"{(r['txid'][:12] if r['txid'] else '—'):<12}  {r['comment']}")
        if r["to_address"]:
            out(f"      → sent to {r['to_address'][:24]}…"
                if len(r["to_address"]) > 24 else f"      → sent to {r['to_address']}")
    ks, txids, racy = payouts.shield_replan_plan(rows)
    settled = [r for r in rows if r["state"] == payouts.SHIELD_SETTLED]
    out("")
    out(f"  settled, KEPT : chunk(s) {', '.join(str(r['k']) for r in settled) or '— none'} "
        f"({_fmt(sum(r['amount'] for r in settled))} {asset.key} already shielded, txids kept)")
    out(f"  would re-plan : chunk(s) {', '.join(str(k) for k in ks) or '— none'} "
        f"({_fmt(sum(r['amount'] for r in rows if r['k'] in ks))} {asset.key} to be re-sent, "
        f"each to its own FRESH max-privacy address)")
    out(f"  writes off    : {', '.join(txids) or '— none'}")
    if racy:
        out("")
        for why in racy:
            out(f"  ⛔ REFUSING: {why}. The wallet may still emit a transaction for it, and a "
                f"re-plan that races /withdraw queues a SECOND send of one chunk")
        return 1
    if not ks:
        out("")
        out("  ⛔ no chunk is `failed`, so there is nothing here to re-plan — and a hold this "
            "tool did not cause is not this tool's to clear")
        return 1
    if not apply:
        out("")
        out("  DRY RUN — nothing was written. Re-run with --apply to clear the hold and mark "
            "those chunks unsent; the processor then re-sends them ONE PER PASS.")
        return 0
    if _paused():
        out(f"  ⛔ the kill switch is set ({settings.stop_file}) — nothing was written")
        return 1
    res = await payouts.replan_shield(deposit_id, ks, txids)
    if not res.get("ok"):
        out(f"  ⛔ REFUSED: {res.get('why')}")
        return 1
    out(f"  APPLIED: chunk(s) {', '.join(str(k) for k in res['chunks'])} of "
        f"{res['chunks_total']} are unsent again (the row was {res['from']}), "
        f"{len(res['writeoffs'])} failed transaction(s) written off. The processor sends one "
        f"chunk per pass, each to its own fresh max-privacy address.")
    return 0


def _opt(argv: list[str], name: str) -> str | None:
    """`--name value` out of an argv, or None. Raises ValueError when the flag is there with
    nothing after it — a flag whose value silently became the next flag is a money bug."""
    if name not in argv:
        return None
    i = argv.index(name)
    if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
        raise ValueError(f"{name} needs a value")
    return argv[i + 1]


async def cmd_split(argv: list[str], out: Any = print) -> int:
    """`split --asset BEAM|ETH|DAI|WBTC [--coins N] [--size <groth>] [--method M] [--apply]`
    or `split --abandon <plan id> [--reason "…"]`.

    Dry run by default and refusing on every gate at `--apply` — the whole policy, the plan and
    every guard live in `pgasme/utxo.py`; this is the argv."""
    from . import utxo  # local: utxo imports payouts, which imports this module

    try:
        asset = _opt(argv, "--asset")
        coins = _opt(argv, "--coins")
        size = _opt(argv, "--size")
        method = _opt(argv, "--method")
        abandon = _opt(argv, "--abandon")
        why = _opt(argv, "--reason")
    except ValueError as e:
        out(str(e))
        return 2
    if abandon:
        # ⛔ THE ONLY WAY OUT OF THE RESUME SET, and it spends nothing: `utxo.abandon_plan`
        # refuses unless every leg is settled or refused and the split address is empty in every
        # asset. Without it an open plan whose work is over captures every later split of that
        # asset and there is nothing to do but edit Mongo by hand, which is forbidden.
        if "--apply" in argv or asset or coins or size or method:
            out("--abandon takes only a plan id and an optional --reason; it changes nothing "
                "on chain, so there is no --apply and no --asset to give it")
            return 2
        return await utxo.abandon_plan(abandon, why, out=out)
    if not asset:
        out("split needs --asset BEAM|ETH|DAI|WBTC (or --abandon <plan id>)")
        return 2
    if method and method not in utxo.SPLIT_METHODS:
        out(f"--method takes {' or '.join(utxo.SPLIT_METHODS)} (got {method!r})")
        return 2
    try:
        return await utxo.cmd_split(
            asset,
            coins=int(coins) if coins else None,
            size=int(size) if size else None,
            method=method or None,
            apply="--apply" in argv,
            out=out,
        )
    except ValueError:
        out("--coins and --size take whole numbers (--size is in groths)")
        return 2


USAGE = """python -m pgasme.beam <command>

  status                        wallet height vs node, balances per asset, float, pending intents
  dry-run --payout <id>         the exact calls a bETH → ETH release would make, and the fee
  dry-run --claim <deposit id>  the exact calls a claim would make
  replan-shield --deposit <id>  one deposit's shield chunks as BeamPay's history has them, and
                                which FAILED chunks a re-plan would hand back to the processor
                                [--apply]  clear the hold and mark them unsent, so they are
                                           re-sent to FRESH max-privacy addresses
  split --asset <A>             make the treasury MORE COINS of one asset (BEAM|ETH|DAI|WBTC).
                                Beam locks a whole coin per pending transaction, so the coin
                                COUNT is what bounds concurrency — `status` says when one is
                                short. The default cuts them in ONE wallet transaction
                                (`tx_split`): one fee, no transfers, no notifications
                                [--coins N]        how many to end with (default: the policy's)
                                [--size <groth>]   each coin's size (default: derived)
                                [--method M]       tx_split (default) | beampay (the FALLBACK:
                                                   2 transfers per coin, 2 fees, and three
                                                   Telegram notifications for every one of them)
                                [--apply]          make it; without it nothing is sent
  split --abandon <plan id>     close an OPEN plan whose work is over so it stops capturing every
                                new split of that asset (a resume adopts the open plan's METHOD).
                                Refuses unless every leg is settled/refused and its split address
                                is empty in every asset. Changes nothing on chain.
                                [--reason "…"]     recorded on the plan and on an event row
  repair-fee --txid <txid>      the ledger/adjust that books an unregistered contract tx's BEAM
                                fee leg off __house__ (a one-off for the OLD claim path)
                                [--apply]           post it; without it nothing is sent
                                [--adjust-id <id>]  retry a REFUSED adjustment under a fresh id
                                                    (default: pgasme:fee:<txid>, which makes a
                                                    replay idempotent)

Every command reads only, EXCEPT `repair-fee --apply`, which posts one zero-sum BeamPay
ledger adjustment, `replan-shield --apply`, which writes one transition on one deposit row, and
`split --apply`, which asks the WALLET to cut one of its own coins into several (`tx_split`;
value never leaves the wallet — only the kernel fee is spent), or with `--method beampay`
makes a series of BeamPay `/withdraw` transfers between two addresses of our own wallet.
`split --apply` is the ONLY command here that reaches the wallet's signing path.
"""


async def cli_main(argv: list[str], out: Any = print) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        out(USAGE)
        return 0 if argv else 2
    cmd = argv[0]
    if cmd == "status":
        return await cmd_status(out)
    if cmd == "dry-run":
        if "--payout" in argv:
            i = argv.index("--payout")
            if i + 1 >= len(argv):
                out("dry-run --payout needs a request id")
                return 2
            return await cmd_dry_run_payout(argv[i + 1], out)
        if "--claim" in argv:
            i = argv.index("--claim")
            if i + 1 >= len(argv):
                out("dry-run --claim needs a deposit id")
                return 2
            return await cmd_dry_run_claim(argv[i + 1], out)
        out("dry-run needs --payout <id> or --claim <deposit id>")
        return 2
    if cmd == "split":
        return await cmd_split(argv, out)
    if cmd == "replan-shield":
        if "--deposit" not in argv or argv.index("--deposit") + 1 >= len(argv):
            out("replan-shield needs --deposit <deposit id>")
            return 2
        return await cmd_replan_shield(
            argv[argv.index("--deposit") + 1], apply="--apply" in argv, out=out
        )
    if cmd == "repair-fee":
        if "--txid" not in argv or argv.index("--txid") + 1 >= len(argv):
            out("repair-fee needs --txid <the settled contract txid>")
            return 2
        adjust_id = None
        if "--adjust-id" in argv:
            i = argv.index("--adjust-id")
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                out("--adjust-id needs an id")
                return 2
            adjust_id = argv[i + 1]
        return await cmd_repair_fee(
            argv[argv.index("--txid") + 1],
            apply="--apply" in argv,
            out=out,
            adjust_id=adjust_id,
        )
    out(USAGE)
    return 2


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(cli_main(list(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover — exercised through cli_main in the tests
    sys.exit(main())
