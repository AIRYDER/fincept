"""
fincept_sdk.strategy — the Strategy ABC + StrategyContext Protocol.

The same ``Strategy`` subclass runs in three places:

  1. Backtester  — ``services/backtester/engine.py`` supplies a sync
                   ``StrategyContext`` driven by Timescale replay.
  2. Live paper  — ``services/oms/`` (TASK-044) supplies a context driven
                   by the live bar stream.
  3. Walk-forward — ``services/backtester/walk_forward.py`` (TASK-023)
                   supplies a series of train/eval contexts.

Because all three honour the same Protocol, a strategy written for the
backtester runs unchanged in paper trading.  This is the entire reason
the SDK exists.

PIT correctness is the strategy author's responsibility within
``on_bar`` / ``on_tick``: the runtime guarantees ``now_ns`` is the bar's
``ts_event`` and that ``positions`` reflect fills strictly before that
moment, but the strategy must not query ``get_feature`` for a horizon
that would imply lookahead.  The PIT join layer (TASK-017) is the
canonical PIT-safe feature accessor when a strategy needs historical
features beyond what's in the live cache.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

from fincept_core.schemas import BarEvent, Fill, OrderIntent, Position, TradeEvent


@runtime_checkable
class StrategyContext(Protocol):
    """Runtime services a strategy consumes.

    ``runtime_checkable`` so tests can assert structural conformance via
    ``isinstance(ctx, StrategyContext)`` without subclassing.  Note that
    Python's runtime check only verifies attribute *presence*, not
    signatures — type-checkers do the rest at static analysis time.
    """

    now_ns: int
    positions: dict[str, Position]

    def submit(self, intent: OrderIntent) -> str:
        """Submit an order; return its ``order_id``."""
        ...

    def cancel(self, order_id: str) -> None:
        """Cancel an open order.  No-op if already filled or unknown."""
        ...

    def get_feature(self, name: str, symbol: str) -> float | None:
        """Return the latest cached feature value, or ``None`` if unknown."""
        ...

    def log(self, msg: str, **kwargs: Any) -> None:
        """Emit a structured log line scoped to the strategy."""
        ...


class Strategy(ABC):
    """Base class for all strategies.

    Every concrete strategy must:

      - declare ``strategy_id`` (class attribute, e.g., ``"ma_crossover.v1"``)
      - declare ``symbols`` (class attribute, list of canonical symbols)
      - implement the lifecycle hooks below

    Defaults are deliberately abstract — implementers must explicitly
    decide whether each hook is a no-op for their strategy.  Silent
    inheritance of empty hooks would hide bugs (e.g., forgetting to
    handle fills and wondering why positions don't update).
    """

    strategy_id: ClassVar[str]
    symbols: ClassVar[list[str]]

    @abstractmethod
    def on_start(self, ctx: StrategyContext) -> None:
        """Called once before the first bar; load state, warm caches."""

    @abstractmethod
    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        """Called per bar; the primary decision point for most strategies."""

    @abstractmethod
    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        """Called per trade tick; high-frequency strategies override."""

    @abstractmethod
    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        """Called when one of this strategy's submitted orders fills."""

    @abstractmethod
    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        """Called when an external signal (e.g., Prediction) is received."""

    @abstractmethod
    def on_stop(self, ctx: StrategyContext) -> None:
        """Called once after the last bar / on shutdown; flush, persist."""
