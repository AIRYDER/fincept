"""Upload dataset to RunPod network volume via S3-compatible API (boto3)."""

import os
import pathlib
import sys

import boto3

_REPO = pathlib.Path(__file__).resolve().parent.parent

VOLUME_ID = os.environ.get("RUNPOD_S3_BUCKET")
REGION = "US-NC-1"
ENDPOINT = os.environ.get("RUNPOD_S3_ENDPOINT")

# RunPod S3 API keys (separate from the RunPod API key)
S3_ACCESS_KEY = os.environ.get("RUNPOD_S3_ACCESS_KEY")
S3_SECRET_KEY = os.environ.get("RUNPOD_S3_SECRET_KEY")

for _var, _val in (
    ("RUNPOD_S3_ACCESS_KEY", S3_ACCESS_KEY),
    ("RUNPOD_S3_SECRET_KEY", S3_SECRET_KEY),
    ("RUNPOD_S3_ENDPOINT", ENDPOINT),
    ("RUNPOD_S3_BUCKET", VOLUME_ID),
):
    if not _val:
        print(f"Missing required environment variable: {_var}", file=sys.stderr)
        sys.exit(1)

csv_path = _REPO / "data" / "datasets" / "deep_real" / "dataset_full.csv"
s3_key = "datasets/deep_real/dataset_full.csv"

print(f"Uploading {csv_path} ({csv_path.stat().st_size / 1024 / 1024:.1f} MB)")
print(f"  to s3://{VOLUME_ID}/{s3_key}")
print(f"  endpoint: {ENDPOINT}")

s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
)

# Upload
with open(csv_path, "rb") as f:
    s3.upload_fileobj(f, VOLUME_ID, s3_key)
print("  Upload successful!")

# Verify
print("\nVerifying upload...")
try:
    response = s3.head_object(Bucket=VOLUME_ID, Key=s3_key)
    print(f"  Content-Length: {response.get('ContentLength', 0):,} bytes")
except Exception as exc:
    print(f"  head_object not supported ({exc}), using list instead")

# List directory
print("\nListing s3://rrsd005i3g/datasets/deep_real/")
response = s3.list_objects_v2(Bucket=VOLUME_ID, Prefix="datasets/deep_real/")
for obj in response.get("Contents", []):
    print(f"  {obj['Key']}: {obj['Size']:,} bytes")

print("\nDataset is now on the network volume.")
print("  Pod path:         /workspace/datasets/deep_real/dataset_full.csv")
print("  Serverless path:  /runpod-volume/datasets/deep_real/dataset_full.csv")
