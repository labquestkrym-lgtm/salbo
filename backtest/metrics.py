"""Backtest performance metrics (R24).

Pure functions over an equity curve / return series / trade P&Ls. Defensive
against degenerate inputs (empty, constant, zero-volatility) so a report never
crashes — it returns 0.0 where a metric is undefined.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def returns_from_equity(equity: Sequence[float]) -> np.ndarray:
    e = np.asarray(equity, dtype=float)
    if e.size < 2:
        return np.array([], dtype=float)
    prev = e[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(prev != 0, np.diff(e) / prev, 0.0)
    return r


def total_return(equity: Sequence[float]) -> float:
    e = np.asarray(equity, dtype=float)
    if e.size < 2 or e[0] == 0:
        return 0.0
    return float(e[-1] / e[0] - 1.0)


def annualized_return(equity: Sequence[float], *, periods_per_year: float) -> float:
    e = np.asarray(equity, dtype=float)
    if e.size < 2 or e[0] <= 0 or e[-1] <= 0:
        return 0.0
    periods = e.size - 1
    growth = e[-1] / e[0]
    return float(growth ** (periods_per_year / periods) - 1.0)


def sharpe_ratio(
    returns: Sequence[float], *, periods_per_year: float, risk_free: float = 0.0
) -> float:
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    excess = r - risk_free / periods_per_year
    sd = float(np.std(excess, ddof=1))
    if sd == 0:
        return 0.0
    return float(np.mean(excess) / sd * np.sqrt(periods_per_year))


def sortino_ratio(
    returns: Sequence[float], *, periods_per_year: float, risk_free: float = 0.0
) -> float:
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return 0.0
    excess = r - risk_free / periods_per_year
    downside = excess[excess < 0]
    dd = float(np.sqrt(np.mean(downside**2))) if downside.size else 0.0
    if dd == 0:
        return 0.0
    return float(np.mean(excess) / dd * np.sqrt(periods_per_year))


def max_drawdown(equity: Sequence[float]) -> float:
    """Maximum peak-to-trough drawdown as a positive fraction (0..1)."""
    e = np.asarray(equity, dtype=float)
    if e.size == 0:
        return 0.0
    running_max = np.maximum.accumulate(e)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(running_max > 0, (running_max - e) / running_max, 0.0)
    return float(np.max(drawdowns))


def calmar_ratio(annual_return: float, mdd: float) -> float:
    if mdd == 0:
        return 0.0
    return annual_return / mdd


def win_rate(trade_pnls: Sequence[float]) -> float:
    p = np.asarray(trade_pnls, dtype=float)
    if p.size == 0:
        return 0.0
    return float(np.mean(p > 0))


def profit_factor(trade_pnls: Sequence[float]) -> float:
    p = np.asarray(trade_pnls, dtype=float)
    gains = float(np.sum(p[p > 0]))
    losses = float(-np.sum(p[p < 0]))
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses
