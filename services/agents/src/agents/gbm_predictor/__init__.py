"""
agents.gbm_predictor - LightGBM directional classifier.

Pipeline:

  Offline (CLI):
        agents.gbm_predictor.train.main(--input <bars.parquet> ...)
        -> writes models/gbm_predictor/{model.txt, meta.json}

  Online (long-running):
        agents.gbm_predictor.main.main()
        -> loads model + meta
        -> for each tick of cadence_s, for each universe symbol:
             reads latest FeatureFrame from OnlineStore
             extracts FEATURES vector
             scores via lightgbm Booster -> p(up)
             yields Prediction(direction = 2*p - 1, confidence = |2*p - 1|)
        -> Producer publishes each Prediction to STREAM_SIG_PREDICT

The orchestrator (TASK-040) consumes from STREAM_SIG_PREDICT and
combines this agent's signals with regime + pairs to produce Decisions.
"""

from agents.gbm_predictor.features import FEATURES, load_live
from agents.gbm_predictor.infer import GBMPredictor

__all__ = ["FEATURES", "GBMPredictor", "load_live"]
