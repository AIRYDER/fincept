"""Tests for fincept_tools.protocol.

Subclasses MUST override ``_run``; ``BaseTool.__call__`` is the framework-
provided wrapper that adds OTel tracing + typed-error handling.  Overriding
``__call__`` directly bypasses observability and is therefore disallowed by
convention; these tests enforce the convention.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from fincept_tools.errors import NotInUniverse, PaperOnlyExec
from fincept_tools.protocol import BaseTool, Tool, ToolInput, ToolMeta, ToolOutput

# ---------------------------------------------------------------------------
# ToolInput
# ---------------------------------------------------------------------------


def test_tool_input_forbids_extra() -> None:
    """ToolInput must reject unexpected fields (extra='forbid')."""

    class MyIn(ToolInput):
        x: int

    with pytest.raises(ValidationError):
        MyIn(x=1, y=2)  # type: ignore[call-arg]


def test_tool_input_validates_fields() -> None:
    class MyIn(ToolInput):
        x: int

    obj = MyIn(x=42)
    assert obj.x == 42


# ---------------------------------------------------------------------------
# ToolOutput
# ---------------------------------------------------------------------------


def test_tool_output_defaults_to_ok() -> None:
    out = ToolOutput()
    assert out.ok is True
    assert out.error is None
    assert out.error_type is None


def test_tool_output_error_variant() -> None:
    out = ToolOutput(ok=False, error="something went wrong", error_type="ToolBackendError")
    assert out.ok is False
    assert out.error == "something went wrong"
    assert out.error_type == "ToolBackendError"


def test_tool_output_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        ToolOutput(ok=True, unknown_field="nope")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# BaseTool — _run is what subclasses override; __call__ is the wrapper
# ---------------------------------------------------------------------------


def test_base_tool_default_run_raises_not_implemented() -> None:
    class T(BaseTool):
        name = "test.tool"
        description = "for testing"
        input_model = ToolInput
        output_model = ToolOutput

    with pytest.raises(NotImplementedError):
        asyncio.run(T()(ToolInput()))


def test_base_tool_subclass_overrides_run() -> None:
    class ConcreteIn(ToolInput):
        value: int

    class ConcreteOut(ToolOutput):
        doubled: int = 0

    class DoubleTool(BaseTool):
        name = "test.double"
        description = "doubles a value"
        input_model = ConcreteIn
        output_model = ConcreteOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            assert isinstance(payload, ConcreteIn)
            return ConcreteOut(doubled=payload.value * 2)

    result = asyncio.run(DoubleTool()(ConcreteIn(value=5)))
    assert isinstance(result, ConcreteOut)
    assert result.doubled == 10
    assert result.ok is True
    assert result.error_type is None


# ---------------------------------------------------------------------------
# BaseTool — typed errors get caught and serialised as ok=False
# ---------------------------------------------------------------------------


def test_base_tool_catches_typed_error_and_returns_ok_false() -> None:
    """Raising a ToolError subclass inside _run becomes a structured failure."""

    class FailIn(ToolInput):
        pass

    class FailOut(ToolOutput):
        pass

    class FailingTool(BaseTool):
        name = "test.fail"
        description = "always fails"
        input_model = FailIn
        output_model = FailOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            raise NotInUniverse("XYZ is not in the universe")

    result = asyncio.run(FailingTool()(FailIn()))
    assert isinstance(result, FailOut)
    assert result.ok is False
    assert result.error == "XYZ is not in the universe"
    assert result.error_type == "NotInUniverse"


def test_base_tool_catches_paper_only_exec_error() -> None:
    """PaperOnlyExec is also a ToolError → caught uniformly."""

    class PIn(ToolInput):
        pass

    class POut(ToolOutput):
        pass

    class T(BaseTool):
        name = "test.paper_only"
        description = "x"
        input_model = PIn
        output_model = POut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            raise PaperOnlyExec("paper only")

    result = asyncio.run(T()(PIn()))
    assert result.ok is False
    assert result.error_type == "PaperOnlyExec"


def test_base_tool_does_not_catch_untyped_exceptions() -> None:
    """RuntimeError (not a ToolError) must propagate — programming errors stay visible."""

    class T(BaseTool):
        name = "test.untyped_fail"
        description = "x"
        input_model = ToolInput
        output_model = ToolOutput

        async def _run(self, payload: ToolInput) -> ToolOutput:
            raise RuntimeError("internal bug — should not be swallowed")

    with pytest.raises(RuntimeError, match="internal bug"):
        asyncio.run(T()(ToolInput()))


# ---------------------------------------------------------------------------
# Tool (Protocol) structural check
# ---------------------------------------------------------------------------


def test_tool_protocol_satisfied_by_base_tool_subclass() -> None:
    class MyIn(ToolInput):
        pass

    class MyOut(ToolOutput):
        pass

    class MyTool(BaseTool):
        name = "my.tool"
        description = "a test tool"
        input_model = MyIn
        output_model = MyOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            return MyOut()

    instance = MyTool()
    assert isinstance(instance, Tool)


# ---------------------------------------------------------------------------
# ToolMeta
# ---------------------------------------------------------------------------


def test_tool_meta_is_frozen() -> None:
    meta = ToolMeta(
        name="x",
        description="desc",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    with pytest.raises(Exception):  # noqa: B017
        meta.name = "y"  # type: ignore[misc]
