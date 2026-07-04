"""
TDD tests for quant_foundry.telemetry (Phase 6 / T-6.3).

Acceptance:
- Cost + queue telemetry for training jobs.
- Per-job cost estimates by GPU type + duration; missing GPU price =>
  cost_unknown=True (NEVER a silent zero cost).
- Per-phase timing (queue, image_pull, train, artifact_upload,
  verification, total) with incremental merge.
- Batch cost report: total/avg cost, by-GPU-type breakdown, by-phase
  avg duration, unknown-cost jobs counted separately and excluded from
  cost totals.
- Queue metrics: depth, avg/max wait, dispatch rate.
- infer_gpu_type(): case-insensitive endpoint-id -> canonical GPUType.
- Append-only JSONL persistence (survives restart); fsync after writes.
- Pydantic v2 frozen + extra=forbid; StrEnum for GPU type + phase.
- GPU_PRICING is immutable (MappingProxyType).
- runpod_client integration: telemetry recording during dispatch.
"""

from __future__ import annotations

import json
import math
import pathlib
import time
import types

import pytest
from quant_foundry.telemetry import (
    GPU_PRICING,
    BatchCostReport,
    CostTelemetry,
    GPUType,
    JobTelemetryRecord,
    PhaseTiming,
    QueueMetrics,
    QueueTelemetry,
    TimingPhase,
    infer_gpu_type,
)

# --- GPUType enum ---------------------------------------------------------- #


def test_gputype_enum_values() -> None:
    """GPUType has the spec-required canonical values (lowercase strings)."""
    expected = {
        "nvidia_rtx_4090",
        "nvidia_a100",
        "nvidia_h100",
        "nvidia_rtx_3090",
        "nvidia_t4",
        "unknown",
    }
    actual = {g.value for g in GPUType}
    assert actual == expected
    # StrEnum members are strings.
    assert GPUType.NVIDIA_RTX_4090.value == "nvidia_rtx_4090"
    assert GPUType.UNKNOWN.value == "unknown"
    assert isinstance(GPUType.NVIDIA_A100, str)


def test_gpu_pricing_contains_all_known_gpus_except_unknown() -> None:
    """Every GPUType except UNKNOWN must have a price entry."""
    for g in GPUType:
        if g == GPUType.UNKNOWN:
            assert g.value not in GPU_PRICING
            continue
        assert g.value in GPU_PRICING, f"missing price for {g.value}"
        assert GPU_PRICING[g.value] > 0


def test_gpu_pricing_is_immutable_mappingproxy() -> None:
    """GPU_PRICING is a MappingProxyType and rejects writes."""
    assert isinstance(GPU_PRICING, types.MappingProxyType)
    # Read access works (dict-like).
    assert GPU_PRICING.get(GPUType.NVIDIA_A100.value) == 263
    assert GPUType.NVIDIA_H100.value in GPU_PRICING
    assert len(GPU_PRICING) >= 5
    # Write attempts raise TypeError.
    with pytest.raises(TypeError):
        GPU_PRICING[GPUType.NVIDIA_A100.value] = 999  # type: ignore[index]
    with pytest.raises(TypeError):
        del GPU_PRICING[GPUType.NVIDIA_A100.value]  # type: ignore[misc]
    with pytest.raises((TypeError, AttributeError)):
        GPU_PRICING.clear()  # type: ignore[attr-defined]
    with pytest.raises((TypeError, AttributeError)):
        GPU_PRICING.update({"x": 1})  # type: ignore[attr-defined]


# --- TimingPhase + PhaseTiming -------------------------------------------- #


def test_timing_phase_enum_values() -> None:
    expected = {
        "queue",
        "image_pull",
        "train",
        "artifact_upload",
        "verification",
        "total",
    }
    actual = {p.value for p in TimingPhase}
    assert actual == expected
    assert TimingPhase.QUEUE.value == "queue"
    assert TimingPhase.TOTAL.value == "total"


def test_phase_timing_frozen_and_extra_forbid() -> None:
    pt = PhaseTiming(phase=TimingPhase.TRAIN, duration_seconds=10.0)
    assert pt.phase == TimingPhase.TRAIN
    assert pt.duration_seconds == 10.0
    assert pt.start_ns is None
    assert pt.end_ns is None
    # frozen: mutation raises
    with pytest.raises(Exception):
        pt.duration_seconds = 5.0  # type: ignore[misc]
    # extra=forbid
    with pytest.raises(Exception):
        PhaseTiming(  # type: ignore[call-arg]
            phase=TimingPhase.TRAIN,
            duration_seconds=1.0,
            unknown="x",
        )


# --- CostTelemetry.estimate_cost ------------------------------------------ #


def test_estimate_cost_known_gpu_returns_cost_and_not_unknown(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    cost, unknown = ct.estimate_cost(GPUType.NVIDIA_RTX_4090.value, 3600.0)
    assert unknown is False
    assert cost == GPU_PRICING[GPUType.NVIDIA_RTX_4090.value]


def test_estimate_cost_unknown_gpu_returns_none_and_unknown(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    cost, unknown = ct.estimate_cost(GPUType.UNKNOWN.value, 3600.0)
    assert cost is None
    assert unknown is True
    # A raw unrecognized string also yields unknown.
    cost2, unknown2 = ct.estimate_cost("definitely_not_a_gpu", 3600.0)
    assert cost2 is None
    assert unknown2 is True


def test_estimate_cost_unknown_gpu_is_not_zero(tmp_path: pathlib.Path) -> None:
    """CRITICAL invariant: unknown cost is None, never 0."""
    ct = CostTelemetry(base_dir=tmp_path)
    cost, unknown = ct.estimate_cost(GPUType.UNKNOWN.value, 3600.0)
    assert cost != 0
    assert cost is None
    assert unknown is True


def test_estimate_cost_scales_linearly_with_duration(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    hourly = GPU_PRICING[GPUType.NVIDIA_A100.value]
    cost_1h, _ = ct.estimate_cost(GPUType.NVIDIA_A100.value, 3600.0)
    cost_2h, _ = ct.estimate_cost(GPUType.NVIDIA_A100.value, 7200.0)
    assert cost_1h == hourly
    assert cost_2h == hourly * 2


def test_estimate_cost_rounds_up_via_math_ceil(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    hourly = GPU_PRICING[GPUType.NVIDIA_RTX_4090.value]
    # 1 second -> hourly/3600 cents, which is fractional -> ceil up.
    cost_1s, _ = ct.estimate_cost(GPUType.NVIDIA_RTX_4090.value, 1.0)
    assert cost_1s == math.ceil(hourly * 1.0 / 3600.0)
    # A duration that would be exactly an integer stays that integer.
    cost_exact, _ = ct.estimate_cost(GPUType.NVIDIA_RTX_4090.value, 3600.0)
    assert cost_exact == hourly


def test_estimate_cost_rejects_negative_duration(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    with pytest.raises(ValueError):
        ct.estimate_cost(GPUType.NVIDIA_A100.value, -1.0)


def test_estimate_cost_zero_duration_is_zero_or_ceil(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    cost, unknown = ct.estimate_cost(GPUType.NVIDIA_A100.value, 0.0)
    assert unknown is False
    assert cost == 0


# --- CostTelemetry.record_job_timing -------------------------------------- #


def test_record_job_timing_creates_record_with_phases(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    phases = (
        PhaseTiming(phase=TimingPhase.QUEUE, duration_seconds=5.0),
        PhaseTiming(phase=TimingPhase.TRAIN, duration_seconds=3600.0),
        PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3605.0),
    )
    rec = ct.record_job_timing("job-1", GPUType.NVIDIA_A100.value, phases)
    assert isinstance(rec, JobTelemetryRecord)
    assert rec.job_id == "job-1"
    assert rec.gpu_type == GPUType.NVIDIA_A100.value
    assert rec.gpu_hourly_cost_cents == GPU_PRICING[GPUType.NVIDIA_A100.value]
    assert len(rec.phases) == 3
    # TOTAL phase (3605s) drives the estimate.
    assert rec.estimated_cost_cents == math.ceil(
        GPU_PRICING[GPUType.NVIDIA_A100.value] * 3605.0 / 3600.0
    )
    assert rec.cost_unknown is False
    assert rec.actual_cost_cents is None
    assert rec.recorded_at_ns > 0


def test_record_job_timing_unknown_gpu_marks_cost_unknown(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    phases = (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),)
    rec = ct.record_job_timing("job-u", GPUType.UNKNOWN.value, phases)
    assert rec.cost_unknown is True
    assert rec.estimated_cost_cents is None
    assert rec.gpu_hourly_cost_cents is None


def test_record_job_timing_incremental_merge_replaces_earlier_phase(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    # First record: queue + train.
    ct.record_job_timing(
        "job-m",
        GPUType.NVIDIA_A100.value,
        (
            PhaseTiming(phase=TimingPhase.QUEUE, duration_seconds=5.0),
            PhaseTiming(phase=TimingPhase.TRAIN, duration_seconds=100.0),
        ),
    )
    # Second record: train updated + artifact upload added.
    rec = ct.record_job_timing(
        "job-m",
        GPUType.NVIDIA_A100.value,
        (
            PhaseTiming(phase=TimingPhase.TRAIN, duration_seconds=3600.0),
            PhaseTiming(phase=TimingPhase.ARTIFACT_UPLOAD, duration_seconds=10.0),
        ),
    )
    phases_by = {p.phase: p for p in rec.phases}
    # queue kept from earlier record.
    assert TimingPhase.QUEUE in phases_by
    assert phases_by[TimingPhase.QUEUE].duration_seconds == 5.0
    # train replaced by later record.
    assert phases_by[TimingPhase.TRAIN].duration_seconds == 3600.0
    # artifact upload added.
    assert phases_by[TimingPhase.ARTIFACT_UPLOAD].duration_seconds == 10.0
    # No TOTAL phase => estimate uses sum of phase durations.
    total_duration = 5.0 + 3600.0 + 10.0
    assert rec.estimated_cost_cents == math.ceil(
        GPU_PRICING[GPUType.NVIDIA_A100.value] * total_duration / 3600.0
    )


def test_record_job_timing_actual_cost_recorded(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    phases = (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),)
    rec = ct.record_job_timing(
        "job-a",
        GPUType.NVIDIA_A100.value,
        phases,
        actual_cost_cents=300,
    )
    assert rec.actual_cost_cents == 300


def test_record_job_timing_rejects_empty_job_id(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    with pytest.raises(ValueError):
        ct.record_job_timing("", GPUType.NVIDIA_A100.value, ())


# --- CostTelemetry.get_job_cost ------------------------------------------- #


def test_get_job_cost_returns_latest_record(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    ct.record_job_timing(
        "job-1",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
    )
    rec = ct.get_job_cost("job-1")
    assert rec is not None
    assert rec.job_id == "job-1"
    assert ct.get_job_cost("nope") is None


# --- CostTelemetry.get_batch_cost_report ---------------------------------- #


def test_batch_cost_report_aggregates_total_cost(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    for jid in ("j1", "j2", "j3"):
        ct.record_job_timing(
            jid,
            GPUType.NVIDIA_A100.value,
            (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
        )
    report = ct.get_batch_cost_report()
    assert isinstance(report, BatchCostReport)
    assert report.total_jobs == 3
    hourly = GPU_PRICING[GPUType.NVIDIA_A100.value]
    assert report.total_estimated_cost_cents == hourly * 3
    assert report.jobs_with_unknown_cost == 0


def test_batch_cost_report_by_gpu_type_breakdown(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    ct.record_job_timing(
        "a1",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
    )
    ct.record_job_timing(
        "a2",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
    )
    ct.record_job_timing(
        "t1",
        GPUType.NVIDIA_T4.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
    )
    report = ct.get_batch_cost_report()
    a100 = report.by_gpu_type[GPUType.NVIDIA_A100.value]
    assert a100["count"] == 2
    assert a100["total_cost_cents"] == GPU_PRICING[GPUType.NVIDIA_A100.value] * 2
    t4 = report.by_gpu_type[GPUType.NVIDIA_T4.value]
    assert t4["count"] == 1
    assert t4["total_cost_cents"] == GPU_PRICING[GPUType.NVIDIA_T4.value]


def test_batch_cost_report_by_phase_avg_duration(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    ct.record_job_timing(
        "j1",
        GPUType.NVIDIA_A100.value,
        (
            PhaseTiming(phase=TimingPhase.QUEUE, duration_seconds=10.0),
            PhaseTiming(phase=TimingPhase.TRAIN, duration_seconds=100.0),
        ),
    )
    ct.record_job_timing(
        "j2",
        GPUType.NVIDIA_A100.value,
        (
            PhaseTiming(phase=TimingPhase.QUEUE, duration_seconds=20.0),
            PhaseTiming(phase=TimingPhase.TRAIN, duration_seconds=200.0),
        ),
    )
    report = ct.get_batch_cost_report()
    assert report.by_phase["queue"] == pytest.approx(15.0)
    assert report.by_phase["train"] == pytest.approx(150.0)


def test_batch_cost_report_unknown_jobs_counted_and_excluded_from_totals(
    tmp_path: pathlib.Path,
) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    ct.record_job_timing(
        "known",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
    )
    ct.record_job_timing(
        "unk",
        GPUType.UNKNOWN.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
    )
    report = ct.get_batch_cost_report()
    assert report.total_jobs == 2
    assert report.jobs_with_unknown_cost == 1
    # Only the known job's cost is in the total.
    assert report.total_estimated_cost_cents == GPU_PRICING[GPUType.NVIDIA_A100.value]


def test_batch_cost_report_subset_by_job_ids(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    for jid in ("j1", "j2", "j3"):
        ct.record_job_timing(
            jid,
            GPUType.NVIDIA_A100.value,
            (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
        )
    report = ct.get_batch_cost_report(job_ids=["j1", "j2"])
    assert report.total_jobs == 2


def test_batch_cost_report_empty_store(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    report = ct.get_batch_cost_report()
    assert report.total_jobs == 0
    assert report.total_estimated_cost_cents == 0
    assert report.jobs_with_unknown_cost == 0
    assert report.by_gpu_type == {}


# --- CostTelemetry.list ---------------------------------------------------- #


def test_list_returns_records_respecting_limit(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    for jid in ("j1", "j2", "j3"):
        ct.record_job_timing(
            jid,
            GPUType.NVIDIA_A100.value,
            (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
        )
    all_recs = ct.list()
    assert len(all_recs) == 3
    limited = ct.list(limit=2)
    assert len(limited) == 2
    # limit <= 0 returns all
    assert len(ct.list(limit=0)) == 3


# --- JSONL persistence ----------------------------------------------------- #


def test_telemetry_survives_restart(tmp_path: pathlib.Path) -> None:
    ct1 = CostTelemetry(base_dir=tmp_path)
    ct1.record_job_timing(
        "job-1",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
        actual_cost_cents=263,
    )
    assert (tmp_path / "cost_telemetry.jsonl").is_file()
    ct2 = CostTelemetry(base_dir=tmp_path)
    rec = ct2.get_job_cost("job-1")
    assert rec is not None
    assert rec.gpu_type == GPUType.NVIDIA_A100.value
    assert rec.estimated_cost_cents == GPU_PRICING[GPUType.NVIDIA_A100.value]
    assert rec.actual_cost_cents == 263


def test_telemetry_jsonl_is_append_only(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    ct.record_job_timing(
        "job-1",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.QUEUE, duration_seconds=5.0),),
    )
    ct.record_job_timing(
        "job-1",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TRAIN, duration_seconds=100.0),),
    )
    with (tmp_path / "cost_telemetry.jsonl").open("r", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    # Two writes => two lines, both for job-1 (last wins on reload).
    assert len(lines) == 2
    for line in lines:
        data = json.loads(line)
        assert data["job_id"] == "job-1"


def test_telemetry_reload_last_writer_wins(tmp_path: pathlib.Path) -> None:
    ct = CostTelemetry(base_dir=tmp_path)
    ct.record_job_timing(
        "job-1",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=3600.0),),
    )
    first = ct.get_job_cost("job-1")
    assert first is not None
    first_ns = first.recorded_at_ns
    ct.record_job_timing(
        "job-1",
        GPUType.NVIDIA_A100.value,
        (PhaseTiming(phase=TimingPhase.TOTAL, duration_seconds=7200.0),),
    )
    ct2 = CostTelemetry(base_dir=tmp_path)
    rec = ct2.get_job_cost("job-1")
    assert rec is not None
    assert rec.recorded_at_ns >= first_ns
    # The later (7200s) record won.
    assert rec.estimated_cost_cents == GPU_PRICING[GPUType.NVIDIA_A100.value] * 2


# --- Pydantic model constraints ------------------------------------------- #


def test_job_telemetry_record_frozen_and_extra_forbid() -> None:
    rec = JobTelemetryRecord(job_id="j1", gpu_type="unknown", recorded_at_ns=1)
    # frozen
    with pytest.raises(Exception):
        rec.gpu_type = "x"  # type: ignore[misc]
    # extra=forbid
    with pytest.raises(Exception):
        JobTelemetryRecord(  # type: ignore[call-arg]
            job_id="j2",
            gpu_type="unknown",
            recorded_at_ns=1,
            unknown_field="x",
        )


def test_batch_cost_report_frozen_and_extra_forbid() -> None:
    rep = BatchCostReport(
        total_jobs=0,
        total_estimated_cost_cents=0,
        total_actual_cost_cents=0,
        jobs_with_unknown_cost=0,
        avg_cost_cents=0,
        generated_at_ns=1,
    )
    with pytest.raises(Exception):
        rep.total_jobs = 5  # type: ignore[misc]
    with pytest.raises(Exception):
        BatchCostReport(  # type: ignore[call-arg]
            total_jobs=0,
            total_estimated_cost_cents=0,
            total_actual_cost_cents=0,
            jobs_with_unknown_cost=0,
            avg_cost_cents=0,
            generated_at_ns=1,
            unknown="x",
        )


def test_queue_metrics_frozen_and_extra_forbid() -> None:
    m = QueueMetrics(
        queue_depth=0,
        avg_wait_seconds=0.0,
        max_wait_seconds=0.0,
        dispatch_rate_per_hour=0.0,
        computed_at_ns=1,
    )
    assert isinstance(m, QueueMetrics)
    with pytest.raises(Exception):
        m.queue_depth = 5  # type: ignore[misc]
    with pytest.raises(Exception):
        QueueMetrics(  # type: ignore[call-arg]
            queue_depth=0,
            avg_wait_seconds=0.0,
            max_wait_seconds=0.0,
            dispatch_rate_per_hour=0.0,
            computed_at_ns=1,
            unknown="x",
        )


# --- infer_gpu_type -------------------------------------------------------- #


@pytest.mark.parametrize(
    "endpoint_id, expected",
    [
        ("rtx4090-training", GPUType.NVIDIA_RTX_4090.value),
        ("RTX4090-training", GPUType.NVIDIA_RTX_4090.value),
        ("a100-prod", GPUType.NVIDIA_A100.value),
        ("A100-prod", GPUType.NVIDIA_A100.value),
        ("h100-box", GPUType.NVIDIA_H100.value),
        ("rtx3090-node", GPUType.NVIDIA_RTX_3090.value),
        ("t4-edge", GPUType.NVIDIA_T4.value),
        ("T4-edge", GPUType.NVIDIA_T4.value),
        ("some-random-endpoint", GPUType.UNKNOWN.value),
        ("", GPUType.UNKNOWN.value),
        (None, GPUType.UNKNOWN.value),
    ],
)
def test_infer_gpu_type_matches_known_and_unknown(endpoint_id: str | None, expected: str) -> None:
    assert infer_gpu_type(endpoint_id) == expected


def test_infer_gpu_type_is_case_insensitive() -> None:
    assert infer_gpu_type("RtX4090") == GPUType.NVIDIA_RTX_4090.value
    assert infer_gpu_type("H100") == GPUType.NVIDIA_H100.value


# --- QueueTelemetry.compute_queue_metrics --------------------------------- #


def _advance_history(outbox, job_id: str, statuses, start_ns: int, step_ns: int):
    """Helper: drive an outbox job through a sequence of statuses with
    controlled timestamps injected into history entries. The initial
    QUEUED entry's timestamp is also reset to ``start_ns`` so waits are
    deterministic."""
    rec = outbox.get(job_id)
    assert rec is not None
    # Rewrite history with controlled timestamps: first entry (QUEUED)
    # at start_ns, subsequent entries at start_ns + k*step_ns.
    history = []
    queued_ts = rec.history[0]["ts_ns"] if rec.history else start_ns
    history.append({"status": "queued", "ts_ns": start_ns})
    ts = start_ns
    for st in statuses:
        ts += step_ns
        history.append({"status": st.value, "ts_ns": ts})
    updated = rec.model_copy(
        update={"status": statuses[-1], "history": history, "created_at_ns": start_ns}
    )
    outbox._records[job_id] = updated
    outbox._append(updated)
    return updated


def test_compute_queue_metrics_depth_and_waits(tmp_path: pathlib.Path) -> None:
    from quant_foundry.outbox import JobOutbox, JobStatus

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    outbox.enqueue(
        job_id="q1",
        job_type="training",
        idempotency_key="i1",
        request_payload={"x": 1},
    )
    outbox.enqueue(
        job_id="q2",
        job_type="training",
        idempotency_key="i2",
        request_payload={"x": 2},
    )
    outbox.enqueue(
        job_id="q3",
        job_type="training",
        idempotency_key="i3",
        request_payload={"x": 3},
    )
    base = time.time_ns()
    # q1 + q2 dispatched after 30s and 60s waits; q3 stays queued.
    _advance_history(
        outbox, "q1", [JobStatus.DISPATCHING, JobStatus.DISPATCHED], base, 30_000_000_000
    )
    _advance_history(
        outbox, "q2", [JobStatus.DISPATCHING, JobStatus.DISPATCHED], base, 60_000_000_000
    )
    qt = QueueTelemetry()
    metrics = qt.compute_queue_metrics(outbox)
    assert isinstance(metrics, QueueMetrics)
    assert metrics.queue_depth == 1  # only q3 still queued
    assert metrics.avg_wait_seconds == pytest.approx(45.0, rel=1e-6)
    assert metrics.max_wait_seconds == pytest.approx(60.0, rel=1e-6)
    assert metrics.dispatch_rate_per_hour >= 0.0


def test_compute_queue_metrics_empty_outbox(tmp_path: pathlib.Path) -> None:
    from quant_foundry.outbox import JobOutbox

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    qt = QueueTelemetry()
    metrics = qt.compute_queue_metrics(outbox)
    assert metrics.queue_depth == 0
    assert metrics.avg_wait_seconds == 0.0
    assert metrics.max_wait_seconds == 0.0
    assert metrics.dispatch_rate_per_hour == 0.0


def test_compute_queue_metrics_dispatch_rate(tmp_path: pathlib.Path) -> None:
    from quant_foundry.outbox import JobOutbox, JobStatus

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    outbox.enqueue(
        job_id="d1",
        job_type="training",
        idempotency_key="i1",
        request_payload={"x": 1},
    )
    base = time.time_ns()
    # Dispatched ~1 hour (3600s) after queued (single DISPATCHED entry so
    # last_dispatched_ns == queued + 3600s and the span is exactly 1hr).
    _advance_history(outbox, "d1", [JobStatus.DISPATCHED], base, 3_600_000_000_000)
    qt = QueueTelemetry()
    metrics = qt.compute_queue_metrics(outbox)
    # 1 dispatch over ~1 hour span -> rate near 1/hr.
    assert metrics.dispatch_rate_per_hour == pytest.approx(1.0, rel=0.2)


# --- runpod_client integration: telemetry recording during dispatch ------- #


def test_dispatcher_with_telemetry_records_cost_and_timing(
    tmp_path: pathlib.Path,
) -> None:
    """A successful dispatch with telemetry enabled records a
    JobTelemetryRecord with QUEUE + TRAIN + TOTAL phases and a cost
    estimate (GPU inferred from the endpoint id)."""
    from quant_foundry.outbox import JobOutbox
    from quant_foundry.runpod_client import (
        BudgetGuard,
        MockRunPodClient,
        RunPodDispatcher,
    )

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    telemetry = CostTelemetry(base_dir=tmp_path / "telemetry")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=MockRunPodClient(api_key="k", cost_per_dispatch_cents=25),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
        endpoint_id="a100-training",
        telemetry=telemetry,
    )
    outbox.enqueue(
        job_id="qf:train:tel:1",
        job_type="training",
        idempotency_key="idem-tel",
        request_payload={"job_id": "qf:train:tel:1"},
    )
    dispatcher.dispatch("qf:train:tel:1", request_payload={"job_id": "qf:train:tel:1"})
    rec = telemetry.get_job_cost("qf:train:tel:1")
    assert rec is not None
    # GPU inferred from endpoint id "a100-training".
    assert rec.gpu_type == GPUType.NVIDIA_A100.value
    assert rec.gpu_hourly_cost_cents == GPU_PRICING[GPUType.NVIDIA_A100.value]
    assert rec.cost_unknown is False
    assert rec.estimated_cost_cents is not None and rec.estimated_cost_cents > 0
    # actual cost from the mock client (25 cents).
    assert rec.actual_cost_cents == 25
    phases = {p.phase for p in rec.phases}
    assert TimingPhase.QUEUE in phases
    assert TimingPhase.TRAIN in phases
    assert TimingPhase.TOTAL in phases


def test_dispatcher_with_telemetry_unknown_gpu_marks_cost_unknown(
    tmp_path: pathlib.Path,
) -> None:
    """When the endpoint id does not map to a known GPU, the telemetry
    record has cost_unknown=True and estimated_cost_cents=None (never 0)."""
    from quant_foundry.outbox import JobOutbox
    from quant_foundry.runpod_client import (
        BudgetGuard,
        MockRunPodClient,
        RunPodDispatcher,
    )

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    telemetry = CostTelemetry(base_dir=tmp_path / "telemetry")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=MockRunPodClient(api_key="k", cost_per_dispatch_cents=25),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
        endpoint_id="mystery-gpu",
        telemetry=telemetry,
    )
    outbox.enqueue(
        job_id="qf:train:telu:1",
        job_type="training",
        idempotency_key="idem-telu",
        request_payload={"job_id": "qf:train:telu:1"},
    )
    dispatcher.dispatch("qf:train:telu:1", request_payload={"job_id": "qf:train:telu:1"})
    rec = telemetry.get_job_cost("qf:train:telu:1")
    assert rec is not None
    assert rec.gpu_type == GPUType.UNKNOWN.value
    assert rec.cost_unknown is True
    assert rec.estimated_cost_cents is None
    # actual cost still recorded from the callback.
    assert rec.actual_cost_cents == 25


def test_dispatcher_without_telemetry_is_backward_compatible(
    tmp_path: pathlib.Path,
) -> None:
    """When no telemetry is provided, dispatch works and no telemetry
    file is created."""
    from quant_foundry.outbox import JobOutbox
    from quant_foundry.runpod_client import (
        BudgetGuard,
        DispatchStatus,
        MockRunPodClient,
        RunPodDispatcher,
    )

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=MockRunPodClient(api_key="k"),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
        # telemetry=None (default)
    )
    outbox.enqueue(
        job_id="qf:train:notel:1",
        job_type="training",
        idempotency_key="idem-notel",
        request_payload={"job_id": "qf:train:notel:1"},
    )
    result = dispatcher.dispatch("qf:train:notel:1", request_payload={"job_id": "qf:train:notel:1"})
    assert result.status == DispatchStatus.DISPATCHED
    assert not (tmp_path / "telemetry" / "cost_telemetry.jsonl").is_file()
