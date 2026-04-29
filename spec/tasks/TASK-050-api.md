# TASK-050 · FastAPI HTTP + WebSocket read model

**Phase:** U · **Depends on:** TASK-004 (db), TASK-044 (oms) · **Blocks:** TASK-052 (dashboard)

**Status:** [x] Implemented and verified.  Includes TASK-051 (WebSocket multiplexer) which is one route in the same service.

## As-built deviations from the original draft

| Spec said | We did | Why |
|---|---|---|
| `/positions` calls `db.fetch_positions_latest()` (a fictional helper) | `/positions` reads the Redis hash that the portfolio service populates (`positions:{strategy_id}`) via `PositionStore.known_strategies` + `get_all` | The Redis hash is already the canonical UI read path (sub-millisecond, no DB round-trip).  Same online/offline split TASK-017 used for features. |
| `/orders` calls `db.fetch_orders` (also fictional) | `/orders` reads the OMS audit log via new helper `fincept_db.audit.list_recent_orders`, which collapses `oms.state` rows to the latest snapshot per `order_id` | Orders aren't persisted to a dedicated table in v1 — the audit log is canonical.  The collapse is one indexed query against `correlation_id`.  Migration to a dedicated `orders` table is a Phase H concern when volume warrants it. |
| `/kill-switch` imported `risk.kill_switch.activate(...)` | Publishes `AlertEvent` directly to `STREAM_ALERTS` with `code="kill_switch_engaged"` / severity `"critical"`; DELETE publishes the all-clear with severity `"info"` | TASK-041 (risk gate) doesn't exist yet so the `risk` package can't be imported.  The alert IS the canonical signal — when TASK-041 lands its consumer reacts to that exact code. |
| `/strategies/{id}/start|stop` endpoints with Redis pub/sub | **Deferred** — only `GET /strategies` shipped | Start/stop requires a strategy host service (TASK-040 territory) that doesn't exist yet.  Without it, the Redis hash + pub/sub would have no consumer.  When the host lands, this module gains those routes that RPC to it.  The shipped `GET /strategies` reads the same `portfolio:strategies` index PortfolioStore writes. |
| `consumer.read(stream, cls)` async-iterator API in `ws.py` | WebSocket uses `redis.xread` directly with `$` cursor (live-tail only) | `Consumer.consume(...)` is for durable consumer groups.  WebSockets are transient broadcast — clients should NOT replay history on reconnect (use REST endpoints for backfill).  The XREAD loop is also smaller and has no group-management overhead. |
| Spec `routes/data.py` imported `read_bars_list` | Uses the actual `fincept_db.bars.read_bars` (singular) plus `model_dump(mode="json")` for Decimal→str serialisation | `read_bars_list` doesn't exist; `read_bars` is the canonical reader. |
| Spec implied a new `read_universe` helper | Added `fincept_db.universe.read_universe()` — wasn't there before, but the table existed | Closing a real gap; the universe table had no reader at all. |
| Tests used FastAPI `TestClient` (sync) | Tests use `httpx.AsyncClient` + `ASGITransport` so async fixtures (which seed fakeredis) run on the SAME event loop as the request handlers | `TestClient` spins up its own sync loop; async fixtures + fakeredis on a different loop produces "Queue is bound to a different event loop" errors.  The async client pattern is also closer to how a real client (Next.js fetch) talks to the API. |
| Spec auth used `Header(...)` with required default | `Header(default=None)` + explicit 401 inside the dependency | `Header(...)` without default returns 422 (Unprocessable Entity) when missing — wrong status code for missing auth.  401 is the right answer for "not authenticated". |
| `Settings.JWT_SECRET` didn't exist | Added to `Settings` + CONTRACTS.md §10 with a dev-only default that production deploys must override | The auth dependency reads it; without the field the API would refuse to start. |
| Lifespan held a singleton Redis client created from settings | Lifespan stashes the client at `app.state.redis`; tests override the lifespan with a fakeredis client of the same shape | Lets tests run end-to-end against a fake without monkeypatching `Redis.from_url`. |
| ruff B008 fired on every FastAPI route | Added `[tool.ruff.lint] ignore = ["B008"]` to root + service pyproject.toml | B008 is a well-known FastAPI false-positive.  `Depends(...)`, `Body(...)`, etc. are the canonical pattern; the calls return immutable sentinels and are safe as defaults. |
| `Redis[Any]` annotations on FastAPI dependency signatures | Bare `Redis` with `# type: ignore[type-arg]` only on the FastAPI signatures; the `_emit_alert` helper keeps `Redis[Any]` since FastAPI doesn't introspect it | FastAPI's runtime introspection forces `eval_str=True` on annotations — even with `from __future__ import annotations`, the string `"Redis[Any]"` gets evaluated and fails because `redis.asyncio.Redis` is not generic at runtime (only `types-redis` stubs make it look generic). |

## Goal

Authenticated HTTP+WS API exposing positions, orders, bars, strategies, and a kill-switch endpoint. Streams positions/fills/predictions over WebSocket.

## Files to create

```
services/api/
├── pyproject.toml
├── src/api/
│   ├── __init__.py
│   ├── main.py
│   ├── auth.py
│   ├── deps.py
│   ├── ws.py
│   └── routes/
│       ├── __init__.py
│       ├── data.py
│       ├── positions.py
│       ├── orders.py
│       ├── strategies.py
│       └── control.py
└── tests/
    ├── test_health.py
    └── test_kill_switch.py
```

## Contracts

### `main.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fincept_core.logging import configure_logging
from fincept_core.tracing import configure_tracing
from .routes import data, positions, orders, strategies, control
from .ws import router as ws_router

configure_logging()
configure_tracing("api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="Fincept API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def health():
    return {"ok": True, "version": app.version}

app.include_router(data.router, prefix="/data", tags=["data"])
app.include_router(positions.router, prefix="/positions", tags=["positions"])
app.include_router(orders.router, prefix="/orders", tags=["orders"])
app.include_router(strategies.router, prefix="/strategies", tags=["strategies"])
app.include_router(control.router, prefix="", tags=["control"])
app.include_router(ws_router, prefix="/ws", tags=["ws"])
```

### `auth.py`

```python
import os, jwt
from fastapi import HTTPException, Header

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")

def require_user(authorization: str = Header(...)) -> dict:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer")
    token = authorization.split(" ", 1)[1]
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, str(e))
```

### `routes/data.py`

```python
from fastapi import APIRouter, Depends, Query
from fincept_db.bars import read_bars_list
from ..auth import require_user

router = APIRouter()

@router.get("/bars/{symbol}")
async def get_bars(
    symbol: str, freq: str = Query("1m"), start: int = Query(...), end: int = Query(...),
    _: dict = Depends(require_user),
):
    return await read_bars_list([symbol], freq, start, end)
```

### `routes/positions.py`

```python
from fastapi import APIRouter, Depends
from fincept_db.engine import get_db
from ..auth import require_user

router = APIRouter()

@router.get("")
async def list_positions(_: dict = Depends(require_user), db = Depends(get_db)):
    rows = await db.fetch_positions_latest()   # implement in fincept-db
    return [r for r in rows]
```

### `routes/orders.py`

```python
from fastapi import APIRouter, Depends, Query
from fincept_core.schemas import OrderStatus
from fincept_db.engine import get_db
from ..auth import require_user

router = APIRouter()

@router.get("")
async def list_orders(
    strategy_id: str | None = Query(None),
    status: OrderStatus | None = Query(None),
    _: dict = Depends(require_user), db = Depends(get_db),
):
    return await db.fetch_orders(strategy_id=strategy_id, status=status)
```

### `routes/strategies.py`

```python
from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from ..auth import require_user
from ..deps import get_redis

router = APIRouter()

@router.get("")
async def list_strategies(_: dict = Depends(require_user), r: Redis = Depends(get_redis)):
    raw = await r.hgetall("strategies:registry")
    return [{"strategy_id": k.decode(), "status": v.decode()} for k, v in raw.items()]

@router.post("/{strategy_id}/start")
async def start(strategy_id: str, _: dict = Depends(require_user), r: Redis = Depends(get_redis)):
    await r.hset("strategies:registry", strategy_id, "running")
    await r.publish("strategies.events", f"start:{strategy_id}")
    return {"ok": True}

@router.post("/{strategy_id}/stop")
async def stop(strategy_id: str, _: dict = Depends(require_user), r: Redis = Depends(get_redis)):
    await r.hset("strategies:registry", strategy_id, "stopped")
    await r.publish("strategies.events", f"stop:{strategy_id}")
    return {"ok": True}
```

### `routes/control.py`

```python
from fastapi import APIRouter, Depends, Body
from redis.asyncio import Redis
from risk import kill_switch
from ..auth import require_user
from ..deps import get_redis

router = APIRouter()

@router.post("/kill-switch")
async def trip(payload: dict = Body(...), _: dict = Depends(require_user), r: Redis = Depends(get_redis)):
    await kill_switch.activate(r, reason=payload.get("reason", "manual"))
    return {"ok": True}

@router.delete("/kill-switch")
async def clear(_: dict = Depends(require_user), r: Redis = Depends(get_redis)):
    await kill_switch.deactivate(r)
    return {"ok": True}
```

### `deps.py`

```python
from redis.asyncio import Redis
from fincept_core.config import get_settings

_redis: Redis | None = None

async def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url)
    return _redis
```

### `ws.py`

```python
import asyncio, json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from fincept_core.config import get_settings
from fincept_core.schemas import Position, Fill, Prediction
from fincept_bus.consumer import Consumer
from fincept_bus.streams import STREAM_POSITIONS, STREAM_FILLS, STREAM_SIG_PREDICT

router = APIRouter()

TOPIC_MAP = {
    "positions": (STREAM_POSITIONS, Position),
    "fills":     (STREAM_FILLS, Fill),
    "predictions": (STREAM_SIG_PREDICT, Prediction),
}

@router.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url)
    sub = {"topics": ["positions", "fills"]}
    try:
        sub = await ws.receive_json()
    except Exception:
        pass
    consumers = []
    queues = []
    for t in sub.get("topics", []):
        stream, cls = TOPIC_MAP.get(t, (None, None))
        if stream is None:
            continue
        c = Consumer(redis, group=f"ws:{t}", consumer="ws-1")
        consumers.append((c, stream, cls))
        queues.append(asyncio.Queue())

    async def reader(c, stream, cls, q):
        async for mid, env in c.read(stream, cls):
            await q.put(env.payload.model_dump(mode="json"))
            await c.ack(stream, mid)

    tasks = [asyncio.create_task(reader(c, s, cls, q)) for (c, s, cls), q in zip(consumers, queues, strict=False)]
    try:
        while True:
            done, _ = await asyncio.wait([asyncio.create_task(q.get()) for q in queues], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                await ws.send_json(t.result())
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await redis.aclose()
```

## Tests

```python
# tests/test_health.py
from fastapi.testclient import TestClient
from api.main import app

def test_health():
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True
```

```python
# tests/test_kill_switch.py — requires JWT_SECRET + redis
import jwt, os, pytest
from fastapi.testclient import TestClient

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test")
    from api.main import app
    return TestClient(app)

def test_kill_switch_requires_auth(client):
    r = client.post("/kill-switch", json={"reason": "x"})
    assert r.status_code == 401

def test_kill_switch_with_token(client):
    tok = jwt.encode({"sub": "alice"}, "test", algorithm="HS256")
    r = client.post("/kill-switch", headers={"Authorization": f"Bearer {tok}"}, json={"reason": "drill"})
    assert r.status_code == 200
```

## Out of scope

- Real OAuth flow / refresh tokens — use simple HS256 for MVP
- Rate limiting — Phase H
- API versioning — defer until first breaking change

## Done when

- [ ] Files exist, tests green
- [ ] `uv run uvicorn api.main:app --reload` boots; `/health` returns 200; `/kill-switch` rejects unauthenticated and accepts JWT
