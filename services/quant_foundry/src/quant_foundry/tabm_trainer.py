"""
quant_foundry.tabm_trainer — TabM research trainer (T-9.3).

TabM is a tabular deep-learning model that improves generalization by
training **k parallel prediction heads (blocks)** on top of a shared
backbone and averaging their outputs at inference time. The ensemble is
trained jointly in a single pass — each block sees the same input and
the same loss is averaged across blocks — so the cost is a single
training run rather than k independent models.

This module provides a self-contained, importable TabM research trainer
that builds on the :mod:`quant_foundry.tabular_neural_runtime` runtime
and integrates with the normalizer (:mod:`quant_foundry.normalizer`),
OOF artifact writer (:mod:`quant_foundry.oof_artifacts`), and model
family registry (:mod:`quant_foundry.alpha_genome`) subsystems.

Capabilities:

- :class:`TabMConfig` — frozen, ``extra='forbid'`` config for a TabM
  training run (architecture, optimizer, research-mode defaults).
- :class:`TabMTrainingResult` — frozen, ``extra='forbid'`` result
  carrying epoch losses, GPU status, artifact paths, and promotion
  eligibility.
- :class:`TabMModel` — a TabM-style ``nn.Module`` with a shared backbone
  and ``k`` parallel MLP blocks, each with its own output head.
- :class:`TabMTrainer` — the train / predict / save / load / OOF-write
  façade used by the research dispatch path.
- :func:`validate_promotion_eligibility` — fail-closed promotion gate:
  research runs are only eligible when they demonstrably improve the
  ensemble's OOF performance.
- :func:`register_tabm_family` — returns a
  :class:`~quant_foundry.alpha_genome.ModelFamilySpec`-compatible dict
  for TabM registration (does not mutate the registry itself).

Design notes (cross-cutting quant rigor, BIG_PLAN):

- **Research mode by default.** ``TabMConfig.research_mode`` defaults to
  ``True`` and ``TabMTrainingResult.promotion_eligible`` is forced to
  ``False`` when research mode is on. Promotion requires an explicit,
  measured OOF improvement over the incumbent ensemble — there is no
  automatic path from research to production.
- **No live trading authority.** A research TabM run never produces
  tradeable predictions; its outputs are OOF predictions for ensemble
  integration and a model artifact for offline evaluation only.
- **No secrets.** Configs carry only architecture + optimizer
  hyperparameters, a device string, and a seed — never credentials or
  filesystem paths beyond the optional artifact path.
- **Cost fails closed.** Invalid configs are rejected at construction;
  training errors surface as exceptions rather than partial results.
- **Lazy torch import.** ``import torch`` happens inside methods, never
  at module top level, so this module is importable on hosts without
  torch (the Pydantic models and ``register_tabm_family`` can be
  constructed without torch installed).
- **File-disjoint.** New module; does not modify ``real_trainer.py`` or
  ``alpha_genome.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.dataset_manifest import ColumnRoles
from quant_foundry.normalizer import (
    NormalizationMethod,
    Normalizer,
    NormalizerArtifact,
)
from quant_foundry.oof_artifacts import OOFWriter
from quant_foundry.tabular_neural_runtime import GPUStatus, check_gpu
from quant_foundry.training_manifest import ModelTaskSpec

# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


class TabMConfig(BaseModel):
    """Configuration for a TabM research training run.

    Frozen + ``extra='forbid'`` for audit integrity. Defaults are
    research-oriented: ``research_mode=True``, a modest ensemble size
    (``k=32``), and a small MLP backbone so a run completes quickly on
    CPU for smoke tests while still exercising the full TabM code path.

    Attributes:
        input_dim: Number of input features (must be >= 1).
        hidden_dims: Widths of the shared backbone's hidden layers.
            Defaults to ``[128, 64, 32]``.
        output_dim: Dimensionality of each block's output (1 for
            regression / binary logit).
        n_blocks: Number of parallel blocks (TabM uses multiple blocks).
            Alias for ``k``; both are kept for clarity — ``n_blocks`` is
            the architectural name, ``k`` is the TabM paper's name.
        k: Number of ensemble members in TabM (must be >= 1).
        learning_rate: Adam learning rate (must be > 0).
        epochs: Number of training epochs.
        batch_size: Mini-batch size.
        dropout: Dropout probability (must be in [0, 1)).
        weight_decay: Adam weight decay (L2 regularization).
        device: Device to run on — ``auto``, ``cpu``, or ``cuda``.
        seed: Random seed for reproducibility.
        research_mode: When ``True`` (default) the run is marked
            research and ``promotion_eligible`` is forced to ``False``.
        normalization_method: Normalization method name (one of
            ``standard``, ``robust``, ``minmax``, ``none``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_dim: int
    hidden_dims: list[int] = Field(default_factory=lambda: [128, 64, 32])
    output_dim: int = 1
    n_blocks: int = 5
    k: int = 32
    learning_rate: float = 0.001
    epochs: int = 100
    batch_size: int = 256
    dropout: float = 0.1
    weight_decay: float = 1e-5
    device: str = "auto"
    seed: int = 42
    research_mode: bool = True
    normalization_method: str = "standard"

    @field_validator("input_dim")
    @classmethod
    def _input_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"input_dim must be >= 1; got {v}")
        return v

    @field_validator("output_dim")
    @classmethod
    def _output_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"output_dim must be >= 1; got {v}")
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

    @field_validator("weight_decay")
    @classmethod
    def _weight_decay_nonnegative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"weight_decay must be >= 0; got {v}")
        return v

    @field_validator("device")
    @classmethod
    def _device_allowed(cls, v: str) -> str:
        allowed = {"auto", "cpu", "cuda"}
        if v not in allowed:
            raise ValueError(f"device must be one of {sorted(allowed)}; got {v!r}")
        return v

    @field_validator("normalization_method")
    @classmethod
    def _normalization_method_allowed(cls, v: str) -> str:
        allowed = {m.value for m in NormalizationMethod}
        if v not in allowed:
            raise ValueError(f"normalization_method must be one of {sorted(allowed)}; got {v!r}")
        return v

    @field_validator("k")
    @classmethod
    def _k_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"k must be >= 1; got {v}")
        return v

    @field_validator("n_blocks")
    @classmethod
    def _n_blocks_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_blocks must be >= 1; got {v}")
        return v

    @model_validator(mode="after")
    def _hidden_dims_nonempty(self) -> TabMConfig:
        """hidden_dims must be a non-empty list of positive ints."""
        if not self.hidden_dims:
            raise ValueError("hidden_dims must be non-empty")
        for i, h in enumerate(self.hidden_dims):
            if h < 1:
                raise ValueError(f"hidden_dims[{i}] must be >= 1; got {h}")
        return self


class TabMTrainingResult(BaseModel):
    """Result of a TabM research training run.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the config
    used, the per-epoch losses, the GPU status at training time, the
    paths to the saved model / OOF artifacts (if any), the normalizer
    artifact applied (if any), and the promotion-eligibility flag.

    Attributes:
        config: The :class:`TabMConfig` used for the run.
        final_loss: The mean loss of the final epoch (NaN if 0 epochs).
        epoch_losses: Mean loss per epoch (one entry per epoch).
        gpu_status: GPU availability + memory state at training time.
        artifact_path: Path to the saved model state_dict, or ``None``.
        normalizer_artifact: The normalizer artifact applied to the
            features, or ``None`` if no normalization was used.
        oof_artifact_path: Path to the OOF predictions artifact, or
            ``None`` if no OOF predictions were written.
        is_research: ``True`` when the run was in research mode.
        promotion_eligible: ``False`` when ``is_research`` is ``True``;
            only ``True`` for non-research runs (or research runs that
            later pass :func:`validate_promotion_eligibility` with a
            measured OOF improvement).
        metrics: Extra metrics (e.g. ``{"mse": ...}``).
        duration_seconds: Wall-clock training duration in seconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: TabMConfig
    final_loss: float
    epoch_losses: list[float] = Field(default_factory=list)
    gpu_status: GPUStatus
    artifact_path: str | None = None
    normalizer_artifact: NormalizerArtifact | None = None
    oof_artifact_path: str | None = None
    is_research: bool
    promotion_eligible: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    duration_seconds: float


# ---------------------------------------------------------------------------
# TabM model
# ---------------------------------------------------------------------------


class TabMModel:
    """A TabM-style ensemble MLP with a shared backbone and ``k`` heads.

    Architecture:

    - A **shared backbone** of ``hidden_dims`` layers (each followed by
      BatchNorm1d + Dropout + ReLU) maps the input to a shared
      representation.
    - ``k`` **parallel blocks** each project the shared representation
      through their own MLP (``n_blocks`` hidden layers, each with
      BatchNorm1d + Dropout + ReLU) to an ``output_dim`` output head.
    - The forward pass returns a tensor of shape ``(batch, k,
      output_dim)`` — the ensemble of ``k`` predictions per row.

    This is a thin wrapper around ``torch.nn.Module`` (built lazily so
    the module remains importable without torch), mirroring the pattern
    in :class:`~quant_foundry.tabular_neural_runtime.TinyTabularNet`.
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dims: list[int],
        output_dim: int = 1,
        n_blocks: int = 5,
        k: int = 32,
        dropout: float = 0.1,
    ) -> None:
        if input_dim <= 0:
            raise ValueError("input_dim must be positive")
        if output_dim <= 0:
            raise ValueError("output_dim must be positive")
        if not hidden_dims:
            raise ValueError("hidden_dims must be non-empty")
        if any(h <= 0 for h in hidden_dims):
            raise ValueError("all hidden_dims must be positive")
        if k < 1:
            raise ValueError("k must be >= 1")
        if n_blocks < 1:
            raise ValueError("n_blocks must be >= 1")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.input_dim = input_dim
        self.hidden_dims = list(hidden_dims)
        self.output_dim = output_dim
        self.n_blocks = n_blocks
        self.k = k
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

        # Shared backbone: input_dim -> hidden_dims[-1].
        backbone_layers: list[Any] = []
        prev = self.input_dim
        for h in self.hidden_dims:
            backbone_layers.append(nn.Linear(prev, h))
            backbone_layers.append(nn.BatchNorm1d(h))
            backbone_layers.append(nn.Dropout(self.dropout))
            backbone_layers.append(nn.ReLU())
            prev = h
        backbone = nn.Sequential(*backbone_layers)

        # k parallel blocks, each: shared_repr -> n_blocks hidden layers
        # -> output_dim head.
        blocks = nn.ModuleList()
        shared_repr_dim = self.hidden_dims[-1]
        for _ in range(self.k):
            block_layers: list[Any] = []
            bprev = shared_repr_dim
            for _j in range(self.n_blocks):
                block_layers.append(nn.Linear(bprev, shared_repr_dim))
                block_layers.append(nn.BatchNorm1d(shared_repr_dim))
                block_layers.append(nn.Dropout(self.dropout))
                block_layers.append(nn.ReLU())
                bprev = shared_repr_dim
            block_layers.append(nn.Linear(bprev, self.output_dim))
            blocks.append(nn.Sequential(*block_layers))

        net = _make_tabm_module_class()(backbone=backbone, blocks=blocks)
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(self, x: Any) -> Any:
        """Run a forward pass.

        Returns a tensor of shape ``(batch, k, output_dim)`` — the
        ensemble of ``k`` predictions per row.
        """
        return self.module(x)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return cast("dict[str, Any]", self.module.state_dict())

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> TabMModel:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> TabMModel:
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> TabMModel:
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


def _make_tabm_module_class() -> Any:
    """Build and return the real ``nn.Module`` subclass for TabM.

    Lazily imports torch. Called from
    :meth:`TabMModel._build_module` each time a new model is built (the
    class is cheap to construct and keeps the torch import boundary
    local to the build call).
    """
    import torch
    import torch.nn as nn

    class _TabMNet(nn.Module):  # type: ignore[misc]  # torch nn.Module is Any when torch not installed
        """Inner nn.Module implementing the TabM forward pass."""

        def __init__(self, backbone: nn.Module, blocks: nn.ModuleList) -> None:
            super().__init__()
            self.backbone = backbone
            self.blocks = blocks

        def forward(self, x: Any) -> Any:
            shared = self.backbone(x)
            # Stack the k block outputs -> (batch, k, output_dim).
            outs = [block(shared) for block in self.blocks]
            return torch.stack(outs, dim=1)

    return _TabMNet


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def _resolve_device(config: TabMConfig, gpu_status: GPUStatus) -> Any:
    """Resolve the torch device for a TabM run.

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
# TabMTrainer
# ---------------------------------------------------------------------------


class TabMTrainer:
    """Train / predict / save / load / OOF-write façade for TabM.

    The trainer builds a :class:`TabMModel`, trains it with Adam +
    weight decay on the provided features / labels, optionally applies a
    :class:`NormalizerArtifact` to the features, saves the trained
    state_dict, and can write OOF predictions via :class:`OOFWriter`.

    Args:
        config: The :class:`TabMConfig` for the run.
        column_roles: Optional :class:`ColumnRoles` declaring the
            feature / label / weight columns. Used for normalization
            and OOF writing when provided.
        task_spec: Optional :class:`ModelTaskSpec` declaring the
            learning task. Used for OOF writing when provided.
    """

    def __init__(
        self,
        config: TabMConfig,
        column_roles: ColumnRoles | None = None,
        task_spec: ModelTaskSpec | None = None,
    ) -> None:
        if not isinstance(config, TabMConfig):
            raise TypeError("config must be a TabMConfig")
        self.config = config
        self.column_roles = column_roles
        self.task_spec = task_spec
        self.model_: TabMModel | None = None
        self.normalizer_artifact_: NormalizerArtifact | None = None

    # -- normalization ----------------------------------------------------

    def _apply_normalization(
        self,
        X: Any,
        normalizer_artifact: NormalizerArtifact | None,
    ) -> Any:
        """Apply normalization to ``X`` using the provided artifact.

        If ``normalizer_artifact`` is ``None``, ``X`` is returned
        unchanged. Otherwise the artifact's per-column stats are applied
        to ``X`` (expected to be a pandas DataFrame when an artifact is
        provided). Returns a numpy array of the transformed features.
        """
        import numpy as np

        if normalizer_artifact is None:
            return np.asarray(X, dtype=float)

        # X is expected to be a DataFrame with named columns matching
        # the artifact's column names.
        stats_by_name = {c.column_name: c for c in normalizer_artifact.columns}
        # Determine the column order from the artifact.
        col_names = [c.column_name for c in normalizer_artifact.columns]
        from quant_foundry.normalizer import (
            apply_missing_policy,
            apply_normalization,
        )

        transformed_cols: list[Any] = []
        for col in col_names:
            if col not in X:
                raise ValueError(f"column {col!r} not found in X for normalization")
            stats = stats_by_name[col]
            arr = np.asarray(X[col], dtype=float)
            arr = apply_missing_policy(arr, stats)
            arr = apply_normalization(arr, stats)
            transformed_cols.append(arr)
        return np.column_stack(transformed_cols)

    def _fit_normalizer(
        self,
        X: Any,
        feature_columns: list[str],
    ) -> NormalizerArtifact:
        """Fit a normalizer on ``X`` (a DataFrame) and return the artifact."""
        method = NormalizationMethod(self.config.normalization_method)
        normalizer = Normalizer(method=method)
        return normalizer.fit(X, feature_columns)

    # -- training ---------------------------------------------------------

    def train(
        self,
        X: Any,
        y: Any,
        weights: Any = None,
        normalizer_artifact: NormalizerArtifact | None = None,
    ) -> TabMTrainingResult:
        """Train a TabM model on ``X`` / ``y`` and return the result.

        Args:
            X: Features — a numpy array, list of lists, or pandas
                DataFrame. When ``normalizer_artifact`` is provided,
                ``X`` must be a DataFrame with named columns.
            y: Labels — a 1-D array-like or a pandas Series.
            weights: Optional sample weights (1-D array-like). When
                provided, the per-batch loss is weighted by them.
            normalizer_artifact: Optional normalizer artifact to apply
                to ``X`` before training. When ``None`` and
                ``column_roles`` is set with feature columns, a new
                normalizer is fit on ``X`` and stored on the result.

        Returns:
            A :class:`TabMTrainingResult` with epoch losses, GPU status,
            and promotion eligibility.
        """
        import numpy as np
        import torch
        import torch.nn as nn

        start = time.perf_counter()

        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)

        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)

        # Normalization.
        used_artifact: NormalizerArtifact | None = normalizer_artifact
        if used_artifact is None and self.column_roles is not None:
            feature_cols = list(self.column_roles.feature_columns)
            if all(c in X for c in feature_cols):
                used_artifact = self._fit_normalizer(X, feature_cols)

        X_arr = self._apply_normalization(X, used_artifact)
        y_arr = np.array(y, dtype=float).reshape(-1, 1)
        w_arr = (
            np.array(weights, dtype=float)
            if weights is not None
            else np.ones(y_arr.shape[0], dtype=float)
        )

        n_samples = X_arr.shape[0]
        x_tensor = torch.from_numpy(X_arr).float()
        y_tensor = torch.from_numpy(y_arr).float()
        w_tensor = torch.from_numpy(w_arr).float()

        model = TabMModel(
            input_dim=self.config.input_dim,
            hidden_dims=list(self.config.hidden_dims),
            output_dim=self.config.output_dim,
            n_blocks=self.config.n_blocks,
            k=self.config.k,
            dropout=self.config.dropout,
        )
        model.to(device)
        model.train()

        nn.MSELoss()
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=self.config.learning_rate,
            weight_decay=self.config.weight_decay,
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
                    # preds: (batch, k, output_dim)
                    # Expand y to (batch, k, output_dim) for loss.
                    by = batch_y.unsqueeze(1).expand_as(preds)
                    # Per-sample, per-block MSE then weighted mean.
                    per_elem = (preds - by) ** 2
                    # Mean over output_dim, keep per-sample per-block.
                    per_elem = per_elem.mean(dim=-1)  # (batch, k)
                    # Weighted mean over k and batch.
                    bw = batch_w.unsqueeze(1).expand_as(per_elem)
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
        self.normalizer_artifact_ = used_artifact

        # Compute a simple MSE metric on the training set (eval mode).
        metrics: dict[str, float] = {}
        if n_samples > 0 and self.config.epochs > 0:
            model.eval()
            with torch.no_grad():
                preds = model.forward(x_tensor.to(device))
                # Average over k -> (batch, output_dim).
                avg_preds = preds.mean(dim=1).cpu().numpy()
                mse = float(np.mean((avg_preds - y_arr) ** 2))
                metrics["mse"] = mse
                metrics["final_loss"] = final_loss

        duration = time.perf_counter() - start

        return TabMTrainingResult(
            config=self.config,
            final_loss=final_loss,
            epoch_losses=epoch_losses,
            gpu_status=gpu_status,
            artifact_path=None,
            normalizer_artifact=used_artifact,
            oof_artifact_path=None,
            is_research=self.config.research_mode,
            promotion_eligible=not self.config.research_mode,
            metrics=metrics,
            duration_seconds=duration,
        )

    # -- prediction -------------------------------------------------------

    def predict(
        self,
        X: Any,
        normalizer_artifact: NormalizerArtifact | None = None,
    ) -> list[float]:
        """Predict ensemble-averaged outputs for ``X``.

        Loads the trained model (or uses the in-memory model if
        available), applies normalization, runs a forward pass, averages
        over the ``k`` blocks, and returns a 1-D list of predictions.

        Args:
            X: Features — same format as :meth:`train`.
            normalizer_artifact: Optional normalizer artifact to apply.
                When ``None``, the artifact stored on the trainer (from
                :meth:`train`) is used if available.

        Returns:
            A list of floats (one prediction per row).
        """
        import torch

        if self.model_ is None:
            raise ValueError("no trained model available — call train() or load_artifact() first")

        used_artifact = normalizer_artifact
        if used_artifact is None:
            used_artifact = self.normalizer_artifact_

        X_arr = self._apply_normalization(X, used_artifact)
        x_tensor = torch.from_numpy(X_arr).float()

        model = self.model_
        model.eval()
        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)
        model.to(device)

        with torch.no_grad():
            preds = model.forward(x_tensor.to(device))
            # Average over k -> (batch, output_dim).
            avg_preds = preds.mean(dim=1).cpu().numpy()

        return [float(v) for v in avg_preds.reshape(-1)]

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

    def load_artifact(self, path: str) -> TabMModel:
        """Load a state_dict from ``path`` into a new :class:`TabMModel`.

        The new model is built from the trainer's config and the saved
        state_dict is loaded into it. The model is set to eval mode on
        CPU and stored on the trainer (``self.model_``).

        Returns:
            The loaded :class:`TabMModel`.
        """
        import torch

        model = TabMModel(
            input_dim=self.config.input_dim,
            hidden_dims=list(self.config.hidden_dims),
            output_dim=self.config.output_dim,
            n_blocks=self.config.n_blocks,
            k=self.config.k,
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
            fold_predictions: Per-row predictions (ensemble-averaged).
                Must have the same length as ``fold_ids``.
            fold_ids: Per-row fold ids.
            symbols: Per-row instrument symbols.
            timestamps: Per-row ISO-format timestamps.
            labels: Per-row ground-truth labels.
            horizons: Per-row prediction horizons.
            weights: Per-row sample weights. When ``None``, 1.0 is
                used for every row.
            output_path: Directory to write the OOF artifact into. The
                file is named ``oof_tabm.json`` inside this directory.

        Returns:
            The path to the written OOF artifact file.
        """
        n = len(fold_predictions)
        if not (
            len(fold_ids) == n
            and len(symbols) == n
            and len(timestamps) == n
            and len(labels) == n
            and len(horizons) == n
        ):
            raise ValueError(
                "fold_predictions, fold_ids, symbols, timestamps, "
                "labels, and horizons must all have the same length"
            )
        if weights is not None and len(weights) != n:
            raise ValueError("weights must have the same length as fold_predictions or be None")

        output_dir = str(Path(output_path).parent)
        writer = OOFWriter(model_family="tabm", output_dir=output_dir)
        for i in range(n):
            row_id = f"{symbols[i]}_{timestamps[i]}_{horizons[i]}"
            w = float(weights[i]) if weights is not None else 1.0
            writer.add_prediction(
                row_id=row_id,
                fold_id=int(fold_ids[i]),
                symbol=str(symbols[i]),
                timestamp=str(timestamps[i]),
                label=float(labels[i]),
                prediction=float(cast("float", fold_predictions[i])),
                horizon=int(horizons[i]),
                weight=w,
            )
        artifact = writer.flush()
        return artifact.artifact_uri


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: TabMTrainingResult,
    ensemble_oof_improvement: float | None = None,
) -> bool:
    """Validate whether a TabM training result is promotion eligible.

    Promotion rules (fail-closed):

    - If ``result.is_research`` is ``True`` (the default for TabM), the
      run is **only** eligible when ``ensemble_oof_improvement`` is not
      ``None`` **and** strictly greater than 0 — i.e. the TabM model
      demonstrably improved the ensemble's OOF performance. A research
      run with no measured improvement, or a negative improvement, is
      **not** eligible.
    - If ``result.is_research`` is ``False`` (a non-research run), the
      run is eligible regardless of ``ensemble_oof_improvement``.

    Args:
        result: The :class:`TabMTrainingResult` to validate.
        ensemble_oof_improvement: The measured improvement in ensemble
            OOF performance attributable to this TabM model (e.g. the
            drop in ensemble OOF MSE when TabM is added). ``None`` when
            no OOF comparison has been performed.

    Returns:
        ``True`` if the result is promotion eligible, ``False``
        otherwise.
    """
    if result.is_research:
        if ensemble_oof_improvement is None:
            return False
        return ensemble_oof_improvement > 0
    return True


# ---------------------------------------------------------------------------
# Family registration helper
# ---------------------------------------------------------------------------


def register_tabm_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for TabM registration.

    The returned dict carries the fields a
    :class:`~quant_foundry.alpha_genome.ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus
    TabM-specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not**
    mutate the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks TabM as a research family: it is **not** a baseline
    exception, does not require a GPU (the trainer degrades gracefully
    to CPU), and defaults to the ``CHALLENGER`` promotion-eligibility
    class (though the trainer itself forces ``promotion_eligible=False``
    when ``research_mode=True``).
    """
    return {
        "family_id": "tabm",
        "display_name": "TabM (research)",
        "version": "1",
        "dataset_shape": "tabular_wide",
        "objectives": ("binary", "regression"),
        "artifact_format": "torch_state_dict",
        "artifact_loader": "quant_foundry.tabm_trainer.TabMTrainer.load_artifact",
        "required_metrics": ("mse", "mae", "final_loss"),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "challenger",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "research_mode": True,
        "default_k": 32,
        "default_n_blocks": 5,
    }


__all__ = [
    "TabMConfig",
    "TabMModel",
    "TabMTrainer",
    "TabMTrainingResult",
    "register_tabm_family",
    "validate_promotion_eligibility",
]
