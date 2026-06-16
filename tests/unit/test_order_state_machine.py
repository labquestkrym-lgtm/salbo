"""Order state-machine transition rules (R20)."""

from __future__ import annotations

from itertools import pairwise

from app.core.enums import OrderState
from app.execution import is_valid_transition


def test_happy_path_transitions_allowed() -> None:
    chain = [
        OrderState.CREATED,
        OrderState.VALIDATED,
        OrderState.SUBMITTED,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]
    for src, dst in pairwise(chain):
        assert is_valid_transition(src, dst)


def test_partial_fill_self_loop_allowed() -> None:
    assert is_valid_transition(OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED)


def test_terminal_states_have_no_exit() -> None:
    for terminal in (
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    ):
        assert not is_valid_transition(terminal, OrderState.ACKNOWLEDGED)


def test_cannot_skip_from_created_to_filled() -> None:
    assert not is_valid_transition(OrderState.CREATED, OrderState.FILLED)


def test_unknown_can_resolve_to_any_terminal() -> None:
    for dst in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED):
        assert is_valid_transition(OrderState.UNKNOWN, dst)
