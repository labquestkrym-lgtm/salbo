"""Backtest metric functions (R24)."""

from __future__ import annotations

import math

from backtest import metrics


def test_total_return() -> None:
    assert metrics.total_return([100, 110, 121]) == 121 / 100 - 1
    assert metrics.total_return([100]) == 0.0
    assert metrics.total_return([]) == 0.0


def test_max_drawdown() -> None:
    assert metrics.max_drawdown([100, 120, 90, 130]) == (120 - 90) / 120
    assert metrics.max_drawdown([100, 101, 102]) == 0.0
    assert metrics.max_drawdown([]) == 0.0


def test_profit_factor() -> None:
    assert metrics.profit_factor([10, -5, 20, -5]) == 3.0
    assert metrics.profit_factor([1, 2, 3]) == math.inf  # no losses
    assert metrics.profit_factor([]) == 0.0


def test_win_rate() -> None:
    assert metrics.win_rate([1, -1, 1, 1]) == 0.75
    assert metrics.win_rate([]) == 0.0


def test_sharpe_zero_when_no_variance() -> None:
    assert metrics.sharpe_ratio([0.01, 0.01, 0.01], periods_per_year=252) == 0.0


def test_sharpe_positive_for_steady_gains() -> None:
    r = [0.01, 0.012, 0.009, 0.011]
    assert metrics.sharpe_ratio(r, periods_per_year=252) > 0


def test_sortino_ignores_upside_volatility() -> None:
    r = [0.02, 0.03, -0.01, 0.04]
    assert metrics.sortino_ratio(r, periods_per_year=252) > 0


def test_calmar() -> None:
    assert metrics.calmar_ratio(0.20, 0.10) == 2.0
    assert metrics.calmar_ratio(0.20, 0.0) == 0.0


def test_annualized_return_constant_equity_is_zero() -> None:
    assert metrics.annualized_return([100, 100, 100], periods_per_year=252) == 0.0
