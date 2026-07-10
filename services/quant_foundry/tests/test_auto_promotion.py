"""Tests for auto-promotion orchestrator (Tier 2b).

Tests the full auto-promotion workflow:
  1. Find promotion targets (challengers eligible for promotion)
  2. Run champion/challenger comparison
  3. Auto-promote through the gate if comparison passes
  4. Record shadow evaluation + promotion decision

Uses in-memory SQLite with all registry tables, synthetic comparison
inputs, and the same _make_engine + _signed_training_callback helpers
from the e2e product loop tests.
"""

from __future__ import annotations

import time
from typing import Any

from quant_foundry.auto_promotion import (
    AutoPromotionOrchestrator,
    PromotionTarget,
)
from quant_foundry.budget import BudgetGuard
from quant_foundry.champion_challenger import (
    ChampionChallengerConfig,
    ComparisonInput,
)
from quant_foundry.cost_tracker import CostTracker
from quant_foundry.dossier import DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.promotion import (
    PromotionGate,
    ReviewDecision,
)
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.runpod_client import MockRunPodClient
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
)
from quant_foundry.signatures import sign_callback
from sqlalchemy import select
from sqlalchemy.orm import Session

# Re-use the engine + callback helpers from the e2e test module.
# Use the same model_id as the e2e helpers (hardcoded in _signed_training_callback).
from test_e2e_product_loop import _ARTIFACT_ID, _MODEL_ID, _make_engine, _training_payload

from fincept_db.registry_tables import (
    ModelVersionRow,
    ShadowEvaluationRow,
)


def _signed_callback_with_artifact(
    job_id: str,
    *,
    secret: str,
    artifact_id: str,
    sha256: str,
) -> tuple[bytes, str, int]:
    """Build a signed callback with a custom artifact_id + sha256.

    This allows creating multiple versions under the same model
    (the default _signed_training_callback uses a fixed artifact hash
    which causes deduplication).
    """
    artifact = ArtifactManifest(
        artifact_id=artifact_id,
        sha256=sha256,
        size_bytes=2048,
        uri="file:///durable/artifact.zip",
        model_family="gbm",
        created_at_ns=time.time_ns(),
        feature_schema_hash="feature-hash-e2e",
        label_schema_hash="label-hash-e2e",
        code_git_sha="git-sha-e2e",
        lockfile_hash="lock-hash-e2e",
        container_image_digest="sha256:container-digest-e2e",
    )
    dossier = ModelDossier(
        model_id=_MODEL_ID,
        artifact_manifest_id=artifact.artifact_id,
        dataset_manifest_id="dataset:training:e2e",
        code_git_sha="git-sha-e2e",
        lockfile_hash="lock-hash-e2e",
        container_image_digest="sha256:container-digest-e2e",
        random_seed=7,
        hardware_class="runpod-gpu",
        training_metrics={"accuracy": 0.62, "logloss": 0.49},
        pbo=0.12,
        deflated_sharpe=1.1,
        authority=Authority.SHADOW_ONLY,
        metadata={"model_family": "gbm"},
    )
    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-training-e2e",
        result_type="training_complete",
        payload={
            "model_family": "gbm",
            "dossier": dossier.model_dump(mode="json"),
            "artifact_manifest": artifact.model_dump(mode="json"),
        },
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    signature = sign_callback(payload, secret=secret, ts=ts, job_id=job_id)
    return payload, signature, ts


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _dispatch_and_callback(
    gateway: QuantFoundryGateway,
    engine: Any,
    secret: str,
    job_id: str,
    model_id: str = _MODEL_ID,
    artifact_id: str = _ARTIFACT_ID,
    sha256: str = "a" * 64,
) -> str:
    """Dispatch a training job, receive the callback, return version_id.

    Pass a unique ``artifact_id`` + ``sha256`` to create distinct
    versions under the same model.
    """
    gateway.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key=f"idem-{job_id}",
        request_payload=_training_payload(job_id),
    )
    payload, signature, ts = _signed_callback_with_artifact(
        job_id,
        secret=secret,
        artifact_id=artifact_id,
        sha256=sha256,
    )
    gateway.receive_callback(
        job_id=job_id,
        payload=payload,
        signature=signature,
        ts=ts,
        worker_id="test-worker",
    )

    with Session(engine) as session:
        version_row = session.scalars(
            select(ModelVersionRow).where(
                ModelVersionRow.model_id == model_id,
                ModelVersionRow.artifact_id == artifact_id,
            )
        ).first()
        assert version_row is not None, f"no version row for artifact {artifact_id}"
        return version_row.version_id


def _record_tournament_metrics(
    registry: ModelRegistryDB,
    version_id: str,
    settled_count: int = 50,
) -> None:
    """Record tournament metrics for a version."""
    registry.record_metrics(
        version_id=version_id,
        metric_type="tournament",
        metrics_dict={
            "model_id": _MODEL_ID,
            "total_score": 0.72,
            "score_components": [],
            "p_value": 0.03,
            "deflated_sharpe": 1.4,
            "raw_sharpe": 1.8,
            "blocking_issues": [],
            "recommendation": "promote",
            "status": "eligible",
            "trial_count": 1,
            "cost_model_version": "cm-v1",
            "settled_count": settled_count,
        },
    )


def _record_sentinel_metrics(
    registry: ModelRegistryDB,
    version_id: str,
) -> None:
    """Record sentinel metrics (passed=True) for a version."""
    registry.record_metrics(
        version_id=version_id,
        metric_type="sentinel",
        metrics_dict={
            "model_id": _MODEL_ID,
            "issues": [],
            "passed": True,
            "checks_run": ["leakage", "overfit", "stability"],
            "ts_ns": time.time_ns(),
            "pbo": 0.12,
            "pbo_flagged": False,
        },
    )


def _record_c7_metrics(
    registry: ModelRegistryDB,
    version_id: str,
) -> None:
    """Record C7 evidence chain metrics for a version."""
    registry.record_metrics(
        version_id=version_id,
        metric_type="selfcheck",
        metrics_dict={"passed": True, "n_rows_scored": 10, "bundle_sha256": "a" * 64},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="pit_evidence",
        metrics_dict={"verified": True, "evidence_sha256": "e" * 64},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="feature_set",
        metrics_dict={"feature_set_version": "fs-v1"},
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="backend",
        metrics_dict={"production_eligible": True},
    )


def _make_gateway(
    engine: Any,
    secret: str,
    registry: ModelRegistryDB,
    tmp_path: Any,
) -> QuantFoundryGateway:
    """Create a gateway with DB sinks + CostTracker."""
    training_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=25)
    return QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf-auto",
        runpod_clients={"training": training_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        registry=registry,
        budget_guard=BudgetGuard(
            base_dir=tmp_path / "qf-auto" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )


def _synthetic_comparison_provider(
    version_id: str,
    *,
    oos_returns: list[float] | None = None,
    settled_count: int = 50,
) -> ComparisonInput:
    """Return a synthetic ComparisonInput for testing."""
    if oos_returns is None:
        # Generate deterministic returns centered around +1 bps per prediction.
        import random

        rng = random.Random(42)
        oos_returns = [rng.gauss(0.0001, 0.001) for _ in range(settled_count)]
    return ComparisonInput(
        model_id=version_id,
        oos_returns_net=oos_returns,
        trial_count=1,
        settled_count=len(oos_returns),
    )


# --------------------------------------------------------------------------- #
# Tests: PromotionTarget                                                       #
# --------------------------------------------------------------------------- #


class TestPromotionTarget:
    def test_frozen(self) -> None:
        target = PromotionTarget(
            model_id="m1",
            challenger_version_id="v1",
            from_status=DossierStatus.CANDIDATE,
            to_status=DossierStatus.RESEARCH_APPROVED,
        )
        try:
            target.model_id = "hacked"  # type: ignore[misc]
            raise AssertionError("should have raised")
        except Exception:
            pass  # expected

    def test_champion_optional(self) -> None:
        target = PromotionTarget(
            model_id="m1",
            champion_version_id=None,
            challenger_version_id="v1",
            from_status=DossierStatus.CANDIDATE,
            to_status=DossierStatus.RESEARCH_APPROVED,
        )
        assert target.champion_version_id is None


# --------------------------------------------------------------------------- #
# Tests: AutoPromotionOrchestrator â€” target finding                           #
# --------------------------------------------------------------------------- #


class TestFindPromotionTargets:
    def test_finds_challenger_with_no_champion(self, tmp_path) -> None:
        """A single candidate version with no champion is a target."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:auto:1")

        orchestrator = AutoPromotionOrchestrator(registry=registry)
        targets = orchestrator._find_promotion_targets()
        assert len(targets) == 1
        assert targets[0].champion_version_id is None
        assert targets[0].from_status == DossierStatus.CANDIDATE
        assert targets[0].to_status == DossierStatus.RESEARCH_APPROVED
        engine.dispose()

    def test_no_targets_for_paper_approved(self, tmp_path) -> None:
        """A version already at paper_approved has no promotion target."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:auto:2")

        # Manually promote to paper_approved via the gate.
        _record_tournament_metrics(registry, version_id)
        _record_sentinel_metrics(registry, version_id)
        _record_c7_metrics(registry, version_id)
        registry.promote(
            version_id=version_id,
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="manual",
            decided_by="test",
        )
        registry.promote(
            version_id=version_id,
            target_status=DossierStatus.SHADOW_APPROVED,
            review_note="manual",
            decided_by="test",
        )
        registry.promote(
            version_id=version_id,
            target_status=DossierStatus.PAPER_APPROVED,
            review_note="manual",
            decided_by="test",
        )

        orchestrator = AutoPromotionOrchestrator(registry=registry)
        targets = orchestrator._find_promotion_targets()
        assert len(targets) == 0  # paper_approved is the MVP max
        engine.dispose()


# --------------------------------------------------------------------------- #
# Tests: AutoPromotionOrchestrator â€” full run                                 #
# --------------------------------------------------------------------------- #


class TestAutoPromotionRun:
    def test_auto_promotes_first_model(self, tmp_path) -> None:
        """Auto-promote a candidate â†’ research_approved (no champion).

        The orchestrator should:
        1. Find the candidate version as a target.
        2. Skip comparison (no champion).
        3. Call registry.promote() which runs the gate.
        4. The gate should approve (with tournament + sentinel metrics).
        """
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:auto:3")

        # Record evidence so the gate can approve.
        _record_tournament_metrics(registry, version_id)
        _record_sentinel_metrics(registry, version_id)
        _record_c7_metrics(registry, version_id)

        orchestrator = AutoPromotionOrchestrator(
            registry=registry,
            comparison_input_provider=lambda vid: _synthetic_comparison_provider(vid),
        )
        receipt = orchestrator.run()

        assert receipt.total == 1
        assert receipt.promoted_count == 1
        assert receipt.failed_count == 0

        result = receipt.results[0]
        assert result.promoted is True
        assert result.target.champion_version_id is None
        assert result.comparison_decision is None  # no champion, no comparison
        assert result.promotion_receipt is not None
        assert result.promotion_receipt.decision == ReviewDecision.APPROVED

        # Verify the version status changed.
        with Session(engine) as session:
            version = session.scalars(
                select(ModelVersionRow).where(ModelVersionRow.version_id == version_id)
            ).first()
            assert version.status == "research_approved"

        engine.dispose()

    def test_skips_when_no_provider(self, tmp_path) -> None:
        """No comparison_input_provider â†’ all targets skipped."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:auto:4")

        orchestrator = AutoPromotionOrchestrator(registry=registry)
        receipt = orchestrator.run()

        assert receipt.total == 1
        assert receipt.promoted_count == 0
        assert receipt.skipped_count == 1
        assert receipt.results[0].error == "no comparison_input_provider configured"
        engine.dispose()

    def test_skips_when_comparison_rejects(self, tmp_path) -> None:
        """Comparison decision != 'promote' â†’ skipped, no promotion."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:auto:5")

        # Provider returns None â†’ no comparison input â†’ skipped.
        orchestrator = AutoPromotionOrchestrator(
            registry=registry,
            comparison_input_provider=lambda vid: None,
        )
        receipt = orchestrator.run()

        assert receipt.promoted_count == 0
        assert receipt.skipped_count == 1
        engine.dispose()

    def test_receipt_is_frozen(self, tmp_path) -> None:
        """The auto-promotion receipt is immutable."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:auto:6")

        orchestrator = AutoPromotionOrchestrator(registry=registry)
        receipt = orchestrator.run()

        try:
            receipt.promoted_count = 999  # type: ignore[misc]
            raise AssertionError("should have raised")
        except Exception:
            pass  # expected
        engine.dispose()

    def test_empty_registry_no_targets(self, tmp_path) -> None:
        """Empty registry â†’ no targets, empty receipt."""
        engine = _make_engine()
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )

        orchestrator = AutoPromotionOrchestrator(registry=registry)
        receipt = orchestrator.run()

        assert receipt.total == 0
        assert receipt.promoted_count == 0
        assert receipt.results == []
        engine.dispose()


# --------------------------------------------------------------------------- #
# Tests: AutoPromotionOrchestrator â€” with champion                            #
# --------------------------------------------------------------------------- #


class TestAutoPromotionWithChampion:
    def test_champion_challenger_comparison_promotes(self, tmp_path) -> None:
        """Full champion/challenger comparison â†’ auto-promotion.

        Setup:
        1. Dispatch + callback for version 1 (becomes champion).
        2. Promote version 1 to research_approved manually.
        3. Dispatch + callback for version 2 (challenger, candidate).
        4. Record tournament + sentinel metrics for version 2.
        5. Run orchestrator with a provider that gives challenger
           better returns than champion.
        6. Verify version 2 is promoted to research_approved.
        """
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)

        # Version 1 (champion).
        v1_id = _dispatch_and_callback(
            gateway,
            engine,
            secret,
            "qf:auto:champ:1",
            artifact_id="artifact:auto:champ:1",
            sha256="b" * 64,
        )
        _record_tournament_metrics(registry, v1_id)
        _record_sentinel_metrics(registry, v1_id)
        _record_c7_metrics(registry, v1_id)
        registry.promote(
            version_id=v1_id,
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="manual champion promotion",
            decided_by="test",
        )

        # Version 2 (challenger) â€” different artifact hash.
        v2_id = _dispatch_and_callback(
            gateway,
            engine,
            secret,
            "qf:auto:chall:1",
            artifact_id="artifact:auto:chall:1",
            sha256="c" * 64,
        )
        _record_tournament_metrics(registry, v2_id)
        _record_sentinel_metrics(registry, v2_id)
        _record_c7_metrics(registry, v2_id)

        # Provider: champion has mediocre returns, challenger has great returns.
        # Delta must be >= 50 bps (net_edge_threshold).
        # 1 bp = 0.0001. Champion: 1 bp/prediction. Challenger: 60 bps/prediction.
        def provider(vid: str) -> ComparisonInput:
            if vid == v1_id:
                return _synthetic_comparison_provider(
                    vid,
                    oos_returns=[0.0001] * 50,
                    settled_count=50,
                )
            else:
                return _synthetic_comparison_provider(
                    vid,
                    oos_returns=[0.006] * 50,
                    settled_count=50,
                )

        orchestrator = AutoPromotionOrchestrator(
            registry=registry,
            config=ChampionChallengerConfig(
                min_settled_count=30,
                net_edge_threshold=50.0,
                alpha=0.05,
                dsr_threshold=0.0,
            ),
            comparison_input_provider=provider,
        )
        receipt = orchestrator.run()

        # Should have found the challenger and promoted it.
        assert receipt.total >= 1
        # Find the result for the challenger.
        chall_result = next(r for r in receipt.results if r.target.challenger_version_id == v2_id)
        assert chall_result.promoted is True
        assert chall_result.comparison_decision == "promote"
        assert chall_result.shadow_evaluation_id is not None

        # Verify version 2 status changed.
        with Session(engine) as session:
            v2 = session.scalars(
                select(ModelVersionRow).where(ModelVersionRow.version_id == v2_id)
            ).first()
            assert v2.status == "research_approved"

        # Verify shadow_evaluations row was created.
        with Session(engine) as session:
            eval_rows = session.scalars(
                select(ShadowEvaluationRow).where(ShadowEvaluationRow.version_id == v2_id)
            ).all()
            assert len(eval_rows) >= 1

        engine.dispose()
