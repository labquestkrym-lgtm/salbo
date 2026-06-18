"""Mandatory failure scenarios from the brief (section 6).

Covers cases not already exercised elsewhere: order timeout that was actually
filled, connection lost during open, duplicate broker fill events, a price gap,
and a margin-utilization breach. (Both-legs / one-leg / restart / desync /
staleness / wide-spread are covered in their own test modules.)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.brokers.paper import PaperBrokerAdapter, PaperFillConfig
from app.config.settings import RiskConfig
from app.core.clock import SimulatedClock
from app.core.enums import OrderState, OrderType, Side
from app.core.exceptions import BrokerError
from app.execution import InMemoryOrderStore, OrderManager
from app.instruments import InstrumentResolver
from app.models import Fill, Order, OrderRequest
from app.portfolio import PortfolioGreeksEngine, PricingInputs, UnderlyingState
from app.risk import KillSwitch, KillSwitchTrigger, RiskManager, RiskState

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _mock() -> MockBrokerAdapter:
    return MockBrokerAdapter(SimulatedClock(_NOW), MockMarketConfig())


def _buy(symbol: str, coid: str, qty: str = "1") -> OrderRequest:
    return OrderRequest(
        client_order_id=coid,
        instrument_symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
    )


# --- Scenario 6: order timed out at send, but was actually filled ----------
class _TimeoutThenFilledBroker(MockBrokerAdapter):
    async def place_order(self, request: OrderRequest) -> Order:
        # The order reached the exchange and filled, but our send call "timed
        # out" — we get an error and must NOT assume the outcome.
        now = self._clock.now()
        self._orders[request.client_order_id] = Order(
            request=request,
            state=OrderState.FILLED,
            broker_order_id="bk-late",
            filled_quantity=request.quantity,
            average_fill_price=Decimal("100"),
            created_at=now,
            updated_at=now,
        )
        raise BrokerError("send timeout")


async def test_order_timeout_then_reconciled_to_filled() -> None:
    broker = _TimeoutThenFilledBroker(SimulatedClock(_NOW), MockMarketConfig())
    await broker.connect()
    oms = OrderManager(broker, InMemoryOrderStore(), SimulatedClock(_NOW))
    order = await oms.submit(_buy(broker.future_symbol, "t1"))
    assert order.state is OrderState.UNKNOWN  # never assumed filled
    resolved = await oms.reconcile_order("t1")
    assert resolved.state is OrderState.FILLED  # broker is the source of truth


# --- Scenario 7: connection lost during open -------------------------------
class _ConnLostBroker(MockBrokerAdapter):
    async def place_order(self, request: OrderRequest) -> Order:
        raise BrokerError("connection lost")


async def test_connection_lost_during_open_marks_unknown_not_filled() -> None:
    broker = _ConnLostBroker(SimulatedClock(_NOW), MockMarketConfig())
    await broker.connect()
    oms = OrderManager(broker, InMemoryOrderStore(), SimulatedClock(_NOW))
    order = await oms.submit(_buy(broker.future_symbol, "c1"))
    assert order.state is OrderState.UNKNOWN
    assert order.filled_quantity == Decimal("0")  # no optimistic fill


# --- Scenario 16: duplicate broker fill events -----------------------------
async def test_duplicate_fill_event_is_idempotent() -> None:
    clock = SimulatedClock(_NOW)
    market = _mock()
    paper = PaperBrokerAdapter(clock, market, PaperFillConfig())
    await paper.connect()
    oms = OrderManager(paper, InMemoryOrderStore(), clock)
    await oms.submit(_buy(market.future_symbol, "d1", qty="5"))
    fill = Fill(
        client_order_id="d1",
        instrument_symbol=market.future_symbol,
        side=Side.BUY,
        quantity=Decimal("5"),
        price=Decimal("100"),
        timestamp=_NOW,
        broker_fill_id="bf-1",
    )
    first = await oms.apply_fill(fill)
    second = await oms.apply_fill(fill)  # same broker_fill_id -> ignored
    assert first.filled_quantity == Decimal("5")
    assert second.filled_quantity == Decimal("5")  # not double-counted
    assert second.state is OrderState.FILLED


# --- Scenario 11: price gap does not break valuation -----------------------
async def test_price_gap_recomputes_greeks_without_error() -> None:
    broker = _mock()
    await broker.connect()
    resolver = InstrumentResolver(list(broker._instruments.values()))
    expiry = resolver.expiries("XYZ")[0]
    straddle = resolver.resolve_straddle("XYZ", expiry=expiry, strike=Decimal("100"))
    engine = PortfolioGreeksEngine(broker._instruments)

    from app.models import Position

    positions = [
        Position(instrument_symbol=straddle.call.symbol, quantity=Decimal("5")),
        Position(instrument_symbol=straddle.put.symbol, quantity=Decimal("5")),
    ]
    sigma = {straddle.call.symbol: 0.20, straddle.put.symbol: 0.20}

    def greeks_at(spot: float) -> float:
        inputs = PricingInputs(
            valuation_time=_NOW,
            underlying=UnderlyingState(spot=spot, rate=0.05),
            sigma_by_symbol=sigma,
            mid_by_symbol={},
            future_multiplier=50.0,
        )
        return engine.compute(positions, inputs).net_delta_units

    before = greeks_at(100.0)
    after = greeks_at(130.0)  # 30% overnight gap up
    assert before != after
    assert after == after  # finite (not NaN)


# --- Scenario 14: margin-utilization breach trips the kill switch ----------
async def test_margin_breach_trips_kill_switch() -> None:
    config = RiskConfig(maximum_margin_utilization=Decimal("0.5"))
    ks = KillSwitch(SimulatedClock(_NOW))
    mgr = RiskManager(config, ks)
    assessment = mgr.evaluate(RiskState(margin_utilization=Decimal("0.9")))
    assert not assessment.allow_new_positions
    assert ks.is_tripped
    assert ks.events[0].trigger is KillSwitchTrigger.RISK_LIMIT_BREACH
