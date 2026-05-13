"""
scripts/openbb_live_proof.py - live OpenBB API integration proof.

Checks the Fincept API's OpenBB-facing routes against a running local stack and
writes a JSON receipt under ``reports/openbb-live/``.  This is intentionally a
manual/live smoke check: start Fincept API on port 8010 and the local OpenBB API
on 127.0.0.1:6900 before expecting all probes to pass.

Usage::

  uv run python scripts/openbb_live_proof.py --symbol NVDA
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = ROOT / "reports" / "openbb-live"


def mint_token() -> str:
    services_api_src = ROOT / "services" / "api" / "src"
    if str(services_api_src) not in sys.path:
        sys.path.insert(0, str(services_api_src))
    from api.auth import encode_token

    return encode_token({"sub": "openbb-live-proof"})


def summarize_json(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        out: dict[str, Any] = {"type": "object", "keys": sorted(body.keys())[:30]}
        for key in ("ok", "error", "error_type", "provider", "path", "url", "warning"):
            value = body.get(key)
            if isinstance(value, str | bool | int | float) or value is None:
                out[key] = value
        if isinstance(body.get("results"), list):
            out["results_length"] = len(body["results"])
            first = body["results"][0] if body["results"] else None
            if isinstance(first, dict):
                out["first_result_keys"] = sorted(first.keys())[:20]
        return out
    if isinstance(body, list):
        return {"type": "list", "length": len(body)}
    return {"type": type(body).__name__}


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await client.request(method, path, headers=headers, json=body)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        payload: Any
        if "application/json" in response.headers.get("content-type", ""):
            payload = response.json()
        else:
            payload = {"text": response.text[:500]}
        return {
            "method": method,
            "path": path,
            "status_code": response.status_code,
            "expected_statuses": list(expected),
            "passed": response.status_code in expected,
            "latency_ms": elapsed_ms,
            "body_summary": summarize_json(payload),
        }
    except httpx.HTTPError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        return {
            "method": method,
            "path": path,
            "status_code": None,
            "expected_statuses": list(expected),
            "passed": False,
            "latency_ms": elapsed_ms,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


async def prove(
    base_url: str,
    token: str,
    symbol: str,
    provider: str,
    request_timeout: float,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    started_unix = time.time()
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=request_timeout) as client:
        results = [
            await request_json(client, "GET", "/research/openbb/health", headers=headers, expected=(200,)),
            await request_json(
                client,
                "GET",
                f"/research/openbb/readiness?symbol={symbol}&provider={provider}",
                headers=headers,
                expected=(200,),
            ),
            await request_json(
                client,
                "POST",
                "/research/openbb/quote",
                headers=headers,
                body={"symbol": symbol, "provider": provider},
                expected=(200,),
            ),
            await request_json(
                client,
                "POST",
                "/research/openbb",
                headers=headers,
                body={
                    "path": "/api/v1/equity/fundamental/income",
                    "params": {"symbol": symbol, "provider": provider, "period": "annual", "limit": "2"},
                },
                expected=(200,),
            ),
        ]
    passed = sum(1 for result in results if result["passed"])
    return {
        "schema_version": 1,
        "generated_at_unix": started_unix,
        "base_url": base_url.rstrip("/"),
        "symbol": symbol,
        "provider": provider,
        "probe_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "all_passed": passed == len(results),
        "results": results,
    }


def write_receipt(receipt: dict[str, Any]) -> Path:
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(receipt["generated_at_unix"]))
    path = RECEIPT_DIR / f"openbb-live-{stamp}.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(prog="openbb_live_proof")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--symbol", default="NVDA")
    parser.add_argument("--provider", default="yfinance")
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    receipt = asyncio.run(
        prove(args.base_url, args.token or mint_token(), args.symbol, args.provider, args.timeout)
    )
    path = write_receipt(receipt)
    print(f"openbb live receipt: {path}")
    print(f"passed: {receipt['passed']}/{receipt['probe_count']}")
    for result in receipt["results"]:
        marker = "OK" if result["passed"] else "FAIL"
        print(f"  {marker:4} {result['method']} {result['path']} -> {result.get('status_code')} ({result['latency_ms']}ms)")
    return 0 if receipt["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
