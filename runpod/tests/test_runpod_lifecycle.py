"""Unit tests for the shared RunPod lifecycle helper.

Tests cover:
- Unique name generation (timestamp/SHA suffix, collision avoidance)
- Timeout configuration (executionTimeout >= 1860, validation, floor enforcement)
- Retry cleanup logic (deleteEndpoint with retry on transient failures)
- Safe scale-to-zero helper
- Template/endpoint input builders
- Receipt-friendly timeout formatting

All RunPod API calls are mocked — no real HTTP calls are made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add scripts/ to sys.path so runpod.runpod_lifecycle is importable.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = str(_REPO_ROOT / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

    build_endpoint_input,
    build_job_policy,
    build_template_input,
    compute_execution_timeout,
    format_timeout_receipt,
    make_unique_name,
    retry_delete_endpoint,
    safe_scale_to_zero,
    validate_execution_timeout,
)

# ---------------------------------------------------------------------------
# Unique naming
# ---------------------------------------------------------------------------


class TestMakeUniqueName:
    def test_basic_name_has_timestamp_and_sha(self):
        name = make_unique_name("qf-canary", "abcdef1234567890", timestamp=1719900000)
        assert name == "qf-canary-abcdef12-1719900000"

    def test_name_with_suffix(self):
        name = make_unique_name(
            "qf-a7train", "abcdef1234567890", suffix="tpl", timestamp=1719900000
        )
        assert name == "qf-a7train-abcdef12-tpl-1719900000"

    def test_sha_truncated_to_sha_len(self):
        name = make_unique_name("qf-canary", "abcdef1234567890", sha_len=8, timestamp=100)
        assert "abcdef12" in name

    def test_default_timestamp_is_current_time(self):
        import time as _time

        before = int(_time.time())
        name = make_unique_name("qf-test", "abcdef1234567890")
        after = int(_time.time())
        # The timestamp in the name should be between before and after.
        ts_str = name.rsplit("-", 1)[-1]
        ts = int(ts_str)
        assert before <= ts <= after

    def test_two_calls_with_same_args_differ_when_timestamp_changes(self):
        n1 = make_unique_name("qf-canary", "abcdef1234567890", timestamp=100)
        n2 = make_unique_name("qf-canary", "abcdef1234567890", timestamp=200)
        assert n1 != n2

    def test_different_prefixes_dont_collide(self):
        n1 = make_unique_name("qf-canary", "abcdef1234567890", timestamp=100)
        n2 = make_unique_name("qf-gpuhc", "abcdef1234567890", timestamp=100)
        assert n1 != n2

    def test_empty_suffix_omitted(self):
        name = make_unique_name("qf-canary", "abcdef1234567890", suffix="", timestamp=100)
        assert name == "qf-canary-abcdef12-100"


# ---------------------------------------------------------------------------
# Timeout configuration
# ---------------------------------------------------------------------------


class TestComputeExecutionTimeout:
    def test_default_is_deadline_plus_slack(self):
        timeout = compute_execution_timeout()
        assert timeout == DEFAULT_DEADLINE_S + DEFAULT_SLACK_S
        assert timeout == 1860

    def test_custom_deadline_and_slack(self):
        timeout = compute_execution_timeout(deadline_s=3600, slack_s=120)
        assert timeout == 3720

    def test_floor_enforced_when_below_minimum(self):
        # If someone passes a tiny deadline, the floor is enforced.
        timeout = compute_execution_timeout(deadline_s=100, slack_s=10)
        assert timeout == MIN_EXECUTION_TIMEOUT_S
        assert timeout >= 1860


class TestValidateExecutionTimeout:
    def test_valid_timeout_passes(self):
        assert validate_execution_timeout(1860) == 1860
        assert validate_execution_timeout(3600) == 3600

    def test_below_minimum_raises(self):
        with pytest.raises(ValueError, match="below the minimum"):
            validate_execution_timeout(600)

    def test_below_minimum_boundary(self):
        with pytest.raises(ValueError):
            validate_execution_timeout(1859)

    def test_exact_minimum_passes(self):
        assert validate_execution_timeout(1860) == 1860


# ---------------------------------------------------------------------------
# Endpoint input builder
# ---------------------------------------------------------------------------


class TestBuildEndpointInput:
    def test_includes_execution_timeout(self):
        config = EndpointConfig(name="test-ep", template_id="tpl-123")
        inp = build_endpoint_input(config)
        assert "executionTimeout" in inp
        assert inp["executionTimeout"] >= MIN_EXECUTION_TIMEOUT_S

    def test_default_execution_timeout_is_1860(self):
        config = EndpointConfig(name="test-ep", template_id="tpl-123")
        inp = build_endpoint_input(config)
        assert inp["executionTimeout"] == 1860

    def test_custom_execution_timeout_accepted(self):
        config = EndpointConfig(
            name="test-ep",
            template_id="tpl-123",
            execution_timeout=3600,
        )
        inp = build_endpoint_input(config)
        assert inp["executionTimeout"] == 3600

    def test_below_minimum_execution_timeout_raises(self):
        config = EndpointConfig(
            name="test-ep",
            template_id="tpl-123",
            execution_timeout=600,
        )
        with pytest.raises(ValueError, match="below the minimum"):
            build_endpoint_input(config)

    def test_all_fields_present(self):
        config = EndpointConfig(name="test-ep", template_id="tpl-123")
        inp = build_endpoint_input(config)
        expected_keys = {
            "name",
            "templateId",
            "gpuIds",
            "workersMin",
            "workersMax",
            "idleTimeout",
            "executionTimeout",
            "scalerType",
            "scalerValue",
        }
        assert set(inp.keys()) == expected_keys

    def test_idle_timeout_configurable(self):
        config = EndpointConfig(name="test-ep", template_id="tpl-123", idle_timeout=600)
        inp = build_endpoint_input(config)
        assert inp["idleTimeout"] == 600


# ---------------------------------------------------------------------------
# Template input builder
# ---------------------------------------------------------------------------


class TestBuildTemplateInput:
    def test_basic_template_input(self):
        config = TemplateConfig(
            name="test-tpl",
            image_name="ghcr.io/test/img:sha",
            env_vars=[{"key": "FOO", "value": "bar"}],
            registry_auth_id="auth-123",
        )
        inp = build_template_input(config)
        assert inp["name"] == "test-tpl"
        assert inp["imageName"] == "ghcr.io/test/img:sha"
        assert inp["env"] == [{"key": "FOO", "value": "bar"}]
        assert inp["containerRegistryAuthId"] == "auth-123"
        assert inp["isServerless"] is True
        assert inp["containerDiskInGb"] == 20

    def test_custom_disk_sizes(self):
        config = TemplateConfig(
            name="test-tpl",
            image_name="img",
            env_vars=[],
            registry_auth_id="auth",
            container_disk_gb=40,
            volume_in_gb=5,
        )
        inp = build_template_input(config)
        assert inp["containerDiskInGb"] == 40
        assert inp["volumeInGb"] == 5


# ---------------------------------------------------------------------------
# Retry delete endpoint
# ---------------------------------------------------------------------------


class TestRetryDeleteEndpoint:
    def test_success_on_first_attempt(self):
        delete_fn = MagicMock()
        result = retry_delete_endpoint("ep-123", delete_fn, logger=None)
        assert result is True
        delete_fn.assert_called_once_with("ep-123")

    def test_retries_on_failure_then_succeeds(self):
        delete_fn = MagicMock(side_effect=[RuntimeError("transient"), None])
        sleeper = MagicMock()
        result = retry_delete_endpoint(
            "ep-123",
            delete_fn,
            max_attempts=5,
            delay_s=10.0,
            sleeper=sleeper,
        )
        assert result is True
        assert delete_fn.call_count == 2
        sleeper.assert_called_once_with(10.0)

    def test_all_attempts_fail(self):
        delete_fn = MagicMock(side_effect=RuntimeError("permanent"))
        sleeper = MagicMock()
        result = retry_delete_endpoint(
            "ep-123",
            delete_fn,
            max_attempts=3,
            delay_s=5.0,
            sleeper=sleeper,
        )
        assert result is False
        assert delete_fn.call_count == 3
        assert sleeper.call_count == 2  # no sleep after last attempt

    def test_no_sleep_after_last_attempt(self):
        delete_fn = MagicMock(side_effect=RuntimeError("fail"))
        sleeper = MagicMock()
        retry_delete_endpoint(
            "ep-123",
            delete_fn,
            max_attempts=2,
            delay_s=5.0,
            sleeper=sleeper,
        )
        # 2 attempts, 1 sleep (between attempt 1 and 2, not after 2)
        assert sleeper.call_count == 1

    def test_logger_called_on_failure(self):
        delete_fn = MagicMock(side_effect=RuntimeError("oops"))
        logger = MagicMock()
        retry_delete_endpoint(
            "ep-123", delete_fn, max_attempts=2, sleeper=MagicMock(), logger=logger
        )
        # Logger should have been called with warning messages
        assert logger.call_count >= 2

    def test_logger_called_on_success(self):
        delete_fn = MagicMock()
        logger = MagicMock()
        retry_delete_endpoint("ep-123", delete_fn, logger=logger)
        logger.assert_called_once()


# ---------------------------------------------------------------------------
# Safe scale to zero
# ---------------------------------------------------------------------------


class TestSafeScaleToZero:
    def test_success(self):
        scale_fn = MagicMock()
        result = safe_scale_to_zero("ep-123", scale_fn, logger=None)
        assert result is True
        scale_fn.assert_called_once_with("ep-123", 0, 0)

    def test_failure_returns_false_not_raise(self):
        scale_fn = MagicMock(side_effect=RuntimeError("scale failed"))
        result = safe_scale_to_zero("ep-123", scale_fn, logger=None)
        assert result is False

    def test_logger_called_on_failure(self):
        scale_fn = MagicMock(side_effect=RuntimeError("scale failed"))
        logger = MagicMock()
        safe_scale_to_zero("ep-123", scale_fn, logger=logger)
        logger.assert_called_once()

    def test_logger_called_on_success(self):
        scale_fn = MagicMock()
        logger = MagicMock()
        safe_scale_to_zero("ep-123", scale_fn, logger=logger)
        logger.assert_called_once()


# ---------------------------------------------------------------------------
# Receipt-friendly timeout formatting
# ---------------------------------------------------------------------------


class TestFormatTimeoutReceipt:
    def test_basic_fields(self):
        receipt = format_timeout_receipt(1860, idle_timeout=300)
        assert receipt["executionTimeout"] == 1860
        assert receipt["idleTimeout"] == 300
        assert receipt["handler_deadline_s"] == 1800
        assert receipt["slack_s"] == 60
        assert receipt["meets_min_requirement"] is True
        assert receipt["min_required_execution_timeout"] == 1860

    def test_meets_min_requirement_false_when_below(self):
        receipt = format_timeout_receipt(600, idle_timeout=300)
        assert receipt["meets_min_requirement"] is False
        assert receipt["slack_s"] == -1200

    def test_note_is_present(self):
        receipt = format_timeout_receipt(1860)
        assert "note" in receipt
        assert isinstance(receipt["note"], str)
        assert len(receipt["note"]) > 0

    def test_custom_deadline(self):
        receipt = format_timeout_receipt(3720, idle_timeout=300, deadline_s=3600)
        assert receipt["handler_deadline_s"] == 3600
        assert receipt["slack_s"] == 120


# ---------------------------------------------------------------------------
# Integration: EndpointConfig + build_endpoint_input
# ---------------------------------------------------------------------------


class TestEndpointConfigIntegration:
    def test_full_config_produces_valid_input(self):
        config = EndpointConfig(
            name="qf-canary-abc-123",
            template_id="tpl-xyz",
            gpu_ids="ADA_24",
            workers_min=1,
            workers_max=1,
            idle_timeout=300,
            execution_timeout=1860,
            scaler_type="QUEUE_DELAY",
            scaler_value=4,
        )
        inp = build_endpoint_input(config)
        assert inp["name"] == "qf-canary-abc-123"
        assert inp["templateId"] == "tpl-xyz"
        assert inp["executionTimeout"] == 1860
        assert inp["idleTimeout"] == 300
        assert inp["gpuIds"] == "ADA_24"

    def test_default_config_meets_min_timeout(self):
        config = EndpointConfig(name="ep", template_id="tpl")
        assert config.execution_timeout >= MIN_EXECUTION_TIMEOUT_S
