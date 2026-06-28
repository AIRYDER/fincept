"""
Tests for vendor API adapters in quant_foundry.data_ingestion.

Tests verify:
- :func:`fetch_alpaca_bars` with a mocked httpx response (MockTransport).
- :func:`ingest_alpaca_equity_bars` with mocked API -> IngestionResult.
- :func:`fetch_fred_series` with a mocked httpx response.
- :func:`ingest_fred_macro` with mocked API -> IngestionResult.
- :func:`fetch_newsapi_articles` with a mocked httpx response.
- Env var fallback works for all three vendors.
- Missing API keys raise a clear ValueError.
- :func:`get_ingester` returns the right function for the new vendors.

HTTP is mocked with ``httpx.MockTransport`` injected by monkeypatching
``httpx.AsyncClient`` so the vendor modules (which construct their own
clients internally) use the transport without code changes.

Tests requiring numpy/polars use ``pytest.importorskip`` so they are skipped
in environments without those deps, following the convention in
``test_data_ingestion.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from datetime import UTC
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Path setup — scripts/ is not a package, so add it to sys.path for the
# synthetic bar generator used by the equity pipeline.
# ---------------------------------------------------------------------------
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

_DAY = dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Mock-transport helpers
# ---------------------------------------------------------------------------


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> httpx.MockTransport:
    """Monkeypatch ``httpx.AsyncClient`` to use a ``MockTransport``.

    Returns the transport so callers can inspect it if needed.  The patch
    injects ``transport=<MockTransport>`` into every ``AsyncClient``
    constructor call, so vendor modules that build their own clients
    internally are transparently mocked.
    """
    transport = httpx.MockTransport(handler)

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockAsyncClient)
    return transport


# ---------------------------------------------------------------------------
# Alpaca bars mock data
# ---------------------------------------------------------------------------


def _alpaca_bars_payload(symbol: str, n_days: int = 60) -> dict[str, Any]:
    """Build a deterministic Alpaca bars response for *symbol*."""
    bars: list[dict[str, Any]] = []
    base = dt.datetime(2024, 1, 1, tzinfo=UTC)
    for i in range(n_days):
        ts = (base + _DAY * i).isoformat().replace("+00:00", "Z")
        bars.append(
            {
                "t": ts,
                "o": 100.0 + i,
                "h": 101.0 + i,
                "l": 99.0 + i,
                "c": 100.5 + i,
                "v": 1000.0 + i,
            },
        )
    return {"bars": bars, "symbol": symbol, "next_page_token": None}


def _alpaca_handler(request: httpx.Request) -> httpx.Response:
    """Route Alpaca bar requests to the mock payload."""
    path = request.url.path
    if "/v2/stocks/" in path and path.endswith("/bars"):
        # path = /v2/stocks/AAPL/bars
        parts = path.split("/")
        symbol = parts[3]
        return httpx.Response(200, json=_alpaca_bars_payload(symbol))
    return httpx.Response(404, json={"error": f"unknown path {path}"})


# ---------------------------------------------------------------------------
# FRED mock data
# ---------------------------------------------------------------------------


def _fred_observations_payload(series_id: str, n: int = 40) -> dict[str, Any]:
    """Build a deterministic FRED observations response."""
    obs: list[dict[str, str]] = []
    for i in range(n):
        month = (i % 12) + 1
        year = 2020 + i // 12
        date = f"{year}-{month:02d}-01"
        obs.append({"date": date, "value": str(round(0.25 + i * 0.05, 4))})
    return {"observations": obs}


def _fred_handler(request: httpx.Request) -> httpx.Response:
    """Route FRED observation requests to the mock payload."""
    series_id = request.url.params.get("series_id", "UNKNOWN")
    return httpx.Response(200, json=_fred_observations_payload(series_id))


# ---------------------------------------------------------------------------
# NewsAPI mock data
# ---------------------------------------------------------------------------


def _newsapi_articles_payload(n: int = 40) -> dict[str, Any]:
    """Build a deterministic NewsAPI everything response.

    Events are spaced in pairs of consecutive days with 2-day gaps between
    pairs so the news label (subsequent event within 1 day) yields a mix of
    0.0 and 1.0 labels.
    """
    articles: list[dict[str, Any]] = []
    base = dt.datetime(2024, 1, 1, tzinfo=UTC)
    day = 0
    for i in range(n):
        # Pairs: (0,1), (3,4), (6,7), ... -> mix of within-1-day and not.
        if i % 2 == 0 and i > 0:
            day += 2  # 2-day gap between pairs
        ts = (base + _DAY * day).isoformat().replace("+00:00", "Z")
        articles.append(
            {
                "source": {"id": None, "name": "Reuters"},
                "author": "test",
                "title": f"Stock market news day {i} surge profit growth",
                "description": f"Details about market day {i}. Strong outlook.",
                "url": f"https://example.com/news/{i}",
                "urlToImage": None,
                "publishedAt": ts,
                "content": f"Content for article {i}.",
            },
        )
        day += 1
    return {"status": "ok", "totalResults": n, "articles": articles}


def _newsapi_handler(request: httpx.Request) -> httpx.Response:
    """Route NewsAPI everything requests to the mock payload."""
    return httpx.Response(200, json=_newsapi_articles_payload())


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


def test_vendor_modules_importable() -> None:
    """The vendor API modules must be importable without numpy/polars."""
    from quant_foundry.data_ingestion import (
        fetch_alpaca_bars,
        fetch_fred_series,
        fetch_newsapi_articles,
        ingest_alpaca_equity_bars,
        ingest_fred_macro,
        ingest_newsapi_events,
    )

    assert callable(fetch_alpaca_bars)
    assert callable(ingest_alpaca_equity_bars)
    assert callable(fetch_fred_series)
    assert callable(ingest_fred_macro)
    assert callable(fetch_newsapi_articles)
    assert callable(ingest_newsapi_events)


def test_vendor_modules_no_module_level_heavy_deps() -> None:
    """httpx must NOT be imported at module level (lazy imports)."""
    import quant_foundry.data_ingestion.alpaca_bars as ab
    import quant_foundry.data_ingestion.fred_macro as fm
    import quant_foundry.data_ingestion.news_vendor as nv

    for mod in (ab, fm, nv):
        assert not hasattr(mod, "httpx"), f"{mod.__name__}: httpx at module level"
        assert not hasattr(mod, "np"), f"{mod.__name__}: numpy at module level"
        assert not hasattr(mod, "pl"), f"{mod.__name__}: polars at module level"


# ---------------------------------------------------------------------------
# Vendor registry
# ---------------------------------------------------------------------------


def test_get_ingester_alpaca() -> None:
    """get_ingester must return ingest_alpaca_equity_bars for alpaca."""
    from quant_foundry.data_ingestion import (
        get_ingester,
        ingest_alpaca_equity_bars,
    )

    assert get_ingester("alpaca_equity_bars") is ingest_alpaca_equity_bars


def test_get_ingester_fred() -> None:
    """get_ingester must return ingest_fred_macro for fred."""
    from quant_foundry.data_ingestion import get_ingester, ingest_fred_macro

    assert get_ingester("fred_macro") is ingest_fred_macro


def test_get_ingester_newsapi() -> None:
    """get_ingester must return ingest_newsapi_events for newsapi."""
    from quant_foundry.data_ingestion import get_ingester, ingest_newsapi_events

    assert get_ingester("newsapi_events") is ingest_newsapi_events


def test_vendor_registry_contains_all_vendors() -> None:
    """VENDOR_INGESTERS must contain all six vendor keys."""
    from quant_foundry.data_ingestion import VENDOR_INGESTERS

    expected = {
        "equity_bars",
        "news_events",
        "macro_indicators",
        "alpaca_equity_bars",
        "fred_macro",
        "newsapi_events",
    }
    assert expected.issubset(set(VENDOR_INGESTERS.keys()))


# ---------------------------------------------------------------------------
# Missing API key errors
# ---------------------------------------------------------------------------


async def test_alpaca_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_alpaca_bars must raise ValueError when credentials are missing."""
    monkeypatch.delenv("FINCEPT_ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("FINCEPT_ALPACA_API_SECRET", raising=False)

    from quant_foundry.data_ingestion.alpaca_bars import fetch_alpaca_bars

    with pytest.raises(ValueError, match="Alpaca API key not provided"):
        await fetch_alpaca_bars(
            symbols=["AAPL"],
            start="2024-01-01",
            end="2024-06-30",
        )


async def test_fred_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_fred_series must raise ValueError when the key is missing."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    from quant_foundry.data_ingestion.fred_macro import fetch_fred_series

    with pytest.raises(ValueError, match="FRED API key not provided"):
        await fetch_fred_series(
            series_ids=["FEDFUNDS"],
            start="2020-01-01",
            end="2024-01-01",
        )


async def test_newsapi_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_newsapi_articles must raise ValueError when the key is missing."""
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)

    from quant_foundry.data_ingestion.news_vendor import fetch_newsapi_articles

    with pytest.raises(ValueError, match="NewsAPI key not provided"):
        await fetch_newsapi_articles(
            query="stock market",
            start="2024-01-01",
            end="2024-06-30",
        )


# ---------------------------------------------------------------------------
# Env var fallback
# ---------------------------------------------------------------------------


async def test_alpaca_env_var_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """fetch_alpaca_bars must read credentials from env vars."""
    monkeypatch.setenv("FINCEPT_ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("FINCEPT_ALPACA_API_SECRET", "test_secret")
    _patch_async_client(monkeypatch, _alpaca_handler)

    from quant_foundry.data_ingestion.alpaca_bars import fetch_alpaca_bars

    out_path = await fetch_alpaca_bars(
        symbols=["AAPL"],
        start="2024-01-01",
        end="2024-03-01",
        # No api_key/api_secret -> must fall back to env vars.
    )
    assert out_path.exists()
    assert out_path.suffix == ".parquet"


async def test_fred_env_var_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_fred_series must read the API key from the env var."""
    monkeypatch.setenv("FRED_API_KEY", "test_fred_key")
    _patch_async_client(monkeypatch, _fred_handler)

    from quant_foundry.data_ingestion.fred_macro import fetch_fred_series

    out_path = await fetch_fred_series(
        series_ids=["FEDFUNDS"],
        start="2020-01-01",
        end="2024-01-01",
        # No api_key -> must fall back to env var.
    )
    assert out_path.exists()
    assert out_path.suffix == ".csv"
    # CSV must have the expected header.
    text = out_path.read_text(encoding="utf-8")
    assert text.startswith("date,indicator,value")


async def test_newsapi_env_var_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_newsapi_articles must read the API key from the env var."""
    monkeypatch.setenv("NEWSAPI_KEY", "test_news_key")
    _patch_async_client(monkeypatch, _newsapi_handler)

    from quant_foundry.data_ingestion.news_vendor import fetch_newsapi_articles

    out_path = await fetch_newsapi_articles(
        query="stock market",
        start="2024-01-01",
        end="2024-06-30",
        # No api_key -> must fall back to env var.
    )
    assert out_path.exists()
    assert out_path.suffix == ".json"
    body = json.loads(out_path.read_text(encoding="utf-8"))
    assert "articles" in body
    assert len(body["articles"]) > 0


# ---------------------------------------------------------------------------
# fetch_* with mocked httpx (explicit credentials)
# ---------------------------------------------------------------------------


async def test_fetch_alpaca_bars_mocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_alpaca_bars must return a parquet file with mapped columns."""
    _patch_async_client(monkeypatch, _alpaca_handler)
    pytest.importorskip("polars")

    import polars as pl
    from quant_foundry.data_ingestion.alpaca_bars import fetch_alpaca_bars

    out_path = await fetch_alpaca_bars(
        symbols=["AAPL", "MSFT"],
        start="2024-01-01",
        end="2024-03-01",
        api_key="k",
        api_secret="s",
    )
    assert out_path.exists()

    df = pl.read_parquet(str(out_path))
    assert df.height > 0
    for col in ("symbol", "date", "ts_event", "open", "high", "low", "close", "volume"):
        assert col in df.columns
    # Both symbols must be present.
    assert set(df["symbol"].unique().to_list()) == {"AAPL", "MSFT"}


async def test_fetch_fred_series_mocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_fred_series must return a CSV with date, indicator, value."""
    _patch_async_client(monkeypatch, _fred_handler)

    from quant_foundry.data_ingestion.fred_macro import fetch_fred_series

    out_path = await fetch_fred_series(
        series_ids=["FEDFUNDS", "CPIAUCSL"],
        start="2020-01-01",
        end="2024-01-01",
        api_key="k",
    )
    assert out_path.exists()

    import csv

    with out_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["date", "indicator", "value"]
        rows = list(reader)
    assert len(rows) > 0
    indicators = {r["indicator"] for r in rows}
    assert indicators == {"FEDFUNDS", "CPIAUCSL"}


async def test_fetch_newsapi_articles_mocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch_newsapi_articles must return a JSON with normalized articles."""
    _patch_async_client(monkeypatch, _newsapi_handler)

    from quant_foundry.data_ingestion.news_vendor import fetch_newsapi_articles

    out_path = await fetch_newsapi_articles(
        query="stock market",
        start="2024-01-01",
        end="2024-06-30",
        api_key="k",
    )
    assert out_path.exists()

    body = json.loads(out_path.read_text(encoding="utf-8"))
    assert "articles" in body
    articles = body["articles"]
    assert len(articles) > 0
    # Normalized rows must have the keys the news loader expects.
    first = articles[0]
    for key in ("headline", "body", "source", "published_at", "url"):
        assert key in first


# ---------------------------------------------------------------------------
# ingest_* with mocked API -> IngestionResult
# ---------------------------------------------------------------------------

_POLARS = pytest.importorskip("polars")
_NUMPY = pytest.importorskip("numpy")


async def test_ingest_alpaca_equity_bars_mocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """ingest_alpaca_equity_bars must produce a valid IngestionResult."""
    _patch_async_client(monkeypatch, _alpaca_handler)

    from quant_foundry.data_ingestion import ingest_alpaca_equity_bars

    result = await ingest_alpaca_equity_bars(
        symbols=["AAPL", "MSFT"],
        start="2024-01-01",
        end="2024-04-01",
        output_dir=tmp_path / "out",
        dataset_id="test_alpaca_ingest",
        label_horizon_days=5,
        n_folds=3,
        api_key="k",
        api_secret="s",
    )

    assert result.parquet_path.exists()
    assert result.manifest_path.exists()
    assert result.receipt_path.exists()
    assert result.quality_path.exists()

    import polars as pl

    df = pl.read_parquet(str(result.parquet_path))
    assert df.height > 0
    assert "decision_time" in df.columns
    assert "label" in df.columns
    for feat in ("ret_1d", "ret_5d", "vol_20d", "mom_10d", "vol_ratio"):
        assert feat in df.columns

    assert result.manifest.pit_proof_verified is True
    assert result.quality_report.total_rows == df.height
    assert result.quality_report.pit_proof_verified is True


async def test_ingest_fred_macro_mocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """ingest_fred_macro must produce a valid IngestionResult."""
    _patch_async_client(monkeypatch, _fred_handler)

    from quant_foundry.data_ingestion import ingest_fred_macro

    result = await ingest_fred_macro(
        series_ids=["FEDFUNDS", "CPIAUCSL"],
        start="2020-01-01",
        end="2024-01-01",
        output_dir=tmp_path / "out",
        dataset_id="test_fred_ingest",
        n_folds=3,
        api_key="k",
    )

    assert result.parquet_path.exists()
    assert result.manifest_path.exists()
    assert result.receipt_path.exists()
    assert result.quality_path.exists()

    import polars as pl

    df = pl.read_parquet(str(result.parquet_path))
    assert df.height > 0
    assert "decision_time" in df.columns
    assert "label" in df.columns
    for feat in ("value", "value_diff_1", "value_pct_change_1"):
        assert feat in df.columns

    assert result.manifest.pit_proof_verified is True
    assert result.quality_report.total_rows == df.height
    assert result.quality_report.pit_proof_verified is True


async def test_ingest_newsapi_events_mocked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """ingest_newsapi_events must produce a valid IngestionResult."""
    _patch_async_client(monkeypatch, _newsapi_handler)

    from quant_foundry.data_ingestion import ingest_newsapi_events

    result = await ingest_newsapi_events(
        query="stock market",
        start="2024-01-01",
        end="2024-06-30",
        output_dir=tmp_path / "out",
        dataset_id="test_newsapi_ingest",
        n_folds=3,
        api_key="k",
    )

    assert result.parquet_path.exists()
    assert result.manifest_path.exists()
    assert result.receipt_path.exists()
    assert result.quality_path.exists()

    import polars as pl

    df = pl.read_parquet(str(result.parquet_path))
    assert df.height > 0
    assert "decision_time" in df.columns
    assert "label" in df.columns
    for feat in ("headline_len", "body_len", "sentiment_proxy", "event_type_count", "symbol_count"):
        assert feat in df.columns

    assert result.manifest.pit_proof_verified is True
    assert result.quality_report.total_rows == df.height
    assert result.quality_report.pit_proof_verified is True
