"""
quant_foundry.modules.sentiment.llm_ensemble — 4-LLM ensemble sentiment engine.

Aggregates sentiment scores from OpenAI, Anthropic, xAI, and MiniMax into
a single ensemble score.  Each social post is scored by all four providers
(or a configurable subset), and the ensemble records:

- ``score`` = mean of per-provider scores (the ensemble aggregate)
- ``confidence`` = mean of per-provider confidences, scaled by agreement

The **disagreement** between providers is itself a signal — high
disagreement correlates with ambiguous/mixed-reaction posts, which is
informative for price impact.  The ensemble exposes per-provider scores
via :meth:`score_detailed` so feature modules can record them as
separate features (``sent_openai``, ``sent_anthropic``, ``sent_xai``,
``sent_minimax``, ``sent_llm_std``).

This module is registered as ``sentiment:llm-ensemble-4:1.0.0``.
"""

from __future__ import annotations

import statistics
from typing import Any

from quant_foundry.modules.registry import (
    MediaItem,
    ModuleInfo,
    SentimentResult,
    register_module,
)


@register_module(
    "sentiment",
    "llm-ensemble-4",
    "1.0.0",
    default_config={
        "providers": ["openai", "anthropic", "xai", "minimax"],
        "aggregation": "mean",  # "mean" or "median"
        "min_providers": 2,  # need at least this many non-zero-confidence results
    },
)
class LLMEnsemble4Sentiment:
    """4-LLM ensemble sentiment engine.

    Delegates scoring to each of the 4 LLM providers, then aggregates.
    If a provider's API key is missing, that provider is silently
    skipped (graceful degradation).  If fewer than ``min_providers``
    return valid results, the ensemble returns neutral (0.0) with
    zero confidence.

    The ensemble is designed for the social-text arm of the FinBERT +
    LLM hybrid — use FinBERT for news, this ensemble for social media.
    """

    info: ModuleInfo

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.provider_names: list[str] = self.config.get(
            "providers", ["openai", "anthropic", "xai", "minimax"],
        )
        self.aggregation: str = self.config.get("aggregation", "mean")
        self.min_providers: int = self.config.get("min_providers", 2)
        self._providers: dict[str, Any] = {}
        self._providers_loaded = False

    def _load_providers(self) -> None:
        """Lazy-load the individual LLM provider modules."""
        if self._providers_loaded:
            return

        from quant_foundry.modules import ModuleRegistry, load_all_modules

        load_all_modules()
        registry = ModuleRegistry.instance()

        provider_map = {
            "openai": "sentiment:llm-openai:1.0.0",
            "anthropic": "sentiment:llm-anthropic:1.0.0",
            "xai": "sentiment:llm-xai:1.0.0",
            "minimax": "sentiment:llm-minimax:1.0.0",
        }

        for name in self.provider_names:
            full_id = provider_map.get(name)
            if full_id is None:
                continue
            try:
                self._providers[name] = registry.create(full_id)
            except (KeyError, ValueError):
                # Provider not registered — skip
                pass

        self._providers_loaded = True

    def score(self, items: list[MediaItem]) -> list[SentimentResult]:
        """Score media items with the 4-LLM ensemble.

        Returns one :class:`SentimentResult` per item with
        ``provider="llm-ensemble"``.  Per-provider scores are available
        via :meth:`score_detailed`.
        """
        detailed = self.score_detailed(items)

        results: list[SentimentResult] = []
        for item_details in detailed:
            scores = [
                p["score"] for p in item_details["providers"]
                if p["confidence"] > 0.0
            ]
            confidences = [
                p["confidence"] for p in item_details["providers"]
                if p["confidence"] > 0.0
            ]

            if len(scores) < self.min_providers:
                results.append(SentimentResult(
                    item_id=item_details["item_id"],
                    provider="llm-ensemble",
                    score=0.0,
                    confidence=0.0,
                ))
                continue

            if self.aggregation == "median":
                agg_score = statistics.median(scores)
            else:
                agg_score = statistics.mean(scores)

            # Confidence: mean of provider confidences, scaled by agreement
            # (lower std → higher confidence)
            mean_conf = statistics.mean(confidences)
            if len(scores) > 1:
                score_std = statistics.stdev(scores) if len(scores) > 1 else 0.0
                agreement = max(0.0, 1.0 - score_std)  # std in [-1,1] range
            else:
                agreement = 1.0
            ensemble_conf = mean_conf * agreement

            results.append(SentimentResult(
                item_id=item_details["item_id"],
                provider="llm-ensemble",
                score=round(agg_score, 6),
                confidence=round(min(1.0, ensemble_conf), 6),
            ))

        return results

    def score_detailed(
        self,
        items: list[MediaItem],
    ) -> list[dict[str, Any]]:
        """Score items and return per-provider details.

        Returns a list of dicts, one per item:
        ::
            {
                "item_id": "...",
                "providers": [
                    {"provider": "openai", "score": 0.8, "confidence": 0.9},
                    {"provider": "anthropic", "score": 0.6, "confidence": 0.8},
                    ...
                ],
                "ensemble_score": 0.7,
                "ensemble_std": 0.1,
            }
        """
        self._load_providers()

        if not self._providers:
            # No providers available — return neutral for all
            return [
                {
                    "item_id": item.item_id,
                    "providers": [],
                    "ensemble_score": 0.0,
                    "ensemble_std": 0.0,
                }
                for item in items
            ]

        # Score with each provider
        per_provider_results: dict[str, list[SentimentResult]] = {}
        for name, provider in self._providers.items():
            try:
                per_provider_results[name] = provider.score(items)
            except (ValueError, Exception):
                # Provider failed (e.g. missing API key) — skip
                per_provider_results[name] = [
                    SentimentResult(
                        item_id=item.item_id,
                        provider=name,
                        score=0.0,
                        confidence=0.0,
                    )
                    for item in items
                ]

        # Aggregate per-item
        detailed: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            provider_details: list[dict[str, float]] = []
            scores: list[float] = []

            for name in self._providers:
                if name not in per_provider_results:
                    continue
                result = per_provider_results[name][i]
                provider_details.append({
                    "provider": name,
                    "score": result.score,
                    "confidence": result.confidence,
                })
                if result.confidence > 0.0:
                    scores.append(result.score)

            ensemble_score = statistics.mean(scores) if scores else 0.0
            ensemble_std = (
                statistics.stdev(scores) if len(scores) > 1 else 0.0
            )

            detailed.append({
                "item_id": item.item_id,
                "providers": provider_details,
                "ensemble_score": round(ensemble_score, 6),
                "ensemble_std": round(ensemble_std, 6),
            })

        return detailed


__all__ = ["LLMEnsemble4Sentiment"]
