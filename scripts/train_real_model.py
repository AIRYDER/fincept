"""Fetch real market data via yfinance and train a real LightGBM model.

This script:
1. Fetches real daily OHLCV bars from Yahoo Finance (free, no API key)
2. Builds a proper FeatureLakeManifest with PIT-correct features + labels
3. Runs the full deep training pipeline (RealLightGBMTrainer + RunPodTrainingHandler)
4. Feeds the signed callback through the gateway
5. Verifies the outbox + dossier registry
6. Prints a full summary with real metrics

Usage:
    uv run python scripts/train_real_model.py
    uv run python scripts/train_real_model.py --symbols AAPL,MSFT,GOOGL,AMZN,NVDA --years 3
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time
from collections.abc import Sequence

# Bootstrap paths
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))
_SHARED = _REPO_ROOT / "runpod" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

# build_dataset_manifest is in scripts/
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from build_dataset_manifest import (  # noqa: E402
    NS_PER_DAY,
    build_dataset_manifest,
    write_dataset_parquet,
    write_manifest_json,
)
from quant_foundry.feature_lake import export_receipt  # noqa: E402
from quant_foundry.gateway import QuantFoundryGateway  # noqa: E402
from quant_foundry.real_trainer import RealLightGBMTrainer  # noqa: E402
from quant_foundry.runpod_training import RunPodTrainingHandler  # noqa: E402
from quant_foundry.schemas import RunPodTrainingRequest  # noqa: E402


def fetch_yfinance_bars(
    symbols: list[str],
    years: int,
) -> dict[str, list[dict[str, float]]]:
    """Fetch real daily OHLCV bars from Yahoo Finance via yfinance.

    Returns a dict mapping symbol → list of bar dicts with keys:
    ts_event (ns), open, high, low, close, volume.
    """
    import yfinance as yf
    from datetime import datetime, timezone

    end = datetime.now(timezone.utc)
    start = datetime(end.year - years, end.month, end.day, tzinfo=timezone.utc)

    bars_by_symbol: dict[str, list[dict[str, float]]] = {}
    for sym in symbols:
        print(f"  Fetching {sym} ({start.date()} to {end.date()})...", end=" ", flush=True)
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"))
            if df.empty:
                print("NO DATA")
                continue
            bars: list[dict[str, float]] = []
            for idx, row in df.iterrows():
                # yfinance returns timezone-aware index (America/New_York)
                # yfinance DatetimeIndex.value is already nanoseconds since epoch
                ts_ns = int(idx.tz_convert("UTC").value)
                bars.append(
                    {
                        "ts_event": ts_ns,
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    }
                )
            bars_by_symbol[sym] = bars
            print(f"{len(bars)} bars")
        except Exception as exc:
            print(f"ERROR: {exc}")

    return bars_by_symbol


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="train_real_model",
        description="Fetch real market data via yfinance and train a real LightGBM model.",
    )
    parser.add_argument(
        "--symbols",
        default="AAPL,MSFT,GOOGL,AMZN,NVDA",
        help="Comma-separated ticker symbols (default: AAPL,MSFT,GOOGL,AMZN,NVDA).",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=3,
        help="Years of history to fetch (default: 3).",
    )
    parser.add_argument(
        "--label-horizon-days",
        type=int,
        default=5,
        help="Label horizon in days (default: 5).",
    )
    parser.add_argument(
        "--n-folds",
        type=int,
        default=3,
        help="Number of walk-forward folds (default: 3).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for LightGBM (default: 42).",
    )
    args = parser.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("ERROR: no symbols specified")
        return 1

    # --- 1. Fetch real data ---
    print("=" * 70)
    print("STEP 1: FETCH REAL MARKET DATA (yfinance)")
    print("=" * 70)
    print(f"  Symbols: {symbols}")
    print(f"  Years:   {args.years}")
    print()

    bars_by_symbol = fetch_yfinance_bars(symbols, args.years)
    if not bars_by_symbol:
        print("\nERROR: no data fetched. Check your internet connection.")
        return 1

    total_bars = sum(len(b) for b in bars_by_symbol.values())
    print(f"\n  Total bars fetched: {total_bars}")
    print(f"  Symbols with data:  {sorted(bars_by_symbol.keys())}")

    # --- 2. Build dataset manifest ---
    print(f"\n{'=' * 70}")
    print("STEP 2: BUILD DATASET MANIFEST (features + labels + folds)")
    print("=" * 70)

    dataset_id = f"yfinance_{'_'.join(sorted(bars_by_symbol.keys()))}_y{args.years}_h{args.label_horizon_days}d"
    dataset_dir = _REPO_ROOT / "data" / "datasets" / "yfinance_real"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    source_refs = [
        "vendor:yfinance",
        f"symbols:{','.join(sorted(bars_by_symbol.keys()))}",
        f"years:{args.years}",
        f"label_horizon:{args.label_horizon_days}d",
        f"seed:{args.seed}",
    ]

    manifest, availability, feature_rows, data_rows = build_dataset_manifest(
        bars_by_symbol,
        label_horizon_days=args.label_horizon_days,
        n_folds=args.n_folds,
        dataset_id=dataset_id,
        source_vintage_refs=source_refs,
    )

    if not data_rows:
        print("ERROR: no usable rows after feature/label computation.")
        print("  Try increasing --years or using different symbols.")
        return 1

    parquet_path = dataset_dir / f"{dataset_id}.parquet"
    manifest_path = dataset_dir / f"{dataset_id}.manifest.json"

    n_written = write_dataset_parquet(data_rows, parquet_path)
    write_manifest_json(manifest, availability, manifest_path)
    receipt = export_receipt(manifest, availability, dataset_dir)

    m_hash = manifest.manifest_hash()
    print(f"  dataset_id:          {manifest.dataset_id}")
    print(f"  manifest_hash:       {m_hash}")
    print(f"  row_count:           {manifest.row_count}")
    print(f"  parquet rows:        {n_written}")
    print(f"  feature_names:       {list(availability.expected_features)}")
    print(f"  pit_proof_verified:  {manifest.pit_proof_verified}")
    print(f"  folds:               {len(manifest.folds.folds)}")
    print(f"  parquet path:        {parquet_path}")
    print(f"  manifest path:       {manifest_path}")

    # Show label balance
    labels = [r["label"] for r in data_rows]
    n_up = sum(1 for l in labels if l == 1.0)
    n_down = sum(1 for l in labels if l == 0.0)
    print(f"  label balance:       {n_up} up / {n_down} down ({n_up / len(labels) * 100:.1f}% up)")

    # --- 3. Set up gateway ---
    print(f"\n{'=' * 70}")
    print("STEP 3: SET UP GATEWAY")
    print("=" * 70)

    callback_secret = "real-training-run-secret"
    base_dir = _REPO_ROOT / "data" / "real_training_run"
    status_dir = base_dir / "worker_status"

    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    status_dir.mkdir(parents=True, exist_ok=True)

    os.environ["QUANT_FOUNDRY_ENABLED"] = "true"
    os.environ["QUANT_FOUNDRY_MODE"] = "local_mock"
    os.environ["QUANT_FOUNDRY_SHADOW_ONLY"] = "true"
    os.environ["QUANT_FOUNDRY_CALLBACK_SECRET"] = callback_secret
    os.environ["QUANT_FOUNDRY_BASE_DIR"] = str(base_dir)
    os.environ["QUANT_FOUNDRY_WORKER_STATUS_DIR"] = str(status_dir)
    os.environ["QUANT_FOUNDRY_USE_REAL_TRAINER"] = "true"

    gateway = QuantFoundryGateway.from_env(base_dir=base_dir)
    print(f"  mode:          {gateway.mode}")
    print(f"  enabled:       {gateway.enabled}")
    print(f"  base_dir:      {base_dir}")

    # --- 4. Build training request ---
    print(f"\n{'=' * 70}")
    print("STEP 4: CONSTRUCT TRAINING REQUEST")
    print("=" * 70)

    dataset_ref = f"file://{parquet_path.as_posix()}"
    job_id = f"real-train-{int(time.time())}"
    idempotency_key = f"idemp-{job_id}"

    request_payload = {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": dataset_ref,
        "model_family": "lightgbm",
        "random_seed": args.seed,
        "extra_constraints": {"bar_seconds": "86400"},
    }
    req = RunPodTrainingRequest.model_validate(request_payload)

    print(f"  job_id:              {req.job_id}")
    print(f"  model_family:        {req.model_family}")
    print(f"  dataset_manifest_ref:{req.dataset_manifest_ref[:80]}...")
    print(f"  random_seed:         {req.random_seed}")

    # --- 5. Enqueue + train ---
    print(f"\n{'=' * 70}")
    print("STEP 5: ENQUEUE + RUN REAL LIGHTGBM TRAINING")
    print("=" * 70)

    from quant_foundry.outbox import JobStatus

    gateway.outbox.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key=idempotency_key,
        request_payload=request_payload,
        priority=0,
        budget_cents=0,
    )
    print(f"  [outbox] Job enqueued: {job_id} (status: queued)")

    # Write worker status: started
    try:
        from worker_status import write_status
        write_status(job_id, "started")
        print(f"  [worker_status] wrote 'started'")
    except ImportError:
        pass

    trainer = RealLightGBMTrainer(n_folds=args.n_folds, annualization_factor=252)
    handler = RunPodTrainingHandler(
        callback_secret=callback_secret,
        trainer=trainer,
        deadline_seconds=600,
        worker_id="local-real-worker-1",
    )

    print(f"\n  Training LightGBM with {args.n_folds}-fold walk-forward validation...")
    print(f"  Dataset: {n_written} rows, {len(availability.expected_features)} features")
    print()

    start_ns = time.time_ns()
    result = handler.handle(req)
    elapsed_s = (time.time_ns() - start_ns) / 1_000_000_000

    # Write worker status: completed
    try:
        from worker_status import write_status
        write_status(job_id, "completed", artifact_id=result.artifact_id)
    except ImportError:
        pass

    print(f"  Training completed in {elapsed_s:.1f}s")
    print(f"  artifact_id: {result.artifact_id}")
    print(f"  dossier_id:  {result.dossier_id}")

    # --- 6. Parse callback ---
    envelope = json.loads(result.callback_payload)
    dossier_data = envelope["payload"]["dossier"]
    artifact_data = envelope["payload"]["artifact_manifest"]

    print(f"\n{'=' * 70}")
    print("STEP 6: TRAINING RESULTS")
    print("=" * 70)

    print(f"\n  Artifact:")
    print(f"    artifact_id:       {artifact_data['artifact_id']}")
    print(f"    sha256:            {artifact_data['sha256'][:16]}...")
    print(f"    size_bytes:        {artifact_data['size_bytes']:,}")
    print(f"    model_family:      {artifact_data['model_family']}")
    print(f"    feature_schema:    {artifact_data['feature_schema_hash'][:16]}...")
    print(f"    label_schema:      {artifact_data['label_schema_hash'][:16]}...")
    print(f"    code_git_sha:      {artifact_data.get('code_git_sha', 'n/a')}")

    metrics = dossier_data["training_metrics"]
    meta = dossier_data["metadata"]

    print(f"\n  Dossier:")
    print(f"    model_id:          {dossier_data['model_id']}")
    print(f"    authority:         {dossier_data['authority']}")
    print(f"    dataset_manifest:  {dossier_data['dataset_manifest_id'][:60]}...")

    print(f"\n  Walk-Forward Metrics (out-of-sample):")
    print(f"    accuracy:          {metrics.get('accuracy', 'n/a'):.6f}")
    print(f"    logloss:           {metrics.get('logloss', 'n/a'):.6f}")
    print(f"    brier_score:       {meta.get('brier_score', 'n/a')}")
    print(f"    win_rate:          {meta.get('win_rate', 'n/a')}")
    print(f"    sharpe_ratio:      {meta.get('sharpe_ratio', 'n/a')}")
    print(f"    max_drawdown:      {meta.get('max_drawdown', 'n/a')}")
    print(f"    pbo:               {dossier_data['pbo']}")
    print(f"    deflated_sharpe:   {dossier_data['deflated_sharpe']}")

    # Interpret the metrics
    print(f"\n  Interpretation:")
    acc = metrics.get("accuracy", 0.5)
    pbo = dossier_data["pbo"]
    dsr = dossier_data["deflated_sharpe"]
    if acc > 0.55:
        print(f"    accuracy {acc:.3f} > 0.55 — model has some predictive signal")
    elif acc > 0.52:
        print(f"    accuracy {acc:.3f} — weak signal, marginal edge")
    else:
        print(f"    accuracy {acc:.3f} — near chance, limited signal in features")
    if pbo < 0.5:
        print(f"    PBO {pbo:.3f} < 0.5 — low probability of backtest overfitting")
    else:
        print(f"    PBO {pbo:.3f} — high overfitting risk (typical for simple features)")
    if dsr > 0.5:
        print(f"    deflated Sharpe {dsr:.3f} — meaningful risk-adjusted edge after overfit penalty")
    elif dsr > 0:
        print(f"    deflated Sharpe {dsr:.3f} — small residual edge after overfit penalty")
    else:
        print(f"    deflated Sharpe {dsr:.3f} — no residual edge after overfit penalty")

    # --- 7. Process callback through gateway ---
    print(f"\n{'=' * 70}")
    print("STEP 7: PROCESS CALLBACK THROUGH GATEWAY")
    print("=" * 70)

    receipt = gateway.receive_callback(
        job_id=job_id,
        payload=result.callback_payload,
        signature=result.callback_signature,
        ts=result.callback_ts,
        worker_id="local-real-worker-1",
    )

    print(f"  ok:            {receipt.get('ok')}")
    print(f"  inbox_status:  {receipt.get('inbox_status')}")
    print(f"  outbox_status: {receipt.get('outbox_status')}")

    # --- 8. Verify ---
    print(f"\n{'=' * 70}")
    print("STEP 8: VERIFICATION")
    print("=" * 70)

    final_rec = gateway.outbox.get(job_id)
    print(f"  Outbox status:  {final_rec.status.value}")

    if final_rec.status != JobStatus.COMPLETED:
        print(f"  ERROR: job did not reach COMPLETED!")
        print(f"  error_code:    {final_rec.error_code}")
        print(f"  error_summary: {final_rec.error_summary}")
        return 1

    try:
        registry = gateway.dossier_registry()
        dossiers = registry.list()
        print(f"  Dossier registry: {len(dossiers)} dossier(s)")
        if dossiers:
            d = dossiers[-1]
            print(f"    model_id:  {d.model_id}")
            print(f"    status:    {d.status.value}")
    except Exception as exc:
        print(f"  Dossier registry: {exc}")

    # --- 9. Save results ---
    results_dir = base_dir / "results"
    results_dir.mkdir(exist_ok=True)
    (results_dir / "callback_envelope.json").write_text(
        json.dumps(envelope, indent=2), encoding="utf-8"
    )
    (results_dir / "artifact_manifest.json").write_text(
        json.dumps(artifact_data, indent=2), encoding="utf-8"
    )
    (results_dir / "dossier.json").write_text(
        json.dumps(dossier_data, indent=2), encoding="utf-8"
    )
    (results_dir / "gateway_receipt.json").write_text(
        json.dumps(receipt, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n  Results saved to: {results_dir}")

    print(f"\n{'=' * 70}")
    print(f"REAL MODEL TRAINING COMPLETE ({elapsed_s:.1f}s training)")
    print(f"{'=' * 70}")
    print(f"  Job:       {job_id}")
    print(f"  Artifact:  {result.artifact_id}")
    print(f"  Dossier:   {result.dossier_id}")
    print(f"  Outbox:    {final_rec.status.value}")
    print(f"  Authority: {dossier_data['authority']}")
    print(f"  Accuracy:  {metrics.get('accuracy', 'n/a'):.4f}")
    print(f"  PBO:       {dossier_data['pbo']}")
    print(f"  Deflated:  {dossier_data['deflated_sharpe']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
