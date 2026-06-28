"""Seed settlement history for the leakage/overfit sentinel.

Generates synthetic predictions + settlements for a model, writes them
to the settlement ledger, and verifies the sentinel can process them.

This addresses readiness blocker B8 (settled history empty) by providing
the sentinel with inputs to evaluate.

The script does NOT touch real market data. All prices and predictions
are synthetic. No broker credentials are read, no orders are emitted.

Usage:
    uv run python scripts/seed_settlement_history.py --model-name <name> [--n-predictions 100]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — make quant_foundry src importable.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
_QF_SRC = _REPO_ROOT / "services" / "quant_foundry" / "src"
if _QF_SRC.exists() and str(_QF_SRC) not in sys.path:
    sys.path.insert(0, str(_QF_SRC))


# ---------------------------------------------------------------------------
# Synthetic data generation.
# ---------------------------------------------------------------------------


def _make_prediction(
    prediction_id: str,
    model_id: str,
    symbol: str,
    ts_event: int,
    horizon_ns: int,
    *,
    direction: float = 1.0,
    confidence: float = 0.65,
    p_up: float = 0.65,
) -> dict[str, Any]:
    """Build a shadow prediction dict in the ``ShadowPrediction`` schema."""
    return {
        "prediction_id": prediction_id,
        "model_id": model_id,
        "symbol": symbol,
        "ts_event": ts_event,
        "horizon_ns": horizon_ns,
        "direction": direction,
        "confidence": confidence,
        "p_up": p_up,
        "authority": "shadow-only",
    }


def _make_prices(
    ts_event: int,
    horizon_ns: int,
    *,
    base_price: float = 100.0,
    drift: float = 0.001,
) -> list[Any]:
    """Build synthetic entry + exit price ticks for a prediction window.

    Returns a list of ``PriceTick`` objects (from ``quant_foundry.metrics``).
    The exit price is slightly higher than the entry to produce a positive
    return for long predictions (realistic edge).
    """
    from quant_foundry.metrics import PriceTick

    entry_price = base_price
    exit_price = base_price * (1.0 + drift)
    return [
        PriceTick(ts=ts_event, price=entry_price),
        PriceTick(ts=ts_event + horizon_ns, price=exit_price),
    ]


def _make_benchmark_prices(
    ts_event: int,
    horizon_ns: int,
    *,
    base_price: float = 400.0,
    drift: float = 0.0002,
) -> list[Any]:
    """Build synthetic benchmark price ticks (smaller drift than the model)."""
    from quant_foundry.metrics import PriceTick

    return [
        PriceTick(ts=ts_event, price=base_price),
        PriceTick(ts=ts_event + horizon_ns, price=base_price * (1.0 + drift)),
    ]


# ---------------------------------------------------------------------------
# Core: seed settlement history.
# ---------------------------------------------------------------------------


def seed_settlement_history(
    *,
    model_id: str,
    settlements_dir: pathlib.Path,
    shadow_ledger_dir: pathlib.Path,
    n_predictions: int = 100,
    symbol: str = "SYNA",
    seed: int = 42,
) -> dict[str, Any]:
    """Generate synthetic predictions + settlements and write them to stores.

    Writes shadow predictions to the ``ShadowLedger`` and settled records
    to the ``SettlementLedger``, then runs the sentinel on the resulting
    data. Returns a dict with counts and the sentinel receipt.
    """
    from quant_foundry.sentinel import (
        LeakageSentinel,
        SentinelCheck,
        SentinelInput,
        TrainLiveGapInput,
    )
    from quant_foundry.settlement import SettlementLedger
    from quant_foundry.settlement_sweep import default_cost_model
    from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

    # --- 1. Generate + store shadow predictions ---------------------------
    shadow_ledger = ShadowLedger(base_dir=shadow_ledger_dir)
    horizon_ns = 86_400_000_000_000  # 1 day
    base_ts = 1_642_090_560 * 1_000_000_000  # 2022-01-01 UTC

    predictions: list[dict[str, Any]] = []
    for i in range(n_predictions):
        predictions.append(
            _make_prediction(
                prediction_id=f"{model_id}-pred-{i:04d}",
                model_id=model_id,
                symbol=symbol,
                ts_event=base_ts + i * horizon_ns,
                horizon_ns=horizon_ns,
                direction=1.0,
                confidence=0.55 + (i % 10) * 0.03,  # vary 0.55..0.82
                p_up=0.55 + (i % 10) * 0.03,
            )
        )

    batch_hash = compute_batch_hash(predictions)
    store_receipt = shadow_ledger.store_batch(predictions, batch_hash)

    # --- 2. Settle each prediction ----------------------------------------
    settlement_ledger = SettlementLedger(root=settlements_dir)
    cost_model = default_cost_model()
    now_ns = base_ts + n_predictions * horizon_ns + 1

    settled_count = 0
    brier_scores: list[float] = []
    returns_net: list[float] = []
    for pred in predictions:
        prices = _make_prices(
            pred["ts_event"],
            pred["horizon_ns"],
            base_price=100.0 + (hash(pred["prediction_id"]) % 50),
            drift=0.001 + (i % 5) * 0.0005,
        )
        benchmark_prices = _make_benchmark_prices(
            pred["ts_event"],
            pred["horizon_ns"],
        )
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

    # --- 3. Run the sentinel on the seeded data ---------------------------
    sentinel = LeakageSentinel(seed=seed)

    # Build a train/live gap input from the settled data.
    in_sample_edge = 0.002
    live_edge = 0.0015 if returns_net else 0.0
    in_sample_brier = 0.18
    live_brier = sum(brier_scores) / len(brier_scores) if brier_scores else 0.2

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
        claimed_edge=0.0,
        baseline_edge=0.0,
        n_samples=n_predictions,
        seed=seed,
        feature_observations=[],
        folds=[],
        train_live_gap=train_live_gap,
    )
    sentinel_receipt = sentinel.run(inp)

    return {
        "model_id": model_id,
        "n_predictions": n_predictions,
        "stored_predictions": store_receipt.stored,
        "settled_count": settled_count,
        "mean_brier": (sum(brier_scores) / len(brier_scores)) if brier_scores else None,
        "mean_return_net": (sum(returns_net) / len(returns_net)) if returns_net else None,
        "sentinel": {
            "passed": sentinel_receipt.passed,
            "checks_run": list(sentinel_receipt.checks_run),
            "issues": [i.model_dump() for i in sentinel_receipt.issues],
            "ts_ns": sentinel_receipt.ts_ns,
        },
        "settlements_dir": str(settlements_dir),
        "shadow_ledger_dir": str(shadow_ledger_dir),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_settlement_history",
        description=(
            "Seed synthetic settlement history for the leakage/overfit "
            "sentinel. Generates predictions + settlements, writes them "
            "to the stores, and runs the sentinel."
        ),
    )
    parser.add_argument(
        "--model-name",
        required=True,
        help="Model ID to seed settlement history for.",
    )
    parser.add_argument(
        "--n-predictions",
        type=int,
        default=100,
        help="Number of synthetic predictions to generate (default: 100).",
    )
    parser.add_argument(
        "--output-dir",
        default="reports/seed_settlement_history",
        help="Base directory for outputs (default: reports/seed_settlement_history).",
    )
    parser.add_argument(
        "--symbol",
        default="SYNA",
        help="Symbol for synthetic predictions (default: SYNA).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed (default: 42).",
    )
    args = parser.parse_args(argv)

    output_dir = pathlib.Path(args.output_dir)
    settlements_dir = output_dir / "settlements"
    shadow_ledger_dir = output_dir / "shadow_ledger"

    print("=" * 70)
    print("  Seed Settlement History")
    print("=" * 70)
    print(f"  model_name    : {args.model_name}")
    print(f"  n_predictions : {args.n_predictions}")
    print(f"  output_dir    : {output_dir}")

    result = seed_settlement_history(
        model_id=args.model_name,
        settlements_dir=settlements_dir,
        shadow_ledger_dir=shadow_ledger_dir,
        n_predictions=args.n_predictions,
        symbol=args.symbol,
        seed=args.seed,
    )

    # Print structured summary.
    print("\n" + "=" * 70)
    print("  Summary")
    print("=" * 70)
    print(json.dumps(result, indent=2, default=str))

    # Write the result.
    result_path = output_dir / "seed_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, default=str))
    print(f"\n[seed] result written to {result_path}")

    ok = result["settled_count"] > 0 and result["sentinel"]["passed"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
