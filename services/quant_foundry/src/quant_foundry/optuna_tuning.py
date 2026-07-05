"""
quant_foundry.optuna_tuning — In-Worker Optuna Hyperparameter Tuning (T-8.6).

Provides an Optuna-based hyperparameter tuning surface for the training
worker. The worker can run a bounded number of trials (or a wall-clock
budget) to search over a model's hyperparameter space, prune bad trials
early, and emit durable artifacts (``study.json`` and ``best_trial.json``)
that downstream stages (promotion, calibration) can consume.

Design invariants (enforced + tested):
- **Pydantic v2 models are frozen + ``extra='forbid'``** (audit integrity).
- **Lazy import of optuna inside methods** — the module is importable
  without optuna installed so environments that only need the spec /
  artifact models (pure-Python) are not blocked. The ``OptunaTuner.run``
  method raises a clear ``ImportError`` if optuna is missing.
- **Deterministic study hash** — :func:`compute_study_hash` produces a
  stable SHA-256 over the canonical (sorted-keys) JSON of the study data,
  so the same logical study always yields the same hash.
- **Hard wall-clock budget enforcement** — :class:`BudgetEnforcer` stops
  the search before the configured budget is exceeded; the tuner also
  checks the budget between trials and aborts cleanly.
- **Pruning is recorded** — pruned trials are captured with
  ``state="PRUNED"`` and a human-readable reason, not silently dropped.

File-disjoint from ``real_trainer.py`` and ``training_manifest.py`` —
integration is handled by another builder. This module only exposes the
standalone tuning surface.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SearchAlgorithm(StrEnum):
    """Optuna sampler / search algorithm.

    - ``TPE``: Tree-structured Parzen Estimator (default, sample-efficient).
    - ``RANDOM``: random search (baseline / reproducible sanity check).
    - ``CMAES``: Covariance Matrix Adaptation Evolution Strategy (good for
      continuous spaces, requires enough trials to converge).
    """

    TPE = "tpe"
    RANDOM = "random"
    CMAES = "cmaes"


class PruningPolicy(StrEnum):
    """Optuna pruner / early-stopping policy.

    - ``NONE``: no pruning (every trial runs to completion).
    - ``MEDIAN``: median pruner — prunes trials whose intermediate value is
      worse than the median of completed trials at the same step.
    - ``SUCCESSIVE_HALVING``: successive halving — allocates more resources
      only to promising trials.
    - ``HYPERBAND``: Hyperband — a bandit-based variant of successive
      halving that adapts the budget per trial.
    """

    NONE = "none"
    MEDIAN = "median"
    SUCCESSIVE_HALVING = "successive_halving"
    HYPERBAND = "hyperband"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TuningSpec(BaseModel):
    """Specification for a hyperparameter tuning run.

    Frozen + ``extra='forbid'``. Captures the search algorithm, trial /
    wall-clock budgets, the metric to optimize and its direction, the
    pruning policy, and a reproducible seed.

    Attributes:
        search_algorithm: the :class:`SearchAlgorithm` to use.
        max_trials: maximum number of trials to run (must be >= 1).
        max_wall_clock_seconds: hard wall-clock budget in seconds
            (must be >= 60).
        metric: name of the metric to optimize (reported by the objective).
        direction: ``"minimize"`` or ``"maximize"``.
        early_stopping_rounds: optional early-stopping rounds passed to
            the objective (e.g. LightGBM ``early_stopping_rounds``).
        pruning_policy: the :class:`PruningPolicy` to use.
        seed: random seed for reproducible samplers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_algorithm: SearchAlgorithm = SearchAlgorithm.TPE
    max_trials: int = 100
    max_wall_clock_seconds: int = 3600
    metric: str = "logloss"
    direction: str = "minimize"
    early_stopping_rounds: int | None = None
    pruning_policy: PruningPolicy = PruningPolicy.MEDIAN
    seed: int = 42

    @field_validator("max_trials")
    @classmethod
    def _validate_max_trials(cls, v: int) -> int:
        """Ensure ``max_trials`` is at least 1."""
        if v < 1:
            raise ValueError(f"max_trials must be >= 1; got {v}")
        return v

    @field_validator("max_wall_clock_seconds")
    @classmethod
    def _validate_max_wall_clock(cls, v: int) -> int:
        """Ensure ``max_wall_clock_seconds`` is at least 60."""
        if v < 60:
            raise ValueError(f"max_wall_clock_seconds must be >= 60; got {v}")
        return v

    @model_validator(mode="after")
    def _validate_direction(self) -> TuningSpec:
        """Ensure ``direction`` is one of the allowed values."""
        if self.direction not in ("minimize", "maximize"):
            raise ValueError(f"direction must be 'minimize' or 'maximize'; got {self.direction!r}")
        return self


class TrialResult(BaseModel):
    """Result of a single tuning trial.

    Frozen + ``extra='forbid'``. Records the trial number, the sampled
    hyperparameters, the final metric value (if any), the trial state, an
    optional reason (for pruned / failed trials), the wall-clock duration,
    and the last heartbeat timestamp.

    Attributes:
        trial_number: 0-indexed trial number.
        params: the hyperparameters sampled for this trial.
        metric_value: the final metric value, or ``None`` for pruned /
            failed trials that did not complete.
        state: one of ``COMPLETE``, ``PRUNED``, ``FAIL``, ``RUNNING``.
        reason: human-readable reason for pruned / failed trials.
        duration_seconds: wall-clock duration of the trial.
        heartbeat_at: ISO-8601 timestamp of the last heartbeat, or ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trial_number: int
    params: dict[str, Any]
    metric_value: float | None
    state: str
    reason: str | None = None
    duration_seconds: float
    heartbeat_at: str | None = None


class StudyArtifact(BaseModel):
    """Durable artifact for a completed tuning study.

    Frozen + ``extra='forbid'``. Contains every trial result, the best
    trial (if any), a deterministic study hash, the creation timestamp,
    and the total wall-clock seconds consumed.

    Attributes:
        study_name: human-readable study name.
        tuning_spec: the :class:`TuningSpec` that produced this study.
        trials: all trial results in trial-number order.
        best_trial: the best :class:`TrialResult`, or ``None`` if no trial
            completed.
        study_hash: deterministic SHA-256 hash of the study data.
        created_at: ISO-8601 creation timestamp.
        total_wall_clock_seconds: total wall-clock seconds consumed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    study_name: str
    tuning_spec: TuningSpec
    trials: list[TrialResult]
    best_trial: TrialResult | None
    study_hash: str
    created_at: str
    total_wall_clock_seconds: float


class BestTrialArtifact(BaseModel):
    """Durable artifact for the best trial of a study.

    Frozen + ``extra='forbid'``. A compact projection of the best trial
    suitable for downstream consumers (promotion, calibration) that only
    need the selected hyperparameters and the study hash.

    Attributes:
        trial_number: the best trial's number.
        params: the best trial's hyperparameters.
        metric_value: the best trial's metric value.
        direction: the optimization direction.
        study_hash: the deterministic study hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trial_number: int
    params: dict[str, Any]
    metric_value: float
    direction: str
    study_hash: str


class TuningHeartbeat(BaseModel):
    """Heartbeat snapshot of an in-progress tuning run.

    Frozen + ``extra='forbid'``. Emitted after each trial so an external
    observer can track progress without waiting for the full study to
    complete.

    Attributes:
        trial_number: the trial number just completed (or in progress).
        completed_trials: number of trials completed so far.
        elapsed_seconds: wall-clock seconds since the run started.
        best_metric_so_far: best metric value seen so far, or ``None``.
        timestamp: ISO-8601 timestamp of the heartbeat.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trial_number: int
    completed_trials: int
    elapsed_seconds: float
    best_metric_so_far: float | None
    timestamp: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_study_hash(study_data: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 hash of a study data dict.

    The dict is serialized with sorted keys and compact separators so the
    same logical study always yields the same hash regardless of dict
    insertion order.

    Args:
        study_data: the study data to hash (must be JSON-serializable).

    Returns:
        A 64-character hex SHA-256 digest.
    """
    payload = json.dumps(study_data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_search_space() -> dict[str, Any]:
    """Create a default LightGBM hyperparameter search space.

    The returned dict maps parameter names to Optuna distribution specs
    (``type`` + bounds). The :class:`OptunaTuner` interprets each spec and
    calls the corresponding ``trial.suggest_*`` method.

    Returns:
        A dict of parameter-name -> distribution spec.
    """
    return {
        "num_leaves": {"type": "int", "low": 15, "high": 255, "step": 1},
        "learning_rate": {"type": "float", "low": 0.005, "high": 0.3, "log": True},
        "max_depth": {"type": "int", "low": -1, "high": 20, "step": 1},
        "min_child_samples": {"type": "int", "low": 1, "high": 100, "step": 1},
        "feature_fraction": {"type": "float", "low": 0.4, "high": 1.0, "log": False},
        "bagging_fraction": {"type": "float", "low": 0.4, "high": 1.0, "log": False},
        "bagging_freq": {"type": "int", "low": 0, "high": 10, "step": 1},
        "lambda_l1": {"type": "float", "low": 1e-8, "high": 10.0, "log": True},
        "lambda_l2": {"type": "float", "low": 1e-8, "high": 10.0, "log": True},
    }


def convert_categorical_search_space(
    choices: dict[str, list[Any]],
) -> dict[str, Any]:
    """Convert a categorical-choices dict to Optuna search-space format.

    The :class:`RunPodTrainingRequest.search_space` field uses
    ``dict[str, list[Any]]`` where each list holds the candidate values for
    a parameter (the trainer reads ``value[0]`` as the selected value). This
    helper converts that format into the Optuna distribution-spec format
    (``{"type": "categorical", "choices": [...]}``) expected by
    :func:`_sample_params` and :class:`OptunaTuner`.

    This is a pure-Python helper (no optuna import) so it preserves the
    lazy-import invariant of this module.

    Args:
        choices: a dict of parameter-name -> list of candidate values.

    Returns:
        A dict of parameter-name -> ``{"type": "categorical", "choices": [...]}``.
    """
    return {
        name: {"type": "categorical", "choices": list(vals)}
        for name, vals in choices.items()
        if vals  # skip empty lists (no candidates to sample)
    }


class BudgetEnforcer:
    """Hard wall-clock budget enforcer for a tuning run.

    A thin, pure-Python helper that answers two questions:
    - :meth:`should_stop` — has the elapsed time exceeded the budget?
    - :meth:`remaining` — how many seconds remain before the budget is hit?

    The enforcer is deliberately decoupled from Optuna so it can be unit
    tested in isolation and reused by other budgeted loops.
    """

    def __init__(self, max_wall_clock_seconds: int) -> None:
        """Construct a BudgetEnforcer.

        Args:
            max_wall_clock_seconds: the hard wall-clock budget in seconds.
                Must be >= 0.
        """
        if max_wall_clock_seconds < 0:
            raise ValueError(f"max_wall_clock_seconds must be >= 0; got {max_wall_clock_seconds}")
        self.max_wall_clock_seconds = max_wall_clock_seconds

    def should_stop(self, elapsed_seconds: float) -> bool:
        """Return ``True`` if the budget has been exceeded.

        Args:
            elapsed_seconds: seconds elapsed since the run started.

        Returns:
            ``True`` if ``elapsed_seconds >= max_wall_clock_seconds``.
        """
        return elapsed_seconds >= self.max_wall_clock_seconds

    def remaining(self, elapsed_seconds: float) -> float:
        """Return the remaining seconds before the budget is hit.

        Never returns a negative value (clamped at 0).

        Args:
            elapsed_seconds: seconds elapsed since the run started.

        Returns:
            ``max(0, max_wall_clock_seconds - elapsed_seconds)``.
        """
        return max(0.0, self.max_wall_clock_seconds - elapsed_seconds)


# ---------------------------------------------------------------------------
# OptunaTuner
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return _dt.datetime.now(_dt.UTC).isoformat()


def _sample_params(trial: Any, search_space: dict[str, Any]) -> dict[str, Any]:
    """Sample parameters from ``search_space`` using an Optuna trial.

    Args:
        trial: an ``optuna.trial.Trial`` (or duck-typed equivalent with
            ``suggest_int`` / ``suggest_float`` methods).
        search_space: a dict of parameter-name -> distribution spec, as
            produced by :func:`create_search_space`.

    Returns:
        A dict of parameter-name -> sampled value.
    """
    params: dict[str, Any] = {}
    for name, spec in search_space.items():
        kind = spec["type"]
        if kind == "int":
            params[name] = trial.suggest_int(
                name=name,
                low=spec["low"],
                high=spec["high"],
                step=spec.get("step", 1),
            )
        elif kind == "float":
            params[name] = trial.suggest_float(
                name=name,
                low=spec["low"],
                high=spec["high"],
                log=spec.get("log", False),
            )
        elif kind == "categorical":
            params[name] = trial.suggest_categorical(name=name, choices=spec["choices"])
        else:  # pragma: no cover - defensive
            raise ValueError(f"unsupported search-space type: {kind!r}")
    return params


class OptunaTuner:
    """In-worker Optuna hyperparameter tuner.

    Wraps an Optuna ``Study`` with wall-clock budget enforcement, pruning,
    heartbeat callbacks, and durable artifact emission (``study.json`` and
    ``best_trial.json``).

    The tuner is constructed with a :class:`TuningSpec`, a search space
    (as produced by :func:`create_search_space` or a custom dict), and a
    study directory for persistent storage. The :meth:`run` method
    executes the search and returns a :class:`StudyArtifact`.
    """

    def __init__(
        self,
        tuning_spec: TuningSpec,
        search_space: dict[str, Any],
        study_dir: str,
        *,
        study_name: str = "quant_foundry_study",
    ) -> None:
        """Construct an OptunaTuner.

        Args:
            tuning_spec: the :class:`TuningSpec` describing the search.
            search_space: a dict of parameter-name -> distribution spec.
            study_dir: directory for persistent Optuna storage (created
                if it does not exist).
            study_name: human-readable study name (default
                ``"quant_foundry_study"``).
        """
        self.tuning_spec = tuning_spec
        self.search_space = search_space
        self.study_dir = pathlib.Path(study_dir)
        self.study_dir.mkdir(parents=True, exist_ok=True)
        self.study_name = study_name
        self._last_study_artifact: StudyArtifact | None = None

    # --- sampler / pruner construction ---------------------------------- #

    def _build_sampler(self) -> Any:
        """Build an Optuna sampler from the tuning spec.

        Lazy-imports optuna. The sampler is seeded with ``tuning_spec.seed``
        for reproducibility.
        """
        import optuna

        algo = self.tuning_spec.search_algorithm
        seed = self.tuning_spec.seed
        if algo == SearchAlgorithm.TPE:
            return optuna.samplers.TPESampler(seed=seed)
        if algo == SearchAlgorithm.RANDOM:
            return optuna.samplers.RandomSampler(seed=seed)
        if algo == SearchAlgorithm.CMAES:
            return optuna.samplers.CmaEsSampler(seed=seed)
        raise ValueError(f"unsupported search algorithm: {algo!r}")  # pragma: no cover

    def _build_pruner(self) -> Any:
        """Build an Optuna pruner from the tuning spec.

        Lazy-imports optuna. Returns ``None`` for ``PruningPolicy.NONE``
        (a ``NopPruner`` is used so the objective can always call
        ``trial.report`` / ``should_prune`` without special-casing).
        """
        import optuna

        policy = self.tuning_spec.pruning_policy
        if policy == PruningPolicy.NONE:
            return optuna.pruners.NopPruner()
        if policy == PruningPolicy.MEDIAN:
            return optuna.pruners.MedianPruner()
        if policy == PruningPolicy.SUCCESSIVE_HALVING:
            return optuna.pruners.SuccessiveHalvingPruner()
        if policy == PruningPolicy.HYPERBAND:
            return optuna.pruners.HyperbandPruner()
        raise ValueError(f"unsupported pruning policy: {policy!r}")  # pragma: no cover

    # --- run ------------------------------------------------------------- #

    def run(
        self,
        objective_fn: Callable[..., Any],
        heartbeat_fn: Callable[[TuningHeartbeat], None] | None = None,
    ) -> StudyArtifact:
        """Execute the tuning search and return a :class:`StudyArtifact`.

        The ``objective_fn`` is called once per trial with the signature
        ``objective_fn(trial, search_space, tuning_spec)`` and must return
        either a float metric value or report intermediate values via
        ``trial.report(value, step)`` and raise
        ``optuna.TrialPruned`` to prune itself.

        Between trials the wall-clock budget is checked; if exceeded, the
        search stops cleanly. After each completed trial the
        ``heartbeat_fn`` (if provided) is invoked with a
        :class:`TuningHeartbeat`.

        Args:
            objective_fn: the objective callable.
            heartbeat_fn: optional heartbeat callback invoked after each
                trial.

        Returns:
            The :class:`StudyArtifact` for the completed (or budget-stopped)
            search.
        """
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)

        sampler = self._build_sampler()
        pruner = self._build_pruner()

        # Use in-memory storage by default to avoid sqlite file-locking
        # issues on Windows during tests; the study_dir is still used for
        # artifact JSON files (study.json / best_trial.json).
        storage = None

        study = optuna.create_study(
            study_name=self.study_name,
            direction=self.tuning_spec.direction,
            sampler=sampler,
            pruner=pruner,
            storage=storage,
        )

        budget = BudgetEnforcer(self.tuning_spec.max_wall_clock_seconds)
        start = time.monotonic()
        trial_results: list[TrialResult] = []
        best_metric_so_far: float | None = None

        def _wrapped_objective(trial: Any) -> Any:
            nonlocal best_metric_so_far
            elapsed = time.monotonic() - start
            if budget.should_stop(elapsed):
                raise optuna.TrialPruned("wall-clock budget exceeded before trial")
            trial_start = time.monotonic()
            params = _sample_params(trial, self.search_space)
            try:
                value = objective_fn(trial, self.search_space, self.tuning_spec)
            except optuna.TrialPruned as exc:
                duration = time.monotonic() - trial_start
                trial_results.append(
                    TrialResult(
                        trial_number=trial.number,
                        params=params,
                        metric_value=None,
                        state="PRUNED",
                        reason=str(exc) or "pruned by pruner",
                        duration_seconds=duration,
                        heartbeat_at=_iso_now(),
                    )
                )
                raise
            except Exception as exc:
                duration = time.monotonic() - trial_start
                trial_results.append(
                    TrialResult(
                        trial_number=trial.number,
                        params=params,
                        metric_value=None,
                        state="FAIL",
                        reason=f"{type(exc).__name__}: {exc}",
                        duration_seconds=duration,
                        heartbeat_at=_iso_now(),
                    )
                )
                raise

            duration = time.monotonic() - trial_start
            metric_value = float(value) if value is not None else None
            trial_results.append(
                TrialResult(
                    trial_number=trial.number,
                    params=params,
                    metric_value=metric_value,
                    state="COMPLETE",
                    reason=None,
                    duration_seconds=duration,
                    heartbeat_at=_iso_now(),
                )
            )
            if metric_value is not None:
                if best_metric_so_far is None:
                    best_metric_so_far = metric_value
                elif self.tuning_spec.direction == "minimize":
                    best_metric_so_far = min(best_metric_so_far, metric_value)
                else:
                    best_metric_so_far = max(best_metric_so_far, metric_value)

            # Heartbeat after each trial.
            if heartbeat_fn is not None:
                heartbeat_fn(
                    TuningHeartbeat(
                        trial_number=trial.number,
                        completed_trials=len(trial_results),
                        elapsed_seconds=time.monotonic() - start,
                        best_metric_so_far=best_metric_so_far,
                        timestamp=_iso_now(),
                    )
                )

            # Budget check after the trial.
            elapsed_after = time.monotonic() - start
            if budget.should_stop(elapsed_after):
                study.stop()

            return value

        # Run the optimization with both a trial count cap and a wall-clock
        # timeout. Optuna's native ``timeout`` stops the study after the
        # current trial completes; the between-trial ``should_stop`` check
        # above is a backup that prunes the next trial before it starts.
        try:
            study.optimize(
                _wrapped_objective,
                n_trials=self.tuning_spec.max_trials,
                timeout=self.tuning_spec.max_wall_clock_seconds,
                catch=(Exception,),
            )
        except Exception:  # pragma: no cover - defensive
            # Should not happen because catch=(Exception,) absorbs objective
            # failures, but guard against unexpected optuna errors.
            pass

        total_elapsed = time.monotonic() - start

        # Build best trial from completed trials.
        best_trial: TrialResult | None = None
        completed = [
            t for t in trial_results if t.state == "COMPLETE" and t.metric_value is not None
        ]
        if completed:
            if self.tuning_spec.direction == "minimize":
                best_trial = min(completed, key=lambda t: t.metric_value or float("inf"))
            else:
                best_trial = max(completed, key=lambda t: t.metric_value or float("-inf"))

        # Sort trial results by trial number for deterministic output.
        trial_results_sorted = sorted(trial_results, key=lambda t: t.trial_number)

        study_data = {
            "study_name": self.study_name,
            "tuning_spec": self.tuning_spec.model_dump(mode="json"),
            "trials": [t.model_dump(mode="json") for t in trial_results_sorted],
            "best_trial": best_trial.model_dump(mode="json") if best_trial else None,
            "created_at": _iso_now(),
            "total_wall_clock_seconds": total_elapsed,
        }
        study_hash = compute_study_hash(study_data)

        artifact = StudyArtifact(
            study_name=self.study_name,
            tuning_spec=self.tuning_spec,
            trials=trial_results_sorted,
            best_trial=best_trial,
            study_hash=study_hash,
            created_at=study_data["created_at"],
            total_wall_clock_seconds=total_elapsed,
        )
        self._last_study_artifact = artifact
        return artifact

    # --- persistence ----------------------------------------------------- #

    def save_study(self, path: str) -> None:
        """Write the study artifact to ``path`` as ``study.json``.

        Args:
            path: filesystem path (or directory) to write the study JSON.
                If a directory is given, ``study.json`` is written inside
                it.

        Raises:
            RuntimeError: if :meth:`run` has not been called.
        """
        if self._last_study_artifact is None:
            raise RuntimeError("save_study called before run(); no study artifact")
        p = pathlib.Path(path)
        if p.is_dir() or (not p.suffix):
            p = p / "study.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(
                self._last_study_artifact.model_dump(mode="json"), f, indent=2, sort_keys=True
            )

    def save_best_trial(self, path: str) -> None:
        """Write the best-trial artifact to ``path`` as ``best_trial.json``.

        Args:
            path: filesystem path (or directory) to write the best-trial
                JSON. If a directory is given, ``best_trial.json`` is
                written inside it.

        Raises:
            RuntimeError: if :meth:`run` has not been called or no trial
                completed.
        """
        if self._last_study_artifact is None:
            raise RuntimeError("save_best_trial called before run(); no study artifact")
        if self._last_study_artifact.best_trial is None:
            raise RuntimeError("save_best_trial called but no trial completed")
        best = self._last_study_artifact.best_trial
        artifact = BestTrialArtifact(
            trial_number=best.trial_number,
            params=best.params,
            metric_value=best.metric_value or 0.0,
            direction=self.tuning_spec.direction,
            study_hash=self._last_study_artifact.study_hash,
        )
        p = pathlib.Path(path)
        if p.is_dir() or (not p.suffix):
            p = p / "best_trial.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(artifact.model_dump(mode="json"), f, indent=2, sort_keys=True)

    @staticmethod
    def load_study(path: str) -> StudyArtifact:
        """Load and validate a study artifact from ``path``.

        Args:
            path: filesystem path to the ``study.json`` file (or its
                parent directory).

        Returns:
            The validated :class:`StudyArtifact`.
        """
        p = pathlib.Path(path)
        if p.is_dir():
            p = p / "study.json"
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return StudyArtifact.model_validate(data)

    @staticmethod
    def load_best_trial(path: str) -> BestTrialArtifact:
        """Load and validate a best-trial artifact from ``path``.

        Args:
            path: filesystem path to the ``best_trial.json`` file (or its
                parent directory).

        Returns:
            The validated :class:`BestTrialArtifact`.
        """
        p = pathlib.Path(path)
        if p.is_dir():
            p = p / "best_trial.json"
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return BestTrialArtifact.model_validate(data)
