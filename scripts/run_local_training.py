"""Run a real LightGBM training job locally on the synthetic dataset.

This script demonstrates the full training pipeline:
1. Load the synthetic dataset parquet
2. Construct a RunPodTrainingRequest
3. Call RealLightGBMTrainer.train()
4. Save the model artifact + dossier to disk
5. Print a summary

Usage:
    uv run python scripts/run_local_training.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

# Bootstrap paths so we can import quant_foundry.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))

from quant_foundry.real_trainer import RealLightGBMTrainer  # noqa: E402
from quant_foundry.runpod_training import TrainingFailure  # noqa: E402
from quant_foundry.schemas import RunPodTrainingRequest  # noqa: E402


def main() -> int:
    dataset_parquet = (
        _REPO_ROOT
        / "data"
        / "datasets"
        / "backtest_synthetic"
        / "synthetic_s5_d500_h5d_seed42.parquet"
    )
    dataset_manifest = (
        _REPO_ROOT
        / "data"
        / "datasets"
        / "backtest_synthetic"
        / "synthetic_s5_d500_h5d_seed42.manifest.json"
    )
    output_dir = _REPO_ROOT / "data" / "training_runs" / "local_lightgbm_run"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not dataset_parquet.exists():
        print(f"ERROR: dataset parquet not found: {dataset_parquet}")
        return 1

    # Build the training request — dataset_manifest_ref is a file:// URI
    # to the parquet file (the trainer loads features + labels from it).
    dataset_ref = f"file://{dataset_parquet.as_posix()}"

    job_id = "local-lightgbm-run-001"
    req = RunPodTrainingRequest(
        job_id=job_id,
        dataset_manifest_ref=dataset_ref,
        model_family="lightgbm",
        random_seed=42,
        extra_constraints={"bar_seconds": "86400"},  # daily bars
    )

    trainer = RealLightGBMTrainer(n_folds=3, annualization_factor=252)

    # 10-minute deadline
    deadline_ns = time.time_ns() + 10 * 60 * 1_000_000_000

    print("=" * 60)
    print("Starting real LightGBM training run")
    print(f"  Dataset:  {dataset_parquet.name}")
    print(f"  Job ID:   {job_id}")
    print(f"  Model:    lightgbm (3-fold walk-forward)")
    print(f"  Seed:     42")
    print(f"  Deadline: 10 minutes")
    print("=" * 60)

    start_ns = time.time_ns()
    try:
        artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)
    except TrainingFailure as exc:
        print(f"\nTRAINING FAILED: {exc.error_code}: {exc.error_summary}")
        return 1

    elapsed_s = (time.time_ns() - start_ns) / 1_000_000_000

    # Save the artifact manifest
    artifact_path = output_dir / "artifact_manifest.json"
    artifact_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")

    # Save the dossier
    dossier_path = output_dir / "dossier.json"
    dossier_path.write_text(dossier.model_dump_json(indent=2), encoding="utf-8")

    # Save the dataset manifest ref for traceability
    ref_path = output_dir / "dataset_manifest_ref.txt"
    ref_path.write_text(dataset_ref, encoding="utf-8")

    # Copy the dataset manifest for local traceability
    manifest_copy = output_dir / "dataset.manifest.json"
    manifest_copy.write_text(dataset_manifest.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"\n{'=' * 60}")
    print(f"TRAINING COMPLETE ({elapsed_s:.1f}s)")
    print(f"{'=' * 60}")
    print(f"\nArtifact:")
    print(f"  artifact_id:    {artifact.artifact_id}")
    print(f"  sha256:         {artifact.sha256[:16]}...")
    print(f"  size_bytes:     {artifact.size_bytes:,}")
    print(f"  model_family:   {artifact.model_family}")
    print(f"  feature_schema: {artifact.feature_schema_hash[:16]}...")
    print(f"  label_schema:   {artifact.label_schema_hash[:16]}...")
    print(f"  code_git_sha:   {artifact.code_git_sha or 'n/a'}")

    print(f"\nDossier:")
    print(f"  model_id:       {dossier.model_id}")
    print(f"  authority:      {dossier.authority}")
    print(f"  dataset_ref:    {dossier.dataset_manifest_id[:60]}...")

    metrics = dossier.training_metrics
    print(f"\nMetrics:")
    print(f"  accuracy:       {metrics.get('accuracy', 'n/a')}")
    print(f"  logloss:        {metrics.get('logloss', 'n/a')}")
    print(f"  brier_score:    {dossier.metadata.get('brier_score', 'n/a')}")
    print(f"  win_rate:       {dossier.metadata.get('win_rate', 'n/a')}")
    print(f"  sharpe:         {dossier.metadata.get('sharpe_ratio', 'n/a')}")
    print(f"  max_drawdown:   {dossier.metadata.get('max_drawdown', 'n/a')}")
    print(f"  pbo:            {dossier.pbo}")
    print(f"  deflated_sharpe:{dossier.deflated_sharpe}")

    print(f"\nOutput files:")
    print(f"  {artifact_path}")
    print(f"  {dossier_path}")
    print(f"  {ref_path}")
    print(f"  {manifest_copy}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
