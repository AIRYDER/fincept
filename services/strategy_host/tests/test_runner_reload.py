"""
Tests for the model-binding resolver + hot-reload watcher inside
``strategy_host.runner``.

These complement ``test_runner.py`` (which covers the bar / fill /
position dispatch path) by exercising the F4 model lifecycle:

  * binding resolves at start -> ``model_dir`` injected into params
  * unresolvable binding -> runner exits cleanly (no on_start)
  * pointer change while running -> reload_from_dir called with new path
  * malformed pointer mid-run -> watcher logs and keeps current model
  * reload_from_dir raises -> runner survives, previous model preserved
  * strategy class without reload_from_dir + binding -> no watcher spawned

We use a stub ``ReloadingStrategy`` registered in the registry via
monkey-patch so we don't need real lightgbm + trained models inside
these unit-level tests.  The integration of the real GBMStrategy +
runner is covered by the GBMStrategy tests in
``services/backtester/tests/test_gbm_strategy.py``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from typing import Any, ClassVar

import pytest
import pytest_asyncio

from fincept_core.schemas import BarEvent, Fill
from fincept_core.strategy_config import StrategyConfig
from fincept_sdk import Strategy, StrategyContext

from strategy_host.runner import run_strategy


# --------------------------------------------------------------------------- #
# Stub strategy with reload_from_dir                                          #
# --------------------------------------------------------------------------- #


class ReloadingStrategy(Strategy):
    """Test-only Strategy that records reload calls.

    Registered in STRATEGY_REGISTRY via ``patch_registry`` so the
    runner can build it via ``backtester.runner.build_strategy``.
    Importantly:

      * Accepts ``model_dir`` in __init__ -- the runner ALWAYS
        injects this when the config has a model_binding, so the
        constructor must take it.
      * Implements ``reload_from_dir`` -- this triggers the runner
        to spawn the watcher.

    Crash injection toggles let tests exercise the failure paths
    without monkey-patching after construction (the runner builds
    + on_start in one go, so attribute writes from the test only
    take effect AFTER on_start; for some tests we need the failure
    visible during on_start, which is why ``__init__`` toggles win
    over post-hoc attribute writes).
    """

    strategy_id: ClassVar[str] = "reloading.v1"
    symbols: ClassVar[list[str]] = []
    instances: ClassVar[list[ReloadingStrategy]] = []

    def __init__(
        self,
        symbols: list[str],
        *,
        model_dir: pathlib.Path | str,
        reload_should_fail: bool = False,
    ) -> None:
        self.symbols = list(symbols)  # type: ignore[misc]
        self._model_dir = pathlib.Path(model_dir)
        self.reloads: list[pathlib.Path] = []
        self.reload_should_fail = reload_should_fail
        ReloadingStrategy.instances.append(self)

    def on_start(self, ctx: StrategyContext) -> None:
        return

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        return

    def on_tick(self, ctx: StrategyContext, trade: Any) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        return

    def on_signal(self, ctx: StrategyContext, signal: Any) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        return

    def reload_from_dir(self, model_dir: pathlib.Path | str) -> None:
        if self.reload_should_fail:
            raise RuntimeError("intentional reload crash")
        new_dir = pathlib.Path(model_dir)
        self._model_dir = new_dir
        self.reloads.append(new_dir)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
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
    from backtester.strategies import STRATEGY_REGISTRY

    monkeypatch.setitem(
        STRATEGY_REGISTRY, "reloading", ReloadingStrategy
    )
    ReloadingStrategy.instances.clear()


@pytest.fixture
def models_tree(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> pathlib.Path:
    """Isolated models/active tree under tmp_path, env-overridden."""
    models = tmp_path / "models"
    active = models / "active"
    active.mkdir(parents=True)
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("ACTIVE_MODELS_DIR", str(active))
    return models


def _write_pointer(
    models: pathlib.Path, binding: str, model_name: str
) -> None:
    active = models / "active"
    (active / f"{binding}.json").write_text(
        json.dumps({"model_name": model_name})
    )


def _config(
    *,
    strategy_id: str = "rl_test",
    class_name: str = "reloading",
    binding: str | None = "test_binding",
    params: dict[str, Any] | None = None,
) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        class_name=class_name,
        symbols=["BTC-USD"],
        params=params or {},
        model_binding=binding,
        enabled=True,
        created_at=0.0,
        updated_at=0.0,
    )


async def _wait_for(
    predicate: Any, *, timeout: float = 3.0, interval: float = 0.02
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# Initial resolution                                                          #
# --------------------------------------------------------------------------- #


class TestInitialResolution:
    async def test_resolved_binding_injects_model_dir(
        self,
        fake_redis: Any,
        patch_registry: None,
        models_tree: pathlib.Path,
    ) -> None:
        _write_pointer(models_tree, "test_binding", "model_v1")
        config = _config()
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_strategy(
                config, fake_redis, stop, reload_poll_interval_s=10.0
            )
        )
        # Wait for the strategy to be constructed -- this signals
        # that the binding was resolved + injected.
        ok = await _wait_for(lambda: bool(ReloadingStrategy.instances))
        assert ok
        try:
            assert (
                ReloadingStrategy.instances[-1]._model_dir  # noqa: SLF001
                == models_tree / "model_v1"
            )
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_unresolvable_binding_exits_cleanly(
        self,
        fake_redis: Any,
        patch_registry: None,
        models_tree: pathlib.Path,
    ) -> None:
        # No pointer file written; resolver returns None; runner
        # logs "binding_unresolvable" and returns BEFORE constructing
        # the strategy.
        config = _config(binding="never_promoted")
        stop = asyncio.Event()
        task = asyncio.create_task(run_strategy(config, fake_redis, stop))
        # Should return on its own without us setting stop.
        await asyncio.wait_for(task, timeout=2.0)
        assert ReloadingStrategy.instances == []

    async def test_no_binding_skips_resolver(
        self,
        fake_redis: Any,
        patch_registry: None,
        models_tree: pathlib.Path,
    ) -> None:
        # When model_binding is None, the runner does NOT touch the
        # resolver and does NOT inject model_dir.  This means a
        # strategy class that requires model_dir would fail at
        # build time -- which is the right behaviour: an operator
        # who configures GBMStrategy without a binding (or
        # explicit model_dir param) is misconfiguring.
        # We use the reloading strategy here with explicit
        # model_dir in params; the runner should pass it through.
        config = _config(
            binding=None, params={"model_dir": str(models_tree / "x")}
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_strategy(
                config, fake_redis, stop, reload_poll_interval_s=10.0
            )
        )
        ok = await _wait_for(lambda: bool(ReloadingStrategy.instances))
        assert ok
        try:
            # No watcher should have been spawned -- the no-binding
            # config means we don't poll.  We can't observe this
            # directly from the test, but we CAN verify that no
            # reload calls happen even after a long wait.
            assert ReloadingStrategy.instances[-1].reloads == []
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)


# --------------------------------------------------------------------------- #
# Hot-reload via pointer change                                               #
# --------------------------------------------------------------------------- #


class TestPointerChange:
    async def test_pointer_change_triggers_reload(
        self,
        fake_redis: Any,
        patch_registry: None,
        models_tree: pathlib.Path,
    ) -> None:
        _write_pointer(models_tree, "test_binding", "model_v1")
        config = _config()
        stop = asyncio.Event()
        # Tight poll interval so the test doesn't sit idle.  0.05s is
        # plenty of margin against asyncio scheduling jitter without
        # being so tight it busy-loops.
        task = asyncio.create_task(
            run_strategy(
                config, fake_redis, stop, reload_poll_interval_s=0.05
            )
        )
        try:
            ok = await _wait_for(lambda: bool(ReloadingStrategy.instances))
            assert ok
            strategy = ReloadingStrategy.instances[-1]
            # No reloads YET -- the watcher only fires on changes.
            assert strategy.reloads == []
            # Promote model_v2.
            _write_pointer(models_tree, "test_binding", "model_v2")
            ok = await _wait_for(
                lambda: len(strategy.reloads) >= 1, timeout=3.0
            )
            assert ok
            assert strategy.reloads[0] == models_tree / "model_v2"
            # Promote again.
            _write_pointer(models_tree, "test_binding", "model_v3")
            ok = await _wait_for(
                lambda: len(strategy.reloads) >= 2, timeout=3.0
            )
            assert ok
            assert strategy.reloads[1] == models_tree / "model_v3"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_unchanged_pointer_does_not_reload(
        self,
        fake_redis: Any,
        patch_registry: None,
        models_tree: pathlib.Path,
    ) -> None:
        # Pointer stays at model_v1; multiple poll cycles pass; no
        # reloads should fire.  This is the steady-state cost of
        # the watcher we want to be sure stays at zero.
        _write_pointer(models_tree, "test_binding", "model_v1")
        config = _config()
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_strategy(
                config, fake_redis, stop, reload_poll_interval_s=0.05
            )
        )
        try:
            ok = await _wait_for(lambda: bool(ReloadingStrategy.instances))
            assert ok
            strategy = ReloadingStrategy.instances[-1]
            # Wait long enough for many poll cycles.
            await asyncio.sleep(0.4)
            assert strategy.reloads == []
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)

    async def test_malformed_pointer_does_not_break_runner(
        self,
        fake_redis: Any,
        patch_registry: None,
        models_tree: pathlib.Path,
    ) -> None:
        _write_pointer(models_tree, "test_binding", "model_v1")
        config = _config()
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_strategy(
                config, fake_redis, stop, reload_poll_interval_s=0.05
            )
        )
        try:
            ok = await _wait_for(lambda: bool(ReloadingStrategy.instances))
            assert ok
            strategy = ReloadingStrategy.instances[-1]
            # Corrupt the pointer.  Resolver returns None; watcher
            # logs a warning and skips -- the previous model stays
            # loaded.  The runner must NOT crash.
            (models_tree / "active" / "test_binding.json").write_text(
                "not-json{{{"
            )
            await asyncio.sleep(0.2)
            # No reloads triggered, runner still alive.
            assert strategy.reloads == []
            assert not task.done()
            # Recovery: write a valid pointer to a new model and
            # verify the watcher picks it up despite the prior
            # malformed read.
            _write_pointer(models_tree, "test_binding", "model_v2")
            ok = await _wait_for(
                lambda: len(strategy.reloads) >= 1, timeout=3.0
            )
            assert ok
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)


# --------------------------------------------------------------------------- #
# Reload failure handling                                                     #
# --------------------------------------------------------------------------- #


class TestReloadFailure:
    async def test_reload_failure_keeps_runner_alive(
        self,
        fake_redis: Any,
        patch_registry: None,
        models_tree: pathlib.Path,
    ) -> None:
        # Start with reload_should_fail=True so the FIRST reload
        # raises.  The runner's watcher must log + continue;
        # subsequent successful reloads must still apply.
        _write_pointer(models_tree, "test_binding", "model_v1")
        config = _config(params={"reload_should_fail": True})
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_strategy(
                config, fake_redis, stop, reload_poll_interval_s=0.05
            )
        )
        try:
            ok = await _wait_for(lambda: bool(ReloadingStrategy.instances))
            assert ok
            strategy = ReloadingStrategy.instances[-1]
            _write_pointer(models_tree, "test_binding", "model_v2")
            await asyncio.sleep(0.3)
            # Reload was attempted (raised) so reloads is empty
            # (the strategy's record-on-success append never ran).
            assert strategy.reloads == []
            # Runner is still alive and consuming.
            assert not task.done()
            # Operator fixes the strategy: disable the failure
            # flag and trigger another pointer change.
            strategy.reload_should_fail = False
            _write_pointer(models_tree, "test_binding", "model_v3")
            ok = await _wait_for(
                lambda: len(strategy.reloads) >= 1, timeout=3.0
            )
            assert ok
            assert strategy.reloads[0] == models_tree / "model_v3"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)


# --------------------------------------------------------------------------- #
# Strategy without reload_from_dir + binding                                  #
# --------------------------------------------------------------------------- #


class _NoReloadStrategy(Strategy):
    """Strategy class that accepts model_dir but DOES NOT have
    reload_from_dir.  Used to verify the runner skips the watcher
    when the strategy class can't hot-reload."""

    strategy_id: ClassVar[str] = "noreload.v1"
    symbols: ClassVar[list[str]] = []
    instances: ClassVar[list[_NoReloadStrategy]] = []

    def __init__(
        self,
        symbols: list[str],
        *,
        model_dir: pathlib.Path | str,
    ) -> None:
        self.symbols = list(symbols)  # type: ignore[misc]
        self._model_dir = pathlib.Path(model_dir)
        _NoReloadStrategy.instances.append(self)

    def on_start(self, ctx: StrategyContext) -> None:
        return

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> None:
        return

    def on_tick(self, ctx: StrategyContext, trade: Any) -> None:
        return

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        return

    def on_signal(self, ctx: StrategyContext, signal: Any) -> None:
        return

    def on_stop(self, ctx: StrategyContext) -> None:
        return


class TestStrategyWithoutReload:
    async def test_no_reload_method_skips_watcher(
        self,
        fake_redis: Any,
        monkeypatch: pytest.MonkeyPatch,
        models_tree: pathlib.Path,
    ) -> None:
        from backtester.strategies import STRATEGY_REGISTRY

        monkeypatch.setitem(
            STRATEGY_REGISTRY, "noreload", _NoReloadStrategy
        )
        _NoReloadStrategy.instances.clear()
        _write_pointer(models_tree, "test_binding", "model_v1")
        config = _config(class_name="noreload")
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_strategy(
                config, fake_redis, stop, reload_poll_interval_s=0.05
            )
        )
        try:
            ok = await _wait_for(
                lambda: bool(_NoReloadStrategy.instances)
            )
            assert ok
            # Promote a new model.  Without the watcher, nothing
            # happens.  We can verify this by promoting and waiting
            # several poll cycles -- no exception, runner still
            # alive, no observable side-effect.
            _write_pointer(models_tree, "test_binding", "model_v2")
            await asyncio.sleep(0.2)
            assert not task.done()
            # The strategy is still pinned to its initial model_dir
            # because no reload was possible.
            assert (
                _NoReloadStrategy.instances[-1]._model_dir  # noqa: SLF001
                == models_tree / "model_v1"
            )
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)
