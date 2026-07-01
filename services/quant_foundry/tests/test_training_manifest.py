"""
TDD tests for quant_foundry.training_manifest (Stage Task 1).

The training manifest is the operator-facing envelope that bundles a
feature-lake manifest reference, baseline hyperparameters, walk-forward
windows, and a budget envelope. It must:

- Be frozen + extra='forbid' (audit integrity).
- Reject secret-shaped names anywhere.
- Reject unknown model families and out-of-bounds hyperparameters.
- Compute a stable content hash so the dispatch script can pin the
  exact manifest it sent.
- Derive a walk-forward window that respects the label-horizon embargo.

The local dispatcher wraps the manifest with a ``LocalTrainer`` (no live
RunPod). It must:

- Consult ``BudgetGuard`` BEFORE dispatching; reject on budget failure.
- Run the trainer and return a ``DispatchReceipt`` on success.
- Surface trainer failures (deadline breach, training error) as
  ``TRAINER_FAILED`` with the error code carried on the receipt.
- Always emit ``Authority.SHADOW_ONLY`` on the dossier authority field.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from pydantic import ValidationError
from quant_foundry.budget import BudgetGuard
from quant_foundry.dataset_manifest import FeatureLakeManifest
from quant_foundry.feature_lake import (
    FeatureLakeBuilder,
    FeatureRow,
    FeatureValue,
    UniverseEntry,
)
from quant_foundry.local_training_dispatch import (
    DispatchReceipt,
    DispatchStatus,
    LocalTrainingDispatcher,
    build_training_manifest_from_feature_lake,
)
from quant_foundry.runpod_training import LocalTrainer
from quant_foundry.schemas import Authority
from quant_foundry.training_manifest import (
    TrainingManifest,
    derive_walk_forward_window,
)

NS_PER_DAY = 86_400_000_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixture_universe() -> tuple[UniverseEntry, ...]:
    return (
        UniverseEntry(symbol="AAPL", listed_until=None, renamed_from=None),
        UniverseEntry(symbol="MSFT", listed_until=None, renamed_from=None),
    )


def _fixture_rows(n_days: int = 30) -> tuple[FeatureRow, ...]:
    rows: list[FeatureRow] = []
    for d in range(n_days):
        ts = (10 + d) * NS_PER_DAY
        rows.append(
            FeatureRow(
                symbol="AAPL",
                event_ts=ts,
                decision_time=ts,
                features=(
                    FeatureValue(name="ret_1d", value=0.001 * d, observed_at=ts),
                    FeatureValue(
                        name="vol_20d",
                        value=0.2 + 0.001 * (d % 5),
                        observed_at=ts - NS_PER_DAY,
                    ),
                ),
                label_horizon_ns=NS_PER_DAY,
            )
        )
    return tuple(rows)


def _fixture_lake_manifest() -> FeatureLakeManifest:
    return FeatureLakeBuilder(
        dataset_id="ds-test",
        universe=_fixture_universe(),
        rows=_fixture_rows(n_days=30),
        feature_schema_hash="feat-v1",
        label_schema_hash="label-v1",
        max_label_horizon_ns=NS_PER_DAY,
        n_folds=2,
        source_vintage_refs=["vintage-2026-06-25"],
    ).build_manifest()


def _training_manifest(**overrides: object) -> TrainingManifest:
    lake = _fixture_lake_manifest()
    base: dict[str, object] = {
        "manifest_id": "tm-001",
        "feature_lake_manifest_ref": lake.dataset_id,
        "feature_lake_manifest_hash": lake.manifest_hash(),
        "model_family": "gbm",
        "hyperparameters": {
            "n_estimators": 100.0,
            "max_depth": 4.0,
            "learning_rate": 0.05,
        },
        "train_window_ns": 30 * NS_PER_DAY,
        "val_window_ns": 10 * NS_PER_DAY,
        "test_window_ns": 10 * NS_PER_DAY,
        "label_horizon_ns": NS_PER_DAY,
        "random_seed": 42,
        "walk_forward_enabled": True,
        "budget_cents": 0,
        "timeout_seconds": 120,
        "operator_note": "test manifest",
    }
    base.update(overrides)
    return TrainingManifest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TrainingManifest schema tests
# ---------------------------------------------------------------------------


class TestTrainingManifestSchema:
    def test_minimal_manifest_constructs(self) -> None:
        m = _training_manifest()
        assert m.schema_version == 1
        assert m.model_family == "gbm"
        assert len(m.content_hash) == 64

    def test_manifest_is_frozen(self) -> None:
        m = _training_manifest()
        with pytest.raises(ValidationError, match="frozen"):
            m.manifest_id = "tm-002"  # type: ignore[misc]

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _training_manifest(unknown_field="x")

    def test_empty_manifest_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="manifest_id"):
            _training_manifest(manifest_id="")

    def test_hash_must_be_64_hex(self) -> None:
        with pytest.raises(ValueError, match="64-char hex"):
            _training_manifest(feature_lake_manifest_hash="not-a-hash")

    def test_unknown_model_family_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _training_manifest(model_family="transformer_v99")

    def test_hyperparameter_out_of_bounds_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside bounds"):
            _training_manifest(hyperparameters={"max_depth": 100.0})

    def test_unknown_hyperparameter_rejected(self) -> None:
        with pytest.raises(ValueError, match="not defined"):
            _training_manifest(hyperparameters={"invented_param": 1.0})

    def test_secret_named_hyperparameter_rejected(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            _training_manifest(hyperparameters={"api_key": 1.0})

    def test_negative_budget_rejected(self) -> None:
        with pytest.raises(ValueError, match="budget_cents"):
            _training_manifest(budget_cents=-1)

    def test_zero_windows_rejected(self) -> None:
        with pytest.raises(ValueError, match="train_window_ns"):
            _training_manifest(train_window_ns=0)

    def test_operator_note_with_secret_rejected(self) -> None:
        with pytest.raises(ValueError, match="secret-like"):
            _training_manifest(operator_note="contains api_key=abc")

    def test_content_hash_is_deterministic(self) -> None:
        m1 = _training_manifest()
        m2 = _training_manifest()
        assert m1.content_hash == m2.content_hash

    def test_different_hyperparameters_change_hash(self) -> None:
        m1 = _training_manifest()
        m2 = _training_manifest(hyperparameters={"n_estimators": 200.0})
        assert m1.content_hash != m2.content_hash

    def test_to_dispatch_request_carries_manifest_ref(self) -> None:
        m = _training_manifest()
        req = m.to_dispatch_request(job_id="qf:test:001")
        assert req["job_id"] == "qf:test:001"
        assert req["model_family"] == "gbm"
        assert req["random_seed"] == 42
        assert "train_window_ns" in req["extra_constraints"]
        assert "manifest_content_hash" in req["extra_constraints"]
        assert req["extra_constraints"]["manifest_content_hash"] == m.content_hash


# ---------------------------------------------------------------------------
# Walk-forward derivation
# ---------------------------------------------------------------------------


class TestWalkForward:
    def test_derive_window_basic(self) -> None:
        as_of = 100 * NS_PER_DAY
        w = derive_walk_forward_window(
            train_window_ns=30 * NS_PER_DAY,
            val_window_ns=10 * NS_PER_DAY,
            test_window_ns=10 * NS_PER_DAY,
            label_horizon_ns=NS_PER_DAY,
            as_of_ts=as_of,
        )
        assert w.test_end == as_of
        assert w.train_start < w.train_end < w.val_start < w.val_end < w.test_start < w.test_end

    def test_embargo_between_train_and_val(self) -> None:
        as_of = 100 * NS_PER_DAY
        horizon = NS_PER_DAY
        w = derive_walk_forward_window(
            train_window_ns=30 * NS_PER_DAY,
            val_window_ns=10 * NS_PER_DAY,
            test_window_ns=10 * NS_PER_DAY,
            label_horizon_ns=horizon,
            as_of_ts=as_of,
        )
        # The gap between train_end and val_start must be at least the
        # label horizon so a train row's label does not bleed into val.
        assert w.val_start - w.train_end >= horizon
        assert w.test_start - w.val_end >= horizon

    def test_train_window_too_long_raises(self) -> None:
        with pytest.raises(ValueError, match="too long"):
            derive_walk_forward_window(
                train_window_ns=10_000 * NS_PER_DAY,
                val_window_ns=10 * NS_PER_DAY,
                test_window_ns=10 * NS_PER_DAY,
                label_horizon_ns=NS_PER_DAY,
                as_of_ts=100 * NS_PER_DAY,
            )

    def test_zero_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="> 0"):
            derive_walk_forward_window(
                train_window_ns=0,
                val_window_ns=10 * NS_PER_DAY,
                test_window_ns=10 * NS_PER_DAY,
                label_horizon_ns=NS_PER_DAY,
                as_of_ts=100 * NS_PER_DAY,
            )


# ---------------------------------------------------------------------------
# LocalTrainingDispatcher
# ---------------------------------------------------------------------------


def _make_guard(tmp_path: pathlib.Path, *, budget_cents: int = 10_000) -> BudgetGuard:
    return BudgetGuard(
        base_dir=tmp_path / "b",
        monthly_budget_cents=budget_cents,
        kill_switch_enabled=False,
    )


def _dispatcher(tmp_path: pathlib.Path, **kwargs: object) -> LocalTrainingDispatcher:
    return LocalTrainingDispatcher(
        budget_guard=_make_guard(tmp_path),
        callback_secret="test-callback-secret",
        trainer=LocalTrainer(),
        worker_id="local-test-1",
        **kwargs,
    )


class TestLocalDispatcher:
    def test_zero_cost_dispatch_succeeds(self, tmp_path: pathlib.Path) -> None:
        dispatcher = _dispatcher(tmp_path)
        manifest = _training_manifest()
        as_of = int(time.time_ns())
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:001", as_of_ts=as_of)
        assert isinstance(receipt, DispatchReceipt)
        assert receipt.status == DispatchStatus.DISPATCHED
        assert receipt.dossier_authority == Authority.SHADOW_ONLY.value
        assert receipt.artifact_id is not None
        assert receipt.artifact_sha256 is not None
        assert receipt.dossier_id is not None
        assert receipt.budget_decision.allowed is True

    def test_paid_dispatch_consumes_budget(self, tmp_path: pathlib.Path) -> None:
        dispatcher = _dispatcher(tmp_path)
        manifest = _training_manifest(budget_cents=500)
        as_of = int(time.time_ns())
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:002", as_of_ts=as_of)
        assert receipt.status == DispatchStatus.DISPATCHED
        # The guard recorded the spend.
        guard = dispatcher.budget_guard
        assert guard.get_monthly_spend() == 500

    def test_over_budget_rejected(self, tmp_path: pathlib.Path) -> None:
        dispatcher = _dispatcher(tmp_path)
        # Manifest requests 5000c but guard only has 100c.
        manifest = _training_manifest(budget_cents=5000)
        # Override the dispatcher to use a 100c budget.
        dispatcher.budget_guard = _make_guard(tmp_path, budget_cents=100)
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:003", as_of_ts=int(time.time_ns()))
        assert receipt.status == DispatchStatus.BUDGET_REJECTED
        assert receipt.artifact_id is None
        assert receipt.error_code == "budget_rejected"

    def test_trainer_failure_returns_failed_status(self, tmp_path: pathlib.Path) -> None:
        failing_trainer = LocalTrainer(should_fail=True)
        dispatcher = LocalTrainingDispatcher(
            budget_guard=_make_guard(tmp_path),
            callback_secret="test-callback-secret",
            trainer=failing_trainer,
            worker_id="local-failing-1",
        )
        manifest = _training_manifest()
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:004", as_of_ts=int(time.time_ns()))
        assert receipt.status == DispatchStatus.TRAINER_FAILED
        assert receipt.error_code == "training_error"
        assert receipt.artifact_id is None

    def test_deadline_breach_is_caught(self, tmp_path: pathlib.Path) -> None:
        dispatcher = _dispatcher(tmp_path)
        # 0-second deadline causes immediate timeout in the handler.
        manifest = _training_manifest(timeout_seconds=0)
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:005", as_of_ts=int(time.time_ns()))
        assert receipt.status == DispatchStatus.TRAINER_FAILED
        assert receipt.error_code == "timeout"

    def test_invalid_window_returns_validation_error(self, tmp_path: pathlib.Path) -> None:
        dispatcher = _dispatcher(tmp_path)
        # train_window > as_of_ts so derivation fails.
        manifest = _training_manifest(
            train_window_ns=10_000 * NS_PER_DAY,
            val_window_ns=10 * NS_PER_DAY,
            test_window_ns=10 * NS_PER_DAY,
        )
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:006", as_of_ts=100 * NS_PER_DAY)
        assert receipt.status == DispatchStatus.VALIDATION_ERROR
        assert receipt.error_code == "validation_error"

    def test_authority_is_always_shadow_only(self, tmp_path: pathlib.Path) -> None:
        dispatcher = _dispatcher(tmp_path)
        manifest = _training_manifest()
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:007", as_of_ts=int(time.time_ns()))
        assert receipt.dossier_authority == "shadow-only"

    def test_to_dict_is_json_serializable(self, tmp_path: pathlib.Path) -> None:
        import json

        dispatcher = _dispatcher(tmp_path)
        manifest = _training_manifest()
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:008", as_of_ts=int(time.time_ns()))
        body = receipt.to_dict()
        # Must round-trip through json without error.
        json.dumps(body, default=str)

    def test_no_secrets_in_receipt(self, tmp_path: pathlib.Path) -> None:
        dispatcher = _dispatcher(tmp_path)
        manifest = _training_manifest()
        receipt = dispatcher.dispatch(manifest, job_id="qf:test:009", as_of_ts=int(time.time_ns()))
        forbidden = (
            "password",
            "token",
            "secret",
            "api_key",
            "apikey",
            "credential",
            "private_key",
        )
        body = receipt.to_dict()
        for k, v in body.items():
            if isinstance(v, str):
                for sub in forbidden:
                    assert sub not in v.lower(), f"secret substring {sub!r} in {k}={v!r}"


# ---------------------------------------------------------------------------
# Convenience builder
# ---------------------------------------------------------------------------


class TestBuildTrainingManifest:
    def test_builder_pulls_lake_manifest_fields(
        self,
    ) -> None:
        lake = _fixture_lake_manifest()
        m = build_training_manifest_from_feature_lake(
            feature_lake_manifest=lake,
            manifest_id="tm-conv-001",
            model_family="gbm",
            hyperparameters={"n_estimators": 50.0, "max_depth": 3.0},
            train_window_ns=30 * NS_PER_DAY,
            val_window_ns=10 * NS_PER_DAY,
            test_window_ns=10 * NS_PER_DAY,
            label_horizon_ns=NS_PER_DAY,
            random_seed=7,
        )
        assert m.feature_lake_manifest_ref == lake.dataset_id
        assert m.feature_lake_manifest_hash == lake.manifest_hash()
        assert m.model_family == "gbm"
        assert m.random_seed == 7
        assert m.content_hash != ""
