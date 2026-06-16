"""Straddle leg execution (R21).

Opens both legs of a straddle and treats the structure as open **only when both
legs are confirmed filled**. If one leg fills and the other cannot within the
configured window, the executor rolls back the filled leg (or escalates), so we
never sit on unintended one-leg (naked) risk.

Leg execution can be sequential (default — confirm leg 1 before sending leg 2)
or parallel, per configuration. Order sizing/marketable-limit construction is
delegated to the caller; this module owns the *coordination* and one-leg-risk
handling, not strategy selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.enums import OrderState
from app.core.logging import get_logger
from app.execution.oms import OrderManager
from app.models import Order, OrderRequest

logger = get_logger(__name__)


class LegExecutionMode(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class StraddleOpenStatus(StrEnum):
    OPENED = "opened"
    ROLLED_BACK = "rolled_back"
    ONE_LEG_STUCK = "one_leg_stuck"  # filled one leg, could not unwind — escalate


@dataclass(frozen=True, slots=True)
class StraddleOpenResult:
    status: StraddleOpenStatus
    call_order: Order
    put_order: Order

    @property
    def is_open(self) -> bool:
        return self.status is StraddleOpenStatus.OPENED


class StraddleExecutor:
    def __init__(self, oms: OrderManager, *, mode: LegExecutionMode = LegExecutionMode.SEQUENTIAL):
        self._oms = oms
        self._mode = mode

    async def open(
        self, call_request: OrderRequest, put_request: OrderRequest
    ) -> StraddleOpenResult:
        if self._mode is LegExecutionMode.SEQUENTIAL:
            return await self._open_sequential(call_request, put_request)
        return await self._open_parallel(call_request, put_request)

    async def _open_sequential(
        self, call_request: OrderRequest, put_request: OrderRequest
    ) -> StraddleOpenResult:
        call_order = await self._oms.submit(call_request)
        if not _is_filled(call_order):
            # First leg didn't fill: cancel it, nothing to roll back.
            await self._safe_cancel(call_request.client_order_id)
            put_order = await self._oms.submit(put_request)  # record intent (will be cancelled)
            await self._safe_cancel(put_request.client_order_id)
            return StraddleOpenResult(StraddleOpenStatus.ROLLED_BACK, call_order, put_order)

        put_order = await self._oms.submit(put_request)
        if not _is_filled(put_order):
            # One-leg risk: unwind the filled call leg.
            return await self._rollback(call_order, put_order, filled_leg="call")
        return StraddleOpenResult(StraddleOpenStatus.OPENED, call_order, put_order)

    async def _open_parallel(
        self, call_request: OrderRequest, put_request: OrderRequest
    ) -> StraddleOpenResult:
        call_order = await self._oms.submit(call_request)
        put_order = await self._oms.submit(put_request)
        call_ok = _is_filled(call_order)
        put_ok = _is_filled(put_order)
        if call_ok and put_ok:
            return StraddleOpenResult(StraddleOpenStatus.OPENED, call_order, put_order)
        if call_ok and not put_ok:
            return await self._rollback(call_order, put_order, filled_leg="call")
        if put_ok and not call_ok:
            return await self._rollback(call_order, put_order, filled_leg="put")
        # Neither filled: cancel both.
        await self._safe_cancel(call_request.client_order_id)
        await self._safe_cancel(put_request.client_order_id)
        return StraddleOpenResult(StraddleOpenStatus.ROLLED_BACK, call_order, put_order)

    async def _rollback(
        self, call_order: Order, put_order: Order, *, filled_leg: str
    ) -> StraddleOpenResult:
        filled = call_order if filled_leg == "call" else put_order
        unfilled = put_order if filled_leg == "call" else call_order
        await self._safe_cancel(unfilled.request.client_order_id)
        closing = _closing_request(filled)
        logger.warning(
            "straddle_one_leg_rollback",
            filled_leg=filled_leg,
            closing_order_id=closing.client_order_id,
        )
        close_order = await self._oms.submit(closing)
        status = (
            StraddleOpenStatus.ROLLED_BACK
            if _is_filled(close_order)
            else StraddleOpenStatus.ONE_LEG_STUCK
        )
        if status is StraddleOpenStatus.ONE_LEG_STUCK:
            logger.error("straddle_one_leg_stuck", filled_leg=filled_leg)
        return StraddleOpenResult(status, call_order, put_order)

    async def _safe_cancel(self, client_order_id: str) -> None:
        try:
            await self._oms.cancel(client_order_id)
        except Exception as exc:
            logger.warning(
                "rollback_cancel_failed", client_order_id=client_order_id, error=str(exc)
            )


def _is_filled(order: Order) -> bool:
    return order.state is OrderState.FILLED


def _closing_request(filled: Order) -> OrderRequest:
    """Build a market-ish closing order opposite to a filled leg."""
    from app.core.enums import OrderType, Side

    opposite = Side.SELL if filled.request.side is Side.BUY else Side.BUY
    return OrderRequest(
        client_order_id=f"{filled.request.client_order_id}-rollback",
        instrument_symbol=filled.request.instrument_symbol,
        side=opposite,
        quantity=filled.filled_quantity,
        order_type=OrderType.MARKET,
    )
