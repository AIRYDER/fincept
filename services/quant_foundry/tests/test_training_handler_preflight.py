from __future__ import annotations

import importlib
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")


@pytest.fixture(scope="module")
def handler_module():
    if _HANDLER_DIR not in sys.path:
        sys.path.insert(0, _HANDLER_DIR)
    return importlib.import_module("handler")


def test_worker_preflight_redacts_runpod_job_take_url(handler_module, monkeypatch):
    monkeypatch.setenv(
        "RUNPOD_WEBHOOK_GET_JOB",
        "https://api.runpod.ai/v2/e/job-take/token?gpu=NVIDIA",
    )
    preflight = handler_module.SecurityPreflight(mode=handler_module.TrainingMode.CANARY)

    redacted = preflight._build_redacted_config(())

    assert redacted["RUNPOD_WEBHOOK_GET_JOB"] == "****"


def test_worker_preflight_fully_masks_callback_secret(handler_module, monkeypatch):
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "dummy-callback-value")
    preflight = handler_module.SecurityPreflight(mode=handler_module.TrainingMode.CANARY)

    redacted = preflight._build_redacted_config(())

    assert redacted["QUANT_FOUNDRY_CALLBACK_SECRET"] == "****"
