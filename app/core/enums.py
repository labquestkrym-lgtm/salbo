"""Domain enumerations shared across the system.

Kept free of any I/O or framework dependency so every layer can import them.
"""

from __future__ import annotations

from enum import StrEnum


class AppMode(StrEnum):
    """Run mode. Only ``LIVE`` may send real orders, and only behind the gates
    defined in :mod:`app.config.settings` (see ADR-0003)."""

    BACKTEST = "backtest"
    PAPER = "paper"
    SANDBOX = "sandbox"
    LIVE = "live"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AssetClass(StrEnum):
    EQUITY = "equity"
    FUTURE = "future"
    OPTION = "option"
    INDEX = "index"


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"


class OptionStyle(StrEnum):
    EUROPEAN = "european"
    AMERICAN = "american"


class SettlementType(StrEnum):
    CASH = "cash"
    PHYSICAL = "physical"


class PricingModel(StrEnum):
    """Which closed-form model prices an instrument."""

    BLACK_SCHOLES_MERTON = "bsm"  # options on spot/equity (with dividend yield)
    BLACK_76 = "black76"  # options on futures/forwards


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    MARKETABLE_LIMIT = "marketable_limit"
    PASSIVE_LIMIT = "passive_limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderState(StrEnum):
    """OMS lifecycle. Transitions are persisted; no fill is assumed without a
    broker acknowledgement (see R20)."""

    CREATED = "created"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class HedgeMode(StrEnum):
    TIME = "time"
    ABSOLUTE_DELTA = "absolute_delta"
    CASH_DELTA = "cash_delta"
    UNDERLYING_MOVE = "underlying_move"
    ATR = "atr"
    COMBINED = "combined"
    ADAPTIVE_DELTA_BAND = "adaptive_delta_band"


class RoundingMode(StrEnum):
    """Rounding for hedge contract sizing (R16)."""

    NEAREST = "nearest"
    FLOOR = "floor"
    CEIL = "ceil"
    CONSERVATIVE = "conservative"  # round toward larger hedge (reduce risk)
    MINIMAL_TURNOVER = "minimal_turnover"  # round toward no/least trade


class KillSwitchPolicy(StrEnum):
    """Action taken when a kill switch trips. Default is conservative HOLD;
    options are not panic-closed by default (illiquid books can worsen loss)."""

    HOLD = "hold"
    HEDGE_ONLY = "hedge_only"
    FLATTEN_FUTURES = "flatten_futures"
    CLOSE_ALL = "close_all"
