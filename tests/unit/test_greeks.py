"""Validate every analytic Greek against central finite differences.

Each Greek is defined operationally as a specific partial derivative (see
``app.pricing.greeks``). We confirm the closed-form value matches a central
finite-difference estimate of exactly that derivative, across a wide,
randomized parameter grid (property-based via Hypothesis).
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.enums import OptionType
from app.pricing.models import bsm


def _price(s: float, k: float, t: float, r: float, sigma: float, q: float, ot: OptionType) -> float:
    return bsm(spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=ot).price


def _delta(s: float, k: float, t: float, r: float, sigma: float, q: float, ot: OptionType) -> float:
    return bsm(spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=ot).delta


def _vega(s: float, k: float, t: float, r: float, sigma: float, q: float, ot: OptionType) -> float:
    return bsm(spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=ot).vega


def _gamma(s: float, k: float, t: float, r: float, sigma: float, q: float, ot: OptionType) -> float:
    return bsm(spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=ot).gamma


def _close(actual: float, expected: float, *, rel: float, abs_: float) -> bool:
    return abs(actual - expected) <= max(abs_, rel * abs(expected))


# Parameter grid kept away from degenerate deep-OTM/near-expiry corners where
# Greeks vanish and finite differences become numerically meaningless.
_params = st.tuples(
    st.floats(min_value=70, max_value=130),  # spot
    st.floats(min_value=70, max_value=130),  # strike
    st.floats(min_value=0.1, max_value=2.0),  # T
    st.floats(min_value=0.0, max_value=0.08),  # r
    st.floats(min_value=0.10, max_value=0.80),  # sigma
    st.floats(min_value=0.0, max_value=0.04),  # q
    st.sampled_from([OptionType.CALL, OptionType.PUT]),
)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_params)
def test_first_and_higher_order_greeks_match_finite_difference(
    p: tuple[float, float, float, float, float, float, OptionType],
) -> None:
    s, k, t, r, sigma, q, ot = p
    g = bsm(spot=s, strike=k, t=t, rate=r, sigma=sigma, dividend_yield=q, option_type=ot)

    hs = s * 1e-4
    hv = 1e-4
    ht = 1e-4
    hr = 1e-5

    # delta = dV/dS
    fd_delta = (_price(s + hs, k, t, r, sigma, q, ot) - _price(s - hs, k, t, r, sigma, q, ot)) / (
        2 * hs
    )
    assert _close(g.delta, fd_delta, rel=1e-4, abs_=1e-6)

    # gamma = d2V/dS2
    fd_gamma = (
        _price(s + hs, k, t, r, sigma, q, ot) - 2 * g.price + _price(s - hs, k, t, r, sigma, q, ot)
    ) / (hs * hs)
    assert _close(g.gamma, fd_gamma, rel=1e-3, abs_=1e-5)

    # vega = dV/dsigma
    fd_vega = (_price(s, k, t, r, sigma + hv, q, ot) - _price(s, k, t, r, sigma - hv, q, ot)) / (
        2 * hv
    )
    assert _close(g.vega, fd_vega, rel=1e-4, abs_=1e-4)

    # theta = dV/dt = -(dV/dT)
    fd_theta = -(_price(s, k, t + ht, r, sigma, q, ot) - _price(s, k, t - ht, r, sigma, q, ot)) / (
        2 * ht
    )
    assert _close(g.theta, fd_theta, rel=1e-3, abs_=1e-3)

    # rho = dV/dr (q held fixed -> carry b = r - q moves with r: "spot" convention)
    fd_rho = (_price(s, k, t, r + hr, sigma, q, ot) - _price(s, k, t, r - hr, sigma, q, ot)) / (
        2 * hr
    )
    assert _close(g.rho, fd_rho, rel=1e-4, abs_=1e-3)

    # vanna = d(delta)/dsigma
    fd_vanna = (_delta(s, k, t, r, sigma + hv, q, ot) - _delta(s, k, t, r, sigma - hv, q, ot)) / (
        2 * hv
    )
    assert _close(g.vanna, fd_vanna, rel=1e-3, abs_=1e-4)

    # volga = d(vega)/dsigma
    fd_volga = (_vega(s, k, t, r, sigma + hv, q, ot) - _vega(s, k, t, r, sigma - hv, q, ot)) / (
        2 * hv
    )
    assert _close(g.volga, fd_volga, rel=1e-3, abs_=1e-3)

    # charm = d(delta)/dT
    fd_charm = (_delta(s, k, t + ht, r, sigma, q, ot) - _delta(s, k, t - ht, r, sigma, q, ot)) / (
        2 * ht
    )
    assert _close(g.charm, fd_charm, rel=1e-3, abs_=1e-4)

    # speed = d(gamma)/dS
    fd_speed = (_gamma(s + hs, k, t, r, sigma, q, ot) - _gamma(s - hs, k, t, r, sigma, q, ot)) / (
        2 * hs
    )
    assert _close(g.speed, fd_speed, rel=1e-2, abs_=1e-6)
