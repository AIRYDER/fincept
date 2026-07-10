"""Tests for risk.state.KillSwitchState."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from fincept_core.schemas import AlertEvent

from risk.state import KILL_SWITCH_STATE_KEY, KillSwitchState


def _alert(*, code: str, severity: str = "critical") -> AlertEvent:
    return AlertEvent(
        alert_id="a1",
        ts_event=1_000,
        severity=severity,
        source="api.control",
        code=code,
        message=f"test {code}",
    )


# ---------------------------------------------------------------------------
# In-memory mode (no Redis) — backward compatible
# ---------------------------------------------------------------------------


def test_kill_switch_starts_disengaged() -> None:
    state = KillSwitchState()
    assert state.engaged is False


def test_engaged_alert_flips_flag() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_engaged"))
    assert state.engaged is True


def test_cleared_alert_after_engaged_resets_flag() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_engaged"))
    assert state.engaged is True
    state.apply(_alert(code="kill_switch_cleared", severity="info"))
    assert state.engaged is False


def test_unknown_code_does_not_change_flag() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="some_other_alert"))
    assert state.engaged is False

    state.apply(_alert(code="kill_switch_engaged"))
    state.apply(_alert(code="another_unrelated_alert"))
    assert state.engaged is True


def test_repeated_engaged_alerts_remain_engaged() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_engaged"))
    state.apply(_alert(code="kill_switch_engaged"))
    assert state.engaged is True


def test_cleared_alert_when_already_disengaged_is_idempotent() -> None:
    state = KillSwitchState()
    state.apply(_alert(code="kill_switch_cleared", severity="info"))
    assert state.engaged is False


# ---------------------------------------------------------------------------
# Redis persistence mode
# ---------------------------------------------------------------------------


def _make_fake_async_redis(*, stored_state: dict | None = None) -> MagicMock:
    """Create a fake async Redis client with a connection pool that
    can be used by KillSwitchState to extract the URL for sync client creation.
    """
    fake = MagicMock()
    fake.connection_pool = MagicMock()
    fake.connection_pool.connection_kwargs = {
        "host": "localhost",
        "port": 6379,
        "db": 0,
    }
    return fake


def _make_fake_sync_redis(*, stored_state: dict | None = None) -> MagicMock:
    """Create a fake sync Redis client that returns the given stored state."""
    fake = MagicMock()
    if stored_state is not None:
        fake.get.return_value = json.dumps(stored_state).encode("utf-8")
    else:
        fake.get.return_value = None
    fake.set = MagicMock()
    fake.close = MagicMock()
    return fake


def test_redis_mode_restores_engaged_state() -> None:
    """When Redis has engaged=True, KillSwitchState starts engaged."""
    stored = {"engaged": True, "actor": "operator", "reason": "test"}
    fake_sync = _make_fake_sync_redis(stored_state=stored)
    fake_async = _make_fake_async_redis()

    with patch("redis.Redis.from_url", return_value=fake_sync):
        state = KillSwitchState(redis=fake_async)

    assert state.engaged is True
    fake_sync.get.assert_called_once_with(KILL_SWITCH_STATE_KEY)


def test_redis_mode_restores_disengaged_state() -> None:
    """When Redis has engaged=False, KillSwitchState starts disengaged."""
    stored = {"engaged": False, "actor": "operator", "reason": "cleared"}
    fake_sync = _make_fake_sync_redis(stored_state=stored)
    fake_async = _make_fake_async_redis()

    with patch("redis.Redis.from_url", return_value=fake_sync):
        state = KillSwitchState(redis=fake_async)

    assert state.engaged is False


def test_redis_mode_no_key_starts_disengaged() -> None:
    """When Redis key doesn't exist, KillSwitchState starts disengaged."""
    fake_sync = _make_fake_sync_redis(stored_state=None)
    fake_async = _make_fake_async_redis()

    with patch("redis.Redis.from_url", return_value=fake_sync):
        state = KillSwitchState(redis=fake_async)

    assert state.engaged is False


def test_redis_mode_redis_failure_defaults_to_engaged() -> None:
    """When Redis read fails, KillSwitchState defaults to engaged=True (fail-closed)."""
    fake_async = _make_fake_async_redis()

    with patch("redis.Redis.from_url", side_effect=Exception("connection refused")):
        state = KillSwitchState(redis=fake_async)

    assert state.engaged is True  # fail-closed


def test_redis_mode_persists_engaged_on_apply() -> None:
    """When apply() engages the kill-switch, the state is written to Redis."""
    fake_sync = _make_fake_sync_redis(stored_state=None)
    fake_async = _make_fake_async_redis()

    with patch("redis.Redis.from_url", return_value=fake_sync):
        state = KillSwitchState(redis=fake_async)
        assert state.engaged is False

        state.apply(_alert(code="kill_switch_engaged"))

    assert state.engaged is True
    # Verify Redis SET was called with engaged=True.
    fake_sync.set.assert_called_once()
    args = fake_sync.set.call_args
    assert args.args[0] == KILL_SWITCH_STATE_KEY
    payload = json.loads(args.args[1])
    assert payload["engaged"] is True


def test_redis_mode_persists_cleared_on_apply() -> None:
    """When apply() clears the kill-switch, the state is written to Redis."""
    stored = {"engaged": True, "actor": "operator", "reason": "test"}
    fake_sync = _make_fake_sync_redis(stored_state=stored)
    fake_async = _make_fake_async_redis()

    with patch("redis.Redis.from_url", return_value=fake_sync):
        state = KillSwitchState(redis=fake_async)
        assert state.engaged is True

        state.apply(_alert(code="kill_switch_cleared", severity="info"))

    assert state.engaged is False
    # Verify Redis SET was called with engaged=False.
    fake_sync.set.assert_called_once()
    args = fake_sync.set.call_args
    assert args.args[0] == KILL_SWITCH_STATE_KEY
    payload = json.loads(args.args[1])
    assert payload["engaged"] is False


def test_redis_mode_write_failure_doesnt_crash() -> None:
    """If Redis write fails on apply(), the in-memory state is still correct."""
    fake_sync = _make_fake_sync_redis(stored_state=None)
    fake_sync.set.side_effect = Exception("write failed")
    fake_async = _make_fake_async_redis()

    with patch("redis.Redis.from_url", return_value=fake_sync):
        state = KillSwitchState(redis=fake_async)
        state.apply(_alert(code="kill_switch_engaged"))

    # In-memory state should still be engaged despite Redis write failure.
    assert state.engaged is True


def test_state_survives_restart_simulation() -> None:
    """Simulate: engage → restart → state should still be engaged."""
    # Simulate a Redis backend with a shared store.
    redis_store: dict[str, str] = {}

    fake_sync_1 = _make_fake_sync_redis(stored_state=None)
    fake_sync_1.set.side_effect = lambda key, val: redis_store.__setitem__(key, val)

    fake_async = _make_fake_async_redis()

    # First instance: engage the kill-switch.
    with patch("redis.Redis.from_url", return_value=fake_sync_1):
        state1 = KillSwitchState(redis=fake_async)
        assert state1.engaged is False
        state1.apply(_alert(code="kill_switch_engaged"))
        assert state1.engaged is True

    # Verify the state was persisted.
    assert KILL_SWITCH_STATE_KEY in redis_store
    assert json.loads(redis_store[KILL_SWITCH_STATE_KEY])["engaged"] is True

    # Second instance: simulate restart by reading from Redis.
    stored = json.loads(redis_store[KILL_SWITCH_STATE_KEY])
    fake_sync_2 = _make_fake_sync_redis(stored_state=stored)

    with patch("redis.Redis.from_url", return_value=fake_sync_2):
        state2 = KillSwitchState(redis=fake_async)

    # State should be restored to engaged=True.
    assert state2.engaged is True
