"""Start a RunPod pod with the network volume, upload dataset via SSH, then stop it."""

import json
import os
import pathlib
import subprocess
import sys
import time

import requests

KEY = os.environ["RUNPOD_API_KEY"]
BASE = "https://rest.runpod.io/v1"
HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
VOLUME_ID = "rrsd005i3g"
REPO = pathlib.Path(__file__).resolve().parent.parent

cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

if cmd == "start-pod":
    # Start a cheap pod with the network volume attached
    # Use RTX 4090 (same as endpoint) or whatever is cheapest
    body = {
        "name": "qf-dataset-upload",
        "imageName": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        "gpuCount": 1,
        "gpuTypeId": "NVIDIA GeForce RTX 4090",
        "containerDiskInGb": 20,
        "networkVolumeId": VOLUME_ID,
        "volumeMountPath": "/workspace",
        "ports": "22/tcp",
        "env": {
            "PUBLIC_KEY": os.environ.get("RUNPOD_SSH_PUBLIC_KEY", ""),
        },
        "startSsh": True,
    }
    # Remove empty PUBLIC_KEY if not set
    if not body["env"]["PUBLIC_KEY"]:
        body.pop("env")

    r = requests.post(f"{BASE}/pods", headers=HEADERS, json=body, timeout=30)
    print(f"Start pod: {r.status_code}")
    if r.status_code == 200:
        result = r.json()
        print(json.dumps(result, indent=2))
        pod_id = result.get("id")
        print(f"\nPod ID: {pod_id}")
        print("Waiting for pod to start...")
        # Wait for pod to be running
        for i in range(60):
            time.sleep(5)
            r2 = requests.get(f"{BASE}/pods", headers=HEADERS, timeout=15)
            if r2.status_code == 200:
                pods = r2.json()
                for p in pods:
                    if p.get("id") == pod_id:
                        status = p.get("desiredStatus")
                        runtime = p.get("runtime", {})
                        public_ip = runtime.get("publicIp", "") if isinstance(runtime, dict) else ""
                        ports = runtime.get("ports", {}) if isinstance(runtime, dict) else {}
                        print(f"  [{i * 5}s] status={status} ip={public_ip} ports={ports}")
                        if status == "RUNNING" and public_ip:
                            print("\nPod is running!")
                            print(f"  IP: {public_ip}")
                            print(f"  SSH: ssh root@{public_ip} -p <port>")
                            print(
                                f"\nNext: python scripts/runpod_volume_upload.py ssh-upload {pod_id} {public_ip}"
                            )
                            sys.exit(0)
        print("Timeout waiting for pod")
    else:
        print(f"Error: {r.text[:500]}")

elif cmd == "list-pods":
    r = requests.get(f"{BASE}/pods", headers=HEADERS, timeout=15)
    print(f"Pods: {r.status_code}")
    if r.status_code == 200:
        for p in r.json():
            pid = p.get("id")
            name = p.get("name")
            status = p.get("desiredStatus")
            runtime = p.get("runtime", {})
            ip = runtime.get("publicIp", "") if isinstance(runtime, dict) else ""
            vol = p.get("networkVolumeId", "")
            print(f"  {pid}: name={name} status={status} ip={ip} vol={vol}")

elif cmd == "stop-pod":
    pod_id = sys.argv[2]
    r = requests.post(f"{BASE}/pods/{pod_id}/stop", headers=HEADERS, timeout=15)
    print(f"Stop pod {pod_id}: {r.status_code}")
    print(r.text[:300])

elif cmd == "ssh-upload":
    # Upload dataset to pod via SSH
    pod_id = sys.argv[2]
    # Find the pod's IP and SSH port
    r = requests.get(f"{BASE}/pods", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print("Failed to list pods")
        sys.exit(1)

    pod = None
    for p in r.json():
        if p.get("id") == pod_id:
            pod = p
            break

    if not pod:
        print(f"Pod {pod_id} not found")
        sys.exit(1)

    runtime = pod.get("runtime", {})
    public_ip = runtime.get("publicIp", "")
    ports = runtime.get("ports", {})
    # Find SSH port
    ssh_port = None
    if isinstance(ports, dict):
        for k, v in ports.items():
            if "22" in k:
                ssh_port = v
    elif isinstance(ports, list):
        for p_info in ports:
            if isinstance(p_info, dict) and "22" in str(p_info.get("privatePort", "")):
                ssh_port = p_info.get("publicPort")

    if not public_ip:
        print(f"Pod {pod_id} has no public IP. Status: {pod.get('desiredStatus')}")
        sys.exit(1)

    print(f"Pod IP: {public_ip}")
    print(f"SSH port: {ssh_port or 22}")

    # Find the dataset
    dataset_dir = REPO / "data" / "datasets" / "deep_real"
    parquet_files = list(dataset_dir.glob("*.parquet"))
    if not parquet_files:
        print("No dataset found")
        sys.exit(1)

    parquet_path = parquet_files[0]
    print(f"Dataset: {parquet_path.name}")

    # Convert to CSV locally
    import pyarrow.parquet as pq

    table = pq.read_table(str(parquet_path))
    df = table.to_pandas()
    csv_path = REPO / "data" / "datasets" / "deep_real" / "dataset_full.csv"
    df.to_csv(csv_path, index=False)
    print(f"CSV: {csv_path.name} ({csv_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # Upload via scp
    ssh_target = f"root@{public_ip}"
    scp_cmd = [
        "scp",
        "-P",
        str(ssh_port or 22),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        str(csv_path),
        f"{ssh_target}:/workspace/dataset_full.csv",
    ]
    print(f"\nUploading via scp: {' '.join(scp_cmd)}")
    result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
    print(f"scp exit: {result.returncode}")
    if result.stdout:
        print(f"stdout: {result.stdout[:200]}")
    if result.stderr:
        print(f"stderr: {result.stderr[:200]}")

    if result.returncode == 0:
        print("\nUpload successful!")
        print("Dataset is now at /workspace/dataset_full.csv on the network volume")
        print("Training jobs can now use dataset_manifest_ref=/workspace/dataset_full.csv")
    else:
        print("\nUpload failed. Trying with ssh key...")
        # Try with ssh key if available
        ssh_key = os.environ.get("RUNPOD_SSH_KEY", "")
        if ssh_key and pathlib.Path(ssh_key).exists():
            scp_cmd.extend(["-i", ssh_key])
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
            print(f"scp with key exit: {result.returncode}")

elif cmd == "help":
    print("Usage:")
    print("  python scripts/runpod_volume_upload.py start-pod")
    print("  python scripts/runpod_volume_upload.py list-pods")
    print("  python scripts/runpod_volume_upload.py ssh-upload <pod-id>")
    print("  python scripts/runpod_volume_upload.py stop-pod <pod-id>")
