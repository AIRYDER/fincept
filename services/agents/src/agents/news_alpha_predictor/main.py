from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import signal
from typing import Any

from redis.asyncio import Redis

from agents.news_alpha_predictor.infer import NewsAlphaPredictor
from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_FEATURES_ONLINE, STREAM_SIG_PREDICT
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import FeatureFrame
from fincept_core.tracing import configure_tracing

log = get_logger(__name__)
SERVICE_NAME = "news_alpha_predictor"
GROUP_NAME = "news_alpha_predictor.v1"
DEFAULT_MODEL_DIR = "models/news_alpha_predictor"


def resolve_model_dir() -> pathlib.Path:
    models_root = pathlib.Path(os.environ.get("MODELS_DIR", "models"))
    active_dir = pathlib.Path(os.environ.get("ACTIVE_MODELS_DIR", str(models_root / "active")))
    pointer = active_dir / "news_alpha_predictor.v1.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text())
            model_name = data.get("model_name")
            if isinstance(model_name, str) and model_name:
                return models_root / model_name
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return pathlib.Path(os.environ.get("NEWS_ALPHA_MODEL_DIR", DEFAULT_MODEL_DIR))


async def handle_feature_event(
    event: Event,
    *,
    predictor: NewsAlphaPredictor,
    producer: Producer,
) -> None:
    if event.type != "feature_frame" or not isinstance(event.payload, FeatureFrame):
        return
    if event.payload.freq != "sentiment":
        return
    prediction = predictor.predict_frame(event.payload)
    if prediction is None:
        return
    await producer.publish(STREAM_SIG_PREDICT, Event(type="prediction", payload=prediction))
    log.info(
        "news_alpha.pred",
        symbol=prediction.symbol,
        direction=prediction.direction,
        confidence=prediction.confidence,
    )


async def run_loop(*, consumer_name: str, stop: asyncio.Event) -> None:
    settings = get_settings()
    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)
    model_dir = resolve_model_dir()
    predictor = NewsAlphaPredictor(model_dir=model_dir)
    heartbeat_task: asyncio.Task[None] | None = None
    consume_task: asyncio.Task[None] | None = None

    try:
        predictor.load()
        heartbeat_task = asyncio.create_task(beat_periodically(redis, SERVICE_NAME))

        async def handler(event: Event) -> None:
            await handle_feature_event(event, predictor=predictor, producer=producer)

        log.info("news_alpha.start", consumer_name=consumer_name, model_dir=str(model_dir))
        consume_task = asyncio.create_task(
            consumer.consume(
                [STREAM_FEATURES_ONLINE],
                GROUP_NAME,
                consumer_name,
                handler,
                block_ms=1000,
                batch=100,
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
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        await redis.aclose()  # type: ignore[attr-defined]


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
        log.info("news_alpha.stop")


def main() -> None:
    parser = argparse.ArgumentParser(prog="news_alpha_predictor.main")
    parser.add_argument("--consumer-name", default="news-alpha-predictor-1")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
