from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from redis.asyncio import Redis

from features.store import OnlineStore
from fincept_core.clock import now_ns
from fincept_core.schemas import FeatureFrame, SentimentSignal

FREQ = "sentiment"
SERVICE_TAG = "sentiment_features.v1"
OBS_KEY_TEMPLATE = "sentiment_features:observations:{symbol}"
DEFAULT_WINDOWS_MIN = (5, 30, 240)
DEFAULT_HISTORY_TTL_S = 5 * 86_400
NANOSECONDS_PER_SECOND = 1_000_000_000
NANOSECONDS_PER_MINUTE = 60 * NANOSECONDS_PER_SECOND


@dataclass(frozen=True)
class SentimentObservation:
    ts_event: int
    score: float
    confidence: float
    event_type: str | None
    source: str


def _observation_key(symbol: str) -> str:
    return OBS_KEY_TEMPLATE.format(symbol=symbol.upper())


def _source_from_signal(signal: SentimentSignal) -> str:
    if signal.source_url:
        parsed = urlparse(signal.source_url)
        if parsed.netloc:
            return parsed.netloc.lower()
    return signal.agent_id


def _member_for(signal: SentimentSignal) -> str:
    payload = {
        "agent_id": signal.agent_id,
        "confidence": signal.confidence,
        "event_type": signal.event_type,
        "score": signal.score,
        "source": _source_from_signal(signal),
        "source_url": signal.source_url,
        "ts_event": signal.ts_event,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_observation(raw: str | bytes) -> SentimentObservation:
    if isinstance(raw, bytes):
        raw = raw.decode()
    payload = json.loads(raw)
    return SentimentObservation(
        ts_event=int(payload["ts_event"]),
        score=float(payload["score"]),
        confidence=float(payload["confidence"]),
        event_type=payload.get("event_type"),
        source=str(payload.get("source") or payload.get("agent_id") or "unknown"),
    )


def _weighted_mean(observations: list[SentimentObservation]) -> float | None:
    weight_sum = sum(max(0.0, obs.confidence) for obs in observations)
    if weight_sum <= 0.0:
        return None
    return (
        sum(obs.score * max(0.0, obs.confidence) for obs in observations) / weight_sum
    )


def _mean_confidence(observations: list[SentimentObservation]) -> float | None:
    if not observations:
        return None
    return sum(obs.confidence for obs in observations) / len(observations)


def _weighted_std(observations: list[SentimentObservation]) -> float | None:
    mean = _weighted_mean(observations)
    if mean is None:
        return None
    weight_sum = sum(max(0.0, obs.confidence) for obs in observations)
    if weight_sum <= 0.0:
        return None
    variance = (
        sum(
            max(0.0, obs.confidence) * ((obs.score - mean) ** 2) for obs in observations
        )
        / weight_sum
    )
    return math.sqrt(max(0.0, variance))


def _latest_event_type(observations: list[SentimentObservation]) -> str | None:
    typed = [obs for obs in observations if obs.event_type]
    if not typed:
        return None
    return max(typed, key=lambda obs: obs.ts_event).event_type


def _dominant_event_type(observations: list[SentimentObservation]) -> str | None:
    counts = Counter(obs.event_type for obs in observations if obs.event_type)
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def build_sentiment_frame(
    *,
    symbol: str,
    observations: list[SentimentObservation],
    ts_event: int,
    windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN,
) -> FeatureFrame:
    values: dict[str, float | None] = {}
    tags: dict[str, str] = {"builder": SERVICE_TAG}
    latest_window_observations: list[SentimentObservation] = []

    for window_min in windows_min:
        cutoff = ts_event - (window_min * NANOSECONDS_PER_MINUTE)
        window_obs = [obs for obs in observations if obs.ts_event >= cutoff]
        if window_min == max(windows_min):
            latest_window_observations = window_obs
        prefix = f"sentiment_{window_min}m"
        values[prefix] = _weighted_mean(window_obs)
        values[f"{prefix}_confidence"] = _mean_confidence(window_obs)
        values[f"{prefix}_article_count"] = float(len(window_obs))
        values[f"{prefix}_unique_sources"] = float(
            len({obs.source for obs in window_obs})
        )
        values[f"{prefix}_disagreement"] = _weighted_std(window_obs)
        values[f"{prefix}_max_negative_urgency"] = max(
            ((-obs.score) * obs.confidence for obs in window_obs if obs.score < 0.0),
            default=0.0,
        )
        if window_min % 60 == 0:
            hours = window_min // 60
            alias = f"sentiment_{hours}h"
            values[alias] = values[prefix]
            values[f"{alias}_confidence"] = values[f"{prefix}_confidence"]
            values[f"{alias}_article_count"] = values[f"{prefix}_article_count"]
            values[f"{alias}_unique_sources"] = values[f"{prefix}_unique_sources"]
            values[f"{alias}_disagreement"] = values[f"{prefix}_disagreement"]
            values[f"{alias}_max_negative_urgency"] = values[
                f"{prefix}_max_negative_urgency"
            ]

    latest_type = _latest_event_type(observations)
    dominant_type = _dominant_event_type(latest_window_observations or observations)
    if latest_type:
        tags["latest_event_category"] = latest_type
    if dominant_type:
        tags["dominant_event_category"] = dominant_type

    return FeatureFrame(
        symbol=symbol.upper(),
        ts_event=ts_event,
        freq=FREQ,
        values=values,
        tags=tags,
    )


class SentimentFeatureStore:
    def __init__(
        self,
        redis: Redis[Any],
        *,
        online_store: OnlineStore | None = None,
        windows_min: tuple[int, ...] = DEFAULT_WINDOWS_MIN,
        history_ttl_s: int = DEFAULT_HISTORY_TTL_S,
    ) -> None:
        self._redis = redis
        self._online_store = online_store or OnlineStore(redis)
        self._windows_min = tuple(sorted(windows_min))
        self._history_ttl_s = history_ttl_s

    @property
    def max_window_min(self) -> int:
        return max(self._windows_min)

    @property
    def windows_min(self) -> tuple[int, ...]:
        return self._windows_min

    async def _build_and_store_frame(
        self, symbol: str, reference_ts: int
    ) -> FeatureFrame | None:
        symbol = symbol.upper()
        key = _observation_key(symbol)
        cutoff = reference_ts - (self.max_window_min * NANOSECONDS_PER_MINUTE)
        await self._redis.zremrangebyscore(key, min=0, max=cutoff - 1)
        await self._redis.expire(key, self._history_ttl_s)
        observations = await self.read_observations(symbol)
        if not observations:
            return None
        frame = build_sentiment_frame(
            symbol=symbol,
            observations=observations,
            ts_event=reference_ts,
            windows_min=self._windows_min,
        )
        await self._online_store.put(frame)
        return frame

    async def add_signal(self, signal: SentimentSignal) -> FeatureFrame:
        symbol = signal.symbol.upper()
        key = _observation_key(symbol)
        await self._redis.zadd(key, {_member_for(signal): signal.ts_event})
        latest = await self._redis.zrange(key, -1, -1, withscores=True)
        reference_ts = int(latest[0][1]) if latest else signal.ts_event
        frame = await self._build_and_store_frame(symbol, reference_ts)
        if frame is None:
            raise RuntimeError(f"no sentiment observations available for {symbol}")
        return frame

    async def refresh_symbol(
        self, symbol: str, *, ts_event: int | None = None
    ) -> FeatureFrame | None:
        return await self._build_and_store_frame(symbol, ts_event or now_ns())

    async def list_symbols(self) -> list[str]:
        symbols: list[str] = []
        async for key in self._redis.scan_iter(
            match=OBS_KEY_TEMPLATE.format(symbol="*")
        ):
            if isinstance(key, bytes):
                key = key.decode()
            symbols.append(str(key).rsplit(":", 1)[-1].upper())
        return sorted(set(symbols))

    async def refresh_all(self, *, ts_event: int | None = None) -> list[FeatureFrame]:
        frames: list[FeatureFrame] = []
        reference_ts = ts_event or now_ns()
        for symbol in await self.list_symbols():
            frame = await self.refresh_symbol(symbol, ts_event=reference_ts)
            if frame is not None:
                frames.append(frame)
        return frames

    async def read_observations(self, symbol: str) -> list[SentimentObservation]:
        raw_members = await self._redis.zrange(_observation_key(symbol), 0, -1)
        return [_decode_observation(raw) for raw in raw_members]

    async def get_latest(self, symbol: str) -> FeatureFrame | None:
        return await self._online_store.get_latest(symbol.upper(), FREQ)
