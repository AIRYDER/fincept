"""
features — online + (eventually) offline feature engineering.

Currently exposes:

  - ``OnlineRunner``                — consumes ``md.bars.1m``, computes
    per-symbol features incrementally, publishes ``FeatureFrame`` to
    ``features.online`` (TASK-016).
  - ``transforms.PriceFeatures``    — log/simple returns, momentum.
  - ``transforms.VolatilityFeatures`` — realized vol, Parkinson, Garman-Klass.
  - ``transforms.CrossFeatures``    — rolling beta + correlation vs benchmark.
"""

from features.online import OnlineRunner
from features.transforms.cross import CrossFeatures
from features.transforms.price import PriceFeatures
from features.transforms.volatility import VolatilityFeatures

__all__ = [
    "CrossFeatures",
    "OnlineRunner",
    "PriceFeatures",
    "VolatilityFeatures",
]
