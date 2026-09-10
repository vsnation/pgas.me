"""POST /v1/quote, mode "uniswap": the registry, the Quoter read, the calldata the user signs,
and every way this route refuses.

The whole point of the mode is that the transaction the user signs IS the deposit — one
signature, no intermediate balance — so what is asserted here is that the bytes we hand over
say exactly what the quote says, and that nothing that could not be read ever becomes a number.
Offline by construction: the Quoter is a scripted `eth_call` on the fake RPC pool, and the
router's own API must never be reached from an Ethereum quote.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import PUBKEY
from eth_abi import encode

from pgasme import ethpipe, uniswap, workers, xchain
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
ZERO = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAI_TOKEN = ASSETS["DAI"].token
HOOK = "0x1111111111111111111111111111111111112888"
ROUTER = "0x2222222222222222222222222222222222222222"
QUOTER = "0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203"
# 50 USDC → this much ETH: the exact swap output measured on the mainnet fork at block
# 25,942,000 and the first row of contracts/test/vectors/grid.json.
FORK_OUT = 20_094_131_394_204_378
PRICES = {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}


def gateway_key(**over: Any) -> dict[str, Any]:
    return {
        "currency0": ZERO,
        "currency1": USDC,
        "fee": 0,
        "tickSpacing": 1,
        "hooks": HOOK,
        **over,
    }


def inner_key(**over: Any) -> dict[str, Any]:
    return {
        "currency0": ZERO,
        "currency1": USDC,
        "fee": 3000,
        "tickSpacing": 60,
        "hooks": ZERO,
        **over,
    }


def pool_row(**over: Any) -> dict[str, Any]:
    """USDC → ETH: the pair is sorted (native ETH is 0x0), so the swap is oneForZero."""
    return {
        "token_in": USDC,
        "symbol": "USDC",
        "decimals": 6,
        "gateway_pool_key": gateway_key(),
        "inner_pool_key": inner_key(),
        "zero_for_one": False,
        "target": "ETH",
        "max_deposit_units": "2000000000",  # 2,000 USDC
        "min_deposit_units": "1000000",  # 1 USDC
        **over,
    }


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    """No test here may reach CoinGecko; `usd` is best effort and priced from this table."""

    async def px(force: bool = False) -> dict[str, float]:
        return dict(PRICES)

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)


@pytest.fixture(autouse=True)
def no_router_orders(monkeypatch):
    """The cross-chain order API must never be called for an Ethereum quote."""

    async def boom(params: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"the order API must not be reached from a uniswap quote: {params}")

    monkeypatch.setattr(xchain, "create_tx", boom)
    monkeypatch.setattr(xchain, "chain_estimation", boom)


@pytest.fixture
def registry(monkeypatch):
    """The flag on, the three addresses set, one registered pair."""

    def install(rows: list[dict[str, Any]] | None = None, **flags: Any) -> None:
        monkeypatch.setattr(settings, "ingress_uniswap", flags.pop("ingress_uniswap", True))
        monkeypatch.setattr(settings, "uniswap_hook", flags.pop("hook", HOOK))
        monkeypatch.setattr(settings, "uniswap_router", flags.pop("router", ROUTER))
        monkeypatch.setattr(settings, "uniswap_quoter", flags.pop("quoter", QUOTER))
        monkeypatch.setattr(
            settings, "uniswap_pools", json.dumps(rows if rows is not None else [pool_row()])
        )
        for k, v in flags.items():
            monkeypatch.setattr(settings, k, v)
        uniswap.clear_cache()

    install()
    yield install
    uniswap.clear_cache()


class Quoter:
    """A scripted V4Quoter. `out` is what it answers; `fail` makes the endpoint unreadable."""

    def __init__(self) -> None:
        self.out = FORK_OUT
        self.gas = 120_000
        self.fail: Exception | None = None
        self.calls: list[dict[str, Any]] = []


@pytest.fixture
def quoter(rpc, monkeypatch) -> Quoter:
    q = Quoter()

    async def call(method: str, params: list[Any], prefer: str | None = None, pin: bool = False):
        if method != "eth_call":
            raise ethpipe.RpcError(f"FakeRpc: unscripted call {method}")
        q.calls.append(params[0])
        if q.fail:
            raise q.fail
        return "0x" + encode(["uint256", "uint256"], [q.out, q.gas]).hex()

    monkeypatch.setattr(rpc, "call", call)
    return q


def body(**over: Any) -> dict[str, Any]:
    return {
        "src_chain_id": 1,
        "src_token": USDC,
        "amount": "50000000",  # 50 USDC
        "target_asset": "ETH",
        **over,
    }


# ----------------------------------------------------------------------------- the registry


def test_the_selectors_and_the_event_topic_match_the_compiled_contracts():
    """Typed a signature differently and the calldata is silently for another function."""
    assert "0x" + uniswap.DEPOSIT_SELECTOR.hex() == uniswap.DEPOSIT_SELECTOR_EXPECTED
    assert "0x" + uniswap.QUOTE_SELECTOR.hex() == uniswap.QUOTE_SELECTOR_EXPECTED
    assert uniswap.PGAS_DEPOSIT_TOPIC == uniswap.PGAS_DEPOSIT_TOPIC_EXPECTED


def test_pool_id_is_keccak_of_the_abi_encoded_key(registry):
    """Cross-checked against foundry: `cast abi-encode 'f((address,address,uint24,int24,address))'
    '(0x0,0xA0b8…eB48,3000,60,0x…2888)' | cast keccak` — a pool id we compute differently is a
    different pool, and would quote a pool that does not exist."""
    key = uniswap.PoolKey(ZERO, USDC, 3000, 60, "0x0000000000000000000000000000000000002888")
    assert key.pool_id == "0x7423486afb807af961cf66ae7bb9e7004587b0f611943ac030ea24806cf24aba"


def test_the_registry_parses_and_ties_the_route_to_the_pipes_asset(registry):
    r = uniswap.route_for(USDC, "ETH")
    assert r and r.symbol == "USDC" and r.decimals == 6 and r.target == "ETH"
    assert r.token_in == USDC and r.token_out == ZERO and r.native_in is False
    assert r.max_deposit_units == 2_000_000_000 and r.min_deposit_units == 1_000_000
    assert uniswap.route_for(DAI_TOKEN, "ETH") is None  # not registered
    assert uniswap.route_for(USDC, "DAI") is None  # registered for another target
    pub = r.as_json()
    assert pub["hook"] == HOOK and pub["router"] == uniswap.to_checksum_address(ROUTER)
    assert pub["gateway_pool_id"] != pub["inner_pool_id"]  # the hook is part of the id


@pytest.mark.parametrize(
    "over,why",
    [
        ({"gateway_pool_key": gateway_key(hooks=ZERO)}, "not the configured hook"),
        ({"inner_pool_key": inner_key(hooks=HOOK)}, "must be the zero address"),
        ({"inner_pool_key": inner_key(currency1=DAI_TOKEN)}, "same pair"),
        ({"zero_for_one": True}, "not the input side"),
        ({"target": "DAI"}, "is not the DAI pipe's token"),
        ({"token_in": DAI_TOKEN}, "not the input side"),
        ({"max_deposit_units": "0"}, "must be ≥ 1"),
        ({"min_deposit_units": "3000000000"}, "above max_deposit_units"),
        ({"gateway_pool_key": gateway_key(currency0=USDC, currency1=ZERO)}, "strictly below"),
    ],
)
def test_a_route_that_could_only_revert_is_refused_at_parse_time(registry, over, why):
    """Every one of these produces calldata the chain rejects. A registry is not a place to be
    optimistic: it raises, and the route refuses, rather than quoting something unusable."""
    registry([pool_row(**over)])
    with pytest.raises(uniswap.RouteError) as e:
        uniswap.routes()
    assert why in str(e.value)
    assert uniswap.configured() is False  # …and /v1/health says so instead of dying


def test_a_broken_registry_is_not_the_same_as_an_unknown_pair(registry, monkeypatch):
    monkeypatch.setattr(settings, "uniswap_pools", "{not json")
    uniswap.clear_cache()
    with pytest.raises(uniswap.RouteError):
        uniswap.route_for(USDC, "ETH")


# ----------------------------------------------------------------------------- the quote


async def test_the_quote_is_one_quoter_read_and_the_calldata_decodes_back(
    client, user, registry, quoter, armed_eth, mock_db
):
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["mode"] == "uniswap" and q["armed"] is True and "order_id" not in q
    assert len(quoter.calls) == 1  # ONE eth_call, on the Quoter, at the head
    assert quoter.calls[0]["to"] == QUOTER
    assert quoter.calls[0]["data"].startswith(uniswap.QUOTE_SELECTOR_EXPECTED)

    est = q["estimate"]
    assert est["out_units"] == str(FORK_OUT)
    fee = ethpipe.min_relayer_fee_units(ETH)
    value, fee_out = ethpipe.split_amount(FORK_OUT, fee, ETH.grid)
    assert est["value_units"] == str(value) and est["relayer_fee_units"] == str(fee_out)
    assert int(est["value_units"]) + int(est["relayer_fee_units"]) == FORK_OUT
    assert int(est["value_units"]) % ETH.grid == 0
    assert est["min_out_units"] == str(FORK_OUT * 9950 // 10000)  # 50 bps
    assert est["out_groth"] == value // ETH.grid
    assert est["eta_s"] == settings.lock_confirmations * 12 + 120
    assert est["usd"] == pytest.approx(FORK_OUT / 1e18 * PRICES["ETH"])
    assert est["src"] == {
        "chain_id": 1,
        "token": USDC,
        "symbol": "USDC",
        "decimals": 6,
        "amount": "50000000",
    }

    # the transaction: our router, no ether for an ERC-20 input, and an exact-amount approval
    assert q["tx"] == {
        "chain_id": 1,
        "to": uniswap.to_checksum_address(ROUTER),
        "data": q["tx"]["data"],
        "value": "0",
    }
    assert q["approval"] == {
        "chain_id": 1,
        "token": USDC,
        "spender": uniswap.to_checksum_address(ROUTER),
        "amount": "50000000",
    }
    # …and the bytes say exactly what the quote says
    call = uniswap.decode_deposit_calldata(q["tx"]["data"])
    assert call["amount_in"] == 50_000_000 and call["zero_for_one"] is False
    assert call["deposit_ref"] == q["deposit_ref"]
    assert call["min_out"] == int(est["min_out_units"]) and call["relayer_fee"] == fee
    assert call["pool_key"].hooks == HOOK and call["pool_key"].fee == 0
    assert call["pool_key"].pool_id == q["route"]["gateway_pool_id"]

    row = await mock_db["pgasme_test"].quotes.find_one({"_id": q["quote_id"]})
    assert row["mode"] == "uniswap" and row["deposit_ref"] == q["deposit_ref"]
    assert row["hook_calldata"] == q["tx"]["data"] and row["pubkey"] == PUBKEY
    assert row["min_out_units"] == est["min_out_units"]
    assert row["relayer_fee_quote_units"] == str(fee)
    assert row["route"] == q["route"] and row["armed"] is True


async def test_two_quotes_never_share_a_deposit_reference(client, user, registry, quoter, armed_eth):
    refs = set()
    for _ in range(3):
        r = await client.post("/v1/quote", json=body(), headers=user["headers"])
        ref = r.json()["deposit_ref"]
        assert len(bytes.fromhex(ref[2:])) == 32
        refs.add(ref)
    assert len(refs) == 3


async def test_unarmed_is_an_estimate_only_and_issues_no_transaction(
    client, user, registry, quoter
):
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["mode"] == "uniswap" and q["armed"] is False
    assert "tx" not in q and "approval" not in q and "not armed" in q["note"]
    assert q["estimate"]["out_units"] == str(FORK_OUT)  # the price still answers


async def test_native_input_carries_the_value_and_needs_no_approval(
    client, user, registry, quoter, armed_eth
):
    """A route whose input is ether: the router forwards msg.value and refunds the remainder."""
    registry(
        [
            pool_row(
                token_in=ZERO,
                symbol="ETH",
                decimals=18,
                zero_for_one=True,
                target="DAI",
                gateway_pool_key=gateway_key(currency1=DAI_TOKEN),
                inner_pool_key=inner_key(currency1=DAI_TOKEN),
                max_deposit_units=str(10**19),
            )
        ],
        beam_pipe_pubkey_dai=PUBKEY,
    )
    quoter.out = 250 * 10**18
    r = await client.post(
        "/v1/quote",
        json=body(src_token=ZERO, amount=str(10**17), target_asset="DAI"),
        headers=user["headers"],
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["mode"] == "uniswap" and "approval" not in q
    assert q["tx"]["value"] == str(10**17)
    assert uniswap.decode_deposit_calldata(q["tx"]["data"])["zero_for_one"] is True


# ----------------------------------------------------------------------------- the refusals


async def test_an_unreadable_quoter_is_503_and_never_a_zero(client, user, registry, quoter):
    quoter.fail = ethpipe.RpcError("eth_call: no endpoint answered")
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 503 and "could not read a price" in r.json()["detail"]


async def test_a_pool_with_no_depth_is_refused_rather_than_quoted_as_zero(
    client, user, registry, quoter
):
    """A pool initialised with zero liquidity looks real and pays nothing."""
    quoter.out = 0
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 400 and "no depth" in r.json()["detail"]


async def test_a_quoter_answer_that_is_not_two_words_is_unreadable(registry):
    with pytest.raises(uniswap.QuoteUnreadable):
        uniswap.decode_quote_result("0x")
    with pytest.raises(uniswap.QuoteUnreadable):
        uniswap.decode_quote_result(None)


async def test_above_the_routes_cap_and_below_its_floor_are_refused(
    client, user, registry, quoter
):
    h = user["headers"]
    r = await client.post("/v1/quote", json=body(amount="2000000001"), headers=h)
    assert r.status_code == 400 and "cap" in r.json()["detail"]
    r = await client.post("/v1/quote", json=body(amount="999999"), headers=h)
    assert r.status_code == 400 and "minimum" in r.json()["detail"]
    assert quoter.calls == []  # neither one cost an upstream read


async def test_an_unregistered_pair_asked_for_by_name_is_refused(client, user, registry, quoter):
    r = await client.post(
        "/v1/quote", json=body(src_token=DAI_TOKEN, route="uniswap"), headers=user["headers"]
    )
    assert r.status_code == 400 and "not a registered source" in r.json()["detail"]


async def test_a_misconfigured_registry_asked_for_by_name_is_503(
    client, user, registry, quoter, monkeypatch
):
    monkeypatch.setattr(settings, "uniswap_pools", "[{}]")
    uniswap.clear_cache()
    r = await client.post("/v1/quote", json=body(route="uniswap"), headers=user["headers"])
    assert r.status_code == 503 and "misconfigured" in r.json()["detail"]


async def test_a_deposit_too_small_for_the_grid_at_the_worst_output_is_refused(
    client, user, registry, quoter
):
    """The floor belongs on the output the hook will still accept, not on the mid-price one."""
    quoter.out = ethpipe.min_relayer_fee_units(ETH) + ETH.grid  # one groth over the fee
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 400
    assert "amount too small" in r.json()["detail"] or "minimum deposit" in r.json()["detail"]


async def test_the_relayer_fee_ceiling_of_the_route_is_applied_before_we_hand_over_calldata(
    client, user, registry, quoter, armed_eth
):
    # a route whose own floor is 0.001 ETH against a ceiling of 1 % of a ~0.02 ETH output
    registry([pool_row(min_relayer_fee_units=str(10**15), max_relayer_fee_bps="100")])
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 400 and "ceiling" in r.json()["detail"]


async def test_the_kill_switch_refuses_a_route_that_would_reach_our_pipe(
    client, user, registry, quoter, armed_eth, monkeypatch
):
    monkeypatch.setattr(workers, "paused", lambda: True)
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 409 and "paused by the operator" in r.json()["detail"]


# ----------------------------------------------------------------------------- the flags


async def test_with_the_flag_off_the_route_is_not_available(client, user, registry, quoter):
    registry(ingress_uniswap=False)
    r = await client.post("/v1/quote", json=body(route="uniswap"), headers=user["headers"])
    assert r.status_code == 409 and r.json()["detail"] == "the Uniswap route is not available"


async def test_with_the_flag_off_auto_falls_back_to_the_existing_swap_route(
    client, user, registry, quoter, monkeypatch
):
    """Turning the route off must not turn the API off: `auto` still answers, the old way."""
    registry(ingress_uniswap=False)
    seen: list[dict[str, Any]] = []

    async def estimation(params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        raise ValueError("stop here — reaching the single-chain swap is the assertion")

    monkeypatch.setattr(xchain, "chain_estimation", estimation)
    with pytest.raises(ValueError):
        await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert seen and seen[0]["tokenIn"] == USDC


async def test_the_cross_chain_route_can_be_paused_without_touching_this_one(
    client, user, registry, quoter, armed_eth, monkeypatch
):
    monkeypatch.setattr(settings, "ingress_xchain", False)
    h = user["headers"]
    r = await client.post("/v1/quote", json=body(src_chain_id=42161), headers=h)
    assert r.status_code == 409 and r.json()["detail"] == (
        "cross-chain deposits are paused — deposit from Ethereum instead"
    )
    r = await client.post("/v1/quote", json=body(route="xchain"), headers=h)
    assert r.status_code == 409  # by name, from Ethereum: the same neutral refusal
    r = await client.post("/v1/quote", json=body(), headers=h)  # …and Ethereum still works
    assert r.status_code == 200 and r.json()["mode"] == "uniswap"


async def test_arming_a_cross_chain_quote_while_it_is_paused_is_refused(
    client, user, armed_eth, mock_db, monkeypatch
):
    import time

    now = time.time()
    await mock_db["pgasme_test"].quotes.insert_one(
        {
            "_id": "qx",
            "account_id": user["account_id"],
            "address": user["address"],
            "asset": "ETH",
            "mode": "xchain",
            "src": {"chain_id": 42161, "route_chain_id": 42161, "token": USDC, "amount": "1"},
            "out_units": "1",
            "value_units": "1",
            "relayer_fee_units": "0",
            "value_groth": 0,
            "metadata": "0x1122334455",
            "armed": True,
            "at": now,
            "expires_at": now + 900,
            "estimate": {},
        }
    )
    monkeypatch.setattr(settings, "ingress_xchain", False)
    r = await client.post("/v1/quote/qx/arm", headers=user["headers"])
    assert r.status_code == 409 and "cross-chain deposits are paused" in r.json()["detail"]


async def test_arming_a_uniswap_quote_hands_back_the_same_transaction(
    client, user, registry, quoter, armed_eth
):
    """There is no order to place, so /arm is a read: the same bytes, and no second Quoter call."""
    q = (await client.post("/v1/quote", json=body(), headers=user["headers"])).json()
    r = await client.post(f"/v1/quote/{q['quote_id']}/arm", headers=user["headers"])
    assert r.status_code == 200, r.text
    armed = r.json()
    assert armed["mode"] == "uniswap" and armed["tx"] == q["tx"]
    assert armed["deposit_ref"] == q["deposit_ref"] and armed["route"] == q["route"]
    assert len(quoter.calls) == 1


async def test_an_unknown_route_name_is_refused(client, user, registry, quoter):
    r = await client.post("/v1/quote", json=body(route="teleport"), headers=user["headers"])
    assert r.status_code == 400 and "unknown route" in r.json()["detail"]


async def test_the_target_asset_itself_still_quotes_direct_not_uniswap(
    client, user, registry, quoter, armed_eth
):
    r = await client.post(
        "/v1/quote", json=body(src_token=ZERO, amount=str(5 * 10**16)), headers=user["headers"]
    )
    assert r.status_code == 200 and r.json()["mode"] == "direct"
    assert quoter.calls == []


# ----------------------------------------------------------------------------- what is published


async def test_health_assets_and_account_state_which_ways_in_are_open(
    client, user, registry, quoter, armed_eth
):
    """A flag the API does not state is a flag the client treats as off, so all three say it."""
    health = (await client.get("/v1/health")).json()
    assert health["ingress"] == {"uniswap": True, "xchain": True, "direct": True}

    assets = (await client.get("/v1/assets")).json()
    assert assets["ingress"]["uniswap"] is True and assets["ingress"]["direct"] is True
    assert assets["ingress"]["uniswap_tokens"] == [
        {"address": USDC, "symbol": "USDC", "decimals": 6}
    ]
    assert (await client.get("/v1/dex/assets")).json() == assets  # one handler, two paths

    acct = (await client.get("/v1/account", headers=user["headers"])).json()
    assert acct["ingress"] == {"armed": True, "near": False, "uniswap": True, "xchain": True}


async def test_with_the_flags_off_nothing_advertises_a_route_that_would_refuse(
    client, user, registry, monkeypatch
):
    registry(ingress_uniswap=False)
    monkeypatch.setattr(settings, "ingress_xchain", False)
    health = (await client.get("/v1/health")).json()
    assert health["ingress"] == {"uniswap": False, "xchain": False, "direct": False}
    assets = (await client.get("/v1/assets")).json()
    assert assets["ingress"]["uniswap"] is False and assets["ingress"]["uniswap_tokens"] == []
    acct = (await client.get("/v1/account", headers=user["headers"])).json()
    assert acct["ingress"]["uniswap"] is False and acct["ingress"]["xchain"] is False


async def test_an_unusable_registry_never_takes_health_down_with_it(
    client, user, registry, monkeypatch
):
    monkeypatch.setattr(settings, "uniswap_pools", '[{"token_in":"nonsense"}]')
    uniswap.clear_cache()
    health = (await client.get("/v1/health")).json()
    assert health["ingress"]["uniswap"] is False
    assert (await client.get("/v1/assets")).json()["ingress"]["uniswap_tokens"] == []
