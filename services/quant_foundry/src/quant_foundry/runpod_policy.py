"""quant_foundry.runpod_policy — RunPod per-request policy + endpoint helpers.

This module is the service-package copy of the per-request execution
timeout policy and endpoint-template helpers that originally lived in
``scripts/runpod/runpod_lifecycle.py`` (Tier 0). The probe scripts keep
their existing import; the service package imports from here so it does
not depend on the ``scripts`` package (which is not installed as a
module).

Hard rules enforced here (see ``.devin/skills/runpod-worker-ops/SKILL.md``):

1. ``executionTimeout`` is always set to **>= 1860s** (handler deadline
   1800s + 60s slack) so the handler's signed failure envelope always
   fires before RunPod times the job out. RunPod's default endpoint job
   timeout is 600s, which would kill a 20-minute training job before the
   handler can emit its signed receipt.
2. The per-request ``policy.executionTimeout`` (in **milliseconds**) is
   the documented, reliable way to override the timeout per job. The
   endpoint-level ``executionTimeout`` in ``build_endpoint_input`` is
   kept as a best-effort field.
3. No live HTTP calls are made at import time — every function is pure.

Usage from the dispatch path::

    from quant_foundry.runpod_policy import build_job_policy

    body = json.dumps({"input": input_data, "policy": build_job_policy()})

Usage for endpoint templates with network volumes::

    from quant_foundry.runpod_policy import build_endpoint_input, EndpointConfig

    inp = build_endpoint_input(EndpointConfig(
        name="qf-train-ep",
        template_id="tpl-abc",
        network_volume_id="vol-123",
        volume_in_gb=200,
        volume_mount_path="/runpod-volume",
    ))
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_DEADLINE_S",
    "DEFAULT_IDLE_TIMEOUT_S",
    "DEFAULT_SLACK_S",
    "DEFAULT_VOLUME_MOUNT_PATH",
    "MIN_EXECUTION_TIMEOUT_S",
    "EndpointConfig",
    "build_endpoint_input",
    "build_job_policy",
    "build_training_job_input",
    "compute_execution_timeout",
    "validate_execution_timeout",
]

# --- Timeout constants -------------------------------------------------------

# Handler deadline (QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS default).
DEFAULT_DEADLINE_S = 1800

# Slack added on top of the deadline so the handler's signed failure
# envelope always fires before RunPod kills the job.
DEFAULT_SLACK_S = 60

# Minimum allowed executionTimeout — the hard floor from the skill file.
MIN_EXECUTION_TIMEOUT_S = 1860

# Default idle timeout (seconds) — how long a worker stays warm with no jobs.
DEFAULT_IDLE_TIMEOUT_S = 300

# Default mount path for a RunPod network volume.
DEFAULT_VOLUME_MOUNT_PATH = "/runpod-volume"


def compute_execution_timeout(
    deadline_s: int = DEFAULT_DEADLINE_S,
    slack_s: int = DEFAULT_SLACK_S,
) -> int:
    """Return the endpoint ``executionTimeout`` (deadline + slack).

    Always >= ``MIN_EXECUTION_TIMEOUT_S`` (1860). If a caller passes a
    smaller deadline+slack, the floor is enforced so the hard rule is
    never violated.
    """
    computed = deadline_s + slack_s
    return max(computed, MIN_EXECUTION_TIMEOUT_S)


def validate_execution_timeout(timeout_s: int) -> int:
    """Validate that an ``executionTimeout`` meets the >= 1860s hard rule.

    Raises ``ValueError`` if the timeout is below the minimum. Returns
    the timeout unchanged when valid.
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


# --- Config dataclass --------------------------------------------------------


class EndpointConfig:
    """Configuration for a RunPod serverless endpoint.

    The ``executionTimeout`` defaults to ``compute_execution_timeout()``
    which is always >= 1860s. Callers can override but
    ``validate_execution_timeout`` is enforced in ``build_endpoint_input``.

    Network volume support (Tier 1A): when ``network_volume_id`` is set,
    the endpoint input includes ``networkVolumeId`` so the worker's
    network volume is attached. ``volume_in_gb`` and
    ``volume_mount_path`` are recorded on the template (via
    ``build_template_input`` in the probe scripts) and echoed here for
    receipt/audit purposes.
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
        volume_in_gb: int = 0,
        volume_mount_path: str = DEFAULT_VOLUME_MOUNT_PATH,
    ) -> None:
        self.name = name
        self.template_id = template_id
        self.gpu_ids = gpu_ids
        self.workers_min = workers_min
        self.workers_max = workers_max
        self.idle_timeout = idle_timeout
        self.execution_timeout = (
            execution_timeout if execution_timeout is not None else compute_execution_timeout()
        )
        self.scaler_type = scaler_type
        self.scaler_value = scaler_value
        self.container_disk_gb = container_disk_gb
        self.network_volume_id = network_volume_id
        self.volume_in_gb = volume_in_gb
        self.volume_mount_path = volume_mount_path


# --- Input builders ----------------------------------------------------------


def build_endpoint_input(config: EndpointConfig) -> dict[str, Any]:
    """Build the ``EndpointInput`` dict for the RunPod GraphQL mutation.

    Always includes ``executionTimeout`` (>= 1860s, validated) so RunPod
    never inherits its 600s default.

    When ``config.network_volume_id`` is set, includes
    ``networkVolumeId`` so the worker's network volume is attached to
    the endpoint. The ``volumeInGb`` and ``volumeMountPath`` fields are
    also included so the endpoint create receipt records the volume
    configuration (the template-level ``volumeInGb`` is set by
    ``build_template_input`` in the probe scripts).
    """
    validated = validate_execution_timeout(config.execution_timeout)
    inp: dict[str, Any] = {
        "name": config.name,
        "templateId": config.template_id,
        "gpuIds": config.gpu_ids,
        "workersMin": config.workers_min,
        "workersMax": config.workers_max,
        "idleTimeout": config.idle_timeout,
        "executionTimeout": validated,
        "scalerType": config.scaler_type,
        "scalerValue": config.scaler_value,
    }
    if config.network_volume_id:
        inp["networkVolumeId"] = config.network_volume_id
        inp["volumeInGb"] = config.volume_in_gb
        inp["volumeMountPath"] = config.volume_mount_path
    return inp


def build_job_policy(
    execution_timeout_s: int | None = None,
    *,
    ttl_s: int | None = None,
    low_priority: bool = False,
) -> dict[str, Any]:
    """Build the per-request ``policy`` dict for a RunPod ``/run`` or
    ``/runsync`` request.

    RunPod's documented way to override the execution timeout per job is
    the ``policy.executionTimeout`` field in the request body (see
    https://docs.runpod.io/serverless/endpoints/send-requests#execution-policies).
    The value is in **milliseconds**, not seconds. The endpoint-level
    ``executionTimeout`` in ``build_endpoint_input`` is kept as a
    best-effort undocumented field — the per-request policy is the
    reliable, documented path.

    Args:
        execution_timeout_s: Execution timeout in **seconds**. Defaults
            to ``compute_execution_timeout()`` (>= 1860). Converted to ms.
        ttl_s: Optional job TTL in seconds. Converted to ms.
        low_priority: If True, the job won't trigger worker scaling.

    Returns:
        Dict suitable for inclusion as the ``policy`` key in a ``/run``
        or ``/runsync`` request body::

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


def build_training_job_input(
    request: Any,
    *,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the RunPod ``/run`` job ``input`` dict from a
    :class:`RunPodTrainingRequest`.

    Serializes the request to a JSON-safe dict and ensures
    ``presigned_artifact_url`` is included when present. The worker's
    ``handler.py`` pops ``presigned_artifact_url`` from the input before
    validating the remainder as a ``RunPodTrainingRequest``, so this
    field travels as a top-level key in the input dict.

    Args:
        request: a ``RunPodTrainingRequest`` (Pydantic model) or a dict
            with the same shape.
        extra_fields: optional additional top-level keys to merge into
            the input dict (e.g. ``output_prefix``, ``dataset_load_spec``).

    Returns:
        A JSON-safe dict suitable for the ``input`` key of a RunPod
        ``/run`` request body.
    """
    if hasattr(request, "model_dump"):
        data: dict[str, Any] = request.model_dump(mode="json")
    elif isinstance(request, dict):
        data = dict(request)
    else:
        raise TypeError(
            f"request must be a RunPodTrainingRequest or dict, got "
            f"{type(request).__name__}"
        )
    if extra_fields:
        data.update(extra_fields)
    # Ensure presigned_artifact_url is present as a top-level key. The
    # worker handler pops it before validating the rest. When None it is
    # still included (the handler treats absence and None identically via
    # dict.pop(..., None), but including it makes the contract explicit
    # and testable).
    if "presigned_artifact_url" not in data:
        data["presigned_artifact_url"] = None
    return data
