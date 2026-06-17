"""Implied-volatility surface (R14).

Builds a per-expiry smile and a term structure from option quotes, with quality
filtering and interpolation. On the first pass this is robust interpolation (no
heavy calibration); the :class:`SmileModel` protocol leaves room to add SVI /
SABR / arbitrage-free smoothing later without touching callers.
"""

from app.volatility.surface import (
    InterpolatedSmile,
    Smile,
    SmileModel,
    SmilePoint,
    VolatilitySurface,
    build_surface,
)

__all__ = [
    "InterpolatedSmile",
    "Smile",
    "SmileModel",
    "SmilePoint",
    "VolatilitySurface",
    "build_surface",
]
