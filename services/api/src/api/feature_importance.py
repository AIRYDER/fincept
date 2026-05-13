"""
api.feature_importance - LightGBM model.txt parser.

We deliberately avoid importing :mod:`lightgbm` in the api service:

* The api wheel is meant to stay light (no native binaries).  Pulling
  lightgbm in for one read-only endpoint would bloat container images
  and add a startup failure mode (wheel-or-die on import).
* The model.txt format is text and stable enough for our use case.
  We need only the feature index used at each tree split, which has
  been emitted on ``split_feature=`` lines since LightGBM 2.x.

The trade-off: we can only compute *split-count* importance (how often
each feature is used as a split, summed across all trees).  Gain-based
importance is more informative but requires the booster's per-split
gain values which only the lightgbm runtime can fully decode.  When the
trainer adds a ``feature_importance.json`` sidecar (TODO: future
trainer change), this module will prefer that file and surface the
richer numbers.

Public API:

  ``compute_feature_importance(model_dir, features)`` -> list[dict]
      Each dict has ``feature``, ``split_count``, ``rank`` (1 = most
      important), and an optional ``gain`` field (None until the
      sidecar lands).  Sorted by ``split_count`` desc, then feature
      name asc for stability.

  ``parse_split_counts(model_text)`` -> dict[int, int]
      Lower-level helper: maps feature-index to total split count.
      Exposed for testing and tooling.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, cast

_SPLIT_FEATURE_PREFIX = "split_feature="


def parse_split_counts(model_text: str) -> dict[int, int]:
    """Count how often each feature index appears as a split feature.

    Iterates ``split_feature=...`` lines (one per tree).  Each line is a
    space-separated list of feature indices, one per non-leaf node.
    Returns ``{feature_index: count}``.

    Robustness:
      * Lines without a numeric token are skipped silently (they're
        either leaves-only trees or malformed sections from an unknown
        LightGBM version).
      * Negative or non-integer tokens (e.g. ``-1`` for "no split") are
        ignored — only valid feature indices contribute.
    """
    counts: dict[int, int] = {}
    for line in model_text.splitlines():
        if not line.startswith(_SPLIT_FEATURE_PREFIX):
            continue
        payload = line[len(_SPLIT_FEATURE_PREFIX):].strip()
        if not payload:
            continue
        for token in payload.split():
            try:
                idx = int(token)
            except ValueError:
                continue
            if idx < 0:
                continue
            counts[idx] = counts.get(idx, 0) + 1
    return counts


def _load_sidecar(model_dir: pathlib.Path) -> dict[str, Any] | None:
    """Read ``feature_importance.json`` if the trainer wrote one.

    Expected shape::

        {
          "gain":  {"feat_a": 12.3, "feat_b": 4.5, ...},
          "split": {"feat_a": 100,  "feat_b": 40,  ...}
        }

    Returns ``None`` (the caller falls back to text parsing) on any IO
    or parse error.  Bad sidecars are non-fatal.
    """
    path = model_dir / "feature_importance.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return cast(dict[str, Any], data)


def compute_feature_importance(
    model_dir: pathlib.Path, *, features: list[str]
) -> dict[str, Any]:
    """Compute per-feature importance from a trained model directory.

    ``features`` is the ordered list from ``meta.json["features"]`` —
    we use it to map the indices in ``model.txt`` (which carries
    placeholder names like ``Column_0``) to the real feature names the
    rest of the app cares about.

    Returns a dict::

        {
          "importances": [
            {"feature": "ret_5m", "split_count": 312, "gain": 1.42, "rank": 1},
            ...
          ],
          "importance_type": "split_count" | "gain_and_split",
          "source":          "sidecar"    | "model_text",
          "warnings":        [...]
        }

    Always returns the full feature list even when a feature was never
    used as a split (count = 0); that's a useful signal for the UI.
    """
    warnings: list[str] = []
    sidecar = _load_sidecar(model_dir)

    if sidecar is not None:
        raw_gain = sidecar.get("gain")
        raw_split = sidecar.get("split")
        gain_map = cast(dict[str, Any], raw_gain) if isinstance(raw_gain, dict) else {}
        split_map = (
            cast(dict[str, Any], raw_split) if isinstance(raw_split, dict) else {}
        )
        rows: list[dict[str, Any]] = []
        for feat in features:
            rows.append(
                {
                    "feature": feat,
                    "split_count": int(split_map.get(feat, 0)),
                    "gain": (
                        float(gain_map[feat]) if feat in gain_map else None
                    ),
                }
            )
        importance_type = (
            "gain_and_split" if any(r["gain"] is not None for r in rows) else "split_count"
        )
        sort_key = "gain" if importance_type == "gain_and_split" else "split_count"
        rows.sort(
            key=lambda r: (-(r[sort_key] or 0.0), r["feature"]),
        )
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return {
            "importances": rows,
            "importance_type": importance_type,
            "source": "sidecar",
            "warnings": warnings,
        }

    # Fallback: parse model.txt directly.
    model_path = model_dir / "model.txt"
    if not model_path.is_file():
        warnings.append("model.txt missing; cannot compute importance")
        return {
            "importances": [
                {"feature": f, "split_count": 0, "gain": None, "rank": i + 1}
                for i, f in enumerate(features)
            ],
            "importance_type": "split_count",
            "source": "model_text",
            "warnings": warnings,
        }
    try:
        text = model_path.read_text()
    except OSError as exc:
        warnings.append(f"model.txt read failed: {exc}")
        return {
            "importances": [
                {"feature": f, "split_count": 0, "gain": None, "rank": i + 1}
                for i, f in enumerate(features)
            ],
            "importance_type": "split_count",
            "source": "model_text",
            "warnings": warnings,
        }

    raw_counts = parse_split_counts(text)
    if not raw_counts:
        warnings.append(
            "no split_feature lines found in model.txt — model may be untrained "
            "or written by an unsupported LightGBM version"
        )

    rows = []
    for idx, feat in enumerate(features):
        rows.append(
            {
                "feature": feat,
                "split_count": int(raw_counts.get(idx, 0)),
                "gain": None,
            }
        )
    rows.sort(key=lambda r: (-r["split_count"], r["feature"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    return {
        "importances": rows,
        "importance_type": "split_count",
        "source": "model_text",
        "warnings": warnings,
    }
