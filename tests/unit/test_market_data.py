"""Market Data Service (R12): book, mid, staleness, gaps, record/replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.clock import SimulatedClock
from app.core.exceptions import MarketDataError
from app.market_data import MarketDataRecorder, MarketDataService, replay_quotes
from app.models import Quote

_T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _quote(symbol: str, ts: datetime, bid: str, ask: str, seq: int) -> Quote:
    return Quote(
        instrument_symbol=symbol,
        timestamp=ts,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        last=Decimal(bid),
        sequence=seq,
    )


def _service(max_age: float = 5.0) -> MarketDataService:
    return MarketDataService(clock=SimulatedClock(_T0), max_age_seconds=max_age)


def test_book_update_and_mid() -> None:
    svc = _service()
    svc.on_quote(_quote("A", _T0, "99.0", "101.0", 1))
    assert svc.get_quote("A") is not None
    assert svc.mid("A") == Decimal("100.0")
    assert svc.is_tradeable("A") is True


def test_gap_detection() -> None:
    svc = _service()
    svc.on_quote(_quote("A", _T0, "1", "2", 1))
    svc.on_quote(_quote("A", _T0, "1", "2", 2))
    svc.on_quote(_quote("A", _T0, "1", "2", 4))  # skips 3
    assert svc.health.gaps == 1
    assert svc.health.missing_sequences == 1


def test_duplicate_and_out_of_order() -> None:
    svc = _service()
    svc.on_quote(_quote("A", _T0, "1", "2", 5))
    svc.on_quote(_quote("A", _T0, "1", "2", 5))  # duplicate
    svc.on_quote(_quote("A", _T0, "1", "2", 3))  # out of order
    assert svc.health.duplicates == 1
    assert svc.health.out_of_order == 1


def test_crossed_quote_flagged_and_not_tradeable() -> None:
    svc = _service()
    svc.on_quote(_quote("A", _T0, "102.0", "100.0", 1))  # bid > ask
    assert svc.health.crossed == 1
    assert svc.is_tradeable("A") is False
    assert svc.mid("A") is None  # crossed -> no usable mid


def test_staleness() -> None:
    clock = SimulatedClock(_T0)
    svc = MarketDataService(clock=clock, max_age_seconds=5.0)
    svc.on_quote(_quote("A", _T0, "99", "101", 1))
    assert svc.is_stale("A") is False
    clock.advance(seconds=6)
    assert svc.is_stale("A") is True
    with pytest.raises(MarketDataError):
        svc.assert_fresh("A")


def test_missing_symbol_is_stale() -> None:
    svc = _service()
    assert svc.is_stale("UNKNOWN") is True
    assert svc.mid("UNKNOWN") is None


def test_record_and_replay_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "quotes.jsonl"
    quotes = [_quote("A", _T0 + timedelta(seconds=i), "99", "101", i + 1) for i in range(3)]
    with MarketDataRecorder(path) as rec:
        for q in quotes:
            rec.record(q)

    import asyncio

    async def _collect() -> list[Quote]:
        return [q async for q in replay_quotes(path)]

    replayed = asyncio.run(_collect())
    assert len(replayed) == 3
    assert replayed[0].instrument_symbol == "A"
    assert replayed[2].sequence == 3
    assert replayed[1].bid == Decimal("99")
