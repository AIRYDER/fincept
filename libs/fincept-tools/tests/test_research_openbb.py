"""Tests for OpenBB-backed research tools."""

from __future__ import annotations

import pytest

import fincept_tools.research  # noqa: F401 - registers research tools as a side-effect
from fincept_tools.registry import REGISTRY


@pytest.mark.asyncio
async def test_openbb_quote_returns_normalized_rows() -> None:
    from fincept_tools.research.openbb import OpenBBQuoteInput, OpenBBQuoteTool

    async def fake_quote(symbol: str, provider: str) -> list[dict[str, object]]:
        assert symbol == "NVDA"
        assert provider == "yfinance"
        return [
            {
                "symbol": "NVDA",
                "last_price": 900.12,
                "bid": 900.0,
                "ask": 900.5,
                "volume": 123456,
            }
        ]

    tool = OpenBBQuoteTool(quote_loader=fake_quote)
    result = await tool(OpenBBQuoteInput(symbol="nvda"))

    assert result.ok is True
    assert result.provider == "yfinance"
    assert result.results == [
        {
            "symbol": "NVDA",
            "last_price": 900.12,
            "bid": 900.0,
            "ask": 900.5,
            "volume": 123456,
        }
    ]


@pytest.mark.asyncio
async def test_openbb_quote_reports_missing_package() -> None:
    from fincept_tools.errors import OpenBBUnavailable
    from fincept_tools.research.openbb import OpenBBQuoteInput, OpenBBQuoteTool

    async def missing_quote(_symbol: str, _provider: str) -> list[dict[str, object]]:
        raise OpenBBUnavailable("Install the openbb package before using OpenBB tools.")

    tool = OpenBBQuoteTool(quote_loader=missing_quote)
    result = await tool(OpenBBQuoteInput(symbol="AAPL"))

    assert result.ok is False
    assert result.error_type == "OpenBBUnavailable"


def test_openbb_quote_is_registered() -> None:
    assert "research.openbb_quote" in {spec["function"]["name"] for spec in REGISTRY.list()}


@pytest.mark.asyncio
async def test_openbb_quote_prefers_local_api(monkeypatch: pytest.MonkeyPatch) -> None:
    from fincept_tools.research.openbb import OpenBBQuoteInput, OpenBBQuoteTool

    calls: list[tuple[str, dict[str, str]]] = []

    async def fake_get_json(url: str, params: dict[str, str]) -> dict[str, object]:
        calls.append((url, params))
        return {
            "results": [
                {
                    "symbol": "NVDA",
                    "last_price": 900.12,
                }
            ],
            "provider": "yfinance",
        }

    monkeypatch.setenv("OPENBB_API_URL", "http://127.0.0.1:6900")

    tool = OpenBBQuoteTool(get_json=fake_get_json)
    result = await tool(OpenBBQuoteInput(symbol="nvda"))

    assert result.ok is True
    assert result.results == [{"symbol": "NVDA", "last_price": 900.12}]
    assert calls == [
        (
            "http://127.0.0.1:6900/api/v1/equity/price/quote",
            {"symbol": "NVDA", "provider": "yfinance"},
        )
    ]


# --------------------------------------------------------------------------- #
# Generic dispatcher                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openbb_call_dispatches_arbitrary_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher should hit ``base_url + path`` with the given params and
    return the normalised result list — proving the dashboard can add
    new endpoints without backend touches."""
    from fincept_tools.research.openbb import OpenBBCallInput, OpenBBCallTool

    calls: list[tuple[str, dict[str, str]]] = []

    async def fake_get_json(url: str, params: dict[str, str]) -> dict[str, object]:
        calls.append((url, params))
        return {
            "provider": "yfinance",
            "results": [
                {"symbol": "NVDA", "revenue": 60_900_000_000},
                {"symbol": "NVDA", "revenue": 26_900_000_000},
            ],
        }

    monkeypatch.setenv("OPENBB_API_URL", "http://127.0.0.1:6900")

    tool = OpenBBCallTool(get_json=fake_get_json)
    result = await tool(
        OpenBBCallInput(
            path="/api/v1/equity/fundamental/income",
            params={"symbol": "NVDA", "period": "annual", "limit": "2"},
        )
    )

    assert result.ok is True
    assert result.path == "/api/v1/equity/fundamental/income"
    assert result.provider == "yfinance"
    assert len(result.results) == 2
    assert result.results[0]["revenue"] == 60_900_000_000
    assert calls == [
        (
            "http://127.0.0.1:6900/api/v1/equity/fundamental/income",
            {"symbol": "NVDA", "period": "annual", "limit": "2"},
        )
    ]


def test_openbb_call_rejects_paths_outside_api_v1() -> None:
    """The dispatcher's path regex blocks anything that isn't
    ``/api/v1/...``; that's the whole safety net for this tool."""
    from pydantic import ValidationError

    from fincept_tools.research.openbb import OpenBBCallInput

    bad_paths = [
        "api/v1/equity/price/quote",  # missing leading slash
        "/admin/users",  # not under /api/v1
        "/api/v2/equity/price/quote",  # wrong API version
        "/api/v1/equity/price/quote?evil=1",  # query string snuck in
        "/api/v1/../etc/passwd",  # traversal attempt
    ]
    for bad in bad_paths:
        with pytest.raises(ValidationError):
            OpenBBCallInput(path=bad)


def test_openbb_call_is_registered() -> None:
    names = {spec["function"]["name"] for spec in REGISTRY.list()}
    assert "research.openbb_call" in names


# --------------------------------------------------------------------------- #
# Health probe                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_check_openbb_health_returns_ok_when_api_responds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fincept_tools.research.openbb import check_openbb_health

    async def fake_get_json(
        url: str,
        params: dict[str, str],
        *,
        request_timeout: float = 15.0,
    ) -> dict[str, object]:
        # The probe targets the FastAPI openapi.json so make sure that's
        # what we were asked for; no upstream provider call.
        assert url == "http://127.0.0.1:6900/openapi.json"
        assert params == {}
        assert request_timeout <= 3.0
        return {"openapi": "3.1.0"}

    monkeypatch.setenv("OPENBB_API_URL", "http://127.0.0.1:6900")
    result = await check_openbb_health(get_json=fake_get_json)
    assert result["ok"] is True
    assert result["url"] == "http://127.0.0.1:6900"
    assert "latency_ms" in result


@pytest.mark.asyncio
async def test_check_openbb_health_returns_unavailable_when_api_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fincept_tools.errors import OpenBBUnavailable
    from fincept_tools.research.openbb import check_openbb_health

    async def failing_get_json(
        url: str,
        params: dict[str, str],
        *,
        request_timeout: float = 15.0,
    ) -> dict[str, object]:
        assert request_timeout <= 3.0
        raise OpenBBUnavailable("OpenBB API is not reachable.")

    monkeypatch.setenv("OPENBB_API_URL", "http://127.0.0.1:6900")
    result = await check_openbb_health(get_json=failing_get_json)
    assert result["ok"] is False
    assert result["error_type"] == "OpenBBUnavailable"
    assert result["url"] == "http://127.0.0.1:6900"
    assert "error" in result
