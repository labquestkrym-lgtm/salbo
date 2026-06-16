"""Clock abstraction.

All time access goes through a ``Clock`` so the same strategy code runs under a
simulated clock (backtest) and wall-clock (paper/live) — see ADR-0002. No module
may call ``datetime.now`` / ``time.time`` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware (UTC)."""
        ...


class SystemClock:
    """Wall-clock time in UTC. Used for paper/sandbox/live."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class SimulatedClock:
    """Manually advanced clock for backtests and tests."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimulatedClock requires a timezone-aware start time")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("Clock value must be timezone-aware")
        self._now = value.astimezone(UTC)

    def advance(self, *, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)
