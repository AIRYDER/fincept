"""
Tests for gateway settlement wiring (Agent A — settlement track).

Covers:
- ``run_settlement_sweep()`` returns a receipt with counts.
- ``settlement_status()`` returns settled / pending / total counts.
- ``shadow_health()`` returns real ``settled_count`` and
  ``settlement_lag_seconds`` after a sweep.
- Disabled gateway returns safe defaults.
"""

from __future__ import annotations

import pathlib

from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.market_data_adapter import BarDataAdapter, PricePoint
from quant_foundry.settlement_sweep import SettlementSweep, default_cost_model
from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

_T_EVENT = 1_000_000_000_000_000_000
_HORIZON_NS = 60_000_000_000
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


def _store_predictions(gw: QuantFoundryGateway, predictions: list[dict]) -> None:
    batch_hash = compute_batch_hash(predictions)
    gw.shadow_ledger_real().store_batch(predictions=predictions, batch_hash=batch_hash)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestRunSettlementSweep:
    def test_returns_receipt_with_counts(self, tmp_path: pathlib.Path) -> None:
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
        _store_predictions(gw, [_make_prediction()])

        receipt = gw.run_settlement_sweep()
        assert receipt["settled_count"] == 1
        assert receipt["pending_time_count"] == 0
        assert receipt["pending_data_count"] == 0
        assert receipt["total"] == 1

    def test_disabled_gateway(self, tmp_path: pathlib.Path) -> None:
        gw = QuantFoundryGateway(
            enabled=False,
            mode="local_mock",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path,
        )
        receipt = gw.run_settlement_sweep()
        assert receipt["enabled"] is False

    def test_idempotent_rerun(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)
        _store_predictions(gw, [_make_prediction()])

        r1 = gw.run_settlement_sweep()
        r2 = gw.run_settlement_sweep()
        assert r1["settled_count"] == 1
        assert r2["settled_count"] == 1
        records = gw.settlement_ledger().read_all()
        assert len(records) == 1


class TestSettlementStatus:
    def test_returns_counts(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)
        _store_predictions(gw, [_make_prediction()])

        gw.run_settlement_sweep()
        status = gw.settlement_status()
        assert status["enabled"] is True
        assert status["settled_count"] == 1
        assert status["pending_time_count"] == 0
        assert status["pending_data_count"] == 0
        assert status["total"] == 1

    def test_disabled_gateway(self, tmp_path: pathlib.Path) -> None:
        gw = QuantFoundryGateway(
            enabled=False,
            mode="local_mock",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path,
        )
        status = gw.settlement_status()
        assert status["enabled"] is False
        assert status["settled_count"] == 0

    def test_empty_ledger(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path, {})
        status = gw.settlement_status()
        assert status["enabled"] is True
        assert status["settled_count"] == 0
        assert status["total"] == 0


class TestShadowHealthSettlement:
    def test_settled_count_after_sweep(self, tmp_path: pathlib.Path) -> None:
        bars = {
            "AAPL": [
                PricePoint(ts_ns=_T_EVENT, close=150.0),
                PricePoint(ts_ns=_WINDOW_END, close=153.0),
            ],
        }
        gw = _make_gateway(tmp_path, bars)
        _store_predictions(gw, [_make_prediction()])

        gw.run_settlement_sweep()
        health = gw.shadow_health()
        assert health["enabled"] is True
        assert health["settled_count"] == 1
        assert health["settlement_lag_seconds"] is not None

    def test_settled_count_zero_before_sweep(self, tmp_path: pathlib.Path) -> None:
        gw = _make_gateway(tmp_path, {})
        _store_predictions(gw, [_make_prediction()])
        health = gw.shadow_health()
        assert health["settled_count"] == 0
        assert health["settlement_lag_seconds"] is None

    def test_disabled_gateway(self, tmp_path: pathlib.Path) -> None:
        gw = QuantFoundryGateway(
            enabled=False,
            mode="local_mock",
            shadow_only=True,
            callback_secret="test-secret",
            base_dir=tmp_path,
        )
        health = gw.shadow_health()
        assert health["enabled"] is False
        assert health["settled_count"] == 0
        assert health["settlement_lag_seconds"] is None
