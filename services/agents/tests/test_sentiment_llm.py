"""Unit tests for sentiment_agent.llm._parse_json_score.

These cover the most common real-world response shapes from Claude
Haiku, since strict JSON-only prompting is best-effort: the model
occasionally wraps in code fences or adds a brief preface.
"""

from __future__ import annotations

import pytest

from agents.sentiment_agent.llm import SentimentScore, _parse_json_score


def _expect(score: SentimentScore | None) -> SentimentScore:
    """Assert non-None and return - lets test bodies stay flat."""
    assert score is not None, "expected parse to succeed"
    return score


class TestParseJsonScore:
    def test_clean_json(self) -> None:
        text = (
            '{"score": 0.7, "confidence": 0.85, '
            '"event_type": "regulatory", "rationale": "favorable ruling"}'
        )
        score = _expect(_parse_json_score(text))
        assert score.score == pytest.approx(0.7)
        assert score.confidence == pytest.approx(0.85)
        assert score.event_type == "regulatory"
        assert score.rationale == "favorable ruling"

    def test_markdown_code_fence(self) -> None:
        text = (
            "```json\n"
            '{"score": -0.5, "confidence": 0.6, '
            '"event_type": "macro", "rationale": "rate hike"}\n'
            "```"
        )
        score = _expect(_parse_json_score(text))
        assert score.score == pytest.approx(-0.5)
        assert score.event_type == "macro"

    def test_clamps_score_above_one(self) -> None:
        """Models occasionally output 1.5 or -2.  Clamp to [-1, 1]."""
        text = '{"score": 1.5, "confidence": 0.9}'
        score = _expect(_parse_json_score(text))
        assert score.score == pytest.approx(1.0)

    def test_clamps_score_below_neg_one(self) -> None:
        text = '{"score": -2.0, "confidence": 0.9}'
        score = _expect(_parse_json_score(text))
        assert score.score == pytest.approx(-1.0)

    def test_clamps_confidence(self) -> None:
        text = '{"score": 0.0, "confidence": 1.5}'
        score = _expect(_parse_json_score(text))
        assert score.confidence == pytest.approx(1.0)

    def test_negative_confidence_clamps_to_zero(self) -> None:
        text = '{"score": 0.0, "confidence": -0.3}'
        score = _expect(_parse_json_score(text))
        assert score.confidence == pytest.approx(0.0)

    def test_returns_none_on_invalid_json(self) -> None:
        assert _parse_json_score("this is not JSON") is None

    def test_returns_none_on_array_root(self) -> None:
        """Root must be a JSON object, not an array."""
        assert _parse_json_score('[{"score": 0.5}]') is None

    def test_missing_score_defaults_to_zero(self) -> None:
        text = '{"confidence": 0.5, "event_type": "general"}'
        score = _expect(_parse_json_score(text))
        assert score.score == pytest.approx(0.0)

    def test_event_type_falls_back_to_general(self) -> None:
        text = '{"score": 0.0, "confidence": 0.0}'
        score = _expect(_parse_json_score(text))
        assert score.event_type == "general"

    def test_string_score_returns_none(self) -> None:
        """If score isn't coercible to float, fail closed (None)
        rather than silently treating it as 0."""
        text = '{"score": "very bullish", "confidence": 0.9}'
        assert _parse_json_score(text) is None
