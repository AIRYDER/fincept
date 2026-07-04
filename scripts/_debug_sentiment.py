"""Debug sentiment feature alignment."""

import datetime as dt

# Check news article timestamps
articles_file = None  # We didn't save them, let me check the dataset

# Check the dataset's last decision times
import pyarrow.parquet as pq

df = pq.read_table("data/datasets/combined/combined_dataset.parquet").to_pandas()
last_dts = sorted(df["decision_time"].unique())[-10:]
print("Last 10 decision_times in dataset:")
for t in last_dts:
    d = dt.datetime.fromtimestamp(t / 1e9, tz=dt.UTC)
    print(f"  {t} -> {d}")

# Check sentiment feature values
print("\nSentiment feature values (last 100 rows):")
sent_cols = [c for c in df.columns if c.startswith("sent_")]
print(df[sent_cols].tail(100).describe())

# Check if ANY sentiment features are non-zero
for col in sent_cols:
    nonzero = (df[col] != 0).sum()
    if nonzero > 0:
        print(f"  {col}: {nonzero} non-zero values")
