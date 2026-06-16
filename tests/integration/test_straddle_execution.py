"""Straddle two-leg execution and one-leg rollback (R21)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from app.core.enums import OrderState, OrderType, Side
from app.execution import InMemoryOrderStore, OrderManager
from app.execution.straddle import LegExecutionMode, StraddleExecutor, StraddleOpenStatus
from app.models import Order, OrderRequest

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _atm(broker: MockBrokerAdapter) -> tuple[str, str]:
    exp = broker._cfg.expiry.strftime("%Y%m%d")
    return f"XYZ-C-100-{exp}", f"XYZ-P-100-{exp}"


def _buy(symbol: str, coid: str) -> OrderRequest:
    return OrderRequest(
        client_order_id=coid,
        instrument_symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
    )


async def test_both_legs_fill_opens_straddle() -> None:
    broker = MockBrokerAdapter(SimulatedClock(_NOW), MockMarketConfig())
    await broker.connect()
    call_sym, put_sym = _atm(broker)
    oms = OrderManager(broker, InMemoryOrderStore(), SimulatedClock(_NOW))
    executor = StraddleExecutor(oms, mode=LegExecutionMode.SEQUENTIAL)
    result = await executor.open(_buy(call_sym, "c"), _buy(put_sym, "p"))
    assert result.status is StraddleOpenStatus.OPENED
    assert result.call_order.state is OrderState.FILLED
    assert result.put_order.state is OrderState.FILLED


class _CallOnlyBroker(MockBrokerAdapter):
    """Fills call legs and rollbacks; leaves put legs unfilled (acknowledged)."""

    async def place_order(self, request: OrderRequest) -> Order:
        if "-P-" in request.instrument_symbol:
            from app.models import Order as _O

            order = _O(
                request=request,
                state=OrderState.ACKNOWLEDGED,
                broker_order_id=f"noput-{request.client_order_id}",
                created_at=self._clock.now(),
                updated_at=self._clock.now(),
            )
            self._orders[request.client_order_id] = order
            return order
        return await super().place_order(request)


async def test_one_leg_failure_rolls_back() -> None:
    broker = _CallOnlyBroker(SimulatedClock(_NOW), MockMarketConfig())
    await broker.connect()
    call_sym, put_sym = _atm(broker)
    oms = OrderManager(broker, InMemoryOrderStore(), SimulatedClock(_NOW))
    executor = StraddleExecutor(oms, mode=LegExecutionMode.SEQUENTIAL)
    result = await executor.open(_buy(call_sym, "c2"), _buy(put_sym, "p2"))
    # Call filled, put didn't -> rollback closes the call leg.
    assert result.status is StraddleOpenStatus.ROLLED_BACK
    # A rollback (closing) order was created opposite to the filled call.
    rollback = await broker.get_order("c2-rollback")
    assert rollback.request.side is Side.SELL
    assert rollback.state is OrderState.FILLED
