"""The operator panel's door — 2026-09-10 (T38).

What this file pins, in the order the guard runs:

  an UNPROVISIONED box answers 404 to every /admin route, for ever — no 401, no 403, no 429,
    and no amount of knocking changes that: a prober must not learn the family is mounted
  a key too short to be a secret is NOT a key (the same `secret_problem` test jwt_secret and
    account_salt face at boot) — presenting it changes nothing
  a wrong key is the same 404 as no key, and it WRITES A ROW (law 12) that names the caller and
    the route and never the key
  the right key works through either header the panel and a curl one-liner use
  five failures from one address inside the window turn the answer into 429 — and reach
    Telegram ONCE, however many more arrive
  a non-ASCII header is a refusal, never a 500 (`compare_digest` on `str` raises TypeError)
  the key never reaches a log record, and an /admin access-log line never carries its query
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from pgasme import main, tg
from pgasme.config import settings
from pgasme.routers import admin

# ≥ 32 chars and not a placeholder — the shape `secrets.token_urlsafe(32)` produces on the box.
KEY = "admin-test-key-" + "0123456789abcdef" * 2
WRONG = "admin-test-key-" + "fedcba9876543210" * 2


@pytest.fixture(autouse=True)
def clean_counter() -> Any:
    """The failure counter is per PROCESS (it must not be a write from a read-only router), so
    a window one test filled is a window the next one would inherit by accident."""
    admin.reset_failures()
    yield
    admin.reset_failures()


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "admin_key", KEY)
    return KEY


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """The REAL `tg.send` path against a mock transport — the requests it actually made.

    Same shape as tests/test_log_redaction.py: `PGAS_TG_LIVE=1` is set only AFTER the transport
    is replaced, so no socket can be opened and the operator's group cannot be reached.
    """
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    real = httpx.AsyncClient

    def fake(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handle)
        return real(*args, **kwargs)

    monkeypatch.setattr(tg.httpx, "AsyncClient", fake)
    monkeypatch.setenv("PGAS_TG_BOT_TOKEN", "1234567:pgas-test-not-a-real-token_0000000000")
    monkeypatch.setenv("PGAS_TG_CHAT_ID", "-100999")
    monkeypatch.setenv("PGAS_TG_LIVE", "1")
    return seen


ROUTES = (
    "/admin/overview",
    "/admin/deposits",
    "/admin/deposits/whatever",
    "/admin/payouts",
    "/admin/payouts/whatever",
    "/admin/unattributed",
    "/admin/accounts",
    "/admin/treasury",
    "/admin/events",
    "/admin/beampay-events",
    "/admin/raw/deposits/whatever",
)


async def test_with_no_key_configured_every_route_is_a_404(client):
    """The default posture of every deployment that has not provisioned the panel. NOT 401:
    the answer an unrouted path gives, so nothing here is discoverable.

    ⛔ The body is compared with the framework's OWN, fetched here, and never with a literal:
    it used to be `{"detail":"not found"}` against the framework's `{"detail":"Not Found"}` and
    the assertion that pinned the literal passed happily through the whole difference."""
    assert settings.admin_key == ""
    unrouted = await client.get("/there-is-no-route-here-at-all")
    for path in ROUTES:
        r = await client.get(path)
        assert r.status_code == 404, path
        assert r.content == unrouted.content, path


async def test_a_wrong_verb_does_not_prove_the_path_exists(client):
    """⛔ Starlette answers a POST on a GET-only route with 405 — and a 405 tells an
    unauthenticated prober that `/admin/overview` is a real path on this box, which is exactly
    what the 404 above is for. Measured before the fix: it really did answer 405. The catch-all
    at the end of the router closes it, for every method and every spelling."""
    unrouted = await client.post("/there-is-no-route-here-at-all")
    for method in ("post", "put", "patch", "delete"):
        r = await getattr(client, method)("/admin/overview")
        assert r.status_code == 404, (method, r.status_code)
        assert r.content == unrouted.content, method
    # an unrouted path under the prefix answers the same thing, so neither is distinguishable
    assert (await client.get("/admin/does-not-exist")).status_code == 404
    assert (await client.delete("/admin/deposits/whatever")).status_code == 404


# The body an UNROUTED path answers with. Not typed as a literal anywhere below: it is read from
# the framework in the test that needs it, because the thing being pinned is "the same as", and a
# hand-copied constant is a second implementation of the answer (law 9) that would keep passing on
# the day the framework's wording changed and the gate's did not.
UNROUTED = "/there-is-no-route-here-at-all"


async def test_the_prefix_itself_answers_like_an_unrouted_path(client):
    """⛔ H1. `/admin` — no trailing slash — used to answer **307 to /admin/**.

    The catch-all is registered on a router with a `/admin` prefix, so its real path is
    `/admin/{rest:path}`; `/admin` matches none of the routes and Starlette's `redirect_slashes`
    then offered the spelling that does. A redirect is a YES: it says the family is mounted, on a
    box that has not provisioned the panel and on which every other answer is deliberately the
    404 an unrouted path gives. Measured before the fix, with NO key configured:
    `GET/POST/HEAD /api/admin -> 307 location: /admin/`.

    Every method and both spellings, with and without a key, now answer the 404."""
    assert settings.admin_key == ""
    for method in ("get", "post", "put", "patch", "delete", "head", "options"):
        for path in ("/admin", "/admin/", "/admin/x"):
            r = await getattr(client, method)(path)
            assert r.status_code == 404, (method, path, r.status_code, r.headers.get("location"))
            assert "location" not in r.headers, (method, path)


async def test_the_prefix_is_not_discoverable_with_a_key_either(client, key):
    """The same, once the panel IS provisioned: `/admin` is not a page, and a caller holding the
    key learns no more about the mount point than one who does not."""
    for method in ("get", "post", "head", "options"):
        for path in ("/admin", "/admin/"):
            r = await getattr(client, method)(path, headers={"X-Admin-Key": KEY})
            assert r.status_code == 404, (method, path, r.status_code)
            assert "location" not in r.headers, (method, path)


async def test_the_refusal_is_byte_identical_to_the_frameworks_own_404(client, key):
    """⛔ M1. The gate answered `{"detail":"not found"}` where the framework answers
    `{"detail":"Not Found"}` — same length, different bytes, and a prober comparing the two
    learns that `/admin` is handled by something rather than by nothing. Both spellings of the
    refusal (no key, wrong key) are now the framework's own answer, header for header."""
    unrouted = await client.get(UNROUTED)
    assert unrouted.status_code == 404
    for label, r in (
        ("no key", await client.get("/admin/overview")),
        ("wrong key", await client.get("/admin/overview", headers={"X-Admin-Key": WRONG})),
        ("unrouted under the prefix", await client.get("/admin/nope", headers={"X-Admin-Key": KEY})),
        ("wrong verb", await client.post("/admin/overview", headers={"X-Admin-Key": KEY})),
    ):
        assert r.status_code == unrouted.status_code, label
        assert r.content == unrouted.content, (label, r.content, unrouted.content)
        assert r.headers["content-type"] == unrouted.headers["content-type"], label
        assert r.headers["content-length"] == unrouted.headers["content-length"], label
    # …and the same on a box with no key configured at all
    admin.reset_failures()
    settings_key, settings.admin_key = settings.admin_key, ""
    try:
        bare = await client.get("/admin/overview")
    finally:
        settings.admin_key = settings_key
    assert bare.status_code == 404 and bare.content == unrouted.content


async def test_head_and_options_are_answered_on_a_real_route(client, key):
    """L2. A panel route is a READ: `HEAD` must answer what `GET` answers with no body, and
    `OPTIONS` must say which verbs it takes — 405 or 404 on either would be a route that
    behaves differently from the one it is. The catch-all still refuses everything else."""
    ok = await client.get("/admin/overview", headers={"X-Admin-Key": KEY})
    head = await client.head("/admin/overview", headers={"X-Admin-Key": KEY})
    assert (ok.status_code, head.status_code) == (200, 200)
    assert head.content == b""
    assert head.headers["content-type"] == ok.headers["content-type"]

    opt = await client.options("/admin/overview", headers={"X-Admin-Key": KEY})
    assert opt.status_code == 204
    allow = opt.headers["allow"]
    assert {v.strip() for v in allow.split(",")} == {"GET", "HEAD", "OPTIONS"}
    assert opt.content == b""

    # ⛔ and NOT on a path that does not exist: the catch-all's 404 is what makes a wrong path
    # indistinguishable from an unrouted one, and an OPTIONS that answered 204 everywhere would
    # map the whole family for anyone holding the key.
    for method in ("head", "options"):
        r = await getattr(client, method)("/admin/not-a-route", headers={"X-Admin-Key": KEY})
        assert r.status_code == 404, (method, r.status_code)


async def test_head_and_options_are_refused_without_the_key(client, key):
    """The guard is on the ROUTER, so a verb added to a route inherits it — asserted rather than
    assumed, because a new verb is exactly where a guard gets forgotten."""
    for method in ("head", "options"):
        assert (await getattr(client, method)("/admin/overview")).status_code == 404, method


def test_the_docstring_says_which_refusal_is_the_first_429():
    """L3. It said "the sixth answer becomes 429". `note_failure` returns the count INCLUDING
    this one and the guard escalates at `n >= FAIL_LIMIT`, so the FIFTH refusal is the first 429
    — which is what `test_five_failures_become_a_429_and_exactly_one_page` measures. A comment
    that describes a different guard from the one below it is worse than no comment."""
    doc = admin.__doc__ or ""
    assert "sixth answer becomes 429" not in doc
    assert "fifth" in doc.lower()
    assert admin.FAIL_LIMIT == 5


async def test_an_unprovisioned_box_never_escalates_to_429(client, telegram):
    """Even knocking past the limit: with nothing to protect, a 429 would be the one answer that
    proves the route family exists. The operator is still paged once."""
    for _ in range(FAIL_LIMIT_TEST := admin.FAIL_LIMIT + 4):
        assert (await client.get("/admin/overview")).status_code == 404
    assert FAIL_LIMIT_TEST > admin.FAIL_LIMIT
    assert len(telegram) == 1


async def test_a_key_too_short_to_be_a_secret_is_not_a_key(client, monkeypatch):
    """Fail-closed: half a secret must never be a whole door. The same test the boot-time
    secrets face — so a 12-character PGAS_ADMIN_KEY leaves the panel unmounted, not weak."""
    monkeypatch.setattr(settings, "admin_key", "short-key")
    assert admin.configured_key() == ""
    r = await client.get("/admin/overview", headers={"X-Admin-Key": "short-key"})
    assert r.status_code == 404


async def test_a_wrong_key_is_the_same_404_and_writes_a_row(client, key, caplog):
    caplog.set_level(logging.WARNING)
    r = await client.get("/admin/overview", headers={"X-Admin-Key": WRONG})
    assert r.status_code == 404
    assert r.content == (await client.get("/there-is-no-route-here-at-all")).content
    rows = [x for x in caplog.records if x.name == "pgasme.admin"]
    assert rows, "a refusal that only printed would be unalertable (law 12)"
    text = rows[-1].getMessage()
    assert "refused GET /admin/overview" in text and "the key did not match" in text
    # neither the key that was tried nor the one configured — and not the MASK either, which
    # proves the line never carried a secret rather than merely having it scrubbed afterwards
    assert WRONG not in text and KEY not in text and main.ADMIN_KEY_MASK not in text


async def test_the_right_key_opens_it_through_either_header(client, key):
    for headers in ({"X-Admin-Key": KEY}, {"Authorization": f"Bearer {KEY}"}):
        r = await client.get("/admin/overview", headers=headers)
        assert r.status_code == 200, headers
        assert r.json()["env"] == "test"


async def test_a_non_ascii_header_is_a_refusal_not_a_500(client, key):
    """`secrets.compare_digest` on `str` raises TypeError the moment either side is not
    ASCII-only, and the header is whatever the caller typed. The webhook route learned this the
    hard way: a 500 is an invitation to retry.

    Sent as BYTES because httpx refuses to encode a non-ASCII header value itself — which is
    also how such a header reaches a real server, and Starlette hands it back as a latin-1 str
    whose code points are still outside ASCII."""
    r = await client.get("/admin/overview", headers={"X-Admin-Key": "tüken".encode()})
    assert r.status_code == 404
    # …and the same value in the Bearer spelling
    r = await client.get("/admin/overview", headers={"Authorization": "Bearer tüken".encode()})
    assert r.status_code == 404


async def test_five_failures_become_a_429_and_exactly_one_page(client, key, telegram):
    for n in range(admin.FAIL_LIMIT - 1):
        assert (await client.get("/admin/overview")).status_code == 404, n
    assert telegram == []
    for _ in range(4):  # the fifth and everything after it
        r = await client.get("/admin/overview")
        assert r.status_code == 429
        assert r.headers["Retry-After"] == str(settings.rate_window_s)
    # ONE page for the whole burst — `tg.send`'s own cooldown under a per-address key is the
    # digest, and a pager that fired five times is a pager the operator learns to ignore.
    assert len(telegram) == 1
    body = telegram[0].content.decode()
    assert "admin-panel attempts" in body and "429" in body
    assert KEY not in body  # the page names the count and the address, never the secret


async def test_the_limit_is_per_address(client, key, telegram):
    """One noisy address must not lock the operator out from another."""
    for _ in range(admin.FAIL_LIMIT):
        await client.get("/admin/overview", headers={"X-Forwarded-For": "9.9.9.9"})
    # the ASGI transport reports one client address for both, so the counter is asserted
    # directly: what matters is that the key is the address and not a global counter
    assert list(admin._fails) == ["127.0.0.1"]
    assert (await client.get("/admin/overview", headers={"X-Admin-Key": KEY})).status_code == 200


async def test_a_successful_read_logs_the_route_and_the_caller_and_nothing_else(
    client, key, caplog
):
    caplog.set_level(logging.INFO)
    r = await client.get("/admin/deposits?account=0xabc&status=credited", headers={"X-Admin-Key": KEY})
    assert r.status_code == 200
    rows = [x for x in caplog.records if x.name == "pgasme.admin"]
    assert len(rows) == 1
    text = rows[0].getMessage()
    assert text == "admin: GET /admin/deposits from 127.0.0.1"
    # the QUERY carries the filters — an account address in a log next to the IP that asked for
    # it is exactly what main.RedactAccessLog exists to prevent
    assert "0xabc" not in text and "credited" not in text
    for record in caplog.records:
        assert KEY not in record.getMessage()


def test_the_admin_key_is_masked_in_any_record_that_does_carry_it(monkeypatch, caplog):
    """Belt for the same braces as the bot token: the header is not in the access log, but a
    traceback that renders the request, or a hand-written debug line, would be."""
    monkeypatch.setattr(settings, "admin_key", KEY)
    assert main.carries_secret(f"X-Admin-Key: {KEY}") is True
    assert main.redact_secrets(f"X-Admin-Key: {KEY}") == f"X-Admin-Key: {main.ADMIN_KEY_MASK}"
    caplog.set_level(logging.INFO)
    logging.getLogger("pgasme.tests.admin").info("headers=%s", {"x-admin-key": KEY})
    record = caplog.records[-1]
    assert KEY not in record.getMessage()
    assert main.ADMIN_KEY_MASK in record.getMessage()


def test_without_a_key_configured_the_mask_leaves_ordinary_text_alone(monkeypatch):
    """An empty key must not turn `str.replace("", …)` into a mask between every character."""
    monkeypatch.setattr(settings, "admin_key", "")
    assert main.redact_secrets("nothing to hide here") == "nothing to hide here"
    assert main.carries_secret("nothing to hide here") is False


def test_an_admin_access_log_line_loses_its_query():
    """uvicorn writes the full request target beside the client address. `/admin/...` is added
    to the same filter that already drops `/internal`'s — and through the SAME prefix the router
    itself declares, so the two cannot drift."""
    fmt = '%s - "%s %s HTTP/%s" %d'
    args = ("1.2.3.4:5", "GET", "/admin/deposits?account=0xabc", "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, fmt, args, None)
    assert main.RedactAccessLog().filter(record) is True
    assert record.args[2] == "/admin/deposits?<redacted>"
    # every spelling of the family, exactly as the internal guard treats its own
    assert main.is_admin_path("//ADMIN/deposits") is True
    assert main.is_admin_path("/v1/account") is False
    assert admin.PREFIX == "/admin"


def test_a_line_with_no_query_is_left_alone():
    fmt = '%s - "%s %s HTTP/%s" %d'
    args = ("1.2.3.4:5", "GET", "/admin/overview", "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, fmt, args, None)
    main.RedactAccessLog().filter(record)
    assert record.args[2] == "/admin/overview"
