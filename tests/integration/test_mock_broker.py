"""MockBrokerAdapter: streaming, reference data, and bid/ask fills (R9)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from app.core.enums import AssetClass, OrderType, Side
from app.core.exceptions import OrderStateError
from app.models import OrderRequest


def _broker() -> MockBrokerAdapter:
    clock = SimulatedClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    return MockBrokerAdapter(clock, MockMarketConfig(seed=7, max_stream_steps=20))


async def test_connect_and_universe() -> None:
    broker = _broker()
    await broker.connect()
    assert await broker.is_connected() is True
    instruments = await broker.list_instruments()
    classes = {i.asset_class for i in instruments}
    assert classes == {AssetClass.EQUITY, AssetClass.FUTURE, AssetClass.OPTION}
    options = [i for i in instruments if i.asset_class is AssetClass.OPTION]
    assert len(options) == 10  # 5 strikes x {call, put}


async def test_quotes_are_two_sided_and_not_crossed() -> None:
    broker = _broker()
    quote = await broker.get_quote(broker.future_symbol)
    assert quote.has_two_sided_market
    assert not quote.is_crossed
    assert quote.mid is not None
    assert quote.sequence is not None


async def test_stream_has_monotonic_sequence_and_moves_price() -> None:
    broker = _broker()
    symbols = [broker._cfg.underlying_symbol, broker.future_symbol]
    seqs: list[int] = []
    mids: list[Decimal] = []
    async for q in broker.stream_quotes(symbols):
        assert q.sequence is not None
        seqs.append(q.sequence)
        if q.instrument_symbol == broker._cfg.underlying_symbol and q.mid is not None:
            mids.append(q.mid)
        if len(seqs) >= 2 * len(symbols) * 5:
            break
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # strictly increasing, no repeats
    assert len(set(mids)) > 1  # price actually moved


async def test_marketable_buy_fills_at_ask_and_charges_commission() -> None:
    broker = _broker()
    fut = broker.future_symbol
    quote = await broker.get_quote(fut)
    ask = quote.ask
    assert ask is not None
    req = OrderRequest(
        client_order_id="o1",
        instrument_symbol=fut,
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.MARKETABLE_LIMIT,
        limit_price=ask + Decimal("100"),
    )
    order = await broker.place_order(req)
    assert order.filled_quantity == Decimal("2")
    assert order.average_fill_price > 0
    positions = {p.instrument_symbol: p for p in await broker.get_positions()}
    assert positions[fut].quantity == Decimal("2")
    fills = await broker.get_fills()
    assert len(fills) == 1
    assert fills[0].commission == broker._cfg.commission_per_contract * 2


async def test_passive_limit_does_not_fill() -> None:
    broker = _broker()
    fut = broker.future_symbol
    quote = await broker.get_quote(fut)
    assert quote.bid is not None
    req = OrderRequest(
        client_order_id="passive",
        instrument_symbol=fut,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.PASSIVE_LIMIT,
        limit_price=quote.bid - Decimal("1"),
    )
    order = await broker.place_order(req)
    assert order.filled_quantity == Decimal("0")
    assert await broker.get_positions() == []


async def test_duplicate_client_order_id_rejected() -> None:
    broker = _broker()
    fut = broker.future_symbol
    req = OrderRequest(
        client_order_id="dup",
        instrument_symbol=fut,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
    )
    await broker.place_order(req)
    with pytest.raises(OrderStateError):
        await broker.place_order(req)
