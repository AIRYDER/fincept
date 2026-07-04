from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.data import load_historical_outcomes
from news_impact_model.training import (
    optimize_analog_weights,
    walk_forward_optimize_analog_weights,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Optimize news-impact analog weights from labeled history."
    )
    parser.add_argument("dataset", help="Path to HistoricalOutcome JSONL, JSON, or CSV")
    parser.add_argument("--horizon", required=True, help="Return horizon to optimize")
    parser.add_argument(
        "--mode",
        choices=("walk-forward", "leave-one-out"),
        default="walk-forward",
        help="Validation mode. Prefer walk-forward for realistic historical tests.",
    )
    parser.add_argument(
        "--min-train-events",
        type=int,
        default=250,
        help="Minimum prior events required for each walk-forward prediction.",
    )
    parser.add_argument("--top-k", type=int, default=5, help="Analogs per prediction")
    args = parser.parse_args(argv)

    outcomes = load_historical_outcomes(args.dataset)
    if args.mode == "walk-forward":
        result = walk_forward_optimize_analog_weights(
            outcomes,
            horizon=args.horizon,
            min_train_events=args.min_train_events,
            top_k=args.top_k,
        )
    else:
        result = optimize_analog_weights(
            outcomes,
            horizon=args.horizon,
            top_k=args.top_k,
        )

    payload = {
        "dataset": str(Path(args.dataset)),
        "mode": args.mode,
        "horizon": args.horizon,
        "n_outcomes": len(outcomes),
        "n_predictions": result.evaluation.n_predictions,
        "mae": result.evaluation.mae,
        "directional_accuracy": result.evaluation.directional_accuracy,
        "candidates_tested": result.candidates_tested,
        "weights": asdict(result.weights),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
