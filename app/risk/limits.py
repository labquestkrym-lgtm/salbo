"""Hard risk-limit evaluation (R22).

A limit configured as ``0`` is treated as *unset* and is not enforced (mandatory
limits are forced to be non-zero before live trading by the config gate). All
comparisons are done in ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.config.settings import RiskConfig


@dataclass(frozen=True, slots=True)
class LimitBreach:
    name: str
    limit: Decimal
    observed: Decimal

    def __str__(self) -> str:
        return f"{self.name}: observed {self.observed} exceeds limit {self.limit}"


@dataclass(frozen=True, slots=True)
class RiskState:
    """Observable risk inputs at a point in time."""

    daily_pnl: Decimal = Decimal("0")  # negative = loss
    drawdown: Decimal = Decimal("0")  # peak-to-current loss, >= 0
    margin_utilization: Decimal = Decimal("0")  # 0..1
    cash_delta: Decimal = Decimal("0")
    net_delta_units: float = 0.0
    net_gamma_units: float = 0.0
    net_vega_per_pct: Decimal = Decimal("0")
    net_theta_per_day: Decimal = Decimal("0")
    futures_contracts: int = 0
    option_contracts: int = 0
    orders_last_minute: int = 0
    max_quote_age_seconds: Decimal = Decimal("0")
    max_spread_fraction: Decimal = Decimal("0")
    position_mismatch: Decimal = Decimal("0")
    critical_breach_names: frozenset[str] = field(default_factory=frozenset)


# Breaches that should trip the kill switch (vs merely block new positions).
CRITICAL_LIMITS = frozenset(
    {
        "maximum_daily_loss",
        "maximum_drawdown",
        "maximum_margin_utilization",
        "stale_market_data_seconds",
        "maximum_position_mismatch",
    }
)


def _d(x: float) -> Decimal:
    return Decimal(str(x))


def evaluate_limits(config: RiskConfig, state: RiskState) -> list[LimitBreach]:
    """Return all breached limits. Unset (0) limits are skipped."""
    breaches: list[LimitBreach] = []

    def check(name: str, limit: Decimal, observed: Decimal) -> None:
        if limit > 0 and observed > limit:
            breaches.append(LimitBreach(name=name, limit=limit, observed=observed))

    # Loss is a breach when the loss magnitude exceeds the limit.
    if config.maximum_daily_loss > 0 and state.daily_pnl < -config.maximum_daily_loss:
        breaches.append(
            LimitBreach("maximum_daily_loss", config.maximum_daily_loss, -state.daily_pnl)
        )
    check("maximum_drawdown", config.maximum_drawdown, state.drawdown)
    check("maximum_margin_utilization", config.maximum_margin_utilization, state.margin_utilization)
    check("maximum_cash_delta", config.maximum_cash_delta, abs(state.cash_delta))
    check("maximum_net_delta", config.maximum_net_delta, _d(abs(state.net_delta_units)))
    check("maximum_gamma", config.maximum_gamma, _d(abs(state.net_gamma_units)))
    check("maximum_vega", config.maximum_vega, abs(state.net_vega_per_pct))
    check("maximum_theta", config.maximum_theta, abs(state.net_theta_per_day))
    check(
        "maximum_futures_position",
        Decimal(config.maximum_futures_position),
        Decimal(abs(state.futures_contracts)),
    )
    check(
        "maximum_option_contracts",
        Decimal(config.maximum_option_contracts),
        Decimal(abs(state.option_contracts)),
    )
    check(
        "maximum_orders_per_minute",
        Decimal(config.maximum_orders_per_minute),
        Decimal(state.orders_last_minute),
    )
    check("maximum_spread", config.maximum_spread, state.max_spread_fraction)
    check(
        "stale_market_data_seconds",
        config.stale_market_data_seconds,
        state.max_quote_age_seconds,
    )
    check("maximum_position_mismatch", config.maximum_position_mismatch, state.position_mismatch)
    return breaches
