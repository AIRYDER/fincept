"""
Tests for backtester.engine.BacktestEngine.

Includes:
- single-buy lifecycle smoke test
- on_start / on_fill / on_stop callback ordering
- position math: open more, close some, flat exit, cross-flip
- equity curve tracks every bar
- MA-crossover sample strategy as an end-to-end integration test
- PIT integrity: an order submitted on bar T does NOT fill against bar T
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel

from backtester.datasource import BarsDataSource
from backtester.engine import BacktestEngine
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    Fill,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    TradeEvent,
    Venue,
)
from fincept_sdk import Strategy, StrategyContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(ts: int, *, close: str = "100") -> BarEvent:
    c = Decimal(close)
    return BarEvent(
        venue=Venue.PAPER,
        symbol="BTC-USD",
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts,
        ts_recv=ts,
        freq="1m",
        open=c,
        high=c + Decimal("1"),
        low=c - Decimal("1"),
        close=c,
        volume=Decimal("10"),
        trades=1,
    )


def _datasource(bars: list[BarEvent]) -> BarsDataSource:
    """Build a DataSource from an in-memory bar list."""
    by_symbol: dict[str, list[BarEvent]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)

    async def reader(symbol: str, freq: str, start_ns: int, end_ns: int) -> list[BarEvent]:
        return [b for b in by_symbol.get(symbol, []) if start_ns <= b.ts_event < end_ns]

    symbols = list({bar.symbol for bar in bars})
    return BarsDataSource(symbols, "1m", 0, 1_000_000_000, bar_reader=reader)


def _intent(
    *,
    order_id: str = "o1",
    side: Side = Side.BUY,
    quantity: str = "1",
    ts_event: int = 0,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="d1",
        ts_event=ts_event,
        strategy_id="t",
        symbol="BTC-USD",
        venue=Venue.PAPER,
        side=side,
        order_type=order_type,
        quantity=Decimal(quantity),
        limit_price=limit_price,
        time_in_force=TimeInForce.GTC,
    )


class _Recorder(Strategy):
    """A strategy that records every callback invocation."""

    strategy_id: ClassVar[str] = "rec"
    symbols: ClassVar[list[str]] = ["BTC-USD"]

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self._submitted = False

    def on_start(self, ctx: StrategyContext) -> None:
        self.events.append(("start", None))

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        self.events.append(("bar", bar.ts_event))
        if not self._submitted:
            ctx.submit(_intent(order_id="o1", ts_event=bar.ts_event))
            self._submitted = True

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        self.events.append(("tick", trade.ts_event))

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        self.events.append(("fill", fill.fill_id))

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        self.events.append(("signal", None))

    def on_stop(self, ctx: StrategyContext) -> None:
        self.events.append(("stop", None))


# ---------------------------------------------------------------------------
# Lifecycle + callback ordering
# ---------------------------------------------------------------------------


async def test_engine_runs_full_lifecycle_with_one_market_buy() -> None:
    bars = [_bar(ts=1_000), _bar(ts=2_000), _bar(ts=3_000)]
    strategy = _Recorder()
    engine = BacktestEngine(strategy, _datasource(bars))

    blotter = await engine.run()

    # Order submitted on bar 1 fills against bar 2 (PIT-correct).
    assert len(blotter.fills) == 1
    fill = blotter.fills[0]
    assert fill.ts_event == 2_000
    assert fill.side == Side.BUY


async def test_callbacks_fire_in_documented_order() -> None:
    bars = [_bar(ts=1_000), _bar(ts=2_000)]
    strategy = _Recorder()
    engine = BacktestEngine(strategy, _datasource(bars))

    await engine.run()
    kinds = [kind for kind, _ in strategy.events]
    assert kinds[0] == "start"
    assert kinds[-1] == "stop"
    assert kinds.count("bar") == 2
    assert kinds.count("fill") == 1


async def test_equity_curve_has_one_sample_per_bar() -> None:
    bars = [_bar(ts=t) for t in (1_000, 2_000, 3_000, 4_000)]
    strategy = _Recorder()
    engine = BacktestEngine(strategy, _datasource(bars))

    blotter = await engine.run()
    assert len(blotter.equity_curve) == 4
    assert [ts for ts, _ in blotter.equity_curve] == [1_000, 2_000, 3_000, 4_000]


# ---------------------------------------------------------------------------
# PIT integrity
# ---------------------------------------------------------------------------


async def test_order_submitted_on_bar_t_does_not_fill_against_bar_t() -> None:
    """Strategy submits on the first bar; first fill must be on the second bar."""
    bars = [_bar(ts=1_000, close="100"), _bar(ts=2_000, close="105")]
    strategy = _Recorder()
    engine = BacktestEngine(strategy, _datasource(bars))

    blotter = await engine.run()
    assert blotter.fills[0].ts_event == 2_000  # not 1_000


# ---------------------------------------------------------------------------
# Position math: 4 cases
# ---------------------------------------------------------------------------


class _ScriptedTrader(Strategy):
    """Submits a pre-baked sequence of intents, one per bar."""

    strategy_id: ClassVar[str] = "scripted"
    symbols: ClassVar[list[str]] = ["BTC-USD"]

    def __init__(self, intents_by_bar: dict[int, list[OrderIntent]]) -> None:
        self._intents = intents_by_bar
        self.fills: list[Fill] = []
        self.final_positions: dict[str, Any] = {}

    def on_start(self, ctx: StrategyContext) -> None:
        pass

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        pass

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        for intent in self._intents.get(bar.ts_event, []):
            ctx.submit(intent)

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        self.fills.append(fill)

    def on_stop(self, ctx: StrategyContext) -> None:
        self.final_positions = dict(ctx.positions)


async def test_open_position_records_avg_cost() -> None:
    """Buy 1 -> position quantity=1, avg_cost ~= fill price."""
    bars = [_bar(ts=t, close="100") for t in (1_000, 2_000)]
    strategy = _ScriptedTrader({1_000: [_intent(order_id="o1")]})
    engine = BacktestEngine(strategy, _datasource(bars))

    await engine.run()
    pos = strategy.final_positions["BTC-USD"]
    assert pos.quantity == Decimal("1")
    assert pos.avg_cost > Decimal("100")  # spread + slippage
    assert pos.realized_pnl == Decimal(0)


async def test_open_more_in_same_direction_blends_avg_cost() -> None:
    """Two buys on different bars at different prices -> weighted-average cost."""
    bars = [
        _bar(ts=1_000, close="100"),
        _bar(ts=2_000, close="100"),
        _bar(ts=3_000, close="200"),
        _bar(ts=4_000, close="200"),
    ]
    strategy = _ScriptedTrader(
        {
            1_000: [_intent(order_id="o1")],  # fills on bar 2 at ~100
            3_000: [_intent(order_id="o2")],  # fills on bar 4 at ~200
        }
    )
    engine = BacktestEngine(strategy, _datasource(bars))
    await engine.run()
    pos = strategy.final_positions["BTC-USD"]
    assert pos.quantity == Decimal("2")
    # Avg cost should be ~ (100 + 200) / 2 = 150 modulo costs.
    assert Decimal("140") < pos.avg_cost < Decimal("160")


async def test_exact_close_realizes_pnl_and_zeros_quantity() -> None:
    bars = [
        _bar(ts=1_000, close="100"),
        _bar(ts=2_000, close="100"),  # buy fills here
        _bar(ts=3_000, close="200"),
        _bar(ts=4_000, close="200"),  # sell fills here
    ]
    strategy = _ScriptedTrader(
        {
            1_000: [_intent(order_id="o1", side=Side.BUY)],
            3_000: [_intent(order_id="o2", side=Side.SELL)],
        }
    )
    engine = BacktestEngine(strategy, _datasource(bars))
    await engine.run()
    pos = strategy.final_positions["BTC-USD"]
    assert pos.quantity == Decimal(0)
    assert pos.realized_pnl > Decimal(0)  # bought at ~100, sold at ~200


async def test_cross_flip_closes_prior_side_and_opens_opposite() -> None:
    """Long 1 -> sell 2 -> ends short 1 with avg_cost = sell fill price."""
    bars = [
        _bar(ts=1_000),  # buy submitted
        _bar(ts=2_000),  # buy fills (long 1)
        _bar(ts=3_000),  # sell 2 submitted
        _bar(ts=4_000),  # sell fills (cross to short 1)
    ]
    strategy = _ScriptedTrader(
        {
            1_000: [_intent(order_id="o1", side=Side.BUY, quantity="1")],
            3_000: [_intent(order_id="o2", side=Side.SELL, quantity="2")],
        }
    )
    engine = BacktestEngine(strategy, _datasource(bars))
    await engine.run()
    pos = strategy.final_positions["BTC-USD"]
    assert pos.quantity == Decimal("-1")
    # New short opened at the sell fill price (the cross-flip case).
    assert pos.avg_cost < Decimal("100")  # sell fill is below mid


# ---------------------------------------------------------------------------
# MA crossover end-to-end
# ---------------------------------------------------------------------------


class MACrossover(Strategy):
    """Tiny MA-crossover strategy: long when short MA > long MA."""

    strategy_id: ClassVar[str] = "ma_crossover"
    symbols: ClassVar[list[str]] = ["BTC-USD"]
    SHORT: ClassVar[int] = 3
    LONG: ClassVar[int] = 6

    def __init__(self) -> None:
        self.closes: list[Decimal] = []
        self._position: int = 0  # -1 / 0 / 1
        self._next_id = 0

    def on_start(self, ctx: StrategyContext) -> None:
        pass

    def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
        pass

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        pass

    def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
        pass

    def on_stop(self, ctx: StrategyContext) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        self.closes.append(bar.close)
        if len(self.closes) <= self.LONG:
            return
        short_ma = sum(self.closes[-self.SHORT :]) / Decimal(self.SHORT)
        long_ma = sum(self.closes[-self.LONG :]) / Decimal(self.LONG)
        target = 1 if short_ma > long_ma else -1 if short_ma < long_ma else 0
        if target == self._position:
            return
        # Close existing + open new in one order = quantity 1 or 2 depending on flip.
        delta = target - self._position
        if delta == 0:
            return
        side = Side.BUY if delta > 0 else Side.SELL
        self._next_id += 1
        ctx.submit(
            _intent(
                order_id=f"ma-{self._next_id}",
                side=side,
                quantity=str(abs(delta)),
                ts_event=bar.ts_event,
            )
        )
        self._position = target


async def test_ma_crossover_produces_fills_on_trending_market() -> None:
    """Steadily rising prices should have the strategy go long and stay there."""
    closes = [str(100 + i) for i in range(20)]  # 100, 101, ..., 119
    bars = [_bar(ts=1_000 * (i + 1), close=closes[i]) for i in range(20)]
    strategy = MACrossover()
    engine = BacktestEngine(strategy, _datasource(bars))

    blotter = await engine.run()
    # Should have at least one fill once the short MA crosses above the long MA.
    assert len(blotter.fills) >= 1
    # Equity curve length matches bar count.
    assert len(blotter.equity_curve) == 20
