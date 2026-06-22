"""Orchestration worker loop.

Ties the live runner together using the SAME components as backtest (ADR-0002):
drives the broker's quote stream through MarketDataService -> strategy entry ->
risk checks -> hedge loop -> OMS, and publishes Greeks/P&L snapshots to the
control plane and Prometheus metrics. The Risk Manager can veto entry/hedging
and trips the kill switch on critical breaches; when tripped, the loop stops
opening new risk (default policy simply holds).

Run-state from the control plane: RUNNING acts fully, PAUSED only manages the
existing hedge (no new entry), STOPPED idles while keeping market data fresh.
Bounded by ``max_steps`` for tests / batch runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.api.control import InMemoryControlPlane, StrategyRunState
from app.brokers.mock import MockBrokerAdapter
from app.core.clock import Clock
from app.core.enums import OrderType, Side
from app.core.exceptions import RiskLimitBreachError
from app.core.logging import get_logger
from app.execution import HedgeBandConfig, HedgeEngine, InMemoryOrderStore, OrderManager
from app.execution.straddle import StraddleExecutor
from app.instruments import InstrumentResolver
from app.instruments.resolver import ResolvedStraddle
from app.market_data import MarketDataService
from app.models import OrderRequest
from app.notifications import NotificationService
from app.observability import Metrics
from app.portfolio import PortfolioGreeks, PortfolioGreeksEngine, PricingInputs, UnderlyingState
from app.risk import RiskManager, RiskState
from app.strategies.atm import StrikeCandidate
from app.strategies.long_straddle import (
    DeltaHedgedLongStraddleStrategy,
    StraddleEntryParams,
    StraddleExitParams,
)
from app.strategies.vol_forecast import ForecastConfig, VolatilityForecast, realized_vol

logger = get_logger(__name__)
_YEAR_SECONDS = 365.0 * 24 * 3600


@dataclass(slots=True)
class OrchestratorConfig:
    symbol: str = "XYZ"
    rate: float = 0.05
    dividend_yield: float = 0.0
    sigma: float = 0.20
    lookback: int = 30
    entry_contracts: int = 5
    exit_min_days_to_expiry: int = 0
    transaction_cost_buffer: float = 0.02
    model_uncertainty_buffer: float = 0.02
    base_trigger_units: float = 20.0
    hedge_to_zero: bool = True
    min_futures_trade: int = 1
    cost_per_contract: Decimal = Decimal("1.0")
    max_steps: int = 500


class Orchestrator:
    def __init__(
        self,
        clock: Clock,
        broker: MockBrokerAdapter,
        risk_manager: RiskManager,
        control: InMemoryControlPlane,
        config: OrchestratorConfig,
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
        dt = broker._cfg.dt_seconds
        self._mds = MarketDataService(clock=clock, max_age_seconds=dt * 5)
        self._oms = OrderManager(broker, InMemoryOrderStore(), clock)
        self._executor = StraddleExecutor(self._oms)
        self._greeks = PortfolioGreeksEngine(broker._instruments)
        self._hedger = HedgeEngine(
            HedgeBandConfig(
                base_trigger_units=config.base_trigger_units,
                hedge_to_zero=config.hedge_to_zero,
                min_futures_trade=config.min_futures_trade,
            )
        )
        self._strategy = DeltaHedgedLongStraddleStrategy(
            entry_params=StraddleEntryParams(contracts=config.entry_contracts),
            exit_params=StraddleExitParams(min_days_to_expiry=config.exit_min_days_to_expiry),
            forecast_config=ForecastConfig(
                transaction_cost_buffer=config.transaction_cost_buffer,
                model_uncertainty_buffer=config.model_uncertainty_buffer,
            ),
        )
        self._opened = False
        self._hedge_count = 0
        self._underlying_mids: list[float] = []

    @property
    def opened(self) -> bool:
        return self._opened

    @property
    def hedge_count(self) -> int:
        return self._hedge_count

    async def run(self) -> None:
        await self._broker.connect()
        resolver = InstrumentResolver(list(self._broker._instruments.values()))
        sym = self._cfg.symbol
        expiry = resolver.expiries(sym)[0]
        spot0 = Decimal(str(self._broker._cfg.spot0))
        strike = min(resolver.strikes(sym, expiry), key=lambda k: abs(k - spot0))
        straddle = resolver.resolve_straddle(sym, expiry=expiry, strike=strike)
        symbols = [sym, straddle.future.symbol, straddle.call.symbol, straddle.put.symbol]
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
            await self._tick(straddle, quote.timestamp, step)
            step += 1
            if step >= self._cfg.max_steps:
                break

    async def _tick(self, straddle: ResolvedStraddle, now: datetime, step: int) -> None:
        run_state = self._control.run_state
        u_mid = self._mds.mid(self._cfg.symbol)
        if u_mid is not None:
            self._underlying_mids.append(float(u_mid))
        if run_state is StrategyRunState.STOPPED or u_mid is None:
            return

        if self._opened:
            greeks = await self._compute_greeks(straddle, now, u_mid)
            self._publish(greeks)
            await self._run_risk(greeks)

        tripped = self._risk.kill_switch.is_tripped
        if self._metrics is not None:
            self._metrics.set_kill_switch(tripped)

        if run_state is StrategyRunState.RUNNING and not self._opened and not tripped:
            await self._maybe_enter(straddle, u_mid, step)
        if run_state is StrategyRunState.RUNNING and self._opened and not tripped:
            await self._maybe_exit(straddle, now, step)
        if self._opened and not tripped:
            await self._maybe_hedge(straddle, now, u_mid, step)

    async def _compute_greeks(
        self, straddle: ResolvedStraddle, now: datetime, u_mid: Decimal
    ) -> PortfolioGreeks:
        positions = await self._broker.get_positions()
        inputs = PricingInputs(
            valuation_time=now,
            underlying=UnderlyingState(
                spot=float(u_mid), rate=self._cfg.rate, dividend_yield=self._cfg.dividend_yield
            ),
            sigma_by_symbol={
                straddle.call.symbol: self._cfg.sigma,
                straddle.put.symbol: self._cfg.sigma,
            },
            mid_by_symbol={},
            future_multiplier=float(straddle.future.spec.multiplier),
        )
        return self._greeks.compute(positions, inputs)

    def _publish(self, g: PortfolioGreeks) -> None:
        self._control.set_greeks_snapshot(
            {
                "available": True,
                "net_delta_units": g.net_delta_units,
                "net_gamma_units": g.net_gamma_units,
                "cash_delta": str(g.cash_delta),
                "net_vega_per_pct": str(g.net_vega_per_pct),
                "net_theta_per_day": str(g.net_theta_per_day),
            }
        )
        if self._metrics is not None:
            self._metrics.set_portfolio(
                net_delta_units=g.net_delta_units,
                net_gamma_units=g.net_gamma_units,
                cash_delta=float(g.cash_delta),
            )

    async def _run_risk(self, g: PortfolioGreeks) -> None:
        state = RiskState(
            cash_delta=g.cash_delta,
            net_delta_units=g.net_delta_units,
            net_gamma_units=g.net_gamma_units,
            net_vega_per_pct=g.net_vega_per_pct,
            net_theta_per_day=g.net_theta_per_day,
        )
        before = self._risk.kill_switch.is_tripped
        self._risk.evaluate(state)
        if self._risk.kill_switch.is_tripped and not before:
            event = self._risk.kill_switch.events[-1]
            logger.error("orchestrator_kill_switch_tripped", trigger=event.trigger.value)
            if self._notifier is not None:
                await self._notifier.kill_switch(event.trigger.value, event.detail)

    async def _maybe_enter(self, straddle: ResolvedStraddle, u_mid: Decimal, step: int) -> None:
        if len(self._underlying_mids) < self._cfg.lookback:
            return
        call_q = self._mds.get_quote(straddle.call.symbol)
        put_q = self._mds.get_quote(straddle.put.symbol)
        if call_q is None or put_q is None:
            return
        rv = realized_vol(
            self._underlying_mids[-self._cfg.lookback :],
            periods_per_year=_YEAR_SECONDS / self._broker._cfg.dt_seconds,
        )
        candidate = StrikeCandidate(strike=straddle.strike, call_quote=call_q, put_quote=put_q)
        entry = self._strategy.evaluate_entry(
            spot=u_mid,
            candidates=[candidate],
            forecast=VolatilityForecast(expected_rv=rv),
            implied_vol=self._cfg.sigma,
        )
        if not entry.enter or entry.selection is None:
            return
        try:
            self._risk.assert_can_open(RiskState())  # veto only on existing breach / kill switch
        except RiskLimitBreachError as exc:
            logger.warning("entry_vetoed_by_risk", error=str(exc))
            return
        call_req, put_req = self._strategy.build_leg_requests(
            entry.selection, straddle.call, straddle.put, id_prefix=f"orch-{step}"
        )
        result = await self._executor.open(call_req, put_req)
        if result.is_open:
            self._opened = True
            logger.info("orchestrator_opened", step=step, strike=str(straddle.strike))
            if self._notifier is not None:
                for order in (result.call_order, result.put_order):
                    await self._notifier.leg_filled(
                        order.request.instrument_symbol,
                        str(order.filled_quantity),
                        str(order.average_fill_price),
                    )

    async def _maybe_hedge(
        self, straddle: ResolvedStraddle, now: datetime, u_mid: Decimal, step: int
    ) -> None:
        greeks = await self._compute_greeks(straddle, now, u_mid)
        future_mult = float(straddle.future.spec.multiplier)
        decision = self._hedger.decide(
            current_delta_units=greeks.net_delta_units,
            future_multiplier=future_mult,
            now=now,
            gamma_units=greeks.net_gamma_units,
            cost_per_contract=self._cfg.cost_per_contract,
        )
        if not decision.should_hedge:
            return
        side = Side.BUY if decision.contracts > 0 else Side.SELL
        order = await self._oms.submit(
            OrderRequest(
                client_order_id=f"orch-hedge-{step}",
                instrument_symbol=straddle.future.symbol,
                side=side,
                quantity=Decimal(abs(decision.contracts)),
                order_type=OrderType.MARKET,
            )
        )
        self._hedge_count += 1
        if self._metrics is not None:
            self._metrics.hedges_total.inc()
        if self._notifier is not None:
            await self._notifier.hedged(
                straddle.future.symbol,
                side.value,
                decision.contracts,
                str(order.average_fill_price),
                decision.reason,
            )

    async def _maybe_exit(self, straddle: ResolvedStraddle, now: datetime, step: int) -> None:
        # Time-stop exit is fully deterministic (date-based). Profit/loss stops
        # stay gated behind exit params (None by default) and need the P&L feed,
        # so we pass zero P&L here — it can never mis-fire a profit/loss stop.
        decision = self._strategy.evaluate_exit(
            now=now.date(), expiry=straddle.expiry, unrealized_pnl=Decimal(0)
        )
        if not decision.exit:
            return
        legs: list[str] = []
        # Long both option legs -> SELL to close.
        for leg in (straddle.call, straddle.put):
            closed = await self._oms.submit(
                OrderRequest(
                    client_order_id=f"orch-exit-{step}-{leg.symbol}",
                    instrument_symbol=leg.symbol,
                    side=Side.SELL,
                    quantity=Decimal(self._cfg.entry_contracts),
                    order_type=OrderType.MARKET,
                )
            )
            legs.append(f"{leg.symbol} SELL {closed.filled_quantity}@{closed.average_fill_price}")
        # Flatten any residual hedge in the future.
        fut_qty = await self._future_position_qty(straddle.future.symbol)
        if fut_qty != 0:
            fut_side = Side.SELL if fut_qty > 0 else Side.BUY
            flat = await self._oms.submit(
                OrderRequest(
                    client_order_id=f"orch-exit-{step}-fut",
                    instrument_symbol=straddle.future.symbol,
                    side=fut_side,
                    quantity=abs(fut_qty),
                    order_type=OrderType.MARKET,
                )
            )
            legs.append(
                f"{straddle.future.symbol} {fut_side.value.upper()} "
                f"{flat.filled_quantity}@{flat.average_fill_price}"
            )
        self._opened = False
        logger.info("orchestrator_closed", step=step, reason=decision.reason)
        if self._notifier is not None:
            await self._notifier.position_closed(decision.reason, "; ".join(legs))

    async def _future_position_qty(self, symbol: str) -> Decimal:
        for position in await self._broker.get_positions():
            if position.instrument_symbol == symbol:
                return position.quantity
        return Decimal(0)
