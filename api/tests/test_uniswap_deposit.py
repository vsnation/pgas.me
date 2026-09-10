"""Registering a Uniswap gateway deposit, and the scanner attributing its pipe lock.

A transaction hash is public the moment it is broadcast, so on this path — as on every other —
the hash is RESOLVED before a row exists: `from` is the wallet the quote was issued to, `to` is
our router, the calldata carries this quote's reference, and once it is mined the receipt has to
hold BOTH our hook's `PgasDeposit` for that reference AND the pipe's `NewLocalMessage` for our
pubkey (§IDENTITY-BEATS-BALANCE). Nothing that could not be READ is ever a verdict: 503 for an
endpoint that would not answer, 409 for a hash the chain has not seen yet.

And then the part that makes this mode different from every other: the amount is NOT known in
advance, because the hook splits the REAL swap output on-chain. So the credit takes the pipe
log's amount, and the guard is a band — with anything outside it going to `unattributed_locks`
and paging, never to somebody's balance.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from conftest import PUBKEY, lock_log
from eth_abi import encode
from test_uniswap_quote import (
    FORK_OUT,
    HOOK,
    QUOTER,
    ROUTER,
    USDC,
    ZERO,
    body,
    pool_row,
    prices,  # noqa: F401 — autouse fixture, imported so this module gets it too
    quoter,  # noqa: F401 — the scripted Quoter fixture
    registry,  # noqa: F401
)

from pgasme import ethpipe, ledger, scanner, uniswap, workers
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
TX = "0x" + "a1" * 32
OTHER = "0x" + "b2" * 32
# the band this quote allows, recomputed by hand from the vectors' measured fork output:
#   lo      = min_out = out × 0.995            → the hook refuses `value < minOut`, so that IS
#                                                 the lowest value the pipe can ever log
#   ceiling = out × 1.05                       → hi = floor((ceiling − fee)/grid)·grid
LO = 19_993_660_737_233_356
HI = 21_098_730_000_000_000
VALUE_AT_QUOTE = 20_094_030_000_000_000
# Grid-aligned, and the largest such value still UNDER the hook's floor. The band that reasoned
# from the old `out < minOut` rule put its floor a whole relayer fee lower and accepted this;
# nothing the hook can produce lands here.
JUST_UNDER_THE_FLOOR = 19_993_660_000_000_000
FEE = 100_000_000_000  # PGAS_MIN_RELAYER_FEE_WEI


def pgas_log(
    ref: str,
    payer: str,
    value: int,
    *,
    hook: str = HOOK,
    tx: str = TX,
    amount_in: int = 50_000_000,
    relayer_fee: int = FEE,
    log_index: int = 1,
) -> dict[str, Any]:
    """One `PgasDeposit(ref, payer, tokenIn, amountIn, target, value, relayerFee, pubkey)`."""
    data = encode(
        ["uint256", "address", "uint256", "uint256", "bytes"],
        [amount_in, ZERO, value, relayer_fee, bytes.fromhex(PUBKEY)],
    )
    pad = lambda a: "0x" + "00" * 12 + a[2:].lower()  # noqa: E731
    return {
        "address": hook,
        "topics": [uniswap.PGAS_DEPOSIT_TOPIC, ref.lower(), pad(payer), pad(USDC)],
        "data": "0x" + data.hex(),
        "transactionHash": tx,
        "logIndex": hex(log_index),
    }


async def quote_and_sign(client, user, rpc, **over: Any) -> dict[str, Any]:
    """A uniswap quote plus the transaction the user would have signed for it."""
    q = (await client.post("/v1/quote", json=body(**over), headers=user["headers"])).json()
    rpc.txs[TX] = {"from": user["address"], "to": ROUTER, "input": q["tx"]["data"]}
    return q


@pytest.fixture
def armed(monkeypatch, registry):  # noqa: F811
    monkeypatch.setattr(settings, "ingress_armed", True)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    monkeypatch.setattr(settings, "lock_scan_chunk", 500)
    monkeypatch.setattr(settings, "lock_scan_blocks", 2000)
    return PUBKEY


# ----------------------------------------------------------------------------- registration


async def test_a_signed_but_unmined_deposit_registers_on_the_transaction_proof_alone(
    client, user, registry, quoter, armed, rpc, mock_db  # noqa: F811
):
    q = await quote_and_sign(client, user, rpc)
    r = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert r.status_code == 200, r.text
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": r.json()["deposit_id"]})
    assert dep["mode"] == "uniswap" and dep["verified"] is True and dep["status"] == "submitted"
    assert dep["deposit_ref"] == q["deposit_ref"] and dep["order_id"] is None
    assert dep["src"] == {"chain_id": 1, "token": USDC, "amount": "50000000"}
    assert dep["min_out_units"] == q["estimate"]["min_out_units"]
    assert dep["out_units"] == str(FORK_OUT) and dep["relayer_fee_quote_units"] == str(FEE)
    assert dep["eth"]["value_units"] == str(VALUE_AT_QUOTE)  # the ESTIMATE, until the lock lands
    shown = (await client.get(f"/v1/deposits/{dep['_id']}", headers=user["headers"])).json()
    assert shown["mode"] == "uniswap" and shown["src_tx_hash"] == TX.lower()


async def test_a_mined_deposit_needs_both_our_hook_and_the_pipe_in_one_receipt(
    client, user, registry, quoter, armed, rpc  # noqa: F811
):
    q = await quote_and_sign(client, user, rpc)
    rpc.receipts[TX.lower()] = {
        "status": "0x1",
        "from": user["address"],
        "logs": [
            pgas_log(q["deposit_ref"], user["address"], VALUE_AT_QUOTE),
            lock_log(ETH.pipe, 91, VALUE_AT_QUOTE, FEE, PUBKEY, 1500, TX, 2),
        ],
    }
    r = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "broken,status,fragment",
    [
        ("sender", 400, "not sent from the wallet"),
        ("to", 400, "not a call to the Pgas router"),
        ("ref", 400, "does not carry this quote's deposit reference"),
        ("missing", 409, "not visible on Ethereum yet"),
    ],
)
async def test_a_transaction_that_is_not_this_quotes_is_refused_before_a_row_exists(
    client, user, registry, quoter, armed, rpc, mock_db, broken, status, fragment  # noqa: F811
):
    q = await quote_and_sign(client, user, rpc)
    if broken == "sender":
        rpc.txs[TX]["from"] = "0x" + "cc" * 20
    elif broken == "to":
        rpc.txs[TX]["to"] = ETH.pipe
    elif broken == "ref":
        other = (await client.post("/v1/quote", json=body(), headers=user["headers"])).json()
        rpc.txs[TX]["input"] = other["tx"]["data"]
    elif broken == "missing":
        rpc.txs.pop(TX)
    r = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert r.status_code == status and fragment in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0  # nothing was written
    if status == 400:  # …but the refusal itself IS written: a refusal nobody sees is unalertable
        assert await mock_db["pgasme_test"].events.count_documents({"kind": "deposit_mismatch"}) == 1


async def test_an_unreadable_endpoint_is_503_and_never_a_rejection(
    client, user, registry, quoter, armed, rpc, monkeypatch  # noqa: F811
):
    q = await quote_and_sign(client, user, rpc)

    async def dead(tx_hash: str, prefer: str | None = None, pin: bool = False):
        raise ethpipe.RpcError("eth_getTransactionByHash: no endpoint answered")

    monkeypatch.setattr(rpc, "transaction", dead)
    r = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert r.status_code == 503 and "could not read that transaction" in r.json()["detail"]


@pytest.mark.parametrize(
    "receipt_kind,fragment",
    [
        ("reverted", "reverted"),
        ("no_hook_log", "did not reach the Pgas hook"),
        ("foreign_hook", "did not reach the Pgas hook"),
        ("no_pipe_lock", "did not lock anything in the ETH pipe"),
        ("other_payer", "paid by a different wallet"),
    ],
)
async def test_a_mined_transaction_that_did_not_do_what_it_claims_is_refused(
    client, user, registry, quoter, armed, rpc, receipt_kind, fragment  # noqa: F811
):
    q = await quote_and_sign(client, user, rpc)
    ref = q["deposit_ref"]
    lock = lock_log(ETH.pipe, 92, VALUE_AT_QUOTE, FEE, PUBKEY, 1500, TX, 2)
    receipts = {
        "reverted": {"status": "0x0", "logs": []},
        "no_hook_log": {"status": "0x1", "logs": [lock]},
        # our topic, our reference — from somebody else's contract
        "foreign_hook": {
            "status": "0x1",
            "logs": [pgas_log(ref, user["address"], VALUE_AT_QUOTE, hook="0x" + "de" * 20), lock],
        },
        "no_pipe_lock": {
            "status": "0x1",
            "logs": [pgas_log(ref, user["address"], VALUE_AT_QUOTE)],
        },
        "other_payer": {
            "status": "0x1",
            "logs": [pgas_log(ref, "0x" + "cc" * 20, VALUE_AT_QUOTE), lock],
        },
    }
    rpc.receipts[TX.lower()] = receipts[receipt_kind]
    r = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert r.status_code == 400 and fragment in r.json()["detail"]


async def test_one_deposit_reference_can_only_be_registered_once(
    client, user, registry, quoter, armed, rpc, mock_db  # noqa: F811
):
    q = await quote_and_sign(client, user, rpc)
    first = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert first.status_code == 200
    rpc.txs[OTHER] = dict(rpc.txs[TX])  # the same calldata, signed again
    again = await client.post(
        "/v1/deposits",
        json={"quote_id": q["quote_id"], "src_tx_hash": OTHER},
        headers=user["headers"],
    )
    assert again.status_code == 200
    assert again.json()["deposit_id"] == first.json()["deposit_id"]  # the row it already has
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 1


async def test_an_unarmed_uniswap_quote_has_nothing_to_register(
    client, user, registry, quoter, rpc  # noqa: F811
):
    q = (await client.post("/v1/quote", json=body(), headers=user["headers"])).json()
    r = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert r.status_code == 409 and "estimate only (ingress not armed)" in r.json()["detail"]


# ----------------------------------------------------------------------------- attribution


async def _register(client, user, rpc, **over: Any) -> tuple[dict[str, Any], str]:
    q = await quote_and_sign(client, user, rpc, **over)
    r = await client.post(
        "/v1/deposits", json={"quote_id": q["quote_id"], "src_tx_hash": TX}, headers=user["headers"]
    )
    assert r.status_code == 200, r.text
    return q, r.json()["deposit_id"]


def _receipt(ref: str, payer: str, value: int, **over: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    lock = lock_log(ETH.pipe, 93, value, FEE, PUBKEY, 1500, TX, 2)
    hooked = pgas_log(ref, payer, over.pop("hook_value", value), **over)
    return lock, {"status": "0x1", "from": payer, "logs": [hooked, lock]}


async def test_the_pipe_log_is_the_money_not_the_estimate(
    client, user, registry, quoter, armed, rpc, mock_db  # noqa: F811
):
    """The swap paid 1 % less than we quoted. The user is credited what ARRIVED."""
    q, dep_id = await _register(client, user, rpc)
    # a little below the estimate and still above the floor the hook enforces: exactly what a
    # normal 15-second-old quote looks like when it settles
    landed = 20_000_000_000_000_000
    lock, receipt = _receipt(q["deposit_ref"], user["address"], landed)
    rpc.logs_.append(lock)
    rpc.receipts[TX] = receipt
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["locked"] == 1 and st["unattributed"] == 0

    d = mock_db["pgasme_test"]
    dep = await d.deposits.find_one({"_id": dep_id})
    assert dep["status"] == "locked" and dep["eth"]["msg_id"] == 93
    assert dep["eth"]["value_units"] == str(landed)
    assert dep["value_groth"] == landed // ETH.grid
    assert dep["value_groth_quoted"] == VALUE_AT_QUOTE // ETH.grid  # the estimate, kept as evidence
    await workers.confirm_locked(head=1511)
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "credited"
    bal = await ledger.balance(user["account_id"], "ETH")
    assert bal["available"] == landed // ETH.grid


async def test_a_lock_for_a_quote_the_user_never_registered_still_reaches_them(
    client, user, registry, quoter, armed, rpc, mock_db  # noqa: F811
):
    """They signed and closed the tab. The money is in the pipe either way — the reference and
    the payer are the whole claim, and the row is opened here."""
    q = await quote_and_sign(client, user, rpc)
    lock, receipt = _receipt(q["deposit_ref"], user["address"], VALUE_AT_QUOTE)
    rpc.logs_.append(lock)
    rpc.receipts[TX] = receipt
    assert (await scanner.scan_pipe(ETH, rpc))["locked"] == 1
    d = mock_db["pgasme_test"]
    dep = await d.deposits.find_one({"quote_id": q["quote_id"]})
    assert dep and dep["mode"] == "uniswap" and dep["status"] == "locked"
    assert dep["account_id"] == user["account_id"] and dep["address"] == user["address"]
    assert dep["deposit_ref"] == q["deposit_ref"] and dep["src_tx_hash"] is None
    assert dep["value_groth"] == VALUE_AT_QUOTE // ETH.grid and "scanner" in dep["note"]
    shown = (await client.get("/v1/account", headers=user["headers"])).json()
    assert shown["deposits"][0]["mode"] == "uniswap"


@pytest.mark.parametrize(
    "value,hook_value,payer_is_user,why",
    [
        (JUST_UNDER_THE_FLOOR, None, True, "outside the quoted band"),
        (HI + 10**10, None, True, "outside the quoted band"),
        (VALUE_AT_QUOTE, VALUE_AT_QUOTE - 10**10, True, "same number"),
        (VALUE_AT_QUOTE, None, False, "payer mismatch"),
        (VALUE_AT_QUOTE + 1, None, True, "not a multiple of the ETH grid"),
    ],
)
async def test_a_lock_that_does_not_match_the_quote_is_never_credited(
    client, user, registry, quoter, armed, rpc, mock_db, value, hook_value, payer_is_user, why  # noqa: F811
):
    q, dep_id = await _register(client, user, rpc)
    payer = user["address"] if payer_is_user else "0x" + "cc" * 20
    extra = {"hook_value": hook_value} if hook_value is not None else {}
    lock, receipt = _receipt(q["deposit_ref"], payer, value, **extra)
    rpc.logs_.append(lock)
    rpc.receipts[TX] = receipt
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["unattributed"] == 1 and st["locked"] == 0
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "submitted"
    assert why in (await d.unattributed_locks.find_one({}))["reason"]
    assert await d.entries.count_documents({}) == 0  # nobody was credited


async def test_a_hook_log_from_a_strangers_contract_is_not_evidence(
    client, user, registry, quoter, armed, rpc, mock_db  # noqa: F811
):
    q, dep_id = await _register(client, user, rpc)
    lock, receipt = _receipt(
        q["deposit_ref"], user["address"], VALUE_AT_QUOTE, hook="0x" + "de" * 20
    )
    rpc.logs_.append(lock)
    rpc.receipts[TX] = receipt
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["unattributed"] == 1 and st["locked"] == 0
    d = mock_db["pgasme_test"]
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "submitted"
    # it never even looked like a uniswap deposit: our hook did not sign the receipt
    assert "not a cross-chain fill" in (await d.unattributed_locks.find_one({}))["reason"]


async def test_a_reference_nobody_holds_pages_instead_of_crediting_anyone(
    client, user, registry, quoter, armed, rpc, mock_db  # noqa: F811
):
    lock, receipt = _receipt("0x" + "99" * 32, user["address"], VALUE_AT_QUOTE)
    rpc.logs_.append(lock)
    rpc.receipts[TX] = receipt
    assert (await scanner.scan_pipe(ETH, rpc))["unattributed"] == 1
    row = await mock_db["pgasme_test"].unattributed_locks.find_one({})
    assert "no deposit or quote carries the hook reference" in row["reason"]


async def test_the_secondary_pass_never_asks_the_router_about_a_gateway_deposit(
    client, user, registry, quoter, armed, rpc, monkeypatch  # noqa: F811
):
    """There is no cross-chain order in this story: asking would page the operator for nothing."""

    async def boom(tx_hash: str, timeout: float | None = None):
        raise AssertionError(f"a uniswap deposit has no cross-chain order to look up ({tx_hash})")

    monkeypatch.setattr("pgasme.xchain.order_ids_by_tx", boom)
    _q, dep_id = await _register(client, user, rpc)
    await workers.db().deposits.update_one({"_id": dep_id}, {"$set": {"created_at": 0.0}})
    assert await workers.xchain_secondary() == 0


# ----------------------------------------------------------------------------- the band itself


def test_the_band_is_what_the_hook_could_have_produced(registry):  # noqa: F811
    min_out = FORK_OUT * 9950 // 10000
    assert uniswap.value_band(min_out, FORK_OUT, FEE, ETH) == (LO, HI)
    # The floor is min_out ITSELF. The hook bounds `value` — what lands on Beam — so nothing
    # under min_out can be logged; the floor does not have to sit on the grid, and does not.
    assert LO == min_out and LO % ETH.grid != 0
    assert HI % ETH.grid == 0
    # The floor this band used to carry reasoned from the older rule (refuse `out < minOut`) and
    # sat a whole relayer fee plus a grid step lower — it accepted locks the hook cannot produce.
    assert ethpipe.split_amount(min_out, FEE, ETH.grid)[0] < LO
    assert JUST_UNDER_THE_FLOOR < LO <= VALUE_AT_QUOTE <= HI


def test_the_upside_tolerance_is_a_knob_and_not_a_guess(registry, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "uniswap_max_upside_bps", 0)
    min_out = FORK_OUT * 9950 // 10000
    lo, hi = uniswap.value_band(min_out, FORK_OUT, FEE, ETH)
    assert (lo, hi) == (LO, VALUE_AT_QUOTE)  # no upside allowed: the quote itself is the ceiling


def test_a_band_that_admits_nothing_refuses_instead_of_widening(registry):  # noqa: F811
    """`hi < lo` is a row contradicting ITSELF, not a band to be repaired.

    One grid step above the highest `value` the hook could ever log for this quote there is no
    amount that both clears the floor and sits under the ceiling. The old code returned
    `(lo, lo)` — so the single number a self-contradictory row would still have credited was
    chosen by the repair itself, out of numbers that disagree. It fails closed now."""
    with pytest.raises(uniswap.ContradictoryBand) as e:
        uniswap.value_band(HI + ETH.grid, FORK_OUT, FEE, ETH)
    assert str(HI + ETH.grid) in str(e.value) and str(HI) in str(e.value)
    # a ValueError, which is exactly what `scanner._sane_uniswap` catches around this call
    assert isinstance(e.value, ValueError)
    # one grid step lower is not a contradiction: it is a real (if single-point) band
    assert uniswap.value_band(HI, FORK_OUT, FEE, ETH) == (HI, HI)


async def test_a_self_contradictory_row_pages_instead_of_crediting_its_own_floor(
    client, user, registry, quoter, armed, rpc, mock_db  # noqa: F811
):
    """The refusal has to land where every other unreadable expectation lands: unattributed."""
    q, dep_id = await _register(client, user, rpc)
    d = mock_db["pgasme_test"]
    await d.deposits.update_one({"_id": dep_id}, {"$set": {"min_out_units": str(HI + ETH.grid)}})
    lock, receipt = _receipt(q["deposit_ref"], user["address"], VALUE_AT_QUOTE)
    rpc.logs_.append(lock)
    rpc.receipts[TX] = receipt
    st = await scanner.scan_pipe(ETH, rpc)
    assert st["unattributed"] == 1 and st["locked"] == 0
    assert (await d.deposits.find_one({"_id": dep_id}))["status"] == "submitted"
    row = await d.unattributed_locks.find_one({})
    assert "cannot check the amount against the quote" in row["reason"]
    assert "ContradictoryBand" in row["reason"]
    assert await d.entries.count_documents({}) == 0  # nobody was credited


def test_indexes_the_reference_is_matched_by_exist(registry):  # noqa: F811
    assert scanner.DEPOSIT_REF_INDEX == "uniq_deposit_ref"


async def test_ensure_indexes_creates_the_reference_guards(mock_db):
    await scanner.ensure_indexes()
    names = [i["name"] for i in await mock_db["pgasme_test"].deposits.list_indexes().to_list(20)]
    assert scanner.DEPOSIT_REF_INDEX in names and scanner.DEPOSIT_HASH_INDEX in names


def test_the_registry_is_json_the_operator_can_paste(registry):  # noqa: F811
    """The .env.example row has to be readable by the same parser the box uses."""
    rows = json.loads(json.dumps([pool_row()]))
    settings.uniswap_pools = json.dumps(rows)
    uniswap.clear_cache()
    assert uniswap.route_for(USDC, "ETH") is not None
    assert QUOTER and time.time() > 0  # the fixtures above are what pin the addresses
