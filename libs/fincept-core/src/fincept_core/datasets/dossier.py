"""Dossier + calibration sidecar helpers for the ML dataset evidence spine.

These are *pure* helpers (no disk I/O, no numpy/lightgbm/scipy/sklearn) so
they can be imported from any service without dragging a heavy ML stack into
the dependency graph.  Callers decide where (and whether) to persist the
returned dicts.

``build_dossier`` returns a JSON-safe dict matching the Quant Foundry
``DossierRecord`` shape (see
``services/quant_foundry/src/quant_foundry/dossier.py:62-180``) for parity
*only* -- this module does NOT import from ``quant_foundry`` (that would
create a circular dependency and violate the layering rule).

``build_calibration_sidecar`` returns the bucket/ECE/Brier shape consumed by
the Quant Foundry real-trainer calibration report (see
``services/quant_foundry/src/quant_foundry/real_trainer.py:485-566``), again
without importing it.  Uses Python's ``statistics`` module for the mean so
the import set stays minimal.
"""

from __future__ import annotations

import statistics
from typing import Any

from .schemas import ArtifactManifest, DatasetManifest

__all__ = ["build_calibration_sidecar", "build_dossier"]


def build_dossier(
    *,
    artifact_manifest: ArtifactManifest,
    dataset_manifest: DatasetManifest,
    training_metrics: dict[str, float],
    blocking_issues: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a JSON-safe dossier dict from artifact + dataset manifests.

    The returned dict matches the field set of the Quant Foundry
    ``DossierRecord`` (parity only -- not imported).  Fields that are
    registry-managed on the Pydantic side (``registered_at_ns``) are emitted
    as ``None`` here; ``content_hash`` is left empty (``""``) because the
    registry computes it from the canonical content.  ``status`` defaults to
    ``"candidate"`` and ``trial_count`` defaults to ``1``.

    ``extra`` may carry additional reproducibility fields
    (``code_git_sha``, ``lockfile_hash``, ``container_image_digest``,
    ``random_seed``, ``hardware_class``, ``trial_count``,
    ``settlement_evidence_refs``, ``shadow_prediction_refs``,
    ``dataset_manifest_ref``) that are not present on the manifests
    themselves; any keys in ``extra`` that collide with manifest-derived
    fields are overridden by the explicit manifest values.
    """
    blocking = list(blocking_issues) if blocking_issues is not None else []
    extra_map = dict(extra) if extra is not None else {}

    dossier: dict[str, Any] = {
        "schema_version": 1,
        "model_id": artifact_manifest.artifact_id,
        "artifact_manifest_id": artifact_manifest.artifact_id,
        "artifact_sha256": artifact_manifest.sha256,
        "dataset_manifest_id": dataset_manifest.dataset_id,
        "dataset_manifest_ref": extra_map.get("dataset_manifest_ref"),
        "feature_schema_hash": artifact_manifest.feature_schema_hash,
        "label_schema_hash": artifact_manifest.label_schema_hash,
        "code_git_sha": artifact_manifest.code_git_sha,
        "lockfile_hash": artifact_manifest.lockfile_hash,
        "container_image_digest": artifact_manifest.container_image_digest,
        "random_seed": extra_map.get("random_seed"),
        "hardware_class": extra_map.get("hardware_class"),
        "trial_count": extra_map.get("trial_count", 1),
        "training_metrics": dict(training_metrics),
        "status": extra_map.get("status", "candidate"),
        "settlement_evidence_refs": list(extra_map.get("settlement_evidence_refs", [])),
        "shadow_prediction_refs": list(extra_map.get("shadow_prediction_refs", [])),
        "blocking_issues": blocking,
        "registered_at_ns": None,
        "content_hash": "",
    }
    return dossier


def build_calibration_sidecar(
    *,
    val_predictions: list[float],
    val_labels: list[int],
    n_buckets: int = 10,
) -> dict[str, Any]:
    """Compute calibration buckets, ECE and Brier score from val predictions.

    Returns ``{"buckets": [...], "ece": float, "brier": float}`` where each
    bucket is ``{"lo": float, "hi": float, "mean_pred": float,
    "mean_actual": float, "count": int}``.

    Buckets divide ``[0, 1]`` into ``n_buckets`` equal-width buckets; the
    final bucket is closed on both ends (``lo <= p <= hi``) while earlier
    buckets are half-open (``lo <= p < hi``), matching the algorithm in
    ``real_trainer._compute_metrics``.

    Edge cases:
      * Empty inputs -> ``{"buckets": [], "ece": 0.0, "brier": 0.0}``
        (no exception).
      * Length mismatch between ``val_predictions`` and ``val_labels`` ->
        ``ValueError``.
    """
    if len(val_predictions) != len(val_labels):
        raise ValueError(
            "val_predictions and val_labels must have the same length; "
            f"got {len(val_predictions)} vs {len(val_labels)}"
        )

    total = len(val_predictions)
    if total == 0:
        return {"buckets": [], "ece": 0.0, "brier": 0.0}

    if n_buckets < 1:
        raise ValueError(f"n_buckets must be >= 1; got {n_buckets}")

    buckets: list[dict[str, Any]] = []
    ece = 0.0
    for i in range(n_buckets):
        lo = i / n_buckets
        hi = (i + 1) / n_buckets
        if i < n_buckets - 1:
            preds_in = [p for p in val_predictions if lo <= p < hi]
            labels_in = [
                lab for p, lab in zip(val_predictions, val_labels, strict=True) if lo <= p < hi
            ]
        else:
            preds_in = [p for p in val_predictions if lo <= p <= hi]
            labels_in = [
                lab for p, lab in zip(val_predictions, val_labels, strict=True) if lo <= p <= hi
            ]

        count = len(preds_in)
        if count == 0:
            continue

        mean_pred = statistics.fmean(preds_in)
        mean_actual = statistics.fmean(labels_in)
        buckets.append(
            {
                "lo": lo,
                "hi": hi,
                "mean_pred": mean_pred,
                "mean_actual": mean_actual,
                "count": count,
            }
        )
        ece += (count / total) * abs(mean_pred - mean_actual)

    brier = statistics.fmean(
        (p - lab) ** 2 for p, lab in zip(val_predictions, val_labels, strict=True)
    )

    return {"buckets": buckets, "ece": ece, "brier": brier}
