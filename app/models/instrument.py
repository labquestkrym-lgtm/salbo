"""Instrument and contract-specification models.

A single :class:`Instrument` represents any tradable (equity, future, option).
Option/future-specific fields are optional and enforced by validators so the
resolver and pricing layers can rely on consistency. Multipliers and lot sizes
are **never** assumed equal across instruments (see R7/R13).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    AssetClass,
    OptionStyle,
    OptionType,
    PricingModel,
    SettlementType,
)


class ContractSpec(BaseModel):
    """Exchange contract specification. All monetary values are ``Decimal``."""

    model_config = ConfigDict(frozen=True)

    tick_size: Decimal = Field(gt=0, description="Minimum price increment")
    tick_value: Decimal = Field(gt=0, description="Cash value of one tick per contract")
    lot_size: int = Field(gt=0, description="Underlying units per contract/lot")
    multiplier: Decimal = Field(gt=0, description="Underlying units per contract (delta/hedge)")
    currency: str = Field(min_length=1)
    settlement: SettlementType = SettlementType.CASH
    # Factor by which the instrument's QUOTED price exceeds the per-underlying-unit
    # price. 1 for equities/options and for futures quoted per underlying unit;
    # >1 for FORTS futures quoted per contract (e.g. SBER future = share x100).
    # The per-unit price used by the pricing kernel is quote / quote_scale.
    quote_scale: Decimal = Field(default=Decimal(1), gt=0)

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Quantize a price to the nearest valid tick."""
        steps = (price / self.tick_size).quantize(Decimal("1"))
        return steps * self.tick_size


class Instrument(BaseModel):
    """A tradable instrument. Equity/future/option share this shape; the
    option/future fields below are validated for consistency."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, description="Unique broker symbol")
    underlying_symbol: str = Field(min_length=1, description="Code of the underlying asset")
    asset_class: AssetClass
    spec: ContractSpec
    pricing_model: PricingModel | None = None

    # Derivative fields
    expiry: date | None = None
    option_type: OptionType | None = None
    strike: Decimal | None = None
    option_style: OptionStyle | None = None

    @model_validator(mode="after")
    def _validate_consistency(self) -> Instrument:
        if self.asset_class is AssetClass.OPTION:
            missing = [
                name
                for name, value in (
                    ("expiry", self.expiry),
                    ("option_type", self.option_type),
                    ("strike", self.strike),
                    ("pricing_model", self.pricing_model),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"option instrument missing fields: {', '.join(missing)}")
            if self.strike is not None and self.strike <= 0:
                raise ValueError("strike must be positive")
        elif self.asset_class is AssetClass.FUTURE:
            if self.expiry is None:
                raise ValueError("future instrument requires an expiry")
            if self.option_type is not None or self.strike is not None:
                raise ValueError("future instrument must not have option_type/strike")
        else:  # equity / index
            if self.option_type is not None or self.strike is not None:
                raise ValueError(f"{self.asset_class} instrument must not have option fields")
        return self

    @property
    def is_option(self) -> bool:
        return self.asset_class is AssetClass.OPTION

    @property
    def is_future(self) -> bool:
        return self.asset_class is AssetClass.FUTURE
