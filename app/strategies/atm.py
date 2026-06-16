"""ATM strike selection (R17).

The chosen straddle strike is not merely the numerically-closest one: a strike
qualifies only if BOTH its call and put have a fresh two-sided, non-crossed
market, an acceptable relative spread, sufficient volume/open interest (when
available), and lie within a maximum distance from spot. Among qualifying
strikes the nearest to spot wins, tie-broken by the tightest combined spread.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models import Quote


@dataclass(frozen=True, slots=True)
class StrikeCandidate:
    strike: Decimal
    call_quote: Quote
    put_quote: Quote
    call_volume: int | None = None
    put_volume: int | None = None
    open_interest: int | None = None


@dataclass(frozen=True, slots=True)
class ATMSelection:
    candidate: StrikeCandidate
    distance: Decimal
    combined_spread_fraction: Decimal


def _quote_ok(q: Quote, max_spread_fraction: Decimal) -> bool:
    if not q.has_two_sided_market or q.is_crossed:
        return False
    sf = q.spread_fraction
    if sf is None:
        return False
    return max_spread_fraction <= 0 or sf <= max_spread_fraction


def select_atm_strike(
    spot: Decimal,
    candidates: list[StrikeCandidate],
    *,
    max_spread_fraction: Decimal = Decimal("0"),
    max_strike_distance: Decimal = Decimal("0"),
    min_volume: int = 0,
    min_open_interest: int = 0,
) -> ATMSelection | None:
    """Return the best ATM strike, or ``None`` if none qualify.

    A ``0`` threshold means "do not filter on this dimension".
    """
    qualifying: list[ATMSelection] = []
    for c in candidates:
        distance = abs(c.strike - spot)
        if max_strike_distance > 0 and distance > max_strike_distance:
            continue
        if not _quote_ok(c.call_quote, max_spread_fraction):
            continue
        if not _quote_ok(c.put_quote, max_spread_fraction):
            continue
        if min_volume > 0 and (
            (c.call_volume is not None and c.call_volume < min_volume)
            or (c.put_volume is not None and c.put_volume < min_volume)
        ):
            continue
        if (
            min_open_interest > 0
            and c.open_interest is not None
            and c.open_interest < min_open_interest
        ):
            continue
        call_sf = c.call_quote.spread_fraction or Decimal("0")
        put_sf = c.put_quote.spread_fraction or Decimal("0")
        qualifying.append(
            ATMSelection(candidate=c, distance=distance, combined_spread_fraction=call_sf + put_sf)
        )

    if not qualifying:
        return None
    qualifying.sort(key=lambda s: (s.distance, s.combined_spread_fraction))
    return qualifying[0]
