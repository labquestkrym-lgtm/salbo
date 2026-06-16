"""Universal broker interface.

No strategy, risk, or persistence logic lives here — adapters only translate
between the broker's API and our domain models (ADR / separation of concerns).
All trading methods are ``async``. Concrete adapters: Mock, Paper, Real.

Capabilities differ between brokers (e.g. some expose Greeks/IV, some allow
order modification). ``capabilities`` advertises what is supported so higher
layers can adapt instead of assuming.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    supports_order_modify: bool = False
    supports_greeks: bool = False
    supports_implied_vol: bool = False
    supports_streaming: bool = True
    supports_open_interest: bool = False


class BaseBrokerAdapter(ABC):
    """Abstract broker. Implementations must not assume option and futures lot
    sizes/multipliers are equal — always read them from :class:`ContractSpec`."""

    name: str = "base"

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities: ...

    # --- Connection lifecycle ----------------------------------------------
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def is_connected(self) -> bool: ...

    async def reconnect(self) -> None:
        """Default: disconnect then connect. Override for smarter resync."""
        await self.disconnect()
        await self.connect()

    # --- Reference data -----------------------------------------------------
    @abstractmethod
    async def list_instruments(self, underlying_symbol: str | None = None) -> list[Instrument]: ...

    @abstractmethod
    async def get_contract_spec(self, symbol: str) -> ContractSpec: ...

    @abstractmethod
    async def get_trading_session(self, symbol: str) -> TradingSession: ...

    # --- Market data --------------------------------------------------------
    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Quote]:
        """Async stream of normalized quotes. Implementations carry monotonic
        ``sequence`` numbers so the market-data layer can detect gaps."""
        ...

    # --- Account ------------------------------------------------------------
    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def get_cash(self) -> list[CashBalance]: ...

    @abstractmethod
    async def get_margin(self) -> MarginInfo: ...

    # --- Orders -------------------------------------------------------------
    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel_order(self, client_order_id: str) -> Order: ...

    @abstractmethod
    async def get_order(self, client_order_id: str) -> Order: ...

    @abstractmethod
    async def get_open_orders(self) -> list[Order]: ...

    @abstractmethod
    async def get_fills(self, since_sequence: int | None = None) -> list[Fill]: ...

    async def modify_order(self, client_order_id: str, *, limit_price: object) -> Order:
        """Optional: amend price/qty if the broker supports it."""
        raise NotImplementedError(f"{self.name} does not support order modification")
