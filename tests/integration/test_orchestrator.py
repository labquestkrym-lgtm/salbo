"""Orchestration worker loop: end-to-end live-style run (wiring test)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.api.control import InMemoryControlPlane
from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.config.settings import AppSettings, RiskConfig
from app.core.clock import SimulatedClock
from app.observability import Metrics
from app.risk import KillSwitch, RiskManager
from workers import Orchestrator, OrchestratorConfig

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _setup(*, vol: float = 0.50):
    clock = SimulatedClock(_NOW)
    broker = MockBrokerAdapter(
        clock,
        MockMarketConfig(
            spot0=100.0,
            annual_vol=vol,
            option_iv=0.20,
            dt_seconds=3600.0,
            days_to_expiry=30,
            seed=3,
            max_stream_steps=200,
        ),
    )
    settings = AppSettings()
    risk = RiskManager(RiskConfig(), KillSwitch(clock))
    control = InMemoryControlPlane(settings, broker, risk)
    metrics = Metrics()
    orch = Orchestrator(
        clock,
        broker,
        risk,
        control,
        OrchestratorConfig(max_steps=120, lookback=30),
        metrics=metrics,
    )
    return orch, control, metrics


async def test_running_orchestrator_opens_and_hedges_and_publishes() -> None:
    orch, control, metrics = _setup()
    await control.start(confirmation_code=None)  # backtest mode -> RUNNING
    await orch.run()
    assert orch.opened is True
    assert orch.hedge_count > 0
    # Control plane received a Greeks snapshot.
    greeks = await control.greeks()
    assert greeks["available"] is True
    assert "net_delta_units" in greeks
    # Metrics gauge was published.
    assert b"tradingbot_net_delta_units" in metrics.render()


async def test_stopped_orchestrator_does_not_open() -> None:
    orch, _control, _ = _setup()
    # run_state stays STOPPED (never started) -> no entry.
    await orch.run()
    assert orch.opened is False
    assert orch.hedge_count == 0


async def test_paused_orchestrator_does_not_open_new_position() -> None:
    orch, control, _ = _setup()
    await control.pause()  # PAUSED -> manage only, no new entry
    await orch.run()
    assert orch.opened is False
