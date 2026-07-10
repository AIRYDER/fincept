"""Unit tests for the orchestrator's SentimentSignal -> Prediction adapter."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fincept_core.events import Event
from fincept_core.schemas import Prediction, SentimentSignal

from orchestrator.main import (
    SENTIMENT_HORIZON_NS,
    _make_sentiment_handler,
    _sentiment_to_prediction,
)


def _make_signal(
    *,
    score: float = 0.5,
    confidence: float = 0.7,
    symbol: str = "BTC-USD",
    agent_id: str = "sentiment_agent.v1",
    ts_event: int = 1_700_000_000_000_000_000,
) -> SentimentSignal:
    return SentimentSignal(
        agent_id=agent_id,
        symbol=symbol,
        ts_event=ts_event,
        score=score,
        confidence=confidence,
        event_type="regulatory",
        source_url="https://example.com/article",
        source_excerpt="Test article",
        entities=[symbol],
    )


class TestSentimentToPrediction:
    """The adapter must preserve enough of the SentimentSignal to feed
    ConsensusBuilder identically to a native Prediction.

    Key invariants (these are what cross-source consensus depends on):
      - direction <- score (range [-1, 1])
      - confidence passes through unchanged
      - agent_id passes through so consensus tracks per-source slots
      - ts_event passes through so staleness checks are accurate
      - horizon_ns is the longer SENTIMENT_HORIZON_NS, not whatever was
        on the source signal (sentiment has slower decay than ticks)
    """

    def test_direction_is_score(self) -> None:
        signal = _make_signal(score=-0.7)
        prediction = _sentiment_to_prediction(signal)
        assert prediction.direction == pytest.approx(-0.7)

    def test_confidence_passes_through(self) -> None:
        signal = _make_signal(confidence=0.42)
        prediction = _sentiment_to_prediction(signal)
        assert prediction.confidence == pytest.approx(0.42)

    def test_agent_id_passes_through(self) -> None:
        signal = _make_signal(agent_id="news_v2")
        prediction = _sentiment_to_prediction(signal)
        assert prediction.agent_id == "news_v2"

    def test_horizon_is_sentiment_default(self) -> None:
        signal = _make_signal()
        prediction = _sentiment_to_prediction(signal)
        assert prediction.horizon_ns == SENTIMENT_HORIZON_NS

    def test_symbol_and_ts_passthrough(self) -> None:
        signal = _make_signal(symbol="ETH-USD", ts_event=12345)
        prediction = _sentiment_to_prediction(signal)
        assert prediction.symbol == "ETH-USD"
        assert prediction.ts_event == 12345


class TestSentimentHandler:
    @pytest.mark.asyncio
    async def test_calls_router_with_translated_prediction(self) -> None:
        router = AsyncMock()
        handler = _make_sentiment_handler(router)
        signal = _make_signal(score=0.8, confidence=0.9)
        event: Event[Any] = Event(type="sentiment", payload=signal)

        await handler(event)

        router.on_prediction.assert_awaited_once()
        call_arg = router.on_prediction.call_args.args[0]
        assert isinstance(call_arg, Prediction)
        assert call_arg.direction == pytest.approx(0.8)
        assert call_arg.confidence == pytest.approx(0.9)
        assert call_arg.agent_id == signal.agent_id

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        """Defensive: STREAM_SIG_SENT should only carry sentiment events,
        but if anything else lands there it must not be re-routed."""
        router = AsyncMock()
        handler = _make_sentiment_handler(router)
        # Wrong type discriminator.
        event: Event[Any] = Event(
            type="prediction",  # type discriminator says "prediction"
            payload=_make_signal(),
        )

        await handler(event)

        router.on_prediction.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_wrong_payload_type(self) -> None:
        """If the payload isn't a SentimentSignal, the handler bails."""
        router = AsyncMock()
        handler = _make_sentiment_handler(router)

        # Build a mismatched event by hand (not via Event() generic).
        class _Fake:
            type = "sentiment"
            payload = "not a SentimentSignal"  # type: ignore[assignment]

        await handler(_Fake())  # type: ignore[arg-type]

        router.on_prediction.assert_not_called()
