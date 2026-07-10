"""Tests for the orchestrator's RegimeSignal fan-out + handler."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fincept_core.events import Event
from fincept_core.schemas import Prediction, RegimeSignal

from orchestrator.main import (
    REGIME_HORIZON_NS,
    _make_regime_handler,
    _regime_to_predictions,
)


def _make_signal(
    regime: str = "risk_off",
    confidence: float = 0.7,
    *,
    agent_id: str = "regime_agent.v1",
    ts_event: int = 1_700_000_000_000_000_000,
) -> RegimeSignal:
    return RegimeSignal(
        agent_id=agent_id,
        ts_event=ts_event,
        regime=regime,
        confidence=confidence,
    )


class TestRegimeFanOut:
    def test_emits_one_prediction_per_universe_symbol(self) -> None:
        signal = _make_signal()
        universe = ["BTC-USD", "ETH-USD", "SOL-USD"]
        preds = _regime_to_predictions(signal, universe=universe)
        assert {p.symbol for p in preds} == set(universe)
        assert len(preds) == len(universe)

    def test_all_predictions_share_direction_and_confidence(self) -> None:
        signal = _make_signal(regime="risk_on", confidence=0.6)
        preds = _regime_to_predictions(signal, universe=["BTC-USD", "ETH-USD"])
        assert len({p.direction for p in preds}) == 1
        assert len({p.confidence for p in preds}) == 1
        # confidence passes through verbatim
        assert preds[0].confidence == pytest.approx(0.6)

    def test_risk_off_direction_is_negative(self) -> None:
        signal = _make_signal(regime="risk_off")
        preds = _regime_to_predictions(signal, universe=["BTC-USD"])
        assert preds[0].direction < 0

    def test_risk_on_direction_is_positive(self) -> None:
        signal = _make_signal(regime="risk_on")
        preds = _regime_to_predictions(signal, universe=["BTC-USD"])
        assert preds[0].direction > 0

    def test_unknown_regime_label_yields_zero_direction(self) -> None:
        """If the agent introduces a new label without updating
        REGIME_DIRECTION, fail closed (no signal) instead of crashing."""
        signal = _make_signal(regime="some_future_label", confidence=0.8)
        preds = _regime_to_predictions(signal, universe=["BTC-USD"])
        assert preds[0].direction == 0.0

    def test_horizon_uses_regime_default(self) -> None:
        signal = _make_signal()
        preds = _regime_to_predictions(signal, universe=["BTC-USD"])
        assert preds[0].horizon_ns == REGIME_HORIZON_NS

    def test_agent_id_passes_through(self) -> None:
        signal = _make_signal(agent_id="regime_v3")
        preds = _regime_to_predictions(signal, universe=["BTC-USD"])
        assert preds[0].agent_id == "regime_v3"

    def test_empty_universe_returns_empty_list(self) -> None:
        signal = _make_signal()
        assert _regime_to_predictions(signal, universe=[]) == []


class TestRegimeHandler:
    @pytest.mark.asyncio
    async def test_calls_router_once_per_symbol(self) -> None:
        router = AsyncMock()
        universe = ["BTC-USD", "ETH-USD", "SOL-USD"]
        handler = _make_regime_handler(router, universe=universe)
        event: Event[Any] = Event(type="regime", payload=_make_signal())

        await handler(event)

        assert router.on_prediction.await_count == len(universe)
        # Every call should be a Prediction.
        for call in router.on_prediction.await_args_list:
            (arg,) = call.args
            assert isinstance(arg, Prediction)

    @pytest.mark.asyncio
    async def test_ignores_wrong_event_type(self) -> None:
        router = AsyncMock()
        handler = _make_regime_handler(router, universe=["BTC-USD"])
        event: Event[Any] = Event(type="prediction", payload=_make_signal())

        await handler(event)

        router.on_prediction.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_wrong_payload_type(self) -> None:
        router = AsyncMock()
        handler = _make_regime_handler(router, universe=["BTC-USD"])

        class _Fake:
            type = "regime"
            payload = "not a RegimeSignal"

        await handler(_Fake())  # type: ignore[arg-type]

        router.on_prediction.assert_not_called()
