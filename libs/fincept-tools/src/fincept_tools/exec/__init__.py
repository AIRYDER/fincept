"""fincept_tools.exec — order-execution tools (paper-only in v1).

Importing this package registers all exec tools with the global REGISTRY.

Tools:
  - exec.submit_order      — publish an OrderIntent to the OMS
  - exec.cancel_order      — publish a cancel request for an existing order
  - exec.get_order_status  — read the most-recent state of an order from the stream

Live execution is gated until Phase H.  Every exec tool checks
``settings.TRADING_MODE`` and raises ``PaperOnlyExec`` if the mode is not
``"paper"``.  The BaseTool runner serialises that as
``ok=False, error=..., error_type='PaperOnlyExec'``.
"""

from fincept_tools.exec.tools import (
    CancelOrderInput,
    CancelOrderOutput,
    CancelOrderTool,
    GetOrderStatusInput,
    GetOrderStatusOutput,
    GetOrderStatusTool,
    SubmitOrderInput,
    SubmitOrderOutput,
    SubmitOrderTool,
)

__all__ = [
    "CancelOrderInput",
    "CancelOrderOutput",
    "CancelOrderTool",
    "GetOrderStatusInput",
    "GetOrderStatusOutput",
    "GetOrderStatusTool",
    "SubmitOrderInput",
    "SubmitOrderOutput",
    "SubmitOrderTool",
]
