"""Paper broker fill simulation: latency, partials, slippage (R10)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.brokers.paper import PaperBrokerAdapter, PaperFillConfig
from app.core.clock import SimulatedClock
from app.core.enums import OrderState, OrderType, Side
from app.models import OrderRequest

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _setup(config: PaperFillConfig) -> tuple[PaperBrokerAdapter, MockBrokerAdapter]:
    clock = SimulatedClock(_NOW)
    market = MockBrokerAdapter(clock, MockMarketConfig())
    return PaperBrokerAdapter(clock, market, config), market


def _buy(
    symbol: str,
    coid: str,
    qty: str,
    otype: OrderType = OrderType.MARKETABLE_LIMIT,
    limit: Decimal | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_order_id=coid,
        instrument_symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=otype,
        limit_price=limit,
    )


async def test_latency_delays_fill() -> None:
    paper, market = _setup(PaperFillConfig(latency_ticks=2, slippage_ticks=0))
    await paper.connect()
    ask = (await paper.get_quote(market.future_symbol)).ask
    await paper.place_order(_buy(market.future_symbol, "l1", "1", limit=ask + Decimal("100")))
    assert await paper.process_pending() == []  # tick 1: still within latency
    fills = await paper.process_pending()  # tick 2: eligible
    assert len(fills) == 1
    assert (await paper.get_order("l1")).state is OrderState.FILLED


async def test_partial_fills_over_multiple_ticks() -> None:
    # Book size is 10 per tick; a 25-lot order fills 10 + 10 + 5.
    paper, market = _setup(PaperFillConfig(latency_ticks=1, slippage_ticks=0))
    await paper.connect()
    ask = (await paper.get_quote(market.future_symbol)).ask
    await paper.place_order(_buy(market.future_symbol, "pf", "25", limit=ask + Decimal("100")))
    f1 = await paper.process_pending()
    assert f1[0].quantity == Decimal("10")
    assert (await paper.get_order("pf")).state is OrderState.PARTIALLY_FILLED
    await paper.process_pending()
    f3 = await paper.process_pending()
    assert f3[0].quantity == Decimal("5")
    assert (await paper.get_order("pf")).state is OrderState.FILLED


async def test_slippage_is_paid_beyond_the_touch() -> None:
    paper, market = _setup(PaperFillConfig(latency_ticks=1, slippage_ticks=3))
    await paper.connect()
    quote = await paper.get_quote(market.future_symbol)
    spec = await paper.get_contract_spec(market.future_symbol)
    await paper.place_order(_buy(market.future_symbol, "s1", "1", otype=OrderType.MARKET))
    fills = await paper.process_pending()
    assert fills[0].price == quote.ask + spec.tick_size * 3


async def test_passive_limit_below_market_does_not_fill() -> None:
    paper, market = _setup(PaperFillConfig(latency_ticks=1))
    await paper.connect()
    bid = (await paper.get_quote(market.future_symbol)).bid
    await paper.place_order(
        _buy(
            market.future_symbol, "pl", "1", otype=OrderType.PASSIVE_LIMIT, limit=bid - Decimal("5")
        )
    )
    for _ in range(5):
        assert await paper.process_pending() == []
    assert (await paper.get_order("pl")).filled_quantity == Decimal("0")


async def test_commission_charged_and_position_updated() -> None:
    paper, market = _setup(PaperFillConfig(latency_ticks=1, commission_per_contract=Decimal("2")))
    await paper.connect()
    await paper.place_order(_buy(market.future_symbol, "c1", "3", otype=OrderType.MARKET))
    await paper.process_pending()
    fills = await paper.get_fills()
    assert sum(f.commission for f in fills) == Decimal("6")
    positions = {p.instrument_symbol: p for p in await paper.get_positions()}
    assert positions[market.future_symbol].quantity == Decimal("3")
