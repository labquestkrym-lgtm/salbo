"""Position reconciliation (R23).

Compares locally-tracked positions against the broker's. A mismatch beyond a
tolerance is treated as critical: the caller must halt new trading and resolve
before continuing (a desync means our risk view is wrong).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.brokers.base import BaseBrokerAdapter
from app.core.logging import get_logger
from app.models import Position
from app.risk import KillSwitchTrigger, RiskManager

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PositionMismatch:
    symbol: str
    local_quantity: Decimal
    broker_quantity: Decimal

    @property
    def difference(self) -> Decimal:
        return self.local_quantity - self.broker_quantity


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    mismatches: list[PositionMismatch]

    @property
    def ok(self) -> bool:
        return not self.mismatches

    @property
    def max_abs_difference(self) -> Decimal:
        if not self.mismatches:
            return Decimal("0")
        return max(abs(m.difference) for m in self.mismatches)


def reconcile_positions(
    local: list[Position],
    broker: list[Position],
    *,
    tolerance: Decimal = Decimal("0"),
) -> ReconciliationResult:
    """Compare two position sets symbol-by-symbol."""
    local_by = {p.instrument_symbol: p.quantity for p in local}
    broker_by = {p.instrument_symbol: p.quantity for p in broker}
    mismatches: list[PositionMismatch] = []
    for symbol in local_by.keys() | broker_by.keys():
        lq = local_by.get(symbol, Decimal("0"))
        bq = broker_by.get(symbol, Decimal("0"))
        if abs(lq - bq) > tolerance:
            mismatches.append(
                PositionMismatch(symbol=symbol, local_quantity=lq, broker_quantity=bq)
            )
    return ReconciliationResult(mismatches=mismatches)


class ReconciliationService:
    """Pulls broker positions and compares with a local view; trips the kill
    switch (POSITION_DESYNC) on a critical mismatch so trading halts."""

    def __init__(
        self,
        broker: BaseBrokerAdapter,
        risk_manager: RiskManager,
        *,
        tolerance: Decimal = Decimal("0"),
    ) -> None:
        self._broker = broker
        self._risk = risk_manager
        self._tolerance = tolerance

    async def reconcile(self, local_positions: list[Position]) -> ReconciliationResult:
        broker_positions = await self._broker.get_positions()
        result = reconcile_positions(local_positions, broker_positions, tolerance=self._tolerance)
        if not result.ok:
            detail = ", ".join(
                f"{m.symbol}: local {m.local_quantity} vs broker {m.broker_quantity}"
                for m in result.mismatches
            )
            logger.error("position_desync", mismatches=detail)
            self._risk.trip(KillSwitchTrigger.POSITION_DESYNC, detail)
        return result
