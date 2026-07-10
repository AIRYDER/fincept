"""
quant_foundry.modules.sentiment.finbert — FinBERT sentiment engine for news.

Uses the `yiyanghkust/finbert-tone` model (finance-tuned BERT) to score
media items for sentiment.  FinBERT is the default sentiment engine for
news text on RunPod GPU workers — it's finance-tuned, free, deterministic,
and cacheable.

Heavy dependencies (``transformers``, ``torch``) are imported **lazily**
inside :meth:`__init__` / :meth:`score` so this module is importable
without them.  On a CPU-only machine the module raises a clear error at
score time; on a RunPod GPU worker it loads the model onto the GPU.

Results are cached by ``item_id`` to a JSON file on disk so re-runs are
deterministic and don't re-incur GPU inference cost.

This module is registered as ``sentiment:finbert:1.0.0``.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)
from quant_foundry.modules.sentiment.language import detect_language
from quant_foundry.modules.sentiment.naive_wordlist import (
    NaiveWordlistMultilingualSentiment,
)

#: Default FinBERT model from HuggingFace.
DEFAULT_MODEL = "yiyanghkust/finbert-tone"

#: Sentiment label → score mapping (FinBERT outputs these labels).
_LABEL_TO_SCORE: dict[str, float] = {
    "positive": 1.0,
    "negative": -1.0,
    "neutral": 0.0,
}


@register_module(
    "sentiment",
    "finbert",
    "1.0.0",
    default_config={
        "model": DEFAULT_MODEL,
        "batch_size": 32,
        "device": "auto",  # "auto", "cuda", "cpu"
        "cache_dir": None,  # set to a path to enable disk caching
        "language": "auto",  # "auto", "en", or an ISO 639-1 code
    },
)
class FinBERTSentiment:
    """FinBERT sentiment engine for financial news text.

    Loads the ``yiyanghkust/finbert-tone`` model on first use (lazy).
    Scores each :class:`MediaItem` headline + body for sentiment in
    ``[-1, 1]``.  Results are cached by ``item_id`` when ``cache_dir``
    is set.

    Requires ``transformers`` and ``torch`` to be installed (available
    on RunPod GPU workers).  Raises :class:`ImportError` at score time
    if they're missing.

    FinBERT is English-only.  When ``language="auto"`` (default) and a
    media item is detected as non-English, the engine gracefully
    degrades to the multilingual naive wordlist scorer for that item
    (with the appropriate language's word list).  A warning is printed
    when falling back.  Set ``language="en"`` to force FinBERT for all
    text (the original behavior).
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.model_name: str = self.config.get("model", DEFAULT_MODEL)
        self.batch_size: int = self.config.get("batch_size", 32)
        self.device: str = self.config.get("device", "auto")
        self.language: str = self.config.get("language", "auto")
        self.cache_dir: pathlib.Path | None = (
            pathlib.Path(self.config["cache_dir"]) if self.config.get("cache_dir") else None
        )
        self._model = None  # lazy-loaded
        self._tokenizer = None  # lazy-loaded
        self._cache: dict[str, dict[str, float]] = {}
        self._cache_loaded = False
        # Lazy fallback scorer for non-English text.
        self._fallback: NaiveWordlistMultilingualSentiment | None = None

    def _load_cache(self) -> None:
        """Load the disk cache if cache_dir is set and not yet loaded."""
        if self.cache_dir is None or self._cache_loaded:
            return
        cache_file = self.cache_dir / "finbert_sentiment_cache.json"
        if cache_file.exists():
            try:
                self._cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._cache = {}
        self._cache_loaded = True

    def _save_cache(self) -> None:
        """Save the disk cache if cache_dir is set."""
        if self.cache_dir is None:
            return
        cache_file = self.cache_dir / "finbert_sentiment_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(self._cache, sort_keys=True),
            encoding="utf-8",
        )

    def _load_model(self) -> None:
        """Lazy-load the FinBERT model + tokenizer."""
        if self._model is not None:
            return

        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise ImportError(
                "FinBERTSentiment requires `transformers` and `torch`. "
                "Install them with: pip install transformers torch "
                "(or use a RunPod GPU worker which has them pre-installed)."
            ) from exc

        self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(self.model_name)

        # Device selection
        if self.device == "auto":
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self._device = self.device

        assert self._model is not None  # loaded above
        self._model.to(self._device)
        self._model.eval()

    def _get_fallback(self) -> NaiveWordlistMultilingualSentiment:
        """Lazy-load the multilingual wordlist fallback scorer."""
        if self._fallback is None:
            self._fallback = NaiveWordlistMultilingualSentiment()
        return self._fallback

    def _is_english(self, item: MediaItem) -> bool:
        """Determine whether an item should be scored by FinBERT.

        When ``language="en"`` FinBERT handles everything (original
        behavior).  When ``language="auto"`` we detect the text's
        language and only use FinBERT for English.
        """
        if self.language == "en":
            return True
        if self.language == "auto":
            return detect_language(item.text) == "en"
        # A specific non-English code was forced → never use FinBERT.
        return False

    def score(self, items: list[MediaItem]) -> list[SentimentResult]:
        """Score media items with FinBERT.

        Returns one :class:`SentimentResult` per item.  Cached items
        are returned from cache without re-running inference.

        Non-English items (when ``language="auto"``) are routed to the
        multilingual naive wordlist fallback scorer instead of FinBERT,
        which is English-only.  A warning is printed on first fallback.
        """
        self._load_cache()

        # Separate items by route: FinBERT (English) vs wordlist fallback.
        results: list[SentimentResult | None] = [None] * len(items)
        to_score: list[tuple[int, MediaItem]] = []
        fallback_items: list[tuple[int, MediaItem]] = []

        for i, item in enumerate(items):
            if item.item_id in self._cache:
                cached = self._cache[item.item_id]
                results[i] = SentimentResult(
                    item_id=item.item_id,
                    provider="finbert",
                    score=cached["score"],
                    confidence=cached["confidence"],
                )
                continue
            if self._is_english(item):
                to_score.append((i, item))
            else:
                fallback_items.append((i, item))

        # --- Wordlist fallback for non-English items ---------------------- #
        if fallback_items:
            print(
                f"[finbert] {len(fallback_items)} non-English item(s) "
                "detected — falling back to multilingual naive wordlist "
                "(FinBERT is English-only).",
                flush=True,
            )
            scorer = self._get_fallback()
            fb_results = scorer.score([item for _, item in fallback_items])
            for (orig_idx, item), fb_result in zip(
                fallback_items,
                fb_results,
                strict=True,
            ):
                # Re-tag the provider so callers know FinBERT routed it.
                results[orig_idx] = SentimentResult(
                    item_id=item.item_id,
                    provider="finbert",
                    score=fb_result.score,
                    confidence=fb_result.confidence,
                    metadata={**fb_result.metadata, "fallback": "naive-wordlist-ml"},
                )

        if not to_score:
            return results  # type: ignore[return-value]

        # --- FinBERT inference for English items -------------------------- #
        self._load_model()

        import torch

        assert self._model is not None  # loaded by _load_model
        assert self._tokenizer is not None  # loaded by _load_model

        new_results: list[SentimentResult] = []
        for batch_start in range(0, len(to_score), self.batch_size):
            batch = to_score[batch_start : batch_start + self.batch_size]
            texts = [item.text[:512] for _, item in batch]  # truncate to 512 tokens

            inputs = self._tokenizer(
                texts,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            inputs = {k: v.to(self._device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = self._model(**inputs)

            probs = torch.softmax(outputs.logits, dim=-1)
            labels = self._model.config.id2label

            for (orig_idx, item), prob in zip(batch, probs, strict=True):
                # Find the label with highest probability
                top_idx = int(prob.argmax().item())
                label = labels[top_idx].lower()
                confidence = float(prob[top_idx].item())
                score = _LABEL_TO_SCORE.get(label, 0.0)

                result = SentimentResult(
                    item_id=item.item_id,
                    provider="finbert",
                    score=round(score, 6),
                    confidence=round(confidence, 6),
                )
                results[orig_idx] = result
                new_results.append(result)

                # Cache the result
                self._cache[item.item_id] = {
                    "score": result.score,
                    "confidence": result.confidence,
                }

        self._save_cache()
        return results


__all__ = ["DEFAULT_MODEL", "FinBERTSentiment"]
