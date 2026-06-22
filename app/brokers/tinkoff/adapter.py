"""T-Invest broker adapter (R11, concrete).

Translates the T-Invest gRPC API to our domain via the tested conversion layer.
The SDK is imported lazily so the rest of the project imports and tests without
``tinkoff-investments`` installed.

NOT exercised in CI: every networked method needs a token + network. Validate
the SDK call signatures and instrument field names against your installed
version before pointing at sandbox. Safety guards (ADR-0003): a non-sandbox
endpoint is refused in development/test, and order methods require all
live-trading gates unless on sandbox.

Verified live against the sandbox: connect, account open/fund, positions/cash,
futures listing, order-book quotes, and **streaming** (order-book subscription).
``option_chain`` uses ``options_by`` (the full ``options()`` dump is unreliable
on the sandbox). Fills, margin and trading-schedule remain explicit
``NotImplementedError`` until validated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import time
from decimal import Decimal
from typing import Any

from app.brokers.base import BaseBrokerAdapter, BrokerCapabilities
from app.brokers.tinkoff.conversions import (
    decimal_to_quotation,
    order_status_to_state,
    quotation_obj_to_decimal,
    side_to_direction,
)
from app.brokers.tinkoff.instruments import future_to_instrument, option_to_instrument
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
_NOT_WIRED = (
    "T-Invest {what} is not wired yet; implement against your installed "
    "tinkoff-investments version and validate on sandbox before use."
)


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
        ti = _load_sdk()
        token = self._settings.broker_api_key
        if token is None or not token.get_secret_value():
            raise BrokerError("BROKER_API_KEY (T-Invest token) is not set")
        target = (
            ti.constants.INVEST_GRPC_API_SANDBOX if self._sandbox else ti.constants.INVEST_GRPC_API
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

        ``options_by`` needs the basic *asset* uid — NOT the future's
        ``uid``/``position_uid`` (those raise INVALID_ARGUMENT). VALIDATE the
        field name and that the call returns the chain against your live
        universe; the sandbox exposes no options, so this path is unverified on
        the network. Degrades to an empty list on any failure (caller falls back
        to the full dump)."""
        asset_uid = getattr(fut, "basic_asset_uid", None) or getattr(fut, "asset_uid", None)
        if not asset_uid:
            logger.warning("future_missing_basic_asset_uid", underlying=underlying_symbol)
            return []
        try:
            chain = await self.option_chain(str(asset_uid))
        except Exception as exc:  # one underlying's chain unavailable must not abort
            logger.warning("option_chain_failed", underlying=underlying_symbol, error=str(exc))
            return []
        return [o for o in chain if o.underlying_symbol == underlying_symbol]

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
        ti = _load_sdk()
        stream = self._svc().create_market_data_stream()
        stream.order_book.subscribe(
            [ti.OrderBookInstrument(instrument_id=s, depth=1) for s in symbols]
        )
        requested = set(symbols)
        async for md in stream:
            ob = getattr(md, "orderbook", None)
            if ob is None:
                continue
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

    async def option_chain(self, basic_asset_uid: str) -> list[Instrument]:
        """Option chain for one underlying via ``options_by`` (the filtered call;
        the full ``options()`` dump is unreliable on the sandbox).

        ``basic_asset_uid`` is the underlying *asset* uid — note this is NOT a
        future's ``uid``/``position_uid`` (those raise INVALID_ARGUMENT). Linking
        a hedging future to its option chain's asset uid, plus an
        options-on-futures path in InstrumentResolver, is required before the
        straddle can run on T-Invest (the resolver currently assumes an equity
        underlying)."""
        resp = await self._svc().instruments.options_by(basic_asset_uid=basic_asset_uid)
        out: list[Instrument] = []
        for opt in resp.instruments:
            _try_map(option_to_instrument, opt, out)
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
        return out

    async def get_cash(self) -> list[CashBalance]:
        res = await self._positions_response()
        return [
            CashBalance(currency=str(m.currency).upper(), cash=quotation_obj_to_decimal(m))
            for m in res.money
        ]

    async def get_margin(self) -> MarginInfo:
        raise NotImplementedError(_NOT_WIRED.format(what="margin (get_margin_attributes)"))

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
            "order_id": request.client_order_id,  # idempotency key
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
        await cancel(account_id=self._account_id, order_id=client_order_id)
        return await self.get_order(client_order_id)

    async def get_order(self, client_order_id: str) -> Order:
        svc = self._svc()
        state = svc.sandbox.get_sandbox_order_state if self._sandbox else svc.orders.get_order_state
        st = await state(account_id=self._account_id, order_id=client_order_id)
        request = OrderRequest(
            client_order_id=client_order_id,
            instrument_symbol=str(st.figi),
            side=Side.BUY if int(st.direction) == 1 else Side.SELL,
            quantity=Decimal(int(st.lots_requested)) or Decimal("1"),
            order_type=OrderType.LIMIT,
        )
        order = Order(
            request=request,
            state=order_status_to_state(int(st.execution_report_status)),
            broker_order_id=client_order_id,
            filled_quantity=Decimal(int(st.lots_executed)),
            created_at=self._clock.now(),
            updated_at=self._clock.now(),
        )
        if st.average_position_price and order.filled_quantity > 0:
            order.average_fill_price = quotation_obj_to_decimal(st.average_position_price)
        return order

    async def get_open_orders(self) -> list[Order]:
        raise NotImplementedError(_NOT_WIRED.format(what="open-orders listing (get_orders)"))

    async def get_fills(self, since_sequence: int | None = None) -> list[Fill]:
        raise NotImplementedError(_NOT_WIRED.format(what="fills (operations stream)"))
