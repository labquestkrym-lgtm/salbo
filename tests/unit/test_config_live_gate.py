"""Live-trading multi-gate (ADR-0003) and config validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config.settings import AppSettings, ParametersFile, RiskConfig, load_settings
from app.core.enums import AppMode, Environment, OrderType
from app.core.exceptions import ConfigurationError, LiveTradingNotAuthorizedError


def _fully_populated_risk() -> RiskConfig:
    return RiskConfig(
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


def test_default_settings_are_not_live() -> None:
    s = AppSettings()
    assert s.app_mode is AppMode.BACKTEST
    assert s.live_trading_enabled is False
    assert s.is_live_trading_allowed() is False


def test_example_config_loads_and_blocks_live() -> None:
    s = load_settings("configs/example.yaml")
    assert s.app_mode is AppMode.BACKTEST
    assert s.is_live_trading_allowed() is False
    # Mandatory limits are intentionally unset in the example.
    assert "risk.maximum_daily_loss is unset" in s.live_trading_blockers()


def test_all_gates_required_for_live() -> None:
    s = AppSettings(
        app_mode=AppMode.LIVE,
        app_environment=Environment.PRODUCTION,
        live_trading_enabled=True,
        live_confirmation_code="secret-code",  # type: ignore[arg-type]
    )
    s.params = ParametersFile()
    s.params.trading.live_trading_enabled = True
    s.params.risk = _fully_populated_risk()
    assert s.live_trading_blockers() == []
    assert s.is_live_trading_allowed() is True


@pytest.mark.parametrize(
    "mutate,expected_fragment",
    [
        (lambda s: setattr(s, "live_trading_enabled", False), "LIVE_TRADING_ENABLED is false"),
        (lambda s: setattr(s, "app_mode", AppMode.PAPER), "APP_MODE is not 'live'"),
        (lambda s: setattr(s, "live_confirmation_code", None), "LIVE_CONFIRMATION_CODE is not set"),
        (
            lambda s: setattr(s, "app_environment", Environment.DEVELOPMENT),
            "environment is development/test",
        ),
    ],
)
def test_removing_any_gate_blocks_live(mutate, expected_fragment: str) -> None:
    s = AppSettings(
        app_mode=AppMode.LIVE,
        app_environment=Environment.PRODUCTION,
        live_trading_enabled=True,
        live_confirmation_code="secret-code",  # type: ignore[arg-type]
    )
    s.params.trading.live_trading_enabled = True
    s.params.risk = _fully_populated_risk()
    mutate(s)
    blockers = s.live_trading_blockers()
    assert any(expected_fragment in b for b in blockers)
    assert s.is_live_trading_allowed() is False


def test_unset_risk_limits_block_live() -> None:
    risk = _fully_populated_risk()
    risk.maximum_gamma = Decimal("0")  # unset one mandatory limit
    assert "maximum_gamma" in risk.unset_mandatory_limits()
    with pytest.raises(ConfigurationError):
        risk.assert_ready_for_live()


def test_live_mode_forbidden_in_dev_environment() -> None:
    with pytest.raises(LiveTradingNotAuthorizedError):
        AppSettings(
            app_mode=AppMode.LIVE, app_environment=Environment.DEVELOPMENT
        ).assert_real_endpoint_allowed()


def test_market_option_order_type_rejected() -> None:
    from app.config.settings import ExecutionConfig

    with pytest.raises(ValueError, match="MARKET"):
        ExecutionConfig(option_order_type=OrderType.MARKET)
