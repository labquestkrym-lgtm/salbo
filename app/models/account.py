"""Account state models: positions, cash, margin."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Position(BaseModel):
    """A signed position in one instrument.

    ``quantity`` is in contracts/lots (signed: long > 0, short < 0).
    ``average_price`` is the volume-weighted entry price per unit.
    """

    model_config = ConfigDict(frozen=True)

    instrument_symbol: str
    quantity: Decimal
    average_price: Decimal = Decimal("0")

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def sign(self) -> int:
        if self.quantity > 0:
            return 1
        if self.quantity < 0:
            return -1
        return 0


class CashBalance(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: str
    cash: Decimal


class MarginInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: str
    used_margin: Decimal = Field(ge=0)
    available_margin: Decimal
    maintenance_margin: Decimal = Field(default=Decimal("0"), ge=0)

    @property
    def utilization(self) -> Decimal:
        """Used / (used + available); 0 when no capital is deployed."""
        denom = self.used_margin + self.available_margin
        if denom <= 0:
            return Decimal("0")
        return self.used_margin / denom
