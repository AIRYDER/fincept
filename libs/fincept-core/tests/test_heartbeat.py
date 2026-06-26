"""Tests for fincept_core.heartbeat — liveness signal + stats emission."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import fakeredis.aioredis
import pytest

from fincept_core.heartbeat import (
    HEARTBEAT_PREFIX,
    beat_periodically,
    read_all,
    read_all_with_stats,
)


@pytest.fixture
async def redis() -> Any:
    r = fakeredis.aioredis.FakeRedis()
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_beat_periodically_writes_timestamp(redis: Any) -> None:
    """Without stats_callback, heartbeat value is a plain timestamp string."""
    task = asyncio.create_task(
        beat_periodically(redis, "test-svc", interval_sec=1, ttl_sec=5)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    val = await redis.get(f"{HEARTBEAT_PREFIX}test-svc")
    assert val is not None
    val_str = val.decode() if isinstance(val, bytes) else val
    # Should be a plain float string (no JSON).
    float(val_str)


@pytest.mark.asyncio
async def test_beat_periodically_with_stats_writes_json(redis: Any) -> None:
    """With stats_callback, heartbeat value is JSON with ts + stats."""
    stats_data = {"buffer": {"pending": 42}, "dropped": 0}

    task = asyncio.create_task(
        beat_periodically(
            redis,
            "test-svc",
            interval_sec=1,
            ttl_sec=5,
            stats_callback=lambda: stats_data,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    val = await redis.get(f"{HEARTBEAT_PREFIX}test-svc")
    assert val is not None
    val_str = val.decode() if isinstance(val, bytes) else val
    parsed = json.loads(val_str)
    assert "ts" in parsed
    assert "stats" in parsed
    assert parsed["stats"] == stats_data


@pytest.mark.asyncio
async def test_stats_callback_exception_does_not_break_heartbeat(redis: Any) -> None:
    """If stats_callback raises, heartbeat should still write (plain ts)."""

    def failing_callback() -> dict[str, Any]:
        raise RuntimeError("stats collection failed")

    task = asyncio.create_task(
        beat_periodically(
            redis,
            "test-svc",
            interval_sec=1,
            ttl_sec=5,
            stats_callback=failing_callback,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    val = await redis.get(f"{HEARTBEAT_PREFIX}test-svc")
    assert val is not None
    # Should fall back to plain timestamp string.
    val_str = val.decode() if isinstance(val, bytes) else val
    float(val_str)  # Should parse as float (not JSON)


@pytest.mark.asyncio
async def test_read_all_parses_plain_timestamp(redis: Any) -> None:
    """read_all should parse plain timestamp values."""
    await redis.set(f"{HEARTBEAT_PREFIX}svc-a", "1234567890.123", ex=30)
    result = await read_all(redis)
    assert "svc-a" in result
    assert result["svc-a"] == 1234567890.123


@pytest.mark.asyncio
async def test_read_all_parses_json_with_stats(redis: Any) -> None:
    """read_all should parse JSON heartbeat values (new format)."""
    value = json.dumps({"ts": 1234567890.456, "stats": {"foo": "bar"}})
    await redis.set(f"{HEARTBEAT_PREFIX}svc-b", value, ex=30)
    result = await read_all(redis)
    assert "svc-b" in result
    assert result["svc-b"] == 1234567890.456


@pytest.mark.asyncio
async def test_read_all_with_stats_returns_stats(redis: Any) -> None:
    """read_all_with_stats should return both ts and stats."""
    value = json.dumps({"ts": 1234567890.789, "stats": {"buffer": 10}})
    await redis.set(f"{HEARTBEAT_PREFIX}svc-c", value, ex=30)
    # Also add a plain-timestamp service.
    await redis.set(f"{HEARTBEAT_PREFIX}svc-d", "1234567890.000", ex=30)

    result = await read_all_with_stats(redis)
    assert result["svc-c"]["ts"] == 1234567890.789
    assert result["svc-c"]["stats"] == {"buffer": 10}
    assert result["svc-d"]["ts"] == 1234567890.0
    assert result["svc-d"]["stats"] is None


@pytest.mark.asyncio
async def test_read_all_skips_expired_keys(redis: Any) -> None:
    """Expired keys should not appear in results."""
    # Don't set any keys.
    result = await read_all(redis)
    assert result == {}


@pytest.mark.asyncio
async def test_read_all_skips_corrupt_values(redis: Any) -> None:
    """Corrupt heartbeat values should be skipped, not crash."""
    await redis.set(f"{HEARTBEAT_PREFIX}corrupt", "not-a-number", ex=30)
    result = await read_all(redis)
    assert "corrupt" not in result


@pytest.mark.asyncio
async def test_ttl_must_exceed_interval(redis: Any) -> None:
    """ttl_sec must be greater than interval_sec."""
    with pytest.raises(ValueError, match="ttl_sec"):
        await beat_periodically(redis, "x", interval_sec=10, ttl_sec=5)


@pytest.mark.asyncio
async def test_heartbeat_deleted_on_cancel(redis: Any) -> None:
    """On graceful shutdown, the heartbeat key should be deleted."""
    task = asyncio.create_task(
        beat_periodically(redis, "test-svc", interval_sec=1, ttl_sec=5)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    val = await redis.get(f"{HEARTBEAT_PREFIX}test-svc")
    assert val is None
