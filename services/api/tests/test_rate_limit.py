"""Unit tests for the Redis fixed-window rate limiter."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from api.rate_limit import RateLimitExceeded, enforce_rate_limit


@pytest.mark.asyncio
async def test_rate_limit_allows_under_budget() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    try:
        for expected_count in range(1, 4):
            state = await enforce_rate_limit(redis, "rl:test", limit=5, window_sec=10)
            assert state.count == expected_count
            assert state.remaining == 5 - expected_count
            assert state.reset_sec <= 10
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_rate_limit_raises_when_exceeded() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    try:
        await enforce_rate_limit(redis, "rl:test", limit=2, window_sec=30)
        await enforce_rate_limit(redis, "rl:test", limit=2, window_sec=30)
        with pytest.raises(RateLimitExceeded) as excinfo:
            await enforce_rate_limit(redis, "rl:test", limit=2, window_sec=30)
        exc = excinfo.value
        assert exc.limit == 2
        assert exc.window_sec == 30
        assert 1 <= exc.retry_after <= 30
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_rate_limit_repairs_missing_ttl() -> None:
    """If a key exists without a TTL, the limiter must re-stamp it.

    Regression guard: earlier versions of the helper could leave the
    key perma-present with no TTL when ``EXPIRE`` failed between the
    ``INCR`` and the expiry call, effectively locking the user out
    until a human manually deleted the key.
    """
    redis = fakeredis.aioredis.FakeRedis()
    try:
        # Pre-seed the key at the exact limit with no TTL.
        await redis.set("rl:test", 2)
        state = await enforce_rate_limit(redis, "rl:test", limit=5, window_sec=10)
        # The limiter should have INCR-ed to 3 and set a TTL close to the window.
        assert state.count == 3
        ttl = await redis.ttl("rl:test")
        assert 1 <= int(ttl) <= 10
    finally:
        await redis.aclose()
