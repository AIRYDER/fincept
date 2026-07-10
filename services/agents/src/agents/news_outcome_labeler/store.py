from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fincept_core.schemas import FeatureFrame
from redis.asyncio import Redis

PENDING_ZSET = "news_alpha:pending_labels"
EXAMPLE_KEY_TEMPLATE = "news_alpha:example:{example_id}"
DEFAULT_HORIZONS_NS: dict[str, int] = {
    "5m": 5 * 60 * 1_000_000_000,
    "30m": 30 * 60 * 1_000_000_000,
    "4h": 4 * 60 * 60 * 1_000_000_000,
}
PriceLookup = Callable[[str, int], Awaitable[Decimal | None]]


@dataclass(frozen=True)
class MaturedLabel:
    example_id: str
    symbol: str
    horizon: str
    return_value: float
    start_price: Decimal
    end_price: Decimal


def example_key(example_id: str) -> str:
    return EXAMPLE_KEY_TEMPLATE.format(example_id=example_id)


def example_id_for(frame: FeatureFrame) -> str:
    payload = json.dumps(
        {
            "symbol": frame.symbol.upper(),
            "ts_event": frame.ts_event,
            "freq": frame.freq,
            "values": frame.values,
            "tags": frame.tags,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _decode(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode()
    return raw


class NewsOutcomeStore:
    def __init__(
        self,
        redis: Redis[Any],
        *,
        horizons_ns: dict[str, int] | None = None,
    ) -> None:
        self._redis = redis
        self._horizons_ns = dict(horizons_ns or DEFAULT_HORIZONS_NS)

    @property
    def horizons_ns(self) -> dict[str, int]:
        return dict(self._horizons_ns)

    async def capture_snapshot(
        self,
        frame: FeatureFrame,
        *,
        start_price: Decimal | None,
    ) -> str | None:
        if frame.freq != "sentiment":
            return None
        if start_price is None:
            return None
        example_id = example_id_for(frame)
        key = example_key(example_id)
        inserted = await self._redis.hsetnx(key, "frame", frame.model_dump_json())
        if inserted:
            await self._redis.hset(
                key,
                mapping={
                    "symbol": frame.symbol.upper(),
                    "ts_event": str(frame.ts_event),
                    "start_price": str(start_price),
                },
            )
            for horizon, horizon_ns in self._horizons_ns.items():
                await self._redis.zadd(
                    PENDING_ZSET,
                    {f"{example_id}:{horizon}": frame.ts_event + horizon_ns},
                )
        return example_id

    async def label_due(
        self,
        *,
        now_ns: int,
        price_lookup: PriceLookup,
        limit: int = 200,
    ) -> list[MaturedLabel]:
        due = await self._redis.zrangebyscore(
            PENDING_ZSET, min=0, max=now_ns, start=0, num=limit
        )
        labels: list[MaturedLabel] = []
        for raw_member in due:
            member = _decode(raw_member)
            if member is None or ":" not in member:
                continue
            example_id, horizon = member.rsplit(":", 1)
            key = example_key(example_id)
            data = await self._redis.hgetall(key)
            decoded = {
                _decode(k): _decode(v)
                for k, v in data.items()
                if _decode(k) is not None and _decode(v) is not None
            }
            symbol = decoded.get("symbol")
            ts_event_raw = decoded.get("ts_event")
            start_raw = decoded.get("start_price")
            if symbol is None or ts_event_raw is None or start_raw is None:
                await self._redis.zrem(PENDING_ZSET, member)
                continue
            start_price = Decimal(start_raw)
            end_price = await price_lookup(
                symbol, int(ts_event_raw) + self._horizons_ns[horizon]
            )
            if end_price is None:
                continue
            return_value = float((end_price / start_price) - Decimal(1))
            await self._redis.hset(
                key,
                mapping={
                    f"label:{horizon}:end_price": str(end_price),
                    f"label:{horizon}:return": str(return_value),
                    f"label:{horizon}:labeled_at": str(now_ns),
                },
            )
            await self._redis.zrem(PENDING_ZSET, member)
            labels.append(
                MaturedLabel(
                    example_id=example_id,
                    symbol=symbol,
                    horizon=horizon,
                    return_value=return_value,
                    start_price=start_price,
                    end_price=end_price,
                )
            )
        return labels
