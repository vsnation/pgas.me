"""POST /v1/quote (the estimate — ONE router call) and POST /v1/quote/{id}/arm (the order),
against a recorded router response (unarmed / armed / moved), plus deposits."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from conftest import PUBKEY, USDC_ARB, XCHAIN_ESTIMATE
from eth_account import Account as EthAccount

from pgasme import assets, ethpipe, xchain
from pgasme.config import ROUTER_HOOK_PARAM, settings

Q = {"src_chain_id": 42161, "src_token": USDC_ARB, "amount": "10000000", "target_asset": "ETH"}


class FakeRouter:
    """create_tx that answers like the recorded API: `auto` → the estimate; an explicit amount +
    hook → that amount echoed back, with a configurable recommendedAmount."""

    def __init__(self, recommended: dict[int, int] | None = None, moved: bool = False):
        self.calls: list[dict[str, Any]] = []
        self.recommended = recommended or {}
        self.moved = moved

    async def __call__(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        body = copy.deepcopy(XCHAIN_ESTIMATE)
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
def router_index_offline(monkeypatch):
    """POST /v1/deposits resolves the hash against the router's index. No test may reach the live API
    for that: the default answer here is "not indexed yet", which is what a freshly signed
    transaction really looks like."""

    async def order_ids(tx_hash: str, timeout: float | None = None) -> list[str]:
        return []

    monkeypatch.setattr(xchain, "order_ids_by_tx", order_ids)
    return order_ids


@pytest.fixture
def fake_xchain(monkeypatch):
    f = FakeRouter()
    monkeypatch.setattr(xchain, "create_tx", f)
    return f


async def arm(client, user, quote_id):
    return await client.post(f"/v1/quote/{quote_id}/arm", headers=user["headers"])


async def quote_and_arm(client, user, body=None):
    """The two calls the web makes: price it, then build the transaction on the Deposit click."""
    q = (await client.post("/v1/quote", json=body or Q, headers=user["headers"])).json()
    r = await arm(client, user, q["quote_id"])
    assert r.status_code == 200, r.text
    return q, r.json()


async def test_unarmed_quote_is_an_estimate_only(client, user, fake_xchain, mock_db):
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "xchain"  # a cross-chain quote is unchanged by the same-chain modes
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
    assert est["route_fees"]["fix_fee"] == "1000000000000000"
    p = fake_xchain.calls[0]
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
    assert p["metadata"].startswith("0x") and len(p["metadata"]) == 12 and ROUTER_HOOK_PARAM not in p
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert (
        q["armed"] is False
        and q["metadata"] == p["metadata"]
        and q["account_id"] == user["account_id"]
    )


async def test_the_estimate_is_one_router_call_and_carries_no_transaction(
    client, user, fake_xchain, armed_eth, mock_db
):
    """The whole point of the split: what the user waits for is ONE upstream call."""
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "xchain" and body["armed"] is True  # armed == "it CAN be armed"
    assert "tx" not in body and "order_id" not in body and "approval" not in body
    assert len(fake_xchain.calls) == 1 and ROUTER_HOOK_PARAM not in fake_xchain.calls[0]
    assert fake_xchain.calls[0]["dstChainTokenOutAmount"] == "auto"
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert q.get("tx") is None and q.get("order_id") is None and q.get("hook") is None


async def test_arm_builds_the_order_and_stores_it(client, user, fake_xchain, armed_eth, mock_db):
    quote, body = await quote_and_arm(client, user)
    assert body["quote_id"] == quote["quote_id"] and "swap_tx" not in body and "next" not in body
    assert body["armed"] is True and body["order_id"] == "0x" + "77" * 32
    assert body["estimate"]["out_units"] == quote["estimate"]["out_units"]
    assert body["expires_at"] == quote["expires_at"]
    assert body["tx"] == {
        "chain_id": 42161,
        "to": XCHAIN_ESTIMATE["tx"]["to"],
        "data": XCHAIN_ESTIMATE["tx"]["data"],
        "value": "1000000000000000",
    }
    assert body["approval"] == {
        "chain_id": 42161,
        "token": USDC_ARB,
        "spender": XCHAIN_ESTIMATE["tx"]["to"],
        "amount": "10000000",
    }
    assert len(fake_xchain.calls) == 2  # the estimate, then the hooked order
    second = fake_xchain.calls[1]
    assert second["dstChainTokenOutAmount"] == "3774812168855201"
    hook = json.loads(second[ROUTER_HOOK_PARAM])
    assert (
        hook["type"] == "evm_transaction_call" and hook["data"]["to"] == assets.ASSETS["ETH"].pipe
    )
    assert hook["data"]["gas"] == settings.hook_gas
    call = ethpipe.decode_send_funds(hook["data"]["calldata"])
    assert call["pubkey"] == PUBKEY and call["value"] + call["relayer_fee"] == 3774812168855201
    assert call["value"] % 10**10 == 0
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": body["quote_id"]})
    assert q["armed"] and q["hook_calldata"] == hook["data"]["calldata"] and q["pubkey"] == PUBKEY
    assert q["tx"] == body["tx"] and q["order_id"] == body["order_id"] and q["hook"] == hook
    assert q["route_metadata"] == XCHAIN_ESTIMATE["order"]["metadata"]


async def test_arm_is_idempotent_while_the_order_is_fresh(client, user, fake_xchain, armed_eth):
    quote, first = await quote_and_arm(client, user)
    again = await arm(client, user, quote["quote_id"])
    assert again.status_code == 200 and again.json() == first
    assert len(fake_xchain.calls) == 2  # the second click did NOT place a second order


async def test_arm_rebuilds_once_the_stored_order_is_stale(
    client, user, fake_xchain, armed_eth, mock_db, monkeypatch
):
    from pgasme.routers import quote as quote_router

    quote, _first = await quote_and_arm(client, user)
    monkeypatch.setattr(quote_router, "ARM_FRESH_S", 0.0)  # the router's validity window has passed
    again = await arm(client, user, quote["quote_id"])
    assert again.status_code == 200 and len(fake_xchain.calls) == 3
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": quote["quote_id"]})
    assert q["arm_tries"] == 2 and q["tx"] == again.json()["tx"]


async def test_arm_refuses_an_expired_or_unknown_or_unarmed_quote(
    client, user, fake_xchain, armed_eth, mock_db, monkeypatch
):
    h = user["headers"]
    qid = (await client.post("/v1/quote", json=Q, headers=h)).json()["quote_id"]
    assert (await client.post("/v1/quote/nope/arm", headers=h)).status_code == 404
    from conftest import sign_in

    other = await sign_in(client, EthAccount.create())
    assert (await arm(client, other, qid)).status_code == 404  # not this account's quote
    await mock_db["pgasme_test"].quotes.update_one({"_id": qid}, {"$set": {"expires_at": 1.0}})
    r = await arm(client, user, qid)
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    # and a quote for an asset with no pipe pubkey can never be armed at all
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", "")
    fresh = (await client.post("/v1/quote", json=Q, headers=h)).json()
    assert fresh["armed"] is False
    r = await arm(client, user, fresh["quote_id"])
    assert r.status_code == 409 and "no Beam pipe pubkey" in r.json()["detail"]


async def test_arming_one_quote_forever_is_refused(client, user, fake_xchain, armed_eth, monkeypatch):
    from pgasme.routers import quote as quote_router

    monkeypatch.setattr(quote_router, "ARM_FRESH_S", 0.0)
    monkeypatch.setattr(quote_router, "ARM_MAX_TRIES", 2)
    qid = (await client.post("/v1/quote", json=Q, headers=user["headers"])).json()["quote_id"]
    assert (await arm(client, user, qid)).status_code == 200
    assert (await arm(client, user, qid)).status_code == 200
    r = await arm(client, user, qid)
    assert r.status_code == 429 and "too many times" in r.json()["detail"]


async def test_asset_without_a_pubkey_quotes_unarmed_even_when_armed(
    client, user, fake_xchain, armed_eth, monkeypatch
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
    assert (await arm(client, user, r.json()["quote_id"])).status_code == 200
    hook = json.loads(fake_xchain.calls[-1][ROUTER_HOOK_PARAM])
    assert hook["data"]["to"] == assets.ASSETS["DAI"].pipe
    assert ethpipe.decode_send_funds(hook["data"]["calldata"])["pubkey"] == "02" + "dd" * 32


async def test_quote_moved_is_409(client, user, monkeypatch, armed_eth):
    """The estimate still prices; the ORDER is where a moved amount is refused."""
    monkeypatch.setattr(xchain, "create_tx", FakeRouter(moved=True))
    q = (await client.post("/v1/quote", json=Q, headers=user["headers"])).json()
    r = await arm(client, user, q["quote_id"])
    assert r.status_code == 409 and "moved" in r.json()["detail"]


async def test_lower_recommendation_requotes_once_on_it(client, user, monkeypatch, armed_eth):
    first = 3774812168855201
    rec = first - 81_000_000_000_000  # what the recorded hooked call showed: the hook gas priced in
    fake = FakeRouter(recommended={first: rec})
    monkeypatch.setattr(xchain, "create_tx", fake)
    _q, armed = await quote_and_arm(client, user)
    assert len(fake.calls) == 3 and fake.calls[2]["dstChainTokenOutAmount"] == str(rec)
    est = armed["estimate"]
    assert est["out_units"] == str(rec)
    call = ethpipe.decode_send_funds(json.loads(fake.calls[2][ROUTER_HOOK_PARAM])["data"]["calldata"])
    assert call["value"] + call["relayer_fee"] == rec and est["value_units"] == str(call["value"])


async def test_min_deposit_eth(client, user, monkeypatch):
    fake = FakeRouter()
    monkeypatch.setattr(xchain, "create_tx", fake)
    monkeypatch.setattr(settings, "min_deposit_wei", 10**16)  # 0.01 ETH > the 0.0038 estimate
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 400 and "minimum deposit" in r.json()["detail"]


async def test_min_deposit_dai_via_prices_and_price_outage(client, user, fake_xchain, monkeypatch):
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


async def test_quote_input_validation(client, user, fake_xchain):
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


async def test_router_refusal_and_outage_are_reported_verbatim(client, user, monkeypatch):
    async def refused(params):
        raise xchain.XchainError(
            "COMPLIANCE_ADDRESS_BLOCKED: liquidity at your address is flagged", status=400
        )

    monkeypatch.setattr(xchain, "create_tx", refused)
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 400 and "COMPLIANCE_ADDRESS_BLOCKED" in r.json()["detail"]

    async def down(params):
        raise xchain.XchainError("https://router.example/v1.0/order/create-tx: ConnectError")

    monkeypatch.setattr(xchain, "create_tx", down)
    r = await client.post("/v1/quote", json=Q, headers=user["headers"])
    assert r.status_code == 502


async def test_hook_failed_simulation_is_409(client, user, monkeypatch, armed_eth):
    async def create_tx(params):
        if ROUTER_HOOK_PARAM in params:
            raise xchain.XchainError(
                "HOOK_FAILED: hook reverted in simulation", status=400, error_id="HOOK_FAILED"
            )
        return copy.deepcopy(XCHAIN_ESTIMATE)

    monkeypatch.setattr(xchain, "create_tx", create_tx)
    q = (await client.post("/v1/quote", json=Q, headers=user["headers"])).json()
    r = await arm(client, user, q["quote_id"])
    assert r.status_code == 409
    assert (
        r.json()["detail"]
        == "the router could not simulate the bridge call — try again or a different amount"
    )


async def test_deposit_needs_an_armed_unexpired_own_quote(client, user, fake_xchain, mock_db):
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


async def test_a_quote_that_was_never_armed_cannot_be_deposited(
    client, user, fake_xchain, armed_eth, mock_db
):
    """`armed: true` says the asset is ready, not that an order exists. The hash of a deposit
    made against an estimate belongs to no order of ours — refuse it, do not adopt one."""
    h = user["headers"]
    qid = (await client.post("/v1/quote", json=Q, headers=h)).json()["quote_id"]
    r = await client.post(
        "/v1/deposits", json={"quote_id": qid, "src_tx_hash": "0x" + "ab" * 32}, headers=h
    )
    assert r.status_code == 409 and "arm" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0


async def test_deposit_from_an_armed_quote(client, user, fake_xchain, armed_eth, mock_db):
    h = user["headers"]
    quote, armed = await quote_and_arm(client, user)
    quote = {**quote, **armed}
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
    assert d["mode"] == "xchain"
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


async def test_a_rebuilt_order_does_not_orphan_the_one_before_it(
    client, user, armed_eth, mock_db, monkeypatch
):
    """/arm can build a second order for one quote once the first goes stale. The first was
    never cancelled — an unsigned order is only a transaction we handed over — so if the
    user signs THAT one it is still this quote's order, and the row records the id that matched."""
    from pgasme.routers import quote as quote_router

    ids = iter(["0x" + "a1" * 32, "0x" + "b2" * 32])
    base = FakeRouter()

    async def create_tx(params):
        body = await base(params)
        if ROUTER_HOOK_PARAM in params:
            body["orderId"] = next(ids)
        return body

    monkeypatch.setattr(xchain, "create_tx", create_tx)
    monkeypatch.setattr(quote_router, "ARM_FRESH_S", 0.0)
    h = user["headers"]
    qid = (await client.post("/v1/quote", json=Q, headers=h)).json()["quote_id"]
    first = (await arm(client, user, qid)).json()["order_id"]
    second = (await arm(client, user, qid)).json()["order_id"]
    assert first == "0x" + "a1" * 32 and second == "0x" + "b2" * 32
    q = await mock_db["pgasme_test"].quotes.find_one({"_id": qid})
    assert q["order_id"] == second and set(q["order_ids_armed"]) == {first, second}

    async def order_ids(tx_hash, timeout=None):
        return [first]  # the user signed the FIRST transaction

    monkeypatch.setattr(xchain, "order_ids_by_tx", order_ids)
    tx = "0x" + "cd" * 32
    r = await client.post("/v1/deposits", json={"quote_id": qid, "src_tx_hash": tx}, headers=h)
    assert r.status_code == 200, r.text
    dep = await mock_db["pgasme_test"].deposits.find_one({"_id": r.json()["deposit_id"]})
    assert dep["verified"] is True and dep["order_id"] == first  # the id that actually matched

    async def stranger(tx_hash, timeout=None):
        return ["0x" + "99" * 32]

    monkeypatch.setattr(xchain, "order_ids_by_tx", stranger)
    r = await client.post(
        "/v1/deposits", json={"quote_id": qid, "src_tx_hash": "0x" + "ef" * 32}, headers=h
    )
    assert r.status_code == 400 and "does not carry this quote" in r.json()["detail"]
