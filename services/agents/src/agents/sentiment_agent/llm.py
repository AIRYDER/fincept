"""
agents.sentiment_agent.llm - LLM-driven sentiment scoring.

Two providers supported, both via plain httpx (no SDK dependency):

  - **anthropic**: ``https://api.anthropic.com/v1/messages``
                   Default model from ``ANTHROPIC_MODEL`` env var
                   (``claude-haiku-4-5`` if unset).
  - **openai**:    ``https://api.openai.com/v1/chat/completions``
                   Default model from ``OPENAI_MODEL`` env var
                   (``gpt-4o-mini`` if unset).

The prompt and JSON parser are provider-agnostic; only the HTTP call
differs.  ``score_article`` is the public entry point and dispatches
to the requested provider.

Provider selection is normally handled by ``pick_provider`` at agent
startup, which prefers Anthropic if an API key is set, falls back to
OpenAI, and returns ``None`` if neither is configured.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal

import httpx

Provider = Literal["anthropic", "openai"]

DEFAULT_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
DEFAULT_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# Strict JSON-only prompt.  Instructed to return ONLY the JSON object so
# we don't have to parse around prose.  ``score`` and ``confidence`` are
# floats in [-1, 1] and [0, 1]; ``event_type`` is a short tag.  We keep
# the prompt deliberately compact - longer prompts burn tokens for no
# accuracy improvement on this scale of task.
PROMPT_TEMPLATE = """\
You are a financial news sentiment scorer.

Read the article below and rate its likely 30-minute price impact on \
{symbol}.

Return ONLY a JSON object with these keys:
- "score": float in [-1, 1]. -1 = strongly bearish, 0 = neutral, +1 = strongly bullish.
- "confidence": float in [0, 1]. How confident is the rating (0 = unsure, 1 = certain).
- "event_type": short string. One of: "regulatory", "macro", "product", "security", "market_move", "partnership", "general".
- "rationale": one short sentence.

Article:
- title: {title}
- description: {description}
- source: {source}

Respond with the JSON object only - no preface, no code fences, no commentary.
"""


@dataclass(frozen=True)
class SentimentScore:
    """LLM-scored sentiment for one article."""

    score: float
    confidence: float
    event_type: str
    rationale: str


def pick_provider(
    *,
    anthropic_key: str | None,
    openai_key: str | None,
    preference: str = "auto",
) -> tuple[Provider, str] | None:
    """Pick a single provider + key for one-shot use (smoke tests).

    Returns the FIRST provider in preference order whose key is set,
    without any fallback handling.  For long-running services use
    :func:`pick_providers` + :class:`LLMRouter` instead.
    """
    options = pick_providers(
        anthropic_key=anthropic_key,
        openai_key=openai_key,
        preference=preference,
    )
    return options[0] if options else None


def pick_providers(
    *,
    anthropic_key: str | None,
    openai_key: str | None,
    preference: str = "auto",
) -> list[tuple[Provider, str]]:
    """Return an ordered list of usable (provider, key) pairs.

    ``preference``:
      - ``"auto"``      try Anthropic first, then OpenAI.
      - ``"anthropic"`` Anthropic only (empty list if no key).
      - ``"openai"``    OpenAI only (empty list if no key).

    The agent feeds this into :class:`LLMRouter` so a provider that
    returns a billing / auth failure mid-session is automatically
    skipped on subsequent calls.
    """
    pref = preference.lower()
    if pref == "anthropic":
        return [("anthropic", anthropic_key)] if anthropic_key else []
    if pref == "openai":
        return [("openai", openai_key)] if openai_key else []
    # auto: Anthropic first, then OpenAI (Anthropic Haiku is cheaper).
    out: list[tuple[Provider, str]] = []
    if anthropic_key:
        out.append(("anthropic", anthropic_key))
    if openai_key:
        out.append(("openai", openai_key))
    return out


def is_unrecoverable_provider_error(exc: BaseException) -> bool:
    """True if the exception means the current provider is hopeless.

    These errors will not transiently resolve - retrying with the same
    key will keep failing - so we mark the provider exhausted and try
    the next one.  Examples:

      - 401 / 403 (bad key, revoked, no permission)
      - 400 + body says "credit"/"billing"/"insufficient" (out of funds)
      - 429 + "insufficient_quota" (OpenAI billing)

    Transient errors (timeouts, 5xx, plain rate limits) are NOT in
    this set - those propagate so the caller can retry the same
    provider on the next cycle.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status = exc.response.status_code
    if status in (401, 403):
        return True
    body_text = ""
    try:
        body_text = exc.response.text
    except Exception:
        return False
    lowered = body_text.lower()
    keywords = ("credit balance", "billing", "insufficient", "exceeded your")
    if status == 400 and any(k in lowered for k in keywords):
        return True
    if status == 429 and ("insufficient_quota" in lowered or "billing" in lowered):
        return True
    return False


class LLMRouter:
    """Stateful provider selector with per-call billing/auth fallback.

    Construct with the ordered list from :func:`pick_providers`.  Each
    call to :meth:`score` tries the first non-exhausted provider; if
    that provider raises an unrecoverable error
    (:func:`is_unrecoverable_provider_error`), the router marks it
    exhausted *for the lifetime of this router* and retries with the
    next provider.

    Once every provider is exhausted, :meth:`score` returns ``None``
    and the agent should log + skip the article.
    """

    def __init__(self, providers: list[tuple[Provider, str]]) -> None:
        self._providers = providers
        self._exhausted: set[Provider] = set()
        self._exhaustion_reasons: dict[Provider, str] = {}

    @property
    def current(self) -> tuple[Provider, str] | None:
        """The next provider to try, or ``None`` if all are exhausted."""
        for provider, key in self._providers:
            if provider not in self._exhausted:
                return (provider, key)
        return None

    @property
    def has_capacity(self) -> bool:
        return self.current is not None

    def configured_providers(self) -> list[Provider]:
        return [p for p, _ in self._providers]

    def exhausted_providers(self) -> dict[Provider, str]:
        """Map of exhausted providers to the error message that killed them."""
        return dict(self._exhaustion_reasons)

    def mark_exhausted(self, provider: Provider, reason: str) -> None:
        self._exhausted.add(provider)
        self._exhaustion_reasons.setdefault(provider, reason)

    async def score(
        self,
        client: httpx.AsyncClient,
        *,
        symbol: str,
        title: str,
        description: str,
        source: str,
        max_tokens: int = 200,
    ) -> tuple[SentimentScore, Provider] | None:
        """Score with fallback.  Returns (score, provider_used) or None.

        ``None`` is returned in two cases:
          - parse failure on the LLM response (transient; try again later)
          - all providers exhausted (give up for this run)

        Network errors that aren't billing/auth propagate so the agent
        treats them as transient and retries on the next cycle.
        """
        while True:
            cur = self.current
            if cur is None:
                return None
            provider, key = cur
            try:
                result = await score_article(
                    client,
                    api_key=key,
                    provider=provider,
                    symbol=symbol,
                    title=title,
                    description=description,
                    source=source,
                    max_tokens=max_tokens,
                )
            except httpx.HTTPStatusError as exc:
                if is_unrecoverable_provider_error(exc):
                    self.mark_exhausted(provider, str(exc)[:200])
                    continue  # try next provider
                raise
            if result is None:
                return None
            return (result, provider)


async def score_article(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    symbol: str,
    title: str,
    description: str,
    source: str,
    provider: Provider = "anthropic",
    model: str | None = None,
    max_tokens: int = 200,
) -> SentimentScore | None:
    """Score one article via the chosen LLM provider.  Returns None on parse failure.

    Network errors propagate as exceptions (caller handles retry/backoff).
    Parse failures return ``None`` so one bad response doesn't poison
    a polling cycle.
    """
    prompt = PROMPT_TEMPLATE.format(
        symbol=symbol,
        title=title or "(no title)",
        description=description or "(no description)",
        source=source or "(no source)",
    )
    if provider == "anthropic":
        text = await _call_anthropic(
            client,
            api_key=api_key,
            prompt=prompt,
            model=model or DEFAULT_ANTHROPIC_MODEL,
            max_tokens=max_tokens,
        )
    elif provider == "openai":
        text = await _call_openai(
            client,
            api_key=api_key,
            prompt=prompt,
            model=model or DEFAULT_OPENAI_MODEL,
            max_tokens=max_tokens,
        )
    else:
        raise ValueError(f"unknown provider: {provider!r}")
    if not text:
        return None
    return _parse_json_score(text)


async def _call_anthropic(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    prompt: str,
    model: str,
    max_tokens: int,
) -> str:
    """POST to Anthropic Messages API; return the assistant text or raise."""
    resp = await client.post(
        ANTHROPIC_MESSAGES_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30.0,
    )
    _raise_with_body(resp, "anthropic")
    body = resp.json()
    pieces = body.get("content") or []
    chunks = [p.get("text", "") for p in pieces if p.get("type") == "text"]
    return "".join(chunks).strip()


async def _call_openai(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    prompt: str,
    model: str,
    max_tokens: int,
) -> str:
    """POST to OpenAI Chat Completions API; return the assistant text or raise."""
    resp = await client.post(
        OPENAI_CHAT_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            # Hard-coded JSON output mode where OpenAI supports it - cuts
            # the rate of code-fenced or prose-prefixed responses.
            "response_format": {"type": "json_object"},
        },
        timeout=30.0,
    )
    _raise_with_body(resp, "openai")
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0].get("message") or {}).get("content") or ""
    return str(msg).strip()


def _raise_with_body(resp: httpx.Response, provider_label: str) -> None:
    """Raise an HTTPStatusError that includes the provider's error body.

    Without this, a 400 from Anthropic / OpenAI shows only the URL,
    hiding the actual reason (bad model, no credits, malformed JSON).
    """
    if resp.status_code < 400:
        return
    try:
        err_body = resp.json()
    except ValueError:
        err_body = {"raw": resp.text[:400]}
    raise httpx.HTTPStatusError(
        f"{provider_label} {resp.status_code}: {err_body}",
        request=resp.request,
        response=resp,
    )


def _parse_json_score(text: str) -> SentimentScore | None:
    """Best-effort JSON parse.  Accepts code-fenced JSON too."""
    candidate = text
    # Strip markdown code fences if the model ignored "no code fences".
    if candidate.startswith("```"):
        # Drop everything up to the first newline.
        candidate = candidate.split("\n", 1)[1] if "\n" in candidate else candidate
        # Drop trailing fence.
        if candidate.endswith("```"):
            candidate = candidate[:-3]
    candidate = candidate.strip()

    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    try:
        score = float(obj.get("score", 0.0))
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None

    score = max(-1.0, min(1.0, score))
    confidence = max(0.0, min(1.0, confidence))

    event_type = str(obj.get("event_type") or "general").strip() or "general"
    rationale = str(obj.get("rationale") or "").strip()

    return SentimentScore(
        score=score,
        confidence=confidence,
        event_type=event_type,
        rationale=rationale,
    )
