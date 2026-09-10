"""/internal is loopback-only, and it answers like a path that does not exist.

The route already refuses a caller that is not an un-proxied loopback caller — but ROUTING gets
there first, and routing leaks: FastAPI answers a GET on a POST-only route with **405**, which
tells an internet prober that the path exists and that only the method was wrong. The same
prober can also spell the path in ways a naive prefix test does not recognise (`//internal/…`,
`/INTERNAL/…`, `/internalx`).

  every method, every spelling, from anywhere but this machine: 404, before the body is read
  the loopback caller BeamPay is unchanged — same client, same 200
  and the access log drops the query of every one of those spellings, because the webhook
    credential can only ride in the url (`?token=…`) and an access log is kept for months
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from pgasme import main
from pgasme.config import settings
from pgasme.main import create_app

HOOK = "/internal/beampay/webhook"
TOKEN = "webhook-token-" + "z" * 32
BODY = {"event": "deposit_confirmed", "txId": "tx-1"}


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "beampay_webhook_token", TOKEN)
    return TOKEN


@pytest.fixture
def public() -> Any:
    """A client whose ASGI scope says the request came from somewhere else on the network."""

    async def make(host: str = "203.0.113.9") -> Any:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(), client=(host, 40000)),
            base_url="http://test",
        )

    return make


# ================================================== §1 · the answer is 404, never 405


async def test_every_method_from_a_public_client_is_404_and_never_405(public):
    async with await public() as c:
        for method in ("GET", "PUT", "HEAD", "DELETE", "PATCH", "OPTIONS", "POST"):
            r = await c.request(method, f"{HOOK}?token={TOKEN}")
            assert r.status_code == 404, method
    # …and the shape is the route's own refusal, so a prober cannot tell WHICH guard answered
    async with await public() as c:
        assert (await c.get(HOOK)).json() == {"detail": "not found"}


async def test_a_loopback_caller_is_the_one_that_still_sees_the_405(client):
    """The leak this guard closes, kept visible: on the loopback interface the route exists and
    says so. That answer is only ever given to this machine talking to itself."""
    assert (await client.get(f"{HOOK}?token={TOKEN}")).status_code == 405


async def test_a_public_caller_never_reaches_the_body(public):
    """The guard is OUTSIDE the JSON body check: a body that would answer 422 anywhere else
    still gets 404 here, so the refusal costs nothing and reveals nothing."""
    async with await public() as c:
        r = await c.post(
            f"{HOOK}?token={TOKEN}",
            content=b'{"event": NaN}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 404
        assert (await c.post(f"{HOOK}?token={TOKEN}", content=b"{")).status_code == 404


# ================================================== §2 · every spelling of the same path


def test_the_spellings_that_walk_around_a_naive_prefix_are_all_this_path():
    assert main.is_internal_path("/internal")
    assert main.is_internal_path("/internal/beampay/webhook")
    assert main.is_internal_path("//INTERNAL//beampay/webhook")
    assert main.is_internal_path("/Internal/")
    assert main.is_internal_path("/internalx")  # a prefix, not a directory
    assert not main.is_internal_path("/v1/internal")
    assert not main.is_internal_path("/")
    assert not main.is_internal_path("/v1/health")
    assert main.normalised_path("//INTERNAL//x") == "/internal/x"


async def test_every_spelling_is_404_from_a_public_client(public):
    """Absolute urls on purpose: `client.get("//internal/x")` is merged against the base url and
    the double slash is collapsed before it ever reaches the app, which would test nothing."""
    async with await public() as c:
        for path in (
            "/internal",
            "//internal/beampay/webhook",
            "/INTERNAL/beampay/webhook",
            "//INTERNAL//beampay/webhook",
            "/internalx",
        ):
            r = await c.get(f"http://test{path}?token={TOKEN}")
            assert r.status_code == 404, path


async def test_a_proxied_delivery_does_not_exist_however_it_is_spelled(client):
    """nginx sets these on everything it proxies, so their presence proves the request did not
    come from BeamPay's direct loopback post — whatever address the socket claims."""
    for header in ("X-Forwarded-For", "X-Real-IP", "Forwarded"):
        r = await client.post(f"{HOOK}?token={TOKEN}", json=BODY, headers={header: "203.0.113.9"})
        assert r.status_code == 404, header
        assert r.json() == {"detail": "not found"}


async def test_the_loopback_post_beampay_makes_is_untouched(client, mock_db):
    """The whole point of the endpoint: the guard must not close the door on the only caller it
    exists for."""
    r = await client.post(f"{HOOK}?token={TOKEN}", json=BODY)
    assert r.status_code == 200 and r.json() == {"ok": True, "stored": True}
    assert len(await mock_db["pgasme_test"].beampay_events.find({}).to_list(10)) == 1


# ================================================== §3 · and none of it reaches the access log


def line(target: str) -> str:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", target, "1.1", 200),
        None,
    )
    main.RedactAccessLog().filter(record)
    return record.getMessage()


def test_the_access_log_drops_the_query_of_every_internal_spelling():
    for path in ("/internal", "//internal/x", "/INTERNAL/x", "/internalx", "//INTERNAL//x"):
        logged = line(f"{path}?token=s3cr3t&event=failed")
        assert "s3cr3t" not in logged, path
        assert logged.endswith(f'{path}?<redacted> HTTP/1.1" 200'), path
    # nothing to redact when there is no query, and the path itself is kept as it was received
    assert '"POST /internal/beampay/webhook HTTP/1.1" 200' in line("/internal/beampay/webhook")
    # …and a path that only looks similar is logged in full
    assert "token=abc" in line("/v1/internal-notes?token=abc")
