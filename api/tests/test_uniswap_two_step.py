"""POST /v1/quote, mode "uniswap" — THE TWO-STEP ROUTE, which is what ships.

Nothing of ours is deployed on this path. Step 1 is a plain swap on Uniswap's own Universal
Router that pays the output to the USER's own wallet; step 2 is the unchanged `direct` deposit
of what actually arrived. So the things worth asserting here are the ones that could hurt
somebody:

  * the calldata says exactly what the quote says, and it is the SAME BYTES a mainnet fork test
    executes through the real Universal Router (`contracts/test/vectors/uniswap-two-step.json`).
    One fact, both sides — a Python-only assertion would only prove Python agrees with itself;
  * `amountOutMinimum` is never zero. It is the only protection the user has on this path,
    because the output lands in their wallet and nothing of ours would notice a bad fill;
  * a swap transaction is NEVER a deposit — `/arm` and `POST /v1/deposits` both refuse it;
  * both Permit2 allowances are READ, and an allowance we could not read is never "already
    approved";
  * a pool that is not live, a price nobody would answer, and a size above the route's cap each
    refuse with their own status and their own reason.

Offline by construction: every chain read is scripted on the fake RPC pool, and the cross-chain
router's API must never be reached from an Ethereum quote.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import pytest
from conftest import PUBKEY
from eth_abi import encode

from pgasme import ethpipe, uniswap, xchain
from pgasme.assets import ASSETS
from pgasme.config import settings

ETH = ASSETS["ETH"]
ZERO = "0x0000000000000000000000000000000000000000"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
DAI_TOKEN = ASSETS["DAI"].token

# Uniswap's own mainnet deployments — the values `config.py` pins, repeated here so a change to
# either side is a failing test and not a silently different transaction.
UNIVERSAL_ROUTER = "0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af"
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
QUOTER = "0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203"
POOL_MANAGER = "0x000000000004444c5dc75cB358380D2e3dE08A90"

# ── the golden vector ────────────────────────────────────────────────────────────────────────
# 50 USDC → ETH on the canonical hook-less ETH/USDC 0.30% pool, at mainnet block 25,942,000.
# `FORK_OUT` is the V4 Quoter's own answer at that block, read with `cast` against an archive
# endpoint (2026-09-10) and identical to the number the Solidity fork suite already pins.
FORK_BLOCK = 25_942_000
FORK_BLOCK_TS = 1_788_983_231  # `cast block 25942000 --field timestamp`
FORK_OUT = 20_094_131_394_204_378
VECTOR_AMOUNT_IN = 50_000_000  # 50 USDC
VECTOR_SLIPPAGE_BPS = 50
VECTOR_MIN_OUT = FORK_OUT * (10_000 - VECTOR_SLIPPAGE_BPS) // 10_000  # 19_993_660_737_233_356
# A deadline is a wall-clock time, and the vector must be the same bytes every run — so it is
# pinned relative to the FORK BLOCK, which also makes it a deadline that is genuinely in the
# future at the block the fork test executes it on.
VECTOR_NOW = FORK_BLOCK_TS
VECTOR_DEADLINE = FORK_BLOCK_TS + 1200
VECTOR_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "contracts/test/vectors/uniswap-two-step.json"
)
# The whole `execute(bytes,bytes[],uint256)` calldata for that swap, pinned. Cross-checked
# against foundry byte for byte, 2026-09-10:
#   cast calldata 'execute(bytes,bytes[],uint256)' 0x10 "[$(cast abi-encode 'f(bytes,bytes[])' \
#     0x060c0f "[$P0,$P1,$P2]")]" 1788984431
# where P0 = cast abi-encode 'f(((address,address,uint24,int24,address),bool,uint128,uint128,bytes))'
#   '((0x0…0,0xA0b8…eB48,3000,60,0x0…0),false,50000000,19993660737233356,0x)',
# P1 = cast abi-encode 'f(address,uint256)' 0xA0b8…eB48 50000000 and
# P2 = cast abi-encode 'f(address,uint256)' 0x0…0 19993660737233356.
# A literal, not a re-computation: a test that builds the expected value the same way the code
# does asserts only that the code is consistent with itself.
VECTOR_CALLDATA = (
    "0x3593564c"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "00000000000000000000000000000000000000000000000000000000000000a0"
    "000000000000000000000000000000000000000000000000000000006aa1bc6f"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "1000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000340"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000080"
    "0000000000000000000000000000000000000000000000000000000000000003"
    "060c0f0000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000003"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "00000000000000000000000000000000000000000000000000000000000001e0"
    "0000000000000000000000000000000000000000000000000000000000000240"
    "0000000000000000000000000000000000000000000000000000000000000160"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000bb8"
    "000000000000000000000000000000000000000000000000000000000000003c"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000002faf080"
    "00000000000000000000000000000000000000000000000000470820e600a1cc"
    "0000000000000000000000000000000000000000000000000000000000000120"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000002faf080"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000470820e600a1cc"
)

PRICES = {"ETH": 2500.0, "DAI": 1.0, "WBTC": 80000.0}
BIG_LIQUIDITY = 2_846_479_248_297_004_574  # what the real pool held at the pinned block
# sqrtPriceX96 of the real ETH/USDC 0.30% pool at the pinned block (`extsload` on the
# PoolManager, low 160 bits of slot0) — the reference `price_impact_bps` is measured against.
FORK_SQRT_PRICE_X96 = 0x3436D972439E9B6AF2701


def inner_key(**over: Any) -> dict[str, Any]:
    """The canonical, HOOK-LESS ETH/USDC 0.30% pool. The pair is sorted, so native ETH (0x0) is
    currency0 and USDC → ETH is oneForZero."""
    return {
        "currency0": ZERO,
        "currency1": USDC,
        "fee": 3000,
        "tickSpacing": 60,
        "hooks": ZERO,
        **over,
    }


def pool_row(**over: Any) -> dict[str, Any]:
    return {
        "token_in": USDC,
        "symbol": "USDC",
        "decimals": 6,
        "inner_pool_key": inner_key(),
        "zero_for_one": False,
        "target": "ETH",
        "max_deposit_units": "2000000000",  # 2,000 USDC
        "min_deposit_units": "1000000",  # 1 USDC
        **over,
    }


def native_row(**over: Any) -> dict[str, Any]:
    """ETH → bDAI: a NATIVE input, which needs no approval and pays with `msg.value`."""
    return {
        "token_in": ZERO,
        "symbol": "ETH",
        "decimals": 18,
        "inner_pool_key": {
            "currency0": ZERO,
            "currency1": DAI_TOKEN,
            "fee": 3000,
            "tickSpacing": 60,
            "hooks": ZERO,
        },
        "zero_for_one": True,
        "target": "DAI",
        "max_deposit_units": str(10**18),
        "min_deposit_units": str(10**15),
        **over,
    }


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    async def px(force: bool = False) -> dict[str, float]:
        return dict(PRICES)

    monkeypatch.setattr("pgasme.routers.quote.usd_prices", px)


@pytest.fixture(autouse=True)
def no_router_orders(monkeypatch):
    """The cross-chain order API must never be reached from an Ethereum quote."""

    async def boom(params: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"the order API must not be reached from a uniswap quote: {params}")

    monkeypatch.setattr(xchain, "create_tx", boom)
    monkeypatch.setattr(xchain, "chain_estimation", boom)
    monkeypatch.setattr(xchain, "chain_transaction", boom)


@pytest.fixture
def registry(monkeypatch):
    """The flag on, Uniswap's own addresses at their pinned values, one registered pair, and this
    route as the operator's default (an omitted `route` is `auto`, which follows the default).

    The HOOK flag stays OFF — that is the whole point of this file. The reference one-transaction
    shape lives in tests/test_uniswap_quote.py, which turns it on."""

    def install(rows: list[dict[str, Any]] | None = None, **flags: Any) -> None:
        monkeypatch.setattr(settings, "ingress_default_route", uniswap.MODE)
        monkeypatch.setattr(settings, "ingress_uniswap", True)
        monkeypatch.setattr(settings, "uniswap_hook_enabled", False)
        monkeypatch.setattr(settings, "uniswap_slippage_bps", VECTOR_SLIPPAGE_BPS)
        monkeypatch.setattr(
            settings, "uniswap_pools", json.dumps(rows if rows is not None else [pool_row()])
        )
        for k, v in flags.items():
            monkeypatch.setattr(settings, k, v)
        uniswap.clear_cache()

    install()
    yield install
    uniswap.clear_cache()


class Chain:
    """Everything the two-step quote reads, scripted by selector.

    `fail` makes ONE of the four reads unreadable, which is how "an unreadable query is not
    evidence of anything" is asserted separately for each of them."""

    def __init__(self) -> None:
        self.out = FORK_OUT
        self.gas = 120_000
        self.sqrt_price = FORK_SQRT_PRICE_X96
        self.liquidity = BIG_LIQUIDITY
        self.erc20_allowance = 2**160 - 1
        self.permit2_amount = 2**160 - 1
        self.permit2_expiration = 2**48 - 1
        self.fail: dict[str, Exception] = {}
        self.short: set[str] = set()
        self.calls: list[dict[str, Any]] = []
        self._slot0_next = True

    def kind(self, data: str) -> str:
        for name, sel in (
            ("extsload", uniswap.EXTSLOAD_SELECTOR),
            ("quote", uniswap.QUOTE_SELECTOR),
            ("erc20_allowance", uniswap.ERC20_ALLOWANCE_SELECTOR),
            ("permit2_allowance", uniswap.PERMIT2_ALLOWANCE_SELECTOR),
        ):
            if data.startswith("0x" + sel.hex()):
                return name
        raise AssertionError(f"unscripted eth_call {data[:10]}")


@pytest.fixture
def chain(rpc, monkeypatch) -> Chain:
    c = Chain()

    async def call(method: str, params: list[Any], prefer: str | None = None, pin: bool = False):
        if method != "eth_call":
            raise ethpipe.RpcError(f"FakeRpc: unscripted call {method}")
        c.calls.append(params[0])
        kind = c.kind(params[0]["data"])
        if kind == "extsload":
            # the two reads arrive in this order: slot0, then liquidity
            first, c._slot0_next = c._slot0_next, not c._slot0_next
            kind = "slot0" if first else "liquidity"
        if e := c.fail.get(kind):
            raise e
        if kind in c.short:
            return "0x"
        if kind == "slot0":
            return "0x" + c.sqrt_price.to_bytes(32, "big").hex()
        if kind == "liquidity":
            return "0x" + c.liquidity.to_bytes(32, "big").hex()
        if kind == "quote":
            return "0x" + encode(["uint256", "uint256"], [c.out, c.gas]).hex()
        if kind == "erc20_allowance":
            return "0x" + encode(["uint256"], [c.erc20_allowance]).hex()
        return (
            "0x"
            + encode(
                ["uint160", "uint48", "uint48"],
                [c.permit2_amount, c.permit2_expiration, 0],
            ).hex()
        )

    monkeypatch.setattr(rpc, "call", call)
    return c


@pytest.fixture
def frozen_deadline(monkeypatch):
    """`swap_deadline()` reads the clock, and a quote's bytes must be assertable. Pinned to the
    fork block's own timestamp so what this file builds is what the fork test executes."""
    monkeypatch.setattr(time, "time", lambda: float(VECTOR_NOW))
    return VECTOR_DEADLINE


def body(**over: Any) -> dict[str, Any]:
    return {
        "src_chain_id": 1,
        "src_token": USDC,
        "amount": str(VECTOR_AMOUNT_IN),
        "target_asset": "ETH",
        **over,
    }


async def quote(client, user, **over: Any) -> dict[str, Any]:
    r = await client.post("/v1/quote", json=body(**over), headers=user["headers"])
    assert r.status_code == 200, r.text
    return dict(r.json())


# ------------------------------------------------------------------ the encoding, pinned


def test_the_execute_selector_and_the_v4_commands_are_the_deployed_ones():
    """Typed a signature differently and the calldata is silently for another function; typed a
    command differently and the router answers `InvalidCommandType`. Both were read off the
    deployed Universal Router with cast (2026-09-10): command 0x3f reverts
    `InvalidCommandType(63)`, command 0x10 reverts `SliceOutOfBounds` — i.e. 0x10 IS V4_SWAP."""
    assert "0x" + uniswap.EXECUTE_SELECTOR.hex() == uniswap.EXECUTE_SELECTOR_EXPECTED
    assert uniswap.COMMAND_V4_SWAP == 0x10
    assert (uniswap.ACTION_SWAP_EXACT_IN_SINGLE, uniswap.ACTION_SETTLE_ALL, uniswap.ACTION_TAKE_ALL) == (
        0x06,
        0x0C,
        0x0F,
    )


def test_the_pinned_addresses_are_uniswaps_own_mainnet_deployments():
    """Pinned in ONE place. `contracts/test/fork/UniswapTwoStep.t.sol` is what proves each one is
    the contract we think it is — code present, and a discriminating view: the Universal Router's
    `poolManager()` (the pre-v4 router has no such function) and the Quoter's."""
    assert settings.uniswap_universal_router == UNIVERSAL_ROUTER
    assert settings.uniswap_permit2 == PERMIT2
    assert settings.uniswap_quoter == QUOTER
    assert settings.uniswap_pool_manager == POOL_MANAGER


def test_the_calldata_is_the_golden_vector_byte_for_byte_and_the_vector_is_written(registry):
    """ONE FACT, BOTH SIDES. This builds the swap for 50 USDC at the pinned block, asserts it
    against a literal, and writes `contracts/test/vectors/uniswap-two-step.json` — which
    `contracts/test/fork/UniswapTwoStep.t.sol` then EXECUTES through the real Universal Router on
    a mainnet fork. Neither side describes the swap in its own words."""
    route = uniswap.route_for(USDC, "ETH")
    assert route is not None
    data = uniswap.swap_calldata(route, VECTOR_AMOUNT_IN, VECTOR_MIN_OUT, VECTOR_DEADLINE)
    assert data == VECTOR_CALLDATA

    actions, params = uniswap.swap_actions(route, VECTOR_AMOUNT_IN, VECTOR_MIN_OUT)
    inputs = ["0x" + encode(["bytes", "bytes[]"], [actions, params]).hex()]
    vector = {
        "_": (
            "Written by api/tests/test_uniswap_two_step.py. The API builds this calldata and "
            "contracts/test/fork/UniswapTwoStep.t.sol executes these exact bytes through the "
            "real Universal Router at the pinned block. Do not hand-edit."
        ),
        "chain_id": 1,
        "block": FORK_BLOCK,
        "block_timestamp": FORK_BLOCK_TS,
        "universal_router": UNIVERSAL_ROUTER,
        "permit2": PERMIT2,
        "quoter": QUOTER,
        "pool_manager": POOL_MANAGER,
        "pool_key": route.inner.as_json(),
        "pool_id": route.inner.pool_id,
        "token_in": route.token_in,
        "token_out": route.token_out,
        "zero_for_one": route.zero_for_one,
        "amount_in": str(VECTOR_AMOUNT_IN),
        "quoter_out": str(FORK_OUT),
        "slippage_bps": VECTOR_SLIPPAGE_BPS,
        "min_out": str(VECTOR_MIN_OUT),
        "commands": "0x" + bytes([uniswap.COMMAND_V4_SWAP]).hex(),
        "actions": "0x" + actions.hex(),
        "inputs": inputs,
        "deadline": VECTOR_DEADLINE,
        "calldata": data,
    }
    VECTOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTOR_PATH.write_text(json.dumps(vector, indent=2) + "\n")

    # and the file that was just written still describes THIS swap, read back through the decoder
    on_disk = json.loads(VECTOR_PATH.read_text())
    call = uniswap.decode_swap_calldata(on_disk["calldata"])
    assert call["pool_key"].pool_id == on_disk["pool_id"]
    assert call["amount_in"] == VECTOR_AMOUNT_IN and call["min_out"] == VECTOR_MIN_OUT
    assert call["settle_token"] == USDC and call["settle_max"] == VECTOR_AMOUNT_IN
    assert call["take_token"] == ZERO and call["take_min"] == VECTOR_MIN_OUT
    assert call["hook_data"] == "0x" and call["deadline"] == VECTOR_DEADLINE
    assert call["zero_for_one"] is False


def test_the_swap_takes_the_output_to_the_user_and_settles_the_input(registry):
    """SETTLE_ALL names the INPUT and TAKE_ALL the OUTPUT. Swap them and the router settles a
    currency it holds nothing of; the pair is sorted, so this is not the same as currency0/1."""
    route = uniswap.route_for(USDC, "ETH")
    call = uniswap.decode_swap_calldata(
        uniswap.swap_calldata(route, VECTOR_AMOUNT_IN, VECTOR_MIN_OUT, VECTOR_DEADLINE)
    )
    assert call["settle_token"] == route.token_in == USDC
    assert call["take_token"] == route.token_out == ZERO


@pytest.mark.parametrize("min_out", [0, -1])
def test_a_zero_slippage_bound_is_refused_and_never_defaulted(registry, min_out):
    """⛔ `amountOutMinimum` is the ONLY thing between the user and a sandwich here: the output
    goes to their own wallet, so nothing of ours would ever notice a bad fill."""
    route = uniswap.route_for(USDC, "ETH")
    with pytest.raises(uniswap.RouteError, match="no bound at all"):
        uniswap.swap_calldata(route, VECTOR_AMOUNT_IN, min_out, VECTOR_DEADLINE)


def test_the_deadline_is_twenty_minutes_and_is_not_read_from_the_clock_twice(registry, monkeypatch):
    monkeypatch.setattr(settings, "uniswap_deadline_s", 1200)
    assert uniswap.swap_deadline(1_000_000) == 1_000_000 + 1200
    monkeypatch.setattr(settings, "uniswap_deadline_s", 5)  # below the floor
    assert uniswap.swap_deadline(1_000_000) == 1_000_000 + 60


# ------------------------------------------------------------------------- the quote itself


async def test_the_quote_is_a_swap_step_with_the_next_handshake(
    client, user, registry, chain, frozen_deadline, armed_eth
):
    q = await quote(client, user)
    assert q["mode"] == "uniswap" and q["step"] == "swap" and q["armed"] is True
    est = q["estimate"]
    assert est["out_units"] == str(FORK_OUT)
    assert est["min_out_units"] == str(VECTOR_MIN_OUT)
    assert est["src"] == {
        "chain_id": 1,
        "token": USDC,
        "symbol": "USDC",
        "decimals": 6,
        "amount": str(VECTOR_AMOUNT_IN),
    }
    assert est["usd"] == pytest.approx(FORK_OUT / 1e18 * 2500.0)
    assert est["price_impact_bps"] == 34  # ~the pool's own 0.30% fee at this size
    assert q["swap_tx"] == {
        "chain_id": 1,
        "to": UNIVERSAL_ROUTER,
        "data": VECTOR_CALLDATA,
        "value": "0",  # an ERC-20 is pulled through Permit2, never attached
    }
    # step 2: quote again in the TARGET token with what actually arrived. The floor here is the
    # bound, not a promise — the client re-quotes on the receipt.
    assert q["next"] == {"src_chain_id": 1, "src_token": ZERO, "amount": str(VECTOR_MIN_OUT)}
    assert q["route"]["router"] == UNIVERSAL_ROUTER and q["route"]["permit2"] == PERMIT2
    assert q["route"]["pool_key"]["hooks"] == ZERO  # the CANONICAL pool, not one of ours
    assert "two steps" in q["note"]


async def test_the_row_records_the_route_the_pool_the_quoter_answer_and_the_calldata(
    client, user, registry, chain, frozen_deadline, armed_eth, mock_db
):
    """A quote that cannot say what it priced cannot vouch for what arrives."""
    q = await quote(client, user)
    row = await mock_db["pgasme_test"].quotes.find_one({"_id": q["quote_id"]})
    assert row["mode"] == "uniswap" and row["step"] == "swap"
    assert row["route"] == q["route"]
    assert row["pool_id"] == uniswap.route_for(USDC, "ETH").inner.pool_id
    assert row["pool_key"]["fee"] == 3000 and row["pool_key"]["hooks"] == ZERO
    assert row["quoter_out_units"] == str(FORK_OUT)
    assert row["min_out_units"] == str(VECTOR_MIN_OUT)
    assert row["swap_deadline"] == VECTOR_DEADLINE
    assert row["swap_calldata"] == VECTOR_CALLDATA
    assert row["swap_tx"] == q["swap_tx"] and row["next"] == q["next"]
    # …and nothing that belongs to the hook shape
    assert row.get("deposit_ref") is None and row.get("tx") is None


async def test_the_pool_is_read_before_the_price_is(
    client, user, registry, chain, frozen_deadline, armed_eth
):
    """The Quoter's refusal for an uninitialised pool is "no depth for this size", which blames
    the amount for something the amount cannot fix. So liveness is read first."""
    await quote(client, user)
    kinds = [chain.kind(c["data"]) for c in chain.calls]
    assert kinds[:3] == ["extsload", "extsload", "quote"]
    assert chain.calls[0]["to"] == POOL_MANAGER and chain.calls[2]["to"] == QUOTER


async def test_a_native_input_pays_with_value_and_needs_no_approval(
    client, user, registry, chain, frozen_deadline, monkeypatch
):
    """ETH → bDAI. `DeltaResolver._settle` pays a native input from the router's own msg.value;
    there is no Permit2 in it at all."""
    registry([native_row()])
    chain.out = 300 * 10**18  # 0.1 ETH buys ~300 DAI, which clears the deposit floor
    monkeypatch.setattr(settings, "ingress_armed", True)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_dai", PUBKEY)
    r = await client.post(
        "/v1/quote",
        json={"src_chain_id": 1, "src_token": ZERO, "amount": str(10**17), "target_asset": "DAI"},
        headers=user["headers"],
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert q["mode"] == "uniswap" and q["swap_tx"]["value"] == str(10**17)
    assert q["approvals"] == []
    assert q["next"]["src_token"] == DAI_TOKEN
    assert "erc20_allowance" not in [chain.kind(c["data"]) for c in chain.calls]


# --------------------------------------------------------------------------- the approvals


async def test_both_allowances_are_read_and_only_the_short_ones_are_offered(
    client, user, registry, chain, frozen_deadline, armed_eth
):
    chain.erc20_allowance = 0
    chain.permit2_amount = 0
    q = await quote(client, user)
    names = [a["name"] for a in q["approvals"]]
    assert names == ["approval", "permit_tx"]  # in the order they must be sent
    erc20, permit = q["approvals"]
    assert erc20["to"] == USDC and erc20["spender"] == PERMIT2
    assert erc20["amount"] == str(VECTOR_AMOUNT_IN) and erc20["value"] == "0"
    assert erc20["data"] == uniswap.erc20_approve_calldata(PERMIT2, VECTOR_AMOUNT_IN)
    assert permit["to"] == PERMIT2 and permit["spender"] == UNIVERSAL_ROUTER
    assert permit["expiration"] == VECTOR_DEADLINE
    assert permit["data"] == uniswap.permit2_approve_calldata(
        USDC, UNIVERSAL_ROUTER, VECTOR_AMOUNT_IN, VECTOR_DEADLINE
    )
    # both already granted, and generously: nothing to send
    chain.erc20_allowance = 2**160 - 1
    chain.permit2_amount = 2**160 - 1
    assert (await quote(client, user))["approvals"] == []


async def test_a_permit2_allowance_that_is_large_enough_but_expired_is_still_short(
    client, user, registry, chain, frozen_deadline, armed_eth
):
    """An allowance that expires before the swap's own deadline is exactly as useful as one that
    was never granted — reading only the amount is how that bug is written."""
    chain.permit2_amount = 2**160 - 1
    chain.permit2_expiration = VECTOR_DEADLINE - 1
    q = await quote(client, user)
    assert [a["name"] for a in q["approvals"]] == ["permit_tx"]

    chain.permit2_expiration = VECTOR_DEADLINE + 1
    assert (await quote(client, user))["approvals"] == []


async def test_a_short_but_non_zero_allowance_is_zeroed_first_because_usdt_demands_it(
    client, user, registry, chain, frozen_deadline, armed_eth
):
    """⛔ USDT's `approve` is `require(!(value != 0 && allowed[msg.sender][spender] != 0))`, and
    USDT is one of the source tokens this route is for. Raising a short allowance in one call
    reverts on it, and a UI with no way forward is worse than one extra 29k-gas transaction."""
    chain.erc20_allowance = VECTOR_AMOUNT_IN - 1
    approvals = (await quote(client, user))["approvals"]
    assert [a["name"] for a in approvals] == ["approval_reset", "approval"]
    reset = approvals[0]
    assert reset["to"] == USDC and reset["spender"] == PERMIT2 and reset["amount"] == "0"
    assert reset["data"] == uniswap.erc20_approve_calldata(PERMIT2, 0, allow_zero=True)
    assert approvals[1]["amount"] == str(VECTOR_AMOUNT_IN)

    # nothing to zero when there is nothing there — the common first-approval case is ONE tx
    chain.erc20_allowance = 0
    assert [a["name"] for a in (await quote(client, user))["approvals"]] == ["approval"]

    chain.erc20_allowance = VECTOR_AMOUNT_IN
    assert (await quote(client, user))["approvals"] == []


def test_an_approval_of_nothing_is_refused_unless_it_is_the_reset(registry):
    with pytest.raises(uniswap.RouteError, match="positive amount"):
        uniswap.erc20_approve_calldata(PERMIT2, 0)
    assert uniswap.erc20_approve_calldata(PERMIT2, 0, allow_zero=True).startswith("0x095ea7b3")


async def test_an_unreadable_allowance_also_gets_the_reset_it_cannot_rule_out(
    client, user, registry, chain, frozen_deadline, armed_eth
):
    """We do not know the allowance is zero, so we do not act as if it were."""
    chain.fail["erc20_allowance"] = ethpipe.RpcError("every endpoint refused")
    names = [a["name"] for a in (await quote(client, user))["approvals"]]
    assert names == ["approval_reset", "approval"]

    # …and the two reads fail INDEPENDENTLY: a Permit2 outage says nothing about the token's own
    # allowance, so it must not conjure a reset the token allowance has already ruled out.
    chain.fail.clear()
    chain.fail["permit2_allowance"] = ethpipe.RpcError("every endpoint refused")
    names = [a["name"] for a in (await quote(client, user))["approvals"]]
    assert names == ["permit_tx"]


@pytest.mark.parametrize("broken", ["erc20_allowance", "permit2_allowance"])
async def test_an_allowance_nobody_answered_is_never_already_approved(
    client, user, registry, chain, frozen_deadline, armed_eth, broken
):
    """⛔ The SAFE direction, and it says so. Skipping an approval we cannot prove exists is a
    reverted swap the user paid gas for; offering one that already exists is a cheap transaction
    their wallet will tell them about."""
    chain.fail[broken] = ethpipe.RpcError("every endpoint refused")
    q = await quote(client, user)
    assert q["approvals"], "an unreadable allowance must not silently skip its approval"
    assert "could not read" in q["note"]

    # a short answer is not a small allowance either
    chain.fail.clear()
    chain.short.add(broken)
    q = await quote(client, user)
    assert q["approvals"] and "could not read" in q["note"]


# ---------------------------------------------------------------------------- the refusals


async def test_a_pool_that_was_never_initialised_is_refused_naming_the_pair(
    client, user, registry, chain, armed_eth
):
    chain.sqrt_price = 0
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 400
    assert "never been initialised" in r.json()["detail"] and "USDC → ETH" in r.json()["detail"]


async def test_a_pool_with_no_liquidity_is_refused_rather_than_quoted(
    client, user, registry, chain, armed_eth
):
    chain.liquidity = 0
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 400 and "no liquidity" in r.json()["detail"]


@pytest.mark.parametrize("broken", ["slot0", "liquidity"])
async def test_an_unreadable_pool_is_503_and_never_a_dead_pool(
    client, user, registry, chain, armed_eth, broken
):
    chain.fail[broken] = ethpipe.RpcError("every endpoint refused")
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 503 and "could not read the pool" in r.json()["detail"]

    chain.fail.clear()
    chain.short.add(broken)
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 503, "a short answer is not a zero"


async def test_an_unreadable_quoter_is_503_and_never_a_zero(
    client, user, registry, chain, armed_eth
):
    chain.fail["quote"] = ethpipe.RpcError("every endpoint refused")
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 503 and "could not read a price" in r.json()["detail"]


async def test_a_pool_that_quotes_nothing_is_refused_rather_than_handed_over_as_zero(
    client, user, registry, chain, armed_eth
):
    chain.out = 0
    r = await client.post("/v1/quote", json=body(), headers=user["headers"])
    assert r.status_code == 400 and "no depth for this size" in r.json()["detail"]


async def test_above_the_routes_cap_and_below_its_floor_are_both_refused(
    client, user, registry, chain, armed_eth
):
    r = await client.post("/v1/quote", json=body(amount="2000000001"), headers=user["headers"])
    assert r.status_code == 400 and "above this route's cap" in r.json()["detail"]
    r = await client.post("/v1/quote", json=body(amount="999999"), headers=user["headers"])
    assert r.status_code == 400 and "below this route's minimum" in r.json()["detail"]


async def test_an_unregistered_pair_names_the_pair_it_refused(
    client, user, registry, chain, armed_eth
):
    r = await client.post(
        "/v1/quote",
        json=body(src_token=DAI_TOKEN, route="uniswap"),
        headers=user["headers"],
    )
    assert r.status_code == 400
    assert "is not a registered source for ETH on the Uniswap route" in r.json()["detail"]


async def test_the_route_is_ethereum_only_and_a_closed_flag_is_a_409(
    client, user, registry, chain, armed_eth, monkeypatch
):
    r = await client.post(
        "/v1/quote", json=body(src_chain_id=56, route="uniswap"), headers=user["headers"]
    )
    assert r.status_code == 400 and "Ethereum-only" in r.json()["detail"]
    monkeypatch.setattr(settings, "ingress_uniswap", False)
    uniswap.clear_cache()
    r = await client.post("/v1/quote", json=body(route="uniswap"), headers=user["headers"])
    assert r.status_code == 409 and "not available" in r.json()["detail"]


# ------------------------------------------------------- a swap is never a deposit


async def test_an_unarmed_quote_still_hands_over_the_swap_and_says_the_deposit_is_not_ready(
    client, user, registry, chain, frozen_deadline, monkeypatch
):
    """Step 1 moves the USER's money inside the USER's wallet. The arming flags are about ours."""
    monkeypatch.setattr(settings, "ingress_armed", False)
    q = await quote(client, user)
    assert q["armed"] is False
    assert q["swap_tx"]["data"] == VECTOR_CALLDATA
    assert "the deposit step is not available yet" in q["note"]


async def test_the_kill_switch_does_not_strand_a_user_mid_route(
    client, user, registry, chain, frozen_deadline, armed_eth, monkeypatch, tmp_path
):
    """A two-step quote issues no pipe transaction, so the kill switch has nothing to stop here —
    and the quote SAYS the deposit step is paused rather than pretending it is ready."""
    stop = tmp_path / "pgasme.stop"
    stop.write_text("")
    monkeypatch.setattr(settings, "stop_file", str(stop))
    q = await quote(client, user)
    assert q["swap_tx"]["data"] == VECTOR_CALLDATA
    assert "the deposit step is paused" in q["note"]
    # …and step 2 really is refused
    r = await client.post(
        "/v1/deposits",
        json={"quote_id": q["quote_id"], "src_tx_hash": "0x" + "11" * 32},
        headers=user["headers"],
    )
    assert r.status_code == 409


async def test_arming_a_two_step_quote_is_refused_the_way_a_swap_is(
    client, user, registry, chain, frozen_deadline, armed_eth
):
    q = await quote(client, user)
    r = await client.post(f"/v1/quote/{q['quote_id']}/arm", headers=user["headers"])
    assert r.status_code == 400 and "a swap is not a deposit" in r.json()["detail"]


async def test_a_swap_transaction_is_never_registered_as_a_deposit(
    client, user, registry, chain, frozen_deadline, armed_eth, mock_db
):
    """⛔ A row opened on a swap hash is an open claim no pipe lock can ever match — and it takes
    `uniq_src_tx_hash` from the deposit that follows it."""
    q = await quote(client, user)
    r = await client.post(
        "/v1/deposits",
        json={"quote_id": q["quote_id"], "src_tx_hash": "0x" + "ab" * 32},
        headers=user["headers"],
    )
    assert r.status_code == 400 and "a swap is not a deposit" in r.json()["detail"]
    assert await mock_db["pgasme_test"].deposits.count_documents({}) == 0


# ---------------------------------------------------------------- what the registry allows


def test_a_row_needs_no_gateway_pool_and_an_old_one_is_ignored(registry):
    """Nothing of ours is deployed, so requiring a gateway pool would refuse every route on this
    box — and a `gateway_pool_key` left in an operator's env from the hook era is not a reason to
    refuse either."""
    r = uniswap.route_for(USDC, "ETH")
    assert r is not None and r.gateway is None
    assert r.token_in == USDC and r.token_out == ZERO and r.native_in is False

    registry([pool_row(gateway_pool_key={"currency0": ZERO, "currency1": USDC, "fee": 0, "tickSpacing": 1, "hooks": "0x1111111111111111111111111111111111112888"})])
    r = uniswap.route_for(USDC, "ETH")
    assert r is not None and r.gateway is None


def test_a_pool_with_a_hook_is_never_the_canonical_pool(registry):
    registry([pool_row(inner_pool_key=inner_key(hooks="0x1111111111111111111111111111111111112888"))])
    with pytest.raises(uniswap.RouteError, match="HOOK-LESS"):
        uniswap.routes()


def test_the_route_is_usable_without_the_hook_addresses_and_says_so_when_it_is_not(
    registry, monkeypatch
):
    """`addresses_ok` asks about THIS shape: the two-step route never calls a contract of ours,
    so requiring the hook and PgasRouter addresses would close it for no reason."""
    monkeypatch.setattr(settings, "uniswap_hook", "")
    monkeypatch.setattr(settings, "uniswap_router", "")
    assert uniswap.addresses_ok() == "" and uniswap.configured() is True
    monkeypatch.setattr(settings, "uniswap_universal_router", "")
    assert uniswap.addresses_ok() == "PGAS_UNISWAP_UNIVERSAL_ROUTER is not set"
    assert uniswap.configured() is False


def test_the_pool_state_slot_is_the_one_v4_core_uses(registry):
    """`keccak256(abi.encodePacked(poolId, 6))`, cross-checked against foundry at the pinned
    block: the ETH/USDC 0.30% pool's slot0 read back sqrtPriceX96 and liquidity that the V4
    Quoter's own answer is consistent with."""
    route = uniswap.route_for(USDC, "ETH")
    assert route.inner.pool_id == (
        "0xdce6394339af00981949f5f3baf27e3610c76326a700af57e4b3e3ae4977f78d"
    )
    slot = uniswap.pool_state_slot(route.inner.pool_id)
    assert (
        "0x" + slot.hex() == "0x7ced19e67a5796b90f206e133d76f6c105cb78d4f9f3e2074d49c272a8094b4e"
    )
    assert uniswap.extsload_calldata(slot).startswith("0x1e2eaeaf")


def test_price_impact_is_measured_against_the_pools_mid_and_never_negative(registry):
    route = uniswap.route_for(USDC, "ETH")
    assert uniswap.price_impact_bps(route, VECTOR_AMOUNT_IN, FORK_OUT, FORK_SQRT_PRICE_X96) == 34
    # a quote at or above mid is integer rounding, not a gift
    assert uniswap.price_impact_bps(route, VECTOR_AMOUNT_IN, 10**18, FORK_SQRT_PRICE_X96) == 0
    # and nothing is invented from an unreadable price
    assert uniswap.price_impact_bps(route, VECTOR_AMOUNT_IN, FORK_OUT, 0) is None
