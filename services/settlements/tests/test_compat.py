"""Tests for the C6 settlement unification compatibility adapter.

Covers the required test scenarios from Task 13:

  * winning long / losing long
  * winning short / losing short (direction-aware PnL sign check)
  * flat / no-trade
  * missing prices / partial prices
  * high confidence prediction (Brier uses p_up, not direction)
  * direction-aware short PnL
  * Brier score uses p_up, not direction
  * abnormal_return populated
  * calibration_bucket populated
  * cm-v1 cost model emitted
  * agent_id maps to model_id
  * cost model versioning (same prediction + different cost model → two records)
  * borrow cost for shorts
  * custom agent_id mapping
  * missing mapping fails clearly
"""

from __future__ import annotations

import pathlib

import pytest
from fincept_core.prediction_log import PredictionLog
from quant_foundry.metrics import PriceTick
from quant_foundry.outcomes import CostModel, SettlementStatus
from quant_foundry.settlement import SettlementLedger
from quant_foundry.settlement_sweep import default_cost_model

from settlements.compat import (
    PathACompatAdapter,
    default_agent_to_model_id,
    default_model_to_agent_id,
    derive_p_up_from_confidence,
    path_b_to_path_a_record,
)

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

T0 = 1_700_000_000_000_000_000  # base time (ns)
HORIZON_NS = 3_600_000_000_000  # 1 hour
NOW_NS = T0 + HORIZON_NS + 60_000_000_000  # 1 min after horizon
AGENT = "test-agent.v1"
MODEL_NAME = "test-model"
SYMBOL = "AAPL"
BENCHMARK_SYMBOL = "SPY"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_prediction(
    *,
    prediction_id: str = "pred-001",
    agent_id: str = AGENT,
    model_name: str = MODEL_NAME,
    symbol: str = SYMBOL,
    ts_event: int = T0,
    horizon_ns: int = HORIZON_NS,
    direction: float = 1.0,
    confidence: float = 0.7,
) -> PredictionLog:
    """Build a PredictionRow for testing."""
    from fincept_core.prediction_log import PredictionRow

    return PredictionRow(
        id=prediction_id,
        agent_id=agent_id,
        model_name=model_name,
        ts_recorded=ts_event,
        ts_event=ts_event,
        horizon_ns=horizon_ns,
        symbol=symbol,
        direction=direction,
        confidence=confidence,
    )


def _make_adapter(
    tmp_path: pathlib.Path,
    *,
    cost_model: CostModel | None = None,
    agent_to_model_id=default_agent_to_model_id,
) -> PathACompatAdapter:
    """Build an adapter with a tmp-path ledger."""
    ledger = SettlementLedger(root=tmp_path / "ledger")
    return PathACompatAdapter(
        agent_to_model_id=agent_to_model_id,
        cost_model=cost_model,
        settlement_ledger=ledger,
    )


def _benchmark_prices(entry: float, exit_: float) -> list[PriceTick]:
    return [
        PriceTick(ts=T0, price=entry),
        PriceTick(ts=T0 + HORIZON_NS, price=exit_),
    ]


# --------------------------------------------------------------------------- #
# Identity mapping                                                            #
# --------------------------------------------------------------------------- #


class TestIdentityMapping:
    def test_default_agent_to_model_id_replaces_dots(self):
        assert default_agent_to_model_id("gbm_predictor.v1") == "gbm_predictor-v1"

    def test_default_agent_to_model_id_no_dots(self):
        assert default_agent_to_model_id("simple_agent") == "simple_agent"

    def test_default_agent_to_model_id_empty_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            default_agent_to_model_id("")

    def test_default_model_to_agent_id_replaces_dashes(self):
        assert default_model_to_agent_id("gbm_predictor-v1") == "gbm_predictor.v1"

    def test_default_model_to_agent_id_empty_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            default_model_to_agent_id("")

    def test_custom_mapping_function(self, tmp_path):
        custom_map = {"agent_x": "model_y"}

        def mapper(agent_id: str) -> str:
            return custom_map.get(agent_id, default_agent_to_model_id(agent_id))

        adapter = _make_adapter(tmp_path, agent_to_model_id=mapper)
        pred = _make_prediction(agent_id="agent_x", prediction_id="pred-custom")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.agent_id == "agent_x"
        # Verify the Path B ledger stored it under model_y
        b_records = adapter.ledger.read_all()
        assert len(b_records) == 1
        assert b_records[0].model_id == "model_y"


# --------------------------------------------------------------------------- #
# p_up derivation                                                             #
# --------------------------------------------------------------------------- #


class TestDerivePUp:
    def test_long_uses_confidence(self):
        assert derive_p_up_from_confidence(1.0, 0.8) == 0.8

    def test_short_uses_one_minus_confidence(self):
        assert derive_p_up_from_confidence(-1.0, 0.7) == pytest.approx(0.3)

    def test_flat_uses_half(self):
        assert derive_p_up_from_confidence(0.0, 0.9) == 0.5

    def test_long_clamped_to_1(self):
        assert derive_p_up_from_confidence(1.0, 1.5) == 1.0

    def test_short_clamped_to_0(self):
        assert derive_p_up_from_confidence(-1.0, 1.5) == 0.0


# --------------------------------------------------------------------------- #
# Settlement scenarios                                                        #
# --------------------------------------------------------------------------- #


class TestSettlementScenarios:
    """Test all 8 replay fixture scenarios through the compat adapter."""

    def test_winning_long(self, tmp_path):
        """Long, price +5% → positive gross and net."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="wl-001")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred,
            prices=prices,
            benchmark_prices=_benchmark_prices(400, 402),
            now_ns=NOW_NS,
        )
        assert record.status == "settled"
        assert record.realized_return_gross == pytest.approx(0.05)
        # cm-v1: fee 10 + spread 5 + slippage 3 = 18 bps = 0.0018
        assert record.realized_return_net == pytest.approx(0.05 - 0.0018)
        assert record.cost_model_version == "cm-v1"

    def test_losing_long(self, tmp_path):
        """Long, price -5% → negative gross and net."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.6, prediction_id="ll-002")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=95.0),
        ]
        record = adapter.settle_prediction(
            pred,
            prices=prices,
            benchmark_prices=_benchmark_prices(400, 402),
            now_ns=NOW_NS,
        )
        assert record.status == "settled"
        assert record.realized_return_gross == pytest.approx(-0.05)
        assert (
            record.realized_return_net < record.realized_return_gross
        )  # costs make it worse

    def test_winning_short(self, tmp_path):
        """Short, price -5% → POSITIVE gross and net (direction-aware)."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=-1.0, confidence=0.65, prediction_id="ws-003")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=95.0),
        ]
        record = adapter.settle_prediction(
            pred,
            prices=prices,
            benchmark_prices=_benchmark_prices(400, 402),
            now_ns=NOW_NS,
        )
        assert record.status == "settled"
        # Short: (entry - exit) / entry = (100 - 95) / 100 = +0.05
        assert record.realized_return_gross == pytest.approx(0.05)
        # cm-v1: fee 10 + spread 5 + slippage 3 + borrow 25*1 = 43 bps = 0.0043
        assert record.realized_return_net == pytest.approx(0.05 - 0.0043)
        assert record.realized_return_net > 0  # winning short after costs

    def test_losing_short(self, tmp_path):
        """Short, price +5% → NEGATIVE gross and net (direction-aware)."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=-1.0, confidence=0.6, prediction_id="ls-004")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred,
            prices=prices,
            benchmark_prices=_benchmark_prices(400, 402),
            now_ns=NOW_NS,
        )
        assert record.status == "settled"
        # Short: (entry - exit) / entry = (100 - 105) / 100 = -0.05
        assert record.realized_return_gross == pytest.approx(-0.05)
        # Costs make it even worse
        assert record.realized_return_net < record.realized_return_gross
        assert record.realized_return_net < 0  # losing short after costs

    def test_flat(self, tmp_path):
        """No price movement → gross 0, net = -costs."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.5, prediction_id="flat-005")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=100.0),
        ]
        record = adapter.settle_prediction(
            pred,
            prices=prices,
            benchmark_prices=_benchmark_prices(400, 400),
            now_ns=NOW_NS,
        )
        assert record.status == "settled"
        assert record.realized_return_gross == pytest.approx(0.0)
        # Long: fee 10 + spread 5 + slippage 3 = 18 bps
        assert record.realized_return_net == pytest.approx(-0.0018)

    def test_missing_prices(self, tmp_path):
        """No price data → pending_data."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="miss-006")
        record = adapter.settle_prediction(
            pred, prices=[], benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.status == "pending_data"
        assert record.realized_return_gross is None
        assert record.realized_return_net is None

    def test_partial_prices_entry_only(self, tmp_path):
        """Entry price available, exit missing → pending_data."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="part-007")
        prices = [PriceTick(ts=T0, price=100.0)]  # only entry
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.status == "pending_data"
        assert record.realized_return_gross is None

    def test_high_confidence_win(self, tmp_path):
        """High confidence (0.9) winning trade +10%."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.9, prediction_id="hc-008")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=110.0),
        ]
        record = adapter.settle_prediction(
            pred,
            prices=prices,
            benchmark_prices=_benchmark_prices(400, 410),
            now_ns=NOW_NS,
        )
        assert record.status == "settled"
        assert record.realized_return_gross == pytest.approx(0.10)


# --------------------------------------------------------------------------- #
# Brier score uses p_up, not direction                                        #
# --------------------------------------------------------------------------- #


class TestBrierScore:
    def test_brier_uses_p_up_not_direction(self, tmp_path):
        """Brier score uses derived p_up, not (direction+1)/2.

        For a long with confidence 0.7:
          - Path A (old): prob_up = (1+1)/2 = 1.0 → brier = (1.0 - 1)^2 = 0.0
          - Path B (new): p_up = confidence = 0.7 → brier = (0.7 - 1)^2 = 0.09

        The adapter should use 0.7, not 1.0.
        """
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(
            direction=1.0, confidence=0.7, prediction_id="brier-001"
        )
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.status == "settled"
        # actual_up = True (gross 0.05 > 0)
        # p_up = 0.7 (from confidence, not (direction+1)/2 = 1.0)
        # brier = (0.7 - 1.0)^2 = 0.09
        assert record.brier_component == pytest.approx(0.09)

    def test_brier_changes_with_confidence(self, tmp_path):
        """Brier score changes when confidence changes (same direction)."""
        adapter = _make_adapter(tmp_path)

        # Prediction 1: confidence 0.7
        pred1 = _make_prediction(direction=1.0, confidence=0.7, prediction_id="brier-a")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        r1 = adapter.settle_prediction(
            pred1, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )

        # Prediction 2: confidence 0.9 (same direction, same prices)
        adapter2 = _make_adapter(tmp_path / "second")
        pred2 = _make_prediction(direction=1.0, confidence=0.9, prediction_id="brier-b")
        r2 = adapter2.settle_prediction(
            pred2, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )

        # Both settled, same gross, but different brier
        assert r1.brier_component != r2.brier_component
        # r1: p_up=0.7, actual=1 → (0.7-1)^2 = 0.09
        assert r1.brier_component == pytest.approx(0.09)
        # r2: p_up=0.9, actual=1 → (0.9-1)^2 = 0.01
        assert r2.brier_component == pytest.approx(0.01)

    def test_brier_short_uses_one_minus_confidence(self, tmp_path):
        """For a short, p_up = 1 - confidence, not (direction+1)/2 = 0."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(
            direction=-1.0, confidence=0.65, prediction_id="brier-short"
        )
        # Short winning: price goes down
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=95.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.status == "settled"
        # gross = (100-95)/100 = 0.05 > 0 → actual_up = True
        # p_up = 1 - 0.65 = 0.35
        # brier = (0.35 - 1.0)^2 = 0.4225
        assert record.brier_component == pytest.approx(0.4225)


# --------------------------------------------------------------------------- #
# abnormal_return and calibration_bucket                                      #
# --------------------------------------------------------------------------- #


class TestAbnormalReturnAndCalibration:
    def test_abnormal_return_populated(self, tmp_path):
        """abnormal_return = realized - benchmark."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="ab-001")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        bench = _benchmark_prices(400, 402)  # benchmark +0.5%
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=bench, now_ns=NOW_NS
        )
        assert record.status == "settled"
        # Path A record does not carry abnormal_return directly —
        # check the Path B record in the ledger
        b_records = adapter.ledger.read_all()
        assert len(b_records) == 1
        b_rec = b_records[0]
        assert b_rec.abnormal_return is not None
        # realized = 0.05, benchmark = (402-400)/400 = 0.005
        assert b_rec.abnormal_return == pytest.approx(0.05 - 0.005)

    def test_abnormal_return_none_when_no_benchmark(self, tmp_path):
        """abnormal_return is None when benchmark_prices is None."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="ab-002")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.status == "settled"
        b_records = adapter.ledger.read_all()
        assert b_records[0].abnormal_return is None

    def test_calibration_bucket_populated(self, tmp_path):
        """calibration_bucket is populated from confidence."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="cal-001")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.status == "settled"
        b_records = adapter.ledger.read_all()
        assert b_records[0].calibration_bucket is not None
        # confidence 0.7 → bucket "0.6-0.8"
        assert b_records[0].calibration_bucket == "0.6-0.8"

    def test_calibration_bucket_low_confidence(self, tmp_path):
        """calibration_bucket for low confidence (0.1) → "0.0-0.2"."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.1, prediction_id="cal-002")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.status == "settled"
        b_records = adapter.ledger.read_all()
        assert b_records[0].calibration_bucket == "0.0-0.2"


# --------------------------------------------------------------------------- #
# Cost model                                                                  #
# --------------------------------------------------------------------------- #


class TestCostModel:
    def test_cm_v1_cost_model_emitted(self, tmp_path):
        """The adapter uses cm-v1 cost model by default."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="cm-001")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert record.cost_model_version == "cm-v1"
        assert record.cost_breakdown_fee_bps == 10.0
        assert record.cost_breakdown_spread_bps == 5.0
        assert record.cost_breakdown_slippage_bps == 3.0

    def test_borrow_cost_for_shorts(self, tmp_path):
        """Short positions pay borrow_bps_per_day * holding_days."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(
            direction=-1.0, confidence=0.65, prediction_id="borrow-001"
        )
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=95.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS, holding_days=1
        )
        assert record.status == "settled"
        # gross = 0.05
        # costs = (10 + 5 + 3 + 25*1) / 10000 = 43 bps = 0.0043
        assert record.realized_return_net == pytest.approx(0.05 - 0.0043)

    def test_no_borrow_cost_for_longs(self, tmp_path):
        """Long positions do not pay borrow cost."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(
            direction=1.0, confidence=0.7, prediction_id="borrow-002"
        )
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        record = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS, holding_days=1
        )
        assert record.status == "settled"
        # gross = 0.05
        # costs = (10 + 5 + 3) / 10000 = 18 bps = 0.0018 (no borrow)
        assert record.realized_return_net == pytest.approx(0.05 - 0.0018)

    def test_cost_model_versioning(self, tmp_path):
        """Same prediction + different cost model → two records."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="ver-001")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]

        # Settle with cm-v1
        r1 = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert r1.cost_model_version == "cm-v1"

        # Settle with a custom cost model (cm-v2-test)
        cm2 = CostModel(
            version="cm-v2-test",
            fee_bps=15.0,
            spread_bps=8.0,
            slippage_bps=5.0,
            borrow_bps_per_day=30.0,
        )
        adapter2 = _make_adapter(tmp_path, cost_model=cm2)
        r2 = adapter2.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert r2.cost_model_version == "cm-v2-test"

        # Both records exist in the ledger
        all_records = adapter.ledger.read_all()
        versions = {r.cost_model_version for r in all_records}
        assert "cm-v1" in versions
        assert "cm-v2-test" in versions


# --------------------------------------------------------------------------- #
# Path A → Path B record translation                                          #
# --------------------------------------------------------------------------- #


class TestRecordTranslation:
    def test_path_b_to_path_a_record(self, tmp_path):
        """Verify the translation from Path B to Path A record shape."""
        from quant_foundry.outcomes import SettlementRecord as BRecord

        b_record = BRecord(
            prediction_id="test-001",
            model_id="test-model",
            symbol="AAPL",
            ts_event=T0,
            horizon_ns=HORIZON_NS,
            status=SettlementStatus.SETTLED,
            settled_at_ns=NOW_NS,
            realized_return_gross=0.05,
            realized_return_net=0.0482,
            abnormal_return=0.045,
            brier=0.09,
            calibration_bucket="0.6-0.8",
            cost_model_version="cm-v1",
            decision_window_start=T0,
            decision_window_end=T0 + HORIZON_NS,
        )
        cm = default_cost_model()
        a_record = path_b_to_path_a_record(
            b_record, agent_id="test-agent.v1", model_name="test-model", cost_model=cm
        )
        assert a_record.prediction_id == "test-001"
        assert a_record.agent_id == "test-agent.v1"
        assert a_record.model_name == "test-model"
        assert a_record.cost_model_version == "cm-v1"
        assert a_record.realized_return_gross == 0.05
        assert a_record.realized_return_net == 0.0482
        assert a_record.brier_component == 0.09
        assert a_record.cost_breakdown_fee_bps == 10.0
        assert a_record.cost_breakdown_spread_bps == 5.0
        assert a_record.cost_breakdown_slippage_bps == 3.0
        assert a_record.status == "settled"


# --------------------------------------------------------------------------- #
# Idempotency                                                                 #
# --------------------------------------------------------------------------- #


class TestIdempotency:
    def test_settle_same_prediction_twice_is_idempotent(self, tmp_path):
        """Settling the same prediction twice returns the same record."""
        adapter = _make_adapter(tmp_path)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="idem-001")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]

        r1 = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        r2 = adapter.settle_prediction(
            pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
        )
        assert r1.prediction_id == r2.prediction_id
        assert r1.realized_return_gross == r2.realized_return_gross
        # Only one record in the ledger
        all_records = adapter.ledger.read_all()
        assert len(all_records) == 1


# --------------------------------------------------------------------------- #
# Sync batch settlement                                                        #
# --------------------------------------------------------------------------- #


class TestBatchSettlement:
    def test_settle_due_predictions_sync(self, tmp_path):
        """The sync batch method settles due predictions from the log."""
        predictions_dir = tmp_path / "predictions"
        log = PredictionLog(predictions_dir=predictions_dir)

        # Write 2 predictions: one due, one not yet due
        log.append(
            agent_id=AGENT,
            model_name=MODEL_NAME,
            ts_event=T0,
            horizon_ns=HORIZON_NS,
            symbol=SYMBOL,
            direction=1.0,
            confidence=0.7,
        )
        log.append(
            agent_id=AGENT,
            model_name=MODEL_NAME,
            ts_event=NOW_NS + 1_000_000_000_000,  # future
            horizon_ns=HORIZON_NS,
            symbol=SYMBOL,
            direction=1.0,
            confidence=0.6,
        )

        adapter = _make_adapter(tmp_path)

        # Price source: returns 100 at T0, 105 at T0+HORIZON
        def price_source(symbol: str, ts1: int, ts2: int) -> float | None:
            if ts2 == T0:
                return 100.0
            if ts2 == T0 + HORIZON_NS:
                return 105.0
            return None

        records = adapter.settle_due_predictions(
            predictions_dir,
            now_ns=NOW_NS,
            market_data_source=price_source,
        )
        # Only the due prediction should be settled
        assert len(records) == 1
        assert records[0].status == "settled"
        assert records[0].realized_return_gross == pytest.approx(0.05)


# --------------------------------------------------------------------------- #
# Missing mapping fails clearly                                               #
# --------------------------------------------------------------------------- #


class TestMissingMapping:
    def test_empty_mapping_raises(self, tmp_path):
        """A mapping function that returns empty string should raise."""

        def bad_mapper(agent_id: str) -> str:
            return ""

        adapter = _make_adapter(tmp_path, agent_to_model_id=bad_mapper)
        pred = _make_prediction(direction=1.0, confidence=0.7, prediction_id="bad-001")
        prices = [
            PriceTick(ts=T0, price=100.0),
            PriceTick(ts=T0 + HORIZON_NS, price=105.0),
        ]
        with pytest.raises(ValueError, match="empty model_id"):
            adapter.settle_prediction(
                pred, prices=prices, benchmark_prices=None, now_ns=NOW_NS
            )
