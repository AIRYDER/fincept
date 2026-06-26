from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from pathlib import Path
import signal
from typing import Any, cast

from redis.asyncio import Redis

from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_INFO_ENRICHED, STREAM_SIG_NEWS_IMPACT
from fincept_core.config import assert_safe_for_runtime, get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import (
    InformationEvent,
    NewsImpactHorizon,
    NewsImpactSignal,
)
from fincept_core.tracing import configure_tracing
from news_impact_model.analogs import HistoricalAnalogIndex
from news_impact_model.data import load_historical_outcomes
from news_impact_model.model import NewsImpactModel
from news_impact_model.schema import MarketContext, NewsEvent

ROOT = Path(__file__).resolve().parents[5]
DEFAULT_HISTORY_PATH = (
    ROOT
    / "experiments"
    / "news-impact-model"
    / "sample_data"
    / "historical_outcomes.jsonl"
)

log = get_logger(__name__)

AGENT_ID = "news_impact_agent.v1"
SERVICE_NAME = "news_impact_agent"
GROUP_NAME = "news_impact_agent.v1"
DEFAULT_HORIZONS = ("5m", "30m", "1h")


def information_to_news_event(info: InformationEvent) -> NewsEvent:
    """Convert enriched information into the experiment's point-in-time event."""

    return NewsEvent(
        event_id=info.event_id,
        available_at_ns=_available_at_ns(info),
        source=info.source,
        headline=info.headline,
        body=info.body,
        symbols=tuple(info.symbols),
        event_type=info.event_category or "general",
        source_priority=info.source_quality,
    )


def build_news_impact_signals(
    info: InformationEvent,
    *,
    model: NewsImpactModel,
) -> list[NewsImpactSignal]:
    """Score one enriched information event for every affected symbol."""

    event = information_to_news_event(info)
    signals: list[NewsImpactSignal] = []
    for symbol in event.symbols:
        prediction = model.predict(
            event,
            MarketContext(symbol=symbol),
        )
        if not prediction.similar_events:
            continue
        horizons = {
            horizon: NewsImpactHorizon(
                expected_return=impact.expected_return,
                p_up=impact.p_up,
                q10=impact.q10,
                q50=impact.q50,
                q90=impact.q90,
                sample_size=impact.sample_size,
            )
            for horizon, impact in prediction.horizons.items()
            if impact.sample_size > 0
        }
        if not horizons:
            continue
        signals.append(
            NewsImpactSignal(
                agent_id=AGENT_ID,
                event_id=info.event_id,
                symbol=symbol,
                ts_event=event.available_at_ns,
                available_at_ns=event.available_at_ns,
                event_type=prediction.event_type,
                confidence=prediction.confidence,
                horizons=horizons,
                source_urls=[info.url] if info.url else [],
                similar_event_ids=[
                    similar.event_id for similar in prediction.similar_events
                ],
                model_version=prediction.model_version,
                metadata={
                    "source": info.source,
                    "source_type": info.source_type,
                    "dedupe_key": info.dedupe_key,
                    "dedupe_group_id": info.dedupe_group_id or "",
                },
            )
        )
    return signals


async def handle_information_event(
    event: Event,
    *,
    model: NewsImpactModel,
    producer: Producer,
) -> int:
    if event.type != "information" or not isinstance(event.payload, InformationEvent):
        return 0
    emitted = 0
    for impact_signal in build_news_impact_signals(event.payload, model=model):
        await producer.publish(
            STREAM_SIG_NEWS_IMPACT,
            Event(type="news_impact", payload=impact_signal),
        )
        emitted += 1
        log.info(
            "news_impact.emitted",
            event_id=impact_signal.event_id,
            symbol=impact_signal.symbol,
            confidence=impact_signal.confidence,
            horizons=list(impact_signal.horizons),
        )
    return emitted


def load_model(
    *,
    history_path: Path | None = None,
    horizons: tuple[str, ...] = DEFAULT_HORIZONS,
) -> NewsImpactModel:
    path = history_path or resolve_history_path()
    outcomes = load_historical_outcomes(path) if path.is_file() else []
    index = HistoricalAnalogIndex()
    index.extend(outcomes)
    return NewsImpactModel(index=index, horizons=horizons)


def resolve_history_path() -> Path:
    return Path(os.environ.get("NEWS_IMPACT_HISTORY_PATH", str(DEFAULT_HISTORY_PATH)))


async def run_loop(*, consumer_name: str, stop: asyncio.Event) -> None:
    settings = get_settings()
    assert_safe_for_runtime(settings)
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)
    model = load_model()
    heartbeat_task = asyncio.create_task(beat_periodically(redis, SERVICE_NAME))
    consume_task: asyncio.Task[None] | None = None

    async def handler(event: Event) -> None:
        await handle_information_event(event, model=model, producer=producer)

    try:
        log.info(
            "news_impact.start",
            consumer_name=consumer_name,
            history_path=str(resolve_history_path()),
            input_stream=STREAM_INFO_ENRICHED,
            output_stream=STREAM_SIG_NEWS_IMPACT,
        )
        consume_task = asyncio.create_task(
            consumer.consume(
                [STREAM_INFO_ENRICHED],
                GROUP_NAME,
                consumer_name,
                handler,
                block_ms=1000,
                batch=50,
            )
        )
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait(
            {consume_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            await task
    finally:
        if consume_task is not None:
            consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consume_task
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await cast(Any, redis).aclose()


def _available_at_ns(info: InformationEvent) -> int:
    raw = info.metadata.get("available_at_ns")
    if isinstance(raw, str) and raw:
        return int(raw)
    return info.ts_event


async def _main(args: argparse.Namespace) -> None:
    configure_logging()
    configure_tracing(SERVICE_NAME)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_loop(consumer_name=args.consumer_name, stop=stop)
    finally:
        log.info("news_impact.stop")


def main() -> None:
    parser = argparse.ArgumentParser(prog="news_impact_agent.main")
    parser.add_argument("--consumer-name", default="news-impact-agent-1")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
