"""
strategy_host — live runtime for StrategyConfig instances.

Public surface:

  - ``Supervisor``           Reconciles the on-disk StrategyConfigStore
                             against a set of running asyncio tasks:
                             starts a runner when a config flips
                             ``enabled=True``, cancels it when the
                             flag flips back, and restarts it when
                             fields the runner depends on change
                             (class_name, symbols, params, binding).
  - ``LiveStrategyContext``  Per-strategy implementation of the
                             :class:`fincept_sdk.StrategyContext`
                             protocol; injected into strategy hooks.
  - ``run_strategy``         Per-strategy runner entrypoint that
                             tails md.bars.1m / ord.fills /
                             ord.positions, dispatches strategy
                             hooks, and publishes OrderIntents to
                             ord.orders.

The host process binds these together in ``main.run`` and exposes a
``service:heartbeat:strategy_host`` key for the start.ps1 health gate
and the dashboard /services view.
"""

from strategy_host.runner import run_strategy
from strategy_host.runtime import LiveStrategyContext
from strategy_host.supervisor import Supervisor

__all__ = ["LiveStrategyContext", "Supervisor", "run_strategy"]
