"""
TDD tests for api.routes.modules (TASK-0203: On-Demand Module Control).

Acceptance criteria from AAAAAAAAAA_BIG_PLAN.md / NEXT_STEPS_PLAN.md:
- Operator can start and stop an allowlisted module.
- A disabled module does not cost resources.
- Duplicate starts do not spawn unbounded processes.
- Idle timeout stops optional modules safely.
- The core dashboard remains usable when modules are stopped.

Security requirements (non-negotiable):
- No arbitrary shell command execution from user input.
- Module IDs must be allowlisted.
- API must require auth.
- Start commands must be predeclared server-side.
- Secrets must never be echoed into dashboard logs.

These tests are file-disjoint from TASK-0304 (Builder 2: quant_foundry
outbox/inbox) and TASK-0401 (Builder 1: settlement ledger). They do NOT
touch services/quant_foundry/**.
"""

from __future__ import annotations

import json
import time

import fakeredis.aioredis
from httpx import AsyncClient


# --------------------------------------------------------------------------- #
# Registry / allowlist                                                         #
# --------------------------------------------------------------------------- #


async def test_list_modules_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/modules")
    assert response.status_code == 401


async def test_list_modules_returns_allowlisted_registry(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """GET /modules returns the predeclared module registry with metadata."""
    response = await client.get("/modules", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    modules = body["modules"]
    assert isinstance(modules, list)
    assert len(modules) > 0
    # Every module carries the registry metadata fields.
    for m in modules:
        for field in (
            "module_id",
            "display_name",
            "description",
            "cost_class",
            "idle_timeout_sec",
            "allowed_environments",
            "status",
        ):
            assert field in m, f"missing field {field} in module {m.get('module_id')}"
    # A known optional module is present (OpenBB is a canonical optional module).
    ids = {m["module_id"] for m in modules}
    assert "openbb" in ids


async def test_get_module_detail_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/modules/openbb")
    assert response.status_code == 401


async def test_get_unknown_module_returns_404(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Unknown module IDs are rejected — no arbitrary module execution."""
    response = await client.get("/modules/does_not_exist", headers=auth_headers)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Start / stop / restart — auth + allowlist + local-only                       #
# --------------------------------------------------------------------------- #


async def test_start_module_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/modules/openbb/start")
    assert response.status_code == 401


async def test_start_unknown_module_rejected(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """An unknown module ID must NOT trigger any subprocess — allowlist only."""
    response = await client.post("/modules/evil_module/start", headers=auth_headers)
    assert response.status_code == 404


async def test_stop_unknown_module_rejected(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post("/modules/evil_module/stop", headers=auth_headers)
    assert response.status_code == 404


async def test_restart_unknown_module_rejected(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post("/modules/evil_module/restart", headers=auth_headers)
    assert response.status_code == 404


async def test_start_module_spawns_allowlisted_script(
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Start delegates to the predeclared start_feature.ps1 script keyed by
    the allowlisted module ID — no user-supplied command string is ever passed
    to the shell."""
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"feature launch requested: openbb", b""

    async def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    # The modules route reuses control.py's allowlisted script runner, so
    # patching the subprocess exec in control.py is sufficient.
    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    response = await client.post("/modules/openbb/start", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["module_id"] == "openbb"
    assert body["action"] == "start"
    assert body["status"] == "launch_requested"
    # The allowlisted start_feature.ps1 was invoked with -FeatureId openbb
    assert calls, "expected the allowlisted start script to be invoked"
    args, _kwargs = calls[0]
    assert any("start_feature.ps1" in str(arg) for arg in args)
    assert args[-2:] == ("-FeatureId", "openbb")


async def test_start_module_records_receipt(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Every start/stop records a receipt in the receipts catalog."""

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"feature launch requested: jobs", b""

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    response = await client.post("/modules/jobs/start", headers=auth_headers)
    assert response.status_code == 200

    receipts = await fake_redis.lrange("module:receipts", 0, -1)
    assert len(receipts) >= 1
    raw = receipts[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    rec = json.loads(raw)
    assert rec["module_id"] == "jobs"
    assert rec["action"] == "start"
    assert rec["status"] == "launch_requested"
    assert rec["actor"] == "test-user"
    assert isinstance(rec["ts_unix"], (int, float))


async def test_stop_module_records_receipt(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"stop requested: openbb", b""

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    response = await client.post("/modules/openbb/stop", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stop_requested"

    receipts = await fake_redis.lrange("module:receipts", 0, -1)
    assert len(receipts) >= 1
    raw = receipts[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    rec = json.loads(raw)
    assert rec["module_id"] == "openbb"
    assert rec["action"] == "stop"


# --------------------------------------------------------------------------- #
# Duplicate starts do not spawn unbounded processes                            #
# --------------------------------------------------------------------------- #


async def test_duplicate_start_when_already_running_does_not_respawn(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """If the module's services are already heartbeating fresh, a second
    start must NOT spawn another process — it returns already_running."""
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

    # Seed fresh heartbeats for the jobs service so the module looks running.
    now = str(time.time())
    await fake_redis.set("service:heartbeat:jobs", now)

    response = await client.post("/modules/jobs/start", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is False
    assert body["status"] == "already_running"
    # No subprocess spawned because services are already fresh.
    assert calls == []


# --------------------------------------------------------------------------- #
# Stop all optional modules                                                    #
# --------------------------------------------------------------------------- #


async def test_stop_all_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/modules/stop-all")
    assert response.status_code == 401


async def test_stop_all_stops_running_modules(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /modules/stop-all stops every module currently marked running."""
    stopped: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"stop requested", b""

    async def fake_exec(*args, **kwargs):
        stopped.append(args[-1])  # the -FeatureId value
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    # Mark two modules as running via the module state keys.
    now = time.time()
    for mid in ("openbb", "jobs"):
        await fake_redis.set(
            f"module:state:{mid}",
            json.dumps(
                {
                    "status": "running",
                    "started_at_unix": now,
                    "last_activity_unix": now,
                    "actor": "test-user",
                }
            ),
        )

    response = await client.post("/modules/stop-all", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    stopped_ids = {s for s in stopped}
    assert "openbb" in stopped_ids
    assert "jobs" in stopped_ids


async def test_stop_all_does_not_spawn_for_already_stopped(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """stop-all must NOT spawn stop scripts for modules that are not running."""
    spawned: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"stop requested", b""

    async def fake_exec(*args, **kwargs):
        spawned.append(args[-1])
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    # No module state set → all modules are stopped/unknown.
    response = await client.post("/modules/stop-all", headers=auth_headers)
    assert response.status_code == 200
    assert spawned == []


# --------------------------------------------------------------------------- #
# Idle timeout stops optional modules safely                                   #
# --------------------------------------------------------------------------- #


async def test_sweep_idle_stops_modules_past_idle_timeout(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """POST /modules/sweep-idle stops any running module whose idle timeout
    has elapsed (no activity for idle_timeout_sec)."""
    stopped: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"stop requested", b""

    async def fake_exec(*args, **kwargs):
        stopped.append(args[-1])
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    # Find the openbb module's idle_timeout_sec from the registry.
    listing = await client.get("/modules", headers=auth_headers)
    openbb = next(m for m in listing.json()["modules"] if m["module_id"] == "openbb")
    idle_timeout = openbb["idle_timeout_sec"]
    assert idle_timeout > 0

    # Mark openbb as running but idle well past the timeout.
    now = time.time()
    await fake_redis.set(
        "module:state:openbb",
        json.dumps(
            {
                "status": "running",
                "started_at_unix": now - (idle_timeout + 60),
                "last_activity_unix": now - (idle_timeout + 60),
                "actor": "test-user",
            }
        ),
    )

    response = await client.post("/modules/sweep-idle", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "openbb" in body["stopped"]
    assert "openbb" in stopped

    # A receipt was recorded for the auto-stop.
    receipts = await fake_redis.lrange("module:receipts", 0, -1)
    raw = receipts[0]
    if isinstance(raw, bytes):
        raw = raw.decode()
    rec = json.loads(raw)
    assert rec["module_id"] == "openbb"
    assert rec["action"] == "auto_stop"


async def test_sweep_idle_does_not_stop_recently_active_modules(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """A module with recent activity is NOT stopped by the idle sweep."""
    spawned: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"stop requested", b""

    async def fake_exec(*args, **kwargs):
        spawned.append(args[-1])
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    now = time.time()
    await fake_redis.set(
        "module:state:openbb",
        json.dumps(
            {
                "status": "running",
                "started_at_unix": now,
                "last_activity_unix": now,  # just now → not idle
                "actor": "test-user",
            }
        ),
    )

    response = await client.post("/modules/sweep-idle", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["stopped"] == []
    assert spawned == []


async def test_get_modules_reports_idle_countdown(
    fake_redis: fakeredis.aioredis.FakeRedis,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """GET /modules reports idle_seconds and idle_countdown_sec for running
    modules so the dashboard can show the countdown."""
    now = time.time()
    await fake_redis.set(
        "module:state:openbb",
        json.dumps(
            {
                "status": "running",
                "started_at_unix": now - 30,
                "last_activity_unix": now - 30,
                "actor": "test-user",
            }
        ),
    )

    response = await client.get("/modules", headers=auth_headers)
    assert response.status_code == 200
    openbb = next(m for m in response.json()["modules"] if m["module_id"] == "openbb")
    assert openbb["status"] == "running"
    assert openbb["idle_seconds"] >= 25  # ~30s elapsed
    assert openbb["idle_countdown_sec"] >= 0


# --------------------------------------------------------------------------- #
# Receipts catalog                                                             #
# --------------------------------------------------------------------------- #


async def test_receipts_endpoint_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/modules/receipts")
    assert response.status_code == 401


async def test_receipts_endpoint_returns_recorded_receipts(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"feature launch requested: jobs", b""

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    await client.post("/modules/jobs/start", headers=auth_headers)

    response = await client.get("/modules/receipts", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert isinstance(body["receipts"], list)
    assert len(body["receipts"]) >= 1
    assert body["receipts"][0]["module_id"] == "jobs"


async def test_receipts_do_not_echo_secrets(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """Even if a script output accidentally contained a secret-looking token,
    the receipt must not surface it verbatim in the receipts catalog response."""
    secret = "sk-live-supersecrettoken12345"

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return f"feature launch requested: jobs token={secret}".encode(), b""

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        "api.routes.control.asyncio.create_subprocess_exec",
        fake_exec,
    )

    await client.post("/modules/jobs/start", headers=auth_headers)

    response = await client.get("/modules/receipts", headers=auth_headers)
    assert response.status_code == 200
    text = response.text
    assert secret not in text


# --------------------------------------------------------------------------- #
# Local-only enforcement                                                       #
# --------------------------------------------------------------------------- #


async def test_start_module_local_only_enforced(
    monkeypatch,
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """The module launcher is local-only; a non-local client is rejected 403.

    The ASGI test client reports a local host (127.0.0.1 / testclient) which
    is in the local allowlist, so we monkeypatch the local-hosts set to empty
    to force a non-local verdict and confirm the 403 path.
    """
    monkeypatch.setattr("api.routes.modules._LOCAL_HOSTS", set())

    response = await client.post("/modules/openbb/start", headers=auth_headers)
    assert response.status_code == 403
    assert "local-only" in response.json()["detail"]
