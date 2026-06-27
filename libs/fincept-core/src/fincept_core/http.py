"""Centralised HTTP client + retry helper.

Every outbound HTTP call should go through :func:`http_request` so we have
one place to add: request-id header propagation, structured logging on
failure, and capped-exponential-backoff retry on transient errors.

Why a wrapper instead of decorator?  Callers need to pass an
``httpx.AsyncClient`` (constructed by the caller with the right
``base_url``, ``auth``, etc.); a wrapper is the smallest change at every
existing call site.  Lifting the client itself would be a bigger
refactor and is not in scope for this audit.

We do NOT retry on 4xx (other than 408 / 425 / 429) — retrying a bad
request is a waste and a billing-bait for LLM-style APIs.

The module is dependency-free beyond :mod:`httpx` (already a project
dependency) and the standard library.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from fincept_core.logging import get_logger

log = get_logger(__name__)

#: HTTP statuses that are safe to retry.  4xx other than these indicate
#: the caller is at fault (bad request, missing auth, etc.).
RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Default retry budget.  3 attempts at base 0.5s with factor 2 and
#: 0.25s jitter gives worst-case delay 0.5 + 0.25 = 0.75s, 1.5 + 0.25s;
#: total wall time stays under 3s for typical 4xx/5xx blips.
DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_BASE_SLEEP_S: float = 0.5
DEFAULT_MAX_SLEEP_S: float = 8.0
DEFAULT_JITTER_S: float = 0.25

#: Default timeouts.  ``read=10s`` covers most public APIs without
#: making the dashboard feel slow on degraded connections.
DEFAULT_TIMEOUT_S: float = 10.0
DEFAULT_CONNECT_TIMEOUT_S: float = 5.0


def build_http_client(
    *,
    base_url: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """Construct an ``httpx.AsyncClient`` with our standard timeouts + headers.

    Use this in every call site that needs a long-lived client (Alpaca
    OMS, news sync).  Single-shot ``client.get(...)`` calls in
    scripts/tests don't need this — they can keep using
    ``httpx.AsyncClient(timeout=...)`` inline.
    """
    timeout = httpx.Timeout(timeout_s, connect=connect_timeout_s)
    return httpx.AsyncClient(
        base_url=base_url or "",
        timeout=timeout,
        headers=headers or {},
    )


def _backoff_sleep_s(attempt: int) -> float:
    """Return capped-exponential-backoff sleep for the *attempt* (1-based)."""
    base = min(DEFAULT_BASE_SLEEP_S * (2 ** (attempt - 1)), DEFAULT_MAX_SLEEP_S)
    # S311: random.uniform is fine for jitter; we don't need crypto-grade
    # randomness for "wait a few extra ms before retrying".
    return float(base + random.uniform(0.0, DEFAULT_JITTER_S))  # noqa: S311


async def http_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    request_id: str | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Issue an HTTP request with capped-exponential-backoff retry.

    Retries on:

    * :class:`httpx.ConnectError`, :class:`httpx.ReadTimeout`,
      :class:`httpx.WriteTimeout`, :class:`httpx.PoolTimeout` (network
      layer).
    * 408 / 425 / 429 / 5xx (server layer) — see
      :data:`RETRYABLE_STATUSES`.

    Does **not** retry on 4xx other than the above (caller is at fault).

    Parameters
    ----------
    client
        Pre-built :class:`httpx.AsyncClient`.  Use :func:`build_http_client`
        for the standard config.
    method, url, kwargs
        Forwarded to :meth:`httpx.AsyncClient.request`.
    max_attempts
        Total number of attempts including the first try.  ``1`` disables
        retry (useful for tests).
    request_id
        Optional correlation id to log on every attempt; also stamped on
        the outbound ``X-Request-ID`` header when present.
    """
    last_exc: Exception | None = None
    headers: dict[str, str] = dict(kwargs.pop("headers", {}) or {})
    if request_id is not None:
        headers.setdefault("X-Request-ID", request_id)
    kwargs["headers"] = headers

    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ) as exc:
            last_exc = exc
        else:
            if resp.status_code not in RETRYABLE_STATUSES:
                return resp
            last_exc = httpx.HTTPStatusError(
                f"transient {resp.status_code} from {method} {url}",
                request=resp.request,
                response=resp,
            )

        if attempt < max_attempts:
            sleep_s = _backoff_sleep_s(attempt)
            log.warning(
                "http.retry",
                method=method,
                url=url,
                attempt=attempt,
                sleep_s=round(sleep_s, 3),
                error=str(last_exc),
            )
            await asyncio.sleep(sleep_s)

    assert last_exc is not None  # unreachable when max_attempts >= 1
    raise last_exc
