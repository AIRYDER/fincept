"""Tests for Exa-backed research tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fincept_tools  # noqa: F401 - side-effect: registers built-in tools
from fincept_tools.registry import REGISTRY


@pytest.mark.asyncio
async def test_exa_market_research_posts_structured_deep_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fincept_tools.research.exa import ExaMarketResearchInput, ExaMarketResearchTool

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr("fincept_tools.research.exa._read_exa_api_key_from_dotenv", lambda: None)
    calls: list[dict[str, object]] = []

    async def fake_post_json(
        url: str, headers: dict[str, str], body: dict[str, object]
    ) -> dict[str, object]:
        calls.append({"url": url, "headers": headers, "body": body})
        return {
            "requestId": "req_123",
            "costDollars": {"total": 0.004},
            "output": {
                "content": {
                    "headline": "NVDA supply constraints remain the key watch item",
                    "summary": "Demand is strong, but supplier availability may cap near-term upside.",
                    "bullCase": ["Hyperscaler capex remains resilient"],
                    "bearCase": ["Export controls could pressure shipments"],
                    "catalysts": ["Earnings call supplier commentary"],
                    "risks": ["Crowded positioning"],
                    "watchItems": ["Lead times", "Gross margin guide"],
                },
                "grounding": [
                    {
                        "field": "summary",
                        "citations": [{"url": "https://example.com/nvda", "title": "NVDA note"}],
                        "confidence": "high",
                    }
                ],
            },
            "results": [{"title": "NVDA note", "url": "https://example.com/nvda"}],
        }

    tool = ExaMarketResearchTool(post_json=fake_post_json)
    result = await tool(
        ExaMarketResearchInput(
            query="NVDA Blackwell supply constraints",
            symbol="NVDA",
            search_type="deep",
            max_age_hours=24,
        )
    )

    assert result.ok is True
    assert result.request_id == "req_123"
    assert result.brief.headline == "NVDA supply constraints remain the key watch item"
    assert result.brief.bull_case == ["Hyperscaler capex remains resilient"]
    assert result.grounding[0].field == "summary"
    assert result.sources[0].url == "https://example.com/nvda"
    assert result.cost_dollars == 0.004

    assert calls
    request = calls[0]
    assert request["url"] == "https://api.exa.ai/search"
    assert request["headers"] == {
        "Content-Type": "application/json",
        "User-Agent": "FinceptTerminal/0.1",
        "x-api-key": "test-key",
    }
    body = request["body"]
    assert isinstance(body, dict)
    assert body["query"] == "NVDA Blackwell supply constraints"
    assert body["type"] == "deep"
    assert body["numResults"] == 10
    assert body["contents"] == {"highlights": {"maxCharacters": 4000}, "maxAgeHours": 24}
    assert "outputSchema" in body
    json.dumps(body["outputSchema"])


@pytest.mark.asyncio
async def test_exa_market_research_prefers_dotenv_over_stale_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fincept_tools.research.exa import ExaMarketResearchInput, ExaMarketResearchTool

    monkeypatch.setenv("EXA_API_KEY", "stale-env-key")
    monkeypatch.setattr(
        "fincept_tools.research.exa._read_exa_api_key_from_dotenv", lambda: "fresh-dotenv-key"
    )
    seen_headers: dict[str, str] = {}

    async def fake_post_json(
        _url: str, headers: dict[str, str], _body: dict[str, object]
    ) -> dict[str, object]:
        seen_headers.update(headers)
        return {
            "output": {
                "content": {
                    "headline": "AAPL earnings risk",
                    "summary": "Earnings risk brief.",
                    "bullCase": [],
                    "bearCase": [],
                    "catalysts": [],
                    "risks": [],
                    "watchItems": [],
                },
                "grounding": [],
            },
            "results": [],
        }

    tool = ExaMarketResearchTool(post_json=fake_post_json)
    result = await tool(ExaMarketResearchInput(query="AAPL earnings risk"))

    assert result.ok is True
    assert seen_headers["x-api-key"] == "fresh-dotenv-key"


@pytest.mark.asyncio
async def test_exa_market_research_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from fincept_tools.research.exa import ExaMarketResearchInput, ExaMarketResearchTool

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr("fincept_tools.research.exa._read_exa_api_key_from_dotenv", lambda: None)

    tool = ExaMarketResearchTool(
        post_json=lambda *_args, **_kwargs: pytest.fail("should not call Exa")
    )
    result = await tool(ExaMarketResearchInput(query="AAPL earnings risk"))

    assert result.ok is False
    assert result.error_type == "MissingExaApiKey"


@pytest.mark.asyncio
async def test_exa_market_research_uses_dotenv_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from fincept_tools.research.exa import ExaMarketResearchInput, ExaMarketResearchTool

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(
        "fincept_tools.research.exa._read_exa_api_key_from_dotenv", lambda: "dotenv-key"
    )
    seen_headers: dict[str, str] = {}

    async def fake_post_json(
        _url: str, headers: dict[str, str], _body: dict[str, object]
    ) -> dict[str, object]:
        seen_headers.update(headers)
        return {
            "output": {
                "content": {
                    "headline": "AAPL earnings risk",
                    "summary": "Earnings risk brief.",
                    "bullCase": [],
                    "bearCase": [],
                    "catalysts": [],
                    "risks": [],
                    "watchItems": [],
                },
                "grounding": [],
            },
            "results": [],
        }

    tool = ExaMarketResearchTool(post_json=fake_post_json)
    result = await tool(ExaMarketResearchInput(query="AAPL earnings risk"))

    assert result.ok is True
    assert seen_headers["x-api-key"] == "dotenv-key"


def test_exa_market_research_is_registered() -> None:
    assert "research.exa_market" in {spec["function"]["name"] for spec in REGISTRY.list()}


def test_read_exa_api_key_from_current_working_directory_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fincept_tools.research.exa import _read_exa_api_key_from_dotenv

    (tmp_path / ".env").write_text("EXA_API_KEY=cwd-key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert _read_exa_api_key_from_dotenv() == "cwd-key"
