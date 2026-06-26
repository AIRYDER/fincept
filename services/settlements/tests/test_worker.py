"""Tests for the settlement worker MVP (todo 11).

Covers the five acceptance scenarios:
  1. settles pending predictions,
  2. ignores already-settled rows,
  3. skips future (horizon not yet elapsed) predictions,
  4. handles missing market data as ``pending_data``,
  5. idempotent on rerun.

Plus the QA scenarios from the plan:
  * Happy: 3 past predictions with fixture prices 100/110/120 yield
    ``realized_return_gross ∈ {0.10, 0.20}`` and ``status="settled"``.
  * Fail: future prediction skipped; second tick_sync no-op for settled
    rows; missing data → ``pending_data``; retry with data → ``settled``.
"""

from __future__ import annotations

import pathlib

import pytest

from fincept_core.datasets import SettlementStore
from fincept_core.prediction_log import PredictionLog
from settlements.worker import tick, tick_sync

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

NOW = 10_000_000_000  # 10s in ns
HORIZON = 1_000_000_000  # 1s in ns
AGENT = "fixture-agent"
MODEL = "fixture-model.v1"
SYMBOL = "FIX"


def _write_prediction(
    log: PredictionLog,
    *,
    ts_event: int,
    horizon_ns: int = HORIZON,
    direction: float = 1.0,
    confidence: float = 0.8,
    symbol: str = SYMBOL,
) -> None:
    log.append(
        agent_id=AGENT,
        model_name=MODEL,
        ts_event=ts_event,
        horizon_ns=horizon_ns,
        symbol=symbol,
        direction=direction,
        confidence=confidence,
    )


def _price_source(prices: dict[tuple[str, int], float]):
    """Return a sync source that returns close at ts2 keyed by (symbol, ts2)."""

    def src(symbol: str, ts1: int, ts2: int) -> float | None:
        return prices.get((symbol, ts2))

    return src


def _async_price_source(prices: dict[tuple[str, int], float]):
    """Return an async source that returns close at ts2 keyed by (symbol, ts2)."""

    async def src(symbol: str, ts1: int, ts2: int) -> float | None:
        return prices.get((symbol, ts2))

    return src


# --------------------------------------------------------------------------- #
# Acceptance scenarios                                                         #
# --------------------------------------------------------------------------- #


def test_settles_pending_predictions(tmp_path: pathlib.Path) -> None:
    """Happy path: due predictions get settled with correct gross return."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)  # entry 100, exit 110 -> 0.10
    prices = {
        (SYMBOL, t0): 100.0,
        (SYMBOL, t0 + HORIZON): 110.0,
    }
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert len(appended) == 1
    rec = appended[0]
    assert rec.status == "settled"
    assert rec.realized_return_gross == pytest.approx(0.10)
    # net = gross - (5 + 3) bps = 0.10 - 0.0008
    assert rec.realized_return_net == pytest.approx(0.10 - 8e-4)
    assert rec.cost_breakdown_fee_bps == 5.0
    assert rec.cost_breakdown_spread_bps == 3.0
    assert rec.cost_breakdown_slippage_bps == 0.0
    assert rec.decision_window_start_ns == t0
    assert rec.decision_window_end_ns == t0 + HORIZON
    assert rec.settled_at_ns == NOW


def test_ignores_already_settled(tmp_path: pathlib.Path) -> None:
    """A second tick_sync is a no-op for already-settled rows."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 110.0}

    first = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert len(first) == 1

    second = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert second == []

    store = SettlementStore(root=sett_dir)
    rows = store.read_for_agent(AGENT)
    assert len(rows) == 1  # no duplicate settled row


def test_skips_future_predictions(tmp_path: pathlib.Path) -> None:
    """A prediction whose horizon has not elapsed is skipped (raises nothing)."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    # horizon ends strictly after NOW
    t0 = NOW - 100
    _write_prediction(log, ts_event=t0, horizon_ns=10_000_000_000)
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + 10_000_000_000): 110.0}

    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert appended == []
    store = SettlementStore(root=sett_dir)
    assert store.read_for_agent(AGENT) == []


def test_missing_market_data_is_pending_data(tmp_path: pathlib.Path) -> None:
    """Missing market data -> status='pending_data', no realized return."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)
    # no prices available
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source({}),
    )
    assert len(appended) == 1
    rec = appended[0]
    assert rec.status == "pending_data"
    assert rec.realized_return_gross is None
    assert rec.realized_return_net is None
    assert rec.brier_component is None
    assert rec.settled_at_ns is None


def test_idempotent_on_rerun(tmp_path: pathlib.Path) -> None:
    """Re-running tick_sync after a full settle appends nothing new."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 110.0}

    tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    appended_again = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert appended_again == []


# --------------------------------------------------------------------------- #
# QA scenarios                                                                 #
# --------------------------------------------------------------------------- #


def test_qa_happy_three_predictions_returns_in_set(tmp_path: pathlib.Path) -> None:
    """3 past predictions, fixture prices 100/110/120 -> gross ∈ {0.10, 0.20}."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 10 * HORIZON
    # Three predictions all entering at t0 (price 100).  Two distinct
    # exit timestamps (price 110 and 120) plus a duplicate exit give a
    # gross-return set of {0.10, 0.20}.
    _write_prediction(log, ts_event=t0, horizon_ns=HORIZON)  # exit -> 110
    _write_prediction(log, ts_event=t0, horizon_ns=2 * HORIZON)  # exit -> 120
    _write_prediction(log, ts_event=t0, horizon_ns=HORIZON)  # exit -> 110 (dup)
    prices = {
        (SYMBOL, t0): 100.0,
        (SYMBOL, t0 + HORIZON): 110.0,
        (SYMBOL, t0 + 2 * HORIZON): 120.0,
    }
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert len(appended) == 3
    assert {r.status for r in appended} == {"settled"}
    gross = sorted(r.realized_return_gross for r in appended)
    assert gross == pytest.approx([0.10, 0.10, 0.20])


def test_qa_pending_data_retry_yields_settled(tmp_path: pathlib.Path) -> None:
    """Missing data -> pending_data; retry with data -> settled."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)

    # First tick: no prices -> pending_data
    first = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source({}),
    )
    assert len(first) == 1
    assert first[0].status == "pending_data"

    # Second tick: prices now available -> settled (supersedes pending)
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 110.0}
    second = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert len(second) == 1
    assert second[0].status == "settled"
    assert second[0].realized_return_gross == pytest.approx(0.10)

    # Ledger retains both rows (append-only history).
    store = SettlementStore(root=sett_dir)
    rows = store.read_for_agent(AGENT)
    statuses = [r.status for r in rows]
    assert statuses == ["pending_data", "settled"]


def test_qa_pending_data_retry_still_missing_no_duplicate(tmp_path: pathlib.Path) -> None:
    """Retry while data is still missing does not append a second pending row."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)

    tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source({}),
    )
    second = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source({}),
    )
    assert second == []
    store = SettlementStore(root=sett_dir)
    rows = store.read_for_agent(AGENT)
    assert len(rows) == 1  # still just the one pending_data row


def test_brier_component_direction_up_actual_up(tmp_path: pathlib.Path) -> None:
    """Brier component is 0 when prob_up matches actual_up."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0, direction=1.0)  # prob_up = 1.0
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 110.0}  # actual_up=1
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert appended[0].brier_component == pytest.approx(0.0)


def test_brier_component_direction_up_actual_down(tmp_path: pathlib.Path) -> None:
    """Brier component is 1.0 when prob_up=1 but actual_up=0."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0, direction=1.0)  # prob_up = 1.0
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 90.0}  # actual_up=0
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert appended[0].brier_component == pytest.approx(1.0)


def test_brier_component_flat_direction_prob_half(tmp_path: pathlib.Path) -> None:
    """direction=0 -> prob_up=0.5; brier is 0.25 regardless of actual."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0, direction=0.0)  # prob_up = 0.5
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 110.0}  # actual_up=1
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert appended[0].brier_component == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# Async tick                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_async_tick_settles(tmp_path: pathlib.Path) -> None:
    """The async ``tick`` settles a due prediction identically to ``tick_sync``."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 110.0}
    appended = await tick(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_async_price_source(prices),
    )
    assert len(appended) == 1
    assert appended[0].status == "settled"
    assert appended[0].realized_return_gross == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_async_tick_missing_data_pending(tmp_path: pathlib.Path) -> None:
    """The async ``tick`` records pending_data when prices are missing."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    _write_prediction(log, ts_event=t0)
    appended = await tick(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_async_price_source({}),
    )
    assert len(appended) == 1
    assert appended[0].status == "pending_data"


def test_empty_predictions_dir_is_noop(tmp_path: pathlib.Path) -> None:
    """An empty/missing predictions dir yields no settlements and no error."""
    pred_dir = tmp_path / "predictions"  # does not exist
    sett_dir = tmp_path / "settlements"
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source({}),
    )
    assert appended == []


def test_multiple_agents_settled_in_one_pass(tmp_path: pathlib.Path) -> None:
    """Predictions from two agents are both settled in a single tick."""
    pred_dir = tmp_path / "predictions"
    sett_dir = tmp_path / "settlements"
    log = PredictionLog(predictions_dir=pred_dir)

    t0 = NOW - 5 * HORIZON
    log.append(
        agent_id="agent-a",
        model_name=MODEL,
        ts_event=t0,
        horizon_ns=HORIZON,
        symbol=SYMBOL,
        direction=1.0,
        confidence=0.7,
    )
    log.append(
        agent_id="agent-b",
        model_name=MODEL,
        ts_event=t0,
        horizon_ns=HORIZON,
        symbol=SYMBOL,
        direction=-1.0,
        confidence=0.6,
    )
    prices = {(SYMBOL, t0): 100.0, (SYMBOL, t0 + HORIZON): 110.0}
    appended = tick_sync(
        NOW,
        predictions_dir=pred_dir,
        settlements_dir=sett_dir,
        market_data_source=_price_source(prices),
    )
    assert len(appended) == 2
    agent_ids = {r.agent_id for r in appended}
    assert agent_ids == {"agent-a", "agent-b"}
