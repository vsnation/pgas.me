"""The Beam order processors: claim → shield, and the ETH payout executor.

The transition table this file enforces (one operator event per transition, ids only):

  PAYOUT (payout_requests.status)
    scheduled  → releasing  payout_releasing   payouts._payout_scheduled   queued
    releasing  → bridging   payout_bridging    payouts._payout_releasing   queued
    bridging   → delivering payout_delivering  payouts._payout_bridging    queued
    delivering → sent       payout_sent        payouts._payout_delivering  queued
    any        → failed     payout_failed      payouts._fail               IMMEDIATE
    (lost response resolved) payout_send_resolved / payout_send_unconfirmed IMMEDIATE
  TREASURY (deposits.treasury — a sub-machine; deposits.status stays `credited`)
    (none)   → claiming     deposit_claiming   payouts._treasury_new       queued
    claiming → claimed      deposit_claimed    payouts._treasury_claiming  queued
    claimed  → shielding    deposit_shielding  payouts._treasury_claimed   queued
    shielding→ shielded     deposit_shielded   payouts._treasury_shielding queued

`FakeWalletApi` SUBCLASSES the real client and replaces only the transport, so every parse,
every pre-broadcast calldata assertion and every idempotency path in `pgasme/beam.py` runs for
real. Nothing in this file can make a `create_tx: true` call, and the tests assert it.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
from conftest import fund

from pgasme import beam, beampay, ethpipe, ledger, payouts, tg, workers
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
W = "0x1111111111111111111111111111111111111111"
# ⛔ THE SIGN CONVENTION, PINNED TO THE CHAIN. BeamPay books a contract flow as
# `available_delta = -amount` under its own comment "A POSITIVE invoke amount is a wallet
# OUTFLOW" (process_payments.py:216), and the live rows in the reference implementation agree: an AMM swap reads
# `[{asset 0: +500125656929}, {asset 37: -5055196828}]` — BEAM SPENT positive, the asset
# RECEIVED negative — and a b2e send on the pipe Pgas.me uses carries `{asset 36: +15995980}`
# against a −0.15995980 bETH movement, while claims on that pipe are negative.
# A SPEND IS POSITIVE. A RECEIVE IS NEGATIVE. Every fake below encodes exactly this.
SEND_IS_POSITIVE = +15_995_980  # cid 8872509d…847369, a 0.15995980 bETH b2e send
CLAIM_IS_NEGATIVE = -75_058  # cid 7c66181b…e8c056, tx b5d4ffa4…, a live e2b claim
# a regular Beam (SBBS) address is 64/66 hex chars; a max-privacy token is base58 and long
TREASURY = "77" + "9a" * 32
MP = "MaxPrivacyTokenForPgasTreasury" + "q" * 60
OTHER_W = "0x2222222222222222222222222222222222222222"
GROTH = 10**8

# eth_feeHistory answers, hex as the RPC gives them. The third is the reference sample below.
LOW_GAS = {"baseFeePerGas": ["0x4C4B400", "0x4C4B400"], "reward": [["0x2625A00"]]}  # 0.2 gwei
# Mainnet as it actually was at 2026-09-09 ~21:40Z, fetched live and fed to the reference
# implementation's `bridge_fee.py` itself:
#   run bridge_fee.py in the reference implementation: estimate_max_gas_price_gwei(...)
#   → gas 0.153283829 gwei · ETH 2.759108922e-05 (2759 groth) · DAI 0.06897772305 (6897772 groth)
LIVE_GAS = {
    "baseFeePerGas": ["0x3e75962", "0x3c7231a", "0x3eb7044", "0x3f686b1", "0x38d65b7",
                      "0x3d5a6d8", "0x3d8be71", "0x3cb71cc", "0x3c1fd14", "0x3a93c58",
                      "0x37ecdfb"],
    "reward": [["0x5f5e100"], ["0x1037622"], ["0x989680"], ["0x10041ad8"], ["0x22550ff"],
               ["0xe975a0"], ["0xe4e1c0"], ["0x2faf080"], ["0x22550ff"], ["0x133b205a"]],
}


def _wallet_args(raw: str) -> dict[str, str]:
    """`k=v,k=v` — the wallet's own `args` format, split the way the WALLET splits it.

    ⛔ THE WALLET SPLITS ON `,` BEFORE THE SHADER SEES ANYTHING (`ProcessorManager::AddArgs`,
    bvm2.cpp:3373). A value containing a comma — `indexes=1,2,3` — therefore reaches the shader
    as `indexes=1` plus two broken pairs, and the two indexes that vanished are two messages
    nobody can see. The fake reproduces that rather than being clever about it, so a caller that
    ever goes back to a comma list fails here instead of on the box. The list separator is `;`."""
    out: dict[str, str] = {}
    for tok in str(raw).split(","):
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _hex(n: int) -> str:
    return hex(n)


class FakeBeamPay(beampay.BeamPay):
    """A recording BeamPay. Only `_http` is fake, so every key choice, every status check and
    every parse in `pgasme/beampay.py` runs for real — including the X-API-Key each route
    demands, which is enforced here exactly as `auth.require_scope` enforces it on the box: an
    `/internal/*` call presented with the ordinary key is a 403, not a courtesy.

    ONE store backs both read routes, as on the box: `db.txs`. `/transactions` filters it on
    sender/receiver (so contract txs, whose sender and receiver are "", are only visible with
    NO address) and `/internal/contract_tx/{id}` reads it by id and reports `success` as
    `booked`."""

    KEY = "pgas-beampay-key-0123456789abcdef"
    INTERNAL_KEY = "pgas-beampay-internal-0123456789abcdef"

    def __init__(self) -> None:
        super().__init__(
            url="http://beampay.invalid",
            key=self.KEY,
            internal_key=self.INTERNAL_KEY,
            timeout=1.0,
        )
        self.calls: list[tuple[str, str, Any]] = []
        self.height_ = 4_030_000
        self.in_sync = True
        self.addresses: dict[str, dict[str, dict[str, int]]] = {}
        self.address_types: dict[str, str] = {}
        # ⛔ THE WALLET'S OWN TOTALS, WHICH ARE NOT THE LEDGER'S. `/wallet_status.totals` is what
        # the WALLET can spend right now, split by bucket, and it is the fact BeamPay's
        # per-address ledger cannot express: on 2026-09-10 the live wallet reported asset 36
        # `available 0 / available_mp 0 / maturing_mp 1652864` while BeamPay's registry summed
        # 2,652,864 — a release would have passed every ledger gate and handed the wallet a send
        # it could not fund. By DEFAULT the fake derives the totals from the balances it already
        # holds (regular = every non-max_privacy address, shielded = every max_privacy one), so a
        # wallet that owes what the ledger says can spend it. `wallet_totals[asset_id]` overrides
        # any field of one asset's row, which is how a test models the max-privacy lock.
        self.wallet_totals: dict[int, dict[str, int]] = {}
        self.invalid: set[str] = set()  # answered is_valid: false
        self.tx_rows: list[dict[str, Any]] = []  # db.txs, oldest first
        self.tx_index: dict[str, dict[str, Any]] = {}
        self.expectations: dict[str, dict[str, Any]] = {}
        self.expect_refusal: str | None = None  # a terminal refusal string
        self.raise_on: set[str] = set()  # path (or "METHOD /path") prefixes that answer 500
        self.missing_routes: set[str] = set()  # routes this build does not have: 404 Not Found
        self.withdraw_ok = True
        self.withdraw_msg = "Insufficient asset balance"
        self.withdrawals: list[dict[str, Any]] = []
        self.withdraw_lands = True  # the daemon emits the transaction immediately
        self.withdraw_fee = 1_100_000  # BeamPay's own offline/max-privacy fee
        self.next_created = "MaxPrivacyTokenCreatedByBeamPay" + "z" * 40
        # ⛔ A REGULAR `/create_wallet` ANSWERS AN SBBS ADDRESS, NOT A TOKEN. A regular Beam
        # address is 64/66 hex characters and a max-privacy one is a long base58 token; the
        # crossing-address path (T40) refuses the second shape on purpose, because a crossing
        # funded into a max-privacy address could not be spent for 72 h. The fake has to be
        # able to answer BOTH shapes or that guard can never be exercised.
        self.next_regular = "7c" + "5e" * 31
        self.created: list[dict[str, Any]] = []  # every /create_wallet body, in order
        self.next_tx = 1
        self.now = time.time

    # ------------------------------------------------------------------ helpers

    def register(self, address: str, kind: str = "regular") -> str:
        self.addresses.setdefault(address, {"available": {}, "locked": {}})
        self.address_types[address] = kind
        return address

    def fund(self, address: str, asset_id: int, groth: int) -> None:
        bal = self.addresses.setdefault(address, {"available": {}, "locked": {}})
        bal["available"][str(asset_id)] = bal["available"].get(str(asset_id), 0) + int(groth)

    def add_tx(self, **row: Any) -> dict[str, Any]:
        doc = {
            "_id": row.pop("txId", f"bp-{self.next_tx}"),
            "status": beam.TX_COMPLETED,
            "status_string": "completed",
            "success": True,
            "type": 12,
            "type_string": "contract",
            "asset_id": "0",
            "value": "0",
            "fee": "0",
            "sender": "",
            "receiver": "",
            "comment": "",
            "confirmations": 0,
            "kernel": None,
            "invoke_data": [],
            "create_time": int(self.now()),
            **row,
        }
        self.next_tx += 1
        self.tx_rows.append(doc)
        self.tx_index[doc["_id"]] = doc
        return doc

    def tx(self, txid: str) -> dict[str, Any]:
        return self.tx_index[txid]

    def withdrawal_row(self, **row: Any) -> dict[str, Any]:
        """One ordinary withdrawal, exactly as the live wallet stores it: `type` is the STRING
        "withdrawal" and there is no `type_string` at all. `find_contract_tx` walks with NO
        address filter (the only way a contract tx is visible), so these are handed to it
        intermixed and dominate the newest page."""
        return self.add_tx(
            type="withdrawal",
            type_string=None,
            sender="7f" + "11" * 32,
            receiver="7f" + "22" * 32,
            value="1000000",
            fee="100000",
            **row,
        )

    def book_attribution(self, txid: str) -> dict[str, int]:
        """BeamPay's OWN attribution arithmetic, applied to the registered address.

        `contract_attribution.attribution_deltas(invoke_data, fee)` walks every amount of every
        invocation — `deltas[asset] -= amount`, because a POSITIVE invoke amount is a wallet
        OUTFLOW — and then ends with `deltas["0"] -= fee`: **the whole BEAM transaction fee is
        debited from the address the txid was registered to**, and it is booked even when that
        drives the address negative ("a negative balance is alerted on, never silently
        adjusted"). Returns the deltas it applied."""
        row = self.tx_index[txid]
        addr = (self.expectations.get(txid) or {}).get("address")
        if not addr:
            return {}
        deltas: dict[str, int] = {}
        for item in row.get("invoke_data") or []:
            for a in item.get("amounts") or []:
                aid = str(a.get("asset_id"))
                deltas[aid] = deltas.get(aid, 0) - int(a.get("amount") or 0)
        deltas["0"] = deltas.get("0", 0) - int(row.get("fee") or 0)
        bal = self.addresses.setdefault(addr, {"available": {}, "locked": {}})
        for aid, d in deltas.items():
            bal["available"][aid] = bal["available"].get(aid, 0) + d
        return deltas

    def totals(self) -> list[dict[str, Any]]:
        """`/wallet_status.totals`, in the shape the live wallet answers (read on the box
        2026-09-10 10:2xZ): one row per asset carrying `available_regular` / `available_mp` /
        `maturing_regular` / `maturing_mp` and the `available` / `maturing` sums.

        Derived from the addresses this fake holds unless `wallet_totals` overrides it: an
        address created `max_privacy` is shielded value, everything else is regular. A negative
        per-address balance (BeamPay books an invocation's whole BEAM fee to the REGISTERED
        address, which drives it below zero) is not a negative wallet balance — the wallet holds
        what it holds — so each bucket is floored at 0."""
        buckets: dict[int, dict[str, int]] = {}
        for addr, bal in self.addresses.items():
            mp = self.address_types.get(addr) == "max_privacy"
            for aid, v in bal["available"].items():
                row = buckets.setdefault(
                    int(aid),
                    {"available_regular": 0, "available_mp": 0,
                     "maturing_regular": 0, "maturing_mp": 0, "locked": 0},
                )
                row["available_mp" if mp else "available_regular"] += int(v)
            for aid, v in bal["locked"].items():
                row = buckets.setdefault(
                    int(aid),
                    {"available_regular": 0, "available_mp": 0,
                     "maturing_regular": 0, "maturing_mp": 0, "locked": 0},
                )
                row["locked"] += int(v)
        for aid, over in self.wallet_totals.items():
            row = buckets.setdefault(
                int(aid),
                {"available_regular": 0, "available_mp": 0,
                 "maturing_regular": 0, "maturing_mp": 0, "locked": 0},
            )
            # only the numbers feed the derivation below; anything else a test pins (a bucket
            # spelled as a word, say) is applied RAW at the end, because a reader that refuses
            # a shape it does not understand has to be able to MEET one
            row.update({k: int(v) for k, v in over.items() if isinstance(v, int)})
        out: list[dict[str, Any]] = []
        for aid, row in sorted(buckets.items()):
            reg = max(0, int(row["available_regular"]))
            mp = max(0, int(row["available_mp"]))
            mreg = max(0, int(row["maturing_regular"]))
            mmp = max(0, int(row["maturing_mp"]))
            out.append(
                {
                    "asset_id": aid,
                    "available": reg + mp,
                    "available_regular": reg,
                    "available_mp": mp,
                    "maturing": mreg + mmp,
                    "maturing_regular": mreg,
                    "maturing_mp": mmp,
                    "locked": max(0, int(row["locked"])),
                }
            )
            out[-1].update({k: v for k, v in self.wallet_totals.get(aid, {}).items()
                            if not isinstance(v, int)})
        return out

    def paths(self) -> list[str]:
        return [path for _m, path, _b in self.calls]

    def bodies_for(self, path: str) -> list[Any]:
        return [b for _m, p, b in self.calls if p == path]

    # ------------------------------------------------------------------ transport

    async def _http(self, method, url, *, params, json_body, headers):
        path = url[len(self.url) :]
        self.calls.append((method, path, json_body if json_body is not None else params))
        internal = path.startswith("/internal/")
        want = self.INTERNAL_KEY if internal else self.KEY
        got = headers.get("X-API-Key")
        if got != want:
            # exactly what the box answers: a scopeless key never reaches /internal/*
            return 403, {"detail": "Invalid API key" if not got else "insufficient_scope"}
        # `raise_on` accepts "/path" (every method) or "POST /path" (that method only) — a
        # registration POST that fails while the side-effect-free GET probe answers is a real
        # state, and the two must be separable or a test cannot reach either one.
        if any(
            path.startswith(pfx) or f"{method} {path}".startswith(pfx) for pfx in self.raise_on
        ):
            return 500, {"detail": "fake outage"}
        if any(
            path.startswith(p) or f"{method} {path}".startswith(p) for p in self.missing_routes
        ):
            # a FastAPI deployment that does not have this route at all — BeamPay master
            # `e09bfc2`, the commit this deployment was copied from, has no /internal/* routes
            return 404, {"detail": "Not Found"}
        return self._route(method, path, params or {}, json_body)

    def _route(self, method, path, params, body):  # noqa: C901 — a router is a router
        if path == "/balances":
            bal = self.addresses.get(params.get("address"))
            if bal is None:
                return 404, {"detail": "Address not found"}
            return 200, {
                "available": {k: str(v) for k, v in bal["available"].items()},
                "locked": {k: str(v) for k, v in bal["locked"].items()},
            }
        if path == "/wallet_status":
            return 200, {
                "status": True,
                "result": {
                    "current_height": self.height_,
                    "is_in_sync": self.in_sync,
                    "totals": self.totals(),
                },
            }
        if path == "/validate_address":
            return 200, {"status": True, "result": params.get("address") not in self.invalid}
        if path == "/create_wallet":
            # ⛔ A REAL /create_wallet ANSWERS A NEW ADDRESS EVERY TIME. A fake that answered
            # one address forever would let "a fresh max-privacy address per shield chunk" pass
            # while every chunk went to the same one — which is precisely the collision the
            # per-chunk address exists to prevent, so the fake must be able to show it.
            kind = str((body or {}).get("wallet_type") or "regular")
            n = len(self.created)
            addr = (
                f"{self.next_regular[:-2]}{n % 100:02d}"
                if kind == "regular"
                else f"{self.next_created}{n}"
            )
            self.created.append(dict(body or {}, address=addr))
            self.register(addr, str((body or {}).get("wallet_type") or "regular"))
            return 200, {"address": addr, "note": (body or {}).get("note")}
        if path == "/transactions":
            b = body or {}
            addr = b.get("address")
            rows = [
                r
                for r in self.tx_rows
                if not addr or r.get("sender") == addr or r.get("receiver") == addr
            ]
            rows.sort(key=lambda r: int(r.get("create_time") or 0), reverse=True)
            skip, count = int(b.get("skip") or 0), int(b.get("count") or 10)
            return 200, {"txs": rows[skip : skip + count], "count": len(rows)}
        if path == "/withdraw":
            return self._withdraw(body or {})
        if path == "/internal/expect_contract_tx":
            if method == "GET":
                return 200, {"available": True, "ttl_sec": 86400, "pending": 0}
            if self.expect_refusal:
                return 409, {"detail": self.expect_refusal}
            b = body or {}
            if b["address"] not in self.addresses:
                return 404, {"detail": "address_not_found"}
            replayed = b["txid"] in self.expectations
            self.expectations.setdefault(b["txid"], dict(b))
            return 200, {"status": True, **b, "replayed": replayed, "ttl_sec": 86400}
        if path.startswith("/internal/contract_tx/"):
            row = self.tx_index.get(path.rsplit("/", 1)[-1])
            if row is None:
                return 404, {"detail": "tx_not_found"}
            if row.get("type") != 12 and row.get("type_string") != "contract":
                return 400, {"detail": "not_a_contract_tx"}
            return 200, {
                "txId": row["_id"],
                "booked": row.get("success", False) is True,
                "status": int(row.get("status", 0) or 0),
                "status_string": row.get("status_string", ""),
                "confirmations": row.get("confirmations", 0),
                "kernel": row.get("kernel"),
                "fee": str(row.get("fee", "0")),
                "invoke_data": row.get("invoke_data") or [],
                "failure_reason": row.get("failure_reason", ""),
                "create_time": row.get("create_time", 0),
                "adjustments": [],
                "attributed_to": (self.expectations.get(row["_id"]) or {}).get("address"),
            }
        return 404, {"detail": f"no fake route for {path}"}

    def _withdraw(self, b: dict[str, Any]) -> tuple[int, Any]:
        self.withdrawals.append(dict(b))
        if not self.withdraw_ok:
            return 200, {"status": False, "msg": self.withdraw_msg}
        frm, aid, amount = b["from_address"], str(b["asset_id"]), int(b["amount"])
        bal = self.addresses.setdefault(frm, {"available": {}, "locked": {}})
        bal["available"][aid] = bal["available"].get(aid, 0) - amount
        bal["available"]["0"] = bal["available"].get("0", 0) - self.withdraw_fee
        if self.withdraw_lands:
            self.add_tx(
                txId=f"wd-{self.next_tx}",
                # ⛔ THE LIVE SHAPE. BeamPay writes `type` as the STRING "withdrawal" at
                # reservation time (process_payments.py:1605), before the send — 2,628 of the
                # newest 3,000 rows in the reference implementation's tx collection are `{type: "withdrawal",
                # type_string: None}` against 171 contract rows. The fake used to write the
                # integer 4, which production never writes, so no walk in this suite ever met
                # the row that made `find_contract_tx` raise ValueError on both money paths.
                type="withdrawal",
                type_string=None,
                asset_id=aid,
                value=str(amount),
                fee=str(self.withdraw_fee),
                sender=frm,
                receiver=b["to_address"],
                comment=b.get("comment", ""),
                kernel=f"kernel-wd-{self.next_tx}",
            )
            to = self.addresses.setdefault(b["to_address"], {"available": {}, "locked": {}})
            to["available"][aid] = to["available"].get(aid, 0) + amount
        return 200, {"status": True, "result": True, "msg": "Withdrawal request recorded"}


class FakeWalletApi(beam.Wallet):
    """A recording wallet-api. Only `rpc` is fake — and the ONLY two methods it implements are
    the two BeamPay has no endpoint for. Anything else raises, which is how this fake proves
    law 10 rather than merely documenting it."""

    def __init__(self, bp: FakeBeamPay | None = None) -> None:
        super().__init__(url="http://fake.invalid/api/wallet", timeout=1.0)
        self.shader = "/opt/pgasme/beam/pipe_app.wasm"
        self.bp = bp
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.height = 4_030_000
        self.incoming: list[dict[str, int]] = []
        self.local_msgs: dict[int, dict[str, Any]] = {}
        self.next_txid = 1
        self.raise_on: set[str] = set()  # method names that answer with a BeamError
        # ── one receiver key per deposit (WO-20260910-3). `patched` is "is K1's pipe app the
        # one being called?" and it defaults to FALSE, because the SHIPPED app is what is on
        # the box: it has no concept of an index, ignores `index=` entirely and answers the
        # cid-derived key for every value of it (measured live, T41 §1). That default is what
        # makes an unpatched box a test case rather than a silent success.
        self.patched = False
        self.pk = "02" + "ab" * 32  # the pipe's LEGACY (cid-derived) receiver key
        self.get_pk_args: list[int | None] = []  # the index asked for, per call
        self.view_index_args: list[str | None] = []  # the `indexes=` of each view_incoming
        self.receive_index_args: list[int | None] = []  # the index each claim signed with
        self.omit_receiver = False
        self.omit_cid = False
        # ⛔ COINS, NOT A BALANCE. Beam locks a WHOLE UTXO per pending transaction, so what
        # bounds concurrent releases is the COUNT of spendable coins, not their total. The fake
        # derives the list from BeamPay's totals and splits each bucket into `default_coins`
        # coins; `coins[(asset_id, "regular"|"shielded")]` overrides the count for one bucket,
        # which is how a test models the box's shape (2 spendable BEAM coins, 0 bETH ones).
        self.coins: dict[tuple[int, str], int] = {}
        self.default_coins = 8
        self.bad_utxo = False  # answer a coin whose `status` is not a number at all
        # exact per-coin amounts for one bucket, when a test cares about SIZE and not only
        # count (the box holds 0.01 BEAM and 9.835 BEAM — only one of them can pay a fee)
        self.coin_amounts: dict[tuple[int, str], list[int]] = {}
        self.on_invoke_send: Any = None  # hook: called when a send is BUILT (create_tx:false)
        self.invoke_fee = 1_100_000  # what the WALLET charges for an invocation; we never set it
        self._pending: dict[str, Any] | None = None

    @property
    def txs(self) -> dict[str, dict[str, Any]]:
        """The contract transactions this wallet made, as BeamPay's daemon recorded them —
        one store, so a test that settles a tx settles the one the code reads."""
        return self.bp.tx_index if self.bp else {}

    # ---------------------------------------------------------------- transport

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        p = dict(params or {})
        self.calls.append((method, p))
        if method in self.raise_on:
            raise beam.BeamError(f"{method}: fake outage")
        fn = getattr(self, f"_m_{method}", None)
        if fn is None:
            raise beam.BeamError(f"{method}: not implemented by the fake")
        return fn(p)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def params_for(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    # ---------------------------------------------------------------- methods

    def _m_process_invoke_data(self, p: dict[str, Any]) -> dict[str, Any]:
        txid = f"beamtx-{self.next_txid}"
        self.next_txid += 1
        # the wallet creates the OUTGOING PIPE MESSAGE when the send is submitted — so its id is
        # always ABOVE the `local_msg_count` the release read a moment earlier. A test that
        # seeds the message beforehand is seeding a message that by construction is not ours,
        # which is exactly what the inclusive `find_local_msg` floor used to accept as proof.
        msg = (self._pending or {}).get("local_msg")
        if msg:
            self.local_msgs[(max(self.local_msgs) if self.local_msgs else 0) + 1] = dict(msg)
        if self.bp is not None:
            # the daemon picks it up: a settled, booked contract tx carrying the flow the
            # invocation was built for, and the fee the WALLET chose (never one we sent)
            self.bp.add_tx(
                txId=txid,
                kernel=f"kernel-{txid}",
                confirmations=0,
                fee=str(self.invoke_fee),
                invoke_data=(self._pending or {}).get("invoke_data") or [],
            )
        return {"txid": txid}

    def _m_get_utxo(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """The wallet's UTXO list, in the shape the live wallet answers (read on the box
        2026-09-10): `type` is `norm`/`chng` for a regular output and `shld` for a shielded
        one, and `status` 1 is available, 3 maturing, 6 spent. Paged with count/skip."""
        rows: list[dict[str, Any]] = []
        for t in (self.bp.totals() if self.bp else []):
            aid = int(t["asset_id"])
            for field, kind, bucket in (
                ("available_regular", "norm", "regular"),
                ("available_mp", "shld", "shielded"),
            ):
                value = int(t.get(field) or 0)
                exact = self.coin_amounts.get((aid, bucket))
                if exact is not None:
                    amounts = list(exact)
                else:
                    n = self.coins.get((aid, bucket), self.default_coins) if value > 0 else 0
                    amounts = [value // n] * n if n else []
                rows.extend(
                    {
                        "amount": amount, "asset_id": aid, "type": kind,
                        "status": 1, "status_string": "available",
                        "id": f"{aid}-{bucket}-{i}", "maturity": self.height,
                        "createTxId": "", "spentTxId": "",
                    }
                    for i, amount in enumerate(amounts)
                )
            if int(t.get("maturing_mp") or 0):
                rows.append(
                    {
                        "amount": int(t["maturing_mp"]), "asset_id": aid, "type": "shld",
                        "status": 3, "status_string": "maturing",
                        "id": f"{aid}-shld-maturing", "maturity": self.height,
                        "createTxId": "", "spentTxId": "",
                    }
                )
        if self.bad_utxo and rows:
            rows[0] = dict(rows[0], status="who knows")
        skip = int(p.get("skip") or 0)
        count = int(p.get("count") or len(rows) or 1)
        return rows[skip : skip + count]

    def pk_for(self, index: int | None) -> str:
        """What the app being called answers for `get_pk`.

        ⛔ THE SHIPPED APP ANSWERS THE LEGACY KEY FOR EVERY INDEX. That is the whole trap: a
        33-byte answer to `index=7` is not evidence that a per-deposit key was derived, so the
        fake reproduces it rather than pretending an unpatched box would fail loudly."""
        if not index or int(index) <= 0 or not self.patched:
            return self.pk
        return "02" + f"{int(index):064x}"

    def _m_invoke_contract(self, p: dict[str, Any]) -> dict[str, Any]:
        assert p.get("create_tx") is False, "a test must NEVER ask for create_tx: true"
        args = _wallet_args(str(p["args"]))
        cid = args.get("cid", "")
        action = args.get("action")
        if action == "get_pk":
            idx = int(args["index"]) if args.get("index") else None
            self.get_pk_args.append(idx)
            body = {"pk": self.pk_for(idx)}
            if self.patched:
                body["index"] = int(idx or 0)  # the patched app echoes what it derived for
            return {"output": json.dumps(body)}
        if action == "view_incoming":
            raw = args.get("indexes")
            self.view_index_args.append(raw)
            # K1's shader, exactly: the legacy key (0) is ALWAYS in the match set; an explicit
            # `indexes` list (`;`-separated, any non-digit is a separator) replaces the window;
            # with neither `indexes` nor `maxIndex` it derives a DEFAULT window of 1..64. An
            # index outside the resulting set is INVISIBLE — never "not delivered yet" — which
            # is the property `receiver_keys.open_indexes` exists to satisfy.
            want: set[int] | None
            if not self.patched:
                want = None  # the shipped app has no concept of an index at all
            elif raw:
                want = {int(x) for x in re.split(r"\D+", raw) if x} | {0}
            elif args.get("maxIndex"):
                want = set(range(0, int(args["maxIndex"]) + 1))
            else:
                want = set(range(0, 65))
            rows = []
            for m in self.incoming:
                i = int(m.get("index") or 0)
                if want is not None and i not in want:
                    continue
                row: dict[str, Any] = {"MsgId": m["msg_id"], "amount": m["amount"]}
                if self.patched:
                    row["index"] = i
                rows.append(row)
            return {"output": json.dumps({"incoming": rows})}
        if action == "local_msg_count":
            return {"output": json.dumps({"count": max(self.local_msgs) if self.local_msgs else 0})}
        if action == "local_msg":
            m = self.local_msgs.get(int(args["msgId"]))
            return {"output": json.dumps(m) if m else ""}
        if action in ("send", "receive"):
            if action == "send" and self.on_invoke_send is not None:
                self.on_invoke_send(args)
            # ⛔ A SPEND IS POSITIVE, A RECEIVE IS NEGATIVE (see SEND_IS_POSITIVE above). The
            # fake used to write `-(amount + relayerFee)` for a send and NOTHING AT ALL for a
            # receive, so the suite was green about a resolver aimed at the opposite operation
            # and the claim resolver had never been exercised against any invoke_data.
            if action == "send":
                signed = int(args["amount"]) + int(args["relayerFee"])
            else:
                asked = int(args["index"]) if args.get("index") else None
                self.receive_index_args.append(asked)
                msg = int(args["msgId"])
                if self.patched:
                    # ⛔ K1's app checks the message's stored receiver against the key of the
                    # index it was given and REFUSES before signing — no raw_data at all. The
                    # fake refuses too, so "we passed the wrong index" fails here and not with a
                    # signature over somebody else's message.
                    delivered = next(
                        (int(r.get("index") or 0) for r in self.incoming
                         if int(r["msg_id"]) == msg),
                        None,
                    )
                    if delivered is not None and delivered != int(asked or 0):
                        return {"output": json.dumps({"error": (
                            "receiver key mismatch: this message was not sent to the key of "
                            "this index")})}
                signed = -next(
                    (int(m["amount"]) for m in self.incoming if int(m["msg_id"]) == msg), 0
                )
            self._pending = {
                "invoke_data": [
                    {"contract_id": cid, "amounts": [{"asset_id": 36, "amount": signed}]}
                ]
                if signed
                else [],
                "local_msg": {
                    "amount": int(args["amount"]),
                    "receiver": args["receiver"],
                    "relayerFee": int(args["relayerFee"]),
                }
                if action == "send"
                else None,
            }
            blob = b"\x99" * 4
            if not self.omit_cid:
                blob += bytes.fromhex(cid)
            if action == "send" and not self.omit_receiver:
                blob += bytes.fromhex(args["receiver"][2:].lower())
            blob += b"\x77" * 8
            return {"raw_data": list(blob)}
        raise beam.BeamError(f"unknown shader action {action}")


class FakeEth:
    """An Ethereum RPC pool where exactly one endpoint serves historical state, and balances
    are a step function of the deliveries scripted into it."""

    ARCHIVE = "https://archive.test"
    LITE = "https://lite.test"

    def __init__(self, head: int = 20_000) -> None:
        self.urls = [self.LITE, self.ARCHIVE]
        self.head = head
        self.fee_history: dict[str, Any] = dict(LOW_GAS)
        # (block, address) → delta in wei; a balance is the sum of every delta up to that block
        self.events: list[tuple[int, str, int]] = []
        self.start: dict[str, int] = {}
        self.blocks: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any, Any, bool]] = []
        self.archive_only = True
        # eth_getCode: address (lowercase) → code hex. Anything not listed is a bare EOA ("0x");
        # an address in `unreadable` makes the endpoint refuse to answer, which is the state a
        # release-time destination re-check must treat as "not a verdict", never as "no code".
        self.code: dict[str, str] = {}
        self.unreadable: set[str] = set()
        self.head_dead = False

    def credit(self, block: int, addr: str, delta: int) -> None:
        self.events.append((block, addr.lower(), delta))

    def balance(self, addr: str, block: int) -> int:
        a = addr.lower()
        return self.start.get(a, 0) + sum(d for b, x, d in self.events if x == a and b <= block)

    async def block_number(self, prefer: str | None = None, pin: bool = False) -> int:
        # ⛔ THE DOUBLE HAS TO BE DEAD WHERE THE CALLER READS (T40b F13). `head_dead` used to
        # stop `head_from` only, and the destination re-read has moved to
        # `ethpipe.code_at_head_anywhere`, which asks each ENDPOINT for its own head through
        # here — so a "dead pool" that still answered this one was a pool the guard could not
        # meet. The prober must call the way the caller calls, and so must the fake.
        if self.head_dead:
            raise ethpipe.RpcError("eth_blockNumber: no endpoint answered")
        return self.head

    async def head_from(self) -> tuple[int, str]:
        if self.head_dead:
            raise ethpipe.RpcError("eth_blockNumber: no endpoint answered")
        return self.head, self.LITE

    def _answer(self, method: str, params: Any) -> Any:
        if method == "eth_feeHistory":
            return self.fee_history
        if method == "eth_getBalance":
            return _hex(self.balance(params[0], int(params[1], 16)))
        if method == "eth_getBlockByNumber":
            return self.blocks.get(int(params[0], 16))
        if method == "eth_getCode":
            addr = str(params[0])
            if addr.lower() in self.unreadable:
                raise ethpipe.RpcError(f"eth_getCode {addr}: no endpoint answered")
            return self.code.get(addr.lower(), "0x")
        raise ethpipe.RpcError(f"{method}: not scripted")

    async def call_on(self, url: str, method: str, params: Any) -> Any:
        if self.archive_only and method == "eth_getBalance" and url != self.ARCHIVE:
            raise ethpipe.RpcError(f"{url}: no historical state")
        return self._answer(method, params)

    async def call(
        self, method: str, params: Any, prefer: str | None = None, pin: bool = False
    ) -> Any:
        self.calls.append((method, params, prefer, pin))
        if pin and prefer is None:
            raise ethpipe.RpcError("a pinned call needs an endpoint")
        if pin and self.archive_only and method == "eth_getBalance" and prefer != self.ARCHIVE:
            raise ethpipe.RpcError(f"{prefer}: no historical state")
        return self._answer(method, params)


@pytest.fixture(autouse=True)
def beam_pay(monkeypatch: pytest.MonkeyPatch) -> FakeBeamPay:
    """BeamPay is the system of record, so it exists in every test that touches Beam: the
    treasury address registered and stocked with BEAM for fees, and the max-privacy address
    registered and holding the shielded float."""
    bp = FakeBeamPay()
    beampay.reset_health()  # observed health is per-process state
    bp.register(TREASURY, "regular")
    bp.register(MP, "max_privacy")
    bp.fund(TREASURY, 0, 10 * GROTH)  # 10 BEAM of fee money
    bp.fund(MP, 36, 5 * GROTH)  # the shielded float
    beampay.set_beampay(bp)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    # the working-float policy (PGAS_SHIELD_KEEP_GROTH) has its own file
    # (test_beam_payout_spendable); this one tests the shield MECHANICS, so the
    # policy is pinned out of the way rather than silently deciding these cases
    monkeypatch.setattr(settings, "shield_keep_groth", 0)
    yield bp
    beampay.set_beampay(None)


@pytest.fixture(autouse=True)
def beam_wallet(monkeypatch: pytest.MonkeyPatch, beam_pay: FakeBeamPay) -> FakeWalletApi:
    w = FakeWalletApi(beam_pay)
    beam.set_wallet(w)
    payouts.reset_archive_pin()
    monkeypatch.setattr(settings, "beam_shader", w.shader)
    yield w
    beam.set_wallet(None)


@pytest.fixture
def eth(monkeypatch: pytest.MonkeyPatch) -> FakeEth:
    fake = FakeEth()
    monkeypatch.setattr(workers, "get_rpc", lambda: fake)
    return fake


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "payout_direct_enabled", True)
    monkeypatch.setattr(settings, "claim_enabled", True)
    monkeypatch.setattr(settings, "shield_enabled", True)


async def make_payout(
    mock_db: Any, amount: int = 500_000, rid: str = "req1", **over: Any
) -> dict[str, Any]:
    """A scheduled 0.005 bETH payout with the ledger debit that paid for it."""
    now = time.time()
    row = {
        "_id": rid,
        "account_id": "acct1",
        "asset": "ETH",
        "mode": "direct",
        "W": W,
        "amount_groth": amount,
        "fee_groth": amount * 2 // 100,
        "window_s": 0,
        "release_at": now - 60,
        "status": "scheduled",
        "dest_chain": 1,
        "created_at": now - 120,
        "updated_at": now - 120,
    }
    row.update(over)
    await mock_db["pgasme_test"].payout_requests.insert_one(dict(row))
    await ledger.credit("acct1", "ETH", 10 * amount, f"seed-{rid}")
    # the debit the row was written against — amount + our fee, plus the bridge fee it funded if
    # this is a post-2026-09-10 row (`bridge_fee_groth` passed through `over`). ONE call, so a
    # test row is debited exactly the way `routers/withdrawals._write_items` debits a real one.
    await ledger.schedule(
        "acct1",
        "ETH",
        amount + row["fee_groth"],
        rid,
        "test",
        bridge_fee_groth=int(row.get("bridge_fee_groth") or 0),
    )
    return row


async def make_deposit(mock_db: Any, dep_id: str = "dep1", value: int = 12_000_000) -> dict:
    now = time.time()
    doc = {
        "_id": dep_id,
        "account_id": "acct1",
        "asset": "ETH",
        "mode": "direct",
        "status": "credited",
        "src": {},
        "eth": {"tx": "0x" + "ab" * 32, "block": 100, "msg_id": 222},
        "value_groth": value,
        "created_at": now - 600,
        "credited_at": now - 300,
        "updated_at": now - 300,
    }
    await mock_db["pgasme_test"].deposits.insert_one(dict(doc))
    # ⛔ THE CLAIM BOOKS THIS DEPOSIT'S bETH TO THE TREASURY, and the shield spends it from
    # there. The fake applies attribution deltas only when a test asks it to, so without this
    # the ledger shows a treasury that never received the deposit it is about to shield — and
    # the working-float policy (`payouts._treasury_shielding`), which reads exactly that
    # balance, would be measuring a wallet that does not exist.
    bp = beampay.beampay()
    if isinstance(bp, FakeBeamPay):
        bp.fund(TREASURY, ASSETS[str(doc["asset"])].aid, int(value))
    return doc


def send_args(w: FakeWalletApi) -> str:
    """The `args` of the pipe SEND that was built, and there must be exactly one.

    NOT `params_for("invoke_contract")[0]`: a release now reads the pipe's `local_msg_count`
    first — the floor it needs to find its own outgoing message again — so the send is no
    longer the first invoke of the pass."""
    sends = [p["args"] for p in w.params_for("invoke_contract") if "action=send," in p["args"]]
    assert len(sends) == 1, sends
    return sends[0]


async def kinds(mock_db: Any) -> list[str]:
    rows = await mock_db["pgasme_test"].events.find({}).sort("at", 1).to_list(200)
    return [r["kind"] for r in rows]


async def payout(mock_db: Any, rid: str = "req1") -> dict[str, Any]:
    return await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid})


async def deposit(mock_db: Any, dep_id: str = "dep1") -> dict[str, Any]:
    return await mock_db["pgasme_test"].deposits.find_one({"_id": dep_id})


# ============================================================== the relayer fee (bridge_fee.py)


@pytest.mark.parametrize(
    "rate_id,fee_history,eth_usd,asset_usd,gas_gwei,expect_groth",
    [
        # Golden numbers produced by RUNNING the reference implementation's bridge_fee.py itself
        # (relayer_fee(rate_id, rpc=<stub>, eth_usd=…, asset_usd=…)) on 2026-09-09 with these
        # exact inputs. ETH: the USD terms cancel, as in their code.
        ("ETH", {"baseFeePerGas": ["0x5F5E100", "0x4C4B400"], "reward": [["0x2540BE400"]]},
         2500.0, 2500.0, 3.16, 56_880),
        ("DAI", {"baseFeePerGas": ["0x12A05F200", "0x1DCD65000"],
                 "reward": [["0x9502F900"], ["0x77359400"], ["0xB2D05E00"]]},
         4000.0, 1.0, 18.5, 1_332_000_000),
        ("WBTC", {"baseFeePerGas": ["0x3B9ACA00"], "reward": []},
         3000.0, 90000.0, 2.01, 1_206),
        # A FOURTH vector taken from LIVE mainnet gas, 2026-09-09: this exact eth_feeHistory
        # was fetched on the box and handed to `bridge_fee.estimate_max_gas_price_gwei` /
        # `bridge_fee.relayer_fee` themselves, and these are the numbers THEY answered. The
        # three above are shaped; this one is what the relayer's own code says about the market
        # as it actually was, which is the sample a synthetic vector cannot be.
        ("ETH", LIVE_GAS, 2500.0, 2500.0, 0.153283829, 2_759),
        ("DAI", LIVE_GAS, 2500.0, 1.0, 0.153283829, 6_897_772),
    ],
)
async def test_the_relayer_fee_is_the_relayers_own_arithmetic(
    eth, monkeypatch, rate_id, fee_history, eth_usd, asset_usd, gas_gwei, expect_groth
):
    """⚠️ WE SET THIS NUMBER. Too low and the message sits for days; too high and we hand over
    the difference. So it must be bridge_fee.py's answer to the groth, not an approximation."""
    eth.fee_history = fee_history

    async def prices(force: bool = False) -> dict[str, float]:
        return {"ETH": eth_usd, "DAI": asset_usd, "WBTC": asset_usd}

    monkeypatch.setattr(beam, "usd_prices", prices)
    assert await beam.max_gas_price_gwei(eth) == pytest.approx(gas_gwei)
    groth, detail = await beam.relayer_fee_groth(ASSETS[rate_id], eth)
    assert groth == expect_groth
    assert detail["margin"] == 1.5 and detail["gas_gwei"] == pytest.approx(gas_gwei)


async def test_an_unreadable_fee_history_refuses_rather_than_guessing(eth):
    eth.fee_history = {}
    with pytest.raises(beam.BeamError):
        await beam.max_gas_price_gwei(eth)


# ============================================================== the payout order, end to end


async def test_a_payout_walks_every_status_and_emits_one_event_per_transition(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    d = mock_db["pgasme_test"]
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}

    # ── scheduled → releasing (the intent, then the send)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing" and row["beam_txid"] == "beamtx-1"
    assert row["relayer_fee_groth"] == 3600  # 120_000 × 0.2 gwei / 1e9 × 1.5 ETH
    assert row["eth_from_block"] == eth.head
    args = send_args(beam_wallet)
    assert args == (
        f"role=user,action=send,cid={ETH.beam_cid},amount=500000,receiver={W},relayerFee=3600"
    )

    # ── releasing → bridging
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "bridging"

    # ── bridging: the kernel confirms → the ledger books the release and the msg id is matched
    beam_wallet.local_msgs = {
        6: {"amount": 999, "receiver": OTHER_W, "relayerFee": 1, "height": 1},
        7: {"amount": 500_000, "receiver": W, "relayerFee": 3600, "height": 2},
    }
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "bridging" and row["msg_id"] == 7 and row["beam_confirmations"] == 0
    assert row["kernel"] == "kernel-beamtx-1"
    entry = await ledger.find_entry("release", "req1")
    assert entry and entry["groth"] == 500_000 and entry["d_sent"] == 500_000

    # ── bridging → delivering at 61 Beam confirmations. ⛔ BeamPay FREEZES a contract tx's
    # `confirmations` the moment it books it (159 of 171 live contract rows read 0), so the
    # count that moves is the wallet's own height against the one recorded at kernel time.
    assert beam_wallet.txs["beamtx-1"]["confirmations"] == 0
    beam_pay.height_ += settings.beam_confirmations
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "delivering"

    # ── delivering → sent, on the identity pair in one Ethereum block
    block = eth.head + 500  # the crossing lands AFTER the baseline block the release wrote down
    eth.head += 1000
    eth.credit(block, ETH.pipe, -500_000 * ETH.grid)
    eth.credit(block, W, 500_000 * ETH.grid)
    eth.blocks[block] = {"transactions": [{"to": ETH.pipe, "hash": "0x" + "ee" * 32}]}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "sent" and row["eth_block"] == block
    assert row["eth_tx"] == "0x" + "ee" * 32

    # ── one event per transition, ids only, and a further pass adds nothing
    await payouts.process_once()
    assert await kinds(mock_db) == [
        "payout_releasing",
        "payout_bridging",
        "payout_delivering",
        "payout_sent",
    ]
    for ev in await d.events.find({}).to_list(50):
        assert ev["request_id"] == "req1"
        assert W.lower() not in ev["text"].lower()


async def test_a_failed_beam_kernel_delays_the_payout_and_refunds_nothing(mock_db, eth, armed):
    """⛔ **NEVER FAILED** (T40, admin 2026-09-10). A dead Beam transaction is a dead ATTEMPT,
    not a dead order: nothing crossed, so the money is still owed and stays reserved, and the
    crossing is tried again once that transaction is PROVEN dead (status 4/2 and no kernel)."""
    await make_payout(mock_db)
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    await payouts.process_once()  # scheduled → releasing (+ sent)
    await payouts.process_once()  # → bridging
    w = beam.wallet()
    txid = (await payout(mock_db))["beam_txid"]
    w.txs[txid].update({"status": beam.TX_FAILED, "status_string": "failed", "kernel": None})
    before = (await ledger.balance("acct1", "ETH"))["available"]
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == payouts.DELAYED and "failed" in row["hold_detail"]
    after = (await ledger.balance("acct1", "ETH"))["available"]
    assert after == before  # nothing came back: the order is not over
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "payout_delayed"})
    assert ev is not None and ev["request_id"] == "req1"
    assert await mock_db["pgasme_test"].events.find_one({"kind": "payout_failed"}) is None


async def test_a_lost_response_is_resolved_from_the_chain_and_never_re_signed(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """The idempotency law: `process_invoke_data` takes no txId, so a repeat would be a SECOND
    signature over one inventory. The intent is written first and the answer comes from the
    chain — never from calling again."""
    await make_payout(mock_db)
    beam_wallet.raise_on.add("process_invoke_data")
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing" and "beam_txid" not in row
    assert row["release_call_at"] > 0  # the intent marker that says "this MAY have landed"
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "payout_send_unconfirmed"})
    assert ev is not None and ev["immediate"] is True

    # it HAD landed: the wallet's own history proves it — the transaction moved EXACTLY
    # +(amount + relayerFee) of this asset on this pipe (a SPEND is POSITIVE, see
    # SEND_IS_POSITIVE), AND our outgoing message to this receiver for this amount exists.
    # Neither half alone may adopt anything.
    #
    # …and the walk has to get there THROUGH the rows production actually stores: this history
    # carries ordinary withdrawals, whose `type` is the string "withdrawal".
    beam_pay.withdrawal_row(comment="an ordinary payout, nothing to do with us")
    beam_pay.add_tx(
        txId="landed-1",
        kernel="k1",
        invoke_data=[
            {"contract_id": ETH.beam_cid, "amounts": [{"asset_id": 36, "amount": +503_600}]}
        ],
    )
    beam_pay.withdrawal_row(comment="and another")
    beam_wallet.local_msgs = {4: {"amount": 500_000, "receiver": W, "relayerFee": 3600}}
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["beam_txid"] == "landed-1" and row["resolved_from_chain"] is True
    assert row["msg_id"] == 4  # the crossing, not just the transaction
    # ⛔ exactly ONE irreversible call was ever made across both passes
    assert beam_wallet.methods().count("process_invoke_data") == 1
    # …and the adopted txid is registered with BeamPay, or its whole flow books to __house__
    assert beam_pay.expectations["landed-1"]["address"] == MP


async def test_an_intent_without_a_call_goes_back_to_scheduled(mock_db, eth, armed, beam_wallet):
    """A crash BETWEEN the intent and the RPC signed nothing, and that is provable: the row has
    no `release_call_at`. Only that case may be replanned."""
    await make_payout(mock_db, status="releasing", release_attempt_at=time.time() - 5)
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "scheduled"
    assert "process_invoke_data" not in beam_wallet.methods()


async def test_an_unresolvable_send_is_held_for_a_human_never_retried(mock_db, eth, armed):
    """…and HELD means the row LEAVES the status whose handler resolves things, exactly as
    `bridge_watcher.resolve_unconfirmed_contract` advances to `on_hold`."""
    await make_payout(
        mock_db,
        status="releasing",
        release_attempt_at=time.time() - 3600,
        release_call_at=time.time() - 3600,
    )
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "held" and row["held_from"] == "releasing"
    assert "NOT auto-retried" in row["hold_detail"] and row["unresolved_at"] > 0
    assert "process_invoke_data" not in beam.wallet().methods()


# ============================================================== the kill switch


async def test_the_kill_switch_halts_the_chain_before_the_irreversible_call(
    mock_db, eth, armed, beam_wallet, monkeypatch, tmp_path
):
    """Thrown mid-chain — after the calldata is built, before it is signed — the switch stops
    the send and hands the order back. `process_invoke_data` is never reached."""
    stop = tmp_path / "pgasme.stop"
    monkeypatch.setattr(settings, "stop_file", str(stop))
    await make_payout(mock_db)
    beam_wallet.on_invoke_send = lambda args: stop.write_text("stop")
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "kill switch" in row["hold_reason"]
    assert "invoke_contract" in beam_wallet.methods()  # the build (a read) happened
    assert "process_invoke_data" not in beam_wallet.methods()  # the signature did not


async def test_nothing_is_planned_at_all_while_the_switch_is_set(
    mock_db, eth, armed, beam_wallet, monkeypatch, tmp_path
):
    stop = tmp_path / "pgasme.stop"
    stop.write_text("stop")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    await make_payout(mock_db)
    await make_deposit(mock_db)
    await payouts.process_once()
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "scheduled"
    assert beam_wallet.methods() == []


# ============================================================== delivery identity


async def test_delivery_needs_the_pipe_down_and_W_up_in_the_same_block(mock_db, eth):
    """§IDENTITY-BEATS-BALANCE. A pipe drop of exactly the amount is somebody ELSE's delivery
    unless our wallet rose by it in that same block — amount alone is never the match."""
    amount_wei = 500_000 * ETH.grid
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    decoy, real = 15_000, 18_000
    eth.credit(decoy, ETH.pipe, -amount_wei)  # the same size, to somebody else
    eth.credit(decoy, OTHER_W, amount_wei)
    eth.credit(real, ETH.pipe, -amount_wei)
    eth.credit(real, W, amount_wei)
    found, scanned, _bal = await payouts.find_delivery(
        eth, ETH.pipe, W, amount_wei, 14_000, eth.head, max_steps=200
    )
    assert found is not None
    assert found["block"] == real and found["amount_wei"] == amount_wei
    assert found["proof"] == "pair"  # W up AND the pipe down, in that one block


async def test_a_pipe_drop_with_no_matching_rise_is_not_our_delivery(mock_db, eth):
    amount_wei = 500_000 * ETH.grid
    eth.start = {ETH.pipe.lower(): 5 * 10**18}
    eth.credit(15_000, ETH.pipe, -amount_wei)
    eth.credit(15_000, OTHER_W, amount_wei)
    found, scanned, _bal = await payouts.find_delivery(
        eth, ETH.pipe, W, amount_wei, 14_000, eth.head, max_steps=200
    )
    assert found is None and scanned >= 15_000


async def test_an_unreadable_archive_raises_and_never_checkpoints(mock_db, eth, armed):
    """"No archive" must never read as "no delivery": the scan raises, the row keeps its
    checkpoint, and the operator hears."""
    await make_payout(
        mock_db, status="delivering", beam_txid="beamtx-1", eth_from_block=100, eth_scan_from=100
    )

    async def no_history(url: str, method: str, params: Any) -> Any:
        raise ethpipe.RpcError(f"{url}: no historical state")

    eth.call_on = no_history
    with pytest.raises(ethpipe.RpcError):
        await payouts.find_delivery(eth, ETH.pipe, W, 1, 100, eth.head)
    await payouts.process_once()  # the loop catches it per row and pages
    row = await payout(mock_db)
    assert row["status"] == "delivering" and row["eth_scan_from"] == 100


async def test_the_archive_endpoint_is_probed_explicitly_and_then_pinned(mock_db, eth):
    eth.start = {ETH.pipe.lower(): 10**18}
    await payouts.find_delivery(eth, ETH.pipe, W, 1, 100, 300)
    pinned = [c for c in eth.calls if c[0] == "eth_getBalance"]
    assert pinned and all(prefer == FakeEth.ARCHIVE and pin for _m, _p, prefer, pin in pinned)


# ============================================================== the treasury sub-machine


async def test_a_deposit_is_claimed_then_shielded_with_one_event_per_transition(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    await make_deposit(mock_db)  # 0.12 ETH → 1 × 0.1 + 2 × 0.01
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    beam_pay.fund(TREASURY, 36, 12_000_000)  # the claim credited the treasury address

    await payouts.process_once()  # (none) → claiming
    assert (await deposit(mock_db))["treasury"] == "claiming"

    await payouts.process_once()  # claiming: view_incoming → receive → claim_txid → register
    row = await deposit(mock_db)
    assert row["treasury"] == "claiming" and row["claim_txid"] == "beamtx-1"
    assert beam_wallet.params_for("invoke_contract")[-1]["args"] == (
        f"role=user,action=receive,cid={ETH.beam_cid},msgId=222"
    )
    # ⛔ the txid is BeamPay's the moment the wallet answers it, and it books to the TREASURY
    assert row["attribution"]["txid"] == "beamtx-1"
    assert beam_pay.expectations["beamtx-1"] == {
        "txid": "beamtx-1", "address": TREASURY, "trade_ref": "dep1"
    }

    await payouts.process_once()  # → claimed (kernel confirmed AND booked by BeamPay)
    assert (await deposit(mock_db))["treasury"] == "claimed"

    await payouts.process_once()  # → shielding, with the plan
    row = await deposit(mock_db)
    assert row["treasury"] == "shielding"
    assert row["shield_plan"] == [10_000_000, 1_000_000, 1_000_000]

    for k in range(3):
        await payouts.process_once()  # ONE chunk per pass (§9.3: ≤ 10 outputs per block)
        assert len(beam_pay.withdrawals) == k + 1
    sends = beam_pay.withdrawals
    assert [s["amount"] for s in sends] == [10_000_000, 1_000_000, 1_000_000]
    assert all(s["asset_id"] == 36 and s["from_address"] == TREASURY for s in sends)
    # ⛔ A FRESH MAX-PRIVACY ADDRESS PER CHUNK, AND NEVER THE PRIMARY. Three sends to ONE
    # max-privacy address is what deposit 60b0e57…'s chunks 1 and 2 died of on 2026-09-09
    # ("Shielded outp duplicate ← Kernel Type 3", status 4): the wallet re-uses the address's
    # one-time voucher and rebuilds the identical shielded output, which the chain refuses.
    targets = [s["to_address"] for s in sends]
    assert len(set(targets)) == 3 and MP not in targets
    assert [c["note"] for c in beam_pay.created] == [f"pgasme shielded|dep1|{k}" for k in range(3)]
    assert all(
        c["wallet_type"] == "max_privacy" and c["expiration"] == "never"
        for c in beam_pay.created
    )
    # …and each one is in the registry the float read sums over, before it was sent to
    regs = await mock_db["pgasme_test"].mp_addresses.find({}).sort("k", 1).to_list(10)
    assert [r["address"] for r in regs] == targets
    assert [(r["deposit_id"], r["k"], r["purpose"]) for r in regs] == [
        ("dep1", k, "shield") for k in range(3)
    ]
    assert [r["_id"] for r in regs] == [f"shield|dep1|{k}" for k in range(3)]
    # ⛔ no `fee` field: BeamPay sets the withdrawal fee and ignores ours (INTEGRATION.md §4)
    assert all("fee" not in s for s in sends)
    assert [s["comment"] for s in sends] == [f"shield|dep1|{k}" for k in range(3)]

    await payouts.process_once()  # every chunk found by its comment and settled → shielded
    row = await deposit(mock_db)
    assert row["treasury"] == "shielded" and len(row["shield_txids"]) == 3
    # the fee BeamPay actually charged, read back — never the constant we budgeted with
    assert row["beam_fee_groth"] == 3 * beam_pay.withdraw_fee

    await payouts.process_once()  # terminal: nothing repeats
    assert await kinds(mock_db) == [
        "deposit_claiming",
        "deposit_claimed",
        "deposit_shielding",
        "deposit_shielded",
    ]


async def test_a_shield_retry_finds_its_comment_and_does_not_send_twice(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ `/withdraw` IS NOT IDEMPOTENT and answers no txid. A retry after a lost response
    queues a SECOND withdrawal of the treasury's money — so the chunk records that it is about
    to call BEFORE it calls, and the next pass resolves the lost answer by looking for the
    comment it chose, never by calling again."""
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    beam_pay.fund(TREASURY, 36, 1_000_000)
    for _ in range(4):
        await payouts.process_once()  # → shielding
    assert (await deposit(mock_db))["treasury"] == "shielding"

    # the withdrawal IS accepted; the answer is lost on the way back
    original = beam_pay._withdraw

    def accept_then_lose(b: dict) -> tuple[int, Any]:
        original(b)
        raise beampay.BeamPayError("POST /withdraw: ReadTimeout")

    beam_pay._withdraw = accept_then_lose
    await payouts.process_once()
    row = await deposit(mock_db)
    assert len(beam_pay.withdrawals) == 1  # it was queued
    # …and the row says so, which is what stops a resend — with the address it was called with
    assert row["shield_calls"][0]["at"] > 0
    assert row["shield_calls"][0]["to_address"] == beam_pay.created[-1]["address"]
    assert row.get("shield_txids") in (None, [])

    beam_pay._withdraw = original
    await payouts.process_once()
    row = await deposit(mock_db)
    # ⛔ ONE withdrawal, found by its comment — not a second send
    assert len(beam_pay.withdrawals) == 1
    assert row["shield_txids"] == [beam_pay.tx_rows[-1]["_id"]]
    assert beam_pay.tx_rows[-1]["comment"] == "shield|dep1|0"


async def test_a_refused_withdrawal_is_a_row_and_stays_retryable(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """`{"status": false}` is HTTP 200 with a reason and nothing queued — so the marker that
    stops a resend must be RELEASED, or the chunk is stranded forever on a refusal."""
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)  # this row is driven pass by pass
    await make_deposit(mock_db, value=1_000_000)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 1_000_000}]
    for _ in range(4):
        await payouts.process_once()
    beam_pay.withdraw_ok = False
    await payouts.process_once()
    row = await deposit(mock_db)
    assert "BeamPay refused shield chunk 1/1" in row["hold_reason"]
    assert row["shield_calls"] == [0]  # released: nothing was queued
    beam_pay.withdraw_ok = True
    await payouts.process_once()
    assert len(beam_pay.withdrawals) == 2  # the refusal, then the one that took


async def test_a_claim_waits_while_the_relayer_has_not_delivered(mock_db, eth, armed, beam_wallet):
    await make_deposit(mock_db)
    beam_wallet.incoming = []
    await payouts.process_once()
    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "claiming" and "has not delivered" in row["hold_reason"]
    assert "process_invoke_data" not in beam_wallet.methods()


# ====================== the shield collision (deposit 60b0e57…, 2026-09-09 23:21–23:23Z)
#
# Three max-privacy self-sends of 1,000,000 groth bETH to the SAME PGAS_BEAM_MP_ADDRESS,
# seconds apart. Chunk 0 settled (tx f15dd77d…, shielded output 28390 at height 4030052).
# Chunks 1 and 2 came back status 4, "failed maximum anonymity", with the wallet-api's own
# reason: **"Shielded outp duplicate ← Kernel Type 3"**. A max-privacy address publishes
# ONE-TIME vouchers; the wallet re-used the same one for every send in quick succession and
# rebuilt the identical shielded output, which the chain refuses. Two thirds of the deposit
# stayed unshielded.


def chunk_tx(bp: FakeBeamPay, dep_id: str, k: int) -> dict[str, Any]:
    """The transaction carrying one chunk's comment — the only identity a `/withdraw` has."""
    return next(r for r in bp.tx_rows if r.get("comment") == f"shield|{dep_id}|{k}")


def fail_chunk(bp: FakeBeamPay, dep_id: str, k: int) -> str:
    """One chunk's transaction, as the wallet came back with chunks 1 and 2 that night."""
    row = chunk_tx(bp, dep_id, k)
    row.update(
        status=beam.TX_FAILED,
        status_string="failed maximum anonymity",
        success=False,
        failure_reason="Shielded outp duplicate ← Kernel Type 3",
    )
    return str(row["_id"])


def fail_last_withdrawal(bp: FakeBeamPay) -> dict[str, Any]:
    """The newest emitted withdrawal, same shape."""
    row = bp.tx_rows[-1]
    row.update(
        status=beam.TX_FAILED,
        status_string="failed maximum anonymity",
        success=False,
        failure_reason="Shielded outp duplicate ← Kernel Type 3",
    )
    return row


async def cli_lines(fn: Any, *args: Any, **kw: Any) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await fn(*args, out=lines.append, **kw)
    return code, lines


async def shielding_deposit(mock_db, beam_wallet, beam_pay, value: int = 3_000_000) -> None:
    """A claimed deposit sitting in `shielding` with `value / 1,000,000` chunks planned."""
    await make_deposit(mock_db, value=value)
    beam_wallet.incoming = [{"msg_id": 222, "amount": value}]
    beam_pay.fund(TREASURY, 36, value)
    for _ in range(4):
        await payouts.process_once()
    assert (await deposit(mock_db))["treasury"] == "shielding"


async def test_each_shield_chunk_gets_its_own_fresh_max_privacy_address(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ ONE ADDRESS PER CHUNK, CREATED THROUGH BEAMPAY, REGISTERED BEFORE IT IS SENT TO.

    The registry is not bookkeeping: it is the only thing that makes the float knowable, so the
    address must be in `mp_addresses` BEFORE any value is on its way to it."""
    await shielding_deposit(mock_db, beam_wallet, beam_pay, value=2_000_000)
    for _ in range(2):
        await payouts.process_once()
    a, b = (w["to_address"] for w in beam_pay.withdrawals)
    assert a != b and MP not in (a, b)
    assert beam_pay.address_types[a] == beam_pay.address_types[b] == "max_privacy"
    # created through BeamPay — never the wallet-api — with the chunk in the note
    assert [c["note"] for c in beam_pay.created] == ["pgasme shielded|dep1|0", "pgasme shielded|dep1|1"]
    assert all(c["expiration"] == "never" for c in beam_pay.created)
    assert "create_wallet" not in beam_wallet.methods()
    # …in the registry the float read sums over, keyed by the chunk's own identity
    rows = await mock_db["pgasme_test"].mp_addresses.find({}).sort("k", 1).to_list(10)
    assert [(r["_id"], r["address"], r["purpose"]) for r in rows] == [
        ("shield|dep1|0", a, "shield"),
        ("shield|dep1|1", b, "shield"),
    ]
    # …and on the chunk record, chosen before the call, beside the marker that stops a resend
    assert [c["to_address"] for c in payouts.shield_calls_of(await deposit(mock_db))] == [a, b]
    # the comment — the only identity /withdraw has — is unchanged by any of this
    assert [w["comment"] for w in beam_pay.withdrawals] == ["shield|dep1|0", "shield|dep1|1"]


async def test_the_float_is_the_sum_over_every_registered_max_privacy_address(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """⛔ THE FLOAT IS NOT ONE ADDRESS'S BALANCE ANY MORE. Reading only PGAS_BEAM_MP_ADDRESS
    sees one chunk of a three-chunk shielding, and the payout then starves on value that is
    sitting there, shielded, in the addresses nobody asked about."""
    beam_pay.addresses[MP]["available"]["36"] = 100_000  # the primary holds almost nothing
    spread = []
    for k, groth in enumerate((400_000, 300_000)):
        addr = beam_pay.register(f"MaxPrivacyChunk{k}" + "y" * 50, "max_privacy")
        beam_pay.fund(addr, 36, groth)
        spread.append(addr)
        await mock_db["pgasme_test"].mp_addresses.insert_one(
            {
                "_id": f"shield|depX|{k}",
                "address": addr,
                "created_at": time.time() + k,
                "deposit_id": "depX",
                "k": k,
                "purpose": "shield",
            }
        )
    payouts.reset_process_state()
    assert await payouts.mp_registry() == [MP, *spread]  # the primary FIRST
    assert await payouts.float_groth(beam_pay, ETH) == 800_000

    # and the money-level half: 0.005 needs 510,000 with the relayer fee, which the primary
    # alone (100,000) cannot pay for and the registry can
    await make_payout(mock_db, amount=500_000)
    await payouts.process_once()
    assert (await payout(mock_db))["status"] == "releasing"


async def test_an_unreadable_address_in_the_registry_is_never_a_smaller_float(
    mock_db, eth, armed, beam_wallet, beam_pay
):
    """A partial sum is a number nobody measured. `available_groth` RAISES on an address
    BeamPay cannot report, and the release holds rather than crossing against a float that is
    missing an address."""
    await mock_db["pgasme_test"].mp_addresses.insert_one(
        {
            "_id": "shield|depX|0",
            "address": "MaxPrivacyNotInBeamPaysBook" + "u" * 40,
            "created_at": time.time(),
            "deposit_id": "depX",
            "k": 0,
            "purpose": "shield",
        }
    )
    payouts.reset_process_state()
    with pytest.raises(beampay.BeamPayError):
        await payouts.float_groth(beam_pay, ETH)
    await make_payout(mock_db)
    await payouts.process_once()  # the loop catches it per row and pages; nothing signs
    assert (await payout(mock_db))["status"] == "scheduled"
    assert "process_invoke_data" not in beam_wallet.methods()


async def test_a_failed_chunk_holds_for_a_human_once_and_names_the_cli(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """⛔ A DEAD TRANSACTION IS NOT A LANDING, AND IT IS NOT AUTO-RETRIED.

    `/withdraw` has no idempotency key, so a resend nobody looked at first is the double send
    the whole design refuses to make possible. The hold is TERMINAL (the row leaves
    `shielding`), so this is decided ONCE rather than re-decided every six hours — and the row
    says the exact command that resolves it."""
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    await shielding_deposit(mock_db, beam_wallet, beam_pay)  # three 0.01 chunks
    for _ in range(3):
        await payouts.process_once()  # all three dispatched, as the live row was
    assert len(beam_pay.withdrawals) == 3
    # chunk 0 settles; 1 and 2 come back "failed maximum anonymity"
    dead = [fail_chunk(beam_pay, "dep1", 1), fail_chunk(beam_pay, "dep1", 2)]

    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == payouts.HELD and row["held_from"] == "shielding"
    assert "a shield chunk failed on Beam" in row["hold_reason"]
    assert "python -m pgasme.beam replan-shield --deposit dep1" in row["hold_reason"]
    assert "--apply" in row["hold_reason"]
    assert row["shield_txids"] == [chunk_tx(beam_pay, "dep1", 0)["_id"]]  # only the LIVE one
    assert "deposit_shield_failed" in await kinds(mock_db)
    # ⛔ and it does not re-decide: a held row is out of the handler's reach entirely
    holds = row.get("holds")
    for _ in range(3):
        await payouts.process_once()
    again = await deposit(mock_db)
    assert again["treasury"] == payouts.HELD and again.get("holds") == holds
    assert len(beam_pay.withdrawals) == 3  # no fourth send into a broken plan
    assert dead  # the two that died are what the re-plan below writes off


async def test_replan_shield_dry_run_reads_beampay_and_writes_nothing(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    await shielding_deposit(mock_db, beam_wallet, beam_pay)
    for _ in range(3):
        await payouts.process_once()
    fail_chunk(beam_pay, "dep1", 1)
    fail_chunk(beam_pay, "dep1", 2)
    await payouts.process_once()
    before = await deposit(mock_db)

    code, lines = await cli_lines(beam.cmd_replan_shield, "dep1")
    text = "\n".join(lines)
    assert code == 0
    assert "DRY RUN" in text and "held from shielding" in text
    assert "settled" in text and "failed" in text
    assert "failed maximum anonymity" in text
    assert "settled, KEPT : chunk(s) 0" in text
    assert "would re-plan : chunk(s) 1, 2" in text
    assert "DRY RUN — nothing was written" in text
    assert await deposit(mock_db) == before  # a look is a look
    assert beam_pay.withdrawals and len(beam_pay.withdrawals) == 3


async def test_replan_shield_apply_resends_the_failed_chunks_to_fresh_addresses(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """The repair, end to end: the settled chunk and its txid are kept, the two dead ones are
    written off ON the row, the hold clears, and the processor re-sends them — each to its own
    fresh max-privacy address, under the SAME comment, with no double-send page."""
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    await shielding_deposit(mock_db, beam_wallet, beam_pay)
    for _ in range(3):
        await payouts.process_once()
    dead = [fail_chunk(beam_pay, "dep1", 1), fail_chunk(beam_pay, "dep1", 2)]
    await payouts.process_once()
    settled_txid = chunk_tx(beam_pay, "dep1", 0)["_id"]
    first_targets = [w["to_address"] for w in beam_pay.withdrawals]

    code, lines = await cli_lines(beam.cmd_replan_shield, "dep1", apply=True)
    assert code == 0 and "APPLIED" in "\n".join(lines)
    row = await deposit(mock_db)
    assert row["treasury"] == "shielding" and "hold_reason" not in row and "held_from" not in row
    calls = payouts.shield_calls_of(row)
    assert calls[0]["at"] > 0 and calls[1]["at"] == 0 and calls[2]["at"] == 0  # 0 is untouched
    assert sorted(row["shield_writeoffs"]) == sorted(dead)
    assert [r["chunks"] for r in row["shield_replans"]] == [[1, 2]]
    assert row["shield_txids"] == [settled_txid]  # the settled chunk's evidence is kept
    assert "deposit_shield_replanned" in await kinds(mock_db)
    # ⛔ the floor a history walk searches back to must not have moved forward, or chunk 0's
    # transaction becomes invisible and the machine sends it again
    assert row["shield_since"] <= row["treasury_at"]

    for _ in range(2):
        await payouts.process_once()
    assert len(beam_pay.withdrawals) == 5
    resent = beam_pay.withdrawals[3:]
    assert [w["comment"] for w in resent] == ["shield|dep1|1", "shield|dep1|2"]
    # ⛔ NEW ADDRESSES, NOT THE ONES THAT FAILED. The failed send already published that
    # address's one-time voucher; re-sending to it reproduces the exact duplicate the chain
    # refused. The attempt count comes from BeamPay's history — how many dead transactions
    # carry this chunk's comment — never from a counter of ours.
    assert not {w["to_address"] for w in resent} & set(first_targets)
    regs = await mock_db["pgasme_test"].mp_addresses.find({}).sort("_id", 1).to_list(20)
    assert [r["_id"] for r in regs] == [
        "shield|dep1|0", "shield|dep1|1", "shield|dep1|1#1", "shield|dep1|2", "shield|dep1|2#1"
    ]
    assert {r["address"] for r in regs} == set(first_targets) | {w["to_address"] for w in resent}
    # a dead transaction sharing a comment with a live one is NOT a double send
    assert "deposit_shield_duplicate" not in await kinds(mock_db)

    await payouts.process_once()
    row = await deposit(mock_db)
    assert row["treasury"] == "shielded" and len(row["shield_txids"]) == 3


async def test_replan_shield_refuses_while_a_chunk_could_still_land(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch
):
    """⛔ NEVER RACE THE WALLET. A pending transaction may still settle and a `/withdraw`
    BeamPay has accepted may still be emitted: re-planning either queues a SECOND send of one
    chunk, which is exactly what this route cannot dedupe."""
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)
    await shielding_deposit(mock_db, beam_wallet, beam_pay)
    for _ in range(3):
        await payouts.process_once()
    fail_chunk(beam_pay, "dep1", 1)
    # chunk 2 is still in flight: the kernel has not registered yet
    chunk_tx(beam_pay, "dep1", 2).update(status=beam.TX_IN_PROGRESS, status_string="in progress")
    code, lines = await cli_lines(beam.cmd_replan_shield, "dep1", apply=True)
    text = "\n".join(lines)
    assert code == 1 and "REFUSING" in text and "chunk 3 is pending" in text
    assert (await deposit(mock_db))["treasury"] == "shielding"
    assert len(beam_pay.withdrawals) == 3  # nothing was re-sent

    # the other racy shape: /withdraw was called and no transaction has appeared at all
    chunk_tx(beam_pay, "dep1", 2).update(comment="")  # chunk 2's transaction is not visible
    code, lines = await cli_lines(beam.cmd_replan_shield, "dep1", apply=True)
    assert code == 1 and "chunk 3 is unknown" in "\n".join(lines)


async def test_replan_shield_refuses_when_nothing_failed_and_when_the_switch_is_set(
    mock_db, eth, armed, beam_wallet, beam_pay, monkeypatch, tmp_path
):
    await shielding_deposit(mock_db, beam_wallet, beam_pay, value=1_000_000)
    await payouts.process_once()  # chunk 0 sent and settled
    code, lines = await cli_lines(beam.cmd_replan_shield, "dep1", apply=True)
    assert code == 1 and "nothing here to re-plan" in "\n".join(lines)

    fail_last_withdrawal(beam_pay)
    stop = tmp_path / "pgasme.stop"
    stop.write_text("stop")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    code, lines = await cli_lines(beam.cmd_replan_shield, "dep1", apply=True)
    assert code == 1 and "kill switch is set" in "\n".join(lines)
    assert (await deposit(mock_db)).get("shield_writeoffs") is None

    code, _lines = await cli_lines(beam.cmd_replan_shield, "no-such-deposit")
    assert code == 1


async def test_the_replan_command_parses_its_arguments(mock_db, beam_pay, capsys):
    assert await beam.cli_main(["replan-shield"]) == 2
    assert await beam.cli_main(["replan-shield", "--deposit"]) == 2
    assert await beam.cli_main(["replan-shield", "--deposit", "nope"]) == 1
    assert "replan-shield --deposit <id>" in beam.USAGE


def test_the_shield_plan_is_denominations_largest_first_then_the_remainder():
    assert payouts.shield_plan(12_340_000, [10_000_000, 1_000_000]) == [
        10_000_000,
        1_000_000,
        1_000_000,
        340_000,
    ]
    assert payouts.shield_plan(0, [1_000_000]) == []


# ============================================================== the flags


async def test_with_the_flags_off_nothing_signs_and_the_orders_say_why(
    mock_db, eth, beam_wallet, monkeypatch
):
    """Dark by construction: no create_tx:true, no process_invoke_data, no tx_send — and the
    order carries the reason so the wait is not a mystery."""
    assert settings.payout_direct_enabled is False
    assert settings.claim_enabled is False and settings.shield_enabled is False
    await make_payout(mock_db)
    await make_deposit(mock_db)
    for _ in range(3):
        await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and row["dark"] is True
    assert "PGAS_PAYOUT_DIRECT_ENABLED=0" in row["hold_detail"]
    dep = await deposit(mock_db)
    assert dep["treasury"] == "claiming" and dep["dark"] is True
    assert "PGAS_CLAIM_ENABLED=0" in dep["hold_reason"]
    assert beam_wallet.methods() == []
    assert all(p.get("create_tx") is not True for p in beam_wallet.params_for("invoke_contract"))


async def test_a_dark_order_is_not_stuck(mock_db, eth, monkeypatch):
    """A wait a FLAG causes on purpose must never page as stuck — alerts that cry wolf get
    ignored when they matter."""
    sent: list[str] = []

    async def fake_send(text, *, key=None, cooldown_s=0.0):
        sent.append(text)
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    await make_payout(mock_db, release_at=time.time() - 4 * 3600)
    await payouts.process_once()
    await workers.stuck_checks()
    assert not any("STUCK" in t for t in sent)
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "req1"}, {"$unset": {"dark": ""}}
    )
    sent.clear()
    await workers.stuck_checks()
    assert any(t.startswith("STUCK: payout due") for t in sent)


async def test_the_any_asset_branch_refuses(mock_db, eth, monkeypatch):
    """The DARK statuses ARE the any-asset branch (bETH → our distributor, then a cross-chain
    swap to the user's own chain and asset): designed, and refused.

    ⚠️ `mode="instant"` stopped being one of them on 2026-09-10 (T34). It is the Ethereum-side
    payout now, with a branch of its own — so it refuses for its own reason, and this test pins
    that the two refusals are DIFFERENT rather than letting one quietly stand in for the other."""
    monkeypatch.setattr(settings, "payout_instant_enabled", True)
    await make_payout(mock_db, mode="instant")
    await make_payout(mock_db, rid="req2", status="waiting_for_dep_eth")
    await payouts.process_once()
    instant = await payout(mock_db, "req1")
    assert "PGAS_DISTRIBUTOR_KEY_FILE" in instant["hold_detail"] and instant["dark"] is True
    assert instant["status"] == "scheduled"
    anyasset = await payout(mock_db, "req2")
    assert "not implemented" in anyasset["hold_detail"] and anyasset["dark"] is True
    assert anyasset["status"] == "waiting_for_dep_eth"


async def test_a_relayer_that_wants_more_than_a_tenth_is_refused(mock_db, eth, armed):
    await make_payout(mock_db, amount=20_000)  # 0.0002 ETH against a 3600-groth fee
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "max 10%" in row["hold_detail"]


async def test_a_short_float_waits_and_says_so(mock_db, eth, armed, beam_wallet, beam_pay):
    """The float is a sum of BeamPay ADDRESS balances — never a wallet total — and since
    2026-09-10 it names both halves so an operator can see which one is short."""
    beam_pay.addresses[MP]["available"]["36"] = 100
    beam_pay.addresses[TREASURY]["available"]["36"] = 0
    await make_payout(mock_db)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "the float holds" in row["hold_detail"]
    assert "shielded across" in row["hold_detail"] and "unshielded at the treasury" in row["hold_detail"]
    assert "invoke_contract" not in beam_wallet.methods()
    assert ("GET", "/balances", {"address": MP}) in beam_pay.calls


async def test_the_float_cannot_be_read_without_a_max_privacy_address(
    mock_db, eth, armed, beam_wallet, monkeypatch
):
    """An address we cannot name is a float we cannot read, and a release must never spend
    against a number nobody measured.

    ⚠️ 2026-09-10: that is the law for the SHIELDED half only. With
    `PGAS_PAYOUT_SPEND_UNSHIELDED=1` the treasury's own balance is float too, and it is
    readable without any max-privacy address — so a missing one is no longer fatal, it simply
    means the shielded half is zero. With the flag OFF the original refusal stands."""
    monkeypatch.setattr(settings, "beam_mp_address", "")
    monkeypatch.setattr(settings, "payout_spend_unshielded", False)
    await make_payout(mock_db)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "shielded float cannot be read" in row["hold_detail"]
    assert "process_invoke_data" not in beam_wallet.methods()

    # …and with the flag on, the unshielded treasury funds it and the release goes
    monkeypatch.setattr(settings, "payout_spend_unshielded", True)
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)  # the row just held; read it again now
    beam_pay = beampay.beampay()
    beam_pay.fund(TREASURY, ETH.aid, 1_000_000)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "releasing" and row["source"] == "regular"


# ============================================================== the calldata assertions


async def test_a_send_whose_calldata_lacks_the_receiver_is_refused(
    mock_db, eth, armed, beam_wallet
):
    """A wrong receiver is unrecoverable, so it must be VISIBLE in the bytes we are about to
    sign — not merely in the string we composed."""
    beam_wallet.omit_receiver = True
    await make_payout(mock_db)
    await payouts.process_once()
    row = await payout(mock_db)
    assert row["status"] == "scheduled" and "RECEIVER is NOT in the calldata" in row["hold_reason"]
    assert "process_invoke_data" not in beam_wallet.methods()


async def test_a_claim_whose_calldata_lacks_the_cid_is_refused(mock_db, eth, armed, beam_wallet):
    beam_wallet.omit_cid = True
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    await make_deposit(mock_db)
    await payouts.process_once()
    await payouts.process_once()
    row = await deposit(mock_db)
    assert "CID is NOT in the calldata" in row["hold_reason"]
    # the build is a create_tx:false READ, so nothing was signed and no "may have landed" marker
    # was written — the order is a wait, not an unknown
    assert "claim_call_at" not in row
    assert "process_invoke_data" not in beam_wallet.methods()
    assert await mock_db["pgasme_test"].events.count_documents({"kind": "deposit_claiming"}) == 1


async def test_view_incoming_reads_the_capital_MsgId_key(beam_wallet):
    beam_wallet.incoming = [{"msg_id": 23, "amount": 313_445}]
    assert await beam_wallet.view_incoming(ETH.beam_cid) == [{"msg_id": 23, "amount": 313445}]


async def test_an_unreadable_view_incoming_raises_rather_than_answering_empty(beam_wallet):
    beam_wallet.raise_on.add("invoke_contract")
    with pytest.raises(beam.BeamError):
        await beam_wallet.view_incoming(ETH.beam_cid)


async def test_find_local_msg_matches_on_identity_not_on_the_count(beam_wallet):
    beam_wallet.local_msgs = {
        9: {"amount": 500_000, "receiver": OTHER_W},  # the same amount, another wallet
        10: {"amount": 500_000, "receiver": W},
    }
    assert await beam_wallet.find_local_msg(ETH.beam_cid, W, 500_000) == 10
    assert await beam_wallet.find_local_msg(ETH.beam_cid, W, 1) is None


# ============================================================== the read-only CLI


async def out_lines(fn, *a, **kw) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await fn(*a, out=lines.append, **kw)
    return code, lines


async def test_the_dry_run_prints_the_exact_calls_for_a_payout_and_sends_nothing(
    mock_db, eth, beam_wallet, capsys
):
    await make_payout(mock_db)
    code = await beam.cli_main(["dry-run", "--payout", "req1"])
    text = capsys.readouterr().out
    assert code == 0
    assert (
        f"role=user,action=send,cid={ETH.beam_cid},amount=500000,receiver={W},relayerFee=3600"
        in text
    )
    assert '"create_tx": false' in text
    assert "calldata verified: receiver ✅  cid ✅" in text
    assert "PGAS_PAYOUT_DIRECT_ENABLED=0" in text
    # ⛔ a dry run that creates a real order is failure mode §7.9 #12
    assert "process_invoke_data" not in beam_wallet.methods()
    assert all(p.get("create_tx") is False for p in beam_wallet.params_for("invoke_contract"))


async def test_the_dry_run_prints_the_exact_calls_for_a_claim_and_sends_nothing(
    mock_db, eth, beam_wallet, capsys
):
    await make_deposit(mock_db)
    beam_wallet.incoming = [{"msg_id": 222, "amount": 12_000_000}]
    code = await beam.cli_main(["dry-run", "--claim", "dep1"])
    text = capsys.readouterr().out
    assert code == 0
    assert f"role=user,action=receive,cid={ETH.beam_cid},msgId=222" in text
    assert "view_incoming: 1 claimable · ours PRESENT" in text
    assert "PGAS_CLAIM_ENABLED=0" in text
    assert "process_invoke_data" not in beam_wallet.methods()


async def test_the_status_command_reads_and_never_writes(
    mock_db, eth, beam_wallet, capsys, monkeypatch
):
    async def height() -> tuple[int, str]:  # the suite is offline by construction
        return 4_030_002, ""

    monkeypatch.setattr(beam, "_explorer_height", height)
    await make_payout(mock_db, status="releasing", release_attempt_at=time.time())
    code = await beam.cli_main(["status"])
    text = capsys.readouterr().out
    assert code == 0
    assert "wallet 4030000 · node/explorer 4030002 (lag 2)" in text
    assert "pending intents: 1 payout · 0 treasury" in text
    assert "ETH" in text and "aid" in text
    # the CLI reads BeamPay for every number except the COIN COUNTS, which only the wallet can
    # answer (`get_utxo` — a read that moves nothing and that BeamPay has no route for)
    assert set(beam_wallet.methods()) <= {"get_utxo"}


async def test_the_cli_refuses_an_unknown_id(mock_db, eth, capsys):
    assert await beam.cli_main(["dry-run", "--payout", "nope"]) == 1
    assert await beam.cli_main(["dry-run"]) == 2
    assert "⛔ no payout request" in capsys.readouterr().out


async def test_fund_is_used_by_the_account_helpers(mock_db):
    """conftest.fund stays exercised here so the shared fixture keeps its meaning."""
    await fund({"account_id": "acct9"}, "ETH", 5)
    assert (await ledger.balance("acct9", "ETH"))["available"] == 5
