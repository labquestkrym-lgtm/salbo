"""Map T-Invest instrument objects to our domain ``Instrument``/``ContractSpec``.

Duck-typed against the SDK's ``Future``/``Option``/``Share`` shapes so the field
contract we depend on is explicit and unit-testable with lightweight fakes. If a
future SDK version renames a field, the failing test points exactly here.

Assumed fields (validate against your ``tinkoff-investments`` version):
* common: ``figi``, ``lot``, ``currency``, ``min_price_increment`` (Quotation)
* derivatives: ``min_price_increment_amount`` (MoneyValue), ``expiration_date``
  (aware datetime), ``basic_asset`` (underlying ticker)
* option: ``strike_price`` (MoneyValue), ``direction`` (OptionDirection int)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.brokers.tinkoff.conversions import quotation_obj_to_decimal
from app.core.enums import AssetClass, OptionStyle, OptionType, PricingModel
from app.core.exceptions import InstrumentResolutionError
from app.models import ContractSpec, Instrument

# T-Invest OptionDirection int enum (validate against SDK): 1=PUT, 2=CALL.
_OPTION_DIRECTION: dict[int, OptionType] = {1: OptionType.PUT, 2: OptionType.CALL}


def _instrument_id(obj: Any) -> str:
    """The id used as our ``Instrument.symbol`` (and for quotes/stream/orders).

    Futures carry a ``figi``; FORTS options do NOT — they only have a ``uid``
    (verified on the live API). Both are accepted as ``instrument_id`` by the
    market-data and orders services, so we prefer ``figi`` and fall back to
    ``uid``."""
    ident = getattr(obj, "figi", None) or getattr(obj, "uid", None)
    if not ident:
        raise InstrumentResolutionError("instrument has neither figi nor uid")
    return str(ident)


def contract_spec_from(obj: Any) -> ContractSpec:
    tick_size = quotation_obj_to_decimal(obj.min_price_increment)
    if tick_size <= 0:
        raise InstrumentResolutionError("min_price_increment must be positive")
    amount = getattr(obj, "min_price_increment_amount", None)
    if amount is not None and getattr(amount, "units", None) is not None:
        tick_value = quotation_obj_to_decimal(amount)
        multiplier = tick_value / tick_size
    else:
        tick_value = tick_size
        multiplier = Decimal("1")
    return ContractSpec(
        tick_size=tick_size,
        tick_value=tick_value,
        lot_size=int(obj.lot),
        multiplier=multiplier,
        currency=str(obj.currency).upper(),
    )


def future_to_instrument(fut: Any) -> Instrument:
    return Instrument(
        symbol=_instrument_id(fut),
        underlying_symbol=str(fut.basic_asset),
        asset_class=AssetClass.FUTURE,
        spec=contract_spec_from(fut),
        expiry=fut.expiration_date.date(),
    )


def option_to_instrument(opt: Any) -> Instrument:
    option_type = _OPTION_DIRECTION.get(int(opt.direction))
    if option_type is None:
        raise InstrumentResolutionError(f"unknown option direction {opt.direction}")
    return Instrument(
        symbol=_instrument_id(opt),
        underlying_symbol=str(opt.basic_asset),
        asset_class=AssetClass.OPTION,
        spec=contract_spec_from(opt),
        expiry=opt.expiration_date.date(),
        option_type=option_type,
        strike=quotation_obj_to_decimal(opt.strike_price),
        option_style=OptionStyle.EUROPEAN,
        # Options on futures -> Black-76 (ADR / STRATEGY.md).
        pricing_model=PricingModel.BLACK_76,
    )
