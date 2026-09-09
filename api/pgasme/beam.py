"""The Beam side: the TWO wallet-api calls BeamPay has no endpoint for, the b2e relayer fee,
and a read-only CLI.

⛔ **BeamPay is the ONLY interface to the Beam wallet** (CLAUDE.md law 10, admin 2026-09-09:
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

CLI (read-only, nothing is ever sent — with ONE named exception):
    python -m pgasme.beam status
    python -m pgasme.beam dry-run --payout <request_id>
    python -m pgasme.beam dry-run --claim <deposit_id>
    python -m pgasme.beam repair-fee --txid <contract txid> [--apply]
        the one-off operator repair for a contract tx made by the OLD claim path, which
        submitted before it registered the txid: BeamPay booked the whole flow to `__house__`,
        including the BEAM fee. `--apply` posts one ZERO-SUM `POST /internal/ledger/adjust`
        (asset 0, treasury → `__house__`, `after_tx` = the settled claim) and nothing else; the
        default is a dry run that prints the body and every gate the route will apply. It never
        reaches the wallet's signing path, and the kill switch is checked before it posts.
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
from .config import settings
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

    async def get_pk(self, cid: str) -> str:
        body = await self.view(f"role=user,action=get_pk,cid={cid}")
        pk = body.get("pk") or body.get("pubkey")
        if not isinstance(pk, str) or not pk:
            raise BeamError(f"get_pk({cid[:12]}…): no pk in {str(body)[:120]}")
        return pk

    async def view_incoming(self, cid: str) -> list[dict[str, int]]:
        """What the relayer has delivered to OUR key on this pipe and we have not claimed.

        ⚠️ The keys are `MsgId` / `amount` — capital M, capital I. Matching `msgId` yields −1 for
        every row, `mine` is always empty, and a genuinely claimable transfer waits forever
        (bridge_watcher.py:559, verified against the live pipe)."""
        body = await self.view(f"role=manager,action=view_incoming,startFrom=0,cid={cid}")
        rows = body.get("incoming")
        if rows is None:
            raise BeamError(f"view_incoming({cid[:12]}…): no 'incoming' key in {str(body)[:120]}")
        out: list[dict[str, int]] = []
        for m in rows:
            for k in ("MsgId", "msgId", "msg_id", "id"):
                if k in m:
                    out.append({"msg_id": int(m[k]), "amount": int(m.get("amount") or 0)})
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

    async def build_receive(self, cid: str, msg_id: int) -> tuple[Any, str]:
        """(raw_data, args) for a CLAIM, with the pipe CID proven present in the bytes.

        Split from `receive` on purpose: this half is a `create_tx:false` read and CANNOT have
        signed anything, so a caller that fails here knows it is safe to plan again."""
        args = f"role=user,action=receive,cid={cid},msgId={int(msg_id)}"
        built = await self.invoke(args)
        raw = built.get("raw_data")
        if not raw:
            raise BeamError(f"claim msg {msg_id}: no raw_data ({str(built)[:160]})")
        if bytes.fromhex(cid) not in _blob(raw):
            raise BeamError(f"claim msg {msg_id}: the pipe CID is NOT in the calldata — refusing")
        return raw, args

    async def receive(self, cid: str, msg_id: int) -> str:
        """CLAIM one delivered message. Returns the Beam txid."""
        raw, _args = await self.build_receive(cid, msg_id)
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
    """Everything here is read from **BeamPay** — the wallet-api is not called at all.

    That is the point of law 10: a per-address ledger balance is the only per-address truth
    that exists on Beam, and a raw wallet balance is one shared UTXO pool that includes value
    that is not ours to spend. The treasury address holds claimed-but-not-yet-shielded value
    and the BEAM every claim, shield and pipe send is paid for with; the max-privacy address
    holds the shielded float a payout may actually cross with."""
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
    out(f"  max-privacy: {mp or '— not configured (the float cannot be read)'}")
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
    out("  asset   aid  treasury(unshielded)  FLOAT(shielded)  locked")
    for key, asset in ASSETS.items():
        try:
            regular = await bp.available_groth(treasury, asset.aid)
            floated = await bp.available_groth(mp, asset.aid) if mp else None
            locked = await bp.locked_groth(treasury, asset.aid)
        except BeamPayError as e:
            out(f"  {key:6} {asset.aid:>4}  unreadable: {e}")
            continue
        out(
            f"  {key:6} {asset.aid:>4}  {_fmt(regular):<21} "
            f"{(_fmt(floated) if floated is not None else '—'):<16} {_fmt(locked)}"
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
    await _print_intents(out)
    return 0


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
    charged = int(row.get("fee_groth") or 0)
    out(f"  our fee (2%) : {_fmt(charged)} — already debited at schedule")
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
    out(f"  relayer share: {share:.3%} (max {settings.max_relayer_share:.0%}) "
        f"{'OK' if share <= settings.max_relayer_share else '⛔ TOO HIGH'}")
    subsidy = fee_groth / charged if charged else float("inf")
    out(f"  vs the 2% charged: {_fmt(fee_groth)} paid against {_fmt(charged)} collected "
        f"({subsidy:.2f}×, limit {settings.max_relayer_subsidy:g}×) "
        f"{'OK' if fee_groth <= charged * settings.max_relayer_subsidy else '⛔ LOSS-MAKING'}")
    bp = beampay.beampay()
    mp = await payouts.float_address()
    out(f"  float address : {mp or '— none configured; the release would hold'}  (BeamPay)")
    try:
        treasury = beampay.treasury_address()
        fl = await bp.available_groth(mp, asset.aid) if mp else 0
        reserved = await payouts.inflight_groth(asset, request_id)
        need = amount + fee_groth + settings.float_min_groth
        out(f"  shielded float: {_fmt(fl)} · {_fmt(reserved)} committed to crossings in flight "
            f"· needs {_fmt(need)} → {'ENOUGH' if fl - reserved >= need else 'NOT ENOUGH'}")
        reg = await bp.available_groth(treasury, asset.aid)
        out(f"  unshielded    : {_fmt(reg)} {asset.key} (treasury) · inputs proven="
            f"{int(settings.beam_send_inputs_proven)} → "
            f"{'OK' if settings.beam_send_inputs_proven or reg <= settings.beam_regular_tolerance_groth else '⛔ REFUSES (spec S2 unanswered)'}")
        budget, why = await payouts.fee_budget("send", bp)
        out(f"  BEAM for fees : {_fmt(await bp.available_groth(treasury, 0))} (treasury, asset 0)"
            f" · this call reserves {payouts.fee_budget_line('send', budget, why)}")
    except BeamPayError as e:
        out(f"  shielded float: unreadable ({e}) — that is 'we cannot see', never 'no float'")
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
        {"txid": "<the txid step 3 returns>", "address": mp or "<PGAS_BEAM_MP_ADDRESS>",
         "trade_ref": request_id}
    ) + "   ← the registration, BEFORE the row advances")
    out("      the float that funds the crossing is the max-privacy address, so the flow books "
        "there; without this the whole invocation books to __house__")
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
    try:
        incoming = await w.view_incoming(asset.beam_cid)
    except BeamError as e:
        out(f"  ⛔ view_incoming unreadable: {e} (that is 'we cannot see', never 'not delivered')")
        return 1
    mine = [m for m in incoming if m["msg_id"] == int(msg_id)]
    out(f"  view_incoming: {len(incoming)} claimable · ours {'PRESENT' if mine else 'not yet'}"
        + (f" (amount {mine[0]['amount']})" if mine else ""))
    args = f"role=user,action=receive,cid={asset.beam_cid},msgId={int(msg_id)}"
    out("")
    out("  the exact JSON-RPC calls (nothing is sent):")
    out("   0) " + json.dumps(
        {
            "jsonrpc": "2.0", "id": 1, "method": "invoke_contract",
            "params": {
                "contract_file": w.shader,
                "args": f"role=manager,action=view_incoming,startFrom=0,cid={asset.beam_cid}",
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


async def cmd_repair_fee(txid: str, apply: bool = False, out: Any = print) -> int:
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
        "adjust_id": f"pgasme:fee:{txid}",
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


USAGE = """python -m pgasme.beam <command>

  status                        wallet height vs node, balances per asset, float, pending intents
  dry-run --payout <id>         the exact calls a bETH → ETH release would make, and the fee
  dry-run --claim <deposit id>  the exact calls a claim would make
  repair-fee --txid <txid>      the ledger/adjust that books an unregistered contract tx's BEAM
                                fee leg off __house__ (a one-off for the OLD claim path)
                                [--apply]  post it; without it nothing is sent

Every command reads only, EXCEPT `repair-fee --apply`, which posts one zero-sum BeamPay
ledger adjustment. Nothing here ever reaches the wallet's signing path.
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
    if cmd == "repair-fee":
        if "--txid" not in argv or argv.index("--txid") + 1 >= len(argv):
            out("repair-fee needs --txid <the settled contract txid>")
            return 2
        return await cmd_repair_fee(
            argv[argv.index("--txid") + 1], apply="--apply" in argv, out=out
        )
    out(USAGE)
    return 2


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(cli_main(list(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":  # pragma: no cover — exercised through cli_main in the tests
    sys.exit(main())
