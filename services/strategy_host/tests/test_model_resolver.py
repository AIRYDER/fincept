"""
Tests for ``strategy_host.model_resolver``.

The resolver translates ``model_binding`` -> on-disk model path via
a JSON pointer file under ``models/active/``.  These tests pin the
exact failure modes (missing / corrupt / invalid pointer) and the
happy path so a refactor can't quietly break operator promotion
flows.

We use ``ACTIVE_MODELS_DIR`` + ``MODELS_DIR`` env overrides to write
into ``tmp_path`` instead of polluting the repo's real models tree.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from strategy_host.model_resolver import (
    pointer_path,
    resolve_active_model_dir,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def models_tree(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Set up an isolated ``models/`` tree under ``tmp_path``.

    Yields the models root.  Caller writes pointer files into
    ``models_tree / "active" / "<binding>.json"`` and model dirs
    into ``models_tree / "<name>"``.
    """
    models = tmp_path / "models"
    active = models / "active"
    active.mkdir(parents=True)
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("ACTIVE_MODELS_DIR", str(active))
    return models


def _write_pointer(active: pathlib.Path, binding: str, body: object) -> pathlib.Path:
    """Write a pointer file with arbitrary content (used for malformed cases)."""
    p = active / f"{binding}.json"
    if isinstance(body, (dict, list)):
        p.write_text(json.dumps(body))
    else:
        p.write_text(str(body))
    return p


# --------------------------------------------------------------------------- #
# Pointer path                                                                #
# --------------------------------------------------------------------------- #


class TestPointerPath:
    def test_uses_active_dir_override(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        active = tmp_path / "custom_active"
        monkeypatch.setenv("ACTIVE_MODELS_DIR", str(active))
        assert pointer_path("gbm_v1") == active / "gbm_v1.json"

    def test_falls_back_to_models_root_active(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only MODELS_DIR set; ACTIVE_MODELS_DIR unset -> derives.
        models = tmp_path / "m"
        monkeypatch.setenv("MODELS_DIR", str(models))
        monkeypatch.delenv("ACTIVE_MODELS_DIR", raising=False)
        assert pointer_path("foo") == models / "active" / "foo.json"


# --------------------------------------------------------------------------- #
# Resolve: happy path                                                         #
# --------------------------------------------------------------------------- #


class TestResolveHappy:
    def test_resolves_to_models_root_subdir(self, models_tree: pathlib.Path) -> None:
        active = models_tree / "active"
        _write_pointer(active, "gbm_predictor", {"model_name": "gbm_2026_04_15"})
        # Note: the resolver is purely lexical -- it does NOT
        # require the resolved directory to actually exist.  That
        # check is the strategy's responsibility at on_start.
        resolved = resolve_active_model_dir("gbm_predictor")
        assert resolved == models_tree / "gbm_2026_04_15"


# --------------------------------------------------------------------------- #
# Resolve: failure modes                                                      #
# --------------------------------------------------------------------------- #


class TestResolveFailures:
    def test_missing_pointer_returns_none(self, models_tree: pathlib.Path) -> None:
        # Different from the agent which falls back to env / default;
        # the host is conservative and returns None.
        assert resolve_active_model_dir("not_promoted") is None

    def test_invalid_json_returns_none(self, models_tree: pathlib.Path) -> None:
        active = models_tree / "active"
        (active / "broken.json").write_text("not-json{{{")
        assert resolve_active_model_dir("broken") is None

    def test_missing_model_name_key_returns_none(self, models_tree: pathlib.Path) -> None:
        active = models_tree / "active"
        _write_pointer(active, "no_name", {"some_other_key": "value"})
        assert resolve_active_model_dir("no_name") is None

    def test_empty_model_name_returns_none(self, models_tree: pathlib.Path) -> None:
        active = models_tree / "active"
        _write_pointer(active, "empty", {"model_name": ""})
        assert resolve_active_model_dir("empty") is None

    def test_non_string_model_name_returns_none(self, models_tree: pathlib.Path) -> None:
        # Defensive: someone could write {"model_name": 42} from a
        # buggy promotion script.  The resolver rejects anything
        # that isn't a non-empty string.
        active = models_tree / "active"
        _write_pointer(active, "numeric", {"model_name": 42})
        assert resolve_active_model_dir("numeric") is None

    def test_top_level_array_returns_none(self, models_tree: pathlib.Path) -> None:
        # ``json.loads`` succeeds; ``data.get("model_name")`` raises
        # AttributeError on a list.  The resolver guards isinstance.
        active = models_tree / "active"
        _write_pointer(active, "array", ["model_name", "x"])
        assert resolve_active_model_dir("array") is None
