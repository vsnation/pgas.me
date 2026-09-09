"""POST /v1/quote against a recorded DLN response (unarmed / armed / moved), and deposits."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from conftest import DLN_ESTIMATE, PUBKEY, USDC_ARB

from pgasme import assets, dln, ethpipe
from pgasme.config import settings

Q = {"src_chain_id": 42161, "src_token": USDC_ARB, "amount": "10000000", "target_asset": "ETH"}


class FakeDln:
    """create_tx that answers like the recorded API: `auto` → the estimate; an explicit amount +
    hook → that amount echoed back, with a configurable recommendedAmount."""

    def __init__(self, recommended: dict[int, int] | None = None, moved: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.recommended = recommended or {}
        self.moved = moved

    async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        body = copy.deepcopy(DLN_ESTIMATE)
        out = body["estimation"]["dstChainTokenOut"]
        if params["dstChainTokenOut"] != "0x0000000000000000000000000000000000000000":
            out.update(
                {
                    "address": params["dstChainTokenOut"],
                    "symbol": "DAI",
                    "amount": "9190021264249749267",
                    "recommendedAmount": "9190021264249749267",
                }
            )
        if params["dstChainTokenOutAmount"] != "auto":
            amt = int(params["dstChainTokenOutAmount"]) + (1 if self.moved else 0)
            out["amount"] = str(amt)
            out["recommendedAmount"] = str(
                self.recommended.get(int(params["dstChainTokenOutAmount"]), amt)
            )
            body["orderId"] = "0x" + "77" * 32
        return body


@pytest.fixture(autouse=True)
def low_floor(monkeypatch):
    """The recorded estimate is 10 USDC ≈ 0.0038 ETH, under the 0.02 ETH product floor."""
    monkeypatch.setattr(settings, "min_deposit_wei", 10**15)


@pytest.fixture(autouse=True)
def dln_index_offline(monkeypatch):
    """POST /v1/deposits resolves the hash against DLN's index. No test may reach the live API
    for that: the default answer here is "not indexed yet", which is what a freshly signed
    transaction really looks like."""

    async def order_ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return []

    monkeypatch.setattr(dln, "order_ids_by_tx", order_ids)
    return order_ids


@pytest.fixture
def fake_dln(monkeypatch):
    f = FakeDln()
    monkeypatch.setattr(dln, "create_tx", f)
    return f


async def test_unarmed_quote_is_an_estimate_only(client, user, fake_dln, mock_db):
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "dln"  # a cross-chain quote is unchanged by the same-chain modes
    assert (
        body["armed"] is False
        and "tx" not in body
        and "approval" not in body
        and "order_id" not in body
    )
    assert "not armed" in body["note"]
    est = body["estimate"]
    assert est["out_units"] == "3774812168855201" and est["value_units"] == "3774710000000000"
    assert int(est["value_units"]) + int(est["relayer_fee_units"]) == int(est["out_units"])
    assert est["out_groth"] == 377_471 and est["src"]["symbol"] == "USDC" and est["eta_s"] > 0
    assert est["dln_fees"]["fix_fee"] == "1000000000000000"
    p = fake_dln.calls[0]
    assert (
        p["dstChainId"] == 1
        and p["dstChainTokenOut"] == "0x0000000000000000000000000000000000000000"
    )
    assert (
        p["dstChainTokenOutRecipient"]
        == user["address"]
        == p["senderAddress"]
        == p["dstChainOrderAuthorityAddress"]
    )
    assert (
        p["srcChainId"] == 42161
        and p["srcChainTokenInAmount"] == "10000000"
        and p["dstChainTokenOutAmount"] == "auto"
    )
    assert p["metadata"].startswith("0x") and len(p["metadata"]) == 12 and "dlnHook" not in p
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert (
        q["armed"] is False
        and q["metadata"] == p["metadata"]
        and q["account_id"] == user["account_id"]
    )


async def test_armed_quote_carries_tx_approval_and_a_hook_for_our_pubkey(
    client, user, fake_dln, armed_eth, mock_db
):
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "dln" and "swap_tx" not in body and "next" not in body
    assert body["armed"] is True and body["order_id"] == "0x" + "77" * 32 and "note" not in body
    assert body["tx"] == {
        "chain_id": 42161,
        "to": DLN_ESTIMATE["tx"]["to"],
        "data": DLN_ESTIMATE["tx"]["data"],
        "value": "1000000000000000",
    }
    assert body["approval"] == {
        "chain_id": 42161,
        "token": USDC_ARB,
        "spender": DLN_ESTIMATE["tx"]["to"],
        "amount": "10000000",
    }
    assert len(fake_dln.calls) == 2
    second = fake_dln.calls[1]
    assert second["dstChainTokenOutAmount"] == "3774812168855201"
    hook = json.loads(second["dlnHook"])
    assert (
        hook["type"] == "evm_transaction_call" and hook["data"]["to"] == assets.ASSETS["ETH"].pipe
    )
    assert hook["data"]["gas"] == settings.hook_gas
    call = ethpipe.decode_send_funds(hook["data"]["calldata"])
    assert call["pubkey"] == PUBKEY and call["value"] + call["relayer_fee"] == 3774812168855201
    assert call["value"] % 10**10 == 0
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert q["armed"] and q["hook_calldata"] == hook["data"]["calldata"] and q["pubkey"] == PUBKEY


async def test_asset_without_a_pubkey_quotes_unarmed_even_when_armed(
    client, user, fake_dln, armed_eth, monkeypatch
):
    async def px(force=False):
        return {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}

    monkeypatch.setattr(assets, "usd_prices", px)
    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)
    r = await client.post("/v1/quote", json={**Q, "target_asset": "DAI"}, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["armed"] is False and "no Beam pipe pubkey configured for DAI" in body["note"]
    assert body["target_asset"] == "DAI" and int(body["estimate"]["value_units"]) % 10**10 == 0
    monkeypatch.setattr(settings, "beam_pipe_pubkey_dai", "02" + "dd" * 32)
    r = await client.post("/v1/quote", json={**Q, "target_asset": "DAI"}, headers=user["headers"])
    assert r.json()["armed"] is True
    hook = json.loads(fake_dln.calls[-1]["dlnHook"])
    assert hook["data"]["to"] == assets.ASSETS["DAI"].pipe
    assert ethpipe.decode_send_funds(hook["data"]["calldata"])["pubkey"] == "02" + "dd" * 32


async def test_quote_moved_is_409(client, user, monkeypatch, armed_eth):
    monkeypatch.setattr(dln, "create_tx", FakeDln(moved=True))
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 409 and "moved" in r.json()["detail"]


async def test_lower_recommendation_requotes_once_on_it(client, user, monkeypatch, armed_eth):
    first = 3774812168855201
    rec = first - 81_000_000_000_000  # what the recorded hooked call showed: the hook gas priced in
    fake = FakeDln(recommended={first: rec})
    monkeypatch.setattr(dln, "create_tx", fake)
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 200, r.text
    assert len(fake.calls) == 3 and fake.calls[2]["dstChainTokenOutAmount"] == str(rec)
    est = r.json()["estimate"]
    assert est["out_units"] == str(rec)
    call = ethpipe.decode_send_funds(json.loads(fake.calls[2]["dlnHook"])["data"]["calldata"])
    assert call["value"] + call["relayer_fee"] == rec and est["value_units"] == str(call["value"])


async def test_min_deposit_eth(client, user, monkeypatch):
    fake = FakeDln()
    monkeypatch.setattr(dln, "create_tx", fake)
    monkeypatch.setattr(settings, "min_deposit_wei", 10**16)  # 0.01 ETH > the 0.0038 estimate
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 400 and "minimum deposit" in r.json()["detail"]


async def test_min_deposit_dai_via_prices_and_price_outage(client, user, fake_dln, monkeypatch):
    async def px(force=False):
        return {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)
    monkeypatch.setattr(settings, "min_deposit_wei", 2 * 10**16)
    r = await client.post("/v1/quote", json={**Q, "target_asset": "DAI"}, headers=user["headers"])
    assert r.status_code == 400 and "minimum deposit" in r.json()["detail"]  # 9.19 DAI < 0.02 ETH

    async def down(force=False):
        raise assets.PriceError("coingecko HTTP 429")

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", down)
    r = await client.post("/v1/quote", json={**Q, "target_asset": "DAI"}, headers=user["headers"])
    assert r.status_code == 200 and "price unavailable" in r.json()["note"]


async def test_quote_input_validation(client, user, fake_dln):
    h = user["headers"]
    assert (
        await client.post("/v1/quote", json={**Q, "target_asset": "USDT"}, headers=h)
    ).status_code == 400
    assert (
        await client.post("/v1/quote", json={**Q, "src_token": "nope"}, headers=h)
    ).status_code == 400
    assert (await client.post("/v1/quote", json={**Q, "amount": "0"}, headers=h)).status_code == 400
    assert (
        await client.post("/v1/quote", json={**Q, "amount": "1e6"}, headers=h)
    ).status_code == 400
    assert (
        await client.post("/v1/quote", json={**Q, "sender": "0x" + "11" * 20}, headers=h)
    ).status_code == 400
    assert (
        await client.post("/v1/quote", json={**Q, "src_chain_id": 999999}, headers=h)
    ).status_code == 400
    assert (
        await client.post("/v1/quote", json={**Q, "sender": user["address"].lower()}, headers=h)
    ).status_code == 200


async def test_dln_refusal_and_outage_are_reported_verbatim(client, user, monkeypatch):
    async def refused(params):
        raise dln.DlnError(
            "COMPLIANCE_ADDRESS_BLOCKED: liquidity at your address is flagged", status=400
        )

    monkeypatch.setattr(dln, "create_tx", refused)
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 400 and "COMPLIANCE_ADDRESS_BLOCKED" in r.json()["detail"]

    async def down(params):
        raise dln.DlnError("https://dln.debridge.finance/v1.0/dln/order/create-tx: ConnectError")

    monkeypatch.setattr(dln, "create_tx", down)
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 502


async def test_hook_failed_simulation_is_409(client, user, monkeypatch, armed_eth):
    async def create_tx(params):
        if "dlnHook" in params:
            raise dln.DlnError(
                "HOOK_FAILED: hook reverted in simulation", status=400, error_id="HOOK_FAILED"
            )
        return copy.deepcopy(DLN_ESTIMATE)

    monkeypatch.setattr(dln, "create_tx", create_tx)
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 409
    assert (
        r.json()["detail"]
        == "deBridge could not simulate the bridge call — try again or a different amount"
    )


async def test_deposit_needs_an_armed_unexpired_own_quote(client, user, fake_dln, mock_db):
    h = user["headers"]
    qid = (await client.post("/v1/quote", json=Q, headers=h)).json()["quote_id"]
    tx = "0x" + "ab" * 32
    r = await client.post("/v1/deposits", json={"quote_id": qid, "src_tx_hash": tx}, headers=h)
    assert r.status_code == 409 and "estimate only" in r.json()["detail"]
    assert (
        await client.post(
            "/v1/deposits", json={"quote_id": "not-a-known-quote", "src_tx_hash": tx}, headers=h
        )
    ).status_code == 404
    assert (
        await client.post(
            "/v1/deposits", json={"quote_id": qid, "src_tx_hash": "0x" + "zz" * 32}, headers=h
        )
    ).status_code == 400
    await mock_db["pgasme_test"].quotes.update_one(
        {"_id": qid}, {"$set": {"armed": True, "expires_at": 1.0}}
    )
    r = await client.post("/v1/deposits", json={"quote_id": qid, "src_tx_hash": tx}, headers=h)
    assert r.status_code == 409 and "expired" in r.json()["detail"]


async def test_deposit_from_an_armed_quote(client, user, fake_dln, armed_eth, mock_db):
    h = user["headers"]
    quote = (await client.post("/v1/quote", json=Q, headers=h)).json()
    tx = "0x" + "AB" * 32
    r = await client.post(
        "/v1/deposits", json={"quote_id": quote["quote_id"], "src_tx_hash": tx}, headers=h
    )
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    dep_id = r.json()["deposit_id"]
    again = await client.post(
        "/v1/deposits", json={"quote_id": quote["quote_id"], "src_tx_hash": tx}, headers=h
    )
    assert again.json()["deposit_id"] == dep_id  # idempotent on the hash
    d = (await client.get(f"/v1/deposits/{dep_id}", headers=h)).json()
    assert d["asset"] == "ETH" and d["status"] == "submitted" and d["order_id"] == quote["order_id"]
    assert d["mode"] == "dln"
    assert d["src"] == {"chain_id": 42161, "token": USDC_ARB, "amount": "10000000"}
    assert (
        d["eth"]["value_units"] == quote["estimate"]["value_units"]
        and d["src_tx_hash"] == tx.lower()
    )
    assert "account_id" not in d and "pubkey" not in d
    acct = (await client.get("/v1/account", headers=h)).json()
    assert acct["balances"]["ETH"]["pending"] == quote["estimate"]["out_groth"]
    assert (await client.get("/v1/deposits/other", headers=h)).status_code == 404
    ev = await mock_db["pgasme_test"].events.find_one({"kind": "deposit_submitted"})
    assert ev and ev["deposit_id"] == dep_id
