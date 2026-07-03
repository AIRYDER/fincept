"""
quant_foundry.telemetry — Cost and queue telemetry (Phase 6 / T-6.3).

Records per-job cost estimates (by GPU type and duration) and per-phase
timing (queue, image pull, train, artifact upload, verification), plus
batch cost reports that let an operator ask "what did this training
batch cost?" and "where was the time spent?".

Design (mirrors job_ledger.py / outbox.py / budget.py for consistency):

- Append-only JSONL under ``<base_dir>/cost_telemetry.jsonl``. Each line
  is the full ``JobTelemetryRecord`` at write time. On restart the last
  line per ``job_id`` wins (last-writer-wins by file order).
- Pydantic v2 ``BaseModel`` with ``frozen=True`` and ``extra="forbid"``
  for audit integrity (matches JobLedgerRecord / OutboxRecord).
- ``StrEnum`` for the GPU type and timing phase enums (matches
  JobStatus / JobLedgerState).
- ``fsync`` after every write (best-effort on platforms without it).

CRITICAL invariant: a missing GPU price marks the cost as UNKNOWN
(``cost_unknown=True``, ``estimated_cost_cents=None``), NOT zero. This
is a hard requirement — silently reporting a zero cost for an unpriced
GPU would hide real spend from operators.
"""

from __future__ import annotations

import json
import math
import pathlib
import time
import types
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - import only for type hints
    from quant_foundry.outbox import JobOutbox


# --- GPU types + pricing --------------------------------------------------- #


class GPUType(StrEnum):
    """Supported GPU types for cost estimation.

    ``UNKNOWN`` is used when the GPU type cannot be determined (e.g. the
    endpoint id does not map to a known GPU). An ``UNKNOWN`` GPU always
    yields ``cost_unknown=True`` — its price is never assumed to be zero.
    """

    NVIDIA_RTX_4090 = "nvidia_rtx_4090"
    NVIDIA_A100 = "nvidia_a100"
    NVIDIA_H100 = "nvidia_h100"
    NVIDIA_RTX_3090 = "nvidia_rtx_3090"
    NVIDIA_T4 = "nvidia_t4"
    UNKNOWN = "unknown"


# Approximate RunPod market rates (per-GPU hourly cost in cents).
# 1 USD = 100 cents. These are conservative round-number estimates used
# for *estimation* only; actual cost is recorded from the RunPod callback
# when available. A GPU not in this table yields cost_unknown=True.
#
# Wrapped in ``types.MappingProxyType`` so the table is truly immutable
# (a plain ``dict`` module-level constant could be mutated by accident,
# which would silently change cost estimates for every caller). The
# proxy preserves dict-like read access (``GPU_PRICING.get(...)``,
# ``GPU_PRICING[k]``, ``k in GPU_PRICING``, iteration) while rejecting
# writes with ``TypeError``.
_GPU_PRICING_RAW: dict[str, int] = {
    GPUType.NVIDIA_RTX_4090.value: 44,  # ~$0.44/hr
    GPUType.NVIDIA_A100.value: 263,  # ~$2.63/hr
    GPUType.NVIDIA_H100.value: 393,  # ~$3.93/hr
    GPUType.NVIDIA_RTX_3090.value: 22,  # ~$0.22/hr
    GPUType.NVIDIA_T4.value: 16,  # ~$0.16/hr
}
GPU_PRICING: types.MappingProxyType[str, int] = types.MappingProxyType(
    _GPU_PRICING_RAW
)


# --- timing phases --------------------------------------------------------- #


class TimingPhase(StrEnum):
    """Phases of a training job's lifecycle for timing breakdown.

    Operators use the per-phase breakdown to answer "where was the time
    spent?" — e.g. a job that spent 90% of wall-clock in IMAGE_PULL
    indicates a container image problem, not a model problem.
    """

    QUEUE = "queue"  # time waiting in the outbox before dispatch
    IMAGE_PULL = "image_pull"  # RunPod worker pulling the container image
    TRAIN = "train"  # actual training compute
    ARTIFACT_UPLOAD = "artifact_upload"  # uploading model artifacts to storage
    VERIFICATION = "verification"  # artifact verification (checksum, schema)
    TOTAL = "total"  # end-to-end wall-clock (sum of the above, approximately)


# --- Pydantic records ------------------------------------------------------ #


class PhaseTiming(BaseModel):
    """Timing for one phase of a job.

    Frozen + ``extra="forbid"`` for audit integrity. ``duration_seconds``
    is the authoritative field; ``start_ns`` / ``end_ns`` are optional
    provenance (a caller may know the duration without exact timestamps).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: TimingPhase
    start_ns: int | None = None
    end_ns: int | None = None
    duration_seconds: float = 0.0


class JobTelemetryRecord(BaseModel):
    """Durable record of one job's cost + timing telemetry.

    Frozen + ``extra="forbid"`` for audit integrity (matches
    ``JobLedgerRecord`` / ``OutboxRecord``). One row per job; the row is
    rewritten (appended) on every update so the JSONL file is a complete
    replay of the job's telemetry.

    Cost semantics:
    - ``estimated_cost_cents``: cost estimated from GPU pricing + duration.
      ``None`` when the GPU price is unknown (``cost_unknown=True``).
    - ``actual_cost_cents``: cost recorded from the RunPod callback when
      available. ``None`` until the callback returns.
    - ``cost_unknown``: ``True`` when the GPU price is missing. This is
      the CRITICAL invariant — unknown cost is never silently zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    gpu_type: str
    gpu_hourly_cost_cents: int | None = None
    phases: tuple[PhaseTiming, ...] = Field(default_factory=tuple)
    estimated_cost_cents: int | None = None
    actual_cost_cents: int | None = None
    cost_unknown: bool = False
    recorded_at_ns: int


class BatchCostReport(BaseModel):
    """Aggregated cost + timing across a batch of jobs.

    Lets an operator ask "what did this training batch cost?" (total /
    average cost, broken down by GPU type) and "where was the time
    spent?" (average duration per phase). Frozen + ``extra="forbid"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_jobs: int
    total_estimated_cost_cents: int
    total_actual_cost_cents: int
    jobs_with_unknown_cost: int
    avg_cost_cents: int
    # gpu_type -> {"count", "total_cost_cents", "avg_cost_cents"}
    by_gpu_type: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # phase -> avg duration_seconds
    by_phase: dict[str, float] = Field(default_factory=dict)
    generated_at_ns: int


class QueueMetrics(BaseModel):
    """Queue health metrics computed from the outbox.

    Lets an operator see queue depth, how long jobs wait on average, the
    worst-case wait, and the dispatch throughput. Frozen +
    ``extra="forbid"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    queue_depth: int
    avg_wait_seconds: float
    max_wait_seconds: float
    dispatch_rate_per_hour: float
    computed_at_ns: int


# --- CostTelemetry --------------------------------------------------------- #


class CostTelemetry:
    """Append-only JSONL-backed cost + timing telemetry store.

    One process, one writer per file. File path:
    ``<base_dir>/cost_telemetry.jsonl``. Each line is the full
    ``JobTelemetryRecord`` JSON at write time. Reload replays all lines
    and keeps the last record per ``job_id`` (last-writer-wins by file
    order).

    The store is read-only with respect to the outbox and ledger: it
    observes cost + timing events but never drives job state.
    """

    FILENAME = "cost_telemetry.jsonl"

    def __init__(self, base_dir: pathlib.Path | str) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / self.FILENAME
        # In-memory index: job_id -> latest JobTelemetryRecord
        self._records: dict[str, JobTelemetryRecord] = {}
        self._reload()

    # --- durability ---

    def _reload(self) -> None:
        """Replay JSONL from disk. Last line per job_id wins."""
        self._records = {}
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                rec = JobTelemetryRecord.model_validate(data)
                self._records[rec.job_id] = rec

    def _append(self, rec: JobTelemetryRecord) -> None:
        """Append one record line and fsync for durability."""
        line = rec.model_dump_json()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                import os

                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                # fsync best-effort on platforms that don't support it.
                pass

    def _store(self, rec: JobTelemetryRecord) -> JobTelemetryRecord:
        """Persist a record and update the in-memory index."""
        self._append(rec)
        self._records[rec.job_id] = rec
        return rec

    # --- public API ---

    def estimate_cost(
        self,
        gpu_type: str,
        duration_seconds: float,
    ) -> tuple[int | None, bool]:
        """Estimate cost for a GPU type + duration.

        Returns ``(cost_cents, cost_unknown)``. If the GPU type is not in
        ``GPU_PRICING`` (including ``GPUType.UNKNOWN``), returns
        ``(None, True)`` — the cost is UNKNOWN, never zero.

        Lookup is by canonical ``GPUType`` enum value (the lowercase
        string form, e.g. ``"nvidia_rtx_4090"``). The lookup itself is
        case-sensitive against the canonical table keys. Callers that
        only have a raw endpoint id (e.g. ``"RTX4090-training"``) should
        first convert it with :func:`infer_gpu_type`, which performs a
        case-insensitive substring match and returns the canonical
        ``GPUType`` value suitable for this method.

        Args:
            gpu_type: a canonical ``GPUType`` value string (or a raw
                string that exactly matches a canonical key). Use
                ``infer_gpu_type()`` to convert endpoint ids.
            duration_seconds: wall-clock duration of the job.

        Returns:
            ``(estimated_cost_cents, cost_unknown)``. ``cost_cents`` is
            ``None`` when the GPU price is unknown.
        """
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be >= 0")
        hourly = GPU_PRICING.get(gpu_type)
        if hourly is None:
            # Also try the enum value form (handles callers passing the
            # enum instance or a raw string that matches an enum value).
            return None, True
        # cost = hourly_cents * (duration_seconds / 3600), rounded up to
        # the nearest cent (conservative — never under-bill).
        cost = math.ceil(hourly * duration_seconds / 3600.0)
        return cost, False

    def record_job_timing(
        self,
        job_id: str,
        gpu_type: str,
        phases: tuple[PhaseTiming, ...] | list[PhaseTiming],
        *,
        actual_cost_cents: int | None = None,
    ) -> JobTelemetryRecord:
        """Record (or update) telemetry for one job.

        Creates a new ``JobTelemetryRecord`` if none exists for
        ``job_id``, or updates the existing one (merging phases and
        actual cost). When ``actual_cost_cents`` is None, the estimated
        cost is computed from the GPU type + the TOTAL phase duration
        (or the sum of phase durations if no TOTAL phase is present).

        Missing GPU price => ``estimated_cost_cents=None``,
        ``cost_unknown=True`` (NOT zero).

        Args:
            job_id: the outbox job id (source of truth).
            gpu_type: GPU type string (a ``GPUType`` value or raw).
            phases: per-phase timing. May be empty.
            actual_cost_cents: actual cost from the RunPod callback, if
                known. ``None`` means "not yet known; use estimate".

        Returns:
            The persisted ``JobTelemetryRecord``.
        """
        if not job_id or not isinstance(job_id, str):
            raise ValueError("job_id must be non-empty str")

        phases_tuple = tuple(phases)
        hourly = GPU_PRICING.get(gpu_type)
        cost_unknown = hourly is None

        # Determine the duration to use for the estimate: prefer a TOTAL
        # phase, else sum all phase durations.
        total_duration = 0.0
        for p in phases_tuple:
            if p.phase == TimingPhase.TOTAL:
                total_duration = p.duration_seconds
                break
            total_duration += p.duration_seconds

        if cost_unknown:
            estimated: int | None = None
        else:
            estimated, _ = self.estimate_cost(gpu_type, total_duration)

        existing = self._records.get(job_id)
        now = time.time_ns()

        if existing is None:
            rec = JobTelemetryRecord(
                job_id=job_id,
                gpu_type=gpu_type,
                gpu_hourly_cost_cents=hourly,
                phases=phases_tuple,
                estimated_cost_cents=estimated,
                actual_cost_cents=actual_cost_cents,
                cost_unknown=cost_unknown,
                recorded_at_ns=now,
            )
        else:
            # Merge: union of phases (caller-supplied phases replace
            # existing ones for the same phase; others are kept). This
            # allows incremental recording (queue time first, then train
            # time, then artifact upload time, etc.).
            existing_by_phase = {p.phase: p for p in existing.phases}
            for p in phases_tuple:
                existing_by_phase[p.phase] = p
            merged_phases = tuple(
                existing_by_phase[k] for k in sorted(existing_by_phase, key=lambda x: x.value)
            )
            # Recompute total duration from merged phases.
            merged_total = 0.0
            for p in merged_phases:
                if p.phase == TimingPhase.TOTAL:
                    merged_total = p.duration_seconds
                    break
                merged_total += p.duration_seconds
            if cost_unknown:
                merged_estimated: int | None = None
            else:
                merged_estimated, _ = self.estimate_cost(gpu_type, merged_total)
            rec = existing.model_copy(
                update={
                    "gpu_type": gpu_type,
                    "gpu_hourly_cost_cents": hourly,
                    "phases": merged_phases,
                    "estimated_cost_cents": merged_estimated,
                    "actual_cost_cents": actual_cost_cents
                    if actual_cost_cents is not None
                    else existing.actual_cost_cents,
                    "cost_unknown": cost_unknown,
                    "recorded_at_ns": now,
                }
            )
        return self._store(rec)

    def get_job_cost(self, job_id: str) -> JobTelemetryRecord | None:
        """Return the latest telemetry record for ``job_id``, or None."""
        return self._records.get(job_id)

    def get_batch_cost_report(
        self,
        job_ids: list[str] | None = None,
    ) -> BatchCostReport:
        """Aggregate cost + timing across a batch of jobs.

        If ``job_ids`` is None, all recorded jobs are included. The
        report lets an operator ask "what did this training batch cost?"
        (total/avg cost, by GPU type) and "where was the time spent?"
        (avg duration per phase).

        Jobs with unknown cost (missing GPU price) are counted in
        ``jobs_with_unknown_cost`` and excluded from cost totals (their
        cost is unknown, not zero — including them would under-report).
        """
        if job_ids is None:
            records = list(self._records.values())
        else:
            records = [self._records[j] for j in job_ids if j in self._records]

        total_estimated = 0
        total_actual = 0
        jobs_unknown = 0
        by_gpu: dict[str, dict[str, Any]] = {}
        phase_durations: dict[str, list[float]] = {}

        for rec in records:
            # Estimated cost: only count known costs.
            if rec.estimated_cost_cents is not None and not rec.cost_unknown:
                total_estimated += rec.estimated_cost_cents
            else:
                jobs_unknown += 1
            # Actual cost: count if known.
            if rec.actual_cost_cents is not None:
                total_actual += rec.actual_cost_cents

            # By GPU type.
            gpu = rec.gpu_type
            bucket = by_gpu.setdefault(gpu, {"count": 0, "total_cost_cents": 0, "avg_cost_cents": 0})
            bucket["count"] += 1
            cost_for_bucket = (
                rec.actual_cost_cents
                if rec.actual_cost_cents is not None
                else rec.estimated_cost_cents
            )
            if cost_for_bucket is not None:
                bucket["total_cost_cents"] += cost_for_bucket

            # By phase: collect durations for averaging.
            for p in rec.phases:
                phase_durations.setdefault(p.phase.value, []).append(p.duration_seconds)

        # Finalize by-gpu averages.
        for bucket in by_gpu.values():
            cnt = bucket["count"]
            bucket["avg_cost_cents"] = (
                bucket["total_cost_cents"] // cnt if cnt > 0 else 0
            )

        # By phase: average duration per phase.
        by_phase: dict[str, float] = {}
        for phase, durations in phase_durations.items():
            by_phase[phase] = sum(durations) / len(durations) if durations else 0.0

        total_jobs = len(records)
        # Average cost: prefer actual, fall back to estimated, over jobs
        # with any known cost.
        jobs_with_cost = total_jobs - jobs_unknown
        if jobs_with_cost > 0:
            avg_cost = (total_estimated + total_actual) // max(jobs_with_cost, 1)
        else:
            avg_cost = 0

        return BatchCostReport(
            total_jobs=total_jobs,
            total_estimated_cost_cents=total_estimated,
            total_actual_cost_cents=total_actual,
            jobs_with_unknown_cost=jobs_unknown,
            avg_cost_cents=avg_cost,
            by_gpu_type=by_gpu,
            by_phase=by_phase,
            generated_at_ns=time.time_ns(),
        )

    def list(self, limit: int = 100) -> list[JobTelemetryRecord]:
        """List telemetry records (insertion order). ``limit <= 0`` = all."""
        records = list(self._records.values())
        if limit and limit > 0:
            records = records[:limit]
        return records


# --- QueueTelemetry -------------------------------------------------------- #


class QueueTelemetry:
    """Compute queue health metrics from the outbox.

    Read-only with respect to the outbox: it observes queue state but
    never drives transitions. Metrics are computed on demand from the
    outbox's in-memory records + history (no separate store needed).
    """

    def compute_queue_metrics(self, outbox: "JobOutbox") -> QueueMetrics:
        """Compute queue depth, wait times, and dispatch rate.

        - ``queue_depth``: number of jobs currently in QUEUED status.
        - ``avg_wait_seconds``: average time jobs spent waiting in the
          queue (from QUEUED -> DISPATCHING/DISPATCHED in history).
        - ``max_wait_seconds``: worst-case queue wait observed.
        - ``dispatch_rate_per_hour``: dispatched jobs per hour, computed
          over the time span from the first QUEUED entry to now (or the
          last DISPATCHED entry, whichever is later).

        Jobs with no dispatch transition yet are counted in queue depth
        but not in wait-time averages (their wait is ongoing).
        """
        from quant_foundry.outbox import JobStatus

        records = outbox.list()
        queue_depth = sum(1 for r in records if r.status == JobStatus.QUEUED)

        wait_seconds: list[float] = []
        first_queued_ns: int | None = None
        last_dispatched_ns: int | None = None
        dispatched_count = 0

        for rec in records:
            queued_ts: int | None = None
            dispatched_ts: int | None = None
            for entry in rec.history:
                status = entry.get("status")
                ts = entry.get("ts_ns")
                if ts is None:
                    continue
                if status == JobStatus.QUEUED.value and queued_ts is None:
                    queued_ts = ts
                    if first_queued_ns is None or ts < first_queued_ns:
                        first_queued_ns = ts
                elif status in (
                    JobStatus.DISPATCHING.value,
                    JobStatus.DISPATCHED.value,
                ):
                    if dispatched_ts is None:
                        dispatched_ts = ts
                    last_dispatched_ns = ts
            if queued_ts is not None and dispatched_ts is not None:
                wait = (dispatched_ts - queued_ts) / 1e9
                if wait >= 0:
                    wait_seconds.append(wait)
                    dispatched_count += 1

        avg_wait = sum(wait_seconds) / len(wait_seconds) if wait_seconds else 0.0
        max_wait = max(wait_seconds) if wait_seconds else 0.0

        # Dispatch rate: dispatched jobs / hours elapsed.
        now_ns = time.time_ns()
        if first_queued_ns is not None:
            span_end = max(last_dispatched_ns or now_ns, now_ns)
            span_seconds = max((span_end - first_queued_ns) / 1e9, 1e-9)
            dispatch_rate = dispatched_count / (span_seconds / 3600.0)
        else:
            dispatch_rate = 0.0

        return QueueMetrics(
            queue_depth=queue_depth,
            avg_wait_seconds=avg_wait,
            max_wait_seconds=max_wait,
            dispatch_rate_per_hour=dispatch_rate,
            computed_at_ns=now_ns,
        )


# --- helpers --------------------------------------------------------------- #


def infer_gpu_type(endpoint_id: str | None) -> str:
    """Infer a GPU type from a RunPod endpoint id.

    RunPod endpoint ids sometimes encode the GPU type (e.g.
    ``"rtx4090-training"``). This helper does a case-insensitive
    substring match against known GPU names. If no match is found,
    returns ``GPUType.UNKNOWN`` — which yields ``cost_unknown=True``
    (never a silent zero cost).

    Args:
        endpoint_id: the RunPod serverless endpoint id (or None).

    Returns:
        A ``GPUType`` value string.
    """
    if not endpoint_id:
        return GPUType.UNKNOWN.value
    lower = endpoint_id.lower()
    # Order matters: check more specific patterns first.
    if "4090" in lower:
        return GPUType.NVIDIA_RTX_4090.value
    if "a100" in lower:
        return GPUType.NVIDIA_A100.value
    if "h100" in lower:
        return GPUType.NVIDIA_H100.value
    if "3090" in lower:
        return GPUType.NVIDIA_RTX_3090.value
    if "t4" in lower:
        return GPUType.NVIDIA_T4.value
    return GPUType.UNKNOWN.value
