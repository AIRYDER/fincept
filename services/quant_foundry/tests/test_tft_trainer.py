"""Tests for quant_foundry.tft_trainer (T-10.4).

Covers the TFT (Temporal Fusion Transformer) canary trainer:
CovariateRole enum, CovariateRoles construction + validation (overlaps,
empty categories, target in other categories), TFTConfig construction +
validation, TFTTrainingResult construction, TFTModel forward pass
(batch input, multi-horizon output), TFTTrainer train/predict/save/load
round-trip, OOF prediction writing, promotion eligibility (shadow /
override / non-shadow), family registration, and fail-closed covariate
role validation (missing, incomplete, overlap).

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
from quant_foundry.tabular_neural_runtime import GPUStatus
from quant_foundry.tft_trainer import (
    CovariateRole,
    CovariateRoles,
    TFTConfig,
    TFTModel,
    TFTTrainer,
    TFTTrainingResult,
    register_tft_family,
    validate_promotion_eligibility,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _small_covariate_roles(**overrides) -> CovariateRoles:
    """Build a small CovariateRoles for tests."""
    defaults = dict(
        static_cols=["sector", "industry"],
        known_future_cols=["day_of_week", "is_holiday"],
        observed_cols=["price", "volume"],
        target_col="return",
    )
    defaults.update(overrides)
    return CovariateRoles(**defaults)


def _small_config(**overrides) -> TFTConfig:
    """Build a small TFTConfig for fast CPU tests."""
    defaults = dict(
        seq_len=16,
        horizon=3,
        d_model=16,
        n_heads=4,
        n_layers=2,
        ff_dim=32,
        learning_rate=0.01,
        epochs=2,
        batch_size=8,
        dropout=0.0,
        device="cpu",
        seed=42,
        shadow_only=True,
    )
    defaults.update(overrides)
    return TFTConfig(**defaults)


def _synthetic_sequences(
    n: int = 16, seq_len: int = 16, n_features: int = 6, seed: int = 0
) -> np.ndarray:
    """Generate synthetic sequence data of shape (n, seq_len, n_features)."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal((n, seq_len, n_features)).astype(np.float32)


def _synthetic_labels(n: int = 16, horizon: int = 3, seed: int = 0) -> np.ndarray:
    """Generate synthetic regression labels of shape (n, horizon)."""
    rng = np.random.default_rng(seed + 1)
    return rng.standard_normal((n, horizon)).astype(np.float32)


def _synthetic_static(n: int = 16, n_static: int = 2, seed: int = 0) -> np.ndarray:
    """Generate synthetic static covariates of shape (n, n_static)."""
    rng = np.random.default_rng(seed + 2)
    return rng.standard_normal((n, n_static)).astype(np.float32)


def _make_result(
    is_shadow: bool = True,
    promotion_eligible: bool | None = None,
) -> TFTTrainingResult:
    """Build a TFTTrainingResult for promotion-eligibility tests."""
    cfg = _small_config(shadow_only=is_shadow)
    roles = _small_covariate_roles()
    if promotion_eligible is None:
        promotion_eligible = not is_shadow
    return TFTTrainingResult(
        config=cfg,
        covariate_roles=roles,
        final_loss=0.5,
        epoch_losses=[0.6, 0.5],
        gpu_status=GPUStatus(available=False),
        artifact_path=None,
        oof_artifact_path=None,
        is_shadow=is_shadow,
        promotion_eligible=promotion_eligible,
        metrics={"mse": 0.5},
        multi_horizon_predictions=[[0.1, 0.2, 0.3]],
        duration_seconds=0.1,
    )


# ---------------------------------------------------------------------------
# CovariateRole enum
# ---------------------------------------------------------------------------


class TestCovariateRole:
    def test_enum_members(self) -> None:
        assert CovariateRole.STATIC is not None
        assert CovariateRole.KNOWN_FUTURE is not None
        assert CovariateRole.OBSERVED is not None
        assert CovariateRole.TARGET is not None

    def test_enum_values(self) -> None:
        assert CovariateRole.STATIC.value == "static"
        assert CovariateRole.KNOWN_FUTURE.value == "known_future"
        assert CovariateRole.OBSERVED.value == "observed"
        assert CovariateRole.TARGET.value == "target"

    def test_enum_has_four_members(self) -> None:
        assert len(list(CovariateRole)) == 4

    def test_enum_distinct_members(self) -> None:
        members = list(CovariateRole)
        assert len(set(members)) == 4


# ---------------------------------------------------------------------------
# CovariateRoles
# ---------------------------------------------------------------------------


class TestCovariateRoles:
    def test_construction(self) -> None:
        roles = _small_covariate_roles()
        assert roles.static_cols == ["sector", "industry"]
        assert roles.known_future_cols == ["day_of_week", "is_holiday"]
        assert roles.observed_cols == ["price", "volume"]
        assert roles.target_col == "return"

    def test_frozen(self) -> None:
        roles = _small_covariate_roles()
        with pytest.raises(Exception):
            roles.target_col = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(  # type: ignore[call-arg]
                static_cols=["a"],
                known_future_cols=["b"],
                observed_cols=["c"],
                target_col="d",
                unexpected="x",
            )

    def test_static_cols_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=[],
                known_future_cols=["b"],
                observed_cols=["c"],
                target_col="d",
            )

    def test_known_future_cols_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=[],
                observed_cols=["c"],
                target_col="d",
            )

    def test_observed_cols_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=["b"],
                observed_cols=[],
                target_col="d",
            )

    def test_target_col_empty_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=["b"],
                observed_cols=["c"],
                target_col="",
            )

    def test_overlap_static_known_future(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["x", "a"],
                known_future_cols=["x", "b"],
                observed_cols=["c"],
                target_col="d",
            )

    def test_overlap_static_observed(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["x", "a"],
                known_future_cols=["b"],
                observed_cols=["x", "c"],
                target_col="d",
            )

    def test_overlap_known_future_observed(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=["x", "b"],
                observed_cols=["x", "c"],
                target_col="d",
            )

    def test_target_in_static_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["return", "a"],
                known_future_cols=["b"],
                observed_cols=["c"],
                target_col="return",
            )

    def test_target_in_known_future_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=["return", "b"],
                observed_cols=["c"],
                target_col="return",
            )

    def test_target_in_observed_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=["b"],
                observed_cols=["return", "c"],
                target_col="return",
            )

    def test_all_feature_cols(self) -> None:
        roles = _small_covariate_roles()
        all_cols = roles.all_feature_cols()
        assert all_cols == [
            "sector", "industry", "day_of_week", "is_holiday", "price", "volume"
        ]

    def test_n_features(self) -> None:
        roles = _small_covariate_roles()
        assert roles.n_features() == 6

    def test_empty_string_in_category_rejected(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a", ""],
                known_future_cols=["b"],
                observed_cols=["c"],
                target_col="d",
            )


# ---------------------------------------------------------------------------
# TFTConfig
# ---------------------------------------------------------------------------


class TestTFTConfig:
    def test_default_construction(self) -> None:
        cfg = TFTConfig(seq_len=32, horizon=5)
        assert cfg.seq_len == 32
        assert cfg.horizon == 5
        assert cfg.d_model == 64
        assert cfg.n_heads == 4
        assert cfg.n_layers == 2
        assert cfg.ff_dim == 128
        assert cfg.dropout == 0.1
        assert cfg.learning_rate == 0.001
        assert cfg.epochs == 10
        assert cfg.batch_size == 32
        assert cfg.device == "auto"
        assert cfg.seed == 42
        assert cfg.shadow_only is True

    def test_custom_construction(self) -> None:
        cfg = TFTConfig(
            seq_len=64,
            horizon=10,
            d_model=32,
            n_heads=4,
            n_layers=3,
            ff_dim=64,
            dropout=0.2,
            learning_rate=0.005,
            epochs=5,
            batch_size=16,
            device="cpu",
            seed=7,
            shadow_only=False,
        )
        assert cfg.seq_len == 64
        assert cfg.horizon == 10
        assert cfg.d_model == 32
        assert cfg.n_layers == 3
        assert cfg.shadow_only is False

    def test_frozen(self) -> None:
        cfg = _small_config()
        with pytest.raises(Exception):
            cfg.seq_len = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, unexpected="x")  # type: ignore[call-arg]

    def test_seq_len_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=0, horizon=5)
        with pytest.raises(Exception):
            TFTConfig(seq_len=-1, horizon=5)

    def test_horizon_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=0)
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=-1)

    def test_d_model_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, d_model=0)

    def test_n_heads_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, n_heads=0)

    def test_n_layers_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, n_layers=0)

    def test_ff_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, ff_dim=0)

    def test_dropout_range(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, dropout=-0.1)
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, dropout=1.0)
        # Boundaries: 0.0 ok, 0.99 ok.
        cfg = TFTConfig(seq_len=32, horizon=5, dropout=0.0)
        assert cfg.dropout == 0.0
        cfg2 = TFTConfig(seq_len=32, horizon=5, dropout=0.99)
        assert cfg2.dropout == 0.99

    def test_learning_rate_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, learning_rate=0.0)
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, learning_rate=-0.001)

    def test_epochs_nonnegative(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, epochs=-1)
        cfg = TFTConfig(seq_len=32, horizon=5, epochs=0)
        assert cfg.epochs == 0

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, batch_size=0)

    def test_device_allowed(self) -> None:
        for d in ("auto", "cpu", "cuda"):
            cfg = TFTConfig(seq_len=32, horizon=5, device=d)
            assert cfg.device == d
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, device="tpu")

    def test_d_model_divisible_by_n_heads(self) -> None:
        with pytest.raises(Exception):
            TFTConfig(seq_len=32, horizon=5, d_model=17, n_heads=4)
        # Divisible is allowed.
        cfg = TFTConfig(seq_len=32, horizon=5, d_model=16, n_heads=4)
        assert cfg.d_model == 16


# ---------------------------------------------------------------------------
# TFTTrainingResult
# ---------------------------------------------------------------------------


class TestTFTTrainingResult:
    def test_construction(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        result = TFTTrainingResult(
            config=cfg,
            covariate_roles=roles,
            final_loss=0.5,
            epoch_losses=[0.6, 0.5],
            gpu_status=GPUStatus(available=False),
            artifact_path="/tmp/model.pt",
            oof_artifact_path="/tmp/oof_tft.json",
            is_shadow=True,
            promotion_eligible=False,
            metrics={"mse": 0.5},
            multi_horizon_predictions=[[0.1, 0.2, 0.3]],
            duration_seconds=1.2,
        )
        assert result.final_loss == 0.5
        assert len(result.epoch_losses) == 2
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.artifact_path == "/tmp/model.pt"
        assert result.metrics["mse"] == 0.5
        assert result.multi_horizon_predictions == [[0.1, 0.2, 0.3]]

    def test_frozen(self) -> None:
        result = _make_result()
        with pytest.raises(Exception):
            result.final_loss = 99.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        with pytest.raises(Exception):
            TFTTrainingResult(
                config=cfg,
                covariate_roles=roles,
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
        roles = _small_covariate_roles()
        result = TFTTrainingResult(
            config=cfg,
            covariate_roles=roles,
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
        assert result.multi_horizon_predictions is None


# ---------------------------------------------------------------------------
# TFTModel
# ---------------------------------------------------------------------------


class TestTFTModel:
    def test_forward_shape(self) -> None:
        import torch

        model = TFTModel(
            n_features=6,
            n_static=2,
            seq_len=16,
            horizon=3,
            d_model=16,
            n_heads=4,
            n_layers=2,
            ff_dim=32,
            dropout=0.0,
        )
        x = torch.randn(4, 16, 6)
        static = torch.randn(4, 2)
        out = model.forward(x, static)
        assert out.shape == (4, 3, 1)

    def test_forward_single_horizon(self) -> None:
        import torch

        model = TFTModel(
            n_features=6,
            n_static=2,
            seq_len=16,
            horizon=1,
            d_model=16,
            n_heads=4,
            n_layers=2,
            ff_dim=32,
            dropout=0.0,
        )
        x = torch.randn(4, 16, 6)
        static = torch.randn(4, 2)
        out = model.forward(x, static)
        assert out.shape == (4, 1, 1)

    def test_forward_without_static(self) -> None:
        import torch

        model = TFTModel(
            n_features=6,
            n_static=0,
            seq_len=16,
            horizon=3,
            d_model=16,
            n_heads=4,
            n_layers=2,
            ff_dim=32,
            dropout=0.0,
        )
        x = torch.randn(4, 16, 6)
        out = model.forward(x, None)
        assert out.shape == (4, 3, 1)

    def test_forward_batch_of_one(self) -> None:
        import torch

        model = TFTModel(
            n_features=6,
            n_static=2,
            seq_len=16,
            horizon=3,
            d_model=16,
            n_heads=4,
            n_layers=2,
            ff_dim=32,
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(1, 16, 6)
        static = torch.randn(1, 2)
        out = model.forward(x, static)
        assert out.shape == (1, 3, 1)

    def test_forward_single_layer(self) -> None:
        import torch

        model = TFTModel(
            n_features=6,
            n_static=2,
            seq_len=16,
            horizon=3,
            d_model=16,
            n_heads=4,
            n_layers=1,
            ff_dim=32,
            dropout=0.0,
        )
        x = torch.randn(4, 16, 6)
        static = torch.randn(4, 2)
        out = model.forward(x, static)
        assert out.shape == (4, 3, 1)

    def test_invalid_construction(self) -> None:
        with pytest.raises(ValueError):
            TFTModel(
                n_features=0, n_static=2, seq_len=16, horizon=3,
                d_model=16, n_heads=4, n_layers=2, ff_dim=32,
            )
        with pytest.raises(ValueError):
            TFTModel(
                n_features=6, n_static=2, seq_len=16, horizon=0,
                d_model=16, n_heads=4, n_layers=2, ff_dim=32,
            )
        with pytest.raises(ValueError):
            TFTModel(
                n_features=6, n_static=2, seq_len=16, horizon=3,
                d_model=17, n_heads=4, n_layers=2, ff_dim=32,
            )

    def test_state_dict_round_trip(self) -> None:
        import torch

        model = TFTModel(
            n_features=6,
            n_static=2,
            seq_len=16,
            horizon=3,
            d_model=16,
            n_heads=4,
            n_layers=1,
            ff_dim=32,
            dropout=0.0,
        )
        _ = model.module
        sd = model.state_dict()
        model2 = TFTModel(
            n_features=6,
            n_static=2,
            seq_len=16,
            horizon=3,
            d_model=16,
            n_heads=4,
            n_layers=1,
            ff_dim=32,
            dropout=0.0,
        )
        _ = model2.module
        model2.load_state_dict(sd)
        model.eval()
        model2.eval()
        x = torch.randn(3, 16, 6)
        static = torch.randn(3, 2)
        torch.testing.assert_close(
            model.forward(x, static), model2.forward(x, static)
        )

    def test_to_and_eval(self) -> None:
        model = TFTModel(
            n_features=6,
            n_static=2,
            seq_len=16,
            horizon=3,
            d_model=16,
            n_heads=4,
            n_layers=1,
            ff_dim=32,
            dropout=0.0,
        )
        ret = model.to("cpu")
        assert ret is model
        ret2 = model.eval()
        assert ret2 is model


# ---------------------------------------------------------------------------
# TFTTrainer.train
# ---------------------------------------------------------------------------


class TestTFTTrainerTrain:
    def test_train_returns_result(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert isinstance(result, TFTTrainingResult)
        assert result.config is cfg
        assert result.covariate_roles is roles
        assert len(result.epoch_losses) == cfg.epochs
        assert result.gpu_status.available is False
        assert result.duration_seconds >= 0.0

    def test_train_shadow_default(self) -> None:
        cfg = _small_config(shadow_only=True)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert result.is_shadow is True
        assert result.promotion_eligible is False

    def test_train_non_shadow(self) -> None:
        cfg = _small_config(shadow_only=False)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert result.is_shadow is False
        assert result.promotion_eligible is True

    def test_train_records_epoch_losses(self) -> None:
        cfg = _small_config(epochs=3)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 3
        for loss in result.epoch_losses:
            assert isinstance(loss, float)

    def test_train_metrics(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert "mse" in result.metrics
        assert "final_loss" in result.metrics

    def test_train_multi_horizon_predictions(self) -> None:
        cfg = _small_config(horizon=3)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert result.multi_horizon_predictions is not None
        assert len(result.multi_horizon_predictions) == 16
        for preds in result.multi_horizon_predictions:
            assert len(preds) == 3
            for p in preds:
                assert isinstance(p, float)

    def test_train_with_static_data(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        static = _synthetic_static(n=16, n_static=2)
        result = trainer.train(X, y, static_data=static)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_single_epoch(self) -> None:
        cfg = _small_config(epochs=1)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 1

    def test_train_small_data(self) -> None:
        cfg = _small_config(batch_size=4, epochs=1)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=5, seq_len=16, n_features=6)
        y = _synthetic_labels(n=5, horizon=3)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 1

    def test_train_single_horizon(self) -> None:
        cfg = _small_config(horizon=1)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=1)
        result = trainer.train(X, y)
        assert result.multi_horizon_predictions is not None
        for preds in result.multi_horizon_predictions:
            assert len(preds) == 1

    def test_train_1d_labels_broadcast(self) -> None:
        cfg = _small_config(horizon=3)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y_1d = np.random.standard_normal(16).astype(np.float32)
        result = trainer.train(X, y_1d)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_rejects_wrong_shape(self) -> None:
        cfg = _small_config(seq_len=16)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = np.random.standard_normal((16, 16)).astype(np.float32)
        y = _synthetic_labels(n=16, horizon=3)
        with pytest.raises(ValueError):
            trainer.train(X, y)

    def test_train_rejects_wrong_seq_len(self) -> None:
        cfg = _small_config(seq_len=16)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=8, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        with pytest.raises(ValueError):
            trainer.train(X, y)

    def test_train_rejects_wrong_n_features(self) -> None:
        cfg = _small_config(seq_len=16)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=5)
        y = _synthetic_labels(n=16, horizon=3)
        with pytest.raises(ValueError):
            trainer.train(X, y)

    def test_train_rejects_bad_config_type(self) -> None:
        roles = _small_covariate_roles()
        with pytest.raises(TypeError):
            TFTTrainer(config="not a config", covariate_roles=roles)  # type: ignore[arg-type]

    def test_train_rejects_bad_roles_type(self) -> None:
        cfg = _small_config()
        with pytest.raises(TypeError):
            TFTTrainer(config=cfg, covariate_roles="not roles")  # type: ignore[arg-type]

    def test_train_zero_epochs(self) -> None:
        cfg = _small_config(epochs=0)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert result.epoch_losses == []
        assert np.isnan(result.final_loss)
        assert result.multi_horizon_predictions is None


# ---------------------------------------------------------------------------
# TFTTrainer.predict
# ---------------------------------------------------------------------------


class TestTFTTrainerPredict:
    def test_predict_after_train(self) -> None:
        cfg = _small_config(horizon=3)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        trainer.train(X, y)
        preds = trainer.predict(X)
        assert isinstance(preds, list)
        assert len(preds) == 16
        for row in preds:
            assert isinstance(row, list)
            assert len(row) == 3
            for p in row:
                assert isinstance(p, float)

    def test_predict_with_static(self) -> None:
        cfg = _small_config(horizon=3)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        static = _synthetic_static(n=16, n_static=2)
        trainer.train(X, y, static_data=static)
        preds = trainer.predict(X, static_data=static)
        assert len(preds) == 16

    def test_predict_without_model_raises(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=4, seq_len=16, n_features=6)
        with pytest.raises(ValueError):
            trainer.predict(X)

    def test_predict_shape_mismatch_raises(self) -> None:
        cfg = _small_config(seq_len=16)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        trainer.train(X, y)
        with pytest.raises(ValueError):
            trainer.predict(np.random.standard_normal((4, 16)).astype(np.float32))

    def test_predict_matches_train_count(self) -> None:
        cfg = _small_config(horizon=3)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=12, seq_len=16, n_features=6)
        y = _synthetic_labels(n=12, horizon=3)
        trainer.train(X, y)
        preds = trainer.predict(X[:5])
        assert len(preds) == 5


# ---------------------------------------------------------------------------
# TFTTrainer save / load
# ---------------------------------------------------------------------------


class TestTFTTrainerArtifact:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        static = _synthetic_static(n=16, n_static=2)
        trainer.train(X, y, static_data=static)
        artifact_path = str(tmp_path / "tft_model.pt")
        trainer.save_artifact(artifact_path)
        assert os.path.exists(artifact_path)

        trainer2 = TFTTrainer(cfg, roles)
        model = trainer2.load_artifact(artifact_path)
        assert isinstance(model, TFTModel)
        # Predictions should match the original trainer.
        preds1 = trainer.predict(X, static_data=static)
        preds2 = trainer2.predict(X, static_data=static)
        np.testing.assert_allclose(preds1, preds2, rtol=1e-5, atol=1e-5)

    def test_save_without_train_raises(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        with pytest.raises(ValueError):
            trainer.save_artifact(str(tmp_path / "model.pt"))

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        trainer.train(X, y)
        nested = tmp_path / "nested" / "dir" / "model.pt"
        trainer.save_artifact(str(nested))
        assert nested.exists()

    def test_load_returns_eval_mode(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        trainer.train(X, y)
        artifact_path = str(tmp_path / "tft_model.pt")
        trainer.save_artifact(artifact_path)

        trainer2 = TFTTrainer(cfg, roles)
        model = trainer2.load_artifact(artifact_path)
        assert not model.module.training

    def test_load_with_different_trainer_instance(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        trainer.train(X, y)
        artifact_path = str(tmp_path / "tft_model.pt")
        trainer.save_artifact(artifact_path)

        trainer2 = TFTTrainer(cfg, roles)
        trainer2.load_artifact(artifact_path)
        preds = trainer2.predict(X)
        assert len(preds) == 16


# ---------------------------------------------------------------------------
# TFTTrainer.write_oof_predictions
# ---------------------------------------------------------------------------


class TestTFTTrainerOOF:
    def test_write_oof_predictions(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        fold_predictions = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]]
        fold_ids = [0, 1, 0]
        symbols = ["AAPL", "MSFT", "GOOG"]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        labels = [1.0, 0.5, -0.5]
        horizons = [5, 5, 5]
        weights = [1.0, 1.0, 1.0]
        output_path = str(tmp_path / "oof_tft.json")
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
        assert result_path.endswith("oof_tft.json")
        assert os.path.exists(result_path)

    def test_write_oof_predictions_readback(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        fold_predictions = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]]
        fold_ids = [0, 1, 0]
        symbols = ["AAPL", "MSFT", "GOOG"]
        timestamps = ["2024-01-01", "2024-01-02", "2024-01-03"]
        labels = [1.0, 0.5, -0.5]
        horizons = [5, 5, 5]
        weights = None
        output_path = str(tmp_path / "oof_tft.json")
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
        assert artifact.model_family == "tft"
        assert artifact.row_count == 3

    def test_write_oof_predictions_length_mismatch(self, tmp_path: Path) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        output_path = str(tmp_path / "oof_tft.json")
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[[0.1], [0.2]],
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
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        output_path = str(tmp_path / "oof_tft.json")
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[[0.1], [0.2], [0.3]],
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
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        fold_predictions = [[0.1, 0.2], [0.3, 0.4]]
        fold_ids = [0, 1]
        symbols = ["AAPL", "MSFT"]
        timestamps = ["2024-01-01", "2024-01-02"]
        labels = [1.0, 0.5]
        horizons = [5, 10]
        weights = None
        output_path = str(tmp_path / "oof_tft.json")
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
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        fold_predictions = [[0.1]]
        fold_ids = [0]
        symbols = ["AAPL"]
        timestamps = ["2024-01-01"]
        labels = [1.0]
        horizons = [5]
        weights = None
        output_path = str(tmp_path / "oof_tft.json")
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
        result = _make_result(is_shadow=True, promotion_eligible=False)
        assert validate_promotion_eligibility(result, manual_override=True) is True


# ---------------------------------------------------------------------------
# register_tft_family
# ---------------------------------------------------------------------------


class TestRegisterTFTFamily:
    def test_returns_dict(self) -> None:
        spec = register_tft_family()
        assert isinstance(spec, dict)

    def test_family_id(self) -> None:
        spec = register_tft_family()
        assert spec["family_id"] == "tft"

    def test_display_name_mentions_tft(self) -> None:
        spec = register_tft_family()
        assert "TFT" in spec["display_name"]

    def test_dataset_shape_sequence(self) -> None:
        spec = register_tft_family()
        assert spec["dataset_shape"] == "sequence_windowed"

    def test_artifact_format(self) -> None:
        spec = register_tft_family()
        assert spec["artifact_format"] == "torch_state_dict"

    def test_artifact_loader_references_tft(self) -> None:
        spec = register_tft_family()
        assert "tft" in spec["artifact_loader"].lower()

    def test_required_metrics(self) -> None:
        spec = register_tft_family()
        assert "mse" in spec["required_metrics"]
        assert "final_loss" in spec["required_metrics"]

    def test_does_not_require_gpu(self) -> None:
        spec = register_tft_family()
        assert spec["requires_gpu"] is False

    def test_shadow_only_flag(self) -> None:
        spec = register_tft_family()
        assert spec["shadow_only"] is True

    def test_not_baseline_exception(self) -> None:
        spec = register_tft_family()
        assert spec["is_baseline_exception"] is False

    def test_does_not_mutate_registry(self) -> None:
        spec1 = register_tft_family()
        spec2 = register_tft_family()
        assert spec1["family_id"] == spec2["family_id"]

    def test_has_created_at_ns(self) -> None:
        spec = register_tft_family()
        assert isinstance(spec["created_at_ns"], int)
        assert spec["created_at_ns"] > 0

    def test_requires_covariate_roles(self) -> None:
        spec = register_tft_family()
        assert spec["requires_covariate_roles"] is True

    def test_multi_horizon_flag(self) -> None:
        spec = register_tft_family()
        assert spec["multi_horizon"] is True

    def test_default_hyperparams(self) -> None:
        spec = register_tft_family()
        assert spec["default_d_model"] == 64
        assert spec["default_n_heads"] == 4
        assert spec["default_n_layers"] == 2
        assert spec["default_ff_dim"] == 128


# ---------------------------------------------------------------------------
# Fail-closed covariate validation
# ---------------------------------------------------------------------------


class TestFailClosedCovariates:
    def test_validate_covariates_passes_with_complete_roles(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        # Should not raise.
        trainer.validate_covariates()

    def test_train_fails_if_roles_incomplete_empty_static(self) -> None:
        """Incomplete covariate roles (empty static) fail at construction."""
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=[],
                known_future_cols=["b"],
                observed_cols=["c"],
                target_col="d",
            )

    def test_train_fails_if_roles_incomplete_empty_known_future(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=[],
                observed_cols=["c"],
                target_col="d",
            )

    def test_train_fails_if_roles_incomplete_empty_observed(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=["b"],
                observed_cols=[],
                target_col="d",
            )

    def test_train_fails_if_covariate_overlap(self) -> None:
        """Overlapping covariate categories fail at construction."""
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["x", "a"],
                known_future_cols=["x", "b"],
                observed_cols=["c"],
                target_col="d",
            )

    def test_train_fails_if_target_in_static(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["return", "a"],
                known_future_cols=["b"],
                observed_cols=["c"],
                target_col="return",
            )

    def test_train_fails_if_target_in_observed(self) -> None:
        with pytest.raises(Exception):
            CovariateRoles(
                static_cols=["a"],
                known_future_cols=["b"],
                observed_cols=["return", "c"],
                target_col="return",
            )

    def test_validate_covariates_rejects_none(self) -> None:
        """validate_covariates fails-closed when roles is None."""
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        # Simulate None by monkey-patching.
        trainer.covariate_roles = None  # type: ignore[assignment]
        with pytest.raises(ValueError):
            trainer.validate_covariates()

    def test_validate_covariates_rejects_wrong_type(self) -> None:
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        trainer.covariate_roles = "not roles"  # type: ignore[assignment]
        with pytest.raises(ValueError):
            trainer.validate_covariates()


# ---------------------------------------------------------------------------
# Integration / acceptance
# ---------------------------------------------------------------------------


class TestTFTIntegration:
    def test_full_train_predict_save_load_oof(self, tmp_path: Path) -> None:
        """End-to-end: train, predict, save, load, write OOF."""
        cfg = _small_config(shadow_only=True, horizon=3)
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=20, seq_len=16, n_features=6)
        y = _synthetic_labels(n=20, horizon=3)
        static = _synthetic_static(n=20, n_static=2)
        result = trainer.train(X, y, static_data=static)

        # Shadow by default.
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert validate_promotion_eligibility(result) is False
        assert validate_promotion_eligibility(result, manual_override=True) is True

        # Multi-horizon predictions.
        assert result.multi_horizon_predictions is not None
        assert len(result.multi_horizon_predictions) == 20
        for preds in result.multi_horizon_predictions:
            assert len(preds) == 3

        # Predict.
        preds = trainer.predict(X, static_data=static)
        assert len(preds) == 20

        # Save + load round-trip.
        artifact_path = str(tmp_path / "tft_model.pt")
        trainer.save_artifact(artifact_path)
        trainer2 = TFTTrainer(cfg, roles)
        trainer2.load_artifact(artifact_path)
        preds2 = trainer2.predict(X, static_data=static)
        np.testing.assert_allclose(preds, preds2, rtol=1e-5, atol=1e-5)

        # Write OOF predictions.
        oof_path = str(tmp_path / "oof_tft.json")
        oof_result = trainer.write_oof_predictions(
            fold_predictions=preds,
            fold_ids=[0] * 20,
            symbols=["AAPL"] * 20,
            timestamps=[f"2024-01-{i+1:02d}" for i in range(20)],
            labels=list(y[:, 0]),
            horizons=[5] * 20,
            weights=None,
            output_path=oof_path,
        )
        assert os.path.exists(oof_result)
        artifact = read_oof_artifact(oof_result)
        assert artifact.row_count == 20
        assert artifact.model_family == "tft"

    def test_metrics_compare_subset(self) -> None:
        """Metrics dict can be compared to a tree stack on the same subset."""
        cfg = _small_config()
        roles = _small_covariate_roles()
        trainer = TFTTrainer(cfg, roles)
        X = _synthetic_sequences(n=16, seq_len=16, n_features=6)
        y = _synthetic_labels(n=16, horizon=3)
        result = trainer.train(X, y)
        assert "mse" in result.metrics
        assert isinstance(result.metrics["mse"], float)
        tree_stack_mse = 1.0
        assert isinstance(result.metrics["mse"] - tree_stack_mse, float)
