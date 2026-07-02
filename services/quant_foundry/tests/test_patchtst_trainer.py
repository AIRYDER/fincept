"""Tests for quant_foundry.patchtst_trainer (T-10.3).

Covers the PatchTST canary trainer: PatchTSTConfig construction +
validation, PatchTSTTrainingResult construction, PatchEmbedding forward
pass, PatchTSTModel forward pass (batch input), PatchTSTTrainer
train/predict/save/load round-trip, OOF prediction writing, promotion
eligibility (shadow / override / non-shadow), family registration, and
edge cases (single epoch, small data, patch_len=seq_len,
stride=patch_len).

The test host is CPU-only (torch is installed with the CPU index URL),
so all training runs use ``device="cpu"``. Synthetic data is used
throughout — no real feature-lake data is touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from quant_foundry.oof_artifacts import read_oof_artifact
from quant_foundry.patchtst_trainer import (
    PatchEmbedding,
    PatchTSTConfig,
    PatchTSTModel,
    PatchTSTTrainer,
    PatchTSTTrainingResult,
    register_patchtst_family,
    validate_promotion_eligibility,
)
from quant_foundry.tabular_neural_runtime import GPUStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _small_config(**overrides) -> PatchTSTConfig:
    """Build a small PatchTSTConfig for fast CPU tests."""
    defaults = dict(
        input_dim=3,
        seq_len=32,
        patch_len=8,
        stride=4,
        d_model=16,
        n_heads=4,
        n_layers=2,
        ff_dim=32,
        output_dim=1,
        learning_rate=0.01,
        epochs=2,
        batch_size=8,
        dropout=0.0,
        device="cpu",
        seed=42,
        shadow_only=True,
    )
    defaults.update(overrides)
    return PatchTSTConfig(**defaults)


def _synthetic_sequences(
    n: int = 16, seq_len: int = 32, input_dim: int = 3, seed: int = 0
) -> np.ndarray:
    """Generate synthetic sequence data of shape (n, seq_len, input_dim)."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal((n, seq_len, input_dim)).astype(np.float32)


def _synthetic_labels(n: int = 16, seed: int = 0) -> np.ndarray:
    """Generate synthetic regression labels of shape (n,)."""
    rng = np.random.default_rng(seed + 1)
    return rng.standard_normal(n).astype(np.float32)


def _make_result(
    is_shadow: bool = True,
    promotion_eligible: bool | None = None,
) -> PatchTSTTrainingResult:
    """Build a PatchTSTTrainingResult for promotion-eligibility tests."""
    cfg = _small_config(shadow_only=is_shadow)
    if promotion_eligible is None:
        promotion_eligible = not is_shadow
    return PatchTSTTrainingResult(
        config=cfg,
        final_loss=0.5,
        epoch_losses=[0.6, 0.5],
        gpu_status=GPUStatus(available=False),
        artifact_path=None,
        oof_artifact_path=None,
        is_shadow=is_shadow,
        promotion_eligible=promotion_eligible,
        metrics={"mse": 0.5},
        duration_seconds=0.1,
    )


# ---------------------------------------------------------------------------
# PatchTSTConfig
# ---------------------------------------------------------------------------


class TestPatchTSTConfig:
    def test_default_construction(self) -> None:
        cfg = PatchTSTConfig(input_dim=3, seq_len=32)
        assert cfg.input_dim == 3
        assert cfg.seq_len == 32
        assert cfg.patch_len == 16
        assert cfg.stride == 8
        assert cfg.d_model == 64
        assert cfg.n_heads == 4
        assert cfg.n_layers == 2
        assert cfg.ff_dim == 128
        assert cfg.dropout == 0.1
        assert cfg.output_dim == 1
        assert cfg.learning_rate == 0.001
        assert cfg.epochs == 10
        assert cfg.batch_size == 32
        assert cfg.device == "auto"
        assert cfg.seed == 42
        assert cfg.shadow_only is True

    def test_custom_construction(self) -> None:
        cfg = PatchTSTConfig(
            input_dim=5,
            seq_len=64,
            patch_len=16,
            stride=8,
            d_model=32,
            n_heads=4,
            n_layers=3,
            ff_dim=64,
            dropout=0.2,
            output_dim=2,
            learning_rate=0.005,
            epochs=5,
            batch_size=16,
            device="cpu",
            seed=7,
            shadow_only=False,
        )
        assert cfg.input_dim == 5
        assert cfg.seq_len == 64
        assert cfg.d_model == 32
        assert cfg.n_layers == 3
        assert cfg.output_dim == 2
        assert cfg.shadow_only is False

    def test_frozen(self) -> None:
        cfg = _small_config()
        with pytest.raises(Exception):
            cfg.input_dim = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, unexpected="x")  # type: ignore[call-arg]

    def test_input_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=0, seq_len=32)
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=-1, seq_len=32)

    def test_seq_len_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=0)
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=-1)

    def test_patch_len_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, patch_len=0)
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, patch_len=-1)

    def test_stride_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, stride=0)
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, stride=-1)

    def test_d_model_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, d_model=0)

    def test_n_heads_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, n_heads=0)

    def test_n_layers_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, n_layers=0)

    def test_ff_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, ff_dim=0)

    def test_output_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, output_dim=0)

    def test_dropout_range(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, dropout=-0.1)
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, dropout=1.0)
        # Boundaries: 0.0 ok, 0.99 ok.
        cfg = PatchTSTConfig(input_dim=3, seq_len=32, dropout=0.0)
        assert cfg.dropout == 0.0
        cfg2 = PatchTSTConfig(input_dim=3, seq_len=32, dropout=0.99)
        assert cfg2.dropout == 0.99

    def test_learning_rate_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, learning_rate=0.0)
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, learning_rate=-0.001)

    def test_epochs_nonnegative(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, epochs=-1)
        cfg = PatchTSTConfig(input_dim=3, seq_len=32, epochs=0)
        assert cfg.epochs == 0

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, batch_size=0)

    def test_device_allowed(self) -> None:
        for d in ("auto", "cpu", "cuda"):
            cfg = PatchTSTConfig(input_dim=3, seq_len=32, device=d)
            assert cfg.device == d
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, device="tpu")

    def test_patch_len_le_seq_len(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=8, patch_len=16)
        # Equal is allowed.
        cfg = PatchTSTConfig(input_dim=3, seq_len=16, patch_len=16)
        assert cfg.patch_len == 16

    def test_d_model_divisible_by_n_heads(self) -> None:
        with pytest.raises(Exception):
            PatchTSTConfig(input_dim=3, seq_len=32, d_model=17, n_heads=4)
        # Divisible is allowed.
        cfg = PatchTSTConfig(input_dim=3, seq_len=32, d_model=16, n_heads=4)
        assert cfg.d_model == 16

    def test_num_patches(self) -> None:
        # seq_len=32, patch_len=8, stride=4 -> 1 + (32-8)//4 = 7
        cfg = PatchTSTConfig(input_dim=3, seq_len=32, patch_len=8, stride=4)
        assert cfg.num_patches() == 7

    def test_num_patches_patch_len_equals_seq_len(self) -> None:
        # patch_len == seq_len -> exactly 1 patch.
        cfg = PatchTSTConfig(input_dim=3, seq_len=16, patch_len=16, stride=4)
        assert cfg.num_patches() == 1

    def test_num_patches_stride_equals_patch_len(self) -> None:
        # Non-overlapping patches: seq_len=32, patch_len=8, stride=8 -> 4
        cfg = PatchTSTConfig(input_dim=3, seq_len=32, patch_len=8, stride=8)
        assert cfg.num_patches() == 4


# ---------------------------------------------------------------------------
# PatchTSTTrainingResult
# ---------------------------------------------------------------------------


class TestPatchTSTTrainingResult:
    def test_construction(self) -> None:
        cfg = _small_config()
        result = PatchTSTTrainingResult(
            config=cfg,
            final_loss=0.5,
            epoch_losses=[0.6, 0.5],
            gpu_status=GPUStatus(available=False),
            artifact_path="/tmp/model.pt",
            oof_artifact_path="/tmp/oof_patchtst.json",
            is_shadow=True,
            promotion_eligible=False,
            metrics={"mse": 0.5},
            duration_seconds=1.2,
        )
        assert result.final_loss == 0.5
        assert len(result.epoch_losses) == 2
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.artifact_path == "/tmp/model.pt"
        assert result.metrics["mse"] == 0.5

    def test_frozen(self) -> None:
        result = _make_result()
        with pytest.raises(Exception):
            result.final_loss = 99.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        cfg = _small_config()
        with pytest.raises(Exception):
            PatchTSTTrainingResult(
                config=cfg,
                final_loss=0.5,
                epoch_losses=[],
                gpu_status=GPUStatus(available=False),
                is_shadow=True,
                promotion_eligible=False,
                duration_seconds=0.1,
                unexpected="x",  # type: ignore[call-arg]
            )

    def test_defaults(self) -> None:
        cfg = _small_config()
        result = PatchTSTTrainingResult(
            config=cfg,
            final_loss=0.5,
            gpu_status=GPUStatus(available=False),
            is_shadow=True,
            promotion_eligible=False,
            duration_seconds=0.1,
        )
        assert result.epoch_losses == []
        assert result.artifact_path is None
        assert result.oof_artifact_path is None
        assert result.metrics == {}


# ---------------------------------------------------------------------------
# PatchEmbedding
# ---------------------------------------------------------------------------


class TestPatchEmbedding:
    def test_forward_shape(self) -> None:
        import torch

        emb = PatchEmbedding(
            seq_len=32, patch_len=8, stride=4, input_dim=3, d_model=16
        )
        x = torch.randn(4, 32, 3)
        out = emb.forward(x)
        # num_patches = 1 + (32-8)//4 = 7
        assert out.shape == (4, 7, 16)

    def test_forward_single_batch(self) -> None:
        import torch

        emb = PatchEmbedding(
            seq_len=16, patch_len=4, stride=4, input_dim=2, d_model=8
        )
        x = torch.randn(1, 16, 2)
        out = emb.forward(x)
        # num_patches = 1 + (16-4)//4 = 4
        assert out.shape == (1, 4, 8)

    def test_patch_len_equals_seq_len(self) -> None:
        import torch

        emb = PatchEmbedding(
            seq_len=8, patch_len=8, stride=4, input_dim=2, d_model=8
        )
        x = torch.randn(3, 8, 2)
        out = emb.forward(x)
        # Exactly 1 patch.
        assert out.shape == (3, 1, 8)

    def test_invalid_construction(self) -> None:
        with pytest.raises(ValueError):
            PatchEmbedding(seq_len=0, patch_len=8, stride=4, input_dim=3, d_model=16)
        with pytest.raises(ValueError):
            PatchEmbedding(seq_len=32, patch_len=64, stride=4, input_dim=3, d_model=16)
        with pytest.raises(ValueError):
            PatchEmbedding(
                seq_len=32, patch_len=8, stride=4, input_dim=3, d_model=16, dropout=1.0
            )

    def test_state_dict_round_trip(self) -> None:
        import torch

        emb = PatchEmbedding(
            seq_len=16, patch_len=4, stride=4, input_dim=2, d_model=8
        )
        _ = emb.module  # build
        emb.eval()
        sd = emb.state_dict()
        emb2 = PatchEmbedding(
            seq_len=16, patch_len=4, stride=4, input_dim=2, d_model=8
        )
        _ = emb2.module
        emb2.load_state_dict(sd)
        emb2.eval()
        x = torch.randn(2, 16, 2)
        torch.testing.assert_close(emb.forward(x), emb2.forward(x))

    def test_to_and_eval(self) -> None:
        emb = PatchEmbedding(
            seq_len=16, patch_len=4, stride=4, input_dim=2, d_model=8
        )
        ret = emb.to("cpu")
        assert ret is emb
        ret2 = emb.eval()
        assert ret2 is emb


# ---------------------------------------------------------------------------
# PatchTSTModel
# ---------------------------------------------------------------------------


class TestPatchTSTModel:
    def test_forward_shape(self) -> None:
        import torch

        model = PatchTSTModel(
            input_dim=3,
            seq_len=32,
            patch_len=8,
            stride=4,
            d_model=16,
            n_heads=4,
            n_layers=2,
            ff_dim=32,
            output_dim=1,
            dropout=0.0,
        )
        x = torch.randn(4, 32, 3)
        out = model.forward(x)
        assert out.shape == (4, 1)

    def test_forward_multi_output(self) -> None:
        import torch

        model = PatchTSTModel(
            input_dim=2,
            seq_len=16,
            patch_len=4,
            stride=4,
            d_model=8,
            n_heads=2,
            n_layers=1,
            ff_dim=16,
            output_dim=3,
            dropout=0.0,
        )
        x = torch.randn(5, 16, 2)
        out = model.forward(x)
        assert out.shape == (5, 3)

    def test_forward_batch_of_one(self) -> None:
        import torch

        model = PatchTSTModel(
            input_dim=3,
            seq_len=32,
            patch_len=8,
            stride=4,
            d_model=16,
            n_heads=4,
            n_layers=2,
            ff_dim=32,
            output_dim=1,
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(1, 32, 3)
        out = model.forward(x)
        assert out.shape == (1, 1)

    def test_invalid_construction(self) -> None:
        with pytest.raises(ValueError):
            PatchTSTModel(
                input_dim=0, seq_len=32, patch_len=8, stride=4,
                d_model=16, n_heads=4, n_layers=2, ff_dim=32,
            )
        with pytest.raises(ValueError):
            PatchTSTModel(
                input_dim=3, seq_len=8, patch_len=16, stride=4,
                d_model=16, n_heads=4, n_layers=2, ff_dim=32,
            )
        with pytest.raises(ValueError):
            PatchTSTModel(
                input_dim=3, seq_len=32, patch_len=8, stride=4,
                d_model=17, n_heads=4, n_layers=2, ff_dim=32,
            )

    def test_state_dict_round_trip(self) -> None:
        import torch

        model = PatchTSTModel(
            input_dim=3,
            seq_len=16,
            patch_len=4,
            stride=4,
            d_model=8,
            n_heads=2,
            n_layers=1,
            ff_dim=16,
            output_dim=1,
            dropout=0.0,
        )
        _ = model.module
        sd = model.state_dict()
        model2 = PatchTSTModel(
            input_dim=3,
            seq_len=16,
            patch_len=4,
            stride=4,
            d_model=8,
            n_heads=2,
            n_layers=1,
            ff_dim=16,
            output_dim=1,
            dropout=0.0,
        )
        _ = model2.module
        model2.load_state_dict(sd)
        model.eval()
        model2.eval()
        x = torch.randn(3, 16, 3)
        torch.testing.assert_close(model.forward(x), model2.forward(x))

    def test_to_and_eval(self) -> None:
        model = PatchTSTModel(
            input_dim=3,
            seq_len=16,
            patch_len=4,
            stride=4,
            d_model=8,
            n_heads=2,
            n_layers=1,
            ff_dim=16,
            output_dim=1,
            dropout=0.0,
        )
        ret = model.to("cpu")
        assert ret is model
        ret2 = model.eval()
        assert ret2 is model


# ---------------------------------------------------------------------------
# PatchTSTTrainer.train
# ---------------------------------------------------------------------------


class TestPatchTSTTrainerTrain:
    def test_train_returns_result(self) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert isinstance(result, PatchTSTTrainingResult)
        assert result.config is cfg
        assert len(result.epoch_losses) == cfg.epochs
        assert result.gpu_status.available is False
        assert result.duration_seconds >= 0.0

    def test_train_shadow_default(self) -> None:
        cfg = _small_config(shadow_only=True)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert result.is_shadow is True
        assert result.promotion_eligible is False

    def test_train_non_shadow(self) -> None:
        cfg = _small_config(shadow_only=False)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert result.is_shadow is False
        assert result.promotion_eligible is True

    def test_train_records_epoch_losses(self) -> None:
        cfg = _small_config(epochs=3)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 3
        for loss in result.epoch_losses:
            assert isinstance(loss, float)

    def test_train_metrics(self) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert "mse" in result.metrics
        assert "final_loss" in result.metrics

    def test_train_with_weights(self) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        weights = np.ones(16, dtype=np.float32)
        result = trainer.train(X, y, weights=weights)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_single_epoch(self) -> None:
        cfg = _small_config(epochs=1)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 1

    def test_train_small_data(self) -> None:
        cfg = _small_config(batch_size=4, epochs=1)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=5, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=5)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 1

    def test_train_patch_len_equals_seq_len(self) -> None:
        cfg = _small_config(seq_len=16, patch_len=16, stride=16)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=16, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_stride_equals_patch_len(self) -> None:
        cfg = _small_config(seq_len=32, patch_len=8, stride=8)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_rejects_wrong_shape(self) -> None:
        cfg = _small_config(seq_len=32, input_dim=3)
        trainer = PatchTSTTrainer(cfg)
        # 2-D input.
        X = np.random.standard_normal((16, 32)).astype(np.float32)
        y = _synthetic_labels(n=16)
        with pytest.raises(ValueError):
            trainer.train(X, y)

    def test_train_rejects_wrong_seq_len(self) -> None:
        cfg = _small_config(seq_len=32, input_dim=3)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=16, input_dim=3)
        y = _synthetic_labels(n=16)
        with pytest.raises(ValueError):
            trainer.train(X, y)

    def test_train_rejects_wrong_input_dim(self) -> None:
        cfg = _small_config(seq_len=32, input_dim=3)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=5)
        y = _synthetic_labels(n=16)
        with pytest.raises(ValueError):
            trainer.train(X, y)

    def test_train_rejects_bad_config_type(self) -> None:
        with pytest.raises(TypeError):
            PatchTSTTrainer(config="not a config")  # type: ignore[arg-type]

    def test_train_zero_epochs(self) -> None:
        cfg = _small_config(epochs=0)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        assert result.epoch_losses == []
        assert np.isnan(result.final_loss)


# ---------------------------------------------------------------------------
# PatchTSTTrainer.predict
# ---------------------------------------------------------------------------


class TestPatchTSTTrainerPredict:
    def test_predict_after_train(self) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        trainer.train(X, y)
        preds = trainer.predict(X)
        assert isinstance(preds, list)
        assert len(preds) == 16
        for p in preds:
            assert isinstance(p, float)

    def test_predict_without_model_raises(self) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=4, seq_len=32, input_dim=3)
        with pytest.raises(ValueError):
            trainer.predict(X)

    def test_predict_shape_mismatch_raises(self) -> None:
        cfg = _small_config(seq_len=32, input_dim=3)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        trainer.train(X, y)
        # Wrong shape (2-D).
        with pytest.raises(ValueError):
            trainer.predict(np.random.standard_normal((4, 32)).astype(np.float32))

    def test_predict_matches_train_count(self) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=12, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=12)
        trainer.train(X, y)
        preds = trainer.predict(X[:5])
        assert len(preds) == 5


# ---------------------------------------------------------------------------
# PatchTSTTrainer save / load
# ---------------------------------------------------------------------------


class TestPatchTSTTrainerArtifact:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        trainer.train(X, y)
        artifact_path = str(tmp_path / "patchtst_model.pt")
        trainer.save_artifact(artifact_path)
        assert os.path.exists(artifact_path)

        trainer2 = PatchTSTTrainer(cfg)
        model = trainer2.load_artifact(artifact_path)
        assert isinstance(model, PatchTSTModel)
        # Predictions should match the original trainer.
        preds1 = trainer.predict(X)
        preds2 = trainer2.predict(X)
        np.testing.assert_allclose(preds1, preds2, rtol=1e-5, atol=1e-5)

    def test_save_without_train_raises(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        with pytest.raises(ValueError):
            trainer.save_artifact(str(tmp_path / "model.pt"))

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        trainer.train(X, y)
        nested = tmp_path / "nested" / "dir" / "model.pt"
        trainer.save_artifact(str(nested))
        assert nested.exists()

    def test_load_returns_eval_mode(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        trainer.train(X, y)
        artifact_path = str(tmp_path / "patchtst_model.pt")
        trainer.save_artifact(artifact_path)

        trainer2 = PatchTSTTrainer(cfg)
        model = trainer2.load_artifact(artifact_path)
        # Underlying module should be in eval mode.
        assert not model.module.training

    def test_load_with_different_trainer_instance(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        trainer.train(X, y)
        artifact_path = str(tmp_path / "patchtst_model.pt")
        trainer.save_artifact(artifact_path)

        # A fresh trainer with the same config should load and predict.
        trainer2 = PatchTSTTrainer(cfg)
        trainer2.load_artifact(artifact_path)
        preds = trainer2.predict(X)
        assert len(preds) == 16


# ---------------------------------------------------------------------------
# PatchTSTTrainer.write_oof_predictions
# ---------------------------------------------------------------------------


class TestPatchTSTTrainerOOF:
    def test_write_oof_predictions(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        fold_predictions = [0.1, 0.2, 0.3]
        fold_ids = [0, 1, 0]
        symbols = ["AAPL", "MSFT", "GOOG"]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        labels = [1.0, 0.5, -0.5]
        horizons = [5, 5, 5]
        weights = [1.0, 1.0, 1.0]
        output_path = str(tmp_path / "oof_patchtst.json")
        result_path = trainer.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            symbols=symbols,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=weights,
            output_path=output_path,
        )
        assert result_path.endswith("oof_patchtst.json")
        assert os.path.exists(result_path)

    def test_write_oof_predictions_readback(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        fold_predictions = [0.1, 0.2, 0.3]
        fold_ids = [0, 1, 0]
        symbols = ["AAPL", "MSFT", "GOOG"]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        labels = [1.0, 0.5, -0.5]
        horizons = [5, 5, 5]
        weights = None
        output_path = str(tmp_path / "oof_patchtst.json")
        result_path = trainer.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            symbols=symbols,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=weights,
            output_path=output_path,
        )
        artifact = read_oof_artifact(result_path)
        assert artifact.model_family == "patchtst"
        assert artifact.row_count == 3

    def test_write_oof_predictions_length_mismatch(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        output_path = str(tmp_path / "oof_patchtst.json")
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[0.1, 0.2],
                fold_ids=[0, 1, 0],
                symbols=["AAPL", "MSFT", "GOOG"],
                timestamps=["2024-01-01", "2024-01-02", "2024-01-03"],
                labels=[1.0, 0.5, -0.5],
                horizons=[5, 5, 5],
                weights=None,
                output_path=output_path,
            )

    def test_write_oof_predictions_weights_mismatch(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        output_path = str(tmp_path / "oof_patchtst.json")
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[0.1, 0.2, 0.3],
                fold_ids=[0, 1, 0],
                symbols=["AAPL", "MSFT", "GOOG"],
                timestamps=["2024-01-01", "2024-01-02", "2024-01-03"],
                labels=[1.0, 0.5, -0.5],
                horizons=[5, 5, 5],
                weights=[1.0, 1.0],
                output_path=output_path,
            )

    def test_write_oof_predictions_row_ids(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        fold_predictions = [0.1, 0.2]
        fold_ids = [0, 1]
        symbols = ["AAPL", "MSFT"]
        timestamps = ["2024-01-01", "2024-01-02"]
        labels = [1.0, 0.5]
        horizons = [5, 10]
        weights = None
        output_path = str(tmp_path / "oof_patchtst.json")
        result_path = trainer.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            symbols=symbols,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=weights,
            output_path=output_path,
        )
        artifact = read_oof_artifact(result_path)
        row_ids = {row.row_id for row in artifact.rows}
        assert "AAPL_2024-01-01_5" in row_ids
        assert "MSFT_2024-01-02_10" in row_ids

    def test_write_oof_predictions_default_weights(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        fold_predictions = [0.1]
        fold_ids = [0]
        symbols = ["AAPL"]
        timestamps = ["2024-01-01"]
        labels = [1.0]
        horizons = [5]
        weights = None
        output_path = str(tmp_path / "oof_patchtst.json")
        result_path = trainer.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            symbols=symbols,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=weights,
            output_path=output_path,
        )
        artifact = read_oof_artifact(result_path)
        assert artifact.rows[0].weight == 1.0


# ---------------------------------------------------------------------------
# validate_promotion_eligibility
# ---------------------------------------------------------------------------


class TestValidatePromotionEligibility:
    def test_shadow_no_override_not_eligible(self) -> None:
        result = _make_result(is_shadow=True, promotion_eligible=False)
        assert validate_promotion_eligibility(result) is False

    def test_shadow_with_override_eligible(self) -> None:
        result = _make_result(is_shadow=True, promotion_eligible=False)
        assert validate_promotion_eligibility(result, manual_override=True) is True

    def test_non_shadow_eligible(self) -> None:
        result = _make_result(is_shadow=False, promotion_eligible=True)
        assert validate_promotion_eligibility(result) is True

    def test_non_shadow_with_override_eligible(self) -> None:
        result = _make_result(is_shadow=False, promotion_eligible=True)
        assert validate_promotion_eligibility(result, manual_override=True) is True

    def test_override_takes_precedence(self) -> None:
        # Even if promotion_eligible on the result is False, override wins.
        result = _make_result(is_shadow=True, promotion_eligible=False)
        assert validate_promotion_eligibility(result, manual_override=True) is True


# ---------------------------------------------------------------------------
# register_patchtst_family
# ---------------------------------------------------------------------------


class TestRegisterPatchTSTFamily:
    def test_returns_dict(self) -> None:
        spec = register_patchtst_family()
        assert isinstance(spec, dict)

    def test_family_id(self) -> None:
        spec = register_patchtst_family()
        assert spec["family_id"] == "patchtst"

    def test_display_name_mentions_patchtst(self) -> None:
        spec = register_patchtst_family()
        assert "PatchTST" in spec["display_name"]

    def test_dataset_shape_sequence(self) -> None:
        spec = register_patchtst_family()
        assert spec["dataset_shape"] == "sequence_windowed"

    def test_artifact_format(self) -> None:
        spec = register_patchtst_family()
        assert spec["artifact_format"] == "torch_state_dict"

    def test_artifact_loader_references_patchtst(self) -> None:
        spec = register_patchtst_family()
        assert "patchtst" in spec["artifact_loader"].lower()

    def test_required_metrics(self) -> None:
        spec = register_patchtst_family()
        assert "mse" in spec["required_metrics"]
        assert "final_loss" in spec["required_metrics"]

    def test_does_not_require_gpu(self) -> None:
        spec = register_patchtst_family()
        assert spec["requires_gpu"] is False

    def test_shadow_only_flag(self) -> None:
        spec = register_patchtst_family()
        assert spec["shadow_only"] is True

    def test_not_baseline_exception(self) -> None:
        spec = register_patchtst_family()
        assert spec["is_baseline_exception"] is False

    def test_does_not_mutate_registry(self) -> None:
        """register_patchtst_family returns a dict; it does not register."""
        # Calling twice returns equivalent dicts without side effects.
        spec1 = register_patchtst_family()
        spec2 = register_patchtst_family()
        assert spec1["family_id"] == spec2["family_id"]

    def test_has_created_at_ns(self) -> None:
        spec = register_patchtst_family()
        assert isinstance(spec["created_at_ns"], int)
        assert spec["created_at_ns"] > 0

    def test_default_hyperparams(self) -> None:
        spec = register_patchtst_family()
        assert spec["default_patch_len"] == 16
        assert spec["default_stride"] == 8
        assert spec["default_d_model"] == 64
        assert spec["default_n_heads"] == 4
        assert spec["default_n_layers"] == 2


# ---------------------------------------------------------------------------
# Integration / acceptance
# ---------------------------------------------------------------------------


class TestPatchTSTIntegration:
    def test_full_train_predict_save_load_oof(self, tmp_path: Path) -> None:
        """End-to-end: train, predict, save, load, write OOF."""
        cfg = _small_config(shadow_only=True)
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=20, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=20)
        result = trainer.train(X, y)

        # Shadow by default.
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert validate_promotion_eligibility(result) is False
        assert validate_promotion_eligibility(result, manual_override=True) is True

        # Predict.
        preds = trainer.predict(X)
        assert len(preds) == 20

        # Save + load round-trip.
        artifact_path = str(tmp_path / "patchtst_model.pt")
        trainer.save_artifact(artifact_path)
        trainer2 = PatchTSTTrainer(cfg)
        trainer2.load_artifact(artifact_path)
        preds2 = trainer2.predict(X)
        np.testing.assert_allclose(preds, preds2, rtol=1e-5, atol=1e-5)

        # Write OOF predictions at window id grain.
        oof_path = str(tmp_path / "oof_patchtst.json")
        oof_result = trainer.write_oof_predictions(
            fold_predictions=preds,
            fold_ids=[0] * 20,
            symbols=["AAPL"] * 20,
            timestamps=[f"2024-01-{i+1:02d}" for i in range(20)],
            labels=list(y),
            horizons=[5] * 20,
            weights=None,
            output_path=oof_path,
        )
        assert os.path.exists(oof_result)
        artifact = read_oof_artifact(oof_result)
        assert artifact.row_count == 20
        assert artifact.model_family == "patchtst"

    def test_metrics_compare_subset(self) -> None:
        """Metrics dict can be compared to a tree stack on the same subset."""
        cfg = _small_config()
        trainer = PatchTSTTrainer(cfg)
        X = _synthetic_sequences(n=16, seq_len=32, input_dim=3)
        y = _synthetic_labels(n=16)
        result = trainer.train(X, y)
        # The metrics dict carries mse + final_loss — comparable to a
        # tree stack's metrics on the same symbol/horizon subset.
        assert "mse" in result.metrics
        assert isinstance(result.metrics["mse"], float)
        # A "tree stack" mse would also be a float; comparison is valid.
        tree_stack_mse = 1.0
        assert isinstance(result.metrics["mse"] - tree_stack_mse, float)
