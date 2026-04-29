"""
backtester — deterministic event-driven historical replay.

Public surface:

  - ``BacktestEngine``    Orchestrates the run: bar replay -> strategy
                          callbacks -> simulated fills -> position +
                          equity tracking.
  - ``BarsDataSource``    Replays bars from Timescale (or any injected
                          ``bar_reader``) in monotonic ``ts_event`` order.
                          Multi-symbol replay merges per-symbol streams
                          via ``heapq.merge``.
  - ``CostModel``         Spread + fee + slippage cost simulator
                          (TASK-021 within the same module per spec).
  - ``SimBroker``         Fill-against-bar broker (TASK-022 within the
                          same module per spec).
  - ``Blotter``           Append-only fills + equity curve store.
"""

from backtester.blotter import Blotter
from backtester.broker import SimBroker
from backtester.costs import CostModel
from backtester.datasource import BarsDataSource
from backtester.engine import BacktestEngine

__all__ = [
    "BacktestEngine",
    "BarsDataSource",
    "Blotter",
    "CostModel",
    "SimBroker",
]
