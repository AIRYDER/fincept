"""
quant_foundry.runpod_client — RunPod dispatch client (TASK-0502).

This is the ONLY component in Fincept that talks to RunPod. It reads jobs
from the outbox (TASK-0304) and calls RunPod's API. Behind a config flag
(`QUANT_FOUNDRY_MODE=runpod`); when disabled, the mock dispatcher
(TASK-0305) is used instead.

Critical invariants (enforced + tested):
- No RunPod call happens unless explicitly enabled (mode == "runpod").
- Failed RunPod calls leave retryable jobs (transient) or fail them
  (terminal). Transient errors include spot preemption (retryable, not
  terminal).
- Rate limits enforced (max_dispatches_per_sweep).
- Per-job budget enforced (refuse over-budget).
- Global monthly GPU budget ceiling fails closed: dispatch is refused
  once the monthly cap is hit, with a clear receipt (not a silent drop).
- API key is never returned in results, logs, or outbox records.
- RunPod job ID stored in outbox.
- Actual cost + duration recorded in outbox history.

The RunPodClient protocol allows swapping MockRunPodClient (tests) for
HttpRunPodClient (production) without changing the dispatcher.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from quant_foundry.outbox import JobOutbox, JobStatus

# --- dispatch status -------------------------------------------------------


class DispatchStatus(StrEnum):
    """Outcome of a dispatch attempt."""

    DISPATCHED = "dispatched"  # successfully sent to RunPod
    SKIPPED = "skipped"  # not sent (mode != runpod, or rate-limited)
    TRANSIENT_FAILURE = "transient_failure"  # retryable (503, spot preemption)
    TERMINAL_FAILURE = "terminal_failure"  # not retryable (bad request)
    BUDGET_EXCEEDED = "budget_exceeded"  # per-job or global ceiling hit


# --- dispatch result -------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Result of a single dispatch attempt."""

    job_id: str
    status: DispatchStatus
    runpod_job_id: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    cost_cents: int = 0
    duration_seconds: float = 0.0

    def model_dump(self) -> dict[str, Any]:
        """Dict representation (excludes API key — never stored here)."""
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "runpod_job_id": self.runpod_job_id,
            "error_code": self.error_code,
            "error_summary": self.error_summary,
            "cost_cents": self.cost_cents,
            "duration_seconds": self.duration_seconds,
        }


# --- RunPodClient protocol -------------------------------------------------


@runtime_checkable
class RunPodClient(Protocol):
    """Interface for talking to RunPod. MockRunPodClient for tests,
    HttpRunPodClient for production."""

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:
        """Dispatch a job to RunPod. Returns a DispatchResult."""
        ...


# --- budget guard ----------------------------------------------------------


@dataclass
class BudgetGuard:
    """Per-job + global monthly GPU budget ceiling. Fails closed.

    The monthly ceiling is a hard kill switch: once the cumulative cost
    for the current month exceeds `monthly_budget_cents`, all further
    dispatches are refused with a clear receipt (not a silent drop).

    Args:
        monthly_budget_cents: global monthly GPU budget ceiling (hard cap).
        per_job_budget_cents: per-job budget limit (refuse over-budget).
        spent_this_month_cents: cumulative cost already spent this month
            (for resume-after-restart; loaded from outbox history).
    """

    monthly_budget_cents: int
    per_job_budget_cents: int | None = None
    spent_this_month_cents: int = 0

    def check_per_job(self, budget_cents: int | None) -> tuple[bool, str | None]:
        """Check per-job budget. Returns (ok, error_code)."""
        if (
            self.per_job_budget_cents is not None
            and budget_cents is not None
            and budget_cents > self.per_job_budget_cents
        ):
            return False, "per_job_budget_exceeded"
        return True, None

    def check_global(self, prospective_cost_cents: int) -> tuple[bool, str | None]:
        """Check global monthly ceiling. Returns (ok, error_code)."""
        if self.spent_this_month_cents + prospective_cost_cents > self.monthly_budget_cents:
            return False, "global_monthly_budget_exceeded"
        return True, None

    def record_spend(self, cost_cents: int) -> None:
        """Record an actual spend against the monthly budget."""
        self.spent_this_month_cents += cost_cents


# --- mock RunPod client (for tests) ----------------------------------------


@dataclass
class MockRunPodClient:
    """In-process mock RunPod client. No HTTP. Deterministic for tests.

    Args:
        api_key: RunPod API key (NEVER returned in results or logs).
        cost_per_dispatch_cents: simulated cost per successful dispatch.
        duration_per_dispatch_seconds: simulated duration per dispatch.
    """

    api_key: str
    cost_per_dispatch_cents: int = 25
    duration_per_dispatch_seconds: float = 1.0
    _dispatch_count: int = field(default=0, init=False)

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:
        self._dispatch_count += 1
        runpod_job_id = f"rp-job-{self._dispatch_count:08d}"
        return DispatchResult(
            job_id=job_id,
            status=DispatchStatus.DISPATCHED,
            runpod_job_id=runpod_job_id,
            cost_cents=self.cost_per_dispatch_cents,
            duration_seconds=self.duration_per_dispatch_seconds,
        )


# --- HTTP RunPod client (production) ---------------------------------------


class HttpRunPodClient:
    """Real HTTP RunPod client. Uses httpx. Behind config flag.

    The API key is read from the constructor (server-side only) and NEVER
    returned in results, logs, or outbox records.

    NOTE: Full HTTP implementation is deferred until RunPod credentials
    are available. The class is defined here so the dispatcher can be
    wired with it via config; the actual HTTP calls will be added when
    TASK-0502 is exercised against a real RunPod endpoint.
    """

    def __init__(self, *, api_key: str, endpoint_id: str, base_url: str) -> None:
        self._api_key = api_key  # private, never exposed
        self._endpoint_id = endpoint_id
        self._base_url = base_url

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:  # pragma: no cover — HTTP path deferred
        raise NotImplementedError(
            "HttpRunPodClient.dispatch is not yet implemented; "
            "use MockRunPodClient for tests or set QUANT_FOUNDRY_MODE=local_mock."
        )


# --- dispatcher ------------------------------------------------------------


@dataclass
class RunPodDispatcher:
    """Reads jobs from the outbox and dispatches them to RunPod.

    The ONLY component in Fincept that talks to RunPod. Behind a config
    flag (mode == "runpod"); when disabled, dispatch() returns SKIPPED
    and no RunPod call is made.

    Args:
        outbox: JobOutbox (TASK-0304) — source of jobs.
        client: RunPodClient (Mock or Http) — the RunPod API client.
        mode: "runpod" or "local_mock". When != "runpod", no calls.
        budget_guard: BudgetGuard — per-job + global monthly ceiling.
        max_dispatches_per_sweep: rate limit (max dispatches per sweep).
    """

    outbox: JobOutbox
    client: RunPodClient
    mode: str = "local_mock"
    budget_guard: BudgetGuard = field(default_factory=lambda: BudgetGuard(monthly_budget_cents=0))
    max_dispatches_per_sweep: int | None = None

    def dispatch(
        self, job_id: str, *, request_payload: dict[str, Any],
    ) -> DispatchResult:
        """Dispatch a single job to RunPod.

        Returns a DispatchResult. Does NOT raise on RunPod failures
        (they're classified into transient/terminal/budget_exceeded).
        """
        if self.mode != "runpod":
            return DispatchResult(
                job_id=job_id, status=DispatchStatus.SKIPPED,
                error_code="mode_not_runpod",
                error_summary=f"QUANT_FOUNDRY_MODE={self.mode}; no RunPod call",
            )

        rec = self.outbox.get(job_id)
        if rec is None:
            return DispatchResult(
                job_id=job_id, status=DispatchStatus.TERMINAL_FAILURE,
                error_code="unknown_job",
                error_summary=f"no outbox record for job_id {job_id}",
            )

        # Per-job budget check.
        ok, err = self.budget_guard.check_per_job(rec.budget_cents)
        if not ok:
            return DispatchResult(
                job_id=job_id, status=DispatchStatus.BUDGET_EXCEEDED,
                error_code=err,
                error_summary=(
                    f"per-job budget exceeded: job budget "
                    f"{rec.budget_cents}c > limit "
                    f"{self.budget_guard.per_job_budget_cents}c"
                ),
            )

        # Global monthly budget check (prospective cost = per-dispatch
        # cost from the client; we don't know the real cost until after
        # the job runs, so we use a conservative estimate).
        # For the mock client, cost_per_dispatch_cents is known upfront.
        prospective_cost = getattr(self.client, "cost_per_dispatch_cents", 0)
        ok, err = self.budget_guard.check_global(prospective_cost)
        if not ok:
            return DispatchResult(
                job_id=job_id, status=DispatchStatus.BUDGET_EXCEEDED,
                error_code=err,
                error_summary=(
                    f"global monthly budget ceiling exceeded: spent "
                    f"{self.budget_guard.spent_this_month_cents}c + "
                    f"prospective {prospective_cost}c > cap "
                    f"{self.budget_guard.monthly_budget_cents}c"
                ),
            )

        # Drive outbox transitions.
        self.outbox.update_status(job_id, JobStatus.DISPATCHING)
        self.outbox.update_status(job_id, JobStatus.DISPATCHED)

        # Call the RunPod client.
        start = time.time()
        result = self.client.dispatch(
            job_id=job_id,
            request_payload=request_payload,
            budget_cents=rec.budget_cents,
        )
        elapsed = time.time() - start

        # Record spend against the global budget.
        if result.cost_cents > 0:
            self.budget_guard.record_spend(result.cost_cents)

        # Store RunPod job ID + cost + duration in outbox.
        if result.runpod_job_id:
            note = json.dumps({
                "cost_cents": result.cost_cents,
                "duration_seconds": result.duration_seconds or elapsed,
            })
            self.outbox.update_status(
                job_id, JobStatus.RUNNING,
                runpod_job_id=result.runpod_job_id,
                note=note,
            )

        # Classify transient vs terminal.
        if result.status == DispatchStatus.TRANSIENT_FAILURE:
            # Leave retryable: update to a non-terminal status with error.
            self.outbox.update_status(
                job_id, JobStatus.QUEUED,  # back to queued for retry
                error_code=result.error_code,
                error_summary=result.error_summary,
                note="transient failure; queued for retry",
            )
        elif result.status == DispatchStatus.TERMINAL_FAILURE:
            self.outbox.update_status(
                job_id, JobStatus.FAILED,
                error_code=result.error_code,
                error_summary=result.error_summary,
            )

        return result

    def dispatch_sweep(self) -> list[DispatchResult]:
        """Dispatch all QUEUED jobs, up to max_dispatches_per_sweep.

        Returns a list of DispatchResult, one per job considered.
        """
        queued = [
            rec for rec in self.outbox.list()
            if rec.status == JobStatus.QUEUED
        ]
        # Sort by priority (desc) then created_at (asc).
        queued.sort(key=lambda r: (-r.priority, r.created_at_ns))

        results: list[DispatchResult] = []
        dispatched = 0
        for rec in queued:
            if (
                self.max_dispatches_per_sweep is not None
                and dispatched >= self.max_dispatches_per_sweep
            ):
                results.append(DispatchResult(
                    job_id=rec.job_id, status=DispatchStatus.SKIPPED,
                    error_code="rate_limited",
                    error_summary="max_dispatches_per_sweep reached",
                ))
                continue

            # Parse the request payload from the outbox record.
            # For MVP, we pass the raw payload hash; the real dispatcher
            # would read the payload from request_payload_ref.
            result = self.dispatch(
                rec.job_id,
                request_payload={"job_id": rec.job_id},
            )
            if result.status == DispatchStatus.DISPATCHED:
                dispatched += 1
            results.append(result)
        return results
