"""Intense RunPod GPU grid search training.

Dispatches multiple training configurations in parallel across the RunPod
serverless endpoint, each with different hyperparameters and random seeds.
Collects all results, ranks by Sharpe ratio / PBO / accuracy, and saves
the best model.

Grid dimensions:
  - 4 hyperparameter configs (conservative, balanced, aggressive, deep)
  - 3 random seeds (42, 137, 2024)
  - 5 walk-forward folds (expanding window + purge gap)
  - Early stopping with 50-round patience
  - Up to 2000 trees per fold

Total: 12 jobs dispatched in parallel.
"""
from __future__ import annotations

import itertools
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
VOLUME_PATH = "/runpod-volume/datasets/deep_real/dataset_full.csv"
OUTPUT_PREFIX = "/runpod-volume/runs"

# ─── Grid definition ──────────────────────────────────────────────────────

HYPERPARAM_CONFIGS = {
    "conservative": {
        "num_leaves": [63],
        "learning_rate": [0.01],
        "max_depth": [6],
        "n_estimators": [1000],
        "min_data_in_leaf": [20],
        "feature_fraction": [0.8],
        "bagging_fraction": [0.8],
        "bagging_freq": [5],
        "lambda_l1": [0.1],
        "lambda_l2": [1.0],
        "min_split_gain": [0.01],
        "early_stopping_rounds": [50],
    },
    "balanced": {
        "num_leaves": [127],
        "learning_rate": [0.01],
        "max_depth": [8],
        "n_estimators": [1500],
        "min_data_in_leaf": [10],
        "feature_fraction": [0.7],
        "bagging_fraction": [0.7],
        "bagging_freq": [5],
        "lambda_l1": [0.0],
        "lambda_l2": [0.5],
        "min_split_gain": [0.0],
        "early_stopping_rounds": [50],
    },
    "aggressive": {
        "num_leaves": [255],
        "learning_rate": [0.005],
        "max_depth": [10],
        "n_estimators": [2000],
        "min_data_in_leaf": [5],
        "feature_fraction": [0.6],
        "bagging_fraction": [0.6],
        "bagging_freq": [3],
        "lambda_l1": [0.0],
        "lambda_l2": [0.1],
        "min_split_gain": [0.0],
        "early_stopping_rounds": [100],
    },
    "deep_reg": {
        "num_leaves": [31],
        "learning_rate": [0.02],
        "max_depth": [12],
        "n_estimators": [1500],
        "min_data_in_leaf": [50],
        "feature_fraction": [0.9],
        "bagging_fraction": [0.9],
        "bagging_freq": [7],
        "lambda_l1": [1.0],
        "lambda_l2": [5.0],
        "min_split_gain": [0.1],
        "path_smooth": [10.0],
        "early_stopping_rounds": [50],
    },
}

SEEDS = [42, 137, 2024]
N_FOLDS = 5


def dispatch_job(job_input: dict, timeout: int = 120) -> str:
    r = requests.post(
        f"{BASE_URL}/{EP}/run",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={"input": job_input},
        timeout=timeout,
    )
    if r.status_code != 200:
        print(f"  ERROR: HTTP {r.status_code}: {r.text[:300]}")
        return ""
    return r.json().get("id", "")


def poll_job(job_id: str, timeout: int = 1800) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(
            f"{BASE_URL}/{EP}/status/{job_id}",
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=30,
        )
        if r.status_code != 200:
            time.sleep(5)
            continue
        result = r.json()
        status = result.get("status", "UNKNOWN")
        if status == "COMPLETED":
            return result.get("output", {})
        if status == "FAILED":
            return {"error": result.get("error", "unknown"), "status": "FAILED"}
        time.sleep(5)
    return {"error": "timeout", "status": "TIMEOUT"}


def main() -> int:
    print("=" * 70)
    print("RUNPOD INTENSE GRID SEARCH TRAINING")
    print("=" * 70)
    print(f"  Endpoint: {EP}")
    print(f"  Dataset:  {VOLUME_PATH}")
    print(f"  Configs:  {list(HYPERPARAM_CONFIGS.keys())}")
    print(f"  Seeds:    {SEEDS}")
    print(f"  Folds:    {N_FOLDS}")
    print(f"  Total jobs: {len(HYPERPARAM_CONFIGS) * len(SEEDS)}")

    # 1. Verify dataset
    print(f"\n{'=' * 70}")
    print("STEP 1: VERIFY DATASET")
    print("=" * 70)
    stat_job = dispatch_job({"task": "stat_volume", "volume_path": VOLUME_PATH})
    stat_out = poll_job(stat_job, timeout=120)
    if stat_out.get("exists"):
        print(f"  OK: {stat_out.get('file_size_mb')} MB")
    else:
        print("  ERROR: dataset not found!")
        return 1

    # 2. Dispatch all jobs
    print(f"\n{'=' * 70}")
    print("STEP 2: DISPATCH GRID SEARCH JOBS")
    print("=" * 70)

    jobs = []
    for config_name, config_params in HYPERPARAM_CONFIGS.items():
        for seed in SEEDS:
            job_id = f"grid-{config_name}-s{seed}-{int(time.time())}"
            job_input = {
                "schema_version": 1,
                "job_id": job_id,
                "dataset_manifest_ref": VOLUME_PATH,
                "model_family": "lightgbm",
                "random_seed": seed,
                "search_space": config_params,
                "extra_constraints": {
                    "bar_seconds": "86400",
                    "horizon_bars": "5",
                    "purge_bars": "5",
                },
                "n_folds": N_FOLDS,
                "output_prefix": f"{OUTPUT_PREFIX}/{job_id}",
            }

            runpod_id = dispatch_job(job_input)
            if runpod_id:
                jobs.append({
                    "config": config_name,
                    "seed": seed,
                    "job_id": job_id,
                    "runpod_id": runpod_id,
                    "params": config_params,
                })
                print(f"  [{config_name}/s{seed}] -> {runpod_id}")
            else:
                print(f"  [{config_name}/s{seed}] FAILED TO DISPATCH")

    print(f"\n  Dispatched {len(jobs)} jobs")

    # 3. Poll all jobs in parallel
    print(f"\n{'=' * 70}")
    print("STEP 3: POLL ALL JOBS")
    print("=" * 70)

    results = []
    pending = list(jobs)
    start = time.time()

    while pending:
        time.sleep(10)
        still_pending = []
        for job in pending:
            r = requests.get(
                f"{BASE_URL}/{EP}/status/{job['runpod_id']}",
                headers={"Authorization": f"Bearer {KEY}"},
                timeout=30,
            )
            if r.status_code != 200:
                still_pending.append(job)
                continue
            result = r.json()
            status = result.get("status", "UNKNOWN")
            elapsed = time.time() - start

            if status == "COMPLETED":
                output = result.get("output", {})
                if "error_code" in output:
                    print(f"  [{elapsed:.0f}s] {job['config']}/s{job['seed']}: FAILED - {output.get('error_code')}")
                    results.append({**job, "output": output, "status": "FAILED"})
                else:
                    callback_str = output.get("callback_payload", "")
                    if callback_str:
                        envelope = json.loads(callback_str)
                        payload = envelope.get("payload", {})
                        dossier = payload.get("dossier", {})
                        metrics = dossier.get("training_metrics", {})
                        meta = dossier.get("metadata", {})
                        print(f"  [{elapsed:.0f}s] {job['config']}/s{job['seed']}: DONE "
                              f"acc={metrics.get('accuracy', 0):.4f} "
                              f"sharpe={meta.get('sharpe_ratio', 'n/a')} "
                              f"pbo={dossier.get('pbo', 'n/a')}")
                        results.append({
                            **job,
                            "output": output,
                            "envelope": envelope,
                            "dossier": dossier,
                            "artifact": payload.get("artifact_manifest", {}),
                            "metrics": metrics,
                            "meta": meta,
                            "status": "COMPLETED",
                        })
                    else:
                        print(f"  [{elapsed:.0f}s] {job['config']}/s{job['seed']}: NO PAYLOAD")
                        results.append({**job, "output": output, "status": "NO_PAYLOAD"})
            elif status == "FAILED":
                print(f"  [{elapsed:.0f}s] {job['config']}/s{job['seed']}: FAILED")
                results.append({**job, "output": result.get("output", {}), "status": "FAILED"})
            else:
                still_pending.append(job)

        pending = still_pending
        if pending:
            elapsed = time.time() - start
            statuses = []
            for j in pending:
                r2 = requests.get(
                    f"{BASE_URL}/{EP}/status/{j['runpod_id']}",
                    headers={"Authorization": f"Bearer {KEY}"},
                    timeout=15,
                )
                s = r2.json().get("status", "?") if r2.status_code == 200 else "?"
                statuses.append(f"{j['config']}/s{j['seed']}={s}")
            print(f"  [{elapsed:.0f}s] pending: {', '.join(statuses)}")

    # 4. Rank results
    print(f"\n{'=' * 70}")
    print("STEP 4: RANK RESULTS BY SHARPE RATIO")
    print("=" * 70)

    completed = [r for r in results if r.get("status") == "COMPLETED"]
    completed.sort(
        key=lambda r: float(r.get("meta", {}).get("sharpe_ratio", 0)),
        reverse=True,
    )

    print(f"\n  {'Rank':<5} {'Config':<15} {'Seed':<6} {'Acc':>8} {'Sharpe':>8} {'PBO':>6} {'DSR':>8} {'Brier':>8} {'BestIter':>8}")
    print(f"  {'-'*5} {'-'*15} {'-'*6} {'-'*8} {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*8}")

    for i, r in enumerate(completed):
        meta = r.get("meta", {})
        metrics = r.get("metrics", {})
        dossier = r.get("dossier", {})
        acc = float(metrics.get("accuracy", 0))
        sharpe = float(meta.get("sharpe_ratio", 0))
        pbo = float(dossier.get("pbo", 1))
        dsr = float(dossier.get("deflated_sharpe", 0))
        brier = float(meta.get("brier_score", 0))
        best_iter = meta.get("avg_best_iteration", "n/a")
        print(f"  {i+1:<5} {r['config']:<15} {r['seed']:<6} {acc:>8.4f} {sharpe:>8.4f} {pbo:>6.2f} {dsr:>8.4f} {brier:>8.4f} {best_iter:>8}")

    if not completed:
        print("\n  No completed jobs!")
        return 1

    # 5. Save best model
    print(f"\n{'=' * 70}")
    print("STEP 5: BEST MODEL DETAILS")
    print("=" * 70)

    best = completed[0]
    best_meta = best.get("meta", {})
    best_metrics = best.get("metrics", {})
    best_dossier = best.get("dossier", {})
    best_artifact = best.get("artifact", {})

    print(f"\n  Best: {best['config']} / seed {best['seed']}")
    print(f"  Artifact ID:    {best_artifact.get('artifact_id', 'n/a')}")
    print(f"  SHA256:         {best_artifact.get('sha256', 'n/a')[:32]}...")
    print(f"  Size:           {best_artifact.get('size_bytes', 0):,} bytes")
    print(f"  Accuracy:       {best_metrics.get('accuracy', 'n/a')}")
    print(f"  Logloss:        {best_metrics.get('logloss', 'n/a')}")
    print(f"  Brier Score:    {best_meta.get('brier_score', 'n/a')}")
    print(f"  Win Rate:       {best_meta.get('win_rate', 'n/a')}")
    print(f"  Sharpe Ratio:   {best_meta.get('sharpe_ratio', 'n/a')}")
    print(f"  Max Drawdown:   {best_meta.get('max_drawdown', 'n/a')}")
    print(f"  PBO:            {best_dossier.get('pbo', 'n/a')}")
    print(f"  Deflated Sharpe:{best_dossier.get('deflated_sharpe', 'n/a')}")
    print(f"  Avg Best Iter:  {best_meta.get('avg_best_iteration', 'n/a')}")
    print(f"  Fold Best Iters:{best_meta.get('fold_best_iterations', 'n/a')}")
    print(f"  N Features:     {best_meta.get('n_features', 'n/a')}")
    print(f"  N Rows:         {best_meta.get('n_rows', 'n/a')}")
    print(f"  N Folds:        {best_meta.get('n_folds', 'n/a')}")
    print(f"  Output on vol:  {best.get('output', {}).get('output_prefix', 'n/a')}")

    # 6. Verify HMAC on best model
    print(f"\n{'=' * 70}")
    print("STEP 6: VERIFY HMAC ON BEST MODEL")
    print("=" * 70)

    from quant_foundry.signatures import verify_callback

    output = best.get("output", {})
    callback_str = output.get("callback_payload", "")
    sig = output.get("callback_signature", "")
    ts = int(output.get("callback_ts", 0))
    sig_valid = verify_callback(
        callback_str.encode("utf-8"),
        secret=CALLBACK_SECRET,
        signature=sig,
        ts=ts,
        job_id=best["job_id"],
    )
    print(f"  HMAC valid: {sig_valid}")

    # 7. Save all results
    results_dir = _REPO_ROOT / "data" / "runpod_grid_search" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # Save summary
    summary = []
    for r in completed:
        summary.append({
            "config": r["config"],
            "seed": r["seed"],
            "job_id": r["job_id"],
            "runpod_id": r["runpod_id"],
            "accuracy": float(r.get("metrics", {}).get("accuracy", 0)),
            "logloss": float(r.get("metrics", {}).get("logloss", 0)),
            "sharpe_ratio": float(r.get("meta", {}).get("sharpe_ratio", 0)),
            "brier_score": float(r.get("meta", {}).get("brier_score", 0)),
            "win_rate": float(r.get("meta", {}).get("win_rate", 0)),
            "max_drawdown": float(r.get("meta", {}).get("max_drawdown", 0)),
            "pbo": float(r.get("dossier", {}).get("pbo", 1)),
            "deflated_sharpe": float(r.get("dossier", {}).get("deflated_sharpe", 0)),
            "avg_best_iteration": r.get("meta", {}).get("avg_best_iteration", "n/a"),
            "artifact_id": r.get("artifact", {}).get("artifact_id", "n/a"),
        })

    (results_dir / "grid_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (results_dir / "best_model.json").write_text(
        json.dumps({
            "config": best["config"],
            "seed": best["seed"],
            "job_id": best["job_id"],
            "dossier": best.get("dossier", {}),
            "artifact": best.get("artifact", {}),
            "metrics": best.get("metrics", {}),
            "meta": best.get("meta", {}),
            "hmac_valid": sig_valid,
        }, indent=2), encoding="utf-8"
    )
    (results_dir / "all_results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n  Results saved: {results_dir}")

    print(f"\n{'=' * 70}")
    print(f"GRID SEARCH COMPLETE — {len(completed)}/{len(jobs)} jobs succeeded")
    print(f"{'=' * 70}")
    print(f"  Best config:    {best['config']} / seed {best['seed']}")
    print(f"  Best Sharpe:    {best_meta.get('sharpe_ratio', 'n/a')}")
    print(f"  Best PBO:       {best_dossier.get('pbo', 'n/a')}")
    print(f"  Best Accuracy:  {best_metrics.get('accuracy', 'n/a')}")
    print(f"  HMAC valid:     {sig_valid}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
