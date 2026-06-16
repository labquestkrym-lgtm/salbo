"""RealBrokerAdapter template safety guards (R11)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.brokers.real_template import RealBrokerAdapter
from app.config.settings import AppSettings, ParametersFile, RiskConfig
from app.core.enums import AppMode, Environment, OrderType, Side
from app.core.exceptions import LiveTradingNotAuthorizedError
from app.models import OrderRequest


def _buy() -> OrderRequest:
    return OrderRequest(
        client_order_id="x",
        instrument_symbol="FUT",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
    )


def test_dev_environment_refuses_non_sandbox_endpoint() -> None:
    with pytest.raises(LiveTradingNotAuthorizedError):
        RealBrokerAdapter(
            settings=AppSettings(app_environment=Environment.DEVELOPMENT), sandbox=False
        )


def test_sandbox_construction_allowed_in_dev() -> None:
    adapter = RealBrokerAdapter(settings=AppSettings(), sandbox=True)
    assert adapter.name == "real_template"


async def test_methods_are_not_implemented() -> None:
    adapter = RealBrokerAdapter(settings=AppSettings(), sandbox=True)
    with pytest.raises(NotImplementedError):
        await adapter.connect()
    with pytest.raises(NotImplementedError):
        await adapter.get_positions()


async def test_non_sandbox_order_blocked_when_live_not_authorized() -> None:
    # Production env, but live trading gates are not satisfied -> trade guard fires.
    settings = AppSettings(app_environment=Environment.PRODUCTION, app_mode=AppMode.PAPER)
    adapter = RealBrokerAdapter(settings=settings, sandbox=False)
    with pytest.raises(LiveTradingNotAuthorizedError):
        await adapter.place_order(_buy())


async def test_sandbox_order_reaches_not_implemented() -> None:
    # On sandbox the trade guard passes, so we hit the (unimplemented) API call.
    adapter = RealBrokerAdapter(settings=AppSettings(), sandbox=True)
    with pytest.raises(NotImplementedError):
        await adapter.place_order(_buy())


def test_fully_gated_live_settings_would_authorize() -> None:
    # Sanity: with every gate satisfied, the trade guard would not block.
    settings = AppSettings(
        app_mode=AppMode.LIVE,
        app_environment=Environment.PRODUCTION,
        live_trading_enabled=True,
        live_confirmation_code="code",  # type: ignore[arg-type]
    )
    settings.params = ParametersFile()
    settings.params.trading.live_trading_enabled = True
    settings.params.risk = RiskConfig(
        maximum_daily_loss=Decimal("1000"),
        maximum_drawdown=Decimal("2000"),
        maximum_margin_utilization=Decimal("0.5"),
        maximum_cash_delta=Decimal("50000"),
        maximum_gamma=Decimal("500"),
        maximum_vega=Decimal("1000"),
        maximum_futures_position=10,
        stale_market_data_seconds=Decimal("5"),
        maximum_single_order_size=5,
        maximum_orders_per_minute=20,
    )
    adapter = RealBrokerAdapter(settings=settings, sandbox=False)
    # Guard passes (returns None) -> only the NotImplemented template body remains.
    adapter._require_can_trade_live()
