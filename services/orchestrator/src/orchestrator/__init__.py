"""
orchestrator - turns Predictions into Decisions + OrderIntents.

Pipeline (one async loop, per-prediction):

  STREAM_SIG_PREDICT  ->  Prediction event arrives
                          |
                          v
            ConsensusBuilder.update(prediction)
                          |
                          v
            ConsensusBuilder.consensus(symbol, now_ns)
                       returns (direction, confidence) or None
                          |
                          v
            allocator.target_notional(direction, confidence, cap)
                       returns signed Decimal in USD
                          |
                          v
            TargetState.delta(symbol, new_target)
                       returns delta_notional vs last emission
                          |
                          v
            (deadband: skip if |delta| < min_delta_usd)
                          |
                          v
            decisions.build_decision_and_intent(symbol, delta, ...)
                          |
                          v
            STREAM_DECISIONS  <-  Decision (audit trail)
            STREAM_ORDERS     <-  OrderIntent (consumed by OMS)

Module map (matches BUILD_ORDER's spec layout):

  consensus.py   per-symbol multi-agent prediction aggregator
  allocator.py   pure (direction, confidence) -> target notional
  decisions.py   pure target_notional + price -> (Decision, OrderIntent)
  router.py      stateful async pipeline gluing the above + bus + audit
  main.py        long-running entrypoint with signal handling
  regime.py      DEFERRED: regime-adaptive weighting (TASK-032 dep)

The package is target-portfolio-aware: it tracks last-emitted target
notional per symbol, so a stable signal doesn't churn the OMS with
duplicate intents.  Position-aware rebalancing (against actual filled
positions, not last-emitted targets) is a Phase H concern - the v1
abstraction is good enough as long as orders broadly fill.
"""

from orchestrator.allocator import target_notional
from orchestrator.consensus import ConsensusBuilder
from orchestrator.decisions import (
    TargetState,
    build_decision_and_intent,
)
from orchestrator.router import OrchestratorRouter

__all__ = [
    "ConsensusBuilder",
    "OrchestratorRouter",
    "TargetState",
    "build_decision_and_intent",
    "target_notional",
]
