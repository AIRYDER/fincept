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

import hashlib
import json
import pathlib
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
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
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.model_name: str = self.config.get("model", DEFAULT_MODEL)
        self.batch_size: int = self.config.get("batch_size", 32)
        self.device: str = self.config.get("device", "auto")
        self.cache_dir: pathlib.Path | None = (
            pathlib.Path(self.config["cache_dir"])
            if self.config.get("cache_dir")
            else None
        )
        self._model = None  # lazy-loaded
        self._tokenizer = None  # lazy-loaded
        self._cache: dict[str, dict[str, float]] = {}
        self._cache_loaded = False

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
            json.dumps(self._cache, sort_keys=True), encoding="utf-8",
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
        self._model.to(self._device)
        self._model.eval()

    def score(self, items: list[MediaItem]) -> list[SentimentResult]:
        """Score media items with FinBERT.

        Returns one :class:`SentimentResult` per item.  Cached items
        are returned from cache without re-running inference.
        """
        self._load_cache()

        # Separate cached vs uncached items
        results: list[SentimentResult | None] = [None] * len(items)
        to_score: list[tuple[int, MediaItem]] = []

        for i, item in enumerate(items):
            if item.item_id in self._cache:
                cached = self._cache[item.item_id]
                results[i] = SentimentResult(
                    item_id=item.item_id,
                    provider="finbert",
                    score=cached["score"],
                    confidence=cached["confidence"],
                )
            else:
                to_score.append((i, item))

        if not to_score:
            return results  # type: ignore[return-value]

        # Load model and run inference
        self._load_model()

        import torch

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
        return results  # type: ignore[return-value]


__all__ = ["FinBERTSentiment", "DEFAULT_MODEL"]
