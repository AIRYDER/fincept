"""Tests for quant_foundry.verification_matrix (T-TV.1).

Covers the VerificationTier / VerificationCategory enums, the
VerificationTestSpec / VerificationResult / VerificationMatrixConfig
Pydantic v2 models (construction + validation), the VerificationMatrix
registry/runner/reporter, and the module-level helpers
``summarize_results`` and ``format_result_table``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from quant_foundry.verification_matrix import (
    VerificationCategory,
    VerificationMatrix,
    VerificationMatrixConfig,
    VerificationResult,
    VerificationTier,
    VerificationTestSpec,
    format_result_table,
    summarize_results,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passing_fn() -> None:
    """A test function that always passes."""
    assert True


def _failing_fn() -> None:
    """A test function that always fails."""
    raise AssertionError("boom")


def _make_spec(
    *,
    test_id: str = "unit_schema_validation_test1",
    tier: VerificationTier = VerificationTier.UNIT,
    category: VerificationCategory = VerificationCategory.SCHEMA_VALIDATION,
    name: str = "test1",
    module: str = __name__,
    test_function: str = "_passing_fn",
    min_passes: int = 1,
    timeout_seconds: float = 30.0,
    enabled: bool = True,
) -> VerificationTestSpec:
    """Build a VerificationTestSpec with sensible defaults."""
    return VerificationTestSpec(
        test_id=test_id,
        tier=tier,
        category=category,
        name=name,
        description="A test spec",
        module=module,
        test_function=test_function,
        min_passes=min_passes,
        timeout_seconds=timeout_seconds,
        enabled=enabled,
    )


def _make_result(
    *,
    test_id: str = "unit_schema_validation_test1",
    tier: VerificationTier = VerificationTier.UNIT,
    category: VerificationCategory = VerificationCategory.SCHEMA_VALIDATION,
    passed: bool = True,
    n_passes: int = 1,
    n_failures: int = 0,
    duration_seconds: float = 0.01,
    error: str | None = None,
) -> VerificationResult:
    """Build a VerificationResult with sensible defaults."""
    return VerificationResult(
        test_id=test_id,
        tier=tier,
        category=category,
        passed=passed,
        n_passes=n_passes,
        n_failures=n_failures,
        duration_seconds=duration_seconds,
        error=error,
    )


def _full_config() -> VerificationMatrixConfig:
    """A config that includes all tiers and all categories."""
    return VerificationMatrixConfig(
        tiers=list(VerificationTier),
        categories=list(VerificationCategory),
    )


# ---------------------------------------------------------------------------
# VerificationTier enum
# ---------------------------------------------------------------------------


class TestVerificationTier:
    """Tests for the VerificationTier StrEnum."""

    def test_unit_value(self) -> None:
        assert VerificationTier.UNIT.value == "unit"

    def test_integration_value(self) -> None:
        assert VerificationTier.INTEGRATION.value == "integration"

    def test_canary_value(self) -> None:
        assert VerificationTier.CANARY.value == "canary"

    def test_hosted_value(self) -> None:
        assert VerificationTier.HOSTED.value == "hosted"

    def test_is_str_enum(self) -> None:
        assert isinstance(VerificationTier.UNIT, str)

    def test_member_count(self) -> None:
        assert len(list(VerificationTier)) == 4


# ---------------------------------------------------------------------------
# VerificationCategory enum
# ---------------------------------------------------------------------------


class TestVerificationCategory:
    """Tests for the VerificationCategory StrEnum."""

    def test_schema_validation_value(self) -> None:
        assert (
            VerificationCategory.SCHEMA_VALIDATION.value == "schema_validation"
        )

    def test_manifest_hashing_value(self) -> None:
        assert (
            VerificationCategory.MANIFEST_HASHING.value == "manifest_hashing"
        )

    def test_request_creation_value(self) -> None:
        assert (
            VerificationCategory.REQUEST_CREATION.value == "request_creation"
        )

    def test_callback_verification_value(self) -> None:
        assert (
            VerificationCategory.CALLBACK_VERIFICATION.value
            == "callback_verification"
        )

    def test_artifact_verification_value(self) -> None:
        assert (
            VerificationCategory.ARTIFACT_VERIFICATION.value
            == "artifact_verification"
        )

    def test_promotion_gate_value(self) -> None:
        assert VerificationCategory.PROMOTION_GATE.value == "promotion_gate"

    def test_pit_safety_value(self) -> None:
        assert VerificationCategory.PIT_SAFETY.value == "pit_safety"

    def test_cost_tracking_value(self) -> None:
        assert VerificationCategory.COST_TRACKING.value == "cost_tracking"

    def test_member_count(self) -> None:
        assert len(list(VerificationCategory)) == 8

    def test_is_str_enum(self) -> None:
        assert isinstance(VerificationCategory.SCHEMA_VALIDATION, str)


# ---------------------------------------------------------------------------
# VerificationTestSpec
# ---------------------------------------------------------------------------


class TestVerificationTestSpec:
    """Tests for VerificationTestSpec construction and validation."""

    def test_valid_construction(self) -> None:
        spec = _make_spec()
        assert spec.test_id == "unit_schema_validation_test1"
        assert spec.tier == VerificationTier.UNIT
        assert spec.category == VerificationCategory.SCHEMA_VALIDATION
        assert spec.min_passes == 1
        assert spec.timeout_seconds == 30.0
        assert spec.enabled is True

    def test_frozen(self) -> None:
        spec = _make_spec()
        with pytest.raises(ValidationError):
            spec.test_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            VerificationTestSpec(
                test_id="t",
                tier=VerificationTier.UNIT,
                category=VerificationCategory.SCHEMA_VALIDATION,
                name="n",
                description="d",
                module="m",
                test_function="f",
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_empty_test_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_spec(test_id="")

    def test_whitespace_test_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_spec(test_id="   ")

    def test_min_passes_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_spec(min_passes=0)

    def test_min_passes_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_spec(min_passes=-1)

    def test_min_passes_one_allowed(self) -> None:
        spec = _make_spec(min_passes=1)
        assert spec.min_passes == 1

    def test_timeout_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_spec(timeout_seconds=0.0)

    def test_timeout_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_spec(timeout_seconds=-1.0)

    def test_timeout_positive_allowed(self) -> None:
        spec = _make_spec(timeout_seconds=0.001)
        assert spec.timeout_seconds == 0.001


# ---------------------------------------------------------------------------
# VerificationResult
# ---------------------------------------------------------------------------


class TestVerificationResult:
    """Tests for VerificationResult construction."""

    def test_valid_construction(self) -> None:
        r = _make_result()
        assert r.passed is True
        assert r.n_passes == 1
        assert r.n_failures == 0
        assert r.error is None

    def test_frozen(self) -> None:
        r = _make_result()
        with pytest.raises(ValidationError):
            r.passed = False  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            VerificationResult(
                test_id="t",
                tier=VerificationTier.UNIT,
                category=VerificationCategory.SCHEMA_VALIDATION,
                passed=True,
                n_passes=1,
                n_failures=0,
                duration_seconds=0.1,
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_error_optional(self) -> None:
        r = _make_result(error="something failed")
        assert r.error == "something failed"


# ---------------------------------------------------------------------------
# VerificationMatrixConfig
# ---------------------------------------------------------------------------


class TestVerificationMatrixConfig:
    """Tests for VerificationMatrixConfig construction and validation."""

    def test_valid_construction(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=[VerificationTier.UNIT],
            categories=[VerificationCategory.SCHEMA_VALIDATION],
        )
        assert cfg.tiers == [VerificationTier.UNIT]
        assert cfg.fail_fast is False
        assert cfg.parallel is False
        assert cfg.report_dir == "reports/verification"

    def test_frozen(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=[VerificationTier.UNIT],
            categories=[VerificationCategory.SCHEMA_VALIDATION],
        )
        with pytest.raises(ValidationError):
            cfg.fail_fast = True  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            VerificationMatrixConfig(
                tiers=[VerificationTier.UNIT],
                categories=[VerificationCategory.SCHEMA_VALIDATION],
                unexpected=1,  # type: ignore[call-arg]
            )

    def test_empty_tiers_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VerificationMatrixConfig(
                tiers=[],
                categories=[VerificationCategory.SCHEMA_VALIDATION],
            )

    def test_empty_categories_rejected(self) -> None:
        with pytest.raises(ValidationError):
            VerificationMatrixConfig(
                tiers=[VerificationTier.UNIT],
                categories=[],
            )

    def test_fail_fast_default_false(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=[VerificationTier.UNIT],
            categories=[VerificationCategory.SCHEMA_VALIDATION],
        )
        assert cfg.fail_fast is False

    def test_fail_fast_true(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=[VerificationTier.UNIT],
            categories=[VerificationCategory.SCHEMA_VALIDATION],
            fail_fast=True,
        )
        assert cfg.fail_fast is True


# ---------------------------------------------------------------------------
# VerificationMatrix.register_test
# ---------------------------------------------------------------------------


class TestRegisterTest:
    """Tests for VerificationMatrix.register_test."""

    def test_register_single(self) -> None:
        matrix = VerificationMatrix(_full_config())
        spec = _make_spec()
        matrix.register_test(spec)
        results = matrix.run()
        assert len(results) == 1
        assert results[0].passed

    def test_register_duplicate_rejected(self) -> None:
        matrix = VerificationMatrix(_full_config())
        spec = _make_spec()
        matrix.register_test(spec)
        with pytest.raises(ValueError, match="duplicate test_id"):
            matrix.register_test(spec)

    def test_register_multiple_distinct(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(_make_spec(test_id="t1", test_function="_passing_fn"))
        matrix.register_test(
            _make_spec(
                test_id="t2",
                category=VerificationCategory.MANIFEST_HASHING,
                test_function="_passing_fn",
            ),
        )
        results = matrix.run()
        assert len(results) == 2


# ---------------------------------------------------------------------------
# VerificationMatrix.register_default_tests
# ---------------------------------------------------------------------------


class TestRegisterDefaultTests:
    """Tests for VerificationMatrix.register_default_tests."""

    def test_registers_all_categories(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_default_tests()
        results = matrix.run()
        categories_seen = {r.category for r in results}
        # Every category should have at least one default test.
        assert categories_seen == set(VerificationCategory)

    def test_default_tests_pass(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_default_tests()
        results = matrix.run()
        assert len(results) > 0
        assert matrix.is_passing(results)

    def test_register_default_idempotent(self) -> None:
        """Calling register_default_tests twice does not duplicate."""
        matrix = VerificationMatrix(_full_config())
        matrix.register_default_tests()
        count_before = len(matrix._specs)
        matrix.register_default_tests()
        count_after = len(matrix._specs)
        assert count_before == count_after

    def test_default_tests_are_unit_tier(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_default_tests()
        results = matrix.run()
        assert all(r.tier == VerificationTier.UNIT for r in results)


# ---------------------------------------------------------------------------
# VerificationMatrix.run
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for VerificationMatrix.run."""

    def test_run_all_pass(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(_make_spec(test_id="t1", test_function="_passing_fn"))
        matrix.register_test(
            _make_spec(
                test_id="t2",
                category=VerificationCategory.MANIFEST_HASHING,
                test_function="_passing_fn",
            ),
        )
        results = matrix.run()
        assert len(results) == 2
        assert all(r.passed for r in results)

    def test_run_some_fail(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(_make_spec(test_id="t1", test_function="_passing_fn"))
        matrix.register_test(_make_spec(test_id="t2", test_function="_failing_fn"))
        results = matrix.run()
        assert len(results) == 2
        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]
        assert len(passed) == 1
        assert len(failed) == 1
        assert failed[0].error is not None

    def test_run_fail_fast_stops_at_first_failure(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=list(VerificationTier),
            categories=list(VerificationCategory),
            fail_fast=True,
        )
        matrix = VerificationMatrix(cfg)
        matrix.register_test(_make_spec(test_id="t1", test_function="_failing_fn"))
        matrix.register_test(_make_spec(test_id="t2", test_function="_passing_fn"))
        results = matrix.run()
        # fail_fast stops at the first failure -> only 1 result.
        assert len(results) == 1
        assert not results[0].passed

    def test_run_empty_matrix(self) -> None:
        matrix = VerificationMatrix(_full_config())
        results = matrix.run()
        assert results == []

    def test_run_respects_enabled_flag(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(
            _make_spec(test_id="t1", test_function="_passing_fn", enabled=False),
        )
        matrix.register_test(_make_spec(test_id="t2", test_function="_passing_fn"))
        results = matrix.run()
        assert len(results) == 1
        assert results[0].test_id == "t2"

    def test_run_respects_tier_filter(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=[VerificationTier.INTEGRATION],
            categories=list(VerificationCategory),
        )
        matrix = VerificationMatrix(cfg)
        matrix.register_test(
            _make_spec(
                test_id="t1",
                tier=VerificationTier.UNIT,
                test_function="_passing_fn",
            ),
        )
        matrix.register_test(
            _make_spec(
                test_id="t2",
                tier=VerificationTier.INTEGRATION,
                test_function="_passing_fn",
            ),
        )
        results = matrix.run()
        assert len(results) == 1
        assert results[0].tier == VerificationTier.INTEGRATION

    def test_run_respects_category_filter(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=list(VerificationTier),
            categories=[VerificationCategory.PIT_SAFETY],
        )
        matrix = VerificationMatrix(cfg)
        matrix.register_test(
            _make_spec(
                test_id="t1",
                category=VerificationCategory.SCHEMA_VALIDATION,
                test_function="_passing_fn",
            ),
        )
        matrix.register_test(
            _make_spec(
                test_id="t2",
                category=VerificationCategory.PIT_SAFETY,
                test_function="_passing_fn",
            ),
        )
        results = matrix.run()
        assert len(results) == 1
        assert results[0].category == VerificationCategory.PIT_SAFETY

    def test_run_min_passes_multiple(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(
            _make_spec(test_id="t1", test_function="_passing_fn", min_passes=3),
        )
        results = matrix.run()
        assert results[0].n_passes == 3
        assert results[0].n_failures == 0
        assert results[0].passed

    def test_run_records_duration(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(_make_spec(test_id="t1", test_function="_passing_fn"))
        results = matrix.run()
        assert results[0].duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# run_tier / run_category
# ---------------------------------------------------------------------------


class TestRunTierCategory:
    """Tests for run_tier and run_category."""

    def test_run_tier_filters(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(
            _make_spec(
                test_id="t1",
                tier=VerificationTier.UNIT,
                test_function="_passing_fn",
            ),
        )
        matrix.register_test(
            _make_spec(
                test_id="t2",
                tier=VerificationTier.INTEGRATION,
                test_function="_passing_fn",
            ),
        )
        results = matrix.run_tier(VerificationTier.UNIT)
        assert len(results) == 1
        assert results[0].tier == VerificationTier.UNIT

    def test_run_category_filters(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(
            _make_spec(
                test_id="t1",
                category=VerificationCategory.SCHEMA_VALIDATION,
                test_function="_passing_fn",
            ),
        )
        matrix.register_test(
            _make_spec(
                test_id="t2",
                category=VerificationCategory.PIT_SAFETY,
                test_function="_passing_fn",
            ),
        )
        results = matrix.run_category(VerificationCategory.PIT_SAFETY)
        assert len(results) == 1
        assert results[0].category == VerificationCategory.PIT_SAFETY

    def test_run_tier_empty(self) -> None:
        matrix = VerificationMatrix(_full_config())
        results = matrix.run_tier(VerificationTier.HOSTED)
        assert results == []

    def test_run_category_empty(self) -> None:
        matrix = VerificationMatrix(_full_config())
        results = matrix.run_category(VerificationCategory.COST_TRACKING)
        assert results == []

    def test_run_tier_fail_fast(self) -> None:
        cfg = VerificationMatrixConfig(
            tiers=list(VerificationTier),
            categories=list(VerificationCategory),
            fail_fast=True,
        )
        matrix = VerificationMatrix(cfg)
        matrix.register_test(
            _make_spec(
                test_id="t1",
                tier=VerificationTier.UNIT,
                test_function="_failing_fn",
            ),
        )
        matrix.register_test(
            _make_spec(
                test_id="t2",
                tier=VerificationTier.UNIT,
                test_function="_passing_fn",
            ),
        )
        results = matrix.run_tier(VerificationTier.UNIT)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# generate_report / save_report
# ---------------------------------------------------------------------------


class TestReport:
    """Tests for generate_report and save_report."""

    def test_generate_report_markdown(self) -> None:
        matrix = VerificationMatrix(_full_config())
        results = [
            _make_result(test_id="t1", passed=True),
            _make_result(
                test_id="t2",
                category=VerificationCategory.PIT_SAFETY,
                passed=False,
                n_failures=1,
                error="boom",
            ),
        ]
        report = matrix.generate_report(results)
        assert "# Verification Matrix Report" in report
        assert "## By Tier" in report
        assert "## By Category" in report
        assert "## Results" in report

    def test_generate_report_counts(self) -> None:
        matrix = VerificationMatrix(_full_config())
        results = [
            _make_result(test_id="t1", passed=True),
            _make_result(test_id="t2", passed=True),
            _make_result(test_id="t3", passed=False, n_failures=1),
        ]
        report = matrix.generate_report(results)
        assert "**Total:** 3" in report
        assert "**Passed:** 2" in report
        assert "**Failed:** 1" in report

    def test_generate_report_empty(self) -> None:
        matrix = VerificationMatrix(_full_config())
        report = matrix.generate_report([])
        assert "**Total:** 0" in report
        assert "**Passed:** 0" in report
        assert "**Failed:** 0" in report

    def test_save_report_writes_file(self, tmp_path: Path) -> None:
        matrix = VerificationMatrix(_full_config())
        results = [_make_result(test_id="t1", passed=True)]
        path = tmp_path / "subdir" / "report.md"
        matrix.save_report(results, str(path))
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# Verification Matrix Report" in content

    def test_save_report_creates_parent_dirs(self, tmp_path: Path) -> None:
        matrix = VerificationMatrix(_full_config())
        results = [_make_result(test_id="t1", passed=True)]
        path = tmp_path / "a" / "b" / "c" / "report.md"
        matrix.save_report(results, str(path))
        assert path.exists()


# ---------------------------------------------------------------------------
# is_passing
# ---------------------------------------------------------------------------


class TestIsPassing:
    """Tests for VerificationMatrix.is_passing."""

    def test_all_pass(self) -> None:
        matrix = VerificationMatrix(_full_config())
        results = [_make_result(test_id="t1"), _make_result(test_id="t2")]
        assert matrix.is_passing(results) is True

    def test_some_fail(self) -> None:
        matrix = VerificationMatrix(_full_config())
        results = [
            _make_result(test_id="t1"),
            _make_result(test_id="t2", passed=False),
        ]
        assert matrix.is_passing(results) is False

    def test_empty_is_passing(self) -> None:
        matrix = VerificationMatrix(_full_config())
        assert matrix.is_passing([]) is True


# ---------------------------------------------------------------------------
# summarize_results
# ---------------------------------------------------------------------------


class TestSummarizeResults:
    """Tests for summarize_results."""

    def test_total_passed_failed(self) -> None:
        results = [
            _make_result(test_id="t1", passed=True),
            _make_result(test_id="t2", passed=True),
            _make_result(test_id="t3", passed=False, n_failures=1),
        ]
        s = summarize_results(results)
        assert s["total"] == 3
        assert s["passed"] == 2
        assert s["failed"] == 1

    def test_by_tier(self) -> None:
        results = [
            _make_result(test_id="t1", tier=VerificationTier.UNIT, passed=True),
            _make_result(
                test_id="t2", tier=VerificationTier.UNIT, passed=False, n_failures=1,
            ),
            _make_result(
                test_id="t3", tier=VerificationTier.INTEGRATION, passed=True,
            ),
        ]
        s = summarize_results(results)
        assert s["by_tier"]["unit"] == {"total": 2, "passed": 1, "failed": 1}
        assert s["by_tier"]["integration"] == {"total": 1, "passed": 1, "failed": 0}

    def test_by_category(self) -> None:
        results = [
            _make_result(
                test_id="t1",
                category=VerificationCategory.SCHEMA_VALIDATION,
                passed=True,
            ),
            _make_result(
                test_id="t2",
                category=VerificationCategory.PIT_SAFETY,
                passed=False,
                n_failures=1,
            ),
        ]
        s = summarize_results(results)
        assert s["by_category"]["schema_validation"] == {
            "total": 1, "passed": 1, "failed": 0,
        }
        assert s["by_category"]["pit_safety"] == {
            "total": 1, "passed": 0, "failed": 1,
        }

    def test_empty(self) -> None:
        s = summarize_results([])
        assert s["total"] == 0
        assert s["passed"] == 0
        assert s["failed"] == 0
        assert s["by_tier"] == {}
        assert s["by_category"] == {}


# ---------------------------------------------------------------------------
# format_result_table
# ---------------------------------------------------------------------------


class TestFormatResultTable:
    """Tests for format_result_table."""

    def test_table_has_header(self) -> None:
        table = format_result_table([_make_result(test_id="t1")])
        assert "| Test ID |" in table
        assert "| Tier |" in table
        assert "| Category |" in table

    def test_table_contains_rows(self) -> None:
        results = [
            _make_result(test_id="t1", passed=True),
            _make_result(test_id="t2", passed=False, error="boom"),
        ]
        table = format_result_table(results)
        assert "t1" in table
        assert "t2" in table
        assert "PASS" in table
        assert "FAIL" in table

    def test_table_empty(self) -> None:
        table = format_result_table([])
        # Header row + separator row only.
        lines = table.strip().split("\n")
        assert len(lines) == 2

    def test_table_truncates_long_error(self) -> None:
        long_error = "x" * 200
        results = [_make_result(test_id="t1", passed=False, error=long_error)]
        table = format_result_table(results)
        # The error column should be truncated to <= 80 chars in the cell.
        assert "xxx" in table  # truncated content present
        assert long_error not in table


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge-case tests for the verification matrix."""

    def test_single_test_all_tiers_config(self) -> None:
        """A single test runs correctly when all tiers are configured."""
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(_make_spec(test_id="t1", test_function="_passing_fn"))
        results = matrix.run()
        assert len(results) == 1
        assert results[0].passed

    def test_disabled_test_skipped(self) -> None:
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(
            _make_spec(test_id="t1", test_function="_passing_fn", enabled=False),
        )
        results = matrix.run()
        assert results == []

    def test_import_error_recorded_as_failure(self) -> None:
        """A non-existent module is recorded as a failure, not a crash."""
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(
            _make_spec(
                test_id="t1",
                module="quant_foundry.does_not_exist_xyz",
                test_function="no_fn",
            ),
        )
        results = matrix.run()
        assert len(results) == 1
        assert not results[0].passed
        assert results[0].error is not None

    def test_missing_function_recorded_as_failure(self) -> None:
        """A non-existent function is recorded as a failure."""
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(
            _make_spec(
                test_id="t1",
                module="quant_foundry.verification_matrix",
                test_function="no_such_function_xyz",
            ),
        )
        results = matrix.run()
        assert len(results) == 1
        assert not results[0].passed

    def test_default_tests_with_unit_only_config(self) -> None:
        """Default tests run when only UNIT tier is configured."""
        cfg = VerificationMatrixConfig(
            tiers=[VerificationTier.UNIT],
            categories=list(VerificationCategory),
        )
        matrix = VerificationMatrix(cfg)
        matrix.register_default_tests()
        results = matrix.run()
        assert len(results) > 0
        assert all(r.tier == VerificationTier.UNIT for r in results)

    def test_run_does_not_mutate_specs(self) -> None:
        """Running the matrix does not mutate the registered specs."""
        matrix = VerificationMatrix(_full_config())
        matrix.register_test(_make_spec(test_id="t1", test_function="_passing_fn"))
        matrix.run()
        matrix.run()
        results = matrix.run()
        assert len(results) == 1
