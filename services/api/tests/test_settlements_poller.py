"""Tests for api.settlements_poller — the production poller that drives
settlement via the canonical Path B ledger.

Tests the poller function in isolation (no full app / lifespan fixture):
  * the poller calls the adapter with the configured paths + market source,
  * a settlement failure is logged and swallowed (loop continues),
  * the interval is read from ``SETTLEMENTS_WORKER_POLL_S``,
  * the legacy Path A path is exercised via ``SETTLEMENTS_USE_PATH_B=0``.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest


def test_settlements_worker_interval_seconds_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SETTLEMENTS_WORKER_POLL_S", raising=False)
    from api.settlements_poller import _settlements_worker_interval_seconds

    assert _settlements_worker_interval_seconds() == 60.0


def test_settlements_worker_interval_seconds_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SETTLEMENTS_WORKER_POLL_S", "12.5")
    from api.settlements_poller import _settlements_worker_interval_seconds

    assert _settlements_worker_interval_seconds() == 12.5


def test_settlements_worker_interval_seconds_zero_disables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SETTLEMENTS_WORKER_POLL_S", "0")
    from api.settlements_poller import _settlements_worker_interval_seconds

    assert _settlements_worker_interval_seconds() == 0.0


def test_settlements_worker_interval_seconds_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SETTLEMENTS_WORKER_POLL_S", "not-a-number")
    from api.settlements_poller import _settlements_worker_interval_seconds

    assert _settlements_worker_interval_seconds() == 60.0


def test_settlements_worker_interval_seconds_negative_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SETTLEMENTS_WORKER_POLL_S", "-5")
    from api.settlements_poller import _settlements_worker_interval_seconds

    assert _settlements_worker_interval_seconds() == 0.0


async def test_poller_calls_tick_with_correct_paths_and_market_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """The poller invokes tick with the env-configured dirs + market source.

    This tests the legacy Path A rollback path (SETTLEMENTS_USE_PATH_B=0).
    """
    predictions_dir = tmp_path / "predictions"
    settlements_dir = tmp_path / "settlements"
    predictions_dir.mkdir()
    settlements_dir.mkdir()
    monkeypatch.setenv("PREDICTIONS_DIR", str(predictions_dir))
    monkeypatch.setenv("SETTLEMENTS_DIR", str(settlements_dir))
    monkeypatch.setenv("SETTLEMENTS_USE_PATH_B", "0")

    captured: dict[str, object] = {}

    async def fake_tick(
        now_ns, *, predictions_dir, settlements_dir, market_data_source
    ):
        captured["now_ns"] = now_ns
        captured["predictions_dir"] = predictions_dir
        captured["settlements_dir"] = settlements_dir
        captured["market_data_source"] = market_data_source
        return []

    async def fake_market_source(symbol, ts1, ts2):
        return None

    def fake_build_market_data_source():
        return fake_market_source

    monkeypatch.setattr("settlements.worker.tick", fake_tick)
    monkeypatch.setattr(
        "api.settlements_poller._build_market_data_source",
        fake_build_market_data_source,
    )

    from api.settlements_poller import _poll_settlements_worker

    # Run the poller briefly, then cancel to stop the infinite loop.
    task = asyncio.create_task(_poll_settlements_worker(0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert captured["predictions_dir"] == predictions_dir
    assert captured["settlements_dir"] == settlements_dir
    assert captured["market_data_source"] is fake_market_source
    assert isinstance(captured["now_ns"], int)


async def test_poller_swallows_tick_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A tick exception is logged and swallowed; the loop keeps running.

    This tests the legacy Path A rollback path (SETTLEMENTS_USE_PATH_B=0).
    """
    monkeypatch.setenv("PREDICTIONS_DIR", str(tmp_path / "preds"))
    monkeypatch.setenv("SETTLEMENTS_DIR", str(tmp_path / "settle"))
    monkeypatch.setenv("SETTLEMENTS_USE_PATH_B", "0")

    call_count = 0

    async def failing_tick(
        now_ns, *, predictions_dir, settlements_dir, market_data_source
    ):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("boom")

    async def fake_market_source(symbol, ts1, ts2):
        return None

    def fake_build_market_data_source():
        return fake_market_source

    monkeypatch.setattr("settlements.worker.tick", failing_tick)
    monkeypatch.setattr(
        "api.settlements_poller._build_market_data_source",
        fake_build_market_data_source,
    )

    from api.settlements_poller import _poll_settlements_worker

    task = asyncio.create_task(_poll_settlements_worker(0.01))
    # Let it iterate a few times — it must NOT raise.
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2, "poller should have continued after the first failure"


async def test_poller_logs_settled_count_when_records_returned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """When tick returns records, the poller logs the count.

    This tests the legacy Path A rollback path (SETTLEMENTS_USE_PATH_B=0).
    """
    monkeypatch.setenv("PREDICTIONS_DIR", str(tmp_path / "preds"))
    monkeypatch.setenv("SETTLEMENTS_DIR", str(tmp_path / "settle"))
    monkeypatch.setenv("SETTLEMENTS_USE_PATH_B", "0")

    async def fake_tick(
        now_ns, *, predictions_dir, settlements_dir, market_data_source
    ):
        return ["record-1", "record-2"]

    async def fake_market_source(symbol, ts1, ts2):
        return None

    def fake_build_market_data_source():
        return fake_market_source

    monkeypatch.setattr("settlements.worker.tick", fake_tick)
    monkeypatch.setattr(
        "api.settlements_poller._build_market_data_source",
        fake_build_market_data_source,
    )

    info_calls: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def info(self, event, **kw):
            info_calls.append((event, kw))

        def warning(self, event, **kw):
            pass

    monkeypatch.setattr("api.settlements_poller.log", _FakeLog())

    from api.settlements_poller import _poll_settlements_worker

    task = asyncio.create_task(_poll_settlements_worker(0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    tick_events = [kw for ev, kw in info_calls if ev == "settlements.worker.tick"]
    assert tick_events, "poller should have logged settlements.worker.tick"
    assert any(kw.get("settled") == 2 for kw in tick_events)


async def test_poller_logs_warning_on_tick_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A tick failure is logged as a warning (best-effort, never raises).

    This tests the legacy Path A rollback path (SETTLEMENTS_USE_PATH_B=0).
    """
    monkeypatch.setenv("PREDICTIONS_DIR", str(tmp_path / "preds"))
    monkeypatch.setenv("SETTLEMENTS_DIR", str(tmp_path / "settle"))
    monkeypatch.setenv("SETTLEMENTS_USE_PATH_B", "0")

    async def failing_tick(
        now_ns, *, predictions_dir, settlements_dir, market_data_source
    ):
        raise RuntimeError("boom")

    async def fake_market_source(symbol, ts1, ts2):
        return None

    def fake_build_market_data_source():
        return fake_market_source

    monkeypatch.setattr("settlements.worker.tick", failing_tick)
    monkeypatch.setattr(
        "api.settlements_poller._build_market_data_source",
        fake_build_market_data_source,
    )

    warning_calls: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def info(self, event, **kw):
            pass

        def warning(self, event, **kw):
            warning_calls.append((event, kw))

    monkeypatch.setattr("api.settlements_poller.log", _FakeLog())

    from api.settlements_poller import _poll_settlements_worker

    task = asyncio.create_task(_poll_settlements_worker(0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    fail_events = [
        kw for ev, kw in warning_calls if ev == "settlements.worker_poll_failed"
    ]
    assert fail_events, "poller should have logged settlements.worker_poll_failed"
    assert any("RuntimeError" in str(kw.get("error", "")) for kw in fail_events)


# --------------------------------------------------------------------------- #
# Path B (canonical) poller tests                                             #
# --------------------------------------------------------------------------- #


async def test_poller_uses_path_b_adapter_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """When SETTLEMENTS_USE_PATH_B is unset (defaults to 1), the poller
    uses the PathACompatAdapter, not the legacy tick."""
    monkeypatch.setenv("PREDICTIONS_DIR", str(tmp_path / "preds"))
    monkeypatch.setenv("SETTLEMENTS_DIR", str(tmp_path / "settle"))
    monkeypatch.delenv("SETTLEMENTS_USE_PATH_B", raising=False)

    adapter_called = False

    class FakeAdapter:
        async def settle_due_predictions_async(
            self, predictions_dir, now_ns, market_data_source
        ):
            nonlocal adapter_called
            adapter_called = True
            return []

    async def fake_market_source(symbol, ts1, ts2):
        return None

    def fake_build_market_data_source():
        return fake_market_source

    def fake_build_compat_adapter():
        return FakeAdapter()

    monkeypatch.setattr(
        "api.settlements_poller._build_market_data_source",
        fake_build_market_data_source,
    )
    monkeypatch.setattr(
        "api.settlements_poller._build_compat_adapter",
        fake_build_compat_adapter,
    )

    from api.settlements_poller import _poll_settlements_worker

    task = asyncio.create_task(_poll_settlements_worker(0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert adapter_called, "poller should have called the Path B adapter"


async def test_poller_path_b_swallows_failure_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A Path B adapter failure is logged and swallowed; the loop keeps running."""
    monkeypatch.setenv("PREDICTIONS_DIR", str(tmp_path / "preds"))
    monkeypatch.setenv("SETTLEMENTS_DIR", str(tmp_path / "settle"))
    monkeypatch.setenv("SETTLEMENTS_USE_PATH_B", "1")

    call_count = 0

    class FailingAdapter:
        async def settle_due_predictions_async(
            self, predictions_dir, now_ns, market_data_source
        ):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("adapter boom")

    async def fake_market_source(symbol, ts1, ts2):
        return None

    def fake_build_market_data_source():
        return fake_market_source

    def fake_build_compat_adapter():
        return FailingAdapter()

    monkeypatch.setattr(
        "api.settlements_poller._build_market_data_source",
        fake_build_market_data_source,
    )
    monkeypatch.setattr(
        "api.settlements_poller._build_compat_adapter",
        fake_build_compat_adapter,
    )

    from api.settlements_poller import _poll_settlements_worker

    task = asyncio.create_task(_poll_settlements_worker(0.01))
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2, "poller should have continued after adapter failure"
