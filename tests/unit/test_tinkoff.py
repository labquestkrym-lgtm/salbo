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
from app.core.enums import AssetClass, Environment, OptionType, OrderState, OrderType, Side
from app.core.exceptions import (
    BrokerError,
    InstrumentResolutionError,
    LiveTradingNotAuthorizedError,
)
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
