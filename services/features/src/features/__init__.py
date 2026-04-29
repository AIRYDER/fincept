"""
features — online + offline feature engineering with PIT joins.

Public surface:

  - ``OnlineRunner``                — consumes ``md.bars.1m``, computes
    per-symbol features incrementally, publishes ``FeatureFrame`` to
    ``features.online`` and optionally caches in ``OnlineStore``.
  - ``FeatureComputer``             — the bar->FeatureFrame kernel shared
    by online and offline (bit-identical guarantee).
  - ``OnlineStore``                 — Redis cache; latest-known frame per
    (symbol, freq) with TTL.  Serves agent inference at <10 ms.
  - ``OfflineStore``                — Timescale-backed authoritative
    history; idempotent on re-run.
  - ``PITJoiner``                   — joins bars with as-of features
    (strict ``feature.ts_event <= bar.ts_event``).
  - ``backfill``                    — replay historical bars through
    ``FeatureComputer`` and persist to ``OfflineStore``.
  - ``transforms.{PriceFeatures, VolatilityFeatures, CrossFeatures}``
"""

from features.computer import DEFAULT_BENCHMARK, FeatureComputer
from features.offline import backfill
from features.online import OnlineRunner
from features.pit import PITJoiner
from features.store import OfflineStore, OnlineStore
from features.transforms.cross import CrossFeatures
from features.transforms.price import PriceFeatures
from features.transforms.volatility import VolatilityFeatures

__all__ = [
    "DEFAULT_BENCHMARK",
    "CrossFeatures",
    "FeatureComputer",
    "OfflineStore",
    "OnlineRunner",
    "OnlineStore",
    "PITJoiner",
    "PriceFeatures",
    "VolatilityFeatures",
    "backfill",
]
