"""Risk limit evaluation and the kill switch (R22)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config.settings import RiskConfig
from app.core.clock import SimulatedClock
from app.core.enums import KillSwitchPolicy
from app.core.exceptions import RiskLimitBreachError
from app.risk import (
    KillSwitch,
    KillSwitchTrigger,
    RiskManager,
    RiskState,
    evaluate_limits,
)


def _clock() -> SimulatedClock:
    return SimulatedClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))


def test_unset_limits_are_not_enforced() -> None:
    config = RiskConfig()  # all zero == unset
    state = RiskState(cash_delta=Decimal("1000000"), net_delta_units=99999.0)
    assert evaluate_limits(config, state) == []


def test_cash_delta_breach_detected() -> None:
    config = RiskConfig(maximum_cash_delta=Decimal("50000"))
    state = RiskState(cash_delta=Decimal("-60000"))  # abs exceeds limit
    breaches = evaluate_limits(config, state)
    assert [b.name for b in breaches] == ["maximum_cash_delta"]


def test_daily_loss_breach_uses_loss_magnitude() -> None:
    config = RiskConfig(maximum_daily_loss=Decimal("1000"))
    assert evaluate_limits(config, RiskState(daily_pnl=Decimal("-1500")))
    assert evaluate_limits(config, RiskState(daily_pnl=Decimal("-500"))) == []
    assert evaluate_limits(config, RiskState(daily_pnl=Decimal("5000"))) == []  # profit


def test_manager_blocks_new_positions_on_breach() -> None:
    config = RiskConfig(maximum_net_delta=Decimal("100"))
    mgr = RiskManager(config, KillSwitch(_clock()))
    state = RiskState(net_delta_units=250.0)
    assessment = mgr.evaluate(state)
    assert assessment.allow_new_positions is False
    with pytest.raises(RiskLimitBreachError):
        mgr.assert_can_open(state)


def test_critical_breach_trips_kill_switch() -> None:
    config = RiskConfig(maximum_daily_loss=Decimal("1000"))
    ks = KillSwitch(_clock())
    mgr = RiskManager(config, ks)
    mgr.evaluate(RiskState(daily_pnl=Decimal("-2000")))
    assert ks.is_tripped is True
    assert ks.events[0].trigger is KillSwitchTrigger.RISK_LIMIT_BREACH


def test_non_critical_breach_does_not_trip_kill_switch() -> None:
    config = RiskConfig(maximum_net_delta=Decimal("100"))
    ks = KillSwitch(_clock())
    mgr = RiskManager(config, ks)
    mgr.evaluate(RiskState(net_delta_units=250.0))
    assert ks.is_tripped is False


def test_kill_switch_latches_and_resets() -> None:
    ks = KillSwitch(_clock(), policy=KillSwitchPolicy.HOLD)
    assert ks.is_tripped is False
    ks.trip(KillSwitchTrigger.BROKER_DISCONNECT, "lost connection")
    ks.trip(KillSwitchTrigger.MARKET_DATA_LOSS, "no quotes")
    assert ks.is_tripped is True
    assert len(ks.events) == 2
    assert ks.policy is KillSwitchPolicy.HOLD
    ks.reset()
    assert ks.is_tripped is False


def test_order_size_limit() -> None:
    mgr = RiskManager(RiskConfig(maximum_single_order_size=5), KillSwitch(_clock()))
    mgr.check_order_size(5)  # ok
    with pytest.raises(RiskLimitBreachError):
        mgr.check_order_size(6)


def test_external_event_trips_switch_and_blocks() -> None:
    mgr = RiskManager(RiskConfig(), KillSwitch(_clock()))
    mgr.trip(KillSwitchTrigger.MANUAL_STOP, "operator STOP")
    assessment = mgr.evaluate(RiskState())
    assert assessment.allow_new_positions is False
    assert assessment.kill_switch_tripped is True
