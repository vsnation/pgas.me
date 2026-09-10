"""The token lists, hosted by us instead of proxied per visitor.

Why this file exists (measured 2026-09-10 through the edge): a chain's token list reached the
client through `GET /v1/dex/tokens` on every chain switch — Ethereum 1.3 MB / 6,617 tokens /
2.5 s, Base 0.66 MB / 0.6 s — and the first quote waited for it. The list is the same for every
visitor and changes daily at most, so it is fetched ONCE by us, written as a static file, and
served by nginx (and cached at the edge) as `/tokens/<evm chain id>.json`.

What this module is shaped by:

  * **ONE implementation of a public token row.** `normalise()` builds the rows for the static
    file AND for the `/v1/dex/tokens` fallback, so a client that falls back never gets a
    different answer to the same question (law 9: two implementations of one fact will
    disagree). The fallback keeps its own behaviour — this only stops the shapes drifting.
  * **A refresh that cannot write REFUSES.** An unwritable directory raises `TokensError`; the
    worker loop logs it and pages once. Nothing is half-written and the API never dies of it:
    the old files keep being served and the manifest says exactly how old they are.
  * **Never a partial file.** Every file is written to a temporary name in the SAME directory
    (`os.replace` is atomic only within one filesystem), fsynced, chmod 0644 — `mkstemp` makes
    0600 files and nginx would answer 403 on one — and renamed over the target. A reader sees
    the whole old file or the whole new one, never a truncated list.
  * **An empty answer is not evidence.** A chain whose list comes back empty keeps the file it
    had; it is a failed chain, not an empty list, because an empty static file would be served
    for six hours as if it were the truth.
  * **One chain's failure is one chain's failure.** The run continues, that chain keeps its
    previous file, and its manifest row keeps the timestamp of the last SUCCESSFUL write — so
    `updated_at` per chain is when that file was actually written, never when a run touched it.
  * **The manifest carries no upstream error text.** These files are public: an error string
    would put the upstream's own host name into content we serve. Failures are `failed` chain
    ids in the manifest and the detail in our log.

Times are epoch seconds (floats), the same clock the rest of the API records with.

CLI (the deploy runs it once after the API restart):

    python -m pgasme.tokens refresh              # fetch every chain, write the files
    python -m pgasme.tokens status               # print the manifest summary
    python -m pgasme.tokens status --min-chains 10   # …and exit 1 if fewer are listed
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import tg, xchain
from .config import ROUTER_BASE, ROUTER_MIRROR, ROUTER_STATS_BASE, settings

log = logging.getLogger("pgasme.tokens")

MANIFEST = "manifest.json"
# One key for the "the refresher cannot write" condition, so it pages once per cooldown and not
# once per pass: a monitor that repeats a condition the operator already knows trains them to
# ignore the pager.
DIR_ALERT_KEY = "tokens-dir"
DIR_ALERT_COOLDOWN_S = 6 * 3600.0
FILE_MODE = 0o644  # nginx (an unprivileged worker) has to be able to read what we write
# How old the hosted lists may be before the deploy's own assertion calls them stale. The
# refresher runs every 6 h, so a day is four missed passes: long enough that a restart or a brief
# upstream outage is not a failed deploy, short enough that "these files are frozen" is caught by
# the deploy that would otherwise ship them.
MAX_AGE_S = 24 * 3600.0


def upstream_domains() -> tuple[str, ...]:
    """The registrable domains of the upstream router, derived from the SAME composed constants
    the client calls (`config.ROUTER_*`) — never written out here.

    Deriving them is the point: `config.py` is the one place this codebase spells the upstream's
    own vocabulary, and a second spelling in this module would be both a second implementation of
    one fact and the very string `publish.sh` refuses to let into the public tree."""
    out: set[str] = set()
    for url in (ROUTER_BASE, ROUTER_MIRROR, ROUTER_STATS_BASE):
        parts = (urlsplit(url).hostname or "").split(".")
        if len(parts) >= 2:
            out.add(".".join(parts[-2:]))
    return tuple(sorted(out))


def public_logo(url: Any) -> str:
    """A logo URL fit to appear in a file WE serve, or `""`.

    ⛔ These files come from our own origin, so every URL in them is a request the visitor's
    browser makes because we told it to. A logo on the upstream router's CDN would put that host
    in front of every visitor and name, on our own wire, the one party this codebase composes its
    hostnames to avoid naming (§9 privacy). The token still ships — a picker row with no icon is
    a picker row; the client already draws the symbol when `logo` is empty."""
    if not isinstance(url, str) or not url.strip():
        return ""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        # nothing to compare: a relative path, a data: URI, a string that is not a URL at all.
        # It names no host, so it does not name the upstream's, and that is this function's only
        # question.
        return url
    for domain in upstream_domains():
        if host == domain or host.endswith("." + domain):
            return ""
    return url


class TokensError(RuntimeError):
    """The refresher cannot do its job — an unwritable directory, an empty list, a bad row.

    Distinct from `xchain.XchainError` on purpose: one is "the upstream did not answer", the
    other is "we cannot store what it answered", and they are fixed by different people."""


# ----------------------------------------------------------------------------- the public row


def normalise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`[{address, symbol, name, decimals, logo}]` — the shape both the static file and
    `/v1/dex/tokens` hand to the client, in the order the source gave (native first).

    A row without an address, or with a `decimals` that is not a number, is DROPPED and counted
    in the log: one unparseable row out of 6,617 must not cost the client the whole list. It is
    never guessed — a dropped row is a token the picker does not offer, which the wallet's own
    holdings still cover.

    ⛔ `decimals` MUST BE A NUMBER, and `None`, `""`, `False` and a missing key are none of them.
    `int(t.get("decimals") or 0)` read all four as **0 decimals**, which is not "unknown": it is
    the claim that this token's raw units ARE its display units, and a picker built on it offers
    "1 USDC" for one millionth of a cent. `False` needs saying separately because `isinstance(
    False, int)` is True in Python — the one shape a numeric check does not catch by itself.

    A `logo` the upstream hosts is dropped (see `public_logo`); the row itself is kept."""
    out: list[dict[str, Any]] = []
    dropped = 0
    logos = 0
    for t in rows:
        addr = t.get("address")
        if not addr:
            dropped += 1
            continue
        raw = t.get("decimals")
        if raw is None or isinstance(raw, bool) or (isinstance(raw, str) and not raw.strip()):
            dropped += 1
            continue
        try:
            decimals = int(raw)
        except (TypeError, ValueError):
            dropped += 1
            continue
        logo = public_logo(t.get("logoURI"))
        if logo != (t.get("logoURI") or ""):
            logos += 1
        out.append(
            {
                "address": addr,
                "symbol": t.get("symbol") or "",
                "name": t.get("name") or "",
                "decimals": decimals,
                "logo": logo,
            }
        )
    if dropped:
        log.warning("token list: dropped %d unreadable row(s) of %d", dropped, len(rows))
    if logos:
        log.info("token list: dropped %d logo(s) hosted by the upstream router", logos)
    return out


# ----------------------------------------------------------------------------- the directory


def dir_path() -> Path:
    return Path(settings.tokens_dir)


def ensure_dir() -> Path:
    """The directory, created if missing. Raises `TokensError` when it cannot be created or
    cannot be written — the caller's job is to say so, not to pretend it wrote."""
    if not str(settings.tokens_dir).strip():
        # Path("") is Path(".") — an unset directory would quietly write the lists into whatever
        # the process's working directory happens to be. Empty is a refusal, never a default.
        raise TokensError("PGAS_TOKENS_DIR is empty")
    d = dir_path()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise TokensError(f"{d}: cannot create ({type(e).__name__}: {e})") from e
    if not os.access(d, os.W_OK | os.X_OK):
        raise TokensError(f"{d}: not writable by this process")
    return d


def _replace(path: Path, data: bytes) -> None:
    """Write `data` at `path` atomically. Temp file in the same directory → fsync → chmod →
    `os.replace`. A failure leaves the previous file exactly as it was."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_pair(d: Path, name: str, data: bytes) -> None:
    """`name` and its pre-compressed twin `name.gz` (nginx `gzip_static on` serves the twin to
    every client that accepts gzip). `mtime=0` keeps the bytes deterministic for equal content.

    ⛔ THE PLAIN FILE IS THE ONE THE MANIFEST DESCRIBES, SO IT IS WRITTEN FIRST. The twin used to
    go first, on the reasoning that a new plain file should never be newer than the twin — but
    the failure that reasoning was protecting against runs the other way: the twin lands, the
    plain replace fails, and `gzip_static` then serves that twin, whose sha no manifest row
    states, to every client that accepts gzip. Almost everyone.

    And a twin that cannot be written is DELETED rather than left behind, because a stale twin is
    a stale token list served to almost everyone. With no `.gz` present nginx falls back to the
    plain file we did write — slower, and correct. A failed twin is therefore NOT a failed
    refresh: it is logged, the manifest still describes exactly what is on disk, and the next
    pass re-writes it."""
    _replace(d / name, data)
    gz = d / f"{name}.gz"
    try:
        _replace(gz, gzip.compress(data, compresslevel=9, mtime=0))
    except OSError as e:
        log.error("token list %s: the gzip twin could not be written (%s: %s) — removing it so "
                  "nginx serves the plain file", name, type(e).__name__, e)
        with contextlib.suppress(OSError):
            gz.unlink()


# ----------------------------------------------------------------------------- the manifest

_manifest_cache: dict[str, Any] = {"key": None, "data": {}}


def read_manifest() -> dict[str, Any]:
    """The manifest on disk, or `{}` when there is none / it cannot be read. NEVER raises: it is
    read by `/v1/health`, and a health endpoint that dies on a missing file tells the operator
    nothing. Cached on (mtime, size) so health is a `stat`, not a parse, between refreshes."""
    path = dir_path() / MANIFEST
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _manifest_cache["key"] == key:
            return _manifest_cache["data"]
        data = json.loads(path.read_bytes())
    except (OSError, ValueError) as e:
        log.debug("token manifest unreadable: %s: %s", type(e).__name__, e)
        _manifest_cache.update({"key": None, "data": {}})
        return {}
    if not isinstance(data, dict):
        _manifest_cache.update({"key": None, "data": {}})
        return {}
    _manifest_cache.update({"key": key, "data": data})
    return data


def clear_cache() -> None:
    """Forget the parsed manifest (tests, and anything that moves `PGAS_TOKENS_DIR`)."""
    _manifest_cache.update({"key": None, "data": {}})


def health() -> dict[str, Any]:
    """What `/v1/health` says about the hosted lists: `{updated_at, chains, age_s, failed}`.

    `updated_at: null` means there is no manifest at all — which is what a box that has never
    run the refresher looks like, and what the watchdog pages on together with `age_s`. Never
    raises, and never reports an age it did not read.

    ⛔ `failed` IS THE SIGNAL A COUNT CANNOT CARRY. A chain that stops refreshing keeps its file
    and its row, so `chains` does not move and `age_s` — now the age of the STALEST chain — is
    the only other tell. The ids say WHICH, which is what an operator needs to act, and they are
    ids: the upstream's own error text never reaches a public payload (§9)."""
    m = read_manifest()
    at = m.get("updated_at")
    at = float(at) if isinstance(at, (int, float)) else None
    failed = m.get("failed")
    return {
        "updated_at": at,
        "chains": len(m.get("chains") or []),
        "age_s": round(time.time() - at, 1) if at else None,
        "failed": [int(c) for c in failed if isinstance(c, (int, float))]
        if isinstance(failed, list)
        else [],
    }


# ----------------------------------------------------------------------------- the refresh


async def refresh_chain(evm_chain_id: int, route_chain_id: int, d: Path) -> dict[str, Any]:
    """One chain: fetch, normalise, write `<evm id>.json` (+ `.gz`). Returns its manifest row.

    `force=True` on the fetch — the in-process 10-minute cache exists for request traffic; a
    refresher that re-wrote its own cached copy every six hours would freeze the lists on
    whatever the first pass saw."""
    rows = await xchain.token_list(int(route_chain_id), force=True)
    toks = normalise(rows)
    if not toks:
        raise TokensError(f"chain {evm_chain_id}: empty token list — the old file is kept")
    now = time.time()
    body = {"chain_id": int(evm_chain_id), "updated_at": round(now, 3), "tokens": toks}
    data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    _write_pair(d, f"{int(evm_chain_id)}.json", data)
    return {
        "chain_id": int(evm_chain_id),
        "count": len(toks),
        "sha256": hashlib.sha256(data).hexdigest(),
        "updated_at": round(now, 3),
    }


async def refresh_all() -> dict[str, Any]:
    """Every chain the router lists, then the manifest. Returns a summary for the CLI/worker.

    Raises `TokensError` only for conditions that make the whole run pointless (no writable
    directory, not one chain written); a single chain that fails keeps its previous file and its
    previous manifest row, so `updated_at` on a row is always when that file was last WRITTEN."""
    d = ensure_dir()
    chains = await xchain.supported_chains(force=True)
    previous = {
        int(c["chain_id"]): c
        for c in (read_manifest().get("chains") or [])
        if isinstance(c, dict) and isinstance(c.get("chain_id"), int)
    }
    rows: list[dict[str, Any]] = []
    failed: list[int] = []
    for c in chains:
        try:
            evm = int(c["originalChainId"])
            internal = int(c["chainId"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            rows.append(await refresh_chain(evm, internal, d))
        except (xchain.XchainError, TokensError, OSError) as e:
            # the detail goes to OUR log, never into a file we serve
            log.warning("token list %s not refreshed: %s: %s", evm, type(e).__name__, e)
            failed.append(evm)
            if evm in previous:
                rows.append(previous[evm])
    if not rows:
        raise TokensError("no chain could be written — the previous files (if any) are unchanged")
    rows.sort(key=lambda r: r["chain_id"])
    # ⛔ THE MANIFEST AGES WITH ITS STALEST CHAIN, NOT WITH THE LAST RUN. `time.time()` here said
    # "fresh" after a pass in which every single chain failed and kept the file it already had:
    # `age_s` on /v1/health — the one number the watchdog and the deploy read to decide whether
    # what nginx serves is current — could never rise above one refresh interval, whatever was
    # actually on disk. The oldest per-chain write is the honest answer to "how old are these
    # lists", and it is the answer that pages.
    ages = [float(r["updated_at"]) for r in rows if isinstance(r.get("updated_at"), (int, float))]
    manifest = {
        "updated_at": round(min(ages), 3) if ages else round(time.time(), 3),
        "chains": rows,
        "failed": sorted(failed),
    }
    _write_pair(
        d, MANIFEST, json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    clear_cache()
    log.info(
        "token lists refreshed: %d chain(s), %d failed, %d token(s) → %s",
        len(rows),
        len(failed),
        sum(int(r.get("count") or 0) for r in rows),
        d,
    )
    return {
        "ok": True,
        "dir": str(d),
        "chains": len(rows),
        "failed": sorted(failed),
        "tokens": sum(int(r.get("count") or 0) for r in rows),
        "updated_at": manifest["updated_at"],
    }


async def refresh_once() -> dict[str, Any]:
    """The loop body a worker runs (every `PGAS_TOKENS_REFRESH_INTERVAL_S`, and once at boot).

    An unwritable directory is an operator condition, not a crash: it logs, pages ONCE per
    cooldown, and returns. Everything else propagates to the caller's own catch-log-alert."""
    try:
        return await refresh_all()
    except TokensError as e:
        log.error("token refresh skipped: %s", e)
        await tg.send(
            f"Token lists are not being refreshed: {tg.esc(str(e))[:200]} — the client falls "
            "back to the API proxy, which is slower but correct",
            key=DIR_ALERT_KEY,
            cooldown_s=DIR_ALERT_COOLDOWN_S,
        )
        return {"ok": False, "error": str(e)}


# ----------------------------------------------------------------------------- the CLI

USAGE = "usage: python -m pgasme.tokens refresh | status [--min-chains N]"


def _status(min_chains: int) -> int:
    """`status` prints; `status --min-chains N` ASSERTS.

    ⛔ The deploy runs the second form, and an assertion that only counts rows passes a manifest
    in which every chain has been frozen for a week — the count does not move when a chain fails,
    because a failed chain KEEPS its row (that is what makes one chain's failure one chain's
    failure). So the assertion reads all three facts the manifest states: enough chains, none of
    them failed in the run that wrote it, and the stalest of them written within `MAX_AGE_S`.
    Without `--min-chains` this is a report and exits 0 — a human asking "what is on this box"
    is not asking to be refused."""
    clear_cache()
    m = read_manifest()
    h = health()
    chains = m.get("chains") or []
    print(
        json.dumps(
            {
                "dir": str(dir_path()),
                "updated_at": h["updated_at"],
                "age_s": h["age_s"],
                "chains": len(chains),
                "failed": h["failed"],
                "tokens": sum(int(c.get("count") or 0) for c in chains if isinstance(c, dict)),
            },
            indent=2,
        )
    )
    if not min_chains:
        return 0
    problems: list[str] = []
    if len(chains) < min_chains:
        problems.append(f"lists {len(chains)} chain(s), expected at least {min_chains}")
    if h["failed"]:
        problems.append(f"chain(s) {', '.join(str(c) for c in h['failed'])} could not be refreshed")
    if h["age_s"] is None:
        problems.append("has no readable timestamp")
    elif h["age_s"] > MAX_AGE_S:
        problems.append(f"is {h['age_s'] / 3600:.1f} h old (limit {MAX_AGE_S / 3600:.0f} h)")
    if problems:
        print(f"FAIL: {dir_path() / MANIFEST} " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cmd = argv[0] if argv else ""
    if cmd == "refresh":
        try:
            summary = asyncio.run(refresh_all())
        except (TokensError, xchain.XchainError) as e:
            print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(json.dumps(summary, indent=2))
        return 0
    if cmd == "status":
        min_chains = 0
        if "--min-chains" in argv:
            try:
                min_chains = int(argv[argv.index("--min-chains") + 1])
            except (IndexError, ValueError):
                print(USAGE, file=sys.stderr)
                return 2
        return _status(min_chains)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover — exercised by the deploy, not the suite
    raise SystemExit(main(sys.argv[1:]))
