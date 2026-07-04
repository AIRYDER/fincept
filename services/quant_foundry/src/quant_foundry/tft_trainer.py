"""quant_foundry.tft_trainer — Temporal Fusion Transformer trainer (T-10.4).

The Temporal Fusion Transformer (TFT) is a transformer-based architecture
designed for multi-horizon forecasting with heterogeneous inputs. Unlike
a generic sequence transformer, TFT explicitly separates covariates into
three categories:

- **Static** covariates (e.g. sector, industry) — do not change over time.
- **Known-future** covariates (e.g. calendar features, holidays) — known
  for the entire forecast horizon in advance.
- **Observed** covariates (e.g. price, volume) — known only up to the
  present (the encoder window).

This module provides a self-contained, importable TFT **canary** trainer
for the quant foundry's sequence-modeling GPU worker path. It builds on
the :mod:`quant_foundry.tabular_neural_runtime` runtime (for
:class:`GPUStatus` / :func:`check_gpu`) and integrates with the OOF
artifact writer (:mod:`quant_foundry.oof_artifacts`) and the model family
registry subsystem.

Capabilities:

- :class:`CovariateRole` — enum of the four covariate categories.
- :class:`CovariateRoles` — frozen, ``extra='forbid'`` declaration of
  which columns are static / known-future / observed / target. Required
  before training (fail-closed).
- :class:`TFTConfig` — frozen, ``extra='forbid'`` config for a TFT
  canary training run (architecture, optimizer, shadow-mode defaults).
- :class:`TFTTrainingResult` — frozen, ``extra='forbid'`` result
  carrying epoch losses, GPU status, artifact paths, multi-horizon
  predictions, and promotion eligibility.
- :class:`TFTModel` — a simplified TFT ``nn.Module`` with a Variable
  Selection Network (VSN), static covariate encoder, temporal
  self-attention, Gated Residual Network (GRN), and a multi-horizon
  output head.
- :class:`TFTTrainer` — the train / predict / save / load / OOF-write
  façade used by the research dispatch path.
- :func:`validate_promotion_eligibility` — fail-closed promotion gate:
  shadow runs are only eligible with an explicit manual override.
- :func:`register_tft_family` — returns a
  :class:`~quant_foundry.alpha_genome.ModelFamilySpec`-compatible dict for
  TFT registration (does not mutate the registry itself).

Design notes (cross-cutting quant rigor, BIG_PLAN):

- **Covariate roles are mandatory (fail-closed).** A TFT training run
  cannot start without an explicit, complete :class:`CovariateRoles`
  declaration. The trainer does not infer covariate categories loosely —
  every category (static, known-future, observed) must be declared and
  the target column must be specified.
- **Shadow mode by default.** ``TFTConfig.shadow_only`` defaults to
  ``True`` and ``TFTTrainingResult.promotion_eligible`` is forced to
  ``False`` when shadow mode is on. Promotion requires an explicit manual
  override — there is no automatic path from shadow to production.
- **No live trading authority.** A shadow TFT run never produces
  tradeable predictions; its outputs are OOF predictions for ensemble
  integration and a model artifact for offline evaluation only.
- **No secrets.** Configs carry only architecture + optimizer
  hyperparameters, a device string, and a seed — never credentials or
  filesystem paths beyond the optional artifact path.
- **Cost fails closed.** Invalid configs / covariate roles are rejected
  at construction; training errors surface as exceptions rather than
  partial results.
- **Lazy torch import.** ``import torch`` happens inside methods, never
  at module top level, so this module is importable on hosts without
  torch (the Pydantic models and ``register_tft_family`` can be
  constructed without torch installed).
- **File-disjoint.** New module; does not modify ``patchtst_trainer.py``,
  ``alpha_genome.py``, ``sequence_runtime.py``, or
  ``windowed_tensor_builder.py``.
"""

from __future__ import annotations

import enum
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quant_foundry.oof_artifacts import OOFWriter
from quant_foundry.tabular_neural_runtime import GPUStatus, check_gpu

# ---------------------------------------------------------------------------
# Covariate roles
# ---------------------------------------------------------------------------


class CovariateRole(enum.Enum):
    """Enumeration of TFT covariate categories.

    A TFT model distinguishes covariates by how they are known over time:

    Attributes:
        STATIC: Time-invariant covariates (e.g. sector, industry) that
            characterize an entity but do not change across timesteps.
        KNOWN_FUTURE: Covariates known in advance for the entire forecast
            horizon (e.g. calendar features, holidays).
        OBSERVED: Covariates known only up to the present (the encoder
            window) — e.g. price, volume.
        TARGET: The variable to forecast.
    """

    STATIC = "static"
    KNOWN_FUTURE = "known_future"
    OBSERVED = "observed"
    TARGET = "target"


class CovariateRoles(BaseModel):
    """Explicit declaration of TFT covariate roles (fail-closed).

    Frozen + ``extra='forbid'`` for audit integrity. A TFT training run
    requires a complete :class:`CovariateRoles` declaration before it can
    start — the trainer does not infer covariate categories loosely.

    Attributes:
        static_cols: Time-invariant covariates (e.g. sector, industry).
            Must be non-empty.
        known_future_cols: Covariates known in advance for the forecast
            horizon (e.g. calendar features, holidays). Must be
            non-empty.
        observed_cols: Covariates known only up to the present (e.g.
            price, volume). Must be non-empty.
        target_col: The variable to forecast.

    Validators:
        - No overlap between any two categories.
        - ``target_col`` must not appear in any other category.
        - ``static_cols``, ``known_future_cols``, and ``observed_cols``
          must each be non-empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    static_cols: list[str]
    known_future_cols: list[str]
    observed_cols: list[str]
    target_col: str

    @field_validator("static_cols", "known_future_cols", "observed_cols")
    @classmethod
    def _categories_nonempty(cls, v: list[str], info: Any) -> list[str]:
        if not v or len(v) == 0:
            raise ValueError(
                f"{info.field_name} must be non-empty — TFT requires "
                f"explicit declaration of all covariate categories"
            )
        for col in v:
            if not isinstance(col, str) or not col.strip():
                raise ValueError(
                    f"{info.field_name} contains an empty or whitespace-only column name"
                )
        return v

    @field_validator("target_col")
    @classmethod
    def _target_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("target_col must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _no_overlap_between_categories(self) -> CovariateRoles:
        """No column may appear in more than one category."""
        static_set = set(self.static_cols)
        known_future_set = set(self.known_future_cols)
        observed_set = set(self.observed_cols)

        overlap = static_set & known_future_set
        if overlap:
            raise ValueError(f"static_cols and known_future_cols overlap: {sorted(overlap)}")
        overlap = static_set & observed_set
        if overlap:
            raise ValueError(f"static_cols and observed_cols overlap: {sorted(overlap)}")
        overlap = known_future_set & observed_set
        if overlap:
            raise ValueError(f"known_future_cols and observed_cols overlap: {sorted(overlap)}")
        return self

    @model_validator(mode="after")
    def _target_not_in_other_categories(self) -> CovariateRoles:
        """target_col must not appear in any other category."""
        target = self.target_col
        if target in self.static_cols:
            raise ValueError(f"target_col {target!r} must not appear in static_cols")
        if target in self.known_future_cols:
            raise ValueError(f"target_col {target!r} must not appear in known_future_cols")
        if target in self.observed_cols:
            raise ValueError(f"target_col {target!r} must not appear in observed_cols")
        return self

    def all_feature_cols(self) -> list[str]:
        """Return all non-target columns in declaration order."""
        return list(self.static_cols) + list(self.known_future_cols) + list(self.observed_cols)

    def n_features(self) -> int:
        """Return the total number of non-target feature columns."""
        return len(self.static_cols) + len(self.known_future_cols) + len(self.observed_cols)


# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


class TFTConfig(BaseModel):
    """Configuration for a TFT canary training run.

    Frozen + ``extra='forbid'`` for audit integrity. Defaults are
    shadow-oriented: ``shadow_only=True``, a modest transformer
    (``d_model=64``, ``n_heads=4``, ``n_layers=2``), and a small
    configuration so a run completes quickly on CPU for smoke tests while
    still exercising the full TFT code path.

    Attributes:
        seq_len: Encoder length — number of timesteps per sample (must
            be >= 1).
        horizon: Forecast horizon — number of future timesteps to
            predict (must be >= 1).
        d_model: Transformer hidden dimension (must be >= 1 and
            divisible by ``n_heads``).
        n_heads: Number of attention heads (must be >= 1).
        n_layers: Number of temporal self-attention layers (must be
            >= 1).
        ff_dim: Feedforward network hidden dimension (must be >= 1).
        dropout: Dropout probability (must be in [0, 1)).
        learning_rate: Adam learning rate (must be > 0).
        epochs: Number of training epochs.
        batch_size: Mini-batch size.
        device: Device to run on — ``auto``, ``cpu``, or ``cuda``.
        seed: Random seed for reproducibility.
        shadow_only: When ``True`` (default) the run is marked shadow
            and ``promotion_eligible`` is forced to ``False``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq_len: int
    horizon: int
    d_model: int = 64
    n_heads: int = 4
    n_layers: int = 2
    ff_dim: int = 128
    dropout: float = 0.1
    learning_rate: float = 0.001
    epochs: int = 10
    batch_size: int = 32
    device: str = "auto"
    seed: int = 42
    shadow_only: bool = True

    @field_validator("seq_len")
    @classmethod
    def _seq_len_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"seq_len must be >= 1; got {v}")
        return v

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"horizon must be >= 1; got {v}")
        return v

    @field_validator("d_model")
    @classmethod
    def _d_model_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"d_model must be >= 1; got {v}")
        return v

    @field_validator("n_heads")
    @classmethod
    def _n_heads_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_heads must be >= 1; got {v}")
        return v

    @field_validator("n_layers")
    @classmethod
    def _n_layers_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_layers must be >= 1; got {v}")
        return v

    @field_validator("ff_dim")
    @classmethod
    def _ff_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"ff_dim must be >= 1; got {v}")
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
    def _d_model_divisible_by_n_heads(self) -> TFTConfig:
        """d_model must be divisible by n_heads."""
        if self.d_model % self.n_heads != 0:
            raise ValueError(
                f"d_model must be divisible by n_heads; "
                f"got d_model={self.d_model}, n_heads={self.n_heads}"
            )
        return self


class TFTTrainingResult(BaseModel):
    """Result of a TFT canary training run.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the config
    used, the covariate roles declared, the per-epoch losses, the GPU
    status at training time, the paths to the saved model / OOF artifacts
    (if any), the multi-horizon predictions, and the promotion-
    eligibility flag.

    Attributes:
        config: The :class:`TFTConfig` used for the run.
        covariate_roles: The :class:`CovariateRoles` declared for the
            run.
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
        metrics: Extra metrics (e.g. ``{"mse": ...}``).
        multi_horizon_predictions: Predictions for each horizon step
            (shape: ``list[list[float]]`` — outer list has one entry per
            sample, inner list has one entry per horizon step), or
            ``None`` if not computed.
        duration_seconds: Wall-clock training duration in seconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: TFTConfig
    covariate_roles: CovariateRoles
    final_loss: float
    epoch_losses: list[float] = Field(default_factory=list)
    gpu_status: GPUStatus
    artifact_path: str | None = None
    oof_artifact_path: str | None = None
    is_shadow: bool
    promotion_eligible: bool
    metrics: dict[str, float] = Field(default_factory=dict)
    multi_horizon_predictions: list[list[float]] | None = None
    duration_seconds: float


# ---------------------------------------------------------------------------
# TFT model
# ---------------------------------------------------------------------------


class TFTModel:
    """A simplified Temporal Fusion Transformer model.

    Architecture (simplified TFT):

    - **Variable Selection Network (VSN)** for observed and known-future
      inputs — learns per-variable weights and projects selected
      variables to ``d_model`` dimensions.
    - **Static covariate encoder** — projects static covariates into a
      context vector that conditions the temporal processing.
    - **Temporal self-attention** — a standard transformer encoder over
      the temporal dimension.
    - **Gated Residual Network (GRN)** — a gated skip-connection block
      applied after attention.
    - **Multi-horizon output head** — one output per horizon step.

    Input shape: ``(batch, seq_len, n_features)`` where ``n_features`` is
    the total number of non-target feature columns (static + known-future
    + observed). Static covariates are expected to be broadcast across
    the temporal dimension by the caller (each static column is repeated
    for every timestep in the window).
    Output shape: ``(batch, horizon, 1)``.

    This is a thin wrapper around ``torch.nn.Module`` (built lazily so
    the module remains importable without torch), mirroring the pattern
    in :class:`~quant_foundry.patchtst_trainer.PatchTSTModel`.
    """

    def __init__(
        self,
        n_features: int,
        n_static: int,
        seq_len: int,
        horizon: int,
        d_model: int,
        n_heads: int,
        n_layers: int,
        ff_dim: int,
        dropout: float = 0.1,
    ) -> None:
        if n_features < 1:
            raise ValueError("n_features must be >= 1")
        if n_static < 0:
            raise ValueError("n_static must be >= 0")
        if seq_len < 1:
            raise ValueError("seq_len must be >= 1")
        if horizon < 1:
            raise ValueError("horizon must be >= 1")
        if d_model < 1:
            raise ValueError("d_model must be >= 1")
        if n_heads < 1:
            raise ValueError("n_heads must be >= 1")
        if n_layers < 1:
            raise ValueError("n_layers must be >= 1")
        if ff_dim < 1:
            raise ValueError("ff_dim must be >= 1")
        if d_model % n_heads != 0:
            raise ValueError("d_model must be divisible by n_heads")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.n_features = n_features
        self.n_static = n_static
        self.seq_len = seq_len
        self.horizon = horizon
        self.d_model = d_model
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.ff_dim = ff_dim
        self.dropout = dropout
        self._module: Any = None

    def _build_module(self) -> Any:
        """Build and return the underlying ``torch.nn.Module``.

        Lazily imports torch. The built module is cached on
        ``self._module`` so repeated calls return the same instance.
        """
        if self._module is not None:
            return self._module

        net = _make_tft_module_class()(
            n_features=self.n_features,
            n_static=self.n_static,
            seq_len=self.seq_len,
            horizon=self.horizon,
            d_model=self.d_model,
            n_heads=self.n_heads,
            n_layers=self.n_layers,
            ff_dim=self.ff_dim,
            dropout=self.dropout,
        )
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(self, x: Any, static_data: Any = None) -> Any:
        """Run a forward pass.

        Args:
            x: Temporal features — tensor of shape
                ``(batch, seq_len, n_features)``.
            static_data: Static covariates — tensor of shape
                ``(batch, n_static)``. When ``None`` (or when
                ``n_static == 0``), a zero context vector is used.

        Returns:
            A tensor of shape ``(batch, horizon, 1)``.
        """
        return self.module(x, static_data)

    def parameters(self) -> Any:
        """Return the underlying module's parameters iterator."""
        return self.module.parameters()

    def state_dict(self) -> dict[str, Any]:
        """Return the underlying module's state_dict."""
        return self.module.state_dict()

    def load_state_dict(self, state_dict: dict[str, Any]) -> None:
        """Load a state_dict into the underlying module."""
        self.module.load_state_dict(state_dict)

    def to(self, device: Any) -> TFTModel:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> TFTModel:
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> TFTModel:
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


def _make_tft_module_class() -> Any:
    """Build and return the real ``nn.Module`` subclass for TFT.

    Lazily imports torch. Called from :meth:`TFTModel._build_module` each
    time a new model is built.
    """
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class _VariableSelectionNetwork(nn.Module):
        """Variable Selection Network (VSN).

        Learns a per-variable weight (via a softmax over variables) and
        produces a weighted sum of variable embeddings. This implements
        the variable-selection component of the TFT architecture in a
        simplified form.
        """

        def __init__(self, n_features: int, d_model: int, dropout: float) -> None:
            super().__init__()
            self.n_features = n_features
            self.d_model = d_model
            # Per-variable linear projection to d_model.
            self.var_proj = nn.Linear(n_features, d_model * n_features)
            # Variable selection weights.
            self.var_weights = nn.Linear(n_features, n_features)
            self.dropout = nn.Dropout(dropout)

        def forward(self, x: Any) -> Any:
            # x: (batch, seq_len, n_features)
            batch, seq_len, n_feat = x.shape
            # Variable selection weights: (batch, seq_len, n_features)
            weights = torch.softmax(self.var_weights(x), dim=-1)
            # Project each variable to d_model.
            proj = self.var_proj(x)  # (batch, seq_len, d_model * n_features)
            proj = proj.view(batch, seq_len, n_feat, self.d_model)
            # Weighted sum over variables: (batch, seq_len, d_model)
            weights_expanded = weights.unsqueeze(-1)  # (batch, seq_len, n_feat, 1)
            selected = (proj * weights_expanded).sum(dim=2)
            return self.dropout(selected)

    class _GatedResidualNetwork(nn.Module):
        """Gated Residual Network (GRN).

        A gated skip-connection block: ``output = LayerNorm(x + GLU(
        linear(ELU(linear(x)))))``. This implements the GRN component of
        the TFT architecture in a simplified form.
        """

        def __init__(self, d_model: int, ff_dim: int, dropout: float) -> None:
            super().__init__()
            self.fc1 = nn.Linear(d_model, ff_dim)
            self.fc2 = nn.Linear(ff_dim, d_model)
            self.gate = nn.Linear(d_model, d_model)
            self.layer_norm = nn.LayerNorm(d_model)
            self.dropout = nn.Dropout(dropout)

        def forward(self, x: Any) -> Any:
            # x: (..., d_model)
            elu_out = F.elu(self.fc1(x))
            transformed = self.fc2(elu_out)
            gate = torch.sigmoid(self.gate(x))
            gated = transformed * gate
            return self.layer_norm(x + self.dropout(gated))

    class _TFTNet(nn.Module):
        """Inner nn.Module implementing the simplified TFT forward pass."""

        def __init__(
            self,
            n_features: int,
            n_static: int,
            seq_len: int,
            horizon: int,
            d_model: int,
            n_heads: int,
            n_layers: int,
            ff_dim: int,
            dropout: float,
        ) -> None:
            super().__init__()
            self.n_features = n_features
            self.n_static = n_static
            self.seq_len = seq_len
            self.horizon = horizon
            self.d_model = d_model

            # Variable Selection Network for temporal features.
            self.vsn = _VariableSelectionNetwork(n_features, d_model, dropout)

            # Static covariate encoder.
            if n_static > 0:
                self.static_encoder = nn.Linear(n_static, d_model)
            else:
                self.static_encoder = None

            # Positional encoding (learned).
            self.pos_embedding = nn.Parameter(torch.randn(1, seq_len, d_model) * 0.02)

            # Temporal self-attention encoder.
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=d_model,
                nhead=n_heads,
                dim_feedforward=ff_dim,
                dropout=dropout,
                batch_first=True,
            )
            self.temporal_attention = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)

            # Gated Residual Network after attention.
            self.grn = _GatedResidualNetwork(d_model, ff_dim, dropout)

            # Multi-horizon output head: one output per horizon step.
            self.horizon_heads = nn.ModuleList([nn.Linear(d_model, 1) for _ in range(horizon)])

            self.dropout = nn.Dropout(dropout)

        def forward(self, x: Any, static_data: Any = None) -> Any:
            # x: (batch, seq_len, n_features)
            x.shape[0]

            # Variable selection over temporal features.
            selected = self.vsn(x)  # (batch, seq_len, d_model)

            # Add positional encoding.
            selected = selected + self.pos_embedding

            # Static covariate context.
            if self.static_encoder is not None and static_data is not None:
                static_ctx = self.static_encoder(static_data)  # (batch, d_model)
                # Broadcast static context across the temporal dimension.
                static_ctx = static_ctx.unsqueeze(1)  # (batch, 1, d_model)
                selected = selected + static_ctx

            # Temporal self-attention.
            attended = self.temporal_attention(selected)

            # GRN after attention.
            grn_out = self.grn(attended)

            # Mean-pool over the temporal dimension -> (batch, d_model).
            pooled = grn_out.mean(dim=1)

            # Multi-horizon output: one head per horizon step.
            outputs: list[Any] = []
            for head in self.horizon_heads:
                outputs.append(head(pooled))  # (batch, 1)
            # Stack -> (batch, horizon, 1).
            return torch.stack(outputs, dim=1)

    return _TFTNet


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def _resolve_device(config: TFTConfig, gpu_status: GPUStatus) -> Any:
    """Resolve the torch device for a TFT run.

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
# TFTTrainer
# ---------------------------------------------------------------------------


class TFTTrainer:
    """Train / predict / save / load / OOF-write façade for TFT.

    The trainer requires an explicit :class:`CovariateRoles` declaration
    at construction time (fail-closed — no loose inference of covariate
    categories). It builds a :class:`TFTModel`, trains it with Adam on
    the provided temporal features / labels, saves the trained
    state_dict, and can write OOF predictions via :class:`OOFWriter`.

    Args:
        config: The :class:`TFTConfig` for the run.
        covariate_roles: The :class:`CovariateRoles` declaration for the
            run. Must be complete (all categories declared).
    """

    def __init__(
        self,
        config: TFTConfig,
        covariate_roles: CovariateRoles,
    ) -> None:
        if not isinstance(config, TFTConfig):
            raise TypeError("config must be a TFTConfig")
        if not isinstance(covariate_roles, CovariateRoles):
            raise TypeError("covariate_roles must be a CovariateRoles")
        self.config = config
        self.covariate_roles = covariate_roles
        self.model_: TFTModel | None = None

    # -- covariate validation ---------------------------------------------

    def validate_covariates(self) -> None:
        """Validate that covariate roles are complete (fail-closed).

        Raises:
            ValueError: if ``covariate_roles`` is ``None`` or incomplete
                (any required category missing or empty).
        """
        roles = self.covariate_roles
        if roles is None:
            raise ValueError(
                "covariate_roles is None — TFT requires explicit "
                "covariate role declaration before training (fail-closed)"
            )
        if not isinstance(roles, CovariateRoles):
            raise ValueError("covariate_roles must be a CovariateRoles instance (fail-closed)")
        # Check all required categories are present and non-empty.
        if not roles.static_cols:
            raise ValueError(
                "static_cols is empty — TFT requires explicit "
                "declaration of static covariates (fail-closed)"
            )
        if not roles.known_future_cols:
            raise ValueError(
                "known_future_cols is empty — TFT requires explicit "
                "declaration of known-future covariates (fail-closed)"
            )
        if not roles.observed_cols:
            raise ValueError(
                "observed_cols is empty — TFT requires explicit "
                "declaration of observed covariates (fail-closed)"
            )
        if not roles.target_col or not roles.target_col.strip():
            raise ValueError(
                "target_col is empty — TFT requires an explicit target "
                "column declaration (fail-closed)"
            )

    # -- training ---------------------------------------------------------

    def train(
        self,
        X: Any,
        y: Any,
        static_data: Any = None,
    ) -> TFTTrainingResult:
        """Train a TFT model on ``X`` / ``y`` and return the result.

        Args:
            X: Temporal features — a numpy array or list of lists of
                shape ``(n_samples, seq_len, n_features)`` where
                ``n_features`` is the total number of non-target feature
                columns (static + known-future + observed, with static
                columns broadcast across the temporal dimension).
            y: Labels — a 1-D array-like or a pandas Series. When
                ``horizon > 1``, ``y`` may be 2-D of shape
                ``(n_samples, horizon)``; if 1-D, the same label is used
                for all horizon steps.
            static_data: Static covariates — a numpy array of shape
                ``(n_samples, n_static)``. When ``None``, a zero context
                vector is used.

        Returns:
            A :class:`TFTTrainingResult` with epoch losses, GPU status,
            multi-horizon predictions, and promotion eligibility.
        """
        import numpy as np
        import torch
        import torch.nn as nn

        # Fail-closed: validate covariate roles before anything else.
        self.validate_covariates()

        start = time.perf_counter()

        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)

        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)

        n_features = self.covariate_roles.n_features()
        n_static = len(self.covariate_roles.static_cols)

        X_arr = np.array(X, dtype=float)
        if X_arr.ndim != 3:
            raise ValueError(
                f"X must be 3-D (n_samples, seq_len, n_features); got shape {X_arr.shape}"
            )
        if X_arr.shape[1] != self.config.seq_len:
            raise ValueError(
                f"X.shape[1] must equal seq_len={self.config.seq_len}; got {X_arr.shape[1]}"
            )
        if X_arr.shape[2] != n_features:
            raise ValueError(
                f"X.shape[2] must equal n_features={n_features} "
                f"(static={len(self.covariate_roles.static_cols)} + "
                f"known_future={len(self.covariate_roles.known_future_cols)} + "
                f"observed={len(self.covariate_roles.observed_cols)}); "
                f"got {X_arr.shape[2]}"
            )

        y_arr = np.array(y, dtype=float)
        if y_arr.ndim == 1:
            # Broadcast 1-D label to all horizon steps.
            y_arr = np.tile(y_arr.reshape(-1, 1), (1, self.config.horizon))
        if y_arr.ndim != 2:
            raise ValueError(
                f"y must be 1-D (n_samples,) or 2-D (n_samples, horizon); got shape {y_arr.shape}"
            )
        if y_arr.shape[1] != self.config.horizon:
            raise ValueError(
                f"y.shape[1] must equal horizon={self.config.horizon}; got {y_arr.shape[1]}"
            )

        n_samples = X_arr.shape[0]

        # Static data.
        if static_data is not None:
            static_arr = np.array(static_data, dtype=float)
            if static_arr.ndim != 2:
                raise ValueError(
                    f"static_data must be 2-D (n_samples, n_static); got shape {static_arr.shape}"
                )
            if static_arr.shape[1] != n_static:
                raise ValueError(
                    f"static_data.shape[1] must equal n_static={n_static}; "
                    f"got {static_arr.shape[1]}"
                )
        else:
            static_arr = np.zeros((n_samples, n_static), dtype=float)

        x_tensor = torch.from_numpy(X_arr).float()
        y_tensor = torch.from_numpy(y_arr).float()
        static_tensor = torch.from_numpy(static_arr).float()

        model = TFTModel(
            n_features=n_features,
            n_static=n_static,
            seq_len=self.config.seq_len,
            horizon=self.config.horizon,
            d_model=self.config.d_model,
            n_heads=self.config.n_heads,
            n_layers=self.config.n_layers,
            ff_dim=self.config.ff_dim,
            dropout=self.config.dropout,
        )
        model.to(device)
        model.train()

        loss_fn = nn.MSELoss()
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
                    batch_static = static_tensor[idx].to(device)

                    # TransformerEncoderLayer with batchnorm requires
                    # more than 1 sample per batch.
                    if batch_x.shape[0] < 2:
                        continue

                    optimizer.zero_grad()
                    preds = model.forward(batch_x, batch_static)
                    # preds: (batch, horizon, 1)
                    # batch_y: (batch, horizon)
                    preds_squeezed = preds.squeeze(-1)  # (batch, horizon)
                    loss = loss_fn(preds_squeezed, batch_y)
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

        # Compute multi-horizon predictions and metrics (eval mode).
        metrics: dict[str, float] = {}
        multi_horizon_preds: list[list[float]] | None = None
        if n_samples > 0 and self.config.epochs > 0:
            model.eval()
            with torch.no_grad():
                preds = model.forward(
                    x_tensor.to(device),
                    static_tensor.to(device),
                )
                # preds: (n_samples, horizon, 1)
                preds_np = preds.squeeze(-1).cpu().numpy()
                # Multi-horizon predictions: list[list[float]]
                multi_horizon_preds = [[float(v) for v in row] for row in preds_np]
                mse = float(np.mean((preds_np - y_arr) ** 2))
                metrics["mse"] = mse
                metrics["final_loss"] = final_loss

        duration = time.perf_counter() - start

        return TFTTrainingResult(
            config=self.config,
            covariate_roles=self.covariate_roles,
            final_loss=final_loss,
            epoch_losses=epoch_losses,
            gpu_status=gpu_status,
            artifact_path=None,
            oof_artifact_path=None,
            is_shadow=self.config.shadow_only,
            promotion_eligible=not self.config.shadow_only,
            metrics=metrics,
            multi_horizon_predictions=multi_horizon_preds,
            duration_seconds=duration,
        )

    # -- prediction -------------------------------------------------------

    def predict(self, X: Any, static_data: Any = None) -> list[list[float]]:
        """Predict multi-horizon outputs for ``X``.

        Uses the in-memory trained model (or the model loaded via
        :meth:`load_artifact`), runs a forward pass, and returns a list
        of per-sample multi-horizon predictions.

        Args:
            X: Temporal features — same format as :meth:`train`.
            static_data: Static covariates — same format as
                :meth:`train`.

        Returns:
            A list of lists of floats — one inner list per sample, each
            containing ``horizon`` predictions.
        """
        import numpy as np
        import torch

        if self.model_ is None:
            raise ValueError("no trained model available — call train() or load_artifact() first")

        n_features = self.covariate_roles.n_features()
        n_static = len(self.covariate_roles.static_cols)

        X_arr = np.array(X, dtype=float)
        if X_arr.ndim != 3:
            raise ValueError(
                f"X must be 3-D (n_samples, seq_len, n_features); got shape {X_arr.shape}"
            )
        if X_arr.shape[1] != self.config.seq_len:
            raise ValueError(
                f"X.shape[1] must equal seq_len={self.config.seq_len}; got {X_arr.shape[1]}"
            )
        if X_arr.shape[2] != n_features:
            raise ValueError(f"X.shape[2] must equal n_features={n_features}; got {X_arr.shape[2]}")

        n_samples = X_arr.shape[0]

        if static_data is not None:
            static_arr = np.array(static_data, dtype=float)
            if static_arr.ndim != 2:
                raise ValueError(
                    f"static_data must be 2-D (n_samples, n_static); got shape {static_arr.shape}"
                )
        else:
            static_arr = np.zeros((n_samples, n_static), dtype=float)

        x_tensor = torch.from_numpy(X_arr).float()
        static_tensor = torch.from_numpy(static_arr).float()

        model = self.model_
        model.eval()
        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)
        model.to(device)

        with torch.no_grad():
            preds = model.forward(
                x_tensor.to(device),
                static_tensor.to(device),
            )
            # preds: (n_samples, horizon, 1)
            preds_np = preds.squeeze(-1).cpu().numpy()

        return [[float(v) for v in row] for row in preds_np]

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

    def load_artifact(self, path: str) -> TFTModel:
        """Load a state_dict from ``path`` into a new :class:`TFTModel`.

        The new model is built from the trainer's config and covariate
        roles, and the saved state_dict is loaded into it. The model is
        set to eval mode on CPU and stored on the trainer
        (``self.model_``).

        Returns:
            The loaded :class:`TFTModel`.
        """
        import torch

        n_features = self.covariate_roles.n_features()
        n_static = len(self.covariate_roles.static_cols)

        model = TFTModel(
            n_features=n_features,
            n_static=n_static,
            seq_len=self.config.seq_len,
            horizon=self.config.horizon,
            d_model=self.config.d_model,
            n_heads=self.config.n_heads,
            n_layers=self.config.n_layers,
            ff_dim=self.config.ff_dim,
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
            fold_predictions: Per-row predictions. Each entry is a
                multi-horizon prediction list. Must have the same length
                as ``fold_ids``.
            fold_ids: Per-row fold ids.
            symbols: Per-row instrument symbols.
            timestamps: Per-row ISO-format timestamps.
            labels: Per-row ground-truth labels.
            horizons: Per-row prediction horizons.
            weights: Per-row sample weights. When ``None``, 1.0 is
                used for every row.
            output_path: Path to write the OOF artifact to.

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
        writer = OOFWriter(model_family="tft", output_dir=output_dir)
        for i in range(n):
            row_id = f"{symbols[i]}_{timestamps[i]}_{horizons[i]}"
            w = float(weights[i]) if weights is not None else 1.0
            # For multi-horizon predictions, use the first horizon's
            # prediction as the OOF prediction value (the standard OOF
            # schema is per-row, per-horizon).
            pred_value = (
                float(fold_predictions[i][0])
                if isinstance(fold_predictions[i], list)
                else float(fold_predictions[i])
            )
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
# Promotion eligibility
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: TFTTrainingResult,
    manual_override: bool = False,
) -> bool:
    """Validate whether a TFT training result is promotion eligible.

    Promotion rules (fail-closed):

    - If ``result.is_shadow`` is ``True`` (the default for TFT), the run
      is **only** eligible when ``manual_override`` is ``True`` — i.e.
      an operator explicitly overrides the shadow gate. A shadow run
      with no override is **not** eligible.
    - If ``result.is_shadow`` is ``False`` (a non-shadow run), the run
      is eligible regardless of ``manual_override``.

    Args:
        result: The :class:`TFTTrainingResult` to validate.
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


def register_tft_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for TFT registration.

    The returned dict carries the fields a
    :class:`~quant_foundry.alpha_genome.ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus
    TFT-specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not**
    mutate the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks TFT as a shadow family: it is **not** a baseline
    exception, does not require a GPU (the trainer degrades gracefully
    to CPU), and defaults to the ``CHALLENGER`` promotion-eligibility
    class (though the trainer itself forces ``promotion_eligible=False``
    when ``shadow_only=True``).
    """
    return {
        "family_id": "tft",
        "display_name": "TFT (Temporal Fusion Transformer, shadow canary)",
        "version": "1",
        "dataset_shape": "sequence_windowed",
        "objectives": ("binary", "regression"),
        "artifact_format": "torch_state_dict",
        "artifact_loader": "quant_foundry.tft_trainer.TFTTrainer.load_artifact",
        "required_metrics": ("mse", "mae", "final_loss"),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "challenger",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "shadow_only": True,
        "default_d_model": 64,
        "default_n_heads": 4,
        "default_n_layers": 2,
        "default_ff_dim": 128,
        "requires_covariate_roles": True,
        "multi_horizon": True,
    }


__all__ = [
    "CovariateRole",
    "CovariateRoles",
    "TFTConfig",
    "TFTModel",
    "TFTTrainer",
    "TFTTrainingResult",
    "register_tft_family",
    "validate_promotion_eligibility",
]
