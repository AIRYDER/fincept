"""quant_foundry.cost_tracker — observability and cost tracking for RunPod jobs.

Records the lifecycle, cost, and operational metrics of every training job
dispatched to RunPod into fincept-db (Postgres) via a **sync** SQLAlchemy
engine. This mirrors the pattern established by ``db_sinks.py``: the
``CostTracker`` uses ``sync_session_scope()`` (or an injected engine in
tests) and does NOT make any live RunPod API calls.

Tables written (migration 0004b_observability):
  - ``training_jobs``     — one row per dispatched job (status lifecycle)
  - ``job_cost_events``   — cost events (gpu_seconds, storage, egress, overhead)
  - ``job_metrics``       — operational metrics (duration, gpu_utilization, etc.)
  - ``cost_summary``      — daily/period cost rollup per model_family

Idempotency:
  ``record_job_dispatch`` uses ``INSERT ... ON CONFLICT (job_id) DO NOTHING``
  so a replayed dispatch does not create a second training_jobs row. The
  other write methods (``record_cost_event``, ``record_metric``) generate
  unique IDs per call and are append-only by design.

Security:
  No column stores the callback secret, the HMAC signature bytes, or the raw
  request payload. ``request_payload_ref`` is a file path to the request JSON
  on disk, never the payload itself. ``callback_receipt_id`` is a FK to
  ``callback_receipts.callback_id``, set when the callback arrives.

GPU cost rates (built-in defaults, overridable via constructor):
  - RTX_4090:   $0.40 / GPU-hour
  - A100_80GB:  $1.10 / GPU-hour
  - A100_40GB:  $0.80 / GPU-hour
  - L4:         $0.25 / GPU-hour
  - Default:    $0.50 / GPU-hour (unknown GPU types)
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from fincept_db.observability import (
    CostSummaryRow,
    JobCostEventRow,
    JobMetricRow,
    TrainingJobRow,
)

__all__ = [
    "DEFAULT_GPU_RATES",
    "CostTracker",
    "estimate_gpu_cost",
]


# ---------------------------------------------------------------------------
# GPU cost rate table (USD per GPU-hour)
# ---------------------------------------------------------------------------

DEFAULT_GPU_RATES: dict[str, Decimal] = {
    "RTX_4090": Decimal("0.40"),
    "A100_80GB": Decimal("1.10"),
    "A100_40GB": Decimal("0.80"),
    "L4": Decimal("0.25"),
}

DEFAULT_GPU_RATE: Decimal = Decimal("0.50")


def estimate_gpu_cost(
    gpu_type: str | None,
    gpu_count: int,
    duration_seconds: float | int | Decimal,
    *,
    rates: dict[str, Decimal] | None = None,
) -> Decimal:
    """Estimate GPU cost from the rate table.

    Args:
        gpu_type: GPU type string (e.g. ``'RTX_4090'``). Unknown types use
            the default rate.
        gpu_count: Number of GPUs.
        duration_seconds: Wall-clock duration in seconds.
        rates: Optional override rate table (USD per GPU-hour). Defaults
            to ``DEFAULT_GPU_RATES``.

    Returns:
        Total cost as a ``Decimal`` (USD). Computed as::

            rate = rates.get(gpu_type, DEFAULT_GPU_RATE)  # $/GPU-hour
            cost = rate * gpu_count * (duration_seconds / 3600)
    """
    rate_table = rates if rates is not None else DEFAULT_GPU_RATES
    rate = rate_table.get(gpu_type, DEFAULT_GPU_RATE) if gpu_type else DEFAULT_GPU_RATE
    hours = Decimal(str(duration_seconds)) / Decimal("3600")
    return (rate * Decimal(str(gpu_count)) * hours).quantize(Decimal("0.000001"))


# ---------------------------------------------------------------------------
# Helpers — dialect-specific insert (mirrors db_sinks.py)
# ---------------------------------------------------------------------------


def _dialect_insert(engine: Engine):
    """Return the dialect-specific insert() for the engine."""
    name = engine.dialect.name
    if name == "sqlite":
        return sqlite_insert
    return pg_insert


def _on_conflict_do_nothing(
    engine: Engine,
    model: type,
    values: dict[str, Any],
    *,
    conflict_cols: list[str],
) -> Any:
    """Build a dialect-specific INSERT ... ON CONFLICT DO NOTHING statement."""
    insert_fn = _dialect_insert(engine)
    stmt = insert_fn(model).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
    return stmt


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


class CostTracker:
    """Observability and cost tracking for RunPod training jobs.

    Uses sync sessions (``sync_session_scope`` from ``fincept_db.engine``) in
    production, or an injected ``Engine`` in tests. Does NOT make any live
    RunPod API calls — it only records what the dispatch/callback path
    reports.

    Usage from the dispatch path::

        from quant_foundry.cost_tracker import CostTracker

        tracker = CostTracker()
        tracker.record_job_dispatch(
            job_id="job-001",
            model_family="gbm",
            mode="canary",
            execution_timeout_ms=1_860_000,
            gpu_type="RTX_4090",
            gpu_count=1,
            container_image="ghcr.io/fincept/quant-foundry-worker:latest",
            request_payload_ref="/data/requests/job-001.json",
        )

    Usage from the callback path::

        tracker.update_job_status("job-001", status="completed",
                                  completed_at_ns=time.time_ns())
        tracker.link_callback("job-001", callback_receipt_id="cb-001")
    """

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        gpu_rates: dict[str, Decimal] | None = None,
    ) -> None:
        self._engine = engine
        self._gpu_rates = gpu_rates if gpu_rates is not None else dict(DEFAULT_GPU_RATES)

    @property
    def engine(self) -> Engine:
        """Return the engine (lazy-init from get_sync_engine if not injected)."""
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    # ------------------------------------------------------------------
    # Job lifecycle writes
    # ------------------------------------------------------------------

    def record_job_dispatch(
        self,
        job_id: str,
        model_family: str,
        mode: str,
        execution_timeout_ms: int | None = None,
        gpu_type: str | None = None,
        gpu_count: int = 1,
        container_image: str | None = None,
        request_payload_ref: str | None = None,
        *,
        dispatched_at_ns: int | None = None,
    ) -> None:
        """Record a job dispatch. Creates a training_jobs row with status='dispatched'.

        Idempotent: uses ``ON CONFLICT (job_id) DO NOTHING`` so a replayed
        dispatch does not create a second row or overwrite the existing one.
        """
        ts = dispatched_at_ns if dispatched_at_ns is not None else time.time_ns()
        engine = self.engine
        with Session(engine) as session:
            stmt = _on_conflict_do_nothing(
                engine,
                TrainingJobRow,
                {
                    "job_id": job_id,
                    "model_family": model_family,
                    "mode": mode,
                    "status": "dispatched",
                    "dispatched_at_ns": ts,
                    "started_at_ns": None,
                    "completed_at_ns": None,
                    "execution_timeout_ms": execution_timeout_ms,
                    "gpu_type": gpu_type,
                    "gpu_count": gpu_count,
                    "container_image": container_image,
                    "request_payload_ref": request_payload_ref,
                    "callback_receipt_id": None,
                },
                conflict_cols=["job_id"],
            )
            session.execute(stmt)
            session.commit()

    def update_job_status(
        self,
        job_id: str,
        status: str,
        started_at_ns: int | None = None,
        completed_at_ns: int | None = None,
    ) -> None:
        """Update the status of a training_jobs row.

        Only sets ``started_at_ns`` / ``completed_at_ns`` if provided (non-None)
        so a status-only update does not clobber existing timestamps.
        """
        with Session(self.engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
            ).first()
            if row is None:
                raise KeyError(f"training_jobs row not found for job_id={job_id!r}")
            row.status = status
            if started_at_ns is not None:
                row.started_at_ns = started_at_ns
            if completed_at_ns is not None:
                row.completed_at_ns = completed_at_ns
            session.commit()

    def link_callback(self, job_id: str, callback_receipt_id: str) -> None:
        """Set ``callback_receipt_id`` on the training_jobs row.

        Called when a signed callback arrives and is processed — links the
        job to its receipt row in ``callback_receipts``.
        """
        with Session(self.engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
            ).first()
            if row is None:
                raise KeyError(f"training_jobs row not found for job_id={job_id!r}")
            row.callback_receipt_id = callback_receipt_id
            session.commit()

    # ------------------------------------------------------------------
    # Cost event + metric writes
    # ------------------------------------------------------------------

    def record_cost_event(
        self,
        job_id: str,
        event_type: str,
        amount: float | int | Decimal,
        unit_cost: float | int | Decimal,
        metadata: dict[str, Any] | None = None,
        *,
        recorded_at_ns: int | None = None,
        currency: str = "USD",
        event_id: str | None = None,
    ) -> str:
        """Record a cost event for a job. Returns the ``event_id``.

        Computes ``total_cost = amount * unit_cost`` (both converted to
        ``Decimal`` for exact arithmetic).
        """
        ts = recorded_at_ns if recorded_at_ns is not None else time.time_ns()
        eid = event_id if event_id is not None else f"ce-{uuid.uuid4().hex[:16]}"
        amt = Decimal(str(amount))
        uc = Decimal(str(unit_cost))
        total = (amt * uc).quantize(Decimal("0.000001"))

        with Session(self.engine) as session:
            row = JobCostEventRow(
                event_id=eid,
                job_id=job_id,
                event_type=event_type,
                amount=amt,
                unit_cost=uc,
                total_cost=total,
                currency=currency,
                recorded_at_ns=ts,
                extra_metadata=metadata,
            )
            session.add(row)
            session.commit()
        return eid

    def record_metric(
        self,
        job_id: str,
        metric_type: str,
        value: float | int | Decimal,
        unit: str,
        *,
        recorded_at_ns: int | None = None,
        metric_id: str | None = None,
    ) -> str:
        """Record an operational metric for a job. Returns the ``metric_id``."""
        ts = recorded_at_ns if recorded_at_ns is not None else time.time_ns()
        mid = metric_id if metric_id is not None else f"jm-{uuid.uuid4().hex[:16]}"
        val = Decimal(str(value))

        with Session(self.engine) as session:
            row = JobMetricRow(
                metric_id=mid,
                job_id=job_id,
                metric_type=metric_type,
                value=val,
                unit=unit,
                recorded_at_ns=ts,
            )
            session.add(row)
            session.commit()
        return mid

    # ------------------------------------------------------------------
    # Cost computation + period rollup
    # ------------------------------------------------------------------

    def compute_job_cost(self, job_id: str) -> Decimal:
        """Sum all ``job_cost_events.total_cost`` for a job.

        Returns ``Decimal("0")`` if the job has no cost events.
        """
        with Session(self.engine) as session:
            total = session.scalars(
                select(func.coalesce(
                    func.sum(JobCostEventRow.total_cost), Decimal("0")
                )).where(JobCostEventRow.job_id == job_id)
            ).one()
            return total if isinstance(total, Decimal) else Decimal(str(total))

    def compute_period_cost(
        self,
        model_family: str,
        period_start_ns: int,
        period_end_ns: int,
        *,
        summary_id: str | None = None,
        currency: str = "USD",
    ) -> Decimal:
        """Aggregate cost across jobs in a period, upsert into ``cost_summary``.

        Sums ``job_cost_events.total_cost`` for all jobs of ``model_family``
        whose ``dispatched_at_ns`` falls within ``[period_start_ns,
        period_end_ns)``. Also counts the jobs and sums GPU-seconds cost
        events. Upserts the result into ``cost_summary`` (insert or update
        on the ``(model_family, period_start_ns)`` unique key).

        Returns the total cost as a ``Decimal``.
        """
        with Session(self.engine) as session:
            # Total cost across all events for jobs in the period.
            total_cost = session.scalars(
                select(func.coalesce(
                    func.sum(JobCostEventRow.total_cost), Decimal("0")
                ))
                .join(TrainingJobRow, JobCostEventRow.job_id == TrainingJobRow.job_id)
                .where(TrainingJobRow.model_family == model_family)
                .where(TrainingJobRow.dispatched_at_ns >= period_start_ns)
                .where(TrainingJobRow.dispatched_at_ns < period_end_ns)
            ).one()
            total_cost = total_cost if isinstance(total_cost, Decimal) else Decimal(str(total_cost))

            # Total GPU-seconds cost (event_type='gpu_seconds').
            gpu_seconds_cost = session.scalars(
                select(func.coalesce(
                    func.sum(JobCostEventRow.total_cost), Decimal("0")
                ))
                .join(TrainingJobRow, JobCostEventRow.job_id == TrainingJobRow.job_id)
                .where(TrainingJobRow.model_family == model_family)
                .where(TrainingJobRow.dispatched_at_ns >= period_start_ns)
                .where(TrainingJobRow.dispatched_at_ns < period_end_ns)
                .where(JobCostEventRow.event_type == "gpu_seconds")
            ).one()
            gpu_seconds_cost = (
                gpu_seconds_cost if isinstance(gpu_seconds_cost, Decimal)
                else Decimal(str(gpu_seconds_cost))
            )

            # Count of distinct jobs in the period.
            total_jobs = session.scalars(
                select(func.count())
                .select_from(TrainingJobRow)
                .where(TrainingJobRow.model_family == model_family)
                .where(TrainingJobRow.dispatched_at_ns >= period_start_ns)
                .where(TrainingJobRow.dispatched_at_ns < period_end_ns)
            ).one()
            total_jobs = int(total_jobs) if total_jobs is not None else 0

            sid = summary_id if summary_id is not None else (
                f"cs-{model_family}-{period_start_ns}"
            )

            # Upsert into cost_summary.
            engine = self.engine
            insert_fn = _dialect_insert(engine)
            values = {
                "summary_id": sid,
                "model_family": model_family,
                "period_start_ns": period_start_ns,
                "period_end_ns": period_end_ns,
                "total_jobs": total_jobs,
                "total_cost": total_cost,
                "total_gpu_seconds": gpu_seconds_cost,
                "currency": currency,
            }
            stmt = insert_fn(CostSummaryRow).values(**values)
            # Update on conflict (recompute the rollup).
            update_cols = {
                "period_end_ns": stmt.excluded.period_end_ns,
                "total_jobs": stmt.excluded.total_jobs,
                "total_cost": stmt.excluded.total_cost,
                "total_gpu_seconds": stmt.excluded.total_gpu_seconds,
                "currency": stmt.excluded.currency,
            }
            stmt = stmt.on_conflict_do_update(
                index_elements=["model_family", "period_start_ns"],
                set_=update_cols,
            )
            session.execute(stmt)
            session.commit()
            return total_cost

    # ------------------------------------------------------------------
    # GPU cost estimation
    # ------------------------------------------------------------------

    def estimate_gpu_cost(
        self,
        gpu_type: str | None,
        gpu_count: int,
        duration_seconds: float | int | Decimal,
    ) -> Decimal:
        """Estimate GPU cost using this tracker's rate table."""
        return estimate_gpu_cost(
            gpu_type, gpu_count, duration_seconds, rates=self._gpu_rates
        )

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Return the training_jobs row for ``job_id``, or None."""
        with Session(self.engine) as session:
            row = session.scalars(
                select(TrainingJobRow).where(TrainingJobRow.job_id == job_id)
            ).first()
            if row is None:
                return None
            return self._job_to_dict(row)

    def list_jobs(
        self,
        status: str | None = None,
        model_family: str | None = None,
    ) -> list[dict[str, Any]]:
        """List training_jobs rows, optionally filtered by status/model_family."""
        with Session(self.engine) as session:
            stmt = select(TrainingJobRow)
            if status is not None:
                stmt = stmt.where(TrainingJobRow.status == status)
            if model_family is not None:
                stmt = stmt.where(TrainingJobRow.model_family == model_family)
            stmt = stmt.order_by(TrainingJobRow.dispatched_at_ns.desc())
            rows = session.scalars(stmt).all()
            return [self._job_to_dict(r) for r in rows]

    def get_job_metrics(self, job_id: str) -> list[dict[str, Any]]:
        """Return all job_metrics rows for ``job_id``."""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(JobMetricRow)
                .where(JobMetricRow.job_id == job_id)
                .order_by(JobMetricRow.recorded_at_ns.asc())
            ).all()
            return [
                {
                    "metric_id": r.metric_id,
                    "job_id": r.job_id,
                    "metric_type": r.metric_type,
                    "value": r.value,
                    "unit": r.unit,
                    "recorded_at_ns": r.recorded_at_ns,
                }
                for r in rows
            ]

    def get_job_cost_events(self, job_id: str) -> list[dict[str, Any]]:
        """Return all job_cost_events rows for ``job_id``."""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(JobCostEventRow)
                .where(JobCostEventRow.job_id == job_id)
                .order_by(JobCostEventRow.recorded_at_ns.asc())
            ).all()
            return [
                {
                    "event_id": r.event_id,
                    "job_id": r.job_id,
                    "event_type": r.event_type,
                    "amount": r.amount,
                    "unit_cost": r.unit_cost,
                    "total_cost": r.total_cost,
                    "currency": r.currency,
                    "recorded_at_ns": r.recorded_at_ns,
                    "metadata": r.extra_metadata,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _job_to_dict(row: TrainingJobRow) -> dict[str, Any]:
        return {
            "job_id": row.job_id,
            "model_family": row.model_family,
            "mode": row.mode,
            "status": row.status,
            "dispatched_at_ns": row.dispatched_at_ns,
            "started_at_ns": row.started_at_ns,
            "completed_at_ns": row.completed_at_ns,
            "execution_timeout_ms": row.execution_timeout_ms,
            "gpu_type": row.gpu_type,
            "gpu_count": row.gpu_count,
            "container_image": row.container_image,
            "request_payload_ref": row.request_payload_ref,
            "callback_receipt_id": row.callback_receipt_id,
        }
