"""Order/fill/event persistence (R25).

Maps between domain models (`app.models`) and ORM rows. Upserts orders by
``client_order_id`` (idempotent), appends events and fills. This is the durable
record the OMS reconciles against after a restart.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import OrderState, OrderType, Side, TimeInForce
from app.models import Fill, Order, OrderEvent, OrderRequest
from app.repositories.models import FillRow, OrderEventRow, OrderRow


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert_order(self, order: Order) -> None:
        req = order.request
        row = await self._s.get(OrderRow, req.client_order_id)
        if row is None:
            row = OrderRow(client_order_id=req.client_order_id)
            self._s.add(row)
        row.instrument_symbol = req.instrument_symbol
        row.side = req.side.value
        row.quantity = req.quantity
        row.order_type = req.order_type.value
        row.limit_price = req.limit_price
        row.time_in_force = req.time_in_force.value
        row.state = order.state.value
        row.broker_order_id = order.broker_order_id
        row.filled_quantity = order.filled_quantity
        row.average_fill_price = order.average_fill_price
        row.created_at = order.created_at
        row.updated_at = order.updated_at

    async def get_order(self, client_order_id: str) -> Order | None:
        row = await self._s.get(OrderRow, client_order_id)
        if row is None:
            return None
        return _row_to_order(row)

    async def list_open_orders(self) -> list[Order]:
        terminal = {
            OrderState.FILLED.value,
            OrderState.CANCELLED.value,
            OrderState.REJECTED.value,
            OrderState.EXPIRED.value,
        }
        stmt = select(OrderRow).where(OrderRow.state.notin_(terminal))
        rows = (await self._s.execute(stmt)).scalars().all()
        return [_row_to_order(r) for r in rows]

    async def add_event(self, event: OrderEvent) -> None:
        self._s.add(
            OrderEventRow(
                client_order_id=event.client_order_id,
                from_state=event.from_state.value,
                to_state=event.to_state.value,
                timestamp=event.timestamp,
                detail=event.detail,
            )
        )

    async def list_events(self, client_order_id: str) -> list[OrderEvent]:
        stmt = (
            select(OrderEventRow)
            .where(OrderEventRow.client_order_id == client_order_id)
            .order_by(OrderEventRow.id)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [
            OrderEvent(
                client_order_id=r.client_order_id,
                from_state=OrderState(r.from_state),
                to_state=OrderState(r.to_state),
                timestamp=r.timestamp,
                detail=r.detail,
            )
            for r in rows
        ]

    async def add_fill(self, fill: Fill) -> None:
        self._s.add(
            FillRow(
                broker_fill_id=fill.broker_fill_id,
                client_order_id=fill.client_order_id,
                instrument_symbol=fill.instrument_symbol,
                side=fill.side.value,
                quantity=fill.quantity,
                price=fill.price,
                commission=fill.commission,
                timestamp=fill.timestamp,
            )
        )

    async def list_fills(self, client_order_id: str) -> list[Fill]:
        stmt = (
            select(FillRow).where(FillRow.client_order_id == client_order_id).order_by(FillRow.id)
        )
        rows = (await self._s.execute(stmt)).scalars().all()
        return [
            Fill(
                client_order_id=r.client_order_id,
                instrument_symbol=r.instrument_symbol,
                side=Side(r.side),
                quantity=r.quantity,
                price=r.price,
                commission=r.commission,
                timestamp=r.timestamp,
                broker_fill_id=r.broker_fill_id,
            )
            for r in rows
        ]


def _row_to_order(row: OrderRow) -> Order:
    request = OrderRequest(
        client_order_id=row.client_order_id,
        instrument_symbol=row.instrument_symbol,
        side=Side(row.side),
        quantity=row.quantity,
        order_type=OrderType(row.order_type),
        limit_price=row.limit_price,
        time_in_force=TimeInForce(row.time_in_force),
    )
    return Order(
        request=request,
        state=OrderState(row.state),
        broker_order_id=row.broker_order_id,
        filled_quantity=row.filled_quantity,
        average_fill_price=row.average_fill_price,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
