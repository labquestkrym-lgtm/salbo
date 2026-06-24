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
from datetime import date, timedelta
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
    roll_buffer_days: int = 3      # roll to the next future when the front is this close to expiry
    roll_check_steps: int = 1000   # re-resolve the front (detect a roll) every N steps
    max_pair_loss: Decimal = Decimal("0")  # close on mark-to-market loss >= this (0 = disabled)
    seed_days: int = 180           # days of daily history to seed the spread z-score window
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
        self._spreads: list[float] = []        # tick-built series (fallback when no daily seed)
        self._daily_spreads: list[float] = []   # seeded daily history (the validated timescale)
        self._last_bar_date: date | None = None
        self._last_live_spread = 0.0
        self._opened = False
        self._direction = 0  # +1 long spread (long A/short B), -1 short spread
        self._contracts_a = 0
        self._contracts_b = 0
        self._entry_mid_a: Decimal | None = None  # mids at entry, for mark-to-market P&L
        self._entry_mid_b: Decimal | None = None
        self._trade_count = 0

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def trade_count(self) -> int:
        return self._trade_count

    async def _resolve_pair(self) -> ResolvedPair:
        """Resolve to the front futures whose expiry is beyond the roll buffer, so
        the loop never trades a contract about to expire (it rolls to the next)."""
        c = self._cfg
        instruments = await self._broker.list_instruments(c.symbol_a)
        instruments += await self._broker.list_instruments(c.symbol_b)
        cutoff = self._clock.now().date() + timedelta(days=c.roll_buffer_days)
        return InstrumentResolver(instruments).resolve_pair(
            c.symbol_a, c.symbol_b, beta=c.beta, on_or_after=cutoff
        )

    async def run(self) -> None:
        await self._broker.connect()
        c = self._cfg
        if self._notifier is not None:
            await self._notifier.started(self._control._settings.app_mode.value)

        step = 0
        pair = await self._resolve_pair()
        await self._reconcile(pair)
        await self._seed_daily_spreads(pair)
        # Outer loop handles the quarterly futures roll: every roll_check_steps we
        # re-resolve the front; if it changed (rolled), flatten the old legs and
        # restart the stream on the new contracts. The spread series carries over
        # (NLMK/CHMF prices are continuous across the roll for the z-score).
        while step < c.max_steps:
            symbols = [pair.leg_a.symbol, pair.leg_b.symbol]
            seen: set[str] = set()
            batch_end = min(step + c.roll_check_steps, c.max_steps)
            async for quote in self._broker.stream_quotes(symbols):
                self._mds.on_quote(quote)
                seen.add(quote.instrument_symbol)
                if len(seen) < len(symbols):
                    continue
                seen.clear()
                try:
                    await self._tick(pair, step)
                except Exception as exc:  # a bad tick/order must not kill the loop
                    logger.warning("pairs_tick_failed", step=step, error=str(exc))
                step += 1
                if step >= batch_end:
                    break
            if step >= c.max_steps:
                break
            new_pair = await self._resolve_pair()
            if new_pair.leg_a.symbol != pair.leg_a.symbol or new_pair.leg_b.symbol != pair.leg_b.symbol:
                logger.info(
                    "pairs_roll", old=pair.leg_a.symbol, new=new_pair.leg_a.symbol, step=step
                )
                if self._opened:
                    await self._close(pair, step, reason="futures roll")
                pair = new_pair

    async def _seed_daily_spreads(self, pair: ResolvedPair) -> None:
        """Seed the spread z-score window from DAILY closes so the live signal runs
        on the validated daily timescale (not tick noise). If history is missing
        (e.g. the mock), the loop falls back to building the series from ticks."""
        c = self._cfg
        closes_a = dict(await self._broker.get_daily_closes(pair.leg_a.symbol, days=c.seed_days))
        closes_b = dict(await self._broker.get_daily_closes(pair.leg_b.symbol, days=c.seed_days))
        common = sorted(set(closes_a) & set(closes_b))
        self._daily_spreads = [
            math.log(closes_a[d]) - c.beta * math.log(closes_b[d])
            for d in common
            if closes_a[d] > 0 and closes_b[d] > 0
        ]
        self._last_bar_date = self._clock.now().date()
        if len(self._daily_spreads) >= c.window:
            logger.info("pairs_seeded_daily", points=len(self._daily_spreads))
        else:
            logger.warning("pairs_seed_insufficient", points=len(self._daily_spreads))

    def _decision_series(self, live_spread: float) -> list[float]:
        """The spread series fed to the strategy. With a daily seed: the daily
        history plus today's live spread (z = today vs the prior daily window),
        rolling a new daily bar when the date changes. Without a seed: the
        tick-built series (degraded fallback for brokers without candle history)."""
        if len(self._daily_spreads) >= self._cfg.window:
            today = self._clock.now().date()
            if self._last_bar_date is not None and today > self._last_bar_date:
                self._daily_spreads.append(self._last_live_spread)  # roll yesterday's close in
                self._last_bar_date = today
            self._last_live_spread = live_spread
            return [*self._daily_spreads, live_spread]
        self._spreads.append(live_spread)
        return self._spreads

    async def _reconcile(self, pair: ResolvedPair) -> None:
        """On startup, adopt any existing pair position (resume) or flatten an
        orphan/partial leg, so a restart never double-opens or leaves one leg
        dangling. Entry mids are unknown on resume and set lazily on the first tick."""
        positions = {p.instrument_symbol: p.quantity for p in await self._broker.get_positions()}
        qa = positions.get(pair.leg_a.symbol, Decimal("0"))
        qb = positions.get(pair.leg_b.symbol, Decimal("0"))
        if qa == 0 and qb == 0:
            return
        if qa != 0 and qb != 0 and (qa > 0) != (qb > 0):
            self._direction = 1 if qa > 0 else -1  # long A/short B -> long spread
            self._contracts_a, self._contracts_b = int(abs(qa)), int(abs(qb))
            self._opened = True
            logger.info(
                "pairs_reconciled_open", direction=self._direction,
                a=self._contracts_a, b=self._contracts_b,
            )
            return
        logger.warning("pairs_reconcile_flatten_orphan", qa=str(qa), qb=str(qb))
        if qa != 0:
            await self._submit(pair.leg_a.symbol, Side.SELL if qa > 0 else Side.BUY,
                               int(abs(qa)), "pair-reconcile-a")
        if qb != 0:
            await self._submit(pair.leg_b.symbol, Side.SELL if qb > 0 else Side.BUY,
                               int(abs(qb)), "pair-reconcile-b")

    async def _tick(self, pair: ResolvedPair, step: int) -> None:
        mid_a = self._mds.mid(pair.leg_a.symbol)
        mid_b = self._mds.mid(pair.leg_b.symbol)
        if mid_a is None or mid_b is None or mid_a <= 0 or mid_b <= 0:
            return
        # Establish the P&L reference if we resumed an open position (entry unknown).
        if self._opened and self._entry_mid_a is None:
            self._entry_mid_a, self._entry_mid_b = mid_a, mid_b
        live_spread = math.log(float(mid_a)) - self._cfg.beta * math.log(float(mid_b))
        series = self._decision_series(live_spread)

        run_state = self._control.run_state
        tripped = self._risk.kill_switch.is_tripped
        if self._metrics is not None:
            self._metrics.set_kill_switch(tripped)
        if run_state is not StrategyRunState.RUNNING or tripped:
            return

        pnl = self._pair_pnl(pair, mid_a, mid_b)
        signal = self._strategy.decide(series, opened=self._opened)
        self._control.set_greeks_snapshot(
            {"available": True, "pair_z": signal.z, "opened": self._opened, "pair_pnl": str(pnl)}
        )
        # Don't open/close on a stale or one-sided book (a fill would be unreliable).
        if not (self._mds.is_tradeable(pair.leg_a.symbol) and self._mds.is_tradeable(pair.leg_b.symbol)):
            return
        # Loss stop fires regardless of the z-signal.
        if self._opened and self._cfg.max_pair_loss > 0 and pnl <= -self._cfg.max_pair_loss:
            await self._close(pair, step, reason=f"loss stop (pnl {pnl})")
            return
        if signal.action in (PairAction.OPEN_LONG_SPREAD, PairAction.OPEN_SHORT_SPREAD):
            await self._open(pair, signal.action, mid_a, mid_b, step)
        elif signal.action is PairAction.CLOSE:
            await self._close(pair, step, reason=signal.reason)

    def _pair_pnl(self, pair: ResolvedPair, mid_a: Decimal, mid_b: Decimal) -> Decimal:
        """Mark-to-market P&L from the entry mids (avoids the sandbox avg_price=0).
        Long spread (direction +1) is long A / short B; signs flip for short."""
        if not self._opened or self._entry_mid_a is None or self._entry_mid_b is None:
            return Decimal("0")
        sign_a, sign_b = Decimal(self._direction), Decimal(-self._direction)
        pnl_a = (mid_a - self._entry_mid_a) * self._contracts_a * pair.leg_a.spec.multiplier * sign_a
        pnl_b = (mid_b - self._entry_mid_b) * self._contracts_b * pair.leg_b.spec.multiplier * sign_b
        return pnl_a + pnl_b

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
        self._entry_mid_a, self._entry_mid_b = mid_a, mid_b
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
        self._entry_mid_a = self._entry_mid_b = None
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
