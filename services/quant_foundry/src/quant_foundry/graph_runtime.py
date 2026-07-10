"""
quant_foundry.graph_runtime — PyTorch Geometric-compatible graph runtime (T-12.6).

This module provides a self-contained, importable PyTorch runtime for graph
neural networks (GNNs) used by the quant foundry's GPU worker path. It is
designed to be **importable without torch or torch_geometric installed** — all
torch / torch_geometric imports are lazy and performed inside methods, so the
module can be imported on CPU-only machines (e.g. the local test suite) and
only fails when a torch-dependent operation is actually invoked.

Capabilities:

- :class:`GraphImageSpec` — declarative spec for the
  ``trainer-gpu-graph`` Docker image (base image, packages,
  healthcheck command, graph cache dir).
- :class:`GraphSnapshotConfig` — configuration for a graph snapshot's
  shape and GNN architecture (Pydantic v2, frozen + ``extra='forbid'``).
- :class:`GraphSnapshot` — typed in-memory representation of a graph
  snapshot (nodes, edges, features, deterministic SHA-256 hash).
- :class:`GraphSnapshotLoader` — loads / saves / lists / validates graph
  snapshots from a cache directory (``.npz`` or ``.json`` formats).
- :class:`GPUMemoryPlanner` — estimates GPU memory needed for a graph
  snapshot + GNN forward/backward pass.
- :class:`GraphHealthcheck` — healthcheck that probes the GPU, loads a
  tiny synthetic graph snapshot, and runs a tiny GNN forward pass.
- :class:`TinyGNNModel` — a simple 2-layer Graph Convolutional Network
  implemented in pure torch (no torch_geometric dependency for the
  forward pass), with PyTorch Geometric-compatible tensor conventions.

Design notes:

- **Lazy torch import.** ``import torch`` happens inside methods, never at
  module top level. The module can be imported, and the Pydantic models /
  ``GraphImageSpec`` can be constructed, on a host without torch.
- **PyTorch Geometric compatibility.** The :class:`TinyGNNModel` follows
  PyG's tensor conventions (``node_features`` as ``[N, F]`` and
  ``edge_index`` as ``[2, E]`` with source / destination rows), so it can
  be swapped for a real ``torch_geometric.nn.GCNConv`` layer inside the GPU
  worker container without changing call sites. The forward pass itself is
  implemented in pure torch (scatter-add message passing) so the
  healthcheck runs on CPU-only test hosts where ``torch_geometric`` is not
  installed.
- **No live trading authority.** The healthcheck runs on synthetic graph
  data only; it never touches real feature-lake data or produces tradeable
  predictions.
- **No secrets.** Configs carry only hyperparameters and filesystem paths
  — never credentials.
- **Cost fails closed.** The healthcheck reports unhealthy when the GPU is
  unavailable or any probe raises; it never reports healthy on a partial
  probe.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Docker image spec
# ---------------------------------------------------------------------------


class GraphImageSpec(BaseModel):
    """Declarative spec for the ``trainer-gpu-graph`` Docker image.

    Frozen + ``extra='forbid'`` for audit integrity. The spec is the source
    of truth for the image's base, packages, healthcheck command, and graph
    cache directory; the Dockerfile in
    ``docker/trainer-gpu-graph/`` is generated from it (kept in sync by
    review).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_name: str = "trainer-gpu-graph"
    base_image: str = "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime"
    python_version: str = "3.12"
    packages: list[str] = Field(
        default_factory=lambda: [
            "torch==2.1.0",
            "torch-geometric>=2.4",
            "numpy>=1.26",
            "pandas>=2.1",
            "pydantic>=2.7",
            "scipy>=1.11",
        ]
    )
    gpu_required: bool = True
    healthcheck_cmd: str = (
        'python -c "from quant_foundry.graph_runtime import '
        "GraphHealthcheck; import sys; "
        'sys.exit(0 if GraphHealthcheck().is_healthy() else 1)"'
    )
    supports_mixed_precision: bool = True
    graph_cache_dir: str = "/opt/graph_cache"


# ---------------------------------------------------------------------------
# Graph snapshot config
# ---------------------------------------------------------------------------


class GraphSnapshotConfig(BaseModel):
    """Configuration for a graph snapshot's shape and GNN architecture.

    Frozen + ``extra='forbid'`` for audit integrity. Describes the graph
    dimensions (nodes / edges / feature dims) and the GNN architecture
    (layers / hidden dim / dropout) that will consume the snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_nodes: int
    n_edges: int
    node_feature_dim: int
    edge_feature_dim: int = 0
    n_layers: int = 2
    hidden_dim: int = 64
    dropout: float = 0.1

    @field_validator("n_nodes")
    @classmethod
    def _validate_n_nodes(cls, v: int) -> int:
        if v < 1:
            raise ValueError("n_nodes must be >= 1")
        return v

    @field_validator("n_edges")
    @classmethod
    def _validate_n_edges(cls, v: int) -> int:
        if v < 1:
            raise ValueError("n_edges must be >= 1")
        return v

    @field_validator("node_feature_dim")
    @classmethod
    def _validate_node_feature_dim(cls, v: int) -> int:
        if v < 1:
            raise ValueError("node_feature_dim must be >= 1")
        return v

    @field_validator("edge_feature_dim")
    @classmethod
    def _validate_edge_feature_dim(cls, v: int) -> int:
        if v < 0:
            raise ValueError("edge_feature_dim must be >= 0")
        return v

    @field_validator("n_layers")
    @classmethod
    def _validate_n_layers(cls, v: int) -> int:
        if v < 1:
            raise ValueError("n_layers must be >= 1")
        return v

    @field_validator("hidden_dim")
    @classmethod
    def _validate_hidden_dim(cls, v: int) -> int:
        if v < 1:
            raise ValueError("hidden_dim must be >= 1")
        return v

    @field_validator("dropout")
    @classmethod
    def _validate_dropout(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        return v


# ---------------------------------------------------------------------------
# Graph snapshot
# ---------------------------------------------------------------------------


def _compute_snapshot_hash(
    snapshot_id: str,
    n_nodes: int,
    n_edges: int,
    node_features: list[list[float]],
    edge_index: list[list[int]],
    edge_weights: list[float] | None,
    snapshot_time: str,
) -> str:
    """Compute a deterministic SHA-256 hash for a graph snapshot.

    The hash covers every field that contributes to the snapshot's identity
    and content. Floats are serialized with a fixed precision (6 decimals)
    so the hash is stable across equivalent float representations.
    """
    h = hashlib.sha256()
    h.update(snapshot_id.encode("utf-8"))
    h.update(f"|n_nodes={n_nodes}|n_edges={n_edges}".encode())
    h.update(f"|t={snapshot_time}".encode())

    # Node features: serialize with fixed precision.
    for row in node_features:
        h.update(b"|n:")
        h.update(",".join(f"{float(v):.6f}" for v in row).encode("utf-8"))

    # Edge index.
    h.update(b"|ei:")
    if len(edge_index) >= 2:
        h.update(",".join(str(int(s)) for s in edge_index[0]).encode("utf-8"))
        h.update(b";")
        h.update(",".join(str(int(d)) for d in edge_index[1]).encode("utf-8"))

    # Edge weights.
    if edge_weights is not None:
        h.update(b"|ew:")
        h.update(",".join(f"{float(w):.6f}" for w in edge_weights).encode("utf-8"))

    return h.hexdigest()


class GraphSnapshot(BaseModel):
    """Typed in-memory representation of a graph snapshot.

    Frozen + ``extra='forbid'`` for audit integrity. ``node_features`` is a
    list of ``n_nodes`` rows, each of length ``node_feature_dim``.
    ``edge_index`` is a 2-row list (``[sources, destinations]``) of length
    ``n_edges`` each. ``edge_weights`` is an optional per-edge weight list
    of length ``n_edges``. ``data_hash`` is a deterministic SHA-256 of the
    snapshot's content.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str
    n_nodes: int
    n_edges: int
    node_features: list[list[float]]
    edge_index: list[list[int]]
    edge_weights: list[float] | None = None
    snapshot_time: str
    data_hash: str

    @model_validator(mode="after")
    def _validate_shapes(self) -> GraphSnapshot:
        if len(self.node_features) != self.n_nodes:
            raise ValueError(
                f"len(node_features) ({len(self.node_features)}) != n_nodes ({self.n_nodes})"
            )
        if len(self.edge_index) != 2:
            raise ValueError(
                f"edge_index must have exactly 2 rows (src, dst), got {len(self.edge_index)}"
            )
        if len(self.edge_index[0]) != self.n_edges:
            raise ValueError(
                f"len(edge_index[0]) ({len(self.edge_index[0])}) != n_edges ({self.n_edges})"
            )
        if len(self.edge_index[1]) != self.n_edges:
            raise ValueError(
                f"len(edge_index[1]) ({len(self.edge_index[1])}) != n_edges ({self.n_edges})"
            )
        if self.edge_weights is not None and len(self.edge_weights) != self.n_edges:
            raise ValueError(
                f"len(edge_weights) ({len(self.edge_weights)}) != n_edges ({self.n_edges})"
            )
        return self

    @classmethod
    def build(
        cls,
        snapshot_id: str,
        n_nodes: int,
        n_edges: int,
        node_features: list[list[float]],
        edge_index: list[list[int]],
        snapshot_time: str,
        edge_weights: list[float] | None = None,
    ) -> GraphSnapshot:
        """Construct a :class:`GraphSnapshot` computing ``data_hash``.

        This is the preferred constructor when the caller does not already
        have a precomputed ``data_hash`` — it computes the deterministic
        SHA-256 from the snapshot's content.
        """
        data_hash = _compute_snapshot_hash(
            snapshot_id,
            n_nodes,
            n_edges,
            node_features,
            edge_index,
            edge_weights,
            snapshot_time,
        )
        return cls(
            snapshot_id=snapshot_id,
            n_nodes=n_nodes,
            n_edges=n_edges,
            node_features=node_features,
            edge_index=edge_index,
            edge_weights=edge_weights,
            snapshot_time=snapshot_time,
            data_hash=data_hash,
        )

    def verify_hash(self) -> bool:
        """Return ``True`` if the stored ``data_hash`` matches a recomputed hash."""
        recomputed = _compute_snapshot_hash(
            self.snapshot_id,
            self.n_nodes,
            self.n_edges,
            self.node_features,
            self.edge_index,
            self.edge_weights,
            self.snapshot_time,
        )
        return recomputed == self.data_hash


# ---------------------------------------------------------------------------
# Graph snapshot loader
# ---------------------------------------------------------------------------


class GraphSnapshotLoader:
    """Loads, saves, lists, and validates graph snapshots from a cache dir.

    Supports two on-disk formats:

    - ``.npz`` — a NumPy archive with arrays keyed by ``node_features``,
      ``edge_index``, ``edge_weights`` (optional), plus scalar metadata
      (``snapshot_id``, ``n_nodes``, ``n_edges``, ``snapshot_time``,
      ``data_hash``).
    - ``.json`` — a JSON file with the full :class:`GraphSnapshot` dict.

    All numpy imports are lazy (inside methods), so the loader can be
    constructed on a host without numpy.
    """

    def __init__(self, cache_dir: str) -> None:
        self.cache_dir = cache_dir
        self._dir = Path(cache_dir)

    def _path_for(self, snapshot_id: str, ext: str) -> Path:
        """Return the on-disk path for a snapshot id with a given extension."""
        safe = snapshot_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.{ext}"

    def save(self, snapshot: GraphSnapshot) -> str:
        """Save ``snapshot`` to the cache dir and return its file path.

        The snapshot is saved in ``.npz`` format when numpy is available,
        otherwise in ``.json`` format. The cache dir is created if it does
        not exist.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        try:
            import numpy as np
        except Exception:
            # Fall back to JSON.
            path = self._path_for(snapshot.snapshot_id, "json")
            path.write_text(
                json.dumps(snapshot.model_dump(), indent=2),
                encoding="utf-8",
            )
            return str(path)

        path = self._path_for(snapshot.snapshot_id, "npz")
        node_features = np.asarray(snapshot.node_features, dtype=np.float32)
        edge_index = np.asarray(snapshot.edge_index, dtype=np.int64)
        arrays: dict[str, Any] = {
            "node_features": node_features,
            "edge_index": edge_index,
        }
        if snapshot.edge_weights is not None:
            arrays["edge_weights"] = np.asarray(snapshot.edge_weights, dtype=np.float32)
        # Scalar metadata stored as 0-d arrays.
        arrays["snapshot_id"] = np.array(snapshot.snapshot_id)
        arrays["n_nodes"] = np.array(snapshot.n_nodes)
        arrays["n_edges"] = np.array(snapshot.n_edges)
        arrays["snapshot_time"] = np.array(snapshot.snapshot_time)
        arrays["data_hash"] = np.array(snapshot.data_hash)
        np.savez(str(path), **arrays)
        return str(path)

    def load(self, snapshot_id: str) -> GraphSnapshot:
        """Load a snapshot from the cache dir by id.

        Looks for ``{snapshot_id}.npz`` first, then ``{snapshot_id}.json``.

        Raises:
            FileNotFoundError: if no snapshot with the given id exists.
            ValueError: if the loaded payload is malformed.
        """
        npz_path = self._path_for(snapshot_id, "npz")
        json_path = self._path_for(snapshot_id, "json")

        if npz_path.exists():
            import numpy as np

            with np.load(str(npz_path), allow_pickle=True) as npz:
                node_features = npz["node_features"].tolist()
                edge_index = npz["edge_index"].tolist()
                edge_weights: list[float] | None = None
                if "edge_weights" in npz.files:
                    edge_weights = npz["edge_weights"].tolist()
                snapshot_id_loaded = str(npz["snapshot_id"].item())
                n_nodes = int(npz["n_nodes"].item())
                n_edges = int(npz["n_edges"].item())
                snapshot_time = str(npz["snapshot_time"].item())
                data_hash = str(npz["data_hash"].item())
            return GraphSnapshot(
                snapshot_id=snapshot_id_loaded,
                n_nodes=n_nodes,
                n_edges=n_edges,
                node_features=node_features,
                edge_index=edge_index,
                edge_weights=edge_weights,
                snapshot_time=snapshot_time,
                data_hash=data_hash,
            )

        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return GraphSnapshot(**data)

        raise FileNotFoundError(f"snapshot not found in cache dir {self.cache_dir}: {snapshot_id}")

    def list_snapshots(self) -> list[str]:
        """Return a sorted list of snapshot ids present in the cache dir."""
        if not self._dir.exists():
            return []
        ids: set[str] = set()
        for f in self._dir.iterdir():
            if f.is_file() and f.suffix in (".npz", ".json"):
                ids.add(f.stem)
        return sorted(ids)

    def validate_snapshot(self, snapshot: GraphSnapshot) -> bool:
        """Validate a snapshot's shapes and hash integrity.

        Returns ``True`` if the node feature / edge index shapes are
        consistent with ``n_nodes`` / ``n_edges`` and the stored
        ``data_hash`` matches a recomputed hash. Returns ``False``
        otherwise.
        """
        try:
            # Shape checks (also enforced by the Pydantic model, but we
            # re-check defensively in case the snapshot was constructed
            # via model_construct or mutated).
            if len(snapshot.node_features) != snapshot.n_nodes:
                return False
            if len(snapshot.edge_index) != 2:
                return False
            if len(snapshot.edge_index[0]) != snapshot.n_edges:
                return False
            if len(snapshot.edge_index[1]) != snapshot.n_edges:
                return False
            if snapshot.edge_weights is not None and len(snapshot.edge_weights) != snapshot.n_edges:
                return False
            # Hash integrity.
            if not snapshot.verify_hash():
                return False
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# GPU memory planner
# ---------------------------------------------------------------------------


class GPUMemoryPlanner:
    """Estimates GPU memory needed for a graph snapshot + GNN pass.

    The estimate covers node features, edge index, and GNN layer
    parameters + activations. All estimates are in megabytes (MB) and use
    a 4-byte float / 8-byte int assumption. The planner is pure-Python
    (no torch import) so it can run on hosts without torch.
    """

    BYTES_PER_FLOAT = 4
    BYTES_PER_INT = 8

    def __init__(self, config: GraphSnapshotConfig) -> None:
        self.config = config

    def estimate_memory(
        self,
        n_nodes: int | None = None,
        n_edges: int | None = None,
    ) -> dict[str, float]:
        """Estimate GPU memory (MB) for a graph of the given size.

        Args:
            n_nodes: override ``config.n_nodes`` if given.
            n_edges: override ``config.n_edges`` if given.

        Returns:
            A dict with ``node_memory_mb``, ``edge_memory_mb``,
            ``layer_memory_mb``, and ``total_memory_mb``.
        """
        nn = n_nodes if n_nodes is not None else self.config.n_nodes
        ne = n_edges if n_edges is not None else self.config.n_edges

        # Node features: [N, F] floats.
        node_bytes = nn * self.config.node_feature_dim * self.BYTES_PER_FLOAT
        node_memory_mb = node_bytes / (1024.0 * 1024.0)

        # Edge index: [2, E] ints.
        edge_bytes = 2 * ne * self.BYTES_PER_INT
        # Edge features (if any): [E, EF] floats.
        if self.config.edge_feature_dim > 0:
            edge_bytes += ne * self.config.edge_feature_dim * self.BYTES_PER_FLOAT
        # Edge weights: [E] floats (assumed present).
        edge_bytes += ne * self.BYTES_PER_FLOAT
        edge_memory_mb = edge_bytes / (1024.0 * 1024.0)

        # GNN layers: parameters + activations.
        # Layer i has weight matrix [in_dim, out_dim] + bias [out_dim].
        # Activations: node embeddings [N, hidden_dim] per layer.
        layer_bytes = 0
        prev_dim = self.config.node_feature_dim
        for _ in range(self.config.n_layers):
            # Weight + bias.
            layer_bytes += prev_dim * self.config.hidden_dim * self.BYTES_PER_FLOAT
            layer_bytes += self.config.hidden_dim * self.BYTES_PER_FLOAT
            # Activation (node embeddings).
            layer_bytes += nn * self.config.hidden_dim * self.BYTES_PER_FLOAT
            # Message buffer: [E, hidden_dim] aggregated.
            layer_bytes += ne * self.config.hidden_dim * self.BYTES_PER_FLOAT
            prev_dim = self.config.hidden_dim
        # Gradients (roughly same size as parameters + activations).
        layer_bytes += layer_bytes // 2
        layer_memory_mb = layer_bytes / (1024.0 * 1024.0)

        total_memory_mb = node_memory_mb + edge_memory_mb + layer_memory_mb
        return {
            "node_memory_mb": node_memory_mb,
            "edge_memory_mb": edge_memory_mb,
            "layer_memory_mb": layer_memory_mb,
            "total_memory_mb": total_memory_mb,
        }

    def fits_in_gpu(
        self,
        available_mb: float,
        n_nodes: int | None = None,
        n_edges: int | None = None,
    ) -> bool:
        """Return ``True`` if the estimated memory fits in ``available_mb``.

        Adds a 20% safety margin to the estimate before comparing.
        """
        estimate = self.estimate_memory(n_nodes=n_nodes, n_edges=n_edges)
        total = estimate["total_memory_mb"] * 1.2
        return total <= available_mb


# ---------------------------------------------------------------------------
# Tiny GNN model
# ---------------------------------------------------------------------------


class TinyGNNModel:
    """A simple 2-layer Graph Convolutional Network (GCN).

    Architecture: ``node_feature_dim`` -> ``hidden_dim`` -> ``hidden_dim``
    with ReLU + Dropout between layers. The graph convolution is
    implemented in pure torch via scatter-add message passing, so the
    model runs on CPU-only hosts without ``torch_geometric`` installed.
    The tensor conventions (``node_features`` as ``[N, F]`` and
    ``edge_index`` as ``[2, E]``) match PyTorch Geometric, so the model
    can be swapped for a real ``torch_geometric.nn.GCNConv`` inside the
    GPU worker container without changing call sites.

    This is a thin wrapper around ``torch.nn.Module``. It is defined as a
    regular class (not a subclass of ``nn.Module`` at the type level) so
    that the module remains importable without torch — the actual
    ``nn.Module`` subclass is built lazily inside :meth:`_build_module`.
    """

    def __init__(
        self,
        node_feature_dim: int,
        hidden_dim: int = 16,
        output_dim: int = 16,
        dropout: float = 0.1,
    ) -> None:
        if node_feature_dim <= 0:
            raise ValueError("node_feature_dim must be positive")
        if hidden_dim <= 0:
            raise ValueError("hidden_dim must be positive")
        if output_dim <= 0:
            raise ValueError("output_dim must be positive")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.node_feature_dim = node_feature_dim
        self.hidden_dim = hidden_dim
        self.output_dim = output_dim
        self.dropout = dropout
        self._module: Any = None

    def _build_module(self) -> Any:
        """Build and return the underlying ``torch.nn.Module``.

        Lazily imports torch. The built module is cached on
        ``self._module`` so repeated calls return the same instance.
        """
        if self._module is not None:
            return self._module

        import torch
        import torch.nn as nn

        planner = self

        class _GCNLayer(nn.Module):  # type: ignore[misc]  # torch nn.Module is Any when torch not installed
            """A single graph convolution layer (pure torch)."""

            def __init__(self, in_dim: int, out_dim: int) -> None:
                super().__init__()
                self.linear = nn.Linear(in_dim, out_dim)

            def forward(
                self,
                x: Any,
                edge_index: Any,
                edge_weight: Any | None = None,
            ) -> Any:
                # x: [N, in_dim], edge_index: [2, E].
                x_transformed = self.linear(x)  # [N, out_dim]
                src = edge_index[0]  # [E]
                dst = edge_index[1]  # [E]
                # Message: source node's transformed features.
                messages = x_transformed[src]  # [E, out_dim]
                if edge_weight is not None:
                    messages = messages * edge_weight.unsqueeze(-1)
                # Aggregate by destination via scatter_add.
                agg = torch.zeros_like(x_transformed)  # [N, out_dim]
                agg.index_add_(0, dst, messages)
                # Self-loop + neighbor aggregation (GCN-style).
                return x_transformed + agg

        class _TinyGNN(nn.Module):  # type: ignore[misc]  # torch nn.Module is Any when torch not installed
            """2-layer GCN with ReLU + Dropout."""

            def __init__(self) -> None:
                super().__init__()
                self.conv1 = _GCNLayer(planner.node_feature_dim, planner.hidden_dim)
                self.conv2 = _GCNLayer(planner.hidden_dim, planner.output_dim)
                self.dropout = nn.Dropout(planner.dropout)

            def forward(
                self,
                node_features: Any,
                edge_index: Any,
                edge_weight: Any | None = None,
            ) -> Any:
                h = self.conv1(node_features, edge_index, edge_weight)
                h = torch.relu(h)
                h = self.dropout(h)
                h = self.conv2(h, edge_index, edge_weight)
                return h  # node embeddings [N, output_dim]

        net = _TinyGNN()
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(
        self,
        node_features: Any,
        edge_index: Any,
        edge_weight: Any | None = None,
    ) -> Any:
        """Run a forward pass: ``(node_features, edge_index) -> embeddings``.

        Args:
            node_features: ``[N, node_feature_dim]`` tensor.
            edge_index: ``[2, E]`` tensor (source row, destination row).
            edge_weight: optional ``[E]`` tensor of per-edge weights.

        Returns:
            Node embeddings of shape ``[N, output_dim]``.
        """
        return self.module(node_features, edge_index, edge_weight)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return cast("dict[str, Any]", self.module.state_dict())

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> TinyGNNModel:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> TinyGNNModel:
        """Set the underlying module's training mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> TinyGNNModel:
        """Set the underlying module to eval mode and return self."""
        self.module.eval()
        return self


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


def _build_synthetic_snapshot(snapshot_id: str = "healthcheck") -> GraphSnapshot:
    """Build a tiny synthetic graph snapshot for the healthcheck.

    The graph has 4 nodes and 6 edges with 3-dim node features. The
    snapshot is deterministic (fixed snapshot_time) so the hash is stable.
    """
    n_nodes = 4
    n_edges = 6
    node_features = [
        [0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6],
        [0.7, 0.8, 0.9],
        [1.0, 1.1, 1.2],
    ]
    # 6 edges (src -> dst), 0-indexed.
    edge_index = [
        [0, 0, 1, 1, 2, 3],
        [1, 2, 2, 3, 3, 0],
    ]
    snapshot_time = "2024-01-01T00:00:00+00:00"
    return GraphSnapshot.build(
        snapshot_id=snapshot_id,
        n_nodes=n_nodes,
        n_edges=n_edges,
        node_features=node_features,
        edge_index=edge_index,
        snapshot_time=snapshot_time,
    )


class GraphHealthcheck:
    """Healthcheck for the graph runtime.

    Probes the GPU via :func:`check_gpu` (reused from
    ``tabular_neural_runtime``), loads a tiny synthetic graph snapshot,
    and runs a tiny GNN forward pass. Used by the GPU worker's
    ``HEALTHCHECK`` step to fail fast when the runtime is broken or the
    GPU is missing.
    """

    def __init__(self, timeout_seconds: int = 60) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def run(self) -> dict[str, Any]:
        """Run the healthcheck and return a status dict.

        The dict contains:

        - ``healthy`` (bool): overall health.
        - ``gpu`` (dict): serialized :class:`GPUStatus`.
        - ``snapshot_load`` (bool): whether the synthetic snapshot load succeeded.
        - ``forward_pass`` (bool): whether the tiny GNN forward pass succeeded.
        - ``error`` (str | None): error message if the check failed.
        - ``duration_seconds`` (float): wall-clock duration.
        """
        from quant_foundry.tabular_neural_runtime import check_gpu

        start = time.perf_counter()
        result: dict[str, Any] = {
            "healthy": False,
            "gpu": None,
            "snapshot_load": False,
            "forward_pass": False,
            "error": None,
            "duration_seconds": 0.0,
        }

        try:
            gpu_status = check_gpu()
            result["gpu"] = gpu_status.model_dump()
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = f"gpu probe failed: {exc}"
            result["duration_seconds"] = time.perf_counter() - start
            return result

        # Tiny synthetic graph snapshot load + GNN forward pass on CPU.
        try:
            import torch

            snapshot = _build_synthetic_snapshot()
            result["snapshot_load"] = True

            node_features = torch.tensor(snapshot.node_features, dtype=torch.float32)
            edge_index = torch.tensor(snapshot.edge_index, dtype=torch.long)
            model = TinyGNNModel(node_feature_dim=3, hidden_dim=8, output_dim=8)
            out = model.forward(node_features, edge_index)
            # Force a reduction so the output is a scalar-ish value.
            _ = float(out.sum().item())
            result["forward_pass"] = True
        except Exception as exc:
            result["error"] = f"graph probe failed: {exc}"

        gpu_ok = bool(result["gpu"].get("available")) if result["gpu"] else False
        result["healthy"] = bool(
            gpu_ok
            and result["snapshot_load"]
            and result["forward_pass"]
            and result["error"] is None
        )
        result["duration_seconds"] = time.perf_counter() - start
        return result

    def is_healthy(self) -> bool:
        """Return ``True`` if the GPU is available and both probes succeed.

        Note: on a CPU-only host this returns ``False`` because the GPU is
        not available. The healthcheck is intended for the GPU worker
        container, where a missing GPU is a hard failure.
        """
        status = self.run()
        return bool(status.get("healthy"))


__all__ = [
    "GPUMemoryPlanner",
    "GraphHealthcheck",
    "GraphImageSpec",
    "GraphSnapshot",
    "GraphSnapshotConfig",
    "GraphSnapshotLoader",
    "TinyGNNModel",
]
