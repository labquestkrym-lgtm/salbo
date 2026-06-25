"""T-Invest broker adapter (R11, concrete).

Translates the T-Invest gRPC API to our domain via the tested conversion layer.
The SDK is imported lazily so the rest of the project imports and tests without
``tinkoff-investments`` installed.

NOT exercised in CI: every networked method needs a token + network. Validate
the SDK call signatures and instrument field names against your installed
version before pointing at sandbox. Safety guards (ADR-0003): a non-sandbox
endpoint is refused in development/test, and order methods require all
live-trading gates unless on sandbox.

Verified live (sandbox + prod): connect, accounts, positions/cash, futures
listing, order-book quotes, streaming, and the option-chain assembly —
``options_by`` keyed by the basic *asset* uid resolved from the future's
``basic_asset_position_uid`` (the full ``options()`` dump is deprecated and
errors with "Stream removed"). Orders, margin, open-orders and fills are mapped
against the documented SDK shapes but UNVERIFIED on the network (margin is
live-only); the trading-schedule remains a static placeholder until validated.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.brokers.base import BaseBrokerAdapter, BrokerCapabilities
from app.brokers.tinkoff.conversions import (
    decimal_to_quotation,
    direction_to_side,
    order_status_to_state,
    quotation_obj_to_decimal,
    side_to_direction,
)
from app.brokers.tinkoff.instruments import (
    basic_asset_size,
    future_to_instrument,
    option_to_instrument,
)
from app.config.settings import AppSettings
from app.core.clock import Clock
from app.core.enums import OrderType, Side
from app.core.exceptions import BrokerError, LiveTradingNotAuthorizedError
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

_ORDER_TYPE_LIMIT = 1
_ORDER_TYPE_MARKET = 2
_ID_TYPE_POSITION_UID = 4  # InstrumentIdType.INSTRUMENT_ID_TYPE_POSITION_UID (protobuf accepts int)
_FILLS_LOOKBACK_DAYS = 1  # how far back get_fills scans operations (no int cursor in T-Invest)
# T-Invest requires the order_id idempotency key to be empty or a UUID; our domain
# client_order_id is a human-readable string, so map it to a DETERMINISTIC uuid5
# (same client id -> same uuid -> still idempotent broker-side).
_ORDER_ID_NS = uuid.UUID("6f9b8c1e-0000-5000-a000-5a1b0707b07a")


def _sdk_order_id(client_order_id: str) -> str:
    return str(uuid.uuid5(_ORDER_ID_NS, client_order_id))


def _order_from_state(state: Any, now: datetime, *, client_order_id: str | None = None) -> Order:
    """Map a T-Invest ``OrderState`` (from get_order_state / get_orders) to our
    domain :class:`Order`. ``client_order_id`` overrides the id when the caller
    already knows it (single-order lookup); otherwise the exchange ``order_id``
    is used."""
    oid = client_order_id or str(getattr(state, "order_id", "") or "unknown")
    avg = getattr(state, "average_position_price", None)
    # Reconstruct the order type: prefer the order's own price (initial), else the
    # average fill price, as the LIMIT price; if neither is present, model it as a
    # MARKET order (which must NOT carry a price). This is a best-effort live view.
    price_obj = (
        getattr(state, "initial_order_price", None)
        or getattr(state, "initial_security_price", None)
        or avg
    )
    price = quotation_obj_to_decimal(price_obj) if price_obj is not None else None
    has_price = price is not None and price != 0
    request = OrderRequest(
        client_order_id=oid,
        instrument_symbol=str(state.figi),
        side=direction_to_side(int(state.direction)),
        quantity=Decimal(int(state.lots_requested)) or Decimal("1"),
        order_type=OrderType.LIMIT if has_price else OrderType.MARKET,
        limit_price=price if has_price else None,
    )
    order = Order(
        request=request,
        state=order_status_to_state(int(state.execution_report_status)),
        broker_order_id=str(getattr(state, "order_id", "") or oid),
        filled_quantity=Decimal(int(state.lots_executed)),
        created_at=now,
        updated_at=now,
    )
    if avg is not None and order.filled_quantity > 0:
        order.average_fill_price = quotation_obj_to_decimal(avg)
    return order


async def _safe_fetch(call: Any, what: str) -> list[Any]:
    """Fetch one instrument class, degrading to an empty list (with a warning)
    if the RPC fails — a transient error on one class must not drop the rest."""
    try:
        return list((await call()).instruments)
    except Exception as exc:  # transient gRPC / one instrument class unavailable
        logger.warning("instrument_fetch_failed", what=what, error=str(exc))
        return []


def _try_map(mapper: Any, obj: Any, out: list[Instrument]) -> None:
    """Map one SDK instrument, skipping (with a log) any that don't fit our
    contract — real universes contain instruments with missing/odd fields, and
    one bad item must not drop the whole list."""
    try:
        out.append(mapper(obj))
    except Exception as exc:  # resilience over a large, noisy instrument universe
        logger.debug("instrument_skipped", figi=getattr(obj, "figi", "?"), error=str(exc))


def _load_sdk() -> Any:
    try:
        import tinkoff.invest as ti
    except ImportError as exc:  # pragma: no cover - depends on optional dep
        raise BrokerError(
            'tinkoff-investments is not installed; `pip install ".[tinkoff]"` to use '
            "the T-Invest adapter."
        ) from exc
    return ti


class TInvestBrokerAdapter(BaseBrokerAdapter):
    name = "tinkoff"

    def __init__(
        self,
        settings: AppSettings,
        clock: Clock,
        *,
        sandbox: bool = True,
        account_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._sandbox = sandbox
        self._account_id = account_id or settings.broker_account_id
        # Guard 1: never point a dev/test build at a non-sandbox endpoint.
        if settings.is_dev_or_test and not sandbox:
            raise LiveTradingNotAuthorizedError(
                "Refusing a non-sandbox T-Invest adapter in development/test (ADR-0003)."
            )
        self._cm: Any = None
        self._client: Any = None
        self._connect_lock = asyncio.Lock()  # idempotent connect for concurrent (multi-pair) callers
        self._futures_lock = asyncio.Lock()
        self._futures_cache: list[Any] = []
        self._futures_cache_at: datetime | None = None

    def _require_can_trade_live(self) -> None:
        if self._sandbox:
            return
        if not self._settings.is_live_trading_allowed():
            raise LiveTradingNotAuthorizedError(
                "Live trading not authorized: " + "; ".join(self._settings.live_trading_blockers())
            )

    def _svc(self) -> Any:
        if self._client is None:
            raise BrokerError("T-Invest adapter is not connected")
        return self._client

    @property
    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            supports_order_modify=False,
            supports_greeks=False,
            supports_implied_vol=False,
            supports_streaming=True,
            supports_open_interest=True,
        )

    # --- connection ---------------------------------------------------------
    async def connect(self) -> None:
        # Idempotent: concurrent callers (the multi-pair orchestrator runs N pair
        # loops) share ONE channel — many streams over one connection, which also
        # avoids tripping the per-token connection limit.
        async with self._connect_lock:
            if self._client is not None:
                return
            ti = _load_sdk()
            token = self._settings.broker_api_key
            if token is None or not token.get_secret_value():
                raise BrokerError("BROKER_API_KEY (T-Invest token) is not set")
            target = (
                ti.constants.INVEST_GRPC_API_SANDBOX
                if self._sandbox
                else ti.constants.INVEST_GRPC_API
            )
            self._cm = ti.AsyncClient(token.get_secret_value(), target=target)
            self._client = await self._cm.__aenter__()
            logger.info("tinkoff_connected", sandbox=self._sandbox)

    async def disconnect(self) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._cm = None
            self._client = None

    async def is_connected(self) -> bool:
        return self._client is not None

    # --- sandbox account management (sandbox only) -------------------------
    async def open_sandbox_account(self) -> str:
        """Open a fresh sandbox account and select it. Sandbox only."""
        if not self._sandbox:
            raise BrokerError("open_sandbox_account is only valid on the sandbox endpoint")
        resp = await self._svc().sandbox.open_sandbox_account()
        self._account_id = str(resp.account_id)
        logger.info("tinkoff_sandbox_account_opened", account_id=self._account_id)
        return self._account_id

    async def get_sandbox_accounts(self) -> list[str]:
        if not self._sandbox:
            raise BrokerError("get_sandbox_accounts is only valid on the sandbox endpoint")
        resp = await self._svc().sandbox.get_sandbox_accounts()
        return [str(a.id) for a in resp.accounts]

    async def sandbox_pay_in(self, amount: Decimal, *, currency: str = "rub") -> None:
        """Top up the selected sandbox account with virtual money. Sandbox only."""
        if not self._sandbox:
            raise BrokerError("sandbox_pay_in is only valid on the sandbox endpoint")
        if self._account_id is None:
            raise BrokerError("no sandbox account selected; call open_sandbox_account first")
        ti = _load_sdk()
        units, nano = decimal_to_quotation(amount)
        money = ti.MoneyValue(currency=currency, units=units, nano=nano)
        await self._svc().sandbox.sandbox_pay_in(account_id=self._account_id, amount=money)
        logger.info("tinkoff_sandbox_pay_in", amount=str(amount), currency=currency)

    # --- reference data -----------------------------------------------------
    async def list_instruments(self, underlying_symbol: str | None = None) -> list[Instrument]:
        """Assemble the future(s) + option chain for an underlying.

        For a specific ``underlying_symbol`` the option chain is resolved via
        ``options_by`` (keyed off the matching future's basic-asset uid) — the
        full ``options()`` dump is unreliable (empty on the sandbox). If
        ``options_by`` yields nothing we fall back to the filtered dump so the
        method still degrades to "futures only" rather than failing.

        The resolver groups futures and options by ``underlying_symbol`` (the
        T-Invest ``basic_asset`` ticker), which is exactly what the
        options-on-futures straddle path consumes.
        """
        svc = self._svc()
        out: list[Instrument] = []
        matched_futures: list[Any] = []
        for fut in await _safe_fetch(svc.instruments.futures, "futures"):
            if underlying_symbol is None or str(fut.basic_asset) == underlying_symbol:
                matched_futures.append(fut)
                _try_map(future_to_instrument, fut, out)

        options: list[Instrument] = []
        if underlying_symbol is not None and matched_futures:
            options = await self._option_chain_for_future(matched_futures[0], underlying_symbol)
        if not options:
            for opt in await _safe_fetch(svc.instruments.options, "options"):
                if underlying_symbol is None or str(opt.basic_asset) == underlying_symbol:
                    _try_map(option_to_instrument, opt, options)
        out.extend(options)
        return out

    async def _option_chain_for_future(self, fut: Any, underlying_symbol: str) -> list[Instrument]:
        """Resolve the option chain for ``fut``'s underlying via ``options_by``.

        VERIFIED on the live prod API (SBER): ``options_by`` is keyed by the
        basic *asset* uid (e.g. the SBER share's ``asset_uid``), NOT the future's
        own ``uid``/``position_uid`` (those return 0). The future links to its
        basic asset via ``basic_asset_position_uid`` (== the share's
        ``position_uid``); see :meth:`_basic_asset_uid`. Degrades to an empty list
        on any failure (caller falls back to the full dump)."""
        asset_uid = await self._basic_asset_uid(fut, underlying_symbol)
        if asset_uid is None:
            return []
        # A FORTS option is 1:1 with its future, so it covers the same underlying
        # units (the future's basic_asset_size, e.g. 100 shares). Stamp that onto
        # the option as its delta multiplier (the option's own basic_asset_size is 1).
        contract_size = basic_asset_size(fut)
        try:
            chain = await self.option_chain(asset_uid, contract_size=contract_size)
        except Exception as exc:  # one underlying's chain unavailable must not abort
            logger.warning("option_chain_failed", underlying=underlying_symbol, error=str(exc))
            return []
        return [o for o in chain if o.underlying_symbol == underlying_symbol]

    async def _basic_asset_uid(self, fut: Any, underlying_symbol: str) -> str | None:
        """The underlying-asset uid that ``options_by`` expects.

        Prefer the future's own ``asset_uid``/``basic_asset_uid`` if present;
        otherwise resolve it from ``basic_asset_position_uid`` via
        ``get_instrument_by(POSITION_UID)`` — on FORTS the future carries the
        basic asset's *position* uid, and the option chain is keyed by that
        asset's ``asset_uid``."""
        direct = getattr(fut, "asset_uid", None) or getattr(fut, "basic_asset_uid", None)
        if direct:
            return str(direct)
        position_uid = getattr(fut, "basic_asset_position_uid", None)
        if not position_uid:
            logger.warning("future_missing_basic_asset_link", underlying=underlying_symbol)
            return None
        try:
            resp = await self._svc().instruments.get_instrument_by(
                id_type=_ID_TYPE_POSITION_UID,  # protobuf enum field accepts the int
                id=str(position_uid),
            )
        except Exception as exc:  # basic-asset lookup unavailable -> no chain
            logger.warning(
                "basic_asset_lookup_failed", underlying=underlying_symbol, error=str(exc)
            )
            return None
        asset_uid = getattr(resp.instrument, "asset_uid", None)
        return str(asset_uid) if asset_uid else None

    def _futures_fresh(self) -> bool:
        if not self._futures_cache or self._futures_cache_at is None:
            return False
        return (self._clock.now() - self._futures_cache_at).total_seconds() < 120

    async def _cached_futures(self) -> list[Any]:
        """All futures, cached ~120s. Concurrent callers (the multi-pair startup)
        share ONE futures() call instead of N — staying under the instruments-service
        rate limit (15/min)."""
        if self._futures_fresh():
            return self._futures_cache
        async with self._futures_lock:
            if self._futures_fresh():
                return self._futures_cache
            self._futures_cache = await _safe_fetch(self._svc().instruments.futures, "futures")
            self._futures_cache_at = self._clock.now()
            return self._futures_cache

    async def list_futures(self, underlying_symbol: str) -> list[Instrument]:
        """Futures only (no option chain) for an underlying, off the shared cache —
        used by the pairs strategy, which never needs options."""
        out: list[Instrument] = []
        for fut in await self._cached_futures():
            if str(fut.basic_asset) == underlying_symbol:
                _try_map(future_to_instrument, fut, out)
        return out

    async def get_contract_spec(self, symbol: str) -> ContractSpec:
        for inst in await self.list_instruments():
            if inst.symbol == symbol:
                return inst.spec
        raise BrokerError(f"unknown instrument: {symbol}")

    async def get_trading_session(self, symbol: str) -> TradingSession:
        # Validate against instruments.trading_schedules before relying on this.
        return TradingSession(open_time=time(7, 0), close_time=time(20, 0))

    # --- market data --------------------------------------------------------
    async def get_quote(self, symbol: str) -> Quote:
        book = await self._svc().market_data.get_order_book(instrument_id=symbol, depth=1)
        bid = quotation_obj_to_decimal(book.bids[0].price) if book.bids else None
        ask = quotation_obj_to_decimal(book.asks[0].price) if book.asks else None
        bid_sz = Decimal(book.bids[0].quantity) if book.bids else None
        ask_sz = Decimal(book.asks[0].quantity) if book.asks else None
        last = quotation_obj_to_decimal(book.last_price) if book.last_price else None
        return Quote(
            instrument_symbol=symbol,
            timestamp=self._clock.now(),
            bid=bid,
            ask=ask,
            bid_size=bid_sz,
            ask_size=ask_sz,
            last=last,
            sequence=None,
        )

    async def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Quote]:
        """Order-book stream, resilient to drops. gRPC market-data streams get torn
        down periodically (RST_STREAM / "Stream removed"); on any stream error or a
        clean end we re-create + re-subscribe with capped backoff so the strategy
        loop keeps receiving quotes. Cancellation (GeneratorExit/CancelledError) is
        BaseException, not Exception, so it still propagates and stops the stream."""
        ti = _load_sdk()
        requested = set(symbols)
        backoff = 1.0
        while True:
            try:
                stream = self._svc().create_market_data_stream()
                stream.order_book.subscribe(
                    [ti.OrderBookInstrument(instrument_id=s, depth=1) for s in symbols]
                )
                async for md in stream:
                    ob = getattr(md, "orderbook", None)
                    if ob is None:
                        continue
                    backoff = 1.0  # a healthy update resets the backoff
                    # Map the update back to the id we subscribed with (figi or uid).
                    if ob.instrument_uid in requested:
                        sym = ob.instrument_uid
                    elif ob.figi in requested:
                        sym = ob.figi
                    else:
                        sym = ob.instrument_uid or ob.figi
                    yield Quote(
                        instrument_symbol=sym,
                        timestamp=self._clock.now(),
                        bid=quotation_obj_to_decimal(ob.bids[0].price) if ob.bids else None,
                        ask=quotation_obj_to_decimal(ob.asks[0].price) if ob.asks else None,
                        bid_size=Decimal(ob.bids[0].quantity) if ob.bids else None,
                        ask_size=Decimal(ob.asks[0].quantity) if ob.asks else None,
                        last=None,
                        sequence=None,
                    )
                logger.warning("market_data_stream_ended", symbols=len(symbols))
            except Exception as exc:  # stream dropped -> reconnect with backoff
                logger.warning("market_data_stream_reconnect", error=str(exc), backoff=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 30.0)

    async def get_daily_closes(self, symbol: str, *, days: int) -> list[tuple[date, float]]:
        """Daily closes (oldest first) via ``get_all_candles`` — used to seed the
        pair spread so the z-score starts on the validated daily timescale."""
        ti = _load_sdk()
        now = self._clock.now()
        out: list[tuple[date, float]] = []
        try:
            async for c in self._svc().get_all_candles(
                instrument_id=symbol,
                from_=now - timedelta(days=days),
                interval=ti.CandleInterval.CANDLE_INTERVAL_DAY,
            ):
                out.append((c.time.date(), float(quotation_obj_to_decimal(c.close))))
        except Exception as exc:  # history unavailable -> caller falls back
            logger.warning("daily_closes_failed", symbol=symbol, error=str(exc))
        return out

    async def option_chain(
        self, basic_asset_uid: str, *, contract_size: Decimal | None = None
    ) -> list[Instrument]:
        """Option chain for one underlying via ``options_by`` (the filtered call;
        the full ``options()`` dump is deprecated and errors with "Stream removed").

        ``basic_asset_uid`` is the underlying *asset* uid (e.g. a share's
        ``asset_uid``) — NOT a future's ``uid``/``position_uid``. ``contract_size``
        (underlying units per contract) is stamped onto each option as its delta
        multiplier; the chain assembly derives it from the hedging future."""
        resp = await self._svc().instruments.options_by(basic_asset_uid=basic_asset_uid)
        out: list[Instrument] = []
        for opt in resp.instruments:
            _try_map(lambda o: option_to_instrument(o, contract_size=contract_size), opt, out)
        return out

    # --- account ------------------------------------------------------------
    async def _positions_response(self) -> Any:
        # Sandbox positions live on the SandboxService, not OperationsService.
        svc = self._svc()
        if self._sandbox:
            return await svc.sandbox.get_sandbox_positions(account_id=self._account_id)
        return await svc.operations.get_positions(account_id=self._account_id)

    async def get_positions(self) -> list[Position]:
        res = await self._positions_response()
        out: list[Position] = []
        for s in res.securities:
            out.append(Position(instrument_symbol=str(s.figi), quantity=Decimal(s.balance)))
        for f in getattr(res, "futures", []):
            out.append(Position(instrument_symbol=str(f.figi), quantity=Decimal(f.balance)))
        # FORTS options sit in their own `options` bucket and have no figi — they
        # are keyed by instrument_uid (which is our Instrument.symbol for options).
        for o in getattr(res, "options", []):
            out.append(
                Position(instrument_symbol=str(o.instrument_uid), quantity=Decimal(o.balance))
            )
        return out

    async def get_cash(self) -> list[CashBalance]:
        res = await self._positions_response()
        return [
            CashBalance(currency=str(m.currency).upper(), cash=quotation_obj_to_decimal(m))
            for m in res.money
        ]

    async def get_margin(self) -> MarginInfo:
        """Portfolio margin via ``operations.get_margin_attributes``. This RPC is
        a live-only feature (the sandbox does not expose margin attributes).

        Mapping: ``used_margin`` = starting (initial) margin, ``available_margin``
        = liquid portfolio minus starting margin, ``maintenance_margin`` = minimal
        margin. The currency comes from the liquid-portfolio MoneyValue."""
        if self._sandbox:
            raise BrokerError("margin attributes are not available on the T-Invest sandbox")
        attrs = await self._svc().operations.get_margin_attributes(account_id=self._account_id)
        liquid = quotation_obj_to_decimal(attrs.liquid_portfolio)
        starting = quotation_obj_to_decimal(attrs.starting_margin)
        minimal = quotation_obj_to_decimal(attrs.minimal_margin)
        return MarginInfo(
            currency=str(attrs.liquid_portfolio.currency).upper(),
            used_margin=max(starting, Decimal("0")),
            available_margin=liquid - starting,
            maintenance_margin=max(minimal, Decimal("0")),
        )

    # --- orders -------------------------------------------------------------
    async def place_order(self, request: OrderRequest) -> Order:
        self._require_can_trade_live()
        ti = _load_sdk()
        svc = self._svc()
        is_market = request.order_type is OrderType.MARKET
        kwargs: dict[str, Any] = {
            "instrument_id": request.instrument_symbol,
            "quantity": int(request.quantity),
            "direction": ti.OrderDirection(side_to_direction(request.side)),
            "account_id": self._account_id,
            "order_type": ti.OrderType(_ORDER_TYPE_MARKET if is_market else _ORDER_TYPE_LIMIT),
            "order_id": _sdk_order_id(request.client_order_id),  # idempotency key (UUID)
        }
        if not is_market and request.limit_price is not None:
            units, nano = decimal_to_quotation(request.limit_price)
            kwargs["price"] = ti.Quotation(units=units, nano=nano)
        post = svc.sandbox.post_sandbox_order if self._sandbox else svc.orders.post_order
        resp = await post(**kwargs)
        now = self._clock.now()
        order = Order(
            request=request,
            state=order_status_to_state(int(resp.execution_report_status)),
            broker_order_id=str(resp.order_id),
            created_at=now,
            updated_at=now,
            filled_quantity=Decimal(int(getattr(resp, "lots_executed", 0))),
        )
        if resp.executed_order_price and order.filled_quantity > 0:
            order.average_fill_price = quotation_obj_to_decimal(resp.executed_order_price)
        return order

    async def cancel_order(self, client_order_id: str) -> Order:
        self._require_can_trade_live()
        svc = self._svc()
        cancel = svc.sandbox.cancel_sandbox_order if self._sandbox else svc.orders.cancel_order
        await cancel(account_id=self._account_id, order_id=_sdk_order_id(client_order_id))
        return await self.get_order(client_order_id)

    async def get_order(self, client_order_id: str) -> Order:
        svc = self._svc()
        state = svc.sandbox.get_sandbox_order_state if self._sandbox else svc.orders.get_order_state
        st = await state(account_id=self._account_id, order_id=_sdk_order_id(client_order_id))
        return _order_from_state(st, self._clock.now(), client_order_id=client_order_id)

    async def get_open_orders(self) -> list[Order]:
        svc = self._svc()
        get = svc.sandbox.get_sandbox_orders if self._sandbox else svc.orders.get_orders
        resp = await get(account_id=self._account_id)
        now = self._clock.now()
        out: list[Order] = []
        for o in getattr(resp, "orders", []):
            try:
                out.append(_order_from_state(o, now))
            except Exception as exc:  # one malformed entry must not drop the list
                logger.debug("open_order_skipped", error=str(exc))
        return out

    async def get_fills(self, since_sequence: int | None = None) -> list[Fill]:
        """Executions over a recent window via ``OperationsService``.

        T-Invest exposes fills over a TIME RANGE, not an integer cursor, so
        ``since_sequence`` is ignored (kept for the broker interface). Each
        operation that carries ``trades`` is one buy/sell; we emit one
        :class:`Fill` per trade leg. Side is inferred from the payment sign
        (buy pays out -> negative). Per-leg commission is not attributable here
        (it arrives as a separate operation), so it defaults to 0; the
        ``client_order_id`` falls back to the operation id (operations do not
        echo the original order id)."""
        svc = self._svc()
        now = self._clock.now()
        frm = now - timedelta(days=_FILLS_LOOKBACK_DAYS)
        get = svc.sandbox.get_sandbox_operations if self._sandbox else svc.operations.get_operations
        resp = await get(account_id=self._account_id, from_=frm, to=now)
        out: list[Fill] = []
        for op in getattr(resp, "operations", []):
            trades = getattr(op, "trades", None) or []
            if not trades:
                continue  # non-trade operation (commission, payin, ...)
            payment = op.payment if getattr(op, "payment", None) else None
            side = (
                Side.BUY
                if (payment is not None and quotation_obj_to_decimal(payment) < 0)
                else Side.SELL
            )
            for tr in trades:
                trade_id = getattr(tr, "trade_id", None)
                try:
                    out.append(
                        Fill(
                            client_order_id=str(getattr(op, "id", "") or "unknown"),
                            instrument_symbol=str(op.figi),
                            side=side,
                            quantity=Decimal(int(tr.quantity)),
                            price=quotation_obj_to_decimal(tr.price),
                            timestamp=getattr(tr, "date_time", None) or op.date,
                            broker_fill_id=str(trade_id) if trade_id else None,
                        )
                    )
                except Exception as exc:  # skip a malformed trade leg, keep the rest
                    logger.debug("fill_skipped", error=str(exc))
        return out
