"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decimal columns are stored as text for exact, backend-portable Decimals
# (see app.repositories.types.DecimalText / ADR-0001).
_DEC = sa.String(length=40)


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("symbol", sa.String(64), primary_key=True),
        sa.Column("underlying_symbol", sa.String(64), nullable=False, index=True),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("multiplier", _DEC, nullable=False),
        sa.Column("lot_size", sa.Integer, nullable=False),
        sa.Column("tick_size", _DEC, nullable=False),
        sa.Column("expiry", sa.String(16)),
        sa.Column("option_type", sa.String(8)),
        sa.Column("strike", _DEC),
    )
    op.create_table(
        "orders",
        sa.Column("client_order_id", sa.String(64), primary_key=True),
        sa.Column("instrument_symbol", sa.String(64), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", _DEC, nullable=False),
        sa.Column("order_type", sa.String(24), nullable=False),
        sa.Column("limit_price", _DEC),
        sa.Column("time_in_force", sa.String(8), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, index=True),
        sa.Column("broker_order_id", sa.String(64)),
        sa.Column("filled_quantity", _DEC, nullable=False),
        sa.Column("average_fill_price", _DEC, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "order_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(64), nullable=False, index=True),
        sa.Column("from_state", sa.String(24), nullable=False),
        sa.Column("to_state", sa.String(24), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
    )
    op.create_table(
        "fills",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("broker_fill_id", sa.String(64)),
        sa.Column("client_order_id", sa.String(64), nullable=False, index=True),
        sa.Column("instrument_symbol", sa.String(64), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", _DEC, nullable=False),
        sa.Column("price", _DEC, nullable=False),
        sa.Column("commission", _DEC, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "positions",
        sa.Column("instrument_symbol", sa.String(64), primary_key=True),
        sa.Column("quantity", _DEC, nullable=False),
        sa.Column("average_price", _DEC, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("net_delta_units", _DEC, nullable=False),
        sa.Column("cash_delta", _DEC, nullable=False),
        sa.Column("net_gamma_units", _DEC, nullable=False),
        sa.Column("net_vega_per_pct", _DEC, nullable=False),
        sa.Column("net_theta_per_day", _DEC, nullable=False),
        sa.Column("gross_exposure", _DEC, nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
    )
    op.create_table(
        "config_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version_hash", sa.String(64), nullable=False, index=True),
        sa.Column("payload", sa.Text, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "config_versions",
        "audit_log",
        "portfolio_snapshots",
        "positions",
        "fills",
        "order_events",
        "orders",
        "instruments",
    ):
        op.drop_table(table)
