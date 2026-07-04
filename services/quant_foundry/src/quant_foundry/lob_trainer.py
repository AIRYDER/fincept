"""quant_foundry.lob_trainer — DeepLOB-style canary trainer (T-LOB.2).

DeepLOB (Zhang et al. 2019) is a deep-learning model for limit order book
(LOB) mid-price directional prediction. It combines a convolutional
feature extractor (1-D convs over the LOB snapshot's price/size levels)
with a temporal LSTM layer (over consecutive snapshots) and a linear
classification head that predicts one of three directional classes:
up, stationary, down.

This module provides a self-contained, importable DeepLOB-style
**canary** trainer for the quant foundry's LOB modeling path. It builds
on :mod:`quant_foundry.tabular_neural_runtime` (for
:class:`GPUStatus` / :func:`check_gpu`), the LOB manifest schema from
:mod:`quant_foundry.lob_manifest` (for :class:`LOBVenue`), and the OOF
artifact writer (:mod:`quant_foundry.oof_artifacts`).

Capabilities:

- :class:`DeepLOBConfig` — frozen, ``extra='forbid'`` config for a
  DeepLOB-style canary training run (architecture, optimizer,
  shadow-mode defaults).
- :class:`DeepLOBTrainingResult` — frozen, ``extra='forbid'`` result
  carrying epoch losses, GPU status, artifact paths, classification
  metrics, cost-adjusted returns, inference latency, and promotion
  eligibility.
- :class:`DeepLOBModel` — a DeepLOB-style ``nn.Module`` wrapper with a
  CNN feature extractor, LSTM temporal layer, and linear classification
  head.
- :class:`DeepLOBTrainer` — the train / predict / save / load /
  OOF-write façade used by the research dispatch path.
- :func:`compute_lob_metrics` — accuracy / precision / recall / f1 /
  directional accuracy for LOB directional prediction.
- :func:`compute_spread_adjusted_return` — net return after spread cost.
- :func:`compute_fee_adjusted_return` — net return after fee cost.
- :func:`measure_inference_latency` — average inference latency in ms.
- :func:`validate_promotion_eligibility` — fail-closed promotion gate:
  shadow runs are only eligible with an explicit manual override.
- :func:`register_lob_family` — returns a
  :class:`~quant_foundry.alpha_genome.ModelFamilySpec`-compatible dict
  for DeepLOB registration (does not mutate the registry itself).

Design notes (cross-cutting quant rigor, BIG_PLAN):

- **Shadow mode by default.** ``DeepLOBConfig.shadow_only`` defaults to
  ``True`` and ``DeepLOBTrainingResult.promotion_eligible`` is forced to
  ``False``. Promotion requires an explicit manual override — there is
  no automatic path from shadow to production.
- **One liquid symbol, one venue, one short horizon.** The trainer is
  scoped to a single (venue, symbol, horizon) tuple so the canary is a
  minimal, auditable unit.
- **Cost-aware evaluation.** The result carries spread-adjusted and
  fee-adjusted returns alongside raw classification metrics, so a
  reviewer sees the post-cost economics before any promotion decision.
- **Latency-aware.** Inference latency is measured and recorded so the
  canary surfaces the round-trip cost of using the model in a trading
  loop.
- **No live trading authority.** A shadow DeepLOB run never produces
  tradeable predictions; its outputs are OOF predictions for ensemble
  integration and a model artifact for offline evaluation only.
- **No secrets.** Configs carry only architecture + optimizer
  hyperparameters, a device string, and a seed — never credentials or
  filesystem paths beyond the optional artifact path.
- **Cost fails closed.** Invalid configs are rejected at construction;
  training errors surface as exceptions rather than partial results.
- **Lazy torch import.** ``import torch`` happens inside methods, never
  at module top level, so this module is importable on hosts without
  torch (the Pydantic models and ``register_lob_family`` can be
  constructed without torch installed).
- **File-disjoint.** New module; does not modify ``patchtst_trainer.py``,
  ``tft_trainer.py``, ``lob_manifest.py``, or ``oof_artifacts.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_foundry.lob_manifest import LOBVenue
from quant_foundry.oof_artifacts import OOFWriter
from quant_foundry.tabular_neural_runtime import GPUStatus, check_gpu

# ---------------------------------------------------------------------------
# Allowed venues (string values of LOBVenue)
# ---------------------------------------------------------------------------

_ALLOWED_VENUES: frozenset[str] = frozenset(v.value for v in LOBVenue)


# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


class DeepLOBConfig(BaseModel):
    """Configuration for a DeepLOB-style canary training run.

    Frozen + ``extra='forbid'`` for audit integrity. Defaults are
    shadow-oriented: ``shadow_only=True``, a modest CNN+LSTM
    (``hidden_dim=64``, ``n_conv_layers=2``, ``n_lstm_layers=1``), a
    short horizon (``horizon=10`` events), and a small batch size so a
    run completes quickly on CPU for smoke tests while still exercising
    the full DeepLOB code path.

    Attributes:
        n_levels: Book depth — number of price levels per side (must be
            >= 1).
        n_features: Number of flattened input features per snapshot
            (``2 * n_levels * 2`` for bids/asks price/size). Must be
            >= 1.
        hidden_dim: Hidden dimension of the conv/LSTM stack (must be
            >= 1).
        n_conv_layers: Number of 1-D conv layers in the feature
            extractor (must be >= 1).
        n_lstm_layers: Number of LSTM layers for temporal modeling
            (must be >= 1).
        horizon: Prediction horizon in events (must be >= 1).
        learning_rate: Adam learning rate (must be > 0).
        epochs: Number of training epochs.
        batch_size: Mini-batch size (must be >= 1).
        dropout: Dropout probability (must be in [0, 1)).
        device: Device to run on — ``auto``, ``cpu``, or ``cuda``.
        seed: Random seed for reproducibility.
        shadow_only: When ``True`` (default) the run is marked shadow
            and ``promotion_eligible`` is forced to ``False``.
        n_classes: Number of directional classes (must be >= 2).
            Default 3 = up / stationary / down.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_levels: int = 10
    n_features: int = 40
    hidden_dim: int = 64
    n_conv_layers: int = 2
    n_lstm_layers: int = 1
    horizon: int = 10
    learning_rate: float = 0.001
    epochs: int = 10
    batch_size: int = 32
    dropout: float = 0.1
    device: str = "auto"
    seed: int = 42
    shadow_only: bool = True
    n_classes: int = 3

    @field_validator("n_levels")
    @classmethod
    def _n_levels_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_levels must be >= 1; got {v}")
        return v

    @field_validator("n_features")
    @classmethod
    def _n_features_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_features must be >= 1; got {v}")
        return v

    @field_validator("hidden_dim")
    @classmethod
    def _hidden_dim_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"hidden_dim must be >= 1; got {v}")
        return v

    @field_validator("n_conv_layers")
    @classmethod
    def _n_conv_layers_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_conv_layers must be >= 1; got {v}")
        return v

    @field_validator("n_lstm_layers")
    @classmethod
    def _n_lstm_layers_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"n_lstm_layers must be >= 1; got {v}")
        return v

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"horizon must be >= 1; got {v}")
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

    @field_validator("n_classes")
    @classmethod
    def _n_classes_min(cls, v: int) -> int:
        if v < 2:
            raise ValueError(f"n_classes must be >= 2; got {v}")
        return v

    @field_validator("device")
    @classmethod
    def _device_allowed(cls, v: str) -> str:
        allowed = {"auto", "cpu", "cuda"}
        if v not in allowed:
            raise ValueError(f"device must be one of {sorted(allowed)}; got {v!r}")
        return v


class DeepLOBTrainingResult(BaseModel):
    """Result of a DeepLOB-style canary training run.

    Frozen + ``extra='forbid'`` for audit integrity. Carries the config
    used, the (venue, symbol) scope, the per-epoch losses, the GPU
    status at training time, the paths to the saved model / OOF
    artifacts (if any), classification metrics, cost-adjusted returns,
    inference latency, and the promotion-eligibility flag.

    Attributes:
        config: The :class:`DeepLOBConfig` used for the run.
        venue: The LOB venue (a :class:`LOBVenue` value).
        symbol: The instrument symbol.
        final_loss: The mean loss of the final epoch (NaN if 0 epochs).
        epoch_losses: Mean loss per epoch (one entry per epoch).
        gpu_status: GPU availability + memory state at training time.
        artifact_path: Path to the saved model state_dict, or ``None``.
        oof_artifact_path: Path to the OOF predictions artifact, or
            ``None`` if no OOF predictions were written.
        is_shadow: ``True`` when the run was in shadow mode.
        promotion_eligible: Always ``False`` — shadow-only canary.
        metrics: Classification metrics (accuracy, precision, recall,
            f1, directional_accuracy).
        spread_adjusted_return: Net return after spread cost, or
            ``None`` if not computed.
        fee_adjusted_return: Net return after fee cost, or ``None`` if
            not computed.
        latency_ms: Average inference latency in milliseconds, or
            ``None`` if not measured.
        duration_seconds: Wall-clock training duration in seconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: DeepLOBConfig
    venue: str
    symbol: str
    final_loss: float
    epoch_losses: list[float] = Field(default_factory=list)
    gpu_status: GPUStatus
    artifact_path: str | None = None
    oof_artifact_path: str | None = None
    is_shadow: bool
    promotion_eligible: bool = False
    metrics: dict[str, float] = Field(default_factory=dict)
    spread_adjusted_return: float | None = None
    fee_adjusted_return: float | None = None
    latency_ms: float | None = None
    duration_seconds: float

    @field_validator("venue")
    @classmethod
    def _venue_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_VENUES:
            raise ValueError(f"venue must be one of {sorted(_ALLOWED_VENUES)!r}; got {v!r}")
        return v

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("symbol must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# DeepLOB model
# ---------------------------------------------------------------------------


class DeepLOBModel:
    """A DeepLOB-style model: CNN feature extractor + LSTM + linear head.

    Architecture:

    - A stack of ``n_conv_layers`` 1-D convolutional layers extracts
      local features from each LOB snapshot (treating the flattened
      price/size features as channels). Each conv layer is followed by
      a ReLU and (optionally) dropout.
    - An LSTM with ``n_lstm_layers`` layers processes the conv-extracted
      feature sequence over time, capturing temporal dependencies
      between consecutive snapshots.
    - A linear classification head maps each LSTM hidden state to
      ``n_classes`` logits (up / stationary / down), giving one
      prediction per timestep.
    - The forward pass takes ``(batch, seq_len, n_features)`` and
      returns ``(batch, seq_len, n_classes)`` — one set of logits per
      LOB snapshot.

    This is a thin wrapper around ``torch.nn.Module`` (built lazily so
    the module remains importable without torch), mirroring the pattern
    in :class:`~quant_foundry.patchtst_trainer.PatchTSTModel`.
    """

    def __init__(
        self,
        n_features: int,
        hidden_dim: int,
        n_conv_layers: int,
        n_lstm_layers: int,
        n_classes: int,
        dropout: float = 0.1,
    ) -> None:
        if n_features < 1:
            raise ValueError("n_features must be >= 1")
        if hidden_dim < 1:
            raise ValueError("hidden_dim must be >= 1")
        if n_conv_layers < 1:
            raise ValueError("n_conv_layers must be >= 1")
        if n_lstm_layers < 1:
            raise ValueError("n_lstm_layers must be >= 1")
        if n_classes < 2:
            raise ValueError("n_classes must be >= 2")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        self.n_features = n_features
        self.hidden_dim = hidden_dim
        self.n_conv_layers = n_conv_layers
        self.n_lstm_layers = n_lstm_layers
        self.n_classes = n_classes
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

        # Build the conv stack: each layer is Conv1d(in, hidden_dim, 3,
        # padding=1) + ReLU [+ Dropout]. The first layer maps
        # n_features channels to hidden_dim; subsequent layers keep
        # hidden_dim channels.
        conv_layers: list[Any] = []
        for i in range(self.n_conv_layers):
            in_ch = self.n_features if i == 0 else self.hidden_dim
            conv_layers.append(
                nn.Conv1d(
                    in_channels=in_ch,
                    out_channels=self.hidden_dim,
                    kernel_size=3,
                    padding=1,
                )
            )
            conv_layers.append(nn.ReLU())
            if self.dropout > 0:
                conv_layers.append(nn.Dropout(self.dropout))
        conv_stack = nn.Sequential(*conv_layers)

        lstm = nn.LSTM(
            input_size=self.hidden_dim,
            hidden_size=self.hidden_dim,
            num_layers=self.n_lstm_layers,
            batch_first=True,
            dropout=self.dropout if self.n_lstm_layers > 1 else 0.0,
        )
        head = nn.Linear(self.hidden_dim, self.n_classes)

        net = _make_deeplob_module_class()(
            conv_stack=conv_stack,
            lstm=lstm,
            head=head,
            hidden_dim=self.hidden_dim,
        )
        self._module = net
        return net

    @property
    def module(self) -> Any:
        """Return the underlying ``torch.nn.Module``, building it if needed."""
        return self._build_module()

    def forward(self, x: Any) -> Any:
        """Run a forward pass.

        Args:
            x: Tensor of shape ``(batch, seq_len, n_features)``.

        Returns:
            Tensor of shape ``(batch, seq_len, n_classes)`` (logits,
            one set per timestep).
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

    def to(self, device: Any) -> DeepLOBModel:
        """Move the underlying module to ``device`` and return self."""
        self._module = self.module.to(device)
        return self

    def train(self, mode: bool = True) -> DeepLOBModel:
        """Set the underlying module's train/eval mode and return self."""
        self.module.train(mode)
        return self

    def eval(self) -> DeepLOBModel:
        """Set the underlying module to eval mode and return self."""
        return self.train(False)


def _make_deeplob_module_class() -> Any:
    """Build and return the real ``nn.Module`` subclass for DeepLOB.

    Lazily imports torch. Called from
    :meth:`DeepLOBModel._build_module` each time a new model is built.
    """
    import torch.nn as nn

    class _DeepLOBNet(nn.Module):
        """Inner nn.Module implementing the DeepLOB forward pass."""

        def __init__(
            self,
            conv_stack: nn.Module,
            lstm: nn.Module,
            head: nn.Module,
            hidden_dim: int,
        ) -> None:
            super().__init__()
            self.conv_stack = conv_stack
            self.lstm = lstm
            self.head = head
            self.hidden_dim = hidden_dim

        def forward(self, x: Any) -> Any:
            # x: (batch, seq_len, n_features)
            # Conv1d expects (batch, channels, length). We treat each
            # snapshot's n_features as channels and seq_len as the
            # spatial length: permute to (batch, n_features, seq_len).
            x_perm = x.permute(0, 2, 1)
            conv_out = self.conv_stack(x_perm)
            # conv_out: (batch, hidden_dim, seq_len) -> permute back to
            # (batch, seq_len, hidden_dim) for the LSTM.
            conv_out = conv_out.permute(0, 2, 1)
            # LSTM over the seq_len dimension.
            lstm_out, _ = self.lstm(conv_out)
            # Apply the classification head to every timestep ->
            # (batch, seq_len, n_classes). This gives one prediction
            # per LOB snapshot, matching the per-snapshot label /
            # predict API.
            return self.head(lstm_out)

    return _DeepLOBNet


# ---------------------------------------------------------------------------
# Device resolution
# ---------------------------------------------------------------------------


def _resolve_device(config: DeepLOBConfig, gpu_status: GPUStatus) -> Any:
    """Resolve the torch device for a DeepLOB run.

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
# Metrics + cost-adjusted returns
# ---------------------------------------------------------------------------


def compute_lob_metrics(
    predictions: list[int],
    actuals: list[int],
    n_classes: int = 3,
) -> dict[str, float]:
    """Compute classification metrics for LOB directional prediction.

    Computes accuracy, macro-averaged precision, macro-averaged recall,
    macro-averaged F1, and directional accuracy. Directional accuracy
    treats the problem as binary "did the model get the direction
    right" — a prediction matches the actual direction when both are
    non-stationary with the same sign, or both are stationary.

    Args:
        predictions: Predicted class indices (0=down, 1=stationary,
            2=up).
        actuals: Ground-truth class indices.
        n_classes: Number of classes (used to enumerate classes for
            macro averaging). Must be >= 1.

    Returns:
        A dict with keys ``accuracy``, ``precision``, ``recall``,
        ``f1``, ``directional_accuracy``.

    Raises:
        ValueError: if ``predictions`` and ``actuals`` have different
            lengths, or either is empty, or ``n_classes`` < 1.
    """
    if n_classes < 1:
        raise ValueError(f"n_classes must be >= 1; got {n_classes}")
    if len(predictions) != len(actuals):
        raise ValueError(
            f"predictions and actuals must have the same length; "
            f"got {len(predictions)} vs {len(actuals)}"
        )
    if len(predictions) == 0:
        raise ValueError("predictions and actuals must be non-empty")

    n = len(predictions)
    correct = sum(1 for p, a in zip(predictions, actuals, strict=False) if p == a)
    accuracy = correct / n

    # Macro-averaged precision / recall / f1.
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    for c in range(n_classes):
        tp = sum(1 for p, a in zip(predictions, actuals, strict=False) if p == c and a == c)
        fp = sum(1 for p, a in zip(predictions, actuals, strict=False) if p == c and a != c)
        fn = sum(1 for p, a in zip(predictions, actuals, strict=False) if p != c and a == c)
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
        precisions.append(prec)
        recalls.append(rec)
        f1s.append(f1)
    macro_precision = sum(precisions) / len(precisions)
    macro_recall = sum(recalls) / len(recalls)
    macro_f1 = sum(f1s) / len(f1s)

    # Directional accuracy: a prediction is directionally correct when
    # (a) both prediction and actual are stationary (class 1), or
    # (b) both are non-stationary and on the same side (both < 1 or
    #     both > 1). With the standard 3-class encoding (0=down,
    # 1=stationary, 2=up) this reduces to "prediction == actual" for
    # the directional classes, but we keep the explicit sign logic so
    # it generalizes to n_classes != 3.
    stationary = 1 if n_classes >= 3 else None
    dir_correct = 0
    for p, a in zip(predictions, actuals, strict=False):
        if stationary is not None and p == stationary and a == stationary:
            dir_correct += 1
        elif stationary is not None and (p == stationary or a == stationary):
            # One is stationary, the other is not — directionally
            # wrong.
            continue
        else:
            # Both non-stationary: compare sign relative to the
            # stationary midpoint.
            mid = stationary if stationary is not None else 0
            if (p - mid) * (a - mid) > 0 or p == a:
                dir_correct += 1
    directional_accuracy = dir_correct / n

    return {
        "accuracy": float(accuracy),
        "precision": float(macro_precision),
        "recall": float(macro_recall),
        "f1": float(macro_f1),
        "directional_accuracy": float(directional_accuracy),
    }


def compute_spread_adjusted_return(
    predictions: list[int],
    actuals: list[int],
    spread_bps: float,
) -> float:
    """Compute the net return after spread cost.

    Simulates a directional trading strategy: for each prediction, a
    correct directional prediction earns ``+1`` unit of return, an
    incorrect prediction loses ``-1`` unit. Each trade (regardless of
    correctness) pays the spread cost in basis points, converted to a
    fractional return (``spread_bps / 1e4``).

    The net return is::

        net = (n_correct - n_incorrect) / n - spread_bps / 1e4

    where ``n = len(predictions)``. A higher spread reduces the net
    return; a model with no edge (``n_correct == n_incorrect``) nets
    negative after spread.

    Args:
        predictions: Predicted class indices.
        actuals: Ground-truth class indices.
        spread_bps: Spread cost in basis points (must be >= 0).

    Returns:
        The net return after spread cost.

    Raises:
        ValueError: if lengths differ, either is empty, or
            ``spread_bps`` is negative.
    """
    if spread_bps < 0:
        raise ValueError(f"spread_bps must be >= 0; got {spread_bps}")
    if len(predictions) != len(actuals):
        raise ValueError(
            f"predictions and actuals must have the same length; "
            f"got {len(predictions)} vs {len(actuals)}"
        )
    if len(predictions) == 0:
        raise ValueError("predictions and actuals must be non-empty")

    n = len(predictions)
    n_correct = sum(1 for p, a in zip(predictions, actuals, strict=False) if p == a)
    n_incorrect = n - n_correct
    gross = (n_correct - n_incorrect) / n
    spread_cost = spread_bps / 1e4
    return float(gross - spread_cost)


def compute_fee_adjusted_return(
    predictions: list[int],
    actuals: list[int],
    fee_bps: float,
) -> float:
    """Compute the net return after fee cost.

    Same simulation as :func:`compute_spread_adjusted_return` but the
    per-trade cost is the fee (in basis points) rather than the spread.

    Args:
        predictions: Predicted class indices.
        actuals: Ground-truth class indices.
        fee_bps: Fee cost in basis points (must be >= 0).

    Returns:
        The net return after fee cost.

    Raises:
        ValueError: if lengths differ, either is empty, or ``fee_bps``
            is negative.
    """
    if fee_bps < 0:
        raise ValueError(f"fee_bps must be >= 0; got {fee_bps}")
    if len(predictions) != len(actuals):
        raise ValueError(
            f"predictions and actuals must have the same length; "
            f"got {len(predictions)} vs {len(actuals)}"
        )
    if len(predictions) == 0:
        raise ValueError("predictions and actuals must be non-empty")

    n = len(predictions)
    n_correct = sum(1 for p, a in zip(predictions, actuals, strict=False) if p == a)
    n_incorrect = n - n_correct
    gross = (n_correct - n_incorrect) / n
    fee_cost = fee_bps / 1e4
    return float(gross - fee_cost)


# ---------------------------------------------------------------------------
# Inference latency
# ---------------------------------------------------------------------------


def measure_inference_latency(
    model: DeepLOBModel,
    snapshots: list[list[float]],
    n_warmup: int = 5,
) -> float:
    """Measure the average inference latency in milliseconds.

    Runs ``n_warmup`` warmup forward passes (not timed) followed by
    timed forward passes over the provided snapshots. The snapshots are
    interpreted as a single batch of shape ``(1, seq_len, n_features)``
    where ``seq_len = len(snapshots)`` and
    ``n_features = len(snapshots[0])``.

    Args:
        model: A trained :class:`DeepLOBModel` (in eval mode).
        snapshots: A list of LOB snapshots (each a list of floats).
        n_warmup: Number of warmup (untimed) forward passes.

    Returns:
        The average inference latency in milliseconds over the timed
        runs.

    Raises:
        ValueError: if ``snapshots`` is empty.
    """
    import numpy as np
    import torch

    if not snapshots:
        raise ValueError("snapshots must be non-empty")

    arr = np.array(snapshots, dtype=float)
    if arr.ndim != 2:
        raise ValueError(f"snapshots must be 2-D (seq_len, n_features); got shape {arr.shape}")
    # (1, seq_len, n_features)
    x = torch.from_numpy(arr).float().unsqueeze(0)

    model.eval()
    # Warmup.
    with torch.no_grad():
        for _ in range(max(0, n_warmup)):
            _ = model.forward(x)

    # Timed runs.
    timings: list[float] = []
    with torch.no_grad():
        for _ in range(10):
            start = time.perf_counter()
            _ = model.forward(x)
            timings.append((time.perf_counter() - start) * 1000.0)
    return float(sum(timings) / len(timings))


# ---------------------------------------------------------------------------
# DeepLOBTrainer
# ---------------------------------------------------------------------------


class DeepLOBTrainer:
    """Train / predict / save / load / OOF-write façade for DeepLOB.

    The trainer builds a :class:`DeepLOBModel`, trains it with Adam +
    CrossEntropyLoss on the provided LOB snapshots / labels, computes
    classification + cost-adjusted metrics, measures inference latency,
    saves the trained state_dict, and can write OOF predictions via
    :class:`OOFWriter`.

    Args:
        config: The :class:`DeepLOBConfig` for the run.
        venue: The LOB venue (a :class:`LOBVenue` value string).
        symbol: The instrument symbol.
    """

    def __init__(
        self,
        config: DeepLOBConfig,
        venue: str,
        symbol: str,
    ) -> None:
        if not isinstance(config, DeepLOBConfig):
            raise TypeError("config must be a DeepLOBConfig")
        if venue not in _ALLOWED_VENUES:
            raise ValueError(f"venue must be one of {sorted(_ALLOWED_VENUES)!r}; got {venue!r}")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        self.config = config
        self.venue = venue
        self.symbol = symbol
        self.model_: DeepLOBModel | None = None

    # -- training ---------------------------------------------------------

    def train(
        self,
        snapshots: list[list[float]],
        labels: list[int],
    ) -> DeepLOBTrainingResult:
        """Train a DeepLOB model on ``snapshots`` / ``labels``.

        Args:
            snapshots: A list of LOB snapshots. Each snapshot is a
                flattened list of bid/ask prices/sizes of length
                ``config.n_features``. The full list is treated as one
                sequence of shape ``(seq_len, n_features)`` and a
                single training example. To train on multiple
                sequences, call :meth:`train` once per sequence and
                average, or pre-window the data into multiple
                ``(seq_len, n_features)`` blocks and pass them as a
                list of lists of lists. For the canary, a single
                sequence is sufficient.
            labels: Directional labels (0=down, 1=stationary, 2=up),
                one per snapshot. The model is trained to predict the
                label at each timestep from the snapshot at that
                timestep.

        Returns:
            A :class:`DeepLOBTrainingResult` with epoch losses, GPU
            status, classification metrics, cost-adjusted returns,
            inference latency, and promotion eligibility (always
            ``False`` for shadow runs).
        """
        import numpy as np
        import torch
        import torch.nn as nn

        start = time.perf_counter()

        torch.manual_seed(self.config.seed)
        np.random.seed(self.config.seed)

        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)

        if not snapshots:
            raise ValueError("snapshots must be non-empty")
        if len(snapshots) != len(labels):
            raise ValueError(
                f"snapshots and labels must have the same length; "
                f"got {len(snapshots)} vs {len(labels)}"
            )

        arr = np.array(snapshots, dtype=float)
        if arr.ndim != 2:
            raise ValueError(f"snapshots must be 2-D (seq_len, n_features); got shape {arr.shape}")
        if arr.shape[1] != self.config.n_features:
            raise ValueError(
                f"snapshots.shape[1] must equal n_features="
                f"{self.config.n_features}; got {arr.shape[1]}"
            )
        y_arr = np.array(labels, dtype=np.int64)
        if y_arr.ndim != 1:
            raise ValueError(f"labels must be 1-D; got shape {y_arr.shape}")
        # Validate label range.
        if y_arr.min() < 0 or y_arr.max() >= self.config.n_classes:
            raise ValueError(
                f"labels must be in [0, n_classes="
                f"{self.config.n_classes}); got min={int(y_arr.min())}, "
                f"max={int(y_arr.max())}"
            )

        # (seq_len, n_features) -> (1, seq_len, n_features) batch.
        x_tensor = torch.from_numpy(arr).float().unsqueeze(0)
        # (seq_len,) -> (seq_len, n_classes) for per-timestep CE.
        # We treat each timestep as an independent training example
        # sharing the same conv+LSTM backbone (teacher-forced).
        y_tensor = torch.from_numpy(y_arr).long()

        model = DeepLOBModel(
            n_features=self.config.n_features,
            hidden_dim=self.config.hidden_dim,
            n_conv_layers=self.config.n_conv_layers,
            n_lstm_layers=self.config.n_lstm_layers,
            n_classes=self.config.n_classes,
            dropout=self.config.dropout,
        )
        model.to(device)
        model.train()

        loss_fn = nn.CrossEntropyLoss()
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=self.config.learning_rate,
        )

        epoch_losses: list[float] = []
        final_loss = float("nan")

        n_samples = arr.shape[0]
        if self.config.epochs > 0 and n_samples > 0:
            for _epoch in range(self.config.epochs):
                permutation = torch.randperm(n_samples)
                batch_losses: list[float] = []
                for i in range(0, n_samples, self.config.batch_size):
                    idx = permutation[i : i + self.config.batch_size]
                    if idx.numel() < 1:
                        continue
                    # Build a batch of (batch, seq_len, n_features)
                    # where seq_len is the number of timesteps in this
                    # batch. Each "sample" is one timestep; we feed the
                    # whole batch as one forward pass and take the
                    # logits at the corresponding timesteps.
                    batch_x = x_tensor[:, idx, :].to(device)
                    batch_y = y_tensor[idx].to(device)

                    optimizer.zero_grad()
                    # logits: (1, seq_len, n_classes) -> squeeze the
                    # batch dim and index the timesteps we used.
                    logits = model.forward(batch_x)
                    # logits shape: (1, len(idx), n_classes)
                    logits_at = logits[0]  # (len(idx), n_classes)
                    loss = loss_fn(logits_at, batch_y)
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

        # Compute classification metrics on the training set (eval
        # mode).
        metrics: dict[str, float] = {}
        preds_list: list[int] = []
        actuals_list: list[int] = []
        if n_samples > 0 and self.config.epochs > 0:
            model.eval()
            with torch.no_grad():
                logits = model.forward(x_tensor.to(device))
                logits_at = logits[0].cpu().numpy()
                preds = logits_at.argmax(axis=-1)
                preds_list = [int(v) for v in preds]
                actuals_list = [int(v) for v in y_arr]
            metrics = compute_lob_metrics(
                preds_list,
                actuals_list,
                n_classes=self.config.n_classes,
            )
            metrics["final_loss"] = final_loss

        # Cost-adjusted returns (default spread/fee assumptions for the
        # canary — a liquid US equity venue).
        spread_adjusted: float | None = None
        fee_adjusted: float | None = None
        if preds_list:
            spread_adjusted = compute_spread_adjusted_return(
                preds_list, actuals_list, spread_bps=1.0
            )
            fee_adjusted = compute_fee_adjusted_return(preds_list, actuals_list, fee_bps=0.5)

        # Inference latency.
        latency_ms: float | None = None
        if n_samples > 0:
            try:
                latency_ms = measure_inference_latency(model, snapshots, n_warmup=3)
            except Exception:
                latency_ms = None

        duration = time.perf_counter() - start

        return DeepLOBTrainingResult(
            config=self.config,
            venue=self.venue,
            symbol=self.symbol,
            final_loss=final_loss,
            epoch_losses=epoch_losses,
            gpu_status=gpu_status,
            artifact_path=None,
            oof_artifact_path=None,
            is_shadow=self.config.shadow_only,
            promotion_eligible=False,
            metrics=metrics,
            spread_adjusted_return=spread_adjusted,
            fee_adjusted_return=fee_adjusted,
            latency_ms=latency_ms,
            duration_seconds=duration,
        )

    # -- prediction -------------------------------------------------------

    def predict(self, snapshots: list[list[float]]) -> list[int]:
        """Predict class indices for ``snapshots``.

        Args:
            snapshots: A list of LOB snapshots (each a list of floats
                of length ``config.n_features``).

        Returns:
            A list of predicted class indices (one per snapshot).
        """
        import numpy as np
        import torch

        if self.model_ is None:
            raise ValueError("no trained model available — call train() or load_artifact() first")
        if not snapshots:
            return []

        arr = np.array(snapshots, dtype=float)
        if arr.ndim != 2:
            raise ValueError(f"snapshots must be 2-D (seq_len, n_features); got shape {arr.shape}")
        if arr.shape[1] != self.config.n_features:
            raise ValueError(
                f"snapshots.shape[1] must equal n_features="
                f"{self.config.n_features}; got {arr.shape[1]}"
            )
        x = torch.from_numpy(arr).float().unsqueeze(0)

        model = self.model_
        model.eval()
        gpu_status = check_gpu()
        device = _resolve_device(self.config, gpu_status)
        model.to(device)

        with torch.no_grad():
            logits = model.forward(x.to(device))
            logits_at = logits[0].cpu().numpy()
            preds = logits_at.argmax(axis=-1)
        return [int(v) for v in preds]

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

    def load_artifact(self, path: str) -> DeepLOBModel:
        """Load a state_dict from ``path`` into a new :class:`DeepLOBModel`.

        The new model is built from the trainer's config and the saved
        state_dict is loaded into it. The model is set to eval mode on
        CPU and stored on the trainer (``self.model_``).

        Returns:
            The loaded :class:`DeepLOBModel`.
        """
        import torch

        model = DeepLOBModel(
            n_features=self.config.n_features,
            hidden_dim=self.config.hidden_dim,
            n_conv_layers=self.config.n_conv_layers,
            n_lstm_layers=self.config.n_lstm_layers,
            n_classes=self.config.n_classes,
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
            fold_predictions: Per-row predictions. Must have the same
                length as ``fold_ids``.
            fold_ids: Per-row fold ids.
            timestamps: Per-row ISO-format timestamps.
            labels: Per-row ground-truth labels.
            horizons: Per-row prediction horizons.
            weights: Per-row sample weights. When ``None``, 1.0 is
                used for every row.
            output_path: Path to write the OOF artifact to. The file is
                named ``oof_deeplob.json`` in the parent directory of
                this path (the parent directory is the OOF output dir).

        Returns:
            The path to the written OOF artifact file.
        """
        n = len(fold_predictions)
        if not (
            len(fold_ids) == n and len(timestamps) == n and len(labels) == n and len(horizons) == n
        ):
            raise ValueError(
                "fold_predictions, fold_ids, timestamps, labels, "
                "and horizons must all have the same length"
            )
        if weights is not None and len(weights) != n:
            raise ValueError("weights must have the same length as fold_predictions or be None")

        output_dir = str(Path(output_path).parent)
        writer = OOFWriter(model_family="deeplob", output_dir=output_dir)
        for i in range(n):
            row_id = f"{self.symbol}_{timestamps[i]}_{horizons[i]}_{fold_ids[i]}_{i}"
            w = float(weights[i]) if weights is not None else 1.0
            # Each prediction may be a scalar or a single-element list
            # (e.g. a class probability wrapped in a list). Normalize
            # to a float.
            pred_raw = fold_predictions[i]
            if isinstance(pred_raw, (list, tuple)):
                pred_val = float(pred_raw[0])
            else:
                pred_val = float(pred_raw)
            writer.add_prediction(
                row_id=row_id,
                fold_id=int(fold_ids[i]),
                symbol=self.symbol,
                timestamp=str(timestamps[i]),
                label=float(labels[i]),
                prediction=pred_val,
                horizon=int(horizons[i]),
                weight=w,
            )
        artifact = writer.flush()
        return artifact.artifact_uri


# ---------------------------------------------------------------------------
# Promotion eligibility
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: DeepLOBTrainingResult,
    manual_override: bool = False,
) -> bool:
    """Validate whether a DeepLOB training result is promotion eligible.

    Promotion rules (fail-closed):

    - If ``result.is_shadow`` is ``True`` (the default for DeepLOB), the
      run is **only** eligible when ``manual_override`` is ``True`` —
      i.e. an operator explicitly overrides the shadow gate. A shadow
      run with no override is **not** eligible.
    - If ``result.is_shadow`` is ``False`` (a non-shadow run), the run
      is eligible regardless of ``manual_override``.

    Args:
        result: The :class:`DeepLOBTrainingResult` to validate.
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


def register_lob_family() -> dict[str, Any]:
    """Return a ``ModelFamilySpec``-compatible dict for DeepLOB registration.

    The returned dict carries the fields a
    :class:`~quant_foundry.alpha_genome.ModelFamilySpec` expects
    (family_id, display_name, version, dataset_shape, objectives,
    artifact_format, artifact_loader, required_metrics, etc.) plus
    DeepLOB-specific metadata. It is intended to be passed to
    ``ModelFamilyRegistry.register`` (after wrapping in a
    ``ModelFamilySpec``) by the caller — this function does **not**
    mutate the registry itself, keeping this module file-disjoint from
    ``alpha_genome.py``.

    The spec marks DeepLOB as a shadow family: it is **not** a baseline
    exception, does not require a GPU (the trainer degrades gracefully
    to CPU), and defaults to the ``CHALLENGER`` promotion-eligibility
    class (though the trainer itself forces ``promotion_eligible=False``
    when ``shadow_only=True``).
    """
    return {
        "family_id": "deeplob",
        "display_name": "DeepLOB (shadow canary)",
        "version": "1",
        "dataset_shape": "lob_snapshots",
        "objectives": ("classification",),
        "artifact_format": "torch_state_dict",
        "artifact_loader": "quant_foundry.lob_trainer.DeepLOBTrainer.load_artifact",
        "required_metrics": (
            "accuracy",
            "precision",
            "recall",
            "f1",
            "directional_accuracy",
            "spread_adjusted_return",
            "fee_adjusted_return",
            "latency_ms",
        ),
        "runpod_image": None,
        "requires_gpu": False,
        "max_budget_cents": 0,
        "promotion_eligibility_class": "challenger",
        "is_baseline_exception": False,
        "created_at_ns": time.time_ns(),
        "shadow_only": True,
        "default_n_levels": 10,
        "default_n_features": 40,
        "default_hidden_dim": 64,
        "default_n_conv_layers": 2,
        "default_n_lstm_layers": 1,
        "default_horizon": 10,
        "default_n_classes": 3,
    }


__all__ = [
    "DeepLOBConfig",
    "DeepLOBModel",
    "DeepLOBTrainer",
    "DeepLOBTrainingResult",
    "compute_fee_adjusted_return",
    "compute_lob_metrics",
    "compute_spread_adjusted_return",
    "measure_inference_latency",
    "register_lob_family",
    "validate_promotion_eligibility",
]
