# TASK-020 · Backtester engine + cost model + broker

**Phase:** B · **Depends on:** TASK-004 (fincept-db), TASK-017 (features) · **Blocks:** TASK-023, all agent training

## Goal

Deterministic event-driven backtester replaying historical bars/ticks from Timescale, simulating fills with realistic costs, emitting a complete trade blotter and equity curve.

## Files to create

```
services/backtester/
├── pyproject.toml
├── src/backtester/
│   ├── __init__.py
│   ├── engine.py
│   ├── datasource.py
│   ├── broker.py
│   ├── costs.py
│   ├── blotter.py
│   ├── report.py
│   └── walk_forward.py
└── tests/
    ├── test_engine.py
    ├── test_broker.py
    └── test_costs.py
```

## Contracts

### `datasource.py`

```python
from typing import AsyncIterator
from fincept_core.schemas import BarEvent
from fincept_db.bars import read_bars

class DataSource:
    """Replays historical bars from Timescale in event-time order."""

    def __init__(self, symbols: list[str], freq: str, start_ns: int, end_ns: int) -> None:
        self.symbols = symbols; self.freq = freq; self.start = start_ns; self.end = end_ns

    async def replay(self) -> AsyncIterator[BarEvent]:
        async for bar in read_bars(self.symbols, self.freq, self.start, self.end):
            yield bar
```

### `costs.py`

```python
from decimal import Decimal
from pydantic import BaseModel
from fincept_core.schemas import Side

class CostModel(BaseModel):
    """Parameters for realistic transaction cost simulation."""
    maker_fee_bps: Decimal = Decimal("1")           # 1 bp
    taker_fee_bps: Decimal = Decimal("5")           # 5 bp
    spread_bps_default: Decimal = Decimal("3")
    slippage_impact_coef: Decimal = Decimal("0.1")  # bps per 1% of ADV

    def apply(
        self, *, side: Side, price: Decimal, quantity: Decimal, is_maker: bool, adv_pct: float
    ) -> tuple[Decimal, Decimal]:
        """Returns (fill_price, fee_usd)."""
        spread = self.spread_bps_default / Decimal(10000) * price
        half = spread / 2
        exec_price = price + half if side == Side.BUY else price - half
        impact_bps = self.slippage_impact_coef * Decimal(str(adv_pct)) * 100
        impact = impact_bps / Decimal(10000) * price
        exec_price = exec_price + impact if side == Side.BUY else exec_price - impact
        notional = exec_price * quantity
        fee_bps = self.maker_fee_bps if is_maker else self.taker_fee_bps
        fee = notional * fee_bps / Decimal(10000)
        return exec_price, fee
```

### `broker.py`

```python
from decimal import Decimal
from fincept_core.clock import now_ns
from fincept_core.ids import new_id
from fincept_core.schemas import (
    OrderIntent, Order, OrderStatus, Fill, Side, OrderType, BarEvent
)
from .costs import CostModel

class SimBroker:
    """Simulates fills against the replayed bar stream."""

    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.costs = cost_model or CostModel()
        self.open_orders: dict[str, Order] = {}

    def submit(self, intent: OrderIntent) -> Order:
        order = Order(**intent.model_dump(), created_at=now_ns(), updated_at=now_ns(), status=OrderStatus.NEW)
        self.open_orders[order.order_id] = order
        return order

    def on_bar(self, bar: BarEvent) -> list[Fill]:
        fills: list[Fill] = []
        for oid in list(self.open_orders):
            o = self.open_orders[oid]
            if o.symbol != bar.symbol:
                continue
            price = self._triggered_at(o, bar)
            if price is None:
                continue
            # Assume 0.5% ADV per order (toy); refine with real ADV service later
            exec_px, fee = self.costs.apply(
                side=o.side, price=price, quantity=o.quantity, is_maker=(o.order_type == OrderType.LIMIT), adv_pct=0.005
            )
            fills.append(Fill(
                fill_id=new_id(), order_id=o.order_id, ts_event=bar.ts_event,
                symbol=o.symbol, side=o.side, price=exec_px, quantity=o.quantity, fee=fee,
                is_maker=(o.order_type == OrderType.LIMIT),
            ))
            o = o.model_copy(update={
                "status": OrderStatus.FILLED, "filled_qty": o.quantity, "avg_fill_price": exec_px, "updated_at": bar.ts_event,
            })
            self.open_orders.pop(oid)
        return fills

    @staticmethod
    def _triggered_at(o: Order, bar: BarEvent) -> Decimal | None:
        if o.order_type == OrderType.MARKET:
            return bar.open
        if o.order_type == OrderType.LIMIT and o.limit_price is not None:
            if o.side == Side.BUY and bar.low <= o.limit_price:
                return min(o.limit_price, bar.open)
            if o.side == Side.SELL and bar.high >= o.limit_price:
                return max(o.limit_price, bar.open)
        return None
```

### `blotter.py`

```python
from decimal import Decimal
from pydantic import BaseModel
from fincept_core.schemas import Fill

class Blotter(BaseModel):
    fills: list[Fill] = []
    equity_curve: list[tuple[int, Decimal]] = []   # (ts_event_ns, equity_usd)
    starting_cash: Decimal = Decimal("100000")

    def add_fill(self, f: Fill) -> None:
        self.fills.append(f)

    def mark_equity(self, ts_ns: int, equity_usd: Decimal) -> None:
        self.equity_curve.append((ts_ns, equity_usd))
```

### `engine.py`

```python
from decimal import Decimal
from fincept_core.schemas import BarEvent, Position, OrderIntent
from fincept_sdk.strategy import Strategy, StrategyContext
from .datasource import DataSource
from .broker import SimBroker
from .blotter import Blotter

class _Context(StrategyContext):
    def __init__(self, engine: "BacktestEngine") -> None:
        self._engine = engine
        self.now_ns = 0
        self.positions: dict[str, Position] = {}

    def submit(self, intent: OrderIntent) -> str:
        o = self._engine.broker.submit(intent)
        return o.order_id

    def cancel(self, order_id: str) -> None:
        self._engine.broker.open_orders.pop(order_id, None)

    def get_feature(self, name: str, symbol: str) -> float | None:
        return self._engine.features.get((name, symbol))

    def log(self, msg: str, **kwargs: object) -> None:
        pass  # integrate structlog in full impl

class BacktestEngine:
    def __init__(self, strategy: Strategy, datasource: DataSource) -> None:
        self.strategy = strategy
        self.datasource = datasource
        self.broker = SimBroker()
        self.blotter = Blotter()
        self.features: dict[tuple[str, str], float] = {}  # populated by strategy.on_bar pre-hook

    async def run(self) -> Blotter:
        ctx = _Context(self)
        self.strategy.on_start(ctx)
        async for bar in self.datasource.replay():
            ctx.now_ns = bar.ts_event
            self.strategy.on_bar(ctx, bar)
            for fill in self.broker.on_bar(bar):
                self.blotter.add_fill(fill)
                self.strategy.on_fill(ctx, fill)
                self._update_positions(ctx, fill)
            equity = self._compute_equity(ctx, bar)
            self.blotter.mark_equity(bar.ts_event, equity)
        self.strategy.on_stop(ctx)
        return self.blotter

    def _update_positions(self, ctx: _Context, fill) -> None:
        pos = ctx.positions.get(fill.symbol)
        signed = fill.quantity if fill.side.value == "buy" else -fill.quantity
        if pos is None:
            ctx.positions[fill.symbol] = Position(
                strategy_id=self.strategy.strategy_id, symbol=fill.symbol,
                quantity=signed, avg_cost=fill.price,
                realized_pnl=Decimal(0), unrealized_pnl=Decimal(0), updated_at=fill.ts_event,
            )
        else:
            new_qty = pos.quantity + signed
            if pos.quantity * new_qty < 0 or new_qty == 0:
                # crossed zero or flat — realize P&L
                realized = (fill.price - pos.avg_cost) * min(abs(pos.quantity), abs(signed)) * (1 if pos.quantity > 0 else -1)
                ctx.positions[fill.symbol] = pos.model_copy(update={
                    "quantity": new_qty, "realized_pnl": pos.realized_pnl + realized,
                    "avg_cost": fill.price if new_qty != 0 else pos.avg_cost, "updated_at": fill.ts_event,
                })
            else:
                new_cost = (pos.avg_cost * abs(pos.quantity) + fill.price * abs(signed)) / abs(new_qty)
                ctx.positions[fill.symbol] = pos.model_copy(update={
                    "quantity": new_qty, "avg_cost": new_cost, "updated_at": fill.ts_event,
                })

    def _compute_equity(self, ctx: _Context, bar: BarEvent) -> Decimal:
        cash = self.blotter.starting_cash
        realized = sum((p.realized_pnl for p in ctx.positions.values()), Decimal(0))
        unrealized = sum(
            ((bar.close - p.avg_cost) * p.quantity for p in ctx.positions.values() if p.symbol == bar.symbol),
            Decimal(0),
        )
        fees = sum((f.fee for f in self.blotter.fills), Decimal(0))
        return cash + realized + unrealized - fees
```

## Tests

### `tests/test_engine.py`

```python
import pytest
from decimal import Decimal
from fincept_core.schemas import BarEvent, Venue, AssetClass, OrderIntent, OrderType, Side, TimeInForce
from backtester.engine import BacktestEngine
from backtester.datasource import DataSource
from fincept_sdk.strategy import Strategy, StrategyContext

class BuyOnce(Strategy):
    strategy_id = "t"
    symbols = ["BTC-USD"]
    bought = False
    def on_start(self, ctx): pass
    def on_bar(self, ctx, bar):
        if not self.bought:
            ctx.submit(OrderIntent(
                order_id="o", decision_id="d", ts_event=bar.ts_event, strategy_id=self.strategy_id,
                symbol="BTC-USD", venue=Venue.PAPER, side=Side.BUY,
                order_type=OrderType.MARKET, quantity=Decimal("1"), time_in_force=TimeInForce.GTC,
            ))
            self.bought = True
    def on_tick(self, ctx, t): pass
    def on_fill(self, ctx, f): pass
    def on_signal(self, ctx, s): pass
    def on_stop(self, ctx): pass

class FakeSource(DataSource):
    def __init__(self):
        super().__init__(["BTC-USD"], "1m", 0, 2)
    async def replay(self):
        for i, p in enumerate([100, 101, 102]):
            yield BarEvent(venue=Venue.PAPER, symbol="BTC-USD", asset_class=AssetClass.CRYPTO_SPOT,
                           ts_event=i, ts_recv=i, freq="1m",
                           open=Decimal(p), high=Decimal(p), low=Decimal(p), close=Decimal(p),
                           volume=Decimal("10"), trades=1)

@pytest.mark.asyncio
async def test_engine_buys_once():
    eng = BacktestEngine(BuyOnce(), FakeSource())
    b = await eng.run()
    assert len(b.fills) == 1
    assert b.fills[0].price > Decimal(100)        # includes spread + slippage
    assert b.equity_curve[-1][1] > b.starting_cash - Decimal(10)  # not catastrophically wrong
```

## Out of scope

- Multi-leg orders — defer
- Short-borrow costs — in `costs.py` stub, implement in TASK-021 refinement
- Walk-forward engine — TASK-023 wraps this
- Live-data leakage detection — TASK-017 handles PIT joins

## Done when

- [ ] Files exist
- [ ] `pytest services/backtester/tests` green
- [ ] Reference MA-crossover notebook in `notebooks/` produces Sharpe within 10% of QuantConnect's number on the same period
