"""Shared fixtures: a mongomock Motor client injected through pgasme.db.set_client, an ASGI
httpx client, a real eth_account key that signs a real EIP-4361 message, destination proofs,
the DLN response shape recorded live on 2026-09-09 and a fake Ethereum RPC pool."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

# The settings singleton reads the environment at import: pin the test posture BEFORE importing pgasme.
for _k in (
    "PGAS_TG_LIVE",
    "PGAS_INGRESS_ARMED",
    "PGAS_BEAM_PIPE_PUBKEY",
    "PGAS_BEAM_PIPE_PUBKEY_ETH",
    "PGAS_BEAM_PIPE_PUBKEY_DAI",
    "PGAS_BEAM_PIPE_PUBKEY_WBTC",
    "PGAS_PAYOUT_DIRECT_ENABLED",
    "PGAS_PAYOUT_INSTANT_ENABLED",
):
    os.environ.pop(_k, None)
os.environ.update(
    {
        "PGAS_WORKERS_ENABLED": "0",
        "PGAS_DEV_ENDPOINTS": "1",
        # PGAS_ENV=test is not a lax environment: config.py refuses to boot outside `dev`
        # with a placeholder / short secret, so the suite must pin real-shaped ones.
        "PGAS_JWT_SECRET": "test-secret-" + "x" * 32,
        "PGAS_ACCOUNT_SALT": "test-salt-" + "y" * 32,
        "PGAS_ENV": "test",
        "PGAS_STOP_FILE": "/nonexistent/pgasme.stop",
    }
)

import httpx
import pytest
import pytest_asyncio
from eth_abi import encode
from eth_account import Account as EthAccount
from eth_account.messages import encode_defunct
from mongomock_motor import AsyncMongoMockClient
from siwe import SiweMessage

from pgasme import assets, dln, ethpipe, ledger, scanner, tg, workers
from pgasme import db as dbmod
from pgasme.config import settings
from pgasme.main import create_app
from pgasme.routers.destinations import proof_message

PUBKEY = "02" + "ab" * 32  # a 33-byte "pipe pubkey" for tests
OTHER_PUBKEY = "03" + "cd" * 32
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
DLN_CHAINS = [
    {"chainId": 1, "originalChainId": 1, "chainName": "Ethereum"},
    {"chainId": 42161, "originalChainId": 42161, "chainName": "Arbitrum"},
    {"chainId": 8453, "originalChainId": 8453, "chainName": "Base"},
    {"chainId": 100000013, "originalChainId": 1514, "chainName": "Story"},
    {"chainId": 7565164, "originalChainId": 7565164, "chainName": "Solana"},
]
# GET /dln/order/create-tx for 10 USDC (Arbitrum) → ETH (mainnet), recorded 2026-09-09, trimmed
DLN_ESTIMATE: dict[str, Any] = {
    "estimation": {
        "srcChainTokenIn": {
            "chainId": 42161,
            "address": USDC_ARB.lower(),
            "name": "USD Coin",
            "symbol": "USDC",
            "decimals": 6,
            "amount": "10000000",
            "approximateOperatingExpense": "507296",
            "approximateUsdValue": 10,
        },
        "dstChainTokenOut": {
            "chainId": 1,
            "address": "0x0000000000000000000000000000000000000000",
            "name": "Ethereum",
            "symbol": "ETH",
            "decimals": 18,
            "amount": "3774812168855201",
            "recommendedAmount": "3774812168855201",
            "approximateUsdValue": 9.456,
        },
        "costsDetails": [
            {
                "chain": "42161",
                "type": "DlnProtocolFee",
                "amountIn": "10000000",
                "amountOut": "9996000",
            }
        ],
        "recommendedSlippage": 0.3,
    },
    "tx": {
        "data": "0xb9303701" + "00" * 64,
        "to": "0xeF4fB24aD0916217251F553c0596F8Edc630EB66",
        "value": "1000000000000000",
    },
    "order": {
        "approximateFulfillmentDelay": 12,
        "salt": 1788957303860,
        "metadata": "0x0101000000a0bd070000000000000000000000000000000000a11e6bf22b690d0000000000000000000000000001020304050000000000000000000000000000000000",
    },
    "orderId": "0x69d0d6154cbcb3056014c40454c0cbbd42aa2381e5bb41a3ef518019fdcc0126",
    "fixFee": "1000000000000000",
    "protocolFee": "4000",
    "estimatedTransactionFee": {"total": "16640000000000"},
}


# GET /chain/estimation and /chain/transaction for 5 USDC → ETH on MAINNET (single-chain swap,
# mode "swap"), recorded live 2026-09-09 on dln.debridge.finance, trimmed. The transaction answer
# carries the same fields FLAT (no "estimation" wrapper) plus `tx`, and no allowanceTarget.
USDC_ETH = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
SWAP_TOKEN_IN = {
    "address": USDC_ETH.lower(),
    "name": "USD Coin",
    "symbol": "USDC",
    "decimals": 6,
    "amount": "5000000",
    "approximateUsdValue": 5,
}
SWAP_TOKEN_OUT = {
    "address": "0x0000000000000000000000000000000000000000",
    "name": "Ethereum",
    "symbol": "ETH",
    "decimals": 18,
    "minAmount": "1993120542274040",
    "amount": "1999122712146377",
    "approximateUsdValue": 4.991652,
}
SWAP_COSTS = [
    {
        "chain": "1",
        "tokenIn": "0x0000000000000000000000000000000000000000",
        "tokenOut": "0x0000000000000000000000000000000000000000",
        "amountIn": "2000723290779000",
        "amountOut": "1999122712146377",
        "type": "SingleChainSwapProtocolFee",
        "payload": {"feeAmount": "1600578632623", "feeBps": "8"},
    },
    {
        "chain": "1",
        "tokenIn": "0x0000000000000000000000000000000000000000",
        "tokenOut": "0x0000000000000000000000000000000000000000",
        "amountIn": "1999122712146377",
        "amountOut": "1993120542274040",
        "type": "SingleChainSwapEstimatedSlippage",
        "payload": {"feeAmount": "6002169872337", "feeBps": "30"},
    },
]
DLN_SWAP_ESTIMATION: dict[str, Any] = {
    "estimation": {
        "tokenIn": SWAP_TOKEN_IN,
        "tokenOut": SWAP_TOKEN_OUT,
        "slippage": 0.3,
        "recommendedSlippage": 0.3,
        "protocolFee": "1600578632623",
        "protocolFeeApproximateUsdValue": 0.003993,
        "estimatedTransactionFee": {
            "total": "439326826112330",
            "details": {"gasLimit": "383410", "baseFee": "145840813"},
            "approximateUsdValue": 1.096087,
        },
        "costsDetails": SWAP_COSTS,
    }
}
DLN_SWAP_TX: dict[str, Any] = {
    "tx": {
        "to": "0x663DC15D3C1aC63ff12E45Ab68FeA3F0a883C251",
        "data": "0x258c16ee" + "00" * 64,
        "value": "0",
    },
    "tokenIn": SWAP_TOKEN_IN,
    "tokenOut": SWAP_TOKEN_OUT,
    "slippage": 0.3,
    "protocolFee": "1600578632623",
    "estimatedTransactionFee": {"total": "418647754266980"},
    "costsDetails": SWAP_COSTS,
}


def iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def sign_text(key: bytes, text: str) -> str:
    sig = EthAccount.sign_message(encode_defunct(text=text), private_key=key).signature
    return "0x" + bytes(sig).hex()


def siwe_message(
    address: str,
    nonce: str,
    *,
    domain: str = "localhost:5173",
    uri: str = "http://localhost:5173",
    chain_id: int = 1,
    statement: str | None = None,
) -> str:
    msg = SiweMessage(
        domain=domain,
        address=address,
        uri=uri,
        version="1",
        chain_id=chain_id,
        nonce=nonce,
        issued_at=iso_now(),
        statement=statement or settings.siwe_statement,
    )
    return msg.prepare_message()


async def sign_in(client: httpx.AsyncClient, wallet: Any) -> dict[str, Any]:
    nonce = (await client.get("/v1/siwe/nonce")).json()["nonce"]
    message = siwe_message(wallet.address, nonce)
    r = await client.post(
        "/v1/siwe/verify", json={"message": message, "signature": sign_text(wallet.key, message)}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    out["headers"] = {"Authorization": f"Bearer {out['token']}"}
    out["wallet"] = wallet
    return out


async def add_destination(
    client: httpx.AsyncClient,
    user: dict[str, Any],
    dest: Any,
    *,
    signer: Any = None,
    kind: str = "proven",
    nonce: str | None = None,
) -> httpx.Response:
    if nonce is None:
        nonce = (await client.get("/v1/destinations/nonce", headers=user["headers"])).json()[
            "nonce"
        ]
    issued = iso_now()
    text = proof_message(user["account_id"], dest.address, nonce, issued)
    sig = sign_text((signer or dest).key, text)
    body = {
        "address": dest.address,
        "kind": kind,
        "nonce": nonce,
        "issued": issued,
        "signature": sig,
    }
    return await client.post("/v1/destinations", json=body, headers=user["headers"])


def lock_log(
    pipe: str,
    msg_id: int,
    amount: int,
    fee: int,
    pubkey_hex: str,
    block: int,
    tx: str,
    log_index: int = 0,
) -> dict[str, Any]:
    data = encode(
        ["uint64", "uint256", "uint256", "bytes"], [msg_id, amount, fee, bytes.fromhex(pubkey_hex)]
    )
    return {
        "address": pipe,
        "topics": [ethpipe.NEWLOCAL_TOPIC],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block),
        "transactionHash": tx,
        "logIndex": hex(log_index),
    }


def fulfilled_log(
    order_id_hex: str,
    take_amount: int,
    receiver: str,
    tx: str,
    log_index: int = 1,
    external_call: bytes = b"\x01",
) -> dict[str, Any]:
    order = (
        7,
        b"\x11" * 20,
        42161,
        b"\x22" * 20,
        10_000_000,
        1,
        b"\x00" * 20,
        take_amount,
        bytes.fromhex(receiver[2:]),
        b"\x33" * 20,
        b"\x44" * 20,
        b"",
        b"",
        external_call,
    )
    data = encode(
        [scanner.ORDER_TUPLE, "bytes32", "uint256", "address", "address"],
        [order, bytes.fromhex(order_id_hex[2:]), take_amount, "0x" + "55" * 20, "0x" + "66" * 20],
    )
    return {
        "address": "0xeF4fB24aD0916217251F553c0596F8Edc630EB66",
        "topics": [scanner.FULFILLED_TOPIC],
        "data": "0x" + data.hex(),
        "transactionHash": tx,
        "logIndex": hex(log_index),
    }


class FakeRpc:
    """An Ethereum RPC pool with a scripted head, per-endpoint heads, logs, receipts, transactions
    and unreadable ranges. `PRIMARY` is the endpoint that answers head_from(); `heads` gives a
    per-endpoint head so a pinned read can be shown lagging behind the range it just answered."""

    PRIMARY = "https://rpc.test/primary"

    def __init__(self, head: int = 0) -> None:
        self.head = head
        self.heads: dict[str, int] = {}
        self.logs_: list[dict[str, Any]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.txs: dict[str, dict[str, Any]] = {}
        self.fail_ranges: set[tuple[int, int]] = set()
        self.calls: list[tuple[Any, ...]] = []

    async def block_number(self, prefer: str | None = None, pin: bool = False) -> int:
        if prefer is not None and prefer in self.heads:
            return self.heads[prefer]
        return self.head

    async def head_from(self) -> tuple[int, str]:
        return self.head, self.PRIMARY

    async def pool_heads(self) -> dict[str, int]:
        return dict(self.heads)

    async def logs(
        self,
        address: str,
        topics: list[Any],
        frm: int,
        to: int,
        prefer: str | None = None,
        pin: bool = False,
    ) -> list[dict[str, Any]]:
        self.calls.append(("logs", address, frm, to, prefer))
        for a, b in self.fail_ranges:
            if frm <= b and to >= a:
                raise ethpipe.RpcError(f"[{frm},{to}] unreadable")
        return [
            lg
            for lg in self.logs_
            if lg["address"].lower() == address.lower() and frm <= int(lg["blockNumber"], 16) <= to
        ]

    async def receipt(
        self, tx: str, prefer: str | None = None, pin: bool = False
    ) -> dict[str, Any] | None:
        return self.receipts.get(tx)

    async def transaction(
        self, tx: str, prefer: str | None = None, pin: bool = False
    ) -> dict[str, Any] | None:
        return self.txs.get(tx)


@pytest.fixture(autouse=True)
def mock_db() -> Any:
    client = AsyncMongoMockClient()
    dbmod.set_client(client, "pgasme_test")
    dln.clear_cache()
    assets.clear_price_cache()
    workers.clear_pool_cache()
    tg._last_sent.clear()
    return client


@pytest.fixture(autouse=True)
def dln_chains(monkeypatch: pytest.MonkeyPatch) -> None:
    async def chains(force: bool = False) -> list[dict[str, Any]]:
        return DLN_CHAINS

    monkeypatch.setattr(dln, "supported_chains", chains)


@pytest_asyncio.fixture
async def client() -> Any:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def wallet() -> Any:
    return EthAccount.create()


@pytest_asyncio.fixture
async def user(client: httpx.AsyncClient, wallet: Any) -> dict[str, Any]:
    return await sign_in(client, wallet)


@pytest.fixture
def rpc(monkeypatch: pytest.MonkeyPatch) -> FakeRpc:
    fake = FakeRpc(head=2000)
    monkeypatch.setattr(workers, "get_rpc", lambda: fake)
    return fake


@pytest.fixture
def armed_eth(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "ingress_armed", True)
    monkeypatch.setattr(settings, "beam_pipe_pubkey_eth", PUBKEY)
    return PUBKEY


async def fund(user: dict[str, Any], asset: str, groth: int) -> None:
    await ledger.credit(user["account_id"], asset, groth, "test-credit")
