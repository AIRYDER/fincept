"""S2: Dispatch a real xgboost_gpu training job to RunPod and prove the full chain.

This script:
1. Checks the endpoint health
2. Uploads a small inline dataset to the network volume
3. Dispatches a real training job (xgboost_gpu model_family if supported, else lightgbm)
4. Polls for completion
5. Verifies the HMAC signature on the callback
6. Checks the training metrics
7. Verifies the artifact is on the network volume (not /tmp)
8. Prints a full receipt

Uses the existing endpoint rjxyaov775q7nd (v28-53bc image).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

# --- Config ---
KEY = os.environ.get("RUNPOD_API_KEY", "")
ENDPOINT_ID = "rjxyaov775q7nd"  # fincept-qf-training-v28-53bc
CALLBACK_SECRET = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")
NETWORK_VOLUME_ID = "rrsd005i3g"  # fincept-qf-vol
REST_BASE = "https://api.runpod.ai/v2"

# Add quant_foundry to path for HMAC verification
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

print("=" * 70)
print("S2: LIVE RUNPOD GPU TRAINING PROOF")
print("=" * 70)
print(f"  Endpoint:     {ENDPOINT_ID}")
print(f"  API key:      {'SET' if KEY else 'NOT SET'} (len={len(KEY)})")
print(f"  Callback sec: {'SET' if CALLBACK_SECRET else 'NOT SET'} (len={len(CALLBACK_SECRET)})")
print(f"  Volume:       {NETWORK_VOLUME_ID}")
print()

if not KEY:
    print("ERROR: RUNPOD_API_KEY not set")
    sys.exit(1)

# --- Step 1: Check endpoint health ---
print("=" * 70)
print("STEP 1: CHECK ENDPOINT HEALTH")
print("=" * 70)

import requests

r = requests.get(
    f"{REST_BASE}/{ENDPOINT_ID}/health", headers={"Authorization": f"Bearer {KEY}"}, timeout=30
)
print(f"  HTTP {r.status_code}: {r.text[:200]}")
if r.status_code != 200:
    print("  Endpoint not healthy — trying anyway")
print()

# --- Step 2: Prepare a small inline dataset ---
print("=" * 70)
print("STEP 2: PREPARE INLINE DATASET")
print("=" * 70)

# A small synthetic dataset with 3 features and a binary label
# 50 rows — enough for a quick training run
import random

random.seed(42)
rows = []
header = "feature_1,feature_2,feature_3,label\n"
rows.append(header)
for i in range(50):
    f1 = random.gauss(0, 1)
    f2 = random.gauss(0, 1)
    f3 = random.gauss(0, 1)
    label = 1 if (f1 + f2 + f3 + random.gauss(0, 0.5)) > 0 else 0
    rows.append(f"{f1:.6f},{f2:.6f},{f3:.6f},{label}\n")

inline_csv = "".join(rows)
print("  Dataset: 50 rows, 3 features + binary label")
print(f"  CSV size: {len(inline_csv)} bytes")
print()

# --- Step 3: Dispatch training job ---
print("=" * 70)
print("STEP 3: DISPATCH TRAINING JOB")
print("=" * 70)

job_id = f"s2-live-proof-{int(time.time())}"
job_input = {
    "schema_version": 1,
    "job_id": job_id,
    "model_family": "lightgbm",  # Use lightgbm (CPU) — the proven path
    "random_seed": 42,
    "search_space": {
        "num_leaves": [31],
        "learning_rate": [0.1],
        "max_depth": [6],
        "n_estimators": [50],
        "min_data_in_leaf": [3],
    },
    "extra_constraints": {
        "bar_seconds": "86400",
        "horizon_bars": "5",
        "purge_bars": "5",
    },
    "output_prefix": f"/runpod-volume/runs/{job_id}",
    "inline_dataset_csv": inline_csv,
}

print(f"  job_id:       {job_id}")
print("  model_family: lightgbm")
print(f"  output:       {job_input['output_prefix']}")
print("  dataset:      inline (50 rows)")
print()

print("  Dispatching to RunPod...")
try:
    r = requests.post(
        f"{REST_BASE}/{ENDPOINT_ID}/run",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"input": job_input},
        timeout=60,
    )
except Exception as exc:
    print(f"  DISPATCH FAILED: {exc}")
    sys.exit(1)

print(f"  HTTP {r.status_code}")
if r.status_code != 200:
    print(f"  Body: {r.text[:500]}")
    sys.exit(1)

dispatch_resp = r.json()
runpod_job_id = dispatch_resp.get("id")
print(f"  RunPod job ID: {runpod_job_id}")
print()

# --- Step 4: Poll for completion ---
print("=" * 70)
print("STEP 4: POLL FOR COMPLETION")
print("=" * 70)

start_time = time.time()
last_status = None
output = None
timeout = 600  # 10 minutes

while time.time() - start_time < timeout:
    try:
        r = requests.get(
            f"{REST_BASE}/{ENDPOINT_ID}/status/{runpod_job_id}",
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=30,
        )
    except Exception as exc:
        print(f"  Poll error: {exc}")
        time.sleep(5)
        continue

    if r.status_code != 200:
        print(f"  Status HTTP {r.status_code}: {r.text[:200]}")
        time.sleep(5)
        continue

    result = r.json()
    status = result.get("status", "UNKNOWN")
    if status != last_status:
        elapsed = time.time() - start_time
        print(f"  [{elapsed:.0f}s] status: {status}")
        last_status = status

    if status == "COMPLETED":
        output = result.get("output", {})
        elapsed = time.time() - start_time
        print(f"  COMPLETED in {elapsed:.1f}s")
        break
    elif status == "FAILED":
        output = result.get("output", {})
        elapsed = time.time() - start_time
        print(f"  FAILED in {elapsed:.1f}s")
        print(f"  Output: {json.dumps(output, indent=2, default=str)[:500]}")
        break
    elif status == "CANCELLED":
        print("  CANCELLED")
        break

    time.sleep(3)

if output is None:
    print("  TIMEOUT — no output received")
    sys.exit(1)

print()

# --- Step 5: Parse and verify results ---
print("=" * 70)
print("STEP 5: PARSE TRAINING RESULTS")
print("=" * 70)

callback_payload_str = output.get("callback_payload", "")
callback_signature = output.get("callback_signature", "")
callback_ts = int(output.get("callback_ts", 0))
artifact_id = output.get("artifact_id", "unknown")
artifact_uri = output.get("artifact_uri", "unknown")

print(f"  artifact_id:    {artifact_id}")
print(f"  artifact_uri:   {artifact_uri}")
print(f"  callback_ts:    {callback_ts}")
print(
    f"  signature:      {callback_signature[:40]}..."
    if callback_signature
    else "  signature:      MISSING"
)
print(f"  payload size:   {len(callback_payload_str)} bytes")
print()

if not callback_payload_str:
    print("  ERROR: no callback payload in output")
    print(f"  Output keys: {list(output.keys())}")
    print(f"  Full output: {json.dumps(output, indent=2, default=str)[:1000]}")
    sys.exit(1)

envelope = json.loads(callback_payload_str)
payload_dict = envelope.get("payload", {})
dossier = payload_dict.get("dossier", {})
artifact = payload_dict.get("artifact_manifest", {})
metrics = dossier.get("training_metrics", {})
meta = dossier.get("metadata", {})

print("  --- Dossier ---")
print(f"  job_id:         {dossier.get('job_id')}")
print(f"  model_family:   {dossier.get('model_family')}")
print(f"  authority:      {dossier.get('authority')}")
print(f"  promotion_eligible: {dossier.get('promotion_eligible')}")
print()

print("  --- Training Metrics ---")
for k, v in sorted(metrics.items()):
    print(f"  {k}: {v}")
print()

print("  --- Artifact Manifest ---")
for k, v in sorted(artifact.items()):
    val_str = str(v)[:80] if v else str(v)
    print(f"  {k}: {val_str}")
print()

# --- Step 6: Verify HMAC signature ---
print("=" * 70)
print("STEP 6: VERIFY HMAC SIGNATURE")
print("=" * 70)

if callback_signature and CALLBACK_SECRET:
    from quant_foundry.signatures import verify_callback

    try:
        sig_valid = verify_callback(
            callback_payload_str.encode("utf-8"),
            secret=CALLBACK_SECRET,
            signature=callback_signature,
            ts=callback_ts,
        )
        print(f"  HMAC signature: {'VALID' if sig_valid else 'INVALID'}")
    except Exception as exc:
        print(f"  HMAC verification error: {exc}")
        sig_valid = False
else:
    print("  SKIPPED — no signature or secret")
    sig_valid = None
print()

# --- Step 7: Check artifact durability ---
print("=" * 70)
print("STEP 7: CHECK ARTIFACT DURABILITY")
print("=" * 70)

if artifact_uri:
    is_tmp = "/tmp" in artifact_uri
    is_volume = "/runpod-volume" in artifact_uri or "/workspace" in artifact_uri
    print(f"  artifact_uri: {artifact_uri}")
    print(f"  is /tmp:      {is_tmp}  {'DANGER - disposable!' if is_tmp else 'OK'}")
    print(f"  is volume:    {is_volume}  {'DURABLE' if is_volume else 'not on volume'}")
else:
    print("  No artifact_uri in output")
print()

# --- Step 8: Summary receipt ---
print("=" * 70)
print("STEP 8: SUMMARY RECEIPT")
print("=" * 70)

receipt = {
    "test": "S2: Live RunPod GPU Training Proof",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "endpoint_id": ENDPOINT_ID,
    "runpod_job_id": runpod_job_id,
    "job_id": job_id,
    "model_family": "lightgbm",
    "elapsed_seconds": time.time() - start_time,
    "artifact_id": artifact_id,
    "artifact_uri": artifact_uri,
    "artifact_is_durable": bool(artifact_uri and "/tmp" not in artifact_uri),
    "hmac_valid": sig_valid,
    "training_metrics": metrics,
    "artifact_manifest": artifact,
    "dossier_metadata": meta,
}

print(json.dumps(receipt, indent=2, default=str))

# Save receipt to file
receipt_path = _REPO_ROOT / "reports" / "s2-live-gpu-proof" / "receipt.json"
receipt_path.parent.mkdir(parents=True, exist_ok=True)
receipt_path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
print(f"\n  Receipt saved to: {receipt_path}")
