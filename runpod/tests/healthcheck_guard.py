"""Guard helpers for the RunPod training worker Dockerfile.

The RunPod serverless training worker (``runpod/quant-foundry-training/Dockerfile``)
must NOT define an import-based Docker ``HEALTHCHECK`` directive. An earlier
revision used a ``HEALTHCHECK`` that ran ``python -c "import handler"`` (or
similar) to probe liveness. On RunPod serverless that pattern broke job
dispatch: the worker reported "ready" but jobs stayed ``IN_QUEUE`` because the
healthcheck import side-effects interfered with the runpod SDK's job-delivery
webhook. See ``runpod/RUNPOD_UNHEALTHY_ROOT_CAUSE.md``.

This module provides :func:`find_import_based_healthcheck`, a pure detector
that returns any offending ``HEALTHCHECK`` lines. The companion pytest module
``test_dockerfile_no_healthcheck.py`` asserts the real Dockerfile is clean.

Allowed (non-offending) states:
  * no ``HEALTHCHECK`` directive at all
  * ``HEALTHCHECK NONE``
  * ``HEALTHCHECK`` lines that are commented out (line starts with ``#``)
  * a non-import ``HEALTHCHECK`` (e.g. one that shells out to ``curl``) — this
    guard only blocks the *import-based* pattern that broke dispatch, not every
    conceivable healthcheck.
"""

from __future__ import annotations

import re
from pathlib import Path

# A directive line is a HEALTHCHECK directive if, after stripping leading
# whitespace, it begins with ``HEALTHCHECK`` (case-insensitive). Comment lines
# (starting with ``#``) are never directives.
_HEALTHCHECK_DIRECTIVE_RE = re.compile(r"^\s*HEALTHCHECK\b", re.IGNORECASE)

# ``HEALTHCHECK NONE`` (with optional flags before NONE) disables the
# inherited healthcheck and is explicitly allowed.
_NONE_RE = re.compile(r"^\s*HEALTHCHECK\b.*\bNONE\b\s*$", re.IGNORECASE)

# A Python interpreter invocation. Matches ``python`` or ``python3`` (optionally
# with a patch suffix like ``python3.12``) as a word boundary.
_PYTHON_INVOCATION_RE = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b", re.IGNORECASE)

# An ``import`` statement inside a ``python -c "..."`` snippet, e.g.
# ``python -c "import handler"`` or ``python -c 'import quant_foundry'``.
_IMPORT_RE = re.compile(r"\bimport\b")


def _is_comment(line: str) -> bool:
    """Return True if the line is a Dockerfile comment (starts with ``#``)."""
    return line.lstrip().startswith("#")


def _is_import_based_healthcheck(line: str) -> bool:
    """Return True if a single line is an import-based HEALTHCHECK directive.

    A line is offending when ALL of the following hold:
      * it is not a comment,
      * it is a ``HEALTHCHECK`` directive (not ``HEALTHCHECK NONE``), and
      * its command portion invokes a Python interpreter that runs an
        ``import`` statement (the pattern that broke RunPod job dispatch).
    """
    if _is_comment(line):
        return False
    if not _HEALTHCHECK_DIRECTIVE_RE.match(line):
        return False
    # ``HEALTHCHECK NONE`` is explicitly allowed.
    if _NONE_RE.match(line):
        return False
    # Only the *import-based* pattern is blocked. A healthcheck that shells
    # out to ``curl`` (or any non-Python probe) is not what broke dispatch.
    return bool(_PYTHON_INVOCATION_RE.search(line) and _IMPORT_RE.search(line))


def find_import_based_healthcheck(dockerfile_path: str | Path) -> list[str]:
    """Return the list of offending import-based HEALTHCHECK lines.

    Reads the Dockerfile at ``dockerfile_path`` and returns every
    non-comment ``HEALTHCHECK`` directive whose command runs a Python
    ``import``. An empty list means the Dockerfile is clean.

    Args:
        dockerfile_path: Path to the Dockerfile to inspect.

    Returns:
        The offending lines (verbatim, including any leading whitespace).
    """
    path = Path(dockerfile_path)
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if _is_import_based_healthcheck(line)]
