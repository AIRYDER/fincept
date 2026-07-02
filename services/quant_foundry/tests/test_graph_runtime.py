"""Tests for quant_foundry.graph_runtime (T-12.6).

Covers the PyTorch Geometric-compatible graph runtime: the Docker image
spec, graph snapshot config, graph snapshot, the snapshot loader, the GPU
memory planner, the graph healthcheck, and the tiny GNN model.

The test host is CPU-only (torch is installed with the CPU index URL,
torch_geometric is not installed), so GPU-dependent assertions check the
"no GPU" degradation path. The snapshot round-trip, memory planning, and
GNN forward pass run on CPU.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_foundry.graph_runtime import (
    GPUMemoryPlanner,
    GraphHealthcheck,
    GraphImageSpec,
    GraphSnapshot,
    GraphSnapshotConfig,
    GraphSnapshotLoader,
    TinyGNNModel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    snapshot_id: str = "snap-001",
    n_nodes: int = 4,
    n_edges: int = 6,
    node_feature_dim: int = 3,
    snapshot_time: str = "2024-01-01T00:00:00+00:00",
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
    )


# ---------------------------------------------------------------------------
# GraphImageSpec
# ---------------------------------------------------------------------------


class TestGraphImageSpec:
    def test_defaults(self) -> None:
        spec = GraphImageSpec()
        assert spec.image_name == "trainer-gpu-graph"
        assert spec.base_image == "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
        assert spec.python_version == "3.12"
        assert spec.gpu_required is True
        assert spec.supports_mixed_precision is True
        assert spec.graph_cache_dir == "/opt/graph_cache"

    def test_default_packages(self) -> None:
        spec = GraphImageSpec()
        assert "torch==2.1.0" in spec.packages
        assert "torch-geometric>=2.4" in spec.packages
        assert "numpy>=1.26" in spec.packages
        assert "pandas>=2.1" in spec.packages
        assert "pydantic>=2.7" in spec.packages
        assert "scipy>=1.11" in spec.packages

    def test_healthcheck_cmd_references_graph_runtime(self) -> None:
        spec = GraphImageSpec()
        assert "GraphHealthcheck" in spec.healthcheck_cmd
        assert "graph_runtime" in spec.healthcheck_cmd

    def test_frozen(self) -> None:
        spec = GraphImageSpec()
        with pytest.raises(Exception):
            spec.image_name = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            GraphImageSpec(unexpected="x")  # type: ignore[call-arg]

    def test_custom_construction(self) -> None:
        spec = GraphImageSpec(
            image_name="custom-graph",
            base_image="pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime",
            python_version="3.11",
            packages=["torch==2.2.0", "numpy"],
            gpu_required=False,
            supports_mixed_precision=False,
            graph_cache_dir="/custom/cache",
        )
        assert spec.image_name == "custom-graph"
        assert spec.gpu_required is False
        assert spec.supports_mixed_precision is False
        assert spec.graph_cache_dir == "/custom/cache"


# ---------------------------------------------------------------------------
# GraphSnapshotConfig
# ---------------------------------------------------------------------------


class TestGraphSnapshotConfig:
    def test_valid_defaults(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=10, n_edges=20, node_feature_dim=8
        )
        assert cfg.n_nodes == 10
        assert cfg.n_edges == 20
        assert cfg.node_feature_dim == 8
        assert cfg.edge_feature_dim == 0
        assert cfg.n_layers == 2
        assert cfg.hidden_dim == 64
        assert cfg.dropout == 0.1

    def test_frozen(self) -> None:
        cfg = GraphSnapshotConfig(n_nodes=10, n_edges=20, node_feature_dim=8)
        with pytest.raises(Exception):
            cfg.n_nodes = 20  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(
                n_nodes=10, n_edges=20, node_feature_dim=8, unexpected=1
            )  # type: ignore[call-arg]

    def test_n_nodes_minimum(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(n_nodes=0, n_edges=20, node_feature_dim=8)

    def test_n_edges_minimum(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(n_nodes=10, n_edges=0, node_feature_dim=8)

    def test_node_feature_dim_minimum(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(n_nodes=10, n_edges=20, node_feature_dim=0)

    def test_edge_feature_dim_can_be_zero(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=10, n_edges=20, node_feature_dim=8, edge_feature_dim=0
        )
        assert cfg.edge_feature_dim == 0

    def test_edge_feature_dim_negative_rejected(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(
                n_nodes=10, n_edges=20, node_feature_dim=8, edge_feature_dim=-1
            )

    def test_n_layers_minimum(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(
                n_nodes=10, n_edges=20, node_feature_dim=8, n_layers=0
            )

    def test_hidden_dim_minimum(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(
                n_nodes=10, n_edges=20, node_feature_dim=8, hidden_dim=0
            )

    def test_dropout_lower_bound(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(
                n_nodes=10, n_edges=20, node_feature_dim=8, dropout=-0.1
            )

    def test_dropout_upper_bound(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshotConfig(
                n_nodes=10, n_edges=20, node_feature_dim=8, dropout=1.0
            )

    def test_dropout_zero_allowed(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=10, n_edges=20, node_feature_dim=8, dropout=0.0
        )
        assert cfg.dropout == 0.0

    def test_custom_construction(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=100,
            n_edges=500,
            node_feature_dim=16,
            edge_feature_dim=4,
            n_layers=3,
            hidden_dim=128,
            dropout=0.5,
        )
        assert cfg.n_layers == 3
        assert cfg.hidden_dim == 128
        assert cfg.dropout == 0.5


# ---------------------------------------------------------------------------
# GraphSnapshot
# ---------------------------------------------------------------------------


class TestGraphSnapshot:
    def test_build_computes_hash(self) -> None:
        snap = _make_snapshot()
        assert snap.data_hash
        assert len(snap.data_hash) == 64  # SHA-256 hex

    def test_build_is_deterministic(self) -> None:
        s1 = _make_snapshot()
        s2 = _make_snapshot()
        assert s1.data_hash == s2.data_hash

    def test_build_different_ids_different_hash(self) -> None:
        s1 = _make_snapshot(snapshot_id="a")
        s2 = _make_snapshot(snapshot_id="b")
        assert s1.data_hash != s2.data_hash

    def test_verify_hash_valid(self) -> None:
        snap = _make_snapshot()
        assert snap.verify_hash() is True

    def test_frozen(self) -> None:
        snap = _make_snapshot()
        with pytest.raises(Exception):
            snap.snapshot_id = "other"  # type: ignore[misc]

    def test_extra_forbid(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshot(
                snapshot_id="x",
                n_nodes=1,
                n_edges=1,
                node_features=[[1.0]],
                edge_index=[[0], [0]],
                snapshot_time="2024-01-01T00:00:00+00:00",
                data_hash="x" * 64,
                unexpected=1,
            )  # type: ignore[call-arg]

    def test_node_features_count_mismatch(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshot(
                snapshot_id="x",
                n_nodes=5,
                n_edges=1,
                node_features=[[1.0]],  # only 1 row, n_nodes=5
                edge_index=[[0], [0]],
                snapshot_time="2024-01-01T00:00:00+00:00",
                data_hash="x" * 64,
            )

    def test_edge_index_wrong_row_count(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshot(
                snapshot_id="x",
                n_nodes=1,
                n_edges=1,
                node_features=[[1.0]],
                edge_index=[[0]],  # only 1 row
                snapshot_time="2024-01-01T00:00:00+00:00",
                data_hash="x" * 64,
            )

    def test_edge_index_wrong_edge_count(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshot(
                snapshot_id="x",
                n_nodes=1,
                n_edges=5,
                node_features=[[1.0]],
                edge_index=[[0], [0]],  # 1 edge, n_edges=5
                snapshot_time="2024-01-01T00:00:00+00:00",
                data_hash="x" * 64,
            )

    def test_edge_weights_count_mismatch(self) -> None:
        with pytest.raises(Exception):
            GraphSnapshot(
                snapshot_id="x",
                n_nodes=1,
                n_edges=2,
                node_features=[[1.0]],
                edge_index=[[0, 0], [0, 0]],
                edge_weights=[1.0],  # 1 weight, n_edges=2
                snapshot_time="2024-01-01T00:00:00+00:00",
                data_hash="x" * 64,
            )

    def test_edge_weights_none_allowed(self) -> None:
        snap = _make_snapshot()
        assert snap.edge_weights is None

    def test_edge_weights_valid(self) -> None:
        snap = GraphSnapshot.build(
            snapshot_id="w",
            n_nodes=3,
            n_edges=3,
            node_features=[[1.0], [2.0], [3.0]],
            edge_index=[[0, 1, 2], [1, 2, 0]],
            edge_weights=[0.5, 0.5, 0.5],
            snapshot_time="2024-01-01T00:00:00+00:00",
        )
        assert snap.edge_weights == [0.5, 0.5, 0.5]
        assert snap.verify_hash() is True


# ---------------------------------------------------------------------------
# GraphSnapshotLoader
# ---------------------------------------------------------------------------


class TestGraphSnapshotLoader:
    def test_save_and_load_npz_roundtrip(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        snap = _make_snapshot()
        path = loader.save(snap)
        assert path.endswith(".npz")
        loaded = loader.load(snap.snapshot_id)
        assert loaded.snapshot_id == snap.snapshot_id
        assert loaded.n_nodes == snap.n_nodes
        assert loaded.n_edges == snap.n_edges
        assert loaded.data_hash == snap.data_hash
        assert loaded.node_features == snap.node_features
        assert loaded.edge_index == snap.edge_index

    def test_save_creates_cache_dir(self, tmp_path: Path) -> None:
        cache = tmp_path / "sub" / "cache"
        loader = GraphSnapshotLoader(str(cache))
        snap = _make_snapshot()
        loader.save(snap)
        assert cache.exists()

    def test_list_snapshots_empty(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        assert loader.list_snapshots() == []

    def test_list_snapshots_empty_when_dir_missing(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path / "nonexistent"))
        assert loader.list_snapshots() == []

    def test_list_snapshots_after_save(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        loader.save(_make_snapshot("a"))
        loader.save(_make_snapshot("b"))
        ids = loader.list_snapshots()
        assert ids == ["a", "b"]

    def test_load_not_found(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            loader.load("missing")

    def test_validate_snapshot_valid(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        snap = _make_snapshot()
        assert loader.validate_snapshot(snap) is True

    def test_validate_snapshot_bad_hash(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        snap = _make_snapshot()
        # Construct a snapshot with a wrong hash via model_construct.
        bad = snap.model_copy(update={"data_hash": "0" * 64})
        assert loader.validate_snapshot(bad) is False

    def test_save_and_load_json_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force numpy import to fail to exercise the JSON fallback.
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "numpy":
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        loader = GraphSnapshotLoader(str(tmp_path))
        snap = _make_snapshot()
        path = loader.save(snap)
        assert path.endswith(".json")
        # JSON load path doesn't need numpy.
        loaded = loader.load(snap.snapshot_id)
        assert loaded.snapshot_id == snap.snapshot_id
        assert loaded.data_hash == snap.data_hash

    def test_json_file_is_valid_json(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        snap = _make_snapshot()
        # Save as JSON directly.
        json_path = tmp_path / f"{snap.snapshot_id}.json"
        json_path.write_text(
            json.dumps(snap.model_dump(), indent=2), encoding="utf-8"
        )
        loaded = loader.load(snap.snapshot_id)
        assert loaded.snapshot_id == snap.snapshot_id
        assert loaded.data_hash == snap.data_hash

    def test_list_snapshots_mixed_formats(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        # Save one as npz.
        loader.save(_make_snapshot("npz-snap"))
        # Save another as json directly.
        snap = _make_snapshot("json-snap")
        (tmp_path / "json-snap.json").write_text(
            json.dumps(snap.model_dump()), encoding="utf-8"
        )
        ids = loader.list_snapshots()
        assert "npz-snap" in ids
        assert "json-snap" in ids


# ---------------------------------------------------------------------------
# GPUMemoryPlanner
# ---------------------------------------------------------------------------


class TestGPUMemoryPlanner:
    def test_estimate_memory_returns_dict(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=100, n_edges=500, node_feature_dim=16
        )
        planner = GPUMemoryPlanner(cfg)
        est = planner.estimate_memory()
        assert "node_memory_mb" in est
        assert "edge_memory_mb" in est
        assert "layer_memory_mb" in est
        assert "total_memory_mb" in est

    def test_estimate_memory_scales_with_nodes(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=100, n_edges=500, node_feature_dim=16
        )
        planner = GPUMemoryPlanner(cfg)
        small = planner.estimate_memory(n_nodes=100)
        large = planner.estimate_memory(n_nodes=10000)
        assert large["node_memory_mb"] > small["node_memory_mb"]
        assert large["total_memory_mb"] > small["total_memory_mb"]

    def test_estimate_memory_scales_with_edges(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=100, n_edges=500, node_feature_dim=16
        )
        planner = GPUMemoryPlanner(cfg)
        small = planner.estimate_memory(n_edges=100)
        large = planner.estimate_memory(n_edges=100000)
        assert large["edge_memory_mb"] > small["edge_memory_mb"]

    def test_estimate_memory_total_is_sum(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=100, n_edges=500, node_feature_dim=16
        )
        planner = GPUMemoryPlanner(cfg)
        est = planner.estimate_memory()
        expected = (
            est["node_memory_mb"]
            + est["edge_memory_mb"]
            + est["layer_memory_mb"]
        )
        assert abs(est["total_memory_mb"] - expected) < 1e-9

    def test_estimate_memory_positive(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=10, n_edges=20, node_feature_dim=8
        )
        planner = GPUMemoryPlanner(cfg)
        est = planner.estimate_memory()
        assert est["node_memory_mb"] > 0
        assert est["edge_memory_mb"] > 0
        assert est["layer_memory_mb"] > 0
        assert est["total_memory_mb"] > 0

    def test_estimate_memory_uses_config_defaults(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=50, n_edges=200, node_feature_dim=4
        )
        planner = GPUMemoryPlanner(cfg)
        est = planner.estimate_memory()
        # No overrides -> uses config values.
        est_explicit = planner.estimate_memory(
            n_nodes=50, n_edges=200
        )
        assert est == est_explicit

    def test_fits_in_gpu_true_when_plenty(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=10, n_edges=20, node_feature_dim=8
        )
        planner = GPUMemoryPlanner(cfg)
        assert planner.fits_in_gpu(available_mb=1024.0) is True

    def test_fits_in_gpu_false_when_tiny(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=100000, n_edges=500000, node_feature_dim=64
        )
        planner = GPUMemoryPlanner(cfg)
        assert planner.fits_in_gpu(available_mb=0.001) is False

    def test_fits_in_gpu_with_overrides(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=10, n_edges=20, node_feature_dim=8
        )
        planner = GPUMemoryPlanner(cfg)
        # Small graph fits, large graph does not.
        assert planner.fits_in_gpu(1024.0, n_nodes=10, n_edges=20) is True
        assert (
            planner.fits_in_gpu(0.001, n_nodes=100000, n_edges=500000)
            is False
        )

    def test_fits_in_gpu_safety_margin(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=10, n_edges=20, node_feature_dim=8
        )
        planner = GPUMemoryPlanner(cfg)
        est = planner.estimate_memory()
        # available exactly equal to estimate -> does not fit (20% margin).
        assert planner.fits_in_gpu(est["total_memory_mb"]) is False
        # available = estimate * 1.2 -> exactly at boundary, fits (<=).
        assert planner.fits_in_gpu(est["total_memory_mb"] * 1.2) is True

    def test_estimate_memory_edge_features_included(self) -> None:
        cfg_no_ef = GraphSnapshotConfig(
            n_nodes=100, n_edges=500, node_feature_dim=16, edge_feature_dim=0
        )
        cfg_ef = GraphSnapshotConfig(
            n_nodes=100, n_edges=500, node_feature_dim=16, edge_feature_dim=8
        )
        no_ef = GPUMemoryPlanner(cfg_no_ef).estimate_memory()
        with_ef = GPUMemoryPlanner(cfg_ef).estimate_memory()
        assert with_ef["edge_memory_mb"] > no_ef["edge_memory_mb"]


# ---------------------------------------------------------------------------
# TinyGNNModel
# ---------------------------------------------------------------------------


class TestTinyGNNModel:
    def test_init_valid(self) -> None:
        model = TinyGNNModel(node_feature_dim=4, hidden_dim=8, output_dim=8)
        assert model.node_feature_dim == 4
        assert model.hidden_dim == 8
        assert model.output_dim == 8

    def test_init_invalid_node_feature_dim(self) -> None:
        with pytest.raises(ValueError):
            TinyGNNModel(node_feature_dim=0)

    def test_init_invalid_hidden_dim(self) -> None:
        with pytest.raises(ValueError):
            TinyGNNModel(node_feature_dim=4, hidden_dim=0)

    def test_init_invalid_output_dim(self) -> None:
        with pytest.raises(ValueError):
            TinyGNNModel(node_feature_dim=4, output_dim=0)

    def test_init_invalid_dropout(self) -> None:
        with pytest.raises(ValueError):
            TinyGNNModel(node_feature_dim=4, dropout=1.0)
        with pytest.raises(ValueError):
            TinyGNNModel(node_feature_dim=4, dropout=-0.1)

    def test_forward_pass(self) -> None:
        import torch

        model = TinyGNNModel(node_feature_dim=3, hidden_dim=8, output_dim=8)
        node_features = torch.randn(4, 3)
        edge_index = torch.tensor(
            [[0, 0, 1, 1, 2, 3], [1, 2, 2, 3, 3, 0]], dtype=torch.long
        )
        out = model.forward(node_features, edge_index)
        assert out.shape == (4, 8)

    def test_forward_pass_with_edge_weights(self) -> None:
        import torch

        model = TinyGNNModel(node_feature_dim=3, hidden_dim=8, output_dim=4)
        node_features = torch.randn(5, 3)
        edge_index = torch.tensor(
            [[0, 1, 2, 3, 4], [1, 2, 3, 4, 0]], dtype=torch.long
        )
        edge_weight = torch.ones(5, dtype=torch.float32)
        out = model.forward(node_features, edge_index, edge_weight)
        assert out.shape == (5, 4)

    def test_forward_pass_deterministic_in_eval(self) -> None:
        import torch

        torch.manual_seed(42)
        model = TinyGNNModel(
            node_feature_dim=3, hidden_dim=8, output_dim=8
        ).eval()
        node_features = torch.randn(4, 3)
        edge_index = torch.tensor(
            [[0, 1, 2, 3], [1, 2, 3, 0]], dtype=torch.long
        )
        out1 = model.forward(node_features, edge_index)
        out2 = model.forward(node_features, edge_index)
        assert torch.allclose(out1, out2)

    def test_state_dict_roundtrip(self) -> None:
        import torch

        # Use eval mode so dropout is disabled -> deterministic outputs.
        model = TinyGNNModel(
            node_feature_dim=3, hidden_dim=8, output_dim=8
        ).eval()
        node_features = torch.randn(4, 3)
        edge_index = torch.tensor(
            [[0, 1, 2, 3], [1, 2, 3, 0]], dtype=torch.long
        )
        out1 = model.forward(node_features, edge_index)
        sd = model.state_dict()
        model2 = TinyGNNModel(
            node_feature_dim=3, hidden_dim=8, output_dim=8
        ).eval()
        model2.load_state_dict(sd)
        out2 = model2.forward(node_features, edge_index)
        assert torch.allclose(out1, out2)

    def test_parameters_returns_iterable(self) -> None:
        model = TinyGNNModel(node_feature_dim=3, hidden_dim=8, output_dim=8)
        params = list(model.parameters())
        assert len(params) > 0

    def test_to_and_train_eval(self) -> None:
        model = TinyGNNModel(node_feature_dim=3, hidden_dim=8, output_dim=8)
        # to() and train()/eval() should not raise.
        model.eval()
        model.train()
        model.to("cpu")


# ---------------------------------------------------------------------------
# GraphHealthcheck
# ---------------------------------------------------------------------------


class TestGraphHealthcheck:
    def test_init_default_timeout(self) -> None:
        hc = GraphHealthcheck()
        assert hc.timeout_seconds == 60

    def test_init_custom_timeout(self) -> None:
        hc = GraphHealthcheck(timeout_seconds=30)
        assert hc.timeout_seconds == 30

    def test_init_invalid_timeout(self) -> None:
        with pytest.raises(ValueError):
            GraphHealthcheck(timeout_seconds=0)
        with pytest.raises(ValueError):
            GraphHealthcheck(timeout_seconds=-1)

    def test_run_returns_status_dict(self) -> None:
        hc = GraphHealthcheck()
        status = hc.run()
        assert "healthy" in status
        assert "gpu" in status
        assert "snapshot_load" in status
        assert "forward_pass" in status
        assert "error" in status
        assert "duration_seconds" in status

    def test_run_snapshot_load_and_forward_pass_succeed(self) -> None:
        hc = GraphHealthcheck()
        status = hc.run()
        # On CPU, snapshot load + forward pass should succeed (torch installed).
        assert status["snapshot_load"] is True
        assert status["forward_pass"] is True
        assert status["error"] is None

    def test_run_unhealthy_on_cpu(self) -> None:
        # On a CPU-only host, GPU is not available -> unhealthy.
        hc = GraphHealthcheck()
        status = hc.run()
        if status["gpu"] and not status["gpu"]["available"]:
            assert status["healthy"] is False
            assert hc.is_healthy() is False

    def test_is_healthy_returns_bool(self) -> None:
        hc = GraphHealthcheck()
        assert isinstance(hc.is_healthy(), bool)

    def test_run_duration_positive(self) -> None:
        hc = GraphHealthcheck()
        status = hc.run()
        assert status["duration_seconds"] >= 0.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_large_graph_memory_estimate(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=1_000_000,
            n_edges=10_000_000,
            node_feature_dim=128,
            hidden_dim=256,
            n_layers=3,
        )
        planner = GPUMemoryPlanner(cfg)
        est = planner.estimate_memory()
        # Large graph should require multiple GB.
        assert est["total_memory_mb"] > 1000.0

    def test_large_graph_does_not_fit_in_small_gpu(self) -> None:
        cfg = GraphSnapshotConfig(
            n_nodes=1_000_000,
            n_edges=10_000_000,
            node_feature_dim=128,
        )
        planner = GPUMemoryPlanner(cfg)
        # 1 GB GPU.
        assert planner.fits_in_gpu(available_mb=1024.0) is False

    def test_snapshot_loader_empty_cache_validate(self, tmp_path: Path) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        snap = _make_snapshot()
        # validate_snapshot works without any on-disk state.
        assert loader.validate_snapshot(snap) is True

    def test_invalid_snapshot_with_corrupt_hash(self) -> None:
        snap = _make_snapshot()
        bad = snap.model_copy(update={"data_hash": "deadbeef"})
        # The model validator runs on construction, but model_copy bypasses
        # validation by default — so we test verify_hash directly.
        assert bad.verify_hash() is False

    def test_snapshot_id_with_path_separators_sanitized(
        self, tmp_path: Path
    ) -> None:
        loader = GraphSnapshotLoader(str(tmp_path))
        snap = _make_snapshot(snapshot_id="group/sub/snap")
        path = loader.save(snap)
        # Path separators in the id should be sanitized.
        assert "group_sub_snap" in Path(path).name
        loaded = loader.load("group/sub/snap")
        assert loaded.snapshot_id == "group/sub/snap"
