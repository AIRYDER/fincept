"""
orchestrator.router - stateful async pipeline.

Glues consensus + allocator + decisions to the bus.  One method
(:meth:`OrchestratorRouter.on_prediction`) is the per-event handler;
the long-running entrypoint in ``main`` wires it to a Consumer.

The router is also responsible for:

  - **Deadband filtering**: if the new target is within
    ``min_delta_usd`` of the last emitted target, no order is sent.
    Without this, every prediction cycle would generate a tiny
    rebalance and saturate the OMS / wear out broker latency.

  - **Price-availability gating**: target -> quantity needs a price.
    If LivePrices doesn't have one for the symbol, we skip rather
    than emit an order with stale data.

  - **Audit log**: every emission appends an entry under
    ``actor="orchestrator"`` with the decision_id as correlation_id,
    so the blotter (TASK-046) can join Decision -> OrderIntent ->
    Order -> Fill via that key.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal

from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_DECISIONS, STREAM_ORDERS
from fincept_core.clock import now_ns
from fincept_core.events import Event
from fincept_core.logging import get_logger
from fincept_core.schemas import Prediction, Venue
from fincept_db import audit

from oms.prices import LivePrices

from orchestrator.allocator import target_notional
from orchestrator.consensus import ConsensusBuilder
from orchestrator.decisions import (
    TargetState,
    build_decision_and_intent,
)

log = get_logger(__name__)

DEFAULT_MIN_DELTA_USD = Decimal("100")
DEFAULT_STRATEGY_ID = "orchestrator.v1"


class OrchestratorRouter:
    """Per-prediction pipeline; constructed once, called many times."""

    def __init__(
        self,
        *,
        producer: Producer,
        prices: LivePrices,
        consensus: ConsensusBuilder,
        target_state: TargetState,
        cap_per_symbol: Decimal,
        min_delta_usd: Decimal = DEFAULT_MIN_DELTA_USD,
        confidence_threshold: float = 0.1,
        strategy_id: str = DEFAULT_STRATEGY_ID,
        venue: Venue = Venue.ALPACA,
    ) -> None:
        self._producer = producer
        self._prices = prices
        self._consensus = consensus
        self._target_state = target_state
        self._cap_per_symbol = cap_per_symbol
        self._min_delta_usd = min_delta_usd
        self._confidence_threshold = confidence_threshold
        self._strategy_id = strategy_id
        self._venue = venue

    async def on_prediction(self, prediction: Prediction) -> None:
        """Single per-prediction step of the pipeline.

        Returns silently when no action is taken (deadband, missing
        price, no consensus).  Logs each emission at INFO so production
        operators can tail orchestrator activity.
        """
        self._consensus.update(prediction)

        consensus = self._consensus.consensus(prediction.symbol, now_ns=now_ns())
        if consensus is None:
            return

        new_target = target_notional(
            direction=consensus.direction,
            confidence=consensus.confidence,
            cap_per_symbol=self._cap_per_symbol,
            confidence_threshold=self._confidence_threshold,
        )
        delta = self._target_state.delta(prediction.symbol, new_target)

        if delta == 0 or delta.copy_abs() < self._min_delta_usd:
            return

        last_price = self._prices.get(prediction.symbol)
        if last_price is None:
            log.warning(
                "orchestrator.no_price",
                symbol=prediction.symbol,
                target=str(new_target),
            )
            return

        rationale = (
            f"consensus(dir={consensus.direction:+.3f},"
            f"conf={consensus.confidence:.3f}) -> target={new_target}"
        )
        decision, intent = build_decision_and_intent(
            symbol=prediction.symbol,
            delta_notional=delta,
            last_price=last_price,
            strategy_id=self._strategy_id,
            ts_event=now_ns(),
            rationale=rationale,
            source_signals=list(consensus.contributing_agents),
            venue=self._venue,
        )

        await self._producer.publish(
            STREAM_DECISIONS, Event(type="decision", payload=decision)
        )
        await self._producer.publish(
            STREAM_ORDERS, Event(type="order_intent", payload=intent)
        )
        with contextlib.suppress(Exception):
            await audit.append(
                actor="orchestrator",
                event_type="orchestrator.decision",
                payload={
                    "decision": decision.model_dump(mode="json"),
                    "order_intent": intent.model_dump(mode="json"),
                    "consensus": {
                        "direction": consensus.direction,
                        "confidence": consensus.confidence,
                        "contributing_agents": list(consensus.contributing_agents),
                    },
                    "target_notional": str(new_target),
                    "delta_notional": str(delta),
                },
                correlation_id=decision.decision_id,
            )

        self._target_state.update(prediction.symbol, new_target)
        log.info(
            "orchestrator.emitted",
            symbol=prediction.symbol,
            side=intent.side.value,
            quantity=str(intent.quantity),
            target_notional=str(new_target),
            delta_notional=str(delta),
            decision_id=decision.decision_id,
            order_id=intent.order_id,
        )
