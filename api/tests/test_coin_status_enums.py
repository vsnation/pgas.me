"""⛔ **TWO STATUS ENUMS, ONE CONSTANT — the bug this file exists to keep dead.**

Beam numbers an ordinary output on `Coin::Status` (Available=1) and a shielded one on
`ShieldedCoin::Status` (Available=2). Both spell Maturing 3, which is exactly why reading a
shielded coin on the ordinary scale looked right for three days: the one shape anybody had
eyeballed — a *maturing* `shld` row — agrees under both enums.

The cost of getting it wrong was not an exception. `coin_counts` silently reported zero
shielded coins, so the release gate refused the only bucket that could fund a crossing, and the
product told people their money was locked in a shielded pool. On 2026-09-13 the wallet held
1,652,864 groth of bETH in two AVAILABLE shielded coins and 427,653 in regular ones — the
larger bucket, and a crossing is funded from ONE source.

The rows below are the live `get_utxo` shape read off the wallet that day, not an invention.
"""

from __future__ import annotations

from typing import Any

import pytest

from pgasme import beam, payouts

# the live wallet, 2026-09-13, trimmed to one row per (type, status) that matters
LIVE_ROWS: list[dict[str, Any]] = [
    {"amount": 15_000_000, "asset_id": 0, "type": "norm", "status": 1, "status_string": "available"},
    {"amount": 633_384_000, "asset_id": 0, "type": "chng", "status": 1, "status_string": "available"},
    {"amount": 1_900_000, "asset_id": 0, "type": "chng", "status": 6, "status_string": "spent"},
    {"amount": 199_990, "asset_id": 36, "type": "norm", "status": 1, "status_string": "available"},
    {"amount": 27_673, "asset_id": 36, "type": "chng", "status": 1, "status_string": "available"},
    {"amount": 104_927, "asset_id": 36, "type": "norm", "status": 6, "status_string": "spent"},
    {"amount": 1_000_000, "asset_id": 36, "type": "shld", "status": 2, "status_string": "available"},
    {"amount": 652_864, "asset_id": 36, "type": "shld", "status": 2, "status_string": "available"},
]


def n(counts: dict[int, dict[str, Any]], aid: int, bucket: str) -> int:
    """How many coins of one bucket, for an asset that may not be in the mapping at all.

    ⛔ AN ASSET WITH NOTHING SPENDABLE IS ABSENT, NOT ZERO — `coin_counts` only creates a row
    when a coin passes the availability check, and every caller reads it with a default. Pinned
    here because a test that indexed it directly would pass for the wrong reason the day an
    asset came back."""
    return int((counts.get(aid) or {}).get(bucket, 0))


class Wallet:
    """The one wallet-api READ this project makes (`get_utxo`); it moves nothing."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def utxos(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        return list(self.rows)


@pytest.fixture
def wallet_rows(monkeypatch: pytest.MonkeyPatch):
    """Put the wallet back and drop the per-pass cache: `coin_counts` answers once per pass, and
    a cache left behind is a fact the next test did not measure."""

    def use(rows: list[dict[str, Any]]) -> None:
        beam.set_wallet(Wallet(rows))  # type: ignore[arg-type]
        payouts.reset_process_state()

    yield use
    beam.set_wallet(None)  # the wallet is a per-PROCESS singleton — put it back
    payouts.reset_process_state()


async def test_a_shielded_coin_is_available_at_2_and_a_regular_one_at_1(wallet_rows):
    """THE REGRESSION. Under the old single `UTXO_AVAILABLE = 1` this answered
    `{"shielded": 0}` and 0.01652864 bETH — the larger of the two buckets — was invisible to
    every release."""
    wallet_rows(LIVE_ROWS)
    counts = await payouts.coin_counts()

    beth = counts[36]
    assert beth[payouts.SOURCE_SHIELDED] == 2, "both AVAILABLE shielded coins must be counted"
    assert sorted(beth["amounts_shielded"]) == [652_864, 1_000_000]
    assert beth[payouts.SOURCE_REGULAR] == 2, "`norm` and `chng` are both ordinary outputs"
    assert sorted(beth["amounts_regular"]) == [27_673, 199_990]  # the spent one is not a coin
    assert counts[0][payouts.SOURCE_REGULAR] == 2  # BEAM fee coins, the spent one excluded


async def test_a_shielded_coin_at_status_1_is_not_spendable(wallet_rows):
    """The other direction, and the one that keeps the fix from being 'accept both numbers'.
    Status 1 on the shielded scale is *Incoming* — value arriving, not value we can spend. A
    reader that treated 1 as available for every type would count it and hand the wallet a send
    it cannot fund, which is the overcount `coin_counts` exists to prevent."""
    wallet_rows([
        {"amount": 500_000, "asset_id": 36, "type": "shld", "status": 1, "status_string": "incoming"},
    ])
    counts = await payouts.coin_counts()
    assert n(counts, 36, payouts.SOURCE_SHIELDED) == 0


async def test_maturing_is_3_on_both_scales_and_is_never_counted(wallet_rows):
    """The coincidence that hid the bug: the one row anybody had looked at reads the same either
    way."""
    wallet_rows([
        {"amount": 1_652_864, "asset_id": 36, "type": "shld", "status": 3, "status_string": "maturing"},
        {"amount": 400_000, "asset_id": 36, "type": "norm", "status": 3, "status_string": "maturing"},
    ])
    counts = await payouts.coin_counts()
    assert n(counts, 36, payouts.SOURCE_SHIELDED) == 0
    assert n(counts, 36, payouts.SOURCE_REGULAR) == 0


async def test_the_word_may_veto_the_number_but_never_decides(wallet_rows):
    """`status_string` is the wallet's own spelling and is enum-independent, so it is allowed to
    refuse a coin whose number looks available. It is NOT allowed to admit one: the number is
    what the release is ultimately built on."""
    wallet_rows([
        {"amount": 1_000_000, "asset_id": 36, "type": "shld", "status": 2, "status_string": "outgoing"},
        {"amount": 200_000, "asset_id": 36, "type": "norm", "status": 3, "status_string": "available"},
    ])
    counts = await payouts.coin_counts()
    assert n(counts, 36, payouts.SOURCE_SHIELDED) == 0, "the word refuses it"
    assert n(counts, 36, payouts.SOURCE_REGULAR) == 0, "the word cannot promote a maturing coin"


async def test_a_status_that_is_not_a_number_still_raises(wallet_rows):
    """⛔ An unreadable coin list is not 'no coins' and not 'plenty' (law 8). The release turns
    this raise into a hold that names the parse failure; a silent `False` would have read as a
    quiet, permanent shortage."""
    wallet_rows([{"amount": 1, "asset_id": 36, "type": "shld", "status": "who knows"}])
    with pytest.raises(ValueError):
        await payouts.coin_counts()


async def test_a_row_with_no_word_at_all_is_read_on_its_number(wallet_rows):
    """Older wallet builds answer `get_utxo` without `status_string`. The number alone must
    still decide, on the right scale — the veto is optional, the enum is not."""
    wallet_rows([
        {"amount": 1_000_000, "asset_id": 36, "type": "shld", "status": 2},
        {"amount": 199_990, "asset_id": 36, "type": "norm", "status": 1},
    ])
    counts = await payouts.coin_counts()
    assert counts[36][payouts.SOURCE_SHIELDED] == 1 and counts[36][payouts.SOURCE_REGULAR] == 1
