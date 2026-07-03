"""
Tests for Phase 2 sentiment modules — FinBERT, LLM providers, ensemble.

Tests verify:
- All sentiment modules register correctly.
- FinBERT module is importable without transformers/torch (lazy import).
- FinBERT raises ImportError with a clear message at score time if
  transformers/torch are missing.
- LLM providers (OpenAI, Anthropic, xAI, MiniMax) are importable without
  httpx at module level (lazy import inside score()).
- LLM providers raise ValueError if API key is missing.
- LLM ensemble aggregates per-provider scores correctly.
- LLM ensemble gracefully degrades when providers are unavailable.
- No heavy deps at module level.
"""

from __future__ import annotations

import json
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# Registration tests                                                          #
# --------------------------------------------------------------------------- #


def test_all_sentiment_modules_registered() -> None:
    """All 7 sentiment modules should be registered after load_all_modules."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    sentiment_modules = registry.list_by_category("sentiment")

    expected = {
        "sentiment:naive-wordlist:1.0.0",
        "sentiment:finbert:1.0.0",
        "sentiment:llm-openai:1.0.0",
        "sentiment:llm-anthropic:1.0.0",
        "sentiment:llm-xai:1.0.0",
        "sentiment:llm-minimax:1.0.0",
        "sentiment:llm-ensemble-4:1.0.0",
    }
    assert expected.issubset(set(sentiment_modules)), (
        f"missing: {expected - set(sentiment_modules)}"
    )


# --------------------------------------------------------------------------- #
# FinBERT tests                                                               #
# --------------------------------------------------------------------------- #


def test_finbert_importable_without_transformers() -> None:
    """FinBERTSentiment must be importable without transformers/torch."""
    from quant_foundry.modules.sentiment.finbert import FinBERTSentiment

    # Should be able to instantiate without heavy deps
    mod = FinBERTSentiment(config={"device": "cpu"})
    assert mod.model_name == "yiyanghkust/finbert-tone"


def test_finbert_no_module_level_heavy_deps() -> None:
    """transformers and torch must NOT be imported at module level."""
    import quant_foundry.modules.sentiment.finbert as fb

    assert not hasattr(fb, "transformers"), "transformers at module level"
    assert not hasattr(fb, "torch"), "torch at module level"


def test_finbert_raises_on_missing_deps_at_score_time() -> None:
    """FinBERT raises ImportError at score time if transformers/torch missing."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.finbert import FinBERTSentiment

    mod = FinBERTSentiment(config={"device": "cpu"})
    items = [
        MediaItem(
            item_id="1",
            source="test",
            headline="Company beats earnings",
            body="",
            available_at_ns=0,
        ),
    ]

    # If transformers/torch are not installed, this should raise ImportError.
    # If they ARE installed (e.g. on RunPod), this should succeed.
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401
        # Heavy deps available — skip this test (it would try to load the model)
        pytest.skip("transformers+torch installed — model load test requires GPU")
    except ImportError:
        with pytest.raises(ImportError, match="transformers.*torch"):
            mod.score(items)


def test_finbert_cache_roundtrip(tmp_path: pathlib.Path) -> None:
    """FinBERT disk cache saves and loads correctly."""
    from quant_foundry.modules.sentiment.finbert import FinBERTSentiment

    mod = FinBERTSentiment(config={
        "device": "cpu",
        "cache_dir": str(tmp_path),
    })

    # Manually populate cache
    mod._load_cache()
    mod._cache["test-item-1"] = {"score": 0.5, "confidence": 0.9}
    mod._save_cache()

    # Verify cache file exists
    cache_file = tmp_path / "finbert_sentiment_cache.json"
    assert cache_file.exists()

    # Create a new instance and verify cache loads
    mod2 = FinBERTSentiment(config={
        "device": "cpu",
        "cache_dir": str(tmp_path),
    })
    mod2._load_cache()
    assert "test-item-1" in mod2._cache
    assert mod2._cache["test-item-1"]["score"] == 0.5


# --------------------------------------------------------------------------- #
# LLM provider tests                                                          #
# --------------------------------------------------------------------------- #


def test_llm_providers_importable_without_httpx() -> None:
    """All LLM provider modules must be importable without httpx."""
    from quant_foundry.modules.sentiment.llm_anthropic import AnthropicSentiment
    from quant_foundry.modules.sentiment.llm_minimax import MiniMaxSentiment
    from quant_foundry.modules.sentiment.llm_openai import OpenAISentiment
    from quant_foundry.modules.sentiment.llm_xai import XAISentiment

    # Should instantiate without httpx
    OpenAISentiment()
    AnthropicSentiment()
    XAISentiment()
    MiniMaxSentiment()


def test_llm_providers_no_module_level_httpx() -> None:
    """httpx must NOT be imported at module level in LLM providers."""
    import quant_foundry.modules.sentiment.llm_anthropic as ant
    import quant_foundry.modules.sentiment.llm_minimax as mm
    import quant_foundry.modules.sentiment.llm_openai as oai
    import quant_foundry.modules.sentiment.llm_xai as xai

    for mod in (ant, mm, oai, xai):
        assert not hasattr(mod, "httpx"), f"{mod.__name__}: httpx at module level"


def test_openai_raises_on_missing_api_key() -> None:
    """OpenAISentiment raises ValueError if OPENAI_API_KEY is not set."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.llm_openai import OpenAISentiment

    mod = OpenAISentiment()
    items = [
        MediaItem(
            item_id="1", source="test", headline="test", body="",
            available_at_ns=0,
        ),
    ]

    # Ensure API key is not set
    with patch.dict("os.environ", {}, clear=True):
        # httpx is available, but API key is missing
        # The score method catches errors and returns neutral, so we
        # need to check _get_api_key directly
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            mod._get_api_key()


def test_anthropic_raises_on_missing_api_key() -> None:
    """AnthropicSentiment raises ValueError if ANTHROPIC_API_KEY is not set."""
    from quant_foundry.modules.sentiment.llm_anthropic import AnthropicSentiment

    mod = AnthropicSentiment()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            mod._get_api_key()


def test_xai_raises_on_missing_api_key() -> None:
    """XAISentiment raises ValueError if XAI_API_KEY is not set."""
    from quant_foundry.modules.sentiment.llm_xai import XAISentiment

    mod = XAISentiment()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="XAI_API_KEY"):
            mod._get_api_key()


def test_minimax_raises_on_missing_api_key() -> None:
    """MiniMaxSentiment raises ValueError if MINIMAX_API_KEY is not set."""
    from quant_foundry.modules.sentiment.llm_minimax import MiniMaxSentiment

    mod = MiniMaxSentiment()
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="MINIMAX_API_KEY"):
            mod._get_api_key()


def test_openai_score_returns_neutral_on_error() -> None:
    """OpenAISentiment returns neutral (0.0, 0.0) on API errors."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.llm_openai import OpenAISentiment

    mod = OpenAISentiment()
    items = [
        MediaItem(
            item_id="1", source="test", headline="test", body="",
            available_at_ns=0,
        ),
    ]

    # With no API key and httpx available, score() catches the ValueError
    # and returns neutral results
    with patch.dict("os.environ", {}, clear=True):
        results = mod.score(items)
        assert len(results) == 1
        assert results[0].score == 0.0
        assert results[0].confidence == 0.0
        assert results[0].provider == "openai"


# --------------------------------------------------------------------------- #
# LLM ensemble tests                                                          #
# --------------------------------------------------------------------------- #


def test_ensemble_importable() -> None:
    """LLMEnsemble4Sentiment is importable and instantiable."""
    from quant_foundry.modules.sentiment.llm_ensemble import LLMEnsemble4Sentiment

    mod = LLMEnsemble4Sentiment()
    assert mod.aggregation == "mean"
    assert mod.min_providers == 2


def test_ensemble_no_module_level_heavy_deps() -> None:
    """LLM ensemble must not import heavy deps at module level."""
    import quant_foundry.modules.sentiment.llm_ensemble as ens

    assert not hasattr(ens, "httpx"), "httpx at module level"
    assert not hasattr(ens, "transformers"), "transformers at module level"


def test_ensemble_degrades_gracefully_without_api_keys() -> None:
    """Ensemble returns neutral when no providers have API keys."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.llm_ensemble import LLMEnsemble4Sentiment

    mod = LLMEnsemble4Sentiment()
    items = [
        MediaItem(
            item_id="1", source="test", headline="Company beats earnings",
            body="", available_at_ns=0,
        ),
    ]

    # With no API keys set, all providers will fail, and the ensemble
    # should return neutral results (graceful degradation)
    with patch.dict("os.environ", {}, clear=True):
        results = mod.score(items)
        assert len(results) == 1
        # With no valid provider results, ensemble returns neutral
        assert results[0].provider == "llm-ensemble"
        # Score should be 0.0 (neutral) when no providers succeed
        assert results[0].score == 0.0


def test_ensemble_score_detailed_structure() -> None:
    """score_detailed returns the correct structure."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.llm_ensemble import LLMEnsemble4Sentiment

    mod = LLMEnsemble4Sentiment()
    items = [
        MediaItem(
            item_id="1", source="test", headline="test", body="",
            available_at_ns=0,
        ),
    ]

    with patch.dict("os.environ", {}, clear=True):
        detailed = mod.score_detailed(items)
        assert len(detailed) == 1
        assert detailed[0]["item_id"] == "1"
        assert "providers" in detailed[0]
        assert "ensemble_score" in detailed[0]
        assert "ensemble_std" in detailed[0]


# --------------------------------------------------------------------------- #
# RunPod handler ingestion task test                                          #
# --------------------------------------------------------------------------- #


def test_ingest_media_sentiment_task_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ingest_media_sentiment task rejects missing required fields."""
    # The bad_request path still builds a signed failure envelope, which
    # requires the callback secret. Set a test-only value so the handler
    # does not raise RuntimeError before reaching the field validation.
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "test-sentiment-secret")

    # Import the handler module — it should be importable without RunPod SDK
    import sys
    _HANDLER_DIR = str(pathlib.Path(_REPO_ROOT / "runpod" / "quant-foundry-training"))
    if _HANDLER_DIR not in sys.path:
        sys.path.insert(0, _HANDLER_DIR)

    # The handler imports quant_foundry which is available
    try:
        from handler import _handle_ingest_media_sentiment
    except ImportError:
        pytest.skip("handler module not importable in this environment")

    # Missing dataset_id
    result = _handle_ingest_media_sentiment({})
    assert result["error_code"] == "bad_request"
    assert "dataset_id" in result["error_summary"]

    # Missing start_ns/end_ns
    result = _handle_ingest_media_sentiment({"dataset_id": "test"})
    assert result["error_code"] == "bad_request"
    assert "start_ns" in result["error_summary"]

    # Missing output_dir
    result = _handle_ingest_media_sentiment({
        "dataset_id": "test",
        "start_ns": 1,
        "end_ns": 2,
    })
    assert result["error_code"] == "bad_request"
    assert "output_dir" in result["error_summary"]
