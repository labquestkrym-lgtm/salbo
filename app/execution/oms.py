"""Order Management System (R20).

A small, persistence-agnostic state machine over :class:`Order`:

* **idempotency** — a ``client_order_id`` is submitted at most once; a repeat
  ``submit`` returns the existing order instead of double-sending.
* **no assumed fills** — fill state only changes from a broker-confirmed
  :class:`Order`/:class:`Fill`, never optimistically.
* **persisted transitions** — every state change is written to an
  :class:`OrderStore` as an :class:`OrderEvent`, enabling audit and crash
  recovery (``recover`` rebuilds in-flight state and resyncs with the broker).
* **timeout / unknown** — if the broker can't confirm, the order goes to
  ``UNKNOWN`` and must be reconciled, not assumed filled.

The store here is in-memory; the SQLAlchemy-backed store arrives with the
persistence stage (R25) behind the same :class:`OrderStore` protocol.
"""

from __future__ import annotations

from typing import Protocol

from app.brokers.base import BaseBrokerAdapter
from app.core.clock import Clock
from app.core.enums import OrderState
from app.core.exceptions import BrokerError, OrderStateError
from app.core.logging import get_logger
from app.models import Fill, Order, OrderEvent, OrderRequest

logger = get_logger(__name__)

# Allowed state transitions. UNKNOWN is reachable from any non-terminal state and
# can be resolved to any state by reconciliation.
_TERMINAL = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
)
_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.VALIDATED, OrderState.REJECTED, OrderState.UNKNOWN}),
    OrderState.VALIDATED: frozenset(
        {OrderState.SUBMITTED, OrderState.REJECTED, OrderState.UNKNOWN}
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.ACKNOWLEDGED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_PENDING,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.CANCEL_PENDING: frozenset(
        {
            OrderState.CANCELLED,
            OrderState.FILLED,
            OrderState.PARTIALLY_FILLED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    ),
}


def is_valid_transition(src: OrderState, dst: OrderState) -> bool:
    if src == dst and dst is OrderState.PARTIALLY_FILLED:
        return True  # additional partial fills
    return dst in _TRANSITIONS.get(src, frozenset())


class OrderStore(Protocol):
    """Async because the durable implementation (DB) is async; the in-memory
    one trivially satisfies the same interface."""

    async def save_order(self, order: Order) -> None: ...
    async def get_order(self, client_order_id: str) -> Order | None: ...
    async def list_open(self) -> list[Order]: ...
    async def save_event(self, event: OrderEvent) -> None: ...
    async def list_events(self, client_order_id: str) -> list[OrderEvent]: ...


class InMemoryOrderStore:
    """Reference store. Swappable for the DB-backed ``SqlOrderStore``."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._events: dict[str, list[OrderEvent]] = {}

    async def save_order(self, order: Order) -> None:
        self._orders[order.request.client_order_id] = order

    async def get_order(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    async def list_open(self) -> list[Order]:
        return [o for o in self._orders.values() if not o.is_terminal]

    async def save_event(self, event: OrderEvent) -> None:
        self._events.setdefault(event.client_order_id, []).append(event)

    async def list_events(self, client_order_id: str) -> list[OrderEvent]:
        return list(self._events.get(client_order_id, []))


class OrderManager:
    def __init__(self, broker: BaseBrokerAdapter, store: OrderStore, clock: Clock) -> None:
        self._broker = broker
        self._store = store
        self._clock = clock

    # --- transitions --------------------------------------------------------
    async def _transition(self, order: Order, dst: OrderState, detail: str = "") -> None:
        src = order.state
        if src == dst and dst is not OrderState.PARTIALLY_FILLED:
            return
        if not is_valid_transition(src, dst):
            raise OrderStateError(
                f"illegal transition {src} -> {dst} for {order.request.client_order_id}"
            )
        order.state = dst
        order.updated_at = self._clock.now()
        await self._store.save_order(order)
        await self._store.save_event(
            OrderEvent(
                client_order_id=order.request.client_order_id,
                from_state=src,
                to_state=dst,
                timestamp=order.updated_at,
                detail=detail,
            )
        )

    # --- submission ---------------------------------------------------------
    async def submit(self, request: OrderRequest) -> Order:
        """Idempotent submit. A repeat of a known client_order_id returns the
        existing order without re-sending to the broker."""
        existing = await self._store.get_order(request.client_order_id)
        if existing is not None:
            logger.info("order_submit_idempotent_hit", client_order_id=request.client_order_id)
            return existing

        now = self._clock.now()
        order = Order(request=request, state=OrderState.CREATED, created_at=now, updated_at=now)
        await self._store.save_order(order)
        await self._store.save_event(
            OrderEvent(
                client_order_id=request.client_order_id,
                from_state=OrderState.CREATED,
                to_state=OrderState.CREATED,
                timestamp=now,
                detail="created",
            )
        )
        await self._transition(order, OrderState.VALIDATED, "validated")
        await self._transition(order, OrderState.SUBMITTED, "submitted to broker")

        try:
            broker_order = await self._broker.place_order(request)
        except OrderStateError:
            # Broker reports a duplicate: resolve authoritatively, don't assume.
            broker_order = await self._broker.get_order(request.client_order_id)
        except BrokerError as exc:
            await self._transition(order, OrderState.UNKNOWN, f"broker error: {exc}")
            return order

        await self._sync_from_broker(order, broker_order)
        return order

    async def _sync_from_broker(self, order: Order, broker_order: Order) -> None:
        order.broker_order_id = broker_order.broker_order_id
        if broker_order.filled_quantity > order.filled_quantity:
            order.filled_quantity = broker_order.filled_quantity
            order.average_fill_price = broker_order.average_fill_price
        # Move through ACKNOWLEDGED if the broker jumped straight to a fill.
        if order.state is OrderState.SUBMITTED and broker_order.state in (
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        ):
            await self._transition(order, OrderState.ACKNOWLEDGED, "broker ack (implied by fill)")
        if broker_order.state != order.state:
            await self._transition(order, broker_order.state, "broker status")

    # --- fills (push) -------------------------------------------------------
    async def apply_fill(self, fill: Fill) -> Order:
        order = await self._store.get_order(fill.client_order_id)
        if order is None:
            raise OrderStateError(f"fill for unknown order {fill.client_order_id}")
        new_filled = order.filled_quantity + fill.quantity
        if new_filled > order.request.quantity:
            raise OrderStateError(
                f"overfill on {fill.client_order_id}: {new_filled} > {order.request.quantity}"
            )
        # Volume-weighted average fill price.
        if new_filled > 0:
            order.average_fill_price = (
                order.average_fill_price * order.filled_quantity + fill.price * fill.quantity
            ) / new_filled
        order.filled_quantity = new_filled
        dst = (
            OrderState.FILLED
            if new_filled == order.request.quantity
            else OrderState.PARTIALLY_FILLED
        )
        await self._transition(order, dst, f"fill {fill.quantity} @ {fill.price}")
        return order

    # --- cancel / reconcile / recover --------------------------------------
    async def cancel(self, client_order_id: str) -> Order:
        order = await self._store.get_order(client_order_id)
        if order is None:
            raise OrderStateError(f"cannot cancel unknown order {client_order_id}")
        if order.is_terminal:
            return order
        await self._transition(order, OrderState.CANCEL_PENDING, "cancel requested")
        broker_order = await self._broker.cancel_order(client_order_id)
        await self._sync_from_broker(order, broker_order)
        return order

    async def reconcile_order(self, client_order_id: str) -> Order:
        """Authoritatively resync one order's state with the broker."""
        order = await self._store.get_order(client_order_id)
        if order is None:
            raise OrderStateError(f"unknown order {client_order_id}")
        try:
            broker_order = await self._broker.get_order(client_order_id)
        except (OrderStateError, BrokerError):
            await self._transition(order, OrderState.UNKNOWN, "broker has no record")
            return order
        await self._sync_from_broker(order, broker_order)
        return order

    async def recover(self) -> list[Order]:
        """On startup, resync every non-terminal order with the broker."""
        recovered: list[Order] = []
        for order in await self._store.list_open():
            recovered.append(await self.reconcile_order(order.request.client_order_id))
        logger.info("oms_recovered", count=len(recovered))
        return recovered
