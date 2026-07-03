"""Deploy the CUDA test image and the layered training image to RunPod endpoints.

Creates two endpoints:
  1. cuda-test: minimal handler on pytorch CUDA base (isolates base image issue)
  2. training-layered: layered handler with lazy imports + QF_DIAG_LAYER=0

Then runs a Layer-0 smoke probe against each.

Usage:
    # After both GitHub Actions builds complete:
    uv run python scripts/runpod_deploy_diag_endpoints.py --git-sha be96d76b

    # Dry-run (print commands only, don't create endpoints):
    uv run python scripts/runpod_deploy_diag_endpoints.py --git-sha be96d76b --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any

IMAGE_PREFIX = "ghcr.io/airyder/fincept"

# Known endpoint IDs from previous session (for diagnostic reference).
PREV_TRAINING_ENDPOINT = "mxp0bv8itggwev"  # last known training endpoint
PREV_DIAG_ENDPOINT = "zbpy7m8s8dps7k"      # previous diagnostic endpoint


def _emit(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": _now(), "event": event, **fields}, sort_keys=True))
    sys.stdout.flush()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _run_script(script: str, args: list[str], dry_run: bool, cwd: str) -> int:
    """Run a Python script and stream output."""
    cmd = [sys.executable, script, *args]
    print(f"\n[deploy] {'DRY-RUN' if dry_run else 'RUNNING'}: {' '.join(cmd)}")
    if dry_run:
        return 0
    result = subprocess.run(cmd, cwd=cwd, capture_output=False)  # noqa: S603
    return result.returncode


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-sha", required=True, help="Git SHA for image tags")
    parser.add_argument(
        "--registry-auth-source-endpoint-id",
        default=os.environ.get("RUNPOD_REGISTRY_AUTH_SOURCE_ENDPOINT_ID", PREV_TRAINING_ENDPOINT),
        help="Existing endpoint to copy registry auth from.",
    )
    parser.add_argument(
        "--callback-secret",
        default=os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", ""),
        help="Callback secret for the training endpoint.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-cuda-test",
        action="store_true",
        help="Skip creating the CUDA test endpoint.",
    )
    parser.add_argument(
        "--skip-training",
        action="store_true",
        help="Skip creating the training endpoint.",
    )
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="Skip running smoke probes after endpoint creation.",
    )
    parser.add_argument(
        "--wait-health",
        action="store_true",
        default=True,
        help="Wait for endpoint health after creation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    sha = args.git_sha[:7] if len(args.git_sha) >= 7 else args.git_sha

    cuda_image = f"{IMAGE_PREFIX}/quant-foundry-cuda-test:{sha}"
    training_image = f"{IMAGE_PREFIX}/quant-foundry-training:{sha}"

    _emit(
        "deploy_start",
        git_sha=sha,
        cuda_image=cuda_image,
        training_image=training_image,
        dry_run=args.dry_run,
    )

    results: dict[str, Any] = {"git_sha": sha, "endpoints": {}}

    # --- 1. CUDA test endpoint ---
    if not args.skip_cuda_test:
        cuda_args = [
            "--image-tag", cuda_image,
            "--name", "fincept-qf-cuda-test",
            "--template-name", "fincept-qf-cuda-test-template",
            "--copy-registry-auth-from-endpoint-id", args.registry_auth_source_endpoint_id,
            "--gpu-ids", "ADA_24",
            "--workers-min", "0",
            "--workers-max", "1",
            "--container-disk-gb", "10",
            "--idle-timeout", "300",
        ]
        if args.wait_health:
            cuda_args.append("--wait-health")
        _emit("creating_cuda_test_endpoint", image=cuda_image)
        rc = _run_script(
            "scripts/runpod_create_smoke_endpoint.py",
            cuda_args,
            args.dry_run,
            os.getcwd(),
        )
        results["endpoints"]["cuda_test"] = {"rc": rc, "image": cuda_image}

    # --- 2. Training layered endpoint ---
    if not args.skip_training:
        training_args = [
            "--image-tag", training_image,
            "--name", "fincept-qf-training-layered-diag",
            "--template-name", "fincept-qf-training-layered-diag-template",
            "--copy-registry-auth-from-endpoint-id", args.registry_auth_source_endpoint_id,
            "--gpu-ids", "ADA_24",
            "--workers-min", "0",
            "--workers-max", "1",
            "--container-disk-gb", "20",
            "--idle-timeout", "300",
        ]
        # Inject diagnostic env vars
        training_args.extend(["--env", "QF_DIAG_LAYER=0"])
        training_args.extend(["--env", "QF_DIAG_SKIP_PREFLIGHT=1"])
        training_args.extend(["--env", "QF_DIAG_SKIP_CHOWN=1"])
        if args.callback_secret:
            training_args.extend(["--env", f"QUANT_FOUNDRY_CALLBACK_SECRET={args.callback_secret}"])
        if args.wait_health:
            training_args.append("--wait-health")
        _emit("creating_training_endpoint", image=training_image)
        rc = _run_script(
            "scripts/runpod_create_smoke_endpoint.py",
            training_args,
            args.dry_run,
            os.getcwd(),
        )
        results["endpoints"]["training"] = {"rc": rc, "image": training_image}

    # --- 3. Smoke probes ---
    if not args.skip_probe and not args.dry_run:
        # We need the endpoint IDs from the creation step.
        # The create script prints them; for now, use the diagnostic script.
        _emit(
            "probe_instructions",
            msg="Run these commands once endpoints are created:",
            cuda_test_cmd=f"uv run python scripts/runpod_smoke_probe.py --endpoint-id <CUDA_EP_ID> --image-tag {cuda_image}",
            training_cmd=f"uv run python scripts/runpod_smoke_probe.py --endpoint-id <TRAINING_EP_ID> --image-tag {training_image} --payload-json '{{\"input\": {{\"diag_layer\": 0}}}}'",
            diagnostic_cmd="uv run python scripts/runpod_endpoint_diagnostic.py --endpoint-id <EP_ID> --poll 10",
        )

    print(f"\n[deploy] results: {json.dumps(results, indent=2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
