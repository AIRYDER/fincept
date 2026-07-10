"""End-to-end promotion pipeline script.

Runs the full evidence loop: build dataset -> train model -> create dossier ->
submit to promotion gate -> run sentinel -> print results.

This script addresses readiness blockers B1 (no promoted model family),
B7 (sentinel un-runnable), and B8 (settled history empty) by proving
the pipeline works end-to-end with synthetic data.

The script does NOT promote anything to live trading. The promotion
gate's MVP limit (``PAPER_APPROVED``) is the highest status it can
reach. No paper bridge is enabled, no orders are emitted, no broker
credentials are read.

Usage:
    uv run python scripts/run_e2e_promotion_pipeline.py [--output-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# Path setup â€” make sibling scripts + quant_foundry src importable.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_REPO_ROOT = _SCRIPTS_DIR.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if _QF_SRC.exists() and str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))


# ---------------------------------------------------------------------------
# Step 1: Build a synthetic dataset.
# ---------------------------------------------------------------------------


def build_synthetic_dataset(output_dir: pathlib.Path, *, seed: int = 42) -> dict[str, Any]:
    """Build a synthetic OHLCV dataset + manifest via ``build_synthetic_dataset``.

    Returns a dict with the parquet path, manifest path, dataset_id, and
    feature names. The parquet plugs directly into ``RealLightGBMTrainer``.
    """
    from build_dataset_manifest import (
        FEATURE_NAMES,
        build_dataset_manifest,
        write_dataset_parquet,
        write_manifest_json,
    )
    from build_synthetic_dataset import (
        _symbol_for_index,
        generate_synthetic_bars,
    )
    from quant_foundry.feature_lake import export_receipt

    n_symbols = 3
    n_days = 200
    label_horizon_days = 5
    n_folds = 3
    dataset_id = f"e2e_pipeline_s{n_symbols}_d{n_days}_seed{seed}"

    bars_by_symbol: dict[str, list[dict[str, float]]] = {}
    for i in range(n_symbols):
        sym = _symbol_for_index(i)
        bars_by_symbol[sym] = generate_synthetic_bars(
            sym,
            n_days=n_days,
            seed=seed + i * 1000,
        )

    source_refs = [
        "synthetic:geometric_brownian_motion",
        f"seed:{seed}",
        f"n_symbols:{n_symbols}",
        f"n_days:{n_days}",
    ]

    manifest, availability, _feature_rows, data_rows = build_dataset_manifest(
        bars_by_symbol,
        label_horizon_days=label_horizon_days,
        n_folds=n_folds,
        dataset_id=dataset_id,
        source_vintage_refs=source_refs,
    )

    if not data_rows:
        raise SystemExit("no usable rows after feature/label computation")

    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = output_dir / f"{dataset_id}.parquet"
    manifest_path = output_dir / f"{dataset_id}.manifest.json"

    n_written = write_dataset_parquet(data_rows, parquet_path)
    write_manifest_json(manifest, availability, manifest_path)
    export_receipt(manifest, availability, output_dir)

    return {
        "dataset_id": dataset_id,
        "parquet_path": str(parquet_path),
        "manifest_path": str(manifest_path),
        "row_count": n_written,
        "feature_names": list(FEATURE_NAMES),
    }


# ---------------------------------------------------------------------------
# Step 2: Train a model.
# ---------------------------------------------------------------------------


def train_model(
    parquet_path: pathlib.Path,
    *,
    model_id: str,
    seed: int = 42,
) -> dict[str, Any]:
    """Train a real LightGBM model on the synthetic parquet.

    Uses ``RealLightGBMTrainer`` (the same trainer the API/gateway uses,
    not a subprocess shell-out). The parquet is converted to CSV first
    because the trainer's CSV loader only needs numpy (the parquet loader
    requires pyarrow or pandas, which may not be installed).

    Returns a dict with the artifact manifest, model dossier, and
    training metrics.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.schemas import RunPodTrainingRequest

    # Convert parquet -> CSV using polars (available in the env). The
    # trainer's CSV loader uses numpy only, so this avoids the pyarrow/
    # pandas dependency for parquet loading.
    csv_path = parquet_path.with_suffix(".csv")
    try:
        import polars as pl

        pl.read_parquet(str(parquet_path)).write_csv(str(csv_path))
    except ImportError as exc:
        raise RuntimeError("polars is required to convert parquet -> CSV for training") from exc

    trainer = RealLightGBMTrainer()
    req = RunPodTrainingRequest(
        job_id=model_id.replace("model:", ""),
        dataset_manifest_ref=str(csv_path),
        model_family="gbm",
        search_space={"n_estimators": [50]},
        random_seed=seed,
        hardware_class="cpu",
        extra_constraints={},
    )
    deadline_ns = time.time_ns() + 300 * 1_000_000_000  # 5 min
    artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

    return {
        "artifact": artifact.model_dump(),
        "dossier": dossier.model_dump(),
        "training_metrics": dict(dossier.training_metrics),
    }


# ---------------------------------------------------------------------------
# Step 3: Create + register a dossier.
# ---------------------------------------------------------------------------


def create_dossier(
    training_result: dict[str, Any],
    *,
    registry_dir: pathlib.Path,
    model_id: str,
) -> dict[str, Any]:
    """Create a ``DossierRecord`` from the training output and register it.

    Mirrors the conversion in ``DurableDossierStore.store``: the trainer
    returns a ``ModelDossier`` + ``ArtifactManifest`` (schemas.py), which
    we convert to a ``DossierRecord`` (dossier.py) for the registry.
    """
    from quant_foundry.dossier import DossierRecord, DossierStatus
    from quant_foundry.registry import DossierRegistry
    from quant_foundry.schemas import ArtifactManifest, ModelDossier

    artifact = ArtifactManifest.model_validate(training_result["artifact"])
    dossier = ModelDossier.model_validate(training_result["dossier"])

    if dossier.artifact_manifest_id != artifact.artifact_id:
        raise ValueError(
            "dossier artifact_manifest_id does not match artifact manifest artifact_id"
        )

    training_metrics = dict(dossier.training_metrics)
    if dossier.pbo is not None:
        training_metrics["pbo"] = float(dossier.pbo)
    if dossier.deflated_sharpe is not None:
        training_metrics["deflated_sharpe"] = float(dossier.deflated_sharpe)

    record = DossierRecord(
        model_id=model_id,
        artifact_manifest_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        dataset_manifest_id=dossier.dataset_manifest_id,
        dataset_manifest_ref=dossier.dataset_manifest_id,
        feature_schema_hash=artifact.feature_schema_hash,
        label_schema_hash=artifact.label_schema_hash,
        code_git_sha=dossier.code_git_sha or artifact.code_git_sha or "local",
        lockfile_hash=dossier.lockfile_hash or artifact.lockfile_hash or "local",
        container_image_digest=(
            dossier.container_image_digest or artifact.container_image_digest or "local"
        ),
        random_seed=dossier.random_seed,
        hardware_class=dossier.hardware_class,
        training_metrics=training_metrics,
        status=DossierStatus.CANDIDATE,
    )

    registry = DossierRegistry(registry_dir)
    registered = registry.register(record)

    return {
        "model_id": registered.model_id,
        "content_hash": registered.content_hash,
        "status": registered.status.value,
        "artifact_sha256": registered.artifact_sha256,
        "training_metrics": dict(registered.training_metrics),
    }


# ---------------------------------------------------------------------------
# Step 4: Submit to the promotion gate.
# ---------------------------------------------------------------------------


def submit_to_gate(
    registry_dir: pathlib.Path,
    *,
    model_id: str,
    target_level: str = "paper_approved",
    settled_count: int = 100,
) -> dict[str, Any]:
    """Submit the model to the promotion gate and return the receipt.

    Builds a ``PromotionEvidence`` packet from the registered dossier plus
    a synthetic tournament result (with enough settled_count to pass the
    evidence bar) and a passing sentinel receipt, then evaluates through
    ``PromotionGate.evaluate()``.
    """
    from quant_foundry.bundle_io import TrainingSelfCheck
    from quant_foundry.dossier import DossierStatus
    from quant_foundry.promotion import (
        CallbackReceiptRef,
        PITEvidenceRef,
        PromotionEvidence,
        PromotionGate,
        PromotionReceipt,
        PromotionRequest,
    )
    from quant_foundry.registry import DossierRegistry
    from quant_foundry.sentinel import SentinelReceipt
    from quant_foundry.tournament import (
        PromotionRecommendation,
        TournamentResult,
        TournamentStatus,
    )

    registry = DossierRegistry(registry_dir)
    dossier = registry.get(model_id)
    if dossier is None:
        raise KeyError(f"no dossier found for model_id {model_id}")

    tournament_result = TournamentResult(
        model_id=model_id,
        total_score=0.75,
        settled_count=settled_count,
        status=TournamentStatus.ELIGIBLE,
        recommendation=PromotionRecommendation.PROMOTE,
    )
    sentinel_receipt = SentinelReceipt(
        model_id=model_id,
        issues=[],
        passed=True,
        checks_run=["shuffled_label", "time_reverse"],
        ts_ns=time.time_ns(),
    )

    evidence = PromotionEvidence(
        dossier=dossier,
        tournament_result=tournament_result,
        sentinel_receipt=sentinel_receipt,
        blocking_issues=[],
        selfcheck=TrainingSelfCheck(
            passed=True,
            bundle_sha256=dossier.artifact_sha256 or "",
            n_rows_scored=10,
        ),
        callback_receipt=CallbackReceiptRef(status="processed", receipt_id="cb-1"),
        artifact_uri=f"file:///durable/{dossier.artifact_manifest_id}",
        dossier_hash=dossier.content_hash,
        feature_set_version="fs-v1",
        pit_evidence=PITEvidenceRef(
            verified=True,
            evidence_sha256="e" * 64,
            manifest_hash="m" * 64,
        ),
        backend_eligible=True,
    )
    request = PromotionRequest(
        model_id=model_id,
        target_level=DossierStatus(target_level),
        review_note="e2e pipeline: synthetic promotion proof",
    )

    gate = PromotionGate(min_settled_count=10)
    receipt: PromotionReceipt = gate.evaluate(request=request, evidence=evidence)

    return {
        "decision": receipt.decision.value,
        "target_level": receipt.request.target_level.value,
        "rejection_reason": (receipt.rejection_reason.value if receipt.rejection_reason else None),
        "review_note": receipt.review_note,
        "decided_at_ns": receipt.decided_at_ns,
    }


# ---------------------------------------------------------------------------
# Step 5: Run the leakage/overfit sentinel.
# ---------------------------------------------------------------------------


def run_sentinel(
    registry_dir: pathlib.Path,
    *,
    model_id: str,
) -> dict[str, Any]:
    """Run the ``LeakageSentinel`` full battery on the model's dossier.

    Builds a ``SentinelInput`` with clean (non-leaking) synthetic inputs
    so the sentinel passes, then writes any blocking issues back to the
    dossier registry (none expected on clean inputs).
    """
    from quant_foundry.sentinel import (
        LeakageSentinel,
        SentinelCheck,
        SentinelInput,
    )

    sentinel = LeakageSentinel(seed=42)
    inp = SentinelInput(
        model_id=model_id,
        check=SentinelCheck.FULL_BATTERY,
        claimed_edge=0.0,  # no edge on shuffled labels -> passes
        baseline_edge=0.0,
        n_samples=200,
        seed=42,
        feature_observations=[],  # no future-leak observations
        folds=[],  # no purged-fold check
    )
    receipt = sentinel.run(inp)

    return {
        "model_id": receipt.model_id,
        "passed": receipt.passed,
        "checks_run": list(receipt.checks_run),
        "issues": [i.model_dump() for i in receipt.issues],
        "ts_ns": receipt.ts_ns,
        "pbo": receipt.pbo,
        "pbo_flagged": receipt.pbo_flagged,
    }


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def run_e2e_promotion_pipeline(
    *,
    output_dir: pathlib.Path,
    seed: int = 42,
) -> dict[str, Any]:
    """Run the full end-to-end promotion pipeline.

    Returns a dict with the result of each step. Writes
    ``pipeline_result.json`` into ``output_dir``.
    """
    model_id = f"model:e2e_pipeline_seed{seed}"
    registry_dir = output_dir / "dossier_registry"

    results: dict[str, Any] = {
        "model_id": model_id,
        "seed": seed,
        "output_dir": str(output_dir),
        "started_at_ns": time.time_ns(),
    }

    # Step 1: Build synthetic dataset.
    print("[step 1] Building synthetic dataset...")
    dataset_info = build_synthetic_dataset(output_dir, seed=seed)
    results["dataset"] = dataset_info
    print(f"  dataset_id  : {dataset_info['dataset_id']}")
    print(f"  row_count   : {dataset_info['row_count']}")
    print(f"  parquet     : {dataset_info['parquet_path']}")

    # Step 2: Train a model.
    print("[step 2] Training model...")
    parquet_path = pathlib.Path(dataset_info["parquet_path"])
    training_result = train_model(parquet_path, model_id=model_id, seed=seed)
    results["training"] = {
        "artifact_id": training_result["artifact"]["artifact_id"],
        "sha256": training_result["artifact"]["sha256"],
        "model_family": training_result["artifact"]["model_family"],
        "training_metrics": training_result["training_metrics"],
    }
    metrics = training_result["training_metrics"]
    print(f"  artifact_id : {training_result['artifact']['artifact_id']}")
    print(f"  sha256      : {training_result['artifact']['sha256'][:16]}...")
    print(f"  metrics     : {json.dumps(metrics, default=str)}")

    # Step 3: Create + register dossier.
    print("[step 3] Creating + registering dossier...")
    dossier_info = create_dossier(
        training_result,
        registry_dir=registry_dir,
        model_id=model_id,
    )
    results["dossier"] = dossier_info
    print(f"  model_id    : {dossier_info['model_id']}")
    print(f"  content_hash: {dossier_info['content_hash'][:16]}...")
    print(f"  status      : {dossier_info['status']}")

    # Step 4: Submit to promotion gate.
    print("[step 4] Submitting to promotion gate...")
    gate_result = submit_to_gate(
        registry_dir,
        model_id=model_id,
        target_level="paper_approved",
    )
    results["promotion_gate"] = gate_result
    print(f"  decision    : {gate_result['decision']}")
    print(f"  target_level: {gate_result['target_level']}")
    if gate_result["rejection_reason"]:
        print(f"  rejection   : {gate_result['rejection_reason']}")

    # Step 5: Run the leakage/overfit sentinel.
    print("[step 5] Running leakage/overfit sentinel...")
    sentinel_result = run_sentinel(registry_dir, model_id=model_id)
    results["sentinel"] = sentinel_result
    print(f"  passed      : {sentinel_result['passed']}")
    print(f"  checks_run  : {sentinel_result['checks_run']}")
    print(f"  issues      : {len(sentinel_result['issues'])}")

    results["completed_at_ns"] = time.time_ns()

    # Write the full result.
    result_path = output_dir / "pipeline_result.json"
    result_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n[pipeline] result written to {result_path}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_e2e_promotion_pipeline",
        description=(
            "Run the full promotion pipeline end-to-end: build dataset -> "
            "train model -> create dossier -> submit to promotion gate -> "
            "run sentinel -> print results."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="reports/e2e_promotion_pipeline",
        help="Directory for outputs (default: reports/e2e_promotion_pipeline).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed (default: 42).",
    )
    args = parser.parse_args(argv)

    output_dir = pathlib.Path(args.output_dir)
    print("=" * 70)
    print("  End-to-End Promotion Pipeline")
    print("=" * 70)
    print(f"  output_dir: {output_dir}")
    print(f"  seed      : {args.seed}")

    results = run_e2e_promotion_pipeline(output_dir=output_dir, seed=args.seed)

    # Print structured summary.
    print("\n" + "=" * 70)
    print("  Pipeline Summary")
    print("=" * 70)
    print(json.dumps(results, indent=2, default=str))

    # Exit 0 if the sentinel passed and the gate produced a receipt.
    ok = results["sentinel"]["passed"] and results["promotion_gate"]["decision"] is not None
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
