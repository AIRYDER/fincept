"""Live minimal train_model job (A7) for the RunPod training worker.

Proves the full training pipeline live: dataset loading -> trainer.fit
(RealLightGBMTrainer walk-forward + final fit) -> model export (artifact
write with sha256 verification + signed write receipt).

The canary path (6/6 PASSED) and gpu_healthcheck (PASSED) prove the
container boots, the handler runs, and the GPU is accessible. They do NOT
exercise actual training. This tool dispatches a minimal implicit
train_model job (no ``task`` field — the RunPodTrainingRequest schema
forbids extra fields, and a missing task is the implicit training
dispatch) with a tiny inline synthetic dataset.

Pipeline exercised on the worker:
- inline_dataset_csv -> temp CSV -> RealLightGBMTrainer._load_csv
- walk-forward validation folds (fincept_core.datasets.cv.make_folds)
- lightgbm.train per fold + final model fit
- pickle export -> VolumeArtifactWriter (output_prefix) -> sha256
  re-verification + HMAC write receipt

The image sets QUANT_FOUNDRY_USE_REAL_TRAINER=true (Dockerfile ENV), so
no extra endpoint env is needed beyond the canary template shape.

Reuses the RunPod API helpers from ``run_live_canary.py`` (same GraphQL
endpoint, same bearer auth, same redaction, same endpoint/template shape).

Usage:
    # local pipeline smoke (no cloud spend, requires lightgbm+numpy):
    uv run python runpod/quant-foundry-training/run_train_model.py --local

    # live dispatch against the exact-SHA production image:
    python runpod/quant-foundry-training/run_train_model.py --sha <full-sha>
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

# Shared lifecycle helpers (unique naming, retry cleanup, timeout config).
import sys as _sys
import time
from pathlib import Path
from pathlib import Path as _Path
from typing import Any

# Reuse the validated RunPod API helpers from the canary tool.
from run_live_canary import (
    CONTAINER_DISK_GB,
    EXECUTION_TIMEOUT,
    GPU_TYPE,
    IDLE_TIMEOUT,
    POLL_INTERVAL_S,
    REGISTRY_AUTH_ID,
    SCALER_TYPE,
    SCALER_VALUE,
    WORKERS_MAX,
    WORKERS_MIN,
    _redact,
    create_endpoint,
    delete_endpoint,
    get_endpoint_health,
    get_job_status,
    run_job,
    save_template,
    update_endpoint_workers,
)

_REPO_ROOT = _Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)

from runpod.runpod_lifecycle import (  # noqa: E402
    format_timeout_receipt,
    make_unique_name,
    retry_delete_endpoint,
    safe_scale_to_zero,
)

# Training can take longer than the 44-50ms canary; give the probe 300s.
TRAIN_PROBE_TIMEOUT_S = 300

# A fresh host must cold-pull the ~6 GB image (torch cu124). The first A7
# attempt spent 155s+ in initializing and missed the canary tool's 180s
# ready window. Give the trainer worker 600s to become ready.
TRAIN_READY_TIMEOUT_S = 600

# Synthetic dataset shape. 300 rows / n_folds=2 satisfies make_folds with
# the default horizon=15/purge=15: min_train=75, purge_budget=30,
# fold_size=(300-75-30)//2=97 -> 75 + 2*(15+97) = 299 <= 300.
DATASET_ROWS = 300
DATASET_SEED = 42
N_FOLDS = 2


def delete_template(template_name: str) -> None:
    """Best-effort template deletion (cleanup after the run).

    NOTE: RunPod's ``deleteTemplate`` mutation takes the template NAME,
    not the template id returned by ``saveTemplate``.
    """
    from run_live_canary import _gql

    _gql(
        "mutation DeleteTemplate($name: String!) { deleteTemplate(templateName: $name) }",
        {"name": template_name},
    )


def build_synthetic_csv(rows: int = DATASET_ROWS, seed: int = DATASET_SEED) -> str:
    """Build a tiny deterministic synthetic dataset as CSV text.

    Legacy loader layout (RealLightGBMTrainer._load_csv): header row,
    first column = timestamp, middle columns = features, last column =
    label (binary 0/1). The label is a noisy linear function of the
    features so lightgbm has real signal to fit.
    """
    rng = random.Random(seed)
    base_ts = 1_700_000_000_000_000_000  # ns epoch, strictly increasing
    lines = ["timestamp,f1,f2,f3,label"]
    for i in range(rows):
        f1 = rng.gauss(0.0, 1.0)
        f2 = rng.gauss(0.0, 1.0)
        f3 = rng.gauss(0.0, 1.0)
        noise = rng.gauss(0.0, 0.3)
        label = 1 if (0.8 * f1 - 0.5 * f2 + 0.3 * f3 + noise) > 0 else 0
        ts = base_ts + i * 60_000_000_000  # 1-minute bars
        lines.append(f"{ts},{f1:.6f},{f2:.6f},{f3:.6f},{label}")
    return "\n".join(lines) + "\n"


def build_train_input(
    job_id: str,
    *,
    model_family: str = "lightgbm",
    output_prefix: str = "/tmp/a7-train-artifacts",  # noqa: S108 - worker-side tmp dir
) -> dict[str, Any]:
    """Build the minimal implicit train_model payload.

    NOTE: no ``task`` field — RunPodTrainingRequest forbids extra fields
    and a missing task is the implicit training dispatch (handler
    normalizes None -> train_model). ``inline_dataset_csv``, ``n_folds``
    and ``output_prefix`` are handler-level extensions popped before
    schema validation. Mode goes in extra_constraints.training_mode
    (canary: permissive, no FoldSpec required, no production fail-closed
    gates — this is a pipeline proof, not a promotion candidate).

    For non-lightgbm families (xgboost, xgboost_gpu, catboost), the handler
    requires explicit ``column_roles`` and ``task_spec`` in
    ``extra_constraints`` (passed as JSON strings since the schema types
    ``extra_constraints`` as ``dict[str, str]``). The handler's
    ``_json_mapping_from_extra`` parses them back to dicts.
    """
    extra: dict[str, Any] = {"training_mode": "canary"}
    search_space: dict[str, list[Any]] = {}
    if model_family != "lightgbm":
        # Non-lightgbm families require explicit column_roles + task_spec.
        # The synthetic dataset has columns: timestamp, f1, f2, f3, label.
        extra["column_roles"] = json.dumps({
            "feature_columns": ["f1", "f2", "f3"],
            "label_columns": ["label"],
            "timestamp_column": "timestamp",
        })
        extra["task_spec"] = json.dumps({
            "task_type": "binary",
            "label_column": "label",
        })
    if model_family in ("xgboost", "xgboost_gpu"):
        search_space = {
            "max_depth": [3],
            "learning_rate": [0.1],
            "n_estimators": [50],
        }
    return {
        "job_id": job_id,
        "dataset_manifest_ref": "inline://a7-train-model-proof",
        "model_family": model_family,
        "random_seed": DATASET_SEED,
        "search_space": search_space,
        "extra_constraints": extra,
        # handler-level extensions (popped before schema validation):
        "inline_dataset_csv": build_synthetic_csv(),
        "n_folds": N_FOLDS,
        "output_prefix": output_prefix,
    }


def extract_train_evidence(final_output: dict[str, Any] | None) -> dict[str, Any] | None:
    """Extract the training-pipeline evidence from the job output.

    Returns a dict with the artifact result (uri/sha256/size/format +
    write receipt), the metrics summary from the typed callback, and the
    preflight/callback flags — or None when no output is present.
    """
    if not final_output:
        return None
    output_field = final_output.get("output")
    if isinstance(output_field, str):
        try:
            output_field = json.loads(output_field)
        except json.JSONDecodeError:
            return None
    if not isinstance(output_field, dict):
        return None
    typed_callback = output_field.get("typed_callback") or {}
    preflight = output_field.get("preflight_result") or {}
    return {
        "job_id": output_field.get("job_id"),
        "artifact_id": output_field.get("artifact_id"),
        "dossier_id": output_field.get("dossier_id"),
        "artifact_result": output_field.get("artifact_result"),
        "artifact_write_receipt_present": bool(output_field.get("artifact_write_receipt")),
        "callback_signature_present": bool(output_field.get("callback_signature")),
        "metrics_summary": typed_callback.get("metrics_summary"),
        "preflight_passed": preflight.get("passed"),
        "output_prefix": output_field.get("output_prefix"),
    }


def check_acceptance(evidence: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """A7 acceptance: dataset load + trainer.fit + model export proven.

    - artifact_result present with a non-empty artifact_uri, sha256 and
      size (model export happened and was re-verified by the writer).
    - metrics_summary present (walk-forward validation ran -> trainer.fit
      on real data, which implies dataset loading succeeded).
    - callback signature present (signed contract intact).
    """
    problems: list[str] = []
    if evidence is None:
        return False, ["no parsable job output"]
    art = evidence.get("artifact_result")
    if not isinstance(art, dict):
        problems.append("artifact_result missing (model export not proven)")
    else:
        if not art.get("artifact_uri"):
            problems.append("artifact_uri empty (artifact not persisted)")
        if not art.get("artifact_sha256"):
            problems.append("artifact_sha256 missing")
        if not art.get("artifact_size_bytes"):
            problems.append("artifact_size_bytes missing/zero")
        if not art.get("write_receipt"):
            problems.append("write_receipt missing (writer did not sign the artifact)")
    metrics = evidence.get("metrics_summary")
    if not metrics:
        problems.append("metrics_summary missing (trainer.fit/validation not proven)")
    if not evidence.get("callback_signature_present"):
        problems.append("callback_signature missing")
    return (not problems), problems


def run_local_smoke(model_family: str = "lightgbm") -> int:
    """Run the exact train_model payload through the handler in-process.

    No cloud spend. Requires lightgbm + numpy locally (uv workspace).
    QUANT_FOUNDRY_USE_REAL_TRAINER is forced on to match the image env.
    """
    import tempfile

    secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not secret:
        print("ERROR: QUANT_FOUNDRY_CALLBACK_SECRET not set")
        return 1
    os.environ["QUANT_FOUNDRY_USE_REAL_TRAINER"] = "true"

    import handler as handler_mod

    out_dir = tempfile.mkdtemp(prefix="a7_local_artifacts_")
    payload = build_train_input(
        f"qf:a7-train:local:{model_family}:001",
        model_family=model_family,
        output_prefix=out_dir,
    )
    started = time.monotonic()
    result = handler_mod.handler({"input": payload})
    elapsed = time.monotonic() - started

    evidence = extract_train_evidence({"output": result})
    ok, problems = check_acceptance(evidence)
    print(f"local smoke elapsed: {elapsed:.1f}s")
    print(json.dumps(_redact(evidence), indent=2, sort_keys=True, default=str))
    if not ok:
        print(f"LOCAL SMOKE FAILED: {problems}")
        if result.get("error_code"):
            print(f"  error_code={result.get('error_code')}")
            print(f"  error_summary={result.get('error_summary')}")
        return 1
    print("LOCAL SMOKE PASSED (dataset load + trainer.fit + model export)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live minimal train_model job (A7)")
    parser.add_argument("--sha", default=None, help="Full git SHA for the image tag")
    parser.add_argument("--template-id", default=None, help="Existing template ID to reuse")
    parser.add_argument("--image-tag", default=None, help="Full image tag (overrides --sha)")
    parser.add_argument(
        "--model-family",
        default="lightgbm",
        help="Model family for the training job (default: lightgbm). "
        "Non-lightgbm families (xgboost, xgboost_gpu, catboost) require "
        "explicit column_roles + task_spec, which this tool auto-includes "
        "for the synthetic canary dataset.",
    )
    parser.add_argument(
        "--receipt-subdir",
        default=None,
        help="Receipt subdirectory under reports/runpod-test-runs/<sha8>/ "
        "(default: train-model for lightgbm, train-model-<family> otherwise)",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run the payload through the handler in-process (no cloud spend)",
    )
    args = parser.parse_args()

    model_family = args.model_family
    receipt_subdir = args.receipt_subdir or (
        "train-model" if model_family == "lightgbm" else f"train-model-{model_family}"
    )

    if args.local:
        return run_local_smoke(model_family=model_family)

    if not args.sha:
        print("ERROR: --sha is required for live dispatch")
        return 1

    sha = args.sha
    image_tag = args.image_tag or f"ghcr.io/airyder/fincept/quant-foundry-training:{sha}"
    callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
    if not callback_secret:
        print("ERROR: QUANT_FOUNDRY_CALLBACK_SECRET not set")
        return 1

    receipt_dir = Path(f"reports/runpod-test-runs/{sha[:8]}/{receipt_subdir}")
    receipt_dir.mkdir(parents=True, exist_ok=True)

    print("Live Minimal train_model Job (A7)")
    print(f"  SHA: {sha}")
    print(f"  Image: {image_tag}")
    print(f"  GPU: {GPU_TYPE}")
    print(f"  Model family: {model_family}")
    print(f"  Receipts: {receipt_dir}")
    print()

    # Create or reuse template (same shape as the canary/gpu-healthcheck runs)
    created_template = False
    template_name = ""
    if args.template_id:
        template_id = args.template_id
        print(f"  Reusing template: {template_id}")
    else:
        # Unique per run — RunPod requires unique template names and the
        # template is deleted best-effort in cleanup.
        template_name = make_unique_name("qf-a7train", sha, suffix="tpl")
        env_vars = [
            {"key": "PYTHONUNBUFFERED", "value": "1"},
            {"key": "PYTHONPATH", "value": "/worker"},
            {"key": "QUANT_FOUNDRY_GIT_SHA", "value": sha},
            {"key": "QUANT_FOUNDRY_CALLBACK_SECRET", "value": callback_secret},
        ]
        template_id = save_template(template_name, image_tag, env_vars, REGISTRY_AUTH_ID)
        created_template = True
        print(f"  Template created: {template_id}")

    (receipt_dir / "template-redacted.txt").write_text(
        f"Template ID: {template_id}\nImage: {image_tag}\nGPU: {GPU_TYPE}\n",
        encoding="utf-8",
    )

    # Create endpoint
    endpoint_name = make_unique_name("qf-a7train", sha)
    endpoint_id = create_endpoint(endpoint_name, template_id)
    print(f"  Endpoint created: {endpoint_id}")

    (receipt_dir / "endpoint-create-redacted.json").write_text(
        json.dumps(
            _redact(
                {
                    "endpoint_id": endpoint_id,
                    "name": endpoint_name,
                    "template_id": template_id,
                    "gpu_type": GPU_TYPE,
                    "workers_min": WORKERS_MIN,
                    "workers_max": WORKERS_MAX,
                    "idle_timeout": IDLE_TIMEOUT,
                    "execution_timeout": EXECUTION_TIMEOUT,
                    "scaler_type": SCALER_TYPE,
                    "scaler_value": SCALER_VALUE,
                    "container_disk_gb": CONTAINER_DISK_GB,
                    "timeout_config": format_timeout_receipt(EXECUTION_TIMEOUT, IDLE_TIMEOUT),
                }
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    job_id: str | None = None
    final_status = "UNKNOWN"
    try:
        # Wait for ready
        print(f"  Waiting for ready (timeout={TRAIN_READY_TIMEOUT_S}s)...")
        health_before = None
        workers: dict[str, Any] = {}
        health: dict[str, Any] = {}
        for i in range(TRAIN_READY_TIMEOUT_S // POLL_INTERVAL_S):
            health = get_endpoint_health(endpoint_id)
            workers = health.get("workers", {})
            ready = workers.get("ready", 0)
            unhealthy = workers.get("unhealthy", 0)
            print(
                f"    [{i * POLL_INTERVAL_S}] ready={ready} idle={workers.get('idle', 0)} "
                f"running={workers.get('running', 0)} unhealthy={unhealthy} "
                f"initializing={workers.get('initializing', 0)}"
            )
            if ready >= 1 and unhealthy == 0:
                health_before = health
                break
            if unhealthy > 0:
                print("  FAIL: worker went unhealthy before dispatch")
                (receipt_dir / "health-before.json").write_text(
                    json.dumps(_redact(health), indent=2, sort_keys=True), encoding="utf-8"
                )
                return 1
            time.sleep(POLL_INTERVAL_S)
        else:
            print(f"  FAIL: worker not ready after {TRAIN_READY_TIMEOUT_S}s")
            (receipt_dir / "health-before.json").write_text(
                json.dumps(_redact(health), indent=2, sort_keys=True), encoding="utf-8"
            )
            return 1

        print(
            f"  Health before: ready={workers.get('ready', 0)} "
            f"idle={workers.get('idle', 0)} unhealthy={workers.get('unhealthy', 0)}"
        )
        (receipt_dir / "health-before.json").write_text(
            json.dumps(_redact(health_before), indent=2, sort_keys=True), encoding="utf-8"
        )

        # Dispatch the implicit train_model job
        train_input = build_train_input(
            f"qf:a7-train:{model_family}:{sha[:8]}:001",
            model_family=model_family,
        )
        job_id = run_job(endpoint_id, train_input)
        print(f"  Job dispatched: {job_id}")
        run_receipt = dict(train_input)
        # Keep the receipt small + readable: record the dataset shape, not
        # the full 300-row CSV body.
        csv_text = run_receipt.pop("inline_dataset_csv")
        run_receipt["inline_dataset_rows"] = DATASET_ROWS
        run_receipt["inline_dataset_bytes"] = len(csv_text.encode("utf-8"))
        run_receipt["inline_dataset_header"] = csv_text.splitlines()[0]
        (receipt_dir / "run-response.json").write_text(
            json.dumps(_redact({"job_id": job_id, "input": run_receipt}), indent=2),
            encoding="utf-8",
        )

        # Poll until terminal
        probe_log: list[dict[str, Any]] = []
        final_output: dict[str, Any] | None = None
        for i in range(TRAIN_PROBE_TIMEOUT_S // POLL_INTERVAL_S):
            health = get_endpoint_health(endpoint_id)
            w = health.get("workers", {})
            status_resp = get_job_status(endpoint_id, job_id)
            job_status = status_resp.get("status", "UNKNOWN")
            final_status = job_status

            probe_log.append(
                {
                    "event": "poll",
                    "job_id": job_id,
                    "status": job_status,
                    "health": _redact(health),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )

            print(
                f"  [{i * POLL_INTERVAL_S}] status={job_status} "
                f"ready={w.get('ready', 0)} running={w.get('running', 0)} "
                f"unhealthy={w.get('unhealthy', 0)} "
                f"inQueue={health.get('jobs', {}).get('inQueue', 0)} "
                f"completed={health.get('jobs', {}).get('completed', 0)}"
            )

            if w.get("unhealthy", 0) > 0:
                print("  FAIL: worker went unhealthy")
                final_output = status_resp
                break

            if job_status == "COMPLETED":
                print("  PASS: job COMPLETED")
                final_output = status_resp
                break

            if job_status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                print(f"  FAIL: job {job_status}")
                final_output = status_resp
                break

            time.sleep(POLL_INTERVAL_S)
        else:
            print(f"  FAIL: probe timed out (job stuck in {final_status})")
            final_output = get_job_status(endpoint_id, job_id) if job_id else None

        # Write probe log and final status
        (receipt_dir / "probe.jsonl").write_text(
            "\n".join(json.dumps(e) for e in probe_log) + "\n",
            encoding="utf-8",
        )
        (receipt_dir / "status-final.json").write_text(
            json.dumps(_redact(final_output), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Extract and check the training-pipeline evidence
        evidence = extract_train_evidence(final_output)
        accepted = False
        problems: list[str] = []
        if evidence is not None:
            (receipt_dir / "train-model-result.json").write_text(
                json.dumps(_redact(evidence), indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            accepted, problems = check_acceptance(evidence)
            art = evidence.get("artifact_result") or {}
            print(f"  Artifact URI:  {art.get('artifact_uri')}")
            print(f"  Artifact sha:  {art.get('artifact_sha256')}")
            print(f"  Artifact size: {art.get('artifact_size_bytes')} bytes")
            print(f"  Write receipt: {'present' if art.get('write_receipt') else 'MISSING'}")
            print(f"  Metrics:       {evidence.get('metrics_summary')}")
        else:
            print("  WARNING: no train_model evidence in job output")
            problems = ["no parsable job output"]

        # Final health
        health_after = get_endpoint_health(endpoint_id)
        print(
            f"  Health after: ready={health_after.get('workers', {}).get('ready', 0)} "
            f"unhealthy={health_after.get('workers', {}).get('unhealthy', 0)}"
        )
        (receipt_dir / "health-after.json").write_text(
            json.dumps(_redact(health_after), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Result
        if final_status == "COMPLETED" and accepted:
            print("\n  TRAIN_MODEL PASSED (dataset load + trainer.fit + model export)")
            return 0
        print(f"\n  TRAIN_MODEL FAILED (final_status={final_status}, problems={problems})")
        return 1

    finally:
        # Cancel stuck job if not terminal
        if job_id and final_status not in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            try:
                api_key = os.environ.get("RUNPOD_API_KEY", "")
                import urllib.request

                url = f"https://api.runpod.ai/v2/{endpoint_id}/cancel/{job_id}"
                req = urllib.request.Request(
                    url,
                    method="POST",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    cancel_resp = json.loads(resp.read())
                (receipt_dir / "cancel.json").write_text(
                    json.dumps(_redact(cancel_resp), indent=2), encoding="utf-8"
                )
                print(f"  Cancelled stuck job: {job_id}")
            except Exception as e:
                print(f"  WARNING: could not cancel job {job_id}: {e}")

        # Scale down and delete
        scaled_down = safe_scale_to_zero(
            endpoint_id,
            update_endpoint_workers,
            logger=print,
        )
        # Endpoint deletion can fail transiently while the worker is
        # still spinning down after the job ("Failed to terminate
        # resources. Try again.") — retry a few times with a delay.
        endpoint_deleted = retry_delete_endpoint(
            endpoint_id,
            delete_endpoint,
            logger=print,
        )
        template_deleted = False
        if created_template:
            try:
                delete_template(template_name)
                template_deleted = True
                print("  Template deleted")
            except Exception as e:
                print(f"  WARNING: could not delete template: {e}")
        (receipt_dir / "cleanup.json").write_text(
            json.dumps(
                {
                    "endpoint_id": endpoint_id,
                    "scaled_down": scaled_down,
                    "deleted": endpoint_deleted,
                    "template_id": template_id,
                    "template_deleted": template_deleted,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    sys.exit(main())
