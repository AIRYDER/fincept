"""
oms — Order Management System (paper simulator for v1).

Public surface:

  - ``PaperFiller``        Simulates fills against a live mid-price + Gaussian
                           latency.  Spread + fee added in the same step.
  - ``LivePrices``         In-memory cache of latest trade prices per symbol;
                           updated by the trades-stream consumer in main.py.
  - ``can_transition``     Order state-machine validator (PENDING_NEW -> NEW
                           -> FILLED etc.); rejects illegal jumps.
  - ``process_intent``     Pure async function: takes an OrderIntent, a
                           LivePrices, and a PaperFiller; returns the
                           sequence of (Order, Fill | None) events to publish.
                           Lets tests exercise the OMS pipeline without a
                           running consumer loop.
"""

from oms.paper import PaperFiller
from oms.prices import LivePrices
from oms.processor import process_intent
from oms.state import VALID_TRANSITIONS, can_transition

__all__ = [
    "VALID_TRANSITIONS",
    "LivePrices",
    "PaperFiller",
    "can_transition",
    "process_intent",
]
