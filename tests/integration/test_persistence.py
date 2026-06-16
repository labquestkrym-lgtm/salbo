"""Persistence round-trips and migrations (R25).

Production targets PostgreSQL (asyncpg); these tests run against async SQLite,
which is sufficient to prove the mapping, Decimal exactness and that the Alembic
migration applies. (Docker/Postgres are not available in this environment.)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.enums import OrderState, OrderType, Side
from app.models import Fill, Order, OrderEvent, OrderRequest, Position
from app.repositories import Database, OrderRepository, PositionRepository, build_engine

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _db(tmp_path: Path) -> Database:
    return Database(build_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"))


def _order() -> Order:
    req = OrderRequest(
        client_order_id="o1",
        instrument_symbol="FUT",
        side=Side.BUY,
        quantity=Decimal("3"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("123.45"),
    )
    return Order(
        request=req,
        state=OrderState.PARTIALLY_FILLED,
        broker_order_id="bk-1",
        filled_quantity=Decimal("1.0001"),
        average_fill_price=Decimal("123.4500001"),
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_order_round_trip_is_decimal_exact(tmp_path: Path) -> None:
    db = _db(tmp_path)
    await db.create_all()
    async with db.session() as s:
        await OrderRepository(s).upsert_order(_order())
    async with db.session() as s:
        got = await OrderRepository(s).get_order("o1")
    assert got is not None
    assert got.request.limit_price == Decimal("123.45")
    assert got.filled_quantity == Decimal("1.0001")  # exact, no float drift
    assert got.average_fill_price == Decimal("123.4500001")
    assert got.state is OrderState.PARTIALLY_FILLED
    await db.dispose()


async def test_events_and_fills_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    await db.create_all()
    async with db.session() as s:
        repo = OrderRepository(s)
        await repo.upsert_order(_order())
        await repo.add_event(
            OrderEvent(
                client_order_id="o1",
                from_state=OrderState.SUBMITTED,
                to_state=OrderState.ACKNOWLEDGED,
                timestamp=_NOW,
                detail="ack",
            )
        )
        await repo.add_fill(
            Fill(
                client_order_id="o1",
                instrument_symbol="FUT",
                side=Side.BUY,
                quantity=Decimal("1.0001"),
                price=Decimal("123.45"),
                commission=Decimal("0.65"),
                timestamp=_NOW,
                broker_fill_id="f1",
            )
        )
    async with db.session() as s:
        repo = OrderRepository(s)
        events = await repo.list_events("o1")
        fills = await repo.list_fills("o1")
        open_orders = await repo.list_open_orders()
    assert [e.to_state for e in events] == [OrderState.ACKNOWLEDGED]
    assert fills[0].commission == Decimal("0.65")
    assert open_orders[0].request.client_order_id == "o1"
    await db.dispose()


async def test_position_round_trip(tmp_path: Path) -> None:
    db = _db(tmp_path)
    await db.create_all()
    async with db.session() as s:
        await PositionRepository(s).upsert(
            Position(
                instrument_symbol="FUT", quantity=Decimal("-2.5"), average_price=Decimal("99.875")
            ),
            at=_NOW,
        )
    async with db.session() as s:
        got = await PositionRepository(s).get("FUT")
    assert got is not None
    assert got.quantity == Decimal("-2.5")
    assert got.average_price == Decimal("99.875")
    await db.dispose()


def test_alembic_migration_applies(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "migrated.db"
    os.environ["ALEMBIC_URL"] = f"sqlite:///{db_path.as_posix()}"
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        con.close()
    assert {"orders", "order_events", "fills", "positions", "audit_log"} <= tables
