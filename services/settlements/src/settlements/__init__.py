"""settlements - settlement worker MVP.

Tails ``fincept_core.prediction_log`` and writes settlement records to
``fincept_core.datasets.SettlementStore``.  See ``worker.tick`` for the
core loop.

The canonical settlement path is now Path B (``quant_foundry.SettlementLedger``)
via ``settlements.compat.PathACompatAdapter``. The legacy ``worker.tick``
and ``worker.tick_sync`` functions are deprecated and delegate to the
adapter when ``SETTLEMENTS_USE_PATH_B=1``.
"""

from __future__ import annotations

from .compat import (
    PathACompatAdapter,
    default_agent_to_model_id,
    default_model_to_agent_id,
    derive_p_up_from_confidence,
    path_b_to_path_a_record,
)
from .market_data_bridge import make_async_market_data_source
from .worker import tick, tick_sync

__all__ = [
    "PathACompatAdapter",
    "default_agent_to_model_id",
    "default_model_to_agent_id",
    "derive_p_up_from_confidence",
    "make_async_market_data_source",
    "path_b_to_path_a_record",
    "tick",
    "tick_sync",
]
