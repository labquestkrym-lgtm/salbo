"""End-to-end: find ATM, open a straddle, compute Greeks, hedge the delta.

Covers acceptance criteria 5-11 (ATM discovery, straddle open, Greeks, hedge
sizing, hedge execution with commission, delta reduction).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from app.core.enums import OrderType, Side
from app.execution import HedgeBandConfig, HedgeEngine, InMemoryOrderStore, OrderManager
from app.execution.straddle import StraddleExecutor, StraddleOpenStatus
from app.instruments import InstrumentResolver
from app.models import OrderRequest
from app.portfolio import PortfolioGreeksEngine, PricingInputs, UnderlyingState
from app.strategies.atm import StrikeCandidate
from app.strategies.long_straddle import (
    DeltaHedgedLongStraddleStrategy,
    StraddleEntryParams,
    StraddleExitParams,
)
from app.strategies.vol_forecast import ForecastConfig, VolatilityForecast

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


async def test_open_straddle_and_hedge_reduces_delta() -> None:
    clock = SimulatedClock(_NOW)
    broker = MockBrokerAdapter(clock, MockMarketConfig(spot0=100.0, option_iv=0.20))
    await broker.connect()
    resolver = InstrumentResolver(list(broker._instruments.values()))

    expiry = resolver.expiries("XYZ")[0]
    straddle = resolver.resolve_straddle("XYZ", expiry=expiry, strike=Decimal("100"))

    call_q = await broker.get_quote(straddle.call.symbol)
    put_q = await broker.get_quote(straddle.put.symbol)
    candidate = StrikeCandidate(strike=Decimal("100"), call_quote=call_q, put_quote=put_q)

    strat = DeltaHedgedLongStraddleStrategy(
        entry_params=StraddleEntryParams(contracts=5),
        exit_params=StraddleExitParams(min_days_to_expiry=7),
        forecast_config=ForecastConfig(),
    )
    # Forecast: realized 40% vs implied 20% -> buy-vol edge.
    entry = strat.evaluate_entry(
        spot=Decimal("100"),
        candidates=[candidate],
        forecast=VolatilityForecast(expected_rv=0.40),
        implied_vol=0.20,
    )
    assert entry.enter and entry.selection is not None

    # --- open the straddle (both legs) ---
    oms = OrderManager(broker, InMemoryOrderStore(), clock)
    executor = StraddleExecutor(oms)
    call_req, put_req = strat.build_leg_requests(
        entry.selection, straddle.call, straddle.put, id_prefix="s1"
    )
    result = await executor.open(call_req, put_req)
    assert result.status is StraddleOpenStatus.OPENED

    # --- compute portfolio Greeks ---
    greeks_engine = PortfolioGreeksEngine(broker._instruments)
    sigma = {straddle.call.symbol: 0.20, straddle.put.symbol: 0.20}
    inputs = PricingInputs(
        valuation_time=_NOW,
        underlying=UnderlyingState(spot=100.0, rate=0.05),
        sigma_by_symbol=sigma,
        mid_by_symbol={},
        future_multiplier=float(straddle.future.spec.multiplier),
    )
    positions = await broker.get_positions()
    g_before = greeks_engine.compute(positions, inputs)
    assert g_before.net_gamma_units > 0  # long straddle is long gamma

    # --- hedge the delta via the futures leg ---
    hedger = HedgeEngine(
        HedgeBandConfig(base_trigger_units=5.0, hedge_to_zero=True, min_futures_trade=1)
    )
    decision = hedger.decide(
        current_delta_units=g_before.net_delta_units,
        future_multiplier=float(straddle.future.spec.multiplier),
        now=_NOW,
        cost_per_contract=Decimal("1"),
    )
    assert decision.should_hedge

    side = Side.BUY if decision.contracts > 0 else Side.SELL
    await oms.submit(
        OrderRequest(
            client_order_id="hedge1",
            instrument_symbol=straddle.future.symbol,
            side=side,
            quantity=Decimal(abs(decision.contracts)),
            order_type=OrderType.MARKET,
        )
    )

    # --- delta should be smaller after hedging ---
    positions_after = await broker.get_positions()
    g_after = greeks_engine.compute(positions_after, inputs)
    assert abs(g_after.net_delta_units) < abs(g_before.net_delta_units)

    # Commission was charged on every fill (no free trades).
    fills = await broker.get_fills()
    assert len(fills) == 3  # call, put, hedge
    assert all(f.commission > 0 for f in fills)
