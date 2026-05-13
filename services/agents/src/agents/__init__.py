"""
agents - strategy agents that consume features and emit Prediction events.

Each agent is a long-running async process that:

  1. Reads live features from the online feature store (TASK-017).
  2. Runs inference (statistical model, ML model, LLM, etc.).
  3. Emits a ``Prediction`` event to ``STREAM_SIG_PREDICT``.

The orchestrator (TASK-040) consumes predictions, fans them through
regime weighting + consensus, and emits ``Decision`` events to the OMS.

This package is the home for v1 baseline (non-LLM) agents:
  - ``gbm_predictor``          LightGBM directional classifier (TASK-031)
  - ``regime_agent``           FRED-based rule-heuristic regime detector (TASK-032)
  - ``sentiment_agent``        NewsAPI + LLM sentiment scorer (optional, key-gated)
  - ``sentiment_features``     Sentiment signal → feature store bridge
  - ``information_enricher``   Raw information event → enriched stream
  - ``news_alpha_predictor``   News-alpha ML predictor (optional, key-gated)
  - ``news_outcome_labeler``   Outcome labeler for news events
  - ``pairs``                  Cointegration pairs trader (TASK-033) - stub

Public surface: just ``Agent`` (the abstract base).  Each concrete agent
ships its own ``main`` entrypoint - see ``agents/gbm_predictor/main.py``.
"""

from agents.base import Agent

__all__ = ["Agent"]
