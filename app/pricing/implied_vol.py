"""Robust implied-volatility solver.

Uses Brent's method on ``model_price(sigma) - target = 0`` with explicit guards
that turn market-quality problems into typed, recoverable exceptions rather than
silent NaNs:

* expired / non-positive time -> :class:`ExpiredOptionError`
* non-positive target price    -> :class:`InvalidQuoteError`
* price below intrinsic bound  -> :class:`PriceBelowIntrinsicError`
* price at/above no-arb upper bound, or no bracket within ``sigma_max``
                                -> :class:`NoSolutionError`
"""

from __future__ import annotations

import math
from typing import Literal

from scipy.optimize import brentq

from app.core.enums import OptionType
from app.core.exceptions import (
    ExpiredOptionError,
    InvalidQuoteError,
    NoSolutionError,
    PriceBelowIntrinsicError,
)
from app.pricing.models import _price_only

Model = Literal["bsm", "black76"]

_SIGMA_MIN = 1e-6
_SIGMA_MAX = 50.0  # 5000% annualized — beyond any sane market
_PRICE_TOL = 1e-10


def _carry(model: Model, rate: float, dividend_yield: float) -> float:
    return 0.0 if model == "black76" else rate - dividend_yield


def implied_volatility(
    *,
    price: float,
    underlying: float,
    strike: float,
    t: float,
    rate: float,
    option_type: OptionType,
    model: Model = "bsm",
    dividend_yield: float = 0.0,
    sigma_max: float = _SIGMA_MAX,
) -> float:
    """Return the implied volatility (annualized, absolute) for ``price``.

    ``underlying`` is the spot for BSM or the forward/future for Black-76.
    """
    if t <= 0.0:
        raise ExpiredOptionError(f"time to expiry must be positive, got {t}")
    if price <= 0.0:
        raise InvalidQuoteError(f"option price must be positive, got {price}")
    if underlying <= 0.0 or strike <= 0.0:
        raise InvalidQuoteError("underlying and strike must be positive")

    b = _carry(model, rate, dividend_yield)
    dq = math.exp((b - rate) * t)
    dr = math.exp(-rate * t)

    # No-arbitrage bounds (discounted intrinsic .. forward value).
    if option_type is OptionType.CALL:
        lower = max(0.0, underlying * dq - strike * dr)
        upper = underlying * dq
    else:
        lower = max(0.0, strike * dr - underlying * dq)
        upper = strike * dr

    if price < lower - _PRICE_TOL:
        raise PriceBelowIntrinsicError(f"price {price} below intrinsic lower bound {lower}")
    if price >= upper - _PRICE_TOL:
        raise NoSolutionError(f"price {price} at/above no-arbitrage upper bound {upper}")

    def objective(sigma: float) -> float:
        return (
            _price_only(
                s=underlying,
                k=strike,
                t=t,
                r=rate,
                sigma=sigma,
                b=b,
                option_type=option_type,
            )
            - price
        )

    f_lo = objective(_SIGMA_MIN)
    f_hi = objective(sigma_max)
    if f_lo > 0.0:
        # Even near-zero vol overprices the target: below intrinsic in practice.
        raise PriceBelowIntrinsicError("price not reachable above sigma floor")
    if f_hi < 0.0:
        raise NoSolutionError(f"price {price} not reachable below sigma_max={sigma_max}")

    try:
        sigma: float = brentq(objective, _SIGMA_MIN, sigma_max, xtol=1e-8, maxiter=200)
    except (ValueError, RuntimeError) as exc:  # pragma: no cover - defensive
        raise NoSolutionError(f"root finder failed: {exc}") from exc
    return sigma
