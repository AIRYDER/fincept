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

from helpers.product_loop_helpers import (
    _MODEL_ID,
    _dispatch_and_callback,
    _make_engine,
    _make_gateway,
)
from quant_foundry.auto_promotion import (
    AutoPromotionOrchestrator,
    PromotionTarget,
)
from quant_foundry.champion_challenger import (
    ChampionChallengerConfig,
    ComparisonInput,
)
from quant_foundry.dossier import DossierStatus
from quant_foundry.promotion import (
    PromotionGate,
    ReviewDecision,
)
from quant_foundry.registry_db import ModelRegistryDB
from sqlalchemy import select
from sqlalchemy.orm import Session

from fincept_db.registry_tables import (
    ModelVersionRow,
    ShadowEvaluationRow,
)


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
# Tests: AutoPromotionOrchestrator — target finding                           #
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
# Tests: AutoPromotionOrchestrator — full run                                 #
# --------------------------------------------------------------------------- #


class TestAutoPromotionRun:
    def test_auto_promotes_first_model(self, tmp_path) -> None:
        """Auto-promote a candidate → research_approved (no champion).

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
        """No comparison_input_provider → all targets skipped."""
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
        """Comparison decision != 'promote' → skipped, no promotion."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:auto:5")

        # Provider returns None → no comparison input → skipped.
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
        """Empty registry → no targets, empty receipt."""
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
# Tests: AutoPromotionOrchestrator — with champion                            #
# --------------------------------------------------------------------------- #


class TestAutoPromotionWithChampion:
    def test_champion_challenger_comparison_promotes(self, tmp_path) -> None:
        """Full champion/challenger comparison → auto-promotion.

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
        registry.promote(
            version_id=v1_id,
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="manual champion promotion",
            decided_by="test",
        )

        # Version 2 (challenger) — different artifact hash.
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
