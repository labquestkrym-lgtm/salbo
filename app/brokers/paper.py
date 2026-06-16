"""Paper-trading broker (R10).

Uses real market data (here, the mock universe — in paper mode it would be a
live read-only feed) and simulates execution realistically:

* fills cross the **bid/ask**, never the last price;
* **latency** — an order is not eligible to fill until ``latency_ticks`` quote
  updates after submission;
* **partial fills** — a fill is capped by top-of-book size (and an optional
  per-tick cap), so large orders fill over several ticks;
* **slippage** — marketable fills pay ``slippage_ticks`` beyond the touch;
* **commission** per contract;
* **passive limits** rest and fill only when the market trades through.

Fills are driven by :meth:`process_pending`, called once per quote tick by the
market-data loop, so timing is explicit and testable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

from app.brokers.base import BaseBrokerAdapter, BrokerCapabilities
from app.brokers.mock import MockBrokerAdapter
from app.core.clock import Clock
from app.core.enums import OrderState, OrderType, Side
from app.core.exceptions import OrderStateError
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


@dataclass(slots=True)
class PaperFillConfig:
    latency_ticks: int = 1
    slippage_ticks: int = 1
    max_fill_per_tick: Decimal | None = None  # None = limited only by book size
    commission_per_contract: Decimal = Decimal("1.0")
    starting_cash: Decimal = Decimal("1000000")


@dataclass(slots=True)
class _Pending:
    order: Order
    submit_step: int


class PaperBrokerAdapter(BaseBrokerAdapter):
    name = "paper"

    def __init__(
        self, clock: Clock, market: MockBrokerAdapter, config: PaperFillConfig | None = None
    ) -> None:
        self._clock = clock
        self._market = market
        self._cfg = config or PaperFillConfig()
        self._step = 0
        self._connected = False
        self._positions: dict[str, Position] = {}
        self._cash = self._cfg.starting_cash
        self._orders: dict[str, Order] = {}
        self._pending: list[_Pending] = []
        self._fills: list[Fill] = []

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(supports_streaming=True)

    # --- delegate connection / reference / market data to the feed ----------
    async def connect(self) -> None:
        self._connected = True
        await self._market.connect()

    async def disconnect(self) -> None:
        self._connected = False
        await self._market.disconnect()

    async def is_connected(self) -> bool:
        return self._connected

    async def list_instruments(self, underlying_symbol: str | None = None) -> list[Instrument]:
        return await self._market.list_instruments(underlying_symbol)

    async def get_contract_spec(self, symbol: str) -> ContractSpec:
        return await self._market.get_contract_spec(symbol)

    async def get_trading_session(self, symbol: str) -> TradingSession:
        return await self._market.get_trading_session(symbol)

    async def get_quote(self, symbol: str) -> Quote:
        return await self._market.get_quote(symbol)

    def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Quote]:
        return self._market.stream_quotes(symbols)

    # --- account ------------------------------------------------------------
    async def get_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if not p.is_flat]

    async def get_cash(self) -> list[CashBalance]:
        return [CashBalance(currency="USD", cash=self._cash)]

    async def get_margin(self) -> MarginInfo:
        return MarginInfo(currency="USD", used_margin=Decimal("0"), available_margin=self._cash)

    # --- orders -------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        if request.client_order_id in self._orders:
            raise OrderStateError(f"duplicate client_order_id: {request.client_order_id}")
        now = self._clock.now()
        order = Order(
            request=request,
            state=OrderState.ACKNOWLEDGED,
            broker_order_id=f"paper-{len(self._orders) + 1}",
            created_at=now,
            updated_at=now,
        )
        self._orders[request.client_order_id] = order
        self._pending.append(_Pending(order=order, submit_step=self._step))
        return order

    async def process_pending(self) -> list[Fill]:
        """Advance one tick and attempt to fill eligible resting orders."""
        self._step += 1
        new_fills: list[Fill] = []
        for pending in list(self._pending):
            order = pending.order
            if order.is_terminal:
                self._pending.remove(pending)
                continue
            if self._step - pending.submit_step < self._cfg.latency_ticks:
                continue
            fill = await self._try_fill(order)
            if fill is not None:
                new_fills.append(fill)
            if order.is_terminal:
                self._pending.remove(pending)
        self._fills.extend(new_fills)
        return new_fills

    async def _try_fill(self, order: Order) -> Fill | None:
        req = order.request
        spec = await self._market.get_contract_spec(req.instrument_symbol)
        quote = await self._market.get_quote(req.instrument_symbol)

        price = self._fill_price(req, quote, spec)
        if price is None:
            return None
        available = self._available_size(req, quote)
        if available <= 0:
            return None
        remaining = order.request.quantity - order.filled_quantity
        fill_qty = min(remaining, available)
        if self._cfg.max_fill_per_tick is not None:
            fill_qty = min(fill_qty, self._cfg.max_fill_per_tick)
        if fill_qty <= 0:
            return None

        self._apply_fill(order, spec, price, fill_qty)
        fill = Fill(
            client_order_id=req.client_order_id,
            instrument_symbol=req.instrument_symbol,
            side=req.side,
            quantity=fill_qty,
            price=price,
            commission=self._cfg.commission_per_contract * fill_qty,
            timestamp=self._clock.now(),
            broker_fill_id=f"pfill-{len(self._fills) + 1}",
        )
        return fill

    def _fill_price(self, req: OrderRequest, quote: Quote, spec: ContractSpec) -> Decimal | None:
        slip = spec.tick_size * self._cfg.slippage_ticks
        if req.side is Side.BUY:
            ask = quote.ask
            if ask is None:
                return None
            if req.order_type is OrderType.PASSIVE_LIMIT:
                # rests; fills only if the market comes to us
                return (
                    req.limit_price
                    if (req.limit_price is not None and ask <= req.limit_price)
                    else None
                )
            crossed = ask + slip
            if req.order_type in (OrderType.MARKET, OrderType.MARKETABLE_LIMIT):
                if req.limit_price is not None:
                    return min(crossed, req.limit_price) if crossed > req.limit_price else crossed
                return crossed
            # plain LIMIT
            return crossed if (req.limit_price is not None and req.limit_price >= ask) else None
        bid = quote.bid
        if bid is None:
            return None
        if req.order_type is OrderType.PASSIVE_LIMIT:
            return (
                req.limit_price
                if (req.limit_price is not None and bid >= req.limit_price)
                else None
            )
        crossed = bid - slip
        if req.order_type in (OrderType.MARKET, OrderType.MARKETABLE_LIMIT):
            if req.limit_price is not None:
                return max(crossed, req.limit_price) if crossed < req.limit_price else crossed
            return crossed
        return crossed if (req.limit_price is not None and req.limit_price <= bid) else None

    def _available_size(self, req: OrderRequest, quote: Quote) -> Decimal:
        size = quote.ask_size if req.side is Side.BUY else quote.bid_size
        return size if size is not None else Decimal("0")

    def _apply_fill(self, order: Order, spec: ContractSpec, price: Decimal, qty: Decimal) -> None:
        req = order.request
        signed = qty if req.side is Side.BUY else -qty
        prev = self._positions.get(
            req.instrument_symbol,
            Position(instrument_symbol=req.instrument_symbol, quantity=Decimal("0")),
        )
        new_qty = prev.quantity + signed
        if prev.quantity == 0 or prev.sign == (1 if signed > 0 else -1):
            denom = abs(prev.quantity) + abs(signed)
            new_avg = (abs(prev.quantity) * prev.average_price + abs(signed) * price) / denom
        else:
            new_avg = prev.average_price if new_qty != 0 else Decimal("0")
        self._positions[req.instrument_symbol] = Position(
            instrument_symbol=req.instrument_symbol, quantity=new_qty, average_price=new_avg
        )
        commission = self._cfg.commission_per_contract * qty
        self._cash += -signed * price * spec.multiplier - commission

        new_filled = order.filled_quantity + qty
        if new_filled > 0:
            order.average_fill_price = (
                order.average_fill_price * order.filled_quantity + price * qty
            ) / new_filled
        order.filled_quantity = new_filled
        order.state = (
            OrderState.FILLED if new_filled >= req.quantity else OrderState.PARTIALLY_FILLED
        )
        order.updated_at = self._clock.now()

    async def cancel_order(self, client_order_id: str) -> Order:
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderStateError(f"unknown order: {client_order_id}")
        if not order.is_terminal:
            order.state = OrderState.CANCELLED
            order.updated_at = self._clock.now()
        return order

    async def get_order(self, client_order_id: str) -> Order:
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderStateError(f"unknown order: {client_order_id}")
        return order

    async def get_open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.is_active]

    async def get_fills(self, since_sequence: int | None = None) -> list[Fill]:
        if since_sequence is None:
            return list(self._fills)
        return self._fills[since_sequence:]
