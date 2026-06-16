"""Futures hedge sizing and rounding modes (R16)."""

from __future__ import annotations

from app.core.enums import RoundingMode
from app.portfolio import compute_hedge_contracts


def test_long_delta_is_hedged_by_selling_futures() -> None:
    # Portfolio long 150 underlying units, target flat, future mult 50.
    sizing = compute_hedge_contracts(
        current_delta_units=150.0, target_delta_units=0.0, future_multiplier=50.0
    )
    assert sizing.contracts == -3  # sell 3 futures (3*50 = 150)
    assert sizing.residual_delta_units == 0.0


def test_short_delta_is_hedged_by_buying_futures() -> None:
    sizing = compute_hedge_contracts(
        current_delta_units=-120.0, target_delta_units=0.0, future_multiplier=50.0
    )
    assert sizing.contracts == 2  # 120/50 = 2.4 -> nearest 2


def test_rounding_modes() -> None:
    # 130 units / 50 = 2.6 contracts to sell -> raw = -2.6
    kw = {"current_delta_units": 130.0, "target_delta_units": 0.0, "future_multiplier": 50.0}
    assert compute_hedge_contracts(**kw, rounding_mode=RoundingMode.NEAREST).contracts == -3
    assert compute_hedge_contracts(**kw, rounding_mode=RoundingMode.FLOOR).contracts == -3
    assert compute_hedge_contracts(**kw, rounding_mode=RoundingMode.CEIL).contracts == -2
    # conservative: away from zero -> hedge more -> -3
    assert compute_hedge_contracts(**kw, rounding_mode=RoundingMode.CONSERVATIVE).contracts == -3
    # minimal turnover: toward zero -> -2
    assert (
        compute_hedge_contracts(**kw, rounding_mode=RoundingMode.MINIMAL_TURNOVER).contracts == -2
    )


def test_nearest_half_rounds_to_even() -> None:
    # 125/50 = 2.5 -> half-even -> 2
    sizing = compute_hedge_contracts(
        current_delta_units=125.0,
        target_delta_units=0.0,
        future_multiplier=50.0,
        rounding_mode=RoundingMode.NEAREST,
    )
    assert sizing.contracts == -2


def test_future_delta_per_unit_scales_contracts() -> None:
    # If each future has delta 1.05 per unit, fewer contracts are needed.
    sizing = compute_hedge_contracts(
        current_delta_units=105.0,
        target_delta_units=0.0,
        future_multiplier=50.0,
        future_delta_per_unit=1.05,
    )
    # 105 / (50*1.05) = 2.0 exactly
    assert sizing.contracts == -2
