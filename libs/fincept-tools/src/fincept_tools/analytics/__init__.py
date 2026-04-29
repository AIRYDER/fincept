"""fincept_tools.analytics — pure-compute analytics tools.

Importing this package registers all analytics tools with the global REGISTRY.

Tools:
  - analytics.compute_returns     — log-returns series from bars
  - analytics.compute_vol         — realised annualised volatility
  - analytics.compute_correlation — Pearson correlation between two return series
  - analytics.compute_vwap        — VWAP from bars
  - analytics.compute_sharpe      — annualised Sharpe ratio over a lookback
  - analytics.compute_drawdown    — max peak-to-trough drawdown over a lookback
"""

from fincept_tools.analytics import tools as _tools  # noqa: F401 — import for side-effects

__all__: list[str] = []
