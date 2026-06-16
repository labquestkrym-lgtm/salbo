"""Strategy interface (R17).

A strategy makes *decisions* (enter / size / hedge / exit) from market state and
risk approval. It never talks to the broker directly — execution goes through
the OMS/execution layer and every action can be vetoed by the Risk Manager.
This keeps one strategy implementation valid across backtest/paper/live
(ADR-0002).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.strategies.atm import ATMSelection


@dataclass(frozen=True, slots=True)
class EntryDecision:
    enter: bool
    reason: str
    selection: ATMSelection | None = None


@dataclass(frozen=True, slots=True)
class ExitDecision:
    exit: bool
    reason: str


class BaseStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def evaluate_entry(self, *args: object, **kwargs: object) -> EntryDecision: ...

    @abstractmethod
    def evaluate_exit(self, *args: object, **kwargs: object) -> ExitDecision: ...
