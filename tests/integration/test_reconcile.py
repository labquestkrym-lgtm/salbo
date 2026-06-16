"""Position reconciliation (R23)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.config.settings import RiskConfig
from app.core.clock import SimulatedClock
from app.core.enums import OrderType, Side
from app.execution import reconcile_positions
from app.execution.reconcile import ReconciliationService
from app.models import OrderRequest, Position
from app.risk import KillSwitch, KillSwitchTrigger, RiskManager

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def test_matching_positions_reconcile() -> None:
    local = [Position(instrument_symbol="A", quantity=Decimal("3"))]
    broker = [Position(instrument_symbol="A", quantity=Decimal("3"))]
    result = reconcile_positions(local, broker)
    assert result.ok
    assert result.max_abs_difference == Decimal("0")


def test_mismatch_detected() -> None:
    local = [Position(instrument_symbol="A", quantity=Decimal("3"))]
    broker = [Position(instrument_symbol="A", quantity=Decimal("1"))]
    result = reconcile_positions(local, broker)
    assert not result.ok
    assert result.mismatches[0].difference == Decimal("2")


def test_missing_on_one_side_is_mismatch() -> None:
    result = reconcile_positions([Position(instrument_symbol="A", quantity=Decimal("2"))], [])
    assert not result.ok
    assert result.mismatches[0].broker_quantity == Decimal("0")


def test_within_tolerance_is_ok() -> None:
    local = [Position(instrument_symbol="A", quantity=Decimal("3"))]
    broker = [Position(instrument_symbol="A", quantity=Decimal("3.0001"))]
    assert reconcile_positions(local, broker, tolerance=Decimal("0.001")).ok


async def test_service_trips_kill_switch_on_desync() -> None:
    clock = SimulatedClock(_NOW)
    broker = MockBrokerAdapter(clock, MockMarketConfig())
    await broker.connect()
    # Give the broker a real position.
    await broker.place_order(
        OrderRequest(
            client_order_id="x",
            instrument_symbol=broker.future_symbol,
            side=Side.BUY,
            quantity=Decimal("2"),
            order_type=OrderType.MARKET,
        )
    )
    ks = KillSwitch(clock)
    risk = RiskManager(RiskConfig(), ks)
    service = ReconciliationService(broker, risk)
    # Local view wrongly thinks we are flat.
    result = await service.reconcile(local_positions=[])
    assert not result.ok
    assert ks.is_tripped
    assert ks.events[0].trigger is KillSwitchTrigger.POSITION_DESYNC
