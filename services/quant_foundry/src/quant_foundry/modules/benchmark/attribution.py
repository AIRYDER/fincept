"""
quant_foundry.modules.benchmark.attribution — feature importance attribution report.

The :class:`AttributionReport` analyzes a trained model's feature
importance to answer the core research questions:

1. **Which event types have the most price impact?**
   Groups features by event type prefix (``sent_regulatory``,
   ``sent_earnings``, etc.) and sums their importance.

2. **Which source (news vs social) has higher signal?**
   Groups by source category — news sources produce event-typed items
   (``regulatory``, ``earnings``, etc.) while social sources produce
   ``social`` event types.  Compares aggregate importance.

3. **Which sentiment engine is better?**
   When multiple sentiment engines are used (e.g. FinBERT for news,
   LLM ensemble for social), the attribution report compares their
   per-provider feature importance (``sent_openai``, ``sent_anthropic``,
   etc. from the LLM ensemble's per-provider features).

4. **How did media→price response change from 2018 to 2025?**
   Groups by year one-hot features (``year_2018``, ``year_2019``, etc.)
   and shows how feature importance shifts across years.

5. **Which horizon is most predictable?**
   Groups by abnormal-return horizon columns (``ar_1d``, ``ar_5d``,
   ``ar_21d``, ``ar_63d``) — these are the multi-horizon labels, and
   their correlation with features reveals which horizon has the
   strongest signal.

Usage::

    report = AttributionReport.from_model(
        model=model,  # trained LightGBM model
        feature_names=feature_names,  # list of feature names
        dataset_path=parquet_path,  # path to the dataset parquet
    )
    report.write(output_path)
    print(report.event_type_attribution())
    print(report.year_attribution())
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

from quant_foundry.modules.features.per_event_type import EVENT_TYPES


@dataclass
class AttributionReport:
    """Feature importance attribution report.

    Groups a trained model's feature importance by:
    - Event type (regulatory, earnings, macro, etc.)
    - Source category (news vs social)
    - Sentiment provider (naive, finbert, openai, anthropic, xai, minimax)
    - Year (2018–2025)
    - Horizon (1d, 5d, 21d, 63d)

    The report is JSON-serializable for persistence and comparison
    across benchmark runs.
    """

    feature_names: list[str]
    feature_importances: list[float]
    event_type_attribution_data: dict[str, float] = field(default_factory=dict)
    source_attribution_data: dict[str, float] = field(default_factory=dict)
    sentiment_provider_attribution_data: dict[str, float] = field(default_factory=dict)
    year_attribution_data: dict[str, float] = field(default_factory=dict)
    horizon_attribution_data: dict[str, float] = field(default_factory=dict)
    top_features: list[dict[str, Any]] = field(default_factory=list)
    total_importance: float = 0.0

    @classmethod
    def from_model(
        cls,
        *,
        feature_importances: list[float],
        feature_names: list[str],
    ) -> AttributionReport:
        """Build an attribution report from a trained model.

        Args:
            feature_importances: Feature importance array from the
                trained model (e.g. ``model.feature_importances_`` from
                LightGBM).
            feature_names: Ordered list of feature names matching the
                importance array.
        """
        if len(feature_importances) != len(feature_names):
            raise ValueError(
                f"length mismatch: {len(feature_importances)} importances "
                f"vs {len(feature_names)} feature names",
            )

        report = cls(
            feature_names=list(feature_names),
            feature_importances=list(feature_importances),
        )
        report._compute_attributions()
        return report

    @classmethod
    def from_parquet(
        cls,
        *,
        parquet_path: pathlib.Path,
        model: Any,
    ) -> AttributionReport:
        """Build an attribution report from a trained model + dataset parquet.

        Reads the feature names from the parquet columns and the feature
        importances from the model.

        Args:
            parquet_path: Path to the dataset parquet file.
            model: Trained LightGBM model with ``feature_importances_``.
        """
        import polars as pl

        df = pl.read_parquet(str(parquet_path))
        # Feature columns = all columns except decision_time, symbol, label
        feature_names = [
            c for c in df.columns
            if c not in ("decision_time", "symbol", "label")
        ]

        importances = list(model.feature_importances_)
        # Ensure lengths match (LightGBM may have different ordering)
        if len(importances) != len(feature_names):
            # Try to match by model's feature_name() if available
            try:
                model_feature_names = model.feature_name()
                if len(model_feature_names) == len(importances):
                    # Reorder importances to match parquet column order
                    idx_map = {
                        name: i for i, name in enumerate(model_feature_names)
                    }
                    importances = [
                        importances[idx_map[name]] if name in idx_map else 0.0
                        for name in feature_names
                    ]
            except (AttributeError, KeyError):
                pass

        return cls.from_model(
            feature_importances=importances,
            feature_names=feature_names,
        )

    def _compute_attributions(self) -> None:
        """Compute all attribution groupings."""
        # Build name → importance lookup
        importance_map = dict(zip(self.feature_names, self.feature_importances, strict=True))
        self.total_importance = sum(self.feature_importances)

        # --- Event type attribution -------------------------------------
        for et in EVENT_TYPES:
            prefix = f"sent_{et}"
            self.event_type_attribution_data[et] = importance_map.get(prefix, 0.0)
        # Also include "social" event type (from social source adapters)
        self.event_type_attribution_data["social"] = importance_map.get("sent_social", 0.0)

        # --- Source attribution (news event types vs social) ------------
        news_types = [et for et in EVENT_TYPES if et != "general"]
        social_types = ["social"]
        self.source_attribution_data["news"] = sum(
            self.event_type_attribution_data.get(et, 0.0) for et in news_types
        )
        self.source_attribution_data["social"] = sum(
            self.event_type_attribution_data.get(et, 0.0) for et in social_types
        )

        # --- Sentiment provider attribution -----------------------------
        # Per-provider features from the LLM ensemble (if present)
        for provider in ("naive", "finbert", "openai", "anthropic", "xai", "minimax", "llm_ensemble"):
            # Check for per-provider feature columns
            provider_features = [
                imp for name, imp in importance_map.items()
                if name.startswith(f"sent_{provider}") or f"_{provider}_" in name
            ]
            self.sentiment_provider_attribution_data[provider] = sum(provider_features)

        # Also check for the aggregate sentiment features
        self.sentiment_provider_attribution_data["sent_mean"] = importance_map.get("sent_mean", 0.0)
        self.sentiment_provider_attribution_data["sent_count"] = importance_map.get("sent_count", 0.0)

        # --- Year attribution -------------------------------------------
        for year in range(2018, 2026):
            self.year_attribution_data[str(year)] = importance_map.get(f"year_{year}", 0.0)

        # --- Horizon attribution ----------------------------------------
        for h in (1, 5, 21, 63):
            self.horizon_attribution_data[f"ar_{h}d"] = importance_map.get(f"ar_{h}d", 0.0)

        # --- Top features ------------------------------------------------
        sorted_features = sorted(
            zip(self.feature_names, self.feature_importances, strict=True),
            key=lambda x: x[1],
            reverse=True,
        )
        self.top_features = [
            {"feature": name, "importance": float(imp)}
            for name, imp in sorted_features[:20]
        ]

    # --- Public accessors --------------------------------------------------

    def event_type_attribution(self) -> dict[str, float]:
        """Return event type → importance mapping, sorted by importance."""
        return dict(sorted(
            self.event_type_attribution_data.items(),
            key=lambda x: x[1],
            reverse=True,
        ))

    def source_attribution(self) -> dict[str, float]:
        """Return source category → importance mapping."""
        return dict(self.source_attribution_data)

    def sentiment_provider_attribution(self) -> dict[str, float]:
        """Return sentiment provider → importance mapping, sorted by importance."""
        return dict(sorted(
            self.sentiment_provider_attribution_data.items(),
            key=lambda x: x[1],
            reverse=True,
        ))

    def year_attribution(self) -> dict[str, float]:
        """Return year → importance mapping (chronological order)."""
        return dict(sorted(
            self.year_attribution_data.items(),
            key=lambda x: x[0],
        ))

    def horizon_attribution(self) -> dict[str, float]:
        """Return horizon → importance mapping (chronological order)."""
        return dict(self.horizon_attribution_data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "total_importance": float(self.total_importance),
            "event_type": self.event_type_attribution(),
            "source": self.source_attribution(),
            "sentiment_provider": self.sentiment_provider_attribution(),
            "year": self.year_attribution(),
            "horizon": self.horizon_attribution(),
            "top_features": self.top_features,
        }

    def write(self, path: pathlib.Path) -> pathlib.Path:
        """Write the attribution report to a JSON file.

        Returns the path to the written file.
        """
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def summary_text(self) -> str:
        """Return a human-readable summary of the attribution report."""
        lines = ["=" * 60, "ATTRIBUTION REPORT", "=" * 60, ""]

        lines.append("Event Type Attribution (importance):")
        for et, imp in self.event_type_attribution().items():
            if imp > 0:
                lines.append(f"  {et:20s} {imp:>10.4f}")
        lines.append("")

        lines.append("Source Attribution:")
        for src, imp in self.source_attribution().items():
            lines.append(f"  {src:20s} {imp:>10.4f}")
        lines.append("")

        lines.append("Sentiment Provider Attribution:")
        for prov, imp in self.sentiment_provider_attribution().items():
            if imp > 0:
                lines.append(f"  {prov:20s} {imp:>10.4f}")
        lines.append("")

        lines.append("Year Attribution:")
        for year, imp in self.year_attribution().items():
            if imp > 0:
                lines.append(f"  {year:20s} {imp:>10.4f}")
        lines.append("")

        lines.append("Horizon Attribution:")
        for h, imp in self.horizon_attribution().items():
            lines.append(f"  {h:20s} {imp:>10.4f}")
        lines.append("")

        lines.append("Top 10 Features:")
        for f in self.top_features[:10]:
            lines.append(f"  {f['feature']:30s} {f['importance']:>10.4f}")
        lines.append("")

        return "\n".join(lines)


__all__ = ["AttributionReport"]
