"""SQLAlchemy 2.0 ORM models (R25).

Covers the core persisted entities: instruments, orders, order events, fills,
positions, portfolio snapshots, audit log and config versions. Money/quantities
use :class:`DecimalText` for exact storage.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.repositories.types import DecimalText


class Base(DeclarativeBase):
    pass


class InstrumentRow(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    underlying_symbol: Mapped[str] = mapped_column(String(64), index=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(8))
    multiplier: Mapped[Decimal] = mapped_column(DecimalText(40))
    lot_size: Mapped[int] = mapped_column(Integer)
    tick_size: Mapped[Decimal] = mapped_column(DecimalText(40))
    expiry: Mapped[str | None] = mapped_column(String(16), nullable=True)
    option_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(DecimalText(40), nullable=True)


class OrderRow(Base):
    __tablename__ = "orders"

    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(DecimalText(40))
    order_type: Mapped[str] = mapped_column(String(24))
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText(40), nullable=True)
    time_in_force: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(24), index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalText(40))
    average_fill_price: Mapped[Decimal] = mapped_column(DecimalText(40))
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrderEventRow(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    from_state: Mapped[str] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str] = mapped_column(Text, default="")


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    broker_fill_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_order_id: Mapped[str] = mapped_column(String(64), index=True)
    instrument_symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(DecimalText(40))
    price: Mapped[Decimal] = mapped_column(DecimalText(40))
    commission: Mapped[Decimal] = mapped_column(DecimalText(40))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PositionRow(Base):
    __tablename__ = "positions"

    instrument_symbol: Mapped[str] = mapped_column(String(64), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(DecimalText(40))
    average_price: Mapped[Decimal] = mapped_column(DecimalText(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PortfolioSnapshotRow(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    net_delta_units: Mapped[Decimal] = mapped_column(DecimalText(40))
    cash_delta: Mapped[Decimal] = mapped_column(DecimalText(40))
    net_gamma_units: Mapped[Decimal] = mapped_column(DecimalText(40))
    net_vega_per_pct: Mapped[Decimal] = mapped_column(DecimalText(40))
    net_theta_per_day: Mapped[Decimal] = mapped_column(DecimalText(40))
    gross_exposure: Mapped[Decimal] = mapped_column(DecimalText(40))


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text, default="")


class ConfigVersionRow(Base):
    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    version_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[str] = mapped_column(Text)
