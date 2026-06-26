"""
TDD tests for quant_foundry.runpod_client (TASK-0502).

Tests the RunPod dispatch client: the only component in Fincept that talks
to RunPod. Behind a config flag (QUANT_FOUNDRY_MODE=runpod). When disabled,
the mock dispatcher (TASK-0305) is used instead.

Acceptance (from NEXT_STEPS_PLAN TASK-0502):
- No RunPod call happens unless explicitly enabled.
- Failed RunPod calls leave retryable jobs.
- Rate and budget limits are enforced.
- The global budget ceiling fails closed: dispatch is refused once the
  monthly cap is hit, with a clear receipt (not a silent drop).
- A simulated spot preemption resumes from checkpoint, not from zero.
- API key is never returned to dashboard or logs.
- RunPod job ID stored in outbox.
- Actual cost + duration recorded in outbox.
"""

from __future__ import annotations

import json
from typing import Any

from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.runpod_client import (
    BudgetGuard,
    DispatchResult,
    DispatchStatus,
    MockRunPodClient,
    RunPodClient,
    RunPodDispatcher,
)

# --- module imports --------------------------------------------------------


def test_runpod_client_imports() -> None:
    assert isinstance(MockRunPodClient, type)
    assert isinstance(RunPodDispatcher, type)
    assert isinstance(BudgetGuard, type)
    # RunPodClient is a Protocol (runtime_checkable)
    assert hasattr(RunPodClient, "_is_protocol") or isinstance(RunPodClient, type)


# --- no RunPod call unless enabled -----------------------------------------


def test_no_runpod_call_unless_enabled(tmp_path) -> None:
    """When mode != 'runpod', the dispatcher must NOT call the RunPod client."""
    calls: list[dict[str, Any]] = []

    class TrackingClient(MockRunPodClient):
        def dispatch(self, **kwargs: Any) -> DispatchResult:  # type: ignore[override]
            calls.append(kwargs)
            return super().dispatch(**kwargs)

    outbox = JobOutbox(base_dir=tmp_path)
    client = TrackingClient(api_key="test-key")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=client,
        mode="local_mock",  # NOT runpod
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
    )

    # Enqueue a job.
    outbox.enqueue(
        job_id="qf:train:rp:1",
        job_type="training",
        idempotency_key="idem-1",
        request_payload={"job_id": "qf:train:rp:1"},
    )

    result = dispatcher.dispatch("qf:train:rp:1", request_payload={"job_id": "qf:train:rp:1"})
    assert result.status == DispatchStatus.SKIPPED
    assert len(calls) == 0  # no RunPod call


def test_runpod_call_when_enabled(tmp_path) -> None:
    """When mode == 'runpod', the dispatcher calls the RunPod client."""
    outbox = JobOutbox(base_dir=tmp_path)
    client = MockRunPodClient(api_key="test-key")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=client,
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
    )

    outbox.enqueue(
        job_id="qf:train:rp:2",
        job_type="training",
        idempotency_key="idem-2",
        request_payload={"job_id": "qf:train:rp:2"},
    )

    result = dispatcher.dispatch("qf:train:rp:2", request_payload={"job_id": "qf:train:rp:2"})
    assert result.status == DispatchStatus.DISPATCHED
    assert result.runpod_job_id  # non-empty


# --- failed calls leave retryable jobs -------------------------------------


def test_transient_failure_leaves_retryable_job(tmp_path) -> None:
    """A transient RunPod failure (e.g. 503) leaves the job retryable."""
    outbox = JobOutbox(base_dir=tmp_path)

    class TransientFailClient(MockRunPodClient):
        def dispatch(self, **kwargs: Any) -> DispatchResult:  # type: ignore[override]
            return DispatchResult(
                job_id=kwargs.get("job_id", ""),
                status=DispatchStatus.TRANSIENT_FAILURE,
                runpod_job_id=None,
                error_code="runpod_503",
                error_summary="RunPod returned 503",
                cost_cents=0,
                duration_seconds=0,
            )

    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=TransientFailClient(api_key="k"),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
    )

    outbox.enqueue(
        job_id="qf:train:trans:1",
        job_type="training",
        idempotency_key="idem-t1",
        request_payload={"job_id": "qf:train:trans:1"},
    )

    result = dispatcher.dispatch("qf:train:trans:1", request_payload={"job_id": "qf:train:trans:1"})
    assert result.status == DispatchStatus.TRANSIENT_FAILURE
    # Job stays in a retryable state (not FAILED).
    rec = outbox.get("qf:train:trans:1")
    assert rec is not None
    assert rec.status != JobStatus.FAILED


def test_terminal_failure_fails_job(tmp_path) -> None:
    """A terminal RunPod failure (e.g. bad request) fails the job."""
    outbox = JobOutbox(base_dir=tmp_path)

    class TerminalFailClient(MockRunPodClient):
        def dispatch(self, **kwargs: Any) -> DispatchResult:  # type: ignore[override]
            return DispatchResult(
                job_id=kwargs.get("job_id", ""),
                status=DispatchStatus.TERMINAL_FAILURE,
                runpod_job_id=None,
                error_code="bad_request",
                error_summary="RunPod rejected the request",
                cost_cents=0,
                duration_seconds=0,
            )

    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=TerminalFailClient(api_key="k"),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=10_00),
    )

    outbox.enqueue(
        job_id="qf:train:term:1",
        job_type="training",
        idempotency_key="idem-term1",
        request_payload={"job_id": "qf:train:term:1"},
    )

    result = dispatcher.dispatch("qf:train:term:1", request_payload={"job_id": "qf:train:term:1"})
    assert result.status == DispatchStatus.TERMINAL_FAILURE
    rec = outbox.get("qf:train:term:1")
    assert rec is not None
    assert rec.status == JobStatus.FAILED


# --- rate limits -----------------------------------------------------------


def test_rate_limit_enforced(tmp_path) -> None:
    """Max dispatches per sweep is enforced."""
    outbox = JobOutbox(base_dir=tmp_path)
    client = MockRunPodClient(api_key="k")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=client,
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=100_00),
        max_dispatches_per_sweep=2,
    )

    for i in range(5):
        outbox.enqueue(
            job_id=f"qf:train:rate:{i}",
            job_type="training",
            idempotency_key=f"idem-rate-{i}",
            request_payload={"job_id": f"qf:train:rate:{i}"},
        )

    results = dispatcher.dispatch_sweep()
    dispatched = [r for r in results if r.status == DispatchStatus.DISPATCHED]
    skipped = [r for r in results if r.status == DispatchStatus.SKIPPED]
    assert len(dispatched) == 2  # rate limit
    assert len(skipped) == 3


# --- per-job budget --------------------------------------------------------


def test_per_job_budget_enforced(tmp_path) -> None:
    """A job whose budget_cents exceeds the per-job limit is refused."""
    outbox = JobOutbox(base_dir=tmp_path)
    client = MockRunPodClient(api_key="k")
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=client,
        mode="runpod",
        budget_guard=BudgetGuard(
            monthly_budget_cents=100_00,
            per_job_budget_cents=50,  # 50 cents max per job
        ),
    )

    outbox.enqueue(
        job_id="qf:train:budget:1",
        job_type="training",
        idempotency_key="idem-b1",
        request_payload={"job_id": "qf:train:budget:1"},
        budget_cents=75,  # exceeds per-job limit
    )

    result = dispatcher.dispatch(
        "qf:train:budget:1", request_payload={"job_id": "qf:train:budget:1"}
    )
    assert result.status == DispatchStatus.BUDGET_EXCEEDED
    assert "per_job" in result.error_code or "budget" in result.error_code.lower()


# --- global monthly budget ceiling -----------------------------------------


def test_global_monthly_budget_ceiling_fails_closed(tmp_path) -> None:
    """Once the monthly cap is hit, dispatch is refused with a clear receipt."""
    outbox = JobOutbox(base_dir=tmp_path)
    client = MockRunPodClient(api_key="k", cost_per_dispatch_cents=60)
    guard = BudgetGuard(monthly_budget_cents=100)  # 100 cents monthly cap
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=client,
        mode="runpod",
        budget_guard=guard,
    )

    # First dispatch: 60 cents (under 100 cap).
    outbox.enqueue(
        job_id="qf:train:cap:1",
        job_type="training",
        idempotency_key="idem-c1",
        request_payload={"job_id": "qf:train:cap:1"},
    )
    r1 = dispatcher.dispatch("qf:train:cap:1", request_payload={"job_id": "qf:train:cap:1"})
    assert r1.status == DispatchStatus.DISPATCHED
    assert r1.cost_cents == 60

    # Second dispatch: would push to 120 (over 100 cap) -> refused.
    outbox.enqueue(
        job_id="qf:train:cap:2",
        job_type="training",
        idempotency_key="idem-c2",
        request_payload={"job_id": "qf:train:cap:2"},
    )
    r2 = dispatcher.dispatch("qf:train:cap:2", request_payload={"job_id": "qf:train:cap:2"})
    assert r2.status == DispatchStatus.BUDGET_EXCEEDED
    assert "monthly" in r2.error_code or "global" in r2.error_code.lower()
    assert r2.error_summary  # clear receipt, not a silent drop


# --- spot preemption is transient ------------------------------------------


def test_spot_preemption_is_transient(tmp_path) -> None:
    """A spot preemption is classified as transient (retryable), not terminal."""
    outbox = JobOutbox(base_dir=tmp_path)

    class PreemptClient(MockRunPodClient):
        def dispatch(self, **kwargs: Any) -> DispatchResult:  # type: ignore[override]
            return DispatchResult(
                job_id=kwargs.get("job_id", ""),
                status=DispatchStatus.TRANSIENT_FAILURE,
                runpod_job_id=kwargs.get("job_id"),
                error_code="spot_preemption",
                error_summary="Spot instance preempted; checkpoint saved",
                cost_cents=10,
                duration_seconds=30,
            )

    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=PreemptClient(api_key="k"),
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=100_00),
    )

    outbox.enqueue(
        job_id="qf:train:preempt:1",
        job_type="training",
        idempotency_key="idem-p1",
        request_payload={"job_id": "qf:train:preempt:1"},
    )

    result = dispatcher.dispatch(
        "qf:train:preempt:1", request_payload={"job_id": "qf:train:preempt:1"}
    )
    assert result.status == DispatchStatus.TRANSIENT_FAILURE
    assert result.error_code == "spot_preemption"
    # Job stays retryable.
    rec = outbox.get("qf:train:preempt:1")
    assert rec is not None
    assert rec.status != JobStatus.FAILED


# --- API key redaction -----------------------------------------------------


def test_api_key_never_in_result_or_logs(tmp_path) -> None:
    """The API key must never appear in the dispatch result or outbox record."""
    secret_key = "sk-runpod-secret-12345"
    outbox = JobOutbox(base_dir=tmp_path)
    client = MockRunPodClient(api_key=secret_key)
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=client,
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=100_00),
    )

    outbox.enqueue(
        job_id="qf:train:redact:1",
        job_type="training",
        idempotency_key="idem-r1",
        request_payload={"job_id": "qf:train:redact:1"},
    )

    result = dispatcher.dispatch(
        "qf:train:redact:1", request_payload={"job_id": "qf:train:redact:1"}
    )
    result_json = json.dumps(
        result.__dict__ if hasattr(result, "__dict__") else result.model_dump()
    )
    assert secret_key not in result_json

    rec = outbox.get("qf:train:redact:1")
    assert rec is not None
    rec_json = rec.model_dump_json()
    assert secret_key not in rec_json


# --- RunPod job ID + cost + duration stored in outbox ----------------------


def test_runpod_job_id_and_cost_stored_in_outbox(tmp_path) -> None:
    """A successful dispatch stores the RunPod job ID + cost + duration."""
    outbox = JobOutbox(base_dir=tmp_path)
    client = MockRunPodClient(api_key="k", cost_per_dispatch_cents=25)
    dispatcher = RunPodDispatcher(
        outbox=outbox,
        client=client,
        mode="runpod",
        budget_guard=BudgetGuard(monthly_budget_cents=100_00),
    )

    outbox.enqueue(
        job_id="qf:train:store:1",
        job_type="training",
        idempotency_key="idem-s1",
        request_payload={"job_id": "qf:train:store:1"},
    )

    result = dispatcher.dispatch("qf:train:store:1", request_payload={"job_id": "qf:train:store:1"})
    assert result.status == DispatchStatus.DISPATCHED
    assert result.runpod_job_id  # non-empty
    assert result.cost_cents == 25
    assert result.duration_seconds >= 0

    rec = outbox.get("qf:train:store:1")
    assert rec is not None
    # RunPod job ID stored in outbox record.
    assert rec.runpod_job_id == result.runpod_job_id
    # Cost + duration stored in the latest history entry's note (JSON).
    last_entry = rec.history[-1]
    assert "cost_cents" in last_entry.get("note", "") or "cost_cents" in json.dumps(last_entry)
    # The actual cost value is recoverable from the history.
    assert "25" in json.dumps(last_entry)


# --- HttpRunPodClient tests (using httpx MockTransport) ---------------------


def test_http_client_success() -> None:
    """HttpRunPodClient.dispatch() returns DISPATCHED on HTTP 200 with id."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "/run" in str(request.url)
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert "input" in body
        return httpx.Response(200, json={"id": "rp-job-12345", "status": "IN_QUEUE"})

    transport = httpx.MockTransport(handler)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        base_url="https://api.runpod.ai/v2",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:1",
        request_payload={"job_id": "qf:train:http:1", "model_family": "gbm"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.DISPATCHED
    assert result.runpod_job_id == "rp-job-12345"
    assert result.cost_cents == 0  # cost recorded on callback, not dispatch


def test_http_client_rate_limit_transient() -> None:
    """HttpRunPodClient classifies HTTP 429 as TRANSIENT_FAILURE."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    transport = httpx.MockTransport(lambda req: httpx.Response(429, text="Rate limited"))
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:429",
        request_payload={"job_id": "qf:train:http:429"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.TRANSIENT_FAILURE
    assert "429" in (result.error_code or "")


def test_http_client_503_transient() -> None:
    """HttpRunPodClient classifies HTTP 503 as TRANSIENT_FAILURE."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    transport = httpx.MockTransport(lambda req: httpx.Response(503, text="Unavailable"))
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:503",
        request_payload={"job_id": "qf:train:http:503"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.TRANSIENT_FAILURE


def test_http_client_400_terminal() -> None:
    """HttpRunPodClient classifies HTTP 400 as TERMINAL_FAILURE."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    transport = httpx.MockTransport(lambda req: httpx.Response(400, text="Bad request"))
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:400",
        request_payload={"job_id": "qf:train:http:400"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.TERMINAL_FAILURE
    assert "400" in (result.error_code or "")


def test_http_client_401_terminal() -> None:
    """HttpRunPodClient classifies HTTP 401 as TERMINAL_FAILURE."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    transport = httpx.MockTransport(lambda req: httpx.Response(401, text="Unauthorized"))
    client = HttpRunPodClient(
        api_key="bad-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:401",
        request_payload={"job_id": "qf:train:http:401"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.TERMINAL_FAILURE


def test_http_client_missing_id_terminal() -> None:
    """HttpRunPodClient returns TERMINAL_FAILURE when response has no 'id'."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"status": "ok"}))
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:noid",
        request_payload={"job_id": "qf:train:http:noid"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.TERMINAL_FAILURE
    assert result.error_code == "missing_job_id"


def test_http_client_network_error_transient() -> None:
    """HttpRunPodClient classifies network errors as TRANSIENT_FAILURE."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    def raise_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(raise_error)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:neterr",
        request_payload={"job_id": "qf:train:http:neterr"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.TRANSIENT_FAILURE
    assert result.error_code == "network_error"


def test_http_client_api_key_never_in_result() -> None:
    """The API key must never appear in the DispatchResult."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"id": "rp-1"}))
    client = HttpRunPodClient(
        api_key="super-secret-key-12345",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:http:secret",
        request_payload={"job_id": "qf:train:http:secret"},
        budget_cents=None,
    )
    dumped = json.dumps(result.model_dump())
    assert "super-secret-key-12345" not in dumped


def test_http_client_check_status() -> None:
    """HttpRunPodClient.check_status() returns the raw JSON response."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/status/rp-job-12345" in str(request.url)
        return httpx.Response(200, json={"status": "COMPLETED", "output": {"result": "ok"}})

    transport = httpx.MockTransport(handler)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    status = client.check_status("rp-job-12345")
    assert status["status"] == "COMPLETED"


def test_http_client_check_health() -> None:
    """HttpRunPodClient.check_health() returns the raw JSON response."""
    import httpx
    from quant_foundry.runpod_client import HttpRunPodClient

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"health": "ok", "workers": 2})
    )
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    health = client.check_health()
    assert health["health"] == "ok"
