"""Tests for quant_foundry.event_trainer (T-12.2).

Covers the event abnormal-return trainer: EventTrainerConfig
construction + validation, EventTrainingResult construction,
EventAbnormalReturnModel forward pass (multi-horizon output),
EventTrainer train/predict/save/load round-trip, OOF prediction
writing, per-event-type metrics, confidence-bucket metrics, promotion
eligibility, family registration, and fail-closed source-hash
validation.

The test host is CPU-only (torch is installed with the CPU index URL),
so all training runs use ``device="cpu"``. Synthetic data is used
throughout — no real feature-lake data is touched.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from quant_foundry.event_trainer import (
    EventAbnormalReturnModel,
    EventTrainer,
    EventTrainerConfig,
    EventTrainingResult,
    compute_confidence_bucket_metrics,
    compute_event_type_metrics,
    register_event_family,
    validate_promotion_eligibility,
)
from quant_foundry.oof_artifacts import read_oof_artifact
from quant_foundry.tabular_neural_runtime import GPUStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SOURCE_HASH = "a" * 64  # 64-char hex SHA-256


def _small_config(**overrides) -> EventTrainerConfig:
    """Build a small EventTrainerConfig for fast CPU tests."""
    defaults = dict(
        embedding_dim=8,
        hidden_dims=[8, 4],
        horizons=[1, 5, 20],
        learning_rate=0.01,
        epochs=2,
        batch_size=8,
        dropout=0.0,
        device="cpu",
        seed=42,
        shadow_only=True,
    )
    defaults.update(overrides)
    return EventTrainerConfig(**defaults)


def _synthetic_embeddings(
    n: int = 20, d: int = 8, seed: int = 0
) -> np.ndarray:
    """Generate synthetic event embeddings."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal((n, d))


def _synthetic_labels(
    n: int = 20, horizons: list[int] | None = None, seed: int = 1
) -> np.ndarray:
    """Generate synthetic multi-horizon abnormal-return labels."""
    if horizons is None:
        horizons = [1, 5, 20]
    rng = np.random.default_rng(seed)
    return rng.standard_normal((n, len(horizons))) * 0.01


def _synthetic_event_types(n: int = 20, seed: int = 2) -> list[str]:
    """Generate synthetic event-type labels (cyclic over a small set)."""
    types = ["earnings", "guidance", "analyst", "filings"]
    return [types[i % len(types)] for i in range(n)]


# ---------------------------------------------------------------------------
# EventTrainerConfig
# ---------------------------------------------------------------------------


class TestEventTrainerConfig:
    def test_default_construction(self) -> None:
        cfg = EventTrainerConfig()
        assert cfg.embedding_dim == 384
        assert cfg.hidden_dims == [128, 64]
        assert cfg.horizons == [1, 5, 20]
        assert cfg.learning_rate == 0.001
        assert cfg.epochs == 10
        assert cfg.batch_size == 32
        assert cfg.dropout == 0.1
        assert cfg.device == "auto"
        assert cfg.seed == 42
        assert cfg.shadow_only is True

    def test_custom_construction(self) -> None:
        cfg = EventTrainerConfig(
            embedding_dim=16,
            hidden_dims=[32, 16],
            horizons=[1, 10],
            learning_rate=0.005,
            epochs=5,
            batch_size=4,
            dropout=0.2,
            device="cpu",
            seed=7,
            shadow_only=False,
        )
        assert cfg.embedding_dim == 16
        assert cfg.hidden_dims == [32, 16]
        assert cfg.horizons == [1, 10]
        assert cfg.learning_rate == 0.005
        assert cfg.epochs == 5
        assert cfg.batch_size == 4
        assert cfg.dropout == 0.2
        assert cfg.device == "cpu"
        assert cfg.seed == 7
        assert cfg.shadow_only is False

    def test_frozen(self) -> None:
        cfg = _small_config()
        with pytest.raises(Exception):
            cfg.epochs = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(unknown_field=1)  # type: ignore[call-arg]

    def test_embedding_dim_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(embedding_dim=0)

    def test_embedding_dim_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(embedding_dim=-1)

    def test_hidden_dims_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(hidden_dims=[])

    def test_hidden_dims_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(hidden_dims=[8, 0])

    def test_horizons_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(horizons=[])

    def test_horizons_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(horizons=[1, 0])

    def test_horizons_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(horizons=[-5])

    def test_learning_rate_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(learning_rate=0.0)

    def test_learning_rate_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(learning_rate=-0.001)

    def test_dropout_below_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(dropout=-0.1)

    def test_dropout_at_one_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(dropout=1.0)

    def test_dropout_above_one_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(dropout=1.5)

    def test_dropout_at_zero_allowed(self) -> None:
        cfg = EventTrainerConfig(dropout=0.0)
        assert cfg.dropout == 0.0

    def test_batch_size_zero_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(batch_size=0)

    def test_epochs_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(epochs=-1)

    def test_epochs_zero_allowed(self) -> None:
        cfg = EventTrainerConfig(epochs=0)
        assert cfg.epochs == 0

    def test_device_invalid_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainerConfig(device="tpu")

    def test_device_cpu_allowed(self) -> None:
        cfg = EventTrainerConfig(device="cpu")
        assert cfg.device == "cpu"

    def test_device_cuda_allowed(self) -> None:
        cfg = EventTrainerConfig(device="cuda")
        assert cfg.device == "cuda"

    def test_single_horizon_allowed(self) -> None:
        cfg = EventTrainerConfig(horizons=[1])
        assert cfg.horizons == [1]


# ---------------------------------------------------------------------------
# EventTrainingResult
# ---------------------------------------------------------------------------


class TestEventTrainingResult:
    def test_construction(self) -> None:
        cfg = _small_config()
        result = EventTrainingResult(
            config=cfg,
            source_hash=_SOURCE_HASH,
            final_loss=0.1,
            epoch_losses=[0.2, 0.1],
            gpu_status=GPUStatus(available=False),
            artifact_path=None,
            oof_artifact_path=None,
            is_shadow=True,
            promotion_eligible=False,
            metrics={"h1_mse": 0.1},
            event_type_metrics={"earnings": {"h1_mse": 0.1}},
            duration_seconds=1.5,
        )
        assert result.config == cfg
        assert result.source_hash == _SOURCE_HASH
        assert result.final_loss == 0.1
        assert result.epoch_losses == [0.2, 0.1]
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.metrics == {"h1_mse": 0.1}
        assert result.event_type_metrics == {"earnings": {"h1_mse": 0.1}}
        assert result.duration_seconds == 1.5

    def test_frozen(self) -> None:
        result = EventTrainingResult(
            config=_small_config(),
            source_hash=_SOURCE_HASH,
            final_loss=0.1,
            epoch_losses=[],
            gpu_status=GPUStatus(available=False),
            is_shadow=True,
            promotion_eligible=False,
            metrics={},
            event_type_metrics={},
            duration_seconds=1.0,
        )
        with pytest.raises(Exception):
            result.final_loss = 99.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            EventTrainingResult(
                config=_small_config(),
                source_hash=_SOURCE_HASH,
                final_loss=0.1,
                epoch_losses=[],
                gpu_status=GPUStatus(available=False),
                is_shadow=True,
                promotion_eligible=False,
                metrics={},
                event_type_metrics={},
                duration_seconds=1.0,
                unknown_field=1,  # type: ignore[call-arg]
            )

    def test_missing_source_hash_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainingResult(
                config=_small_config(),
                source_hash="",
                final_loss=0.1,
                epoch_losses=[],
                gpu_status=GPUStatus(available=False),
                is_shadow=True,
                promotion_eligible=False,
                metrics={},
                event_type_metrics={},
                duration_seconds=1.0,
            )

    def test_whitespace_source_hash_rejected(self) -> None:
        with pytest.raises(Exception):
            EventTrainingResult(
                config=_small_config(),
                source_hash="   ",
                final_loss=0.1,
                epoch_losses=[],
                gpu_status=GPUStatus(available=False),
                is_shadow=True,
                promotion_eligible=False,
                metrics={},
                event_type_metrics={},
                duration_seconds=1.0,
            )

    def test_defaults(self) -> None:
        result = EventTrainingResult(
            config=_small_config(),
            source_hash=_SOURCE_HASH,
            final_loss=0.1,
            epoch_losses=[],
            gpu_status=GPUStatus(available=False),
            is_shadow=True,
            promotion_eligible=False,
            duration_seconds=1.0,
        )
        assert result.artifact_path is None
        assert result.oof_artifact_path is None
        assert result.metrics == {}
        assert result.event_type_metrics == {}


# ---------------------------------------------------------------------------
# EventAbnormalReturnModel
# ---------------------------------------------------------------------------


class TestEventAbnormalReturnModel:
    def test_forward_shape(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = EventAbnormalReturnModel(
            embedding_dim=8,
            hidden_dims=[8, 4],
            horizons=[1, 5, 20],
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(4, 8)
        out = model.forward(x)
        assert out.shape == (4, 3)

    def test_forward_single_horizon(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = EventAbnormalReturnModel(
            embedding_dim=4,
            hidden_dims=[4],
            horizons=[1],
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(3, 4)
        out = model.forward(x)
        assert out.shape == (3, 1)

    def test_invalid_embedding_dim(self) -> None:
        with pytest.raises(ValueError):
            EventAbnormalReturnModel(
                embedding_dim=0,
                hidden_dims=[4],
                horizons=[1],
            )

    def test_invalid_hidden_dims_empty(self) -> None:
        with pytest.raises(ValueError):
            EventAbnormalReturnModel(
                embedding_dim=4,
                hidden_dims=[],
                horizons=[1],
            )

    def test_invalid_hidden_dims_zero(self) -> None:
        with pytest.raises(ValueError):
            EventAbnormalReturnModel(
                embedding_dim=4,
                hidden_dims=[0],
                horizons=[1],
            )

    def test_invalid_horizons_empty(self) -> None:
        with pytest.raises(ValueError):
            EventAbnormalReturnModel(
                embedding_dim=4,
                hidden_dims=[4],
                horizons=[],
            )

    def test_invalid_horizons_zero(self) -> None:
        with pytest.raises(ValueError):
            EventAbnormalReturnModel(
                embedding_dim=4,
                hidden_dims=[4],
                horizons=[0],
            )

    def test_invalid_dropout(self) -> None:
        with pytest.raises(ValueError):
            EventAbnormalReturnModel(
                embedding_dim=4,
                hidden_dims=[4],
                horizons=[1],
                dropout=1.0,
            )

    def test_state_dict_round_trip(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = EventAbnormalReturnModel(
            embedding_dim=4,
            hidden_dims=[4],
            horizons=[1, 5],
            dropout=0.0,
        )
        _ = model.module  # build
        sd = model.state_dict()
        assert isinstance(sd, dict)
        assert len(sd) > 0

        model2 = EventAbnormalReturnModel(
            embedding_dim=4,
            hidden_dims=[4],
            horizons=[1, 5],
            dropout=0.0,
        )
        _ = model2.module
        model2.load_state_dict(sd)
        # Both models in eval mode so BatchNorm uses running stats.
        model.eval()
        model2.eval()
        x = torch.randn(2, 4)
        # Same weights -> same output.
        out1 = model.forward(x)
        out2 = model2.forward(x)
        assert torch.allclose(out1, out2)


# ---------------------------------------------------------------------------
# EventTrainer — source hash validation
# ---------------------------------------------------------------------------


class TestSourceHashValidation:
    def test_validate_source_hash_passes(self) -> None:
        trainer = EventTrainer(_small_config(), source_hash=_SOURCE_HASH)
        trainer.validate_source_hash()  # no raise

    def test_validate_source_hash_empty_rejected(self) -> None:
        trainer = EventTrainer(_small_config(), source_hash="")
        with pytest.raises(ValueError, match="missing event source hash"):
            trainer.validate_source_hash()

    def test_validate_source_hash_whitespace_rejected(self) -> None:
        trainer = EventTrainer(_small_config(), source_hash="   ")
        with pytest.raises(ValueError, match="missing event source hash"):
            trainer.validate_source_hash()

    def test_train_fails_closed_on_missing_source_hash(self) -> None:
        trainer = EventTrainer(_small_config(), source_hash="")
        emb = _synthetic_embeddings()
        labels = _synthetic_labels()
        types = _synthetic_event_types()
        with pytest.raises(ValueError, match="missing event source hash"):
            trainer.train(emb, labels, types)

    def test_init_rejects_non_config(self) -> None:
        with pytest.raises(TypeError):
            EventTrainer("not a config", source_hash=_SOURCE_HASH)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# EventTrainer — train
# ---------------------------------------------------------------------------


class TestEventTrainerTrain:
    def test_train_returns_result(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        emb = _synthetic_embeddings()
        labels = _synthetic_labels()
        types = _synthetic_event_types()
        result = trainer.train(emb, labels, types)
        assert isinstance(result, EventTrainingResult)
        assert result.config == cfg
        assert result.source_hash == _SOURCE_HASH
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert len(result.epoch_losses) == cfg.epochs
        assert result.duration_seconds > 0

    def test_train_records_epoch_losses(self) -> None:
        cfg = _small_config(epochs=3)
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        result = trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        assert len(result.epoch_losses) == 3
        assert all(isinstance(l, float) for l in result.epoch_losses)

    def test_train_computes_per_horizon_metrics(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        result = trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        assert "h1_mse" in result.metrics
        assert "h5_mse" in result.metrics
        assert "h20_mse" in result.metrics
        assert "h1_mae" in result.metrics
        assert "h5_mae" in result.metrics
        assert "h20_mae" in result.metrics
        assert "final_loss" in result.metrics

    def test_train_computes_event_type_metrics(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        result = trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        assert isinstance(result.event_type_metrics, dict)
        assert len(result.event_type_metrics) > 0
        for et, metrics in result.event_type_metrics.items():
            assert isinstance(et, str)
            assert "h1_mse" in metrics
            assert "h5_mse" in metrics
            assert "h20_mse" in metrics

    def test_train_shadow_only_default(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        result = trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        assert result.is_shadow is True
        assert result.promotion_eligible is False

    def test_train_non_shadow(self) -> None:
        cfg = _small_config(shadow_only=False)
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        result = trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        assert result.is_shadow is False
        assert result.promotion_eligible is True

    def test_train_with_weights(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        n = 20
        weights = [1.0] * n
        result = trainer.train(
            _synthetic_embeddings(n=n),
            _synthetic_labels(n=n),
            _synthetic_event_types(n=n),
            weights=weights,
        )
        assert isinstance(result, EventTrainingResult)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_wrong_embedding_dim_rejected(self) -> None:
        cfg = _small_config(embedding_dim=8)
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        emb = _synthetic_embeddings(d=4)  # wrong dim
        with pytest.raises(ValueError):
            trainer.train(emb, _synthetic_labels(), _synthetic_event_types())

    def test_train_wrong_label_horizons_rejected(self) -> None:
        cfg = _small_config(horizons=[1, 5, 20])
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        labels = np.random.default_rng(0).standard_normal((20, 2))  # wrong
        with pytest.raises(ValueError):
            trainer.train(
                _synthetic_embeddings(), labels, _synthetic_event_types()
            )

    def test_train_mismatched_event_types_rejected(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        with pytest.raises(ValueError):
            trainer.train(
                _synthetic_embeddings(n=20),
                _synthetic_labels(n=20),
                _synthetic_event_types(n=10),
            )

    def test_train_mismatched_weights_rejected(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        with pytest.raises(ValueError):
            trainer.train(
                _synthetic_embeddings(n=20),
                _synthetic_labels(n=20),
                _synthetic_event_types(n=20),
                weights=[1.0] * 10,
            )

    def test_train_single_event_type(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        n = 20
        types = ["earnings"] * n
        result = trainer.train(
            _synthetic_embeddings(n=n),
            _synthetic_labels(n=n),
            types,
        )
        assert set(result.event_type_metrics.keys()) == {"earnings"}

    def test_train_single_horizon(self) -> None:
        cfg = _small_config(horizons=[1])
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        labels = _synthetic_labels(horizons=[1])
        result = trainer.train(
            _synthetic_embeddings(),
            labels,
            _synthetic_event_types(),
        )
        assert "h1_mse" in result.metrics
        assert "h5_mse" not in result.metrics

    def test_train_small_data(self) -> None:
        cfg = _small_config(batch_size=2, epochs=1)
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        n = 3
        result = trainer.train(
            _synthetic_embeddings(n=n),
            _synthetic_labels(n=n),
            _synthetic_event_types(n=n),
        )
        assert isinstance(result, EventTrainingResult)

    def test_train_zero_epochs(self) -> None:
        cfg = _small_config(epochs=0)
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        result = trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        assert result.epoch_losses == []
        assert np.isnan(result.final_loss)

    def test_train_gpu_status_recorded(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        result = trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        assert isinstance(result.gpu_status, GPUStatus)


# ---------------------------------------------------------------------------
# EventTrainer — predict
# ---------------------------------------------------------------------------


class TestEventTrainerPredict:
    def test_predict_shape(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        preds = trainer.predict(_synthetic_embeddings(n=5))
        assert len(preds) == 5
        assert all(len(row) == 3 for row in preds)

    def test_predict_returns_floats(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        preds = trainer.predict(_synthetic_embeddings(n=3))
        for row in preds:
            for v in row:
                assert isinstance(v, float)

    def test_predict_no_model_raises(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        with pytest.raises(ValueError, match="no trained model"):
            trainer.predict(_synthetic_embeddings(n=3))

    def test_predict_wrong_dim_raises(self) -> None:
        cfg = _small_config(embedding_dim=8)
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        with pytest.raises(ValueError):
            trainer.predict(_synthetic_embeddings(d=4))


# ---------------------------------------------------------------------------
# EventTrainer — save / load
# ---------------------------------------------------------------------------


class TestEventTrainerArtifact:
    def test_save_and_load_round_trip(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "model.pt")
            trainer.save_artifact(path)
            assert os.path.exists(path)

            trainer2 = EventTrainer(cfg, source_hash=_SOURCE_HASH)
            model = trainer2.load_artifact(path)
            assert isinstance(model, EventAbnormalReturnModel)
            assert trainer2.model_ is not None

            # Predictions should match the original trainer.
            emb = _synthetic_embeddings(n=5)
            preds1 = trainer.predict(emb)
            preds2 = trainer2.predict(emb)
            assert len(preds1) == len(preds2)
            for r1, r2 in zip(preds1, preds2):
                assert np.allclose(r1, r2, atol=1e-5)

    def test_save_without_model_raises(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(ValueError, match="no trained model"):
                trainer.save_artifact(os.path.join(d, "model.pt"))

    def test_save_creates_parent_dirs(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        trainer.train(
            _synthetic_embeddings(),
            _synthetic_labels(),
            _synthetic_event_types(),
        )
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "dir", "model.pt")
            trainer.save_artifact(path)
            assert os.path.exists(path)


# ---------------------------------------------------------------------------
# EventTrainer — OOF writing
# ---------------------------------------------------------------------------


class TestEventTrainerOOF:
    def test_write_oof_predictions(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        n = 5
        horizons = [1, 5, 20]
        fold_predictions = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
            [0.7, 0.8, 0.9],
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]
        fold_ids = [0, 0, 1, 1, 2]
        symbols = ["AAPL", "MSFT", "GOOG", "AMZN", "META"]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        labels = [
            [0.05, 0.15, 0.25],
            [0.35, 0.45, 0.55],
            [0.65, 0.75, 0.85],
            [0.05, 0.15, 0.25],
            [0.35, 0.45, 0.55],
        ]
        weights = [1.0, 1.0, 1.0, 1.0, 1.0]
        with tempfile.TemporaryDirectory() as d:
            output_path = os.path.join(d, "oof", "oof_event.json")
            uri = trainer.write_oof_predictions(
                fold_predictions=fold_predictions,
                fold_ids=fold_ids,
                symbols=symbols,
                timestamps=timestamps,
                labels=labels,
                horizons=horizons,
                weights=weights,
                output_path=output_path,
            )
            assert os.path.exists(uri)
            artifact = read_oof_artifact(uri)
            assert artifact.model_family == "event"
            # n rows * n horizons
            assert artifact.row_count == n * len(horizons)

    def test_write_oof_no_weights(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        n = 3
        horizons = [1, 5]
        fold_predictions = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        fold_ids = [0, 1, 2]
        symbols = ["AAPL", "MSFT", "GOOG"]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        labels = [[0.05, 0.15], [0.25, 0.35], [0.45, 0.55]]
        with tempfile.TemporaryDirectory() as d:
            output_path = os.path.join(d, "oof", "oof_event.json")
            uri = trainer.write_oof_predictions(
                fold_predictions=fold_predictions,
                fold_ids=fold_ids,
                symbols=symbols,
                timestamps=timestamps,
                labels=labels,
                horizons=horizons,
                weights=None,
                output_path=output_path,
            )
            artifact = read_oof_artifact(uri)
            assert artifact.row_count == n * len(horizons)

    def test_write_oof_length_mismatch_rejected(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(ValueError):
                trainer.write_oof_predictions(
                    fold_predictions=[[0.1, 0.2]],
                    fold_ids=[0, 1],  # mismatch
                    symbols=["AAPL"],
                    timestamps=["2024-01-01"],
                    labels=[[0.05, 0.15]],
                    horizons=[1, 5],
                    weights=None,
                    output_path=os.path.join(d, "oof", "oof_event.json"),
                )

    def test_write_oof_wrong_horizon_count_rejected(self) -> None:
        cfg = _small_config()
        trainer = EventTrainer(cfg, source_hash=_SOURCE_HASH)
        with tempfile.TemporaryDirectory() as d:
            with pytest.raises(ValueError):
                trainer.write_oof_predictions(
                    fold_predictions=[[0.1]],  # only 1, should be 2
                    fold_ids=[0],
                    symbols=["AAPL"],
                    timestamps=["2024-01-01"],
                    labels=[[0.05, 0.15]],
                    horizons=[1, 5],
                    weights=None,
                    output_path=os.path.join(d, "oof", "oof_event.json"),
                )


# ---------------------------------------------------------------------------
# compute_event_type_metrics
# ---------------------------------------------------------------------------


class TestComputeEventTypeMetrics:
    def test_basic(self) -> None:
        preds = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        actuals = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
        types = ["a", "b", "a"]
        result = compute_event_type_metrics(preds, actuals, types, [1, 5])
        assert "a" in result
        assert "b" in result
        assert result["a"]["h1_mse"] == 0.0
        assert result["b"]["h1_mse"] == 0.0

    def test_mse_mae_nonzero(self) -> None:
        preds = [[1.0, 2.0], [3.0, 4.0]]
        actuals = [[0.0, 0.0], [0.0, 0.0]]
        types = ["x", "x"]
        result = compute_event_type_metrics(preds, actuals, types, [1, 5])
        assert result["x"]["h1_mse"] == 5.0  # mean of 1 and 9
        assert result["x"]["h1_mae"] == 2.0  # mean of 1 and 3

    def test_single_event_type(self) -> None:
        preds = [[0.1], [0.2]]
        actuals = [[0.1], [0.2]]
        types = ["only", "only"]
        result = compute_event_type_metrics(preds, actuals, types, [1])
        assert set(result.keys()) == {"only"}

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_event_type_metrics(
                [[0.1]], [[0.1]], ["a", "b"], [1]
            )

    def test_empty_horizons_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_event_type_metrics([[0.1]], [[0.1]], ["a"], [])

    def test_multiple_event_types(self) -> None:
        preds = [[0.1], [0.2], [0.3], [0.4]]
        actuals = [[0.1], [0.2], [0.3], [0.4]]
        types = ["a", "b", "c", "a"]
        result = compute_event_type_metrics(preds, actuals, types, [1])
        assert set(result.keys()) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# compute_confidence_bucket_metrics
# ---------------------------------------------------------------------------


class TestComputeConfidenceBucketMetrics:
    def test_basic(self) -> None:
        preds = [[0.1], [0.2], [0.3], [0.4], [0.5]]
        actuals = [[0.1], [0.2], [0.3], [0.4], [0.5]]
        confs = [0.1, 0.2, 0.3, 0.4, 0.5]
        result = compute_confidence_bucket_metrics(
            preds, actuals, confs, n_buckets=5
        )
        assert len(result) == 5
        for b in range(5):
            assert f"bucket_{b}" in result
            assert "mse" in result[f"bucket_{b}"]
            assert "mae" in result[f"bucket_{b}"]
            assert "count" in result[f"bucket_{b}"]
            assert "mean_confidence" in result[f"bucket_{b}"]

    def test_count_sums_to_n(self) -> None:
        preds = [[0.1], [0.2], [0.3], [0.4], [0.5], [0.6]]
        actuals = [[0.1], [0.2], [0.3], [0.4], [0.5], [0.6]]
        confs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        result = compute_confidence_bucket_metrics(
            preds, actuals, confs, n_buckets=3
        )
        total = sum(result[f"bucket_{b}"]["count"] for b in range(3))
        assert total == 6

    def test_n_buckets_one(self) -> None:
        preds = [[0.1], [0.2]]
        actuals = [[0.1], [0.2]]
        confs = [0.5, 0.5]
        result = compute_confidence_bucket_metrics(
            preds, actuals, confs, n_buckets=1
        )
        assert len(result) == 1
        assert result["bucket_0"]["count"] == 2

    def test_length_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_confidence_bucket_metrics(
                [[0.1]], [[0.1]], [0.5, 0.6], n_buckets=2
            )

    def test_n_buckets_zero_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_confidence_bucket_metrics(
                [[0.1]], [[0.1]], [0.5], n_buckets=0
            )

    def test_perfect_predictions_zero_mse(self) -> None:
        preds = [[0.1], [0.2], [0.3]]
        actuals = [[0.1], [0.2], [0.3]]
        confs = [0.1, 0.5, 0.9]
        result = compute_confidence_bucket_metrics(
            preds, actuals, confs, n_buckets=3
        )
        for b in range(3):
            if result[f"bucket_{b}"]["count"] > 0:
                assert result[f"bucket_{b}"]["mse"] == 0.0


# ---------------------------------------------------------------------------
# validate_promotion_eligibility
# ---------------------------------------------------------------------------


class TestValidatePromotionEligibility:
    def _make_result(self, is_shadow: bool) -> EventTrainingResult:
        return EventTrainingResult(
            config=_small_config(),
            source_hash=_SOURCE_HASH,
            final_loss=0.1,
            epoch_losses=[],
            gpu_status=GPUStatus(available=False),
            is_shadow=is_shadow,
            promotion_eligible=not is_shadow,
            metrics={},
            event_type_metrics={},
            duration_seconds=1.0,
        )

    def test_shadow_no_override_not_eligible(self) -> None:
        result = self._make_result(is_shadow=True)
        assert validate_promotion_eligibility(result) is False

    def test_shadow_with_override_eligible(self) -> None:
        result = self._make_result(is_shadow=True)
        assert validate_promotion_eligibility(result, manual_override=True) is True

    def test_non_shadow_eligible(self) -> None:
        result = self._make_result(is_shadow=False)
        assert validate_promotion_eligibility(result) is True

    def test_non_shadow_override_irrelevant(self) -> None:
        result = self._make_result(is_shadow=False)
        assert validate_promotion_eligibility(result, manual_override=False) is True


# ---------------------------------------------------------------------------
# register_event_family
# ---------------------------------------------------------------------------


class TestRegisterEventFamily:
    def test_returns_dict(self) -> None:
        spec = register_event_family()
        assert isinstance(spec, dict)

    def test_family_id(self) -> None:
        spec = register_event_family()
        assert spec["family_id"] == "event"

    def test_display_name(self) -> None:
        spec = register_event_family()
        assert "Event" in spec["display_name"]

    def test_artifact_format(self) -> None:
        spec = register_event_family()
        assert spec["artifact_format"] == "torch_state_dict"

    def test_artifact_loader(self) -> None:
        spec = register_event_family()
        assert "event_trainer" in spec["artifact_loader"]

    def test_shadow_only(self) -> None:
        spec = register_event_family()
        assert spec["shadow_only"] is True

    def test_default_horizons(self) -> None:
        spec = register_event_family()
        assert spec["default_horizons"] == [1, 5, 20]

    def test_default_embedding_dim(self) -> None:
        spec = register_event_family()
        assert spec["default_embedding_dim"] == 384

    def test_required_metrics(self) -> None:
        spec = register_event_family()
        assert "h1_mse" in spec["required_metrics"]
        assert "h5_mse" in spec["required_metrics"]
        assert "h20_mse" in spec["required_metrics"]

    def test_requires_gpu_false(self) -> None:
        spec = register_event_family()
        assert spec["requires_gpu"] is False

    def test_promotion_eligibility_class(self) -> None:
        spec = register_event_family()
        assert spec["promotion_eligibility_class"] == "shadow"

    def test_created_at_ns_present(self) -> None:
        spec = register_event_family()
        assert "created_at_ns" in spec
        assert isinstance(spec["created_at_ns"], int)
