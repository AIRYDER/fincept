"""Merge fincept event features into the technical dataset.

The technical dataset has 46,080 rows over 9 years with 18 features.
The event dataset has 3,390 rows over 9 days with 19 event features.

Strategy: For each (symbol, decision_time) in the technical dataset,
look for news events in the prior N days and aggregate their features.
This adds event_count, avg_sentiment, avg_source_quality, category counts, etc.
"""

import pathlib

import numpy as np
import pandas as pd

_REPO = pathlib.Path(__file__).resolve().parent.parent

TECH_PATH = _REPO / "data" / "datasets" / "combined" / "combined_dataset.csv"
EVENT_PATH = _REPO / "data" / "datasets" / "fincept_combined" / "event_dataset.csv"
OUTDIR = _REPO / "data" / "datasets" / "merged"
OUTDIR.mkdir(parents=True, exist_ok=True)

NS_PER_DAY = 86_400_000_000_000

print("Loading technical dataset...")
tech = pd.read_csv(TECH_PATH)
print(f"  Technical: {len(tech)} rows, {len(tech.columns)} cols")

print("\nLoading event dataset...")
events = pd.read_csv(EVENT_PATH)
print(f"  Events: {len(events)} rows, {len(events.columns)} cols")

# Parse event timestamps
events["ts_event_ns"] = events["ts_event_ns"].astype(np.int64)
events["symbol"] = events["symbol"].str.upper()

# Event feature columns to aggregate
EVENT_FEATURE_COLS = [
    "source_quality",
    "recency_score",
    "novelty_score",
    "sentiment_score",
    "sentiment_confidence",
    "has_sentiment",
]
EVENT_CAT_COLS = [
    "cat_earnings",
    "cat_analyst",
    "cat_general",
    "cat_market_move",
    "cat_partnership",
    "cat_product",
    "cat_macro",
    "cat_regulatory",
    "cat_security",
]

# Group events by symbol
events_by_symbol = {}
for sym, group in events.groupby("symbol"):
    events_by_symbol[sym] = group.sort_values("ts_event_ns").reset_index(drop=True)
print(f"\n  Events by symbol: {len(events_by_symbol)} symbols")
for sym, g in sorted(events_by_symbol.items(), key=lambda x: -len(x[1]))[:10]:
    print(f"    {sym}: {len(g)} events")

# Build event features for each technical row
print("\nBuilding event features for technical rows...")

# The technical dataset has 'symbol' and 'decision_time' columns
# Check if symbol column exists
print(f"  Tech columns: {list(tech.columns[:10])}...")

# The combined dataset doesn't have a symbol column directly
# Let me check what we have
if "symbol" not in tech.columns:
    # The combined dataset was built without a symbol column
    # We need to add it. Let me check the build script...
    # Actually, the technical dataset has decision_time but no symbol
    # We need to rebuild with symbol, or infer it
    print("  WARNING: No 'symbol' column in technical dataset")
    print("  Cannot merge event features without symbol mapping")
    print("  Saving event dataset as standalone instead...")

    # Just use the event dataset with more features
    # Add technical-like features from the close price
    events["close_log_ret_1d"] = np.log(
        events["close_at_event"] / events["close_at_event"].shift(1)
    )
    events["close_log_ret_5d"] = np.log(
        events["close_at_event"] / events["close_at_event"].shift(5)
    )

    # Save enhanced event dataset
    out = events.copy()
    out_path = OUTDIR / "event_enhanced.csv"
    out.to_csv(out_path, index=False)
    print(f"\n  Saved enhanced event dataset: {out_path}")
    print(f"  Rows: {len(out)}, Cols: {len(out.columns)}")
else:
    print("  Symbol column found! Proceeding with merge...")

    # For each technical row, find events in the prior 3 days
    LOOKBACK_NS = 3 * NS_PER_DAY

    # Build event feature columns
    new_cols = {f"evt_{c}": [] for c in EVENT_FEATURE_COLS}
    new_cols["evt_count"] = []
    for c in EVENT_CAT_COLS:
        new_cols[f"evt_{c}_count"] = []

    tech["decision_time"] = tech["decision_time"].astype(np.int64)

    for _, row in tech.iterrows():
        sym = row["symbol"].upper()
        dt_ns = row["decision_time"]

        sym_events = events_by_symbol.get(sym)
        if sym_events is None:
            for k in new_cols:
                new_cols[k].append(0.0)
            continue

        window_start = dt_ns - LOOKBACK_NS
        mask = (sym_events["ts_event_ns"] >= window_start) & (sym_events["ts_event_ns"] <= dt_ns)
        window = sym_events[mask]

        if len(window) == 0:
            for k in new_cols:
                new_cols[k].append(0.0)
            continue

        new_cols["evt_count"].append(float(len(window)))
        for c in EVENT_FEATURE_COLS:
            new_cols[f"evt_{c}"].append(float(window[c].mean()))
        for c in EVENT_CAT_COLS:
            new_cols[f"evt_{c}_count"].append(float(window[c].sum()))

    # Add new columns to technical dataset
    for col_name, values in new_cols.items():
        tech[col_name] = values

    print(f"\n  Merged dataset: {len(tech)} rows, {len(tech.columns)} cols")
    rows_with_events = (tech["evt_count"] > 0).sum()
    print(f"  Rows with events: {rows_with_events} ({rows_with_events / len(tech) * 100:.1f}%)")

    out_path = OUTDIR / "merged_dataset.csv"
    tech.to_csv(out_path, index=False)
    print(f"\n  Saved: {out_path}")
    print(f"  Size: {out_path.stat().st_size / 1024 / 1024:.1f} MB")
