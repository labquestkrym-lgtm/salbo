"""Async engine / session management (R25)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.repositories.models import Base


def build_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine. Use a postgresql+asyncpg URL in production or a
    sqlite+aiosqlite URL for tests."""
    return create_async_engine(url, echo=echo, future=True)


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create_all(self) -> None:
        """Create tables from metadata (tests / first boot). Production uses
        Alembic migrations instead."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
