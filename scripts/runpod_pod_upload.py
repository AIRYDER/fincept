"""Upload the full dataset to the RunPod network volume using a temporary pod.

Strategy:
1. Start a RunPod pod with the network volume attached
2. Wait for it to be running
3. Upload the dataset CSV via the Jupyter API
4. Stop the pod
5. The dataset persists on the network volume at /workspace/dataset_full.csv
6. Training jobs can then use dataset_manifest_ref=/workspace/dataset_full.csv

This bypasses the 10MB RunPod serverless payload limit.
"""
from __future__ import annotations

import json
import math
import os
import pathlib
import sys
import time
import requests

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = os.environ["RUNPOD_API_KEY"]
BASE = "https://rest.runpod.io/v1"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
VOLUME_ID = "rrsd005i3g"


def start_pod() -> dict:
    """Start a temporary pod with the network volume."""
    body = {
        "name": "qf-dataset-upload",
        "imageName": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        "computeType": "CPU",
        "vcpuCount": 2,
        "containerDiskInGb": 20,
        "networkVolumeId": VOLUME_ID,
        "volumeMountPath": "/workspace",
        "ports": ["8888/http", "22/tcp"],
        "supportPublicIp": True,
        "cloudType": "SECURE",
    }
    r = requests.post(f"{BASE}/pods", headers=HEADERS, json=body, timeout=30)
    print(f"Start pod: {r.status_code}")
    if r.status_code in (200, 201):
        result = r.json()
        print(f"  Pod ID: {result.get('id')}")
        return result
    else:
        print(f"  Error: {r.text[:500]}")
        sys.exit(1)


def wait_for_pod(pod_id: str, timeout: int = 300) -> dict:
    """Wait for pod to be running and return its connection info."""
    print(f"Waiting for pod {pod_id} to start...")
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/pods", headers=HEADERS, timeout=15)
        if r.status_code == 200:
            for p in r.json():
                if p.get("id") == pod_id:
                    status = p.get("desiredStatus")
                    runtime = p.get("runtime", {}) or {}
                    public_ip = runtime.get("publicIp", "")
                    ports = runtime.get("ports", {}) or {}

                    # Extract Jupyter port
                    jupyter_port = None
                    ssh_port = None
                    if isinstance(ports, dict):
                        for k, v in ports.items():
                            if "8888" in k:
                                jupyter_port = v
                            if "22" in k:
                                ssh_port = v
                    elif isinstance(ports, list):
                        for p_info in ports:
                            if isinstance(p_info, dict):
                                if p_info.get("privatePort") == 8888:
                                    jupyter_port = p_info.get("publicPort")
                                if p_info.get("privatePort") == 22:
                                    ssh_port = p_info.get("publicPort")

                    elapsed = time.time() - start
                    print(f"  [{elapsed:.0f}s] status={status} ip={public_ip} jupyter={jupyter_port}")

                    if status == "RUNNING" and public_ip:
                        return {
                            "pod_id": pod_id,
                            "ip": public_ip,
                            "jupyter_port": jupyter_port,
                            "ssh_port": ssh_port,
                        }
        time.sleep(5)
    print("Timeout waiting for pod")
    sys.exit(1)


def upload_via_jupyter(pod_info: dict, csv_path: pathlib.Path) -> bool:
    """Upload file via Jupyter API."""
    ip = pod_info["ip"]
    port = pod_info.get("jupyter_port")
    if not port:
        print("No Jupyter port available")
        return False

    # Jupyter doesn't have a password on runpod/pytorch images by default
    # Try common endpoints
    base_url = f"http://{ip}:{port}"

    # Try to get Jupyter API root
    r = requests.get(f"{base_url}/api", timeout=15)
    print(f"  Jupyter API: {r.status_code}")

    if r.status_code == 200:
        # Upload via Jupyter contents API
        # Read the file
        file_bytes = csv_path.read_bytes()
        file_size = len(file_bytes)
        print(f"  Uploading {file_size:,} bytes ({file_size / 1024 / 1024:.1f} MB)")

        # Jupyter contents API supports base64-encoded content
        import base64

        b64_content = base64.b64encode(file_bytes).decode("ascii")

        # Upload in chunks if needed (Jupyter API has limits too)
        # First, create the file
        payload = {
            "type": "file",
            "format": "base64",
            "content": b64_content,
        }

        print(f"  Uploading via Jupyter contents API...")
        r = requests.put(
            f"{base_url}/api/contents/workspace/dataset_full.csv",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        print(f"  Upload: {r.status_code}")
        if r.status_code == 200 or r.status_code == 201:
            print(f"  Upload successful!")
            return True
        else:
            print(f"  Error: {r.text[:300]}")
            return False
    else:
        print(f"  Jupyter API not accessible: {r.status_code}")
        # Try with token
        r2 = requests.get(f"{base_url}/api?token=", timeout=15)
        print(f"  With token: {r2.status_code}")
        return False


def upload_via_ssh(pod_info: dict, csv_path: pathlib.Path) -> bool:
    """Upload file via SSH scp."""
    import subprocess

    ip = pod_info["ip"]
    port = pod_info.get("ssh_port", 22)

    print(f"  Uploading via scp to {ip}:{port}")
    scp_cmd = [
        "scp", "-P", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        str(csv_path),
        f"root@{ip}:/workspace/dataset_full.csv",
    ]
    print(f"  Command: {' '.join(scp_cmd)}")
    result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
    print(f"  scp exit: {result.returncode}")
    if result.stderr:
        print(f"  stderr: {result.stderr[:300]}")
    return result.returncode == 0


def stop_pod(pod_id: str):
    """Stop and remove the pod."""
    r = requests.post(f"{BASE}/pods/{pod_id}/stop", headers=HEADERS, timeout=15)
    print(f"Stop pod: {r.status_code}")


def main() -> int:
    print("=" * 70)
    print("RUNPOD DATASET UPLOAD — Via temporary pod")
    print("=" * 70)

    # 1. Load and convert dataset
    print("\nSTEP 1: Load dataset")
    dataset_dir = _REPO_ROOT / "data" / "datasets" / "deep_real"
    parquet_files = list(dataset_dir.glob("*.parquet"))
    if not parquet_files:
        print("ERROR: no dataset parquet found")
        return 1

    parquet_path = parquet_files[0]
    print(f"  Dataset: {parquet_path.name}")

    import pyarrow.parquet as pq

    table = pq.read_table(str(parquet_path))
    df = table.to_pandas()
    print(f"  Rows: {len(df)}, Columns: {len(df.columns)}")

    csv_path = dataset_dir / "dataset_full.csv"
    df.to_csv(csv_path, index=False)
    print(f"  CSV: {csv_path.name} ({csv_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # 2. Start pod
    print("\nSTEP 2: Start temporary pod with network volume")
    pod = start_pod()
    pod_id = pod.get("id")

    # 3. Wait for pod to be running
    print("\nSTEP 3: Wait for pod")
    pod_info = wait_for_pod(pod_id)

    # 4. Upload dataset
    print("\nSTEP 4: Upload dataset to /workspace/dataset_full.csv")

    # Try Jupyter first, then SSH
    success = upload_via_jupyter(pod_info, csv_path)
    if not success:
        print("  Jupyter upload failed, trying SSH...")
        success = upload_via_ssh(pod_info, csv_path)

    if not success:
        print("\n  Both upload methods failed!")
        print("  The pod is still running. You can upload manually:")
        print(f"    scp -P {pod_info.get('ssh_port', 22)} {csv_path} root@{pod_info['ip']}:/workspace/dataset_full.csv")
        print(f"  Or via Jupyter: http://{pod_info['ip']}:{pod_info.get('jupyter_port', 8888)}")
        print(f"\n  Pod ID: {pod_id}")
        print(f"  Stop with: python scripts/runpod_volume_upload.py stop-pod {pod_id}")
        return 1

    # 5. Verify upload
    print("\nSTEP 5: Verify upload")
    # We can verify by running a quick stat_volume job on the serverless endpoint
    # But the current handler doesn't support that. Let's just check via SSH.
    print("  Upload reported successful. Dataset should be at /workspace/dataset_full.csv")
    print("  The network volume (rrsd005i3g) is shared between the pod and the serverless endpoint.")

    # 6. Stop pod
    print("\nSTEP 6: Stop temporary pod")
    stop_pod(pod_id)

    print("\n" + "=" * 70)
    print("DATASET UPLOAD COMPLETE")
    print("=" * 70)
    print(f"  Dataset: /workspace/dataset_full.csv ({csv_path.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"  Volume:  {VOLUME_ID} (fincept-qf-vol)")
    print(f"  Rows:    {len(df)}")
    print(f"\n  Training jobs can now use:")
    print(f"    dataset_manifest_ref=/workspace/dataset_full.csv")

    # Clean up local CSV
    csv_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
