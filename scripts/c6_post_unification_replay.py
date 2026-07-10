"""C6 Task 13 — Post-Unification Replay Verification.

Replays the same deterministic fixtures from Task 10 through:

  1. Path A compat wrapper (PathACompatAdapter) — the new unified path
  2. Path B canonical ledger (SettlementLedger) — the canonical path

After unification, both paths should produce MATCHING settlement
semantics. The only allowed differences are legacy field names and
formatting.

Usage::

    uv run python scripts/c6_post_unification_replay.py
"""

from __future__ import annotations

import csv
import json
import pathlib
import sys
import tempfile
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

T0 = 1_700_000_000_000_000_000  # base time
HORIZON_NS = 3_600_000_000_000  # 1 hour
NOW_NS = T0 + HORIZON_NS + 60_000_000_000  # 1 min after horizon


# ---------------------------------------------------------------------------
# Fixtures (same as Task 10)
# ---------------------------------------------------------------------------


def build_fixtures() -> list[dict[str, Any]]:
    """Build deterministic replay fixtures (same as Task 10)."""
    fixtures: list[dict[str, Any]] = []

    # Fixture 1: winning long
    fixtures.append(
        {
            "name": "winning_long",
            "description": "Long prediction, price goes up 5%",
            "prediction": {
                "prediction_id": "fix-001",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": 1.0,
                "confidence": 0.7,
                "p_up": 0.7,
            },
            "prices": [
                {"ts": T0, "price": 100.0},
                {"ts": T0 + HORIZON_NS, "price": 105.0},
            ],
            "benchmark_prices": [
                {"ts": T0, "price": 400.0},
                {"ts": T0 + HORIZON_NS, "price": 402.0},
            ],
        }
    )

    # Fixture 2: losing long
    fixtures.append(
        {
            "name": "losing_long",
            "description": "Long prediction, price goes down 5%",
            "prediction": {
                "prediction_id": "fix-002",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": 1.0,
                "confidence": 0.6,
                "p_up": 0.6,
            },
            "prices": [
                {"ts": T0, "price": 100.0},
                {"ts": T0 + HORIZON_NS, "price": 95.0},
            ],
            "benchmark_prices": [
                {"ts": T0, "price": 400.0},
                {"ts": T0 + HORIZON_NS, "price": 402.0},
            ],
        }
    )

    # Fixture 3: winning short
    fixtures.append(
        {
            "name": "winning_short",
            "description": "Short prediction, price goes down 5%",
            "prediction": {
                "prediction_id": "fix-003",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": -1.0,
                "confidence": 0.65,
                "p_up": 0.35,
            },
            "prices": [
                {"ts": T0, "price": 100.0},
                {"ts": T0 + HORIZON_NS, "price": 95.0},
            ],
            "benchmark_prices": [
                {"ts": T0, "price": 400.0},
                {"ts": T0 + HORIZON_NS, "price": 402.0},
            ],
        }
    )

    # Fixture 4: flat
    fixtures.append(
        {
            "name": "flat",
            "description": "No price movement",
            "prediction": {
                "prediction_id": "fix-004",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": 1.0,
                "confidence": 0.5,
                "p_up": 0.5,
            },
            "prices": [
                {"ts": T0, "price": 100.0},
                {"ts": T0 + HORIZON_NS, "price": 100.0},
            ],
            "benchmark_prices": [
                {"ts": T0, "price": 400.0},
                {"ts": T0 + HORIZON_NS, "price": 400.0},
            ],
        }
    )

    # Fixture 5: missing prices
    fixtures.append(
        {
            "name": "missing_prices",
            "description": "No price data available at horizon",
            "prediction": {
                "prediction_id": "fix-005",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": 1.0,
                "confidence": 0.7,
                "p_up": 0.7,
            },
            "prices": [],
            "benchmark_prices": [],
        }
    )

    # Fixture 6: partial prices
    fixtures.append(
        {
            "name": "partial_prices_entry_only",
            "description": "Entry price available, exit price missing",
            "prediction": {
                "prediction_id": "fix-006",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": 1.0,
                "confidence": 0.7,
                "p_up": 0.7,
            },
            "prices": [{"ts": T0, "price": 100.0}],
            "benchmark_prices": [{"ts": T0, "price": 400.0}],
        }
    )

    # Fixture 7: high confidence win
    fixtures.append(
        {
            "name": "high_confidence_win",
            "description": "High confidence (0.9) winning trade +10%",
            "prediction": {
                "prediction_id": "fix-007",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": 1.0,
                "confidence": 0.9,
                "p_up": 0.9,
            },
            "prices": [
                {"ts": T0, "price": 100.0},
                {"ts": T0 + HORIZON_NS, "price": 110.0},
            ],
            "benchmark_prices": [
                {"ts": T0, "price": 400.0},
                {"ts": T0 + HORIZON_NS, "price": 410.0},
            ],
        }
    )

    # Fixture 8: losing short
    fixtures.append(
        {
            "name": "losing_short",
            "description": "Short prediction, price goes up 5%",
            "prediction": {
                "prediction_id": "fix-008",
                "agent_id": "test-agent",
                "model_id": "test-model",
                "model_name": "test-model",
                "symbol": "AAPL",
                "ts_event": T0,
                "horizon_ns": HORIZON_NS,
                "direction": -1.0,
                "confidence": 0.6,
                "p_up": 0.4,
            },
            "prices": [
                {"ts": T0, "price": 100.0},
                {"ts": T0 + HORIZON_NS, "price": 105.0},
            ],
            "benchmark_prices": [
                {"ts": T0, "price": 400.0},
                {"ts": T0 + HORIZON_NS, "price": 402.0},
            ],
        }
    )

    return fixtures


# ---------------------------------------------------------------------------
# Path A compat wrapper (unified)
# ---------------------------------------------------------------------------


def replay_path_a_compat(fixture: dict[str, Any], tmp_dir: pathlib.Path) -> dict[str, Any]:
    """Replay a fixture through the PathACompatAdapter (unified path).

    This is the post-unification Path A — it delegates to Path B's
    SettlementLedger via the compat adapter.
    """
    from quant_foundry.metrics import PriceTick
    from settlements.compat import PathACompatAdapter

    from fincept_core.prediction_log import PredictionRow

    pred = fixture["prediction"]
    prices_data = fixture["prices"]
    benchmark_data = fixture.get("benchmark_prices", [])

    # Build a PredictionRow (Path A's input)
    pred_row = PredictionRow(
        id=pred["prediction_id"],
        agent_id=pred["agent_id"],
        model_name=pred["model_name"],
        ts_recorded=pred["ts_event"],
        ts_event=pred["ts_event"],
        horizon_ns=pred["horizon_ns"],
        symbol=pred["symbol"],
        direction=pred["direction"],
        confidence=pred["confidence"],
    )

    price_ticks = [PriceTick(ts=p["ts"], price=p["price"]) for p in prices_data]
    benchmark_ticks = [PriceTick(ts=p["ts"], price=p["price"]) for p in benchmark_data] or None

    adapter = PathACompatAdapter(
        settlement_ledger=__import__(
            "quant_foundry.settlement",
            fromlist=["SettlementLedger"],
        ).SettlementLedger(root=tmp_dir / "compat_a"),
    )

    record = adapter.settle_prediction(
        pred_row,
        prices=price_ticks,
        benchmark_prices=benchmark_ticks,
        now_ns=NOW_NS,
    )

    # Also read the Path B record from the ledger to get abnormal_return
    # and calibration_bucket (which are on the Path B record, not the
    # Path A record returned by the adapter).
    b_records = adapter.ledger.read_all()
    b_rec = next((r for r in b_records if r.prediction_id == pred["prediction_id"]), None)

    return {
        "path": "A_compat_wrapper",
        "status": record.status,
        "realized_return_gross": record.realized_return_gross,
        "realized_return_net": record.realized_return_net,
        "cost_fee_bps": record.cost_breakdown_fee_bps,
        "cost_spread_bps": record.cost_breakdown_spread_bps,
        "cost_slippage_bps": record.cost_breakdown_slippage_bps,
        "brier_component": record.brier_component,
        "cost_model_version": record.cost_model_version,
        "key_field": "agent_id",
        "key_value": record.agent_id,
        "abnormal_return": b_rec.abnormal_return if b_rec else None,
        "calibration_bucket": b_rec.calibration_bucket if b_rec else None,
        "settled_at_ns": record.settled_at_ns,
    }


# ---------------------------------------------------------------------------
# Path B canonical ledger (direct)
# ---------------------------------------------------------------------------


def replay_path_b(fixture: dict[str, Any], tmp_dir: pathlib.Path) -> dict[str, Any]:
    """Replay a fixture through Path B (quant_foundry.SettlementLedger.settle)."""
    from quant_foundry.metrics import PriceTick
    from quant_foundry.settlement import SettlementLedger
    from quant_foundry.settlement_sweep import default_cost_model

    pred = fixture["prediction"]
    prices_data = fixture["prices"]
    benchmark_data = fixture.get("benchmark_prices", [])

    price_ticks = [PriceTick(ts=p["ts"], price=p["price"]) for p in prices_data]
    benchmark_ticks = [PriceTick(ts=p["ts"], price=p["price"]) for p in benchmark_data] or None

    ledger = SettlementLedger(root=tmp_dir / "path_b")
    cost_model = default_cost_model()

    record = ledger.settle(
        prediction=pred,
        prices=price_ticks,
        benchmark_prices=benchmark_ticks,
        cost_model=cost_model,
        now_ns=NOW_NS,
    )

    return {
        "path": "B_settlement_ledger",
        "status": record.status.value,
        "realized_return_gross": record.realized_return_gross,
        "realized_return_net": record.realized_return_net,
        "cost_fee_bps": cost_model.fee_bps,
        "cost_spread_bps": cost_model.spread_bps,
        "cost_slippage_bps": cost_model.slippage_bps,
        "brier_component": record.brier,
        "cost_model_version": record.cost_model_version,
        "key_field": "model_id",
        "key_value": record.model_id,
        "abnormal_return": record.abnormal_return,
        "calibration_bucket": record.calibration_bucket,
        "settled_at_ns": record.settled_at_ns,
    }


# ---------------------------------------------------------------------------
# Divergence analysis
# ---------------------------------------------------------------------------

COMPARE_FIELDS = [
    "status",
    "realized_return_gross",
    "realized_return_net",
    "cost_fee_bps",
    "cost_spread_bps",
    "cost_slippage_bps",
    "brier_component",
    "cost_model_version",
    "abnormal_return",
    "calibration_bucket",
]


def classify_divergence(field: str, val_a: Any, val_b: Any) -> str:
    """Classify a divergence between two values."""
    if val_a == val_b:
        return "MATCH"
    if val_a is None and val_b is None:
        return "MATCH"
    if val_a is None or val_b is None:
        return "MISSING_FIELD"
    if isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
        if abs(val_a - val_b) < 1e-9:
            return "MATCH"
        if abs(val_a - val_b) < 0.01:
            return "ROUNDING_ONLY"
    if field in ("cost_fee_bps", "cost_spread_bps", "cost_slippage_bps"):
        return "EXPECTED_MODE_DIFFERENCE"
    if field == "cost_model_version":
        return "EXPECTED_MODE_DIFFERENCE"
    if field in ("realized_return_gross", "realized_return_net", "brier_component"):
        return "SEMANTIC_DIFFERENCE"
    if field == "status":
        return "REVIEW_REQUIRED"
    return "REVIEW_REQUIRED"


def analyze_divergences(
    fixture_name: str,
    result_a: dict[str, Any],
    result_b: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare two settlement path outputs field by field."""
    divergences: list[dict[str, Any]] = []
    for field in COMPARE_FIELDS:
        val_a = result_a.get(field)
        val_b = result_b.get(field)
        classification = classify_divergence(field, val_a, val_b)
        if classification != "MATCH":
            delta = None
            if isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
                delta = val_b - val_a
            divergences.append(
                {
                    "fixture_name": fixture_name,
                    "path_a": "A_compat_wrapper",
                    "path_b": "B_settlement_ledger",
                    "field": field,
                    "value_a": val_a,
                    "value_b": val_b,
                    "delta": delta,
                    "classification": classification,
                }
            )
    return divergences


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Get current branch SHA for the report directory
    import subprocess

    sha = (
        subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT)
        .decode()
        .strip()
    )

    report_dir = _REPO_ROOT / "reports" / "c6-settlement-replay" / sha / "post-unification"
    report_dir.mkdir(parents=True, exist_ok=True)

    print("C6 Task 13 — Post-Unification Replay Verification")
    print(f"  Branch SHA: {sha}")
    print(f"  Report dir: {report_dir}")
    print()

    fixtures = build_fixtures()
    print(f"Fixtures: {len(fixtures)}")
    for f in fixtures:
        print(f"  {f['name']}: {f['description']}")
    print()

    # Save fixtures
    fixtures_out = [
        {
            "name": f["name"],
            "description": f["description"],
            "prediction": f["prediction"],
            "prices": f["prices"],
            "benchmark_prices": f.get("benchmark_prices", []),
        }
        for f in fixtures
    ]
    (report_dir / "fixtures.json").write_text(
        json.dumps(fixtures_out, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Replay every fixture through both paths
    all_results: list[dict[str, Any]] = []
    all_divergences: list[dict[str, Any]] = []

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="c6_post_uni_"))

    for fixture in fixtures:
        print(f"--- {fixture['name']} ---")
        result_a = replay_path_a_compat(fixture, tmp_dir)
        result_b = replay_path_b(fixture, tmp_dir)

        print(
            f"  A (compat): status={result_a['status']} gross={result_a['realized_return_gross']} net={result_a['realized_return_net']}"
        )
        print(
            f"  B (direct): status={result_b['status']} gross={result_b['realized_return_gross']} net={result_b['realized_return_net']}"
        )

        all_results.append(
            {
                "fixture_name": fixture["name"],
                "description": fixture["description"],
                "path_a_compat": result_a,
                "path_b_direct": result_b,
            }
        )

        divergences = analyze_divergences(fixture["name"], result_a, result_b)
        all_divergences.extend(divergences)

        if divergences:
            print(f"  Divergences: {len(divergences)}")
            for d in divergences:
                print(
                    f"    {d['field']}: A={d['value_a']} B={d['value_b']} [{d['classification']}]"
                )
        else:
            print("  Divergences: 0 (MATCH)")
        print()

    # Save replay results
    (report_dir / "post_unification_replay.json").write_text(
        json.dumps(all_results, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    # Save divergence report
    (report_dir / "post_unification_divergences.json").write_text(
        json.dumps(all_divergences, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    # Save normalized CSV
    csv_path = report_dir / "post_unification_normalized.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "fixture_name",
                "path",
                "status",
                "realized_return_gross",
                "realized_return_net",
                "cost_fee_bps",
                "cost_spread_bps",
                "cost_slippage_bps",
                "brier_component",
                "cost_model_version",
                "abnormal_return",
                "calibration_bucket",
                "key_field",
                "key_value",
                "settled_at_ns",
            ]
        )
        for r in all_results:
            for path_key, path_label in [
                ("path_a_compat", "A_compat"),
                ("path_b_direct", "B_direct"),
            ]:
                p = r[path_key]
                writer.writerow(
                    [
                        r["fixture_name"],
                        path_label,
                        p.get("status"),
                        p.get("realized_return_gross"),
                        p.get("realized_return_net"),
                        p.get("cost_fee_bps"),
                        p.get("cost_spread_bps"),
                        p.get("cost_slippage_bps"),
                        p.get("brier_component"),
                        p.get("cost_model_version"),
                        p.get("abnormal_return"),
                        p.get("calibration_bucket"),
                        p.get("key_field"),
                        p.get("key_value"),
                        p.get("settled_at_ns"),
                    ]
                )

    # Summary
    classification_counts: dict[str, int] = {}
    for d in all_divergences:
        c = d["classification"]
        classification_counts[c] = classification_counts.get(c, 0) + 1

    print("=" * 60)
    print("POST-UNIFICATION DIVERGENCE SUMMARY")
    print("=" * 60)
    print(f"Total fixtures: {len(fixtures)}")
    print(f"Total comparisons: {len(fixtures) * len(COMPARE_FIELDS)}")
    print(f"Total divergences: {len(all_divergences)}")
    print()
    for c, count in sorted(classification_counts.items()):
        print(f"  {c}: {count}")
    print()

    if not all_divergences:
        print("[OK] ALL MATCH - Path A compat wrapper and Path B canonical ledger")
        print("  produce identical settlement semantics.")
    else:
        print("[WARN] Divergences remain - review before opening PR.")

    # Cleanup
    import shutil

    shutil.rmtree(tmp_dir, ignore_errors=True)

    return 0 if not all_divergences else 1


if __name__ == "__main__":
    sys.exit(main())
