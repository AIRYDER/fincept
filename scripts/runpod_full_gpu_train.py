"""Full RunPod GPU training pipeline with the updated container.

This script:
1. Uploads the full 46K row dataset to the network volume in chunks
   using the new write_volume handler task
2. Verifies the upload with stat_volume
3. Dispatches a real training job reading from /workspace/dataset_full.csv
4. Polls for completion
5. Verifies HMAC signature
6. Saves and displays results

Requirements:
- Updated RunPod container with write_volume/stat_volume tasks
- RUNPOD_API_KEY, RUNPOD_TRAINING_ENDPOINT_ID, QUANT_FOUNDRY_CALLBACK_SECRET
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import requests

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

KEY = os.environ["RUNPOD_API_KEY"]
EP = os.environ["RUNPOD_TRAINING_ENDPOINT_ID"]
BASE_URL = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")
CALLBACK_SECRET = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
# Serverless mounts the network volume at /runpod-volume/
# The handler's resolve_volume_path() will rewrite this to /workspace/ if needed
VOLUME_PATH = "/runpod-volume/datasets/deep_real/dataset_full.csv"
OUTPUT_PREFIX = "/runpod-volume/runs"
CHUNK_SIZE = 7 * 1024 * 1024  # 7 MB per chunk (under 10MB RunPod limit)


def dispatch_job(job_input: dict, timeout: int = 120) -> dict:
    """Dispatch a job to RunPod and return the output."""
    r = requests.post(
        f"{BASE_URL}/{EP}/run",
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
        },
        json={"input": job_input},
        timeout=timeout,
    )
    if r.status_code != 200:
        print(f"  ERROR: HTTP {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    job_id = r.json().get("id")
    return job_id


def poll_job(job_id: str, timeout: int = 600, quiet: bool = False) -> dict:
    """Poll for job completion."""
    start = time.time()
    last_status = None
    while time.time() - start < timeout:
        r = requests.get(
            f"{BASE_URL}/{EP}/status/{job_id}",
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=30,
        )
        if r.status_code != 200:
            time.sleep(3)
            continue
        result = r.json()
        status = result.get("status", "UNKNOWN")
        if status != last_status and not quiet:
            elapsed = time.time() - start
            print(f"  [{elapsed:.0f}s] status: {status}")
            last_status = status
        if status == "COMPLETED":
            output = result.get("output", {})
            if "error_code" in output:
                if not quiet:
                    print(f"  FAILED: {output.get('error_code')}: {output.get('error_summary')}")
                return output
            if not quiet:
                print(f"  Completed in {time.time() - start:.1f}s")
            return output
        if status == "FAILED":
            if not quiet:
                print(f"  FAILED: {result}")
            return result.get("output", {})
        time.sleep(3)
    if not quiet:
        print("  TIMEOUT")
    return {}


def main() -> int:
    print("=" * 70)
    print("RUNPOD FULL GPU TRAINING PIPELINE")
    print("=" * 70)
    print(f"  Endpoint: {EP}")
    print(f"  Dataset:  {VOLUME_PATH}")
    print(f"  Output:   {OUTPUT_PREFIX}")

    # 1. Verify dataset exists on the network volume
    print(f"\n{'=' * 70}")
    print("STEP 1: VERIFY DATASET ON NETWORK VOLUME")
    print("=" * 70)

    job_id = dispatch_job({"task": "stat_volume", "volume_path": VOLUME_PATH})
    output = poll_job(job_id, timeout=120)
    if output.get("exists"):
        print(f"  File exists: {output.get('volume_path')}")
        print(f"  Size: {output.get('file_size_mb')} MB ({output.get('file_size_bytes'):,} bytes)")
    else:
        print("  ERROR: dataset not found on volume!")
        print("  Run scripts/runpod_s3_upload.py first to upload the dataset.")
        return 1

    # 2. Dispatch training job
    print(f"\n{'=' * 70}")
    print("STEP 2: DISPATCH TRAINING JOB (full 46K rows from network volume)")
    print("=" * 70)

    train_job_id = f"runpod-full-gpu-{int(time.time())}"
    job_input = {
        "schema_version": 1,
        "job_id": train_job_id,
        "dataset_manifest_ref": VOLUME_PATH,
        "model_family": "lightgbm",
        "random_seed": 42,
        "search_space": {
            "num_leaves": [127],
            "learning_rate": [0.01],
            "max_depth": [8],
            "n_estimators": [500],
            "min_data_in_leaf": [5],
        },
        "extra_constraints": {
            "bar_seconds": "86400",
            "horizon_bars": "5",
            "purge_bars": "5",
        },
        # Handler-level extension: write results to the network volume
        "output_prefix": f"{OUTPUT_PREFIX}/{train_job_id}",
    }

    print(f"  job_id: {train_job_id}")
    print(f"  dataset: {VOLUME_PATH}")
    print(f"  output:  {job_input['output_prefix']}")
    print("  model: 500 trees, 127 leaves, depth 8, lr=0.01")

    runpod_job_id = dispatch_job(job_input)
    print(f"  RunPod job: {runpod_job_id}")

    # 3. Poll for completion
    print(f"\n{'=' * 70}")
    print("STEP 3: POLL FOR COMPLETION")
    print("=" * 70)

    output = poll_job(runpod_job_id, timeout=1800)  # 30 min deadline

    # 4. Parse results
    print(f"\n{'=' * 70}")
    print("STEP 4: TRAINING RESULTS")
    print("=" * 70)

    callback_payload_str = output.get("callback_payload", "")
    callback_signature = output.get("callback_signature", "")
    callback_ts = int(output.get("callback_ts", 0))
    artifact_id = output.get("artifact_id", "unknown")

    if not callback_payload_str:
        print("  ERROR: no callback payload in output")
        print(f"  Output keys: {list(output.keys())}")
        print(f"  Full output: {json.dumps(output, indent=2, default=str)[:500]}")
        return 1

    envelope = json.loads(callback_payload_str)
    payload_dict = envelope.get("payload", {})
    dossier = payload_dict.get("dossier", {})
    artifact = payload_dict.get("artifact_manifest", {})
    metrics = dossier.get("training_metrics", {})
    meta = dossier.get("metadata", {})

    # 5. Verify HMAC
    print(f"\n{'=' * 70}")
    print("STEP 5: VERIFY HMAC SIGNATURE")
    print("=" * 70)

    from quant_foundry.signatures import verify_callback

    sig_valid = verify_callback(
        callback_payload_str.encode("utf-8"),
        secret=CALLBACK_SECRET,
        signature=callback_signature,
        ts=callback_ts,
        job_id=train_job_id,
    )
    print(f"  Signature valid: {sig_valid}")

    # 6. Display results
    print(f"\n{'=' * 70}")
    print("STEP 6: FULL RESULTS")
    print("=" * 70)

    print("\n  Artifact:")
    print(f"    artifact_id:       {artifact.get('artifact_id', 'n/a')}")
    print(f"    sha256:            {artifact.get('sha256', 'n/a')[:16]}...")
    print(f"    size_bytes:        {artifact.get('size_bytes', 0):,}")
    print(f"    model_family:      {artifact.get('model_family', 'n/a')}")

    print("\n  Dossier:")
    print(f"    model_id:          {dossier.get('model_id', 'n/a')}")
    print(f"    authority:         {dossier.get('authority', 'n/a')}")
    print(f"    trainer:           {meta.get('trainer', 'n/a')}")
    print(f"    n_rows:            {meta.get('n_rows', 'n/a')}")
    print(f"    n_features:        {meta.get('n_features', 'n/a')}")
    print(f"    n_folds:           {meta.get('n_folds', 'n/a')}")

    print("\n  Walk-Forward Metrics (out-of-sample):")
    print(f"    accuracy:          {metrics.get('accuracy', 'n/a')}")
    print(f"    logloss:           {metrics.get('logloss', 'n/a')}")
    print(f"    brier_score:       {meta.get('brier_score', 'n/a')}")
    print(f"    win_rate:          {meta.get('win_rate', 'n/a')}")
    print(f"    sharpe_ratio:      {meta.get('sharpe_ratio', 'n/a')}")
    print(f"    max_drawdown:      {meta.get('max_drawdown', 'n/a')}")
    print(f"    pbo:               {dossier.get('pbo', 'n/a')}")
    print(f"    deflated_sharpe:   {dossier.get('deflated_sharpe', 'n/a')}")

    # 7. Save results locally
    results_dir = _REPO_ROOT / "data" / "runpod_full_gpu_training" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "callback_envelope.json").write_text(
        json.dumps(envelope, indent=2), encoding="utf-8"
    )
    (results_dir / "dossier.json").write_text(json.dumps(dossier, indent=2), encoding="utf-8")
    (results_dir / "artifact_manifest.json").write_text(
        json.dumps(artifact, indent=2), encoding="utf-8"
    )
    (results_dir / "runpod_output.json").write_text(
        json.dumps(output, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n  Results saved: {results_dir}")

    # 8. Also check if results were written to the volume
    if output.get("output_prefix"):
        print(f"\n  Results on volume: {output['output_prefix']}")
        # List the output directory
        list_job = dispatch_job({"task": "list_volume", "volume_path": output["output_prefix"]})
        list_output = poll_job(list_job, timeout=60, quiet=True)
        if list_output.get("exists"):
            for f in list_output.get("files", []):
                print(f"    {f['name']}: {f['size_bytes']:,} bytes")

    print(f"\n{'=' * 70}")
    print("FULL GPU TRAINING COMPLETE")
    print(f"{'=' * 70}")
    print(f"  RunPod Job:    {runpod_job_id}")
    print(f"  Artifact:      {artifact_id}")
    print(f"  HMAC valid:    {sig_valid}")
    print(f"  Authority:     {dossier.get('authority', 'n/a')}")
    print(f"  Accuracy:      {metrics.get('accuracy', 'n/a')}")
    print(f"  Sharpe:        {meta.get('sharpe_ratio', 'n/a')}")
    print(f"  PBO:           {dossier.get('pbo', 'n/a')}")
    print(f"  Deflated:      {dossier.get('deflated_sharpe', 'n/a')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
