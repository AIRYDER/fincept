"""Approved-root enforcement for dataset / model input paths.

This module owns the fail-closed gate every training, backtest, and
settlement input path must pass before it reaches the orchestrator.
It rejects:

* absolute paths that are not inside any approved root,
* traversal (``..``) anywhere in the candidate,
* symlink escape (by default -- symlinks are disallowed even when
  they currently resolve inside a root, to block TOCTOU swaps).

The approved-roots list is never echoed in error messages so a
probing caller cannot learn the on-disk layout from the failure.

The validation style mirrors ``fincept_core.prediction_log``: a small
explicit allow-list with a single ``ValueError`` subclass for
violations.  Default roots come from the
``FINCEPT_APPROVED_DATA_ROOTS`` env var (comma-separated); a missing
or empty var falls back to the fail-closed default ``["data",
"models"]``.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "ApprovedRoots",
    "ApprovedRootsError",
    "ResolvedPath",
    "default_approved_roots",
]

# Fail-closed default when the env var is absent / empty.  Kept as a
# tuple of relative names so a process whose cwd is the repo root
# (the normal launch posture) gets ``<cwd>/data`` and ``<cwd>/models``.
_DEFAULT_ROOTS: tuple[str, ...] = ("data", "models")
_ENV_VAR = "FINCEPT_APPROVED_DATA_ROOTS"


class ApprovedRootsError(ValueError):
    """A candidate path violated the approved-root gate.

    Subclass of :class:`ValueError` so existing ``except ValueError``
    handlers in callers keep catching it.  ``code`` is a short
    machine-readable string (``outside_root``, ``traversal``,
    ``symlink_escape``, ``no_roots``) the API layer maps to an HTTP
    problem-detail ``code`` field.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedPath:
    """A candidate that survived the gate.

    ``path`` is the absolute, symlink-resolved candidate.  ``inside_root``
    is the canonical (absolute, resolved) approved root it was found to
    live under; callers use it to compute a root-relative display path
    without re-running the gate.
    """

    path: pathlib.Path
    inside_root: pathlib.Path


class ApprovedRoots:
    """Fail-closed allowlist of filesystem roots.

    Roots are canonicalized to absolute paths at construction time.
    ``extra_dev_roots`` are honored alongside ``roots`` -- the test
    fixture uses them to admit a scratch directory without polluting
    the production default.

    The gate is intentionally strict: a candidate is accepted only if
    it contains no ``..`` component AND its resolved form lives under
    one of the approved roots AND (when ``allow_symlinks=False``) no
    component on the path from the root to the leaf is a symlink.
    """

    def __init__(
        self,
        *,
        roots: Sequence[pathlib.Path | str],
        extra_dev_roots: Sequence[pathlib.Path | str] = (),
    ) -> None:
        if not roots and not extra_dev_roots:
            raise ApprovedRootsError(
                "no_roots",
                "approved roots must not be empty (fail-closed)",
            )
        self._roots: tuple[pathlib.Path, ...] = tuple(
            _canonical_root(r) for r in roots
        )
        self._extra_dev_roots: tuple[pathlib.Path, ...] = tuple(
            _canonical_root(r) for r in extra_dev_roots
        )

    @property
    def roots(self) -> tuple[pathlib.Path, ...]:
        """The canonical approved roots (dev roots included)."""
        return self._roots + self._extra_dev_roots

    def resolve(
        self,
        candidate: pathlib.Path | str,
        *,
        allow_symlinks: bool = False,
    ) -> ResolvedPath:
        """Validate ``candidate`` and return its resolved form.

        Raises :class:`ApprovedRootsError` on any violation.  The
        approved-roots list is not included in the message.
        """
        raw = pathlib.Path(candidate)

        # 1. Reject any `..` component in the raw candidate.  We do
        #    not attempt to "fix" the path -- the caller must supply a
        #    canonical relative (or in-root absolute) path.  This is
        #    the fail-closed stance the plan calls for; it also blocks
        #    `data/../etc` and `..` as the whole path in one pass.
        if any(part == ".." for part in raw.parts):
            raise ApprovedRootsError(
                "traversal",
                "candidate path contains a `..` component",
            )

        # 2. Resolve (follows symlinks, makes absolute).  strict=False
        #    so a not-yet-existing target is still validated against
        #    its parent chain -- the common case for a training input
        #    that the orchestrator is about to materialize.
        resolved = raw.resolve(strict=False)

        # 3. Find the approved root the resolved path lives under.
        inside = self._find_root(resolved)
        if inside is None:
            raise ApprovedRootsError(
                "outside_root",
                "candidate path is not inside an approved root",
            )

        # 4. Symlink defense.  A symlink that escapes the root is
        #    already caught by step 3 (resolve() follows it).  When
        #    symlinks are disallowed we additionally reject any
        #    symlinked component on the literal path from the root
        #    down -- even one that currently resolves inside the root
        #    -- to block TOCTOU swaps.
        if not allow_symlinks:
            self._reject_symlinks(inside, raw)

        return ResolvedPath(path=resolved, inside_root=inside)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                   #
    # ------------------------------------------------------------------ #

    def _find_root(self, resolved: pathlib.Path) -> pathlib.Path | None:
        for root in self.roots:
            if _is_relative_to(resolved, root):
                return root
        return None

    def _reject_symlinks(
        self, inside: pathlib.Path, raw: pathlib.Path
    ) -> None:
        # Anchor the raw candidate so we walk real filesystem entries.
        literal = raw if raw.is_absolute() else (pathlib.Path.cwd() / raw)
        inside_resolved = inside.resolve(strict=False)
        cursor = literal
        while cursor != cursor.parent:
            try:
                if cursor.is_symlink():
                    raise ApprovedRootsError(
                        "symlink_escape",
                        "candidate path traverses a symlink (disallowed)",
                    )
            except OSError:
                # Missing component (strict=False) -- a non-existent
                # entry cannot be a symlink, so continue the walk.
                pass
            if cursor.resolve(strict=False) == inside_resolved:
                break
            cursor = cursor.parent


def _canonical_root(root: pathlib.Path | str) -> pathlib.Path:
    p = pathlib.Path(root)
    if any(part == ".." for part in p.parts):
        raise ApprovedRootsError(
            "traversal",
            "approved root contains a `..` component",
        )
    return p.resolve(strict=False)


def _is_relative_to(path: pathlib.Path, base: pathlib.Path) -> bool:
    """True if ``path`` is ``base`` or a descendant of it."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def default_approved_roots() -> ApprovedRoots:
    """Build the process-default :class:`ApprovedRoots`.

    Roots come from the ``FINCEPT_APPROVED_DATA_ROOTS`` env var
    (comma-separated).  When the var is missing or empty the
    fail-closed default ``["data", "models"]`` is used.  An explicitly
    empty value is treated as "use the default" rather than "allow
    nothing" so an unset-in-prod env var can't silently brick every
    training request.
    """
    raw = os.environ.get(_ENV_VAR, "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    roots: list[pathlib.Path | str] = list(parts) if parts else list(
        _DEFAULT_ROOTS
    )
    return ApprovedRoots(roots=roots)
