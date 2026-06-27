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


def _make_redis() -> Any:
    return fakeredis.aioredis.FakeRedis()


async def test_beat_periodically_writes_then_deletes_on_cancel() -> None:
    """beat_periodically writes a heartbeat, then deletes it on cancel."""
    redis: Any = _make_redis()
    try:
        # Start the heartbeat task.
        task = asyncio.create_task(beat_periodically(redis, "test-svc", interval_sec=1, ttl_sec=5))
        # Poll for the key to appear (up to 2 seconds).
        key = f"{HEARTBEAT_PREFIX}test-svc"
        found = False
        for _ in range(20):
            await asyncio.sleep(0.1)
            val = await redis.get(key)
            if val is not None:
                found = True
                break

        assert found, "heartbeat key was never written"

        # Cancel and verify cleanup.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        val = await redis.get(key)
        assert val is None, "heartbeat key should be deleted on cancel"
    finally:
        await redis.aclose()


async def test_beat_periodically_with_stats_writes_json() -> None:
    """With stats_callback, heartbeat value is JSON with ts + stats."""
    redis: Any = _make_redis()
    try:
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
        # Poll for the key.
        key = f"{HEARTBEAT_PREFIX}test-svc"
        found = False
        for _ in range(20):
            await asyncio.sleep(0.1)
            val = await redis.get(key)
            if val is not None:
                found = True
                break

        assert found, "heartbeat key was never written"

        val_str = val.decode() if isinstance(val, bytes) else val
        parsed = json.loads(val_str)
        assert "ts" in parsed
        assert "stats" in parsed
        assert parsed["stats"] == stats_data

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await redis.aclose()


async def test_stats_callback_exception_does_not_break_heartbeat() -> None:
    """If stats_callback raises, heartbeat should still write (plain ts)."""
    redis: Any = _make_redis()
    try:

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
        # Poll for the key.
        key = f"{HEARTBEAT_PREFIX}test-svc"
        found = False
        for _ in range(20):
            await asyncio.sleep(0.1)
            val = await redis.get(key)
            if val is not None:
                found = True
                break

        assert found, "heartbeat key was never written"

        val_str = val.decode() if isinstance(val, bytes) else val
        # Should fall back to plain timestamp string (not JSON).
        float(val_str)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await redis.aclose()


async def test_read_all_parses_plain_timestamp() -> None:
    """read_all should parse plain timestamp values."""
    redis: Any = _make_redis()
    try:
        await redis.set(f"{HEARTBEAT_PREFIX}svc-a", "1234567890.123", ex=30)
        result = await read_all(redis)
        assert "svc-a" in result
        assert result["svc-a"] == 1234567890.123
    finally:
        await redis.aclose()


async def test_read_all_parses_json_with_stats() -> None:
    """read_all should parse JSON heartbeat values (new format)."""
    redis: Any = _make_redis()
    try:
        value = json.dumps({"ts": 1234567890.456, "stats": {"foo": "bar"}})
        await redis.set(f"{HEARTBEAT_PREFIX}svc-b", value, ex=30)
        result = await read_all(redis)
        assert "svc-b" in result
        assert result["svc-b"] == 1234567890.456
    finally:
        await redis.aclose()


async def test_read_all_with_stats_returns_stats() -> None:
    """read_all_with_stats should return both ts and stats."""
    redis: Any = _make_redis()
    try:
        value = json.dumps({"ts": 1234567890.789, "stats": {"buffer": 10}})
        await redis.set(f"{HEARTBEAT_PREFIX}svc-c", value, ex=30)
        await redis.set(f"{HEARTBEAT_PREFIX}svc-d", "1234567890.000", ex=30)

        result = await read_all_with_stats(redis)
        assert result["svc-c"]["ts"] == 1234567890.789
        assert result["svc-c"]["stats"] == {"buffer": 10}
        assert result["svc-d"]["ts"] == 1234567890.0
        assert result["svc-d"]["stats"] is None
    finally:
        await redis.aclose()


async def test_read_all_skips_expired_keys() -> None:
    """Expired keys should not appear in results."""
    redis: Any = _make_redis()
    try:
        result = await read_all(redis)
        assert result == {}
    finally:
        await redis.aclose()


async def test_read_all_skips_corrupt_values() -> None:
    """Corrupt heartbeat values should be skipped, not crash."""
    redis: Any = _make_redis()
    try:
        await redis.set(f"{HEARTBEAT_PREFIX}corrupt", "not-a-number", ex=30)
        result = await read_all(redis)
        assert "corrupt" not in result
    finally:
        await redis.aclose()


async def test_ttl_must_exceed_interval() -> None:
    """ttl_sec must be greater than interval_sec."""
    redis: Any = _make_redis()
    try:
        with pytest.raises(ValueError, match="ttl_sec"):
            await beat_periodically(redis, "x", interval_sec=10, ttl_sec=5)
    finally:
        await redis.aclose()
