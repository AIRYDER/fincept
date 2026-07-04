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
from typing import Any, Protocol, cast, runtime_checkable

from quant_foundry.job_ledger import JobLedgerState, TrainingJobLedger
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.telemetry import CostTelemetry, PhaseTiming, TimingPhase, infer_gpu_type

# --- helpers --------------------------------------------------------------- #


def _compute_queue_wait_ns(rec: Any) -> float:
    """Compute queue wait time (seconds) from an outbox record's history.

    Reads the QUEUED -> DISPATCHING transition timestamps from the
    outbox history and returns the difference in seconds. Returns 0.0
    if the record is None or the transition is not found.
    """
    if rec is None:
        return 0.0
    queued_ts: int | None = None
    dispatched_ts: int | None = None
    for entry in rec.history:
        status = entry.get("status")
        ts = entry.get("ts_ns")
        if ts is None:
            continue
        if status == JobStatus.QUEUED.value and queued_ts is None:
            queued_ts = ts
        elif status == JobStatus.DISPATCHING.value and dispatched_ts is None:
            dispatched_ts = ts
    if queued_ts is not None and dispatched_ts is not None:
        wait = (dispatched_ts - queued_ts) / 1e9
        return max(wait, 0.0)
    return 0.0


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

    Uses RunPod's async ``/run`` endpoint: the job is submitted and the
    RunPod job ID is returned immediately. The actual result is retrieved
    by polling ``/status/{job_id}`` (see ``check_status``). The RunPod
    serverless worker embeds ``callback_payload``, ``callback_signature``,
    and ``callback_ts`` in the job output; the API extracts these signed
    callback fields from the polled status response and routes them
    through the same callback processing path
    (``gateway.receive_callback`` → ``verify_callback``).

    The ``POST /quant-foundry/callbacks/runpod`` endpoint is NOT used by
    normal RunPod serverless dispatch — RunPod does not push results to
    it. It exists only for operator-initiated manual callback submission
    or external webhook integrations. The polling path is the real
    production path.

    Error classification:
    - HTTP 429, 502, 503, 504 → TRANSIENT_FAILURE (retryable)
    - HTTP 400, 401, 403, 422 → TERMINAL_FAILURE (not retryable)
    - Network errors (connect/timeout) → TRANSIENT_FAILURE (retryable)
    - HTTP 200 → DISPATCHED (success; RunPod job ID in response)

    Args:
        api_key: RunPod API key (server-side only, never exposed).
        endpoint_id: RunPod serverless endpoint ID.
        base_url: RunPod API base URL (default: ``https://api.runpod.ai/v2``).
        timeout_seconds: HTTP request timeout for the dispatch call.
        cost_per_dispatch_cents: estimated cost per dispatch (for budget
            guard prospective cost check; actual cost is recorded when the
            callback returns).
        transport: optional httpx transport (for testing; production uses
            the default httpx transport).
    """

    def __init__(
        self,
        *,
        api_key: str,
        endpoint_id: str,
        base_url: str = "https://api.runpod.ai/v2",
        timeout_seconds: float = 30.0,
        cost_per_dispatch_cents: int = 0,
        transport: Any = None,
    ) -> None:
        self._api_key = api_key  # private, never exposed
        self._endpoint_id = endpoint_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self.cost_per_dispatch_cents = cost_per_dispatch_cents
        self._transport = transport

    @property
    def endpoint_id(self) -> str:
        return self._endpoint_id

    def _build_client(self) -> Any:
        """Build an httpx.Client. Uses injected transport for tests."""
        import httpx

        if self._transport is not None:
            return httpx.Client(transport=self._transport, timeout=self._timeout)
        return httpx.Client(timeout=self._timeout)

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:
        """Submit a job to RunPod's async ``/run`` endpoint.

        Returns a DispatchResult. On success, ``runpod_job_id`` is the
        RunPod-assigned job ID (from the ``id`` field of the response).
        Cost and duration are 0 at dispatch time; they are recorded when
        the callback returns.
        """
        url = f"{self._base_url}/{self._endpoint_id}/run"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps({"input": request_payload})

        try:
            client = self._build_client()
            with client as c:
                resp = c.post(url, headers=headers, content=body)
        except Exception as exc:
            # Network errors (connect failure, timeout, DNS) → transient.
            return DispatchResult(
                job_id=job_id,
                status=DispatchStatus.TRANSIENT_FAILURE,
                error_code="network_error",
                error_summary=f"RunPod HTTP request failed: {type(exc).__name__}: {exc}",
            )

        # Classify HTTP status.
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as exc:
                return DispatchResult(
                    job_id=job_id,
                    status=DispatchStatus.TERMINAL_FAILURE,
                    error_code="bad_response_body",
                    error_summary=f"RunPod returned 200 but body was not valid JSON: {exc}",
                )
            runpod_job_id = data.get("id")
            if not runpod_job_id:
                return DispatchResult(
                    job_id=job_id,
                    status=DispatchStatus.TERMINAL_FAILURE,
                    error_code="missing_job_id",
                    error_summary="RunPod response missing 'id' field",
                )
            return DispatchResult(
                job_id=job_id,
                status=DispatchStatus.DISPATCHED,
                runpod_job_id=str(runpod_job_id),
                cost_cents=0,  # actual cost recorded on callback
                duration_seconds=0.0,
            )

        # Transient: 429 (rate limit), 502/503/504 (upstream errors).
        if resp.status_code in (429, 502, 503, 504):
            return DispatchResult(
                job_id=job_id,
                status=DispatchStatus.TRANSIENT_FAILURE,
                error_code=f"http_{resp.status_code}",
                error_summary=f"RunPod returned HTTP {resp.status_code} (transient): {resp.text[:200]}",
            )

        # Terminal: all other 4xx/5xx.
        return DispatchResult(
            job_id=job_id,
            status=DispatchStatus.TERMINAL_FAILURE,
            error_code=f"http_{resp.status_code}",
            error_summary=f"RunPod returned HTTP {resp.status_code} (terminal): {resp.text[:200]}",
        )

    def check_status(self, runpod_job_id: str) -> dict[str, Any]:
        """Poll RunPod ``/status/{job_id}`` for job status.

        Returns the raw JSON response dict. Raises on network/HTTP errors.
        Useful for debugging or for a polling fallback if callbacks are
        not received.
        """
        url = f"{self._base_url}/{self._endpoint_id}/status/{runpod_job_id}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        client = self._build_client()
        with client as c:
            resp = c.get(url, headers=headers)
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())

    def check_health(self) -> dict[str, Any]:
        """Check RunPod endpoint health via ``/health``.

        Returns the raw JSON response dict.
        """
        url = f"{self._base_url}/{self._endpoint_id}/health"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        client = self._build_client()
        with client as c:
            resp = c.get(url, headers=headers)
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())


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
        ledger: optional TrainingJobLedger (Phase 6 / T-6.1) — when
            provided, the dispatcher records an audit trail of every
            state transition, failure, retry, and cost event for each
            job. When None, ledger tracking is disabled (backward
            compat). The ledger is read-only with respect to the outbox:
            it observes transitions but never drives them.
        telemetry: optional CostTelemetry (Phase 6 / T-6.3) — when
            provided, the dispatcher records per-job cost estimates (by
            GPU type + duration) and per-phase timing (queue, train).
            When None, telemetry tracking is disabled (backward compat).
            Missing GPU prices are recorded as cost_unknown=True (never
            silently zero).
    """

    outbox: JobOutbox
    client: RunPodClient
    mode: str = "local_mock"
    budget_guard: BudgetGuard = field(default_factory=lambda: BudgetGuard(monthly_budget_cents=0))
    max_dispatches_per_sweep: int | None = None
    endpoint_id: str | None = None
    ledger: TrainingJobLedger | None = None
    telemetry: CostTelemetry | None = None

    def dispatch(
        self,
        job_id: str,
        *,
        request_payload: dict[str, Any],
        gpu_type: str | None = None,
    ) -> DispatchResult:
        """Dispatch a single job to RunPod.

        Returns a DispatchResult. Does NOT raise on RunPod failures
        (they're classified into transient/terminal/budget_exceeded).

        Args:
            gpu_type: optional GPU type override (a ``GPUType`` value or
                raw string). When None, the GPU type is inferred from
                the endpoint id. Used for cost telemetry estimation;
                an unknown GPU type yields ``cost_unknown=True``.
        """
        if self.mode != "runpod":
            return DispatchResult(
                job_id=job_id,
                status=DispatchStatus.SKIPPED,
                error_code="mode_not_runpod",
                error_summary=f"QUANT_FOUNDRY_MODE={self.mode}; no RunPod call",
            )

        rec = self.outbox.get(job_id)
        if rec is None:
            return DispatchResult(
                job_id=job_id,
                status=DispatchStatus.TERMINAL_FAILURE,
                error_code="unknown_job",
                error_summary=f"no outbox record for job_id {job_id}",
            )

        # Per-job budget check.
        ok, err = self.budget_guard.check_per_job(rec.budget_cents)
        if not ok:
            return DispatchResult(
                job_id=job_id,
                status=DispatchStatus.BUDGET_EXCEEDED,
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
                job_id=job_id,
                status=DispatchStatus.BUDGET_EXCEEDED,
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

        # Ledger: ensure a QUEUED row exists (created on first dispatch;
        # on retries the row already exists so we reuse it). The ledger
        # observes transitions but never drives them.
        if self.ledger is not None:
            lrec = self.ledger.get_by_outbox_id(job_id)
            if lrec is None:
                self.ledger.create_row(job_id)
            self.ledger.update_state(job_id, JobLedgerState.DISPATCHED)

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
            endpoint_id = self.endpoint_id
            if endpoint_id is None:
                endpoint_id = getattr(self.client, "endpoint_id", None)
            if endpoint_id is None:
                endpoint_id = getattr(self.client, "_endpoint_id", None)
            note = json.dumps(
                {
                    "cost_cents": result.cost_cents,
                    "duration_seconds": result.duration_seconds or elapsed,
                }
            )
            self.outbox.update_status(
                job_id,
                JobStatus.RUNNING,
                runpod_endpoint_id=endpoint_id,
                runpod_job_id=result.runpod_job_id,
                note=note,
            )
            # Ledger: record runpod_job_id, transition to RUNPOD_RUNNING,
            # and record cost + duration.
            if self.ledger is not None:
                self.ledger.update_state(
                    job_id,
                    JobLedgerState.RUNPOD_RUNNING,
                    runpod_job_id=result.runpod_job_id,
                )
                if result.cost_cents > 0 or (result.duration_seconds or elapsed) > 0:
                    self.ledger.record_cost(
                        job_id,
                        result.cost_cents,
                        result.duration_seconds or elapsed,
                    )
            # Telemetry: record queue time (from outbox history) and
            # train timing + cost estimate. The GPU type is inferred
            # from the endpoint id (or the caller-supplied gpu_type).
            # Missing GPU price => cost_unknown=True (never zero).
            if self.telemetry is not None:
                resolved_gpu = gpu_type or infer_gpu_type(endpoint_id)
                # Compute queue wait from outbox history: time from the
                # QUEUED entry to the DISPATCHING entry.
                queue_wait = _compute_queue_wait_ns(self.outbox.get(job_id))
                train_duration = result.duration_seconds or elapsed
                phases = (
                    PhaseTiming(
                        phase=TimingPhase.QUEUE,
                        duration_seconds=queue_wait,
                    ),
                    PhaseTiming(
                        phase=TimingPhase.TRAIN,
                        duration_seconds=train_duration,
                    ),
                    PhaseTiming(
                        phase=TimingPhase.TOTAL,
                        duration_seconds=queue_wait + train_duration,
                    ),
                )
                self.telemetry.record_job_timing(
                    job_id,
                    resolved_gpu,
                    phases,
                    actual_cost_cents=result.cost_cents or None,
                )

        # Classify transient vs terminal.
        if result.status == DispatchStatus.TRANSIENT_FAILURE:
            # Leave retryable: update to a non-terminal status with error.
            self.outbox.update_status(
                job_id,
                JobStatus.QUEUED,  # back to queued for retry
                error_code=result.error_code,
                error_summary=result.error_summary,
                note="transient failure; queued for retry",
            )
            # Ledger: record the failure (increments retries) and keep
            # the row in a non-terminal state (back to QUEUED).
            if self.ledger is not None:
                self.ledger.record_failure(
                    job_id,
                    error_code=result.error_code or "transient_failure",
                    error_message=result.error_summary or "",
                    note="transient failure; queued for retry",
                )
                self.ledger.update_state(
                    job_id,
                    JobLedgerState.QUEUED,
                    note="transient failure; queued for retry",
                )
        elif result.status == DispatchStatus.TERMINAL_FAILURE:
            self.outbox.update_status(
                job_id,
                JobStatus.FAILED,
                error_code=result.error_code,
                error_summary=result.error_summary,
            )
            # Ledger: record the failure and transition to FAILED.
            if self.ledger is not None:
                self.ledger.record_failure(
                    job_id,
                    error_code=result.error_code or "terminal_failure",
                    error_message=result.error_summary or "",
                    note="terminal failure",
                )
                self.ledger.update_state(
                    job_id,
                    JobLedgerState.FAILED,
                    note="terminal failure",
                )

        return result

    def dispatch_sweep(self) -> list[DispatchResult]:
        """Dispatch all QUEUED jobs, up to max_dispatches_per_sweep.

        Returns a list of DispatchResult, one per job considered.
        """
        queued = [rec for rec in self.outbox.list() if rec.status == JobStatus.QUEUED]
        # Sort by priority (desc) then created_at (asc).
        queued.sort(key=lambda r: (-r.priority, r.created_at_ns))

        results: list[DispatchResult] = []
        dispatched = 0
        for rec in queued:
            if (
                self.max_dispatches_per_sweep is not None
                and dispatched >= self.max_dispatches_per_sweep
            ):
                results.append(
                    DispatchResult(
                        job_id=rec.job_id,
                        status=DispatchStatus.SKIPPED,
                        error_code="rate_limited",
                        error_summary="max_dispatches_per_sweep reached",
                    )
                )
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
