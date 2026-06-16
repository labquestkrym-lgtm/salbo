"""Market Data Service (R12).

Maintains top-of-book state per instrument, tracks a monotonic feed sequence to
detect gaps/duplicates/out-of-order messages, flags stale and crossed markets,
and optionally records every quote for replay. It normalizes nothing beyond what
adapters already produce; adapters are responsible for emitting domain
:class:`Quote` objects.

Crucially: ``last`` is never used as a tradeable price — callers use ``mid`` for
valuation and bid/ask for execution.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.clock import Clock
from app.core.exceptions import MarketDataError
from app.core.logging import get_logger
from app.market_data.recorder import MarketDataRecorder
from app.models import Quote

logger = get_logger(__name__)


@dataclass(slots=True)
class FeedHealth:
    quotes_seen: int = 0
    gaps: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    crossed: int = 0
    last_sequence: int | None = None
    missing_sequences: int = 0  # total count of skipped sequence numbers


@dataclass(slots=True)
class MarketDataService:
    clock: Clock
    max_age_seconds: float
    recorder: MarketDataRecorder | None = None
    _latest: dict[str, Quote] = field(default_factory=dict)
    health: FeedHealth = field(default_factory=FeedHealth)

    def on_quote(self, quote: Quote) -> None:
        """Ingest a single quote: update book, track sequence health, record."""
        self.health.quotes_seen += 1
        self._track_sequence(quote)
        if quote.is_crossed:
            self.health.crossed += 1
            logger.warning(
                "crossed_quote",
                symbol=quote.instrument_symbol,
                bid=str(quote.bid),
                ask=str(quote.ask),
            )
        self._latest[quote.instrument_symbol] = quote
        if self.recorder is not None:
            self.recorder.record(quote)

    def _track_sequence(self, quote: Quote) -> None:
        seq = quote.sequence
        if seq is None:
            return
        last = self.health.last_sequence
        if last is None:
            self.health.last_sequence = seq
            return
        if seq == last:
            self.health.duplicates += 1
            return
        if seq < last:
            self.health.out_of_order += 1
            return
        if seq > last + 1:
            self.health.gaps += 1
            self.health.missing_sequences += seq - last - 1
            logger.warning("sequence_gap", previous=last, current=seq, missing=seq - last - 1)
        self.health.last_sequence = seq

    async def ingest(self, source: AsyncIterator[Quote]) -> None:
        """Drain an async quote source into the book."""
        async for quote in source:
            self.on_quote(quote)

    # --- queries ------------------------------------------------------------
    def get_quote(self, symbol: str) -> Quote | None:
        return self._latest.get(symbol)

    def require_quote(self, symbol: str) -> Quote:
        quote = self._latest.get(symbol)
        if quote is None:
            raise MarketDataError(f"no quote for {symbol}")
        return quote

    def mid(self, symbol: str) -> Decimal | None:
        quote = self._latest.get(symbol)
        return quote.mid if quote is not None else None

    def age_seconds(self, symbol: str) -> float | None:
        quote = self._latest.get(symbol)
        if quote is None:
            return None
        return (self.clock.now() - quote.timestamp).total_seconds()

    def is_stale(self, symbol: str) -> bool:
        age = self.age_seconds(symbol)
        return age is None or age > self.max_age_seconds

    def assert_fresh(self, symbol: str) -> None:
        age = self.age_seconds(symbol)
        if age is None:
            raise MarketDataError(f"no quote for {symbol}")
        if age > self.max_age_seconds:
            raise MarketDataError(
                f"stale quote for {symbol}: age {age:.3f}s > {self.max_age_seconds}s"
            )

    def is_tradeable(self, symbol: str) -> bool:
        """True only for a fresh, two-sided, non-crossed market."""
        quote = self._latest.get(symbol)
        return (
            quote is not None
            and quote.has_two_sided_market
            and not quote.is_crossed
            and not self.is_stale(symbol)
        )
