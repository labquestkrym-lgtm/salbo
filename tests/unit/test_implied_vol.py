"""Implied volatility: round-trip recovery + robustness guards."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.enums import OptionType
from app.core.exceptions import (
    ExpiredOptionError,
    InvalidQuoteError,
    NoSolutionError,
    PriceBelowIntrinsicError,
)
from app.pricing.implied_vol import implied_volatility
from app.pricing.models import black76, bsm


@settings(max_examples=150, deadline=None)
@given(
    s=st.floats(min_value=70, max_value=130),
    k=st.floats(min_value=70, max_value=130),
    t=st.floats(min_value=0.05, max_value=2.0),
    r=st.floats(min_value=0.0, max_value=0.08),
    sigma=st.floats(min_value=0.05, max_value=2.0),
    q=st.floats(min_value=0.0, max_value=0.04),
    ot=st.sampled_from([OptionType.CALL, OptionType.PUT]),
)
def test_bsm_round_trip(
    s: float, k: float, t: float, r: float, sigma: float, q: float, ot: OptionType
) -> None:
    g = bsm(spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=ot)
    price = g.price
    # IV is only identifiable where price actually responds to vol. When vega is
    # near the float-precision floor (deep ITM/OTM, near expiry) the time value
    # carries no recoverable vol information — real surfaces drop such points too.
    if price <= 1e-6 or g.vega < 1e-2:
        return
    recovered = implied_volatility(
        price=price,
        underlying=s,
        strike=k,
        t=t,
        rate=r,
        option_type=ot,
        model="bsm",
        dividend_yield=q,
    )
    assert recovered == pytest.approx(sigma, rel=1e-3, abs=1e-3)


def test_black76_round_trip() -> None:
    for sigma in (0.1, 0.25, 0.6):
        price = black76(
            forward=100, strike=105, t=0.5, rate=0.03, sigma=sigma, option_type=OptionType.CALL
        ).price
        recovered = implied_volatility(
            price=price,
            underlying=100,
            strike=105,
            t=0.5,
            rate=0.03,
            option_type=OptionType.CALL,
            model="black76",
        )
        assert recovered == pytest.approx(sigma, rel=1e-4)


def test_expired_option_raises() -> None:
    with pytest.raises(ExpiredOptionError):
        implied_volatility(
            price=5.0, underlying=100, strike=100, t=0.0, rate=0.05, option_type=OptionType.CALL
        )


def test_non_positive_price_raises() -> None:
    with pytest.raises(InvalidQuoteError):
        implied_volatility(
            price=0.0, underlying=100, strike=100, t=1.0, rate=0.05, option_type=OptionType.CALL
        )


def test_price_below_intrinsic_raises() -> None:
    # Deep ITM call must be worth at least discounted intrinsic.
    with pytest.raises(PriceBelowIntrinsicError):
        implied_volatility(
            price=1.0, underlying=150, strike=100, t=1.0, rate=0.0, option_type=OptionType.CALL
        )


def test_price_above_upper_bound_raises() -> None:
    # A call cannot be worth more than the (carry-adjusted) underlying.
    with pytest.raises(NoSolutionError):
        implied_volatility(
            price=120.0, underlying=100, strike=100, t=1.0, rate=0.05, option_type=OptionType.CALL
        )
