"""Walk-forward window generation (R24)."""

from __future__ import annotations

import pytest

from backtest import walk_forward_windows


def test_non_overlapping_oos_windows() -> None:
    windows = walk_forward_windows(100, train=50, validation=20, out_of_sample=10)
    assert len(windows) == 3  # blocks at start 0, 10, 20
    first = windows[0]
    assert first.train == range(0, 50)
    assert first.validation == range(50, 70)
    assert first.out_of_sample == range(70, 80)
    # Next block advances by out_of_sample (default step).
    assert windows[1].train == range(10, 60)


def test_custom_step() -> None:
    windows = walk_forward_windows(80, train=40, validation=10, out_of_sample=10, step=30)
    assert len(windows) == 1
    assert windows[0].out_of_sample == range(50, 60)


def test_too_short_yields_nothing() -> None:
    assert walk_forward_windows(30, train=20, validation=10, out_of_sample=10) == []


def test_invalid_sizes_raise() -> None:
    with pytest.raises(ValueError):
        walk_forward_windows(100, train=0, validation=10, out_of_sample=10)
