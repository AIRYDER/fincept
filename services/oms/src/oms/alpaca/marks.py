"""
oms.alpaca.marks — shared mark-price store backed by Redis.

Alpaca's ``GET /v2/positions`` already returns ``current_price`` per
position, so we don't need a second network call to get a "mark".  We
simply stash the value under ``md:last:{symbol}`` so any service can
read the latest mark without parsing position payloads.

Redis schema::

    md:last:{symbol}  -> HASH { px: str(Decimal), ts_ns: str(int) }

Both fields are stored as strings so Decimal precision survives the
Python/Redis boundary.  ``ts_ns`` is a nanosecond Unix timestamp from
``fincept_core.clock.now_ns`` - consumers can compute staleness as
``now_ns() - ts_ns`` and drop marks that are older than some SLA.

The key has a TTL (see :data:`MARK_TTL_SEC` and ``Settings.MARK_TTL_SEC``)
so a process restart or upstream outage does not leave a stale mark
indefinitely.  Consumers should treat a missing mark as
``DataFreshness.STALE`` rather than ``absent``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from redis.asyncio import Redis

from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_db.provider_data import build_alpaca_mark_record, write_provider_data

#: Default TTL in seconds.  Override at process level via
#: ``Settings.MARK_TTL_SEC`` if the operator wants a longer window.
MARK_TTL_SEC: int = 300


def mark_key(symbol: str) -> str:
    return f"md:last:{symbol}"


async def write_mark(redis: Redis[Any], symbol: str, price: Decimal) -> None:
    """Upsert the latest mark for ``symbol`` with a TTL.

    The TTL bounds how long a stale mark survives a process restart or
    upstream outage.  Default 5 min; configurable via Settings.
    """
    ttl = get_settings().MARK_TTL_SEC or MARK_TTL_SEC
    key = mark_key(symbol)
    ts = now_ns()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping={"px": str(price), "ts_ns": str(ts)})
        pipe.expire(key, ttl)
        await pipe.execute()
    # Provider evidence for freshness receipts (TASK-0205). Best-effort.
    try:
        rec = build_alpaca_mark_record(symbol=symbol, price=price, ts_ns=ts)
        await write_provider_data([rec])
    except Exception:
        pass


async def read_mark(redis: Redis[Any], symbol: str) -> Decimal | None:
    """Return the last written mark, or None if we've never seen it."""
    raw = await redis.hget(mark_key(symbol), "px")
    if raw is None:
        return None
    return Decimal(raw.decode() if isinstance(raw, bytes) else raw)


async def read_marks(
    redis: Redis[Any], symbols: list[str]
) -> dict[str, Decimal]:
    """Bulk read.  Pipeline keeps the round-trip to a single call."""
    if not symbols:
        return {}
    pipe = redis.pipeline()
    for symbol in symbols:
        pipe.hget(mark_key(symbol), "px")
    results = await pipe.execute()
    out: dict[str, Decimal] = {}
    for symbol, raw in zip(symbols, results, strict=True):
        if raw is None:
            continue
        out[symbol] = Decimal(raw.decode() if isinstance(raw, bytes) else raw)
    return out
