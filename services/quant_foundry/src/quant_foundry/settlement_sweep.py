"""
quant_foundry.settlement_sweep — periodic settlement worker (Agent A).

Sweeps the shadow prediction ledger and settles every prediction whose
horizon has elapsed. The sweep is idempotent — ``SettlementLedger.settle``
returns the existing record for a ``(prediction_id, cost_model_version)``
pair without duplicating it, so reruns are safe.

Design:
- Lists all shadow predictions from ``ShadowLedger.list()``.
- Filters to predictions where ``ts_event + horizon_ns <= now_ns``
  (horizon expired). Not-yet-expired predictions are counted as
  ``pending_time``.
- Fetches prices for each prediction's symbol over the decision window
  via the ``BarDataAdapter``. Missing data causes the settlement ledger
  to produce ``PENDING_DATA``.
- Settles each prediction via ``SettlementLedger.settle()``.
- Returns a ``SweepReceipt`` with settled / pending_time / pending_data
  / failed counts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from quant_foundry.market_data_adapter import BarDataAdapter, PricePoint
from quant_foundry.metrics import PriceTick
from quant_foundry.outcomes import CostModel, SettlementRecord, SettlementStatus
from quant_foundry.settlement import SettlementLedger
from quant_foundry.shadow_ledger import ShadowLedger


def default_cost_model() -> CostModel:
    """Default cost model for the settlement sweep."""
    return CostModel(
        version="cm-v1",
        fee_bps=10.0,
        spread_bps=5.0,
        slippage_bps=3.0,
        borrow_bps_per_day=25.0,
    )


@dataclass(frozen=True)
class SweepReceipt:
    """Result of a single ``SettlementSweep.sweep()`` call."""

    settled_count: int = 0
    pending_time_count: int = 0
    pending_data_count: int = 0
    failed_count: int = 0
    total: int = 0
    records: list[SettlementRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "settled_count": self.settled_count,
            "pending_time_count": self.pending_time_count,
            "pending_data_count": self.pending_data_count,
            "failed_count": self.failed_count,
            "total": self.total,
        }


class SettlementSweep:
    """Periodic sweep that settles expired shadow predictions.

    Idempotent: rerunning with the same inputs and cost-model version
    produces the same records without duplication.
    """

    def __init__(
        self,
        shadow_ledger: ShadowLedger,
        settlement_ledger: SettlementLedger,
        market_data_adapter: BarDataAdapter,
        cost_model: CostModel | None = None,
        benchmark_symbol: str = "SPY",
    ) -> None:
        self.shadow_ledger = shadow_ledger
        self.settlement_ledger = settlement_ledger
        self.market_data_adapter = market_data_adapter
        self.cost_model = cost_model or default_cost_model()
        self.benchmark_symbol = benchmark_symbol

    def sweep(self, now_ns: int | None = None) -> SweepReceipt:
        """Sweep all shadow predictions and settle expired ones.

        Returns a ``SweepReceipt`` with settled / pending_time /
        pending_data / failed counts.
        """
        if now_ns is None:
            now_ns = time.time_ns()

        predictions = self.shadow_ledger.list()
        settled_count = 0
        pending_time_count = 0
        pending_data_count = 0
        failed_count = 0
        records: list[SettlementRecord] = []

        for pred in predictions:
            window_end = pred.ts_event + pred.horizon_ns

            if now_ns < window_end:
                pending_time_count += 1
                continue

            fetch_end = window_end + 1
            prices = self.market_data_adapter.get_prices(
                pred.symbol,
                pred.ts_event,
                fetch_end,
            )
            benchmark_prices = self.market_data_adapter.get_benchmark_prices(
                pred.ts_event,
                fetch_end,
            )

            prediction_dict: dict[str, Any] = {
                "prediction_id": pred.prediction_id,
                "model_id": pred.model_id,
                "symbol": pred.symbol,
                "ts_event": pred.ts_event,
                "horizon_ns": pred.horizon_ns,
                "direction": pred.direction,
                "confidence": pred.confidence,
                "p_up": pred.p_up if pred.p_up is not None else pred.confidence,
            }

            price_ticks = _to_price_ticks(prices)
            benchmark_ticks = _to_price_ticks(benchmark_prices) or None

            try:
                record = self.settlement_ledger.settle(
                    prediction=prediction_dict,
                    prices=price_ticks,
                    benchmark_prices=benchmark_ticks,
                    cost_model=self.cost_model,
                    now_ns=now_ns,
                )
            except Exception:
                failed_count += 1
                continue

            records.append(record)

            if record.status == SettlementStatus.SETTLED:
                settled_count += 1
            elif record.status == SettlementStatus.PENDING_DATA:
                pending_data_count += 1
            elif record.status == SettlementStatus.PENDING_TIME:
                pending_time_count += 1
            else:
                failed_count += 1

        return SweepReceipt(
            settled_count=settled_count,
            pending_time_count=pending_time_count,
            pending_data_count=pending_data_count,
            failed_count=failed_count,
            total=len(predictions),
            records=records,
        )


def _to_price_ticks(points: list[PricePoint]) -> list[PriceTick]:
    """Convert adapter ``PricePoint`` to ``metrics.PriceTick``."""
    return [PriceTick(ts=p.ts_ns, price=p.close) for p in points]
