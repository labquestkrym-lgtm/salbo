"""Typed application settings.

Secrets come from environment variables (``.env`` locally). Strategy/risk/
execution parameters come from a YAML file (``configs/*.yaml``) so they are
versionable and reviewable. Both are validated by Pydantic.

Live trading is gated by multiple independent conditions (ADR-0003). Mandatory
risk limits MUST be explicitly set (non-zero) before live is allowed; we never
inject arbitrary "safe" defaults.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import AppMode, Environment, HedgeMode, KillSwitchPolicy, OrderType
from app.core.exceptions import ConfigurationError, LiveTradingNotAuthorizedError


# ---------------------------------------------------------------------------
# Parameter blocks (loaded from YAML)
# ---------------------------------------------------------------------------
class StrategyConfig(BaseModel):
    symbol: str = "PLACEHOLDER"
    option_structure: str = "straddle"
    contracts: int = Field(default=1, ge=1)
    min_days_to_expiry: int = Field(default=10, ge=0)
    max_days_to_expiry: int = Field(default=45, ge=0)
    target_delta: Decimal = Decimal("0")
    hedge_mode: HedgeMode = HedgeMode.ADAPTIVE_DELTA_BAND
    hedge_to_zero: bool = False
    minimum_edge: Decimal = Decimal("0")
    maximum_option_spread_percent: Decimal = Decimal("0")
    maximum_futures_spread_ticks: int = 0
    minimum_option_volume: int = 0
    minimum_open_interest: int = 0
    max_strike_distance_from_atm: Decimal = Decimal("0")
    permitted_expirations: list[str] = Field(default_factory=list)
    blackout_dates: list[str] = Field(default_factory=list)
    permitted_open_hours_utc: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_expiry_window(self) -> StrategyConfig:
        if self.max_days_to_expiry < self.min_days_to_expiry:
            raise ValueError("max_days_to_expiry must be >= min_days_to_expiry")
        return self


class RiskConfig(BaseModel):
    """Hard risk limits. Mandatory limits default to 0 == 'unset'; live trading
    is refused until they are explicitly configured (see ``assert_ready_for_live``)."""

    maximum_daily_loss: Decimal = Decimal("0")
    maximum_drawdown: Decimal = Decimal("0")
    maximum_margin_utilization: Decimal = Decimal("0")  # fraction 0..1
    maximum_cash_delta: Decimal = Decimal("0")
    maximum_net_delta: Decimal = Decimal("0")
    maximum_gamma: Decimal = Decimal("0")
    maximum_vega: Decimal = Decimal("0")
    maximum_theta: Decimal = Decimal("0")
    maximum_futures_position: int = 0
    maximum_option_contracts: int = 0
    maximum_single_order_size: int = 0
    maximum_turnover: Decimal = Decimal("0")
    maximum_orders_per_minute: int = 0
    maximum_spread: Decimal = Decimal("0")
    maximum_slippage_ticks: int = 0
    minimum_cash_reserve: Decimal = Decimal("0")
    stale_market_data_seconds: Decimal = Decimal("0")
    maximum_position_mismatch: Decimal = Decimal("0")
    kill_switch_policy: KillSwitchPolicy = KillSwitchPolicy.HOLD

    # Limits that MUST be set before live trading is permitted.
    _MANDATORY_FOR_LIVE: tuple[str, ...] = (
        "maximum_daily_loss",
        "maximum_drawdown",
        "maximum_margin_utilization",
        "maximum_cash_delta",
        "maximum_gamma",
        "maximum_vega",
        "maximum_futures_position",
        "stale_market_data_seconds",
        "maximum_single_order_size",
        "maximum_orders_per_minute",
    )

    def unset_mandatory_limits(self) -> list[str]:
        """Return mandatory limits still left at their 'unset' (0) value."""
        unset: list[str] = []
        for name in self._MANDATORY_FOR_LIVE:
            value = getattr(self, name)
            if value is None or Decimal(str(value)) == 0:
                unset.append(name)
        return unset

    def assert_ready_for_live(self) -> None:
        missing = self.unset_mandatory_limits()
        if missing:
            raise ConfigurationError(
                "Live trading blocked: mandatory risk limits are unset (0): " + ", ".join(missing)
            )


class ExecutionConfig(BaseModel):
    option_order_type: OrderType = OrderType.LIMIT
    futures_order_type: OrderType = OrderType.MARKETABLE_LIMIT
    maximum_slippage_ticks: int = 0
    order_timeout_seconds: Decimal = Decimal("0")
    maximum_orders_per_minute: int = 0
    leg_execution: str = "sequential"  # sequential | parallel
    max_seconds_between_legs: Decimal = Decimal("0")

    @model_validator(mode="after")
    def _check_option_order_type(self) -> ExecutionConfig:
        # R: options default must not be plain market orders.
        if self.option_order_type is OrderType.MARKET:
            raise ValueError(
                "option_order_type must not be MARKET by default (use LIMIT / MARKETABLE_LIMIT)"
            )
        return self


class TradingConfig(BaseModel):
    live_trading_enabled: bool = False
    symbol: str = "PLACEHOLDER"


class ParametersFile(BaseModel):
    """Full validated contents of a configs/*.yaml file."""

    trading: TradingConfig = Field(default_factory=TradingConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


# ---------------------------------------------------------------------------
# Top-level settings (env + YAML)
# ---------------------------------------------------------------------------
class AppSettings(BaseSettings):
    """Environment-driven settings. Secrets live here; tunables come from YAML."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
    )

    app_mode: AppMode = AppMode.BACKTEST
    app_environment: Environment = Environment.DEVELOPMENT

    # Live gates
    live_trading_enabled: bool = False
    live_confirmation_code: SecretStr | None = None

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "tradingbot"
    postgres_user: str = "tradingbot"
    postgres_password: SecretStr = SecretStr("")

    redis_url: str = "redis://localhost:6379/0"
    api_auth_token: SecretStr = SecretStr("")

    # Broker
    broker_name: str = "mock"
    broker_api_key: SecretStr | None = None
    broker_api_secret: SecretStr | None = None
    broker_account_id: str | None = None
    broker_endpoint: str | None = None

    # Notifications
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # Loaded parameters (populated by ``load_settings``)
    params: ParametersFile = Field(default_factory=ParametersFile)

    @property
    def database_url(self) -> str:
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_dev_or_test(self) -> bool:
        return self.app_environment in (Environment.DEVELOPMENT, Environment.TEST)

    def assert_real_endpoint_allowed(self) -> None:
        """Refuse a non-sandbox broker endpoint in development/test (ADR-0003)."""
        if self.is_dev_or_test and self.app_mode is AppMode.LIVE:
            raise LiveTradingNotAuthorizedError(
                "Live mode is forbidden in development/test environments."
            )

    def live_trading_blockers(self) -> list[str]:
        """Return reasons live trading is NOT permitted. Empty == all gates pass
        (the final confirmation-code echo is still checked at start time)."""
        blockers: list[str] = []
        if self.app_mode is not AppMode.LIVE:
            blockers.append("APP_MODE is not 'live'")
        if not self.live_trading_enabled:
            blockers.append("LIVE_TRADING_ENABLED is false")
        if not self.params.trading.live_trading_enabled:
            blockers.append("trading.live_trading_enabled is false in YAML")
        if (
            self.live_confirmation_code is None
            or not self.live_confirmation_code.get_secret_value()
        ):
            blockers.append("LIVE_CONFIRMATION_CODE is not set")
        if self.app_environment in (Environment.DEVELOPMENT, Environment.TEST):
            blockers.append("environment is development/test")
        blockers.extend(
            f"risk.{name} is unset" for name in self.params.risk.unset_mandatory_limits()
        )
        return blockers

    def is_live_trading_allowed(self) -> bool:
        return not self.live_trading_blockers()


def load_settings(config_path: str | Path | None = None) -> AppSettings:
    """Build settings from environment + an optional YAML parameters file."""
    settings = AppSettings()
    if config_path is not None:
        path = Path(config_path)
        if not path.exists():
            raise ConfigurationError(f"Config file not found: {path}")
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # The optional top-level `app:` block can override mode/environment.
        app_block = raw.get("app", {})
        if "mode" in app_block:
            settings.app_mode = AppMode(app_block["mode"])
        if "environment" in app_block:
            settings.app_environment = Environment(app_block["environment"])
        settings.params = ParametersFile.model_validate(
            {k: v for k, v in raw.items() if k != "app"}
        )
    settings.assert_real_endpoint_allowed()
    return settings
