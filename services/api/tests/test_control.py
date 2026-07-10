"""Tests for /kill-switch endpoints."""

from __future__ import annotations

import time

import fakeredis.aioredis
from fincept_bus.streams import STREAM_ALERTS
from httpx import AsyncClient


async def test_kill_switch_post_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/kill-switch", json={"reason": "drill"})
    assert response.status_code == 401


async def test_kill_switch_delete_requires_auth(client: AsyncClient) -> None:
    response = await client.delete("/kill-switch")
    assert response.status_code == 401


async def test_kill_switch_get_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/kill-switch")
    assert response.status_code == 401


async def test_kill_switch_get_returns_default_clear_state(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/kill-switch", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "engaged": False,
        "actor": None,
        "reason": None,
        "alert_id": None,
        "ts_unix": None,
    }


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


async def test_kill_switch_post_and_delete_update_read_state(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    trip = await client.post(
        "/kill-switch", json={"reason": "operator drill"}, headers=auth_headers
    )
    assert trip.status_code == 200

    engaged = await client.get("/kill-switch", headers=auth_headers)
    assert engaged.status_code == 200
    engaged_body = engaged.json()
    assert engaged_body["engaged"] is True
    assert engaged_body["actor"] == "test-user"
    assert engaged_body["reason"] == "operator drill"
    assert engaged_body["alert_id"] == trip.json()["alert_id"]
    assert isinstance(engaged_body["ts_unix"], float)

    clear = await client.delete("/kill-switch", headers=auth_headers)
    assert clear.status_code == 200

    cleared = await client.get("/kill-switch", headers=auth_headers)
    assert cleared.status_code == 200
    cleared_body = cleared.json()
    assert cleared_body["engaged"] is False
    assert cleared_body["actor"] == "test-user"
    assert cleared_body["reason"] is None
    assert cleared_body["alert_id"] == clear.json()["alert_id"]
    assert isinstance(cleared_body["ts_unix"], float)


async def test_start_feature_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/features/market_data/start")
    assert response.status_code == 401


async def test_start_feature_rejects_unknown_feature(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post("/features/unknown/start", headers=auth_headers)
    assert response.status_code == 404


async def test_start_feature_returns_already_running_when_heartbeats_fresh(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    now = str(time.time())
    await fake_redis.set("service:heartbeat:ingestor", now)
    await fake_redis.set("service:heartbeat:features", now)

    response = await client.post("/features/market_data/start", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is False
    assert body["status"] == "already_running"
    assert body["fresh_services"] == ["ingestor", "features"]


async def test_start_feature_spawns_allowlisted_script(
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"feature launch requested: jobs", b""

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    response = await client.post("/features/jobs/start", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    assert body["feature_id"] == "jobs"
    assert calls
    args, kwargs = calls[0]
    assert any("start_feature.ps1" in str(arg) for arg in args)
    assert args[-2:] == ("-FeatureId", "jobs")
    assert kwargs["stdout"] is not None
    assert kwargs["stderr"] is not None


async def test_start_feature_blocks_missing_model_before_script(
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_blocker(feature_id: str) -> dict[str, object] | None:
        if feature_id != "news_alpha_predictor":
            return None
        return {
            "reason": "missing_model",
            "message": "news_alpha_predictor model.txt not found",
            "next_step": "Train the model first.",
            "path": "models/news_alpha_predictor",
        }

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("script should not run")

    monkeypatch.setattr("api.routes.control._feature_preflight_blocker", fake_blocker)
    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    response = await client.post(
        "/features/news_alpha_predictor/start",
        headers=auth_headers,
    )

    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["status"] == "blocked"
    assert body["reason"] == "missing_model"
    assert body["next_step"] == "Train the model first."
    assert calls == []


async def test_stop_feature_spawns_stop_script(
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"feature stop requested: jobs", b""

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    response = await client.post("/features/jobs/stop", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["feature_id"] == "jobs"
    assert body["action"] == "stop"
    assert body["status"] == "stop_requested"
    args, _kwargs = calls[0]
    assert any("stop_feature.ps1" in str(arg) for arg in args)


async def test_restart_feature_records_last_control_log(
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    class FakeProcess:
        def __init__(self, output: bytes) -> None:
            self.output = output
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return self.output, b""

    async def fake_exec(*args, **_kwargs):
        script = next(str(arg) for arg in args if str(arg).endswith(".ps1"))
        if "stop_feature.ps1" in script:
            return FakeProcess(b"feature stop requested: jobs")
        return FakeProcess(b"feature launch requested: jobs")

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    response = await client.post("/features/jobs/restart", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "restart_requested"

    logs = await client.get("/features/jobs/logs", headers=auth_headers)
    assert logs.status_code == 200
    last_control = logs.json()["last_control"]
    assert last_control["action"] == "restart"
    assert "feature stop requested: jobs" in last_control["output"]
    assert "feature launch requested: jobs" in last_control["output"]
