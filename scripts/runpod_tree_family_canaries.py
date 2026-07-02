from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from typing import Any

import requests

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

from quant_foundry.signatures import verify_callback  # noqa: E402

BASE_URL = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")
DEFAULT_FAMILIES = ("catboost_gpu", "xgboost_gpu", "tabm_gpu")
EXPECTED_FAILURES = {"tabm_gpu": "model_family_not_routed"}


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _inline_csv() -> str:
    rows = [
        "ts,f1,f2,label",
        "1,0.10,1.00,0",
        "2,0.20,0.90,0",
        "3,0.35,0.80,0",
        "4,0.45,0.70,1",
        "5,0.60,0.40,1",
        "6,0.75,0.30,1",
        "7,0.85,0.20,1",
        "8,0.95,0.10,1",
    ]
    return "\n".join(rows) + "\n"


def _route_extra() -> dict[str, str]:
    return {
        "training_mode": "canary",
        "allow_cpu_fallback": "1",
        "gpu_required": "1",
        "column_roles": json.dumps(
            {
                "feature_columns": ["f1", "f2"],
                "label_columns": ["label"],
                "timestamp_column": "ts",
            },
            sort_keys=True,
        ),
        "task_spec": json.dumps(
            {
                "task_type": "binary",
                "label_column": "label",
            },
            sort_keys=True,
        ),
    }


def _dispatch(api_key: str, endpoint_id: str, payload: dict[str, Any]) -> str:
    response = requests.post(
        f"{BASE_URL}/{endpoint_id}/run",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"input": payload},
        timeout=120,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"RunPod dispatch failed: HTTP {response.status_code}: {response.text[:500]}"
        )
    runpod_job_id = response.json().get("id")
    if not runpod_job_id:
        raise RuntimeError(f"RunPod dispatch response did not include an id: {response.text[:500]}")
    return str(runpod_job_id)


def _poll(api_key: str, endpoint_id: str, runpod_job_id: str, timeout_s: int) -> dict[str, Any]:
    started_at = time.time()
    last_status = None
    while time.time() - started_at < timeout_s:
        response = requests.get(
            f"{BASE_URL}/{endpoint_id}/status/{runpod_job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        if response.status_code != 200:
            time.sleep(3)
            continue
        body = response.json()
        status = body.get("status", "UNKNOWN")
        if status != last_status:
            print(f"  {runpod_job_id}: {status}")
            last_status = status
        if status == "COMPLETED":
            return body.get("output") or {}
        if status == "FAILED":
            return body.get("output") or body
        time.sleep(3)
    raise TimeoutError(f"RunPod job {runpod_job_id} timed out after {timeout_s}s")


def _verify_success(output: dict[str, Any], job_id: str, callback_secret: str) -> dict[str, Any]:
    callback_payload = output.get("callback_payload")
    callback_signature = output.get("callback_signature")
    callback_ts = int(output.get("callback_ts") or 0)
    if not callback_payload or not callback_signature or not callback_ts:
        raise RuntimeError(f"{job_id} did not return a complete signed callback")
    valid = verify_callback(
        callback_payload.encode("utf-8"),
        signature=callback_signature,
        secret=callback_secret,
        ts=callback_ts,
        job_id=job_id,
    )
    if not valid:
        raise RuntimeError(f"{job_id} returned an invalid callback signature")
    envelope = json.loads(callback_payload)
    payload = envelope.get("payload", {})
    dossier = payload.get("dossier", {})
    artifact = payload.get("artifact_manifest", {})
    return {
        "signature_valid": valid,
        "trainer": (dossier.get("metadata") or {}).get("trainer"),
        "backend": (dossier.get("metadata") or {}).get("backend"),
        "artifact_id": artifact.get("artifact_id"),
        "artifact_size": artifact.get("size_bytes"),
        "model_family": artifact.get("model_family"),
    }


def _job_input(family: str) -> dict[str, Any]:
    job_id = f"runpod-{family}-canary-{int(time.time())}"
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "inline://tree-family-canary.csv",
        "model_family": family,
        "random_seed": 11,
        "hardware_class": "runpod-gpu-canary",
        "search_space": {
            "n_estimators": [4],
            "max_depth": [2],
            "learning_rate": [0.2],
        },
        "extra_constraints": _route_extra(),
        "inline_dataset_csv": _inline_csv(),
        "n_folds": 2,
    }


def run_canaries(families: list[str], timeout_s: int) -> list[dict[str, Any]]:
    api_key = _required_env("RUNPOD_API_KEY")
    endpoint_id = os.environ.get("RUNPOD_TRAINING_ENDPOINT_ID") or os.environ.get(
        "RUNPOD_ENDPOINT_ID"
    )
    if not endpoint_id:
        raise RuntimeError("RUNPOD_TRAINING_ENDPOINT_ID or RUNPOD_ENDPOINT_ID is required")
    callback_secret = _required_env("QUANT_FOUNDRY_CALLBACK_SECRET")
    results: list[dict[str, Any]] = []
    print(f"Endpoint: {endpoint_id}")
    for family in families:
        payload = _job_input(family)
        job_id = payload["job_id"]
        print(f"\nDispatching {family}: {job_id}")
        runpod_job_id = _dispatch(api_key, endpoint_id, payload)
        output = _poll(api_key, endpoint_id, runpod_job_id, timeout_s)
        expected_failure = EXPECTED_FAILURES.get(family)
        error_code = output.get("error_code")
        if expected_failure:
            if error_code != expected_failure:
                raise RuntimeError(
                    f"{family} expected {expected_failure}, got {error_code or sorted(output)}"
                )
            result = {
                "family": family,
                "job_id": job_id,
                "runpod_job_id": runpod_job_id,
                "status": "expected_fail_closed",
                "error_code": error_code,
                "error_summary": output.get("error_summary"),
            }
        else:
            if error_code:
                raise RuntimeError(f"{family} failed: {error_code}: {output.get('error_summary')}")
            verified = _verify_success(output, job_id, callback_secret)
            result = {
                "family": family,
                "job_id": job_id,
                "runpod_job_id": runpod_job_id,
                "status": "passed",
                **verified,
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        results.append(result)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--families", nargs="+", default=list(DEFAULT_FAMILIES))
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--output",
        default=str(_REPO_ROOT / "data" / "runpod_family_canaries" / "results.json"),
    )
    args = parser.parse_args()
    results = run_canaries(args.families, args.timeout)
    output_path = pathlib.Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nWrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
