"""
scripts/test_sentiment_pipeline.py - one-shot validation of the sentiment chain.

Fetches a single news article from NewsAPI and scores it with Anthropic
without going through the full sentiment_agent loop.  Use this before
the first ``.\\start.bat`` with a fresh ANTHROPIC_API_KEY to confirm
both API integrations work and to eyeball the LLM's output quality.

Usage::

  uv run python scripts/test_sentiment_pipeline.py --symbol BTC-USD

Cost: one NewsAPI request + one Anthropic Messages call (~$0.001 with
claude-haiku-4-5).  Does NOT publish to Redis - safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from agents.sentiment_agent.llm import LLMRouter, pick_providers
from agents.sentiment_agent.news import fetch_articles, query_for_symbol
from fincept_core.config import get_settings


async def smoke_test(symbol: str) -> int:
    settings = get_settings()
    if not settings.NEWSAPI_API_KEY:
        print("ERROR: FINCEPT_NEWSAPI_API_KEY not set in .env", file=sys.stderr)
        return 1
    providers = pick_providers(
        anthropic_key=settings.ANTHROPIC_API_KEY,
        openai_key=settings.OPENAI_API_KEY,
        preference=settings.LLM_PROVIDER,
    )
    if not providers:
        print(
            "ERROR: no LLM provider configured "
            "(set FINCEPT_ANTHROPIC_API_KEY or FINCEPT_OPENAI_API_KEY)",
            file=sys.stderr,
        )
        return 1
    router = LLMRouter(providers)
    print(f"      provider preference: {[p for p, _ in providers]}")

    query = query_for_symbol(symbol)
    if query is None:
        print(
            f"ERROR: no NewsAPI query mapped for {symbol}; "
            f"add it to agents.sentiment_agent.news.SYMBOL_QUERIES",
            file=sys.stderr,
        )
        return 1

    async with httpx.AsyncClient() as http:
        print(f"[1/2] fetching articles for {symbol} (q={query!r}) ...")
        try:
            articles = await fetch_articles(
                http,
                query=query,
                api_key=settings.NEWSAPI_API_KEY,
                lookback_minutes=60 * 24 * 7,  # 7 days for the smoke test
                page_size=5,
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            print(f"NewsAPI request FAILED: {exc}", file=sys.stderr)
            return 2
        if not articles:
            print(
                "no articles returned in the last 24h; "
                "either the universe symbol is quiet or NewsAPI is rate-limited.",
                file=sys.stderr,
            )
            return 3
        article = articles[0]
        print(f"      got {len(articles)} articles; scoring the most recent:")
        print(f"        title       : {article.title}")
        print(f"        source      : {article.source}")
        print(f"        url         : {article.url}")
        print(f"        published   : {article.published_at_unix}")
        print(f"        description : {article.description[:140]}...")
        print()

        print("[2/2] scoring via LLMRouter (with provider fallback) ...")
        try:
            scored = await router.score(
                http,
                symbol=symbol,
                title=article.title,
                description=article.description,
                source=article.source,
            )
        except httpx.HTTPError as exc:
            print(f"transient LLM error: {exc}", file=sys.stderr)
            return 4
        if scored is None:
            if not router.has_capacity:
                print(
                    "ERROR: every provider was marked exhausted (auth/billing).",
                    file=sys.stderr,
                )
                for prov, why in router.exhausted_providers().items():
                    print(f"  - {prov}: {why}", file=sys.stderr)
                return 6
            print(
                "LLM returned a malformed response that wouldn't parse as JSON. "
                "Try again - this is occasional.",
                file=sys.stderr,
            )
            return 5
        score, provider_used = scored
        print(f"      provider   : {provider_used}")
        print(f"      score      : {score.score:+.3f}")
        print(f"      confidence : {score.confidence:.3f}")
        print(f"      event_type : {score.event_type}")
        print(f"      rationale  : {score.rationale}")
        if router.exhausted_providers():
            print()
            print(
                "Note: the following providers were exhausted during this run "
                "(billing/auth failure) and skipped:"
            )
            for prov, why in router.exhausted_providers().items():
                print(f"  - {prov}: {why}")

    print()
    print("OK both APIs work. Run .\\stop.bat && .\\start.bat to enable the live agent.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="test_sentiment_pipeline")
    parser.add_argument(
        "--symbol",
        default="BTC-USD",
        help="Universe symbol to test (must be in agents.sentiment_agent.news.SYMBOL_QUERIES).",
    )
    args = parser.parse_args()
    return asyncio.run(smoke_test(args.symbol))


if __name__ == "__main__":
    sys.exit(main())
