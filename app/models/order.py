"""Order and fill models.

Minimal but real; the full OMS state machine (transitions, idempotency,
reconciliation) is built on these in Stage 6. ``client_order_id`` is the
idempotency key — the broker layer must never submit the same one twice.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import OrderState, OrderType, Side, TimeInForce


class OrderRequest(BaseModel):
    """An intent to trade, before it is sent to a broker."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str = Field(min_length=1, description="Idempotency key")
    instrument_symbol: str
    side: Side
    quantity: Decimal = Field(gt=0)
    order_type: OrderType
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY

    @model_validator(mode="after")
    def _validate_limit(self) -> OrderRequest:
        needs_limit = self.order_type in (
            OrderType.LIMIT,
            OrderType.MARKETABLE_LIMIT,
            OrderType.PASSIVE_LIMIT,
        )
        if needs_limit and self.limit_price is None:
            raise ValueError(f"{self.order_type} requires a limit_price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET order must not carry a limit_price")
        return self


class Fill(BaseModel):
    """A single (possibly partial) execution."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    instrument_symbol: str
    side: Side
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    commission: Decimal = Field(default=Decimal("0"), ge=0)
    timestamp: datetime
    broker_fill_id: str | None = None


class Order(BaseModel):
    """Live view of an order including fill progress."""

    model_config = ConfigDict(frozen=False)

    request: OrderRequest
    state: OrderState = OrderState.CREATED
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal = Decimal("0")
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def remaining_quantity(self) -> Decimal:
        return self.request.quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )

    @property
    def is_active(self) -> bool:
        return not self.is_terminal and self.state is not OrderState.CREATED
