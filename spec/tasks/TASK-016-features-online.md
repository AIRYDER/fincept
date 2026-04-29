# TASK-016 · Online feature transforms (returns, vol, microstructure, cross)

**Phase:** D · **Depends on:** TASK-002, TASK-003, TASK-004, TASK-010 · **Blocks:** TASK-017 (feature store + PIT joins), TASK-031 (GBM)

**Status:** [x] Implemented and verified.

## As-built deviations from the original draft

| Spec said | We did | Why |
|---|---|---|
| Three transform families plus a `microstructure.py` | Shipped `price.py`, `volatility.py`, `cross.py` only | Spec's own out-of-scope says "Tick-level features (microstructure) — stub only in v1; full implementation in TASK-096 (Phase Y)." Done-when checklist doesn't require microstructure features. Skipping the stub file avoids dead code + a misleading import surface. |
| `consumer.read(STREAM, BarEvent)` async-iterator API | Wired through the existing `Consumer.consume(streams, group, consumer_name, handler)` API with `OnlineRunner.handle_event` as the dispatcher | Spec snippet didn't match the actual `fincept_bus.Consumer` shape from earlier tasks. Aligning with the existing API keeps the bus contract uniform — `quality_main.py` uses the same pattern. |
| Loader did `await self.producer.publish(STREAM, ff)` directly with a `FeatureFrame` | Wrapped: `Event(type="feature_frame", payload=ff)` | `Producer.publish` is typed `(stream, Event)`. Extending `Event.payload` union and `_EVENT_SCHEMAS["feature_frame"]` keeps every stream message a deserializable Event — same pattern as `AlertEvent` from TASK-014. |
| Inline `Producer(redis)` and `get_settings().universe[0]` (lowercase) inside `OnlineRunner` | Constructor takes any `_FeaturePublisher` (Protocol with `async publish(stream, Event)`) and an optional `benchmark_symbol`; `_default_benchmark()` reads `Settings.UNIVERSE` (correct uppercase) and falls back to `BTC-USD` | DI lets tests inject `FakeProducer` (capture-list) and call `on_bar` directly without Redis, a consumer loop, or settings setup. The lowercase typo would've crashed at startup. |
| Test snippet relied on imports working without unique-feature-key transform-level docs | Each transform exposes a `feature_keys` property, and the bootstrap path uses `dict.fromkeys(self.feature_keys)` so missing windows always emit `None` for every key | Spec landmine #3 (bootstrap nulls): downstream consumers must see consistent key-sets, not a moving feature surface. |
| Test for "perfect correlation" used `sym = bench` only | Added 6 cross-feature tests including beta = 2 when sym = 2*bench, beta = None on constant benchmark (var = 0), and a runner-level test that ETH bars use the BTC bench deque positionally | Pinned the corner cases the spec lists as landmines — no defaulting to zero, no NaNs leaking out. |
| Volatility used parameter name `l` for bar low | Renamed to `low` | Ruff E741 — single-letter `l` is ambiguous with `1`. |
| Garman-Klass formula returns NaN-prone values for degenerate bars | `_garman_klass` returns `None` when accumulator ≤ 0 instead of `sqrt(negative)` | The GK estimator is a sample formula that can dip below zero on close-to-open-dominated bars. Pinned by `test_gk_returns_none_when_close_to_open_dominates`. |
| Tests asserted "beta ≈ 1.0" for the non-benchmark-uses-bench-history case | Test uses 61 bars on both BTC and ETH (matched counts) so the last-60 windows are positionally aligned with the same alternation phase | With mismatched bar counts the deque tails fall out of phase and beta flips sign — that's mathematically correct but a fragile test target. Documented inline. |

## Goal

Real-time feature engineering. Subscribes to `md.bars.1m` (and optionally `md.trades`, `md.books`), computes per-symbol features incrementally as bars close, and publishes a `FeatureFrame` for each (symbol, ts_event) to a `features.online` Redis stream. Implements four families:

1. **Price** — log returns, simple returns, momentum.
2. **Volatility** — realized vol (rolling std of returns), Parkinson, Garman-Klass.
3. **Microstructure** — top-of-book imbalance, effective spread (per-bar averages from L2 stream).
4. **Cross-sectional** — beta vs benchmark (e.g., BTC), pairwise correlation, cross-sectional z-score.

All transforms are **online + incremental**: state evolves bar-by-bar, no full re-scan. Identical math is reused offline (TASK-017) for backfill + PIT joins.

## Files to create

```
services/features/
├── pyproject.toml
├── src/features/
│   ├── __init__.py
│   ├── main.py                       # entrypoint: subscribe + dispatch
│   ├── online.py                     # online runner / state holder
│   └── transforms/
│       ├── __init__.py
│       ├── price.py                  # returns, momentum
│       ├── volatility.py             # realized vol, Parkinson, Garman-Klass
│       ├── microstructure.py         # imbalance, eff spread (placeholder for L2)
│       └── cross.py                  # beta, correlation, z-score
└── tests/
    ├── test_price.py
    ├── test_volatility.py
    ├── test_cross.py
    └── test_online_runner.py

# Schema additions to fincept-core:
libs/fincept-core/src/fincept_core/schemas.py   # add FeatureFrame
libs/fincept-bus/src/fincept_bus/streams.py     # add STREAM_FEATURES_ONLINE
```

## Schema additions

```python
# fincept_core/schemas.py — add to existing file
class FeatureFrame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: int = 1
    symbol: str
    ts_event: int                                # bar close ns; PIT-as-of
    freq: str                                    # "1m" | "1h" | "1d"
    values: dict[str, float | None]              # feature_name → value (None = insufficient data)
    tags: dict[str, str] = Field(default_factory=dict)  # e.g., {"micro_book_top_n":"5"}
```

```python
# fincept_bus/streams.py — add
STREAM_FEATURES_ONLINE = "features.online"       # 14-day retention, maxlen ~5M
```

Add `FeatureFrame` to `spec/CONTRACTS.md §3` and the new stream to `§6`.

## Contracts

### `transforms/price.py`

```python
from collections import deque
from decimal import Decimal
import math

class PriceFeatures:
    """Online: maintains last N closes, emits log/simple returns and momentum."""
    def __init__(self, max_lookback: int = 240) -> None:
        self.closes: deque[float] = deque(maxlen=max_lookback + 1)

    def update(self, close: Decimal) -> dict[str, float | None]:
        self.closes.append(float(close))
        out: dict[str, float | None] = {}
        if len(self.closes) < 2:
            return {"ret_log_1": None, "ret_simple_1": None, "mom_5": None, "mom_20": None, "mom_60": None}
        c0, c_prev = self.closes[-1], self.closes[-2]
        out["ret_log_1"] = math.log(c0 / c_prev) if c_prev > 0 else None
        out["ret_simple_1"] = (c0 / c_prev) - 1.0 if c_prev > 0 else None
        for k in (5, 20, 60):
            if len(self.closes) > k:
                ck = self.closes[-(k + 1)]
                out[f"mom_{k}"] = (c0 / ck) - 1.0 if ck > 0 else None
            else:
                out[f"mom_{k}"] = None
        return out
```

### `transforms/volatility.py`

```python
import math
from collections import deque
from decimal import Decimal
from .price import PriceFeatures

class VolatilityFeatures:
    """Realized vol over windows + Parkinson + Garman-Klass per bar."""
    def __init__(self, windows: tuple[int, ...] = (20, 60, 240), max_lookback: int = 240) -> None:
        self.windows = windows
        self.log_rets: deque[float] = deque(maxlen=max(windows) + 1)
        # for Parkinson / GK we need (high, low, open, close) of each bar
        self.bars: deque[tuple[float, float, float, float]] = deque(maxlen=max(windows) + 1)

    def update(self, o: Decimal, h: Decimal, l: Decimal, c: Decimal,
               log_ret: float | None) -> dict[str, float | None]:
        if log_ret is not None:
            self.log_rets.append(log_ret)
        self.bars.append((float(o), float(h), float(l), float(c)))
        out: dict[str, float | None] = {}
        for w in self.windows:
            if len(self.log_rets) >= w:
                xs = list(self.log_rets)[-w:]
                mean = sum(xs) / w
                var = sum((x - mean) ** 2 for x in xs) / max(w - 1, 1)
                out[f"vol_rs_{w}"] = math.sqrt(var)
            else:
                out[f"vol_rs_{w}"] = None
            # Parkinson: sqrt( (1/(4 ln 2)) * (1/N) * sum( ln(H/L)^2 ) )
            if len(self.bars) >= w:
                sub = list(self.bars)[-w:]
                pk = sum(math.log(b[1] / b[2]) ** 2 for b in sub if b[2] > 0) / w
                out[f"vol_park_{w}"] = math.sqrt(pk / (4.0 * math.log(2.0)))
                # Garman-Klass:
                gk_sum = 0.0
                for o_, h_, l_, c_ in sub:
                    if l_ > 0:
                        gk_sum += 0.5 * (math.log(h_ / l_)) ** 2 - (2 * math.log(2) - 1) * (math.log(c_ / o_)) ** 2
                out[f"vol_gk_{w}"] = math.sqrt(gk_sum / w) if gk_sum > 0 else None
            else:
                out[f"vol_park_{w}"] = None
                out[f"vol_gk_{w}"] = None
        return out
```

### `transforms/cross.py`

```python
from collections import deque
import math

class CrossFeatures:
    """Per-symbol, per-benchmark: rolling beta + correlation + cross-sectional z-score."""
    def __init__(self, benchmark_symbol: str = "BTC-USD", windows: tuple[int, ...] = (60, 240)) -> None:
        self.bench = benchmark_symbol
        self.windows = windows
        self._sym_rets: dict[str, deque[float]] = {}
        self._bench_rets: deque[float] = deque(maxlen=max(windows))

    def on_benchmark_ret(self, r: float | None) -> None:
        if r is not None:
            self._bench_rets.append(r)

    def on_symbol_ret(self, symbol: str, r: float | None) -> dict[str, float | None]:
        d = self._sym_rets.setdefault(symbol, deque(maxlen=max(self.windows)))
        if r is not None:
            d.append(r)
        out: dict[str, float | None] = {}
        for w in self.windows:
            if len(d) >= w and len(self._bench_rets) >= w:
                xs = list(d)[-w:]
                ys = list(self._bench_rets)[-w:]
                mx = sum(xs) / w
                my = sum(ys) / w
                num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                den = sum((y - my) ** 2 for y in ys)
                out[f"beta_{self.bench}_{w}"] = num / den if den > 0 else None
                # corr
                sx2 = sum((x - mx) ** 2 for x in xs)
                sy2 = den
                out[f"corr_{self.bench}_{w}"] = num / math.sqrt(sx2 * sy2) if sx2 > 0 and sy2 > 0 else None
            else:
                out[f"beta_{self.bench}_{w}"] = None
                out[f"corr_{self.bench}_{w}"] = None
        return out
```

### `online.py` — runner

```python
import asyncio
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from fincept_core.schemas import BarEvent, FeatureFrame
from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_MD_BARS_1M, STREAM_FEATURES_ONLINE
from redis.asyncio import Redis
from .transforms.price import PriceFeatures
from .transforms.volatility import VolatilityFeatures
from .transforms.cross import CrossFeatures

log = get_logger(__name__)

class OnlineRunner:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.consumer = Consumer(redis, group="features.online", consumer="features.online.1")
        self.producer = Producer(redis)
        self.price: dict[str, PriceFeatures] = {}
        self.vol: dict[str, VolatilityFeatures] = {}
        self.cross = CrossFeatures(benchmark_symbol=get_settings().universe[0]
                                   if get_settings().universe else "BTC-USD")

    async def run(self) -> None:
        async for msg_id, env in self.consumer.read(STREAM_MD_BARS_1M, BarEvent):
            bar = env.payload
            self._on_bar(bar)
            await self.consumer.ack(STREAM_MD_BARS_1M, msg_id)

    async def _on_bar(self, bar: BarEvent) -> None:
        sym = bar.symbol
        pf = self.price.setdefault(sym, PriceFeatures())
        vf = self.vol.setdefault(sym, VolatilityFeatures())
        price_vals = pf.update(bar.close)
        vol_vals = vf.update(bar.open, bar.high, bar.low, bar.close, price_vals.get("ret_log_1"))
        if sym == self.cross.bench:
            self.cross.on_benchmark_ret(price_vals.get("ret_log_1"))
        cross_vals = self.cross.on_symbol_ret(sym, price_vals.get("ret_log_1"))
        merged: dict[str, float | None] = {**price_vals, **vol_vals, **cross_vals}
        ff = FeatureFrame(symbol=sym, ts_event=bar.ts_event, freq=bar.freq, values=merged)
        await self.producer.publish(STREAM_FEATURES_ONLINE, ff)
```

## Tests

### `tests/test_price.py`

```python
from decimal import Decimal
from features.transforms.price import PriceFeatures

def test_log_returns_sequence():
    p = PriceFeatures()
    assert p.update(Decimal("100"))["ret_log_1"] is None       # bootstrap
    out = p.update(Decimal("110"))
    assert out["ret_log_1"] is not None
    assert abs(out["ret_log_1"] - 0.0953) < 1e-3                # log(1.1)

def test_momentum_lookback_required():
    p = PriceFeatures()
    for px in (100, 101, 102, 103, 104):
        out = p.update(Decimal(str(px)))
    assert out["mom_5"] is None    # need 6 bars total for 5-bar momentum
    out = p.update(Decimal("105"))
    assert out["mom_5"] is not None
```

### `tests/test_volatility.py`

```python
from decimal import Decimal
from features.transforms.volatility import VolatilityFeatures

def test_realized_vol_zero_when_constant():
    v = VolatilityFeatures(windows=(5,))
    for _ in range(7):
        out = v.update(Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), 0.0)
    assert out["vol_rs_5"] == 0.0
    assert out["vol_park_5"] == 0.0
```

### `tests/test_cross.py`

```python
from features.transforms.cross import CrossFeatures

def test_beta_perfect_correlation():
    c = CrossFeatures(benchmark_symbol="B", windows=(5,))
    rs = [0.01, -0.02, 0.03, -0.01, 0.02]
    for r in rs:
        c.on_benchmark_ret(r)
        out = c.on_symbol_ret("S", r)
    # When sym = bench, beta should be 1.0 and corr should be 1.0
    assert abs(out["beta_B_5"] - 1.0) < 1e-9
    assert abs(out["corr_B_5"] - 1.0) < 1e-9
```

## Landmines

- **PIT correctness:** features for bar at `ts_event = T` must use ONLY data with `ts_event <= T`. The online runner gets this for free (bars arrive in order). The OFFLINE replay (TASK-017) is where leakage usually creeps in — guard it with explicit asserts.
- **Float vs Decimal:** features intentionally use `float` (returns and ratios). Money quantities never use float; features that ARE money (e.g., absolute spread in $) should use Decimal until aggregated. Document this convention.
- **Bootstrap nulls:** every feature returns `None` until enough history is available. Downstream consumers must handle `None`; do NOT default to 0.
- **Cross features need a benchmark:** if the benchmark symbol has no bars yet, all betas/corrs are `None`. That's correct; do not impute.
- **Restart resumes mid-state:** state is in-memory only. After a restart, the runner re-warms from a configurable look-back via offline replay (TASK-017 batch backfill).
- **Bar gaps:** if a bar is missing (e.g., venue down for 1m), DO NOT interpolate. Emit `None` and continue. Imputation is a backtest-time choice, not a feature-time one.

## Out of scope

- Tick-level features (microstructure) — stub only in v1; full implementation in TASK-096 (Phase Y).
- Fundamental features (P/E, sector dummies) — separate task in Phase Y.
- Order-book deep features (book pressure curve) — TASK-096.
- Cross-sectional rankings beyond per-pair beta/corr — TASK-083 (Phase X+).

## Done when

- [ ] All files exist; `FeatureFrame` and `STREAM_FEATURES_ONLINE` added to contracts
- [ ] `pytest services/features/tests/` is green
- [ ] `mypy services/features` is green
- [ ] Manual smoke: with bars flowing in `md.bars.1m`, `features.online` receives ≥1 message per bar with `vol_rs_20`, `mom_20`, and `corr_BTC-USD_60` populated after warmup
- [ ] Per-bar latency from bar-close → feature-published p99 < 50 ms
