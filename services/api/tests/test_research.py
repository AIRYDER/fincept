"""Tests for research API endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_exa_research_endpoint_returns_structured_brief(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_exa_research(**_kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "request_id": "req_123",
            "brief": {
                "headline": "NVDA supply constraints remain the key watch item",
                "summary": "Demand is strong, but supplier availability may cap near-term upside.",
                "bull_case": ["Hyperscaler capex remains resilient"],
                "bear_case": ["Export controls could pressure shipments"],
                "catalysts": ["Earnings call supplier commentary"],
                "risks": ["Crowded positioning"],
                "watch_items": ["Lead times", "Gross margin guide"],
            },
            "grounding": [
                {
                    "field": "summary",
                    "citations": [{"url": "https://example.com/nvda", "title": "NVDA note"}],
                    "confidence": "high",
                }
            ],
            "sources": [{"url": "https://example.com/nvda", "title": "NVDA note"}],
            "cost_dollars": 0.004,
        }

    captured_records: list[object] = []

    async def fake_write_provider_data(records: object) -> int:
        assert isinstance(records, list)
        captured_records.extend(records)
        return len(records)

    monkeypatch.setattr("api.routes.research.run_exa_research", fake_run_exa_research)
    monkeypatch.setattr("api.routes.research.write_provider_data", fake_write_provider_data)

    response = await client.post(
        "/research/exa",
        headers=auth_headers,
        json={
            "query": "NVDA Blackwell supply constraints",
            "symbol": "NVDA",
            "search_type": "deep",
            "max_age_hours": 24,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["request_id"] == "req_123"
    assert payload["brief"]["headline"] == "NVDA supply constraints remain the key watch item"
    assert payload["brief"]["bull_case"] == ["Hyperscaler capex remains resilient"]
    assert payload["grounding"][0]["confidence"] == "high"
    assert len(captured_records) == 1
    assert getattr(captured_records[0], "provider") == "exa"
    assert getattr(captured_records[0], "dataset") == "research_brief"


@pytest.mark.asyncio
async def test_exa_research_endpoint_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/research/exa",
        json={"query": "AAPL earnings risk"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_openbb_quote_endpoint_returns_market_rows(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_records: list[object] = []

    async def fake_write_provider_data(records: object) -> int:
        assert isinstance(records, list)
        captured_records.extend(records)
        return len(records)

    async def fake_run_openbb_quote(**kwargs: object) -> dict[str, object]:
        assert kwargs == {"symbol": "NVDA", "provider": "yfinance"}
        return {
            "ok": True,
            "provider": "yfinance",
            "results": [
                {
                    "symbol": "NVDA",
                    "last_price": 900.12,
                    "bid": 900.0,
                    "ask": 900.5,
                    "volume": 123456,
                }
            ],
        }

    monkeypatch.setattr("api.routes.research.run_openbb_quote", fake_run_openbb_quote)
    monkeypatch.setattr("api.routes.research.write_provider_data", fake_write_provider_data)

    response = await client.post(
        "/research/openbb/quote",
        headers=auth_headers,
        json={"symbol": "nvda"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "yfinance"
    assert payload["results"][0]["symbol"] == "NVDA"
    assert len(captured_records) == 1
    assert getattr(captured_records[0], "provider") == "openbb"
    assert getattr(captured_records[0], "dataset") == "equity.price.quote"


@pytest.mark.asyncio
async def test_openbb_quote_endpoint_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/research/openbb/quote",
        json={"symbol": "AAPL"},
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Generic dispatcher: POST /research/openbb                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openbb_call_endpoint_dispatches_to_tool(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route should forward path + params verbatim to the tool runner."""

    captured: dict[str, object] = {}
    captured_records: list[object] = []

    async def fake_write_provider_data(records: object) -> int:
        assert isinstance(records, list)
        captured_records.extend(records)
        return len(records)

    async def fake_run_openbb_call(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "ok": True,
            "path": kwargs["path"],
            "provider": "yfinance",
            "results": [{"symbol": "NVDA", "revenue": 60_900_000_000}],
        }

    monkeypatch.setattr("api.routes.research.run_openbb_call", fake_run_openbb_call)
    monkeypatch.setattr("api.routes.research.write_provider_data", fake_write_provider_data)

    response = await client.post(
        "/research/openbb",
        headers=auth_headers,
        json={
            "path": "/api/v1/equity/fundamental/income",
            "params": {"symbol": "NVDA", "period": "annual", "limit": "2"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["path"] == "/api/v1/equity/fundamental/income"
    assert body["results"][0]["revenue"] == 60_900_000_000
    assert captured["path"] == "/api/v1/equity/fundamental/income"
    assert captured["params"] == {"symbol": "NVDA", "period": "annual", "limit": "2"}
    assert len(captured_records) == 1
    assert getattr(captured_records[0], "provider") == "openbb"
    assert getattr(captured_records[0], "dataset") == "equity.fundamental.income"
    # Rate-limit diagnostics should be advertised on successful dispatches
    # so the UI can show "n requests left this window" without a probe.
    assert response.headers["X-RateLimit-Limit"] == "60"
    assert int(response.headers["X-RateLimit-Remaining"]) == 59


# --------------------------------------------------------------------------- #
# Allowlist + rate limit + uniform error mapping                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openbb_call_endpoint_rejects_path_outside_allowlist(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paths matching the schema but outside the allowlist must 403.

    Guards against OpenBB adding a new ``/api/v1/admin/...`` namespace
    that would otherwise pass the dispatcher's regex.
    """

    async def unreachable(**_kwargs: object) -> dict[str, object]:  # pragma: no cover
        raise AssertionError("tool must not be called when path is blocked")

    async def capture_unreachable(records: object) -> int:  # pragma: no cover
        raise AssertionError("provider capture must not run when path is blocked")

    monkeypatch.setattr("api.routes.research.run_openbb_call", unreachable)
    monkeypatch.setattr("api.routes.research.write_provider_data", capture_unreachable)

    response = await client.post(
        "/research/openbb",
        headers=auth_headers,
        json={"path": "/api/v1/admin/users", "params": {}},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["ok"] is False
    assert body["error_type"] == "PathNotAllowed"
    assert "allowlist" in body["error"].lower()


@pytest.mark.asyncio
async def test_openbb_call_endpoint_rate_limit_enforced(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the per-user window fills, further calls return 429."""

    monkeypatch.setattr("api.routes.research.OPENBB_DISPATCH_RATE_LIMIT", 2)
    monkeypatch.setattr("api.routes.research.OPENBB_DISPATCH_RATE_WINDOW_SEC", 60)

    async def fake_run_openbb_call(**kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "path": kwargs["path"],
            "provider": "yfinance",
            "results": [],
        }

    monkeypatch.setattr("api.routes.research.run_openbb_call", fake_run_openbb_call)

    payload = {"path": "/api/v1/equity/price/quote", "params": {"symbol": "NVDA"}}

    first = await client.post("/research/openbb", headers=auth_headers, json=payload)
    second = await client.post("/research/openbb", headers=auth_headers, json=payload)
    third = await client.post("/research/openbb", headers=auth_headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429

    body = third.json()
    assert body["ok"] is False
    assert body["error_type"] == "RateLimited"
    assert body["limit"] == 2
    assert body["window_sec"] == 60
    assert body["retry_after"] >= 1
    assert third.headers["Retry-After"] == str(body["retry_after"])


@pytest.mark.asyncio
async def test_openbb_call_maps_unavailable_to_503(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool-layer OpenBBUnavailable results must surface as HTTP 503.

    Dashboard branches on status, not string-parsing, so we assert both
    the status code and that the body preserves the ``{ok, error_type,
    error}`` contract.
    """

    async def fake_run_openbb_call(**_kwargs: object) -> dict[str, object]:
        return {
            "ok": False,
            "error_type": "OpenBBUnavailable",
            "error": "OpenBB API is not reachable.",
            "path": "",
            "provider": None,
            "results": [],
        }

    monkeypatch.setattr("api.routes.research.run_openbb_call", fake_run_openbb_call)

    response = await client.post(
        "/research/openbb",
        headers=auth_headers,
        json={"path": "/api/v1/equity/price/quote", "params": {"symbol": "NVDA"}},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["error_type"] == "OpenBBUnavailable"


@pytest.mark.asyncio
async def test_openbb_call_maps_backend_error_to_502(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_openbb_call(**_kwargs: object) -> dict[str, object]:
        return {
            "ok": False,
            "error_type": "ToolBackendError",
            "error": "OpenBB returned non-JSON body",
            "path": "",
            "provider": None,
            "results": [],
        }

    monkeypatch.setattr("api.routes.research.run_openbb_call", fake_run_openbb_call)

    response = await client.post(
        "/research/openbb",
        headers=auth_headers,
        json={"path": "/api/v1/equity/price/quote", "params": {"symbol": "NVDA"}},
    )
    assert response.status_code == 502
    body = response.json()
    assert body["error_type"] == "ToolBackendError"


@pytest.mark.asyncio
async def test_openbb_quote_maps_unavailable_to_503(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_openbb_quote(**_kwargs: object) -> dict[str, object]:
        return {
            "ok": False,
            "error_type": "OpenBBUnavailable",
            "error": "OpenBB API is not reachable.",
            "provider": "yfinance",
            "results": [],
        }

    monkeypatch.setattr("api.routes.research.run_openbb_quote", fake_run_openbb_quote)

    response = await client.post(
        "/research/openbb/quote",
        headers=auth_headers,
        json={"symbol": "NVDA"},
    )
    assert response.status_code == 503
    assert response.json()["error_type"] == "OpenBBUnavailable"


@pytest.mark.asyncio
async def test_openbb_call_endpoint_requires_auth(client: AsyncClient) -> None:
    response = await client.post(
        "/research/openbb",
        json={"path": "/api/v1/equity/price/quote", "params": {"symbol": "AAPL"}},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_openbb_call_endpoint_rejects_path_outside_api_v1(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The route must reject malformed paths before the tool runs.

    Pydantic returns 422 on the regex/validator failure; that's what
    the dashboard relies on to highlight the wrong field instead of
    bubbling a 500 from the tool layer.
    """
    response = await client.post(
        "/research/openbb",
        headers=auth_headers,
        json={"path": "/admin/users", "params": {}},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Health probe: GET /research/openbb/health                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openbb_health_endpoint_returns_ok(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_health() -> dict[str, object]:
        return {"ok": True, "url": "http://127.0.0.1:6900", "latency_ms": 12}

    monkeypatch.setattr("api.routes.research.run_openbb_health", fake_health)

    response = await client.get("/research/openbb/health", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload == {"ok": True, "url": "http://127.0.0.1:6900", "latency_ms": 12}


@pytest.mark.asyncio
async def test_openbb_health_endpoint_returns_unavailable(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_health() -> dict[str, object]:
        return {
            "ok": False,
            "url": "http://127.0.0.1:6900",
            "error_type": "OpenBBUnavailable",
            "error": "OpenBB API is not reachable.",
        }

    monkeypatch.setattr("api.routes.research.run_openbb_health", fake_health)

    response = await client.get("/research/openbb/health", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error_type"] == "OpenBBUnavailable"


@pytest.mark.asyncio
async def test_openbb_health_endpoint_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/research/openbb/health")
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Health history persistence                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openbb_health_endpoint_persists_history(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful probes must land in the Redis history stream.

    Guards the ``/history`` endpoint: if persistence regresses silently
    the dashboard uptime chart would show a flat zero without any
    explicit error.
    """

    results = [
        {"ok": True, "url": "http://127.0.0.1:6900", "latency_ms": 12},
        {"ok": True, "url": "http://127.0.0.1:6900", "latency_ms": 18},
        {
            "ok": False,
            "url": "http://127.0.0.1:6900",
            "error_type": "OpenBBUnavailable",
            "error": "down",
        },
    ]

    async def fake_health() -> dict[str, object]:
        return dict(results[fake_health.calls])  # type: ignore[attr-defined]

    fake_health.calls = 0  # type: ignore[attr-defined]

    async def wrapper() -> dict[str, object]:
        result = await fake_health()
        fake_health.calls += 1  # type: ignore[attr-defined]
        return result

    monkeypatch.setattr("api.routes.research.run_openbb_health", wrapper)

    for _ in range(3):
        response = await client.get("/research/openbb/health", headers=auth_headers)
        assert response.status_code == 200

    history = await client.get(
        "/research/openbb/health/history",
        headers=auth_headers,
    )
    assert history.status_code == 200
    body = history.json()
    assert len(body["entries"]) == 3
    # Oldest → newest ordering so the dashboard can feed a chart directly.
    assert body["entries"][0]["latency_ms"] == 12
    assert body["entries"][1]["latency_ms"] == 18
    assert body["entries"][2]["ok"] is False
    assert body["entries"][2]["error_type"] == "OpenBBUnavailable"

    summary = body["summary"]
    assert summary["samples"] == 3
    # 2 / 3 ok => 66.67 %, rounded.
    assert summary["uptime_pct"] == pytest.approx(66.67, rel=0.01)
    assert summary["last_error_type"] == "OpenBBUnavailable"
    assert summary["latency_p50_ms"] in (12, 18)


@pytest.mark.asyncio
async def test_openbb_health_history_empty_summary(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """With no probes yet, the summary fields must be explicitly null."""
    response = await client.get(
        "/research/openbb/health/history",
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["entries"] == []
    assert body["summary"] == {
        "samples": 0,
        "uptime_pct": None,
        "latency_p50_ms": None,
        "latency_p95_ms": None,
        "last_error_type": None,
    }


@pytest.mark.asyncio
async def test_openbb_health_history_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/research/openbb/health/history")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_openbb_readiness_endpoint_returns_diagnostics(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_readiness(symbol: str, provider: str) -> dict[str, object]:
        return {
            "ok": True,
            "url": "http://127.0.0.1:6900",
            "symbol": symbol,
            "provider": provider,
            "api_reachable": True,
            "provider_ready": False,
            "checks": [
                {"name": "openapi", "ok": True, "required": True},
                {"name": "quote", "ok": True, "required": True},
                {
                    "name": "fundamentals_income",
                    "ok": False,
                    "required": False,
                    "error_type": "ToolBackendError",
                },
            ],
        }

    monkeypatch.setattr("api.routes.research.run_openbb_readiness", fake_readiness)

    response = await client.get(
        "/research/openbb/readiness?symbol=nvda&provider=yfinance",
        headers=auth_headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["symbol"] == "NVDA"
    assert payload["api_reachable"] is True
    assert payload["provider_ready"] is False
    assert payload["checks"][2]["error_type"] == "ToolBackendError"


@pytest.mark.asyncio
async def test_openbb_readiness_endpoint_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/research/openbb/readiness")
    assert response.status_code == 401
