"""ATM strike selection by liquidity/spread/proximity (R17)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.models import Quote
from app.strategies.atm import StrikeCandidate, select_atm_strike

_T = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _q(bid: str, ask: str) -> Quote:
    return Quote(
        instrument_symbol="o",
        timestamp=_T,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
    )


def _cand(strike: str, bid: str = "5.00", ask: str = "5.10", **kw: object) -> StrikeCandidate:
    return StrikeCandidate(
        strike=Decimal(strike), call_quote=_q(bid, ask), put_quote=_q(bid, ask), **kw
    )  # type: ignore[arg-type]


def test_picks_nearest_liquid_strike() -> None:
    spot = Decimal("100.40")
    cands = [_cand("95"), _cand("100"), _cand("105")]
    sel = select_atm_strike(spot, cands)
    assert sel is not None
    assert sel.candidate.strike == Decimal("100")


def test_rejects_wide_spread() -> None:
    spot = Decimal("100")
    wide = _cand("100", bid="4.00", ask="6.00")  # ~40% spread
    sel = select_atm_strike(spot, [wide], max_spread_fraction=Decimal("0.10"))
    assert sel is None


def test_falls_back_to_next_strike_when_atm_illiquid() -> None:
    spot = Decimal("100")
    illiquid_atm = _cand("100", bid="4.00", ask="6.00")
    liquid_next = _cand("105", bid="3.00", ask="3.05")
    sel = select_atm_strike(spot, [illiquid_atm, liquid_next], max_spread_fraction=Decimal("0.05"))
    assert sel is not None
    assert sel.candidate.strike == Decimal("105")


def test_max_strike_distance_filters() -> None:
    spot = Decimal("100")
    sel = select_atm_strike(spot, [_cand("110")], max_strike_distance=Decimal("5"))
    assert sel is None


def test_min_volume_filter() -> None:
    spot = Decimal("100")
    thin = _cand("100", call_volume=1, put_volume=1)
    sel = select_atm_strike(spot, [thin], min_volume=100)
    assert sel is None


def test_crossed_market_rejected() -> None:
    spot = Decimal("100")
    crossed = StrikeCandidate(
        strike=Decimal("100"),
        call_quote=Quote(
            instrument_symbol="o",
            timestamp=_T,
            bid=Decimal("6"),
            ask=Decimal("5"),
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
        ),
        put_quote=_q("5", "5.1"),
    )
    assert select_atm_strike(spot, [crossed]) is None
