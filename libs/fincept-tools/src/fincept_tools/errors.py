"""
fincept_tools.errors — typed error hierarchy for tool failures.

Every tool error MUST subclass ``ToolError``.  ``BaseTool.__call__`` catches
``ToolError`` (only), serialises it as a structured ``ToolOutput`` with
``ok=False``, ``error=str(exc)``, and ``error_type=type(exc).__name__``,
and records the exception on the active OTel span.

Untyped exceptions are *not* caught — they propagate, which is the correct
behaviour for programming errors (they should crash the agent loop and be
visible in logs / traces, not silently swallowed).

Why subclass FinceptError?  So that orchestration code that already catches
``FinceptError`` (e.g. the agent runtime) sees tool errors uniformly.
"""

from __future__ import annotations

from fincept_core.errors import FinceptError


class ToolError(FinceptError):
    """Base for all tool errors.  Subclass this for typed errors raised
    inside a tool's ``_run`` method.
    """


class NotInUniverse(ToolError):
    """The requested symbol/entity is not in the active trading universe.

    Raised by ``entity.resolve`` (and any tool that resolves a free-text
    identifier) when the query does not match an active symbol.  This is
    the gate that prevents LLM-hallucinated tickers from reaching the OMS.
    """


class PaperOnlyExec(ToolError):
    """An exec tool was invoked while ``TRADING_MODE != 'paper'``.

    Live execution is gated until Phase H; until then, every exec tool
    must check the trading mode and raise this error before any side effect.
    """


class ToolValidationError(ToolError):
    """Tool-internal validation failed (invariant or post-condition violated).

    Distinct from Pydantic ``ValidationError``, which fires *before* a tool
    runs.  Use this for invariants enforced inside ``_run``.
    """


class ToolBackendError(ToolError):
    """A downstream backend (DB, Redis, HTTP service) failed unrecoverably.

    Wraps the underlying exception (use ``raise ToolBackendError(...) from
    exc``) so the original traceback is preserved while still surfacing a
    typed error to the caller.
    """


class MissingExaApiKey(ToolError):
    """EXA_API_KEY is required before an Exa-backed research tool can run."""


class OpenBBUnavailable(ToolError):
    """The optional OpenBB package is not installed or could not be loaded."""
