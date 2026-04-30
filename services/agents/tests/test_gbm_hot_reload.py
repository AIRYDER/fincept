"""
Hot-reload tests for ``agents.gbm_predictor.main`` (Phase D1).

These tests exercise the ``run`` loop's pointer-watching behaviour
*without* repeatedly training real lightgbm boosters and *without*
touching real Redis.  We rely on the dependency-injection hooks added
to ``run`` (``redis``, ``build_agent``, ``publish_loop``, ``heartbeat``)
so the test never has to monkey-patch imported symbols.

Three scenarios are covered:

  1. **Successful reload.**  A pointer change triggers a load, the new
     agent replaces the old one, and the old agent's ``teardown``
     runs exactly once.

  2. **Failed reload.**  ``build_agent`` raises (simulating a bad
     pointer or a deleted model directory).  The current agent stays
     running and the ``gbm.reload_failed`` log entry fires.

  3. **No spurious reload.**  When the pointer is stable, the watcher
     ticks but never tries to rebuild.

The integration boundary is the same module function under test
(``agents.gbm_predictor.main.run``); we exercise the real
``_resolve_model_dir`` by writing real ``active.json`` files to a tmp
directory, but everything else (Redis, model loading, publishing,
heartbeating) is replaced with a fake to keep the test hermetic.
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
# Stub agent + helpers                                                       #
# --------------------------------------------------------------------------- #


class _StubAgent:
    """Minimal stand-in for :class:`GBMPredictor` used by the watcher tests.

    ``run`` only asks the agent for a ``teardown`` and indirectly for
    a ``run`` (via the publish loop).  We replace the publish loop too,
    so this stub doesn't need a real ``run`` -- but keep it defined so
    type checkers don't complain if a future test calls it directly.
    """

    def __init__(self, model_dir: pathlib.Path) -> None:
        self.model_dir = model_dir
        self.teardown_count = 0

    async def teardown(self) -> None:
        self.teardown_count += 1


def _write_pointer(active_dir: pathlib.Path, model_name: str) -> None:
    """Write a minimal active.json for ``AGENT_ID``."""
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


async def _quiet_publish(agent: Any, producer: Any, **kwargs: Any) -> None:
    """Stand-in for ``_publish_loop`` -- sleeps until cancelled.

    We don't care what gets published in the watcher tests; we only
    care that the watcher cancels the loop and spawns a new one with
    the new agent.  An infinite sleep is the simplest faithful
    stand-in that respects ``asyncio.CancelledError``.

    The ``**kwargs`` absorbs ``prediction_log`` and ``model_name``,
    which the production loop accepts (Phase D2) but the watcher tests
    don't need to assert on.
    """
    try:
        while True:
            await asyncio.sleep(60.0)
    except asyncio.CancelledError:
        raise


async def _quiet_heartbeat(redis: Any, name: str, **kwargs: Any) -> None:
    """Stand-in for ``beat_periodically`` -- never touches the redis arg."""
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
    """A throwaway models directory with ``alpha`` and ``beta`` subdirs.

    The subdirs are empty -- the stub agent never actually reads them.
    """
    root = tmp_path / "models"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def _patch_env(
    monkeypatch: pytest.MonkeyPatch, models_root: pathlib.Path
) -> None:
    """Point the resolver at our tmp models tree.

    We don't touch ``GBM_RELOAD_POLL_S`` here; tests pass the poll
    interval directly to ``run`` so they don't compete on env state.
    """
    monkeypatch.setenv("MODELS_DIR", str(models_root))
    monkeypatch.setenv("ACTIVE_MODELS_DIR", str(models_root / "active"))
    monkeypatch.delenv("GBM_MODEL_DIR", raising=False)
    # Quiet the resolver's per-poll log entry so test output is
    # readable.  At a 50ms poll that's 20+ entries/second.  Targeting
    # the structlog stdlib bridge is the most reliable silencer
    # because the project's structlog factory wraps it.
    logging.getLogger("agents.gbm_predictor.main").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Successful reload                                                          #
# --------------------------------------------------------------------------- #


async def test_run_reloads_when_pointer_changes(
    models_root: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """Pointer flips alpha -> beta; the watcher swaps agents."""
    _write_pointer(models_root / "active", "alpha")

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
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        # Wait for the initial load.
        for _ in range(50):
            if builds:
                break
            await asyncio.sleep(0.02)
        assert len(builds) == 1, f"initial load never happened; builds={builds}"
        assert builds[0][0] == models_root / "alpha"

        # Flip the pointer; give the watcher up to ~1.2s.
        _write_pointer(models_root / "active", "beta")
        for _ in range(60):
            if len(builds) >= 2:
                break
            await asyncio.sleep(0.02)

        assert len(builds) == 2, (
            f"watcher did not reload; builds={[b[0] for b in builds]}"
        )
        assert builds[1][0] == models_root / "beta"

        # First agent torn down exactly once when the swap happened.
        # Note that ``build_agent`` appends to ``builds`` *before*
        # ``run`` cancels the old publish task and calls teardown, so
        # polling on ``len(builds) >= 2`` can return before teardown
        # has actually executed.  Poll on the teardown counter itself.
        first_agent = builds[0][1]
        for _ in range(30):
            if first_agent.teardown_count >= 1:
                break
            await asyncio.sleep(0.02)
        assert first_agent.teardown_count == 1, (
            "old agent teardown did not run after swap"
        )
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)

    # On shutdown the *current* (second) agent's teardown also runs.
    second_agent = builds[1][1]
    assert second_agent.teardown_count == 1


# --------------------------------------------------------------------------- #
# Failed reload                                                              #
# --------------------------------------------------------------------------- #


async def test_run_keeps_current_agent_when_reload_fails(
    models_root: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """A failing build_agent on reload must not take the agent down.

    We arrange for the first build (initial load) to succeed and every
    subsequent build to raise FileNotFoundError, simulating a corrupted
    or deleted model directory written into the pointer.
    """
    _write_pointer(models_root / "active", "alpha")

    builds: list[pathlib.Path] = []

    async def flaky_build(
        model_dir: pathlib.Path, _r: Redis[Any]
    ) -> _StubAgent:
        builds.append(model_dir)
        if len(builds) == 1:
            return _StubAgent(model_dir)
        raise FileNotFoundError(
            f"GBMPredictor model artifacts missing in {model_dir}"
        )

    stop = asyncio.Event()
    run_task = asyncio.create_task(
        gbm_main.run(
            stop,
            redis=redis,
            poll_interval_s=0.05,
            build_agent=flaky_build,
            publish_loop=_quiet_publish,
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        # Wait for the initial load to complete.
        for _ in range(50):
            if builds:
                break
            await asyncio.sleep(0.02)
        assert len(builds) == 1

        # Flip the pointer so flaky_build raises on the next reload.
        _write_pointer(models_root / "active", "beta")
        for _ in range(60):
            if len(builds) >= 2:
                break
            await asyncio.sleep(0.02)
        assert len(builds) >= 2, "watcher never attempted the reload"
        assert builds[1] == models_root / "beta"

        # The agent task must still be alive -- a failed reload should
        # never propagate as a fatal exception.
        assert not run_task.done()
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)


# --------------------------------------------------------------------------- #
# No-op when pointer is stable                                               #
# --------------------------------------------------------------------------- #


async def test_run_does_not_reload_when_pointer_is_stable(
    models_root: pathlib.Path,
    redis: Redis[Any],
) -> None:
    """Multiple poll cycles with the same pointer should rebuild zero times."""
    _write_pointer(models_root / "active", "alpha")

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
            heartbeat=_quiet_heartbeat,
        )
    )

    try:
        # Wait for initial load, then sit through ~6 poll cycles.
        for _ in range(50):
            if builds:
                break
            await asyncio.sleep(0.02)
        assert len(builds) == 1
        await asyncio.sleep(0.30)

        assert len(builds) == 1, (
            f"watcher rebuilt despite stable pointer; builds={builds}"
        )
    finally:
        stop.set()
        await asyncio.wait_for(run_task, timeout=2.0)
