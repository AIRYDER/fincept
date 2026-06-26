"""
Tests for TASK-0602: Add Live Feature Snapshot Export.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `feature_snapshot_export.py`
exists.

Acceptance criteria covered:
- Feature snapshots are compact.
- Feature availability is measurable.
- Missing required features produce abstain or degraded state.

Additional checks from the spec:
- Snapshots have freshness metadata.
- If availability is too low, the worker abstains rather than predicting
  on incomplete data.
- Snapshots are exported from the feature lake (Builder 4's feature_lake.py)
  as compact, point-in-time feature vectors.

File-disjoint from Builder 4's `feature_lake.py` + `feature_availability.py`
(read-only imports). Does NOT modify them.
"""

from __future__ import annotations

import pytest
from quant_foundry.feature_availability import FeatureAvailabilityReport
from quant_foundry.feature_lake import (
    FeatureRow,
    FeatureValue,
    UniverseEntry,
)
from quant_foundry.feature_snapshot_export import (
    FeatureSnapshotExport,
    SnapshotExportConfig,
    export_feature_snapshot,
)
from quant_foundry.shadow_inference import FeatureSnapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_feature_rows(
    symbols: list[str] | None = None,
    n_features: int = 3,
    decision_time: int = 1000,
) -> tuple[tuple[FeatureRow, ...], tuple[UniverseEntry, ...]]:
    """Build minimal feature rows for testing."""
    if symbols is None:
        symbols = ["AAPL", "MSFT"]
    rows: list[FeatureRow] = []
    for sym in symbols:
        features = tuple(
            FeatureValue(name=f"f{i}", value=0.1 * i, observed_at=decision_time - 100)
            for i in range(n_features)
        )
        rows.append(
            FeatureRow(
                symbol=sym,
                event_ts=decision_time - 50,
                decision_time=decision_time,
                features=features,
            )
        )
    universe = tuple(UniverseEntry(symbol=s, listed_until=None) for s in symbols)
    return tuple(rows), universe


# ---------------------------------------------------------------------------
# SnapshotExportConfig
# ===========================================================================


class TestSnapshotExportConfig:
    """The config for a feature snapshot export."""

    def test_config_has_required_fields(self) -> None:
        """Config has min_availability_pct, max_freshness_ns, etc."""
        config = SnapshotExportConfig(
            min_availability_pct=80.0,
            max_freshness_ns=60_000_000_000,
        )
        assert config.min_availability_pct == 80.0
        assert config.max_freshness_ns == 60_000_000_000

    def test_config_defaults_are_reasonable(self) -> None:
        """Config has reasonable defaults."""
        config = SnapshotExportConfig()
        assert config.min_availability_pct > 0.0
        assert config.max_freshness_ns > 0

    def test_config_is_frozen(self) -> None:
        """Config is frozen (immutable for audit)."""
        config = SnapshotExportConfig()
        with pytest.raises((TypeError, ValueError)):
            config.min_availability_pct = 50.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FeatureSnapshotExport — compact snapshots
# ===========================================================================


class TestCompactSnapshots:
    """Feature snapshots are compact."""

    def test_export_produces_feature_snapshot(self) -> None:
        """Export produces a FeatureSnapshot suitable for shadow inference."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(rows=rows, decision_time=1000)
        assert isinstance(snapshot, FeatureSnapshot)
        assert len(snapshot.symbols) > 0
        assert len(snapshot.features) > 0

    def test_snapshot_features_are_compact_vectors(self) -> None:
        """Snapshot features are compact float vectors (not full FeatureRow objects)."""
        rows, _ = _make_feature_rows(n_features=3)
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(rows=rows, decision_time=1000)
        for sym in snapshot.symbols:
            # Each symbol has a compact list of floats.
            assert isinstance(snapshot.features[sym], list)
            assert all(isinstance(v, float) for v in snapshot.features[sym])

    def test_snapshot_includes_timestamp(self) -> None:
        """Snapshot includes the decision time as ts_event."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(rows=rows, decision_time=1000)
        assert snapshot.ts_event == 1000

    def test_snapshot_includes_freshness(self) -> None:
        """Snapshot includes freshness metadata."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(rows=rows, decision_time=1000)
        assert snapshot.freshness_ns >= 0


# ---------------------------------------------------------------------------
# Feature availability is measurable
# ===========================================================================


class TestFeatureAvailability:
    """Feature availability is measurable."""

    def test_export_includes_availability_report(self) -> None:
        """Export includes a FeatureAvailabilityReport."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        assert isinstance(receipt.availability_report, FeatureAvailabilityReport)

    def test_availability_report_counts_present_features(self) -> None:
        """Availability report counts how many rows have each feature."""
        rows, _ = _make_feature_rows(n_features=3)
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        # All 2 rows have all 3 features.
        assert receipt.availability_report.total_rows == 2
        for f in ("f0", "f1", "f2"):
            assert receipt.availability_report.availability_pct(f) == 100.0

    def test_availability_report_detects_missing_features(self) -> None:
        """Availability report detects features that are missing in some rows."""
        # Row 1 has f0, f1, f2; Row 2 has only f0, f1.
        rows = (
            FeatureRow(
                symbol="AAPL",
                event_ts=950,
                decision_time=1000,
                features=(
                    FeatureValue(name="f0", value=0.0, observed_at=900),
                    FeatureValue(name="f1", value=0.1, observed_at=900),
                    FeatureValue(name="f2", value=0.2, observed_at=900),
                ),
            ),
            FeatureRow(
                symbol="MSFT",
                event_ts=950,
                decision_time=1000,
                features=(
                    FeatureValue(name="f0", value=0.0, observed_at=900),
                    FeatureValue(name="f1", value=0.1, observed_at=900),
                ),
            ),
        )
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(
            rows=rows,
            decision_time=1000,
            expected_features=("f0", "f1", "f2"),
        )
        # f0: 2/2 = 100%, f1: 2/2 = 100%, f2: 1/2 = 50%.
        assert receipt.availability_report.availability_pct("f0") == 100.0
        assert receipt.availability_report.availability_pct("f1") == 100.0
        assert receipt.availability_report.availability_pct("f2") == 50.0

    def test_snapshot_availability_flags_are_set(self) -> None:
        """Snapshot availability flags are set per symbol."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(rows=rows, decision_time=1000)
        for sym in snapshot.symbols:
            assert sym in snapshot.availability
            assert snapshot.availability[sym] is True


# ---------------------------------------------------------------------------
# Missing required features produce abstain or degraded state
# ===========================================================================


class TestMissingFeaturesAbstain:
    """Missing required features produce abstain or degraded state."""

    def test_low_availability_produces_degraded_snapshot(self) -> None:
        """Low availability produces a degraded snapshot (availability=False)."""
        # Row with only 1 of 3 features.
        rows = (
            FeatureRow(
                symbol="AAPL",
                event_ts=950,
                decision_time=1000,
                features=(FeatureValue(name="f0", value=0.0, observed_at=900),),
            ),
        )
        config = SnapshotExportConfig(min_availability_pct=80.0)
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(
            rows=rows,
            decision_time=1000,
            expected_features=("f0", "f1", "f2"),
        )
        # AAPL has only 33% availability — below 80% threshold.
        assert snapshot.availability["AAPL"] is False

    def test_high_availability_produces_healthy_snapshot(self) -> None:
        """High availability produces a healthy snapshot (availability=True)."""
        rows, _ = _make_feature_rows(n_features=3)
        config = SnapshotExportConfig(min_availability_pct=80.0)
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(
            rows=rows,
            decision_time=1000,
            expected_features=("f0", "f1", "f2"),
        )
        for sym in snapshot.symbols:
            assert snapshot.availability[sym] is True

    def test_receipt_includes_degraded_symbols(self) -> None:
        """The export receipt includes degraded symbols (below availability threshold)."""
        rows = (
            FeatureRow(
                symbol="AAPL",
                event_ts=950,
                decision_time=1000,
                features=(
                    FeatureValue(name="f0", value=0.0, observed_at=900),
                    FeatureValue(name="f1", value=0.1, observed_at=900),
                    FeatureValue(name="f2", value=0.2, observed_at=900),
                ),
            ),
            FeatureRow(
                symbol="MSFT",
                event_ts=950,
                decision_time=1000,
                features=(FeatureValue(name="f0", value=0.0, observed_at=900),),
            ),
        )
        config = SnapshotExportConfig(min_availability_pct=80.0)
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(
            rows=rows,
            decision_time=1000,
            expected_features=("f0", "f1", "f2"),
        )
        assert "AAPL" not in receipt.degraded_symbols
        assert "MSFT" in receipt.degraded_symbols

    def test_empty_rows_produce_empty_snapshot(self) -> None:
        """Empty rows produce an empty snapshot (abstain)."""
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        snapshot = export.export(rows=(), decision_time=1000)
        assert len(snapshot.symbols) == 0
        assert len(snapshot.features) == 0


# ---------------------------------------------------------------------------
# Export receipt
# ===========================================================================


class TestExportReceipt:
    """The export receipt records what was emitted."""

    def test_receipt_has_snapshot(self) -> None:
        """The receipt includes the exported snapshot."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        assert isinstance(receipt.snapshot, FeatureSnapshot)

    def test_receipt_has_availability_report(self) -> None:
        """The receipt includes the availability report."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        assert isinstance(receipt.availability_report, FeatureAvailabilityReport)

    def test_receipt_has_decision_time(self) -> None:
        """The receipt includes the decision time."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        assert receipt.decision_time == 1000

    def test_receipt_has_degraded_symbols(self) -> None:
        """The receipt includes the list of degraded symbols."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        assert isinstance(receipt.degraded_symbols, list)

    def test_receipt_to_dict_is_json_serializable(self) -> None:
        """The receipt can be serialized to JSON."""
        import json

        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        d = receipt.to_dict()
        json.dumps(d)
        assert "snapshot" in d
        assert "availability_report" in d
        assert "degraded_symbols" in d


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestExportFeatureSnapshot:
    """The convenience function export_feature_snapshot works end-to-end."""

    def test_export_feature_snapshot_returns_snapshot(self) -> None:
        """export_feature_snapshot returns a FeatureSnapshot."""
        rows, _ = _make_feature_rows()
        snapshot = export_feature_snapshot(rows=rows, decision_time=1000)
        assert isinstance(snapshot, FeatureSnapshot)
        assert len(snapshot.symbols) > 0

    def test_export_feature_snapshot_with_config(self) -> None:
        """export_feature_snapshot accepts a config."""
        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig(min_availability_pct=50.0)
        snapshot = export_feature_snapshot(rows=rows, decision_time=1000, config=config)
        assert isinstance(snapshot, FeatureSnapshot)


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInSnapshotExport:
    """Snapshot export output must not leak secrets."""

    @pytest.mark.parametrize(
        "secret_field",
        [
            "api_key",
            "token",
            "secret",
            "password",
            "broker_account",
            "credential",
        ],
    )
    def test_config_has_no_secret_fields(self, secret_field: str) -> None:
        """SnapshotExportConfig must not have any secret-named field."""
        fields = set(SnapshotExportConfig.model_fields.keys())
        assert secret_field not in fields

    def test_receipt_to_dict_has_no_secret_keys(self) -> None:

        rows, _ = _make_feature_rows()
        config = SnapshotExportConfig()
        export = FeatureSnapshotExport(config=config)
        receipt = export.export_with_receipt(rows=rows, decision_time=1000)
        d = receipt.to_dict()

        def _has_secret(d: object, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password", "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
