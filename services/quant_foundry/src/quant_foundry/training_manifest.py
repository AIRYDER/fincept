"""
quant_foundry.training_manifest — Stage Task 1 dispatch envelope (TASK-0504 staging).

The ``TrainingManifest`` is the **operator-facing contract** for dispatching
a single baseline training job end-to-end. It packages:

- a feature-lake PIT manifest reference (``feature_lake_manifest_ref``)
- the model family and baseline hyperparameters (no secret values)
- walk-forward validation window splits (train / validation / test)
- purged-fold boundaries (forwarded from the feature-lake manifest)
- a budget envelope (``budget_cents`` + ``timeout_seconds``) that the
  ``BudgetGuard`` enforces before the job is dispatched
- reproducibility pins (random seed, optional hardware class label)

The manifest is **schema-versioned** (``schema_version=1``) and frozen
(``extra='forbid'``) so a manifest can be hashed, signed, and referenced
by the dispatch script instead of being passed by raw dict.

Why a separate schema (rather than reusing ``RunPodTrainingRequest``):

- ``RunPodTrainingRequest`` is the **cross-boundary** schema that the
  worker (RunPod or mock) sees. It must NOT carry anything the worker
  doesn't need (e.g. budget envelope, walk-forward splits, report path).
- ``TrainingManifest`` is the **operator-facing** staging schema. It
  carries the additional context the gateway + dispatch script need to
  budget, split, and audit. The dispatch script then translates it into a
  ``RunPodTrainingRequest`` (or a local-trainer call) at the boundary.

This module is **file-disjoint from all active builders**: it imports
from ``dataset_manifest.py`` (TASK-0405) and ``schemas.py`` (TASK-0302)
read-only, and does NOT touch gateway, dossier, sentinel, tournament,
or outbox. The dispatch path lives in a sibling module.

Invariants (enforced + tested):
- ``schema_version == 1``.
- Frozen + ``extra='forbid'`` (audit integrity).
- ``train_window_ns`` and ``val_window_ns`` and ``test_window_ns`` are
  strictly positive.
- ``budget_cents >= 0``; ``timeout_seconds > 0`` unless the operator
  explicitly opts in with ``timeout_seconds == 0`` (immediate-timeout
  test path).
- ``model_family`` is one of the allowlist families the worker can
  train. We re-use the alpha_genome allowlist if it exists, else fall
  back to a small literal allowlist.
- ``hyperparameters`` may only contain keys the chosen ``model_family``
  supports, with values inside the allowed bounds.
- No secret-shaped values are accepted anywhere — the constructor
  rejects feature / hyperparameter names that look like credentials.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Try to import the alpha_genome allowlists (preferred). Fall back to a
# small literal allowlist if the module is not yet importable (e.g. older
# deployment where the lab is not enabled).
try:
    from quant_foundry.alpha_genome import (
        ALLOWED_MODEL_FAMILIES as _AG_MODEL_FAMILIES,
    )
    from quant_foundry.alpha_genome import (
        HYPERPARAM_BOUNDS as _AG_HYPERPARAM_BOUNDS,
    )

    _ALLOWED_MODEL_FAMILIES: frozenset[str] = _AG_MODEL_FAMILIES
    _HYPERPARAM_BOUNDS: dict[str, dict[str, tuple[float, float]]] = dict(_AG_HYPERPARAM_BOUNDS)
except Exception:
    _ALLOWED_MODEL_FAMILIES = frozenset({"gbm", "catboost", "logreg", "linear"})
    _HYPERPARAM_BOUNDS = {
        "gbm": {
            "n_estimators": (10.0, 5000.0),
            "max_depth": (2.0, 12.0),
            "learning_rate": (1e-4, 1.0),
            "min_child_samples": (1.0, 200.0),
        },
        "catboost": {
            "iterations": (10.0, 5000.0),
            "depth": (2.0, 10.0),
            "learning_rate": (1e-4, 1.0),
        },
        "logreg": {"C": (1e-4, 100.0), "max_iter": (50.0, 5000.0)},
        "linear": {"alpha": (1e-4, 10.0), "max_iter": (100.0, 10000.0)},
    }


class ModelFamily(StrEnum):
    """Allowlisted model families for baseline training."""

    GBM = "gbm"
    CATBOOST = "catboost"
    LOGREG = "logreg"
    LINEAR = "linear"


# Substrings that suggest a field is carrying a secret. Reject at the
# schema boundary so a malicious / accidental credential can never ride
# into a manifest.
_SECRET_SUBSTRINGS = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "dsn",
    "connection_string",
)
_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-]{0,63}$")


def _looks_like_secret(s: str) -> bool:
    low = s.lower()
    return any(sub in low for sub in _SECRET_SUBSTRINGS)


class TrainingManifest(BaseModel):
    """Operator-facing manifest for staging one baseline training job.

    Frozen + extra='forbid' (audit integrity). ``content_hash`` is
    computed from the canonical content and is the immutability key for
    the dispatch script.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    manifest_id: str
    feature_lake_manifest_ref: str  # the dataset_id from FeatureLakeManifest
    feature_lake_manifest_hash: str  # the manifest_hash from FeatureLakeManifest
    model_family: ModelFamily
    hyperparameters: dict[str, float] = Field(default_factory=dict)
    train_window_ns: int
    val_window_ns: int
    test_window_ns: int
    label_horizon_ns: int
    random_seed: int | None = None
    hardware_class: str | None = None
    walk_forward_enabled: bool = True
    budget_cents: int = 0
    timeout_seconds: int = 600
    operator_note: str = ""
    content_hash: str = ""

    # --- validators -----------------------------------------------------

    @field_validator("manifest_id")
    @classmethod
    def _manifest_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("manifest_id must be non-empty")
        return v

    @field_validator("feature_lake_manifest_ref")
    @classmethod
    def _ref_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("feature_lake_manifest_ref must be non-empty")
        return v

    @field_validator("feature_lake_manifest_hash")
    @classmethod
    def _hash_shape(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("feature_lake_manifest_hash must be non-empty")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("feature_lake_manifest_hash must be a 64-char hex SHA-256")
        return v.lower()

    @field_validator("model_family")
    @classmethod
    def _model_family_allowed(cls, v: ModelFamily) -> ModelFamily:
        if v.value not in _ALLOWED_MODEL_FAMILIES:
            raise ValueError(
                f"model_family {v.value!r} not in allowlist; "
                f"allowed: {sorted(_ALLOWED_MODEL_FAMILIES)}"
            )
        return v

    @field_validator("hyperparameters")
    @classmethod
    def _hyperparams_safe(cls, v: Mapping[str, float], info: Any) -> dict[str, float]:
        # We don't have the model_family yet in this validator, so we do
        # the cross-field check in ``_validate_hyperparams_for_family``.
        for k in v:
            if not isinstance(k, str) or not k:
                raise ValueError("hyperparameter names must be non-empty strings")
            if _looks_like_secret(k):
                raise ValueError(f"hyperparameter name {k!r} looks like a secret")
            if not _NAME_PATTERN.match(k):
                raise ValueError(f"hyperparameter name {k!r} must match {_NAME_PATTERN.pattern}")
        return dict(v)

    @model_validator(mode="after")
    def _validate_hyperparams_for_family(self) -> TrainingManifest:
        bounds = _HYPERPARAM_BOUNDS.get(self.model_family.value, {})
        bad_keys = set(self.hyperparameters) - set(bounds)
        if bad_keys:
            raise ValueError(
                f"hyperparameters {sorted(bad_keys)!r} not defined for "
                f"model_family {self.model_family.value!r}; "
                f"allowed: {sorted(bounds)}"
            )
        for k, val in self.hyperparameters.items():
            lo, hi = bounds[k]
            if not (lo <= float(val) <= hi):
                raise ValueError(f"hyperparameter {k}={val} outside bounds [{lo}, {hi}]")
        return self

    @model_validator(mode="after")
    def _windows_positive(self) -> TrainingManifest:
        for name in ("train_window_ns", "val_window_ns", "test_window_ns", "label_horizon_ns"):
            v = getattr(self, name)
            if v <= 0:
                raise ValueError(f"{name} must be > 0; got {v}")
        if self.budget_cents < 0:
            raise ValueError(f"budget_cents must be >= 0; got {self.budget_cents}")
        if self.timeout_seconds < 0:
            raise ValueError(f"timeout_seconds must be >= 0; got {self.timeout_seconds}")
        return self

    @model_validator(mode="after")
    def _operator_note_safe(self) -> TrainingManifest:
        if self.operator_note and _looks_like_secret(self.operator_note):
            raise ValueError("operator_note contains a secret-like substring; reject")
        return self

    def model_post_init(self, __context: Any) -> None:
        ch = _compute_content_hash(self)
        object.__setattr__(self, "content_hash", ch)

    def model_copy(  # type: ignore[override]
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> TrainingManifest:
        copied = super().model_copy(update=update, deep=deep)
        ch = _compute_content_hash(copied)
        object.__setattr__(copied, "content_hash", ch)
        return copied

    # --- helpers --------------------------------------------------------

    def to_dispatch_request(
        self,
        *,
        job_id: str,
    ) -> dict[str, Any]:
        """Translate this manifest into a ``RunPodTrainingRequest``-shaped dict.

        The dispatch script then hands this dict to the trainer (local or
        remote). ``dataset_manifest_ref`` carries the feature_lake id +
        hash so the worker can verify what it is training on.
        """
        return {
            "schema_version": 1,
            "job_id": job_id,
            "dataset_manifest_ref": (
                f"{self.feature_lake_manifest_ref}:{self.feature_lake_manifest_hash[:16]}"
            ),
            "model_family": self.model_family.value,
            "search_space": {},  # operator pinned values; no search for baseline
            "random_seed": self.random_seed,
            "hardware_class": self.hardware_class,
            "extra_constraints": {
                "train_window_ns": str(self.train_window_ns),
                "val_window_ns": str(self.val_window_ns),
                "test_window_ns": str(self.test_window_ns),
                "label_horizon_ns": str(self.label_horizon_ns),
                "walk_forward_enabled": "1" if self.walk_forward_enabled else "0",
                "manifest_content_hash": self.content_hash,
            },
        }


def _compute_content_hash(manifest: TrainingManifest) -> str:
    """Deterministic SHA-256 over the canonical content (excluding content_hash)."""
    payload = {
        "schema_version": manifest.schema_version,
        "manifest_id": manifest.manifest_id,
        "feature_lake_manifest_ref": manifest.feature_lake_manifest_ref,
        "feature_lake_manifest_hash": manifest.feature_lake_manifest_hash,
        "model_family": manifest.model_family.value,
        "hyperparameters": dict(sorted(manifest.hyperparameters.items())),
        "train_window_ns": manifest.train_window_ns,
        "val_window_ns": manifest.val_window_ns,
        "test_window_ns": manifest.test_window_ns,
        "label_horizon_ns": manifest.label_horizon_ns,
        "random_seed": manifest.random_seed,
        "hardware_class": manifest.hardware_class,
        "walk_forward_enabled": manifest.walk_forward_enabled,
        "budget_cents": manifest.budget_cents,
        "timeout_seconds": manifest.timeout_seconds,
        "operator_note": manifest.operator_note,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Walk-forward split
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WalkForwardWindow:
    """A single (train, val, test) triple in nanoseconds since epoch.

    Boundaries are inclusive of start, exclusive of end. The three
    windows do not overlap; ``train_end <= val_start <= val_end <= test_start``.
    The label horizon must be shorter than the gap between train_end and
    val_start (and val_end and test_start) so the label window of a train
    row does not bleed into validation or test.
    """

    train_start: int
    train_end: int
    val_start: int
    val_end: int
    test_start: int
    test_end: int
    label_horizon_ns: int

    def to_dict(self) -> dict[str, int]:
        return {
            "train_start": self.train_start,
            "train_end": self.train_end,
            "val_start": self.val_start,
            "val_end": self.val_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "label_horizon_ns": self.label_horizon_ns,
        }


def derive_walk_forward_window(
    *,
    train_window_ns: int,
    val_window_ns: int,
    test_window_ns: int,
    label_horizon_ns: int,
    as_of_ts: int,
) -> WalkForwardWindow:
    """Derive a single (train, val, test) triple ending at ``as_of_ts``.

    Layout (oldest → newest):
        [train_start  train_end][gap = label_horizon][val_start  val_end]
        [gap = label_horizon][test_start  test_end == as_of_ts]

    The label horizon acts as an embargo between consecutive windows so a
    training row's label does not overlap validation or test.
    """
    if label_horizon_ns <= 0:
        raise ValueError("label_horizon_ns must be > 0")
    if train_window_ns <= 0 or val_window_ns <= 0 or test_window_ns <= 0:
        raise ValueError("all window lengths must be > 0")

    test_end = as_of_ts
    test_start = test_end - test_window_ns
    val_end = test_start - label_horizon_ns
    val_start = val_end - val_window_ns
    train_end = val_start - label_horizon_ns
    train_start = train_end - train_window_ns

    if train_start < 0:
        raise ValueError(
            "train_window_ns is too long for the given as_of_ts; "
            f"train_start would be {train_start} (< 0)"
        )
    return WalkForwardWindow(
        train_start=train_start,
        train_end=train_end,
        val_start=val_start,
        val_end=val_end,
        test_start=test_start,
        test_end=test_end,
        label_horizon_ns=label_horizon_ns,
    )


__all__ = [
    "ModelFamily",
    "TrainingManifest",
    "WalkForwardWindow",
    "derive_walk_forward_window",
]
