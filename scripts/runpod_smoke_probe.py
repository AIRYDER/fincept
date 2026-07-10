from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlparse

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}


class RunPodProbeError(RuntimeError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _emit(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": _now(), "event": event, **fields}, sort_keys=True))
    sys.stdout.flush()


def _request_json(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"https", "http"}:
        raise RunPodProbeError(f"Unsupported URL scheme: {parsed_url.scheme}")

    connection_class = (
        http.client.HTTPSConnection if parsed_url.scheme == "https" else http.client.HTTPConnection
    )
    request_target = parsed_url.path or "/"
    if parsed_url.query:
        request_target = f"{request_target}?{parsed_url.query}"

    connection = connection_class(parsed_url.netloc, timeout=30)
    try:
        connection.request(method, request_target, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RunPodProbeError(f"{method} {url} failed: {exc}") from exc
    finally:
        connection.close()

    if response.status >= 400:
        raise RunPodProbeError(f"{method} {url} returned HTTP {response.status}: {raw}")

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunPodProbeError(f"{method} {url} returned non-JSON: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise RunPodProbeError(f"{method} {url} returned JSON {type(parsed).__name__}")
    return parsed


def _extract_job_id(run_response: dict[str, Any]) -> str:
    candidates = (
        run_response.get("id"),
        run_response.get("jobId"),
        run_response.get("job_id"),
    )
    data = run_response.get("data")
    if isinstance(data, dict):
        candidates += (
            data.get("id"),
            data.get("jobId"),
            data.get("job_id"),
        )

    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    raise RunPodProbeError(f"Could not find job id in /run response: {run_response}")


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_json:
        parsed = json.loads(args.payload_json)
    elif args.payload_file:
        with open(args.payload_file, encoding="utf-8") as handle:
            parsed = json.load(handle)
    else:
        parsed = {
            "input": {
                "task": "smoke",
                "job_id": f"smoke-{uuid.uuid4()}",
                "nonce": str(uuid.uuid4()),
                "expected_image_tag": args.image_tag,
            }
        }

    if not isinstance(parsed, dict):
        raise RunPodProbeError("Payload must be a JSON object")
    if "input" not in parsed:
        parsed = {"input": parsed}
    return parsed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch and poll a RunPod serverless smoke job.")
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--api-key", default=None)
    parser.add_argument(
        "--api-base",
        default="https://api.runpod.ai/v2",
        help="RunPod serverless API base URL",
    )
    parser.add_argument("--image-tag", required=True)
    parser.add_argument("--payload-json", default=None)
    parser.add_argument("--payload-file", default=None)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    api_key = args.api_key
    if not api_key:
        import os

        api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        raise RunPodProbeError("Set RUNPOD_API_KEY or pass --api-key")

    endpoint_base = f"{args.api_base.rstrip('/')}/{args.endpoint_id}"
    payload = _load_payload(args)

    _emit(
        "probe_start",
        endpoint_id=args.endpoint_id,
        image_tag=args.image_tag,
        timeout_seconds=args.timeout,
    )

    health_before = _request_json("GET", f"{endpoint_base}/health", api_key)
    _emit("health_before", body=health_before)

    run_response = _request_json("POST", f"{endpoint_base}/run", api_key, payload)
    job_id = _extract_job_id(run_response)
    _emit("run_response", job_id=job_id, body=run_response)

    deadline = time.monotonic() + args.timeout
    last_status = "UNKNOWN"
    while time.monotonic() < deadline:
        status_body = _request_json("GET", f"{endpoint_base}/status/{job_id}", api_key)
        status = str(status_body.get("status", "UNKNOWN"))
        last_status = status
        _emit("status", job_id=job_id, status=status, body=status_body)

        health_body = _request_json("GET", f"{endpoint_base}/health", api_key)
        _emit("health", job_id=job_id, body=health_body)

        if status in TERMINAL_STATUSES:
            _emit("probe_end", job_id=job_id, final_status=status)
            return 0 if status == "COMPLETED" else 2
        time.sleep(args.interval)

    _emit("probe_timeout", job_id=job_id, last_status=last_status)
    return 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunPodProbeError as exc:
        _emit("probe_error", error=str(exc))
        raise SystemExit(1) from exc
