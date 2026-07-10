"""Tests for sentinel receipt lookup in the gateway (Tier 2d).

Tests that the gateway's `_find_sentinel_receipt()` method correctly
looks up sentinel metrics from the model_metrics table and builds a
SentinelReceipt.
"""

from __future__ import annotations

import time

from quant_foundry.dossier import DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.promotion import PromotionGate
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.runpod_client import MockRunPodClient
from quant_foundry.sentinel import SentinelReceipt
from helpers.product_loop_helpers import (
    _MODEL_ID,
    _dispatch_and_callback,
    _make_engine,
    _make_gateway,
)

# --------------------------------------------------------------------------- #
# Tests: _find_sentinel_receipt                                               #
# --------------------------------------------------------------------------- #


class TestFindSentinelReceipt:
    def test_returns_receipt_when_sentinel_metrics_exist(self, tmp_path) -> None:
        """_find_sentinel_receipt returns a SentinelReceipt when metrics exist."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:sentinel:1")

        # Record sentinel metrics.
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

        receipt = gateway._find_sentinel_receipt(_MODEL_ID)

        assert receipt is not None
        assert isinstance(receipt, SentinelReceipt)
        assert receipt.model_id == _MODEL_ID
        assert receipt.passed is True
        assert receipt.checks_run == ["leakage", "overfit", "stability"]
        assert receipt.pbo == 0.12
        assert receipt.pbo_flagged is False
        engine.dispose()

    def test_returns_none_when_no_sentinel_metrics(self, tmp_path) -> None:
        """_find_sentinel_receipt returns None when no sentinel metrics exist."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:sentinel:2")

        receipt = gateway._find_sentinel_receipt(_MODEL_ID)

        assert receipt is None
        engine.dispose()

    def test_returns_none_when_no_version(self, tmp_path) -> None:
        """_find_sentinel_receipt returns None for an unknown model_id."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)

        receipt = gateway._find_sentinel_receipt("model:nonexistent")

        assert receipt is None
        engine.dispose()

    def test_returns_latest_sentinel_when_multiple(self, tmp_path) -> None:
        """_find_sentinel_receipt returns the most recent sentinel metrics."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:sentinel:3")

        # Record two sentinel metrics — the second one should be returned.
        registry.record_metrics(
            version_id=version_id,
            metric_type="sentinel",
            metrics_dict={
                "model_id": _MODEL_ID,
                "issues": [],
                "passed": True,
                "checks_run": ["leakage"],
                "ts_ns": 1,
                "pbo": 0.12,
                "pbo_flagged": False,
            },
        )
        time.sleep(0.01)  # ensure different timestamp
        registry.record_metrics(
            version_id=version_id,
            metric_type="sentinel",
            metrics_dict={
                "model_id": _MODEL_ID,
                "issues": [],
                "passed": False,  # second one failed
                "checks_run": ["leakage", "overfit", "stability"],
                "ts_ns": 2,
                "pbo": 0.45,
                "pbo_flagged": True,
            },
        )

        receipt = gateway._find_sentinel_receipt(_MODEL_ID)

        assert receipt is not None
        assert receipt.passed is False  # latest one
        assert receipt.pbo == 0.45
        assert receipt.pbo_flagged is True
        engine.dispose()

    def test_returns_receipt_with_issues(self, tmp_path) -> None:
        """_find_sentinel_receipt correctly reconstructs SentinelIssue objects."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:sentinel:4")

        registry.record_metrics(
            version_id=version_id,
            metric_type="sentinel",
            metrics_dict={
                "model_id": _MODEL_ID,
                "issues": [
                    {
                        "code": "FUTURE_LEAK",
                        "severity": "blocking",
                        "message": "leak detected",
                        "detail": {},
                    },
                    {
                        "code": "SHUFFLED_LABEL",
                        "severity": "warning",
                        "message": "edge too high",
                        "detail": {"edge": 0.8},
                    },
                ],
                "passed": False,
                "checks_run": ["leakage", "overfit"],
                "ts_ns": time.time_ns(),
                "pbo": None,
                "pbo_flagged": None,
            },
        )

        receipt = gateway._find_sentinel_receipt(_MODEL_ID)

        assert receipt is not None
        assert receipt.passed is False
        assert len(receipt.issues) == 2
        assert receipt.issues[0].code == "FUTURE_LEAK"
        assert receipt.issues[0].severity == "blocking"
        assert receipt.issues[1].code == "SHUFFLED_LABEL"
        assert receipt.issues[1].severity == "warning"
        engine.dispose()

    def test_returns_none_when_no_db_engine(self, tmp_path) -> None:
        """_find_sentinel_receipt returns None when no DB engine is wired."""
        # Create a gateway without a DB engine (non-DB mode).
        gateway = QuantFoundryGateway(
            enabled=True,
            mode="runpod",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path / "qf-no-db",
            runpod_clients={"training": MockRunPodClient(api_key="test-key")},
        )
        receipt = gateway._find_sentinel_receipt("any-model")
        assert receipt is None


# --------------------------------------------------------------------------- #
# Tests: Integration with submit_promotion                                    #
# --------------------------------------------------------------------------- #


class TestSentinelWiringWithPromotion:
    def test_registry_promote_rejects_when_sentinel_failed(self, tmp_path) -> None:
        """registry.promote() rejects when sentinel metrics show passed=False.

        This is the path the auto-promotion orchestrator uses. The
        registry's _assemble_evidence() queries model_metrics for
        sentinel metrics and builds a SentinelReceipt. If the sentinel
        failed, the gate should reject with SENTINEL_FAILED.
        """
        from quant_foundry.promotion import ReviewDecision

        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:sentinel:5")

        # Record tournament + sentinel metrics (sentinel FAILED).
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
                "settled_count": 50,
            },
        )
        registry.record_metrics(
            version_id=version_id,
            metric_type="sentinel",
            metrics_dict={
                "model_id": _MODEL_ID,
                "issues": [
                    {"code": "FUTURE_LEAK", "severity": "blocking", "message": "leak", "detail": {}}
                ],
                "passed": False,
                "checks_run": ["leakage"],
                "ts_ns": time.time_ns(),
                "pbo": None,
                "pbo_flagged": None,
            },
        )

        receipt = registry.promote(
            version_id=version_id,
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="test with failed sentinel",
            decided_by="test",
        )

        # The gate should reject because sentinel failed.
        assert receipt.decision == ReviewDecision.REJECTED
        assert "sentinel" in receipt.rejection_reason.value.lower()
        engine.dispose()

    def test_registry_promote_approves_when_sentinel_passed(self, tmp_path) -> None:
        """registry.promote() approves when sentinel metrics show passed=True."""
        from quant_foundry.promotion import ReviewDecision

        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:sentinel:6")

        # Record tournament + sentinel metrics (both pass).
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

        receipt = registry.promote(
            version_id=version_id,
            target_status=DossierStatus.RESEARCH_APPROVED,
            review_note="test with passed sentinel",
            decided_by="test",
        )

        # The gate should approve.
        assert receipt.decision == ReviewDecision.APPROVED
        engine.dispose()
