"""fincept_tools — typed tool protocol, registry, and baseline tools.

Importing this package registers all built-in tools with REGISTRY.

Public surface:
  - fincept_tools.protocol   — ToolInput, ToolOutput, BaseTool, Tool (Protocol), ToolMeta
  - fincept_tools.registry   — REGISTRY, register(), to_openai_function_spec(),
                               to_anthropic_tool_spec()
  - fincept_tools.data       — data.get_bars, data.get_quote, data.get_trades,
                               data.get_universe, data.get_positions, entity.resolve
  - fincept_tools.analytics  — analytics.compute_returns, analytics.compute_vol,
                               analytics.compute_correlation, analytics.compute_vwap
  - fincept_tools.exec       — exec.submit_order, exec.cancel_order
"""

from fincept_tools import analytics as _analytics  # noqa: F401
from fincept_tools import data as _data  # noqa: F401
from fincept_tools import exec as _exec  # noqa: F401
from fincept_tools.protocol import BaseTool, Tool, ToolInput, ToolMeta, ToolOutput
from fincept_tools.registry import (
    REGISTRY,
    register,
    to_anthropic_tool_spec,
    to_openai_function_spec,
)

__all__ = [
    "REGISTRY",
    "BaseTool",
    "Tool",
    "ToolInput",
    "ToolMeta",
    "ToolOutput",
    "register",
    "to_anthropic_tool_spec",
    "to_openai_function_spec",
]
