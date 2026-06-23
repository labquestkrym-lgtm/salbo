"""Pairs mean-reversion decision logic."""

from __future__ import annotations

import math

import pytest

from app.strategies.pairs import (
    PairAction,
    PairsParams,
    PairsSpreadStrategy,
    spread_value,
)


def test_spread_value_is_log_ratio() -> None:
    assert spread_value(100.0, 50.0, beta=1.0) == pytest.approx(math.log(2.0))
    assert spread_value(100.0, 100.0, beta=1.0) == pytest.approx(0.0)


def test_params_reject_inverted_bands() -> None:
    with pytest.raises(ValueError):
        PairsParams(entry_z=0.5, exit_z=2.0)
    with pytest.raises(ValueError):
        PairsParams(window=1)


def test_zscore_excludes_current_point() -> None:
    strat = PairsSpreadStrategy(PairsParams(window=4, entry_z=2.0, exit_z=0.5))
    # prior window [0,0,0,0] has zero variance -> None despite a big last value.
    assert strat.zscore([0.0, 0.0, 0.0, 0.0, 5.0]) is None
    # prior window [1,2,3,4] (mean 2.5, pstd ~1.118); last 2.5 -> z 0.
    z = strat.zscore([1.0, 2.0, 3.0, 4.0, 2.5])
    assert z == pytest.approx(0.0, abs=1e-9)


def test_warming_up_holds() -> None:
    strat = PairsSpreadStrategy(PairsParams(window=60))
    sig = strat.decide([0.1, 0.2, 0.3], opened=False)
    assert sig.action is PairAction.HOLD


def _series_with_last_z(window: int, target_z: float) -> list[float]:
    # window of mean 0, pstd 1 (use +/-1 alternating), then a last point = target_z.
    base = [1.0 if i % 2 == 0 else -1.0 for i in range(window)]
    return [*base, target_z]


def test_high_z_opens_short_spread() -> None:
    strat = PairsSpreadStrategy(PairsParams(window=10, entry_z=2.0, exit_z=0.5))
    sig = strat.decide(_series_with_last_z(10, 2.5), opened=False)
    assert sig.action is PairAction.OPEN_SHORT_SPREAD  # A rich -> short A / long B
    assert sig.z == pytest.approx(2.5, abs=1e-9)


def test_low_z_opens_long_spread() -> None:
    strat = PairsSpreadStrategy(PairsParams(window=10, entry_z=2.0, exit_z=0.5))
    sig = strat.decide(_series_with_last_z(10, -2.5), opened=False)
    assert sig.action is PairAction.OPEN_LONG_SPREAD


def test_inside_entry_band_holds_when_flat() -> None:
    strat = PairsSpreadStrategy(PairsParams(window=10, entry_z=2.0, exit_z=0.5))
    sig = strat.decide(_series_with_last_z(10, 1.0), opened=False)
    assert sig.action is PairAction.HOLD


def test_reversion_closes_open_position() -> None:
    strat = PairsSpreadStrategy(PairsParams(window=10, entry_z=2.0, exit_z=0.5))
    sig = strat.decide(_series_with_last_z(10, 0.3), opened=True)
    assert sig.action is PairAction.CLOSE


def test_still_extended_holds_open_position() -> None:
    strat = PairsSpreadStrategy(PairsParams(window=10, entry_z=2.0, exit_z=0.5))
    sig = strat.decide(_series_with_last_z(10, 1.5), opened=True)
    assert sig.action is PairAction.HOLD
