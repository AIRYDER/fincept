"""Prepare merged dataset for LightGBM training (strip non-numeric cols)."""
import pathlib
import pandas as pd
import numpy as np

_REPO = pathlib.Path(__file__).resolve().parent.parent
_INDIR = _REPO / "data" / "datasets" / "merged"
_OUTDIR = _REPO / "data" / "datasets" / "merged_lgbm"
_OUTDIR.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(_INDIR / "merged_dataset.csv")
print(f"Input: {len(df)} rows, {len(df.columns)} cols")

# Strip symbol column (keep only numeric)
out = df.drop(columns=["symbol"])
out["decision_time"] = out["decision_time"].astype(np.int64)
out["label"] = out["label"].astype(int)

# Reorder: decision_time first, label last
cols = ["decision_time"] + [c for c in out.columns if c not in ("decision_time", "label")] + ["label"]
out = out[cols]

print(f"Output: {len(out)} rows, {len(out.columns)} cols")
print(f"Columns: {list(out.columns)}")

out_path = _OUTDIR / "dataset_full.csv"
out.to_csv(out_path, index=False)
print(f"\nSaved: {out_path}")
print(f"Size: {out_path.stat().st_size / 1024 / 1024:.1f} MB")
