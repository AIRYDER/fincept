"""
quant_foundry.tabpfn_adapter — TabPFN shadow adapter (T-9.4).

TabPFN is a tabular foundation model that uses **in-context learning**: the
model is not explicitly trained on the user's data — the train set is fed
directly into the forward pass as context, and predictions are produced for
the test set in a single inference call. That makes it extremely fast on
small datasets but introduces two specific risks that this adapter guards
against:

1. **Size limit.** TabPFN is hard-capped at small datasets (the published
   model supports up to 10,000 train samples / 500 features; the adapter's
   default is even tighter at 1,000 samples / 100 features to stay well
   inside the regime where TabPFN is competitive). Running it on an
   oversized dataset silently degrades or errors out, so the adapter
   **fails closed** when the dataset exceeds the configured limit.

2. **In-context label leakage.** Because the train set is part of the
   inference context, any test row that is an exact duplicate of a train
   row (or that has train labels embedded in its features) is a leakage
   signal — the model would be "memorising" rather than generalising. The
   adapter checks for both and **fails closed** when leakage is detected.

The adapter is **shadow-only by default**: ``TabPFNConfig.shadow_only`` is
``True`` and ``TabPFNShadowResult.promotion_eligible`` is forced to
``False``. Promoting a TabPFN shadow to production requires an explicit
manual policy change via ``validate_promotion_eligibility(...,
manual_override=True)`` — there is no automatic path.

Design notes (cross-cutting quant rigor, BIG_PLAN):

- **No live trading authority.** Shadow output is never promotion eligible
  without a manual override. The default config marks every run as shadow.
- **Cost fails closed.** Oversized datasets and detected leakage both
  produce a ``TabPFNShadowResult`` with ``predictions=None`` and
  ``is_shadow=True`` rather than a partial / silent run.
- **No secrets.** Configs carry only limits, device, task type, ensemble
  size, and a seed — never credentials or filesystem paths beyond the
  optional artifact path.
- **Lazy import.** ``tabpfn`` is imported inside methods, so this module is
  importable on hosts without ``tabpfn`` installed (the test suite mocks
  the inference path).
- **File-disjoint.** New module; does not modify ``real_trainer.py`` or
  ``alpha_genome.py``. ``register_tabpfn_family`` returns a
  ``ModelFamilySpec``-compatible dict for later registration but does not
  mutate the registry itself.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Hard limits (TabPFN published model caps)
# ---------------------------------------------------------------------------

# TabPFN's published model supports up to 10,000 train samples. The adapter
# default (TabPFNConfig.max_train_samples) is 1,000 — well inside the
# regime where TabPFN is competitive — but a user may raise it up to this
# hard cap. Anything above this is rejected by the validator.
TABPFN_HARD_MAX_TRAIN_SAMPLES: int = 10_000

# TabPFN's published model supports up to 500 features. The adapter default
# (TabPFNConfig.max_features) is 100. Anything above this hard cap is
# rejected by the validator.
TABPFN_HARD_MAX_FEATURES: int = 500

# Allowed task types.
ALLOWED_TASK_TYPES: frozenset[str] = frozenset({"binary", "multiclass", "regression"})

# Allowed device strings.
ALLOWED_DEVICES: frozenset[str] = frozenset({"auto", "cpu", "cuda"})


# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


class TabPFNConfig(BaseModel):
    """Configuration for a TabPFN shadow adapter run.

    Frozen + ``extra='forbid'`` for audit integrity. Defaults keep TabPFN
    well inside its small-dataset regime and mark every run as shadow-only
    (not promotion eligible without a manual policy override).

    Attributes:
        max_train_samples: Maximum train samples the adapter will accept.
            TabPFN's published hard limit is 10,000; the adapter default is
            1,000 to stay in the competitive regime.
        max_features: Maximum features the adapter will accept. TabPFN's
            published hard limit is 500; the adapter default is 100.
        device: Device to run on — ``auto``, ``cpu``, or ``cuda``.
        shadow_only: When ``True`` (default), the run is marked shadow and
            ``promotion_eligible`` is forced to ``False``.
        task_type: One of ``binary``, ``multiclass``, ``regression``.
        n_ensemble_configurations: Number of ensemble configurations
            TabPFN averages over (passed through to the TabPFN client).
        seed: Random seed for reproducibility.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_train_samples: int = 1000
    max_features: int = 100
    device: str = "auto"
    shadow_only: bool = True
    task_type: str = "binary"
    n_ensemble_configurations: int = 4
    seed: int = 42

    @field_validator("max_train_samples")
    @classmethod
    def _validate_max_train_samples(cls, v: int) -> int:
        """Reject values above the TabPFN hard limit or below 1."""
        if v < 1:
            raise ValueError("max_train_samples must be >= 1")
        if v > TABPFN_HARD_MAX_TRAIN_SAMPLES:
            raise ValueError(
                f"max_train_samples must be <= {TABPFN_HARD_MAX_TRAIN_SAMPLES} (TabPFN hard limit)"
            )
        return v

    @field_validator("max_features")
    @classmethod
    def _validate_max_features(cls, v: int) -> int:
        """Reject values above the TabPFN hard feature limit or below 1."""
        if v < 1:
            raise ValueError("max_features must be >= 1")
        if v > TABPFN_HARD_MAX_FEATURES:
            raise ValueError(
                f"max_features must be <= {TABPFN_HARD_MAX_FEATURES} (TabPFN hard limit)"
            )
        return v

    @field_validator("device")
    @classmethod
    def _validate_device(cls, v: str) -> str:
        """Reject device strings outside the allowlist."""
        if v not in ALLOWED_DEVICES:
            raise ValueError(f"device must be one of {sorted(ALLOWED_DEVICES)}, got {v!r}")
        return v

    @field_validator("task_type")
    @classmethod
    def _validate_task_type(cls, v: str) -> str:
        """Reject task types outside the allowlist."""
        if v not in ALLOWED_TASK_TYPES:
            raise ValueError(f"task_type must be one of {sorted(ALLOWED_TASK_TYPES)}, got {v!r}")
        return v

    @field_validator("n_ensemble_configurations")
    @classmethod
    def _validate_n_ensemble(cls, v: int) -> int:
        """Reject non-positive ensemble sizes."""
        if v < 1:
            raise ValueError("n_ensemble_configurations must be >= 1")
        return v

    @field_validator("seed")
    @classmethod
    def _validate_seed(cls, v: int) -> int:
        """Reject negative seeds (0 is allowed)."""
        if v < 0:
            raise ValueError("seed must be >= 0")
        return v


class DatasetSizeCheck(BaseModel):
    """Result of checking a dataset against a :class:`TabPFNConfig`'s limits.

    Frozen + ``extra='forbid'``. ``within_limit`` is ``True`` only when both
    ``n_samples`` and ``n_features`` are at or below the configured maximums.
    ``reason`` carries a human-readable explanation when the dataset is out
    of limit (``None`` when within limit).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_samples: int
    n_features: int
    within_limit: bool
    reason: str | None = None


class TabPFNShadowResult(BaseModel):
    """Result of a :class:`TabPFNShadowAdapter` shadow run.

    Frozen + ``extra='forbid'`` for audit integrity. A fail-closed run
    (oversized dataset or detected leakage) has ``predictions=None``,
    ``is_shadow=True``, ``promotion_eligible=False``,
    ``leakage_check_passed=False`` (when leakage was the cause), and an
    explanatory entry in ``metrics``.

    Attributes:
        config: The :class:`TabPFNConfig` used for the run.
        size_check: The :class:`DatasetSizeCheck` for the submitted dataset.
        predictions: Predictions for the test set, or ``None`` when the run
            failed closed (oversized / leakage).
        artifact_path: Path the artifact was saved to, or ``None``.
        is_shadow: ``True`` for shadow runs (always ``True`` when
            ``config.shadow_only`` is ``True``).
        promotion_eligible: ``False`` when ``is_shadow`` is ``True``; only
            ``True`` for a non-shadow run.
        leakage_check_passed: ``True`` when the in-context leakage check
            passed; ``False`` when leakage was detected.
        metrics: Computed metrics (e.g. accuracy / rmse) plus explanatory
            entries for fail-closed runs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: TabPFNConfig
    size_check: DatasetSizeCheck
    predictions: list[float] | None = None
    artifact_path: str | None = None
    is_shadow: bool = True
    promotion_eligible: bool = False
    leakage_check_passed: bool = True
    metrics: dict[str, float] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dataset size check
# ---------------------------------------------------------------------------


def check_dataset_size(n_samples: int, n_features: int, config: TabPFNConfig) -> DatasetSizeCheck:
    """Check whether a dataset fits within ``config``'s limits.

    Returns a :class:`DatasetSizeCheck` with ``within_limit=True`` when
    ``n_samples <= config.max_train_samples`` and
    ``n_features <= config.max_features``. Otherwise returns
    ``within_limit=False`` with a ``reason`` explaining which limit was
    exceeded.

    Args:
        n_samples: Number of train samples in the dataset.
        n_features: Number of features in the dataset.
        config: The :class:`TabPFNConfig` to check against.

    Returns:
        A :class:`DatasetSizeCheck`.
    """
    reasons: list[str] = []
    if n_samples > config.max_train_samples:
        reasons.append(
            f"n_samples={n_samples} exceeds max_train_samples={config.max_train_samples}"
        )
    if n_features > config.max_features:
        reasons.append(f"n_features={n_features} exceeds max_features={config.max_features}")
    if reasons:
        return DatasetSizeCheck(
            n_samples=n_samples,
            n_features=n_features,
            within_limit=False,
            reason="; ".join(reasons),
        )
    return DatasetSizeCheck(
        n_samples=n_samples,
        n_features=n_features,
        within_limit=True,
        reason=None,
    )


# ---------------------------------------------------------------------------
# In-context leakage detection
# ---------------------------------------------------------------------------


def _as_row_set(data: Any) -> set[tuple[float, ...]]:
    """Convert a 2-D array-like to a set of float tuples for fast lookup.

    Accepts lists of lists or any object with ``.tolist()`` (numpy arrays,
    torch tensors). Each row is converted to a tuple of floats so it is
    hashable and comparable. Non-finite values (nan / inf) are kept as-is
    (Python's ``float`` handles them in tuples / sets).
    """
    rows: list[tuple[float, ...]] = []
    if hasattr(data, "tolist"):
        data = data.tolist()
    for row in data:
        rows.append(tuple(float(x) for x in row))
    return set(rows)


def detect_in_context_leakage(train_data: Any, train_labels: Any, test_data: Any) -> bool:
    """Detect in-context label leakage for a TabPFN run.

    TabPFN uses in-context learning — the train set is fed into the forward
    pass as context, so any test row that is an exact duplicate of a train
    row, or any test feature vector that contains a train label value, is a
    leakage signal. This function checks both:

    1. **Exact row match.** If any test row is byte-for-byte identical to a
       train row, the model would be memorising rather than generalising.
    2. **Label embedding.** If any train label value appears as one of the
       feature values in a test row, the label may be embedded in the
       features (a subtle leakage where the label leaks into a feature
       column).

    Args:
        train_data: 2-D array-like of train features.
        train_labels: 1-D sequence of train labels.
        test_data: 2-D array-like of test features.

    Returns:
        ``True`` if **no** leakage was detected (safe to proceed).
        ``False`` if leakage was detected (fail closed).
    """
    # Empty test data — nothing to leak.
    if test_data is None:
        return True
    test_rows = _as_row_set(test_data)
    if not test_rows:
        return True

    # 1. Exact row match: any test row identical to a train row.
    train_rows = _as_row_set(train_data)
    if train_rows & test_rows:
        return False

    # 2. Label embedding: any train label value appears in a test feature.
    label_values: set[float] = set()
    if train_labels is not None:
        for lbl in train_labels:
            label_values.add(float(lbl))
    if label_values:
        for row in test_rows:
            if any(feat in label_values for feat in row):
                return False

    return True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _compute_metrics(
    predictions: list[float],
    test_labels: Any,
    task_type: str,
) -> dict[str, float]:
    """Compute simple metrics for a shadow run.

    For ``binary`` / ``multiclass`` tasks, predictions are interpreted as
    class indices (rounded) and accuracy is computed. For ``regression``,
    RMSE and MAE are computed. ``test_labels`` may be a sequence or any
    object with ``.tolist()``.
    """
    if hasattr(test_labels, "tolist"):
        labels_list = [float(x) for x in test_labels.tolist()]
    else:
        labels_list = [float(x) for x in test_labels]

    metrics: dict[str, float] = {}
    if task_type == "regression":
        if not labels_list:
            return {"rmse": float("nan"), "mae": float("nan")}
        sq_errors = [(p - y) ** 2 for p, y in zip(predictions, labels_list, strict=False)]
        abs_errors = [abs(p - y) for p, y in zip(predictions, labels_list, strict=False)]
        metrics["rmse"] = float(sum(sq_errors) / len(sq_errors)) ** 0.5
        metrics["mae"] = float(sum(abs_errors) / len(abs_errors))
    else:
        # binary / multiclass — round predictions to nearest class index.
        if not labels_list:
            return {"accuracy": float("nan")}
        correct = sum(
            1 for p, y in zip(predictions, labels_list, strict=False) if round(p) == round(y)
        )
        metrics["accuracy"] = float(correct) / len(labels_list)
    return metrics


# ---------------------------------------------------------------------------
# Shadow adapter
# ---------------------------------------------------------------------------


class TabPFNShadowAdapter:
    """Shadow adapter that restricts TabPFN to small / regime datasets.

    The adapter enforces three safety guards before running TabPFN
    inference:

    1. **Dataset size.** ``check_dataset_size`` rejects datasets that
       exceed ``config.max_train_samples`` / ``config.max_features``.
    2. **In-context leakage.** ``detect_in_context_leakage`` rejects runs
       where a test row duplicates a train row or a train label is embedded
       in a test feature.
    3. **Shadow-only.** When ``config.shadow_only`` is ``True`` (default),
       the result is marked shadow and ``promotion_eligible`` is forced to
       ``False``.

    Each guard fails closed: a guard failure produces a
    :class:`TabPFNShadowResult` with ``predictions=None`` rather than a
    partial / silent run.

    The ``tabpfn`` package is imported lazily inside :meth:`run_shadow`, so
    the adapter is constructable and the guards are runnable on hosts
    without ``tabpfn`` installed.
    """

    def __init__(self, config: TabPFNConfig) -> None:
        """Construct the adapter with a :class:`TabPFNConfig`.

        Args:
            config: The :class:`TabPFNConfig` to use for all runs.
        """
        if not isinstance(config, TabPFNConfig):
            raise TypeError("config must be a TabPFNConfig")
        self.config = config

    # --- public API ---------------------------------------------------

    def run_shadow(
        self,
        train_data: Any,
        train_labels: Any,
        test_data: Any,
        test_labels: Any,
        artifact_path: str | None = None,
    ) -> TabPFNShadowResult:
        """Run a TabPFN shadow inference with all safety guards.

        Args:
            train_data: 2-D array-like of train features.
            train_labels: 1-D sequence of train labels.
            test_data: 2-D array-like of test features.
            test_labels: 1-D sequence of test labels (used for metrics).
            artifact_path: Optional path to save the result artifact to.

        Returns:
            A :class:`TabPFNShadowResult`. Fail-closed runs have
            ``predictions=None`` and an explanatory ``metrics`` entry.
        """
        # Derive dataset shape.
        n_samples = self._count_rows(train_data)
        n_features = self._count_features(train_data, test_data)

        # 1. Size guard (fail closed).
        size_check = check_dataset_size(n_samples, n_features, self.config)
        if not size_check.within_limit:
            result = TabPFNShadowResult(
                config=self.config,
                size_check=size_check,
                predictions=None,
                artifact_path=None,
                is_shadow=True,
                promotion_eligible=False,
                leakage_check_passed=True,
                metrics={
                    "status_oversized": 1.0,
                },
            )
            return self._maybe_save(result, artifact_path)

        # 2. Leakage guard (fail closed).
        leakage_ok = detect_in_context_leakage(train_data, train_labels, test_data)
        if not leakage_ok:
            result = TabPFNShadowResult(
                config=self.config,
                size_check=size_check,
                predictions=None,
                artifact_path=None,
                is_shadow=True,
                promotion_eligible=False,
                leakage_check_passed=False,
                metrics={"status_leakage_detected": 1.0},
            )
            return self._maybe_save(result, artifact_path)

        # 3. Run TabPFN inference (lazy import).
        predictions = self._run_tabpfn_inference(train_data, train_labels, test_data)

        # 4. Compute metrics.
        metrics = _compute_metrics(predictions, test_labels, self.config.task_type)

        # 5. Build result (shadow-only forces promotion_eligible=False).
        is_shadow = self.config.shadow_only
        promotion_eligible = (not is_shadow) and True
        result = TabPFNShadowResult(
            config=self.config,
            size_check=size_check,
            predictions=predictions,
            artifact_path=None,
            is_shadow=is_shadow,
            promotion_eligible=promotion_eligible,
            leakage_check_passed=True,
            metrics=metrics,
        )
        return self._maybe_save(result, artifact_path)

    def _maybe_save(
        self, result: TabPFNShadowResult, artifact_path: str | None
    ) -> TabPFNShadowResult:
        """Save ``result`` to ``artifact_path`` if provided.

        Returns a new :class:`TabPFNShadowResult` with ``artifact_path``
        set to the path (or the original result when ``artifact_path`` is
        ``None``). The saved JSON always carries the final
        ``artifact_path`` so a round-trip load is equal to the returned
        result.
        """
        if artifact_path is None:
            return result
        result = result.model_copy(update={"artifact_path": artifact_path})
        self.save_artifact(result, artifact_path)
        return result

    def save_artifact(self, result: TabPFNShadowResult, path: str) -> None:
        """Save a :class:`TabPFNShadowResult` to ``path`` as JSON.

        Creates parent directories as needed. The result is serialised via
        ``model_dump_json`` (Pydantic v2 canonical JSON).
        """
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    def load_artifact(self, path: str) -> TabPFNShadowResult:
        """Load a :class:`TabPFNShadowResult` from ``path`` (JSON).

        Raises ``FileNotFoundError`` if the file does not exist.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"artifact not found: {path}")
        return TabPFNShadowResult.model_validate_json(p.read_text(encoding="utf-8"))

    # --- internals ----------------------------------------------------

    @staticmethod
    def _count_rows(data: Any) -> int:
        """Count the number of rows in a 2-D array-like."""
        if data is None:
            return 0
        if hasattr(data, "shape"):
            try:
                return int(data.shape[0])
            except Exception:
                pass
        if hasattr(data, "__len__"):
            return len(data)
        return 0

    @staticmethod
    def _count_features(train_data: Any, test_data: Any) -> int:
        """Count the number of features from train or test data."""
        for data in (train_data, test_data):
            if data is None:
                continue
            if hasattr(data, "shape"):
                try:
                    if len(data.shape) >= 2:
                        return int(data.shape[1])
                except Exception:
                    pass
            if hasattr(data, "__len__") and len(data) > 0:
                first = data[0]
                if hasattr(first, "__len__"):
                    return len(first)
        return 0

    def _run_tabpfn_inference(
        self,
        train_data: Any,
        train_labels: Any,
        test_data: Any,
    ) -> list[float]:
        """Run TabPFN inference (lazy import of ``tabpfn``).

        Converts inputs to lists, constructs a TabPFN classifier/regressor
        with the configured ensemble size and seed, and calls ``fit`` /
        ``predict_proba`` (classification) or ``fit`` / ``predict``
        (regression). Predictions are returned as a flat list of floats.

        Raises ``ImportError`` if ``tabpfn`` is not installed.
        """
        try:
            from tabpfn import TabPFNClassifier, TabPFNRegressor
        except Exception as exc:  # pragma: no cover - environment-specific
            raise ImportError(
                "tabpfn is not installed; install the 'tabpfn' package to run TabPFN inference"
            ) from exc

        # Normalise inputs to lists.
        if hasattr(train_data, "tolist"):
            train_x = train_data.tolist()
        else:
            train_x = [list(r) for r in train_data]
        if hasattr(train_labels, "tolist"):
            train_y = train_labels.tolist()
        else:
            train_y = list(train_labels)
        if hasattr(test_data, "tolist"):
            test_x = test_data.tolist()
        else:
            test_x = [list(r) for r in test_data]

        n_ensemble = self.config.n_ensemble_configurations
        seed = self.config.seed

        if self.config.task_type == "regression":
            model = TabPFNRegressor(
                n_estimators=n_ensemble,
                random_state=seed,
            )
            model.fit(train_x, train_y)
            preds = model.predict(test_x)
            return [float(p) for p in preds]

        # binary / multiclass
        model = TabPFNClassifier(
            n_estimators=n_ensemble,
            random_state=seed,
        )
        model.fit(train_x, train_y)
        proba = model.predict_proba(test_x)
        # For binary, return probability of the positive class. For
        # multiclass, return the argmax class index as a float.
        out: list[float] = []
        for row in proba:
            row_list = [float(p) for p in row]
            if len(row_list) == 2:
                out.append(row_list[1])
            else:
                out.append(float(row_list.index(max(row_list))))
        return out


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: TabPFNShadowResult, manual_override: bool = False
) -> bool:
    """Validate whether a :class:`TabPFNShadowResult` is promotion eligible.

    Shadow results are **never** promotion eligible without an explicit
    manual policy change. This function encodes that policy:

    - If ``result.is_shadow`` is ``True`` and ``manual_override`` is
      ``False``: returns ``False`` (shadow cannot be promoted automatically).
    - If ``manual_override`` is ``True``: returns ``True`` (an explicit
      manual policy change has authorised promotion).
    - If ``result.is_shadow`` is ``False``: returns ``True`` (non-shadow
      runs are promotion eligible by default).

    Args:
        result: The :class:`TabPFNShadowResult` to validate.
        manual_override: When ``True``, authorises promotion of a shadow
            result (requires an explicit manual policy change — there is no
            automatic path that sets this).

    Returns:
        ``True`` if the result is promotion eligible, ``False`` otherwise.
    """
    if manual_override:
        return True
    if result.is_shadow:
        return False
    return True


# ---------------------------------------------------------------------------
# Family registration helper
# ---------------------------------------------------------------------------


def register_tabpfn_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for TabPFN registration.

    The returned dict carries the fields a :class:`ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus TabPFN-
    specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not** mutate
    the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks TabPFN as a shadow / research family: it is **not** a
    baseline exception, requires a GPU (TabPFN benefits from CUDA), and
    defaults to the ``CHALLENGER`` promotion-eligibility class (though the
    adapter itself forces ``promotion_eligible=False`` when
    ``shadow_only=True``).
    """
    return {
        "family_id": "tabpfn",
        "display_name": "TabPFN (shadow)",
        "version": "1",
        "dataset_shape": "small_tabular",
        "objectives": ("binary", "multiclass", "regression"),
        "artifact_format": "json",
        "artifact_loader": "tabpfn_shadow_result",
        "required_metrics": ("accuracy", "rmse", "mae"),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "challenger",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "shadow_only": True,
        "max_train_samples": TABPFN_HARD_MAX_TRAIN_SAMPLES,
        "max_features": TABPFN_HARD_MAX_FEATURES,
    }


__all__ = [
    "ALLOWED_DEVICES",
    "ALLOWED_TASK_TYPES",
    "TABPFN_HARD_MAX_FEATURES",
    "TABPFN_HARD_MAX_TRAIN_SAMPLES",
    "DatasetSizeCheck",
    "TabPFNConfig",
    "TabPFNShadowAdapter",
    "TabPFNShadowResult",
    "check_dataset_size",
    "detect_in_context_leakage",
    "register_tabpfn_family",
    "validate_promotion_eligibility",
]
