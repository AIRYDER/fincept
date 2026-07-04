"""Regression guard: the RunPod training worker Dockerfile must not reintroduce
an import-based Docker ``HEALTHCHECK``.

Background: an earlier revision of
``runpod/quant-foundry-training/Dockerfile`` defined a ``HEALTHCHECK`` that ran
``python -c "import handler"``. On RunPod serverless that import side-effect
broke job dispatch — the worker showed ``ready=1, idle=1`` but submitted jobs
stayed ``IN_QUEUE`` and the endpoint eventually went unhealthy. The fix was to
remove the ``HEALTHCHECK`` entirely (RunPod does its own worker health
management). See ``runpod/RUNPOD_UNHEALTHY_ROOT_CAUSE.md``.

These tests ensure the offending pattern cannot be silently reintroduced:

  * ``test_dockerfile_has_no_import_based_healthcheck`` — the real Dockerfile
    must be clean.
  * ``test_detector_catches_import_based_healthcheck`` — a synthetic
    Dockerfile containing ``HEALTHCHECK CMD python -c "import handler"`` is
    flagged (sanity check for the detector, without touching the real file).
  * ``test_detector_allows_healthcheck_none`` and
    ``test_detector_allows_commented_healthcheck`` — the allowed states do
    not trigger the guard.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add runpod/tests to sys.path so the local helper module is importable
# regardless of pytest's rootdir / import-mode.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from healthcheck_guard import find_import_based_healthcheck  # noqa: E402

# Resolve the real Dockerfile relative to this test file so the test works
# regardless of the pytest invocation's current working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "runpod" / "quant-foundry-training" / "Dockerfile"


def test_dockerfile_has_no_import_based_healthcheck() -> None:
    """The real training worker Dockerfile must not define an import-based
    Docker HEALTHCHECK directive.

    RunPod serverless manages worker health itself; a Docker HEALTHCHECK that
    imports the worker module previously broke job dispatch. If this test
    fails, remove the HEALTHCHECK (or replace it with ``HEALTHCHECK NONE``)
    and re-run the layer-0 probe before reintroducing any healthcheck.
    """
    assert _DOCKERFILE.is_file(), f"Dockerfile not found at {_DOCKERFILE}"
    offending = find_import_based_healthcheck(_DOCKERFILE)
    assert offending == [], (
        "runpod/quant-foundry-training/Dockerfile reintroduced an "
        "import-based HEALTHCHECK, which previously broke RunPod serverless "
        f"job dispatch. Offending line(s): {offending}"
    )


def test_detector_catches_import_based_healthcheck(tmp_path: Path) -> None:
    """The detector flags a synthetic Dockerfile with an import-based
    HEALTHCHECK (sanity check; does not touch the real Dockerfile)."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        'FROM python:3.12-slim\nHEALTHCHECK CMD python -c "import handler"\n',
        encoding="utf-8",
    )
    offending = find_import_based_healthcheck(dockerfile)
    assert len(offending) == 1
    assert "HEALTHCHECK" in offending[0]
    assert "import handler" in offending[0]


def test_detector_catches_python3_import_variant(tmp_path: Path) -> None:
    """The detector also flags ``python3 -c 'import ...'`` variants."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.12-slim\nHEALTHCHECK --interval=30s CMD python3 -c 'import quant_foundry'\n",
        encoding="utf-8",
    )
    offending = find_import_based_healthcheck(dockerfile)
    assert len(offending) == 1


def test_detector_allows_healthcheck_none(tmp_path: Path) -> None:
    """``HEALTHCHECK NONE`` is explicitly allowed and must not be flagged."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.12-slim\nHEALTHCHECK NONE\n",
        encoding="utf-8",
    )
    assert find_import_based_healthcheck(dockerfile) == []


def test_detector_allows_commented_healthcheck(tmp_path: Path) -> None:
    """A commented-out HEALTHCHECK line must not be flagged."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        'FROM python:3.12-slim\n# HEALTHCHECK CMD python -c "import handler"\n',
        encoding="utf-8",
    )
    assert find_import_based_healthcheck(dockerfile) == []


def test_detector_allows_non_import_healthcheck(tmp_path: Path) -> None:
    """A non-import HEALTHCHECK (e.g. curl) is not the pattern that broke
    dispatch and must not be flagged by this import-specific guard."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        "FROM python:3.12-slim\n"
        "HEALTHCHECK --interval=30s CMD curl -f http://localhost:8080/health || exit 1\n",
        encoding="utf-8",
    )
    assert find_import_based_healthcheck(dockerfile) == []


def test_detector_clean_when_no_healthcheck(tmp_path: Path) -> None:
    """A Dockerfile with no HEALTHCHECK directive at all is clean."""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        'FROM python:3.12-slim\nENTRYPOINT ["python", "-u", "/worker/handler.py"]\n',
        encoding="utf-8",
    )
    assert find_import_based_healthcheck(dockerfile) == []


if __name__ == "__main__":
    # Allow direct execution for quick manual checks.
    pytest.main([__file__, "-q"])
