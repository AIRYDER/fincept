"""Tests for fincept_sdk.strategy — ABC enforcement + Protocol conformance."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from fincept_core.schemas import BarEvent, Fill, OrderIntent, Position, TradeEvent
from fincept_sdk import Strategy, StrategyContext


def test_strategy_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        Strategy()  # type: ignore[abstract]


def test_concrete_strategy_must_implement_all_hooks() -> None:
    """Subclasses missing any lifecycle hook stay abstract and uninstantiable."""

    class HalfBaked(Strategy):
        strategy_id: ClassVar[str] = "half"
        symbols: ClassVar[list[str]] = ["BTC-USD"]

        def on_start(self, ctx: StrategyContext) -> None:
            pass

        # Missing on_bar / on_tick / on_fill / on_signal / on_stop.

    with pytest.raises(TypeError):
        HalfBaked()  # type: ignore[abstract]


def test_minimal_concrete_strategy_instantiates() -> None:
    """A subclass implementing every hook can be constructed."""

    class Noop(Strategy):
        strategy_id: ClassVar[str] = "noop"
        symbols: ClassVar[list[str]] = ["BTC-USD"]

        def on_start(self, ctx: StrategyContext) -> None:
            pass

        def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
            pass

        def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
            pass

        def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
            pass

        def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
            pass

        def on_stop(self, ctx: StrategyContext) -> None:
            pass

    s = Noop()
    assert s.strategy_id == "noop"
    assert s.symbols == ["BTC-USD"]


def test_strategy_context_is_structural() -> None:
    """Any object with the right attributes passes ``isinstance(StrategyContext)``."""

    class _MockCtx:
        def __init__(self) -> None:
            self.now_ns: int = 0
            self.positions: dict[str, Position] = {}

        def submit(self, intent: OrderIntent) -> str:
            return "x"

        def cancel(self, order_id: str) -> None:
            pass

        def get_feature(self, name: str, symbol: str) -> float | None:
            return None

        def log(self, msg: str, **kwargs: Any) -> None:
            pass

    ctx = _MockCtx()
    assert isinstance(ctx, StrategyContext)


def test_strategy_context_rejects_object_missing_required_attribute() -> None:
    """An object missing ``now_ns`` is NOT a StrategyContext."""

    class _Incomplete:
        def __init__(self) -> None:
            self.positions: dict[str, Position] = {}

        def submit(self, intent: OrderIntent) -> str:
            return "x"

        def cancel(self, order_id: str) -> None:
            pass

        def get_feature(self, name: str, symbol: str) -> float | None:
            return None

        def log(self, msg: str, **kwargs: Any) -> None:
            pass

    assert not isinstance(_Incomplete(), StrategyContext)


def test_strategy_holds_class_level_metadata() -> None:
    """``strategy_id`` and ``symbols`` are class attributes, accessible without an instance."""

    class Demo(Strategy):
        strategy_id: ClassVar[str] = "demo.v1"
        symbols: ClassVar[list[str]] = ["BTC-USD", "ETH-USD"]

        def on_start(self, ctx: StrategyContext) -> None:
            pass

        def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
            pass

        def on_tick(self, ctx: StrategyContext, trade: TradeEvent) -> None:
            pass

        def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
            pass

        def on_signal(self, ctx: StrategyContext, signal: BaseModel) -> None:
            pass

        def on_stop(self, ctx: StrategyContext) -> None:
            pass

    assert Demo.strategy_id == "demo.v1"
    assert Demo.symbols == ["BTC-USD", "ETH-USD"]
