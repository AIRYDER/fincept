"""settlements - settlement worker MVP.

Tails ``fincept_core.prediction_log`` and writes settlement records to
``fincept_core.datasets.SettlementStore``.  See ``worker.tick`` for the
core loop.
"""

from __future__ import annotations

from .worker import tick, tick_sync

__all__ = ["tick", "tick_sync"]
