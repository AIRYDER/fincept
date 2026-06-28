"""
quant_foundry.modules.sentiment.llm_anthropic — Anthropic LLM sentiment provider.

Scores media items for sentiment using Anthropic's Messages API (Claude).
Uses ``httpx`` directly (no SDK dependency).  The API key is read from
``ANTHROPIC_API_KEY``.

This module is registered as ``sentiment:llm-anthropic:1.0.0``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)
from quant_foundry.modules.sentiment.language import (
    detect_language,
    translate_prompt,
)

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_API_VERSION = "2023-06-01"

_SYSTEM_PROMPT = (
    "You are a financial sentiment analyzer. Given a social media post "
    "or news headline about a stock, return a JSON object with two fields: "
    '"score" (a float in [-1, 1] where -1 is very bearish, 0 is neutral, '
    "1 is very bullish) and \"confidence\" (a float in [0, 1] indicating "
    "how confident you are in the assessment). "
    "Return ONLY the JSON object, no other text."
)


@register_module(
    "sentiment",
    "llm-anthropic",
    "1.0.0",
    default_config={
        "model": DEFAULT_MODEL,
        "base_url": DEFAULT_BASE_URL,
        "api_version": DEFAULT_API_VERSION,
        "timeout": 30.0,
        "max_tokens": 100,
        "language": "auto",  # "auto" or an ISO 639-1 code (e.g. "zh")
    },
)
class AnthropicSentiment:
    """Anthropic LLM sentiment provider.

    Scores each :class:`MediaItem` by sending the headline + body to
    Anthropic's Messages API and parsing the JSON response.

    Requires ``ANTHROPIC_API_KEY`` env var to be set.

    When ``language="auto"`` (default), each item's language is detected
    and a language-appropriate prompt is used so the LLM analyzes
    sentiment in the text's native language.  Set ``language`` to a
    specific ISO 639-1 code to force that language.  The default
    ``"auto"`` behavior is identical to the original English prompt for
    English text (backward compatible).
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.model: str = self.config.get("model", DEFAULT_MODEL)
        self.base_url: str = self.config.get("base_url", DEFAULT_BASE_URL)
        self.api_version: str = self.config.get("api_version", DEFAULT_API_VERSION)
        self.timeout: float = self.config.get("timeout", 30.0)
        self.max_tokens: int = self.config.get("max_tokens", 100)
        self.language: str = self.config.get("language", "auto")

    def _system_prompt_for(self, text: str) -> str:
        """Return the system prompt appropriate for the item's language."""
        if self.language == "auto":
            lang = detect_language(text)
        else:
            lang = self.language
        return translate_prompt(lang)

    def _get_api_key(self) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. Set it in the environment "
                "or RunPod container env."
            )
        return key

    def score(self, items: list[MediaItem]) -> list[SentimentResult]:
        """Score media items using Anthropic Messages API."""
        import httpx

        try:
            api_key = self._get_api_key()
        except ValueError:
            return [
                SentimentResult(item_id=item.item_id, provider="anthropic", score=0.0, confidence=0.0)
                for item in items
            ]

        headers = {
            "x-api-key": api_key,
            "anthropic-version": self.api_version,
            "Content-Type": "application/json",
        }

        results: list[SentimentResult] = []
        for item in items:
            try:
                system_prompt = self._system_prompt_for(item.text)
                payload = {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": item.text[:2000]},
                    ],
                }
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(
                        f"{self.base_url}/messages",
                        headers=headers,
                        json=payload,
                    )
                    resp.raise_for_status()
                    body = resp.json()

                content = body["content"][0]["text"].strip()
                parsed = json.loads(content)
                score = float(parsed.get("score", 0.0))
                confidence = float(parsed.get("confidence", 0.5))

                score = max(-1.0, min(1.0, score))
                confidence = max(0.0, min(1.0, confidence))

                results.append(SentimentResult(
                    item_id=item.item_id,
                    provider="anthropic",
                    score=round(score, 6),
                    confidence=round(confidence, 6),
                ))
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError, TypeError):
                results.append(SentimentResult(
                    item_id=item.item_id,
                    provider="anthropic",
                    score=0.0,
                    confidence=0.0,
                ))

        return results


__all__ = ["AnthropicSentiment"]
