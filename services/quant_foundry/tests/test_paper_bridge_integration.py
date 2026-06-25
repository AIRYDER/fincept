"""
End-to-end integration tests for the paper bridge (TASK-0704).

Tests the full flow: shadow prediction -> settlement -> tournament ->
promotion -> paper bridge publish.

Covers:
- Full promotion flow: create dossier -> settle predictions -> run
  tournament -> submit promotion -> approve -> verify receipt is APPROVED.
- Paper bridge publish with an approved model: verify BridgeReceipt
  status=PUBLISHED, rollback pointer exists, PaperPrediction has no
  order fields.
- Circuit breaker: trip it, verify it blocks, reset it, verify it works.
- Paper bridge refuses non-paper runtime.
- Paper bridge refuses without evidence packet.
- Paper bridge refuses without paper_approved status.
- No secrets in any bridge output.

Uses fixture data and injectable adapters (same pattern as
test_gateway_settlement.py and test_gateway_tournament.py).
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

import pytest

from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.market_data_adapter import BarDataAdapter, PricePoint
from quant_foundry.outcomes import SettlementRecord, SettlementStatus
from quant_foundry.paper_bridge import (
    BridgeCircuitBreaker,
    BridgeConfig,
    BridgeReceipt,
    BridgeStatus,
    PaperBridge,
    PaperPrediction,
    RollbackPointer,
    convert_shadow_to_paper,
)
from quant_foundry.promotion import (
    PromotionEvidence,
    PromotionReceipt,
    PromotionRejectionReason,
    ReviewDecision,
)
from quant_foundry.sentinel import SentinelReceipt
from quant_foundry.settlement_sweep import SettlementSweep, default_cost_model
from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash
from quant_foundry.tournament import (
    PromotionRecommendation,
    TournamentResult,
    TournamentStatus,
)


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

_T_EVENT = 1_000_000_000_000_000_000
_HORIZON_NS = 60_000_000_000
_WINDOW_END = _T_EVENT + _HORIZON_NS


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_prediction(
    *,
    prediction_id: str = "pred-1",
    model_id: str = "bridge-model-1",
    symbol: str = "AAPL",
    ts_event: int = _T_EVENT,
    horizon_ns: int = _HORIZON_NS,
    direction: float = 1.0,
    confidence: float = 0.7,
    p_up: float = 0.7,
) -> dict[str, Any]:
    return {
        "prediction_id": prediction_id,
        "model_id": model_id,
        "symbol": symbol,
        "ts_event": ts_event,
        "horizon_ns": horizon_ns,
        "direction": direction,
        "confidence": confidence,
        "p_up": p_up,
        "authority": "shadow-only",
    }


def _make_bar_reader(bars: dict[str, list[PricePoint]]):
    def reader(symbol: str, start_ns: int, end_ns: int) -> list[PricePoint]:
        return [p for p in bars.get(symbol, []) if start_ns <= p.ts_ns < end_ns]
    return reader


def _make_gateway(
    tmp_path: pathlib.Path,
    bars: dict[str, list[PricePoint]] | None = None,
) -> QuantFoundryGateway:
    gw = QuantFoundryGateway(
        enabled=True,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-secret",
        base_dir=tmp_path,
    )
    adapter = BarDataAdapter(
        bar_reader=_make_bar_reader(bars or {}),
        benchmark_symbol="SPY",
    )
    sweep = SettlementSweep(
        shadow_ledger=gw.shadow_ledger_real(),
        settlement_ledger=gw.settlement_ledger(),
        market_data_adapter=adapter,
        cost_model=default_cost_model(),
    )
    gw._settlement_sweep = sweep
    return gw


def _store_predictions(
    gw: QuantFoundryGateway, predictions: list[dict[str, Any]]
) -> None:
    batch_hash = compute_batch_hash(predictions)
    gw.shadow_ledger_real().store_batch(predictions=predictions, batch_hash=batch_hash)


def _make_dossier(
    model_id: str = "bridge-model-1",
    status: DossierStatus = DossierStatus.PAPER_APPROVED,
    trial_count: int = 3,
) -> DossierRecord:
    return DossierRecord(
        model_id=model_id,
        artifact_manifest_id=f"artifact-{model_id}",
        artifact_sha256=f"sha256-{model_id}",
        dataset_manifest_id="dataset-test",
        feature_schema_hash="fs-hash",
        label_schema_hash="ls-hash",
        trial_count=trial_count,
        status=status,
    )


def _make_tournament_result(
    model_id: str = "bridge-model-1",
    settled_count: int = 100,
) -> TournamentResult:
    return TournamentResult(
        model_id=model_id,
        total_score=0.8,
        settled_count=settled_count,
        status=TournamentStatus.ELIGIBLE,
        recommendation=PromotionRecommendation.PROMOTE,
    )


def _make_sentinel_receipt(model_id: str = "bridge-model-1") -> SentinelReceipt:
    return SentinelReceipt(
        model_id=model_id,
        issues=[],
        passed=True,
        checks_run=["shuffled_label"],
        ts_ns=1000,
    )


def _make_evidence(
    model_id: str = "bridge-model-1",
    dossier_status: DossierStatus = DossierStatus.PAPER_APPROVED,
) -> PromotionEvidence:
    return PromotionEvidence(
        dossier=_make_dossier(model_id=model_id, status=dossier_status),
        tournament_result=_make_tournament_result(model_id=model_id),
        sentinel_receipt=_make_sentinel_receipt(model_id=model_id),
        blocking_issues=[],
    )


def _write_settlements(
    base_dir: pathlib.Path, model_id: str, count: int
) -> None:
    ledger_dir = base_dir / "settlements"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / f"{model_id}.settlements.jsonl"
    now_ns = time.time_ns()
    bucket_pairs = [
        ("very_low", 0.001),
        ("very_low", 0.001),
        ("low", 0.002),
        ("low", 0.002),
        ("medium", 0.003),
        ("medium", 0.003),
        ("medium", 0.003),
        ("high", 0.004),
        ("high", 0.004),
        ("very_high", 0.005),
        ("very_high", 0.005),
        ("very_high", 0.005),
        ("very_high", 0.006),
        ("very_high", 0.006),
        ("very_high", 0.006),
    ]
    with path.open("a", encoding="utf-8") as f:
        for i in range(count):
            bucket, ret = bucket_pairs[i % len(bucket_pairs)]
            rec = SettlementRecord(
                prediction_id=f"{model_id}-pred-{i}",
                model_id=model_id,
                symbol="AAPL",
                ts_event=now_ns - 1000,
                horizon_ns=86_400_000_000_000,
                status=SettlementStatus.SETTLED,
                settled_at_ns=now_ns - i * 1_000_000_000,
                realized_return_gross=ret,
                realized_return_net=ret,
                abnormal_return=None,
                brier=0.2,
                calibration_bucket=bucket,
                cost_model_version="cm-v1",
                decision_window_start=now_ns - 1000,
                decision_window_end=now_ns,
            )
            f.write(rec.to_json() + "\n")


def _has_secret(obj: Any, secret_names: set[str]) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in secret_names:
                return True
            if _has_secret(v, secret_names):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _has_secret(item, secret_names):
                return True
    return False


_ORDER_FIELDS = frozenset({
    "order", "signal", "trade", "position", "allocation",
    "quantity", "side", "broker", "order_type", "order_id",
    "client_order_id", "time_in_force", "leverage", "margin_type",
    "account_id", "sig_predict", "size",
})


# --------------------------------------------------------------------------- #
# 1. Full promotion flow                                                       #
# --------------------------------------------------------------------------- #


class TestFullPromotionFlow:
    """Test the full promotion flow through the gateway."""

    def test_full_promotion_flow_approved(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path / "qf")
        gw.dossier_registry().register(_make_dossier(
            model_id="promo-model",
            status=DossierStatus.CANDIDATE,
        ))
        _write_settlements(tmp_path / "qf", "promo-model", 12)

        gw.run_tournament_sweep()

        submit_result = gw.submit_promotion(
            model_id="promo-model",
            target_level="shadow_approved",
            review_note="evidence looks solid",
        )
        assert submit_result["ok"] is True

        process_result = gw.process_promotion(
            model_id="promo-model",
            approve=True,
            review_note="approved by operator",
        )
        assert process_result["ok"] is True
        receipt_dict = process_result["receipt"]
        assert receipt_dict["decision"] == "approved"

    def test_promotion_rejected_no_dossier(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path / "qf")
        result = gw.submit_promotion(
            model_id="nonexistent",
            target_level="shadow_approved",
            review_note="test",
        )
        assert result["ok"] is False
        assert result["error_code"] == "no_dossier"

    def test_promotion_rejected_insufficient_evidence(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _make_gateway(tmp_path / "qf")
        gw.dossier_registry().register(_make_dossier(
            model_id="few-model",
            status=DossierStatus.CANDIDATE,
        ))
        _write_settlements(tmp_path / "qf", "few-model", 3)

        gw.run_tournament_sweep()
        gw.submit_promotion(
            model_id="few-model",
            target_level="shadow_approved",
            review_note="test",
        )
        result = gw.process_promotion(
            model_id="few-model",
            approve=True,
            review_note="test",
        )
        assert result["receipt"]["decision"] == "rejected"
        assert result["receipt"]["rejection_reason"] == "insufficient_evidence"

    def test_promotion_to_paper_approved_succeeds(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _make_gateway(tmp_path / "qf")
        gw.dossier_registry().register(_make_dossier(
            model_id="mvp-model",
            status=DossierStatus.SHADOW_APPROVED,
        ))
        _write_settlements(tmp_path / "qf", "mvp-model", 12)

        gw.run_tournament_sweep()
        gw.submit_promotion(
            model_id="mvp-model",
            target_level="paper_approved",
            review_note="attempting paper promotion",
        )
        result = gw.process_promotion(
            model_id="mvp-model",
            approve=True,
            review_note="test",
        )
        assert result["receipt"]["decision"] == "approved"

    def test_promotion_to_limited_live_approved_rejected_mvp_level_limit(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _make_gateway(tmp_path / "qf")
        gw.dossier_registry().register(_make_dossier(
            model_id="live-model",
            status=DossierStatus.PAPER_APPROVED,
        ))
        _write_settlements(tmp_path / "qf", "live-model", 12)

        gw.run_tournament_sweep()
        gw.submit_promotion(
            model_id="live-model",
            target_level="limited_live_approved",
            review_note="attempting limited live pilot",
        )
        result = gw.process_promotion(
            model_id="live-model",
            approve=True,
            review_note="test",
        )
        assert result["receipt"]["decision"] == "rejected"
        assert result["receipt"]["rejection_reason"] == "mvp_level_limit"


# --------------------------------------------------------------------------- #
# 2. Paper bridge publish with approved model                                  #
# --------------------------------------------------------------------------- #


class TestPaperBridgePublish:
    """Test paper bridge publish with an approved model."""

    def test_publish_succeeds_with_paper_approved(
        self, tmp_path: pathlib.Path
    ) -> None:
        gw = _make_gateway(tmp_path / "qf")
        gw.dossier_registry().register(_make_dossier(
            model_id="bridge-model-1",
            status=DossierStatus.PAPER_APPROVED,
        ))
        _write_settlements(tmp_path / "qf", "bridge-model-1", 12)
        gw.run_tournament_sweep()

        evidence = _make_evidence(
            model_id="bridge-model-1",
            dossier_status=DossierStatus.PAPER_APPROVED,
        )
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(model_id="bridge-model-1"),
            evidence=evidence,
        )

        assert receipt.status == BridgeStatus.PUBLISHED
        assert receipt.reason == "published to paper stream"
        assert receipt.prediction is not None
        assert receipt.prediction.model_id == "bridge-model-1"
        assert receipt.prediction.authority == "paper-only"

    def test_publish_creates_rollback_pointer(
        self, tmp_path: pathlib.Path
    ) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.status == BridgeStatus.PUBLISHED
        assert receipt.rollback_pointer is not None
        assert isinstance(receipt.rollback_pointer, RollbackPointer)
        assert receipt.rollback_pointer.model_id == "bridge-model-1"
        assert receipt.rollback_pointer.pointer_id.startswith("rb-")
        assert receipt.rollback_pointer.created_at_ns > 0
        assert receipt.rollback_pointer.reason == "paper bridge publish"

    def test_paper_prediction_has_no_order_fields(
        self, tmp_path: pathlib.Path
    ) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.prediction is not None
        pred = receipt.prediction
        for field in _ORDER_FIELDS:
            assert not hasattr(pred, field), (
                f"PaperPrediction must not have order field: {field}"
            )

    def test_paper_prediction_has_prediction_fields_only(
        self, tmp_path: pathlib.Path
    ) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.prediction is not None
        pred = receipt.prediction
        assert pred.prediction_id == "pred-1"
        assert pred.model_id == "bridge-model-1"
        assert pred.symbol == "AAPL"
        assert pred.ts_event == _T_EVENT
        assert pred.horizon_ns == _HORIZON_NS
        assert pred.direction == 1.0
        assert pred.confidence == 0.7
        assert pred.p_up == 0.7
        assert pred.authority == "paper-only"

    def test_receipt_to_dict_is_json_serializable(
        self, tmp_path: pathlib.Path
    ) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        d = receipt.to_dict()
        json.dumps(d)
        assert d["status"] == "published"
        assert d["prediction"] is not None
        assert d["rollback_pointer"] is not None

    def test_full_flow_settlement_to_bridge(
        self, tmp_path: pathlib.Path
    ) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
            "SPY": [
                PricePoint(ts_ns=_T_EVENT, close=400.0),
                PricePoint(ts_ns=_WINDOW_END, close=401.0),
            ],
        }
        gw = _make_gateway(tmp_path / "qf", bars)
        gw.dossier_registry().register(_make_dossier(
            model_id="flow-model",
            status=DossierStatus.PAPER_APPROVED,
        ))

        predictions = [
            _make_prediction(
                prediction_id=f"flow-pred-{i}",
                model_id="flow-model",
                ts_event=_T_EVENT,
            )
            for i in range(12)
        ]
        _store_predictions(gw, predictions)

        settle_receipt = gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)
        assert settle_receipt["settled_count"] == 12

        tournament_receipt = gw.run_tournament_sweep()
        assert len(tournament_receipt["scored_models"]) == 1

        dossier = gw.dossier_registry().get("flow-model")
        assert dossier is not None
        assert dossier.status == DossierStatus.PAPER_APPROVED

        evidence = PromotionEvidence(
            dossier=dossier,
            tournament_result=_make_tournament_result(model_id="flow-model"),
            sentinel_receipt=_make_sentinel_receipt(model_id="flow-model"),
            blocking_issues=[],
        )
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(
                model_id="flow-model",
                prediction_id="flow-pred-0",
            ),
            evidence=evidence,
        )
        assert receipt.status == BridgeStatus.PUBLISHED
        assert receipt.prediction is not None
        assert receipt.prediction.model_id == "flow-model"
        assert receipt.rollback_pointer is not None


# --------------------------------------------------------------------------- #
# 3. Circuit breaker                                                           #
# --------------------------------------------------------------------------- #


class TestCircuitBreaker:
    """Test the circuit breaker trips, blocks, and resets."""

    def test_circuit_breaker_trips_after_5_failures(self) -> None:
        breaker = BridgeCircuitBreaker(failure_threshold=5)
        for _ in range(4):
            breaker.record_failure()
        assert not breaker.is_tripped()
        breaker.record_failure()
        assert breaker.is_tripped()

    def test_circuit_breaker_blocks_publish(self) -> None:
        breaker = BridgeCircuitBreaker(failure_threshold=5)
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            ),
            circuit_breaker=breaker,
        )
        bad_prediction = {"prediction_id": "bad", "model_id": "m"}
        for _ in range(5):
            receipt = bridge.publish(
                prediction=bad_prediction,
                evidence=None,
            )
            assert receipt.status == BridgeStatus.REFUSED

        assert breaker.is_tripped()
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.status == BridgeStatus.REFUSED
        assert "circuit" in receipt.reason.lower()

    def test_circuit_breaker_reset_allows_publish(self) -> None:
        breaker = BridgeCircuitBreaker(failure_threshold=5)
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            ),
            circuit_breaker=breaker,
        )
        bad_prediction = {"prediction_id": "bad", "model_id": "m"}
        for _ in range(5):
            bridge.publish(prediction=bad_prediction, evidence=None)
        assert breaker.is_tripped()

        breaker.reset()
        assert not breaker.is_tripped()

        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.status == BridgeStatus.PUBLISHED

    def test_success_resets_failure_count(self) -> None:
        breaker = BridgeCircuitBreaker(failure_threshold=5)
        bridge = PaperBridge(
            config=BridgeConfig(
                allow_paper_bridge=True,
                runtime_mode="paper",
            ),
            circuit_breaker=breaker,
        )
        for _ in range(4):
            bridge.publish(
                prediction={"prediction_id": "bad", "model_id": "m"},
                evidence=None,
            )
        assert not breaker.is_tripped()

        bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert not breaker.is_tripped()

        for _ in range(4):
            bridge.publish(
                prediction={"prediction_id": "bad", "model_id": "m"},
                evidence=None,
            )
        assert not breaker.is_tripped()


# --------------------------------------------------------------------------- #
# 4. Paper bridge refuses non-paper runtime                                    #
# --------------------------------------------------------------------------- #


class TestRefusesNonPaperRuntime:
    """Paper bridge refuses non-paper runtime."""

    def test_refuses_live_runtime(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="live",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.status == BridgeStatus.REFUSED
        assert "non-paper" in receipt.reason.lower()

    def test_refuses_shadow_runtime(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="shadow",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.status == BridgeStatus.REFUSED

    def test_refuses_when_disabled(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=False,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        assert receipt.status == BridgeStatus.REFUSED
        assert "disabled" in receipt.reason.lower()


# --------------------------------------------------------------------------- #
# 5. Paper bridge refuses without evidence packet                              #
# --------------------------------------------------------------------------- #


class TestRefusesWithoutEvidence:
    """Paper bridge refuses without evidence packet."""

    def test_refuses_none_evidence(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=None,
        )
        assert receipt.status == BridgeStatus.REFUSED
        assert "evidence" in receipt.reason.lower()

    def test_refuses_none_dossier(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=PromotionEvidence(dossier=None),
        )
        assert receipt.status == BridgeStatus.REFUSED
        assert "dossier" in receipt.reason.lower()


# --------------------------------------------------------------------------- #
# 6. Paper bridge refuses without paper_approved status                        #
# --------------------------------------------------------------------------- #


class TestRefusesWithoutPaperApproved:
    """Paper bridge refuses without paper_approved status."""

    def test_refuses_candidate_status(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(
                dossier_status=DossierStatus.CANDIDATE,
            ),
        )
        assert receipt.status == BridgeStatus.REFUSED
        assert "paper" in receipt.reason.lower()

    def test_refuses_shadow_approved_status(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(
                dossier_status=DossierStatus.SHADOW_APPROVED,
            ),
        )
        assert receipt.status == BridgeStatus.REFUSED
        assert "paper" in receipt.reason.lower()

    def test_refuses_research_approved_status(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(
                dossier_status=DossierStatus.RESEARCH_APPROVED,
            ),
        )
        assert receipt.status == BridgeStatus.REFUSED


# --------------------------------------------------------------------------- #
# 7. No secrets in bridge output                                               #
# --------------------------------------------------------------------------- #


class TestNoSecretsInBridgeOutput:
    """No secrets in any bridge output."""

    SECRET_NAMES = {
        "api_key", "token", "secret", "password",
        "broker_account", "credential", "private_key",
        "access_key", "session_token",
    }

    def test_receipt_to_dict_has_no_secrets(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        d = receipt.to_dict()
        assert not _has_secret(d, self.SECRET_NAMES)

    def test_refused_receipt_has_no_secrets(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=None,
        )
        d = receipt.to_dict()
        assert not _has_secret(d, self.SECRET_NAMES)

    def test_paper_prediction_has_no_secrets(self) -> None:
        pred = convert_shadow_to_paper(_make_prediction())
        d = pred.model_dump()
        assert not _has_secret(d, self.SECRET_NAMES)

    def test_rollback_pointer_has_no_secrets(self) -> None:
        ptr = RollbackPointer(
            model_id="m1",
            pointer_id="ptr-1",
            created_at_ns=1000,
            reason="test",
        )
        d = ptr.model_dump()
        assert not _has_secret(d, self.SECRET_NAMES)

    def test_receipt_json_serializable_no_secrets_in_string(self) -> None:
        bridge = PaperBridge(config=BridgeConfig(
            allow_paper_bridge=True,
            runtime_mode="paper",
        ))
        receipt = bridge.publish(
            prediction=_make_prediction(),
            evidence=_make_evidence(),
        )
        text = json.dumps(receipt.to_dict())
        for secret in self.SECRET_NAMES:
            assert secret not in text.lower()
