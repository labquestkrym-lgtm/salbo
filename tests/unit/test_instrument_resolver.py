"""Instrument Resolver (R13): mapping and validation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from app.core.enums import AssetClass, OptionStyle, OptionType, PricingModel
from app.core.exceptions import InstrumentResolutionError
from app.instruments import InstrumentResolver
from app.models import ContractSpec, Instrument


def _resolver() -> tuple[InstrumentResolver, MockBrokerAdapter]:
    clock = SimulatedClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    broker = MockBrokerAdapter(clock, MockMarketConfig())
    instruments = list(broker._instruments.values())
    return InstrumentResolver(instruments), broker


def test_resolve_straddle() -> None:
    resolver, _ = _resolver()
    expiry = resolver.expiries("XYZ")[0]
    strike = Decimal("100")
    straddle = resolver.resolve_straddle("XYZ", expiry=expiry, strike=strike)
    assert straddle.call.option_type is OptionType.CALL
    assert straddle.put.option_type is OptionType.PUT
    assert straddle.future.asset_class is AssetClass.FUTURE
    assert straddle.underlying.asset_class is AssetClass.EQUITY


def test_resolve_straddle_on_future() -> None:
    # FORTS-style: options written on the future, no equity underlying. The
    # future itself is the spot/forward reference (underlying == future).
    clock = SimulatedClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    broker = MockBrokerAdapter(clock, MockMarketConfig(options_on_futures=True))
    resolver = InstrumentResolver(list(broker._instruments.values()))
    expiry = resolver.expiries("XYZ")[0]
    straddle = resolver.resolve_straddle_on_future("XYZ", expiry=expiry, strike=Decimal("100"))
    assert straddle.underlying.asset_class is AssetClass.FUTURE
    assert straddle.underlying is straddle.future
    assert straddle.call.pricing_model is PricingModel.BLACK_76
    assert straddle.put.pricing_model is PricingModel.BLACK_76


def test_resolve_straddle_on_future_missing_strike_raises() -> None:
    clock = SimulatedClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    broker = MockBrokerAdapter(clock, MockMarketConfig(options_on_futures=True))
    resolver = InstrumentResolver(list(broker._instruments.values()))
    expiry = resolver.expiries("XYZ")[0]
    with pytest.raises(InstrumentResolutionError):
        resolver.resolve_straddle_on_future("XYZ", expiry=expiry, strike=Decimal("123"))


def test_option_and_future_multipliers_may_differ() -> None:
    resolver, _ = _resolver()
    expiry = resolver.expiries("XYZ")[0]
    straddle = resolver.resolve_straddle("XYZ", expiry=expiry, strike=Decimal("100"))
    # Resolver must NOT require equal multipliers (option=100, future=50).
    assert straddle.call.spec.multiplier == Decimal("100")
    assert straddle.future.spec.multiplier == Decimal("50")
    assert straddle.call.spec.multiplier != straddle.future.spec.multiplier


def test_missing_strike_raises() -> None:
    resolver, _ = _resolver()
    expiry = resolver.expiries("XYZ")[0]
    with pytest.raises(InstrumentResolutionError):
        resolver.resolve_straddle("XYZ", expiry=expiry, strike=Decimal("123"))


def test_nearest_future_must_cover_expiry() -> None:
    resolver, _ = _resolver()
    far = date(2099, 1, 1)
    with pytest.raises(InstrumentResolutionError):
        resolver.nearest_future("XYZ", on_or_after=far)


def _future(symbol: str, underlying: str, currency: str = "RUB") -> Instrument:
    spec = ContractSpec(
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        lot_size=1,
        multiplier=Decimal("1"),
        currency=currency,
    )
    return Instrument(
        symbol=symbol,
        underlying_symbol=underlying,
        asset_class=AssetClass.FUTURE,
        spec=spec,
        expiry=date(2026, 9, 18),
    )


def test_resolve_pair_picks_front_futures() -> None:
    near = _future("NLMK-FUT-0926", "NLMK")
    far = Instrument(
        symbol="NLMK-FUT-1226",
        underlying_symbol="NLMK",
        asset_class=AssetClass.FUTURE,
        spec=near.spec,
        expiry=date(2026, 12, 18),
    )
    chmf = _future("CHMF-FUT-0926", "CHMF")
    resolver = InstrumentResolver([far, near, chmf])  # unsorted on purpose
    pair = resolver.resolve_pair("NLMK", "CHMF", beta=1.02)
    assert pair.leg_a.symbol == "NLMK-FUT-0926"  # front month, not the far one
    assert pair.leg_b.symbol == "CHMF-FUT-0926"
    assert pair.beta == 1.02


def test_resolve_pair_on_or_after_rolls_past_near_expiry() -> None:
    # With on_or_after set (the roll cutoff), the near-expiry front is skipped for
    # the next contract — this is how the orchestrator rolls before expiry.
    near_a = _future("NLMK-0926", "NLMK")  # expiry 2026-09-18
    far_a = Instrument(
        symbol="NLMK-1226", underlying_symbol="NLMK", asset_class=AssetClass.FUTURE,
        spec=near_a.spec, expiry=date(2026, 12, 18),
    )
    near_b = _future("CHMF-0926", "CHMF")
    far_b = Instrument(
        symbol="CHMF-1226", underlying_symbol="CHMF", asset_class=AssetClass.FUTURE,
        spec=near_b.spec, expiry=date(2026, 12, 18),
    )
    resolver = InstrumentResolver([near_a, far_a, near_b, far_b])
    # cutoff after the September expiry -> must pick the December contracts.
    pair = resolver.resolve_pair("NLMK", "CHMF", on_or_after=date(2026, 9, 19))
    assert pair.leg_a.symbol == "NLMK-1226"
    assert pair.leg_b.symbol == "CHMF-1226"


def test_resolve_pair_currency_mismatch_rejected() -> None:
    a = _future("A-FUT", "A", currency="RUB")
    b = _future("B-FUT", "B", currency="USD")
    resolver = InstrumentResolver([a, b])
    with pytest.raises(InstrumentResolutionError, match="currency"):
        resolver.resolve_pair("A", "B")


def test_front_future_missing_raises() -> None:
    resolver = InstrumentResolver([_future("A-FUT", "A")])
    with pytest.raises(InstrumentResolutionError):
        resolver.front_future("ZZZ")


def test_currency_mismatch_rejected() -> None:
    spec_usd = ContractSpec(
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        lot_size=100,
        multiplier=Decimal("100"),
        currency="USD",
    )
    spec_eur = ContractSpec(
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        lot_size=1,
        multiplier=Decimal("50"),
        currency="EUR",
    )
    option = Instrument(
        symbol="O",
        underlying_symbol="XYZ",
        asset_class=AssetClass.OPTION,
        spec=spec_usd,
        expiry=date(2026, 2, 1),
        option_type=OptionType.CALL,
        strike=Decimal("100"),
        option_style=OptionStyle.EUROPEAN,
        pricing_model=PricingModel.BLACK_SCHOLES_MERTON,
    )
    future = Instrument(
        symbol="F",
        underlying_symbol="XYZ",
        asset_class=AssetClass.FUTURE,
        spec=spec_eur,
        expiry=date(2026, 3, 1),
    )
    resolver = InstrumentResolver([option, future])
    with pytest.raises(InstrumentResolutionError, match="currency"):
        resolver.validate_hedge_pair(option, future)
