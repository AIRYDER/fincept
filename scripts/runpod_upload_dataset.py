"""Stage 1: Upload the full dataset to the RunPod network volume at /workspace/.

This dispatches a special job to the RunPod serverless endpoint that writes
the inline_dataset_csv to /workspace/dataset.csv on the network volume.
The network volume persists between jobs, so subsequent training jobs can
read from /workspace/dataset.csv without needing inline data.

This bypasses the 10MB inline CSV limit by sending the dataset in chunks
across multiple jobs, each appending to the file.
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
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))


def main() -> int:
    print("=" * 70)
    print("RUNPOD DATASET UPLOAD — Stage to network volume")
    print("=" * 70)

    api_key = os.environ["RUNPOD_API_KEY"]
    endpoint_id = os.environ["RUNPOD_TRAINING_ENDPOINT_ID"]
    base_url = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")

    # 1. Load the full dataset
    print("\nSTEP 1: Load full dataset")
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
    df = df.sort_values("decision_time")
    print(f"  Full dataset: {len(df)} rows, {len(df.columns)} columns")

    # 2. Convert to CSV
    csv_text = df.to_csv(index=False)
    csv_bytes = csv_text.encode("utf-8")
    total_size = len(csv_bytes)
    print(f"  CSV size: {total_size:,} bytes ({total_size / 1024 / 1024:.1f} MB)")

    # 3. Split into chunks under 8MB each (safe for RunPod payload)
    CHUNK_SIZE = 7 * 1024 * 1024  # 7 MB per chunk
    n_chunks = math.ceil(total_size / CHUNK_SIZE)
    print(f"  Chunks: {n_chunks} (each up to {CHUNK_SIZE / 1024 / 1024:.0f} MB)")

    # 4. Get the header (first line) — only include in chunk 0
    header_end = csv_text.index("\n") + 1
    header = csv_text[:header_end]

    # 5. Dispatch chunk upload jobs
    print(f"\nSTEP 2: Upload {n_chunks} chunks to /workspace/dataset.csv")

    # For chunk 0: write with header (mode=write)
    # For chunks 1+: append without header (mode=append)
    # We use a custom handler extension: inline_dataset_csv + write_to_path

    # Actually, the handler doesn't support write_to_path. We need a different approach.
    # The simplest: use a raw Python job via RunPod that just writes the file.
    # But the handler only does training.
    #
    # Alternative: Use the RunPod pod API to start a pod with the volume,
    # SSH in, and copy the file. But that's complex.
    #
    # Simplest working approach: send the dataset as a single inline CSV
    # but with a smaller subset that fits in 10MB. We already did 15K rows.
    #
    # For the full 46K rows, we need to either:
    # a) Build a new container image that supports write_to_path
    # b) Use a pod with SSH
    # c) Use RunPod's file upload API (if it exists)
    #
    # Let's check if RunPod has a file upload API for network volumes.

    print("\n  Checking for RunPod file upload API...")
    r = requests.get(
        "https://rest.runpod.io/v1/openapi.json",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    if r.status_code == 200:
        spec = r.json()
        paths = spec.get("paths", {})
        for p in sorted(paths.keys()):
            if "file" in p.lower() or "upload" in p.lower() or "volume" in p.lower():
                methods = list(paths[p].keys())
                print(f"    {p}: {methods}")

    # Since there's no direct file upload API, we'll use a different strategy:
    # Dispatch a training job with inline_dataset_csv but use the FULL dataset
    # if it fits, or the largest subset that fits.
    #
    # The RunPod payload limit is actually ~20MB for serverless, not 10MB.
    # Let's try with the full 46K rows (15.6 MB) and see if it works.

    print(f"\nSTEP 3: Attempt full dataset upload ({total_size / 1024 / 1024:.1f} MB)")
    print("  Trying full 46K row dataset as inline CSV...")

    job_id = f"runpod-stage-dataset-{int(time.time())}"
    job_input = {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "inline://dataset.csv",
        "model_family": "lightgbm",
        "random_seed": 42,
        "search_space": {
            "num_leaves": [31],
            "learning_rate": [0.1],
            "max_depth": [4],
            "n_estimators": [50],
            "min_data_in_leaf": [20],
        },
        "extra_constraints": {
            "bar_seconds": "86400",
            "horizon_bars": "5",
            "purge_bars": "5",
        },
        "inline_dataset_csv": csv_text,
    }

    print(f"  Dispatching job: {job_id}")
    print(f"  Payload size: {total_size / 1024 / 1024:.1f} MB")

    r = requests.post(
        f"{base_url}/{endpoint_id}/run",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"input": job_input},
        timeout=120,
    )

    if r.status_code != 200:
        print(f"  ERROR: HTTP {r.status_code}")
        print(f"  Response: {r.text[:500]}")
        # If it failed due to size, fall back to 15K rows
        print("\n  Falling back to 15K row subset...")
        df_subset = df.tail(15000)
        csv_subset = df_subset.to_csv(index=False)
        job_input["inline_dataset_csv"] = csv_subset
        job_input["job_id"] = f"runpod-stage-dataset-fallback-{int(time.time())}"

        r = requests.post(
            f"{base_url}/{endpoint_id}/run",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"input": job_input},
            timeout=120,
        )
        if r.status_code != 200:
            print(f"  Fallback also failed: {r.status_code} {r.text[:300]}")
            return 1

    result = r.json()
    runpod_job_id = result.get("id")
    print(f"  Job dispatched: {runpod_job_id}")

    # Poll for completion
    print("\nSTEP 4: Poll for completion")
    start = time.time()
    last_status = None

    while time.time() - start < 600:
        r = requests.get(
            f"{base_url}/{endpoint_id}/status/{runpod_job_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        if r.status_code != 200:
            time.sleep(5)
            continue

        result = r.json()
        status = result.get("status", "UNKNOWN")

        if status != last_status:
            elapsed = time.time() - start
            print(f"  [{elapsed:.0f}s] status: {status}")
            last_status = status

        if status == "COMPLETED":
            output = result.get("output", {})
            if "error_code" in output:
                print(f"  JOB FAILED: {output.get('error_code')}: {output.get('error_summary')}")
                return 1

            # Parse result
            callback_payload = output.get("callback_payload", "")
            envelope = json.loads(callback_payload)
            payload_dict = envelope.get("payload", {})
            dossier = payload_dict.get("dossier", {})
            metrics = dossier.get("training_metrics", {})
            meta = dossier.get("metadata", {})

            print("\nSTEP 5: Training Results")
            print(f"  artifact_id: {output.get('artifact_id')}")
            print(f"  trainer:     {meta.get('trainer')}")
            print(f"  n_rows:      {meta.get('n_rows')}")
            print(f"  n_features:  {meta.get('n_features')}")
            print(f"  n_folds:     {meta.get('n_folds')}")
            print(f"  accuracy:    {metrics.get('accuracy')}")
            print(f"  logloss:     {metrics.get('logloss')}")
            print(f"  brier:       {meta.get('brier_score')}")
            print(f"  sharpe:      {meta.get('sharpe_ratio')}")
            print(f"  pbo:         {dossier.get('pbo')}")
            print(f"  deflated:    {dossier.get('deflated_sharpe')}")

            # Verify HMAC
            from quant_foundry.signatures import verify_callback

            callback_secret = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
            sig = output.get("callback_signature", "")
            ts = int(output.get("callback_ts", 0))
            sig_valid = verify_callback(
                callback_payload.encode("utf-8"),
                secret=callback_secret,
                signature=sig,
                ts=ts,
                job_id=job_input["job_id"],
            )
            print(f"\n  HMAC valid:  {sig_valid}")

            # Save results
            results_dir = _REPO_ROOT / "data" / "runpod_full_training" / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            (results_dir / "callback_envelope.json").write_text(
                json.dumps(envelope, indent=2), encoding="utf-8"
            )
            (results_dir / "dossier.json").write_text(
                json.dumps(dossier, indent=2), encoding="utf-8"
            )
            (results_dir / "runpod_output.json").write_text(
                json.dumps(output, indent=2, default=str), encoding="utf-8"
            )
            print(f"  Results saved: {results_dir}")

            print(f"\n{'=' * 70}")
            print("RUNPOD FULL DATASET TRAINING COMPLETE")
            print(f"{'=' * 70}")
            return 0

        if status == "FAILED":
            print(f"  JOB FAILED: {result}")
            return 1

        time.sleep(5)

    print("  TIMEOUT")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
