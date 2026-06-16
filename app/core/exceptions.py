"""Typed exception hierarchy.

Pricing/IV errors are typed so callers can distinguish recoverable market-quality
problems (illiquid, crossed quotes) from programming errors.
"""

from __future__ import annotations


class TradingBotError(Exception):
    """Base class for all application errors."""


# --- Configuration / safety -------------------------------------------------
class ConfigurationError(TradingBotError):
    """Invalid or incomplete configuration."""


class LiveTradingNotAuthorizedError(TradingBotError):
    """Raised when a live action is attempted without all gates satisfied."""


# --- Pricing / volatility ---------------------------------------------------
class PricingError(TradingBotError):
    """Base for pricing/volatility numerical issues."""


class ImpliedVolatilityError(PricingError):
    """IV could not be computed for a recoverable, market-quality reason."""


class PriceBelowIntrinsicError(ImpliedVolatilityError):
    """Option price is below intrinsic value — no real implied vol exists."""


class NoSolutionError(ImpliedVolatilityError):
    """Root finder failed to bracket/converge within configured bounds."""


class ExpiredOptionError(PricingError):
    """Time to expiry is zero or negative."""


class InvalidQuoteError(PricingError):
    """Bid/ask is missing, non-positive, or crossed."""


# --- Trading / OMS / risk ---------------------------------------------------
class RiskLimitBreachError(TradingBotError):
    """A risk limit would be or has been breached."""


class OrderStateError(TradingBotError):
    """Illegal order state transition or duplicate submission."""


class ReconciliationError(TradingBotError):
    """Local and broker state disagree beyond tolerance."""


class BrokerError(TradingBotError):
    """Broker connectivity or API error."""


class InstrumentResolutionError(TradingBotError):
    """Could not resolve or validate an instrument mapping."""


class MarketDataError(TradingBotError):
    """Market-data quality problem (stale, gapped, crossed, missing)."""
