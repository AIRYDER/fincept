"""
quant_foundry.feature_availability — per-feature availability report for a dataset export.

TASK-0405: Build Feature Lake Builder MVP.

For each expected feature, the report records how many rows actually carried a
value (vs. missing). This is emitted alongside the manifest so a training job
can refuse to train on a dataset where a required feature is mostly missing,
and so the tournament can apply a feature-availability penalty.

CPU-only, fixture-driven; no DB access.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # avoid runtime import cycle
    from quant_foundry.feature_lake import FeatureRow


class FeatureAvailabilityReport(BaseModel):
    """Per-feature availability percentages for an exported dataset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    total_rows: int
    expected_features: tuple[str, ...]
    per_feature: dict[str, int]  # feature -> count of rows where present

    # --- queries ---------------------------------------------------------

    def availability_pct(self, feature: str) -> float:
        """Percentage (0-100) of rows where ``feature`` was present."""
        if self.total_rows <= 0:
            return 0.0
        return 100.0 * self.per_feature.get(feature, 0) / self.total_rows

    def missing_features(self) -> tuple[str, ...]:
        """Features that are absent in every row (0% availability)."""
        return tuple(f for f in self.expected_features if self.per_feature.get(f, 0) == 0)

    # --- construction ----------------------------------------------------

    @classmethod
    def from_rows(
        cls,
        rows: tuple[FeatureRow, ...],
        expected_features: tuple[str, ...],
    ) -> FeatureAvailabilityReport:
        counts: dict[str, int] = {f: 0 for f in expected_features}
        for row in rows:
            present = {fv.name for fv in row.features}
            for f in expected_features:
                if f in present:
                    counts[f] += 1
        return cls(
            total_rows=len(rows),
            expected_features=expected_features,
            per_feature=counts,
        )

    # --- serialization ---------------------------------------------------

    def to_json(self) -> str:
        body: dict[str, Any] = {
            "schema_version": self.schema_version,
            "total_rows": self.total_rows,
            "expected_features": list(self.expected_features),
            "per_feature": dict(self.per_feature),
            "availability_pct": {
                f: round(self.availability_pct(f), 4) for f in self.expected_features
            },
            "missing_features": list(self.missing_features()),
        }
        return json.dumps(body, sort_keys=True, indent=2)
