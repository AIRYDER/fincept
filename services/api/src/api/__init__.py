"""
api — FastAPI HTTP + WebSocket service.

Read model: positions (Redis hash), orders (audit log), bars (Timescale),
universe (Postgres), strategies (Redis hash + portfolio metadata),
real-time WS streams (positions / fills / predictions / alerts).

Write model: kill-switch only.  Strategy start/stop is deferred to the
strategy host service (TASK-040 territory) — the API will RPC to it
when that lands.
"""

from api.main import app

__all__ = ["app"]
