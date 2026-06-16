"""End-to-end backtest run (R24, acceptance criterion 16).

Drives the mock market through the strategy + hedge + OMS pipeline and checks a
coherent report is produced. This is a controlled simulation, not a
profitability claim.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from backtest import BacktestConfig, BacktestEngine

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


async def test_backtest_opens_hedges_and_reports() -> None:
    clock = SimulatedClock(_NOW)
    broker = MockBrokerAdapter(
        clock,
        MockMarketConfig(
            spot0=100.0,
            annual_vol=0.50,
            option_iv=0.20,
            dt_seconds=3600.0,
            days_to_expiry=30,
            seed=3,
            max_stream_steps=320,
        ),
    )
    engine = BacktestEngine(
        clock, broker, BacktestConfig(steps=300, lookback=30, entry_contracts=5)
    )
    report = await engine.run()

    # Opened a straddle (realized 50% vol >> implied 20% -> buy signal).
    assert report.opened_position is True
    # Re-hedged the delta at least once as spot moved.
    assert report.hedge_count > 0
    # Equity curve recorded for every step; commissions charged.
    assert report.steps == 300
    assert len(report.equity_curve) == 300
    assert report.commissions > 0
    assert report.slippage >= 0
    # Delta deviations tracked.
    assert report.max_abs_delta_deviation >= report.avg_abs_delta_deviation >= 0
    # Metrics are finite real numbers (never NaN/inf in a normal run).
    for value in (report.total_return, report.sharpe, report.sortino, report.max_drawdown):
        assert math.isfinite(value)
    assert set(report.summary()) >= {"sharpe", "max_drawdown", "hedge_count"}


async def test_backtest_without_signal_does_not_open() -> None:
    clock = SimulatedClock(_NOW)
    # Realized vol (10%) below implied (20%) -> no buy-vol edge -> no entry.
    broker = MockBrokerAdapter(
        clock,
        MockMarketConfig(
            spot0=100.0,
            annual_vol=0.10,
            option_iv=0.20,
            dt_seconds=3600.0,
            days_to_expiry=30,
            seed=5,
            max_stream_steps=120,
        ),
    )
    engine = BacktestEngine(clock, broker, BacktestConfig(steps=100, lookback=30))
    report = await engine.run()
    assert report.opened_position is False
    assert report.hedge_count == 0
