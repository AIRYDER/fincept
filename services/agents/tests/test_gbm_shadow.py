"""
Shadow-slot tests for ``agents.gbm_predictor.main`` (Phase E2).

Mirrors ``test_gbm_hot_reload.py``: dependency-injected fakes for
``build_agent``, ``publish_loop``, ``shadow_loop``, and ``heartbeat``,
plus a fakeredis instance.  Real ``_resolve_model_dir`` and
``_resolve_shadow_model_dir`` are exercised against tmp pointer files.

Five scenarios:

  1. **Shadow loaded on startup.**  A pre-existing shadow.json causes
     the run loop to spawn a shadow task in addition to the active
     publish task.

  2. **Shadow set mid-run (None -> Path).**  Writing a shadow.json
     while the agent is running causes a shadow task to spawn within
     one poll interval, without disturbing the active publish task.

  3. **Shadow cleared mid-run (Path -> None).**  Removing the
     shadow.json file triggers a clean shadow teardown.

  4. **Shadow swapped (Path -> Path').**  Updating shadow.json to a
     different model swaps the shadow agent in-place.

  5. **Shadow load failure.**  A pointer to a missing model dir is
     logged as a warning; the active task continues unaffected and
     the shadow_task remains ``None``.

The shadow loop must NEVER be spawned with a producer; the ``shadow_loop``
fake we inject has no producer parameter at all (defence in depth: the
production ``_shadow_loop`` signature also has no producer, so even an
operator mistake of editing the agent code can't accidentally publish
shadow predictions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from collections.abc import AsyncIterator
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from agents.gbm_predictor import main as gbm_main


# --------------------------------------------------------------------------- #
# Stubs                                                                      #
# --------------------------------------------------------------------------- #


class _StubAgent:
    """Same shape as the hot-reload tests' stub.  See that file for
    discussion -- we duplicate here rather than importing because
    pytest's collection treats imports across test files unevenly."""

    def __init__(self, model_dir: pathlib.Path) -> None:
        self.model_dir = model_dir
        self.teardown_count = 0

    async def teardown(self) -> None:
        self.teardown_count += 1


def _write_active_pointer(active_dir: pathlib.Path, model_name: str) -> None:
    active_dir.mkdir(parents=True, exist_ok=True)
    pointer = active_dir / f"{gbm_main.AGENT_ID}.json"
    pointer.write_text(
        json.dumps(
            {
                "agent_id": gbm_main.AGENT_ID,
                "model_name": model_name,
                "promoted_at": 0,
                "promoted_by": "test",
            }
        )
    )


def _write_shadow_pointer(active_dir: pathlib.Path, model_name: str) -> None:
    active_dir.mkdir(parents=True, exist_ok=True)
    pointer = active_dir / f"{gbm_main.AGENT_ID}.shadow.json"
    pointer.write_text(
        json.dumps(
            {
                "agent_id": gbm_main.AGENT_ID,
                "model_name": model_name,
                "promoted_at": 0,
                "promoted_by": "test",
            }
        )
    )


def _clear_shadow_pointer(active_dir: pathlib.Path) -> None:
    pointer = active_dir / f"{gbm_main.AGENT_ID}.shadow.json"
    if pointer.is_file():
        pointer.unlink()


async def _quiet_publish(agent: Any, producer: Any, **kwargs: Any) -> None:
    try:
        while True:
            await asyncio.sleep(60.0)
    except asyncio.CancelledError:
        raise


async def _quiet_shadow(agent: Any, **kwargs: Any) -> None:
    """Shadow-loop stand-in.  No producer parameter (mirrors prod sig)."""
    try:
        while True:
            await asyncio.sleep(60.0)
    except asyncio.CancelledError:
        raise


async def _quiet_heartbeat(redis: Any, name: str, **kwargs: Any) -> None:
    try:
        while True:
            await asyncio.sleep(60.0)
    except asyncio.CancelledError:
        raise


# --------------------------------------------------------------------------- #
# Fixtures                                                                   #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis[Any]]:
    client = fakeredis.aioredis.FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def models_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Tmp models tree with three candidate dirs (active, shadow_a, shadow_b).

    The dirs are empty -- the stub agent never reads files from them.
    """
    root = tmp_path / "models"
    (root / "alpha").mkdir(parents=True)
    (root / "shadow_a").mkdir(parents=True)
    (root / "shadow_b").mkdir(parents=True)
    return root


@pytest.fixture
def active_dir(models_root: pathlib.Path) -> pathlib.Path:
    d = models_root / "active"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(autouse=True)
def _patch_env(
    monkeypatch: pytest.MonkeyPatch, models_root: pathlib.Path
) -> None:
    monkeypatch.setenv("MODELS_DIR", str(models_root))
    monkeypatch.setenv("ACTIVE_MODELS_DIR", str(models_root / "active"))
    monkeypatch.delenv("GBM_MODEL_DIR", raising=False)
    logging.getLogger("agents.gbm_predictor.main").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Helpers                                                                    #
# --------------------------------------------------------------------------- #


async def _wait_for(predicate, timeout_s: float = 1.5, step_s: float = 0.02):
    """Poll ``predicate`` until it returns truthy, up to ``timeout_s``."""
    iters = max(1, int(timeout_s / step_s))
    for _ in range(iters):
        if predicate():
            return
        await asyncio.sleep(step_s)


# --------------------------------------------------------------------------- #
# 1. Shadow loaded on startup                                                #
# --------------------------------------------------------------------------- #


async def test_shadow_loaded_on_startup_when_pointer_present(
    models_root: pathlib.Path,
    active_dir: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """A shadow.json present at startup -> shadow agent built immediately."""
    _write_active_pointer(active_dir, "alpha")
    _write_shadow_pointer(active_dir, "shadow_a")

    builds: list[pathlib.Path] = []

    async def fake_build(model_dir: pathlib.Path, _r: Redis[Any]) -> _StubAgent:
        builds.append(model_dir)
        return _StubAgent(model_dir)

    stop = asyncio.Event()
    run_task = asyncio.create_task(
        gbm_main.run(
            stop,
            redis=redis,
            poll_interval_s=0.05,
            build_agent=fake_build,
            publish_loop=_quiet_publish,
            shadow_loop=_quiet_shadow,
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        # Two builds expected: active alpha + shadow_a.
        await _wait_for(lambda: len(builds) >= 2)
        assert models_root / "alpha" in builds
        assert models_root / "shadow_a" in builds
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)


# --------------------------------------------------------------------------- #
# 2. Shadow set mid-run                                                      #
# --------------------------------------------------------------------------- #


async def test_shadow_spawns_when_pointer_appears(
    models_root: pathlib.Path,
    active_dir: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """No shadow at start; operator writes shadow.json mid-run."""
    _write_active_pointer(active_dir, "alpha")

    builds: list[pathlib.Path] = []

    async def fake_build(model_dir: pathlib.Path, _r: Redis[Any]) -> _StubAgent:
        builds.append(model_dir)
        return _StubAgent(model_dir)

    stop = asyncio.Event()
    run_task = asyncio.create_task(
        gbm_main.run(
            stop,
            redis=redis,
            poll_interval_s=0.05,
            build_agent=fake_build,
            publish_loop=_quiet_publish,
            shadow_loop=_quiet_shadow,
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        await _wait_for(lambda: len(builds) >= 1)
        assert builds == [models_root / "alpha"]

        _write_shadow_pointer(active_dir, "shadow_a")
        await _wait_for(
            lambda: any(b == models_root / "shadow_a" for b in builds)
        )
        assert models_root / "shadow_a" in builds
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)


# --------------------------------------------------------------------------- #
# 3. Shadow cleared mid-run                                                  #
# --------------------------------------------------------------------------- #


async def test_shadow_torn_down_when_pointer_removed(
    models_root: pathlib.Path,
    active_dir: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """Shadow set at start; removing shadow.json triggers teardown."""
    _write_active_pointer(active_dir, "alpha")
    _write_shadow_pointer(active_dir, "shadow_a")

    builds: list[tuple[pathlib.Path, _StubAgent]] = []

    async def fake_build(model_dir: pathlib.Path, _r: Redis[Any]) -> _StubAgent:
        agent = _StubAgent(model_dir)
        builds.append((model_dir, agent))
        return agent

    stop = asyncio.Event()
    run_task = asyncio.create_task(
        gbm_main.run(
            stop,
            redis=redis,
            poll_interval_s=0.05,
            build_agent=fake_build,
            publish_loop=_quiet_publish,
            shadow_loop=_quiet_shadow,
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        await _wait_for(lambda: len(builds) >= 2)
        # Find the shadow_a stub.
        shadow_stub = next(
            agent for path, agent in builds if path == models_root / "shadow_a"
        )

        # Remove the pointer; watcher should tear down the shadow.
        _clear_shadow_pointer(active_dir)
        await _wait_for(lambda: shadow_stub.teardown_count >= 1)
        assert shadow_stub.teardown_count == 1
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)


# --------------------------------------------------------------------------- #
# 4. Shadow swapped                                                          #
# --------------------------------------------------------------------------- #


async def test_shadow_swaps_when_pointer_changes(
    models_root: pathlib.Path,
    active_dir: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """Shadow_a -> shadow_b: old torn down, new spawned."""
    _write_active_pointer(active_dir, "alpha")
    _write_shadow_pointer(active_dir, "shadow_a")

    builds: list[tuple[pathlib.Path, _StubAgent]] = []

    async def fake_build(model_dir: pathlib.Path, _r: Redis[Any]) -> _StubAgent:
        agent = _StubAgent(model_dir)
        builds.append((model_dir, agent))
        return agent

    stop = asyncio.Event()
    run_task = asyncio.create_task(
        gbm_main.run(
            stop,
            redis=redis,
            poll_interval_s=0.05,
            build_agent=fake_build,
            publish_loop=_quiet_publish,
            shadow_loop=_quiet_shadow,
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        await _wait_for(lambda: len(builds) >= 2)
        shadow_a_stub = next(
            agent for path, agent in builds if path == models_root / "shadow_a"
        )

        # Swap shadow_a -> shadow_b.
        _write_shadow_pointer(active_dir, "shadow_b")
        await _wait_for(
            lambda: any(p == models_root / "shadow_b" for p, _ in builds)
        )
        await _wait_for(lambda: shadow_a_stub.teardown_count >= 1)
        assert shadow_a_stub.teardown_count == 1
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)


# --------------------------------------------------------------------------- #
# 5. Shadow load failure leaves active untouched                             #
# --------------------------------------------------------------------------- #


async def test_shadow_load_failure_does_not_kill_run(
    models_root: pathlib.Path,
    active_dir: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """A bad shadow pointer must not propagate to the active task."""
    _write_active_pointer(active_dir, "alpha")
    _write_shadow_pointer(active_dir, "shadow_a")

    builds: list[pathlib.Path] = []

    async def flaky_build(model_dir: pathlib.Path, _r: Redis[Any]) -> _StubAgent:
        builds.append(model_dir)
        # Fail any shadow_* build; succeed for active alpha.
        if "shadow" in model_dir.name:
            raise FileNotFoundError(
                f"GBMPredictor model artifacts missing in {model_dir}"
            )
        return _StubAgent(model_dir)

    stop = asyncio.Event()
    run_task = asyncio.create_task(
        gbm_main.run(
            stop,
            redis=redis,
            poll_interval_s=0.05,
            build_agent=flaky_build,
            publish_loop=_quiet_publish,
            shadow_loop=_quiet_shadow,
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        # Both builds attempted.
        await _wait_for(lambda: len(builds) >= 2)
        # Active task still alive.
        assert not run_task.done()
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)
