"""Pure-Python tests for ``api.feature_importance``.

Pin the parsing contract:

* ``split_feature=`` lines accumulate counts across trees.
* ``feature_importance.json`` sidecar (when the trainer writes it) wins
  over the text parser and surfaces gain values.
* Features that never split out get a 0 count, not a missing entry —
  the UI relies on the full list for stable rendering.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from api.feature_importance import (
    compute_feature_importance,
    parse_split_counts,
)

# --------------------------------------------------------------------------- #
# parse_split_counts                                                          #
# --------------------------------------------------------------------------- #


class TestParseSplitCounts:
    def test_empty_string_returns_empty_dict(self) -> None:
        assert parse_split_counts("") == {}

    def test_no_split_feature_lines_returns_empty(self) -> None:
        text = "tree=0\nnum_leaves=3\nfeature_names=a b c\n"
        assert parse_split_counts(text) == {}

    def test_single_tree_counts_each_index_once(self) -> None:
        text = "split_feature=0 1 2 1 0\n"
        # idx 0 used twice, idx 1 used twice, idx 2 used once.
        assert parse_split_counts(text) == {0: 2, 1: 2, 2: 1}

    def test_multiple_trees_accumulate(self) -> None:
        text = (
            "split_feature=0 1 2\n"
            "split_feature=2 2 1\n"
            "split_feature=0\n"
        )
        # idx 0: 1+0+1 = 2; idx 1: 1+1+0 = 2; idx 2: 1+2+0 = 3.
        assert parse_split_counts(text) == {0: 2, 1: 2, 2: 3}

    def test_negative_indices_are_skipped(self) -> None:
        """LightGBM uses -1 to mark leaf-only positions; ignore them."""
        text = "split_feature=-1 0 -1 1 -1\n"
        assert parse_split_counts(text) == {0: 1, 1: 1}

    def test_non_integer_tokens_are_skipped(self) -> None:
        text = "split_feature=0 abc 1 2.5 3\n"
        assert parse_split_counts(text) == {0: 1, 1: 1, 3: 1}

    def test_other_lines_dont_pollute(self) -> None:
        text = (
            "tree=0\n"
            "split_feature=0 1\n"
            "feature_infos=[0:1] [0:1]\n"
            "split_feature=1 1\n"
            "version=v3\n"
        )
        assert parse_split_counts(text) == {0: 1, 1: 3}


# --------------------------------------------------------------------------- #
# compute_feature_importance — text-parser fallback                           #
# --------------------------------------------------------------------------- #


def _write_text_model(model_dir: pathlib.Path, *, splits: list[list[int]]) -> None:
    """Write a minimal ``model.txt`` containing only ``split_feature=`` lines."""
    model_dir.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        "split_feature=" + " ".join(str(i) for i in line) for line in splits
    )
    (model_dir / "model.txt").write_text(body + "\n")


class TestComputeFeatureImportanceTextFallback:
    def test_returns_one_row_per_feature_in_features_list(
        self, tmp_path: pathlib.Path
    ) -> None:
        features = ["a", "b", "c"]
        _write_text_model(tmp_path, splits=[[0, 1, 2, 0, 1], [1, 2]])
        # idx 0 -> 2, idx 1 -> 3, idx 2 -> 2.
        result = compute_feature_importance(tmp_path, features=features)
        assert result["importance_type"] == "split_count"
        assert result["source"] == "model_text"
        assert result["warnings"] == []
        rows = result["importances"]
        assert len(rows) == 3
        # Sorted by split_count desc.
        assert rows[0]["feature"] == "b"
        assert rows[0]["split_count"] == 3
        assert rows[0]["rank"] == 1
        # Tie between "a" and "c" at count=2; tie-broken by feature name asc.
        assert (rows[1]["feature"], rows[1]["split_count"]) == ("a", 2)
        assert (rows[2]["feature"], rows[2]["split_count"]) == ("c", 2)
        # Gain is None until a sidecar is written.
        assert all(r["gain"] is None for r in rows)

    def test_unused_feature_still_returned_with_zero_count(
        self, tmp_path: pathlib.Path
    ) -> None:
        features = ["a", "b", "c"]
        # Only idx 0 used.
        _write_text_model(tmp_path, splits=[[0, 0, 0]])
        result = compute_feature_importance(tmp_path, features=features)
        rows_by_feature = {r["feature"]: r for r in result["importances"]}
        assert rows_by_feature["a"]["split_count"] == 3
        assert rows_by_feature["b"]["split_count"] == 0
        assert rows_by_feature["c"]["split_count"] == 0

    def test_missing_model_txt_warns_and_returns_zero_rows(
        self, tmp_path: pathlib.Path
    ) -> None:
        features = ["a", "b"]
        # No model.txt written.
        result = compute_feature_importance(tmp_path, features=features)
        assert any("model.txt missing" in w for w in result["warnings"])
        rows = result["importances"]
        assert len(rows) == 2
        assert all(r["split_count"] == 0 for r in rows)
        # Rank still assigned for stability of the UI.
        assert {r["rank"] for r in rows} == {1, 2}

    def test_empty_model_txt_warns_about_no_splits(
        self, tmp_path: pathlib.Path
    ) -> None:
        features = ["a", "b"]
        (tmp_path / "model.txt").write_text("tree=0\nfeature_names=a b\n")
        result = compute_feature_importance(tmp_path, features=features)
        assert any("no split_feature lines" in w for w in result["warnings"])

    def test_more_indices_than_features_does_not_crash(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Defensive: an out-of-range idx (e.g. mismatched features list) is
        silently ignored; we just under-report counts for that idx."""
        features = ["a", "b"]  # only 2 features...
        _write_text_model(tmp_path, splits=[[0, 1, 2, 3]])  # ...but 4 indices used
        result = compute_feature_importance(tmp_path, features=features)
        assert len(result["importances"]) == 2  # still one per declared feature


# --------------------------------------------------------------------------- #
# compute_feature_importance — sidecar (preferred)                            #
# --------------------------------------------------------------------------- #


def _write_sidecar(model_dir: pathlib.Path, payload: dict[str, Any]) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "feature_importance.json").write_text(json.dumps(payload))


class TestComputeFeatureImportanceSidecar:
    def test_sidecar_with_gain_takes_precedence(
        self, tmp_path: pathlib.Path
    ) -> None:
        features = ["a", "b", "c"]
        _write_text_model(tmp_path, splits=[[0, 0, 0]])  # would say a=3 only
        _write_sidecar(
            tmp_path,
            {
                "gain": {"a": 1.0, "b": 5.0, "c": 2.0},
                "split": {"a": 10, "b": 50, "c": 20},
            },
        )
        result = compute_feature_importance(tmp_path, features=features)
        assert result["source"] == "sidecar"
        assert result["importance_type"] == "gain_and_split"
        # Sorted by gain desc (b > c > a).
        names = [r["feature"] for r in result["importances"]]
        assert names == ["b", "c", "a"]
        assert result["importances"][0]["gain"] == pytest.approx(5.0)
        assert result["importances"][0]["split_count"] == 50

    def test_sidecar_without_gain_falls_back_to_split_sort(
        self, tmp_path: pathlib.Path
    ) -> None:
        features = ["a", "b"]
        _write_sidecar(tmp_path, {"split": {"a": 1, "b": 3}})
        result = compute_feature_importance(tmp_path, features=features)
        assert result["importance_type"] == "split_count"
        assert result["source"] == "sidecar"
        names = [r["feature"] for r in result["importances"]]
        assert names == ["b", "a"]

    def test_malformed_sidecar_falls_back_to_text(
        self, tmp_path: pathlib.Path
    ) -> None:
        features = ["a", "b"]
        _write_text_model(tmp_path, splits=[[1, 1, 0]])
        (tmp_path / "feature_importance.json").write_text("{not valid json")
        result = compute_feature_importance(tmp_path, features=features)
        assert result["source"] == "model_text"
        # Sanity: we got the text-parser counts.
        rows_by_feature = {r["feature"]: r["split_count"] for r in result["importances"]}
        assert rows_by_feature == {"a": 1, "b": 2}
