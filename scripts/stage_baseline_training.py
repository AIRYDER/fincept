"""Stage Task 1 dispatch proof: real manifest -> local trainer -> dispatch receipt.

Builds a deterministic ``TrainingManifest`` over fixture data, dispatches
through the local trainer (no live RunPod), and writes JSON receipts
under ``reports/training-stage/``.

This script is the operator-facing proof of the Stage Task 1 pipeline.
It does NOT call RunPod; the training is fully local and CPU-only.

Usage:
    uv run python scripts/stage_baseline_training.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import uuid
from typing import Any

# Make the quant_foundry package importable when running from repo root.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "services" / "quant_foundry" / "src"))

from quant_foundry.budget import BudgetGuard
from quant_foundry.dataset_manifest import FeatureLakeManifest  # noqa: E402
from quant_foundry.feature_lake import (  # noqa: E402
    FeatureLakeBuilder,
    FeatureRow,
    FeatureValue,
    UniverseEntry,
)
from quant_foundry.local_training_dispatch import (  # noqa: E402
    DispatchReceipt,
    LocalTrainingDispatcher,
    build_training_manifest_from_feature_lake,
    write_dispatch_receipt,
)
from quant_foundry.training_manifest import TrainingManifest  # noqa: E402

# Reproducibility: nanoseconds per day. Used to keep the manifest
# deterministic across runs.
NS_PER_DAY = 86_400_000_000_000


def _fixture_universe() -> tuple[UniverseEntry, ...]:
    """A small deterministic universe for the staging proof."""
    return (
        UniverseEntry(symbol="AAPL", listed_until=None, renamed_from=None),
        UniverseEntry(symbol="MSFT", listed_until=None, renamed_from=None),
    )


def _fixture_rows(n_days: int = 60) -> tuple[FeatureRow, ...]:
    """PIT-correct rows: every feature observed_at <= decision_time."""
    rows: list[FeatureRow] = []
    for d in range(n_days):
        ts = (10 + d) * NS_PER_DAY
        rows.append(
            FeatureRow(
                symbol="AAPL",
                event_ts=ts,
                decision_time=ts,
                features=(
                    FeatureValue(name="ret_1d", value=0.001 * d, observed_at=ts),
                    FeatureValue(
                        name="vol_20d", value=0.2 + 0.001 * (d % 5), observed_at=ts - NS_PER_DAY
                    ),
                ),
                label_horizon_ns=NS_PER_DAY,
            )
        )
        rows.append(
            FeatureRow(
                symbol="MSFT",
                event_ts=ts,
                decision_time=ts,
                features=(
                    FeatureValue(name="ret_1d", value=-0.0005 * d, observed_at=ts),
                    FeatureValue(
                        name="vol_20d", value=0.18 + 0.001 * (d % 7), observed_at=ts - NS_PER_DAY
                    ),
                ),
                label_horizon_ns=NS_PER_DAY,
            )
        )
    return tuple(rows)


def build_feature_lake_manifest() -> FeatureLakeManifest:
    """Build a deterministic feature-lake manifest from the fixture rows."""
    builder = FeatureLakeBuilder(
        dataset_id="ds-stage-baseline-001",
        universe=_fixture_universe(),
        rows=_fixture_rows(n_days=60),
        feature_schema_hash="feat-schema-hash-v1",
        label_schema_hash="label-schema-hash-v1",
        max_label_horizon_ns=NS_PER_DAY,
        n_folds=3,
        source_vintage_refs=["vintage-2026-06-25"],
    )
    return builder.build_manifest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=pathlib.Path,
        default=_REPO_ROOT / "reports" / "training-stage",
        help="Where to write the receipt JSON files (default: reports/training-stage).",
    )
    parser.add_argument(
        "--manifest-id",
        type=str,
        default="training-manifest-baseline-001",
        help="Deterministic manifest id (default: training-manifest-baseline-001).",
    )
    parser.add_argument(
        "--budget-cents",
        type=int,
        default=0,
        help="Budget envelope in cents; 0 = local / staging bypass (default: 0).",
    )
    args = parser.parse_args()

    # 1. Feature-lake manifest.
    lake_manifest = build_feature_lake_manifest()
    print(
        json.dumps(
            {
                "step": "feature_lake_manifest",
                "dataset_id": lake_manifest.dataset_id,
                "manifest_hash": lake_manifest.manifest_hash(),
                "row_count": lake_manifest.row_count,
                "n_folds": len(lake_manifest.folds.folds),
                "pit_proof_verified": lake_manifest.pit_proof_verified,
            },
            indent=2,
        )
    )

    # 2. Training manifest (operator envelope).
    training_manifest = build_training_manifest_from_feature_lake(
        feature_lake_manifest=lake_manifest,
        manifest_id=args.manifest_id,
        model_family="gbm",
        hyperparameters={
            "n_estimators": 100.0,
            "max_depth": 4.0,
            "learning_rate": 0.05,
            "min_child_samples": 20.0,
        },
        train_window_ns=30 * NS_PER_DAY,
        val_window_ns=10 * NS_PER_DAY,
        test_window_ns=10 * NS_PER_DAY,
        label_horizon_ns=NS_PER_DAY,
        random_seed=42,
        walk_forward_enabled=True,
        budget_cents=args.budget_cents,
        timeout_seconds=120,
        operator_note="stage task 1 dispatch proof",
    )
    print(
        json.dumps(
            {
                "step": "training_manifest",
                "manifest_id": training_manifest.manifest_id,
                "content_hash": training_manifest.content_hash,
                "model_family": training_manifest.model_family.value,
                "walk_forward_enabled": training_manifest.walk_forward_enabled,
                "budget_cents": training_manifest.budget_cents,
            },
            indent=2,
        )
    )

    # 3. Dispatch (no live RunPod).
    budget_dir = args.output_dir / "budget"
    budget_dir.mkdir(parents=True, exist_ok=True)
    guard = BudgetGuard(
        base_dir=budget_dir,
        monthly_budget_cents=10_000,  # generous, zero-cost will still bypass
        kill_switch_enabled=False,
    )
    dispatcher = LocalTrainingDispatcher(
        budget_guard=guard,
        callback_secret="staging-callback-secret-not-real",
        worker_id="local-stager-1",
    )
    job_id = f"qf:stage:{uuid.uuid4().hex[:8]}"
    as_of_ts = int(time.time_ns())
    receipt: DispatchReceipt = dispatcher.dispatch(
        training_manifest,
        job_id=job_id,
        as_of_ts=as_of_ts,
    )

    # 4. Persist receipts.
    manifest_path = args.output_dir / f"{training_manifest.manifest_id}.training_manifest.json"
    manifest_path.write_text(training_manifest.model_dump_json(indent=2), encoding="utf-8")
    receipt_path = write_dispatch_receipt(
        receipt, args.output_dir / f"{training_manifest.manifest_id}.dispatch_receipt.json"
    )
    print(
        json.dumps(
            {
                "step": "dispatch_receipt",
                "status": receipt.status.value,
                "receipt_id": receipt.receipt_id,
                "job_id": receipt.job_id,
                "artifact_id": receipt.artifact_id,
                "dossier_id": receipt.dossier_id,
                "dossier_authority": receipt.dossier_authority,
                "budget_allowed": receipt.budget_decision.allowed,
                "walk_forward": receipt.walk_forward.to_dict(),
                "manifest_path": str(manifest_path),
                "receipt_path": str(receipt_path),
            },
            indent=2,
        )
    )

    # 5. Exit non-zero on any non-dispatched status so CI / operators
    # notice a budget or trainer failure.
    if receipt.status.value != "dispatched":
        print(
            f"ERROR: dispatch ended in status {receipt.status.value}; "
            f"error_code={receipt.error_code}, error_summary={receipt.error_summary}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())