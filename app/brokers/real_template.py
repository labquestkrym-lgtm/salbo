"""Real broker adapter TEMPLATE (R11).

This is a *scaffold*, not a working broker. Every method raises
``NotImplementedError`` until a concrete broker's API is wired in. It exists so
the integration surface and the safety guards are fixed before any real
credentials or endpoints are involved.

Two safety guards are enforced here (in addition to the global live-trading gate
in :mod:`app.config.settings`, ADR-0003):

1. **Construction guard** — in ``development``/``test`` environments, building an
   adapter pointed at a non-sandbox endpoint raises immediately.
2. **Trade guard** — any order-submitting method first calls
   :meth:`_require_can_trade_live`, which refuses unless either we are on a
   sandbox endpoint or *all* live-trading gates pass.

Do not implement the order methods against a production endpoint until mock,
backtest and paper modes are validated and the runbooks in OPERATIONS.md /
INCIDENT_RESPONSE.md are in place.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.brokers.base import BaseBrokerAdapter, BrokerCapabilities
from app.config.settings import AppSettings
from app.core.exceptions import LiveTradingNotAuthorizedError
from app.core.logging import get_logger
from app.models import (
    CashBalance,
    ContractSpec,
    Fill,
    Instrument,
    MarginInfo,
    Order,
    OrderRequest,
    Position,
    Quote,
    TradingSession,
)

logger = get_logger(__name__)

_NOT_IMPLEMENTED = (
    "RealBrokerAdapter is a template. Implement this against the chosen broker's "
    "API only after mock/backtest/paper are validated; live stays disabled by default."
)


class RealBrokerAdapter(BaseBrokerAdapter):
    name = "real_template"

    def __init__(self, *, settings: AppSettings, sandbox: bool = True) -> None:
        self._settings = settings
        self._sandbox = sandbox
        # Guard 1: never point a dev/test build at a non-sandbox endpoint.
        if settings.is_dev_or_test and not sandbox:
            raise LiveTradingNotAuthorizedError(
                "Refusing a non-sandbox RealBrokerAdapter in a development/test "
                "environment (ADR-0003)."
            )
        logger.info(
            "real_broker_template_constructed",
            sandbox=sandbox,
            environment=settings.app_environment.value,
        )

    def _require_can_trade_live(self) -> None:
        """Guard 2: order methods must not act on a production endpoint unless
        every live-trading gate passes."""
        if self._sandbox:
            return
        if not self._settings.is_live_trading_allowed():
            raise LiveTradingNotAuthorizedError(
                "Live trading not authorized: " + "; ".join(self._settings.live_trading_blockers())
            )

    @property
    def capabilities(self) -> BrokerCapabilities:
        # Conservative defaults; a real implementation advertises the broker's.
        return BrokerCapabilities(
            supports_order_modify=False,
            supports_greeks=False,
            supports_implied_vol=False,
            supports_streaming=True,
            supports_open_interest=False,
        )

    # --- connection ---------------------------------------------------------
    async def connect(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def disconnect(self) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def is_connected(self) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # --- reference data -----------------------------------------------------
    async def list_instruments(self, underlying_symbol: str | None = None) -> list[Instrument]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_contract_spec(self, symbol: str) -> ContractSpec:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_trading_session(self, symbol: str) -> TradingSession:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # --- market data --------------------------------------------------------
    async def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Quote]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # --- account ------------------------------------------------------------
    async def get_positions(self) -> list[Position]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_cash(self) -> list[CashBalance]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_margin(self) -> MarginInfo:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    # --- orders (guarded) ---------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        self._require_can_trade_live()
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def cancel_order(self, client_order_id: str) -> Order:
        self._require_can_trade_live()
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_order(self, client_order_id: str) -> Order:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_open_orders(self) -> list[Order]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def get_fills(self, since_sequence: int | None = None) -> list[Fill]:
        raise NotImplementedError(_NOT_IMPLEMENTED)
