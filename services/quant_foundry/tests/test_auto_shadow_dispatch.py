"""Tests for auto shadow dispatcher (Tier 2f).

Tests the AutoShadowDispatcher that automatically dispatches shadow
inference for research_approved versions that have no shadow
predictions yet.
"""

from __future__ import annotations

import time
from typing import Any

from helpers.product_loop_helpers import _MODEL_ID, _dispatch_and_callback, _make_engine
from quant_foundry.auto_shadow_dispatch import (
    AutoShadowDispatcher,
)
from quant_foundry.budget import BudgetGuard
from quant_foundry.cost_tracker import CostTracker
from quant_foundry.dossier import DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.promotion import PromotionGate
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.runpod_client import MockRunPodClient
from quant_foundry.schemas import Authority
from quant_foundry.shadow_ledger import ShadowLedgerRecord


def _make_shadow_gateway(
    engine: Any,
    secret: str,
    registry: ModelRegistryDB,
    tmp_path: Any,
) -> QuantFoundryGateway:
    """Create a gateway with both training + inference RunPod clients."""
    training_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=25)
    inference_client = MockRunPodClient(api_key="test-key", cost_per_dispatch_cents=10)
    return QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf-shadow",
        runpod_clients={"training": training_client, "inference": inference_client},
        cost_tracker=CostTracker(engine=engine),
        sink_backend="db",
        db_engine=engine,
        registry=registry,
        budget_guard=BudgetGuard(
            base_dir=tmp_path / "qf-shadow" / "budget",
            monthly_budget_cents=1_000_000,
        ),
    )


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_shadow_ledger_record(
    *,
    model_id: str = _MODEL_ID,
    prediction_id: str = "pred-1",
) -> ShadowLedgerRecord:
    """Create a synthetic ShadowLedgerRecord for testing."""
    return ShadowLedgerRecord(
        prediction_id=prediction_id,
        model_id=model_id,
        symbol="AAPL",
        ts_event=time.time_ns(),
        horizon_ns=86_400_000_000_000,
        direction=1.0,
        confidence=0.6,
        authority=Authority.SHADOW_ONLY,
        batch_hash="batch-test",
        stored_at_ns=time.time_ns(),
    )


class _FakeShadowLedger:
    """In-memory shadow ledger for testing."""

    def __init__(self, records: list[ShadowLedgerRecord] | None = None) -> None:
        self._records = records or []

    def read_by_model(self, model_id: str) -> list[ShadowLedgerRecord]:
        return [r for r in self._records if r.model_id == model_id]

    def add(self, record: ShadowLedgerRecord) -> None:
        self._records.append(record)


# --------------------------------------------------------------------------- #
# Tests: AutoShadowDispatcher                                                 #
# --------------------------------------------------------------------------- #


def _record_evidence_and_promote(
    registry: ModelRegistryDB,
    version_id: str,
    target: DossierStatus = DossierStatus.RESEARCH_APPROVED,
) -> None:
    """Record tournament + sentinel metrics, then promote through the gate."""
    registry.record_metrics(
        version_id=version_id,
        metric_type="tournament",
        metrics_dict={
            "model_id": _MODEL_ID,
            "total_score": 0.85,
            "score_components": [],
            "p_value": 0.01,
            "deflated_sharpe": 2.1,
            "raw_sharpe": 2.5,
            "blocking_issues": [],
            "recommendation": "promote",
            "status": "eligible",
            "trial_count": 1,
            "cost_model_version": "cm-v1",
            "settled_count": 50,
        },
    )
    registry.record_metrics(
        version_id=version_id,
        metric_type="sentinel",
        metrics_dict={
            "model_id": _MODEL_ID,
            "issues": [],
            "passed": True,
            "checks_run": ["leakage"],
            "ts_ns": time.time_ns(),
            "pbo": 0.12,
            "pbo_flagged": False,
        },
    )
    registry.promote(
        version_id=version_id,
        target_status=target,
        review_note="manual promotion for shadow test",
        decided_by="test",
    )


class TestAutoShadowDispatcher:
    def test_dispatches_for_research_approved_version(self, tmp_path) -> None:
        """Dispatcher dispatches shadow inference for a research_approved version."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:shadow:1")

        # Record evidence + promote to research_approved.
        _record_evidence_and_promote(registry, version_id)

        # No shadow predictions exist yet.
        shadow_ledger = _FakeShadowLedger()

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        assert receipt.dispatched == 1
        assert receipt.skipped == 0
        assert receipt.errored == 0
        assert receipt.total == 1

        result = receipt.results[0]
        assert result.version_id == version_id
        assert result.model_id == _MODEL_ID
        assert result.dispatched is True
        assert result.job_id.startswith("shadow-inference-")
        engine.dispose()

    def test_skips_version_with_existing_shadow_predictions(self, tmp_path) -> None:
        """Dispatcher skips versions that already have shadow predictions."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:shadow:2")

        _record_evidence_and_promote(registry, version_id)

        # Shadow predictions already exist.
        shadow_ledger = _FakeShadowLedger()
        shadow_ledger.add(_make_shadow_ledger_record(model_id=_MODEL_ID))

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        # No versions to dispatch — already has shadow predictions.
        assert receipt.dispatched == 0
        assert receipt.total == 0
        engine.dispose()

    def test_skips_candidate_versions(self, tmp_path) -> None:
        """Dispatcher does not dispatch for candidate (not research_approved) versions."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:shadow:3")
        # Version is still candidate — not promoted.

        shadow_ledger = _FakeShadowLedger()

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        assert receipt.dispatched == 0
        assert receipt.total == 0  # no eligible versions
        engine.dispose()

    def test_skips_shadow_approved_versions(self, tmp_path) -> None:
        """Dispatcher does not dispatch for shadow_approved versions (already past shadow)."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:shadow:4")

        # Promote past research_approved to shadow_approved.
        _record_evidence_and_promote(registry, version_id, DossierStatus.RESEARCH_APPROVED)
        _record_evidence_and_promote(registry, version_id, DossierStatus.SHADOW_APPROVED)

        shadow_ledger = _FakeShadowLedger()

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        # shadow_approved is not the target status (research_approved is).
        assert receipt.dispatched == 0
        assert receipt.total == 0
        engine.dispose()

    def test_dispatches_for_multiple_versions(self, tmp_path) -> None:
        """Dispatcher dispatches for multiple eligible versions."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)

        # Create two versions under different models.
        v1 = _dispatch_and_callback(
            gateway,
            engine,
            secret,
            "qf:shadow:multi:1",
            artifact_id="artifact:shadow:1",
            sha256="h" * 64,
        )
        v2 = _dispatch_and_callback(
            gateway,
            engine,
            secret,
            "qf:shadow:multi:2",
            artifact_id="artifact:shadow:2",
            sha256="i" * 64,
        )

        # Both versions share the same model_id (_MODEL_ID).
        # Promote both to research_approved.
        _record_evidence_and_promote(registry, v1)
        _record_evidence_and_promote(registry, v2)

        shadow_ledger = _FakeShadowLedger()

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        # Both versions are research_approved with no shadow predictions.
        assert receipt.dispatched == 2
        assert receipt.total == 2
        engine.dispose()

    def test_empty_registry_no_dispatches(self, tmp_path) -> None:
        """Empty registry → no dispatches."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)

        shadow_ledger = _FakeShadowLedger()

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        assert receipt.dispatched == 0
        assert receipt.total == 0
        engine.dispose()

    def test_receipt_is_frozen(self, tmp_path) -> None:
        """Receipt is immutable (frozen pydantic model)."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)

        shadow_ledger = _FakeShadowLedger()

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        try:
            receipt.dispatched = 999
            raise AssertionError("should have raised")
        except Exception:
            pass  # expected — frozen model
        engine.dispose()

    def test_dispatched_job_is_in_outbox(self, tmp_path) -> None:
        """The dispatched job appears in the gateway's outbox."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:shadow:5")

        _record_evidence_and_promote(registry, version_id)

        shadow_ledger = _FakeShadowLedger()

        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        receipt = dispatcher.run()

        assert receipt.dispatched == 1
        job_id = receipt.results[0].job_id

        # Verify the job is in the gateway's outbox.
        job_detail = gateway.get_job(job_id)
        assert job_detail is not None
        assert job_detail["job_type"] == "inference"
        engine.dispose()


# --------------------------------------------------------------------------- #
# Tests: Integration — full automated product loop                            #
# --------------------------------------------------------------------------- #


class TestFullAutomatedProductLoop:
    def test_full_loop_dispatch_to_shadow_dispatch(self, tmp_path) -> None:
        """Full automated product loop: dispatch → callback → promote → shadow dispatch.

        1. Dispatch training + callback → version registered.
        2. Record tournament + sentinel metrics.
        3. Auto-promote to research_approved.
        4. Auto-shadow-dispatch dispatches shadow inference for the version.
        5. Verify the shadow inference job is in the outbox.
        """
        from helpers.product_loop_helpers import _FakeSettlementLedger, _make_settlement_record
        from quant_foundry.auto_promotion import AutoPromotionOrchestrator
        from quant_foundry.champion_challenger import ChampionChallengerConfig
        from quant_foundry.settlement_provider import SettledComparisonInputProvider

        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_shadow_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:shadow:e2e:1")

        # Record tournament + sentinel metrics.
        registry.record_metrics(
            version_id=version_id,
            metric_type="tournament",
            metrics_dict={
                "model_id": _MODEL_ID,
                "total_score": 0.85,
                "score_components": [],
                "p_value": 0.01,
                "deflated_sharpe": 2.1,
                "raw_sharpe": 2.5,
                "blocking_issues": [],
                "recommendation": "promote",
                "status": "eligible",
                "trial_count": 1,
                "cost_model_version": "cm-v1",
                "settled_count": 50,
            },
        )
        registry.record_metrics(
            version_id=version_id,
            metric_type="sentinel",
            metrics_dict={
                "model_id": _MODEL_ID,
                "issues": [],
                "passed": True,
                "checks_run": ["leakage"],
                "ts_ns": time.time_ns(),
                "pbo": 0.12,
                "pbo_flagged": False,
            },
        )
        # Auto-promote: candidate → research_approved.
        settlement_ledger = _FakeSettlementLedger()
        import random

        rng = random.Random(42)
        for i in range(50):
            settlement_ledger.add(
                _make_settlement_record(
                    prediction_id=f"pred-{i}",
                    realized_return_net=0.006 + rng.gauss(0, 0.002),
                    brier=0.20,
                )
            )

        provider = SettledComparisonInputProvider(
            registry=registry,
            settlement_ledger=settlement_ledger,
            min_settled_count=30,
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
        promo_receipt = orchestrator.run()
        assert any(r.promoted for r in promo_receipt.results)

        # Auto-shadow-dispatch: research_approved → shadow inference job.
        shadow_ledger = _FakeShadowLedger()
        dispatcher = AutoShadowDispatcher(
            gateway=gateway,
            registry=registry,
            shadow_ledger=shadow_ledger,
        )
        shadow_receipt = dispatcher.run()

        assert shadow_receipt.dispatched == 1
        assert shadow_receipt.total == 1

        # Verify the version is now research_approved.
        versions = registry.list_versions(_MODEL_ID)
        assert any(v["status"] == DossierStatus.RESEARCH_APPROVED.value for v in versions)

        # Verify the shadow inference job is in the outbox.
        job_id = shadow_receipt.results[0].job_id
        job_detail = gateway.get_job(job_id)
        assert job_detail is not None
        assert job_detail["job_type"] == "inference"
        engine.dispose()
