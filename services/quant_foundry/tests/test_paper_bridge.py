"""
Tests for TASK-0704: Build Paper-Only Model Pointer Bridge.

TDD red phase — these tests are written BEFORE the implementation and must
fail with ModuleNotFoundError / ImportError until `paper_bridge.py` exists.

Acceptance criteria covered:
- Bridge is disabled by default.
- Bridge refuses non-paper runtime.
- Bridge refuses models without evidence packet.
- Rollback pointer exists.
- Risk/OMS boundaries remain unchanged.

Additional checks from the spec:
- Require paper-approved model status.
- Require explicit config: QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true.
- Convert shadow prediction to existing Prediction schema.
- Publish only in paper mode.
- Store rollback pointer before enabling.
- Add circuit breaker for bad predictions or missing evidence.
- Keep OMS and risk authoritative.

File-disjoint from libs/fincept-core/, libs/fincept-bus/,
services/orchestrator/, services/risk/, services/oms/ (other builders'
files). Imports from my promotion.py (TASK-0702), dossier.py (TASK-0403),
schemas.py (read-only).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.paper_bridge import (
    BridgeCircuitBreaker,
    BridgeConfig,
    BridgeStatus,
    PaperBridge,
    PaperPrediction,
    RollbackPointer,
    convert_shadow_to_paper,
)
from quant_foundry.promotion import (
    PromotionEvidence,
)
from quant_foundry.sentinel import SentinelReceipt
from quant_foundry.tournament import (
    PromotionRecommendation,
    TournamentResult,
    TournamentStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_shadow_prediction(
    prediction_id: str = "pred-1",
    model_id: str = "m1",
    symbol: str = "AAPL",
) -> dict[str, Any]:
    return {
        "prediction_id": prediction_id,
        "model_id": model_id,
        "symbol": symbol,
        "ts_event": 1000,
        "horizon_ns": 3600_000_000_000,
        "direction": 0.5,
        "confidence": 0.7,
        "authority": "shadow-only",
        "p_up": 0.7,
    }


def _make_dossier(
    model_id: str = "m1",
    status: DossierStatus = DossierStatus.PAPER_APPROVED,
) -> DossierRecord:
    return DossierRecord(
        model_id=model_id,
        artifact_sha256="a" * 64,
        artifact_manifest_id="manifest-1",
        dataset_manifest_id="ds-1",
        code_git_sha="gitsha",
        lockfile_hash="lockhash",
        container_image_digest="digest",
        feature_schema_hash="f" * 64,
        label_schema_hash="l" * 64,
        status=status,
        trial_count=1,
    )


def _make_evidence(
    model_id: str = "m1",
    dossier_status: DossierStatus = DossierStatus.PAPER_APPROVED,
) -> PromotionEvidence:
    return PromotionEvidence(
        dossier=_make_dossier(model_id=model_id, status=dossier_status),
        tournament_result=TournamentResult(
            model_id=model_id,
            total_score=0.8,
            settled_count=100,
            status=TournamentStatus.ELIGIBLE,
            recommendation=PromotionRecommendation.PROMOTE,
        ),
        sentinel_receipt=SentinelReceipt(
            model_id=model_id,
            issues=[],
            passed=True,
            checks_run=["shuffled_label"],
            ts_ns=1000,
        ),
        blocking_issues=[],
    )


# ---------------------------------------------------------------------------
# BridgeConfig
# ===========================================================================


class TestBridgeConfig:
    """The bridge configuration."""

    def test_config_has_required_fields(self) -> None:
        """Config has allow_paper_bridge, runtime_mode."""
        config = BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        )
        assert config.allow_paper_bridge is True
        assert config.runtime_mode == "paper"

    def test_config_defaults_to_disabled(self) -> None:
        """Config defaults to disabled (allow_paper_bridge=False)."""
        config = BridgeConfig()
        assert config.allow_paper_bridge is False

    def test_config_is_frozen(self) -> None:
        """Config is frozen."""
        config = BridgeConfig()
        with pytest.raises((TypeError, ValueError)):
            config.allow_paper_bridge = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Bridge is disabled by default
# ===========================================================================


class TestBridgeDisabledByDefault:
    """Bridge is disabled by default."""

    def test_bridge_disabled_by_default(self) -> None:
        """The bridge is disabled by default (no env var set)."""
        # Ensure the env var is not set.
        old = os.environ.pop("QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE", None)
        try:
            bridge = PaperBridge()
            assert bridge.status == BridgeStatus.DISABLED
        finally:
            if old is not None:
                os.environ["QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE"] = old

    def test_bridge_enabled_with_env_var(self) -> None:
        """The bridge is enabled when QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE=true."""
        old = os.environ.get("QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE")
        try:
            os.environ["QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE"] = "true"
            bridge = PaperBridge(
                config=BridgeConfig(
                    allow_paper_bridge=True,
                    runtime_mode="paper",
                )
            )
            assert bridge.status == BridgeStatus.ENABLED
        finally:
            if old is not None:
                os.environ["QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE"] = old
            else:
                os.environ.pop("QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE", None)


# ---------------------------------------------------------------------------
# Bridge refuses non-paper runtime
# ===========================================================================


class TestBridgeRefusesNonPaper:
    """Bridge refuses non-paper runtime."""

    def test_bridge_refuses_live_runtime(self) -> None:
        """The bridge refuses to publish in live runtime."""
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="live",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(),
        )
        assert result.status == BridgeStatus.REFUSED
        assert "non-paper" in result.reason.lower() or "runtime" in result.reason.lower()

    def test_bridge_accepts_paper_runtime(self) -> None:
        """The bridge accepts paper runtime."""
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(),
        )
        assert result.status == BridgeStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Bridge refuses models without evidence packet
# ===========================================================================


class TestBridgeRefusesNoEvidence:
    """Bridge refuses models without evidence packet."""

    def test_bridge_refuses_without_evidence(self) -> None:
        """The bridge refuses to publish without an evidence packet."""
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=None,
        )
        assert result.status == BridgeStatus.REFUSED
        assert "evidence" in result.reason.lower()

    def test_bridge_refuses_without_dossier(self) -> None:
        """The bridge refuses to publish without a dossier in the evidence."""
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=PromotionEvidence(dossier=None),
        )
        assert result.status == BridgeStatus.REFUSED

    def test_bridge_refuses_non_paper_approved_model(self) -> None:
        """The bridge refuses a model that is not paper-approved."""
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(dossier_status=DossierStatus.SHADOW_APPROVED),
        )
        assert result.status == BridgeStatus.REFUSED
        assert "paper" in result.reason.lower() or "approved" in result.reason.lower()


# ---------------------------------------------------------------------------
# Rollback pointer exists
# ===========================================================================


class TestRollbackPointer:
    """Rollback pointer exists before enabling."""

    def test_rollback_pointer_has_required_fields(self) -> None:
        """RollbackPointer has model_id, pointer_id, created_at_ns."""
        ptr = RollbackPointer(
            model_id="m1",
            pointer_id="ptr-1",
            created_at_ns=1000,
            reason="paper bridge enable",
        )
        assert ptr.model_id == "m1"
        assert ptr.pointer_id == "ptr-1"
        assert ptr.created_at_ns == 1000
        assert ptr.reason == "paper bridge enable"

    def test_rollback_pointer_is_frozen(self) -> None:
        """RollbackPointer is frozen."""
        ptr = RollbackPointer(
            model_id="m1",
            pointer_id="ptr-1",
            created_at_ns=1000,
            reason="test",
        )
        with pytest.raises((TypeError, ValueError)):
            ptr.reason = "changed"  # type: ignore[misc]

    def test_bridge_creates_rollback_pointer_before_publishing(self) -> None:
        """The bridge creates a rollback pointer before publishing."""
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(),
        )
        assert result.status == BridgeStatus.PUBLISHED
        assert result.rollback_pointer is not None
        assert result.rollback_pointer.model_id == "m1"


# ---------------------------------------------------------------------------
# Risk/OMS boundaries remain unchanged
# ===========================================================================


class TestRiskOMSBoundaries:
    """Risk/OMS boundaries remain unchanged."""

    def test_paper_prediction_has_no_order_fields(self) -> None:
        """The converted PaperPrediction has no order/OMS fields."""
        pred = convert_shadow_to_paper(_make_shadow_prediction())
        order_keys = {
            "order",
            "signal",
            "trade",
            "position",
            "allocation",
            "quantity",
            "side",
            "sig_predict",
        }
        for key in order_keys:
            assert not hasattr(pred, key)

    def test_paper_prediction_has_prediction_fields(self) -> None:
        """The converted PaperPrediction has prediction fields."""
        pred = convert_shadow_to_paper(_make_shadow_prediction())
        assert pred.prediction_id == "pred-1"
        assert pred.model_id == "m1"
        assert pred.symbol == "AAPL"

    def test_paper_prediction_is_frozen(self) -> None:
        """The PaperPrediction is frozen."""
        pred = convert_shadow_to_paper(_make_shadow_prediction())
        with pytest.raises((TypeError, ValueError)):
            pred.model_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Bridge receipt
# ===========================================================================


class TestBridgeReceipt:
    """The bridge receipt records what was published."""

    def test_receipt_has_required_fields(self) -> None:
        """Receipt has status, reason, prediction, rollback_pointer."""
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(),
        )
        assert hasattr(result, "status")
        assert hasattr(result, "reason")
        assert hasattr(result, "rollback_pointer")

    def test_receipt_to_dict_is_json_serializable(self) -> None:
        """The receipt can be serialized to JSON."""
        import json

        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(),
        )
        d = result.to_dict()
        json.dumps(d)
        assert "status" in d
        assert "reason" in d


# ---------------------------------------------------------------------------
# Circuit breaker
# ===========================================================================


class TestCircuitBreaker:
    """Circuit breaker for bad predictions or missing evidence."""

    def test_circuit_breaker_trips_on_failures(self) -> None:
        """The circuit breaker trips after too many failures."""
        breaker = BridgeCircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert not breaker.is_tripped()
        breaker.record_failure()
        assert breaker.is_tripped()

    def test_circuit_breaker_resets(self) -> None:
        """The circuit breaker can be reset."""
        breaker = BridgeCircuitBreaker(failure_threshold=2)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_tripped()
        breaker.reset()
        assert not breaker.is_tripped()

    def test_bridge_refuses_when_circuit_breaker_tripped(self) -> None:
        """The bridge refuses when the circuit breaker is tripped."""
        breaker = BridgeCircuitBreaker(failure_threshold=1)
        breaker.record_failure()
        bridge = PaperBridge(
            config=BridgeConfig(allow_paper_bridge=True, runtime_mode="paper"),
            circuit_breaker=breaker,
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(),
        )
        assert result.status == BridgeStatus.REFUSED
        assert "circuit" in result.reason.lower() or "breaker" in result.reason.lower()


# ---------------------------------------------------------------------------
# Convenience function
# ===========================================================================


class TestConvertShadowToPaper:
    """The convenience function convert_shadow_to_paper works."""

    def test_convert_returns_paper_prediction(self) -> None:
        """convert_shadow_to_paper returns a PaperPrediction."""
        pred = convert_shadow_to_paper(_make_shadow_prediction())
        assert isinstance(pred, PaperPrediction)

    def test_convert_preserves_fields(self) -> None:
        """convert_shadow_to_paper preserves key fields."""
        pred = convert_shadow_to_paper(_make_shadow_prediction())
        assert pred.prediction_id == "pred-1"
        assert pred.model_id == "m1"
        assert pred.symbol == "AAPL"
        assert pred.ts_event == 1000


# ---------------------------------------------------------------------------
# No secrets in output
# ===========================================================================


class TestNoSecretsInPaperBridge:
    """Paper bridge output must not leak secrets."""

    def test_receipt_to_dict_has_no_secret_keys(self) -> None:

        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            )
        )
        result = bridge.publish(
            prediction=_make_shadow_prediction(),
            evidence=_make_evidence(),
        )
        d = result.to_dict()

        def _has_secret(d: Any, secret_names: set[str]) -> bool:
            if isinstance(d, dict):
                for k, v in d.items():
                    if k.lower() in secret_names:
                        return True
                    if _has_secret(v, secret_names):
                        return True
            elif isinstance(d, list):
                for item in d:
                    if _has_secret(item, secret_names):
                        return True
            return False

        secret_names = {"api_key", "token", "secret", "password", "broker_account", "credential"}
        assert not _has_secret(d, secret_names)
