"""Source-inspection matrix proving every service entrypoint applies the
runtime safety guard.

The runtime guard ``assert_safe_for_runtime`` is the single invariant
that makes "I forgot to set ``FINCEPT_JWT_SECRET`` in production"
impossible to deploy by mistake (audit R4 / P3).  A guard that only
exists in the API is not enough: ingestor, orchestrator, OMS, strategy
host, features, jobs, portfolio, and every long-running agent can touch
Redis, streams, schedulers, and broker-adjacent clients.  Each of those
entrypoints must call the guard after ``get_settings()`` and before any
side effect.

These tests are deliberately source-inspection based (not behaviour
based) so they fail the moment a future commit drops the guard from any
entrypoint — even if that entrypoint has no other unit test.  This is
the regression net TASK-0102 asks for.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

# Every long-running entrypoint that can touch Redis, streams,
# schedulers, or broker-adjacent clients.  Paths are relative to the
# repo root.  Add new entrypoints here when they are created; the test
# will then enforce the guard on them too.
SERVICE_ENTRYPOINTS: list[str] = [
    "services/api/src/api/main.py",
    "services/ingestor/src/ingestor/main.py",
    "services/orchestrator/src/orchestrator/main.py",
    "services/oms/src/oms/main.py",
    "services/strategy_host/src/strategy_host/main.py",
    "services/features/src/features/main.py",
    "services/jobs/src/jobs/main.py",
    "services/portfolio/src/portfolio/main.py",
    "services/agents/src/agents/gbm_predictor/main.py",
    "services/agents/src/agents/sentiment_agent/main.py",
    "services/agents/src/agents/regime_agent/main.py",
    "services/agents/src/agents/information_enricher/main.py",
    "services/agents/src/agents/sentiment_features/main.py",
    "services/agents/src/agents/news_alpha_predictor/main.py",
    "services/agents/src/agents/news_outcome_labeler/main.py",
    "services/agents/src/agents/news_impact_agent/main.py",
]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _entrypoint_path(rel: str) -> pathlib.Path:
    return REPO_ROOT / rel


def _file_calls_guard(path: pathlib.Path) -> bool:
    """Return True if *path* imports and calls ``assert_safe_for_runtime``."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imported_alias: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "fincept_core.config":
                for alias in node.names:
                    if alias.name == "assert_safe_for_runtime":
                        imported_alias = alias.asname or alias.name
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == imported_alias:
                return True
            if isinstance(func, ast.Attribute) and func.attr == "assert_safe_for_runtime":
                return True
    return False


@pytest.mark.parametrize("rel", SERVICE_ENTRYPOINTS)
def test_entrypoint_applies_runtime_guard(rel: str) -> None:
    """Each listed entrypoint must import + call ``assert_safe_for_runtime``.

    If you intentionally add a new entrypoint that does not need the
    guard (e.g. a pure CLI with no Redis/stream/broker access), document
    the exception in this test rather than weakening the matrix.
    """
    path = _entrypoint_path(rel)
    assert path.exists(), f"entrypoint not found: {rel}"
    assert _file_calls_guard(path), (
        f"{rel} does not call assert_safe_for_runtime; every service "
        "entrypoint that touches Redis/streams/schedulers/broker must "
        "fail closed on the dev JWT secret in non-dev envs. See "
        "libs/fincept-core/src/fincept_core/config.py and audit R4/P3."
    )


def test_guard_fails_closed_on_dev_secret_in_non_dev_env(monkeypatch) -> None:
    """Behaviour check: the guard itself rejects dev defaults in staging."""
    from fincept_core.config import Settings, assert_safe_for_runtime, get_settings
    from fincept_core.errors import ConfigError

    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_ENV", "staging")
    monkeypatch.setenv("FINCEPT_JWT_SECRET", "dev-only-change-me")
    with pytest.raises(ConfigError):
        assert_safe_for_runtime(get_settings())


def test_guard_allows_dev_secret_in_dev_env(monkeypatch) -> None:
    """Behaviour check: dev/local/test keep working with the dev default."""
    from fincept_core.config import Settings, assert_safe_for_runtime, get_settings

    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_ENV", "dev")
    monkeypatch.setenv("FINCEPT_JWT_SECRET", "dev-only-change-me")
    assert_safe_for_runtime(get_settings())  # must not raise
