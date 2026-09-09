"""Sign-in with a real key and a real EIP-4361 message; the passive destination list.

The signed proof flow and its nonce route were REMOVED on 2026-09-09: a payout names any
address the user types (routers/withdrawals.py checks the checksum and the chain), so there is
nothing left for a destination to prove. What remains is the list the account page shows, the
signed-in wallet auto-added to it at SIWE, and removal.
"""

from __future__ import annotations

from conftest import add_destination, sign_in, sign_text, siwe_message
from eth_account import Account as EthAccount


async def test_nonce_endpoint(client):
    r = await client.get("/v1/siwe/nonce")
    body = r.json()
    assert r.status_code == 200 and len(body["nonce"]) >= 8
    assert "localhost:5173" in body["domains"] and body["statement"]


async def test_siwe_round_trip_issues_a_working_token(client, wallet):
    user = await sign_in(client, wallet)
    assert user["address"] == wallet.address and len(user["account_id"]) == 64
    r = await client.get("/v1/account", headers=user["headers"])
    assert r.status_code == 200
    assert r.json()["address"] == wallet.address
    assert set(r.json()["balances"]) == {"ETH", "DAI", "WBTC"}


async def test_siwe_rejects_foreign_domain(client, wallet):
    nonce = (await client.get("/v1/siwe/nonce")).json()["nonce"]
    msg = siwe_message(wallet.address, nonce, domain="evil.example", uri="https://evil.example")
    r = await client.post(
        "/v1/siwe/verify", json={"message": msg, "signature": sign_text(wallet.key, msg)}
    )
    assert r.status_code == 400 and "domain" in r.json()["detail"]


async def test_siwe_nonce_is_single_use(client, wallet):
    nonce = (await client.get("/v1/siwe/nonce")).json()["nonce"]
    msg = siwe_message(wallet.address, nonce)
    body = {"message": msg, "signature": sign_text(wallet.key, msg)}
    assert (await client.post("/v1/siwe/verify", json=body)).status_code == 200
    r = await client.post("/v1/siwe/verify", json=body)
    assert r.status_code == 400 and "nonce" in r.json()["detail"]


async def test_siwe_rejects_signature_by_another_key(client, wallet):
    nonce = (await client.get("/v1/siwe/nonce")).json()["nonce"]
    msg = siwe_message(wallet.address, nonce)
    other = EthAccount.create()
    r = await client.post(
        "/v1/siwe/verify", json={"message": msg, "signature": sign_text(other.key, msg)}
    )
    assert r.status_code == 401


async def test_locked_routes_need_a_token(client):
    for method, path in (
        ("GET", "/v1/account"),
        ("GET", "/v1/destinations"),
        ("POST", "/v1/quote"),
        ("POST", "/v1/withdrawals"),
        ("POST", "/v1/deposits"),
    ):
        r = await client.request(method, path, json={})
        assert r.status_code == 401, path
    r = await client.get("/v1/account", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


async def test_connected_wallet_is_a_destination(client, user):
    rows = (await client.get("/v1/destinations", headers=user["headers"])).json()["destinations"]
    assert [(d["address"], d["kind"]) for d in rows] == [(user["address"], "connected")]


async def test_there_is_no_proof_route_to_call_any_more(client, user):
    """Both halves of the old flow are gone — not disabled, gone."""
    r = await client.get("/v1/destinations/nonce", headers=user["headers"])
    # 405: nothing serves GET on that path any more (only DELETE /{address} matches its shape)
    assert r.status_code in (404, 405)
    dest = EthAccount.create()
    r = await client.post(
        "/v1/destinations",
        json={
            "address": dest.address,
            "kind": "proven",
            "nonce": "x" * 16,
            "issued": "2026-09-09T00:00:00Z",
            "signature": "0x" + "11" * 65,
        },
        headers=user["headers"],
    )
    assert r.status_code == 405


async def test_destination_delete_refused_while_a_payout_targets_it(client, user, mock_db):
    dest = EthAccount.create()
    await add_destination(client, user, dest)
    await mock_db["pgasme_test"].payout_requests.insert_one(
        {"_id": "r1", "account_id": user["account_id"], "W": dest.address, "status": "scheduled"}
    )
    r = await client.delete(f"/v1/destinations/{dest.address}", headers=user["headers"])
    assert r.status_code == 409
    await mock_db["pgasme_test"].payout_requests.update_one(
        {"_id": "r1"}, {"$set": {"status": "sent"}}
    )
    r = await client.delete(f"/v1/destinations/{dest.address}", headers=user["headers"])
    assert r.status_code == 200 and r.json()["removed"] == dest.address
    rows = (await client.get("/v1/destinations", headers=user["headers"])).json()["destinations"]
    assert dest.address not in {d["address"] for d in rows}


async def test_signed_in_wallet_cannot_be_removed(client, user):
    r = await client.delete(f"/v1/destinations/{user['address']}", headers=user["headers"])
    assert r.status_code == 409
