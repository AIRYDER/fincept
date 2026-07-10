"""fincept_tools.data — read-only data-access tools.

Importing this package registers all data tools with the global REGISTRY.

Tools:
  - data.get_bars       — OHLCV bars for a symbol over a time window
  - data.get_quote      — latest close as a quote proxy for a symbol
  - data.get_trades     — raw tick trades for a symbol over a time window
  - data.get_universe   — list of active in-universe symbols
  - data.get_positions  — current positions for a strategy
  - data.get_features   — online feature snapshot for a symbol
  - entity.resolve      — resolve free-text ticker/name to canonical symbol;
                          raises NotInUniverse on miss
"""

from fincept_tools.data import tools as _tools  # noqa: F401 - re-export

__all__: list[str] = []
