# TASK-041 · Risk gate + Kelly sizing + kill switch (singleton)

**Phase:** O · **Depends on:** TASK-040, TASK-045 (portfolio reader) · **Blocks:** TASK-044 (paper OMS)

## Goal

Between orchestrator decisions and the OMS, enforce pre-trade checks, size the order via Kelly-optimal sizing, and honor a global kill switch. Emits `OrderIntent` to `ord.orders` (status=PENDING_NEW) on approval, or writes a rejection to the audit log on denial.

## Files to create

```
services/risk/
├── pyproject.toml
├── src/risk/
│   ├── __init__.py
│   ├── main.py
│   ├── gate.py
│   ├── limits.py
│   ├── kelly.py
│   ├── kill_switch.py
│   └── var.py         # placeholder; implement in TASK-043
└── tests/
    ├── test_gate.py
    ├── test_kelly.py
    └── test_kill_switch.py
```

## Contracts

### `limits.py`

```python
from decimal import Decimal
from pydantic import BaseModel
from fincept_core.config import get_settings

class Limits(BaseModel):
    max_notional_usd_per_symbol: Decimal
    max_gross_notional_usd: Decimal
    max_daily_loss_usd: Decimal

def from_env() -> Limits:
    s = get_settings()
    return Limits(
        max_notional_usd_per_symbol=Decimal(str(s.max_notional_usd_per_symbol)),
        max_gross_notional_usd=Decimal(str(s.max_gross_notional_usd)),
        max_daily_loss_usd=Decimal(str(s.max_daily_loss_usd)),
    )
```

### `kelly.py`

```python
from decimal import Decimal

def kelly_fraction(edge: float, variance: float, cap: float = 0.25) -> float:
    """Fractional Kelly: f* = edge / variance. Capped at `cap` for safety.

    edge = expected return per trade (e.g., 0.002 = 20 bps).
    variance = squared std-dev of trade-return (non-zero).
    """
    if variance <= 0:
        return 0.0
    f = edge / variance
    return max(-cap, min(cap, f))

def size_from_kelly(
    target_notional: Decimal, kelly_f: float, fraction: float = 0.5
) -> Decimal:
    """Scale target notional by fraction of Kelly. `fraction=0.5` is half-Kelly (industry default)."""
    return target_notional * Decimal(str(abs(kelly_f) * fraction))
```

### `kill_switch.py`

```python
from redis.asyncio import Redis
from fincept_core.logging import get_logger
log = get_logger(__name__)

KEY = "kill_switch"

async def is_active(redis: Redis) -> bool:
    v = await redis.get(KEY)
    return v is not None

async def activate(redis: Redis, reason: str) -> None:
    await redis.set(KEY, reason)
    log.error("kill_switch.activated", reason=reason)

async def deactivate(redis: Redis) -> None:
    await redis.delete(KEY)
    log.warning("kill_switch.deactivated")
```

### `gate.py`

```python
from decimal import Decimal
from redis.asyncio import Redis
from fincept_core.clock import now_ns
from fincept_core.ids import new_id
from fincept_core.schemas import (
    Decision, OrderIntent, OrderType, TimeInForce, RiskCheckResult, Venue
)
from fincept_core.config import get_settings
from .limits import Limits
from . import kill_switch, kelly

async def check(
    decision: Decision,
    positions_by_symbol: dict[str, Decimal],         # signed notional per symbol
    gross_notional: Decimal,
    realized_daily_pnl: Decimal,
    edge_variance: dict[str, tuple[float, float]],   # symbol -> (edge, variance)
    redis: Redis,
    limits: Limits | None = None,
) -> tuple[RiskCheckResult, OrderIntent | None]:
    limits = limits or Limits.model_validate({
        "max_notional_usd_per_symbol": Decimal(str(get_settings().max_notional_usd_per_symbol)),
        "max_gross_notional_usd": Decimal(str(get_settings().max_gross_notional_usd)),
        "max_daily_loss_usd": Decimal(str(get_settings().max_daily_loss_usd)),
    })
    reasons: list[str] = []
    if await kill_switch.is_active(redis):
        reasons.append("kill_switch_active")
        return RiskCheckResult(approved=False, reasons=reasons, checked_at=now_ns()), None

    # Per-symbol notional
    signed_target = decision.target_notional_usd if decision.side.value == "buy" else -decision.target_notional_usd
    new_symbol_notional = positions_by_symbol.get(decision.symbol, Decimal(0)) + signed_target
    if abs(new_symbol_notional) > limits.max_notional_usd_per_symbol:
        reasons.append("symbol_notional_limit")

    # Gross
    delta_gross = abs(signed_target)
    if gross_notional + delta_gross > limits.max_gross_notional_usd:
        reasons.append("gross_notional_limit")

    # Daily loss
    if realized_daily_pnl <= -limits.max_daily_loss_usd:
        reasons.append("daily_loss_limit")

    # Kelly sizing
    edge, var = edge_variance.get(decision.symbol, (0.001, 0.0001))
    kf = kelly.kelly_fraction(edge, var)
    sized = kelly.size_from_kelly(decision.target_notional_usd, kf)

    if reasons:
        return RiskCheckResult(approved=False, reasons=reasons, checked_at=now_ns()), None

    reduced = sized if sized < decision.target_notional_usd else None
    intent = OrderIntent(
        order_id=new_id(), decision_id=decision.decision_id, ts_event=now_ns(),
        strategy_id=decision.strategy_id, symbol=decision.symbol,
        venue=Venue.PAPER if get_settings().trading_mode == "paper" else Venue.BINANCE,
        side=decision.side, order_type=OrderType.MARKET,
        quantity=sized,  # quantity is in USD notional at decision layer; OMS converts
        time_in_force=TimeInForce.IOC, tags={"source": "risk_gate", "kelly_fraction": f"{kf:.3f}"},
    )
    return RiskCheckResult(approved=True, reduced_notional_usd=reduced, checked_at=now_ns()), intent
```

### `main.py`

```python
import asyncio, socket
from decimal import Decimal
from redis.asyncio import Redis
from fincept_core.config import get_settings
from fincept_core.logging import configure_logging, get_logger
from fincept_core.leadership import Leader
from fincept_core.schemas import Decision
from fincept_bus.producer import Producer
from fincept_bus.consumer import Consumer
from fincept_bus.streams import STREAM_DECISIONS, STREAM_ORDERS
from .gate import check

configure_logging()
log = get_logger(__name__)

async def run() -> None:
    s = get_settings()
    redis = Redis.from_url(s.redis_url)
    leader = Leader(redis, role="risk")
    await leader.start()
    try:
        while not leader.is_leader:
            await asyncio.sleep(0.5)
        producer = Producer(redis)
        consumer = Consumer(redis, "risk", f"risk-{socket.gethostname()}")
        async for mid, env in consumer.read(STREAM_DECISIONS, Decision):
            dec = env.payload
            # TODO: replace stubs with real portfolio lookups + edge estimates
            positions: dict[str, Decimal] = {}
            gross = Decimal(0)
            realized = Decimal(0)
            edge_var = {dec.symbol: (0.001, 0.0001)}
            result, intent = await check(dec, positions, gross, realized, edge_var, redis)
            if result.approved and intent is not None:
                await producer.publish(STREAM_ORDERS, intent)
                log.info("risk.approved", symbol=dec.symbol, qty=str(intent.quantity))
            else:
                log.warning("risk.denied", symbol=dec.symbol, reasons=result.reasons)
            await consumer.ack(STREAM_DECISIONS, mid)
    finally:
        await leader.stop()
        await redis.aclose()

def main() -> None:
    asyncio.run(run())

if __name__ == "__main__":
    main()
```

## Tests

### `tests/test_gate.py`

```python
import pytest, os
from decimal import Decimal
from redis.asyncio import Redis
from fincept_core.schemas import Decision, Side
from risk.gate import check
from risk.limits import Limits

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("TRADING_MODE", "paper")

@pytest.mark.asyncio
async def test_deny_if_symbol_limit_breached(env):
    r = Redis.from_url("redis://localhost:6379/15")
    await r.delete("kill_switch")
    d = Decision(decision_id="x", ts_event=0, strategy_id="s", symbol="BTC-USD",
                 side=Side.BUY, target_notional_usd=Decimal("100000"),
                 urgency=1.0, rationale="", source_signals=[])
    limits = Limits(max_notional_usd_per_symbol=Decimal("1000"),
                    max_gross_notional_usd=Decimal("50000"),
                    max_daily_loss_usd=Decimal("2000"))
    res, intent = await check(d, {}, Decimal(0), Decimal(0), {}, r, limits=limits)
    assert not res.approved and "symbol_notional_limit" in res.reasons
    await r.aclose()
```

### `tests/test_kelly.py`

```python
from decimal import Decimal
from risk.kelly import kelly_fraction, size_from_kelly

def test_kelly_capped():
    assert kelly_fraction(1.0, 0.001) <= 0.25    # cap
    assert kelly_fraction(-1.0, 0.001) >= -0.25

def test_kelly_zero_variance():
    assert kelly_fraction(0.01, 0.0) == 0.0

def test_size():
    assert size_from_kelly(Decimal("1000"), 0.1, fraction=0.5) == Decimal("50.00")
```

### `tests/test_kill_switch.py`

```python
import pytest
from redis.asyncio import Redis
from risk import kill_switch

@pytest.mark.asyncio
async def test_kill_switch_cycle():
    r = Redis.from_url("redis://localhost:6379/15")
    await kill_switch.deactivate(r)
    assert not await kill_switch.is_active(r)
    await kill_switch.activate(r, "test")
    assert await kill_switch.is_active(r)
    await kill_switch.deactivate(r)
    await r.aclose()
```

## Out of scope

- Real VaR — TASK-043
- Borrow limits, restricted list, self-trade prevention — TASK-042 extension

## Done when

- [ ] Files exist, tests green
- [ ] Manual: publish a Decision with notional > limit → observe `risk.denied` log + no order emitted
