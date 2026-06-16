"""Backtest report aggregation (R24)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BacktestReport:
    steps: int
    starting_equity: Decimal
    ending_equity: Decimal
    total_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    commissions: Decimal
    slippage: Decimal
    hedge_count: int
    avg_abs_delta_deviation: float
    max_abs_delta_deviation: float
    opened_position: bool
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, str]:
        """Human-readable one-line-per-metric summary (for logs/CLI)."""
        return {
            "steps": str(self.steps),
            "total_return": f"{self.total_return:.4%}",
            "annualized_return": f"{self.annualized_return:.4%}",
            "sharpe": f"{self.sharpe:.3f}",
            "sortino": f"{self.sortino:.3f}",
            "max_drawdown": f"{self.max_drawdown:.4%}",
            "calmar": f"{self.calmar:.3f}",
            "commissions": str(self.commissions),
            "slippage": str(self.slippage),
            "hedge_count": str(self.hedge_count),
            "avg_abs_delta_dev": f"{self.avg_abs_delta_deviation:.2f}",
            "max_abs_delta_dev": f"{self.max_abs_delta_deviation:.2f}",
        }
