"""Tests for oms.state — order lifecycle FSM."""

from __future__ import annotations

import pytest

from fincept_core.schemas import OrderStatus
from oms.state import VALID_TRANSITIONS, can_transition


@pytest.mark.parametrize(
    ("frm", "to", "expected"),
    [
        # Happy paths from PENDING_NEW.
        (OrderStatus.PENDING_NEW, OrderStatus.NEW, True),
        (OrderStatus.PENDING_NEW, OrderStatus.REJECTED, True),
        # Happy paths from NEW.
        (OrderStatus.NEW, OrderStatus.FILLED, True),
        (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, True),
        (OrderStatus.NEW, OrderStatus.CANCELED, True),
        (OrderStatus.NEW, OrderStatus.EXPIRED, True),
        (OrderStatus.NEW, OrderStatus.REJECTED, True),
        # PARTIALLY_FILLED leg.
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, True),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED, True),
        # Illegal: terminal states are sinks.
        (OrderStatus.FILLED, OrderStatus.NEW, False),
        (OrderStatus.FILLED, OrderStatus.CANCELED, False),
        (OrderStatus.CANCELED, OrderStatus.NEW, False),
        (OrderStatus.REJECTED, OrderStatus.NEW, False),
        (OrderStatus.EXPIRED, OrderStatus.FILLED, False),
        # Illegal: skipping the venue-acknowledgement gate.
        (OrderStatus.PENDING_NEW, OrderStatus.FILLED, False),
        (OrderStatus.PENDING_NEW, OrderStatus.CANCELED, False),
    ],
)
def test_can_transition(frm: OrderStatus, to: OrderStatus, expected: bool) -> None:
    assert can_transition(frm, to) is expected


def test_valid_transitions_covers_every_status() -> None:
    """Every OrderStatus must appear as a key (even if its target set is empty)."""
    assert set(VALID_TRANSITIONS.keys()) == set(OrderStatus)


def test_terminal_statuses_have_empty_transition_sets() -> None:
    for terminal in (
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    ):
        assert VALID_TRANSITIONS[terminal] == set()
