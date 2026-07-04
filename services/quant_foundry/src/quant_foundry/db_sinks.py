"""quant_foundry.db_sinks — DB-backed callback ingestion sinks.

Postgres-backed implementations of the callback ingestion sink protocols.
These mirror the JSONL-backed sinks (``DurableDossierStore``,
``DurableShadowLedgerStore``, ``CallbackInbox``, ``CallbackDLQ``,
``CallbackMetricsStore``) but write to fincept-db (Postgres) via a **sync**
SQLAlchemy engine instead of JSONL files.

Why sync, not async:
  The ``CallbackProcessor`` is sync (it drives outbox/inbox transitions
  synchronously). Making it async would ripple through the whole gateway.
  Instead, the DB-backed sinks use a sync SQLAlchemy engine + sync sessions
  (``get_sync_engine()`` / ``sync_session_scope()`` from
  ``fincept_db.engine``). The cost is a second connection pool — acceptable
  for the first cut (see references/fincept-db-schema.md, option 1).

Idempotency:
  Every sink uses ``INSERT ... ON CONFLICT (key) DO NOTHING`` so a replayed
  callback does not create a second row. The Python-side idempotency in the
  JSONL sinks is defense in depth; the DB layer is the second guard.

Security:
  No sink writes the callback secret, the HMAC signature bytes, or the raw
  payload to the DB. The receipt row stores ``signature_valid: bool`` +
  ``payload_hash`` + ``payload_ref`` (a file path), never the secret, the
  signature, or the raw payload. The shadow_predictions table has a CHECK
  constraint forcing ``authority = 'shadow-only'`` so the DB rejects a
  non-shadow prediction even if Python is bypassed.

Protocols implemented (no interface change to CallbackProcessor):
  - ``DbDossierStore``       -> ``DossierStoreSink``  (store training_result)
  - ``DbShadowLedgerStore``  -> ``ShadowLedgerSink``  (store predictions)
  - ``CallbackReceiptDbStore`` — writes InboxRecord to callback_receipts
  - ``CallbackDlqDbStore``     — writes DLQRecord to callback_dlq
  - ``CallbackMetricsDbStore`` — writes metrics events to callback_metrics
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from fincept_db.callback_tables import (
    ArtifactManifestRow,
    CallbackDlqRow,
    CallbackMetricRow,
    CallbackReceiptRow,
    ModelDossierRow,
    ShadowPredictionRow,
)

from quant_foundry.dossier import DossierRecord
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    ShadowPrediction,
)
from quant_foundry.shadow_ledger import compute_batch_hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dialect_insert(engine: Engine):
    """Return the dialect-specific insert() for the engine.

    Both SQLite and Postgres support ``on_conflict_do_nothing()``. We pick
    the right one based on the engine dialect so the same sink code works
    against SQLite (tests) and Postgres (production).
    """
    name = engine.dialect.name
    if name == "sqlite":
        return sqlite_insert
    return pg_insert


def _on_conflict_do_nothing(
    engine: Engine,
    model: type,
    values: dict[str, Any],
    *,
    conflict_cols: list[str],
) -> Any:
    """Build a dialect-specific INSERT ... ON CONFLICT DO NOTHING statement.

    ``conflict_cols`` are the column(s) whose unique constraint triggers the
    conflict (e.g. ``["content_hash"]`` for model_dossiers).
    """
    insert_fn = _dialect_insert(engine)
    stmt = insert_fn(model).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
    return stmt


# ---------------------------------------------------------------------------
# Dossier store (DossierStoreSink protocol)
# ---------------------------------------------------------------------------


class DbDossierStore:
    """DB-backed ``DossierStoreSink``. Writes DossierRecord to model_dossiers.

    Implements the same ``store(training_result)`` interface as
    ``DurableDossierStore`` so the ``CallbackProcessor`` does not change.
    Validates the nested ``ModelDossier`` + ``ArtifactManifest`` (defense in
    depth), checks ``dossier.artifact_manifest_id == artifact.artifact_id``,
    builds a ``DossierRecord``, and inserts it (plus the artifact manifest
    row) with ``ON CONFLICT DO NOTHING`` for idempotency.

    The artifact manifest is inserted first (it's the FK parent) with its
    own ``ON CONFLICT (artifact_id) DO NOTHING``.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Return the engine (lazy-init from get_sync_engine if not injected)."""
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    def store(self, training_result: dict[str, Any]) -> None:
        """Store a training-result dict. Mirrors DurableDossierStore.store.

        Validates the nested dossier + artifact manifest, builds a
        DossierRecord, and inserts both rows idempotently.
        """
        dossier = ModelDossier.model_validate(training_result["dossier"])
        artifact = ArtifactManifest.model_validate(training_result["artifact_manifest"])
        if dossier.artifact_manifest_id != artifact.artifact_id:
            raise ValueError(
                "dossier artifact_manifest_id does not match artifact manifest artifact_id"
            )

        # Build the DossierRecord (reuses the same logic as DurableDossierStore
        # so the content_hash is identical for the same input).
        training_metrics = dict(dossier.training_metrics)
        if dossier.pbo is not None:
            training_metrics["pbo"] = float(dossier.pbo)
        if dossier.deflated_sharpe is not None:
            training_metrics["deflated_sharpe"] = float(dossier.deflated_sharpe)

        record = DossierRecord(
            model_id=dossier.model_id,
            artifact_manifest_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            dataset_manifest_id=dossier.dataset_manifest_id,
            dataset_manifest_ref=dossier.dataset_manifest_id,
            feature_schema_hash=artifact.feature_schema_hash,
            label_schema_hash=artifact.label_schema_hash,
            code_git_sha=dossier.code_git_sha or artifact.code_git_sha,
            lockfile_hash=dossier.lockfile_hash or artifact.lockfile_hash,
            container_image_digest=(
                dossier.container_image_digest or artifact.container_image_digest
            ),
            random_seed=dossier.random_seed,
            hardware_class=dossier.hardware_class,
            training_metrics=training_metrics,
        )

        engine = self.engine
        with Session(engine) as session:
            # Insert the artifact manifest first (FK parent). Idempotent.
            artifact_stmt = _on_conflict_do_nothing(
                engine,
                ArtifactManifestRow,
                {
                    "schema_version": artifact.schema_version,
                    "artifact_id": artifact.artifact_id,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "uri": artifact.uri,
                    "model_family": artifact.model_family,
                    "created_at_ns": artifact.created_at_ns,
                    "feature_schema_hash": artifact.feature_schema_hash,
                    "label_schema_hash": artifact.label_schema_hash,
                    "code_git_sha": artifact.code_git_sha,
                    "lockfile_hash": artifact.lockfile_hash,
                    "container_image_digest": artifact.container_image_digest,
                },
                conflict_cols=["artifact_id"],
            )
            session.execute(artifact_stmt)

            # Insert the dossier. Idempotent on content_hash.
            registered_at_ns = record.registered_at_ns or time.time_ns()
            dossier_stmt = _on_conflict_do_nothing(
                engine,
                ModelDossierRow,
                {
                    "schema_version": record.schema_version,
                    "model_id": record.model_id,
                    "artifact_manifest_id": record.artifact_manifest_id,
                    "artifact_sha256": record.artifact_sha256,
                    "dataset_manifest_id": record.dataset_manifest_id,
                    "dataset_manifest_ref": record.dataset_manifest_ref,
                    "feature_schema_hash": record.feature_schema_hash,
                    "label_schema_hash": record.label_schema_hash,
                    "code_git_sha": record.code_git_sha,
                    "lockfile_hash": record.lockfile_hash,
                    "container_image_digest": record.container_image_digest,
                    "random_seed": record.random_seed,
                    "hardware_class": record.hardware_class,
                    "trial_count": record.trial_count,
                    "training_metrics": record.training_metrics,
                    "status": record.status.value,
                    "settlement_evidence_refs": list(record.settlement_evidence_refs),
                    "shadow_prediction_refs": list(record.shadow_prediction_refs),
                    "blocking_issues": list(record.blocking_issues),
                    "registered_at_ns": registered_at_ns,
                    "content_hash": record.content_hash,
                },
                conflict_cols=["content_hash"],
            )
            session.execute(dossier_stmt)
            session.commit()

    def get(self, model_id: str) -> dict[str, Any] | None:
        """Return the dossier row for ``model_id``, or None."""
        with Session(self.engine) as session:
            row = session.scalars(
                select(ModelDossierRow).where(ModelDossierRow.model_id == model_id)
            ).first()
            if row is None:
                return None
            return {
                "model_id": row.model_id,
                "content_hash": row.content_hash,
                "status": row.status,
                "artifact_manifest_id": row.artifact_manifest_id,
            }

    def list(self) -> list[dict[str, Any]]:
        """Return all dossier rows (lightweight projection)."""
        with Session(self.engine) as session:
            rows = session.scalars(select(ModelDossierRow)).all()
            return [
                {
                    "model_id": r.model_id,
                    "content_hash": r.content_hash,
                    "status": r.status,
                }
                for r in rows
            ]


# ---------------------------------------------------------------------------
# Shadow ledger store (ShadowLedgerSink protocol)
# ---------------------------------------------------------------------------


class DbShadowLedgerStore:
    """DB-backed ``ShadowLedgerSink``. Writes shadow predictions to DB.

    Implements the same ``store(predictions)`` interface as
    ``DurableShadowLedgerStore`` so the ``CallbackProcessor`` does not
    change. Validates each prediction via ``ShadowPrediction.model_validate``
    (extra='forbid'), asserts ``authority == SHADOW_ONLY`` (defense in depth),
    computes ``batch_hash``, and inserts each prediction with
    ``ON CONFLICT (prediction_id) DO NOTHING`` for idempotency.

    The DB CHECK constraint ``authority = 'shadow-only'`` is a third guard —
    the DB rejects a non-shadow prediction even if Python is bypassed.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    def store(self, predictions: list[dict[str, Any]]) -> None:
        """Store a batch of shadow prediction dicts. Asserts shadow-only."""
        if not predictions:
            return

        batch_hash = compute_batch_hash(predictions)
        received_at_ns = time.time_ns()
        engine = self.engine

        with Session(engine) as session:
            for p in predictions:
                # Defense in depth: validate via the real schema (extra='forbid').
                sp = ShadowPrediction.model_validate(p)
                if sp.authority != Authority.SHADOW_ONLY:
                    raise ValueError(
                        f"non-shadow authority in shadow ledger: {sp.authority} "
                        "(security invariant violation — only shadow-only allowed)"
                    )

                stmt = _on_conflict_do_nothing(
                    engine,
                    ShadowPredictionRow,
                    {
                        "schema_version": sp.schema_version,
                        "prediction_id": sp.prediction_id,
                        "model_id": sp.model_id,
                        "symbol": sp.symbol,
                        "ts_event": sp.ts_event,
                        "horizon_ns": sp.horizon_ns,
                        "direction": sp.direction,
                        "confidence": sp.confidence,
                        "authority": sp.authority.value,
                        "p_up": sp.p_up,
                        "feature_availability": sp.feature_availability,
                        "latency_ms": sp.latency_ms,
                        "batch_hash": batch_hash,
                        "received_at_ns": received_at_ns,
                    },
                    conflict_cols=["prediction_id"],
                )
                session.execute(stmt)
            session.commit()

    def list(self) -> list[dict[str, Any]]:
        """Return all stored shadow predictions (lightweight projection)."""
        with Session(self.engine) as session:
            rows = session.scalars(select(ShadowPredictionRow)).all()
            return [
                {
                    "prediction_id": r.prediction_id,
                    "model_id": r.model_id,
                    "symbol": r.symbol,
                    "authority": r.authority,
                    "batch_hash": r.batch_hash,
                }
                for r in rows
            ]


# ---------------------------------------------------------------------------
# Callback receipt store (InboxRecord -> callback_receipts)
# ---------------------------------------------------------------------------


class CallbackReceiptDbStore:
    """DB-backed callback receipt store. Writes InboxRecord to callback_receipts.

    Mirrors the JSONL ``CallbackInbox`` audit trail but in Postgres. Uses
    ``ON CONFLICT (callback_id) DO NOTHING`` for idempotency — a replayed
    callback does not create a second receipt row.

    This store does NOT enforce signature validity or tamper detection —
    those are the job of the signature layer (``signatures.py``) and the
    inbox (``inbox.py``). This store only persists the receipt after the
    inbox has adjudicated it.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    def write(self, record: Any) -> None:
        """Write an InboxRecord (or dict) to callback_receipts.

        ``record`` can be an ``InboxRecord`` instance or a dict with the
        same keys. Only the safe fields are stored — no secret, no
        signature, no raw payload.
        """
        if hasattr(record, "model_dump"):
            data = record.model_dump(mode="json")
        elif isinstance(record, dict):
            data = dict(record)
        else:
            raise TypeError(f"expected InboxRecord or dict, got {type(record)}")

        engine = self.engine
        with Session(engine) as session:
            stmt = _on_conflict_do_nothing(
                engine,
                CallbackReceiptRow,
                {
                    "schema_version": data.get("schema_version", 1),
                    "callback_id": data["callback_id"],
                    "job_id": data["job_id"],
                    "idempotency_key": data["idempotency_key"],
                    "signature_valid": data["signature_valid"],
                    "payload_hash": data["payload_hash"],
                    "payload_ref": data.get("payload_ref"),
                    "worker_id": data.get("worker_id"),
                    "received_at_ns": data["received_at_ns"],
                    "processed_at_ns": data.get("processed_at_ns"),
                    "status": (
                        data["status"].value
                        if hasattr(data["status"], "value")
                        else data["status"]
                    ),
                    "error_code": data.get("error_code"),
                    "error_summary": data.get("error_summary"),
                    "history": data.get("history", []),
                },
                conflict_cols=["callback_id"],
            )
            session.execute(stmt)
            session.commit()

    def get_by_job_id(self, job_id: str) -> dict[str, Any] | None:
        """Return the latest receipt for ``job_id``, or None."""
        with Session(self.engine) as session:
            row = session.scalars(
                select(CallbackReceiptRow)
                .where(CallbackReceiptRow.job_id == job_id)
                .order_by(CallbackReceiptRow.received_at_ns.desc())
            ).first()
            if row is None:
                return None
            return {
                "callback_id": row.callback_id,
                "job_id": row.job_id,
                "status": row.status,
                "payload_hash": row.payload_hash,
                "signature_valid": row.signature_valid,
            }


# ---------------------------------------------------------------------------
# Callback DLQ store (DLQRecord -> callback_dlq)
# ---------------------------------------------------------------------------


class CallbackDlqDbStore:
    """DB-backed callback DLQ store. Writes DLQRecord to callback_dlq.

    Mirrors the JSONL ``CallbackDLQ`` but in Postgres. Uses
    ``ON CONFLICT (idempotency_key) DO NOTHING`` for idempotency — a
    duplicate rejection does not create a second DLQ row.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    def write(self, record: Any) -> None:
        """Write a DLQRecord (or dict) to callback_dlq."""
        if hasattr(record, "model_dump"):
            data = record.model_dump(mode="json")
        elif isinstance(record, dict):
            data = dict(record)
        else:
            raise TypeError(f"expected DLQRecord or dict, got {type(record)}")

        rejection_reason = data["rejection_reason"]
        if hasattr(rejection_reason, "value"):
            rejection_reason = rejection_reason.value

        engine = self.engine
        with Session(engine) as session:
            stmt = _on_conflict_do_nothing(
                engine,
                CallbackDlqRow,
                {
                    "schema_version": data.get("schema_version", 1),
                    "dlq_id": data["dlq_id"],
                    "callback_id": data.get("callback_id"),
                    "job_id": data["job_id"],
                    "manifest_hash": data["manifest_hash"],
                    "idempotency_key": data["idempotency_key"],
                    "rejection_reason": rejection_reason,
                    "rejection_detail": data["rejection_detail"],
                    "payload_ref": data.get("payload_ref"),
                    "retry_count": data.get("retry_count", 0),
                    "max_retries": data.get("max_retries", 3),
                    "next_retry_at_ns": data.get("next_retry_at_ns"),
                    "backoff_base_seconds": data.get("backoff_base_seconds", 1.0),
                    "is_retryable": data["is_retryable"],
                    "created_at_ns": data["created_at_ns"],
                    "updated_at_ns": data["updated_at_ns"],
                    "history": list(data.get("history", [])),
                },
                conflict_cols=["idempotency_key"],
            )
            session.execute(stmt)
            session.commit()

    def count(self) -> int:
        """Return the number of DLQ rows."""
        from sqlalchemy import func

        with Session(self.engine) as session:
            return session.scalar(
                select(func.count()).select_from(CallbackDlqRow)
            ) or 0


# ---------------------------------------------------------------------------
# Callback metrics store (metrics events -> callback_metrics)
# ---------------------------------------------------------------------------


class CallbackMetricsDbStore:
    """DB-backed callback metrics store. Writes events to callback_metrics.

    Mirrors the JSONL ``CallbackMetricsStore`` but in Postgres. One row per
    event (``received`` / ``accepted`` / ``rejected``). No secrets, no raw
    payload — only ``ts_ns``, ``event``, and an optional ``reason_code``.

    The composite primary key ``(ts_ns, event)`` provides natural idempotency
    for exact-timestamp replays; ``ON CONFLICT DO NOTHING`` is still used so
    a replayed event at the same nanosecond is a no-op.
    """

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    def record(
        self,
        event: str,
        *,
        reason_code: str | None = None,
        ts_ns: int | None = None,
    ) -> None:
        """Append a single metric event to the DB.

        ``event`` must be one of ``received`` / ``accepted`` / ``rejected``.
        ``reason_code`` is an optional short label — never a secret or raw
        payload.
        """
        valid = {"received", "accepted", "rejected"}
        if event not in valid:
            raise ValueError(f"event must be one of {sorted(valid)}; got {event!r}")

        record_ts_ns = int(ts_ns) if ts_ns is not None else time.time_ns()
        engine = self.engine
        with Session(engine) as session:
            stmt = _on_conflict_do_nothing(
                engine,
                CallbackMetricRow,
                {
                    "ts_ns": record_ts_ns,
                    "event": event,
                    "reason_code": reason_code,
                },
                conflict_cols=["ts_ns", "event"],
            )
            session.execute(stmt)
            session.commit()

    def rejection_rate(
        self,
        window_ns: int = 24 * 3600 * 1_000_000_000,
    ) -> float:
        """Return ``rejected / (accepted + rejected)`` over the last window.

        Mirrors ``CallbackMetricsStore.rejection_rate``. ``received`` is
        excluded from the denominator (in-flight callbacks must not dilute
        the rate). Returns ``0.0`` when there are no accepted + rejected
        events in the window.
        """
        from sqlalchemy import func

        if window_ns < 0:
            raise ValueError("window_ns must be non-negative")

        now_ns = time.time_ns()
        cutoff = now_ns - window_ns
        with Session(self.engine) as session:
            accepted = session.scalar(
                select(func.count())
                .select_from(CallbackMetricRow)
                .where(
                    CallbackMetricRow.ts_ns >= cutoff,
                    CallbackMetricRow.event == "accepted",
                )
            ) or 0
            rejected = session.scalar(
                select(func.count())
                .select_from(CallbackMetricRow)
                .where(
                    CallbackMetricRow.ts_ns >= cutoff,
                    CallbackMetricRow.event == "rejected",
                )
            ) or 0

        denom = accepted + rejected
        if denom == 0:
            return 0.0
        return rejected / denom
