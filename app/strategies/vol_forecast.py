"""Volatility forecasting (R18).

Estimators over OHLC bars (close-to-close, EWMA, Parkinson, Garman-Klass,
realized) plus ATR, combined into an expected realized volatility. The buy-vol
signal is::

    expected_rv > implied_vol + transaction_cost_buffer + model_uncertainty_buffer

Thresholds/weights come from :class:`ForecastConfig`. This is a *forecast*, not a
guarantee — a positive edge does not promise profit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

_TRADING_DAYS = 252.0


def _log_returns(closes: Sequence[float]) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    if arr.size < 2:
        raise ValueError("need at least 2 closes for returns")
    return np.diff(np.log(arr))


def historical_vol(closes: Sequence[float], *, periods_per_year: float = _TRADING_DAYS) -> float:
    r = _log_returns(closes)
    return float(np.std(r, ddof=1) * np.sqrt(periods_per_year))


def ewma_vol(
    closes: Sequence[float], *, lambda_: float = 0.94, periods_per_year: float = _TRADING_DAYS
) -> float:
    r = _log_returns(closes)
    weights = (1 - lambda_) * lambda_ ** np.arange(r.size - 1, -1, -1)
    var = float(np.sum(weights * r**2) / np.sum(weights))
    return float(np.sqrt(var * periods_per_year))


def parkinson_vol(
    highs: Sequence[float], lows: Sequence[float], *, periods_per_year: float = _TRADING_DAYS
) -> float:
    h = np.asarray(highs, dtype=float)
    low = np.asarray(lows, dtype=float)
    if h.size == 0 or h.size != low.size:
        raise ValueError("highs and lows must be equal, non-empty")
    factor = 1.0 / (4.0 * np.log(2.0))
    var = float(np.mean(factor * np.log(h / low) ** 2))
    return float(np.sqrt(var * periods_per_year))


def garman_klass_vol(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    periods_per_year: float = _TRADING_DAYS,
) -> float:
    o, h, low, c = (np.asarray(x, dtype=float) for x in (opens, highs, lows, closes))
    if not (o.size == h.size == low.size == c.size) or o.size == 0:
        raise ValueError("OHLC arrays must be equal, non-empty")
    hl = 0.5 * np.log(h / low) ** 2
    co = (2.0 * np.log(2.0) - 1.0) * np.log(c / o) ** 2
    var = float(np.mean(hl - co))
    return float(np.sqrt(max(var, 0.0) * periods_per_year))


def realized_vol(closes: Sequence[float], *, periods_per_year: float = _TRADING_DAYS) -> float:
    r = _log_returns(closes)
    return float(np.sqrt(np.mean(r**2) * periods_per_year))


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], *, period: int = 14
) -> float:
    h, low, c = (np.asarray(x, dtype=float) for x in (highs, lows, closes))
    if h.size < 2:
        raise ValueError("need at least 2 bars for ATR")
    prev_close = c[:-1]
    tr = np.maximum.reduce(
        [h[1:] - low[1:], np.abs(h[1:] - prev_close), np.abs(low[1:] - prev_close)]
    )
    window = min(period, tr.size)
    return float(np.mean(tr[-window:]))


@dataclass(frozen=True, slots=True)
class ForecastConfig:
    # Weights for combining estimators into expected RV (normalized internally).
    weight_historical: float = 0.25
    weight_ewma: float = 0.35
    weight_parkinson: float = 0.20
    weight_garman_klass: float = 0.20
    transaction_cost_buffer: float = 0.02  # in vol points (absolute, e.g. 0.02 = 2 vol pts)
    model_uncertainty_buffer: float = 0.02
    periods_per_year: float = _TRADING_DAYS


@dataclass(frozen=True, slots=True)
class VolatilityForecast:
    expected_rv: float
    components: dict[str, float] = field(default_factory=dict)

    def edge_over(self, implied_vol: float, config: ForecastConfig) -> float:
        """Forecast edge net of cost/uncertainty buffers (in vol points)."""
        return (
            self.expected_rv
            - implied_vol
            - (config.transaction_cost_buffer + config.model_uncertainty_buffer)
        )

    def is_buy_signal(self, implied_vol: float, config: ForecastConfig) -> bool:
        return self.edge_over(implied_vol, config) > 0.0


class VolatilityForecastModel:
    """Interface: produce an expected realized volatility from OHLC bars."""

    def forecast(
        self,
        opens: Sequence[float],
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
    ) -> VolatilityForecast:
        raise NotImplementedError


class RealizedVolForecastModel(VolatilityForecastModel):
    """Weighted blend of estimators. Replaceable with a richer model later."""

    def __init__(self, config: ForecastConfig | None = None) -> None:
        self._cfg = config or ForecastConfig()

    def forecast(
        self,
        opens: Sequence[float],
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
    ) -> VolatilityForecast:
        cfg = self._cfg
        ppy = cfg.periods_per_year
        components = {
            "historical": historical_vol(closes, periods_per_year=ppy),
            "ewma": ewma_vol(closes, periods_per_year=ppy),
            "parkinson": parkinson_vol(highs, lows, periods_per_year=ppy),
            "garman_klass": garman_klass_vol(opens, highs, lows, closes, periods_per_year=ppy),
            "realized": realized_vol(closes, periods_per_year=ppy),
        }
        weights = {
            "historical": cfg.weight_historical,
            "ewma": cfg.weight_ewma,
            "parkinson": cfg.weight_parkinson,
            "garman_klass": cfg.weight_garman_klass,
        }
        total_w = sum(weights.values())
        expected = sum(components[k] * w for k, w in weights.items()) / total_w
        return VolatilityForecast(expected_rv=expected, components=components)
