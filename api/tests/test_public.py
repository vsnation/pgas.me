"""Public routes: chains, tokens, assets, stats, health — DLN calls patched with recorded shapes."""

from __future__ import annotations

import time

from pgasme import dln, workers
from pgasme.assets import ASSETS


async def test_chains_map_ids_and_batch_balance(client):
    rows = (await client.get("/v1/dex/chains")).json()["chains"]
    by = {c["chain_id"]: c for c in rows}
    assert by[42161] == {
        "chain_id": 42161,
        "dln_chain_id": 42161,
        "name": "Arbitrum",
        "native_symbol": "ETH",
        "batch_balance": "0x50188692d5549386d102642036bab916b998c814",
    }
    assert by[8453]["batch_balance"] == "0x202eF28cA6D4d2B94C4Ea0534a8E6261581c70a4"
    assert by[1514] == {
        "chain_id": 1514,
        "dln_chain_id": 100000013,
        "name": "Story",
        "native_symbol": "IP",
    }
    assert "batch_balance" not in by[7565164]


async def test_tokens_native_first(client, monkeypatch):
    recorded = {
        "tokens": {
            "0xaf88d065e77c8cc2239327c5edb3a432268e5831": {
                "symbol": "USDC",
                "name": "USD Coin",
                "decimals": 6,
                "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
                "logoURI": "u",
            },
            "0x0000000000000000000000000000000000000000": {
                "symbol": "ETH",
                "name": "Ethereum",
                "decimals": 18,
                "address": "0x0000000000000000000000000000000000000000",
                "logoURI": "e",
                "isNative": True,
            },
        }
    }
    seen = []

    async def get(path, params=None, **kw):
        seen.append((path, params))
        return recorded

    monkeypatch.setattr(dln, "_get", get)
    body = (await client.get("/v1/dex/tokens", params={"chain_id": 1514})).json()
    assert seen == [("token-list", {"chainId": 100000013})]
    assert [t["symbol"] for t in body["tokens"]] == ["ETH", "USDC"]
    assert body["tokens"][1] == {
        "address": "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
        "symbol": "USDC",
        "name": "USD Coin",
        "decimals": 6,
        "logo": "u",
    }
    await client.get("/v1/dex/tokens", params={"chain_id": 1514})
    assert len(seen) == 1  # cached
    assert (await client.get("/v1/dex/tokens", params={"chain_id": 4242})).status_code == 400


async def test_dln_errors_surface_as_502_with_the_upstream_message(client, monkeypatch):
    async def chains(force=False):
        raise dln.DlnError("https://dln.debridge.finance/v1.0/supported-chains-info: ConnectError")

    monkeypatch.setattr(dln, "supported_chains", chains)
    r = await client.get("/v1/dex/chains")
    assert r.status_code == 502 and "ConnectError" in r.json()["detail"]


async def test_assets_table(client):
    rows = (await client.get("/v1/assets")).json()["assets"]
    assert [a["key"] for a in rows] == ["ETH", "DAI", "WBTC"]
    assert (
        rows[0]["pipe"] == ASSETS["ETH"].pipe and rows[0]["aid"] == 36 and rows[2]["decimals"] == 8
    )


async def test_stats_and_health(client, mock_db, monkeypatch):
    d = mock_db["pgasme_test"]
    await d.deposits.insert_one({"_id": "a", "status": "credited", "created_at": time.time() - 100})
    await d.deposits.insert_one({"_id": "b", "status": "failed", "created_at": time.time() - 100})
    await d.deposits.insert_one(
        {"_id": "c", "status": "credited", "created_at": time.time() - 3 * 86400}
    )
    await d.payout_requests.insert_one(
        {"_id": "r", "status": "sent", "updated_at": time.time() - 10}
    )

    async def ok():
        return {
            "height": 1,
            "shielded_outputs_total": 2,
            "shielded_outputs_per_24h": 3,
            "at": time.time(),
        }

    monkeypatch.setattr(workers, "fetch_pool_status", ok)
    body = (await client.get("/v1/stats")).json()
    assert body["deposits_24h"] == 1 and body["deposits_7d"] == 2 and body["payouts_24h"] == 1
    assert body["pool"]["shielded_outputs_total"] == 2 and body["pool"]["stale"] is False
    assert body["armed"] == {"ingress": False, "direct": False, "instant": False}
    h = (await client.get("/v1/health")).json()
    assert (
        h["ok"] is True
        and h["mongo"] is True
        and h["ingress_armed"] is False
        and h["env"] == "test"
    )
    assert (
        h["ingress_assets"] == {"ETH": False, "DAI": False, "WBTC": False} and h["paused"] is False
    )
