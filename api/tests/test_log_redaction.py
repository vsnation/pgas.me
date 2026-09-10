"""The bot token never reaches a log record — 2026-09-10, and never again.

httpx logs one INFO line per request carrying the FULL url, and the Telegram send path's url is
`https://api.telegram.org/bot<TOKEN>/sendMessage`. So the operator group's bot token was written
into the API's log file 21 times, in cleartext, in a file that gets tailed, copied and pasted.

What this file pins, in the order the defences run:

  the two libraries that log a url per request are held at WARNING — the line is never made
  a record that IS made carries the mask, whatever level anyone sets later (belt and braces:
    a future debug flag must not re-leak, so the mask is applied where the record is BORN and
    every handler — a file, stdout, a test's capture — sees the same masked text)
  a traceback carrying the same url is masked too
  an ordinary record keeps its `args`, because the access-log filter reads them
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from pgasme import main, tg

# Shaped like the real thing (`bot<digits>:<secret>` is what the url spells) and matched by the
# same regex, but no digits/prefix any scanner would take for a live credential.
TOKEN = "1234567:pgas-test-not-a-real-token_0000000000"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """The REAL tg.send path against a mock transport — the requests it made.

    ⚠️ This is the one place in the suite where `PGAS_TG_LIVE=1`. It is set only AFTER the
    transport is replaced, so no socket can be opened and the operator's group cannot be
    reached; monkeypatch puts both back at the end of the test.
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
    monkeypatch.setenv("PGAS_TG_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("PGAS_TG_CHAT_ID", "-100999")
    monkeypatch.setenv("PGAS_TG_LIVE", "1")
    return seen


def test_the_libraries_that_log_a_url_per_request_are_quiet_after_import():
    """`pgasme.main` is imported by every client fixture in this suite, and importing it is what
    installs this. Their OWN level, not the effective one: a root logger raised to DEBUG by
    `uvicorn --log-level debug` must not un-quieten them."""
    assert main.QUIET_LOGGERS == ("httpx", "httpcore")
    for name in main.QUIET_LOGGERS:
        assert logging.getLogger(name).level >= logging.WARNING, name


def test_the_mask_is_one_implementation_and_the_root_logger_carries_it():
    assert main.redact_secrets(f"POST {URL}") == (
        "POST https://api.telegram.org/bot<REDACTED>/sendMessage"
    )
    assert main.redact_secrets("nothing to hide here") == "nothing to hide here"
    assert any(isinstance(f, main.RedactSecrets) for f in logging.getLogger().filters)


def test_installing_it_twice_does_not_stack_a_second_factory():
    """`create_app()` runs per test in this suite and the module ran it at import: an install
    that wrapped the record factory again on every call would nest hundreds of them."""
    factory = logging.getLogRecordFactory()
    main.install_log_redaction()
    main.install_log_redaction()
    assert logging.getLogRecordFactory() is factory
    carried = [f for f in logging.getLogger().filters if isinstance(f, main.RedactSecrets)]
    assert len(carried) == 1


async def test_the_bot_token_never_reaches_a_record_even_with_httpx_at_debug(telegram, caplog):
    """The leak itself, reproduced with the library talking as loudly as it can."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")  # ← the future debug flag
    assert await tg.send("a message the operator should get") is True

    # the request really was made, and really did carry the token: without this the assertions
    # below would pass on a path that never ran
    assert len(telegram) == 1 and telegram[0].url.host == "api.telegram.org"
    assert TOKEN in str(telegram[0].url)
    from_httpx = [r for r in caplog.records if r.name == "httpx"]
    assert from_httpx, "httpx logged nothing — the leak path was not exercised"

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in formatter.format(record)
    assert any(main.BOT_TOKEN_MASK in r.getMessage() for r in from_httpx)


def test_a_traceback_carrying_the_url_is_masked_too(caplog):
    """httpx names the url in `HTTPStatusError`, and `log.exception` writes the whole traceback.
    A formatter reuses `exc_text` when it is already set, which is the only place a filter can
    reach a traceback's text at all."""
    caplog.set_level(logging.ERROR)
    log = logging.getLogger("pgasme.tests.redaction")
    try:
        raise RuntimeError(f"Client error '401 Unauthorized' for url '{URL}'")
    except RuntimeError:
        log.exception("the send failed")
    record = caplog.records[-1]
    formatted = logging.Formatter("%(message)s").format(record)
    assert TOKEN not in formatted
    assert main.BOT_TOKEN_MASK in formatted


def test_an_ordinary_record_keeps_its_args(caplog):
    """The mask clears `args` only when it actually masked something. uvicorn's access log is
    formatted from `args`, and `RedactAccessLog` reads `args[2]` — a filter that flattened every
    record would silently stop redacting destination addresses."""
    caplog.set_level(logging.INFO)
    logging.getLogger("pgasme.tests.redaction").info("%s - %s", "1.2.3.4:5", "/v1/health")
    record = caplog.records[-1]
    assert record.args == ("1.2.3.4:5", "/v1/health")
    assert record.getMessage() == "1.2.3.4:5 - /v1/health"
