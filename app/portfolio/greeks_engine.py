"""Portfolio Greeks & risk aggregation (R15).

Aggregates per-position option/future/equity Greeks into portfolio-level
quantities. Dimensions are explicit:

* ``net_delta_units``  — underlying-unit exposure: Σ delta · multiplier · qty.
* ``cash_delta``       — currency exposure that moves 1:1 with spot
                         (= net_delta_units · spot).
* ``futures_equivalent_delta`` — net delta expressed in **futures contracts**
                         (= net_delta_units / future_multiplier · future_delta).
* ``net_gamma_units``  — Σ gamma · multiplier · qty.
* ``cash_gamma``       — "$ gamma per 1% move" = net_gamma_units · spot² · 0.01
                         (≈ change in cash delta for a 1% spot move).
* ``net_theta_per_day``, ``net_vega_per_pct`` — currency / day and per IV point.

Option and futures multipliers are read per-instrument and never assumed equal
(R7). Money quantities are ``Decimal``; the conversion from the float pricing
kernel happens here at the boundary (ADR-0001).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal

from app.core.enums import AssetClass
from app.core.exceptions import PricingError
from app.models import Instrument, Position
from app.pricing import bsm

_YEAR_SECONDS = 365.0 * 24 * 3600


def _money(x: float) -> Decimal:
    return Decimal(str(x))


def _tau_years(expiry_date: datetime | None, now: datetime) -> float:
    if expiry_date is None:
        raise PricingError("derivative position requires an expiry")
    seconds = (expiry_date - now).total_seconds()
    return max(seconds / _YEAR_SECONDS, 1.0 / _YEAR_SECONDS)


@dataclass(frozen=True, slots=True)
class UnderlyingState:
    spot: float
    rate: float = 0.0
    dividend_yield: float = 0.0


@dataclass(frozen=True, slots=True)
class PricingInputs:
    valuation_time: datetime
    underlying: UnderlyingState
    sigma_by_symbol: Mapping[str, float]  # IV per option symbol (absolute)
    mid_by_symbol: Mapping[str, Decimal]  # mid price per symbol (for exposure)
    future_multiplier: float  # multiplier of the hedging future


@dataclass(frozen=True, slots=True)
class PortfolioGreeks:
    net_delta_units: float
    cash_delta: Decimal
    futures_equivalent_delta: float
    net_gamma_units: float
    cash_gamma: Decimal
    net_theta_per_day: Decimal
    net_vega_per_pct: Decimal
    net_vanna: float
    net_volga: float
    gross_exposure: Decimal


class PortfolioGreeksEngine:
    """Stateless aggregator. Inject instrument lookup; pass market inputs per call."""

    def __init__(self, instruments_by_symbol: Mapping[str, Instrument]) -> None:
        self._instruments = instruments_by_symbol

    def compute(self, positions: list[Position], inputs: PricingInputs) -> PortfolioGreeks:
        u = inputs.underlying
        now = _to_utc(inputs.valuation_time)

        delta_units = 0.0
        gamma_units = 0.0
        theta_day = 0.0
        vega_pct = 0.0
        vanna = 0.0
        volga = 0.0
        gross = Decimal("0")

        for pos in positions:
            if pos.is_flat:
                continue
            inst = self._instruments.get(pos.instrument_symbol)
            if inst is None:
                raise PricingError(f"unknown instrument in portfolio: {pos.instrument_symbol}")
            qty = float(pos.quantity)
            mult = float(inst.spec.multiplier)

            if inst.asset_class is AssetClass.OPTION:
                sigma = inputs.sigma_by_symbol.get(pos.instrument_symbol)
                if sigma is None:
                    raise PricingError(f"missing IV for option {pos.instrument_symbol}")
                assert inst.strike is not None and inst.option_type is not None
                tau = _tau_years(_expiry_dt(inst), now)
                g = bsm(
                    spot=u.spot,
                    strike=float(inst.strike),
                    t=tau,
                    rate=u.rate,
                    sigma=sigma,
                    option_type=inst.option_type,
                    dividend_yield=u.dividend_yield,
                )
                delta_units += g.delta * mult * qty
                gamma_units += g.gamma * mult * qty
                theta_day += g.theta_per_day * mult * qty
                vega_pct += g.vega_per_pct * mult * qty
                vanna += g.vanna * mult * qty
                volga += g.volga * mult * qty
            elif inst.asset_class is AssetClass.FUTURE:
                # dFuture/dSpot = e^{(r-q)·tau}; ~1 but computed for correctness.
                tau = _tau_years(_expiry_dt(inst), now)
                d_fut = math.exp((u.rate - u.dividend_yield) * tau)
                delta_units += d_fut * mult * qty
            else:  # equity / index: delta 1 per unit
                delta_units += mult * qty

            mid = inputs.mid_by_symbol.get(pos.instrument_symbol)
            if mid is not None:
                gross += abs(pos.quantity) * mid * inst.spec.multiplier

        spot = u.spot
        cash_delta = _money(delta_units * spot)
        cash_gamma = _money(gamma_units * spot * spot * 0.01)
        fut_equiv = delta_units / inputs.future_multiplier if inputs.future_multiplier else 0.0

        return PortfolioGreeks(
            net_delta_units=delta_units,
            cash_delta=cash_delta,
            futures_equivalent_delta=fut_equiv,
            net_gamma_units=gamma_units,
            cash_gamma=cash_gamma,
            net_theta_per_day=_money(theta_day),
            net_vega_per_pct=_money(vega_pct),
            net_vanna=vanna,
            net_volga=volga,
            gross_exposure=gross,
        )


def _expiry_dt(inst: Instrument) -> datetime:
    assert inst.expiry is not None
    return datetime.combine(inst.expiry, time(23, 59, 59), tzinfo=UTC)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise PricingError("valuation_time must be timezone-aware")
    return dt.astimezone(UTC)
