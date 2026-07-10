"""Tests for :mod:`fincept_core.http`."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from fincept_core.http import (
    _backoff_sleep_s,
    build_http_client,
    http_request,
)


class _MockTransport(httpx.AsyncBaseTransport):
    """A transport that returns a scripted sequence of responses/errors."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        item = self.script_pop()
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item, request=request, text=f"status {item}")
        if isinstance(item, dict):
            return httpx.Response(200, request=request, json=item)
        raise AssertionError(f"unscripted item: {item!r}")

    def script_pop(self) -> Any:
        if not self._script:
            raise AssertionError("transport out of scripted responses")
        return self._script.pop(0)


@pytest.mark.asyncio
async def test_http_request_succeeds_first_try() -> None:
    transport = _MockTransport([{"ok": True}])
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await http_request(client, "GET", "http://x/y", max_attempts=3)
    assert resp.status_code == 200
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_http_request_retries_on_5xx_then_succeeds() -> None:
    transport = _MockTransport([503, 502, {"ok": True}])
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await http_request(client, "GET", "http://x/y", max_attempts=3)
    assert resp.status_code == 200
    assert transport.calls == 3


@pytest.mark.asyncio
async def test_http_request_retries_on_connect_error() -> None:
    transport = _MockTransport([httpx.ConnectError("boom"), {"ok": True}])
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await http_request(client, "GET", "http://x/y", max_attempts=3)
    assert resp.status_code == 200
    assert transport.calls == 2


@pytest.mark.asyncio
async def test_http_request_does_not_retry_4xx() -> None:
    transport = _MockTransport([400])
    async with httpx.AsyncClient(transport=transport) as client:
        resp = await http_request(client, "GET", "http://x/y", max_attempts=3)
    assert resp.status_code == 400
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_http_request_raises_after_max_attempts() -> None:
    transport = _MockTransport([503, 503, 503, 503])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await http_request(client, "GET", "http://x/y", max_attempts=3)
    assert transport.calls == 3


@pytest.mark.asyncio
async def test_http_request_propagates_request_id_header() -> None:
    captured: dict[str, str] = {}

    class _Capturing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.headers))
            return httpx.Response(200, request=request, json={"ok": True})

    transport = _Capturing()
    async with httpx.AsyncClient(transport=transport) as client:
        await http_request(client, "GET", "http://x/y", request_id="rid-123", max_attempts=1)
    assert captured.get("x-request-id") == "rid-123"


def test_backoff_grows_then_caps() -> None:
    sleeps = [_backoff_sleep_s(a) for a in (1, 2, 3, 4, 5, 10)]
    # All sleeps are positive and capped at MAX_SLEEP + JITTER.
    for s in sleeps:
        assert 0.0 < s <= 8.0 + 0.25 + 1e-9
    # Attempts 1..2 should usually be smaller than attempts 5..10 (cap).
    assert max(sleeps[:2]) < max(sleeps[4:])


def test_build_http_client_applies_timeouts() -> None:
    client = build_http_client(base_url="http://example.com", timeout_s=2.5)
    assert client.timeout.read == 2.5
    assert client.timeout.connect == 5.0
    # headers default is an empty dict; not None.
    assert client.headers is not None
