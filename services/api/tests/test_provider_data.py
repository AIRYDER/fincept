"""Tests for provider data normalization helpers."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from fincept_db.provider_data import (
    build_exa_record,
    build_openbb_call_record,
    build_openbb_quote_record,
)


def test_build_exa_record_normalizes_research_brief() -> None:
    record = build_exa_record(
        request={
            "query": "NVDA supply chain",
            "symbol": "nvda",
            "search_type": "deep",
            "num_results": 5,
            "max_age_hours": 24,
        },
        response={
            "ok": True,
            "request_id": "req_1",
            "brief": {"headline": "NVDA supply is improving", "summary": "Lead times eased."},
            "sources": [{"url": "https://example.com", "title": "Example"}],
            "grounding": [{"field": "summary", "confidence": "high"}],
            "cost_dollars": 0.003,
        },
        ts_event=1_000,
    )

    assert record.provider == "exa"
    assert record.dataset == "research_brief"
    assert record.symbol == "NVDA"
    assert record.row_count == 1
    assert record.normalized["headline"] == "NVDA supply is improving"
    assert record.normalized["source_count"] == 1
    assert record.request_hash


def test_build_openbb_quote_record_normalizes_market_rows() -> None:
    record = build_openbb_quote_record(
        request={"symbol": "nvda", "provider": "yfinance"},
        response={
            "ok": True,
            "provider": "yfinance",
            "results": [{"symbol": "NVDA", "last_price": 900.12, "volume": 123}],
        },
        ts_event=1_000,
    )

    assert record.provider == "openbb"
    assert record.source == "research.openbb_quote"
    assert record.dataset == "equity.price.quote"
    assert record.symbol == "NVDA"
    assert record.row_count == 1
    assert record.normalized["upstream_provider"] == "yfinance"
    assert record.normalized["fields"] == ["last_price", "symbol", "volume"]


def test_build_openbb_call_record_classifies_dispatch_path() -> None:
    record = build_openbb_call_record(
        request={
            "path": "/api/v1/equity/fundamental/income",
            "params": {"symbol": "nvda", "provider": "fmp", "limit": "2"},
        },
        response={
            "ok": True,
            "provider": "fmp",
            "results": [{"symbol": "NVDA", "revenue": 60_900_000_000}],
        },
        ts_event=1_000,
    )

    assert record.provider == "openbb"
    assert record.dataset == "equity.fundamental.income"
    assert record.endpoint == "/api/v1/equity/fundamental/income"
    assert record.symbol == "NVDA"
    assert record.normalized["path"] == "/api/v1/equity/fundamental/income"
    assert record.normalized["rows"][0]["revenue"] == 60_900_000_000


@pytest.mark.asyncio
async def test_provider_data_endpoint_returns_capture_summary(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = build_openbb_quote_record(
        request={"symbol": "NVDA", "provider": "yfinance"},
        response={
            "ok": True,
            "provider": "yfinance",
            "results": [{"symbol": "NVDA", "last_price": 900.12}],
        },
        ts_event=1_000,
    )
    captured: dict[str, object] = {}

    async def fake_read_provider_data(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return [record]

    monkeypatch.setattr("api.routes.research.read_provider_data", fake_read_provider_data)

    response = await client.get(
        "/research/provider-data?provider=openbb&dataset=equity.price.quote&symbol=nvda&limit=5",
        headers=auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["capture_enabled"] is True
    assert payload["summary"]["total_records"] == 1
    assert payload["summary"]["providers"] == {"openbb": 1}
    assert payload["records"][0]["symbol"] == "NVDA"
    assert payload["records"][0]["normalized"]["dataset"] == "equity.price.quote"
    assert captured == {
        "provider": "openbb",
        "dataset": "equity.price.quote",
        "symbol": "NVDA",
        "limit": 5,
    }


@pytest.mark.asyncio
async def test_provider_data_endpoint_reports_disabled_capture(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read_provider_data(**_kwargs: object) -> list[object]:
        raise RuntimeError("FINCEPT_DB_URL is empty; set FINCEPT_DB_URL")

    monkeypatch.setattr("api.routes.research.read_provider_data", fake_read_provider_data)

    response = await client.get("/research/provider-data", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["capture_enabled"] is False
    assert payload["error_type"] == "ProviderDataDisabled"
    assert payload["summary"]["total_records"] == 0
    assert payload["records"] == []
