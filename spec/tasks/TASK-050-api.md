# TASK-050 · FastAPI HTTP + WebSocket read model

**Phase:** U · **Depends on:** TASK-004 (db), TASK-044 (oms) · **Blocks:** TASK-052 (dashboard)

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
