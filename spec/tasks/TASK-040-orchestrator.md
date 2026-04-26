# TASK-040 · Orchestrator (singleton: fan-in, consensus, regime-adaptive allocation, decisions)

**Phase:** O · **Depends on:** TASK-031, TASK-032, TASK-033 · **Blocks:** TASK-044 (paper OMS)

## Goal

Consumer-group reader of all `sig.*` streams. For each symbol, maintain a sliding state of the latest predictions, regimes, sentiments. On each tick, compute a consensus direction + confidence, apply regime-adaptive weighting, and emit `Decision` events when a trading threshold is crossed. Must be a singleton (leader-elected).

## Files to create

```
services/orchestrator/
├── pyproject.toml
├── src/orchestrator/
│   ├── __init__.py
│   ├── main.py
│   ├── router.py           # fan-in reader
│   ├── state.py            # per-symbol sliding state
│   ├── consensus.py        # weighted fusion
│   ├── regime.py           # regime-adaptive weights
│   ├── allocator.py        # capital allocation → target notional
│   └── decisions.py        # emitter
└── tests/
    ├── test_consensus.py
    └── test_allocator.py
```

## Contracts

### `state.py`

```python
from dataclasses import dataclass, field
from fincept_core.schemas import Prediction, SentimentSignal, RegimeSignal

@dataclass
class SymbolState:
    symbol: str
    predictions: dict[str, Prediction] = field(default_factory=dict)    # agent_id -> latest
    sentiments: dict[str, SentimentSignal] = field(default_factory=dict)
    regime: RegimeSignal | None = None

    def update(self, ev) -> None:
        if isinstance(ev, Prediction):
            self.predictions[ev.agent_id] = ev
        elif isinstance(ev, SentimentSignal):
            self.sentiments[ev.agent_id] = ev
        elif isinstance(ev, RegimeSignal):
            self.regime = ev
```

### `consensus.py`

```python
from dataclasses import dataclass
from .state import SymbolState

@dataclass
class Consensus:
    direction: float     # [-1, +1]
    confidence: float    # [0, 1]
    rationale: str
    sources: list[str]

def fuse(state: SymbolState, agent_weights: dict[str, float]) -> Consensus:
    """Weighted average across predictions (using agent weights) + sentiment nudge."""
    if not state.predictions:
        return Consensus(direction=0.0, confidence=0.0, rationale="no signals", sources=[])
    total_w = 0.0
    weighted_dir = 0.0
    sources: list[str] = []
    for aid, p in state.predictions.items():
        w = agent_weights.get(aid, 1.0) * p.confidence
        weighted_dir += w * p.direction
        total_w += w
        sources.append(aid)
    direction = weighted_dir / total_w if total_w > 0 else 0.0
    # Sentiment nudge: bounded by ±0.2
    sent_adj = 0.0
    for s in state.sentiments.values():
        sent_adj += 0.1 * s.score * s.confidence
        sources.append(s.agent_id)
    sent_adj = max(-0.2, min(0.2, sent_adj))
    direction = max(-1.0, min(1.0, direction + sent_adj))
    confidence = min(1.0, total_w / max(1, len(state.predictions)))
    return Consensus(direction=direction, confidence=confidence,
                     rationale=f"consensus from {len(sources)} sources", sources=sources)
```

### `regime.py`

```python
from .state import SymbolState

def agent_weights_for(state: SymbolState) -> dict[str, float]:
    """Given detected regime, return weights for each agent_id. Default weights when no regime."""
    default = {"gbm_predictor.v1": 1.0, "ts_foundation.v1": 1.0, "pairs.v1": 1.0}
    if state.regime is None:
        return default
    r = state.regime.regime
    if r == "trend_up" or r == "trend_down":
        return {"gbm_predictor.v1": 0.7, "ts_foundation.v1": 1.3, "pairs.v1": 0.3}
    if r == "mean_revert":
        return {"gbm_predictor.v1": 0.8, "ts_foundation.v1": 0.7, "pairs.v1": 1.5}
    if r == "high_vol":
        return {"gbm_predictor.v1": 0.5, "ts_foundation.v1": 0.5, "pairs.v1": 0.8}
    if r == "low_liq":
        return {"gbm_predictor.v1": 0.3, "ts_foundation.v1": 0.3, "pairs.v1": 0.5}
    return default
```

### `allocator.py`

```python
from decimal import Decimal
from fincept_core.config import get_settings
from .consensus import Consensus

def target_notional(symbol: str, consensus: Consensus, threshold: float = 0.3) -> Decimal:
    """Translate consensus → target $ notional (signed). Zero if below threshold."""
    s = get_settings()
    if abs(consensus.direction) * consensus.confidence < threshold:
        return Decimal(0)
    cap = Decimal(str(s.max_notional_usd_per_symbol))
    # scale linearly with direction*conf; sign preserved
    scaled = Decimal(str(consensus.direction * consensus.confidence)) * cap
    return scaled
```

### `decisions.py`

```python
from decimal import Decimal
from fincept_core.clock import now_ns
from fincept_core.ids import new_id
from fincept_core.schemas import Decision, Side

def build(symbol: str, consensus, target_usd: Decimal, strategy_id: str = "ensemble.v1") -> Decision | None:
    if target_usd == 0:
        return None
    side = Side.BUY if target_usd > 0 else Side.SELL
    return Decision(
        decision_id=new_id(), ts_event=now_ns(), strategy_id=strategy_id,
        symbol=symbol, side=side, target_notional_usd=abs(target_usd),
        urgency=min(1.0, consensus.confidence), rationale=consensus.rationale,
        source_signals=consensus.sources,
    )
```

### `main.py`

```python
import asyncio, socket
from redis.asyncio import Redis
from fincept_core.config import get_settings
from fincept_core.logging import configure_logging, get_logger
from fincept_core.leadership import Leader
from fincept_core.schemas import Prediction, SentimentSignal, RegimeSignal
from fincept_bus.producer import Producer
from fincept_bus.consumer import Consumer
from fincept_bus.streams import (
    STREAM_SIG_PREDICT, STREAM_SIG_SENT, STREAM_SIG_REGIME, STREAM_DECISIONS,
)
from .state import SymbolState
from .consensus import fuse
from .regime import agent_weights_for
from .allocator import target_notional
from .decisions import build

configure_logging()
log = get_logger(__name__)

STATE: dict[str, SymbolState] = {}
def ensure(sym: str) -> SymbolState:
    st = STATE.get(sym)
    if st is None:
        st = STATE[sym] = SymbolState(symbol=sym)
    return st

async def consume_one(consumer: Consumer, stream: str, model_cls, producer: Producer):
    async for mid, env in consumer.read(stream, model_cls):
        sym = env.payload.symbol if hasattr(env.payload, "symbol") else None
        if sym is None and isinstance(env.payload, RegimeSignal):
            # regime may be global; apply to all symbols in universe
            for s in get_settings().universe:
                ensure(s).update(env.payload)
        elif sym:
            ensure(sym).update(env.payload)
        await consumer.ack(stream, mid)
        await maybe_decide(sym, producer)

async def maybe_decide(sym: str | None, producer: Producer) -> None:
    if sym is None:
        return
    st = STATE.get(sym)
    if st is None or not st.predictions:
        return
    weights = agent_weights_for(st)
    cons = fuse(st, weights)
    target = target_notional(sym, cons)
    dec = build(sym, cons, target)
    if dec is not None:
        await producer.publish(STREAM_DECISIONS, dec)
        log.info("decision", symbol=sym, side=dec.side.value, notional=str(dec.target_notional_usd))

async def run() -> None:
    s = get_settings()
    redis = Redis.from_url(s.redis_url)
    leader = Leader(redis, role="orchestrator")
    await leader.start()
    try:
        # wait until leader
        while not leader.is_leader:
            await asyncio.sleep(0.5)
        producer = Producer(redis)
        cid = f"orch-{socket.gethostname()}"
        c_pred = Consumer(redis, "orch", f"{cid}-pred")
        c_sent = Consumer(redis, "orch", f"{cid}-sent")
        c_reg = Consumer(redis, "orch", f"{cid}-reg")
        await asyncio.gather(
            consume_one(c_pred, STREAM_SIG_PREDICT, Prediction, producer),
            consume_one(c_sent, STREAM_SIG_SENT, SentimentSignal, producer),
            consume_one(c_reg, STREAM_SIG_REGIME, RegimeSignal, producer),
        )
    finally:
        await leader.stop()
        await redis.aclose()

def main() -> None:
    asyncio.run(run())

if __name__ == "__main__":
    main()
```

## Tests

### `tests/test_consensus.py`

```python
from decimal import Decimal
from fincept_core.schemas import Prediction, RegimeSignal
from orchestrator.state import SymbolState
from orchestrator.consensus import fuse
from orchestrator.regime import agent_weights_for
from orchestrator.allocator import target_notional

def make_pred(aid, d, c):
    return Prediction(agent_id=aid, symbol="BTC-USD", horizon_ns=1, ts_event=0, direction=d, confidence=c)

def test_fuse_and_allocate(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("MAX_NOTIONAL_USD_PER_SYMBOL", "1000")
    s = SymbolState(symbol="BTC-USD")
    s.update(make_pred("gbm_predictor.v1", 0.8, 0.9))
    s.update(make_pred("ts_foundation.v1", 0.4, 0.8))
    s.regime = RegimeSignal(agent_id="regime.v1", ts_event=0, regime="trend_up", confidence=0.9)
    w = agent_weights_for(s)
    cons = fuse(s, w)
    assert cons.direction > 0.3
    notional = target_notional("BTC-USD", cons)
    assert notional > Decimal(0)
```

## Out of scope

- LLM reflection loop — TASK-064
- Portfolio-level optimization — defer to Phase X

## Done when

- [ ] Files exist
- [ ] `pytest services/orchestrator/tests` green
- [ ] Manual: publish synthetic predictions → observe `ord.decisions` emissions
