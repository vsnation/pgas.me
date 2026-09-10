"""`beam_height` on the public deposit and payout rows (T31b item 8 / T31 item I).

The admin gave us the bridge's own explorer — `…/#/explorer/bridge?tx={block_height}` — so a user
can watch the crossing that funds their wallet. It keys on the **Beam block height** of the
kernel, not on a txid, and the client must never compute or guess one: the API is the ONE writer
of that number and publishes it under ONE name on both kinds of row.

`null` until it is known is the whole contract. A link built on a height nobody recorded would
point at some other block's bridge traffic and read as evidence about this user's money.
"""

from __future__ import annotations

from typing import Any

from pgasme.routers.account import beam_height, public_deposit, public_request

CLAIM_KERNEL = "9f" * 32


def dep(**over: Any) -> dict[str, Any]:
    return {"_id": "d1", "account_id": "a1", "asset": "ETH", "status": "credited", **over}


def req(**over: Any) -> dict[str, Any]:
    return {
        "_id": "r1",
        "asset": "ETH",
        "status": "bridging",
        "amount_groth": 100,
        "W": "0x" + "11" * 20,
        **over,
    }


# ----------------------------------------------------------------- the one reader


def test_a_row_with_no_beam_side_kernel_yet_publishes_null_and_never_a_zero():
    """⛔ 0 is a block. A row that has not crossed yet must say "we do not know", because the
    client renders the link on presence — and `?tx=0` is a link to somebody else's block."""
    assert beam_height(dep()) is None
    assert beam_height(req()) is None
    assert public_deposit(dep())["beam_height"] is None
    assert public_request(req())["beam_height"] is None


def test_the_payout_publishes_the_beam_height_its_crossing_recorded():
    row = public_request(req(kernel="ab" * 32, beam_height_at_kernel=4_031_777))
    assert row["beam_height"] == 4_031_777


def test_the_deposit_publishes_the_height_its_claim_recorded():
    assert public_deposit(dep(claim_kernel=CLAIM_KERNEL, claim_height=4_030_012))["beam_height"] == 4_030_012


def test_a_height_that_is_not_a_positive_whole_block_is_not_a_height():
    """Every one of these has reached this codebase on a real row at some point. None of them is
    a block, and each of them would build a link to a block that is not this crossing's."""
    for bad in (None, 0, -1, "", "later", float("nan"), float("inf"), True, [4_030_012]):
        assert beam_height(req(beam_height_at_kernel=bad)) is None, bad
    assert beam_height(req(beam_height_at_kernel="4030012")) == 4_030_012  # Mongo hands back str


def test_one_reader_for_both_kinds_of_row_so_the_two_links_cannot_disagree():
    """Law 9. The deposit's claim and the payout's crossing are the same question asked of two
    collections; two readers would eventually answer it two ways."""
    assert beam_height({"beam_height_at_kernel": 5}) == 5
    assert beam_height({"claim_height": 5}) == 5
    assert beam_height({"beam_height": 5}) == 5


def test_an_explicit_height_wins_over_the_derived_one():
    """`beam_height_at_kernel` is the wallet's height when the kernel was FIRST SEEN — at most
    one poll interval late. If a writer ever records the kernel's own block, that is the truth
    and this reader must prefer it rather than keeping the approximation forever."""
    assert beam_height({"beam_height": 100, "beam_height_at_kernel": 103}) == 100


# ----------------------------------------------------------------- through the endpoint


async def test_the_account_endpoint_carries_it_on_both_lists(client, user, mock_db):
    from pgasme.db import db

    await db().deposits.insert_one(
        dep(_id="d-h", account_id=user["account_id"], claim_kernel=CLAIM_KERNEL, claim_height=4_030_012)
    )
    await db().deposits.insert_one(dep(_id="d-none", account_id=user["account_id"]))
    await db().payout_requests.insert_one(
        req(_id="r-h", account_id=user["account_id"], created_at=2.0, beam_height_at_kernel=4_031_777)
    )
    await db().payout_requests.insert_one(req(_id="r-none", account_id=user["account_id"], created_at=1.0))

    body = (await client.get("/v1/account", headers=user["headers"])).json()
    heights = {d["_id"]: d["beam_height"] for d in body["deposits"]}
    assert heights == {"d-h": 4_030_012, "d-none": None}
    heights = {r["_id"]: r["beam_height"] for r in body["requests"]}
    assert heights == {"r-h": 4_031_777, "r-none": None}
