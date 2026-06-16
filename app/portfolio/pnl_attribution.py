"""P&L attribution (R29).

Decomposes the change in portfolio value over an interval into Greek-driven
terms (delta / gamma / theta / vega) computed from the *beginning-of-interval*
Greeks, plus execution-derived terms (hedge, basis, commissions, spread,
slippage) supplied by the OMS/execution layer. Whatever is not explained is the
``residual`` — a small residual is a sanity check on the model.

Sign convention:
* ``hedge_pnl``, ``basis_pnl`` may be positive or negative (mark-to-market).
* ``commissions``, ``spread_cost``, ``slippage`` are costs (>= 0) and reduce P&L.
* ``total`` is the actual realized+unrealized P&L for the interval.
* ``residual = total - (explained terms)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.portfolio.greeks_engine import PortfolioGreeks


def _money(x: float) -> Decimal:
    return Decimal(str(x))


@dataclass(frozen=True, slots=True)
class PnLAttribution:
    delta_pnl: Decimal
    gamma_pnl: Decimal
    theta_pnl: Decimal
    vega_pnl: Decimal
    hedge_pnl: Decimal
    basis_pnl: Decimal
    commissions: Decimal
    spread_cost: Decimal
    slippage: Decimal
    residual: Decimal
    total: Decimal

    @property
    def explained(self) -> Decimal:
        return (
            self.delta_pnl
            + self.gamma_pnl
            + self.theta_pnl
            + self.vega_pnl
            + self.hedge_pnl
            + self.basis_pnl
            - self.commissions
            - self.spread_cost
            - self.slippage
        )


def attribute_pnl(
    *,
    begin_greeks: PortfolioGreeks,
    spot_begin: Decimal,
    spot_end: Decimal,
    iv_begin_pct: Decimal,
    iv_end_pct: Decimal,
    days_elapsed: Decimal,
    total_pnl: Decimal,
    hedge_pnl: Decimal = Decimal("0"),
    basis_pnl: Decimal = Decimal("0"),
    commissions: Decimal = Decimal("0"),
    spread_cost: Decimal = Decimal("0"),
    slippage: Decimal = Decimal("0"),
) -> PnLAttribution:
    """Attribute ``total_pnl`` across Greek and execution components.

    ``iv_*_pct`` are implied vols in percentage points (e.g. 20 for 20%), to
    match ``net_vega_per_pct``.
    """
    d_spot = spot_end - spot_begin
    delta_pnl = _money(begin_greeks.net_delta_units) * d_spot
    gamma_pnl = Decimal("0.5") * _money(begin_greeks.net_gamma_units) * d_spot * d_spot
    theta_pnl = begin_greeks.net_theta_per_day * days_elapsed
    vega_pnl = begin_greeks.net_vega_per_pct * (iv_end_pct - iv_begin_pct)

    attribution = PnLAttribution(
        delta_pnl=delta_pnl,
        gamma_pnl=gamma_pnl,
        theta_pnl=theta_pnl,
        vega_pnl=vega_pnl,
        hedge_pnl=hedge_pnl,
        basis_pnl=basis_pnl,
        commissions=commissions,
        spread_cost=spread_cost,
        slippage=slippage,
        residual=Decimal("0"),
        total=total_pnl,
    )
    residual = total_pnl - attribution.explained
    return PnLAttribution(
        delta_pnl=delta_pnl,
        gamma_pnl=gamma_pnl,
        theta_pnl=theta_pnl,
        vega_pnl=vega_pnl,
        hedge_pnl=hedge_pnl,
        basis_pnl=basis_pnl,
        commissions=commissions,
        spread_cost=spread_cost,
        slippage=slippage,
        residual=residual,
        total=total_pnl,
    )
