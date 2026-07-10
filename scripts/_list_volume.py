"""List all datasets on the RunPod volume."""

import os
import sys

import boto3

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

s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
)

resp = s3.list_objects_v2(Bucket=VOLUME_ID, Prefix="datasets/")
for obj in resp.get("Contents", []):
    print(f"  {obj['Key']}: {obj['Size']:,} bytes")
