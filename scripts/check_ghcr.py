"""Check if ghcr.io image is publicly pullable."""

import httpx

r = httpx.get(
    "https://ghcr.io/v2/airyder/fincept/quant-foundry-training/manifests/latest",
    headers={"Accept": "application/vnd.oci.image.index.v1+json"},
    timeout=15.0,
)
print(f"Status: {r.status_code}")
auth = r.headers.get("www-authenticate", "none")
print(f"Www-Authenticate: {auth}")
print(f"Body: {r.text[:300]}")
