"""Tests for fincept_tools.registry."""

from __future__ import annotations

import pytest

import fincept_tools.analytics  # side-effect: registers analytics tools
import fincept_tools.data  # side-effect: registers data tools
import fincept_tools.research  # noqa: F401 — side-effect: registers research tools
from fincept_tools.protocol import BaseTool, ToolInput, ToolOutput
from fincept_tools.registry import (
    REGISTRY,
    ToolRegistry,
    to_anthropic_tool_spec,
    to_openai_function_spec,
)

# ---------------------------------------------------------------------------
# ToolRegistry isolation helper
# ---------------------------------------------------------------------------


def _fresh_registry() -> ToolRegistry:
    """Create a fresh registry for isolation tests."""
    return ToolRegistry()


# ---------------------------------------------------------------------------
# Registration and retrieval
# ---------------------------------------------------------------------------


def test_registry_register_and_get() -> None:
    reg = _fresh_registry()

    class PingIn(ToolInput):
        pass

    class PingOut(ToolOutput):
        pass

    class PingTool(BaseTool):
        name = "test.ping"
        description = "ping"
        input_model = PingIn
        output_model = PingOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            return PingOut()

    tool = PingTool()
    reg.register(tool)
    assert reg.get("test.ping") is tool


def test_registry_get_raises_key_error_for_unknown() -> None:
    reg = _fresh_registry()
    with pytest.raises(KeyError):
        reg.get("no.such.tool")


def test_registry_duplicate_registration_raises() -> None:
    reg = _fresh_registry()

    class TIn(ToolInput):
        pass

    class TOut(ToolOutput):
        pass

    class T(BaseTool):
        name = "dup.tool"
        description = "duplicate"
        input_model = TIn
        output_model = TOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            return TOut()

    reg.register(T())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(T())


def test_registry_contains() -> None:
    reg = _fresh_registry()

    class TIn(ToolInput):
        pass

    class TOut(ToolOutput):
        pass

    class T(BaseTool):
        name = "contain.tool"
        description = "x"
        input_model = TIn
        output_model = TOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            return TOut()

    reg.register(T())
    assert "contain.tool" in reg
    assert "other.tool" not in reg


def test_registry_len() -> None:
    reg = _fresh_registry()
    assert len(reg) == 0

    class TIn(ToolInput):
        pass

    class TOut(ToolOutput):
        pass

    class T(BaseTool):
        name = "len.tool"
        description = "l"
        input_model = TIn
        output_model = TOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            return TOut()

    reg.register(T())
    assert len(reg) == 1


# ---------------------------------------------------------------------------
# Global REGISTRY — built-in tools are registered on import
# ---------------------------------------------------------------------------


def test_global_registry_has_all_expected_tools() -> None:
    import fincept_tools.exec  # noqa: F401 — side-effect: registers execution tools

    names = {s["function"]["name"] for s in REGISTRY.list()}
    required = {
        # data
        "data.get_bars",
        "data.get_quote",
        "data.get_trades",
        "data.get_universe",
        "data.get_positions",
        "data.get_features",
        "entity.resolve",
        # analytics
        "analytics.compute_returns",
        "analytics.compute_vol",
        "analytics.compute_correlation",
        "analytics.compute_vwap",
        "analytics.compute_sharpe",
        "analytics.compute_drawdown",
        # exec
        "exec.submit_order",
        "exec.cancel_order",
        "exec.get_order_status",
    }
    assert required.issubset(names), f"Missing tools: {required - names}"


def test_registry_list_returns_valid_openai_format() -> None:
    """Each entry in REGISTRY.list() must match OpenAI function-call shape."""
    schemas = REGISTRY.list()
    assert len(schemas) >= 6  # at least 6 built-in tools

    for spec in schemas:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


def test_list_meta_returns_tool_meta_objects() -> None:
    metas = REGISTRY.list_meta()
    assert len(metas) >= 6
    for m in metas:
        assert m.name
        assert m.description
        assert isinstance(m.input_schema, dict)
        assert isinstance(m.output_schema, dict)


# ---------------------------------------------------------------------------
# to_openai_function_spec
# ---------------------------------------------------------------------------


def test_to_openai_function_spec_structure() -> None:
    tool = REGISTRY.get("data.get_bars")
    spec = to_openai_function_spec(tool)

    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "data.get_bars"
    assert "description" in fn

    params = fn["parameters"]
    assert params["type"] == "object"
    assert "properties" in params
    props = params["properties"]
    assert "symbol" in props
    assert "freq" in props
    assert "start_ns" in props
    assert "end_ns" in props


def test_to_openai_function_spec_required_fields() -> None:
    """Required fields (no default) must appear in the 'required' array."""
    tool = REGISTRY.get("data.get_bars")
    spec = to_openai_function_spec(tool)
    params = spec["function"]["parameters"]
    required = params.get("required", [])
    # symbol, freq (has pattern but no default? check), start_ns, end_ns are required
    assert "symbol" in required
    assert "start_ns" in required
    assert "end_ns" in required


# ---------------------------------------------------------------------------
# to_anthropic_tool_spec
# ---------------------------------------------------------------------------


def test_to_anthropic_tool_spec_structure() -> None:
    tool = REGISTRY.get("analytics.compute_vol")
    spec = to_anthropic_tool_spec(tool)

    assert spec["name"] == "analytics.compute_vol"
    assert "description" in spec
    assert "input_schema" in spec
    schema = spec["input_schema"]
    assert schema["type"] == "object"
    assert "properties" in schema


def test_to_anthropic_tool_spec_differs_from_openai() -> None:
    """Anthropic uses 'input_schema'; OpenAI uses 'parameters' under 'function'."""
    tool = REGISTRY.get("analytics.compute_vol")
    oai = to_openai_function_spec(tool)
    anth = to_anthropic_tool_spec(tool)

    # OpenAI wraps in type+function; Anthropic is flat
    assert "type" in oai
    assert "function" in oai
    assert "type" not in anth or anth.get("type") != "function"
    assert "input_schema" in anth
    assert "input_schema" not in oai


# ---------------------------------------------------------------------------
# register() helper function
# ---------------------------------------------------------------------------


def test_register_helper_returns_tool() -> None:
    """register() should return the tool instance (decorator pattern)."""
    # Use a private registry so we don't pollute REGISTRY
    reg = _fresh_registry()

    class TIn(ToolInput):
        pass

    class TOut(ToolOutput):
        pass

    class T(BaseTool):
        name = "helper.tool"
        description = "h"
        input_model = TIn
        output_model = TOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            return TOut()

    t = T()
    # register on the private registry directly
    reg.register(t)
    assert reg.get("helper.tool") is t


# ---------------------------------------------------------------------------
# Round-trip: register → retrieve → call → typed result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_trip_register_retrieve_call() -> None:
    """Full round-trip using a private registry."""
    reg = _fresh_registry()

    class AddIn(ToolInput):
        a: int
        b: int

    class AddOut(ToolOutput):
        total: int = 0

    class AddTool(BaseTool):
        name = "math.add"
        description = "adds two integers"
        input_model = AddIn
        output_model = AddOut

        async def _run(self, payload: ToolInput) -> ToolOutput:
            assert isinstance(payload, AddIn)
            return AddOut(total=payload.a + payload.b)

    tool = AddTool()
    reg.register(tool)

    retrieved = reg.get("math.add")
    result = await retrieved(AddIn(a=3, b=4))

    assert isinstance(result, AddOut)
    assert result.ok is True
    assert result.total == 7
