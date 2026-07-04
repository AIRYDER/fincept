import pandas as pd
import pyarrow.parquet as pq

df = pq.read_table(
    "data/datasets/deep_real/deep_real_AAPL_AMZN_BAC_DIS_GOOGL_y10_h5d.parquet"
).to_pandas()
dt = pd.to_datetime(df["decision_time"], unit="ns")
print(f"Decision time range: {dt.min()} to {dt.max()}")
print(f"Span: {(dt.max() - dt.min()).days} days")
print(f"Unique decision_times: {df['decision_time'].nunique()}")
print(f"Rows per unique decision_time: {len(df) / df['decision_time'].nunique():.1f}")
print("\nFirst 10 rows:")
print(df.head(10))
print("\nLast 10 rows:")
print(df.tail(10))
# Check if there's a pattern — 5 symbols x ~9232 days = 46160
print(f"\n46160 / 5 = {46160 / 5}")
