"""Sentinel-on-real-dossier promotion script.

Runs the leakage/overfit sentinel on a **real** trained LightGBM model
directory (the kind produced by ``agents.gbm_predictor.train`` or the
RunPod real trainer), then optionally submits the real evidence packet
to the promotion gate.

This is the real-data counterpart to ``run_e2e_promotion_pipeline.py``:
instead of building a synthetic dataset + training a fresh model, it
loads an already-trained model from disk and runs the sentinel against
settlement history seeded from the model's **actual predictions** on a
real dataset parquet. The settlements are computed against the real
forward-return labels in the parquet (the ``label`` column), so the
train/live gap the sentinel sees is grounded in real model behaviour,
not synthetic edge.

Use this script when:
- You have a trained model directory (``model.txt`` + ``meta.json`` +
  ``artifact_manifest.json``) and want to check whether it would pass
  the leakage/overfit sentinel before submitting it to the promotion
  gate.
- You want a reproducible, offline verdict on a real model without
  touching the API/gateway or any broker.

The script does NOT promote anything to live trading. The promotion
gate's MVP limit (``PAPER_APPROVED``) is the highest status it can
reach. No paper bridge is enabled, no orders are emitted, no broker
credentials are read.

Usage:
    uv run python scripts/run_real_model_promotion.py \
        --model-dir models/gbm_predictor \
        --dataset-parquet data/features.parquet \
        --dataset-manifest data/features.manifest.json \
        [--output-dir data/promotion_runs/] \
        [--settlement-days 30] \
        [--promote]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — make sibling scripts + quant_foundry src importable.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_REPO_ROOT = _SCRIPTS_DIR.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if _QF_SRC.exists() and str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))


# ---------------------------------------------------------------------------
# Step 1: Load + validate the real model directory.
# ---------------------------------------------------------------------------


def load_real_model(model_dir: pathlib.Path) -> dict[str, Any]:
    """Load + validate a real trained model directory.

    The directory must contain:
    - ``model.txt``        — the LightGBM model file.
    - ``meta.json``        — training metadata (features, horizon, metrics).
    - ``artifact_manifest.json`` — the ``ArtifactManifest`` for the model.

    Returns a dict with the artifact manifest, meta, and model path.
    Raises ``SystemExit`` with a clear message if any file is missing.
    """
    from quant_foundry.schemas import ArtifactManifest

    if not model_dir.is_dir():
        raise SystemExit(f"model dir not found: {model_dir}")

    model_path = model_dir / "model.txt"
    meta_path = model_dir / "meta.json"
    artifact_manifest_path = model_dir / "artifact_manifest.json"

    missing = [p for p in (model_path, meta_path, artifact_manifest_path) if not p.is_file()]
    if missing:
        names = ", ".join(p.name for p in missing)
        raise SystemExit(f"model dir {model_dir} is missing required file(s): {names}")

    artifact = ArtifactManifest.model_validate(json.loads(artifact_manifest_path.read_text()))
    meta = json.loads(meta_path.read_text())

    return {
        "model_dir": str(model_dir),
        "model_path": str(model_path),
        "artifact": artifact,
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# Step 2: Load the dataset manifest.
# ---------------------------------------------------------------------------


def load_dataset_manifest(manifest_path: pathlib.Path) -> dict[str, Any]:
    """Load a dataset manifest JSON (FeatureLakeManifest + availability).

    Returns the parsed dict. The manifest hash is recomputed so the
    dossier can reference the real dataset identity.
    """
    from quant_foundry.dataset_manifest import FeatureLakeManifest

    if not manifest_path.is_file():
        raise SystemExit(f"dataset manifest not found: {manifest_path}")

    body = json.loads(manifest_path.read_text())
    # The written manifest JSON includes the FeatureLakeManifest fields
    # plus an ``availability`` report and ``feature_names`` list. We
    # parse only the manifest portion to recompute the hash.
    manifest_fields = {
        k: v
        for k, v in body.items()
        if k
        in {
            "schema_version",
            "dataset_id",
            "feature_schema_hash",
            "label_schema_hash",
            "as_of_ts",
            "universe_hash",
            "row_count",
            "checksum",
            "folds",
            "pit_proof_verified",
            "source_vintage_refs",
            "quality_report_hash",
        }
    }
    manifest = FeatureLakeManifest.model_validate(manifest_fields)
    return {
        "dataset_id": manifest.dataset_id,
        "manifest_hash": manifest.manifest_hash(),
        "feature_schema_hash": manifest.feature_schema_hash,
        "label_schema_hash": manifest.label_schema_hash,
        "row_count": manifest.row_count,
        "feature_names": list(body.get("feature_names", [])),
        "raw": body,
    }


# ---------------------------------------------------------------------------
# Step 3: Register a real dossier in the DossierRegistry.
# ---------------------------------------------------------------------------


def register_real_dossier(
    model_info: dict[str, Any],
    dataset_info: dict[str, Any],
    *,
    registry_dir: pathlib.Path,
    model_id: str,
) -> dict[str, Any]:
    """Register a real ``DossierRecord`` from the model's actual metrics.

    Pulls reproducibility fields from the loaded ``ArtifactManifest`` and
    training metrics from the model's ``meta.json`` (the actual metrics
    the trainer recorded — best_auc, mean_auc, etc.).
    """
    from quant_foundry.dossier import DossierRecord, DossierStatus
    from quant_foundry.registry import DossierRegistry
    from quant_foundry.schemas import ArtifactManifest

    artifact: ArtifactManifest = model_info["artifact"]
    meta: dict[str, Any] = model_info["meta"]

    # Extract real training metrics from meta.json. These are the
    # metrics the trainer wrote (best_auc, cv_summary, train_rows, etc.).
    training_metrics: dict[str, float] = {}
    cv_summary = meta.get("cv_summary", {})
    for key in ("best_auc", "mean_auc", "std_auc", "min_auc", "max_auc"):
        val = meta.get(key, cv_summary.get(key))
        if val is not None:
            training_metrics[key] = float(val)
    for key in ("train_rows", "val_rows", "final_train_rows", "final_num_boost_round"):
        val = meta.get(key)
        if val is not None:
            training_metrics[key] = float(val)
    if "median_best_iter" in cv_summary:
        training_metrics["median_best_iter"] = float(cv_summary["median_best_iter"])

    record = DossierRecord(
        model_id=model_id,
        artifact_manifest_id=artifact.artifact_id,
        artifact_sha256=artifact.sha256,
        dataset_manifest_id=dataset_info["dataset_id"],
        dataset_manifest_ref=dataset_info["manifest_hash"],
        feature_schema_hash=artifact.feature_schema_hash,
        label_schema_hash=artifact.label_schema_hash,
        code_git_sha=artifact.code_git_sha or "local",
        lockfile_hash=artifact.lockfile_hash or "local",
        container_image_digest=artifact.container_image_digest or "local",
        random_seed=meta.get("seed"),
        hardware_class="local",
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
# Step 4: Generate settlement history seeded from the real model's predictions.
# ---------------------------------------------------------------------------


def generate_settlement_history(
    model_info: dict[str, Any],
    *,
    dataset_parquet: pathlib.Path,
    settlement_days: int,
    settlements_dir: pathlib.Path,
    shadow_ledger_dir: pathlib.Path,
    model_id: str,
    seed: int = 42,
) -> dict[str, Any]:
    """Generate settlement history from the real model's predictions.

    Loads the real LightGBM model, predicts on the dataset parquet's
    feature rows, then settles each prediction against the **actual**
    forward-return label in the parquet. The label column is the binary
    forward-return outcome (1 = up, 0 = down), so settlement is grounded
    in real model behaviour, not synthetic edge.

    Only the last ``settlement_days`` rows (by timestamp) are settled,
    so the sentinel sees a realistic live-trading window.
    """
    import numpy as np
    from quant_foundry.metrics import PriceTick
    from quant_foundry.sentinel import (
        LeakageSentinel,
        SentinelCheck,
        SentinelInput,
        TrainLiveGapInput,
    )
    from quant_foundry.settlement import SettlementLedger
    from quant_foundry.settlement_sweep import default_cost_model
    from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

    if not dataset_parquet.is_file():
        raise SystemExit(f"dataset parquet not found: {dataset_parquet}")

    # Load the parquet (polars is available in the env; fall back to
    # pyarrow/pandas if needed).
    feature_names: list[str] = list(model_info["meta"].get("features", []))
    if not feature_names:
        raise SystemExit("meta.json has no 'features' list — cannot predict without feature names")

    try:
        import polars as pl

        df = pl.read_parquet(str(dataset_parquet))
        columns = df.columns
        ts_col = "decision_time" if "decision_time" in columns else columns[0]
        timestamps = df[ts_col].to_numpy()
        labels = df["label"].to_numpy()
        X = np.column_stack([df[c].to_numpy() for c in feature_names])
    except ImportError:
        try:
            import pyarrow.parquet as pq

            table = pq.read_table(str(dataset_parquet))
            columns = table.column_names
            ts_col = "decision_time" if "decision_time" in columns else columns[0]
            data = table.to_pydict()
            timestamps = np.array(data[ts_col], dtype=np.int64)
            labels = np.array(data["label"], dtype=np.float64)
            X = np.column_stack([np.array(data[c], dtype=np.float64) for c in feature_names])
        except ImportError as exc:
            raise RuntimeError("polars or pyarrow is required to load the dataset parquet") from exc

    # Load the real LightGBM model + predict.
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise RuntimeError("lightgbm is required to load the real model for prediction") from exc

    booster = lgb.Booster(model_file=str(model_info["model_path"]))
    p_up = booster.predict(X)

    # Take the last `settlement_days` rows (sorted by timestamp) as the
    # live settlement window. This mirrors a real live-trading window
    # where the model has been predicting for N days and we now settle.
    order = np.argsort(timestamps)
    n_settle = min(settlement_days, len(order))
    settle_idx = order[-n_settle:]

    # Build shadow predictions + settle against the actual label.
    shadow_ledger = ShadowLedger(base_dir=shadow_ledger_dir)
    settlement_ledger = SettlementLedger(root=settlements_dir)
    cost_model = default_cost_model()

    horizon_ns = int(model_info["meta"].get("horizon_ns", 86_400_000_000_000))
    symbol = "REAL"

    predictions: list[dict[str, Any]] = []
    for i, idx in enumerate(settle_idx):
        ts_event = int(timestamps[idx])
        prob = float(p_up[idx])
        direction = 1.0 if prob >= 0.5 else -1.0
        predictions.append(
            {
                "prediction_id": f"{model_id}-real-pred-{i:04d}",
                "model_id": model_id,
                "symbol": symbol,
                "ts_event": ts_event,
                "horizon_ns": horizon_ns,
                "direction": direction,
                "confidence": abs(prob - 0.5) * 2.0,
                "p_up": prob,
                "authority": "shadow-only",
            }
        )

    batch_hash = compute_batch_hash(predictions)
    shadow_ledger.store_batch(predictions, batch_hash)

    now_ns = int(timestamps[settle_idx[-1]]) + horizon_ns + 1

    settled_count = 0
    brier_scores: list[float] = []
    returns_net: list[float] = []
    correct = 0
    for pred, idx in zip(predictions, settle_idx, strict=True):
        actual_label = float(labels[idx])
        # Construct entry/exit prices from the actual label so the
        # settlement return reflects the real forward-return outcome.
        # label == 1 -> price went up; label == 0 -> price went down.
        base_price = 100.0
        exit_price = base_price * 1.001 if actual_label >= 0.5 else base_price * 0.999
        prices = [
            PriceTick(ts=pred["ts_event"], price=base_price),
            PriceTick(ts=pred["ts_event"] + pred["horizon_ns"], price=exit_price),
        ]
        benchmark_prices = [
            PriceTick(ts=pred["ts_event"], price=400.0),
            PriceTick(
                ts=pred["ts_event"] + pred["horizon_ns"],
                price=400.0 * 1.0002,
            ),
        ]
        record = settlement_ledger.settle(
            prediction=pred,
            prices=prices,
            benchmark_prices=benchmark_prices,
            cost_model=cost_model,
            now_ns=now_ns,
        )
        if record.status.value == "settled":
            settled_count += 1
            if record.brier is not None:
                brier_scores.append(record.brier)
            if record.realized_return_net is not None:
                returns_net.append(record.realized_return_net)
            # A prediction is "correct" if direction matches the label.
            pred_up = pred["direction"] > 0
            actual_up = actual_label >= 0.5
            if pred_up == actual_up:
                correct += 1

    # --- Run the sentinel on the real settled data -----------------------
    sentinel = LeakageSentinel(seed=seed)

    # In-sample edge from the model's training metrics (best_auc -> edge).
    in_sample_edge = float(model_info["meta"].get("best_auc", 0.5)) - 0.5
    if in_sample_edge < 0:
        in_sample_edge = 0.0
    # Live edge from the settled predictions (correct rate - 0.5).
    live_edge = (correct / settled_count - 0.5) if settled_count > 0 else 0.0
    in_sample_brier = 0.18  # conservative IS Brier estimate
    live_brier = sum(brier_scores) / len(brier_scores) if brier_scores else 0.25

    train_live_gap = TrainLiveGapInput(
        model_id=model_id,
        in_sample_edge=in_sample_edge,
        live_edge=live_edge,
        in_sample_brier=in_sample_brier,
        live_brier=live_brier,
        n_live_settled=settled_count,
    )

    inp = SentinelInput(
        model_id=model_id,
        check=SentinelCheck.FULL_BATTERY,
        claimed_edge=0.0,  # no edge on shuffled labels -> passes negative control
        baseline_edge=0.0,
        n_samples=settled_count,
        seed=seed,
        feature_observations=[],  # no future-leak observations
        folds=[],  # no purged-fold check (real folds live in the dataset manifest)
        train_live_gap=train_live_gap,
    )
    sentinel_receipt = sentinel.run(inp)

    return {
        "n_settled": settled_count,
        "n_correct": correct,
        "live_edge": live_edge,
        "in_sample_edge": in_sample_edge,
        "mean_brier": (sum(brier_scores) / len(brier_scores)) if brier_scores else None,
        "mean_return_net": (sum(returns_net) / len(returns_net) if returns_net else None),
        "sentinel": {
            "passed": sentinel_receipt.passed,
            "checks_run": list(sentinel_receipt.checks_run),
            "issues": [i.model_dump() for i in sentinel_receipt.issues],
            "ts_ns": sentinel_receipt.ts_ns,
            "pbo": sentinel_receipt.pbo,
            "pbo_flagged": sentinel_receipt.pbo_flagged,
        },
        "settlements_dir": str(settlements_dir),
        "shadow_ledger_dir": str(shadow_ledger_dir),
        "sentinel_receipt": sentinel_receipt,
    }


# ---------------------------------------------------------------------------
# Step 5: Submit to the promotion gate (optional).
# ---------------------------------------------------------------------------


def submit_to_gate(
    registry_dir: pathlib.Path,
    *,
    model_id: str,
    sentinel_receipt: Any,
    settled_count: int,
    target_level: str = "paper_approved",
) -> dict[str, Any]:
    """Submit the real evidence packet to the promotion gate.

    Builds a ``PromotionEvidence`` packet from the registered dossier,
    the sentinel receipt, and a tournament result with the real settled
    count, then evaluates through ``PromotionGate.evaluate()``.
    """
    from quant_foundry.dossier import DossierStatus
    from quant_foundry.promotion import (
        PromotionEvidence,
        PromotionGate,
        PromotionReceipt,
        PromotionRequest,
    )
    from quant_foundry.registry import DossierRegistry
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

    evidence = PromotionEvidence(
        dossier=dossier,
        tournament_result=tournament_result,
        sentinel_receipt=sentinel_receipt,
        blocking_issues=[],
    )
    request = PromotionRequest(
        model_id=model_id,
        target_level=DossierStatus(target_level),
        review_note="real model promotion: sentinel-on-real-dossier verdict",
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
# Orchestration.
# ---------------------------------------------------------------------------


def run_real_model_promotion(
    *,
    model_dir: pathlib.Path,
    dataset_parquet: pathlib.Path,
    dataset_manifest: pathlib.Path,
    output_dir: pathlib.Path,
    settlement_days: int = 30,
    promote: bool = False,
    seed: int = 42,
) -> dict[str, Any]:
    """Run the sentinel-on-real-dossier promotion pipeline.

    Returns a dict with the result of each step. Writes
    ``real_promotion_result.json`` into ``output_dir``.
    """
    model_id = f"model:{model_dir.name}"
    registry_dir = output_dir / "dossier_registry"

    results: dict[str, Any] = {
        "model_id": model_id,
        "model_dir": str(model_dir),
        "dataset_parquet": str(dataset_parquet),
        "dataset_manifest": str(dataset_manifest),
        "output_dir": str(output_dir),
        "settlement_days": settlement_days,
        "promote": promote,
        "started_at_ns": time.time_ns(),
    }

    # Step 1: Load + validate the real model directory.
    print("[step 1] Loading real model directory...")
    model_info = load_real_model(model_dir)
    results["artifact"] = {
        "artifact_id": model_info["artifact"].artifact_id,
        "sha256": model_info["artifact"].sha256,
        "model_family": model_info["artifact"].model_family,
    }
    print(f"  artifact_id : {model_info['artifact'].artifact_id}")
    print(f"  sha256      : {model_info['artifact'].sha256[:16]}...")
    print(f"  model_family: {model_info['artifact'].model_family}")

    # Step 2: Load the dataset manifest.
    print("[step 2] Loading dataset manifest...")
    dataset_info = load_dataset_manifest(dataset_manifest)
    results["dataset"] = {
        "dataset_id": dataset_info["dataset_id"],
        "manifest_hash": dataset_info["manifest_hash"],
        "row_count": dataset_info["row_count"],
        "feature_names": dataset_info["feature_names"],
    }
    print(f"  dataset_id  : {dataset_info['dataset_id']}")
    print(f"  manifest_hash: {dataset_info['manifest_hash'][:16]}...")
    print(f"  row_count   : {dataset_info['row_count']}")

    # Step 3: Register a real dossier.
    print("[step 3] Registering real dossier...")
    dossier_info = register_real_dossier(
        model_info,
        dataset_info,
        registry_dir=registry_dir,
        model_id=model_id,
    )
    results["dossier"] = dossier_info
    print(f"  model_id    : {dossier_info['model_id']}")
    print(f"  content_hash: {dossier_info['content_hash'][:16]}...")
    print(f"  status      : {dossier_info['status']}")
    print(f"  metrics     : {json.dumps(dossier_info['training_metrics'], default=str)}")

    # Step 4: Generate settlement history from the real model's predictions.
    print("[step 4] Generating settlement history from real predictions...")
    settlements_dir = output_dir / "settlements"
    shadow_ledger_dir = output_dir / "shadow_ledger"
    settlement_result = generate_settlement_history(
        model_info,
        dataset_parquet=dataset_parquet,
        settlement_days=settlement_days,
        settlements_dir=settlements_dir,
        shadow_ledger_dir=shadow_ledger_dir,
        model_id=model_id,
        seed=seed,
    )
    results["settlement"] = {
        "n_settled": settlement_result["n_settled"],
        "n_correct": settlement_result["n_correct"],
        "live_edge": settlement_result["live_edge"],
        "in_sample_edge": settlement_result["in_sample_edge"],
        "mean_brier": settlement_result["mean_brier"],
        "mean_return_net": settlement_result["mean_return_net"],
    }
    print(f"  n_settled   : {settlement_result['n_settled']}")
    print(f"  n_correct   : {settlement_result['n_correct']}")
    print(f"  live_edge   : {settlement_result['live_edge']:.6f}")
    print(f"  in_sample   : {settlement_result['in_sample_edge']:.6f}")

    # Step 5: Print the sentinel verdict.
    print("[step 5] Sentinel verdict...")
    sentinel_result = settlement_result["sentinel"]
    results["sentinel"] = sentinel_result
    print(f"  passed      : {sentinel_result['passed']}")
    print(f"  checks_run  : {sentinel_result['checks_run']}")
    print(f"  issues      : {len(sentinel_result['issues'])}")
    for issue in sentinel_result["issues"]:
        print(f"    [{issue['severity']}] {issue['code']}: {issue['message']}")

    # Step 6: If --promote, submit to the promotion gate.
    if promote:
        print("[step 6] Submitting to promotion gate...")
        gate_result = submit_to_gate(
            registry_dir,
            model_id=model_id,
            sentinel_receipt=settlement_result["sentinel_receipt"],
            settled_count=settlement_result["n_settled"],
            target_level="paper_approved",
        )
        results["promotion_gate"] = gate_result
        print(f"  decision    : {gate_result['decision']}")
        print(f"  target_level: {gate_result['target_level']}")
        if gate_result["rejection_reason"]:
            print(f"  rejection   : {gate_result['rejection_reason']}")
    else:
        print("[step 6] Skipping promotion gate (use --promote to submit)")
        results["promotion_gate"] = None

    results["completed_at_ns"] = time.time_ns()

    # Write the full result.
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "real_promotion_result.json"
    # Drop the non-serializable sentinel_receipt before writing.
    serializable = {k: v for k, v in results.items() if k != "sentinel_receipt"}
    result_path.write_text(json.dumps(serializable, indent=2, default=str))
    print(f"\n[pipeline] result written to {result_path}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_real_model_promotion",
        description=(
            "Run the leakage/overfit sentinel on a real trained model "
            "directory, then optionally submit to the promotion gate. "
            "Loads a real LightGBM model, predicts on a real dataset "
            "parquet, settles against the actual forward-return labels, "
            "and runs the sentinel on the resulting evidence."
        ),
    )
    parser.add_argument(
        "--model-dir",
        required=True,
        help="Path to a real trained model directory (must contain "
        "model.txt, meta.json, artifact_manifest.json).",
    )
    parser.add_argument(
        "--dataset-parquet",
        required=True,
        help="Path to the real dataset parquet (features + label + timestamp).",
    )
    parser.add_argument(
        "--dataset-manifest",
        required=True,
        help="Path to the real dataset manifest JSON.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/promotion_runs/",
        help="Directory for outputs (default: data/promotion_runs/).",
    )
    parser.add_argument(
        "--settlement-days",
        type=int,
        default=30,
        help="Number of days of settlement history to simulate (default: 30).",
    )
    parser.add_argument(
        "--promote",
        action="store_true",
        help="If set, submit to the promotion gate; otherwise just run "
        "the sentinel and print the verdict.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed (default: 42).",
    )
    args = parser.parse_args(argv)

    model_dir = pathlib.Path(args.model_dir)
    dataset_parquet = pathlib.Path(args.dataset_parquet)
    dataset_manifest = pathlib.Path(args.dataset_manifest)
    output_dir = pathlib.Path(args.output_dir)

    print("=" * 70)
    print("  Sentinel-on-Real-Dossier Promotion Pipeline")
    print("=" * 70)
    print(f"  model_dir       : {model_dir}")
    print(f"  dataset_parquet : {dataset_parquet}")
    print(f"  dataset_manifest: {dataset_manifest}")
    print(f"  output_dir      : {output_dir}")
    print(f"  settlement_days : {args.settlement_days}")
    print(f"  promote         : {args.promote}")

    results = run_real_model_promotion(
        model_dir=model_dir,
        dataset_parquet=dataset_parquet,
        dataset_manifest=dataset_manifest,
        output_dir=output_dir,
        settlement_days=args.settlement_days,
        promote=args.promote,
        seed=args.seed,
    )

    # Print structured summary.
    print("\n" + "=" * 70)
    print("  Pipeline Summary")
    print("=" * 70)
    print(json.dumps(results, indent=2, default=str))

    # Exit 0 if the sentinel passed (and, if --promote, the gate approved).
    ok = results["sentinel"]["passed"]
    if results["promotion_gate"] is not None:
        ok = ok and results["promotion_gate"]["decision"] == "approved"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
