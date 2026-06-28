"""
quant_foundry.data_ingestion.vendors — vendor registry mapping vendor names to
their ingestion functions.

This module provides a single :data:`VENDOR_INGESTERS` registry and a
:func:`get_ingester` lookup so callers can dispatch to the correct ingestion
function by vendor name without importing every module at the call site.

Local-file ingestion functions (``ingest_equity_bars``, ``ingest_news_events``,
``ingest_macro_indicators``) are synchronous.  Vendor API adapters
(``ingest_alpaca_equity_bars``, ``ingest_fred_macro``, ``ingest_newsapi_events``)
are async.  The registry type is therefore ``Callable[..., Any]`` to
accommodate both; callers must ``await`` the async entries.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from quant_foundry.data_ingestion.alpaca_bars import ingest_alpaca_equity_bars
from quant_foundry.data_ingestion.equities import ingest_equity_bars
from quant_foundry.data_ingestion.fred_macro import ingest_fred_macro
from quant_foundry.data_ingestion.macro import ingest_macro_indicators
from quant_foundry.data_ingestion.news import ingest_news_events
from quant_foundry.data_ingestion.news_vendor import ingest_newsapi_events

#: Registry of vendor name -> ingestion function.  Every function returns an
#: :class:`IngestionResult` (sync) or a coroutine resolving to one (async).
#: Callers must ``await`` the async entries.
VENDOR_INGESTERS: dict[str, Callable[..., Any]] = {
    "equity_bars": ingest_equity_bars,
    "news_events": ingest_news_events,
    "macro_indicators": ingest_macro_indicators,
    "alpaca_equity_bars": ingest_alpaca_equity_bars,
    "fred_macro": ingest_fred_macro,
    "newsapi_events": ingest_newsapi_events,
}


def get_ingester(vendor: str) -> Callable[..., Any]:
    """Return the ingestion function for a vendor name.

    Parameters
    ----------
    vendor
        Vendor name key into :data:`VENDOR_INGESTERS`.

    Returns
    -------
    Callable[..., Any]
        The ingestion function for *vendor*.  Sync for local-file vendors,
        async (must be awaited) for vendor API adapters.

    Raises
    ------
    ValueError
        If *vendor* is not a known vendor name.
    """
    if vendor not in VENDOR_INGESTERS:
        raise ValueError(
            f"unknown vendor: {vendor!r}; available: {list(VENDOR_INGESTERS)}",
        )
    return VENDOR_INGESTERS[vendor]


__all__ = [
    "VENDOR_INGESTERS",
    "get_ingester",
]
