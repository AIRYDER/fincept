"""TDD tests for quant_foundry.ui.job_ledger_view (T-UI.1).

Acceptance:
- Operator can distinguish queued, running, failed, rejected, verified,
  promotion_ineligible, and completed states.
- UI does not imply health that receipts do not prove (fail-closed).
- JobLedgerViewConfig and JobLedgerRow are Pydantic v2 frozen + extra=forbid.
- render / render_summary / render_job_detail / filter_by_status / sort_rows
  behave per spec.
- format_status / format_cost / format_bool helpers.
- Edge cases: empty list, single job, all same status.
"""

from __future__ import annotations

import pytest
from quant_foundry.ui.job_ledger_view import (
    KNOWN_STATUSES,
    JobLedgerRow,
    JobLedgerView,
    JobLedgerViewConfig,
    format_bool,
    format_cost,
    format_status,
    validate_no_false_health,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    *,
    job_id: str = "job-1",
    dataset_id: str = "ds-1",
    model_family: str = "xgboost",
    runpod_job_id: str | None = "rp-1",
    status: str = "queued",
    gpu_type: str | None = "RTX4090",
    cost_estimate: float | None = 1.2345,
    artifact_verified: bool | None = None,
    promotion_eligible: bool | None = None,
    failure_reason: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> JobLedgerRow:
    """Build a :class:`JobLedgerRow` with sensible defaults for tests."""
    return JobLedgerRow(
        job_id=job_id,
        dataset_id=dataset_id,
        model_family=model_family,
        runpod_job_id=runpod_job_id,
        status=status,
        gpu_type=gpu_type,
        cost_estimate=cost_estimate,
        artifact_verified=artifact_verified,
        promotion_eligible=promotion_eligible,
        failure_reason=failure_reason,
        created_at=created_at,
    )


def _rows_all_statuses() -> list[JobLedgerRow]:
    """Build one row per known status (with honest health fields)."""
    return [
        _row(job_id="q", status="queued", created_at="2026-01-01T00:00:00Z"),
        _row(job_id="r", status="running", created_at="2026-01-02T00:00:00Z"),
        _row(
            job_id="f",
            status="failed",
            failure_reason="OOM",
            created_at="2026-01-03T00:00:00Z",
        ),
        _row(
            job_id="rej",
            status="rejected",
            failure_reason="bad schema",
            created_at="2026-01-04T00:00:00Z",
        ),
        _row(
            job_id="v",
            status="verified",
            artifact_verified=True,
            promotion_eligible=True,
            created_at="2026-01-05T00:00:00Z",
        ),
        _row(
            job_id="pi",
            status="promotion_ineligible",
            artifact_verified=True,
            promotion_eligible=False,
            created_at="2026-01-06T00:00:00Z",
        ),
        _row(
            job_id="c",
            status="completed",
            artifact_verified=True,
            promotion_eligible=True,
            created_at="2026-01-07T00:00:00Z",
        ),
    ]


# ---------------------------------------------------------------------------
# Module / imports
# ---------------------------------------------------------------------------


def test_module_imports_and_exports() -> None:
    """All public symbols are importable."""
    assert callable(JobLedgerView)
    assert issubclass(JobLedgerViewConfig, object)
    assert issubclass(JobLedgerRow, object)
    assert callable(format_status)
    assert callable(format_cost)
    assert callable(format_bool)
    assert callable(validate_no_false_health)


def test_known_statuses_complete() -> None:
    """All seven operator-facing statuses are present."""
    assert {
        "queued",
        "running",
        "failed",
        "rejected",
        "verified",
        "promotion_ineligible",
        "completed",
    } == KNOWN_STATUSES


# ---------------------------------------------------------------------------
# JobLedgerViewConfig
# ---------------------------------------------------------------------------


class TestJobLedgerViewConfig:
    def test_defaults(self) -> None:
        cfg = JobLedgerViewConfig()
        assert cfg.show_cost is True
        assert cfg.show_gpu_type is True
        assert cfg.show_artifact_verification is True
        assert cfg.show_promotion_eligibility is True
        assert cfg.show_failure_reason is True
        assert cfg.max_rows == 100
        assert cfg.sort_by == "created_at"
        assert cfg.sort_order == "desc"

    def test_frozen(self) -> None:
        cfg = JobLedgerViewConfig()
        with pytest.raises(Exception):
            cfg.show_cost = False  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            JobLedgerViewConfig(bogus=True)  # type: ignore[call-arg]

    def test_max_rows_minimum_one(self) -> None:
        JobLedgerViewConfig(max_rows=1)
        with pytest.raises(Exception):
            JobLedgerViewConfig(max_rows=0)
        with pytest.raises(Exception):
            JobLedgerViewConfig(max_rows=-5)

    def test_sort_by_validates(self) -> None:
        JobLedgerViewConfig(sort_by="created_at")
        JobLedgerViewConfig(sort_by="status")
        JobLedgerViewConfig(sort_by="cost")
        with pytest.raises(Exception):
            JobLedgerViewConfig(sort_by="bogus")

    def test_sort_order_validates(self) -> None:
        JobLedgerViewConfig(sort_order="asc")
        JobLedgerViewConfig(sort_order="desc")
        with pytest.raises(Exception):
            JobLedgerViewConfig(sort_order="bogus")

    def test_custom_config(self) -> None:
        cfg = JobLedgerViewConfig(
            show_cost=False,
            show_gpu_type=False,
            show_artifact_verification=False,
            show_promotion_eligibility=False,
            show_failure_reason=False,
            max_rows=5,
            sort_by="cost",
            sort_order="asc",
        )
        assert cfg.show_cost is False
        assert cfg.max_rows == 5
        assert cfg.sort_by == "cost"
        assert cfg.sort_order == "asc"


# ---------------------------------------------------------------------------
# JobLedgerRow
# ---------------------------------------------------------------------------


class TestJobLedgerRow:
    def test_construction_defaults(self) -> None:
        row = JobLedgerRow(
            job_id="j1",
            dataset_id="d1",
            model_family="xgboost",
            status="queued",
            created_at="2026-01-01T00:00:00Z",
        )
        assert row.runpod_job_id is None
        assert row.gpu_type is None
        assert row.cost_estimate is None
        assert row.artifact_verified is None
        assert row.promotion_eligible is None
        assert row.failure_reason is None

    def test_frozen(self) -> None:
        row = _row()
        with pytest.raises(Exception):
            row.status = "failed"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            JobLedgerRow(  # type: ignore[call-arg]
                job_id="j1",
                dataset_id="d1",
                model_family="xgboost",
                status="queued",
                created_at="2026-01-01T00:00:00Z",
                bogus=True,
            )

    def test_status_must_be_known(self) -> None:
        with pytest.raises(Exception):
            _row(status="bogus")

    def test_required_strings_non_empty(self) -> None:
        with pytest.raises(Exception):
            _row(job_id="")
        with pytest.raises(Exception):
            _row(dataset_id="")
        with pytest.raises(Exception):
            _row(model_family="")
        with pytest.raises(Exception):
            _row(created_at="")

    def test_cost_non_negative(self) -> None:
        with pytest.raises(Exception):
            _row(cost_estimate=-1.0)

    def test_all_known_statuses_accepted(self) -> None:
        for status in KNOWN_STATUSES:
            row = _row(status=status)
            assert row.status == status


# ---------------------------------------------------------------------------
# format_status
# ---------------------------------------------------------------------------


class TestFormatStatus:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("queued", "[QUEUED]"),
            ("running", "[RUNNING]"),
            ("failed", "[FAILED]"),
            ("rejected", "[REJECTED]"),
            ("verified", "[VERIFIED]"),
            ("promotion_ineligible", "[PROMO_INELIGIBLE]"),
            ("completed", "[COMPLETED]"),
        ],
    )
    def test_known_statuses(self, raw: str, expected: str) -> None:
        assert format_status(raw) == expected

    def test_unknown_status(self) -> None:
        assert format_status("bogus") == "[UNKNOWN]"

    def test_empty_string(self) -> None:
        assert format_status("") == "[UNKNOWN]"

    def test_non_string(self) -> None:
        assert format_status(123) == "[UNKNOWN]"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# format_cost
# ---------------------------------------------------------------------------


class TestFormatCost:
    def test_value(self) -> None:
        assert format_cost(1.2345) == "$1.2345"

    def test_zero(self) -> None:
        assert format_cost(0.0) == "$0.0000"

    def test_none(self) -> None:
        assert format_cost(None) == "—"

    def test_large_value(self) -> None:
        assert format_cost(1234.5678) == "$1234.5678"

    def test_rounding(self) -> None:
        # Four decimal places.
        assert format_cost(1.234567) == "$1.2346"


# ---------------------------------------------------------------------------
# format_bool
# ---------------------------------------------------------------------------


class TestFormatBool:
    def test_true(self) -> None:
        assert format_bool(True) == "✓"

    def test_false(self) -> None:
        assert format_bool(False) == "✗"

    def test_none(self) -> None:
        assert format_bool(None) == "—"


# ---------------------------------------------------------------------------
# validate_no_false_health
# ---------------------------------------------------------------------------


class TestValidateNoFalseHealth:
    def test_honest_queued(self) -> None:
        assert validate_no_false_health(_row(status="queued")) is True

    def test_honest_verified(self) -> None:
        assert validate_no_false_health(_row(status="verified", artifact_verified=True)) is True

    def test_verified_without_artifact_raises(self) -> None:
        row = _row(status="verified", artifact_verified=None)
        with pytest.raises(ValueError, match="verified"):
            validate_no_false_health(row)

    def test_verified_with_false_artifact_raises(self) -> None:
        row = _row(status="verified", artifact_verified=False)
        with pytest.raises(ValueError):
            validate_no_false_health(row)

    def test_completed_without_artifact_raises(self) -> None:
        row = _row(status="completed", artifact_verified=None)
        with pytest.raises(ValueError, match="completed"):
            validate_no_false_health(row)

    def test_promotion_eligible_without_verification_raises(self) -> None:
        row = _row(
            status="queued",
            promotion_eligible=True,
            artifact_verified=None,
        )
        with pytest.raises(ValueError, match="promotion"):
            validate_no_false_health(row)

    def test_promotion_eligible_with_verification_ok(self) -> None:
        row = _row(
            status="verified",
            promotion_eligible=True,
            artifact_verified=True,
        )
        assert validate_no_false_health(row) is True

    def test_promotion_ineligible_honest(self) -> None:
        # promotion_eligible=False does not assert health; always honest.
        row = _row(
            status="promotion_ineligible",
            artifact_verified=True,
            promotion_eligible=False,
        )
        assert validate_no_false_health(row) is True

    def test_failed_honest(self) -> None:
        assert validate_no_false_health(_row(status="failed")) is True


# ---------------------------------------------------------------------------
# JobLedgerView.render
# ---------------------------------------------------------------------------


class TestRender:
    def test_all_columns_present(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render(_rows_all_statuses())
        assert "JOB_ID" in out
        assert "STATUS" in out
        assert "DATASET" in out
        assert "MODEL" in out
        assert "GPU" in out
        assert "COST" in out
        assert "VERIFIED" in out
        assert "PROMO" in out
        assert "FAILURE_REASON" in out
        assert "CREATED_AT" in out

    def test_all_status_labels_visible(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render(_rows_all_statuses())
        assert "[QUEUED]" in out
        assert "[RUNNING]" in out
        assert "[FAILED]" in out
        assert "[REJECTED]" in out
        assert "[VERIFIED]" in out
        assert "[PROMO_INELIGIBLE]" in out
        assert "[COMPLETED]" in out

    def test_partial_columns_cost_hidden(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(show_cost=False))
        out = view.render([_row()])
        assert "COST" not in out
        assert "JOB_ID" in out

    def test_partial_columns_gpu_hidden(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(show_gpu_type=False))
        out = view.render([_row()])
        assert "GPU" not in out

    def test_partial_columns_all_optional_hidden(self) -> None:
        view = JobLedgerView(
            JobLedgerViewConfig(
                show_cost=False,
                show_gpu_type=False,
                show_artifact_verification=False,
                show_promotion_eligibility=False,
                show_failure_reason=False,
            )
        )
        out = view.render([_row()])
        assert "COST" not in out
        assert "GPU" not in out
        assert "VERIFIED" not in out
        assert "PROMO" not in out
        assert "FAILURE_REASON" not in out
        # Core columns remain.
        assert "JOB_ID" in out
        assert "STATUS" in out

    def test_empty_list(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render([])
        assert "JOB_ID" in out
        assert "(no jobs)" in out

    def test_single_job(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render([_row(job_id="solo")])
        assert "solo" in out
        assert "[QUEUED]" in out

    def test_max_rows_truncation(self) -> None:
        rows = [_row(job_id=f"j{i}", created_at=f"2026-01-{i:02d}T00:00:00Z") for i in range(1, 11)]
        view = JobLedgerView(JobLedgerViewConfig(max_rows=3, sort_order="asc"))
        out = view.render(rows)
        # Only 3 rows + header + separator.
        body_lines = [
            ln
            for ln in out.splitlines()
            if ln.startswith("|") and "---" not in ln and "JOB_ID" not in ln
        ]
        assert len(body_lines) == 3
        assert "j1" in out
        assert "j4" not in out

    def test_render_rejects_false_health(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        bad = _row(status="verified", artifact_verified=None)
        with pytest.raises(ValueError):
            view.render([bad])

    def test_cost_formatted_in_table(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render([_row(cost_estimate=9.9999)])
        assert "$9.9999" in out

    def test_none_cost_shows_dash(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render([_row(cost_estimate=None)])
        assert "—" in out


# ---------------------------------------------------------------------------
# render_summary
# ---------------------------------------------------------------------------


class TestRenderSummary:
    def test_total_jobs(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render_summary(_rows_all_statuses())
        assert "Total jobs: 7" in out

    def test_counts_by_status(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render_summary(_rows_all_statuses())
        assert "[QUEUED] queued: 1" in out
        assert "[VERIFIED] verified: 1" in out
        assert "[PROMO_INELIGIBLE] promotion_ineligible: 1" in out

    def test_total_cost(self) -> None:
        rows = [
            _row(job_id="a", cost_estimate=1.0),
            _row(job_id="b", cost_estimate=2.5),
        ]
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render_summary(rows)
        assert "$3.5000" in out

    def test_verified_and_promo_counts(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render_summary(_rows_all_statuses())
        assert "Verified artifacts: 3" in out
        assert "Promotion-eligible: 2" in out

    def test_empty_summary(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render_summary([])
        assert "Total jobs: 0" in out
        assert "Verified artifacts: 0" in out

    def test_summary_rejects_false_health(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        bad = _row(status="completed", artifact_verified=False)
        with pytest.raises(ValueError):
            view.render_summary([bad])

    def test_all_same_status(self) -> None:
        rows = [_row(job_id=f"j{i}", status="queued") for i in range(5)]
        view = JobLedgerView(JobLedgerViewConfig())
        out = view.render_summary(rows)
        assert "[QUEUED] queued: 5" in out
        assert "Total jobs: 5" in out


# ---------------------------------------------------------------------------
# render_job_detail
# ---------------------------------------------------------------------------


class TestRenderJobDetail:
    def test_contains_all_fields(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        row = _row(
            job_id="detail-1",
            status="failed",
            failure_reason="OOM",
            cost_estimate=5.5,
        )
        out = view.render_job_detail(row)
        assert "detail-1" in out
        assert "[FAILED]" in out
        assert "OOM" in out
        assert "$5.5000" in out
        assert "Job Detail" in out

    def test_none_fields_show_dash(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        row = _row(runpod_job_id=None, gpu_type=None, cost_estimate=None)
        out = view.render_job_detail(row)
        assert "—" in out

    def test_detail_rejects_false_health(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        bad = _row(status="verified", artifact_verified=False)
        with pytest.raises(ValueError):
            view.render_job_detail(bad)


# ---------------------------------------------------------------------------
# filter_by_status
# ---------------------------------------------------------------------------


class TestFilterByStatus:
    def test_filters_to_one_status(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        rows = _rows_all_statuses()
        verified = view.filter_by_status(rows, "verified")
        assert len(verified) == 1
        assert verified[0].status == "verified"

    def test_unknown_status_returns_empty(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        assert view.filter_by_status(_rows_all_statuses(), "bogus") == []

    def test_preserves_order(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        rows = [
            _row(job_id="a", status="queued"),
            _row(job_id="b", status="running"),
            _row(job_id="c", status="queued"),
        ]
        queued = view.filter_by_status(rows, "queued")
        assert [r.job_id for r in queued] == ["a", "c"]

    def test_no_matches(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        rows = [_row(status="queued")]
        assert view.filter_by_status(rows, "failed") == []


# ---------------------------------------------------------------------------
# sort_rows
# ---------------------------------------------------------------------------


class TestSortRows:
    def test_sort_by_created_at_desc(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(sort_by="created_at", sort_order="desc"))
        rows = [
            _row(job_id="old", created_at="2026-01-01T00:00:00Z"),
            _row(job_id="new", created_at="2026-01-10T00:00:00Z"),
            _row(job_id="mid", created_at="2026-01-05T00:00:00Z"),
        ]
        out = view.sort_rows(rows)
        assert [r.job_id for r in out] == ["new", "mid", "old"]

    def test_sort_by_created_at_asc(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(sort_by="created_at", sort_order="asc"))
        rows = [
            _row(job_id="old", created_at="2026-01-01T00:00:00Z"),
            _row(job_id="new", created_at="2026-01-10T00:00:00Z"),
        ]
        out = view.sort_rows(rows)
        assert [r.job_id for r in out] == ["old", "new"]

    def test_sort_by_status_asc(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(sort_by="status", sort_order="asc"))
        rows = [
            _row(job_id="z", status="verified", artifact_verified=True),
            _row(job_id="a", status="failed"),
        ]
        out = view.sort_rows(rows)
        # "failed" < "verified" alphabetically.
        assert [r.job_id for r in out] == ["a", "z"]

    def test_sort_by_cost_desc(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(sort_by="cost", sort_order="desc"))
        rows = [
            _row(job_id="cheap", cost_estimate=1.0),
            _row(job_id="pricey", cost_estimate=100.0),
            _row(job_id="none", cost_estimate=None),
        ]
        out = view.sort_rows(rows)
        assert [r.job_id for r in out] == ["pricey", "cheap", "none"]

    def test_sort_by_cost_asc(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(sort_by="cost", sort_order="asc"))
        rows = [
            _row(job_id="cheap", cost_estimate=1.0),
            _row(job_id="pricey", cost_estimate=100.0),
        ]
        out = view.sort_rows(rows)
        assert [r.job_id for r in out] == ["cheap", "pricey"]

    def test_sort_does_not_mutate_input(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig(sort_by="cost", sort_order="desc"))
        rows = [
            _row(job_id="a", cost_estimate=1.0),
            _row(job_id="b", cost_estimate=2.0),
        ]
        original = list(rows)
        view.sort_rows(rows)
        assert [r.job_id for r in rows] == [r.job_id for r in original]

    def test_sort_empty(self) -> None:
        view = JobLedgerView(JobLedgerViewConfig())
        assert view.sort_rows([]) == []
