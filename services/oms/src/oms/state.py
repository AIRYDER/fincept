"""
oms.state — order lifecycle state machine.

A small finite-state machine over ``OrderStatus``.  Public surface:

  - ``VALID_TRANSITIONS``  Mapping of ``from`` status to legal ``to`` set.
  - ``can_transition``     Boolean predicate.

Why bother for a paper OMS?  Because the live OMS will share this exact
table, and venue rejections / partial fills / cancels traverse it in
ways that are easy to bug without a clear declarative source.  The
audit trail (``oms.audit``) records every transition; an illegal one
should fail loudly here, not corrupt the audit record.
"""

from __future__ import annotations

from fincept_core.schemas import OrderStatus

VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING_NEW: {OrderStatus.NEW, OrderStatus.REJECTED},
    OrderStatus.NEW: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
    },
    # Terminal states — no further transitions.
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


def can_transition(frm: OrderStatus, to: OrderStatus) -> bool:
    """Return True iff ``frm -> to`` is a legal state transition."""
    return to in VALID_TRANSITIONS.get(frm, set())
