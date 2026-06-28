"""List all datasets on the RunPod volume."""
import boto3

VOLUME_ID = "rrsd005i3g"
REGION = "US-NC-1"
ENDPOINT = f"https://s3api-{REGION}.runpod.io/"
S3_ACCESS_KEY = "user_32lBXOnFFDX3g9rIuiuE0xg1U1h"
S3_SECRET_KEY = "rps_LWRJPV4VJLIUPROC43HC3IB7MG06GK4DF4YSD1SUnj5x7m"

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
