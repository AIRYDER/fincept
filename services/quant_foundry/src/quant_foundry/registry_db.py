"""quant_foundry.registry_db — DB-backed model registry with promotion workflow.

The ``ModelRegistryDB`` is the durable, Postgres-backed home for model identity,
versions, metrics, promotion decisions, and shadow evaluations. It mirrors the
JSONL-backed ``DossierRegistry`` (TASK-0403) but writes to fincept-db via a
**sync** SQLAlchemy engine instead of JSONL files.

Why sync, not async:
  The ``PromotionGate.evaluate`` is sync, and the promotion workflow is a
  synchronous request-response cycle (assemble evidence → evaluate → persist
  receipt → update status). The DB-backed registry uses a sync SQLAlchemy
  engine + sync sessions (``sync_session_scope`` from ``fincept_db.engine``).

CRITICAL — the registry persists, the gate enforces:
  The registry's ``promote()`` method does NOT duplicate PromotionGate logic.
  It:
    1. Queries the registry tables to assemble ``PromotionEvidence``.
    2. Calls ``PromotionGate.evaluate(request, evidence)``.
    3. Persists the resulting ``PromotionReceipt`` into ``promotion_decisions``.
    4. Only if approved: updates ``model_versions.status`` and
       ``models.current_status``.
    5. Always persists the ``promotions`` row (approved or rejected) so the
       audit trail is complete.

Security:
  No column stores the callback secret, the HMAC signature bytes, or the raw
  payload. The ``promotion_decisions`` row stores ``review_note`` +
  ``rejection_reason`` + ``waivers`` (a JSON list of
  ``{issue_code, waived_by, reason}`` dicts), never secrets.

Idempotency:
  ``register_model`` and ``register_version`` use
  ``INSERT ... ON CONFLICT DO NOTHING`` so a replayed registration does not
  create a second row.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from fincept_db.callback_tables import ModelDossierRow
from fincept_db.registry_tables import (
    ModelMetricRow,
    ModelRow,
    ModelVersionRow,
    PromotionDecisionRow,
    PromotionRow,
    ShadowEvaluationRow,
)
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.promotion import (
    BlockingIssue,
    PromotionEvidence,
    PromotionGate,
    PromotionReceipt,
    PromotionRequest,
    PromotionWaiver,
    ReviewDecision,
)
from quant_foundry.sentinel import SentinelReceipt, SentinelSeverity
from quant_foundry.tournament import TournamentResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dialect_insert(engine: Engine) -> Callable[..., Any]:
    """Return the dialect-specific insert() for the engine."""
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
    """Build a dialect-specific INSERT ... ON CONFLICT DO NOTHING statement."""
    insert_fn = _dialect_insert(engine)
    stmt = insert_fn(model).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=conflict_cols)
    return stmt


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an ORM row to a dict of column-name -> value."""
    return {c: getattr(row, c) for c in row.__table__.columns.keys()}


# ---------------------------------------------------------------------------
# ModelRegistryDB
# ---------------------------------------------------------------------------


class ModelRegistryDB:
    """DB-backed model registry with promotion workflow.

    Uses sync sessions (``sync_session_scope`` from ``fincept_db.engine``).
    All write methods are idempotent via ``ON CONFLICT DO NOTHING``.

    The ``promote()`` method assembles ``PromotionEvidence`` from the registry
    tables, calls ``PromotionGate.evaluate(...)``, persists the receipt, and
    only then updates status (if approved). The gate enforces; the registry
    persists.
    """

    def __init__(
        self,
        engine: Engine | None = None,
        gate: PromotionGate | None = None,
    ) -> None:
        self._engine = engine
        self._gate = gate or PromotionGate()

    @property
    def engine(self) -> Engine:
        """Return the engine (lazy-init from get_sync_engine if not injected)."""
        if self._engine is None:
            from fincept_db.engine import get_sync_engine

            self._engine = get_sync_engine()
        return self._engine

    @property
    def gate(self) -> PromotionGate:
        """Return the promotion gate."""
        return self._gate

    # ------------------------------------------------------------------
    # Model registration
    # ------------------------------------------------------------------

    def register_model(
        self,
        model_id: str,
        name: str,
        model_family: str,
        description: str | None = None,
    ) -> dict[str, Any] | None:
        """Register a model row. Idempotent via ON CONFLICT DO NOTHING.

        Returns the model row as a dict, or None if the insert was a no-op
        (model_id already exists).
        """
        now_ns = time.time_ns()
        engine = self.engine
        with Session(engine) as session:
            stmt = _on_conflict_do_nothing(
                engine,
                ModelRow,
                {
                    "model_id": model_id,
                    "name": name,
                    "model_family": model_family,
                    "created_at_ns": now_ns,
                    "current_version_id": None,
                    "current_status": DossierStatus.CANDIDATE.value,
                    "description": description,
                },
                conflict_cols=["model_id"],
            )
            result = session.execute(stmt)
            session.commit()
            # ON CONFLICT DO NOTHING returns no row; check if inserted.
            if result.rowcount == 0:  # type: ignore[attr-defined]  # CursorResult has rowcount but Result type stubs don't expose it
                return None
            return {
                "model_id": model_id,
                "name": name,
                "model_family": model_family,
                "created_at_ns": now_ns,
                "current_version_id": None,
                "current_status": DossierStatus.CANDIDATE.value,
                "description": description,
            }

    # ------------------------------------------------------------------
    # Version registration
    # ------------------------------------------------------------------

    def register_version(
        self,
        model_id: str,
        version_id: str,
        dossier_content_hash: str,
        artifact_id: str,
        callback_receipt_id: str,
        version_number: int,
    ) -> dict[str, Any] | None:
        """Register a version row. Idempotent via ON CONFLICT DO NOTHING.

        Returns the version row as a dict, or None if the insert was a no-op
        (version_id already exists).
        """
        now_ns = time.time_ns()
        engine = self.engine
        with Session(engine) as session:
            stmt = _on_conflict_do_nothing(
                engine,
                ModelVersionRow,
                {
                    "version_id": version_id,
                    "model_id": model_id,
                    "dossier_content_hash": dossier_content_hash,
                    "artifact_id": artifact_id,
                    "callback_receipt_id": callback_receipt_id,
                    "version_number": version_number,
                    "status": DossierStatus.CANDIDATE.value,
                    "created_at_ns": now_ns,
                    "promoted_at_ns": None,
                },
                conflict_cols=["version_id"],
            )
            result = session.execute(stmt)
            session.commit()
            if result.rowcount == 0:  # type: ignore[attr-defined]  # CursorResult has rowcount but Result type stubs don't expose it
                return None
            return {
                "version_id": version_id,
                "model_id": model_id,
                "dossier_content_hash": dossier_content_hash,
                "artifact_id": artifact_id,
                "callback_receipt_id": callback_receipt_id,
                "version_number": version_number,
                "status": DossierStatus.CANDIDATE.value,
                "created_at_ns": now_ns,
                "promoted_at_ns": None,
            }

    # ------------------------------------------------------------------
    # Metrics recording
    # ------------------------------------------------------------------

    def record_metrics(
        self,
        version_id: str,
        metric_type: str,
        metrics_dict: dict[str, Any],
    ) -> str:
        """Write a metrics row. Returns the metric_id.

        ``metric_type`` must be one of ``training``, ``tournament``,
        ``sentinel``, ``settlement`` (enforced by DB CHECK constraint).
        """
        valid_types = {"training", "tournament", "sentinel", "settlement"}
        if metric_type not in valid_types:
            raise ValueError(
                f"metric_type must be one of {sorted(valid_types)}; got {metric_type!r}"
            )
        metric_id = f"metric:{version_id}:{metric_type}:{time.time_ns()}"
        now_ns = time.time_ns()
        engine = self.engine
        with Session(engine) as session:
            row = ModelMetricRow(
                metric_id=metric_id,
                version_id=version_id,
                metric_type=metric_type,
                metrics=metrics_dict,
                recorded_at_ns=now_ns,
            )
            session.add(row)
            session.commit()
            return metric_id

    # ------------------------------------------------------------------
    # Shadow evaluation recording
    # ------------------------------------------------------------------

    def record_shadow_evaluation(
        self,
        version_id: str,
        settled_count: int,
        evaluation_metrics: dict[str, Any],
        tournament_result_id: str | None = None,
    ) -> str:
        """Write a shadow evaluation row. Returns the evaluation_id."""
        if settled_count < 0:
            raise ValueError("settled_count must be >= 0")
        evaluation_id = f"eval:{version_id}:{time.time_ns()}"
        now_ns = time.time_ns()
        engine = self.engine
        with Session(engine) as session:
            row = ShadowEvaluationRow(
                evaluation_id=evaluation_id,
                version_id=version_id,
                settled_count=settled_count,
                evaluation_metrics=evaluation_metrics,
                evaluated_at_ns=now_ns,
                tournament_result_id=tournament_result_id,
            )
            session.add(row)
            session.commit()
            return evaluation_id

    def run_shadow_comparison(
        self,
        champion_version_id: str,
        challenger_version_id: str,
        champion_input: Any,
        challenger_input: Any,
        config: Any,
    ) -> tuple[str, Any]:
        """Run a champion/challenger shadow comparison and record it.

        Tier 2.4: compares a challenger model's settled shadow
        performance against the champion's, records the result in the
        ``shadow_evaluations`` table, and returns the decision.

        Args:
            champion_version_id: the champion's model version ID.
            challenger_version_id: the challenger's model version ID.
            champion_input: ``ComparisonInput`` for the champion.
            challenger_input: ``ComparisonInput`` for the challenger.
            config: ``ChampionChallengerConfig`` for the comparison.

        Returns:
            ``(evaluation_id, promotion_decision)`` where
            ``promotion_decision`` is a ``PromotionDecision``.
        """
        from quant_foundry.champion_challenger import compare_champion_challenger

        decision = compare_champion_challenger(
            champion_input,
            challenger_input,
            config,
        )

        # Record the shadow evaluation for the challenger
        evaluation_metrics: dict[str, Any] = {
            "decision": decision.decision,
            "reason": decision.reason,
            "champion_model_id": decision.result.champion_model_id,
            "challenger_model_id": decision.result.challenger_model_id,
            "champion_settled_count": decision.result.champion_settled_count,
            "challenger_settled_count": decision.result.challenger_settled_count,
            "champion_net_edge_bps": decision.result.champion_net_edge_bps,
            "challenger_net_edge_bps": decision.result.challenger_net_edge_bps,
            "net_edge_delta_bps": decision.result.net_edge_delta_bps,
            "champion_dsr": decision.result.champion_dsr,
            "challenger_dsr": decision.result.challenger_dsr,
            "dsr_delta": decision.result.dsr_delta,
            "bootstrap_p_value": decision.result.bootstrap_p_value,
            "brier_delta": decision.result.brier_delta,
        }

        evaluation_id = self.record_shadow_evaluation(
            version_id=challenger_version_id,
            settled_count=challenger_input.settled_count,
            evaluation_metrics=evaluation_metrics,
        )

        return evaluation_id, decision

    # ------------------------------------------------------------------
    # Promotion workflow
    # ------------------------------------------------------------------

    def promote(
        self,
        version_id: str,
        target_status: DossierStatus,
        review_note: str,
        decided_by: str,
        waivers: list[PromotionWaiver] | None = None,
    ) -> PromotionReceipt:
        """Promote a version through the gate.

        This method:
          1. Queries the registry tables to assemble ``PromotionEvidence``.
          2. Calls ``PromotionGate.evaluate(request, evidence)``.
          3. Persists the ``promotions`` row (always — approved or rejected).
          4. Persists the ``PromotionReceipt`` into ``promotion_decisions``.
          5. Only if approved: updates ``model_versions.status`` and
             ``models.current_status``.

        If the gate rejects, status does NOT change but the rejection receipt
        IS persisted (audit trail).

        Returns the ``PromotionReceipt`` from the gate.
        """
        waivers = waivers or []
        engine = self.engine
        now_ns = time.time_ns()

        # --- 1. Query the version row to get from_status + model_id ---
        with Session(engine) as session:
            version_row = session.scalars(
                select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
            ).first()
            if version_row is None:
                raise KeyError(f"unknown version_id: {version_id}")
            from_status = DossierStatus(version_row.status)
            model_id = version_row.model_id

        # --- 2. Assemble PromotionEvidence from the registry tables ---
        evidence = self._assemble_evidence(version_id)

        # --- 3. Build the PromotionRequest ---
        request = PromotionRequest(
            model_id=model_id,
            target_level=target_status,
            review_note=review_note,
            waivers=waivers,
        )

        # --- 4. Call the gate (the gate enforces; the registry persists) ---
        receipt = self._gate.evaluate(request=request, evidence=evidence)

        # --- 5. Persist the promotions row (always — audit trail) ---
        promotion_id = f"promo:{version_id}:{now_ns}"
        decided_at_ns = receipt.decided_at_ns or time.time_ns()
        with Session(engine) as session:
            promo_row = PromotionRow(
                promotion_id=promotion_id,
                version_id=version_id,
                from_status=from_status.value,
                to_status=target_status.value,
                requested_at_ns=now_ns,
                decided_at_ns=decided_at_ns,
                decision=receipt.decision.value,
            )
            session.add(promo_row)
            session.flush()  # ensure FK is visible

            # --- 6. Persist the PromotionReceipt into promotion_decisions ---
            decision_id = f"decision:{promotion_id}"
            waivers_json = [w.model_dump(mode="json") for w in waivers]
            decision_row = PromotionDecisionRow(
                decision_id=decision_id,
                promotion_id=promotion_id,
                decision=receipt.decision.value,
                review_note=review_note,
                rejection_reason=(
                    receipt.rejection_reason.value if receipt.rejection_reason else None
                ),
                waivers=waivers_json,
                decided_at_ns=decided_at_ns,
                decided_by=decided_by,
            )
            session.add(decision_row)

            # --- 7. Only if approved: update status ---
            if receipt.decision == ReviewDecision.APPROVED:
                session.execute(
                    sqlalchemy_update_status(
                        ModelVersionRow,
                        version_id,
                        target_status.value,
                        decided_at_ns,
                    )
                )
                session.execute(
                    sqlalchemy_update_model_status(
                        ModelRow,
                        model_id,
                        version_id,
                        target_status.value,
                    )
                )

            session.commit()

        return receipt

    def _assemble_evidence(self, version_id: str) -> PromotionEvidence:
        """Assemble PromotionEvidence from the registry tables.

        Queries:
          - ``model_dossiers`` (via the version's ``dossier_content_hash``)
            to build a ``DossierRecord``.
          - ``model_metrics`` (metric_type='tournament') to build a
            ``TournamentResult``.
          - ``model_metrics`` (metric_type='sentinel') to build a
            ``SentinelReceipt``.
          - ``model_dossiers.blocking_issues`` for the blocking issues list.

        If the dossier is missing, evidence.dossier is None (the gate will
        reject with NO_DOSSIER). If tournament/sentinel metrics are missing,
        those evidence fields are None (the gate will reject with
        INSUFFICIENT_EVIDENCE / SENTINEL_FAILED).
        """
        engine = self.engine

        with Session(engine) as session:
            # Get the version row to find the dossier_content_hash.
            version_row = session.scalars(
                select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
            ).first()
            if version_row is None:
                return PromotionEvidence()

            dossier_hash = version_row.dossier_content_hash

            # Query the dossier row.
            dossier_row = session.scalars(
                select(ModelDossierRow).where(ModelDossierRow.content_hash == dossier_hash)
            ).first()

            # Build DossierRecord from the dossier row.
            dossier: DossierRecord | None = None
            if dossier_row is not None:
                dossier = _build_dossier_record(dossier_row)

            # Query tournament metrics.
            tournament_metrics_row = session.scalars(
                select(ModelMetricRow)
                .where(
                    ModelMetricRow.version_id == version_id,
                    ModelMetricRow.metric_type == "tournament",
                )
                .order_by(ModelMetricRow.recorded_at_ns.desc())
            ).first()

            tournament_result: TournamentResult | None = None
            if tournament_metrics_row is not None:
                tournament_result = _build_tournament_result(tournament_metrics_row.metrics)

            # Query sentinel metrics.
            sentinel_metrics_row = session.scalars(
                select(ModelMetricRow)
                .where(
                    ModelMetricRow.version_id == version_id,
                    ModelMetricRow.metric_type == "sentinel",
                )
                .order_by(ModelMetricRow.recorded_at_ns.desc())
            ).first()

            sentinel_receipt: SentinelReceipt | None = None
            if sentinel_metrics_row is not None:
                sentinel_receipt = _build_sentinel_receipt(sentinel_metrics_row.metrics)

            # Build blocking issues from the dossier's blocking_issues list.
            blocking_issues: list[BlockingIssue] = []
            if dossier_row is not None and dossier_row.blocking_issues:
                for issue_dict in dossier_row.blocking_issues:
                    code = issue_dict.get("code", issue_dict.get("source", "unknown"))
                    message = issue_dict.get("note", issue_dict.get("message", ""))
                    severity_str = issue_dict.get("severity", "blocking")
                    try:
                        severity = SentinelSeverity(severity_str)
                    except ValueError:
                        severity = SentinelSeverity.BLOCKING
                    blocking_issues.append(
                        BlockingIssue(
                            code=code,
                            severity=severity,
                            message=message,
                        )
                    )

        return PromotionEvidence(
            dossier=dossier,
            tournament_result=tournament_result,
            sentinel_receipt=sentinel_receipt,
            blocking_issues=blocking_issues,
        )

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        """Return the model row as a dict, or None."""
        with Session(self.engine) as session:
            row = session.scalars(select(ModelRow).where(ModelRow.model_id == model_id)).first()
            if row is None:
                return None
            return _row_to_dict(row)

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        """Return the version row as a dict, or None."""
        with Session(self.engine) as session:
            row = session.scalars(
                select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
            ).first()
            if row is None:
                return None
            return _row_to_dict(row)

    def list_models(self, status: DossierStatus | str | None = None) -> list[dict[str, Any]]:
        """List models, optionally filtered by status."""
        with Session(self.engine) as session:
            stmt = select(ModelRow)
            if status is not None:
                status_val = status.value if hasattr(status, "value") else str(status)
                stmt = stmt.where(ModelRow.current_status == status_val)
            rows = session.scalars(stmt).all()
            return [_row_to_dict(r) for r in rows]

    def list_versions(self, model_id: str) -> list[dict[str, Any]]:
        """List versions for a model_id."""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ModelVersionRow)
                .where(ModelVersionRow.model_id == model_id)
                .order_by(ModelVersionRow.version_number.asc())
            ).all()
            return [_row_to_dict(r) for r in rows]

    def get_promotion_history(self, version_id: str) -> list[dict[str, Any]]:
        """List all promotion attempts for a version (with decisions)."""
        with Session(self.engine) as session:
            promo_rows = session.scalars(
                select(PromotionRow)
                .where(PromotionRow.version_id == version_id)
                .order_by(PromotionRow.requested_at_ns.asc())
            ).all()
            result: list[dict[str, Any]] = []
            for promo in promo_rows:
                promo_dict = _row_to_dict(promo)
                # Attach the decision receipt.
                decision_row = session.scalars(
                    select(PromotionDecisionRow).where(
                        PromotionDecisionRow.promotion_id == promo.promotion_id
                    )
                ).first()
                if decision_row is not None:
                    promo_dict["decision_receipt"] = _row_to_dict(decision_row)
                result.append(promo_dict)
            return result


# ---------------------------------------------------------------------------
# Evidence assembly helpers
# ---------------------------------------------------------------------------


def _build_dossier_record(row: ModelDossierRow) -> DossierRecord:
    """Build a DossierRecord from a ModelDossierRow."""
    return DossierRecord(
        schema_version=row.schema_version,
        model_id=row.model_id,
        artifact_manifest_id=row.artifact_manifest_id,
        artifact_sha256=row.artifact_sha256,
        dataset_manifest_id=row.dataset_manifest_id,
        dataset_manifest_ref=row.dataset_manifest_ref,
        feature_schema_hash=row.feature_schema_hash,
        label_schema_hash=row.label_schema_hash,
        code_git_sha=row.code_git_sha,
        lockfile_hash=row.lockfile_hash,
        container_image_digest=row.container_image_digest,
        random_seed=row.random_seed,
        hardware_class=row.hardware_class,
        trial_count=row.trial_count,
        training_metrics=row.training_metrics,
        status=DossierStatus(row.status),
        settlement_evidence_refs=row.settlement_evidence_refs,
        shadow_prediction_refs=row.shadow_prediction_refs,
        blocking_issues=row.blocking_issues,
        registered_at_ns=row.registered_at_ns,
    )


def _build_tournament_result(metrics: dict[str, Any]) -> TournamentResult:
    """Build a TournamentResult from a metrics dict.

    The metrics dict is the JSONB stored in model_metrics.metrics for a
    ``tournament`` metric_type row. It should contain the fields needed to
    construct a TournamentResult (at minimum: model_id, total_score,
    settled_count).
    """
    from quant_foundry.tournament import (
        PromotionRecommendation,
        ScoreComponent,
        TournamentStatus,
    )

    score_components = [ScoreComponent(**c) for c in metrics.get("score_components", [])]
    return TournamentResult(
        model_id=metrics.get("model_id", ""),
        total_score=metrics.get("total_score", 0.0),
        score_components=score_components,
        p_value=metrics.get("p_value"),
        deflated_sharpe=metrics.get("deflated_sharpe"),
        raw_sharpe=metrics.get("raw_sharpe"),
        blocking_issues=metrics.get("blocking_issues", []),
        recommendation=PromotionRecommendation(metrics.get("recommendation", "hold")),
        status=TournamentStatus(metrics.get("status", "eligible")),
        trial_count=metrics.get("trial_count", 1),
        cost_model_version=metrics.get("cost_model_version", "cm-v1"),
        settled_count=metrics.get("settled_count", 0),
    )


def _build_sentinel_receipt(metrics: dict[str, Any]) -> SentinelReceipt:
    """Build a SentinelReceipt from a metrics dict.

    The metrics dict is the JSONB stored in model_metrics.metrics for a
    ``sentinel`` metric_type row. It should contain the fields needed to
    construct a SentinelReceipt (at minimum: model_id, passed).
    """
    from quant_foundry.sentinel import SentinelIssue

    issues = [SentinelIssue(**i) for i in metrics.get("issues", [])]
    return SentinelReceipt(
        model_id=metrics.get("model_id", ""),
        issues=issues,
        passed=metrics.get("passed", True),
        checks_run=metrics.get("checks_run", []),
        ts_ns=metrics.get("ts_ns", 0),
        pbo=metrics.get("pbo"),
        pbo_flagged=metrics.get("pbo_flagged"),
    )


# ---------------------------------------------------------------------------
# SQL update helpers (avoid circular import with sqlalchemy.orm)
# ---------------------------------------------------------------------------


def sqlalchemy_update_status(
    model: Any,
    version_id: str,
    status: str,
    promoted_at_ns: int,
) -> Any:
    """Build an UPDATE statement for model_versions.status."""
    from sqlalchemy import update

    return (
        update(model)
        .where(model.version_id == version_id)
        .values(status=status, promoted_at_ns=promoted_at_ns)
    )


def sqlalchemy_update_model_status(
    model: Any,
    model_id: str,
    version_id: str,
    status: str,
) -> Any:
    """Build an UPDATE statement for models.current_status + current_version_id."""
    from sqlalchemy import update

    return (
        update(model)
        .where(model.model_id == model_id)
        .values(current_status=status, current_version_id=version_id)
    )
