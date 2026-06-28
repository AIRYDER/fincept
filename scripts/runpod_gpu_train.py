"""Dispatch a real GPU training job to RunPod and poll for the result.

This script:
1. Loads the deep real dataset (18 features, 20 symbols, 10 years)
2. Subsets to ~15K rows to fit RunPod's payload limit (~10MB)
3. Converts to inline CSV
4. Dispatches a real training job to the RunPod serverless endpoint
5. Polls RunPod for job completion
6. Parses the signed callback envelope
7. Verifies HMAC signature
8. Saves the trained model artifact + dossier
9. Prints full training results

Requirements:
- RUNPOD_API_KEY env var
- RUNPOD_TRAINING_ENDPOINT_ID env var
- QUANT_FOUNDRY_CALLBACK_SECRET env var (must match the RunPod container's secret)

Usage:
    uv run python scripts/runpod_gpu_train.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any

# Bootstrap paths
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))


def get_env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        print(f"ERROR: env var {name} is not set")
        sys.exit(1)
    return val


def load_dataset_csv(parquet_path: pathlib.Path, max_rows: int = 15000) -> str:
    """Load parquet dataset and convert to CSV for inline transport."""
    import pyarrow.parquet as pq

    table = pq.read_table(str(parquet_path))
    df = table.to_pandas()
    df = df.sort_values("decision_time")

    if len(df) > max_rows:
        df = df.tail(max_rows)

    csv = df.to_csv(index=False)
    print(f"  Dataset: {len(df)} rows, {len(df.columns)} columns")
    print(f"  CSV size: {len(csv):,} bytes ({len(csv) / 1024 / 1024:.1f} MB)")
    return csv


def dispatch_runpod_job(
    api_key: str,
    endpoint_id: str,
    base_url: str,
    job_input: dict[str, Any],
) -> str:
    """Dispatch a job to RunPod serverless. Returns the job ID."""
    import requests

    url = f"{base_url}/{endpoint_id}/run"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"input": job_input}

    print(f"  POST {url}")
    print(f"  Payload keys: {list(job_input.keys())}")
    if "inline_dataset_csv" in job_input:
        csv_size = len(job_input["inline_dataset_csv"])
        print(f"  inline_dataset_csv: {csv_size:,} bytes")

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        print(f"  ERROR: HTTP {r.status_code}")
        print(f"  Response: {r.text[:500]}")
        sys.exit(1)

    result = r.json()
    job_id = result.get("id")
    if not job_id:
        print(f"  ERROR: no job ID in response: {result}")
        sys.exit(1)

    print(f"  Job dispatched: {job_id}")
    return job_id


def poll_runpod_job(
    api_key: str,
    endpoint_id: str,
    base_url: str,
    job_id: str,
    timeout_seconds: int = 600,
    poll_interval: int = 5,
) -> dict[str, Any]:
    """Poll RunPod for job status until completion or timeout."""
    import requests

    url = f"{base_url}/{endpoint_id}/status/{job_id}"
    headers = {"Authorization": f"Bearer {api_key}"}

    start = time.time()
    last_status = None

    while time.time() - start < timeout_seconds:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"  Poll error: HTTP {r.status_code} {r.text[:200]}")
            time.sleep(poll_interval)
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
                sys.exit(1)
            print(f"  Job completed in {elapsed:.1f}s")
            return output

        if status == "FAILED":
            print(f"  JOB FAILED: {result}")
            sys.exit(1)

        time.sleep(poll_interval)

    print(f"  TIMEOUT after {timeout_seconds}s")
    sys.exit(1)


def verify_callback_signature(
    payload: bytes,
    signature: str,
    secret: str,
    ts: int,
    job_id: str,
) -> bool:
    """Verify the HMAC callback signature."""
    from quant_foundry.signatures import verify_callback

    return verify_callback(payload, secret=secret, signature=signature, ts=ts, job_id=job_id)


def main() -> int:
    # --- 1. Check env vars ---
    print("=" * 70)
    print("RUNPOD GPU TRAINING — REAL LIGHTGBM ON GPU")
    print("=" * 70)

    api_key = get_env("RUNPOD_API_KEY")
    endpoint_id = get_env("RUNPOD_TRAINING_ENDPOINT_ID")
    base_url = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")
    callback_secret = get_env("QUANT_FOUNDRY_CALLBACK_SECRET")

    print(f"  Endpoint:     {endpoint_id}")
    print(f"  Base URL:     {base_url}")
    print(f"  API key:      {api_key[:8]}...")
    print(f"  Callback:     {callback_secret[:8]}...")

    # --- 2. Load dataset ---
    print(f"\n{'=' * 70}")
    print("STEP 1: LOAD DATASET")
    print("=" * 70)

    # Find the deep real dataset
    dataset_dir = _REPO_ROOT / "data" / "datasets" / "deep_real"
    parquet_files = list(dataset_dir.glob("*.parquet"))

    if not parquet_files:
        # Fall back to yfinance_real
        dataset_dir = _REPO_ROOT / "data" / "datasets" / "yfinance_real"
        parquet_files = list(dataset_dir.glob("*.parquet"))

    if not parquet_files:
        print("ERROR: no dataset parquet found. Run scripts/train_deep_real_model.py first.")
        return 1

    parquet_path = parquet_files[0]
    print(f"  Dataset: {parquet_path.name}")
    inline_csv = load_dataset_csv(parquet_path, max_rows=15000)

    # --- 3. Build job input ---
    print(f"\n{'=' * 70}")
    print("STEP 2: BUILD TRAINING REQUEST")
    print("=" * 70)

    job_id = f"runpod-gpu-train-{int(time.time())}"

    job_input = {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "inline://dataset.csv",  # overridden by handler
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
        # Handler-level extension: embed the dataset as inline CSV
        "inline_dataset_csv": inline_csv,
    }

    print(f"  job_id:       {job_id}")
    print(f"  model_family: lightgbm")
    print(f"  search_space: {job_input['search_space']}")
    print(f"  extra:        {job_input['extra_constraints']}")

    # --- 4. Dispatch to RunPod ---
    print(f"\n{'=' * 70}")
    print("STEP 3: DISPATCH TO RUNPOD GPU ENDPOINT")
    print("=" * 70)

    runpod_job_id = dispatch_runpod_job(api_key, endpoint_id, base_url, job_input)

    # --- 5. Poll for completion ---
    print(f"\n{'=' * 70}")
    print("STEP 4: POLL RUNPOD FOR COMPLETION")
    print("=" * 70)

    output = poll_runpod_job(api_key, endpoint_id, base_url, runpod_job_id, timeout_seconds=600)

    # --- 6. Parse result ---
    print(f"\n{'=' * 70}")
    print("STEP 5: PARSE TRAINING RESULT")
    print("=" * 70)

    # The handler returns callback_payload, callback_signature, callback_ts
    callback_payload_str = output.get("callback_payload", "")
    callback_signature = output.get("callback_signature", "")
    callback_ts = int(output.get("callback_ts", 0))
    artifact_id = output.get("artifact_id", "unknown")
    dossier_id = output.get("dossier_id", "unknown")

    print(f"  artifact_id:  {artifact_id}")
    print(f"  dossier_id:   {dossier_id}")
    print(f"  callback_ts:  {callback_ts}")

    # Parse the callback envelope
    envelope = json.loads(callback_payload_str)
    payload_dict = envelope.get("payload", {})
    dossier_data = payload_dict.get("dossier", {})
    artifact_data = payload_dict.get("artifact_manifest", {})

    # --- 7. Verify HMAC signature ---
    print(f"\n{'=' * 70}")
    print("STEP 6: VERIFY HMAC CALLBACK SIGNATURE")
    print("=" * 70)

    payload_bytes = callback_payload_str.encode("utf-8")
    sig_valid = verify_callback_signature(
        payload_bytes, callback_signature, callback_secret, callback_ts, job_id
    )
    print(f"  Signature valid: {sig_valid}")

    if not sig_valid:
        print("  WARNING: HMAC signature verification failed!")
        print("  The callback secret may not match between local env and RunPod container.")

    # --- 8. Display results ---
    print(f"\n{'=' * 70}")
    print("STEP 7: GPU TRAINING RESULTS")
    print("=" * 70)

    metrics = dossier_data.get("training_metrics", {})
    meta = dossier_data.get("metadata", {})

    print(f"\n  Artifact:")
    print(f"    artifact_id:       {artifact_data.get('artifact_id', 'n/a')}")
    print(f"    sha256:            {artifact_data.get('sha256', 'n/a')[:16]}...")
    print(f"    size_bytes:        {artifact_data.get('size_bytes', 'n/a'):,}")
    print(f"    model_family:      {artifact_data.get('model_family', 'n/a')}")
    print(f"    code_git_sha:      {artifact_data.get('code_git_sha', 'n/a')}")
    print(f"    container_digest:  {artifact_data.get('container_image_digest', 'n/a')}")

    print(f"\n  Dossier:")
    print(f"    model_id:          {dossier_data.get('model_id', 'n/a')}")
    print(f"    authority:         {dossier_data.get('authority', 'n/a')}")
    print(f"    dataset_manifest:  {dossier_data.get('dataset_manifest_id', 'n/a')[:60]}...")

    print(f"\n  Walk-Forward Metrics (out-of-sample):")
    print(f"    accuracy:          {metrics.get('accuracy', 'n/a')}")
    print(f"    logloss:           {metrics.get('logloss', 'n/a')}")
    print(f"    brier_score:       {meta.get('brier_score', 'n/a')}")
    print(f"    win_rate:          {meta.get('win_rate', 'n/a')}")
    print(f"    sharpe_ratio:      {meta.get('sharpe_ratio', 'n/a')}")
    print(f"    max_drawdown:      {meta.get('max_drawdown', 'n/a')}")
    print(f"    pbo:               {dossier_data.get('pbo', 'n/a')}")
    print(f"    deflated_sharpe:   {dossier_data.get('deflated_sharpe', 'n/a')}")

    # --- 9. Save results ---
    results_dir = _REPO_ROOT / "data" / "runpod_gpu_training" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    (results_dir / "callback_envelope.json").write_text(
        json.dumps(envelope, indent=2), encoding="utf-8"
    )
    (results_dir / "artifact_manifest.json").write_text(
        json.dumps(artifact_data, indent=2), encoding="utf-8"
    )
    (results_dir / "dossier.json").write_text(
        json.dumps(dossier_data, indent=2), encoding="utf-8"
    )
    (results_dir / "runpod_output.json").write_text(
        json.dumps(output, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n  Results saved to: {results_dir}")

    print(f"\n{'=' * 70}")
    print(f"RUNPOD GPU TRAINING COMPLETE")
    print(f"{'=' * 70}")
    print(f"  RunPod Job:    {runpod_job_id}")
    print(f"  Artifact:      {artifact_id}")
    print(f"  Dossier:       {dossier_id}")
    print(f"  HMAC valid:    {sig_valid}")
    print(f"  Authority:     {dossier_data.get('authority', 'n/a')}")
    acc = metrics.get("accuracy", 0)
    print(f"  Accuracy:      {acc:.4f}" if isinstance(acc, float) else f"  Accuracy:      {acc}")
    print(f"  PBO:           {dossier_data.get('pbo', 'n/a')}")
    print(f"  Deflated:      {dossier_data.get('deflated_sharpe', 'n/a')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
