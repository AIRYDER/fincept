#!/usr/bin/env python3
"""Rebuild + push RunPod serverless containers for Quant Foundry.

This script automates the rebuild of the two RunPod serverless containers
(``runpod/quant-foundry-training`` and ``runpod/quant-foundry-inference``)
after ML dependency updates (lightgbm, pyarrow, onnxruntime, numpy). It
builds the Docker images, optionally pushes them to a container registry,
and optionally triggers a RunPod endpoint refresh so the serverless
workers pick up the new image.

Usage (from repo root):

    # Dry run — print commands without executing
    python scripts/rebuild_runpod_containers.py --dry-run

    # Build both containers locally
    python scripts/rebuild_runpod_containers.py

    # Build + push to a registry
    python scripts/rebuild_runpod_containers.py --push --registry ghcr.io/fincept

    # Build + push + refresh RunPod endpoints
    python scripts/rebuild_runpod_containers.py --push --registry ghcr.io/fincept --refresh-endpoint

Safety invariants:
- ``--dry-run`` MUST NOT execute any Docker commands (only print them).
- No API keys are hardcoded. The RunPod API key comes from the
  ``RUNPOD_API_KEY`` env var (required only for ``--refresh-endpoint``).
- Errors are handled gracefully — build/push/API failures exit with a
  non-zero code and a clear message, never a raw traceback.

Exit codes:
- 0: success
- 1: precondition failure (Docker missing, Dockerfile missing, etc.)
- 2: build failure
- 3: push failure
- 4: endpoint refresh failure
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Repo root is the parent of the scripts/ directory.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Container definitions: name -> (dockerfile path relative to repo root,
# build context directory relative to repo root).
CONTAINERS: dict[str, tuple[str, str]] = {
    "training": (
        "runpod/quant-foundry-training/Dockerfile",
        "runpod/quant-foundry-training",
    ),
    "inference": (
        "runpod/quant-foundry-inference/Dockerfile",
        "runpod/quant-foundry-inference",
    ),
}

# RunPod endpoint IDs — import from shared config (single source of truth).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runpod_config import (  # noqa: E402
    INFERENCE_ENDPOINT_ID as DEFAULT_INFERENCE_ENDPOINT_ID,
)
from runpod_config import (
    TRAINING_ENDPOINT_ID as DEFAULT_TRAINING_ENDPOINT_ID,
)

# RunPod API base URL for endpoint refresh.
RUNPOD_API_BASE = "https://api.runpod.ai/v2"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class BuildResult:
    """Result of building a single container."""

    container: str
    image_tag: str
    success: bool
    duration_seconds: float = 0.0
    error: str | None = None
    command: str | None = None


@dataclass
class PushResult:
    """Result of pushing a single container."""

    container: str
    image_tag: str
    success: bool
    duration_seconds: float = 0.0
    error: str | None = None
    command: str | None = None


@dataclass
class RefreshResult:
    """Result of a RunPod endpoint refresh call."""

    endpoint_id: str
    success: bool
    status_code: int | None = None
    error: str | None = None


@dataclass
class Summary:
    """Aggregate summary of the rebuild run."""

    builds: list[BuildResult] = field(default_factory=list)
    pushes: list[PushResult] = field(default_factory=list)
    refreshes: list[RefreshResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def all_success(self) -> bool:
        return (
            all(b.success for b in self.builds)
            and all(p.success for p in self.pushes)
            and all(r.success for r in self.refreshes)
        )


# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------


class PreconditionError(Exception):
    """Raised when a precondition for the rebuild is not met."""


def check_docker_installed() -> None:
    """Check that Docker is installed and on PATH.

    Raises PreconditionError if Docker is not found.
    """
    if shutil.which("docker") is None:
        raise PreconditionError(
            "Docker is not installed or not on PATH. "
            "Install Docker Desktop (https://www.docker.com/products/docker-desktop) "
            "and ensure the 'docker' command is available."
        )


def check_docker_running(dry_run: bool = False) -> None:
    """Check that the Docker daemon is running.

    In dry-run mode, this is skipped (we cannot assume Docker is available
    in CI/preview environments).

    Raises PreconditionError if the daemon is not responding.
    """
    if dry_run:
        return
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise PreconditionError(
                "Docker daemon is not running. Start Docker Desktop or the "
                "Docker service before rebuilding containers.\n"
                f"docker info stderr: {result.stderr.strip()}"
            )
    except FileNotFoundError as exc:
        raise PreconditionError(
            f"Docker executable not found: {exc}. Ensure Docker is installed and on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PreconditionError(
            "Docker daemon did not respond within 30 seconds. "
            "It may be starting up — wait and retry."
        ) from exc


def check_dockerfile_exists(container: str) -> Path:
    """Check that the Dockerfile for the given container exists.

    Returns the absolute path to the Dockerfile.
    Raises PreconditionError if the file is missing.
    """
    if container not in CONTAINERS:
        raise PreconditionError(
            f"Unknown container '{container}'. Valid options: {', '.join(CONTAINERS.keys())}"
        )
    dockerfile_rel, _ = CONTAINERS[container]
    dockerfile = REPO_ROOT / dockerfile_rel
    if not dockerfile.is_file():
        raise PreconditionError(
            f"Dockerfile not found for container '{container}': "
            f"{dockerfile} (expected at {dockerfile_rel})"
        )
    return dockerfile


# ---------------------------------------------------------------------------
# Docker build / push
# ---------------------------------------------------------------------------


def build_container(
    container: str,
    tag: str,
    dry_run: bool = False,
) -> BuildResult:
    """Build a single container image.

    Args:
        container: one of 'training', 'inference'.
        tag: image tag (e.g. 'latest').
        dry_run: if True, print the command but do not execute it.

    Returns a BuildResult.
    """
    dockerfile_rel, _context_rel = CONTAINERS[container]
    image_name = f"fincept/quant-foundry-{container}:{tag}"

    # Build from the repo root so COPY paths in the Dockerfile resolve.
    # The Dockerfile uses paths like services/quant_foundry/src/... which
    # are relative to the repo root.
    cmd = [
        "docker",
        "build",
        "-t",
        image_name,
        "-f",
        dockerfile_rel,
        ".",
    ]
    cmd_str = " ".join(cmd)

    if dry_run:
        print(f"[dry-run] would build: {cmd_str}")
        print(f"[dry-run]   image: {image_name}")
        print(f"[dry-run]   context: {REPO_ROOT}")
        return BuildResult(
            container=container,
            image_tag=image_name,
            success=True,
            command=cmd_str,
        )

    print(f"Building {image_name} ...")
    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min max for a build
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return BuildResult(
            container=container,
            image_tag=image_name,
            success=False,
            duration_seconds=elapsed,
            error=f"Docker build timed out after {elapsed:.0f}s",
            command=cmd_str,
        )

    elapsed = time.time() - start
    if result.returncode != 0:
        # Print the tail of the build log for debugging.
        stderr_tail = result.stderr[-2000:] if result.stderr else ""
        stdout_tail = result.stdout[-2000:] if result.stdout else ""
        return BuildResult(
            container=container,
            image_tag=image_name,
            success=False,
            duration_seconds=elapsed,
            error=(
                f"Docker build failed (exit {result.returncode}).\n"
                f"--- stdout (tail) ---\n{stdout_tail}\n"
                f"--- stderr (tail) ---\n{stderr_tail}"
            ),
            command=cmd_str,
        )

    print(f"  built in {elapsed:.1f}s")
    return BuildResult(
        container=container,
        image_tag=image_name,
        success=True,
        duration_seconds=elapsed,
        command=cmd_str,
    )


def push_container(
    container: str,
    image_tag: str,
    registry: str,
    tag: str,
    dry_run: bool = False,
) -> PushResult:
    """Tag and push a container image to a registry.

    Args:
        container: one of 'training', 'inference'.
        image_tag: the local image tag (e.g. 'fincept/quant-foundry-training:latest').
        registry: the registry URL (e.g. 'ghcr.io/fincept').
        tag: the image tag to push (e.g. 'latest').
        dry_run: if True, print commands but do not execute.

    Returns a PushResult.
    """
    # The remote image name: <registry>/fincept/quant-foundry-<container>:<tag>
    remote_name = f"{registry}/fincept/quant-foundry-{container}:{tag}"

    tag_cmd = ["docker", "tag", image_tag, remote_name]
    push_cmd = ["docker", "push", remote_name]
    tag_str = " ".join(tag_cmd)
    push_str = " ".join(push_cmd)

    if dry_run:
        print(f"[dry-run] would tag:   {tag_str}")
        print(f"[dry-run] would push:  {push_str}")
        return PushResult(
            container=container,
            image_tag=remote_name,
            success=True,
            command=push_str,
        )

    print(f"Tagging {image_tag} -> {remote_name} ...")
    try:
        tag_result = subprocess.run(
            tag_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        return PushResult(
            container=container,
            image_tag=remote_name,
            success=False,
            error=f"docker tag timed out: {exc}",
            command=tag_str,
        )
    if tag_result.returncode != 0:
        return PushResult(
            container=container,
            image_tag=remote_name,
            success=False,
            error=f"docker tag failed (exit {tag_result.returncode}): {tag_result.stderr.strip()}",
            command=tag_str,
        )

    print(f"Pushing {remote_name} ...")
    start = time.time()
    try:
        push_result = subprocess.run(
            push_cmd,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min max
        )
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return PushResult(
            container=container,
            image_tag=remote_name,
            success=False,
            duration_seconds=elapsed,
            error=f"docker push timed out after {elapsed:.0f}s",
            command=push_str,
        )
    elapsed = time.time() - start
    if push_result.returncode != 0:
        stderr_tail = push_result.stderr[-2000:] if push_result.stderr else ""
        return PushResult(
            container=container,
            image_tag=remote_name,
            success=False,
            duration_seconds=elapsed,
            error=f"docker push failed (exit {push_result.returncode}): {stderr_tail}",
            command=push_str,
        )

    print(f"  pushed in {elapsed:.1f}s")
    return PushResult(
        container=container,
        image_tag=remote_name,
        success=True,
        duration_seconds=elapsed,
        command=push_str,
    )


# ---------------------------------------------------------------------------
# RunPod endpoint refresh
# ---------------------------------------------------------------------------


def refresh_endpoint(
    endpoint_id: str,
    api_key: str,
    dry_run: bool = False,
) -> RefreshResult:
    """Trigger a RunPod endpoint refresh so workers pick up the new image.

    RunPod serverless endpoints automatically pull the latest image when a
    new worker is spun up. This call forces a redeploy by hitting the
    endpoint's health/status endpoint with the API key, which causes RunPod
    to recycle workers.

    Args:
        endpoint_id: the RunPod endpoint ID.
        api_key: the RunPod API key (Bearer token).
        dry_run: if True, print the would-be request but do not send it.

    Returns a RefreshResult.
    """
    url = f"{RUNPOD_API_BASE}/{endpoint_id}/health"
    if dry_run:
        print(f"[dry-run] would refresh endpoint: GET {url}")
        print("[dry-run]   Authorization: Bearer <redacted>")
        return RefreshResult(endpoint_id=endpoint_id, success=True)

    # Lazy import: httpx is only needed for the API call, not for builds.
    try:
        import httpx  # type: ignore[import-untyped]
    except ImportError:
        try:
            import requests as httpx  # type: ignore[import-untyped]
        except ImportError:
            return RefreshResult(
                endpoint_id=endpoint_id,
                success=False,
                error=(
                    "Neither httpx nor requests is installed. "
                    "Install one: 'pip install httpx' or 'pip install requests'."
                ),
            )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        if hasattr(httpx, "Client"):
            # httpx
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, headers=headers)
        else:
            # requests
            resp = httpx.get(url, headers=headers, timeout=30.0)
    except Exception as exc:
        return RefreshResult(
            endpoint_id=endpoint_id,
            success=False,
            error=f"Endpoint refresh request failed: {type(exc).__name__}: {exc}",
        )

    if resp.status_code != 200:
        return RefreshResult(
            endpoint_id=endpoint_id,
            success=False,
            status_code=resp.status_code,
            error=(
                f"Endpoint refresh failed: HTTP {resp.status_code}. "
                f"Response: {getattr(resp, 'text', '')[:500]}"
            ),
        )

    print(f"  endpoint {endpoint_id} refreshed (HTTP 200)")
    return RefreshResult(
        endpoint_id=endpoint_id,
        success=True,
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------


def print_summary(summary: Summary) -> None:
    """Print a human-readable summary of the rebuild run."""
    print("\n" + "=" * 70)
    print("RunPod Container Rebuild Summary")
    print("=" * 70)
    if summary.dry_run:
        print("  MODE: DRY RUN (no commands were executed)")
    print()

    print("Builds:")
    if not summary.builds:
        print("  (none)")
    for b in summary.builds:
        status = "OK" if b.success else "FAILED"
        print(f"  [{status}] {b.container}: {b.image_tag} ({b.duration_seconds:.1f}s)")
        if b.error:
            for line in b.error.splitlines()[:5]:
                print(f"         {line}")

    print("\nPushes:")
    if not summary.pushes:
        print("  (none)")
    for p in summary.pushes:
        status = "OK" if p.success else "FAILED"
        print(f"  [{status}] {p.container}: {p.image_tag} ({p.duration_seconds:.1f}s)")
        if p.error:
            for line in p.error.splitlines()[:5]:
                print(f"         {line}")

    print("\nEndpoint refreshes:")
    if not summary.refreshes:
        print("  (none)")
    for r in summary.refreshes:
        status = "OK" if r.success else "FAILED"
        print(f"  [{status}] endpoint {r.endpoint_id}")
        if r.error:
            for line in r.error.splitlines()[:3]:
                print(f"         {line}")

    print("=" * 70)
    if summary.all_success:
        print("Result: ALL SUCCESS")
    else:
        print("Result: FAILURES DETECTED (see above)")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild and push RunPod serverless containers for Quant Foundry. "
            "Updates the training and inference containers with new ML "
            "dependencies (lightgbm, pyarrow, onnxruntime, numpy)."
        ),
    )
    parser.add_argument(
        "--container",
        choices=["training", "inference", "both"],
        default="both",
        help="Which container to rebuild (default: both).",
    )
    parser.add_argument(
        "--tag",
        default="latest",
        help="Image tag (default: latest).",
    )
    parser.add_argument(
        "--registry",
        default=None,
        help=(
            "Container registry URL (e.g. ghcr.io/fincept). "
            "Defaults to the RUNPOD_REGISTRY env var. "
            "If empty/unset, images are built locally only (no push)."
        ),
    )
    parser.add_argument(
        "--push",
        action="store_true",
        default=False,
        help="Push images to the registry after building (default: False).",
    )
    parser.add_argument(
        "--refresh-endpoint",
        action="store_true",
        default=False,
        help=(
            "Trigger a RunPod endpoint refresh after pushing so workers "
            "pick up the new image (default: False). Requires RUNPOD_API_KEY."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print commands without executing them (default: False).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns an exit code."""
    args = parse_args(argv)
    summary = Summary(dry_run=args.dry_run)

    # Resolve which containers to build.
    if args.container == "both":
        targets = ["training", "inference"]
    else:
        targets = [args.container]

    # Resolve registry.
    registry = args.registry or os.environ.get("RUNPOD_REGISTRY", "")

    # --- Precondition checks ---
    # In dry-run mode we skip the Docker-installed/running checks so the
    # script can be used in CI/preview environments where Docker is not
    # available (e.g. just to preview the commands that would run).
    try:
        for target in targets:
            check_dockerfile_exists(target)
        if not args.dry_run:
            check_docker_installed()
            check_docker_running(dry_run=False)
    except PreconditionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Validate push preconditions.
    if args.push and not registry:
        print(
            "ERROR: --push requires --registry or the RUNPOD_REGISTRY env var.",
            file=sys.stderr,
        )
        return 1

    # Validate refresh preconditions.
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if args.refresh_endpoint and not api_key and not args.dry_run:
        print(
            "ERROR: --refresh-endpoint requires the RUNPOD_API_KEY env var.",
            file=sys.stderr,
        )
        return 1

    # --- Build ---
    build_exit_code = 0
    for target in targets:
        result = build_container(target, args.tag, dry_run=args.dry_run)
        summary.builds.append(result)
        if not result.success:
            build_exit_code = 2
            break

    if build_exit_code != 0:
        print_summary(summary)
        return build_exit_code

    # --- Push ---
    push_exit_code = 0
    if args.push:
        for target in targets:
            # Find the corresponding build result to get the local image tag.
            build_result = next(b for b in summary.builds if b.container == target)
            result = push_container(
                target,
                build_result.image_tag,
                registry,
                args.tag,
                dry_run=args.dry_run,
            )
            summary.pushes.append(result)
            if not result.success:
                push_exit_code = 3
                break

    if push_exit_code != 0:
        print_summary(summary)
        return push_exit_code

    # --- Refresh endpoints ---
    refresh_exit_code = 0
    if args.refresh_endpoint:
        endpoint_map = {
            "training": os.environ.get("RUNPOD_TRAINING_ENDPOINT_ID", DEFAULT_TRAINING_ENDPOINT_ID),
            "inference": os.environ.get(
                "RUNPOD_INFERENCE_ENDPOINT_ID", DEFAULT_INFERENCE_ENDPOINT_ID
            ),
        }
        for target in targets:
            endpoint_id = endpoint_map[target]
            result = refresh_endpoint(endpoint_id, api_key, dry_run=args.dry_run)
            summary.refreshes.append(result)
            if not result.success:
                refresh_exit_code = 4
                break

    print_summary(summary)
    return refresh_exit_code


if __name__ == "__main__":
    sys.exit(main())
