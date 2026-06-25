"""Pairs orchestration loop: end-to-end on a synthetic cointegrated pair."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.api.control import InMemoryControlPlane
from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.config.settings import AppSettings, RiskConfig
from app.core.clock import SimulatedClock
from app.core.enums import AssetClass
from app.instruments.resolver import ResolvedPair
from app.models import ContractSpec, Instrument, Position
from app.notifications import CollectingChannel, NotificationService
from app.risk import KillSwitch, RiskManager
from workers import PairsOrchestrator, PairsOrchestratorConfig

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_SPEC = ContractSpec(
    tick_size=Decimal("0.01"),
    tick_value=Decimal("1"),
    lot_size=1,
    multiplier=Decimal("1"),
    currency="USD",
)


class _PairMock(MockBrokerAdapter):
    """Two futures (AAA/BBB) whose log-spread is a mean-reverting OU process, so
    ln(A)-ln(B) is stationary (cointegrated) and the z-score breaches the bands."""

    def _build_universe(self) -> None:  # type: ignore[override]
        c = self._cfg
        self._instruments = {}
        for u in ("AAA", "BBB"):
            sym = f"{u}-FUT"
            self._instruments[sym] = Instrument(
                symbol=sym,
                underlying_symbol=u,
                asset_class=AssetClass.FUTURE,
                spec=_SPEC,
                expiry=c.expiry,
            )
        self._future_symbol = "AAA-FUT"
        self._log_a = math.log(100.0)
        self._log_spread = 0.0
        self._k = 0.12  # OU mean-reversion speed

    def _advance_price(self) -> None:  # type: ignore[override]
        z1 = float(self._rng.standard_normal())
        z2 = float(self._rng.standard_normal())
        self._log_a += 0.008 * z1
        self._log_spread = (1.0 - self._k) * self._log_spread + 0.06 * z2  # OU around 0
        self._step += 1

    def _theoretical_mid(self, inst: Instrument, now: datetime) -> float:  # type: ignore[override]
        if inst.underlying_symbol == "AAA":
            return math.exp(self._log_a)
        return math.exp(self._log_a - self._log_spread)


def _setup() -> tuple[PairsOrchestrator, InMemoryControlPlane, CollectingChannel]:
    clock = SimulatedClock(_NOW)
    broker = _PairMock(
        clock, MockMarketConfig(dt_seconds=3600.0, seed=7, max_stream_steps=400)
    )
    control = InMemoryControlPlane(
        AppSettings(), broker, RiskManager(RiskConfig(), KillSwitch(clock))
    )
    channel = CollectingChannel()
    orch = PairsOrchestrator(
        clock,
        broker,
        RiskManager(RiskConfig(), KillSwitch(clock)),
        control,
        PairsOrchestratorConfig(
            symbol_a="AAA",
            symbol_b="BBB",
            window=20,
            entry_z=2.0,
            exit_z=0.5,
            dt_seconds=3600.0,
            target_notional_per_leg=Decimal("300"),
            max_contracts_per_leg=5,
            max_steps=300,
        ),
        notifier=NotificationService([channel]),
    )
    return orch, control, channel


async def test_pairs_orchestrator_opens_and_closes_on_reversion() -> None:
    orch, control, channel = _setup()
    await control.start(confirmation_code=None)  # backtest mode -> RUNNING
    await orch.run()
    assert orch.trade_count > 0  # the spread breached the entry band at least once
    events = {n.event for n in channel.sent}
    assert "started" in events
    assert "leg_filled" in events  # two-leg futures orders were sent
    # A round trip should have produced at least one close notification.
    assert any(n.event == "position_closed" for n in channel.sent)


def _pair(orch: PairsOrchestrator) -> ResolvedPair:
    b = orch._broker
    return ResolvedPair(leg_a=b._instruments["AAA-FUT"], leg_b=b._instruments["BBB-FUT"], beta=1.0)


async def test_seed_daily_spreads_uses_daily_window() -> None:
    orch, _control, _channel = _setup()  # window=20
    pair = _pair(orch)

    async def fake_closes(symbol: str, *, days: int) -> list[tuple[date, float]]:
        base = date(2026, 1, 1)
        if symbol == "AAA-FUT":
            return [(base + timedelta(days=i), 100.0 + (1.0 if i % 2 else -1.0)) for i in range(80)]
        return [(base + timedelta(days=i), 100.0) for i in range(80)]

    orch._broker.get_daily_closes = fake_closes  # type: ignore[method-assign]
    await orch._seed_daily_spreads(pair)
    assert len(orch._daily_spreads) >= orch._cfg.window  # seeded the window
    series = orch._decision_series(0.05)
    assert series[-1] == 0.05  # today's live spread is appended to the daily history
    assert len(series) == len(orch._daily_spreads) + 1


def test_decision_series_falls_back_to_ticks_without_seed() -> None:
    orch, _control, _channel = _setup()  # no daily seed
    assert orch._decision_series(0.1) == [0.1]
    assert orch._decision_series(0.2) == [0.1, 0.2]  # tick-accumulated fallback


async def test_reconcile_resumes_existing_spread() -> None:
    orch, _control, _channel = _setup()
    pair = _pair(orch)

    async def fake_positions() -> list[Position]:
        return [
            Position(instrument_symbol="AAA-FUT", quantity=Decimal("2")),
            Position(instrument_symbol="BBB-FUT", quantity=Decimal("-2")),
        ]

    orch._broker.get_positions = fake_positions  # type: ignore[method-assign]
    await orch._reconcile(pair)
    assert orch._opened is True
    assert orch._direction == 1  # long A / short B
    assert orch._contracts_a == 2 and orch._contracts_b == 2


async def test_reconcile_flattens_orphan_leg() -> None:
    orch, _control, _channel = _setup()
    pair = _pair(orch)
    await orch._broker.connect()

    async def fake_positions() -> list[Position]:
        return [Position(instrument_symbol="AAA-FUT", quantity=Decimal("1"))]

    orch._broker.get_positions = fake_positions  # type: ignore[method-assign]
    await orch._reconcile(pair)
    assert orch._opened is False  # a lone leg is flattened, not adopted as a pair


def test_pair_pnl_mark_to_market() -> None:
    orch, _control, _channel = _setup()
    pair = _pair(orch)
    # Long spread (long A / short B), 1 lot each, entered at 100/100.
    orch._opened = True
    orch._direction = 1
    orch._contracts_a = orch._contracts_b = 1
    orch._entry_mid_a = orch._entry_mid_b = Decimal("100")
    # A rises by 1 point; point value = tick_value/tick_size = 1/0.01 = 100 -> +100.
    assert orch._pair_pnl(pair, Decimal("101"), Decimal("100")) == Decimal("100")
    # Same move while SHORT the spread -> -100.
    orch._direction = -1
    assert orch._pair_pnl(pair, Decimal("101"), Decimal("100")) == Decimal("-100")
    # Flat -> zero regardless of marks.
    orch._opened = False
    assert orch._pair_pnl(pair, Decimal("150"), Decimal("90")) == Decimal("0")


async def test_pairs_orchestrator_stopped_does_not_trade() -> None:
    orch, _control, _channel = _setup()
    # never started -> run-state STOPPED -> no orders.
    await orch.run()
    assert orch.trade_count == 0
    assert orch.opened is False
