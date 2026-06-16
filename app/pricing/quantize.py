"""The float <-> Decimal boundary (ADR-0001).

The pricing kernel works in ``float``; everything outward of it works in
``Decimal`` quantized to an instrument's tick. These helpers are the only
sanctioned place to cross between the two.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from app.models.instrument import ContractSpec


def price_to_decimal(
    value: float, spec: ContractSpec, *, rounding: str = ROUND_HALF_EVEN
) -> Decimal:
    """Convert a model price (float) to a tick-aligned ``Decimal``."""
    raw = Decimal(str(value))
    steps = (raw / spec.tick_size).quantize(Decimal("1"), rounding=rounding)
    return steps * spec.tick_size


def floor_to_tick(value: Decimal, spec: ContractSpec) -> Decimal:
    steps = (value / spec.tick_size).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    return steps * spec.tick_size


def ceil_to_tick(value: Decimal, spec: ContractSpec) -> Decimal:
    steps = (value / spec.tick_size).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return steps * spec.tick_size
