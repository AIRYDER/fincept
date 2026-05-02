"""OpenBB-backed read-only market data tools.

Exposes three pieces of public surface:

  * ``OpenBBQuoteTool``  — focused tool for ``equity.price.quote``.  Dual-path:
                            local OpenBB API first, then in-process ``openbb``
                            package as a fallback.
  * ``OpenBBCallTool``   — generic dispatcher.  Takes a path
                            (``/api/v1/...``) plus a ``params`` dict and
                            returns the normalised result list.  Lets the
                            dashboard add fundamentals, options, macro,
                            etc. without backend changes.
  * ``check_openbb_health()`` — fast reachability probe used by the
                            ``GET /research/openbb/health`` route + UI
                            status pill.

All helpers funnel through ``_resolve_openbb_url`` so .env / env-var /
default precedence stays consistent across endpoints.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from pydantic import Field, field_validator

from fincept_tools.errors import OpenBBUnavailable, ToolBackendError
from fincept_tools.protocol import BaseTool, ToolInput, ToolOutput
from fincept_tools.registry import register

QuoteLoader = Callable[[str, str], Awaitable[list[dict[str, object]]]]
GetJson = Callable[..., Awaitable[dict[str, object]]]
OPENBB_API_URL = "http://127.0.0.1:6900"
OPENBB_DATA_TIMEOUT_SEC = 15.0
OPENBB_HEALTH_TIMEOUT_SEC = 2.0

# Keep the dispatcher hard-locked to the OpenBB read-only namespace.
# A bare ``/api/v1/...`` is enough to prevent path traversal; the
# pattern further blocks query strings or fragments smuggled into
# ``path`` (those belong in ``params``).
_ALLOWED_PATH_PATTERN = r"^/api/v1/[A-Za-z0-9._/-]+$"


class OpenBBQuoteInput(ToolInput):
    symbol: str = Field(min_length=1, max_length=32)
    provider: str = Field(default="yfinance", min_length=1, max_length=64)


class OpenBBQuoteOutput(ToolOutput):
    provider: str = "yfinance"
    results: list[dict[str, object]] = Field(default_factory=list)


def _to_plain_dict(item: Any) -> dict[str, object]:
    if isinstance(item, dict):
        return {str(key): value for key, value in item.items()}
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        if isinstance(dumped, dict):
            return {str(key): value for key, value in dumped.items()}
    if hasattr(item, "dict"):
        dumped = item.dict()
        if isinstance(dumped, dict):
            return {str(key): value for key, value in dumped.items()}
    if hasattr(item, "__dict__"):
        return {
            str(key): value
            for key, value in vars(item).items()
            if not str(key).startswith("_")
        }
    raise ToolBackendError("OpenBB quote returned an unsupported row shape")


def _normalize_openbb_result(result: Any) -> list[dict[str, object]]:
    if hasattr(result, "to_df"):
        dataframe = result.to_df()
        if hasattr(dataframe, "to_dict"):
            records = dataframe.to_dict(orient="records")
            if isinstance(records, list):
                return [_to_plain_dict(row) for row in records]

    rows = getattr(result, "results", result)
    if rows is result and isinstance(result, dict) and "results" in result:
        rows = result["results"]
    if isinstance(rows, dict):
        return [_to_plain_dict(rows)]
    if isinstance(rows, list | tuple):
        return [_to_plain_dict(row) for row in rows]
    return [_to_plain_dict(rows)]


def _read_openbb_api_url_from_dotenv() -> str | None:
    search_roots = [Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents]
    seen: set[Path] = set()
    for parent in search_roots:
        if parent in seen:
            continue
        seen.add(parent)
        env_path = parent / ".env"
        if not env_path.is_file():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key.strip() == "OPENBB_API_URL":
                    cleaned = value.strip().strip('"').strip("'")
                    return cleaned or None
        except OSError:
            return None
    return None


async def _load_openbb_quote(symbol: str, provider: str) -> list[dict[str, object]]:
    def load() -> list[dict[str, object]]:
        try:
            from openbb import obb
        except ImportError as exc:
            raise OpenBBUnavailable(
                "Install the openbb package before using OpenBB tools."
            ) from exc

        try:
            result = obb.equity.price.quote(symbol=symbol, provider=provider)
        except AttributeError as exc:
            raise ToolBackendError(
                "Installed OpenBB package does not expose obb.equity.price.quote."
            ) from exc
        return _normalize_openbb_result(result)

    return await asyncio.to_thread(load)


async def _get_json(
    url: str,
    params: dict[str, str],
    *,
    request_timeout: float = OPENBB_DATA_TIMEOUT_SEC,
) -> dict[str, object]:
    def load() -> dict[str, object]:
        query = parse.urlencode(params)
        full_url = f"{url}?{query}" if query else url
        parsed_url = parse.urlparse(url)
        if parsed_url.scheme == "http" and parsed_url.hostname not in {"127.0.0.1", "localhost"}:
            raise ToolBackendError("OpenBB API HTTP URL must be local")
        if parsed_url.scheme not in {"http", "https"}:
            raise ToolBackendError("OpenBB API URL must use HTTP or HTTPS")
        req = request.Request(  # noqa: S310
            full_url,
            headers={"Accept": "application/json", "User-Agent": "FinceptTerminal/0.1"},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=request_timeout) as response:  # noqa: S310
                response_body = response.read().decode("utf-8")
        except error.URLError as exc:
            raise OpenBBUnavailable(
                "OpenBB API is not reachable. Start the OpenBB API backend or install openbb in the Fincept environment."
            ) from exc
        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise ToolBackendError("OpenBB API returned a non-object JSON response")
        return parsed

    return await asyncio.to_thread(load)


def _resolve_openbb_url() -> str:
    """Single source of truth for the OpenBB API base URL.

    Resolution order: live env var → ``.env`` walk → built-in default.
    Trailing slashes are stripped so callers can append paths cleanly.
    """
    return (
        os.getenv("OPENBB_API_URL")
        or _read_openbb_api_url_from_dotenv()
        or OPENBB_API_URL
    ).rstrip("/")


async def _load_openbb_api_quote(
    symbol: str,
    provider: str,
    get_json: GetJson = _get_json,
) -> list[dict[str, object]]:
    base_url = _resolve_openbb_url()
    response = await get_json(
        f"{base_url}/api/v1/equity/price/quote",
        {"symbol": symbol, "provider": provider},
    )
    return _normalize_openbb_result(response)


class OpenBBQuoteTool(BaseTool):
    name = "research.openbb_quote"
    description = "Read-only OpenBB quote lookup for a symbol and provider."
    input_model = OpenBBQuoteInput
    output_model = OpenBBQuoteOutput

    def __init__(
        self,
        quote_loader: QuoteLoader | None = None,
        get_json: GetJson = _get_json,
    ) -> None:
        self._quote_loader = quote_loader
        self._get_json = get_json

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, OpenBBQuoteInput)
        symbol = payload.symbol.strip().upper()
        provider = payload.provider.strip()
        if self._quote_loader is not None:
            rows = await self._quote_loader(symbol, provider)
        else:
            try:
                rows = await _load_openbb_api_quote(symbol, provider, self._get_json)
            except OpenBBUnavailable as api_exc:
                try:
                    rows = await _load_openbb_quote(symbol, provider)
                except OpenBBUnavailable as package_exc:
                    raise OpenBBUnavailable(
                        f"{api_exc} Also, {package_exc}"
                    ) from package_exc
        return OpenBBQuoteOutput(provider=provider, results=rows)


register(OpenBBQuoteTool())


# --------------------------------------------------------------------------- #
# Generic dispatcher                                                          #
# --------------------------------------------------------------------------- #
#
# The dispatcher exists so the dashboard can talk to *any* OpenBB API
# endpoint without us writing a bespoke Pydantic model + route per
# endpoint.  The trade-off vs. the focused ``OpenBBQuoteTool`` is that
# the dispatcher does no validation of the path's semantic meaning; it
# only enforces that:
#
#   1. The path is a relative ``/api/v1/...`` URL (no schemes, no
#      cross-host injection, no traversal characters).
#   2. The base URL is the operator-configured local OpenBB API.
#
# Because OpenBB's local API is itself read-only and has no destructive
# mutations, this is a safe surface to expose.  If/when OpenBB adds
# write endpoints, gate this through an allowlist.


class OpenBBCallInput(ToolInput):
    """Generic OpenBB API call payload.

    ``path`` is the part of the URL **after** the OpenBB base
    (e.g. ``/api/v1/equity/fundamental/income``).  ``params`` are the
    query string arguments and must already be string-coerced.
    """

    path: str = Field(min_length=1, max_length=256, pattern=_ALLOWED_PATH_PATTERN)
    params: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        # The base regex allows dots (for path segments like ``v1.0``)
        # and slashes (for nested paths), which together unfortunately
        # admit ``..`` traversal segments.  Belt-and-braces: explicitly
        # ban any ``..`` substring so the dispatcher can never escape
        # the OpenBB API namespace.
        if ".." in value:
            raise ValueError("path must not contain '..' segments")
        return value


class OpenBBCallOutput(ToolOutput):
    path: str = ""
    results: list[dict[str, object]] = Field(default_factory=list)
    provider: str | None = None


class OpenBBCallTool(BaseTool):
    name = "research.openbb_call"
    description = (
        "Generic OpenBB API dispatcher.  Calls an /api/v1/... path on the "
        "configured local OpenBB backend and returns the normalised result list."
    )
    input_model = OpenBBCallInput
    output_model = OpenBBCallOutput

    def __init__(self, get_json: GetJson = _get_json) -> None:
        self._get_json = get_json

    async def _run(self, payload: ToolInput) -> ToolOutput:
        assert isinstance(payload, OpenBBCallInput)
        base_url = _resolve_openbb_url()
        url = f"{base_url}{payload.path}"
        response = await self._get_json(url, payload.params)
        rows = _normalize_openbb_result(response)
        # Surface provider when the OpenBB response advertises it so
        # callers can show "data via fmp" badges without re-parsing.
        provider: str | None = None
        if isinstance(response, dict):
            raw_provider = response.get("provider")
            if isinstance(raw_provider, str):
                provider = raw_provider
        return OpenBBCallOutput(path=payload.path, results=rows, provider=provider)


register(OpenBBCallTool())


# --------------------------------------------------------------------------- #
# Health probe                                                                #
# --------------------------------------------------------------------------- #


async def check_openbb_health(
    *,
    get_json: GetJson = _get_json,
) -> dict[str, object]:
    """Probe the local OpenBB API for reachability.

    The probe hits ``/openapi.json`` because every FastAPI service
    (which OpenBB API is) exposes it cheaply with no upstream
    provider calls.  Any successful 2xx response — even if the body
    isn't a dict — is treated as alive; the only "down" signals are
    connection refused / DNS failures / timeouts, which surface as
    :class:`OpenBBUnavailable`.

    Returns a JSON-serialisable dict so the route handler can emit
    it directly with no further translation.
    """
    base_url = _resolve_openbb_url()
    started = time.perf_counter()
    try:
        await get_json(
            f"{base_url}/openapi.json",
            {},
            request_timeout=OPENBB_HEALTH_TIMEOUT_SEC,
        )
    except OpenBBUnavailable as exc:
        return {
            "ok": False,
            "url": base_url,
            "error_type": "OpenBBUnavailable",
            "error": str(exc),
        }
    except ToolBackendError as exc:
        # Server answered, but the body wasn't a JSON object -- still
        # alive, just unusual.  Return ok=True with a warning so the
        # UI can surface "reachable but unexpected response".
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "url": base_url,
            "latency_ms": latency_ms,
            "warning": str(exc),
        }
    latency_ms = int((time.perf_counter() - started) * 1000)
    return {"ok": True, "url": base_url, "latency_ms": latency_ms}
