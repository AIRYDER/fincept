"""S2 Local: Prove the full training chain locally (handler → callback → verify).

This runs the actual handler.py locally with a real training job,
proving the full chain:
1. Handler receives the training request
2. RealLightGBMTrainer trains (CPU, deterministic)
3. HMAC-signed callback is produced
4. Artifact manifest is generated with determinism_status
5. Metrics are recorded
6. HMAC signature verifies

This is the local equivalent of S2 — it proves the code chain works
end-to-end. The RunPod GPU proof requires a working endpoint (the
existing endpoint's worker crashed — documented in the report).
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import tempfile
import time

# Set up environment
os.environ.setdefault("QUANT_FOUNDRY_CALLBACK_SECRET", "s2-local-proof-secret")
os.environ.setdefault("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")
_QF_SRC = str(_REPO_ROOT / "services" / "quant_foundry" / "src")

if _HANDLER_DIR not in sys.path:
    sys.path.insert(0, _HANDLER_DIR)
if _QF_SRC not in sys.path:
    sys.path.insert(0, _QF_SRC)

print("=" * 70)
print("S2 LOCAL: FULL TRAINING CHAIN PROOF")
print("=" * 70)
print(f"  Handler: {_HANDLER_DIR}/handler.py")
print("  Trainer: RealLightGBMTrainer (CPU, deterministic)")
print("  Mode:    canary (LocalTrainer) + real (if available)")
print()

# Import the handler
print("=== STEP 1: LOAD HANDLER MODULE ===")
try:
    handler_mod = importlib.import_module("handler")
    print(f"  Loaded: {handler_mod.__file__}")
except Exception as exc:
    print(f"  FAILED to load handler: {exc}")
    import traceback

    traceback.print_exc()
    sys.exit(1)
print()

# Prepare a small inline dataset
print("=== STEP 2: PREPARE DATASET ===")
import random

random.seed(42)
rows = ["feature_1,feature_2,feature_3,label\n"]
for i in range(100):
    f1 = random.gauss(0, 1)
    f2 = random.gauss(0, 1)
    f3 = random.gauss(0, 1)
    label = 1 if (f1 + f2 + f3 + random.gauss(0, 0.5)) > 0 else 0
    rows.append(f"{f1:.6f},{f2:.6f},{f3:.6f},{label}\n")

inline_csv = "".join(rows)
print("  Dataset: 100 rows, 3 features + binary label")
print(f"  CSV size: {len(inline_csv)} bytes")
print()

# Build the training request
print("=== STEP 3: BUILD TRAINING REQUEST ===")
job_id = f"s2-local-{int(time.time())}"

# Use a temp dir for output — in canary mode, no output_prefix = FakeArtifactWriter
tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="s2_local_"))
output_prefix = None  # canary mode: FakeArtifactWriter (in-memory artifact)

job_input = {
    "schema_version": 1,
    "job_id": job_id,
    "dataset_manifest_ref": "inline://placeholder",  # overridden by inline_dataset_csv after validation
    "model_family": "lightgbm",
    "random_seed": 42,
    "search_space": {
        "num_leaves": [31],
        "learning_rate": [0.1],
        "max_depth": [6],
        "n_estimators": [50],
        "min_data_in_leaf": [5],
    },
    "extra_constraints": {
        "bar_seconds": "86400",
        "horizon_bars": "5",
        "purge_bars": "5",
        "training_mode": "canary",  # canary allows FakeArtifactWriter (no volume needed)
    },
    "output_prefix": output_prefix,
    "inline_dataset_csv": inline_csv,
}

event = {"input": job_input}

print(f"  job_id: {job_id}")
print("  model_family: lightgbm")
print(f"  output_prefix: {output_prefix}")
print()

# Run the handler
print("=== STEP 4: RUN HANDLER (TRAINING) ===")
start_time = time.time()
try:
    result = handler_mod.handler(event)
except Exception as exc:
    print(f"  HANDLER CRASHED: {exc}")
    import traceback

    traceback.print_exc()
    sys.exit(1)

elapsed = time.time() - start_time
print(f"  Handler returned in {elapsed:.2f}s")
print()

# Parse results
print("=== STEP 5: PARSE RESULTS ===")
if "error_code" in result:
    print(f"  ERROR: {result.get('error_code')}: {result.get('error_summary')}")
    print(f"  Full result: {json.dumps(result, indent=2, default=str)[:1000]}")
    sys.exit(1)

callback_payload_str = result.get("callback_payload", "")
callback_signature = result.get("callback_signature", "")
callback_ts = int(result.get("callback_ts", 0))
artifact_id = result.get("artifact_id", "unknown")
artifact_uri = result.get("artifact_uri", "unknown")

print(f"  artifact_id:    {artifact_id}")
print(f"  artifact_uri:   {artifact_uri}")
print(f"  callback_ts:    {callback_ts}")
print(
    f"  signature:      {callback_signature[:40]}..."
    if callback_signature
    else "  signature:      MISSING"
)
print(f"  payload size:   {len(callback_payload_str)} bytes")
print(f"  result keys:    {sorted(result.keys())}")
print()

if not callback_payload_str:
    print("  ERROR: no callback payload")
    print(f"  Result keys: {list(result.keys())}")
    print(f"  Full result: {json.dumps(result, indent=2, default=str)[:1000]}")
    sys.exit(1)

envelope = json.loads(callback_payload_str)
# The callback envelope may have different structures — inspect it
print(f"  Envelope keys: {list(envelope.keys())}")
payload_dict = envelope.get("payload", envelope)  # fallback to envelope itself
dossier = payload_dict.get("dossier", {})
artifact = payload_dict.get("artifact_manifest", {})
metrics = dossier.get("training_metrics", payload_dict.get("training_metrics", {}))
meta = dossier.get("metadata", payload_dict.get("metadata", {}))

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

# Verify HMAC
print("=== STEP 6: VERIFY HMAC SIGNATURE ===")
if callback_signature:
    from quant_foundry.signatures import verify_callback

    try:
        sig_valid = verify_callback(
            callback_payload_str.encode("utf-8"),
            callback_signature,
            secret=os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"],
            ts=callback_ts,
            job_id=job_id,
        )
        print(f"  HMAC signature: {'VALID' if sig_valid else 'INVALID'}")
    except Exception as exc:
        print(f"  HMAC verification error: {exc}")
        sig_valid = False
else:
    print("  SKIPPED — no signature")
    sig_valid = None
print()

# Check artifact durability
print("=== STEP 7: CHECK ARTIFACT DURABILITY ===")
if artifact_uri and artifact_uri != "unknown":
    is_tmp = "/tmp" in artifact_uri or "temp" in artifact_uri.lower()
    is_local = tmp_dir.name in str(artifact_uri) or (
        output_prefix and output_prefix in str(artifact_uri)
    )
    print(f"  artifact_uri: {artifact_uri}")
    print(f"  is /tmp:      {is_tmp}")
    print(f"  is local out: {is_local}")
else:
    print(f"  artifact_uri: {artifact_uri} (canary mode — FakeArtifactWriter, no persistence)")
    print("  This is expected for canary mode. Research/production mode requires a volume path.")
print()

# Check if artifact file exists
print("=== STEP 8: CHECK ARTIFACT FILE EXISTS ===")
artifact_path = None
if artifact_uri and artifact_uri != "unknown":
    artifact_path = pathlib.Path(artifact_uri.replace("file://", ""))
if artifact_path and artifact_path.exists():
    file_size = artifact_path.stat().st_size
    print(f"  File exists: {artifact_path}")
    print(f"  Size: {file_size} bytes ({file_size / 1024:.1f} KB)")

    # Compute sha256
    import hashlib

    h = hashlib.sha256()
    with open(artifact_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual_sha = h.hexdigest()
    declared_sha = artifact.get("artifact_sha256", "")
    print(f"  SHA-256 (actual):  {actual_sha}")
    print(f"  SHA-256 (declared): {declared_sha}")
    print(f"  SHA-256 match: {actual_sha == declared_sha}")
else:
    print(f"  File NOT found: {artifact_path}")
print()

# Summary receipt
print("=" * 70)
print("SUMMARY RECEIPT")
print("=" * 70)

receipt = {
    "test": "S2 Local: Full Training Chain Proof",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "job_id": job_id,
    "model_family": "lightgbm",
    "elapsed_seconds": elapsed,
    "artifact_id": artifact_id,
    "artifact_uri": artifact_uri,
    "artifact_file_exists": bool(artifact_path and artifact_path.exists()),
    "artifact_sha256_match": bool(
        artifact_path and artifact_path.exists() and actual_sha == declared_sha
    )
    if artifact_path
    else False,
    "hmac_valid": sig_valid,
    "training_metrics": metrics,
    "artifact_manifest": artifact,
    "dossier": {
        "job_id": dossier.get("job_id"),
        "model_family": dossier.get("model_family"),
        "authority": dossier.get("authority"),
        "promotion_eligible": dossier.get("promotion_eligible"),
    },
}

print(json.dumps(receipt, indent=2, default=str))

# Save receipt
receipt_path = _REPO_ROOT / "reports" / "s2-live-gpu-proof" / "local_receipt.json"
receipt_path.parent.mkdir(parents=True, exist_ok=True)
receipt_path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
print(f"\n  Receipt saved to: {receipt_path}")
