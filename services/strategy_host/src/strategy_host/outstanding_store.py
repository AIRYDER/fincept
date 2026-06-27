"""strategy_host.outstanding_store — Redis-backed outstanding-order ledger.

Persists the ``order_id -> OrderIntent`` mapping that the runner uses
to attribute fills to strategies.  Previously this was an in-memory
dict that was lost on restart, causing fills for pre-restart orders
to be silently ignored.

Redis key layout::

    strategy_host:outstanding:{strategy_id}   -> Redis hash
        field:   order_id (str)
        value:   OrderIntent.model_dump_json()

The hash is per-strategy, so multiple strategies on the same host
don't interfere.  On startup, the runner hydrates its in-memory dict
from this hash; on every order submission, it writes to the hash;
cleanup of filled/canceled orders is left to a future task (the
hash grows slowly — one entry per submitted order — and is bounded
in practice by the strategy's order rate).
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from fincept_core.logging import get_logger
from fincept_core.schemas import OrderIntent

log = get_logger(__name__)

OUTSTANDING_KEY_TEMPLATE = "strategy_host:outstanding:{strategy_id}"


class OutstandingOrderStore:
    """Redis-backed store for outstanding order intents.

    All operations are best-effort: if Redis is unavailable, the
    in-memory dict in the runner is still authoritative for the
    current process lifetime.  Redis persistence is a recovery
    mechanism for restarts, not a consistency requirement.
    """

    def __init__(self, redis: Redis[Any], strategy_id: str) -> None:
        self._redis = redis
        self._strategy_id = strategy_id
        self._key = OUTSTANDING_KEY_TEMPLATE.format(strategy_id=strategy_id)

    async def hydrate(self) -> dict[str, OrderIntent]:
        """Load all outstanding orders from Redis into a dict.

        Called once at runner startup.  If Redis fails, returns an
        empty dict (the runner starts fresh — same as the old
        in-memory-only behavior).
        """
        result: dict[str, OrderIntent] = {}
        try:
            raw = await self._redis.hgetall(self._key)
            if not raw:
                return result
            for order_id_bytes, intent_json in raw.items():
                order_id = (
                    order_id_bytes.decode()
                    if isinstance(order_id_bytes, bytes)
                    else str(order_id_bytes)
                )
                if isinstance(intent_json, bytes):
                    intent_json = intent_json.decode()
                try:
                    intent = OrderIntent.model_validate_json(intent_json)
                    result[order_id] = intent
                except Exception as exc:
                    log.warning(
                        "strategy_host.outstanding.hydrate_skip",
                        strategy_id=self._strategy_id,
                        order_id=order_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            if result:
                log.info(
                    "strategy_host.outstanding.hydrated",
                    strategy_id=self._strategy_id,
                    count=len(result),
                )
        except Exception as exc:
            log.warning(
                "strategy_host.outstanding.hydrate_failed",
                strategy_id=self._strategy_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        return result

    async def put(self, order_id: str, intent: OrderIntent) -> None:
        """Record a newly-submitted order intent.

        Best-effort: logs a warning on failure but does not raise.
        The in-memory dict is still updated by the caller.
        """
        try:
            await self._redis.hset(self._key, order_id, intent.model_dump_json())
        except Exception as exc:
            log.warning(
                "strategy_host.outstanding.put_failed",
                strategy_id=self._strategy_id,
                order_id=order_id,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def remove(self, order_id: str) -> None:
        """Remove an order from the ledger (e.g., after terminal fill).

        Best-effort: logs a warning on failure.
        """
        try:
            await self._redis.hdel(self._key, order_id)
        except Exception as exc:
            log.warning(
                "strategy_host.outstanding.remove_failed",
                strategy_id=self._strategy_id,
                order_id=order_id,
                error=f"{type(exc).__name__}: {exc}",
            )
