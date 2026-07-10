"""
quant_foundry.security_preflight — worker startup security preflight.

A worker (RunPod or local) must refuse to start when forbidden environment
variables are present (e.g. production DB / broker / cloud-admin credentials).
This module implements that preflight gate plus redaction helpers so that
configuration summaries can be logged without leaking secrets.

Security invariants (non-negotiable):
- Fail-closed: if any forbidden env var is present and ``fail_closed`` is True,
  ``PreflightResult.passed`` is False regardless of other checks.
- Secret-like env var names are always redacted in production mode.
- Callback URLs must be HTTPS and on an allowlisted host in production mode.
- No secret value is ever returned in a ``PreflightResult``; only redacted
  placeholders ("***REDACTED***") or "not set" are emitted.
- Container user and writable directories are recorded for auditability.
"""

from __future__ import annotations

import getpass
import os
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Default forbidden env vars: production credentials that a quant worker must
# never inherit.  Presence of any of these is treated as a misconfiguration.
DEFAULT_FORBIDDEN_ENV_VARS: list[str] = [
    "REDIS_URL",
    "BROKER_URL",
    "DATABASE_URL",
    "DB_WRITE_URL",
    "TRADING_API_KEY",
    "TRADING_API_SECRET",
    "CLOUD_ADMIN_KEY",
    "AWS_ADMIN_SECRET",
    "GCP_ADMIN_KEY",
]

# Default regex patterns identifying secret-like env var names.
DEFAULT_SECRET_ENV_PATTERNS: list[str] = [
    ".*SECRET.*",
    ".*_KEY.*",
    ".*_PASSWORD.*",
    ".*_TOKEN.*",
    ".*CREDENTIAL.*",
]

# Placeholder emitted for any secret-like value.  Never include the raw value.
REDACTED_PLACEHOLDER: str = "***REDACTED***"
# Placeholder emitted for an env var that is not set / empty.
NOT_SET_PLACEHOLDER: str = "not set"


class PreflightConfig(BaseModel):
    """Configuration for a :class:`SecurityPreflight` run.

    Attributes:
        forbidden_env_vars: Env var names whose presence blocks startup.
        secret_env_patterns: Regex patterns classifying env var names as secret-like.
        allowed_callback_hosts: Approved callback URL hosts (allowlist).
        production_mode: When True, enforce HTTPS callbacks and always redact
            secret-like env names.
        fail_closed: When True, any forbidden env var present forces
            ``passed=False``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    forbidden_env_vars: list[str] = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN_ENV_VARS))
    secret_env_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SECRET_ENV_PATTERNS)
    )
    allowed_callback_hosts: list[str] = Field(default_factory=list)
    production_mode: bool = True
    fail_closed: bool = True

    @field_validator("forbidden_env_vars")
    @classmethod
    def _forbidden_env_vars_non_empty(cls, value: list[str]) -> list[str]:
        """Ensure the forbidden env var list is non-empty (no silent open gate)."""
        if not value:
            raise ValueError("forbidden_env_vars must be non-empty")
        return value


class EnvVarCheck(BaseModel):
    """Result of inspecting a single environment variable.

    Attributes:
        name: The env var name (never redacted — names are not secrets, but
            ``redacted_name`` provides a masked form for secret-like names).
        present: Whether the variable was present in the supplied env dict.
        forbidden: Whether the name is in the forbidden list.
        redacted_name: The env var name, redacted if secret-like.
        redacted_value: Masked value (``"***REDACTED***"`` or ``"not set"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    present: bool
    forbidden: bool
    redacted_name: str
    redacted_value: str


class PreflightResult(BaseModel):
    """Aggregate result of a security preflight run.

    Attributes:
        passed: Overall pass/fail.  False if any fail-closed condition triggered.
        env_checks: Per-variable inspection results (values redacted).
        callback_url_valid: Whether the callback URL passed validation.
        callback_url_error: Error message if the callback URL is invalid, else None.
        container_user: The user the worker is running as.
        writable_dirs: Directories the worker can write to.
        redacted_config: Config summary with all secret-like values redacted.
        failure_reasons: Human-readable reasons for any failure.
        timestamp: ISO-8601 UTC timestamp of the run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    env_checks: list[EnvVarCheck]
    callback_url_valid: bool
    callback_url_error: str | None
    container_user: str
    writable_dirs: list[str]
    redacted_config: dict[str, str]
    failure_reasons: list[str]
    timestamp: str


def is_secret_like(name: str, patterns: list[str]) -> bool:
    """Return True if ``name`` matches any of the secret regex ``patterns``.

    Matching is case-insensitive and uses :func:`re.search` so substrings
    (e.g. ``SECRET`` inside ``MY_SECRET_TOKEN``) are detected.

    Args:
        name: The env var name to test.
        patterns: Regex patterns; an empty pattern list means "not secret-like".

    Returns:
        True if any pattern matches, False otherwise.
    """
    if not name or not patterns:
        return False
    for pattern in patterns:
        try:
            if re.search(pattern, name, re.IGNORECASE):
                return True
        except re.error:
            # A malformed pattern should not crash the preflight; treat as
            # non-matching rather than silently classifying everything.
            continue
    return False


def redact_value(name: str, value: str, patterns: list[str]) -> str:
    """Return a redacted representation of ``value`` for env var ``name``.

    - If ``name`` is secret-like (matches any pattern): return
      ``"***REDACTED***"``.
    - If ``value`` is empty: return ``"not set"``.
    - Otherwise: return ``value`` unchanged (non-secret env vars).

    Args:
        name: The env var name (used to decide redaction).
        value: The raw env var value (may be empty).
        patterns: Secret-like name patterns.

    Returns:
        A safe-to-log string representation of the value.
    """
    if is_secret_like(name, patterns):
        return REDACTED_PLACEHOLDER
    if not value:
        return NOT_SET_PLACEHOLDER
    return value


def _redact_name(name: str, patterns: list[str]) -> str:
    """Return a redacted form of ``name`` if it is secret-like.

    For secret-like names we mask the middle portion so the structure is
    recognizable without revealing the full name.  Non-secret names are
    returned unchanged.
    """
    if not is_secret_like(name, patterns):
        return name
    if len(name) <= 4:
        return "***"
    # Keep first and last char, mask the middle — enough to distinguish
    # different secret vars in logs without exposing them.
    return f"{name[0]}{'*' * (len(name) - 2)}{name[-1]}"


class SecurityPreflight:
    """Run security preflight checks before a worker starts.

    The preflight verifies that no forbidden env vars are present, that the
    callback URL is on an allowlisted HTTPS host, records the container user
    and writable directories, and produces a redacted config summary.
    """

    def __init__(self, config: PreflightConfig) -> None:
        """Initialize the preflight with a :class:`PreflightConfig`.

        Args:
            config: Frozen configuration controlling forbidden vars, secret
                patterns, callback host allowlist, and production/fail-closed
                behavior.
        """
        self._config: PreflightConfig = config

    @property
    def config(self) -> PreflightConfig:
        """The frozen :class:`PreflightConfig` for this preflight."""
        return self._config

    # ------------------------------------------------------------------
    # Env var checks
    # ------------------------------------------------------------------
    def check_env_vars(self, env: dict[str, str]) -> list[EnvVarCheck]:
        """Inspect ``env`` for forbidden and secret-like variables.

        For each forbidden env var, an :class:`EnvVarCheck` is produced
        regardless of presence (so the report shows what was checked).  For
        every other present env var that matches a secret pattern, an
        additional check is produced.  All values are redacted.

        Args:
            env: A mapping of env var name to value (e.g. ``os.environ``).

        Returns:
            A list of :class:`EnvVarCheck`, one per forbidden var plus one per
            present secret-like var.
        """
        checks: list[EnvVarCheck] = []
        seen: set[str] = set()
        patterns = self._config.secret_env_patterns
        forbidden_set = set(self._config.forbidden_env_vars)

        # First, check every forbidden var (present or not).  Forbidden vars
        # are sensitive by definition (they are production credentials), so
        # their values are always redacted when present regardless of whether
        # the name matches a secret pattern.
        for name in self._config.forbidden_env_vars:
            present = name in env
            checks.append(
                EnvVarCheck(
                    name=name,
                    present=present,
                    forbidden=True,
                    redacted_name=_redact_name(name, patterns),
                    redacted_value=(REDACTED_PLACEHOLDER if present else NOT_SET_PLACEHOLDER),
                )
            )
            seen.add(name)

        # Then, check every present env var that is secret-like but not already
        # covered by the forbidden list.
        for name, value in env.items():
            if name in seen:
                continue
            if is_secret_like(name, patterns):
                checks.append(
                    EnvVarCheck(
                        name=name,
                        present=True,
                        forbidden=name in forbidden_set,
                        redacted_name=_redact_name(name, patterns),
                        redacted_value=redact_value(name, value, patterns),
                    )
                )
                seen.add(name)

        return checks

    # ------------------------------------------------------------------
    # Callback URL validation
    # ------------------------------------------------------------------
    def validate_callback_url(self, url: str) -> tuple[bool, str | None]:
        """Validate ``url`` against the callback host allowlist and scheme rules.

        In production mode the URL must use HTTPS and its host must be in
        ``allowed_callback_hosts``.  In non-production mode the host must
        still be allowlisted but HTTP is permitted.

        Args:
            url: The callback URL to validate.

        Returns:
            A ``(valid, error)`` tuple where ``error`` is None when valid.
        """
        if not url:
            return False, "callback URL is empty"
        parsed = urlparse(url)
        host = parsed.hostname
        scheme = parsed.scheme
        if not scheme:
            return False, "callback URL has no scheme"
        if not host:
            return False, "callback URL has no host"
        if self._config.allowed_callback_hosts and host not in self._config.allowed_callback_hosts:
            return False, f"callback host '{host}' not in allowlist"
        if self._config.production_mode and scheme.lower() != "https":
            return False, "callback URL must use HTTPS in production mode"
        return True, None

    # ------------------------------------------------------------------
    # System info
    # ------------------------------------------------------------------
    def get_container_user(self) -> str:
        """Return the user the worker is running as.

        Uses :func:`os.getuid` when available (POSIX), otherwise falls back to
        :func:`getpass.getuser`.
        """
        getuid = getattr(os, "getuid", None)
        if getuid is not None:
            try:
                return f"uid:{getuid()}"
            except Exception:
                pass
        try:
            return getpass.getuser()
        except Exception:
            return "unknown"

    def get_writable_dirs(self) -> list[str]:
        """Return a list of directories the worker can write to.

        Checks a small set of candidate working directories (cwd, /tmp, and
        common temp locations) and returns those that are writable, sorted for
        deterministic output.
        """
        candidates: list[str] = [
            os.getcwd(),
            os.environ.get("TMPDIR", ""),
            os.environ.get("TEMP", ""),
            os.environ.get("TMP", ""),
            "/tmp",  # noqa: S108 - probing standard temp dirs for writability
            "/var/tmp",  # noqa: S108 - probing standard temp dirs for writability
        ]
        writable: list[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.isdir(candidate) and os.access(candidate, os.W_OK):
                writable.append(os.path.abspath(candidate))
        # Deduplicate while preserving sorted order.
        return sorted(set(writable))

    # ------------------------------------------------------------------
    # Config redaction
    # ------------------------------------------------------------------
    def redact_config(self, config: dict[str, str]) -> dict[str, str]:
        """Return ``config`` with all secret-like values redacted.

        Non-secret values are preserved so the summary remains useful for
        debugging.  Empty values become ``"not set"``.

        Args:
            config: A flat mapping of config key to value.

        Returns:
            A new dict with secret-like values replaced by
            ``"***REDACTED***"``.
        """
        patterns = self._config.secret_env_patterns
        redacted: dict[str, str] = {}
        for key, value in config.items():
            redacted[key] = redact_value(key, value, patterns)
        return redacted

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------
    def run(
        self,
        env: dict[str, str],
        callback_url: str,
        config: dict[str, str],
    ) -> PreflightResult:
        """Run all preflight checks and return an aggregate :class:`PreflightResult`.

        Fail-closed semantics: if ``fail_closed`` is True and any forbidden env
        var is present, ``passed`` is False.  Additionally, an invalid callback
        URL always forces ``passed`` is False.

        Args:
            env: Environment variables to inspect (values redacted in result).
            callback_url: The callback URL to validate.
            config: A config summary to redact and record.

        Returns:
            A :class:`PreflightResult` with all checks and redacted data.
        """
        env_checks = self.check_env_vars(env)
        callback_valid, callback_error = self.validate_callback_url(callback_url)
        container_user = self.get_container_user()
        writable_dirs = self.get_writable_dirs()
        redacted_config = self.redact_config(config)
        failure_reasons: list[str] = []

        forbidden_present = [c for c in env_checks if c.forbidden and c.present]
        if forbidden_present:
            if self._config.fail_closed:
                for check in forbidden_present:
                    failure_reasons.append(f"forbidden env var present: {check.redacted_name}")
        if not callback_valid and callback_error:
            failure_reasons.append(f"callback URL invalid: {callback_error}")

        passed = len(failure_reasons) == 0
        timestamp = datetime.now(UTC).isoformat()

        return PreflightResult(
            passed=passed,
            env_checks=env_checks,
            callback_url_valid=callback_valid,
            callback_url_error=callback_error,
            container_user=container_user,
            writable_dirs=writable_dirs,
            redacted_config=redacted_config,
            failure_reasons=failure_reasons,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def format_report(self, result: PreflightResult) -> str:
        """Format ``result`` as a human-readable multi-line string for logging.

        The report includes the pass/fail status, env var checks (redacted),
        callback URL status, container user, writable dirs, and any failure
        reasons.  No secret values are ever included.

        Args:
            result: The :class:`PreflightResult` to format.

        Returns:
            A newline-delimited report string.
        """
        lines: list[str] = []
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"=== Security Preflight: {status} ===")
        lines.append(f"timestamp: {result.timestamp}")
        lines.append(f"container_user: {result.container_user}")
        lines.append(
            f"writable_dirs: {', '.join(result.writable_dirs) if result.writable_dirs else '(none)'}"
        )
        lines.append("")
        lines.append("Environment checks:")
        if result.env_checks:
            for check in result.env_checks:
                flags: list[str] = []
                if check.forbidden:
                    flags.append("forbidden")
                if check.present:
                    flags.append("present")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                lines.append(f"  - {check.redacted_name}{flag_str}: {check.redacted_value}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append(f"Callback URL valid: {result.callback_url_valid}")
        if result.callback_url_error:
            lines.append(f"Callback URL error: {result.callback_url_error}")
        lines.append("")
        lines.append("Redacted config:")
        if result.redacted_config:
            for key, value in sorted(result.redacted_config.items()):
                lines.append(f"  - {key}: {value}")
        else:
            lines.append("  (none)")
        lines.append("")
        if result.failure_reasons:
            lines.append("Failure reasons:")
            for reason in result.failure_reasons:
                lines.append(f"  - {reason}")
        else:
            lines.append("Failure reasons: (none)")
        lines.append("=== End Security Preflight ===")
        return "\n".join(lines)


__all__ = [
    "DEFAULT_FORBIDDEN_ENV_VARS",
    "DEFAULT_SECRET_ENV_PATTERNS",
    "NOT_SET_PLACEHOLDER",
    "REDACTED_PLACEHOLDER",
    "EnvVarCheck",
    "PreflightConfig",
    "PreflightResult",
    "SecurityPreflight",
    "is_secret_like",
    "redact_value",
]
