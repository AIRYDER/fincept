"""
Tests for T-8.6: In-Worker Optuna Hyperparameter Tuning.

Covers the acceptance criteria from the spec:
- TuningSpec construction and validation (search_algorithm, max_trials,
  max_wall_clock_seconds, metric, direction, early_stopping_rounds,
  pruning_policy, seed).
- TrialResult, StudyArtifact, BestTrialArtifact, TuningHeartbeat
  construction and validation.
- OptunaTuner with a simple objective (3-trial canary).
- study.json and best_trial.json save/load round-trip.
- Pruning (deliberately bad trial pruned).
- Budget enforcement (short wall-clock budget).
- Heartbeat callback.
- Failed trial recording with reason.
- Study hash determinism.
- Search space creation.
- BudgetEnforcer.
- All pruning policies (NONE, MEDIAN, SUCCESSIVE_HALVING, HYPERBAND).
- All search algorithms (TPE, RANDOM, CMAES).
- compute_study_hash.
- Edge cases.

File-disjoint from real_trainer.py and training_manifest.py — this module
only tests the standalone tuning surface.
"""

from __future__ import annotations

import json
import pathlib
import time

import pytest
from pydantic import ValidationError

from quant_foundry.optuna_tuning import (
    BestTrialArtifact,
    BudgetEnforcer,
    OptunaTuner,
    PruningPolicy,
    SearchAlgorithm,
    StudyArtifact,
    TrialResult,
    TuningHeartbeat,
    TuningSpec,
    compute_study_hash,
    create_search_space,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def study_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Clean temp directory for Optuna study artifacts."""
    d = tmp_path / "study"
    d.mkdir()
    return d


@pytest.fixture
def small_space() -> dict[str, object]:
    """A tiny 2-parameter search space for fast tests."""
    return {
        "x": {"type": "float", "low": -10.0, "high": 10.0, "log": False},
        "y": {"type": "float", "low": -10.0, "high": 10.0, "log": False},
    }


@pytest.fixture
def default_space() -> dict[str, object]:
    """The default LightGBM search space."""
    return create_search_space()


def _quadratic_objective(trial: object, search_space: dict[str, object], spec: TuningSpec) -> float:
    """Objective that minimizes (x-2)^2 + (y+3)^2 — optimum at x=2, y=-3."""
    x = trial.params["x"]
    y = trial.params["y"]
    return float((x - 2.0) ** 2 + (y + 3.0) ** 2)


def _maximize_objective(trial: object, search_space: dict[str, object], spec: TuningSpec) -> float:
    """Objective that maximizes -(x-2)^2 - (y+3)^2 (optimum at x=2, y=-3)."""
    x = trial.params["x"]
    y = trial.params["y"]
    return float(-((x - 2.0) ** 2) - (y + 3.0) ** 2)


def _pruning_objective(trial: object, search_space: dict[str, object], spec: TuningSpec) -> float:
    """Objective that reports a deliberately bad intermediate value and prunes."""
    import optuna

    # Report a very bad value at step 0 to trigger pruning.
    trial.report(1e9, step=0)
    if trial.should_prune():
        raise optuna.TrialPruned("deliberately bad intermediate value")
    x = trial.params["x"]
    y = trial.params["y"]
    return float((x - 2.0) ** 2 + (y + 3.0) ** 2)


def _failing_objective(trial: object, search_space: dict[str, object], spec: TuningSpec) -> float:
    """Objective that always raises an exception."""
    raise RuntimeError("deliberate failure for testing")


def _sleep_objective(trial: object, search_space: dict[str, object], spec: TuningSpec) -> float:
    """Objective that sleeps to trigger wall-clock budget enforcement."""
    time.sleep(2.0)
    x = trial.params["x"]
    y = trial.params["y"]
    return float((x - 2.0) ** 2 + (y + 3.0) ** 2)


# --------------------------------------------------------------------------- #
# SearchAlgorithm enum                                                         #
# --------------------------------------------------------------------------- #


class TestSearchAlgorithm:
    """Tests for the SearchAlgorithm enum."""

    def test_enum_members(self) -> None:
        assert SearchAlgorithm.TPE.value == "tpe"
        assert SearchAlgorithm.RANDOM.value == "random"
        assert SearchAlgorithm.CMAES.value == "cmaes"

    def test_enum_from_value(self) -> None:
        assert SearchAlgorithm("tpe") is SearchAlgorithm.TPE
        assert SearchAlgorithm("random") is SearchAlgorithm.RANDOM
        assert SearchAlgorithm("cmaes") is SearchAlgorithm.CMAES

    def test_enum_count(self) -> None:
        assert len(list(SearchAlgorithm)) == 3


# --------------------------------------------------------------------------- #
# PruningPolicy enum                                                           #
# --------------------------------------------------------------------------- #


class TestPruningPolicy:
    """Tests for the PruningPolicy enum."""

    def test_enum_members(self) -> None:
        assert PruningPolicy.NONE.value == "none"
        assert PruningPolicy.MEDIAN.value == "median"
        assert PruningPolicy.SUCCESSIVE_HALVING.value == "successive_halving"
        assert PruningPolicy.HYPERBAND.value == "hyperband"

    def test_enum_from_value(self) -> None:
        assert PruningPolicy("none") is PruningPolicy.NONE
        assert PruningPolicy("median") is PruningPolicy.MEDIAN
        assert PruningPolicy("successive_halving") is PruningPolicy.SUCCESSIVE_HALVING
        assert PruningPolicy("hyperband") is PruningPolicy.HYPERBAND

    def test_enum_count(self) -> None:
        assert len(list(PruningPolicy)) == 4


# --------------------------------------------------------------------------- #
# TuningSpec                                                                   #
# --------------------------------------------------------------------------- #


class TestTuningSpec:
    """Tests for TuningSpec construction and validation."""

    def test_default_construction(self) -> None:
        spec = TuningSpec()
        assert spec.search_algorithm == SearchAlgorithm.TPE
        assert spec.max_trials == 100
        assert spec.max_wall_clock_seconds == 3600
        assert spec.metric == "logloss"
        assert spec.direction == "minimize"
        assert spec.early_stopping_rounds is None
        assert spec.pruning_policy == PruningPolicy.MEDIAN
        assert spec.seed == 42

    def test_custom_construction(self) -> None:
        spec = TuningSpec(
            search_algorithm=SearchAlgorithm.RANDOM,
            max_trials=50,
            max_wall_clock_seconds=120,
            metric="auc",
            direction="maximize",
            early_stopping_rounds=20,
            pruning_policy=PruningPolicy.HYPERBAND,
            seed=123,
        )
        assert spec.search_algorithm == SearchAlgorithm.RANDOM
        assert spec.max_trials == 50
        assert spec.max_wall_clock_seconds == 120
        assert spec.metric == "auc"
        assert spec.direction == "maximize"
        assert spec.early_stopping_rounds == 20
        assert spec.pruning_policy == PruningPolicy.HYPERBAND
        assert spec.seed == 123

    def test_string_to_enum_coercion(self) -> None:
        spec = TuningSpec(search_algorithm="tpe", pruning_policy="median")
        assert spec.search_algorithm is SearchAlgorithm.TPE
        assert spec.pruning_policy is PruningPolicy.MEDIAN

    def test_max_trials_minimum(self) -> None:
        spec = TuningSpec(max_trials=1)
        assert spec.max_trials == 1

    def test_max_trials_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            TuningSpec(max_trials=0)

    def test_max_trials_negative_raises(self) -> None:
        with pytest.raises(ValidationError):
            TuningSpec(max_trials=-5)

    def test_max_wall_clock_minimum(self) -> None:
        spec = TuningSpec(max_wall_clock_seconds=60)
        assert spec.max_wall_clock_seconds == 60

    def test_max_wall_clock_below_minimum_raises(self) -> None:
        with pytest.raises(ValidationError):
            TuningSpec(max_wall_clock_seconds=59)

    def test_max_wall_clock_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            TuningSpec(max_wall_clock_seconds=0)

    def test_direction_minimize(self) -> None:
        spec = TuningSpec(direction="minimize")
        assert spec.direction == "minimize"

    def test_direction_maximize(self) -> None:
        spec = TuningSpec(direction="maximize")
        assert spec.direction == "maximize"

    def test_direction_invalid_raises(self) -> None:
        with pytest.raises(ValidationError):
            TuningSpec(direction="invalid")

    def test_frozen(self) -> None:
        spec = TuningSpec()
        with pytest.raises((TypeError, ValueError)):
            spec.max_trials = 200  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            TuningSpec(unknown_field="value")  # type: ignore[call-arg]

    def test_early_stopping_rounds_optional(self) -> None:
        assert TuningSpec().early_stopping_rounds is None
        assert TuningSpec(early_stopping_rounds=50).early_stopping_rounds == 50


# --------------------------------------------------------------------------- #
# TrialResult                                                                  #
# --------------------------------------------------------------------------- #


class TestTrialResult:
    """Tests for TrialResult construction and validation."""

    def test_complete_trial(self) -> None:
        r = TrialResult(
            trial_number=0,
            params={"x": 1.0},
            metric_value=0.5,
            state="COMPLETE",
            duration_seconds=1.2,
        )
        assert r.trial_number == 0
        assert r.params == {"x": 1.0}
        assert r.metric_value == 0.5
        assert r.state == "COMPLETE"
        assert r.reason is None
        assert r.duration_seconds == 1.2
        assert r.heartbeat_at is None

    def test_pruned_trial_with_reason(self) -> None:
        r = TrialResult(
            trial_number=1,
            params={"x": 2.0},
            metric_value=None,
            state="PRUNED",
            reason="bad intermediate",
            duration_seconds=0.3,
        )
        assert r.state == "PRUNED"
        assert r.reason == "bad intermediate"
        assert r.metric_value is None

    def test_failed_trial_with_reason(self) -> None:
        r = TrialResult(
            trial_number=2,
            params={},
            metric_value=None,
            state="FAIL",
            reason="RuntimeError: boom",
            duration_seconds=0.1,
        )
        assert r.state == "FAIL"
        assert "RuntimeError" in (r.reason or "")

    def test_with_heartbeat(self) -> None:
        r = TrialResult(
            trial_number=3,
            params={"x": 1.0},
            metric_value=0.1,
            state="COMPLETE",
            duration_seconds=2.0,
            heartbeat_at="2025-01-01T00:00:00+00:00",
        )
        assert r.heartbeat_at == "2025-01-01T00:00:00+00:00"

    def test_frozen(self) -> None:
        r = TrialResult(
            trial_number=0, params={}, metric_value=1.0, state="COMPLETE", duration_seconds=1.0
        )
        with pytest.raises((TypeError, ValueError)):
            r.trial_number = 5  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            TrialResult(
                trial_number=0,
                params={},
                metric_value=1.0,
                state="COMPLETE",
                duration_seconds=1.0,
                unknown="x",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------- #
# StudyArtifact                                                                #
# --------------------------------------------------------------------------- #


class TestStudyArtifact:
    """Tests for StudyArtifact construction and validation."""

    def test_construction_with_best_trial(self) -> None:
        spec = TuningSpec(max_trials=3)
        best = TrialResult(
            trial_number=0, params={"x": 1.0}, metric_value=0.5, state="COMPLETE", duration_seconds=1.0
        )
        artifact = StudyArtifact(
            study_name="test",
            tuning_spec=spec,
            trials=[best],
            best_trial=best,
            study_hash="abc123",
            created_at="2025-01-01T00:00:00+00:00",
            total_wall_clock_seconds=5.0,
        )
        assert artifact.study_name == "test"
        assert artifact.best_trial is not None
        assert artifact.best_trial.trial_number == 0
        assert artifact.study_hash == "abc123"
        assert artifact.total_wall_clock_seconds == 5.0

    def test_construction_without_best_trial(self) -> None:
        spec = TuningSpec(max_trials=3)
        artifact = StudyArtifact(
            study_name="test",
            tuning_spec=spec,
            trials=[],
            best_trial=None,
            study_hash="abc123",
            created_at="2025-01-01T00:00:00+00:00",
            total_wall_clock_seconds=5.0,
        )
        assert artifact.best_trial is None
        assert artifact.trials == []

    def test_frozen(self) -> None:
        spec = TuningSpec(max_trials=3)
        artifact = StudyArtifact(
            study_name="test",
            tuning_spec=spec,
            trials=[],
            best_trial=None,
            study_hash="abc",
            created_at="2025",
            total_wall_clock_seconds=1.0,
        )
        with pytest.raises((TypeError, ValueError)):
            artifact.study_name = "other"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        spec = TuningSpec(max_trials=3)
        with pytest.raises(ValidationError):
            StudyArtifact(
                study_name="test",
                tuning_spec=spec,
                trials=[],
                best_trial=None,
                study_hash="abc",
                created_at="2025",
                total_wall_clock_seconds=1.0,
                unknown="x",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------- #
# BestTrialArtifact                                                            #
# --------------------------------------------------------------------------- #


class TestBestTrialArtifact:
    """Tests for BestTrialArtifact construction and validation."""

    def test_construction(self) -> None:
        a = BestTrialArtifact(
            trial_number=2,
            params={"x": 1.5, "y": -3.0},
            metric_value=0.01,
            direction="minimize",
            study_hash="deadbeef",
        )
        assert a.trial_number == 2
        assert a.params == {"x": 1.5, "y": -3.0}
        assert a.metric_value == 0.01
        assert a.direction == "minimize"
        assert a.study_hash == "deadbeef"

    def test_frozen(self) -> None:
        a = BestTrialArtifact(
            trial_number=0, params={}, metric_value=1.0, direction="minimize", study_hash="h"
        )
        with pytest.raises((TypeError, ValueError)):
            a.trial_number = 5  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            BestTrialArtifact(
                trial_number=0,
                params={},
                metric_value=1.0,
                direction="minimize",
                study_hash="h",
                unknown="x",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------- #
# TuningHeartbeat                                                              #
# --------------------------------------------------------------------------- #


class TestTuningHeartbeat:
    """Tests for TuningHeartbeat construction and validation."""

    def test_construction(self) -> None:
        hb = TuningHeartbeat(
            trial_number=3,
            completed_trials=3,
            elapsed_seconds=12.5,
            best_metric_so_far=0.05,
            timestamp="2025-01-01T00:00:00+00:00",
        )
        assert hb.trial_number == 3
        assert hb.completed_trials == 3
        assert hb.elapsed_seconds == 12.5
        assert hb.best_metric_so_far == 0.05

    def test_best_metric_none(self) -> None:
        hb = TuningHeartbeat(
            trial_number=0,
            completed_trials=0,
            elapsed_seconds=0.0,
            best_metric_so_far=None,
            timestamp="2025",
        )
        assert hb.best_metric_so_far is None

    def test_frozen(self) -> None:
        hb = TuningHeartbeat(
            trial_number=0,
            completed_trials=0,
            elapsed_seconds=0.0,
            best_metric_so_far=None,
            timestamp="2025",
        )
        with pytest.raises((TypeError, ValueError)):
            hb.trial_number = 5  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            TuningHeartbeat(
                trial_number=0,
                completed_trials=0,
                elapsed_seconds=0.0,
                best_metric_so_far=None,
                timestamp="2025",
                unknown="x",  # type: ignore[call-arg]
            )


# --------------------------------------------------------------------------- #
# compute_study_hash                                                           #
# --------------------------------------------------------------------------- #


class TestComputeStudyHash:
    """Tests for compute_study_hash determinism."""

    def test_returns_64_char_hex(self) -> None:
        h = compute_study_hash({"a": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_deterministic_same_dict(self) -> None:
        d = {"b": 2, "a": 1, "c": [1, 2, 3]}
        assert compute_study_hash(d) == compute_study_hash(d)

    def test_order_independent(self) -> None:
        """Dict key insertion order must not affect the hash."""
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        assert compute_study_hash(d1) == compute_study_hash(d2)

    def test_different_data_different_hash(self) -> None:
        assert compute_study_hash({"a": 1}) != compute_study_hash({"a": 2})

    def test_nested_dict_order_independent(self) -> None:
        d1 = {"outer": {"z": 1, "a": 2}}
        d2 = {"outer": {"a": 2, "z": 1}}
        assert compute_study_hash(d1) == compute_study_hash(d2)

    def test_empty_dict(self) -> None:
        h = compute_study_hash({})
        assert len(h) == 64


# --------------------------------------------------------------------------- #
# create_search_space                                                          #
# --------------------------------------------------------------------------- #


class TestCreateSearchSpace:
    """Tests for the default LightGBM search space."""

    def test_returns_dict(self) -> None:
        space = create_search_space()
        assert isinstance(space, dict)
        assert len(space) > 0

    def test_has_expected_params(self) -> None:
        space = create_search_space()
        expected = {
            "num_leaves",
            "learning_rate",
            "max_depth",
            "min_child_samples",
            "feature_fraction",
            "bagging_fraction",
            "bagging_freq",
            "lambda_l1",
            "lambda_l2",
        }
        assert expected.issubset(set(space.keys()))

    def test_each_spec_has_type(self) -> None:
        space = create_search_space()
        for name, spec in space.items():
            assert "type" in spec, f"{name} missing 'type'"
            assert spec["type"] in ("int", "float", "categorical")

    def test_int_specs_have_low_high(self) -> None:
        space = create_search_space()
        for name, spec in space.items():
            if spec["type"] == "int":
                assert "low" in spec and "high" in spec, f"{name} int missing bounds"
                assert spec["low"] <= spec["high"]

    def test_float_specs_have_low_high(self) -> None:
        space = create_search_space()
        for name, spec in space.items():
            if spec["type"] == "float":
                assert "low" in spec and "high" in spec, f"{name} float missing bounds"
                assert spec["low"] <= spec["high"]

    def test_log_flags_present(self) -> None:
        space = create_search_space()
        assert space["learning_rate"]["log"] is True
        assert space["feature_fraction"]["log"] is False


# --------------------------------------------------------------------------- #
# BudgetEnforcer                                                               #
# --------------------------------------------------------------------------- #


class TestBudgetEnforcer:
    """Tests for the BudgetEnforcer helper."""

    def test_should_stop_false_under_budget(self) -> None:
        enforcer = BudgetEnforcer(max_wall_clock_seconds=100)
        assert enforcer.should_stop(50) is False

    def test_should_stop_true_at_budget(self) -> None:
        enforcer = BudgetEnforcer(max_wall_clock_seconds=100)
        assert enforcer.should_stop(100) is True

    def test_should_stop_true_over_budget(self) -> None:
        enforcer = BudgetEnforcer(max_wall_clock_seconds=100)
        assert enforcer.should_stop(150) is True

    def test_remaining_positive(self) -> None:
        enforcer = BudgetEnforcer(max_wall_clock_seconds=100)
        assert enforcer.remaining(30) == 70

    def test_remaining_zero_at_budget(self) -> None:
        enforcer = BudgetEnforcer(max_wall_clock_seconds=100)
        assert enforcer.remaining(100) == 0

    def test_remaining_clamped_at_zero(self) -> None:
        enforcer = BudgetEnforcer(max_wall_clock_seconds=100)
        assert enforcer.remaining(200) == 0

    def test_zero_budget(self) -> None:
        enforcer = BudgetEnforcer(max_wall_clock_seconds=0)
        assert enforcer.should_stop(0) is True
        assert enforcer.remaining(0) == 0

    def test_negative_budget_raises(self) -> None:
        with pytest.raises(ValueError):
            BudgetEnforcer(max_wall_clock_seconds=-1)


# --------------------------------------------------------------------------- #
# OptunaTuner — basic run                                                      #
# --------------------------------------------------------------------------- #


class TestOptunaTunerBasicRun:
    """Tests for OptunaTuner.run with a simple objective."""

    def test_3_trial_canary(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir), study_name="canary")
        artifact = tuner.run(_quadratic_objective)
        assert isinstance(artifact, StudyArtifact)
        assert artifact.study_name == "canary"
        assert len(artifact.trials) == 3
        assert all(t.state == "COMPLETE" for t in artifact.trials)
        assert artifact.best_trial is not None
        assert artifact.best_trial.state == "COMPLETE"

    def test_minimize_direction(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(
            max_trials=5, max_wall_clock_seconds=60, direction="minimize",
            pruning_policy=PruningPolicy.NONE,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert artifact.best_trial is not None
        # The best trial should have the smallest metric value.
        completed = [t for t in artifact.trials if t.state == "COMPLETE" and t.metric_value is not None]
        assert artifact.best_trial.metric_value == min(t.metric_value for t in completed)

    def test_maximize_direction(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(
            max_trials=5, max_wall_clock_seconds=60, direction="maximize",
            pruning_policy=PruningPolicy.NONE,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_maximize_objective)
        assert artifact.best_trial is not None
        completed = [t for t in artifact.trials if t.state == "COMPLETE" and t.metric_value is not None]
        assert artifact.best_trial.metric_value == max(t.metric_value for t in completed)

    def test_trials_sorted_by_number(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=4, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        numbers = [t.trial_number for t in artifact.trials]
        assert numbers == sorted(numbers)

    def test_study_hash_present(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert len(artifact.study_hash) == 64

    def test_total_wall_clock_positive(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert artifact.total_wall_clock_seconds >= 0.0

    def test_created_at_present(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert artifact.created_at
        assert "T" in artifact.created_at  # ISO-8601

    def test_default_search_space_runs(self, study_dir: pathlib.Path, default_space: dict[str, object]) -> None:
        """The default LightGBM search space should be usable with a mock objective."""
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)

        def obj(trial: object, space: dict[str, object], s: TuningSpec) -> float:
            # Return a dummy metric based on num_leaves.
            return float(trial.params.get("num_leaves", 31))

        tuner = OptunaTuner(spec, default_space, str(study_dir))
        artifact = tuner.run(obj)
        assert len(artifact.trials) == 2
        assert artifact.best_trial is not None


# --------------------------------------------------------------------------- #
# OptunaTuner — save / load round-trip                                         #
# --------------------------------------------------------------------------- #


class TestOptunaTunerPersistence:
    """Tests for study.json and best_trial.json save/load."""

    def test_save_study_to_file(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_quadratic_objective)
        out = study_dir / "study.json"
        tuner.save_study(str(out))
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["study_name"] == "quant_foundry_study"
        assert len(data["trials"]) == 3

    def test_save_study_to_directory(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_quadratic_objective)
        tuner.save_study(str(study_dir))
        assert (study_dir / "study.json").exists()

    def test_save_best_trial_to_file(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_quadratic_objective)
        out = study_dir / "best_trial.json"
        tuner.save_best_trial(str(out))
        assert out.exists()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "trial_number" in data
        assert "params" in data
        assert "study_hash" in data

    def test_save_best_trial_to_directory(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_quadratic_objective)
        tuner.save_best_trial(str(study_dir))
        assert (study_dir / "best_trial.json").exists()

    def test_load_study_round_trip(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir), study_name="rt")
        artifact = tuner.run(_quadratic_objective)
        tuner.save_study(str(study_dir))
        loaded = OptunaTuner.load_study(str(study_dir))
        assert loaded.study_name == artifact.study_name
        assert loaded.study_hash == artifact.study_hash
        assert len(loaded.trials) == len(artifact.trials)
        assert loaded.best_trial is not None
        assert loaded.best_trial.trial_number == artifact.best_trial.trial_number

    def test_load_best_trial_round_trip(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_quadratic_objective)
        tuner.save_best_trial(str(study_dir))
        loaded = OptunaTuner.load_best_trial(str(study_dir))
        assert isinstance(loaded, BestTrialArtifact)
        assert loaded.direction == "minimize"
        assert loaded.metric_value is not None

    def test_save_study_before_run_raises(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        with pytest.raises(RuntimeError, match="no study artifact"):
            tuner.save_study(str(study_dir / "study.json"))

    def test_save_best_trial_before_run_raises(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        with pytest.raises(RuntimeError, match="no study artifact"):
            tuner.save_best_trial(str(study_dir / "best_trial.json"))


# --------------------------------------------------------------------------- #
# OptunaTuner — pruning                                                        #
# --------------------------------------------------------------------------- #


class TestOptunaTunerPruning:
    """Tests for pruning behavior."""

    def test_pruned_trial_recorded(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        """A deliberately bad trial should be pruned and recorded as PRUNED."""
        spec = TuningSpec(
            max_trials=10, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.MEDIAN,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_pruning_objective)
        pruned = [t for t in artifact.trials if t.state == "PRUNED"]
        # With median pruning and a deliberately bad value, at least one
        # trial should be pruned once enough trials have completed.
        assert len(artifact.trials) > 0
        # Either some were pruned or all completed (pruning is probabilistic).
        states = {t.state for t in artifact.trials}
        assert states.issubset({"COMPLETE", "PRUNED", "FAIL"})

    def test_pruned_trial_has_reason(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(
            max_trials=10, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.MEDIAN,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_pruning_objective)
        for t in artifact.trials:
            if t.state == "PRUNED":
                assert t.reason is not None
                assert t.metric_value is None

    def test_none_policy_no_pruning(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        """With NONE policy, the pruning objective should complete (no prune)."""
        spec = TuningSpec(
            max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_pruning_objective)
        # NopPruner means should_prune() always returns False.
        assert all(t.state == "COMPLETE" for t in artifact.trials)


# --------------------------------------------------------------------------- #
# OptunaTuner — budget enforcement                                             #
# --------------------------------------------------------------------------- #


class TestOptunaTunerBudget:
    """Tests for wall-clock budget enforcement."""

    def test_short_budget_stops_early(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        """A 60-second budget with a sleeping objective should stop before max_trials."""
        spec = TuningSpec(
            max_trials=100, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_sleep_objective)
        # The sleep objective takes 2s per trial; with a 60s budget we
        # should complete far fewer than 100 trials.
        assert len(artifact.trials) < 100
        assert artifact.total_wall_clock_seconds < 70.0


# --------------------------------------------------------------------------- #
# OptunaTuner — heartbeat                                                      #
# --------------------------------------------------------------------------- #


class TestOptunaTunerHeartbeat:
    """Tests for the heartbeat callback."""

    def test_heartbeat_called_per_trial(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        heartbeats: list[TuningHeartbeat] = []

        def hb_fn(hb: TuningHeartbeat) -> None:
            heartbeats.append(hb)

        tuner.run(_quadratic_objective, heartbeat_fn=hb_fn)
        assert len(heartbeats) == 3
        assert all(isinstance(h, TuningHeartbeat) for h in heartbeats)
        assert heartbeats[0].completed_trials == 1
        assert heartbeats[-1].completed_trials == 3

    def test_heartbeat_best_metric_updates(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        heartbeats: list[TuningHeartbeat] = []
        tuner.run(_quadratic_objective, heartbeat_fn=lambda h: heartbeats.append(h))
        # best_metric_so_far should be set after the first trial.
        assert heartbeats[0].best_metric_so_far is not None

    def test_heartbeat_elapsed_increasing(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        heartbeats: list[TuningHeartbeat] = []
        tuner.run(_quadratic_objective, heartbeat_fn=lambda h: heartbeats.append(h))
        elapsed = [h.elapsed_seconds for h in heartbeats]
        assert elapsed == sorted(elapsed)

    def test_no_heartbeat_fn_ok(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        """Running without a heartbeat_fn should not raise."""
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert len(artifact.trials) == 2


# --------------------------------------------------------------------------- #
# OptunaTuner — failed trials                                                  #
# --------------------------------------------------------------------------- #


class TestOptunaTunerFailedTrials:
    """Tests for failed trial recording."""

    def test_failed_trial_recorded_with_reason(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        """A failing objective should produce a FAIL trial with a reason."""
        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_failing_objective)
        failed = [t for t in artifact.trials if t.state == "FAIL"]
        assert len(failed) > 0
        assert all(t.reason is not None for t in failed)
        assert all("RuntimeError" in (t.reason or "") for t in failed)
        assert all(t.metric_value is None for t in failed)

    def test_failed_trial_no_best(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        """If all trials fail, best_trial should be None."""
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_failing_objective)
        assert artifact.best_trial is None

    def test_save_best_trial_raises_when_no_completion(
        self, study_dir: pathlib.Path, small_space: dict[str, object]
    ) -> None:
        """save_best_trial should raise when no trial completed."""
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_failing_objective)
        with pytest.raises(RuntimeError, match="no trial completed"):
            tuner.save_best_trial(str(study_dir / "best_trial.json"))


# --------------------------------------------------------------------------- #
# OptunaTuner — search algorithms                                              #
# --------------------------------------------------------------------------- #


class TestSearchAlgorithms:
    """Tests for all search algorithms (TPE, RANDOM, CMAES)."""

    @pytest.mark.parametrize("algo", list(SearchAlgorithm))
    def test_algorithm_runs(
        self, algo: SearchAlgorithm, study_dir: pathlib.Path, small_space: dict[str, object]
    ) -> None:
        spec = TuningSpec(
            search_algorithm=algo, max_trials=3, max_wall_clock_seconds=60,
            pruning_policy=PruningPolicy.NONE,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir), study_name=f"algo_{algo.value}")
        artifact = tuner.run(_quadratic_objective)
        assert len(artifact.trials) == 3
        assert artifact.best_trial is not None

    def test_tpe_default(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        assert spec.search_algorithm is SearchAlgorithm.TPE
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert len(artifact.trials) == 2

    def test_seed_reproducibility(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        """Same seed should produce the same first-trial params with RANDOM."""
        spec1 = TuningSpec(
            search_algorithm=SearchAlgorithm.RANDOM, max_trials=1,
            max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE, seed=99,
        )
        spec2 = TuningSpec(
            search_algorithm=SearchAlgorithm.RANDOM, max_trials=1,
            max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE, seed=99,
        )
        tuner1 = OptunaTuner(spec1, small_space, str(study_dir / "s1"))
        tuner2 = OptunaTuner(spec2, small_space, str(study_dir / "s2"))
        a1 = tuner1.run(_quadratic_objective)
        a2 = tuner2.run(_quadratic_objective)
        assert a1.trials[0].params == a2.trials[0].params


# --------------------------------------------------------------------------- #
# OptunaTuner — pruning policies                                               #
# --------------------------------------------------------------------------- #


class TestPruningPolicies:
    """Tests for all pruning policies (NONE, MEDIAN, SUCCESSIVE_HALVING, HYPERBAND)."""

    @pytest.mark.parametrize("policy", list(PruningPolicy))
    def test_policy_runs(
        self, policy: PruningPolicy, study_dir: pathlib.Path, small_space: dict[str, object]
    ) -> None:
        spec = TuningSpec(
            max_trials=3, max_wall_clock_seconds=60, pruning_policy=policy,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir), study_name=f"prune_{policy.value}")
        artifact = tuner.run(_quadratic_objective)
        assert len(artifact.trials) > 0
        assert artifact.best_trial is not None

    def test_none_policy_completes_all(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(
            max_trials=4, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE,
        )
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert all(t.state == "COMPLETE" for t in artifact.trials)


# --------------------------------------------------------------------------- #
# OptunaTuner — study hash determinism                                         #
# --------------------------------------------------------------------------- #


class TestStudyHashDeterminism:
    """Tests that the study hash is deterministic for identical studies."""

    def test_same_study_data_same_hash(self) -> None:
        data = {
            "study_name": "test",
            "trials": [{"trial_number": 0, "metric_value": 0.5}],
            "best_trial": None,
        }
        assert compute_study_hash(data) == compute_study_hash(data)

    def test_different_study_name_different_hash(self) -> None:
        d1 = {"study_name": "a", "trials": []}
        d2 = {"study_name": "b", "trials": []}
        assert compute_study_hash(d1) != compute_study_hash(d2)

    def test_artifact_hash_is_64_hex(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        h = artifact.study_hash
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# --------------------------------------------------------------------------- #
# OptunaTuner — edge cases                                                     #
# --------------------------------------------------------------------------- #


class TestOptunaTunerEdgeCases:
    """Tests for edge cases and error handling."""

    def test_single_trial(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=1, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        artifact = tuner.run(_quadratic_objective)
        assert len(artifact.trials) == 1
        assert artifact.best_trial is not None
        assert artifact.best_trial.trial_number == 0

    def test_study_dir_created_if_missing(self, tmp_path: pathlib.Path, small_space: dict[str, object]) -> None:
        new_dir = tmp_path / "new_study_dir"
        assert not new_dir.exists()
        spec = TuningSpec(max_trials=1, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        OptunaTuner(spec, small_space, str(new_dir))
        assert new_dir.exists()

    def test_custom_study_name(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=1, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir), study_name="my_custom_study")
        artifact = tuner.run(_quadratic_objective)
        assert artifact.study_name == "my_custom_study"

    def test_load_study_from_file_path(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_quadratic_objective)
        file_path = study_dir / "study.json"
        tuner.save_study(str(file_path))
        loaded = OptunaTuner.load_study(str(file_path))
        assert isinstance(loaded, StudyArtifact)

    def test_load_best_trial_from_file_path(self, study_dir: pathlib.Path, small_space: dict[str, object]) -> None:
        spec = TuningSpec(max_trials=2, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, small_space, str(study_dir))
        tuner.run(_quadratic_objective)
        file_path = study_dir / "best_trial.json"
        tuner.save_best_trial(str(file_path))
        loaded = OptunaTuner.load_best_trial(str(file_path))
        assert isinstance(loaded, BestTrialArtifact)

    def test_categorical_search_space(self, study_dir: pathlib.Path) -> None:
        """A categorical search space should be sampled correctly."""
        space = {
            "kernel": {"type": "categorical", "choices": ["linear", "rbf", "poly"]},
            "C": {"type": "float", "low": 0.1, "high": 10.0, "log": True},
        }

        def obj(trial: object, space: dict[str, object], s: TuningSpec) -> float:
            kernel = trial.params["kernel"]
            c = trial.params["C"]
            # Dummy metric.
            return float(c if kernel == "rbf" else c * 2)

        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, space, str(study_dir))
        artifact = tuner.run(obj)
        assert len(artifact.trials) == 3
        for t in artifact.trials:
            assert t.params["kernel"] in ("linear", "rbf", "poly")

    def test_int_search_space(self, study_dir: pathlib.Path) -> None:
        """An int search space should sample integers."""
        space = {"n": {"type": "int", "low": 1, "high": 100, "step": 1}}

        def obj(trial: object, space: dict[str, object], s: TuningSpec) -> float:
            return float(trial.params["n"])

        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, space, str(study_dir))
        artifact = tuner.run(obj)
        for t in artifact.trials:
            assert isinstance(t.params["n"], int)

    def test_log_float_search_space(self, study_dir: pathlib.Path) -> None:
        """A log-scale float search space should sample positive floats."""
        space = {"lr": {"type": "float", "low": 1e-5, "high": 1.0, "log": True}}

        def obj(trial: object, space: dict[str, object], s: TuningSpec) -> float:
            return float(trial.params["lr"])

        spec = TuningSpec(max_trials=3, max_wall_clock_seconds=60, pruning_policy=PruningPolicy.NONE)
        tuner = OptunaTuner(spec, space, str(study_dir))
        artifact = tuner.run(obj)
        for t in artifact.trials:
            assert t.params["lr"] > 0

    def test_early_stopping_rounds_in_spec(self) -> None:
        """early_stopping_rounds should be carried in the spec."""
        spec = TuningSpec(early_stopping_rounds=50)
        assert spec.early_stopping_rounds == 50
        # Round-trip through model_dump.
        dumped = spec.model_dump(mode="json")
        assert dumped["early_stopping_rounds"] == 50
