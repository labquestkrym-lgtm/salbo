"""Black-Scholes-Merton: reference values, put-call parity, sanity."""

from __future__ import annotations

import math

import pytest

from app.core.enums import OptionType
from app.core.exceptions import PricingError
from app.pricing.models import bsm


def test_atm_reference_values() -> None:
    # Textbook case: S=K=100, T=1, r=5%, sigma=20%, no dividend.
    call = bsm(spot=100, strike=100, t=1.0, rate=0.05, sigma=0.20, option_type=OptionType.CALL)
    put = bsm(spot=100, strike=100, t=1.0, rate=0.05, sigma=0.20, option_type=OptionType.PUT)
    assert call.price == pytest.approx(10.4506, abs=1e-3)
    assert put.price == pytest.approx(5.5735, abs=1e-3)
    assert call.delta == pytest.approx(0.6368, abs=1e-3)


def test_put_call_parity() -> None:
    # C - P = S e^{-qT} - K e^{-rT}
    s, k, t, r, q, sigma = 105.0, 100.0, 0.5, 0.03, 0.01, 0.25
    call = bsm(
        spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=OptionType.CALL
    )
    put = bsm(
        spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=OptionType.PUT
    )
    parity = s * math.exp(-q * t) - k * math.exp(-r * t)
    assert (call.price - put.price) == pytest.approx(parity, abs=1e-9)


def test_call_put_delta_relationship() -> None:
    # delta_call - delta_put = e^{-qT}
    s, k, t, r, q, sigma = 100.0, 110.0, 0.75, 0.04, 0.02, 0.30
    call = bsm(
        spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=OptionType.CALL
    )
    put = bsm(
        spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=OptionType.PUT
    )
    assert (call.delta - put.delta) == pytest.approx(math.exp(-q * t), abs=1e-9)


def test_gamma_and_vega_shared_between_call_and_put() -> None:
    kw = {"spot": 100, "strike": 95, "t": 0.4, "rate": 0.05, "sigma": 0.22, "dividend_yield": 0.0}
    call = bsm(option_type=OptionType.CALL, **kw)
    put = bsm(option_type=OptionType.PUT, **kw)
    assert call.gamma == pytest.approx(put.gamma, abs=1e-12)
    assert call.vega == pytest.approx(put.vega, abs=1e-12)


def test_long_option_theta_is_negative() -> None:
    call = bsm(spot=100, strike=100, t=0.5, rate=0.05, sigma=0.2, option_type=OptionType.CALL)
    assert call.theta < 0.0
    assert call.theta_per_day == pytest.approx(call.theta / 365.0)


@pytest.mark.parametrize("bad", [{"spot": -1}, {"strike": 0}, {"t": 0}, {"sigma": -0.1}])
def test_invalid_inputs_raise(bad: dict[str, float]) -> None:
    kw: dict[str, float] = {"spot": 100, "strike": 100, "t": 1.0, "rate": 0.05, "sigma": 0.2}
    kw.update(bad)
    with pytest.raises(PricingError):
        bsm(option_type=OptionType.CALL, **kw)
