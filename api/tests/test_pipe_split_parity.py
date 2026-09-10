"""The bridge grid, both implementations, ONE file of truth.

`ethpipe.split_amount` (Python, the API) and `PipeSplit.split` (Solidity, the hook) compute the
same thing: what the Beam bridge can mint, and what rides on the relayer fee. Two
implementations of one fact will disagree and one of them reaches money — so neither owns the
truth. `contracts/test/vectors/grid.json` does, `contracts/test/PipeSplit.t.sol` runs it on that
side, and this file runs the identical file on this one.

It asserts every row INCLUDING WHICH SIDE REFUSES: a divergence in the refusals is a divergence.
The `fee_bounds` rows are the hook's own floor/ceiling on the quoted relayer fee, which the
quote route has to apply before it hands anyone calldata (`uniswap.relayer_fee_problem`).

The vectors are READ, never copied. They live with the contracts, which the public mirror does
not ship — so when the file is not there this whole module skips, loudly, naming the path it
looked for. A skip is not a pass: in the dev tree, where the file exists, a divergence is red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pgasme import ethpipe, uniswap

VECTORS = Path(__file__).resolve().parents[2] / "contracts" / "test" / "vectors" / "grid.json"

if not VECTORS.exists():  # pragma: no cover — only in a tree without contracts/ (the mirror)
    pytest.skip(
        f"the shared golden vectors are not in this tree ({VECTORS}) — the parity test needs "
        "the contracts, which the public mirror does not ship",
        allow_module_level=True,
    )

GOLDEN = json.loads(VECTORS.read_text())

# Which Solidity revert each Python refusal corresponds to. `split_amount` raises one SplitError
# for all three, so the MESSAGE is what says which rule refused — and that is what has to agree.
REFUSALS = {
    "GridZero": "grid must be",
    "AmountBelowRelayerFee": "does not cover the relayer fee",
    "NothingMintable": "too small to mint",
}


def test_the_vector_file_is_the_one_the_solidity_suite_runs():
    """A file that lost its rows would make every parity assertion below vacuously true."""
    assert GOLDEN["count"] == len(GOLDEN["vectors"]) == 16
    assert GOLDEN["fee_bounds_count"] == len(GOLDEN["fee_bounds"]) == 4
    assert {v["name"] for v in GOLDEN["vectors"]} >= {
        "eth_measured_fork_output",
        "eth_amount_equals_min_fee",
        "eth_dust_below_one_grid_step",
        "wbtc_no_grid",
        "grid_zero_refused",
    }


@pytest.mark.parametrize("vec", GOLDEN["vectors"], ids=[v["name"] for v in GOLDEN["vectors"]])
def test_split_amount_reproduces_every_golden_vector(vec):
    amount, fee, grid = int(vec["amount"]), int(vec["min_relayer_fee"]), int(vec["grid"])
    if vec["revert"]:
        with pytest.raises(ethpipe.SplitError) as e:
            ethpipe.split_amount(amount, fee, grid)
        assert REFUSALS[vec["revert"]] in str(e.value), (
            f"{vec['name']}: Solidity refuses with {vec['revert']}, Python refuses with "
            f"{e.value!r} — the refusals must be the same rule"
        )
        return
    value, relayer_fee = ethpipe.split_amount(amount, fee, grid)
    assert (value, relayer_fee) == (int(vec["value"]), int(vec["relayer_fee"])), vec["name"]
    # the two invariants the vectors exist to protect, restated on every row
    assert value + relayer_fee == amount  # sendFunds requires it exactly
    assert value % grid == 0 and value > 0  # a sub-grid tail is unmintable on Beam, forever
    assert relayer_fee >= fee  # the tail is ADDED to the quoted tariff, never substituted


@pytest.mark.parametrize(
    "vec", GOLDEN["fee_bounds"], ids=[v["name"] for v in GOLDEN["fee_bounds"]]
)
def test_the_quote_applies_the_hooks_own_relayer_fee_bounds(vec):
    problem = uniswap.relayer_fee_problem(
        int(vec["out"]),
        int(vec["quote"]),
        int(vec["min_relayer_fee"]),
        int(vec["max_relayer_fee_bps"]),
    )
    if vec["accept"]:
        assert problem is None, f"{vec['name']}: the hook accepts this fee and we refused it"
        return
    assert problem, f"{vec['name']}: the hook reverts {vec['revert']} and we accepted it"
    wanted = "floor" if vec["revert"] == "RelayerFeeBelowFloor" else "ceiling"
    assert wanted in problem, f"{vec['name']}: refused for the wrong reason ({problem})"
