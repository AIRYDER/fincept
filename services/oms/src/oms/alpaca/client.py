"""
oms.alpaca.client - thin async REST wrapper for Alpaca.

We use only three endpoints in v1:

  - ``POST /v2/orders``               submit a new order
  - ``GET  /v2/orders/{order_id}``    check status of an order we placed
  - ``DELETE /v2/orders/{order_id}``  cancel an open order

Higher-level concepts (idempotency, retry, rate-limit handling) live in
``runtime.py``; this module is just the wire protocol.

Error model: every method raises ``AlpacaError`` for any non-2xx
response.  Callers catch and translate to OrderStatus.REJECTED with
the Alpaca error code in the audit payload.

We deliberately do NOT use the official ``alpaca-py`` SDK.  Three
endpoints * a few JSON fields each is small enough to keep flat with
``httpx``, and skipping the SDK avoids pulling in pandas, pydantic v1
back-compat shims, and a heavier dependency tree.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

import httpx

from fincept_core.schemas import (
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
)
from oms.alpaca.symbols import to_alpaca_symbol


class AlpacaError(Exception):
    """Raised when Alpaca returns a non-2xx response."""

    def __init__(self, status_code: int, body: Mapping[str, Any] | str) -> None:
        super().__init__(f"Alpaca error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


# Map our enum values to Alpaca's wire form.  Alpaca uses lowercase strings
# for everything; our enums are mostly already lowercase via StrEnum, but
# explicit is safer than relying on .value coincidence.
_SIDE_MAP: dict[Side, str] = {Side.BUY: "buy", Side.SELL: "sell"}
_ORDER_TYPE_MAP: dict[OrderType, str] = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP: "stop",
    OrderType.STOP_LIMIT: "stop_limit",
}
_TIF_MAP: dict[TimeInForce, str] = {
    TimeInForce.GTC: "gtc",
    TimeInForce.IOC: "ioc",
    TimeInForce.FOK: "fok",
    TimeInForce.DAY: "day",
}


class AlpacaClient:
    """Async REST client; constructed with an httpx.AsyncClient injected
    for testability.  Production wiring is in ``main.py``."""

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        api_key: str,
        api_secret: str,
    ) -> None:
        if not api_key or not api_secret:
            raise ValueError("AlpacaClient requires both api_key and api_secret")
        self._http = http
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def submit_order(self, intent: OrderIntent) -> dict[str, Any]:
        """POST /v2/orders.  Returns Alpaca's order JSON on success."""
        body = self._intent_to_body(intent)
        response = await self._http.post("/v2/orders", json=body, headers=self._headers)
        return cast(dict[str, Any], self._parse(response))

    async def get_order(self, alpaca_order_id: str) -> dict[str, Any]:
        """GET /v2/orders/{id}.  Returns the latest order JSON."""
        response = await self._http.get(f"/v2/orders/{alpaca_order_id}", headers=self._headers)
        return cast(dict[str, Any], self._parse(response))

    async def cancel_order(self, alpaca_order_id: str) -> None:
        """DELETE /v2/orders/{id}.  Returns 204 No Content on success."""
        response = await self._http.delete(f"/v2/orders/{alpaca_order_id}", headers=self._headers)
        if response.status_code not in (200, 204):
            self._raise(response)

    # ------------------------------------------------------------------
    # Read endpoints (account + positions + orders) used by the sync job
    # ------------------------------------------------------------------

    async def get_account(self) -> dict[str, Any]:
        """GET /v2/account - cash, equity, buying power, status."""
        response = await self._http.get("/v2/account", headers=self._headers)
        return cast(dict[str, Any], self._parse(response))

    async def list_positions(self) -> list[dict[str, Any]]:
        """GET /v2/positions - all open positions on the account."""
        response = await self._http.get("/v2/positions", headers=self._headers)
        data = self._parse(response)
        # Alpaca returns a JSON array directly; _parse always returns dict
        # by signature, so we have to call response.json() again here for
        # the list case.  Cleaner: parse manually.
        if isinstance(data, list):
            return cast(list[dict[str, Any]], data)
        # Fallback: if Alpaca ever wraps it (they don't today), look for
        # a common 'positions' key.
        return list(cast(dict[str, Any], data).get("positions", []))

    async def list_orders(
        self,
        *,
        status: str = "all",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /v2/orders - recent orders (default: all statuses, 100 rows)."""
        params = {"status": status, "limit": str(limit), "direction": "desc"}
        response = await self._http.get(
            "/v2/orders", headers=self._headers, params=params
        )
        data = self._parse(response)
        if isinstance(data, list):
            return cast(list[dict[str, Any]], data)
        return list(cast(dict[str, Any], data).get("orders", []))

    async def list_activities(
        self,
        *,
        activity_types: str = "FILL",
        date: str | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /v2/account/activities - fills / dividends / transfers.

        ``activity_types`` is a comma-separated set; ``FILL`` is the
        default because that's what drives realised P&L.  ``date`` is an
        ISO YYYY-MM-DD string; omitting it returns the most recent
        ``page_size`` activities across all days.
        """
        params: dict[str, str] = {
            "activity_types": activity_types,
            "page_size": str(page_size),
            "direction": "desc",
        }
        if date:
            params["date"] = date
        response = await self._http.get(
            "/v2/account/activities", headers=self._headers, params=params
        )
        data = self._parse(response)
        if isinstance(data, list):
            return cast(list[dict[str, Any]], data)
        return list(cast(dict[str, Any], data).get("activities", []))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _intent_to_body(intent: OrderIntent) -> dict[str, Any]:
        """Translate our OrderIntent to Alpaca's POST /v2/orders shape."""
        body: dict[str, Any] = {
            # Pass our order_id as Alpaca's client_order_id so we can
            # correlate without storing the alpaca-side UUID separately.
            "client_order_id": intent.order_id,
            "symbol": to_alpaca_symbol(intent.symbol),
            "side": _SIDE_MAP[intent.side],
            "type": _ORDER_TYPE_MAP[intent.order_type],
            "qty": _decimal_to_str(intent.quantity),
            "time_in_force": _TIF_MAP[intent.time_in_force],
        }
        if intent.limit_price is not None:
            body["limit_price"] = _decimal_to_str(intent.limit_price)
        if intent.stop_price is not None:
            body["stop_price"] = _decimal_to_str(intent.stop_price)
        return body

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        """Parse a 2xx response.  Returns dict (for single-object endpoints)
        or list (for /v2/positions, /v2/orders).  Raises AlpacaError on
        any non-2xx."""
        if response.status_code >= 400:
            AlpacaClient._raise(response)
        try:
            return response.json()
        except ValueError as exc:
            raise AlpacaError(response.status_code, response.text) from exc

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            body = response.text
        raise AlpacaError(response.status_code, body)


def _decimal_to_str(value: Decimal) -> str:
    """Format a Decimal for Alpaca's wire (it accepts strings).

    We strip trailing zeros to keep the wire compact and match the
    representation a human would write in the Alpaca dashboard.
    """
    text = format(value.normalize(), "f")
    # Decimal.normalize on integers like Decimal("1") returns "1E+0"; the
    # format spec "f" handles the int case correctly via the fallback.
    return text if "." in text or "E" not in text.upper() else format(value, "f")
