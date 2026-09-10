"""T36 — the treasury's COINS: the target, the split that reaches it, and every refusal.

2026-09-10, read off the box: the wallet holds **two** spendable BEAM coins, 0.01 and 9.582,
and exactly one of them is big enough to pay for a call. T40b's F12 then made a regular crossing
budget TWO BEAM coins — BeamPay's fee on the funding transfer, then the wallet's own on the pipe
invocation — so every direct payout HOLDS on "no free coin" until the BEAM is split.

The method was RESOLVED BY EVIDENCE, not by the brief. `/opt/pgasme/beampay/api.py:429` queues
every `/withdraw` into `pending_withdrawals` whether or not the receiver is one of BeamPay's own
addresses, and `process_payments.py:1733` sends every queued row with `beam_api.tx_send(...)`
unconditionally — an "internal transfer" settles with a KERNEL (`process_payments.py:692`).
The wallet's own UTXO table shows it: tx `7b5a42bc24…` spent one 9.879 BEAM coin and created
9.868 `norm` + 0.01 `chng` for a fee of exactly 100,000 groth. **A self-transfer is a real
transaction and it mints a coin of exactly the amount sent.**

The laws this file pins:

  * The plan is arithmetic on a GRID, ascending by one step, and it creates no dust: the
    remainder stays as change on the last transaction.
  * `/withdraw` is not idempotent and answers no txid, so a leg is looked up BY ITS COMMENT
    before it is ever sent again — and a leg that was queued and cannot be found is a human's.
  * `--apply` refuses while anything else is holding an input, while the wallet is out of sync,
    while the kill switch is set, and when the plan re-derived is not the plan that was printed.
  * The ledger comes out EXACTLY: the treasury's BEAM ends at before − Σ fees, its asset ends
    unchanged, and the split address ends at zero — measured, never asserted.
  * A count that could not be read is `None`, never 0, everywhere it is shown.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from test_beam_payout import GROTH, MP, TREASURY, FakeBeamPay, FakeWalletApi

from pgasme import beam, beampay, payouts, utxo, workers
from pgasme.config import settings

ETH_AID = 36
FEE = utxo.SPLIT_FEE_GROTH  # BeamPay's own FEE_REGULAR, 100,000 groth
BOX_BEAM = 958_200_000  # the box's big BEAM coin, 2026-09-10
BOX_SMALL = 1_000_000  # …and the 0.01 that cannot pay for anything
BOX_BETH = 175_681  # the treasury's spendable bETH change coin


# ═══════════════════════════════════════════════════════ the wallet, modelled as Beam behaves


class SplitWallet(FakeWalletApi):
    """A wallet whose COINS move the way the box's do.

    Beam picks the inputs, not us, and the selection this wallet's own history is consistent
    with is *the smallest single coin that covers the amount*. Every transfer therefore:

      * spends the smallest coin ≥ what it needs (and RAISES when there is none — which is the
        `Not enough inputs to process the transaction` two live releases met on 2026-09-10),
      * creates ONE output of exactly the amount sent (this is what mints a coin), and
      * creates the change, if there is any.

    A fake that answered a fixed coin list could not tell a plan that works from a plan that
    spends the coins it is making, which is the single thing this command has to get right."""

    def __init__(self, bp: FakeBeamPay | None = None) -> None:
        super().__init__(bp)
        self.pool: dict[int, list[int]] = {}
        self.starved = False  # answer "not enough inputs" for the next spend

    def seed(self, aid: int, amounts: list[int]) -> None:
        self.pool[int(aid)] = [int(a) for a in amounts]

    def _take(self, aid: int, need: int) -> int:
        coins = self.pool.setdefault(int(aid), [])
        fits = sorted(c for c in coins if c >= need)
        if not fits:
            raise AssertionError(
                f"not enough inputs: no single coin of asset {aid} covers {need} in {coins}"
            )
        coins.remove(fits[0])
        return fits[0]

    def apply(self, aid: int, amount: int, fee: int) -> None:
        """One settled transfer, as the wallet's UTXO table would show it afterwards."""
        if int(aid) == 0:
            spent = self._take(0, amount + fee)
            self.pool[0].append(amount)  # the receiver's output — a NEW coin
            if spent - amount - fee:
                self.pool[0].append(spent - amount - fee)
            return
        spent = self._take(int(aid), amount)
        self.pool[int(aid)].append(amount)
        if spent - amount:
            self.pool[int(aid)].append(spent - amount)
        paid = self._take(0, fee)  # the BEAM fee comes out of a BEAM coin of its own
        if paid - fee:
            self.pool[0].append(paid - fee)

    def _m_get_utxo(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        rows = [
            {
                "amount": amount, "asset_id": aid, "type": "norm",
                "status": 1, "status_string": "available",
                "id": f"{aid}-{i}", "maturity": self.height,
                "createTxId": "", "spentTxId": "",
            }
            for aid, amounts in sorted(self.pool.items())
            for i, amount in enumerate(amounts)
        ]
        skip, count = int(p.get("skip") or 0), int(p.get("count") or len(rows) or 1)
        return rows[skip : skip + count]


class SplitBeamPay(FakeBeamPay):
    """FakeBeamPay, plus the wallet-side consequence of a landed `/withdraw`, plus BeamPay
    patch #10 — the self-tx expectation and the processor branch that consumes it.

    ⛔ **PATCHED BY DEFAULT, BECAUSE THAT IS WHAT THIS COMMAND IS SHIPPED AGAINST.** `--apply`
    refuses on a deployment that cannot book a split's kernel fee, so a fake that defaulted to
    unpatched would make every apply-test a refusal-test. `self_tx_patched = False` is the box
    BEFORE the patch, and it is exercised explicitly in §7.5."""

    def __init__(self) -> None:
        super().__init__()
        self.wallet: SplitWallet | None = None
        self.withdraw_fee = FEE  # a REGULAR → REGULAR transfer, api.py:311
        self.adjustments: list[dict[str, Any]] = []
        self.adjust_ids: set[str] = set()  # idempotent on adjust_id, as the route is
        # ── BeamPay patch #10 ────────────────────────────────────────────────────────────
        self.self_tx_patched = True            # does this deployment have /internal/*self_tx?
        self.self_tx_kinds = ["utxo_split"]    # what it will book
        self.self_tx_rows: dict[str, dict[str, Any]] = {}   # db.self_tx_expectations
        self.self_tx_refusal: str | None = None             # a terminal refusal string
        # what the PROCESSOR does with a registration when the transaction settles:
        # "consumed" books the fee, "abandoned" refuses it with a reason, "pending" models a
        # processor that simply has not got there yet (the bounded wait, then the timeout).
        self.self_tx_outcome = "consumed"
        # ⛔ ORDER MATTERS AND ONLY A SHARED LOG CAN SHOW IT: the registration must be POSTed
        # BEFORE the wallet is asked for the transaction, and two separate call lists can never
        # prove that. Both sides append here.
        self.events: list[str] = []

    def book_self_tx(self, txid: str, fee: int) -> dict[str, Any] | None:
        """BeamPay's patch-#10 processor branch, modelled: ONLY the kernel fee, to the address
        the txid was registered to. The split's VALUE never moves — it is the wallet's own coin
        either way — which is exactly why the ledger falls by the fee and nothing else."""
        doc = self.self_tx_rows.get(txid)
        if doc is None or doc.get("status") != "pending":
            return None
        row = self.tx_index.get(txid)
        if row is not None and row.get("attributed_to"):
            return None                     # one movement, one booking
        if self.self_tx_outcome == "pending":
            return None
        if self.self_tx_outcome == "abandoned":
            doc.update(status="abandoned",
                       error="sender and receiver are not the same address")
            return doc
        addr = str(doc["address"])
        bal = self.addresses.setdefault(addr, {"available": {}, "locked": {}})
        bal["available"]["0"] = bal["available"].get("0", 0) - int(fee)
        doc.update(status="consumed", booked={"0": str(-int(fee))},
                   fee_groth=str(int(fee)), consumed_at=self.now())
        if row is not None:
            row["attributed_to"] = addr
            row["attribution_source"] = "self_tx_expectation"
        return doc

    def _self_tx_route(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        """The three routes patch #10 adds. An UNPATCHED deployment answers FastAPI's routing
        miss for all of them, and that exact string is what the preflight reads."""
        if not self.self_tx_patched:
            return 404, {"detail": "Not Found"}
        if path == "/internal/expect_self_tx":
            if method == "GET":
                pending = sum(1 for d in self.self_tx_rows.values()
                              if d.get("status") == "pending")
                return 200, {"available": True, "ttl_sec": 86400, "pending": pending,
                             "kinds": list(self.self_tx_kinds)}
            self.events.append("expect_self_tx")
            if self.self_tx_refusal:
                return 409, {"detail": self.self_tx_refusal}
            b = dict(body or {})
            if b.get("address") not in self.addresses:
                return 404, {"detail": "address_not_found"}
            if b.get("kind") not in self.self_tx_kinds:
                return 422, {"detail": "unknown self-tx kind"}
            row = self.tx_index.get(str(b.get("txid")))
            if row is not None and row.get("attributed_to"):
                # ⛔ NOT "already seen" — "already OWNED". A self-tx fee that was never booked is
                # booked late as readily as early, because booking it is the first move either
                # way; a flow that already has an owner would be moved twice.
                return 409, {"detail": "tx_already_attributed"}
            replayed = str(b["txid"]) in self.self_tx_rows
            if not replayed:
                self.self_tx_rows[str(b["txid"])] = {
                    **b, "status": "pending", "booked": {}, "fee_groth": None,
                    "error": None, "consumed_at": None, "created_at": self.now(),
                    "expires_at": self.now() + 86400,
                }
            if row is not None and int(row.get("status", 0) or 0) == beam.TX_COMPLETED:
                # the LATE path: `sweep_self_tx_expectations` runs on the processor's 3-second
                # loop, so a registration for a transaction that has already settled is booked
                # essentially at once. Modelled here rather than in a loop the fake does not have.
                self.book_self_tx(str(b["txid"]), int(row.get("fee", 0) or 0))
            return 200, {"status": True, **b, "replayed": replayed, "ttl_sec": 86400}
        doc = self.self_tx_rows.get(path.rsplit("/", 1)[-1])
        if doc is None:
            return 404, {"detail": "expectation_not_found"}
        return 200, {
            "txid": doc.get("txid"),
            "status": doc.get("status"),
            "kind": doc.get("kind"),
            "address": doc.get("address"),
            "trade_ref": doc.get("trade_ref"),
            "asset_id": doc.get("asset_id"),
            "expected_fee_groth": doc.get("expected_fee_groth"),
            "fee_groth": doc.get("fee_groth"),
            "booked": doc.get("booked") or {},
            "negative_after": [],
            "error": doc.get("error"),
            "created_at": doc.get("created_at"),
            "expires_at": doc.get("expires_at"),
            "consumed_at": doc.get("consumed_at"),
        }

    def _withdraw(self, b: dict[str, Any]) -> tuple[int, Any]:
        status, res = super()._withdraw(b)
        if isinstance(res, dict) and res.get("status") is True and self.withdraw_lands:
            if self.wallet is not None:
                self.wallet.apply(int(b["asset_id"]), int(b["amount"]), self.withdraw_fee)
        return status, res

    def _route(self, method: str, path: str, params: Any, body: Any) -> tuple[int, Any]:
        """…plus `POST /internal/ledger/adjust`, with the gate that decides T36b.

        ⛔ **THE FIRST CHECK IS THE ONE THAT MATTERS HERE.** api.py:694 reads

            is_contract = gate_doc.get("type") == 12 or gate_doc.get("type_string") == "contract"
            if not is_contract or gate_doc.get("status") != 3 or gate_doc.get("success") is not True:
                raise HTTPException(status_code=409, detail="tx_not_booked")

        so a `tx_split` — TxType::Simple, type 0 — can never be the gate tx of a fee repair. A
        fake without that line would let the whole design pass on a route the box refuses."""
        if path == "/internal/ledger/adjust" and method == "POST":
            b = dict(body or {})
            if str(b.get("adjust_id")) in self.adjust_ids:
                return 200, {"status": True, "replayed": True, "adjust_id": b.get("adjust_id")}
            gate = self.tx_index.get(str(b.get("after_tx") or ""))
            if not gate:
                return 409, {"detail": "tx_not_booked"}
            if gate.get("type") != 12 and gate.get("type_string") != "contract":
                return 409, {"detail": "tx_not_booked"}
            if gate.get("status") != beam.TX_COMPLETED or gate.get("success") is not True:
                return 409, {"detail": "tx_not_booked"}
            if "__house__" not in (b.get("from_address"), b.get("to_address")):
                return 400, {"detail": "house_leg_required"}
            flow = beam.house_flow(
                {"invoke_data": gate.get("invoke_data"), "fee": gate.get("fee")},
                int(b.get("asset_id") or 0),
            )
            if not flow:
                return 409, {"detail": "no_flow_for_asset"}
            if flow > 0 and b.get("to_address") != "__house__":
                return 409, {"detail": "wrong_direction"}
            if int(b.get("amount_groth") or 0) > abs(flow):
                return 409, {"detail": "amount_exceeds_tx_flow"}
            self.adjust_ids.add(str(b.get("adjust_id")))
            self.adjustments.append(b)
            return 200, {"status": True, "replayed": False, "adjust_id": b.get("adjust_id")}
        if path == "/internal/expect_self_tx" or path.startswith("/internal/self_tx/"):
            return self._self_tx_route(method, path, body)
        return super()._route(method, path, params, body)


@pytest.fixture(autouse=True)
def bp(monkeypatch: pytest.MonkeyPatch) -> SplitBeamPay:
    c = SplitBeamPay()
    beampay.reset_health()
    c.register(TREASURY, "regular")
    c.register(MP, "max_privacy")
    c.fund(TREASURY, 0, BOX_BEAM + BOX_SMALL)
    beampay.set_beampay(c)
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    utxo.reset_health_cache()
    yield c
    beampay.set_beampay(None)
    utxo.reset_health_cache()


@pytest.fixture(autouse=True)
def wallet_api(bp: SplitBeamPay, monkeypatch: pytest.MonkeyPatch) -> SplitWallet:
    w = SplitWallet(bp)
    w.seed(0, [BOX_BEAM, BOX_SMALL])
    bp.wallet = w
    beam.set_wallet(w)
    monkeypatch.setattr(settings, "beam_shader", w.shader)
    yield w
    beam.set_wallet(None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this suite may wait on a clock: the fake lands every withdrawal at once."""
    monkeypatch.setattr(utxo, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(utxo, "POLL_TIMEOUT_S", 0.0)
    # …and the wait for BeamPay to BOOK the fee. Both loops read first and check the deadline
    # afterwards, so a zero timeout still sees an answer that is already there and gives up at
    # once on one that is not — which is exactly the two cases §7.8 needs.
    monkeypatch.setattr(utxo, "BOOK_INTERVAL_S", 0.0)
    monkeypatch.setattr(utxo, "BOOK_TIMEOUT_S", 0.0)


@pytest.fixture
def lines() -> list[str]:
    return []


def out_of(lines: list[str]) -> Any:
    def out(text: Any = "") -> None:
        lines.append(str(text))

    return out


def said(lines: list[str], needle: str) -> bool:
    return any(needle in line for line in lines)


# ⚠️ EVERYTHING FROM HERE TO §7 EXERCISES `--method beampay`, THE FALLBACK. T36b made the
# wallet's own `tx_split` the default (§7): one transaction, one fee, no /withdraw and none of
# BeamPay's three notifications per leg. These tests name the method explicitly rather than
# relying on a default, because that is exactly the mistake that would let a demoted method go
# on being the one everything is proved against.


# ═════════════════════════════════════════════════════════════ 1 · the plan, on the grid


async def test_the_beam_plan_is_ascending_coins_of_the_fee_budget(bp):
    """20 fee coins, the smallest of them exactly what a call costs, ascending by one step.

    ⛔ THE SIZE OF A FEE COIN IS DATA. `fee_budget("send")` is the number `coin_capacity`
    groups against, so a coin minted below it would not be counted a fee coin by the very gate
    this command exists to unblock."""
    plan = await utxo.plan_split("BEAM", method="beampay")
    assert plan.coins == settings.beam_fee_coins == 20
    assert plan.base_groth == payouts.fee_floor("send") == 15_000_000
    assert plan.step_groth == utxo.GRID_GROTH["BEAM"]
    assert plan.sizes == tuple(15_000_000 + i * 100_000 for i in range(20))
    # every size is on the grid, and every one of them can pay for a call
    assert all(s % plan.step_groth == 0 for s in plan.sizes)
    assert min(plan.sizes) >= plan.fee_budget_groth


async def test_a_beam_chunk_is_a_round_trip_that_leaves_the_split_address_at_zero(bp):
    """`out` carries the size PLUS the fee, so the `back` leg pays its own way out of what it
    was sent and the split address ends each chunk at exactly zero."""
    plan = await utxo.plan_split("BEAM", coins=3, method="beampay")
    assert [leg.kind for leg in plan.legs] == ["out", "back"] * 3
    for i, size in enumerate(plan.sizes):
        out_leg, back_leg = plan.legs[2 * i], plan.legs[2 * i + 1]
        assert out_leg.amount_groth == size + FEE
        assert back_leg.amount_groth == size
        assert out_leg.amount_groth - back_leg.amount_groth == FEE  # what the back leg spends
        assert out_leg.outbound is True and back_leg.outbound is False
    assert plan.total_fee_groth == 6 * FEE


async def test_an_asset_plan_primes_the_split_address_with_beam_for_its_own_fees(bp):
    """A payout asset's fee is BEAM, and the `back` legs are sent BY the split address — so one
    `prime` leg carries exactly `coins × fee` up front and the address ends empty in BOTH."""
    bp.fund(TREASURY, ETH_AID, BOX_BETH)
    bp.wallet.seed(ETH_AID, [BOX_BETH])
    plan = await utxo.plan_split("ETH", coins=4, method="beampay")
    prime = plan.legs[0]
    assert prime.kind == "prime" and prime.aid == 0
    assert prime.amount_groth == 4 * FEE  # one fee per `back` leg, and not one groth more
    assert [leg.kind for leg in plan.legs[1:]] == ["out", "back"] * 4
    # the asset legs carry the size on both directions — the fee is never taken from the asset
    for i, size in enumerate(plan.sizes):
        assert plan.legs[1 + 2 * i].amount_groth == size
        assert plan.legs[2 + 2 * i].amount_groth == size
        assert plan.legs[1 + 2 * i].aid == ETH_AID


async def test_the_plan_fits_the_budget_and_leaves_the_remainder_as_change_not_dust(bp):
    """No dust: what does not divide onto the grid stays in the change coin, un-minted."""
    bp.fund(TREASURY, ETH_AID, BOX_BETH)
    bp.wallet.seed(ETH_AID, [BOX_BETH])
    plan = await utxo.plan_split("ETH", coins=12, method="beampay")
    assert plan.step_groth == 1_000
    assert all(s % 1_000 == 0 for s in plan.sizes)
    committed = sum(plan.sizes)
    assert committed <= BOX_BETH
    remainder = BOX_BETH - committed
    # the remainder is smaller than one more coin would be — it is change, not a coin
    assert 0 <= remainder < plan.base_groth + plan.coins * plan.step_groth
    assert plan.asset_needed_groth == plan.sizes[-1]  # the peak is the largest chunk


async def test_a_size_off_the_grid_is_refused(bp):
    with pytest.raises(utxo.SplitError, match="not a multiple"):
        await utxo.plan_split("BEAM", coins=2, size=15_000_001, method="beampay")


async def test_a_treasury_too_thin_for_the_plan_is_refused_before_anything_is_sent(bp):
    """⛔ The peak is the LARGEST chunk plus every fee before it, not the average one."""
    bp.addresses[TREASURY]["available"]["0"] = 5_000_000  # 0.05 BEAM
    bp.wallet.seed(0, [5_000_000])
    with pytest.raises(utxo.SplitError, match="at its peak"):
        await utxo.plan_split("BEAM", coins=20, method="beampay")


async def test_an_unreadable_coin_list_never_becomes_a_plan(bp, wallet_api):
    """Law 8: 'we cannot see' is not 'there is nothing there', and it is certainly not a plan."""
    wallet_api.raise_on.add("get_utxo")
    with pytest.raises(utxo.SplitError, match="could not be read"):
        await utxo.plan_split("BEAM", method="beampay")


# ═══════════════════════════════════════════════════════════════ 2 · the dry run sends nothing


async def test_the_dry_run_prints_every_leg_and_sends_nothing(bp, lines):
    rc = await utxo.cmd_split("BEAM", coins=3, out=out_of(lines), method="beampay")
    assert rc == 0
    assert bp.withdrawals == []
    assert bp.created == []  # not even the address is created by a dry run
    assert said(lines, "DRY RUN")
    for i in range(3):
        assert said(lines, "split|BEAM|") and said(lines, f"|{i}|back")
    assert said(lines, "Nothing was sent")
    assert await payouts.db().utxo_splits.count_documents({}) == 0


# ═══════════════════════════════════════════════════════════════════ 3 · apply, end to end


async def test_apply_mints_the_coins_and_the_ledger_comes_out_exactly(bp, wallet_api, lines):
    """The whole thing: 3 chunks, 6 transfers, the treasury's BEAM down by exactly the fees,
    the split address empty, and three coins in the wallet that were not there before."""
    before = await bp.available_groth(TREASURY, 0)
    coins_before = utxo.split_needed(await utxo.coin_targets(bp))
    assert "BEAM" in coins_before  # one usable fee coin: below target, and the status says so

    rc = await utxo.cmd_split("BEAM", coins=3, apply=True, out=out_of(lines), method="beampay")

    assert [int(w["amount"]) for w in bp.withdrawals] == [
        15_000_000 + FEE, 15_000_000,
        15_100_000 + FEE, 15_100_000,
        15_200_000 + FEE, 15_200_000,
    ]
    split_addr = bp.created[0]["address"]
    assert bp.created[0]["wallet_type"] == "regular"
    assert bp.created[0]["note"].startswith("pgasme-utxo-split|BEAM|")
    # ⛔ THE LEDGER, EXACTLY: value round-trips, so only the fees left the treasury
    assert await bp.available_groth(TREASURY, 0) == before - 6 * FEE
    assert await bp.available_groth(split_addr, 0) == 0
    # …and the wallet really has three more coins, each of exactly the planned size
    assert sorted(wallet_api.pool[0])[:4] == [BOX_SMALL, 15_000_000, 15_100_000, 15_200_000]
    assert said(lines, "= before − ")
    assert said(lines, "treasury BEAM")
    # three chunks against a target of 20 is still SHORT, and it says so rather than passing
    assert rc == 1
    assert said(lines, "SHORT by")


async def test_an_asset_split_ends_with_the_address_empty_in_BOTH_assets(bp, wallet_api, lines):
    """bETH end to end: one `prime` leg of BEAM, then four round trips whose fees the split
    address pays out of what it was primed with. The treasury's bETH is UNCHANGED — a round
    trip moves no value — and its BEAM is down by exactly the nine fees."""
    bp.fund(TREASURY, ETH_AID, BOX_BETH)
    wallet_api.seed(ETH_AID, [BOX_BETH])
    beam_before = await bp.available_groth(TREASURY, 0)

    await utxo.cmd_split("ETH", coins=4, apply=True, out=out_of(lines), method="beampay")

    split_addr = bp.created[0]["address"]
    assert [int(w["asset_id"]) for w in bp.withdrawals] == [0] + [ETH_AID] * 8
    assert int(bp.withdrawals[0]["amount"]) == 4 * FEE  # the prime leg, one fee per back leg
    assert await bp.available_groth(TREASURY, ETH_AID) == BOX_BETH  # value round-tripped
    assert await bp.available_groth(TREASURY, 0) == beam_before - 9 * FEE
    assert await bp.available_groth(split_addr, 0) == 0
    assert await bp.available_groth(split_addr, ETH_AID) == 0
    assert said(lines, "the split address is empty in both assets")
    # four coins of exactly the planned sizes, minted where there was one
    plan = await utxo.plan_split("ETH", coins=4, method="beampay")
    assert set(plan.sizes) <= set(wallet_api.pool[ETH_AID])


async def test_every_leg_writes_its_row_before_the_call_and_after_it(bp):
    """⛔ `/withdraw` is not idempotent and answers no txid: the row that says we called is the
    only thing that stops a second call, so it is written BEFORE the call and keyed on the
    comment — a duplicate is impossible at the database, not merely unlikely in the code."""
    await utxo.cmd_split("BEAM", coins=2, apply=True, out=lambda *_a: None, method="beampay")
    d = payouts.db()
    plan_row = await d.utxo_splits.find_one({"kind": "plan"})
    assert plan_row["status"] == "done"
    assert plan_row["coins"] == 2 and plan_row["legs"] == 4
    legs = await d.utxo_splits.find({"kind": "leg"}).to_list(None)
    assert len(legs) == 4
    for leg in legs:
        assert leg["_id"] == leg["comment"]
        assert leg["status"] == "settled"
        assert leg["txid"] and leg["kernel"]
        assert leg["fee_groth"] == FEE
        assert leg["called_at"] <= leg["settled_at"]


async def test_a_retry_never_re_sends_a_chunk_whose_comment_already_exists(bp):
    """The plan is RESUMED, not restarted: the stamp comes off the open row, so every comment
    is the one already in BeamPay's history and every leg is adopted rather than re-sent."""
    await utxo.cmd_split("BEAM", coins=2, apply=True, out=lambda *_a: None, method="beampay")
    sent = len(bp.withdrawals)
    made = len(bp.created)
    lines: list[str] = []

    # the plan is DONE, so a re-run plans afresh — force the resume path by reopening it
    await payouts.db().utxo_splits.update_one({"kind": "plan"}, {"$set": {"status": "running"}})
    rc = await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines), method="beampay")

    assert len(bp.withdrawals) == sent, "a resumed leg must never be sent a second time"
    assert len(bp.created) == made, "the split address is created once per plan, then reused"
    assert said(lines, "already sent")
    assert said(lines, "not re-sent")
    assert rc == 1  # 2 coins against a target of 20 is still short, and it says so
    # ⛔ AND THE LEDGER CHECK SPANS THE WHOLE PLAN, NOT THIS RUN. Σ fees counts every leg,
    # including the ones the FIRST run paid for, so `before` is the plan's own starting balance
    # — re-reading it here would compare this run's opening balance with the whole plan's fees
    # and report drift at its own bookkeeping.
    assert said(lines, "= before − ")
    assert not said(lines, "Something other than this plan moved BEAM")


async def test_a_queued_leg_that_cannot_be_found_is_never_re_sent_by_a_machine(bp):
    """⛔ The lost-answer state. A row that says we called and a history with no transaction
    carrying the comment is exactly the shape a second `/withdraw` would turn into a double
    send of the treasury's money."""
    plan = await utxo.plan_split("BEAM", coins=2, method="beampay")
    await utxo.split_address_for(bp, plan)
    leg = plan.legs[0]
    await payouts.db().utxo_splits.update_one(
        {"_id": leg.comment},
        {"$set": {"kind": "leg", "status": "queued", "called_at": time.time(),
                  "plan_id": plan.plan_id}},
        upsert=True,
    )
    lines: list[str] = []
    rc = await utxo.run_split(plan, out_of(lines), bp)
    assert rc == 1
    assert bp.withdrawals == []
    assert said(lines, "NOT re-sent")
    assert said(lines, "a human resolves it")


async def test_a_halted_plan_is_resumed_and_never_replaced_by_a_second_one(bp):
    """⛔ A HALTED PLAN IS STILL OPEN. Every early stop leaves something behind — value at the
    split address, a leg nobody can find — and a SECOND plan on a fresh address on top of that
    is how the first one's money is forgotten. A re-run resumes it, re-adopts every leg by its
    comment, and halts again at the same place with the same sentence."""
    bp.withdraw_fee = 1_100_000  # the canary halts this one after exactly one leg
    await utxo.cmd_split("BEAM", coins=4, apply=True, out=lambda *_a: None, method="beampay")
    plan_row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert plan_row["status"] == "halted"
    made, sent = len(bp.created), len(bp.withdrawals)

    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=4, apply=True, out=out_of(lines), method="beampay") == 1

    assert said(lines, "resuming " + plan_row["_id"])
    assert said(lines, "this plan HALTED earlier")
    assert len(bp.created) == made, "a resumed plan reuses its own split address"
    assert len(bp.withdrawals) == sent, "and re-sends nothing"
    assert await payouts.db().utxo_splits.count_documents({"kind": "plan"}) == 1


async def test_two_transactions_carrying_one_comment_stop_everything(bp):
    """`find_txs_by_comments` answers a LIST precisely so a double send is VISIBLE."""
    plan = await utxo.plan_split("BEAM", coins=2, method="beampay")
    addr = await utxo.split_address_for(bp, plan)
    for _ in range(2):
        bp.add_tx(type="withdrawal", type_string=None, asset_id="0",
                  value=str(plan.legs[0].amount_groth), fee=str(FEE), sender=TREASURY,
                  receiver=addr, comment=plan.legs[0].comment, kernel="k")
    lines: list[str] = []
    assert await utxo.run_split(plan, out_of(lines), bp) == 1
    assert said(lines, "sent twice")
    assert bp.withdrawals == []


# ══════════════════════════════════════════════════════════════════════ 4 · the refusals


async def test_apply_refuses_while_the_kill_switch_is_set(bp, tmp_path, monkeypatch):
    stop = tmp_path / "pgasme.stop"
    stop.write_text("")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    assert workers.paused() is True
    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines), method="beampay") == 1
    assert bp.withdrawals == []
    assert said(lines, "kill switch")


async def test_apply_refuses_while_a_payout_is_holding_an_input(bp):
    """A release in flight is holding a coin of its asset AND a BEAM fee coin. Running a
    forty-transaction plan across it hands the payout the 'Not enough inputs' this exists to
    prevent, from the tool that exists to prevent it."""
    for status in ("releasing", "bridging", "paying"):
        await payouts.db().payout_requests.delete_many({})
        await payouts.db().payout_requests.insert_one({"_id": f"r-{status}", "status": status})
        lines: list[str] = []
        assert await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines), method="beampay") == 1
        assert bp.withdrawals == []
        assert said(lines, status)


async def test_apply_refuses_while_a_deposit_is_claiming_or_shielding(bp):
    await payouts.db().deposits.insert_one({"_id": "d1", "treasury": "shielding"})
    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines), method="beampay") == 1
    assert bp.withdrawals == []
    assert said(lines, "shielding")


async def test_apply_refuses_while_a_wallet_transaction_is_still_in_flight(bp):
    bp.add_tx(type="withdrawal", type_string=None, status=beam.TX_IN_PROGRESS,
              status_string="in progress", sender=TREASURY, receiver=MP, value="1")
    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines), method="beampay") == 1
    assert bp.withdrawals == []
    assert said(lines, "in flight")


async def test_apply_refuses_while_the_wallet_is_out_of_sync(bp):
    bp.in_sync = False
    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines), method="beampay") == 1
    assert bp.withdrawals == []
    assert said(lines, "NOT in sync")


async def test_apply_refuses_when_the_plan_re_derived_is_not_the_plan_that_was_printed(
    bp, monkeypatch
):
    """⛔ The operator approved NUMBERS, not a command. A plan re-derived from the SIZE the dry
    run computed would pin the very number that is supposed to be re-computed — so the plan
    remembers what it was ASKED for (no --size at all, here) and derives the size again."""
    plan = await utxo.plan_split("BEAM", coins=3, method="beampay")
    assert plan.requested_size is None
    monkeypatch.setattr(settings, "beam_fee_coin_groth", 30_000_000)  # a call costs more now
    lines: list[str] = []
    assert await utxo.run_split(plan, out_of(lines), bp) == 1
    assert bp.withdrawals == []
    assert said(lines, "is NOT the plan that was printed")


async def test_apply_refuses_with_a_REASON_when_the_treasury_can_no_longer_afford_the_plan(bp):
    """A gate that cannot re-derive its plan writes a sentence, never a traceback."""
    plan = await utxo.plan_split("BEAM", coins=3, method="beampay")
    bp.addresses[TREASURY]["available"]["0"] = 5_000_000  # something else spent the BEAM
    bp.wallet.seed(0, [5_000_000])
    lines: list[str] = []
    assert await utxo.run_split(plan, out_of(lines), bp) == 1
    assert bp.withdrawals == []
    assert said(lines, "cannot be re-derived")
    assert said(lines, "at its peak")


async def test_a_beampay_refusal_stops_the_plan_and_says_nothing_was_queued(bp):
    bp.withdraw_ok = False
    bp.withdraw_msg = "Insufficient BEAM balance (including transaction fee)"
    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines), method="beampay") == 1
    assert said(lines, "nothing was queued")
    row = await payouts.db().utxo_splits.find_one({"kind": "leg"})
    assert row["status"] == "refused"
    assert "Insufficient BEAM" in row["error"]


async def test_a_max_privacy_split_address_is_refused_before_it_is_used(bp):
    """A max-privacy token is charged the offline fee and its outputs are locked for up to 72 h:
    a split through one makes the treasury's own money unspendable."""
    plan = await utxo.plan_split("BEAM", coins=2, method="beampay")
    bp.next_regular = bp.next_created  # /create_wallet answers a token for a regular request
    with pytest.raises(utxo.SplitError, match="not the shape of a regular"):
        await utxo.split_address_for(bp, plan)


async def test_a_67_character_regular_address_is_accepted_it_is_a_bbs_channel_not_a_parity_byte(
    bp,
):
    """⛔ THE SHAPE GUARD REFUSED THE LIVE WALLET'S OWN ADDRESS (2026-09-10, T3s deploy).

    `looks_like_regular_address` allowed 64–66 hex on the guess *"64, 66 with the parity byte"*.
    There is no parity byte: a Beam SBBS address is `hex(PeerID)` — 64 characters — plus the BBS
    channel in hex with its leading zeros stripped, so the tail is 0–8 characters long. The
    first live `--apply` of a BEAM split died on `/create_wallet` answering a **67**-character
    regular address, with a second 67-character regular one (BeamPay's own "default") already in
    the same address book. A guard that refuses the only thing that can ever be right is a guard
    that fails.

    Both directions are pinned here, because the same predicate is read the other way round by
    `_prove_mp_address` (which demands NOT-regular): 67 hex is regular, and a max-privacy token
    still is not."""
    sbbs_67 = "ab" * 32 + "1ff"  # 64 hex of PeerID + a 3-hex channel — a real shape
    assert len(sbbs_67) == 67
    assert beampay.looks_like_regular_address(sbbs_67)
    assert beampay.looks_like_regular_address("cd" * 32)  # 64: channel 0, no tail
    assert beampay.looks_like_regular_address("cd" * 32 + "ffffffff")  # 72: a full uint32
    assert not beampay.looks_like_regular_address("cd" * 32 + "f" * 9)  # 73: not an address
    assert not beampay.looks_like_regular_address(MP)  # a base58 token is never regular

    plan = await utxo.plan_split("BEAM", coins=2, method="beampay")
    bp.next_regular = sbbs_67  # the fake serialises the last two characters; the LENGTH is the point
    addr = await utxo.split_address_for(bp, plan)
    assert len(addr) == 67 and beampay.looks_like_regular_address(addr)
    assert (await payouts.db().utxo_splits.find_one({"_id": plan.plan_id}))["split_address"] == addr


# ══════════════════════════════════════════════════════════════ 5 · the two run-time canaries


async def test_a_fee_that_is_not_the_planned_one_halts_after_exactly_one_leg(bp):
    """§WE-SET-IT-WE-DONT-READ-IT. BeamPay sets the fee and ignores ours, so the constant the
    plan is SIZED with is checked against the first transaction it made — before the second one
    is paid for."""
    bp.withdraw_fee = 1_100_000  # the OFFLINE fee: this address is not what we thought
    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines), method="beampay") == 1
    assert len(bp.withdrawals) == 1, "one fee spent, not five"
    assert said(lines, "the plan stops here")
    plan_row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert plan_row["status"] == "halted"
    assert "1100000" in plan_row["reason"]


async def test_a_plan_that_is_spending_the_coins_it_mints_stops_instead_of_paying_on(bp, wallet_api):
    """⛔ THE WALLET PICKS THE INPUTS, NOT US. The ascent is an inference about somebody else's
    algorithm; the count rising is the measurement. When it does not rise, the remaining legs
    would buy nothing, so the plan stops and says how far it got."""
    real_apply = wallet_api.apply
    seen: list[int] = []

    def apply(aid: int, amount: int, fee: int) -> None:
        real_apply(aid, amount, fee)
        seen.append(amount)
        if len(seen) >= 4:  # from the second chunk on, model a wallet that consolidates
            wallet_api.pool[0] = [sum(wallet_api.pool[0]) - 0]

    wallet_api.apply = apply  # type: ignore[method-assign]
    lines: list[str] = []
    assert await utxo.cmd_split("BEAM", coins=6, apply=True, out=out_of(lines), method="beampay") == 1
    assert said(lines, "did not rise")
    assert said(lines, "would buy nothing")
    assert len(bp.withdrawals) == 4, "it stops at the chunk that did not help, not at the end"


async def test_a_split_address_that_does_not_hold_what_was_sent_stops_the_plan(bp):
    """The `back` leg moves the split address's WHOLE balance. If BeamPay credited something
    other than what we sent, the round trip would not close and nothing further is sent."""
    plan = await utxo.plan_split("BEAM", coins=2, method="beampay")
    addr = await utxo.split_address_for(bp, plan)
    original = bp._withdraw

    def short(b: dict[str, Any]) -> tuple[int, Any]:
        got = original(b)
        if b["to_address"] == addr:
            bp.addresses[addr]["available"]["0"] = int(b["amount"]) - 1  # one groth missing
        return got

    bp._withdraw = short  # type: ignore[method-assign]
    lines: list[str] = []
    assert await utxo.run_split(plan, out_of(lines), bp) == 1
    assert said(lines, "needs it to hold exactly")
    assert len(bp.withdrawals) == 1


# ══════════════════════════════════════════════════════════ 6 · the status and health shapes


async def test_coin_targets_count_what_the_release_gate_counts(bp, wallet_api):
    """⛔ ONE READER (law 8). `have` for BEAM is `fee_coins` — how many BEAM coins can each pay
    for a call, which on the box is ONE of the two the wallet holds — and for a payout asset it
    is `coins_regular`. An operator who reads a different number here than the one that held
    the payout has been told a story."""
    bp.fund(TREASURY, ETH_AID, BOX_BETH)
    wallet_api.seed(ETH_AID, [BOX_BETH])
    targets = await utxo.coin_targets(bp)
    assert targets["BEAM"]["have"] == 1  # 9.582 counts, 0.01 does not
    assert targets["BEAM"]["target"] == settings.beam_fee_coins
    assert targets["BEAM"]["spendable_groth"] == BOX_BEAM + BOX_SMALL
    assert targets["ETH"]["have"] == 1
    assert targets["ETH"]["target"] == settings.utxo_target_coins
    assert utxo.split_needed(targets) == ["BEAM", "ETH"]  # BEAM first: it pays for the rest


async def test_an_asset_the_treasury_holds_none_of_is_not_below_target(bp):
    """A monitor that pages about an empty asset teaches the operator to ignore the pager."""
    targets = await utxo.coin_targets(bp)
    assert targets["DAI"]["have"] == 0 and targets["DAI"]["target"] > 0
    assert "DAI" not in utxo.split_needed(targets)


async def test_an_unreadable_wallet_is_none_and_never_a_shortage(bp, wallet_api):
    wallet_api.raise_on.add("get_utxo")
    targets = await utxo.coin_targets(bp)
    assert targets["BEAM"]["have"] is None
    assert targets["ETH"]["have"] is None
    assert utxo.split_needed(targets) == []  # 'we cannot see' is not 'you are short'


async def test_beam_status_prints_target_versus_actual_and_names_the_split_command(bp, lines):
    from pgasme.beam import cmd_status

    await cmd_status(out_of(lines))
    assert said(lines, "COIN TARGETS")
    assert said(lines, "split needed: BEAM")
    assert said(lines, "python -m pgasme.beam split --asset BEAM")


async def test_health_publishes_counts_and_never_amounts(bp, client):
    body = (await client.get("/v1/health")).json()
    coins = body["coins"]
    assert coins["BEAM"] == {"have": 1, "target": settings.beam_fee_coins}
    assert set(coins) == {"BEAM", "ETH", "DAI", "WBTC"}
    for row in coins.values():
        assert set(row) == {"have", "target"}  # ⛔ no balance, no address, no size
        assert all(v is None or isinstance(v, int) for v in row.values())


async def test_health_answers_even_when_the_wallet_cannot_be_read(bp, wallet_api, client):
    """A health route never raises; it says less. `have: null` is not a zero."""
    wallet_api.raise_on.add("get_utxo")
    utxo.reset_health_cache()
    body = (await client.get("/v1/health")).json()
    assert body["coins"]["BEAM"]["have"] is None
    assert body["ok"] is True  # the coin count is not what `ok` means


async def test_the_hold_that_names_the_split_command_names_the_asset_that_is_short(bp, wallet_api):
    """The digest already said "no free coin". It now says which command fixes it — and for a
    wallet short of FEE coins that command is `--asset BEAM`, not the payout asset.

    Driven through the REAL gate (`payouts._funding_gates`), on the box's own 10:30Z shape: a
    rich ledger (9.724 BEAM) and one free 0.01 BEAM coin that can pay for nothing."""
    from pgasme.assets import get_asset

    bp.fund(TREASURY, ETH_AID, 5 * GROTH)
    bp.addresses[TREASURY]["available"]["0"] = 972_400_000
    wallet_api.seed(0, [BOX_SMALL])  # the big coin is locked inside a pending transaction
    wallet_api.seed(ETH_AID, [5 * GROTH])
    payouts.reset_process_state()
    row = {"_id": "req-1", "status": "scheduled", "asset": "ETH", "amount_groth": GROTH,
           "created_at": time.time()}
    await payouts.db().payout_requests.insert_one(dict(row))

    got = await payouts._funding_gates(bp, row, get_asset("ETH"), GROTH, [payouts.SOURCE_REGULAR])

    assert got is None  # nothing was admitted to sign
    held = await payouts.db().payout_requests.find_one({"_id": "req-1"})
    # ⛔ THE COMMAND IS GONE FROM THE ROW (T52, admin 2026-09-10 15:36Z, holding this very
    # sentence): `python -m pgasme.beam split --asset BEAM` is OURS to run, on a box nobody
    # outside it can reach, and `hold_reason` is what a user's order page renders. The OPERATOR
    # half still names both counts and the asset that is short — which is what the split is
    # decided from — and the command itself lives in the admin panel's attention list.
    assert "no free coin" in held["hold_detail"]
    assert "BEAM fee coins 0 and this crossing needs 2" in held["hold_detail"]
    assert "the short asset is BEAM" in held["hold_detail"]
    assert "python -m" not in held["hold_detail"] and "`" not in held["hold_detail"]
    assert held["hold_reason"] == payouts.USER_COIN
    assert "python -m" not in held["hold_reason"] and "coin" in held["hold_reason"]


# ══════════════════════════ 7 · T36b — the split the WALLET makes (`tx_split`, the default)
#
# The admin, watching the BeamPay self-transfers land in his group 2026-09-10: *"What happened,
# why do you send beam to yourself? If it's for UTXOs, there's split_utxos method"*. There is —
# `tx_split`, one transaction, one fee, no `/withdraw` and therefore none of BeamPay's three
# notifications per leg. Read out of the wallet's own source (`beam-bridge-pipe/beam`,
# `wallet/api/v6_0/`, and v6_1/v6_2 add no Split of their own so this IS what :10001 serves):
#
#   v6_api_defs.h:52   macro(Split, "tx_split", API_WRITE_ACCESS, API_SYNC, APPS_BLOCKED)
#   v6_api_parse.cpp:555-597
#       split.assetId = readOptionalAssetID(*this, params);
#       const json coins = getMandatoryParam<NonEmptyJsonArray>(params, "coins");
#       … each amount must be a NON-ZERO 64-bit unsigned integer …
#       auto outsCnt = split.coins.size() + 1;      // the split's outputs + the change coin
#       if (split.assetId … != beam::Asset::s_BeamID) outsCnt++;   // the ASSET change coin
#       Amount minimumFee = std::max(fs.m_Kernel + fs.m_Output * outsCnt, fs.get_DefaultStd());
#       split.fee   = getBeamFeeParam(params, "fee", minimumFee);   // omitted ⇒ the minimum
#       split.txId  = getOptionalParam<ValidTxID>(params, "txId");  // 16 bytes, 32 hex
#   v6_api_handle.cpp:501-537
#       if (data.txId && walletDB->getTx(*data.txId)) { doTxAlreadyExistsError(id); return; }
#       … CreateSplitTransactionParameters(senderAddress.m_walletID, data.coins, data.txId) …
#   → result {"txId": "<32 hex>"}
#
# and `core/block_crypt.cpp:1489-1491` is where the three fee constants come from
# (m_Output 18000, m_Kernel 10000, m_Default 100000 after HF3).


class TxSplitWallet(SplitWallet):
    """`SplitWallet`, plus the one write this method makes — modelled the way the C++ does it.

    ⛔ **THE SENDER AND THE RECEIVER ARE EMPTY STRINGS — MEASURED, NOT READ OUT OF THE C++.**
    `v6_api_handle.cpp:513` calls `walletDB->createAddress(senderAddress)` and
    `CreateSplitTransactionParameters(senderAddress.m_walletID, …)` sets MyID **and** PeerID to
    it (`simple_transaction.cpp:36-42`), so this fake used to answer that address on both sides
    — and BeamPay patch #10 was built to match. The first real split on the box
    (`390def92c7deb2e9c0274b73daaeef57`, 2026-09-10 17:55Z) settled and BeamPay recorded:

        {type: 0, type_string: 'simple', value: '45000000', fee: '100000',
         sender: '', receiver: '', sender_identity: '', receiver_identity: '', success: true}

    #10's guard read `not sender` and refused to book a live registration, leaving a
    100,000-groth drift that paged hourly. Either way BeamPay knows neither side — which is why
    §7 exists at all — but the FIELDS are what the code branches on, so this fake now answers
    what the wallet answers. Patch #10b widened the guard to the shape above."""

    def __init__(self, bp: FakeBeamPay | None = None) -> None:
        super().__init__(bp)
        self.split_calls: list[dict[str, Any]] = []
        self.split_txids: set[str] = set()
        self.split_lands = True  # the daemon picks the transaction up immediately
        self.split_status = beam.TX_COMPLETED
        self.fee_override: int | None = None  # what the WALLET really charged
        # what the wallet-api actually reports on both sides of a split: NOTHING (see above)
        self.peer = ""

    def _m_tx_split(self, p: dict[str, Any]) -> dict[str, Any]:
        coins = p.get("coins")
        assert isinstance(coins, list) and coins, "coins is a mandatory NON-EMPTY array"
        assert all(isinstance(c, int) and c > 0 for c in coins), "each coin is a non-zero uint64"
        aid = int(p.get("asset_id") or 0)
        txid = str(p.get("txId") or "")
        assert txid, "this project always names the txId — it is the whole idempotency"
        if txid in self.split_txids:
            # v6_api_handle.cpp:518 → doTxAlreadyExistsError → ApiError::InvalidTxId (-32004)
            raise beam.BeamError(
                'tx_split: {"code": -32004, "message": "Provided transaction ID already exists '
                'in the wallet."}'
            )
        self.split_txids.add(txid)
        self.split_calls.append(dict(p))
        if self.bp is not None:
            self.bp.events.append("tx_split")
        fee = self.fee_override if self.fee_override is not None else utxo.split_min_fee(
            len(coins), aid
        )
        if self.split_lands:
            total = sum(int(c) for c in coins)
            if aid == 0:
                spent = self._take(0, total + fee)
                self.pool[0].extend(int(c) for c in coins)
                if spent - total - fee:
                    self.pool[0].append(spent - total - fee)
            else:
                spent = self._take(aid, total)
                self.pool[aid].extend(int(c) for c in coins)
                if spent - total:
                    self.pool[aid].append(spent - total)
                paid = self._take(0, fee)
                if paid - fee:
                    self.pool[0].append(paid - fee)
            if self.bp is not None:
                # …and BeamPay's daemon records it exactly as `process_payments.py:479-508`
                # does for any non-contract tx: inserted, and NOTHING booked, because neither
                # `sender` nor `receiver` is in `db.addresses`.
                self.bp.add_tx(
                    txId=txid, type=0, type_string="simple", income=False,
                    asset_id=str(aid), value=str(total), fee=str(fee),
                    sender=self.peer, receiver=self.peer,
                    sender_identity="", receiver_identity="",
                    status=self.split_status, kernel=f"kernel-{txid[:12]}",
                    # the live row says "sent", not "completed" — status 3 is what is read
                    status_string="sent" if self.split_status == 3 else "failed",
                )
                if self.split_status == beam.TX_COMPLETED:
                    # …and then patch #10's branch, BEFORE the untracked page: a txid this
                    # deployment was told about in advance books its kernel fee to the address
                    # that registered it. Nothing else about the transaction is booked, because
                    # a split's outputs never left the wallet.
                    self.bp.book_self_tx(txid, fee)
        return {"txId": txid}


@pytest.fixture
def wsplit(bp: SplitBeamPay, monkeypatch: pytest.MonkeyPatch) -> TxSplitWallet:
    w = TxSplitWallet(bp)
    w.seed(0, [BOX_BEAM, BOX_SMALL])
    bp.wallet = w
    beam.set_wallet(w)
    monkeypatch.setattr(settings, "beam_shader", w.shader)
    yield w
    beam.set_wallet(None)


# ───────────────────────────────────────────────────────────── 7.1 the call, off the source


def test_the_minimum_fee_is_the_wallets_own_arithmetic_port_for_port():
    """`max(m_Kernel + m_Output × outsCnt, m_Default)` — and `outsCnt` counts the CHANGE coin,
    plus a second one for an asset (the BEAM change the fee leaves behind).

    ⚠️ We do not SET this (§WE-SET-IT-WE-DONT-READ-IT): no `fee` goes on the wire, the wallet
    charges its own minimum, and this exists so the dry run can PREDICT the cost and the result
    can compare the prediction with what was actually charged."""
    assert (utxo.FEE_KERNEL_GROTH, utxo.FEE_OUTPUT_GROTH, utxo.FEE_DEFAULT_STD_GROTH) == (
        10_000, 18_000, 100_000
    )
    for n in (1, 2, 3, 4):
        assert utxo.split_min_fee(n, 0) == 100_000  # the default floor covers 5 outputs
    assert utxo.split_min_fee(5, 0) == 10_000 + 18_000 * 6 == 118_000
    assert utxo.split_min_fee(20, 0) == 10_000 + 18_000 * 21 == 388_000
    # an asset carries one more change coin, so it crosses the floor one coin earlier
    for n in (1, 2, 3):
        assert utxo.split_min_fee(n, 36) == 100_000
    assert utxo.split_min_fee(4, 36) == 10_000 + 18_000 * 6 == 118_000
    assert utxo.split_min_fee(12, 36) == 10_000 + 18_000 * 14 == 262_000


def test_the_txid_is_ours_deterministic_and_a_valid_beam_txid():
    """⛔ THIS IS THE IDEMPOTENCY, AND IT IS ENFORCED BY THE WALLET ITSELF. `tx_split` takes an
    optional `txId` and refuses one it already has (`v6_api_handle.cpp:518`), so a txid derived
    from the plan id means a retry cannot make a second split — the wallet says no. A Beam TxID
    is 16 bytes (`wallet/core/common.h:56`), i.e. 32 hex characters (`parse_utils.h:215`)."""
    a = utxo.split_txid("split|BEAM|20260910T150000Z")
    b = utxo.split_txid("split|BEAM|20260910T150000Z")
    c = utxo.split_txid("split|BEAM|20260910T150001Z")
    assert a == b and a != c
    assert len(a) == 32 and all(ch in "0123456789abcdef" for ch in a)


async def test_the_default_method_is_one_transaction_with_equal_coins_and_no_legs(bp, wsplit):
    """⛔ THE ASCENT IS GONE, AND ON PURPOSE. It existed because the BeamPay method made the
    coins ONE AT A TIME and the wallet kept spending the coin the previous chunk had just
    minted. `tx_split` names every output in a single transaction, so there is no sequence to
    poison: equal coins, all of them the size a call costs."""
    plan = await utxo.plan_split("BEAM", coins=4)
    assert plan.method == "tx_split"
    assert plan.legs == ()
    assert plan.sizes == (payouts.fee_floor("send"),) * 4
    assert plan.fee_groth == utxo.split_min_fee(4, 0) == 100_000
    assert plan.total_fee_groth == plan.fee_groth  # ONE fee, not one per leg


async def test_the_rpc_body_is_the_coins_list_the_asset_id_and_our_txid_and_no_fee(bp, wsplit):
    """The exact JSON-RPC params, and nothing else on the wire."""
    await utxo.cmd_split("BEAM", coins=3, apply=True, out=lambda *_a: None)
    assert len(wsplit.split_calls) == 1
    call = wsplit.split_calls[0]
    assert call["coins"] == [payouts.fee_floor("send")] * 3
    assert call["asset_id"] == 0
    assert call["txId"] == utxo.split_txid(f"split|BEAM|{ (await payouts.db().utxo_splits.find_one({'kind': 'plan'}))['stamp'] }")
    assert "fee" not in call, "the wallet sets its own minimum; we never send one"


async def test_an_asset_split_names_the_asset_id_and_pays_its_fee_in_beam(bp, wsplit, lines):
    bp.fund(TREASURY, ETH_AID, BOX_BETH)
    wsplit.seed(ETH_AID, [BOX_BETH])
    beam_before_wallet = sum(wsplit.pool[0])

    rc = await utxo.cmd_split("ETH", coins=4, apply=True, out=out_of(lines))

    call = wsplit.split_calls[0]
    assert call["asset_id"] == ETH_AID
    assert len(call["coins"]) == 4 and len(set(call["coins"])) == 1
    assert sum(call["coins"]) <= BOX_BETH
    fee = utxo.split_min_fee(4, ETH_AID)
    assert sum(wsplit.pool[ETH_AID]) == BOX_BETH, "an asset split moves no asset value at all"
    assert sum(wsplit.pool[0]) == beam_before_wallet - fee, "only the BEAM fee is spent"
    assert rc in (0, 1)  # 4 of a target of 12 is short; the point here is the call


async def test_no_dust_and_no_zero_coin_is_ever_asked_for(bp, wsplit):
    """`v6_api_parse.cpp:571` refuses a zero amount outright — so a plan that would produce one
    is refused HERE, with a sentence, rather than by the wallet with a JSON-RPC error."""
    bp.addresses[TREASURY]["available"]["0"] = 20_000_000
    wsplit.seed(0, [20_000_000])
    with pytest.raises(utxo.SplitError):
        await utxo.plan_split("BEAM", coins=40)  # 40 fee coins do not fit in 0.2 BEAM


# ─────────────────────────────────────────────────── 7.2 the dry run, and what it discloses


async def test_the_dry_run_prints_the_exact_call_and_the_fee_accounting_and_sends_nothing(
    bp, wsplit, lines
):
    rc = await utxo.cmd_split("BEAM", coins=3, out=out_of(lines))
    assert rc == 0
    assert wsplit.split_calls == []
    assert bp.withdrawals == [] and bp.created == []
    assert said(lines, "tx_split")
    assert said(lines, '"coins"')
    assert said(lines, '"asset_id"')
    assert said(lines, '"txId"')
    assert said(lines, "Nothing was sent")
    # …and the CONSEQUENCE, before the operator authorises it — which on a PATCHED BeamPay is
    # that the fee is booked, said with the registration that will do it
    assert said(lines, "THE FEE IS BOOKED")
    assert said(lines, "expect_self_tx")
    assert not said(lines, "ack_residual"), "there is no residual to acknowledge"
    # …and the probe that established it was a GET: nothing was registered on a dry run
    assert [c for c in bp.calls if c[0] == "POST" and "self_tx" in str(c[1])] == []
    assert bp.self_tx_rows == {}
    assert await payouts.db().utxo_splits.count_documents({}) == 0


async def test_the_dry_run_on_an_UNPATCHED_beampay_discloses_the_drift_and_the_refusal(
    bp, wsplit, lines
):
    """⛔ THE DISCLOSURE IS A FACT ABOUT THE DEPLOYMENT, ASKED EVERY TIME. The same plan against
    a BeamPay without patch #10 prints the old, worse truth — and says `--apply` will refuse
    rather than create a drift only a human can clear."""
    bp.self_tx_patched = False

    rc = await utxo.cmd_split("BEAM", coins=3, out=out_of(lines))

    assert rc == 0 and wsplit.split_calls == []
    assert said(lines, "THE FEE CANNOT BE BOOKED")
    assert said(lines, "Untracked transfer")
    assert said(lines, "not_a_contract_tx")
    assert said(lines, "ack_residual")
    assert said(lines, "patch #10") or said(lines, "010-self-tx-expectation")
    assert said(lines, "--method beampay")


async def test_a_beampay_that_cannot_be_ASKED_is_never_read_as_a_no(bp, wsplit, lines):
    """Law 8, in the one place it decides whether real money moves: an unreadable probe is not
    'this deployment does not book self-transactions'. Both refuse `--apply`, and they say
    different things, because they are different facts."""
    bp.raise_on.add("GET /internal/expect_self_tx")

    rc = await utxo.cmd_split("BEAM", coins=3, out=out_of(lines))

    assert rc == 0 and wsplit.split_calls == []
    assert said(lines, "UNKNOWN")
    assert said(lines, "cannot see")


# ──────────────────────────────────────────────────────────────── 7.3 apply, end to end


async def test_apply_makes_exactly_one_transaction_and_mints_the_coins(bp, wsplit, lines):
    ledger_before = await bp.available_groth(TREASURY, 0)
    wallet_before = sum(wsplit.pool[0])
    coins_before = len(wsplit.pool[0])

    rc = await utxo.cmd_split("BEAM", coins=6, apply=True, out=out_of(lines))

    assert len(wsplit.split_calls) == 1
    assert bp.withdrawals == [], "the whole point: not one /withdraw, not one notification"
    assert bp.created == [], "and no address of our own — the wallet makes its own"
    fee = utxo.split_min_fee(6, 0)
    # six coins where there was one big one, plus the change
    assert sorted(wsplit.pool[0]).count(payouts.fee_floor("send")) == 6
    assert len(wsplit.pool[0]) == coins_before + 6
    # ⛔ BOTH SIDES MOVE BY THE FEE, AND BY NOTHING ELSE. The wallet burned the kernel fee; the
    # registration made before the call is what lets BeamPay take the same number off the
    # treasury. That equality IS `verify_balances` — the drift this command used to create is
    # gone, not disclosed. The split's VALUE is in neither number: those coins never left.
    assert sum(wsplit.pool[0]) == wallet_before - fee
    assert await bp.available_groth(TREASURY, 0) == ledger_before - fee
    assert said(lines, "ledger")
    assert said(lines, "moved") or said(lines, "together")
    assert not said(lines, "ack_residual")
    assert rc == 1 and said(lines, "SHORT by")  # 6 of 20 is short and it says so


async def test_the_coin_count_is_re_read_and_the_target_assertion_is_measured(bp, wsplit, lines):
    """`get_utxo` again after the kernel, and the verdict is that number — never the plan's."""
    rc = await utxo.cmd_split("BEAM", coins=20, apply=True, out=out_of(lines))
    assert rc == 0
    assert said(lines, "the target of 20 spendable BEAM coin(s) is met")
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert row["status"] == "done"
    assert row["coins_after"] >= 20


async def test_a_row_is_written_before_the_call_and_after_it(bp, wsplit, monkeypatch):
    """⛔ BEFORE, because the call is irreversible and a lost answer must be resolvable; AFTER,
    because the txid, kernel and fee are the evidence.

    The spy READS THE DATABASE at the instant the wallet is asked to sign — the only way to
    prove "before" is to look at what was written while the call is in progress, not at what is
    there once it has returned."""
    at_call: list[dict[str, Any] | None] = []
    real = beam.Wallet.split

    async def spy(self: Any, coins: list[int], asset_id: int, txid: str) -> str:
        at_call.append(await payouts.db().utxo_splits.find_one({"kind": "plan"}))
        return await real(self, coins, asset_id, txid)

    monkeypatch.setattr(beam.Wallet, "split", spy)
    await utxo.cmd_split("BEAM", coins=3, apply=True, out=lambda *_a: None)

    assert len(at_call) == 1
    pre = at_call[0]
    assert pre is not None, "the row exists before the wallet is ever asked"
    assert pre["status"] == "calling" and pre["called_at"]
    assert pre["txid"] == utxo.split_txid(pre["_id"])
    assert pre["call"]["coins"] == [payouts.fee_floor("send")] * 3
    assert "settled_at" not in pre and "kernel" not in pre

    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert row["method"] == "tx_split"
    assert row["txid"] == utxo.split_txid(row["_id"])
    assert row["called_at"] <= row["settled_at"]
    assert row["kernel"] and row["fee_groth"] == utxo.split_min_fee(3, 0)
    assert row["call"]["coins"] == [payouts.fee_floor("send")] * 3
    assert row["status"] == "done"


# ─────────────────────────────────────────────────────── 7.4 the retry that cannot re-send


async def test_a_retry_never_re_issues_the_split_it_is_resumed_by_status(bp, wsplit, lines):
    """The txid is derived from the plan id, so a re-run looks THAT up in BeamPay's history and
    finds the settled transaction. Nothing is asked of the wallet a second time."""
    await utxo.cmd_split("BEAM", coins=4, apply=True, out=lambda *_a: None)
    await payouts.db().utxo_splits.update_one({"kind": "plan"}, {"$set": {"status": "running"}})

    rc = await utxo.cmd_split("BEAM", coins=4, apply=True, out=out_of(lines))

    assert len(wsplit.split_calls) == 1, "a resumed split is never re-issued"
    assert said(lines, "already made")
    assert rc == 1  # 4 of 20 is short


async def test_even_a_forced_second_call_is_refused_by_the_wallet_itself(bp, wsplit):
    """⛔ THE LAST LINE OF DEFENCE IS NOT OURS. Even if every check above were removed, the
    wallet refuses a txId it already holds — so a double split is impossible by construction,
    not merely unlikely (v6_api_handle.cpp:518 → ApiError::InvalidTxId)."""
    txid = utxo.split_txid("split|BEAM|xyz")
    w = beam.wallet()
    await w.split([15_000_000, 15_000_000], 0, txid)
    with pytest.raises(beam.BeamError, match="already exists"):
        await w.split([15_000_000, 15_000_000], 0, txid)


async def test_a_lost_answer_is_recorded_and_never_re_sent_by_a_machine(bp, wsplit, lines):
    """The transport died with the wallet possibly holding the transaction. The row says so and
    the command STOPS; the deterministic txid is what lets a human — or a re-run — resolve it."""
    wsplit.raise_on.add("tx_split")
    rc = await utxo.cmd_split("BEAM", coins=3, apply=True, out=out_of(lines))
    assert rc == 1
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert row["status"] == "halted"
    assert row["halted_from"] == "lost", "WHERE it stopped survives: the wallet may hold it"
    assert row["txid"] and row["called_at"] and row["error"]
    assert said(lines, "NOT re-issued") or said(lines, "not re-issued")


async def test_a_failed_split_transaction_is_reported_not_retried(bp, wsplit, lines):
    wsplit.split_status = beam.TX_FAILED
    rc = await utxo.cmd_split("BEAM", coins=3, apply=True, out=out_of(lines))
    assert rc == 1
    assert len(wsplit.split_calls) == 1
    assert said(lines, "failed") or said(lines, "STOPPED")


# ───────────────────────────────────────────────── 7.5 the fee, and who can and cannot book it


async def test_an_unpatched_beampay_REFUSES_the_apply_and_sends_nothing(bp, wsplit, lines):
    """⛔ THE FINDING THIS WHOLE METHOD TURNS ON, verified against BeamPay's own gates.

    A `tx_split` is `TxType::Simple` (`simple_transaction.cpp:36` builds it with
    `CreateSimpleTransactionParameters`), so on the box:

      * `process_payments.py:125` `is_contract_tx` is False → it is NOT handled as a contract
        tx, so `expect_contract_tx` can never redirect it: the expectation is only ever consumed
        inside `handle_contract_transaction`;
      * `process_payments.py:557` `is_self_send(tx, sender_exists, receiver_exists)` is
        `bool(receiver_exists) and …` and the receiver is the wallet's own fresh address, so
        this is NOT a self-send either — no bridge-claim hunt, no house offset;
      * `process_payments.py:656-664` `if not sender_exists and not receiver_exists:` →
        *"⚠️ Untracked transfer — NOT booked … attribute manually if needed (ledger vs wallet
        will drift until then)"* and `return`;
      * `api.py:694-696` the adjust route's gate — `is_contract = type == 12 …; if not
        is_contract …: 409 tx_not_booked` — so `/internal/ledger/adjust` REFUSES it, and
        `api.py:997` answers `400 not_a_contract_tx` to a read.

    So on a BeamPay without patch #10 there is no route at all, and a split made anyway would
    burn a kernel fee that the ledger never sees — a drift only a human writing an
    `ack_residual` into Mongo can clear, which the operator has forbidden. So the command
    REFUSES, sends nothing, and names both ways forward."""
    bp.self_tx_patched = False

    rc = await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))

    assert rc == 1
    assert wsplit.split_calls == [], "nothing was sent"
    assert bp.withdrawals == [] and bp.created == []
    posted = [c for c in bp.calls if "ledger/adjust" in str(c[1])]
    assert posted == [], "never post a body whose refusal is already known"
    registered = [c for c in bp.calls if c[0] == "POST" and "expect_" in str(c[1])]
    assert registered == [], "and never register against a deployment that cannot honour it"
    assert said(lines, "REFUSED")
    assert said(lines, "no /internal/expect_self_tx route")
    assert said(lines, "verify_balances")
    assert said(lines, "--method beampay")
    # ⛔ NOT ONE ROW OF PLAN STATE EITHER: a refusal before the first call is not a half-made
    # plan, and leaving one behind would make the next run "resume" work that never started.
    assert await payouts.db().utxo_splits.count_documents({"status": {"$ne": "planned"}}) == 0


async def test_a_beampay_that_cannot_be_asked_also_refuses_the_apply(bp, wsplit, lines):
    bp.raise_on.add("GET /internal/expect_self_tx")
    assert await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines)) == 1
    assert wsplit.split_calls == []
    assert said(lines, "cannot see")


async def test_a_beampay_that_books_some_OTHER_kind_is_not_good_enough(bp, wsplit, lines):
    """A route that exists is not a route that will accept THIS registration: BeamPay whitelists
    the kinds it books, and one it does not know is a 422 at the door — after the money moved,
    if we had not asked first."""
    bp.self_tx_kinds = ["something_else"]
    assert await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines)) == 1
    assert wsplit.split_calls == []
    assert said(lines, "utxo_split")


async def test_the_fee_IS_booked_when_beampay_can_book_it(bp, wsplit, lines):
    """The route the brief named, wired and exercised: when BeamPay reports the transaction as
    one it CAN attribute, the T22 zero-sum adjust is posted, once, under a deterministic id.

    ⛔ STILL LIVE, AND STILL REACHABLE — on a plan whose transaction was made before there was
    ever a registration to read back (one halted before patch #10 and resumed after it). That is
    why the expectation is dropped here: with one present, `_await_self_tx_booking` answers
    first and this route is never consulted, which is the whole ordering."""
    await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    txid = row["txid"]
    # a BeamPay that books this the way it books an invocation
    bp.tx_index[txid].update({"type": 12, "type_string": "contract", "invoke_data": []})
    bp.self_tx_rows.pop(txid, None)      # …and no self-tx registration for it
    await payouts.db().utxo_splits.update_one(
        {"_id": row["_id"]}, {"$unset": {"fee_accounting": ""}}
    )

    lines.clear()
    got = await utxo.book_split_fee(bp, row["_id"], txid, row["fee_groth"], out_of(lines))

    assert got["route"] == "ledger_adjust"
    assert got["adjust_id"] == f"pgasme:split:{txid}"
    posted = [c for c in bp.calls if c[0] == "POST" and "ledger/adjust" in str(c[1])]
    assert len(posted) == 1
    body = posted[0][2]
    assert body["asset_id"] == 0
    assert body["amount_groth"] == row["fee_groth"]
    assert body["after_tx"] == txid
    assert body["from_address"] == TREASURY and body["to_address"] == "__house__"

    # …and it is IDEMPOTENT: a second call posts nothing new
    again = await utxo.book_split_fee(bp, row["_id"], txid, row["fee_groth"], out_of(lines))
    assert again["route"] in ("ledger_adjust", "already")
    assert len([c for c in bp.calls if c[0] == "POST" and "ledger/adjust" in str(c[1])]) == 1


async def test_a_fee_that_is_not_the_predicted_one_is_reported_never_silently_absorbed(
    bp, wsplit, lines
):
    """§WE-SET-IT-WE-DONT-READ-IT the other way round: we predict, the wallet charges, and the
    two are compared out loud. Nothing here adjusts a number to make its own arithmetic close."""
    wsplit.fee_override = 250_000
    await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))
    assert said(lines, "250")
    assert said(lines, "predicted")
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert row["fee_groth"] == 250_000
    assert row["fee_predicted_groth"] == utxo.split_min_fee(5, 0)
    # …and the CHARGE is what got booked, not the guess: the registration carries the prediction
    # for BeamPay to compare against, never as the amount.
    assert row["self_tx"]["expected_fee_groth"] == utxo.split_min_fee(5, 0)
    assert row["fee_accounting"]["route"] == "self_tx"
    assert row["fee_accounting"]["booked_groth"] == 250_000
    assert row["fee_accounting"]["drift_groth"] == 0


# ───────────────────────────────────────────────────────── 7.6 every refusal still applies


async def test_every_apply_refusal_covers_tx_split_too(bp, wsplit, tmp_path, monkeypatch):
    """The gates are the plan's, not the method's: a split competes for the very inputs it is
    trying to create whichever way it makes them."""
    async def refused(**kw: Any) -> None:
        lines: list[str] = []
        assert await utxo.cmd_split("BEAM", coins=3, apply=True, out=out_of(lines)) == 1
        assert wsplit.split_calls == [], kw
        return lines

    await payouts.db().payout_requests.insert_one({"_id": "r1", "status": "bridging"})
    assert said(await refused(gate="payout"), "bridging")
    await payouts.db().payout_requests.delete_many({})

    await payouts.db().deposits.insert_one({"_id": "d1", "treasury": "claiming"})
    assert said(await refused(gate="deposit"), "claiming")
    await payouts.db().deposits.delete_many({})

    bp.in_sync = False
    assert said(await refused(gate="sync"), "NOT in sync")
    bp.in_sync = True

    bp.add_tx(type="withdrawal", type_string=None, status=beam.TX_IN_PROGRESS,
              status_string="in progress", sender=TREASURY, receiver=MP, value="1")
    assert said(await refused(gate="in flight"), "in flight")
    bp.tx_rows.clear()
    bp.tx_index.clear()

    stop = tmp_path / "pgasme.stop"
    stop.write_text("")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    assert said(await refused(gate="kill switch"), "kill switch")


async def test_the_kill_switch_is_checked_inside_the_mover(bp, wsplit, tmp_path, monkeypatch):
    """⛔ One file, one implementation, checked before the irreversible step itself — so a switch
    thrown while the plan is being built halts it before the wallet is ever asked."""
    stop = tmp_path / "pgasme.stop"
    monkeypatch.setattr(settings, "stop_file", str(stop))
    w = beam.wallet()
    stop.write_text("")
    with pytest.raises(beam.Halted, match="kill switch"):
        await w.split([15_000_000], 0, utxo.split_txid("split|BEAM|k"))
    assert wsplit.split_calls == []


async def test_the_plan_re_derived_at_apply_must_be_the_plan_that_was_printed(
    bp, wsplit, monkeypatch
):
    plan = await utxo.plan_split("BEAM", coins=3)
    monkeypatch.setattr(settings, "beam_fee_coin_groth", 30_000_000)
    lines: list[str] = []
    assert await utxo.run_split(plan, out_of(lines), bp) == 1
    assert wsplit.split_calls == []
    assert said(lines, "is NOT the plan that was printed")


# ─────────────────────────────────────────────── 7.7 the BeamPay method, demoted to a fallback


async def test_the_beampay_method_is_still_there_behind_method_beampay_and_says_what_it_costs(
    bp, wallet_api, lines
):
    plan = await utxo.plan_split("BEAM", coins=3, method="beampay")
    assert plan.method == "beampay"
    assert len(plan.legs) == 6
    assert plan.fee_groth == utxo.SPLIT_FEE_GROTH
    assert plan.total_fee_groth == 6 * utxo.SPLIT_FEE_GROTH

    rc = await utxo.cmd_split("BEAM", coins=3, method="beampay", apply=True, out=out_of(lines))
    assert rc == 1  # 3 of 20
    assert len(bp.withdrawals) == 6
    assert said(lines, "FALLBACK") or said(lines, "fallback")
    assert said(lines, "notifi")  # the three-per-leg notification cost, said out loud


async def test_an_unknown_method_is_refused(bp):
    with pytest.raises(utxo.SplitError, match="method"):
        await utxo.plan_split("BEAM", coins=2, method="split_utxos")


async def test_a_resumed_plan_keeps_the_method_it_was_made_with(bp, wallet_api, lines):
    """⛔ A plan is resumed by its stamp, and a plan half-made by one method must never be
    continued by the other — the legs and the txid are not interchangeable."""
    await utxo.cmd_split("BEAM", coins=2, method="beampay", apply=True, out=lambda *_a: None)
    await payouts.db().utxo_splits.update_one({"kind": "plan"}, {"$set": {"status": "running"}})
    rc = await utxo.cmd_split("BEAM", coins=2, apply=True, out=out_of(lines))
    assert rc == 1
    assert said(lines, "beampay")
    assert len(bp.withdrawals) == 4, "resumed as a beampay plan (2 legs per coin), every leg adopted"


async def test_the_cli_parses_the_method_flag(bp, wsplit, lines):
    from pgasme.beam import cli_main

    rc = await cli_main(["split", "--asset", "BEAM", "--coins", "3"], out_of(lines))
    assert rc == 0 and said(lines, "tx_split")

    lines.clear()
    rc = await cli_main(["split", "--asset", "BEAM", "--coins", "3", "--method", "beampay"],
                        out_of(lines))
    assert rc == 0 and said(lines, "beampay")

    lines.clear()
    rc = await cli_main(["split", "--asset", "BEAM", "--method", "nonsense"], out_of(lines))
    assert rc == 2
    assert said(lines, "method")


# ══════════════════════════════ 7.8 · the fee books itself (T36c + BeamPay patch #10) ══════
#
# T36b's finding was that NOTHING in BeamPay could book a `tx_split`'s kernel fee, so the ledger
# ran above the wallet by it for ever and `verify_balances` re-paged that gap until a human wrote
# an `ack_residual` into Mongo by hand. Patch #10 adds the fourth branch: a txid registered in
# advance books `{asset 0: −fee}` to the address that registered it, with no page.
#
# What these pin is the ORDER and the EVIDENCE — the two things a marker-before-the-call design
# is worth nothing without.


async def test_the_registration_is_POSTED_BEFORE_the_wallet_is_asked(bp, wsplit, lines):
    """⛔ THE ONLY ORDER THAT WORKS, and the only one that is safe. BeamPay claims the
    transaction's `success` flag before the branch that consumes the expectation runs and never
    clears it, so a registration that arrives after settlement is refused `tx_already_booked`
    and can never be honoured. Registering first also means a crash between the two leaves a
    harmless pending row that expires on its own — the reverse leaves a fee nobody can attribute.

    Two separate call lists could never show this, so both sides append to ONE log."""
    await utxo.cmd_split("BEAM", coins=4, apply=True, out=out_of(lines))

    assert bp.events[:2] == ["expect_self_tx", "tx_split"], bp.events
    assert bp.events.count("tx_split") == 1
    assert said(lines, "fee registered")


async def test_the_registration_names_the_treasury_the_plan_and_the_PREDICTED_fee(bp, wsplit):
    """The exact body on the wire. The prediction travels so BeamPay can compare it with what
    the wallet actually charged; it is never the amount booked, because the registration carries
    no amount at all — the fee comes from the settled transaction's own field."""
    await utxo.cmd_split("BEAM", coins=4, apply=True, out=lambda *_a: None)

    posted = [c for c in bp.calls if c[0] == "POST" and c[1] == "/internal/expect_self_tx"]
    assert len(posted) == 1
    body = posted[0][2]
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert body["txid"] == row["txid"]
    assert body["address"] == TREASURY
    assert body["trade_ref"] == row["_id"]
    assert body["kind"] == "utxo_split"
    assert body["asset_id"] == 0
    assert body["expected_fee_groth"] == utxo.split_min_fee(4, 0)
    assert "amount" not in body and "fee" not in body


async def test_an_asset_split_registers_its_asset_id_and_still_predicts_a_BEAM_fee(bp, wsplit):
    bp.fund(TREASURY, ETH_AID, BOX_BETH)
    wsplit.seed(ETH_AID, [BOX_BETH])

    await utxo.cmd_split("ETH", coins=4, apply=True, out=lambda *_a: None)

    body = [c for c in bp.calls if c[0] == "POST" and c[1] == "/internal/expect_self_tx"][0][2]
    assert body["asset_id"] == ETH_AID
    assert body["expected_fee_groth"] == utxo.split_min_fee(4, ETH_AID)


async def test_a_registration_beampay_REFUSES_stops_the_plan_before_anything_is_sent(
    bp, wsplit, lines
):
    """The gate promised the operator this fee would be booked. Sending the transaction after
    the registration was refused would make that sentence false, so the plan stops — and it
    stops before the wallet is asked, so there is nothing to unwind."""
    bp.self_tx_refusal = "expectation_conflict"

    rc = await utxo.cmd_split("BEAM", coins=4, apply=True, out=out_of(lines))

    assert rc == 1
    assert wsplit.split_calls == [], "NOTHING was sent"
    assert said(lines, "STOPPED")
    assert said(lines, "NOTHING WAS SENT")
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert row["status"] == "halted"
    assert row["self_tx"]["registered"] is False
    assert "expectation_conflict" in row["self_tx"]["error"]


async def test_the_READ_BACK_is_the_authority_not_our_own_post_reply(bp, wsplit, lines):
    """⛔ THE POST REPLY CAN BE LOST WHILE THE BOOKING STILL HAPPENS — it happens on a later
    processor sweep, in a different process. So what became of the fee is read off BeamPay's own
    row, and that read is what goes on the plan."""
    await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))

    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    acct = row["fee_accounting"]
    fee = utxo.split_min_fee(5, 0)
    assert acct["route"] == "self_tx" and acct["posted"] is True
    assert acct["booked_groth"] == fee
    assert acct["drift_groth"] == 0 and acct["ack_residual_groth"] == 0
    assert acct["address"] == TREASURY
    assert acct["trade_ref"] == row["_id"]
    assert [c for c in bp.calls if c[1].startswith("/internal/self_tx/")], "it was read back"
    assert said(lines, "fee booked")
    assert not said(lines, "ack_residual")


async def test_the_ledger_and_the_wallet_fall_by_the_SAME_number_and_the_report_says_so(
    bp, wsplit, lines
):
    """`verify_balances` in miniature, and the reason the whole route exists. The split's VALUE
    is in neither number: those coins never left the wallet."""
    ledger_before = await bp.available_groth(TREASURY, 0)
    wallet_before = sum(wsplit.pool[0])

    rc = await utxo.cmd_split("BEAM", coins=20, apply=True, out=out_of(lines))

    fee = utxo.split_min_fee(20, 0)
    assert rc == 0
    assert sum(wsplit.pool[0]) == wallet_before - fee
    assert await bp.available_groth(TREASURY, 0) == ledger_before - fee
    assert said(lines, "moved together") or said(lines, "no drift to acknowledge")


async def test_an_ABANDONED_registration_is_reported_with_the_exact_drift_and_the_remedy(
    bp, wsplit, lines
):
    """BeamPay refusing to honour a registration — what settled was not the shape that was
    claimed — is a verdict, not an absence. The command reports the drift it now owns and names
    the acknowledgement, exactly as it did before the patch existed."""
    bp.self_tx_outcome = "abandoned"

    await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))

    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    acct = row["fee_accounting"]
    fee = utxo.split_min_fee(5, 0)
    assert acct["route"] == "none"
    assert acct["drift_groth"] == fee and acct["ack_residual_groth"] == fee
    assert "abandoned" in acct["reason"]
    assert said(lines, "ack_residual")
    # …and the ledger really did NOT move, which is what the report must now say
    assert said(lines, "UNBOOKED")


async def test_a_booking_WE_CANNOT_SEE_is_never_reported_as_one_that_did_not_HAPPEN(
    bp, wsplit, lines
):
    """⛔ LAW 8, AND THE ONE THAT PROTECTS THE OPERATOR HERE. BeamPay books on a later sweep, so
    "not booked yet" is the normal state for minutes. A timeout must therefore say "we could not
    see it" and send the reader back to the row — because acknowledging a residual for a fee
    that IS booked would take the money off the ledger twice."""
    bp.self_tx_outcome = "pending"

    await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))

    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    acct = row["fee_accounting"]
    assert acct["route"] == "unreadable"
    assert acct["drift_groth"] == utxo.split_min_fee(5, 0)
    assert "NOT" in acct["reason"] and "not booked" in acct["reason"]
    assert said(lines, "re-read")
    assert said(lines, "/internal/self_tx/")


async def test_a_plan_made_before_any_of_this_falls_through_to_the_old_accounting(bp, wsplit):
    """A transaction with no registration to read back — a plan whose split was made before
    patch #10 and resumed after it. `expectation_not_found` is a definite answer, and the answer
    is "this route has nothing to say about that txid", so the older accounting gets its turn."""
    await utxo.cmd_split("BEAM", coins=5, apply=True, out=lambda *_a: None)
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    txid = row["txid"]
    bp.self_tx_rows.pop(txid)
    await payouts.db().utxo_splits.update_one(
        {"_id": row["_id"]}, {"$unset": {"fee_accounting": ""}}
    )

    lines: list[str] = []
    acct = await utxo.book_split_fee(bp, row["_id"], txid, row["fee_groth"], out_of(lines))

    assert acct["route"] == "none"
    assert "not_a_contract_tx" in acct["reason"]
    assert acct["drift_groth"] == row["fee_groth"]
    assert said(lines, "ack_residual")


async def test_the_fee_accounting_never_books_or_reports_twice(bp, wsplit, lines):
    """Idempotent on the plan's own row: a re-run of the command re-reads the same booking and
    posts nothing, because a second `POST /internal/expect_self_tx` on a settled transaction is
    exactly what BeamPay refuses."""
    await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))
    ledger_after_one = await bp.available_groth(TREASURY, 0)
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})

    again = await utxo.book_split_fee(bp, row["_id"], row["txid"], row["fee_groth"], out_of(lines))

    assert again["route"] == "already"
    assert await bp.available_groth(TREASURY, 0) == ledger_after_one
    assert len(bp.self_tx_rows) == 1
    assert len([c for c in bp.calls
                if c[0] == "POST" and c[1] == "/internal/expect_self_tx"]) == 1


async def test_a_resume_never_makes_a_SECOND_registration_or_a_second_booking(bp, wsplit, lines):
    """The transaction is already made, so the resume neither re-issues it nor books its fee
    twice. It DOES post the registration again — that is how a plan whose split predates the
    route claims its fee — and BeamPay replays it, because the txid is the document `_id`.

    ⛔ THE INVARIANT IS ONE ROW AND ONE DEBIT, NOT ONE REQUEST. A registration is idempotent by
    construction; a booking is idempotent because the claim is atomic. Counting POSTs instead
    would pin an implementation detail and miss both."""
    ledger_before = await bp.available_groth(TREASURY, 0)
    await utxo.cmd_split("BEAM", coins=5, apply=True, out=lambda *_a: None)
    fee = utxo.split_min_fee(5, 0)
    await payouts.db().utxo_splits.update_one({"kind": "plan"}, {"$set": {"status": "running"}})

    await utxo.cmd_split("BEAM", coins=5, apply=True, out=out_of(lines))

    assert len(wsplit.split_calls) == 1
    assert len(bp.self_tx_rows) == 1, "one registration row, whatever was posted"
    assert await bp.available_groth(TREASURY, 0) == ledger_before - fee, "debited ONCE"
    assert said(lines, "already made")


async def test_the_beampay_fallback_needs_no_patch_and_asks_for_none(bp, wallet_api, lines):
    """⛔ THE GATE IS THE METHOD'S, NOT THE PLAN'S. `--method beampay` moves value between two
    addresses BeamPay owns, so BeamPay books both sides itself and there is no fee to register.
    Gating it on patch #10 would take away the very fallback the refusal above points at."""
    bp.self_tx_patched = False

    rc = await utxo.cmd_split("BEAM", coins=3, method="beampay", apply=True, out=out_of(lines))

    assert rc == 1  # 3 of 20
    assert len(bp.withdrawals) == 6, "every leg was sent"
    assert [c for c in bp.calls if "self_tx" in str(c[1])] == []


async def test_a_resume_of_an_already_booked_plan_does_not_flag_its_own_bookkeeping(
    bp, wsplit, lines
):
    """⛔ THE GUARD MUST KNOW WHAT THIS PLAN ITSELF DID. On a resume the fee is already booked,
    so the treasury's ledger is legitimately below the opening balance this plan recorded — and
    a report that only knew "no route ran THIS time" would call that somebody else moving BEAM
    and fail a run in which nothing is wrong."""
    await utxo.cmd_split("BEAM", coins=20, apply=True, out=lambda *_a: None)
    fee = utxo.split_min_fee(20, 0)
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert row["fee_accounting"]["booked_groth"] == fee
    await payouts.db().utxo_splits.update_one({"kind": "plan"}, {"$set": {"status": "halted"}})

    rc = await utxo.cmd_split("BEAM", coins=20, apply=True, out=out_of(lines))

    assert rc == 0, [ln for ln in lines if "⚠️" in ln]
    assert len(wsplit.split_calls) == 1, "and nothing was re-issued"
    assert not said(lines, "Something ELSE moved BEAM")
    assert said(lines, "moved together") or said(lines, "no drift to acknowledge")


async def test_a_plan_whose_split_predates_the_route_claims_its_fee_on_the_resume(
    bp, wsplit, lines
):
    """⛔ THE SPLIT THAT WAS ALREADY MADE. A payout stuck on "no free coin" does not wait for a
    patch, so a split gets made by the tool that predates this route — and its kernel fee is then
    burned on chain and absent from the ledger, which `verify_balances` re-pages every pass.

    BeamPay accepts the registration LATE (a fee that was never booked is booked late as readily
    as early — booking it is the first move either way), so the resume claims it, BeamPay's sweep
    books it, and the drift closes with no human editing Mongo."""
    await utxo.cmd_split("BEAM", coins=6, apply=True, out=lambda *_a: None)
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    txid, fee = row["txid"], row["fee_groth"]
    # …rewind to the world in which that split was made with no registration behind it
    bp.self_tx_rows.pop(txid)
    bp.tx_index[txid].pop("attributed_to", None)
    bp.fund(TREASURY, 0, fee)                       # the ledger never lost it: that IS the drift
    await payouts.db().utxo_splits.update_one(
        {"_id": row["_id"]}, {"$unset": {"fee_accounting": "", "self_tx": ""},
                              "$set": {"status": "halted"}})
    ledger_before = await bp.available_groth(TREASURY, 0)

    rc = await utxo.cmd_split("BEAM", coins=6, apply=True, out=out_of(lines))

    assert len(wsplit.split_calls) == 1, "nothing was re-issued"
    assert said(lines, "already made")
    assert said(lines, "fee registered LATE")
    after = await payouts.db().utxo_splits.find_one({"_id": row["_id"]})
    assert after["self_tx"]["late"] is True
    assert after["fee_accounting"]["route"] == "self_tx"
    assert after["fee_accounting"]["booked_groth"] == fee
    assert await bp.available_groth(TREASURY, 0) == ledger_before - fee
    assert rc == 1 and said(lines, "SHORT by")   # 6 of 20; the fee is what this test is about


async def test_a_late_registration_that_is_REFUSED_stops_nothing_and_reports_honestly(
    bp, wsplit, lines
):
    """The transaction is already made — refusing to register it cannot un-make it. So a refusal
    is recorded and printed, the run carries on, and the fee accounting says what is true."""
    await utxo.cmd_split("BEAM", coins=6, apply=True, out=lambda *_a: None)
    row = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    bp.self_tx_rows.pop(row["txid"])
    await payouts.db().utxo_splits.update_one(
        {"_id": row["_id"]}, {"$unset": {"fee_accounting": ""}, "$set": {"status": "halted"}})
    bp.self_tx_refusal = "tx_already_attributed"

    rc = await utxo.cmd_split("BEAM", coins=6, apply=True, out=out_of(lines))

    assert said(lines, "late fee registration") and said(lines, "refused")
    assert not said(lines, "STOPPED")
    after = await payouts.db().utxo_splits.find_one({"_id": row["_id"]})
    assert after["self_tx"]["registered"] is False and after["self_tx"]["late"] is True
    assert after["fee_accounting"]["route"] == "none"
    assert rc == 1


# ═══════════ 7.9 · closing a plan that will never finish, and never doing it silently ═══════
#
# 2026-09-10, on the box: `split|BEAM|20260910T150102Z` was `halted` with all four legs settled
# and its coins long since re-spent. `cmd_split` resumes the newest OPEN plan for an asset and
# continues it with the METHOD IT WAS MADE WITH — so every later `--method tx_split` run became
# a `--method beampay` run. There was no CLI way out and Mongo is off limits. Both halves of
# that are fixed here: the mismatch REFUSES instead of warning, and `--abandon` is the way out.


async def finished_beampay_plan(bp, wallet_api) -> dict[str, Any]:
    """A plan whose work is over: every leg settled, split address empty, row left `halted`."""
    await utxo.cmd_split("BEAM", coins=3, method="beampay", apply=True, out=lambda *_a: None)
    await payouts.db().utxo_splits.update_one({"kind": "plan"}, {"$set": {"status": "halted"}})
    return await payouts.db().utxo_splits.find_one({"kind": "plan"})


async def test_a_method_that_is_not_the_open_plans_is_REFUSED_and_names_the_way_out(
    bp, wallet_api, lines
):
    """⛔ A WARNING IS NOT A DECISION. The old code printed "the --method you asked for applies
    to the next NEW plan" and then ran the other method anyway: the operator authorises one
    thing and a different one happens. When those differ, nothing happens."""
    plan = await finished_beampay_plan(bp, wallet_api)
    sent_before = len(bp.withdrawals)

    rc = await utxo.cmd_split("BEAM", coins=3, method="tx_split", apply=True, out=out_of(lines))

    assert rc == 2
    assert len(bp.withdrawals) == sent_before, "nothing was sent either way"
    assert said(lines, "--method tx_split was asked for")
    assert said(lines, plan["_id"])
    assert said(lines, "--abandon")


async def test_leaving_method_OFF_still_resumes_the_open_plan_as_it_was(bp, wallet_api, lines):
    """The refusal is about a CONFLICT, not about resuming: asking for nothing in particular is
    still "continue what is open", which is the behaviour every other test relies on."""
    await finished_beampay_plan(bp, wallet_api)
    rc = await utxo.cmd_split("BEAM", coins=3, apply=True, out=out_of(lines))
    assert rc != 2
    assert said(lines, "beampay")


async def test_abandon_closes_a_finished_plan_and_a_new_split_then_starts_fresh(
    bp, wallet_api, wsplit, lines
):
    plan = await finished_beampay_plan(bp, wallet_api)

    rc = await utxo.abandon_plan(plan["_id"], "its coins were re-spent", out_of(lines), bp)

    assert rc == 0
    row = await payouts.db().utxo_splits.find_one({"_id": plan["_id"]})
    assert row["status"] == "abandoned"
    assert row["abandoned_from"] == "halted"
    assert row["abandon_reason"] == "its coins were re-spent"
    assert row["abandoned_at"] > 0
    # ⛔ AND AN EVENT ROW CARRYING THE EVIDENCE, not merely the fact (law 12)
    ev = await payouts.db().utxo_splits.find_one({"_id": f"{plan['_id']}|abandoned"})
    assert ev["kind"] == "event" and ev["event"] == "abandoned"
    assert ev["from_status"] == "halted" and ev["method"] == "beampay"
    assert ev["split_address"] == row["split_address"]
    assert set(ev["legs"].values()) == {"settled"}
    assert said(lines, "ABANDONED")
    assert said(lines, "Nothing on chain changed")

    # …and the whole point: the next run is a NEW plan, on the method that was asked for
    lines.clear()
    rc = await utxo.cmd_split("BEAM", coins=3, method="tx_split", out=out_of(lines))
    assert rc == 0
    assert said(lines, "tx_split")
    assert not said(lines, "resuming " + plan["_id"])


async def test_abandon_refuses_while_any_leg_is_still_in_flight(bp, wallet_api, lines):
    plan = await finished_beampay_plan(bp, wallet_api)
    leg = await payouts.db().utxo_splits.find_one({"kind": "leg", "plan_id": plan["_id"]})
    await payouts.db().utxo_splits.update_one({"_id": leg["_id"]}, {"$set": {"status": "queued"}})

    rc = await utxo.abandon_plan(plan["_id"], None, out_of(lines), bp)

    assert rc == 1
    assert said(lines, "is not finished")
    assert said(lines, "`queued`")
    assert (await payouts.db().utxo_splits.find_one({"_id": plan["_id"]}))["status"] == "halted"
    assert await payouts.db().utxo_splits.count_documents({"kind": "event"}) == 0


async def test_abandon_refuses_while_the_split_address_still_holds_ANYTHING(
    bp, wallet_api, lines
):
    """⛔ ABANDONING A PLAN WITH VALUE AT ITS ADDRESS DOES NOT END THE WORK — it ends the only
    record of the work, and the money is then attached to an address nothing will look at again.
    Locked counts as held: value locked there is value in flight."""
    plan = await finished_beampay_plan(bp, wallet_api)
    bp.fund(plan["split_address"], 0, 1)

    assert await utxo.abandon_plan(plan["_id"], None, out_of(lines), bp) == 1
    assert said(lines, "still holds value") and said(lines, "available.0 = 1")

    bp.addresses[plan["split_address"]]["available"]["0"] = 0
    bp.addresses[plan["split_address"]]["locked"]["36"] = 5
    lines.clear()
    assert await utxo.abandon_plan(plan["_id"], None, out_of(lines), bp) == 1
    assert said(lines, "locked.36 = 5")


async def test_abandon_refuses_when_the_split_address_cannot_be_READ(bp, wallet_api, lines):
    """Law 8 where it decides whether a record is thrown away: an unreadable balance is not an
    empty one."""
    plan = await finished_beampay_plan(bp, wallet_api)
    bp.raise_on.add("/balances")

    assert await utxo.abandon_plan(plan["_id"], None, out_of(lines), bp) == 1
    assert said(lines, "could not be read") and said(lines, "never 'it is empty'")
    assert (await payouts.db().utxo_splits.find_one({"_id": plan["_id"]}))["status"] == "halted"


async def test_abandon_refuses_a_tx_split_whose_transaction_may_be_in_the_wallet(
    bp, wsplit, lines
):
    """⛔ NO LEGS DOES NOT MEAN NOTHING IN FLIGHT. A `tx_split` plan IS its transaction, so its
    own status is what says whether the wallet may be holding one — and `lost` means precisely
    "the answer was lost, the wallet may have it"."""
    await utxo.cmd_split("BEAM", coins=5, apply=True, out=lambda *_a: None)
    plan = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    await payouts.db().utxo_splits.update_one(
        {"_id": plan["_id"]}, {"$set": {"status": "halted", "halted_from": "lost"}})

    assert await utxo.abandon_plan(plan["_id"], None, out_of(lines), bp) == 1
    assert said(lines, "halted_from") and said(lines, "`lost`")

    # …and once the row records what became of it, it can be closed
    await payouts.db().utxo_splits.update_one(
        {"_id": plan["_id"]}, {"$set": {"halted_from": "settled"}})
    lines.clear()
    assert await utxo.abandon_plan(plan["_id"], None, out_of(lines), bp) == 0
    assert said(lines, "creates no address")


async def test_abandon_refuses_a_plan_that_is_already_ignored(bp, wsplit, lines):
    """A `done` plan is not resumed by anything, so abandoning it would record a decision that
    was never needed."""
    await utxo.cmd_split("BEAM", coins=20, apply=True, out=lambda *_a: None)
    plan = await payouts.db().utxo_splits.find_one({"kind": "plan"})
    assert plan["status"] == "done"

    assert await utxo.abandon_plan(plan["_id"], None, out_of(lines), bp) == 1
    assert said(lines, "already ignored")


async def test_abandon_is_idempotent_and_an_unknown_plan_is_a_plain_refusal(
    bp, wallet_api, lines
):
    plan = await finished_beampay_plan(bp, wallet_api)
    assert await utxo.abandon_plan(plan["_id"], "once", out_of(lines), bp) == 0

    lines.clear()
    assert await utxo.abandon_plan(plan["_id"], "again", out_of(lines), bp) == 0
    assert said(lines, "already abandoned") and said(lines, "once")
    row = await payouts.db().utxo_splits.find_one({"_id": plan["_id"]})
    assert row["abandon_reason"] == "once", "the first reason stands"

    lines.clear()
    assert await utxo.abandon_plan("split|BEAM|no-such-plan", None, out_of(lines), bp) == 2
    assert said(lines, "no split plan")


async def test_the_cli_parses_abandon_and_refuses_it_mixed_with_a_plan(bp, wallet_api, lines):
    from pgasme.beam import cli_main

    plan = await finished_beampay_plan(bp, wallet_api)

    rc = await cli_main(["split", "--abandon", plan["_id"], "--reason", "coins re-spent"],
                        out_of(lines))
    assert rc == 0 and said(lines, "ABANDONED")
    row = await payouts.db().utxo_splits.find_one({"_id": plan["_id"]})
    assert row["status"] == "abandoned" and row["abandon_reason"] == "coins re-spent"

    lines.clear()
    rc = await cli_main(["split", "--abandon", plan["_id"], "--apply"], out_of(lines))
    assert rc == 2 and said(lines, "there is no --apply")

    lines.clear()
    rc = await cli_main(["split", "--abandon"], out_of(lines))
    assert rc == 2 and said(lines, "--abandon needs a value")
