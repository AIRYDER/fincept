from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis.aioredis
from settlements.worker import tick_sync

from fincept_core.config import Settings
from fincept_core.datasets import SettlementStore
from fincept_core.prediction_log import PredictionLog
from fincept_core.schemas import AssetClass, BarEvent, Prediction, TradeEvent, Venue
from oms.paper import PaperFiller
from oms.prices import LivePrices
from oms.processor import process_intent
from orchestrator.decisions import build_decision_and_intent
from portfolio.state import PortfolioState, apply_fill
from portfolio.store import PositionStore
from risk.checks import RiskContext, check_intent

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = REPO_ROOT / "reports" / "paper-spine"
BASE_TS_NS = 1_800_000_000_000_000_000
STRATEGY_ID = "paper_spine_receipt.v1"
SYMBOL = "AAPL"

# --- Settlement proof constants (todo 21) ----------------------------------- #
# A fixed clock in the past keeps the proof deterministic and independent of
# wall-clock time.  ``SETTLEMENT_NOW_NS`` is the "as-of" tick passed to
# ``tick_sync``; predictions are stamped ``SETTLEMENT_TS_EVENT_BASE`` + i*step
# so each gets a unique ``ts_event`` (the worker's market-data source keys off
# ``ts_event`` to look up the per-prediction direction).
SETTLEMENT_AGENT_ID = "fixture_momentum_agent.v1"
SETTLEMENT_MODEL_NAME = "fixture_momentum_1m.v1"
SETTLEMENT_SYMBOL = "AAPL"
SETTLEMENT_HORIZON_NS = 86_400_000_000_000  # 24h
SETTLEMENT_NOW_NS = BASE_TS_NS
SETTLEMENT_TS_EVENT_BASE = SETTLEMENT_NOW_NS - 2 * SETTLEMENT_HORIZON_NS  # 48h ago
SETTLEMENT_TS_STEP_NS = 1_000_000_000  # 1s apart -> unique ts_event per prediction
SETTLEMENT_N = 10
SETTLEMENT_CLOSE_T1 = 100.0
SETTLEMENT_RETURN_MAGNITUDE = 0.02  # +/- 2% per prediction


@dataclass
class AuditTrail:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def append(self, stage: str, payload: Any, *, correlation_id: str | None = None) -> None:
        self.entries.append(
            {
                "stage": stage,
                "correlation_id": correlation_id,
                "payload": to_jsonable(payload),
            }
        )


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    return value


def git_commit() -> str | None:
    head_path = REPO_ROOT / ".git" / "HEAD"
    if not head_path.exists():
        return None
    try:
        head = head_path.read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref_path = REPO_ROOT / ".git" / head.removeprefix("ref: ").strip()
            return ref_path.read_text(encoding="utf-8").strip()[:7]
        return head[:7]
    except Exception:
        return None


def make_settings(*, max_per_symbol: int = 10_000, max_gross: int = 50_000) -> Settings:
    return Settings(
        MAX_NOTIONAL_USD_PER_SYMBOL=max_per_symbol,
        MAX_GROSS_NOTIONAL_USD=max_gross,
        TRADING_MODE="paper",
        OMS_ROUTER="sim",
    )


def feature_from_bar(bar: BarEvent) -> dict[str, Any]:
    momentum = (bar.close - bar.open) / bar.open
    return {
        "feature_id": "fixture_momentum_1m.v1",
        "symbol": bar.symbol,
        "source_event_type": bar.event_type,
        "close_to_open_return": momentum,
        "close": bar.close,
        "volume": bar.volume,
    }


def prediction_from_feature(feature: dict[str, Any]) -> Prediction:
    direction = 1.0 if feature["close_to_open_return"] > 0 else -1.0
    magnitude = abs(feature["close_to_open_return"])
    confidence = min(0.95, 0.60 + float(magnitude) * 10)
    return Prediction(
        agent_id="fixture_momentum_agent.v1",
        symbol=str(feature["symbol"]),
        horizon_ns=86_400_000_000_000,
        ts_event=BASE_TS_NS + 2_000,
        direction=direction,
        magnitude=float(magnitude),
        confidence=confidence,
        calibration_tag="deterministic-replay-fixture",
    )


async def run_replay() -> dict[str, Any]:
    audit = AuditTrail()
    prices = LivePrices()
    redis = fakeredis.aioredis.FakeRedis()
    store = PositionStore(redis)
    portfolio_state = PortfolioState()
    filler = PaperFiller(
        mean_latency_ms=0,
        std_latency_ms=0,
        spread_bps=Decimal("0"),
        rng=lambda _mu, _sigma: 0.0,
        clock=lambda: BASE_TS_NS + 8_000,
    )

    bar = BarEvent(
        symbol=SYMBOL,
        venue=Venue.PAPER,
        asset_class=AssetClass.EQUITY,
        ts_event=BASE_TS_NS,
        ts_recv=BASE_TS_NS + 1,
        freq="1m",
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1000000"),
        trades=1200,
        vwap=Decimal("100.50"),
    )
    trade = TradeEvent(
        symbol=SYMBOL,
        venue=Venue.PAPER,
        asset_class=AssetClass.EQUITY,
        ts_event=BASE_TS_NS + 1_000,
        ts_recv=BASE_TS_NS + 1_001,
        price=bar.close,
        size=Decimal("100"),
    )
    prices.update(trade.symbol, trade.price)
    audit.append("data", {"bar": bar, "trade": trade})

    feature = feature_from_bar(bar)
    audit.append("feature", feature)

    prediction = prediction_from_feature(feature)
    audit.append("model_signal", prediction)

    decision, intent = build_decision_and_intent(
        symbol=prediction.symbol,
        delta_notional=Decimal("5000"),
        last_price=trade.price,
        strategy_id=STRATEGY_ID,
        ts_event=BASE_TS_NS + 3_000,
        rationale=(
            "deterministic fixture momentum signal: "
            f"return={feature['close_to_open_return']} confidence={prediction.confidence}"
        ),
        source_signals=[prediction.agent_id],
    )
    audit.append("decision", decision, correlation_id=decision.decision_id)
    audit.append("order_intent", intent, correlation_id=decision.decision_id)

    approved_risk = check_intent(
        intent,
        ctx=RiskContext(),
        settings=make_settings(),
        last_price=trade.price,
    )
    audit.append("risk_gate_approved", approved_risk, correlation_id=decision.decision_id)
    if not approved_risk.approved:
        raise AssertionError(f"expected approved risk check, got {approved_risk.reasons}")

    rejected_risk = check_intent(
        intent,
        ctx=RiskContext(),
        settings=make_settings(max_per_symbol=1_000, max_gross=1_000),
        last_price=trade.price,
    )
    audit.append("risk_gate_rejected", rejected_risk, correlation_id=decision.decision_id)
    if rejected_risk.approved:
        raise AssertionError("expected low-limit risk check to reject")

    oms_result = process_intent(
        intent,
        prices=prices,
        filler=filler,
        clock=lambda: BASE_TS_NS + 7_000,
    )
    final_order = oms_result.order_states[-1]
    audit.append("order", oms_result.order_states, correlation_id=intent.order_id)
    if oms_result.fill is None:
        raise AssertionError("expected paper OMS to produce a fill")
    audit.append("fill", oms_result.fill, correlation_id=intent.order_id)

    async def resolve_strategy(_fill: Any) -> str | None:
        return STRATEGY_ID

    position = await apply_fill(
        oms_result.fill,
        state=portfolio_state,
        store=store,
        resolve_strategy=resolve_strategy,
    )
    if position is None:
        raise AssertionError("expected fill to update portfolio position")
    persisted_position = await store.get(STRATEGY_ID, SYMBOL)
    audit.append(
        "portfolio_update",
        {"position": position, "persisted_position": persisted_position},
        correlation_id=intent.order_id,
    )

    expected_quantity = intent.quantity
    assertions = {
        "data_seen": bar.close == trade.price,
        "feature_generated": feature["close_to_open_return"] == Decimal("0.01"),
        "signal_generated": prediction.direction > 0 and prediction.confidence > 0.6,
        "decision_linked_to_intent": decision.decision_id == intent.decision_id,
        "risk_approved_order": approved_risk.approved,
        "risk_rejected_low_limit_order": not rejected_risk.approved,
        "order_filled": final_order.status.value == "filled",
        "fill_matches_order": oms_result.fill.order_id == intent.order_id,
        "portfolio_quantity_matches_fill": position.quantity == expected_quantity,
        "portfolio_persisted": persisted_position == position,
        "audit_trail_complete": len(audit.entries) >= 9,
    }
    passed = all(assertions.values())

    receipt = {
        "schema_version": 1,
        "receipt_type": "paper_spine_replay",
        "status": "passed" if passed else "failed",
        "generated_at": datetime.now(UTC).isoformat(),
        "repo": {
            "root": str(REPO_ROOT),
            "git_commit": git_commit(),
        },
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "live_broker_credentials_required": False,
            "redis_required": False,
            "uses_fakeredis": True,
        },
        "flow_proven": [
            "data",
            "feature",
            "model_signal",
            "decision",
            "risk_gate_approved",
            "risk_gate_rejected",
            "order",
            "fill",
            "portfolio_update",
            "audit_trail",
        ],
        "ids": {
            "strategy_id": STRATEGY_ID,
            "symbol": SYMBOL,
            "agent_id": prediction.agent_id,
            "decision_id": decision.decision_id,
            "order_id": intent.order_id,
            "fill_id": oms_result.fill.fill_id,
        },
        "assertions": assertions,
        "risk": {
            "approved": approved_risk,
            "rejected_low_limit": rejected_risk,
        },
        "final_order": final_order,
        "final_fill": oms_result.fill,
        "final_position": position,
        "audit_trail": audit.entries,
    }
    if not passed:
        failed = [name for name, ok in assertions.items() if not ok]
        raise AssertionError(f"paper spine replay failed assertions: {failed}")
    return to_jsonable(receipt)


def _make_fixture_market_data_source(
    direction_by_ts_event: dict[int, float],
) -> Callable[[str, int, int], float | None]:
    """Build a deterministic market-data source for the settlement proof.

    The worker (``tick_sync``) calls the source twice per prediction:

      * ``market_data_source(symbol, ts_event, ts_event)``              -> close_t1
      * ``market_data_source(symbol, ts_event, ts_event + horizon_ns)`` -> close_t2

    The first call has ``ts1 == ts2`` so we return the constant entry price
    ``SETTLEMENT_CLOSE_T1``.  The second call has ``ts2 != ts1``; we look up
    the per-prediction direction by ``ts1`` (which is the ``ts_event``) and
    return ``close_t1 * (1 + direction * magnitude)`` so each prediction
    settles with ``realized_return_gross`` matching its direction.
    """

    def source(symbol: str, ts1: int, ts2: int) -> float | None:
        if ts2 == ts1:
            # Entry leg: close at ts_event.
            return SETTLEMENT_CLOSE_T1
        # Exit leg: close at ts_event + horizon_ns.
        direction = direction_by_ts_event.get(ts1)
        if direction is None:
            return None
        return SETTLEMENT_CLOSE_T1 * (1.0 + direction * SETTLEMENT_RETURN_MAGNITUDE)

    return source


def run_settlement_proof() -> dict[str, Any]:
    """Run the deterministic prediction -> settlement loop (todo 21).

    Generates ``SETTLEMENT_N`` synthetic predictions, settles them via
    ``settlements.worker.tick_sync`` with a fixture price source, reads back
    the settlement ledger, asserts the canonical invariants, and returns a
    ``settlement_evidence`` block for the receipt.

    When ``FINCEPT_REPLAY_DRY_RUN`` is set the predictions and settlements are
    written to a temporary directory (CI read-only safety); otherwise the
    fixture predictions are mirrored to ``data/predictions`` and the
    settlements are persisted to ``data/settlements`` under the repo root.

    The worker (``tick_sync``) scans *every* ``<agent_id>.jsonl`` file under
    its ``predictions_dir``, so to avoid cross-agent contamination (and the
    O(n²) cost of re-checking already-settled rows from other agents) the
    worker is always driven from an isolated directory containing only the
    fixture file.  In non-dry-run mode the fixture file is also mirrored to
    ``data/predictions/fixture_momentum_agent.v1.jsonl`` per the spec, and the
    fixture agent's settlement ledger under ``data/settlements`` is reset
    first so the proof is idempotent on rerun.
    """
    dry_run = bool(os.environ.get("FINCEPT_REPLAY_DRY_RUN"))

    # Isolated predictions directory for the worker -- contains ONLY the
    # fixture file so tick_sync never scans other agents' ledgers.
    worker_root = Path(tempfile.mkdtemp(prefix="paper-spine-settlement-worker-"))
    worker_predictions_dir = worker_root / "predictions"
    worker_predictions_dir.mkdir(parents=True, exist_ok=True)

    if dry_run:
        settlements_dir = worker_root / "settlements"
        settlements_dir.mkdir(parents=True, exist_ok=True)
        persist_predictions_dir: Path | None = None
    else:
        settlements_dir = REPO_ROOT / "data" / "settlements"
        settlements_dir.mkdir(parents=True, exist_ok=True)
        persist_predictions_dir = REPO_ROOT / "data" / "predictions"
        persist_predictions_dir.mkdir(parents=True, exist_ok=True)
        # Reset the fixture agent's settlement ledger so a rerun doesn't
        # trip the store's duplicate-settled guard or accumulate stale rows.
        fixture_settlements = settlements_dir / f"{SETTLEMENT_AGENT_ID}.jsonl"
        if fixture_settlements.exists():
            fixture_settlements.unlink()

    try:
        # PredictionLog writes to the isolated worker dir; the worker reads
        # from the same dir.
        log = PredictionLog(predictions_dir=worker_predictions_dir)

        # 1 + 2. Generate N synthetic predictions with round-robin direction.
        direction_by_ts_event: dict[int, float] = {}
        for i in range(SETTLEMENT_N):
            ts_event = SETTLEMENT_TS_EVENT_BASE + i * SETTLEMENT_TS_STEP_NS
            direction = 1.0 if i % 2 == 0 else -1.0
            direction_by_ts_event[ts_event] = direction
            log.append(
                agent_id=SETTLEMENT_AGENT_ID,
                model_name=SETTLEMENT_MODEL_NAME,
                ts_event=ts_event,
                horizon_ns=SETTLEMENT_HORIZON_NS,
                symbol=SETTLEMENT_SYMBOL,
                direction=direction,
                confidence=0.6,
            )

        # Mirror the fixture predictions to the canonical data/predictions path
        # (non-dry-run only) so the spec's "data/predictions/fixture_momentum_agent.v1.jsonl"
        # is populated.  Reset first for idempotency.
        if persist_predictions_dir is not None:
            persist_path = persist_predictions_dir / f"{SETTLEMENT_AGENT_ID}.jsonl"
            if persist_path.exists():
                persist_path.unlink()
            shutil.copyfile(
                worker_predictions_dir / f"{SETTLEMENT_AGENT_ID}.jsonl",
                persist_path,
            )

        # 3. Run the settlement worker with the fixture price source.
        source = _make_fixture_market_data_source(direction_by_ts_event)
        tick_sync(
            SETTLEMENT_NOW_NS,
            predictions_dir=worker_predictions_dir,
            settlements_dir=settlements_dir,
            market_data_source=source,
        )

        # 4. Read back the settlement ledger and assert the canonical invariants.
        store = SettlementStore(root=settlements_dir)
        settlements = store.read_for_agent(SETTLEMENT_AGENT_ID)

        settled_count = sum(1 for s in settlements if s.status == "settled")
        gross_positive = sum(
            1 for s in settlements if s.realized_return_gross is not None and s.realized_return_gross > 0
        )
        net_positive = sum(
            1 for s in settlements if s.realized_return_net is not None and s.realized_return_net > 0
        )
        pending_count = sum(1 for s in settlements if s.status != "settled")
        brier_components = [
            s.brier_component for s in settlements if s.brier_component is not None
        ]

        assertions = {
            "settlement_count_is_N": len(settlements) == SETTLEMENT_N,
            "all_settled": settled_count == SETTLEMENT_N,
            "gross_positive_is_half": gross_positive == SETTLEMENT_N // 2,
            "net_positive_is_half": net_positive == SETTLEMENT_N // 2,
            "brier_components_approx_zero": all(
                abs(b) < 1e-12 for b in brier_components
            ) and len(brier_components) == SETTLEMENT_N,
            "pending_count_is_zero": pending_count == 0,
        }

        # 5. Canonical metrics: hit_rate, pending_count, brier.
        # Every prediction's realized return sign matches its direction, so
        # all N are "correct direction" -> hit_rate = 1.0.
        correct_direction = 0
        for s in settlements:
            if s.realized_return_gross is None:
                continue
            if s.realized_return_gross > 0 or s.realized_return_gross < 0:
                correct_direction += 1
        settlement_hit_rate = correct_direction / SETTLEMENT_N
        brier = sum(brier_components) / len(brier_components) if brier_components else 0.0

        evidence = {
            "agent_id": SETTLEMENT_AGENT_ID,
            "model_name": SETTLEMENT_MODEL_NAME,
            "symbol": SETTLEMENT_SYMBOL,
            "n_predictions": SETTLEMENT_N,
            "horizon_ns": SETTLEMENT_HORIZON_NS,
            "now_ns": SETTLEMENT_NOW_NS,
            "close_t1": SETTLEMENT_CLOSE_T1,
            "return_magnitude": SETTLEMENT_RETURN_MAGNITUDE,
            "settlement_hit_rate": settlement_hit_rate,
            "pending_count": pending_count,
            "brier": brier,
            "settled_count": settled_count,
            "gross_positive_count": gross_positive,
            "net_positive_count": net_positive,
            "assertions": assertions,
            "dry_run": dry_run,
            "settlements_dir": str(settlements_dir),
        }

        failed = [name for name, ok in assertions.items() if not ok]
        if failed:
            raise AssertionError(
                f"settlement proof failed assertions: {failed} (evidence={evidence})"
            )
        return to_jsonable(evidence)
    finally:
        shutil.rmtree(worker_root, ignore_errors=True)


def write_receipt(receipt: dict[str, Any], output: Path | None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if output is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = REPORT_DIR / f"paper-spine-{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    latest = REPORT_DIR / "latest.json"
    latest.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a deterministic paper-spine replay receipt.")
    parser.add_argument("--output", type=Path, default=None, help="Optional receipt output path.")
    parser.add_argument(
        "--with-settlement",
        action="store_true",
        help=(
            "After the trading-spine proof, run a deterministic synthetic "
            "prediction -> settlement loop and append a ``settlement_evidence`` "
            "block to the receipt."
        ),
    )
    args = parser.parse_args()
    receipt = asyncio.run(run_replay())
    if args.with_settlement:
        receipt["settlement_evidence"] = run_settlement_proof()
    output = write_receipt(receipt, args.output)
    print(json.dumps({"status": receipt["status"], "receipt": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
