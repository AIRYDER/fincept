"""agents.sentiment_agent - news-driven sentiment signal source.

Polls NewsAPI for symbols in ``settings.UNIVERSE``, scores each fresh
article via Anthropic's Messages API, and emits a ``SentimentSignal``
to ``STREAM_SIG_SENT``.

Optional service: the agent skips startup if ``NEWSAPI_API_KEY`` or
``ANTHROPIC_API_KEY`` is missing, so the rest of the stack runs fine
without these credentials.
"""
