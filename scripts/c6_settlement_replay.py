"""C6 Task 10 — Settlement Replay Harness.

Replays identical deterministic fixtures through every settlement path
and compares outputs to identify divergences.

Two settlement paths exist in the codebase:

  Path A (new/fincept_core): settlements.worker.tick_sync →
    fincept_core.datasets.SettlementStore
    - cost model: v1.default (fee 5 bps, spread 3 bps, slippage 0 bps)
    - key: agent_id
    - return: (close_t2 / close_t1) - 1.0 (no direction)
    - net: gross - (fee + spread) / 10000

  Path B (old/quant_foundry): quant_foundry.settlement.SettlementLedger.settle
    - cost model: cm-v1 (fee 10 bps, spread 5 bps, slippage 3 bps, borrow 25 bps/day)
    - key: model_id
    - return: direction-aware (long: (exit-entry)/entry, short: (entry-exit)/entry)
    - net: gross - (fee + spread + slippage + borrow) / 10000

Usage::

    uv run python scripts/c6_settlement_replay.py
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

MAIN_SHA = "5cfb6cfaf75bae5bb67fad298fc1716217682a9d"
REPORT_DIR = _REPO_ROOT / "reports" / "c6-settlement-replay" / MAIN_SHA[:8]

# Fixed timestamps (ns)
T0 = 1_700_000_000_000_000_000  # base time
HORIZON_NS = 3_600_000_000_000  # 1 hour
NOW_NS = T0 + HORIZON_NS + 60_000_000_000  # 1 min after horizon

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_fixtures() -> list[dict[str, Any]]:
    """Build deterministic replay fixtures.

    Each fixture is a prediction + price pair that exercises a specific
    settlement scenario.
    """
    fixtures: list[dict[str, Any]] = []

    # Fixture 1: simple winning long trade
    # Entry 100, Exit 105 → gross +5%
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

    # Fixture 2: simple losing long trade
    # Entry 100, Exit 95 → gross -5%
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

    # Fixture 3: winning short trade
    # Entry 100, Exit 95 → gross +5% for short (direction=-1)
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

    # Fixture 4: flat / no movement
    # Entry 100, Exit 100 → gross 0%
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

    # Fixture 5: missing prices (pending_data)
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

    # Fixture 6: partial prices (only entry, no exit)
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
            "prices": [
                {"ts": T0, "price": 100.0},
            ],
            "benchmark_prices": [
                {"ts": T0, "price": 400.0},
            ],
        }
    )

    # Fixture 7: high confidence winning trade
    # Entry 100, Exit 110 → gross +10%, confidence 0.9
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

    # Fixture 8: short losing trade
    # Entry 100, Exit 105 → gross -5% for short
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
# Path A: settlements.worker.tick_sync (new/fincept_core)
# ---------------------------------------------------------------------------


def replay_path_a(fixture: dict[str, Any], tmp_dir: pathlib.Path) -> dict[str, Any]:
    """Replay a fixture through Path A (settlements.worker.tick_sync).

    Returns normalized output fields.
    """
    from settlements.worker import _build_pending_data_record, _build_settled_record

    from fincept_core.prediction_log import PredictionRow

    pred = fixture["prediction"]
    prices = fixture["prices"]

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

    # Path A: market_data_source returns close at ts2
    # The worker calls source(symbol, ts_event, ts_event) → close_t1
    # and source(symbol, ts_event, ts_event + horizon_ns) → close_t2
    close_t1 = None
    close_t2 = None
    for p in prices:
        if p["ts"] == pred["ts_event"]:
            close_t1 = p["price"]
        if p["ts"] == pred["ts_event"] + pred["horizon_ns"]:
            close_t2 = p["price"]

    if close_t1 is not None and close_t2 is not None and close_t1 != 0 and close_t2 != 0:
        record = _build_settled_record(
            pred_row,
            now_ns=NOW_NS,
            close_t1=close_t1,
            close_t2=close_t2,
        )
    else:
        record = _build_pending_data_record(pred_row)

    return {
        "path": "A_settlements_worker",
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
        "abnormal_return": None,  # Path A does not compute abnormal return
        "calibration_bucket": None,  # Path A does not compute calibration bucket
        "settled_at_ns": record.settled_at_ns,
    }


# ---------------------------------------------------------------------------
# Path B: quant_foundry.settlement.SettlementLedger.settle (old/quant_foundry)
# ---------------------------------------------------------------------------


def replay_path_b(fixture: dict[str, Any], tmp_dir: pathlib.Path) -> dict[str, Any]:
    """Replay a fixture through Path B (quant_foundry.SettlementLedger.settle).

    Returns normalized output fields.
    """
    from quant_foundry.metrics import PriceTick
    from quant_foundry.settlement import SettlementLedger
    from quant_foundry.settlement_sweep import default_cost_model

    pred = fixture["prediction"]
    prices_data = fixture["prices"]
    benchmark_data = fixture.get("benchmark_prices", [])

    # Build PriceTick list
    price_ticks = [PriceTick(ts=p["ts"], price=p["price"]) for p in prices_data]
    benchmark_ticks = [PriceTick(ts=p["ts"], price=p["price"]) for p in benchmark_data] or None

    # Path B: SettlementLedger.settle
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
        if field in ("abnormal_return", "calibration_bucket"):
            return "MISSING_FIELD"
        return "MISSING_FIELD"
    if isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
        if abs(val_a - val_b) < 1e-9:
            return "MATCH"
        if abs(val_a - val_b) < 0.01:
            return "ROUNDING_ONLY"
    if field == "cost_model_version":
        return "EXPECTED_MODE_DIFFERENCE"
    if field in ("cost_fee_bps", "cost_spread_bps", "cost_slippage_bps"):
        return "EXPECTED_MODE_DIFFERENCE"
    if field == "realized_return_gross":
        return "SEMANTIC_DIFFERENCE"
    if field == "realized_return_net":
        return "SEMANTIC_DIFFERENCE"
    if field == "brier_component":
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
                    "path_a": "A_settlements_worker",
                    "path_b": "B_settlement_ledger",
                    "field": field,
                    "value_a": val_a,
                    "value_b": val_b,
                    "delta": delta,
                    "classification": classification,
                    "likely_cause": _likely_cause(field, val_a, val_b),
                    "recommended_action": _recommended_action(field, classification),
                }
            )
    return divergences


def _likely_cause(field: str, val_a: Any, val_b: Any) -> str:
    if field == "cost_model_version":
        return "Different cost model versions (v1.default vs cm-v1) — by design"
    if field in ("cost_fee_bps", "cost_spread_bps", "cost_slippage_bps"):
        return "Different cost model parameters — v1.default vs cm-v1"
    if field == "realized_return_gross":
        return "Path A: (t2/t1)-1 (no direction). Path B: direction-aware return."
    if field == "realized_return_net":
        return "Different gross + different cost model → different net"
    if field == "brier_component":
        return "Path A: prob_up=(direction+1)/2. Path B: prob_up from prediction.p_up"
    if field == "abnormal_return":
        return "Path A does not compute abnormal return. Path B does."
    if field == "calibration_bucket":
        return "Path A does not compute calibration bucket. Path B does."
    if field == "status":
        return "Status string representation may differ (enum vs string)"
    return "Unknown"


def _recommended_action(field: str, classification: str) -> str:
    if classification == "EXPECTED_MODE_DIFFERENCE":
        return "Document as known difference. Choose one cost model during unification."
    if classification == "SEMANTIC_DIFFERENCE":
        return "Decide which return formula is canonical during unification."
    if classification == "MISSING_FIELD":
        return "Add missing field to the canonical path during unification."
    if classification == "REVIEW_REQUIRED":
        return "Investigate whether status values are compatible."
    return "Investigate"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("C6 Task 10 — Settlement Replay Harness")
    print(f"  Main SHA: {MAIN_SHA}")
    print(f"  Report dir: {REPORT_DIR}")
    print()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Build fixtures
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
    (REPORT_DIR / "fixtures.json").write_text(
        json.dumps(fixtures_out, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Replay every fixture through every path
    all_results: list[dict[str, Any]] = []
    all_divergences: list[dict[str, Any]] = []

    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="c6_replay_"))

    for fixture in fixtures:
        print(f"--- {fixture['name']} ---")
        result_a = replay_path_a(fixture, tmp_dir)
        result_b = replay_path_b(fixture, tmp_dir)

        print(
            f"  Path A: status={result_a['status']} gross={result_a['realized_return_gross']} net={result_a['realized_return_net']}"
        )
        print(
            f"  Path B: status={result_b['status']} gross={result_b['realized_return_gross']} net={result_b['realized_return_net']}"
        )

        all_results.append(
            {
                "fixture_name": fixture["name"],
                "description": fixture["description"],
                "path_a": result_a,
                "path_b": result_b,
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
    (REPORT_DIR / "replay_results.json").write_text(
        json.dumps(all_results, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    # Save normalized CSV
    csv_path = REPORT_DIR / "replay_results_normalized.csv"
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
            for path_key, path_label in [("path_a", "A"), ("path_b", "B")]:
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

    # Save divergence report
    (REPORT_DIR / "divergence_report.json").write_text(
        json.dumps(all_divergences, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    # Summary
    classification_counts: dict[str, int] = {}
    for d in all_divergences:
        c = d["classification"]
        classification_counts[c] = classification_counts.get(c, 0) + 1

    print("=" * 60)
    print("DIVERGENCE SUMMARY")
    print("=" * 60)
    print(f"Total fixtures: {len(fixtures)}")
    print(f"Total comparisons: {len(fixtures) * len(COMPARE_FIELDS)}")
    print(f"Total divergences: {len(all_divergences)}")
    print()
    for c, count in sorted(classification_counts.items()):
        print(f"  {c}: {count}")
    print()

    # Cleanup
    import shutil

    shutil.rmtree(tmp_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
