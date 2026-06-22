"""Orchestration worker loop: end-to-end live-style run (wiring test)."""

from __future__ import annotations

from datetime import UTC, date, datetime

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
        OrchestratorConfig(max_steps=120, lookback=30, dt_seconds=3600.0),
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


async def test_orchestrator_emits_notifications() -> None:
    from app.api.control import InMemoryControlPlane
    from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
    from app.config.settings import AppSettings, RiskConfig
    from app.core.clock import SimulatedClock
    from app.notifications import CollectingChannel, NotificationService
    from app.risk import KillSwitch, RiskManager
    from workers import Orchestrator, OrchestratorConfig

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
            max_stream_steps=200,
        ),
    )
    control = InMemoryControlPlane(
        AppSettings(), broker, RiskManager(RiskConfig(), KillSwitch(clock))
    )
    channel = CollectingChannel()
    orch = Orchestrator(
        clock,
        broker,
        RiskManager(RiskConfig(), KillSwitch(clock)),
        control,
        OrchestratorConfig(max_steps=120, lookback=30, dt_seconds=3600.0),
        notifier=NotificationService([channel]),
    )
    await control.start(confirmation_code=None)
    await orch.run()
    events = {n.event for n in channel.sent}
    assert "started" in events
    assert "leg_filled" in events  # straddle opened -> leg notification
    assert "hedged" in events  # delta hedged at least once
    # The hedge notification now carries explicit trade text (symbol/side/price).
    hedge = next(n for n in channel.sent if n.event == "hedged")
    assert {"symbol", "side", "contracts", "price"} <= set(hedge.fields)


def test_select_expiry_picks_nearest_in_window() -> None:
    orch, _, _ = _setup()
    orch._cfg.min_days_to_expiry = 10
    orch._cfg.max_days_to_expiry = 45
    # _NOW = 2026-01-05: +3 (too near), +20 (pick), +40, +177 (too far).
    expiries = [date(2026, 1, 8), date(2026, 1, 25), date(2026, 2, 14), date(2026, 7, 1)]
    assert orch._select_expiry(expiries) == date(2026, 1, 25)


def test_select_expiry_falls_back_to_earliest_when_none_in_window() -> None:
    orch, _, _ = _setup()
    orch._cfg.min_days_to_expiry = 10
    orch._cfg.max_days_to_expiry = 45
    expiries = [date(2026, 1, 8), date(2026, 12, 1)]  # +3 and +330: none inside window
    assert orch._select_expiry(expiries) == date(2026, 1, 8)


async def test_orchestrator_options_on_futures_opens_hedges_and_notifies() -> None:
    # FORTS-style: options on the future (Black-76), no equity underlying. The
    # future is the spot/forward reference for entry, greeks and the delta hedge.
    from app.notifications import CollectingChannel, NotificationService

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
            max_stream_steps=200,
            options_on_futures=True,
        ),
    )
    # No equity instrument exists in this mode.
    assert all(i.asset_class.value != "equity" for i in await broker.list_instruments("XYZ"))
    control = InMemoryControlPlane(
        AppSettings(), broker, RiskManager(RiskConfig(), KillSwitch(clock))
    )
    channel = CollectingChannel()
    orch = Orchestrator(
        clock,
        broker,
        RiskManager(RiskConfig(), KillSwitch(clock)),
        control,
        OrchestratorConfig(max_steps=120, lookback=30, dt_seconds=3600.0, options_on_futures=True),
        notifier=NotificationService([channel]),
    )
    await control.start(confirmation_code=None)
    await orch.run()
    assert orch.opened is True
    assert orch.hedge_count > 0
    events = {n.event for n in channel.sent}
    assert {"started", "leg_filled", "hedged"} <= events
    # The hedge trades the future (the spot reference), and greeks were published.
    hedge = next(n for n in channel.sent if n.event == "hedged")
    assert "FUT" in hedge.fields["symbol"]
    greeks = await control.greeks()
    assert greeks["available"] is True


async def test_orchestrator_notifies_on_position_close() -> None:
    from app.notifications import CollectingChannel, NotificationService

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
            max_stream_steps=200,
        ),
    )
    control = InMemoryControlPlane(
        AppSettings(), broker, RiskManager(RiskConfig(), KillSwitch(clock))
    )
    channel = CollectingChannel()
    # A time-stop that is always satisfied (30 DTE <= 999) forces an exit right
    # after the straddle opens.
    orch = Orchestrator(
        clock,
        broker,
        RiskManager(RiskConfig(), KillSwitch(clock)),
        control,
        OrchestratorConfig(
            max_steps=120, lookback=30, dt_seconds=3600.0, exit_min_days_to_expiry=999
        ),
        notifier=NotificationService([channel]),
    )
    await control.start(confirmation_code=None)
    await orch.run()
    closes = [n for n in channel.sent if n.event == "position_closed"]
    assert closes  # at least one close notification was emitted
    assert "SELL" in closes[0].fields["legs"]  # explicit closing trade text
