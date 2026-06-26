"""settlements - settlement worker MVP.

Tails ``fincept_core.prediction_log`` and writes settlement records to
``fincept_core.datasets.SettlementStore``.  See ``worker.tick`` for the
core loop.
"""

from __future__ import annotations

from .market_data_bridge import make_async_market_data_source
from .worker import tick, tick_sync

__all__ = ["make_async_market_data_source", "tick", "tick_sync"]
