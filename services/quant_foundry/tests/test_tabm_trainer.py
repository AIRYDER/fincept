"""Tests for quant_foundry.tabm_trainer (T-9.3).

Covers the TabM research trainer: TabMConfig construction + validation,
TabMTrainingResult construction, TabMModel forward pass (k ensemble
outputs), TabMTrainer train/predict/save/load round-trip, OOF
prediction writing, promotion eligibility, family registration, and
normalization integration.

The test host is CPU-only (torch is installed with the CPU index URL),
so all training runs use ``device="cpu"``. Synthetic data is used
throughout — no real feature-lake data is touched.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("torch")

from quant_foundry.dataset_manifest import ColumnRoles
from quant_foundry.normalizer import (
    NormalizationMethod,
    Normalizer,
)
from quant_foundry.oof_artifacts import read_oof_artifact
from quant_foundry.tabm_trainer import (
    TabMConfig,
    TabMModel,
    TabMTrainer,
    TabMTrainingResult,
    register_tabm_family,
    validate_promotion_eligibility,
)
from quant_foundry.tabular_neural_runtime import GPUStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _small_config(**overrides) -> TabMConfig:
    """Build a small TabMConfig for fast CPU tests."""
    defaults = dict(
        input_dim=4,
        hidden_dims=[8, 4],
        output_dim=1,
        n_blocks=2,
        k=3,
        learning_rate=0.01,
        epochs=2,
        batch_size=8,
        dropout=0.0,
        weight_decay=0.0,
        device="cpu",
        seed=42,
        research_mode=True,
        normalization_method="standard",
    )
    defaults.update(overrides)
    return TabMConfig(**defaults)


def _synthetic_data(n: int = 20, d: int = 4, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Generate synthetic regression data."""
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n, d))
    y = X[:, 0] * 0.5 + rng.standard_normal(n) * 0.1
    return X, y


def _synthetic_df(n: int = 20, seed: int = 0) -> pd.DataFrame:
    """Generate a synthetic DataFrame with named feature columns."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "f1": rng.standard_normal(n),
            "f2": rng.standard_normal(n),
            "f3": rng.standard_normal(n),
            "f4": rng.standard_normal(n),
            "y": rng.standard_normal(n),
        }
    )


# ---------------------------------------------------------------------------
# TabMConfig
# ---------------------------------------------------------------------------


class TestTabMConfig:
    def test_default_construction(self) -> None:
        cfg = TabMConfig(input_dim=10)
        assert cfg.input_dim == 10
        assert cfg.hidden_dims == [128, 64, 32]
        assert cfg.output_dim == 1
        assert cfg.n_blocks == 5
        assert cfg.k == 32
        assert cfg.learning_rate == 0.001
        assert cfg.epochs == 100
        assert cfg.batch_size == 256
        assert cfg.dropout == 0.1
        assert cfg.weight_decay == 1e-5
        assert cfg.device == "auto"
        assert cfg.seed == 42
        assert cfg.research_mode is True
        assert cfg.normalization_method == "standard"

    def test_custom_construction(self) -> None:
        cfg = TabMConfig(
            input_dim=5,
            hidden_dims=[16, 8],
            k=10,
            epochs=50,
            research_mode=False,
            normalization_method="minmax",
        )
        assert cfg.input_dim == 5
        assert cfg.hidden_dims == [16, 8]
        assert cfg.k == 10
        assert cfg.epochs == 50
        assert cfg.research_mode is False
        assert cfg.normalization_method == "minmax"

    def test_frozen(self) -> None:
        cfg = _small_config()
        with pytest.raises(Exception):
            cfg.input_dim = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, unexpected="x")  # type: ignore[call-arg]

    def test_input_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=0)
        with pytest.raises(Exception):
            TabMConfig(input_dim=-1)

    def test_output_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, output_dim=0)

    def test_hidden_dims_nonempty(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, hidden_dims=[])

    def test_hidden_dims_positive(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, hidden_dims=[8, 0])
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, hidden_dims=[-1, 8])

    def test_k_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, k=0)
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, k=-1)

    def test_n_blocks_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, n_blocks=0)

    def test_dropout_range(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, dropout=-0.1)
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, dropout=1.0)
        # dropout == 0 is valid
        cfg = TabMConfig(input_dim=4, dropout=0.0)
        assert cfg.dropout == 0.0
        # dropout just below 1 is valid
        cfg2 = TabMConfig(input_dim=4, dropout=0.99)
        assert cfg2.dropout == 0.99

    def test_learning_rate_positive(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, learning_rate=0.0)
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, learning_rate=-0.001)

    def test_epochs_nonnegative(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, epochs=-1)
        # epochs == 0 is valid (no training)
        cfg = TabMConfig(input_dim=4, epochs=0)
        assert cfg.epochs == 0

    def test_batch_size_positive(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, batch_size=0)

    def test_weight_decay_nonnegative(self) -> None:
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, weight_decay=-1e-5)

    def test_device_allowed(self) -> None:
        for d in ("auto", "cpu", "cuda"):
            cfg = TabMConfig(input_dim=4, device=d)
            assert cfg.device == d
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, device="tpu")

    def test_normalization_method_allowed(self) -> None:
        for m in ("standard", "robust", "minmax", "none"):
            cfg = TabMConfig(input_dim=4, normalization_method=m)
            assert cfg.normalization_method == m
        with pytest.raises(Exception):
            TabMConfig(input_dim=4, normalization_method="quantile")


# ---------------------------------------------------------------------------
# TabMTrainingResult
# ---------------------------------------------------------------------------


class TestTabMTrainingResult:
    def test_construction_minimal(self) -> None:
        cfg = _small_config()
        result = TabMTrainingResult(
            config=cfg,
            final_loss=0.5,
            epoch_losses=[0.6, 0.5],
            gpu_status=GPUStatus(available=False),
            is_research=True,
            promotion_eligible=False,
            duration_seconds=1.0,
        )
        assert result.final_loss == 0.5
        assert result.epoch_losses == [0.6, 0.5]
        assert result.is_research is True
        assert result.promotion_eligible is False
        assert result.artifact_path is None
        assert result.normalizer_artifact is None
        assert result.oof_artifact_path is None
        assert result.metrics == {}

    def test_construction_full(self) -> None:
        cfg = _small_config()
        normalizer = Normalizer(method=NormalizationMethod.STANDARD)
        df = _synthetic_df()
        norm_artifact = normalizer.fit(df, ["f1", "f2"])
        result = TabMTrainingResult(
            config=cfg,
            final_loss=0.5,
            epoch_losses=[0.6, 0.5],
            gpu_status=GPUStatus(available=False),
            artifact_path="/tmp/m.pt",
            normalizer_artifact=norm_artifact,
            oof_artifact_path="/tmp/oof.json",
            is_research=False,
            promotion_eligible=True,
            metrics={"mse": 0.5, "mae": 0.6},
            duration_seconds=2.5,
        )
        assert result.artifact_path == "/tmp/m.pt"
        assert result.normalizer_artifact is not None
        assert result.oof_artifact_path == "/tmp/oof.json"
        assert result.metrics == {"mse": 0.5, "mae": 0.6}
        assert result.duration_seconds == 2.5

    def test_frozen(self) -> None:
        cfg = _small_config()
        result = TabMTrainingResult(
            config=cfg,
            final_loss=0.5,
            gpu_status=GPUStatus(available=False),
            is_research=True,
            promotion_eligible=False,
            duration_seconds=1.0,
        )
        with pytest.raises(Exception):
            result.final_loss = 0.9  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        cfg = _small_config()
        with pytest.raises(Exception):
            TabMTrainingResult(
                config=cfg,
                final_loss=0.5,
                gpu_status=GPUStatus(available=False),
                is_research=True,
                promotion_eligible=False,
                duration_seconds=1.0,
                unexpected="x",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# TabMModel
# ---------------------------------------------------------------------------


class TestTabMModel:
    def test_construction(self) -> None:
        model = TabMModel(
            input_dim=4,
            hidden_dims=[8, 4],
            output_dim=1,
            n_blocks=2,
            k=3,
            dropout=0.0,
        )
        assert model.input_dim == 4
        assert model.hidden_dims == [8, 4]
        assert model.k == 3
        assert model.n_blocks == 2

    def test_invalid_input_dim(self) -> None:
        with pytest.raises(ValueError):
            TabMModel(input_dim=0, hidden_dims=[8], k=1)

    def test_invalid_k(self) -> None:
        with pytest.raises(ValueError):
            TabMModel(input_dim=4, hidden_dims=[8], k=0)

    def test_invalid_dropout(self) -> None:
        with pytest.raises(ValueError):
            TabMModel(input_dim=4, hidden_dims=[8], k=1, dropout=1.0)

    def test_invalid_hidden_dims(self) -> None:
        with pytest.raises(ValueError):
            TabMModel(input_dim=4, hidden_dims=[], k=1)

    def test_forward_returns_k_ensemble(self) -> None:
        import torch

        model = TabMModel(
            input_dim=4,
            hidden_dims=[8, 4],
            output_dim=1,
            n_blocks=2,
            k=3,
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(8, 4)
        out = model.forward(x)
        # Shape: (batch, k, output_dim)
        assert out.shape == (8, 3, 1)

    def test_forward_k1(self) -> None:
        import torch

        model = TabMModel(
            input_dim=4,
            hidden_dims=[8],
            output_dim=1,
            n_blocks=1,
            k=1,
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(8, 4)
        out = model.forward(x)
        assert out.shape == (8, 1, 1)

    def test_forward_output_dim_2(self) -> None:
        import torch

        model = TabMModel(
            input_dim=4,
            hidden_dims=[8],
            output_dim=2,
            n_blocks=1,
            k=2,
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(8, 4)
        out = model.forward(x)
        assert out.shape == (8, 2, 2)

    def test_state_dict_roundtrip(self) -> None:
        import torch

        model = TabMModel(
            input_dim=4,
            hidden_dims=[8, 4],
            output_dim=1,
            n_blocks=2,
            k=3,
            dropout=0.0,
        )
        _ = model.module  # build
        sd = model.state_dict()
        assert isinstance(sd, dict)
        assert len(sd) > 0

        model2 = TabMModel(
            input_dim=4,
            hidden_dims=[8, 4],
            output_dim=1,
            n_blocks=2,
            k=3,
            dropout=0.0,
        )
        _ = model2.module
        model2.load_state_dict(sd)
        # Forward passes should match.
        model.eval()
        model2.eval()
        x = torch.randn(8, 4)
        with torch.no_grad():
            out1 = model.forward(x)
            out2 = model2.forward(x)
        assert torch.allclose(out1, out2)

    def test_to_and_train_eval(self) -> None:
        import torch

        model = TabMModel(
            input_dim=4,
            hidden_dims=[8],
            k=2,
            dropout=0.0,
        )
        model.to(torch.device("cpu"))
        model.train()
        assert model.module.training is True
        model.eval()
        assert model.module.training is False


# ---------------------------------------------------------------------------
# TabMTrainer.train
# ---------------------------------------------------------------------------


class TestTabMTrainerTrain:
    def test_train_returns_result(self) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert isinstance(result, TabMTrainingResult)
        assert result.config == cfg
        assert len(result.epoch_losses) == cfg.epochs
        assert result.gpu_status.available is False
        assert result.is_research is True
        assert result.promotion_eligible is False
        assert result.duration_seconds >= 0.0

    def test_train_records_epoch_losses(self) -> None:
        cfg = _small_config(epochs=3)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 3
        assert all(isinstance(l, float) for l in result.epoch_losses)

    def test_train_final_loss_matches_last_epoch(self) -> None:
        cfg = _small_config(epochs=3)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert result.final_loss == result.epoch_losses[-1]

    def test_train_research_mode_default(self) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert result.is_research is True
        assert result.promotion_eligible is False

    def test_train_non_research_mode(self) -> None:
        cfg = _small_config(research_mode=False)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert result.is_research is False
        assert result.promotion_eligible is True

    def test_train_with_weights(self) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        w = np.ones(len(y)) * 2.0
        result = trainer.train(X, y, weights=w)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_single_epoch(self) -> None:
        cfg = _small_config(epochs=1)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 1

    def test_train_zero_epochs(self) -> None:
        cfg = _small_config(epochs=0)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == 0
        assert np.isnan(result.final_loss)

    def test_train_small_data(self) -> None:
        cfg = _small_config(batch_size=4)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data(n=5)
        result = trainer.train(X, y)
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_k1(self) -> None:
        cfg = _small_config(k=1)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert result.config.k == 1
        assert len(result.epoch_losses) == cfg.epochs

    def test_train_metrics_included(self) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert "mse" in result.metrics
        assert "final_loss" in result.metrics

    def test_train_sets_model(self) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        assert trainer.model_ is not None

    def test_train_invalid_config_type(self) -> None:
        with pytest.raises(TypeError):
            TabMTrainer(config="not a config")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TabMTrainer.predict
# ---------------------------------------------------------------------------


class TestTabMTrainerPredict:
    def test_predict_after_train(self) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        preds = trainer.predict(X)
        assert isinstance(preds, list)
        assert len(preds) == X.shape[0]
        assert all(isinstance(p, float) for p in preds)

    def test_predict_without_model_raises(self) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, _ = _synthetic_data()
        with pytest.raises(ValueError):
            trainer.predict(X)

    def test_predict_averages_k_blocks(self) -> None:
        cfg = _small_config(k=4)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        preds = trainer.predict(X)
        # Predictions should be finite floats.
        assert all(np.isfinite(p) for p in preds)

    def test_predict_matches_manual_average(self) -> None:
        import torch

        cfg = _small_config(k=3)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        preds = trainer.predict(X)

        # Manually compute the ensemble average.
        model = trainer.model_
        assert model is not None
        model.eval()
        x_tensor = torch.from_numpy(X).float()
        with torch.no_grad():
            out = model.forward(x_tensor)
            manual = out.mean(dim=1).cpu().numpy().reshape(-1)
        assert np.allclose(preds, manual, atol=1e-5)


# ---------------------------------------------------------------------------
# TabMTrainer save / load
# ---------------------------------------------------------------------------


class TestTabMTrainerArtifact:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        artifact_path = str(tmp_path / "tabm_model.pt")
        trainer.save_artifact(artifact_path)
        assert os.path.exists(artifact_path)

        # Load into a new trainer.
        trainer2 = TabMTrainer(cfg)
        model = trainer2.load_artifact(artifact_path)
        assert isinstance(model, TabMModel)
        assert trainer2.model_ is not None

        # Predictions should match.
        preds1 = trainer.predict(X)
        preds2 = trainer2.predict(X)
        assert np.allclose(preds1, preds2, atol=1e-5)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        artifact_path = str(tmp_path / "nested" / "dir" / "model.pt")
        trainer.save_artifact(artifact_path)
        assert os.path.exists(artifact_path)

    def test_save_without_model_raises(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        with pytest.raises(ValueError):
            trainer.save_artifact(str(tmp_path / "m.pt"))

    def test_load_returns_model(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        artifact_path = str(tmp_path / "model.pt")
        trainer.save_artifact(artifact_path)

        trainer2 = TabMTrainer(cfg)
        model = trainer2.load_artifact(artifact_path)
        assert isinstance(model, TabMModel)
        # The loaded model should be in eval mode.
        assert model.module.training is False

    def test_load_wrong_config_raises(self, tmp_path: Path) -> None:
        cfg = _small_config(k=3)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        trainer.train(X, y)
        artifact_path = str(tmp_path / "model.pt")
        trainer.save_artifact(artifact_path)

        # Load with a different k — state_dict shapes won't match.
        cfg2 = _small_config(k=5)
        trainer2 = TabMTrainer(cfg2)
        with pytest.raises(Exception):
            trainer2.load_artifact(artifact_path)


# ---------------------------------------------------------------------------
# TabMTrainer.write_oof_predictions
# ---------------------------------------------------------------------------


class TestTabMTrainerOOF:
    def test_write_oof_predictions(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        output_path = str(tmp_path / "oof_tabm.json")
        result_path = trainer.write_oof_predictions(
            fold_predictions=[0.1, 0.2, 0.3],
            fold_ids=[0, 1, 0],
            symbols=["AAPL", "MSFT", "GOOG"],
            timestamps=["2024-01-01", "2024-01-02", "2024-01-03"],
            labels=[1.0, 2.0, 3.0],
            horizons=[5, 5, 5],
            weights=[1.0, 1.0, 1.0],
            output_path=output_path,
        )
        assert os.path.exists(result_path)
        assert result_path.endswith("oof_tabm.json")

    def test_write_oof_predictions_readable(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        output_path = str(tmp_path / "oof_tabm.json")
        trainer.write_oof_predictions(
            fold_predictions=[0.1, 0.2, 0.3],
            fold_ids=[0, 1, 0],
            symbols=["AAPL", "MSFT", "GOOG"],
            timestamps=["2024-01-01", "2024-01-02", "2024-01-03"],
            labels=[1.0, 2.0, 3.0],
            horizons=[5, 5, 5],
            weights=None,
            output_path=output_path,
        )
        artifact = read_oof_artifact(output_path)
        assert artifact.model_family == "tabm"
        assert artifact.row_count == 3
        assert artifact.fold_count == 2

    def test_write_oof_predictions_length_mismatch(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[0.1, 0.2],
                fold_ids=[0, 1, 0],
                symbols=["A", "B", "C"],
                timestamps=["t1", "t2", "t3"],
                labels=[1.0, 2.0, 3.0],
                horizons=[5, 5, 5],
                weights=None,
                output_path=str(tmp_path / "oof.json"),
            )

    def test_write_oof_predictions_weights_mismatch(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[0.1, 0.2, 0.3],
                fold_ids=[0, 1, 0],
                symbols=["A", "B", "C"],
                timestamps=["t1", "t2", "t3"],
                labels=[1.0, 2.0, 3.0],
                horizons=[5, 5, 5],
                weights=[1.0, 1.0],
                output_path=str(tmp_path / "oof.json"),
            )

    def test_write_oof_uses_oof_writer_schema(self, tmp_path: Path) -> None:
        """OOF predictions use the standard OOFWriter schema."""
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        output_path = str(tmp_path / "oof_tabm.json")
        trainer.write_oof_predictions(
            fold_predictions=[0.5],
            fold_ids=[0],
            symbols=["AAPL"],
            timestamps=["2024-01-01"],
            labels=[1.0],
            horizons=[5],
            weights=None,
            output_path=output_path,
        )
        artifact = read_oof_artifact(output_path)
        row = artifact.rows[0]
        assert row.model_family == "tabm"
        assert row.symbol == "AAPL"
        assert row.prediction == 0.5
        assert row.weight == 1.0


# ---------------------------------------------------------------------------
# validate_promotion_eligibility
# ---------------------------------------------------------------------------


class TestValidatePromotionEligibility:
    def _make_result(self, is_research: bool) -> TabMTrainingResult:
        cfg = _small_config(research_mode=is_research)
        return TabMTrainingResult(
            config=cfg,
            final_loss=0.5,
            gpu_status=GPUStatus(available=False),
            is_research=is_research,
            promotion_eligible=not is_research,
            duration_seconds=1.0,
        )

    def test_research_no_improvement_not_eligible(self) -> None:
        result = self._make_result(is_research=True)
        assert validate_promotion_eligibility(result) is False

    def test_research_positive_improvement_eligible(self) -> None:
        result = self._make_result(is_research=True)
        assert validate_promotion_eligibility(result, 0.5) is True

    def test_research_zero_improvement_not_eligible(self) -> None:
        result = self._make_result(is_research=True)
        assert validate_promotion_eligibility(result, 0.0) is False

    def test_research_negative_improvement_not_eligible(self) -> None:
        result = self._make_result(is_research=True)
        assert validate_promotion_eligibility(result, -0.1) is False

    def test_non_research_eligible_without_improvement(self) -> None:
        result = self._make_result(is_research=False)
        assert validate_promotion_eligibility(result) is True

    def test_non_research_eligible_with_improvement(self) -> None:
        result = self._make_result(is_research=False)
        assert validate_promotion_eligibility(result, 0.5) is True

    def test_non_research_eligible_with_negative_improvement(self) -> None:
        result = self._make_result(is_research=False)
        assert validate_promotion_eligibility(result, -1.0) is True


# ---------------------------------------------------------------------------
# register_tabm_family
# ---------------------------------------------------------------------------


class TestRegisterTabmFamily:
    def test_returns_dict(self) -> None:
        spec = register_tabm_family()
        assert isinstance(spec, dict)

    def test_family_id(self) -> None:
        spec = register_tabm_family()
        assert spec["family_id"] == "tabm"

    def test_display_name(self) -> None:
        spec = register_tabm_family()
        assert "TabM" in spec["display_name"]

    def test_research_mode_flag(self) -> None:
        spec = register_tabm_family()
        assert spec["research_mode"] is True

    def test_required_fields_present(self) -> None:
        spec = register_tabm_family()
        for field in (
            "family_id",
            "display_name",
            "version",
            "dataset_shape",
            "objectives",
            "artifact_format",
            "artifact_loader",
            "required_metrics",
            "runpod_image",
            "requires_gpu",
            "max_budget_cents",
            "promotion_eligibility_class",
            "is_baseline_exception",
            "created_at_ns",
        ):
            assert field in spec, f"missing field {field!r}"

    def test_is_baseline_exception_false(self) -> None:
        spec = register_tabm_family()
        assert spec["is_baseline_exception"] is False

    def test_objectives_include_regression(self) -> None:
        spec = register_tabm_family()
        assert "regression" in spec["objectives"]

    def test_artifact_loader_references_tabm(self) -> None:
        spec = register_tabm_family()
        assert "tabm" in spec["artifact_loader"].lower()

    def test_does_not_mutate_registry(self) -> None:
        """register_tabm_family returns a dict; it does not register."""
        from quant_foundry.alpha_genome import MODEL_FAMILY_REGISTRY

        before = set(MODEL_FAMILY_REGISTRY.list())
        register_tabm_family()
        after = set(MODEL_FAMILY_REGISTRY.list())
        assert before == after


# ---------------------------------------------------------------------------
# Normalization integration
# ---------------------------------------------------------------------------


class TestNormalizationIntegration:
    def test_train_fits_normalizer_with_column_roles(self) -> None:
        cfg = _small_config(input_dim=4, normalization_method="standard")
        roles = ColumnRoles(
            feature_columns=("f1", "f2", "f3", "f4"),
            label_columns=("y",),
        )
        trainer = TabMTrainer(cfg, column_roles=roles)
        df = _synthetic_df()
        result = trainer.train(df, df["y"])
        assert result.normalizer_artifact is not None
        assert result.normalizer_artifact.artifact_id.startswith("normalizer::")

    def test_train_with_explicit_normalizer_artifact(self) -> None:
        cfg = _small_config(input_dim=4)
        df = _synthetic_df()
        normalizer = Normalizer(method=NormalizationMethod.STANDARD)
        norm_artifact = normalizer.fit(df, ["f1", "f2", "f3", "f4"])

        trainer = TabMTrainer(cfg)
        result = trainer.train(df, df["y"], normalizer_artifact=norm_artifact)
        assert result.normalizer_artifact is not None
        assert result.normalizer_artifact.artifact_id == norm_artifact.artifact_id

    def test_predict_uses_stored_normalizer(self) -> None:
        cfg = _small_config(input_dim=4)
        df = _synthetic_df()
        normalizer = Normalizer(method=NormalizationMethod.STANDARD)
        norm_artifact = normalizer.fit(df, ["f1", "f2", "f3", "f4"])

        trainer = TabMTrainer(cfg)
        trainer.train(df, df["y"], normalizer_artifact=norm_artifact)
        # predict without passing the artifact — should use stored one.
        preds = trainer.predict(df)
        assert len(preds) == len(df)

    def test_no_normalizer_when_no_column_roles(self) -> None:
        cfg = _small_config(input_dim=4)
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)
        assert result.normalizer_artifact is None

    def test_normalization_method_robust(self) -> None:
        cfg = _small_config(input_dim=4, normalization_method="robust")
        roles = ColumnRoles(
            feature_columns=("f1", "f2", "f3", "f4"),
            label_columns=("y",),
        )
        trainer = TabMTrainer(cfg, column_roles=roles)
        df = _synthetic_df()
        result = trainer.train(df, df["y"])
        assert result.normalizer_artifact is not None
        # All columns should use robust method.
        for col in result.normalizer_artifact.columns:
            assert col.method == NormalizationMethod.ROBUST

    def test_normalization_method_none(self) -> None:
        cfg = _small_config(input_dim=4, normalization_method="none")
        roles = ColumnRoles(
            feature_columns=("f1", "f2", "f3", "f4"),
            label_columns=("y",),
        )
        trainer = TabMTrainer(cfg, column_roles=roles)
        df = _synthetic_df()
        result = trainer.train(df, df["y"])
        assert result.normalizer_artifact is not None
        for col in result.normalizer_artifact.columns:
            assert col.method == NormalizationMethod.NONE

    def test_predict_with_explicit_normalizer_artifact(self) -> None:
        cfg = _small_config(input_dim=4)
        df = _synthetic_df()
        normalizer = Normalizer(method=NormalizationMethod.STANDARD)
        norm_artifact = normalizer.fit(df, ["f1", "f2", "f3", "f4"])

        trainer = TabMTrainer(cfg)
        trainer.train(df, df["y"], normalizer_artifact=norm_artifact)
        # Pass the artifact explicitly to predict.
        preds = trainer.predict(df, normalizer_artifact=norm_artifact)
        assert len(preds) == len(df)


# ---------------------------------------------------------------------------
# Integration: train + save + load + predict + OOF
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_full_workflow(self, tmp_path: Path) -> None:
        """Train, save, load, predict, and write OOF end-to-end."""
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data(n=30)

        # Train.
        result = trainer.train(X, y)
        assert result.is_research is True
        assert result.promotion_eligible is False

        # Save.
        model_path = str(tmp_path / "tabm.pt")
        trainer.save_artifact(model_path)
        assert os.path.exists(model_path)

        # Load into a new trainer.
        trainer2 = TabMTrainer(cfg)
        trainer2.load_artifact(model_path)

        # Predictions match.
        preds1 = trainer.predict(X)
        preds2 = trainer2.predict(X)
        assert np.allclose(preds1, preds2, atol=1e-5)

        # Write OOF.
        oof_path = str(tmp_path / "oof_tabm.json")
        oof_result = trainer.write_oof_predictions(
            fold_predictions=preds1[:3],
            fold_ids=[0, 0, 1],
            symbols=["A", "B", "C"],
            timestamps=["2024-01-01", "2024-01-02", "2024-01-03"],
            labels=list(y[:3]),
            horizons=[5, 5, 5],
            weights=None,
            output_path=oof_path,
        )
        assert os.path.exists(oof_result)

    def test_promotion_gate_with_oof_improvement(self, tmp_path: Path) -> None:
        """A research run with positive OOF improvement is promotable."""
        cfg = _small_config()
        trainer = TabMTrainer(cfg)
        X, y = _synthetic_data()
        result = trainer.train(X, y)

        # No improvement → not eligible.
        assert validate_promotion_eligibility(result) is False
        # Positive improvement → eligible.
        assert validate_promotion_eligibility(result, 0.01) is True
