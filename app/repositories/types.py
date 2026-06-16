"""Custom column types.

``DecimalText`` stores a ``Decimal`` as its canonical string and reads it back as
``Decimal`` — exact on every backend (SQLite, PostgreSQL), unlike binary-float
NUMERIC round-trips on engines without a native decimal type. We do all money
arithmetic in Python (ADR-0001), so we don't rely on SQL-side decimal math.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator


class DecimalText(TypeDecorator[Decimal]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: Decimal | int | str | None, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(Decimal(value))

    def process_result_value(self, value: str | None, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return Decimal(value)
