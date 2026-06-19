"""Pure conversions between the T-Invest API wire types and our domain.

T-Invest encodes prices as ``Quotation``/``MoneyValue`` = ``units`` (int) plus
``nano`` (int, billionths). We convert to/from :class:`~decimal.Decimal` exactly
(ADR-0001) — no binary-float ever touches money. Order-direction and
order-status mappings use the API's stable integer enum values so they can be
unit-tested without the SDK installed.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

from app.core.enums import OrderState, Side

_NANO = Decimal("1000000000")  # 1e9


def quotation_to_decimal(units: int, nano: int) -> Decimal:
    """``units + nano/1e9`` as an exact Decimal (works for Quotation & MoneyValue)."""
    return Decimal(units) + (Decimal(nano) / _NANO)


# MoneyValue carries the same units/nano shape (plus a currency code).
money_to_decimal = quotation_to_decimal


def decimal_to_quotation(value: Decimal) -> tuple[int, int]:
    """Inverse of :func:`quotation_to_decimal`. ``units`` truncates toward zero;
    ``nano`` carries the fractional remainder with a matching sign (the T-Invest
    convention for negative values)."""
    units = int(value.to_integral_value(rounding=ROUND_DOWN))
    frac = value - units
    nano = int((frac * _NANO).to_integral_value(rounding=ROUND_HALF_UP))
    return units, nano


def quotation_obj_to_decimal(q: Any) -> Decimal:
    """Convert an SDK ``Quotation``/``MoneyValue`` object (``.units``/``.nano``)."""
    return quotation_to_decimal(q.units, q.nano)


# --- order direction (our Side -> T-Invest OrderDirection int) -------------
_ORDER_DIRECTION_BUY = 1
_ORDER_DIRECTION_SELL = 2


def side_to_direction(side: Side) -> int:
    return _ORDER_DIRECTION_BUY if side is Side.BUY else _ORDER_DIRECTION_SELL


def direction_to_side(direction: int) -> Side:
    return Side.BUY if direction == _ORDER_DIRECTION_BUY else Side.SELL


# --- order status (T-Invest OrderExecutionReportStatus int -> OrderState) --
# Stable integer values from the T-Invest API enum.
_STATUS_TO_STATE: dict[int, OrderState] = {
    0: OrderState.UNKNOWN,  # EXECUTION_REPORT_STATUS_UNSPECIFIED
    1: OrderState.FILLED,  # FILL
    2: OrderState.REJECTED,  # REJECTED
    3: OrderState.CANCELLED,  # CANCELLED
    4: OrderState.ACKNOWLEDGED,  # NEW (accepted by the exchange)
    5: OrderState.PARTIALLY_FILLED,  # PARTIALLYFILL
}


def order_status_to_state(status: int) -> OrderState:
    return _STATUS_TO_STATE.get(status, OrderState.UNKNOWN)
