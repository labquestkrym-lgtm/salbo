"""Deterministic in-memory mock broker (R9).

Generates a reproducible market: a geometric-Brownian-motion underlying, a
carry-priced future, and a flat-IV option chain. Quotes carry monotonic
``sequence`` numbers so the market-data layer can detect gaps. Fills cross the
bid/ask (never the last price) and charge commission.

Intended for backtests and tests. It is *not* a queue/latency simulator — that
is the PaperBroker's job (Stage 6).
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import numpy as np

from app.brokers.base import BaseBrokerAdapter, BrokerCapabilities
from app.core.clock import Clock
from app.core.enums import (
    AssetClass,
    OptionStyle,
    OptionType,
    OrderState,
    OrderType,
    PricingModel,
    Side,
)
from app.core.exceptions import BrokerError, OrderStateError
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
from app.pricing import bsm
from app.pricing.quantize import ceil_to_tick, floor_to_tick, price_to_decimal

_YEAR_SECONDS = 365.0 * 24 * 3600

_EQUITY_SPEC = ContractSpec(
    tick_size=Decimal("0.01"),
    tick_value=Decimal("0.01"),
    lot_size=1,
    multiplier=Decimal("1"),
    currency="USD",
)
# Deliberately different multipliers: option=100, future=50 (exercises R7/R13).
_OPTION_SPEC = ContractSpec(
    tick_size=Decimal("0.05"),
    tick_value=Decimal("5"),
    lot_size=100,
    multiplier=Decimal("100"),
    currency="USD",
)
_FUTURE_SPEC = ContractSpec(
    tick_size=Decimal("0.25"),
    tick_value=Decimal("12.5"),
    lot_size=1,
    multiplier=Decimal("50"),
    currency="USD",
)


@dataclass(slots=True)
class MockMarketConfig:
    underlying_symbol: str = "XYZ"
    spot0: float = 100.0
    annual_vol: float = 0.20
    drift: float = 0.0
    rate: float = 0.05
    dividend_yield: float = 0.0
    option_iv: float = 0.20
    seed: int = 42
    dt_seconds: float = 1.0
    spread_ticks: int = 2
    quote_size: Decimal = Decimal("10")
    commission_per_contract: Decimal = Decimal("1.0")
    days_to_expiry: int = 30
    strikes: tuple[float, ...] = (90.0, 95.0, 100.0, 105.0, 110.0)
    starting_cash: Decimal = Decimal("1000000")
    max_stream_steps: int = 1000

    expiry: date = field(default_factory=lambda: datetime.now(UTC).date() + timedelta(days=30))


class MockBrokerAdapter(BaseBrokerAdapter):
    name = "mock"

    def __init__(self, clock: Clock, config: MockMarketConfig | None = None) -> None:
        self._clock = clock
        self._cfg = config or MockMarketConfig()
        self._cfg.expiry = self._clock.now().date() + timedelta(days=self._cfg.days_to_expiry)
        self._rng = np.random.default_rng(self._cfg.seed)
        self._spot = self._cfg.spot0
        self._sequence = 0
        self._step = 0
        self._connected = False
        self._stream_start = self._clock.now()

        self._instruments: dict[str, Instrument] = {}
        self._positions: dict[str, Position] = {}
        self._cash = self._cfg.starting_cash
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._build_universe()

    # --- universe -----------------------------------------------------------
    def _build_universe(self) -> None:
        c = self._cfg
        u = c.underlying_symbol
        exp_tag = c.expiry.strftime("%Y%m%d")
        self._instruments[u] = Instrument(
            symbol=u,
            underlying_symbol=u,
            asset_class=AssetClass.EQUITY,
            spec=_EQUITY_SPEC,
            pricing_model=None,
        )
        self._future_symbol = f"{u}-FUT-{exp_tag}"
        self._instruments[self._future_symbol] = Instrument(
            symbol=self._future_symbol,
            underlying_symbol=u,
            asset_class=AssetClass.FUTURE,
            spec=_FUTURE_SPEC,
            expiry=c.expiry,
        )
        for strike in c.strikes:
            for ot, tag in ((OptionType.CALL, "C"), (OptionType.PUT, "P")):
                sym = f"{u}-{tag}-{int(strike)}-{exp_tag}"
                self._instruments[sym] = Instrument(
                    symbol=sym,
                    underlying_symbol=u,
                    asset_class=AssetClass.OPTION,
                    spec=_OPTION_SPEC,
                    expiry=c.expiry,
                    option_type=ot,
                    strike=Decimal(str(strike)),
                    option_style=OptionStyle.EUROPEAN,
                    pricing_model=PricingModel.BLACK_SCHOLES_MERTON,
                )

    @property
    def future_symbol(self) -> str:
        return self._future_symbol

    # --- pricing helpers ----------------------------------------------------
    def _tau_years(self, expiry: date, now: datetime) -> float:
        end = datetime.combine(expiry, time(23, 59, 59), tzinfo=UTC)
        seconds = (end - now).total_seconds()
        return max(seconds / _YEAR_SECONDS, 1.0 / _YEAR_SECONDS)

    def _theoretical_mid(self, inst: Instrument, now: datetime) -> float:
        c = self._cfg
        if inst.asset_class is AssetClass.EQUITY:
            return self._spot
        if inst.asset_class is AssetClass.FUTURE:
            assert inst.expiry is not None
            tau = self._tau_years(inst.expiry, now)
            return self._spot * math.exp((c.rate - c.dividend_yield) * tau)
        # option
        assert inst.expiry is not None and inst.strike is not None and inst.option_type is not None
        tau = self._tau_years(inst.expiry, now)
        greeks = bsm(
            spot=self._spot,
            strike=float(inst.strike),
            t=tau,
            rate=c.rate,
            sigma=c.option_iv,
            option_type=inst.option_type,
            dividend_yield=c.dividend_yield,
        )
        return max(greeks.price, 0.0)

    def _quote_for(self, inst: Instrument, now: datetime) -> Quote:
        spec = inst.spec
        mid = price_to_decimal(self._theoretical_mid(inst, now), spec)
        half = spec.tick_size * self._cfg.spread_ticks
        bid = floor_to_tick(mid - half, spec)
        ask = ceil_to_tick(mid + half, spec)
        if bid <= 0:
            bid = spec.tick_size
        if ask <= bid:
            ask = bid + spec.tick_size
        self._sequence += 1
        return Quote(
            instrument_symbol=inst.symbol,
            timestamp=now,
            bid=bid,
            ask=ask,
            bid_size=self._cfg.quote_size,
            ask_size=self._cfg.quote_size,
            last=mid,
            sequence=self._sequence,
        )

    def _advance_price(self) -> None:
        c = self._cfg
        dt = c.dt_seconds / _YEAR_SECONDS
        z = float(self._rng.standard_normal())
        self._spot *= math.exp(
            (c.drift - 0.5 * c.annual_vol**2) * dt + c.annual_vol * math.sqrt(dt) * z
        )
        self._step += 1

    # --- connection ---------------------------------------------------------
    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            supports_order_modify=False,
            supports_greeks=True,
            supports_implied_vol=True,
            supports_streaming=True,
            supports_open_interest=False,
        )

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    # --- reference data -----------------------------------------------------
    async def list_instruments(self, underlying_symbol: str | None = None) -> list[Instrument]:
        values = list(self._instruments.values())
        if underlying_symbol is None:
            return values
        return [i for i in values if i.underlying_symbol == underlying_symbol]

    async def get_contract_spec(self, symbol: str) -> ContractSpec:
        return self._require(symbol).spec

    async def get_trading_session(self, symbol: str) -> TradingSession:
        return TradingSession(open_time=time(13, 30), close_time=time(20, 0))  # ~US RTH in UTC

    def _require(self, symbol: str) -> Instrument:
        inst = self._instruments.get(symbol)
        if inst is None:
            raise BrokerError(f"unknown instrument: {symbol}")
        return inst

    # --- market data --------------------------------------------------------
    async def get_quote(self, symbol: str) -> Quote:
        return self._quote_for(self._require(symbol), self._clock.now())

    async def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Quote]:
        instruments = [self._require(s) for s in symbols]
        for _ in range(self._cfg.max_stream_steps):
            self._advance_price()
            now = self._stream_start + timedelta(seconds=self._step * self._cfg.dt_seconds)
            for inst in instruments:
                yield self._quote_for(inst, now)

    # --- account ------------------------------------------------------------
    async def get_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if not p.is_flat]

    async def get_cash(self) -> list[CashBalance]:
        return [CashBalance(currency="USD", cash=self._cash)]

    async def get_margin(self) -> MarginInfo:
        # Simplified: margin = notional of futures + option premium at risk.
        used = Decimal("0")
        for sym, pos in self._positions.items():
            inst = self._instruments[sym]
            mid = price_to_decimal(self._theoretical_mid(inst, self._clock.now()), inst.spec)
            used += abs(pos.quantity) * mid * inst.spec.multiplier * Decimal("0.1")
        return MarginInfo(currency="USD", used_margin=used, available_margin=self._cash - used)

    # --- orders -------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        if request.client_order_id in self._orders:
            raise OrderStateError(f"duplicate client_order_id: {request.client_order_id}")
        inst = self._require(request.instrument_symbol)
        now = self._clock.now()
        order = Order(
            request=request,
            state=OrderState.ACKNOWLEDGED,
            broker_order_id=f"mock-{len(self._orders) + 1}",
            created_at=now,
            updated_at=now,
        )
        self._orders[request.client_order_id] = order

        quote = self._quote_for(inst, now)
        fill_price = self._marketable_fill_price(request, quote)
        if fill_price is not None:
            self._apply_fill(order, inst, fill_price, now)
        return order

    def _marketable_fill_price(self, req: OrderRequest, quote: Quote) -> Decimal | None:
        if req.order_type is OrderType.PASSIVE_LIMIT:
            return None  # rests in the book; never crosses
        if req.side is Side.BUY:
            ask = quote.ask
            if ask is None:
                return None
            if req.order_type in (OrderType.MARKET, OrderType.MARKETABLE_LIMIT):
                return ask
            return ask if (req.limit_price is not None and req.limit_price >= ask) else None
        bid = quote.bid
        if bid is None:
            return None
        if req.order_type in (OrderType.MARKET, OrderType.MARKETABLE_LIMIT):
            return bid
        return bid if (req.limit_price is not None and req.limit_price <= bid) else None

    def _apply_fill(self, order: Order, inst: Instrument, price: Decimal, now: datetime) -> None:
        req = order.request
        qty = req.quantity
        commission = self._cfg.commission_per_contract * qty
        signed = qty if req.side is Side.BUY else -qty

        prev = self._positions.get(
            inst.symbol, Position(instrument_symbol=inst.symbol, quantity=Decimal("0"))
        )
        new_qty = prev.quantity + signed
        if prev.quantity == 0 or (prev.sign == (1 if signed > 0 else -1)):
            # opening or increasing: volume-weighted average price
            denom = abs(prev.quantity) + abs(signed)
            new_avg = (abs(prev.quantity) * prev.average_price + abs(signed) * price) / denom
        else:
            new_avg = prev.average_price if new_qty != 0 else Decimal("0")
        self._positions[inst.symbol] = Position(
            instrument_symbol=inst.symbol,
            quantity=new_qty,
            average_price=new_avg,
        )

        cash_delta = -signed * price * inst.spec.multiplier - commission
        self._cash += cash_delta

        self._fills.append(
            Fill(
                client_order_id=req.client_order_id,
                instrument_symbol=inst.symbol,
                side=req.side,
                quantity=qty,
                price=price,
                commission=commission,
                timestamp=now,
                broker_fill_id=f"fill-{len(self._fills) + 1}",
            )
        )
        order.state = OrderState.FILLED
        order.filled_quantity = qty
        order.average_fill_price = price
        order.updated_at = now

    async def cancel_order(self, client_order_id: str) -> Order:
        order = self._orders.get(client_order_id)
        if order is None:
            raise OrderStateError(f"unknown order: {client_order_id}")
        if order.is_terminal:
            return order
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
