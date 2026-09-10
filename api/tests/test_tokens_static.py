"""The token lists we host ourselves (T31 C1): pgasme/tokens.py, /v1/health.tokens and the
Cache-Control on the /v1/dex/tokens fallback.

Offline by construction: the router's client is a scripted stub, the directory is a tmp_path,
and nothing here reaches the network or /opt.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from pgasme import tg, tokens, xchain
from pgasme.config import settings

# What the router answers for a chain, in its own shape (address-keyed, native marked).
ROUTER_ROWS: dict[int, list[dict[str, Any]]] = {
    1: [
        {
            "address": "0x0000000000000000000000000000000000000000",
            "symbol": "ETH",
            "name": "Ethereum",
            "decimals": 18,
            "logoURI": "https://example.invalid/eth.png",
            "isNative": True,
        },
        {
            "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "symbol": "USDC",
            "name": "USD Coin",
            "decimals": 6,
            "logoURI": "",
        },
    ],
    42161: [
        {
            "address": "0x0000000000000000000000000000000000000000",
            "symbol": "ETH",
            "name": "Ethereum",
            "decimals": 18,
            "isNative": True,
        }
    ],
}
DEFAULT_ROWS = [
    {"address": "0x1111111111111111111111111111111111111111", "symbol": "X", "decimals": 18}
]


@pytest.fixture(autouse=True)
def tokens_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PGAS_TOKENS_DIR under the test's own tmp_path — no test may write to /opt."""
    d = tmp_path / "tokens"
    monkeypatch.setattr(settings, "tokens_dir", str(d))
    tokens.clear_cache()
    yield d
    tokens.clear_cache()


class Router:
    """A scripted `xchain.token_list`: what each ROUTE chain id answers, and which ones fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []
        self.fail: dict[int, Exception] = {}
        self.rows: dict[int, list[dict[str, Any]]] = dict(ROUTER_ROWS)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> Router:
    r = Router()

    async def token_list(route_chain: int, force: bool = False) -> list[dict[str, Any]]:
        r.calls.append((int(route_chain), force))
        if int(route_chain) in r.fail:
            raise r.fail[int(route_chain)]
        return [dict(t) for t in r.rows.get(int(route_chain), DEFAULT_ROWS)]

    monkeypatch.setattr(xchain, "token_list", token_list)
    return r


def manifest_of(d: Path) -> dict[str, Any]:
    return json.loads((d / "manifest.json").read_bytes())


def row_for(d: Path, chain_id: int) -> dict[str, Any]:
    return next(c for c in manifest_of(d)["chains"] if c["chain_id"] == chain_id)


# ----------------------------------------------------------------- what a refresh writes


async def test_a_refresh_writes_one_file_per_chain_native_first_with_a_matching_gzip_twin(
    tokens_dir, router
):
    summary = await tokens.refresh_all()

    assert summary["ok"] and summary["failed"] == []
    # conftest's chain table: 1, 42161, 8453, 1514, 7565164 — every one of them gets a file
    assert summary["chains"] == 5
    body = json.loads((tokens_dir / "1.json").read_bytes())
    assert body["chain_id"] == 1 and body["updated_at"] > 0
    assert [t["symbol"] for t in body["tokens"]] == ["ETH", "USDC"]  # native first, order kept
    assert body["tokens"][0] == {
        "address": "0x0000000000000000000000000000000000000000",
        "symbol": "ETH",
        "name": "Ethereum",
        "decimals": 18,
        "logo": "https://example.invalid/eth.png",
    }
    # the pre-compressed twin nginx serves is the SAME bytes, not another rendering of them
    assert gzip.decompress((tokens_dir / "1.json.gz").read_bytes()) == (
        tokens_dir / "1.json"
    ).read_bytes()
    # compact: no spaces after the separators, or the 1.3 MB list ships megabytes of them
    assert b'", "' not in (tokens_dir / "1.json").read_bytes()


async def test_the_manifest_states_the_sha256_of_the_file_that_is_actually_on_disk(
    tokens_dir, router
):
    await tokens.refresh_all()
    m = manifest_of(tokens_dir)
    assert m["updated_at"] > 0 and m["failed"] == []
    assert [c["chain_id"] for c in m["chains"]] == sorted(c["chain_id"] for c in m["chains"])
    for c in m["chains"]:
        data = (tokens_dir / f"{c['chain_id']}.json").read_bytes()
        assert c["sha256"] == hashlib.sha256(data).hexdigest()
        assert c["count"] == len(json.loads(data)["tokens"]) > 0


async def test_every_file_is_world_readable_because_nginx_is_not_root(tokens_dir, router):
    """mkstemp makes 0600 files: unchmodded, the edge would answer 403 on every list."""
    await tokens.refresh_all()
    for name in ("1.json", "1.json.gz", "manifest.json"):
        mode = stat.S_IMODE(os.stat(tokens_dir / name).st_mode)
        assert mode == 0o644, f"{name} is {oct(mode)}"


async def test_the_refresher_never_serves_its_own_cache_back_to_itself(tokens_dir, router):
    await tokens.refresh_all()
    assert router.calls and all(force is True for _, force in router.calls)


async def test_a_refresh_leaves_no_temporary_files_behind(tokens_dir, router):
    await tokens.refresh_all()
    assert [p.name for p in tokens_dir.iterdir() if p.name.startswith(".")] == []


# ----------------------------------------------------------------- when something goes wrong


async def test_one_chain_that_cannot_be_fetched_keeps_the_file_and_the_timestamp_it_had(
    tokens_dir, router
):
    await tokens.refresh_all()
    before = (tokens_dir / "1.json").read_bytes()
    before_row = row_for(tokens_dir, 1)

    router.fail[1] = xchain.XchainError("token-list 1: HTTP 502")
    summary = await tokens.refresh_all()

    assert summary["ok"] and summary["failed"] == [1] and summary["chains"] == 5
    assert (tokens_dir / "1.json").read_bytes() == before
    # `updated_at` on a row is when that FILE was written, never when a run touched it
    assert row_for(tokens_dir, 1) == before_row
    assert row_for(tokens_dir, 8453)["updated_at"] >= before_row["updated_at"]


async def test_an_empty_list_is_a_failure_and_never_overwrites_a_good_one(tokens_dir, router):
    await tokens.refresh_all()
    before = (tokens_dir / "1.json").read_bytes()

    router.rows[1] = []
    summary = await tokens.refresh_all()

    assert summary["failed"] == [1]
    assert (tokens_dir / "1.json").read_bytes() == before


async def test_a_write_that_fails_half_way_leaves_the_previous_file_whole(
    tokens_dir, router, monkeypatch
):
    await tokens.refresh_all()
    before = (tokens_dir / "1.json").read_bytes()
    real_replace = os.replace

    def replace(src, dst, *a, **k):
        if str(dst).endswith("/1.json"):  # the plain file, after its gz twin is already in place
            raise OSError("disk full")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(tokens.os, "replace", replace)
    summary = await tokens.refresh_all()

    assert summary["failed"] == [1]
    assert (tokens_dir / "1.json").read_bytes() == before
    assert [p.name for p in tokens_dir.iterdir() if p.name.startswith(".")] == []


async def test_the_manifest_never_carries_the_upstream_message(tokens_dir, router):
    """These files are public. An error string would publish the upstream's own host name."""
    router.fail[1] = xchain.XchainError("token-list: https://router.example.invalid refused")
    await tokens.refresh_all()
    assert b"router.example.invalid" not in (tokens_dir / "manifest.json").read_bytes()
    assert manifest_of(tokens_dir)["failed"] == [1]


async def test_a_directory_it_cannot_write_refuses_and_never_half_writes(
    tokens_dir, router, monkeypatch, tmp_path
):
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    monkeypatch.setattr(settings, "tokens_dir", str(blocked / "tokens"))
    with pytest.raises(tokens.TokensError):
        await tokens.refresh_all()


async def test_an_unset_directory_is_a_refusal_and_never_the_working_directory(
    tokens_dir, router, monkeypatch
):
    """Path("") is Path(".") — an unset setting would scatter the lists wherever we started."""
    monkeypatch.setattr(settings, "tokens_dir", "  ")
    with pytest.raises(tokens.TokensError, match="empty"):
        await tokens.refresh_all()


async def test_an_unwritable_directory_pages_once_and_never_takes_the_worker_down(
    tokens_dir, router, monkeypatch, tmp_path
):
    blocked = tmp_path / "blocked2"
    blocked.mkdir(mode=0o500)
    monkeypatch.setattr(settings, "tokens_dir", str(blocked / "tokens"))
    sent: list[str] = []

    async def send(text: str, *, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        if key and cooldown_s > 0 and key in {s.split("|")[0] for s in sent}:
            return False
        sent.append(f"{key}|{text}")
        return True

    monkeypatch.setattr(tg, "send", send)

    first = await tokens.refresh_once()
    second = await tokens.refresh_once()

    assert first["ok"] is False and second["ok"] is False  # returns, never raises
    assert len(sent) == 1 and sent[0].startswith(tokens.DIR_ALERT_KEY + "|")


# ----------------------------------------------------------------- what the API says about them


async def test_health_reports_when_the_lists_were_written_and_how_many_chains(
    client, tokens_dir, router
):
    empty = (await client.get("/v1/health")).json()["tokens"]
    assert empty == {"updated_at": None, "chains": 0, "age_s": None, "failed": []}

    await tokens.refresh_all()
    after = (await client.get("/v1/health")).json()["tokens"]
    assert after["chains"] == 5
    assert after["updated_at"] == manifest_of(tokens_dir)["updated_at"]
    assert 0 <= after["age_s"] < 60


async def test_health_survives_a_manifest_that_is_not_json(client, tokens_dir, router):
    await tokens.refresh_all()
    (tokens_dir / "manifest.json").write_bytes(b"{not json")
    tokens.clear_cache()
    assert (await client.get("/v1/health")).json()["tokens"]["updated_at"] is None


async def test_the_fallback_endpoint_answers_the_same_rows_as_the_static_file(
    client, tokens_dir, router
):
    """One implementation of the public row: falling back changes the speed, not the answer."""
    await tokens.refresh_all()
    r = await client.get("/v1/dex/tokens", params={"chain_id": 1})
    assert r.status_code == 200
    assert r.json()["tokens"] == json.loads((tokens_dir / "1.json").read_bytes())["tokens"]


async def test_the_fallback_endpoint_may_be_cached_for_an_hour(client, router):
    r = await client.get("/v1/dex/tokens", params={"chain_id": 1})
    assert r.headers["cache-control"] == "public, max-age=3600"


async def test_a_row_without_an_address_or_with_unreadable_decimals_is_dropped_not_guessed():
    rows = [
        {"address": "0xaa", "symbol": "A", "decimals": 18},
        {"symbol": "NOADDR", "decimals": 18},
        {"address": "0xbb", "symbol": "B", "decimals": "eighteen"},
    ]
    assert [t["symbol"] for t in tokens.normalise(rows)] == ["A"]


# ----------------------------------------------------------------- the CLI the deploy runs


async def test_the_status_command_fails_when_too_few_chains_are_listed(tokens_dir, router, capsys):
    assert tokens.main(["status", "--min-chains", "10"]) == 1  # no manifest at all
    await tokens.refresh_all()
    assert tokens.main(["status", "--min-chains", "5"]) == 0
    assert tokens.main(["status", "--min-chains", "10"]) == 1
    assert tokens.main(["nonsense"]) == 2


# =============================================================== T31b: the skeptic's round
#
# Six defects, each of which let the refresher look healthy while it was not.


# ----------------------------------------------------------------- item 2: the age it reports


async def test_the_manifest_age_is_the_STALEST_chain_not_the_last_run(tokens_dir, router):
    """⛔ A run that refreshed nothing used to stamp `updated_at = now`.

    The top-level timestamp is what `/v1/health` turns into `age_s` and what the watchdog pages
    on. Stamped unconditionally it said "fresh" for as long as the process kept running, however
    many chains were failing — the one signal that says "these lists are stale" could not say it."""
    await tokens.refresh_all()
    old = manifest_of(tokens_dir)["updated_at"]

    # a day passes: age every row on disk, then let every chain but Ethereum fail
    m = json.loads((tokens_dir / "manifest.json").read_bytes())
    for c in m["chains"]:
        c["updated_at"] = old - 86_400
    (tokens_dir / "manifest.json").write_bytes(json.dumps(m).encode())
    tokens.clear_cache()

    router.fail = {c: xchain.XchainError("boom") for c in (42161, 8453, 100000013, 7565164)}
    out = await tokens.refresh_all()
    assert out["failed"] == [1514, 8453, 42161, 7565164]

    m2 = manifest_of(tokens_dir)
    ages = [c["updated_at"] for c in m2["chains"]]
    assert m2["updated_at"] == min(ages), "the manifest must age with its stalest chain"
    assert m2["updated_at"] == old - 86_400
    h = tokens.health()
    assert h["age_s"] is not None and h["age_s"] > 86_000


async def test_health_names_the_chains_that_could_not_be_refreshed(client, tokens_dir, router):
    """A count of chains cannot show a chain that is quietly frozen: the ids can."""
    empty = (await client.get("/v1/health")).json()["tokens"]
    assert empty["failed"] == []

    router.fail = {42161: xchain.XchainError("upstream 502")}
    await tokens.refresh_all()
    after = (await client.get("/v1/health")).json()["tokens"]
    assert after["failed"] == [42161]
    # …and the upstream's own words never reach a public payload
    assert "502" not in json.dumps(after)


async def test_the_deploy_assertion_fails_on_a_failed_chain_and_on_a_days_old_manifest(
    tokens_dir, router, capsys
):
    """`--min-chains N` is the DEPLOY's assertion, not a report: it must fail on every condition
    that means "what nginx is about to serve is not what this box just fetched"."""
    await tokens.refresh_all()
    assert tokens.main(["status", "--min-chains", "5"]) == 0

    # one chain frozen
    router.fail = {42161: xchain.XchainError("nope")}
    await tokens.refresh_all()
    assert tokens.main(["status", "--min-chains", "5"]) == 1
    assert "42161" in capsys.readouterr().err

    # …and a manifest nobody has refreshed for over a day
    router.fail = {}
    await tokens.refresh_all()
    m = json.loads((tokens_dir / "manifest.json").read_bytes())
    m["updated_at"] = m["updated_at"] - (25 * 3600)
    for c in m["chains"]:
        c["updated_at"] = m["updated_at"]
    (tokens_dir / "manifest.json").write_bytes(json.dumps(m).encode())
    tokens.clear_cache()
    assert tokens.main(["status", "--min-chains", "5"]) == 1
    assert "old" in capsys.readouterr().err.lower()

    # a bare `status` is a report and still exits 0 on the same manifest
    assert tokens.main(["status"]) == 0


# ----------------------------------------------------------------- item 3: the worker loop


async def test_the_refresher_is_actually_wired_into_the_workers(monkeypatch):
    """⛔ C1 asked for a loop; nothing ran it. Without this the lists are refreshed exactly once,
    by the deploy, and then never again for as long as the box stays up."""
    from pgasme import workers

    seen: list[tuple[str, float, Any]] = []

    async def fake_forever(name: str, interval_s: float, fn: Any) -> None:
        seen.append((name, interval_s, fn))

    monkeypatch.setattr(workers, "run_forever", fake_forever)
    monkeypatch.setattr(settings, "tokens_refresh_interval_s", 6 * 3600.0)
    tasks = workers.start()
    try:
        # `create_task` builds the coroutine; the body runs on the next turn of the loop
        await asyncio.sleep(0)
        row = next((r for r in seen if r[2] is tokens.refresh_once), None)
        assert row is not None, f"no token-list loop among {[r[0] for r in seen]}"
        assert row[1] == 6 * 3600.0
    finally:
        for t in tasks:
            t.cancel()


# ----------------------------------------------------------------- item 4: a row is or is not


@pytest.mark.parametrize("decimals", [None, "", False])
def test_a_row_whose_decimals_are_not_a_number_is_dropped_never_read_as_zero(decimals):
    """⛔ `int(t.get("decimals") or 0)` turned all three of these into a token with 0 decimals —
    a picker row whose "1" means 1 wei. The docstring already said DROPPED; now it is."""
    rows = [
        {"address": "0xaa", "symbol": "GOOD", "decimals": 18},
        {"address": "0xbb", "symbol": "BAD", "decimals": decimals},
    ]
    assert [t["symbol"] for t in tokens.normalise(rows)] == ["GOOD"]


def test_a_missing_decimals_key_is_the_fourth_shape_of_the_same_hole():
    rows = [{"address": "0xcc", "symbol": "NODEC"}, {"address": "0xdd", "symbol": "ZERO", "decimals": 0}]
    # …and a genuine 0-decimals token is a NUMBER, so it stays
    assert [t["symbol"] for t in tokens.normalise(rows)] == ["ZERO"]


# ----------------------------------------------------------------- item 5: which file first


async def test_the_plain_file_is_written_before_its_gzip_twin(tokens_dir, router, monkeypatch):
    """The manifest states the sha of the PLAIN file. Writing the twin first meant a failure in
    between left a `.gz` that no manifest row describes being served to every gzip client."""
    order: list[str] = []
    real = tokens._replace

    def spy(path: Path, data: bytes) -> None:
        order.append(path.name)
        real(path, data)

    monkeypatch.setattr(tokens, "_replace", spy)
    await tokens.refresh_all()
    assert order[0] == "1.json" and order[1] == "1.json.gz"
    assert order.index("manifest.json") < order.index("manifest.json.gz")


async def test_a_gzip_twin_that_cannot_be_written_is_REMOVED_never_left_stale(
    tokens_dir, router, monkeypatch
):
    """nginx `gzip_static on` serves the twin to every client that accepts gzip. A stale twin is
    a stale token list served to almost everyone while the plain file — and the sha the manifest
    states — say something else. Deleted, so nginx falls back to the file we did write."""
    await tokens.refresh_all()
    first = (tokens_dir / "1.json").read_bytes()
    assert (tokens_dir / "1.json.gz").exists()

    router.rows[1] = [{"address": "0xfeed", "symbol": "NEW", "name": "New", "decimals": 18}]
    real = tokens._replace

    def spy(path: Path, data: bytes) -> None:
        if path.name.endswith(".gz"):
            raise OSError("no space left on device")
        real(path, data)

    monkeypatch.setattr(tokens, "_replace", spy)
    await tokens.refresh_all()

    plain = (tokens_dir / "1.json").read_bytes()
    assert plain != first and b"NEW" in plain
    assert not (tokens_dir / "1.json.gz").exists(), "a stale twin outlived the file it mirrors"
    # …and the manifest still describes what is actually on disk
    assert row_for(tokens_dir, 1)["sha256"] == hashlib.sha256(plain).hexdigest()


# ----------------------------------------------------------------- item 6: whose host is that


def test_a_logo_hosted_by_the_upstream_router_never_reaches_a_file_we_serve():
    """⛔ These files are served from OUR origin. A `logo` pointing at the upstream's CDN puts
    that host in front of every visitor's browser and names, on our own wire, the one party this
    whole codebase composes its hostname to avoid naming."""
    from urllib.parse import urlsplit

    from pgasme.config import ROUTER_BASE, ROUTER_STATS_BASE

    for base in (ROUTER_BASE, ROUTER_STATS_BASE):
        host = urlsplit(base).hostname or ""
        rows = [
            {"address": "0x01", "symbol": "A", "decimals": 18, "logoURI": f"https://{host}/a.png"},
            {
                "address": "0x02",
                "symbol": "B",
                "decimals": 18,
                "logoURI": f"https://cdn.{'.'.join(host.split('.')[-2:])}/b.png",
            },
            {"address": "0x03", "symbol": "C", "decimals": 18, "logoURI": "https://example.invalid/c.png"},
        ]
        out = tokens.normalise(rows)
        assert [t["symbol"] for t in out] == ["A", "B", "C"], "a bad logo drops the LOGO, not the token"
        assert out[0]["logo"] == "" and out[1]["logo"] == ""
        assert out[2]["logo"] == "https://example.invalid/c.png"


async def test_no_file_this_module_writes_carries_the_upstream_host(tokens_dir, router):
    """The whole-file assertion, because one row is not the claim: nothing we serve names them."""
    from urllib.parse import urlsplit

    from pgasme.config import ROUTER_BASE, ROUTER_MIRROR, ROUTER_STATS_BASE

    marks = {".".join((urlsplit(u).hostname or "").split(".")[-2:]) for u in (ROUTER_BASE, ROUTER_MIRROR, ROUTER_STATS_BASE)}
    router.rows[1] = [
        {"address": "0x0", "symbol": "ETH", "decimals": 18, "logoURI": f"https://assets.{sorted(marks)[0]}/eth.png"}
    ]
    await tokens.refresh_all()
    for f in sorted(tokens_dir.iterdir()):
        blob = gzip.decompress(f.read_bytes()) if f.suffix == ".gz" else f.read_bytes()
        for mark in marks:
            assert mark.encode() not in blob, f"{f.name} names {mark}"
