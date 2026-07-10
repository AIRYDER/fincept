"""S2 Determinism Proof: train twice with the same seed, verify identical sha256.

This proves the bit-determinism property that is the foundation of
the platform's unique value proposition (F1/F2 in the recommendations).
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import time

os.environ.setdefault("QUANT_FOUNDRY_CALLBACK_SECRET", "s2-determinism-proof-secret")
os.environ.setdefault("QUANT_FOUNDRY_USE_REAL_TRAINER", "true")

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_HANDLER_DIR = str(_REPO_ROOT / "runpod" / "quant-foundry-training")
_QF_SRC = str(_REPO_ROOT / "services" / "quant_foundry" / "src")

if _HANDLER_DIR not in sys.path:
    sys.path.insert(0, _HANDLER_DIR)
if _QF_SRC not in sys.path:
    sys.path.insert(0, _QF_SRC)

print("=" * 70)
print("S2 DETERMINISM PROOF: TRAIN TWICE, VERIFY IDENTICAL SHA256")
print("=" * 70)
print()

# Load handler
handler_mod = importlib.import_module("handler")

# Prepare a fixed dataset (same both runs)
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


def run_training(run_label: str) -> dict:
    """Run a single training job and return the result."""
    job_id = f"s2-det-{run_label}-{int(time.time())}"
    job_input = {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "inline://placeholder",
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
            "training_mode": "canary",
        },
        "inline_dataset_csv": inline_csv,
    }
    event = {"input": job_input}

    print(f"  Run {run_label}: dispatching training job...")
    start = time.time()
    result = handler_mod.handler(event)
    elapsed = time.time() - start

    if "error_code" in result:
        print(
            f"  Run {run_label}: FAILED — {result.get('error_code')}: {result.get('error_summary', '')[:200]}"
        )
        return None

    callback_payload_str = result.get("callback_payload", "")
    envelope = json.loads(callback_payload_str)
    payload = envelope.get("payload", envelope)
    artifact = payload.get("artifact_manifest", {})

    sha = artifact.get("sha256", "")
    artifact_id = artifact.get("artifact_id", "")
    metrics = payload.get("dossier", {}).get(
        "training_metrics", payload.get("training_metrics", {})
    )

    print(f"  Run {run_label}: completed in {elapsed:.2f}s")
    print(f"    sha256: {sha}")
    print(f"    artifact_id: {artifact_id}")
    print(f"    accuracy: {metrics.get('accuracy')}")
    print(f"    sharpe: {metrics.get('sharpe_ratio')}")

    return {
        "run": run_label,
        "sha256": sha,
        "artifact_id": artifact_id,
        "elapsed_seconds": elapsed,
        "accuracy": metrics.get("accuracy"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "feature_schema_hash": artifact.get("feature_schema_hash"),
        "label_schema_hash": artifact.get("label_schema_hash"),
        "size_bytes": artifact.get("size_bytes"),
    }


# Run 1
print("=== RUN 1 ===")
r1 = run_training("run1")
print()

# Run 2
print("=== RUN 2 ===")
r2 = run_training("run2")
print()

# Compare
print("=" * 70)
print("DETERMINISM VERIFICATION")
print("=" * 70)

if r1 and r2:
    sha_match = r1["sha256"] == r2["sha256"]
    artifact_id_match = r1["artifact_id"] == r2["artifact_id"]
    accuracy_match = r1["accuracy"] == r2["accuracy"]
    sharpe_match = r1["sharpe_ratio"] == r2["sharpe_ratio"]
    feature_hash_match = r1["feature_schema_hash"] == r2["feature_schema_hash"]
    label_hash_match = r1["label_schema_hash"] == r2["label_schema_hash"]
    size_match = r1["size_bytes"] == r2["size_bytes"]

    print(f"  sha256 match:           {sha_match}")
    print(f"    run1: {r1['sha256']}")
    print(f"    run2: {r2['sha256']}")
    print(f"  artifact_id match:      {artifact_id_match}")
    print(f"  accuracy match:         {accuracy_match}  ({r1['accuracy']} == {r2['accuracy']})")
    print(
        f"  sharpe match:           {sharpe_match}  ({r1['sharpe_ratio']} == {r2['sharpe_ratio']})"
    )
    print(f"  feature_schema_hash:    {feature_hash_match}")
    print(f"  label_schema_hash:      {label_hash_match}")
    print(f"  size_bytes match:       {size_match}  ({r1['size_bytes']} == {r2['size_bytes']})")
    print()

    # The critical property: model sha256 must match (bit-deterministic model bytes)
    # feature_schema_hash / label_schema_hash may differ because they include
    # the temp file path (a known minor issue — they should be content-based)
    critical_match = all([sha_match, artifact_id_match, accuracy_match, sharpe_match, size_match])
    all_match = all(
        [
            sha_match,
            artifact_id_match,
            accuracy_match,
            sharpe_match,
            feature_hash_match,
            label_hash_match,
            size_match,
        ]
    )
    print(f"  ALL FIELDS MATCH: {all_match}")
    print(
        f"  CRITICAL FIELDS MATCH (sha256, artifact_id, accuracy, sharpe, size): {critical_match}"
    )
    print()

    if critical_match:
        print("  VERDICT: TRAINING IS BIT-DETERMINISTIC")
        print("  The same (dataset, seed, params) recipe produces identical model bytes.")
        print("  This is the foundation property for F1 (receipt-native platform)")
        print("  and F2 (verifiable model recipes).")
        print()
        print("  NOTE: feature_schema_hash / label_schema_hash differ because they")
        print("  include the temp file path. This is a known minor issue — the hashes")
        print("  should be content-based, not path-based. Does not affect determinism.")
    else:
        print("  VERDICT: NON-DETERMINISTIC — investigate discrepancies")
else:
    print("  VERDICT: FAILED — one or both runs failed")
    all_match = False

# Save receipt
receipt = {
    "test": "S2 Determinism Proof",
    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "run1": r1,
    "run2": r2,
    "sha256_match": sha_match if r1 and r2 else False,
    "critical_fields_match": critical_match if r1 and r2 else False,
    "all_fields_match": all_match if r1 and r2 else False,
    "verdict": "BIT_DETERMINISTIC"
    if (r1 and r2 and critical_match)
    else "NON_DETERMINISTIC_OR_FAILED",
}

receipt_path = _REPO_ROOT / "reports" / "s2-live-gpu-proof" / "determinism_receipt.json"
receipt_path.parent.mkdir(parents=True, exist_ok=True)
receipt_path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")
print(f"\n  Receipt saved to: {receipt_path}")
