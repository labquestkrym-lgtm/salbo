"""Event-driven backtesting (R24).

The backtester drives the *same* strategy/hedge/OMS code as live (ADR-0002)
through a simulated market, accounting for bid/ask, commissions, slippage,
partial fills, expiries and gaps. Synthetic results are NOT evidence of live
profitability.
"""

from backtest.engine import BacktestConfig, BacktestEngine
from backtest.metrics import (
    annualized_return,
    calmar_ratio,
    max_drawdown,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    total_return,
    win_rate,
)
from backtest.report import BacktestReport
from backtest.walk_forward import WalkForwardWindow, walk_forward_windows

__all__ = [
    "BacktestConfig",
    "BacktestEngine",
    "BacktestReport",
    "WalkForwardWindow",
    "annualized_return",
    "calmar_ratio",
    "max_drawdown",
    "profit_factor",
    "sharpe_ratio",
    "sortino_ratio",
    "total_return",
    "walk_forward_windows",
    "win_rate",
]
