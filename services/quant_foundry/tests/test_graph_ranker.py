"""Tests for quant_foundry.graph_ranker (T-12.4).

Covers the GNN-based graph ranker: config + result models, the graph
ranker model forward pass, training on synthetic graph data (CPU),
prediction (ranked output), artifact save/load round-trip, OOF
prediction writing, edge attribution, PIT-safe edge availability,
graph rank metrics (ndcg, MAP, Kendall's tau), promotion eligibility,
and family registration.

The test host is CPU-only (torch is installed with the CPU index URL),
so GPU-dependent assertions check the "no GPU" degradation path. All
training runs use tiny configs so they complete in well under a second
on CPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("torch")

from quant_foundry.graph_runtime import GraphSnapshot
from quant_foundry.graph_ranker import (
    GraphRanker,
    GraphRankerConfig,
    GraphRankerModel,
    GraphRankerResult,
    compute_edge_attribution,
    compute_graph_rank_metrics,
    register_graph_ranker_family,
    validate_edge_availability,
    validate_promotion_eligibility,
)
from quant_foundry.tabular_neural_runtime import GPUStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    snapshot_id: str = "snap-001",
    n_nodes: int = 4,
    n_edges: int = 6,
    node_feature_dim: int = 3,
    snapshot_time: str = "2024-01-01T00:00:00+00:00",
    edge_weights: list[float] | None = None,
) -> GraphSnapshot:
    """Build a small valid snapshot for tests."""
    node_features = [
        [float(i * node_feature_dim + j) for j in range(node_feature_dim)]
        for i in range(n_nodes)
    ]
    # Build a simple cycle of edges.
    src = [i % n_nodes for i in range(n_edges)]
    dst = [(i + 1) % n_nodes for i in range(n_edges)]
    edge_index = [src, dst]
    return GraphSnapshot.build(
        snapshot_id=snapshot_id,
        n_nodes=n_nodes,
        n_edges=n_edges,
        node_features=node_features,
        edge_index=edge_index,
        snapshot_time=snapshot_time,
        edge_weights=edge_weights,
    )


def _make_labels(n_nodes: int, seed: int = 0) -> list[float]:
    """Build deterministic labels (higher = better) for n_nodes."""
    # Deterministic but non-trivial: reverse order so node 0 is worst.
    return [float(n_nodes - i) for i in range(n_nodes)]


def _make_edge_types(n_edges: int) -> list[str]:
    """Build a deterministic list of edge types cycling through types."""
    types = ["sector", "industry", "correlation", "supply_chain"]
    return [types[i % len(types)] for i in range(n_edges)]


def _make_config(**kwargs) -> GraphRankerConfig:
    """Build a tiny config for fast CPU tests."""
    defaults = dict(
        node_feature_dim=3,
        hidden_dim=8,
        n_layers=2,
        dropout=0.0,
        learning_rate=0.01,
        epochs=2,
        batch_size=1,
        device="cpu",
        seed=42,
        shadow_only=True,
    )
    defaults.update(kwargs)
    return GraphRankerConfig(**defaults)


# ---------------------------------------------------------------------------
# GraphRankerConfig
# ---------------------------------------------------------------------------


class TestGraphRankerConfig:
    def test_defaults(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3)
        assert cfg.node_feature_dim == 3
        assert cfg.hidden_dim == 64
        assert cfg.n_layers == 2
        assert cfg.dropout == 0.1
        assert cfg.learning_rate == 0.001
        assert cfg.epochs == 10
        assert cfg.batch_size == 1
        assert cfg.device == "auto"
        assert cfg.seed == 42
        assert cfg.shadow_only is True

    def test_frozen(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3)
        with pytest.raises(Exception):
            cfg.hidden_dim = 16  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, unexpected="x")  # type: ignore[call-arg]

    def test_node_feature_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=0)
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=-1)

    def test_hidden_dim_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, hidden_dim=0)

    def test_n_layers_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, n_layers=0)

    def test_dropout_range(self) -> None:
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, dropout=-0.1)
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, dropout=1.0)
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, dropout=1.5)

    def test_dropout_zero_allowed(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3, dropout=0.0)
        assert cfg.dropout == 0.0

    def test_learning_rate_must_be_positive(self) -> None:
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, learning_rate=0.0)
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, learning_rate=-0.001)

    def test_epochs_nonnegative(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3, epochs=0)
        assert cfg.epochs == 0
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, epochs=-1)

    def test_batch_size_positive(self) -> None:
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, batch_size=0)

    def test_device_allowed(self) -> None:
        for d in ("auto", "cpu", "cuda"):
            cfg = GraphRankerConfig(node_feature_dim=3, device=d)
            assert cfg.device == d
        with pytest.raises(Exception):
            GraphRankerConfig(node_feature_dim=3, device="tpu")

    def test_custom_construction(self) -> None:
        cfg = GraphRankerConfig(
            node_feature_dim=5,
            hidden_dim=32,
            n_layers=3,
            dropout=0.2,
            learning_rate=0.005,
            epochs=20,
            batch_size=2,
            device="cuda",
            seed=123,
            shadow_only=False,
        )
        assert cfg.node_feature_dim == 5
        assert cfg.hidden_dim == 32
        assert cfg.n_layers == 3
        assert cfg.dropout == 0.2
        assert cfg.learning_rate == 0.005
        assert cfg.epochs == 20
        assert cfg.batch_size == 2
        assert cfg.device == "cuda"
        assert cfg.seed == 123
        assert cfg.shadow_only is False


# ---------------------------------------------------------------------------
# GraphRankerResult
# ---------------------------------------------------------------------------


class TestGraphRankerResult:
    def test_construction(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3)
        gpu = GPUStatus(available=False)
        result = GraphRankerResult(
            config=cfg,
            final_loss=0.5,
            epoch_losses=[0.6, 0.5],
            gpu_status=gpu,
            artifact_path="/tmp/model.pt",
            oof_artifact_path="/tmp/oof.json",
            is_shadow=True,
            promotion_eligible=False,
            metrics={"ndcg": 0.8, "map": 0.7, "kendall_tau": 0.6},
            edge_attribution={"sector": 0.1, "industry": 0.2},
            ranked_symbols=["AAPL", "MSFT", "GOOG"],
            duration_seconds=1.5,
        )
        assert result.final_loss == 0.5
        assert result.epoch_losses == [0.6, 0.5]
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.metrics["ndcg"] == 0.8
        assert result.edge_attribution["sector"] == 0.1
        assert result.ranked_symbols == ["AAPL", "MSFT", "GOOG"]
        assert result.duration_seconds == 1.5

    def test_frozen(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3)
        gpu = GPUStatus(available=False)
        result = GraphRankerResult(
            config=cfg,
            final_loss=0.5,
            gpu_status=gpu,
            is_shadow=True,
            promotion_eligible=False,
            duration_seconds=1.0,
        )
        with pytest.raises(Exception):
            result.final_loss = 0.4  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3)
        gpu = GPUStatus(available=False)
        with pytest.raises(Exception):
            GraphRankerResult(
                config=cfg,
                final_loss=0.5,
                gpu_status=gpu,
                is_shadow=True,
                promotion_eligible=False,
                duration_seconds=1.0,
                unexpected="x",  # type: ignore[call-arg]
            )

    def test_defaults(self) -> None:
        cfg = GraphRankerConfig(node_feature_dim=3)
        gpu = GPUStatus(available=False)
        result = GraphRankerResult(
            config=cfg,
            final_loss=0.5,
            gpu_status=gpu,
            is_shadow=True,
            promotion_eligible=False,
            duration_seconds=1.0,
        )
        assert result.epoch_losses == []
        assert result.artifact_path is None
        assert result.oof_artifact_path is None
        assert result.metrics == {}
        assert result.edge_attribution == {}
        assert result.ranked_symbols is None


# ---------------------------------------------------------------------------
# GraphRankerModel
# ---------------------------------------------------------------------------


class TestGraphRankerModel:
    def test_forward_pass(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        node_features = torch.tensor(
            [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]],
            dtype=torch.float32,
        )
        edge_index = torch.tensor(
            [[0, 1, 2], [1, 2, 0]], dtype=torch.long
        )
        scores = model.forward(node_features, edge_index)
        assert scores.shape == (3, 1)

    def test_forward_with_edge_weights(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        node_features = torch.tensor(
            [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]], dtype=torch.float32
        )
        edge_index = torch.tensor([[0, 1], [1, 0]], dtype=torch.long)
        edge_weight = torch.tensor([1.0, 0.5], dtype=torch.float32)
        scores = model.forward(node_features, edge_index, edge_weight)
        assert scores.shape == (2, 1)

    def test_invalid_node_feature_dim(self) -> None:
        with pytest.raises(ValueError):
            GraphRankerModel(node_feature_dim=0)

    def test_invalid_hidden_dim(self) -> None:
        with pytest.raises(ValueError):
            GraphRankerModel(node_feature_dim=3, hidden_dim=0)

    def test_invalid_n_layers(self) -> None:
        with pytest.raises(ValueError):
            GraphRankerModel(node_feature_dim=3, n_layers=0)

    def test_invalid_dropout(self) -> None:
        with pytest.raises(ValueError):
            GraphRankerModel(node_feature_dim=3, dropout=1.0)

    def test_state_dict_round_trip(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model1 = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        _ = model1.module  # build
        sd = model1.state_dict()
        model2 = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        _ = model2.module  # build
        model2.load_state_dict(sd)
        node_features = torch.tensor(
            [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]], dtype=torch.float32
        )
        edge_index = torch.tensor([[0, 1], [1, 0]], dtype=torch.long)
        s1 = model1.forward(node_features, edge_index)
        s2 = model2.forward(node_features, edge_index)
        assert torch.allclose(s1, s2)

    def test_eval_mode(self) -> None:
        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.5
        )
        model.eval()
        # In eval mode, dropout is identity.
        assert model.module.training is False

    def test_train_mode(self) -> None:
        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.5
        )
        model.train()
        assert model.module.training is True


# ---------------------------------------------------------------------------
# GraphRanker — construction
# ---------------------------------------------------------------------------


class TestGraphRankerConstruction:
    def test_construction(self) -> None:
        cfg = _make_config()
        ranker = GraphRanker(cfg, node_ids=["AAPL", "MSFT", "GOOG"])
        assert ranker.config == cfg
        assert ranker.node_ids == ["AAPL", "MSFT", "GOOG"]
        assert ranker.model_ is None

    def test_empty_node_ids(self) -> None:
        cfg = _make_config()
        with pytest.raises(ValueError):
            GraphRanker(cfg, node_ids=[])

    def test_duplicate_node_ids(self) -> None:
        cfg = _make_config()
        with pytest.raises(ValueError):
            GraphRanker(cfg, node_ids=["AAPL", "AAPL"])

    def test_non_string_node_ids(self) -> None:
        cfg = _make_config()
        with pytest.raises(ValueError):
            GraphRanker(cfg, node_ids=["AAPL", ""])  # type: ignore[list-item]

    def test_config_type_check(self) -> None:
        with pytest.raises(TypeError):
            GraphRanker("not a config", node_ids=["AAPL"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GraphRanker — train
# ---------------------------------------------------------------------------


class TestGraphRankerTrain:
    def test_train_basic(self) -> None:
        cfg = _make_config(epochs=2)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        labels = _make_labels(4)
        result = ranker.train([snap], [labels])
        assert isinstance(result, GraphRankerResult)
        assert len(result.epoch_losses) == 2
        assert result.final_loss == result.epoch_losses[-1]
        assert result.is_shadow is True
        assert result.promotion_eligible is False
        assert result.ranked_symbols is not None
        assert set(result.ranked_symbols) == set(node_ids)
        assert len(result.ranked_symbols) == 4
        assert "ndcg" in result.metrics
        assert "map" in result.metrics
        assert "kendall_tau" in result.metrics
        assert result.duration_seconds >= 0.0

    def test_train_multiple_snapshots(self) -> None:
        cfg = _make_config(epochs=3)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap1 = _make_snapshot(snapshot_id="s1", snapshot_time="2024-01-01T00:00:00+00:00")
        snap2 = _make_snapshot(snapshot_id="s2", snapshot_time="2024-01-02T00:00:00+00:00")
        labels = _make_labels(4)
        result = ranker.train([snap1, snap2], [labels, labels])
        assert len(result.epoch_losses) == 3
        assert result.ranked_symbols is not None

    def test_train_with_edge_attribution(self) -> None:
        cfg = _make_config(epochs=2)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        labels = _make_labels(4)
        edge_types = _make_edge_types(6)
        result = ranker.train([snap], [labels], edge_types_per_snapshot=[edge_types])
        assert len(result.edge_attribution) > 0
        assert all(v >= 0.0 for v in result.edge_attribution.values())

    def test_train_shadow_default(self) -> None:
        cfg = _make_config(shadow_only=True)
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=3)
        labels = _make_labels(2)
        result = ranker.train([snap], [labels])
        assert result.is_shadow is True
        assert result.promotion_eligible is False

    def test_train_non_shadow(self) -> None:
        cfg = _make_config(shadow_only=False)
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=3)
        labels = _make_labels(2)
        result = ranker.train([snap], [labels])
        assert result.is_shadow is False
        assert result.promotion_eligible is True

    def test_train_zero_epochs(self) -> None:
        cfg = _make_config(epochs=0)
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=3)
        labels = _make_labels(2)
        result = ranker.train([snap], [labels])
        assert result.epoch_losses == []
        assert result.ranked_symbols is not None

    def test_train_empty_snapshots_raises(self) -> None:
        cfg = _make_config()
        ranker = GraphRanker(cfg, node_ids=["AAPL", "MSFT"])
        with pytest.raises(ValueError):
            ranker.train([], [])

    def test_train_label_length_mismatch_raises(self) -> None:
        cfg = _make_config()
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=3)
        with pytest.raises(ValueError):
            ranker.train([snap], [[1.0]])  # wrong number of label lists

    def test_train_node_count_mismatch_raises(self) -> None:
        cfg = _make_config()
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=3, n_edges=4, node_feature_dim=3)
        labels = [1.0, 2.0, 3.0]
        with pytest.raises(ValueError):
            ranker.train([snap], [labels])

    def test_train_feature_dim_mismatch_raises(self) -> None:
        cfg = _make_config(node_feature_dim=3)
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=5)
        labels = _make_labels(2)
        with pytest.raises(ValueError):
            ranker.train([snap], [labels])

    def test_train_label_per_node_mismatch_raises(self) -> None:
        cfg = _make_config()
        node_ids = ["AAPL", "MSFT", "GOOG"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=3, n_edges=4, node_feature_dim=3)
        with pytest.raises(ValueError):
            ranker.train([snap], [[1.0, 2.0]])  # only 2 labels for 3 nodes


# ---------------------------------------------------------------------------
# GraphRanker — predict
# ---------------------------------------------------------------------------


class TestGraphRankerPredict:
    def test_predict_after_train(self) -> None:
        cfg = _make_config(epochs=2)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        labels = _make_labels(4)
        ranker.train([snap], [labels])
        ranked = ranker.predict(snap)
        assert set(ranked) == set(node_ids)
        assert len(ranked) == 4

    def test_predict_without_model_raises(self) -> None:
        cfg = _make_config()
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=3)
        with pytest.raises(ValueError):
            ranker.predict(snap)

    def test_predict_node_count_mismatch_raises(self) -> None:
        cfg = _make_config()
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=3)
        labels = _make_labels(2)
        ranker.train([snap], [labels])
        wrong_snap = _make_snapshot(n_nodes=3, n_edges=4, node_feature_dim=3)
        with pytest.raises(ValueError):
            ranker.predict(wrong_snap)

    def test_predict_is_deterministic(self) -> None:
        cfg = _make_config(epochs=2)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        labels = _make_labels(4)
        ranker.train([snap], [labels])
        ranked1 = ranker.predict(snap)
        ranked2 = ranker.predict(snap)
        assert ranked1 == ranked2


# ---------------------------------------------------------------------------
# GraphRanker — artifact save/load
# ---------------------------------------------------------------------------


class TestGraphRankerArtifact:
    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        cfg = _make_config(epochs=2)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        labels = _make_labels(4)
        ranker.train([snap], [labels])
        path = tmp_path / "model.pt"
        ranker.save_artifact(str(path))
        assert path.exists()

        # Load into a new ranker.
        ranker2 = GraphRanker(cfg, node_ids=node_ids)
        model = ranker2.load_artifact(str(path))
        assert isinstance(model, GraphRankerModel)
        assert ranker2.model_ is not None

        # Predictions should match.
        ranked1 = ranker.predict(snap)
        ranked2 = ranker2.predict(snap)
        assert ranked1 == ranked2

    def test_save_without_model_raises(self, tmp_path: Path) -> None:
        cfg = _make_config()
        ranker = GraphRanker(cfg, node_ids=["AAPL", "MSFT"])
        with pytest.raises(ValueError):
            ranker.save_artifact(str(tmp_path / "model.pt"))

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg = _make_config(epochs=1)
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=2, n_edges=2, node_feature_dim=3)
        labels = _make_labels(2)
        ranker.train([snap], [labels])
        path = tmp_path / "nested" / "dir" / "model.pt"
        ranker.save_artifact(str(path))
        assert path.exists()


# ---------------------------------------------------------------------------
# GraphRanker — OOF writing
# ---------------------------------------------------------------------------


class TestGraphRankerOOF:
    def test_write_oof_predictions(self, tmp_path: Path) -> None:
        cfg = _make_config()
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        fold_predictions = [[0.5, 0.3], [0.4, 0.2]]
        fold_ids = [0, 1]
        symbols = ["AAPL", "MSFT"]
        timestamps = ["2024-01-01T00:00:00+00:00", "2024-01-02T00:00:00+00:00"]
        labels = [0.1, 0.2]
        horizons = [5, 5]
        weights = [1.0, 1.0]
        output_path = tmp_path / "oof" / "oof_graph_ranker.json"
        result_path = ranker.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            symbols=symbols,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=weights,
            output_path=str(output_path),
        )
        assert Path(result_path).exists()
        # Verify the artifact content.
        data = json.loads(Path(result_path).read_text(encoding="utf-8"))
        assert data["model_family"] == "graph_ranker"
        assert data["row_count"] == 2

    def test_write_oof_no_weights(self, tmp_path: Path) -> None:
        cfg = _make_config()
        node_ids = ["AAPL"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        fold_predictions = [[0.5]]
        fold_ids = [0]
        symbols = ["AAPL"]
        timestamps = ["2024-01-01T00:00:00+00:00"]
        labels = [0.1]
        horizons = [5]
        output_path = tmp_path / "oof" / "oof_graph_ranker.json"
        result_path = ranker.write_oof_predictions(
            fold_predictions=fold_predictions,
            fold_ids=fold_ids,
            symbols=symbols,
            timestamps=timestamps,
            labels=labels,
            horizons=horizons,
            weights=None,
            output_path=str(output_path),
        )
        assert Path(result_path).exists()

    def test_write_oof_length_mismatch_raises(self, tmp_path: Path) -> None:
        cfg = _make_config()
        ranker = GraphRanker(cfg, node_ids=["AAPL"])
        with pytest.raises(ValueError):
            ranker.write_oof_predictions(
                fold_predictions=[[0.5]],
                fold_ids=[0, 1],  # mismatch
                symbols=["AAPL"],
                timestamps=["2024-01-01T00:00:00+00:00"],
                labels=[0.1],
                horizons=[5],
                weights=None,
                output_path=str(tmp_path / "oof.json"),
            )

    def test_write_oof_weights_mismatch_raises(self, tmp_path: Path) -> None:
        cfg = _make_config()
        ranker = GraphRanker(cfg, node_ids=["AAPL"])
        with pytest.raises(ValueError):
            ranker.write_oof_predictions(
                fold_predictions=[[0.5]],
                fold_ids=[0],
                symbols=["AAPL"],
                timestamps=["2024-01-01T00:00:00+00:00"],
                labels=[0.1],
                horizons=[5],
                weights=[1.0, 2.0],  # mismatch
                output_path=str(tmp_path / "oof.json"),
            )


# ---------------------------------------------------------------------------
# compute_edge_attribution
# ---------------------------------------------------------------------------


class TestComputeEdgeAttribution:
    def test_basic_attribution(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        model.to(torch.device("cpu"))
        model.eval()
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        edge_types = _make_edge_types(6)
        attribution = compute_edge_attribution(
            model=model, snapshot=snap, edge_types=edge_types
        )
        assert len(attribution) > 0
        assert all(v >= 0.0 for v in attribution.values())
        # All edge types present in edge_types should be in attribution.
        for etype in set(edge_types):
            assert etype in attribution

    def test_attribution_length_mismatch_raises(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        model.to(torch.device("cpu"))
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        with pytest.raises(ValueError):
            compute_edge_attribution(
                model=model, snapshot=snap, edge_types=["sector"]  # too few
            )

    def test_attribution_single_edge_type(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        model.to(torch.device("cpu"))
        snap = _make_snapshot(n_nodes=4, n_edges=6, node_feature_dim=3)
        edge_types = ["sector"] * 6
        attribution = compute_edge_attribution(
            model=model, snapshot=snap, edge_types=edge_types
        )
        assert "sector" in attribution
        # Masking all edges of the only type -> all edges removed.
        assert attribution["sector"] >= 0.0

    def test_attribution_with_edge_weights(self) -> None:
        import torch  # noqa: WPS433 lazy import

        model = GraphRankerModel(
            node_feature_dim=3, hidden_dim=8, n_layers=2, dropout=0.0
        )
        model.to(torch.device("cpu"))
        snap = _make_snapshot(
            n_nodes=4, n_edges=6, node_feature_dim=3,
            edge_weights=[1.0, 0.5, 0.8, 0.3, 0.9, 0.2],
        )
        edge_types = _make_edge_types(6)
        attribution = compute_edge_attribution(
            model=model, snapshot=snap, edge_types=edge_types
        )
        assert len(attribution) > 0


# ---------------------------------------------------------------------------
# validate_edge_availability
# ---------------------------------------------------------------------------


class TestValidateEdgeAvailability:
    def test_valid_snapshot_time(self) -> None:
        snap = _make_snapshot(snapshot_time="2024-01-01T00:00:00+00:00")
        assert validate_edge_availability(
            snap, decision_time="2024-01-02T00:00:00+00:00"
        ) is True

    def test_equal_snapshot_time(self) -> None:
        snap = _make_snapshot(snapshot_time="2024-01-01T00:00:00+00:00")
        assert validate_edge_availability(
            snap, decision_time="2024-01-01T00:00:00+00:00"
        ) is True

    def test_future_snapshot_time_raises(self) -> None:
        snap = _make_snapshot(snapshot_time="2024-01-03T00:00:00+00:00")
        with pytest.raises(ValueError, match="future edge"):
            validate_edge_availability(
                snap, decision_time="2024-01-01T00:00:00+00:00"
            )

    def test_per_edge_valid(self) -> None:
        snap = _make_snapshot(n_edges=3, snapshot_time="2024-01-01T00:00:00+00:00")
        edge_avail = [
            "2024-01-01T00:00:00+00:00",
            "2023-12-31T00:00:00+00:00",
            "2024-01-01T12:00:00+00:00",
        ]
        assert validate_edge_availability(
            snap,
            decision_time="2024-01-02T00:00:00+00:00",
            edge_available_at=edge_avail,
        ) is True

    def test_per_edge_future_raises(self) -> None:
        snap = _make_snapshot(n_edges=3, snapshot_time="2024-01-01T00:00:00+00:00")
        edge_avail = [
            "2024-01-01T00:00:00+00:00",
            "2024-01-03T00:00:00+00:00",  # future
            "2024-01-01T12:00:00+00:00",
        ]
        with pytest.raises(ValueError, match="future edge"):
            validate_edge_availability(
                snap,
                decision_time="2024-01-02T00:00:00+00:00",
                edge_available_at=edge_avail,
            )

    def test_per_edge_length_mismatch_raises(self) -> None:
        snap = _make_snapshot(n_edges=3)
        with pytest.raises(ValueError):
            validate_edge_availability(
                snap,
                decision_time="2024-01-02T00:00:00+00:00",
                edge_available_at=["2024-01-01T00:00:00+00:00"],  # too few
            )


# ---------------------------------------------------------------------------
# compute_graph_rank_metrics
# ---------------------------------------------------------------------------


class TestComputeGraphRankMetrics:
    def test_perfect_prediction(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        actual_ranks = [1, 2, 3, 4]  # AAPL best
        predictions = ["AAPL", "MSFT", "GOOG", "AMZN"]  # perfect
        metrics = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        assert metrics["ndcg"] == pytest.approx(1.0)
        assert metrics["map"] == pytest.approx(1.0)
        assert metrics["kendall_tau"] == pytest.approx(1.0)

    def test_worst_prediction(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        actual_ranks = [1, 2, 3, 4]  # AAPL best
        predictions = ["AMZN", "GOOG", "MSFT", "AAPL"]  # reversed
        metrics = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        assert metrics["ndcg"] < 1.0
        assert metrics["kendall_tau"] == pytest.approx(-1.0)

    def test_ndcg_range(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        actual_ranks = [3, 1, 4, 2]
        predictions = ["MSFT", "AMZN", "AAPL", "GOOG"]
        metrics = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        assert 0.0 <= metrics["ndcg"] <= 1.0

    def test_map_range(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        actual_ranks = [3, 1, 4, 2]
        predictions = ["MSFT", "AMZN", "AAPL", "GOOG"]
        metrics = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        assert 0.0 <= metrics["map"] <= 1.0

    def test_kendall_tau_range(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        actual_ranks = [3, 1, 4, 2]
        predictions = ["MSFT", "AMZN", "AAPL", "GOOG"]
        metrics = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        assert -1.0 <= metrics["kendall_tau"] <= 1.0

    def test_length_mismatch_raises(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG"]
        actual_ranks = [1, 2]  # mismatch
        predictions = ["AAPL", "MSFT", "GOOG"]
        with pytest.raises(ValueError):
            compute_graph_rank_metrics(predictions, actual_ranks, node_ids)

    def test_prediction_length_mismatch_raises(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG"]
        actual_ranks = [1, 2, 3]
        predictions = ["AAPL", "MSFT"]  # mismatch
        with pytest.raises(ValueError):
            compute_graph_rank_metrics(predictions, actual_ranks, node_ids)

    def test_prediction_set_mismatch_raises(self) -> None:
        node_ids = ["AAPL", "MSFT", "GOOG"]
        actual_ranks = [1, 2, 3]
        predictions = ["AAPL", "MSFT", "AMZN"]  # AMZN not in node_ids
        with pytest.raises(ValueError):
            compute_graph_rank_metrics(predictions, actual_ranks, node_ids)

    def test_single_node(self) -> None:
        node_ids = ["AAPL"]
        actual_ranks = [1]
        predictions = ["AAPL"]
        metrics = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        assert metrics["ndcg"] == pytest.approx(1.0)
        assert metrics["map"] == pytest.approx(1.0)
        # Kendall's tau is 0.0 for a single element (no pairs).
        assert metrics["kendall_tau"] == 0.0

    def test_metrics_recompute_from_artifact(self) -> None:
        """Rank metrics recompute from prediction artifacts (no model state)."""
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        actual_ranks = [2, 1, 4, 3]
        predictions = ["MSFT", "AAPL", "AMZN", "GOOG"]
        metrics1 = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        # Recompute from the same artifact inputs -> identical.
        metrics2 = compute_graph_rank_metrics(predictions, actual_ranks, node_ids)
        assert metrics1 == metrics2


# ---------------------------------------------------------------------------
# validate_promotion_eligibility
# ---------------------------------------------------------------------------


class TestValidatePromotionEligibility:
    def _make_result(self, is_shadow: bool) -> GraphRankerResult:
        cfg = GraphRankerConfig(node_feature_dim=3)
        gpu = GPUStatus(available=False)
        return GraphRankerResult(
            config=cfg,
            final_loss=0.5,
            gpu_status=gpu,
            is_shadow=is_shadow,
            promotion_eligible=not is_shadow,
            duration_seconds=1.0,
        )

    def test_shadow_no_override_not_eligible(self) -> None:
        result = self._make_result(is_shadow=True)
        assert validate_promotion_eligibility(result, manual_override=False) is False

    def test_shadow_with_override_eligible(self) -> None:
        result = self._make_result(is_shadow=True)
        assert validate_promotion_eligibility(result, manual_override=True) is True

    def test_non_shadow_no_override_eligible(self) -> None:
        result = self._make_result(is_shadow=False)
        assert validate_promotion_eligibility(result, manual_override=False) is True

    def test_non_shadow_with_override_eligible(self) -> None:
        result = self._make_result(is_shadow=False)
        assert validate_promotion_eligibility(result, manual_override=True) is True

    def test_fail_closed_shadow(self) -> None:
        """Shadow promotion is fail-closed without explicit override."""
        result = self._make_result(is_shadow=True)
        # No override -> never eligible, regardless of result fields.
        assert validate_promotion_eligibility(result) is False


# ---------------------------------------------------------------------------
# register_graph_ranker_family
# ---------------------------------------------------------------------------


class TestRegisterGraphRankerFamily:
    def test_returns_dict(self) -> None:
        spec = register_graph_ranker_family()
        assert isinstance(spec, dict)

    def test_family_id(self) -> None:
        spec = register_graph_ranker_family()
        assert spec["family_id"] == "graph_ranker"

    def test_display_name(self) -> None:
        spec = register_graph_ranker_family()
        assert "Graph Ranker" in spec["display_name"]

    def test_dataset_shape(self) -> None:
        spec = register_graph_ranker_family()
        assert spec["dataset_shape"] == "graph_snapshot"

    def test_objectives(self) -> None:
        spec = register_graph_ranker_family()
        assert "ranking" in spec["objectives"]

    def test_artifact_format(self) -> None:
        spec = register_graph_ranker_family()
        assert spec["artifact_format"] == "torch_state_dict"

    def test_artifact_loader(self) -> None:
        spec = register_graph_ranker_family()
        assert "graph_ranker" in spec["artifact_loader"]
        assert "load_artifact" in spec["artifact_loader"]

    def test_required_metrics(self) -> None:
        spec = register_graph_ranker_family()
        assert "ndcg" in spec["required_metrics"]
        assert "map" in spec["required_metrics"]
        assert "kendall_tau" in spec["required_metrics"]

    def test_shadow_only(self) -> None:
        spec = register_graph_ranker_family()
        assert spec["shadow_only"] is True

    def test_requires_gpu_false(self) -> None:
        spec = register_graph_ranker_family()
        assert spec["requires_gpu"] is False

    def test_edge_types(self) -> None:
        spec = register_graph_ranker_family()
        assert "sector" in spec["edge_types"]
        assert "industry" in spec["edge_types"]
        assert "correlation" in spec["edge_types"]
        assert "supply_chain" in spec["edge_types"]

    def test_created_at_ns(self) -> None:
        spec = register_graph_ranker_family()
        assert spec["created_at_ns"] > 0


# ---------------------------------------------------------------------------
# Edge cases: single node, single edge, single snapshot
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_single_node(self) -> None:
        cfg = _make_config(epochs=1)
        node_ids = ["AAPL"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        # Single node needs at least 1 edge (GraphSnapshot requires n_edges >= 1).
        # Self-loops are not allowed in GraphSnapshot? Actually GraphSnapshot
        # allows self-loops (it's the runtime, not the manifest). Use a
        # self-loop edge [0, 0].
        snap = GraphSnapshot.build(
            snapshot_id="single",
            n_nodes=1,
            n_edges=1,
            node_features=[[0.1, 0.2, 0.3]],
            edge_index=[[0], [0]],
            snapshot_time="2024-01-01T00:00:00+00:00",
        )
        labels = [1.0]
        result = ranker.train([snap], [labels])
        assert result.ranked_symbols == ["AAPL"]

    def test_single_edge(self) -> None:
        cfg = _make_config(epochs=1)
        node_ids = ["AAPL", "MSFT"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = GraphSnapshot.build(
            snapshot_id="single-edge",
            n_nodes=2,
            n_edges=1,
            node_features=[[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]],
            edge_index=[[0], [1]],
            snapshot_time="2024-01-01T00:00:00+00:00",
        )
        labels = [2.0, 1.0]
        result = ranker.train([snap], [labels])
        assert len(result.ranked_symbols) == 2

    def test_single_snapshot(self) -> None:
        cfg = _make_config(epochs=2)
        node_ids = ["AAPL", "MSFT", "GOOG"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(n_nodes=3, n_edges=4, node_feature_dim=3)
        labels = _make_labels(3)
        result = ranker.train([snap], [labels])
        assert len(result.epoch_losses) == 2
        assert result.ranked_symbols is not None

    def test_train_with_edge_weights(self) -> None:
        cfg = _make_config(epochs=2)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(
            n_nodes=4, n_edges=6, node_feature_dim=3,
            edge_weights=[1.0, 0.5, 0.8, 0.3, 0.9, 0.2],
        )
        labels = _make_labels(4)
        result = ranker.train([snap], [labels])
        assert result.ranked_symbols is not None


# ---------------------------------------------------------------------------
# Integration: PIT-safe prediction flow
# ---------------------------------------------------------------------------


class TestPITSafeFlow:
    def test_pit_safe_prediction(self) -> None:
        """Full PIT-safe prediction flow: validate edges then predict."""
        cfg = _make_config(epochs=1)
        node_ids = ["AAPL", "MSFT", "GOOG", "AMZN"]
        ranker = GraphRanker(cfg, node_ids=node_ids)
        snap = _make_snapshot(
            n_nodes=4, n_edges=6, node_feature_dim=3,
            snapshot_time="2024-01-01T00:00:00+00:00",
        )
        labels = _make_labels(4)
        ranker.train([snap], [labels])
        # Validate edges are available at decision time.
        decision_time = "2024-01-02T00:00:00+00:00"
        assert validate_edge_availability(snap, decision_time) is True
        # Predict.
        ranked = ranker.predict(snap)
        assert set(ranked) == set(node_ids)

    def test_pit_unsafe_prediction_raises(self) -> None:
        """Future edge blocks prediction (fail-closed)."""
        snap = _make_snapshot(
            snapshot_time="2024-01-03T00:00:00+00:00",
        )
        with pytest.raises(ValueError, match="future edge"):
            validate_edge_availability(snap, "2024-01-01T00:00:00+00:00")
