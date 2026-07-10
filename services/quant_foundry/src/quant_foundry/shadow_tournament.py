"""
quant_foundry.shadow_tournament — Shadow Tournament (T-11.3).

Runs foundation models (TimesFM, Chronos, Moirai, Lag-LLaM) on the same
registered series dataset, compares them to the tree stack and the sequence
model (PatchTST / TFT), settles predictions against actuals, and keeps
``promotion_eligible=False`` until an explicit policy override.

Design invariants (non-negotiable, fail-closed):

- **Shadow output cannot publish live signal.** Every :class:`TournamentEntry`
  and every :class:`TournamentResult` defaults to ``promotion_eligible=False``.
  :func:`validate_promotion_eligibility` returns ``False`` unless an explicit
  ``manual_policy_override=True`` is supplied by the operator, and
  :func:`validate_no_live_signal` raises ``ValueError`` if any entry is
  promotion-eligible. There is no code path inside this module that flips
  promotion on automatically.
- **Metrics recompute from the forecast artifact.**
  :func:`compute_tournament_metrics` consumes
  :class:`~quant_foundry.forecast_distribution.ForecastDistributionArtifact`
  instances and recomputes ``mse``, ``mae``, ``crps`` (continuous ranked
  probability score) and ``pinball_loss`` from the artifact's quantiles — no
  pre-computed metric values are trusted.
- **Foundation models compared to two baselines.** A model is declared the
  tournament ``winner`` only if it beats *both* the tree-stack baseline and
  the sequence-model baseline on the primary metric (``mse``). Ties and
  losses both yield ``winner=None``.
- **Predictions settled against actuals.** :func:`settle_predictions` pairs
  each forecast with its realized actual and records the error and squared
  error so the settlement is auditable.
- **All Pydantic models are ``frozen=True`` + ``extra='forbid'``** — no
  mutation, no surprise fields.

Public surface:

  - :class:`FoundationModel` (enum)
  - :class:`TournamentEntry`, :class:`TournamentResult`,
    :class:`ShadowScorecard` (Pydantic v2 models)
  - :func:`compute_tournament_metrics`
  - :func:`settle_predictions`
  - :class:`ShadowTournament`
  - :func:`validate_promotion_eligibility`
  - :func:`validate_no_live_signal`
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from quant_foundry.forecast_distribution import (
    ForecastDistributionArtifact,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


# The primary metric used to determine the tournament winner. Lower is
# better (it is an error metric).
_PRIMARY_METRIC: str = "mse"

# All metric names produced by :func:`compute_tournament_metrics`. All are
# error metrics where lower is better.
_METRIC_NAMES: tuple[str, ...] = ("mse", "mae", "crps", "pinball_loss")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class FoundationModel(StrEnum):
    """Foundation time-series models competing in the shadow tournament.

    ``TIMESFM``  — Google TimesFM.
    ``CHRONOS``  — Amazon Chronos.
    ``MOIRAI``   — Salesforce Moirai.
    ``LAG_LLM``  — Lag-LLaM.
    """

    TIMESFM = "timesfm"
    CHRONOS = "chronos"
    MOIRAI = "moirai"
    LAG_LLM = "lag_llm"


# ---------------------------------------------------------------------------
# TournamentEntry
# ---------------------------------------------------------------------------


class TournamentEntry(BaseModel):
    """A single foundation model's entry in a shadow tournament.

    Frozen + extra-forbid. Captures the model, its specific ``model_id``,
    the pinned ``weight_hash`` (from
    :mod:`quant_foundry.foundation_weights`), the forecast artifacts it
    produced, and the recomputed metrics. ``promotion_eligible`` is always
    ``False`` in the shadow tournament — it can only be flipped by an
    explicit operator policy override outside this module.

    Invariants enforced at construction (fail-closed):

    - At least one forecast must be present.
    - ``model_id`` and ``weight_hash`` must be non-empty.
    - ``metrics`` must contain at least the primary metric (``mse``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: FoundationModel
    model_id: str
    weight_hash: str
    forecasts: list[ForecastDistributionArtifact]
    metrics: dict[str, float]
    promotion_eligible: bool = False

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("model_id must be a non-empty string")
        return v

    @field_validator("weight_hash")
    @classmethod
    def _weight_hash_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("weight_hash must be a non-empty string")
        return v

    @field_validator("forecasts")
    @classmethod
    def _forecasts_nonempty(
        cls, v: list[ForecastDistributionArtifact]
    ) -> list[ForecastDistributionArtifact]:
        if not isinstance(v, list) or len(v) < 1:
            raise ValueError("at least one forecast is required")
        return v

    @field_validator("metrics")
    @classmethod
    def _metrics_well_formed(cls, v: dict[str, float]) -> dict[str, float]:
        if not isinstance(v, dict) or not v:
            raise ValueError("metrics must be a non-empty dict")
        if _PRIMARY_METRIC not in v:
            raise ValueError(f"metrics must contain the primary metric {_PRIMARY_METRIC!r}")
        for key, val in v.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("metric names must be non-empty strings")
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ValueError(f"metric {key!r} must be a number")
        return v

    @model_validator(mode="after")
    def _promotion_always_false_in_shadow(self) -> TournamentEntry:
        # Defense in depth: the shadow tournament never marks an entry as
        # promotion-eligible. If someone tries to construct an entry with
        # promotion_eligible=True directly, we reject it (fail-closed).
        if self.promotion_eligible:
            raise ValueError("promotion_eligible must be False in the shadow tournament")
        return self


# ---------------------------------------------------------------------------
# TournamentResult
# ---------------------------------------------------------------------------


class TournamentResult(BaseModel):
    """The settled result of a shadow tournament run.

    Frozen + extra-forbid. Contains one :class:`TournamentEntry` per
    foundation model, the tree-stack and sequence-model baseline metrics,
    the determined winner (if any), and the improvement over the best
    baseline. ``promotion_eligible`` is always ``False`` unless an explicit
    policy override is applied outside this module.

    Invariants enforced at construction (fail-closed):

    - No duplicate foundation models in ``entries``.
    - ``tree_stack_baseline`` and ``sequence_baseline`` must each contain
      the primary metric.
    - ``winner`` / ``winner_improvement`` are consistent (winner set ⇒
      improvement set, and vice versa).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tournament_id: str
    dataset_id: str
    entries: list[TournamentEntry]
    tree_stack_baseline: dict[str, float]
    sequence_baseline: dict[str, float]
    winner: FoundationModel | None = None
    winner_improvement: float | None = None
    settled: bool = False
    created_at: str
    promotion_eligible: bool = False

    @field_validator("tournament_id")
    @classmethod
    def _tournament_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("tournament_id must be a non-empty string")
        return v

    @field_validator("dataset_id")
    @classmethod
    def _dataset_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("dataset_id must be a non-empty string")
        return v

    @field_validator("entries")
    @classmethod
    def _entries_list(cls, v: list[TournamentEntry]) -> list[TournamentEntry]:
        if not isinstance(v, list):
            raise ValueError("entries must be a list")
        return v

    @field_validator("tree_stack_baseline")
    @classmethod
    def _tree_stack_baseline_has_primary(cls, v: dict[str, float]) -> dict[str, float]:
        if not isinstance(v, dict) or not v:
            raise ValueError("tree_stack_baseline must be a non-empty dict")
        if _PRIMARY_METRIC not in v:
            raise ValueError(f"tree_stack_baseline must contain {_PRIMARY_METRIC!r}")
        return v

    @field_validator("sequence_baseline")
    @classmethod
    def _sequence_baseline_has_primary(cls, v: dict[str, float]) -> dict[str, float]:
        if not isinstance(v, dict) or not v:
            raise ValueError("sequence_baseline must be a non-empty dict")
        if _PRIMARY_METRIC not in v:
            raise ValueError(f"sequence_baseline must contain {_PRIMARY_METRIC!r}")
        return v

    @model_validator(mode="after")
    def _no_duplicate_models(self) -> TournamentResult:
        models = [e.model for e in self.entries]
        if len(set(models)) != len(models):
            raise ValueError("duplicate foundation models in entries")
        return self

    @model_validator(mode="after")
    def _winner_improvement_consistency(self) -> TournamentResult:
        if self.winner is not None and self.winner_improvement is None:
            raise ValueError("winner_improvement must be set when winner is set")
        if self.winner is None and self.winner_improvement is not None:
            raise ValueError("winner_improvement must be None when winner is None")
        return self

    @model_validator(mode="after")
    def _promotion_always_false_in_shadow(self) -> TournamentResult:
        # Defense in depth: the tournament-level promotion flag is always
        # False in the shadow tournament. A direct construction with
        # promotion_eligible=True is rejected (fail-closed).
        if self.promotion_eligible:
            raise ValueError("promotion_eligible must be False in the shadow tournament")
        return self


# ---------------------------------------------------------------------------
# ShadowScorecard
# ---------------------------------------------------------------------------


class ShadowScorecard(BaseModel):
    """A single model × metric scorecard comparing a model to the baseline.

    Frozen + extra-forbid. ``improvement`` is ``value - baseline_value``;
    for error metrics a *negative* improvement means the model is better
    than the baseline. ``beats_baseline`` is ``True`` when the model's
    metric value is strictly lower than the baseline value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tournament_id: str
    model: FoundationModel
    metric_name: str
    value: float
    baseline_value: float
    improvement: float
    beats_baseline: bool

    @field_validator("tournament_id")
    @classmethod
    def _tournament_id_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("tournament_id must be a non-empty string")
        return v

    @field_validator("metric_name")
    @classmethod
    def _metric_name_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("metric_name must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    """Return the arithmetic mean of ``values`` (0.0 for empty)."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _pinball_loss_single(actual: float, quantile_value: float, level: float) -> float:
    """Compute the pinball loss for a single quantile level.

    ``pinball = max(level * (actual - q), (level - 1) * (actual - q))``.
    """
    diff = actual - quantile_value
    if diff >= 0.0:
        return level * diff
    return (level - 1.0) * diff


def _crps_single(actual: float, quantiles: dict[float, float]) -> float:
    """Compute the CRPS for a single forecast using the quantile form.

    Uses the closed-form quantile-based CRPS approximation:

        CRPS = (2 / M) * sum_i [ (I(actual <= q_i) - tau_i) * (q_i - actual) ]

    where ``M`` is the number of quantile levels, ``tau_i`` the level, and
    ``q_i`` the forecast quantile value. This is the standard quantile-score
    decomposition of CRPS.
    """
    if not quantiles:
        return 0.0
    total = 0.0
    count = 0
    for level, q_val in quantiles.items():
        indicator = 1.0 if actual <= q_val else 0.0
        total += (indicator - float(level)) * (float(q_val) - actual)
        count += 1
    if count == 0:
        return 0.0
    return (2.0 / count) * total


def compute_tournament_metrics(
    forecasts: list[ForecastDistributionArtifact],
    actuals: list[float],
) -> dict[str, float]:
    """Compute tournament metrics from forecast artifacts and actuals.

    Recomputes four error metrics directly from the
    :class:`~quant_foundry.forecast_distribution.ForecastDistributionArtifact`
    instances — no pre-computed metric values are trusted:

    - ``mse`` — mean squared error of the median point forecast.
    - ``mae`` — mean absolute error of the median point forecast.
    - ``crps`` — continuous ranked probability score (quantile form).
    - ``pinball_loss`` — mean pinball loss across all quantile levels.

    The ``i``-th forecast is paired with the ``i``-th actual. The two lists
    must have equal, non-zero length.

    Args:
        forecasts: Forecast distribution artifacts (one per horizon step).
        actuals: Realized actual values, aligned index-for-index with
            ``forecasts``.

    Returns:
        A dict with keys ``"mse"``, ``"mae"``, ``"crps"``, ``"pinball_loss"``.

    Raises:
        TypeError: if ``forecasts`` is not a list of forecast artifacts or
            ``actuals`` is not a list of numbers.
        ValueError: if the lists are empty or of mismatched length.
    """
    if not isinstance(forecasts, list):
        raise TypeError("forecasts must be a list")
    if not isinstance(actuals, list):
        raise TypeError("actuals must be a list")
    if len(forecasts) == 0:
        raise ValueError("forecasts must not be empty")
    if len(actuals) == 0:
        raise ValueError("actuals must not be empty")
    if len(forecasts) != len(actuals):
        raise ValueError(
            f"forecasts and actuals must have equal length ({len(forecasts)} != {len(actuals)})"
        )
    for f in forecasts:
        if not isinstance(f, ForecastDistributionArtifact):
            raise TypeError("each forecast must be a ForecastDistributionArtifact")
    for a in actuals:
        if not isinstance(a, (int, float)) or isinstance(a, bool):
            raise TypeError("each actual must be a number")

    sq_errors: list[float] = []
    abs_errors: list[float] = []
    crps_values: list[float] = []
    pinball_values: list[float] = []

    for forecast, actual in zip(forecasts, actuals, strict=False):
        actual_f = float(actual)
        point = float(forecast.median)
        err = actual_f - point
        sq_errors.append(err * err)
        abs_errors.append(abs(err))
        crps_values.append(_crps_single(actual_f, forecast.quantiles))
        # Pinball loss averaged across all quantile levels for this forecast.
        if forecast.quantiles:
            pb_per_level = [
                _pinball_loss_single(actual_f, float(qv), float(lvl))
                for lvl, qv in forecast.quantiles.items()
            ]
            pinball_values.append(_mean(pb_per_level))
        else:
            pinball_values.append(0.0)

    return {
        "mse": _mean(sq_errors),
        "mae": _mean(abs_errors),
        "crps": _mean(crps_values),
        "pinball_loss": _mean(pinball_values),
    }


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def settle_predictions(
    forecasts: list[ForecastDistributionArtifact],
    actuals: list[float],
) -> list[dict[str, Any]]:
    """Settle each forecast against its realized actual outcome.

    Pairs the ``i``-th forecast with the ``i``-th actual and records a
    settlement record containing the forecast id, the actual value, the
    signed error (``actual - median``), and the squared error.

    Args:
        forecasts: Forecast distribution artifacts (one per horizon step).
        actuals: Realized actual values, aligned index-for-index with
            ``forecasts``.

    Returns:
        A list of settlement dicts, each with keys ``"forecast_id"``,
        ``"actual"``, ``"error"``, ``"squared_error"``.

    Raises:
        TypeError: if ``forecasts`` is not a list of forecast artifacts or
            ``actuals`` is not a list of numbers.
        ValueError: if the lists are empty or of mismatched length.
    """
    if not isinstance(forecasts, list):
        raise TypeError("forecasts must be a list")
    if not isinstance(actuals, list):
        raise TypeError("actuals must be a list")
    if len(forecasts) == 0:
        raise ValueError("forecasts must not be empty")
    if len(actuals) == 0:
        raise ValueError("actuals must not be empty")
    if len(forecasts) != len(actuals):
        raise ValueError(
            f"forecasts and actuals must have equal length ({len(forecasts)} != {len(actuals)})"
        )
    for f in forecasts:
        if not isinstance(f, ForecastDistributionArtifact):
            raise TypeError("each forecast must be a ForecastDistributionArtifact")
    for a in actuals:
        if not isinstance(a, (int, float)) or isinstance(a, bool):
            raise TypeError("each actual must be a number")

    records: list[dict[str, Any]] = []
    for forecast, actual in zip(forecasts, actuals, strict=False):
        actual_f = float(actual)
        point = float(forecast.median)
        err = actual_f - point
        records.append(
            {
                "forecast_id": forecast.artifact_id,
                "actual": actual_f,
                "error": err,
                "squared_error": err * err,
            }
        )
    return records


# ---------------------------------------------------------------------------
# ShadowTournament
# ---------------------------------------------------------------------------


class ShadowTournament:
    """Orchestrates a shadow tournament across foundation models.

    A :class:`ShadowTournament` is created for a specific
    ``tournament_id`` / ``dataset_id`` pair. Foundation model entries are
    added via :meth:`add_entry`, then :meth:`run` computes metrics for each
    entry, settles predictions against ``actuals``, compares the entries to
    the tree-stack and sequence-model baselines, and determines a winner.

    The tournament is **shadow only**: every entry and the resulting
    :class:`TournamentResult` have ``promotion_eligible=False``. Promotion
    requires an explicit operator policy override via
    :func:`validate_promotion_eligibility`.
    """

    def __init__(self, tournament_id: str, dataset_id: str) -> None:
        """Create a new shadow tournament.

        Args:
            tournament_id: Unique identifier for this tournament run.
            dataset_id: Identifier of the registered series dataset the
                models are evaluated on.

        Raises:
            ValueError: if either id is empty.
        """
        if not isinstance(tournament_id, str) or not tournament_id.strip():
            raise ValueError("tournament_id must be a non-empty string")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id must be a non-empty string")
        self._tournament_id: str = tournament_id
        self._dataset_id: str = dataset_id
        self._entries: list[TournamentEntry] = []

    @property
    def tournament_id(self) -> str:
        """The tournament id (read-only)."""
        return self._tournament_id

    @property
    def dataset_id(self) -> str:
        """The dataset id (read-only)."""
        return self._dataset_id

    @property
    def entries(self) -> list[TournamentEntry]:
        """A copy of the entries added so far (read-only)."""
        return list(self._entries)

    def add_entry(
        self,
        model: FoundationModel,
        model_id: str,
        weight_hash: str,
        forecasts: list[ForecastDistributionArtifact],
    ) -> TournamentEntry:
        """Add a foundation model entry to the tournament.

        The entry is created with ``promotion_eligible=False`` (always, in
        the shadow tournament). Metrics are *not* computed here — they are
        computed during :meth:`run` once actuals are available. The entry
        is stored with a placeholder metrics dict containing only the
        primary metric set to ``0.0``; :meth:`run` returns a result with
        the fully computed metrics.

        Args:
            model: The foundation model enum value.
            model_id: Specific model identifier (e.g. ``"chronos-base"``).
            weight_hash: SHA-256 weight hash from
                :mod:`quant_foundry.foundation_weights`.
            forecasts: Forecast artifacts produced by this model.

        Returns:
            The created :class:`TournamentEntry` (with placeholder metrics).

        Raises:
            TypeError: if ``model`` is not a :class:`FoundationModel`.
            ValueError: if any field is invalid or the model is already
                entered.
        """
        if not isinstance(model, FoundationModel):
            raise TypeError("model must be a FoundationModel")
        # Reject duplicate models — one entry per foundation model.
        for existing in self._entries:
            if existing.model == model:
                raise ValueError(f"model {model!r} already has an entry in this tournament")
        entry = TournamentEntry(
            model=model,
            model_id=model_id,
            weight_hash=weight_hash,
            forecasts=list(forecasts),
            metrics={_PRIMARY_METRIC: 0.0},
            promotion_eligible=False,
        )
        self._entries.append(entry)
        return entry

    def run(
        self,
        actuals: list[float],
        tree_stack_metrics: dict[str, float],
        sequence_metrics: dict[str, float],
    ) -> TournamentResult:
        """Run the tournament: compute metrics, settle, determine winner.

        For each entry, recomputes metrics from its forecast artifacts
        against ``actuals``, settles the predictions, and compares the
        entry's primary metric to both baselines. A winner is declared only
        if a model's primary metric is *strictly lower* than both the
        tree-stack and sequence-model baselines. The winner is the model
        with the lowest primary metric among those that beat both
        baselines. Ties (exact equality with a baseline) do not count as
        beating.

        The returned :class:`TournamentResult` always has
        ``promotion_eligible=False`` and ``settled=True``.

        Args:
            actuals: Realized actual values aligned with each entry's
                forecasts. All entries must have the same number of
                forecasts as ``len(actuals)``.
            tree_stack_metrics: Metrics from the tree-stack ensemble
                (must contain ``"mse"``).
            sequence_metrics: Metrics from the sequence model
                (PatchTST / TFT; must contain ``"mse"``).

        Returns:
            A settled :class:`TournamentResult`.

        Raises:
            ValueError: if no entries have been added, if an entry's
                forecast count does not match ``len(actuals)``, or if a
                baseline is missing the primary metric.
        """
        if not self._entries:
            raise ValueError("no entries have been added to the tournament")
        if not isinstance(actuals, list):
            raise TypeError("actuals must be a list")
        if len(actuals) == 0:
            raise ValueError("actuals must not be empty")
        if not isinstance(tree_stack_metrics, dict) or not tree_stack_metrics:
            raise ValueError("tree_stack_metrics must be a non-empty dict")
        if not isinstance(sequence_metrics, dict) or not sequence_metrics:
            raise ValueError("sequence_metrics must be a non-empty dict")
        if _PRIMARY_METRIC not in tree_stack_metrics:
            raise ValueError(f"tree_stack_metrics must contain {_PRIMARY_METRIC!r}")
        if _PRIMARY_METRIC not in sequence_metrics:
            raise ValueError(f"sequence_metrics must contain {_PRIMARY_METRIC!r}")

        # Compute metrics for each entry and build settled entries.
        settled_entries: list[TournamentEntry] = []
        for entry in self._entries:
            if len(entry.forecasts) != len(actuals):
                raise ValueError(
                    f"entry {entry.model!r} has {len(entry.forecasts)} "
                    f"forecasts but {len(actuals)} actuals were provided"
                )
            metrics = compute_tournament_metrics(entry.forecasts, actuals)
            settled_entries.append(
                TournamentEntry(
                    model=entry.model,
                    model_id=entry.model_id,
                    weight_hash=entry.weight_hash,
                    forecasts=entry.forecasts,
                    metrics=metrics,
                    promotion_eligible=False,
                )
            )

        # Settle predictions (used for audit; recorded implicitly via the
        # settled flag on the result).
        for entry in settled_entries:
            settle_predictions(entry.forecasts, actuals)

        # Determine winner: model must beat both baselines on the primary
        # metric (strictly lower). Among those that beat both, the one with
        # the lowest primary metric wins.
        tree_primary = float(tree_stack_metrics[_PRIMARY_METRIC])
        seq_primary = float(sequence_metrics[_PRIMARY_METRIC])
        best_baseline = min(tree_primary, seq_primary)

        candidates: list[tuple[FoundationModel, float]] = []
        for entry in settled_entries:
            val = float(entry.metrics[_PRIMARY_METRIC])
            if val < tree_primary and val < seq_primary:
                candidates.append((entry.model, val))

        winner: FoundationModel | None = None
        winner_improvement: float | None = None
        if candidates:
            # Lowest primary metric among candidates.
            candidates.sort(key=lambda c: c[1])
            winner = candidates[0][0]
            winner_improvement = candidates[0][1] - best_baseline

        result = TournamentResult(
            tournament_id=self._tournament_id,
            dataset_id=self._dataset_id,
            entries=settled_entries,
            tree_stack_baseline=dict(tree_stack_metrics),
            sequence_baseline=dict(sequence_metrics),
            winner=winner,
            winner_improvement=winner_improvement,
            settled=True,
            created_at=_now_iso(),
            promotion_eligible=False,
        )
        return result

    def generate_scorecards(self, result: TournamentResult) -> list[ShadowScorecard]:
        """Generate per-model, per-metric scorecards from a tournament result.

        For each entry and each metric present in the entry's ``metrics``,
        produces a :class:`ShadowScorecard` comparing the model's metric
        value to the *best* (lowest) baseline value for that metric across
        the tree-stack and sequence-model baselines. ``improvement`` is
        ``value - baseline_value`` (negative is better for error metrics),
        and ``beats_baseline`` is ``True`` when the model's value is
        strictly lower than the baseline value.

        Args:
            result: A settled :class:`TournamentResult`.

        Returns:
            A list of :class:`ShadowScorecard` instances.

        Raises:
            TypeError: if ``result`` is not a :class:`TournamentResult`.
        """
        if not isinstance(result, TournamentResult):
            raise TypeError("result must be a TournamentResult")

        scorecards: list[ShadowScorecard] = []
        for entry in result.entries:
            for metric_name, value in entry.metrics.items():
                # Best baseline = lowest value for this metric (error
                # metrics, lower is better).
                tree_val = result.tree_stack_baseline.get(metric_name)
                seq_val = result.sequence_baseline.get(metric_name)
                if tree_val is not None and seq_val is not None:
                    baseline_value = min(float(tree_val), float(seq_val))
                elif tree_val is not None:
                    baseline_value = float(tree_val)
                elif seq_val is not None:
                    baseline_value = float(seq_val)
                else:
                    # Metric not in either baseline: skip (cannot compare).
                    continue
                value_f = float(value)
                improvement = value_f - baseline_value
                beats = value_f < baseline_value
                scorecards.append(
                    ShadowScorecard(
                        tournament_id=result.tournament_id,
                        model=entry.model,
                        metric_name=metric_name,
                        value=value_f,
                        baseline_value=baseline_value,
                        improvement=improvement,
                        beats_baseline=beats,
                    )
                )
        return scorecards

    # -- persistence ---------------------------------------------------

    def save_result(self, result: TournamentResult, path: str) -> None:
        """Serialize ``result`` to a JSON file at ``path``.

        The result is written as pretty-printed JSON with sorted keys.
        Forecast quantile keys are rendered as strings (JSON keys are
        always strings) and restored to floats on load.

        Args:
            result: The tournament result to persist.
            path: File path to write. Parent directories are created.

        Raises:
            TypeError: if ``result`` is not a :class:`TournamentResult`.
            ValueError: if ``path`` is empty.
        """
        if not isinstance(result, TournamentResult):
            raise TypeError("result must be a TournamentResult")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        payload = result.model_dump(mode="json")
        # Render quantile keys as strings for JSON.
        for entry in payload.get("entries", []):
            for fc in entry.get("forecasts", []):
                if isinstance(fc.get("quantiles"), dict):
                    fc["quantiles"] = {str(float(k)): v for k, v in fc["quantiles"].items()}
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, default=str)

    def load_result(self, path: str) -> TournamentResult:
        """Load and validate a :class:`TournamentResult` from ``path``.

        Args:
            path: Path to a previously written result JSON file.

        Returns:
            A validated :class:`TournamentResult`.

        Raises:
            ValueError: if ``path`` is empty or the file does not exist.
            ValidationError: if the JSON does not satisfy the result schema.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if not os.path.exists(path):
            raise ValueError(f"result file not found: {path!r}")
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        # Coerce string quantile keys back to floats.
        for entry in raw.get("entries", []):
            for fc in entry.get("forecasts", []):
                if isinstance(fc.get("quantiles"), dict):
                    fc["quantiles"] = {float(k): v for k, v in fc["quantiles"].items()}
        return TournamentResult.model_validate(raw)


# ---------------------------------------------------------------------------
# Promotion / live-signal gates
# ---------------------------------------------------------------------------


def validate_promotion_eligibility(
    result: TournamentResult,
    manual_policy_override: bool = False,
) -> bool:
    """Determine whether a tournament result is eligible for promotion.

    **Fail-closed: shadow output cannot publish live signal.** Without an
    explicit operator policy override, this function *always* returns
    ``False`` — the shadow tournament never auto-promotes. With
    ``manual_policy_override=True``, it returns ``True``, recording that an
    explicit policy change was required.

    Args:
        result: The tournament result to check.
        manual_policy_override: Must be explicitly set to ``True`` by the
            operator to allow promotion. Defaults to ``False``.

    Returns:
        ``False`` if no override (always, fail-closed). ``True`` if the
        operator explicitly passed ``manual_policy_override=True``.

    Raises:
        TypeError: if ``result`` is not a :class:`TournamentResult`.
    """
    if not isinstance(result, TournamentResult):
        raise TypeError("result must be a TournamentResult")
    if not manual_policy_override:
        return False
    return True


def validate_no_live_signal(result: TournamentResult) -> bool:
    """Verify that no live signal can be published from a tournament result.

    Checks that:

    - No entry in ``result`` has ``promotion_eligible=True``.
    - ``result.promotion_eligible`` is ``False``.

    Returns ``True`` if no live signal is possible (the safe, expected
    state for a shadow tournament). Raises ``ValueError`` if any entry is
    promotion-eligible or if the result itself is promotion-eligible
    (fail-closed).

    Args:
        result: The tournament result to check.

    Returns:
        ``True`` if no live signal can be published.

    Raises:
        TypeError: if ``result`` is not a :class:`TournamentResult`.
        ValueError: if any entry has ``promotion_eligible=True`` or the
            result has ``promotion_eligible=True``.
    """
    if not isinstance(result, TournamentResult):
        raise TypeError("result must be a TournamentResult")
    if result.promotion_eligible:
        raise ValueError("result.promotion_eligible is True — live signal possible (fail-closed)")
    for entry in result.entries:
        if entry.promotion_eligible:
            raise ValueError(
                f"entry {entry.model!r} has promotion_eligible=True — "
                f"live signal possible (fail-closed)"
            )
    return True
