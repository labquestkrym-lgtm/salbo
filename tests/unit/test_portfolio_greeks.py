"""Portfolio Greeks aggregation (R15)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from app.core.enums import AssetClass
from app.models import Position
from app.portfolio import PortfolioGreeksEngine, PricingInputs, UnderlyingState

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _engine_and_universe() -> tuple[PortfolioGreeksEngine, MockBrokerAdapter]:
    broker = MockBrokerAdapter(SimulatedClock(_NOW), MockMarketConfig(spot0=100.0))
    return PortfolioGreeksEngine(broker._instruments), broker


def _atm_symbols(broker: MockBrokerAdapter) -> tuple[str, str]:
    exp = broker._cfg.expiry.strftime("%Y%m%d")
    return f"XYZ-C-100-{exp}", f"XYZ-P-100-{exp}"


def test_long_straddle_is_near_delta_neutral_and_long_gamma() -> None:
    engine, broker = _engine_and_universe()
    call_sym, put_sym = _atm_symbols(broker)
    positions = [
        Position(instrument_symbol=call_sym, quantity=Decimal("1")),
        Position(instrument_symbol=put_sym, quantity=Decimal("1")),
    ]
    inputs = PricingInputs(
        valuation_time=_NOW,
        underlying=UnderlyingState(spot=100.0, rate=0.05, dividend_yield=0.0),
        sigma_by_symbol={call_sym: 0.20, put_sym: 0.20},
        mid_by_symbol={},
        future_multiplier=50.0,
    )
    g = engine.compute(positions, inputs)
    # ATM straddle: delta small, gamma and vega clearly positive.
    assert abs(g.net_delta_units) < 20.0  # small relative to 100*2 multiplier units
    assert g.net_gamma_units > 0
    assert g.net_vega_per_pct > 0
    # Long options -> negative theta (time decay).
    assert g.net_theta_per_day < 0


def test_futures_position_contributes_delta_only() -> None:
    engine, broker = _engine_and_universe()
    fut = broker.future_symbol
    positions = [Position(instrument_symbol=fut, quantity=Decimal("-2"))]
    inputs = PricingInputs(
        valuation_time=_NOW,
        underlying=UnderlyingState(spot=100.0, rate=0.05),
        sigma_by_symbol={},
        mid_by_symbol={},
        future_multiplier=50.0,
    )
    g = engine.compute(positions, inputs)
    # short 2 futures, mult 50 -> ~ -100 delta units, no gamma/vega/theta.
    assert g.net_delta_units < -90
    assert g.net_gamma_units == 0
    assert g.net_vega_per_pct == Decimal("0")
    assert g.futures_equivalent_delta < 0


def test_option_on_future_priced_with_black76_not_bsm() -> None:
    # A BLACK_76 option's portfolio delta must equal the Black-76 delta (scaled),
    # not the BSM delta — the engine selects the model by pricing_model.
    from datetime import date

    from app.core.enums import OptionStyle, OptionType, PricingModel
    from app.models import ContractSpec, Instrument
    from app.pricing import black76, bsm

    expiry = date(2026, 2, 4)
    spec = ContractSpec(
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        lot_size=1,
        multiplier=Decimal("10"),
        currency="RUB",
    )
    opt = Instrument(
        symbol="FUT-C-100",
        underlying_symbol="FUT",
        asset_class=AssetClass.OPTION,
        spec=spec,
        expiry=expiry,
        option_type=OptionType.CALL,
        strike=Decimal("100"),
        option_style=OptionStyle.EUROPEAN,
        pricing_model=PricingModel.BLACK_76,
    )
    engine = PortfolioGreeksEngine({opt.symbol: opt})
    valuation = datetime(2026, 1, 5, tzinfo=UTC)
    inputs = PricingInputs(
        valuation_time=valuation,
        underlying=UnderlyingState(spot=100.0, rate=0.05),  # spot == future price
        sigma_by_symbol={opt.symbol: 0.25},
        mid_by_symbol={},
        future_multiplier=10.0,
    )
    g = engine.compute([Position(instrument_symbol=opt.symbol, quantity=Decimal("1"))], inputs)

    tau = (datetime(2026, 2, 4, 23, 59, 59, tzinfo=UTC) - valuation).total_seconds() / (
        365 * 24 * 3600
    )
    b76 = black76(
        forward=100.0, strike=100.0, t=tau, rate=0.05, sigma=0.25, option_type=OptionType.CALL
    )
    bsm_g = bsm(spot=100.0, strike=100.0, t=tau, rate=0.05, sigma=0.25, option_type=OptionType.CALL)
    assert g.net_delta_units == pytest.approx(b76.delta * 10.0, rel=1e-9)
    assert g.net_delta_units != pytest.approx(bsm_g.delta * 10.0, rel=1e-6)  # truly Black-76


def test_straddle_plus_hedge_reduces_net_delta() -> None:
    engine, broker = _engine_and_universe()
    call_sym, put_sym = _atm_symbols(broker)
    fut = broker.future_symbol
    base = [
        Position(instrument_symbol=call_sym, quantity=Decimal("10")),
        Position(instrument_symbol=put_sym, quantity=Decimal("10")),
    ]
    inputs = PricingInputs(
        valuation_time=_NOW,
        underlying=UnderlyingState(spot=100.0, rate=0.05),
        sigma_by_symbol={call_sym: 0.20, put_sym: 0.20},
        mid_by_symbol={},
        future_multiplier=50.0,
    )
    g_unhedged = engine.compute(base, inputs)
    # Add a futures position opposite to the straddle delta.
    from app.core.enums import RoundingMode
    from app.portfolio import compute_hedge_contracts

    sizing = compute_hedge_contracts(
        current_delta_units=g_unhedged.net_delta_units,
        target_delta_units=0.0,
        future_multiplier=50.0,
        rounding_mode=RoundingMode.NEAREST,
    )
    hedged = [*base, Position(instrument_symbol=fut, quantity=Decimal(sizing.contracts))]
    g_hedged = engine.compute(hedged, inputs)
    assert abs(g_hedged.net_delta_units) < abs(g_unhedged.net_delta_units)
