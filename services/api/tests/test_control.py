"""Tests for /kill-switch endpoints."""

from __future__ import annotations

import fakeredis.aioredis
from httpx import AsyncClient

from fincept_bus.streams import STREAM_ALERTS


async def test_kill_switch_post_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/kill-switch", json={"reason": "drill"})
    assert response.status_code == 401


async def test_kill_switch_delete_requires_auth(client: AsyncClient) -> None:
    response = await client.delete("/kill-switch")
    assert response.status_code == 401


async def test_kill_switch_post_publishes_critical_alert(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/kill-switch", json={"reason": "drill"}, headers=auth_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "alert_id" in body

    messages = await fake_redis.xrange(STREAM_ALERTS, count=10)
    assert len(messages) == 1
    _msg_id, fields = messages[0]
    payload = (
        fields[b"payload"].decode()
        if isinstance(fields.get(b"payload"), bytes)
        else str(fields.get(b"payload", fields.get("payload", "")))
    )
    assert "kill_switch_engaged" in payload
    assert "critical" in payload


async def test_kill_switch_delete_publishes_all_clear_alert(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.delete("/kill-switch", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["ok"] is True

    messages = await fake_redis.xrange(STREAM_ALERTS, count=10)
    assert len(messages) == 1
    _msg_id, fields = messages[0]
    payload = (
        fields[b"payload"].decode()
        if isinstance(fields.get(b"payload"), bytes)
        else str(fields.get(b"payload", fields.get("payload", "")))
    )
    assert "kill_switch_cleared" in payload


async def test_kill_switch_post_records_actor_from_token(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The decoded JWT's ``sub`` claim should appear in the alert tags."""
    response = await client.post(
        "/kill-switch", json={"reason": "manual"}, headers=auth_headers
    )
    assert response.status_code == 200

    messages = await fake_redis.xrange(STREAM_ALERTS, count=10)
    _msg_id, fields = messages[0]
    payload = (
        fields[b"payload"].decode()
        if isinstance(fields.get(b"payload"), bytes)
        else str(fields.get(b"payload", fields.get("payload", "")))
    )
    assert "test-user" in payload
