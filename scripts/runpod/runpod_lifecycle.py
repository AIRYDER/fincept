"""Shared RunPod endpoint/template lifecycle helpers.

This module centralizes the duplicated endpoint/template creation, naming,
cleanup, retry, and timeout-configuration logic that was previously copied
across ``run_live_canary.py``, ``run_train_model.py``, and
``run_gpu_healthcheck.py``.

Hard rules enforced here (see ``.devin/skills/runpod-worker-ops/SKILL.md``):

1. ``executionTimeout`` is always set to **>= 1860s** (handler deadline 1800s
   + 60s slack) so the handler's signed failure envelope always fires before
   RunPod times the job out. RunPod's default endpoint job timeout is 600s,
   which would kill a 20-minute training job before the handler can emit its
   signed receipt.
2. Template and endpoint names are unique per run (timestamp + SHA suffix) to
   avoid RunPod name-collision errors.
3. Endpoint deletion is retried on transient failures (RunPod can return
   "Failed to terminate resources. Try again." while the worker is still
   spinning down).
4. No live HTTP calls are made at import time — every function that touches
   the RunPod API accepts an injectable ``graphql_fn`` / ``delete_fn`` so the
   logic is fully unit-testable with mocks.

Usage from the live-probe tools::

    from scripts.runpod.runpod_lifecycle import (
        EndpointConfig,
        build_template_input,
        build_endpoint_input,
        make_unique_name,
        retry_delete_endpoint,
        safe_scale_to_zero,
    )
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

__all__ = [
    "DEFAULT_DEADLINE_S",
    "DEFAULT_IDLE_TIMEOUT_S",
    "DEFAULT_SLACK_S",
    "MIN_EXECUTION_TIMEOUT_S",
    "EndpointConfig",
    "TemplateConfig",
    "build_endpoint_input",
    "build_job_policy",
    "build_template_input",
    "compute_execution_timeout",
    "make_unique_name",
    "retry_delete_endpoint",
    "safe_scale_to_zero",
    "validate_execution_timeout",
    # Tier 0.2: network volume management (durable artifact storage)
    "create_network_volume",
    "list_network_volumes",
    "delete_network_volume",
]

# --- Timeout constants -------------------------------------------------------

# Handler deadline (QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS default).
DEFAULT_DEADLINE_S = 1800

# Slack added on top of the deadline so the handler's signed failure envelope
# always fires before RunPod kills the job.
DEFAULT_SLACK_S = 60

# Minimum allowed executionTimeout — the hard floor from the skill file.
MIN_EXECUTION_TIMEOUT_S = 1860

# Default idle timeout (seconds) — how long a worker stays warm with no jobs.
DEFAULT_IDLE_TIMEOUT_S = 300


def compute_execution_timeout(
    deadline_s: int = DEFAULT_DEADLINE_S,
    slack_s: int = DEFAULT_SLACK_S,
) -> int:
    """Return the endpoint ``executionTimeout`` (deadline + slack).

    Always >= ``MIN_EXECUTION_TIMEOUT_S`` (1860). If a caller passes a smaller
    deadline+slack, the floor is enforced so the hard rule is never violated.
    """
    computed = deadline_s + slack_s
    return max(computed, MIN_EXECUTION_TIMEOUT_S)


def validate_execution_timeout(timeout_s: int) -> int:
    """Validate that an ``executionTimeout`` meets the >= 1860s hard rule.

    Raises ``ValueError`` if the timeout is below the minimum. Returns the
    timeout unchanged when valid.
    """
    if timeout_s < MIN_EXECUTION_TIMEOUT_S:
        raise ValueError(
            f"executionTimeout {timeout_s}s is below the minimum "
            f"{MIN_EXECUTION_TIMEOUT_S}s (handler deadline "
            f"{DEFAULT_DEADLINE_S}s + {DEFAULT_SLACK_S}s slack). "
            f"RunPod would kill the job before the handler's signed "
            f"failure envelope fires."
        )
    return timeout_s


# --- Config dataclasses ------------------------------------------------------


class TemplateConfig:
    """Configuration for a RunPod serverless template."""

    def __init__(
        self,
        name: str,
        image_name: str,
        env_vars: Sequence[dict[str, str]],
        registry_auth_id: str,
        container_disk_gb: int = 20,
        volume_in_gb: int = 0,
        docker_args: str = "",
        is_serverless: bool = True,
    ) -> None:
        self.name = name
        self.image_name = image_name
        self.env_vars = list(env_vars)
        self.registry_auth_id = registry_auth_id
        self.container_disk_gb = container_disk_gb
        self.volume_in_gb = volume_in_gb
        self.docker_args = docker_args
        self.is_serverless = is_serverless


class EndpointConfig:
    """Configuration for a RunPod serverless endpoint.

    The ``executionTimeout`` defaults to ``compute_execution_timeout()`` which
    is always >= 1860s. Callers can override but ``validate_execution_timeout``
    is enforced in ``build_endpoint_input``.
    """

    def __init__(
        self,
        name: str,
        template_id: str,
        gpu_ids: str = "ADA_24",
        workers_min: int = 1,
        workers_max: int = 1,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT_S,
        execution_timeout: int | None = None,
        scaler_type: str = "QUEUE_DELAY",
        scaler_value: int = 4,
        container_disk_gb: int = 20,
        network_volume_id: str | None = None,
    ) -> None:
        self.name = name
        self.template_id = template_id
        self.gpu_ids = gpu_ids
        self.workers_min = workers_min
        self.workers_max = workers_max
        self.idle_timeout = idle_timeout
        # Default to the computed safe timeout if not explicitly provided.
        self.execution_timeout = (
            execution_timeout if execution_timeout is not None else compute_execution_timeout()
        )
        self.scaler_type = scaler_type
        self.scaler_value = scaler_value
        self.container_disk_gb = container_disk_gb
        # Tier 0.2: optional network volume to mount at /runpod-volume/
        # inside serverless workers. When set, the endpoint creation
        # mutation includes ``networkVolumeId`` so artifacts written to
        # /runpod-volume/ survive worker shutdown.
        self.network_volume_id = network_volume_id


# --- Input builders ----------------------------------------------------------


def build_template_input(config: TemplateConfig) -> dict[str, Any]:
    """Build the ``SaveTemplateInput`` dict for the RunPod GraphQL mutation."""
    return {
        "name": config.name,
        "imageName": config.image_name,
        "env": list(config.env_vars),
        "containerRegistryAuthId": config.registry_auth_id,
        "dockerArgs": config.docker_args,
        "volumeInGb": config.volume_in_gb,
        "containerDiskInGb": config.container_disk_gb,
        "isServerless": config.is_serverless,
    }


def build_endpoint_input(config: EndpointConfig) -> dict[str, Any]:
    """Build the ``EndpointInput`` dict for the RunPod GraphQL mutation.

    Always includes ``executionTimeoutMs`` (>= 1_860_000 ms, validated) so
    RunPod never inherits its 600s default. RunPod's GraphQL schema renamed
    ``executionTimeout`` (seconds) to ``executionTimeoutMs`` (milliseconds);
    the per-request ``policy.executionTimeout`` from ``build_job_policy``
    remains the documented, reliable override path.
    """
    validated = validate_execution_timeout(config.execution_timeout)
    result: dict[str, Any] = {
        "name": config.name,
        "templateId": config.template_id,
        "gpuIds": config.gpu_ids,
        "workersMin": config.workers_min,
        "workersMax": config.workers_max,
        "idleTimeout": config.idle_timeout,
        "executionTimeoutMs": validated * 1000,
        "scalerType": config.scaler_type,
        "scalerValue": config.scaler_value,
    }
    # Tier 0.2: attach a network volume so artifacts written to
    # /runpod-volume/ persist across worker shutdowns.
    if config.network_volume_id:
        result["networkVolumeId"] = config.network_volume_id
    return result


# --- Per-request job policy --------------------------------------------------


def build_job_policy(
    execution_timeout_s: int | None = None,
    *,
    ttl_s: int | None = None,
    low_priority: bool = False,
) -> dict[str, Any]:
    """Build the per-request ``policy`` dict for a RunPod ``/run`` or
    ``/runsync`` request.

    RunPod's documented way to override the execution timeout per job is the
    ``policy.executionTimeout`` field in the request body (see
    https://docs.runpod.io/serverless/endpoints/send-requests#execution-policies).
    The value is in **milliseconds**, not seconds. The endpoint-level
    ``executionTimeout`` in ``build_endpoint_input`` is kept as a best-effort
    undocumented field — the per-request policy is the reliable, documented
    path.

    Args:
        execution_timeout_s: Execution timeout in **seconds**. Defaults to
            ``compute_execution_timeout()`` (>= 1860). Converted to ms.
        ttl_s: Optional job TTL in seconds. Converted to ms.
        low_priority: If True, the job won't trigger worker scaling.

    Returns:
        Dict suitable for inclusion as the ``policy`` key in a ``/run`` or
        ``/runsync`` request body::

            {"input": {...}, "policy": build_job_policy()}
    """
    timeout_s = (
        execution_timeout_s if execution_timeout_s is not None else compute_execution_timeout()
    )
    validated = validate_execution_timeout(timeout_s)
    policy: dict[str, Any] = {
        "executionTimeout": validated * 1000,
        "lowPriority": low_priority,
    }
    if ttl_s is not None:
        policy["ttl"] = ttl_s * 1000
    return policy


# --- Unique naming -----------------------------------------------------------


def make_unique_name(
    prefix: str,
    sha: str,
    *,
    suffix: str = "",
    timestamp: int | None = None,
    sha_len: int = 8,
) -> str:
    """Generate a unique RunPod resource name to avoid collisions.

    RunPod requires unique template names and endpoint names. Reusing a bare
    ``qf-canary-<sha8>`` name across runs or across probe types causes
    "name already exists" errors. This helper appends a timestamp (and optional
    suffix) so every run gets a unique name.

    Format: ``<prefix>-<sha[:sha_len]>[-<suffix>]-<timestamp>``

    Examples::

        make_unique_name("qf-canary", "abcdef1234567890")
        # -> "qf-canary-abcdef-1719900000"

        make_unique_name("qf-a7train", "abcdef1234567890", suffix="tpl")
        # -> "qf-a7train-abcdef-tpl-1719900000"
    """
    ts = timestamp if timestamp is not None else int(time.time())
    sha_part = sha[:sha_len]
    parts = [prefix, sha_part]
    if suffix:
        parts.append(suffix)
    parts.append(str(ts))
    return "-".join(parts)


# --- Retry cleanup -----------------------------------------------------------

# Type alias for an injectable delete-endpoint function (for testability).
DeleteFn = Callable[[str], None]
ScaleFn = Callable[[str, int, int], Any]


def retry_delete_endpoint(
    endpoint_id: str,
    delete_fn: DeleteFn,
    *,
    max_attempts: int = 5,
    delay_s: float = 10.0,
    sleeper: Callable[[float], None] = time.sleep,
    logger: Callable[[str], None] | None = None,
) -> bool:
    """Delete a RunPod endpoint with retry on transient failures.

    RunPod's ``deleteEndpoint`` can fail transiently with "Failed to terminate
    resources. Try again." while the worker is still spinning down after a
    job. This helper retries up to ``max_attempts`` times with ``delay_s``
    between attempts.

    Args:
        endpoint_id: The endpoint ID to delete.
        delete_fn: Callable that performs the actual deletion (typically
            ``run_live_canary.delete_endpoint``).
        max_attempts: Maximum number of retry attempts.
        delay_s: Delay between attempts in seconds.
        sleeper: Injectable sleep function (for tests).
        logger: Optional logging callable for progress messages.

    Returns:
        attempts failed.
    """

    def _log(msg: str) -> None:
        if logger:
            logger(msg)

    for attempt in range(1, max_attempts + 1):
        try:
            delete_fn(endpoint_id)
            _log(f"Endpoint {endpoint_id} deleted (attempt {attempt}/{max_attempts})")
            return True
        except Exception as e:
            _log(
                f"WARNING: could not delete endpoint {endpoint_id} "
                f"(attempt {attempt}/{max_attempts}): {e}"
            )
            if attempt < max_attempts:
                sleeper(delay_s)
    return False


def safe_scale_to_zero(
    endpoint_id: str,
    scale_fn: ScaleFn,
    *,
    logger: Callable[[str], None] | None = None,
) -> bool:
    """Scale an endpoint's workers to 0/0 (safe shutdown).

    This is best-effort: if the scale call fails, the endpoint will still be
    deleted by ``retry_delete_endpoint``. The function never raises.

    Args:
        endpoint_id: The endpoint ID to scale down.
        scale_fn: Callable(endpoint_id, workers_min, workers_max) that performs
            the scale (typically ``run_live_canary.update_endpoint_workers``).
        logger: Optional logging callable.

    Returns:
        ``True`` if scaled successfully, ``False`` on failure.
    """
    try:
        scale_fn(endpoint_id, 0, 0)
        if logger:
            logger(f"Scaled endpoint {endpoint_id} to 0/0")
        return True
    except Exception as e:
        if logger:
            logger(f"WARNING: could not scale down {endpoint_id}: {e}")
        return False


# --- Receipt-friendly logging ------------------------------------------------


def format_timeout_receipt(
    execution_timeout: int,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT_S,
    deadline_s: int = DEFAULT_DEADLINE_S,
) -> dict[str, Any]:
    """Build a receipt-friendly dict recording the timeout configuration.

    This is written into the endpoint-create receipt JSON so future agents can
    audit that the timeout was set correctly.
    """
    return {
        "executionTimeout": execution_timeout,
        "idleTimeout": idle_timeout,
        "handler_deadline_s": deadline_s,
        "slack_s": execution_timeout - deadline_s,
        "meets_min_requirement": execution_timeout >= MIN_EXECUTION_TIMEOUT_S,
        "min_required_execution_timeout": MIN_EXECUTION_TIMEOUT_S,
        "note": (
            "executionTimeout >= handler deadline + 60s slack so the "
            "handler's signed failure envelope fires before RunPod times "
            "the job out."
        ),
    }


# --- Tier 0.2: Network volume management (durable artifact storage) ----------
#
# Network volumes are persistent NVMe-backed storage that mounts at
# /runpod-volume/ inside serverless workers. Data survives worker shutdown
# and endpoint deletion. Used by VolumeArtifactWriter to persist trained
# model artifacts so they don't die with the worker.
#
# API: https://docs.runpod.io/serverless/storage/network-volumes
# REST: https://rest.runpod.io/v1/networkvolumes


def create_network_volume(
    name: str,
    size_gb: int,
    data_center_id: str,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Create a RunPod network volume via the REST API.

    Args:
        name: Human-readable volume name.
        size_gb: Volume size in GB (cannot be decreased later).
        data_center_id: Data center ID (e.g. ``"US-KS-2"``).
        api_key: RunPod API key. Defaults to ``RUNPOD_API_KEY`` env var.

    Returns:
        The created volume dict from the API response (includes ``id``,
        ``name``, ``size``, ``dataCenterId``).

    Raises:
        RuntimeError: if the API key is missing or the request fails.
    """
    import json
    import os
    import urllib.request

    key = api_key or os.environ.get("RUNPOD_API_KEY", "")
    if not key:
        raise RuntimeError("RUNPOD_API_KEY not set — cannot create network volume")

    payload = json.dumps(
        {
            "name": name,
            "size": size_gb,
            "dataCenterId": data_center_id,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://rest.runpod.io/v1/networkvolumes",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - RunPod REST API
            return json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Failed to create network volume: {exc}") from exc


def list_network_volumes(
    *,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """List all network volumes in the account.

    Args:
        api_key: RunPod API key. Defaults to ``RUNPOD_API_KEY`` env var.

    Returns:
        List of volume dicts (each with ``id``, ``name``, ``size``,
        ``dataCenterId``, ``mountPath``).

    Raises:
        RuntimeError: if the API key is missing or the request fails.
    """
    import os
    import urllib.request

    key = api_key or os.environ.get("RUNPOD_API_KEY", "")
    if not key:
        raise RuntimeError("RUNPOD_API_KEY not set — cannot list network volumes")

    req = urllib.request.Request(
        "https://rest.runpod.io/v1/networkvolumes",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - RunPod REST API
            import json

            return json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Failed to list network volumes: {exc}") from exc


def delete_network_volume(
    volume_id: str,
    *,
    api_key: str | None = None,
) -> bool:
    """Delete a network volume by ID.

    Args:
        volume_id: The network volume ID to delete.
        api_key: RunPod API key. Defaults to ``RUNPOD_API_KEY`` env var.

    Returns:
        ``True`` if deleted, ``False`` on failure.
    """
    import os
    import urllib.request

    key = api_key or os.environ.get("RUNPOD_API_KEY", "")
    if not key:
        raise RuntimeError("RUNPOD_API_KEY not set — cannot delete network volume")

    req = urllib.request.Request(
        f"https://rest.runpod.io/v1/networkvolumes/{volume_id}",
        headers={"Authorization": f"Bearer {key}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - RunPod REST API
            return True
    except Exception:
        return False
