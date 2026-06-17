"""Volatility surface construction and interpolation (R14).

Pipeline:
1. For each option quote, solve implied vol from bid / ask / mid (typed guards
   in :func:`app.pricing.implied_volatility` reject crossed/illiquid/below-
   intrinsic quotes — those points are flagged unreliable, not silently zeroed).
2. Group points by expiry into a :class:`Smile` keyed on log-moneyness
   ``ln(strike / forward)``.
3. Interpolate within an expiry (mid IV vs log-moneyness, flat extrapolation,
   never negative), and across expiries via ATM total-variance.

Quality: a point is reliable only with a fresh two-sided non-crossed quote, a
solvable IV inside a sane band, and an acceptable relative spread. Interpolation
beyond the observed strike range is flagged as extrapolation (unreliable).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Protocol

from app.core.enums import OptionType
from app.core.exceptions import PricingError
from app.models import Instrument, Quote
from app.pricing import implied_volatility

_YEAR_SECONDS = 365.0 * 24 * 3600
_MIN_IV = 1e-4
_MAX_IV = 5.0  # 500% — anything above is treated as noise


def _tau_years(expiry: date, now: datetime) -> float:
    end = datetime.combine(expiry, time(23, 59, 59), tzinfo=UTC)
    return max((end - now.astimezone(UTC)).total_seconds() / _YEAR_SECONDS, 1.0 / _YEAR_SECONDS)


def _safe_iv(
    *,
    price: float,
    underlying: float,
    strike: float,
    t: float,
    rate: float,
    option_type: OptionType,
    dividend_yield: float,
) -> float | None:
    try:
        iv = implied_volatility(
            price=price,
            underlying=underlying,
            strike=strike,
            t=t,
            rate=rate,
            option_type=option_type,
            model="bsm",
            dividend_yield=dividend_yield,
        )
    except PricingError:
        return None
    if not (_MIN_IV <= iv <= _MAX_IV):
        return None
    return iv


@dataclass(frozen=True, slots=True)
class SmilePoint:
    strike: float
    log_moneyness: float
    iv_bid: float | None
    iv_ask: float | None
    iv_mid: float | None
    reliable: bool


class SmileModel(Protocol):
    """Interpolates mid IV at a log-moneyness. SVI/SABR can implement this."""

    def iv(self, log_moneyness: float) -> float: ...


@dataclass(frozen=True, slots=True)
class InterpolatedSmile:
    """Piecewise-linear smile in (log-moneyness, mid IV) with flat extrapolation.

    Built only from reliable points; never returns a negative IV."""

    expiry: date
    forward: float
    tau: float
    points: tuple[SmilePoint, ...]

    @property
    def reliable_points(self) -> tuple[SmilePoint, ...]:
        return tuple(p for p in self.points if p.reliable and p.iv_mid is not None)

    @property
    def is_reliable(self) -> bool:
        return len(self.reliable_points) >= 2

    def _xy(self) -> tuple[list[float], list[float]]:
        pts = sorted(self.reliable_points, key=lambda p: p.log_moneyness)
        return [p.log_moneyness for p in pts], [float(p.iv_mid) for p in pts]  # type: ignore[arg-type]

    def iv(self, log_moneyness: float) -> float:
        xs, ys = self._xy()
        if not xs:
            raise PricingError(f"no reliable smile points for expiry {self.expiry}")
        if len(xs) == 1:
            return max(ys[0], _MIN_IV)
        value = _interp_clamped(log_moneyness, xs, ys)
        return max(value, _MIN_IV)

    def iv_at_strike(self, strike: float) -> float:
        return self.iv(math.log(strike / self.forward))

    def is_extrapolated(self, log_moneyness: float) -> bool:
        xs, _ = self._xy()
        return bool(xs) and (log_moneyness < xs[0] or log_moneyness > xs[-1])

    def atm_iv(self) -> float:
        return self.iv(0.0)


Smile = InterpolatedSmile  # public alias; swap for SVI/SABR later


def _interp_clamped(x: float, xs: list[float], ys: list[float]) -> float:
    """Linear interpolation with flat extrapolation at the ends."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            w = (x - x0) / (x1 - x0)
            return y0 + w * (y1 - y0)
    return ys[-1]


@dataclass(frozen=True, slots=True)
class VolatilitySurface:
    smiles: Mapping[date, InterpolatedSmile]

    def expiries(self) -> list[date]:
        return sorted(self.smiles)

    def smile(self, expiry: date) -> InterpolatedSmile:
        if expiry not in self.smiles:
            raise PricingError(f"no smile for expiry {expiry}")
        return self.smiles[expiry]

    def iv(self, expiry: date, strike: float) -> float:
        return self.smile(expiry).iv_at_strike(strike)

    def atm_term_structure(self) -> list[tuple[date, float]]:
        out: list[tuple[date, float]] = []
        for exp in self.expiries():
            sm = self.smiles[exp]
            if sm.is_reliable or sm.reliable_points:
                out.append((exp, sm.atm_iv()))
        return out

    def atm_iv_at(self, tau_years: float) -> float:
        """ATM IV at an arbitrary maturity via linear interpolation in total
        variance (var = iv^2 * tau), which is the arbitrage-friendly quantity."""
        ts = self.atm_term_structure()
        if not ts:
            raise PricingError("empty term structure")
        taus = [self.smiles[e].tau for e, _ in ts]
        variances = [iv * iv * self.smiles[e].tau for e, iv in ts]
        var = _interp_clamped(tau_years, taus, variances) if len(taus) > 1 else variances[0]
        return math.sqrt(max(var, 0.0) / max(tau_years, 1.0 / _YEAR_SECONDS))


def build_surface(
    *,
    spot: float,
    rate: float,
    dividend_yield: float,
    valuation_time: datetime,
    quotes: Iterable[tuple[Instrument, Quote]],
    max_spread_fraction: Decimal = Decimal("0"),
) -> VolatilitySurface:
    """Build a surface from ``(option_instrument, quote)`` pairs."""
    by_expiry: dict[date, list[SmilePoint]] = {}
    for inst, quote in quotes:
        if inst.expiry is None or inst.strike is None or inst.option_type is None:
            continue
        option_type = inst.option_type
        tau = _tau_years(inst.expiry, valuation_time)
        forward = spot * math.exp((rate - dividend_yield) * tau)
        strike = float(inst.strike)
        log_m = math.log(strike / forward)

        reliable = quote.has_two_sided_market and not quote.is_crossed
        sf = quote.spread_fraction
        if max_spread_fraction > 0 and (sf is None or sf > max_spread_fraction):
            reliable = False

        def _iv(px: Decimal | None, _ot: OptionType = option_type, _k: float = strike,
                _t: float = tau) -> float | None:
            if px is None or px <= 0:
                return None
            return _safe_iv(
                price=float(px), underlying=spot, strike=_k, t=_t, rate=rate,
                option_type=_ot, dividend_yield=dividend_yield,
            )

        iv_bid = _iv(quote.bid)
        iv_ask = _iv(quote.ask)
        iv_mid = _iv(quote.mid)
        if iv_mid is None:
            reliable = False

        by_expiry.setdefault(inst.expiry, []).append(
            SmilePoint(
                strike=strike,
                log_moneyness=log_m,
                iv_bid=iv_bid,
                iv_ask=iv_ask,
                iv_mid=iv_mid,
                reliable=reliable,
            )
        )

    smiles: dict[date, InterpolatedSmile] = {}
    for expiry, points in by_expiry.items():
        tau = _tau_years(expiry, valuation_time)
        forward = spot * math.exp((rate - dividend_yield) * tau)
        smiles[expiry] = InterpolatedSmile(
            expiry=expiry,
            forward=forward,
            tau=tau,
            points=tuple(sorted(points, key=lambda p: p.log_moneyness)),
        )
    return VolatilitySurface(smiles=smiles)
