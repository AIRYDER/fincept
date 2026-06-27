"""
risk.state - KillSwitchState with Redis persistence.

The kill-switch is engaged or cleared by ``AlertEvent`` records on
``STREAM_ALERTS``.  Canonical codes (set by ``services/api`` in
:mod:`api.routes.control`):

  - ``code="kill_switch_engaged"``  severity="critical"  -> flip engaged=True
  - ``code="kill_switch_cleared"``  severity="info"      -> flip engaged=False

The engaged flag is consulted by ``check_intent`` on every order intent.
When engaged, *every* intent is rejected regardless of notional, until
an operator publishes the cleared alert.

Persistence:
  When a ``redis`` client is provided, the state is persisted to the
  Redis key ``control:kill_switch:state`` (the same key the API writes
  to). On construction, the state is read from Redis. If Redis is
  unavailable or the key is missing, the state defaults to **engaged=True**
  (fail-closed: trading halted) — this is the safe default because an
  operator who engaged the kill-switch expects it to remain engaged
  across restarts.

  The ``apply`` method also writes to Redis on every state transition,
  so the API dashboard and the OMS always see the same state. This fixes
  the state divergence bug where the dashboard showed "ENGAGED" but the
  OMS allowed trading after a restart.

Thread-safety: not thread-safe.  Asyncio-safe because all updates
come from a single ``apply`` call inside the alert consumer task.
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis

from fincept_core.logging import get_logger
from fincept_core.schemas import AlertEvent

log = get_logger(__name__)

CODE_ENGAGED = "kill_switch_engaged"
CODE_CLEARED = "kill_switch_cleared"

KILL_SWITCH_STATE_KEY = "control:kill_switch:state"


class KillSwitchState:
    """Boolean flag fed by AlertEvents, optionally persisted to Redis.

    When ``redis`` is provided:
      - On construction, reads the persisted state from Redis.
      - On every ``apply`` that changes state, writes the new state to Redis.
      - If Redis read fails, defaults to **engaged=True** (fail-closed).

    When ``redis`` is None (e.g. in tests):
      - Behaves exactly as the old in-memory-only implementation.
    """

    def __init__(self, redis: Redis[Any] | None = None) -> None:
        self._redis = redis
        # Default to fail-closed (engaged=True) when Redis is provided
        # but the read fails. Default to engaged=False when no Redis
        # (backward-compatible in-memory behavior for tests).
        if redis is not None:
            self._engaged = True  # fail-closed default until Redis confirms
            self._sync_from_redis()
        else:
            self._engaged = False

    def _sync_from_redis(self) -> None:
        """Read the persisted state from Redis. Called only at construction.

        Uses a sync Redis client because ``__init__`` is not async. The
        async Redis client's connection pool is reused via ``sync_client``.
        """
        if self._redis is None:
            return
        try:
            import redis as sync_redis_mod

            # Extract URL from the async client's connection pool.
            pool = self._redis.connection_pool
            kw = pool.connection_kwargs
            host = kw.get("host", "localhost")
            port = kw.get("port", 6379)
            db = kw.get("db", 0)
            password = kw.get("password")
            if password:
                url = f"redis://:{password}@{host}:{port}/{db}"
            else:
                url = f"redis://{host}:{port}/{db}"

            sync_client = sync_redis_mod.Redis.from_url(url)
            try:
                raw = sync_client.get(KILL_SWITCH_STATE_KEY)
                if raw is not None:
                    raw_text = (
                        raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                    )
                    payload = json.loads(raw_text)
                    self._engaged = bool(payload.get("engaged", True))
                    log.info(
                        "risk.kill_switch.restored_from_redis",
                        engaged=self._engaged,
                        actor=payload.get("actor"),
                        reason=payload.get("reason"),
                    )
                else:
                    # Key doesn't exist — no kill-switch was ever engaged.
                    # Safe to start disengaged.
                    self._engaged = False
                    log.info("risk.kill_switch.no_persisted_state")
            finally:
                sync_client.close()
        except Exception as exc:
            # Fail-closed: if we can't read Redis, assume engaged=True.
            self._engaged = True
            log.warning(
                "risk.kill_switch.redis_read_failed",
                error=f"{type(exc).__name__}: {exc}",
                default="engaged",
            )

    def _persist_to_redis(self, *, engaged: bool, event: AlertEvent) -> None:
        """Write the current state to Redis. Best-effort, non-blocking.

        Uses a sync Redis client because ``apply`` is sync. If the write
        fails, the in-memory state is still correct — the next OMS restart
        will read the last successfully-persisted state.
        """
        if self._redis is None:
            return
        try:
            import redis as sync_redis_mod

            pool = self._redis.connection_pool
            kw = pool.connection_kwargs
            host = kw.get("host", "localhost")
            port = kw.get("port", 6379)
            db = kw.get("db", 0)
            password = kw.get("password")
            if password:
                url = f"redis://:{password}@{host}:{port}/{db}"
            else:
                url = f"redis://{host}:{port}/{db}"

            sync_client = sync_redis_mod.Redis.from_url(url)
            try:
                payload = json.dumps(
                    {
                        "engaged": engaged,
                        "actor": event.source,
                        "reason": event.message,
                        "alert_id": event.alert_id,
                        "ts_unix": __import__("time").time(),
                    }
                )
                sync_client.set(KILL_SWITCH_STATE_KEY, payload)
            finally:
                sync_client.close()
        except Exception as exc:
            log.warning(
                "risk.kill_switch.redis_write_failed",
                error=f"{type(exc).__name__}: {exc}",
                engaged=engaged,
            )

    @property
    def engaged(self) -> bool:
        return self._engaged

    def apply(self, event: AlertEvent) -> None:
        """Apply an alert.  Non-kill-switch alerts are ignored."""
        if event.code == CODE_ENGAGED:
            if not self._engaged:
                log.warning(
                    "risk.kill_switch.engaged",
                    alert_id=event.alert_id,
                    severity=event.severity,
                    source=event.source,
                    message=event.message,
                )
            self._engaged = True
            self._persist_to_redis(engaged=True, event=event)
        elif event.code == CODE_CLEARED:
            if self._engaged:
                log.info(
                    "risk.kill_switch.cleared",
                    alert_id=event.alert_id,
                    source=event.source,
                    message=event.message,
                )
            self._engaged = False
            self._persist_to_redis(engaged=False, event=event)
