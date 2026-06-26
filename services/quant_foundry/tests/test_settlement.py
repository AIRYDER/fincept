"""
TDD tests for quant_foundry.settlement (TASK-0401: Prediction Settlement Ledger).

Acceptance criteria from NEXT_STEPS_PLAN:
- Fixture predictions settle deterministically.
- Missing market data does not crash settlement.
- Reruns do not duplicate outcomes.
- Output can feed tournament scoring.

Plus the spec details:
- Settle strictly on the post-decision window (t, t+h] — realized return must
  use only prices observed after the prediction's decision time. A prediction
  whose horizon has not fully elapsed stays `pending_time`.
- `pending_time` (horizon not elapsed) and `pending_data` (market data missing)
  are kept distinct so a stuck provider is not confused with a not-yet-due
  prediction.
- Realized return by horizon.
- Abnormal return versus benchmark where data exists.
- Brier score.
- Calibration bucket.
- Explicit, versioned cost/slippage assumptions (fee bps, modeled spread,
  slippage model, borrow cost). Store the cost-model version on each outcome
  so a later cost-model change does not silently rewrite history; settle both
  gross and net so the tournament can rank on net.
- Reruns idempotent (re-settling a prediction with the same inputs and
  cost-model version yields the identical outcome row).

These tests are file-disjoint from TASK-0304 (outbox/inbox) and TASK-0204
(dashboard api.ts). They do NOT touch schemas.py.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
from quant_foundry.metrics import (
    abnormal_return,
    apply_costs,
    brier_score,
    calibration_bucket,
    realized_return,
)
from quant_foundry.outcomes import (
    CostModel,
    SettlementRecord,
    SettlementStatus,
)
from quant_foundry.settlement import SettlementLedger

# --------------------------------------------------------------------------- #
# Test fixtures                                                               #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class PriceTick:
    """A price observed at a known wall-clock time (nanoseconds)."""

    ts: int
    price: float


def _prediction(
    *,
    prediction_id: str = "pred-1",
    model_id: str = "gbm-1",
    symbol: str = "AAPL",
    ts_event: int = 1_000_000_000_000_000_000,  # decision time t
    horizon_ns: int = 60_000_000_000,  # 60s
    direction: float = 1.0,
    confidence: float = 0.7,
    p_up: float = 0.7,
) -> dict:
    """A minimal prediction shape the ledger accepts (decoupled from schemas)."""
    return {
        "prediction_id": prediction_id,
        "model_id": model_id,
        "symbol": symbol,
        "ts_event": ts_event,
        "horizon_ns": horizon_ns,
        "direction": direction,
        "confidence": confidence,
        "p_up": p_up,
    }


def _cost_model(version: str = "cost-v1") -> CostModel:
    return CostModel(
        version=version,
        fee_bps=1.0,
        spread_bps=2.0,
        slippage_bps=1.5,
        borrow_bps_per_day=0.5,
    )


# --------------------------------------------------------------------------- #
# metrics.py                                                                  #
# --------------------------------------------------------------------------- #


def test_realized_return_uses_only_post_decision_prices() -> None:
    """Realized return on (t, t+h] uses price at t as base and price at t+h as end.

    A price observed BEFORE the decision time must NOT be used as the base
    (that would be look-ahead in the form of using the decision-time print
    that may have been revised). We use the first price observed at or after
    t as the entry, and the first price observed at or after t+h as the exit.
    """
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [
        PriceTick(ts=t - 10_000_000_000, price=90.0),  # pre-decision: must be ignored
        PriceTick(ts=t, price=100.0),  # entry print at decision time
        PriceTick(ts=t + h, price=105.0),  # exit print at horizon
    ]
    ret = realized_return(prices=prices, decision_ts=t, horizon_ns=h, direction=1.0)
    assert ret == pytest.approx((105.0 - 100.0) / 100.0)


def test_realized_return_short_direction_flips_sign() -> None:
    """A short prediction's realized return is inverted (profit when price falls)."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [
        PriceTick(ts=t, price=100.0),
        PriceTick(ts=t + h, price=95.0),  # price fell -> short profits
    ]
    ret = realized_return(prices=prices, decision_ts=t, horizon_ns=h, direction=-1.0)
    assert ret == pytest.approx((100.0 - 95.0) / 100.0)


def test_realized_return_returns_none_when_no_exit_price_in_window() -> None:
    """If no price exists at or after t+h, the return cannot be computed."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t, price=100.0)]  # entry only, no exit
    ret = realized_return(prices=prices, decision_ts=t, horizon_ns=h, direction=1.0)
    assert ret is None


def test_realized_return_returns_none_when_no_entry_price() -> None:
    """If no price exists at or after t, the return cannot be computed."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t - 5_000_000_000, price=100.0)]  # only pre-decision
    ret = realized_return(prices=prices, decision_ts=t, horizon_ns=h, direction=1.0)
    assert ret is None


def test_brier_score_perfect_confidence_correct_direction() -> None:
    """Brier score for a confident-correct prediction is near 0."""
    # p_up=0.9, actual up (1): (0.9-1)^2 = 0.01
    assert brier_score(p_up=0.9, actual_up=True) == pytest.approx(0.01)


def test_brier_score_confident_wrong_is_large() -> None:
    """Brier score for a confident-wrong prediction is large."""
    # p_up=0.9, actual down (0): (0.9-0)^2 = 0.81
    assert brier_score(p_up=0.9, actual_up=False) == pytest.approx(0.81)


def test_brier_score_unconfident_is_moderate() -> None:
    # p_up=0.5, actual up: (0.5-1)^2 = 0.25
    assert brier_score(p_up=0.5, actual_up=True) == pytest.approx(0.25)


def test_calibration_bucket_buckets_by_confidence() -> None:
    """Confidence is bucketed into named ranges for reliability curves."""
    assert calibration_bucket(confidence=0.05) == "0.0-0.2"
    assert calibration_bucket(confidence=0.2) == "0.0-0.2"
    assert calibration_bucket(confidence=0.25) == "0.2-0.4"
    assert calibration_bucket(confidence=0.55) == "0.4-0.6"
    assert calibration_bucket(confidence=0.75) == "0.6-0.8"
    assert calibration_bucket(confidence=0.95) == "0.8-1.0"


def test_abnormal_return_subtracts_benchmark() -> None:
    """Abnormal return = realized - benchmark over the same window."""
    assert abnormal_return(realized=0.05, benchmark=0.02) == pytest.approx(0.03)
    assert abnormal_return(realized=-0.01, benchmark=0.03) == pytest.approx(-0.04)


def test_abnormal_return_none_when_benchmark_missing() -> None:
    """Missing benchmark data yields None (not a crash, not a zero)."""
    assert abnormal_return(realized=0.05, benchmark=None) is None


def test_apply_costs_gross_to_net_long() -> None:
    """Net = gross - round-trip costs (fee + spread + slippage), long side.

    Round-trip cost in bps is converted to a fraction: fee_bps + spread_bps +
    slippage_bps, divided by 1e4. Borrow is zero for longs.
    """
    cm = _cost_model()  # fee 1 + spread 2 + slippage 1.5 = 4.5 bps round-trip
    net = apply_costs(gross_return=0.10, cost_model=cm, direction=1.0, holding_days=1)
    expected = 0.10 - 4.5 / 1e4
    assert net == pytest.approx(expected)


def test_apply_costs_includes_borrow_for_short() -> None:
    """Short side pays borrow cost scaled by holding days."""
    cm = _cost_model()  # borrow 0.5 bps/day
    net = apply_costs(gross_return=0.10, cost_model=cm, direction=-1.0, holding_days=3)
    # round-trip 4.5 bps + 3 days * 0.5 bps = 6.0 bps
    expected = 0.10 - (4.5 + 3 * 0.5) / 1e4
    assert net == pytest.approx(expected)


def test_apply_costs_negative_gross_becomes_more_negative_net() -> None:
    """Costs apply symmetrically to losing trades (net is worse than gross)."""
    cm = _cost_model()
    net = apply_costs(gross_return=-0.05, cost_model=cm, direction=1.0, holding_days=1)
    expected = -0.05 - 4.5 / 1e4
    assert net == pytest.approx(expected)
    assert net < -0.05


# --------------------------------------------------------------------------- #
# outcomes.py                                                                 #
# --------------------------------------------------------------------------- #


def test_settlement_status_distinct_pending_states() -> None:
    """pending_time (horizon not elapsed) and pending_data (data missing) are distinct."""
    assert SettlementStatus.PENDING_TIME != SettlementStatus.PENDING_DATA
    assert SettlementStatus.PENDING_TIME.value == "pending_time"
    assert SettlementStatus.PENDING_DATA.value == "pending_data"
    assert SettlementStatus.SETTLED.value == "settled"


def test_cost_model_is_frozen_and_versioned() -> None:
    """CostModel is immutable and carries an explicit version (no silent history rewrite)."""
    cm = _cost_model(version="cost-v1")
    assert cm.version == "cost-v1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        cm.version = "cost-v2"  # type: ignore[misc]  # frozen


def test_settlement_record_carries_cost_model_version_gross_and_net() -> None:
    """A settled record stores both gross and net return plus the cost-model version."""
    rec = SettlementRecord(
        prediction_id="pred-1",
        model_id="gbm-1",
        symbol="AAPL",
        ts_event=1_000_000_000_000_000_000,
        horizon_ns=60_000_000_000,
        status=SettlementStatus.SETTLED,
        settled_at_ns=1_000_000_000_000_000_000 + 60_000_000_000,
        realized_return_gross=0.05,
        realized_return_net=0.05 - 4.5e-4,
        abnormal_return=0.03,
        brier=0.01,
        calibration_bucket="0.6-0.8",
        cost_model_version="cost-v1",
        decision_window_start=1_000_000_000_000_000_000,
        decision_window_end=1_000_000_000_000_000_000 + 60_000_000_000,
    )
    assert rec.cost_model_version == "cost-v1"
    assert rec.realized_return_gross == 0.05
    assert rec.realized_return_net < rec.realized_return_gross


def test_settlement_record_pending_time_has_no_returns() -> None:
    """A pending_time record has no realized returns (horizon not elapsed)."""
    rec = SettlementRecord(
        prediction_id="pred-1",
        model_id="gbm-1",
        symbol="AAPL",
        ts_event=1_000_000_000_000_000_000,
        horizon_ns=60_000_000_000,
        status=SettlementStatus.PENDING_TIME,
        settled_at_ns=None,
        realized_return_gross=None,
        realized_return_net=None,
        abnormal_return=None,
        brier=None,
        calibration_bucket=None,
        cost_model_version="cost-v1",
        decision_window_start=1_000_000_000_000_000_000,
        decision_window_end=1_000_000_000_000_000_000 + 60_000_000_000,
    )
    assert rec.status == SettlementStatus.PENDING_TIME
    assert rec.realized_return_gross is None


# --------------------------------------------------------------------------- #
# settlement.py — the ledger                                                  #
# --------------------------------------------------------------------------- #


def test_settle_prediction_settled_deterministically(tmp_path: pathlib.Path) -> None:
    """A prediction with prices spanning (t, t+h] settles deterministically."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [
        PriceTick(ts=t - 10_000_000_000, price=90.0),
        PriceTick(ts=t, price=100.0),
        PriceTick(ts=t + h, price=105.0),
    ]
    benchmark = [
        PriceTick(ts=t, price=100.0),
        PriceTick(ts=t + h, price=101.0),
    ]
    ledger = SettlementLedger(root=tmp_path)
    rec = ledger.settle(
        prediction=_prediction(ts_event=t, horizon_ns=h, direction=1.0, confidence=0.7, p_up=0.7),
        prices=prices,
        benchmark_prices=benchmark,
        cost_model=_cost_model(),
        now_ns=t + h + 1,
    )
    assert rec.status == SettlementStatus.SETTLED
    assert rec.realized_return_gross == pytest.approx(0.05)
    # net = 0.05 - 4.5bps
    assert rec.realized_return_net == pytest.approx(0.05 - 4.5e-4)
    assert rec.abnormal_return == pytest.approx(0.05 - 0.01)
    assert rec.brier == pytest.approx((0.7 - 1) ** 2)
    assert rec.calibration_bucket == "0.6-0.8"
    assert rec.cost_model_version == "cost-v1"


def test_settle_prediction_pending_time_when_horizon_not_elapsed(
    tmp_path: pathlib.Path,
) -> None:
    """If now < t+h, the prediction stays pending_time (not yet due)."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t, price=100.0), PriceTick(ts=t + h, price=105.0)]
    ledger = SettlementLedger(root=tmp_path)
    rec = ledger.settle(
        prediction=_prediction(ts_event=t, horizon_ns=h),
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + 1,  # horizon NOT elapsed
    )
    assert rec.status == SettlementStatus.PENDING_TIME
    assert rec.realized_return_gross is None


def test_settle_prediction_pending_data_when_market_data_missing(
    tmp_path: pathlib.Path,
) -> None:
    """Horizon elapsed but no exit price -> pending_data (distinct from pending_time)."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t, price=100.0)]  # entry only, no exit
    ledger = SettlementLedger(root=tmp_path)
    rec = ledger.settle(
        prediction=_prediction(ts_event=t, horizon_ns=h),
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + h + 1,  # horizon elapsed
    )
    assert rec.status == SettlementStatus.PENDING_DATA
    assert rec.realized_return_gross is None


def test_settle_does_not_crash_on_completely_missing_prices(
    tmp_path: pathlib.Path,
) -> None:
    """No prices at all -> pending_data, no exception."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    ledger = SettlementLedger(root=tmp_path)
    rec = ledger.settle(
        prediction=_prediction(ts_event=t, horizon_ns=h),
        prices=[],
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + h + 1,
    )
    assert rec.status == SettlementStatus.PENDING_DATA


def test_reruns_are_idempotent_same_inputs_same_record(
    tmp_path: pathlib.Path,
) -> None:
    """Re-settling with identical inputs + cost-model version yields the same row, no duplication."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t, price=100.0), PriceTick(ts=t + h, price=105.0)]
    ledger = SettlementLedger(root=tmp_path)
    pred = _prediction(prediction_id="pred-1", ts_event=t, horizon_ns=h)
    rec1 = ledger.settle(
        prediction=pred,
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + h + 1,
    )
    rec2 = ledger.settle(
        prediction=pred,
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + h + 1,
    )
    assert rec1 == rec2
    # Only one record persisted for this prediction_id
    rows = ledger.read_all()
    assert sum(1 for r in rows if r.prediction_id == "pred-1") == 1


def test_rerun_with_different_cost_model_version_produces_new_record(
    tmp_path: pathlib.Path,
) -> None:
    """A different cost-model version is a new (append) record, not an overwrite."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t, price=100.0), PriceTick(ts=t + h, price=105.0)]
    ledger = SettlementLedger(root=tmp_path)
    pred = _prediction(prediction_id="pred-1", ts_event=t, horizon_ns=h)
    rec1 = ledger.settle(
        prediction=pred,
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(version="cost-v1"),
        now_ns=t + h + 1,
    )
    rec2 = ledger.settle(
        prediction=pred,
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(version="cost-v2"),
        now_ns=t + h + 1,
    )
    assert rec1.cost_model_version == "cost-v1"
    assert rec2.cost_model_version == "cost-v2"
    # Both records persisted (history preserved, not overwritten)
    rows = ledger.read_all()
    versions = sorted(r.cost_model_version for r in rows if r.prediction_id == "pred-1")
    assert versions == ["cost-v1", "cost-v2"]


def test_settlement_output_can_feed_tournament(tmp_path: pathlib.Path) -> None:
    """The settled record exposes the fields a tournament scorer needs:
    prediction_id, model_id, realized_return_net, brier, calibration_bucket,
    abnormal_return, cost_model_version."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t, price=100.0), PriceTick(ts=t + h, price=108.0)]
    ledger = SettlementLedger(root=tmp_path)
    rec = ledger.settle(
        prediction=_prediction(model_id="gbm-1", ts_event=t, horizon_ns=h, p_up=0.8),
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + h + 1,
    )
    # Tournament-relevant fields all present
    for field in (
        "prediction_id",
        "model_id",
        "realized_return_net",
        "brier",
        "calibration_bucket",
        "abnormal_return",
        "cost_model_version",
    ):
        assert hasattr(rec, field), f"missing tournament field: {field}"
    assert rec.status == SettlementStatus.SETTLED


def test_settlement_persists_across_restart(tmp_path: pathlib.Path) -> None:
    """A new ledger instance pointing at the same root reads prior settlements."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [PriceTick(ts=t, price=100.0), PriceTick(ts=t + h, price=105.0)]
    ledger1 = SettlementLedger(root=tmp_path)
    ledger1.settle(
        prediction=_prediction(prediction_id="pred-1", ts_event=t, horizon_ns=h),
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + h + 1,
    )
    # Simulate restart: new instance, same root
    ledger2 = SettlementLedger(root=tmp_path)
    rows = ledger2.read_all()
    assert any(r.prediction_id == "pred-1" for r in rows)


def test_settlement_uses_strict_post_decision_window_no_lookahead(
    tmp_path: pathlib.Path,
) -> None:
    """A price strictly BEFORE t must not be used as the entry; a revised
    pre-decision print does not leak into the realized return."""
    t = 1_000_000_000_000_000_000
    h = 60_000_000_000
    prices = [
        PriceTick(ts=t - 1_000_000_000, price=50.0),  # pre-decision, would inflate return
        PriceTick(ts=t, price=100.0),
        PriceTick(ts=t + h, price=105.0),
    ]
    ledger = SettlementLedger(root=tmp_path)
    rec = ledger.settle(
        prediction=_prediction(ts_event=t, horizon_ns=h, direction=1.0),
        prices=prices,
        benchmark_prices=None,
        cost_model=_cost_model(),
        now_ns=t + h + 1,
    )
    # If the pre-decision price leaked, return would be (105-50)/50 = 1.1.
    # Correct value is (105-100)/100 = 0.05.
    assert rec.realized_return_gross == pytest.approx(0.05)


def test_settlement_record_is_immutable(tmp_path: pathlib.Path) -> None:
    """SettlementRecord is frozen — no mutation after creation (audit integrity)."""
    rec = SettlementRecord(
        prediction_id="p",
        model_id="m",
        symbol="S",
        ts_event=1,
        horizon_ns=1,
        status=SettlementStatus.SETTLED,
        settled_at_ns=2,
        realized_return_gross=0.01,
        realized_return_net=0.009,
        abnormal_return=None,
        brier=0.1,
        calibration_bucket="0.4-0.6",
        cost_model_version="cost-v1",
        decision_window_start=1,
        decision_window_end=2,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.realized_return_gross = 0.99  # type: ignore[misc]
