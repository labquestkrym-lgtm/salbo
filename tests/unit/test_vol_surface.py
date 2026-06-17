"""Volatility surface: smile, interpolation, quality flags, term structure (R14)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from app.core.enums import AssetClass, OptionStyle, OptionType, PricingModel
from app.core.exceptions import PricingError
from app.models import ContractSpec, Instrument, Quote
from app.volatility import InterpolatedSmile, SmilePoint, build_surface

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_OPT_SPEC = ContractSpec(
    tick_size=Decimal("0.05"),
    tick_value=Decimal("5"),
    lot_size=100,
    multiplier=Decimal("100"),
    currency="USD",
)


# --- InterpolatedSmile direct behaviour ------------------------------------
def _pt(lm: float, iv: float, reliable: bool = True) -> SmilePoint:
    return SmilePoint(
        strike=100.0, log_moneyness=lm, iv_bid=iv, iv_ask=iv, iv_mid=iv, reliable=reliable
    )


def _smile(points: list[SmilePoint]) -> InterpolatedSmile:
    return InterpolatedSmile(expiry=date(2026, 2, 1), forward=100.0, tau=0.1, points=tuple(points))


def test_interpolation_between_points() -> None:
    smile = _smile([_pt(-0.1, 0.25), _pt(0.0, 0.20), _pt(0.1, 0.23)])
    assert smile.atm_iv() == pytest.approx(0.20)
    mid = smile.iv(0.05)  # between 0.20 and 0.23
    assert 0.20 < mid < 0.23


def test_flat_extrapolation_flagged() -> None:
    smile = _smile([_pt(-0.1, 0.25), _pt(0.0, 0.20), _pt(0.1, 0.23)])
    assert smile.iv(-0.5) == pytest.approx(0.25)  # flat to leftmost
    assert smile.iv(0.5) == pytest.approx(0.23)  # flat to rightmost
    assert smile.is_extrapolated(-0.5)
    assert not smile.is_extrapolated(0.0)


def test_never_negative_iv() -> None:
    smile = _smile([_pt(-0.1, 0.0), _pt(0.1, 0.0)])
    assert smile.iv(0.0) > 0  # clamped to a small positive floor


def test_unreliable_smile_with_too_few_points() -> None:
    smile = _smile([_pt(0.0, 0.20, reliable=False)])
    assert not smile.is_reliable
    with pytest.raises(PricingError):
        smile.iv(0.0)


# --- build_surface from quotes ---------------------------------------------
async def test_surface_recovers_flat_iv_from_mock_chain() -> None:
    broker = MockBrokerAdapter(SimulatedClock(_NOW), MockMarketConfig(spot0=100.0, option_iv=0.20))
    await broker.connect()
    options = [i for i in broker._instruments.values() if i.asset_class is AssetClass.OPTION]
    pairs = [(inst, await broker.get_quote(inst.symbol)) for inst in options]

    surface = build_surface(
        spot=100.0, rate=0.05, dividend_yield=0.0, valuation_time=_NOW, quotes=pairs
    )
    expiry = surface.expiries()[0]
    smile = surface.smile(expiry)
    assert smile.is_reliable
    # Flat 20% IV chain -> recovered ATM IV near 0.20 (tick rounding aside).
    assert smile.atm_iv() == pytest.approx(0.20, abs=0.05)
    assert all(0.1 < p.iv_mid < 0.4 for p in smile.reliable_points)
    # Interpolated IV between grid strikes stays in band and non-negative.
    assert 0.1 < surface.iv(expiry, 102.0) < 0.4


async def test_term_structure_single_expiry() -> None:
    broker = MockBrokerAdapter(SimulatedClock(_NOW), MockMarketConfig(spot0=100.0, option_iv=0.20))
    await broker.connect()
    options = [i for i in broker._instruments.values() if i.asset_class is AssetClass.OPTION]
    pairs = [(inst, await broker.get_quote(inst.symbol)) for inst in options]
    surface = build_surface(
        spot=100.0, rate=0.05, dividend_yield=0.0, valuation_time=_NOW, quotes=pairs
    )
    ts = surface.atm_term_structure()
    assert len(ts) == 1
    tau = surface.smile(surface.expiries()[0]).tau
    assert surface.atm_iv_at(tau) == pytest.approx(0.20, abs=0.05)


def test_crossed_quote_marked_unreliable() -> None:
    inst = Instrument(
        symbol="XYZ-C-100",
        underlying_symbol="XYZ",
        asset_class=AssetClass.OPTION,
        spec=_OPT_SPEC,
        expiry=date(2026, 2, 1),
        option_type=OptionType.CALL,
        strike=Decimal("100"),
        option_style=OptionStyle.EUROPEAN,
        pricing_model=PricingModel.BLACK_SCHOLES_MERTON,
    )
    crossed = Quote(
        instrument_symbol=inst.symbol,
        timestamp=_NOW,
        bid=Decimal("6"),
        ask=Decimal("5"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
    )
    surface = build_surface(
        spot=100.0, rate=0.05, dividend_yield=0.0, valuation_time=_NOW, quotes=[(inst, crossed)]
    )
    smile = surface.smile(date(2026, 2, 1))
    assert not smile.is_reliable
    assert all(not p.reliable for p in smile.points)
