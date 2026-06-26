"""
Tests for ``strategy_host.supervisor``.

The supervisor's job is reconciliation: read configs, compare to the
running set, start / stop / restart tasks to make them match.  These
tests inject a controllable mock runner so the supervisor's logic
can be exercised without depending on the F3 bar/fill machinery.

Crucially: the mock runner records every (config, lifecycle event)
so we can assert "this strategy_id was started exactly once",
"its restart was triggered by a config change", etc.
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from typing import Any

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from strategy_host.supervisor import (
    Supervisor,
    _runtime_signature,
)

from fincept_core.strategy_config import (
    StrategyConfig,
    StrategyConfigStore,
)

# --------------------------------------------------------------------------- #
# Helpers + fixtures                                                          #
# --------------------------------------------------------------------------- #


def _cfg(
    strategy_id: str,
    *,
    class_name: str = "buy_and_hold",
    symbols: list[str] | None = None,
    params: dict[str, Any] | None = None,
    model_binding: str | None = None,
    enabled: bool = True,
) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        class_name=class_name,
        symbols=symbols or ["BTC-USD"],
        params=params or {},
        model_binding=model_binding,
        enabled=enabled,
        created_at=0.0,
        updated_at=0.0,
    )


class RecordingRunner:
    """Mock runner the supervisor will spawn one task per strategy of.

    Tracks each call so tests can assert on the lifecycle:

      ``starts``   list of (strategy_id, signature) at start time
      ``stops``    list of strategy_ids that completed (cancellation
                   counts as completion).

    The runner blocks on ``stop.wait()`` so the supervisor's
    cancellation path is exercised; tests that need an *immediate
    crash* runner use :meth:`crash_next`.
    """

    def __init__(self) -> None:
        self.starts: list[tuple[str, str]] = []
        self.stops: list[str] = []
        self._crash_ids: set[str] = set()
        self._crash_after: float = 0.0

    def crash_next(self, strategy_id: str, *, after: float = 0.0) -> None:
        self._crash_ids.add(strategy_id)
        self._crash_after = after

    async def __call__(
        self,
        config: StrategyConfig,
        redis: Redis[Any],
        stop: asyncio.Event,
    ) -> None:
        del redis
        self.starts.append((config.strategy_id, _runtime_signature(config)))
        try:
            if config.strategy_id in self._crash_ids:
                if self._crash_after > 0:
                    await asyncio.sleep(self._crash_after)
                raise RuntimeError(f"intentional crash for {config.strategy_id}")
            await stop.wait()
        finally:
            self.stops.append(config.strategy_id)


@pytest.fixture
def store(tmp_path: pathlib.Path) -> StrategyConfigStore:
    return StrategyConfigStore(configs_dir=tmp_path / "strategies")


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


@pytest_asyncio.fixture
async def fake_redis() -> Any:
    """fakeredis async client; the F2 supervisor never actually uses
    it but the runner signature requires one so we pass a real-ish
    instance to keep the type system happy.
    """
    pytest.importorskip("fakeredis")
    import fakeredis.aioredis  # type: ignore[import-not-found]

    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def supervisor(
    store: StrategyConfigStore,
    runner: RecordingRunner,
    fake_redis: Any,
) -> Supervisor:
    return Supervisor(
        store=store,
        redis=fake_redis,
        runner=runner,
        poll_interval_sec=0.05,
    )


async def _yield(times: int = 1) -> None:
    """Pump the event loop ``times`` times so spawned tasks make
    progress.  Cheaper and more deterministic than asyncio.sleep(0)
    for assertions that only need "one or two task switches".
    """
    for _ in range(times):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# Runtime signature                                                           #
# --------------------------------------------------------------------------- #


class TestRuntimeSignature:
    def test_same_runtime_fields_same_signature(self) -> None:
        a = _cfg("x", params={"fast": 5}, enabled=True)
        # Different ``enabled`` and ``updated_at``-ish fields mustn't
        # shift the signature; the supervisor uses ``enabled`` for
        # start/stop, not restart.
        b = dataclasses.replace(a, enabled=False)
        assert _runtime_signature(a) == _runtime_signature(b)

    def test_param_change_changes_signature(self) -> None:
        a = _cfg("x", params={"fast": 5})
        b = _cfg("x", params={"fast": 7})
        assert _runtime_signature(a) != _runtime_signature(b)

    def test_symbol_change_changes_signature(self) -> None:
        a = _cfg("x", symbols=["BTC-USD"])
        b = _cfg("x", symbols=["BTC-USD", "ETH-USD"])
        assert _runtime_signature(a) != _runtime_signature(b)

    def test_model_binding_change_changes_signature(self) -> None:
        a = _cfg("x", model_binding=None)
        b = _cfg("x", model_binding="gbm_predictor.v1")
        assert _runtime_signature(a) != _runtime_signature(b)

    def test_class_name_change_changes_signature(self) -> None:
        a = _cfg("x", class_name="buy_and_hold")
        b = _cfg("x", class_name="ma_crossover")
        assert _runtime_signature(a) != _runtime_signature(b)


# --------------------------------------------------------------------------- #
# Single-tick reconcile                                                       #
# --------------------------------------------------------------------------- #


class TestTick:
    async def test_empty_store_no_runners(
        self, supervisor: Supervisor, runner: RecordingRunner
    ) -> None:
        await supervisor.tick()
        assert supervisor.running == set()
        assert runner.starts == []

    async def test_enabled_config_starts_runner(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", enabled=True))
        await supervisor.tick()
        await _yield()
        assert supervisor.running == {"alpha"}
        assert [s[0] for s in runner.starts] == ["alpha"]

    async def test_disabled_config_does_not_start(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", enabled=False))
        await supervisor.tick()
        assert supervisor.running == set()
        assert runner.starts == []

    async def test_two_enabled_configs_start_two_runners(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", enabled=True))
        store.upsert(_cfg("beta", enabled=True))
        await supervisor.tick()
        await _yield()
        assert supervisor.running == {"alpha", "beta"}

    async def test_already_running_skip_on_repeat_tick(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        # If a config didn't change, repeated ticks must NOT respawn
        # the runner.  Two ticks in a row should produce exactly one
        # start.  The yield between ticks lets the spawned task body
        # actually execute (RecordingRunner appends to ``starts`` at
        # the top of its body); without it we'd assert before the
        # task got its turn on the loop.
        store.upsert(_cfg("alpha", enabled=True))
        await supervisor.tick()
        await _yield()
        await supervisor.tick()
        await _yield()
        assert len(runner.starts) == 1


# --------------------------------------------------------------------------- #
# Toggle enabled                                                              #
# --------------------------------------------------------------------------- #


class TestEnableToggle:
    async def test_enabling_starts_runner(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", enabled=False))
        await supervisor.tick()
        assert supervisor.running == set()
        store.set_enabled("alpha", enabled=True)
        await supervisor.tick()
        await _yield()
        assert supervisor.running == {"alpha"}

    async def test_disabling_cancels_runner(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", enabled=True))
        await supervisor.tick()
        await _yield()
        assert supervisor.running == {"alpha"}
        store.set_enabled("alpha", enabled=False)
        await supervisor.tick()
        assert supervisor.running == set()
        assert runner.stops == ["alpha"]


# --------------------------------------------------------------------------- #
# Config change -> restart                                                    #
# --------------------------------------------------------------------------- #


class TestRestartOnChange:
    async def test_param_change_restarts(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", params={"fast": 5}, enabled=True))
        await supervisor.tick()
        await _yield()
        assert len(runner.starts) == 1
        store.upsert(_cfg("alpha", params={"fast": 7}, enabled=True))
        await supervisor.tick()
        await _yield()
        # Started twice (initial + restart), stopped once (the
        # cancel of the first task).
        assert len(runner.starts) == 2
        assert runner.stops == ["alpha"]
        assert supervisor.running == {"alpha"}

    async def test_idempotent_upsert_does_not_restart(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        # An upsert that doesn't change runtime fields bumps
        # ``updated_at`` only.  The supervisor must not restart on
        # that signal alone.
        store.upsert(_cfg("alpha", enabled=True))
        await supervisor.tick()
        await _yield()
        store.upsert(_cfg("alpha", enabled=True))  # same fields
        await supervisor.tick()
        await _yield()
        assert len(runner.starts) == 1
        assert runner.stops == []

    async def test_model_binding_change_restarts(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", model_binding=None, enabled=True))
        await supervisor.tick()
        await _yield()
        store.upsert(_cfg("alpha", model_binding="gbm_predictor.v1", enabled=True))
        await supervisor.tick()
        await _yield()
        assert len(runner.starts) == 2
        assert runner.stops == ["alpha"]


# --------------------------------------------------------------------------- #
# Crash semantics                                                             #
# --------------------------------------------------------------------------- #


class TestCrashRecovery:
    async def test_crashed_runner_is_reaped_and_restarted(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        # Round 1: runner crashes during execution.
        runner.crash_next("alpha")
        store.upsert(_cfg("alpha", enabled=True))
        await supervisor.tick()
        # Yield enough to let the runner raise + the task complete.
        for _ in range(5):
            await asyncio.sleep(0)
        # Round 2: supervisor reaps the dead task and restarts.
        # After reaping, the desired state (enabled=True) is unmet,
        # so a fresh runner spawns.  Disable the crash trigger so
        # the second start runs successfully.
        runner._crash_ids.clear()
        await supervisor.tick()
        await _yield()
        assert len(runner.starts) == 2
        # Both starts had the same signature (config didn't change).
        assert runner.starts[0][1] == runner.starts[1][1]
        assert supervisor.running == {"alpha"}

    async def test_dead_task_does_not_persist_in_running_set(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        # If the supervisor never reaped, the running set would
        # incorrectly include a dead strategy_id and a subsequent
        # tick would skip the desired start.  Verify that a tick
        # immediately after a crash sees ``running == set()``.
        runner.crash_next("alpha")
        store.upsert(_cfg("alpha", enabled=True))
        await supervisor.tick()
        for _ in range(5):
            await asyncio.sleep(0)
        # Disable so the supervisor doesn't try to restart.
        store.set_enabled("alpha", enabled=False)
        await supervisor.tick()
        assert supervisor.running == set()


# --------------------------------------------------------------------------- #
# Shutdown                                                                    #
# --------------------------------------------------------------------------- #


class TestShutdown:
    async def test_run_cancels_all_runners_on_stop(
        self,
        supervisor: Supervisor,
        store: StrategyConfigStore,
        runner: RecordingRunner,
    ) -> None:
        store.upsert(_cfg("alpha", enabled=True))
        store.upsert(_cfg("beta", enabled=True))
        stop = asyncio.Event()
        # Run the supervisor as a background task; let it run a
        # tick or two, then signal stop.
        run_task = asyncio.create_task(supervisor.run(stop))
        # Wait for both runners to be registered.
        for _ in range(20):
            if supervisor.running == {"alpha", "beta"}:
                break
            await asyncio.sleep(0.01)
        assert supervisor.running == {"alpha", "beta"}
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)
        # All runners have been cancelled and reaped.
        assert supervisor.running == set()
        assert sorted(runner.stops) == ["alpha", "beta"]

    async def test_run_survives_tick_exception(
        self,
        store: StrategyConfigStore,
        runner: RecordingRunner,
        fake_redis: Any,
    ) -> None:
        # If list_all() raises (transient FS error, etc.), the run
        # loop must log and continue.  We simulate by patching the
        # store to throw once, then succeed on the next tick.
        sup = Supervisor(store=store, redis=fake_redis, runner=runner, poll_interval_sec=0.02)

        original_list = store.list_all
        calls = {"count": 0}

        def flaky_list_all() -> list[StrategyConfig]:
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("transient")
            return original_list()

        store.list_all = flaky_list_all  # type: ignore[method-assign]
        store.upsert(_cfg("alpha", enabled=True))

        stop = asyncio.Event()
        run_task = asyncio.create_task(sup.run(stop))
        # Wait until we observe at least 2 list_all calls and the
        # runner has started.
        for _ in range(50):
            if calls["count"] >= 2 and "alpha" in sup.running:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)
        assert calls["count"] >= 2
        # The supervisor recovered: it ran multiple ticks and
        # eventually started the runner.
        assert "alpha" in [s[0] for s in runner.starts]
