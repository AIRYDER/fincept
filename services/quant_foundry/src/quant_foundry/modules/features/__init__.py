"""Feature computer modules. Importing this package registers all feature modules."""

from __future__ import annotations

from quant_foundry.modules.features.engagement_weighted import EngagementWeightedFeatures
from quant_foundry.modules.features.interactions import InteractionsFeatures
from quant_foundry.modules.features.per_event_type import PerEventTypeFeatures
from quant_foundry.modules.features.per_year import PerYearFeatures
from quant_foundry.modules.features.pre_event_momentum import PreEventMomentumFeatures
from quant_foundry.modules.features.time_decay import TimeDecayFeatures

__all__ = [
    "EngagementWeightedFeatures",
    "InteractionsFeatures",
    "PerEventTypeFeatures",
    "PerYearFeatures",
    "PreEventMomentumFeatures",
    "TimeDecayFeatures",
]
