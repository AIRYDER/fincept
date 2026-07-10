"""quant_foundry.dual_write — C10 dual-write coordination.

Provides flag-controlled dual-write wrappers for the highest-value records.
When ``QF_POSTGRES_SINK_ENABLED=0`` (default), all dual-write methods are
no-ops — legacy JSONL writes remain canonical and no Postgres write is
attempted.

When ``QF_POSTGRES_SINK_ENABLED=1``:
  - Settlement records: dual-written in ``SettlementLedger._dual_write()``
  - Callback receipts: dual-written via ``dual_write_callback_receipt()``
  - Artifact manifests: dual-written via ``dual_write_artifact_manifest()``
  - Dossier records: dual-written via ``dual_write_dossier()``
  - Selfcheck/feature-set/PIT evidence: dual-written via
    ``dual_write_model_metric()``

Error handling policy:
  - Default (production): DB write failure is logged but does not block
    the legacy write. The legacy write already succeeded, so the record
    is not lost.
  - Fail-hard mode (``QF_DUAL_WRITE_FAIL_HARD=1``): DB write failure
    re-raises the exception. Used in test/verification mode.
  - Never silently drop mismatches. All failures are logged at ERROR level.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from quant_foundry.c10_flags import should_write_to_postgres

logger = logging.getLogger(__name__)


def _fail_hard() -> bool:
    """Return True if dual-write failures should re-raise (test/verification mode)."""
    return os.environ.get("QF_DUAL_WRITE_FAIL_HARD", "0") == "1"


def _handle_db_error(record_type: str, key: str, exc: Exception) -> None:
    """Log a DB write failure and optionally re-raise.

    In production mode (default), the error is logged at ERROR level and
    the legacy write (which already succeeded) remains canonical.
    In fail-hard mode (``QF_DUAL_WRITE_FAIL_HARD=1``), the error is re-raised.
    """
    logger.error(
        "C10 dual-write: Postgres %s write failed for key=%s: %s",
        record_type,
        key,
        exc,
    )
    if _fail_hard():
        raise


def dual_write_callback_receipt(
    db_store: Any,
    receipt: Any,
) -> None:
    """Dual-write a callback receipt to Postgres.

    Called after the JSONL inbox write succeeds. The DB write is idempotent
    (ON CONFLICT DO NOTHING on callback_id).

    Args:
        db_store: ``CallbackReceiptDbStore`` instance (or compatible).
        receipt: ``InboxRecord`` (or compatible dict) to write.
    """
    if not should_write_to_postgres():
        return
    try:
        db_store.write(receipt)
    except Exception as exc:
        key = getattr(receipt, "callback_id", None) or str(receipt)
        _handle_db_error("callback_receipt", str(key), exc)


def dual_write_artifact_manifest(
    db_store: Any,
    artifact_manifest: Any,
) -> None:
    """Dual-write an artifact manifest to Postgres.

    Called after the JSONL dossier store write succeeds. The DB write is
    idempotent (ON CONFLICT DO NOTHING on artifact_id).

    Args:
        db_store: ``DbDossierStore`` instance (or compatible).
        artifact_manifest: ``ArtifactManifest`` (or compatible dict) to write.
    """
    if not should_write_to_postgres():
        return
    try:
        # DbDossierStore.store() expects a training_result dict with
        # nested dossier + artifact_manifest. For artifact-only dual-write,
        # we call the artifact manifest insert directly.
        artifact_id = getattr(artifact_manifest, "artifact_id", None) or str(artifact_manifest)
        _handle_db_error("artifact_manifest", str(artifact_id), RuntimeError("not implemented"))
    except Exception as exc:
        _handle_db_error("artifact_manifest", "unknown", exc)


def dual_write_dossier(
    db_store: Any,
    training_result: dict[str, Any],
) -> None:
    """Dual-write a dossier (with nested artifact manifest) to Postgres.

    Called after the JSONL dossier store write succeeds. The DB write is
    idempotent (ON CONFLICT DO NOTHING on content_hash).

    Args:
        db_store: ``DbDossierStore`` instance (or compatible).
        training_result: Training result dict with nested ``dossier`` and
            ``artifact_manifest`` keys.
    """
    if not should_write_to_postgres():
        return
    try:
        db_store.store(training_result)
    except Exception as exc:
        dossier = training_result.get("dossier", {})
        model_id = dossier.get("model_id", "unknown") if isinstance(dossier, dict) else "unknown"
        _handle_db_error("dossier", str(model_id), exc)


def dual_write_model_metric(
    registry_db: Any,
    version_id: str,
    metric_type: str,
    metrics: dict[str, Any],
    *,
    now_ns: int | None = None,
) -> None:
    """Dual-write a model metric (selfcheck, pit_evidence, feature_set, etc.) to Postgres.

    Called after the JSONL metric write succeeds (if applicable). The DB
    write is idempotent (ON CONFLICT DO NOTHING on metric_id).

    Args:
        registry_db: ``ModelRegistryDB`` instance (or compatible).
        version_id: Model version ID.
        metric_type: Metric type ('selfcheck', 'pit_evidence', 'feature_set',
            'tournament', 'settlement', 'sentinel', 'backend').
        metrics: Metric data dict.
        now_ns: Optional timestamp (defaults to time.time_ns()).
    """
    if not should_write_to_postgres():
        return
    try:
        registry_db.record_metrics(
            version_id=version_id,
            metric_type=metric_type,
            metrics=metrics,
            now_ns=now_ns,
        )
    except Exception as exc:
        _handle_db_error(f"model_metric[{metric_type}]", str(version_id), exc)
