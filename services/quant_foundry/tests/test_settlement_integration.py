"""
Settlement integration test (Agent A — settlement track).

End-to-end test that verifies the full settlement loop:
1. Store shadow predictions in the shadow ledger.
2. Wait for the horizon to expire.
3. Run the settlement sweep.
4. Verify settlement records are persisted and correct.
5. Verify ``shadow_health()`` returns real metrics after sweep.
6. Verify settlement records can feed ``Tournament.score()`` (shape check
   against ``ScoringInput``).
"""

from __future__ import annotations

import pathlib

from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.market_data_adapter import BarDataAdapter, PricePoint
from quant_foundry.outcomes import SettlementStatus
from quant_foundry.settlement_sweep import SettlementSweep, default_cost_model
from quant_foundry.shadow_ledger import compute_batch_hash
from quant_foundry.tournament import ScoringInput, Tournament

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

_T_EVENT = 1_000_000_000_000_000_000
_HORIZON_NS = 60_000_000_000
_WINDOW_END = _T_EVENT + _HORIZON_NS


def _make_prediction(
    *,
    prediction_id: str,
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


def _make_bar_reader(bars: dict[str, list[PricePoint]]):
    def reader(symbol: str, start_ns: int, end_ns: int) -> list[PricePoint]:
        return [p for p in bars.get(symbol, []) if start_ns <= p.ts_ns < end_ns]

    return reader


def _make_gateway(tmp_path: pathlib.Path, bars: dict[str, list[PricePoint]]) -> QuantFoundryGateway:
    gw = QuantFoundryGateway(
        enabled=True,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-secret",
        base_dir=tmp_path,
    )
    adapter = BarDataAdapter(bar_reader=_make_bar_reader(bars))
    sweep = SettlementSweep(
        shadow_ledger=gw.shadow_ledger_real(),
        settlement_ledger=gw.settlement_ledger(),
        market_data_adapter=adapter,
        cost_model=default_cost_model(),
    )
    gw._settlement_sweep = sweep
    return gw


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestSettlementEndToEnd:
    """End-to-end: store → wait → sweep → verify."""

    def test_full_loop_single_prediction(self, tmp_path: pathlib.Path) -> None:
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
        gw = _make_gateway(tmp_path, bars)

        predictions = [_make_prediction(prediction_id="pred-e2e-1")]
        batch_hash = compute_batch_hash(predictions)
        gw.shadow_ledger_real().store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
        )

        ledger_records = gw.shadow_ledger_real().list()
        assert len(ledger_records) == 1
        assert ledger_records[0].prediction_id == "pred-e2e-1"

        receipt = gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)
        assert receipt["settled_count"] == 1
        assert receipt["total"] == 1

        records = gw.settlement_ledger().read_all()
        assert len(records) == 1
        record = records[0]
        assert record.status == SettlementStatus.SETTLED
        assert record.prediction_id == "pred-e2e-1"
        assert record.realized_return_gross is not None
        assert record.realized_return_net is not None
        assert record.abnormal_return is not None
        assert record.brier is not None
        assert record.calibration_bucket is not None
        assert record.cost_model_version == "cm-v1"

    def test_full_loop_multiple_predictions(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
            "MSFT": [
                PricePoint(ts_ns=_T_EVENT, close=300.0),
                PricePoint(ts_ns=_WINDOW_END, close=297.0),
            ],
            "SPY": [
                PricePoint(ts_ns=_T_EVENT, close=400.0),
                PricePoint(ts_ns=_WINDOW_END, close=401.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)

        predictions = [
            _make_prediction(prediction_id="pred-1", symbol="AAPL", direction=1.0),
            _make_prediction(prediction_id="pred-2", symbol="MSFT", direction=-1.0, p_up=0.3),
        ]
        batch_hash = compute_batch_hash(predictions)
        gw.shadow_ledger_real().store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
        )

        receipt = gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)
        assert receipt["settled_count"] == 2
        assert receipt["total"] == 2

        records = gw.settlement_ledger().read_all()
        assert len(records) == 2
        for r in records:
            assert r.status == SettlementStatus.SETTLED
            assert r.realized_return_gross is not None

    def test_pending_time_before_horizon(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)

        predictions = [_make_prediction(prediction_id="pred-pt-1")]
        batch_hash = compute_batch_hash(predictions)
        gw.shadow_ledger_real().store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
        )

        receipt = gw.run_settlement_sweep(now_ns=_T_EVENT + 1)
        assert receipt["pending_time_count"] == 1
        assert receipt["settled_count"] == 0

        receipt2 = gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)
        assert receipt2["settled_count"] == 1
        assert receipt2["pending_time_count"] == 0

    def test_pending_data_missing_prices(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path, {})

        predictions = [_make_prediction(prediction_id="pred-pd-1")]
        batch_hash = compute_batch_hash(predictions)
        gw.shadow_ledger_real().store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
        )

        receipt = gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)
        assert receipt["pending_data_count"] == 1
        assert receipt["settled_count"] == 0

        records = gw.settlement_ledger().read_all()
        assert len(records) == 1
        assert records[0].status == SettlementStatus.PENDING_DATA


class TestShadowHealthAfterSweep:
    """Verify shadow_health() returns real metrics after sweep."""

    def test_health_reflects_settled_count(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)

        predictions = [_make_prediction(prediction_id="pred-h-1")]
        batch_hash = compute_batch_hash(predictions)
        gw.shadow_ledger_real().store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
        )

        health_before = gw.shadow_health()
        assert health_before["enabled"] is True
        assert health_before["settled_count"] == 0
        assert health_before["settlement_lag_seconds"] is None
        assert health_before["prediction_count"] == 1

        gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)

        health_after = gw.shadow_health()
        assert health_after["settled_count"] == 1
        assert health_after["settlement_lag_seconds"] is not None
        assert health_after["prediction_count"] == 1


class TestSettlementFeedsTournament:
    """Verify settlement records can feed Tournament.score() via ScoringInput."""

    def test_settlement_record_to_scoring_input(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)

        predictions = [_make_prediction(prediction_id="pred-t-1")]
        batch_hash = compute_batch_hash(predictions)
        gw.shadow_ledger_real().store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
        )

        gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)

        records = gw.settlement_ledger().read_all()
        settled = [r for r in records if r.status == SettlementStatus.SETTLED]
        assert len(settled) == 1

        r = settled[0]
        assert r.realized_return_net is not None
        assert r.realized_return_gross is not None

        scoring_input = ScoringInput(
            model_id=r.model_id,
            oos_returns_net=[r.realized_return_net],
            oos_returns_gross=[r.realized_return_gross],
            oos_returns_baseline=[0.0],
            settled_count=1,
            last_settled_at_ns=r.settled_at_ns,
            brier=r.brier,
            cost_model_version=r.cost_model_version,
        )

        assert scoring_input.model_id == "gbm-1"
        assert len(scoring_input.oos_returns_net) == 1
        assert scoring_input.settled_count == 1
        assert scoring_input.cost_model_version == "cm-v1"

    def test_tournament_score_accepts_settlement_data(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)

        predictions = [_make_prediction(prediction_id="pred-ts-1")]
        batch_hash = compute_batch_hash(predictions)
        gw.shadow_ledger_real().store_batch(
            predictions=predictions,
            batch_hash=batch_hash,
        )

        gw.run_settlement_sweep(now_ns=_WINDOW_END + 1)

        records = gw.settlement_ledger().read_all()
        settled = [r for r in records if r.status == SettlementStatus.SETTLED]
        assert len(settled) == 1

        r = settled[0]
        scoring_input = ScoringInput(
            model_id=r.model_id,
            oos_returns_net=[r.realized_return_net],
            oos_returns_gross=[r.realized_return_gross],
            oos_returns_baseline=[0.0],
            settled_count=1,
            min_settled_samples=1,
            last_settled_at_ns=r.settled_at_ns,
            brier=r.brier,
            cost_model_version=r.cost_model_version,
        )

        tournament = Tournament()
        result = tournament.score(scoring_input)
        assert result.model_id == "gbm-1"
        assert result.status is not None
