"""
fincept_tools.protocol — Tool protocol per CONTRACTS §8.

Defines:
  - ToolInput   — base class for per-tool input schemas (extra='forbid')
  - ToolOutput  — base class for per-tool output schemas (carries ok/error/error_type)
  - ToolOK      — alias documenting the success shape
  - ToolError   — alias documenting the error shape (NB: distinct from
                  fincept_tools.errors.ToolError, which is the *exception*)
  - Tool        — structural Protocol that concrete tool classes satisfy
  - BaseTool    — convenience concrete base.  Subclasses override ``_run``.
  - ToolMeta    — lightweight summary for registry listing

Subclasses of ``BaseTool`` MUST override ``_run``, NOT ``__call__``.
``__call__`` is the public entry point; it wraps every invocation in:

  1. an OpenTelemetry span (``tool.<name>``) carrying args_size,
     result_size, duration_ns, ok, and error_type as attributes — the
     orchestrator aggregates these for cost tracking.
  2. typed-error handling: ``ToolError`` subclasses are caught and
     surfaced as ``output_model(ok=False, error=str(exc),
     error_type=type(exc).__name__)``.  Untyped exceptions propagate.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from fincept_core.tracing import tracer
from fincept_tools.errors import ToolError as ToolErrorException


class ToolInput(BaseModel):
    """Per-tool input schema.  Subclasses declare fields.

    Extra fields are forbidden so that LLM output that includes stray keys
    raises a validation error rather than silently ignoring them — this is
    one of the gates against hallucinated arguments.
    """

    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    """Per-tool output schema.  Subclasses declare payload fields.

    Every response carries:
      - ``ok``: True on success, False on a typed error.
      - ``error``: human-readable message; None on success.
      - ``error_type``: class name of the typed error (e.g. ``NotInUniverse``)
        on failure; None on success.  Callers branch on ``error_type`` to
        recover from specific failure modes without parsing strings.
    """

    model_config = ConfigDict(extra="forbid")
    ok: bool = True
    error: str | None = None
    error_type: str | None = None


# Convenience aliases — same shape, used to document intent at call sites.
ToolOK = ToolOutput
ToolError = ToolOutput  # NB: distinct from fincept_tools.errors.ToolError (exception)


class ToolMeta(BaseModel):
    """Lightweight summary of a registered tool, returned by ``ToolRegistry.list_meta``."""

    model_config = ConfigDict(frozen=True)
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


@runtime_checkable
class Tool(Protocol):
    """Structural protocol — any object with these class-level attributes
    and an async ``__call__`` satisfies it.  Used for type-hints in callers
    that don't want to import the concrete ``BaseTool``.
    """

    name: str
    description: str
    input_model: type[ToolInput]
    output_model: type[ToolOutput]

    async def __call__(self, payload: ToolInput) -> ToolOutput: ...


class BaseTool:
    """Concrete base.  Subclasses set the four class-vars and override ``_run``.

    Do NOT override ``__call__`` — it provides:
      - per-call OTel span with attributes (cost-tracking-ready)
      - typed-error catch + structured serialisation
      - args/result size + duration measurement

    A subclass that needs custom dispatching (rare) can override ``__call__``
    but must replicate the observability + error contract itself.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[ToolInput]]
    output_model: ClassVar[type[ToolOutput]]

    async def __call__(self, payload: ToolInput) -> ToolOutput:
        """Public entry — wraps ``_run`` in tracing + typed-error handling."""
        tr = tracer("fincept_tools")
        with tr.start_as_current_span(f"tool.{self.name}") as span:
            t0 = time.perf_counter_ns()
            try:
                args_size = len(payload.model_dump_json())
            except Exception:
                args_size = -1
            span.set_attribute("tool.name", self.name)
            span.set_attribute("tool.args_size", args_size)

            try:
                result = await self._run(payload)
            except ToolErrorException as exc:
                duration = time.perf_counter_ns() - t0
                error_type = type(exc).__name__
                span.set_attribute("tool.duration_ns", duration)
                span.set_attribute("tool.ok", False)
                span.set_attribute("tool.error_type", error_type)
                span.record_exception(exc)
                return self.output_model(ok=False, error=str(exc), error_type=error_type)
            # Untyped exceptions propagate intentionally (programming errors).

            duration = time.perf_counter_ns() - t0
            try:
                result_size = len(result.model_dump_json())
            except Exception:
                result_size = -1
            span.set_attribute("tool.result_size", result_size)
            span.set_attribute("tool.duration_ns", duration)
            span.set_attribute("tool.ok", result.ok)
            if not result.ok and result.error_type:
                span.set_attribute("tool.error_type", result.error_type)
            return result

    async def _run(self, payload: ToolInput) -> ToolOutput:
        """Override this in subclasses.  Raise ``ToolError`` subclasses for
        typed failures; all other exceptions propagate.
        """
        raise NotImplementedError
