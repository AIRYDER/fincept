"""Tests for orchestrator.decisions: TargetState + build_decision_and_intent."""

from __future__ import annotations

from decimal import Decimal

import pytest

from fincept_core.schemas import OrderType, Side, TimeInForce, Venue
from orchestrator.decisions import TargetState, build_decision_and_intent


# ---------------------------------------------------------------------------
# TargetState
# ---------------------------------------------------------------------------


def test_target_state_starts_empty() -> None:
    st = TargetState()
    assert st.delta("BTC-USD", Decimal("5000")) == Decimal("5000")
    assert st.known_symbols() == set()


def test_target_state_delta_after_update() -> None:
    st = TargetState()
    st.update("BTC-USD", Decimal("5000"))
    assert st.delta("BTC-USD", Decimal("8000")) == Decimal("3000")
    assert st.delta("BTC-USD", Decimal("5000")) == Decimal("0")
    assert st.delta("BTC-USD", Decimal("-2000")) == Decimal("-7000")


def test_target_state_clear() -> None:
    st = TargetState()
    st.update("BTC-USD", Decimal("5000"))
    st.update("ETH-USD", Decimal("3000"))
    assert st.known_symbols() == {"BTC-USD", "ETH-USD"}
    st.clear("BTC-USD")
    assert st.known_symbols() == {"ETH-USD"}
    # Delta against cleared symbol is the new target itself.
    assert st.delta("BTC-USD", Decimal("100")) == Decimal("100")


def test_target_state_per_symbol_isolation() -> None:
    st = TargetState()
    st.update("BTC-USD", Decimal("5000"))
    assert st.delta("ETH-USD", Decimal("3000")) == Decimal("3000")


# ---------------------------------------------------------------------------
# build_decision_and_intent
# ---------------------------------------------------------------------------


def test_buy_intent_for_positive_delta() -> None:
    decision, intent = build_decision_and_intent(
        symbol="BTC-USD",
        delta_notional=Decimal("5000"),
        last_price=Decimal("50000"),
        strategy_id="orchestrator.v1",
        ts_event=1_000_000_000,
        rationale="test",
        source_signals=["gbm.v1"],
    )
    assert intent.side == Side.BUY
    assert decision.side == Side.BUY
    # 5000 / 50000 = 0.1 BTC
    assert intent.quantity == Decimal("0.10000000")
    assert intent.order_type == OrderType.MARKET
    assert intent.time_in_force == TimeInForce.GTC
    assert intent.venue == Venue.ALPACA


def test_sell_intent_for_negative_delta() -> None:
    decision, intent = build_decision_and_intent(
        symbol="BTC-USD",
        delta_notional=Decimal("-3000"),
        last_price=Decimal("30000"),
        strategy_id="orchestrator.v1",
        ts_event=1_000_000_000,
        rationale="test",
        source_signals=["gbm.v1"],
    )
    assert intent.side == Side.SELL
    assert decision.side == Side.SELL
    assert intent.quantity == Decimal("0.10000000")
    # Decision target_notional is unsigned magnitude.
    assert decision.target_notional_usd == Decimal("3000")


def test_decision_and_intent_share_decision_id() -> None:
    decision, intent = build_decision_and_intent(
        symbol="BTC-USD",
        delta_notional=Decimal("5000"),
        last_price=Decimal("50000"),
        strategy_id="orchestrator.v1",
        ts_event=1,
        rationale="r",
        source_signals=[],
    )
    assert intent.decision_id == decision.decision_id
    # IDs are distinct - order_id != decision_id by design.
    assert intent.order_id != decision.decision_id


def test_decision_carries_source_signals() -> None:
    decision, _ = build_decision_and_intent(
        symbol="BTC-USD",
        delta_notional=Decimal("5000"),
        last_price=Decimal("50000"),
        strategy_id="orchestrator.v1",
        ts_event=1,
        rationale="r",
        source_signals=["gbm.v1", "regime.v1"],
    )
    assert decision.source_signals == ["gbm.v1", "regime.v1"]


def test_intent_tags_carry_strategy_id() -> None:
    _, intent = build_decision_and_intent(
        symbol="BTC-USD",
        delta_notional=Decimal("5000"),
        last_price=Decimal("50000"),
        strategy_id="orchestrator.v1",
        ts_event=1,
        rationale="r",
        source_signals=[],
    )
    assert intent.tags == {"orchestrator": "orchestrator.v1"}


def test_zero_delta_raises() -> None:
    with pytest.raises(ValueError, match="delta_notional"):
        build_decision_and_intent(
            symbol="BTC-USD",
            delta_notional=Decimal(0),
            last_price=Decimal("50000"),
            strategy_id="orchestrator.v1",
            ts_event=1,
            rationale="r",
            source_signals=[],
        )


def test_non_positive_price_raises() -> None:
    with pytest.raises(ValueError, match="last_price"):
        build_decision_and_intent(
            symbol="BTC-USD",
            delta_notional=Decimal(5000),
            last_price=Decimal(0),
            strategy_id="orchestrator.v1",
            ts_event=1,
            rationale="r",
            source_signals=[],
        )


def test_quantity_quantized_to_eight_decimals() -> None:
    """1/30000 BTC has many decimals; we round to 8."""
    _, intent = build_decision_and_intent(
        symbol="BTC-USD",
        delta_notional=Decimal("1"),
        last_price=Decimal("30000"),
        strategy_id="orchestrator.v1",
        ts_event=1,
        rationale="r",
        source_signals=[],
    )
    # 1/30000 = 0.00003333...; quantize to 8 decimals -> 0.00003333
    assert intent.quantity == Decimal("0.00003333")
