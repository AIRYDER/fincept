"""
quant_foundry.event_trainer — Event abnormal-return trainer (T-12.2).

This module provides a self-contained, importable event abnormal-return
trainer that encodes event text/tags into dense embeddings and predicts
abnormal returns over multiple horizons (e.g. 1d / 5d / 20d), with
shadow predictions broken out by event type and confidence bucket.

It builds on the tabular neural runtime
(:mod:`quant_foundry.tabular_neural_runtime`) for GPU telemetry, the
OOF artifact writer (:mod:`quant_foundry.oof_artifacts`) for ensemble
integration, and the event manifest
(:mod:`quant_foundry.event_manifest`) for source-hash provenance.

Capabilities:

- :class:`EventTrainerConfig` — frozen, ``extra='forbid'`` config for an
  event abnormal-return training run (architecture, optimizer,
  horizons, shadow-mode default).
- :class:`EventTrainingResult` — frozen, ``extra='forbid'`` result
  carrying epoch losses, GPU status, artifact paths, per-horizon and
  per-event-type metrics, and promotion eligibility.
- :class:`EventAbnormalReturnModel` — an ``nn.Module`` wrapper with a
  shared backbone (BatchNorm + Dropout + ReLU) and a separate output
  head per horizon (multi-horizon prediction).
- :class:`EventTrainer` — the train / predict / save / load / OOF-write
  façade used by the research dispatch path.
- :func:`compute_event_type_metrics` — per-event-type MSE / MAE.
- :func:`compute_confidence_bucket_metrics` — quantile-bucketed metrics.
- :func:`validate_promotion_eligibility` — fail-closed promotion gate:
  shadow runs are only eligible via an explicit manual override.
- :func:`register_event_family` — returns a
  :class:`~quant_foundry.alpha_genome.ModelFamilySpec`-compatible dict
  for event-family registration (does not mutate the registry itself).

Design notes (cross-cutting quant rigor, BIG_PLAN):

- **Shadow mode by default.** ``EventTrainerConfig.shadow_only`` defaults
  to ``True`` and ``EventTrainingResult.promotion_eligible`` is forced to
  ``False`` when shadow mode is on. Promotion requires an explicit
  manual override — there is no automatic path from shadow to
  production.
- **Fail-closed source hash.** A missing / empty event source hash is
  rejected before any training begins (``validate_source_hash``).
- **No live trading authority.** A shadow event run never produces
  tradeable predictions; its outputs are OOF predictions for ensemble
  integration and a model artifact for offline evaluation only.
- **No secrets.** Configs carry only architecture + optimizer
  hyperparameters, a device string, and a seed — never credentials or
  filesystem paths beyond the optional artifact path.
- **Cost fails closed.** Invalid configs are rejected at construction;
  training errors surface as exceptions rather than partial results.
- **Lazy torch import.** ``import torch`` happens inside methods, never
  at module top level, so this module is importable on hosts without
  torch (the Pydantic models and ``register_event_family`` can be
  constructed without torch installed).
- **File-disjoint.** New module; does not modify ``event_manifest.py``,
  ``event_text_runtime.py``, or ``alpha_genome.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.oof_artifacts import OOFWriter
from quant_foundry.tabular_neural_runtime import GPUStatus, check_gpu

# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


class EventTrainerConfig(BaseModel):
    """Configuration for an event abnormal-return training run.

    Frozen + ``extra='forbid'`` for audit integrity. Defaults are
    shadow-oriented: ``shadow_only=True``, a modest MLP backbone, and
    the canonical 1d / 5d / 20d horizons so a run completes quickly on
    CPU for smoke tests while still exercising the full multi-horizon
    code path.

    Attributes:
        embedding_dim: Dimensionality of the input event embedding
            (must be >= 1).
        hidden_dims: Widths of the shared backbone's hidden layers.
            Defaults to ``[128, 64]``.
        horizons: Prediction horizons in days (must be non-empty with
            each >= 1). Defaults to ``[1, 5, 20]``.
        learning_rate: Adam learning rate (must be > 0).
        epochs: Number of training epochs.
        batch_size: Mini-batch size.
        dropout: Dropout probability (must be in [0, 1)).
        device: Device to run on — ``auto``, ``cpu``, or ``cuda``.
        seed: Random seed for reproducibility.
        shadow_only: When ``True`` (default) the run is marked shadow
            and ``promotion_eligible`` is forced to ``False``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    embedding_dim: int = 384
    hidden_dims: list[int] = Field(default_factory=lambda: [128, 64])
    horizons: list[int] = Field(default_factory=lambda: [1, 5, 20])
    learning_rate: float = 0.001
    epochs: int = 10
    batch_size: int = 32
    dropout: float = 0.1
    device: str = "auto"
    seed: int = 42
    shadow_only: bool = True

    @field_validator("embedding_dim")
    @classmethod
    def _embedding_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"embedding_dim must be >= 1; got {v}")
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

    @field_validator("learning_rate")
    @classmethod
    def _learning_rate_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"learning_rate must be > 0; got {v}")
        return v

    @field_validator("dropout")
    @classmethod
    def _dropout_range(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError(f"dropout must be in [0, 1); got {v}")
        return v

    @field_validator("device")
    @classmethod
    def _device_allowed(cls, v: str) -> str:
        allowed = {"auto", "cpu", "cuda"}
        if v not in allowed:
            raise ValueError(f"device must be one of {sorted(allowed)}; got {v!r}")
        return v

    @model_validator(mode="after")
    def _hidden_dims_nonempty(self) -> EventTrainerConfig:
        """hidden_dims must be a non-empty list of positive ints."""
        if not self.hidden_dims:
            raise ValueError("hidden_dims must be non-empty")
        for i, h in enumerate(self.hidden_dims):
            if h < 1:
                raise ValueError(f"hidden_dims[{i}] must be >= 1; got {h}")
        return self

    @model_validator(mode="after")
    def _horizons_valid(self) -> EventTrainerConfig:
        """horizons must be non-empty with each horizon >= 1."""
        if not self.horizons:
            raise ValueError("horizons must be non-empty")
        for i, hz in enumerate(self.horizons):
            if hz < 1:
                raise ValueError(f"horizons[{i}] must be >= 1; got {hz}")
        return self


class EventTrainingResult(BaseModel):
    """Result of an event abnormal-return training run.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the config
    used, the event source hash (fail-closed when missing), the per-epoch
    losses, the GPU status at training time, the paths to the saved
    model / OOF artifacts (if any), the shadow flag, the
    promotion-eligibility flag, per-horizon metrics, per-event-type
    metrics, and the wall-clock training duration.

    Attributes:
        config: The :class:`EventTrainerConfig` used for the run.
        source_hash: The event source hash (required, fail-closed if
            missing).
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
        metrics: Per-horizon metrics (e.g.
            ``{"h1_mse": ..., "h5_mse": ...}``).
        event_type_metrics: Metrics broken out by event type (e.g.
            ``{"earnings": {"h1_mse": ...}, "guidance": {...}}``).
        duration_seconds: Wall-clock training duration in seconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: EventTrainerConfig
    source_hash: str
    final_loss: float
    epoch_losses: list[float] = Field(default_factory=list)
    gpu_status: GPUStatus
    artifact_path: str | None = None
    oof_artifact_path: str | None = None
    is_shadow: bool
    promotion_eligible: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    event_type_metrics: dict[str, dict[str, float]] = Field(default_factory=dict)
    duration_seconds: float

    @field_validator("source_hash")
    @classmethod
    def _source_hash_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("missing event source hash")
        return v


# ---------------------------------------------------------------------------
# Event abnormal-return model
# ---------------------------------------------------------------------------


class EventAbnormalReturnModel:
    """Multi-horizon event abnormal-return model.

    Architecture:

    - A **shared backbone** of ``hidden_dims`` layers (each followed by
      BatchNorm1d + Dropout + ReLU) maps the event embedding to a shared
      representation.
    - A **separate output head** per horizon projects the shared
      representation to a single scalar abnormal-return prediction for
      that horizon.
    - The forward pass returns a tensor of shape
      ``(batch, n_horizons)`` — one abnormal-return prediction per
      horizon per row.

    This is a thin wrapper around ``torch.nn.Module`` (built lazily so
    the module remains importable without torch), mirroring the pattern
    in :class:`~quant_foundry.tabular_neural_runtime.TinyTabularNet`.
    """

    def __init__(
        self,
        embedding_dim: int,
        hidden_dims: list[int],
        horizons: list[int],
        dropout: float = 0.1,
    ) -> None:
        if embedding_dim <= 0:
            raise ValueError("embedding_dim must be positive")
        if not hidden_dims:
            raise ValueError("hidden_dims must be non-empty")
        if any(h <= 0 for h in hidden_dims):
            raise ValueError("all hidden_dims must be positive")
        if not horizons:
            raise ValueError("horizons must be non-empty")
        if any(hz < 1 for hz in horizons):
            raise ValueError("all horizons must be >= 1")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.embedding_dim = embedding_dim
        self.hidden_dims = list(hidden_dims)
        self.horizons = list(horizons)
        self.dropout = dropout
        self._module: Any = None

    def _build_module(self) -> Any:
        """Build and return the underlying ``torch.nn.Module``.

        Lazily imports torch. The built module is cached on
        ``self._module`` so repeated calls return the same instance.
        """
        if self._module is not None:
            return self._module

        import torch.nn as nn

        # Shared backbone: embedding_dim -> hidden_dims[-1].
        backbone_layers: list[Any] = []
        prev = self.embedding_dim
        for h in self.hidden_dims:
            backbone_layers.append(nn.Linear(prev, h))
            backbone_layers.append(nn.BatchNorm1d(h))
            backbone_layers.append(nn.Dropout(self.dropout))
            backbone_layers.append(nn.ReLU())
            prev = h
        backbone = nn.Sequential(*backbone_layers)

        # Separate output head per horizon.
        heads = nn.ModuleList()
        shared_repr_dim = self.hidden_dims[-1]
        for _ in self.horizons:
            heads.append(nn.Linear(shared_repr_dim, 1))

        net = _make_event_module_class()(backbone=backbone, heads=heads)
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(self, x: Any) -> Any:
        """Run a forward pass.

        Returns a tensor of shape ``(batch, n_horizons)`` — one
        abnormal-return prediction per horizon per row.
        """
        return self.module(x)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return self.module.state_dict()

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> EventAbnormalReturnModel:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> EventAbnormalReturnModel:
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> EventAbnormalReturnModel:
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


def _make_event_module_class() -> Any:
    """Build and return the real ``nn.Module`` subclass for the event model.

    Lazily imports torch. Called from
    :meth:`EventAbnormalReturnModel._build_module` each time a new model
    is built (the class is cheap to construct and keeps the torch import
    boundary local to the build call).
    """
    import torch
    import torch.nn as nn

    class _EventAbnormalReturnNet(nn.Module):
        """Inner nn.Module implementing the multi-horizon forward pass."""

        def __init__(
            self,
            backbone: nn.Module,
            heads: nn.ModuleList,
        ) -> None:
            super().__init__()
            self.backbone = backbone
            self.heads = heads

        def forward(self, x: Any) -> Any:
            shared = self.backbone(x)
            # Stack the per-horizon head outputs -> (batch, n_horizons).
            outs = [head(shared) for head in self.heads]
            return torch.cat(outs, dim=-1)

    return _EventAbnormalReturnNet


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def _resolve_device(config: EventTrainerConfig, gpu_status: GPUStatus) -> Any:
    """Resolve the torch device for an event run.

    ``auto`` picks CUDA when available, else CPU. ``cpu`` / ``cuda`` are
    honored literally (``cuda`` on a CPU-only host falls back to CPU).
    """
    import torch

    if config.device == "cpu":
        return torch.device("cpu")
    if config.device == "cuda":
        if gpu_status.available:
            return torch.device("cuda")
        return torch.device("cpu")
    # auto
    return torch.device("cuda" if gpu_status.available else "cpu")


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------


def compute_event_type_metrics(
    predictions: list[list[float]],
    actuals: list[list[float]],
    event_types: list[str],
    horizons: list[int],
) -> dict[str, dict[str, float]]:
    """Compute per-event-type MSE / MAE per horizon.

    Groups predictions by event type and computes, for each event type
    and each horizon, the mean squared error and mean absolute error of
    the predictions against the actuals.

    Args:
        predictions: Per-row multi-horizon predictions
            (``predictions[i]`` is a list of length ``len(horizons)``).
        actuals: Per-row multi-horizon actuals (same shape as
            ``predictions``).
        event_types: Per-row event type label.
        horizons: The horizons (in days) corresponding to the columns of
            ``predictions`` / ``actuals``.

    Returns:
        A nested dict ``{event_type: {metric_name: value}}`` where
        ``metric_name`` is ``h<horizon>_mse`` or ``h<horizon>_mae``.
    """
    import numpy as np

    n = len(predictions)
    if not (len(actuals) == n and len(event_types) == n):
        raise ValueError("predictions, actuals, and event_types must have the same length")
    n_horizons = len(horizons)
    if n_horizons == 0:
        raise ValueError("horizons must be non-empty")

    # Group row indices by event type.
    groups: dict[str, list[int]] = {}
    for i, et in enumerate(event_types):
        groups.setdefault(str(et), []).append(i)

    result: dict[str, dict[str, float]] = {}
    for et, idxs in groups.items():
        metrics: dict[str, float] = {}
        for h_idx, hz in enumerate(horizons):
            preds_h = np.array(
                [float(predictions[i][h_idx]) for i in idxs],
                dtype=float,
            )
            acts_h = np.array(
                [float(actuals[i][h_idx]) for i in idxs],
                dtype=float,
            )
            if preds_h.size == 0:
                metrics[f"h{hz}_mse"] = float("nan")
                metrics[f"h{hz}_mae"] = float("nan")
            else:
                diff = preds_h - acts_h
                metrics[f"h{hz}_mse"] = float(np.mean(diff**2))
                metrics[f"h{hz}_mae"] = float(np.mean(np.abs(diff)))
        result[et] = metrics
    return result


def compute_confidence_bucket_metrics(
    predictions: list[list[float]],
    actuals: list[list[float]],
    confidences: list[float],
    n_buckets: int = 5,
) -> dict[str, dict[str, float]]:
    """Compute metrics bucketed by prediction confidence.

    Buckets predictions by confidence (quantile-based) and computes, for
    each bucket, the mean squared error and mean absolute error across
    all horizons, plus the bucket's mean confidence and count.

    Args:
        predictions: Per-row multi-horizon predictions.
        actuals: Per-row multi-horizon actuals (same shape as
            ``predictions``).
        confidences: Per-row confidence score (higher = more
            confident).
        n_buckets: Number of quantile buckets (must be >= 1).

    Returns:
        A nested dict ``{bucket_name: {metric_name: value}}`` where
        ``bucket_name`` is ``bucket_0`` ... ``bucket_{n_buckets-1}`` and
        ``metric_name`` is one of ``mse``, ``mae``, ``mean_confidence``,
        ``count``.
    """
    import numpy as np

    n = len(predictions)
    if not (len(actuals) == n and len(confidences) == n):
        raise ValueError("predictions, actuals, and confidences must have the same length")
    if n_buckets < 1:
        raise ValueError("n_buckets must be >= 1")

    confs = np.array(confidences, dtype=float)
    # Quantile-based bucket assignment.
    if n == 0:
        return {
            f"bucket_{b}": {
                "mse": float("nan"),
                "mae": float("nan"),
                "mean_confidence": float("nan"),
                "count": 0,
            }
            for b in range(n_buckets)
        }

    # Compute quantile edges. When n < n_buckets, some buckets will be
    # empty; np.quantile still returns valid edges.
    quantiles = np.linspace(0.0, 1.0, n_buckets + 1)
    edges = np.quantile(confs, quantiles)
    # Ensure strictly-increasing edges (collapse duplicates).
    edges = np.unique(edges)
    # np.unique may reduce the number of edges; assign via digitize which
    # handles this gracefully.
    bucket_idx = np.digitize(confs, edges[1:-1]) if len(edges) > 2 else np.zeros(n, dtype=int)

    result: dict[str, dict[str, float]] = {}
    for b in range(n_buckets):
        mask = bucket_idx == b
        count = int(np.sum(mask))
        metrics: dict[str, float] = {
            "count": count,
            "mean_confidence": float(np.mean(confs[mask])) if count else float("nan"),
        }
        if count == 0:
            metrics["mse"] = float("nan")
            metrics["mae"] = float("nan")
        else:
            preds_b = np.array(
                [predictions[i] for i in range(n) if mask[i]],
                dtype=float,
            )
            acts_b = np.array(
                [actuals[i] for i in range(n) if mask[i]],
                dtype=float,
            )
            diff = preds_b - acts_b
            metrics["mse"] = float(np.mean(diff**2))
            metrics["mae"] = float(np.mean(np.abs(diff)))
        result[f"bucket_{b}"] = metrics
    return result


# ---------------------------------------------------------------------------
# EventTrainer
# ---------------------------------------------------------------------------


class EventTrainer:
    """Train / predict / save / load / OOF-write façade for event models.

    The trainer builds an :class:`EventAbnormalReturnModel`, trains it
    with Adam on the provided event embeddings / multi-horizon labels,
    records per-horizon and per-event-type metrics, saves the trained
    state_dict, and can write OOF predictions via :class:`OOFWriter`.

    Args:
        config: The :class:`EventTrainerConfig` for the run.
        source_hash: The event source hash (fail-closed when missing /
            empty).
    """

    def __init__(
        self,
        config: EventTrainerConfig,
        source_hash: str,
    ) -> None:
        if not isinstance(config, EventTrainerConfig):
            raise TypeError("config must be an EventTrainerConfig")
        self.config = config
        self.source_hash = source_hash
        self.model_: EventAbnormalReturnModel | None = None

    # -- source-hash validation -------------------------------------------

    def validate_source_hash(self) -> None:
        """Validate the event source hash (fail-closed).

        Raises:
            ValueError: if ``source_hash`` is empty or ``None``.
        """
        if not isinstance(self.source_hash, str) or not self.source_hash.strip():
            raise ValueError("missing event source hash")

    # -- training ---------------------------------------------------------

    def train(
        self,
        embeddings: Any,
        labels: Any,
        event_types: list[str],
        weights: Any = None,
    ) -> EventTrainingResult:
        """Train an event abnormal-return model and return the result.

        Args:
            embeddings: Event embeddings — a numpy array or list of
                lists of shape ``(n_events, embedding_dim)``.
            labels: Multi-horizon abnormal-return labels — a numpy
                array or list of lists of shape
                ``(n_events, n_horizons)``.
            event_types: Per-row event type label (length
                ``n_events``).
            weights: Optional per-row sample weights (length
                ``n_events``). When provided, the per-batch loss is
                weighted by them.

        Returns:
            An :class:`EventTrainingResult` with epoch losses, GPU
            status, per-horizon and per-event-type metrics, and
            promotion eligibility (``False`` when ``shadow_only``).
        """
        import numpy as np
        import torch
        import torch.nn as nn

        # Fail-closed source hash validation.
        self.validate_source_hash()

        start = time.perf_counter()

        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)

        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)

        emb_arr = np.array(embeddings, dtype=float)
        if emb_arr.ndim != 2:
            raise ValueError(
                f"embeddings must be 2-D (n_events, embedding_dim); got shape {emb_arr.shape}"
            )
        if emb_arr.shape[1] != self.config.embedding_dim:
            raise ValueError(
                f"embeddings second dimension must equal embedding_dim="
                f"{self.config.embedding_dim}; got {emb_arr.shape[1]}"
            )

        n_horizons = len(self.config.horizons)
        label_arr = np.array(labels, dtype=float)
        if label_arr.ndim != 2:
            raise ValueError(
                f"labels must be 2-D (n_events, n_horizons); got shape {label_arr.shape}"
            )
        if label_arr.shape[1] != n_horizons:
            raise ValueError(
                f"labels second dimension must equal n_horizons="
                f"{n_horizons}; got {label_arr.shape[1]}"
            )
        if label_arr.shape[0] != emb_arr.shape[0]:
            raise ValueError(
                f"embeddings and labels must have the same number of "
                f"rows; got {emb_arr.shape[0]} vs {label_arr.shape[0]}"
            )

        n_samples = emb_arr.shape[0]
        if len(event_types) != n_samples:
            raise ValueError(
                f"event_types must have length n_events={n_samples}; got {len(event_types)}"
            )

        w_arr = (
            np.array(weights, dtype=float)
            if weights is not None
            else np.ones(n_samples, dtype=float)
        )
        if w_arr.shape[0] != n_samples:
            raise ValueError(f"weights must have length n_events={n_samples}; got {w_arr.shape[0]}")

        x_tensor = torch.from_numpy(emb_arr).float()
        y_tensor = torch.from_numpy(label_arr).float()
        w_tensor = torch.from_numpy(w_arr).float()

        model = EventAbnormalReturnModel(
            embedding_dim=self.config.embedding_dim,
            hidden_dims=list(self.config.hidden_dims),
            horizons=list(self.config.horizons),
            dropout=self.config.dropout,
        )
        model.to(device)
        model.train()

        nn.MSELoss()
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=self.config.learning_rate,
        )

        epoch_losses: list[float] = []
        final_loss = float("nan")

        if self.config.epochs > 0 and n_samples > 0:
            for _epoch in range(self.config.epochs):
                permutation = torch.randperm(n_samples)
                batch_losses: list[float] = []
                for i in range(0, n_samples, self.config.batch_size):
                    idx = permutation[i : i + self.config.batch_size]
                    batch_x = x_tensor[idx].to(device)
                    batch_y = y_tensor[idx].to(device)
                    batch_w = w_tensor[idx].to(device)

                    # BatchNorm1d requires more than 1 sample per batch.
                    if batch_x.shape[0] < 2:
                        continue

                    optimizer.zero_grad()
                    preds = model.forward(batch_x)
                    # preds: (batch, n_horizons)
                    # Per-sample, per-horizon MSE then weighted mean.
                    per_elem = (preds - batch_y) ** 2
                    bw = batch_w.unsqueeze(-1).expand_as(per_elem)
                    loss = (per_elem * bw).sum() / bw.sum().clamp(min=1e-8)
                    loss.backward()
                    optimizer.step()
                    batch_losses.append(float(loss.item()))

                if batch_losses:
                    epoch_loss = float(sum(batch_losses) / len(batch_losses))
                else:
                    epoch_loss = float("nan")
                epoch_losses.append(epoch_loss)

            if epoch_losses:
                final_loss = epoch_losses[-1]

        self.model_ = model

        # Compute per-horizon metrics on the training set (eval mode).
        metrics: dict[str, float] = {}
        event_type_metrics: dict[str, dict[str, float]] = {}
        if n_samples > 0 and self.config.epochs > 0:
            model.eval()
            with torch.no_grad():
                preds = model.forward(x_tensor.to(device))
                preds_np = preds.cpu().numpy()
            for h_idx, hz in enumerate(self.config.horizons):
                diff = preds_np[:, h_idx] - label_arr[:, h_idx]
                metrics[f"h{hz}_mse"] = float(np.mean(diff**2))
                metrics[f"h{hz}_mae"] = float(np.mean(np.abs(diff)))
            metrics["final_loss"] = final_loss

            # Per-event-type metrics.
            preds_list = preds_np.tolist()
            actuals_list = label_arr.tolist()
            event_type_metrics = compute_event_type_metrics(
                predictions=preds_list,
                actuals=actuals_list,
                event_types=event_types,
                horizons=list(self.config.horizons),
            )

        duration = time.perf_counter() - start

        return EventTrainingResult(
            config=self.config,
            source_hash=self.source_hash,
            final_loss=final_loss,
            epoch_losses=epoch_losses,
            gpu_status=gpu_status,
            artifact_path=None,
            oof_artifact_path=None,
            is_shadow=self.config.shadow_only,
            promotion_eligible=not self.config.shadow_only,
            metrics=metrics,
            event_type_metrics=event_type_metrics,
            duration_seconds=duration,
        )

    # -- prediction -------------------------------------------------------

    def predict(self, embeddings: Any) -> list[list[float]]:
        """Predict multi-horizon abnormal returns for ``embeddings``.

        Loads the trained model (or uses the in-memory model if
        available), runs a forward pass, and returns a list of
        per-row multi-horizon predictions.

        Args:
            embeddings: Event embeddings — a numpy array or list of
                lists of shape ``(n_events, embedding_dim)``.

        Returns:
            A list of lists of floats — ``predictions[i]`` is a list of
            length ``len(horizons)``.
        """
        import numpy as np
        import torch

        if self.model_ is None:
            raise ValueError("no trained model available — call train() or load_artifact() first")

        emb_arr = np.array(embeddings, dtype=float)
        if emb_arr.ndim != 2:
            raise ValueError(
                f"embeddings must be 2-D (n_events, embedding_dim); got shape {emb_arr.shape}"
            )
        if emb_arr.shape[1] != self.config.embedding_dim:
            raise ValueError(
                f"embeddings second dimension must equal embedding_dim="
                f"{self.config.embedding_dim}; got {emb_arr.shape[1]}"
            )

        x_tensor = torch.from_numpy(emb_arr).float()

        model = self.model_
        model.eval()
        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)
        model.to(device)

        with torch.no_grad():
            preds = model.forward(x_tensor.to(device))
            preds_np = preds.cpu().numpy()

        return preds_np.tolist()

    # -- artifact persistence ---------------------------------------------

    def save_artifact(self, path: str) -> None:
        """Save the trained model's state_dict to ``path``.

        Creates parent directories as needed. The file is written via
        ``torch.save`` (state_dict only — no pickled module).

        Raises:
            ValueError: if no model has been trained.
        """
        import torch

        if self.model_ is None:
            raise ValueError("no trained model to save — call train() first")
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        torch.save(self.model_.state_dict(), str(p))

    def load_artifact(self, path: str) -> EventAbnormalReturnModel:
        """Load a state_dict from ``path`` into a new model.

        The new model is built from the trainer's config and the saved
        state_dict is loaded into it. The model is set to eval mode on
        CPU and stored on the trainer (``self.model_``).

        Returns:
            The loaded :class:`EventAbnormalReturnModel`.
        """
        import torch

        model = EventAbnormalReturnModel(
            embedding_dim=self.config.embedding_dim,
            hidden_dims=list(self.config.hidden_dims),
            horizons=list(self.config.horizons),
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
        labels: list[list[float]],
        horizons: list[int],
        weights: list[float] | None,
        output_path: str,
    ) -> str:
        """Write OOF predictions for ensemble integration.

        Uses :class:`OOFWriter` from :mod:`quant_foundry.oof_artifacts`
        to write the predictions in the standard OOF artifact schema so
        they can be merged with other model families' OOF predictions
        for stacking. One OOF row is written per (row, horizon) pair.

        Args:
            fold_predictions: Per-row multi-horizon predictions
                (``fold_predictions[i]`` is a list of length
                ``len(horizons)``).
            fold_ids: Per-row fold ids.
            symbols: Per-row instrument symbols.
            timestamps: Per-row ISO-format timestamps.
            labels: Per-row multi-horizon ground-truth labels (same
                shape as ``fold_predictions``).
            horizons: The horizons (in days) corresponding to the
                columns of ``fold_predictions`` / ``labels``.
            weights: Per-row sample weights. When ``None``, 1.0 is
                used for every row.
            output_path: Directory to write the OOF artifact into. The
                file is named ``oof_event.json`` inside this directory.

        Returns:
            The path to the written OOF artifact file.
        """
        n = len(fold_predictions)
        if not (
            len(fold_ids) == n and len(symbols) == n and len(timestamps) == n and len(labels) == n
        ):
            raise ValueError(
                "fold_predictions, fold_ids, symbols, timestamps, "
                "and labels must all have the same length"
            )
        if weights is not None and len(weights) != n:
            raise ValueError("weights must have the same length as fold_predictions or be None")
        n_horizons = len(horizons)
        if n_horizons == 0:
            raise ValueError("horizons must be non-empty")
        for i in range(n):
            if len(fold_predictions[i]) != n_horizons:
                raise ValueError(f"fold_predictions[{i}] must have length n_horizons={n_horizons}")
            if len(labels[i]) != n_horizons:
                raise ValueError(f"labels[{i}] must have length n_horizons={n_horizons}")

        output_dir = str(Path(output_path).parent)
        writer = OOFWriter(model_family="event", output_dir=output_dir)
        for i in range(n):
            w = float(weights[i]) if weights is not None else 1.0
            for h_idx, hz in enumerate(horizons):
                row_id = f"{symbols[i]}_{timestamps[i]}_{hz}"
                writer.add_prediction(
                    row_id=row_id,
                    fold_id=int(fold_ids[i]),
                    symbol=str(symbols[i]),
                    timestamp=str(timestamps[i]),
                    label=float(labels[i][h_idx]),
                    prediction=float(fold_predictions[i][h_idx]),
                    horizon=int(hz),
                    weight=w,
                )
        artifact = writer.flush()
        return artifact.artifact_uri


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: EventTrainingResult,
    manual_override: bool = False,
) -> bool:
    """Validate whether an event training result is promotion eligible.

    Promotion rules (fail-closed):

    - If ``result.is_shadow`` is ``True`` (the default for event runs),
      the run is **only** eligible when ``manual_override`` is ``True``.
      A shadow run with no manual override is **not** eligible.
    - If ``result.is_shadow`` is ``False`` (a non-shadow run), the run
      is eligible regardless of ``manual_override``.

    Args:
        result: The :class:`EventTrainingResult` to validate.
        manual_override: When ``True``, allow a shadow run to be
            promoted (explicit operator override). Has no effect on
            non-shadow runs.

    Returns:
        ``True`` if the result is promotion eligible, ``False``
        otherwise.
    """
    if result.is_shadow:
        return bool(manual_override)
    return True


# ---------------------------------------------------------------------------
# Family registration helper
# ---------------------------------------------------------------------------


def register_event_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for event registration.

    The returned dict carries the fields a
    :class:`~quant_foundry.alpha_genome.ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus
    event-specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not**
    mutate the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks the event family as a shadow family: it is **not** a
    baseline exception, does not require a GPU (the trainer degrades
    gracefully to CPU), and defaults to the ``SHADOW`` promotion-
    eligibility class (though the trainer itself forces
    ``promotion_eligible=False`` when ``shadow_only=True``).
    """
    return {
        "family_id": "event",
        "display_name": "Event Abnormal-Return (shadow)",
        "version": "1",
        "dataset_shape": "event_embedding",
        "objectives": ("regression",),
        "artifact_format": "torch_state_dict",
        "artifact_loader": "quant_foundry.event_trainer.EventTrainer.load_artifact",
        "required_metrics": ("h1_mse", "h5_mse", "h20_mse", "final_loss"),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "shadow",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "shadow_only": True,
        "default_horizons": [1, 5, 20],
        "default_embedding_dim": 384,
    }


__all__ = [
    "EventAbnormalReturnModel",
    "EventTrainer",
    "EventTrainerConfig",
    "EventTrainingResult",
    "compute_confidence_bucket_metrics",
    "compute_event_type_metrics",
    "register_event_family",
    "validate_promotion_eligibility",
]
