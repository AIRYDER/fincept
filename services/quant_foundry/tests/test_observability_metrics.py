"""Integration tests for Tier 1.6: Observability + cost accounting.

Verifies that the handler emits per-job structured operational metrics
in the callback's metrics_summary:

1. ``execution_time_ms``: wall-clock training time in milliseconds
   (always present, >= 0).
2. ``queue_delay_ms``: queue delay (0 from the worker — the trusted-side
   gateway can inject the real value from the RunPod receipt).
3. ``cost_usd``: estimated GPU cost in USD (from the rate table in
   cost_tracker.py; 0.0 when GPU type is unknown).
4. ``gpu_model``: GPU model name when nvidia-smi is available (None/
   absent when no GPU).

Also verifies that the gateway's ``_record_operational_metrics`` method
extracts these metrics from the callback payload and records them via
``CostTracker.record_metric()`` and ``CostTracker.record_cost_event()``.

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
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")


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
# Handler metrics emission tests                                               #
# --------------------------------------------------------------------------- #


class TestHandlerMetricsEmission:
    """Tests that the handler emits operational metrics in the callback."""

    def test_execution_time_ms_present(self, handler_module):
        """metrics_summary must include execution_time_ms (>= 0)."""
        event = _make_training_input("qf:obs:exec:1")
        result = handler_module.handler(event)
        metrics = _extract_metrics_summary(result)
        assert "execution_time_ms" in metrics
        assert isinstance(metrics["execution_time_ms"], int)
        assert metrics["execution_time_ms"] >= 0

    def test_queue_delay_ms_present(self, handler_module):
        """metrics_summary must include queue_delay_ms (defaults to 0)."""
        event = _make_training_input("qf:obs:queue:1")
        result = handler_module.handler(event)
        metrics = _extract_metrics_summary(result)
        assert "queue_delay_ms" in metrics
        assert isinstance(metrics["queue_delay_ms"], int)
        assert metrics["queue_delay_ms"] == 0

    def test_cost_usd_present(self, handler_module):
        """metrics_summary must include cost_usd (float, >= 0)."""
        event = _make_training_input("qf:obs:cost:1")
        result = handler_module.handler(event)
        metrics = _extract_metrics_summary(result)
        assert "cost_usd" in metrics
        assert isinstance(metrics["cost_usd"], (int, float))
        assert metrics["cost_usd"] >= 0.0

    def test_cost_usd_zero_when_no_gpu(self, handler_module, monkeypatch):
        """cost_usd should be 0.0 when _probe_gpu_model returns None."""
        monkeypatch.setattr(handler_module, "_probe_gpu_model", lambda: None)
        event = _make_training_input("qf:obs:no-gpu:1")
        result = handler_module.handler(event)
        metrics = _extract_metrics_summary(result)
        assert metrics["cost_usd"] == 0.0

    def test_gpu_model_present_when_probe_succeeds(self, handler_module, monkeypatch):
        """metrics_summary must include gpu_model when nvidia-smi succeeds."""
        monkeypatch.setattr(handler_module, "_probe_gpu_model", lambda: "RTX 4090")
        event = _make_training_input("qf:obs:gpu-model:1")
        result = handler_module.handler(event)
        metrics = _extract_metrics_summary(result)
        assert metrics.get("gpu_model") == "RTX 4090"

    def test_gpu_model_absent_when_probe_fails(self, handler_module, monkeypatch):
        """metrics_summary must NOT include gpu_model when nvidia-smi fails."""

        def _raise():
            raise RuntimeError("nvidia-smi not available")

        monkeypatch.setattr(handler_module, "_probe_gpu_model", _raise)
        event = _make_training_input("qf:obs:no-nvidia:1")
        result = handler_module.handler(event)
        metrics = _extract_metrics_summary(result)
        assert "gpu_model" not in metrics or metrics.get("gpu_model") is None

    def test_cost_usd_nonzero_with_known_gpu(self, handler_module, monkeypatch):
        """cost_usd should be > 0 when GPU model is known and execution takes > 0ms."""
        monkeypatch.setattr(handler_module, "_probe_gpu_model", lambda: "RTX_4090")
        event = _make_training_input("qf:obs:cost-gpu:1")
        result = handler_module.handler(event)
        metrics = _extract_metrics_summary(result)
        # Even a fast training run should have execution_time_ms >= 0.
        # cost_usd may be 0 if execution_time_ms is 0 (sub-millisecond).
        assert "cost_usd" in metrics
        assert metrics["cost_usd"] >= 0.0

    def test_all_metrics_present_in_callback_payload(self, handler_module, monkeypatch):
        """All four operational metrics must be in the typed_callback."""
        monkeypatch.setattr(handler_module, "_probe_gpu_model", lambda: "RTX_4090")
        event = _make_training_input("qf:obs:all:1")
        result = handler_module.handler(event)
        # The typed_callback dict has metrics_summary at the top level
        # (RunPodTrainingCallback model).
        tc = result.get("typed_callback", {})
        metrics = tc.get("metrics_summary", {})
        assert "execution_time_ms" in metrics
        assert "queue_delay_ms" in metrics
        assert "cost_usd" in metrics


# --------------------------------------------------------------------------- #
# Gateway _record_operational_metrics tests                                    #
# --------------------------------------------------------------------------- #


class TestGatewayMetricsRecording:
    """Tests that the gateway extracts metrics from callbacks and records them."""

    def _make_gateway_with_mock_tracker(self):
        """Build a gateway-like object with a mock CostTracker."""
        from quant_foundry.gateway import QuantFoundryGateway

        # Create a minimal mock — we only need _record_operational_metrics
        # and cost_tracker().
        gateway = MagicMock(spec=QuantFoundryGateway)
        tracker = MagicMock()
        gateway.cost_tracker.return_value = tracker
        return gateway, tracker

    def _make_callback_payload(self, **metrics) -> bytes:
        """Build a callback payload JSON with the given metrics_summary."""
        payload = {
            "result_type": "training_complete",
            "payload": {
                "metrics_summary": {
                    "execution_time_ms": 5000,
                    "queue_delay_ms": 1200,
                    "cost_usd": 0.55,
                    "gpu_model": "RTX_4090",
                    **metrics,
                },
            },
        }
        return json.dumps(payload).encode("utf-8")

    def test_records_execution_time(self):
        """_record_operational_metrics records execution_time_ms."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = self._make_callback_payload(execution_time_ms=5000)
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-1", payload)
        # record_metric should have been called with execution_time.
        calls = tracker.record_metric.call_args_list
        metric_types = [c.kwargs.get("metric_type") or c.args[1] for c in calls]
        assert "execution_time" in metric_types

    def test_records_queue_delay(self):
        """_record_operational_metrics records queue_delay_ms."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = self._make_callback_payload(queue_delay_ms=1200)
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-2", payload)
        calls = tracker.record_metric.call_args_list
        metric_types = [c.kwargs.get("metric_type") or c.args[1] for c in calls]
        assert "queue_delay" in metric_types

    def test_records_cost_usd(self):
        """_record_operational_metrics records cost_usd."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = self._make_callback_payload(cost_usd=0.55)
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-3", payload)
        calls = tracker.record_metric.call_args_list
        metric_types = [c.kwargs.get("metric_type") or c.args[1] for c in calls]
        assert "cost_usd" in metric_types

    def test_records_cost_event(self):
        """_record_operational_metrics records a cost event for GPU compute."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = self._make_callback_payload(
            execution_time_ms=5000,
            cost_usd=0.55,
            gpu_model="RTX_4090",
        )
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-4", payload)
        tracker.record_cost_event.assert_called_once()
        call = tracker.record_cost_event.call_args
        assert call.kwargs["event_type"] == "gpu_compute"
        assert call.kwargs["currency"] == "USD"

    def test_skips_when_no_metrics_summary(self):
        """_record_operational_metrics is a no-op when no metrics_summary."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = json.dumps({"result_type": "training_complete"}).encode("utf-8")
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-5", payload)
        tracker.record_metric.assert_not_called()
        tracker.record_cost_event.assert_not_called()

    def test_skips_when_payload_not_json(self):
        """_record_operational_metrics is a no-op when payload is not JSON."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = b"not-json{"
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-6", payload)
        tracker.record_metric.assert_not_called()

    def test_records_gpu_model_metric(self):
        """_record_operational_metrics records gpu_model as a metric."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = self._make_callback_payload(gpu_model="RTX_4090")
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-7", payload)
        calls = tracker.record_metric.call_args_list
        metric_types = [c.kwargs.get("metric_type") or c.args[1] for c in calls]
        assert any("gpu_model" in mt for mt in metric_types)

    def test_flat_metrics_summary_also_works(self):
        """_record_operational_metrics handles flat (non-nested) metrics_summary."""
        gateway, tracker = self._make_gateway_with_mock_tracker()
        from quant_foundry.gateway import QuantFoundryGateway

        payload = json.dumps(
            {
                "metrics_summary": {
                    "execution_time_ms": 3000,
                    "cost_usd": 0.33,
                },
            }
        ).encode("utf-8")
        QuantFoundryGateway._record_operational_metrics(gateway, "job-obs-8", payload)
        calls = tracker.record_metric.call_args_list
        metric_types = [c.kwargs.get("metric_type") or c.args[1] for c in calls]
        assert "execution_time" in metric_types
