"""
quant_foundry.artifact_io — typed loaders for trained model artifacts (T-7.1).

The :data:`LOADER_REGISTRY` maps the ``artifact_loader`` names referenced by
:class:`quant_foundry.alpha_genome.ModelFamilySpec` entries to concrete loader
callables. Each loader validates the path exists and is non-empty, then loads
the model object using the appropriate backend library.

Design:

- **Lazy imports.** The ML backends (``lightgbm``, ``catboost``,
  ``xgboost``) are imported *inside* the loader function, not at module
  level. This keeps ``quant_foundry`` importable in environments where one
  or more backends are not installed — a family spec can still be
  registered and validated, and the loader only fails if it is actually
  invoked without its backend present.
- **Path validation.** Every loader raises ``FileNotFoundError`` if the
  path does not exist and ``ValueError`` if the file is empty (a
  zero-byte model is never a real artifact).
- **Graceful backend-missing errors.** When a backend library is not
  installed, the loader raises ``ImportError`` with a helpful message
  naming the missing dependency and the loader that needs it.
- **``joblib`` / ``pickle`` fallback.** ``load_sklearn_pickle`` prefers
  ``joblib`` (the standard sklearn persistence format) and falls back to
  the stdlib ``pickle`` module so it works even if joblib is absent.

This module is file-disjoint from all active builders. It imports nothing
from settlement / dossier / tournament / gateway / outbox / inbox.
"""

from __future__ import annotations

import os
import pickle
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Path validation helpers
# ---------------------------------------------------------------------------


def _validate_path(path: str) -> None:
    """Validate that ``path`` exists and is a non-empty file.

    Raises:
        FileNotFoundError: if ``path`` does not exist or is not a file.
        ValueError: if ``path`` is empty or the file is zero bytes.
    """
    if not path or not isinstance(path, str) or not path.strip():
        raise ValueError("artifact path must be a non-empty string")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"artifact file not found: {path!r}")
    if os.path.getsize(path) == 0:
        raise ValueError(f"artifact file is empty (0 bytes): {path!r}")


# ---------------------------------------------------------------------------
# Loader functions
# ---------------------------------------------------------------------------


def load_lightgbm_model(path: str) -> Any:
    """Load a LightGBM model from ``path``.

    Prefers ``lightgbm.Booster`` (the native saved-model format produced by
    ``booster.save_model``). Falls back to ``joblib``/``pickle`` for models
    saved via the sklearn API (``LGBMClassifier`` / ``LGBMRegressor``).

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if ``path`` is empty or the file is zero bytes.
        ImportError: if ``lightgbm`` is not installed and the file cannot
            be loaded via the pickle fallback.
    """
    _validate_path(path)
    try:
        import lightgbm as lgb
    except ImportError:
        # Fall back to joblib/pickle for sklearn-API models saved without
        # the native booster format. If that also fails, raise a helpful
        # ImportError.
        try:
            return _load_pickle_or_joblib(path)
        except Exception as exc:
            raise ImportError(
                "lightgbm is not installed and the artifact at "
                f"{path!r} could not be loaded via the pickle/joblib "
                f"fallback (original error: {exc}). Install lightgbm to "
                "load native LightGBM booster models."
            ) from exc
    return lgb.Booster(model_file=path)


def load_catboost_model(path: str) -> Any:
    """Load a CatBoost model from ``path``.

    Uses ``catboost.CatBoostClassifier.load_model`` /
    ``CatBoostRegressor.load_model`` depending on which class can load the
    file (CatBoost stores a model-type tag inside the file). If neither
    loads, falls back to ``joblib``/``pickle`` for sklearn-API models.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if ``path`` is empty or the file is zero bytes.
        ImportError: if ``catboost`` is not installed and the file cannot
            be loaded via the pickle fallback.
    """
    _validate_path(path)
    try:
        import catboost as cb
    except ImportError:
        try:
            return _load_pickle_or_joblib(path)
        except Exception as exc:
            raise ImportError(
                "catboost is not installed and the artifact at "
                f"{path!r} could not be loaded via the pickle/joblib "
                f"fallback (original error: {exc}). Install catboost to "
                "load native CatBoost models."
            ) from exc
    # Try classifier first, then regressor. CatBoost's load_model raises
    # if the file is the wrong model type, so we attempt both.
    for cls_name in ("CatBoostClassifier", "CatBoostRegressor"):
        cls = getattr(cb, cls_name, None)
        if cls is None:
            continue
        try:
            model = cls()
            model.load_model(path)
            return model
        except Exception:
            continue
    # Native load failed — try pickle/joblib (sklearn-API persistence).
    return _load_pickle_or_joblib(path)


def load_xgboost_model(path: str) -> Any:
    """Load an XGBoost model from ``path``.

    Prefers ``xgboost.Booster`` (native format). Falls back to
    ``joblib``/``pickle`` for sklearn-API models.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if ``path`` is empty or the file is zero bytes.
        ImportError: if ``xgboost`` is not installed and the file cannot
            be loaded via the pickle fallback.
    """
    _validate_path(path)
    try:
        import xgboost as xgb
    except ImportError:
        try:
            return _load_pickle_or_joblib(path)
        except Exception as exc:
            raise ImportError(
                "xgboost is not installed and the artifact at "
                f"{path!r} could not be loaded via the pickle/joblib "
                f"fallback (original error: {exc}). Install xgboost to "
                "load native XGBoost booster models."
            ) from exc
    return xgb.Booster(model_file=path)


def load_sklearn_pickle(path: str) -> Any:
    """Load a scikit-learn model from a pickle/joblib file at ``path``.

    Prefers ``joblib`` (the standard sklearn persistence format) and
    falls back to the stdlib ``pickle`` module so the loader works even
    if joblib is absent.

    Raises:
        FileNotFoundError: if ``path`` does not exist.
        ValueError: if ``path`` is empty or the file is zero bytes.
    """
    _validate_path(path)
    return _load_pickle_or_joblib(path)


def _load_pickle_or_joblib(path: str) -> Any:
    """Load a pickled object from ``path``, preferring joblib.

    Raises the underlying exception if both joblib and pickle fail.
    """
    try:
        import joblib

        return joblib.load(path)
    except ImportError:
        pass
    with open(path, "rb") as fh:
        return pickle.load(fh)


# ---------------------------------------------------------------------------
# Loader registry + resolver
# ---------------------------------------------------------------------------

#: Mapping of fully-qualified loader names (as referenced by
#: :attr:`ModelFamilySpec.artifact_loader`) to the concrete loader
#: callables. Adding a loader is a single entry here + a function above.
LOADER_REGISTRY: dict[str, Callable[..., Any]] = {
    "quant_foundry.artifact_io.load_lightgbm_model": load_lightgbm_model,
    "quant_foundry.artifact_io.load_catboost_model": load_catboost_model,
    "quant_foundry.artifact_io.load_xgboost_model": load_xgboost_model,
    "quant_foundry.artifact_io.load_sklearn_pickle": load_sklearn_pickle,
}


def resolve_loader(name: str) -> Callable[..., Any]:
    """Resolve a loader by its fully-qualified name.

    Args:
        name: the loader name (a key in :data:`LOADER_REGISTRY`), e.g.
            ``"quant_foundry.artifact_io.load_sklearn_pickle"``.

    Returns:
        The loader callable.

    Raises:
        ValueError: if ``name`` is not a registered loader.
    """
    if not name or not isinstance(name, str) or not name.strip():
        raise ValueError("loader name must be a non-empty string")
    if name not in LOADER_REGISTRY:
        raise ValueError(f"unknown artifact loader {name!r}; known: {sorted(LOADER_REGISTRY)}")
    return LOADER_REGISTRY[name]
