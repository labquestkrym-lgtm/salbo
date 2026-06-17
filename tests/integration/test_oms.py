"""OMS submission, idempotency, fills, recovery, reconciliation (R20)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.brokers.paper import PaperBrokerAdapter, PaperFillConfig
from app.core.clock import SimulatedClock
from app.core.enums import OrderState, OrderType, Side
from app.execution import InMemoryOrderStore, OrderManager
from app.models import Fill, OrderRequest

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


async def test_submit_fills_and_records_transitions() -> None:
    broker = _mock()
    store = InMemoryOrderStore()
    oms = OrderManager(broker, store, SimulatedClock(_NOW))
    order = await oms.submit(_buy(broker.future_symbol, "o1"))
    assert order.state is OrderState.FILLED
    states = [e.to_state for e in await store.list_events("o1")]
    assert OrderState.VALIDATED in states
    assert OrderState.SUBMITTED in states
    assert OrderState.ACKNOWLEDGED in states
    assert states[-1] is OrderState.FILLED


async def test_idempotent_submit_does_not_resend() -> None:
    broker = _mock()
    store = InMemoryOrderStore()
    oms = OrderManager(broker, store, SimulatedClock(_NOW))
    req = _buy(broker.future_symbol, "dup")
    first = await oms.submit(req)
    second = await oms.submit(req)  # would raise at broker if re-sent
    assert first is second
    assert len(await broker.get_fills()) == 1


async def test_partial_then_full_fill_via_apply_fill() -> None:
    clock = SimulatedClock(_NOW)
    market = _mock()
    paper = PaperBrokerAdapter(clock, market, PaperFillConfig())
    await paper.connect()
    store = InMemoryOrderStore()
    oms = OrderManager(paper, store, clock)
    req = _buy(market.future_symbol, "p1", qty="10")
    order = await oms.submit(req)  # paper acknowledges, no immediate fill
    assert order.state is OrderState.ACKNOWLEDGED

    await oms.apply_fill(_fill("p1", market.future_symbol, "4"))
    assert (await store.get_order("p1")).state is OrderState.PARTIALLY_FILLED
    await oms.apply_fill(_fill("p1", market.future_symbol, "6"))
    final = await store.get_order("p1")
    assert final.state is OrderState.FILLED
    assert final.filled_quantity == Decimal("10")


async def test_recover_resyncs_inflight_order_after_restart() -> None:
    clock = SimulatedClock(_NOW)
    market = _mock()
    paper = PaperBrokerAdapter(clock, market, PaperFillConfig(latency_ticks=1))
    await paper.connect()
    store = InMemoryOrderStore()
    oms1 = OrderManager(paper, store, clock)
    await oms1.submit(_buy(market.future_symbol, "r1"))
    # Broker fills it, but our (crashed) OMS never saw the fill.
    while (await paper.get_order("r1")).state is not OrderState.FILLED:
        await paper.process_pending()
    assert (await store.get_order("r1")).state is OrderState.ACKNOWLEDGED  # stale local view

    oms2 = OrderManager(paper, store, clock)  # "restart"
    recovered = await oms2.recover()
    assert len(recovered) == 1
    assert (await store.get_order("r1")).state is OrderState.FILLED


async def test_reconcile_unknown_order_goes_to_unknown() -> None:
    broker = _mock()
    store = InMemoryOrderStore()
    oms = OrderManager(broker, store, SimulatedClock(_NOW))
    # An order the broker has no record of (e.g. submit crashed before send).
    from app.models import Order

    await store.save_order(
        Order(request=_buy(broker.future_symbol, "ghost"), state=OrderState.SUBMITTED)
    )
    resolved = await oms.reconcile_order("ghost")
    assert resolved.state is OrderState.UNKNOWN


async def test_overfill_is_rejected() -> None:
    clock = SimulatedClock(_NOW)
    market = _mock()
    paper = PaperBrokerAdapter(clock, market, PaperFillConfig())
    await paper.connect()
    store = InMemoryOrderStore()
    oms = OrderManager(paper, store, clock)
    await oms.submit(_buy(market.future_symbol, "of1", qty="2"))
    from app.core.exceptions import OrderStateError

    with pytest.raises(OrderStateError):
        await oms.apply_fill(_fill("of1", market.future_symbol, "5"))


async def test_sql_store_persists_oms_state_across_restart(tmp_path: object) -> None:
    from pathlib import Path

    from app.repositories import Database, SqlOrderStore, build_engine

    db_path = Path(str(tmp_path)) / "oms.db"
    clock = SimulatedClock(_NOW)
    broker = _mock()
    await broker.connect()

    db1 = Database(build_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}"))
    await db1.create_all()
    oms1 = OrderManager(broker, SqlOrderStore(db1), clock)
    await oms1.submit(_buy(broker.future_symbol, "persist1"))
    await db1.dispose()

    # "Restart": brand-new store/OMS over the same database file.
    db2 = Database(build_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}"))
    store2 = SqlOrderStore(db2)
    reloaded = await store2.get_order("persist1")
    assert reloaded is not None
    assert reloaded.state is OrderState.FILLED
    assert reloaded.filled_quantity == Decimal("1")
    # The transition history was persisted too.
    events = await store2.list_events("persist1")
    assert OrderState.SUBMITTED in [e.to_state for e in events]
    await db2.dispose()


def _fill(coid: str, symbol: str, qty: str) -> Fill:
    return Fill(
        client_order_id=coid,
        instrument_symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal("100"),
        timestamp=_NOW,
    )
