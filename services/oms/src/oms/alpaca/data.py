"""
oms.alpaca.data — thin client for Alpaca's market-data API.

This is a sibling to ``AlpacaClient`` (which targets the trading API at
``paper-api.alpaca.markets``).  The data API lives under a different
base URL (``data.alpaca.markets``) but uses the same auth headers, so
we keep it as a small separate class rather than parameterising
``AlpacaClient``.  Two endpoints are enough for the news feature:

  - GET /v1beta1/news      recent article metadata + mentioned symbols
  - GET /v2/stocks/bars    1-min/5-min bars for N symbols at once

Both are documented at https://docs.alpaca.markets/reference/.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

DATA_BASE_URL = "https://data.alpaca.markets"


class AlpacaDataError(Exception):
    def __init__(self, status_code: int, body: Mapping[str, Any] | str) -> None:
        super().__init__(f"Alpaca data error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class AlpacaDataClient:
    """Async REST client for Alpaca market-data endpoints."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        api_secret: str,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("AlpacaDataClient requires api_key and api_secret")
        self._http = http
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Accept": "application/json",
        }

    async def list_news(
        self,
        *,
        symbols: list[str] | None = None,
        limit: int = 50,
        start: str | None = None,
        end: str | None = None,
        page_token: str | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """GET /v1beta1/news.  ``start``/``end`` are ISO-8601 strings."""
        params: dict[str, str] = {
            "limit": str(limit),
            "sort": "desc",
            "include_content": "true" if include_content else "false",
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if page_token:
            params["page_token"] = page_token
        response = await self._http.get(
            f"{DATA_BASE_URL}/v1beta1/news",
            headers=self._headers,
            params=params,
        )
        return self._parse(response)

    async def list_bars(
        self,
        symbols: list[str],
        *,
        timeframe: str = "1Min",
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
        feed: str = "iex",
    ) -> dict[str, Any]:
        """GET /v2/stocks/bars for multiple symbols.  ``feed=iex`` is the
        free tier; ``sip`` requires a paid data subscription."""
        if not symbols:
            return {"bars": {}}
        params: dict[str, str] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "limit": str(limit),
            "feed": feed,
            "adjustment": "raw",
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        response = await self._http.get(
            f"{DATA_BASE_URL}/v2/stocks/bars",
            headers=self._headers,
            params=params,
        )
        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise AlpacaDataError(response.status_code, body)
        try:
            data = response.json()
        except ValueError as exc:
            raise AlpacaDataError(response.status_code, response.text) from exc
        if not isinstance(data, dict):
            return {"data": data}
        return data
