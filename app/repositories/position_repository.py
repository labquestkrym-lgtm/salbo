"""Position persistence (R25) — used by reconciliation and recovery."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Position
from app.repositories.models import PositionRow


class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(self, position: Position, *, at: datetime) -> None:
        row = await self._s.get(PositionRow, position.instrument_symbol)
        if row is None:
            row = PositionRow(instrument_symbol=position.instrument_symbol)
            self._s.add(row)
        row.quantity = position.quantity
        row.average_price = position.average_price
        row.updated_at = at

    async def get(self, instrument_symbol: str) -> Position | None:
        row = await self._s.get(PositionRow, instrument_symbol)
        if row is None:
            return None
        return Position(
            instrument_symbol=row.instrument_symbol,
            quantity=row.quantity,
            average_price=row.average_price,
        )

    async def list_all(self) -> list[Position]:
        rows = (await self._s.execute(select(PositionRow))).scalars().all()
        return [
            Position(
                instrument_symbol=r.instrument_symbol,
                quantity=r.quantity,
                average_price=r.average_price,
            )
            for r in rows
        ]
