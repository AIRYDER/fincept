"""
agents.sentiment_agent.main - long-running entrypoint.

Polling loop::

  python -m agents.sentiment_agent.main

Per cycle:

  1. For each symbol in ``settings.UNIVERSE`` with a NewsAPI query
     mapping, fetch the latest articles published since
     ``last_published_seen[symbol]``.
  2. Skip articles already scored (Redis dedup key keeps URLs for
     ``DEDUP_TTL_SEC``).
  3. Score each surviving article via Anthropic.
  4. Publish a ``SentimentSignal`` to ``STREAM_SIG_SENT``.
  5. Heartbeat.

Cycle cadence is controlled by ``--interval-sec`` (default 300 = 5 min).
The free NewsAPI tier allows 100 requests/day per key; with 3 symbols
and a 5-minute cycle, that's 3 * 12 * 24 = 864 requests/day - over the
free quota.  In practice you should either bump to a paid tier
($449/mo for Business at the time of writing) or set the interval to
15+ minutes.

Operationally this agent is OPT-IN: if either NEWSAPI_API_KEY or
ANTHROPIC_API_KEY is unset, the service exits cleanly.  It's listed
in the dashboard's "expected services" only when both are configured
(see services/api/src/api/routes/services.py).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
from typing import Any

import httpx
from redis.asyncio import Redis

from agents.sentiment_agent.llm import LLMRouter, pick_providers
from agents.sentiment_agent.news import Article, fetch_articles, query_for_symbol
from fincept_bus.consumer import Consumer
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_INFO_ENRICHED, STREAM_SIG_SENT
from fincept_core.clock import now_ns
from fincept_core.config import get_settings
from fincept_core.events import Event
from fincept_core.heartbeat import beat_periodically
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import InformationEvent, SentimentSignal
from fincept_core.tracing import configure_tracing

log = get_logger(__name__)

AGENT_ID = "sentiment_agent.v1"
DEDUP_KEY_PREFIX = "sentiment:seen_url:"
INFO_DEDUP_KEY_PREFIX = "sentiment:seen_info:"
DEDUP_TTL_SEC = 24 * 3600  # don't re-score the same article within 24h
INFO_CONSUMER_GROUP = "sentiment_agent.info.v1"


async def _already_seen(redis: Redis[Any], url: str) -> bool:
    """Has this article URL been scored within DEDUP_TTL_SEC?"""
    key = f"{DEDUP_KEY_PREFIX}{url}"
    return await redis.exists(key) > 0


async def _mark_seen(redis: Redis[Any], url: str) -> None:
    key = f"{DEDUP_KEY_PREFIX}{url}"
    await redis.set(key, "1", ex=DEDUP_TTL_SEC)


def _info_seen_key(event: InformationEvent, symbol: str) -> str:
    base = event.dedupe_group_id or event.dedupe_key or event.event_id
    return f"{INFO_DEDUP_KEY_PREFIX}{base}:{symbol}"


async def _already_seen_info(redis: Redis[Any], event: InformationEvent, symbol: str) -> bool:
    return await redis.exists(_info_seen_key(event, symbol)) > 0


async def _mark_seen_info(redis: Redis[Any], event: InformationEvent, symbol: str) -> None:
    await redis.set(_info_seen_key(event, symbol), "1", ex=DEDUP_TTL_SEC)


async def _publish_sentiment(
    *,
    producer: Producer,
    signal: SentimentSignal,
    provider_used: str,
    source: str,
) -> None:
    await producer.publish(
        STREAM_SIG_SENT,
        Event(type="sentiment", payload=signal),
    )
    log.info(
        "sentiment.emitted",
        symbol=signal.symbol,
        score=signal.score,
        confidence=signal.confidence,
        event_type=signal.event_type,
        provider=provider_used,
        source=source,
        url=signal.source_url,
    )


async def _process_information_event(
    *,
    info: InformationEvent,
    llm_router: LLMRouter,
    http: httpx.AsyncClient,
    redis: Redis[Any],
    producer: Producer,
    max_symbols: int,
) -> int:
    emitted = 0
    symbols = [symbol.strip().upper() for symbol in info.symbols if symbol.strip()]
    for symbol in symbols[:max_symbols]:
        if not llm_router.has_capacity:
            break
        if await _already_seen_info(redis, info, symbol):
            continue
        try:
            scored = await llm_router.score(
                http,
                symbol=symbol,
                title=info.headline,
                description=info.body,
                source=info.source,
            )
        except httpx.HTTPError as exc:
            log.warning(
                "sentiment.info_llm_transient_error",
                symbol=symbol,
                event_id=info.event_id,
                error=str(exc),
            )
            raise
        if scored is None:
            await _mark_seen_info(redis, info, symbol)
            continue
        score, provider_used = scored
        entities = list(dict.fromkeys([*info.entities, symbol]))
        signal = SentimentSignal(
            agent_id=AGENT_ID,
            symbol=symbol,
            ts_event=now_ns(),
            score=score.score,
            confidence=score.confidence,
            event_type=score.event_type or info.event_category,
            source_url=info.url,
            source_excerpt=info.headline[:200] if info.headline else None,
            entities=entities,
        )
        await _publish_sentiment(
            producer=producer,
            signal=signal,
            provider_used=provider_used,
            source=info.source,
        )
        await _mark_seen_info(redis, info, symbol)
        emitted += 1
    return emitted


async def _process_symbol(
    *,
    symbol: str,
    query: str,
    newsapi_key: str,
    llm_router: LLMRouter,
    http: httpx.AsyncClient,
    redis: Redis[Any],
    producer: Producer,
    lookback_minutes: int,
    max_per_cycle: int,
) -> int:
    """Fetch -> dedup -> score -> publish for one symbol.  Returns rows emitted."""
    try:
        articles = await fetch_articles(
            http,
            query=query,
            api_key=newsapi_key,
            lookback_minutes=lookback_minutes,
            page_size=max(max_per_cycle, 5),
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        log.warning("sentiment.newsapi_error", symbol=symbol, error=str(exc))
        return 0

    if not articles:
        return 0

    fresh: list[Article] = []
    for article in articles:
        if await _already_seen(redis, article.url):
            continue
        fresh.append(article)
        if len(fresh) >= max_per_cycle:
            break

    emitted = 0
    for article in fresh:
        if not llm_router.has_capacity:
            # All providers exhausted mid-cycle; don't keep trying.
            break
        try:
            scored = await llm_router.score(
                http,
                symbol=symbol,
                title=article.title,
                description=article.description,
                source=article.source,
            )
        except httpx.HTTPError as exc:
            # Transient (timeout, 5xx, plain rate limit).  Don't mark
            # the URL as seen so we retry on the next cycle.
            log.warning(
                "sentiment.llm_transient_error",
                symbol=symbol,
                url=article.url,
                error=str(exc),
            )
            continue

        # Mark as seen on every non-transient outcome (success OR parse
        # failure OR all providers exhausted) so a single bad article
        # can't loop and burn the rate limit.
        await _mark_seen(redis, article.url)
        if scored is None:
            continue
        score, provider_used = scored

        signal = SentimentSignal(
            agent_id=AGENT_ID,
            symbol=symbol,
            ts_event=now_ns(),
            score=score.score,
            confidence=score.confidence,
            event_type=score.event_type,
            source_url=article.url,
            source_excerpt=article.title[:200] if article.title else None,
            entities=[symbol],
        )
        await _publish_sentiment(
            producer=producer,
            signal=signal,
            provider_used=provider_used,
            source=article.source,
        )
        emitted += 1

    return emitted


async def run_loop(
    *,
    interval_sec: int,
    lookback_minutes: int,
    max_per_cycle: int,
    stop: asyncio.Event,
) -> None:
    settings = get_settings()
    providers = pick_providers(
        anthropic_key=settings.ANTHROPIC_API_KEY,
        openai_key=settings.OPENAI_API_KEY,
        preference=settings.LLM_PROVIDER,
    )
    if not providers:
        log.warning(
            "sentiment.skip",
            reason="no LLM provider configured (set ANTHROPIC_API_KEY or OPENAI_API_KEY)",
        )
        return
    llm_router = LLMRouter(providers)

    redis: Redis[Any] = Redis.from_url(settings.REDIS_URL)
    producer = Producer(redis)
    consumer = Consumer(redis)
    heartbeat_task = asyncio.create_task(beat_periodically(redis, "sentiment_agent"))
    info_consumer_task: asyncio.Task[None] | None = None

    try:
        async with httpx.AsyncClient() as http:
            async def info_handler(event: Event) -> None:
                if event.type != "information" or not isinstance(event.payload, InformationEvent):
                    return
                emitted = await _process_information_event(
                    info=event.payload,
                    llm_router=llm_router,
                    http=http,
                    redis=redis,
                    producer=producer,
                    max_symbols=max_per_cycle,
                )
                log.info(
                    "sentiment.info_done",
                    event_id=event.payload.event_id,
                    emitted=emitted,
                    current_provider=(llm_router.current or (None,))[0],
                )

            info_consumer_task = asyncio.create_task(
                consumer.consume(
                    [STREAM_INFO_ENRICHED],
                    INFO_CONSUMER_GROUP,
                    "sentiment-agent-1",
                    info_handler,
                    block_ms=1000,
                    batch=50,
                )
            )
            log.info(
                "sentiment.start",
                providers=llm_router.configured_providers(),
                info_stream=STREAM_INFO_ENRICHED,
                newsapi_enabled=bool(settings.NEWSAPI_API_KEY),
                interval_sec=interval_sec,
                lookback_minutes=lookback_minutes,
                max_per_cycle=max_per_cycle,
                universe=list(settings.UNIVERSE),
            )
            while not stop.is_set():
                if not settings.NEWSAPI_API_KEY:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=interval_sec)
                    continue
                cycle_emitted = 0
                for symbol in settings.UNIVERSE:
                    if stop.is_set():
                        break
                    if not llm_router.has_capacity:
                        # All providers exhausted (auth/billing).  Don't
                        # spin the loop hot - sleep and re-check next cycle
                        # in case the operator added credit / fixed a key.
                        log.warning(
                            "sentiment.no_provider_capacity",
                            exhausted=llm_router.exhausted_providers(),
                        )
                        break
                    query = query_for_symbol(symbol)
                    if query is None:
                        continue
                    cycle_emitted += await _process_symbol(
                        symbol=symbol,
                        query=query,
                        newsapi_key=settings.NEWSAPI_API_KEY,
                        llm_router=llm_router,
                        http=http,
                        redis=redis,
                        producer=producer,
                        lookback_minutes=lookback_minutes,
                        max_per_cycle=max_per_cycle,
                    )
                log.info(
                    "sentiment.cycle_done",
                    emitted=cycle_emitted,
                    current_provider=(llm_router.current or (None,))[0],
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval_sec)
    finally:
        if info_consumer_task is not None:
            info_consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await info_consumer_task
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await redis.aclose()  # type: ignore[attr-defined]


async def _main(args: argparse.Namespace) -> None:
    configure_logging()
    configure_tracing("sentiment_agent")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_loop(
            interval_sec=args.interval_sec,
            lookback_minutes=args.lookback_minutes,
            max_per_cycle=args.max_per_cycle,
            stop=stop,
        )
    finally:
        log.info("sentiment.stop")


def main() -> None:
    parser = argparse.ArgumentParser(prog="sentiment_agent.main")
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=300,
        help="Cycle period.  Default 5 minutes.",
    )
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=360,
        help=(
            "How far back NewsAPI 'from=' clip.  Default 6h - longer than "
            "the cycle interval so ingestion delays on the news side don't "
            "cause us to miss articles.  The Redis dedup layer prevents "
            "re-scoring articles we've already processed."
        ),
    )
    parser.add_argument(
        "--max-per-cycle",
        type=int,
        default=3,
        help="Max articles to score per symbol per cycle (caps Anthropic spend).",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
