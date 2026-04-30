"""Unit tests for LLMRouter + is_unrecoverable_provider_error.

Mocks score_article so we can simulate provider responses (success,
billing failure, auth failure, transient 5xx, parse-fail) deterministically
and confirm the router's fallback / exhaustion semantics.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agents.sentiment_agent.llm import (
    LLMRouter,
    SentimentScore,
    is_unrecoverable_provider_error,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _http_status_error(status: int, body: str) -> httpx.HTTPStatusError:
    """Build a real HTTPStatusError so the .response surface matches prod."""
    request = httpx.Request("POST", "https://example.com/v1/messages")
    response = httpx.Response(status, content=body.encode(), request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


def _score(value: float = 0.5) -> SentimentScore:
    return SentimentScore(
        score=value,
        confidence=0.7,
        event_type="general",
        rationale="test",
    )


# --------------------------------------------------------------------------- #
# is_unrecoverable_provider_error                                             #
# --------------------------------------------------------------------------- #


class TestIsUnrecoverable:
    def test_anthropic_billing_400(self) -> None:
        exc = _http_status_error(
            400,
            '{"error":{"message":"Your credit balance is too low to access the Anthropic API."}}',
        )
        assert is_unrecoverable_provider_error(exc) is True

    def test_openai_insufficient_quota_429(self) -> None:
        exc = _http_status_error(
            429,
            '{"error":{"type":"insufficient_quota","message":"You exceeded your current quota"}}',
        )
        assert is_unrecoverable_provider_error(exc) is True

    def test_401_is_unrecoverable(self) -> None:
        exc = _http_status_error(401, '{"error":"unauthorized"}')
        assert is_unrecoverable_provider_error(exc) is True

    def test_403_is_unrecoverable(self) -> None:
        exc = _http_status_error(403, '{"error":"forbidden"}')
        assert is_unrecoverable_provider_error(exc) is True

    def test_500_is_recoverable(self) -> None:
        """Server errors are transient - keep using same provider."""
        exc = _http_status_error(500, '{"error":"internal"}')
        assert is_unrecoverable_provider_error(exc) is False

    def test_429_without_quota_is_recoverable(self) -> None:
        """Plain rate limit (no insufficient_quota) is transient."""
        exc = _http_status_error(429, '{"error":{"message":"too many requests"}}')
        assert is_unrecoverable_provider_error(exc) is False

    def test_400_validation_error_is_recoverable(self) -> None:
        """A 400 from a bad request body (not billing) shouldn't kill the provider."""
        exc = _http_status_error(400, '{"error":{"message":"invalid model name"}}')
        assert is_unrecoverable_provider_error(exc) is False

    def test_non_http_error_returns_false(self) -> None:
        assert is_unrecoverable_provider_error(ValueError("not http")) is False
        assert is_unrecoverable_provider_error(httpx.ConnectTimeout("x")) is False


# --------------------------------------------------------------------------- #
# LLMRouter                                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def patched_score_article(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the module-level score_article with an AsyncMock.

    Tests configure ``side_effect`` on the mock to inject success or
    failure per call; the router invokes it once per provider attempt.
    """
    mock = AsyncMock()
    monkeypatch.setattr("agents.sentiment_agent.llm.score_article", mock)
    return mock


def _client() -> Any:
    """A throwaway sentinel - the mock doesn't inspect it."""
    return MagicMock(spec=httpx.AsyncClient)


class TestLLMRouterFallback:
    @pytest.mark.asyncio
    async def test_first_provider_success(
        self, patched_score_article: AsyncMock
    ) -> None:
        patched_score_article.return_value = _score(0.6)
        router = LLMRouter([("anthropic", "k1"), ("openai", "k2")])

        result = await router.score(
            _client(), symbol="BTC-USD", title="t", description="d", source="s"
        )

        assert result is not None
        score, provider = result
        assert provider == "anthropic"
        assert score.score == pytest.approx(0.6)
        # Only the first provider was called.
        patched_score_article.assert_awaited_once()
        # Anthropic still has capacity.
        assert "anthropic" not in router.exhausted_providers()

    @pytest.mark.asyncio
    async def test_falls_back_on_billing_error(
        self, patched_score_article: AsyncMock
    ) -> None:
        patched_score_article.side_effect = [
            _http_status_error(
                400,
                '{"error":{"message":"Your credit balance is too low"}}',
            ),
            _score(0.4),
        ]
        router = LLMRouter([("anthropic", "k1"), ("openai", "k2")])

        result = await router.score(
            _client(), symbol="BTC-USD", title="t", description="d", source="s"
        )

        assert result is not None
        score, provider = result
        assert provider == "openai"
        assert score.score == pytest.approx(0.4)
        # Anthropic was permanently marked exhausted.
        assert "anthropic" in router.exhausted_providers()
        assert "openai" not in router.exhausted_providers()

    @pytest.mark.asyncio
    async def test_exhaustion_persists_across_calls(
        self, patched_score_article: AsyncMock
    ) -> None:
        """Once Anthropic fails on call #1, call #2 should skip straight to OpenAI."""
        patched_score_article.side_effect = [
            _http_status_error(401, '{"error":"unauthorized"}'),
            _score(0.3),
            _score(0.5),
        ]
        router = LLMRouter([("anthropic", "k1"), ("openai", "k2")])

        first = await router.score(
            _client(), symbol="X", title="t", description="d", source="s"
        )
        second = await router.score(
            _client(), symbol="X", title="t", description="d", source="s"
        )

        # First call: Anthropic 401 -> fallback to OpenAI returns 0.3
        assert first is not None and first[0].score == pytest.approx(0.3)
        # Second call: Anthropic skipped, OpenAI returns 0.5 directly
        assert second is not None and second[0].score == pytest.approx(0.5)
        # Total upstream calls: 1 (anthropic 401) + 1 (openai 0.3) + 1 (openai 0.5) = 3
        assert patched_score_article.await_count == 3

    @pytest.mark.asyncio
    async def test_returns_none_when_all_exhausted(
        self, patched_score_article: AsyncMock
    ) -> None:
        patched_score_article.side_effect = [
            _http_status_error(
                400,
                '{"error":{"message":"credit balance"}}',
            ),
            _http_status_error(401, '{"error":"unauthorized"}'),
        ]
        router = LLMRouter([("anthropic", "k1"), ("openai", "k2")])

        result = await router.score(
            _client(), symbol="X", title="t", description="d", source="s"
        )

        assert result is None
        assert router.has_capacity is False
        assert set(router.exhausted_providers()) == {"anthropic", "openai"}

    @pytest.mark.asyncio
    async def test_transient_error_propagates(
        self, patched_score_article: AsyncMock
    ) -> None:
        """5xx / transient should NOT mark exhausted - operator's first
        provider stays available for the next cycle."""
        patched_score_article.side_effect = _http_status_error(
            500, '{"error":"internal"}'
        )
        router = LLMRouter([("anthropic", "k1"), ("openai", "k2")])

        with pytest.raises(httpx.HTTPStatusError):
            await router.score(
                _client(), symbol="X", title="t", description="d", source="s"
            )

        # No provider was marked exhausted - we still have Anthropic.
        assert router.has_capacity is True
        assert router.current == ("anthropic", "k1")

    @pytest.mark.asyncio
    async def test_parse_failure_returns_none_without_exhausting(
        self, patched_score_article: AsyncMock
    ) -> None:
        """A None return from score_article (LLM returned non-JSON) is
        transient - one bad article shouldn't kill the provider."""
        patched_score_article.return_value = None
        router = LLMRouter([("anthropic", "k1"), ("openai", "k2")])

        result = await router.score(
            _client(), symbol="X", title="t", description="d", source="s"
        )

        assert result is None
        # Anthropic is still healthy.
        assert "anthropic" not in router.exhausted_providers()
        assert router.has_capacity is True

    @pytest.mark.asyncio
    async def test_single_provider_no_fallback(
        self, patched_score_article: AsyncMock
    ) -> None:
        patched_score_article.side_effect = _http_status_error(
            400, '{"error":{"message":"credit balance"}}'
        )
        router = LLMRouter([("anthropic", "k1")])

        result = await router.score(
            _client(), symbol="X", title="t", description="d", source="s"
        )

        assert result is None
        assert router.has_capacity is False

    def test_configured_providers_returns_order(self) -> None:
        router = LLMRouter([("anthropic", "k1"), ("openai", "k2")])
        assert router.configured_providers() == ["anthropic", "openai"]

    def test_mark_exhausted_first_reason_wins(self) -> None:
        """Marking the same provider twice keeps the original reason."""
        router = LLMRouter([("anthropic", "k1")])
        router.mark_exhausted("anthropic", "first reason")
        router.mark_exhausted("anthropic", "second reason")
        assert router.exhausted_providers()["anthropic"] == "first reason"
