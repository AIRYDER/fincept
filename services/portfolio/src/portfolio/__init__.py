"""
portfolio — live position tracking + UI-facing Redis snapshots.

Public surface:

  - ``PositionStore``    Redis-backed live position cache.  Hash key is
                         ``positions:{strategy_id}``, field is the symbol,
                         value is the JSON-serialized Position.  This is
                         the read path for the upcoming /positions REST
                         endpoint (TASK-050) — designed for sub-millisecond
                         lookups, no DB round-trips.
  - ``PortfolioState``   In-memory dict-of-dicts: strategy_id -> symbol ->
                         Position.  Sourced from PositionStore on startup
                         and kept in sync with each incoming Fill.
  - ``apply_fill``       Async helper that updates state + store atomically
                         and returns the new Position.  Wraps the shared
                         ``fincept_core.portfolio.apply_fill_to_position``.

Each Fill produces one Position snapshot which is published to
``ord.positions`` (the change log) AND written to the Redis hash (the
live state).  Same online/offline split TASK-017 used for features.
"""

from portfolio.state import PortfolioState, apply_fill
from portfolio.store import PositionStore

__all__ = [
    "PortfolioState",
    "PositionStore",
    "apply_fill",
]
