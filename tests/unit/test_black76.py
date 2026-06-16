"""Black-76 (options on futures): reference, parity, equivalence to BSM."""

from __future__ import annotations

import math

import pytest

from app.core.enums import OptionType
from app.pricing.models import black76, bsm


def test_atm_forward_call_equals_put() -> None:
    # At the forward, call and put have equal value under Black-76.
    call = black76(
        forward=100, strike=100, t=1.0, rate=0.05, sigma=0.2, option_type=OptionType.CALL
    )
    put = black76(forward=100, strike=100, t=1.0, rate=0.05, sigma=0.2, option_type=OptionType.PUT)
    assert call.price == pytest.approx(put.price, abs=1e-12)


def test_reference_value() -> None:
    # e^{-rT}[F N(d1) - K N(d2)] with d1=0.1, d2=-0.1.
    call = black76(
        forward=100, strike=100, t=1.0, rate=0.05, sigma=0.2, option_type=OptionType.CALL
    )
    expected = math.exp(-0.05) * 100.0 * (0.5398278 - 0.4601722)
    assert call.price == pytest.approx(expected, abs=1e-4)


def test_put_call_parity_black76() -> None:
    # C - P = e^{-rT}(F - K)
    f, k, t, r, sigma = 105.0, 100.0, 0.5, 0.03, 0.25
    call = black76(forward=f, strike=k, t=t, rate=r, sigma=sigma, option_type=OptionType.CALL)
    put = black76(forward=f, strike=k, t=t, rate=r, sigma=sigma, option_type=OptionType.PUT)
    assert (call.price - put.price) == pytest.approx(math.exp(-r * t) * (f - k), abs=1e-9)


def test_black76_equals_bsm_with_dividend_equal_rate() -> None:
    # BSM with q = r reduces to Black-76 (carry b = 0).
    s, k, t, r, sigma = 100.0, 95.0, 0.7, 0.06, 0.3
    b76 = black76(forward=s, strike=k, t=t, rate=r, sigma=sigma, option_type=OptionType.CALL)
    bsm_eq = bsm(
        spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=r, option_type=OptionType.CALL
    )
    assert b76.price == pytest.approx(bsm_eq.price, abs=1e-10)
    assert b76.delta == pytest.approx(bsm_eq.delta, abs=1e-10)
    assert b76.gamma == pytest.approx(bsm_eq.gamma, abs=1e-10)


def test_black76_rho_is_negative_t_times_price() -> None:
    call = black76(
        forward=100, strike=100, t=2.0, rate=0.05, sigma=0.2, option_type=OptionType.CALL
    )
    assert call.rho == pytest.approx(-2.0 * call.price, abs=1e-10)
