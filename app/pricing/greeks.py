"""Greeks value object.

All values are *per one unit of the underlying* and in their natural analytic
units:

* ``delta``    — dV/dS (dimensionless per 1.0 of underlying)
* ``gamma``    — d2V/dS2
* ``vega``     — dV/dsigma, per **1.0** of volatility (i.e. per 100 vol points)
* ``theta``    — dV/dt (calendar time), **per year**; negative for long premium
* ``rho``      — dV/dr, per **1.0** (100%) of the rate; model-specific convention
* ``vanna``    — d(delta)/dsigma  (= dVega/dS)
* ``volga``    — d(vega)/dsigma   (vomma)
* ``charm``    — d(delta)/dT      (sensitivity to time-to-expiry)
* ``speed``    — d(gamma)/dS

Convenience properties give the trader-facing per-day / per-1%-vol scalings.
Contract/lot scaling (multiplier, number of contracts) is applied by the
portfolio layer, never here.
"""

from __future__ import annotations

from dataclasses import dataclass

_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True, slots=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    vanna: float
    volga: float
    charm: float
    speed: float

    @property
    def theta_per_day(self) -> float:
        """Calendar decay per day."""
        return self.theta / _DAYS_PER_YEAR

    @property
    def vega_per_pct(self) -> float:
        """Vega per 1 percentage point of implied volatility."""
        return self.vega / 100.0

    @property
    def rho_per_pct(self) -> float:
        """Rho per 1 percentage point of the interest rate."""
        return self.rho / 100.0

    def scaled(self, *, multiplier: float, quantity: float) -> Greeks:
        """Return Greeks scaled by ``multiplier * quantity`` (per-unit -> per
        position). ``price`` is also scaled to a position notional."""
        factor = multiplier * quantity
        return Greeks(
            price=self.price * factor,
            delta=self.delta * factor,
            gamma=self.gamma * factor,
            vega=self.vega * factor,
            theta=self.theta * factor,
            rho=self.rho * factor,
            vanna=self.vanna * factor,
            volga=self.volga * factor,
            charm=self.charm * factor,
            speed=self.speed * factor,
        )
