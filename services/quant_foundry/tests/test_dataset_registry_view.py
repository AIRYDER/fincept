"""Tests for the dataset registry view layer (T-UI.2).

Covers config/row construction, rendering, filtering, sorting,
formatting helpers, blocking-reason logic, and the fail-closed
``validate_no_false_readiness`` invariant.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quant_foundry.ui.dataset_registry_view import (
    DatasetRegistryRow,
    DatasetRegistryView,
    DatasetRegistryViewConfig,
    format_quality_gate,
    format_readiness,
    format_upload_status,
    get_blocking_reasons,
    validate_no_false_readiness,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _row(
    dataset_id: str = "ds-001",
    readiness_level: str = "L3_production",
    manifest_hash: str | None = "sha256:abc",
    quality_gate_status: str | None = "passed",
    upload_status: str | None = "verified",
    eligible_modes: list[str] | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    blocking_reasons: list[str] | None = None,
) -> DatasetRegistryRow:
    """Build a row with production-eligible defaults."""
    return DatasetRegistryRow(
        dataset_id=dataset_id,
        readiness_level=readiness_level,
        manifest_hash=manifest_hash,
        quality_gate_status=quality_gate_status,
        upload_status=upload_status,
        eligible_modes=eligible_modes if eligible_modes is not None else ["production"],
        created_at=created_at,
        blocking_reasons=blocking_reasons if blocking_reasons is not None else [],
    )


def _sample_rows() -> list[DatasetRegistryRow]:
    """A representative mix of rows across readiness levels."""
    return [
        _row("ds-001", "L4_golden", "h1", "passed", "verified", ["production", "research"], "2026-01-01"),
        _row("ds-002", "L3_production", "h2", "passed", "verified", ["production"], "2026-01-02"),
        _row("ds-003", "L2_validated", "h3", "pending", "uploaded", ["research"], "2026-01-03"),
        _row("ds-004", "L1_cleaned", None, "failed", "staged", ["canary"], "2026-01-04"),
        _row("ds-005", "L0_raw", None, "not_run", None, [], "2026-01-05"),
    ]


# ---------------------------------------------------------------------------
# DatasetRegistryViewConfig
# ---------------------------------------------------------------------------


class TestDatasetRegistryViewConfig:
    def test_defaults(self) -> None:
        cfg = DatasetRegistryViewConfig()
        assert cfg.show_manifest_hash is True
        assert cfg.show_quality_gate is True
        assert cfg.show_upload_status is True
        assert cfg.show_eligible_modes is True
        assert cfg.show_readiness_level is True
        assert cfg.max_rows == 100
        assert cfg.sort_by == "dataset_id"
        assert cfg.sort_order == "asc"

    def test_custom_values(self) -> None:
        cfg = DatasetRegistryViewConfig(
            show_manifest_hash=False,
            show_quality_gate=False,
            show_upload_status=False,
            show_eligible_modes=False,
            show_readiness_level=False,
            max_rows=50,
            sort_by="readiness_level",
            sort_order="desc",
        )
        assert cfg.show_manifest_hash is False
        assert cfg.max_rows == 50
        assert cfg.sort_by == "readiness_level"
        assert cfg.sort_order == "desc"

    def test_frozen(self) -> None:
        cfg = DatasetRegistryViewConfig()
        with pytest.raises(ValidationError):
            cfg.max_rows = 10  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryViewConfig(unknown_field=True)  # type: ignore[call-arg]

    def test_max_rows_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryViewConfig(max_rows=0)

    def test_max_rows_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryViewConfig(max_rows=-5)

    def test_invalid_sort_by(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryViewConfig(sort_by="unknown")

    def test_invalid_sort_order(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryViewConfig(sort_order="random")

    def test_all_valid_sort_by(self) -> None:
        for field in ["dataset_id", "readiness_level", "created_at"]:
            cfg = DatasetRegistryViewConfig(sort_by=field)
            assert cfg.sort_by == field

    def test_all_valid_sort_order(self) -> None:
        for order in ["asc", "desc"]:
            cfg = DatasetRegistryViewConfig(sort_order=order)
            assert cfg.sort_order == order


# ---------------------------------------------------------------------------
# DatasetRegistryRow
# ---------------------------------------------------------------------------


class TestDatasetRegistryRow:
    def test_construction_defaults(self) -> None:
        row = DatasetRegistryRow(dataset_id="ds-1", readiness_level="L0_raw")
        assert row.dataset_id == "ds-1"
        assert row.readiness_level == "L0_raw"
        assert row.manifest_hash is None
        assert row.quality_gate_status is None
        assert row.upload_status is None
        assert row.eligible_modes == []
        assert row.created_at == ""
        assert row.blocking_reasons == []

    def test_construction_full(self) -> None:
        row = _row()
        assert row.dataset_id == "ds-001"
        assert row.readiness_level == "L3_production"
        assert row.manifest_hash == "sha256:abc"
        assert row.quality_gate_status == "passed"
        assert row.upload_status == "verified"
        assert row.eligible_modes == ["production"]
        assert row.created_at == "2026-01-01T00:00:00Z"

    def test_frozen(self) -> None:
        row = _row()
        with pytest.raises(ValidationError):
            row.dataset_id = "changed"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryRow(  # type: ignore[call-arg]
                dataset_id="ds-1", readiness_level="L0_raw", extra="bad"
            )

    def test_invalid_readiness_level(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryRow(dataset_id="ds-1", readiness_level="L5_unknown")

    def test_invalid_quality_gate_status(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryRow(
                dataset_id="ds-1", readiness_level="L0_raw", quality_gate_status="bogus"
            )

    def test_invalid_upload_status(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryRow(
                dataset_id="ds-1", readiness_level="L0_raw", upload_status="bogus"
            )

    def test_invalid_eligible_mode(self) -> None:
        with pytest.raises(ValidationError):
            DatasetRegistryRow(
                dataset_id="ds-1", readiness_level="L0_raw", eligible_modes=["backtest"]
            )

    def test_none_quality_gate_allowed(self) -> None:
        row = DatasetRegistryRow(
            dataset_id="ds-1", readiness_level="L0_raw", quality_gate_status=None
        )
        assert row.quality_gate_status is None

    def test_none_upload_status_allowed(self) -> None:
        row = DatasetRegistryRow(
            dataset_id="ds-1", readiness_level="L0_raw", upload_status=None
        )
        assert row.upload_status is None


# ---------------------------------------------------------------------------
# format_readiness
# ---------------------------------------------------------------------------


class TestFormatReadiness:
    @pytest.mark.parametrize(
        "level,expected",
        [
            ("L0_raw", "[L0]"),
            ("L1_cleaned", "[L1]"),
            ("L2_validated", "[L2]"),
            ("L3_production", "[L3]"),
            ("L4_golden", "[L4]"),
        ],
    )
    def test_valid_levels(self, level: str, expected: str) -> None:
        assert format_readiness(level) == expected

    def test_empty_string(self) -> None:
        assert format_readiness("") == "[??]"

    def test_no_underscore(self) -> None:
        assert format_readiness("L3") == "[??]"


# ---------------------------------------------------------------------------
# format_quality_gate
# ---------------------------------------------------------------------------


class TestFormatQualityGate:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("passed", "[PASS]"),
            ("failed", "[FAIL]"),
            ("pending", "[PEND]"),
            ("not_run", "[—]"),
            (None, "—"),
        ],
    )
    def test_known_statuses(self, status: str | None, expected: str) -> None:
        assert format_quality_gate(status) == expected

    def test_unknown_status(self) -> None:
        assert format_quality_gate("bogus") == "—"


# ---------------------------------------------------------------------------
# format_upload_status
# ---------------------------------------------------------------------------


class TestFormatUploadStatus:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("staged", "[STAGED]"),
            ("uploaded", "[UPLOADED]"),
            ("verified", "[VERIFIED]"),
            ("failed", "[FAILED]"),
            (None, "—"),
        ],
    )
    def test_known_statuses(self, status: str | None, expected: str) -> None:
        assert format_upload_status(status) == expected

    def test_unknown_status(self) -> None:
        assert format_upload_status("bogus") == "—"


# ---------------------------------------------------------------------------
# get_blocking_reasons
# ---------------------------------------------------------------------------


class TestGetBlockingReasons:
    def test_production_eligible_no_reasons(self) -> None:
        row = _row(readiness_level="L3_production", quality_gate_status="passed",
                   upload_status="verified", manifest_hash="h")
        assert get_blocking_reasons(row) == []

    def test_golden_eligible_no_reasons(self) -> None:
        row = _row(readiness_level="L4_golden", quality_gate_status="passed",
                   upload_status="verified", manifest_hash="h")
        assert get_blocking_reasons(row) == []

    def test_low_readiness_blocks(self) -> None:
        row = _row(readiness_level="L2_validated", quality_gate_status="passed",
                   upload_status="verified", manifest_hash="h")
        reasons = get_blocking_reasons(row)
        assert any("below L3_production" in r for r in reasons)

    def test_quality_gate_not_passed_blocks(self) -> None:
        row = _row(readiness_level="L3_production", quality_gate_status="pending",
                   upload_status="verified", manifest_hash="h")
        reasons = get_blocking_reasons(row)
        assert any("not 'passed'" in r for r in reasons)

    def test_upload_not_verified_blocks(self) -> None:
        row = _row(readiness_level="L3_production", quality_gate_status="passed",
                   upload_status="uploaded", manifest_hash="h")
        reasons = get_blocking_reasons(row)
        assert any("not 'verified'" in r for r in reasons)

    def test_missing_manifest_hash_blocks(self) -> None:
        row = _row(readiness_level="L3_production", quality_gate_status="passed",
                   upload_status="verified", manifest_hash=None)
        reasons = get_blocking_reasons(row)
        assert any("manifest_hash missing" in r for r in reasons)

    def test_all_four_reasons(self) -> None:
        row = _row(readiness_level="L0_raw", quality_gate_status="failed",
                   upload_status="staged", manifest_hash=None)
        reasons = get_blocking_reasons(row)
        assert len(reasons) == 4

    def test_quality_gate_none_blocks(self) -> None:
        row = _row(readiness_level="L3_production", quality_gate_status=None,
                   upload_status="verified", manifest_hash="h")
        reasons = get_blocking_reasons(row)
        assert any("not 'passed'" in r for r in reasons)


# ---------------------------------------------------------------------------
# validate_no_false_readiness
# ---------------------------------------------------------------------------


class TestValidateNoFalseReadiness:
    def test_honest_production(self) -> None:
        row = _row(readiness_level="L3_production", quality_gate_status="passed",
                   eligible_modes=["production"])
        assert validate_no_false_readiness(row) is True

    def test_honest_golden(self) -> None:
        row = _row(readiness_level="L4_golden", quality_gate_status="passed",
                   eligible_modes=["production", "research"])
        assert validate_no_false_readiness(row) is True

    def test_honest_no_production_claim(self) -> None:
        row = _row(readiness_level="L0_raw", quality_gate_status="not_run",
                   eligible_modes=["canary"])
        assert validate_no_false_readiness(row) is True

    def test_false_readiness_low_level(self) -> None:
        row = _row(readiness_level="L2_validated", quality_gate_status="passed",
                   eligible_modes=["production"])
        with pytest.raises(ValueError, match="receipts do not prove"):
            validate_no_false_readiness(row)

    def test_false_readiness_gate_not_passed(self) -> None:
        row = _row(readiness_level="L3_production", quality_gate_status="failed",
                   eligible_modes=["production"])
        with pytest.raises(ValueError, match="receipts do not prove"):
            validate_no_false_readiness(row)

    def test_false_readiness_both_fail(self) -> None:
        row = _row(readiness_level="L1_cleaned", quality_gate_status="not_run",
                   eligible_modes=["production"])
        with pytest.raises(ValueError):
            validate_no_false_readiness(row)

    def test_empty_modes_honest(self) -> None:
        row = _row(readiness_level="L0_raw", quality_gate_status="not_run",
                   eligible_modes=[])
        assert validate_no_false_readiness(row) is True


# ---------------------------------------------------------------------------
# DatasetRegistryView.render
# ---------------------------------------------------------------------------


class TestRender:
    def test_all_columns(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render(_sample_rows())
        assert "Dataset ID" in out
        assert "Readiness" in out
        assert "Manifest Hash" in out
        assert "Quality Gate" in out
        assert "Upload" in out
        assert "Eligible Modes" in out

    def test_partial_columns(self) -> None:
        cfg = DatasetRegistryViewConfig(
            show_manifest_hash=False,
            show_quality_gate=False,
            show_upload_status=False,
            show_eligible_modes=False,
            show_readiness_level=False,
        )
        view = DatasetRegistryView(cfg)
        out = view.render(_sample_rows())
        assert "Dataset ID" in out
        assert "Readiness" not in out
        assert "Manifest Hash" not in out
        assert "Quality Gate" not in out

    def test_empty_list(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render([])
        assert "(no datasets)" in out

    def test_single_dataset(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render([_row("ds-only")])
        assert "ds-only" in out

    def test_max_rows_truncation(self) -> None:
        rows = _sample_rows()
        cfg = DatasetRegistryViewConfig(max_rows=2)
        view = DatasetRegistryView(cfg)
        out = view.render(rows)
        # header + separator + 2 data rows = 4 lines
        lines = [l for l in out.split("\n") if l and not l.startswith("-")]
        assert len(lines) == 3  # header + 2 data rows

    def test_readiness_badge_in_output(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render([_row(readiness_level="L4_golden")])
        assert "[L4]" in out


# ---------------------------------------------------------------------------
# DatasetRegistryView.render_summary
# ---------------------------------------------------------------------------


class TestRenderSummary:
    def test_total_count(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render_summary(_sample_rows())
        assert "Total datasets: 5" in out

    def test_by_readiness(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render_summary(_sample_rows())
        assert "[L4]" in out
        assert "[L3]" in out
        assert "[L2]" in out
        assert "[L1]" in out
        assert "[L0]" in out

    def test_by_quality_gate(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render_summary(_sample_rows())
        assert "passed" in out
        assert "pending" in out
        assert "failed" in out

    def test_production_eligible_count(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render_summary(_sample_rows())
        assert "Production-eligible: 2" in out

    def test_empty_summary(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render_summary([])
        assert "Total datasets: 0" in out
        assert "Production-eligible: 0" in out


# ---------------------------------------------------------------------------
# DatasetRegistryView.render_dataset_detail
# ---------------------------------------------------------------------------


class TestRenderDatasetDetail:
    def test_includes_blocking_reasons(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        row = _row(readiness_level="L0_raw", quality_gate_status="failed",
                   upload_status="staged", manifest_hash=None)
        out = view.render_dataset_detail(row)
        assert "Blocking Reasons" in out
        assert "below L3_production" in out
        assert "manifest_hash missing" in out

    def test_production_eligible_no_blocking(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        row = _row(readiness_level="L3_production", quality_gate_status="passed",
                   upload_status="verified", manifest_hash="h")
        out = view.render_dataset_detail(row)
        assert "production-eligible" in out

    def test_includes_all_fields(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        row = _row()
        out = view.render_dataset_detail(row)
        assert row.dataset_id in out
        assert "[L3]" in out
        assert "[PASS]" in out
        assert "[VERIFIED]" in out

    def test_claims_production_line(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        row = _row(eligible_modes=["production", "research"])
        out = view.render_dataset_detail(row)
        assert "Claims Production" in out


# ---------------------------------------------------------------------------
# filter_by_readiness
# ---------------------------------------------------------------------------


class TestFilterByReadiness:
    def test_filter_l4(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        result = view.filter_by_readiness(_sample_rows(), "L4_golden")
        assert len(result) == 1
        assert result[0].dataset_id == "ds-001"

    def test_filter_l0(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        result = view.filter_by_readiness(_sample_rows(), "L0_raw")
        assert len(result) == 1
        assert result[0].dataset_id == "ds-005"

    def test_filter_no_match(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        result = view.filter_by_readiness([_row(readiness_level="L3_production")], "L0_raw")
        assert result == []

    def test_invalid_level_raises(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        with pytest.raises(ValueError):
            view.filter_by_readiness(_sample_rows(), "L5_unknown")


# ---------------------------------------------------------------------------
# filter_production_eligible
# ---------------------------------------------------------------------------


class TestFilterProductionEligible:
    def test_returns_only_eligible(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        result = view.filter_production_eligible(_sample_rows())
        assert len(result) == 2
        ids = {r.dataset_id for r in result}
        assert ids == {"ds-001", "ds-002"}

    def test_empty_list(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        assert view.filter_production_eligible([]) == []

    def test_all_l0(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        rows = [_row("ds-a", "L0_raw", None, "not_run", None, []),
                _row("ds-b", "L0_raw", None, "not_run", None, [])]
        assert view.filter_production_eligible(rows) == []

    def test_all_l4(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        rows = [_row("ds-a", "L4_golden", "h", "passed", "verified", ["production"]),
                _row("ds-b", "L4_golden", "h", "passed", "verified", ["production"])]
        result = view.filter_production_eligible(rows)
        assert len(result) == 2

    def test_l3_gate_not_passed_excluded(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        row = _row(readiness_level="L3_production", quality_gate_status="pending")
        assert view.filter_production_eligible([row]) == []


# ---------------------------------------------------------------------------
# sort_rows
# ---------------------------------------------------------------------------


class TestSortRows:
    def test_sort_by_dataset_id_asc(self) -> None:
        cfg = DatasetRegistryViewConfig(sort_by="dataset_id", sort_order="asc")
        view = DatasetRegistryView(cfg)
        result = view.sort_rows(_sample_rows())
        assert [r.dataset_id for r in result] == [
            "ds-001", "ds-002", "ds-003", "ds-004", "ds-005"
        ]

    def test_sort_by_dataset_id_desc(self) -> None:
        cfg = DatasetRegistryViewConfig(sort_by="dataset_id", sort_order="desc")
        view = DatasetRegistryView(cfg)
        result = view.sort_rows(_sample_rows())
        assert result[0].dataset_id == "ds-005"

    def test_sort_by_readiness_level_asc(self) -> None:
        cfg = DatasetRegistryViewConfig(sort_by="readiness_level", sort_order="asc")
        view = DatasetRegistryView(cfg)
        result = view.sort_rows(_sample_rows())
        assert result[0].readiness_level == "L0_raw"
        assert result[-1].readiness_level == "L4_golden"

    def test_sort_by_readiness_level_desc(self) -> None:
        cfg = DatasetRegistryViewConfig(sort_by="readiness_level", sort_order="desc")
        view = DatasetRegistryView(cfg)
        result = view.sort_rows(_sample_rows())
        assert result[0].readiness_level == "L4_golden"
        assert result[-1].readiness_level == "L0_raw"

    def test_sort_by_created_at_asc(self) -> None:
        cfg = DatasetRegistryViewConfig(sort_by="created_at", sort_order="asc")
        view = DatasetRegistryView(cfg)
        result = view.sort_rows(_sample_rows())
        assert result[0].created_at == "2026-01-01"

    def test_sort_empty(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        assert view.sort_rows([]) == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_all_l0_render(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        rows = [_row("ds-a", "L0_raw", None, "not_run", None, []),
                _row("ds-b", "L0_raw", None, "not_run", None, [])]
        out = view.render(rows)
        assert "[L0]" in out
        assert "ds-a" in out

    def test_all_l4_summary(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        rows = [_row("ds-a", "L4_golden", "h", "passed", "verified", ["production"]),
                _row("ds-b", "L4_golden", "h", "passed", "verified", ["production"])]
        out = view.render_summary(rows)
        assert "Production-eligible: 2" in out

    def test_single_row_detail(self) -> None:
        view = DatasetRegistryView(DatasetRegistryViewConfig())
        out = view.render_dataset_detail(_row("solo"))
        assert "solo" in out
