"""Hedge engine: delta band, cost gate, cooldown (R19)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.execution import HedgeBandConfig, HedgeEngine

_T = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def test_no_hedge_within_band() -> None:
    eng = HedgeEngine(HedgeBandConfig(base_trigger_units=100.0))
    d = eng.decide(current_delta_units=50.0, future_multiplier=50.0, now=_T)
    assert d.should_hedge is False
    assert "within delta band" in d.reason


def test_hedge_when_outside_band_to_zero() -> None:
    eng = HedgeEngine(HedgeBandConfig(base_trigger_units=100.0, hedge_to_zero=True))
    d = eng.decide(current_delta_units=160.0, future_multiplier=50.0, now=_T)
    assert d.should_hedge is True
    assert d.contracts == -3  # 160/50 ~ 3.2 -> nearest 3, sell
    assert d.target_delta_units == 0.0


def test_cost_gate_blocks_uneconomic_hedge() -> None:
    # Trigger is 0 (always outside band); make cost exceed benefit.
    eng = HedgeEngine(
        HedgeBandConfig(
            base_trigger_units=0.0,
            hedge_to_zero=True,
            min_futures_trade=1,
            min_expected_benefit=Decimal("100"),
        )
    )
    d = eng.decide(
        current_delta_units=60.0,
        future_multiplier=50.0,
        now=_T,
        cost_per_contract=Decimal("1000"),
        spread_cost=Decimal("0.001"),
    )
    assert d.should_hedge is False
    assert "benefit below cost" in d.reason


def test_emergency_bypasses_band_and_cost() -> None:
    eng = HedgeEngine(
        HedgeBandConfig(base_trigger_units=10_000.0, min_expected_benefit=Decimal("1e9"))
    )
    d = eng.decide(
        current_delta_units=60.0,
        future_multiplier=50.0,
        now=_T,
        cost_per_contract=Decimal("1000"),
        emergency=True,
    )
    assert d.should_hedge is True


def test_cooldown_blocks_rapid_rehedge() -> None:
    eng = HedgeEngine(HedgeBandConfig(base_trigger_units=10.0, cooldown_seconds=30.0))
    first = eng.decide(current_delta_units=200.0, future_multiplier=50.0, now=_T)
    assert first.should_hedge is True
    # 5 seconds later, still outside band, but within cooldown.
    second = eng.decide(
        current_delta_units=200.0, future_multiplier=50.0, now=_T + timedelta(seconds=5)
    )
    assert second.should_hedge is False
    assert "cooldown" in second.reason


def test_adaptive_trigger_tightens_with_vol_and_gamma() -> None:
    eng = HedgeEngine(HedgeBandConfig(base_trigger_units=100.0))
    calm = eng.adaptive_trigger(gamma_units=0.0, realized_vol=0.0, spread_cost=Decimal("0"))
    stormy = eng.adaptive_trigger(gamma_units=5000.0, realized_vol=0.8, spread_cost=Decimal("0"))
    assert stormy < calm
