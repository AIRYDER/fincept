"""Public facade for the ``fincept_core.datasets`` package.

This module re-exports the stable public surface of the ML dataset
evidence spine so that callers can write a single import line::

    from fincept_core.datasets import (
        ApprovedRoots,
        DatasetManifest,
        SettlementStore,
        FeatureSnapshotStore,
        build_evidence_receipt,
    )

The spine is built incrementally by the ``ml-dataset-evidence-spine``
plan.  Todos 1-4 land ``approved_roots``, ``schemas``, ``settlement`` and
``feature_snapshot``; todo 17 lands the cross-validation utility
(``cv``).  The CV names are imported here *today* (guarded by a
``try/except ImportError``) so callers get a stable facade from day one
-- the names are bound to ``None`` until todo 17 creates ``cv.py``, at
which point the real symbols replace the ``None`` placeholders without
any caller change.

Design constraints (from the plan, todo 5):

  * No import from ``services/quant_foundry`` (would create a circular
    dependency and violate the layering rule).
  * No star-imports -- every re-export is explicit so the public surface
    is auditable.
  * ``PredictionRow`` is imported from ``fincept_core.prediction_log``
    (same package, no circular dep) for the ``build_evidence_receipt``
    helper.
"""

from __future__ import annotations

from typing import Any

from fincept_core.prediction_log import PredictionRow

from .approved_roots import ApprovedRoots, ApprovedRootsError, default_approved_roots
from .dossier import build_calibration_sidecar, build_dossier
from .feature_snapshot import FeatureSnapshotStore

# Phase 3 / T-2.2: manifest-first dataset loader with hash verification.
from .manifest_loader import (
    ColumnRoles,
    DatasetLoadError,
    DatasetLoadReceipt,
    LoadedDataset,
    ManifestDatasetLoader,
)
from .schema_compat import (
    SchemaCompatResult,
    SchemaIncompatibilityError,
    assert_feature_schema_compatible,
    check_feature_schema_compatibility,
)
from .schemas import (
    ArtifactManifest,
    DatasetManifest,
    FeatureRow,
    FeatureSnapshot,
)
from .settlement import (
    DEFAULT_COST_MODEL_VERSION,
    SettlementError,
    SettlementRecord,
    SettlementStore,
)

# --------------------------------------------------------------------------- #
# Cross-validation utility (todo 17)                                          #
# --------------------------------------------------------------------------- #
# ``cv.py`` is implemented by todo 17.  We attempt the import here so
# the facade is stable: if ``cv.py`` ever fails to import (e.g. a
# transient Pydantic version mismatch) the names are bound to ``None``
# rather than breaking the whole ``datasets`` package.  Under normal
# operation the real symbols replace the ``None`` placeholders.
try:
    from .cv import (
        CPCVFold,
        Fold,
        WalkForwardWindow,
        derive_walk_forward_window,
        fold_iter_to_dicts,
        make_cpcv_folds,
        make_folds,
    )
except ImportError:  # pragma: no cover - safety net for cv.py import errors
    CPCVFold = None  # type: ignore[assignment,misc]
    Fold = None  # type: ignore[assignment,misc]
    WalkForwardWindow = None  # type: ignore[assignment,misc]
    derive_walk_forward_window = None  # type: ignore[assignment]
    fold_iter_to_dicts = None  # type: ignore[assignment]
    make_cpcv_folds = None  # type: ignore[assignment]
    make_folds = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Labeling (triple-barrier + meta-labeling, Tier 2.3)                         #
# --------------------------------------------------------------------------- #
try:
    from .labels import (
        BarrierConfig,
        MetaLabelConfig,
        TripleBarrierLabel,
        meta_labels,
        triple_barrier_labels,
        volatility_scaled_widths,
    )
except ImportError:  # pragma: no cover - safety net
    BarrierConfig = None  # type: ignore[assignment,misc]
    MetaLabelConfig = None  # type: ignore[assignment,misc]
    TripleBarrierLabel = None  # type: ignore[assignment,misc]
    meta_labels = None  # type: ignore[assignment]
    triple_barrier_labels = None  # type: ignore[assignment]
    volatility_scaled_widths = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Evidence-receipt helper                                                     #
# --------------------------------------------------------------------------- #


def build_evidence_receipt(
    *,
    prediction: PredictionRow,
    settlement: SettlementRecord | None,
    feature_snapshot: FeatureSnapshot | None,
    feature_health: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Join a prediction + its settlement into a JSON-safe receipt dict.

    This is the shape consumed by ``GET /models/{name}/outcomes``: a
    flat dict that a dashboard can render without a second round-trip.
    When ``settlement`` is ``None`` (horizon not yet elapsed, or the
    settlement worker has not caught up) the settlement fields are
    filled with ``None`` and ``settlement_status`` is ``"pending_time"``
    so the UI can show "awaiting outcome" deterministically.

    When ``feature_snapshot`` is not ``None`` the receipt includes the
    snapshot's ``feature_schema_hash`` so a consumer can verify the
    feature schema the prediction was made against.  When
    ``feature_health`` is not ``None`` it is included verbatim under the
    ``"feature_health"`` key.
    """
    receipt: dict[str, Any] = {
        "prediction_id": prediction.id,
        "agent_id": prediction.agent_id,
        "model_name": prediction.model_name,
        "ts_event": prediction.ts_event,
        "horizon_ns": prediction.horizon_ns,
        "symbol": prediction.symbol,
        "direction": prediction.direction,
        "confidence": prediction.confidence,
    }

    if settlement is not None:
        receipt["settlement_status"] = settlement.status
        receipt["realized_return_gross"] = settlement.realized_return_gross
        receipt["realized_return_net"] = settlement.realized_return_net
        receipt["settled_at_ns"] = settlement.settled_at_ns
        receipt["brier_component"] = settlement.brier_component
    else:
        receipt["settlement_status"] = "pending_time"
        receipt["realized_return_gross"] = None
        receipt["realized_return_net"] = None
        receipt["settled_at_ns"] = None
        receipt["brier_component"] = None

    if feature_snapshot is not None:
        receipt["feature_schema_hash"] = feature_snapshot.feature_schema_hash

    if feature_health is not None:
        receipt["feature_health"] = feature_health

    return receipt


__all__ = [
    "DEFAULT_COST_MODEL_VERSION",
    "ApprovedRoots",
    "ApprovedRootsError",
    "ArtifactManifest",
    "BarrierConfig",
    "CPCVFold",
    "ColumnRoles",
    "DatasetLoadError",
    "DatasetLoadReceipt",
    "DatasetManifest",
    "FeatureRow",
    "FeatureSnapshot",
    "FeatureSnapshotStore",
    "Fold",
    "LoadedDataset",
    "ManifestDatasetLoader",
    "MetaLabelConfig",
    "SchemaCompatResult",
    "SchemaIncompatibilityError",
    "SettlementError",
    "SettlementRecord",
    "SettlementStore",
    "TripleBarrierLabel",
    "WalkForwardWindow",
    "assert_feature_schema_compatible",
    "build_calibration_sidecar",
    "build_dossier",
    "build_evidence_receipt",
    "check_feature_schema_compatibility",
    "default_approved_roots",
    "derive_walk_forward_window",
    "fold_iter_to_dicts",
    "make_cpcv_folds",
    "make_folds",
    "meta_labels",
    "triple_barrier_labels",
    "volatility_scaled_widths",
]
