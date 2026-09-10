"""What the operator panel SHOWS — 2026-09-10 (T38). Every route, on fixtures.

The panel is read-only by construction, so the properties worth pinning are about faithfulness
rather than effect:

  every route answers the shape the page renders, and none of them writes anything
  a row written before the 2026-09-10 rename renders under the names of record, through the
    ONE normaliser `/v1/account` uses — the stored row is never rewritten (the old spellings
    are composed from `config.LEGACY_*`, never typed: this tree publishes only neutral names)
  a number that cannot be READ is rendered as an error, never as a zero (the treasury table)
  a legacy row carrying NaN degrades one field and never the response (it 500'd a whole account
    once, which hid the very order the user needed)
  an ObjectId `_id` — which `entries` and `events` carry — survives as text instead of a 500
  the raw drawer is an ALLOW-LIST: `siwe_nonces` is not readable through it, ever
  paging is a window plus the total it is a window of
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from pgasme import beam, beampay, ledger
from pgasme.config import LEGACY_MODE, LEGACY_STATUS_FIELD, settings
from pgasme.routers import admin

KEY = "admin-routes-key-" + "0123456789abcdef" * 2
H = {"X-Admin-Key": KEY}
NOW = 1789038000.0
ACCT = "acct-1"
ADDR = "0x1111111111111111111111111111111111111111"
TREASURY = "779746a5" + "00" * 28 + "e6"
MP = "mp-address-1"


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    admin.reset_failures()
    monkeypatch.setattr(settings, "admin_key", KEY)
    return KEY


async def seed(d: Any) -> None:
    """One of everything, including the two shapes that used to break a page: a pre-rename
    deposit row (the router's own mode value and status field) and a payout order carrying NaN."""
    await d.deposits.insert_many(
        [
            {
                "_id": f"dep{i}",
                "account_id": ACCT,
                "address": ADDR,
                "asset": "ETH",
                # the pre-rename spellings, deliberately: rows are never rewritten. Composed
                # from config, never typed — the router's own name is not in this tree.
                "mode": LEGACY_MODE,
                LEGACY_STATUS_FIELD: "Fulfilled",
                "status": "credited" if i else "submitted",
                "treasury": "claimed" if i else None,
                "src": {"chain_id": 42161 if i else 1, "token": "0xusdc", "amount": "10000000"},
                "src_tx_hash": f"0xaaa{i}",
                "quote_id": "q1",
                "value_groth": 1_000_000 + i,
                "eth": {"tx": f"0xbbb{i}", "log_index": 0, "msg_id": 137 + i},
                "pubkey": "02" + "ab" * 32,
                "created_at": NOW - 100 + i,
                "updated_at": NOW,
                "credited_at": NOW,
                "claim_txid": "beam-claim-1" if i else None,
                "shield_txids": ["shield-1"] if i else [],
                "shield_plan": [1_000_000] if i else [],
            }
            for i in range(3)
        ]
    )
    await d.payout_requests.insert_many(
        [
            {
                "_id": "req1",
                "account_id": ACCT,
                "asset": "ETH",
                "status": "scheduled",
                "W": "0xdest",
                "amount_groth": 500_000,
                "fee_groth": 10_000,
                "bridge_fee_groth": 14_733,
                "created_at": NOW - 30,
                "release_at": NOW,
                "hold_reason": "the float is short",
                "hold_at": NOW,
                "holds": 3,
                # the row that used to 500 an entire account page
                "deliver_at": float("nan"),
            },
            {
                "_id": "req2",
                "account_id": ACCT,
                "asset": "ETH",
                "status": "bridging",
                "W": "0xdest",
                "amount_groth": 100_000,
                "created_at": NOW - 20,
                "status_at": NOW,
                "beam_txid": "beam-send-1",
            },
        ]
    )
    await d.accounts.insert_one(
        {"_id": ACCT, "created_at": NOW - 1000, "last_login_at": NOW, "last_chain_id": 1}
    )
    await d.destinations.insert_one(
        {"account_id": ACCT, "address": ADDR, "kind": "connected", "created_at": NOW}
    )
    await ledger.credit(ACCT, "ETH", 1_000_000, "dep1", "lock msg 138")
    await d.events.insert_one(
        {
            "kind": "deposit_credited",
            "text": "credited",
            "at": NOW,
            "notified": True,
            "notified_at": NOW,
            "deposit_id": "dep1",
        }
    )
    await d.beampay_events.insert_one(
        {
            "_id": "deposit_confirmed:tx1",
            "event": "deposit_confirmed",
            "txId": "tx1",
            "payload": {"amount": "1000000"},
            "received_at": NOW,
        }
    )
    await d.unattributed_locks.insert_one(
        {
            "_id": "0xlock:0",
            "asset": "ETH",
            "reason": "no quote matched this lock",
            "status": "open",
            "at": NOW,
            "amount": 1_000_000,
        }
    )
    await d.scanner_state.insert_one({"_id": "0xpipe", "last_block": 123, "at": NOW, "asset": "ETH"})
    await d.leases.insert_one({"_id": "payout_processor", "owner": "42:abc", "at": NOW})
    await d.stats.insert_one({"_id": "pool", "height": 4_030_000, "at": NOW})
    await d.quotes.insert_one({"_id": "q1", "asset": "ETH", "at": NOW})
    await d.mp_addresses.insert_one({"_id": MP, "address": MP, "created_at": NOW})


@pytest.fixture
async def seeded(mock_db: Any) -> Any:
    d = mock_db["pgasme_test"]
    await seed(d)
    return d


class StubBeamPay:
    """Only what the panel asks BeamPay for. Deliberately NOT the full fake from the payout
    suite: this file must keep working while that one is being edited, and a panel that reads
    four routes should be tested against four routes."""

    def __init__(self) -> None:
        self.available = {(TREASURY, 0): 900_000_000, (TREASURY, 36): 2_000_000, (MP, 36): 652_864}
        self.locked = {(TREASURY, 36): 1_000}
        self.contract_txs: dict[str, dict[str, Any]] = {
            "beam-send-1": {"booked": True, "status": 3, "fee": 121_000}
        }
        self.raise_on: set[str] = set()

    async def available_groth(self, address: str, asset_id: int) -> int:
        if "available" in self.raise_on:
            raise beampay.BeamPayError("GET /balances: connection refused")
        return int(self.available.get((address, int(asset_id)), 0))

    async def locked_groth(self, address: str, asset_id: int) -> int:
        return int(self.locked.get((address, int(asset_id)), 0))

    async def wallet_status(self) -> dict[str, Any]:
        if "wallet_status" in self.raise_on:
            raise beampay.BeamPayError("GET /wallet_status: connection refused")
        return {
            "current_height": 4_030_100,
            "is_in_sync": True,
            "totals": [
                {
                    "asset_id": 36,
                    "available": 2_000_000,
                    "available_regular": 2_000_000,
                    "available_mp": 0,
                    "maturing_mp": 652_864,
                }
            ],
        }

    async def transactions(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        # the fee-budget read behind the coin count; unreadable here on purpose, because
        # `wallet_spendable` documents that it falls back to the floor rather than failing
        raise beampay.BeamPayError("GET /transactions: not stubbed")

    async def contract_tx(self, txid: str) -> dict[str, Any] | None:
        if "contract_tx" in self.raise_on:
            raise beampay.BeamPayError(f"contract_tx({txid[:12]}…): HTTP 500")
        return self.contract_txs.get(txid)


class StubWallet:
    """The one wallet-api READ this project makes: `get_utxo` (it moves nothing)."""

    async def utxos(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [
            {"amount": 2_000_000, "asset_id": 36, "type": "norm", "status": 1},
            {"amount": 900_000_000, "asset_id": 0, "type": "norm", "status": 1},
            {"amount": 652_864, "asset_id": 36, "type": "shld", "status": 3},
        ]


@pytest.fixture
def beam_side(monkeypatch: pytest.MonkeyPatch) -> StubBeamPay:
    """A BeamPay and a wallet the panel can read, put back afterwards — both are per-PROCESS
    singletons, and isolation kept per file is isolation the next file does not inherit."""
    bp = StubBeamPay()
    beampay.set_beampay(bp)  # type: ignore[arg-type]
    beam.set_wallet(StubWallet())  # type: ignore[arg-type]
    monkeypatch.setattr(settings, "beam_treasury_address", TREASURY)
    monkeypatch.setattr(settings, "beam_mp_address", MP)
    yield bp
    beampay.set_beampay(None)
    beam.set_wallet(None)


# ───────────────────────────────────────────────────────────────────────────────────── overview


async def test_overview_reports_the_posture_the_flags_and_the_counts(client, seeded, monkeypatch):
    body = (await client.get("/admin/overview", headers=H)).json()
    assert body["env"] == "test" and body["version"]
    # ONE implementation of the posture: the same body /v1/health serves
    assert body["health"] == (await client.get("/v1/health")).json()
    assert body["flags"]["ingress_armed"] is False
    assert body["flags"]["telegram_live"] is False  # the boolean only — never the token
    assert set(body["flags"]["ingress_assets"]) == {"ETH", "DAI", "WBTC"}
    assert body["counts"]["deposits"] == {"credited": 2, "submitted": 1}
    assert body["counts"]["deposits_treasury"] == {"claimed": 2}
    assert body["counts"]["payouts"] == {"scheduled": 1, "bridging": 1}
    assert body["counts"]["unattributed_locks"] == {"open": 1}
    assert body["counts"]["accounts"] == 1 and body["counts"]["beampay_events"] == 1
    assert body["counts"]["held"] == {"payouts": 1, "deposits": 0}
    # `claimed` is treasury work the processor still owns (it is waiting to be shielded), so
    # both credited deposits are in flight on the Beam side — TREASURY_ACTIVE, not "finished"
    assert body["counts"]["in_flight"] == {"payouts": 2, "treasury": 2}


async def test_overview_names_where_every_worker_number_came_from(client, seeded):
    """No worker writes a heartbeat and this panel does not create one (it would be a WRITE).
    Each entry is the newest row that loop is the only writer of, and it SAYS which."""
    passes = (await client.get("/admin/overview", headers=H)).json()["workers"]["last_pass"]
    assert set(passes) == {"deposit_watcher", "payout_processor", "stats_refresher", "monitor"}
    assert all(p["source"] for p in passes.values())
    assert passes["deposit_watcher"]["at"] == NOW
    assert passes["deposit_watcher"]["detail"]["last_block"] == 123
    assert passes["payout_processor"]["detail"]["owner"] == "42:abc"
    assert passes["stats_refresher"]["detail"]["height"] == 4_030_000
    assert passes["monitor"]["detail"]["kind"] == "deposit_credited"


async def test_overview_shows_the_kill_switch_by_the_file_it_actually_reads(
    client, seeded, tmp_path, monkeypatch
):
    stop = tmp_path / "pgasme.stop"
    monkeypatch.setattr(settings, "stop_file", str(stop))
    body = (await client.get("/admin/overview", headers=H)).json()
    assert body["kill_switch"] == {"file": str(stop), "engaged": False}
    stop.write_text("")
    body = (await client.get("/admin/overview", headers=H)).json()
    assert body["kill_switch"]["engaged"] is True


async def test_the_watchdog_line_is_masked_and_a_missing_log_says_so(
    client, seeded, tmp_path, monkeypatch
):
    """Logs on this box are secrets-adjacent — that is the incident that made the mask exist —
    and "the API cannot open it" is a real answer, not silence."""
    body = (await client.get("/admin/overview", headers=H)).json()
    assert body["watchdog"]["line"] is None and "FileNotFoundError" in body["watchdog"]["why"]

    log = tmp_path / "watch.log"
    log.write_text(
        "2026-09-10 11:00:00 ok\n"
        "2026-09-10 11:02:00 telegram https://api.telegram.org/bot123:SECRETTOKEN_x/sendMessage\n"
    )
    monkeypatch.setattr(admin, "WATCHDOG_LOG", str(log))
    body = (await client.get("/admin/overview", headers=H)).json()
    assert body["watchdog"]["why"] is None
    assert "bot<REDACTED>" in body["watchdog"]["line"]
    assert "SECRETTOKEN" not in body["watchdog"]["line"]


# ───────────────────────────────────────────────────────────────────────────────────── deposits


async def test_deposits_render_pre_rename_rows_under_the_names_of_record(client, seeded):
    body = (await client.get("/admin/deposits", headers=H)).json()
    assert body["total"] == 3 and len(body["rows"]) == 3 and body["has_more"] is False
    row = body["rows"][0]
    assert row["_id"] == "dep2"  # newest first
    assert row["mode"] == "xchain" and row["route_status"] == "Fulfilled"
    assert LEGACY_STATUS_FIELD not in row  # the row is not rewritten; the wire never carries it
    # the panel gets back the two fields the public view drops
    assert row["account_id"] == ACCT and row["pubkey"].startswith("02ab")
    # identifiers, not URLs: the web owns one implementation of "which explorer is this chain's"
    assert row["tx"]["src"] == {"chain_id": 42161, "hash": "0xaaa2"}
    assert row["tx"]["eth"] == {"chain_id": 1, "hash": "0xbbb2"}
    assert row["tx"]["beam"] == {"claim_txid": "beam-claim-1", "shield_txids": ["shield-1"]}
    assert body["statuses"][0] == "submitted"


async def test_deposit_filters(client, seeded):
    async def rows(**params: Any) -> list[dict[str, Any]]:
        r = await client.get("/admin/deposits", headers=H, params=params)
        assert r.status_code == 200, r.text
        return r.json()["rows"]

    assert [r["_id"] for r in await rows(status="submitted")] == ["dep0"]
    assert [r["_id"] for r in await rows(treasury="claimed")] == ["dep2", "dep1"]
    assert len(await rows(account=ACCT)) == 3
    assert len(await rows(account=ADDR)) == 3  # either spelling the operator happens to hold
    assert len(await rows(account="nobody")) == 0
    assert [r["_id"] for r in await rows(chain=1)] == ["dep0"]
    assert [r["_id"] for r in await rows(since=NOW - 99)] == ["dep2", "dep1"]


async def test_deposits_page(client, seeded):
    body = (await client.get("/admin/deposits", headers=H, params={"limit": 2})).json()
    assert [r["_id"] for r in body["rows"]] == ["dep2", "dep1"]
    assert (body["total"], body["limit"], body["offset"], body["has_more"]) == (3, 2, 0, True)
    body = (
        await client.get("/admin/deposits", headers=H, params={"limit": 2, "offset": 2})
    ).json()
    assert [r["_id"] for r in body["rows"]] == ["dep0"] and body["has_more"] is False
    # the window is bounded — an operator's typo must not ask Mongo for everything
    assert (await client.get("/admin/deposits", headers=H, params={"limit": 10_000})).status_code == 422
    assert (await client.get("/admin/deposits", headers=H, params={"offset": -1})).status_code == 422


async def test_one_deposit_carries_its_events_entries_and_quote(client, seeded):
    body = (await client.get("/admin/deposits/dep1", headers=H)).json()
    assert body["deposit"]["_id"] == "dep1"
    assert body["raw"][LEGACY_STATUS_FIELD] == "Fulfilled"  # the RAW row, verbatim
    assert [e["kind"] for e in body["events"]] == ["deposit_credited"]
    # the ledger entry is found by `ref`, which for a deposit IS its id
    assert [e["kind"] for e in body["entries"]] == ["credit"]
    # …and it carries an ObjectId `_id`, which would 500 the response unrendered
    assert isinstance(body["entries"][0]["_id"], str) and len(body["entries"][0]["_id"]) == 24
    assert body["quote"]["_id"] == "q1"
    assert (await client.get("/admin/deposits/nope", headers=H)).status_code == 404


# ────────────────────────────────────────────────────────────────────────────────────── payouts


async def test_payouts_surface_the_hold_and_survive_a_nan(client, seeded):
    body = (await client.get("/admin/payouts", headers=H)).json()
    assert body["total"] == 2
    by = {r["_id"]: r for r in body["rows"]}
    # T52 — `reason` is the OPERATOR half (`hold_detail` when the row has one, the row's own
    # `hold_reason` otherwise) and the sentence the CUSTOMER is being told travels beside it, so
    # this console can answer them without going and reading their page.
    assert by["req1"]["hold"] == {
        "reason": "the float is short",
        "user_reason": "the float is short",
        "code": None,
        "at": NOW,
        "count": 3,
        "dark": False,
        "paged_at": None,
        "reminded_at": None,
    }
    assert by["req1"]["deliver_at"] is None  # NaN blanked, one field, never the response
    assert by["req2"]["hold"]["reason"] is None
    assert "held" in body["statuses"] and "scheduled" in body["statuses"]
    assert [r["_id"] for r in (await client.get(
        "/admin/payouts", headers=H, params={"status": "bridging"}
    )).json()["rows"]] == ["req2"]
    assert (await client.get(
        "/admin/payouts", headers=H, params={"asset": "eth"}
    )).json()["total"] == 2


async def test_one_payout_asks_beampay_about_its_contract_tx(client, seeded, beam_side):
    body = (await client.get("/admin/payouts/req2", headers=H)).json()
    assert body["payout"]["status"] == "bridging"
    assert body["beampay_tx"] == {
        "txid": "beam-send-1",
        "tx": {"booked": True, "status": 3, "fee": 121_000},
        "error": None,
    }
    # an order with no Beam transaction yet asks nothing and says so
    assert (await client.get("/admin/payouts/req1", headers=H)).json()["beampay_tx"] == {
        "txid": None,
        "tx": None,
        "error": None,
    }
    assert (await client.get("/admin/payouts/nope", headers=H)).status_code == 404


async def test_a_beampay_that_will_not_answer_degrades_one_field(client, seeded, beam_side):
    """"We could not look" is not "there is nothing" — and it is certainly not a 500 on the page
    the operator opened to find out what happened."""
    beam_side.raise_on.add("contract_tx")
    body = (await client.get("/admin/payouts/req2", headers=H)).json()
    assert body["payout"]["status"] == "bridging"  # the row is still there
    assert body["beampay_tx"]["tx"] is None
    assert "HTTP 500" in body["beampay_tx"]["error"]


# ───────────────────────────────────────────────────────────────── unattributed / accounts


async def test_unattributed_locks_are_listed_as_they_are(client, seeded):
    body = (await client.get("/admin/unattributed", headers=H)).json()
    assert body["total"] == 1 and body["rows"][0]["status"] == "open"
    assert body["rows"][0]["reason"] == "no quote matched this lock"
    assert (await client.get(
        "/admin/unattributed", headers=H, params={"status": "abandoned"}
    )).json()["total"] == 0


async def test_accounts_show_the_address_the_balances_and_the_counts(client, seeded):
    body = (await client.get("/admin/accounts", headers=H)).json()
    assert body["total"] == 1
    row = body["rows"][0]
    assert row["account_id"] == ACCT
    # the address is NOT on the account row (the id is a one-way keccak) — it comes from the
    # `connected` destination sign-in writes
    assert row["address"] == ADDR
    # the SAME reader /v1/account answers the user with, so the two can never disagree
    assert row["balances"] == await ledger.balances(ACCT)
    assert row["balances"]["ETH"]["available"] == 1_000_000
    assert (row["deposits"], row["payouts"], row["destinations"]) == (3, 2, 1)
    assert row["last_login_at"] == NOW
    # either spelling finds it, and neither finds a stranger
    assert (await client.get("/admin/accounts", headers=H, params={"account": ADDR})).json()["total"] == 1
    assert (await client.get("/admin/accounts", headers=H, params={"account": ACCT})).json()["total"] == 1
    assert (await client.get("/admin/accounts", headers=H, params={"account": "0xdead"})).json()["total"] == 0


# ───────────────────────────────────────────────────────────────────────────────────── treasury


async def test_treasury_keeps_the_ledger_and_the_wallet_apart(client, seeded, beam_side):
    """THREE TABLES THAT SAY DIFFERENT THINGS. On 2026-09-10 the registry summed 2,652,864
    groth of bETH and the wallet could spend none of it — a panel that showed one number would
    have shown the wrong one."""
    body = (await client.get("/admin/treasury", headers=H)).json()
    assert body["addresses"]["treasury"] == TREASURY
    assert body["addresses"]["mp_registry"] == [MP] and body["addresses"]["mp_registry_size"] == 1
    assert body["beam_fees"] == {"value": 900_000_000, "error": None}
    eth = next(r for r in body["ledger"] if r["asset"] == "ETH")
    assert eth["treasury"]["value"] == 2_000_000 and eth["locked"]["value"] == 1_000
    assert eth["float"]["value"] == 652_864  # summed over the registry, not one address
    spend = next(r for r in body["wallet"] if r["asset"] == "ETH")["spendable"]["value"]
    assert spend["regular"] == 2_000_000 and spend["shielded"] == 0
    assert spend["maturing_mp"] == 652_864  # ← the locked chunk the ledger table calls float
    assert spend["coins_regular"] == 1 and spend["coins_shielded"] == 0
    assert body["wallet_status"]["value"]["is_in_sync"] is True


async def test_treasury_states_what_it_could_not_read_and_never_prints_a_zero(
    client, seeded, beam_side
):
    beam_side.raise_on.add("available")
    beam_side.raise_on.add("wallet_status")
    body = (await client.get("/admin/treasury", headers=H)).json()
    assert body["beam_fees"] == {"value": None, "error": "BeamPayError: GET /balances: connection refused"}
    eth = next(r for r in body["ledger"] if r["asset"] == "ETH")
    assert eth["treasury"]["value"] is None and "connection refused" in eth["treasury"]["error"]
    assert eth["locked"]["value"] == 1_000  # one unreadable cell, not a blank table
    spend = next(r for r in body["wallet"] if r["asset"] == "ETH")["spendable"]
    assert spend["value"] is None and "connection refused" in spend["error"]


async def test_treasury_on_an_unconfigured_box_refuses_rather_than_reporting_zero(client, seeded):
    """No treasury address means there is no address to read a balance OF, and 0 is not an
    answer — the same refusal `beampay.treasury_address()` raises."""
    body = (await client.get("/admin/treasury", headers=H)).json()
    assert "PGAS_BEAM_TREASURY_ADDRESS is not configured" in body["treasury_error"]
    assert body["addresses"]["treasury"] is None
    assert body["beam_fees"]["value"] is None
    assert all(r["treasury"]["value"] is None for r in body["ledger"])


async def test_treasury_lists_the_held_rows_the_intents_and_the_shield(client, seeded, beam_side):
    body = (await client.get("/admin/treasury", headers=H)).json()
    assert [r["_id"] for r in body["held"]["payouts"]] == ["req1"]
    assert body["held"]["deposits"] == []
    # an intent is a row that recorded what it was ABOUT to do and has no settled txid yet —
    # the rows a restart reconciles from the chain and must never re-sign
    assert [r["_id"] for r in body["intents"]["payouts"]] == ["req2"]
    assert body["intents"]["treasury"] == []
    shield = {r["deposit_id"]: r for r in body["shield"]}
    assert shield["dep1"]["treasury"] == "claimed"
    assert shield["dep1"]["plan"] == [1_000_000] and shield["dep1"]["sent"] == 1
    assert body["float_policy"]["keep_groth"] == settings.shield_keep_groth
    eth = next(r for r in body["float_policy"]["per_asset"] if r["asset"] == "ETH")
    assert eth["scheduled_liability"]["value"] == 500_000 + 14_733
    assert eth["keeps_groth"] >= eth["with_buffer_groth"]


async def test_the_treasury_route_never_touches_the_payout_passs_caches(
    client, seeded, beam_side
):
    """⛔ M3. The route used to call `payouts.mp_registry()`, `float_groth()` and
    `wallet_spendable()` — every one of which CACHES ITS ANSWER IN `payouts._PASS`, the payout
    processor's per-pass memory, in this same process. The panel and the processor run in one
    event loop (one `--workers 1` unit, the loop started by `workers.start()`), so an operator
    refreshing this tab mid-pass could plant the numbers the next gate of that pass would read,
    and a value the route wrote is a value the pass never measured.

    The route now reads through helpers in this router that consult no cache and write none. The
    proof is a deep copy either side of the request: not "it looks the same", the whole
    structure, including the nested dicts the caches live in."""
    from copy import deepcopy

    from pgasme import payouts

    before = deepcopy(payouts._PASS)
    r = await client.get("/admin/treasury", headers=H)
    assert r.status_code == 200, r.text
    assert deepcopy(payouts._PASS) == before


async def test_the_treasury_route_neither_reads_nor_overwrites_a_pass_in_flight(
    client, seeded, beam_side
):
    """The same fact from the other side: a pass that is ALREADY holding cached answers must
    find them untouched afterwards, and the panel must not answer FROM them — a stale registry
    a pass cached ten minutes ago is not what the operator opened this page to see."""
    from copy import deepcopy

    from pgasme import payouts

    payouts._PASS["mp_registry"] = ["an-address-only-the-pass-knows"]
    payouts._PASS["float"][36] = 999_999_999
    payouts._PASS["wallet"][36] = {"regular": 1, "shielded": 2}
    payouts._PASS["coins"] = {36: {"regular": 7, "shielded": 7}}
    mid_pass = deepcopy(payouts._PASS)

    body = (await client.get("/admin/treasury", headers=H)).json()

    assert deepcopy(payouts._PASS) == mid_pass  # nothing of the pass's was disturbed
    # …and nothing of the pass's was believed, either
    assert body["addresses"]["mp_registry"] == [MP]
    eth = next(r for r in body["ledger"] if r["asset"] == "ETH")
    assert eth["float"]["value"] == 652_864
    spend = next(r for r in body["wallet"] if r["asset"] == "ETH")["spendable"]["value"]
    assert spend["regular"] == 2_000_000 and spend["coins_regular"] == 1


async def test_the_panels_treasury_readers_agree_with_the_money_paths(seeded, beam_side):
    """⛔ TWO IMPLEMENTATIONS OF ONE FACT WILL DISAGREE (law 9), so this test is the thing that
    holds them together. The panel reads without the pass's cache; `payouts` reads with it. They
    must answer the same numbers on the same wallet, or the operator is being shown a float the
    gate is not using — which is the only way this split can hurt.

    `payouts` is asked SECOND and its per-pass state is cleared afterwards: this test must not
    leave a cache behind for the next one (isolation kept per file is isolation a new file does
    not inherit)."""
    from pgasme import payouts
    from pgasme.assets import ASSETS

    bp = beam_side
    eth = ASSETS["ETH"]
    try:
        assert await admin.mp_registry_now() == await payouts.mp_registry()
        assert await admin.float_now(bp, eth) == await payouts.float_groth(bp, eth)
        mine = await admin.wallet_buckets(bp, eth)
        theirs = await payouts.wallet_spendable(bp, eth)
        for k in ("regular", "shielded", "maturing_regular", "maturing_mp", "maturing", "locked",
                  "coins_regular", "coins_shielded", "coins_error"):
            assert mine[k] == theirs[k], k
    finally:
        payouts.reset_process_state()


async def test_a_panel_read_of_the_float_is_the_sum_over_the_registry(seeded, beam_side):
    """Not one address's balance: the float is spread over one max-privacy address per shield
    chunk, and a reader that named only the primary would understate it. Pinned here because
    the panel's copy of that loop is the one this file owns."""
    from pgasme.assets import ASSETS

    beam_side.available[("mp-address-2", 36)] = 1_000
    d = seeded
    await d.mp_addresses.insert_one({"_id": "mp-address-2", "address": "mp-address-2", "created_at": NOW})
    assert await admin.mp_registry_now() == [MP, "mp-address-2"]
    assert await admin.float_now(beam_side, ASSETS["ETH"]) == 652_864 + 1_000


# ────────────────────────────────────────────────────────────────────── events / raw drawer


async def test_events_and_beampay_events(client, seeded):
    body = (await client.get("/admin/events", headers=H)).json()
    assert body["total"] == 1 and body["kinds"] == ["deposit_credited"]
    assert (await client.get(
        "/admin/events", headers=H, params={"notified": "false"}
    )).json()["total"] == 0
    body = (await client.get("/admin/beampay-events", headers=H)).json()
    assert body["rows"][0]["event"] == "deposit_confirmed"
    assert body["rows"][0]["payload"] == {"amount": "1000000"}
    assert (await client.get(
        "/admin/beampay-events", headers=H, params={"event": "failed"}
    )).json()["total"] == 0


async def test_the_raw_drawer_is_an_allow_list(client, seeded):
    r = await client.get("/admin/raw/deposits/dep1", headers=H)
    assert r.status_code == 200 and r.json()["doc"]["_id"] == "dep1"
    assert r.json()["collection"] == "deposits"
    # ⛔ never, whoever asks: these are live sign-in credentials
    for banned in ("siwe_nonces", "dest_nonces", "rate_limits"):
        bad = await client.get(f"/admin/raw/{banned}/x", headers=H)
        assert bad.status_code == 400, banned
        assert banned not in admin.RAW_COLLECTIONS
        assert "not one of the collections" in bad.json()["detail"]
    assert (await client.get("/admin/raw/deposits/nope", headers=H)).status_code == 404
    # every collection a list route renders is openable
    for name in ("payout_requests", "events", "beampay_events", "unattributed_locks", "accounts"):
        assert name in admin.RAW_COLLECTIONS


async def test_nothing_the_panel_does_writes_anything(client, seeded):
    """The whole point of rule 1, asserted rather than asserted-in-a-docstring: every count in
    every collection is the same after a full sweep of the panel as before it."""
    d = seeded
    names = (
        "deposits", "payout_requests", "entries", "events", "beampay_events", "accounts",
        "destinations", "unattributed_locks", "quotes", "leases", "stats", "scanner_state",
        "mp_addresses", "rate_limits",
    )
    before = {n: await d[n].count_documents({}) for n in names}
    for path in (
        "/admin/overview", "/admin/deposits", "/admin/deposits/dep1", "/admin/payouts",
        "/admin/payouts/req1", "/admin/unattributed", "/admin/accounts", "/admin/treasury",
        "/admin/events", "/admin/beampay-events", "/admin/raw/deposits/dep1",
    ):
        assert (await client.get(path, headers=H)).status_code == 200, path
    assert {n: await d[n].count_documents({}) for n in names} == before
    # …including the per-IP counter, which is why it is in memory and not in `rate_limits`
    assert before["rate_limits"] == 0


async def test_the_panel_has_no_way_in_other_than_GET(client, seeded):
    """No writes in v1 means no button that moves money — and no verb that could grow one.

    404 and NOT 405, even with the key: the catch-all at the end of the router is what makes a
    wrong verb indistinguishable from an unrouted path, and a v2 that wants a POST has to add
    it deliberately."""
    for method in ("post", "put", "patch", "delete"):
        r = await getattr(client, method)("/admin/overview", headers=H)
        assert r.status_code == 404, (method, r.status_code)
    assert (await client.get("/admin/nothing-here", headers=H)).status_code == 404


def test_the_admin_routes_are_not_in_the_public_schema():
    """`/v1/docs` is mounted outside prod. The operator panel is not part of the API's public
    surface and must not be advertised on a box where the docs are on.

    BOTH routers: `bare` carries the one route that cannot live on a prefixed one (`/admin`
    itself), and a second router is a second place to forget this."""
    for r in (admin.router, admin.bare):
        assert r.include_in_schema is False
        assert all(x.include_in_schema is False for x in r.routes)  # type: ignore[attr-defined]


def test_every_panel_read_takes_get_head_and_options_and_nothing_else():
    """The route table itself, rather than a request: no read may grow a writing verb by
    accident, and every one of them must answer HEAD and OPTIONS (L2). The catch-alls are the
    two deliberate exceptions and are named here so adding a third is a decision."""
    catch_all = {"/admin/{rest:path}", "/admin"}
    reads: dict[str, set[str]] = {}
    for route in admin.router.routes + admin.bare.routes:
        path = route.path  # type: ignore[attr-defined]
        if path in catch_all:
            assert set(route.methods) == set(admin.ANY_METHOD), path  # type: ignore[attr-defined]
            continue
        reads.setdefault(path, set()).update(route.methods)  # type: ignore[attr-defined]
    assert reads, "the panel serves nothing at all"
    for path, methods in reads.items():
        assert methods == set(admin.READ_METHODS), (path, methods)


def test_the_time_this_file_pins_is_not_the_time_it_runs():
    """A fixture clock, so a slow box cannot make `since=` flaky."""
    assert NOW < time.time()
