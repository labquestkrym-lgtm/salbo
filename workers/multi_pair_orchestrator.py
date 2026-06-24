"""Portfolio of cointegrated pairs run concurrently (raises activity the right way).

The single-pair daily trade is robust but rare (~10 trades / 2y); intraday loses
(noise + friction). The validated way to be more active AND keep the edge is to
trade MANY daily pairs at once. This composes one ``PairsOrchestrator`` per pair,
sharing the broker (one channel, many streams), risk and control plane, and runs
them concurrently. Each pair publishes a labelled snapshot the control plane
aggregates (open_pairs, total_pair_pnl).

Per-pair risk caps still apply inside each loop; a portfolio-level risk overlay
(aggregate gross/loss) is a follow-up.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from app.api.control import InMemoryControlPlane
from app.brokers.base import BaseBrokerAdapter
from app.core.clock import Clock
from app.core.logging import get_logger
from app.notifications import NotificationService
from app.observability import Metrics
from app.risk import RiskManager
from workers.pairs_orchestrator import PairsOrchestrator, PairsOrchestratorConfig

logger = get_logger(__name__)


@dataclass(slots=True)
class MultiPairOrchestratorConfig:
    pairs: list[tuple[str, str, float]]  # (symbol_a, symbol_b, beta) per pair
    window: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5
    dt_seconds: float = 1.0
    target_notional_per_leg: Decimal = Decimal("5000")
    max_contracts_per_leg: int = 5
    roll_buffer_days: int = 3
    roll_check_steps: int = 1000
    max_pair_loss: Decimal = Decimal("0")
    seed_days: int = 180
    max_steps: int = 500


class MultiPairOrchestrator:
    def __init__(
        self,
        clock: Clock,
        broker: BaseBrokerAdapter,
        risk_manager: RiskManager,
        control: InMemoryControlPlane,
        config: MultiPairOrchestratorConfig,
        *,
        metrics: Metrics | None = None,
        notifier: NotificationService | None = None,
    ) -> None:
        self._cfg = config
        self._subs: list[PairsOrchestrator] = [
            PairsOrchestrator(
                clock,
                broker,
                risk_manager,
                control,
                PairsOrchestratorConfig(
                    symbol_a=a,
                    symbol_b=b,
                    beta=beta,
                    window=config.window,
                    entry_z=config.entry_z,
                    exit_z=config.exit_z,
                    dt_seconds=config.dt_seconds,
                    target_notional_per_leg=config.target_notional_per_leg,
                    max_contracts_per_leg=config.max_contracts_per_leg,
                    roll_buffer_days=config.roll_buffer_days,
                    roll_check_steps=config.roll_check_steps,
                    max_pair_loss=config.max_pair_loss,
                    seed_days=config.seed_days,
                    max_steps=config.max_steps,
                ),
                metrics=metrics,
                notifier=notifier,
                label=f"{a}/{b}",
            )
            for (a, b, beta) in config.pairs
        ]

    @property
    def trade_count(self) -> int:
        return sum(s.trade_count for s in self._subs)

    @property
    def subs(self) -> list[PairsOrchestrator]:
        return self._subs

    async def run(self) -> None:
        logger.info("multi_pair_start", pairs=[s._label for s in self._subs])
        # One loop per pair, concurrently. They share the broker channel (connect is
        # idempotent). A failing pair must not abort the others.
        results = await asyncio.gather(*(s.run() for s in self._subs), return_exceptions=True)
        for sub, res in zip(self._subs, results, strict=True):
            if isinstance(res, Exception):
                logger.warning("multi_pair_sub_failed", label=sub._label, error=str(res))
