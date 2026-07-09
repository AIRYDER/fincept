"""Tests for quant_foundry.cost_tracker — observability and cost tracking.

Tests the ``CostTracker`` against an in-memory SQLite database (no Postgres
required). The tracker uses ``INSERT ... ON CONFLICT DO NOTHING`` for
idempotency on training_jobs, which works on both SQLite and Postgres (the
tracker code picks the right dialect-specific insert at runtime).

Test coverage:
  - Job dispatch recording (training_jobs row created with status='dispatched')
  - Job status updates (running -> completed, timestamps set)
  - Callback linking (callback_receipt_id set on the job row)
  - Cost event recording (amount * unit_cost = total_cost)
  - Metric recording (job_metrics row created)
  - Job cost computation (sum of all cost events)
  - Period cost rollup (upsert into cost_summary)
  - GPU cost estimation (each GPU type + default)
  - CHECK constraint enforcement (bad status, bad event_type, bad metric_type,
    negative cost)
  - Idempotency (ON CONFLICT DO NOTHING for training_jobs)
  - No secrets in DB (request_payload_ref is a file path, not a payload)
  - List/filter queries (by status, by model_family)
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from quant_foundry.cost_tracker import (
    DEFAULT_GPU_RATE,
    CostTracker,
    estimate_gpu_cost,
)
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fincept_db.callback_tables import CallbackReceiptRow
from fincept_db.models import Base
from fincept_db.observability import (
    CostSummaryRow,
    JobCostEventRow,
    JobMetricRow,
    TrainingJobRow,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """In-memory SQLite engine with the observability + callback_receipts tables.

    We create the callback_receipts table (FK parent for training_jobs) plus
    the 4 observability tables. The observability tables use generic JSON
    type (not JSONB) so SQLite can render them. The FK from
    training_jobs.callback_receipt_id -> callback_receipts.callback_id is
    enforced by SQLite when foreign_keys=ON (pragma).
    """
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
    )
    # Enable FK enforcement for SQLite.
    from sqlalchemy import event

    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    tables = [
        CallbackReceiptRow.__table__,  # FK parent
        TrainingJobRow.__table__,
        JobCostEventRow.__table__,
        JobMetricRow.__table__,
        CostSummaryRow.__table__,
    ]
    Base.metadata.create_all(eng, tables=tables)
    yield eng
    eng.dispose()


@pytest.fixture()
def tracker(engine):
    return CostTracker(engine=engine)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_receipt(engine, callback_id: str = "cb-001", job_id: str = "job-001") -> None:
    """Insert a minimal callback_receipts row (FK parent for training_jobs)."""
    with Session(engine) as session:
        row = CallbackReceiptRow(
            callback_id=callback_id,
            job_id=job_id,
            idempotency_key=f"idem-{callback_id}",
            signature_valid=True,
            payload_hash="a" * 64,
            payload_ref="/tmp/payloads/cb-001.bin",
            received_at_ns=1_700_000_000_000_000_000,
            status="processed",
        )
        session.add(row)
        session.commit()


# ---------------------------------------------------------------------------
# Job dispatch recording tests
# ---------------------------------------------------------------------------


class TestRecordJobDispatch:
    def test_dispatch_creates_row_with_status_dispatched(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-001",
            model_family="gbm",
            mode="canary",
            execution_timeout_ms=1_860_000,
            gpu_type="RTX_4090",
            gpu_count=1,
            container_image="ghcr.io/fincept/worker:latest",
            request_payload_ref="/data/requests/job-001.json",
        )

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-001")
            ).one()
            assert row.model_family == "gbm"
            assert row.mode == "canary"
            assert row.status == "dispatched"
            assert row.execution_timeout_ms == 1_860_000
            assert row.gpu_type == "RTX_4090"
            assert row.gpu_count == 1
            assert row.container_image == "ghcr.io/fincept/worker:latest"
            assert row.request_payload_ref == "/data/requests/job-001.json"
            assert row.callback_receipt_id is None
            assert row.started_at_ns is None
            assert row.completed_at_ns is None

    def test_dispatch_uses_time_ns_by_default(self, tracker, engine) -> None:
        import time

        before = time.time_ns()
        tracker.record_job_dispatch(
            job_id="job-ts",
            model_family="gbm",
            mode="research",
        )
        after = time.time_ns()

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-ts")
            ).one()
            assert before <= row.dispatched_at_ns <= after

    def test_dispatch_accepts_explicit_timestamp(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-explicit",
            model_family="gbm",
            mode="production",
            dispatched_at_ns=1_700_000_000_000_000_000,
        )

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-explicit")
            ).one()
            assert row.dispatched_at_ns == 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_replayed_dispatch_is_noop(self, tracker, engine) -> None:
        """Same job_id dispatched twice -> one row, no overwrite."""
        tracker.record_job_dispatch(
            job_id="job-idem",
            model_family="gbm",
            mode="canary",
            dispatched_at_ns=1_700_000_000_000_000_000,
            gpu_type="RTX_4090",
        )
        # Replay with different args — should be ignored (DO NOTHING).
        tracker.record_job_dispatch(
            job_id="job-idem",
            model_family="xgb",
            mode="production",
            dispatched_at_ns=1_800_000_000_000_000_000,
            gpu_type="A100_80GB",
        )

        with Session(engine) as session:
            count = session.query(TrainingJobRow).count()
            assert count == 1, "replayed dispatch should not create a second row"

            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-idem")
            ).one()
            # Original values preserved (DO NOTHING did not overwrite).
            assert row.model_family == "gbm"
            assert row.mode == "canary"
            assert row.gpu_type == "RTX_4090"
            assert row.dispatched_at_ns == 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# Job status update tests
# ---------------------------------------------------------------------------


class TestUpdateJobStatus:
    def test_update_status_to_running(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-status",
            model_family="gbm",
            mode="canary",
        )
        tracker.update_job_status(
            "job-status",
            status="running",
            started_at_ns=1_700_000_001_000_000_000,
        )

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-status")
            ).one()
            assert row.status == "running"
            assert row.started_at_ns == 1_700_000_001_000_000_000
            assert row.completed_at_ns is None

    def test_update_status_to_completed(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-done",
            model_family="gbm",
            mode="canary",
        )
        tracker.update_job_status("job-done", status="running", started_at_ns=100)
        tracker.update_job_status("job-done", status="completed", completed_at_ns=200)

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-done")
            ).one()
            assert row.status == "completed"
            assert row.started_at_ns == 100  # preserved
            assert row.completed_at_ns == 200

    def test_update_status_only_changes_status(self, tracker, engine) -> None:
        """Status-only update does not clobber existing timestamps."""
        tracker.record_job_dispatch(
            job_id="job-preserve",
            model_family="gbm",
            mode="canary",
        )
        tracker.update_job_status("job-preserve", status="running", started_at_ns=111)
        # Status-only update — should NOT clear started_at_ns.
        tracker.update_job_status("job-preserve", status="completed")

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-preserve")
            ).one()
            assert row.status == "completed"
            assert row.started_at_ns == 111  # preserved

    def test_update_status_unknown_job_raises(self, tracker) -> None:
        with pytest.raises(KeyError, match="not found"):
            tracker.update_job_status("nonexistent", status="running")


# ---------------------------------------------------------------------------
# Callback linking tests
# ---------------------------------------------------------------------------


class TestLinkCallback:
    def test_link_callback_sets_receipt_id(self, tracker, engine) -> None:
        _insert_receipt(engine, callback_id="cb-link-1", job_id="job-link")
        tracker.record_job_dispatch(
            job_id="job-link",
            model_family="gbm",
            mode="canary",
        )
        tracker.link_callback("job-link", callback_receipt_id="cb-link-1")

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-link")
            ).one()
            assert row.callback_receipt_id == "cb-link-1"

    def test_link_callback_unknown_job_raises(self, tracker) -> None:
        with pytest.raises(KeyError, match="not found"):
            tracker.link_callback("nonexistent", callback_receipt_id="cb-x")


# ---------------------------------------------------------------------------
# Cost event recording tests
# ---------------------------------------------------------------------------


class TestRecordCostEvent:
    def test_cost_event_computes_total(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-cost",
            model_family="gbm",
            mode="canary",
        )
        eid = tracker.record_cost_event(
            "job-cost",
            event_type="gpu_seconds",
            amount=3600,
            unit_cost=0.40,
        )

        with Session(engine) as session:
            row = session.scalars(
                select(JobCostEventRow).where(JobCostEventRow.event_id == eid)
            ).one()
            assert row.event_type == "gpu_seconds"
            assert row.amount == Decimal("3600")
            assert row.unit_cost == Decimal("0.40")
            assert row.total_cost == Decimal("1440.000000")
            assert row.currency == "USD"

    def test_cost_event_with_metadata(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-meta",
            model_family="gbm",
            mode="canary",
        )
        eid = tracker.record_cost_event(
            "job-meta",
            event_type="storage_gb_hours",
            amount=10,
            unit_cost=0.05,
            metadata={"region": "us-east-1", "volume_id": "vol-123"},
        )

        with Session(engine) as session:
            row = session.scalars(
                select(JobCostEventRow).where(JobCostEventRow.event_id == eid)
            ).one()
            assert row.extra_metadata == {"region": "us-east-1", "volume_id": "vol-123"}

    def test_cost_event_returns_event_id(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-eid",
            model_family="gbm",
            mode="canary",
        )
        eid = tracker.record_cost_event("job-eid", event_type="overhead", amount=1, unit_cost=0.01)
        assert eid is not None and len(eid) > 0

    def test_cost_event_decimal_arithmetic(self, tracker, engine) -> None:
        """amount * unit_cost with fractional values is exact."""
        tracker.record_job_dispatch(
            job_id="job-dec",
            model_family="gbm",
            mode="canary",
        )
        eid = tracker.record_cost_event(
            "job-dec", event_type="network_egress_gb", amount=2.5, unit_cost=0.09
        )

        with Session(engine) as session:
            row = session.scalars(
                select(JobCostEventRow).where(JobCostEventRow.event_id == eid)
            ).one()
            assert row.total_cost == Decimal("0.225000")


# ---------------------------------------------------------------------------
# Metric recording tests
# ---------------------------------------------------------------------------


class TestRecordMetric:
    def test_metric_creates_row(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-metric",
            model_family="gbm",
            mode="canary",
        )
        mid = tracker.record_metric(
            "job-metric",
            metric_type="duration",
            value=1800.5,
            unit="seconds",
        )

        with Session(engine) as session:
            row = session.scalars(select(JobMetricRow).where(JobMetricRow.metric_id == mid)).one()
            assert row.metric_type == "duration"
            assert row.value == Decimal("1800.5")
            assert row.unit == "seconds"

    def test_metric_returns_metric_id(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-mid",
            model_family="gbm",
            mode="canary",
        )
        mid = tracker.record_metric(
            "job-mid", metric_type="gpu_utilization", value=85.2, unit="percent"
        )
        assert mid is not None and len(mid) > 0


# ---------------------------------------------------------------------------
# Job cost computation tests
# ---------------------------------------------------------------------------


class TestComputeJobCost:
    def test_sum_of_events(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-sum",
            model_family="gbm",
            mode="canary",
        )
        tracker.record_cost_event("job-sum", "gpu_seconds", 3600, 0.40)
        tracker.record_cost_event("job-sum", "storage_gb_hours", 10, 0.05)
        tracker.record_cost_event("job-sum", "overhead", 1, 0.50)

        total = tracker.compute_job_cost("job-sum")
        # 3600*0.40 + 10*0.05 + 1*0.50 = 1440 + 0.5 + 0.5 = 1441
        assert total == Decimal("1441.000000")

    def test_no_events_returns_zero(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-empty",
            model_family="gbm",
            mode="canary",
        )
        total = tracker.compute_job_cost("job-empty")
        assert total == Decimal("0")


# ---------------------------------------------------------------------------
# Period cost rollup tests
# ---------------------------------------------------------------------------


class TestComputePeriodCost:
    def test_period_rollup_upserts_summary(self, tracker, engine) -> None:
        # Two jobs in the same period for the same model_family.
        tracker.record_job_dispatch(
            job_id="job-p1",
            model_family="gbm",
            mode="canary",
            dispatched_at_ns=1_700_000_000_000_000_000,
        )
        tracker.record_job_dispatch(
            job_id="job-p2",
            model_family="gbm",
            mode="research",
            dispatched_at_ns=1_700_000_100_000_000_000,
        )
        # One job outside the period.
        tracker.record_job_dispatch(
            job_id="job-p3",
            model_family="gbm",
            mode="canary",
            dispatched_at_ns=1_800_000_000_000_000_000,
        )

        tracker.record_cost_event("job-p1", "gpu_seconds", 3600, 0.40)  # 1440
        tracker.record_cost_event("job-p2", "gpu_seconds", 1800, 0.40)  # 720

        period_start = 1_700_000_000_000_000_000
        period_end = 1_700_001_000_000_000_000
        total = tracker.compute_period_cost("gbm", period_start, period_end)

        assert total == Decimal("2160.000000")  # 1440 + 720

        with Session(engine) as session:
            row = session.scalars(
                select(CostSummaryRow)
                .where(CostSummaryRow.model_family == "gbm")
                .where(CostSummaryRow.period_start_ns == period_start)
            ).one()
            assert row.total_cost == Decimal("2160.000000")
            assert row.total_jobs == 2
            assert row.total_gpu_seconds == Decimal("2160.000000")
            assert row.currency == "USD"

    def test_period_rollup_upsert_on_recompute(self, tracker, engine) -> None:
        """Calling compute_period_cost twice updates the existing summary row."""
        tracker.record_job_dispatch(
            job_id="job-rc1",
            model_family="xgb",
            mode="canary",
            dispatched_at_ns=1_700_000_000_000_000_000,
        )
        tracker.record_cost_event("job-rc1", "gpu_seconds", 3600, 0.40)  # 1440

        period_start = 1_700_000_000_000_000_000
        period_end = 1_700_001_000_000_000_000
        tracker.compute_period_cost("xgb", period_start, period_end)

        # Add a second job and recompute.
        tracker.record_job_dispatch(
            job_id="job-rc2",
            model_family="xgb",
            mode="research",
            dispatched_at_ns=1_700_000_500_000_000_000,
        )
        tracker.record_cost_event("job-rc2", "gpu_seconds", 1800, 0.40)  # 720
        tracker.compute_period_cost("xgb", period_start, period_end)

        with Session(engine) as session:
            count = session.query(CostSummaryRow).count()
            assert count == 1, "recompute should upsert, not insert a second row"

            row = session.scalars(
                select(CostSummaryRow).where(CostSummaryRow.model_family == "xgb")
            ).one()
            assert row.total_cost == Decimal("2160.000000")
            assert row.total_jobs == 2

    def test_period_excludes_other_model_families(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-mf1",
            model_family="gbm",
            mode="canary",
            dispatched_at_ns=1_700_000_000_000_000_000,
        )
        tracker.record_job_dispatch(
            job_id="job-mf2",
            model_family="xgb",
            mode="canary",
            dispatched_at_ns=1_700_000_000_000_000_000,
        )
        tracker.record_cost_event("job-mf1", "gpu_seconds", 3600, 0.40)  # 1440
        tracker.record_cost_event("job-mf2", "gpu_seconds", 3600, 0.40)  # 1440

        period_start = 1_700_000_000_000_000_000
        period_end = 1_700_001_000_000_000_000
        total = tracker.compute_period_cost("gbm", period_start, period_end)
        assert total == Decimal("1440.000000")  # only gbm job


# ---------------------------------------------------------------------------
# GPU cost estimation tests
# ---------------------------------------------------------------------------


class TestEstimateGpuCost:
    def test_rtx_4090_one_gpu_one_hour(self) -> None:
        cost = estimate_gpu_cost("RTX_4090", 1, 3600)
        assert cost == Decimal("0.400000")

    def test_a100_80gb_two_gpus_one_hour(self) -> None:
        cost = estimate_gpu_cost("A100_80GB", 2, 3600)
        assert cost == Decimal("2.200000")  # 1.10 * 2 * 1

    def test_a100_40gb_one_gpu_half_hour(self) -> None:
        cost = estimate_gpu_cost("A100_40GB", 1, 1800)
        assert cost == Decimal("0.400000")  # 0.80 * 1 * 0.5

    def test_l4_one_gpu_one_hour(self) -> None:
        cost = estimate_gpu_cost("L4", 1, 3600)
        assert cost == Decimal("0.250000")

    def test_unknown_gpu_uses_default(self) -> None:
        cost = estimate_gpu_cost("UNKNOWN_GPU", 1, 3600)
        assert cost == Decimal("0.500000")  # DEFAULT_GPU_RATE

    def test_none_gpu_uses_default(self) -> None:
        cost = estimate_gpu_cost(None, 1, 3600)
        assert cost == Decimal("0.500000")

    def test_tracker_uses_injected_rates(self, engine) -> None:
        custom_rates = {"RTX_4090": Decimal("0.99")}
        tracker = CostTracker(engine=engine, gpu_rates=custom_rates)
        cost = tracker.estimate_gpu_cost("RTX_4090", 1, 3600)
        assert cost == Decimal("0.990000")

    def test_tracker_unknown_gpu_uses_default(self, tracker) -> None:
        cost = tracker.estimate_gpu_cost("MYSTERY", 1, 3600)
        assert cost == DEFAULT_GPU_RATE


# ---------------------------------------------------------------------------
# CHECK constraint enforcement tests
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    def test_bad_status_rejected(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-bad-status",
            model_family="gbm",
            mode="canary",
        )
        with pytest.raises(IntegrityError):
            tracker.update_job_status("job-bad-status", status="bogus")

    def test_bad_mode_rejected_on_dispatch(self, engine) -> None:
        """Direct insert with bad mode -> CHECK constraint fires."""
        with Session(engine) as session:
            row = TrainingJobRow(
                job_id="job-bad-mode",
                model_family="gbm",
                mode="bogus",
                status="dispatched",
                dispatched_at_ns=1_700_000_000_000_000_000,
            )
            session.add(row)
            with pytest.raises(IntegrityError):
                session.commit()

    def test_bad_event_type_rejected(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-bad-et",
            model_family="gbm",
            mode="canary",
        )
        with pytest.raises(IntegrityError):
            tracker.record_cost_event("job-bad-et", "bogus_event", 1, 0.01)

    def test_bad_metric_type_rejected(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-bad-mt",
            model_family="gbm",
            mode="canary",
        )
        with pytest.raises(IntegrityError):
            tracker.record_metric("job-bad-mt", "bogus_metric", 1.0, "x")

    def test_negative_amount_rejected(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-neg-amt",
            model_family="gbm",
            mode="canary",
        )
        with pytest.raises(IntegrityError):
            tracker.record_cost_event("job-neg-amt", "gpu_seconds", -1, 0.40)

    def test_negative_unit_cost_rejected(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-neg-uc",
            model_family="gbm",
            mode="canary",
        )
        with pytest.raises(IntegrityError):
            tracker.record_cost_event("job-neg-uc", "gpu_seconds", 1, -0.40)


# ---------------------------------------------------------------------------
# No secrets in DB tests
# ---------------------------------------------------------------------------


class TestNoSecretsInDb:
    def test_request_payload_ref_is_file_path_not_payload(self, tracker, engine) -> None:
        """request_payload_ref stores a file path, never the raw payload."""
        tracker.record_job_dispatch(
            job_id="job-nosec",
            model_family="gbm",
            mode="canary",
            request_payload_ref="/data/requests/job-nosec.json",
        )

        with Session(engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == "job-nosec")
            ).one()
            # The column is a file path, not a JSON payload.
            assert row.request_payload_ref == "/data/requests/job-nosec.json"
            assert not row.request_payload_ref.startswith("{")
            assert "secret" not in str(row.request_payload_ref).lower()
            assert "password" not in str(row.request_payload_ref).lower()

    def test_no_secret_columns_in_training_jobs(self, engine) -> None:
        """The training_jobs table has no column named secret/key/token/sig."""
        inspector = inspect(engine)
        cols = [c["name"] for c in inspector.get_columns("training_jobs")]
        forbidden = {"secret", "api_key", "token", "signature", "password", "hmac"}
        for col in cols:
            assert col.lower() not in forbidden, f"training_jobs has forbidden column: {col}"


# ---------------------------------------------------------------------------
# Read API tests
# ---------------------------------------------------------------------------


class TestReadApi:
    def test_get_job_returns_dict(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-get",
            model_family="gbm",
            mode="canary",
            gpu_type="RTX_4090",
        )
        job = tracker.get_job("job-get")
        assert job is not None
        assert job["job_id"] == "job-get"
        assert job["model_family"] == "gbm"
        assert job["status"] == "dispatched"
        assert job["gpu_type"] == "RTX_4090"

    def test_get_job_returns_none_for_unknown(self, tracker) -> None:
        assert tracker.get_job("nonexistent") is None

    def test_list_jobs_all(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-l1",
            model_family="gbm",
            mode="canary",
            dispatched_at_ns=100,
        )
        tracker.record_job_dispatch(
            job_id="job-l2",
            model_family="xgb",
            mode="research",
            dispatched_at_ns=200,
        )
        jobs = tracker.list_jobs()
        assert len(jobs) == 2
        # Ordered by dispatched_at_ns desc.
        assert jobs[0]["job_id"] == "job-l2"
        assert jobs[1]["job_id"] == "job-l1"

    def test_list_jobs_filter_by_status(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-f1",
            model_family="gbm",
            mode="canary",
        )
        tracker.record_job_dispatch(
            job_id="job-f2",
            model_family="gbm",
            mode="canary",
        )
        tracker.update_job_status("job-f2", status="completed")
        jobs = tracker.list_jobs(status="completed")
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-f2"

    def test_list_jobs_filter_by_model_family(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-mf-a",
            model_family="gbm",
            mode="canary",
        )
        tracker.record_job_dispatch(
            job_id="job-mf-b",
            model_family="xgb",
            mode="canary",
        )
        jobs = tracker.list_jobs(model_family="xgb")
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-mf-b"

    def test_get_job_metrics(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-gm",
            model_family="gbm",
            mode="canary",
        )
        tracker.record_metric("job-gm", "duration", 1800, "seconds", recorded_at_ns=100)
        tracker.record_metric("job-gm", "gpu_utilization", 85.0, "percent", recorded_at_ns=200)
        metrics = tracker.get_job_metrics("job-gm")
        assert len(metrics) == 2
        assert metrics[0]["metric_type"] == "duration"
        assert metrics[1]["metric_type"] == "gpu_utilization"

    def test_get_job_cost_events(self, tracker, engine) -> None:
        tracker.record_job_dispatch(
            job_id="job-gce",
            model_family="gbm",
            mode="canary",
        )
        tracker.record_cost_event("job-gce", "gpu_seconds", 3600, 0.40, recorded_at_ns=100)
        tracker.record_cost_event("job-gce", "overhead", 1, 0.50, recorded_at_ns=200)
        events = tracker.get_job_cost_events("job-gce")
        assert len(events) == 2
        assert events[0]["event_type"] == "gpu_seconds"
        assert events[1]["event_type"] == "overhead"
        assert events[0]["total_cost"] == Decimal("1440.000000")
