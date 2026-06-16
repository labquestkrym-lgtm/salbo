"""Volatility estimators and the buy-vol signal (R18)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.strategies.vol_forecast import (
    ForecastConfig,
    RealizedVolForecastModel,
    VolatilityForecast,
    atr,
    ewma_vol,
    garman_klass_vol,
    historical_vol,
    parkinson_vol,
    realized_vol,
)


def _gbm_path(sigma: float, n: int = 504, seed: int = 1) -> list[float]:
    rng = np.random.default_rng(seed)
    dt = 1.0 / 252.0
    shocks = rng.standard_normal(n) * sigma * math.sqrt(dt) - 0.5 * sigma**2 * dt
    return list(100.0 * np.exp(np.cumsum(shocks)))


def test_historical_vol_recovers_known_sigma() -> None:
    closes = _gbm_path(0.25, n=2000, seed=42)
    assert historical_vol(closes) == pytest.approx(0.25, abs=0.03)


def test_realized_and_ewma_are_positive_and_reasonable() -> None:
    closes = _gbm_path(0.30, n=1000)
    assert 0.1 < realized_vol(closes) < 0.6
    assert 0.1 < ewma_vol(closes) < 0.6


def test_parkinson_and_garman_klass_on_ohlc() -> None:
    opens = [100, 101, 102, 101, 103]
    highs = [102, 103, 104, 103, 105]
    lows = [99, 100, 101, 100, 102]
    closes = [101, 102, 101, 103, 104]
    assert parkinson_vol(highs, lows) > 0
    assert garman_klass_vol(opens, highs, lows, closes) >= 0


def test_atr_is_average_true_range() -> None:
    highs = [10, 11, 12, 11.5]
    lows = [9, 9.5, 10, 10.5]
    closes = [9.5, 10.5, 11, 11]
    assert atr(highs, lows, closes, period=3) > 0


def test_buy_signal_when_forecast_exceeds_iv_plus_buffers() -> None:
    cfg = ForecastConfig(transaction_cost_buffer=0.02, model_uncertainty_buffer=0.02)
    fc = VolatilityForecast(expected_rv=0.40)
    # Forecast 40% vs implied 20%: edge = 0.40 - 0.20 - 0.04 = 0.16 > 0.
    assert fc.is_buy_signal(implied_vol=0.20, config=cfg)
    assert fc.edge_over(implied_vol=0.20, config=cfg) == pytest.approx(0.16)
    # Implied 60% -> no edge.
    assert not fc.is_buy_signal(implied_vol=0.60, config=cfg)


def test_model_blends_estimators_into_positive_forecast() -> None:
    cfg = ForecastConfig()
    model = RealizedVolForecastModel(cfg)
    n = 600
    closes = _gbm_path(0.35, n=n, seed=11)
    # Build plausible OHLC around closes so range-based estimators are non-degenerate.
    opens = [closes[0], *closes[:-1]]
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    fc = model.forecast(opens, highs, lows, closes)
    assert fc.expected_rv > 0.10
    assert set(fc.components) == {"historical", "ewma", "parkinson", "garman_klass", "realized"}


def test_insufficient_data_raises() -> None:
    with pytest.raises(ValueError):
        historical_vol([100.0])
