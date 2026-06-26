"""
agents.gbm_predictor.features - feature spec + online lookup.

The list ``FEATURES`` is the canonical input vector for the model.
Order matters: lightgbm expects the same feature order at train and
inference time.  Changing this list requires retraining.

The actual features are computed by services/features (TASK-016).  This
module only depends on the *names* and the OnlineStore wire format.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

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

# The live feature service has evolved from the original GBM training
# fixture names.  Keep a narrow compatibility layer so old model
# artifacts can still run online while the trainer catches up.
FEATURE_ALIASES: dict[str, str] = {
    "ret_1m": "ret_simple_1",
    "ret_5m": "mom_5",
    "ret_15m": "mom_15",
    "ret_60m": "mom_60",
    "rv_5m": "vol_rs_5",
    "rv_30m": "vol_rs_30",
}

DEFAULTABLE_FEATURES: set[str] = {
    # Warm-up windows.  ret_1m remains strict so we don't predict before
    # the live price feed has produced at least one actual return.
    "ret_5m",
    "ret_15m",
    "ret_60m",
    "rv_5m",
    "rv_30m",
    # Long-window and book-derived features may be unavailable in the
    # first minutes of a dev session.  Neutral 0.0 lets the operator see
    # live predictions instead of a silent empty panel.
    "mom_z_30m",
    "mom_z_240m",
    "book_imbalance_1",
    "spread_bps",
}


def _compute_feature_schema_hash(feature_names: list[str]) -> str:
    """SHA-256 (64-char lowercase hex) of the sorted feature-name list.

    Binds a :class:`FeatureSnapshot` to the feature schema that defines
    the keys of each row's ``features`` dict.  Sorting makes the hash
    order-independent so two agents with the same feature set (but
    different training-time column order) produce the same hash.
    """
    payload = json.dumps(sorted(feature_names), separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class FeatureHealth:
    """Per-cycle feature-availability diagnostics.

    Returned alongside the feature vector from :func:`load_live` so the
    publish loop can record a sidecar JSONL row (``FeatureHealthRow``)
    without having to re-derive what was missing / defaulted / aliased.

    Semantics:

      * ``missing`` -- the canonical feature name was absent from the
        online frame (direct lookup returned ``None``) AND it was not
        recovered via an alias.  A feature that fell back to the 0.0
        default appears here *and* in ``defaulted``; a feature that was
        not recoverable at all causes :func:`load_live` to return
        ``None`` (no health row is emitted for that cycle).
      * ``defaulted`` -- the feature was filled with the 0.0 compat
        default (subset of ``missing``).
      * ``aliased`` -- the feature was resolved via
        :data:`FEATURE_ALIASES` (the canonical name was absent but the
        alias name was present).  These are NOT in ``missing`` -- the
        data exists, just under a legacy name.
    """

    missing: list[str]
    defaulted: list[str]
    aliased: list[str]


async def load_live(
    store: OnlineStore,
    symbol: str,
    *,
    feature_names: list[str],
    freq: str = "1m",
    allow_compat_defaults: bool = False,
    frame_ts_out: list[int] | None = None,
) -> tuple[dict[str, float], FeatureHealth] | None:
    """Read the latest FeatureFrame and project it onto ``feature_names``.

    Returns ``None`` if:
      - the FeatureFrame is missing entirely (cache miss / stale), OR
      - any required feature is missing or null in the frame.

    Returning ``None`` instead of raising lets the inference loop quietly
    skip a symbol when its feature pipeline isn't producing fresh data
    yet (e.g., during the warm-up window after a service restart).

    When ``allow_compat_defaults`` is true, legacy model feature names
    are projected from the current online feature vocabulary and a
    small set of non-price features may default to 0.0.  This is used
    only by the live GBM agent so older trained artifacts don't leave
    the dashboard permanently empty.

    The second element of the returned tuple is a :class:`FeatureHealth`
    snapshot describing which requested features were missing from the
    online frame, which fell back to a default, and which were resolved
    via an alias.  Callers that don't care about diagnostics can ignore
    it; the dict is always the first element so existing unpacking
    ``features, _ = await load_live(...)`` works.

    If ``frame_ts_out`` is provided, the frame's ``ts_event`` is appended
    to it on a successful read so the caller can capture the point-in-
    time timestamp of the feature data without a second Redis lookup.
    The list is left untouched when the frame is missing (``None``
    return).
    """
    frame = await store.get_latest(symbol, freq=freq)
    if frame is None:
        return None
    if frame_ts_out is not None:
        frame_ts_out.append(frame.ts_event)
    out: dict[str, float] = {}
    missing: list[str] = []
    defaulted: list[str] = []
    aliased: list[str] = []
    for name in feature_names:
        value = frame.values.get(name)
        if value is None and allow_compat_defaults:
            alias = FEATURE_ALIASES.get(name)
            if alias is not None:
                alias_value = frame.values.get(alias)
                if alias_value is not None:
                    value = alias_value
                    aliased.append(name)
                else:
                    # Alias exists but the alias name is also absent --
                    # the canonical feature is genuinely missing.
                    missing.append(name)
            else:
                missing.append(name)
        elif value is None:
            missing.append(name)
        if value is None and allow_compat_defaults and name in DEFAULTABLE_FEATURES:
            value = 0.0
            defaulted.append(name)
        if value is None:
            return None
        out[name] = float(value)
    return out, FeatureHealth(missing=missing, defaulted=defaulted, aliased=aliased)
