from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import fakeredis.aioredis

from fincept_core.config import Settings
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
    args = parser.parse_args()
    receipt = asyncio.run(run_replay())
    output = write_receipt(receipt, args.output)
    print(json.dumps({"status": receipt["status"], "receipt": str(output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
