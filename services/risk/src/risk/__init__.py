"""
risk - pre-trade limit checks + kill-switch state.

Public surface:

  - ``check_intent(intent, *, ctx, settings, last_price) -> RiskCheckResult``
        Pure function.  Takes an OrderIntent and a snapshot of current
        risk state; returns the canonical RiskCheckResult schema with
        ``approved`` + ``reasons``.

  - ``RiskContext``
        Dataclass aggregating the state ``check_intent`` reads:
        per-symbol notionals, gross notional, kill-switch flag.
        The function is pure given a ``RiskContext``; the ``snapshot``
        module knows how to build one from PositionStore + a price
        callable.

  - ``KillSwitchState``
        In-memory tracker that the alert consumer feeds.  Reads
        ``code="kill_switch_engaged"`` / ``"kill_switch_cleared"``
        AlertEvents from STREAM_ALERTS and flips the engaged flag.

  - ``build_context(...)``
        Async helper.  Reads positions from PositionStore, multiplies
        by latest prices, and assembles a RiskContext.  Used by the
        OMS before each intent.

This package is library-first by design.  The OMS imports it and calls
``check_intent`` inline so risk decisions add zero RTT.  A separate-
process risk service (Phase H scale-out concern) can wrap the same
checks behind a stream consumer without touching this surface.
"""

from risk.checks import RiskContext, check_intent
from risk.snapshot import build_context
from risk.state import KillSwitchState

__all__ = [
    "KillSwitchState",
    "RiskContext",
    "build_context",
    "check_intent",
]
