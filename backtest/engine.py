"""Event-driven backtest engine (R24).

Drives the mock market's quote stream through the *same* components used live —
MarketDataService, PortfolioGreeksEngine, HedgeEngine, OMS, StraddleExecutor and
the strategy decision logic (ADR-0002). Each step: ingest quotes, (maybe) open
the straddle on a vol signal, re-hedge the delta, and mark the book to market
(mid for valuation, fills crossed the bid/ask). Produces a :class:`BacktestReport`.

This is a controlled simulation, not a profitability claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.brokers.mock import MockBrokerAdapter
from app.core.clock import Clock
from app.core.enums import OrderType, Side
from app.core.logging import get_logger
from app.execution import HedgeBandConfig, HedgeEngine, InMemoryOrderStore, OrderManager
from app.execution.straddle import StraddleExecutor
from app.instruments import InstrumentResolver
from app.instruments.resolver import ResolvedStraddle
from app.market_data import MarketDataService
from app.models import Instrument, Order, OrderRequest
from app.portfolio import PortfolioGreeksEngine, PricingInputs, UnderlyingState
from app.strategies.atm import StrikeCandidate
from app.strategies.long_straddle import (
    DeltaHedgedLongStraddleStrategy,
    StraddleEntryParams,
    StraddleExitParams,
)
from app.strategies.vol_forecast import ForecastConfig, VolatilityForecast, realized_vol
from backtest import metrics
from backtest.report import BacktestReport

logger = get_logger(__name__)

_YEAR_SECONDS = 365.0 * 24 * 3600


@dataclass(slots=True)
class BacktestConfig:
    symbol: str = "XYZ"
    rate: float = 0.05
    dividend_yield: float = 0.0
    sigma: float = 0.20  # implied vol used for valuation Greeks
    lookback: int = 30
    steps: int = 300
    entry_contracts: int = 5
    transaction_cost_buffer: float = 0.02
    model_uncertainty_buffer: float = 0.02
    base_trigger_units: float = 20.0
    hedge_to_zero: bool = True
    min_futures_trade: int = 1
    cost_per_contract: Decimal = Decimal("1.0")
    max_spread_fraction: Decimal = Decimal("0")


@dataclass(slots=True)
class _RunState:
    opened: bool = False
    hedge_count: int = 0
    slippage: Decimal = Decimal("0")
    equity_curve: list[float] = field(default_factory=list)
    abs_deltas: list[float] = field(default_factory=list)
    underlying_mids: list[float] = field(default_factory=list)


class BacktestEngine:
    def __init__(self, clock: Clock, broker: MockBrokerAdapter, config: BacktestConfig) -> None:
        self._clock = clock
        self._broker = broker
        self._cfg = config
        dt = broker._cfg.dt_seconds
        self._periods_per_year = _YEAR_SECONDS / dt
        self._mds = MarketDataService(clock=clock, max_age_seconds=dt * 5)
        store = InMemoryOrderStore()
        self._oms = OrderManager(broker, store, clock)
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
            entry_params=StraddleEntryParams(
                contracts=config.entry_contracts, max_spread_fraction=config.max_spread_fraction
            ),
            exit_params=StraddleExitParams(),
            forecast_config=ForecastConfig(
                transaction_cost_buffer=config.transaction_cost_buffer,
                model_uncertainty_buffer=config.model_uncertainty_buffer,
            ),
        )

    async def run(self) -> BacktestReport:
        await self._broker.connect()
        resolver = InstrumentResolver(list(self._broker._instruments.values()))
        sym = self._cfg.symbol
        expiry = resolver.expiries(sym)[0]
        spot0 = Decimal(str(self._broker._cfg.spot0))
        strike = min(resolver.strikes(sym, expiry), key=lambda k: abs(k - spot0))
        straddle = resolver.resolve_straddle(sym, expiry=expiry, strike=strike)
        future_mult = float(straddle.future.spec.multiplier)
        symbols = [sym, straddle.future.symbol, straddle.call.symbol, straddle.put.symbol]

        starting_cash = (await self._broker.get_cash())[0].cash
        state = _RunState()
        step = 0
        seen: set[str] = set()

        async for quote in self._broker.stream_quotes(symbols):
            self._mds.on_quote(quote)
            seen.add(quote.instrument_symbol)
            if len(seen) < len(symbols):
                continue
            seen.clear()  # one full step of quotes ingested
            now = quote.timestamp

            u_mid = self._mds.mid(sym)
            if u_mid is not None:
                state.underlying_mids.append(float(u_mid))

            await self._maybe_enter(state, straddle, step, u_mid)
            await self._maybe_hedge(state, straddle, future_mult, step, now, u_mid)

            state.equity_curve.append(float(await self._mark_to_market(symbols)))
            step += 1
            if step >= self._cfg.steps:
                break

        return await self._build_report(state, starting_cash)

    async def _maybe_enter(
        self, state: _RunState, straddle: ResolvedStraddle, step: int, u_mid: Decimal | None
    ) -> None:
        if state.opened or u_mid is None or len(state.underlying_mids) < self._cfg.lookback:
            return
        call_q = self._mds.get_quote(straddle.call.symbol)
        put_q = self._mds.get_quote(straddle.put.symbol)
        if call_q is None or put_q is None:
            return
        closes = state.underlying_mids[-self._cfg.lookback :]
        rv = realized_vol(closes, periods_per_year=self._periods_per_year)
        forecast = VolatilityForecast(expected_rv=rv)
        candidate = StrikeCandidate(strike=straddle.strike, call_quote=call_q, put_quote=put_q)
        entry = self._strategy.evaluate_entry(
            spot=u_mid, candidates=[candidate], forecast=forecast, implied_vol=self._cfg.sigma
        )
        if not entry.enter or entry.selection is None:
            return
        call_req, put_req = self._strategy.build_leg_requests(
            entry.selection, straddle.call, straddle.put, id_prefix=f"bt-{step}"
        )
        result = await self._executor.open(call_req, put_req)
        if result.is_open:
            state.opened = True
            state.slippage += self._slippage(result.call_order) + self._slippage(result.put_order)
            logger.info("backtest_opened_straddle", step=step, strike=str(straddle.strike))

    async def _maybe_hedge(
        self,
        state: _RunState,
        straddle: ResolvedStraddle,
        future_mult: float,
        step: int,
        now: datetime,
        u_mid: Decimal | None,
    ) -> None:
        if not state.opened or u_mid is None:
            return
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
            future_multiplier=future_mult,
        )
        g = self._greeks.compute(positions, inputs)
        state.abs_deltas.append(abs(g.net_delta_units))
        decision = self._hedger.decide(
            current_delta_units=g.net_delta_units,
            future_multiplier=future_mult,
            now=now,
            gamma_units=g.net_gamma_units,
            cost_per_contract=self._cfg.cost_per_contract,
        )
        if not decision.should_hedge:
            return
        side = Side.BUY if decision.contracts > 0 else Side.SELL
        order = await self._oms.submit(
            OrderRequest(
                client_order_id=f"hedge-{step}",
                instrument_symbol=straddle.future.symbol,
                side=side,
                quantity=Decimal(abs(decision.contracts)),
                order_type=OrderType.MARKET,
            )
        )
        state.hedge_count += 1
        state.slippage += self._slippage(order)

    def _slippage(self, order: Order) -> Decimal:
        if order.filled_quantity <= 0:
            return Decimal("0")
        mid = self._mds.mid(order.request.instrument_symbol)
        if mid is None:
            return Decimal("0")
        inst = self._broker._instruments[order.request.instrument_symbol]
        return abs(order.average_fill_price - mid) * order.filled_quantity * inst.spec.multiplier

    async def _mark_to_market(self, symbols: list[str]) -> Decimal:
        cash = (await self._broker.get_cash())[0].cash
        value = cash
        for pos in await self._broker.get_positions():
            mid = self._mds.mid(pos.instrument_symbol)
            if mid is None:
                continue
            inst: Instrument = self._broker._instruments[pos.instrument_symbol]
            value += pos.quantity * mid * inst.spec.multiplier
        return value

    async def _build_report(self, state: _RunState, starting_cash: Decimal) -> BacktestReport:
        equity = state.equity_curve
        returns = metrics.returns_from_equity(equity).tolist()
        ppy = self._periods_per_year
        ann = metrics.annualized_return(equity, periods_per_year=ppy)
        mdd = metrics.max_drawdown(equity)
        fills = await self._broker.get_fills()
        commissions = sum((f.commission for f in fills), Decimal("0"))
        avg_dev = sum(state.abs_deltas) / len(state.abs_deltas) if state.abs_deltas else 0.0
        max_dev = max(state.abs_deltas) if state.abs_deltas else 0.0
        return BacktestReport(
            steps=len(equity),
            starting_equity=starting_cash,
            ending_equity=Decimal(str(equity[-1])) if equity else starting_cash,
            total_return=metrics.total_return(equity),
            annualized_return=ann,
            sharpe=metrics.sharpe_ratio(returns, periods_per_year=ppy),
            sortino=metrics.sortino_ratio(returns, periods_per_year=ppy),
            max_drawdown=mdd,
            calmar=metrics.calmar_ratio(ann, mdd),
            commissions=commissions,
            slippage=state.slippage,
            hedge_count=state.hedge_count,
            avg_abs_delta_deviation=avg_dev,
            max_abs_delta_deviation=max_dev,
            opened_position=state.opened,
            equity_curve=equity,
        )
