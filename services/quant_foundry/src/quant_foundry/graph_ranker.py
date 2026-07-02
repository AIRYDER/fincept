"""quant_foundry.graph_ranker — GNN-based graph ranker (T-12.4).

This module provides a **graph ranker** that uses a Graph Neural Network
(GNN) to predict relative return ranks for symbols (graph nodes), with
edge attribution and point-in-time (PIT) safe edge handling.

The graph ranker treats each instrument symbol as a node and edges
(``"sector"``, ``"industry"``, ``"correlation"``, ``"supply_chain"``)
as relational structure. A small GCN (:class:`GraphRankerModel`, built
on :class:`~quant_foundry.graph_runtime.TinyGNNModel`) produces a
node-level score per snapshot; the scores are sorted to produce a
predicted ranking of symbols. Training uses a listMLE-style ranking
loss so the model learns *relative* ordering rather than absolute
returns.

Capabilities:

- :class:`GraphRankerConfig` — frozen, ``extra='forbid'`` config for a
  graph ranker training run (architecture, optimizer, shadow-mode
  defaults).
- :class:`GraphRankerResult` — frozen, ``extra='forbid'`` result
  carrying epoch losses, GPU status, artifact paths, rank metrics,
  edge attribution, the predicted ranking, and promotion eligibility.
- :class:`GraphRankerModel` — an ``nn.Module`` wrapper that produces
  node-level ranking scores ``(n_nodes, 1)``.
- :class:`GraphRanker` — the train / predict / save / load / OOF-write
  façade used by the research dispatch path.
- :func:`compute_edge_attribution` — mask edges by type and measure the
  change in predicted scores (edge-type importance).
- :func:`validate_edge_availability` — PIT-safe edge availability check
  (fail-closed on a future edge).
- :func:`compute_graph_rank_metrics` — ndcg, MAP, and Kendall's tau
  from a ranked prediction list.
- :func:`validate_promotion_eligibility` — fail-closed promotion gate
  (shadow runs are only eligible with an explicit manual override).
- :func:`register_graph_ranker_family` — returns a
  :class:`~quant_foundry.alpha_genome.ModelFamilySpec`-compatible dict
  for graph ranker registration.

Design notes (cross-cutting quant rigor, BIG_PLAN):

- **Shadow mode by default.** ``GraphRankerConfig.shadow_only`` defaults
  to ``True`` and ``GraphRankerResult.promotion_eligible`` is forced to
  ``False`` when shadow mode is on. Promotion requires an explicit
  manual override.
- **PIT-safe edge handling.** :func:`validate_edge_availability`
  fail-closes when an edge's ``edge_available_at`` is after the decision
  time (no future leakage into the graph snapshot).
- **No live trading authority.** A shadow graph ranker never produces
  tradeable predictions; its outputs are OOF predictions for ensemble
  integration and a model artifact for offline evaluation only.
- **No secrets.** Configs carry only hyperparameters, a device string,
  and a seed — never credentials.
- **Cost fails closed.** Invalid configs are rejected at construction;
  training errors surface as exceptions.
- **Lazy torch import.** ``import torch`` happens inside methods, never
  at module top level, so this module is importable on hosts without
  torch (the Pydantic models and ``register_graph_ranker_family`` can
  be constructed without torch installed).
- **File-disjoint.** New module; does not modify ``graph_runtime.py``,
  ``graph_manifest.py``, ``rank_metrics.py``, or ``oof_artifacts.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_foundry.graph_runtime import GraphSnapshot, TinyGNNModel
from quant_foundry.oof_artifacts import OOFWriter
from quant_foundry.tabular_neural_runtime import GPUStatus, check_gpu


# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


class GraphRankerConfig(BaseModel):
    """Configuration for a graph ranker training run.

    Frozen + ``extra='forbid'`` for audit integrity. Defaults are
    shadow-oriented: ``shadow_only=True``, a modest 2-layer GCN
    (``hidden_dim=64``), and a small number of epochs so a run completes
    quickly on CPU for smoke tests while still exercising the full
    graph-ranker code path.

    Attributes:
        node_feature_dim: Number of node features (must be >= 1).
        hidden_dim: GNN hidden dimension (must be >= 1).
        n_layers: Number of GNN layers (must be >= 1).
        dropout: Dropout probability (must be in [0, 1)).
        learning_rate: Adam learning rate (must be > 0).
        epochs: Number of training epochs (must be >= 0).
        batch_size: Graph-level batch size (one snapshot at a time).
        device: Device to run on — ``auto``, ``cpu``, or ``cuda``.
        seed: Random seed for reproducibility.
        shadow_only: When ``True`` (default) the run is marked shadow
            and ``promotion_eligible`` is forced to ``False``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_feature_dim: int
    hidden_dim: int = 64
    n_layers: int = 2
    dropout: float = 0.1
    learning_rate: float = 0.001
    epochs: int = 10
    batch_size: int = 1
    device: str = "auto"
    seed: int = 42
    shadow_only: bool = True

    @field_validator("node_feature_dim")
    @classmethod
    def _node_feature_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"node_feature_dim must be >= 1; got {v}")
        return v

    @field_validator("hidden_dim")
    @classmethod
    def _hidden_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"hidden_dim must be >= 1; got {v}")
        return v

    @field_validator("n_layers")
    @classmethod
    def _n_layers_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_layers must be >= 1; got {v}")
        return v

    @field_validator("dropout")
    @classmethod
    def _dropout_range(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError(f"dropout must be in [0, 1); got {v}")
        return v

    @field_validator("learning_rate")
    @classmethod
    def _learning_rate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"learning_rate must be > 0; got {v}")
        return v

    @field_validator("epochs")
    @classmethod
    def _epochs_nonnegative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"epochs must be >= 0; got {v}")
        return v

    @field_validator("batch_size")
    @classmethod
    def _batch_size_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"batch_size must be >= 1; got {v}")
        return v

    @field_validator("device")
    @classmethod
    def _device_allowed(cls, v: str) -> str:
        allowed = {"auto", "cpu", "cuda"}
        if v not in allowed:
            raise ValueError(
                f"device must be one of {sorted(allowed)}; got {v!r}"
            )
        return v


class GraphRankerResult(BaseModel):
    """Result of a graph ranker training run.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the config
    used, the per-epoch losses, the GPU status at training time, the
    paths to the saved model / OOF artifacts (if any), the rank metrics,
    the edge attribution, the predicted ranking, and the
    promotion-eligibility flag.

    Attributes:
        config: The :class:`GraphRankerConfig` used for the run.
        final_loss: The mean loss of the final epoch (NaN if 0 epochs).
        epoch_losses: Mean loss per epoch (one entry per epoch).
        gpu_status: GPU availability + memory state at training time.
        artifact_path: Path to the saved model state_dict, or ``None``.
        oof_artifact_path: Path to the OOF predictions artifact, or
            ``None`` if no OOF predictions were written.
        is_shadow: ``True`` when the run was in shadow mode.
        promotion_eligible: ``False`` when ``is_shadow`` is ``True``;
            only ``True`` for non-shadow runs (or shadow runs that later
            pass :func:`validate_promotion_eligibility` with a manual
            override).
        metrics: Rank metrics (``ndcg``, ``map``, ``kendall_tau``).
        edge_attribution: Edge-type -> importance score.
        ranked_symbols: Predicted ranking of node ids (best first), or
            ``None`` if no prediction was made.
        duration_seconds: Wall-clock training duration in seconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: GraphRankerConfig
    final_loss: float
    epoch_losses: list[float] = Field(default_factory=list)
    gpu_status: GPUStatus
    artifact_path: str | None = None
    oof_artifact_path: str | None = None
    is_shadow: bool
    promotion_eligible: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    edge_attribution: dict[str, float] = Field(default_factory=dict)
    ranked_symbols: list[str] | None = None
    duration_seconds: float


# ---------------------------------------------------------------------------
# Graph ranker model
# ---------------------------------------------------------------------------


class GraphRankerModel:
    """A GNN model that produces node-level ranking scores.

    Architecture: a stack of ``n_layers`` graph convolution layers
    (built on :class:`~quant_foundry.graph_runtime.TinyGNNModel`) that
    maps node features to a hidden representation, followed by a linear
    output head that produces a single ranking score per node.

    Forward pass: ``(node_features, edge_index) -> (n_nodes, 1)`` scores.

    This is a thin wrapper around ``torch.nn.Module`` (built lazily so
    the module remains importable without torch), mirroring the pattern
    in :class:`~quant_foundry.patchtst_trainer.PatchTSTModel`.
    """

    def __init__(
        self,
        node_feature_dim: int,
        hidden_dim: int = 64,
        n_layers: int = 2,
        dropout: float = 0.1,
    ) -> None:
        if node_feature_dim < 1:
            raise ValueError("node_feature_dim must be >= 1")
        if hidden_dim < 1:
            raise ValueError("hidden_dim must be >= 1")
        if n_layers < 1:
            raise ValueError("n_layers must be >= 1")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.node_feature_dim = node_feature_dim
        self.hidden_dim = hidden_dim
        self.n_layers = n_layers
        self.dropout = dropout
        self._module: Any = None
        self._gnn: TinyGNNModel | None = None

    def _build_module(self) -> Any:
        """Build and return the underlying ``torch.nn.Module``.

        Lazily imports torch. The built module is cached on
        ``self._module`` so repeated calls return the same instance.
        """
        if self._module is not None:
            return self._module

        import torch  # noqa: WPS433 lazy import
        import torch.nn as nn  # noqa: WPS433 lazy import

        # The GNN backbone produces hidden_dim node embeddings.
        gnn = TinyGNNModel(
            node_feature_dim=self.node_feature_dim,
            hidden_dim=self.hidden_dim,
            output_dim=self.hidden_dim,
            dropout=self.dropout,
        )
        # Build the underlying nn.Module so we can compose it.
        gnn_module = gnn.module
        head = nn.Linear(self.hidden_dim, 1)

        planner = self

        class _GraphRankerNet(nn.Module):
            """Inner nn.Module implementing the graph ranker forward pass."""

            def __init__(self) -> None:
                super().__init__()
                self.gnn = gnn_module
                self.head = head

            def forward(
                self,
                node_features: Any,
                edge_index: Any,
                edge_weight: Any | None = None,
            ) -> Any:
                # node_features: [N, F], edge_index: [2, E].
                h = self.gnn(node_features, edge_index, edge_weight)
                # h: [N, hidden_dim].
                scores = self.head(h)  # [N, 1]
                return scores

        net = _GraphRankerNet()
        self._module = net
        self._gnn = gnn
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
        """Run a forward pass: ``(node_features, edge_index) -> scores``.

        Args:
            node_features: ``[N, node_feature_dim]`` tensor.
            edge_index: ``[2, E]`` tensor (source row, destination row).
            edge_weight: optional ``[E]`` tensor of per-edge weights.

        Returns:
            Node scores of shape ``[N, 1]``.
        """
        return self.module(node_features, edge_index, edge_weight)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return self.module.state_dict()

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> "GraphRankerModel":
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> "GraphRankerModel":
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> "GraphRankerModel":
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def _resolve_device(config: GraphRankerConfig, gpu_status: GPUStatus) -> Any:
    """Resolve the torch device for a graph ranker run.

    ``auto`` picks CUDA when available, else CPU. ``cpu`` / ``cuda`` are
    honored literally (``cuda`` on a CPU-only host falls back to CPU).
    """
    import torch  # noqa: WPS433 lazy import

    if config.device == "cpu":
        return torch.device("cpu")
    if config.device == "cuda":
        if gpu_status.available:
            return torch.device("cuda")
        return torch.device("cpu")
    # auto
    return torch.device("cuda" if gpu_status.available else "cpu")


# ---------------------------------------------------------------------------
# Ranking loss (listMLE-style)
# ---------------------------------------------------------------------------


def _listMLE_loss(scores: Any, labels: Any) -> Any:
    """Compute a listMLE-style ranking loss.

    Given node scores ``[N]`` and labels ``[N]`` (relative return ranks,
    higher = better), the loss sorts nodes by label (descending) and
    computes the negative log-likelihood of the observed ranking under a
    Plackett-Luce model. This is the standard listMLE objective.

    Args:
        scores: 1-D tensor of predicted scores ``[N]``.
        labels: 1-D tensor of target relevance / ranks ``[N]``.

    Returns:
        A scalar tensor (the mean listMLE loss).
    """
    import torch  # noqa: WPS433 lazy import

    n = scores.shape[0]
    if n < 2:
        return torch.tensor(0.0, requires_grad=True, device=scores.device)
    # Sort by label descending (best first).
    sorted_idx = torch.argsort(labels, descending=True)
    scores_sorted = scores[sorted_idx]  # [N]
    # listMLE: sum_{i=1..N} log( exp(s_i) / sum_{j>=i} exp(s_j) )
    # = sum_i [ s_i - logsumexp(scores_sorted[i:] ) ]
    # We compute logsumexp from the end.
    max_val = scores_sorted.max()
    scores_sorted = scores_sorted - max_val  # numerical stability
    # Cumulative sum of exp from the end.
    exp_scores = torch.exp(scores_sorted)
    # Reverse cumsum: cumsum from the end to the start.
    cumsum_from_end = torch.flip(
        torch.cumsum(torch.flip(exp_scores, dims=[0]), dim=0), dims=[0]
    )
    logsumexp_from_end = torch.log(cumsum_from_end) + max_val
    # listMLE loss = -sum_i (scores_sorted[i] - logsumexp_from_end[i])
    # (we already subtracted max_val from scores_sorted, and
    # logsumexp_from_end has max_val added back, so the max_val cancels).
    loss = -(scores_sorted + max_val - logsumexp_from_end).sum()
    return loss / float(n)


# ---------------------------------------------------------------------------
# GraphRanker
# ---------------------------------------------------------------------------


class GraphRanker:
    """Train / predict / save / load / OOF-write façade for the graph ranker.

    The ranker builds a :class:`GraphRankerModel`, trains it with Adam
    on the provided graph snapshots / labels (one snapshot per time
    step), saves the trained state_dict, and can write OOF predictions
    via :class:`OOFWriter`.

    Args:
        config: The :class:`GraphRankerConfig` for the run.
        node_ids: The list of node ids (symbols) the ranker will rank.
            The order of ``node_ids`` fixes the row order of node
            features / labels in every snapshot.
    """

    def __init__(
        self,
        config: GraphRankerConfig,
        node_ids: list[str],
    ) -> None:
        if not isinstance(config, GraphRankerConfig):
            raise TypeError("config must be a GraphRankerConfig")
        if not node_ids:
            raise ValueError("node_ids must contain at least 1 node id")
        for nid in node_ids:
            if not isinstance(nid, str) or not nid.strip():
                raise ValueError("node_ids entries must be non-empty strings")
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node_ids must not contain duplicates")
        self.config = config
        self.node_ids: list[str] = list(node_ids)
        self.model_: GraphRankerModel | None = None

    # -- training ---------------------------------------------------------

    def train(
        self,
        snapshots: list[GraphSnapshot],
        labels: list[list[float]],
        edge_types_per_snapshot: list[list[str]] | None = None,
    ) -> GraphRankerResult:
        """Train the graph ranker on ``snapshots`` / ``labels``.

        Args:
            snapshots: List of graph snapshots (one per time step).
                Every snapshot's ``n_nodes`` must equal
                ``len(self.node_ids)`` and every snapshot's
                ``node_feature_dim`` (inferred from
                ``node_features[i]``) must equal
                ``config.node_feature_dim``.
            labels: Relative return ranks for each snapshot's nodes
                (one list per snapshot, aligned with ``node_ids``).
                Higher = better. Each list must have length
                ``n_nodes``.
            edge_types_per_snapshot: Optional per-snapshot list of edge
                types (one per edge, aligned with ``edge_index``). Used
                for edge attribution. When ``None``, edge attribution is
                empty.

        Returns:
            A :class:`GraphRankerResult` with epoch losses, GPU status,
            rank metrics, edge attribution, the predicted ranking, and
            promotion eligibility.
        """
        import torch  # noqa: WPS433 lazy import
        import numpy as np  # noqa: WPS433 lazy import

        start = time.perf_counter()

        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)

        if not snapshots:
            raise ValueError("snapshots must contain at least 1 snapshot")
        if len(labels) != len(snapshots):
            raise ValueError(
                "labels must have the same length as snapshots; "
                f"got {len(labels)} vs {len(snapshots)}"
            )
        n_nodes = len(self.node_ids)
        for i, snap in enumerate(snapshots):
            if snap.n_nodes != n_nodes:
                raise ValueError(
                    f"snapshots[{i}].n_nodes ({snap.n_nodes}) must equal "
                    f"len(node_ids) ({n_nodes})"
                )
            if len(snap.node_features[0]) != self.config.node_feature_dim:
                raise ValueError(
                    f"snapshots[{i}] node_feature_dim "
                    f"({len(snap.node_features[0])}) must equal "
                    f"config.node_feature_dim "
                    f"({self.config.node_feature_dim})"
                )
            if len(labels[i]) != n_nodes:
                raise ValueError(
                    f"labels[{i}] length ({len(labels[i])}) must equal "
                    f"n_nodes ({n_nodes})"
                )

        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)

        model = GraphRankerModel(
            node_feature_dim=self.config.node_feature_dim,
            hidden_dim=self.config.hidden_dim,
            n_layers=self.config.n_layers,
            dropout=self.config.dropout,
        )
        model.to(device)
        model.train()

        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=self.config.learning_rate,
        )

        epoch_losses: list[float] = []
        final_loss = float("nan")

        if self.config.epochs > 0:
            for _epoch in range(self.config.epochs):
                batch_losses: list[float] = []
                # Graph-level batching: one snapshot at a time
                # (batch_size is graph-level, fixed at 1 snapshot).
                for i, snap in enumerate(snapshots):
                    node_features = torch.tensor(
                        snap.node_features, dtype=torch.float32
                    ).to(device)
                    edge_index = torch.tensor(
                        snap.edge_index, dtype=torch.long
                    ).to(device)
                    edge_weight = None
                    if snap.edge_weights is not None:
                        edge_weight = torch.tensor(
                            snap.edge_weights, dtype=torch.float32
                        ).to(device)
                    label_tensor = torch.tensor(
                        labels[i], dtype=torch.float32
                    ).to(device)

                    optimizer.zero_grad()
                    scores = model.forward(
                        node_features, edge_index, edge_weight
                    )  # [N, 1]
                    scores = scores.squeeze(-1)  # [N]
                    loss = _listMLE_loss(scores, label_tensor)
                    loss.backward()
                    optimizer.step()
                    batch_losses.append(float(loss.item()))

                if batch_losses:
                    epoch_loss = float(
                        sum(batch_losses) / len(batch_losses)
                    )
                else:
                    epoch_loss = float("nan")
                epoch_losses.append(epoch_loss)

            if epoch_losses:
                final_loss = epoch_losses[-1]

        self.model_ = model

        # Compute rank metrics + predicted ranking on the last snapshot.
        metrics: dict[str, float] = {}
        ranked_symbols: list[str] | None = None
        if snapshots:
            last_snap = snapshots[-1]
            ranked_symbols = self._predict_ranked(last_snap, device)
            # Build actual ranks from the last snapshot's labels.
            last_labels = labels[-1]
            actual_ranks = self._labels_to_ranks(last_labels)
            metrics = compute_graph_rank_metrics(
                predictions=ranked_symbols,
                actual_ranks=actual_ranks,
                node_ids=self.node_ids,
            )
            metrics["final_loss"] = final_loss

        # Compute edge attribution on the last snapshot.
        edge_attribution: dict[str, float] = {}
        if (
            snapshots
            and edge_types_per_snapshot is not None
            and len(edge_types_per_snapshot) == len(snapshots)
        ):
            last_edge_types = edge_types_per_snapshot[-1]
            last_snap = snapshots[-1]
            if last_edge_types and len(last_edge_types) == last_snap.n_edges:
                model.eval()
                edge_attribution = compute_edge_attribution(
                    model=model,
                    snapshot=last_snap,
                    edge_types=last_edge_types,
                    device=device,
                )

        duration = time.perf_counter() - start

        return GraphRankerResult(
            config=self.config,
            final_loss=final_loss,
            epoch_losses=epoch_losses,
            gpu_status=gpu_status,
            artifact_path=None,
            oof_artifact_path=None,
            is_shadow=self.config.shadow_only,
            promotion_eligible=not self.config.shadow_only,
            metrics=metrics,
            edge_attribution=edge_attribution,
            ranked_symbols=ranked_symbols,
            duration_seconds=duration,
        )

    def _labels_to_ranks(self, labels: list[float]) -> list[int]:
        """Convert labels (higher = better) to 1-based ranks (1 = best).

        Args:
            labels: list of label values (higher = better).

        Returns:
            list of 1-based ranks aligned with ``labels``.
        """
        import numpy as np  # noqa: WPS433 lazy import

        arr = np.asarray(labels, dtype=np.float64)
        # Rank descending: best (highest) gets rank 1.
        order = np.argsort(-arr, kind="stable")
        ranks = np.empty(len(arr), dtype=np.int64)
        for position, idx in enumerate(order):
            ranks[idx] = position + 1
        return [int(r) for r in ranks]

    def _predict_scores(
        self,
        snapshot: GraphSnapshot,
        device: Any,
    ) -> list[float]:
        """Run a forward pass on ``snapshot`` and return node scores.

        Args:
            snapshot: The graph snapshot to predict on.
            device: The torch device to run on.

        Returns:
            A list of floats (one score per node, aligned with
            ``self.node_ids``).
        """
        import torch  # noqa: WPS433 lazy import

        if self.model_ is None:
            raise ValueError(
                "no trained model available — call train() or "
                "load_artifact() first"
            )
        if snapshot.n_nodes != len(self.node_ids):
            raise ValueError(
                f"snapshot.n_nodes ({snapshot.n_nodes}) must equal "
                f"len(node_ids) ({len(self.node_ids)})"
            )

        model = self.model_
        model.eval()
        node_features = torch.tensor(
            snapshot.node_features, dtype=torch.float32
        ).to(device)
        edge_index = torch.tensor(
            snapshot.edge_index, dtype=torch.long
        ).to(device)
        edge_weight = None
        if snapshot.edge_weights is not None:
            edge_weight = torch.tensor(
                snapshot.edge_weights, dtype=torch.float32
            ).to(device)

        with torch.no_grad():
            scores = model.forward(node_features, edge_index, edge_weight)
            scores_np = scores.squeeze(-1).cpu().numpy()
        return [float(v) for v in scores_np]

    def _predict_ranked(
        self,
        snapshot: GraphSnapshot,
        device: Any,
    ) -> list[str]:
        """Predict scores on ``snapshot`` and return ranked node ids.

        Args:
            snapshot: The graph snapshot to predict on.
            device: The torch device to run on.

        Returns:
            A list of node ids sorted by predicted score (best first).
        """
        import numpy as np  # noqa: WPS433 lazy import

        scores = self._predict_scores(snapshot, device)
        arr = np.asarray(scores, dtype=np.float64)
        # Sort descending (best first). Stable sort preserves node_ids
        # order for ties.
        order = np.argsort(-arr, kind="stable")
        return [self.node_ids[int(i)] for i in order]

    # -- prediction -------------------------------------------------------

    def predict(self, snapshot: GraphSnapshot) -> list[str]:
        """Predict a ranked list of node ids for ``snapshot``.

        Uses the in-memory trained model (or the model loaded via
        :meth:`load_artifact`), runs a forward pass, and returns the
        node ids sorted by predicted score (best first).

        Args:
            snapshot: The graph snapshot to predict on.

        Returns:
            A list of node ids (symbols) sorted by predicted score
            (best first).
        """
        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)
        return self._predict_ranked(snapshot, device)

    # -- artifact persistence ---------------------------------------------

    def save_artifact(self, path: str) -> None:
        """Save the trained model's state_dict to ``path``.

        Creates parent directories as needed. The file is written via
        ``torch.save`` (state_dict only — no pickled module).

        Raises:
            ValueError: if no model has been trained.
        """
        import torch  # noqa: WPS433 lazy import

        if self.model_ is None:
            raise ValueError(
                "no trained model to save — call train() first"
            )
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        torch.save(self.model_.state_dict(), str(p))

    def load_artifact(self, path: str) -> GraphRankerModel:
        """Load a state_dict from ``path`` into a new :class:`GraphRankerModel`.

        The new model is built from the ranker's config and the saved
        state_dict is loaded into it. The model is set to eval mode on
        CPU and stored on the ranker (``self.model_``).

        Returns:
            The loaded :class:`GraphRankerModel`.
        """
        import torch  # noqa: WPS433 lazy import

        model = GraphRankerModel(
            node_feature_dim=self.config.node_feature_dim,
            hidden_dim=self.config.hidden_dim,
            n_layers=self.config.n_layers,
            dropout=self.config.dropout,
        )
        # Build the underlying module before loading state_dict.
        _ = model.module
        state_dict = torch.load(str(path), map_location="cpu")
        model.load_state_dict(state_dict)
        model.eval()
        self.model_ = model
        return model

    # -- OOF writing ------------------------------------------------------

    def write_oof_predictions(
        self,
        fold_predictions: list[list[float]],
        fold_ids: list[int],
        symbols: list[str],
        timestamps: list[str],
        labels: list[float],
        horizons: list[int],
        weights: list[float] | None,
        output_path: str,
    ) -> str:
        """Write OOF predictions for ensemble integration.

        Uses :class:`OOFWriter` from :mod:`quant_foundry.oof_artifacts`
        to write the predictions in the standard OOF artifact schema so
        they can be merged with other model families' OOF predictions
        for stacking.

        Args:
            fold_predictions: Per-fold predictions. Each entry is the
                list of node-level scores for one fold's snapshot.
                Must have the same length as ``fold_ids``.
            fold_ids: Per-fold fold ids.
            symbols: Per-row instrument symbols (flattened across
                folds).
            timestamps: Per-row ISO-format timestamps.
            labels: Per-row ground-truth labels.
            horizons: Per-row prediction horizons.
            weights: Per-row sample weights. When ``None``, 1.0 is
                used for every row.
            output_path: Path to write the OOF artifact to. The file is
                named ``oof_graph_ranker.json`` in the parent directory
                of this path (the parent directory is the OOF output
                dir).

        Returns:
            The path to the written OOF artifact file.
        """
        n_folds = len(fold_predictions)
        if not (
            len(fold_ids) == n_folds
            and len(symbols) == n_folds
            and len(timestamps) == n_folds
            and len(labels) == n_folds
            and len(horizons) == n_folds
        ):
            raise ValueError(
                "fold_predictions, fold_ids, symbols, timestamps, "
                "labels, and horizons must all have the same length"
            )
        if weights is not None and len(weights) != n_folds:
            raise ValueError(
                "weights must have the same length as fold_predictions "
                "or be None"
            )

        output_dir = str(Path(output_path).parent)
        writer = OOFWriter(model_family="graph_ranker", output_dir=output_dir)
        for i in range(n_folds):
            # Each fold's prediction is a list of node scores; we
            # aggregate to a single representative prediction (the mean
            # score) for the OOF row. This keeps the OOF schema
            # (one prediction per row) compatible with the tabular
            # stackers.
            preds = fold_predictions[i]
            if preds:
                pred_value = float(sum(preds) / len(preds))
            else:
                pred_value = 0.0
            row_id = f"{symbols[i]}_{timestamps[i]}_{horizons[i]}"
            w = float(weights[i]) if weights is not None else 1.0
            writer.add_prediction(
                row_id=row_id,
                fold_id=int(fold_ids[i]),
                symbol=str(symbols[i]),
                timestamp=str(timestamps[i]),
                label=float(labels[i]),
                prediction=pred_value,
                horizon=int(horizons[i]),
                weight=w,
            )
        artifact = writer.flush()
        return artifact.artifact_uri


# ---------------------------------------------------------------------------
# Edge attribution
# ---------------------------------------------------------------------------


def compute_edge_attribution(
    model: GraphRankerModel,
    snapshot: GraphSnapshot,
    edge_types: list[str],
    device: Any | None = None,
) -> dict[str, float]:
    """Compute edge-type importance via edge masking.

    For each edge type, masks (removes) all edges of that type and
    measures the change in the predicted node scores. The importance
    score is the mean absolute change in node scores when that edge
    type is removed — a larger change means the edge type contributes
    more to the prediction.

    Args:
        model: The trained :class:`GraphRankerModel`.
        snapshot: The graph snapshot to compute attribution on.
        edge_types: A list of edge types, one per edge (aligned with
            ``snapshot.edge_index``). Must have length
            ``snapshot.n_edges``.
        device: Optional torch device. When ``None``, the model's
            current device is used.

    Returns:
        A dict mapping edge type -> importance score (mean absolute
        change in node scores when that edge type is masked).
    """
    import torch  # noqa: WPS433 lazy import
    import numpy as np  # noqa: WPS433 lazy import

    if len(edge_types) != snapshot.n_edges:
        raise ValueError(
            f"edge_types length ({len(edge_types)}) must equal "
            f"snapshot.n_edges ({snapshot.n_edges})"
        )

    if device is None:
        # Infer device from the model's first parameter.
        try:
            device = next(model.parameters()).device
        except StopIteration:
            device = torch.device("cpu")

    model.eval()
    node_features = torch.tensor(
        snapshot.node_features, dtype=torch.float32
    ).to(device)
    edge_index = torch.tensor(
        snapshot.edge_index, dtype=torch.long
    ).to(device)
    edge_weight = None
    if snapshot.edge_weights is not None:
        edge_weight = torch.tensor(
            snapshot.edge_weights, dtype=torch.float32
        ).to(device)

    # Baseline scores (all edges).
    with torch.no_grad():
        base_scores = model.forward(
            node_features, edge_index, edge_weight
        ).squeeze(-1).cpu().numpy()

    unique_types = sorted(set(edge_types))
    attribution: dict[str, float] = {}
    for etype in unique_types:
        # Mask edges of this type: keep edges whose type != etype.
        keep_mask = [t != etype for t in edge_types]
        keep_indices = [i for i, keep in enumerate(keep_mask) if keep]
        if keep_indices:
            masked_edge_index = edge_index[:, torch.tensor(
                keep_indices, dtype=torch.long, device=device
            )]
            masked_edge_weight = None
            if edge_weight is not None:
                masked_edge_weight = edge_weight[torch.tensor(
                    keep_indices, dtype=torch.long, device=device
                )]
        else:
            # All edges masked — use an empty edge index [2, 0].
            masked_edge_index = torch.zeros(
                (2, 0), dtype=torch.long, device=device
            )
            masked_edge_weight = None

        with torch.no_grad():
            masked_scores = model.forward(
                node_features, masked_edge_index, masked_edge_weight
            ).squeeze(-1).cpu().numpy()

        change = float(np.mean(np.abs(masked_scores - base_scores)))
        attribution[etype] = change

    return attribution


# ---------------------------------------------------------------------------
# PIT-safe edge availability
# ---------------------------------------------------------------------------


def validate_edge_availability(
    snapshot: GraphSnapshot,
    decision_time: str,
    edge_available_at: list[str] | None = None,
) -> bool:
    """Check that all edges are available at or before ``decision_time``.

    Point-in-time safety: an edge must have been available (``<=``) at
    the decision time to be used in a prediction. A future edge (one
    whose availability is after the decision time) would leak future
    information into the prediction.

    The :class:`GraphSnapshot` itself does not carry per-edge
    availability timestamps (it only carries ``snapshot_time``). The
    caller may supply ``edge_available_at`` — a list of ISO datetime
    strings, one per edge (aligned with ``snapshot.edge_index``). When
    supplied, each entry must be ``<= decision_time``. When ``None``,
    the snapshot's ``snapshot_time`` is used as a single availability
    timestamp for all edges (and must be ``<= decision_time``).

    Args:
        snapshot: The graph snapshot whose edges to check.
        decision_time: The ISO datetime of the prediction decision.
        edge_available_at: Optional per-edge availability timestamps.

    Returns:
        ``True`` if all edges are PIT-safe (available at or before the
        decision time).

    Raises:
        ValueError: if any edge's availability is after the decision
            time (future edge detected — fail-closed).
    """
    from quant_foundry.dataset_manifest import _parse_temporal

    decision_epoch = _parse_temporal(decision_time)

    if edge_available_at is not None:
        if len(edge_available_at) != snapshot.n_edges:
            raise ValueError(
                f"edge_available_at length ({len(edge_available_at)}) "
                f"must equal snapshot.n_edges ({snapshot.n_edges})"
            )
        for i, avail in enumerate(edge_available_at):
            avail_epoch = _parse_temporal(avail)
            if avail_epoch > decision_epoch:
                raise ValueError(
                    f"future edge detected: edge_available_at "
                    f"({avail!r}) must be <= decision_time "
                    f"({decision_time!r}) for edge index {i}"
                )
    else:
        # Use the snapshot's snapshot_time as the single availability.
        snap_epoch = _parse_temporal(snapshot.snapshot_time)
        if snap_epoch > decision_epoch:
            raise ValueError(
                f"future edge detected: snapshot_time "
                f"({snapshot.snapshot_time!r}) must be <= decision_time "
                f"({decision_time!r})"
            )

    return True


# ---------------------------------------------------------------------------
# Graph rank metrics
# ---------------------------------------------------------------------------


def compute_graph_rank_metrics(
    predictions: list[str],
    actual_ranks: list[int],
    node_ids: list[str],
) -> dict[str, float]:
    """Compute rank metrics (ndcg, MAP, Kendall's tau) from predictions.

    Recomputes rank metrics from a ranked prediction list so the metrics
    can be verified from the prediction artifacts alone (no model state
    required).

    Args:
        predictions: The predicted ranking — a list of node ids sorted
            by predicted score (best first).
        actual_ranks: The actual 1-based ranks of each node (1 = best),
            aligned with ``node_ids``.
        node_ids: The list of node ids in canonical order. ``actual_ranks``
            is aligned with this list.

    Returns:
        A dict with keys ``"ndcg"``, ``"map"``, and ``"kendall_tau"``.

    Raises:
        ValueError: if the inputs have mismatched lengths or
            ``predictions`` does not contain exactly the same node ids
            as ``node_ids``.
    """
    import numpy as np  # noqa: WPS433 lazy import

    if len(actual_ranks) != len(node_ids):
        raise ValueError(
            f"actual_ranks length ({len(actual_ranks)}) must equal "
            f"node_ids length ({len(node_ids)})"
        )
    if len(predictions) != len(node_ids):
        raise ValueError(
            f"predictions length ({len(predictions)}) must equal "
            f"node_ids length ({len(node_ids)})"
        )
    pred_set = set(predictions)
    node_set = set(node_ids)
    if pred_set != node_set:
        raise ValueError(
            f"predictions must contain exactly the same node ids as "
            f"node_ids; missing={node_set - pred_set}, "
            f"extra={pred_set - node_set}"
        )

    n = len(node_ids)
    # Map node_id -> actual rank.
    rank_map = {nid: actual_ranks[i] for i, nid in enumerate(node_ids)}

    # --- NDCG ---
    # Relevance = inverse actual rank (best = n, worst = 1) so higher
    # relevance is better. DCG over the predicted ordering.
    rel = np.array(
        [float(n - rank_map[nid] + 1) for nid in predictions],
        dtype=np.float64,
    )
    # Ideal: sort relevance descending.
    ideal_rel = np.sort(rel)[::-1]
    dcg = float(np.sum(rel / np.log2(np.arange(2, n + 2))))
    idcg = float(np.sum(ideal_rel / np.log2(np.arange(2, n + 2))))
    ndcg = dcg / idcg if idcg > 0 else 0.0

    # --- MAP (Mean Average Precision) ---
    # Treat "relevant" as actual rank <= n/2 (top half). Average
    # precision = mean of precision@k over relevant items in the
    # predicted ordering.
    relevant_threshold = max(1, n // 2)
    is_relevant = np.array(
        [1 if rank_map[nid] <= relevant_threshold else 0 for nid in predictions],
        dtype=np.float64,
    )
    n_relevant = float(np.sum(is_relevant))
    if n_relevant > 0:
        # Precision@k for each position.
        cumsum = np.cumsum(is_relevant)
        positions = np.arange(1, n + 1, dtype=np.float64)
        precision_at_k = cumsum / positions
        # Average precision = mean of precision_at_k over relevant items.
        ap = float(np.sum(precision_at_k * is_relevant) / n_relevant)
    else:
        ap = 0.0

    # --- Kendall's tau ---
    # Kendall's tau between the predicted ordering and the ideal
    # (actual-rank) ordering.
    predicted_ranks = np.array(
        [rank_map[nid] for nid in predictions], dtype=np.float64
    )
    # The ideal ordering is the actual ranks in ascending order.
    ideal_ordering = np.sort(predicted_ranks)
    kendall_tau = _kendall_tau(predicted_ranks, ideal_ordering)

    return {
        "ndcg": float(ndcg),
        "map": float(ap),
        "kendall_tau": float(kendall_tau),
    }


def _kendall_tau(a: Any, b: Any) -> float:
    """Compute Kendall's tau-b between two 1-D arrays.

    Uses scipy when available, otherwise falls back to a pure-numpy
    O(n^2) implementation.

    Args:
        a: 1-D array.
        b: 1-D array of the same length.

    Returns:
        Kendall's tau-b correlation in [-1, 1].
    """
    import numpy as np  # noqa: WPS433 lazy import

    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    n = a.shape[0]
    if n < 2:
        return 0.0

    try:
        from scipy.stats import kendalltau  # noqa: WPS433 lazy import

        tau, _p = kendalltau(a, b)
        if tau is None or np.isnan(tau):
            return 0.0
        return float(tau)
    except Exception:
        # Pure-numpy fallback: count concordant / discordant pairs.
        concordant = 0
        discordant = 0
        for i in range(n):
            for j in range(i + 1, n):
                da = a[i] - a[j]
                db = b[i] - b[j]
                prod = da * db
                if prod > 0:
                    concordant += 1
                elif prod < 0:
                    discordant += 1
        total = concordant + discordant
        if total == 0:
            return 0.0
        return float((concordant - discordant) / total)


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: GraphRankerResult,
    manual_override: bool = False,
) -> bool:
    """Validate whether a graph ranker result is promotion eligible.

    Promotion rules (fail-closed):

    - If ``result.is_shadow`` is ``True`` (the default for the graph
      ranker), the run is **only** eligible when ``manual_override`` is
      ``True`` — i.e. an operator explicitly overrides the shadow gate.
      A shadow run with no override is **not** eligible.
    - If ``result.is_shadow`` is ``False`` (a non-shadow run), the run is
      eligible regardless of ``manual_override``.

    Args:
        result: The :class:`GraphRankerResult` to validate.
        manual_override: When ``True``, override the shadow gate and
            mark a shadow run as eligible.

    Returns:
        ``True`` if the result is promotion eligible, ``False``
        otherwise.
    """
    if result.is_shadow and not manual_override:
        return False
    if manual_override:
        return True
    # Not shadow and no override -> eligible.
    return True


# ---------------------------------------------------------------------------
# Family registration helper
# ---------------------------------------------------------------------------


def register_graph_ranker_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for graph ranker registration.

    The returned dict carries the fields a
    :class:`~quant_foundry.alpha_genome.ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus
    graph-ranker-specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not**
    mutate the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks the graph ranker as a shadow family: it is **not** a
    baseline exception, does not require a GPU (the trainer degrades
    gracefully to CPU), and defaults to the ``CHALLENGER``
    promotion-eligibility class (though the trainer itself forces
    ``promotion_eligible=False`` when ``shadow_only=True``).
    """
    return {
        "family_id": "graph_ranker",
        "display_name": "Graph Ranker (GNN, shadow canary)",
        "version": "1",
        "dataset_shape": "graph_snapshot",
        "objectives": ("ranking",),
        "artifact_format": "torch_state_dict",
        "artifact_loader": "quant_foundry.graph_ranker.GraphRanker.load_artifact",
        "required_metrics": ("ndcg", "map", "kendall_tau", "final_loss"),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "challenger",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "shadow_only": True,
        "default_hidden_dim": 64,
        "default_n_layers": 2,
        "default_dropout": 0.1,
        "edge_types": ("sector", "industry", "correlation", "supply_chain"),
    }


__all__ = [
    "GraphRankerConfig",
    "GraphRankerResult",
    "GraphRankerModel",
    "GraphRanker",
    "compute_edge_attribution",
    "validate_edge_availability",
    "compute_graph_rank_metrics",
    "validate_promotion_eligibility",
    "register_graph_ranker_family",
]
