"""Pairs mean-reversion strategy (stat-arb on a cointegrated futures pair).

Decision logic only -- execution/coordination is the orchestrator's job (OMS,
risk, two-leg fills), mirroring ADR-0002 so the same logic runs in backtest and
live.

Trades the log-spread ``s = ln(P_a) - beta*ln(P_b)`` of two cointegrated legs
(e.g. NLMK/CHMF futures). When the spread's rolling z-score is extreme it is
expected to revert: a HIGH z (leg A rich) -> short the spread (short A / long B);
a LOW z -> long the spread (long A / short B). Close when the z-score reverts
inside the exit band. Validated params (scripts/*): window 60, entry |z|>=2,
exit |z|<=0.5, beta~1 for NLMK/CHMF.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class PairAction(StrEnum):
    HOLD = "hold"
    OPEN_LONG_SPREAD = "open_long_spread"    # long A / short B (spread is cheap)
    OPEN_SHORT_SPREAD = "open_short_spread"  # short A / long B (spread is rich)
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class PairsParams:
    window: int = 60          # rolling lookback for the z-score (trading periods)
    entry_z: float = 2.0      # open when |z| >= this
    exit_z: float = 0.5       # close when |z| <= this
    beta: float = 1.0         # hedge ratio in s = ln(A) - beta*ln(B)

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("window must be >= 2")
        if self.entry_z <= self.exit_z:
            raise ValueError("entry_z must exceed exit_z")


@dataclass(frozen=True, slots=True)
class PairSignal:
    action: PairAction
    z: float
    reason: str


def spread_value(price_a: float, price_b: float, beta: float = 1.0) -> float:
    """The traded log-spread ``ln(A) - beta*ln(B)``."""
    if price_a <= 0 or price_b <= 0:
        raise ValueError("prices must be positive")
    return math.log(price_a) - beta * math.log(price_b)


class PairsSpreadStrategy:
    name = "pairs_spread_reversion"

    def __init__(self, params: PairsParams) -> None:
        self._p = params

    def zscore(self, spreads: Sequence[float]) -> float | None:
        """z of the latest spread vs the PRIOR ``window`` values (the current
        point is excluded from the mean/std, matching the backtest). ``None``
        until enough history or if the window is degenerate (zero variance)."""
        p = self._p
        if len(spreads) < p.window + 1:
            return None
        window = spreads[-p.window - 1 : -1]
        mu = statistics.fmean(window)
        sd = statistics.pstdev(window, mu)
        if sd <= 0.0:
            return None
        return (spreads[-1] - mu) / sd

    def decide(self, spreads: Sequence[float], *, opened: bool) -> PairSignal:
        z = self.zscore(spreads)
        if z is None:
            return PairSignal(PairAction.HOLD, 0.0, "warming up / no variance")
        p = self._p
        if not opened:
            if z >= p.entry_z:
                return PairSignal(PairAction.OPEN_SHORT_SPREAD, z, f"z {z:+.2f} >= {p.entry_z}")
            if z <= -p.entry_z:
                return PairSignal(PairAction.OPEN_LONG_SPREAD, z, f"z {z:+.2f} <= -{p.entry_z}")
            return PairSignal(PairAction.HOLD, z, f"z {z:+.2f} inside entry band")
        if abs(z) <= p.exit_z:
            return PairSignal(PairAction.CLOSE, z, f"|z| {abs(z):.2f} <= {p.exit_z} (reverted)")
        return PairSignal(PairAction.HOLD, z, f"z {z:+.2f} still beyond exit band")
