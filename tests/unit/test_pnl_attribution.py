"""P&L attribution decomposition (R29)."""

from __future__ import annotations

from decimal import Decimal

from app.portfolio import attribute_pnl
from app.portfolio.greeks_engine import PortfolioGreeks


def _greeks(delta: float, gamma: float, theta_day: str, vega_pct: str) -> PortfolioGreeks:
    return PortfolioGreeks(
        net_delta_units=delta,
        cash_delta=Decimal("0"),
        futures_equivalent_delta=0.0,
        net_gamma_units=gamma,
        cash_gamma=Decimal("0"),
        net_theta_per_day=Decimal(theta_day),
        net_vega_per_pct=Decimal(vega_pct),
        net_vanna=0.0,
        net_volga=0.0,
        gross_exposure=Decimal("0"),
    )


def test_delta_gamma_theta_vega_terms() -> None:
    g = _greeks(delta=10.0, gamma=2.0, theta_day="-5", vega_pct="3")
    # spot 100 -> 101 (+1), iv 20 -> 21 (+1 pct), 1 day elapsed.
    attr = attribute_pnl(
        begin_greeks=g,
        spot_begin=Decimal("100"),
        spot_end=Decimal("101"),
        iv_begin_pct=Decimal("20"),
        iv_end_pct=Decimal("21"),
        days_elapsed=Decimal("1"),
        total_pnl=Decimal("0"),
    )
    assert attr.delta_pnl == Decimal("10")  # 10 units * +1
    assert attr.gamma_pnl == Decimal("1")  # 0.5 * 2 * 1^2
    assert attr.theta_pnl == Decimal("-5")  # -5/day * 1 day
    assert attr.vega_pnl == Decimal("3")  # 3 per pct * +1 pct


def test_residual_reconciles_to_total() -> None:
    g = _greeks(delta=10.0, gamma=2.0, theta_day="-5", vega_pct="3")
    attr = attribute_pnl(
        begin_greeks=g,
        spot_begin=Decimal("100"),
        spot_end=Decimal("101"),
        iv_begin_pct=Decimal("20"),
        iv_end_pct=Decimal("21"),
        days_elapsed=Decimal("1"),
        total_pnl=Decimal("20"),
        hedge_pnl=Decimal("4"),
        commissions=Decimal("2"),
        slippage=Decimal("1"),
    )
    # explained = 10 + 1 - 5 + 3 + hedge 4 - commissions 2 - slippage 1 = 10
    assert attr.explained == Decimal("10")
    assert attr.residual == Decimal("10")  # total 20 - explained 10
    # full reconciliation: explained + residual == total
    assert attr.explained + attr.residual == attr.total
