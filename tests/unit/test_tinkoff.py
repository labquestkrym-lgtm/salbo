"""T-Invest conversion/mapping layer and adapter safety guards.

The networked adapter methods are not exercised (no SDK / token / network here);
these tests pin the pure conversions, the instrument-field contract, and the
construction/trade guards.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.brokers.tinkoff import TInvestBrokerAdapter
from app.brokers.tinkoff.conversions import (
    decimal_to_quotation,
    direction_to_side,
    order_status_to_state,
    quotation_to_decimal,
    side_to_direction,
)
from app.brokers.tinkoff.instruments import future_to_instrument, option_to_instrument
from app.config.settings import AppSettings
from app.core.clock import SimulatedClock
from app.core.enums import (
    AssetClass,
    Environment,
    OptionType,
    OrderState,
    OrderType,
    PricingModel,
    Side,
)
from app.core.exceptions import (
    BrokerError,
    InstrumentResolutionError,
    LiveTradingNotAuthorizedError,
)
from app.instruments import InstrumentResolver
from app.models import OrderRequest

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


# --- conversions ------------------------------------------------------------
def test_quotation_round_trip_positive() -> None:
    assert quotation_to_decimal(123, 450_000_000) == Decimal("123.45")
    assert decimal_to_quotation(Decimal("123.45")) == (123, 450_000_000)


def test_quotation_round_trip_negative() -> None:
    assert quotation_to_decimal(-1, -500_000_000) == Decimal("-1.5")
    assert decimal_to_quotation(Decimal("-1.5")) == (-1, -500_000_000)


def test_side_direction_mapping() -> None:
    assert side_to_direction(Side.BUY) == 1
    assert side_to_direction(Side.SELL) == 2
    assert direction_to_side(1) is Side.BUY
    assert direction_to_side(2) is Side.SELL


def test_order_status_mapping() -> None:
    assert order_status_to_state(1) is OrderState.FILLED
    assert order_status_to_state(5) is OrderState.PARTIALLY_FILLED
    assert order_status_to_state(4) is OrderState.ACKNOWLEDGED
    assert order_status_to_state(2) is OrderState.REJECTED
    assert order_status_to_state(3) is OrderState.CANCELLED
    assert order_status_to_state(0) is OrderState.UNKNOWN
    assert order_status_to_state(99) is OrderState.UNKNOWN


# --- instrument mapping (duck-typed fakes) ---------------------------------
def _q(units: int, nano: int = 0) -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano)


def test_future_to_instrument() -> None:
    fut = SimpleNamespace(
        figi="FUT-SBER-0326",
        basic_asset="SBER",
        lot=1,
        currency="rub",
        min_price_increment=_q(0, 10_000_000),  # 0.01
        min_price_increment_amount=_q(12, 500_000_000),  # 12.5 per tick
        expiration_date=datetime(2026, 3, 20, tzinfo=UTC),
    )
    inst = future_to_instrument(fut)
    assert inst.asset_class is AssetClass.FUTURE
    assert inst.spec.tick_size == Decimal("0.01")
    assert inst.spec.tick_value == Decimal("12.5")
    assert inst.spec.multiplier == Decimal("1250")  # 12.5 / 0.01
    assert inst.spec.currency == "RUB"
    assert inst.expiry == date(2026, 3, 20)


def test_option_to_instrument_call_and_put() -> None:
    base = {
        "figi": "OPT-SBER-300-C",
        "basic_asset": "SBER",
        "lot": 1,
        "currency": "rub",
        "min_price_increment": _q(0, 10_000_000),
        "expiration_date": datetime(2026, 3, 20, tzinfo=UTC),
        "strike_price": _q(300, 0),
    }
    call = option_to_instrument(SimpleNamespace(**base, direction=2))
    put = option_to_instrument(SimpleNamespace(**{**base, "figi": "OPT-SBER-300-P"}, direction=1))
    assert call.option_type is OptionType.CALL
    assert put.option_type is OptionType.PUT
    assert call.strike == Decimal("300")
    assert call.asset_class is AssetClass.OPTION
    # No min_price_increment_amount -> multiplier defaults to 1.
    assert call.spec.multiplier == Decimal("1")


def test_unknown_option_direction_raises() -> None:
    opt = SimpleNamespace(
        figi="X",
        basic_asset="SBER",
        lot=1,
        currency="rub",
        min_price_increment=_q(0, 10_000_000),
        expiration_date=datetime(2026, 3, 20, tzinfo=UTC),
        strike_price=_q(300, 0),
        direction=0,
    )
    with pytest.raises(InstrumentResolutionError):
        option_to_instrument(opt)


# --- instrument-universe assembly (fake client, no network) ----------------
class _FakeInstruments:
    def __init__(self, futures: list, options: list, chain: list) -> None:
        self._futures, self._options, self._chain = futures, options, chain
        self.seen_uid: str | None = None

    async def futures(self) -> SimpleNamespace:
        return SimpleNamespace(instruments=self._futures)

    async def options(self) -> SimpleNamespace:
        return SimpleNamespace(instruments=self._options)

    async def options_by(self, basic_asset_uid: str) -> SimpleNamespace:
        self.seen_uid = basic_asset_uid
        return SimpleNamespace(instruments=self._chain)


class _FakeClient:
    def __init__(self, instruments: _FakeInstruments) -> None:
        self.instruments = instruments


def _fut(uid: str | None = "asset-uid-1") -> SimpleNamespace:
    kw: dict[str, object] = {
        "figi": "FUT-SBER-0326",
        "basic_asset": "SBER",
        "lot": 1,
        "currency": "rub",
        "min_price_increment": _q(0, 10_000_000),
        "min_price_increment_amount": _q(12, 500_000_000),
        "expiration_date": datetime(2026, 3, 20, tzinfo=UTC),
    }
    if uid is not None:
        kw["basic_asset_uid"] = uid
    return SimpleNamespace(**kw)


def _opt(figi: str, direction: int) -> SimpleNamespace:
    return SimpleNamespace(
        figi=figi,
        basic_asset="SBER",
        lot=1,
        currency="rub",
        min_price_increment=_q(0, 10_000_000),
        expiration_date=datetime(2026, 3, 20, tzinfo=UTC),
        strike_price=_q(300, 0),
        direction=direction,
    )


def test_list_instruments_assembles_chain_via_options_by() -> None:
    import asyncio

    chain = [_opt("OPT-C", 2), _opt("OPT-P", 1)]
    fake = _FakeClient(_FakeInstruments([_fut()], [], chain))
    adapter = TInvestBrokerAdapter(AppSettings(), SimulatedClock(_NOW), sandbox=True)
    adapter._client = fake  # type: ignore[attr-defined]
    insts = asyncio.run(adapter.list_instruments("SBER"))
    # Resolvable as an options-on-futures straddle (future is the spot reference).
    resolver = InstrumentResolver(insts)
    straddle = resolver.resolve_straddle_on_future(
        "SBER", expiry=date(2026, 3, 20), strike=Decimal("300")
    )
    assert straddle.underlying.asset_class is AssetClass.FUTURE
    assert straddle.call.pricing_model is PricingModel.BLACK_76
    assert straddle.put.pricing_model is PricingModel.BLACK_76
    # The chain was fetched via options_by keyed off the future's basic-asset uid.
    assert fake.instruments.seen_uid == "asset-uid-1"


def test_list_instruments_falls_back_to_dump_without_asset_uid() -> None:
    import asyncio

    dump = [_opt("OPT-C", 2), _opt("OPT-P", 1)]
    fake = _FakeClient(_FakeInstruments([_fut(uid=None)], dump, []))
    adapter = TInvestBrokerAdapter(AppSettings(), SimulatedClock(_NOW), sandbox=True)
    adapter._client = fake  # type: ignore[attr-defined]
    insts = asyncio.run(adapter.list_instruments("SBER"))
    options = [i for i in insts if i.asset_class is AssetClass.OPTION]
    assert len(options) == 2  # degraded to the filtered full dump
    assert fake.instruments.seen_uid is None  # options_by never reached (no uid)


# --- adapter safety guards (no SDK / network needed) -----------------------
def _settings(**kw: object) -> AppSettings:
    return AppSettings(**kw)  # type: ignore[arg-type]


def test_dev_environment_refuses_non_sandbox() -> None:
    with pytest.raises(LiveTradingNotAuthorizedError):
        TInvestBrokerAdapter(
            _settings(app_environment=Environment.DEVELOPMENT), SimulatedClock(_NOW), sandbox=False
        )


def test_non_sandbox_order_blocked_when_live_not_authorized() -> None:
    # Production env, live gates unmet -> trade guard fires before any SDK call.
    settings = _settings(app_environment=Environment.PRODUCTION)
    adapter = TInvestBrokerAdapter(settings, SimulatedClock(_NOW), sandbox=False, account_id="acc")
    req = OrderRequest(
        client_order_id="o1",
        instrument_symbol="FUT",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
    )
    import asyncio

    with pytest.raises(LiveTradingNotAuthorizedError):
        asyncio.run(adapter.place_order(req))


def test_connect_without_sdk_raises_clear_error() -> None:
    # tinkoff-investments is an optional dep, not installed in CI.
    adapter = TInvestBrokerAdapter(_settings(), SimulatedClock(_NOW), sandbox=True)
    import asyncio

    with pytest.raises(BrokerError, match="tinkoff-investments"):
        asyncio.run(adapter.connect())


def test_methods_require_connection() -> None:
    adapter = TInvestBrokerAdapter(_settings(), SimulatedClock(_NOW), sandbox=True)
    import asyncio

    with pytest.raises(BrokerError, match="not connected"):
        asyncio.run(adapter.get_quote("FUT"))


# --- account/order/fill mapping (fake services, no network) ----------------
class _AsyncReturn:
    """A stand-in async SDK method that records kwargs and returns a fixed value."""

    def __init__(self, value: object) -> None:
        self._value = value
        self.kwargs: dict = {}

    async def __call__(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return self._value


def _money(units: int, nano: int = 0, currency: str = "rub") -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano, currency=currency)


def test_get_margin_maps_attributes() -> None:
    import asyncio

    margin = SimpleNamespace(
        liquid_portfolio=_money(100000),
        starting_margin=_money(30000),
        minimal_margin=_money(20000),
    )
    client = SimpleNamespace(operations=SimpleNamespace(get_margin_attributes=_AsyncReturn(margin)))
    adapter = TInvestBrokerAdapter(
        _settings(app_environment=Environment.PRODUCTION),
        SimulatedClock(_NOW),
        sandbox=False,
        account_id="acc",
    )
    adapter._client = client  # type: ignore[attr-defined]
    m = asyncio.run(adapter.get_margin())
    assert m.used_margin == Decimal("30000")
    assert m.available_margin == Decimal("70000")  # liquid - starting
    assert m.maintenance_margin == Decimal("20000")
    assert m.currency == "RUB"


def test_get_margin_unavailable_on_sandbox() -> None:
    import asyncio

    adapter = TInvestBrokerAdapter(_settings(), SimulatedClock(_NOW), sandbox=True)
    with pytest.raises(BrokerError, match="sandbox"):
        asyncio.run(adapter.get_margin())


def test_get_open_orders_maps_states() -> None:
    import asyncio

    state = SimpleNamespace(
        order_id="o1",
        figi="FUT",
        direction=1,  # BUY
        lots_requested=5,
        lots_executed=2,
        execution_report_status=5,  # PARTIALLYFILL
        average_position_price=_money(100),
    )
    client = SimpleNamespace(
        sandbox=SimpleNamespace(get_sandbox_orders=_AsyncReturn(SimpleNamespace(orders=[state])))
    )
    adapter = TInvestBrokerAdapter(_settings(), SimulatedClock(_NOW), sandbox=True)
    adapter._client = client  # type: ignore[attr-defined]
    orders = asyncio.run(adapter.get_open_orders())
    assert len(orders) == 1
    o = orders[0]
    assert o.state is OrderState.PARTIALLY_FILLED
    assert o.filled_quantity == Decimal("2")
    assert o.request.side is Side.BUY
    assert o.average_fill_price == Decimal("100")


def test_get_fills_maps_trades_and_skips_non_trade_ops() -> None:
    import asyncio

    trade = SimpleNamespace(trade_id="t1", date_time=_NOW, quantity=3, price=_money(150))
    op_buy = SimpleNamespace(id="op1", figi="FUT", payment=_money(-450), date=_NOW, trades=[trade])
    op_commission = SimpleNamespace(id="op2", figi="FUT", payment=_money(-1), date=_NOW, trades=[])
    client = SimpleNamespace(
        sandbox=SimpleNamespace(
            get_sandbox_operations=_AsyncReturn(SimpleNamespace(operations=[op_buy, op_commission]))
        )
    )
    adapter = TInvestBrokerAdapter(_settings(), SimulatedClock(_NOW), sandbox=True)
    adapter._client = client  # type: ignore[attr-defined]
    fills = asyncio.run(adapter.get_fills())
    assert len(fills) == 1  # commission op (no trades) skipped
    f = fills[0]
    assert f.side is Side.BUY  # negative payment -> bought
    assert f.quantity == Decimal("3")
    assert f.price == Decimal("150")
    assert f.broker_fill_id == "t1"
