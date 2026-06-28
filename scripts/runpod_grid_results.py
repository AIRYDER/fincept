"""Fetch results from completed grid search jobs and rank them."""
import json
import os
import pathlib
import sys
import requests

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

KEY = os.environ["RUNPOD_API_KEY"]
EP = os.environ["RUNPOD_TRAINING_ENDPOINT_ID"]
BASE_URL = os.environ.get("RUNPOD_BASE_URL", "https://api.runpod.ai/v2")
CALLBACK_SECRET = os.environ.get("QUANT_FOUNDRY_CALLBACK_SECRET", "")

# The 12 job IDs from the grid search
JOB_IDS = [
    ("conservative", 42, "e6aeed4a-280a-4f46-8f54-49b5c5f08a2a-u1"),
    ("conservative", 137, "33a40e49-2b8f-4065-b005-2b48b3852c62-u2"),
    ("conservative", 2024, "4ac20744-8d8e-40bc-a311-1b5d4605a336-u1"),
    ("balanced", 42, "73b19fae-d96f-4e16-9820-72ed6b203a1e-u2"),
    ("balanced", 137, "fa0c3860-97dd-425d-8981-254229b81761-u1"),
    ("balanced", 2024, "9a67bd33-67f2-480c-b00a-bcd9813183dd-u2"),
    ("aggressive", 42, "778e0fae-f5c9-4b7a-a56e-23b81db591a3-u1"),
    ("aggressive", 137, "1c10cc59-9b24-4b3d-b8d6-331068293788-u2"),
    ("aggressive", 2024, "802bffdb-ab18-4de0-9467-500cea7ceb65-u2"),
    ("deep_reg", 42, "a39f8d88-3b9e-4fe6-a933-99fafeb158bc-u2"),
    ("deep_reg", 137, "e8fe3e2c-3e29-4a81-a482-ad4bf25f8a95-u2"),
    ("deep_reg", 2024, "29659dc7-5599-4813-8aff-26011721e844-u2"),
]

results = []
for config, seed, runpod_id in JOB_IDS:
    r = requests.get(
        f"{BASE_URL}/{EP}/status/{runpod_id}",
        headers={"Authorization": f"Bearer {KEY}"},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  ERROR fetching {runpod_id}: {r.status_code}")
        continue
    result = r.json()
    output = result.get("output", {})
    callback_str = output.get("callback_payload", "")
    if not callback_str:
        print(f"  {config}/s{seed}: no callback payload")
        continue
    envelope = json.loads(callback_str)
    payload = envelope.get("payload", {})
    dossier = payload.get("dossier", {})
    artifact = payload.get("artifact_manifest", {})
    metrics = dossier.get("training_metrics", {})
    meta = dossier.get("metadata", {})
    results.append({
        "config": config,
        "seed": seed,
        "runpod_id": runpod_id,
        "output": output,
        "envelope": envelope,
        "dossier": dossier,
        "artifact": artifact,
        "metrics": metrics,
        "meta": meta,
    })
    print(f"  {config}/s{seed}: acc={metrics.get('accuracy', 0):.4f} sharpe={meta.get('sharpe_ratio', 'n/a')}")

# Rank by Sharpe
results.sort(key=lambda r: float(r["meta"].get("sharpe_ratio", 0)), reverse=True)

print(f"\n{'='*90}")
print("RANKED RESULTS (sorted by Sharpe ratio)")
print(f"{'='*90}")
print(f"  {'Rank':<5} {'Config':<15} {'Seed':<6} {'Acc':>8} {'Sharpe':>8} {'PBO':>6} {'DSR':>8} {'Brier':>8} {'BestIter':>8}")
print(f"  {'-'*5} {'-'*15} {'-'*6} {'-'*8} {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*8}")

for i, r in enumerate(results):
    meta = r["meta"]
    metrics = r["metrics"]
    dossier = r["dossier"]
    acc = float(metrics.get("accuracy", 0))
    sharpe = float(meta.get("sharpe_ratio", 0))
    pbo = float(dossier.get("pbo", 1))
    dsr = float(dossier.get("deflated_sharpe", 0))
    brier = float(meta.get("brier_score", 0))
    best_iter = meta.get("avg_best_iteration", "n/a")
    print(f"  {i+1:<5} {r['config']:<15} {r['seed']:<6} {acc:>8.4f} {sharpe:>8.4f} {pbo:>6.2f} {dsr:>8.4f} {brier:>8.4f} {str(best_iter):>8}")

# Best model details
best = results[0]
best_meta = best["meta"]
best_dossier = best["dossier"]
best_artifact = best["artifact"]
best_metrics = best["metrics"]

print(f"\n{'='*90}")
print("BEST MODEL")
print(f"{'='*90}")
print(f"  Config:          {best['config']} / seed {best['seed']}")
print(f"  Artifact ID:     {best_artifact.get('artifact_id', 'n/a')}")
print(f"  SHA256:          {best_artifact.get('sha256', 'n/a')[:32]}...")
print(f"  Size:            {best_artifact.get('size_bytes', 0):,} bytes")
print(f"  Accuracy:        {best_metrics.get('accuracy', 'n/a')}")
print(f"  Logloss:         {best_metrics.get('logloss', 'n/a')}")
print(f"  Brier Score:     {best_meta.get('brier_score', 'n/a')}")
print(f"  Win Rate:        {best_meta.get('win_rate', 'n/a')}")
print(f"  Sharpe Ratio:    {best_meta.get('sharpe_ratio', 'n/a')}")
print(f"  Max Drawdown:    {best_meta.get('max_drawdown', 'n/a')}")
print(f"  PBO:             {best_dossier.get('pbo', 'n/a')}")
print(f"  Deflated Sharpe: {best_dossier.get('deflated_sharpe', 'n/a')}")
print(f"  Avg Best Iter:   {best_meta.get('avg_best_iteration', 'n/a')}")
print(f"  Fold Best Iters: {best_meta.get('fold_best_iterations', 'n/a')}")
print(f"  N Features:      {best_meta.get('n_features', 'n/a')}")
print(f"  N Rows:          {best_meta.get('n_rows', 'n/a')}")
print(f"  N Folds:         {best_meta.get('n_folds', 'n/a')}")

# Verify HMAC
from quant_foundry.signatures import verify_callback

output = best["output"]
sig_valid = verify_callback(
    output.get("callback_payload", "").encode("utf-8"),
    secret=CALLBACK_SECRET,
    signature=output.get("callback_signature", ""),
    ts=int(output.get("callback_ts", 0)),
    job_id=best["output"].get("job_id", ""),
)
print(f"  HMAC Valid:      {sig_valid}")

# Save results
results_dir = _REPO_ROOT / "data" / "runpod_grid_search" / "results"
results_dir.mkdir(parents=True, exist_ok=True)

summary = []
for r in results:
    summary.append({
        "config": r["config"],
        "seed": r["seed"],
        "runpod_id": r["runpod_id"],
        "accuracy": float(r["metrics"].get("accuracy", 0)),
        "logloss": float(r["metrics"].get("logloss", 0)),
        "sharpe_ratio": float(r["meta"].get("sharpe_ratio", 0)),
        "brier_score": float(r["meta"].get("brier_score", 0)),
        "win_rate": float(r["meta"].get("win_rate", 0)),
        "max_drawdown": float(r["meta"].get("max_drawdown", 0)),
        "pbo": float(r["dossier"].get("pbo", 1)),
        "deflated_sharpe": float(r["dossier"].get("deflated_sharpe", 0)),
        "avg_best_iteration": r["meta"].get("avg_best_iteration", "n/a"),
        "fold_best_iterations": r["meta"].get("fold_best_iterations", "n/a"),
        "artifact_id": r["artifact"].get("artifact_id", "n/a"),
    })

(results_dir / "grid_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
(results_dir / "best_model.json").write_text(json.dumps({
    "config": best["config"],
    "seed": best["seed"],
    "dossier": best["dossier"],
    "artifact": best["artifact"],
    "metrics": best["metrics"],
    "meta": best["meta"],
    "hmac_valid": sig_valid,
}, indent=2), encoding="utf-8")
(results_dir / "all_results.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

print(f"\n  Results saved: {results_dir}")
print(f"\n{'='*90}")
print(f"GRID SEARCH COMPLETE — 12/12 jobs succeeded")
print(f"{'='*90}")
