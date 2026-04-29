"""
portfolio.state — in-memory portfolio state + the apply_fill helper.

State shape:  ``{strategy_id: {symbol: Position}}``

The portfolio service holds the live state in memory for fast updates
on each Fill, and mirrors writes to the Redis hash via ``PositionStore``
so the API layer can serve reads without going through this process.

``apply_fill`` is the single entry point for the consume-Fill loop:

  1. Look up the prior Position (in-memory if hot; Redis if cold).
  2. Run the shared ``fincept_core.portfolio.apply_fill_to_position``.
  3. Mutate in-memory state + write to PositionStore.
  4. Return the new Position so the caller can publish to STREAM_POSITIONS.

Because this and the backtester engine both call the same kernel,
position values are bit-identical between offline backtests and live
paper trading on the same Fill stream.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fincept_core.portfolio import apply_fill_to_position
from fincept_core.schemas import Fill, Position
from portfolio.store import PositionStore


class PortfolioState:
    """In-memory ``{strategy_id: {symbol: Position}}``.

    ``hydrate`` loads existing state from a PositionStore on startup so
    a restarted portfolio service picks up where the previous instance
    left off (the Redis hash survives process restarts).
    """

    def __init__(self) -> None:
        self._state: dict[str, dict[str, Position]] = {}

    def get(self, strategy_id: str, symbol: str) -> Position | None:
        return self._state.get(strategy_id, {}).get(symbol)

    def record(self, position: Position) -> None:
        self._state.setdefault(position.strategy_id, {})[position.symbol] = position

    def all_for_strategy(self, strategy_id: str) -> dict[str, Position]:
        return dict(self._state.get(strategy_id, {}))

    def known_strategies(self) -> set[str]:
        return set(self._state)

    async def hydrate(self, store: PositionStore) -> None:
        """Pull existing positions from the store into memory on startup."""
        for strategy_id in await store.known_strategies():
            positions = await store.get_all(strategy_id)
            self._state[strategy_id] = positions


# A FillStrategyResolver maps a Fill to the strategy_id that owns it.
# Fills don't carry strategy_id directly (Fill schema doesn't have one);
# in production we recover it via the order_id -> Order.strategy_id
# lookup against the audit log.  For v1 we accept any callable so tests
# inject a hardcoded resolver and production wires the audit lookup.
FillStrategyResolver = Callable[[Fill], Awaitable[str | None]]


async def apply_fill(
    fill: Fill,
    *,
    state: PortfolioState,
    store: PositionStore,
    resolve_strategy: FillStrategyResolver,
) -> Position | None:
    """Apply a Fill to portfolio state.  Returns the new Position, or None
    if the fill couldn't be attributed to a strategy."""
    strategy_id = await resolve_strategy(fill)
    if strategy_id is None:
        return None  # caller logs; we can't update positions for an orphan fill

    prev = state.get(strategy_id, fill.symbol)
    new_position = apply_fill_to_position(prev, fill, strategy_id=strategy_id)
    state.record(new_position)
    await store.put(new_position)
    return new_position
