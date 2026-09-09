"""POST /v1/quote when the source chain IS Ethereum: mode "direct" (the deposit is the pipe call
itself) and mode "swap" (DLN's single-chain endpoints into the user's own wallet), plus what
/v1/deposits does with each. DLN's order API must never be reached from chain 1 — that is the
SAME_SOURCE_AND_DESTINATION_CHAINS refusal this whole path exists to avoid."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from conftest import DLN_SWAP_ESTIMATION, DLN_SWAP_TX, PUBKEY, USDC_ETH

from pgasme import assets, dln, ethpipe
from pgasme.assets import ASSETS
from pgasme.config import settings

ZERO = "0x0000000000000000000000000000000000000000"
ETH = ASSETS["ETH"]
DAI = ASSETS["DAI"]
HALF_ETH_TENTH = 50_000_000_000_000_000  # 0.05 ETH — clears the 0.02 ETH product floor
PRICES = {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}


def direct(**over: Any) -> dict[str, Any]:
    return {
        "src_chain_id": 1,
        "src_token": ZERO,
        "amount": str(HALF_ETH_TENTH),
        "target_asset": "ETH",
        **over,
    }


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    """No same-chain test may reach CoinGecko; `usd` is best effort and priced from here."""

    async def px(force: bool = False) -> dict[str, float]:
        return dict(PRICES)

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)
    return px


@pytest.fixture(autouse=True)
def no_dln_orders(monkeypatch):
    """create-tx is the endpoint that refuses same-chain quotes; a call from here is the bug."""

    async def boom(params: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(
            f"the DLN ORDER api must not be called for a same-chain quote: {params}"
        )

    monkeypatch.setattr(dln, "create_tx", boom)
    return boom


class FakeSwap:
    """dln.chain_estimation / chain_transaction answering with the recorded live bodies."""

    def __init__(self) -> None:
        self.est_calls: list[dict[str, Any]] = []
        self.tx_calls: list[dict[str, Any]] = []

    async def estimation(self, params: dict[str, Any]) -> dict[str, Any]:
        self.est_calls.append(params)
        return copy.deepcopy(DLN_SWAP_ESTIMATION["estimation"])

    async def transaction(self, params: dict[str, Any]) -> dict[str, Any]:
        self.tx_calls.append(params)
        return copy.deepcopy(DLN_SWAP_TX)


@pytest.fixture
def fake_swap(monkeypatch):
    f = FakeSwap()
    monkeypatch.setattr(dln, "chain_estimation", f.estimation)
    monkeypatch.setattr(dln, "chain_transaction", f.transaction)
    return f


@pytest.fixture
def armed_dai(monkeypatch):
    monkeypatch.setattr(settings, "ingress_armed", True)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_dai", "02" + "dd" * 32)
    return "02" + "dd" * 32


# ----------------------------------------------------------------------------- the DLN client


async def test_single_chain_client_reads_the_recorded_shapes(monkeypatch):
    seen: list[tuple[str, dict[str, Any]]] = []

    async def get(path: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        seen.append((path, params or {}))
        return copy.deepcopy(DLN_SWAP_ESTIMATION if path == "chain/estimation" else DLN_SWAP_TX)

    monkeypatch.setattr(dln, "_get", get)
    est = await dln.chain_estimation({"chainId": 1})
    assert dln.swap_out_amount(est) == 1999122712146377 and est["protocolFee"] == "1600578632623"
    body = await dln.chain_transaction({"chainId": 1})
    assert (
        body["tx"]["to"] == DLN_SWAP_TX["tx"]["to"]
        and dln.swap_out_amount(body) == 1999122712146377
    )
    assert [p for p, _ in seen] == ["chain/estimation", "chain/transaction"]

    async def empty(path: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        return {"estimation": {}} if path == "chain/estimation" else {"orderId": "0x00"}

    monkeypatch.setattr(dln, "_get", empty)
    with pytest.raises(dln.DlnError):
        await dln.chain_estimation({})
    with pytest.raises(dln.DlnError):  # "no transaction" is an error, never an empty result
        await dln.chain_transaction({})


# ----------------------------------------------------------------------------- mode "direct"


async def test_direct_eth_unarmed_is_an_estimate_and_calls_no_dln(client, user, mock_db):
    r = await client.post("/v1/quote", json=direct(), headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "direct" and body["armed"] is False
    assert "tx" not in body and "approval" not in body and "order_id" not in body
    assert "not armed" in body["note"]
    est = body["estimate"]
    assert est["out_units"] == str(HALF_ETH_TENTH)  # nothing is bridged: out == in
    assert int(est["value_units"]) + int(est["relayer_fee_units"]) == HALF_ETH_TENTH
    assert int(est["value_units"]) % 10**10 == 0 and est["out_groth"] == 4_999_990
    assert est["src"] == {
        "chain_id": 1,
        "token": ZERO,
        "symbol": "ETH",
        "decimals": 18,
        "amount": str(HALF_ETH_TENTH),
    }
    assert est["usd"] == pytest.approx(0.05 * PRICES["ETH"])
    assert est["eta_s"] == settings.lock_confirmations * 12 + 120 and "dln_fees" not in est
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert q["mode"] == "direct" and q["armed"] is False and q.get("order_id") is None
    assert q["src"]["chain_id"] == 1 and q["value_groth"] == est["out_groth"]


async def test_direct_eth_armed_hands_back_the_pipe_call_itself(client, user, armed_eth, mock_db):
    r = await client.post("/v1/quote", json=direct(), headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "direct" and body["armed"] is True and "note" not in body
    assert "approval" not in body and "order_id" not in body  # native ETH: nothing to approve
    tx = body["tx"]
    assert tx["chain_id"] == 1 and tx["to"] == ETH.pipe and tx["value"] == str(HALF_ETH_TENTH)
    call = ethpipe.decode_send_funds(tx["data"])
    est = body["estimate"]
    assert call["pubkey"] == PUBKEY == armed_eth
    assert call["value"] == int(est["value_units"]) and call["relayer_fee"] == int(
        est["relayer_fee_units"]
    )
    # EthPipe requires msg.value == value + relayerFee EXACTLY
    assert call["value"] + call["relayer_fee"] == int(tx["value"]) == HALF_ETH_TENTH
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert q["mode"] == "direct" and q["pubkey"] == PUBKEY and q["hook_calldata"] == tx["data"]
    assert q["tx"] == tx and q["approval"] is None and q.get("order_id") is None


async def test_direct_dai_armed_approves_the_pipe_for_the_exact_amount(client, user, armed_dai):
    amount = 100 * 10**18
    r = await client.post(
        "/v1/quote",
        json=direct(src_token=DAI.token, amount=str(amount), target_asset="DAI"),
        headers=user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "direct" and body["armed"] is True and body["target_asset"] == "DAI"
    assert body["tx"] == {
        "chain_id": 1,
        "to": DAI.pipe,
        "data": body["tx"]["data"],
        "value": "0",  # an ERC-20 pipe pulls the tokens; the call carries no ether
    }
    assert body["approval"] == {
        "chain_id": 1,
        "token": DAI.token,
        "spender": DAI.pipe,
        "amount": str(amount),
    }
    call = ethpipe.decode_send_funds(body["tx"]["data"])
    assert call["pubkey"] == armed_dai and call["value"] + call["relayer_fee"] == amount
    assert call["value"] % 10**10 == 0
    assert body["estimate"]["usd"] == pytest.approx(100.0)


async def test_direct_below_the_floor_and_below_the_relayer_fee_are_refused(client, user):
    h = user["headers"]
    r = await client.post("/v1/quote", json=direct(amount=str(10**16)), headers=h)  # 0.01 ETH
    assert r.status_code == 400 and "minimum deposit" in r.json()["detail"]
    r = await client.post("/v1/quote", json=direct(amount="1000"), headers=h)
    assert r.status_code == 400 and "amount too small" in r.json()["detail"]


async def test_direct_usd_is_blank_when_the_price_is_out(client, user, monkeypatch):
    async def down(force: bool = False) -> dict[str, float]:
        raise assets.PriceError("coingecko HTTP 429")

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", down)
    r = await client.post("/v1/quote", json=direct(), headers=user["headers"])
    assert r.status_code == 200 and r.json()["estimate"]["usd"] is None


# ----------------------------------------------------------------------------- mode "swap"


async def test_swap_usdc_to_eth_returns_a_swap_tx_and_the_next_quote(
    client, user, fake_swap, mock_db
):
    r = await client.post(
        "/v1/quote",
        json=direct(src_token=USDC_ETH, amount="5000000"),
        headers=user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "swap" and body["armed"] is False  # informational: ingress is not armed
    assert "tx" not in body and "order_id" not in body
    assert body["swap_tx"] == {
        "chain_id": 1,
        "to": DLN_SWAP_TX["tx"]["to"],
        "data": DLN_SWAP_TX["tx"]["data"],
        "value": "0",
    }
    # no allowanceTarget came back: approve the swap target itself, for the input amount
    assert body["approval"] == {
        "chain_id": 1,
        "token": USDC_ETH,
        "spender": DLN_SWAP_TX["tx"]["to"],
        "amount": "5000000",
    }
    assert body["next"] == {"src_chain_id": 1, "src_token": ZERO, "amount": "1999122712146377"}
    est = body["estimate"]
    assert est["out_units"] == "1999122712146377" and est["src"]["symbol"] == "USDC"
    assert int(est["value_units"]) + int(est["relayer_fee_units"]) == 1999122712146377
    assert int(est["value_units"]) % 10**10 == 0
    assert est["usd"] == 4.991652 and est["eta_s"] == 120 + settings.lock_confirmations * 12 + 120
    assert est["dln_fees"]["protocol_fee"] == "1600578632623"
    assert est["dln_fees"]["estimated_tx_fee"] == "439326826112330"
    assert est["dln_fees"]["min_out_units"] == "1993120542274040"
    assert [c["type"] for c in est["dln_fees"]["costs"]] == [
        "SingleChainSwapProtocolFee",
        "SingleChainSwapEstimatedSlippage",
    ]
    assert "two steps" in body["note"]
    # both single-chain endpoints, the output token is ours, the recipient is the USER
    e, t = fake_swap.est_calls[0], fake_swap.tx_calls[0]
    assert e["chainId"] == 1 and e["tokenIn"] == USDC_ETH and e["tokenInAmount"] == "5000000"
    assert e["tokenOut"] == ZERO and e["tokenOutAmount"] == "auto" and "tokenOutRecipient" not in e
    assert t["tokenOutRecipient"] == user["address"] == t["senderAddress"]
    assert t["tokenOut"] == ZERO and t["referralCode"] == settings.dln_referral_code
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert q["mode"] == "swap" and q["swap_tx"] == body["swap_tx"] and q["next"] == body["next"]


async def test_swap_from_native_eth_needs_no_approval(client, user, fake_swap, armed_eth):
    r = await client.post(
        "/v1/quote",
        json=direct(src_token=ZERO, amount=str(10**16), target_asset="DAI"),
        headers=user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "swap" and "approval" not in body
    assert fake_swap.est_calls[0]["tokenOut"] == DAI.token  # the DAI pipe's own token
    assert body["next"]["src_token"] == DAI.token
    assert body["armed"] is False and "no Beam pipe pubkey configured for DAI" in body["note"]


async def test_swap_refusal_from_dln_is_reported(client, user, monkeypatch):
    async def refused(params: dict[str, Any]) -> dict[str, Any]:
        raise dln.DlnError("SOME_ERROR: no route", status=400)

    monkeypatch.setattr(dln, "chain_estimation", refused)
    r = await client.post(
        "/v1/quote", json=direct(src_token=USDC_ETH, amount="5000000"), headers=user["headers"]
    )
    assert r.status_code == 400 and "SOME_ERROR" in r.json()["detail"]


# ----------------------------------------------------------------------------- deposits


async def test_a_direct_quote_can_be_registered_as_a_deposit(
    client, user, armed_eth, mock_db, rpc
):
    h = user["headers"]
    quote = (await client.post("/v1/quote", json=direct(), headers=h)).json()
    tx = "0x" + "CD" * 32
    # the pipe call itself, sent by the signed-in wallet: this is what makes the hash a claim
    rpc.txs[tx.lower()] = {
        "from": user["address"],
        "to": ETH.pipe,
        "input": quote["tx"]["data"],
        "value": hex(HALF_ETH_TENTH),
    }
    r = await client.post(
        "/v1/deposits", json={"quote_id": quote["quote_id"], "src_tx_hash": tx}, headers=h
    )
    assert r.status_code == 200, r.text
    dep_id = r.json()["deposit_id"]
    d = (await client.get(f"/v1/deposits/{dep_id}", headers=h)).json()
    assert d["mode"] == "direct" and d["order_id"] is None and d["status"] == "submitted"
    assert d["src"] == {"chain_id": 1, "token": ZERO, "amount": str(HALF_ETH_TENTH)}
    assert d["src_tx_hash"] == tx.lower()
    assert d["eth"]["value_units"] == quote["estimate"]["value_units"]
    acct = (await client.get("/v1/account", headers=h)).json()
    assert acct["deposits"][0]["mode"] == "direct"
    assert acct["balances"]["ETH"]["pending"] == quote["estimate"]["out_groth"]


async def test_a_swap_quote_is_not_a_deposit(client, user, fake_swap):
    h = user["headers"]
    quote = (
        await client.post("/v1/quote", json=direct(src_token=USDC_ETH, amount="5000000"), headers=h)
    ).json()
    r = await client.post(
        "/v1/deposits",
        json={"quote_id": quote["quote_id"], "src_tx_hash": "0x" + "ef" * 32},
        headers=h,
    )
    assert r.status_code == 400 and r.json()["detail"] == (
        "a swap is not a deposit — quote again with the target token after it lands"
    )


async def test_a_deposit_written_before_modes_existed_reads_as_dln(client, user, mock_db):
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "old1",
            "account_id": user["account_id"],
            "asset": "ETH",
            "status": "credited",
            "src": {"chain_id": 42161, "token": ZERO, "amount": "1"},
            "quote_id": "q0",
            "eth": {"value_units": "1", "relayer_fee_units": "1"},
            "value_groth": 1,
            "created_at": 0.0,
            "updated_at": 0.0,
        }
    )
    d = (await client.get("/v1/deposits/old1", headers=user["headers"])).json()
    assert d["mode"] == "dln"
