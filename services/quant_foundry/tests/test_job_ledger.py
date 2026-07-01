"""
TDD tests for quant_foundry.job_ledger (Phase 6 / T-6.1).

Acceptance:
- Append-only job ledger connecting outbox_id, runpod_job_id, dataset_id,
  artifact_id, callbacks, failures, retries, cost.
- State transitions: queued, dispatched, runpod_running, callback_received,
  artifact_verified, rejected, failed, expired.
- Dispatch creates ledger row; callback updates row; artifact verification
  updates row.
- One job can be traced end to end without reading logs.
- Append-only JSONL storage (survives restart); fsync after writes.
- Pydantic v2 frozen + extra=forbid; StrEnum for state.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from quant_foundry.job_ledger import (
    JobLedgerRecord,
    JobLedgerState,
    TrainingJobLedger,
)


# --- module imports / types ------------------------------------------------


def test_job_ledger_module_imports_and_types() -> None:
    assert callable(TrainingJobLedger)
    assert JobLedgerState.QUEUED
    assert issubclass(JobLedgerRecord, object)
    # StrEnum: values are strings
    assert JobLedgerState.QUEUED.value == "queued"
    assert JobLedgerState.ARTIFACT_VERIFIED.value == "artifact_verified"
    assert JobLedgerState.REJECTED.value == "rejected"
    assert JobLedgerState.EXPIRED.value == "expired"
    assert JobLedgerState.FAILED.value == "failed"
    assert JobLedgerState.RUNPOD_RUNNING.value == "runpod_running"
    assert JobLedgerState.CALLBACK_RECEIVED.value == "callback_received"
    assert JobLedgerState.DISPATCHED.value == "dispatched"


def test_job_ledger_record_frozen_and_extra_forbid() -> None:
    rec = JobLedgerRecord(
        ledger_id="l1",
        outbox_id="o1",
        created_at_ns=1,
        updated_at_ns=1,
    )
    # frozen: mutation raises
    with pytest.raises(Exception):
        rec.cost_cents = 5  # type: ignore[misc]
    # extra=forbid: unknown field raises
    with pytest.raises(Exception):
        JobLedgerRecord(  # type: ignore[call-arg]
            ledger_id="l2",
            outbox_id="o2",
            created_at_ns=1,
            updated_at_ns=1,
            unknown_field="x",
        )


# --- create_row -----------------------------------------------------------


def test_create_row_makes_queued_record_with_history(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    rec = ledger.create_row("job-1", dataset_id="ds-1")
    assert isinstance(rec, JobLedgerRecord)
    assert rec.ledger_id == "job-1"  # defaults to outbox_id
    assert rec.outbox_id == "job-1"
    assert rec.dataset_id == "ds-1"
    assert rec.state == JobLedgerState.QUEUED
    assert rec.runpod_job_id is None
    assert rec.artifact_id is None
    assert rec.callbacks == ()
    assert rec.failures == ()
    assert rec.retries == 0
    assert rec.cost_cents == 0
    assert len(rec.history) == 1
    assert rec.history[0]["state"] == "queued"
    assert rec.created_at_ns > 0
    assert rec.updated_at_ns >= rec.created_at_ns


def test_create_row_rejects_empty_outbox_id(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    with pytest.raises(ValueError):
        ledger.create_row("")


def test_create_row_rejects_duplicate_ledger_id(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    with pytest.raises(ValueError, match="already exists"):
        ledger.create_row("job-1")


# --- state transitions -----------------------------------------------------


def test_update_state_transitions_and_appends_history(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    r1 = ledger.update_state("job-1", JobLedgerState.DISPATCHED, runpod_job_id="rp-1")
    assert r1.state == JobLedgerState.DISPATCHED
    assert r1.runpod_job_id == "rp-1"
    assert len(r1.history) == 2
    r2 = ledger.update_state("job-1", JobLedgerState.RUNPOD_RUNNING)
    assert r2.state == JobLedgerState.RUNPOD_RUNNING
    assert len(r2.history) == 3
    states = [h["state"] for h in r2.history]
    assert states == ["queued", "dispatched", "runpod_running"]


def test_update_state_unknown_ledger_id_raises(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    with pytest.raises(KeyError):
        ledger.update_state("nope", JobLedgerState.DISPATCHED)


# --- record_callback -------------------------------------------------------


def test_record_callback_appends_and_transitions(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    r = ledger.record_callback("job-1", "cb:job-1:1")
    assert r.state == JobLedgerState.CALLBACK_RECEIVED
    assert r.callbacks == ("cb:job-1:1",)
    assert len(r.history) == 2


def test_record_callback_idempotent_on_duplicate(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    ledger.record_callback("job-1", "cb:1")
    r = ledger.record_callback("job-1", "cb:1")  # duplicate
    assert r.callbacks == ("cb:1",)  # not re-appended
    assert r.history[-1]["duplicate"] is True


def test_record_callback_rejects_empty(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    with pytest.raises(ValueError):
        ledger.record_callback("job-1", "")


# --- record_failure --------------------------------------------------------


def test_record_failure_appends_and_increments_retries(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    r = ledger.record_failure("job-1", "runpod_503", "transient 503")
    assert r.retries == 1
    assert len(r.failures) == 1
    assert r.failures[0]["error_code"] == "runpod_503"
    assert r.failures[0]["error_message"] == "transient 503"
    # state unchanged by record_failure alone
    assert r.state == JobLedgerState.QUEUED
    r2 = ledger.record_failure("job-1", "runpod_503", "again")
    assert r2.retries == 2
    assert len(r2.failures) == 2


def test_record_failure_rejects_empty_code(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    with pytest.raises(ValueError):
        ledger.record_failure("job-1", "", "msg")


# --- record_cost -----------------------------------------------------------


def test_record_cost_accumulates(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    r = ledger.record_cost("job-1", 25, 1.5)
    assert r.cost_cents == 25
    assert r.duration_seconds == 1.5
    r2 = ledger.record_cost("job-1", 10, 0.5)
    assert r2.cost_cents == 35
    assert r2.duration_seconds == 2.0


def test_record_cost_rejects_negative(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    with pytest.raises(ValueError):
        ledger.record_cost("job-1", -1, 0.0)


# --- record_artifact -------------------------------------------------------


def test_record_artifact_sets_artifact_id_and_terminal_state(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    r = ledger.record_artifact("job-1", "art:sha256:abc")
    assert r.artifact_id == "art:sha256:abc"
    assert r.state == JobLedgerState.ARTIFACT_VERIFIED


def test_record_artifact_rejects_empty(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    with pytest.raises(ValueError):
        ledger.record_artifact("job-1", "")


# --- durability / restart --------------------------------------------------


def test_ledger_survives_restart(tmp_path: pathlib.Path) -> None:
    ledger1 = TrainingJobLedger(base_dir=tmp_path)
    ledger1.create_row("job-1", dataset_id="ds-1")
    ledger1.update_state("job-1", JobLedgerState.DISPATCHED, runpod_job_id="rp-1")
    ledger2 = TrainingJobLedger(base_dir=tmp_path)
    rec = ledger2.get("job-1")
    assert rec is not None
    assert rec.state == JobLedgerState.DISPATCHED
    assert rec.runpod_job_id == "rp-1"
    assert rec.dataset_id == "ds-1"
    assert (tmp_path / "job_ledger.jsonl").is_file()


def test_ledger_jsonl_is_append_only(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    ledger.update_state("job-1", JobLedgerState.DISPATCHED)
    ledger.record_callback("job-1", "cb:1")
    # 3 writes => 3 lines
    with (tmp_path / "job_ledger.jsonl").open("r", encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    assert len(lines) == 3
    for line in lines:
        data = json.loads(line)
        assert data["ledger_id"] == "job-1"


# --- list / get_by_outbox_id / trace ---------------------------------------


def test_list_filters_by_state_and_limit(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("j1")
    ledger.create_row("j2")
    ledger.create_row("j3")
    ledger.update_state("j1", JobLedgerState.DISPATCHED)
    all_rows = ledger.list()
    assert len(all_rows) == 3
    dispatched = ledger.list(state=JobLedgerState.DISPATCHED)
    assert len(dispatched) == 1
    assert dispatched[0].ledger_id == "j1"
    limited = ledger.list(limit=2)
    assert len(limited) == 2


def test_get_by_outbox_id(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1")
    rec = ledger.get_by_outbox_id("job-1")
    assert rec is not None
    assert rec.outbox_id == "job-1"
    assert ledger.get_by_outbox_id("nope") is None


def test_trace_returns_end_to_end_view(tmp_path: pathlib.Path) -> None:
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("job-1", dataset_id="ds-1")
    ledger.update_state("job-1", JobLedgerState.DISPATCHED, runpod_job_id="rp-1")
    ledger.update_state("job-1", JobLedgerState.RUNPOD_RUNNING)
    ledger.record_callback("job-1", "cb:1")
    ledger.record_cost("job-1", 25, 1.5)
    ledger.record_artifact("job-1", "art:sha256:abc")
    trace = ledger.trace("job-1")
    assert trace is not None
    assert trace["ledger_id"] == "job-1"
    assert trace["outbox_id"] == "job-1"
    assert trace["runpod_job_id"] == "rp-1"
    assert trace["dataset_id"] == "ds-1"
    assert trace["artifact_id"] == "art:sha256:abc"
    assert trace["state"] == "artifact_verified"
    assert trace["cost_cents"] == 25
    assert trace["duration_seconds"] == 1.5
    assert trace["callbacks"] == ("cb:1",)
    assert len(trace["failures"]) == 0
    assert trace["retries"] == 0
    assert trace["terminal"] is True
    # trajectory shows the state trajectory
    traj_states = [t["state"] for t in trace["trajectory"]]
    assert traj_states[0] == "queued"
    assert "artifact_verified" in traj_states
    assert ledger.trace("nope") is None


# --- end-to-end: one job traced without reading logs -----------------------


def test_end_to_end_one_job_traced_without_logs(tmp_path: pathlib.Path) -> None:
    """A single job goes queued -> dispatched -> runpod_running ->
    callback_received -> artifact_verified, and the trace shows the
    full picture without reading any log file."""
    ledger = TrainingJobLedger(base_dir=tmp_path)
    ledger.create_row("qf:train:ds1:1", dataset_id="ds-1")
    ledger.update_state("qf:train:ds1:1", JobLedgerState.DISPATCHED, runpod_job_id="rp-99")
    ledger.update_state("qf:train:ds1:1", JobLedgerState.RUNPOD_RUNNING)
    ledger.record_cost("qf:train:ds1:1", 25, 1.0)
    ledger.record_callback("qf:train:ds1:1", "cb:qf:train:ds1:1:1")
    ledger.record_artifact("qf:train:ds1:1", "art:sha256:deadbeef")
    trace = ledger.trace("qf:train:ds1:1")
    assert trace is not None
    # All the linked identifiers are present in one place.
    assert trace["outbox_id"] == "qf:train:ds1:1"
    assert trace["runpod_job_id"] == "rp-99"
    assert trace["dataset_id"] == "ds-1"
    assert trace["artifact_id"] == "art:sha256:deadbeef"
    assert trace["callbacks"] == ("cb:qf:train:ds1:1:1",)
    assert trace["state"] == "artifact_verified"
    assert trace["cost_cents"] == 25
    assert trace["terminal"] is True


# --- dispatcher integration (runpod_client + ledger) ----------------------


def test_dispatcher_with_ledger_records_dispatch_success(tmp_path: pathlib.Path) -> None:
    """A successful dispatch creates + advances a ledger row through
    DISPATCHED -> RUNPOD_RUNNING and records cost."""
    from quant_foundry.outbox import JobOutbox
    from quant_foundry.runpod_client import (
        BudgetGuard,
        MockRunPodClient,
        RunPodDispatcher,
    )

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    ledger = TrainingJobLedger(base_dir=tmp_path / "ledger")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=MockRunPodClient(api_key="k", cost_per_dispatch_cents=25),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
        ledger=ledger,
    )
    outbox.enqueue(
        job_id="qf:train:led:1",
        job_type="training",
        idempotency_key="idem-1",
        request_payload={"job_id": "qf:train:led:1"},
    )
    dispatcher.dispatch("qf:train:led:1", request_payload={"job_id": "qf:train:led:1"})
    rec = ledger.get_by_outbox_id("qf:train:led:1")
    assert rec is not None
    assert rec.state == JobLedgerState.RUNPOD_RUNNING
    assert rec.runpod_job_id is not None
    assert rec.cost_cents == 25
    # trajectory includes queued -> dispatched -> runpod_running (cost
    # record appends an additional runpod_running history entry)
    states = [h["state"] for h in rec.history]
    assert states[0] == "queued"
    assert states[1] == "dispatched"
    assert states[-1] == "runpod_running"
    assert "runpod_running" in states


def test_dispatcher_with_ledger_records_transient_failure_and_retry(tmp_path: pathlib.Path) -> None:
    """A transient failure records a failure, increments retries, and
    leaves the ledger row in QUEUED (retryable)."""
    from quant_foundry.outbox import JobOutbox
    from quant_foundry.runpod_client import (
        BudgetGuard,
        DispatchResult,
        DispatchStatus,
        MockRunPodClient,
        RunPodDispatcher,
    )

    class TransientFail(MockRunPodClient):
        def dispatch(self, **kwargs):  # type: ignore[override]
            return DispatchResult(
                job_id=kwargs.get("job_id", ""),
                status=DispatchStatus.TRANSIENT_FAILURE,
                error_code="runpod_503",
                error_summary="503",
            )

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    ledger = TrainingJobLedger(base_dir=tmp_path / "ledger")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=TransientFail(api_key="k"),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
        ledger=ledger,
    )
    outbox.enqueue(
        job_id="qf:train:led:trans:1",
        job_type="training",
        idempotency_key="idem-t",
        request_payload={"job_id": "qf:train:led:trans:1"},
    )
    dispatcher.dispatch(
        "qf:train:led:trans:1",
        request_payload={"job_id": "qf:train:led:trans:1"},
    )
    rec = ledger.get_by_outbox_id("qf:train:led:trans:1")
    assert rec is not None
    assert rec.state == JobLedgerState.QUEUED
    assert rec.retries == 1
    assert len(rec.failures) == 1
    assert rec.failures[0]["error_code"] == "runpod_503"


def test_dispatcher_with_ledger_records_terminal_failure(tmp_path: pathlib.Path) -> None:
    """A terminal failure records a failure and transitions to FAILED."""
    from quant_foundry.outbox import JobOutbox
    from quant_foundry.runpod_client import (
        BudgetGuard,
        DispatchResult,
        DispatchStatus,
        MockRunPodClient,
        RunPodDispatcher,
    )

    class TerminalFail(MockRunPodClient):
        def dispatch(self, **kwargs):  # type: ignore[override]
            return DispatchResult(
                job_id=kwargs.get("job_id", ""),
                status=DispatchStatus.TERMINAL_FAILURE,
                error_code="bad_request",
                error_summary="rejected",
            )

    outbox = JobOutbox(base_dir=tmp_path / "outbox")
    ledger = TrainingJobLedger(base_dir=tmp_path / "ledger")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=TerminalFail(api_key="k"),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
        ledger=ledger,
    )
    outbox.enqueue(
        job_id="qf:train:led:term:1",
        job_type="training",
        idempotency_key="idem-term",
        request_payload={"job_id": "qf:train:led:term:1"},
    )
    dispatcher.dispatch(
        "qf:train:led:term:1",
        request_payload={"job_id": "qf:train:led:term:1"},
    )
    rec = ledger.get_by_outbox_id("qf:train:led:term:1")
    assert rec is not None
    assert rec.state == JobLedgerState.FAILED
    assert rec.retries == 1
    assert len(rec.failures) == 1


def test_dispatcher_without_ledger_is_backward_compatible(tmp_path: pathlib.Path) -> None:
    """When no ledger is provided, dispatch works exactly as before."""
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
        # ledger=None (default)
    )
    outbox.enqueue(
        job_id="qf:train:noleg:1",
        job_type="training",
        idempotency_key="idem-nl",
        request_payload={"job_id": "qf:train:noleg:1"},
    )
    result = dispatcher.dispatch(
        "qf:train:noleg:1",
        request_payload={"job_id": "qf:train:noleg:1"},
    )
    assert result.status == DispatchStatus.DISPATCHED
    # no ledger file created
    assert not (tmp_path / "ledger" / "job_ledger.jsonl").is_file()
