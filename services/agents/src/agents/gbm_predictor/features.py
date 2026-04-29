"""
agents.gbm_predictor.features - feature spec + online lookup.

The list ``FEATURES`` is the canonical input vector for the model.
Order matters: lightgbm expects the same feature order at train and
inference time.  Changing this list requires retraining.

The actual features are computed by services/features (TASK-016).  This
module only depends on the *names* and the OnlineStore wire format.
"""

from __future__ import annotations

from features.store import OnlineStore

# Feature order is fixed at train-time and read at inference-time from
# meta.json.  This list is the v1 default; if the trainer reads a
# parquet that exposes a different feature set, it should still write
# its own list to meta.json so the inference loop reads from there.
FEATURES: list[str] = [
    "ret_1m",
    "ret_5m",
    "ret_15m",
    "ret_60m",
    "rv_5m",
    "rv_30m",
    "mom_z_30m",
    "mom_z_240m",
    "book_imbalance_1",
    "spread_bps",
]


async def load_live(
    store: OnlineStore,
    symbol: str,
    *,
    feature_names: list[str],
    freq: str = "1m",
) -> dict[str, float] | None:
    """Read the latest FeatureFrame and project it onto ``feature_names``.

    Returns ``None`` if:
      - the FeatureFrame is missing entirely (cache miss / stale), OR
      - any required feature is missing or null in the frame.

    Returning ``None`` instead of raising lets the inference loop quietly
    skip a symbol when its feature pipeline isn't producing fresh data
    yet (e.g., during the warm-up window after a service restart).
    """
    frame = await store.get_latest(symbol, freq=freq)
    if frame is None:
        return None
    out: dict[str, float] = {}
    for name in feature_names:
        value = frame.values.get(name)
        if value is None:
            return None
        out[name] = float(value)
    return out
