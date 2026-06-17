"""Persistence layer: SQLAlchemy 2.0 async models + repositories.

Money/quantities are stored exactly as text-backed ``Decimal`` (ADR-0001) so no
binary-float error creeps into cash accounting, on any backend. Production uses
PostgreSQL (asyncpg); tests run against async SQLite.
"""

from app.repositories.database import Database, build_engine
from app.repositories.order_repository import OrderRepository
from app.repositories.order_store import SqlOrderStore
from app.repositories.position_repository import PositionRepository

__all__ = [
    "Database",
    "OrderRepository",
    "PositionRepository",
    "SqlOrderStore",
    "build_engine",
]
