"""PGAS_INGRESS_DEFAULT_ROUTE (T31 D1): which route `route:"auto"` resolves to, and the ONE
place every endpoint reads it from.

The toggle exists so the operator can steer new quotes to one of the two open ways in without
touching either flag. It can only ever choose BETWEEN OPEN ROUTES — it opens nothing — and the
client preselects its route control from what the API publishes, so what is published must be a
route that would actually serve.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from conftest import PUBKEY, XCHAIN_SWAP_ESTIMATION, XCHAIN_SWAP_TX
from eth_abi import encode
from pydantic import ValidationError

from pgasme import ethpipe, uniswap, xchain
from pgasme.config import DEFAULT_INGRESS_ROUTE, INGRESS_ROUTES, Settings, settings

HOOK = "0x1111111111111111111111111111111111112888"
ROUTER = "0x2222222222222222222222222222222222222222"
QUOTER = "0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203"
ZERO = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


@pytest.fixture
def uniswap_open(monkeypatch: pytest.MonkeyPatch):
    """The Uniswap route configured and usable: flag on, addresses set, one registered pair."""
    pool = {
        "token_in": USDC,
        "symbol": "USDC",
        "decimals": 6,
        "target": "ETH",
        "gateway_pool_key": {
            "currency0": ZERO,
            "currency1": USDC,
            "fee": 0,
            "tickSpacing": 1,
            "hooks": HOOK,
        },
        "inner_pool_key": {
            "currency0": ZERO,
            "currency1": USDC,
            "fee": 3000,
            "tickSpacing": 60,
            "hooks": ZERO,
        },
        "zero_for_one": False,
        "max_deposit_units": "500000000",
    }
    monkeypatch.setattr(settings, "ingress_uniswap", True)
    monkeypatch.setattr(settings, "uniswap_hook", HOOK)
    monkeypatch.setattr(settings, "uniswap_router", ROUTER)
    monkeypatch.setattr(settings, "uniswap_quoter", QUOTER)
    monkeypatch.setattr(settings, "uniswap_pools", json.dumps([pool]))
    uniswap.clear_cache()
    yield
    uniswap.clear_cache()


def set_route(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    monkeypatch.setattr(settings, "ingress_default_route", route)


# ----------------------------------------------------------------- the name of the thing


def test_the_two_route_names_have_exactly_one_spelling_each():
    """config.py cannot import these two modules (they import it), so the strings are repeated
    there — and this is what stops the copies drifting (law 9)."""
    assert set(INGRESS_ROUTES) == {xchain.MODE, uniswap.MODE}
    assert DEFAULT_INGRESS_ROUTE == xchain.MODE


def test_an_unknown_default_route_refuses_at_parse_time_instead_of_quietly_falling_back():
    assert Settings(ingress_default_route="UNISWAP ").ingress_default_route == "uniswap"
    assert Settings(ingress_default_route="").ingress_default_route == DEFAULT_INGRESS_ROUTE
    with pytest.raises(ValidationError, match="PGAS_INGRESS_DEFAULT_ROUTE"):
        Settings(ingress_default_route="unisawp")


# ----------------------------------------------------------------- what auto resolves to


def test_with_both_routes_open_the_default_is_what_the_operator_set(monkeypatch, uniswap_open):
    monkeypatch.setattr(settings, "ingress_xchain", True)
    set_route(monkeypatch, "xchain")
    assert uniswap.default_route() == "xchain"
    set_route(monkeypatch, "uniswap")
    assert uniswap.default_route() == "uniswap"


def test_a_default_that_names_a_closed_route_is_clamped_to_the_open_one(monkeypatch, uniswap_open):
    """A client preselects its control from this: a closed route would be a button that 409s."""
    set_route(monkeypatch, "uniswap")
    monkeypatch.setattr(settings, "ingress_uniswap", False)
    uniswap.clear_cache()
    monkeypatch.setattr(settings, "ingress_xchain", True)
    assert uniswap.default_route() == "xchain"

    monkeypatch.setattr(settings, "ingress_uniswap", True)
    uniswap.clear_cache()
    set_route(monkeypatch, "xchain")
    monkeypatch.setattr(settings, "ingress_xchain", False)
    assert uniswap.default_route() == "uniswap"


def test_with_nothing_open_it_states_the_preference_and_invents_no_third_answer(monkeypatch):
    monkeypatch.setattr(settings, "ingress_uniswap", False)
    monkeypatch.setattr(settings, "ingress_xchain", False)
    uniswap.clear_cache()
    set_route(monkeypatch, "uniswap")
    flags = uniswap.ingress_flags()
    assert flags == {
        "uniswap": False,
        "xchain": False,
        "direct": False,
        "default_route": "uniswap",
    }


def test_an_explicit_route_always_beats_the_default_and_auto_always_follows_it(
    monkeypatch, uniswap_open
):
    monkeypatch.setattr(settings, "ingress_xchain", True)
    set_route(monkeypatch, "xchain")
    assert uniswap.wants_uniswap("uniswap") is True  # explicit, whatever the default says
    assert uniswap.wants_uniswap("auto") is False
    assert uniswap.wants_uniswap(None) is False  # an omitted route IS auto

    set_route(monkeypatch, "uniswap")
    assert uniswap.wants_uniswap("auto") is True
    assert uniswap.wants_uniswap(None) is True
    for other in ("xchain", "direct", "swap"):
        assert uniswap.wants_uniswap(other) is False


def test_auto_never_resolves_to_a_route_that_is_closed(monkeypatch, uniswap_open):
    set_route(monkeypatch, "uniswap")
    monkeypatch.setattr(settings, "ingress_uniswap", False)
    uniswap.clear_cache()
    assert uniswap.wants_uniswap("auto") is False


# ----------------------------------------------------------------- what the three endpoints say


async def test_health_assets_and_account_all_publish_the_same_default_route(
    client, user, monkeypatch, uniswap_open
):
    """Three endpoints, ONE implementation — a client that reads any of them gets one answer."""
    monkeypatch.setattr(settings, "ingress_xchain", True)
    monkeypatch.setattr(settings, "ingress_armed", True)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    set_route(monkeypatch, "uniswap")

    health = (await client.get("/v1/health")).json()["ingress"]
    assets = (await client.get("/v1/dex/assets")).json()["ingress"]
    acct = (await client.get("/v1/account", headers=user["headers"])).json()["ingress"]

    assert health == {"uniswap": True, "xchain": True, "direct": True, "default_route": "uniswap"}
    assert assets["default_route"] == "uniswap" and assets["uniswap_tokens"]
    assert acct == {
        "armed": True,
        "near": False,
        "uniswap": True,
        "xchain": True,
        "default_route": "uniswap",
    }

    set_route(monkeypatch, "xchain")
    again: dict[str, Any] = (await client.get("/v1/health")).json()["ingress"]
    assert again["default_route"] == "xchain"


# ------------------------------------------------------- and what the QUOTE ROUTE does with it
# T31b item 1: `default_route()` and `wants_uniswap()` above were the ONE resolver — and the quote
# route did not call either of them. It matched `route in ("auto", "uniswap")` itself, so `auto`
# picked Uniswap whenever the pair happened to be registered and `PGAS_INGRESS_DEFAULT_ROUTE` was
# a setting three endpoints published and nothing obeyed (law 9: two implementations of one fact
# will disagree, and one of them reaches money).


@pytest.fixture
def prices(monkeypatch: pytest.MonkeyPatch):
    """No quote test may reach CoinGecko; `usd` is best effort and priced from here."""

    async def px(force: bool = False) -> dict[str, float]:
        return {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)


@pytest.fixture
def quoter(rpc: Any, monkeypatch: pytest.MonkeyPatch):
    """The chain, scripted by SELECTOR — a live pool, a price, and both allowances already
    granted. A blanket answer would have made the quote's `note` carry "could not read …" in a
    file that is not about that, and would have hidden the difference between the reads."""

    async def call(method: str, params: list[Any], prefer: str | None = None, pin: bool = False):
        if method != "eth_call":
            raise ethpipe.RpcError(f"FakeRpc: unscripted call {method}")
        data = params[0]["data"]
        if data.startswith("0x" + uniswap.EXTSLOAD_SELECTOR.hex()):  # slot0 / liquidity
            return "0x" + (10**24).to_bytes(32, "big").hex()
        if data.startswith("0x" + uniswap.ERC20_ALLOWANCE_SELECTOR.hex()):
            return "0x" + encode(["uint256"], [2**160 - 1]).hex()
        if data.startswith("0x" + uniswap.PERMIT2_ALLOWANCE_SELECTOR.hex()):
            return "0x" + encode(["uint160", "uint48", "uint48"], [2**160 - 1, 2**48 - 1, 0]).hex()
        return "0x" + encode(["uint256", "uint256"], [20_094_131_394_204_378, 120_000]).hex()

    monkeypatch.setattr(rpc, "call", call)


@pytest.fixture
def swap_route(monkeypatch: pytest.MonkeyPatch):
    """The classic Ethereum answer for a token that is not the pipe's own: the router's
    single-chain swap into the user's wallet. Scripted from the recorded live bodies."""

    async def estimation(params: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(XCHAIN_SWAP_ESTIMATION["estimation"])

    async def transaction(params: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(XCHAIN_SWAP_TX)

    monkeypatch.setattr(xchain, "chain_estimation", estimation)
    monkeypatch.setattr(xchain, "chain_transaction", transaction)


def quote_body(**over: Any) -> dict[str, Any]:
    return {"src_chain_id": 1, "src_token": USDC, "amount": "50000000", "target_asset": "ETH", **over}


async def mode_of(client: Any, user: dict[str, Any], **over: Any) -> str:
    r = await client.post("/v1/quote", json=quote_body(**over), headers=user["headers"])
    assert r.status_code == 200, r.text
    return str(r.json()["mode"])


async def test_auto_from_ethereum_follows_the_default_route_and_not_the_registry(
    client, user, monkeypatch, uniswap_open, quoter, swap_route, prices, armed_eth
):
    """Both routes open, the pair registered, the default set to cross-chain: an omitted route —
    and the explicit `auto` that means the same thing — must answer the CLASSIC Ethereum shape."""
    monkeypatch.setattr(settings, "ingress_xchain", True)
    set_route(monkeypatch, "xchain")

    assert await mode_of(client, user) == "swap"  # omitted IS auto
    assert await mode_of(client, user, route="auto") == "swap"
    # …and asking for it by name still wins over the default, in either direction
    assert await mode_of(client, user, route="uniswap") == "uniswap"


async def test_auto_answers_uniswap_when_that_is_the_operators_default(
    client, user, monkeypatch, uniswap_open, quoter, swap_route, prices, armed_eth
):
    monkeypatch.setattr(settings, "ingress_xchain", True)
    set_route(monkeypatch, "uniswap")

    assert await mode_of(client, user) == "uniswap"
    assert await mode_of(client, user, route="auto") == "uniswap"


async def test_the_quote_route_and_health_can_never_disagree_about_what_auto_means(
    client, user, monkeypatch, uniswap_open, quoter, swap_route, prices, armed_eth
):
    """The client preselects its control from `ingress.default_route`; a quote that resolved
    `auto` some other way would hand it a different route than the one the button says."""
    monkeypatch.setattr(settings, "ingress_xchain", True)
    for want in ("xchain", "uniswap"):
        set_route(monkeypatch, want)
        stated = (await client.get("/v1/health")).json()["ingress"]["default_route"]
        got = await mode_of(client, user, route="auto")
        assert stated == want
        assert (got == "uniswap") is (stated == "uniswap"), f"{want}: health says {stated}, quote {got}"
