"""
fincept_tools.registry — ToolRegistry singleton and LLM spec helpers.

Exports:
  - ToolRegistry        — register/get/list tools; process-scoped singleton
  - REGISTRY            — the singleton instance
  - register(tool)      — decorator-friendly registration helper
  - to_openai_function_spec(tool)    — OpenAI tool-calling JSON schema
  - to_anthropic_tool_spec(tool)     — Anthropic tool-use JSON schema
  - ToolMeta            — re-exported from protocol for convenience
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .protocol import BaseTool, ToolMeta


def to_openai_function_spec(tool: BaseTool) -> dict[str, Any]:
    """Return the OpenAI function-calling JSON schema for *tool*.

    Shape::

        {
          "type": "function",
          "function": {
            "name": "<tool.name>",
            "description": "<tool.description>",
            "parameters": <JSON Schema object>
          }
        }

    ``parameters`` is derived from ``tool.input_model.model_json_schema()``.
    Pydantic generates a proper ``{"type": "object", "properties": {...}}``
    schema, which is exactly what OpenAI expects.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_model.model_json_schema(),
        },
    }


def to_anthropic_tool_spec(tool: BaseTool) -> dict[str, Any]:
    """Return the Anthropic tool-use JSON schema for *tool*.

    Shape::

        {
          "name": "<tool.name>",
          "description": "<tool.description>",
          "input_schema": <JSON Schema object>
        }

    Anthropic's API calls the parameter block ``input_schema`` instead of
    ``parameters``, but the content is identical JSON Schema.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_model.model_json_schema(),
    }


class ToolRegistry:
    """Process-scoped registry.  Tools self-register at import time.

    The registry keeps insertion order (Python 3.7+ dict guarantee) so
    ``list()`` output is deterministic.

    Usage::

        from fincept_tools.registry import REGISTRY, register

        @register           # or register(my_tool_instance)
        class _:
            ...             # tool instance returned from register()
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register *tool*.  Raises ``ValueError`` on duplicate name."""
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        """Retrieve a tool by name.  Raises ``KeyError`` if absent."""
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"no such tool: {name!r}") from None

    # Note: return types use ``Sequence`` instead of ``list`` because the
    # method name ``list`` shadows the builtin within class scope.
    def list(self) -> Sequence[dict[str, Any]]:
        """Return OpenAI function-call JSON schema for every registered tool.

        Suitable for passing directly to the ``tools`` parameter of an
        OpenAI Chat Completion request.
        """
        return [to_openai_function_spec(t) for t in self._tools.values()]

    def list_meta(self) -> Sequence[ToolMeta]:
        """Return lightweight ``ToolMeta`` objects for every registered tool."""
        return [
            ToolMeta(
                name=t.name,
                description=t.description,
                input_schema=t.input_model.model_json_schema(),
                output_schema=t.output_model.model_json_schema(),
            )
            for t in self._tools.values()
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


#: Singleton used by all tools via ``from fincept_tools.registry import REGISTRY``.
REGISTRY: ToolRegistry = ToolRegistry()


def register(tool: BaseTool) -> BaseTool:
    """Decorator-friendly helper — registers *tool* in the global REGISTRY.

    Can be used as a plain function call or as a decorator on a class that
    yields a tool instance::

        register(GetBarsTool())
    """
    REGISTRY.register(tool)
    return tool
