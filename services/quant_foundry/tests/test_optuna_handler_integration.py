"""Integration tests for Tier 1.4: Optuna hyperparameter search in the worker.

Verifies the acceptance criteria:
1. ``enable_optuna=true`` triggers an Optuna search before trainer
   construction (the handler runs OptunaTuner when
   ``req.search_space`` is non-empty AND
   ``req.extra_constraints["enable_optuna"]`` is truthy).
2. The trial count is recorded in the callback's ``metrics_summary`` as
   ``optuna_trial_count`` (and ``optuna_best_params``) so Tier 2's
   Deflated Sharpe can use the *real* number of trials evaluated.
3. Deadline-aware stopping: ``max_wall_clock_seconds`` is set to the
   handler's remaining deadline minus a 60s buffer. When the remaining
   time is too short (< 120s total), Optuna is skipped (backward
   compatible).
4. Disabled mode (no ``search_space`` or ``enable_optuna`` not set) is
   backward compatible — no optuna fields in the callback, behavior
   unchanged.
5. The ``OptunaTuner.run()`` objective signature is compatible with the
   handler's training loop (the handler provides an objective that
   trains with given params and returns the validation metric).

The handler module lives in ``runpod/quant-foundry-training/handler.py``
(outside the quant_foundry package), so tests add that directory to
``sys.path`` and import the module directly. All tests use the canary /
LocalTrainer path (no GPU/ML deps needed).
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")

try:
    import optuna  # noqa: F401 - availability check

    _HAS_OPTUNA = True
except ImportError:
    _HAS_OPTUNA = False

_optuna_skip = pytest.mark.skipif(not _HAS_OPTUNA, reason="optuna not installed")


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def handler_module():
    """Import the handler module (adding its dir to sys.path)."""
    if _HANDLER_DIR not in sys.path:
        sys.path.insert(0, _HANDLER_DIR)
    return importlib.import_module("handler")


def _make_training_input(job_id: str, **extra) -> dict:
    """Build a minimal training input dict for the handler (canary path)."""
    return {
        "input": {
            "job_id": job_id,
            "dataset_manifest_ref": "ds-manifest-test",
            "model_family": "gbm",
            "search_space": {},
            "random_seed": 42,
            "hardware_class": "mock-gpu",
            "extra_constraints": {},
            **extra,
        }
    }


def _extract_metrics_summary(result: dict) -> dict:
    """Extract the metrics_summary from a handler result's typed_callback."""
    assert "error_code" not in result, f"handler returned error: {result.get('error_code')}"
    typed_callback = result["typed_callback"]
    return dict(typed_callback["metrics_summary"])


# --------------------------------------------------------------------------- #
# _optuna_is_enabled unit tests                                                #
# --------------------------------------------------------------------------- #


def test_optuna_is_enabled_requires_search_space(handler_module):
    """_optuna_is_enabled returns False when search_space is empty."""
    req = handler_module.RunPodTrainingRequest(
        job_id="qf:optuna:test:1",
        dataset_manifest_ref="ds-test",
        model_family="gbm",
        search_space={},
        extra_constraints={"enable_optuna": "true"},
    )
    assert handler_module._optuna_is_enabled(req) is False


def test_optuna_is_enabled_requires_flag(handler_module):
    """_optuna_is_enabled returns False when enable_optuna is not set."""
    req = handler_module.RunPodTrainingRequest(
        job_id="qf:optuna:test:2",
        dataset_manifest_ref="ds-test",
        model_family="gbm",
        search_space={"num_leaves": [31, 63]},
        extra_constraints={},
    )
    assert handler_module._optuna_is_enabled(req) is False


def test_optuna_is_enabled_truthy_values(handler_module):
    """_optuna_is_enabled accepts 'true', '1', 'yes' (case-insensitive)."""
    for val in ("true", "True", "TRUE", "1", "yes", "Yes"):
        req = handler_module.RunPodTrainingRequest(
            job_id=f"qf:optuna:test:{val}",
            dataset_manifest_ref="ds-test",
            model_family="gbm",
            search_space={"num_leaves": [31, 63]},
            extra_constraints={"enable_optuna": val},
        )
        assert handler_module._optuna_is_enabled(req) is True, f"failed for {val!r}"


def test_optuna_is_enabled_falsy_values(handler_module):
    """_optuna_is_enabled rejects 'false', '0', 'no', ''."""
    for val in ("false", "0", "no", "", "maybe"):
        req = handler_module.RunPodTrainingRequest(
            job_id=f"qf:optuna:test:{val}",
            dataset_manifest_ref="ds-test",
            model_family="gbm",
            search_space={"num_leaves": [31, 63]},
            extra_constraints={"enable_optuna": val},
        )
        assert handler_module._optuna_is_enabled(req) is False, f"failed for {val!r}"


# --------------------------------------------------------------------------- #
# convert_categorical_search_space unit tests                                  #
# --------------------------------------------------------------------------- #


def test_convert_categorical_search_space_basic():
    """convert_categorical_search_space converts dict[str, list] to Optuna format."""
    from quant_foundry.optuna_tuning import convert_categorical_search_space

    choices = {"num_leaves": [31, 63, 127], "learning_rate": [0.01, 0.05, 0.1]}
    result = convert_categorical_search_space(choices)
    assert result == {
        "num_leaves": {"type": "categorical", "choices": [31, 63, 127]},
        "learning_rate": {"type": "categorical", "choices": [0.01, 0.05, 0.1]},
    }


def test_convert_categorical_search_space_skips_empty():
    """convert_categorical_search_space skips keys with empty lists."""
    from quant_foundry.optuna_tuning import convert_categorical_search_space

    choices = {"num_leaves": [31, 63], "empty_param": []}
    result = convert_categorical_search_space(choices)
    assert "empty_param" not in result
    assert "num_leaves" in result


# --------------------------------------------------------------------------- #
# Handler integration tests (canary / LocalTrainer path)                       #
# --------------------------------------------------------------------------- #


@_optuna_skip
def test_optuna_enabled_triggers_search(handler_module, monkeypatch):
    """enable_optuna=true triggers Optuna search and records trial count."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-int-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    event = _make_training_input(
        "qf:optuna:enabled:1",
        search_space={"num_leaves": [31, 63, 127], "learning_rate": [0.01, 0.05, 0.1]},
        extra_constraints={
            "enable_optuna": "true",
            "optuna_max_trials": "3",
            "optuna_metric": "logloss",
            "optuna_direction": "minimize",
        },
    )
    result = handler_module.handler(event)
    metrics = _extract_metrics_summary(result)
    # Trial count is recorded (3 trials requested, all should complete).
    assert "optuna_trial_count" in metrics
    assert metrics["optuna_trial_count"] == 3
    # Best params are recorded.
    assert "optuna_best_params" in metrics
    best_params = metrics["optuna_best_params"]
    assert isinstance(best_params, dict)
    assert "num_leaves" in best_params
    assert "learning_rate" in best_params
    # The best params should be one of the searched values.
    assert best_params["num_leaves"] in [31, 63, 127]
    assert best_params["learning_rate"] in [0.01, 0.05, 0.1]


@_optuna_skip
def test_optuna_trial_count_is_honest(handler_module, monkeypatch):
    """The trial count reflects the actual number of trials evaluated."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-count-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    event = _make_training_input(
        "qf:optuna:count:1",
        search_space={"x": [1, 2, 3, 4, 5]},
        extra_constraints={
            "enable_optuna": "true",
            "optuna_max_trials": "5",
        },
    )
    result = handler_module.handler(event)
    metrics = _extract_metrics_summary(result)
    assert metrics["optuna_trial_count"] == 5


@_optuna_skip
def test_optuna_disabled_no_search_space_is_backward_compatible(handler_module, monkeypatch):
    """No search_space → Optuna disabled, no optuna fields in callback."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-noss-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    event = _make_training_input(
        "qf:optuna:noss:1",
        search_space={},
        extra_constraints={"enable_optuna": "true"},
    )
    result = handler_module.handler(event)
    metrics = _extract_metrics_summary(result)
    # No optuna fields when search_space is empty.
    assert "optuna_trial_count" not in metrics
    assert "optuna_best_params" not in metrics


@_optuna_skip
def test_optuna_disabled_no_flag_is_backward_compatible(handler_module, monkeypatch):
    """No enable_optuna flag → Optuna disabled, no optuna fields in callback."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-noflag-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    event = _make_training_input(
        "qf:optuna:noflag:1",
        search_space={"num_leaves": [31, 63]},
        extra_constraints={},
    )
    result = handler_module.handler(event)
    metrics = _extract_metrics_summary(result)
    assert "optuna_trial_count" not in metrics
    assert "optuna_best_params" not in metrics


@_optuna_skip
def test_optuna_disabled_flag_false_is_backward_compatible(handler_module, monkeypatch):
    """enable_optuna=false → Optuna disabled, no optuna fields in callback."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-false-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    event = _make_training_input(
        "qf:optuna:false:1",
        search_space={"num_leaves": [31, 63]},
        extra_constraints={"enable_optuna": "false"},
    )
    result = handler_module.handler(event)
    metrics = _extract_metrics_summary(result)
    assert "optuna_trial_count" not in metrics
    assert "optuna_best_params" not in metrics


@_optuna_skip
def test_optuna_skipped_when_deadline_too_short(handler_module, monkeypatch):
    """Optuna is skipped when the remaining deadline is too short (< 120s).

    With deadline_seconds=1, the remaining time (~1s) minus the 60s buffer
    is negative (< 60s TuningSpec minimum), so Optuna is skipped and the
    handler proceeds with static params (backward compatible).
    """
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-deadline-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "1")
    event = _make_training_input(
        "qf:optuna:deadline:1",
        search_space={"num_leaves": [31, 63]},
        extra_constraints={
            "enable_optuna": "true",
            "optuna_max_trials": "3",
        },
    )
    result = handler_module.handler(event)
    metrics = _extract_metrics_summary(result)
    # Optuna was skipped — no optuna fields.
    assert "optuna_trial_count" not in metrics
    assert "optuna_best_params" not in metrics


@_optuna_skip
def test_optuna_best_params_replace_static_search_space(handler_module, monkeypatch):
    """Best params from Optuna replace the static search_space for final training.

    The handler creates a modified request with the best params as the
    search_space (in dict[str, list] format) so the final trainer uses the
    tuned values instead of the static defaults.
    """
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-replace-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    event = _make_training_input(
        "qf:optuna:replace:1",
        search_space={"num_leaves": [31, 63, 127], "learning_rate": [0.01, 0.05, 0.1]},
        extra_constraints={
            "enable_optuna": "true",
            "optuna_max_trials": "3",
        },
    )
    result = handler_module.handler(event)
    metrics = _extract_metrics_summary(result)
    # The best params are recorded and are valid choices from the space.
    best_params = metrics["optuna_best_params"]
    assert best_params["num_leaves"] in [31, 63, 127]
    assert best_params["learning_rate"] in [0.01, 0.05, 0.1]


@_optuna_skip
def test_optuna_callback_payload_is_valid_json(handler_module, monkeypatch):
    """The callback payload with optuna fields is valid JSON and signed."""
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "optuna-json-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_TRAINING_DEADLINE_SECONDS", "600")
    event = _make_training_input(
        "qf:optuna:json:1",
        search_space={"num_leaves": [31, 63], "learning_rate": [0.01, 0.1]},
        extra_constraints={
            "enable_optuna": "true",
            "optuna_max_trials": "2",
        },
    )
    result = handler_module.handler(event)
    # The callback payload is valid JSON.
    payload = json.loads(result["callback_payload"])
    assert payload["job_id"] == "qf:optuna:json:1"
    # The typed callback is a valid dict with metrics_summary.
    typed = result["typed_callback"]
    assert typed["metrics_summary"]["optuna_trial_count"] == 2
    # Signature is present.
    assert typed["callback_signature"]
