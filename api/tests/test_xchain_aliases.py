"""The compatibility surface of the 2026-09-10 rename, in ONE file so it is one thing to delete.

`xchain` is the name of record for the cross-chain mode; `route_chain_id` is the name of record
for the router's own chain id; `PGAS_XCHAIN_*` are the names of record for its settings. For one
release the older spellings keep working, because three of them are not ours to change at will:

  * rows already in Mongo carry the old mode value and the old chain-id field (`quotes`,
    `deposits`) — a deposit registered yesterday must still arm, register and render;
  * an SPA already loaded in a browser reads the old chain-id field off /v1/dex/chains;
  * the box's own environment file carries the old settings keys.

Every one of those is read through ONE implementation (`xchain.norm_mode`, `LEGACY_*`,
`config._router_env`), so this file is what proves the alias exists and what fails loudly the
day someone removes it deliberately.
"""

from __future__ import annotations

import time

from conftest import USDC_ARB
from test_quote_deposits import FakeRouter

from pgasme import xchain
from pgasme.config import LEGACY_CHAIN_ID_FIELD, LEGACY_MODE, LEGACY_STATUS_FIELD, Settings

ZERO = "0x0000000000000000000000000000000000000000"


def test_norm_mode_is_the_only_reader_of_the_mode() -> None:
    assert xchain.MODE == "xchain"
    assert xchain.norm_mode(LEGACY_MODE) == "xchain"  # the router's own older name
    assert xchain.norm_mode("xchain") == "xchain"
    assert xchain.norm_mode(None) == "xchain"  # rows from before the same-chain modes existed
    assert xchain.norm_mode("") == "xchain"
    assert xchain.norm_mode("direct") == "direct"  # the other two are not aliased
    assert xchain.norm_mode("swap") == "swap"


def test_the_deprecated_env_keys_are_still_read_and_the_neutral_one_wins(monkeypatch) -> None:
    """The box was provisioned before the rename. Its keys must not go dark under it."""
    legacy = f"PGAS_{LEGACY_MODE.upper()}_BASE"
    monkeypatch.setenv(legacy, "https://legacy.example/v1.0")
    monkeypatch.setenv(f"PGAS_{LEGACY_MODE.upper()}_TIMEOUT_S", "7")
    assert Settings().xchain_base == "https://legacy.example/v1.0"
    assert Settings().xchain_timeout_s == 7.0
    monkeypatch.setenv("PGAS_XCHAIN_BASE", "https://neutral.example/v1.0")
    assert Settings().xchain_base == "https://neutral.example/v1.0"


async def test_a_deposit_stored_under_the_old_mode_renders_as_xchain(client, user, mock_db) -> None:
    """The row is not rewritten (never edit history to fix a ledger) — it is READ tolerantly."""
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "legacy1",
            "account_id": user["account_id"],
            "asset": "ETH",
            "mode": LEGACY_MODE,
            "status": "credited",
            "src": {"chain_id": 42161, "token": ZERO, "amount": "1"},
            "quote_id": "q0",
            "eth": {"value_units": "1", "relayer_fee_units": "1"},
            "value_groth": 1,
            "created_at": 0.0,
            "updated_at": 0.0,
        }
    )
    one = (await client.get("/v1/deposits/legacy1", headers=user["headers"])).json()
    assert one["mode"] == "xchain"
    acct = (await client.get("/v1/account", headers=user["headers"])).json()
    assert [d["mode"] for d in acct["deposits"] if d["_id"] == "legacy1"] == ["xchain"]
    # the stored row still says what it said: the alias is a read, not a migration
    row = await mock_db["pgasme_test"].deposits.find_one({"_id": "legacy1"})
    assert row["mode"] == LEGACY_MODE


async def test_a_quote_stored_under_the_old_names_still_arms(
    client, user, armed_eth, mock_db, monkeypatch
) -> None:
    """A quote priced before the rename carries the old mode AND the old chain-id field. /arm has
    to find the router's chain id in it, and must not leak either spelling into `estimate.src`."""
    monkeypatch.setattr(xchain, "create_tx", FakeRouter())
    monkeypatch.setattr("pgasme.config.settings.min_deposit_wei", 10**15)
    now = time.time()
    await mock_db["pgasme_test"].quotes.insert_one(
        {
            "_id": "legacyq",
            "account_id": user["account_id"],
            "address": user["address"],
            "asset": "ETH",
            "mode": LEGACY_MODE,
            "src": {
                "chain_id": 42161,
                "token": USDC_ARB,
                "symbol": "USDC",
                "decimals": 6,
                "amount": "10000000",
                LEGACY_CHAIN_ID_FIELD: 42161,
            },
            "out_units": "3774812168855201",
            "value_units": "3774812160000000",
            "relayer_fee_units": "8855201",
            "value_groth": 377481,
            "metadata": "0x1122334455",
            "armed": True,
            "at": now,
            "expires_at": now + 900,
            "estimate": {},
        }
    )
    r = await client.post("/v1/quote/legacyq/arm", headers=user["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "xchain" and body["order_id"] == "0x" + "77" * 32
    assert body["tx"]["chain_id"] == 42161
    # neither the old nor the new internal field is published back to the client
    assert LEGACY_CHAIN_ID_FIELD not in body["estimate"]["src"]
    assert "route_chain_id" not in body["estimate"]["src"]


async def test_a_deposit_stored_under_the_old_status_key_renders_as_route_status(
    client, user, mock_db
) -> None:
    """The router's status field was renamed with everything else; rows written before it were
    NOT (never edit history to fix a ledger). One reader maps it — `routers/account.public_deposit`,
    which is what /v1/account and /v1/deposits/{id} both render through — and the old spelling
    never reaches the wire."""
    assert LEGACY_STATUS_FIELD == f"{LEGACY_MODE}_status"
    now = time.time()
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "legacy2",
            "account_id": user["account_id"],
            "asset": "ETH",
            "mode": LEGACY_MODE,
            "status": "credited",
            LEGACY_STATUS_FIELD: "Fulfilled",
            "src": {"chain_id": 42161, "token": ZERO, "amount": "1"},
            "quote_id": "q0",
            "eth": {"value_units": "1", "relayer_fee_units": "1"},
            "value_groth": 1,
            "created_at": now,
            "updated_at": now,
        }
    )
    one = (await client.get("/v1/deposits/legacy2", headers=user["headers"])).json()
    assert one["route_status"] == "Fulfilled" and LEGACY_STATUS_FIELD not in one
    acct = (await client.get("/v1/account", headers=user["headers"])).json()
    row = next(d for d in acct["deposits"] if d["_id"] == "legacy2")
    assert row["route_status"] == "Fulfilled" and LEGACY_STATUS_FIELD not in row
    # the stored row is untouched: the alias is a read, not a migration
    stored = await mock_db["pgasme_test"].deposits.find_one({"_id": "legacy2"})
    assert stored[LEGACY_STATUS_FIELD] == "Fulfilled" and "route_status" not in stored
    # …and a deposit that never had a router status gains no null one — a blank `route_status`
    # would read as "unknown", not as "not applicable"
    await mock_db["pgasme_test"].deposits.insert_one(
        {
            "_id": "plain1",
            "account_id": user["account_id"],
            "asset": "ETH",
            "mode": "direct",
            "status": "credited",
            "src": {"chain_id": 1, "token": ZERO, "amount": "1"},
            "quote_id": "q1",
            "eth": {"value_units": "1", "relayer_fee_units": "1"},
            "value_groth": 1,
            "created_at": now,
            "updated_at": now,
        }
    )
    plain = (await client.get("/v1/deposits/plain1", headers=user["headers"])).json()
    assert plain["mode"] == "direct" and "route_status" not in plain
