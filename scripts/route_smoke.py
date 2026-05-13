"""
scripts/route_smoke.py - local API route smoke receipt generator.

Probes the operator-facing API routes that commonly drift during dashboard,
research, and trading-control work.  The script writes a JSON receipt under
``reports/route-smoke/`` with status codes, latency, pass/fail, and small
shape summaries.  It never records bearer tokens or full response payloads.

Usage::

  uv run python scripts/route_smoke.py --base-url http://127.0.0.1:8010

If ``--token`` is omitted, the script mints a local JWT using the current
``FINCEPT_JWT_SECRET`` via ``api.auth.encode_token``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = ROOT / "reports" / "route-smoke"


@dataclass(frozen=True)
class Probe:
    name: str
    method: str
    path: str
    expected_statuses: tuple[int, ...]
    params: dict[str, str] | None = None
    json_body: dict[str, Any] | None = None


PROBES: tuple[Probe, ...] = (
    Probe("health", "GET", "/health", (200,)),
    Probe("data_sources", "GET", "/data/sources", (200,)),
    Probe(
        "data_coverage",
        "GET",
        "/data/coverage",
        (200, 503),
        params={"freq": "1m", "lookback_ns": "3600000000000", "stale_after_ns": "3600000000000"},
    ),
    Probe("symbol_search", "GET", "/data/symbols/search", (200, 503), params={"q": "BTC", "limit": "5"}),
    Probe("openbb_health", "GET", "/research/openbb/health", (200, 503)),
    Probe("news_impact_status", "GET", "/news-impact/status", (200, 503)),
    Probe("strategy_configs", "GET", "/strategies/configs", (200, 503)),
    Probe("orders", "GET", "/orders", (200, 503), params={"limit": "5"}),
    Probe("services", "GET", "/services", (200, 503)),
)


def mint_token() -> str:
    services_api_src = ROOT / "services" / "api" / "src"
    if str(services_api_src) not in sys.path:
        sys.path.insert(0, str(services_api_src))
    from api.auth import encode_token

    return encode_token({"sub": "route-smoke"})


def summarize_body(body: Any) -> dict[str, Any]:
    if isinstance(body, list):
        return {
            "type": "list",
            "length": len(body),
            "first_keys": sorted(body[0].keys())[:20] if body and isinstance(body[0], dict) else [],
        }
    if isinstance(body, dict):
        summary: dict[str, Any] = {
            "type": "object",
            "keys": sorted(body.keys())[:30],
        }
        for key in ("ok", "error", "error_type", "detail"):
            value = body.get(key)
            if isinstance(value, str | bool | int | float) or value is None:
                summary[key] = value
        for key in ("sources", "rows", "configs", "orders", "services", "alert", "impact", "universe"):
            value = body.get(key)
            if isinstance(value, list):
                summary[f"{key}_length"] = len(value)
        if isinstance(body.get("summary"), dict):
            summary["summary_keys"] = sorted(body["summary"].keys())[:20]
        return summary
    if isinstance(body, str):
        return {"type": "string", "sha256": hashlib.sha256(body.encode()).hexdigest(), "length": len(body)}
    return {"type": type(body).__name__}


async def run_probe(client: httpx.AsyncClient, probe: Probe, headers: dict[str, str]) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await client.request(
            probe.method,
            probe.path,
            params=probe.params,
            json=probe.json_body,
            headers=headers if probe.path != "/health" else {},
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            body: Any = response.json()
        else:
            body = response.text
        return {
            "name": probe.name,
            "method": probe.method,
            "path": probe.path,
            "status_code": response.status_code,
            "expected_statuses": list(probe.expected_statuses),
            "passed": response.status_code in probe.expected_statuses,
            "latency_ms": elapsed_ms,
            "body_summary": summarize_body(body),
        }
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "name": probe.name,
            "method": probe.method,
            "path": probe.path,
            "status_code": None,
            "expected_statuses": list(probe.expected_statuses),
            "passed": False,
            "latency_ms": elapsed_ms,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


async def smoke(base_url: str, token: str, request_timeout: float) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    started_unix = time.time()
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=request_timeout) as client:
        results = [await run_probe(client, probe, headers) for probe in PROBES]
    passed = sum(1 for result in results if result["passed"])
    return {
        "schema_version": 1,
        "generated_at_unix": started_unix,
        "base_url": base_url.rstrip("/"),
        "probe_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "all_passed": passed == len(results),
        "results": results,
    }


def write_receipt(receipt: dict[str, Any]) -> Path:
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(receipt["generated_at_unix"]))
    path = RECEIPT_DIR / f"route-smoke-{stamp}.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(prog="route_smoke")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--token", default=None, help="Optional bearer token. Omit to mint a local dev JWT.")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    token = args.token or mint_token()
    receipt = asyncio.run(smoke(args.base_url, token, args.timeout))
    path = write_receipt(receipt)

    print(f"route smoke receipt: {path}")
    print(f"passed: {receipt['passed']}/{receipt['probe_count']}")
    for result in receipt["results"]:
        marker = "OK" if result["passed"] else "FAIL"
        status = result.get("status_code")
        print(f"  {marker:4} {result['method']} {result['path']} -> {status} ({result['latency_ms']}ms)")
    return 0 if receipt["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
