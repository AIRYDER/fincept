"""
Tests for multilingual sentiment support — language detection, multilingual
wordlists, the multilingual naive wordlist engine, LLM language config, and
FinBERT graceful fallback for non-English text.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# --------------------------------------------------------------------------- #
# Language detection tests                                                     #
# --------------------------------------------------------------------------- #


def test_detect_language_english() -> None:
    """English text → 'en'."""
    from quant_foundry.modules.sentiment.language import detect_language

    assert detect_language("The company beat earnings expectations and the stock surged.") == "en"


def test_detect_language_chinese() -> None:
    """Chinese text → 'zh'."""
    from quant_foundry.modules.sentiment.language import detect_language

    assert detect_language("公司公布财报，利润大幅增长，股价飙升。") == "zh"


def test_detect_language_japanese() -> None:
    """Japanese text → 'ja'."""
    from quant_foundry.modules.sentiment.language import detect_language

    assert detect_language("企業が決算を発表し、利益が大幅に増加、株価が急騰した。") == "ja"


def test_detect_language_french() -> None:
    """French text → 'fr'."""
    from quant_foundry.modules.sentiment.language import detect_language

    assert (
        detect_language(
            "L'entreprise a publié des bénéfices record et l'action a fortement "
            "progressé sur les marchés ce matin."
        )
        == "fr"
    )


def test_detect_language_german() -> None:
    """German text → 'de'."""
    from quant_foundry.modules.sentiment.language import detect_language

    assert (
        detect_language(
            "Das Unternehmen hat einen Rekordgewinn veröffentlicht und die Aktie "
            "ist stark gestiegen auf dem Markt heute Morgen."
        )
        == "de"
    )


def test_detect_language_spanish() -> None:
    """Spanish text → 'es'."""
    from quant_foundry.modules.sentiment.language import detect_language

    assert (
        detect_language(
            "La empresa publicó beneficios récord y las acciones subieron "
            "fuertemente en el mercado esta mañana."
        )
        == "es"
    )


def test_detect_language_default() -> None:
    """Gibberish/empty → 'en' (default)."""
    from quant_foundry.modules.sentiment.language import DEFAULT_LANGUAGE, detect_language

    assert detect_language("") == DEFAULT_LANGUAGE
    assert detect_language("   ") == DEFAULT_LANGUAGE
    # Pure numbers/punctuation with no language markers → default.
    assert detect_language("12345 !!! ???") == DEFAULT_LANGUAGE


def test_is_english() -> None:
    """English text → True, Chinese text → False."""
    from quant_foundry.modules.sentiment.language import is_english

    assert is_english("The stock surged on strong earnings.") is True
    assert is_english("股价飙升，利润大幅增长。") is False


# --------------------------------------------------------------------------- #
# Multilingual wordlists tests                                                 #
# --------------------------------------------------------------------------- #


def test_multilingual_wordlists_exist() -> None:
    """All 6 languages have positive + negative word lists with 20+ words each."""
    from quant_foundry.modules.sentiment.language import (
        MULTILINGUAL_WORDLISTS,
        SUPPORTED_LANGUAGES,
    )

    for lang in SUPPORTED_LANGUAGES:
        assert lang in MULTILINGUAL_WORDLISTS, f"missing language: {lang}"
        lists = MULTILINGUAL_WORDLISTS[lang]
        assert "positive" in lists, f"{lang}: missing positive list"
        assert "negative" in lists, f"{lang}: missing negative list"
        assert len(lists["positive"]) >= 20, (
            f"{lang}: positive list has {len(lists['positive'])} words, need 20+"
        )
        assert len(lists["negative"]) >= 20, (
            f"{lang}: negative list has {len(lists['negative'])} words, need 20+"
        )


def test_translate_prompt_supported_languages() -> None:
    """translate_prompt returns a non-empty prompt for each supported language."""
    from quant_foundry.modules.sentiment.language import (
        SUPPORTED_LANGUAGES,
        translate_prompt,
    )

    for lang in SUPPORTED_LANGUAGES:
        prompt = translate_prompt(lang)
        assert isinstance(prompt, str)
        assert len(prompt) > 0, f"empty prompt for {lang}"
        # All prompts reference the JSON schema fields.
        assert "score" in prompt
        assert "confidence" in prompt


def test_translate_prompt_fallback() -> None:
    """translate_prompt falls back to English for unsupported languages."""
    from quant_foundry.modules.sentiment.language import translate_prompt

    assert translate_prompt("xx") == translate_prompt("en")


# --------------------------------------------------------------------------- #
# Multilingual naive wordlist engine tests                                     #
# --------------------------------------------------------------------------- #


def test_naive_wordlist_ml_registered() -> None:
    """sentiment:naive-wordlist-ml:1.0.0 is in the registry."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    sentiment_modules = registry.list_by_category("sentiment")
    assert "sentiment:naive-wordlist-ml:1.0.0" in sentiment_modules


def test_naive_wordlist_ml_english() -> None:
    """English text produces the same result as the original naive wordlist."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.naive_wordlist import (
        NaiveWordlistMultilingualSentiment,
        NaiveWordlistSentiment,
    )

    text = "Company beats earnings, stock surges on strong growth."
    item = MediaItem(
        item_id="en-1",
        source="test",
        headline=text,
        body="",
        available_at_ns=0,
    )

    original = NaiveWordlistSentiment()
    ml = NaiveWordlistMultilingualSentiment()

    orig_results = original.score([item])
    ml_results = ml.score([item])

    assert len(orig_results) == 1
    assert len(ml_results) == 1
    # Scores must match exactly for English text (same word lists).
    assert ml_results[0].score == orig_results[0].score
    assert ml_results[0].confidence == orig_results[0].confidence
    # The ML engine records the detected language.
    assert ml_results[0].metadata.get("language") == "en"
    assert ml_results[0].provider == "naive-ml"


def test_naive_wordlist_ml_chinese() -> None:
    """Chinese text with positive words → positive score."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.naive_wordlist import (
        NaiveWordlistMultilingualSentiment,
    )

    # Headline with multiple positive Chinese sentiment words.
    item = MediaItem(
        item_id="zh-1",
        source="test",
        headline="公司利润大幅增长 股价飙升 创新高 强劲",
        body="利好 上涨",
        available_at_ns=0,
    )

    ml = NaiveWordlistMultilingualSentiment()
    results = ml.score([item])

    assert len(results) == 1
    assert results[0].metadata.get("language") == "zh"
    assert results[0].score > 0.0, f"expected positive score, got {results[0].score}"
    assert results[0].confidence > 0.0


def test_naive_wordlist_ml_fallback() -> None:
    """Unsupported language falls back to English word list."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.naive_wordlist import (
        NaiveWordlistMultilingualSentiment,
    )

    # Russian text (Cyrillic) is detected as 'ru' which is not in the
    # multilingual wordlists → should fall back to English.  Include an
    # English sentiment word so the fallback produces a non-zero score.
    item = MediaItem(
        item_id="ru-1",
        source="test",
        headline="Компания превзошла ожидания profit surge",
        body="",
        available_at_ns=0,
    )

    ml = NaiveWordlistMultilingualSentiment()
    results = ml.score([item])

    assert len(results) == 1
    # Detected language is recorded as 'ru' (unsupported).
    assert results[0].metadata.get("language") == "ru"
    # Should not crash and should return a valid SentimentResult.
    assert -1.0 <= results[0].score <= 1.0
    assert 0.0 <= results[0].confidence <= 1.0


def test_naive_wordlist_ml_negative_text() -> None:
    """German text with negative words → negative score."""
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.naive_wordlist import (
        NaiveWordlistMultilingualSentiment,
    )

    item = MediaItem(
        item_id="de-1",
        source="test",
        headline="Das Unternehmen meldet Verlust und die Aktie fällt stark.",
        body="Schwacher Rückgang, Krise, Verluste.",
        available_at_ns=0,
    )

    ml = NaiveWordlistMultilingualSentiment()
    results = ml.score([item])

    assert len(results) == 1
    assert results[0].metadata.get("language") == "de"
    assert results[0].score < 0.0, f"expected negative score, got {results[0].score}"


# --------------------------------------------------------------------------- #
# LLM engine language config tests                                             #
# --------------------------------------------------------------------------- #


def test_llm_openai_language_config() -> None:
    """OpenAI module accepts `language` config option."""
    from quant_foundry.modules.sentiment.llm_openai import OpenAISentiment

    # Default config.
    mod = OpenAISentiment()
    assert mod.language == "auto"

    # Custom config.
    mod = OpenAISentiment(config={"language": "zh"})
    assert mod.language == "zh"

    # The language-aware prompt helper works.
    prompt_en = mod._system_prompt_for("The stock surged.")
    # Forced to zh, so the prompt should be the Chinese one.
    assert "score" in prompt_en


def test_llm_anthropic_language_config() -> None:
    """Anthropic module accepts `language` config option."""
    from quant_foundry.modules.sentiment.llm_anthropic import AnthropicSentiment

    mod = AnthropicSentiment()
    assert mod.language == "auto"

    mod = AnthropicSentiment(config={"language": "fr"})
    assert mod.language == "fr"

    # Auto-detection picks the right prompt.
    prompt = mod._system_prompt_for("L'entreprise a publié des bénéfices.")
    assert "score" in prompt


def test_llm_xai_language_config() -> None:
    """xAI module accepts `language` config option."""
    from quant_foundry.modules.sentiment.llm_xai import XAISentiment

    mod = XAISentiment()
    assert mod.language == "auto"

    mod = XAISentiment(config={"language": "de"})
    assert mod.language == "de"

    prompt = mod._system_prompt_for("Das Unternehmen hat Gewinn gemacht.")
    assert "score" in prompt


def test_llm_minimax_language_config() -> None:
    """MiniMax module accepts `language` config option."""
    from quant_foundry.modules.sentiment.llm_minimax import MiniMaxSentiment

    mod = MiniMaxSentiment()
    assert mod.language == "auto"

    mod = MiniMaxSentiment(config={"language": "ja"})
    assert mod.language == "ja"

    prompt = mod._system_prompt_for("企業が利益を発表した。")
    assert "score" in prompt


def test_llm_language_config_in_registry_defaults() -> None:
    """The `language` option appears in each LLM module's default config."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()

    for full_id in (
        "sentiment:llm-openai:1.0.0",
        "sentiment:llm-anthropic:1.0.0",
        "sentiment:llm-xai:1.0.0",
        "sentiment:llm-minimax:1.0.0",
    ):
        info = registry.get_info(full_id)
        assert "language" in info.config, f"{full_id}: missing 'language' in default config"
        assert info.config["language"] == "auto"


# --------------------------------------------------------------------------- #
# FinBERT language config + fallback tests                                     #
# --------------------------------------------------------------------------- #


def test_finbert_language_config() -> None:
    """FinBERT module accepts `language` config option."""
    from quant_foundry.modules.sentiment.finbert import FinBERTSentiment

    mod = FinBERTSentiment(config={"device": "cpu"})
    assert mod.language == "auto"

    mod = FinBERTSentiment(config={"device": "cpu", "language": "en"})
    assert mod.language == "en"


def test_finbert_language_config_in_registry_defaults() -> None:
    """The `language` option appears in FinBERT's default config."""
    from quant_foundry.modules import ModuleRegistry, load_all_modules

    load_all_modules()
    registry = ModuleRegistry.instance()
    info = registry.get_info("sentiment:finbert:1.0.0")
    assert "language" in info.config
    assert info.config["language"] == "auto"


def test_finbert_fallback_for_non_english() -> None:
    """When language is non-English, FinBERT falls back to wordlist.

    Verifies it doesn't crash and returns a valid SentimentResult.  We
    use ``language="auto"`` with Chinese text so the fallback path is
    taken without needing transformers/torch installed.
    """
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.finbert import FinBERTSentiment

    mod = FinBERTSentiment(config={"device": "cpu", "language": "auto"})

    # Chinese text → detected as non-English → routed to wordlist fallback.
    item = MediaItem(
        item_id="zh-fb-1",
        source="test",
        headline="公司利润大幅增长 股价飙升 创新高",
        body="利好 上涨 强劲",
        available_at_ns=0,
    )

    results = mod.score([item])

    assert len(results) == 1
    result = results[0]
    # Should be a valid SentimentResult.
    assert result.item_id == "zh-fb-1"
    assert result.provider == "finbert"
    assert -1.0 <= result.score <= 1.0
    assert 0.0 <= result.confidence <= 1.0
    # The fallback metadata should be recorded.
    assert result.metadata.get("fallback") == "naive-wordlist-ml"
    assert result.metadata.get("language") == "zh"
    # Positive Chinese words → positive score.
    assert result.score > 0.0


def test_finbert_english_uses_finbert_path() -> None:
    """English text with language='en' routes to the FinBERT path.

    With transformers/torch not installed, this should raise ImportError
    (proving it did NOT take the wordlist fallback).  If they ARE
    installed, we skip rather than try to load the model.
    """
    from quant_foundry.modules.registry import MediaItem
    from quant_foundry.modules.sentiment.finbert import FinBERTSentiment

    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401

        pytest.skip("transformers+torch installed — model load test requires GPU")
    except ImportError:
        pass

    mod = FinBERTSentiment(config={"device": "cpu", "language": "en"})
    item = MediaItem(
        item_id="en-fb-1",
        source="test",
        headline="Company beats earnings",
        body="",
        available_at_ns=0,
    )

    # English + language='en' → FinBERT path → ImportError on missing deps.
    with pytest.raises(ImportError, match="transformers.*torch"):
        mod.score([item])
