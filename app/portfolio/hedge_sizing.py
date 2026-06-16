"""Futures hedge sizing (R16).

Computes how many futures contracts to trade to move portfolio delta toward a
target, with explicit, dimensionally-checked arithmetic and several rounding
modes. This module decides the *size only*; whether to act (cost/benefit,
cooldown, turnover limits) is the Hedge Engine's job (Stage 7).

Sign convention: a positive result means BUY that many futures contracts; a
negative result means SELL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.core.enums import RoundingMode


@dataclass(frozen=True, slots=True)
class HedgeSizing:
    raw_contracts: float  # unrounded contracts required
    contracts: int  # rounded, signed (buy>0 / sell<0)
    residual_delta_units: float  # delta left after trading ``contracts``


def compute_hedge_contracts(
    *,
    current_delta_units: float,
    target_delta_units: float,
    future_multiplier: float,
    rounding_mode: RoundingMode = RoundingMode.NEAREST,
    future_delta_per_unit: float = 1.0,
) -> HedgeSizing:
    """Contracts to trade so that delta moves from current toward target.

    ``future_delta_per_unit`` is dFuture/dSpot (≈1; e^{(r-q)·tau} for a forward).
    One contract changes portfolio delta by
    ``future_multiplier · future_delta_per_unit`` underlying units.
    """
    if future_multiplier <= 0:
        raise ValueError("future_multiplier must be positive")
    if future_delta_per_unit == 0:
        raise ValueError("future_delta_per_unit must be non-zero")

    delta_per_contract = future_multiplier * future_delta_per_unit
    # We need to add (target - current) units via the futures leg.
    required_units = target_delta_units - current_delta_units
    raw = required_units / delta_per_contract

    contracts = _round(raw, rounding_mode)
    residual = current_delta_units + contracts * delta_per_contract - target_delta_units
    return HedgeSizing(raw_contracts=raw, contracts=contracts, residual_delta_units=residual)


def _round(value: float, mode: RoundingMode) -> int:
    match mode:
        case RoundingMode.NEAREST:
            # round half to even to avoid upward bias
            floor = math.floor(value)
            frac = value - floor
            if frac < 0.5:
                return floor
            if frac > 0.5:
                return floor + 1
            return floor if floor % 2 == 0 else floor + 1
        case RoundingMode.FLOOR:
            return math.floor(value)
        case RoundingMode.CEIL:
            return math.ceil(value)
        case RoundingMode.CONSERVATIVE:
            # round away from zero -> hedge at least as much (less residual risk)
            return math.ceil(value) if value > 0 else math.floor(value)
        case RoundingMode.MINIMAL_TURNOVER:
            # round toward zero -> trade as little as possible
            return math.trunc(value)
