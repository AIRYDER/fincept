"""Tests for ``quant_foundry.security_preflight``.

Covers PreflightConfig validation, EnvVarCheck / PreflightResult construction,
SecurityPreflight env/callback/system/redaction/run/report behavior, the
``is_secret_like`` and ``redact_value`` helpers, and fail-closed semantics.
No real environment is mutated — all checks use explicit dicts.
"""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError
from quant_foundry.security_preflight import (
    DEFAULT_SECRET_ENV_PATTERNS,
    NOT_SET_PLACEHOLDER,
    REDACTED_PLACEHOLDER,
    EnvVarCheck,
    PreflightConfig,
    PreflightResult,
    SecurityPreflight,
    is_secret_like,
    redact_value,
)

# ---------------------------------------------------------------------------
# PreflightConfig
# ---------------------------------------------------------------------------


def test_preflight_config_defaults():
    cfg = PreflightConfig(allowed_callback_hosts=["api.example.com"])
    assert "REDIS_URL" in cfg.forbidden_env_vars
    assert "BROKER_URL" in cfg.forbidden_env_vars
    assert cfg.production_mode is True
    assert cfg.fail_closed is True
    assert cfg.allowed_callback_hosts == ["api.example.com"]


def test_preflight_config_default_secret_patterns():
    cfg = PreflightConfig(allowed_callback_hosts=["api.example.com"])
    assert any("SECRET" in p for p in cfg.secret_env_patterns)
    assert ".*_KEY.*" in cfg.secret_env_patterns


def test_preflight_config_forbidden_env_vars_non_empty():
    with pytest.raises(ValidationError):
        PreflightConfig(forbidden_env_vars=[], allowed_callback_hosts=["h"])


def test_preflight_config_frozen():
    cfg = PreflightConfig(allowed_callback_hosts=["h"])
    with pytest.raises(ValidationError):
        cfg.production_mode = False  # type: ignore[misc]


def test_preflight_config_extra_forbidden():
    with pytest.raises(ValidationError):
        PreflightConfig(allowed_callback_hosts=["h"], bogus_field=1)  # type: ignore[call-arg]


def test_preflight_config_custom_forbidden_vars():
    cfg = PreflightConfig(forbidden_env_vars=["MY_SECRET"], allowed_callback_hosts=["h"])
    assert cfg.forbidden_env_vars == ["MY_SECRET"]


def test_preflight_config_non_production_mode():
    cfg = PreflightConfig(allowed_callback_hosts=["h"], production_mode=False)
    assert cfg.production_mode is False


def test_preflight_config_fail_closed_false():
    cfg = PreflightConfig(allowed_callback_hosts=["h"], fail_closed=False)
    assert cfg.fail_closed is False


# ---------------------------------------------------------------------------
# EnvVarCheck
# ---------------------------------------------------------------------------


def test_env_var_check_construction():
    check = EnvVarCheck(
        name="REDIS_URL",
        present=True,
        forbidden=True,
        redacted_name="R*******L",
        redacted_value=REDACTED_PLACEHOLDER,
    )
    assert check.name == "REDIS_URL"
    assert check.present is True
    assert check.forbidden is True
    assert check.redacted_value == REDACTED_PLACEHOLDER


def test_env_var_check_frozen():
    check = EnvVarCheck(
        name="X",
        present=False,
        forbidden=False,
        redacted_name="X",
        redacted_value=NOT_SET_PLACEHOLDER,
    )
    with pytest.raises(ValidationError):
        check.present = True  # type: ignore[misc]


def test_env_var_check_extra_forbidden():
    with pytest.raises(ValidationError):
        EnvVarCheck(
            name="X",
            present=False,
            forbidden=False,
            redacted_name="X",
            redacted_value=NOT_SET_PLACEHOLDER,
            extra=1,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# PreflightResult
# ---------------------------------------------------------------------------


def test_preflight_result_construction():
    result = PreflightResult(
        passed=True,
        env_checks=[],
        callback_url_valid=True,
        callback_url_error=None,
        container_user="uid:1000",
        writable_dirs=["/tmp"],
        redacted_config={"k": "v"},
        failure_reasons=[],
        timestamp="2024-01-01T00:00:00+00:00",
    )
    assert result.passed is True
    assert result.callback_url_valid is True
    assert result.container_user == "uid:1000"


def test_preflight_result_frozen():
    result = PreflightResult(
        passed=True,
        env_checks=[],
        callback_url_valid=True,
        callback_url_error=None,
        container_user="u",
        writable_dirs=[],
        redacted_config={},
        failure_reasons=[],
        timestamp="t",
    )
    with pytest.raises(ValidationError):
        result.passed = False  # type: ignore[misc]


def test_preflight_result_extra_forbidden():
    with pytest.raises(ValidationError):
        PreflightResult(
            passed=True,
            env_checks=[],
            callback_url_valid=True,
            callback_url_error=None,
            container_user="u",
            writable_dirs=[],
            redacted_config={},
            failure_reasons=[],
            timestamp="t",
            extra=1,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# is_secret_like
# ---------------------------------------------------------------------------


def test_is_secret_like_matches_secret():
    assert is_secret_like("MY_SECRET", DEFAULT_SECRET_ENV_PATTERNS) is True


def test_is_secret_like_matches_key():
    assert is_secret_like("API_KEY", DEFAULT_SECRET_ENV_PATTERNS) is True


def test_is_secret_like_matches_password():
    assert is_secret_like("DB_PASSWORD", DEFAULT_SECRET_ENV_PATTERNS) is True


def test_is_secret_like_matches_token():
    assert is_secret_like("AUTH_TOKEN", DEFAULT_SECRET_ENV_PATTERNS) is True


def test_is_secret_like_matches_credential():
    assert is_secret_like("MY_CREDENTIAL", DEFAULT_SECRET_ENV_PATTERNS) is True


def test_is_secret_like_no_match():
    assert is_secret_like("PATH", DEFAULT_SECRET_ENV_PATTERNS) is False


def test_is_secret_like_empty_name():
    assert is_secret_like("", DEFAULT_SECRET_ENV_PATTERNS) is False


def test_is_secret_like_empty_patterns():
    assert is_secret_like("MY_SECRET", []) is False


def test_is_secret_like_case_insensitive():
    assert is_secret_like("my_secret", DEFAULT_SECRET_ENV_PATTERNS) is True


def test_is_secret_like_bad_pattern_ignored():
    # A malformed pattern should not crash; valid patterns still evaluated.
    assert is_secret_like("API_KEY", ["[bad", ".*_KEY.*"]) is True
    assert is_secret_like("PATH", ["[bad", ".*_KEY.*"]) is False


# ---------------------------------------------------------------------------
# redact_value
# ---------------------------------------------------------------------------


def test_redact_value_secret():
    assert (
        redact_value("API_SECRET", "supersecret", DEFAULT_SECRET_ENV_PATTERNS)
        == REDACTED_PLACEHOLDER
    )


def test_redact_value_non_secret():
    assert redact_value("PATH", "/usr/bin", DEFAULT_SECRET_ENV_PATTERNS) == "/usr/bin"


def test_redact_value_empty():
    assert redact_value("PATH", "", DEFAULT_SECRET_ENV_PATTERNS) == NOT_SET_PLACEHOLDER


def test_redact_value_secret_empty_still_redacted():
    # Secret-like name wins over the empty check.
    assert redact_value("API_SECRET", "", DEFAULT_SECRET_ENV_PATTERNS) == REDACTED_PLACEHOLDER


# ---------------------------------------------------------------------------
# SecurityPreflight.check_env_vars
# ---------------------------------------------------------------------------


def _make_preflight(**kwargs) -> SecurityPreflight:
    defaults = {"allowed_callback_hosts": ["api.example.com"]}
    defaults.update(kwargs)
    return SecurityPreflight(PreflightConfig(**defaults))


def test_check_env_vars_forbidden_present():
    preflight = _make_preflight()
    checks = preflight.check_env_vars({"REDIS_URL": "redis://prod:6379"})
    redis_check = next(c for c in checks if c.name == "REDIS_URL")
    assert redis_check.present is True
    assert redis_check.forbidden is True
    assert redis_check.redacted_value == REDACTED_PLACEHOLDER


def test_check_env_vars_forbidden_absent():
    preflight = _make_preflight()
    checks = preflight.check_env_vars({"PATH": "/usr/bin"})
    redis_check = next(c for c in checks if c.name == "REDIS_URL")
    assert redis_check.present is False
    assert redis_check.forbidden is True
    assert redis_check.redacted_value == NOT_SET_PLACEHOLDER


def test_check_env_vars_secret_like_redacted():
    preflight = _make_preflight()
    checks = preflight.check_env_vars({"MY_API_KEY": "abc123"})
    key_check = next(c for c in checks if c.name == "MY_API_KEY")
    assert key_check.present is True
    assert key_check.forbidden is False
    assert key_check.redacted_value == REDACTED_PLACEHOLDER


def test_check_env_vars_non_secret_visible():
    preflight = _make_preflight()
    checks = preflight.check_env_vars({"PATH": "/usr/bin"})
    # PATH is not forbidden and not secret-like, so it is not reported.
    names = [c.name for c in checks]
    assert "PATH" not in names


def test_check_env_vars_empty_env():
    preflight = _make_preflight()
    checks = preflight.check_env_vars({})
    # All forbidden vars reported as absent.
    assert all(not c.present for c in checks)
    assert len(checks) == len(preflight.config.forbidden_env_vars)


def test_check_env_vars_all_forbidden_present():
    preflight = _make_preflight()
    env = {name: "value" for name in preflight.config.forbidden_env_vars}
    checks = preflight.check_env_vars(env)
    assert all(c.present for c in checks)
    assert all(c.forbidden for c in checks)
    assert all(c.redacted_value == REDACTED_PLACEHOLDER for c in checks)


def test_check_env_vars_secret_name_redacted_in_name():
    preflight = _make_preflight()
    checks = preflight.check_env_vars({"MY_API_KEY": "abc123"})
    key_check = next(c for c in checks if c.name == "MY_API_KEY")
    # Redacted name should differ from raw name and contain mask chars.
    assert key_check.redacted_name != "MY_API_KEY"
    assert "*" in key_check.redacted_name


# ---------------------------------------------------------------------------
# SecurityPreflight.validate_callback_url
# ---------------------------------------------------------------------------


def test_validate_callback_url_valid():
    preflight = _make_preflight()
    valid, error = preflight.validate_callback_url("https://api.example.com/cb")
    assert valid is True
    assert error is None


def test_validate_callback_url_invalid_host():
    preflight = _make_preflight()
    valid, error = preflight.validate_callback_url("https://evil.example.com/cb")
    assert valid is False
    assert error is not None
    assert "evil.example.com" in error


def test_validate_callback_url_non_https_in_production():
    preflight = _make_preflight()
    valid, error = preflight.validate_callback_url("http://api.example.com/cb")
    assert valid is False
    assert error is not None
    assert "HTTPS" in error


def test_validate_callback_url_http_allowed_non_production():
    preflight = _make_preflight(production_mode=False)
    valid, error = preflight.validate_callback_url("http://api.example.com/cb")
    assert valid is True
    assert error is None


def test_validate_callback_url_empty():
    preflight = _make_preflight()
    valid, error = preflight.validate_callback_url("")
    assert valid is False
    assert error is not None


def test_validate_callback_url_no_scheme():
    preflight = _make_preflight()
    valid, error = preflight.validate_callback_url("api.example.com/cb")
    assert valid is False
    assert error is not None


def test_validate_callback_url_no_host_allowlist_restriction():
    # When allowlist is empty, any host passes (but HTTPS still required).
    preflight = SecurityPreflight(PreflightConfig(allowed_callback_hosts=[]))
    valid, error = preflight.validate_callback_url("https://anywhere.example.com/cb")
    assert valid is True
    assert error is None


# ---------------------------------------------------------------------------
# SecurityPreflight.get_container_user
# ---------------------------------------------------------------------------


def test_get_container_user_returns_non_empty():
    preflight = _make_preflight()
    user = preflight.get_container_user()
    assert isinstance(user, str)
    assert len(user) > 0


# ---------------------------------------------------------------------------
# SecurityPreflight.get_writable_dirs
# ---------------------------------------------------------------------------


def test_get_writable_dirs_returns_list():
    preflight = _make_preflight()
    dirs = preflight.get_writable_dirs()
    assert isinstance(dirs, list)
    # At least the cwd should be writable in the test environment.
    assert len(dirs) >= 1


def test_get_writable_dirs_are_unique_and_sorted():
    preflight = _make_preflight()
    dirs = preflight.get_writable_dirs()
    assert dirs == sorted(set(dirs))


# ---------------------------------------------------------------------------
# SecurityPreflight.redact_config
# ---------------------------------------------------------------------------


def test_redact_config_secrets_redacted():
    preflight = _make_preflight()
    out = preflight.redact_config({"API_SECRET": "s3cr3t", "DB_PASSWORD": "hunter2"})
    assert out["API_SECRET"] == REDACTED_PLACEHOLDER
    assert out["DB_PASSWORD"] == REDACTED_PLACEHOLDER


def test_redact_config_non_secrets_visible():
    preflight = _make_preflight()
    out = preflight.redact_config({"MODEL_FAMILY": "gbm", "EPOCHS": "10"})
    assert out["MODEL_FAMILY"] == "gbm"
    assert out["EPOCHS"] == "10"


def test_redact_config_empty_value():
    preflight = _make_preflight()
    out = preflight.redact_config({"MODEL_FAMILY": ""})
    assert out["MODEL_FAMILY"] == NOT_SET_PLACEHOLDER


def test_redact_config_does_not_mutate_input():
    preflight = _make_preflight()
    cfg = {"API_SECRET": "s3cr3t", "MODEL_FAMILY": "gbm"}
    preflight.redact_config(cfg)
    assert cfg["API_SECRET"] == "s3cr3t"
    assert cfg["MODEL_FAMILY"] == "gbm"


# ---------------------------------------------------------------------------
# SecurityPreflight.run
# ---------------------------------------------------------------------------


def test_run_pass():
    preflight = _make_preflight()
    result = preflight.run(
        env={"PATH": "/usr/bin"},
        callback_url="https://api.example.com/cb",
        config={"MODEL_FAMILY": "gbm"},
    )
    assert result.passed is True
    assert result.failure_reasons == []
    assert result.callback_url_valid is True
    assert result.redacted_config["MODEL_FAMILY"] == "gbm"


def test_run_fail_on_forbidden_env():
    preflight = _make_preflight()
    result = preflight.run(
        env={"REDIS_URL": "redis://prod:6379"},
        callback_url="https://api.example.com/cb",
        config={},
    )
    assert result.passed is False
    assert any("forbidden" in r for r in result.failure_reasons)


def test_run_fail_on_broker_secret():
    preflight = _make_preflight()
    result = preflight.run(
        env={"BROKER_URL": "amqp://broker:5672"},
        callback_url="https://api.example.com/cb",
        config={},
    )
    assert result.passed is False
    assert any("forbidden" in r for r in result.failure_reasons)


def test_run_fail_on_callback_url():
    preflight = _make_preflight()
    result = preflight.run(
        env={"PATH": "/usr/bin"},
        callback_url="http://evil.example.com/cb",
        config={},
    )
    assert result.passed is False
    assert result.callback_url_valid is False
    assert result.callback_url_error is not None


def test_run_fail_closed_false_allows_forbidden():
    preflight = _make_preflight(fail_closed=False)
    result = preflight.run(
        env={"REDIS_URL": "redis://prod:6379"},
        callback_url="https://api.example.com/cb",
        config={},
    )
    # fail_closed=False means forbidden presence alone does not fail.
    assert result.passed is True


def test_run_redacts_secret_like_env_names():
    preflight = _make_preflight()
    result = preflight.run(
        env={"MY_API_KEY": "abc123"},
        callback_url="https://api.example.com/cb",
        config={},
    )
    key_check = next(c for c in result.env_checks if c.name == "MY_API_KEY")
    assert key_check.redacted_value == REDACTED_PLACEHOLDER
    assert "*" in key_check.redacted_name


def test_run_records_container_user_and_writable_dirs():
    preflight = _make_preflight()
    result = preflight.run(env={}, callback_url="https://api.example.com/cb", config={})
    assert isinstance(result.container_user, str)
    assert len(result.container_user) > 0
    assert isinstance(result.writable_dirs, list)


def test_run_timestamp_is_iso():
    preflight = _make_preflight()
    result = preflight.run(env={}, callback_url="https://api.example.com/cb", config={})
    # ISO-8601 with timezone offset.
    assert re.match(r"\d{4}-\d{2}-\d{2}T", result.timestamp)


def test_run_production_mode_rejects_http():
    preflight = _make_preflight()
    result = preflight.run(
        env={"PATH": "/usr/bin"},
        callback_url="http://api.example.com/cb",
        config={},
    )
    assert result.passed is False
    assert result.callback_url_valid is False


def test_run_non_production_mode_allows_http():
    preflight = _make_preflight(production_mode=False)
    result = preflight.run(
        env={"PATH": "/usr/bin"},
        callback_url="http://api.example.com/cb",
        config={},
    )
    assert result.passed is True
    assert result.callback_url_valid is True


def test_run_app_credentials_blocked_in_production():
    # TRADING_API_KEY + TRADING_API_SECRET are forbidden; production mode
    # must block startup.
    preflight = _make_preflight()
    result = preflight.run(
        env={
            "TRADING_API_KEY": "key123",
            "TRADING_API_SECRET": "sec456",
        },
        callback_url="https://api.example.com/cb",
        config={},
    )
    assert result.passed is False
    assert len(result.failure_reasons) >= 2


def test_run_empty_env_passes():
    preflight = _make_preflight()
    result = preflight.run(env={}, callback_url="https://api.example.com/cb", config={})
    assert result.passed is True


def test_run_all_secrets_no_forbidden_passes():
    # Secret-like but not forbidden vars are redacted but do not fail.
    preflight = _make_preflight()
    result = preflight.run(
        env={"MY_API_KEY": "abc", "MY_TOKEN": "xyz"},
        callback_url="https://api.example.com/cb",
        config={},
    )
    assert result.passed is True
    assert all(
        c.redacted_value == REDACTED_PLACEHOLDER
        for c in result.env_checks
        if c.present and not c.forbidden
    )


def test_run_multiple_failures_collected():
    preflight = _make_preflight()
    result = preflight.run(
        env={"REDIS_URL": "redis://prod:6379"},
        callback_url="http://evil.example.com/cb",
        config={},
    )
    assert result.passed is False
    # Both forbidden env and callback URL failures recorded.
    assert len(result.failure_reasons) >= 2


# ---------------------------------------------------------------------------
# SecurityPreflight.format_report
# ---------------------------------------------------------------------------


def test_format_report_pass():
    preflight = _make_preflight()
    result = preflight.run(
        env={"PATH": "/usr/bin"},
        callback_url="https://api.example.com/cb",
        config={"MODEL_FAMILY": "gbm"},
    )
    report = preflight.format_report(result)
    assert "PASS" in report
    assert "MODEL_FAMILY: gbm" in report
    assert REDACTED_PLACEHOLDER not in report or "REDACTED" in report


def test_format_report_fail():
    preflight = _make_preflight()
    result = preflight.run(
        env={"REDIS_URL": "redis://prod:6379"},
        callback_url="http://evil.example.com/cb",
        config={"API_SECRET": "s3cr3t"},
    )
    report = preflight.format_report(result)
    assert "FAIL" in report
    assert "forbidden" in report
    assert "s3cr3t" not in report
    assert REDACTED_PLACEHOLDER in report


def test_format_report_contains_container_user():
    preflight = _make_preflight()
    result = preflight.run(env={}, callback_url="https://api.example.com/cb", config={})
    report = preflight.format_report(result)
    assert "container_user" in report
    assert result.container_user in report


def test_format_report_contains_writable_dirs():
    preflight = _make_preflight()
    result = preflight.run(env={}, callback_url="https://api.example.com/cb", config={})
    report = preflight.format_report(result)
    assert "writable_dirs" in report


def test_format_report_no_raw_secret_values():
    preflight = _make_preflight()
    result = preflight.run(
        env={"MY_API_KEY": "super-secret-value-123"},
        callback_url="https://api.example.com/cb",
        config={"DB_PASSWORD": "hunter2"},
    )
    report = preflight.format_report(result)
    assert "super-secret-value-123" not in report
    assert "hunter2" not in report
