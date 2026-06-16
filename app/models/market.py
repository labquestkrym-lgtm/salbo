"""Market data value objects.

``last`` is stored but is **never** treated as a guaranteed execution price
(R12). Execution logic uses bid/ask; valuation uses mid when both sides exist.
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator


class Quote(BaseModel):
    """Top-of-book snapshot for one instrument at a point in time."""

    model_config = ConfigDict(frozen=True)

    instrument_symbol: str
    timestamp: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    last: Decimal | None = None
    sequence: int | None = None

    @model_validator(mode="after")
    def _require_tz(self) -> Quote:
        if self.timestamp.tzinfo is None:
            raise ValueError("Quote.timestamp must be timezone-aware")
        return self

    @property
    def has_two_sided_market(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def is_crossed(self) -> bool:
        """True when bid > ask (a corrupt/locked market we must not trade on)."""
        return self.has_two_sided_market and self.bid > self.ask  # type: ignore[operator]

    @property
    def mid(self) -> Decimal | None:
        """Mid price, or ``None`` if the market is one-sided or crossed."""
        if not self.has_two_sided_market or self.is_crossed:
            return None
        return (self.bid + self.ask) / Decimal(2)  # type: ignore[operator]

    @property
    def spread(self) -> Decimal | None:
        if not self.has_two_sided_market:
            return None
        return self.ask - self.bid  # type: ignore[operator]

    @property
    def spread_fraction(self) -> Decimal | None:
        """Spread as a fraction of mid (used for liquidity filters)."""
        m = self.mid
        if m is None or m == 0:
            return None
        return self.spread / m  # type: ignore[operator]


class TradingSession(BaseModel):
    """A single instrument's trading window on a given day (exchange-local)."""

    model_config = ConfigDict(frozen=True)

    open_time: time
    close_time: time

    def contains(self, t: time) -> bool:
        return self.open_time <= t <= self.close_time
