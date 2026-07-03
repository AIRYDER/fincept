"""Upload merged dataset to RunPod."""
import pathlib
import boto3

_REPO = pathlib.Path(__file__).resolve().parent.parent

VOLUME_ID = "rrsd005i3g"
REGION = "US-NC-1"
ENDPOINT = f"https://s3api-{REGION}.runpod.io/"
S3_ACCESS_KEY = "user_32lBXOnFFDX3g9rIuiuE0xg1U1h"
S3_SECRET_KEY = "rps_LWRJPV4VJLIUPROC43HC3IB7MG06GK4DF4YSD1SUnj5x7m"

csv_path = _REPO / "data" / "datasets" / "merged_lgbm" / "dataset_full.csv"
s3_key = "datasets/merged_lgbm/dataset_full.csv"

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

resp = s3.list_objects_v2(Bucket=VOLUME_ID, Prefix="datasets/merged_lgbm/")
for obj in resp.get("Contents", []):
    print(f"  {obj['Key']}: {obj['Size']:,} bytes")

print(f"\nServerless path: /runpod-volume/datasets/merged_lgbm/dataset_full.csv")
