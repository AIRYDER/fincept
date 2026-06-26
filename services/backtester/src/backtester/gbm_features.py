"""
backtester.gbm_features — OHLCV-derivable feature kit for the GBM
strategy adapter.

The live ``gbm_predictor`` agent reads features from Redis (populated
by the features service from microstructure data: book imbalance,
spreads, returns, realized vol, momentum z-scores).  Inside a backtest
we only have OHLCV bars, so this module computes the **subset** of
features that can be derived purely from a rolling window of bars.

Supported feature names follow the trainer's convention
``<kind>_<n><unit>`` where ``<unit>`` is one of:

  - ``m`` minutes  (1 minute)
  - ``h`` hours    (60 minutes)
  - ``d`` days     (1440 minutes — calendar days, *not* trading days)

All three suffixes are normalised to minutes at parse time so the rest
of the module works in a single unit.  This lets the same feature kit
drive both intraday minute-bar models (``ret_5m``, ``mom_z_240m``) and
daily-bar models (``ret_5d``, ``rv_20d``) without code changes.

Feature kinds:
  - ``ret_<N><u>``    log return over the last N units
                      = log(close_t / close_{t-bars_back})
  - ``rv_<N><u>``     realized vol = stdev of bar-to-bar log returns
                      over the last N units (population stdev; same
                      convention as the features service)
  - ``mom_z_<N><u>``  momentum z-score = recent N-unit return divided
                      by its rolling std over the same window

If a model's ``meta.json`` lists a feature this module can't compute
(e.g., ``book_imbalance_1``, ``spread_bps``), :func:`require_supported`
raises ``ValueError`` with a clear message — the caller (GBMStrategy)
should treat that as "this model can't be backtested on OHLCV alone;
either re-train without book features, or supply an enriched parquet."
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from decimal import Decimal

from fincept_core.schemas import BarEvent

SUPPORTED_KINDS: frozenset[str] = frozenset({"ret", "rv", "mom_z"})

# Unit suffix -> minutes-per-unit.  Days are calendar days (1440 min);
# this matters because ``required_window_bars`` divides by ``bar_minutes``
# to get a bar count, so the unit only needs to be self-consistent
# between training and backtest — not aligned with NYSE session length.
_UNIT_TO_MINUTES: dict[str, int] = {
    "m": 1,
    "h": 60,
    "d": 60 * 24,
}
SUPPORTED_UNITS: frozenset[str] = frozenset(_UNIT_TO_MINUTES)

# ``ret_5m``, ``rv_30m``, ``mom_z_240m``, ``ret_5d``, ``rv_2h``
# -> (kind, count, unit)
_FEATURE_RE = re.compile(r"^(ret|rv|mom_z)_(\d+)([mhd])$")


def parse_feature_name(name: str) -> tuple[str, int]:
    """Decode a feature name into ``(kind, total_minutes)``.

    The unit suffix (``m`` / ``h`` / ``d``) is normalised to minutes so
    every caller works in a single unit:

    >>> parse_feature_name("ret_5m")
    ('ret', 5)
    >>> parse_feature_name("mom_z_240m")
    ('mom_z', 240)
    >>> parse_feature_name("ret_2h")
    ('ret', 120)
    >>> parse_feature_name("rv_5d")
    ('rv', 7200)

    Raises ``ValueError`` if the name doesn't match the supported
    pattern; the GBM strategy uses this to fail fast on unsupported
    features at startup rather than silently producing garbage.
    """
    match = _FEATURE_RE.match(name)
    if not match:
        raise ValueError(
            f"unsupported feature name {name!r}; "
            f"expected <{'|'.join(sorted(SUPPORTED_KINDS))}>_<N>"
            f"<{'|'.join(sorted(SUPPORTED_UNITS))}>"
        )
    kind = match.group(1)
    count = int(match.group(2))
    unit = match.group(3)
    if count <= 0:
        raise ValueError(f"feature {name!r} has non-positive count")
    return kind, count * _UNIT_TO_MINUTES[unit]


def require_supported(feature_names: Sequence[str]) -> list[tuple[str, int]]:
    """Validate every name parses; return parsed ``[(kind, minutes), ...]``.

    Surfaces *all* unsupported names in the error message (not just the
    first) so the caller doesn't have to play whack-a-mole.
    """
    parsed: list[tuple[str, int]] = []
    bad: list[str] = []
    for name in feature_names:
        try:
            parsed.append(parse_feature_name(name))
        except ValueError:
            bad.append(name)
    if bad:
        kinds = "|".join(sorted(SUPPORTED_KINDS))
        units = "|".join(sorted(SUPPORTED_UNITS))
        raise ValueError(
            f"GBMStrategy cannot compute these features from OHLCV bars: "
            f"{bad}. Either re-train the model with only OHLCV-derivable "
            f"features (<{kinds}>_<N><{units}>) or feed an enriched "
            f"parquet through a custom strategy."
        )
    return parsed


def required_window_bars(feature_names: Sequence[str], *, bar_minutes: int) -> int:
    """Return the minimum window size needed to compute every feature.

    Every feature ``<kind>_<N><unit>`` is normalised to a total minute
    count at parse time, then converted to bars via ``ceil(total_minutes
    / bar_minutes)``.  We add ``+1`` uniformly so the oldest bar can
    serve as the denominator for a log return ``close_t / close_{t-N}``
    — small overcost, simpler invariant.

    Examples (with ``bar_minutes=1`` for minute bars,
    ``bar_minutes=1440`` for daily bars):

      - ``ret_5m``  on  1m bars → ceil(5/1)    + 1 = 6 bars
      - ``ret_2h``  on  1m bars → ceil(120/1)  + 1 = 121 bars
      - ``ret_5d``  on  1d bars → ceil(7200/1440) + 1 = 6 bars
      - ``mom_z_20d`` on 1d bars → ceil(28800/1440) + 1 = 21 bars
    """
    if bar_minutes <= 0:
        raise ValueError(f"bar_minutes must be positive, got {bar_minutes}")
    parsed = require_supported(feature_names)
    max_bars = 0
    for _, minutes in parsed:
        bars = math.ceil(minutes / bar_minutes)
        if bars > max_bars:
            max_bars = bars
    # +1 extra bar so the oldest bar can serve as the denominator for a
    # log return (close_t / close_{t-N}).
    return max_bars + 1


def compute_features(
    window: Sequence[BarEvent],
    *,
    feature_names: Sequence[str],
    bar_minutes: int,
) -> dict[str, float] | None:
    """Compute *feature_names* from a rolling *window* of bars.

    *window* is expected to be ordered oldest -> newest.  Returns
    ``None`` if the window is too short for any requested feature
    (caller should skip the bar instead of feeding NaN to the model).

    Conventions:
      - log returns use natural log of close ratios
      - realized vol is population stdev (ddof=0) of bar-to-bar log
        returns over the window — same as the features service
      - mom_z = (sum of bar returns over N minutes) / (stdev * sqrt(N))
        where N is the bar count; if stdev is 0 we return 0.0 (flat)
    """
    if not window:
        return None
    parsed = require_supported(feature_names)
    closes = [float(b.close) for b in window]
    # Reject any non-positive close (would make log undefined).
    if any(c <= 0 for c in closes):
        return None

    out: dict[str, float] = {}
    n_bars = len(closes)

    # Pre-compute bar-to-bar log returns once; many features use this.
    log_returns: list[float] = [math.log(closes[i] / closes[i - 1]) for i in range(1, n_bars)]

    for name, (kind, minutes) in zip(feature_names, parsed, strict=True):
        bars_back = math.ceil(minutes / bar_minutes)
        if bars_back < 1:
            return None

        if kind == "ret":
            if n_bars < bars_back + 1:
                return None
            out[name] = math.log(closes[-1] / closes[-1 - bars_back])
            continue

        if kind == "rv":
            # Need bars_back log returns; that's bars_back+1 closes.
            if len(log_returns) < bars_back:
                return None
            recent = log_returns[-bars_back:]
            out[name] = _pop_stdev(recent)
            continue

        if kind == "mom_z":
            if len(log_returns) < bars_back:
                return None
            recent = log_returns[-bars_back:]
            mean = sum(recent) / len(recent)
            stdev = _pop_stdev(recent)
            if stdev <= 0:
                out[name] = 0.0
            else:
                # Cumulative recent return / std-error of the sum
                cum = sum(recent)
                # std of sum of N i.i.d. = stdev * sqrt(N)
                out[name] = cum / (stdev * math.sqrt(len(recent)))
            # mean is unused beyond the stdev calc; reference to silence
            # potential unused-var lint without changing behaviour.
            del mean
            continue

        # require_supported guarantees we never reach here.
        raise AssertionError(f"unhandled feature kind {kind!r}")

    return out


def _pop_stdev(values: Sequence[float]) -> float:
    """Population standard deviation; returns 0.0 if len < 2."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


# Light helper kept here so callers don't have to import Decimal just
# to build a synthetic BarEvent for unit tests.
def _decimal(value: float | str) -> Decimal:
    return Decimal(str(value))
