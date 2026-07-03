"""Prepare the fincept event dataset for LightGBM training.

Converts the event dataset into the format expected by the RunPod trainer:
  - decision_time (ns), label, feature_1, feature_2, ...
"""
import pathlib
import pandas as pd
import numpy as np

_REPO = pathlib.Path(__file__).resolve().parent.parent
_INDIR = _REPO / "data" / "datasets" / "fincept_combined"
_OUTDIR = _REPO / "data" / "datasets" / "fincept_event_lgbm"
_OUTDIR.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(_INDIR / "event_dataset.csv")
print(f"Input: {len(df)} rows, {len(df.columns)} columns")
print(f"Columns: {list(df.columns)}")

# Select numeric features only
# Drop: event_id, symbol, ts_event_dt, date, headline, event_category, source
# Keep: ts_event_ns -> decision_time, forward_label_5d -> label
feature_cols = [
    "source_quality",
    "recency_score",
    "novelty_score",
    "sentiment_score",
    "sentiment_confidence",
    "has_sentiment",
    "hour_of_day",
    "day_of_week",
    "is_market_hours",
    "cat_earnings",
    "cat_analyst",
    "cat_general",
    "cat_market_move",
    "cat_partnership",
    "cat_product",
    "cat_macro",
    "cat_regulatory",
    "cat_security",
    "close_at_event",
]

# Build the trainer-compatible dataframe
out = pd.DataFrame()
out["decision_time"] = df["ts_event_ns"].astype(np.int64)
out["label"] = df["forward_label_5d"].astype(int)

for i, col in enumerate(feature_cols, 1):
    out[f"feature_{i:02d}"] = df[col].astype(float)

print(f"\nOutput: {len(out)} rows, {len(out.columns)} columns")
print(f"Features: {len(feature_cols)}")
print(f"Label balance: {(out['label'] == 1).sum()} up / {(out['label'] == 0).sum()} down "
      f"({(out['label'] == 1).mean() * 100:.1f}% up)")
print(f"Columns: {list(out.columns)}")

# Save
out_path = _OUTDIR / "dataset_full.csv"
out.to_csv(out_path, index=False)
print(f"\nSaved: {out_path}")
print(f"Size: {out_path.stat().st_size / 1024:.1f} KB")

# Also save feature names for reference
feat_names = {f"feature_{i:02d}": col for i, col in enumerate(feature_cols, 1)}
import json
with open(_OUTDIR / "feature_names.json", "w") as f:
    json.dump(feat_names, f, indent=2)
print(f"Feature names saved: {_OUTDIR / 'feature_names.json'}")
