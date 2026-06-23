"""Pairs stat-arb orchestration loop (cointegrated futures spread).

Drives the SAME components as the straddle path (MarketDataService, OMS,
control plane, notifier) but trades a two-leg mean-reverting spread instead of a
straddle. Each step: ingest both legs' quotes, append the log-spread, ask
:class:`PairsSpreadStrategy` for a signal, and open/close the dollar-neutral
two-leg futures position via the OMS. Both legs are futures (freely shortable).

Run-state (control plane): RUNNING trades, PAUSED/STOPPED only keep market data
fresh. Risk is gated on the kill switch and a hard per-leg contract cap (the
greeks-based RiskManager is straddle-specific; pair risk is position-size based).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from app.api.control import InMemoryControlPlane, StrategyRunState
from app.brokers.base import BaseBrokerAdapter
from app.core.clock import Clock
from app.core.enums import OrderType, Side
from app.core.logging import get_logger
from app.execution import InMemoryOrderStore, OrderManager
from app.instruments import InstrumentResolver
from app.instruments.resolver import ResolvedPair
from app.market_data import MarketDataService
from app.models import OrderRequest
from app.notifications import NotificationService
from app.observability import Metrics
from app.risk import RiskManager
from app.strategies.pairs import PairAction, PairsParams, PairsSpreadStrategy

logger = get_logger(__name__)


@dataclass(slots=True)
class PairsOrchestratorConfig:
    symbol_a: str
    symbol_b: str
    beta: float = 1.0
    window: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.5
    dt_seconds: float = 1.0
    target_notional_per_leg: Decimal = Decimal("5000")  # gross per leg, account currency
    max_contracts_per_leg: int = 5
    max_steps: int = 500


class PairsOrchestrator:
    def __init__(
        self,
        clock: Clock,
        broker: BaseBrokerAdapter,
        risk_manager: RiskManager,
        control: InMemoryControlPlane,
        config: PairsOrchestratorConfig,
        *,
        metrics: Metrics | None = None,
        notifier: NotificationService | None = None,
    ) -> None:
        self._clock = clock
        self._broker = broker
        self._risk = risk_manager
        self._control = control
        self._cfg = config
        self._metrics = metrics
        self._notifier = notifier
        self._mds = MarketDataService(clock=clock, max_age_seconds=config.dt_seconds * 5)
        self._oms = OrderManager(broker, InMemoryOrderStore(), clock)
        self._strategy = PairsSpreadStrategy(
            PairsParams(
                window=config.window,
                entry_z=config.entry_z,
                exit_z=config.exit_z,
                beta=config.beta,
            )
        )
        self._spreads: list[float] = []
        self._opened = False
        self._direction = 0  # +1 long spread (long A/short B), -1 short spread
        self._contracts_a = 0
        self._contracts_b = 0
        self._trade_count = 0

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def trade_count(self) -> int:
        return self._trade_count

    async def run(self) -> None:
        await self._broker.connect()
        c = self._cfg
        instruments = await self._broker.list_instruments(c.symbol_a)
        instruments += await self._broker.list_instruments(c.symbol_b)
        resolver = InstrumentResolver(instruments)
        pair = resolver.resolve_pair(c.symbol_a, c.symbol_b, beta=c.beta)
        symbols = [pair.leg_a.symbol, pair.leg_b.symbol]
        if self._notifier is not None:
            await self._notifier.started(self._control._settings.app_mode.value)

        step = 0
        seen: set[str] = set()
        async for quote in self._broker.stream_quotes(symbols):
            self._mds.on_quote(quote)
            seen.add(quote.instrument_symbol)
            if len(seen) < len(symbols):
                continue
            seen.clear()
            await self._tick(pair, step)
            step += 1
            if step >= c.max_steps:
                break

    async def _tick(self, pair: ResolvedPair, step: int) -> None:
        mid_a = self._mds.mid(pair.leg_a.symbol)
        mid_b = self._mds.mid(pair.leg_b.symbol)
        if mid_a is None or mid_b is None or mid_a <= 0 or mid_b <= 0:
            return
        spread = math.log(float(mid_a)) - self._cfg.beta * math.log(float(mid_b))
        self._spreads.append(spread)

        run_state = self._control.run_state
        tripped = self._risk.kill_switch.is_tripped
        if self._metrics is not None:
            self._metrics.set_kill_switch(tripped)
        if run_state is not StrategyRunState.RUNNING or tripped:
            return

        signal = self._strategy.decide(self._spreads, opened=self._opened)
        self._control.set_greeks_snapshot(
            {"available": True, "pair_z": signal.z, "opened": self._opened}
        )
        if signal.action in (PairAction.OPEN_LONG_SPREAD, PairAction.OPEN_SHORT_SPREAD):
            await self._open(pair, signal.action, mid_a, mid_b, step)
        elif signal.action is PairAction.CLOSE:
            await self._close(pair, step, reason=signal.reason)

    def _size(self, mid: Decimal, multiplier: Decimal, scale: float = 1.0) -> int:
        notional = float(self._cfg.target_notional_per_leg) * scale
        per_contract = float(mid) * float(multiplier)
        n = round(notional / per_contract) if per_contract > 0 else 0
        return max(1, min(n, self._cfg.max_contracts_per_leg))

    async def _open(
        self, pair: ResolvedPair, action: PairAction, mid_a: Decimal, mid_b: Decimal, step: int
    ) -> None:
        self._contracts_a = self._size(mid_a, pair.leg_a.spec.multiplier)
        self._contracts_b = self._size(mid_b, pair.leg_b.spec.multiplier, scale=self._cfg.beta)
        long_spread = action is PairAction.OPEN_LONG_SPREAD
        self._direction = 1 if long_spread else -1
        # long spread = long A / short B; short spread = short A / long B.
        side_a = Side.BUY if long_spread else Side.SELL
        side_b = Side.SELL if long_spread else Side.BUY
        await self._submit(pair.leg_a.symbol, side_a, self._contracts_a, f"pair-{step}-a")
        await self._submit(pair.leg_b.symbol, side_b, self._contracts_b, f"pair-{step}-b")
        self._opened = True
        self._trade_count += 1
        logger.info(
            "pairs_opened", step=step, action=action.value,
            a=self._contracts_a, b=self._contracts_b,
        )

    async def _close(self, pair: ResolvedPair, step: int, *, reason: str) -> None:
        long_spread = self._direction == 1
        # reverse the opening sides to flatten.
        side_a = Side.SELL if long_spread else Side.BUY
        side_b = Side.BUY if long_spread else Side.SELL
        legs: list[str] = []
        oa = await self._submit(pair.leg_a.symbol, side_a, self._contracts_a, f"pair-{step}-xa")
        ob = await self._submit(pair.leg_b.symbol, side_b, self._contracts_b, f"pair-{step}-xb")
        legs.append(f"{pair.leg_a.symbol} {side_a.value.upper()} {oa}")
        legs.append(f"{pair.leg_b.symbol} {side_b.value.upper()} {ob}")
        self._opened = False
        self._direction = 0
        logger.info("pairs_closed", step=step, reason=reason)
        if self._notifier is not None:
            await self._notifier.position_closed(reason, "; ".join(legs))

    async def _submit(self, symbol: str, side: Side, qty: int, coid: str) -> str:
        order = await self._oms.submit(
            OrderRequest(
                client_order_id=coid,
                instrument_symbol=symbol,
                side=side,
                quantity=Decimal(qty),
                order_type=OrderType.MARKET,
            )
        )
        if self._notifier is not None:
            await self._notifier.leg_filled(
                symbol, str(order.filled_quantity), str(order.average_fill_price)
            )
        return f"{order.filled_quantity}@{order.average_fill_price}"
