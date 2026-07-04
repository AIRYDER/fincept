"""Static validation tests for the slim training worker Dockerfile.

These tests validate ``runpod/quant-foundry-training/Dockerfile.slim`` — a
size-optimised variant of the production Dockerfile that drops the ~2 GB
torch CUDA wheel for lightgbm/xgboost/catboost-only training (Tier 0.4).

All checks are **static** (no Docker build) because Docker is not available
in the local CI environment. They parse the Dockerfile text directly.

Validated properties:
  * Base image is ``python:3.12-slim`` (never nvidia/cuda, pytorch/pytorch,
    or runpod/base — proven to break RunPod job dispatch).
  * No import-based Docker ``HEALTHCHECK`` (reuses the existing
    ``healthcheck_guard.py`` detector that guards the production Dockerfile).
  * torch is NOT installed (the whole point of the slim variant).
  * lightgbm, xgboost, and runpod ARE installed (the dispatch path + tree
    model training must still work).
  * ENTRYPOINT matches the production Dockerfile exactly.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

# Add runpod/tests to sys.path so the local helper module is importable
# regardless of pytest's rootdir / import-mode.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from healthcheck_guard import find_import_based_healthcheck  # noqa: E402

# Resolve Dockerfiles relative to this test file so the tests work regardless
# of the pytest invocation's current working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SLIM_DOCKERFILE = _REPO_ROOT / "runpod" / "quant-foundry-training" / "Dockerfile.slim"
_PROD_DOCKERFILE = _REPO_ROOT / "runpod" / "quant-foundry-training" / "Dockerfile"

# Base images that are PROVEN to break RunPod job dispatch. The slim
# Dockerfile must never use any of these as its FROM base.
_FORBIDDEN_BASES = (
    "nvidia/cuda",
    "pytorch/pytorch",
    "runpod/base",
)


def _read_dockerfile(path: Path) -> str:
    """Read a Dockerfile, asserting it exists first."""
    assert path.is_file(), f"Dockerfile not found at {path}"
    return path.read_text(encoding="utf-8")


def _from_lines(text: str) -> list[str]:
    """Return all non-comment FROM directive lines from Dockerfile text."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"^FROM\s", stripped, re.IGNORECASE):
            lines.append(stripped)
    return lines


def _pip_install_lines(text: str) -> list[str]:
    """Return all non-comment lines that are part of pip install commands.

    A pip install command may span multiple lines (backslash continuation).
    This helper joins continuation lines and returns one string per
    complete ``RUN pip install ...`` statement.
    """
    statements: list[str] = []
    current: list[str] = []
    in_pip_install = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"^RUN\s+pip\s+install", stripped, re.IGNORECASE):
            in_pip_install = True
            current = [stripped]
        elif in_pip_install:
            current.append(stripped)
        # A continuation line ends when the previous line did not end with
        # a backslash. We detect completion by checking the raw line.
        if in_pip_install and not line.rstrip().endswith("\\"):
            statements.append("\n".join(current))
            current = []
            in_pip_install = False
    return statements


# ---------------------------------------------------------------------------
# Base image tests
# ---------------------------------------------------------------------------


def test_slim_uses_python_312_slim_base() -> None:
    """The slim Dockerfile must use python:3.12-slim as its base image.

    Controlled A/B tests (2026-07-03) proved nvidia/cuda, pytorch/pytorch,
    and runpod/base all break RunPod serverless job dispatch. Only
    python:3.12-slim reliably dispatches jobs.
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    from_lines = _from_lines(text)
    assert len(from_lines) >= 1, "Dockerfile.slim has no FROM directive"
    # The first FROM is the base image (no multi-stage build expected here).
    first_from = from_lines[0]
    assert "python:3.12-slim" in first_from, (
        f"Dockerfile.slim base image is not python:3.12-slim: {first_from!r}"
    )


def test_slim_does_not_use_forbidden_base() -> None:
    """The slim Dockerfile must never use nvidia/cuda, pytorch/pytorch, or
    runpod/base as a base image — all proven to break RunPod dispatch."""
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    from_lines = _from_lines(text)
    for line in from_lines:
        for forbidden in _FORBIDDEN_BASES:
            assert forbidden not in line.lower(), (
                f"Dockerfile.slim uses forbidden base image {forbidden!r} in line: {line!r}"
            )


# ---------------------------------------------------------------------------
# HEALTHCHECK tests (reuse the existing detector)
# ---------------------------------------------------------------------------


def test_slim_has_no_import_based_healthcheck() -> None:
    """The slim Dockerfile must not define an import-based HEALTHCHECK.

    RunPod serverless manages worker health itself; an import-based
    HEALTHCHECK previously broke job dispatch (worker showed ready=1 but
    jobs stayed IN_QUEUE). This reuses the same detector that guards the
    production Dockerfile.
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    offending = find_import_based_healthcheck(_SLIM_DOCKERFILE)
    assert offending == [], (
        "Dockerfile.slim reintroduced an import-based HEALTHCHECK, which "
        f"broke RunPod job dispatch. Offending line(s): {offending}"
    )


def test_slim_has_no_healthcheck_directive_at_all() -> None:
    """The slim Dockerfile should have no HEALTHCHECK directive whatsoever.

    While the import-based guard only blocks the specific pattern that
    broke dispatch, the slim variant — like the production Dockerfile —
    should have no HEALTHCHECK at all. This is a stricter check.
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.match(r"^HEALTHCHECK\b", stripped, re.IGNORECASE), (
            f"Dockerfile.slim defines a HEALTHCHECK directive: {line!r}. "
            "RunPod manages worker health; remove the HEALTHCHECK."
        )


# ---------------------------------------------------------------------------
# torch exclusion tests
# ---------------------------------------------------------------------------


def test_slim_does_not_install_torch() -> None:
    """The slim Dockerfile must NOT install torch.

    The torch==2.4.1 CUDA 12.4 wheel is ~2 GB and is the single largest
    layer in the production image. Dropping it is the entire point of the
    slim variant. Any pip install line mentioning torch is a regression.
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    pip_statements = _pip_install_lines(text)
    for stmt in pip_statements:
        # Match torch as a package name, not as a substring of another word.
        # ``torch`` appears in package specs like ``torch==2.4.1`` or
        # ``"torch>=2.0"``. We check for the word boundary.
        assert not re.search(r"\btorch\b", stmt, re.IGNORECASE), (
            f"Dockerfile.slim installs torch in pip statement:\n{stmt}\n"
            "The slim variant must NOT install torch — use the production "
            "-torch Dockerfile for NN work."
        )


def test_slim_does_not_reference_pytorch_index_url() -> None:
    """The slim Dockerfile must not use the pytorch.org wheel index in any
    pip install command.

    The production Dockerfile uses ``--index-url
    https://download.pytorch.org/whl/cu124`` for the torch install. The
    slim variant has no torch, so no pip install statement should reference
    the pytorch wheel index. (Comments may mention it to explain what was
    dropped — only actual RUN pip install lines are checked.)
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    pip_statements = _pip_install_lines(text)
    for stmt in pip_statements:
        assert "download.pytorch.org" not in stmt, (
            "Dockerfile.slim uses the pytorch wheel index in a pip install "
            f"statement:\n{stmt}\nThe slim variant should install from the "
            "default PyPI index since torch is not installed."
        )


# ---------------------------------------------------------------------------
# Required dependency tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "package",
    ["lightgbm", "xgboost", "runpod"],
)
def test_slim_installs_required_package(package: str) -> None:
    """The slim Dockerfile must install lightgbm, xgboost, and runpod.

    These are the core dispatch + tree-model training dependencies. runpod
    is the serverless SDK that delivers jobs to the worker. lightgbm and
    xgboost are the tree-model trainers. Dropping any of these breaks the
    working dispatch path.
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    pip_statements = _pip_install_lines(text)
    all_pip_text = "\n".join(pip_statements)
    assert re.search(rf"\b{re.escape(package)}\b", all_pip_text, re.IGNORECASE), (
        f"Dockerfile.slim does not install {package}. The slim variant must "
        "keep lightgbm, xgboost, and runpod for the working dispatch path."
    )


@pytest.mark.parametrize(
    "package",
    [
        "catboost",
        "pandas",
        "pyarrow",
        "scikit-learn",
        "numpy",
        "pydantic",
        "pydantic-settings",
        "httpx",
    ],
)
def test_slim_installs_supporting_package(package: str) -> None:
    """The slim Dockerfile must keep the supporting ML/data dependencies.

    catboost (CPU), pandas, pyarrow, scikit-learn, numpy are used by the
    tree-model training pipeline. pydantic/pydantic-settings/httpx are
    handler runtime deps. These match the production Dockerfile's pip
    install block (minus torch).
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    pip_statements = _pip_install_lines(text)
    all_pip_text = "\n".join(pip_statements)
    # scikit-learn is imported as sklearn but installed as scikit-learn.
    # pydantic-settings is a separate package. Use word-boundary match.
    assert re.search(rf"\b{re.escape(package)}\b", all_pip_text, re.IGNORECASE), (
        f"Dockerfile.slim does not install {package}. The slim variant must "
        "keep the same supporting dependencies as the production Dockerfile."
    )


# ---------------------------------------------------------------------------
# ENTRYPOINT parity test
# ---------------------------------------------------------------------------


def test_slim_entrypoint_matches_production() -> None:
    """The slim Dockerfile ENTRYPOINT must match the production Dockerfile.

    Both must use ``ENTRYPOINT ["python", "-u", "/worker/handler.py"]`` so
    RunPod's dockerArgs cannot override the entrypoint and the dispatch
    path is identical between the two image variants.
    """
    slim_text = _read_dockerfile(_SLIM_DOCKERFILE)
    prod_text = _read_dockerfile(_PROD_DOCKERFILE)

    _ENTRYPOINT_RE = re.compile(
        r'^ENTRYPOINT\s+\["python",\s*"-u",\s*"/worker/handler\.py"\]',
        re.IGNORECASE,
    )

    def _find_entrypoint(text: str) -> str | None:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if _ENTRYPOINT_RE.match(stripped):
                return stripped
        return None

    slim_entry = _find_entrypoint(slim_text)
    prod_entry = _find_entrypoint(prod_text)

    assert slim_entry is not None, (
        "Dockerfile.slim has no ENTRYPOINT directive matching the expected "
        'ENTRYPOINT ["python", "-u", "/worker/handler.py"]'
    )
    assert prod_entry is not None, (
        "Production Dockerfile has no ENTRYPOINT directive — cannot compare."
    )
    assert slim_entry == prod_entry, (
        f"Dockerfile.slim ENTRYPOINT differs from production:\n"
        f"  slim: {slim_entry!r}\n  prod: {prod_entry!r}"
    )


# ---------------------------------------------------------------------------
# Structural parity tests
# ---------------------------------------------------------------------------


def test_slim_has_git_sha_arg() -> None:
    """The slim Dockerfile must define the GIT_SHA build arg for
    reproducibility, matching the production Dockerfile."""
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    assert re.search(r"^ARG\s+GIT_SHA", text, re.IGNORECASE | re.MULTILINE), (
        "Dockerfile.slim is missing the ARG GIT_SHA build arg."
    )


def test_slim_has_non_root_user() -> None:
    """The slim Dockerfile must create the non-root trainer user, matching
    the production Dockerfile's security posture."""
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    assert re.search(r"useradd.*\btrainer\b", text, re.IGNORECASE), (
        "Dockerfile.slim does not create the non-root 'trainer' user."
    )


def test_slim_copies_handler_and_preflight() -> None:
    """The slim Dockerfile must COPY handler.py and preflight.py, matching
    the production dispatch + security-preflight path."""
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    assert "handler.py" in text, "Dockerfile.slim does not COPY handler.py"
    assert "preflight.py" in text, "Dockerfile.slim does not COPY preflight.py"


def test_slim_copies_quant_foundry_source() -> None:
    """The slim Dockerfile must COPY the quant_foundry source tree, matching
    the production Dockerfile's source layout."""
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    assert "quant_foundry/" in text, "Dockerfile.slim does not COPY the quant_foundry source tree."


def test_slim_installs_libgomp1() -> None:
    """The slim Dockerfile must install libgomp1 (OpenMP runtime).

    Without libgomp1, xgboost/lightgbm/sklearn imports crash with
    'libgomp.so.1: cannot open shared object file'. This was the root cause
    of the 8c45c484 worker failure.
    """
    text = _read_dockerfile(_SLIM_DOCKERFILE)
    assert "libgomp1" in text, (
        "Dockerfile.slim does not install libgomp1 — xgboost/lightgbm/sklearn "
        "imports will crash without the OpenMP runtime."
    )


if __name__ == "__main__":
    # Allow direct execution for quick manual checks.
    pytest.main([__file__, "-q"])
