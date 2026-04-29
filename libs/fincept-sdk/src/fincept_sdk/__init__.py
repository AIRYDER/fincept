"""
fincept_sdk — Strategy SDK.

Public surface:

  - ``Strategy``           ABC every strategy implementation extends.
                           Lifecycle hooks: ``on_start``, ``on_bar``,
                           ``on_tick``, ``on_fill``, ``on_signal``,
                           ``on_stop``.
  - ``StrategyContext``    Protocol describing the runtime services a
                           strategy consumes (now_ns, positions, submit,
                           cancel, get_feature, log).  The backtester
                           supplies one implementation; live OMS will
                           supply another (TASK-044).  Strategies don't
                           know which they're running against.
"""

from fincept_sdk.strategy import Strategy, StrategyContext

__all__ = ["Strategy", "StrategyContext"]
