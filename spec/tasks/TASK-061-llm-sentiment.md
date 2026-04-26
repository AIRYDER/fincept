# TASK-061 · `llm_sentiment` agent (cutting-edge: news + filings → structured signals)

**Phase:** X · **Depends on:** TASK-005 (fincept-tools), TASK-030 (Agent base), TASK-060 (vector memory) · **Blocks:** TASK-064

## Goal

Pull news and SEC filings for the universe, run an LLM with structured-output schema to extract `(symbol, score, event_type)`, deduplicate via vector memory, and emit `SentimentSignal` events. Designed to run with either OpenAI or Anthropic; fail-safe to no signal if both unreachable.

## Why this matters for profit

Most retail strategies use lagging price-derived features. Real-time event extraction from primary sources (10-K/10-Q, BLS data, central bank releases) gives a 1-30 minute lead on sentiment-driven moves before mainstream feeds digest them. This is a real edge that disappears if implemented sloppily — schema-typed extraction with confidence is non-negotiable.

## Files to create

```
services/agents/src/agents/llm_sentiment/
├── __init__.py
├── main.py
├── fetchers.py        # news + EDGAR clients
├── extractor.py       # LLM with structured output
├── entity.py          # ticker / company resolution
└── prompts.py         # system + user prompts
```

## Contracts

### `fetchers.py`

```python
import asyncio, datetime as dt
from typing import AsyncIterator, NamedTuple
import httpx, feedparser

class Article(NamedTuple):
    source: str          # "news.<vendor>" or "edgar"
    url: str
    title: str
    body: str
    published_at: int    # ns
    raw_id: str

class NewsFeed:
    """Generic RSS/Atom feed reader for free news sources. Replace with paid API keys for prod."""

    SOURCES = [
        ("news.coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("news.cointelegraph", "https://cointelegraph.com/rss"),
        ("news.reuters", "https://feeds.reuters.com/reuters/businessNews"),
    ]

    async def stream(self, poll_seconds: int = 60) -> AsyncIterator[Article]:
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                for src, url in self.SOURCES:
                    try:
                        resp = await client.get(url)
                        feed = feedparser.parse(resp.content)
                        for e in feed.entries:
                            uid = e.get("id") or e.get("link")
                            if uid in seen:
                                continue
                            seen.add(uid)
                            ts = int(dt.datetime(*e.published_parsed[:6], tzinfo=dt.UTC).timestamp() * 1e9) \
                                if hasattr(e, "published_parsed") else int(dt.datetime.now(dt.UTC).timestamp() * 1e9)
                            yield Article(source=src, url=e.link, title=e.title,
                                          body=e.get("summary", ""), published_at=ts, raw_id=uid)
                    except Exception:
                        continue
                await asyncio.sleep(poll_seconds)

class EdgarFeed:
    """SEC EDGAR latest filings (free; respect rate limits per https://www.sec.gov/os/accessing-edgar-data)."""

    URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=10-K&output=atom"
    HEADERS = {"User-Agent": "fincept-terminal research@example.com"}

    async def stream(self, poll_seconds: int = 300) -> AsyncIterator[Article]:
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=10, headers=self.HEADERS) as client:
            while True:
                try:
                    resp = await client.get(self.URL)
                    feed = feedparser.parse(resp.content)
                    for e in feed.entries:
                        if e.id in seen:
                            continue
                        seen.add(e.id)
                        ts = int(dt.datetime(*e.updated_parsed[:6], tzinfo=dt.UTC).timestamp() * 1e9)
                        yield Article(source="edgar", url=e.link, title=e.title, body=e.summary,
                                      published_at=ts, raw_id=e.id)
                except Exception:
                    pass
                await asyncio.sleep(poll_seconds)
```

### `prompts.py`

```python
SYSTEM = """You extract structured trading-relevant signals from financial text.
Return ONLY valid JSON matching the schema. No prose. If no clear trading signal, return null."""

USER_TEMPLATE = """Article (source={source}, published={published}):
TITLE: {title}
BODY: {body}

Universe of tickers we trade: {universe}

Return JSON: {{
  "signals": [
    {{
      "symbol": "<ticker from universe or canonical crypto pair>",
      "score": <-1.0 to 1.0; -1=very bearish, +1=very bullish>,
      "confidence": <0.0 to 1.0>,
      "event_type": "earnings|guidance|m&a|regulatory|macro|protocol_event|hack|listing|delisting|other",
      "rationale": "<1 sentence>"
    }}
  ]
}}
If no relevant signal: {{"signals": []}}"""
```

### `extractor.py`

```python
import json
from typing import Any
from fincept_core.config import get_settings
from fincept_core.logging import get_logger
from .prompts import SYSTEM, USER_TEMPLATE
from .fetchers import Article

log = get_logger(__name__)

async def extract(article: Article, universe: list[str]) -> list[dict[str, Any]]:
    """Returns list of signal dicts conforming to the inline schema in prompts.USER_TEMPLATE."""
    settings = get_settings()
    msg_user = USER_TEMPLATE.format(
        source=article.source, published=article.published_at,
        title=article.title, body=article.body[:4000],   # truncate to control cost
        universe=", ".join(universe),
    )
    if settings.anthropic_api_key:
        return await _call_anthropic(SYSTEM, msg_user)
    if settings.openai_api_key:
        return await _call_openai(SYSTEM, msg_user)
    log.warning("llm.no_api_key")
    return []

async def _call_anthropic(system: str, user: str) -> list[dict[str, Any]]:
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    msg = await client.messages.create(
        model="claude-sonnet-4-5", max_tokens=1024, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = msg.content[0].text  # type: ignore[union-attr]
    return _safe_parse(text)

async def _call_openai(system: str, user: str) -> list[dict[str, Any]]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI()
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
    )
    text = resp.choices[0].message.content or "{}"
    return _safe_parse(text)

def _safe_parse(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
        sigs = data.get("signals", []) if isinstance(data, dict) else []
        return [s for s in sigs if isinstance(s, dict)]
    except json.JSONDecodeError:
        log.warning("llm.parse_failed", text=text[:200])
        return []
```

### `entity.py`

```python
def resolve(symbol_str: str, universe: list[str]) -> str | None:
    """Return canonical universe symbol or None."""
    s = symbol_str.upper().replace("$", "").strip()
    direct = {u.upper(): u for u in universe}
    if s in direct:
        return direct[s]
    # crypto: BTC matches BTC-USD when only one BTC pair
    candidates = [u for u in universe if u.upper().startswith(s + "-")]
    if len(candidates) == 1:
        return candidates[0]
    return None
```

### `main.py`

```python
import asyncio, hashlib
from typing import AsyncIterator
from redis.asyncio import Redis
from pydantic import BaseModel
from fincept_core.config import get_settings
from fincept_core.clock import now_ns
from fincept_core.logging import configure_logging, get_logger
from fincept_core.schemas import SentimentSignal
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_SENT
from ..base import Agent
from ..memory import VectorMemory     # from TASK-060
from .fetchers import NewsFeed, EdgarFeed
from .extractor import extract
from .entity import resolve

configure_logging()
log = get_logger(__name__)

class LLMSentiment(Agent):
    agent_id = "llm_sentiment.v1"

    def __init__(self, redis: Redis, memory: VectorMemory) -> None:
        self.redis = redis
        self.memory = memory

    async def setup(self) -> None:
        await self.memory.setup()

    async def teardown(self) -> None:
        pass

    async def run(self) -> AsyncIterator[BaseModel]:
        s = get_settings()
        feeds = [NewsFeed().stream(), EdgarFeed().stream()]
        async for article in _merge(*feeds):
            # dedup via vector memory: skip if cosine-similar article seen recently
            digest = hashlib.sha256(article.title.encode()).hexdigest()
            if await self.memory.seen(digest, article.title):
                continue
            await self.memory.remember(digest, article.title, ttl_s=86400)
            sigs = await extract(article, s.universe)
            for sig in sigs:
                sym = resolve(sig.get("symbol", ""), s.universe)
                if sym is None:
                    continue
                yield SentimentSignal(
                    agent_id=self.agent_id, symbol=sym, ts_event=article.published_at,
                    score=float(sig.get("score", 0.0)),
                    confidence=float(sig.get("confidence", 0.0)),
                    event_type=sig.get("event_type"),
                    source_url=article.url,
                    source_excerpt=(sig.get("rationale") or article.title)[:300],
                )

async def _merge(*aiters):
    queues: list[asyncio.Queue] = [asyncio.Queue() for _ in aiters]
    async def feed(it, q):
        async for x in it:
            await q.put(x)
    tasks = [asyncio.create_task(feed(a, q)) for a, q in zip(aiters, queues, strict=False)]
    try:
        while True:
            done, _ = await asyncio.wait([asyncio.create_task(q.get()) for q in queues], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                yield t.result()
    finally:
        for t in tasks:
            t.cancel()

async def run() -> None:
    s = get_settings()
    redis = Redis.from_url(s.redis_url)
    mem = VectorMemory(redis, namespace="llm_sentiment")
    agent = LLMSentiment(redis, mem)
    producer = Producer(redis)
    await agent.setup()
    try:
        async for sig in agent.run():
            await producer.publish(STREAM_SIG_SENT, sig)
            log.info("sentiment", symbol=sig.symbol, score=sig.score, event=sig.event_type)
    finally:
        await agent.teardown()
        await redis.aclose()

def main() -> None:
    asyncio.run(run())

if __name__ == "__main__":
    main()
```

## Tests (no live API calls)

```python
# tests/test_entity.py
from agents.llm_sentiment.entity import resolve

def test_resolve_direct():
    assert resolve("BTC-USD", ["BTC-USD", "ETH-USD"]) == "BTC-USD"

def test_resolve_partial_crypto():
    assert resolve("BTC", ["BTC-USD", "ETH-USD"]) == "BTC-USD"

def test_resolve_unknown():
    assert resolve("DOGE", ["BTC-USD"]) is None

# tests/test_extractor_parse.py
from agents.llm_sentiment.extractor import _safe_parse

def test_parse_valid():
    txt = '{"signals": [{"symbol": "BTC-USD", "score": 0.5, "confidence": 0.8, "event_type": "macro", "rationale": "x"}]}'
    out = _safe_parse(txt)
    assert len(out) == 1 and out[0]["symbol"] == "BTC-USD"

def test_parse_garbage():
    assert _safe_parse("not json at all") == []
```

## Out of scope

- Multi-language support — Phase X+1
- Fine-tuning a smaller model on extraction outputs — Phase X+1
- Cost-controlled batching — implement once API spend is observable

## Done when

- [ ] Files exist, tests green
- [ ] Manual: with `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` set, run `python -m agents.llm_sentiment.main` and observe ≥1 `sig.sentiment` event within 5 min
