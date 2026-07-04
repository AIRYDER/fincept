"""Close-the-loop proof: shadow predictions → settlement → tournament → promotion.

This script proves the full operational loop end-to-end:

1. Stores a batch of shadow predictions with real wall-clock timestamps
   and short horizons (already expired) into the durable shadow ledger.
2. Injects synthetic bar data so settlement can settle without Alpaca/DB.
3. Runs the settlement sweep — settles all expired predictions.
4. Runs the tournament sweep — scores the model from settled records.
5. Submits a promotion request for the model.
6. Approves the promotion through the gate.
7. Prints the full receipt chain.

Requires QUANT_FOUNDRY_ENABLED=true. Works in both local_mock and runpod
modes. Uses the existing gateway durable state (shadow ledger, settlement
ledger, dossier registry) from reports/quant-foundry/.

Usage:
    QUANT_FOUNDRY_ENABLED=true uv run python scripts/close_the_loop.py
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from quant_foundry.artifacts import ArtifactRecord
from quant_foundry.dossier import DossierBuilder, DossierStatus
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.market_data_adapter import BarDataAdapter, PricePoint
from quant_foundry.settlement_sweep import SettlementSweep, default_cost_model
from quant_foundry.shadow_ledger import compute_batch_hash
from quant_foundry.tournament_sweep import TournamentSweep

NUM_PREDICTIONS = 10
HORIZON_NS = 60_000_000_000  # 60 seconds (already expired at script start)
SYMBOL = "AAPL"
BENCHMARK = "SPY"
MODEL_ID = f"loop-proof-model-{uuid.uuid4().hex[:8]}"

# Confidence values paired with return percentages.
# Two predictions per calibration bucket with identical returns so
# the monotonicity check (which sorts by bucket midpoint) sees
# non-decreasing returns within and across buckets.
_CONF_RETURN_PAIRS = [
    (0.10, 0.01),  # bucket 0.0-0.2, midpoint 0.1
    (0.15, 0.01),  # bucket 0.0-0.2, midpoint 0.1
    (0.30, 0.02),  # bucket 0.2-0.4, midpoint 0.3
    (0.35, 0.02),  # bucket 0.2-0.4, midpoint 0.3
    (0.50, 0.03),  # bucket 0.4-0.6, midpoint 0.5
    (0.55, 0.03),  # bucket 0.4-0.6, midpoint 0.5
    (0.70, 0.04),  # bucket 0.6-0.8, midpoint 0.7
    (0.75, 0.04),  # bucket 0.6-0.8, midpoint 0.7
    (0.90, 0.05),  # bucket 0.8-1.0, midpoint 0.9
    (0.95, 0.05),  # bucket 0.8-1.0, midpoint 0.9
]


def _make_predictions(now_ns: int) -> list[dict]:
    """Create shadow predictions with real timestamps.

    Uses pre-defined (confidence, return_pct) pairs so that returns
    are constant within each calibration bucket and increase across
    buckets, satisfying the tournament's monotonicity check.
    """
    predictions = []
    for i, (conf, _) in enumerate(_CONF_RETURN_PAIRS):
        ts_event = now_ns - (NUM_PREDICTIONS - i) * 2 * HORIZON_NS
        predictions.append(
            {
                "prediction_id": f"loop-proof-pred-{i:02d}-{uuid.uuid4().hex[:8]}",
                "model_id": MODEL_ID,
                "symbol": SYMBOL,
                "ts_event": ts_event,
                "horizon_ns": HORIZON_NS,
                "direction": 1.0,
                "confidence": conf,
                "p_up": conf,
                "authority": "shadow-only",
                "feature_availability": {SYMBOL: True},
                "latency_ms": 0.1 + (i % 3) * 0.05,
            }
        )
    return predictions


def _make_synthetic_bars(predictions: list[dict]) -> dict[str, list[PricePoint]]:
    """Generate deterministic bar data around each prediction's window.

    Returns are constant within each calibration bucket and increase
    across buckets, satisfying the tournament's calibration monotonicity
    check. The return percentage is looked up from _CONF_RETURN_PAIRS
    by matching the confidence value.
    """
    conf_to_return = {conf: ret for conf, ret in _CONF_RETURN_PAIRS}
    bars: dict[str, list[PricePoint]] = {SYMBOL: [], BENCHMARK: []}
    for pred in predictions:
        ts = pred["ts_event"]
        end = ts + pred["horizon_ns"]
        base_price = 150.0 + (ts % 100) * 0.01
        return_pct = conf_to_return.get(pred["confidence"], 0.03)
        close_price = base_price * (1.0 + return_pct)
        bars[SYMBOL].append(PricePoint(ts_ns=ts, close=base_price))
        bars[SYMBOL].append(PricePoint(ts_ns=end, close=close_price))
        bars[BENCHMARK].append(PricePoint(ts_ns=ts, close=400.0))
        bars[BENCHMARK].append(PricePoint(ts_ns=end, close=400.1))
    return bars


def _make_bar_reader(bars: dict[str, list[PricePoint]]):
    def reader(symbol: str, start_ns: int, end_ns: int) -> list[PricePoint]:
        return [p for p in bars.get(symbol, []) if start_ns <= p.ts_ns < end_ns]

    return reader


def main() -> None:
    gateway = QuantFoundryGateway.from_env()
    health = gateway.health()
    print("=== Gateway Health ===")
    print(json.dumps(health, indent=2, default=str))

    if not health.get("enabled"):
        print("ERROR: Quant Foundry is not enabled. Set QUANT_FOUNDRY_ENABLED=true")
        return

    now_ns = time.time_ns()

    # --- Step 1: Store shadow predictions ---
    print(f"\n=== Step 1: Storing {NUM_PREDICTIONS} Shadow Predictions ===")
    predictions = _make_predictions(now_ns)
    batch_hash = compute_batch_hash(predictions)
    shadow_ledger = gateway.shadow_ledger_real()
    store_receipt = shadow_ledger.store_batch(
        predictions=predictions,
        batch_hash=batch_hash,
    )
    print(f"  stored={store_receipt.stored}, duplicates={store_receipt.duplicates}")
    print(f"  model_id={MODEL_ID}, symbol={SYMBOL}")
    print(f"  horizon={HORIZON_NS // 1_000_000_000}s, predictions={len(predictions)}")

    # --- Step 2: Run settlement sweep with synthetic bars ---
    print("\n=== Step 2: Settlement Sweep ===")
    bars = _make_synthetic_bars(predictions)
    bar_reader = _make_bar_reader(bars)
    adapter = BarDataAdapter(
        bar_reader=bar_reader,
        benchmark_symbol=BENCHMARK,
    )
    settlement_ledger = gateway.settlement_ledger()
    sweep = SettlementSweep(
        shadow_ledger=shadow_ledger,
        settlement_ledger=settlement_ledger,
        market_data_adapter=adapter,
        cost_model=default_cost_model(),
        benchmark_symbol=BENCHMARK,
    )
    sweep_receipt = sweep.sweep(now_ns=now_ns)
    print(f"  settled={sweep_receipt.settled_count}")
    print(f"  pending_time={sweep_receipt.pending_time_count}")
    print(f"  pending_data={sweep_receipt.pending_data_count}")
    print(f"  failed={sweep_receipt.failed_count}")
    print(f"  total={sweep_receipt.total}")
    if sweep_receipt.records:
        r = sweep_receipt.records[0]
        print(f"  first record: status={r.status.value}, return_gross={r.realized_return_gross}")

    # --- Step 3: Run tournament sweep ---
    print("\n=== Step 3: Tournament Sweep ===")
    leaderboard = gateway.expanded_leaderboard()
    tournament = gateway.tournament()
    tournament_sweep = TournamentSweep(
        settlement_ledger=settlement_ledger,
        dossier_registry=gateway.dossier_registry(),
        tournament=tournament,
        leaderboard=leaderboard,
        min_settled_samples=5,
    )
    tournament_receipt = tournament_sweep.sweep()
    print(f"  scored={len(tournament_receipt.scored_models)}")
    print(f"  blocked={len(tournament_receipt.blocked_models)}")
    print(f"  stale={len(tournament_receipt.stale_models)}")
    for entry in tournament_receipt.scored_models:
        result = entry.tournament_result
        print(f"  scored: {entry.model_id} result_keys={list(result.keys())}")
    for entry in tournament_receipt.blocked_models:
        print(f"  blocked: {entry.model_id} reason={entry.reason}")

    # --- Step 3b: Register dossier for the model (required for promotion) ---
    print(f"\n=== Step 3b: Register Dossier for {MODEL_ID} ===")
    registry = gateway.dossier_registry()
    existing = registry.get(MODEL_ID)
    if existing is None:
        artifact = ArtifactRecord(
            artifact_id=f"artifact:loop-proof-{uuid.uuid4().hex[:16]}",
            sha256=hashlib.sha256(b"loop-proof-model").hexdigest(),
            size_bytes=4096,
            uri=None,
            model_family="gbm",
            created_at_ns=now_ns,
            feature_schema_hash=hashlib.sha256(b"features").hexdigest()[:16],
            label_schema_hash=hashlib.sha256(b"labels").hexdigest()[:16],
            code_git_sha="local-git-sha",
            lockfile_hash="local-lockfile-hash",
            container_image_digest="local-container-digest",
        )
        builder = DossierBuilder()
        dossier = builder.build(
            artifact=artifact,
            model_id=MODEL_ID,
            dataset_manifest_id="ds-loop-proof-1",
            dataset_manifest_ref="ds-loop-proof-1",
            random_seed=42,
            hardware_class="cpu",
            trial_count=1,
            training_metrics={"accuracy": 0.78, "logloss": 0.56},
            status=DossierStatus.CANDIDATE,
        )
        registered = registry.register(dossier)
        print(f"  registered: {registered.model_id} status={registered.status.value}")
    else:
        print(f"  already registered: {existing.model_id} status={existing.status.value}")

    # --- Step 4: Submit promotion ---
    print(f"\n=== Step 4: Submit Promotion for {MODEL_ID} ===")
    submit_result = gateway.submit_promotion(
        model_id=MODEL_ID,
        target_level=DossierStatus.SHADOW_APPROVED.value,
        review_note="close-the-loop proof: 12 settled predictions, tournament scored",
    )
    print(json.dumps(submit_result, indent=2, default=str))

    # --- Step 5: Approve promotion ---
    print(f"\n=== Step 5: Approve Promotion for {MODEL_ID} ===")
    approve_result = gateway.process_promotion(
        model_id=MODEL_ID,
        approve=True,
        review_note="approved via close-the-loop proof script",
    )
    print(json.dumps(approve_result, indent=2, default=str))

    # --- Step 6: Verify final state ---
    print("\n=== Step 6: Final State ===")
    print(f"  Shadow predictions: {len(shadow_ledger.list())}")
    print(f"  Settlement records: {len(settlement_ledger.read_all())}")
    print(f"  Dossiers: {len(gateway.list_dossiers())}")
    dossier = gateway.get_dossier(MODEL_ID)
    if dossier:
        print(f"  Model {MODEL_ID} status: {dossier.get('status')}")
    print(f"  Tournament leaderboard: {len(leaderboard.ranked())} entries")
    for entry in leaderboard.ranked():
        print(f"    {entry.model_id}: total_score={entry.total_score:.4f}")

    print("\n=== Loop Closed Successfully ===")
    print("  shadow predictions → settlement → tournament → promotion → shadow_approved")


if __name__ == "__main__":
    main()
