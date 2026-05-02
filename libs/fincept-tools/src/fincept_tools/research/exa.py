"""Exa-backed market research tool."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal
from urllib import error, request

from pydantic import BaseModel, Field

from fincept_tools.errors import MissingExaApiKey, ToolBackendError
from fincept_tools.protocol import BaseTool, ToolInput, ToolOutput
from fincept_tools.registry import register

EXA_SEARCH_URL = "https://api.exa.ai/search"

SearchType = Literal["auto", "fast", "instant", "deep-lite", "deep", "deep-reasoning"]


class ResearchBrief(BaseModel):
    headline: str
    summary: str
    bull_case: list[str] = Field(default_factory=list, alias="bullCase")
    bear_case: list[str] = Field(default_factory=list, alias="bearCase")
    catalysts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    watch_items: list[str] = Field(default_factory=list, alias="watchItems")


class ResearchCitation(BaseModel):
    url: str
    title: str | None = None


class ResearchGrounding(BaseModel):
    field: str
    citations: list[ResearchCitation] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] | None = None


class ExaMarketResearchInput(ToolInput):
    query: str = Field(min_length=3, max_length=500)
    symbol: str | None = Field(default=None, max_length=16)
    search_type: SearchType = "deep"
    num_results: int = Field(default=10, ge=1, le=20)
    max_age_hours: int | None = Field(default=None, ge=-1)


class ExaMarketResearchOutput(ToolOutput):
    request_id: str | None = None
    brief: ResearchBrief = Field(
        default_factory=lambda: ResearchBrief(headline="", summary="")
    )
    grounding: list[ResearchGrounding] = Field(default_factory=list)
    sources: list[ResearchCitation] = Field(default_factory=list)
    cost_dollars: float | None = None


PostJson = Callable[[str, dict[str, str], dict[str, object]], Awaitable[dict[str, object]]]


def _read_exa_api_key_from_dotenv() -> str | None:
    """Read EXA_API_KEY from the repo-root .env when it was not exported."""
    search_roots = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    seen: set[Path] = set()
    for parent in search_roots:
        if parent in seen:
            continue
        seen.add(parent)
        env_path = parent / ".env"
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key.strip() == "EXA_API_KEY":
                    cleaned = value.strip().strip('"').strip("'")
                    return cleaned or None
        except OSError:
            return None
    return None


def _brief_output_schema() -> dict[str, object]:
    string_array = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "description": "Trading research brief grounded in cited web sources.",
        "required": ["headline", "summary", "bullCase", "bearCase", "catalysts", "risks", "watchItems"],
        "properties": {
            "headline": {"type": "string", "description": "One concise operator-facing headline."},
            "summary": {"type": "string", "description": "Short synthesis of the research result."},
            "bullCase": string_array,
            "bearCase": string_array,
            "catalysts": string_array,
            "risks": string_array,
            "watchItems": string_array,
        },
    }


async def _post_json(url: str, headers: dict[str, str], body: dict[str, object]) -> dict[str, object]:
    payload = json.dumps(body).encode("utf-8")

    def send() -> dict[str, object]:
        if not url.startswith("https://"):
            raise ToolBackendError("Exa search URL must use HTTPS")
        req = request.Request(url, data=payload, headers=headers, method="POST")  # noqa: S310
        try:
            with request.urlopen(req, timeout=45) as response:  # noqa: S310
                response_body = response.read().decode("utf-8")
        except error.URLError as exc:
            raise ToolBackendError(f"Exa search request failed: {exc}") from exc
        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise ToolBackendError("Exa search returned a non-object JSON response")
        return parsed

    return await asyncio.to_thread(send)


class ExaMarketResearchTool(BaseTool):
    name = "research.exa_market"
    description = "Read-only Exa web research that returns a structured trading brief with citations."
    input_model = ExaMarketResearchInput
    output_model = ExaMarketResearchOutput

    def __init__(self, post_json: PostJson = _post_json) -> None:
        self._post_json = post_json

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, ExaMarketResearchInput)
        api_key = _read_exa_api_key_from_dotenv() or os.getenv("EXA_API_KEY")
        if not api_key:
            raise MissingExaApiKey("Set EXA_API_KEY before using Exa research tools.")

        body: dict[str, object] = {
            "query": payload.query,
            "type": payload.search_type,
            "numResults": payload.num_results,
            "contents": {"highlights": {"maxCharacters": 4000}},
            "outputSchema": _brief_output_schema(),
            "systemPrompt": (
                "Produce a concise trading-platform research brief. Separate evidence from inference, "
                "do not recommend order placement, and prefer source-grounded catalysts and risks."
            ),
        }
        if payload.symbol:
            body["systemPrompt"] = f"{body['systemPrompt']} Focus on symbol {payload.symbol.upper()}."
        if payload.max_age_hours is not None:
            contents = body["contents"]
            assert isinstance(contents, dict)
            contents["maxAgeHours"] = payload.max_age_hours

        response = await self._post_json(
            EXA_SEARCH_URL,
            {
                "Content-Type": "application/json",
                "User-Agent": "FinceptTerminal/0.1",
                "x-api-key": api_key,
            },
            body,
        )

        output = response.get("output", {})
        if not isinstance(output, dict):
            raise ToolBackendError("Exa search response omitted output object")
        content = output.get("content", {})
        if not isinstance(content, dict):
            raise ToolBackendError("Exa search response omitted structured output.content")

        results = response.get("results", [])
        sources: list[ResearchCitation] = []
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    sources.append(
                        ResearchCitation(url=item["url"], title=item.get("title") if isinstance(item.get("title"), str) else None)
                    )

        cost = response.get("costDollars", {})
        cost_dollars = cost.get("total") if isinstance(cost, dict) else None
        request_id_value = response.get("requestId")
        request_id = request_id_value if isinstance(request_id_value, str) else None

        return ExaMarketResearchOutput(
            request_id=request_id,
            brief=ResearchBrief.model_validate(content),
            grounding=[
                ResearchGrounding.model_validate(item)
                for item in output.get("grounding", [])
                if isinstance(item, dict)
            ],
            sources=sources,
            cost_dollars=cost_dollars if isinstance(cost_dollars, int | float) else None,
        )


register(ExaMarketResearchTool())
