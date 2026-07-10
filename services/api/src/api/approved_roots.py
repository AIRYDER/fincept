"""Shared approved-roots dependency for API routes.

Every endpoint that accepts a user-supplied filesystem path (training
input, backtest bars, dataset path) must run it through
:class:`fincept_core.datasets.ApprovedRoots.resolve` before handing it
to the orchestrator.  This module owns the single FastAPI dependency
that returns the process-default :class:`ApprovedRoots` instance so
todo 6 (models) and todo 7 (backtest) share the same gate, plus the
shared exception handler that maps :class:`ApprovedRootsError` to the
uniform ``HTTP 422`` problem-detail body
``{"detail": "<message>", "code": "approved_roots_violation"}``.

The dependency is fail-closed: there is no env-var switch to disable
the check at runtime, and an empty approved-roots list raises
``ApprovedRootsError("no_roots", ...)`` at construction time.
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fincept_core.datasets import (
    ApprovedRoots,
    ApprovedRootsError,
    default_approved_roots,
)

# Uniform machine-readable code surfaced in every approved-roots
# violation response body.  The underlying ``ApprovedRootsError.code``
# (``outside_root`` / ``traversal`` / ``symlink_escape`` / ``no_roots``)
# is preserved in the ``X-Approved-Roots-Code`` response header for
# operators / dashboards that want the finer reason without parsing the
# message text.
VIOLATION_CODE = "approved_roots_violation"


def get_approved_roots() -> ApprovedRoots:
    """FastAPI dependency returning the process-default approved roots.

    Rebuilt per request so a test that patches the env var
    (``FINCEPT_APPROVED_DATA_ROOTS``) or the ``default_approved_roots``
    factory sees its change without process restart.  The instance is
    cheap to construct (a couple of ``pathlib.Path.resolve`` calls).
    """
    return default_approved_roots()


async def approved_roots_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Map :class:`ApprovedRootsError` to the uniform 422 body.

    Registered once on the app (see :func:`register_approved_roots_handler`)
    so every route that calls ``approved_roots.resolve(...)`` gets the
    same response shape for free -- no per-route try/except boilerplate.
    The approved-roots list is never echoed (the message from
    ``ApprovedRootsError`` is already scrubbed upstream).
    """
    approved_exc = cast(ApprovedRootsError, exc)
    return JSONResponse(
        status_code=422,
        content={"detail": str(approved_exc), "code": VIOLATION_CODE},
        headers={"X-Approved-Roots-Code": approved_exc.code},
    )


def register_approved_roots_handler(app: FastAPI) -> None:
    """Register the shared approved-roots exception handler on ``app``.

    Idempotent: safe to call from every route module that wants the
    handler; FastAPI keeps only the last registration for a given
    exception type, and the handler is always the same closure.
    """
    app.add_exception_handler(ApprovedRootsError, approved_roots_exception_handler)


__all__ = [
    "ApprovedRoots",
    "ApprovedRootsError",
    "VIOLATION_CODE",
    "get_approved_roots",
    "register_approved_roots_handler",
]
