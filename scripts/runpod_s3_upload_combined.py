"""Upload combined dataset to RunPod network volume via S3."""

import os
import pathlib
import sys

import boto3

_REPO = pathlib.Path(__file__).resolve().parent.parent

VOLUME_ID = os.environ.get("RUNPOD_S3_BUCKET")
REGION = "US-NC-1"
ENDPOINT = os.environ.get("RUNPOD_S3_ENDPOINT")
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

csv_path = _REPO / "data" / "datasets" / "combined" / "combined_dataset.csv"
s3_key = "datasets/combined/combined_dataset.csv"

print(f"Uploading {csv_path} ({csv_path.stat().st_size / 1024 / 1024:.1f} MB)")
print(f"  to s3://{VOLUME_ID}/{s3_key}")

s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
)

s3.upload_file(str(csv_path), VOLUME_ID, s3_key)
print("Upload successful!")

# List to verify
resp = s3.list_objects_v2(Bucket=VOLUME_ID, Prefix="datasets/combined/")
for obj in resp.get("Contents", []):
    print(f"  {obj['Key']}: {obj['Size']:,} bytes")

print("\nDataset is now on the network volume.")
print("  Serverless path:  /runpod-volume/datasets/combined/combined_dataset.csv")
