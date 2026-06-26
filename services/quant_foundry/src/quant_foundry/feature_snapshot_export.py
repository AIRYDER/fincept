"""
quant_foundry.feature_snapshot_export — live feature snapshot export (TASK-0602).

Exports compact, point-in-time feature snapshots from the feature lake for
shadow inference. The snapshots are compact float vectors (not full
``FeatureRow`` objects) with freshness metadata and availability scores.

Key invariants:
- **Compact.** Snapshots are per-symbol lists of floats, not full
  ``FeatureRow`` objects. This minimizes network transfer to the RunPod
  inference worker.
- **Point-in-time.** Only rows whose ``decision_time`` matches the export
  decision time are included. The feature lake's PIT proof (Builder 4's
  ``feature_lake.py``) ensures no look-ahead.
- **Availability measurable.** A ``FeatureAvailabilityReport`` is produced
  alongside the snapshot, recording per-feature availability percentages.
- **Abstain on low availability.** If a symbol's feature availability is
  below the configured threshold, the symbol is marked as degraded
  (``availability=False``) in the snapshot. The inference worker abstains
  rather than predicting on incomplete data.

File-disjoint from Builder 4's ``feature_lake.py`` +
``feature_availability.py`` (read-only imports). Does NOT modify them.
Imports ``FeatureSnapshot`` from my ``shadow_inference.py`` (TASK-0601).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.feature_availability import FeatureAvailabilityReport
from quant_foundry.feature_lake import FeatureRow
from quant_foundry.shadow_inference import FeatureSnapshot

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class SnapshotExportConfig(BaseModel):
    """Configuration for a feature snapshot export.

    Frozen + extra='forbid'. Carries the minimum availability percentage
    (below which a symbol is marked degraded) and the maximum freshness
    (above which the snapshot is considered stale).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_availability_pct: float = 80.0
    max_freshness_ns: int = 60_000_000_000  # 60 seconds


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


class SnapshotExportReceipt(BaseModel):
    """Receipt for a feature snapshot export.

    Frozen + extra='forbid'. Carries the exported snapshot, the availability
    report, the decision time, and the list of degraded symbols.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: FeatureSnapshot
    availability_report: FeatureAvailabilityReport
    decision_time: int
    degraded_symbols: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for audit/persistence."""
        return {
            "snapshot": self.snapshot.model_dump(),
            "availability_report": {
                "total_rows": self.availability_report.total_rows,
                "expected_features": list(self.availability_report.expected_features),
                "per_feature": dict(self.availability_report.per_feature),
                "availability_pct": {
                    f: round(self.availability_report.availability_pct(f), 4)
                    for f in self.availability_report.expected_features
                },
                "missing_features": list(self.availability_report.missing_features()),
            },
            "decision_time": self.decision_time,
            "degraded_symbols": list(self.degraded_symbols),
        }


# ---------------------------------------------------------------------------
# The exporter
# ===========================================================================


class FeatureSnapshotExport:
    """Exports compact feature snapshots from the feature lake.

    Converts ``FeatureRow`` objects (Builder 4's ``feature_lake.py``) into
    compact ``FeatureSnapshot`` objects (TASK-0601's ``shadow_inference.py``)
    suitable for the RunPod inference worker. Measures feature availability
    and marks degraded symbols (below the configured threshold).
    """

    def __init__(self, config: SnapshotExportConfig | None = None) -> None:
        self.config = config or SnapshotExportConfig()

    def export(
        self,
        rows: tuple[FeatureRow, ...],
        decision_time: int,
        expected_features: tuple[str, ...] | None = None,
    ) -> FeatureSnapshot:
        """Export a compact feature snapshot from feature lake rows.

        Args:
        - ``rows``: the feature lake rows to export.
        - ``decision_time``: the point-in-time cutoff (only rows at this
          decision time are included).
        - ``expected_features``: the expected feature names. If None, inferred
          from the first row.

        Returns a ``FeatureSnapshot`` with compact float vectors, availability
        flags, and freshness metadata.
        """
        if not rows:
            return FeatureSnapshot(
                symbols=[],
                features={},
                availability={},
                ts_event=decision_time,
                freshness_ns=0,
            )

        # Infer expected features from the first row if not provided.
        if expected_features is None:
            expected_features = tuple(fv.name for fv in rows[0].features)

        # Filter rows at the decision time and build compact snapshots.
        symbols: list[str] = []
        features: dict[str, list[float]] = {}
        availability: dict[str, bool] = {}

        for row in rows:
            if row.decision_time != decision_time:
                continue

            sym = row.symbol
            symbols.append(sym)

            # Build compact feature vector (in expected_features order).
            feature_map = {fv.name: fv.value for fv in row.features}
            compact = [float(feature_map.get(fname, 0.0)) for fname in expected_features]
            features[sym] = compact

            # Compute per-symbol availability.
            present_count = sum(1 for fname in expected_features if fname in feature_map)
            avail_pct = 100.0 * present_count / max(len(expected_features), 1)
            availability[sym] = avail_pct >= self.config.min_availability_pct

        # Compute freshness (time since the most recent feature was observed).
        max_observed = max(
            (fv.observed_at for row in rows for fv in row.features),
            default=decision_time,
        )
        freshness_ns = max(0, decision_time - max_observed)

        return FeatureSnapshot(
            symbols=symbols,
            features=features,
            availability=availability,
            ts_event=decision_time,
            freshness_ns=freshness_ns,
        )

    def export_with_receipt(
        self,
        rows: tuple[FeatureRow, ...],
        decision_time: int,
        expected_features: tuple[str, ...] | None = None,
    ) -> SnapshotExportReceipt:
        """Export a feature snapshot with a full receipt.

        Returns a ``SnapshotExportReceipt`` with the snapshot, availability
        report, decision time, and degraded symbols list.
        """
        if not rows:
            if expected_features is None:
                expected_features = ()
            avail_report = FeatureAvailabilityReport(
                total_rows=0,
                expected_features=expected_features,
                per_feature={},
            )
            snapshot = FeatureSnapshot(
                symbols=[],
                features={},
                availability={},
                ts_event=decision_time,
                freshness_ns=0,
            )
            return SnapshotExportReceipt(
                snapshot=snapshot,
                availability_report=avail_report,
                decision_time=decision_time,
                degraded_symbols=[],
            )

        # Infer expected features from the first row if not provided.
        if expected_features is None:
            expected_features = tuple(fv.name for fv in rows[0].features)

        # Build the availability report.
        avail_report = FeatureAvailabilityReport.from_rows(
            rows=rows,
            expected_features=expected_features,
        )

        # Export the snapshot.
        snapshot = self.export(
            rows=rows,
            decision_time=decision_time,
            expected_features=expected_features,
        )

        # Identify degraded symbols.
        degraded = [sym for sym in snapshot.symbols if not snapshot.availability.get(sym, False)]

        return SnapshotExportReceipt(
            snapshot=snapshot,
            availability_report=avail_report,
            decision_time=decision_time,
            degraded_symbols=degraded,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def export_feature_snapshot(
    rows: tuple[FeatureRow, ...],
    decision_time: int,
    config: SnapshotExportConfig | None = None,
    expected_features: tuple[str, ...] | None = None,
) -> FeatureSnapshot:
    """Export a compact feature snapshot from feature lake rows.

    Convenience entry point for TASK-0602. Creates a ``FeatureSnapshotExport``
    and runs it.
    """
    exporter = FeatureSnapshotExport(config=config)
    return exporter.export(
        rows=rows,
        decision_time=decision_time,
        expected_features=expected_features,
    )
