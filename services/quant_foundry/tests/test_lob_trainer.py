"""Tests for quant_foundry.lob_trainer (T-LOB.2).

Covers the DeepLOB-style canary trainer: DeepLOBConfig construction +
validation, DeepLOBTrainingResult construction + validation, DeepLOBModel
forward pass (with synthetic LOB data), DeepLOBTrainer train/predict/
save/load round-trip, OOF prediction writing, compute_lob_metrics
(accuracy / precision / recall / f1 / directional_accuracy),
compute_spread_adjusted_return (profitable / unprofitable / break-even),
compute_fee_adjusted_return, measure_inference_latency,
validate_promotion_eligibility (shadow / override / non-shadow),
register_lob_family, fail-closed (shadow promotion, invalid config), and
edge cases (single snapshot, single class, minimal data).

The test host is CPU-only (torch is installed with the CPU index URL),
so all training runs use ``device="cpu"``. Synthetic LOB data is used
throughout — no real LOB manifest data is touched.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("torch")

from quant_foundry.lob_manifest import LOBVenue
from quant_foundry.lob_trainer import (
    DeepLOBConfig,
    DeepLOBModel,
    DeepLOBTrainer,
    DeepLOBTrainingResult,
    compute_fee_adjusted_return,
    compute_lob_metrics,
    compute_spread_adjusted_return,
    measure_inference_latency,
    register_lob_family,
    validate_promotion_eligibility,
)
from quant_foundry.oof_artifacts import read_oof_artifact
from quant_foundry.tabular_neural_runtime import GPUStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _small_config(**overrides) -> DeepLOBConfig:
    """Build a small DeepLOBConfig for fast CPU tests."""
    defaults = dict(
        n_levels=5,
        n_features=20,
        hidden_dim=16,
        n_conv_layers=2,
        n_lstm_layers=1,
        horizon=5,
        learning_rate=0.01,
        epochs=2,
        batch_size=8,
        dropout=0.0,
        device="cpu",
        seed=42,
        shadow_only=True,
        n_classes=3,
    )
    defaults.update(overrides)
    return DeepLOBConfig(**defaults)


def _synthetic_snapshots(
    seq_len: int = 16, n_features: int = 20, seed: int = 0
) -> list[list[float]]:
    """Generate synthetic LOB snapshots of shape (seq_len, n_features)."""
    rng = np.random.default_rng(seed)
    arr = rng.standard_normal((seq_len, n_features)).astype(np.float32)
    return arr.tolist()


def _synthetic_labels(seq_len: int = 16, seed: int = 1) -> list[int]:
    """Generate synthetic directional labels (0/1/2)."""
    rng = np.random.default_rng(seed)
    return [int(v) for v in rng.integers(0, 3, size=seq_len)]


def _make_result(
    is_shadow: bool = True,
    promotion_eligible: bool = False,
    **overrides,
) -> DeepLOBTrainingResult:
    """Build a DeepLOBTrainingResult for promotion-eligibility tests."""
    cfg = _small_config(shadow_only=is_shadow)
    base = dict(
        config=cfg,
        venue="NASDAQ",
        symbol="AAPL",
        final_loss=0.5,
        epoch_losses=[0.6, 0.5],
        gpu_status=GPUStatus(available=False),
        artifact_path=None,
        oof_artifact_path=None,
        is_shadow=is_shadow,
        promotion_eligible=promotion_eligible,
        metrics={"accuracy": 0.5},
        spread_adjusted_return=0.1,
        fee_adjusted_return=0.1,
        latency_ms=1.0,
        duration_seconds=0.1,
    )
    base.update(overrides)
    return DeepLOBTrainingResult(**base)


# ---------------------------------------------------------------------------
# DeepLOBConfig
# ---------------------------------------------------------------------------


class TestDeepLOBConfig:
    def test_default_construction(self) -> None:
        cfg = DeepLOBConfig()
        assert cfg.n_levels == 10
        assert cfg.n_features == 40
        assert cfg.hidden_dim == 64
        assert cfg.n_conv_layers == 2
        assert cfg.n_lstm_layers == 1
        assert cfg.horizon == 10
        assert cfg.learning_rate == 0.001
        assert cfg.epochs == 10
        assert cfg.batch_size == 32
        assert cfg.dropout == 0.1
        assert cfg.device == "auto"
        assert cfg.seed == 42
        assert cfg.shadow_only is True
        assert cfg.n_classes == 3

    def test_custom_construction(self) -> None:
        cfg = DeepLOBConfig(
            n_levels=20,
            n_features=80,
            hidden_dim=128,
            n_conv_layers=3,
            n_lstm_layers=2,
            horizon=50,
            learning_rate=0.0005,
            epochs=20,
            batch_size=64,
            dropout=0.2,
            device="cuda",
            seed=7,
            shadow_only=False,
            n_classes=5,
        )
        assert cfg.n_levels == 20
        assert cfg.n_features == 80
        assert cfg.hidden_dim == 128
        assert cfg.n_conv_layers == 3
        assert cfg.n_lstm_layers == 2
        assert cfg.horizon == 50
        assert cfg.learning_rate == 0.0005
        assert cfg.epochs == 20
        assert cfg.batch_size == 64
        assert cfg.dropout == 0.2
        assert cfg.device == "cuda"
        assert cfg.seed == 7
        assert cfg.shadow_only is False
        assert cfg.n_classes == 5

    def test_frozen(self) -> None:
        cfg = _small_config()
        with pytest.raises(Exception):
            cfg.hidden_dim = 99  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(unknown_field=1)  # type: ignore[call-arg]

    def test_n_levels_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(n_levels=0)

    def test_n_features_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(n_features=0)

    def test_hidden_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(hidden_dim=0)

    def test_n_conv_layers_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(n_conv_layers=0)

    def test_n_lstm_layers_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(n_lstm_layers=0)

    def test_horizon_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(horizon=0)

    def test_learning_rate_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(learning_rate=0.0)

    def test_learning_rate_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(learning_rate=-0.001)

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(batch_size=0)

    def test_dropout_lower_bound(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(dropout=-0.1)

    def test_dropout_upper_bound(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(dropout=1.0)

    def test_dropout_zero_allowed(self) -> None:
        cfg = DeepLOBConfig(dropout=0.0)
        assert cfg.dropout == 0.0

    def test_n_classes_must_be_at_least_two(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(n_classes=1)

    def test_device_invalid_rejected(self) -> None:
        with pytest.raises(Exception):
            DeepLOBConfig(device="tpu")

    def test_epochs_zero_allowed(self) -> None:
        cfg = DeepLOBConfig(epochs=0)
        assert cfg.epochs == 0


# ---------------------------------------------------------------------------
# DeepLOBTrainingResult
# ---------------------------------------------------------------------------


class TestDeepLOBTrainingResult:
    def test_construction(self) -> None:
        result = _make_result()
        assert result.venue == "NASDAQ"
        assert result.symbol == "AAPL"
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.spread_adjusted_return == 0.1
        assert result.fee_adjusted_return == 0.1
        assert result.latency_ms == 1.0

    def test_frozen(self) -> None:
        result = _make_result()
        with pytest.raises(Exception):
            result.final_loss = 99.0  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            DeepLOBTrainingResult(
                config=_small_config(),
                venue="NASDAQ",
                symbol="AAPL",
                final_loss=0.5,
                gpu_status=GPUStatus(available=False),
                is_shadow=True,
                duration_seconds=0.1,
                unknown_field=1,  # type: ignore[call-arg]
            )

    def test_venue_must_be_valid(self) -> None:
        with pytest.raises(Exception):
            _make_result(venue="INVALID")

    def test_venue_accepts_all_lob_venues(self) -> None:
        for v in LOBVenue:
            result = _make_result(venue=v.value)
            assert result.venue == v.value

    def test_symbol_must_be_nonempty(self) -> None:
        with pytest.raises(Exception):
            _make_result(symbol="")

    def test_symbol_whitespace_rejected(self) -> None:
        with pytest.raises(Exception):
            _make_result(symbol="   ")

    def test_optional_fields_default_none(self) -> None:
        result = DeepLOBTrainingResult(
            config=_small_config(),
            venue="NASDAQ",
            symbol="AAPL",
            final_loss=0.5,
            gpu_status=GPUStatus(available=False),
            is_shadow=True,
            duration_seconds=0.1,
        )
        assert result.artifact_path is None
        assert result.oof_artifact_path is None
        assert result.spread_adjusted_return is None
        assert result.fee_adjusted_return is None
        assert result.latency_ms is None
        assert result.promotion_eligible is False
        assert result.epoch_losses == []
        assert result.metrics == {}


# ---------------------------------------------------------------------------
# DeepLOBModel
# ---------------------------------------------------------------------------


class TestDeepLOBModel:
    def test_forward_pass_shape(self) -> None:
        import torch

        model = DeepLOBModel(
            n_features=20,
            hidden_dim=16,
            n_conv_layers=2,
            n_lstm_layers=1,
            n_classes=3,
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(4, 16, 20)
        out = model.forward(x)
        assert out.shape == (4, 16, 3)

    def test_forward_pass_single_batch(self) -> None:
        import torch

        model = DeepLOBModel(
            n_features=20,
            hidden_dim=16,
            n_conv_layers=2,
            n_lstm_layers=1,
            n_classes=3,
            dropout=0.0,
        )
        model.eval()
        x = torch.randn(1, 8, 20)
        out = model.forward(x)
        assert out.shape == (1, 8, 3)

    def test_module_cached(self) -> None:
        model = DeepLOBModel(
            n_features=10,
            hidden_dim=8,
            n_conv_layers=1,
            n_lstm_layers=1,
            n_classes=2,
        )
        m1 = model.module
        m2 = model.module
        assert m1 is m2

    def test_state_dict_round_trip(self) -> None:
        import torch

        model = DeepLOBModel(
            n_features=10,
            hidden_dim=8,
            n_conv_layers=1,
            n_lstm_layers=1,
            n_classes=2,
            dropout=0.0,
        )
        model.eval()
        _ = model.module
        sd = model.state_dict()
        assert len(sd) > 0
        model2 = DeepLOBModel(
            n_features=10,
            hidden_dim=8,
            n_conv_layers=1,
            n_lstm_layers=1,
            n_classes=2,
            dropout=0.0,
        )
        model2.eval()
        model2.load_state_dict(sd)
        x = torch.randn(2, 6, 10)
        out1 = model.forward(x)
        out2 = model2.forward(x)
        assert torch.allclose(out1, out2)

    def test_invalid_n_features(self) -> None:
        with pytest.raises(ValueError):
            DeepLOBModel(
                n_features=0,
                hidden_dim=8,
                n_conv_layers=1,
                n_lstm_layers=1,
                n_classes=3,
            )

    def test_invalid_hidden_dim(self) -> None:
        with pytest.raises(ValueError):
            DeepLOBModel(
                n_features=10,
                hidden_dim=0,
                n_conv_layers=1,
                n_lstm_layers=1,
                n_classes=3,
            )

    def test_invalid_n_classes(self) -> None:
        with pytest.raises(ValueError):
            DeepLOBModel(
                n_features=10,
                hidden_dim=8,
                n_conv_layers=1,
                n_lstm_layers=1,
                n_classes=1,
            )

    def test_invalid_dropout(self) -> None:
        with pytest.raises(ValueError):
            DeepLOBModel(
                n_features=10,
                hidden_dim=8,
                n_conv_layers=1,
                n_lstm_layers=1,
                n_classes=3,
                dropout=1.0,
            )

    def test_eval_mode(self) -> None:
        model = DeepLOBModel(
            n_features=10,
            hidden_dim=8,
            n_conv_layers=1,
            n_lstm_layers=1,
            n_classes=3,
        )
        model.eval()
        assert model.module.training is False

    def test_train_mode(self) -> None:
        model = DeepLOBModel(
            n_features=10,
            hidden_dim=8,
            n_conv_layers=1,
            n_lstm_layers=1,
            n_classes=3,
        )
        model.train()
        assert model.module.training is True


# ---------------------------------------------------------------------------
# DeepLOBTrainer
# ---------------------------------------------------------------------------


class TestDeepLOBTrainer:
    def test_init_rejects_bad_config_type(self) -> None:
        with pytest.raises(TypeError):
            DeepLOBTrainer(config="not a config", venue="NASDAQ", symbol="AAPL")  # type: ignore[arg-type]

    def test_init_rejects_bad_venue(self) -> None:
        with pytest.raises(ValueError):
            DeepLOBTrainer(
                config=_small_config(),
                venue="INVALID",
                symbol="AAPL",
            )

    def test_init_rejects_empty_symbol(self) -> None:
        with pytest.raises(ValueError):
            DeepLOBTrainer(
                config=_small_config(),
                venue="NASDAQ",
                symbol="",
            )

    def test_train_basic(self) -> None:
        cfg = _small_config(epochs=2)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=16, n_features=20)
        labels = _synthetic_labels(seq_len=16)
        result = trainer.train(snapshots, labels)
        assert result.config is cfg
        assert result.venue == "NASDAQ"
        assert result.symbol == "AAPL"
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert len(result.epoch_losses) == 2
        assert result.final_loss == result.epoch_losses[-1]
        assert result.duration_seconds > 0

    def test_train_records_metrics(self) -> None:
        cfg = _small_config(epochs=2)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=16, n_features=20)
        labels = _synthetic_labels(seq_len=16)
        result = trainer.train(snapshots, labels)
        assert "accuracy" in result.metrics
        assert "precision" in result.metrics
        assert "recall" in result.metrics
        assert "f1" in result.metrics
        assert "directional_accuracy" in result.metrics
        assert 0.0 <= result.metrics["accuracy"] <= 1.0

    def test_train_computes_cost_adjusted_returns(self) -> None:
        cfg = _small_config(epochs=2)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=16, n_features=20)
        labels = _synthetic_labels(seq_len=16)
        result = trainer.train(snapshots, labels)
        assert result.spread_adjusted_return is not None
        assert result.fee_adjusted_return is not None

    def test_train_measures_latency(self) -> None:
        cfg = _small_config(epochs=2)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=16, n_features=20)
        labels = _synthetic_labels(seq_len=16)
        result = trainer.train(snapshots, labels)
        assert result.latency_ms is not None
        assert result.latency_ms >= 0.0

    def test_train_gpu_status_recorded(self) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=8)
        result = trainer.train(snapshots, labels)
        assert isinstance(result.gpu_status, GPUStatus)

    def test_train_rejects_empty_snapshots(self) -> None:
        cfg = _small_config()
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        with pytest.raises(ValueError):
            trainer.train([], [])

    def test_train_rejects_length_mismatch(self) -> None:
        cfg = _small_config()
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=4)
        with pytest.raises(ValueError):
            trainer.train(snapshots, labels)

    def test_train_rejects_wrong_n_features(self) -> None:
        cfg = _small_config(n_features=20)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=10)
        labels = _synthetic_labels(seq_len=8)
        with pytest.raises(ValueError):
            trainer.train(snapshots, labels)

    def test_train_rejects_out_of_range_labels(self) -> None:
        cfg = _small_config(n_classes=3)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = [5] * 8  # out of range for n_classes=3
        with pytest.raises(ValueError):
            trainer.train(snapshots, labels)

    def test_train_zero_epochs(self) -> None:
        cfg = _small_config(epochs=0)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=8)
        result = trainer.train(snapshots, labels)
        assert result.epoch_losses == []
        assert result.final_loss != result.final_loss  # NaN

    def test_predict(self) -> None:
        cfg = _small_config(epochs=2)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=16, n_features=20)
        labels = _synthetic_labels(seq_len=16)
        trainer.train(snapshots, labels)
        preds = trainer.predict(snapshots)
        assert len(preds) == 16
        assert all(0 <= p < 3 for p in preds)

    def test_predict_without_model_raises(self) -> None:
        cfg = _small_config()
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        with pytest.raises(ValueError):
            trainer.predict(_synthetic_snapshots(seq_len=8, n_features=20))

    def test_predict_empty_returns_empty(self) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=8)
        trainer.train(snapshots, labels)
        assert trainer.predict([]) == []

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        cfg = _small_config(epochs=2)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=8)
        trainer.train(snapshots, labels)
        artifact = tmp_path / "deeplob.pt"
        trainer.save_artifact(str(artifact))
        assert artifact.exists()

        trainer2 = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        loaded = trainer2.load_artifact(str(artifact))
        assert isinstance(loaded, DeepLOBModel)
        preds1 = trainer.predict(snapshots)
        preds2 = trainer2.predict(snapshots)
        assert preds1 == preds2

    def test_save_without_model_raises(self, tmp_path: Path) -> None:
        cfg = _small_config()
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        with pytest.raises(ValueError):
            trainer.save_artifact(str(tmp_path / "x.pt"))

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=8)
        trainer.train(snapshots, labels)
        nested = tmp_path / "nested" / "dir" / "deeplob.pt"
        trainer.save_artifact(str(nested))
        assert nested.exists()


# ---------------------------------------------------------------------------
# OOF writing
# ---------------------------------------------------------------------------


class TestOOFWriting:
    def test_write_oof_predictions(self, tmp_path: Path) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        out_path = tmp_path / "oof" / "oof_deeplob.json"
        fold_predictions = [[0.1], [0.2], [0.3]]
        fold_ids = [0, 0, 1]
        timestamps = ["2024-01-01T00:00:00Z"] * 3
        labels = [0.0, 1.0, 2.0]
        horizons = [5, 5, 5]
        weights = [1.0, 1.0, 1.0]
        uri = trainer.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=weights,
            output_path=str(out_path),
        )
        assert Path(uri).exists()
        artifact = read_oof_artifact(uri)
        assert artifact.model_family == "deeplob"
        assert len(artifact.rows) == 3

    def test_write_oof_predictions_no_weights(self, tmp_path: Path) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        out_path = tmp_path / "oof" / "oof_deeplob.json"
        fold_predictions = [[0.1], [0.2]]
        fold_ids = [0, 1]
        timestamps = ["2024-01-01T00:00:00Z"] * 2
        labels = [0.0, 1.0]
        horizons = [5, 5]
        uri = trainer.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=None,
            output_path=str(out_path),
        )
        artifact = read_oof_artifact(uri)
        assert all(r.weight == 1.0 for r in artifact.rows)

    def test_write_oof_length_mismatch_raises(self, tmp_path: Path) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[[0.1], [0.2]],
                fold_ids=[0],
                timestamps=["t"],
                labels=[0.0],
                horizons=[5],
                weights=None,
                output_path=str(tmp_path / "oof.json"),
            )

    def test_write_oof_weights_mismatch_raises(self, tmp_path: Path) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        with pytest.raises(ValueError):
            trainer.write_oof_predictions(
                fold_predictions=[[0.1], [0.2]],
                fold_ids=[0, 1],
                timestamps=["t", "t"],
                labels=[0.0, 1.0],
                horizons=[5, 5],
                weights=[1.0],
                output_path=str(tmp_path / "oof.json"),
            )


# ---------------------------------------------------------------------------
# compute_lob_metrics
# ---------------------------------------------------------------------------


class TestComputeLOBMetrics:
    def test_perfect_predictions(self) -> None:
        preds = [0, 1, 2, 0, 1, 2]
        actuals = [0, 1, 2, 0, 1, 2]
        m = compute_lob_metrics(preds, actuals, n_classes=3)
        assert m["accuracy"] == 1.0
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0
        assert m["f1"] == 1.0
        assert m["directional_accuracy"] == 1.0

    def test_all_wrong(self) -> None:
        preds = [0, 0, 0]
        actuals = [2, 2, 2]
        m = compute_lob_metrics(preds, actuals, n_classes=3)
        assert m["accuracy"] == 0.0

    def test_partial(self) -> None:
        preds = [0, 1, 2, 0]
        actuals = [0, 1, 1, 2]
        m = compute_lob_metrics(preds, actuals, n_classes=3)
        assert 0.0 <= m["accuracy"] <= 1.0
        assert 0.0 <= m["precision"] <= 1.0
        assert 0.0 <= m["recall"] <= 1.0
        assert 0.0 <= m["f1"] <= 1.0

    def test_directional_accuracy_stationary_match(self) -> None:
        preds = [1, 1, 1]
        actuals = [1, 1, 1]
        m = compute_lob_metrics(preds, actuals, n_classes=3)
        assert m["directional_accuracy"] == 1.0

    def test_directional_accuracy_mixed(self) -> None:
        # 3 correct direction, 1 wrong (stationary vs up)
        preds = [0, 1, 2, 1]
        actuals = [0, 1, 2, 2]
        m = compute_lob_metrics(preds, actuals, n_classes=3)
        assert m["directional_accuracy"] == 0.75

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_lob_metrics([0, 1], [0], n_classes=3)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_lob_metrics([], [], n_classes=3)

    def test_n_classes_invalid(self) -> None:
        with pytest.raises(ValueError):
            compute_lob_metrics([0], [0], n_classes=0)

    def test_returns_floats(self) -> None:
        m = compute_lob_metrics([0, 1], [0, 1], n_classes=3)
        for v in m.values():
            assert isinstance(v, float)


# ---------------------------------------------------------------------------
# compute_spread_adjusted_return
# ---------------------------------------------------------------------------


class TestComputeSpreadAdjustedReturn:
    def test_profitable(self) -> None:
        # All correct -> gross = +1, spread = 1bp = 0.0001
        preds = [0, 1, 2]
        actuals = [0, 1, 2]
        r = compute_spread_adjusted_return(preds, actuals, spread_bps=1.0)
        assert r == pytest.approx(1.0 - 0.0001)

    def test_unprofitable(self) -> None:
        # All wrong -> gross = -1, spread = 1bp
        preds = [0, 0, 0]
        actuals = [2, 2, 2]
        r = compute_spread_adjusted_return(preds, actuals, spread_bps=1.0)
        assert r == pytest.approx(-1.0 - 0.0001)

    def test_break_even(self) -> None:
        # Half correct -> gross = 0, spread = 1bp
        preds = [0, 0]
        actuals = [0, 1]
        r = compute_spread_adjusted_return(preds, actuals, spread_bps=1.0)
        assert r == pytest.approx(0.0 - 0.0001)

    def test_higher_spread_reduces_return(self) -> None:
        preds = [0, 1, 2]
        actuals = [0, 1, 2]
        r_low = compute_spread_adjusted_return(preds, actuals, spread_bps=1.0)
        r_high = compute_spread_adjusted_return(preds, actuals, spread_bps=100.0)
        assert r_high < r_low

    def test_negative_spread_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_spread_adjusted_return([0], [0], spread_bps=-1.0)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_spread_adjusted_return([0, 1], [0], spread_bps=1.0)

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_spread_adjusted_return([], [], spread_bps=1.0)


# ---------------------------------------------------------------------------
# compute_fee_adjusted_return
# ---------------------------------------------------------------------------


class TestComputeFeeAdjustedReturn:
    def test_profitable(self) -> None:
        preds = [0, 1, 2]
        actuals = [0, 1, 2]
        r = compute_fee_adjusted_return(preds, actuals, fee_bps=0.5)
        assert r == pytest.approx(1.0 - 0.00005)

    def test_unprofitable(self) -> None:
        preds = [0, 0, 0]
        actuals = [2, 2, 2]
        r = compute_fee_adjusted_return(preds, actuals, fee_bps=0.5)
        assert r == pytest.approx(-1.0 - 0.00005)

    def test_higher_fee_reduces_return(self) -> None:
        preds = [0, 1, 2]
        actuals = [0, 1, 2]
        r_low = compute_fee_adjusted_return(preds, actuals, fee_bps=0.5)
        r_high = compute_fee_adjusted_return(preds, actuals, fee_bps=50.0)
        assert r_high < r_low

    def test_negative_fee_rejected(self) -> None:
        with pytest.raises(ValueError):
            compute_fee_adjusted_return([0], [0], fee_bps=-1.0)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_fee_adjusted_return([0, 1], [0], fee_bps=1.0)


# ---------------------------------------------------------------------------
# measure_inference_latency
# ---------------------------------------------------------------------------


class TestMeasureInferenceLatency:
    def test_returns_positive(self) -> None:
        model = DeepLOBModel(
            n_features=20,
            hidden_dim=16,
            n_conv_layers=2,
            n_lstm_layers=1,
            n_classes=3,
        )
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        latency = measure_inference_latency(model, snapshots, n_warmup=2)
        assert latency >= 0.0

    def test_empty_raises(self) -> None:
        model = DeepLOBModel(
            n_features=20,
            hidden_dim=16,
            n_conv_layers=2,
            n_lstm_layers=1,
            n_classes=3,
        )
        with pytest.raises(ValueError):
            measure_inference_latency(model, [], n_warmup=2)

    def test_warmup_zero(self) -> None:
        model = DeepLOBModel(
            n_features=20,
            hidden_dim=16,
            n_conv_layers=1,
            n_lstm_layers=1,
            n_classes=3,
        )
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        latency = measure_inference_latency(model, snapshots, n_warmup=0)
        assert latency >= 0.0


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
        result = _make_result(is_shadow=False, promotion_eligible=False)
        assert validate_promotion_eligibility(result) is True

    def test_non_shadow_with_override_eligible(self) -> None:
        result = _make_result(is_shadow=False, promotion_eligible=False)
        assert validate_promotion_eligibility(result, manual_override=True) is True

    def test_trainer_result_always_not_eligible(self) -> None:
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=8)
        result = trainer.train(snapshots, labels)
        assert result.promotion_eligible is False
        assert validate_promotion_eligibility(result) is False


# ---------------------------------------------------------------------------
# register_lob_family
# ---------------------------------------------------------------------------


class TestRegisterLOBFamily:
    def test_returns_dict(self) -> None:
        spec = register_lob_family()
        assert isinstance(spec, dict)

    def test_family_id(self) -> None:
        spec = register_lob_family()
        assert spec["family_id"] == "deeplob"

    def test_shadow_only_true(self) -> None:
        spec = register_lob_family()
        assert spec["shadow_only"] is True

    def test_required_fields_present(self) -> None:
        spec = register_lob_family()
        for key in (
            "family_id",
            "display_name",
            "version",
            "dataset_shape",
            "objectives",
            "artifact_format",
            "artifact_loader",
            "required_metrics",
            "requires_gpu",
            "promotion_eligibility_class",
            "is_baseline_exception",
            "created_at_ns",
            "shadow_only",
        ):
            assert key in spec, f"missing key {key}"

    def test_artifact_loader_points_to_trainer(self) -> None:
        spec = register_lob_family()
        assert "DeepLOBTrainer.load_artifact" in spec["artifact_loader"]

    def test_objectives_is_classification(self) -> None:
        spec = register_lob_family()
        assert "classification" in spec["objectives"]

    def test_does_not_require_gpu(self) -> None:
        spec = register_lob_family()
        assert spec["requires_gpu"] is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_snapshot(self) -> None:
        cfg = _small_config(epochs=1, batch_size=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=1, n_features=20)
        labels = [1]
        result = trainer.train(snapshots, labels)
        assert len(result.epoch_losses) == 1
        preds = trainer.predict(snapshots)
        assert len(preds) == 1

    def test_minimal_data_two_timesteps(self) -> None:
        cfg = _small_config(epochs=1, batch_size=2)
        trainer = DeepLOBTrainer(cfg, venue="NYSE", symbol="MSFT")
        snapshots = _synthetic_snapshots(seq_len=2, n_features=20)
        labels = [0, 2]
        result = trainer.train(snapshots, labels)
        assert result.symbol == "MSFT"
        assert result.venue == "NYSE"

    def test_single_class_in_data(self) -> None:
        # All labels are class 1 (stationary) — still valid.
        cfg = _small_config(epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = [1] * 8
        result = trainer.train(snapshots, labels)
        assert result.metrics["accuracy"] >= 0.0

    def test_two_class_config(self) -> None:
        cfg = _small_config(n_classes=2, epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = [0, 1, 0, 1, 0, 1, 0, 1]
        trainer.train(snapshots, labels)
        preds = trainer.predict(snapshots)
        assert all(p in (0, 1) for p in preds)

    def test_shadow_only_false_still_not_eligible_in_result(self) -> None:
        # Even with shadow_only=False, the result's promotion_eligible
        # is forced False by the trainer (canary is always shadow).
        cfg = _small_config(shadow_only=False, epochs=1)
        trainer = DeepLOBTrainer(cfg, venue="NASDAQ", symbol="AAPL")
        snapshots = _synthetic_snapshots(seq_len=8, n_features=20)
        labels = _synthetic_labels(seq_len=8)
        result = trainer.train(snapshots, labels)
        assert result.promotion_eligible is False

    def test_multiple_venues(self) -> None:
        for venue in ("NASDAQ", "NYSE", "CME", "BATS", "IEX"):
            cfg = _small_config(epochs=1)
            trainer = DeepLOBTrainer(cfg, venue=venue, symbol="TEST")
            snapshots = _synthetic_snapshots(seq_len=4, n_features=20)
            labels = _synthetic_labels(seq_len=4)
            result = trainer.train(snapshots, labels)
            assert result.venue == venue
