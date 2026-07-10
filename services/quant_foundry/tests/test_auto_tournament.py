"""Tests for auto-tournament consumer (Tier 2e).

Tests the AutoTournamentConsumer that automatically consumes settled
shadow predictions, runs the tournament scorer, and records metrics.
"""

from __future__ import annotations

import time

from quant_foundry.auto_tournament import (
    AutoTournamentConsumer,
)
from quant_foundry.dossier import DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.leaderboard_expanded import ExpandedLeaderboard
from quant_foundry.outcomes import SettlementStatus
from quant_foundry.promotion import PromotionGate
from quant_foundry.registry_db import ModelRegistryDB
from quant_foundry.tournament import Tournament
from quant_foundry.tournament_sweep import TournamentSweep
from sqlalchemy import select
from sqlalchemy.orm import Session
from helpers.product_loop_helpers import (
    _FakeSettlementLedger,
    _MODEL_ID,
    _dispatch_and_callback,
    _make_engine,
    _make_gateway,
    _make_settlement_record,
)

from fincept_db.registry_tables import ModelMetricRow

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _make_tournament_sweep(gateway: QuantFoundryGateway) -> TournamentSweep:
    """Build a TournamentSweep using the gateway's settlement ledger."""
    return TournamentSweep(
        settlement_ledger=gateway.tournament_sweep().settlement_ledger,
        dossier_registry=gateway.dossier_registry(),
        tournament=Tournament(seed=42, n_bootstrap=100),
        leaderboard=ExpandedLeaderboard(),
        min_settled_samples=10,
    )


# --------------------------------------------------------------------------- #
# Tests: AutoTournamentConsumer                                                #
# --------------------------------------------------------------------------- #


class TestAutoTournamentConsumer:
    def test_scores_model_with_enough_settled(self, tmp_path) -> None:
        """Consumer scores a model with >= min_settled_count settled records."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:tourn:1")
        sweep = _make_tournament_sweep(gateway)

        # Create 50 settled records.
        ledger = _FakeSettlementLedger()
        for i in range(50):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"pred-{i}",
                    realized_return_net=0.001 + i * 0.0001,
                    brier=0.20,
                )
            )

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        receipt = consumer.run()

        assert receipt.scored == 1
        assert receipt.skipped == 0
        assert receipt.errored == 0
        assert receipt.total == 1

        result = receipt.results[0]
        assert result.model_id == _MODEL_ID
        assert result.version_id == version_id
        assert result.settled_count == 50
        assert result.skipped is False
        assert result.error is None
        assert result.metric_id != ""  # was recorded
        assert result.tournament_result.total_score > 0
        engine.dispose()

    def test_skips_model_with_insufficient_settled(self, tmp_path) -> None:
        """Consumer skips a model with < min_settled_count settled records."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:tourn:2")
        sweep = _make_tournament_sweep(gateway)

        ledger = _FakeSettlementLedger()
        for i in range(10):  # only 10, min is 30
            ledger.add(_make_settlement_record(prediction_id=f"pred-{i}"))

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        receipt = consumer.run()

        assert receipt.scored == 0
        assert receipt.skipped == 1
        assert receipt.errored == 0

        result = receipt.results[0]
        assert result.skipped is True
        assert "insufficient" in (result.error or "")
        engine.dispose()

    def test_skips_model_not_in_registry(self, tmp_path) -> None:
        """Consumer skips a model that has no version in the registry."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        sweep = _make_tournament_sweep(gateway)

        ledger = _FakeSettlementLedger()
        for i in range(50):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"pred-{i}",
                    model_id="model:unknown:1",
                )
            )

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        receipt = consumer.run()

        assert receipt.scored == 0
        assert receipt.skipped == 1
        result = receipt.results[0]
        assert "no version" in (result.error or "")
        engine.dispose()

    def test_records_tournament_metrics_in_db(self, tmp_path) -> None:
        """Consumer records tournament metrics in the model_metrics table."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:tourn:3")
        sweep = _make_tournament_sweep(gateway)

        ledger = _FakeSettlementLedger()
        for i in range(50):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"pred-{i}",
                    realized_return_net=0.002,
                    brier=0.20,
                )
            )

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        consumer.run()

        # Verify metrics were recorded in the DB.
        with Session(engine) as session:
            metrics_rows = session.scalars(
                select(ModelMetricRow).where(
                    ModelMetricRow.version_id == version_id,
                    ModelMetricRow.metric_type == "tournament",
                )
            ).all()
            assert len(metrics_rows) == 1
            metrics = metrics_rows[0].metrics
            assert metrics["model_id"] == _MODEL_ID
            assert metrics["settled_count"] == 50
            assert "total_score" in metrics
            assert "deflated_sharpe" in metrics
        engine.dispose()

    def test_filters_non_settled_records(self, tmp_path) -> None:
        """Consumer only scores SETTLED records, not PENDING."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        _dispatch_and_callback(gateway, engine, secret, "qf:tourn:4")
        sweep = _make_tournament_sweep(gateway)

        ledger = _FakeSettlementLedger()
        # 50 SETTLED + 30 PENDING_TIME
        for i in range(50):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"settled-{i}",
                    status=SettlementStatus.SETTLED,
                    realized_return_net=0.001,
                )
            )
        for i in range(30):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"pending-{i}",
                    status=SettlementStatus.PENDING_TIME,
                    realized_return_net=None,
                )
            )

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        receipt = consumer.run()

        assert receipt.scored == 1
        result = receipt.results[0]
        assert result.settled_count == 50  # only SETTLED
        engine.dispose()

    def test_scores_multiple_models(self, tmp_path) -> None:
        """Consumer scores multiple models in one run."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        # Create two versions under the same model.
        v1 = _dispatch_and_callback(
            gateway,
            engine,
            secret,
            "qf:tourn:multi:1",
            artifact_id="artifact:tourn:multi:1",
            sha256="f" * 64,
        )
        v2 = _dispatch_and_callback(
            gateway,
            engine,
            secret,
            "qf:tourn:multi:2",
            artifact_id="artifact:tourn:multi:2",
            sha256="g" * 64,
        )
        sweep = _make_tournament_sweep(gateway)

        ledger = _FakeSettlementLedger()
        # 50 records for our model + 50 for another model.
        for i in range(50):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"ours-{i}",
                    model_id=_MODEL_ID,
                )
            )
        for i in range(50):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"theirs-{i}",
                    model_id="model:other:1",
                )
            )

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        receipt = consumer.run()

        # Our model should be scored; the other model has no version.
        assert receipt.scored == 1
        assert receipt.skipped == 1  # other model has no version
        engine.dispose()

    def test_empty_ledger_no_scores(self, tmp_path) -> None:
        """Empty settlement ledger → no scores."""
        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        sweep = _make_tournament_sweep(gateway)

        ledger = _FakeSettlementLedger()  # empty

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        receipt = consumer.run()

        assert receipt.scored == 0
        assert receipt.skipped == 0
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
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        sweep = _make_tournament_sweep(gateway)

        ledger = _FakeSettlementLedger()
        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        receipt = consumer.run()

        try:
            receipt.scored = 999
            assert False, "should have raised"
        except Exception:
            pass  # expected — frozen model
        engine.dispose()


# --------------------------------------------------------------------------- #
# Tests: Integration with auto-promotion orchestrator                         #
# --------------------------------------------------------------------------- #


class TestAutoTournamentWithAutoPromotion:
    def test_auto_tournament_then_auto_promotion(self, tmp_path) -> None:
        """Full flow: auto-tournament → auto-promotion.

        1. Dispatch + callback for version 1.
        2. Auto-tournament consumer scores it from settled records.
        3. Auto-promotion orchestrator finds the scored version and
           promotes it (first-model scenario, no champion).
        4. Verify version 1 is promoted to research_approved.
        """
        from quant_foundry.auto_promotion import AutoPromotionOrchestrator
        from quant_foundry.champion_challenger import ChampionChallengerConfig

        engine = _make_engine()
        secret = "test-secret"
        registry = ModelRegistryDB(
            engine=engine,
            gate=PromotionGate(min_settled_count=10),
        )
        gateway = _make_gateway(engine, secret, registry, tmp_path)
        version_id = _dispatch_and_callback(gateway, engine, secret, "qf:tourn:e2e:1")
        sweep = _make_tournament_sweep(gateway)

        # Record sentinel metrics (passed).
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
        # Auto-tournament: score from settled records.
        # Use varied returns so the Sharpe ratio is meaningful.
        ledger = _FakeSettlementLedger()
        import random

        rng = random.Random(42)
        for i in range(50):
            ledger.add(
                _make_settlement_record(
                    prediction_id=f"pred-{i}",
                    realized_return_net=0.006 + rng.gauss(0, 0.002),
                    brier=0.20,
                )
            )

        consumer = AutoTournamentConsumer(
            settlement_ledger=ledger,
            tournament=Tournament(seed=42, n_bootstrap=100),
            registry=registry,
            tournament_sweep=sweep,
            min_settled_count=30,
        )
        tourn_receipt = consumer.run()
        assert tourn_receipt.scored == 1

        # Auto-promotion: promote the scored version.
        # Use the SettledComparisonInputProvider from Tier 2c so the
        # orchestrator can get comparison input for the challenger.
        from quant_foundry.settlement_provider import SettledComparisonInputProvider

        provider = SettledComparisonInputProvider(
            registry=registry,
            settlement_ledger=ledger,
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
        assert promo_receipt.total >= 1

        # Verify the version was promoted.
        versions = registry.list_versions(_MODEL_ID)
        assert any(v["status"] == DossierStatus.RESEARCH_APPROVED.value for v in versions)
        engine.dispose()
