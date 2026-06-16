"""Closed-form option models in the generalized Black-Scholes framework.

A single core (:func:`_generalized`) is parameterized by the cost-of-carry ``b``
and an interest-rate-sensitivity convention, giving:

* **Black-Scholes-Merton** (options on spot with dividend yield ``q``):
  ``b = r - q``; rho holds ``q`` fixed.
* **Black-76** (options on a future/forward ``F``): ``b = 0`` and the underlying
  is ``F``; rho is taken with ``F`` held fixed (rho = -T * price).

References: Haug, *The Complete Guide to Option Pricing Formulas*.
"""

from __future__ import annotations

import math
from typing import Literal

from app.core.enums import OptionType
from app.core.exceptions import PricingError
from app.pricing.greeks import Greeks

# Standard normal pdf/cdf via math (scalar, fast, no NumPy dependency here).
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)


def _pdf(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


RhoConvention = Literal["spot", "futures"]


def _validate_inputs(s: float, k: float, t: float, sigma: float) -> None:
    if s <= 0.0:
        raise PricingError(f"underlying must be positive, got {s}")
    if k <= 0.0:
        raise PricingError(f"strike must be positive, got {k}")
    if t <= 0.0:
        raise PricingError(f"time to expiry must be positive, got {t}")
    if sigma <= 0.0:
        raise PricingError(f"volatility must be positive, got {sigma}")


def _price_only(
    *,
    s: float,
    k: float,
    t: float,
    r: float,
    sigma: float,
    b: float,
    option_type: OptionType,
) -> float:
    """Price for one unit of underlying (no Greeks) — used by the IV solver."""
    _validate_inputs(s, k, t, sigma)
    vol_sqrt_t = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (b + 0.5 * sigma * sigma) * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    dq = math.exp((b - r) * t)
    dr = math.exp(-r * t)
    if option_type is OptionType.CALL:
        return s * dq * _cdf(d1) - k * dr * _cdf(d2)
    return k * dr * _cdf(-d2) - s * dq * _cdf(-d1)


def _generalized(
    *,
    s: float,
    k: float,
    t: float,
    r: float,
    sigma: float,
    b: float,
    option_type: OptionType,
    rho_convention: RhoConvention,
) -> Greeks:
    """Generalized Black-Scholes price and Greeks for one unit of underlying.

    ``s`` is the spot for BSM or the forward/future ``F`` for Black-76.
    """
    _validate_inputs(s, k, t, sigma)

    sqrt_t = math.sqrt(t)
    vol_sqrt_t = sigma * sqrt_t
    d1 = (math.log(s / k) + (b + 0.5 * sigma * sigma) * t) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t

    dq = math.exp((b - r) * t)  # carry discount applied to the underlying
    dr = math.exp(-r * t)  # risk-free discount applied to the strike
    nd1 = _pdf(d1)

    is_call = option_type is OptionType.CALL
    n_d1 = _cdf(d1)
    n_d2 = _cdf(d2)
    n_neg_d1 = _cdf(-d1)
    n_neg_d2 = _cdf(-d2)

    if is_call:
        price = s * dq * n_d1 - k * dr * n_d2
        delta = dq * n_d1
    else:
        price = k * dr * n_neg_d2 - s * dq * n_neg_d1
        delta = -dq * n_neg_d1

    # Second order — identical for calls and puts.
    gamma = dq * nd1 / (s * vol_sqrt_t)
    vega = s * dq * nd1 * sqrt_t  # dV/dsigma (per 1.0 vol)

    # Theta = dV/dt (calendar) = -dV/dT. Negative for long premium.
    common_theta = -(s * dq * nd1 * sigma) / (2.0 * sqrt_t)
    if is_call:
        theta = common_theta - (b - r) * s * dq * n_d1 - r * k * dr * n_d2
    else:
        theta = common_theta + (b - r) * s * dq * n_neg_d1 + r * k * dr * n_neg_d2

    # Rho — convention dependent.
    if rho_convention == "spot":
        rho = (k * t * dr * n_d2) if is_call else (-k * t * dr * n_neg_d2)
    else:  # futures: F held fixed when bumping r
        rho = -t * price

    # Higher-order (call/put differ only through the N(.) term).
    vanna = -dq * nd1 * d2 / sigma  # d(delta)/dsigma
    volga = vega * d1 * d2 / sigma  # d(vega)/dsigma
    dd1_dT = b / vol_sqrt_t - d2 / (2.0 * t)  # d(d1)/dT
    if is_call:
        charm = dq * ((b - r) * n_d1 + nd1 * dd1_dT)
    else:
        charm = dq * (-(b - r) * n_neg_d1 + nd1 * dd1_dT)
    speed = -gamma / s * (1.0 + d1 / vol_sqrt_t)  # d(gamma)/dS

    return Greeks(
        price=price,
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        rho=rho,
        vanna=vanna,
        volga=volga,
        charm=charm,
        speed=speed,
    )


def bsm(
    *,
    spot: float,
    strike: float,
    t: float,
    rate: float,
    sigma: float,
    option_type: OptionType,
    dividend_yield: float = 0.0,
) -> Greeks:
    """Black-Scholes-Merton price and Greeks for an option on spot.

    ``rate`` and ``dividend_yield`` are continuously-compounded annualized; ``t``
    is in years.
    """
    return _generalized(
        s=spot,
        k=strike,
        t=t,
        r=rate,
        sigma=sigma,
        b=rate - dividend_yield,
        option_type=option_type,
        rho_convention="spot",
    )


def black76(
    *,
    forward: float,
    strike: float,
    t: float,
    rate: float,
    sigma: float,
    option_type: OptionType,
) -> Greeks:
    """Black-76 price and Greeks for an option on a future/forward."""
    return _generalized(
        s=forward,
        k=strike,
        t=t,
        r=rate,
        sigma=sigma,
        b=0.0,
        option_type=option_type,
        rho_convention="futures",
    )
