"""T34 — INSTANT payouts: an Ethereum distributor EOA pays the user directly, in ~one block.

The direct path burns bETH into the pipe and waits ~66 minutes for the relayer. The instant path
sends ETH we already hold, from a key this process loads at boot, and every law the direct path
was built out of applies to it one venue further along:

  * **Broadcast is not done.** The hash is recorded BEFORE the bytes leave, the receipt is read
    back, and `sent` needs status 1 AND `to == W` AND `value == delivered`. A receipt we cannot
    read is not a receipt that says no.
  * **A retry never re-signs.** One order signs exactly one transaction. A pass that finds no
    receipt re-broadcasts the SAME bytes — the hash on the row never changes — and only a nonce
    consumed by a DIFFERENT transaction ends the order (failed, refunded, paged once).
  * **Every guard needs the level it protects.** The float is read live and compared against
    delivered + gas × headroom; an unreadable float HOLDS, because "we cannot see" is never
    "there is enough". The nonce the row will use is reserved by one conditional update, so two
    passes cannot sign one nonce twice.
  * **A refusal writes a row.** Every gate above holds with its reason ON the order.
  * **A refill is not a payout.** Topping the distributor up is a treasury→own-address crossing:
    it books no ledger entry, refunds nothing and belongs to no user.
  * **A key file that anyone can read is not a key.** 0600 or the API refuses to load it, and
    nothing anywhere prints, logs or pages the key itself.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from eth_account import Account as EthAccount
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

from pgasme import distributor, ledger, payouts, tg, workers
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
GROTH = 10**8
GRID = ETH.grid  # 1e10 wei per groth
W = "0x2222222222222222222222222222222222222222"
STRANGER = "0x3333333333333333333333333333333333333333"

# A throwaway key generated for this file and used nowhere else. The production key never
# reaches a repository, an argument list or a log line — it is read from a 0600 file inside the
# process (`distributor.signer`).
TEST_KEY = "0x" + "11" * 32
TEST_ADDR = EthAccount.from_key(TEST_KEY).address

# eth_feeHistory shaped like the real answer: one more baseFeePerGas entry than blocks asked
# for (the last is the block being built), and a 50th-percentile tip per block.
BASE_WEI = 10**9  # 1 gwei
TIP_WEI = 10**8  # 0.1 gwei
FEE_HISTORY = {
    "baseFeePerGas": [hex(BASE_WEI)] * 11,
    "reward": [[hex(TIP_WEI)] for _ in range(10)],
}
MAX_FEE_WEI = 2 * BASE_WEI + TIP_WEI  # what distributor.fee_estimate computes
GAS_COST_WEI = -(-21_000 * MAX_FEE_WEI * 12_500 // 10_000)  # × 1.25, ceil
# what a correctly quoted order funds that gas with, on the Beam grid: exactly the gas
# the release measures, rounded UP. A row that funds less is the "gas spiked" case.
GAS_FEE_GROTH = -(-GAS_COST_WEI // GRID)


def _hex(n: int) -> str:
    return hex(int(n))


class FakeChain:
    """An Ethereum RPC pool that ACCEPTS RAW TRANSACTIONS and answers everything the instant
    path reads: eth_feeHistory, eth_getBalance, eth_getTransactionCount, eth_sendRawTransaction,
    eth_getTransactionReceipt, eth_getTransactionByHash, eth_getCode.

    It decodes the bytes it is handed, so a receipt is derived from what was ACTUALLY signed
    rather than from what the test hoped was signed — `mine()` with no overrides can only
    confirm the real nonce, recipient and value.

    Failure modes it can be told to play, each one an incident the design has to survive:
      `dropped`        — accept the bytes and never mine them (the mempool forgot us)
      `refuse_send`    — every endpoint refuses the broadcast with this message
      `unreadable`     — these JSON-RPC methods answer nothing at all (law 8: an unreadable
                         query is not evidence of anything)
      `replace_nonce`  — a DIFFERENT transaction consumed our nonce
    """

    URLS = ("https://a.test", "https://b.test")

    def __init__(self, head: int = 1_000) -> None:
        self.head = head
        self.urls: list[str] = list(self.URLS)
        self.balances: dict[str, int] = {}
        self.latest: dict[str, int] = {}
        self.pending: dict[str, int] = {}
        self.fee_history: dict[str, Any] = dict(FEE_HISTORY)
        self.receipts: dict[str, dict[str, Any]] = {}
        self.txs: dict[str, dict[str, Any]] = {}
        self.code: dict[str, str] = {}
        # every raw payload handed to eth_sendRawTransaction, in order — the record a
        # "re-broadcast the SAME bytes" assertion is made against
        self.sent: list[str] = []
        self.dropped = False
        self.refuse_send: str | None = None
        self.refuse_urls: set[str] = set()
        self.unreadable: set[str] = set()

    # ---------------------------------------------------------------- scripting helpers
    def fund(self, addr: str, wei: int) -> None:
        self.balances[addr.lower()] = int(wei)

    def nonce(self, addr: str, latest: int, pending: int | None = None) -> None:
        self.latest[addr.lower()] = int(latest)
        self.pending[addr.lower()] = int(latest if pending is None else pending)

    def decode(self, raw: str) -> dict[str, Any]:
        d = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        return {
            "from": EthAccount.recover_transaction(raw).lower(),
            "to": "0x" + HexBytes(d["to"]).hex().removeprefix("0x").lower(),
            "value": int(d["value"]),
            "nonce": int(d["nonce"]),
        }

    def mine(
        self,
        raw: str | None = None,
        *,
        status: int = 1,
        to: str | None = None,
        value: int | None = None,
        block: int | None = None,
    ) -> str:
        """Mine the last (or the given) broadcast. Overrides exist so a test can play the one
        thing that must never happen quietly: a receipt that does not match what we signed."""
        raw = raw or self.sent[-1]
        tx = self.decode(raw)
        h = _tx_hash(raw)  # the transaction hash IS the keccak of the signed bytes
        blk = self.head if block is None else block
        self.receipts[h] = {
            "transactionHash": h,
            "status": _hex(status),
            "to": (to or tx["to"]),
            "blockNumber": _hex(blk),
            "gasUsed": _hex(21_000),
            "effectiveGasPrice": _hex(MAX_FEE_WEI),
        }
        self.txs[h] = {
            "hash": h,
            "from": tx["from"],
            "to": (to or tx["to"]),
            "value": _hex(tx["value"] if value is None else value),
            "nonce": _hex(tx["nonce"]),
            "blockNumber": _hex(blk),
        }
        self.latest[tx["from"]] = max(self.latest.get(tx["from"], 0), tx["nonce"] + 1)
        self.pending[tx["from"]] = max(self.pending.get(tx["from"], 0), tx["nonce"] + 1)
        return h

    def confirm(self, n: int = 2) -> None:
        """Advance the head so what is already mined clears the reorg floor (R1).

        A receipt one block deep is not a delivery: `PGAS_INSTANT_CONFIRMATIONS` blocks have to
        sit on top of it before the release is booked, and this is how a test buys them."""
        self.head += int(n)

    def reorg(self, addr: str, block: int | None = None) -> None:
        """The block that carried our transaction is GONE: no receipt, no transaction, and the
        account's nonce is back where it was. Exactly what a re-organisation does to a payout
        that was one confirmation deep, and the reason the floor exists at all."""
        self.receipts.clear()
        self.txs.clear()
        a = addr.lower()
        self.latest[a] = 0 if block is None else int(block)
        self.pending[a] = self.latest[a]
        self.dropped = True

    def replace_nonce(self, addr: str, nonce: int) -> None:
        """SOMEBODY ELSE spent our nonce: the account moved past it and our hash is nowhere."""
        a = addr.lower()
        self.latest[a] = max(self.latest.get(a, 0), int(nonce) + 1)
        self.pending[a] = max(self.pending.get(a, 0), int(nonce) + 1)
        self.dropped = True

    # ---------------------------------------------------------------- the JSON-RPC surface
    def _answer(self, method: str, params: list[Any], url: str | None = None) -> Any:
        from pgasme import ethpipe

        if method in self.unreadable:
            raise ethpipe.RpcError(f"{method}: no endpoint answered")
        if method == "eth_blockNumber":
            return _hex(self.head)
        if method == "eth_feeHistory":
            if self.fee_history is None:
                raise ethpipe.RpcError("eth_feeHistory: no endpoint answered")
            return self.fee_history
        if method == "eth_getBalance":
            return _hex(self.balances.get(str(params[0]).lower(), 0))
        if method == "eth_getTransactionCount":
            a = str(params[0]).lower()
            table = self.pending if params[1] == "pending" else self.latest
            return _hex(table.get(a, 0))
        if method == "eth_sendRawTransaction":
            raw = str(params[0])
            if url is not None and url in self.refuse_urls:
                raise ethpipe.RpcError(f"{url}: this endpoint refuses everything")
            if self.refuse_send:
                raise ethpipe.RpcError(f"{url or 'pool'}: {self.refuse_send}")
            self.sent.append(raw)
            h = _tx_hash(raw)
            if not self.dropped:
                self.mine(raw)
            return h
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(str(params[0]))
        if method == "eth_getTransactionByHash":
            return self.txs.get(str(params[0]))
        if method == "eth_getCode":
            return self.code.get(str(params[0]).lower(), "0x")
        raise ethpipe.RpcError(f"FakeChain: unscripted call {method}")

    async def call(
        self, method: str, params: list[Any], prefer: str | None = None, pin: bool = False
    ) -> Any:
        return self._answer(method, params, prefer)

    async def call_on(self, url: str, method: str, params: list[Any]) -> Any:
        return self._answer(method, params, url)

    async def block_number(self, prefer: str | None = None, pin: bool = False) -> int:
        return self.head

    async def head_from(self) -> tuple[int, str]:
        return self.head, self.urls[0]

    async def receipt(
        self, tx: str, prefer: str | None = None, pin: bool = False
    ) -> dict[str, Any] | None:
        return self._answer("eth_getTransactionReceipt", [tx])

    async def transaction(
        self, tx: str, prefer: str | None = None, pin: bool = False
    ) -> dict[str, Any] | None:
        return self._answer("eth_getTransactionByHash", [tx])

    async def transaction_anywhere(
        self, tx: str
    ) -> tuple[dict[str, Any] | None, str | None, int, list[str]]:
        from pgasme import ethpipe

        answered, errors = 0, []
        for url in self.urls:
            try:
                got = self._answer("eth_getTransactionByHash", [tx], url)
            except ethpipe.RpcError as e:
                errors.append(str(e))
                continue
            answered += 1
            if got:
                return got, url, answered, errors
        return None, None, answered, errors


def _tx_hash(raw: str) -> str:
    from eth_utils import keccak

    return "0x" + keccak(HexBytes(raw)).hex().removeprefix("0x")


# ============================================================================== fixtures


@pytest.fixture
def key_file(tmp_path: Any) -> str:
    p = tmp_path / "distributor-1.env"
    p.write_text(f"# the instant payout distributor\nPGAS_DISTRIBUTOR_KEY={TEST_KEY}\n")
    os.chmod(p, 0o600)
    return str(p)


@pytest.fixture(autouse=True)
def clean_distributor() -> Any:
    """The signer is loaded once per PROCESS. A key from one test must never answer in another."""
    distributor.reset()
    yield
    distributor.reset()


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> FakeChain:
    c = FakeChain()
    c.fund(TEST_ADDR, 10**18)
    c.nonce(TEST_ADDR, 0)
    monkeypatch.setattr(workers, "get_rpc", lambda: c)
    return c


@pytest.fixture
def armed(monkeypatch: pytest.MonkeyPatch, key_file: str) -> None:
    monkeypatch.setattr(settings, "payout_instant_enabled", True)
    monkeypatch.setattr(settings, "distributor_key_file", key_file)
    monkeypatch.setattr(settings, "hold_backoff_s", 0.0)


@pytest.fixture
def paged(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []

    async def fake_send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        out.append((text, key))
        return True

    monkeypatch.setattr(tg, "send", fake_send)
    return out


async def make_instant(
    mock_db: Any,
    delivered: int = 500_000,
    rid: str = "ins1",
    gas_fee: int = GAS_FEE_GROTH,
    **over: Any,
) -> dict[str, Any]:
    """A scheduled instant payout of 0.005 ETH, with the ledger debit that paid for it.

    `delivered_groth` is what the WALLET receives; `fee_groth` is our 2 % and `gas_fee_groth`
    is the distributor's gas, charged at cost. Both fees were debited at scheduling, so the
    release has to clear both out of Scheduled or they sit there for ever."""
    now = time.time()
    row: dict[str, Any] = {
        "_id": rid,
        "account_id": "acct1",
        "asset": "ETH",
        "mode": "instant",
        "W": W,
        "amount_groth": delivered,
        "delivered_groth": delivered,
        "fee_groth": delivered * 2 // 100,
        "gas_fee_groth": gas_fee,
        "bridge_fee_groth": 0,
        "release_at": now - 60,
        "deliver_at": now,
        "status": "scheduled",
        "dest_chain": 1,
        "created_at": now - 120,
        "updated_at": now - 120,
    }
    row.update(over)
    await mock_db["pgasme_test"].payout_requests.insert_one(dict(row))
    await ledger.credit("acct1", "ETH", 10 * delivered, f"seed-{rid}")
    await ledger.schedule(
        "acct1",
        "ETH",
        int(row["delivered_groth"]) + int(row["fee_groth"]),
        rid,
        "test",
        bridge_fee_groth=int(row["gas_fee_groth"]),
    )
    return row


async def request(mock_db: Any, rid: str = "ins1") -> dict[str, Any]:
    return await mock_db["pgasme_test"].payout_requests.find_one({"_id": rid})


async def kinds(mock_db: Any, rid: str) -> list[str]:
    rows = await mock_db["pgasme_test"].entries.find({"ref": rid}).to_list(50)
    return sorted(str(r["kind"]) for r in rows)


# ============================================================================== the key file


async def test_a_key_file_anyone_can_read_is_refused(tmp_path: Any) -> None:
    p = tmp_path / "loose.env"
    p.write_text(f"PGAS_DISTRIBUTOR_KEY={TEST_KEY}\n")
    os.chmod(p, 0o644)
    with pytest.raises(distributor.KeyFileError) as e:
        distributor.signer(str(p))
    assert "0644" in str(e.value)
    # ⛔ and the refusal must not carry the thing it is protecting
    assert TEST_KEY.removeprefix("0x") not in str(e.value)


async def test_a_missing_key_file_is_refused_and_never_guessed(tmp_path: Any) -> None:
    with pytest.raises(distributor.KeyFileError):
        distributor.signer(str(tmp_path / "nope.env"))


async def test_the_address_is_derived_from_the_key_and_the_key_never_leaves(
    key_file: str,
) -> None:
    s = distributor.signer(key_file)
    assert s.address == TEST_ADDR
    # the object may be repr'd into a log line by any exception handler
    assert TEST_KEY.removeprefix("0x") not in repr(s)
    assert TEST_KEY.removeprefix("0x") not in str(s)


async def test_a_key_file_with_no_key_in_it_is_refused(tmp_path: Any) -> None:
    p = tmp_path / "empty.env"
    p.write_text("# nothing here\n")
    os.chmod(p, 0o600)
    with pytest.raises(distributor.KeyFileError):
        distributor.signer(str(p))


# ============================================================================== the fee


async def test_the_gas_estimate_is_read_from_the_chain_with_headroom(chain: FakeChain) -> None:
    est = await distributor.fee_estimate(chain)
    assert est["base_wei"] == BASE_WEI
    assert est["tip_wei"] == TIP_WEI
    assert est["max_fee_wei"] == MAX_FEE_WEI
    assert est["gas_limit"] == 21_000
    # headroom is applied to the COST, and it is a ceiling division: never a wei short
    assert est["gas_cost_wei"] == GAS_COST_WEI


async def test_an_unreadable_fee_history_is_not_a_cheap_one(chain: FakeChain) -> None:
    chain.unreadable.add("eth_feeHistory")
    with pytest.raises(distributor.Unreadable):
        await distributor.fee_estimate(chain)


async def test_headroom_below_one_reads_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A headroom under 1 would make the float gate admit a spend it cannot pay for."""
    monkeypatch.setattr(settings, "instant_gas_headroom", 0.5)
    assert distributor.with_headroom(1_000) == 1_000


# ============================================================================== the refill plan


@pytest.mark.parametrize(
    "have,expect",
    [
        (0, 200),  # empty → all the way to target
        (49, 151),  # below min → to target
        (50, 0),  # exactly min → nothing
        (120, 0),  # between min and target → nothing (a top-up is not a trickle)
        (400, 0),  # over target → nothing, and never a negative
    ],
)
def test_the_refill_planner_tops_up_only_below_the_floor(have: int, expect: int) -> None:
    assert distributor.plan_refill(have, 50, 200) == expect


# ============================================================================== the happy path


async def test_an_instant_payout_records_its_hash_before_it_broadcasts_and_settles_on_the_receipt(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db)
    chain.dropped = True  # do not mine it yet: we want to look at the row mid-flight
    await payouts.process_once()

    row = await request(mock_db)
    assert row["status"] == "paying"
    assert row["instant_nonce"] == 0
    # THE EVIDENCE EXISTS BEFORE THE BYTES LEAVE: the hash on the row is the hash of the bytes
    # the chain was handed, and there is exactly one broadcast.
    assert len(chain.sent) == 1
    assert row["instant_tx"] == _tx_hash(chain.sent[0])
    signed = chain.decode(chain.sent[0])
    assert signed["from"] == TEST_ADDR.lower()
    assert signed["to"] == W.lower()
    assert signed["value"] == 500_000 * GRID
    # the nonce is reserved on the distributor, so the next order cannot sign the same one
    assert (await distributor.active_row())["nonce_next"] == 1

    chain.mine(chain.sent[0])
    block = chain.head
    await payouts.process_once()
    # ⛔ ONE CONFIRMATION IS NOT A DELIVERY (R1): the receipt exists and the floor is not met.
    assert (await request(mock_db))["status"] == "paying"
    chain.confirm()
    await payouts.process_once()

    row = await request(mock_db)
    assert row["status"] == "sent"
    assert row["eth_tx"] == row["instant_tx"]
    assert int(row["eth_block"]) == block
    # the ledger: the release, our 2 % and the gas the order funded — all three out of Scheduled
    assert await kinds(mock_db, "ins1") == [
        "fee",
        "instant_gas_fee",
        "release",
        "schedule",
        "schedule_bridge_fee",
    ]
    bal = await ledger.balance("acct1", "ETH")
    assert bal["scheduled"] == 0
    assert bal["sent"] == 500_000


async def test_a_second_pass_does_not_book_the_release_twice(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db)
    await payouts.process_once()  # scheduled → paying, mined immediately
    chain.confirm()  # …and the reorg floor is met
    await payouts.process_once()  # → sent
    await payouts.process_once()  # nothing left to do
    assert (await request(mock_db))["status"] == "sent"
    assert await kinds(mock_db, "ins1") == [
        "fee",
        "instant_gas_fee",
        "release",
        "schedule",
        "schedule_bridge_fee",
    ]
    assert len(chain.sent) == 1


# ============================================================================== the gates


async def test_the_flag_holds_the_order_dark_and_signs_nothing(
    mock_db: Any, chain: FakeChain, key_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "payout_instant_enabled", False)
    monkeypatch.setattr(settings, "distributor_key_file", key_file)
    await make_instant(mock_db)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert row["dark"] is True
    assert "PGAS_PAYOUT_INSTANT_ENABLED" in row["hold_detail"]
    assert chain.sent == []


async def test_the_kill_switch_signs_nothing(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workers, "paused", lambda: True)
    await make_instant(mock_db)
    await payouts.process_once()
    assert (await request(mock_db))["status"] == "scheduled"
    assert chain.sent == []


async def test_a_float_that_cannot_cover_the_payout_holds_with_both_numbers(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db, delivered=500_000)
    chain.fund(TEST_ADDR, 500_000 * GRID)  # exactly the amount: nothing left for gas
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert "float" in row["hold_detail"]
    assert chain.sent == []


async def test_an_unreadable_float_holds_because_we_cannot_see_is_never_enough(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db)
    chain.unreadable.add("eth_getBalance")
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert "could not be read" in row["hold_detail"]
    assert chain.sent == []


async def test_an_unreadable_gas_price_holds(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db)
    chain.unreadable.add("eth_feeHistory")
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert row["hold_reason"]
    assert chain.sent == []


async def test_a_chain_nonce_ahead_of_ours_holds_rather_than_signing_over_a_stranger(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """Our record says the next nonce is 0 and the account has already used three. Somebody or
    something else is signing with this key: refuse, say so, sign nothing."""
    await distributor.ensure_row(chain)
    chain.nonce(TEST_ADDR, 3)
    await make_instant(mock_db)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert "nonce" in row["hold_detail"]
    assert chain.sent == []


async def test_a_destination_that_grew_code_holds(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db)
    chain.code[W.lower()] = "0x60016002"
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert chain.sent == []


async def test_a_gas_spike_past_the_subsidy_holds_rather_than_paying_out_at_a_loss(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "max_relayer_subsidy", 1.0)
    await make_instant(mock_db, gas_fee=1)  # funded 1 groth of gas; the estimate is far above
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert "gas" in row["hold_detail"]
    assert chain.sent == []


# ============================================================================== in flight


async def test_a_dropped_transaction_is_re_broadcast_byte_for_byte(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ A RETRY NEVER RE-SIGNS. The mempool forgot us; the bytes are the same bytes and the
    hash on the row does not move, so at most one of these can ever mine."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    first = await request(mock_db)
    assert len(chain.sent) == 1

    await payouts.process_once()
    await payouts.process_once()
    again = await request(mock_db)
    assert len(chain.sent) == 3
    assert chain.sent[0] == chain.sent[1] == chain.sent[2]
    assert again["instant_tx"] == first["instant_tx"]
    assert again["instant_nonce"] == first["instant_nonce"]
    assert again["status"] == "paying"
    # and the distributor's nonce did not move either: one order, one nonce
    assert (await distributor.active_row())["nonce_next"] == 1

    chain.dropped = False
    chain.mine(chain.sent[0])
    chain.confirm()
    await payouts.process_once()
    assert (await request(mock_db))["status"] == "sent"


async def test_a_pass_inside_the_re_broadcast_window_does_nothing(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 3600.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    await payouts.process_once()
    assert len(chain.sent) == 1


async def test_a_nonce_consumed_by_a_stranger_holds_for_a_human_and_pages_once(
    mock_db: Any, chain: FakeChain, armed: None, paged: list[tuple[str, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ **NEVER RE-SIGN, AND NEVER FAIL** (T40). The order's own bytes can no longer mine, but
    a fresh signature on a fresh nonce would be a second independently-valid payment for one
    order — and the very fact that brought us here says something else is signing with this key.
    So a HUMAN owns it: the money stays reserved, the user still reads it as delayed, and the
    processor gives nothing back (only the user's own cancel refunds, and not once a transaction
    exists)."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    row = await request(mock_db)
    chain.replace_nonce(TEST_ADDR, int(row["instant_nonce"]))

    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == payouts.HELD and row["held_from"] == payouts.PAYING
    assert "nonce" in row["hold_detail"] and "NEVER re-signed" in row["hold_detail"]
    # ⛔ the money does NOT go back: nothing here refunds any more
    assert await ledger.find_entry("cancel", "ins1") is None
    # every groth the order debited is still in Scheduled — amount + our 2% + the gas it funded
    assert (await ledger.balance("acct1", "ETH"))["scheduled"] == await ledger.debited_groth("ins1")
    # …and the user reads it as delayed, with no ETA anybody can honestly promise
    at, _tail, note = payouts.eta_for(row)
    # ⛔ AND IN THE USER'S WORDS (T52): a row parked for a human used to publish the operator's
    # own sentence — "the nonce was consumed by a transaction that is not ours … NEVER
    # re-signed" — under the word "delayed:". The numbers and the internals are on
    # `hold_detail` now, and this is what the person waiting for their money reads.
    assert at is None and note == payouts.USER_HUMAN

    events = await mock_db["pgasme_test"].events.find(
        {"kind": "payout_instant_nonce_taken"}
    ).to_list(10)
    assert len(events) == 1
    assert not await mock_db["pgasme_test"].events.find_one({"kind": "payout_failed"})
    # …and a second pass does not hold it again or page again — no handler owns `held`
    await payouts.process_once()
    events = await mock_db["pgasme_test"].events.find(
        {"kind": "payout_instant_nonce_taken"}
    ).to_list(10)
    assert len(events) == 1


async def test_a_receipt_that_is_not_ours_is_never_read_as_a_delivery(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """The receipt says the ETH went somewhere else. That is never `sent` — and since T40 it is
    never `failed` either: the nonce is spent by our OWN transaction, so signing a second
    transfer for one order would be the double spend. A human reads the receipt."""
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    chain.mine(chain.sent[0], to=STRANGER)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == payouts.HELD and row["held_from"] == payouts.PAYING
    assert STRANGER.lower() in row["hold_detail"].lower()
    events = await mock_db["pgasme_test"].events.find(
        {"kind": "payout_instant_not_ours"}
    ).to_list(10)
    assert len(events) == 1


async def test_a_receipt_for_the_wrong_amount_is_never_read_as_a_delivery(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    chain.mine(chain.sent[0], value=1)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == payouts.HELD
    assert "value" in row["hold_detail"]


async def test_a_reverted_transaction_holds_and_books_neither_a_release_nor_a_refund(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """⛔ NEITHER HALF. No release: nothing was delivered. And since T40 no refund either — the
    processor does not give money back at all (that is `cancel`, the user's own path, and it is
    refused once an Ethereum transaction exists). The order is `held`: the money sits in
    Scheduled where it has always been and an operator decides what happens to it."""
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    chain.mine(chain.sent[0], status=0)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == payouts.HELD
    assert await ledger.find_entry("release", "ins1") is None
    assert await ledger.find_entry("cancel", "ins1") is None
    assert (await ledger.balance("acct1", "ETH"))["scheduled"] == await ledger.debited_groth("ins1")


async def test_an_unreadable_receipt_is_never_a_verdict(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    row = await request(mock_db)
    chain.replace_nonce(TEST_ADDR, int(row["instant_nonce"]))
    chain.unreadable.add("eth_getTransactionByHash")  # we cannot disprove our own transaction
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "paying"  # ⛔ not failed: an unreadable query is not evidence
    assert row.get("failed_reason") is None


async def test_a_broadcast_every_endpoint_refuses_keeps_the_same_bytes_for_the_next_pass(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    await make_instant(mock_db)
    chain.refuse_send = "insufficient funds for gas * price + value"
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "paying"
    assert row["instant_tx"]
    assert "insufficient funds" in row["instant_send_error"]

    chain.refuse_send = None
    await payouts.process_once()  # the same bytes go out and are taken this time
    assert len(chain.sent) == 1
    chain.confirm()
    await payouts.process_once()  # …and only THEN is the receipt read back
    assert (await request(mock_db))["status"] == "sent"


async def test_one_endpoint_refusing_is_not_the_pool_refusing(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """Any provider that accepts is enough — that is what a pool is for."""
    chain.refuse_urls.add(chain.urls[0])
    await make_instant(mock_db)
    await payouts.process_once()
    assert (await request(mock_db))["status"] in ("paying", "sent")
    assert len(chain.sent) == 1


async def test_already_known_is_acceptance_not_a_failure(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await make_instant(mock_db)
    chain.refuse_send = "already known"
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "paying"
    assert row.get("instant_send_error") is None


# ============================================================================== the refill


async def test_a_float_below_the_floor_writes_exactly_one_refill_and_it_is_not_a_payout(
    mock_db: Any, chain: FakeChain, armed: None, paged: list[tuple[str, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "distributor_float_min_wei", 50 * 10**15)
    monkeypatch.setattr(settings, "distributor_float_target_wei", 200 * 10**15)
    chain.fund(TEST_ADDR, 10 * 10**15)  # 0.01 ETH — below the floor
    assert await payouts.refill_once() == 1

    rows = await mock_db["pgasme_test"].payout_requests.find({"mode": "refill"}).to_list(10)
    assert len(rows) == 1
    r = rows[0]
    assert r["W"] == TEST_ADDR
    assert r["status"] == "scheduled"
    assert r["amount_groth"] == (200 - 10) * 10**15 // GRID
    # ⛔ NOT A USER PAYOUT: no account debited it, so nothing may ever be refunded to one
    assert await ledger.debited_groth(r["_id"]) is None
    assert r["account_id"].startswith("__")
    # paged once, and only once
    assert len([p for p in paged if "refill" in p[0].lower()]) == 1

    # a second pass does not write a second refill while the first is in flight
    assert await payouts.refill_once() == 0
    assert await mock_db["pgasme_test"].payout_requests.count_documents({"mode": "refill"}) == 1


async def test_a_healthy_float_writes_no_refill(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "distributor_float_min_wei", 50 * 10**15)
    chain.fund(TEST_ADDR, 10**18)
    assert await payouts.refill_once() == 0
    assert await mock_db["pgasme_test"].payout_requests.count_documents({"mode": "refill"}) == 0


async def test_an_unreadable_float_writes_no_refill(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    chain.unreadable.add("eth_getBalance")
    assert await payouts.refill_once() == 0
    assert await mock_db["pgasme_test"].payout_requests.count_documents({"mode": "refill"}) == 0


async def test_a_refill_books_no_ledger_entry_when_it_settles(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """`_book_release` is the one place a crossing becomes a ledger fact. A refill has no user,
    no debit and no fee — so it must pass through leaving the ledger untouched."""
    now = time.time()
    row = {
        "_id": "refill1",
        "account_id": "__distributor__",
        "asset": "ETH",
        "mode": "refill",
        "W": TEST_ADDR,
        "amount_groth": 1_000_000,
        "fee_groth": 0,
        "bridge_fee_groth": 500,
        "status": "bridging",
        "release_at": now,
        "created_at": now,
        "updated_at": now,
    }
    await mock_db["pgasme_test"].payout_requests.insert_one(dict(row))
    await payouts._book_release(row)
    assert await mock_db["pgasme_test"].entries.count_documents({"ref": "refill1"}) == 0


# ============================================================================== status / health


async def test_health_states_the_distributor(
    mock_db: Any, chain: FakeChain, armed: None, client: Any
) -> None:
    """⛔ **TWO BOOLEANS AND NOTHING ELSE** (M5). /v1/health is public and unauthenticated: the
    address, the float and the next nonce it will sign are an inventory of a hot wallet and a
    schedule of what it is about to do. The operator's full view is `beam status` and the
    key-protected admin panel; the public one answers "is it there" and "is it well"."""
    import json as _json

    assert await payouts.refill_once() == 0  # a healthy float: nothing to do, but it is READ
    body = (await client.get("/v1/health")).json()
    assert body["distributor"] == {"configured": True, "healthy": True}
    # …and no other corner of the public body leaks what this one refuses to say
    blob = _json.dumps(body)
    assert TEST_ADDR not in blob and TEST_ADDR.lower() not in blob
    assert str(10**18) not in blob
    # the full summary still exists for the operator's own (authenticated) surfaces
    full = await distributor.summary()
    assert full["address"] == TEST_ADDR and full["float_wei"] == str(10**18)


async def test_health_says_a_starved_distributor_is_not_healthy(
    mock_db: Any, chain: FakeChain, armed: None, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`healthy` is not `configured` said twice: a float below the floor cannot pay the next
    order, and a health endpoint that says "true" about it is one the watchdog learns nothing
    from."""
    monkeypatch.setattr(settings, "distributor_float_min_wei", 50 * 10**15)
    chain.fund(TEST_ADDR, 10**15)
    await payouts.refill_once()
    body = (await client.get("/v1/health")).json()
    assert body["distributor"] == {"configured": True, "healthy": False}


async def test_health_says_so_when_no_distributor_is_configured(
    mock_db: Any, client: Any
) -> None:
    d = (await client.get("/v1/health")).json()["distributor"]
    assert d == {"configured": False, "healthy": False}


async def test_beam_status_states_the_distributor_and_never_its_key(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """The operator's read-only view: the address, the float with its age against the floor, the
    nonce it will sign next, and the key file's PATH. Never the key."""
    from pgasme import beam

    await payouts.refill_once()  # registers the row and records the float it read
    lines: list[str] = []
    await beam._print_distributor(lines.append)
    text = "\n".join(lines)
    assert TEST_ADDR in text
    assert str(10**18) in text
    assert "next nonce 0" in text
    assert TEST_KEY.removeprefix("0x") not in text


async def test_beam_status_says_when_there_is_no_distributor(mock_db: Any) -> None:
    from pgasme import beam

    lines: list[str] = []
    await beam._print_distributor(lines.append)
    assert "PGAS_DISTRIBUTOR_KEY_FILE is not set" in "\n".join(lines)


async def test_beam_status_names_a_float_below_the_floor(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pgasme import beam

    monkeypatch.setattr(settings, "distributor_float_min_wei", 50 * 10**15)
    chain.fund(TEST_ADDR, 10**15)
    await payouts.refill_once()
    lines: list[str] = []
    await beam._print_distributor(lines.append)
    assert "BELOW THE FLOOR" in "\n".join(lines)


async def test_the_pass_reports_what_it_refilled(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`process_once` carries the refill decision out with it — a decision path that returns no
    row and no number is one nothing can alert on."""
    monkeypatch.setattr(settings, "distributor_float_min_wei", 50 * 10**15)
    chain.fund(TEST_ADDR, 10**15)
    out = await payouts.process_once()
    assert out["refills"] == 1
    out = await payouts.process_once()
    assert out["refills"] == 0  # one is already crossing


async def test_the_hash_is_on_the_row_before_the_bytes_leave(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ THE EVIDENCE BEFORE THE BYTES. A broadcast whose answer is lost has still landed, so a
    hash we had not written down first is a transfer nobody could find — and the only way to
    resolve one of those is a second signature."""
    seen: list[Any] = []
    real = distributor.broadcast

    async def spy(rpc: Any, raw: str) -> dict[str, Any]:
        row = await mock_db["pgasme_test"].payout_requests.find_one({"_id": "ins1"})
        seen.append((row or {}).get("instant_tx"))
        return await real(rpc, raw)

    monkeypatch.setattr(distributor, "broadcast", spy)
    await make_instant(mock_db)
    await payouts.process_once()
    assert seen, "nothing was broadcast at all"
    assert seen[0] == (await request(mock_db))["instant_tx"]


async def test_two_orders_in_one_pass_cannot_sign_one_nonce(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """The nonce is claimed by ONE conditional update. Two orders that both read `nonce_next`
    and both signed it would produce two transactions of which only one could ever mine — and
    the other is a payout that silently never happens."""
    await make_instant(mock_db, rid="ins1")
    await make_instant(mock_db, rid="ins2")
    await payouts.process_once()
    a, b = await request(mock_db, "ins1"), await request(mock_db, "ins2")
    assert sorted([a["instant_nonce"], b["instant_nonce"]]) == [0, 1]
    assert a["instant_tx"] != b["instant_tx"]
    assert (await distributor.active_row())["nonce_next"] == 2
    assert len(chain.sent) == 2


async def test_a_transfer_already_on_the_wire_is_finished_even_after_the_flag_goes_off(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning the flag off refuses NEW payouts. It must never strand one whose ETH has already
    left — the money is gone from the distributor and the user's balance is still in Scheduled."""
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    chain.mine(chain.sent[0])
    chain.confirm()
    monkeypatch.setattr(settings, "payout_instant_enabled", False)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "sent"
    assert (await ledger.balance("acct1", "ETH"))["scheduled"] == 0


async def test_a_refill_is_not_written_when_the_crossing_cannot_be_priced(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crossing we cannot price is one we do not schedule — never one we schedule unpriced."""
    monkeypatch.setattr(settings, "distributor_float_min_wei", 50 * 10**15)
    chain.fund(TEST_ADDR, 10**15)
    chain.unreadable.add("eth_feeHistory")
    assert await payouts.refill_once() == 0
    assert await mock_db["pgasme_test"].payout_requests.count_documents({"mode": "refill"}) == 0


# ====================================================== T34b — the skeptic's findings, closed
#
# Everything below was found by a read-only review of the T34/T40 instant path. Each one is a
# case where a guard existed at the wrong level, a reader had two implementations, or a decision
# path returned without writing anything down.


# --------------------------------------------------------------- H1: the float is RESERVED


async def test_three_orders_against_a_float_for_one_sign_once_and_hold_twice(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """⛔ **A SIGNED TRANSFER HAS ALREADY SPENT THE FLOAT** even though `eth_getBalance` still
    counts it. `latest` is what is MINED; an order that signed a minute ago is in the mempool
    and its ETH is committed, not available. Three orders gated on the raw balance all pass,
    all sign, and the last two produce transactions that can never mine — a payout that
    silently never happens, which is exactly what the nonce reservation exists to prevent one
    level down."""
    need = 500_000 * GRID + GAS_COST_WEI
    chain.fund(TEST_ADDR, need)  # exactly one payout's worth
    chain.dropped = True  # nothing mines: the first order's ETH is committed, not gone
    now = time.time()
    for i, rid in enumerate(("ins1", "ins2", "ins3")):
        await make_instant(mock_db, rid=rid, release_at=now - 300 + i)

    await payouts.process_once()

    rows = [await request(mock_db, r) for r in ("ins1", "ins2", "ins3")]
    signed = [r for r in rows if r["status"] == "paying"]
    held = [r for r in rows if r["status"] == "scheduled"]
    assert len(signed) == 1, [r["status"] for r in rows]
    assert len(held) == 2
    assert len(chain.sent) == 1
    assert all("float" in str(r.get("hold_detail") or "") for r in held)
    # ONE reader of what is already committed, and it is what the gate subtracted
    assert await payouts.committed_float_wei(TEST_ADDR) == need
    # …and it is still committed on the next pass: the balance has not moved and neither has
    # the verdict (across passes, not only within one)
    await payouts.process_once()
    assert len(chain.sent) == 1
    assert [(await request(mock_db, r))["status"] for r in ("ins2", "ins3")] == [
        "scheduled",
        "scheduled",
    ]


async def test_the_committed_float_counts_only_this_distributor_and_only_what_is_in_flight(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """A row somebody else signed, and a row that is finished, are not this float's commitments."""
    now = time.time()
    d = mock_db["pgasme_test"]
    for rid, status, frm in (
        ("live", payouts.PAYING, TEST_ADDR),
        ("done", "sent", TEST_ADDR),
        ("other", payouts.PAYING, STRANGER),
    ):
        await d.payout_requests.insert_one(
            {
                "_id": rid,
                "status": status,
                "mode": "instant",
                "instant_from": frm,
                "instant_value_wei": str(7 * GRID),
                "instant_gas_cost_wei": str(3),
                "release_at": now,
                "updated_at": now,
            }
        )
    assert await payouts.committed_float_wei(TEST_ADDR) == 7 * GRID + 3
    # …and the row being re-gated is never counted against itself
    assert await payouts.committed_float_wei(TEST_ADDR, exclude_rid="live") == 0


# ------------------------------------------------- H2: the crash between claim and reservation


async def test_a_crash_between_the_claim_and_the_reservation_never_stalls_the_queue(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """The one window a kill -9 can leave open: the ROW says it owns nonce 0 and the
    DISTRIBUTOR's record was never advanced past it. The next order then reads `nonce_next` 0,
    reserves it, and signs a second transaction over the same nonce — of which at most one can
    ever mine. The recovery is idempotent and it happens before anything is signed."""
    await distributor.ensure_row(chain)  # nonce_next 0, seeded from the chain
    now = time.time()
    await make_instant(
        mock_db,
        rid="crash1",
        status=payouts.PAYING,
        status_at=now - 30,
        release_at=now - 300,
        instant_nonce=0,
        instant_from=TEST_ADDR,
        instant_value_wei=str(500_000 * GRID),
        instant_gas_cost_wei=str(GAS_COST_WEI),
    )
    await make_instant(mock_db, rid="next1", release_at=now - 200)

    await payouts.process_once()
    crashed = await request(mock_db, "crash1")
    # the crashed order signs THE NONCE IT ALREADY OWNED — this is its first signature, not a
    # second one, and the record now covers it however the crash left it
    assert crashed["instant_nonce"] == 0 and crashed["instant_tx"]
    assert int((await distributor.active_row())["nonce_next"]) >= 1

    await payouts.process_once()
    nxt = await request(mock_db, "next1")
    assert nxt["instant_nonce"] == 1  # n + 1, never a second nonce 0
    assert nxt["instant_tx"] and nxt["instant_tx"] != crashed["instant_tx"]
    assert sorted(chain.decode(raw)["nonce"] for raw in chain.sent) == [0, 1]
    assert int((await distributor.active_row())["nonce_next"]) == 2


# --------------------------------------------------------- M1: a `paying` row is never silent


async def test_the_monitor_watches_paying_rows_too(
    mock_db: Any, paged: list[tuple[str, str | None]]
) -> None:
    """`paying` is where the money is already on the wire, and it was the ONE active status with
    no SLA: a row that stopped answering sat there for ever and no monitor said a word."""
    assert workers.SLA_S[payouts.PAYING] == 15 * 60
    now = time.time()
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {
            "_id": "stuck1",
            "status": payouts.PAYING,
            "mode": "instant",
            "status_at": now - 16 * 60,
            "updated_at": now - 16 * 60,
            "release_at": now - 3600,
        }
    )
    await workers.stuck_checks()
    assert any("stuck1" in t for t, _k in paged)


async def test_an_unreadable_receipt_writes_a_row_and_reaches_the_pager(
    mock_db: Any, chain: FakeChain, armed: None, paged: list[tuple[str, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ A `log.info` and a `return` is a decision path that writes nothing (law 12). The
    receipt is unreadable, the money is on the wire, and the row must SAY so — and after
    `PGAS_INSTANT_STUCK_AFTER_S` the operator must hear it."""
    monkeypatch.setattr(settings, "instant_stuck_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    paged.clear()
    chain.unreadable.add("eth_getTransactionReceipt")

    await payouts.process_once()

    row = await request(mock_db)
    assert row["status"] == payouts.PAYING  # ⛔ never a verdict
    assert "receipt" in str(row.get("instant_receipt_error") or "").lower()
    assert int(row.get("instant_receipt_unreadable") or 0) == 1
    assert any("ins1" in t for t, _k in paged), paged
    assert any("STUCK" in t and "ins1" in t for t, _k in paged), paged


# ------------------------------------------------------------- M2: the resume re-runs the gates


async def _crashed_row(mock_db: Any, chain: FakeChain, **over: Any) -> dict[str, Any]:
    await distributor.ensure_row(chain)
    now = time.time()
    return await make_instant(
        mock_db,
        rid="crash1",
        status=payouts.PAYING,
        status_at=now - 30,
        release_at=now - 300,
        instant_nonce=0,
        instant_from=TEST_ADDR,
        instant_value_wei=str(500_000 * GRID),
        instant_gas_cost_wei=str(GAS_COST_WEI),
        **over,
    )


async def test_a_resume_with_too_little_float_holds_instead_of_signing(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """A resume is a FIRST signature, and every gate the first signature would have passed has
    to be asked again — hours may have gone by, and the one that pays for the transfer is the
    float."""
    await _crashed_row(mock_db, chain)
    chain.fund(TEST_ADDR, 500_000 * GRID)  # the amount, and not one wei of gas
    await payouts.process_once()
    row = await request(mock_db, "crash1")
    assert row["status"] == payouts.PAYING and not row.get("instant_tx")
    assert "float" in str(row.get("hold_detail") or "")
    assert chain.sent == []


async def test_a_resume_refuses_a_destination_that_grew_code(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    await _crashed_row(mock_db, chain)
    chain.code[W.lower()] = "0x60016002"
    await payouts.process_once()
    row = await request(mock_db, "crash1")
    assert not row.get("instant_tx")
    assert chain.sent == []


async def test_a_resume_whose_nonce_a_stranger_already_spent_holds_for_a_human(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """Nothing was ever broadcast for this order, so its nonce was free when the process died.
    If the account has moved past it now, something else is signing with this key — and a
    machine does not pick a fresh nonce and pay out under those conditions."""
    await _crashed_row(mock_db, chain)
    chain.nonce(TEST_ADDR, 1)  # somebody spent nonce 0
    await payouts.process_once()
    row = await request(mock_db, "crash1")
    assert row["status"] == payouts.HELD
    assert chain.sent == []


# ------------------------------------------------------------------ M3: priced, or not paid


async def test_an_instant_order_that_was_never_priced_for_gas_holds_for_a_requote(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """⛔ The economic gate used to be skipped entirely when the order funded NOTHING — the one
    row for which paying is guaranteed to be at a loss. A legacy row, or one written before
    instant orders were priced, is re-quoted by a human; it is never paid on the house."""
    await make_instant(mock_db, gas_fee=0, bridge_fee_groth=0)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert "not priced" in str(row.get("hold_detail") or "")
    assert chain.sent == []


async def test_the_gas_fee_groth_on_the_row_is_the_charged_fact(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """T35 writes `gas_fee_groth`; a row from before it carried the same pass-through in
    `bridge_fee_groth`. ONE reader of "what did this order fund" (law 9), and a priced order
    pays."""
    assert payouts.instant_gas_charged_groth({"gas_fee_groth": 7}) == 7
    assert payouts.instant_gas_charged_groth({"bridge_fee_groth": 9}) == 9
    assert payouts.instant_gas_charged_groth({"gas_fee_groth": 7, "bridge_fee_groth": 9}) == 7
    await make_instant(mock_db, gas_fee=GAS_FEE_GROTH)
    await payouts.process_once()
    assert (await request(mock_db))["status"] in (payouts.PAYING, "sent")


# ------------------------------------------------------------- M4: a refill is not a payout


async def test_the_public_counters_never_count_a_refill(mock_db: Any, client: Any) -> None:
    """A refill is a treasury→own-address crossing. Counting it as a payout tells the public
    (and us) that we served a user we did not serve."""
    now = time.time()
    d = mock_db["pgasme_test"]
    await d.payout_requests.insert_one(
        {"_id": "u1", "status": "sent", "mode": "instant", "account_id": "acct1",
         "updated_at": now - 10}
    )
    await d.payout_requests.insert_one(
        {"_id": "r1", "status": "sent", "mode": payouts.REFILL,
         "account_id": payouts.REFILL_ACCOUNT, "updated_at": now - 10}
    )
    # …and one written before `mode` existed on the row: the ACCOUNT is the other half
    await d.payout_requests.insert_one(
        {"_id": "r2", "status": "sent", "account_id": payouts.REFILL_ACCOUNT,
         "updated_at": now - 10}
    )
    body = (await client.get("/v1/stats")).json()
    assert body["payouts_24h"] == 1


# ----------------------------------------------------- L1: the one documented re-signature


async def test_an_unminable_transaction_is_fee_bumped_once_on_the_same_nonce(
    mock_db: Any, chain: FakeChain, armed: None, paged: list[tuple[str, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⛔ **THE ONE EXCEPTION TO "NEVER RE-SIGN", AND IT IS SAFE FOR ONE REASON ONLY: THE NONCE
    DOES NOT MOVE.** Same nonce, same destination, same value, a higher fee — two bundles of
    bytes of which the chain can include AT MOST ONE. The attempt is recorded before the bytes
    leave and the old hash is kept for ever, so a late inclusion of either is still recognised
    as this payout."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    monkeypatch.setattr(settings, "instant_stuck_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    first = await request(mock_db)
    h1 = str(first["instant_tx"])
    assert len(chain.sent) == 1

    await payouts.process_once()

    row = await request(mock_db)
    assert len(chain.sent) == 2
    a, b = chain.decode(chain.sent[0]), chain.decode(chain.sent[1])
    assert (a["nonce"], a["to"], a["value"]) == (b["nonce"], b["to"], b["value"])
    assert chain.sent[0] != chain.sent[1]
    h2 = _tx_hash(chain.sent[1])
    assert row["instant_tx"] == h2
    assert int(row["instant_max_fee_wei"]) > int(first["instant_max_fee_wei"])
    assert int(row["instant_tip_wei"]) > int(first["instant_tip_wei"])
    # the ATTEMPTS are the append-only record, written before each broadcast
    assert [str(x.get("txid")) for x in payouts.attempts_of(row)] == [h1, h2]
    assert payouts.instant_hashes(row) == [h2, h1]
    assert int(row["instant_bumps"]) == 1
    assert any("bump" in t.lower() for t, _k in paged), paged
    # the distributor's nonce did not move: one order, one nonce, whatever it signed
    assert int((await distributor.active_row())["nonce_next"]) == 1

    await payouts.process_once()
    row = await request(mock_db)
    assert int(row["instant_bumps"]) == 1  # ⛔ ONE replacement, ever
    assert len(chain.sent) == 3 and chain.sent[2] == chain.sent[1]  # the same bytes again


async def test_a_late_inclusion_of_the_replaced_transaction_is_still_this_payout(
    mock_db: Any, chain: FakeChain, armed: None, paged: list[tuple[str, str | None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replacement is the one on the row, but the ORIGINAL is the one that mined. Both were
    signed for this order and both are recorded, so the receipt is recognised, the release is
    booked once, and nobody is told a stranger is signing with our key."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    monkeypatch.setattr(settings, "instant_stuck_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    h1 = str((await request(mock_db))["instant_tx"])
    await payouts.process_once()  # the bump
    assert len(chain.sent) == 2

    chain.mine(chain.sent[0])  # the ORIGINAL lands
    chain.confirm()
    await payouts.process_once()

    row = await request(mock_db)
    assert row["status"] == "sent"
    assert row["eth_tx"] == h1
    assert await ledger.find_entry("release", "ins1")
    assert not any("SOMETHING ELSE IS SIGNING" in t for t, _k in paged), paged


async def test_a_transaction_a_node_still_holds_is_never_bumped(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement is for a transaction NOBODY has. One that is sitting in a mempool is not
    unminable — it is waiting, and paying a second fee for it buys nothing."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    monkeypatch.setattr(settings, "instant_stuck_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    row = await request(mock_db)
    # the pool can see it, it just has not been included yet
    chain.txs[str(row["instant_tx"])] = {
        "hash": row["instant_tx"], "from": TEST_ADDR.lower(), "to": W.lower(),
        "value": _hex(500_000 * GRID), "nonce": _hex(0), "blockNumber": None,
    }
    await payouts.process_once()
    row = await request(mock_db)
    assert int(row.get("instant_bumps") or 0) == 0
    assert len(chain.sent) == 2 and chain.sent[1] == chain.sent[0]


# ------------------------------------------------- L2: whose transaction consumed the nonce


async def test_the_something_else_is_signing_verdict_reads_every_hash_we_signed(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ The nonce IS consumed — by one of OUR OWN hashes. "Something else is signing with this
    key" there sends an operator hunting for a second processor that does not exist, and the
    page that cries wolf is the page nobody reads (law 15)."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    monkeypatch.setattr(settings, "instant_stuck_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    await payouts.process_once()  # the bump: two hashes on the row now
    row = await request(mock_db)
    assert len(payouts.instant_hashes(row)) == 2

    chain.mine(chain.sent[0])  # the ORIGINAL consumed the nonce; the replacement is nowhere
    row = await request(mock_db)
    assert await payouts._nonce_was_taken(row) is False

    # …and a hash that is genuinely NOT ours still ends the order for a human
    chain.reorg(TEST_ADDR, block=0)
    chain.replace_nonce(TEST_ADDR, 0)
    assert await payouts._nonce_was_taken(await request(mock_db)) is True


# ---------------------------------------------------------------------- R1: the reorg floor


def test_the_reorg_floor_is_two_confirmations() -> None:
    assert int(settings.instant_confirmations) == 2


async def test_a_receipt_that_disappears_before_the_floor_never_becomes_a_delivery(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ ONE CONFIRMATION IS A CANDIDATE, NOT A FACT. A release booked on a receipt that a
    re-organisation then removes is a user credited `sent` for ETH that never left — and the
    ledger has no un-send. The floor is `PGAS_INSTANT_CONFIRMATIONS` blocks, and the receipt is
    read AGAIN in the pass that books it."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    chain.mine(chain.sent[0])

    await payouts.process_once()  # one confirmation deep
    row = await request(mock_db)
    assert row["status"] == payouts.PAYING
    assert await ledger.find_entry("release", "ins1") is None

    chain.reorg(TEST_ADDR)  # …and the block that carried it is gone
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == payouts.PAYING
    assert row["instant_tx"] == _tx_hash(chain.sent[0])
    assert chain.sent[-1] == chain.sent[0]  # the SAME bytes, still the order's only transaction
    assert await ledger.find_entry("release", "ins1") is None

    chain.dropped = False
    chain.mine(chain.sent[0])
    chain.confirm()
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "sent"
    assert (await ledger.balance("acct1", "ETH"))["sent"] == 500_000


async def test_a_transfer_on_the_wire_is_never_marked_dark_by_the_bump_path(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⛔ `dark` DESCRIBES THE CURRENT REASON FOR WAITING OR IT DESCRIBES NOTHING. The fee bump
    re-runs every gate, and with the flag turned off under an order whose ETH is already on the
    wire that gate says no — but the ORDER is not waiting on the flag, it is waiting for a
    receipt. A hold there would park the row for `hold_backoff_s` and hide it from every stuck
    check, which is the one row that must never be invisible."""
    monkeypatch.setattr(settings, "instant_rebroadcast_after_s", 0.0)
    monkeypatch.setattr(settings, "instant_stuck_after_s", 0.0)
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    monkeypatch.setattr(settings, "payout_instant_enabled", False)

    await payouts.process_once()

    row = await request(mock_db)
    assert row["status"] == payouts.PAYING
    assert row.get("dark") is not True
    assert row.get("hold_at") is None
    assert "PGAS_PAYOUT_INSTANT_ENABLED" in str(row.get("instant_plan_note") or "")
    assert int(row.get("instant_bumps") or 0) == 0
    assert len(chain.sent) == 2 and chain.sent[1] == chain.sent[0]  # the SAME bytes, still


async def test_the_receipt_is_read_again_at_the_moment_of_booking(
    mock_db: Any, chain: FakeChain, armed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor is met and the receipt is a candidate — then it is read ONE more time, and the
    second read is what the booking stands on. A re-organisation between the two must not leave
    a `sent` row and a credited ledger behind."""
    await make_instant(mock_db)
    chain.dropped = True
    await payouts.process_once()
    chain.mine(chain.sent[0])
    chain.confirm()

    real = distributor.verify_receipt_any
    calls: list[int] = []

    async def flaky(rpc: Any, hashes: Any, to: str, value_wei: int) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            return await real(rpc, hashes, to, value_wei)
        return {"mined": False, "ok": False, "problem": "", "block": 0, "status": None, "hash": ""}

    monkeypatch.setattr(distributor, "verify_receipt_any", flaky)
    await payouts.process_once()

    assert len(calls) == 2, "the booking pass did not re-read the receipt"
    row = await request(mock_db)
    assert row["status"] == payouts.PAYING
    assert await ledger.find_entry("release", "ins1") is None


async def test_a_row_in_flight_that_cannot_be_valued_holds_the_next_order(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """⛔ AN UNDER-COUNT IS A GATE THAT ADMITS ONE SPEND TWICE. If something already in flight
    cannot be valued, what the float owes is unknown — and unknown holds. It is never treated as
    zero, which is the direction that signs."""
    now = time.time()
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {
            "_id": "weird",
            "status": payouts.PAYING,
            "mode": "instant",
            "instant_from": TEST_ADDR,
            "asset": "NOTANASSET",
            "release_at": now,
            "updated_at": now,
        }
    )
    await make_instant(mock_db)
    await payouts.process_once()
    row = await request(mock_db)
    assert row["status"] == "scheduled"
    assert "cannot be valued" in str(row.get("hold_detail") or "")
    assert chain.sent == []


async def test_a_crash_between_the_attempt_and_the_bytes_is_resumed_not_verified(
    mock_db: Any, chain: FakeChain, armed: None
) -> None:
    """⛔ THE OTHER HALF OF THE CRASH WINDOW. `append_attempt` runs BEFORE the row records the
    transaction, so a kill between them leaves a hash nothing ever broadcast — and no bytes to
    re-offer. That row is RESUMED (its first signature), never read as a transfer in flight:
    verifying a transaction that was never sent, and then re-broadcasting an `instant_raw` that
    is not on the row, is a payout that pages every pass and never moves."""
    await distributor.ensure_row(chain)
    now = time.time()
    await make_instant(
        mock_db,
        rid="crash1",
        status=payouts.PAYING,
        status_at=now - 30,
        release_at=now - 300,
        instant_nonce=0,
        instant_from=TEST_ADDR,
        instant_value_wei=str(500_000 * GRID),
        instant_gas_cost_wei=str(GAS_COST_WEI),
        attempts=[
            {"n": 1, "kind": "eth", "txid": "0x" + "ee" * 32, "at": now - 30, "state": "signed"}
        ],
    )
    await payouts.process_once()
    row = await request(mock_db, "crash1")
    assert row.get("instant_raw") and row.get("instant_tx")
    assert len(chain.sent) == 1
    assert row["instant_tx"] == _tx_hash(chain.sent[0])
    # append-only: the hash that was never sent stays, and the one that was is the newest
    assert [str(a["txid"]) for a in payouts.attempts_of(row)][-1] == row["instant_tx"]
    assert len(payouts.attempts_of(row)) == 2
