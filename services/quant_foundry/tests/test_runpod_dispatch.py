"""Tests for the RunPod training dispatch wiring (Tier 1A).

Verifies that the dispatch path:
- Passes ``presigned_artifact_url`` through to the worker's job input.
- Includes ``policy.executionTimeout`` (in ms, >= 1860000) from
  ``build_job_policy()`` in the ``/run`` request body.
- The endpoint template includes ``networkVolumeId`` when a network
  volume is configured.

Hard rules:
- NO live RunPod API calls are made. All HTTP interaction uses
  ``httpx.MockTransport`` or the in-process ``MockRunPodClient``.
- The handler.py worker code is NOT changed (it already reads
  ``presigned_artifact_url``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from quant_foundry.runpod_client import (
    DispatchStatus,
    HttpRunPodClient,
    MockRunPodClient,
)
from quant_foundry.runpod_policy import (
    DEFAULT_VOLUME_MOUNT_PATH,
    MIN_EXECUTION_TIMEOUT_S,
    EndpointConfig,
    build_endpoint_input,
    build_job_policy,
    build_training_job_input,
)
from quant_foundry.schemas import RunPodTrainingRequest

# --- RunPodTrainingRequest schema -------------------------------------------


def test_request_accepts_presigned_artifact_url() -> None:
    """RunPodTrainingRequest accepts presigned_artifact_url."""
    req = RunPodTrainingRequest(
        job_id="qf:train:presigned:1",
        dataset_manifest_ref="s3://bucket/manifest.json",
        model_family="gbm",
        presigned_artifact_url="https://s3.example.com/bucket/model.pkl",
    )
    assert req.presigned_artifact_url == "https://s3.example.com/bucket/model.pkl"


def test_request_presigned_artifact_url_defaults_none() -> None:
    """presigned_artifact_url defaults to None when omitted."""
    req = RunPodTrainingRequest(
        job_id="qf:train:nopresigned:1",
        dataset_manifest_ref="s3://bucket/manifest.json",
        model_family="gbm",
    )
    assert req.presigned_artifact_url is None


def test_request_rejects_extra_fields() -> None:
    """extra='forbid' still enforced with the new field."""
    with pytest.raises(Exception):
        RunPodTrainingRequest(
            job_id="qf:train:extra:1",
            dataset_manifest_ref="s3://bucket/manifest.json",
            model_family="gbm",
            unknown_field="bad",
        )


# --- build_training_job_input (presigned URL pass-through) ------------------


def test_build_training_job_input_includes_presigned_url() -> None:
    """build_training_job_input includes presigned_artifact_url when set."""
    req = RunPodTrainingRequest(
        job_id="qf:train:input:1",
        dataset_manifest_ref="s3://bucket/manifest.json",
        model_family="gbm",
        presigned_artifact_url="https://s3.example.com/bucket/model.pkl",
    )
    inp = build_training_job_input(req)
    assert inp["presigned_artifact_url"] == "https://s3.example.com/bucket/model.pkl"
    assert inp["job_id"] == "qf:train:input:1"


def test_build_training_job_input_includes_none_presigned() -> None:
    """build_training_job_input includes presigned_artifact_url=None when unset."""
    req = RunPodTrainingRequest(
        job_id="qf:train:input:2",
        dataset_manifest_ref="s3://bucket/manifest.json",
        model_family="gbm",
    )
    inp = build_training_job_input(req)
    assert "presigned_artifact_url" in inp
    assert inp["presigned_artifact_url"] is None


def test_build_training_job_input_merges_extra_fields() -> None:
    """build_training_job_input merges extra top-level fields."""
    req = RunPodTrainingRequest(
        job_id="qf:train:input:3",
        dataset_manifest_ref="s3://bucket/manifest.json",
        model_family="gbm",
        presigned_artifact_url="https://s3.example.com/bucket/model.pkl",
    )
    inp = build_training_job_input(
        req,
        extra_fields={"output_prefix": "/runpod-volume/out", "n_folds": 5},
    )
    assert inp["presigned_artifact_url"] == "https://s3.example.com/bucket/model.pkl"
    assert inp["output_prefix"] == "/runpod-volume/out"
    assert inp["n_folds"] == 5


# --- HttpRunPodClient.dispatch (policy + presigned URL) ---------------------


def test_dispatch_includes_presigned_url_in_input() -> None:
    """HttpRunPodClient.dispatch sends presigned_artifact_url in the input."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "rp-job-presigned-1"})

    transport = httpx.MockTransport(handler)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    req = RunPodTrainingRequest(
        job_id="qf:train:dispatch:1",
        dataset_manifest_ref="s3://bucket/manifest.json",
        model_family="gbm",
        presigned_artifact_url="https://s3.example.com/bucket/model.pkl",
    )
    input_data = build_training_job_input(req)
    result = client.dispatch(
        job_id="qf:train:dispatch:1",
        request_payload=input_data,
        budget_cents=None,
    )
    assert result.status == DispatchStatus.DISPATCHED
    assert result.runpod_job_id == "rp-job-presigned-1"
    # The presigned URL is in the input dict sent to RunPod.
    assert captured["body"]["input"]["presigned_artifact_url"] == (
        "https://s3.example.com/bucket/model.pkl"
    )


def test_dispatch_includes_policy_execution_timeout_ms() -> None:
    """HttpRunPodClient.dispatch includes policy.executionTimeout in ms."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "rp-job-policy-1"})

    transport = httpx.MockTransport(handler)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    result = client.dispatch(
        job_id="qf:train:policy:1",
        request_payload={"job_id": "qf:train:policy:1", "model_family": "gbm"},
        budget_cents=None,
    )
    assert result.status == DispatchStatus.DISPATCHED
    body = captured["body"]
    assert "policy" in body
    assert "executionTimeout" in body["policy"]
    # executionTimeout is in milliseconds and >= 1860000 (1860s).
    assert body["policy"]["executionTimeout"] >= 1860000
    assert body["policy"]["executionTimeout"] == MIN_EXECUTION_TIMEOUT_S * 1000


def test_dispatch_body_shape_is_input_plus_policy() -> None:
    """The /run body is {"input": ..., "policy": ...}."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "rp-job-shape-1"})

    transport = httpx.MockTransport(handler)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-1",
        transport=transport,
    )
    client.dispatch(
        job_id="qf:train:shape:1",
        request_payload={"job_id": "qf:train:shape:1"},
        budget_cents=None,
    )
    body = captured["body"]
    assert set(body.keys()) == {"input", "policy"}
    assert body["input"]["job_id"] == "qf:train:shape:1"


def test_dispatch_no_live_calls_mock_client() -> None:
    """MockRunPodClient makes no HTTP calls and returns DISPATCHED."""
    client = MockRunPodClient(api_key="test-key")
    result = client.dispatch(
        job_id="qf:train:mock:1",
        request_payload={
            "job_id": "qf:train:mock:1",
            "presigned_artifact_url": "https://s3.example.com/bucket/model.pkl",
        },
        budget_cents=None,
    )
    assert result.status == DispatchStatus.DISPATCHED
    assert result.runpod_job_id is not None


# --- build_job_policy -------------------------------------------------------


def test_build_job_policy_default_meets_minimum() -> None:
    """build_job_policy() default executionTimeout >= 1860000 ms."""
    policy = build_job_policy()
    assert policy["executionTimeout"] >= 1860000
    assert policy["executionTimeout"] == MIN_EXECUTION_TIMEOUT_S * 1000
    assert policy["lowPriority"] is False


def test_build_job_policy_rejects_below_minimum() -> None:
    """build_job_policy raises on timeout below 1860s."""
    with pytest.raises(ValueError):
        build_job_policy(execution_timeout_s=600)


def test_build_job_policy_ttl_converted_to_ms() -> None:
    """ttl_s is converted to milliseconds."""
    policy = build_job_policy(ttl_s=3600)
    assert policy["ttl"] == 3600000


# --- Endpoint template volume mounting --------------------------------------


def test_endpoint_template_includes_network_volume_id() -> None:
    """Endpoint input includes networkVolumeId when volume configured."""
    cfg = EndpointConfig(
        name="qf-train-ep",
        template_id="tpl-abc",
        network_volume_id="vol-123",
        volume_in_gb=200,
        volume_mount_path="/runpod-volume",
    )
    inp = build_endpoint_input(cfg)
    assert inp["networkVolumeId"] == "vol-123"
    assert inp["volumeInGb"] == 200
    assert inp["volumeMountPath"] == "/runpod-volume"


def test_endpoint_template_omits_network_volume_id_when_unset() -> None:
    """Endpoint input omits networkVolumeId when no volume configured."""
    cfg = EndpointConfig(
        name="qf-train-ep",
        template_id="tpl-abc",
    )
    inp = build_endpoint_input(cfg)
    assert "networkVolumeId" not in inp
    assert "volumeInGb" not in inp
    assert "volumeMountPath" not in inp


def test_endpoint_template_default_volume_mount_path() -> None:
    """Default volume_mount_path is /runpod-volume."""
    cfg = EndpointConfig(
        name="qf-train-ep",
        template_id="tpl-abc",
        network_volume_id="vol-123",
    )
    inp = build_endpoint_input(cfg)
    assert inp["volumeMountPath"] == DEFAULT_VOLUME_MOUNT_PATH


def test_endpoint_template_execution_timeout_always_present() -> None:
    """executionTimeout is always present and >= 1860s."""
    cfg = EndpointConfig(
        name="qf-train-ep",
        template_id="tpl-abc",
        network_volume_id="vol-123",
    )
    inp = build_endpoint_input(cfg)
    assert inp["executionTimeout"] >= MIN_EXECUTION_TIMEOUT_S


# --- No live calls guard ----------------------------------------------------


def test_no_live_runpod_api_calls_in_test_suite() -> None:
    """Sanity guard: confirm the test suite uses only mocked transports.

    This is a meta-test: it asserts that the HttpRunPodClient used in
    this file never opens a real network connection by verifying the
    MockTransport intercepts every request. If a future test
    accidentally constructs an HttpRunPodClient without a transport,
    this test will not catch it directly — but it documents the hard
    rule and exercises the mock path end-to-end.
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": "rp-no-live"})

    transport = httpx.MockTransport(handler)
    client = HttpRunPodClient(
        api_key="test-key",
        endpoint_id="ep-no-live",
        transport=transport,
    )
    client.dispatch(
        job_id="qf:train:nolive:1",
        request_payload={"job_id": "qf:train:nolive:1"},
        budget_cents=None,
    )
    # The URL was intercepted by the mock transport (no real DNS/HTTP).
    assert "ep-no-live" in captured["url"]
    assert "api.runpod.ai" in captured["url"]
