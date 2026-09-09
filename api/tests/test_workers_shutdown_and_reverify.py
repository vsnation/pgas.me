"""Two things `workers.py` owed the payout side (T20 addendum, 2026-09-09).

  §A  SHUTDOWN GIVES THE LEASE BACK. The TTL is what makes a CRASH safe; handing the lease back
      is what makes a RESTART fast. Without it the dying process's claim stands for a whole
      `payout_lease_ttl_s`, so the newly started processor does nothing for two minutes and then
      pages that "a second payout processor is running" — about its own corpse. And only the
      OWNER may release: a process that lost its lease mid-pass must not free somebody else's
      claim on its way out, which would hand one wallet to two writers at the worst moment.
  §B  THE RE-VERIFY WINDOW IS A DEPLOY CADENCE, NOT AN INDEXING DELAY. `xchain_secondary` re-asks
      the router about a deposit that advanced past `submitted` before the index could vouch for it —
      but only while the row is inside `REVERIFY_WINDOW_S`, and the pass only runs while the API
      runs. At 24 h the flag flipped only if a deploy happened to land within a day of the
      deposit; the 2026-09-09 20:40Z deposit (credited, `verified: false`) is the row that
      proved it. A week covers a normal deploy cadence and is still bounded.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pgasme import payouts, workers, xchain

DB = "pgasme_test"
TX = "0x" + "ab" * 32
ORDER = "0x" + "cd" * 32


# ══════════════════════════ §A — stop() releases a lease we own ══════════════════════════════


async def test_stop_releases_the_lease_this_process_owns(mock_db):
    d = mock_db[DB]
    now = time.time()
    await d.leases.insert_one({"_id": payouts.LEASE_ID, "owner": payouts.OWNER, "at": now})

    await workers.stop([])

    row = await d.leases.find_one({"_id": payouts.LEASE_ID})
    # `at: 0.0` rather than a delete: `acquire_lease` already treats 0 as free, and the row keeps
    # who held it and when they let go, so the next process can name its predecessor.
    assert row["at"] == 0.0
    assert row["released_by"] == payouts.OWNER and row["released_at"] > 0


async def test_stop_never_frees_a_lease_another_process_holds(mock_db):
    d = mock_db[DB]
    now = time.time()
    await d.leases.insert_one({"_id": payouts.LEASE_ID, "owner": "another-process:1234", "at": now})

    await workers.stop([])

    row = await d.leases.find_one({"_id": payouts.LEASE_ID})
    assert row["owner"] == "another-process:1234" and row["at"] == now
    assert "released_at" not in row  # untouched: it was never ours to release


async def test_stop_cancels_every_task_before_it_touches_the_lease(mock_db):
    """The order matters: releasing while a pass is still executing would put the lease back on
    the shelf for another process to take while this one is still writing."""
    d = mock_db[DB]
    await d.leases.insert_one({"_id": payouts.LEASE_ID, "owner": payouts.OWNER, "at": time.time()})
    seen: list[str] = []

    async def loop_forever() -> None:
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen.append(await _lease_state(d))
            raise

    async def _lease_state(dd: Any) -> str:
        row = await dd.leases.find_one({"_id": payouts.LEASE_ID})
        return "held" if row["at"] else "free"

    task = asyncio.create_task(loop_forever())
    await asyncio.sleep(0)
    await workers.stop([task])

    assert seen == ["held"]  # the lease was still ours while the loop was being cancelled
    assert (await d.leases.find_one({"_id": payouts.LEASE_ID}))["at"] == 0.0


# ════════════════════ §B — a row inside the week is re-verified, one outside is not ══════════


def _row(age_s: float, **over: Any) -> dict[str, Any]:
    row = {
        "_id": "dep-reverify",
        "account_id": "acct-1",
        "asset": "ETH",
        "mode": "xchain",
        "status": "credited",
        "verified": False,
        "order_id": ORDER,
        "src_tx_hash": TX,
        "created_at": time.time() - age_s,
    }
    row.update(over)
    return row


def _index(monkeypatch, asked: list[str], ids: list[str]) -> None:
    async def order_ids_by_tx(h: str, timeout: float | None = None) -> list[str]:
        asked.append(h)
        return ids

    monkeypatch.setattr(xchain, "order_ids_by_tx", order_ids_by_tx)


async def test_the_window_is_a_week(mock_db):
    """A day was too short for the reason that matters: the re-check runs only while the process
    runs, so the window has to cover a deploy cadence, not the router's indexing delay."""
    assert workers.REVERIFY_WINDOW_S == 7 * 86_400


async def test_a_three_day_old_credited_deposit_is_re_verified(mock_db, monkeypatch):
    d = mock_db[DB]
    await d.deposits.insert_one(_row(3 * 86_400))
    asked: list[str] = []
    _index(monkeypatch, asked, [ORDER])

    await workers.xchain_secondary()

    assert asked == [TX]  # inside the window: the router was asked once more
    after = await d.deposits.find_one({"_id": "dep-reverify"})
    assert after["verified"] is True
    # …and NOTHING else moved: `_reverify` only ever sets the flag true
    assert after["status"] == "credited" and after["order_id"] == ORDER
    assert await d.events.count_documents({}) == 0


async def test_a_row_older_than_the_window_is_still_left_alone(mock_db, monkeypatch):
    """Still bounded. An unbounded re-check would ask the same question about the same dead rows
    every 15 seconds forever."""
    d = mock_db[DB]
    await d.deposits.insert_one(_row(8 * 86_400))
    asked: list[str] = []
    _index(monkeypatch, asked, [ORDER])

    await workers.xchain_secondary()

    assert asked == []
    assert (await d.deposits.find_one({"_id": "dep-reverify"}))["verified"] is False
