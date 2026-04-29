"""
features.store — online (Redis) + offline (Timescale) FeatureFrame stores.

Two layers, two access patterns:

  - ``OnlineStore``   Last-known FeatureFrame per ``(symbol, freq)`` in
                      Redis with a 5-day TTL.  Serves agent inference at
                      <10 ms.  Lossy and ephemeral by design — restart
                      the cluster and the cache empties; backfill from
                      Timescale repopulates it.
  - ``OfflineStore``  Append-only Timescale hypertable.  Authoritative
                      history for backtesting, training, and PIT joins.
                      Idempotent via ``ON CONFLICT DO UPDATE``.

The two are **independently** populated:

  - ``OnlineRunner`` writes to ``OnlineStore`` from the live bar feed.
  - ``offline.backfill`` writes to ``OfflineStore`` by replaying bars
    from Timescale through ``FeatureComputer``.

Bit-identical guarantee comes from sharing the ``FeatureComputer`` code
path; we never copy formulas between online and offline.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from redis.asyncio import Redis

from fincept_core.schemas import FeatureFrame
from fincept_db.features import read_features, write_features

ONLINE_KEY_TEMPLATE = "features:{symbol}:{freq}"
DEFAULT_ONLINE_TTL_S = 5 * 86_400  # 5 days


class OnlineStore:
    """Latest-known FeatureFrame per ``(symbol, freq)`` in Redis."""

    def __init__(self, redis: Redis[Any], *, ttl_s: int = DEFAULT_ONLINE_TTL_S) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    @staticmethod
    def _key(symbol: str, freq: str) -> str:
        return ONLINE_KEY_TEMPLATE.format(symbol=symbol, freq=freq)

    async def put(self, frame: FeatureFrame) -> None:
        """Cache *frame* as the latest for its ``(symbol, freq)``."""
        await self._redis.set(
            self._key(frame.symbol, frame.freq),
            frame.model_dump_json(),
            ex=self._ttl_s,
        )

    async def get_latest(self, symbol: str, freq: str = "1m") -> FeatureFrame | None:
        """Return the cached latest FeatureFrame, or ``None`` if expired/missing."""
        raw = await self._redis.get(self._key(symbol, freq))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return FeatureFrame.model_validate_json(raw)


class _OfflineWriter(Protocol):
    async def __call__(self, frames: Iterable[FeatureFrame]) -> int: ...


class _OfflineReader(Protocol):
    async def __call__(
        self, symbol: str, freq: str, start_ns: int, end_ns: int
    ) -> list[FeatureFrame]: ...


class OfflineStore:
    """Authoritative Timescale-backed FeatureFrame store.

    The default constructor wires through ``fincept_db.features`` so
    production code just needs ``OfflineStore()``.  Tests inject in-memory
    fakes via ``write_fn`` and ``read_fn`` for deterministic execution
    without a database.
    """

    def __init__(
        self,
        *,
        write_fn: _OfflineWriter | None = None,
        read_fn: _OfflineReader | None = None,
    ) -> None:
        self._write_fn: _OfflineWriter = write_fn if write_fn is not None else write_features
        self._read_fn: _OfflineReader = read_fn if read_fn is not None else read_features

    async def put_many(self, frames: Iterable[FeatureFrame]) -> int:
        return await self._write_fn(frames)

    async def read_range(
        self, symbol: str, freq: str, start_ns: int, end_ns: int
    ) -> list[FeatureFrame]:
        return await self._read_fn(symbol, freq, start_ns, end_ns)
