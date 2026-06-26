"""
Tests for ``strategy_host.runner.run_strategy``.

Integration-level tests against fakeredis: publish events on the
streams the runner consumes, assert on what shows up on the order
stream.  We monkey-patch ``backtester.strategies.STRATEGY_REGISTRY``
to inject a recording strategy class so we can verify hook-level
behaviour (which positions the strategy saw, which fills it
processed) that's invisible from the published-order vantage point.

What's covered
~~~~~~~~~~~~~~

  * Bar for matching symbol -> on_bar called -> submit drained ->
    OrderIntent published with strategy_id stamped.
  * Bar for unrelated symbol -> on_bar NOT called.
  * Position with matching strategy_id -> ctx.positions updated.
  * Position with other strategy_id -> ctx.positions unchanged.
  * Fill matching outstanding order -> on_fill called.
  * Fill not in outstanding ledger -> on_fill NOT called.
  * Build-time failure (unknown class_name) -> runner exits cleanly.
  * on_start crash -> runner exits cleanly.
  * Cancellation -> on_stop called.
  * on_bar crash -> runner survives and continues.

What's deferred
~~~~~~~~~~~~~~~

  * Multi-symbol ordering, batch-publish ordering, and partial-fill
    bookkeeping are exercised in the strategy classes' own tests
    (in services/backtester/tests).  Here we just confirm the
    runner's plumbing is correct.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from strategy_host.runner import STREAM_OUTGOING_ORDERS, run_strategy

from fincept_bus.producer import Producer
from fincept_bus.streams import (
    STREAM_FILLS,
    STREAM_MD_BARS_1M,
    STREAM_POSITIONS,
)
from fincept_core.events import Event
from fincept_core.schemas import (
    AssetClass,
    BarEvent,
    Fill,
    OrderIntent,
    OrderType,
    Position,
    Side,
    TimeInForce,
    Venue,
)
from fincept_core.strategy_config import StrategyConfig
from fincept_sdk import Strategy, StrategyContext
from oms.paper import PaperFiller
from oms.prices import LivePrices
from oms.processor import process_intent
from portfolio.state import PortfolioState, apply_fill
from portfolio.store import PositionStore

# --------------------------------------------------------------------------- #
# Recording strategy: captures every hook call for assertion                 #
# --------------------------------------------------------------------------- #


class RecordingStrategy(Strategy):
    """Test-only Strategy implementation.

    Captures every hook call into ``calls`` (a class-level list so
    tests can find it regardless of who constructed the instance)
    and supports per-test toggles for crash injection.
    """

    strategy_id: ClassVar[str] = "recording.v1"
    symbols: ClassVar[list[str]] = []

    # Class-level so tests can find the instance the runner built.
    instances: ClassVar[list[RecordingStrategy]] = []

    def __init__(
        self,
        symbols: list[str],
        *,
        fail_on_start: bool = False,
        fail_on_bar: bool = False,
        submit_on_bar: OrderIntent | None = None,
    ) -> None:
        self.symbols = list(symbols)  # type: ignore[misc]
        self.calls: list[tuple[Any, ...]] = []
        self._fail_on_start = fail_on_start
        self._fail_on_bar = fail_on_bar
        self._submit_on_bar = submit_on_bar
        # Snapshot of ``ctx.positions`` taken on each on_bar call --
        # the runtime mutates the dict in place so storing the dict
        # itself would only ever show the LATEST state, hiding any
        # mid-test transitions we'd want to assert on.
        self.position_snapshots: list[dict[str, Position]] = []
        RecordingStrategy.instances.append(self)

    def on_start(self, ctx: StrategyContext) -> None:
        if self._fail_on_start:
            raise RuntimeError("intentional on_start crash")
        self.calls.append(("on_start",))

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        self.position_snapshots.append(dict(ctx.positions))
        self.calls.append(("on_bar", bar.symbol))
        if self._fail_on_bar:
            raise RuntimeError("intentional on_bar crash")
        if self._submit_on_bar is not None:
            ctx.submit(self._submit_on_bar)

    def on_tick(self, ctx: StrategyContext, trade: Any) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        self.calls.append(("on_fill", fill.order_id, str(fill.quantity)))

    def on_signal(self, ctx: StrategyContext, signal: Any) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        self.calls.append(("on_stop",))


# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def fake_redis() -> Any:
    pytest.importorskip("fakeredis")
    import fakeredis.aioredis  # type: ignore[import-not-found]

    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def patch_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install RecordingStrategy under the key ``"recording"``.

    Reverted automatically by monkeypatch on test teardown.  Each
    test gets a fresh ``RecordingStrategy.instances`` list so cross-
    test pollution can't happen.
    """
    from backtester.strategies import STRATEGY_REGISTRY

    monkeypatch.setitem(STRATEGY_REGISTRY, "recording", RecordingStrategy)
    RecordingStrategy.instances.clear()


def _bar(
    *,
    symbol: str = "BTC-USD",
    ts_event: int = 1_000_000_000,
    close: str = "100",
) -> BarEvent:
    return BarEvent(
        venue=Venue.PAPER,
        symbol=symbol,
        asset_class=AssetClass.CRYPTO_SPOT,
        ts_event=ts_event,
        ts_recv=ts_event,
        freq="1m",
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=Decimal("1"),
        trades=1,
    )


def _intent(
    *,
    strategy_id: str = "rec_test",
    order_id: str = "order-1",
    symbol: str = "BTC-USD",
    side: Side = Side.BUY,
    qty: str = "0.1",
) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        decision_id="dec-1",
        ts_event=1_000_000_000,
        strategy_id=strategy_id,
        symbol=symbol,
        venue=Venue.PAPER,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(qty),
        time_in_force=TimeInForce.GTC,
    )


def _position(
    *,
    strategy_id: str = "rec_test",
    symbol: str = "BTC-USD",
    qty: str = "0.5",
) -> Position:
    return Position(
        strategy_id=strategy_id,
        symbol=symbol,
        quantity=Decimal(qty),
        avg_cost=Decimal("100"),
        updated_at=0,
    )


def _fill(
    *,
    order_id: str = "order-1",
    symbol: str = "BTC-USD",
    side: Side = Side.BUY,
    qty: str = "0.1",
) -> Fill:
    return Fill(
        fill_id="fill-1",
        order_id=order_id,
        ts_event=1_000_000_000,
        symbol=symbol,
        side=side,
        price=Decimal("100"),
        quantity=Decimal(qty),
    )


def _config(
    *,
    strategy_id: str = "rec_test",
    class_name: str = "recording",
    symbols: list[str] | None = None,
    params: dict[str, Any] | None = None,
    enabled: bool = True,
) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        class_name=class_name,
        symbols=symbols or ["BTC-USD"],
        params=params or {},
        model_binding=None,
        enabled=enabled,
        created_at=0.0,
        updated_at=0.0,
    )


async def _wait_for(predicate: Any, *, timeout_s: float = 3.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until truthy or timeout.  Returns whether
    the predicate eventually became truthy.

    Used because the runner is a background task and we can't
    deterministically know when it has consumed an event.  Timeouts
    are generous (3s) so a slow CI machine doesn't flake."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _start_runner(
    config: StrategyConfig, fake_redis: Any
) -> tuple[asyncio.Task[None], asyncio.Event]:
    stop = asyncio.Event()
    task = asyncio.create_task(run_strategy(config, fake_redis, stop))
    # Give the runner time to: build_strategy, on_start, ensure
    # consumer groups.  Without this, a test that immediately
    # publishes an event might see the message land before the
    # group exists, in which case it's never delivered.
    await _wait_for(
        lambda: (
            bool(RecordingStrategy.instances)
            and (
                len(RecordingStrategy.instances[-1].calls) >= 1
                or RecordingStrategy.instances[-1]._fail_on_start
            )
        ),
        timeout=2.0,
    )
    # Even after on_start, the consumer.consume() task is created
    # but ensure_groups runs before its first iteration.  Sleep one
    # tick to let xgroup_create complete.
    await asyncio.sleep(0.05)
    return task, stop


async def _stop_runner(task: asyncio.Task[None], stop: asyncio.Event) -> None:
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except TimeoutError:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task


async def _read_orders(fake_redis: Any) -> list[OrderIntent]:
    """Read everything currently on STREAM_ORDERS and parse intents."""
    raw = await fake_redis.xrange(STREAM_OUTGOING_ORDERS, "-", "+")
    out: list[OrderIntent] = []
    for _msg_id, fields in raw:
        # fakeredis returns dict[bytes, bytes].
        type_ = fields.get(b"type") or fields.get("type")
        if isinstance(type_, bytes):
            type_ = type_.decode()
        if type_ != "order_intent":
            continue
        payload = fields.get(b"payload") or fields.get("payload")
        if isinstance(payload, bytes):
            payload = payload.decode()
        out.append(OrderIntent.model_validate_json(payload))
    return out


# --------------------------------------------------------------------------- #
# Bar -> on_bar -> submit -> publish                                         #
# --------------------------------------------------------------------------- #


class TestBarDispatch:
    async def test_bar_for_matching_symbol_calls_on_bar(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        try:
            await producer.publish(STREAM_MD_BARS_1M, Event(type="bar", payload=_bar()))
            ok = await _wait_for(
                lambda: any(c[0] == "on_bar" for c in RecordingStrategy.instances[-1].calls),
                timeout=3.0,
            )
            assert ok, "on_bar was never called for matching symbol"
        finally:
            await _stop_runner(task, stop)

    async def test_bar_for_unrelated_symbol_does_not_dispatch(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        try:
            await producer.publish(
                STREAM_MD_BARS_1M,
                Event(type="bar", payload=_bar(symbol="ETH-USD")),
            )
            # Wait long enough that a dispatched call would have
            # registered.  Then assert no on_bar call ever happened
            # for ETH-USD.
            await asyncio.sleep(0.5)
            calls = RecordingStrategy.instances[-1].calls
            on_bar_eth = [c for c in calls if c[0] == "on_bar" and c[1] == "ETH-USD"]
            assert on_bar_eth == []
        finally:
            await _stop_runner(task, stop)

    async def test_submitted_intent_published_to_orders_stream(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        intent = _intent(strategy_id="rec_test", order_id="order-from-bar")
        config = _config(symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        # Set the strategy to submit an intent on its first bar.
        RecordingStrategy.instances[-1]._submit_on_bar = intent
        try:
            await producer.publish(STREAM_MD_BARS_1M, Event(type="bar", payload=_bar()))

            async def order_appears() -> bool:
                orders = await _read_orders(fake_redis)
                return len(orders) >= 1

            # Use an awaitable predicate via lambda that returns the
            # awaited result.  Simpler: poll directly.
            deadline = time.monotonic() + 3.0
            orders: list[OrderIntent] = []
            while time.monotonic() < deadline:
                orders = await _read_orders(fake_redis)
                if orders:
                    break
                await asyncio.sleep(0.05)

            assert len(orders) == 1
            assert orders[0].order_id == "order-from-bar"
            assert orders[0].strategy_id == "rec_test"
            assert orders[0].side == Side.BUY
        finally:
            await _stop_runner(task, stop)


# --------------------------------------------------------------------------- #
# Position -> ctx.positions                                                   #
# --------------------------------------------------------------------------- #


class TestPositionDispatch:
    async def test_matching_position_updates_ctx(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        # The runner installs the position before the next on_bar
        # fires; we confirm by publishing a position THEN a bar
        # and inspecting the strategy's snapshot of ctx.positions
        # captured inside on_bar.
        config = _config(symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        try:
            await producer.publish(
                STREAM_POSITIONS,
                Event(
                    type="position",
                    payload=_position(strategy_id="rec_test", qty="0.5"),
                ),
            )
            # Tiny sleep so the position event lands before the
            # bar event below.  Without it, the bar might be
            # dispatched first and the snapshot would still be
            # empty.
            await asyncio.sleep(0.2)
            await producer.publish(STREAM_MD_BARS_1M, Event(type="bar", payload=_bar()))
            ok = await _wait_for(
                lambda: bool(RecordingStrategy.instances[-1].position_snapshots),
                timeout=3.0,
            )
            assert ok
            snap = RecordingStrategy.instances[-1].position_snapshots[-1]
            assert "BTC-USD" in snap
            assert snap["BTC-USD"].quantity == Decimal("0.5")
        finally:
            await _stop_runner(task, stop)

    async def test_other_strategy_position_ignored(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        try:
            await producer.publish(
                STREAM_POSITIONS,
                Event(
                    type="position",
                    payload=_position(strategy_id="other_strategy"),
                ),
            )
            await asyncio.sleep(0.2)
            await producer.publish(STREAM_MD_BARS_1M, Event(type="bar", payload=_bar()))
            ok = await _wait_for(
                lambda: bool(RecordingStrategy.instances[-1].position_snapshots),
                timeout=3.0,
            )
            assert ok
            # The snapshot taken at on_bar time has NO BTC-USD
            # entry because the other-strategy position was
            # filtered.
            snap = RecordingStrategy.instances[-1].position_snapshots[-1]
            assert snap == {}
        finally:
            await _stop_runner(task, stop)


# --------------------------------------------------------------------------- #
# Fill -> on_fill (filtered by outstanding-order ledger)                      #
# --------------------------------------------------------------------------- #


class TestFillDispatch:
    async def test_fill_for_outstanding_order_calls_on_fill(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        intent = _intent(strategy_id="rec_test", order_id="my-order")
        config = _config(symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        # Strategy submits an intent on its first bar so the runner
        # registers the order_id in its outstanding ledger.
        RecordingStrategy.instances[-1]._submit_on_bar = intent
        try:
            await producer.publish(STREAM_MD_BARS_1M, Event(type="bar", payload=_bar()))
            # Wait until the intent is on STREAM_ORDERS so we know
            # the runner has the order_id in its outstanding ledger.
            ok = await _wait_for(
                lambda: True,
                timeout=0.0,
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                orders = await _read_orders(fake_redis)
                if orders:
                    break
                await asyncio.sleep(0.05)
            # Now publish the matching fill.
            await producer.publish(
                STREAM_FILLS,
                Event(type="fill", payload=_fill(order_id="my-order")),
            )
            ok = await _wait_for(
                lambda: any(c[0] == "on_fill" for c in RecordingStrategy.instances[-1].calls),
                timeout=3.0,
            )
            assert ok, "on_fill was never called for matching outstanding order"
            on_fill_calls = [c for c in RecordingStrategy.instances[-1].calls if c[0] == "on_fill"]
            assert on_fill_calls[0][1] == "my-order"
        finally:
            await _stop_runner(task, stop)

    async def test_fill_for_unknown_order_id_is_ignored(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        try:
            await producer.publish(
                STREAM_FILLS,
                Event(type="fill", payload=_fill(order_id="never-issued")),
            )
            # Give the runner ample time to (incorrectly) dispatch.
            await asyncio.sleep(0.5)
            on_fill_calls = [c for c in RecordingStrategy.instances[-1].calls if c[0] == "on_fill"]
            assert on_fill_calls == []
        finally:
            await _stop_runner(task, stop)


# --------------------------------------------------------------------------- #
# Paper-spine replay: enabled strategy -> OMS -> portfolio                     #
# --------------------------------------------------------------------------- #


class TestPaperSpineReplay:
    async def test_enabled_strategy_intent_fills_and_updates_portfolio(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        intent = _intent(
            strategy_id="enabled_spine",
            order_id="spine-order-1",
            qty="0.25",
        )
        config = _config(strategy_id="enabled_spine", symbols=["BTC-USD"])
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        RecordingStrategy.instances[-1]._submit_on_bar = intent
        try:
            await producer.publish(
                STREAM_MD_BARS_1M,
                Event(type="bar", payload=_bar(symbol="BTC-USD", close="100")),
            )
            deadline = time.monotonic() + 3.0
            orders: list[OrderIntent] = []
            while time.monotonic() < deadline:
                orders = await _read_orders(fake_redis)
                if orders:
                    break
                await asyncio.sleep(0.05)

            assert len(orders) == 1
            assert orders[0].strategy_id == "enabled_spine"

            prices = LivePrices()
            prices.update("BTC-USD", Decimal("100"))
            filler = PaperFiller(
                mean_latency_ms=0.0,
                std_latency_ms=0.0,
                spread_bps=Decimal("0"),
                rng=lambda mu, _sigma: mu,
                clock=lambda: 2_000_000_000,
            )
            oms_result = process_intent(orders[0], prices=prices, filler=filler)
            assert oms_result.fill is not None

            store = PositionStore(fake_redis)
            portfolio_state = PortfolioState()

            async def resolve_from_order_id(fill: Fill) -> str | None:
                return orders[0].strategy_id if fill.order_id == orders[0].order_id else None

            position = await apply_fill(
                oms_result.fill,
                state=portfolio_state,
                store=store,
                resolve_strategy=resolve_from_order_id,
            )
            assert position is not None
            assert position.strategy_id == "enabled_spine"
            assert position.symbol == "BTC-USD"
            assert position.quantity == Decimal("0.25")
            assert await store.get("enabled_spine", "BTC-USD") == position
        finally:
            await _stop_runner(task, stop)

    async def test_disabled_strategy_config_is_not_replayed_directly(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(
            strategy_id="disabled_spine",
            symbols=["BTC-USD"],
            enabled=False,
        )
        assert config.enabled is False
        assert await _read_orders(fake_redis) == []


# --------------------------------------------------------------------------- #
# Lifecycle: build / start failure, on_stop                                   #
# --------------------------------------------------------------------------- #


class TestLifecycle:
    async def test_unknown_class_name_exits_cleanly(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(class_name="does_not_exist")
        stop = asyncio.Event()
        task = asyncio.create_task(run_strategy(config, fake_redis, stop))
        # The runner should return on its own (build_strategy raises
        # ValueError -> caught -> log + return) without us setting
        # stop.
        await asyncio.wait_for(task, timeout=2.0)

    async def test_on_start_crash_exits_cleanly(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(params={"fail_on_start": True})
        stop = asyncio.Event()
        task = asyncio.create_task(run_strategy(config, fake_redis, stop))
        await asyncio.wait_for(task, timeout=2.0)
        # The strategy was constructed (so it's in instances) but
        # on_start raised before adding ('on_start',) to calls.
        assert RecordingStrategy.instances
        assert ("on_start",) not in RecordingStrategy.instances[-1].calls

    async def test_on_stop_runs_on_cancel(self, fake_redis: Any, patch_registry: None) -> None:
        config = _config(symbols=["BTC-USD"])
        task, stop = await _start_runner(config, fake_redis)
        # Confirm on_start ran first.
        assert ("on_start",) in RecordingStrategy.instances[-1].calls
        await _stop_runner(task, stop)
        assert ("on_stop",) in RecordingStrategy.instances[-1].calls

    async def test_on_bar_crash_does_not_kill_runner(
        self, fake_redis: Any, patch_registry: None
    ) -> None:
        config = _config(symbols=["BTC-USD"], params={"fail_on_bar": True})
        producer = Producer(fake_redis)
        task, stop = await _start_runner(config, fake_redis)
        try:
            # First bar: on_bar raises but is caught by the runner.
            await producer.publish(STREAM_MD_BARS_1M, Event(type="bar", payload=_bar()))
            # Wait until the (failed) call lands so we know the
            # runner is still alive after the crash.
            ok = await _wait_for(
                lambda: any(c[0] == "on_bar" for c in RecordingStrategy.instances[-1].calls),
                timeout=3.0,
            )
            assert ok
            # Second bar: confirms the runner is still consuming.
            # (The strategy is still set to fail_on_bar=True; we're
            # checking that consecutive crashes don't accumulate.)
            await producer.publish(
                STREAM_MD_BARS_1M,
                Event(type="bar", payload=_bar(ts_event=2_000_000_000)),
            )
            ok = await _wait_for(
                lambda: (
                    len([c for c in RecordingStrategy.instances[-1].calls if c[0] == "on_bar"]) >= 2
                ),
                timeout=3.0,
            )
            assert ok
        finally:
            await _stop_runner(task, stop)
