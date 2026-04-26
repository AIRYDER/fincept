# TASK-031 · Agent base + `gbm_predictor` (LightGBM direction agent)

**Phase:** A · **Depends on:** TASK-017 (features), TASK-002 · **Blocks:** TASK-040 (orchestrator)

## Goal

Agent process that loads a trained LightGBM model, reads live features from the online store, emits `Prediction` events at a fixed cadence, and supports shadow evaluation.

## Files to create

```
services/agents/
├── pyproject.toml
├── src/agents/
│   ├── __init__.py
│   ├── base.py
│   └── gbm_predictor/
│       ├── __init__.py
│       ├── main.py
│       ├── train.py         # offline CLI to train a model
│       ├── infer.py         # online inference loop
│       └── features.py      # wire to feature store
└── tests/
    └── test_gbm_infer.py
```

## Contracts

### `base.py`

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator
from pydantic import BaseModel

class Agent(ABC):
    agent_id: str

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def run(self) -> AsyncIterator[BaseModel]: ...

    @abstractmethod
    async def teardown(self) -> None: ...
```

### `gbm_predictor/features.py`

```python
from features.store import OnlineStore  # service path; import at runtime

FEATURES: list[str] = [
    "ret_1m", "ret_5m", "ret_15m", "ret_60m",
    "rv_5m", "rv_30m",
    "mom_z_30m", "mom_z_240m",
    "book_imbalance_1", "spread_bps",
]

async def load_live(store: OnlineStore, symbol: str) -> dict[str, float] | None:
    row = await store.get_row(symbol, FEATURES)
    if any(v is None for v in row.values()):
        return None
    return {k: float(v) for k, v in row.items()}
```

### `gbm_predictor/train.py`

```python
"""Offline trainer. Run: python -m agents.gbm_predictor.train --symbol BTC-USD --start 2023-01-01 --end 2024-12-31"""
import argparse, json, pathlib
import lightgbm as lgb
import numpy as np
import polars as pl
from .features import FEATURES

def build_dataset(df: pl.DataFrame, horizon_bars: int) -> tuple[np.ndarray, np.ndarray]:
    # label = sign of forward return over horizon_bars
    df = df.with_columns(
        forward=(pl.col("close").shift(-horizon_bars) / pl.col("close") - 1).alias("forward"),
    ).drop_nulls("forward")
    y = (df["forward"] > 0).to_numpy().astype(int)
    X = df.select(FEATURES).to_numpy()
    return X, y

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)    # parquet with bars + features
    ap.add_argument("--horizon-bars", type=int, default=15)
    ap.add_argument("--out-dir", default="models/gbm_predictor")
    args = ap.parse_args()
    df = pl.read_parquet(args.input)
    X, y = build_dataset(df, args.horizon_bars)
    # walk-forward: simple 80/20 holdout for demo; real pipeline uses TASK-023
    split = int(len(X) * 0.8)
    dtrain = lgb.Dataset(X[:split], y[:split])
    dval = lgb.Dataset(X[split:], y[split:], reference=dtrain)
    params = {"objective": "binary", "metric": "auc", "learning_rate": 0.05, "num_leaves": 63}
    model = lgb.train(params, dtrain, num_boost_round=500, valid_sets=[dval],
                      callbacks=[lgb.early_stopping(30)])
    out = pathlib.Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out / "model.txt"))
    (out / "meta.json").write_text(json.dumps({
        "features": FEATURES, "horizon_bars": args.horizon_bars,
    }))
    print(f"Saved to {out}")

if __name__ == "__main__":
    main()
```

### `gbm_predictor/infer.py`

```python
import asyncio, json, pathlib
from typing import AsyncIterator
import lightgbm as lgb
import numpy as np
from redis.asyncio import Redis
from pydantic import BaseModel
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.schemas import Prediction
from fincept_core.logging import get_logger
from features.store import OnlineStore
from ..base import Agent
from .features import FEATURES, load_live

log = get_logger(__name__)

class GBMPredictor(Agent):
    agent_id = "gbm_predictor.v1"

    def __init__(self, model_dir: str, redis: Redis, cadence_s: float = 60.0) -> None:
        self.model_dir = pathlib.Path(model_dir)
        self.redis = redis
        self.cadence_s = cadence_s
        self.model: lgb.Booster | None = None
        self.horizon_bars: int = 15
        self.store = OnlineStore(redis)

    async def setup(self) -> None:
        self.model = lgb.Booster(model_file=str(self.model_dir / "model.txt"))
        meta = json.loads((self.model_dir / "meta.json").read_text())
        self.horizon_bars = int(meta["horizon_bars"])
        log.info("gbm.loaded", features=meta["features"])

    async def run(self) -> AsyncIterator[BaseModel]:
        assert self.model is not None
        settings = get_settings()
        while True:
            for sym in settings.universe:
                row = await load_live(self.store, sym)
                if row is None:
                    continue
                X = np.array([[row[f] for f in FEATURES]])
                prob_up = float(self.model.predict(X)[0])
                direction = 2 * prob_up - 1  # [-1,+1]
                conf = abs(direction)
                yield Prediction(
                    agent_id=self.agent_id, symbol=sym,
                    horizon_ns=self.horizon_bars * 60 * 1_000_000_000,
                    ts_event=now_ns(), direction=direction, confidence=conf,
                    calibration_tag="gbm.v1",
                )
            await asyncio.sleep(self.cadence_s)

    async def teardown(self) -> None:
        pass
```

### `gbm_predictor/main.py`

```python
import asyncio, signal, os
from redis.asyncio import Redis
from fincept_core.config import get_settings
from fincept_core.logging import configure_logging, get_logger
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_PREDICT
from .infer import GBMPredictor

configure_logging()
log = get_logger(__name__)

async def run() -> None:
    s = get_settings()
    redis = Redis.from_url(s.redis_url)
    model_dir = os.getenv("GBM_MODEL_DIR", "models/gbm_predictor")
    agent = GBMPredictor(model_dir, redis)
    producer = Producer(redis)
    await agent.setup()
    try:
        async for pred in agent.run():
            await producer.publish(STREAM_SIG_PREDICT, pred)
            log.info("gbm.pred", symbol=pred.symbol, direction=pred.direction, conf=pred.confidence)
    finally:
        await agent.teardown()
        await redis.aclose()

def main() -> None:
    asyncio.run(run())

if __name__ == "__main__":
    main()
```

## Tests

### `tests/test_gbm_infer.py`

```python
import pytest, json, pathlib
import numpy as np, lightgbm as lgb

def test_train_and_load(tmp_path):
    X = np.random.rand(1000, 10); y = (X[:, 0] > 0.5).astype(int)
    dtrain = lgb.Dataset(X, y)
    m = lgb.train({"objective":"binary","metric":"auc","num_leaves":15,"learning_rate":0.1},
                  dtrain, num_boost_round=10)
    p = tmp_path / "m.txt"; m.save_model(str(p))
    loaded = lgb.Booster(model_file=str(p))
    preds = loaded.predict(X[:5])
    assert len(preds) == 5 and all(0 <= x <= 1 for x in preds)
```

## Out of scope

- Walk-forward / purged CV — TASK-023
- Registry / MLflow — TASK-064
- Shadow deployment harness — TASK-065

## Done when

- [ ] Files exist
- [ ] Training CLI produces `model.txt` + `meta.json`
- [ ] `main.py` publishes at least one `Prediction` per universe symbol within 2 minutes
