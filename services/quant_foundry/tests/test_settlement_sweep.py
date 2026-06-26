"""
Tests for quant_foundry.settlement_sweep (Agent A — settlement track).

Covers:
- Fixture predictions + fixture prices → deterministic settlement.
- Missing data → PENDING_DATA.
- Not-yet-expired predictions → PENDING_TIME.
- Rerun produces same records (idempotency).
"""

from __future__ import annotations

import pathlib

from quant_foundry.market_data_adapter import BarDataAdapter, PricePoint
from quant_foundry.outcomes import CostModel, SettlementStatus
from quant_foundry.settlement import SettlementLedger
from quant_foundry.settlement_sweep import SettlementSweep, default_cost_model
from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

_T_EVENT = 1_000_000_000_000_000_000  # 1e18 ns
_HORIZON_NS = 60_000_000_000  # 60 s
_WINDOW_END = _T_EVENT + _HORIZON_NS


def _make_prediction(
    *,
    prediction_id: str = "pred-1",
    model_id: str = "gbm-1",
    symbol: str = "AAPL",
    ts_event: int = _T_EVENT,
    horizon_ns: int = _HORIZON_NS,
    direction: float = 1.0,
    confidence: float = 0.7,
    p_up: float = 0.7,
) -> dict:
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


def _store_predictions(
    ledger: ShadowLedger,
    predictions: list[dict],
) -> None:
    batch_hash = compute_batch_hash(predictions)
    ledger.store_batch(predictions=predictions, batch_hash=batch_hash)


def _make_bar_reader(
    bars: dict[str, list[PricePoint]],
):
    def reader(symbol: str, start_ns: int, end_ns: int) -> list[PricePoint]:
        return [p for p in bars.get(symbol, []) if start_ns <= p.ts_ns < end_ns]

    return reader


def _make_sweep(
    tmp_path: pathlib.Path,
    bars: dict[str, list[PricePoint]],
    cost_model: CostModel | None = None,
    benchmark_symbol: str = "SPY",
) -> SettlementSweep:
    shadow = ShadowLedger(base_dir=tmp_path / "shadow")
    settlement = SettlementLedger(root=tmp_path / "settlements")
    adapter = BarDataAdapter(
        bar_reader=_make_bar_reader(bars),
        benchmark_symbol=benchmark_symbol,
    )
    return SettlementSweep(
        shadow_ledger=shadow,
        settlement_ledger=settlement,
        market_data_adapter=adapter,
        cost_model=cost_model,
        benchmark_symbol=benchmark_symbol,
    )


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestSettlementSweepDeterministic:
    def test_settled_prediction(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
            "SPY": [
                PricePoint(ts_ns=_T_EVENT, close=400.0),
                PricePoint(ts_ns=_WINDOW_END, close=401.0),
            ],
        }
        sweep = _make_sweep(tmp_path, bars)
        _store_predictions(sweep.shadow_ledger, [_make_prediction()])

        now_ns = _WINDOW_END + 1
        receipt = sweep.sweep(now_ns=now_ns)

        assert receipt.settled_count == 1
        assert receipt.pending_time_count == 0
        assert receipt.pending_data_count == 0
        assert receipt.failed_count == 0
        assert receipt.total == 1

        record = receipt.records[0]
        assert record.status == SettlementStatus.SETTLED
        assert record.prediction_id == "pred-1"
        assert record.realized_return_gross is not None
        assert record.realized_return_net is not None
        assert record.abnormal_return is not None

    def test_short_direction_settles(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=147.0),
            ],
        }
        sweep = _make_sweep(tmp_path, bars)
        pred = _make_prediction(direction=-1.0, p_up=0.3)
        _store_predictions(sweep.shadow_ledger, [pred])

        receipt = sweep.sweep(now_ns=_WINDOW_END + 1)
        assert receipt.settled_count == 1
        record = receipt.records[0]
        assert record.status == SettlementStatus.SETTLED
        assert record.realized_return_gross is not None
        assert record.realized_return_gross > 0  # short profits when price drops


class TestSettlementSweepPendingData:
    def test_missing_prices_pending_data(self, tmp_path: pathlib.Path) -> None:
        sweep = _make_sweep(tmp_path, {})
        _store_predictions(sweep.shadow_ledger, [_make_prediction()])

        receipt = sweep.sweep(now_ns=_WINDOW_END + 1)
        assert receipt.pending_data_count == 1
        assert receipt.settled_count == 0
        record = receipt.records[0]
        assert record.status == SettlementStatus.PENDING_DATA
        assert record.realized_return_gross is None

    def test_partial_prices_pending_data(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
            ],
        }
        sweep = _make_sweep(tmp_path, bars)
        _store_predictions(sweep.shadow_ledger, [_make_prediction()])

        receipt = sweep.sweep(now_ns=_WINDOW_END + 1)
        assert receipt.pending_data_count == 1
        assert receipt.settled_count == 0


class TestSettlementSweepPendingTime:
    def test_not_yet_expired_pending_time(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        sweep = _make_sweep(tmp_path, bars)
        _store_predictions(sweep.shadow_ledger, [_make_prediction()])

        receipt = sweep.sweep(now_ns=_T_EVENT + 1)
        assert receipt.pending_time_count == 1
        assert receipt.settled_count == 0
        assert receipt.pending_data_count == 0
        assert receipt.records == []


class TestSettlementSweepIdempotency:
    def test_rerun_produces_same_records(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
            "SPY": [
                PricePoint(ts_ns=_T_EVENT, close=400.0),
                PricePoint(ts_ns=_WINDOW_END, close=401.0),
            ],
        }
        sweep = _make_sweep(tmp_path, bars)
        _store_predictions(sweep.shadow_ledger, [_make_prediction()])

        now_ns = _WINDOW_END + 1
        receipt1 = sweep.sweep(now_ns=now_ns)
        receipt2 = sweep.sweep(now_ns=now_ns)

        assert receipt1.settled_count == 1
        assert receipt2.settled_count == 1

        r1 = receipt1.records[0]
        r2 = receipt2.records[0]
        assert r1.prediction_id == r2.prediction_id
        assert r1.status == r2.status
        assert r1.realized_return_gross == r2.realized_return_gross
        assert r1.realized_return_net == r2.realized_return_net

        all_records = sweep.settlement_ledger.read_all()
        assert len(all_records) == 1


class TestSettlementSweepMultiplePredictions:
    def test_mixed_statuses(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
            "MSFT": [],
        }
        sweep = _make_sweep(tmp_path, bars)
        preds = [
            _make_prediction(prediction_id="pred-1", symbol="AAPL"),
            _make_prediction(prediction_id="pred-2", symbol="MSFT"),
            _make_prediction(
                prediction_id="pred-3",
                symbol="AAPL",
                ts_event=_WINDOW_END + 1,
                horizon_ns=_HORIZON_NS,
            ),
        ]
        _store_predictions(sweep.shadow_ledger, preds)

        now_ns = _WINDOW_END + 1
        receipt = sweep.sweep(now_ns=now_ns)
        assert receipt.settled_count == 1
        assert receipt.pending_data_count == 1
        assert receipt.pending_time_count == 1
        assert receipt.total == 3


class TestDefaultCostModel:
    def test_default_values(self) -> None:
        cm = default_cost_model()
        assert cm.fee_bps == 10.0
        assert cm.spread_bps == 5.0
        assert cm.slippage_bps == 3.0
        assert cm.borrow_bps_per_day == 25.0
        assert cm.version == "cm-v1"
