"""Durable OMS order store (R25 + R20).

Implements the async ``OrderStore`` protocol that :class:`OrderManager` depends
on, persisting to the database via :class:`OrderRepository`. Each operation runs
in its own committed session, so OMS state survives a process restart and can be
recovered (``OrderManager.recover``).
"""

from __future__ import annotations

from app.models import Order, OrderEvent
from app.repositories.database import Database
from app.repositories.order_repository import OrderRepository


class SqlOrderStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def save_order(self, order: Order) -> None:
        async with self._db.session() as session:
            await OrderRepository(session).upsert_order(order)

    async def get_order(self, client_order_id: str) -> Order | None:
        async with self._db.session() as session:
            return await OrderRepository(session).get_order(client_order_id)

    async def list_open(self) -> list[Order]:
        async with self._db.session() as session:
            return await OrderRepository(session).list_open_orders()

    async def save_event(self, event: OrderEvent) -> None:
        async with self._db.session() as session:
            await OrderRepository(session).add_event(event)

    async def list_events(self, client_order_id: str) -> list[OrderEvent]:
        async with self._db.session() as session:
            return await OrderRepository(session).list_events(client_order_id)
