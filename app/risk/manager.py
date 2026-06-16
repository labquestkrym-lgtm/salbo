"""Risk Manager and kill switch (R22).

The :class:`RiskManager` can veto any new position. It evaluates hard limits and
owns a :class:`KillSwitch`. Critical limit breaches and external events (data
loss, broker disconnect, position desync, manual STOP, …) trip the switch.

When tripped, the switch records the reason and exposes the configured policy
(``HOLD`` by default — options are not panic-closed; see RISK_MANAGEMENT.md).
The orchestrator is responsible for *executing* the policy (cancel orders,
reconcile, hedge/flatten/close); the manager only decides and records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.config.settings import RiskConfig
from app.core.clock import Clock
from app.core.enums import KillSwitchPolicy
from app.core.exceptions import RiskLimitBreachError
from app.core.logging import get_logger
from app.risk.limits import CRITICAL_LIMITS, LimitBreach, RiskState, evaluate_limits

logger = get_logger(__name__)


class KillSwitchTrigger(StrEnum):
    MARKET_DATA_LOSS = "market_data_loss"
    BROKER_DISCONNECT = "broker_disconnect"
    POSITION_DESYNC = "position_desync"
    UNKNOWN_ORDER_STATUS = "unknown_order_status"
    DAILY_LOSS_BREACH = "daily_loss_breach"
    MARGIN_BREACH = "margin_breach"
    ABNORMAL_PRICE_MOVE = "abnormal_price_move"
    EXCESSIVE_SPREAD = "excessive_spread"
    INVALID_VOLATILITY = "invalid_volatility"
    GREEKS_ERROR = "greeks_error"
    REPEATED_REJECTS = "repeated_rejects"
    SYSTEM_ERROR = "system_error"
    MANUAL_STOP = "manual_stop"
    RISK_LIMIT_BREACH = "risk_limit_breach"


@dataclass(frozen=True, slots=True)
class KillSwitchEvent:
    trigger: KillSwitchTrigger
    detail: str
    at: datetime


class KillSwitch:
    """Latches on the first trip; only a manual ``reset`` clears it."""

    def __init__(self, clock: Clock, policy: KillSwitchPolicy = KillSwitchPolicy.HOLD) -> None:
        self._clock = clock
        self.policy = policy
        self._events: list[KillSwitchEvent] = []

    @property
    def is_tripped(self) -> bool:
        return bool(self._events)

    @property
    def events(self) -> list[KillSwitchEvent]:
        return list(self._events)

    def trip(self, trigger: KillSwitchTrigger, detail: str = "") -> KillSwitchEvent:
        event = KillSwitchEvent(trigger=trigger, detail=detail, at=self._clock.now())
        self._events.append(event)
        logger.error(
            "kill_switch_tripped", trigger=trigger.value, detail=detail, policy=self.policy.value
        )
        return event

    def reset(self) -> None:
        logger.warning("kill_switch_reset", cleared_events=len(self._events))
        self._events.clear()


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    breaches: list[LimitBreach]
    kill_switch_tripped: bool
    allow_new_positions: bool

    @property
    def ok(self) -> bool:
        return not self.breaches and not self.kill_switch_tripped


class RiskManager:
    def __init__(self, config: RiskConfig, kill_switch: KillSwitch) -> None:
        self._config = config
        self._kill = kill_switch

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill

    def evaluate(self, state: RiskState) -> RiskAssessment:
        """Evaluate limits; trip the kill switch on any critical breach."""
        breaches = evaluate_limits(self._config, state)
        for breach in breaches:
            if breach.name in CRITICAL_LIMITS:
                self._kill.trip(KillSwitchTrigger.RISK_LIMIT_BREACH, str(breach))
        return RiskAssessment(
            breaches=breaches,
            kill_switch_tripped=self._kill.is_tripped,
            allow_new_positions=not breaches and not self._kill.is_tripped,
        )

    def assert_can_open(self, state: RiskState) -> None:
        """Raise if a new position must not be opened."""
        assessment = self.evaluate(state)
        if not assessment.allow_new_positions:
            reasons = [str(b) for b in assessment.breaches]
            if assessment.kill_switch_tripped:
                reasons.append("kill switch is tripped")
            raise RiskLimitBreachError("new positions blocked: " + "; ".join(reasons))

    def check_order_size(self, quantity: int) -> None:
        limit = self._config.maximum_single_order_size
        if limit > 0 and quantity > limit:
            raise RiskLimitBreachError(
                f"order size {quantity} exceeds maximum_single_order_size {limit}"
            )

    def trip(self, trigger: KillSwitchTrigger, detail: str = "") -> None:
        """Trip the kill switch for an external (non-limit) event."""
        self._kill.trip(trigger, detail)
